"""MCP transport adapter for database query execution."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import sys
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import asyncpg
from mcp.server.fastmcp import Context, FastMCP

from engine.approvals import (
    APPROVED,
    DEFAULT_TTL_SECONDS,
    ApprovalStore,
    token_hash,
)
from engine.audit import AuditLog
from engine.classifier import DDL, WRITE, classify
from engine.intent import check_intent, llm_second_opinion
from engine.migration import migrate_legacy_control
from engine.object_identity import classification_tables, load_object_fingerprint
from engine.policy import (
    Policy,
    ReasonCode,
    apply_approval_ceiling,
    apply_blast_radius,
    apply_intent,
    apply_undo_capture_limit,
    evaluate,
    policy_fingerprint,
)
from engine.runtime_security import (
    PRODUCTION,
    inspect_database_security,
    production_errors,
    validate_profile,
)
from engine.schema import (
    Decision as SchemaDecision,
)
from engine.schema import (
    PrincipalKind,
    decision_from_legacy_policy,
    principal_from_legacy,
)
from engine.schema import (
    ReasonCode as SchemaReasonCode,
)
from engine.security import client_db_error, diagnostic_error, redact_text
from engine.simulate import is_risky_write, load_unique_columns, simulate
from engine.undo import UndoStore, execute_with_undo, revert

# --- Config (env-overridable; defaults match docker-compose.yml) -------------
DB_DSN = os.environ.get(
    "AGENT_DB_DSN",
    "postgresql://postgres:postgres@localhost:5433/pagila",
)
CONTROL_DSN = os.environ.get("AGENT_CONTROL_DSN")
CONTROL_SCHEMA = "interdict_control"
SAFETY_PROFILE = os.environ.get("AGENT_SAFETY_PROFILE", PRODUCTION)
OPERATOR_ID = os.environ.get("AGENT_OPERATOR_ID")
# Default to a stable per-user location: MCP clients launch this server from an
# arbitrary cwd, so a relative default would scatter logs (or hide them).
AUDIT_LOG_PATH = os.environ.get(
    "AGENT_AUDIT_LOG",
    str(Path.home() / ".interdict" / "audit.jsonl"),
)
# Policy file loaded once at startup (off the hot path). Defaults to the
# repo's default policy.
POLICY_PATH = os.environ.get(
    "AGENT_POLICY",
    str(Path(__file__).resolve().parent.parent / "policies" / "default.yaml"),
)
# Pool is opened once at startup; per-query cost is just an acquire (cheap when
# a connection is free). Sized small for dev.
POOL_MIN_SIZE = int(os.environ.get("AGENT_POOL_MIN", "1"))
POOL_MAX_SIZE = int(os.environ.get("AGENT_POOL_MAX", "10"))
OPERATOR_TOKEN = os.environ.get("AGENT_OPERATOR_TOKEN")
MIN_OPERATOR_TOKEN_LENGTH = 32
# How long a held write stays approvable. After this, its measured blast
# radius is considered stale and the agent must re-run the query.
APPROVAL_TTL_SECONDS = int(
    os.environ.get("AGENT_APPROVAL_TTL_SECONDS", str(DEFAULT_TTL_SECONDS))
)


def _simulation_summary(simulation: dict | None) -> str:
    if not simulation:
        return "blast radius not measured"
    if simulation.get("timed_out"):
        return "blast radius unknown because simulation timed out"
    rows = simulation.get("affected_rows")
    if rows is None:
        return "blast radius unknown"
    return f"blast radius {rows:,} row(s)"


def _blocked_summary(reason: str | None, message: str | None) -> str:
    if reason and message:
        return f"Interdict blocked this before Postgres: {reason} - {message}"
    if reason:
        return f"Interdict blocked this before Postgres: {reason}"
    return "Interdict blocked this before Postgres."


def _held_summary(approval_id: str, simulation: dict | None) -> str:
    return (
        "This write requires out-of-band approval (the operator token must "
        "never enter this chat). "
        f"approval_id={approval_id}. "
        f"{_simulation_summary(simulation)}.\n\n"
        "NEXT STEPS:\n"
        "1. In YOUR terminal (not here): "
        f"AGENT_OPERATOR_TOKEN=your_token interdict approve {approval_id}\n"
        "2. Then in this chat: call "
        f'run_approved_query(approval_id="{approval_id}")\n'
        "   (No token needed - the approval already happened)"
    )


def _executed_summary(
    status: str | None,
    row_count: int,
    error: str | None,
    action_id: str | None,
) -> str:
    if error:
        return f"Interdict allowed the statement, but Postgres returned: {error}"
    if action_id:
        return (
            f"Interdict allowed and executed {status or 'the write'}. "
            f'undo_id={action_id} - call request_revert(action_id="{action_id}") '
            "to ask a human to approve reversing this change."
        )
    if status:
        return f"Interdict allowed and executed {status}. returned {row_count} row(s)."
    return f"Interdict allowed the statement. returned {row_count} row(s)."


class ShadowSession:
    """Execute a SQL statement, log the result, and return the response."""

    def __init__(
        self,
        pool: asyncpg.Pool,
        audit: AuditLog,
        policy: Policy | None = None,
        undo_store: UndoStore | None = None,
        llm_assessor=None,
        unique_columns: frozenset[str] = frozenset(),
        operator_token: str | None = None,
        approval_store: ApprovalStore | None = None,
        control_pool: asyncpg.Pool | None = None,
    ) -> None:
        self._pool = pool
        self._audit = audit
        self._policy = policy
        self._undo_store = undo_store
        # "table.column" set of single-column unique/PK columns (loaded at
        # startup). A point write is only routine when scoped to one of these;
        # otherwise it's simulated. Empty => every scoped write is simulated.
        self._unique_columns = unique_columns
        # Optional async callable (prompt)->str for the advisory LLM second
        # opinion. None (default) => the LLM check never runs.
        self._llm_assessor = llm_assessor
        self._operator_token = operator_token
        self._control_pool = control_pool or pool
        # Held writes are persisted (sidecar schema) so a human can approve
        # them from their own terminal -- the operator token never enters the
        # agent chat. The store is the single source of truth for holds.
        self._approvals = approval_store or ApprovalStore(
            policy.undo.schema if policy else "adb_undo",
            ttl_seconds=APPROVAL_TTL_SECONDS,
        )
        self._bg_tasks: set[asyncio.Task] = set()

    def _operator_allowed(self, token: str | None) -> bool:
        """True only when operator approval is enabled and the token matches."""
        if (
            not self._operator_token
            or len(self._operator_token) < MIN_OPERATOR_TOKEN_LENGTH
            or token is None
        ):
            return False
        return secrets.compare_digest(token, self._operator_token)

    @staticmethod
    def _approval_summary(row: dict[str, Any]) -> dict[str, Any]:
        """Operator-safe view of a persisted hold (no token material)."""
        simulation = row.get("simulation")
        if isinstance(simulation, str):
            simulation = json.loads(simulation)
        principal = row.get("principal")
        if isinstance(principal, str):
            principal = json.loads(principal)
        return {
            "approval_id": str(row["approval_id"]),
            "sql": row["sql"],
            "stated_task": row.get("stated_task"),
            "agent": row.get("agent"),
            "principal": principal,
            "created_at": (
                row["created_at"].isoformat() if row.get("created_at") else None
            ),
            "simulation": simulation,
            "action_kind": row.get("action_kind", "sql"),
            "undo_action_id": (
                str(row["undo_action_id"]) if row.get("undo_action_id") else None
            ),
        }

    def _maybe_schedule_llm(
        self, stated_task, classification, affected, agent, flag=None
    ) -> None:
        """Run an asynchronous advisory check without blocking query execution."""
        cfg = self._policy.intent if self._policy else None
        if not (cfg and cfg.llm_enabled and self._llm_assessor and stated_task):
            return
        if not is_risky_write(classification, self._unique_columns):
            return
        if flag is None:
            flag = check_intent(
                stated_task,
                classification,
                affected,
                cfg,
                table_vocab=self._policy.allowed_tables,
            )
        if flag.severity != "high":
            return
        if len(self._bg_tasks) >= cfg.llm_max_concurrent:
            return  # shed load rather than pile up unbounded background work

        async def _run() -> None:
            try:
                opinion = await asyncio.wait_for(
                    llm_second_opinion(
                        stated_task, classification, affected, self._llm_assessor
                    ),
                    timeout=cfg.llm_timeout_s,
                )
            except TimeoutError:
                opinion = "llm second opinion timed out"
            if opinion is not None:
                self._audit.record(
                    {"event": "intent_llm", "agent": agent, "opinion": opinion}
                )

        task = asyncio.create_task(_run())
        self._bg_tasks.add(task)  # keep a ref so it isn't GC'd mid-flight
        task.add_done_callback(self._bg_tasks.discard)

    def _undo_enabled(self, classification) -> bool:
        """True when this statement should run through the undo-capture path."""
        return (
            self._undo_store is not None
            and self._policy is not None
            and self._policy.undo.enabled
            and classification.statement_count == 1
            and bool(classification.statements)
            and classification.statements[0].kind == WRITE
        )

    async def run_query(
        self,
        sql: str,
        *,
        stated_task: str | None = None,
        agent: str | None = None,
        operator_approved: bool = False,
        approved_impact_ceiling: int | None = None,
    ) -> dict[str, Any]:
        """Evaluate policy, optionally simulate risky writes, then execute."""
        # Parse + classify on the hot path -- both LRU-cached, pure, ~0.1 ms cold.
        classification = classify(sql)

        # Deterministic policy decision (pure in-memory, no I/O). With no policy,
        # behave as a pass-through (allow everything, rewrite nothing).
        decision = (
            evaluate(sql, classification, self._policy)
            if self._policy is not None
            else None
        )
        # Fail closed: enforce unless the mode is *explicitly* "observe". An
        # unknown/typo'd mode therefore enforces rather than silently passing
        # traffic through (Policy also rejects invalid modes at load time).
        enforce = self._policy is not None and self._policy.mode != "observe"
        # Reserved control-plane schemas are a security boundary, not a tunable
        # policy.  Observe mode may shadow ordinary rules, but it must never let
        # an agent edit its own approvals or undo history.
        hard_deny = bool(
            decision
            and any(
                v.reason_code == ReasonCode.CONTROL_PLANE for v in decision.violations
            )
        )

        # Blast-radius simulation (Day 4) -- OFF the normal path: only a risky
        # write, only when enforcing and enabled. May escalate the decision to
        # blocked (over the block limit) or to requires_confirmation (sec. 4).
        if (
            decision is not None
            and decision.allowed
            and enforce
            and self._policy.simulation.enabled
            and classification.statement_count == 1
            and bool(classification.statements)
            and classification.statements[0].kind == WRITE
        ):
            async with self._pool.acquire() as sim_conn:
                sim = await simulate(
                    sim_conn,
                    sql,
                    classification,
                    self._policy.simulation,
                    self._unique_columns,
                )
            decision = apply_blast_radius(decision, sim, self._policy.simulation)
            if self._policy.undo.enabled:
                decision = apply_undo_capture_limit(decision, self._policy.undo)
            decision = apply_approval_ceiling(decision, approved_impact_ceiling)

        # Intent-mismatch (Day 6) -- ADVISORY. Deterministic, in-memory, no I/O.
        # Compares the stated task to the statement's effect (blast radius from
        # simulation if measured). A HIGH contradiction may escalate to human
        # confirmation; it NEVER blocks on its own (sec. 11).
        if (
            decision is not None
            and self._policy.intent.enabled
            and classification.statements
            and classification.statements[0].kind in (WRITE, DDL)
        ):
            affected = (
                decision.simulation.get("affected_rows")
                if decision.simulation
                else None
            )
            flag = check_intent(
                stated_task,
                classification,
                affected,
                self._policy.intent,
                table_vocab=self._policy.allowed_tables,
            )
            decision = apply_intent(decision, flag, self._policy.intent)
            # Optional out-of-band LLM second opinion: scheduled (never awaited),
            # and only on the risky/HIGH subset (see _maybe_schedule_llm).
            self._maybe_schedule_llm(stated_task, classification, affected, agent, flag)

        # Blocked + enforcing: reject without going near the database.
        if decision is not None and not decision.allowed and (enforce or hard_deny):
            violations = [v.to_dict() for v in decision.violations]
            primary_violation = violations[0] if violations else None
            block_reason = (
                primary_violation["reason_code"] if primary_violation else None
            )
            block_message = primary_violation["message"] if primary_violation else None
            schema_decision = decision_from_legacy_policy(decision).to_dict()
            self._audit.record(
                {
                    "event": "query",
                    "agent": agent,
                    "stated_task": stated_task,
                    "sql": sql,
                    "blocked": True,
                    "violations": violations,
                    "simulation": decision.simulation,
                    "intent": decision.intent,
                    "decision": schema_decision,
                    "classification": classification.to_dict(),
                }
            )
            return {
                "status": None,
                "rows": [],
                "row_count": 0,
                "error": None,
                "blocked": True,
                "summary": _blocked_summary(block_reason, block_message),
                "block_reason": block_reason,
                "block_message": block_message,
                "violations": violations,
                "requires_confirmation": False,
                "simulation": decision.simulation,
                "intent": decision.intent,
                "decision": schema_decision,
            }

        # Allowed but gated: a risky write whose blast radius needs confirmation.
        # Held until an out-of-band operator approves (not the agent). Never
        # touches the DB otherwise.
        if (
            decision is not None
            and decision.requires_confirmation
            and enforce
            and not operator_approved
        ):
            approval_id = str(uuid4())
            async with self._pool.acquire() as identity_conn:
                object_fingerprint = await load_object_fingerprint(
                    identity_conn, classification_tables(classification)
                )
            schema_decision = decision_from_legacy_policy(
                decision, approval_id=approval_id
            ).to_dict()
            # Persist the hold so a human can approve it out-of-band
            # (`interdict approve <id>` in their own terminal). The token
            # itself never enters the chat; only its hash is stored.
            async with self._control_pool.acquire() as conn:
                await self._approvals.create(
                    conn,
                    approval_id=approval_id,
                    sql=sql,
                    stated_task=stated_task,
                    agent=agent,
                    principal=principal_from_legacy(
                        agent, stated_task=stated_task
                    ).to_dict(),
                    simulation=decision.simulation,
                    operator_token_hash=(
                        token_hash(self._operator_token)
                        if self._operator_token
                        else None
                    ),
                    policy_sha256=policy_fingerprint(self._policy),
                    approved_rows=(
                        decision.simulation.get("affected_rows")
                        if decision.simulation
                        else None
                    ),
                    object_fingerprint=object_fingerprint,
                )
            self._audit.record(
                {
                    "event": "query",
                    "agent": agent,
                    "stated_task": stated_task,
                    "sql": sql,
                    "blocked": False,
                    "requires_confirmation": True,
                    "approval_id": approval_id,
                    "simulation": decision.simulation,
                    "intent": decision.intent,
                    "decision": schema_decision,
                    "classification": classification.to_dict(),
                }
            )
            return {
                "status": None,
                "rows": [],
                "row_count": 0,
                "error": None,
                "blocked": False,
                "summary": _held_summary(approval_id, decision.simulation),
                "violations": [],
                "requires_confirmation": True,
                "approval_id": approval_id,
                "simulation": decision.simulation,
                "intent": decision.intent,
                "decision": schema_decision,
            }

        # Allowed, or observe-mode: decide what actually runs. Only *enforcing*
        # mode applies a rewrite (e.g. injected LIMIT); observe mode must run the
        # original SQL unchanged so a shadow rollout never alters live results --
        # the decision's would-be effective_sql is still logged below.
        if enforce and decision is not None:
            effective_sql = decision.effective_sql
            rewritten = decision.rewritten
        else:
            effective_sql = sql
            rewritten = False

        started = time.perf_counter()
        status: str | None = None
        rows: list[dict[str, Any]] = []
        error: str | None = None
        action_id: str | None = None
        reversible: bool | None = None
        undo_reason: str | None = None

        try:
            async with self._pool.acquire() as conn:
                if self._undo_enabled(classification):
                    # Reversibility (Day 5): capture before/after images so this
                    # write can be reverted, then execute -- all in one
                    # transaction. Write path only; reads never reach here (sec. 4).
                    if self._control_pool is self._pool:
                        outcome = await execute_with_undo(
                            conn,
                            effective_sql,
                            classification,
                            agent=agent,
                            stated_task=stated_task,
                            config=self._policy.undo,
                            store=self._undo_store,
                        )
                    else:
                        async with self._control_pool.acquire() as control_conn:
                            outcome = await execute_with_undo(
                                conn,
                                effective_sql,
                                classification,
                                agent=agent,
                                stated_task=stated_task,
                                config=self._policy.undo,
                                store=self._undo_store,
                                control_conn=control_conn,
                            )
                    status, rows, error = outcome.status, outcome.rows, outcome.error
                    action_id, reversible = outcome.action_id, outcome.reversible
                    # When not reversible, tell the agent why (structured).
                    undo_reason = None if outcome.reversible else outcome.reason
                    if outcome.blocked:
                        violation = {
                            "reason_code": ReasonCode.NON_REVERSIBLE_WRITE,
                            "message": (
                                "Write was not executed because it cannot be "
                                "recorded for safe undo."
                            ),
                            "suggested_fix": (
                                "Use a simple INSERT, UPDATE, or DELETE on a "
                                "primary-keyed table, or explicitly disable "
                                "undo.block_non_reversible for local evaluation."
                            ),
                            "statement_index": 0,
                        }
                        schema_decision = SchemaDecision.deny(
                            reason_code=SchemaReasonCode.NON_REVERSIBLE_WRITE,
                            explanation=violation["message"],
                            repair_hint=violation["suggested_fix"],
                            impact=(
                                decision_from_legacy_policy(
                                    decision, confirmation_satisfied=True
                                ).impact
                                if decision is not None
                                else None
                            ),
                        ).to_dict()
                        self._audit.record(
                            {
                                "event": "query",
                                "agent": agent,
                                "stated_task": stated_task,
                                "sql": sql,
                                "blocked": True,
                                "violations": [violation],
                                "undo_reason": undo_reason,
                                "decision": schema_decision,
                                "classification": classification.to_dict(),
                            }
                        )
                        return {
                            "status": None,
                            "rows": [],
                            "row_count": 0,
                            "error": None,
                            "blocked": True,
                            "summary": _blocked_summary(
                                violation["reason_code"], violation["message"]
                            ),
                            "block_reason": violation["reason_code"],
                            "block_message": violation["message"],
                            "violations": [violation],
                            "requires_confirmation": False,
                            "simulation": (
                                decision.simulation if decision is not None else None
                            ),
                            "undo_action_id": None,
                            "reversible": False,
                            "undo_reason": undo_reason,
                            "intent": (
                                decision.intent if decision is not None else None
                            ),
                            "decision": schema_decision,
                        }
                else:
                    # prepare()+fetch() runs the statement once and exposes BOTH
                    # the returned rows and the command tag -- see DECISIONS.
                    stmt = await conn.prepare(effective_sql)
                    records = await stmt.fetch()
                    status = stmt.get_statusmsg()
                    rows = [dict(r) for r in records]
        except asyncpg.PostgresError as exc:
            error = client_db_error(exc)
            diagnostic = diagnostic_error(exc)
        else:
            diagnostic = None

        elapsed_ms = (time.perf_counter() - started) * 1000.0
        # Reaching this point means any hold was resolved: the operator approved
        # the write, or observe mode ran it. Project the executed record as
        # allow rather than re-raising the hold guard.
        schema_decision = decision_from_legacy_policy(
            decision,
            confirmation_satisfied=True,
            undo_handle=action_id,
            reversible=reversible,
            undo_reason=undo_reason,
        ).to_dict()

        # Non-blocking enqueue -- the query does not wait on this (sec. 4).
        self._audit.record(
            {
                "event": "query",
                "agent": agent,
                "stated_task": stated_task,
                "sql": sql,
                "effective_sql": effective_sql if rewritten else None,
                "status": status,
                "rows_returned": len(rows),
                "error": error,
                "error_detail": diagnostic,
                "duration_ms": round(elapsed_ms, 3),
                "blocked": False,
                "undo_action_id": action_id,
                "reversible": reversible,
                "undo_reason": undo_reason,
                "intent": decision.intent if decision is not None else None,
                "decision": schema_decision,
                "classification": classification.to_dict(),
            }
        )

        return {
            "status": status,
            "rows": rows,
            "row_count": len(rows),
            "error": error,
            "blocked": False,
            "summary": _executed_summary(status, len(rows), error, action_id),
            "violations": [],
            "requires_confirmation": False,
            "approval_id": None,
            "simulation": decision.simulation if decision is not None else None,
            "undo_action_id": action_id,
            "reversible": reversible,
            "undo_reason": undo_reason,
            "intent": decision.intent if decision is not None else None,
            "decision": schema_decision,
        }

    async def approve_query(
        self,
        approval_id: str,
        *,
        operator_token: str | None,
        operator: str | None = None,
    ) -> dict[str, Any]:
        """Approve and execute a held write using an operator token."""
        if not self._operator_allowed(operator_token):
            schema_decision = SchemaDecision.deny(
                reason_code=SchemaReasonCode.OPERATOR_APPROVAL_DENIED,
                explanation="Operator approval token is missing or invalid.",
                repair_hint="Provide the configured operator token.",
            ).to_dict()
            self._audit.record(
                {
                    "event": "approval_denied",
                    "operator": operator,
                    "approval_id": approval_id,
                    "decision": schema_decision,
                }
            )
            return {
                "ok": False,
                "approval_id": approval_id,
                "summary": (
                    "Interdict did not approve this write: operator token is "
                    "missing or invalid."
                ),
                "error": "operator approval token is missing or invalid",
                "decision": schema_decision,
            }
        async with self._control_pool.acquire() as conn:
            granted = await self._approvals.decide(
                conn,
                approval_id,
                approve=True,
                operator_token_hash=token_hash(operator_token),
                decided_by=operator or "operator",
            )
        if granted is None:
            schema_decision = SchemaDecision.deny(
                reason_code=SchemaReasonCode.OPERATOR_APPROVAL_DENIED,
                explanation="No pending approval exists for that id.",
                repair_hint=(
                    "List pending approvals and retry with a valid approval_id."
                ),
            ).to_dict()
            return {
                "ok": False,
                "approval_id": approval_id,
                "summary": (
                    "Interdict did not approve this write: no pending approval "
                    "exists for that id."
                ),
                "error": "no such pending approval",
                "decision": schema_decision,
            }
        async with self._control_pool.acquire() as conn:
            approved_row = await self._approvals.get(conn, approval_id)
        if approved_row and approved_row.get("action_kind") == "revert":
            return await self.run_approved_revert(approval_id, executor=operator)
        return await self.run_approved_query(approval_id, executor=operator)

    async def run_approved_query(
        self,
        approval_id: str,
        *,
        executor: str | None = None,
    ) -> dict[str, Any]:
        """Execute a hold that an operator has already approved out-of-band.

        Takes no token: the authorization already happened (``interdict
        approve`` from the operator's terminal, or ``approve_query``). The SQL
        comes from the persisted hold -- the caller cannot substitute a
        different statement. The approved->executed transition is atomic, so a
        hold can only ever execute once.
        """
        async with self._control_pool.acquire() as conn:
            row = await self._approvals.claim_approved(
                conn, approval_id, action_kind="sql"
            )
            if row is None:
                current = await self._approvals.get(conn, approval_id)
        if row is None:
            status = current["status"] if current else "unknown"
            if (
                current is not None
                and current.get("expired")
                and status
                in (
                    "pending",
                    "approved",
                )
            ):
                status = "expired"
            hints = {
                "pending": (
                    "This write has not been approved yet. Ask a human "
                    f"operator to run `interdict approve {approval_id}`."
                ),
                "denied": "An operator denied this write. Do not retry it.",
                "executed": "This approval was already executed once.",
                "executing": "This approval is already being executed.",
                "failed": "This approval failed and cannot be reused.",
                "approved": "This approval belongs to a different action type.",
                "expired": (
                    "This hold expired: its measured blast radius is stale. "
                    "Re-run the query to get a fresh measurement and a new "
                    "approval_id."
                ),
                "unknown": "No approval exists for that id.",
            }
            schema_decision = SchemaDecision.deny(
                reason_code=SchemaReasonCode.OPERATOR_APPROVAL_DENIED,
                explanation=hints.get(status, hints["unknown"]),
                repair_hint=hints.get(status, hints["unknown"]),
            ).to_dict()
            return {
                "ok": False,
                "approval_id": approval_id,
                "summary": f"Interdict did not execute: {hints.get(status)}",
                "error": f"approval status is {status}",
                "decision": schema_decision,
            }
        expected_sql_hash = row.get("sql_sha256")
        actual_sql_hash = hashlib.sha256(row["sql"].encode("utf-8")).hexdigest()
        stale_reason = None
        if expected_sql_hash and expected_sql_hash != actual_sql_hash:
            stale_reason = "held SQL failed its integrity check"
        elif row.get("policy_sha256") and row["policy_sha256"] != policy_fingerprint(
            self._policy
        ):
            stale_reason = "policy changed after approval"
        elif row.get("object_fingerprint"):
            held_fingerprint = row["object_fingerprint"]
            if isinstance(held_fingerprint, str):
                held_fingerprint = json.loads(held_fingerprint)
            current_classification = classify(row["sql"])
            async with self._pool.acquire() as identity_conn:
                current_fingerprint = await load_object_fingerprint(
                    identity_conn, classification_tables(current_classification)
                )
            if current_fingerprint != held_fingerprint:
                stale_reason = "database object identity changed after approval"
        if stale_reason:
            async with self._control_pool.acquire() as conn:
                await self._approvals.finish_execution(
                    conn,
                    approval_id,
                    succeeded=False,
                    failure_reason=stale_reason,
                )
            return {
                "ok": False,
                "approval_id": approval_id,
                "blocked": True,
                "summary": f"Interdict did not execute: {stale_reason}.",
                "error": stale_reason,
                "decision": SchemaDecision.deny(
                    reason_code=SchemaReasonCode.OPERATOR_APPROVAL_DENIED,
                    explanation=stale_reason,
                    repair_hint="Submit the query again for a fresh approval.",
                ).to_dict(),
            }
        self._audit.record(
            {
                "event": "approval_executed",
                "operator": row.get("decided_by"),
                "executor": executor,
                **self._approval_summary(row),
                "principal": principal_from_legacy(
                    row.get("decided_by"), kind=PrincipalKind.HUMAN.value
                ).to_dict(),
            }
        )
        result = await self.run_query(
            row["sql"],
            stated_task=row.get("stated_task"),
            agent=row.get("agent"),
            operator_approved=True,
            approved_impact_ceiling=row.get("approved_rows"),
        )
        succeeded = result.get("error") is None and not result.get("blocked", False)
        failure = (
            None
            if succeeded
            else (
                result.get("block_message") or result.get("error") or "execution denied"
            )
        )
        async with self._control_pool.acquire() as conn:
            await self._approvals.finish_execution(
                conn,
                approval_id,
                succeeded=succeeded,
                failure_reason=failure,
            )
        return {"ok": succeeded, "approval_id": approval_id, **result}

    async def pending_approvals(self) -> list[dict[str, Any]]:
        """Currently held writes (persisted), safe to show to an operator."""
        async with self._control_pool.acquire() as conn:
            rows = await self._approvals.list_pending(conn)
        return [self._approval_summary(row) for row in rows]

    async def request_revert(
        self,
        action_id: str,
        *,
        agent: str | None = None,
    ) -> dict[str, Any]:
        """Create a human-approval hold for an undo; never execute it here."""
        if self._undo_store is None:
            schema_decision = SchemaDecision.deny(
                reason_code=SchemaReasonCode.NON_REVERSIBLE_WRITE,
                explanation="Undo is not enabled for this session.",
                repair_hint="Enable undo before attempting to revert writes.",
            ).to_dict()
            return {
                "ok": False,
                "action_id": action_id,
                "summary": (
                    "Interdict could not revert this write: undo is not enabled."
                ),
                "error": "undo not enabled",
                "decision": schema_decision,
            }
        async with self._control_pool.acquire() as conn:
            record = await self._undo_store.get(conn, action_id)
            if record is None or record["status"] != "active":
                reason = "no active undo record exists for that action_id"
                return {
                    "ok": False,
                    "action_id": action_id,
                    "error": reason,
                    "summary": f"Interdict could not request a revert: {reason}.",
                    "decision": SchemaDecision.deny(
                        reason_code=SchemaReasonCode.NON_REVERSIBLE_WRITE,
                        explanation=reason,
                    ).to_dict(),
                }
            approval_id = str(uuid4())
            rows = int(record["row_count"])
            simulation = {
                "method": "recorded",
                "estimated_rows": None,
                "estimated_cost": None,
                "exact_rows": rows,
                "affected_rows": rows,
                "timed_out": False,
                "error": None,
            }
            await self._approvals.create(
                conn,
                approval_id=approval_id,
                sql=f"REVERT {action_id}",
                stated_task=f"Revert Interdict action {action_id}",
                agent=agent,
                principal=principal_from_legacy(agent).to_dict(),
                simulation=simulation,
                operator_token_hash=(
                    token_hash(self._operator_token) if self._operator_token else None
                ),
                policy_sha256=policy_fingerprint(self._policy),
                approved_rows=rows,
                action_kind="revert",
                undo_action_id=action_id,
            )
        self._audit.record(
            {
                "event": "revert_requested",
                "agent": agent,
                "principal": principal_from_legacy(agent).to_dict(),
                "action_id": action_id,
                "approval_id": approval_id,
            }
        )
        return {
            "ok": True,
            "action_id": action_id,
            "approval_id": approval_id,
            "requires_confirmation": True,
            "simulation": simulation,
            "summary": _held_summary(approval_id, simulation),
            "decision": SchemaDecision.hold_for_approval(
                approval_id=approval_id,
                reason_code=SchemaReasonCode.OPERATOR_APPROVAL_REQUIRED,
                explanation="Undo requires human approval before execution.",
            ).to_dict(),
        }

    async def run_approved_revert(
        self, approval_id: str, *, executor: str | None = None
    ) -> dict[str, Any]:
        """Execute exactly one human-approved undo request."""
        async with self._control_pool.acquire() as control_conn:
            row = await self._approvals.claim_approved(
                control_conn, approval_id, action_kind="revert"
            )
            if row is None:
                return {
                    "ok": False,
                    "approval_id": approval_id,
                    "error": "approval is not an approved revert",
                    "summary": "Interdict did not revert: approval is not ready.",
                }
            action_id = str(row["undo_action_id"])
            async with self._pool.acquire() as target_conn:
                result = await revert(
                    target_conn,
                    action_id,
                    self._undo_store,
                    agent=row.get("decided_by") or executor,
                    require_agent_match=False,
                    control_conn=(
                        control_conn if self._control_pool is not self._pool else None
                    ),
                    principal_kind=PrincipalKind.HUMAN.value,
                )
            await self._approvals.finish_execution(
                control_conn,
                approval_id,
                succeeded=result.ok,
                failure_reason=result.error,
            )
        payload = result.to_dict()
        payload.update(
            {
                "approval_id": approval_id,
                "summary": (
                    f"Interdict reverted {action_id}: restored "
                    f"{result.rows_restored} row(s)."
                    if result.ok
                    else f"Interdict could not revert {action_id}: {result.error}"
                ),
            }
        )
        return payload

    async def revert_write(
        self, action_id: str, *, agent: str | None = None
    ) -> dict[str, Any]:
        """Compatibility alias: requesting a revert no longer executes it."""
        return await self.request_revert(action_id, agent=agent)

    def audit_status(self) -> dict[str, Any]:
        """Expose audit queue health so dropped records are visible."""
        return self._audit.status()


@dataclass
class AppContext:
    """Resources shared across requests, set up once in the lifespan."""

    session: ShadowSession
    audit: AuditLog
    pool: asyncpg.Pool
    control_pool: asyncpg.Pool
    policy: Policy


def control_pool_is_separate(app: AppContext) -> bool:
    """Whether approvals live outside the guarded application database pool."""
    return app.control_pool is not app.pool


def _mcp_actor(ctx: Context) -> str:
    """Return a stable actor identity for all tool calls in one MCP session."""
    client_id = getattr(ctx, "client_id", None)
    if client_id:
        return str(client_id)

    try:
        session = ctx.session
    except Exception:
        session = None
    if session is not None:
        return f"mcp-session:{id(session)}"

    return "mcp-client"


@asynccontextmanager
async def lifespan(_server: FastMCP) -> AsyncIterator[AppContext]:
    """Open the pool + audit log and load the policy on startup; close cleanly.

    Policy loading (YAML -> Policy) happens here, once, off the hot path (sec. 4).
    """
    policy = Policy.load(POLICY_PATH)

    async def _init_application_connection(conn) -> None:
        # Never inherit a role/user-controlled search_path. Qualified names still
        # work; bare development names resolve only in trusted public.
        await conn.execute("SET search_path = pg_catalog, public")

    pool = await asyncpg.create_pool(
        dsn=DB_DSN,
        min_size=POOL_MIN_SIZE,
        max_size=POOL_MAX_SIZE,
        init=_init_application_connection,
    )
    control_pool = (
        await asyncpg.create_pool(
            dsn=CONTROL_DSN, min_size=1, max_size=max(2, POOL_MAX_SIZE // 2)
        )
        if CONTROL_DSN
        else pool
    )
    audit = AuditLog(
        AUDIT_LOG_PATH,
        control_pool=control_pool if control_pool is not pool else None,
        control_schema=CONTROL_SCHEMA,
    )
    await audit.start()
    undo_store = (
        UndoStore(policy.undo, schema=CONTROL_SCHEMA) if policy.undo.enabled else None
    )
    approval_store = ApprovalStore(CONTROL_SCHEMA, ttl_seconds=APPROVAL_TTL_SECONDS)
    # Load the unique/PK column metadata once, off the hot path (sec. 4): it lets
    # a point write by a unique key skip simulation while a bulk write on a
    # non-unique column is still simulated. Ensure the approvals table exists in
    # the same round trip (startup-only DDL).
    async with pool.acquire() as conn:
        unique_columns = await load_unique_columns(conn)
    async with control_pool.acquire() as conn:
        await approval_store.ensure_schema(conn)
        if undo_store is not None:
            await undo_store.ensure_schema(conn)
    try:
        yield AppContext(
            session=ShadowSession(
                pool,
                audit,
                policy,
                undo_store,
                unique_columns=unique_columns,
                operator_token=OPERATOR_TOKEN,
                approval_store=approval_store,
                control_pool=control_pool,
            ),
            audit=audit,
            pool=pool,
            control_pool=control_pool,
            policy=policy,
        )
    finally:
        await audit.stop()
        if control_pool is not pool:
            await control_pool.close()
        await pool.close()


mcp = FastMCP("interdict", lifespan=lifespan)


@mcp.tool()
async def run_query(
    sql: str,
    ctx: Context,
    stated_task: str | None = None,
) -> dict[str, Any]:
    """Run a SQL statement against the database and return its result.

    Use this tool for SQL/database work even when the user's request is nested
    inside a larger task; the user does not need to say "use Interdict". If the
    chat has this MCP server connected, Interdict is the database safety layer.

    The statement is parsed, classified, and checked against the deterministic
    policy. If it's blocked, the result has ``blocked=True`` and a ``violations``
    list explaining why and how to fix it -- the database is not touched. If it's
    allowed it runs (a read may come back with an injected LIMIT).

    Every write is measured without executing it first. If its blast radius
    exceeds the confirm threshold the result has ``requires_confirmation=True``
    and a ``simulation`` summary (e.g. "would affect 2.3M rows") and the write is
    held. There is deliberately no agent-facing way to approve it -- an agent
    confirming its own write is not human confirmation. Tell the user to run
    ``interdict approve <approval_id>`` in their own terminal, then call
    ``run_approved_query(approval_id)`` to execute the approved write. Never
    ask the user for the operator token; it must not appear in this chat.
    ``stated_task`` is the agent's description of what it's doing -- captured
    for intent-mismatch detection later (sec. 10); advisory.

    When a write is reversible, the result carries ``undo_action_id``. Pass it
    to ``request_revert``; a human must approve the undo before it can run.
    """
    app: AppContext = ctx.request_context.lifespan_context
    agent = _mcp_actor(ctx)
    return await app.session.run_query(sql, stated_task=stated_task, agent=agent)


@mcp.tool()
async def run_approved_query(
    approval_id: str,
    ctx: Context,
) -> dict[str, Any]:
    """Execute a held write after a human has approved it out-of-band.

    Takes no token. A held write is approved by a human running ``interdict
    approve <approval_id>`` in their own terminal (never by pasting a token
    into this chat). Once approved, this tool executes the exact statement
    that was held -- it cannot run anything else, and it can only run once.
    """
    app: AppContext = ctx.request_context.lifespan_context
    return await app.session.run_approved_query(approval_id, executor=_mcp_actor(ctx))


@mcp.tool()
async def list_pending_approvals(ctx: Context) -> dict[str, Any]:
    """List currently held writes awaiting out-of-band operator approval."""
    app: AppContext = ctx.request_context.lifespan_context
    return {"pending": await app.session.pending_approvals()}


@mcp.tool()
async def request_revert(
    action_id: str,
    ctx: Context,
) -> dict[str, Any]:
    """Request human approval to undo a previously executed write.

    This tool never performs the undo. A human approves the returned approval_id
    out-of-band, then the agent calls ``run_approved_revert``.
    """
    app: AppContext = ctx.request_context.lifespan_context
    return await app.session.request_revert(action_id, agent=_mcp_actor(ctx))


@mcp.tool()
async def run_approved_revert(approval_id: str, ctx: Context) -> dict[str, Any]:
    """Execute an undo only after its human approval was recorded out-of-band."""
    app: AppContext = ctx.request_context.lifespan_context
    return await app.session.run_approved_revert(approval_id, executor=_mcp_actor(ctx))


@mcp.tool()
async def revert_write(action_id: str, ctx: Context) -> dict[str, Any]:
    """Compatibility alias for ``request_revert``; it does not execute undo."""
    app: AppContext = ctx.request_context.lifespan_context
    return await app.session.request_revert(action_id, agent=_mcp_actor(ctx))


def _package_version() -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("interdict-db")
    except PackageNotFoundError:
        return "source"


@mcp.tool()
async def interdict_status(ctx: Context) -> dict[str, Any]:
    """Report Interdict's full status: active/guarded database, policy in
    force, and audit-trail health. This is the single status tool -- use it to
    verify Interdict is protecting this chat."""
    app: AppContext = ctx.request_context.lifespan_context
    policy = app.policy
    return {
        "active": True,
        "summary": (
            "Interdict is active in this chat. Database operations should go "
            "through the Interdict MCP tools before touching Postgres."
        ),
        "server": "interdict",
        "version": _package_version(),
        "actor": _mcp_actor(ctx),
        "dsn": redact_text(DB_DSN),
        "policy_path": POLICY_PATH,
        "mode": policy.mode,
        "allowed_tables": sorted(policy.allowed_tables or []),
        "simulation_enabled": policy.simulation.enabled,
        "undo_enabled": policy.undo.enabled,
        "operator_approval_configured": bool(OPERATOR_TOKEN),
        "safety_profile": SAFETY_PROFILE,
        "control_store_separate": control_pool_is_separate(app),
        "audit": app.session.audit_status(),
    }


def _preflight() -> None:
    """Fail fast, with one clear line, before speaking MCP.

    Runs once at startup (never on the query path). Everything user-facing goes
    to stderr: stdout is the MCP protocol channel and must stay clean.
    """
    try:
        profile = validate_profile(SAFETY_PROFILE)
    except ValueError as exc:
        print(f"interdict: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    if "AGENT_DB_DSN" not in os.environ:
        print(
            "interdict: AGENT_DB_DSN is not set; using the dev default "
            f"({redact_text(DB_DSN)}). Point it at your database with "
            "AGENT_DB_DSN=postgresql://user:pass@host:5432/dbname",
            file=sys.stderr,
        )

    try:
        Policy.load(POLICY_PATH)
    except Exception as exc:
        print(
            f"interdict: cannot load policy {POLICY_PATH}: {exc}\n"
            "Fix the YAML or unset AGENT_POLICY to use the built-in default.",
            file=sys.stderr,
        )
        raise SystemExit(1) from exc

    async def _check_db() -> list[str]:
        conn = await asyncpg.connect(dsn=DB_DSN, timeout=5)
        control_conn = None
        try:
            application = await inspect_database_security(conn)
            control = None
            if CONTROL_DSN:
                control_conn = await asyncpg.connect(dsn=CONTROL_DSN, timeout=5)
                control = await inspect_database_security(control_conn)
            if profile == PRODUCTION:
                return production_errors(
                    policy=Policy.load(POLICY_PATH),
                    application=application,
                    control=control,
                    operator_token=OPERATOR_TOKEN,
                    operator_id=OPERATOR_ID,
                    min_token_length=MIN_OPERATOR_TOKEN_LENGTH,
                )
            return []
        finally:
            if control_conn is not None:
                await control_conn.close()
            await conn.close()

    try:
        security_errors = asyncio.run(_check_db())
    except Exception as exc:
        print(
            f"interdict: cannot reach Postgres at {redact_text(DB_DSN)}: {exc}\n"
            "Check that the database is running and AGENT_DB_DSN is correct.",
            file=sys.stderr,
        )
        raise SystemExit(1) from exc

    if security_errors:
        print("interdict: production safety checks failed:", file=sys.stderr)
        for error in security_errors:
            print(f"  - {error}", file=sys.stderr)
        print(
            "Run `interdict doctor` after configuring a least-privilege "
            "application role and separate control database.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    if not OPERATOR_TOKEN:
        print(
            "interdict: AGENT_OPERATOR_TOKEN is not set -- held writes cannot "
            "be approved this session (they stay blocked, which is safe). Set "
            "a random token of 32+ chars to enable out-of-band approval.",
            file=sys.stderr,
        )
    elif len(OPERATOR_TOKEN) < MIN_OPERATOR_TOKEN_LENGTH:
        print(
            f"interdict: AGENT_OPERATOR_TOKEN is shorter than "
            f"{MIN_OPERATOR_TOKEN_LENGTH} chars and will be rejected -- "
            "approvals are effectively disabled until it is lengthened.",
            file=sys.stderr,
        )

    print(
        f"interdict: ready -- guarding {redact_text(DB_DSN)} "
        f"(policy: {POLICY_PATH}, audit: {AUDIT_LOG_PATH})",
        file=sys.stderr,
    )
    print(
        "\nTo connect Claude Code:\n"
        "  claude mcp add interdict \\\n"
        f"    --env AGENT_DB_DSN={redact_text(DB_DSN)} \\\n"
        '    --env AGENT_OPERATOR_TOKEN="..." \\\n'
        "    -- interdict\n",
        file=sys.stderr,
    )


_USAGE = """Usage:
  interdict                     Run the MCP server over stdio (agent-facing)
  interdict doctor              Check production privilege boundaries
  interdict init                Print least-privilege setup SQL (does not run it)
  interdict migrate-control     Copy legacy control records; delete nothing
  interdict pending             List writes held for approval
  interdict approve <id>        Approve a held write (agent then executes it)
  interdict deny <id>           Deny a held write
  interdict --version           Print version

Environment:
  AGENT_DB_DSN             Postgres DSN to protect
  AGENT_CONTROL_DSN        Separate Postgres database for approvals/undo metadata
  AGENT_SAFETY_PROFILE     production (default) or development
  AGENT_OPERATOR_TOKEN     Token required for operator approvals
  AGENT_OPERATOR_ID        Stable human operator identity (required in production)
  AGENT_POLICY             YAML policy path
  AGENT_AUDIT_LOG          JSONL audit log path

Approving is done here, in YOUR terminal, on purpose: the agent never sees
the operator token. After you approve, the agent runs the write by calling
run_approved_query(approval_id)."""


def _operator_cli(argv: list[str]) -> int:
    """Human-side approval commands. Runs in the operator's own terminal."""
    command, args = argv[0], argv[1:]
    store = ApprovalStore(CONTROL_SCHEMA, ttl_seconds=APPROVAL_TTL_SECONDS)

    async def _run() -> int:
        conn = await asyncpg.connect(dsn=CONTROL_DSN or DB_DSN, timeout=5)
        try:
            if command == "pending":
                rows = await store.list_pending(conn)
                if not rows:
                    print("No writes are waiting for approval.")
                    return 0
                for row in rows:
                    sim = row.get("simulation")
                    print(f"{row['approval_id']}  [{row['created_at']:%H:%M:%S}]")
                    print(f"  sql:  {row['sql']}")
                    if row.get("stated_task"):
                        print(f"  task: {row['stated_task']}")
                    if isinstance(sim, str):
                        sim = json.loads(sim)
                    print(f"  {_simulation_summary(sim)}")
                return 0

            # approve / deny need the token and an id.
            if not args:
                print(f"interdict {command}: missing <approval_id>", file=sys.stderr)
                return 2
            if not OPERATOR_TOKEN or len(OPERATOR_TOKEN) < MIN_OPERATOR_TOKEN_LENGTH:
                print(
                    "interdict: AGENT_OPERATOR_TOKEN must be set in this shell "
                    "(32+ chars) and match the server's token.",
                    file=sys.stderr,
                )
                return 2
            approval_id = args[0]
            decided = await store.decide(
                conn,
                approval_id,
                approve=(command == "approve"),
                operator_token_hash=token_hash(OPERATOR_TOKEN),
                decided_by=OPERATOR_ID or "development-operator",
            )
            if decided is None:
                print(
                    f"interdict: could not {command} {approval_id}: no pending "
                    "approval matches that id and this token.",
                    file=sys.stderr,
                )
                return 1
            if decided == APPROVED:
                row = await store.get(conn, approval_id)
                tool = (
                    "run_approved_revert"
                    if row and row.get("action_kind") == "revert"
                    else "run_approved_query"
                )
                print(
                    f"Approved {approval_id}. The agent can now execute it with "
                    f'{tool}(approval_id="{approval_id}").'
                )
            else:
                print(f"Denied {approval_id}. It will not run.")
            return 0
        finally:
            await conn.close()

    try:
        return asyncio.run(_run())
    except Exception as exc:
        print(
            f"interdict: cannot reach Postgres at {redact_text(DB_DSN)}: {exc}",
            file=sys.stderr,
        )
        return 1


def _doctor_cli() -> int:
    """Human-readable deployment audit; never starts the MCP server."""
    try:
        profile = validate_profile(SAFETY_PROFILE)
        policy = Policy.load(POLICY_PATH)

        async def _run():
            app_conn = await asyncpg.connect(dsn=DB_DSN, timeout=5)
            control_conn = None
            try:
                application = await inspect_database_security(app_conn)
                control = None
                if CONTROL_DSN:
                    control_conn = await asyncpg.connect(dsn=CONTROL_DSN, timeout=5)
                    control = await inspect_database_security(control_conn)
                return application, control
            finally:
                if control_conn is not None:
                    await control_conn.close()
                await app_conn.close()

        application, control = asyncio.run(_run())
        print(f"Safety profile: {profile}")
        print(
            f"Application: role={application.role} " f"database={application.database}"
        )
        print(
            "Control store: "
            + (
                f"role={control.role} database={control.database}"
                if control is not None
                else "not configured"
            )
        )
        if profile != PRODUCTION:
            print("OK for development; this profile is not production-safe.")
            return 0
        errors = production_errors(
            policy=policy,
            application=application,
            control=control,
            operator_token=OPERATOR_TOKEN,
            operator_id=OPERATOR_ID,
            min_token_length=MIN_OPERATOR_TOKEN_LENGTH,
        )
        if errors:
            print("FAILED production checks:")
            for error in errors:
                print(f"  - {error}")
            return 1
        print("PASS: production privilege boundaries are configured.")
        return 0
    except Exception as exc:
        print(f"interdict doctor: {exc}", file=sys.stderr)
        return 1


def _init_cli() -> int:
    """Print a reviewable psql template; never mutate a database."""

    def ident(value: str) -> str:
        return '"' + value.replace('"', '""') + '"'

    policy = Policy.load(POLICY_PATH)
    tables = sorted(policy.allowed_tables or [])
    print("-- Review and run this in the APPLICATION database as an administrator.")
    print("\\prompt 'Password for interdict_app: ' interdict_app_password")
    print(
        "CREATE ROLE interdict_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
        "NOREPLICATION NOBYPASSRLS PASSWORD :'interdict_app_password';"
    )
    schemas = sorted({table.split(".", 1)[0] for table in tables if "." in table})
    for schema in schemas:
        print(
            f"-- Ensure PUBLIC cannot grant inherited CREATE on {ident(schema)}; "
            "review impact on other roles."
        )
        print(f"REVOKE CREATE ON SCHEMA {ident(schema)} FROM PUBLIC;")
        print(f"GRANT USAGE ON SCHEMA {ident(schema)} TO interdict_app;")
    for table in tables:
        if "." not in table:
            print(f"-- SKIPPED unqualified allowlist entry: {table}")
            continue
        schema, relation = table.split(".", 1)
        print(
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON "
            f"{ident(schema)}.{ident(relation)} "
            "TO interdict_app;"
        )
    for reserved in ("adb_undo", "interdict_control"):
        print(
            "DO $$ BEGIN IF to_regnamespace(" + repr(reserved) + ") IS NOT NULL "
            "THEN EXECUTE 'REVOKE ALL ON SCHEMA "
            + ident(reserved)
            + " FROM interdict_app'; END IF; END $$;"
        )
    print()
    print("-- Run this section as an administrator for the CONTROL database.")
    print("-- The control database and role must differ from the application ones.")
    print("\\prompt 'Password for interdict_control: ' interdict_control_password")
    print(
        "CREATE ROLE interdict_control LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
        "NOREPLICATION NOBYPASSRLS PASSWORD :'interdict_control_password';"
    )
    print("CREATE SCHEMA interdict_control AUTHORIZATION interdict_control;")
    print("REVOKE ALL ON SCHEMA interdict_control FROM PUBLIC;")
    print("-- Interdict never executes this output automatically.")
    return 0


def _migrate_control_cli() -> int:
    if not CONTROL_DSN:
        print("interdict: AGENT_CONTROL_DSN is required", file=sys.stderr)
        return 2

    async def _run() -> dict[str, int]:
        app = await asyncpg.connect(DB_DSN, timeout=5)
        control = await asyncpg.connect(CONTROL_DSN, timeout=5)
        try:
            return await migrate_legacy_control(
                app, control, destination_schema=CONTROL_SCHEMA
            )
        finally:
            await app.close()
            await control.close()

    try:
        copied = asyncio.run(_run())
    except Exception as exc:
        print(f"interdict migrate-control: {exc}", file=sys.stderr)
        return 1
    print(
        f"Copied {copied['approvals']} approval(s) and "
        f"{copied['undo_records']} undo record(s)."
    )
    print("The legacy adb_undo schema was not modified or deleted.")
    return 0


def main() -> None:
    """Entry point: run the MCP server over stdio (the standard MCP transport)."""
    argv = sys.argv[1:]
    if any(arg in {"-h", "--help"} for arg in argv):
        print(_USAGE)
        return
    if "--version" in argv:
        print(f"interdict {_package_version()}")
        return
    if argv and argv[0] == "doctor":
        raise SystemExit(_doctor_cli())
    if argv and argv[0] == "init":
        raise SystemExit(_init_cli())
    if argv and argv[0] == "migrate-control":
        raise SystemExit(_migrate_control_cli())
    if argv and argv[0] in {"pending", "approve", "deny"}:
        raise SystemExit(_operator_cli(argv))
    if argv:
        print(f"interdict: unknown command {argv[0]!r}\n\n{_USAGE}", file=sys.stderr)
        raise SystemExit(2)
    _preflight()
    mcp.run()


if __name__ == "__main__":
    main()
