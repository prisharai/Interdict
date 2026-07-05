"""Concurrency races on the approval and undo paths (needs Postgres).

The product's core promise is that authorization and reversal hold up under
real parallelism, not just in sequential tests: a hold executes exactly once
no matter how many agents retry it, one operator decision wins, expired holds
are dead everywhere, and a revert never double-fires or clobbers a later
write. Each test here races real connections from an asyncpg pool so the
row-lock/status-guard behavior is Postgres's, not the event loop's.

Uses a dedicated sidecar schema so it never touches real adb_undo data.
Skips cleanly when the dev DB isn't up.
"""

import asyncio
import os
import tempfile
from pathlib import Path

import asyncpg
import pytest

from adapters.mcp_server import ShadowSession
from engine.approvals import APPROVED, DENIED, ApprovalStore, token_hash
from engine.audit import AuditLog
from engine.classifier import classify
from engine.policy import Policy
from engine.simulate import SimulationConfig, load_unique_columns
from engine.undo import UndoConfig, UndoStore, execute_with_undo, revert

DB_DSN = os.environ.get(
    "AGENT_DB_DSN",
    "postgresql://postgres:postgres@localhost:5433/pagila",
)
SCHEMA = "adb_race_test"
OPERATOR_TOKEN = "race-test-operator-token-with-at-least-32-chars"
TOKEN_HASH = token_hash(OPERATOR_TOKEN)
UNDO_CFG = UndoConfig(enabled=True, schema=SCHEMA)


@pytest.fixture
async def pool():
    """Connection pool + fresh scratch table; sidecar schema dropped after."""
    try:
        p = await asyncpg.create_pool(dsn=DB_DSN, min_size=2, max_size=8, timeout=5)
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.skip(f"dev Postgres not reachable at {DB_DSN} ({exc})")
    async with p.acquire() as conn:
        await conn.execute(f'DROP SCHEMA IF EXISTS "{SCHEMA}" CASCADE')
        await conn.execute("DROP TABLE IF EXISTS _race_test")
        await conn.execute("CREATE TABLE _race_test (id int primary key, val text)")
        await conn.execute("INSERT INTO _race_test VALUES (1,'a'),(2,'b'),(3,'c')")
    try:
        yield p
    finally:
        async with p.acquire() as conn:
            await conn.execute("DROP TABLE IF EXISTS _race_test")
            await conn.execute(f'DROP SCHEMA IF EXISTS "{SCHEMA}" CASCADE')
        await p.close()


async def _create_hold(pool, store: ApprovalStore, approval_id: str) -> None:
    async with pool.acquire() as conn:
        await store.create(
            conn,
            approval_id=approval_id,
            sql="DELETE FROM _race_test WHERE id <= 2",
            stated_task="race test",
            agent="race-agent",
            principal=None,
            simulation={"affected_rows": 2},
            operator_token_hash=TOKEN_HASH,
        )


async def _backdate(pool, approval_id: str, hours: int = 2) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            f'UPDATE "{SCHEMA}".pending_approval '
            f"SET created_at = now() - interval '{hours} hours' "
            "WHERE approval_id = $1",
            approval_id,
        )


# --- Approval-store races ----------------------------------------------------


async def test_concurrent_claims_execute_exactly_once(pool):
    """N agents racing run_approved_query's claim: exactly one wins."""
    store = ApprovalStore(SCHEMA)
    approval_id = "00000000-0000-0000-0000-000000000001"
    await _create_hold(pool, store, approval_id)
    async with pool.acquire() as conn:
        assert (
            await store.decide(
                conn,
                approval_id,
                approve=True,
                operator_token_hash=TOKEN_HASH,
                decided_by="op",
            )
            == APPROVED
        )

    async def claim():
        async with pool.acquire() as conn:
            return await store.claim_approved(conn, approval_id)

    results = await asyncio.gather(*(claim() for _ in range(8)))
    winners = [r for r in results if r is not None]
    assert len(winners) == 1
    async with pool.acquire() as conn:
        row = await store.get(conn, approval_id)
    assert row["status"] == "executed"


async def test_concurrent_approve_and_deny_exactly_one_decision(pool):
    """Two operators race approve-vs-deny: one decision lands, it's final."""
    store = ApprovalStore(SCHEMA)
    approval_id = "00000000-0000-0000-0000-000000000002"
    await _create_hold(pool, store, approval_id)

    async def decide(approve: bool):
        async with pool.acquire() as conn:
            return await store.decide(
                conn,
                approval_id,
                approve=approve,
                operator_token_hash=TOKEN_HASH,
                decided_by="op-a" if approve else "op-b",
            )

    results = await asyncio.gather(*(decide(i % 2 == 0) for i in range(8)))
    decisions = [r for r in results if r is not None]
    assert len(decisions) == 1
    async with pool.acquire() as conn:
        row = await store.get(conn, approval_id)
    assert row["status"] == decisions[0]
    assert row["status"] in (APPROVED, DENIED)


async def test_approved_hold_expired_before_execution_cannot_run(pool):
    """The TTL race: approved in time, but stale by execution time -> dead."""
    store = ApprovalStore(SCHEMA)
    approval_id = "00000000-0000-0000-0000-000000000003"
    await _create_hold(pool, store, approval_id)
    async with pool.acquire() as conn:
        await store.decide(
            conn,
            approval_id,
            approve=True,
            operator_token_hash=TOKEN_HASH,
            decided_by="op",
        )
    await _backdate(pool, approval_id)
    async with pool.acquire() as conn:
        assert await store.claim_approved(conn, approval_id) is None
        row = await store.get(conn, approval_id)
    assert row["status"] == "approved" and row["expired"] is True


