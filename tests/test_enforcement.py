"""Day 3 integration: policy enforcement through the adapter (needs Postgres).

Proves the engine's decision actually governs the database: a blocked statement
never reaches Postgres, an allowed read runs, and an unbounded read comes back
capped by an injected LIMIT. Skips cleanly when the dev DB isn't up.
"""

import asyncio
import json
import os

import asyncpg
import pytest

from adapters.mcp_server import ShadowSession
from engine.audit import AuditLog
from engine.intent import IntentConfig
from engine.policy import Policy
from engine.simulate import SimulationConfig, load_unique_columns
from engine.undo import UndoConfig, UndoStore

DB_DSN = os.environ.get(
    "AGENT_DB_DSN",
    "postgresql://postgres:postgres@localhost:5433/pagila",
)
STRONG_OPERATOR_TOKEN = "test-operator-token-with-at-least-32-chars"


@pytest.fixture
async def make_session(tmp_path):
    """Factory: build a ShadowSession with a given policy on the real pool."""
    pools = []
    audits = []

    async def _make(policy):
        try:
            pool = await asyncpg.create_pool(
                dsn=DB_DSN, min_size=1, max_size=4, timeout=5
            )
        except (OSError, asyncpg.PostgresError) as exc:
            pytest.skip(f"dev Postgres not reachable at {DB_DSN} ({exc})")
        log = tmp_path / f"audit{len(pools)}.jsonl"
        audit = AuditLog(log)
        await audit.start()
        pools.append(pool)
        audits.append(audit)
        store = UndoStore(policy.undo) if policy.undo.enabled else None
        return ShadowSession(pool, audit, policy, store), log

    yield _make

    for audit in audits:
        await audit.stop()
    for pool in pools:
        await pool.close()


async def test_blocked_statement_never_touches_the_database(make_session):
    # A disallowed table that also does not exist: if the statement were executed
    # we'd get a Postgres "relation does not exist" error. Blocked => error is
    # None, proving we never ran it.
    sess, _ = await make_session(Policy(allowed_tables=frozenset({"film"})))
    res = await sess.run_query("SELECT * FROM secret_accounts")
    assert res["blocked"] is True
    assert res["error"] is None  # the DB was never asked
    assert res["rows"] == []
    assert any(v["reason_code"] == "TABLE_NOT_ALLOWED" for v in res["violations"])


async def test_allowed_read_runs(make_session):
    sess, _ = await make_session(Policy(allowed_tables=frozenset({"film"})))
    res = await sess.run_query("SELECT film_id FROM film WHERE film_id = 1")
    assert res["blocked"] is False
    assert res["row_count"] == 1
    assert res["rows"][0]["film_id"] == 1


async def test_injected_limit_caps_rows_returned(make_session):
    # actor has 200 rows; max_rows_read=5 must cap the result at 5.
    sess, _ = await make_session(
        Policy(allowed_tables=frozenset({"actor"}), max_rows_read=5)
    )
    res = await sess.run_query("SELECT * FROM actor")
    assert res["blocked"] is False
    assert res["row_count"] == 5


async def test_observe_mode_logs_decision_but_still_runs(make_session):
    # film is NOT allowed -> the decision is "block", but observe mode runs anyway.
    sess, log = await make_session(
        Policy(mode="observe", allowed_tables=frozenset({"actor"}))
    )
    res = await sess.run_query("SELECT film_id FROM film WHERE film_id = 1")
    assert res["blocked"] is False  # observe never blocks the response
    assert res["row_count"] == 1  # it actually ran
    # ...but the recorded decision shows it WOULD have been blocked.
    await sess._audit.stop()  # flush
    entry = json.loads(log.read_text().splitlines()[-1])
    assert entry["decision"]["allowed"] is False
    assert entry["decision"]["violations"][0]["reason_code"] == "TABLE_NOT_ALLOWED"


async def test_observe_mode_does_not_rewrite_live_reads(make_session):
    # P1c regression: observe must run the ORIGINAL sql, not an injected LIMIT --
    # actor has 200 rows; observe with max_rows_read=5 must still return all 200.
    sess, _ = await make_session(
        Policy(mode="observe", allowed_tables=frozenset({"actor"}), max_rows_read=5)
    )
    res = await sess.run_query("SELECT * FROM actor")
    assert res["blocked"] is False
    assert res["row_count"] == 200  # NOT capped at 5


# --- Day 4: blast-radius simulation through the adapter -----------------------


