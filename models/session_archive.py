"""
session_archive.py — Archive byte-exact LLM I/O to session_archive.db for training.

When EVONIC_SESSION_ARCHIVE=1 is set, agent sessions are archived to
shared/db/session_archive.db. The archive captures the GROUND-TRUTH the model saw
at inference time — the exact request payload (system + messages + tools, after all
pipeline transformations) and the raw response (content, CoT/reasoning_content,
tool_calls, finish_reason, usage) — one row per LLM API call.

Two triggers:
  - /clear  → archives the main session (models.mixins.chat_delegation.clear_session)
  - sub-agent turn-end → archives single-turn sub-agents (backend/agent_runtime/llm_loop)

Schema (intentionally minimal — debugging is served by the per-agent session JSONL):
  archive_sessions   — session metadata (agent_id, agent_kind, parent_agent_id, ...)
  archive_llm_calls  — one row per LLM call: byte-exact request + raw response

The per-call records are read from the LLMTraceLog file written during inference
(models/llm_trace.py), NOT reconstructed from chat_messages/JSONL (which differ from
the real payload).
"""

import json
import logging
import os
import sqlite3
import threading
import time
from typing import List, Optional

import config

_logger = logging.getLogger(__name__)

# Archive DB location
_ARCHIVE_DIR = os.path.join(config.APP_ROOT, "shared", "db")
_ARCHIVE_PATH = os.path.join(_ARCHIVE_DIR, "session_archive.db")


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS archive_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    agent_kind TEXT DEFAULT 'main',
    parent_agent_id TEXT,
    external_user_id TEXT,
    channel_id TEXT,
    archived_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS archive_llm_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    archive_id INTEGER NOT NULL,
    call_index INTEGER NOT NULL,
    turn_index INTEGER,
    model TEXT,
    request_json TEXT,
    response_json TEXT,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    total_tokens INTEGER,
    finish_reason TEXT,
    duration_ms INTEGER,
    ts INTEGER,
    FOREIGN KEY (archive_id) REFERENCES archive_sessions(id)
);

CREATE TABLE IF NOT EXISTS cmp (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    agent_id TEXT,
    category TEXT NOT NULL,
    system_prompt TEXT NOT NULL,
    input TEXT NOT NULL,
    output TEXT NOT NULL,
    model TEXT,
    decision TEXT,
    target TEXT,
    ts INTEGER
);

