"""
session_archive.py — Archive session data to session_archive.db before /clear.

When EVONIC_SESSION_ARCHIVE=1 is set, every /clear command copies the full
session data (chat messages, summaries, session state, and raw JSONL log) to
shared/db/session_archive.db before the data is purged.

The archive DB is designed as a training dataset source for improving model
performance on Evonic agentic operations.

Schema tables:
  archive_sessions      — session metadata
  archive_messages      — chat_messages rows
  archive_summaries     — chat_summaries row
  archive_session_state — session_state row
  archive_jsonl         — every raw JSONL entry (most detailed trace)
"""

import json
import logging
import os
import sqlite3
import threading
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
    channel_id TEXT,
    external_user_id TEXT NOT NULL,
    bot_enabled BOOLEAN DEFAULT 1,
    archived_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS archive_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    archive_id INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT,
    tool_calls TEXT,
    tool_call_id TEXT,
    metadata TEXT,
    created_at TEXT,
    FOREIGN KEY (archive_id) REFERENCES archive_sessions(id)
);

CREATE TABLE IF NOT EXISTS archive_summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    archive_id INTEGER NOT NULL UNIQUE,
    summary TEXT NOT NULL,
    last_message_id INTEGER,
    message_count INTEGER,
    created_at TEXT,
    FOREIGN KEY (archive_id) REFERENCES archive_sessions(id)
);

CREATE TABLE IF NOT EXISTS archive_session_state (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    archive_id INTEGER NOT NULL UNIQUE,
    content TEXT NOT NULL,
    FOREIGN KEY (archive_id) REFERENCES archive_sessions(id)
);