@pytest.fixture
async def scratch():
    """A throwaway 50-row table for write tests; dropped on teardown."""
    try:
        c = await asyncpg.connect(dsn=DB_DSN, timeout=5)
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.skip(f"dev Postgres not reachable at {DB_DSN} ({exc})")
    await c.execute("DROP TABLE IF EXISTS _sim_scratch")
    await c.execute("CREATE TABLE _sim_scratch (id int primary key)")
    await c.execute("INSERT INTO _sim_scratch SELECT generate_series(1, 50)")
    try:
        yield c
    finally:
        await c.execute("DROP TABLE IF EXISTS _sim_scratch")
        await c.close()


async def test_risky_write_held_until_operator_approval(make_session, scratch):
    sess, _ = await make_session(
        Policy(
            allowed_tables=None,
            simulation=SimulationConfig(
                enabled=True, precise=True, confirm_over_rows=10, block_over_rows=100000
            ),
        )
    )
    sql = "DELETE FROM _sim_scratch WHERE id <= 29"  # 29 rows > confirm limit 10

    # Agent attempt: held for confirmation, blast radius measured, NOT executed.
    res = await sess.run_query(sql)
    assert res["requires_confirmation"] is True
    assert res["approval_id"]
    assert res["blocked"] is False
    assert res["simulation"]["exact_rows"] == 29
    assert await scratch.fetchval("SELECT count(*) FROM _sim_scratch") == 50

    # Out-of-band operator approval (the agent can't reach this) runs it.
    res2 = await sess.run_query(sql, operator_approved=True)
    assert res2["requires_confirmation"] is False
    assert res2["status"] == "DELETE 29"
    assert await scratch.fetchval("SELECT count(*) FROM _sim_scratch") == 21


async def test_risky_write_over_block_limit_is_blocked(make_session, scratch):
    sess, _ = await make_session(
        Policy(
            allowed_tables=None,
            simulation=SimulationConfig(enabled=True, precise=True, block_over_rows=10),
        )
    )
    # 39 rows > block limit 10 -> blocked outright, even with operator approval.
    res = await sess.run_query(
        "DELETE FROM _sim_scratch WHERE id <= 39", operator_approved=True
    )
    assert res["blocked"] is True
    assert res["block_reason"] == "BLAST_RADIUS_EXCEEDED"
    assert "over the limit" in res["block_message"]
    assert any(v["reason_code"] == "BLAST_RADIUS_EXCEEDED" for v in res["violations"])
    assert res["simulation"]["exact_rows"] == 39
    assert (
        await scratch.fetchval("SELECT count(*) FROM _sim_scratch") == 50
    )  # untouched


def test_agent_tool_has_no_confirmation_bypass():
    # QA P1e: the MCP tool (the agent's only interface) must not expose any way
    # to approve a held write -- that would be the agent confirming itself.
    import inspect

    from adapters.mcp_server import run_query as tool

    params = set(inspect.signature(tool).parameters)
    assert "confirm" not in params
    assert "operator_approved" not in params  # operator seam is server-side only


async def test_operator_approval_requires_token(make_session, scratch):
    sess, _ = await make_session(
        Policy(
            allowed_tables=None,
            simulation=SimulationConfig(
                enabled=True, precise=True, confirm_over_rows=10, block_over_rows=100000
            ),
        )
    )
    sess._operator_token = STRONG_OPERATOR_TOKEN
    held = await sess.run_query("DELETE FROM _sim_scratch WHERE id <= 29")
    approval_id = held["approval_id"]

    denied = await sess.approve_query(
        approval_id, operator_token="wrong", operator="human-1"
    )
    assert denied["ok"] is False
    assert await scratch.fetchval("SELECT count(*) FROM _sim_scratch") == 50

    approved = await sess.approve_query(
        approval_id, operator_token=STRONG_OPERATOR_TOKEN, operator="human-1"
    )
    assert approved["ok"] is True
    assert approved["status"] == "DELETE 29"
    assert await scratch.fetchval("SELECT count(*) FROM _sim_scratch") == 21


async def test_reads_are_not_simulated_through_adapter(make_session):
    sess, _ = await make_session(
        Policy(
            allowed_tables=frozenset({"film"}),
            simulation=SimulationConfig(enabled=True),
        )
    )
    res = await sess.run_query("SELECT film_id FROM film WHERE film_id = 1")
    assert res["simulation"] is None  # reads are never simulated
    assert res["row_count"] == 1


# --- Day 5: reversible writes through the adapter -----------------------------


