"""
Tests for _kb_index.md canonical index and agent KB coaching (Phase 2D+2E).
"""
import os
import sys
import tempfile

import pytest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ─── helpers ────────────────────────────────────────────────────────────────

def _make_kb_dir(tmp_path, files: dict, include_index: bool = False):
    """Create a fake KB directory with given files. files: {name: content}.

    _build_kb_listing reads <_AGENTS_DIR>/<agent_id>/kb, and tests patch
    _AGENTS_DIR to tmp_path and call it with agent_id "test-agent", so the
    files must live under tmp_path/test-agent/kb.
    """
    kb_dir = tmp_path / "test-agent" / "kb"
    kb_dir.mkdir(parents=True)
    for fname, content in files.items():
        (kb_dir / fname).write_text(content, encoding="utf-8")
    return kb_dir


def _make_mock_db():
    m = MagicMock()
    m.get_setting.return_value = None
    m.get_agent_tools.return_value = set()
    m.get_agent_skills.return_value = []
    m.get_agent_variables.return_value = []
    return m


# ─── _kb_index.md creation tests ───────────────────────────────────────────

class TestKbIndexCreation:
    def test_frontmatter_correct(self, tmp_path):
        idx = tmp_path / "_kb_index.md"
        idx.write_text(
            '---\ndescription: Canonical index\ntags: [meta, index]\n---\n\n# Index\n',
            encoding="utf-8",
        )
        from backend.agent_runtime.context import _extract_kb_frontmatter
        fm = _extract_kb_frontmatter(str(idx))
        assert fm["description"] == "Canonical index"
        assert "meta" in fm["tags"]
        assert "index" in fm["tags"]


# ─── KB listing integration tests ──────────────────────────────────────────

class TestKbListingIntegration:
    def test_index_content_shown(self, tmp_path):
        kb_dir = _make_kb_dir(tmp_path, {
            "_kb_index.md": '---\ndescription: Index\ntags: [meta, index]\n---\n\n# KB Index\n\n- [[kb/test-doc]]\n',
            "test-doc.md": '---\ndescription: Test\n---\ncontent',
        })
        from backend.agent_runtime.context import _build_kb_listing

        with patch("backend.agent_runtime.context._AGENTS_DIR", str(tmp_path)), \
             patch("backend.agent_runtime.context.get_kb_graph_metadata", return_value=None):
            result = _build_kb_listing("test-agent")

        text = "\n".join(result)
        assert "# KB Index" in text
        assert "[[kb/test-doc]]" in text
        assert 'read_file(file_path="/_self/kb/_kb_index.md")' in text

    def test_graph_metadata_after_index(self, tmp_path):
        kb_dir = _make_kb_dir(tmp_path, {
            "_kb_index.md": '---\ndescription: Index\ntags: [meta, index]\n---\n\n# Index\n',
            "doc.md": '---\ndescription: Doc\n---\ncontent',
        })
        from backend.agent_runtime.context import _build_kb_listing

        with patch("backend.agent_runtime.context._AGENTS_DIR", str(tmp_path)), \
             patch("backend.agent_runtime.context.get_kb_graph_metadata", return_value=None):
            result = _build_kb_listing("test-agent")

        text = "\n".join(result)
        idx_pos = text.find("# Index")
        graph_pos = text.find("### Graph metadata")
        assert idx_pos != -1
        assert graph_pos != -1
        assert graph_pos > idx_pos, "Graph metadata should come after index content"

    def test_index_missing_fallback(self, tmp_path):
        kb_dir = _make_kb_dir(tmp_path, {
            "guide.md": '---\ndescription: Guide\n---\ncontent',
        })
        from backend.agent_runtime.context import _build_kb_listing

        with patch("backend.agent_runtime.context._AGENTS_DIR", str(tmp_path)), \
             patch("backend.agent_runtime.context.get_kb_graph_metadata", return_value=None):
            result = _build_kb_listing("test-agent")

        text = "\n".join(result)
        assert "### Graph metadata" not in text  # fallback uses per-file format
        assert "guide.md" in text
        # Empty graph relations are omitted (no noisy "<none>" lines)
        assert "referenced by" not in text
        assert "references:" not in text

    def test_kb_index_excluded_from_metadata(self, tmp_path):
        kb_dir = _make_kb_dir(tmp_path, {
            "_kb_index.md": '---\ndescription: Index\ntags: [meta, index]\n---\n\n# Index\n',
            "notes.md": '---\ndescription: Notes\n---\ncontent',
        })
        from backend.agent_runtime.context import _build_kb_listing

        with patch("backend.agent_runtime.context._AGENTS_DIR", str(tmp_path)), \
             patch("backend.agent_runtime.context.get_kb_graph_metadata", return_value=None):
            result = _build_kb_listing("test-agent")

        text = "\n".join(result)
        # _kb_index.md should NOT be listed as a file entry in graph metadata.
        # (It is legitimately mentioned elsewhere, e.g. the KB coaching text,
        # so match the per-file entry format specifically.)
        assert "- _kb_index.md:" not in text

    def test_empty_kb_dir(self, tmp_path):
        kb_dir = tmp_path / "test-agent" / "kb"
        kb_dir.mkdir(parents=True)
        from backend.agent_runtime.context import _build_kb_listing

        with patch("backend.agent_runtime.context._AGENTS_DIR", str(tmp_path)):
            assert _build_kb_listing("test-agent") == []


