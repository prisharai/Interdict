"""Persistent out-of-band approvals for held writes.

A risky write that exceeds the confirm threshold is *held*: it does not run
until a human operator approves it. The whole point of holding is that the
agent cannot approve its own write -- which means the operator token must never
transit the agent chat. This store makes that possible: held writes are
persisted in the control store, the operator decides from their own terminal
(``interdict approve <id>``, token read from their
environment), and the agent then resumes with only the approval id.

Latency notes (CLAUDE.md sec. 4): nothing here is on the pass-through path. A
row is written only when a write is held (it already paid for simulation), and
reads happen only when an operator or a resumed approval asks.

Token handling: the server stores ``sha256(operator_token)`` on each held row.
The CLI hashes the token from its own environment and the decision UPDATE only
matches when the hashes agree, so the raw token is never persisted and never
appears in chat, transcripts, or the database.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from engine.undo import _execute_tolerating_duplicates


def _q(identifier: str) -> str:
    """Quote a SQL identifier (schema/table names only, never values)."""
    return '"' + identifier.replace('"', '""') + '"'


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


PENDING = "pending"
APPROVED = "approved"
DENIED = "denied"
EXECUTED = "executed"
EXECUTING = "executing"
FAILED = "failed"

# A hold's blast radius was measured at hold time; the longer it sits, the
# staler that measurement gets. After the TTL the hold can no longer be
# approved or executed -- the agent must re-run the query so the impact is
# measured fresh.
DEFAULT_TTL_SECONDS = 30 * 60


class ApprovalStore:
    """Held writes, normally persisted in a separate control database."""

    def __init__(self, schema: str, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> None:
        self._schema = schema
        self._ttl_seconds = ttl_seconds
        self._ensured = False

    @property
    def ttl_seconds(self) -> int:
        return self._ttl_seconds

    @property
    def _fresh(self) -> str:
        """SQL predicate: the hold is still within its TTL."""
        return f"created_at > now() - interval '{int(self._ttl_seconds)} seconds'"

    @property
    def _table(self) -> str:
        return f"{_q(self._schema)}.pending_approval"

    async def ensure_schema(self, conn) -> None:
        """Create the approvals schema/table if missing. Idempotent.

        Duplicate-object errors are tolerated: ``IF NOT EXISTS`` still races
        under concurrent first use (see ``_execute_tolerating_duplicates``).
        """
        if self._ensured:
            return
        await _execute_tolerating_duplicates(
            conn, f"CREATE SCHEMA IF NOT EXISTS {_q(self._schema)}"
        )
        await _execute_tolerating_duplicates(
            conn,
            f"""CREATE TABLE IF NOT EXISTS {self._table} (
                approval_id  uuid PRIMARY KEY,
                created_at   timestamptz NOT NULL DEFAULT now(),
                sql          text NOT NULL,
                stated_task  text,
                agent        text,
                principal    jsonb,
                simulation   jsonb,
                token_hash   text,
                status       text NOT NULL DEFAULT 'pending',
                decided_by   text,
                decided_at   timestamptz,
                executed_at  timestamptz
            )""",
        )
        for column in (
            "ADD COLUMN IF NOT EXISTS sql_sha256 text",
            "ADD COLUMN IF NOT EXISTS policy_sha256 text",
            "ADD COLUMN IF NOT EXISTS approved_rows bigint",
            "ADD COLUMN IF NOT EXISTS claimed_at timestamptz",
            "ADD COLUMN IF NOT EXISTS failure_reason text",
            "ADD COLUMN IF NOT EXISTS action_kind text NOT NULL DEFAULT 'sql'",
            "ADD COLUMN IF NOT EXISTS undo_action_id uuid",
            "ADD COLUMN IF NOT EXISTS object_fingerprint jsonb",
        ):
            await _execute_tolerating_duplicates(
                conn, f"ALTER TABLE {self._table} {column}"
            )
        self._ensured = True

    async def create(
        self,
        conn,
        *,
        approval_id: str,
        sql: str,
        stated_task: str | None,
        agent: str | None,
        principal: dict[str, Any] | None,
        simulation: dict[str, Any] | None,
        operator_token_hash: str | None,
        policy_sha256: str | None = None,
        approved_rows: int | None = None,
        action_kind: str = "sql",
        undo_action_id: str | None = None,
        object_fingerprint: dict[str, Any] | None = None,
    ) -> None:
        await self.ensure_schema(conn)
        await conn.execute(
            f"""INSERT INTO {self._table}
                (approval_id, sql, stated_task, agent, principal, simulation,
                 token_hash, sql_sha256, policy_sha256, approved_rows,
                 action_kind, undo_action_id, object_fingerprint)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                        $11, $12::uuid, $13)""",
            approval_id,
            sql,
            stated_task,
            agent,
            json.dumps(principal) if principal is not None else None,
            json.dumps(simulation) if simulation is not None else None,
            operator_token_hash,
            hashlib.sha256(sql.encode("utf-8")).hexdigest(),
            policy_sha256,
            approved_rows,
            action_kind,
            undo_action_id,
            json.dumps(object_fingerprint) if object_fingerprint is not None else None,
        )

    async def get(self, conn, approval_id: str) -> dict[str, Any] | None:
        await self.ensure_schema(conn)
        row = await conn.fetchrow(
            f"SELECT *, NOT ({self._fresh}) AS expired "
            f"FROM {self._table} WHERE approval_id = $1",
            approval_id,
        )
        return dict(row) if row is not None else None

    async def decide(
        self,
        conn,
        approval_id: str,
        *,
        approve: bool,
        operator_token_hash: str,
        decided_by: str,
    ) -> str | None:
        """Approve or deny a pending hold. Returns the new status, or ``None``
        when nothing matched (unknown id, already decided, or token mismatch --
        deliberately indistinguishable to a caller probing for ids)."""
        await self.ensure_schema(conn)
        status = APPROVED if approve else DENIED
        result = await conn.execute(
            f"""UPDATE {self._table}
                SET status = $2, decided_by = $3, decided_at = now()
                WHERE approval_id = $1
                  AND status = 'pending'
                  AND {self._fresh}
                  AND token_hash IS NOT NULL
                  AND token_hash = $4""",
            approval_id,
            status,
            decided_by,
            operator_token_hash,
        )
        return status if result == "UPDATE 1" else None

    async def claim_approved(
        self, conn, approval_id: str, *, action_kind: str = "sql"
    ) -> dict[str, Any] | None:
        """Atomically lease one approved action for execution and return it.

        The UPDATE-with-status-guard makes concurrent double execution
        impossible. Completion is recorded separately after the target commit.
        """
        await self.ensure_schema(conn)
        row = await conn.fetchrow(
            f"""UPDATE {self._table}
                SET status = 'executing', claimed_at = now()
                WHERE approval_id = $1 AND status = 'approved'
                  AND action_kind = $2
                  AND {self._fresh}
                RETURNING *""",
            approval_id,
            action_kind,
        )
        return dict(row) if row is not None else None

    async def finish_execution(
        self,
        conn,
        approval_id: str,
        *,
        succeeded: bool,
        failure_reason: str | None = None,
    ) -> bool:
        status = EXECUTED if succeeded else FAILED
        result = await conn.execute(
            f"""UPDATE {self._table}
                SET status=$2, executed_at=CASE WHEN $2='executed' THEN now() END,
                    failure_reason=$3
                WHERE approval_id=$1 AND status='executing'""",
            approval_id,
            status,
            failure_reason,
        )
        return result == "UPDATE 1"

    async def list_pending(self, conn) -> list[dict[str, Any]]:
        await self.ensure_schema(conn)
        rows = await conn.fetch(
            f"""SELECT approval_id, created_at, sql, stated_task, agent,
                       principal, simulation, action_kind, undo_action_id
                FROM {self._table}
                WHERE status = 'pending' AND {self._fresh}
                ORDER BY created_at"""
        )
        return [dict(r) for r in rows]
