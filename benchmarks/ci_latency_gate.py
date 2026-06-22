"""CI latency gate (methodology §8 / the §4 latency budget).

A scaled-down, open-loop version of the benchmark that fails the build if the
added p99 on the pass-through path (B - A) exceeds 5 ms. Robust to single-run
noise: median B-A p99 across a few short interleaved runs.

The pass/fail is computed on the **pure-overhead view** (``SELECT 1``), because
that is where pass-through engine cost is actually measurable on this hardware:
in the realistic mix the heavy-aggregation query variance (tens of ms) swamps the
sub-ms engine delta, making B-A p99 noise (we still run a mix cell for shape and
print it, but don't gate on it). Open-loop is preserved in both.

Exit code 0 = pass, 1 = fail, 2 = skipped (no database).
Run:  uv run python -m benchmarks.ci_latency_gate
"""

from __future__ import annotations

import asyncio
import statistics
import sys

import asyncpg

from benchmarks import harness
from benchmarks.workload import Query

GATE_MS = (
    5.0  # the §4 latency budget: added p99 on the pass-through path must be < 5 ms
)
RATE = 500  # within the harness's valid (non-saturating) range
MEASURE_S = 5.0
WARMUP_S = 1.0
RUNS = 3


def _p99_ms(load) -> float:
    return load.latency_us.get_value_at_percentile(99) / 1000.0


def _tag_p99(load, tag) -> float:
    return load.latency_by_tag[tag].get_value_at_percentile(99) / 1000.0


async def _gate() -> int:
    try:
        ctx = await harness.open_context(pool_size=16)
    except (OSError, asyncpg.PostgresError) as exc:
        print(f"SKIP: dev Postgres not reachable ({exc})")
        return 2

    n = int(RATE * (WARMUP_S + MEASURE_S))
    pure_deltas: list[float] = []
    try:
        for run in range(RUNS):
            # Paired A/B in one timeline -> clean, common-mode-cancelled delta.
            queries = [Query("read", "SELECT 1") for _ in range(n)]
            async with ctx.pool.acquire() as conn:
                await harness.reset_cell(conn, ctx.undo_schema)
            harness.clear_engine_caches()
            load = await harness.run_paired(
                queries,
                ("A", "B"),
                target_rate=RATE,
                warmup_s=WARMUP_S,
                pool=ctx.pool,
                session_b=ctx.session_b,
                session_c=ctx.session_c,
            )
            d = _tag_p99(load, "B") - _tag_p99(load, "A")
            pure_deltas.append(d)
            print(
                f"  run {run+1}/{RUNS}: A p99={_tag_p99(load,'A'):.2f}ms "
                f"B p99={_tag_p99(load,'B'):.2f}ms -> B-A={d:+.2f}ms",
                flush=True,
            )
    finally:
        await harness.close_context(ctx)

    median_delta = statistics.median(pure_deltas)
    passed = median_delta < GATE_MS
    print(
        f"\npass-through overhead (paired, median B-A p99 over {RUNS} runs): "
        f"{median_delta:+.2f} ms  [gate < {GATE_MS} ms]  "
        f"{'PASS' if passed else 'FAIL'}"
    )
    return 0 if passed else 1


def main() -> int:
    return asyncio.run(_gate())


if __name__ == "__main__":
    sys.exit(main())
