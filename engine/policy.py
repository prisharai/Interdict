"""Deterministic, YAML-driven policy engine.

HOT PATH. ``evaluate()`` is pure in-memory rule evaluation -- no I/O, no network,
no LLM. It blocks the obviously dangerous and allows
the obviously safe, and on a block returns a structured, machine-readable
rejection (reason code, human explanation, suggested fix) so the agent can
self-correct. Policy *loading* (YAML -> ``Policy``) happens once at startup and
is explicitly off the hot path.

Runtime posture:
* **Fail closed on uncertainty for writes.** An unparseable statement can't be
  reasoned about, so it's blocked (we can't prove it's a harmless read).
* **Fail open for reads.** A clean read that breaks no rule is allowed. The
  optional LIMIT auto-injection is a guardrail, not a gate: if rewriting fails
  for any reason we let the original read through rather than block it.
* **Default-deny tables.** When a table allowlist is configured, anything not on
  it is blocked.

This evaluates classifications from ``engine.classifier`` -- it never re-derives
statement facts by string matching.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from functools import lru_cache
from hashlib import sha256
from pathlib import Path

import yaml

from engine import classifier
from engine.classifier import DDL, UNKNOWN, WRITE, Classification
from engine.intent import HIGH, IntentConfig, IntentFlag
from engine.simulate import SimulationConfig, SimulationResult
from engine.undo import UndoConfig


# --- Stable, machine-readable reason codes -----------------------------------
class ReasonCode:
    """Stable codes an agent can branch on. Strings, never reordered."""

    READ_ONLY = "READ_ONLY_MODE"
    WRITE_WITHOUT_WHERE = "WRITE_WITHOUT_WHERE"
    MULTI_STATEMENT = "MULTI_STATEMENT"
    SYSTEM_CATALOG = "SYSTEM_CATALOG_ACCESS"
    CONTROL_PLANE = "CONTROL_PLANE_ACCESS"
    TABLE_NOT_ALLOWED = "TABLE_NOT_ALLOWED"
    DDL_NOT_ALLOWED = "DDL_NOT_ALLOWED"
    COLUMN_BLOCKED = "COLUMN_BLOCKED"
    LOCKING_NOT_ALLOWED = "LOCKING_NOT_ALLOWED"
    FUNCTION_NOT_ALLOWED = "FUNCTION_NOT_ALLOWED"
    BLAST_RADIUS_EXCEEDED = "BLAST_RADIUS_EXCEEDED"
    BLAST_RADIUS_UNKNOWN = "BLAST_RADIUS_UNKNOWN"
    NON_REVERSIBLE_WRITE = "NON_REVERSIBLE_WRITE"
    WRAPPED_WRITE = "WRAPPED_WRITE"
    UNPARSEABLE = "UNPARSEABLE"


# Valid policy modes. An unknown mode is a config error -> rejected at load
# (see Policy.__post_init__), and the adapter fails closed regardless.
VALID_MODES = frozenset({"enforce", "observe"})

# Functions blocked by default: they sleep/hold connections, take advisory locks,
# touch the filesystem/large objects, reach out over the network, or change
# server state. A SELECT calling one of these is not an "obviously safe" read.
_DEFAULT_BLOCKED_FUNCTIONS = frozenset(
    {
        "pg_sleep",
        "pg_sleep_for",
        "pg_sleep_until",
        "set_config",
        "pg_read_file",
        "pg_read_binary_file",
        "pg_ls_dir",
        "lo_import",
        "lo_export",
        "pg_terminate_backend",
        "pg_cancel_backend",
    }
)
# Whole families blocked by name prefix.
_BLOCKED_FUNCTION_PREFIXES = ("lo_", "dblink", "pg_advisory")

# These namespaces contain the approvals and undo evidence that make Interdict
# trustworthy.  They are not ordinary policy data: an agent may never touch
# them, even in observe mode or under a permissive table policy.  Keep the
# legacy name reserved so upgrading cannot expose old security records.
RESERVED_SCHEMAS = frozenset({"adb_undo", "interdict_control"})


def _is_blocked_function(name: str, policy: Policy) -> bool:
    bare = name.rsplit(".", 1)[-1]
    return bare in policy.blocked_functions or bare.startswith(
        _BLOCKED_FUNCTION_PREFIXES
    )


def _reserved_schema(name: str) -> str | None:
    """Return the protected schema named by a relation/function, if any."""
    schema, dot, _object = name.lower().partition(".")
    return schema if dot and schema in RESERVED_SCHEMAS else None


@dataclass(frozen=True)
class Violation:
    """One reason a statement was blocked. Machine-readable + human-actionable."""

    reason_code: str
    message: str
    suggested_fix: str
    statement_index: int = 0

    def to_dict(self) -> dict:
        return {
            "reason_code": self.reason_code,
            "message": self.message,
            "suggested_fix": self.suggested_fix,
            "statement_index": self.statement_index,
        }


@dataclass(frozen=True)
class Decision:
    """The verdict for one input string.

    ``effective_sql`` is what should actually run when allowed -- identical to the
    input unless a guardrail rewrote it (e.g. LIMIT injection), in which case
    ``rewritten`` is True.
    """

    allowed: bool
    violations: tuple[Violation, ...]
    effective_sql: str
    rewritten: bool = False
    # Blast-radius simulation, present only when a risky write was
    # simulated. ``requires_confirmation`` means allowed-but-gated -- a human/agent
    # must explicitly confirm before it runs.
    simulation: dict | None = None
    requires_confirmation: bool = False
    # Intent-mismatch advisory flag; can escalate to confirmation, never
    # blocks on its own.
    intent: dict | None = None

    def to_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "rewritten": self.rewritten,
            "effective_sql": self.effective_sql,
            "violations": [v.to_dict() for v in self.violations],
            "requires_confirmation": self.requires_confirmation,
            "simulation": self.simulation,
            "intent": self.intent,
        }


def _normalize_table(name: str) -> str:
    """Fold ``public.x`` and bare ``x`` to the same key; keep other schemas.

    Pagila lives in ``public``; an unqualified name resolves there by default, so
    ``film`` and ``public.film`` are the same table. A name in another schema
    stays distinct so the allowlist can't be bypassed via ``other.film``.
    """
    if "." not in name:
        return name
    schema, _, rel = name.partition(".")
    return rel if schema == "public" else name


def _bare_column(colref: str) -> str:
    """Last component of a (possibly qualified) column reference: ``f.x`` -> ``x``."""
    return colref.rsplit(".", 1)[-1]


@dataclass(frozen=True)
class Policy:
    """A declarative policy. Construct via :meth:`from_dict` / :meth:`load`.

    ``allowed_tables=None`` means "no table allowlist configured" -> all tables
    allowed. An empty frozenset means "allow nothing" (strict default-deny).
    """

    mode: str = "enforce"  # "enforce" (block) | "observe" (decide + log, run anyway)
    read_only: bool = False
    allow_multi_statement: bool = False
    block_system_catalog: bool = True
    require_where_on_writes: bool = True
    block_locking: bool = True  # block SELECT ... FOR UPDATE/SHARE (takes locks)
    max_rows_read: int | None = None  # auto-inject LIMIT on unbounded reads
    allowed_tables: frozenset[str] | None = None  # None = no allowlist (allow all)
    # None = allow all DDL; empty set = deny all DDL; non-empty = allowlist.
    ddl_allowed_tables: frozenset[str] | None = field(default_factory=frozenset)
    # table (normalized) -> blocked column names. A reference to one of these
    # columns -- or a star select over such a table -- is blocked.
    blocked_columns: dict[str, frozenset[str]] = field(default_factory=dict)
    blocked_functions: frozenset[str] = _DEFAULT_BLOCKED_FUNCTIONS
    require_qualified_tables: bool = False
    simulation: SimulationConfig = field(default_factory=SimulationConfig)
    undo: UndoConfig = field(default_factory=UndoConfig)
    intent: IntentConfig = field(default_factory=IntentConfig)

    def __post_init__(self) -> None:
        # Config errors fail loudly at construction/load, not silently at runtime.
        if self.mode not in VALID_MODES:
            raise ValueError(
                f"invalid policy mode {self.mode!r}; must be one of "
                f"{sorted(VALID_MODES)}"
            )

    @classmethod
    def from_dict(cls, data: dict) -> Policy:
        tables = data.get("tables") or {}
        allow = tables.get("allow")
        allowed_tables = (
            frozenset(_normalize_table(t) for t in allow) if allow is not None else None
        )
        ddl = data.get("ddl") or {}
        blocked_columns = {
            _normalize_table(table): frozenset(c.lower() for c in cols)
            for table, cols in (data.get("blocked_columns") or {}).items()
        }
        # User-listed unsafe functions are added to (never subtracted from) the
        # built-in defaults, so config can tighten but not weaken the safety set.
        blocked_functions = _DEFAULT_BLOCKED_FUNCTIONS | frozenset(
            f.lower() for f in (data.get("blocked_functions") or [])
        )
        return cls(
            mode=data.get("mode", "enforce"),
            read_only=bool(data.get("read_only", False)),
            allow_multi_statement=bool(data.get("allow_multi_statement", False)),
            block_system_catalog=bool(data.get("block_system_catalog", True)),
            require_where_on_writes=bool(data.get("require_where_on_writes", True)),
            block_locking=bool(data.get("block_locking", True)),
            max_rows_read=data.get("max_rows_read"),
            allowed_tables=allowed_tables,
            ddl_allowed_tables=frozenset(
                _normalize_table(t) for t in (ddl.get("allow") or [])
            ),
            blocked_columns=blocked_columns,
            blocked_functions=blocked_functions,
            require_qualified_tables=bool(data.get("require_qualified_tables", False)),
            simulation=SimulationConfig.from_dict(data.get("simulation")),
            undo=UndoConfig.from_dict(data.get("undo")),
            intent=IntentConfig.from_dict(data.get("intent")),
        )

    @classmethod
    def load(cls, path: str | Path) -> Policy:
        """Load a policy from a YAML file. Startup only -- never the hot path."""
        with Path(path).open(encoding="utf-8") as fh:
            return cls.from_dict(yaml.safe_load(fh) or {})

    @classmethod
    def permissive(cls) -> Policy:
        """A do-nothing policy: allows everything, rewrites nothing (tests/dev)."""
        return cls(
            mode="enforce",
            block_system_catalog=False,
            require_where_on_writes=False,
            allow_multi_statement=True,
            block_locking=False,
            allowed_tables=None,
            ddl_allowed_tables=None,
            blocked_functions=frozenset(),
        )


@lru_cache(maxsize=2048)
def _inject_limit(sql: str, limit: int) -> tuple[str, bool]:
    """Add ``LIMIT <limit>`` to a single unbounded top-level SELECT.

    Operates on a FRESH parse -- never the LRU-cached AST, which must stay
    immutable. Fails open: any problem returns the original SQL unchanged so a
    read is never blocked by a rewrite hiccup.

    Cached: the rewrite is deterministic in ``(sql, limit)``, so the re-parse +
    re-render (the priciest work on the read path) happens once per unique read,
    not every call.
    """
    try:
        from pglast import ast, parse_sql
        from pglast.enums import LimitOption
        from pglast.stream import RawStream

        stmts = parse_sql(sql)
        if len(stmts) != 1:
            return sql, False
        sel = stmts[0].stmt
        if type(sel).__name__ != "SelectStmt":
            return sql, False
        # Leave SELECT INTO and already-limited queries alone.
        if getattr(sel, "intoClause", None) is not None or sel.limitCount is not None:
            return sql, False
        sel.limitCount = ast.A_Const(val=ast.Integer(ival=limit))
        sel.limitOption = LimitOption.LIMIT_OPTION_COUNT
        return RawStream()(stmts), True
    except Exception:
        return sql, False


def _column_violations(stmt, policy: Policy, idx: int) -> list[Violation]:
    """Block references to sensitive columns, including star selects.

    Per-table blocked columns let us fail closed on ``SELECT *`` / ``t.*`` over a
    sensitive table (we can't prove the star excludes a blocked column) without
    over-blocking stars on non-sensitive tables.
    """
    if not policy.blocked_columns:
        return []
    sensitive = {
        _normalize_table(t): policy.blocked_columns[_normalize_table(t)]
        for t in stmt.tables
        if _normalize_table(t) in policy.blocked_columns
    }
    if not sensitive:
        return []
    blocked_names = frozenset().union(*sensitive.values())

    out: list[Violation] = []
    for col in stmt.columns:
        if col == "*":
            out.append(
                Violation(
                    ReasonCode.COLUMN_BLOCKED,
                    "SELECT * over a table with restricted columns is blocked; "
                    "the star may expose a sensitive column.",
                    "List the specific non-sensitive columns instead of '*'.",
                    idx,
                )
            )
        elif col.endswith(".*"):
            if _normalize_table(col[:-2]) in policy.blocked_columns:
                out.append(
                    Violation(
                        ReasonCode.COLUMN_BLOCKED,
                        f"'{col}' may expose a restricted column.",
                        "List the specific non-sensitive columns instead.",
                        idx,
                    )
                )
        elif _bare_column(col).lower() in blocked_names:
            out.append(
                Violation(
                    ReasonCode.COLUMN_BLOCKED,
                    f"Column '{col}' is marked sensitive and is blocked.",
                    "Remove the sensitive column from the statement.",
                    idx,
                )
            )
    return out


def evaluate(sql: str, classification: Classification, policy: Policy) -> Decision:
    """Decide whether ``sql`` may run under ``policy``. Pure, hot-path safe.

    Collects *all* violations (not just the first) so the agent gets complete,
    actionable feedback in one round trip. Allowed reads may be returned with a
    LIMIT injected (``rewritten=True``).
    """
    # Unparseable -> fail closed. We can't prove it's a safe read, so block.
    if classification.kind == UNKNOWN or classification.parse_error is not None:
        return Decision(
            allowed=False,
            violations=(
                Violation(
                    ReasonCode.UNPARSEABLE,
                    "Statement could not be parsed, so its effect can't be "
                    "verified; blocked as a fail-closed default.",
                    "Send a single, syntactically valid SQL statement.",
                ),
            ),
            effective_sql=sql,
        )

    violations: list[Violation] = []

    # Multi-statement: a classic way to smuggle a second statement past review.
    if classification.is_multi_statement and not policy.allow_multi_statement:
        violations.append(
            Violation(
                ReasonCode.MULTI_STATEMENT,
                f"Input contains {classification.statement_count} statements; "
                "multiple statements per request are not allowed.",
                "Send one statement per request.",
            )
        )

    for stmt in classification.statements:
        idx = stmt.index

        # This check is deliberately unconditional.  Policy authors can tune
        # application access, but cannot grant an agent access to the lockbox
        # that stores Interdict approvals and undo evidence.
        protected = sorted(
            {
                schema
                for name in (*stmt.tables, *stmt.functions)
                if (schema := _reserved_schema(name)) is not None
            }
        )
        for schema in protected:
            violations.append(
                Violation(
                    ReasonCode.CONTROL_PLANE,
                    f"Schema '{schema}' is reserved for Interdict security records.",
                    "Use an application table; control-plane records are never "
                    "available to an agent.",
                    idx,
                )
            )

        # A write wrapped inside a non-write top node (data-modifying CTE,
        # EXPLAIN ANALYZE <write>, ...) can hide or even execute the write while
        # looking like a read. Block it deterministically -- independent of
        # simulation, which might be disabled. Fail closed here.
        if stmt.nested_dml:
            violations.append(
                Violation(
                    ReasonCode.WRAPPED_WRITE,
                    f"A data-modifying statement is wrapped inside another "
                    f"({stmt.stmt_type}); this can hide or unexpectedly execute "
                    "the write and is not permitted.",
                    "Issue the UPDATE/DELETE/INSERT as a top-level statement.",
                    idx,
                )
            )

        if policy.read_only and stmt.kind in (WRITE, DDL):
            violations.append(
                Violation(
                    ReasonCode.READ_ONLY,
                    f"Policy is read-only; a {stmt.kind} statement "
                    f"({stmt.stmt_type}) is not permitted.",
                    "Issue a read-only (SELECT) statement instead.",
                    idx,
                )
            )

        if policy.require_where_on_writes and stmt.unbounded_write:
            violations.append(
                Violation(
                    ReasonCode.WRITE_WITHOUT_WHERE,
                    "UPDATE/DELETE has no WHERE clause and would affect every "
                    "row in the table.",
                    "Add a WHERE clause that scopes the statement to the "
                    "intended rows.",
                    idx,
                )
            )

        if policy.block_system_catalog and stmt.touches_system_catalog:
            violations.append(
                Violation(
                    ReasonCode.SYSTEM_CATALOG,
                    "Statement reads PostgreSQL system catalogs / "
                    "information_schema, which is not permitted.",
                    "Query application tables directly instead of catalog " "metadata.",
                    idx,
                )
            )

        # Default-deny table allowlist.
        if policy.require_qualified_tables:
            for table in stmt.tables:
                if "." not in table:
                    violations.append(
                        Violation(
                            ReasonCode.TABLE_NOT_ALLOWED,
                            f"Table '{table}' must be schema-qualified in this policy.",
                            "Use schema.table so Postgres cannot resolve a "
                            "different object.",
                            idx,
                        )
                    )
        if policy.allowed_tables is not None:
            for table in stmt.tables:
                if _normalize_table(table) not in policy.allowed_tables:
                    violations.append(
                        Violation(
                            ReasonCode.TABLE_NOT_ALLOWED,
                            f"Table '{table}' is not on the allowlist.",
                            "Restrict the statement to allowed tables, or request "
                            "that this table be added to the policy.",
                            idx,
                        )
                    )

        # DDL is default-deny unless every target table is DDL-allowlisted.
        # ddl_allowed_tables=None means DDL is unrestricted (e.g. permissive dev).
        if stmt.kind == DDL and policy.ddl_allowed_tables is not None:
            ddl_tables = stmt.tables or ("",)  # "" => object-level DDL w/o a table
            for table in ddl_tables:
                if _normalize_table(table) not in policy.ddl_allowed_tables:
                    target = f"'{table}'" if table else "this object"
                    violations.append(
                        Violation(
                            ReasonCode.DDL_NOT_ALLOWED,
                            f"Schema-changing statement ({stmt.stmt_type}) on "
                            f"{target} is not permitted.",
                            "DDL is blocked by default; request an explicit "
                            "allowlist entry if this change is intended.",
                            idx,
                        )
                    )

        # SELECT ... FOR UPDATE/SHARE takes row locks -- not an "obviously safe"
        # read.
        if policy.block_locking and stmt.has_locking:
            violations.append(
                Violation(
                    ReasonCode.LOCKING_NOT_ALLOWED,
                    "Statement uses a locking clause (FOR UPDATE/SHARE) and would "
                    "take row locks.",
                    "Remove the FOR UPDATE / FOR SHARE locking clause.",
                    idx,
                )
            )

        # Unsafe functions: sleeps, advisory locks, filesystem/network/server
        # state. A SELECT calling one is not a safe read.
        for fn in stmt.functions:
            if _is_blocked_function(fn, policy):
                violations.append(
                    Violation(
                        ReasonCode.FUNCTION_NOT_ALLOWED,
                        f"Function '{fn}()' is not permitted (it can sleep, lock, "
                        "reach the filesystem/network, or change server state).",
                        "Remove the call to this function.",
                        idx,
                    )
                )

        # Sensitive columns. With per-table blocked columns we can fail closed on
        # star selects over a sensitive table (can't prove '*' excludes a blocked
        # column), and match explicit/qualified references too.
        violations.extend(_column_violations(stmt, policy, idx))

    if violations:
        return Decision(False, tuple(violations), effective_sql=sql)

    # Allowed. Apply the read guardrail (LIMIT injection) if configured.
    effective_sql, rewritten = sql, False
    if policy.max_rows_read is not None and classification.statement_count == 1:
        stmt = classification.statements[0]
        if stmt.kind == "read" and stmt.stmt_type == "SelectStmt":
            effective_sql, rewritten = _inject_limit(sql, policy.max_rows_read)

    return Decision(True, (), effective_sql=effective_sql, rewritten=rewritten)


def apply_blast_radius(
    decision: Decision, result: SimulationResult, config: SimulationConfig
) -> Decision:
    """Fold a blast-radius simulation into a decision. Pure.

    * Over ``block_over_rows`` -> blocked with a ``BLAST_RADIUS_EXCEEDED``
      violation (a hard gate that always wins).
    * Over ``confirm_over_rows`` -> allowed but ``requires_confirmation``.
    * Timed out -> ``requires_confirmation`` (we couldn't bound the impact, so a
      human/agent should look before it runs).
    * Otherwise -> allowed, with the measurement attached for the audit trail.

    A ``skipped`` simulation leaves the decision untouched.
    """
    if result.method == "skipped":
        return decision

    sim = result.to_dict()
    rows = result.affected_rows

    # If the read-only counting path timed out, the blast radius is not bounded.
    # Hold for confirmation even if a planner estimate happens to be present.
    if result.timed_out and result.exact_rows is None:
        return replace(decision, simulation=sim, requires_confirmation=True)

    # Over the hard limit -> block (always wins).
    if (
        rows is not None
        and config.block_over_rows is not None
        and rows > config.block_over_rows
    ):
        violation = Violation(
            ReasonCode.BLAST_RADIUS_EXCEEDED,
            f"Statement would affect {rows} rows, over the limit of "
            f"{config.block_over_rows}.",
            "Scope the statement with a narrower WHERE clause so it affects "
            "fewer rows.",
        )
        return replace(
            decision,
            allowed=False,
            violations=decision.violations + (violation,),
            simulation=sim,
        )

    # Unknown blast radius: we asked but couldn't get a row count. Fail closed
    # for writes -- never silently allow. A timeout is treated above as
    # recoverable (hold for confirmation); any other unmeasurable case
    # (planner error, nested-DML CTE) is a hard block.
    if rows is None:
        violation = Violation(
            ReasonCode.BLAST_RADIUS_UNKNOWN,
            "Could not measure the statement's blast radius "
            f"({result.error or 'no row estimate'}); blocked as a fail-closed "
            "default for writes.",
            "Simplify or narrow the statement so its impact can be simulated.",
        )
        return replace(
            decision,
            allowed=False,
            violations=decision.violations + (violation,),
            simulation=sim,
        )

    needs_confirmation = (
        config.confirm_over_rows is not None and rows > config.confirm_over_rows
    )
    return replace(decision, simulation=sim, requires_confirmation=needs_confirmation)


def apply_undo_capture_limit(decision: Decision, config: UndoConfig) -> Decision:
    """Deny a measured write that would overflow the undo evidence budget."""
    if not decision.allowed or not decision.simulation:
        return decision
    rows = decision.simulation.get("affected_rows")
    if isinstance(rows, int) and rows > config.max_capture_rows:
        violation = Violation(
            ReasonCode.NON_REVERSIBLE_WRITE,
            f"Write would capture {rows} rows, over the undo limit of "
            f"{config.max_capture_rows}.",
            "Narrow the write or raise the limit only after reviewing control "
            "storage capacity.",
        )
        return replace(
            decision,
            allowed=False,
            violations=decision.violations + (violation,),
        )
    return decision


def apply_approval_ceiling(decision: Decision, approved_rows: int | None) -> Decision:
    """Invalidate an approval when fresh impact is larger than what was shown."""
    if approved_rows is None or not decision.allowed or not decision.simulation:
        return decision
    rows = decision.simulation.get("affected_rows")
    if not isinstance(rows, int) or rows <= approved_rows:
        return decision
    violation = Violation(
        ReasonCode.BLAST_RADIUS_EXCEEDED,
        f"Fresh measurement is {rows} rows, above the {approved_rows} rows approved.",
        "Request a new approval using the fresh blast-radius measurement.",
    )
    return replace(
        decision,
        allowed=False,
        requires_confirmation=False,
        violations=decision.violations + (violation,),
    )


def policy_fingerprint(policy: Policy) -> str:
    """Stable fingerprint used to bind a hold to the policy that produced it."""
    return sha256(repr(policy).encode("utf-8")).hexdigest()


def apply_intent(
    decision: Decision, flag: IntentFlag, config: IntentConfig
) -> Decision:
    """Fold an advisory intent-mismatch flag into a decision. Pure.

    Always attaches the flag for the audit trail. A HIGH-severity contradiction
    may escalate an *allowed* write to ``requires_confirmation`` (human review) --
    but intent NEVER blocks on its own: a blocked decision stays
    blocked-for-its-own-reasons and is never un-blocked here either.
    """
    if not flag.mismatch:
        return replace(decision, intent=flag.to_dict())
    escalate = decision.allowed and config.confirm_on_high and flag.severity == HIGH
    return replace(
        decision,
        intent=flag.to_dict(),
        requires_confirmation=decision.requires_confirmation or escalate,
    )


def decide(sql: str, policy: Policy) -> Decision:
    """Convenience: classify ``sql`` then evaluate it. Both are cached."""
    return evaluate(sql, classifier.classify(sql), policy)
