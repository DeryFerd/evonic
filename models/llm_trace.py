"""
llm_trace.py — append-only JSONL log of byte-exact LLM request/response per call.

Each session gets its own file at agents/<agent_id>/llm_traces/<session_id>.jsonl
(kept in a separate directory from sessions/*.jsonl so it never pollutes the
session listing in chatlog.ChatLogManager.list_sessions).

One line per LLM API call (one tool-loop iteration). The record captures the
GROUND-TRUTH training data — exactly what the model received and produced:

  ts            — epoch ms when the call was persisted
  turn_index    — ordinal of the agent turn this call belongs to (1-based)
  model         — model id actually used (from the request payload)
  request       — the exact request payload POSTed to the provider
                  (system + messages + tools, after ALL pipeline transformations)
  response      — the raw provider response object (content, reasoning_content/CoT,
                  tool_calls, finish_reason, usage)
  usage         — {prompt_tokens, completion_tokens, total_tokens}
  duration_ms   — call latency

Only written when EVONIC_SESSION_ARCHIVE is enabled. The SessionArchiver copies
these records into session_archive.db (archive_llm_calls) at archive time.
"""

import json
import os
import threading
import time
from typing import Dict, List, Optional, Tuple

_AGENTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'agents')


def _now_ms() -> int:
    return int(time.time() * 1000)


class LLMTraceLog:
    """Append-only JSONL trace of LLM calls for one session.

    Mirrors models.chatlog.ChatLog's path logic so sub-agent traces land under the
    DB-owning agent's directory, but in llm_traces/ instead of sessions/.
    """

    def __init__(self, agent_id: str, session_id: str):
        self.agent_id = agent_id
        self.session_id = session_id
        from backend.subagent_manager import subagent_manager
        if subagent_manager.is_subagent(agent_id):
            from models.chat import SUB_AGENTS_TMP_DIR
            base_dir = os.path.join(SUB_AGENTS_TMP_DIR, agent_id)
        else:
            base_dir = os.path.join(_AGENTS_DIR, agent_id)
        traces_dir = os.path.join(base_dir, 'llm_traces')
        os.makedirs(traces_dir, exist_ok=True)
        # session_id is "{agent_id}-{hash}" — strip the prefix for the filename
        filename = session_id
        if filename.startswith(f'{agent_id}-'):
            filename = filename[len(agent_id) + 1:]
        self._path = os.path.join(traces_dir, f'{filename}.jsonl')
        self._lock = threading.Lock()

    def append(self, entry: dict) -> None:
        """Serialize entry and append as a single line to the trace log."""
        if 'ts' not in entry:
            entry = dict(entry)
            entry['ts'] = _now_ms()
        line = json.dumps(entry, ensure_ascii=False) + '\n'
        with self._lock:
            with open(self._path, 'a', encoding='utf-8') as f:
                f.write(line)

    def get_all(self) -> List[dict]:
        """Return all trace records, oldest first."""
        results: List[dict] = []
        try:
            with open(self._path, 'r', encoding='utf-8', errors='replace') as f:
                for raw in f:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        results.append(json.loads(raw))
                    except (json.JSONDecodeError, ValueError):
                        continue
        except FileNotFoundError:
            return []
        return results

    def clear(self) -> None:
        """Remove the trace file for this session."""
        with self._lock:
            try:
                os.remove(self._path)
            except FileNotFoundError:
                pass


class LLMTraceManager:
    """Caches LLMTraceLog instances per (agent_id, session_id)."""

    def __init__(self):
        self._logs: Dict[Tuple[str, str], LLMTraceLog] = {}
        self._lock = threading.Lock()

    def get(self, agent_id: str, session_id: str) -> LLMTraceLog:
        key = (agent_id, session_id)
        with self._lock:
            if key not in self._logs:
                self._logs[key] = LLMTraceLog(agent_id, session_id)
            return self._logs[key]

    def evict(self, agent_id: str, session_id: str) -> None:
        key = (agent_id, session_id)
        with self._lock:
            self._logs.pop(key, None)


llm_trace_manager = LLMTraceManager()
