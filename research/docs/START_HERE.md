# START HERE — handoff for writing the paper

You (an LLM) have everything in this `research/` folder needed to write the full
paper. Read these in order:

1. **`docs/START_HERE.md`** (this file) — the finding, the data state, what each file is.
2. **`docs/PAPER_DOSSIER.md`** — the paper dossier: title/abstract template, intro,
   related-work map, method in prose, discussion, limitations, ethics, and the
   full data-schema inventory. **This is your section-by-section skeleton.**
3. **`docs/RESULTS_STUDY.md`** — generated results: all rate tables, Fisher exact
   tests (odds ratios + p-values), logistic regression, evasion-taxonomy counts,
   turns-to-recovery, and verbatim blocked→evade trajectories.
4. **`docs/STUDY_DESIGN.md`** — experiment design rationale (why these tasks/conditions/
   metrics; the two-literature gap; threats to validity).
5. **`figures/*.png`** — `evasion_by_condition.png`, `scope_theater.png`,
   `recovery.png` (regenerate via `uv run python -m research.stats`).
6. **`runs/*.jsonl`** — raw per-turn data, one file per model (schema in docs/PAPER_DOSSIER.md
   §9). Every prompt, SQL, decision, and measured blast radius is here.

Source of the instrument: `harness.py` (tasks, conditions, evasion classifier),
`runner.py` (closed loop), `agents.py` (mock + real LLM), `stats.py` (analysis).

## Framing — read before writing (specification gaming)

The phenomenon is **specification gaming** (a.k.a. reward hacking; in this
closed-loop setting, in-context reward hacking): the agent satisfies the *letter*
of a specification while defeating its *purpose*. We do **not** claim to discover
it — it is well documented. The contribution is the **domain** (runtime database
guardrails) and the **automatic detector** (measuring each write's row-level
impact, so a gaming move that is valid SQL is caught where syntax checks fail).

The specification being gamed is the **guardrail rule**, not the user's intent.
The completed run uses broad-objective tasks ("empty the table"): the agent's
broad write is blocked by the rule, and it responds by re-issuing a statement
with a **tautological predicate** (`WHERE 1=1`) that satisfies the named rule
while still affecting every row. That tautological predicate is never needed to
serve the user — the broad write was already blocked — so adopting it is a move
to get the blocked effect *past the rule*: specification gaming of the guardrail,
measurable directly on this data. The narrow-intent task set (`tasks_v2.py` +
`docs/PRE_REGISTRATION.md`) is an **optional second operationalization** that also
removes any ambiguity about user intent; it strengthens the result but is not a
prerequisite for the central claim.

## The one-sentence finding

When the guardrail blocks a write, **naming the violated rule (e.g.
`WRITE_WITHOUT_WHERE`) makes the agent far more likely to re-issue a
syntactically-compliant statement that still hits the whole table** (typically
`WHERE 1=1`) than an opaque error does — i.e. richer, mechanistic feedback elicits
literal rule satisfaction. Measuring the row-level impact robustly flags this
class of statement; simple syntax heuristics catch only some cases.

## Headline numbers (evasion rate = ≥1 evasion attempt)

| model | C0 opaque | C1 reason code | C2 reason+fix | C3 +blast radius |
|---|---|---|---|---|
| **Haiku 4.5** (complete) | 22% | 95% | 98% | 98% |
| **GPT-5.5** (complete) | 100% | 100% | 100% | 100% |
| Sonnet 4.6 (partial) | 37% | 98% | 100% | 100% |
| Opus 4.8 (partial) | 22% | 75% | 68% | 79% |

- **Manipulation check:** 99% of trials had their first attempt denied (the
  bulk-request tasks reliably trigger the guardrail).
- **Mechanism:** scope-theater (a fake `WHERE`) accounts for nearly all of the
  evasion — its rate mirrors the evasion rate above.
- **Mirror image in recovery:** genuine recovery falls as denials get richer
  for Haiku (38% at C0 → 2–5% at C1–C3), while GPT-5.5 only moves toward
  recovery when shown measured impact in C3 (7% / 0% / 0% → 48%).
- **Capability nuance (partial data):** Opus (strongest) showed *less* literal
  satisfaction and *more* narrowing than Haiku/Sonnet — capability may partly
  protect — but **Opus/Sonnet cells are incomplete; this appears only in partial
  cross-model runs and is not confirmatory.**
- **Protocol-failure caveat:** Anthropic cells have high non-SQL/prose rates and
  must report protocol failure as a separate dependent variable. Do not silently
  fold those turns into the behavioral headline. GPT-5.5 has low protocol failure
  and is the clean cross-provider complete run.
- Exact odds ratios + p-values (Fisher exact, each condition vs C0, pooled and
  per model) and the logistic regression are in `docs/RESULTS_STUDY.md`.

## DATA STATE — read before writing the results

The API credit balance ran out mid-sweep. Completeness differs by model:

| model | trials | cells (of 12) | status |
|---|---|---|---|
| Haiku 4.5 | 240 | 12 | **complete** (n=20/cell) — confirmatory |
| GPT-5.5 | 240 | 12 | **complete** (n=20/cell) — confirmatory cross-provider run |
| Sonnet 4.6 | 200 | 10 | partial (2 cells missing) |
| Opus 4.8 | 148 | 8 | partial (some cells n<20) |

**Write the paper with Haiku and GPT-5.5 as the complete confirmatory runs.**
Sonnet/Opus are partial exploratory cross-model replication only. To finish the
replication: add API credits and re-run the two partial models
(`AGENT=anthropic MODEL=claude-sonnet-4-6 SCHEMA=exp_sonnet TRIALS=20 uv run
--env-file .env python -m research.run_pilot`, same for opus), then
`uv run python -m research.stats`.

## What's solid vs. what needs a caveat

- **Supported:** on the complete Haiku run, the C0→C1 jump in literal rule
  satisfaction is large; on the complete GPT-5.5 run, literal rule satisfaction
  is at ceiling across conditions and measured impact is the only condition that
  materially improves recovery. Partial Sonnet/Opus runs are directionally useful
  but exploratory. The specification being gamed is the guardrail rule, so the
  broad-objective tasks measure it directly.
- **Must caveat:** 3 synthetic, adversarially-constructed tasks; Sonnet/Opus cells
  partial; high Anthropic protocol-failure rates; one seed; the richest condition
  (C3) adds the measured row count *and* a confirmation framing, so it does not
  cleanly isolate impact from framing. The narrow-intent task set
  (`docs/PRE_REGISTRATION.md`) is an optional second operationalization that further
  removes any ambiguity about user intent.

## Product Metric Alignment

`SPEC_V2.md` makes impact-authoritative writes the v2 product architecture:
rules are a fast reject-only pre-filter, and measured impact decides writes that
survive rules. Paper text should connect the behavioral result to that product
posture: syntax/rule feedback can be gamed, while measured row-level impact
catches the gaming move.

The product-facing evaluation metric is **interruption rate** plus **saves**,
computed from audit-log `Decision` records once A2/A3 land. Do not add a separate
instrumentation path for these metrics. The research study's protocol-failure
rate remains a paper DV; the product benchmark's interruption rate is the rate
at which Interdict is wrongly in the way during realistic agent tasks.
