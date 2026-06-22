"""A/B/C run harness (methodology §1, §5).

Runs the *same* workload through each layer, changing nothing but the layer:

* **A direct**    -- raw asyncpg: prepare/fetch + row materialization.
* **B passthrough** -- ShadowSession(policy=None): parse + classify + async audit.
* **C enforcing** -- ShadowSession(default policy): full policy + gated simulate
  + undo + intent.

Same machine, same Postgres, same pool size, same dataset, same query list. Each
cell resets ``bench_writes`` and the undo log first (repeatable, no data drift),
pre-warms the parse cache to steady state, then drives the layer open-loop.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path

import asyncpg

from adapters.mcp_server import ShadowSession
from benchmarks.loadgen import LoadResult, open_loop
from benchmarks.workload import BENCH_ROWS, Query
from engine import classifier, parser
from engine.audit import AuditLog
from engine.policy import Policy
from engine.undo import UndoConfig, UndoStore

DB_DSN = os.environ.get(
    "AGENT_DB_DSN", "postgresql://postgres:postgres@localhost:5433/pagila"
)
POLICY_PATH = str(Path(__file__).resolve().parent.parent / "policies" / "default.yaml")


async def seed_bench_table(conn) -> None:
    """Create the writable bench table once (id PK + a couple of columns)."""
    await conn.execute(
        """CREATE TABLE IF NOT EXISTS bench_writes (
               id bigserial PRIMARY KEY,
               val int NOT NULL DEFAULT 0,
               tag text,
               updated_at timestamptz NOT NULL DEFAULT now()
           )"""
    )


async def reset_cell(conn, undo_schema: str) -> None:
    """Restore a clean, identical starting state before each cell."""
    await conn.execute("TRUNCATE bench_writes RESTART IDENTITY")
    await conn.execute(
        f"INSERT INTO bench_writes (val) SELECT 0 FROM generate_series(1, {BENCH_ROWS})"
    )
    # Keep the undo log bounded so its growing size never skews per-write cost.
    await conn.execute(
        f"DO $$ BEGIN IF to_regclass('{undo_schema}.undo_log') IS NOT NULL "
        f"THEN TRUNCATE {undo_schema}.undo_log; END IF; END $$"
    )


def bench_policy() -> Policy:
    """The shipped default policy, extended to allow the bench write table."""
    p = Policy.load(POLICY_PATH)
    return replace(
        p,
        allowed_tables=(
            (p.allowed_tables | {"bench_writes"})
            if p.allowed_tables is not None
            else None
        ),
    )


@dataclass
class CellResult:
    layer: str
    target_rate: float
    load: LoadResult
    cache_hit_rate: float | None  # None for layer A (no parse cache)


async def _run_a(pool, sql: str) -> None:
    async with pool.acquire() as conn:
        stmt = await conn.prepare(sql)
        [dict(r) for r in await stmt.fetch()]


async def exec_layer(layer, sql, pool, session_b, session_c) -> None:
    if layer == "A":
        await _run_a(pool, sql)
    elif layer == "B":
        await session_b.run_query(sql)
    else:
        await session_c.run_query(sql)


async def run_paired(
    queries: list[Query],
    layers: tuple[str, ...],
    *,
    target_rate: float,
    warmup_s: float,
    pool: asyncpg.Pool,
    session_b: ShadowSession,
    session_c: ShadowSession,
):
    """Drive every layer at ``target_rate`` (PER LAYER) in ONE open-loop timeline.

    ``target_rate`` is the rate *each layer* is driven at: the combined stream
    runs at ``target_rate * len(layers)`` and round-robins, so every layer replays
    the *full* ``queries`` list at the labeled rate (not rate/N). Because all
    layers share one schedule, they see the same scheduler-stall / Docker-jitter
    events, so the per-layer percentile *deltas* cancel that common-mode noise.
    ``latency_by_tag`` holds one histogram per layer.
    """
    nlayers = len(layers)
    if any(lyr in ("B", "C") for lyr in layers):
        for q in queries:
            classifier.classify(q.sql)

    # request j -> layer (j mod N), query (j div N): each layer runs the whole list.
    async def req(j: int) -> None:
        layer = layers[j % nlayers]
        await exec_layer(layer, queries[j // nlayers].sql, pool, session_b, session_c)

    total_n = len(queries) * nlayers
    tags = [layers[j % nlayers] for j in range(total_n)]
    return await open_loop(
        req,
        target_rate=target_rate * nlayers,  # combined; each layer -> target_rate
        n_requests=total_n,
        warmup_s=warmup_s,
        seed=0,
        tags=tags,
    )


async def run_cell(
    layer: str,
    queries: list[Query],
    *,
    target_rate: float,
    warmup_s: float,
    pool: asyncpg.Pool,
    session_b: ShadowSession,
    session_c: ShadowSession,
) -> CellResult:
    """Drive one (layer, rate) cell open-loop and measure it."""
    n = len(queries)

    if layer == "A":

        async def req(i: int) -> None:
            await _run_a(pool, queries[i].sql)

    elif layer == "B":

        async def req(i: int) -> None:
            await session_b.run_query(queries[i].sql)

    elif layer == "C":

        async def req(i: int) -> None:
            await session_c.run_query(queries[i].sql)

    else:
        raise ValueError(layer)

    # Pre-warm the parse/classify cache to steady state (B/C only), then measure
    # the cache hit rate over the timed window via cache_info deltas.
    cache_hit: float | None = None
    if layer in ("B", "C"):
        for q in queries:
            classifier.classify(q.sql)
        base = classifier.classify.cache_info()

    load = await open_loop(
        req,
        target_rate=target_rate,
        n_requests=n,
        warmup_s=warmup_s,
        seed=0,
        tags=[q.category for q in queries],
    )

    if layer in ("B", "C"):
        now = classifier.classify.cache_info()
        hits = now.hits - base.hits
        misses = now.misses - base.misses
        cache_hit = hits / (hits + misses) * 100 if (hits + misses) else None

    return CellResult(layer, target_rate, load, cache_hit)


@dataclass
class HarnessContext:
    pool: asyncpg.Pool
    audit: AuditLog
    session_b: ShadowSession
    session_c: ShadowSession
    undo_schema: str


async def open_context(pool_size: int = 16) -> HarnessContext:
    """Open the shared pool, audit log, and the B/C sessions (one Postgres)."""
    pool = await asyncpg.create_pool(dsn=DB_DSN, min_size=pool_size, max_size=pool_size)
    audit = AuditLog(Path(tempfile.mkdtemp()) / "bench_audit.jsonl")
    await audit.start()
    async with pool.acquire() as conn:
        await seed_bench_table(conn)
    cpolicy = bench_policy()
    undo_store = UndoStore(cpolicy.undo) if cpolicy.undo.enabled else None
    session_b = ShadowSession(pool, audit, None)
    session_c = ShadowSession(pool, audit, cpolicy, undo_store)
    return HarnessContext(
        pool, audit, session_b, session_c, cpolicy.undo.schema or "adb_undo"
    )


async def close_context(ctx: HarnessContext) -> None:
    await ctx.audit.stop()
    await ctx.pool.close()


def clear_engine_caches() -> None:
    parser.cache_clear()
    classifier.cache_clear()


# Re-export for callers that build their own configs.
__all__ = [
    "CellResult",
    "HarnessContext",
    "UndoConfig",
    "bench_policy",
    "clear_engine_caches",
    "close_context",
    "open_context",
    "reset_cell",
    "run_cell",
]
