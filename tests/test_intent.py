"""Intent-mismatch detection tests (advisory only).

Pure, in-memory, no database. Covers the deterministic contradiction signals
(scope/magnitude, read-vs-write, table mismatch), that it only ever flags writes,
and that folding a flag into a decision can escalate to confirmation but NEVER
blocks (or un-blocks) on its own.
"""

import time

from engine.classifier import classify
from engine.intent import HIGH, LOW, NONE, IntentConfig, check_intent
from engine.policy import Decision, apply_intent

CFG = IntentConfig(enabled=True)
VOCAB = frozenset({"customer", "film", "rental", "orders", "users"})


def _flag(task, sql, rows, cfg=CFG):
    return check_intent(task, classify(sql), rows, cfg, table_vocab=VOCAB)


# --- QA regressions ----------------------------------------------------------


def test_qa_p1a_oversized_task_is_bounded_and_fast():
    # A huge stated task must not blow the latency budget (it's capped).
    huge = "delete " + ("x " * 500000)  # ~1 MB
    sql = "UPDATE customer SET active=0 WHERE customer_id <= 100"
    start = time.perf_counter()
    f = check_intent(huge, classify(sql), 100, CFG)
    elapsed_ms = (time.perf_counter() - start) * 1000
    assert elapsed_ms < 5.0, f"intent check took {elapsed_ms:.1f} ms"
    # stored task is capped, not the full megabyte
    assert f.stated_task is None or len(f.stated_task) <= 2000


def test_qa_p1c_requested_count_is_not_a_single_cue():
    # "50 test rows" affecting 50 rows is consistent -> no flag.
    assert not _flag("delete 50 test rows", "DELETE FROM customer WHERE x", 50).mismatch
    # a duration number is a time window, not a row scope -> no magnitude flag.
    assert not _flag(
        "delete rows older than 30 days", "DELETE FROM customer WHERE x", 50
    ).mismatch
    # ...but exceeding the requested count IS flagged.
    f = _flag("delete 50 test rows", "DELETE FROM customer WHERE x", 5000)
    assert f.mismatch and f.severity == HIGH


def test_qa_p1c_id_reference_still_counts_as_single():
    # "customer 5" is a specific entity, not a count.
    f = _flag("fix customer 5", "UPDATE customer SET active=1 WHERE x", 8000)
    assert f.severity == HIGH


def test_qa_p2_get_rid_of_is_a_write_not_a_read():
    f = _flag(
        "get rid of old customers", "DELETE FROM customer WHERE customer_id <= 50", 50
    )
    assert not f.mismatch  # no read-vs-write false positive


def test_qa_wrong_table_write_is_high():
    # QA P1: task names film, statement targets customer -> HIGH (was LOW).
    f = _flag(
        "update film table", "UPDATE customer SET active=0 WHERE customer_id=5", None
    )
    assert f.severity == HIGH


def test_qa_schema_destructive_ddl_vs_row_task_is_high():
    # QA P1: a row-level task but the statement DROPs the whole table -> HIGH.
    f = _flag("delete old customer rows", "DROP TABLE customer", None)
    assert f.mismatch and f.severity == HIGH

    # ...but a task that actually asks to drop the table is consistent.
    assert not _flag("drop the customer table", "DROP TABLE customer", None).mismatch
    # TRUNCATE counts as table-destructive too.
    assert _flag("remove customer records", "TRUNCATE customer", None).severity == HIGH


def test_qa_p2_only_n_rows_is_a_count_not_single():
    # "only 50 rows" with 50 affected is consistent -> no flag.
    assert not _flag(
        "delete only 50 test rows", "DELETE FROM customer WHERE x", 50
    ).mismatch
    assert not _flag(
        "update just 5 records", "UPDATE customer SET x=1 WHERE y", 5
    ).mismatch
    # exceeding the stated count still flags.
    assert (
        _flag("delete only 50 test rows", "DELETE FROM customer WHERE x", 5000).severity
        == HIGH
    )


# --- Magnitude / scope contradictions ----------------------------------------


def test_specific_id_but_many_rows_is_high():
    f = _flag(
        "fix customer 5", "UPDATE customer SET active=1 WHERE create_date < now()", 8000
    )
    assert f.mismatch and f.severity == HIGH
    assert "single/specific" in f.reasons[0]


