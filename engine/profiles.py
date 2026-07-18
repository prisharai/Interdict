"""Named Interdict connection profiles and secret storage.

Profiles keep ordinary settings in JSON and credentials in the operating-system
keychain when one is available. Headless systems fall back to a separate file
with owner-only permissions. Nothing in this module prints secret values.
"""

from __future__ import annotations

import json
import os
import re
import stat
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

PROFILE_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")
SECRET_FIELDS = ("db_dsn", "control_dsn", "operator_token")
KEYRING_SERVICE = "interdict-db"


def _atomic_owner_write(path: Path, content: str) -> None:
    """Replace a file atomically without ever creating it world-readable."""
    path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}."
    )
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(file_descriptor, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as handle:
            file_descriptor = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    finally:
        if file_descriptor >= 0:
            os.close(file_descriptor)
        temporary_path.unlink(missing_ok=True)


def config_dir() -> Path:
    override = os.environ.get("INTERDICT_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "interdict"


def validate_profile_name(name: str) -> str:
    if not PROFILE_NAME_RE.fullmatch(name):
        raise ValueError(
            "profile names may contain letters, numbers, hyphens, and "
            "underscores (maximum 64 characters)"
        )
    return name


@dataclass(frozen=True)
class ConnectionProfile:
    name: str
    database_label: str
    database_name: str
    database_role: str
    safety_profile: str
    policy_path: str
    audit_log_path: str
    operator_id: str | None
    control_mode: str
    approval_required: bool
    status: str = "ready"
    created_at: str = ""
    safety_preset: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ConnectionProfile:
        return cls(**data)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class SecretStore:
    """Keychain-backed secrets with a locked-file fallback."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or config_dir()
        self.fallback_path = self.root / "secrets.json"
        self._keyring = self._load_keyring()

    @staticmethod
    def _load_keyring():
        if os.environ.get("INTERDICT_DISABLE_KEYRING") == "1":
            return None
        try:
            import keyring

            backend = keyring.get_keyring()
            if getattr(backend, "priority", 0) <= 0:
                return None
            return keyring
        except Exception:
            return None

    @property
    def backend_name(self) -> str:
        return "operating-system keychain" if self._keyring else "locked local file"

    @staticmethod
    def _username(profile_name: str, field: str) -> str:
        return f"{profile_name}:{field}"

    def set(self, profile_name: str, field: str, value: str | None) -> None:
        if field not in SECRET_FIELDS:
            raise ValueError(f"unsupported secret field {field!r}")
        if self._keyring is not None:
            username = self._username(profile_name, field)
            if value:
                self._keyring.set_password(KEYRING_SERVICE, username, value)
            else:
                try:
                    self._keyring.delete_password(KEYRING_SERVICE, username)
                except Exception:
                    pass
            return
        values = self._read_fallback()
        profile_values = values.setdefault(profile_name, {})
        if value:
            profile_values[field] = value
        else:
            profile_values.pop(field, None)
        self._write_fallback(values)

    def get(self, profile_name: str, field: str) -> str | None:
        if field not in SECRET_FIELDS:
            raise ValueError(f"unsupported secret field {field!r}")
        if self._keyring is not None:
            return self._keyring.get_password(
                KEYRING_SERVICE, self._username(profile_name, field)
            )
        return self._read_fallback().get(profile_name, {}).get(field)

    def delete_profile(self, profile_name: str) -> None:
        if self._keyring is not None:
            for field in SECRET_FIELDS:
                self.set(profile_name, field, None)
            return
        values = self._read_fallback()
        values.pop(profile_name, None)
        self._write_fallback(values)

    def _read_fallback(self) -> dict[str, dict[str, str]]:
        if not self.fallback_path.exists():
            return {}
        return json.loads(self.fallback_path.read_text(encoding="utf-8"))

    def _write_fallback(self, values: dict[str, dict[str, str]]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.root.chmod(stat.S_IRWXU)
        _atomic_owner_write(
            self.fallback_path,
            json.dumps(values, indent=2, sort_keys=True) + "\n",
        )


class ProfileStore:
    def __init__(
        self, root: Path | None = None, secret_store: SecretStore | None = None
    ) -> None:
        self.root = root or config_dir()
        self.profiles_path = self.root / "profiles.json"
        self.active_path = self.root / "active-profile"
        self.policy_dir = self.root / "policies"
        self.audit_dir = self.root / "audit"
        self.setup_dir = self.root / "setup"
        self.secrets = secret_store or SecretStore(self.root)

    def save(
        self,
        profile: ConnectionProfile,
        *,
        db_dsn: str,
        control_dsn: str | None,
        operator_token: str | None,
        make_active: bool = True,
    ) -> None:
        validate_profile_name(profile.name)
        values = self._read_profiles()
        values[profile.name] = profile.to_dict()
        self._write_profiles(values)
        self.secrets.set(profile.name, "db_dsn", db_dsn)
        self.secrets.set(profile.name, "control_dsn", control_dsn)
        self.secrets.set(profile.name, "operator_token", operator_token)
        if make_active:
            self.set_active(profile.name)

    def get(self, name: str) -> ConnectionProfile:
        validate_profile_name(name)
        data = self._read_profiles().get(name)
        if data is None:
            raise KeyError(f"unknown Interdict profile {name!r}")
        return ConnectionProfile.from_dict(data)

    def list(self) -> list[ConnectionProfile]:
        return [
            ConnectionProfile.from_dict(data)
            for _, data in sorted(self._read_profiles().items())
        ]

    def active_name(self) -> str | None:
        if not self.active_path.exists():
            return None
        name = self.active_path.read_text(encoding="utf-8").strip()
        return name or None

    def set_active(self, name: str) -> None:
        self.get(name)
        self._ensure_root()
        _atomic_owner_write(self.active_path, name + "\n")

    def runtime_environment(self, name: str) -> dict[str, str]:
        profile = self.get(name)
        db_dsn = self.secrets.get(name, "db_dsn")
        if not db_dsn:
            raise ValueError(f"profile {name!r} has no stored database credential")
        result = {
            "AGENT_DB_DSN": db_dsn,
            "AGENT_SAFETY_PROFILE": profile.safety_profile,
            "AGENT_POLICY": profile.policy_path,
            "AGENT_AUDIT_LOG": profile.audit_log_path,
        }
        control_dsn = self.secrets.get(name, "control_dsn")
        operator_token = self.secrets.get(name, "operator_token")
        if control_dsn:
            result["AGENT_CONTROL_DSN"] = control_dsn
        if operator_token:
            result["AGENT_OPERATOR_TOKEN"] = operator_token
        if profile.operator_id:
            result["AGENT_OPERATOR_ID"] = profile.operator_id
        return result

    def policy_path(self, name: str) -> Path:
        validate_profile_name(name)
        self.policy_dir.mkdir(parents=True, exist_ok=True)
        return self.policy_dir / f"{name}.yaml"

    def audit_path(self, name: str) -> Path:
        validate_profile_name(name)
        self.audit_dir.mkdir(parents=True, exist_ok=True)
        return self.audit_dir / f"{name}.jsonl"

    def setup_sql_path(self, name: str) -> Path:
        validate_profile_name(name)
        self.setup_dir.mkdir(parents=True, exist_ok=True)
        return self.setup_dir / f"{name}.sql"

    def _ensure_root(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.root.chmod(stat.S_IRWXU)

    def _read_profiles(self) -> dict[str, dict[str, Any]]:
        if not self.profiles_path.exists():
            return {}
        return json.loads(self.profiles_path.read_text(encoding="utf-8"))

    def _write_profiles(self, values: dict[str, dict[str, Any]]) -> None:
        self._ensure_root()
        _atomic_owner_write(
            self.profiles_path,
            json.dumps(values, indent=2, sort_keys=True) + "\n",
        )


def build_policy(
    *,
    preset: str,
    tables: list[str],
    approval_required: bool,
) -> dict[str, Any]:
    if preset not in {"read-only", "development", "production"}:
        raise ValueError(f"unknown safety preset {preset!r}")
    production = preset == "production"
    return {
        "mode": "enforce",
        "read_only": preset == "read-only",
        "allow_multi_statement": False,
        "block_system_catalog": True,
        "require_where_on_writes": True,
        "block_locking": True,
        "require_qualified_tables": production,
        "max_rows_read": 1000,
        "tables": {"allow": sorted(set(tables))},
        "ddl": {"allow": []},
        "simulation": {
            "enabled": True,
            "statement_timeout_ms": 1000,
            "lock_timeout_ms": 200,
            "confirm_over_rows": 1000 if approval_required else None,
            "block_over_rows": 100000,
        },
        "undo": {
            "enabled": preset != "read-only",
            "schema": "interdict_control",
            "block_non_reversible": True,
            "require_agent_match": True,
            "max_capture_rows": 10000,
            "max_capture_bytes": 16 * 1024 * 1024,
        },
        "intent": {
            "enabled": True,
            "single_scope_max": 10,
            "bulk_threshold": 1000,
            "confirm_on_high": approval_required,
            "llm_enabled": False,
        },
    }


def write_policy(path: Path, policy: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_owner_write(path, yaml.safe_dump(policy, sort_keys=False))


def new_profile(
    *,
    name: str,
    database_label: str,
    database_name: str,
    database_role: str,
    safety_profile: str,
    policy_path: Path,
    audit_log_path: Path,
    operator_id: str | None,
    control_mode: str,
    approval_required: bool,
    status: str,
    safety_preset: str | None = None,
) -> ConnectionProfile:
    return ConnectionProfile(
        name=validate_profile_name(name),
        database_label=database_label,
        database_name=database_name,
        database_role=database_role,
        safety_profile=safety_profile,
        policy_path=str(policy_path),
        audit_log_path=str(audit_log_path),
        operator_id=operator_id,
        control_mode=control_mode,
        approval_required=approval_required,
        status=status,
        created_at=datetime.now(UTC).isoformat(),
        safety_preset=safety_preset or safety_profile,
    )
