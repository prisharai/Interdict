"""Fault injection: infrastructure failures, not policy decisions.

The runtime contract says writes fail closed on uncertainty and logging is
fail-open. The enforcement tests prove that for *decisions*; these tests
prove it when the infrastructure itself fails mid-flight: a backend
terminated while a write waits on a lock, a statement timeout firing inside
undo capture, a revert interrupted halfway, and an audit disk that stops
accepting writes. In every case the invariant is the same -- no partial
effects, no orphan records, the failure is contained (a structured error,
never an exception through the safety layer), and the system stays usable.

DB tests skip cleanly when the dev Postgres isn't up; the audit test is
local-only and always runs.
"""

import asyncio
import os

import asyncpg
import pytest

from engine.audit import AuditLog
from engine.classifier import classify
from engine.undo import UndoConfig, UndoStore, execute_with_undo, revert

DB_DSN = os.environ.get(
    "AGENT_DB_DSN",
    "postgresql://postgres:postgres@localhost:5433/pagila",
)
SCHEMA = "adb_fault_test"
CFG = UndoConfig(enabled=True, schema=SCHEMA)


@pytest.fixture
async def db():
    """Three independent connections (victim, blocker, admin) + scratch table."""
    conns = []
    try:
        for _ in range(3):
            conns.append(await asyncpg.connect(dsn=DB_DSN, timeout=5))
    except (OSError, asyncpg.PostgresError) as exc:
        for c in conns:
            await c.close()
        pytest.skip(f"dev Postgres not reachable at {DB_DSN} ({exc})")
    victim, blocker, admin = conns
    await admin.execute(f'DROP SCHEMA IF EXISTS "{SCHEMA}" CASCADE')
    await admin.execute("DROP TABLE IF EXISTS _fault_test")
    await admin.execute("CREATE TABLE _fault_test (id int primary key, val text)")
    await admin.execute("INSERT INTO _fault_test VALUES (1,'a'),(2,'b'),(3,'c')")
    try:
        yield victim, blocker, admin
    finally:
        await admin.execute("DROP TABLE IF EXISTS _fault_test")
        await admin.execute(f'DROP SCHEMA IF EXISTS "{SCHEMA}" CASCADE')
        for c in conns:
            if not c.is_closed():
                await c.close()


async def _wait_until_blocked(admin, pid: int, timeout_s: float = 5.0) -> None:
    """Poll pg_stat_activity until `pid` is waiting on a lock (deterministic
    alternative to sleeping and hoping)."""
    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        wait = await admin.fetchval(
            "SELECT wait_event_type FROM pg_stat_activity WHERE pid = $1", pid
        )
        if wait == "Lock":
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"backend {pid} never blocked on a lock")


async def _undo_write(conn, sql: str, store: UndoStore):
    return await execute_with_undo(
        conn,
        sql,
        classify(sql),
        agent="fault-agent",
        stated_task="fault test",
        config=CFG,
        store=store,
    )


async def _row_val(admin, row_id: int) -> str:
    return await admin.fetchval("SELECT val FROM _fault_test WHERE id = $1", row_id)


async def _undo_records(admin) -> int:
    return await admin.fetchval(
        f'SELECT count(*) FROM "{SCHEMA}".undo_log '
        "WHERE target_table = '_fault_test'"
    )


# --- Backend terminated mid-write ---------------------------------------------


async def test_backend_killed_mid_write_applies_nothing(db):
    """Terminate the writer's backend while its UPDATE waits on a lock: the
    write must vanish without a trace -- no row change, no undo record, and a
    structured error rather than an exception through the safety layer."""
    victim, blocker, admin = db
    store = UndoStore(CFG)
    victim_pid = await victim.fetchval("SELECT pg_backend_pid()")

    blocker_tr = blocker.transaction()
    await blocker_tr.start()
    await blocker.execute("SELECT * FROM _fault_test WHERE id = 1 FOR UPDATE")

    async def doomed_write():
        return await _undo_write(
            victim, "UPDATE _fault_test SET val = 'never' WHERE id = 1", store
        )

    write_task = asyncio.create_task(doomed_write())
    await _wait_until_blocked(admin, victim_pid)
    await admin.execute("SELECT pg_terminate_backend($1)", victim_pid)

    outcome = await write_task  # must return, not raise
    await blocker_tr.rollback()

    assert outcome.action_id is None
    assert outcome.error is not None
    assert await _row_val(admin, 1) == "a"
    assert await _undo_records(admin) == 0


