import json
import stat
from pathlib import Path

from adapters import mcp_server
from adapters.profile_cli import client_command, connect_cli, setup_cli
from engine.profiles import (
    ProfileStore,
    SecretStore,
    build_policy,
    new_profile,
    write_policy,
)
from engine.runtime_security import DatabaseSecurityInfo


def _info(**overrides):
    values = {
        "server_addr": "10.0.0.1",
        "server_port": 5432,
        "database": "application",
        "role": "interdict_app",
        "superuser": False,
        "create_role": False,
        "create_db": False,
        "replication": False,
        "bypass_rls": False,
        "database_owner": False,
        "accessible_reserved_schemas": (),
        "owned_relations": (),
        "creatable_schemas": (),
        "dangerous_table_privileges": (),
    }
    values.update(overrides)
    return DatabaseSecurityInfo(**values)


def _store(tmp_path, monkeypatch):
    monkeypatch.setenv("INTERDICT_DISABLE_KEYRING", "1")
    return ProfileStore(tmp_path, SecretStore(tmp_path))


def _save_ready(store: ProfileStore, name: str = "production"):
    policy_path = store.policy_path(name)
    write_policy(
        policy_path,
        build_policy(
            preset="production",
            tables=["public.orders"],
            approval_required=True,
        ),
    )
    profile = new_profile(
        name=name,
        database_label="app-prod",
        database_name="app",
        database_role="interdict_app",
        safety_profile="production",
        policy_path=policy_path,
        audit_log_path=store.audit_path(name),
        operator_id="alice@example.com",
        control_mode="external",
        approval_required=True,
        status="ready",
        safety_preset="production",
    )
    store.save(
        profile,
        db_dsn="postgresql://app:secret@app/app",
        control_dsn="postgresql://control:other@control/control",
        operator_token="x" * 48,
    )
    return profile


def test_profile_json_contains_no_credentials_and_fallback_is_owner_only(
    tmp_path, monkeypatch
):
    store = _store(tmp_path, monkeypatch)
    _save_ready(store)

    public_text = store.profiles_path.read_text()
    secret_text = store.secrets.fallback_path.read_text()

    assert "postgresql://" not in public_text
    assert "secret" not in public_text
    assert "postgresql://" in secret_text
    assert stat.S_IMODE(store.secrets.fallback_path.stat().st_mode) == 0o600
    assert store.active_name() == "production"


def test_runtime_environment_resolves_secrets_without_exposing_them_in_profile(
    tmp_path, monkeypatch
):
    store = _store(tmp_path, monkeypatch)
    _save_ready(store)

    runtime = store.runtime_environment("production")

    assert runtime["AGENT_DB_DSN"].endswith("@app/app")
    assert runtime["AGENT_CONTROL_DSN"].endswith("@control/control")
    assert runtime["AGENT_OPERATOR_TOKEN"] == "x" * 48
    assert runtime["AGENT_POLICY"].endswith("production.yaml")


def test_generated_production_policy_is_qualified_and_deny_ddl():
    policy = build_policy(
        preset="production",
        tables=["public.orders", "billing.invoices"],
        approval_required=True,
    )

    assert policy["require_qualified_tables"] is True
    assert policy["ddl"]["allow"] == []
    assert policy["tables"]["allow"] == ["billing.invoices", "public.orders"]
    assert policy["simulation"]["confirm_over_rows"] == 1000


def test_setup_wizard_discovers_tables_and_creates_ready_profile(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)

    async def inspect_app(_dsn):
        return _info(), ["public.customers", "public.orders"]

    async def inspect_control(_dsn):
        return _info(
            server_addr="10.0.0.2",
            database="control",
            role="interdict_control",
        )

    monkeypatch.setattr("adapters.profile_cli._inspect_connection", inspect_app)
    monkeypatch.setattr("adapters.profile_cli._inspect_control", inspect_control)
    passwords = iter(
        [
            "postgresql://app:secret@app/app",
            "postgresql://control:secret@control/control",
        ]
    )
    answers = iter(["1", "all", "1", "", "alice@example.com", "app-prod"])
    monkeypatch.setattr(
        "adapters.profile_cli.getpass.getpass", lambda _p: next(passwords)
    )
    monkeypatch.setattr("builtins.input", lambda _p: next(answers))

    result = setup_cli(["--name", "production"], store)

    assert result == 0
    profile = store.get("production")
    assert profile.status == "ready"
    assert profile.database_label == "app-prod"
    assert profile.database_role == "interdict_app"
    assert profile.safety_preset == "production"
    assert "public.orders" in Path(profile.policy_path).read_text()


def test_client_commands_contain_profile_name_not_database_credentials(monkeypatch):
    monkeypatch.setattr(
        "adapters.profile_cli.shutil.which", lambda name: f"/bin/{name}"
    )

    command = client_command("claude", "interdict-production", "production")

    rendered = " ".join(command)
    assert "--profile production" in rendered
    assert "postgresql://" not in rendered
    assert "AGENT_OPERATOR_TOKEN" not in rendered


def test_connect_dry_run_does_not_modify_external_client(tmp_path, monkeypatch, capsys):
    store = _store(tmp_path, monkeypatch)
    _save_ready(store)
    monkeypatch.setattr(
        "adapters.profile_cli.shutil.which", lambda name: f"/bin/{name}"
    )

    result = connect_cli(["claude", "--profile", "production", "--dry-run"], store)

    assert result == 0
    output = capsys.readouterr().out
    assert "--profile production" in output
    assert "postgresql://" not in output
    assert "Status: ready" in output


def test_activate_profile_updates_mcp_runtime_from_selected_profile(
    tmp_path, monkeypatch
):
    store = _store(tmp_path, monkeypatch)
    _save_ready(store)
    monkeypatch.setenv("INTERDICT_CONFIG_DIR", str(tmp_path))

    original = {
        name: getattr(mcp_server, name)
        for name in (
            "ACTIVE_PROFILE_NAME",
            "DB_DSN",
            "CONTROL_DSN",
            "SAFETY_PROFILE",
            "OPERATOR_TOKEN",
            "OPERATOR_ID",
            "POLICY_PATH",
            "AUDIT_LOG_PATH",
        )
    }
    try:
        mcp_server._activate_profile("production")

        assert mcp_server.ACTIVE_PROFILE_NAME == "production"
        assert mcp_server.DB_DSN.endswith("@app/app")
        assert mcp_server.CONTROL_DSN.endswith("@control/control")
        assert mcp_server.SAFETY_PROFILE == "production"
    finally:
        for name, value in original.items():
            setattr(mcp_server, name, value)


def test_profiles_file_is_valid_json(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    _save_ready(store)
    assert "production" in json.loads(store.profiles_path.read_text())
