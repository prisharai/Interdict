"""Stable database-object identity for approval time-of-check/time-of-use checks."""

from __future__ import annotations

import hashlib
import json
from typing import Any


async def load_object_fingerprint(conn, table_names) -> dict[str, Any]:
    """Resolve table names to Postgres OIDs plus a compact column signature.

    A table's OID changes when a relation is dropped and recreated, even when
    the replacement has the same name.  Binding an approval to this value
    prevents an old approval from being replayed against a swapped object.
    """
    requested = sorted({str(name) for name in table_names if name})
    objects: list[dict[str, Any]] = []
    for name in requested:
        row = await conn.fetchrow(
            """SELECT c.oid::bigint AS oid, n.nspname AS schema_name,
                      c.relname, c.relkind
                 FROM pg_catalog.pg_class AS c
                 JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
                WHERE c.oid = pg_catalog.to_regclass($1)""",
            name,
        )
        if row is None:
            objects.append({"requested": name, "missing": True})
            continue
        columns = await conn.fetch(
            """SELECT attnum, attname, atttypid::bigint AS type_oid,
                      atttypmod, attnotnull
                 FROM pg_catalog.pg_attribute
                WHERE attrelid = $1::oid AND attnum > 0 AND NOT attisdropped
                ORDER BY attnum""",
            row["oid"],
        )
        column_shape = [dict(column) for column in columns]
        shape_hash = hashlib.sha256(
            json.dumps(column_shape, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        relation_kind = row["relkind"]
        if isinstance(relation_kind, bytes):
            relation_kind = relation_kind.decode("ascii")
        objects.append(
            {
                "requested": name,
                "oid": row["oid"],
                "schema": row["schema_name"],
                "relation": row["relname"],
                "kind": relation_kind,
                "column_shape_sha256": shape_hash,
            }
        )
    return {"version": 1, "objects": objects}


def classification_tables(classification) -> tuple[str, ...]:
    """Return every real relation named by a parsed SQL classification."""
    return tuple(
        sorted(
            {
                table
                for statement in classification.statements
                for table in statement.tables
            }
        )
    )
