"""MCP transport adapter for database query execution."""

from __future__ import annotations

import asyncio
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

from engine.audit import AuditLog
from engine.classifier import DDL, WRITE, classify
from engine.intent import check_intent, llm_second_opinion
from engine.policy import Policy, ReasonCode, apply_blast_radius, apply_intent, evaluate
from engine.security import client_db_error, diagnostic_error, redact_text
from engine.simulate import is_risky_write, load_unique_columns, simulate
from engine.undo import UndoStore, execute_with_undo, revert

# --- Config (env-overridable; defaults match docker-compose.yml) -------------
DB_DSN = os.environ.get(
    "AGENT_DB_DSN",
    "postgresql://postgres:postgres@localhost:5433/pagila",
)
AUDIT_LOG_PATH = os.environ.get("AGENT_AUDIT_LOG", "logs/audit.jsonl")
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
        "Interdict held this write for out-of-band approval: "
        f"{_simulation_summary(simulation)}. approval_id={approval_id}"
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
            f"undo={action_id}"
        )
    if status:
        return f"Interdict allowed and executed {status}. returned {row_count} row(s)."
    return f"Interdict allowed the statement. returned {row_count} row(s)."


@dataclass(frozen=True)
class PendingApproval:
    """A held write awaiting out-of-band operator approval."""

    sql: str
    stated_task: str | None
    agent: str | None
    created_at: float
    simulation: dict | None
    intent: dict | None


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
        self._pending_approvals: dict[str, PendingApproval] = {}
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

    def _approval_summary(self, approval_id: str, pending: PendingApproval) -> dict:
        return {
            "approval_id": approval_id,
            "sql": pending.sql,
            "stated_task": pending.stated_task,
            "agent": pending.agent,
            "created_at": pending.created_at,
            "simulation": pending.simulation,
            "intent": pending.intent,
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

        # Blast-radius simulation (Day 4) -- OFF the normal path: only a risky
        # write, only when enforcing and enabled. May escalate the decision to
        # blocked (over the block limit) or to requires_confirmation (sec. 4).
        if (
            decision is not None
            and decision.allowed
            and enforce
            and self._policy.simulation.enabled
            and is_risky_write(classification, self._unique_columns)
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
        if decision is not None and not decision.allowed and enforce:
            violations = [v.to_dict() for v in decision.violations]
            primary_violation = violations[0] if violations else None
            block_reason = (
                primary_violation["reason_code"] if primary_violation else None
            )
            block_message = primary_violation["message"] if primary_violation else None
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
            self._pending_approvals[approval_id] = PendingApproval(
                sql=sql,
                stated_task=stated_task,
                agent=agent,
                created_at=time.time(),
                simulation=decision.simulation,
                intent=decision.intent,
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
                    outcome = await execute_with_undo(
                        conn,
                        effective_sql,
                        classification,
                        agent=agent,
                        stated_task=stated_task,
                        config=self._policy.undo,
                        store=self._undo_store,
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
                        self._audit.record(
                            {
                                "event": "query",
                                "agent": agent,
                                "stated_task": stated_task,
                                "sql": sql,
                                "blocked": True,
                                "violations": [violation],
                                "undo_reason": undo_reason,
                                "decision": (
                                    decision.to_dict() if decision is not None else None
                                ),
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
                "decision": decision.to_dict() if decision is not None else None,
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
            self._audit.record(
                {
                    "event": "approval_denied",
                    "operator": operator,
                    "approval_id": approval_id,
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
            }
        pending = self._pending_approvals.pop(approval_id, None)
        if pending is None:
            return {
                "ok": False,
                "approval_id": approval_id,
                "summary": (
                    "Interdict did not approve this write: no pending approval "
                    "exists for that id."
                ),
                "error": "no such pending approval",
            }
        self._audit.record(
            {
                "event": "approval_granted",
                "operator": operator,
                **self._approval_summary(approval_id, pending),
            }
        )
        result = await self.run_query(
            pending.sql,
            stated_task=pending.stated_task,
            agent=pending.agent,
            operator_approved=True,
        )
        return {"ok": result.get("error") is None, "approval_id": approval_id, **result}

    def pending_approvals(self) -> list[dict[str, Any]]:
        """Current in-memory held writes, safe to show to an operator."""
        return [
            self._approval_summary(approval_id, pending)
            for approval_id, pending in self._pending_approvals.items()
        ]

    async def revert_write(
        self,
        action_id: str,
        *,
        agent: str | None = None,
        operator_token: str | None = None,
    ) -> dict[str, Any]:
        """Undo a previously-recorded write by its action id. Itself audited."""
        if self._undo_store is None:
            return {
                "ok": False,
                "action_id": action_id,
                "summary": (
                    "Interdict could not revert this write: undo is not enabled."
                ),
                "error": "undo not enabled",
            }
        operator_override = self._operator_allowed(operator_token)
        require_agent_match = (
            bool(self._policy and self._policy.undo.require_agent_match)
            and not operator_override
        )
        async with self._pool.acquire() as conn:
            result = await revert(
                conn,
                action_id,
                self._undo_store,
                agent=agent,
                require_agent_match=require_agent_match,
            )
        self._audit.record({"event": "revert", "agent": agent, **result.to_dict()})
        payload = result.to_dict()
        if result.ok:
            payload["summary"] = (
                f"Interdict reverted {action_id}: restored "
                f"{result.rows_restored} row(s)."
            )
        else:
            payload["summary"] = (
                f"Interdict could not revert {action_id}: {result.error}"
            )
        return payload

    def audit_status(self) -> dict[str, Any]:
        """Expose audit queue health so dropped records are visible."""
        return self._audit.status()


@dataclass
class AppContext:
    """Resources shared across requests, set up once in the lifespan."""

    session: ShadowSession
    audit: AuditLog
    pool: asyncpg.Pool
    policy: Policy


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
    pool = await asyncpg.create_pool(
        dsn=DB_DSN, min_size=POOL_MIN_SIZE, max_size=POOL_MAX_SIZE
    )
    audit = AuditLog(AUDIT_LOG_PATH)
    await audit.start()
    undo_store = UndoStore(policy.undo) if policy.undo.enabled else None
    # Load the unique/PK column metadata once, off the hot path (sec. 4): it lets
    # a point write by a unique key skip simulation while a bulk write on a
    # non-unique column is still simulated.
    async with pool.acquire() as conn:
        unique_columns = await load_unique_columns(conn)
    try:
        yield AppContext(
            session=ShadowSession(
                pool,
                audit,
                policy,
                undo_store,
                unique_columns=unique_columns,
                operator_token=OPERATOR_TOKEN,
            ),
            audit=audit,
            pool=pool,
            policy=policy,
        )
    finally:
        await audit.stop()
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

    A risky write may be simulated first to measure its blast radius. If that
    exceeds the confirm threshold the result has ``requires_confirmation=True``
    and a ``simulation`` summary (e.g. "would affect 2.3M rows") and the write is
    held. There is deliberately no agent-facing way to approve it -- an agent
    confirming its own write is not human confirmation; approval is out-of-band
    (Day 6). ``stated_task`` is the agent's description of what it's doing --
    captured for intent-mismatch detection later (sec. 10); advisory.

    When a write is reversible, the result carries ``undo_action_id`` -- pass it
    to ``revert_write`` to undo the change.
    """
    app: AppContext = ctx.request_context.lifespan_context
    agent = _mcp_actor(ctx)
    return await app.session.run_query(sql, stated_task=stated_task, agent=agent)


@mcp.tool()
async def approve_query(
    approval_id: str,
    operator_token: str,
    ctx: Context,
) -> dict[str, Any]:
    """Approve and execute a held write.

    This tool requires ``AGENT_OPERATOR_TOKEN`` to be set on the server and the
    same token to be provided here. Without that token, risky writes remain held.
    """
    app: AppContext = ctx.request_context.lifespan_context
    operator = _mcp_actor(ctx)
    return await app.session.approve_query(
        approval_id, operator_token=operator_token, operator=operator
    )


@mcp.tool()
async def list_pending_approvals(ctx: Context) -> dict[str, Any]:
    """List currently held writes awaiting operator approval."""
    app: AppContext = ctx.request_context.lifespan_context
    return {"pending": app.session.pending_approvals()}


@mcp.tool()
async def revert_write(
    action_id: str,
    ctx: Context,
    operator_token: str | None = None,
) -> dict[str, Any]:
    """Undo a previously-executed write by its ``undo_action_id``.

    Restores the affected rows to their captured before-image (UPDATE values
    restored in place, DELETEd rows re-inserted, INSERTed rows removed). The
    revert is itself recorded; a record can only be reverted once. Returns
    ``{ok, action_id, operation, rows_restored, error}``.
    """
    app: AppContext = ctx.request_context.lifespan_context
    agent = _mcp_actor(ctx)
    return await app.session.revert_write(
        action_id, agent=agent, operator_token=operator_token
    )


@mcp.tool()
async def audit_status(ctx: Context) -> dict[str, Any]:
    """Return audit queue health, including dropped-record count."""
    app: AppContext = ctx.request_context.lifespan_context
    return app.session.audit_status()


@mcp.tool()
async def interdict_status(ctx: Context) -> dict[str, Any]:
    """Show that Interdict is active in this chat and what database it guards."""
    app: AppContext = ctx.request_context.lifespan_context
    policy = app.policy
    return {
        "active": True,
        "summary": (
            "Interdict is active in this chat. Database operations should go "
            "through the Interdict MCP tools before touching Postgres."
        ),
        "server": "interdict",
        "actor": _mcp_actor(ctx),
        "dsn": redact_text(DB_DSN),
        "policy_path": POLICY_PATH,
        "mode": policy.mode,
        "allowed_tables": sorted(policy.allowed_tables or []),
        "simulation_enabled": policy.simulation.enabled,
        "undo_enabled": policy.undo.enabled,
        "operator_approval_configured": bool(OPERATOR_TOKEN),
    }


def main() -> None:
    """Entry point: run the MCP server over stdio (the standard MCP transport)."""
    if any(arg in {"-h", "--help"} for arg in sys.argv[1:]):
        print(
            "Usage: interdict\n\n"
            "Run the Interdict MCP server over stdio.\n\n"
            "Environment:\n"
            "  AGENT_DB_DSN             Postgres DSN to protect\n"
            "  AGENT_OPERATOR_TOKEN     Token required for operator approvals\n"
            "  AGENT_POLICY             YAML policy path\n"
            "  AGENT_AUDIT_LOG          JSONL audit log path"
        )
        return
    mcp.run()


if __name__ == "__main__":
    main()
