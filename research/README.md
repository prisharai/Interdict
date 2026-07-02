# Interdict Research + Paper

This folder is the single home for the study, paper, data, figures, and research
helpers.

## Read First

1. `docs/START_HERE.md` - paper handoff, headline result, data state.
2. `docs/PAPER_DOSSIER.md` - section-by-section writing kit.
3. `docs/RESULTS_STUDY.md` - generated tables, tests, and trajectories.
4. `docs/STUDY_DESIGN.md` - study rationale, hypotheses, and threats.
5. `paper/main.tex` - current paper draft.

## Layout

| Path | Purpose |
|---|---|
| `agents.py`, `harness.py`, `runner.py`, `run_pilot.py` | Closed-loop study runner. |
| `stats.py` | Regenerates `docs/RESULTS_STUDY.md` and `figures/*.png`. |
| `validate_runs.py` | Checks raw runs against the generated results doc. |
| `tasks_v2.py` | Narrow-intent task set for the next study slice. |
| `runs/` | Raw model logs and manifests. |
| `figures/` | Generated figures used by docs and paper. |
| `paper/` | Current LaTeX paper source and archived old draft. |
| `schemas/` | JSON schemas for run metadata. |
| `validation/` | Human audit sheets and keys. |

## Common Commands

```bash
docker compose up -d
uv run python -m research.run_pilot
uv run python -m research.stats
uv run python -m research.validate_runs
uv run python -m research.figures.paper_figs
```

Real LLM runs need provider credentials, for example:

```bash
AGENT=anthropic uv run python -m research.run_pilot
```