# --- Statement timeout inside undo capture ------------------------------------


async def test_undo_capture_timeout_fails_closed_and_connection_survives(db):
    """A statement timeout while capturing the before-image aborts the whole
    write atomically, and the connection stays usable afterwards."""
    victim, blocker, admin = db
    store = UndoStore(CFG)
    await victim.execute("SET statement_timeout = 300")

    blocker_tr = blocker.transaction()
    await blocker_tr.start()
    await blocker.execute("SELECT * FROM _fault_test WHERE id = 2 FOR UPDATE")

    outcome = await _undo_write(
        victim, "UPDATE _fault_test SET val = 'never' WHERE id = 2", store
    )
    await blocker_tr.rollback()

    assert outcome.action_id is None
    assert outcome.error is not None
    assert await _row_val(admin, 2) == "b"
    assert await _undo_records(admin) == 0
    # The rollback succeeded and the connection is healthy, not poisoned.
    await victim.execute("RESET statement_timeout")
    assert await victim.fetchval("SELECT 1") == 1


# --- Revert interrupted, then retried ------------------------------------------


async def test_interrupted_revert_leaves_record_active_and_retryable(db):
    """A revert that dies mid-flight must restore nothing, leave the undo
    record active, and succeed cleanly when retried later."""
    victim, blocker, admin = db
    store = UndoStore(CFG)
    out = await _undo_write(
        victim, "UPDATE _fault_test SET val = 'changed' WHERE id = 3", store
    )
    assert out.action_id is not None

    blocker_tr = blocker.transaction()
    await blocker_tr.start()
    await blocker.execute("SELECT * FROM _fault_test WHERE id = 3 FOR UPDATE")

    await victim.execute("SET statement_timeout = 300")
    interrupted = await revert(victim, out.action_id, store)
    assert interrupted.ok is False
    assert interrupted.error is not None

    # Nothing half-applied: row still holds the write, record still active.
    rec = await store.get(victim, out.action_id)
    assert rec["status"] == "active"
    assert await _row_val(admin, 3) == "changed"

    # Release the lock; the retry must now succeed exactly as normal.
    await blocker_tr.rollback()
    await victim.execute("RESET statement_timeout")
    retried = await revert(victim, out.action_id, store)
    assert retried.ok is True and retried.rows_restored == 1
    assert await _row_val(admin, 3) == "c"


# --- Audit disk failure ---------------------------------------------------------


async def test_audit_disk_failure_is_fail_open_and_visible(tmp_path):
    """When the log directory stops accepting writes, queries must feel
    nothing: record() never raises, the consumer survives to keep draining,
    and the loss is visible in status() instead of silent."""
    logdir = tmp_path / "logs"
    logdir.mkdir()
    audit = AuditLog(logdir / "audit.jsonl", max_queue=100)
    await audit.start()

    # Break the disk out from under the running consumer.
    (logdir / "audit.jsonl").unlink()
    logdir.chmod(0o500)
    try:
        for i in range(10):
            audit.record({"event": "fault-test", "i": i})  # must never raise
        await asyncio.sleep(0.3)  # let the consumer attempt (and fail) a write

        status = audit.status()
        assert status["running"] is True  # consumer survived the OSError
        assert audit.dropped >= 1  # the loss is counted, not silent
        audit.record({"event": "still-accepting"})  # hot path unaffected
    finally:
        logdir.chmod(0o700)
        await audit.stop()
