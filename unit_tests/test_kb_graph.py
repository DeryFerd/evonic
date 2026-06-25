"""
Tests for the kb_graph tool — KB document link graph traversal (1-hop).
"""
import os
import json
import sqlite3
import tempfile
from datetime import datetime, timezone, timedelta

import pytest
from unittest.mock import patch

# Anchor to the repo root so the tool-definition path resolves regardless of
# the pytest working directory.
_TOOL_JSON = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "tools", "kb_graph.json",
)


# ─── helpers ────────────────────────────────────────────────────────────────

def _make_test_db() -> str:
    """Create a temp evomem DB with KB pages and links."""
    tdir = tempfile.mkdtemp()
    db_path = os.path.join(tdir, ".evomem.db")
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE pages (
            id INTEGER PRIMARY KEY, slug TEXT NOT NULL UNIQUE, title TEXT NOT NULL,
            page_type TEXT NOT NULL DEFAULT 'note', source_dir TEXT NOT NULL DEFAULT '',
            tags TEXT NOT NULL DEFAULT '[]', content_hash TEXT NOT NULL,
            created_at TEXT, updated_at TEXT, synced_at TEXT NOT NULL, deleted_at TEXT
        );
        CREATE TABLE links (
            src_page_id INTEGER NOT NULL REFERENCES pages(id),
            dst_slug TEXT NOT NULL, dst_page_id INTEGER REFERENCES pages(id),
            edge_type TEXT NOT NULL DEFAULT 'mentions', anchor_text TEXT,
            PRIMARY KEY (src_page_id, dst_slug, edge_type)
        );
    """)

    now = datetime.now(timezone.utc)
    old = (now - timedelta(days=7)).isoformat()
    newer = (now - timedelta(days=1)).isoformat()

    pages = [
        (1, "notes.md", "User Notes", "kb", '["preferences","instructions"]', old),
        (2, "howto-report.md", "Report Guide", "kb", '["guide","reporting"]', old),
        (3, "changelog-format.md", "Changelog Format", "kb", '["guide"]', newer),
        (4, "api-docs.md", "API Docs", "kb", '["reference"]', old),
        (5, "kanban-guide.md", "Kanban Guide", "kb", '["guide"]', old),
    ]
    for p in pages:
        conn.execute(
            "INSERT INTO pages(id,slug,title,page_type,source_dir,tags,updated_at,synced_at,content_hash,deleted_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (p[0], p[1], p[2], p[3], "kb", p[4], p[5], p[5], "hash", None),
        )

    links = [
        (1, "howto-report.md", 2),
        (1, "changelog-format.md", 3),
        (3, "notes.md", 1),
        (3, "api-docs.md", 4),
        (2, "nonexistent.md", None),
    ]
    for l in links:
        conn.execute(
            "INSERT INTO links(src_page_id,dst_slug,dst_page_id,edge_type) VALUES(?,?,?,?)",
            (l[0], l[1], l[2], "mentions"),
        )
    conn.commit()
    conn.close()
    return tdir


# ─── Tool registration tests ────────────────────────────────────────────────

class TestToolRegistration:
    def test_tool_json_exists(self):
        assert os.path.isfile(_TOOL_JSON)

    def test_tool_json_valid(self):
        with open(_TOOL_JSON) as f:
            data = json.load(f)
        assert data["id"] == "kb_graph"
        assert data["function"]["name"] == "kb_graph"
        params = data["function"]["parameters"]
        assert "filename" in params["properties"]
        assert "filename" in params["required"]

    def test_backend_has_execute(self):
        from backend.tools.kb_graph import execute
        assert callable(execute)

    def test_missing_filename_error(self):
        from backend.tools.kb_graph import execute
        result = execute({"agent_id": "test"}, {})
        assert "error" in result

    def test_empty_filename_error(self):
        from backend.tools.kb_graph import execute
        result = execute({"agent_id": "test"}, {"filename": "  "})
        assert "error" in result

    def test_non_md_filename_error(self):
        from backend.tools.kb_graph import execute
        result = execute({"agent_id": "test"}, {"filename": "notes.txt"})
        assert "error" in result
        assert ".md" in result["error"]


# ─── Outgoing links tests ──────────────────────────────────────────────────

class TestOutgoingLinks:
    def test_outgoing_count_correct(self):
        db_dir = _make_test_db()
        from backend.tools.kb_graph import execute
        with patch("backend.agent_runtime.evomem_client._get_brain_dir", return_value=db_dir):
            result = execute({"agent_id": "test"}, {"filename": "notes.md"})
        text = result["result"]
        assert "→ references (2):" in text
        assert "howto-report.md" in text
        assert "changelog-format.md" in text

    def test_timestamps_shown(self):
        db_dir = _make_test_db()
        from backend.tools.kb_graph import execute
        with patch("backend.agent_runtime.evomem_client._get_brain_dir", return_value=db_dir):
            result = execute({"agent_id": "test"}, {"filename": "notes.md"})
        text = result["result"]
        assert "last updated" in text

    def test_zero_outgoing_shows_none(self):
        db_dir = _make_test_db()
        from backend.tools.kb_graph import execute
        with patch("backend.agent_runtime.evomem_client._get_brain_dir", return_value=db_dir):
            result = execute({"agent_id": "test"}, {"filename": "api-docs.md"})
        text = result["result"]
        assert "→ references (0):" in text
        assert "<none>" in text


# ─── Incoming links tests ──────────────────────────────────────────────────

class TestIncomingLinks:
    def test_incoming_count_correct(self):
        db_dir = _make_test_db()
        from backend.tools.kb_graph import execute
        with patch("backend.agent_runtime.evomem_client._get_brain_dir", return_value=db_dir):
            result = execute({"agent_id": "test"}, {"filename": "howto-report.md"})
        text = result["result"]
        assert "↑ referenced by (1):" in text
        assert "notes.md" in text

    def test_zero_incoming_shows_none(self):
        db_dir = _make_test_db()
        from backend.tools.kb_graph import execute
        with patch("backend.agent_runtime.evomem_client._get_brain_dir", return_value=db_dir):
            result = execute({"agent_id": "test"}, {"filename": "kanban-guide.md"})
        text = result["result"]
        assert "↑ referenced by (0):" in text
        assert "<none>" in text


# ─── Same-tag tests ────────────────────────────────────────────────────────

class TestSameTagDiscovery:
    def test_same_tag_docs_shown(self):
        db_dir = _make_test_db()
        from backend.tools.kb_graph import execute
        with patch("backend.agent_runtime.evomem_client._get_brain_dir", return_value=db_dir):
            result = execute({"agent_id": "test"}, {"filename": "howto-report.md"})
        text = result["result"]
        assert "Related by tag" in text
        assert "changelog-format.md" in text
        assert "kanban-guide.md" in text

    def test_source_not_in_related(self):
        db_dir = _make_test_db()
        from backend.tools.kb_graph import execute
        with patch("backend.agent_runtime.evomem_client._get_brain_dir", return_value=db_dir):
            result = execute({"agent_id": "test"}, {"filename": "howto-report.md"})
        text = result["result"]
        # howto-report.md should not list itself under Related by tag
        related_start = text.find("Related by tag")
        if related_start >= 0:
            after_related = text[related_start:]
            assert "howto-report.md" not in after_related

    def test_no_shared_tags_no_section(self):
        db_dir = _make_test_db()
        from backend.tools.kb_graph import execute
        with patch("backend.agent_runtime.evomem_client._get_brain_dir", return_value=db_dir):
            result = execute({"agent_id": "test"}, {"filename": "api-docs.md"})
        # api-docs.md has tag "reference" — no other doc shares that tag
        text = result["result"]
        # The section should not appear if no docs share tags
        assert "Related by tag" not in text


# ─── Staleness / timestamps ────────────────────────────────────────────────

class TestStaleness:
    def test_newer_target_shows_recent(self):
        db_dir = _make_test_db()
        from backend.tools.kb_graph import execute
        with patch("backend.agent_runtime.evomem_client._get_brain_dir", return_value=db_dir):
            result = execute({"agent_id": "test"}, {"filename": "notes.md"})
        text = result["result"]
        # notes → changelog-format: changelog is NEWER (1 day ago)
        # notes → howto-report: howto is older (7 days ago)
        assert "1 day ago" in text
        assert "7 days ago" in text


# ─── 1-hop limit ───────────────────────────────────────────────────────────

class TestOneHopLimit:
    def test_only_direct_neighbors(self):
        db_dir = _make_test_db()
        from backend.tools.kb_graph import execute
        with patch("backend.agent_runtime.evomem_client._get_brain_dir", return_value=db_dir):
            result = execute({"agent_id": "test"}, {"filename": "notes.md"})
        text = result["result"]
        # notes → changelog → api-docs. So api-docs is 2nd hop from notes.
        # notes' direct outgoing: howto-report + changelog-format
        # api-docs should NOT appear
        assert "api-docs.md" not in text


# ─── Dangling links ────────────────────────────────────────────────────────

class TestDanglingLinks:
    def test_dangling_shown(self):
        db_dir = _make_test_db()
        from backend.tools.kb_graph import execute
        with patch("backend.agent_runtime.evomem_client._get_brain_dir", return_value=db_dir):
            result = execute({"agent_id": "test"}, {"filename": "howto-report.md"})
        text = result["result"]
        assert "⚠ dangling" in text
        assert "nonexistent.md" in text
        assert "target page does not exist" in text

    def test_dangling_count_included(self):
        db_dir = _make_test_db()
        from backend.tools.kb_graph import execute
        with patch("backend.agent_runtime.evomem_client._get_brain_dir", return_value=db_dir):
            result = execute({"agent_id": "test"}, {"filename": "howto-report.md"})
        text = result["result"]
        # 1 dangling (nonexistent.md) + 0 resolved = (1) total
        assert "→ references (1):" in text


# ─── Cycle handling ────────────────────────────────────────────────────────

class TestCycleHandling:
    def test_cycle_no_infinite_loop(self):
        db_dir = _make_test_db()
        from backend.tools.kb_graph import execute
        with patch("backend.agent_runtime.evomem_client._get_brain_dir", return_value=db_dir):
            result = execute({"agent_id": "test"}, {"filename": "notes.md"})
        text = result["result"]
        # notes ↔ changelog is a mutual link, so changelog-format.md appears
        # once as an outgoing reference and once as an incoming referrer.
        assert "changelog-format.md" in text
        # The cycle must terminate: it shows up in both directions (2x), not
        # repeated infinitely.
        assert text.count("changelog-format.md") == 2


# ─── Edge cases ────────────────────────────────────────────────────────────

class TestEdgeCases:
    def test_missing_agent_id(self):
        from backend.tools.kb_graph import execute
        result = execute({}, {"filename": "notes.md"})
        assert "error" in result
        assert "agent_id" in result["error"]

    def test_file_not_in_evomem(self):
        db_dir = _make_test_db()
        from backend.tools.kb_graph import execute
        with patch("backend.agent_runtime.evomem_client._get_brain_dir", return_value=db_dir):
            result = execute({"agent_id": "test"}, {"filename": "nonexistent.md"})
        assert "error" in result
        assert "not found" in result["error"]

    def test_whitespace_filename(self):
        from backend.tools.kb_graph import execute
        result = execute({"agent_id": "test"}, {"filename": "   "})
        assert "error" in result
