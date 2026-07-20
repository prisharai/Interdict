"""Structural SQL parsing and classification tests.

Pure, in-memory, no database. Covers the classification matrix and difficult
Postgres shapes -- CTEs, sub-selects, UPDATE...FROM, comments, multi-statement,
system-catalog refs -- plus the anti-evasion extras
(data-modifying CTEs, bare writes) and safe handling of parse failures.
"""

import pytest

from engine import classifier, parser
from engine.classifier import DDL, READ, UNKNOWN, WRITE, classify


@pytest.fixture(autouse=True)
def _clear_caches():
    """Isolate cache state between tests."""
    parser.cache_clear()
    classifier.cache_clear()
    yield


# --- Basic kinds -------------------------------------------------------------


def test_plain_select_is_read():
    c = classify("SELECT a, b FROM film WHERE film_id = 1")
    assert c.kind == READ
    s = c.statements[0]
    assert s.stmt_type == "SelectStmt"
    assert s.tables == ("film",)
    assert set(s.columns) == {"a", "b", "film_id"}
    assert s.has_where is True
    assert s.unbounded_write is False


def test_update_with_where_is_bounded_write():
    c = classify("UPDATE film SET rental_rate = 1 WHERE film_id = 5")
    s = c.statements[0]
    assert c.kind == WRITE and s.kind == WRITE
    assert s.has_where is True
    assert s.unbounded_write is False
    assert s.tables == ("film",)


def test_bare_delete_is_unbounded_write():
    c = classify("DELETE FROM rental")
    s = c.statements[0]
    assert s.kind == WRITE
    assert s.has_where is False
    assert s.unbounded_write is True  # consumed by the policy's write guard


def test_bare_update_is_unbounded_write():
    s = classify("UPDATE film SET rental_rate = 0").statements[0]
    assert s.unbounded_write is True
    assert s.has_where is False


def test_insert_is_write():
    s = classify("INSERT INTO actor (first_name) VALUES ('x')").statements[0]
    assert s.kind == WRITE
    assert s.tables == ("actor",)


@pytest.mark.parametrize(
    "sql, expected_type",
    [
        ("DROP TABLE foo", "DropStmt"),
        ("TRUNCATE film, actor", "TruncateStmt"),
        ("ALTER TABLE film ADD COLUMN x int", "AlterTableStmt"),
        ("CREATE INDEX i ON film (title)", "IndexStmt"),
        ("CREATE TABLE t AS SELECT * FROM film", "CreateTableAsStmt"),
    ],
)
def test_ddl_statements(sql, expected_type):
    s = classify(sql).statements[0]
    assert s.kind == DDL
    assert s.stmt_type == expected_type


def test_show_is_other_but_session_control_fails_closed():
    assert classify("SHOW work_mem").kind == "other"
    assert classify("BEGIN").kind == DDL
    assert classify("SET search_path = x").kind == DDL


# --- Structurally tricky Postgres cases --------------------------------------


def test_cte_name_is_not_counted_as_a_table():
    c = classify(
        "WITH x AS (SELECT actor_id FROM actor) "
        "SELECT f.title FROM film f JOIN x ON true"
    )
    s = c.statements[0]
    # 'x' is a CTE, not a real table; 'actor' and 'film' are.
    assert s.tables == ("actor", "film")
    assert "x" not in s.tables


def test_update_from_collects_all_tables():
    c = classify(
        "UPDATE film f SET rental_rate = 1 FROM category c "
        "WHERE f.film_id = c.category_id"
    )
    s = c.statements[0]
    assert s.kind == WRITE
    assert s.tables == ("category", "film")
    assert s.has_where is True


def test_subselect_tables_are_collected():
    c = classify(
        "SELECT title FROM film WHERE film_id IN (SELECT film_id FROM inventory)"
    )
    s = c.statements[0]
    assert s.tables == ("film", "inventory")


def test_comments_are_stripped_by_the_real_parser():
    c = classify("/* hello */ SELECT 1 AS one -- trailing comment")
    assert c.kind == READ
    assert c.statements[0].stmt_type == "SelectStmt"


# --- Multi-statement detection (evasion-relevant) ----------------------------


def test_multi_statement_detected_and_aggregates_to_worst():
    c = classify("SELECT 1; DROP TABLE foo;")
    assert c.is_multi_statement is True
    assert c.statement_count == 2
    # A batch is as dangerous as its worst statement.
    assert c.kind == DDL
    assert [s.stmt_type for s in c.statements] == ["SelectStmt", "DropStmt"]


def test_single_statement_is_not_multi():
    c = classify("SELECT 1")
    assert c.is_multi_statement is False
    assert c.statement_count == 1


# --- System catalog references -----------------------------------------------


def test_qualified_catalog_reference_flagged():
    s = classify("SELECT * FROM pg_catalog.pg_tables").statements[0]
    assert s.touches_system_catalog is True


def test_unqualified_pg_relation_flagged():
    s = classify("SELECT relname FROM pg_class").statements[0]
    assert s.touches_system_catalog is True


