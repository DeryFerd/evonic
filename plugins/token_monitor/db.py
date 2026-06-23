"""
UsageDB — SQLite storage for LLM token usage records.

One row per successful LLM completion, captured from the generic ``llm_usage``
event. All state lives in data/db/plugins/token_monitor.db (WAL mode).
Timestamps are UTC ISO8601, so time-bucketing uses plain string slicing.
"""

import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, Generator, List, Optional

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_shared_data = os.path.join(BASE_DIR, 'shared', 'data')
_data_root = _shared_data if os.path.isdir(_shared_data) else os.path.join(BASE_DIR, 'data')
PLUGIN_DB_DIR = os.path.join(_data_root, 'db', 'plugins')
DB_PATH = os.path.join(PLUGIN_DB_DIR, 'token_monitor.db')

_SUBAGENT_RE = re.compile(r'_sub_\d+$')
_EXPLORER_RE = re.compile(r'_explorer_\d+$')


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class UsageDB:
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._init_tables()

    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection, None, None]:
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_tables(self):
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS token_usage (
                    id                INTEGER PRIMARY KEY AUTOINCREMENT,
                    source            TEXT NOT NULL DEFAULT 'other',
                    agent_id          TEXT,
                    agent_name        TEXT,
                    session_id        TEXT,
                    model             TEXT,
                    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
                    completion_tokens INTEGER NOT NULL DEFAULT 0,
                    total_tokens      INTEGER NOT NULL DEFAULT 0,
                    estimated         INTEGER NOT NULL DEFAULT 0,
                    duration_ms       INTEGER NOT NULL DEFAULT 0,
                    created_at        TEXT NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tu_created ON token_usage(created_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tu_agent   ON token_usage(agent_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tu_source  ON token_usage(source)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tu_model   ON token_usage(model)")

    # ── Write ─────────────────────────────────────────────────────────
    def record(self, *, source: str, agent_id: Optional[str], agent_name: Optional[str],
               session_id: Optional[str], model: Optional[str],
               prompt_tokens: int, completion_tokens: int, total_tokens: int,
               estimated: bool = False, duration_ms: int = 0) -> None:
        with self._connect() as conn:
            conn.execute("""
                INSERT INTO token_usage (source, agent_id, agent_name, session_id, model,
                                         prompt_tokens, completion_tokens, total_tokens,
                                         estimated, duration_ms, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (source or 'other', agent_id, agent_name, session_id, model or '',
                  int(prompt_tokens or 0), int(completion_tokens or 0), int(total_tokens or 0),
                  1 if estimated else 0, int(duration_ms or 0), _now()))

    # ── Aggregations ──────────────────────────────────────────────────
    @staticmethod
    def _since_clause(since: Optional[str]) -> tuple:
        return (" WHERE created_at >= ?", (since,)) if since else ("", ())

    def overall_totals(self, since: Optional[str] = None) -> Dict[str, Any]:
        where, params = self._since_clause(since)
        with self._connect() as conn:
            row = conn.execute(f"""
                SELECT COUNT(*) AS calls,
                       COALESCE(SUM(prompt_tokens), 0)     AS prompt_tokens,
                       COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                       COALESCE(SUM(total_tokens), 0)      AS total_tokens,
                       COALESCE(SUM(estimated), 0)         AS estimated_calls
                FROM token_usage{where}
            """, params).fetchone()
            return dict(row)

    def by_agent(self, since: Optional[str] = None, rollup_subagents: bool = False) -> List[Dict[str, Any]]:
        where, params = self._since_clause(since)
        with self._connect() as conn:
            rows = [dict(r) for r in conn.execute(f"""
                SELECT agent_id,
                       MAX(agent_name) AS agent_name,
                       COUNT(*) AS calls,
                       COALESCE(SUM(prompt_tokens), 0)     AS prompt_tokens,
                       COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                       COALESCE(SUM(total_tokens), 0)      AS total_tokens
                FROM token_usage{where}
                GROUP BY agent_id
                ORDER BY total_tokens DESC
            """, params).fetchall()]
        # Flag explorer sub-agents (id like "parent_explorer_1") so the UI can
        # render them distinctly.
        for r in rows:
            r['is_explorer'] = bool(_EXPLORER_RE.search(r.get('agent_id') or ''))
        if not rollup_subagents:
            return rows
        # Re-aggregate sub-agents under a parent key. Regular sub-agents
        # ("parent_sub_1") roll into the parent; explorers ("parent_explorer_1")
        # collapse into a single per-parent explorer entry kept separate from
        # the parent so they stay visually distinct.
        merged: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            aid = r.get('agent_id') or ''
            if _EXPLORER_RE.search(aid):
                key = _EXPLORER_RE.sub('_explorer', aid)  # parent_explorer_1 -> parent_explorer
                is_explorer = True
            else:
                key = _SUBAGENT_RE.sub('', aid) or aid
                is_explorer = False
            acc = merged.setdefault(key, {
                'agent_id': key, 'agent_name': r.get('agent_name'),
                'calls': 0, 'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0,
                'is_explorer': is_explorer,
            })
            for f in ('calls', 'prompt_tokens', 'completion_tokens', 'total_tokens'):
                acc[f] += r.get(f, 0)
        return sorted(merged.values(), key=lambda x: x['total_tokens'], reverse=True)

    def _group_by(self, column: str, since: Optional[str]) -> List[Dict[str, Any]]:
        where, params = self._since_clause(since)
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(f"""
                SELECT {column} AS key,
                       COUNT(*) AS calls,
                       COALESCE(SUM(prompt_tokens), 0)     AS prompt_tokens,
                       COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                       COALESCE(SUM(total_tokens), 0)      AS total_tokens
                FROM token_usage{where}
                GROUP BY {column}
                ORDER BY total_tokens DESC
            """, params).fetchall()]

    def by_source(self, since: Optional[str] = None) -> List[Dict[str, Any]]:
        return self._group_by('source', since)

    def by_model(self, since: Optional[str] = None) -> List[Dict[str, Any]]:
        return self._group_by('model', since)

    def series(self, since: Optional[str] = None, bucket: str = 'hour') -> List[Dict[str, Any]]:
        # created_at is UTC ISO8601 → slice the prefix for the bucket key.
        # 'YYYY-MM-DDTHH' (13 chars) for hourly, 'YYYY-MM-DD' (10) for daily.
        length = 13 if bucket == 'hour' else 10
        where, params = self._since_clause(since)
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(f"""
                SELECT substr(created_at, 1, {length}) AS bucket,
                       COALESCE(SUM(prompt_tokens), 0)     AS prompt_tokens,
                       COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                       COALESCE(SUM(total_tokens), 0)      AS total_tokens
                FROM token_usage{where}
                GROUP BY bucket
                ORDER BY bucket ASC
            """, params).fetchall()]


# Singleton
usage_db = UsageDB()
