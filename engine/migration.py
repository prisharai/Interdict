"""Idempotent migration of legacy in-database control records.

Nothing is deleted from the application database.  Operators verify the copy,
revoke the application role's legacy-schema access, and archive/drop the old
schema themselves.
"""

from __future__ import annotations

from engine.approvals import ApprovalStore
from engine.undo import UndoConfig, UndoStore

LEGACY_SCHEMA = "adb_undo"


async def migrate_legacy_control(
    application_conn, control_conn, *, destination_schema: str
) -> dict[str, int]:
    approvals = ApprovalStore(destination_schema)
    undo = UndoStore(UndoConfig(enabled=True), schema=destination_schema)
    await approvals.ensure_schema(control_conn)
    await undo.ensure_schema(control_conn)
    copied = {"approvals": 0, "undo_records": 0}

    if await application_conn.fetchval(
        "SELECT to_regclass('adb_undo.pending_approval')"
    ):
        rows = await application_conn.fetch(
            "SELECT * FROM adb_undo.pending_approval ORDER BY created_at"
        )
        for raw in rows:
            row = dict(raw)
            result = await control_conn.execute(
                f"""INSERT INTO "{destination_schema}".pending_approval
                    (approval_id, created_at, sql, stated_task, agent, principal,
                     simulation, token_hash, status, decided_by, decided_at,
                     executed_at, sql_sha256, policy_sha256, approved_rows,
                     claimed_at, failure_reason, action_kind, undo_action_id,
                     object_fingerprint)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,
                            $16,$17,$18,$19,$20)
                    ON CONFLICT (approval_id) DO NOTHING""",
                row["approval_id"],
                row["created_at"],
                row["sql"],
                row.get("stated_task"),
                row.get("agent"),
                row.get("principal"),
                row.get("simulation"),
                row.get("token_hash"),
                row.get("status", "pending"),
                row.get("decided_by"),
                row.get("decided_at"),
                row.get("executed_at"),
                row.get("sql_sha256"),
                row.get("policy_sha256"),
                row.get("approved_rows"),
                row.get("claimed_at"),
                row.get("failure_reason"),
                row.get("action_kind", "sql"),
                row.get("undo_action_id"),
                row.get("object_fingerprint"),
            )
            copied["approvals"] += result == "INSERT 0 1"

    if await application_conn.fetchval("SELECT to_regclass('adb_undo.undo_log')"):
        rows = await application_conn.fetch(
            "SELECT * FROM adb_undo.undo_log ORDER BY created_at"
        )
        for raw in rows:
            row = dict(raw)
            result = await control_conn.execute(
                f"""INSERT INTO "{destination_schema}".undo_log
                    (action_id, created_at, agent, stated_task, principal,
                     target_table, operation, pk_columns, row_count, before_images,
                     after_images, status, reverted_at, reverted_by, failure_reason)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)
                    ON CONFLICT (action_id) DO NOTHING""",
                row["action_id"],
                row["created_at"],
                row.get("agent"),
                row.get("stated_task"),
                row.get("principal"),
                row["target_table"],
                row["operation"],
                row["pk_columns"],
                row["row_count"],
                row["before_images"],
                row.get("after_images", []),
                (
                    "active"
                    if row.get("status") == "prepared"
                    else row.get("status", "active")
                ),
                row.get("reverted_at"),
                row.get("reverted_by"),
                row.get("failure_reason"),
            )
            copied["undo_records"] += result == "INSERT 0 1"

    return copied
