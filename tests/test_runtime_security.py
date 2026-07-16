from engine.policy import Policy
from engine.runtime_security import DatabaseSecurityInfo, production_errors


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
    }
    values.update(overrides)
    return DatabaseSecurityInfo(**values)


def _errors(application=None, control=None, policy=None):
    return production_errors(
        policy=policy
        or Policy(
            allowed_tables=frozenset({"accounts"}),
            require_qualified_tables=True,
        ),
        application=application or _info(),
        control=control or _info(database="control", role="interdict_control_writer"),
        operator_token="x" * 32,
        operator_id="alice",
        min_token_length=32,
    )


def test_safe_production_topology_passes():
    assert _errors() == []


def test_overpowered_application_role_is_rejected():
    errors = _errors(application=_info(superuser=True, bypass_rls=True))
    assert any("SUPERUSER" in error and "BYPASSRLS" in error for error in errors)


def test_overpowered_control_role_is_rejected():
    errors = _errors(
        control=_info(database="control", role="control", create_role=True)
    )
    assert any("control role" in error and "CREATEROLE" in error for error in errors)


def test_application_role_must_not_access_reserved_schema():
    errors = _errors(application=_info(accessible_reserved_schemas=("adb_undo",)))
    assert any("reserved schema" in error for error in errors)


def test_application_role_must_not_own_objects_or_hold_ddl_privileges():
    errors = _errors(
        application=_info(
            owned_relations=("public.accounts",),
            creatable_schemas=("public",),
            dangerous_table_privileges=("public.accounts:TRUNCATE",),
        )
    )
    assert any("owns relation" in error for error in errors)
    assert any("can CREATE" in error for error in errors)
    assert any("TRUNCATE" in error for error in errors)


def test_control_store_must_be_separate():
    errors = _errors(control=_info(role="interdict_app"))
    assert any("different Postgres database" in error for error in errors)
    assert any("different database role" in error for error in errors)


def test_production_requires_schema_qualified_allowlist():
    errors = _errors(policy=Policy(allowed_tables=frozenset({"accounts"})))
    assert any("require_qualified_tables" in error for error in errors)


def test_production_rejects_weakened_structural_policy():
    errors = _errors(
        policy=Policy(
            allowed_tables=frozenset({"public.accounts"}),
            require_qualified_tables=True,
            allow_multi_statement=True,
            block_system_catalog=False,
            require_where_on_writes=False,
            block_locking=False,
            ddl_allowed_tables=frozenset({"public.accounts"}),
        )
    )
    assert any("multi-statement" in error for error in errors)
    assert any("system-catalog" in error for error in errors)
    assert any("require WHERE" in error for error in errors)
    assert any("locking reads" in error for error in errors)
    assert any("deny all DDL" in error for error in errors)