def test_singular_determiner_but_many_rows_is_high():
    assert (
        _flag(
            "delete this account", "DELETE FROM customer WHERE store_id=1", 300
        ).severity
        == HIGH
    )
    assert (
        _flag(
            "update a price", "UPDATE film SET rental_rate=1 WHERE rental_rate<3", 5000
        ).severity
        == HIGH
    )


def test_bulk_cue_is_consistent_with_large_blast_radius():
    assert (
        _flag(
            "delete all expired rentals", "DELETE FROM rental WHERE x IS NULL", 50000
        ).severity
        == NONE
    )
    assert (
        _flag("archive every old film", "UPDATE film SET v=1 WHERE 1=1", 5000).severity
        == NONE
    )


def test_scopeless_large_write_is_low():
    f = _flag("clean up rentals", "UPDATE rental SET v=1 WHERE x IS NULL", 5000)
    assert f.mismatch and f.severity == LOW


def test_small_blast_radius_is_not_flagged():
    assert not _flag(
        "delete customer 5", "DELETE FROM customer WHERE customer_id=5", 1
    ).mismatch
    assert not _flag("tidy a row", "UPDATE film SET v=1 WHERE film_id=1", 1).mismatch


# --- Operation contradiction -------------------------------------------------


def test_read_language_on_a_write_is_high():
    f = _flag("show me the customers", "DELETE FROM customer WHERE store_id=1", 300)
    assert f.severity == HIGH
    assert "read-only language" in f.reasons[0]


def test_read_word_with_a_write_word_is_not_a_read_mismatch():
    # "list ... then delete" mentions a write word -> not a pure read intent.
    f = _flag(
        "list and delete old customers", "DELETE FROM customer WHERE x IS NULL", 5
    )
    assert "read-only language" not in " ".join(f.reasons)


# --- Table contradiction -----------------------------------------------------


def test_table_mismatch_is_flagged_high():
    # QA: a wrong-target write (task names one table, statement hits another) is
    # a clear contradiction -> HIGH (so it escalates to confirmation).
    f = _flag("update the orders table", "DELETE FROM customer WHERE customer_id<10", 9)
    assert f.mismatch and f.severity == HIGH and "orders" in f.reasons[0]


# --- Gating ------------------------------------------------------------------


def test_no_task_no_flag():
    assert not _flag(None, "DELETE FROM customer WHERE customer_id=5", 1).mismatch


def test_disabled_no_flag():
    off = IntentConfig(enabled=False)
    assert not _flag(
        "delete this one thing", "DELETE FROM customer", 9999, off
    ).mismatch


def test_reads_are_never_flagged():
    # Intent is about the destructive surface; a SELECT is not flagged.
    f = check_intent(
        "delete a single customer", classify("SELECT * FROM customer"), None, CFG
    )
    assert not f.mismatch


# --- apply_intent: advisory only ---------------------------------------------

_ALLOWED = Decision(True, (), effective_sql="UPDATE t SET x=1 WHERE id<99")
_BLOCKED = Decision(False, (), effective_sql="x")


def test_high_mismatch_escalates_to_confirmation():
    f = _flag("update a single row", "UPDATE customer SET x=1 WHERE x IS NULL", 500)
    d = apply_intent(_ALLOWED, f, CFG)
    assert d.allowed is True  # never blocks
    assert d.requires_confirmation is True
    assert d.intent["severity"] == HIGH


def test_low_mismatch_is_advisory_only():
    f = _flag("clean up rentals", "UPDATE rental SET v=1 WHERE x IS NULL", 5000)
    d = apply_intent(_ALLOWED, f, CFG)
    assert d.requires_confirmation is False  # LOW does not escalate
    assert d.intent["severity"] == LOW


def test_intent_never_unblocks_a_blocked_decision():
    f = _flag("update a single row", "UPDATE customer SET x=1 WHERE x IS NULL", 500)
    d = apply_intent(_BLOCKED, f, CFG)
    assert d.allowed is False  # intent can't rescue a blocked statement
    assert d.intent is not None


def test_confirm_on_high_can_be_disabled():
    cfg = IntentConfig(enabled=True, confirm_on_high=False)
    f = _flag("update a single row", "UPDATE customer SET x=1 WHERE x IS NULL", 500)
    d = apply_intent(_ALLOWED, f, cfg)
    assert d.requires_confirmation is False  # flagged but not escalated
    assert d.intent["severity"] == HIGH
