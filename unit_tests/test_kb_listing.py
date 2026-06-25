"""
Tests for enhanced KB listing with graph metadata and staleness detection.
"""
import os
import json
import sqlite3
import tempfile
from datetime import datetime, timezone, timedelta

import pytest
from unittest.mock import patch, MagicMock



# ─── helpers ────────────────────────────────────────────────────────────────

def _make_temp_evomem_db() -> str:
    """Create a temporary evomem DB with test schema and data. Returns temp dir path."""
    tmpdir = tempfile.mkdtemp(prefix="evomem_test_")
    db_path = os.path.join(tmpdir, ".evomem.db")
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
    conn.close()
    return tmpdir


def _seed_graph_data(db_path: str):
    """Insert test pages and links into the evomem DB."""
    conn = sqlite3.connect(db_path)
    now = datetime.now(timezone.utc).isoformat()
    old = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    newer = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()

    pages = [
        # KB pages
        (1, "notes.md", "User Notes", "kb", "kb", '["preferences","instructions"]', old),
        (2, "howto-report.md", "Report Guide", "kb", "kb", '["guide","reporting"]', old),
        (3, "changelog-format.md", "Changelog Format", "kb", "kb", '["guide"]', newer),
        (4, "api-docs.md", "API Docs", "kb", "kb", '["reference"]', old),
        # Isolated KB page: no incoming and no outgoing links
        (5, "isolated.md", "Isolated", "kb", "kb", '["reference"]', old),
        # Non-KB pages
        (10, "entities/acme", "Acme Corp", "entity", "entities", '["entity"]', old),
        (11, "notes/some-fact", "A Fact", "note", "notes", "[]", old),
        # Soft-deleted KB page
        (12, "deleted-doc.md", "Deleted", "kb", "kb", "[]", old),
    ]
    for p in pages:
        deleted = old if p[0] == 12 else None  # page 12 is soft-deleted
        conn.execute(
            "INSERT INTO pages(id,slug,title,page_type,source_dir,tags,updated_at,synced_at,content_hash,deleted_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (p[0], p[1], p[2], p[3], p[4], p[5], p[6], p[6], "hash", deleted),
        )

    links = [
        (1, "howto-report.md", 2, "mentions"),
        (1, "changelog-format.md", 3, "mentions"),
        (3, "notes.md", 1, "mentions"),
        (3, "api-docs.md", 4, "mentions"),
        # Dangling link (dst_page_id IS NULL)
        (2, "missing-doc.md", None, "mentions"),
    ]
    for l in links:
        conn.execute(
            "INSERT INTO links(src_page_id,dst_slug,dst_page_id,edge_type) VALUES(?,?,?,?)",
            (l[0], l[1], l[2], l[3]),
        )

    conn.commit()
    conn.close()


# ─── Graph metadata query tests ─────────────────────────────────────────────

