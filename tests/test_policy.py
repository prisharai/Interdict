"""Deterministic policy-engine tests.

Pure, in-memory, no database. Each rule is exercised in isolation, plus the
structured-rejection shape, LIMIT injection (and its parse-cache safety), and the
fail-closed defaults. The end-to-end red/green corpus lives in test_corpus.py.
"""

import pytest

from engine import classifier, parser
from engine.classifier import classify
from engine.policy import (
    Decision,
    Policy,
    ReasonCode,
    apply_blast_radius,
    decide,
    evaluate,
)
from engine.simulate import SimulationConfig, SimulationResult


@pytest.fixture(autouse=True)
def _clear_caches():
    parser.cache_clear()
    classifier.cache_clear()
    yield


def _codes(decision):
    return {v.reason_code for v in decision.violations}


# --- Individual rules --------------------------------------------------------


def test_read_only_blocks_writes_and_ddl_allows_reads():
    p = Policy(read_only=True, allowed_tables=None, ddl_allowed_tables=frozenset())
    assert not decide("UPDATE film SET x = 1 WHERE id = 1", p).allowed
    assert ReasonCode.READ_ONLY in _codes(decide("DELETE FROM film WHERE id=1", p))
    assert decide("SELECT * FROM film WHERE film_id = 1", p).allowed


def test_bare_write_blocked_scoped_write_allowed():
    p = Policy(allowed_tables=None)
    assert ReasonCode.WRITE_WITHOUT_WHERE in _codes(decide("DELETE FROM film", p))
    assert decide("DELETE FROM film WHERE film_id = 1", p).allowed


def test_multi_statement_blocked_unless_allowed():
    p = Policy(allowed_tables=None, ddl_allowed_tables=frozenset({"film"}))
    assert ReasonCode.MULTI_STATEMENT in _codes(decide("SELECT 1; SELECT 2", p))
    p2 = Policy(allowed_tables=None, allow_multi_statement=True)
    assert decide("SELECT 1; SELECT 2", p2).allowed


def test_system_catalog_blocked():
    p = Policy(allowed_tables=None)
    assert ReasonCode.SYSTEM_CATALOG in _codes(
        decide("SELECT * FROM pg_catalog.pg_tables", p)
    )
    assert ReasonCode.SYSTEM_CATALOG in _codes(decide("SELECT * FROM pg_class", p))


@pytest.mark.parametrize("mode", ["enforce", "observe"])
@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM adb_undo.pending_approval",
        "UPDATE adb_undo.pending_approval SET status='approved' "
        "WHERE approval_id='00000000-0000-0000-0000-000000000000'",
        "DELETE FROM interdict_control.undo_log "
        "WHERE action_id='00000000-0000-0000-0000-000000000000'",
        "SELECT interdict_control.approve('x')",
    ],
)
def test_control_plane_is_always_reserved(mode, sql):
    decision = decide(sql, Policy(mode=mode, allowed_tables=None))
    assert ReasonCode.CONTROL_PLANE in _codes(decision)


def test_table_allowlist_default_deny():
    p = Policy(allowed_tables=frozenset({"film"}))
    assert decide("SELECT * FROM film WHERE film_id = 1", p).allowed
    blocked = decide("SELECT * FROM customer WHERE customer_id = 1", p)
    assert ReasonCode.TABLE_NOT_ALLOWED in _codes(blocked)


def test_table_allowlist_folds_public_schema():
    p = Policy(allowed_tables=frozenset({"film"}))
    # public.film and bare film are the same table.
    assert decide("SELECT * FROM public.film WHERE film_id = 1", p).allowed
    # a different schema is NOT folded -> blocked.
    assert not decide("SELECT * FROM other.film WHERE film_id = 1", p).allowed


def test_production_style_policy_requires_qualified_query_tables():
    p = Policy(
        allowed_tables=frozenset({"film"}),
        require_qualified_tables=True,
    )
    assert not decide("SELECT * FROM film WHERE film_id = 1", p).allowed
    assert decide("SELECT * FROM public.film WHERE film_id = 1", p).allowed


