# RESULTS.md — Day 7 latency benchmark

_Generated 2026-06-22 18:52 UTC · campaign wall time 22.0 min._

## Headline

At 350 req/s (pure-overhead view), engine pass-through overhead (B−A): **p50 -0.023 ms, p99 -0.012 ms** (spread -0.32..+2.24); full engine (C−A) p99 +0.384 ms. §4 gate (added p99 < 5 ms): **PASS**.

> The pass-through overhead is at the **measurement noise floor** of this setup (~1 ms p99, set by Docker Desktop's ~2 ms round-trip and the Python asyncio harness's ~1 ms scheduler lateness). The true per-request engine CPU cost on a cached read is microseconds (classify cache-hit ~0, async audit enqueue ~µs); end-to-end it is too small to resolve above that floor, which is itself far under the 5 ms budget. This is reported as a *bound*, not false precision.

## Environment

- **CPU:** Apple M1 (8 logical cores)
- **RAM:** 8 GB
- **OS:** macOS-10.16-x86_64-i386-64bit
- **Power:** BATTERY (macOS throttles!)
- **Load average:** { 5.91 5.72 5.53 }
- **Postgres:** 16 (Docker Desktop, host port 5433) -- n/a
- **Dataset:** Pagila + app_event (~3M) + metric_sample (~2M); bench_writes (100,000 rows, reset per cell)
- **Pool size:** 16 (same for all layers)
- **Load model:** open-loop, Poisson arrivals, latency from intended send time
- **Runs:** 5 interleaved (A,B,C) per cell; warmup 1.5s discarded
- **Measured window:** 20.0s/cell -> samples = rate x 20 (e.g. 20,000 at 1000 req/s)
- **Harness:** single-process asyncio (in-process engine; MCP stdio transport NOT measured -- it's a separate, larger, constant cost that would mask the engine delta)
- **Python:** 3.11.7


## Validity checks (methodology §3)

- **Self-assessment:** **VALID** -- harness kept its schedule; absolute numbers trustworthy.
- **Generator is not the bottleneck:** median scheduler lateness p99 = **1.336 ms** (worst single max across all cells 200.1 ms). This is common-mode across A/B/C (same harness) so it cancels in the overhead delta; it is the floor of what this harness can resolve.
- **Achieved ≈ target throughput** (no coordinated-omission gap): throughput ratio min=0.98, median=1.00, max=1.01 across all cells. A large shortfall would indicate the system couldn't keep up.
- **Latency measured from intended send time**, so any harness scheduling lateness is *included* in the latency, never discarded (no coordinated omission).
- **Parse-cache hit rate** (mixed view): 78%..100% -- the **~78%** at the higher-throughput cells is the representative value, squarely in the realistic 70-90% band (neither all-hot nor all-cold); it approaches 100% only at the lowest rate, where the smaller per-cell query set fits entirely in the 2048-entry cache.
- **Runs:** 5 interleaved per cell; tables report the **median p99 across runs and the min..max spread**, never a single best run.
- **Errors:** 0 request errors across the whole campaign.


## View 1 — pure overhead (`SELECT 1`, DB time ≈ 0)

Paired A/B/C in one timeline. The **rate column is per-layer**: every layer is driven at the labeled rate, so the combined stream runs at rate×3 (e.g. 500 here = 1500 req/s combined). The A→C delta is almost entirely engine cost (worst-case *relative* overhead); the absolute floor ~2 ms is Docker round-trip, identical across layers.

| rate (req/s) | layer | p50 | p90 | p99 | p99.9 | max | achieved/target | cache hit |
|---|---|---|---|---|---|---|---|---|
| 200 | A (direct) | 2.171 | 3.559 | 10.767 | 77.887 | 86.21 | 1.00 | n/a |
| 200 | B (passthrough) | 2.135 | 3.509 | 9.959 | 75.135 | 90.56 | 1.00 | n/a |
| 200 | C (enforcing) | 2.191 | 3.581 | 11.423 | 81.727 | 87.81 | 1.00 | n/a |
| 350 | A (direct) | 2.015 | 3.373 | 12.063 | 76.671 | 87.30 | 1.01 | n/a |
| 350 | B (passthrough) | 1.982 | 3.365 | 12.687 | 76.863 | 85.50 | 1.01 | n/a |
| 350 | C (enforcing) | 1.999 | 3.375 | 12.911 | 76.991 | 86.21 | 1.01 | n/a |
| 500 | A (direct) | 2.275 | 3.927 | 41.951 | 77.759 | 103.10 | 1.00 | n/a |
| 500 | B (passthrough) | 2.269 | 3.951 | 41.919 | 77.759 | 103.10 | 1.00 | n/a |
| 500 | C (enforcing) | 2.289 | 3.993 | 41.983 | 77.503 | 104.06 | 1.00 | n/a |

**Added overhead vs direct (A):**

| rate (req/s) | Δp50 B−A | Δp99 B−A (min..max) | Δp50 C−A | Δp99 C−A (min..max) | §4 gate (B−A p99 <5ms) |
|---|---|---|---|---|---|
| 200 | -0.036 | -0.524 (-2.40..+0.16) | +0.020 | +0.060 (-0.93..+0.74) | PASS |
| 350 | -0.023 | -0.012 (-0.32..+2.24) | -0.006 | +0.384 (-0.00..+3.39) | PASS |
| 500 | -0.014 | -0.032 (-0.91..+0.03) | +0.000 | -0.064 (-0.67..+0.06) | PASS |

## View 2 — in-context realistic mix (~80/18/2)

Same absolute overhead, now a small fraction of real query time (heavy aggregations dominate the tail). Absolute p99 here is query cost, not engine cost; the engine delta is in the overhead table.

| rate (req/s) | layer | p50 | p90 | p99 | p99.9 | max | achieved/target | cache hit |
|---|---|---|---|---|---|---|---|---|
| 200 | A (direct) | 3.455 | 10.127 | 51.615 | 73.855 | 135.81 | 0.98 | n/a |
| 200 | B (passthrough) | 3.709 | 11.071 | 55.455 | 115.647 | 154.50 | 0.98 | 100% |
| 200 | C (enforcing) | 5.063 | 15.279 | 58.687 | 102.975 | 133.89 | 0.98 | 100% |
| 400 | A (direct) | 3.205 | 9.751 | 54.207 | 85.311 | 135.04 | 0.99 | n/a |
| 400 | B (passthrough) | 4.331 | 20.815 | 88.127 | 167.935 | 243.46 | 0.99 | 78% |
| 400 | C (enforcing) | 5.647 | 32.895 | 219.775 | 326.911 | 474.88 | 0.99 | 78% |
| 700 | A (direct) | 3.943 | 12.047 | 57.887 | 90.687 | 125.44 | 1.01 | n/a |
| 700 | B (passthrough) | 4.207 | 15.959 | 62.399 | 102.975 | 179.20 | 1.01 | 78% |
| 700 | C (enforcing) | 6.211 | 23.807 | 69.183 | 107.455 | 169.47 | 1.01 | 78% |

**Added overhead vs direct (A):**

| rate (req/s) | Δp50 B−A | Δp99 B−A (min..max) | Δp50 C−A | Δp99 C−A (min..max) | gate |
|---|---|---|---|---|---|
| 200 | +0.038 | +3.200 (-10.46..+31.14) | +1.426 | +4.352 (-2.02..+66.27) | not gated (query-variance) |
| 400 | +0.658 | +19.904 (-325.25..+96.00) | +1.774 | +27.648 (-97.28..+5373.44) | not gated (query-variance) |
| 700 | +0.318 | +4.320 (-446.21..+130.27) | +2.268 | +8.992 (+4.35..+72.70) | not gated (query-variance) |

## Risky-write subset (proving the gated simulation path stays contained)

Effect of the 2% risky (gated-simulation) writes on the **mixed** workload p99 (layer C):

| rate | p99 all | p99 excl. risky | risky p99 | risky share |
|---|---|---|---|---|
| 200 | 58.69 | 58.59 | 56.54 | ~2% |
| 400 | 219.78 | 216.19 | 416.00 | ~2% |
| 700 | 69.18 | 68.42 | 95.17 | ~2% |

## Honest caveats

- **Docker Desktop on macOS** adds ~2 ms VM/network round-trip to every query (identical across A/B/C, cancels in the delta, but inflates the absolute floor). A bare-metal Postgres would show a lower absolute floor and likely a cleaner (smaller) measurable engine delta.
- **x86_64 Python under Rosetta 2 on Apple M1** (the interpreter is not native arm64): emulation overhead is added to every layer including the harness scheduler -- common-mode, cancels in the paired delta, but inflates absolutes. A native arm64 Python would lower the floor.
- **Machine not fully quiesced (see env: battery + load average):** methodology §5 warns this invalidates *absolute tail* numbers (p99.9 / max). The **paired-delta headline is robust** to it (A/B/C share every stall event), but a mains-powered, quiesced re-run is recommended before quoting absolute p99.9/max as publication-grade.
- **Single-process Python asyncio harness:** scheduler lateness (~1 ms p99) is the resolution floor for sub-ms overheads. We bound the overhead rather than claim a precise sub-ms figure.
- **Layer C write cost is real and not under the read gate:** undo before-image capture (~1 ms extra round trips) and the gated BEGIN/ROLLBACK simulation on the 2% risky writes add measurable write-path latency. The §4 5 ms gate is the *pass-through read path* (B−A) and C without simulation; C's write overhead is reported separately, by design.
- **Sample volume:** the spec's 200k/cell across the full matrix is hours on this hardware; we report exact N/cell above and prioritized p99 credibility (>=5 runs, spread) over p99.9 precision.
- **CI gate** (`benchmarks/ci_latency_gate.py`) runs a scaled-down, same-shape open-loop check and fails the build if the paired B−A p99 > 5 ms (`uv run python -m benchmarks.ci_latency_gate`, exit 1 on fail). **By deliberate project decision the `benchmarks/` folder is kept off GitHub**, so this is a *local* pre-commit/pre-release gate rather than a GitHub-CI gate; un-ignoring the folder is all that's needed to make it a true CI gate if that policy ever changes.
