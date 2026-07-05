"""
Tests for the kb_graph tool — KB document link graph traversal (1-hop).

KB pages are the top-level pages of the evomem knowledge root, so their slug is
the filename stem (e.g. 'notes'). The tool accepts a filename
('notes.md') and query_kb_graph normalises it to the slug.
"""
import os
import json
import sqlite3
import tempfile
from datetime import datetime, timezone, timedelta

import pytest
from unittest.mock import patch

# ─── helpers ────────────────────────────────────────────────────────────────

def _make_test_db() -> str:
    """Create a temp evomem DB with KB pages and links (bare slugs)."""
    tdir = tempfile.mkdtemp()
    db_path = os.path.join(tdir, ".evomem.db")
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE docs (
            id INTEGER PRIMARY KEY, slug TEXT NOT NULL UNIQUE, title TEXT NOT NULL,
            doc_type TEXT NOT NULL DEFAULT 'note', source_dir TEXT NOT NULL DEFAULT '',
            tags TEXT NOT NULL DEFAULT '[]', content_hash TEXT NOT NULL,
            created_at TEXT, updated_at TEXT, synced_at TEXT NOT NULL, deleted_at TEXT
        );
        CREATE TABLE links (
            src_doc_id INTEGER NOT NULL REFERENCES docs(id),
            dst_slug TEXT NOT NULL, dst_doc_id INTEGER REFERENCES docs(id),
            edge_type TEXT NOT NULL DEFAULT 'mentions', anchor_text TEXT,
            PRIMARY KEY (src_doc_id, dst_slug, edge_type)
        );
    """)

    now = datetime.now(timezone.utc)
    old = (now - timedelta(days=7)).isoformat()
    newer = (now - timedelta(days=1)).isoformat()

    pages = [
        (1, "notes", "User Notes", "note", '["preferences","instructions"]', old),
        (2, "howto-report", "Report Guide", "note", '["guide","reporting"]', old),
        (3, "changelog-format", "Changelog Format", "note", '["guide"]', newer),
        (4, "api-docs", "API Docs", "note", '["reference"]', old),
        (5, "kanban-guide", "Kanban Guide", "note", '["guide"]', old),
    ]
    for p in pages:
        conn.execute(
            "INSERT INTO docs(id,slug,title,doc_type,source_dir,tags,updated_at,synced_at,content_hash,deleted_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (p[0], p[1], p[2], p[3], "", p[4], p[5], p[5], "hash", None),
        )

    links = [
        (1, "howto-report", 2),
        (1, "changelog-format", 3),
        (3, "notes", 1),
        (3, "api-docs", 4),
        (2, "nonexistent", None),
    ]
    for l in links:
        conn.execute(
            "INSERT INTO links(src_doc_id,dst_slug,dst_doc_id,edge_type) VALUES(?,?,?,?)",
            (l[0], l[1], l[2], "mentions"),
        )
    conn.commit()
    conn.close()
    return tdir


# ─── Tool registration tests ────────────────────────────────────────────────

class TestToolRegistration:
    def test_backend_has_execute(self):
        # kb_graph is no longer a standalone tool; its execute() is the formatter
        # behind recall(mode='links').
        from backend.tools.kb_graph import execute
        assert callable(execute)

    def test_recall_links_mode_routes_to_kb_graph(self):
        # The unified 'recall' tool exposes a 'links' mode in its schema.
        from backend.tools.registry import _builtin_recall_factory
        tool_def, _ = _builtin_recall_factory({"id": "test"})
        modes = tool_def["function"]["parameters"]["properties"]["mode"]["enum"]
        assert "links" in modes

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
        with patch("backend.agent_runtime.evomem_client._get_evomem_dir", return_value=db_dir):
            result = execute({"agent_id": "test"}, {"filename": "notes.md"})
        text = result["result"]
        assert "→ references (2):" in text
        assert "howto-report" in text
        assert "changelog-format" in text

    def test_timestamps_shown(self):
        db_dir = _make_test_db()
        from backend.tools.kb_graph import execute
        with patch("backend.agent_runtime.evomem_client._get_evomem_dir", return_value=db_dir):
            result = execute({"agent_id": "test"}, {"filename": "notes.md"})
        text = result["result"]
        assert "last updated" in text

    def test_zero_outgoing_shows_none(self):
        db_dir = _make_test_db()
        from backend.tools.kb_graph import execute
        with patch("backend.agent_runtime.evomem_client._get_evomem_dir", return_value=db_dir):
            result = execute({"agent_id": "test"}, {"filename": "api-docs.md"})
        text = result["result"]
        assert "→ references (0):" in text
        assert "<none>" in text


# ─── Incoming links tests ──────────────────────────────────────────────────

class TestIncomingLinks:
    def test_incoming_count_correct(self):
        db_dir = _make_test_db()
        from backend.tools.kb_graph import execute
        with patch("backend.agent_runtime.evomem_client._get_evomem_dir", return_value=db_dir):
            result = execute({"agent_id": "test"}, {"filename": "howto-report.md"})
        text = result["result"]
        assert "↑ referenced by (1):" in text
        assert "notes" in text

    def test_zero_incoming_shows_none(self):
        db_dir = _make_test_db()
        from backend.tools.kb_graph import execute
        with patch("backend.agent_runtime.evomem_client._get_evomem_dir", return_value=db_dir):
            result = execute({"agent_id": "test"}, {"filename": "kanban-guide.md"})
        text = result["result"]
        assert "↑ referenced by (0):" in text
        assert "<none>" in text


# ─── Same-tag tests ────────────────────────────────────────────────────────

class TestSameTagDiscovery:
    def test_same_tag_docs_shown(self):
        db_dir = _make_test_db()
        from backend.tools.kb_graph import execute
        with patch("backend.agent_runtime.evomem_client._get_evomem_dir", return_value=db_dir):
            result = execute({"agent_id": "test"}, {"filename": "howto-report.md"})
        text = result["result"]
        assert "Related by tag" in text
        assert "changelog-format" in text
        assert "kanban-guide" in text

    def test_source_not_in_related(self):
        db_dir = _make_test_db()
        from backend.tools.kb_graph import execute
        with patch("backend.agent_runtime.evomem_client._get_evomem_dir", return_value=db_dir):
            result = execute({"agent_id": "test"}, {"filename": "howto-report.md"})
        text = result["result"]
        # howto-report should not list itself under Related by tag
        related_start = text.find("Related by tag")
        if related_start >= 0:
            after_related = text[related_start:]
            assert "howto-report" not in after_related

    def test_no_shared_tags_no_section(self):
        db_dir = _make_test_db()
        from backend.tools.kb_graph import execute
        with patch("backend.agent_runtime.evomem_client._get_evomem_dir", return_value=db_dir):
            result = execute({"agent_id": "test"}, {"filename": "api-docs.md"})
        # api-docs has tag "reference" — no other doc shares that tag
        text = result["result"]
        assert "Related by tag" not in text


# ─── Staleness / timestamps ────────────────────────────────────────────────

class TestStaleness:
    def test_newer_target_shows_recent(self):
        db_dir = _make_test_db()
        from backend.tools.kb_graph import execute
        with patch("backend.agent_runtime.evomem_client._get_evomem_dir", return_value=db_dir):
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
        with patch("backend.agent_runtime.evomem_client._get_evomem_dir", return_value=db_dir):
            result = execute({"agent_id": "test"}, {"filename": "notes.md"})
        text = result["result"]
        # notes → changelog → api-docs. So api-docs is 2nd hop from notes.
        # notes' direct outgoing: howto-report + changelog-format
        assert "api-docs" not in text


# ─── Dangling links ────────────────────────────────────────────────────────

class TestDanglingLinks:
    def test_dangling_shown(self):
        db_dir = _make_test_db()
        from backend.tools.kb_graph import execute
        with patch("backend.agent_runtime.evomem_client._get_evomem_dir", return_value=db_dir):
            result = execute({"agent_id": "test"}, {"filename": "howto-report.md"})
        text = result["result"]
        assert "⚠ dangling" in text
        assert "nonexistent" in text
        assert "target page does not exist" in text

    def test_dangling_count_included(self):
        db_dir = _make_test_db()
        from backend.tools.kb_graph import execute
        with patch("backend.agent_runtime.evomem_client._get_evomem_dir", return_value=db_dir):
            result = execute({"agent_id": "test"}, {"filename": "howto-report.md"})
        text = result["result"]
        # 1 dangling (nonexistent) + 0 resolved = (1) total
        assert "→ references (1):" in text


# ─── Cycle handling ────────────────────────────────────────────────────────

class TestCycleHandling:
    def test_cycle_no_infinite_loop(self):
        db_dir = _make_test_db()
        from backend.tools.kb_graph import execute
        with patch("backend.agent_runtime.evomem_client._get_evomem_dir", return_value=db_dir):
            result = execute({"agent_id": "test"}, {"filename": "notes.md"})
        text = result["result"]
        # notes ↔ changelog is a mutual link, so changelog-format appears
        # once as an outgoing reference and once as an incoming referrer.
        assert "changelog-format" in text
        assert text.count("changelog-format") == 2


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
        with patch("backend.agent_runtime.evomem_client._get_evomem_dir", return_value=db_dir):
            result = execute({"agent_id": "test"}, {"filename": "does-not-exist.md"})
        assert "error" in result
        assert "not found" in result["error"]

    def test_whitespace_filename(self):
        from backend.tools.kb_graph import execute
        result = execute({"agent_id": "test"}, {"filename": "   "})
        assert "error" in result