async def test_write_is_reversible_through_adapter(make_session, scratch):
    sess, _ = await make_session(
        Policy(allowed_tables=None, undo=UndoConfig(enabled=True))
    )
    # _sim_scratch has 50 rows (id 1..50); add a non-PK column to mutate.
    await scratch.execute("ALTER TABLE _sim_scratch ADD COLUMN label text")
    res = await sess.run_query(
        "UPDATE _sim_scratch SET label = 'tagged' WHERE id <= 3",
        stated_task="tag rows",
        agent="agent-7",
    )
    assert res["reversible"] is True
    assert res["undo_action_id"]
    tagged = "SELECT count(*) FROM _sim_scratch WHERE label = 'tagged'"
    assert await scratch.fetchval(tagged) == 3

    rev = await sess.revert_write(res["undo_action_id"], agent="agent-7")
    assert rev["ok"] is True
    assert rev["operation"] == "update"
    assert await scratch.fetchval(tagged) == 0  # labels restored to NULL


async def test_revert_requires_same_agent_or_operator_token(make_session, scratch):
    sess, _ = await make_session(
        Policy(allowed_tables=None, undo=UndoConfig(enabled=True))
    )
    sess._operator_token = STRONG_OPERATOR_TOKEN
    await scratch.execute("ALTER TABLE _sim_scratch ADD COLUMN label text")
    res = await sess.run_query(
        "UPDATE _sim_scratch SET label = 'tagged' WHERE id = 1",
        agent="agent-owner",
    )
    denied = await sess.revert_write(res["undo_action_id"], agent="agent-other")
    assert denied["ok"] is False
    assert denied["unauthorized"] is True
    assert (
        await scratch.fetchval("SELECT label FROM _sim_scratch WHERE id = 1")
        == "tagged"
    )

    approved = await sess.revert_write(
        res["undo_action_id"],
        agent="agent-other",
        operator_token=STRONG_OPERATOR_TOKEN,
    )
    assert approved["ok"] is True
    assert await scratch.fetchval("SELECT label FROM _sim_scratch WHERE id = 1") is None


async def test_operator_token_rejects_short_configured_secret(make_session, scratch):
    sess, _ = await make_session(
        Policy(
            allowed_tables=None,
            simulation=SimulationConfig(
                enabled=True, precise=True, confirm_over_rows=10, block_over_rows=100000
            ),
        )
    )
    sess._operator_token = "short"
    held = await sess.run_query("DELETE FROM _sim_scratch WHERE id <= 29")

    denied = await sess.approve_query(
        held["approval_id"], operator_token="short", operator="human-1"
    )

    assert denied["ok"] is False
    assert await scratch.fetchval("SELECT count(*) FROM _sim_scratch") == 50


async def test_reads_carry_no_undo_action(make_session):
    sess, _ = await make_session(
        Policy(allowed_tables=frozenset({"film"}), undo=UndoConfig(enabled=True))
    )
    res = await sess.run_query("SELECT film_id FROM film WHERE film_id = 1")
    assert res["undo_action_id"] is None
    assert res["reversible"] is None  # reads never go through the undo path


async def test_intent_mismatch_escalates_to_confirmation_not_block(
    make_session, scratch
):
    # Task implies a single change; the write touches 29 rows -> HIGH mismatch ->
    # held for confirmation (advisory), never hard-blocked.
    sess, _ = await make_session(
        Policy(
            allowed_tables=None,
            simulation=SimulationConfig(
                enabled=True,
                precise=True,
                confirm_over_rows=100000,
                block_over_rows=100000,
            ),
            intent=IntentConfig(enabled=True, single_scope_max=10),
        )
    )
    sql = "UPDATE _sim_scratch SET id = id WHERE id <= 29"
    res = await sess.run_query(sql, stated_task="fix a single test row")
    assert res["blocked"] is False  # intent never hard-blocks
    assert res["requires_confirmation"] is True
    assert res["intent"]["severity"] == "high"
    # held -- nothing ran (no undo action recorded, no rows touched is implicit)
    assert res["status"] is None

    # operator approval lets it through
    ok = await sess.run_query(
        sql, stated_task="fix a single test row", operator_approved=True
    )
    assert ok["requires_confirmation"] is False
    assert ok["status"] == "UPDATE 29"


