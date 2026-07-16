"""Transport-agnostic interactive gate: propose -> (confirm) -> execute.

Day 4/5 wired blast-radius simulation and undo into the MCP adapter's
*fire-and-hold-for-an-out-of-band-operator* flow (``ShadowSession``). Human Mode
(the TUI) and the future web UI need a different shape: an INTERACTIVE two-phase
gate where the *same* caller (a) proposes a statement and is shown the verdict
plus measured blast radius, then (b) explicitly confirms or abandons it. This
module is that gate.

It is built entirely from the existing engine primitives -- ``classify``,
``evaluate``, ``simulate``, ``apply_blast_radius``, ``execute_with_undo``,
``revert`` -- so the load-bearing policy/simulation/undo logic is never copied
into a UI. The TUI and a later web dashboard are thin renderers over
``Proposal`` and ``Result``; adding a transport never touches this file.

Latency (CLAUDE.md sec. 4): ``propose`` does the same cheap hot-path work as the
MCP path -- parse (cached) -> classify -> evaluate -- and only ever runs the
expensive, time-boxed simulation on a *risky write*. Reads and routine point
writes get an ``allow`` verdict with no simulation, so the interactive path
costs no more than the pass-through path for normal traffic. Writes fail closed,
reads fail open (sec. 4).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import asyncpg

from engine.classifier import WRITE, Classification, classify
from engine.policy import (
    Decision,
    Policy,
    apply_blast_radius,
    evaluate,
)
from engine.schema import (
    Decision as SchemaDecision,
)
from engine.schema import (
    ReasonCode as SchemaReasonCode,
)
from engine.schema import (
    decision_from_legacy_policy,
    impact_from_legacy_simulation,
    undo_from_execution,
)
from engine.security import client_db_error
from engine.simulate import is_risky_write, simulate
from engine.undo import UndoStore, execute_with_undo, revert

# Verdicts a Proposal can carry.
ALLOW = "allow"  # safe to run as-is
CONFIRM = "confirm"  # allowed, but blast radius needs an explicit human OK
BLOCK = "block"  # a policy violation; must not run

PASSTHROUGH = "passthrough"  # no policy configured -> nothing to enforce


def _verdict(decision: Decision, enforce: bool) -> str:
    """Collapse a Decision into a single interactive verdict.

    Observe mode never blocks or gates (it only logs), so it always reports
    ``allow`` -- the simulation/violations still ride along on the Proposal as
    advisory context for the UI.
    """
    if not enforce:
        return ALLOW
    if not decision.allowed:
        return BLOCK
    if decision.requires_confirmation:
        return CONFIRM
    return ALLOW


@dataclass(frozen=True)
class Proposal:
    """The result of evaluating a statement WITHOUT running it.

    Everything a UI needs to render the gate and, on confirm, to execute exactly
    what was previewed (``effective_sql`` -- which may differ from ``sql`` when a
    guardrail rewrote it, e.g. an injected LIMIT).
    """

    sql: str
    verdict: str  # ALLOW | CONFIRM | BLOCK | PASSTHROUGH
    effective_sql: str
    classification: Classification
    decision: Decision | None
    blast_radius: int | None  # affected rows (exact if measured, else estimate)
    blast_method: str | None  # "count" | "bounded" | "static" | "unsupported" | None
    blast_timed_out: bool
    violations: tuple[dict, ...]
    stated_task: str | None
    actor: str | None
    schema_decision: dict | None = None

    @property
    def is_write(self) -> bool:
        return bool(self.classification.statements) and (
            self.classification.statements[0].kind == WRITE
        )

    @property
    def needs_confirmation(self) -> bool:
        return self.verdict == CONFIRM

    @property
    def blocked(self) -> bool:
        return self.verdict == BLOCK

    def summary(self) -> dict[str, Any]:
        """Compact, render/audit-friendly view."""
        return {
            "sql": self.sql,
            "verdict": self.verdict,
            "effective_sql": self.effective_sql,
            "rewritten": self.effective_sql != self.sql,
            "blast_radius": self.blast_radius,
            "blast_method": self.blast_method,
            "blast_timed_out": self.blast_timed_out,
            "violations": list(self.violations),
            "is_write": self.is_write,
            "decision": self.schema_decision,
        }


@dataclass(frozen=True)
class Result:
    """What actually happened when a Proposal was executed (or refused)."""

    executed: bool
    status: str | None = None
    rows: list[dict[str, Any]] = field(default_factory=list)
    row_count: int = 0
    error: str | None = None
    refused: str | None = None  # None | "blocked" | "needs_confirmation"
    # Undo handle (Day 5): present when the write was captured reversibly.
    action_id: str | None = None
    reversible: bool | None = None
    undo_reason: str | None = None
    overridden: bool = False  # ran despite a BLOCK, on a deliberate human override
    schema_decision: dict | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "executed": self.executed,
            "status": self.status,
            "row_count": self.row_count,
            "error": self.error,
            "refused": self.refused,
            "action_id": self.action_id,
            "reversible": self.reversible,
            "undo_reason": self.undo_reason,
            "overridden": self.overridden,
            "decision": self.schema_decision,
        }


def _executed_schema_decision(
    proposal: Proposal,
    *,
    action_id: str | None,
    reversible: bool | None,
    undo_reason: str | None,
    overridden: bool,
) -> dict:
    impact = (
        impact_from_legacy_simulation(proposal.decision.simulation)
        if proposal.decision is not None
        else None
    )
    undo = undo_from_execution(
        handle=action_id, reversible=reversible, reason=undo_reason
    )
    if overridden:
        explanation = "Action executed after human override."
    elif proposal.needs_confirmation:
        explanation = "Action executed after interactive confirmation."
    else:
        explanation = "Action allowed and executed."
    return SchemaDecision.allow(
        reason_code=SchemaReasonCode.ALLOW,
        explanation=explanation,
        impact=impact,
        undo=undo,
    ).to_dict()


class GuardedSession:
    """Interactive, transport-agnostic safety gate.

    Usage::

        prop = await sess.propose(sql, actor="alice")
        if prop.blocked:
            ...                       # show prop.violations, refuse
        elif prop.needs_confirmation:
            # show prop.blast_radius, ask the human
            res = await sess.execute(prop, force=user_said_yes)
        else:
            res = await sess.execute(prop)

    ``execute`` will NOT run a CONFIRM proposal unless ``force=True`` (the human
    explicitly approved), and will NEVER run a BLOCK proposal. That guarantee is
    enforced here, in the engine -- a UI cannot accidentally bypass it.
    """

    def __init__(
        self,
        pool: asyncpg.Pool,
        policy: Policy | None,
        *,
        undo_store: UndoStore | None = None,
        unique_columns: frozenset[str] = frozenset(),
        audit=None,
        allow_override: bool = False,
    ) -> None:
        self._pool = pool
        self._policy = policy
        self._undo_store = undo_store
        self._unique_columns = unique_columns
        self._audit = audit  # optional AuditLog; record() is non-blocking
        # When True, a caller may deliberately run a BLOCKed statement via
        # execute(override=True). This is a HUMAN escape hatch (Human Mode opts
        # in); the MCP/agent path never constructs a session with it, so an agent
        # can never override a block. Off by default -- fail closed (sec. 4).
        self._allow_override = allow_override

    @property
    def _enforce(self) -> bool:
        # Fail closed: enforce unless the mode is *explicitly* observe.
        return self._policy is not None and self._policy.mode != "observe"

    def _record(self, event: dict) -> None:
        if self._audit is not None:
            self._audit.record(event)  # async, non-blocking (sec. 4)

    async def propose(
        self,
        sql: str,
        *,
        actor: str | None = None,
        stated_task: str | None = None,
    ) -> Proposal:
        """Evaluate + (for risky writes only) simulate. Does NOT execute."""
        classification = classify(sql)

        if self._policy is None:
            # No policy: behave as a pure pass-through preview.
            return Proposal(
                sql=sql,
                verdict=PASSTHROUGH,
                effective_sql=sql,
                classification=classification,
                decision=None,
                blast_radius=None,
                blast_method=None,
                blast_timed_out=False,
                violations=(),
                stated_task=stated_task,
                actor=actor,
                schema_decision=decision_from_legacy_policy(None).to_dict(),
            )

        decision = evaluate(sql, classification, self._policy)
        enforce = self._enforce

        # Blast-radius simulation -- OFF the normal path: only a risky write,
        # only when enforcing and enabled. Mirrors the MCP gate exactly.
        if (
            decision.allowed
            and enforce
            and self._policy.simulation.enabled
            and is_risky_write(classification, self._unique_columns)
        ):
            async with self._pool.acquire() as conn:
                sim = await simulate(
                    conn,
                    sql,
                    classification,
                    self._policy.simulation,
                    self._unique_columns,
                )
            decision = apply_blast_radius(decision, sim, self._policy.simulation)

        sim_dict = decision.simulation or {}
        proposal = Proposal(
            sql=sql,
            verdict=_verdict(decision, enforce),
            effective_sql=decision.effective_sql if enforce else sql,
            classification=classification,
            decision=decision,
            blast_radius=sim_dict.get("affected_rows"),
            blast_method=sim_dict.get("method"),
            blast_timed_out=bool(sim_dict.get("timed_out")),
            violations=tuple(v.to_dict() for v in decision.violations),
            stated_task=stated_task,
            actor=actor,
            schema_decision=decision_from_legacy_policy(
                decision,
                approval_id=(
                    "interactive-confirmation"
                    if enforce and decision.requires_confirmation
                    else None
                ),
            ).to_dict(),
        )
        self._record(
            {
                "event": "propose",
                "actor": actor,
                "stated_task": stated_task,
                "sql": sql,
                "verdict": proposal.verdict,
                "blast_radius": proposal.blast_radius,
                "violations": list(proposal.violations),
            }
        )
        return proposal

    def _undo_enabled(self, classification: Classification) -> bool:
        return (
            self._undo_store is not None
            and self._policy is not None
            and self._policy.undo.enabled
            and classification.statement_count == 1
            and bool(classification.statements)
            and classification.statements[0].kind == WRITE
        )

    async def execute(
        self,
        proposal: Proposal,
        *,
        actor: str | None = None,
        force: bool = False,
        override: bool = False,
    ) -> Result:
        """Run a proposal, honoring its verdict.

        * BLOCK  -> never runs, UNLESS ``override=True`` *and* this session was
          built with ``allow_override=True`` (the human escape hatch). An
          overridden write still goes through undo capture when its shape allows,
          so it stays revertible, and the bypassed violations are audited.
        * CONFIRM -> runs only when ``force=True`` (the human said yes) or under a
          deliberate ``override``; otherwise ``refused="needs_confirmation"``.
        * ALLOW / PASSTHROUGH -> runs.
        """
        can_override = override and self._allow_override
        if proposal.verdict == BLOCK and not can_override:
            self._record(
                {
                    "event": "execute_refused",
                    "actor": actor or proposal.actor,
                    "sql": proposal.sql,
                    "reason": "blocked",
                    "violations": list(proposal.violations),
                }
            )
            return Result(
                executed=False,
                refused="blocked",
                schema_decision=proposal.schema_decision,
            )
        if proposal.verdict == CONFIRM and not force and not can_override:
            return Result(
                executed=False,
                refused="needs_confirmation",
                schema_decision=proposal.schema_decision,
            )

        overridden = proposal.verdict == BLOCK and can_override
        if overridden:
            # A human is deliberately bypassing a block. Audit it loudly with the
            # exact violations being overridden -- this is the accountability that
            # makes the escape hatch acceptable (sec. 11).
            self._record(
                {
                    "event": "execute_override",
                    "actor": actor or proposal.actor,
                    "sql": proposal.sql,
                    "violations": list(proposal.violations),
                }
            )

        effective_sql = proposal.effective_sql
        classification = proposal.classification
        actor = actor or proposal.actor

        status: str | None = None
        rows: list[dict[str, Any]] = []
        error: str | None = None
        action_id: str | None = None
        reversible: bool | None = None
        undo_reason: str | None = None

        try:
            async with self._pool.acquire() as conn:
                if self._undo_enabled(classification):
                    # Capture before/after images and execute in one transaction
                    # so the write can be reverted (Day 5).
                    outcome = await execute_with_undo(
                        conn,
                        effective_sql,
                        classification,
                        agent=actor,
                        stated_task=proposal.stated_task,
                        config=self._policy.undo,
                        store=self._undo_store,
                    )
                    status, rows, error = outcome.status, outcome.rows, outcome.error
                    action_id = outcome.action_id
                    reversible = outcome.reversible
                    undo_reason = None if outcome.reversible else outcome.reason
                    if outcome.blocked:
                        # Non-reversible write refused before execution.
                        self._record(
                            {
                                "event": "execute_refused",
                                "actor": actor,
                                "sql": proposal.sql,
                                "reason": "non_reversible",
                                "undo_reason": undo_reason,
                            }
                        )
                        return Result(
                            executed=False,
                            refused="non_reversible",
                            reversible=False,
                            undo_reason=undo_reason,
                            overridden=overridden,
                            schema_decision=SchemaDecision.deny(
                                reason_code=SchemaReasonCode.NON_REVERSIBLE_WRITE,
                                explanation=(
                                    "Write was not executed because it cannot be "
                                    "recorded for safe undo."
                                ),
                                repair_hint=(
                                    "Use a simple INSERT, UPDATE, or DELETE on a "
                                    "primary-keyed table, or explicitly disable "
                                    "undo.block_non_reversible for local evaluation."
                                ),
                            ).to_dict(),
                        )
                else:
                    # prepare()+fetch() runs the statement once and exposes both
                    # the rows and the command tag.
                    stmt = await conn.prepare(effective_sql)
                    records = await stmt.fetch()
                    status = stmt.get_statusmsg()
                    rows = [dict(r) for r in records]
        except asyncpg.PostgresError as exc:
            error = client_db_error(exc)

        self._record(
            {
                "event": "execute",
                "actor": actor,
                "stated_task": proposal.stated_task,
                "sql": proposal.sql,
                "effective_sql": (
                    effective_sql if effective_sql != proposal.sql else None
                ),
                "status": status,
                "error": error,
                "action_id": action_id,
                "reversible": reversible,
                "confirmed": force,
                "overridden": overridden,
            }
        )
        return Result(
            executed=error is None,
            status=status,
            rows=rows,
            row_count=len(rows),
            error=error,
            action_id=action_id,
            reversible=reversible,
            undo_reason=undo_reason,
            overridden=overridden,
            schema_decision=_executed_schema_decision(
                proposal,
                action_id=action_id,
                reversible=reversible,
                undo_reason=undo_reason,
                overridden=overridden,
            ),
        )

    async def revert(self, action_id: str, *, actor: str | None = None):
        """Reverse a previously executed write by its undo handle (Day 5)."""
        if self._undo_store is None:
            raise RuntimeError("undo is not enabled for this session")
        async with self._pool.acquire() as conn:
            result = await revert(conn, action_id, self._undo_store, agent=actor)
        self._record(
            {
                "event": "revert",
                "actor": actor,
                "action_id": action_id,
                "ok": result.ok,
                "error": result.error,
            }
        )
        return result
