"""Tests for evomem_writer.py — structured markdown writer.

These tests write to a temporary brain dir (no evomem binary needed): the
writer is pure Python. `_ensure_brain` is patched to True so writes proceed
without a real `.evomem.db`.
"""

import os
import pytest
from unittest.mock import patch

from backend.agent_runtime import evomem_writer as W


@pytest.fixture
def brain(tmp_path, monkeypatch):
    """Point the writer at a temp brain dir and skip the DB-existence check."""
    root = tmp_path / "agents" / "a1" / "brain"
    monkeypatch.setattr(W, "_get_evomem_dir", lambda agent_id: str(root))
    monkeypatch.setattr(W, "_ensure_brain", lambda agent_id: True)
    return root


class TestSlugify:
    def test_deterministic_and_ascii(self):
        assert W.slugify("Acme Corp") == "acme-corp"
        assert W.slugify("Acme Corp") == W.slugify("Acme Corp")

    def test_strips_accents_and_symbols(self):
        assert W.slugify("Café R&D!!") == "cafe-r-d"

    def test_empty(self):
        assert W.slugify("") == ""
        assert W.slugify("!!!") == ""


class TestUpsertDoc:
    def test_creates_doc_with_frontmatter_and_inline_links(self, brain):
        slug = W.upsert_doc("a1", title="Jakarta",
                            body="Ibu kota Indonesia; dekat [[Monas]].",
                            doc_type="place", description="Ibu kota Indonesia",
                            tags=["place"], aliases=["DKI Jakarta"])
        assert slug == "jakarta"
        text = (brain / "jakarta.md").read_text()
        assert 'title: "Jakarta"' in text
        assert "type: place" in text
        assert 'description: "Ibu kota Indonesia"' in text
        assert "[[Monas]]" in text             # inline link kept in the body
        assert "DKI Jakarta" in text           # alias
        assert "## Relationships" not in text  # no blockquote-edge section

    def test_places_doc_in_collection_folder(self, brain):
        slug = W.upsert_doc("a1", title="Monas", body="Tugu di [[Jakarta]].",
                            doc_type="place", description="Monas", folder="riset-jkt")
        assert slug == "riset-jkt/monas"
        assert (brain / "riset-jkt" / "monas.md").exists()

    def test_rewrite_preserves_created_and_merges_tags(self, brain):
        import re
        W.upsert_doc("a1", title="X", body="a", doc_type="note",
                     description="d", tags=["t1"])
        created = re.search(r"created: (\S+)", (brain / "x.md").read_text()).group(1)
        W.upsert_doc("a1", title="X", body="b", doc_type="note",
                     description="d", tags=["t2"])
        text = (brain / "x.md").read_text()
        assert f"created: {created}" in text          # created preserved
        assert "t1" in text and "t2" in text          # tags merged (union)


class TestSessionGroupCoercion:
    """session/group are reserved for collection index.md; a standalone doc that
    asks for them is coerced to `note` (prevents stray flat 'session' files)."""

    def test_session_coerced_to_note(self, brain):
        W.upsert_doc("a1", title="Trip", body="x", doc_type="session", description="d")
        text = (brain / "trip.md").read_text()
        assert "type: note" in text and "type: session" not in text

    def test_group_coerced_to_note(self, brain):
        W.upsert_doc("a1", title="Grp", body="x", doc_type="group", description="d")
        assert "type: note" in (brain / "grp.md").read_text()


class TestAppendToDoc:
    def test_appends_paragraph_preserving_body(self, brain):
        W.upsert_doc("a1", title="Jakarta", body="Paragraf satu.",
                     doc_type="place", description="d")
        assert W.append_to_doc("a1", "jakarta", "Paragraf dua dengan [[Monas]].")
        text = (brain / "jakarta.md").read_text()
        assert "Paragraf satu." in text and "Paragraf dua" in text
        assert "[[Monas]]" in text

    def test_returns_false_when_doc_missing(self, brain):
        assert W.append_to_doc("a1", "nope", "x") is False


class TestCollections:
    def test_create_collection_writes_typed_index(self, brain):
        folder = W.create_collection("a1", folder="Riset XYZ", title="Riset XYZ",
                                     kind="session", description="riset")
        assert folder == "riset-xyz"
        idx = (brain / "riset-xyz" / "index.md").read_text()
        assert "type: session" in idx and "## Contents" in idx

    def test_create_collection_rejects_bad_kind(self, brain):
        assert W.create_collection("a1", "X", "X", kind="bogus", description="d") == ""

    def test_add_to_collection_index_idempotent(self, brain):
        W.create_collection("a1", "Riset", "Riset", kind="group", description="d")
        assert W.add_to_collection_index("a1", "riset", "Monas") is True
        assert "[[Monas]]" in (brain / "riset" / "index.md").read_text()
        # second add of the same title is a no-op
        assert W.add_to_collection_index("a1", "riset", "Monas") is False


class TestReadDoc:
    def test_reads_frontmatter_and_body(self, brain):
        W.upsert_doc("a1", title="Jakarta", body="Body dengan [[Monas]].",
                     doc_type="place", description="d")
        doc = W.read_doc("a1", "jakarta")
        assert doc["title"] == "Jakarta"
        assert doc["frontmatter"]["type"] == "place"
        assert "[[Monas]]" in doc["body"]

    def test_missing_returns_none(self, brain):
        assert W.read_doc("a1", "nope") is None


class TestMarkDirty:
    def test_schedules_debounced_sync(self, monkeypatch):
        calls = []
        monkeypatch.setattr(W, "_SYNC_DEBOUNCE_SECONDS", 0.05)
        monkeypatch.setattr(W, "_evomem_sync", lambda agent_id: calls.append(agent_id))
        W.mark_dirty("a1")
        import time
        time.sleep(0.2)
        assert calls == ["a1"]

    def test_bursts_coalesce_into_one_sync(self, monkeypatch):
        calls = []
        monkeypatch.setattr(W, "_SYNC_DEBOUNCE_SECONDS", 0.1)
        monkeypatch.setattr(W, "_evomem_sync", lambda agent_id: calls.append(agent_id))
        for _ in range(5):
            W.mark_dirty("a1")
        import time
        time.sleep(0.3)
        assert calls == ["a1"]  # coalesced