# ─── Listing cap tests (top-N recent) ──────────────────────────────────────

class TestKbListingCap:
    """The file listing is capped at _KB_LISTING_LIMIT (5) recent files; the
    rest are summarized with a count + a `recall` pointer."""

    def _set_mtimes_desc(self, kb_dir, names):
        """Stamp mtimes so names[0] is oldest and names[-1] is newest."""
        for i, name in enumerate(names):
            ts = 1_000_000 + i * 100
            os.utime(kb_dir / name, (ts, ts))

    def test_fallback_caps_at_five(self, tmp_path):
        import re
        names = [f"doc{i:02d}.md" for i in range(8)]
        files = {n: f'---\ndescription: D{n}\n---\ncontent' for n in names}
        kb_dir = _make_kb_dir(tmp_path, files)
        self._set_mtimes_desc(kb_dir, names)  # doc07 newest .. doc00 oldest

        from backend.agent_runtime.context import _build_kb_listing
        with patch("backend.agent_runtime.context._AGENTS_DIR", str(tmp_path)), \
             patch("backend.agent_runtime.context.get_kb_graph_metadata", return_value=None):
            result = _build_kb_listing("test-agent")
        text = "\n".join(result)

        listed = re.findall(r"^- doc\d+\.md ", text, flags=re.MULTILINE)
        assert len(listed) == 5, f"expected 5 listed, got {len(listed)}"
        # Most recent five shown; oldest three omitted from the per-file listing.
        for n in ["doc07.md", "doc06.md", "doc05.md", "doc04.md", "doc03.md"]:
            assert f"- {n} " in text
        for n in ["doc00.md", "doc01.md", "doc02.md"]:
            assert f"- {n} " not in text
        # Truncation summary + recall pointer present.
        assert "…and 3 more" in text
        assert "recall(query=" in text

    def test_fallback_no_truncation_when_five_or_fewer(self, tmp_path):
        import re
        names = [f"doc{i:02d}.md" for i in range(4)]
        files = {n: f'---\ndescription: D{n}\n---\ncontent' for n in names}
        kb_dir = _make_kb_dir(tmp_path, files)

        from backend.agent_runtime.context import _build_kb_listing
        with patch("backend.agent_runtime.context._AGENTS_DIR", str(tmp_path)), \
             patch("backend.agent_runtime.context.get_kb_graph_metadata", return_value=None):
            result = _build_kb_listing("test-agent")
        text = "\n".join(result)

        listed = re.findall(r"^- doc\d+\.md ", text, flags=re.MULTILINE)
        assert len(listed) == 4
        assert "more knowledge file" not in text

    def test_index_branch_caps_graph_metadata(self, tmp_path):
        import re
        names = [f"doc{i:02d}.md" for i in range(8)]
        files = {n: f'---\ndescription: D{n}\n---\ncontent' for n in names}
        files["_kb_index.md"] = '---\ndescription: Index\ntags: [meta, index]\n---\n\n# KB Index\n'
        kb_dir = _make_kb_dir(tmp_path, files)
        self._set_mtimes_desc(kb_dir, names)

        from backend.agent_runtime.context import _build_kb_listing
        with patch("backend.agent_runtime.context._AGENTS_DIR", str(tmp_path)), \
             patch("backend.agent_runtime.context.get_kb_graph_metadata", return_value=None):
            result = _build_kb_listing("test-agent")
        text = "\n".join(result)

        # graph_pages is None → "no graph data" per-file lines; capped at 5.
        listed = re.findall(r"^- doc\d+\.md: ", text, flags=re.MULTILINE)
        assert len(listed) == 5
        assert "…and 3 more" in text
        assert "recall(query=" in text

    def test_notes_md_excluded_from_listing(self, tmp_path):
        kb_dir = _make_kb_dir(tmp_path, {
            "notes.md": '---\ndescription: Notes\n---\ncontent',
            "guide.md": '---\ndescription: Guide\n---\ncontent',
        })
        from backend.agent_runtime.context import _build_kb_listing
        with patch("backend.agent_runtime.context._AGENTS_DIR", str(tmp_path)), \
             patch("backend.agent_runtime.context.get_kb_graph_metadata", return_value=None):
            result = _build_kb_listing("test-agent")
        text = "\n".join(result)

        # notes.md has its own dedicated section, so it must NOT appear as a
        # per-file listing entry; guide.md should.
        assert "- notes.md (" not in text
        assert "- guide.md (" in text


