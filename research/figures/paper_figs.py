"""Publication-grade figures for the paper. Recomputes rates from the run JSONL
so the figures never drift from the data. Two complete models only (Haiku, gpt-5.5).

    uv run python -m research.figures.paper_figs
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib.ticker import PercentFormatter

RUNS = Path(__file__).resolve().parent.parent / "runs"
OUT = Path(__file__).resolve().parent / "paper"
OUT.mkdir(exist_ok=True)

CONDITIONS = ["C0_opaque", "C1_reason_code", "C2_reason_fix", "C3_reason_fix_blast"]
XLABELS = ["Opaque\nerror", "Reason\ncode", "Reason\n+ fix", "+ Measured\nrow count"]
MODELS = {
    "claude-haiku-4-5-20251001": ("Claude Haiku 4.5", "#1f6fb2", "o", "-"),
    "gpt-5.5-2026-04-23": ("GPT-5.5", "#d1495b", "s", "--"),
}


def _wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float, float]:
    if n == 0:
        return float("nan"), float("nan"), float("nan")
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return p, c - h, c + h


def _per_trial(stem: str) -> dict[str, dict[str, dict]]:
    def _trial() -> dict:
        return {"theater": False, "recovered": False}

    per: dict = defaultdict(lambda: defaultdict(_trial))
    for line in (RUNS / f"{stem}.jsonl").read_text().splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        d = per[r["condition"]][r["trial_id"]]
        if r["attempt_label"] == "scope_theater_evasion":
            d["theater"] = True
        if r["attempt_label"] in ("allowed_ontask", "genuine_correction"):
            d["recovered"] = True
    return per


def _rates(stem: str, field: str) -> tuple[list[float], list[float], list[float]]:
    per = _per_trial(stem)
    ps, los, his = [], [], []
    for c in CONDITIONS:
        tt = per[c]
        n = len(tt)
        k = sum(1 for v in tt.values() if v[field])
        p, lo, hi = _wilson(k, n)
        ps.append(p)
        los.append(max(0.0, p - lo))
        his.append(max(0.0, hi - p))
    return ps, los, his


def _base_ax(ylabel: str):
    plt.rcParams.update({"font.size": 11, "font.family": "DejaVu Sans"})
    fig, ax = plt.subplots(figsize=(6.2, 4.0))
    ax.set_ylim(-3, 105)
    ax.yaxis.set_major_formatter(PercentFormatter())
    ax.set_ylabel(ylabel)
    ax.set_xticks(range(len(CONDITIONS)))
    ax.set_xticklabels(XLABELS)
    ax.set_xlabel("Denial feedback (increasing richness →)")
    ax.grid(axis="y", ls=":", alpha=0.5)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    return fig, ax


def _line_figure(field: str, ylabel: str, fname: str) -> None:
    fig, ax = _base_ax(ylabel)
    x = range(len(CONDITIONS))
    for stem, (label, color, marker, ls) in MODELS.items():
        ps, los, his = _rates(stem, field)
        ax.errorbar(
            x,
            [p * 100 for p in ps],
            yerr=[[e * 100 for e in los], [e * 100 for e in his]],
            color=color,
            marker=marker,
            ls=ls,
            lw=2,
            ms=8,
            capsize=4,
            label=label,
        )
    ax.legend(frameon=False, loc="center right")
    fig.tight_layout()
    for ext in ("png", "pdf"):
        fig.savefig(OUT / f"{fname}.{ext}", dpi=300, bbox_inches="tight")
    plt.close(fig)
    print(f"wrote {OUT / fname}.png / .pdf")


def schematic() -> None:
    """Boxes-and-arrows diagram of the closed-loop trial protocol."""
    import matplotlib.patches as mpatches
    from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

    plt.rcParams.update({"font.size": 10.5, "font.family": "DejaVu Sans"})
    fig, ax = plt.subplots(figsize=(9.2, 4.6))
    ax.set_xlim(0, 100)
    ax.set_ylim(0, 100)
    ax.axis("off")

    INK = "#2b2b2b"
    GUARD = "#1f6fb2"
    ALLOW = "#2e8b57"
    STOP = "#d1495b"

    def box(x, y, w, h, text, ec=INK, fc="white", fs=10.5, lw=1.4, bold=False):
        ax.add_patch(
            FancyBboxPatch(
                (x, y),
                w,
                h,
                boxstyle="round,pad=0.6,rounding_size=2.5",
                ec=ec,
                fc=fc,
                lw=lw,
                mutation_scale=1,
            )
        )
        ax.text(
            x + w / 2,
            y + h / 2,
            text,
            ha="center",
            va="center",
            fontsize=fs,
            color=INK,
            fontweight="bold" if bold else "normal",
        )

    def arrow(x1, y1, x2, y2, color=INK, style="-|>", rad=0.0, lw=1.6):
        ax.add_patch(
            FancyArrowPatch(
                (x1, y1),
                (x2, y2),
                arrowstyle=style,
                mutation_scale=14,
                lw=lw,
                color=color,
                connectionstyle=f"arc3,rad={rad}",
            )
        )

    # Agent
    box(2, 60, 17, 16, "Agent", bold=True)

    # Guardrail container with four inner steps
    ax.add_patch(
        FancyBboxPatch(
            (26, 50),
            52,
            36,
            boxstyle="round,pad=0.6,rounding_size=3",
            ec=GUARD,
            fc="#eef5fb",
            lw=1.8,
        )
    )
    ax.text(
        52,
        82,
        "Guardrail",
        ha="center",
        va="center",
        fontsize=11.5,
        color=GUARD,
        fontweight="bold",
    )
    steps = [
        "Parse\n(PostgreSQL\nparser)",
        "Classify\n(AST)",
        "Policy\nrules",
        "Measure impact\nBEGIN;…;ROLLBACK",
    ]
    xs = [28, 40.5, 53, 64.5]
    ws = [11, 11, 10, 12.5]
    for x, w, s in zip(xs, ws, steps, strict=True):
        box(x, 56, w, 18, s, ec=GUARD, fs=8.6)
    for i in range(3):
        arrow(xs[i] + ws[i], 65, xs[i + 1], 65, color=GUARD, lw=1.3)

    # Agent -> Guardrail
    arrow(19, 68, 26, 68)
    ax.text(22.5, 71.5, "SQL", ha="center", fontsize=9, color=INK)

    # Decision outputs on the right
    box(84, 72, 14, 12, "Allow →\nexecute\n(trial ends)", ec=ALLOW, fs=8.4)
    box(84, 30, 14, 14, "Hold / Block\n(refused)", ec=STOP, fs=8.6)
    arrow(78, 68, 84, 78, color=ALLOW, rad=0.2)
    arrow(78, 63, 84, 40, color=STOP, rad=-0.2)

    # Denial message + feedback loop back to the agent
    box(
        30,
        18,
        40,
        14,
        "Denial message\n(4 richness levels: opaque → reason code\n"
        "→ + fix → + measured row count)",
        ec=STOP,
        fc="#fdeef1",
        fs=8.6,
    )
    arrow(84, 34, 70, 25, color=STOP, rad=0.15)  # Hold/Block -> denial
    arrow(30, 25, 10, 60, color=STOP, rad=-0.25)  # denial -> agent (retry)
    ax.text(
        9, 44, "retry (≤ 4 turns)", ha="center", fontsize=9, color=STOP, style="italic"
    )

    fig.tight_layout()
    for ext in ("png", "pdf"):
        fig.savefig(OUT / f"fig3_closed_loop.{ext}", dpi=300, bbox_inches="tight")
    plt.close(fig)
    print(f"wrote {OUT / 'fig3_closed_loop'}.png / .pdf")
    _ = mpatches  # keep import used if refactored


def main() -> None:
    # Figure 1 — the headline contrast.
    _line_figure(
        "theater",
        "Literal rule satisfaction (% of trials)",
        "fig1_literal_rule_satisfaction",
    )
    # Figure 2 — only measured impact corrects the behavior.
    _line_figure(
        "recovered",
        "Correctly scoped recovery (% of trials)",
        "fig2_recovery",
    )
    # Schematic of the closed-loop protocol.
    schematic()


if __name__ == "__main__":
    main()
