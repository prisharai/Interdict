"""Day 5 tests: reversibility / instant undo (needs Postgres).

Proves a recorded write can be reverted to restore prior state for UPDATE,
DELETE, and INSERT; that the undo log is the who/what/when audit trail; that a
revert can't be replayed; and that unsupported shapes are blocked by default.
Skips cleanly when the dev DB isn't up.
"""

import json
import os

import asyncpg
import pytest

from engine.classifier import classify
from engine.undo import UndoConfig, UndoStore, execute_with_undo, revert

DB_DSN = os.environ.get(
    "AGENT_DB_DSN",
    "postgresql://postgres:postgres@localhost:5433/pagila",
)
CFG = UndoConfig(enabled=True)
PERMISSIVE_CFG = UndoConfig(enabled=True, block_non_reversible=False)


@pytest.fixture
async def db():
    """(conn, store) with a fresh 3-row scratch table; dropped on teardown."""
    try:
        conn = await asyncpg.connect(dsn=DB_DSN, timeout=5)
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.skip(f"dev Postgres not reachable at {DB_DSN} ({exc})")
    await conn.execute("DROP TABLE IF EXISTS _undo_test")
    await conn.execute(
        "CREATE TABLE _undo_test (id int primary key, val text, amt numeric)"
    )
    await conn.execute(
        "INSERT INTO _undo_test VALUES (1,'a',1.5),(2,'b',2.5),(3,'c',3.5)"
    )
    store = UndoStore(CFG)
    try:
        yield conn, store
    finally:
        await conn.execute("DROP TABLE IF EXISTS _undo_test")
        await conn.close()


async def _state(conn):
    return [dict(r) for r in await conn.fetch("SELECT * FROM _undo_test ORDER BY id")]


async def _run(conn, store, sql, task="t"):
    return await execute_with_undo(
        conn,
        sql,
        classify(sql),
        agent="agent-x",
        stated_task=task,
        config=CFG,
        store=store,
    )


async def _run_permissive(conn, store, sql, task="t"):
    return await execute_with_undo(
        conn,
        sql,
        classify(sql),
        agent="agent-x",
        stated_task=task,
        config=PERMISSIVE_CFG,
        store=store,
    )


# --- Round-trips -------------------------------------------------------------


async def test_update_is_reversible(db):
    conn, store = db
    before = await _state(conn)
    out = await _run(conn, store, "UPDATE _undo_test SET val='X', amt=0 WHERE id <= 2")
    assert out.reversible and out.status == "UPDATE 2"
    assert (await _state(conn))[0]["val"] == "X"

    r = await revert(conn, out.action_id, store)
    assert r.ok and r.rows_restored == 2
    assert await _state(conn) == before  # exact prior state, types intact


async def test_delete_is_reversible(db):
    conn, store = db
    before = await _state(conn)
    out = await _run(conn, store, "DELETE FROM _undo_test WHERE id = 3")
    assert out.reversible and out.status == "DELETE 1"
    assert len(await _state(conn)) == 2

    r = await revert(conn, out.action_id, store)
    assert r.ok
    assert await _state(conn) == before  # row re-inserted


async def test_insert_is_reversible(db):
    conn, store = db
    before = await _state(conn)
    out = await _run(conn, store, "INSERT INTO _undo_test VALUES (4,'d',4.5)")
    assert out.reversible and out.status == "INSERT 0 1"
    assert len(await _state(conn)) == 4

    r = await revert(conn, out.action_id, store)
    assert r.ok
    assert await _state(conn) == before  # inserted row removed


# --- Audit trail + revert semantics ------------------------------------------


async def test_undo_record_is_the_audit_trail(db):
    conn, store = db
    out = await _run(conn, store, "DELETE FROM _undo_test WHERE id = 1", task="cleanup")
    rec = await store.get(conn, out.action_id)
    assert rec["agent"] == "agent-x"
    assert rec["stated_task"] == "cleanup"
    principal = json.loads(rec["principal"])
    assert principal["id"] == "agent-x"
    assert principal["kind"] == "agent"
    assert principal["stated_task"] == "cleanup"
    assert rec["operation"] == "delete"
    assert rec["target_table"] == "_undo_test"
    assert rec["row_count"] == 1
    assert rec["status"] == "active"


async def test_revert_records_reverting_principal(db):
    conn, store = db
    out = await _run(conn, store, "DELETE FROM _undo_test WHERE id = 1")
    result = await revert(conn, out.action_id, store, agent="operator-x")
    assert result.ok
    rec = await store.get(conn, out.action_id)
    reverted_by = json.loads(rec["reverted_by"])
    assert reverted_by["id"] == "operator-x"
    assert reverted_by["kind"] == "agent"


async def test_revert_cannot_be_replayed(db):
    conn, store = db
    out = await _run(conn, store, "DELETE FROM _undo_test WHERE id = 1")
    assert (await revert(conn, out.action_id, store)).ok
    second = await revert(conn, out.action_id, store)
    assert not second.ok and "already reverted" in second.error