async def test_intent_llm_second_opinion_runs_out_of_band(
    make_session, scratch, tmp_path
):
    # The optional LLM check must NOT block the query: it's scheduled as a
    # background task that only appends to the audit log.
    calls: list[str] = []

    async def fake_assessor(prompt: str) -> str:
        calls.append(prompt)
        return "looks contradictory"

    pool = await asyncpg.create_pool(dsn=DB_DSN, min_size=1, max_size=2)
    audit = AuditLog(tmp_path / "llm.jsonl")
    await audit.start()
    try:
        policy = Policy(
            allowed_tables=None,
            simulation=SimulationConfig(enabled=False),
            intent=IntentConfig(enabled=True, llm_enabled=True),
        )
        sess = ShadowSession(pool, audit, policy, None, llm_assessor=fake_assessor)
        # A risky write (range, not a point write) with read-only language ->
        # HIGH mismatch -> the LLM second opinion is scheduled out-of-band.
        res = await sess.run_query(
            "UPDATE _sim_scratch SET id = id WHERE id <= 5",
            stated_task="show these rows",
        )
        # run_query returned without waiting on the assessor.
        assert res["blocked"] is False
        # Drain the out-of-band task, then confirm it ran and was logged.
        await asyncio.gather(*sess._bg_tasks)
        assert calls, "assessor should have been invoked out-of-band"
    finally:
        await audit.stop()
        await pool.close()


async def test_intent_llm_skipped_for_routine_point_write(make_session, scratch):
    # QA P1-b: routine point writes must NOT spawn background LLM work.
    calls: list[str] = []

    async def fake_assessor(prompt: str) -> str:
        calls.append(prompt)
        return "ok"

    sess, _ = await make_session(
        Policy(allowed_tables=None, intent=IntentConfig(enabled=True, llm_enabled=True))
    )
    sess._llm_assessor = fake_assessor
    await sess.run_query(
        "UPDATE _sim_scratch SET id = id WHERE id = 1", stated_task="update row 1"
    )
    await asyncio.gather(*sess._bg_tasks)
    assert calls == []  # point write -> no LLM


async def test_non_reversible_reason_is_surfaced(make_session, scratch):
    # QA P2: when a write can't be reverted, the agent gets a machine-readable
    # undo_reason, not just reversible=False.
    sess, _ = await make_session(
        Policy(allowed_tables=None, undo=UndoConfig(enabled=True))
    )
    res = await sess.run_query(
        "UPDATE _sim_scratch t SET id = id FROM (SELECT 1 AS x) s WHERE t.id = 1"
    )
    assert res["blocked"] is True
    assert res["reversible"] is False
    assert res["undo_reason"] is not None
    assert "FROM/USING" in res["undo_reason"]


async def test_audit_status_reports_queue_health(make_session):
    sess, _ = await make_session(Policy(allowed_tables=None))
    status = sess.audit_status()
    assert status["running"] is True
    assert status["dropped"] == 0
    assert status["queue_max"] > 0


async def test_bulk_equality_write_is_simulated_pk_point_write_is_not():
    # QA P0: UPDATE ... WHERE non_unique_col = v is bulk-capable and MUST be
    # simulated; UPDATE ... WHERE pk = v is one row and stays routine.
    try:
        pool = await asyncpg.create_pool(dsn=DB_DSN, min_size=1, max_size=4, timeout=5)
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.skip(f"dev Postgres not reachable at {DB_DSN} ({exc})")
    audit = AuditLog(__import__("tempfile").mktemp())
    await audit.start()
    async with pool.acquire() as c:
        await c.execute("DROP TABLE IF EXISTS bulk_scratch")
        await c.execute("CREATE TABLE bulk_scratch (id serial PRIMARY KEY, grp int)")
        await c.execute(
            "INSERT INTO bulk_scratch (grp) SELECT 1 FROM generate_series(1, 200)"
        )
        unique_columns = await load_unique_columns(c)
    policy = Policy(
        allowed_tables=frozenset({"bulk_scratch"}),
        simulation=SimulationConfig(
            enabled=True, precise=True, confirm_over_rows=10, block_over_rows=100000
        ),
    )
    sess = ShadowSession(pool, audit, policy, None, unique_columns=unique_columns)
    try:
        # bulk: grp is NOT unique -> simulated -> 200 rows > confirm 10 -> held
        bulk = await sess.run_query("UPDATE bulk_scratch SET grp = grp WHERE grp = 1")
        assert bulk["requires_confirmation"] is True
        assert bulk["simulation"]["exact_rows"] == 200
        # point: id IS the PK -> not simulated -> executes
        point = await sess.run_query("UPDATE bulk_scratch SET grp = 2 WHERE id = 1")
        assert point["requires_confirmation"] is False
        assert point["status"] == "UPDATE 1"
        assert point["simulation"] is None
    finally:
        async with pool.acquire() as c:
            await c.execute("DROP TABLE IF EXISTS bulk_scratch")
        await audit.stop()
        await pool.close()
