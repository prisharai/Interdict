from types import SimpleNamespace

from adapters.mcp_server import _mcp_actor


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