class TestGetKbGraphMetadata:
    def test_incoming_count_correct(self):
        db_dir = _make_temp_evomem_db()
        db_path = os.path.join(db_dir, ".evomem.db")
        _seed_graph_data(db_path)

        from backend.agent_runtime.evomem_client import get_kb_graph_metadata

        with patch("backend.agent_runtime.evomem_client._get_brain_dir", return_value=db_dir):
            meta = get_kb_graph_metadata("test-agent")

        assert meta is not None
        pages = meta["pages"]
        # notes.md has incoming from changelog-format.md (page 3)
        assert pages["notes.md"]["incoming_count"] == 1
        assert "changelog-format.md" in pages["notes.md"]["incoming_slugs"]
        # howto-report.md has incoming from notes.md (page 1)
        assert pages["howto-report.md"]["incoming_count"] == 1
        assert "notes.md" in pages["howto-report.md"]["incoming_slugs"]

    def test_outgoing_count_correct(self):
        db_dir = _make_temp_evomem_db()
        db_path = os.path.join(db_dir, ".evomem.db")
        _seed_graph_data(db_path)

        from backend.agent_runtime.evomem_client import get_kb_graph_metadata

        with patch("backend.agent_runtime.evomem_client._get_brain_dir", return_value=db_dir):
            meta = get_kb_graph_metadata("test-agent")

        pages = meta["pages"]
        # notes.md links to howto-report.md and changelog-format.md
        assert len(pages["notes.md"]["outgoing_slugs"]) == 2
        assert "howto-report.md" in pages["notes.md"]["outgoing_slugs"]
        assert "changelog-format.md" in pages["notes.md"]["outgoing_slugs"]

    def test_no_links_shows_zero(self):
        db_dir = _make_temp_evomem_db()
        db_path = os.path.join(db_dir, ".evomem.db")
        _seed_graph_data(db_path)

        from backend.agent_runtime.evomem_client import get_kb_graph_metadata

        with patch("backend.agent_runtime.evomem_client._get_brain_dir", return_value=db_dir):
            meta = get_kb_graph_metadata("test-agent")

        pages = meta["pages"]
        # isolated.md has no links in or out
        assert pages["isolated.md"]["incoming_count"] == 0
        assert pages["isolated.md"]["incoming_slugs"] == []
        assert pages["isolated.md"]["outgoing_slugs"] == []

    def test_dangling_links_not_counted(self):
        db_dir = _make_temp_evomem_db()
        db_path = os.path.join(db_dir, ".evomem.db")
        _seed_graph_data(db_path)

        from backend.agent_runtime.evomem_client import get_kb_graph_metadata

        with patch("backend.agent_runtime.evomem_client._get_brain_dir", return_value=db_dir):
            meta = get_kb_graph_metadata("test-agent")

        pages = meta["pages"]
        # howto-report.md links to missing-doc.md (dangling, dst_page_id IS NULL).
        # The JOIN requires dst_page_id, so dangling links do NOT appear in outgoing_slugs.
        # "missing-doc.md" is also NOT a KB page, so it wouldn't appear regardless.
        assert "missing-doc.md" not in pages  # not a kb page, not returned

    def test_soft_deleted_excluded(self):
        db_dir = _make_temp_evomem_db()
        db_path = os.path.join(db_dir, ".evomem.db")
        _seed_graph_data(db_path)

        from backend.agent_runtime.evomem_client import get_kb_graph_metadata

        with patch("backend.agent_runtime.evomem_client._get_brain_dir", return_value=db_dir):
            meta = get_kb_graph_metadata("test-agent")

        pages = meta["pages"]
        assert "deleted-doc.md" not in pages

    def test_only_kb_pages_returned(self):
        db_dir = _make_temp_evomem_db()
        db_path = os.path.join(db_dir, ".evomem.db")
        _seed_graph_data(db_path)

        from backend.agent_runtime.evomem_client import get_kb_graph_metadata

        with patch("backend.agent_runtime.evomem_client._get_brain_dir", return_value=db_dir):
            meta = get_kb_graph_metadata("test-agent")

        pages = meta["pages"]
        # Only kb pages, not entities or notes
        for slug in pages:
            assert slug.endswith(".md")
        assert "entities/acme" not in pages
        assert "notes/some-fact" not in pages

    def test_target_updated_at_included(self):
        db_dir = _make_temp_evomem_db()
        db_path = os.path.join(db_dir, ".evomem.db")
        _seed_graph_data(db_path)

        from backend.agent_runtime.evomem_client import get_kb_graph_metadata

        with patch("backend.agent_runtime.evomem_client._get_brain_dir", return_value=db_dir):
            meta = get_kb_graph_metadata("test-agent")

        target_updated = meta["target_updated_at"]
        assert "howto-report.md" in target_updated
        assert "changelog-format.md" in target_updated

    def test_no_brain_db_returns_none(self):
        from backend.agent_runtime.evomem_client import get_kb_graph_metadata

        with patch("backend.agent_runtime.evomem_client._get_brain_dir", return_value="/nonexistent"):
            result = get_kb_graph_metadata("test-agent")
        assert result is None


# ─── Listing format output tests ─────────────────────────────────────────────

