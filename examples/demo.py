"""End-to-end demo: what an AI agent experiences behind the safety layer.

Runs four scenarios against a throwaway `accounts` table through the real
adapter (`ShadowSession`) -- the same path an MCP agent uses:

  1. A destructive write is BLOCKED with a structured, machine-readable reason.
  2. The agent SELF-CORRECTS using the suggested fix, and the safe version runs.
  3. A risky bulk write has its BLAST RADIUS measured ("would affect N rows")
     and is held for confirmation before it can commit.
  4. An allowed write is UNDONE with a single revert, restoring prior state.

Needs the dev Postgres up (`docker compose up`). Self-contained and repeatable:
it creates and drops its own table and never touches real data.

    python -m examples.demo
"""

import asyncio
import os
import tempfile

import asyncpg

from adapters.mcp_server import ShadowSession
from engine.audit import AuditLog
from engine.policy import Policy
from engine.simulate import SimulationConfig, load_unique_columns
from engine.undo import UndoConfig, UndoStore, revert

DB_DSN = os.environ.get(
    "AGENT_DB_DSN", "postgresql://postgres:postgres@localhost:5433/pagila"
)


def banner(text: str) -> None:
    print("\n" + "=" * 72 + f"\n  {text}\n" + "=" * 72)


def show(label: str, res: dict) -> None:
    """Print the parts of the response the agent actually sees."""
    print(f"\n  agent> {label}")
    if res.get("blocked"):
        v = res["violations"][0]
        print(f"  BLOCKED [{v['reason_code']}]: {v['message']}")
        print(f"     fix: {v['suggested_fix']}")
    elif res.get("requires_confirmation"):
        sim = res["simulation"]
        print(f"  HELD FOR CONFIRMATION — blast radius: {sim['exact_rows']} rows")
        print("     (a human operator must approve before this commits)")
    else:
        extra = ""
        if res.get("undo_action_id"):
            extra = f" | reversible, undo id = {res['undo_action_id'][:8]}…"
        print(f"  OK: {res.get('status') or res.get('row_count', 0)} rows{extra}")


async def main() -> None:
    try:
        pool = await asyncpg.create_pool(dsn=DB_DSN, min_size=1, max_size=4, timeout=5)
    except (OSError, asyncpg.PostgresError) as exc:
        print(
            f"Dev Postgres not reachable at {DB_DSN} ({exc}). Run `docker compose up`."
        )
        return

    audit = AuditLog(tempfile.mktemp())
    await audit.start()
    async with pool.acquire() as c:
        await c.execute("DROP TABLE IF EXISTS accounts")
        await c.execute(
            "CREATE TABLE accounts (id int PRIMARY KEY, owner text, balance int)"
        )
        await c.execute(
            "INSERT INTO accounts "
            "SELECT g, 'user' || g, g * 100 FROM generate_series(1, 50) g"
        )
        unique_columns = await load_unique_columns(c)

    policy = Policy(
        allowed_tables=frozenset({"accounts"}),
        simulation=SimulationConfig(
            enabled=True, precise=True, confirm_over_rows=10, block_over_rows=100000
        ),
        undo=UndoConfig(enabled=True),
    )
    store = UndoStore(policy.undo)
    sess = ShadowSession(pool, audit, policy, store, unique_columns=unique_columns)

    try:
        # 1 — destructive write is blocked with a structured reason
        banner("1. A destructive write is blocked (with a fix the agent can use)")
        show(
            "UPDATE accounts SET balance = 0",
            await sess.run_query("UPDATE accounts SET balance = 0"),
        )

        # 2 — the agent self-corrects and the safe version runs
        banner("2. The agent self-corrects using the suggested fix")
        show(
            "UPDATE accounts SET balance = 0 WHERE id = 1",
            await sess.run_query("UPDATE accounts SET balance = 0 WHERE id = 1"),
        )

        # 3 — blast radius is measured before a risky bulk write can commit
        banner("3. A risky bulk write: blast radius measured, held for confirmation")
        before = await pool.fetchval(
            "SELECT count(*) FROM accounts WHERE balance < 2000"
        )
        print(f"  (ground truth: {before} rows have balance < 2000)")
        show(
            "DELETE FROM accounts WHERE balance < 2000",
            await sess.run_query("DELETE FROM accounts WHERE balance < 2000"),
        )
        still = await pool.fetchval("SELECT count(*) FROM accounts")
        print(f"  table still intact: {still} rows (nothing was deleted)")

        # 4 — an allowed write, then a one-call undo
        banner("4. An allowed write is undone with a single revert")
        res = await sess.run_query("UPDATE accounts SET balance = 999999 WHERE id = 2")
        show("UPDATE accounts SET balance = 999999 WHERE id = 2", res)
        bad = await pool.fetchval("SELECT balance FROM accounts WHERE id = 2")
        print(f"  balance of account 2 is now {bad}")
        async with pool.acquire() as c:
            r = await revert(c, res["undo_action_id"], store)
            restored = await c.fetchval("SELECT balance FROM accounts WHERE id = 2")
        print(
            f"  revert({res['undo_action_id'][:8]}…) -> restored {r.rows_restored} row"
        )
        print(f"  balance of account 2 is back to {restored} (was 200)")

        banner(
            "Done — blocked, self-corrected, previewed, and undone. "
            "No real data touched."
        )
    finally:
        async with pool.acquire() as c:
            await c.execute("DROP TABLE IF EXISTS accounts")
        await audit.stop()
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
