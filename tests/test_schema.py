import json

import pytest

from engine.policy import Decision as LegacyDecision
from engine.policy import Violation
from engine.schema import (
    ActionRequest,
    Decision,
    Impact,
    Principal,
    PrincipalKind,
    ReasonCode,
    SchemaError,
    Undo,
    Verdict,
    decision_from_legacy_policy,
    map_legacy_reason_code,
    principal_from_legacy,
)


def test_action_request_round_trips_with_reserved_principal_fields():
    req = ActionRequest.sql(
        "UPDATE film SET rental_rate = 1 WHERE film_id = 1",
        adapter="mcp",
        session_id="sess-1",
        principal=Principal(
            id="agent-1",
            kind=PrincipalKind.AGENT,
            delegated_by="human-7",
            task_id="task-9",
            stated_task="adjust one film",
        ),
    )

    blob = req.to_dict()

    assert blob["schema_version"] == "1"
    assert blob["principal"] == {
        "id": "agent-1",
        "kind": "agent",
        "delegated_by": "human-7",
        "task_id": "task-9",
        "stated_task": "adjust one film",
    }
    assert blob["action"] == {
        "type": "sql",
        "payload": "UPDATE film SET rental_rate = 1 WHERE film_id = 1",
        "dialect": "postgres",
    }
    assert ActionRequest.from_dict(json.loads(json.dumps(blob))) == req


def test_absent_principal_is_recorded_explicitly_as_anonymous():
    req = ActionRequest.from_dict(
        {
            "schema_version": "1",
            "action": {"type": "sql", "payload": "SELECT 1", "dialect": "postgres"},
            "context": {"adapter": "tui"},
        }
    )

    assert req.principal.id == "anonymous"
    assert req.to_dict()["principal"]["delegated_by"] is None


def test_legacy_identity_becomes_explicit_principal():
    principal = principal_from_legacy("agent-x", stated_task="clean up")

    assert principal.to_dict() == {
        "id": "agent-x",
        "kind": "agent",
        "delegated_by": None,
        "task_id": None,
        "stated_task": "clean up",
    }
    assert principal_from_legacy(None).id == "anonymous"


def test_action_type_is_closed_to_sql_for_v2():
    with pytest.raises(SchemaError, match="action.type"):
        ActionRequest.from_dict(
            {
                "schema_version": "1",
                "principal": {"id": "a", "kind": "agent"},
                "action": {"type": "http", "payload": "GET /", "dialect": "postgres"},
                "context": {"adapter": "mcp"},
            }
        )


def test_decision_round_trips_with_reserved_impact_fields_and_undo():
    decision = Decision.allow(
        reason_code=ReasonCode.IMPACT_WITHIN_THRESHOLD,
        explanation="Measured impact is within policy threshold.",
        impact=Impact(measured=True, rows_affected=1, cost_estimate=3.5),
        undo=Undo(handle="undo-1", reversible=True, caveats=("sequence not reset",)),
    )

    blob = decision.to_dict()

    assert blob["verdict"] == "allow"
    assert blob["impact"] == {
        "measured": True,
        "rows_affected": 1,
        "cost_estimate": 3.5,
        "read_rows": None,
        "read_sensitivity": None,
    }
    assert blob["undo"]["caveats"] == ["sequence not reset"]
    assert Decision.from_dict(json.loads(json.dumps(blob))) == decision


def test_hold_verdict_requires_approval_id_and_rejects_hold_on_other_verdicts():
    held = Decision.hold_for_approval(
        approval_id="approval-1",
        explanation="Measured impact exceeds confirmation threshold.",
        impact=Impact(measured=True, rows_affected=5000),
    )

    assert held.verdict == Verdict.HOLD.value
    assert held.to_dict()["hold"] == {"approval_id": "approval-1"}

    with pytest.raises(SchemaError, match="hold verdict requires hold"):
        Decision(
            verdict="hold",
            reason_code=ReasonCode.OPERATOR_APPROVAL_REQUIRED,
            explanation="Needs approval.",
        )

    with pytest.raises(SchemaError, match="only valid"):
        Decision.from_dict(
            {
                "schema_version": "1",
                "verdict": "deny",
                "reason_code": ReasonCode.IMPACT_OVER_THRESHOLD,
                "explanation": "Too broad.",
                "hold": {"approval_id": "approval-1"},
            }
        )


def test_verdict_enum_is_closed():
    with pytest.raises(SchemaError, match="verdict"):
        Decision(
            verdict="confirm",
            reason_code=ReasonCode.OPERATOR_APPROVAL_REQUIRED,
            explanation="Old v1 wording is not a v2 verdict.",
        )


def test_schema_version_rejects_silent_breakage():
    with pytest.raises(SchemaError, match="unsupported schema_version"):
        Decision.from_dict(
            {
                "schema_version": "2",
                "verdict": "allow",
                "reason_code": ReasonCode.ALLOW,
                "explanation": "ok",
            }
        )


def test_legacy_reason_code_mapping_is_the_migration_artifact():
    assert map_legacy_reason_code("WRITE_WITHOUT_WHERE") == ReasonCode.UNBOUNDED_WRITE
    assert map_legacy_reason_code("BLAST_RADIUS_EXCEEDED") == (
        ReasonCode.IMPACT_OVER_THRESHOLD
    )
    assert map_legacy_reason_code("BLAST_RADIUS_UNKNOWN") == ReasonCode.IMPACT_UNKNOWN
    assert map_legacy_reason_code(None) == ReasonCode.ALLOW
    assert map_legacy_reason_code("FUTURE_CODE") == "FUTURE_CODE"


def test_legacy_policy_decision_projects_to_canonical_denial():
    legacy = LegacyDecision(
        allowed=False,
        effective_sql="DELETE FROM film",
        violations=(
            Violation(
                "WRITE_WITHOUT_WHERE",
                "UPDATE/DELETE has no WHERE clause.",
                "Add a WHERE clause.",
            ),
        ),
    )

    decision = decision_from_legacy_policy(legacy)

    assert decision.verdict == "deny"
    assert decision.reason_code == ReasonCode.UNBOUNDED_WRITE
    assert decision.repair_hint == "Add a WHERE clause."


def test_legacy_policy_hold_projection_requires_approval_id():
    legacy = LegacyDecision(
        allowed=True,
        violations=(),
        effective_sql="DELETE FROM film WHERE rental_rate < 3",
        requires_confirmation=True,
        simulation={"method": "precise", "exact_rows": 5000, "affected_rows": 5000},
    )

    with pytest.raises(SchemaError, match="approval_id"):
        decision_from_legacy_policy(legacy)

    held = decision_from_legacy_policy(legacy, approval_id="approval-1")
    assert held.verdict == "hold"
    assert held.reason_code == ReasonCode.IMPACT_OVER_THRESHOLD
    assert held.impact.rows_affected == 5000
    assert held.hold.approval_id == "approval-1"
