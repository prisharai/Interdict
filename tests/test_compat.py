"""Day 8 protocol/compatibility: a real client and a real ORM through the server.

Proves real-world SQL flows through the safety layer unchanged:

* **Real ORM (SQLAlchemy):** queries are built with SQLAlchemy Core and compiled
  to literal SQL (the shapes an ORM emits -- qualified columns, joins, newlines),
  then run through ``ShadowSession`` and return correct results.
* **Real client (asyncpg):** the same read through the engine returns byte-for-byte
  the same rows as a direct asyncpg query -- the layer is transparent for allowed
  traffic.

Needs Postgres; skips cleanly when it's down.
"""

import os
from dataclasses import replace

import asyncpg
import pytest
from sqlalchemy import (
    Column,
    Integer,
    MetaData,
    Numeric,
    Table,
    Text,
    func,
    insert,
    select,
    update,
)
from sqlalchemy.dialects import postgresql

from adapters.mcp_server import ShadowSession
from engine.audit import AuditLog
from engine.policy import Policy
from engine.undo import UndoStore

DB_DSN = os.environ.get(
    "AGENT_DB_DSN", "postgresql://postgres:postgres@localhost:5433/pagila"
)
_ROOT_POLICY = "policies/pagila.yaml"

# --- A small SQLAlchemy schema mirroring the tables we touch ------------------
_md = MetaData()
film = Table(
    "film",
    _md,
    Column("film_id", Integer, primary_key=True),
    Column("title", Text),
    Column("rental_rate", Numeric),
)
customer = Table(
    "customer",
    _md,
    Column("customer_id", Integer, primary_key=True),
    Column("first_name", Text),
    Column("last_name", Text),
)
rental = Table(
    "rental",
    _md,
    Column("rental_id", Integer, primary_key=True),
    Column("customer_id", Integer),
)
compat_scratch = Table(
    "compat_scratch",
    _md,
    Column("id", Integer, primary_key=True),
    Column("name", Text),
    Column("val", Numeric),
)


def orm_sql(stmt) -> str:
    """Compile a SQLAlchemy statement to a literal SQL string (what an ORM emits)."""
    return str(
        stmt.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )


@pytest.fixture
async def session():
    try:
        pool = await asyncpg.create_pool(dsn=DB_DSN, min_size=1, max_size=4, timeout=5)
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.skip(f"dev Postgres not reachable at {DB_DSN} ({exc})")
    # Default policy, extended to allow the write scratch table.
    p = Policy.load(_ROOT_POLICY)
    p = replace(p, allowed_tables=p.allowed_tables | {"compat_scratch"})
    import tempfile
    from pathlib import Path

    audit = AuditLog(Path(tempfile.mkdtemp()) / "compat.jsonl")
    await audit.start()
    store = UndoStore(p.undo)
    async with pool.acquire() as c:
        await c.execute("DROP TABLE IF EXISTS compat_scratch")
        await c.execute(
            "CREATE TABLE compat_scratch (id int primary key, name text, val numeric)"
        )
        await c.execute("INSERT INTO compat_scratch VALUES (1,'a',1.0),(2,'b',2.0)")
    try:
        yield ShadowSession(pool, audit, p, store), pool
    finally:
        async with pool.acquire() as c:
            await c.execute("DROP TABLE IF EXISTS compat_scratch")
        await audit.stop()
        await pool.close()


# --- Real ORM: reads --------------------------------------------------------


async def test_orm_point_select(session):
    sess, _ = session
    sql = orm_sql(select(film.c.film_id, film.c.title).where(film.c.film_id == 1))
    res = await sess.run_query(sql)
    assert res["blocked"] is False
    assert res["row_count"] == 1
    assert res["rows"][0]["film_id"] == 1


async def test_orm_join(session):
    sess, _ = session
    j = customer.join(rental, customer.c.customer_id == rental.c.customer_id)
    sql = orm_sql(
        select(customer.c.first_name, rental.c.rental_id)
        .select_from(j)
        .where(customer.c.customer_id == 1)
        .limit(5)
    )
    res = await sess.run_query(sql)
    assert res["blocked"] is False
    assert res["row_count"] >= 1


async def test_orm_aggregate(session):
    sess, _ = session
    sql = orm_sql(select(func.count()).select_from(film))
    res = await sess.run_query(sql)
    assert res["blocked"] is False
    assert res["rows"][0]["count_1"] == 1000  # Pagila has 1000 films


# --- Real ORM: writes flow through (and stay reversible) --------------------


async def test_orm_insert_and_update_execute(session):
    sess, pool = session
    ins = orm_sql(insert(compat_scratch).values(id=3, name="c", val=3.0))
    res = await sess.run_query(ins)
    assert res["blocked"] is False and res["reversible"] is True
    assert await pool.fetchval("SELECT count(*) FROM compat_scratch") == 3

    upd = orm_sql(
        update(compat_scratch).where(compat_scratch.c.id == 3).values(val=9.0)
    )
    res2 = await sess.run_query(upd)
    assert res2["blocked"] is False
    assert await pool.fetchval("SELECT val FROM compat_scratch WHERE id=3") == 9


# --- Real client: the layer is transparent for allowed reads ----------------


async def test_engine_is_transparent_vs_direct_asyncpg(session):
    sess, pool = session
    sql = "SELECT film_id, title FROM film WHERE film_id = 42"
    direct = [dict(r) for r in await pool.fetch(sql)]
    through = (await sess.run_query(sql))["rows"]
    assert through == direct  # identical rows through the engine vs direct
