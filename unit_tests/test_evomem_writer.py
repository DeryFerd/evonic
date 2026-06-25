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


class TestUpsertEntity:
    def test_creates_page(self, brain):
        slug = W.upsert_entity_page("a1", "Acme Corp", aliases=["Acme"],
                                    tags=["organization"])
        assert slug == "entities/acme-corp"
        path = brain / "entities" / "acme-corp.md"
        assert path.exists()
        text = path.read_text()
        assert "title: \"Acme Corp\"" in text
        assert "Acme" in text  # alias
        assert "organization" in text

    def test_merges_aliases_without_clobber(self, brain):
        W.upsert_entity_page("a1", "Acme Corp", aliases=["Acme"])
        W.upsert_entity_page("a1", "Acme Corp", aliases=["ACME Inc"], tags=["org"])
        text = (brain / "entities" / "acme-corp.md").read_text()
        assert "Acme" in text and "ACME Inc" in text  # both aliases preserved


class TestAddEdge:
    def test_appends_typed_edge(self, brain):
        W.upsert_entity_page("a1", "Robin")
        ok = W.add_edge("a1", "entities/robin", "works_at", "entities/acme-corp",
                        anchor="Acme Corp")
        assert ok
        text = (brain / "entities" / "robin.md").read_text()
        assert "## Relationships" in text
        assert "> **works_at:** [Acme Corp](entities/acme-corp)" in text

    def test_idempotent(self, brain):
        W.upsert_entity_page("a1", "Robin")
        W.add_edge("a1", "entities/robin", "works_at", "entities/acme")
        W.add_edge("a1", "entities/robin", "works_at", "entities/acme")
        text = (brain / "entities" / "robin.md").read_text()
        assert text.count("**works_at:**") == 1

    def test_unknown_edge_falls_back_to_mentions(self, brain):
        W.upsert_entity_page("a1", "Robin")
        W.add_edge("a1", "entities/robin", "bogus_rel", "entities/x")
        text = (brain / "entities" / "robin.md").read_text()
        assert "**mentions:**" in text


class TestWriteNote:
    def test_writes_note_with_mentions_and_memory_id(self, brain):
        slug = W.write_note("a1", "Lang preference",
                            "User prefers English.", tags=["preference"],
                            mentions=["entities/user"], memory_id=7)
        assert slug == "notes/mem-7"
        text = (brain / "notes" / "mem-7.md").read_text()
        assert "memory_id: 7" in text
        assert "[[entities/user]]" in text
        assert "User prefers English." in text

    def test_memory_id_makes_idempotent_path(self, brain):
        W.write_note("a1", "v1", "first", memory_id=3)
        W.write_note("a1", "v2", "second", memory_id=3)
        # same memory_id → same file (upsert, not duplicate)
        notes = list((brain / "notes").glob("*.md"))
        assert len(notes) == 1
        assert "second" in notes[0].read_text()


class TestDeleteNote:
    def test_removes_note_file(self, brain):
        W.write_note("a1", "t", "body", memory_id=9)
        path = brain / "notes" / "mem-9.md"
        assert path.exists()
        assert W.delete_note("a1", 9) is True
        assert not path.exists()

    def test_returns_false_when_absent(self, brain):
        assert W.delete_note("a1", 123) is False

    def test_returns_false_for_none_id(self, brain):
        assert W.delete_note("a1", None) is False


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
