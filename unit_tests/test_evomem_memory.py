"""Integration tests for evomem + FTS5 primary+fallback in memory_manager."""

import pytest
from unittest.mock import patch, MagicMock


class TestEngineSelection:
    def test_evomem_is_default_when_available(self, monkeypatch):
        monkeypatch.delenv("EVONIC_MEMORY_ENGINE", raising=False)
        monkeypatch.setattr(
            "backend.agent_runtime.evomem_client.is_available", lambda: True)
        from backend.agent_runtime.evomem_client import get_engine
        assert get_engine() == "evomem"

    def test_downgrades_to_fts5_when_binary_unavailable(self, monkeypatch):
        monkeypatch.delenv("EVONIC_MEMORY_ENGINE", raising=False)
        monkeypatch.setattr(
            "backend.agent_runtime.evomem_client.is_available", lambda: False)
        from backend.agent_runtime.evomem_client import get_engine
        assert get_engine() == "fts5"

    def test_evomem_when_env_set(self, monkeypatch):
        monkeypatch.setenv("EVONIC_MEMORY_ENGINE", "evomem")
        monkeypatch.setattr(
            "backend.agent_runtime.evomem_client.is_available", lambda: True)
        from backend.agent_runtime.evomem_client import get_engine
        assert get_engine() == "evomem"

    def test_explicit_fts5_overrides_default(self, monkeypatch):
        monkeypatch.setenv("EVONIC_MEMORY_ENGINE", "fts5")
        from backend.agent_runtime.evomem_client import get_engine
        assert get_engine() == "fts5"

    def test_invalid_env_defaults_to_evomem(self, monkeypatch):
        monkeypatch.setenv("EVONIC_MEMORY_ENGINE", "bogus")
        monkeypatch.setattr(
            "backend.agent_runtime.evomem_client.is_available", lambda: True)
        from backend.agent_runtime.evomem_client import get_engine
        assert get_engine() == "evomem"


class TestGetMemoriesForContext:
    """Test get_memories_for_context with mocked database."""

    def test_fts5_returns_formatted_markdown(self, monkeypatch):
        monkeypatch.setenv("EVONIC_MEMORY_ENGINE", "fts5")
        monkeypatch.delenv("EVONIC_MEMORY_ENGINE", raising=False)
        fake_memories = [
            {"id": 1, "content": "User prefers Python", "category": "preference"},
        ]
        with patch("backend.agent_runtime.memory_manager.db.search_memories",
                   return_value=fake_memories):
            from backend.agent_runtime.memory_manager import get_memories_for_context
            msgs = [{"role": "user", "content": "What language?"}]
            result = get_memories_for_context("test-agent", msgs)
            assert result is not None
            assert "User prefers Python" in result
            assert "## Memory" in result

    def test_fts5_no_memories_returns_none(self, monkeypatch):
        monkeypatch.setenv("EVONIC_MEMORY_ENGINE", "fts5")
        with patch("backend.agent_runtime.memory_manager.db.search_memories", return_value=[]), \
             patch("backend.agent_runtime.memory_manager.db.get_recent_memories", return_value=[]):
            from backend.agent_runtime.memory_manager import get_memories_for_context
            msgs = [{"role": "user", "content": "query"}]
            result = get_memories_for_context("test-agent", msgs)
            assert result is None

    def test_fts5_no_user_message_uses_recent(self, monkeypatch):
        monkeypatch.setenv("EVONIC_MEMORY_ENGINE", "fts5")
        fake_recent = [
            {"id": 2, "content": "User prefers Golang", "category": "preference"},
        ]
        with patch("backend.agent_runtime.memory_manager.db.search_memories", return_value=[]), \
             patch("backend.agent_runtime.memory_manager.db.get_recent_memories", return_value=fake_recent):
            from backend.agent_runtime.memory_manager import get_memories_for_context
            msgs = []  # No user message
            result = get_memories_for_context("test-agent", msgs)
            assert result is not None
            assert "User prefers Golang" in result

    def test_evomem_primary_fallback_to_fts5(self, monkeypatch):
        """When evomem is primary but fails, fall back to FTS5."""
        monkeypatch.setenv("EVONIC_MEMORY_ENGINE", "evomem")
        fake_fts5 = [
            {"id": 3, "content": "User prefers Rust", "category": "preference"},
        ]
        with patch(
            "backend.agent_runtime.memory_manager._try_evomem_retrieval",
            return_value=None  # evomem fails
        ), patch(
            "backend.agent_runtime.memory_manager.db.search_memories",
            return_value=fake_fts5
        ):
            from backend.agent_runtime.memory_manager import get_memories_for_context
            msgs = [{"role": "user", "content": "language preference"}]
            result = get_memories_for_context("test-agent", msgs)
            assert result is not None
            assert "User prefers Rust" in result


