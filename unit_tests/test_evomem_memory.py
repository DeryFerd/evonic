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
        # store_memory only pins the fact to the running summary; durable
        # authoring is deferred to a background thread (_extract_from_fact_async),
        # so nothing writes long-term memory or calls the LLM synchronously.
        with patch("backend.agent_runtime.memory_manager.db.get_summary", return_value=None), \
             patch("backend.agent_runtime.memory_manager.db.upsert_summary"), \
             patch("backend.agent_runtime.memory_manager.db.add_memory") as add_mem, \
             patch("backend.agent_runtime.memory_manager._extract_from_fact_async") as evo, \
             patch("backend.agent_runtime.memory_manager.llm_client.chat_completion") as llm:
            from backend.agent_runtime.memory_manager import store_memory
            store_memory("test-agent", "sess-1", "Test fact", "general")
            add_mem.assert_not_called()
            llm.assert_not_called()
            evo.assert_called_once()  # durable authoring deferred to background

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


class TestForgetMemory:
    """forget_memory expires the FTS memory row; in the doc model it has no
    evomem note to delete (notes/ was removed), so it never touches evomem."""

    def _patch_db(self):
        mem = {"id": 1, "content": "fact", "category": "general", "expired": False}
        return patch.multiple(
            "backend.agent_runtime.memory_manager.db",
            get_all_memories=lambda *a, **k: [mem],
            expire_memory=lambda *a, **k: None,
        )

    def test_expires_memory_and_returns_result(self, monkeypatch):
        monkeypatch.setenv("EVONIC_MEMORY_ENGINE", "evomem")
        monkeypatch.setattr(
            "backend.agent_runtime.evomem_client.is_available", lambda: True)
        with self._patch_db():
            from backend.agent_runtime.memory_manager import forget_memory
            result = forget_memory("test-agent", 1)
            assert result["result"] == "Memory forgotten."
            assert result["id"] == 1
            # Knowledge docs are durable authored content — not auto-deleted here.
            assert "evomem" not in result

    def test_missing_memory_returns_error(self, monkeypatch):
        monkeypatch.setenv("EVONIC_MEMORY_ENGINE", "fts5")
        with patch.multiple(
            "backend.agent_runtime.memory_manager.db",
            get_all_memories=lambda *a, **k: [],
        ):
            from backend.agent_runtime.memory_manager import forget_memory
            result = forget_memory("test-agent", 999)
            assert "error" in result