CREATE INDEX IF NOT EXISTS idx_archive_sessions_session ON archive_sessions(session_id);
CREATE INDEX IF NOT EXISTS idx_archive_llm_calls_archive ON archive_llm_calls(archive_id);
CREATE INDEX IF NOT EXISTS idx_cmp_session ON cmp(session_id);
CREATE INDEX IF NOT EXISTS idx_cmp_category ON cmp(category);
"""


def _get_connection() -> sqlite3.Connection:
    os.makedirs(_ARCHIVE_DIR, exist_ok=True)
    conn = sqlite3.connect(_ARCHIVE_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.row_factory = sqlite3.Row
    return conn


def _init_schema() -> None:
    conn = _get_connection()
    try:
        conn.executescript(_SCHEMA_SQL)
        conn.commit()
    finally:
        conn.close()


class SessionArchiver:
    """Archives a session's byte-exact LLM I/O to session_archive.db.

    Usage:
        SessionArchiver.archive_session(agent_id, session_id, agent_kind='main')
        SessionArchiver.delete_for_session(session_id)  # cancel a tentative archive

    Reads the LLM-trace file (fast local read), then writes to the archive DB in a
    daemon thread so the /clear or turn-end response is never delayed.
    """

    _init_lock = threading.Lock()
    _schema_initialized = False

    @classmethod
    def _ensure_schema(cls) -> None:
        if cls._schema_initialized:
            return
        with cls._init_lock:
            if cls._schema_initialized:
                return
            _init_schema()
            cls._schema_initialized = True

    @classmethod
    def archive_session(cls, agent_id: str, session_id: str,
                        agent_kind: str = 'main',
                        parent_agent_id: Optional[str] = None) -> None:
        """Read the session's LLM-trace records and write the archive in the background.

        Returns immediately — the actual write runs in a daemon thread.
        `agent_id` is the DB-owning agent id (the parent id for sub-agents).
        """
        cls._ensure_schema()

        # --- Read session metadata (best-effort) ---
        external_user_id = None
        channel_id = None
        try:
            from models.chat import agent_chat_manager
            chat_db = agent_chat_manager.get(agent_id)
            session_info = chat_db.get_session(session_id)
            if session_info:
                session_info = dict(session_info)
                external_user_id = session_info.get("external_user_id")
                channel_id = session_info.get("channel_id")
        except Exception:
            _logger.exception(
                "SessionArchive: failed to read session info for %s", session_id
            )

        # --- Read byte-exact LLM-call records ---
        calls: List[dict] = []
        try:
            from models.llm_trace import llm_trace_manager
            calls = llm_trace_manager.get(agent_id, session_id).get_all()
        except Exception:
            _logger.exception(
                "SessionArchive: failed to read LLM trace for %s", session_id
            )

        if not calls:
            _logger.info(
                "SessionArchive: no LLM-call records for session %s — skipping",
                session_id,
            )
            return

        # --- Launch background thread to write archive ---
        t = threading.Thread(
            target=cls._write_archive,
            args=(session_id, agent_id, agent_kind, parent_agent_id,
                  external_user_id, channel_id, calls),
            daemon=True,
            name=f"session-archive-{session_id[:8]}",
        )
        t.start()
        _logger.info(
            "SessionArchive: launched background archive for session %s "
            "(kind=%s, %d LLM calls)",
            session_id, agent_kind, len(calls),
        )

    @classmethod
    def _write_archive(
        cls,
        session_id: str,
        agent_id: str,
        agent_kind: str,
        parent_agent_id: Optional[str],
        external_user_id: Optional[str],
        channel_id: Optional[str],
        calls: List[dict],
    ) -> None:
        """Write the session metadata + per-call records. Errors are logged, never raised."""
        conn = _get_connection()
        try:
            conn.execute(
                """INSERT INTO archive_sessions
                   (session_id, agent_id, agent_kind, parent_agent_id,
                    external_user_id, channel_id)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (session_id, agent_id, agent_kind, parent_agent_id,
                 external_user_id, channel_id),
            )
            archive_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

            for i, rec in enumerate(calls):
                req = rec.get("request")
                resp = rec.get("response")
                usage = rec.get("usage") or {}
                conn.execute(
                    """INSERT INTO archive_llm_calls
                       (archive_id, call_index, turn_index, model,
                        request_json, response_json,
                        prompt_tokens, completion_tokens, total_tokens,
                        finish_reason, duration_ms, ts)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        archive_id,
                        i + 1,
                        rec.get("turn_index"),
                        rec.get("model"),
                        json.dumps(req, ensure_ascii=False) if req is not None else None,
                        json.dumps(resp, ensure_ascii=False) if resp is not None else None,
                        usage.get("prompt_tokens"),
                        usage.get("completion_tokens"),
                        usage.get("total_tokens"),
                        rec.get("finish_reason"),
                        rec.get("duration_ms"),
                        rec.get("ts"),
                    ),
                )

            conn.commit()
            _logger.info(
                "SessionArchive: archived session %s (archive_id=%d, %d LLM calls)",
                session_id, archive_id, len(calls),
            )
        except Exception:
            _logger.exception(
                "SessionArchive: failed to write archive for session %s", session_id
            )
        finally:
            conn.close()

    @classmethod
    def delete_for_session(cls, session_id: str) -> None:
        """Remove any archived rows for a session_id (cancels a tentative archive).

        Used when a sub-agent runs a 2nd turn: its turn-1 archive is removed so
        multi-turn sub-agents leave nothing. Sub-agent session_ids are unique per
        spawn, so this never touches the main session's archive.
        """
        cls._ensure_schema()
        conn = _get_connection()
        try:
            rows = conn.execute(
                "SELECT id FROM archive_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchall()
            for r in rows:
                conn.execute(
                    "DELETE FROM archive_llm_calls WHERE archive_id = ?", (r["id"],)
                )
            conn.execute(
                "DELETE FROM archive_sessions WHERE session_id = ?", (session_id,)
            )
            conn.commit()
            if rows:
                _logger.info(
                    "SessionArchive: deleted %d archive(s) for session %s",
                    len(rows), session_id,
                )
        except Exception:
            _logger.exception(
                "SessionArchive: failed to delete archive for session %s", session_id
            )
        finally:
            conn.close()

    @classmethod
    def write_cmp_example(cls, *,
                          session_id: str,
                          agent_id: Optional[str],
                          category: str,
                          system_prompt: str,
                          input: str,
                          output: str,
                          model: Optional[str] = None,
                          decision: Optional[str] = None,
                          target: Optional[str] = None) -> None:
        """Write a single CMP training example to the cmp table.

        Called inline from classifier_chat when SESSION_ARCHIVE is enabled.
        Minimal work — no background thread needed for a single INSERT.
        Parses boundary decision/target from output if not provided.
        """
        if not config.SESSION_ARCHIVE:
            return
        cls._ensure_schema()
        conn = None
        try:
            if category == "boundary" and (decision is None or target is None):
                # Parse decision/target from boundary output
                output_clean = (output or "").strip().upper()
                if output_clean.startswith("RETURN:"):
                    decision = "return"
                    target = output_clean.replace("RETURN:", "").strip()
                elif output_clean.startswith("DEP_BRANCH:"):
                    decision = "dep_branch"
                    target = output_clean.replace("DEP_BRANCH:", "").strip()
                elif output_clean == "INDEP_BRANCH":
                    decision = "indep_branch"
                elif output_clean == "CONTINUE":
                    decision = "continue"
                elif "NEW_TASK" in output_clean:
                    decision = "new_task"
                elif "CONTINUATION" in output_clean:
                    decision = "continuation"

            conn = _get_connection()
            conn.execute(
                """INSERT INTO cmp
                   (session_id, agent_id, category, system_prompt, input, output,
                    model, decision, target, ts)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (session_id, agent_id, category, system_prompt, input, output,
                 model, decision, target, int(time.time())),
            )
            conn.commit()
        except Exception:
            _logger.debug("SessionArchive: write_cmp_example failed", exc_info=True)
        finally:
            if conn:
                conn.close()
