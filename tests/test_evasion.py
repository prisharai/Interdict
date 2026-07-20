"""Evasion suite: disguises must not get danger past the policy.

Because classification is done on the real Postgres AST (never the raw text),
comments / casing / whitespace / quoting cannot smuggle anything through. This
is a *matrix*: every dangerous base statement is run through every disguise and
must still be blocked. The named cases at the bottom are permanent regression
tests for evasions found during development.

Pure, in-memory (no DB), against the shipped policy.
"""

from pathlib import Path

import pytest

from engine.policy import Policy, decide

POLICY = Policy.load(
    Path(__file__).resolve().parent.parent / "policies" / "pagila.yaml"
)


def _blocked(sql: str) -> bool:
    return not decide(sql, POLICY).allowed


# Dangerous statements with NO quoted identifiers / meaningful string literals,
# so case/whitespace transforms preserve their meaning.
_DANGEROUS = [
    "DELETE FROM rental",  # bare delete
    "UPDATE film SET rental_rate = 0",  # bare update
    "DROP TABLE film",  # ddl
    "TRUNCATE rental",  # ddl
    "SELECT relname FROM pg_catalog.pg_class",  # system catalog
    "SELECT pg_sleep(60)",  # unsafe function
    "SELECT * FROM film FOR UPDATE",  # locking
]

# Disguises that preserve the danger (semantics unchanged).
_DISGUISES = {
    "identity": lambda s: s,
    "lower": str.lower,
    "upper": str.upper,
    "lead_block_comment": lambda s: "/* totally safe */ " + s,
    "trail_line_comment": lambda s: s + "   -- trust me",
    "newlines_tabs": lambda s: s.replace(" ", " \n\t "),
    "inner_block_comment": lambda s: s.replace(" ", " /* x */ ", 1),
}


@pytest.mark.parametrize("base", _DANGEROUS, ids=[s[:24] for s in _DANGEROUS])
@pytest.mark.parametrize("disguise", _DISGUISES)
def test_disguise_does_not_evade(base, disguise):
    sql = _DISGUISES[disguise](base)
    assert _blocked(sql), f"EVASION via {disguise!r}: {sql!r} was NOT blocked"


# --- Named permanent regressions (previously-found evasions) ------------------


def test_commented_out_where_is_still_a_bare_write():
    # A WHERE hidden in a comment looks scoped but isn't.
    assert _blocked("DELETE FROM rental /* WHERE rental_id = 1 */")
    assert _blocked("UPDATE film SET rental_rate = 0 -- WHERE film_id = 1")


def test_sneaky_second_statement_blocked():
    assert _blocked("SELECT 1; DROP TABLE film")
    assert _blocked("SELECT 1; /* gap */ DELETE FROM rental;")


def test_data_modifying_cte_is_not_a_read():
    # Top node parses as a SELECT, but it DELETEs every row.
    assert _blocked("WITH d AS (DELETE FROM rental RETURNING *) SELECT * FROM d")


def test_procedural_body_is_not_a_safe_read():
    # DO/CALL bodies are opaque to the parser -> fail closed (DDL severity).
    assert _blocked("DO $$ BEGIN DELETE FROM rental; END $$")
    assert _blocked("CALL some_proc()")


def test_quoted_catalog_access_blocked():
    assert _blocked('SELECT * FROM "pg_catalog"."pg_tables"')


def test_schema_qualified_disallowed_table_blocked():
    # Qualifying a non-allowlisted table with a schema doesn't allow it.
    assert _blocked("SELECT * FROM public.secret_accounts WHERE id = 1")


def test_execute_hides_a_prepared_write():
    # EXECUTE of a prepared statement -- the write isn't visible here -> fail closed.
    assert _blocked("EXECUTE p")


def test_alias_and_whole_row_projections_dont_leak_columns():
    # QA P1: a blocked column must not escape through an alias star or whole-row ref.
    assert _blocked("SELECT s.* FROM staff s WHERE staff_id = 1")
    assert _blocked("SELECT row_to_json(s) FROM staff s WHERE staff_id = 1")
    assert _blocked("SELECT s FROM staff s WHERE staff_id = 1")
    # ...but an alias star over a non-sensitive table is fine (no false positive).
    assert not _blocked("SELECT f.* FROM film f WHERE film_id = 1")


def test_explain_analyze_write_is_blocked():
    # QA P1: EXPLAIN ANALYZE actually executes the wrapped write -> blocked
    # deterministically, regardless of whether simulation is on.
    assert _blocked("EXPLAIN ANALYZE DELETE FROM rental WHERE rental_id = 1")
    assert _blocked("EXPLAIN ANALYZE UPDATE film SET rental_rate = 1 WHERE film_id = 1")