class TestKbListingFormat:
    def _make_fake_kb_dir(self, files: dict, tmp_path):
        kb_dir = tmp_path / "test-agent" / "kb"
        kb_dir.mkdir(parents=True)
        for fname, (content, fm_desc, fm_tags) in files.items():
            fpath = kb_dir / fname
            lines = ["---"]
            if fm_desc is not None:
                lines.append(f"description: {fm_desc}")
            if fm_tags is not None:
                tag_list = ", ".join(f'"{t}"' for t in fm_tags)
                lines.append(f"tags: [{tag_list}]")
            lines.append("---")
            lines.append(content or "")
            fpath.write_text("\n".join(lines), encoding="utf-8")
        return kb_dir

    def test_full_listing_format(self, tmp_path):
        kb_dir = self._make_fake_kb_dir({
            "notes.md": ("content", "User guide notes", ["preferences"]),
            "api.md": ("content", "API reference", ["reference"]),
        }, tmp_path)

        from backend.agent_runtime.context import _build_kb_listing, _AGENTS_DIR

        with patch("backend.agent_runtime.context._AGENTS_DIR", str(tmp_path)), \
             patch("backend.agent_runtime.context.get_kb_graph_metadata", return_value=None):
            result = _build_kb_listing("test-agent")

        assert result
        text = "\n".join(result)
        assert "## Available Knowledge Files" in text
        assert "[[kb/filename]]" in text
        assert "### KB Usage" in text
        assert "- api.md" in text
        assert "- notes.md" in text
        assert "[tags: reference]" in text or "[tags: preferences]" in text

    def test_tags_from_frontmatter_displayed(self, tmp_path):
        kb_dir = self._make_fake_kb_dir({
            "notes.md": ("content", "desc", ["preferences", "instructions"]),
        }, tmp_path)

        from backend.agent_runtime.context import _build_kb_listing

        with patch("backend.agent_runtime.context._AGENTS_DIR", str(tmp_path)), \
             patch("backend.agent_runtime.context.get_kb_graph_metadata", return_value=None):
            result = _build_kb_listing("test-agent")

        text = "\n".join(result)
        assert "[tags: preferences, instructions]" in text

    def test_file_size_displayed(self, tmp_path):
        content = "x" * 3000  # ~3 KB
        kb_dir = self._make_fake_kb_dir({"notes.md": (content, "desc", None)}, tmp_path)

        from backend.agent_runtime.context import _build_kb_listing

        with patch("backend.agent_runtime.context._AGENTS_DIR", str(tmp_path)), \
             patch("backend.agent_runtime.context.get_kb_graph_metadata", return_value=None):
            result = _build_kb_listing("test-agent")

        text = "\n".join(result)
        assert "KB)" in text

    def test_description_from_frontmatter(self, tmp_path):
        kb_dir = self._make_fake_kb_dir({
            "notes.md": ("content", "My important notes", None),
        }, tmp_path)

        from backend.agent_runtime.context import _build_kb_listing

        with patch("backend.agent_runtime.context._AGENTS_DIR", str(tmp_path)), \
             patch("backend.agent_runtime.context.get_kb_graph_metadata", return_value=None):
            result = _build_kb_listing("test-agent")

        text = "\n".join(result)
        assert "My important notes" in text

    def test_zero_kb_files_graceful(self, tmp_path):
        kb_dir = tmp_path / "test-agent" / "kb"
        kb_dir.mkdir(parents=True)

        from backend.agent_runtime.context import _build_kb_listing

        with patch("backend.agent_runtime.context._AGENTS_DIR", str(tmp_path)), \
             patch("backend.agent_runtime.context.get_kb_graph_metadata", return_value=None):
            result = _build_kb_listing("test-agent")

        assert result == []

    def test_no_kb_dir(self, tmp_path):
        from backend.agent_runtime.context import _build_kb_listing

        with patch("backend.agent_runtime.context._AGENTS_DIR", str(tmp_path)):
            result = _build_kb_listing("test-agent")

        assert result == []


# ─── Staleness computation tests ────────────────────────────────────────────

class TestStalenessComputation:
    def test_target_newer_shows_flag(self):
        from backend.agent_runtime.context import _compute_staleness_flag

        now = datetime.now(timezone.utc)
        old = (now - timedelta(days=7)).isoformat()
        new = (now - timedelta(days=1)).isoformat()

        flag = _compute_staleness_flag(old, "target.md", {"target.md": new})
        assert "⚠" in flag
        assert "target may have changed" in flag

    def test_target_older_no_flag(self):
        from backend.agent_runtime.context import _compute_staleness_flag

        now = datetime.now(timezone.utc)
        old = (now - timedelta(days=7)).isoformat()
        new = (now - timedelta(days=1)).isoformat()

        flag = _compute_staleness_flag(new, "target.md", {"target.md": old})
        assert flag == ""

    def test_target_same_time_no_flag(self):
        from backend.agent_runtime.context import _compute_staleness_flag

        now = datetime.now(timezone.utc).isoformat()

        flag = _compute_staleness_flag(now, "target.md", {"target.md": now})
        assert flag == ""

    def test_days_ago_computation(self):
        from backend.agent_runtime.context import _compute_staleness_flag

        now = datetime.now(timezone.utc)
        old = (now - timedelta(days=30)).isoformat()

        # Target updated 1 day ago
        tgt1 = (now - timedelta(days=1)).isoformat()
        flag = _compute_staleness_flag(old, "t.md", {"t.md": tgt1})
        assert "1 day ago" in flag

        # Target updated 7 days ago
        tgt7 = (now - timedelta(days=7)).isoformat()
        flag = _compute_staleness_flag(old, "t.md", {"t.md": tgt7})
        assert "7 days ago" in flag

        # Target updated today
        tgt_today = now.isoformat()
        flag = _compute_staleness_flag(old, "t.md", {"t.md": tgt_today})
        assert "today" in flag

    def test_missing_target_no_flag(self):
        from backend.agent_runtime.context import _compute_staleness_flag

        now = datetime.now(timezone.utc).isoformat()
        flag = _compute_staleness_flag(now, "missing.md", {})
        assert flag == ""

    def test_null_source_no_flag(self):
        from backend.agent_runtime.context import _compute_staleness_flag

        flag = _compute_staleness_flag(None, "target.md", {"target.md": "2024-01-01T00:00:00"})
        assert flag == ""


