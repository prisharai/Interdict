"""Development-database smoke test.

Confirms the dev stack is actually usable: we can reach the Dockerized Postgres
and the seed completed in full (Pagila + the two large generated tables). This
deliberately checks real row counts rather than ``assert True`` -- this catches
a half-seeded volume that *looks* healthy but is missing the large tables.

If the database isn't running (e.g. CI without Docker, or before
``docker compose up``), the test SKIPS rather than fails, so the suite stays
green wherever there's no DB to talk to.
"""

import os

import asyncpg
import pytest

# Connection defaults match docker-compose.yml (host port 5433 -> container
# 5432). Overridable via env so CI / alternate setups don't have to edit code.
DB_DSN = os.environ.get(
    "AGENT_DB_DSN",
    "postgresql://postgres:postgres@localhost:5433/pagila",
)

# Expected seed contents. Pagila's canonical sizes plus the large tables from
# db/03-large-tables.sql.
EXPECTED_COUNTS = {
    "film": 1_000,
    "customer": 599,
    "app_event": 3_000_000,
    "metric_sample": 2_000_000,
}


async def _connect():
    """Connect, or skip the test if the dev DB isn't reachable."""
    try:
        return await asyncpg.connect(dsn=DB_DSN, timeout=5)
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.skip(
            f"dev Postgres not reachable at {DB_DSN} ({exc}); "
            "run `docker compose up -d` to enable this test"
        )


async def test_seed_is_complete():
    """Every seeded table exists with its expected row count."""
    conn = await _connect()
    try:
        for table, expected in EXPECTED_COUNTS.items():
            count = await conn.fetchval(f"SELECT count(*) FROM {table}")
            assert count == expected, (
                f"{table}: expected {expected} rows, found {count} -- seed is "
                "incomplete; re-seed with `docker compose down -v && "
                "docker compose up -d`"
            )
    finally:
        await conn.close()
