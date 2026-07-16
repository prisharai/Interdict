"""Production startup checks for Interdict's privilege boundary.

These checks run once, before MCP starts.  They intentionally fail closed: a
guardrail connected as a database owner or superuser is only pretending to be a
boundary.  Development mode can relax deployment topology, but never the
reserved-schema rule enforced by :mod:`engine.policy`.
"""

from __future__ import annotations

from dataclasses import dataclass

from engine.policy import RESERVED_SCHEMAS, Policy

DEVELOPMENT = "development"
PRODUCTION = "production"
VALID_PROFILES = frozenset({DEVELOPMENT, PRODUCTION})


@dataclass(frozen=True)
class DatabaseSecurityInfo:
    server_addr: str | None
    server_port: int | None
    database: str
    role: str
    superuser: bool
    create_role: bool
    create_db: bool
    replication: bool
    bypass_rls: bool
    database_owner: bool
    accessible_reserved_schemas: tuple[str, ...] = ()
    owned_relations: tuple[str, ...] = ()
    creatable_schemas: tuple[str, ...] = ()
    dangerous_table_privileges: tuple[str, ...] = ()

    @property
    def identity(self) -> tuple[str | None, int | None, str]:
        return (self.server_addr, self.server_port, self.database)


async def inspect_database_security(conn) -> DatabaseSecurityInfo:
    """Read role and database facts using ordinary Postgres catalog views."""
    row = await conn.fetchrow(
        """
        SELECT inet_server_addr()::text AS server_addr,
               inet_server_port() AS server_port,
               current_database() AS database,
               current_user AS role,
               r.rolsuper AS superuser,
               r.rolcreaterole AS create_role,
               r.rolcreatedb AS create_db,
               r.rolreplication AS replication,
               r.rolbypassrls AS bypass_rls,
               d.datdba = r.oid AS database_owner
        FROM pg_roles r
        JOIN pg_database d ON d.datname = current_database()
        WHERE r.rolname = current_user
        """
    )
    reserved = await conn.fetch(
        """
        SELECT n.nspname
        FROM pg_namespace n
        WHERE n.nspname = ANY($1::text[])
          AND (has_schema_privilege(current_user, n.oid, 'USAGE')
               OR has_schema_privilege(current_user, n.oid, 'CREATE'))
        ORDER BY n.nspname
        """,
        sorted(RESERVED_SCHEMAS),
    )
    owned = await conn.fetch(
        """
        SELECT n.nspname || '.' || c.relname AS relation
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        JOIN pg_roles r ON r.oid = c.relowner
        WHERE r.rolname = current_user
          AND c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f')
          AND n.nspname NOT LIKE 'pg_%'
          AND n.nspname <> 'information_schema'
        ORDER BY 1
        LIMIT 20
        """
    )
    creatable = await conn.fetch(
        """
        SELECT nspname
        FROM pg_namespace
        WHERE nspname NOT LIKE 'pg_%'
          AND nspname <> 'information_schema'
          AND has_schema_privilege(current_user, oid, 'CREATE')
        ORDER BY nspname
        """
    )
    dangerous = await conn.fetch(
        """
        SELECT table_schema || '.' || table_name || ':' || privilege_type AS item
        FROM information_schema.role_table_grants
        WHERE grantee = current_user
          AND privilege_type IN ('TRUNCATE', 'REFERENCES', 'TRIGGER')
          AND table_schema <> 'information_schema'
          AND table_schema NOT LIKE 'pg_%'
        ORDER BY 1
        LIMIT 20
        """
    )
    return DatabaseSecurityInfo(
        server_addr=row["server_addr"],
        server_port=row["server_port"],
        database=row["database"],
        role=row["role"],
        superuser=row["superuser"],
        create_role=row["create_role"],
        create_db=row["create_db"],
        replication=row["replication"],
        bypass_rls=row["bypass_rls"],
        database_owner=row["database_owner"],
        accessible_reserved_schemas=tuple(r["nspname"] for r in reserved),
        owned_relations=tuple(r["relation"] for r in owned),
        creatable_schemas=tuple(r["nspname"] for r in creatable),
        dangerous_table_privileges=tuple(r["item"] for r in dangerous),
    )