# ─── Agent coaching tests ───────────────────────────────────────────────────

class TestKbCoaching:
    def test_coaching_injected(self, tmp_path):
        kb_dir = _make_kb_dir(tmp_path, {
            "notes.md": '---\ndescription: Notes\n---\ncontent',
        })
        from backend.agent_runtime.context import _build_kb_listing

        with patch("backend.agent_runtime.context._AGENTS_DIR", str(tmp_path)), \
             patch("backend.agent_runtime.context.get_kb_graph_metadata", return_value=None):
            result = _build_kb_listing("test-agent")

        text = "\n".join(result)
        assert "### KB Coaching" in text
        assert "[[Doc Title]]" in text  # Obsidian-style inline links
        assert "mode='links'" in text   # recall links-neighborhood guidance

    def test_coaching_token_budget(self, tmp_path):
        kb_dir = _make_kb_dir(tmp_path, {
            "notes.md": '---\ndescription: Notes\n---\ncontent',
        })
        from backend.agent_runtime.context import _build_kb_listing

        with patch("backend.agent_runtime.context._AGENTS_DIR", str(tmp_path)), \
             patch("backend.agent_runtime.context.get_kb_graph_metadata", return_value=None):
            result = _build_kb_listing("test-agent")

        text = "\n".join(result)
        # Find coaching section
        coaching_start = text.find("### KB Coaching")
        coaching_end = text.find("\n\n", coaching_start + 20)
        if coaching_end == -1:
            coaching_end = len(text)
        coaching_text = text[coaching_start:coaching_end]
        # Rough token estimate: ~4 chars per token
        est_tokens = len(coaching_text.split())
        assert est_tokens <= 50, f"Coaching text ~{est_tokens} tokens, should be <=50"


# ─── Wiki-link validation tests ────────────────────────────────────────────

class TestWikiLinkValidation:
    def test_broken_link_handled(self, tmp_path):
        """A _kb_index.md with a broken link should not crash."""
        kb_dir = _make_kb_dir(tmp_path, {
            "_kb_index.md": '---\ndescription: Index\ntags: [meta, index]\n---\n\n- [[kb/nonexistent-doc]]\n',
            "real-doc.md": '---\ndescription: Real\n---\ncontent',
        })
        from backend.agent_runtime.context import _build_kb_listing

        with patch("backend.agent_runtime.context._AGENTS_DIR", str(tmp_path)), \
             patch("backend.agent_runtime.context.get_kb_graph_metadata", return_value=None):
            result = _build_kb_listing("test-agent")

        text = "\n".join(result)
        assert "[[kb/nonexistent-doc]]" in text


# ─── Edge case tests ──────────────────────────────────────────────────────

class TestEdgeCases:
    def test_one_kb_file(self, tmp_path):
        kb_dir = _make_kb_dir(tmp_path, {
            "_kb_index.md": '---\ndescription: Index\ntags: [meta, index]\n---\n\n# Index\n- [[kb/only-doc]]\n',
            "only-doc.md": '---\ndescription: Only\n---\ncontent',
        })
        from backend.agent_runtime.context import _build_kb_listing

        with patch("backend.agent_runtime.context._AGENTS_DIR", str(tmp_path)), \
             patch("backend.agent_runtime.context.get_kb_graph_metadata", return_value=None):
            result = _build_kb_listing("test-agent")

        text = "\n".join(result)
        assert "# Index" in text
        assert "only-doc.md" in text

    def test_non_ascii_descriptions(self, tmp_path):
        kb_dir = _make_kb_dir(tmp_path, {
            "_kb_index.md": '---\ndescription: Indeks kanonis\ntags: [meta, index]\n---\n\n# Indeks KB\n- [[kb/panduan]] — Panduan penggunaan\n',
            "panduan.md": '---\ndescription: "Panduan penggunaan"\n---\ncontent',
        })
        from backend.agent_runtime.context import _build_kb_listing

        with patch("backend.agent_runtime.context._AGENTS_DIR", str(tmp_path)), \
             patch("backend.agent_runtime.context.get_kb_graph_metadata", return_value=None):
            result = _build_kb_listing("test-agent")

        text = "\n".join(result)
        assert "Indeks KB" in text or "Indeks" in text

    def test_index_tags_prevent_treatment_as_regular(self, tmp_path):
        """_kb_index.md tags [meta, index] mark it as special — frontmatter
        extraction should surface those tags."""
        from backend.agent_runtime.context import _extract_kb_frontmatter
        idx = tmp_path / "_kb_index.md"
        idx.write_text(
            '---\ndescription: Canonical index\ntags: [meta, index]\n---\n\n# KB Index\n',
            encoding="utf-8",
        )
        fm = _extract_kb_frontmatter(str(idx))
        assert "meta" in fm["tags"] or "index" in fm["tags"]