def test_ddl_default_deny_and_allowlist():
    p = Policy(allowed_tables=None, ddl_allowed_tables=frozenset())
    assert ReasonCode.DDL_NOT_ALLOWED in _codes(decide("DROP TABLE film", p))
    p2 = Policy(allowed_tables=None, ddl_allowed_tables=frozenset({"film"}))
    assert decide("TRUNCATE film", p2).allowed
    # DDL on a non-allowlisted table is still blocked.
    assert not decide("DROP TABLE actor", p2).allowed


def test_object_level_ddl_without_a_table_is_blocked():
    # CREATE DATABASE / ALTER SYSTEM / SET have no target table -> default-deny.
    p = Policy(allowed_tables=None, ddl_allowed_tables=frozenset({"film"}))
    assert ReasonCode.DDL_NOT_ALLOWED in _codes(
        decide("ALTER SYSTEM SET work_mem = '64MB'", p)
    )
    assert ReasonCode.DDL_NOT_ALLOWED in _codes(
        decide("SET search_path = pg_catalog, public", p)
    )


def test_blocked_columns_denylist():
    p = Policy(allowed_tables=None, blocked_columns={"staff": frozenset({"password"})})
    assert ReasonCode.COLUMN_BLOCKED in _codes(
        decide("SELECT password FROM staff WHERE staff_id = 1", p)
    )
    assert decide("SELECT first_name FROM staff WHERE staff_id = 1", p).allowed


def test_blocked_columns_star_fails_closed_on_sensitive_table():
    # P1c: a star select over a table with restricted columns can't be proven
    # safe -> blocked. A star over a non-sensitive table is fine.
    p = Policy(allowed_tables=None, blocked_columns={"staff": frozenset({"password"})})
    assert not decide("SELECT * FROM staff WHERE staff_id = 1", p).allowed
    assert not decide("SELECT staff.* FROM staff WHERE staff_id = 1", p).allowed
    assert decide("SELECT * FROM actor", p).allowed


def test_locking_clause_blocked():
    p = Policy(allowed_tables=None)
    assert ReasonCode.LOCKING_NOT_ALLOWED in _codes(
        decide("SELECT * FROM film WHERE film_id = 1 FOR UPDATE", p)
    )
    p2 = Policy(allowed_tables=None, block_locking=False)
    assert decide("SELECT * FROM film WHERE film_id = 1 FOR UPDATE", p2).allowed


def test_unsafe_functions_blocked():
    p = Policy(allowed_tables=None)
    for sql in ("SELECT pg_sleep(60)", "SELECT lo_import('/etc/passwd')"):
        assert ReasonCode.FUNCTION_NOT_ALLOWED in _codes(decide(sql, p)), sql
    # ordinary aggregate functions are fine
    assert decide("SELECT count(*) FROM film", p).allowed


def test_invalid_mode_rejected_at_construction():
    # P1a: a typo'd mode is a config error, not a silent observe.
    with pytest.raises(ValueError):
        Policy(mode="enforec")


def test_drop_respects_ddl_allowlist_target():
    # P2: DROP target names are extracted, so the DDL allowlist applies to drops.
    p = Policy(allowed_tables=None, ddl_allowed_tables=frozenset({"film"}))
    assert decide("DROP TABLE film", p).allowed
    assert not decide("DROP TABLE actor", p).allowed


def test_unparseable_fails_closed():
    p = Policy(allowed_tables=None)
    d = decide("SELECT FROM WHERE ;;;", p)
    assert not d.allowed
    assert ReasonCode.UNPARSEABLE in _codes(d)


# --- Structured rejection shape ----------------------------------------------


def test_violation_carries_actionable_fields():
    p = Policy(allowed_tables=None)
    d = decide("DELETE FROM film", p)
    v = d.violations[0]
    assert v.reason_code and v.message and v.suggested_fix
    blob = d.to_dict()
    assert blob["allowed"] is False
    assert blob["violations"][0]["reason_code"] == ReasonCode.WRITE_WITHOUT_WHERE