class TestStoreMemory:
    """`store_memory` (the `remember` tool) pins a fact into the running session
    summary — no LLM, no direct long-term write. The summarizer persists it later."""

    def test_pins_to_new_session_summary(self):
        with patch("backend.agent_runtime.memory_manager.db.get_summary", return_value=None), \
             patch("backend.agent_runtime.memory_manager.db.upsert_summary") as upsert:
            from backend.agent_runtime.memory_manager import store_memory
            result = store_memory("test-agent", "sess-1", "Test fact", "preference")
            assert result["result"].startswith("Noted")
            assert result["content"] == "Test fact"
            # Fresh summary seeded with the noted bullet; watermarks at zero.
            args, kwargs = upsert.call_args
            assert args[0] == "sess-1"
            assert "- (noted, preference) Test fact" in args[1]
            assert args[2] == 0 and args[3] == 0

    def test_appends_to_existing_summary_preserving_watermarks(self):
        rec = {"summary": "Prior text.", "last_message_id": 42,
               "message_count": 7, "last_message_ts": 99}
        with patch("backend.agent_runtime.memory_manager.db.get_summary", return_value=rec), \
             patch("backend.agent_runtime.memory_manager.db.upsert_summary") as upsert:
            from backend.agent_runtime.memory_manager import store_memory
            store_memory("test-agent", "sess-1", "Phone 555", "user_info")
            args, kwargs = upsert.call_args
            assert "Prior text." in args[1]
            assert "- (noted, user_info) Phone 555" in args[1]
            assert args[2] == 42 and args[3] == 7
            assert kwargs.get("last_message_ts") == 99

    def test_makes_no_llm_or_longterm_write(self):
        with patch("backend.agent_runtime.memory_manager.db.get_summary", return_value=None), \
             patch("backend.agent_runtime.memory_manager.db.upsert_summary"), \
             patch("backend.agent_runtime.memory_manager.db.add_memory") as add_mem, \
             patch("backend.agent_runtime.memory_manager._try_evomem_store") as evo, \
             patch("backend.agent_runtime.memory_manager.llm_client.chat_completion") as llm:
            from backend.agent_runtime.memory_manager import store_memory
            store_memory("test-agent", "sess-1", "Test fact", "general")
            add_mem.assert_not_called()
            evo.assert_not_called()
            llm.assert_not_called()

    def test_empty_content_returns_error(self):
        from backend.agent_runtime.memory_manager import store_memory
        result = store_memory("test-agent", "sess-1", "", "general")
        assert "error" in result

    def test_missing_session_returns_error(self):
        from backend.agent_runtime.memory_manager import store_memory
        result = store_memory("test-agent", "", "Test fact", "general")
        assert "error" in result


class TestSearchMemories:
    def test_fts5_search_returns_results(self, monkeypatch):
        monkeypatch.setenv("EVONIC_MEMORY_ENGINE", "fts5")
        fake = [{"id": 1, "content": "User prefers Python", "category": "preference",
                 "created_at": "2026-01-01"}]
        with patch("backend.agent_runtime.memory_manager.db.search_memories", return_value=fake):
            from backend.agent_runtime.memory_manager import search_memories
            result = search_memories("test-agent", "Python")
            assert result["count"] == 1
            assert result["memories"][0]["content"] == "User prefers Python"

    def test_fts5_search_no_match_returns_empty(self, monkeypatch):
        monkeypatch.setenv("EVONIC_MEMORY_ENGINE", "fts5")
        with patch("backend.agent_runtime.memory_manager.db.search_memories", return_value=[]), \
             patch("backend.agent_runtime.memory_manager.db.get_recent_memories", return_value=[]):
            from backend.agent_runtime.memory_manager import search_memories
            result = search_memories("test-agent", "nonexistent")
            assert result["count"] == 0
            assert result["memories"] == []

    def test_evomem_search_falls_back_to_fts5_when_unavailable(self, monkeypatch):
        monkeypatch.setenv("EVONIC_MEMORY_ENGINE", "evomem")
        fake_fts5 = [{"id": 1, "content": "User prefers Python", "category": "preference",
                      "created_at": "2026-01-01"}]
        with patch(
            "backend.agent_runtime.memory_manager.evomem_search",
            return_value=None  # evomem unavailable
        ), patch(
            "backend.agent_runtime.memory_manager.db.search_memories",
            return_value=fake_fts5
        ):
            from backend.agent_runtime.memory_manager import search_memories
            result = search_memories("test-agent", "Python")
            assert result["count"] == 1
            assert result["memories"][0]["content"] == "User prefers Python"


