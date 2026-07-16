"""Structural classification of a parsed statement.

HOT PATH. Given an AST, determine: read / write / DDL; tables and columns
touched; presence/absence of a WHERE clause; multi-statement detection;
references to system catalogs (CLAUDE.md sec. 8, Day 2). Pure in-memory work --
no I/O, no string matching, everything derived from the AST (sec. 6).

This is still **observe-only**: we describe a statement, we don't decide on it.
The policy engine (Day 3) consumes this. A few extras beyond the literal Day 2
list are computed because they're cheap and directly serve the project's
anti-evasion goals (sec. 8 Day 8):

* ``nested_dml`` -- a write hidden under a non-write top node, e.g. a
  data-modifying CTE ``WITH d AS (DELETE ... RETURNING *) SELECT * FROM d``. The
  top node parses as a ``SelectStmt``; without looking inside we'd call a DELETE
  a read. We look inside.
* ``unbounded_write`` -- any UPDATE/DELETE in the tree (top-level *or* nested)
  with no WHERE clause. This is exactly the "bare DELETE/UPDATE" the policy will
  block on Day 3, surfaced here so detection lives with the parse, not the rule.

Latency: pure tree walks over an already-parsed (cached) AST -- microseconds.
``classify`` itself is LRU-cached on the raw SQL so repeated identical
statements skip even the walk.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache

from pglast.enums import A_Expr_Kind, ObjectType
from pglast.visitors import Visitor

from engine import parser

# DROP object types that name a relation -- their targets feed the DDL allowlist.
_DROP_RELATION_TYPES = {
    ObjectType.OBJECT_TABLE,
    ObjectType.OBJECT_VIEW,
    ObjectType.OBJECT_MATVIEW,
    ObjectType.OBJECT_INDEX,
    ObjectType.OBJECT_SEQUENCE,
    ObjectType.OBJECT_FOREIGN_TABLE,
}

# --- Statement-kind taxonomy -------------------------------------------------
# Kinds, in increasing order of severity. The aggregate kind of a multi-statement
# input is the most severe of its parts (a batch is as dangerous as its worst
# statement). UNKNOWN is reserved for parse failures -- treated as dangerous so
# writes fail closed downstream (sec. 4).
READ = "read"
WRITE = "write"
DDL = "ddl"
OTHER = "other"
UNKNOWN = "unknown"

_SEVERITY = {OTHER: 0, READ: 1, WRITE: 2, DDL: 3, UNKNOWN: 4}

# Top-level statement node types we treat as data writes (DML).
_WRITE_STMTS = {"InsertStmt", "UpdateStmt", "DeleteStmt", "MergeStmt"}

# DML nodes whose absence of a WHERE makes them unbounded.
_CONDITIONAL_WRITE_STMTS = {"UpdateStmt", "DeleteStmt"}

# Node types that change schema/objects (DDL). Curated for the dangerous and
# common cases; anything unrecognized falls through to OTHER but keeps its
# ``stmt_type`` so we can spot and add it from the corpus (sec. 10).
_DDL_STMTS = {
    "CreateStmt",
    "CreateTableAsStmt",
    "CreateSchemaStmt",
    "CreateSeqStmt",
    "CreateExtensionStmt",
    "CreateFunctionStmt",
    "CreateTrigStmt",
    "CreateEnumStmt",
    "CreateDomainStmt",
    "CreateRoleStmt",
    "CreatePolicyStmt",
    "CreateForeignTableStmt",
    "DropStmt",
    "DropRoleStmt",
    "DropdbStmt",
    "TruncateStmt",
    "AlterTableStmt",
    "AlterSeqStmt",
    "AlterObjectSchemaStmt",
    "AlterRoleStmt",
    "RenameStmt",
    "IndexStmt",
    "ViewStmt",
    "GrantStmt",
    "GrantRoleStmt",
    "CommentStmt",
    "DefineStmt",
}

# Top-level node types that are genuinely safe: session-scoped, mutate no data or
# schema, and execute no arbitrary query. These (and only these) classify as
# OTHER. Everything unrecognized fails closed (see _kind_for). Note ExplainStmt
# is deliberately NOT here -- EXPLAIN ANALYZE actually runs the statement, so it
# isn't safe by node type alone; Day 3 policy can carve out plain EXPLAIN.
_SAFE_UTILITY_STMTS = {
    "VariableShowStmt",  # SHOW
}

# System schemas. A reference into these is a catalog touch -- something the
# policy will gate (sec. 8 Day 3). Unqualified ``pg_*`` relations resolve to
# pg_catalog in Postgres, so we flag those by name too.
_SYSTEM_SCHEMAS = {"pg_catalog", "information_schema"}


@dataclass(frozen=True)
class StatementInfo:
    """Structural facts about a single statement. Pure description, no verdict."""

    index: int  # position within a multi-statement input (0-based)
    stmt_type: str  # top AST node class name, e.g. "UpdateStmt"
    kind: str  # READ / WRITE / DDL / OTHER
    tables: tuple[str, ...]  # all real tables referenced (CTE names excluded)
    columns: tuple[str, ...]  # column references seen (incl. "*" / "t.*" stars)
    has_where: bool | None  # for top SELECT/UPDATE/DELETE; None if N/A
    unbounded_write: bool  # any UPDATE/DELETE (incl. nested) with no WHERE
    nested_dml: bool  # a write hidden under a non-write top node
    touches_system_catalog: bool
    has_locking: bool  # SELECT ... FOR UPDATE/SHARE/NO KEY UPDATE (takes locks)
    functions: tuple[
        str, ...
    ]  # lower-cased function names, schema-qualified when given
    point_write: bool  # UPDATE/DELETE whose WHERE is a single `col = value`
    point_write_column: str | None  # "table.column" of that equality (uniqueness TBD)


@dataclass(frozen=True)
class Classification:
    """Classification of a whole input string (one or more statements)."""

    statements: tuple[StatementInfo, ...]
    statement_count: int
    is_multi_statement: bool
    kind: str  # most severe kind across statements (UNKNOWN on parse error)
    parse_error: str | None = None
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        """JSON-serializable form for the audit log (sec. 10)."""
        return {
            "kind": self.kind,
            "statement_count": self.statement_count,
            "is_multi_statement": self.is_multi_statement,
            "parse_error": self.parse_error,
            "statements": [
                {
                    "index": s.index,
                    "stmt_type": s.stmt_type,
                    "kind": s.kind,
                    "tables": list(s.tables),
                    "columns": list(s.columns),
                    "has_where": s.has_where,
                    "unbounded_write": s.unbounded_write,
                    "nested_dml": s.nested_dml,
                    "touches_system_catalog": s.touches_system_catalog,
                    "has_locking": s.has_locking,
                    "functions": list(s.functions),
                    "point_write": s.point_write,
                    "point_write_column": s.point_write_column,
                }
                for s in self.statements
            ],
        }


def _ancestor_defines_cte(ancestors, name: str) -> bool:
    """True if some ancestor statement has a WITH clause defining CTE ``name``.

    This is the scope test: a ``WITH`` clause is only visible to the statement it
    is attached to (and that statement's subqueries). Walking *up* the ancestor
    chain from a RangeVar, any ancestor whose node carries a matching ``ctes``
    entry shadows the name at this position. A CTE defined in an inner subquery
    therefore can't shadow a reference in an outer query, because that inner
    WITH clause is not an ancestor of the outer reference.
    """
    a = ancestors
    while a is not None:
        node = getattr(a, "node", None)
        with_clause = getattr(node, "withClause", None)  # None for tuples/leaves
        if with_clause is not None:
            for cte in with_clause.ctes or ():
                if cte.ctename == name:
                    return True
        a = getattr(a, "parent", None)
    return False


class _Walk(Visitor):
    """Single pass over one statement's AST, collecting structural facts.

    Table resolution is scope-aware: each RangeVar records whether an in-scope
    CTE shadows it (computed from its ancestors), and write/merge *target*
    relations are remembered by identity so a same-named CTE can never hide the
    real table a statement writes to.
    """

    def __init__(self) -> None:
        # Per RangeVar: (schema, relname, shadowed_by_in_scope_cte, node_id).
        self.rangevars: list[tuple[str | None, str, bool, int]] = []
        self.target_ids: set[int] = set()  # id() of DML target RangeVars
        self.col_refs: list[list[str]] = []  # raw ColumnRef field lists (unresolved)
        self.alias_map: dict[str, str] = {}  # lower(alias|table) -> base relname
        self.write_nodes: list[str] = []  # node type names of DML found anywhere
        self.unbounded_write = False
        self.has_locking = False  # FOR UPDATE / SHARE etc. anywhere in the tree
        self.functions: set[str] = set()  # bare lower-cased function names

    def visit_RangeVar(self, ancestors, node):
        # Only an *unqualified* name can resolve to a CTE; a schema-qualified
        # name is always a real relation.
        shadowed = node.schemaname is None and _ancestor_defines_cte(
            ancestors, node.relname
        )
        self.rangevars.append((node.schemaname, node.relname, shadowed, id(node)))
        # Map both the bare table name and any alias to the base relation, so a
        # column qualifier (alias.col / alias.*) or whole-row ref resolves to it.
        self.alias_map[node.relname.lower()] = node.relname
        if node.alias is not None:
            self.alias_map[node.alias.aliasname.lower()] = node.relname

    def visit_ColumnRef(self, _ancestors, node):
        parts = []
        for f in node.fields:
            # A_Star is "*"; String fields carry the identifier in ``sval``.
            parts.append(
                "*" if type(f).__name__ == "A_Star" else getattr(f, "sval", "?")
            )
        self.col_refs.append(parts)

    def visit_LockingClause(self, _ancestors, _node):
        # SELECT ... FOR UPDATE/SHARE/NO KEY UPDATE/KEY SHARE -- takes row locks.
        self.has_locking = True

    def visit_FuncCall(self, _ancestors, node):
        # Preserve an explicit schema qualifier.  Interdict's own schemas are a
        # hard trust boundary, so ``interdict_control.some_function()`` must not
        # be reduced to an innocent-looking bare function name.
        if node.funcname:
            parts = [getattr(part, "sval", None) for part in node.funcname]
            if parts and all(parts):
                self.functions.add(".".join(parts).lower())

    def visit_DropStmt(self, _ancestors, node):
        # DROP of a relation carries its target(s) in ``objects`` (name lists),
        # not as RangeVars. Surface those names as tables so the DDL allowlist can
        # match a drop target (otherwise every DROP looks object-level).
        if node.removeType in _DROP_RELATION_TYPES:
            for obj in node.objects or ():
                names = [getattr(p, "sval", None) for p in obj]
                names = [n for n in names if n]
                if names:
                    schema = names[-2] if len(names) >= 2 else None
                    self.rangevars.append((schema, names[-1], False, id(obj)))

    def _note_write(self, node, type_name: str) -> None:
        self.write_nodes.append(type_name)
        # The target relation is a real table, never a CTE -- protect it from CTE
        # suppression even if a same-named CTE is in scope.
        relation = getattr(node, "relation", None)
        if relation is not None:
            self.target_ids.add(id(relation))
        if type_name in _CONDITIONAL_WRITE_STMTS and node.whereClause is None:
            self.unbounded_write = True

    def visit_UpdateStmt(self, _ancestors, node):
        self._note_write(node, "UpdateStmt")

    def visit_DeleteStmt(self, _ancestors, node):
        self._note_write(node, "DeleteStmt")

    def visit_InsertStmt(self, _ancestors, node):
        self._note_write(node, "InsertStmt")

    def visit_MergeStmt(self, _ancestors, node):
        self._note_write(node, "MergeStmt")


def _real_tables(walk: _Walk) -> tuple[str, ...]:
    """RangeVars minus in-scope CTE references, schema-qualified, sorted.

    A RangeVar is dropped only when an in-scope CTE shadows it AND it is not a
    write target -- so nested CTEs can't hide outer real tables and same-named
    CTEs can't hide write targets (scope-correct, sec. 6).
    """
    out: set[str] = set()
    for schema, relname, shadowed, node_id in walk.rangevars:
        if shadowed and node_id not in walk.target_ids:
            continue
        out.add(f"{schema}.{relname}" if schema else relname)
    return tuple(sorted(out))


def _resolve_columns(walk: _Walk) -> tuple[str, ...]:
    """Normalize column refs, resolving aliases and whole-row references.

    ``s.col`` / ``s.*`` resolve through the alias map to ``staff.col`` / ``staff.*``,
    and a bare reference to a table/alias (``SELECT s`` or ``row_to_json(s)``) is a
    whole-row reference -> ``staff.*``. This is what stops a blocked column leaking
    through an alias or a whole-row projection.
    """
    amap = walk.alias_map
    out: set[str] = set()
    for parts in walk.col_refs:
        if parts == ["*"]:
            out.add("*")  # unqualified star (policy checks it per referenced table)
        elif len(parts) >= 2 and parts[-1] == "*":  # qualified star: alias.* / t.*
            rel = amap.get(parts[-2].lower(), parts[-2])
            out.add(f"{rel}.*")
        elif len(parts) == 1:
            name = parts[0]
            if name.lower() in amap:  # bare table/alias -> whole-row reference
                out.add(f"{amap[name.lower()]}.*")
            else:
                out.add(name)  # an ordinary unqualified column
        else:  # qualified column: (schema.)?table.col
            rel = amap.get(parts[-2].lower(), parts[-2])
            out.add(f"{rel}.{parts[-1]}")
    return tuple(sorted(out))


def _touches_catalog(walk: _Walk) -> bool:
    for schema, relname, _shadowed, _node_id in walk.rangevars:
        if schema in _SYSTEM_SCHEMAS:
            return True
        if schema is None and relname.startswith("pg_"):
            return True
    return False


def _kind_for(stmt_type: str, walk: _Walk, sel_into: bool) -> str:
    """Severity-based kind: the most dangerous effect the statement carries."""
    if stmt_type in _DDL_STMTS or sel_into:
        return DDL
    if stmt_type in _WRITE_STMTS or walk.write_nodes:
        return WRITE
    if stmt_type == "SelectStmt":
        return READ
    if stmt_type in _SAFE_UTILITY_STMTS:
        return OTHER
    # Fail closed (sec. 4): every other top-level node is side-effecting or
    # unrecognized -- DO/CALL blocks (whose bodies we can't parse), COPY (incl.
    # COPY ... PROGRAM shell-out), LOCK, VACUUM, REINDEX, REFRESH MATERIALIZED
    # VIEW, CREATE/DROP DATABASE, ALTER SYSTEM, PREPARE/EXECUTE, future node
    # types, etc. Treat as DDL-severity (dangerous, gate it) rather than letting
    # it pass as harmless OTHER. UNKNOWN stays reserved for parse failures.
    return DDL


def _point_predicate_column(where) -> str | None:
    """The bare column of a single ``column = constant/param`` equality, else None.

    Identifies a "point" predicate (update/delete one record by ``col = value``).
    The *column name* is returned so callers can check whether it is actually a
    unique/PK column -- equality on a NON-unique column (e.g. ``customer_id``) is
    NOT a point write and must still be simulated. Anything compound (AND/OR),
    ranged (`<`), or set-based (`IN`) returns None.
    """
    if where is None or type(where).__name__ != "A_Expr":
        return None
    if where.kind != A_Expr_Kind.AEXPR_OP:
        return None
    names = [getattr(n, "sval", None) for n in (where.name or ())]
    if names != ["="]:
        return None
    if type(where.lexpr).__name__ != "ColumnRef":
        return None
    if type(where.rexpr).__name__ not in {"A_Const", "ParamRef"}:
        return None
    fields = where.lexpr.fields  # last field is the bare column name
    return getattr(fields[-1], "sval", None) if fields else None


def _classify_one(index: int, stmt_node) -> StatementInfo:
    walk = _Walk()
    walk(stmt_node)
    stmt_type = type(stmt_node).__name__

    # SELECT ... INTO creates a table -> treat as DDL, not a read.
    sel_into = (
        stmt_type == "SelectStmt" and getattr(stmt_node, "intoClause", None) is not None
    )

    kind = _kind_for(stmt_type, walk, sel_into)

    # has_where applies to the top statement when it's a SELECT/UPDATE/DELETE.
    if stmt_type in {"SelectStmt", "UpdateStmt", "DeleteStmt"}:
        has_where = getattr(stmt_node, "whereClause", None) is not None
    else:
        has_where = None

    # A write nested under a non-write top node (e.g. data-modifying CTE).
    nested_dml = bool(walk.write_nodes) and stmt_type not in _WRITE_STMTS

    # A scoped UPDATE/DELETE ... WHERE col = value. Whether it's actually
    # "routine" depends on col being unique -- callers check point_write_column
    # against known unique columns (a non-unique col like customer_id is bulk).
    point_write_column = None
    if stmt_type in {"UpdateStmt", "DeleteStmt"}:
        col = _point_predicate_column(getattr(stmt_node, "whereClause", None))
        if col is not None:
            point_write_column = f"{stmt_node.relation.relname}.{col}"
    point_write = point_write_column is not None

    return StatementInfo(
        index=index,
        stmt_type=stmt_type,
        kind=kind,
        tables=_real_tables(walk),
        columns=_resolve_columns(walk),
        has_where=has_where,
        unbounded_write=walk.unbounded_write,
        nested_dml=nested_dml,
        touches_system_catalog=_touches_catalog(walk),
        has_locking=walk.has_locking,
        functions=tuple(sorted(walk.functions)),
        point_write=point_write,
        point_write_column=point_write_column,
    )


@lru_cache(maxsize=2048)
def classify(sql: str) -> Classification:
    """Classify an input string. Cached, non-raising, hot-path safe.

    On a parse failure, returns a :class:`Classification` with ``kind=UNKNOWN``
    and the parser message -- the safe default, so writes fail closed downstream.
    """
    result = parser.parse(sql)
    if not result.ok:
        return Classification(
            statements=(),
            statement_count=0,
            is_multi_statement=False,
            kind=UNKNOWN,
            parse_error=result.error,
        )

    infos = tuple(_classify_one(i, raw.stmt) for i, raw in enumerate(result.statements))
    count = len(infos)
    agg_kind = (
        max((s.kind for s in infos), key=lambda k: _SEVERITY[k]) if infos else OTHER
    )
    return Classification(
        statements=infos,
        statement_count=count,
        is_multi_statement=count > 1,
        kind=agg_kind,
    )


def cache_clear() -> None:
    """Clear the classification cache (tests; or a policy/schema reload)."""
    classify.cache_clear()
