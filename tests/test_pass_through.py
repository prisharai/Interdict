"""Day 1 tests: pass-through + shadow-mode logging.

Exercises ``ShadowSession`` directly (no MCP transport needed -- that's the point
of keeping it transport-agnostic). Verifies that:

* reads return correct rows and the SELECT status,
* writes/DDL pass through and report the affected-row count via the status tag,
* DB errors are captured and returned (not raised) so the agent can self-correct,
* every statement -- success or failure -- lands in the async audit log.

Skips cleanly when the dev Postgres isn't reachable, like the smoke test.
"""

import json
import os
import time
import uuid
from hashlib import sha256

import asyncpg
import pytest

from adapters.mcp_server import ShadowSession
from engine.audit import AuditLog

DB_DSN = os.environ.get(
    "AGENT_DB_DSN",
    "postgresql://postgres:postgres@localhost:5433/pagila",
)


@pytest.fixture
async def session(tmp_path):
    """A ShadowSession on a real pool, logging to a temp JSONL file.

    Yields ``(session, audit, log_path)``. Tears down the scratch table, audit
    log, and pool.
    """
    try:
        pool = await asyncpg.create_pool(dsn=DB_DSN, min_size=1, max_size=4, timeout=5)
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.skip(f"dev Postgres not reachable at {DB_DSN} ({exc})")

    log_path = tmp_path / "audit.jsonl"
    audit = AuditLog(log_path)
    await audit.start()
    sess = ShadowSession(pool, audit)
    try:
        yield sess, audit, log_path
    finally:
        # Best-effort cleanup of any scratch table a test created.
        try:
            async with pool.acquire() as conn:
                await conn.execute("DROP TABLE IF EXISTS _passthrough_scratch")
        finally:
            await audit.stop()
            await pool.close()


async def _wait_for_lines(path, n, timeout=5.0):
    """Poll the JSONL log until it has >= n lines (async logging is eventual)."""
    import asyncio

    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if path.exists():
            lines = [ln for ln in path.read_text().splitlines() if ln.strip()]
            if len(lines) >= n:
                return [json.loads(ln) for ln in lines]
        await asyncio.sleep(0.02)
    raise AssertionError(f"audit log never reached {n} lines within {timeout}s")


async def test_select_returns_rows(session):
    sess, _audit, _path = session
    result = await sess.run_query(
        "SELECT film_id, title FROM film ORDER BY film_id LIMIT 3"
    )
    assert result["error"] is None
    assert result["status"] == "SELECT 3"
    assert result["row_count"] == 3
    assert result["rows"][0]["film_id"] == 1
    assert "title" in result["rows"][0]


async def test_write_and_ddl_report_affected_counts(session):
    sess, _audit, _path = session

    created = await sess.run_query(
        "CREATE TABLE _passthrough_scratch (id int primary key, n int)"
    )
    assert created["status"] == "CREATE TABLE"
    assert created["error"] is None

    inserted = await sess.run_query(
        "INSERT INTO _passthrough_scratch (id, n) "
        "SELECT g, g FROM generate_series(1, 5) g"
    )
    # Command tag carries the affected-row count -- no string matching needed.
    assert inserted["status"] == "INSERT 0 5"

    updated = await sess.run_query("UPDATE _passthrough_scratch SET n = n + 1")
    assert updated["status"] == "UPDATE 5"

    deleted = await sess.run_query("DELETE FROM _passthrough_scratch WHERE id <= 2")
    assert deleted["status"] == "DELETE 2"


async def test_db_error_is_captured_not_raised(session):
    sess, _audit, _path = session
    # Must NOT raise -- shadow mode observes; the agent gets a structured error.
    result = await sess.run_query("SELECT * FROM table_that_does_not_exist")
    assert result["error"] is not None
    assert "Database execution failed" in result["error"]
    assert result["rows"] == []


async def test_every_statement_is_audited(session):
    sess, _audit, log_path = session
    task = f"test-task-{uuid.uuid4()}"

    await sess.run_query("SELECT 1 AS one", stated_task=task, agent="agent-x")
    await sess.run_query("SELECT * FROM nope_not_here", stated_task=task)

    entries = await _wait_for_lines(log_path, 2)
    by_sql_hash = {e["sql_sha256"]: e for e in entries}

    ok = by_sql_hash[sha256(b"SELECT 1 AS one").hexdigest()]
    assert ok["sql"] == "[REDACTED]"
    assert ok["sql_redacted"] is True
    assert ok["stated_task"] == "[REDACTED]"
    assert ok["stated_task_sha256"] == sha256(task.encode("utf-8")).hexdigest()
    assert ok["agent"] == "agent-x"
    assert ok["principal"]["id"] == "agent-x"
    assert ok["principal"]["kind"] == "agent"
    assert ok["principal"]["stated_task"] == "[REDACTED]"
    assert ok["status"] == "SELECT 1"
    assert ok["error"] is None
    assert "duration_ms" in ok and isinstance(ok["duration_ms"], (int, float))
    assert "ts" in ok

    failed = by_sql_hash[sha256(b"SELECT * FROM nope_not_here").hexdigest()]
    assert failed["error"] is not None  # failures are logged too (corpus)
    assert failed["principal"]["id"] == "anonymous"