async def test_expired_pending_hold_is_unlisted_and_undecidable(pool):
    store = ApprovalStore(SCHEMA)
    approval_id = "00000000-0000-0000-0000-000000000004"
    await _create_hold(pool, store, approval_id)
    await _backdate(pool, approval_id)
    async with pool.acquire() as conn:
        listed = await store.list_pending(conn)
        decided = await store.decide(
            conn,
            approval_id,
            approve=True,
            operator_token_hash=TOKEN_HASH,
            decided_by="op",
        )
    assert listed == []
    assert decided is None


# --- Undo races ---------------------------------------------------------------


async def _undo_write(pool, sql: str, store: UndoStore):
    async with pool.acquire() as conn:
        return await execute_with_undo(
            conn,
            sql,
            classify(sql),
            agent="race-agent",
            stated_task="race test",
            config=UNDO_CFG,
            store=store,
        )


async def test_concurrent_reverts_restore_exactly_once(pool):
    """Two racing reverts of one action: one restores, the other is refused."""
    store = UndoStore(UNDO_CFG)
    out = await _undo_write(
        pool, "UPDATE _race_test SET val = 'changed' WHERE id = 1", store
    )
    assert out.action_id is not None

    async def do_revert():
        async with pool.acquire() as conn:
            return await revert(conn, out.action_id, store)

    results = await asyncio.gather(do_revert(), do_revert())
    oks = [r for r in results if r.ok]
    losers = [r for r in results if not r.ok]
    assert len(oks) == 1 and oks[0].rows_restored == 1
    # The loser must not have half-applied anything: conflict or already-done.
    assert losers[0].conflict or "already reverted" in (losers[0].error or "")
    async with pool.acquire() as conn:
        val = await conn.fetchval("SELECT val FROM _race_test WHERE id = 1")
    assert val == "a"


async def test_concurrent_writes_serialize_and_lifo_revert_restores(pool):
    """Two agents update the same row at once; undo history stays coherent:
    reverting out of order conflicts (never clobbers the later write), and
    LIFO revert walks the row back to the original state."""
    store = UndoStore(UNDO_CFG)
    first, second = await asyncio.gather(
        _undo_write(pool, "UPDATE _race_test SET val = 'from-A' WHERE id = 2", store),
        _undo_write(pool, "UPDATE _race_test SET val = 'from-B' WHERE id = 2", store),
    )
    assert first.action_id and second.action_id
    assert first.captured_rows == 1 and second.captured_rows == 1

    async with pool.acquire() as conn:
        # Chronological order of the two undo records (row locks serialized them).
        ordered = [
            r["action_id"]
            for r in await conn.fetch(
                f'SELECT action_id::text FROM "{SCHEMA}".undo_log '
                "WHERE target_table = '_race_test' AND operation = 'update' "
                "AND action_id::text = ANY($1::text[]) ORDER BY created_at",
                [first.action_id, second.action_id],
            )
        ]
        older, newer = ordered

        # FIFO (wrong order) refuses: the row no longer matches its after-image.
        fifo = await revert(conn, older, store)
        assert fifo.ok is False and fifo.conflict is True

        # LIFO walks back cleanly: newest first, then the older one.
        assert (await revert(conn, newer, store)).ok is True
        assert (await revert(conn, older, store)).ok is True
        val = await conn.fetchval("SELECT val FROM _race_test WHERE id = 2")
    assert val == "b"


# --- Full-stack race through the session -------------------------------------


async def test_full_stack_approved_write_executes_exactly_once(pool):
    """Concurrent run_approved_query calls on a real session: the write lands
    once, exactly one caller gets the success payload."""
    audit = AuditLog(Path(tempfile.gettempdir()) / "interdict-race-audit.jsonl")
    await audit.start()
    policy = Policy(
        allowed_tables=frozenset({"_race_test"}),
        simulation=SimulationConfig(
            enabled=True, precise=True, confirm_over_rows=1, block_over_rows=100000
        ),
        undo=UNDO_CFG,
    )
    async with pool.acquire() as conn:
        unique_columns = await load_unique_columns(conn)
    sess = ShadowSession(
        pool,
        audit,
        policy,
        UndoStore(policy.undo),
        unique_columns=unique_columns,
        operator_token=OPERATOR_TOKEN,
    )
    try:
        held = await sess.run_query(
            "DELETE FROM _race_test WHERE id <= 2",
            stated_task="remove two rows",
            agent="race-agent",
        )
        approval_id = held["approval_id"]
        assert approval_id

        async with pool.acquire() as conn:
            decided = await ApprovalStore(SCHEMA).decide(
                conn,
                approval_id,
                approve=True,
                operator_token_hash=TOKEN_HASH,
                decided_by="op",
            )
        assert decided == APPROVED

        results = await asyncio.gather(
            *(
                sess.run_approved_query(approval_id, executor="race-agent")
                for _ in range(4)
            )
        )
        winners = [r for r in results if r.get("ok")]
        assert len(winners) == 1
        assert winners[0]["undo_action_id"]

        async with pool.acquire() as conn:
            remaining = await conn.fetchval("SELECT count(*) FROM _race_test")
        assert remaining == 1  # the DELETE ran exactly once
    finally:
        await audit.stop()
