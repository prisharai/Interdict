"""Day 3 corpus harness: red (should-block) and green (should-allow).

Evaluates the committed corpora against the committed ``policies/pagila.yaml`` --
so this is a regression test on the *real* demo policy, not a hand-built one.

* Every RED query must be blocked, and must surface its expected reason code.
  A red leak (false negative) is the worst failure mode (CLAUDE.md sec. 8 Day 8).
* Every GREEN query must be allowed. A green block (false positive) erodes trust
  and gets the tool disabled (sec. 11).
"""

from pathlib import Path

import pytest
import yaml

from engine.policy import Policy, decide

_ROOT = Path(__file__).resolve().parent.parent
POLICY = Policy.load(_ROOT / "policies" / "pagila.yaml")

RED = yaml.safe_load((_ROOT / "corpus" / "red" / "queries.yaml").read_text())
GREEN = yaml.safe_load((_ROOT / "corpus" / "green" / "queries.yaml").read_text())


@pytest.mark.parametrize("entry", RED, ids=[e["sql"][:40] for e in RED])
def test_red_corpus_is_blocked(entry):
    d = decide(entry["sql"], POLICY)
    assert not d.allowed, f"RED LEAK (false negative): {entry['sql']!r}"
    codes = {v.reason_code for v in d.violations}
    assert (
        entry["expect"] in codes
    ), f"blocked, but not for the expected reason {entry['expect']}: got {codes}"


@pytest.mark.parametrize("entry", GREEN, ids=[e["sql"][:40] for e in GREEN])
def test_green_corpus_is_allowed(entry):
    d = decide(entry["sql"], POLICY)
    assert d.allowed, (
        f"GREEN FALSE POSITIVE: {entry['sql']!r} blocked by "
        f"{[v.reason_code for v in d.violations]}"
    )


def test_corpora_are_nonempty_and_disjoint():
    red_sql = {e["sql"] for e in RED}
    green_sql = {e["sql"] for e in GREEN}
    assert red_sql and green_sql
    assert red_sql.isdisjoint(green_sql)


def test_corpus_metrics(capsys):
    """Compute and report false-negative / false-positive rates (§8 Day 8).

    False negatives (a red query allowed) are the worst failure mode and MUST be
    zero. False positives (a green query blocked) erode trust and must stay low.
    """
    false_neg = [e for e in RED if decide(e["sql"], POLICY).allowed]
    false_pos = [e for e in GREEN if not decide(e["sql"], POLICY).allowed]
    fn_rate = len(false_neg) / len(RED)
    fp_rate = len(false_pos) / len(GREEN)

    report = (
        "\n=== Corpus metrics (policies/pagila.yaml) ===\n"
        f"  RED  (should block): {len(RED)} | false negatives (LEAKS): "
        f"{len(false_neg)} -> FN rate {fn_rate:.1%}\n"
        f"  GREEN (should allow): {len(GREEN)} | false positives: "
        f"{len(false_pos)} -> FP rate {fp_rate:.1%}\n"
    )
    with capsys.disabled():
        print(report)

    # Hard gates: zero red leaks; low green over-blocking.
    assert (
        not false_neg
    ), f"RED LEAKS (false negatives): {[e['sql'] for e in false_neg]}"
    assert (
        fp_rate <= 0.05
    ), f"FP rate {fp_rate:.1%} too high: {[e['sql'] for e in false_pos]}"