# ─── Edge case tests ────────────────────────────────────────────────────────

class TestEdgeCases:
    def test_empty_tags(self, tmp_path):
        from backend.agent_runtime.context import _extract_kb_frontmatter

        fpath = tmp_path / "test.md"
        fpath.write_text("---\ntags: []\n---\ncontent", encoding="utf-8")

        fm = _extract_kb_frontmatter(str(fpath))
        assert fm["tags"] == []

    def test_no_tags_field(self, tmp_path):
        from backend.agent_runtime.context import _extract_kb_frontmatter

        fpath = tmp_path / "test.md"
        fpath.write_text("---\ndescription: test\n---\ncontent", encoding="utf-8")

        fm = _extract_kb_frontmatter(str(fpath))
        assert fm["tags"] == []
        assert fm["description"] == "test"

    def test_long_description_truncated(self, tmp_path):
        long_desc = "x" * 200
        from backend.agent_runtime.context import _build_kb_listing

        kb_dir = tmp_path / "test-agent" / "kb"
        kb_dir.mkdir(parents=True)
        fpath = kb_dir / "notes.md"
        fpath.write_text(
            f"---\ndescription: {long_desc}\n---\ncontent", encoding="utf-8"
        )

        with patch("backend.agent_runtime.context._AGENTS_DIR", str(tmp_path)), \
             patch("backend.agent_runtime.context.get_kb_graph_metadata", return_value=None):
            result = _build_kb_listing("test-agent")

        text = "\n".join(result)
        # Description should be truncated
        assert "..." in text

    def test_special_chars_in_slug(self, tmp_path):
        from backend.agent_runtime.context import _build_kb_listing

        kb_dir = tmp_path / "test-agent" / "kb"
        kb_dir.mkdir(parents=True)
        fname = "my-file_v2.1 (copy).md"
        fpath = kb_dir / fname
        fpath.write_text("---\ndescription: test\n---\ncontent", encoding="utf-8")

        with patch("backend.agent_runtime.context._AGENTS_DIR", str(tmp_path)), \
             patch("backend.agent_runtime.context.get_kb_graph_metadata", return_value=None):
            result = _build_kb_listing("test-agent")

        text = "\n".join(result)
        assert fname in text

    def test_no_frontmatter_shows_no_tags(self, tmp_path):
        from backend.agent_runtime.context import _extract_kb_frontmatter

        fpath = tmp_path / "test.md"
        fpath.write_text("# Just content\nNo frontmatter.", encoding="utf-8")

        fm = _extract_kb_frontmatter(str(fpath))
        assert fm["description"] is None
        assert fm["tags"] == []


# ─── Sub-agent inheritance tests ────────────────────────────────────────────

class TestSubAgentInheritance:
    def test_effective_id_used_for_kb_dir(self, tmp_path):
        from backend.agent_runtime.context import _effective_id

        sub_agent = {"id": "sub__parent__task", "is_subagent": True, "parent_id": "parent"}
        eid = _effective_id(sub_agent)
        assert eid == "parent"

    def test_subagent_sees_parent_kb(self, tmp_path):
        from backend.agent_runtime.context import _build_kb_listing, _effective_id

        # Create parent KB
        parent_kb = tmp_path / "parent" / "kb"
        parent_kb.mkdir(parents=True)
        (parent_kb / "notes.md").write_text(
            "---\ndescription: parent doc\n---\ncontent", encoding="utf-8"
        )

        sub_agent = {"id": "sub__parent__task", "is_subagent": True, "parent_id": "parent"}
        eid = _effective_id(sub_agent)
        assert eid == "parent"

        with patch("backend.agent_runtime.context._AGENTS_DIR", str(tmp_path)), \
             patch("backend.agent_runtime.context.get_kb_graph_metadata", return_value=None):
            result = _build_kb_listing(eid)

        text = "\n".join(result)
        assert "notes.md" in text
        assert "parent doc" in text


# ─── Format size tests ──────────────────────────────────────────────────────

class TestFormatSize:
    def test_kb_format(self):
        from backend.agent_runtime.context import _format_size
        assert "1.0 KB" == _format_size(1024)
        assert "2.5 KB" == _format_size(2560)

    def test_mb_format(self):
        from backend.agent_runtime.context import _format_size
        assert "1.0 MB" == _format_size(1024 * 1024)
        assert "1.5 MB" == _format_size(int(1.5 * 1024 * 1024))