class TestEvomemRetrievalFormatting:
    def test_skips_when_not_evomem_engine(self, monkeypatch):
        monkeypatch.setenv("EVONIC_MEMORY_ENGINE", "fts5")
        from backend.agent_runtime.memory_manager import _try_evomem_retrieval
        result = _try_evomem_retrieval("test-agent", "query")
        assert result is None

    def test_formats_evomem_hits_into_markdown(self, monkeypatch):
        monkeypatch.setenv("EVONIC_MEMORY_ENGINE", "evomem")
        # is_available() checks a cwd-relative binary path, so force it True to
        # keep get_engine() == "evomem" regardless of the test's working dir.
        monkeypatch.setattr(
            "backend.agent_runtime.evomem_client.is_available", lambda: True)
        fake_hits = {
            "query": "preference",
            "hits": [
                {
                    "rank": 1,
                    "slug": "inbox/fact-1",
                    "title": "User prefers Javanese",
                    "snippet": "User prefers Javanese language",
                    "evidence": "exact_title_match",
                    "source_dir": "inbox",
                    "score": 0.05,
                }
            ]
        }
        with patch(
            "backend.agent_runtime.memory_manager.evomem_search",
            return_value=fake_hits
        ):
            from backend.agent_runtime.memory_manager import _try_evomem_retrieval
            result = _try_evomem_retrieval("test-agent", "preference", limit=8)
            assert result is not None
            assert "## Memory (Evomem)" in result
            assert "User prefers Javanese" in result
            assert "exact_title_match" in result

    def test_returns_none_when_evomem_search_fails(self, monkeypatch):
        monkeypatch.setenv("EVONIC_MEMORY_ENGINE", "evomem")
        with patch(
            "backend.agent_runtime.memory_manager.evomem_search",
            side_effect=Exception("connection refused")
        ):
            from backend.agent_runtime.memory_manager import _try_evomem_retrieval
            result = _try_evomem_retrieval("test-agent", "query")
            assert result is None

    def test_returns_none_when_no_hits(self, monkeypatch):
        monkeypatch.setenv("EVONIC_MEMORY_ENGINE", "evomem")
        with patch(
            "backend.agent_runtime.memory_manager.evomem_search",
            return_value={"query": "test", "hits": [], "cached": False}
        ):
            from backend.agent_runtime.memory_manager import _try_evomem_retrieval
            result = _try_evomem_retrieval("test-agent", "query")
            assert result is None


class TestEvomemStore:
    def test_skips_when_not_evomem_engine(self, monkeypatch):
        monkeypatch.setenv("EVONIC_MEMORY_ENGINE", "fts5")
        from backend.agent_runtime.memory_manager import _try_evomem_store
        result = _try_evomem_store("test-agent", "fact", "general")
        assert result is False

    def test_returns_false_when_write_fails(self, monkeypatch):
        monkeypatch.setenv("EVONIC_MEMORY_ENGINE", "evomem")
        with patch(
            "backend.agent_runtime.memory_manager.evomem_writer.write_note",
            return_value=""
        ), patch(
            "backend.agent_runtime.memory_manager.evomem_writer.upsert_entity_page",
            return_value="entities/user"
        ), patch(
            "backend.agent_runtime.memory_manager.evomem_writer.mark_dirty"
        ):
            from backend.agent_runtime.memory_manager import _try_evomem_store
            result = _try_evomem_store("test-agent", "fact", "general")
            assert result is False

    def test_returns_true_when_write_succeeds(self, monkeypatch):
        monkeypatch.setenv("EVONIC_MEMORY_ENGINE", "evomem")
        # is_available() checks a cwd-relative binary path, so force it True to
        # keep get_engine() == "evomem" regardless of the test's working dir.
        monkeypatch.setattr(
            "backend.agent_runtime.evomem_client.is_available", lambda: True)
        with patch(
            "backend.agent_runtime.memory_manager.evomem_writer.write_note",
            return_value="notes/mem-1"
        ), patch(
            "backend.agent_runtime.memory_manager.evomem_writer.upsert_entity_page",
            return_value="entities/user"
        ), patch(
            "backend.agent_runtime.memory_manager.evomem_writer.mark_dirty"
        ) as mock_dirty:
            from backend.agent_runtime.memory_manager import _try_evomem_store
            result = _try_evomem_store("test-agent", "fact", "preference",
                                         memory_id=1)
            assert result is True
            mock_dirty.assert_called_once()