def test_information_schema_flagged():
    s = classify("SELECT table_name FROM information_schema.tables").statements[0]
    assert s.touches_system_catalog is True


def test_ordinary_table_not_flagged_as_catalog():
    s = classify("SELECT * FROM film").statements[0]
    assert s.touches_system_catalog is False


# --- Anti-evasion extras -----------------------------------------------------


def test_data_modifying_cte_is_a_write_not_a_read():
    # Top node parses as SelectStmt, but it DELETEs. Must not be called a read.
    c = classify("WITH d AS (DELETE FROM rental RETURNING *) SELECT * FROM d")
    s = c.statements[0]
    assert s.stmt_type == "SelectStmt"
    assert s.kind == WRITE
    assert s.nested_dml is True
    assert s.unbounded_write is True  # the nested DELETE has no WHERE


def test_select_into_creates_a_table_so_is_ddl():
    s = classify("SELECT film_id INTO archived FROM film").statements[0]
    assert s.kind == DDL


# --- Parse failures fail safe ------------------------------------------------


def test_parse_error_is_unknown_not_an_exception():
    # Must not raise on the hot path; must classify as UNKNOWN (dangerous).
    c = classify("SELECT FROM WHERE ;;;")
    assert c.kind == UNKNOWN
    assert c.parse_error is not None
    assert c.statements == ()


def test_parser_surfaces_error_without_raising():
    r = parser.parse("this is not sql")
    assert r.ok is False
    assert r.error is not None
    assert r.statements == ()


# --- Caching (hot-path requirement) ------------------------------------------


def test_classify_is_cached():
    # The second identical call must hit the LRU cache, not re-parse/re-walk.
    classifier.cache_clear()
    classify("SELECT 1 FROM film")
    classify("SELECT 1 FROM film")
    assert classify.cache_info().hits >= 1


def test_to_dict_is_json_serializable():
    import json

    c = classify("UPDATE film SET rental_rate = 1 WHERE film_id = 1")
    blob = json.dumps(c.to_dict())  # must not raise
    assert "classification" not in blob  # sanity: it's the inner dict
    assert json.loads(blob)["kind"] == WRITE


# --- QA regressions ----------------------------------------------------------
# Permanent regressions for issues found in QA review. Each must
# stay fixed permanently.


@pytest.mark.parametrize(
    "sql",
    [
        "DO $$ BEGIN DELETE FROM rental; END $$",  # body unparsed, could mutate
        "COPY film TO PROGRAM 'id'",  # shell-out
        "ALTER SYSTEM SET work_mem = '64MB'",
        "CREATE DATABASE qa_db",
        "LOCK TABLE film",
        "VACUUM film",
        "REINDEX TABLE film",
        "REFRESH MATERIALIZED VIEW mv",
        "CALL myproc()",
        "EXECUTE p",  # the prepared statement's write is no longer visible here
    ],
)
def test_qa_p1a_side_effecting_statements_fail_closed_not_other(sql):
    # Must NOT classify as harmless OTHER; fail closed to DDL severity.
    assert classify(sql).kind == DDL


def test_qa_p1a_prepare_with_visible_write_is_caught_as_write():
    # PREPARE ... AS DELETE is parsed, so the write IS visible -> WRITE (not OTHER).
    assert classify("PREPARE p AS DELETE FROM film").kind == WRITE


@pytest.mark.parametrize("sql", ["SHOW work_mem"])
def test_qa_p1a_safe_utilities_stay_other(sql):
    assert classify(sql).kind == "other"


@pytest.mark.parametrize(
    "sql",
    [
        "BEGIN",
        "COMMIT",
        "SET search_path = x",
        "SET ROLE postgres",
        "SET statement_timeout = 0",
    ],
)
def test_session_control_fails_closed(sql):
    assert classify(sql).kind == DDL


def test_qa_p1b_nested_cte_does_not_hide_outer_real_table():
    # A CTE named in an inner subquery must not suppress the outer real table.
    s = classify(
        "SELECT * FROM secret "
        "WHERE EXISTS (WITH secret AS (SELECT 1) SELECT * FROM secret)"
    ).statements[0]
    assert s.tables == ("secret",)


def test_qa_p1b_same_named_cte_does_not_hide_write_target():
    s = classify("WITH film AS (SELECT 1) DELETE FROM film").statements[0]
    assert s.tables == ("film",)
    assert s.kind == WRITE


def test_qa_p1b_schema_qualified_name_is_always_real():
    s = classify("WITH film AS (SELECT 1) SELECT * FROM public.film").statements[0]
    assert s.tables == ("public.film",)


@pytest.mark.parametrize("bad", ["SELECT \ud800", "\udfff", "SELECT \udc00 FROM t"])
def test_qa_p2_malformed_unicode_does_not_raise(bad):
    # The hot path must never raise; malformed Unicode becomes UNKNOWN.
    c = classify(bad)
    assert c.kind == UNKNOWN
    assert c.parse_error is not None
