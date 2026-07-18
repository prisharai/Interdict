from types import SimpleNamespace

from adapters.mcp_server import _executed_summary, _held_summary, _mcp_actor


def test_mcp_actor_prefers_client_id():
    ctx = SimpleNamespace(
        client_id="codex-client", session=object(), request_id="req-1"
    )

    assert _mcp_actor(ctx) == "codex-client"


def test_mcp_actor_falls_back_to_stable_session_identity():
    session = object()
    first = SimpleNamespace(client_id=None, session=session, request_id="req-1")
    second = SimpleNamespace(client_id=None, session=session, request_id="req-2")

    assert _mcp_actor(first) == _mcp_actor(second)
    assert _mcp_actor(first) != "req-1"


def test_held_summary_points_to_terminal_then_chat():
    summary = _held_summary("approval-123", {"affected_rows": 100})

    assert "approval credential outside this chat" in summary
    assert "In YOUR terminal (not here)" in summary
    assert "interdict approvals" in summary
    assert "interdict approve approval-123" in summary
    assert "AGENT_OPERATOR_TOKEN" not in summary
    assert 'run_approved_query(approval_id="approval-123")' in summary


def test_executed_summary_includes_revert_hint_for_undoable_write():
    summary = _executed_summary("DELETE 100", 100, None, "undo-123")

    assert "undo_id=undo-123" in summary
    assert 'request_revert(action_id="undo-123")' in summary
    assert "human" in summary