def test_all_violations_collected_not_just_first():
    # A statement that breaks several rules reports all of them in one shot.
    p = Policy(allowed_tables=frozenset({"film"}))
    d = decide("SELECT * FROM pg_class; DROP TABLE secret", p)
    codes = _codes(d)
    assert {ReasonCode.MULTI_STATEMENT, ReasonCode.SYSTEM_CATALOG} <= codes


# --- LIMIT injection (read guardrail) ----------------------------------------


def test_limit_injected_on_unbounded_read():
    p = Policy(allowed_tables=None, max_rows_read=500)
    d = decide("SELECT * FROM film", p)
    assert d.allowed and d.rewritten
    assert d.effective_sql.rstrip().endswith("LIMIT 500")


def test_limit_not_injected_when_already_limited():
    p = Policy(allowed_tables=None, max_rows_read=500)
    d = decide("SELECT * FROM film LIMIT 5", p)
    assert d.allowed and not d.rewritten
    assert d.effective_sql == "SELECT * FROM film LIMIT 5"


def test_limit_not_injected_when_disabled():
    p = Policy(allowed_tables=None, max_rows_read=None)
    d = decide("SELECT * FROM film", p)
    assert d.allowed and not d.rewritten


def test_limit_not_injected_on_writes():
    p = Policy(allowed_tables=None, max_rows_read=500)
    d = decide("UPDATE film SET rental_rate = 1 WHERE film_id = 1", p)
    assert d.allowed and not d.rewritten


def test_limit_injection_does_not_corrupt_parse_cache():
    # _inject_limit must mutate a FRESH parse, never the cached AST.
    p = Policy(allowed_tables=None, max_rows_read=10)
    before = classify("SELECT a FROM film")
    decide("SELECT a FROM film", p)  # triggers injection on a fresh parse
    after = classify("SELECT a FROM film")
    assert after.statements[0].tables == before.statements[0].tables == ("film",)
    assert after.kind == "read"


# --- Policy plumbing ---------------------------------------------------------


def test_permissive_policy_allows_everything():
    p = Policy.permissive()
    assert decide("DROP TABLE film", p).allowed
    assert decide("SELECT * FROM anything", p).allowed
    assert decide("DELETE FROM film", p).allowed


def test_evaluate_ignores_mode_decision_is_modeless():
    # mode (enforce/observe) is an adapter concern; evaluate() returns the same
    # Decision either way -- it only decides, it doesn't act.
    enforce = Policy(mode="enforce", allowed_tables=frozenset({"film"}))
    observe = Policy(mode="observe", allowed_tables=frozenset({"film"}))
    sql = "SELECT * FROM customer WHERE customer_id = 1"
    assert evaluate(sql, classify(sql), enforce).allowed is False
    assert evaluate(sql, classify(sql), observe).allowed is False


def test_pagila_policy_file_loads():
    """The Pagila demo policy: locked-down example with table allowlist."""
    p = Policy.load("policies/pagila.yaml")
    assert p.mode == "enforce"
    assert p.max_rows_read == 1000
    assert "film" in p.allowed_tables
    assert "password" in p.blocked_columns["staff"]
    assert p.simulation.enabled is True
    assert p.simulation.block_over_rows == 100000
    assert p.undo.block_non_reversible is True
    assert p.undo.require_agent_match is True


def test_default_policy_file_loads():
    """The shipped default policy is database-agnostic: guards on, no allowlist."""
    p = Policy.load("policies/default.yaml")
    assert p.mode == "enforce"
    assert p.max_rows_read == 1000
    assert p.allowed_tables is None  # no table allowlist -> works on any database
    assert p.blocked_columns == {}
    assert p.simulation.enabled is True
    assert p.simulation.block_over_rows == 100000
    assert p.undo.block_non_reversible is True
    assert p.undo.require_agent_match is True