CREATE TABLE IF NOT EXISTS archive_jsonl (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    archive_id INTEGER NOT NULL,
    line_number INTEGER NOT NULL,
    entry_type TEXT NOT NULL,
    entry_json TEXT NOT NULL,
    ts INTEGER,
    FOREIGN KEY (archive_id) REFERENCES archive_sessions(id)
);
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
    """Archives a session's data to session_archive.db in a background thread.

    Usage:
        SessionArchiver.archive_session(agent_id, session_id)

    The archive reads all data *before* clearing begins (fast reads from local
    SQLite/JSONL), then writes to the archive DB in a daemon thread so the
    /clear response is not delayed.
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
    def archive_session(cls, agent_id: str, session_id: str) -> None:
        """Read session data and launch a background thread to write the archive.

        This returns immediately — the actual archive write runs in a daemon
        thread so it never blocks the /clear response.
        """
        cls._ensure_schema()
        chat_db = None
        try:
            # Import here to avoid circular imports
            from models.chat import agent_chat_manager
            chat_db = agent_chat_manager.get(agent_id)
        except Exception:
            _logger.exception(
                "SessionArchive: could not get chat DB for %s", agent_id
            )
            return

        # --- Read all data into memory (fast, before clearing) ---
        try:
            session_info = chat_db.get_session(session_id)
            if not session_info:
                _logger.warning(
                    "SessionArchive: session %s not found for agent %s",
                    session_id, agent_id,
                )
                return
            session_info = dict(session_info)
        except Exception:
            _logger.exception(
                "SessionArchive: failed to read session info for %s", session_id
            )
            return

        messages: List[dict] = []
        try:
            with chat_db._connect() as conn:
                conn.row_factory = sqlite3.Row
                # No LIMIT: archiving requires all messages for this session
                raw = conn.execute(
                    "SELECT id, session_id, role, content, tool_calls, tool_call_id, metadata, created_at FROM chat_messages WHERE session_id = ? ORDER BY id ASC",
                    (session_id,),
                ).fetchall()
                for r in raw:
                    d = dict(r)
                    if d.get("tool_calls") and isinstance(d["tool_calls"], str):
                        try:
                            d["tool_calls"] = json.loads(d["tool_calls"])
                        except Exception:
                            pass
                    if d.get("metadata") and isinstance(d["metadata"], str):
                        try:
                            d["metadata"] = json.loads(d["metadata"])
                        except Exception:
                            pass
                    messages.append(d)
        except Exception:
            _logger.exception(
                "SessionArchive: failed to read messages for %s", session_id
            )

        summary = None
        try:
            raw = chat_db.get_summary(session_id)
            if raw:
                summary = dict(raw)
        except Exception:
            _logger.exception(
                "SessionArchive: failed to read summary for %s", session_id
            )

        session_state = None
        try:
            raw = chat_db.get_session_state(session_id)
            if raw:
                session_state = raw
        except Exception:
            _logger.exception(
                "SessionArchive: failed to read session_state for %s", session_id
            )

        # --- Read JSONL entries ---
        jsonl_entries: List[dict] = []
        try:
            from models.chatlog import chatlog_manager
            chatlog = chatlog_manager.get(agent_id, session_id)
            jsonl_entries = chatlog.get_all_for_session()
        except Exception:
            _logger.exception(
                "SessionArchive: failed to read JSONL for %s", session_id
            )

        # --- Launch background thread to write archive ---
        t = threading.Thread(
            target=cls._write_archive,
            args=(session_info, messages, summary, session_state, jsonl_entries),
            daemon=True,
            name=f"session-archive-{session_id[:8]}",
        )
        t.start()
        _logger.info(
            "SessionArchive: launched background archive for session %s "
            "(%d messages, %d JSONL entries)",
            session_id, len(messages), len(jsonl_entries),
        )

    @classmethod
    def _write_archive(
        cls,
        session_info: dict,
        messages: List[dict],
        summary: Optional[dict],
        session_state: Optional[str],
        jsonl_entries: List[dict],
    ) -> None:
        """Write all collected session data into session_archive.db.

        Runs in a background daemon thread — errors are logged but never raise.
        """
        session_id = session_info.get("id", "")
        agent_id = session_info.get("agent_id", "")
        conn = _get_connection()
        try:
            # 1. Insert session metadata
            conn.execute(
                """INSERT INTO archive_sessions
                   (session_id, agent_id, channel_id, external_user_id, bot_enabled)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    session_id,
                    agent_id,
                    session_info.get("channel_id"),
                    session_info.get("external_user_id", ""),
                    session_info.get("bot_enabled", 1),
                ),
            )
            archive_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

            # 2. Insert messages
            for msg in messages:
                tool_calls = msg.get("tool_calls")
                if tool_calls is not None and not isinstance(tool_calls, str):
                    tool_calls = json.dumps(tool_calls, ensure_ascii=False)
                metadata = msg.get("metadata")
                if metadata is not None and not isinstance(metadata, str):
                    metadata = json.dumps(metadata, ensure_ascii=False)
                conn.execute(
                    """INSERT INTO archive_messages
                       (archive_id, role, content, tool_calls, tool_call_id, metadata, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        archive_id,
                        msg.get("role", ""),
                        msg.get("content"),
                        tool_calls,
                        msg.get("tool_call_id"),
                        metadata,
                        msg.get("created_at"),
                    ),
                )

            # 3. Insert summary
            if summary:
                conn.execute(
                    """INSERT INTO archive_summaries
                       (archive_id, summary, last_message_id, message_count, created_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (
                        archive_id,
                        summary.get("summary", ""),
                        summary.get("last_message_id"),
                        summary.get("message_count"),
                        summary.get("created_at"),
                    ),
                )

            # 4. Insert session state
            if session_state:
                conn.execute(
                    "INSERT INTO archive_session_state (archive_id, content) VALUES (?, ?)",
                    (archive_id, session_state),
                )

            # 5. Insert JSONL entries (the richest data for training)
            for i, entry in enumerate(jsonl_entries):
                entry_type = entry.get("type", "unknown")
                entry_ts = entry.get("ts")
                conn.execute(
                    """INSERT INTO archive_jsonl
                       (archive_id, line_number, entry_type, entry_json, ts)
                       VALUES (?, ?, ?, ?, ?)""",
                    (
                        archive_id,
                        i + 1,
                        entry_type,
                        json.dumps(entry, ensure_ascii=False),
                        entry_ts,
                    ),
                )

            conn.commit()
            _logger.info(
                "SessionArchive: archived session %s (archive_id=%d, "
                "%d messages, %d JSONL entries)",
                session_id, archive_id, len(messages), len(jsonl_entries),
            )
        except Exception:
            _logger.exception(
                "SessionArchive: failed to write archive for session %s", session_id
            )
        finally:
            conn.close()
