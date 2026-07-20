# METRICS.md

A single place collecting every measured number in this project, with how it was
obtained, so results can be cited and reproduced. Numbers are from the dev setup
in the environment block below; re-run the listed command to regenerate any of
them. Where a measurement has an honest caveat, it is stated inline rather than
omitted.

**Environment (all measurements unless noted):** Apple M1 (8 logical cores),
8 GB RAM, macOS; Postgres 16 in Docker Desktop (host port 5433); Python 3.11;
dataset = Pagila + `app_event` (~3M rows) + `metric_sample` (~2M rows). The
end-to-end latency campaign environment is recorded in full in `RESULTS.md`.

---

## 1. Hot-path latency (the load-bearing claim)

The engine's per-request cost on the path an agent waits on: parse (cached) →
classify → policy decision. Budget: **added p99 < 5 ms** on the pass-through path.

| Measurement | p50 | p99 | Notes |
|---|---|---|---|
| `decide()` warm (parse cache hit) | **2.6 µs** | **2.7 µs** | repeated/looping agent queries — effectively free |
| `decide()` cold (first sight: parse+classify+decide) | **166 µs** | **189 µs** | worst case, a never-seen query; ~26× under the 5 ms budget |
| pathological-input rejection (pre-parse guards) | **2 µs** | — | oversized, high-comma, or deeply nested inputs fail closed before pglast |

End-to-end (through the MCP adapter + Postgres round trip), from the paired A/B/C
campaign in `RESULTS.md`:

| Quantity | Value |
|---|---|
| Pass-through overhead (engine vs direct), p50 | ≈ 0 ms (at noise floor) |
| Pass-through overhead, p99 | ≈ 0 ms (within ±0.5 ms noise floor) |
| Latency gate (added p99 < 5 ms) | **PASS** |
| Throughput ratio (achieved/target) | 0.98–1.01 (no coordinated omission) |
| Parse-cache hit rate (realistic mix) | ~78% |
| Request errors over the campaign | 0 |

> Method: open-loop load generator, Poisson arrivals, latency measured from
> intended send time (coordinated-omission-safe), HdrHistogram, **paired** A/B/C
> in one timeline so common-mode noise cancels in the per-layer delta. Full
> methodology, per-rate tables, and validity checks in `RESULTS.md`.
> Reproduce: `python -m benchmarks.run_benchmark`.

---

## 2. Correctness & evasion resistance

| Measurement | Value |
|---|---|
| Red corpus (must block) | **38** statements |
| Green corpus (must allow) | **18** statements |
| **False-negative rate** (red leaks) | **0%** (0 / 38) |
| **False-positive rate** (green over-blocks) | **0%** (0 / 18) |
| Evasion matrix | 7 dangerous bases × 7 disguises (comments/casing/whitespace/quoting) |
| Edge-case fuzz | 200 random byte strings + boundary inputs, **0 crashes** |
| Total automated tests | **294** passing |

> Classification is on the real Postgres AST (`pglast`), never string matching, so
> comments, casing, whitespace, alias stars, whole-row refs, and wrapped writes
> are all normalized before the policy sees them.
> Reproduce: `uv run pytest tests/test_corpus.py -s` (prints the FN/FP report).

---

## 3. Differentiator 1 — blast-radius simulation

Measuring a risky write's true impact *before* deciding, via a time-boxed
`BEGIN; <stmt>; ROLLBACK` that captures the exact affected-row count.

| Statement | Ground truth | Precise (BEGIN/ROLLBACK) | Cheap EXPLAIN estimate |
|---|---|---|---|
| `UPDATE film ... WHERE rental_rate < 3` | 664 rows | **664 (exact)** | 0 (see caveat) |
| `UPDATE app_event ... WHERE customer_id < 100` | 494,317 rows | exact | 0 (see caveat) |

- **Precise-path cost:** ~31 ms median for the 664-row simulation. Paid **only**
  on flagged risky writes, time-boxed by `statement_timeout` + `lock_timeout`,
  never on reads or routine traffic.
- **Honest caveat (a finding in itself):** the cheap EXPLAIN-estimate tier reads
  the top plan node, which for `UPDATE`/`DELETE` reports 0 rows (the statement
  returns no rows without `RETURNING`). This is precisely why risky writes use
  the **precise** path for the actual decision; the estimate tier is not relied
  on for write blast radius.
- **Further caveats:** `BEGIN/ROLLBACK` does not roll back external side effects
  (triggers calling out, already-consumed sequences) and takes locks — hence the
  strict gating and time-boxing.

> Reproduce: see the script under "blast-radius exactness" in the project notes,
> or `uv run pytest tests/test_simulate.py`.

---

## 4. Differentiator 2 — reversibility / instant undo

Every allowed write records a before-image so it can be reverted with one call;
revert is conditional (only if affected rows still match the after-state) and
atomic.

| Measurement | Value |
|---|---|
| Before-image capture overhead (point write, through adapter) | ~4.2 ms median |
| `revert()` round-trip | ~4.1 ms; restored row exactly to original value |
| Revert safety | conflict-checked + atomic; reverts are themselves audited |

- Capture cost is on the **write** path only (never reads), and is configurable.
- **Honest caveat:** not everything is perfectly reversible — external calls,
  cascades, and already-consumed sequences cannot be undone; this is stated in
  the structured `undo_reason` when a write is recorded as non-reversible.

> Reproduce: `uv run pytest tests/test_undo.py`.

---

## 5. Differentiator 3 — intent-mismatch detection (advisory only)

Compares the agent's stated task to the query's measured blast radius and raises
an **advisory** flag on contradiction. Deterministic where possible; an optional
LLM "second opinion" is **async/out-of-band, on the risky subset only, never
blocking, never on the hot path**. No latency is added to any query the agent is
waiting on. (Advisory by design — never a sole gate.)

> Reproduce: `uv run pytest tests/test_intent.py`.

---

## 6. One-line summary for the paper

> A deterministic AST-based safety layer adds **~0 ms p99** to the pass-through
> path (warm hot-path cost **2.6 µs**, cold **166 µs**; both far under a 5 ms
> budget), blocks a 38-statement red corpus with **0% false negatives** and **0%
> false positives** on an 18-statement green corpus, measures risky-write blast
> radius **exactly** (664 vs a planner estimate of 0) before deciding, and makes
> every allowed write reversible with a single conflict-checked, atomic
> `revert`.