class TestSupersedeCleanup:
    def test_superseded_notes_deleted_from_evomem(self, monkeypatch):
        monkeypatch.setenv("EVONIC_MEMORY_ENGINE", "evomem")
        monkeypatch.setattr(
            "backend.agent_runtime.evomem_client.is_available", lambda: True)
        with patch("backend.agent_runtime.memory_manager._extract_dimension",
                   return_value="user.city"), \
             patch("backend.agent_runtime.memory_manager._backfill_null_dimensions"), \
             patch("backend.agent_runtime.memory_manager.db.get_memories_by_dimension",
                   return_value=[{"id": 7}, {"id": 8}]), \
             patch("backend.agent_runtime.memory_manager.db.add_memory", return_value=9), \
             patch("backend.agent_runtime.memory_manager.db.supersede_memory"), \
             patch("backend.agent_runtime.memory_manager.evomem_writer.delete_note",
                   return_value=True) as mock_del, \
             patch("backend.agent_runtime.memory_manager.evomem_writer.mark_dirty") as mock_dirty:
            from backend.agent_runtime.memory_manager import _store_with_conflict_detection
            res = _store_with_conflict_detection("a1", "s1", "User lives in Bandung",
                                                 "user_info")
            assert res["superseded"] == [7, 8]
            assert {c.args for c in mock_del.call_args_list} == {("a1", 7), ("a1", 8)}
            mock_dirty.assert_called_once()


class TestUpdateRewritesEvomem:
    def _resp(self, content):
        return {"success": True, "response": {"choices": [
            {"message": {"content": content}, "finish_reason": "stop"}]}}

    def test_update_action_rewrites_note(self, monkeypatch):
        import threading
        monkeypatch.setenv("EVONIC_MEMORY_ENGINE", "evomem")
        monkeypatch.setattr(
            "backend.agent_runtime.evomem_client.is_available", lambda: True)
        # LLM call order: extract facts -> graph extract -> dedup ops -> dimension
        seq = [
            self._resp('[{"content": "User lives in Bandung", "category": "user_info"}]'),
            self._resp('{"entities": [], "relations": []}'),
            self._resp('[{"action": "update", "id": 5, "content": "User lives in Bandung", "category": "user_info"}]'),
            self._resp('user.city'),
        ]
        with patch("backend.agent_runtime.memory_manager.llm_client.chat_completion",
                   side_effect=seq), \
             patch("backend.agent_runtime.memory_manager.db.get_all_memories",
                   return_value=[{"id": 5, "content": "User lives in Jakarta",
                                  "category": "user_info"}]), \
             patch("backend.agent_runtime.memory_manager.db.update_memory"), \
             patch("backend.agent_runtime.memory_manager._try_evomem_store",
                   return_value=True) as mock_store:
            from backend.agent_runtime.memory_manager import extract_and_store_memories
            extract_and_store_memories({"id": "a1"}, "s1", "summary text",
                                       threading.Lock())
            # the update branch must rewrite the evomem note for memory id 5
            assert any(c.kwargs.get("memory_id") == 5 for c in mock_store.call_args_list)


class TestForgetMemory:
    def _patch_db(self):
        mem = {"id": 1, "content": "fact", "category": "general", "expired": False}
        return patch.multiple(
            "backend.agent_runtime.memory_manager.db",
            get_all_memories=lambda *a, **k: [mem],
            expire_memory=lambda *a, **k: None,
        )

    def test_removes_evomem_note_when_engine_evomem(self, monkeypatch):
        monkeypatch.setenv("EVONIC_MEMORY_ENGINE", "evomem")
        monkeypatch.setattr(
            "backend.agent_runtime.evomem_client.is_available", lambda: True)
        with self._patch_db(), patch(
            "backend.agent_runtime.memory_manager.evomem_writer.delete_note",
            return_value=True
        ) as mock_del, patch(
            "backend.agent_runtime.memory_manager.evomem_writer.mark_dirty"
        ) as mock_dirty:
            from backend.agent_runtime.memory_manager import forget_memory
            result = forget_memory("test-agent", 1)
            assert result["result"] == "Memory forgotten."
            assert result.get("evomem") == "removed"
            mock_del.assert_called_once_with("test-agent", 1)
            mock_dirty.assert_called_once()

    def test_skips_evomem_when_engine_fts5(self, monkeypatch):
        monkeypatch.setenv("EVONIC_MEMORY_ENGINE", "fts5")
        with self._patch_db(), patch(
            "backend.agent_runtime.memory_manager.evomem_writer.delete_note"
        ) as mock_del:
            from backend.agent_runtime.memory_manager import forget_memory
            result = forget_memory("test-agent", 1)
            assert result["result"] == "Memory forgotten."
            assert "evomem" not in result
            mock_del.assert_not_called()
