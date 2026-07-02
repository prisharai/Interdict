"""Reproducibility check: raw logs <-> generated doc, and run provenance.

Re-derives the evasion-rate-by-cell table straight from the raw JSONL (the source
of truth) and verifies those rates appear in the committed docs/RESULTS_STUDY.md, so a
stale or hand-edited results doc is caught. Also flags runs missing a manifest.

    uv run python -m research.validate_runs    # exit 0 = ok, 1 = mismatch
"""

# ruff: noqa: E501

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RUNS = ROOT / "runs"
EVASIONS = {"scope_theater_evasion", "obfuscation_evasion", "structural_evasion"}


def main() -> int:
    results_path = ROOT / "docs" / "RESULTS_STUDY.md"
    doc = results_path.read_text() if results_path.exists() else ""
    problems: list[str] = []
    for f in sorted(RUNS.glob("claude-*.jsonl")):
        by_trial: dict[str, list[dict]] = defaultdict(list)
        for line in f.read_text().splitlines():
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                problems.append(f"{f.name}: unparseable line")
                continue
            by_trial[r["trial_id"]].append(r)
        # per-condition evasion rate from raw
        cells: dict[str, list[int]] = defaultdict(list)
        for turns in by_trial.values():
            cond = turns[0]["condition"]
            cells[cond].append(int(any(t["attempt_label"] in EVASIONS for t in turns)))
        for cond, vals in cells.items():
            rate = round(100 * sum(vals) / len(vals))
            # the generated doc prints integer percents; check the value is present
            if doc and f"{rate}%" not in doc:
                problems.append(
                    f"{f.stem}/{cond}: raw evasion rate {rate}% not found in docs/RESULTS_STUDY.md"
                )
        if not (RUNS / f"{f.stem}.manifest.json").exists():
            problems.append(
                f"{f.stem}: missing manifest (re-run writes one; legacy runs predate manifests)"
            )

    if problems:
        print("VALIDATION ISSUES:")
        for p in problems:
            print("  -", p)
        return 1
    print("OK: raw logs consistent with docs/RESULTS_STUDY.md; manifests present.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
