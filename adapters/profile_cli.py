"""Human-facing setup, profile, and MCP-client connection commands."""

from __future__ import annotations

import asyncio
import getpass
import json
import os
import secrets
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import asyncpg

from engine.approvals import ApprovalStore
from engine.policy import Policy
from engine.profiles import (
    ProfileStore,
    build_policy,
    new_profile,
    validate_profile_name,
    write_policy,
)
from engine.runtime_security import (
    PRODUCTION,
    inspect_database_security,
    production_errors,
)
from engine.undo import UndoConfig, UndoStore


def _prompt(message: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{message}{suffix}: ").strip()
    return value or (default or "")


def _yes_no(message: str, *, default: bool) -> bool:
    label = "Y/n" if default else "y/N"
    value = input(f"{message} [{label}]: ").strip().lower()
    if not value:
        return default
    return value in {"y", "yes"}


def _choice(message: str, options: list[str], *, default: int = 1) -> int:
    print(message)
    for index, option in enumerate(options, 1):
        marker = " (recommended)" if index == default else ""
        print(f"  {index}. {option}{marker}")
    while True:
        raw = _prompt("Choose", str(default))
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return int(raw)
        print(f"Enter a number from 1 to {len(options)}.")


def _extract_name(args: list[str], default: str | None = None) -> str | None:
    if "--name" in args:
        index = args.index("--name")
        if index + 1 >= len(args):
            raise ValueError("--name requires a profile name")
        return args[index + 1]
    if "--profile" in args:
        index = args.index("--profile")
        if index + 1 >= len(args):
            raise ValueError("--profile requires a profile name")
        return args[index + 1]
    return default


async def _inspect_connection(dsn: str):
    conn = await asyncpg.connect(dsn=dsn, timeout=8)
    try:
        info = await inspect_database_security(conn)
        rows = await conn.fetch(
            """SELECT n.nspname || '.' || c.relname AS name
                 FROM pg_catalog.pg_class c
                 JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
                WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f')
                  AND n.nspname NOT LIKE 'pg_%'
                  AND n.nspname <> 'information_schema'
                ORDER BY 1"""
        )
        return info, [row["name"] for row in rows]
    finally:
        await conn.close()


async def _inspect_control(dsn: str):
    conn = await asyncpg.connect(dsn=dsn, timeout=8)
    try:
        info = await inspect_database_security(conn)
        approvals = ApprovalStore("interdict_control")
        undo = UndoStore(UndoConfig(enabled=True), schema="interdict_control")
        await approvals.ensure_schema(conn)
        await undo.ensure_schema(conn)
        return info
    finally:
        await conn.close()


def _select_tables(discovered: list[str]) -> list[str]:
    print(f"\nDiscovered {len(discovered)} table/view(s):")
    for table in discovered[:50]:
        print(f"  - {table}")
    if len(discovered) > 50:
        print(f"  ... and {len(discovered) - 50} more")
    print("\nEnter 'all' or a comma-separated list of schema.table names.")
    while True:
        raw = _prompt("Tables the agent may access", "all")
        selected = (
            discovered
            if raw.lower() == "all"
            else [item.strip() for item in raw.split(",") if item.strip()]
        )
        unknown = sorted(set(selected) - set(discovered))
        if unknown:
            print("Not discovered: " + ", ".join(unknown))
            continue
        if not selected:
            print("Choose at least one table.")
            continue
        return selected


def _setup_sql(tables: list[str]) -> str:
    def ident(value: str) -> str:
        return '"' + value.replace('"', '""') + '"'

    schemas = sorted({table.split(".", 1)[0] for table in tables})
    lines = [
        "-- Review with your DBA. Interdict does not execute this file.",
        "\\prompt 'Password for interdict_app: ' interdict_app_password",
        "CREATE ROLE interdict_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE",
        "  NOREPLICATION NOBYPASSRLS PASSWORD :'interdict_app_password';",
    ]
    for schema in schemas:
        lines.extend(
            [
                f"REVOKE CREATE ON SCHEMA {ident(schema)} FROM PUBLIC;",
                f"GRANT USAGE ON SCHEMA {ident(schema)} TO interdict_app;",
            ]
        )
    for table in tables:
        schema, relation = table.split(".", 1)
        lines.append(
            "GRANT SELECT, INSERT, UPDATE, DELETE ON "
            f"{ident(schema)}.{ident(relation)} TO interdict_app;"
        )
    lines.append("-- Re-run `interdict setup --name PROFILE` with the new role DSN.")
    return "\n".join(lines) + "\n"


def setup_cli(args: list[str], store: ProfileStore | None = None) -> int:
    store = store or ProfileStore()
    try:
        requested_name = _extract_name(args)
        name = validate_profile_name(
            requested_name
            or _prompt("What should we call this connection?", "production")
        )
        print("\nInterdict Setup")
        print("Secrets will never be printed or placed in agent configuration.\n")
        db_dsn = getpass.getpass("PostgreSQL connection URL to protect: ").strip()
        if not db_dsn:
            print("A PostgreSQL connection URL is required.", file=sys.stderr)
            return 2
        print("Testing the application database connection...")
        application, discovered = asyncio.run(_inspect_connection(db_dsn))
        print(
            f"Connected to database {application.database!r} as "
            f"role {application.role!r}."
        )

        control_choice = _choice(
            "\nWhere should approvals, undo evidence, and audit records live?",
            [
                "A separate PostgreSQL database I provide",
                "Interdict-managed control store (coming soon)",
                "The same database (local development only)",
            ],
            default=1,
        )
        if control_choice == 2:
            print(
                "The managed control store is not generally available yet. "
                "Choose a separate database or local development mode."
            )
            return 2
        control_mode = "external" if control_choice == 1 else "local"
        control_dsn = (
            getpass.getpass("Separate control database URL: ").strip()
            if control_mode == "external"
            else db_dsn
        )
        if not control_dsn:
            print("A control database URL is required.", file=sys.stderr)
            return 2
        print("Testing and initializing the control store...")
        control = asyncio.run(_inspect_control(control_dsn))

        tables = _select_tables(discovered)
        preset_index = _choice(
            "\nChoose a safety preset:",
            ["Production", "Read-only", "Development"],
            default=1,
        )
        preset = {1: "production", 2: "read-only", 3: "development"}[preset_index]
        if preset == "production" and control_mode != "external":
            print(
                "Production requires a separate control database. Run setup "
                "again and choose option 1.",
                file=sys.stderr,
            )
            return 2
        approval_required = _yes_no(
            "Should large writes require human approval?", default=True
        )
        operator_id = _prompt(
            "Human operator identity (email or team handle)",
            os.environ.get("USER", "operator"),
        )
        operator_token = secrets.token_urlsafe(48) if approval_required else None

        policy_data = build_policy(
            preset=preset,
            tables=tables,
            approval_required=approval_required,
        )
        policy_path = store.policy_path(name)
        write_policy(policy_path, policy_data)
        policy = Policy.from_dict(policy_data)
        safety_profile = PRODUCTION if preset == "production" else "development"
        errors = (
            production_errors(
                policy=policy,
                application=application,
                control=control,
                operator_token=operator_token,
                operator_id=operator_id,
                min_token_length=32,
            )
            if safety_profile == PRODUCTION
            else []
        )
        status = "ready" if not errors else "needs-dba"
        label = _prompt("Friendly database label", name)
        profile = new_profile(
            name=name,
            database_label=label,
            database_name=application.database,
            database_role=application.role,
            safety_profile=safety_profile,
            policy_path=policy_path,
            audit_log_path=store.audit_path(name),
            operator_id=operator_id,
            control_mode=control_mode,
            approval_required=approval_required,
            status=status,
            safety_preset=preset,
        )
        store.save(
            profile,
            db_dsn=db_dsn,
            control_dsn=control_dsn,
            operator_token=operator_token,
        )

        print("\nProfile saved.")
        print(f"  Profile: {name}")
        print(f"  Database: {application.database}")
        print(f"  Safety preset: {preset}")
        print(f"  Secrets: {store.secrets.backend_name}")
        if errors:
            setup_path = store.setup_sql_path(name)
            setup_path.write_text(_setup_sql(tables), encoding="utf-8")
            setup_path.chmod(0o600)
            print("\nThis database role needs DBA attention:")
            for error in errors:
                print(f"  - {error}")
            print(f"\nReviewable DBA SQL: {setup_path}")
            print("After your DBA applies it, rerun setup with the restricted DSN.")
            return 1
        print("  Status: ready")
        print(f"\nNext: interdict connect claude --profile {name}")
        return 0
    except (OSError, asyncpg.PostgresError, ValueError, KeyError) as exc:
        print(f"interdict setup: {exc}", file=sys.stderr)
        return 1


def profiles_cli(store: ProfileStore | None = None) -> int:
    store = store or ProfileStore()
    profiles = store.list()
    if not profiles:
        print("No profiles yet. Run `interdict setup`.")
        return 0
    active = store.active_name()
    print(f"{'':2} {'NAME':18} {'DATABASE':18} {'SAFETY':12} STATUS")
    for profile in profiles:
        marker = "*" if profile.name == active else " "
        preset = profile.safety_preset or profile.safety_profile
        print(
            f"{marker:2} {profile.name[:18]:18} {profile.database_label[:18]:18} "
            f"{preset[:12]:12} {profile.status}"
        )
    print("\n* active human-selected profile")
    return 0


def profile_cli(args: list[str], store: ProfileStore | None = None) -> int:
    store = store or ProfileStore()
    if len(args) != 2 or args[0] != "use":
        print("Usage: interdict profile use <name>", file=sys.stderr)
        return 2
    try:
        store.set_active(args[1])
    except (KeyError, ValueError) as exc:
        print(f"interdict profile: {exc}", file=sys.stderr)
        return 1
    print(f"Active profile is now {args[1]!r}.")
    print("Agents cannot switch this selection through an MCP tool.")
    return 0


def client_command(client: str, server_name: str, profile_name: str) -> list[str]:
    interdict = shutil.which("interdict") or "interdict"
    if client == "claude":
        return [
            "claude",
            "mcp",
            "add",
            server_name,
            "--scope",
            "user",
            "--",
            interdict,
            "--profile",
            profile_name,
        ]
    if client == "codex":
        return [
            "codex",
            "mcp",
            "add",
            server_name,
            "--",
            interdict,
            "--profile",
            profile_name,
        ]
    raise ValueError(f"automatic connection is not available for {client!r}")


def _mcp_json(profile_name: str) -> dict[str, Any]:
    interdict = shutil.which("interdict") or "interdict"
    return {
        "command": interdict,
        "args": ["--profile", profile_name],
    }


def connect_cli(args: list[str], store: ProfileStore | None = None) -> int:
    store = store or ProfileStore()
    if not args:
        print(
            "Usage: interdict connect <claude|codex|cursor|custom> --profile <name>",
            file=sys.stderr,
        )
        return 2
    client = args[0].lower()
    profile_name = _extract_name(args, store.active_name())
    if not profile_name:
        print("Choose a profile with --profile or `interdict profile use`.")
        return 2
    try:
        profile = store.get(profile_name)
    except (KeyError, ValueError) as exc:
        print(f"interdict connect: {exc}", file=sys.stderr)
        return 1
    if profile.status != "ready":
        print(
            f"Profile {profile_name!r} is {profile.status}; resolve setup/doctor "
            "findings before connecting an agent.",
            file=sys.stderr,
        )
        return 1
    server_name = f"interdict-{profile_name}"
    dry_run = "--dry-run" in args
    if client in {"claude", "codex"}:
        command = client_command(client, server_name, profile_name)
        if dry_run or shutil.which(client) is None:
            print("Run this command:")
            print(" ".join(command))
            if shutil.which(client) is None and not dry_run:
                print(f"\n{client} CLI was not found, so nothing was changed.")
                return 1
        else:
            completed = subprocess.run(command, check=False)
            if completed.returncode != 0:
                return completed.returncode
    elif client == "cursor":
        cursor_dir = Path.cwd() / ".cursor"
        target = cursor_dir / "mcp.json"
        cursor_dir.mkdir(parents=True, exist_ok=True)
        data = json.loads(target.read_text()) if target.exists() else {}
        data.setdefault("mcpServers", {})[server_name] = _mcp_json(profile_name)
        target.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        print(f"Updated {target}")
    elif client == "custom":
        print(json.dumps({server_name: _mcp_json(profile_name)}, indent=2))
    else:
        print(f"Unsupported client {client!r}.", file=sys.stderr)
        return 2

    print("\nInterdict was connected.")
    print(f"  Client: {client}")
    print(f"  Profile: {profile.name}")
    print(f"  Database: {profile.database_label}")
    print(f"  Safety mode: {profile.safety_preset or profile.safety_profile}")
    print(
        "  Approval mode: "
        + ("human required" if profile.approval_required else "policy only")
    )
    print("  Status: ready")
    return 0
