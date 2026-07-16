"""Versioned transport-clean ActionRequest -> Decision schema.

This module is the v2 contract boundary. Adapters may wrap these objects, but
they should not invent transport-specific verdict shapes. PR #2 only introduces
the schema and migration helpers; existing adapters are intentionally left on
their legacy response dictionaries until the projection PR.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

SCHEMA_VERSION = "1"


class SchemaError(ValueError):
    """Raised when a payload does not conform to schema version 1."""


class PrincipalKind(StrEnum):
    AGENT = "agent"
    HUMAN = "human"
    SERVICE = "service"


class ActionType(StrEnum):
    SQL = "sql"


class Verdict(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    HOLD = "hold"


class ReasonCode:
    """Stable v2 reason codes.

    These are semantic codes. Legacy v1 policy codes that mentioned SQL syntax
    map into this set via ``LEGACY_REASON_CODE_MAP`` below.
    """

    ALLOW = "ALLOW"
    IMPACT_WITHIN_THRESHOLD = "IMPACT_WITHIN_THRESHOLD"
    IMPACT_OVER_THRESHOLD = "IMPACT_OVER_THRESHOLD"
    IMPACT_UNKNOWN = "IMPACT_UNKNOWN"
    IMPACT_MEASUREMENT_FAILED = "IMPACT_MEASUREMENT_FAILED"
    NON_REVERSIBLE_WRITE = "NON_REVERSIBLE_WRITE"
    UNBOUNDED_WRITE = "UNBOUNDED_WRITE"
    MULTI_STATEMENT = "MULTI_STATEMENT"
    SYSTEM_CATALOG_ACCESS = "SYSTEM_CATALOG_ACCESS"
    CONTROL_PLANE_ACCESS = "CONTROL_PLANE_ACCESS"
    TABLE_NOT_ALLOWED = "TABLE_NOT_ALLOWED"
    DDL_NOT_ALLOWED = "DDL_NOT_ALLOWED"
    COLUMN_BLOCKED = "COLUMN_BLOCKED"
    LOCKING_NOT_ALLOWED = "LOCKING_NOT_ALLOWED"
    FUNCTION_NOT_ALLOWED = "FUNCTION_NOT_ALLOWED"
    WRAPPED_WRITE = "WRAPPED_WRITE"
    READ_ONLY_MODE = "READ_ONLY_MODE"
    UNPARSEABLE = "UNPARSEABLE"
    DATABASE_ERROR = "DATABASE_ERROR"
    OPERATOR_APPROVAL_REQUIRED = "OPERATOR_APPROVAL_REQUIRED"
    OPERATOR_APPROVAL_DENIED = "OPERATOR_APPROVAL_DENIED"


LEGACY_REASON_CODE_MAP: dict[str, str] = {
    "READ_ONLY_MODE": ReasonCode.READ_ONLY_MODE,
    "WRITE_WITHOUT_WHERE": ReasonCode.UNBOUNDED_WRITE,
    "MULTI_STATEMENT": ReasonCode.MULTI_STATEMENT,
    "SYSTEM_CATALOG_ACCESS": ReasonCode.SYSTEM_CATALOG_ACCESS,
    "CONTROL_PLANE_ACCESS": ReasonCode.CONTROL_PLANE_ACCESS,
    "TABLE_NOT_ALLOWED": ReasonCode.TABLE_NOT_ALLOWED,
    "DDL_NOT_ALLOWED": ReasonCode.DDL_NOT_ALLOWED,
    "COLUMN_BLOCKED": ReasonCode.COLUMN_BLOCKED,
    "LOCKING_NOT_ALLOWED": ReasonCode.LOCKING_NOT_ALLOWED,
    "FUNCTION_NOT_ALLOWED": ReasonCode.FUNCTION_NOT_ALLOWED,
    "BLAST_RADIUS_EXCEEDED": ReasonCode.IMPACT_OVER_THRESHOLD,
    "BLAST_RADIUS_UNKNOWN": ReasonCode.IMPACT_UNKNOWN,
    "NON_REVERSIBLE_WRITE": ReasonCode.NON_REVERSIBLE_WRITE,
    "WRAPPED_WRITE": ReasonCode.WRAPPED_WRITE,
    "UNPARSEABLE": ReasonCode.UNPARSEABLE,
}


def map_legacy_reason_code(code: str | None) -> str:
    """Map a v1 adapter/policy reason code into the v2 semantic code set."""
    if not code:
        return ReasonCode.ALLOW
    return LEGACY_REASON_CODE_MAP.get(code, code)


def impact_from_legacy_simulation(simulation: dict[str, Any] | None) -> Impact | None:
    """Project the current simulation dict shape into ``Impact v1``."""
    if not simulation:
        return None
    rows = simulation.get("affected_rows")
    cost = simulation.get("estimated_cost")
    return Impact(
        measured=simulation.get("method") not in (None, "skipped"),
        rows_affected=rows if isinstance(rows, int) and rows >= 0 else None,
        cost_estimate=(
            float(cost) if isinstance(cost, (int, float)) and cost >= 0 else None
        ),
    )


def undo_from_execution(
    *,
    handle: str | None,
    reversible: bool | None,
    reason: str | None = None,
) -> Undo | None:
    """Project legacy undo execution metadata into ``Undo v1`` when possible."""
    if handle is None:
        return None
    caveats = (reason,) if reason else ()
    return Undo(handle=handle, reversible=bool(reversible), caveats=caveats)


def decision_from_legacy_policy(
    legacy_decision: Any | None,
    *,
    approval_id: str | None = None,
    confirmation_satisfied: bool = False,
    undo_handle: str | None = None,
    reversible: bool | None = None,
    undo_reason: str | None = None,
) -> Decision:
    """Project the current policy ``Decision`` object into canonical v2.

    This is the migration bridge for PR #3. It intentionally accepts ``Any`` so
    ``engine.schema`` stays independent from ``engine.policy`` and the hot-path
    policy module does not need to import schema types.
    """
    undo = undo_from_execution(
        handle=undo_handle, reversible=reversible, reason=undo_reason
    )
    if legacy_decision is None:
        return Decision.allow(
            reason_code=ReasonCode.ALLOW,
            explanation="No policy configured; action allowed.",
            undo=undo,
        )

    impact = impact_from_legacy_simulation(getattr(legacy_decision, "simulation", None))
    violations = tuple(getattr(legacy_decision, "violations", ()) or ())
    if not getattr(legacy_decision, "allowed", False):
        first = violations[0] if violations else None
        legacy_code = getattr(first, "reason_code", None)
        return Decision.deny(
            reason_code=(
                map_legacy_reason_code(legacy_code)
                if legacy_code
                else ReasonCode.IMPACT_UNKNOWN
            ),
            explanation=(
                getattr(first, "message", None) or "Action denied by Interdict policy."
            ),
            repair_hint=getattr(first, "suggested_fix", None),
            impact=impact,
            undo=undo,
        )

    if (
        getattr(legacy_decision, "requires_confirmation", False)
        and not confirmation_satisfied
    ):
        # confirmation_satisfied means the hold was already resolved (operator
        # approved, or observe mode executed the statement): project as allow
        # below instead of a hold. A live hold still requires its approval_id.
        if approval_id is None:
            raise SchemaError("approval_id is required for hold projection")
        reason = (
            ReasonCode.IMPACT_OVER_THRESHOLD
            if impact and impact.rows_affected is not None
            else ReasonCode.OPERATOR_APPROVAL_REQUIRED
        )
        return Decision.hold_for_approval(
            approval_id=approval_id,
            reason_code=reason,
            explanation="Action requires operator approval before execution.",
            repair_hint="Request operator approval before retrying this action.",
            impact=impact,
            undo=undo,
        )

    reason = ReasonCode.IMPACT_WITHIN_THRESHOLD if impact else ReasonCode.ALLOW
    explanation = (
        "Measured impact is within policy threshold."
        if impact
        else "Action allowed by Interdict policy."
    )
    return Decision.allow(
        reason_code=reason,
        explanation=explanation,
        impact=impact,
        undo=undo,
    )


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SchemaError(f"{name} must be an object")
    return value


def _string(value: Any, name: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise SchemaError(f"{name} must be a string")
    if not allow_empty and value == "":
        raise SchemaError(f"{name} must not be empty")
    return value


def _optional_string(value: Any, name: str) -> str | None:
    if value is None:
        return None
    return _string(value, name)


def _enum_value(enum: type[StrEnum], value: Any, name: str) -> str:
    try:
        return enum(value).value
    except ValueError as exc:
        allowed = ", ".join(e.value for e in enum)
        raise SchemaError(f"{name} must be one of: {allowed}") from exc


def _optional_int(value: Any, name: str) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or value < 0:
        raise SchemaError(f"{name} must be a non-negative integer or null")
    return value


def _optional_float(value: Any, name: str) -> float | None:
    if value is None:
        return None
    if not isinstance(value, (int, float)) or value < 0:
        raise SchemaError(f"{name} must be a non-negative number or null")
    return float(value)


def _schema_version(value: Any) -> str:
    version = value if value is not None else SCHEMA_VERSION
    if version != SCHEMA_VERSION:
        raise SchemaError(f"unsupported schema_version {version!r}")
    return SCHEMA_VERSION


@dataclass(frozen=True)
class Principal:
    id: str = "anonymous"
    kind: str = PrincipalKind.AGENT.value
    delegated_by: str | None = None
    task_id: str | None = None
    stated_task: str | None = None

    def __post_init__(self) -> None:
        _string(self.id, "principal.id")
        _enum_value(PrincipalKind, self.kind, "principal.kind")
        _optional_string(self.delegated_by, "principal.delegated_by")
        _optional_string(self.task_id, "principal.task_id")
        _optional_string(self.stated_task, "principal.stated_task")

    @classmethod
    def anonymous(cls) -> Principal:
        return cls()

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> Principal:
        data = payload or {}
        _mapping(data, "principal")
        return cls(
            id=data.get("id", "anonymous"),
            kind=data.get("kind", PrincipalKind.AGENT.value),
            delegated_by=data.get("delegated_by"),
            task_id=data.get("task_id"),
            stated_task=data.get("stated_task"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "delegated_by": self.delegated_by,
            "task_id": self.task_id,
            "stated_task": self.stated_task,
        }


def principal_from_legacy(
    identity: str | None = None,
    *,
    kind: str = PrincipalKind.AGENT.value,
    delegated_by: str | None = None,
    task_id: str | None = None,
    stated_task: str | None = None,
) -> Principal:
    """Build a v2 principal from legacy agent/operator/actor fields.

    Durable records must never omit principal. When the older call site has no
    identity, record the absence explicitly as ``anonymous``.
    """
    return Principal(
        id=identity or "anonymous",
        kind=kind,
        delegated_by=delegated_by,
        task_id=task_id,
        stated_task=stated_task,
    )


@dataclass(frozen=True)
class Action:
    type: str
    payload: str
    dialect: str = "postgres"

    def __post_init__(self) -> None:
        _enum_value(ActionType, self.type, "action.type")
        _string(self.payload, "action.payload", allow_empty=True)
        if self.dialect != "postgres":
            raise SchemaError("action.dialect must be 'postgres'")

    @classmethod
    def sql(cls, payload: str, dialect: str = "postgres") -> Action:
        return cls(type=ActionType.SQL.value, payload=payload, dialect=dialect)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> Action:
        data = _mapping(payload, "action")
        return cls(
            type=data.get("type"),
            payload=data.get("payload"),
            dialect=data.get("dialect", "postgres"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, "payload": self.payload, "dialect": self.dialect}


@dataclass(frozen=True)
class RequestContext:
    adapter: str
    session_id: str | None = None

    def __post_init__(self) -> None:
        _string(self.adapter, "context.adapter")
        _optional_string(self.session_id, "context.session_id")

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> RequestContext:
        data = _mapping(payload, "context")
        return cls(adapter=data.get("adapter"), session_id=data.get("session_id"))

    def to_dict(self) -> dict[str, Any]:
        return {"session_id": self.session_id, "adapter": self.adapter}


@dataclass(frozen=True)
class ActionRequest:
    principal: Principal
    action: Action
    context: RequestContext
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _schema_version(self.schema_version)

    @classmethod
    def sql(
        cls,
        sql: str,
        *,
        adapter: str,
        principal: Principal | None = None,
        session_id: str | None = None,
    ) -> ActionRequest:
        return cls(
            principal=principal or Principal.anonymous(),
            action=Action.sql(sql),
            context=RequestContext(adapter=adapter, session_id=session_id),
        )

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ActionRequest:
        data = _mapping(payload, "ActionRequest")
        return cls(
            schema_version=_schema_version(data.get("schema_version")),
            principal=Principal.from_dict(data.get("principal")),
            action=Action.from_dict(data.get("action")),
            context=RequestContext.from_dict(data.get("context")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "principal": self.principal.to_dict(),
            "action": self.action.to_dict(),
            "context": self.context.to_dict(),
        }


@dataclass(frozen=True)
class Impact:
    measured: bool
    rows_affected: int | None = None
    cost_estimate: float | None = None
    read_rows: int | None = None
    read_sensitivity: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.measured, bool):
            raise SchemaError("impact.measured must be a boolean")
        _optional_int(self.rows_affected, "impact.rows_affected")
        _optional_float(self.cost_estimate, "impact.cost_estimate")
        _optional_int(self.read_rows, "impact.read_rows")
        _optional_string(self.read_sensitivity, "impact.read_sensitivity")

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> Impact:
        data = _mapping(payload, "impact")
        return cls(
            measured=data.get("measured"),
            rows_affected=data.get("rows_affected"),
            cost_estimate=data.get("cost_estimate"),
            read_rows=data.get("read_rows"),
            read_sensitivity=data.get("read_sensitivity"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "measured": self.measured,
            "rows_affected": self.rows_affected,
            "cost_estimate": self.cost_estimate,
            "read_rows": self.read_rows,
            "read_sensitivity": self.read_sensitivity,
        }


@dataclass(frozen=True)
class Undo:
    handle: str
    reversible: bool
    caveats: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        _string(self.handle, "undo.handle")
        if not isinstance(self.reversible, bool):
            raise SchemaError("undo.reversible must be a boolean")
        for caveat in self.caveats:
            _string(caveat, "undo.caveats[]")

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> Undo:
        data = _mapping(payload, "undo")
        caveats = data.get("caveats", [])
        if not isinstance(caveats, list):
            raise SchemaError("undo.caveats must be a list")
        return cls(
            handle=data.get("handle"),
            reversible=data.get("reversible"),
            caveats=tuple(caveats),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "handle": self.handle,
            "reversible": self.reversible,
            "caveats": list(self.caveats),
        }


@dataclass(frozen=True)
class Hold:
    approval_id: str

    def __post_init__(self) -> None:
        _string(self.approval_id, "hold.approval_id")

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> Hold:
        data = _mapping(payload, "hold")
        return cls(approval_id=data.get("approval_id"))

    def to_dict(self) -> dict[str, Any]:
        return {"approval_id": self.approval_id}


@dataclass(frozen=True)
class Decision:
    verdict: str
    reason_code: str
    explanation: str
    repair_hint: str | None = None
    impact: Impact | None = None
    undo: Undo | None = None
    hold: Hold | None = None
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _schema_version(self.schema_version)
        verdict = _enum_value(Verdict, self.verdict, "verdict")
        _string(self.reason_code, "reason_code")
        _string(self.explanation, "explanation")
        _optional_string(self.repair_hint, "repair_hint")
        if verdict == Verdict.HOLD.value and self.hold is None:
            raise SchemaError("hold verdict requires hold")
        if verdict != Verdict.HOLD.value and self.hold is not None:
            raise SchemaError("hold is only valid for hold verdicts")

    @classmethod
    def allow(
        cls,
        *,
        reason_code: str = ReasonCode.ALLOW,
        explanation: str = "Action allowed.",
        repair_hint: str | None = None,
        impact: Impact | None = None,
        undo: Undo | None = None,
    ) -> Decision:
        return cls(
            verdict=Verdict.ALLOW.value,
            reason_code=reason_code,
            explanation=explanation,
            repair_hint=repair_hint,
            impact=impact,
            undo=undo,
        )

    @classmethod
    def deny(
        cls,
        *,
        reason_code: str,
        explanation: str,
        repair_hint: str | None = None,
        impact: Impact | None = None,
        undo: Undo | None = None,
    ) -> Decision:
        return cls(
            verdict=Verdict.DENY.value,
            reason_code=reason_code,
            explanation=explanation,
            repair_hint=repair_hint,
            impact=impact,
            undo=undo,
        )

    @classmethod
    def hold_for_approval(
        cls,
        *,
        approval_id: str,
        reason_code: str = ReasonCode.OPERATOR_APPROVAL_REQUIRED,
        explanation: str,
        repair_hint: str | None = None,
        impact: Impact | None = None,
        undo: Undo | None = None,
    ) -> Decision:
        return cls(
            verdict=Verdict.HOLD.value,
            reason_code=reason_code,
            explanation=explanation,
            repair_hint=repair_hint,
            impact=impact,
            undo=undo,
            hold=Hold(approval_id),
        )

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> Decision:
        data = _mapping(payload, "Decision")
        return cls(
            schema_version=_schema_version(data.get("schema_version")),
            verdict=data.get("verdict"),
            reason_code=data.get("reason_code"),
            explanation=data.get("explanation"),
            repair_hint=data.get("repair_hint"),
            impact=Impact.from_dict(data["impact"]) if data.get("impact") else None,
            undo=Undo.from_dict(data["undo"]) if data.get("undo") else None,
            hold=Hold.from_dict(data["hold"]) if data.get("hold") else None,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "verdict": self.verdict,
            "reason_code": self.reason_code,
            "explanation": self.explanation,
            "repair_hint": self.repair_hint,
            "impact": self.impact.to_dict() if self.impact else None,
            "undo": self.undo.to_dict() if self.undo else None,
            "hold": self.hold.to_dict() if self.hold else None,
        }
