from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read_repo_file(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_global_sse_streams_close_on_page_navigation():
    approval_modal = read_repo_file("static/js/approval-modal.js")
    agent_sidebar = read_repo_file("static/js/agent-sidebar.js")
    agents_page = read_repo_file("templates/agents.html")

    assert "var _sse = null;" in approval_modal
    assert "function _closeSSE()" in approval_modal
    assert "RealtimeClient" in approval_modal
    assert "window.addEventListener('pagehide', _closeSSE);" in approval_modal
    assert "window.addEventListener('beforeunload', _closeSSE);" in approval_modal
    assert "window.addEventListener('pageshow', _startSSE);" in approval_modal

    assert "var _busySSE = null;" in agent_sidebar
    assert "if (_busySSE) return;" in agent_sidebar
    assert "RealtimeClient" in agent_sidebar
    assert "evonic:agent-busy-changed" in agent_sidebar
    assert "function resyncBusyState()" in agent_sidebar
    assert "evonic:agent-busy-resync" in agent_sidebar
    assert "_busySSE.close();" in agent_sidebar
    assert "window.addEventListener('pagehide', closeBusyRealtime);" in agent_sidebar
    assert "window.addEventListener('beforeunload', closeBusyRealtime);" in agent_sidebar
    assert "window.addEventListener('pageshow', function ()" in agent_sidebar

    assert "let agentStatusEventsSubscribed = false;" in agents_page
    assert "function subscribeAgentStatusEvents()" in agents_page
    assert "document.addEventListener('evonic:agent-busy-changed'" in agents_page
    assert "document.addEventListener('evonic:agent-busy-resync'" in agents_page
    assert "window.addEventListener('pageshow', loadBusyAgents);" in agents_page
    assert "new RealtimeClient({" not in agents_page
    assert "new EventSource(`/api/agents/status/stream`)" not in agents_page
    assert "new EventSource('/api/agents/status/stream')" not in agents_page


def test_state_changed_sse_reaches_agent_state_listener():
    """The 'state_changed' SSE event must be listened for by the transport
    (source module AND the generated bundle — rebuild scripts/build_chat_ui.py
    if the bundle assert fails) and bridged to the document-level
    'evonic:agent-state-changed' event that the agent detail / sessions pages
    already handle with a debounced state re-fetch."""
    transport = read_repo_file("static/js/chat-ui/transport.js")
    bundle = read_repo_file("static/js/chat-ui.js")
    for src in (transport, bundle):
        assert "'state_changed'" in src
        assert "new CustomEvent('evonic:agent-state-changed'" in src
    agent_detail = read_repo_file("templates/agent_detail.html")
    assert "document.addEventListener('evonic:agent-state-changed'" in agent_detail
