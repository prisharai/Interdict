"""Real-Postgres proof that security records live outside the application DB."""

import os
import tempfile
from uuid import uuid4

import asyncpg
import pytest

from adapters.mcp_server import ShadowSession
from engine.approvals import ApprovalStore, token_hash
from engine.audit import AuditLog
from engine.migration import migrate_legacy_control
from engine.policy import Policy
from engine.simulate import SimulationConfig, load_unique_columns
from engine.undo import UndoConfig, UndoStore

APP_DSN = os.environ.get(
    "AGENT_DB_DSN", "postgresql://postgres:postgres@localhost:5433/pagila"
)
CONTROL_DSN = "postgresql://postgres:postgres@localhost:5433/postgres"
TOKEN = "control-store-test-token-that-is-long-enough"
SCHEMA = "interdict_control_external_test"
MIGRATION_SCHEMA = "interdict_control_migration_test"


async def test_undo_and_approvals_use_separate_control_database():
    try:
        app_pool = await asyncpg.create_pool(APP_DSN, min_size=1, max_size=4)
        control_pool = await asyncpg.create_pool(CONTROL_DSN, min_size=1, max_size=4)
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.skip(f"local Postgres is unavailable ({exc})")

    audit = AuditLog(
        tempfile.mktemp(), control_pool=control_pool, control_schema=SCHEMA
    )
    cfg = UndoConfig(enabled=True, schema=SCHEMA)
    undo = UndoStore(cfg, schema=SCHEMA)
    approvals = ApprovalStore(SCHEMA)
    try:
        async with app_pool.acquire() as app:
            await app.execute("DROP TABLE IF EXISTS _external_control_test")
            await app.execute(
                "CREATE TABLE _external_control_test (id int primary key, value text)"
            )
            unique = await load_unique_columns(app)
        async with control_pool.acquire() as control:
            await control.execute(f'DROP SCHEMA IF EXISTS "{SCHEMA}" CASCADE')
            await undo.ensure_schema(control)
            await approvals.ensure_schema(control)
        await audit.start()

        session = ShadowSession(
            app_pool,
            audit,
            Policy(
                allowed_tables=None,
                simulation=SimulationConfig(enabled=True),
                undo=cfg,
            ),
            undo,
            unique_columns=unique,
            operator_token=TOKEN,
            approval_store=approvals,
            control_pool=control_pool,
        )
        result = await session.run_query(
            "INSERT INTO _external_control_test VALUES (1, 'created')",
            agent="agent-1",
        )
        assert result["reversible"] is True

        async with app_pool.acquire() as app:
            assert (
                await app.fetchval("SELECT to_regclass($1)", f"{SCHEMA}.undo_log")
                is None
            )
        async with control_pool.acquire() as control:
            record = await undo.get(control, result["undo_action_id"])
            assert record["status"] == "active"

        requested = await session.request_revert(
            result["undo_action_id"], agent="agent-1"
        )
        async with control_pool.acquire() as control:
            assert (
                await approvals.decide(
                    control,
                    requested["approval_id"],
                    approve=True,
                    operator_token_hash=token_hash(TOKEN),
                    decided_by="human-1",
                )
                == "approved"
            )
        reverted = await session.run_approved_revert(requested["approval_id"])
        assert reverted["ok"] is True
        await audit.stop()
        async with app_pool.acquire() as app:
            assert (
                await app.fetchval("SELECT count(*) FROM _external_control_test") == 0
            )
            assert (
                await app.fetchval("SELECT to_regclass($1)", f"{SCHEMA}.audit_event")
                is None
            )
        async with control_pool.acquire() as control:
            assert (
                await control.fetchval(f'SELECT count(*) FROM "{SCHEMA}".audit_event')
                > 0
            )
    finally:
        async with app_pool.acquire() as app:
            await app.execute("DROP TABLE IF EXISTS _external_control_test")
        async with control_pool.acquire() as control:
            await control.execute(f'DROP SCHEMA IF EXISTS "{SCHEMA}" CASCADE')
        await audit.stop()
        await app_pool.close()
        await control_pool.close()


async def test_legacy_control_migration_is_copy_only_and_idempotent():
    try:
        app = await asyncpg.connect(APP_DSN)
        control = await asyncpg.connect(CONTROL_DSN)
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.skip(f"local Postgres is unavailable ({exc})")

    approval_id = str(uuid4())
    action_id = str(uuid4())
    legacy_approvals = ApprovalStore("adb_undo")
    legacy_undo = UndoStore(UndoConfig(enabled=True))
    try:
        await legacy_approvals.ensure_schema(app)
        await legacy_undo.ensure_schema(app)
        await legacy_approvals.create(
            app,
            approval_id=approval_id,
            sql="DELETE FROM public.example WHERE id = 1",
            stated_task="migration proof",
            agent="agent-1",
            principal={"id": "agent-1", "kind": "agent"},
            simulation={"affected_rows": 1},
            operator_token_hash="test-hash",
        )
        await legacy_undo.record(
            app,
            action_id=action_id,
            agent="agent-1",
            stated_task="migration proof",
            principal={"id": "agent-1", "kind": "agent"},
            target_table="public.example",
            operation="delete",
            pk_columns=["id"],
            row_count=1,
            before_images='[{"id": 1}]',
            after_images="[]",
            status="prepared",
        )
        await control.execute(f'DROP SCHEMA IF EXISTS "{MIGRATION_SCHEMA}" CASCADE')

        first = await migrate_legacy_control(
            app, control, destination_schema=MIGRATION_SCHEMA
        )
        second = await migrate_legacy_control(
            app, control, destination_schema=MIGRATION_SCHEMA
        )

        assert first["approvals"] >= 1
        assert first["undo_records"] >= 1
        assert second == {"approvals": 0, "undo_records": 0}
        assert (
            await app.fetchval(
                "SELECT count(*) FROM adb_undo.pending_approval "
                "WHERE approval_id=$1",
                approval_id,
            )
            == 1
        )
        assert (
            await app.fetchval(
                "SELECT count(*) FROM adb_undo.undo_log WHERE action_id=$1",
                action_id,
            )
            == 1
        )
        copied_principal = await control.fetchval(
            f'SELECT principal::text FROM "{MIGRATION_SCHEMA}".undo_log '
            "WHERE action_id=$1",
            action_id,
        )
        assert "agent-1" in copied_principal
        copied_status = await control.fetchval(
            f'SELECT status FROM "{MIGRATION_SCHEMA}".undo_log WHERE action_id=$1',
            action_id,
        )
        assert copied_status == "active"
    finally:
        await app.execute(
            "DELETE FROM adb_undo.pending_approval WHERE approval_id=$1", approval_id
        )
        await app.execute("DELETE FROM adb_undo.undo_log WHERE action_id=$1", action_id)
        await control.execute(f'DROP SCHEMA IF EXISTS "{MIGRATION_SCHEMA}" CASCADE')
        await app.close()
        await control.close()
