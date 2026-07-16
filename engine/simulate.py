"""Non-mutating blast-radius measurement for writes.

Every write is measured before execution. INSERT ... VALUES is counted from the
AST, primary-key equality is bounded to one row, and simple UPDATE/DELETE uses a
read-only ``SELECT count(*)`` over the same target predicate. EXPLAIN supplies a
secondary planner estimate. The proposed write is never executed for preview,
so preview cannot fire user triggers, advance sequences, or contact an external
service. Shapes that cannot be translated safely fail closed as unsupported.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from asyncpg.exceptions import LockNotAvailableError, QueryCanceledError
from pglast import parse_sql
from pglast.stream import RawStream

from engine.classifier import WRITE, Classification


@dataclass(frozen=True)
class SimulationConfig:
    """When and how to simulate. Default OFF -- simulation is opt-in (sec. 4)."""

    enabled: bool = False
    # Deprecated compatibility field. Measurement is always non-mutating.
    precise: bool = True
    statement_timeout_ms: int = 1000  # hard cap on planning/counting
    lock_timeout_ms: int = 200  # don't wait on contended locks
    block_over_rows: int | None = None  # block if blast radius exceeds this
    confirm_over_rows: int | None = None  # require confirmation above this

    @classmethod
    def from_dict(cls, data: dict | None) -> SimulationConfig:
        data = data or {}
        return cls(
            enabled=bool(data.get("enabled", False)),
            precise=bool(data.get("precise", True)),
            statement_timeout_ms=int(data.get("statement_timeout_ms", 1000)),
            lock_timeout_ms=int(data.get("lock_timeout_ms", 200)),
            block_over_rows=data.get("block_over_rows"),
            confirm_over_rows=data.get("confirm_over_rows"),
        )


@dataclass(frozen=True)
class SimulationResult:
    """What a simulation learned about a statement's blast radius."""

    method: str  # "count" | "bounded" | "static" | "unsupported" | "skipped"
    estimated_rows: int | None = None
    estimated_cost: float | None = None
    exact_rows: int | None = None
    timed_out: bool = False
    error: str | None = None

    @property
    def affected_rows(self) -> int | None:
        """Best available row count -- exact if we have it, else the estimate."""
        if self.timed_out and self.exact_rows is None:
            return None
        return self.exact_rows if self.exact_rows is not None else self.estimated_rows

    def to_dict(self) -> dict:
        return {
            "method": self.method,
            "estimated_rows": self.estimated_rows,
            "estimated_cost": self.estimated_cost,
            "exact_rows": self.exact_rows,
            "affected_rows": self.affected_rows,
            "timed_out": self.timed_out,
            "error": self.error,
        }


_SKIPPED = SimulationResult(method="skipped")

# Statement shapes whose affected rows need a database count.
_RISKY_WRITE_STMTS = {"UpdateStmt", "DeleteStmt", "MergeStmt"}

# Single-column unique / primary-key columns of every table, e.g. "film.film_id".
# A point write is only "routine" when scoped to one of these.
_UNIQUE_COLS_SQL = """
SELECT n.nspname || '.' || c.relname || '.' || a.attname
FROM pg_index i
JOIN pg_class c ON c.oid = i.indrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY (i.indkey)
WHERE i.indisunique
  AND array_length(i.indkey, 1) = 1
  AND n.nspname NOT IN ('pg_catalog', 'information_schema')
"""


async def load_unique_columns(conn) -> frozenset[str]:
    """Load ``table.column`` for every single-column unique/PK index (startup only)."""
    qualified = {r[0] for r in await conn.fetch(_UNIQUE_COLS_SQL)}
    # Development compatibility: public.x is also reachable as bare x under
    # the pinned trusted search_path. Production policies require qualification.
    bare_public = {
        name.removeprefix("public.") for name in qualified if name.startswith("public.")
    }
    return frozenset(qualified | bare_public)


def is_risky_write(
    classification: Classification, unique_columns: frozenset[str] = frozenset()
) -> bool:
    """Gate: only a *risky-shaped* single write is simulated (sec. 4).

    Risky = a single-statement UPDATE/DELETE/MERGE that is NOT scoped to a unique
    column, or a data-modifying CTE (routed here so it fails closed). A point write
    counts as routine (skip simulation) only when its predicate column is a known
    single-column unique/PK -- ``WHERE film_id = 1`` is one row, but
    ``WHERE customer_id = 1`` on a non-unique column can be thousands and MUST be
    simulated. With no ``unique_columns`` metadata, every scoped write is simulated
    (the safe default).
    """
    if classification.statement_count != 1 or not classification.statements:
        return False
    s = classification.statements[0]
    if s.kind != WRITE:
        return False
    if s.nested_dml:
        return True  # data-modifying CTE: simulate so it fails closed (P0)
    if s.stmt_type in _RISKY_WRITE_STMTS:
        # Routine only if scoped to a known-unique column; otherwise risky.
        return not (s.point_write and s.point_write_column in unique_columns)
    return False


