"""Small live demo for Interdict's Postgres safety path.

Requires the dev database:

    docker compose up -d
    uv run python examples/demo.py
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

import asyncpg

from adapters.mcp_server import ShadowSession
from engine.approvals import ApprovalStore, token_hash
from engine.audit import AuditLog
from engine.policy import Policy
from engine.simulate import SimulationConfig, load_unique_columns
from engine.undo import UndoConfig, UndoStore

DB_DSN = os.environ.get(
    "AGENT_DB_DSN",
    "postgresql://postgres:postgres@localhost:5433/pagila",
)
OPERATOR_TOKEN = "demo-operator-token-with-at-least-32-chars"


async def _setup(pool: asyncpg.Pool) -> frozenset[str]:
    async with pool.acquire() as conn:
        await conn.execute("DROP TABLE IF EXISTS _demo_accounts")
        await conn.execute(
            """
            CREATE TABLE _demo_accounts (
                id int PRIMARY KEY,
                balance int NOT NULL,
                status text NOT NULL DEFAULT 'active'
            )
            """
        )
        await conn.execute(
            """
            INSERT INTO _demo_accounts (id, balance)
            SELECT g, 100 + g FROM generate_series(1, 50) g
            """
        )
        return await load_unique_columns(conn)


async def _count(pool: asyncpg.Pool) -> int:
    async with pool.acquire() as conn:
        return await conn.fetchval("SELECT count(*) FROM _demo_accounts")


async def main() -> None:
    pool = await asyncpg.create_pool(dsn=DB_DSN, min_size=1, max_size=4, timeout=5)
    audit = AuditLog(Path(tempfile.gettempdir()) / "interdict-demo-audit.jsonl")
    await audit.start()
    policy = Policy(
        allowed_tables=frozenset({"_demo_accounts"}),
        simulation=SimulationConfig(
            enabled=True,
            precise=True,
            confirm_over_rows=10,
            block_over_rows=100000,
        ),
        undo=UndoConfig(enabled=True),
    )
    unique_columns = await _setup(pool)
    sess = ShadowSession(
        pool,
        audit,
        policy,
        UndoStore(policy.undo),
        unique_columns=unique_columns,
        operator_token=OPERATOR_TOKEN,
    )

    try:
        print("Interdict demo: block -> self-correct -> blast radius -> undo")
        print(f"Seeded _demo_accounts with {await _count(pool)} rows.\n")

        print("1. Block an unsafe write")
        blocked = await sess.run_query("DELETE FROM _demo_accounts")
        print(f"   blocked={blocked['blocked']} reason={blocked['block_reason']}")

        print("\n2. Self-correct to a scoped reversible write")
        scoped = await sess.run_query(
            "UPDATE _demo_accounts SET status = 'review' WHERE id = 1",
            stated_task="mark account 1 for review",
            agent="demo-agent",
        )
        print(f"   status={scoped['status']} undo_id={scoped['undo_action_id']}")

        print("\n3. Measure blast radius and hold for approval")
        held = await sess.run_query(
            "DELETE FROM _demo_accounts WHERE id <= 29",
            stated_task="remove obsolete demo accounts",
            agent="demo-agent",
        )
        approval_id = held["approval_id"]
        print(f"   held approval_id={approval_id}")
        print(f"   operator runs: interdict approve {approval_id}")

        async with pool.acquire() as conn:
            decided = await ApprovalStore(policy.undo.schema).decide(
                conn,
                approval_id,
                approve=True,
                operator_token_hash=token_hash(OPERATOR_TOKEN),
                decided_by="demo-operator",
            )
        print(f"   operator decision={decided}")

        executed = await sess.run_approved_query(approval_id, executor="demo-agent")
        print(f"   run_approved_query ok={executed['ok']} status={executed['status']}")
        print(f"   rows remaining={await _count(pool)}")

        print("\n4. Undo the approved delete")
        reverted = await sess.revert_write(
            executed["undo_action_id"], agent="demo-agent"
        )
        print(f"   reverted={reverted['ok']} rows_restored={reverted['rows_restored']}")
        print(f"   rows restored={await _count(pool)}")
        print("\nDemo complete.")
    finally:
        async with pool.acquire() as conn:
            await conn.execute("DROP TABLE IF EXISTS _demo_accounts")
            # Remove only the demo's own holds -- never touch real pending
            # approvals that may exist in this database.
            await conn.execute(
                "DELETE FROM adb_undo.pending_approval WHERE agent = 'demo-agent'"
            )
        await audit.stop()
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