def production_errors(
    *,
    policy: Policy,
    application: DatabaseSecurityInfo,
    control: DatabaseSecurityInfo | None,
    operator_token: str | None,
    operator_id: str | None,
    min_token_length: int,
) -> list[str]:
    """Return every unsafe production condition, so setup is fixable at once."""
    errors: list[str] = []
    elevated: list[str] = []
    for enabled, label in (
        (application.superuser, "SUPERUSER"),
        (application.database_owner, "database owner"),
        (application.create_role, "CREATEROLE"),
        (application.create_db, "CREATEDB"),
        (application.replication, "REPLICATION"),
        (application.bypass_rls, "BYPASSRLS"),
    ):
        if enabled:
            elevated.append(label)
    if elevated:
        errors.append(
            f"application role {application.role!r} is overpowered: "
            + ", ".join(elevated)
        )
    if application.accessible_reserved_schemas:
        errors.append(
            f"application role {application.role!r} can access reserved schema(s): "
            + ", ".join(application.accessible_reserved_schemas)
        )
    if application.owned_relations:
        errors.append(
            f"application role {application.role!r} owns relation(s): "
            + ", ".join(application.owned_relations)
        )
    if application.creatable_schemas:
        errors.append(
            f"application role {application.role!r} can CREATE in schema(s): "
            + ", ".join(application.creatable_schemas)
        )
    if application.dangerous_table_privileges:
        errors.append(
            f"application role {application.role!r} has dangerous table "
            "privilege(s): " + ", ".join(application.dangerous_table_privileges)
        )
    if control is None:
        errors.append("AGENT_CONTROL_DSN is required in production")
    else:
        if control.identity == application.identity:
            errors.append("control storage must use a different Postgres database")
        if control.role == application.role:
            errors.append("control storage must use a different database role")
        control_elevated = [
            label
            for enabled, label in (
                (control.superuser, "SUPERUSER"),
                (control.create_role, "CREATEROLE"),
                (control.create_db, "CREATEDB"),
                (control.replication, "REPLICATION"),
                (control.bypass_rls, "BYPASSRLS"),
            )
            if enabled
        ]
        if control_elevated:
            errors.append(
                f"control role {control.role!r} is overpowered: "
                + ", ".join(control_elevated)
            )
    if policy.mode == "observe":
        errors.append("observe mode is not permitted in production")
    if policy.allow_multi_statement:
        errors.append("multi-statement SQL is not permitted in production")
    if not policy.block_system_catalog:
        errors.append("production policy must block system-catalog access")
    if not policy.require_where_on_writes:
        errors.append("production policy must require WHERE on UPDATE/DELETE")
    if not policy.block_locking:
        errors.append("production policy must block locking reads")
    if policy.ddl_allowed_tables is None or policy.ddl_allowed_tables:
        errors.append("production policy must deny all DDL")
    if policy.allowed_tables is None:
        errors.append("production policy must define a tables.allow allowlist")
    elif not policy.require_qualified_tables:
        errors.append("production policy must set require_qualified_tables: true")
    if not operator_token or len(operator_token) < min_token_length:
        errors.append(
            f"AGENT_OPERATOR_TOKEN must contain at least {min_token_length} characters"
        )
    if not operator_id:
        errors.append("AGENT_OPERATOR_ID is required in production")
    return errors


def validate_profile(value: str) -> str:
    if value not in VALID_PROFILES:
        allowed = ", ".join(sorted(VALID_PROFILES))
        raise ValueError(f"invalid AGENT_SAFETY_PROFILE {value!r}; use {allowed}")
    return value
