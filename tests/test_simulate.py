"""Blast-radius measurement tests (needs Postgres).

Writes are never executed merely to preview them. UPDATE/DELETE use a safe
counting query; INSERT VALUES is counted from the AST.
"""

import os

import asyncpg
import pytest

from engine.classifier import classify
from engine.simulate import SimulationConfig, is_risky_write, simulate

DB_DSN = os.environ.get(
    "AGENT_DB_DSN",
    "postgresql://postgres:postgres@localhost:5433/pagila",
)


@pytest.fixture
async def conn():
    try:
        c = await asyncpg.connect(dsn=DB_DSN, timeout=5)
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.skip(f"dev Postgres not reachable at {DB_DSN} ({exc})")
    try:
        yield c
    finally:
        await c.close()


ON = SimulationConfig(enabled=True, precise=True)


# --- Gating (pure, no DB) ----------------------------------------------------


def test_risky_write_predicate():
    # The unique/PK columns the adapter loads at startup; a point write is only
    # routine when scoped to one of these.
    uniq = frozenset({"film.film_id", "rental.rental_id"})

    # Risky = a bulk-shaped single write whose blast radius isn't obvious.
    assert is_risky_write(
        classify("UPDATE film SET rental_rate = 1 WHERE rental_rate < 3"), uniq
    )
    assert is_risky_write(classify("DELETE FROM rental WHERE inventory_id > 100"), uniq)
    # data-modifying CTE is routed in (so it fails closed)
    assert is_risky_write(
        classify("WITH d AS (DELETE FROM rental RETURNING *) SELECT * FROM d"), uniq
    )
    # Equality on a NON-unique column is bulk-capable -> still risky (QA P0).
    assert is_risky_write(
        classify("UPDATE app_event SET amount = 0 WHERE customer_id = 1"), uniq
    )
    # NOT risky -- point writes scoped to a known unique/PK column:
    assert not is_risky_write(
        classify("UPDATE film SET rental_rate=1 WHERE film_id = 1"), uniq
    )
    assert not is_risky_write(classify("DELETE FROM rental WHERE rental_id = 5"), uniq)
    assert not is_risky_write(classify("INSERT INTO film (title) VALUES ('x')"), uniq)
    assert not is_risky_write(classify("SELECT * FROM film"), uniq)  # read
    assert not is_risky_write(classify("DROP TABLE film"), uniq)  # ddl
    assert not is_risky_write(classify("UPDATE film SET x=1; SELECT 1"), uniq)  # multi
    # No metadata -> conservative: even a PK point write is simulated.
    assert is_risky_write(classify("UPDATE film SET rental_rate=1 WHERE film_id = 1"))


async def test_disabled_config_skips(conn):
    r = await simulate(
        conn,
        "UPDATE film SET rental_rate = 1 WHERE film_id = 1",
        classify("UPDATE film SET rental_rate = 1 WHERE film_id = 1"),
        SimulationConfig(enabled=False),
    )
    assert r.method == "skipped"


async def test_reads_are_not_simulated(conn):
    sql = "SELECT * FROM film"
    r = await simulate(conn, sql, classify(sql), ON)
    assert r.method == "skipped"


# --- Exact non-mutating counts ------------------------------------------------


async def test_exact_affected_rows_without_executing_delete(conn):
    await conn.execute("DROP TABLE IF EXISTS _sim_del_scratch")
    await conn.execute("CREATE TABLE _sim_del_scratch (id int)")
    await conn.execute("INSERT INTO _sim_del_scratch SELECT generate_series(1, 20)")
    try:
        sql = "DELETE FROM _sim_del_scratch WHERE id <= 9"
        r = await simulate(conn, sql, classify(sql), ON)
        assert r.method == "count"
        assert r.exact_rows == 9  # measured the real blast radius
        assert r.affected_rows == 9
        # The DELETE never ran -- the table is unchanged.
        assert await conn.fetchval("SELECT count(*) FROM _sim_del_scratch") == 20
    finally:
        await conn.execute("DROP TABLE IF EXISTS _sim_del_scratch")


async def test_update_exact_count(conn):
    sql = "UPDATE film SET rental_rate = rental_rate WHERE rental_rate < 3"
    expected = await conn.fetchval("SELECT count(*) FROM film WHERE rental_rate < 3")
    r = await simulate(conn, sql, classify(sql), ON)
    assert r.exact_rows == expected


async def test_measurement_does_not_fire_foreign_key_action(conn):
    sql = "DELETE FROM rental WHERE rental_id < 10"
    r = await simulate(conn, sql, classify(sql), ON)
    assert r.method == "count"
    assert r.exact_rows is not None
    assert await conn.fetchval("SELECT 1") == 1


# --- The legacy precise flag never permits write execution -------------------