async def test_revert_unknown_action_id(db):
    conn, store = db
    await store.ensure_schema(conn)
    r = await revert(conn, "00000000-0000-0000-0000-000000000000", store)
    assert not r.ok and "no such action_id" in r.error


# --- Unsupported shapes block by default -------------------------------------


async def test_multi_table_update_blocks_when_undo_required(db):
    conn, store = db
    out = await _run(
        conn,
        store,
        "UPDATE _undo_test t SET val='z' FROM (SELECT 1 AS id) s WHERE t.id = s.id",
    )
    assert out.reversible is False
    assert out.blocked is True
    assert out.action_id is None
    assert "FROM/USING" in out.reason
    assert out.status is None
    assert (await _state(conn))[0]["val"] == "a"


async def test_non_reversible_writes_can_be_opted_into_for_local_eval(db):
    conn, store = db
    out = await _run_permissive(
        conn,
        store,
        "UPDATE _undo_test t SET val='z' FROM (SELECT 1 AS id) s WHERE t.id = s.id",
    )
    assert out.reversible is False
    assert out.blocked is False
    assert out.status == "UPDATE 1"


async def test_update_without_primary_key_blocks_by_default(db):
    conn, store = db
    await conn.execute("CREATE TABLE _undo_nopk (id int, val text)")
    await conn.execute("INSERT INTO _undo_nopk VALUES (1,'a')")
    try:
        out = await _run(conn, store, "UPDATE _undo_nopk SET val='b' WHERE id = 1")
        assert out.reversible is False
        assert out.blocked is True
        assert "primary key" in out.reason
        assert out.status is None
        assert await conn.fetchval("SELECT val FROM _undo_nopk WHERE id = 1") == "a"
    finally:
        await conn.execute("DROP TABLE _undo_nopk")


async def test_update_changing_primary_key_blocks_by_default(db):
    # We match the old PK on revert; if the UPDATE changes the PK, the old key
    # is gone -- so we refuse rather than silently fail to restore.
    conn, store = db
    out = await _run(conn, store, "UPDATE _undo_test SET id = id + 100 WHERE id = 1")
    assert out.reversible is False
    assert out.blocked is True
    assert "primary-key" in out.reason
    assert out.status is None
    assert await conn.fetchval("SELECT count(*) FROM _undo_test WHERE id = 1") == 1


# --- QA regressions ----------------------------------------------------------


async def test_qa_p0_upsert_is_not_reversible(db):
    # ON CONFLICT DO UPDATE would be reverted by deleting a pre-existing row.
    conn, store = db
    out = await _run(
        conn,
        store,
        "INSERT INTO _undo_test (id, val) VALUES (1, 'new') "
        "ON CONFLICT (id) DO UPDATE SET val = excluded.val",
    )
    assert out.reversible is False
    assert out.blocked is True
    assert "ON CONFLICT" in out.reason
    assert out.status is None
    assert await conn.fetchval("SELECT val FROM _undo_test WHERE id = 1") == "a"


async def test_qa_p0_revert_conflicts_on_concurrent_change(db):
    # Agent updates val; a SEPARATE session then changes amt on the same row.
    conn, store = db
    out = await _run(conn, store, "UPDATE _undo_test SET val='agent' WHERE id=1")
    other = await asyncpg.connect(dsn=DB_DSN)
    try:
        await other.execute("UPDATE _undo_test SET amt=99 WHERE id=1")
    finally:
        await other.close()

    r = await revert(conn, out.action_id, store)
    assert r.ok is False and r.conflict is True
    # the later change is preserved -- revert clobbered nothing
    row = await conn.fetchrow("SELECT * FROM _undo_test WHERE id=1")
    assert row["amt"] == 99
    assert row["val"] == "agent"


async def test_qa_p0_insert_revert_conflicts_if_row_modified(db):
    conn, store = db
    out = await _run(conn, store, "INSERT INTO _undo_test VALUES (9,'i',9.9)")
    await conn.execute("UPDATE _undo_test SET val='changed' WHERE id=9")
    r = await revert(conn, out.action_id, store)
    assert r.ok is False and r.conflict is True
    assert await conn.fetchval("SELECT count(*) FROM _undo_test WHERE id=9") == 1


async def test_qa_p1_returning_write_preserves_rows_not_reversible(db):
    conn, store = db
    out = await _run_permissive(
        conn, store, "UPDATE _undo_test SET val='x' WHERE id=1 RETURNING id, val"
    )
    assert out.reversible is False
    assert "RETURNING" in out.reason
    assert out.rows == [{"id": 1, "val": "x"}]  # RETURNING rows preserved


async def test_qa_p1_write_cte_runs_normally_not_reversible(db):
    # A valid WITH ... UPDATE can still run only in permissive local-eval mode.
    conn, store = db
    out = await _run_permissive(
        conn,
        store,
        "WITH ids AS (SELECT 1 AS id) "
        "UPDATE _undo_test SET val='x' WHERE id IN (SELECT id FROM ids)",
    )
    assert out.reversible is False
    assert "WITH" in out.reason
    assert out.error is None
    assert out.status == "UPDATE 1"
    assert (await _state(conn))[0]["val"] == "x"