async def _estimate(
    conn, sql: str, config: SimulationConfig
) -> tuple[int | None, float | None, str | None, bool]:
    """Planner estimate via EXPLAIN. Time-boxed (sec. 4).

    Returns (rows, cost, error, timed_out). EXPLAIN still takes an AccessShare
    lock for planning, so we run it inside a transaction with SET LOCAL
    statement/lock timeouts -- otherwise it could hang on a contended relation
    lock or pathological planner work. Read-only; always rolled back.
    """
    tr = conn.transaction()
    await tr.start()
    try:
        await conn.execute(
            f"SET LOCAL statement_timeout = {int(config.statement_timeout_ms)}"
        )
        await conn.execute(f"SET LOCAL lock_timeout = {int(config.lock_timeout_ms)}")
        raw = await conn.fetchval(f"EXPLAIN (FORMAT JSON) {sql}")
        plan = json.loads(raw)[0]["Plan"]
        return plan.get("Plan Rows"), plan.get("Total Cost"), None, False
    except (QueryCanceledError, LockNotAvailableError) as exc:
        return None, None, type(exc).__name__, True
    except Exception as exc:  # malformed plan, planner error -- estimate is optional
        return None, None, f"{type(exc).__name__}: {exc}", False
    finally:
        await tr.rollback()


def _static_insert_rows(sql: str) -> int | None:
    """Count INSERT ... VALUES rows from the AST without running any SQL."""
    stmt = parse_sql(sql)[0].stmt
    if type(stmt).__name__ != "InsertStmt":
        return None
    source = getattr(stmt, "selectStmt", None)
    values = getattr(source, "valuesLists", None) if source is not None else None
    return len(values) if values is not None else None


def _count_sql(sql: str) -> str | None:
    """Build a non-mutating count for a simple UPDATE/DELETE target predicate."""
    stmt = parse_sql(sql)[0].stmt
    kind = type(stmt).__name__
    if kind not in {"UpdateStmt", "DeleteStmt"}:
        return None
    if getattr(stmt, "withClause", None) is not None:
        return None
    if getattr(stmt, "fromClause", None) or getattr(stmt, "usingClause", None):
        return None
    relation = RawStream()(stmt.relation)
    where = getattr(stmt, "whereClause", None)
    where_sql = RawStream()(where) if where is not None else "true"
    return f"SELECT count(*)::bigint FROM {relation} WHERE {where_sql}"


async def _count_impact(
    conn, sql: str, config: SimulationConfig
) -> tuple[int | None, bool, str | None]:
    """Count target rows without executing the write or firing its triggers."""
    count_sql = _count_sql(sql)
    if count_sql is None:
        return None, False, "statement shape has no safe counting query"
    tr = conn.transaction(readonly=True)
    await tr.start()
    try:
        await conn.execute(
            f"SET LOCAL statement_timeout = {int(config.statement_timeout_ms)}"
        )
        await conn.execute(f"SET LOCAL lock_timeout = {int(config.lock_timeout_ms)}")
        return int(await conn.fetchval(count_sql)), False, None
    except (QueryCanceledError, LockNotAvailableError) as exc:
        return None, True, type(exc).__name__
    except Exception as exc:
        return None, False, f"{type(exc).__name__}: {exc}"
    finally:
        await tr.rollback()


async def simulate(
    conn,
    sql: str,
    classification: Classification,
    config: SimulationConfig,
    unique_columns: frozenset[str] = frozenset(),
) -> SimulationResult:
    """Measure a statement's blast radius. Only ever runs on a risky write.

    Returns a ``skipped`` result for anything that isn't a single-statement
    write, or when simulation is disabled -- so callers can invoke it
    unconditionally and it self-gates.
    """
    if (
        not config.enabled
        or classification.statement_count != 1
        or not classification.statements
        or classification.statements[0].kind != WRITE
    ):
        return _SKIPPED

    info = classification.statements[0]

    if info.stmt_type == "InsertStmt":
        rows = _static_insert_rows(sql)
        if rows is None:
            return SimulationResult(
                method="unsupported",
                error="INSERT ... SELECT impact is not safely measurable",
            )
        return SimulationResult(method="static", exact_rows=rows)

    if (
        info.point_write
        and info.point_write_column in unique_columns
        and info.stmt_type in {"UpdateStmt", "DeleteStmt"}
    ):
        return SimulationResult(method="bounded", estimated_rows=1)

    # Data-modifying CTE (P0): the outer command tag (e.g. "SELECT 1") does NOT
    # reflect the rows the nested write touches, so the exact count is not
    # measurable this way. Report it as unmeasurable -- the decision layer fails
    # closed on an unknown blast radius (apply_blast_radius).
    if classification.statements[0].nested_dml:
        return SimulationResult(
            method="unsupported",
            error="nested data-modifying CTE: blast radius not measurable",
        )

    est_rows, est_cost, est_err, est_timed_out = await _estimate(conn, sql, config)
    exact, timed_out, exact_err = await _count_impact(conn, sql, config)
    return SimulationResult(
        method="count",
        estimated_rows=est_rows,
        estimated_cost=est_cost,
        exact_rows=exact,
        timed_out=timed_out or est_timed_out,
        error=exact_err or est_err,
    )
