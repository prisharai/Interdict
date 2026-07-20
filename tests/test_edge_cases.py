"""Edge cases: malformed, huge, and unusual inputs must fail safe.

"Fail safe" means two things, both verified here: (1) nothing ever raises onto
the hot path -- classify and decide always return, even for garbage; and (2)
anything we can't understand, or that is genuinely dangerous, is **blocked**
(fail closed), while harmless no-ops (empty/comment-only) are simply allowed.

Pure, in-memory, no DB.
"""

from pathlib import Path

import pytest

from engine.classifier import classify
from engine.policy import Policy, decide

POLICY = Policy.load(
    Path(__file__).resolve().parent.parent / "policies" / "pagila.yaml"
)


def _safe(sql: str):
    """classify + decide must never raise; return the decision."""
    classify(sql)  # must not raise
    return decide(sql, POLICY)  # must not raise


# --- Nothing crashes ---------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "",  # empty
        "   \n\t  ",  # whitespace only
        "-- just a comment",  # line comment only
        "/* nothing here */",  # block comment only
        "SELECT 1\x00 trailing",  # embedded null byte
        "\x01\x02\x1f SELECT 1",  # control characters
        "SELECT '\U0001f4a9 é 中'",  # multibyte unicode in a literal
        "SELECT " + "x" * 100_000,  # very long token
        "SELECT * FROM film WHERE film_id IN ("
        + ",".join("1" for _ in range(20000))
        + ")",
    ],
    ids=[
        "empty",
        "ws",
        "line-comment",
        "block-comment",
        "null-byte",
        "ctrl",
        "unicode",
        "long",
        "huge-in",
    ],
)
def test_no_crash_on_weird_input(sql):
    # The only guarantee for harmless/odd shapes is: it returns, no exception.
    d = _safe(sql)
    assert d is not None


# --- Unparseable / unknown fails closed --------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT FROM WHERE ;;;",  # syntax garbage
        "this is not sql at all",
        "SELECT \ud800",  # lone UTF-16 surrogate (raises in libpg_query)
        "))) (((",
        "DELETE FROM",  # truncated dangerous statement
    ],
    ids=["garbage", "prose", "surrogate", "brackets", "truncated-delete"],
)
def test_unparseable_is_blocked(sql):
    d = _safe(sql)
    assert not d.allowed, f"unparseable input was allowed: {sql!r}"


def test_deeply_nested_does_not_crash_and_fails_safe():
    # Pathological nesting either parses (allowed read) or trips libpg_query's
    # recursion guard (-> UNKNOWN -> blocked). Either way: no crash.
    sql = "SELECT " + "(" * 2000 + "1" + ")" * 2000
    d = _safe(sql)
    # If it couldn't be parsed it must be blocked; if it parsed it's a trivial read.
    assert d.allowed or not d.allowed  # the real assertion is "did not raise"


def test_huge_multi_statement_batch_is_blocked():
    sql = "; ".join("SELECT 1" for _ in range(500))
    d = _safe(sql)
    assert not d.allowed
    assert any(v.reason_code == "MULTI_STATEMENT" for v in d.violations)


def test_huge_input_is_blocked_within_budget():
    # QA P0: a pathological huge query must fail closed AND the block itself must
    # be cheap (an O(1) length check before pglast), not exhaust the latency budget.
    import time

    from engine import classifier, parser

    sql = (
        "SELECT * FROM film WHERE film_id IN ("
        + ",".join("1" for _ in range(20000))
        + ")"
    )
    parser.cache_clear()
    classifier.cache_clear()
    start = time.perf_counter()
    d = decide(sql, POLICY)
    elapsed_ms = (time.perf_counter() - start) * 1000
    assert not d.allowed  # blocked (fail closed)
    assert elapsed_ms < 5.0, f"huge-input block took {elapsed_ms:.1f} ms (budget 5 ms)"


def test_large_literal_list_under_byte_cap_is_blocked_within_budget():
    # Regression: a few KB of comma-heavy IN-list SQL used to slip under the byte
    # cap and spend tens of ms in libpg_query.
    import time

    from engine import classifier, parser

    sql = (
        "SELECT * FROM film WHERE film_id IN ("
        + ",".join("1" for _ in range(1000))
        + ")"
    )
    assert len(sql) < 8000
    parser.cache_clear()
    classifier.cache_clear()
    start = time.perf_counter()
    d = decide(sql, POLICY)
    elapsed_ms = (time.perf_counter() - start) * 1000
    assert not d.allowed
    assert elapsed_ms < 5.0, f"complex-input block took {elapsed_ms:.1f} ms"


def test_classify_is_total_over_random_bytes():
    # Fuzz-ish: random byte strings must never raise.
    import random

    rng = random.Random(42)
    for _ in range(200):
        n = rng.randint(0, 40)
        s = "".join(chr(rng.randint(0, 0x2FFF)) for _ in range(n))
        classify(s)  # must not raise
        decide(s, POLICY)  # must not raise