# --- Blast-radius thresholds (apply_blast_radius, pure) ----------------------

_ALLOWED = Decision(True, (), effective_sql="UPDATE t SET x=1 WHERE id<9")


def _result(**kw):
    return SimulationResult(method="precise", **kw)


def test_blast_radius_over_block_limit_blocks():
    cfg = SimulationConfig(block_over_rows=1000, confirm_over_rows=100)
    d = apply_blast_radius(_ALLOWED, _result(exact_rows=5000), cfg)
    assert d.allowed is False
    assert ReasonCode.BLAST_RADIUS_EXCEEDED in {v.reason_code for v in d.violations}
    assert d.simulation["exact_rows"] == 5000


def test_blast_radius_over_confirm_limit_requires_confirmation():
    cfg = SimulationConfig(block_over_rows=100000, confirm_over_rows=1000)
    d = apply_blast_radius(_ALLOWED, _result(exact_rows=5000), cfg)
    assert d.allowed is True
    assert d.requires_confirmation is True


def test_blast_radius_under_thresholds_allows_with_measurement():
    cfg = SimulationConfig(block_over_rows=100000, confirm_over_rows=1000)
    d = apply_blast_radius(_ALLOWED, _result(exact_rows=3), cfg)
    assert d.allowed is True and d.requires_confirmation is False
    assert d.simulation["exact_rows"] == 3


def test_blast_radius_timeout_requires_confirmation():
    # Couldn't bound the impact -> ask a human, don't silently allow.
    cfg = SimulationConfig(block_over_rows=100000, confirm_over_rows=1000)
    d = apply_blast_radius(_ALLOWED, _result(exact_rows=None, timed_out=True), cfg)
    assert d.allowed is True and d.requires_confirmation is True


def test_blast_radius_timeout_with_zero_estimate_requires_confirmation():
    # Regression: a precise count timeout may still carry a planner estimate of
    # zero. The timeout means the impact is unknown, not safe.
    cfg = SimulationConfig(block_over_rows=100000, confirm_over_rows=1000)
    d = apply_blast_radius(
        _ALLOWED,
        _result(estimated_rows=0, exact_rows=None, timed_out=True),
        cfg,
    )
    assert d.allowed is True and d.requires_confirmation is True
    assert d.simulation["affected_rows"] is None


def test_skipped_simulation_leaves_decision_untouched():
    d = apply_blast_radius(
        _ALLOWED, SimulationResult(method="skipped"), SimulationConfig()
    )
    assert d is _ALLOWED


def test_block_limit_wins_over_confirm():
    # Over both thresholds -> hard block, not merely confirmation.
    cfg = SimulationConfig(block_over_rows=1000, confirm_over_rows=100)
    d = apply_blast_radius(_ALLOWED, _result(exact_rows=2000), cfg)
    assert d.allowed is False


def test_blast_radius_unknown_fails_closed():
    # QA P1d: a write whose blast radius couldn't be measured (error, no count)
    # must NOT be silently allowed -- fail closed for writes.
    cfg = SimulationConfig(enabled=True, precise=False, block_over_rows=1)
    d = apply_blast_radius(
        _ALLOWED,
        SimulationResult(method="estimate", error="PlannerError", estimated_rows=None),
        cfg,
    )
    assert d.allowed is False
    assert ReasonCode.BLAST_RADIUS_UNKNOWN in {v.reason_code for v in d.violations}


def test_nested_dml_unsupported_blocks():
    # QA P0: a data-modifying CTE is reported unsupported -> blocked, never
    # allowed on a bogus top-level command-tag count.
    cfg = SimulationConfig(enabled=True, precise=True, block_over_rows=100000)
    d = apply_blast_radius(
        _ALLOWED,
        SimulationResult(method="unsupported", error="nested data-modifying CTE"),
        cfg,
    )
    assert d.allowed is False
    assert ReasonCode.BLAST_RADIUS_UNKNOWN in {v.reason_code for v in d.violations}
