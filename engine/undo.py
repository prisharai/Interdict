"""Reversibility / instant undo (differentiator #2).

For *allowed writes only* -- never on the read path (CLAUDE.md sec. 4, Day 5).
Around an allowed write we record a **before-image** (to restore to) and an
**after-image** (the state the write left behind), keyed by a per-action id, so
the change can be reverted with one call. Every record ties to an agent identity
and its stated task; reverts are audited and single-use.

Capture (type-safe via Postgres jsonb, so Python never types columns):

* **UPDATE:** before-image via ``SELECT to_jsonb(row) ... FOR UPDATE`` over the
  write's own target+WHERE (locks the affected rows), then run the UPDATE with an
  internal ``RETURNING *`` to capture the after-image.
* **DELETE:** run the DELETE with an internal ``RETURNING *`` -- the deleted rows
  are the before-image; the after-state is "absent".
* **INSERT:** run the INSERT with an internal ``RETURNING *`` -- the new rows are
  the after-image; there is nothing to restore.

Revert is **conditional and atomic** (this is what makes undo safe under
concurrent writes): it only acts on rows that still match the after-image, all in
one transaction. If any affected row changed since the agent write (or, for a
DELETE, its key was re-created), revert restores *nothing* and returns a conflict
for manual resolution -- it never clobbers a later change.

Honest limits (sec. 11) -- by default these are blocked before execution because
they cannot be safely inverted: multi-table ``UPDATE...FROM`` /
``DELETE...USING``, ``MERGE``, data-modifying CTEs, a top-level ``WITH`` on
UPDATE/DELETE, ``INSERT ... ON CONFLICT`` (upsert), any write with its own
``RETURNING``, UPDATEs that change a primary-key column, and tables without a
primary key (update/insert). A policy can opt into executing them with
``reversible=False`` for local experimentation. Out-of-row effects are not
reversed: consumed sequence values, ``ON DELETE CASCADE``, external triggers,
trigger-maintained columns.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from uuid import uuid4

from pglast import ast, parse_sql
from pglast.stream import RawStream

from engine.classifier import WRITE, Classification
from engine.schema import PrincipalKind, principal_from_legacy
from engine.security import client_db_error

# Primary-key columns of a relation, in index order.
_PK_SQL = """
SELECT a.attname
FROM pg_index i
JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY (i.indkey)
WHERE i.indrelid = $1::regclass AND i.indisprimary
ORDER BY array_position(i.indkey, a.attnum)
"""

# All live columns of a relation, in attribute order.
_COLS_SQL = """
SELECT attname
FROM pg_attribute
WHERE attrelid = $1::regclass AND attnum > 0 AND NOT attisdropped
ORDER BY attnum
"""

_PRE_V2_PRINCIPAL_JSON = json.dumps(
    {
        "id": "pre-v2",
        "kind": "service",
        "delegated_by": None,
        "task_id": None,
        "stated_task": None,
    }
)


def _q(ident: str) -> str:
    """Quote an SQL identifier."""
    return '"' + ident.replace('"', '""') + '"'


async def _rollback_quietly(tr) -> None:
    """Best-effort rollback for exception paths.

    When the failure that got us here killed the connection itself (terminated
    backend, dropped socket), the server has already aborted the transaction --
    a failed client-side ROLLBACK must not mask the original error by raising
    through the safety layer.
    """
    try:
        await tr.rollback()
    except Exception:
        pass


async def _execute_tolerating_duplicates(conn, sql: str) -> None:
    """Run DDL, treating "already exists" as success.

    Postgres's ``IF NOT EXISTS`` still errors when two sessions create the
    same object at the same instant (the loser collides on the catalog row).
    That race is real for us: the sidecar schema is created lazily on the
    first write, and concurrent agents can both be "first".
    """
    import asyncpg

    try:
        await conn.execute(sql)
    except (
        asyncpg.exceptions.UniqueViolationError,
        asyncpg.exceptions.DuplicateSchemaError,
        asyncpg.exceptions.DuplicateTableError,
        asyncpg.exceptions.DuplicateObjectError,
        asyncpg.exceptions.DuplicateColumnError,
    ):
        pass  # another session created it first — the object exists, we're done


def _tag_count(tag: str | None) -> int:
    """Affected-row count from a command tag ('UPDATE 3' / 'INSERT 0 3')."""
    if not tag:
        return 0
    last = tag.split()[-1]
    return int(last) if last.isdigit() else 0


def _with_returning_star(sql: str) -> str:
    """Render ``sql`` with an internal ``RETURNING *`` appended (fresh parse)."""
    tree = parse_sql(sql)
    tree[0].stmt.returningList = (
        ast.ResTarget(val=ast.ColumnRef(fields=(ast.A_Star(),))),
    )
    return RawStream()(tree)


def _capture_write(sql: str) -> str:
    """Wrap a write so it runs AND returns its affected rows as a jsonb array."""
    return (
        f"WITH __adb_w AS ({_with_returning_star(sql)}) "
        "SELECT coalesce(jsonb_agg(to_jsonb(__adb_w)), '[]'::jsonb)::text FROM __adb_w"
    )


@dataclass(frozen=True)
class UndoConfig:
    enabled: bool = False
    schema: str = "adb_undo"
    block_non_reversible: bool = True
    require_agent_match: bool = True

    @classmethod
    def from_dict(cls, data: dict | None) -> UndoConfig:
        data = data or {}
        return cls(
            enabled=bool(data.get("enabled", False)),
            schema=data.get("schema", "adb_undo"),
            block_non_reversible=bool(data.get("block_non_reversible", True)),
            require_agent_match=bool(data.get("require_agent_match", True)),
        )


@dataclass(frozen=True)
class UndoOutcome:
    """Result of executing a write through the undo path."""

    status: str | None
    rows: list
    error: str | None
    action_id: str | None  # set only when an undo record was written
    reversible: bool
    reason: str | None = None  # why not reversible (when reversible is False)
    captured_rows: int | None = None
    blocked: bool = False


@dataclass(frozen=True)
class RevertResult:
    ok: bool
    action_id: str
    operation: str | None = None
    rows_restored: int | None = None
    conflict: bool = False
    unauthorized: bool = False
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "action_id": self.action_id,
            "operation": self.operation,
            "rows_restored": self.rows_restored,
            "conflict": self.conflict,
            "unauthorized": self.unauthorized,
            "error": self.error,
        }


@dataclass(frozen=True)
class _Plan:
    operation: str  # insert | update | delete
    target: str  # table ref for type/DML, e.g. "film" / "public.film"
    write_capture_sql: str  # runs the write, returns affected rows as jsonb text
    before_capture_sql: str | None = None  # UPDATE only: old rows (FOR UPDATE)
    set_columns: tuple[str, ...] = ()  # columns an UPDATE assigns (PK-change check)


def _target_ref(relation) -> str:
    schema = relation.schemaname
    return f"{schema}.{relation.relname}" if schema else relation.relname


def _plan(sql: str, classification: Classification) -> tuple[_Plan | None, str | None]:
    """Build an undo capture plan, or (None, reason) if the shape is unsupported."""
    if classification.statement_count != 1 or not classification.statements:
        return None, "only single statements are reversible"
    info = classification.statements[0]
    if info.kind != WRITE or info.nested_dml:
        return None, "statement shape is not a simple reversible write"

    stmt = parse_sql(sql)[0].stmt
    op_node = type(stmt).__name__

    # A write that already returns rows: route to plain execute so its RETURNING
    # rows are preserved (we'd otherwise overwrite RETURNING for capture).
    if getattr(stmt, "returningList", None):
        return None, "writes with RETURNING are not auto-reversible"

    if op_node == "InsertStmt":
        if getattr(stmt, "onConflictClause", None):
            return None, "INSERT ... ON CONFLICT (upsert) is not auto-reversible"
        return (
            _Plan("insert", _target_ref(stmt.relation), _capture_write(sql)),
            None,
        )

    if op_node in ("UpdateStmt", "DeleteStmt"):
        if getattr(stmt, "withClause", None):
            return None, "WITH on UPDATE/DELETE is not auto-reversible"
        if getattr(stmt, "fromClause", None) or getattr(stmt, "usingClause", None):
            return None, "multi-table UPDATE/DELETE (FROM/USING) is not reversible"

        target = _target_ref(stmt.relation)
        if op_node == "DeleteStmt":
            # DELETE ... RETURNING * captures the deleted rows = before-image.
            return _Plan("delete", target, _capture_write(sql)), None

        # UPDATE: capture old rows (locks them), then run with RETURNING *.
        relation_sql = RawStream()(stmt.relation)
        rowvar = (
            stmt.relation.alias.aliasname
            if stmt.relation.alias
            else stmt.relation.relname
        )
        where = stmt.whereClause
        where_sql = RawStream()(where) if where is not None else "true"
        before_sql = (
            f"WITH __adb_b AS (SELECT to_jsonb({_q(rowvar)}) AS j "
            f"FROM {relation_sql} WHERE {where_sql} FOR UPDATE) "
            "SELECT coalesce(jsonb_agg(j), '[]'::jsonb)::text FROM __adb_b"
        )
        set_cols = tuple(rt.name for rt in (stmt.targetList or ()) if rt.name)
        return (
            _Plan("update", target, _capture_write(sql), before_sql, set_cols),
            None,
        )

    return None, "statement is not an INSERT/UPDATE/DELETE"


class UndoStore:
    """The sidecar undo log (a table in the target DB) + small schema cache."""

    def __init__(self, config: UndoConfig) -> None:
        self._schema = config.schema
        self._ensured = False
        self._pk: dict[str, list[str]] = {}
        self._cols: dict[str, list[str]] = {}

    @property
    def _log(self) -> str:
        return f"{_q(self._schema)}.undo_log"

    async def ensure_schema(self, conn) -> None:
        """Create the undo schema/table if missing. Idempotent; runs once.

        ``IF NOT EXISTS`` is not concurrency-safe in Postgres: two sessions
        creating the same table at the same instant can still collide on the
        pg_type row. First-ever writes from concurrent agents hit exactly
        that, so duplicate errors here mean "another session won" — fine.
        """
        if self._ensured:
            return
        await _execute_tolerating_duplicates(
            conn, f"CREATE SCHEMA IF NOT EXISTS {_q(self._schema)}"
        )
        await _execute_tolerating_duplicates(
            conn,
            f"""CREATE TABLE IF NOT EXISTS {self._log} (
                action_id     uuid PRIMARY KEY,
                created_at    timestamptz NOT NULL DEFAULT now(),
                agent         text,
                stated_task   text,
                principal     jsonb NOT NULL DEFAULT '{_PRE_V2_PRINCIPAL_JSON}'::jsonb,
                target_table  text NOT NULL,
                operation     text NOT NULL,
                pk_columns    text[] NOT NULL,
                row_count     int NOT NULL,
                before_images jsonb NOT NULL,
                after_images  jsonb NOT NULL DEFAULT '[]'::jsonb,
                status        text NOT NULL DEFAULT 'active',
                reverted_at   timestamptz,
                reverted_by   jsonb
            )""",
        )
        # Migrate an older log that predates after_images.
        await _execute_tolerating_duplicates(
            conn,
            f"ALTER TABLE {self._log} "
            "ADD COLUMN IF NOT EXISTS after_images jsonb NOT NULL DEFAULT '[]'::jsonb",
        )
        await _execute_tolerating_duplicates(
            conn,
            f"ALTER TABLE {self._log} "
            f"ADD COLUMN IF NOT EXISTS principal jsonb NOT NULL "
            f"DEFAULT '{_PRE_V2_PRINCIPAL_JSON}'::jsonb",
        )
        await _execute_tolerating_duplicates(
            conn,
            f"ALTER TABLE {self._log} ADD COLUMN IF NOT EXISTS reverted_by jsonb",
        )
        self._ensured = True

    async def primary_key(self, conn, table: str) -> list[str]:
        if table not in self._pk:
            self._pk[table] = [r[0] for r in await conn.fetch(_PK_SQL, table)]
        return self._pk[table]

    async def columns(self, conn, table: str) -> list[str]:
        if table not in self._cols:
            self._cols[table] = [r[0] for r in await conn.fetch(_COLS_SQL, table)]
        return self._cols[table]

    async def record(
        self,
        conn,
        *,
        action_id,
        agent,
        stated_task,
        principal,
        target_table,
        operation,
        pk_columns,
        row_count,
        before_images,
        after_images,
    ) -> None:
        await conn.execute(
            f"""INSERT INTO {self._log}
                (action_id, agent, stated_task, principal, target_table, operation,
                 pk_columns, row_count, before_images, after_images)
                VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7, $8,
                        $9::jsonb, $10::jsonb)""",
            action_id,
            agent,
            stated_task,
            json.dumps(principal),
            target_table,
            operation,
            pk_columns,
            row_count,
            before_images,
            after_images,
        )

    async def get(self, conn, action_id) -> dict | None:
        row = await conn.fetchrow(
            f"""SELECT action_id::text, agent, stated_task, principal::text,
                       target_table, operation,
                       pk_columns, row_count, before_images::text,
                       after_images::text, status, reverted_by::text
                FROM {self._log} WHERE action_id = $1::uuid""",
            action_id,
        )
        return dict(row) if row else None

    async def mark_reverted(self, conn, action_id, reverted_by: dict) -> None:
        await conn.execute(
            f"UPDATE {self._log} SET status='reverted', reverted_at=now() "
            "WHERE action_id=$1::uuid",
            action_id,
        )
        await conn.execute(
            f"UPDATE {self._log} SET reverted_by=$2::jsonb WHERE action_id=$1::uuid",
            action_id,
            json.dumps(reverted_by),
        )


async def _plain_execute(conn, sql: str) -> tuple[str | None, list, str | None]:
    """Execute a statement without undo capture (unsupported shapes).

    Uses prepare()+fetch() so RETURNING rows/status are preserved for the agent.
    """
    import asyncpg

    try:
        stmt = await conn.prepare(sql)
        records = await stmt.fetch()
        return stmt.get_statusmsg(), [dict(r) for r in records], None
    except asyncpg.PostgresError as exc:
        return None, [], client_db_error(exc)


async def execute_with_undo(
    conn,
    sql: str,
    classification: Classification,
    *,
    agent: str | None,
    stated_task: str | None,
    config: UndoConfig,
    store: UndoStore,
) -> UndoOutcome:
    """Execute a write, capturing before/after images so it can be reverted.

    Capture, the write, and the undo-log row all commit together. A write whose
    shape we can't safely invert is blocked by default; callers may explicitly
    set ``block_non_reversible=false`` for local experimentation.
    """
    plan, reason = _plan(sql, classification)
    if plan is None:
        if config.block_non_reversible:
            return UndoOutcome(
                None,
                [],
                None,
                None,
                reversible=False,
                reason=reason,
                blocked=True,
            )
        status, rows, error = await _plain_execute(conn, sql)
        return UndoOutcome(status, rows, error, None, reversible=False, reason=reason)

    await store.ensure_schema(conn)
    pk = await store.primary_key(conn, plan.target)
    if plan.operation in ("update", "insert") and not pk:
        reason = "target has no primary key"
        if config.block_non_reversible:
            return UndoOutcome(
                None,
                [],
                None,
                None,
                reversible=False,
                reason=reason,
                blocked=True,
            )
        status, rows, error = await _plain_execute(conn, sql)
        return UndoOutcome(
            status,
            rows,
            error,
            None,
            reversible=False,
            reason=reason,
        )
    if plan.operation == "update" and any(c in pk for c in plan.set_columns):
        # Revert matches the old PK; a changed PK can't be matched.
        reason = "UPDATE modifies a primary-key column"
        if config.block_non_reversible:
            return UndoOutcome(
                None,
                [],
                None,
                None,
                reversible=False,
                reason=reason,
                blocked=True,
            )
        status, rows, error = await _plain_execute(conn, sql)
        return UndoOutcome(
            status,
            rows,
            error,
            None,
            reversible=False,
            reason=reason,
        )

    action_id = uuid4()
    principal = principal_from_legacy(agent, stated_task=stated_task).to_dict()
    tr = conn.transaction()
    await tr.start()
    try:
        if plan.operation == "update":
            before = await conn.fetchval(plan.before_capture_sql)  # old rows (locked)
            after = await conn.fetchval(plan.write_capture_sql)  # runs UPDATE; new rows
            row_count = len(json.loads(after))
            status = f"UPDATE {row_count}"
        elif plan.operation == "delete":
            before = await conn.fetchval(
                plan.write_capture_sql
            )  # runs DELETE; old rows
            after = "[]"
            row_count = len(json.loads(before))
            status = f"DELETE {row_count}"
        else:  # insert
            after = await conn.fetchval(plan.write_capture_sql)  # runs INSERT; new rows
            before = "[]"
            row_count = len(json.loads(after))
            status = f"INSERT 0 {row_count}"
        await store.record(
            conn,
            action_id=action_id,
            agent=agent,
            stated_task=stated_task,
            principal=principal,
            target_table=plan.target,
            operation=plan.operation,
            pk_columns=pk,
            row_count=row_count,
            before_images=before,
            after_images=after,
        )
        await tr.commit()
    except Exception as exc:
        await _rollback_quietly(tr)
        return UndoOutcome(
            None,
            [],
            client_db_error(exc),
            None,
            reversible=False,
            reason="execution failed",
        )

    return UndoOutcome(
        status, [], None, str(action_id), reversible=True, captured_rows=row_count
    )


def _revert_sql(operation: str, table: str, pk: list[str], cols: list[str]) -> str:
    """Build the conditional inverse statement.

    Only rows that still match the after-image are touched -- so a row changed
    since the agent write is left alone (and shows up as a count shortfall the
    caller treats as a conflict). ``$1`` = before-images, ``$2`` = after-images.
    """

    def recordset(param: int) -> str:
        return f"jsonb_populate_recordset(null::{table}, ${param}::jsonb)"

    def pk_match(x: str, y: str) -> str:
        return " AND ".join(f"{x}.{_q(c)} = {y}.{_q(c)}" for c in pk)

    # Param order matches the tuple revert() passes for each operation.
    if operation == "update":  # $1=before, $2=after; restore where current==after
        set_cols = [c for c in cols if c not in pk]
        set_clause = ", ".join(f"{_q(c)} = __b.{_q(c)}" for c in set_cols)
        return (
            f"UPDATE {table} AS __t SET {set_clause} "
            f"FROM {recordset(1)} AS __b JOIN {recordset(2)} AS __a "
            f"ON {pk_match('__a', '__b')} "
            f"WHERE {pk_match('__t', '__b')} AND to_jsonb(__t) = to_jsonb(__a)"
        )
    if operation == "insert":  # $1=after; delete inserted rows where current==after
        return (
            f"DELETE FROM {table} AS __t USING {recordset(1)} AS __a "
            f"WHERE {pk_match('__t', '__a')} AND to_jsonb(__t) = to_jsonb(__a)"
        )
    if operation == "delete":  # $1=before; re-insert only where the key is now free
        return (
            f"INSERT INTO {table} SELECT __b.* FROM {recordset(1)} AS __b "
            f"WHERE NOT EXISTS (SELECT 1 FROM {table} AS __c "
            f"WHERE {pk_match('__c', '__b')})"
        )
    raise ValueError(f"unknown operation {operation!r}")


async def revert(
    conn,
    action_id: str,
    store: UndoStore,
    *,
    agent: str | None = None,
    require_agent_match: bool = False,
) -> RevertResult:
    """Reverse a recorded write by its action id. Conditional and atomic.

    Restores only if every affected row still matches the write's after-state;
    otherwise nothing is changed and a conflict is reported.
    """
    rec = await store.get(conn, action_id)
    if rec is None:
        return RevertResult(False, action_id, error="no such action_id")
    if rec["status"] == "reverted":
        return RevertResult(False, action_id, error="already reverted")
    original_principal = json.loads(rec["principal"])
    original_id = original_principal.get("id")
    if require_agent_match and original_id not in (None, "anonymous", "pre-v2"):
        authorized = agent == original_id
    else:
        authorized = not (
            require_agent_match and rec["agent"] and agent != rec["agent"]
        )
    if not authorized:
        return RevertResult(
            False,
            action_id,
            unauthorized=True,
            error="agent is not authorized to revert this action",
        )

    op = rec["operation"]
    table = rec["target_table"]
    cols = await store.columns(conn, table)
    sql = _revert_sql(op, table, rec["pk_columns"], cols)
    expected = rec["row_count"]
    params = (
        (rec["before_images"], rec["after_images"])
        if op == "update"
        else (rec["after_images"],) if op == "insert" else (rec["before_images"],)
    )
    reverted_by = principal_from_legacy(agent, kind=PrincipalKind.AGENT.value).to_dict()

    tr = conn.transaction()
    await tr.start()
    try:
        tag = await conn.execute(sql, *params)
        restored = _tag_count(tag)
        if restored != expected:
            # Some rows changed since the write -> all-or-nothing: undo nothing.
            await tr.rollback()
            return RevertResult(
                False,
                action_id,
                operation=op,
                conflict=True,
                error=(
                    f"conflict: {expected - restored} of {expected} affected rows "
                    "changed since the write; manual resolution required"
                ),
            )
        await store.mark_reverted(conn, action_id, reverted_by)
        await tr.commit()
    except Exception as exc:
        await _rollback_quietly(tr)
        return RevertResult(
            False, action_id, operation=op, error=f"{type(exc).__name__}: {exc}"
        )

    return RevertResult(True, action_id, operation=op, rows_restored=restored)
