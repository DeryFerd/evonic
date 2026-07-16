from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BASE_TEMPLATE = ROOT / "templates" / "base.html"
REALTIME_JS = ROOT / "static" / "js" / "realtime.js"


def read_base_template() -> str:
    return BASE_TEMPLATE.read_text(encoding="utf-8")


def test_disconnect_banner_uses_same_tailwind_palette_in_both_themes():
    template = read_base_template()
    banner = template.split('id="wa-disconnect-banner"', 1)[1].split("</div>", 3)[0]

    assert 'class="hidden bg-amber-50 border-b border-amber-200 text-gray-700"' in banner
    assert "dark:" not in banner
    assert "style=\"display:none;background:#f59e0b" not in template


def test_disconnect_banner_normalizes_agent_detail_url():
    template = read_base_template()

    assert "String(a.id || '').replace(/^\\/+|\\/+$/g, '')" in template
    assert "'/agents/' + encodeURIComponent(agentId)" in template
    assert "'/agents/' + esc(a.id || '')" not in template


def test_bridge_status_event_refreshes_disconnect_banner_in_realtime():
    template = read_base_template()
    realtime = REALTIME_JS.read_text(encoding="utf-8")

    assert "'agent_busy_changed', 'agent_turn_complete', 'whatsapp_bridge_status'" in realtime
    assert "evtName === 'whatsapp_bridge_status'" in realtime
    assert "window._evRealtime.on('status', 'whatsapp_bridge_status', updateWABadge)" in template