async def test_estimate_only_mode(conn):
    sql = "UPDATE film SET rental_rate = 1 WHERE rental_rate < 3"
    r = await simulate(
        conn, sql, classify(sql), SimulationConfig(enabled=True, precise=False)
    )
    assert r.method == "count"
    assert r.exact_rows is not None
    assert r.estimated_rows is not None and r.estimated_cost is not None


async def test_insert_values_counted_statically(conn):
    sql = "INSERT INTO category (name) VALUES ('x'), ('y'), ('z')"
    r = await simulate(conn, sql, classify(sql), ON)
    assert r.method == "static"
    assert r.exact_rows == 3


async def test_insert_select_is_not_guessed(conn):
    sql = "INSERT INTO category (name) SELECT title FROM film"
    r = await simulate(conn, sql, classify(sql), ON)
    assert r.method == "unsupported"
    assert r.affected_rows is None


async def test_preview_does_not_fire_user_trigger(conn):
    await conn.execute("DROP TABLE IF EXISTS _preview_target, _preview_effects")
    await conn.execute("CREATE TABLE _preview_target (id int primary key)")
    await conn.execute("CREATE TABLE _preview_effects (n int)")
    await conn.execute("INSERT INTO _preview_target SELECT generate_series(1, 5)")
    await conn.execute("INSERT INTO _preview_effects VALUES (0)")
    await conn.execute(
        """CREATE FUNCTION _preview_trigger() RETURNS trigger AS $$
        BEGIN UPDATE _preview_effects SET n=n+1; RETURN OLD; END
        $$ LANGUAGE plpgsql"""
    )
    await conn.execute(
        "CREATE TRIGGER _preview_user_trigger BEFORE DELETE ON _preview_target "
        "FOR EACH ROW EXECUTE FUNCTION _preview_trigger()"
    )
    try:
        sql = "DELETE FROM _preview_target WHERE id <= 3"
        result = await simulate(conn, sql, classify(sql), ON)
        assert result.exact_rows == 3
        assert await conn.fetchval("SELECT n FROM _preview_effects") == 0
        assert await conn.fetchval("SELECT count(*) FROM _preview_target") == 5
    finally:
        await conn.execute("DROP TABLE _preview_target, _preview_effects CASCADE")
        await conn.execute("DROP FUNCTION _preview_trigger()")


# --- Time-boxing aborts cleanly ----------------------------------------------


async def test_nested_dml_cte_is_unsupported_not_undercounted(conn):
    # QA P0: a data-modifying CTE's outer command tag ("SELECT N") doesn't reflect
    # the nested write's rows, so we refuse to measure it rather than undercount.
    sql = (
        "WITH u AS (UPDATE film SET rental_rate = rental_rate "
        "WHERE rental_rate < 3 RETURNING 1) SELECT count(*) FROM u"
    )
    r = await simulate(conn, sql, classify(sql), ON)
    assert r.method == "unsupported"
    assert r.exact_rows is None
    assert r.error is not None


async def test_explain_respects_lock_timeout(conn):
    # QA P1b: EXPLAIN runs inside a timeout-scoped tx, so a held lock makes it
    # abort fast instead of hanging. Hold ACCESS EXCLUSIVE on film in another
    # connection, then estimate an update of film.
    blocker = await asyncpg.connect(dsn=DB_DSN, timeout=5)
    tr = blocker.transaction()
    await tr.start()
    try:
        await blocker.execute("LOCK TABLE film IN ACCESS EXCLUSIVE MODE")
        r = await simulate(
            conn,
            "UPDATE film SET rental_rate = rental_rate WHERE rental_rate < 3",
            classify("UPDATE film SET rental_rate = rental_rate WHERE rental_rate < 3"),
            SimulationConfig(
                enabled=True,
                precise=False,
                statement_timeout_ms=5000,
                lock_timeout_ms=200,
            ),
        )
        assert r.timed_out is True  # aborted on the lock, did not hang
    finally:
        await tr.rollback()
        await blocker.close()


async def test_statement_timeout_aborts_cleanly_and_rolls_back(conn):
    # A large write with a 10 ms cap must time out, report it, and leave the
    # table untouched -- never a partial commit.
    sql = "UPDATE app_event SET amount = amount WHERE customer_id < 600"
    before = await conn.fetchval("SELECT count(*) FROM app_event")
    r = await simulate(
        conn,
        sql,
        classify(sql),
        SimulationConfig(enabled=True, precise=True, statement_timeout_ms=10),
    )
    assert r.timed_out is True
    assert r.exact_rows is None
    after = await conn.fetchval("SELECT count(*) FROM app_event")
    assert after == before  # rolled back despite the timeout
    # The connection is still usable after a clean abort.
    assert await conn.fetchval("SELECT 1") == 1
