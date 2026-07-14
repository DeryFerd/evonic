from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "static/js/chat-ui.js"
MODULE = ROOT / "static/js/chat-ui/renderers.js"


def _recall_block(path):
    source = path.read_text()
    start = source.index("function _renderRecallResult(result) {")
    end = source.index("function buildToolResultDetail(ev) {")
    return source[start:end]


def test_recall_renderers_stay_in_sync():
    assert _recall_block(BUNDLE) == _recall_block(MODULE).split("/**\n * Build a jQuery element for the tool result detail section.")[0]


def test_recall_renderer_supports_evomem_and_legacy_fields():
    block = _recall_block(BUNDLE)
    assert "_recallText(m.slug)" in block
    assert "m.id !== null && m.id !== undefined" in block
    assert "_recallText(m.snippet)" in block
    assert "_recallText(m.content)" in block
    assert "_recallText(m.title)" in block
    assert "m.source_file" in block
    assert "m.evidence" in block
    assert "Number.isFinite(score)" in block
    assert ".text('#' + m.id)" not in block
    assert ".text(m.content)" not in block


def test_recall_renderer_guards_optional_values():
    block = _recall_block(BUNDLE)
    assert "_recallText" in block
    assert "_recallDate" in block
    assert "m.score === null || m.score === undefined" in block
    assert "meta.some(item => item.value)" in block


def test_recall_renderer_is_compact_and_expandable():
    source = BUNDLE.read_text()
    block = _recall_block(BUNDLE)
    css = (ROOT / "static/style.css").read_text()
    assert "_recallBody" in block
    assert "Show more" in source
    assert "is-clamped" in source
    assert "result.query" not in block
    assert ".recall-card { min-width: 0; padding: 0.25rem 0.34rem; border: 0" in css
    assert "html.dark .recall-card" in css
    assert "grid-template-columns: repeat(2" in css
