import os
import shutil
import sqlite3
import logging
from typing import Dict, Any, List, Optional, Tuple

logger = logging.getLogger(__name__)


class AttachmentsMixin:
    """Attachments CRUD + retention + per-agent config resolution.

    Requires self._connect() from the host class.
    Attachments back the Telegram (and future) attachment ingestion feature.
    Files live under data/attachments/<agent_id>/<session_id>/<ts>_<name>.
    """

    def save_attachment(self,
                        agent_id: str,
                        session_id: str,
                        filename: str,
                        file_path: str,
                        external_user_id: Optional[str] = None,
                        channel_id: Optional[str] = None,
                        channel_type: Optional[str] = None,
                        original_filename: Optional[str] = None,
                        mime_type: Optional[str] = None,
                        file_type: Optional[str] = None,
                        size_bytes: Optional[int] = None,
                        telegram_file_id: Optional[str] = None) -> int:
        """Insert a new attachment row and return its id."""
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO attachments (
                    agent_id, session_id, external_user_id, channel_id, channel_type,
                    filename, original_filename, mime_type, file_type, size_bytes,
                    file_path, telegram_file_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    agent_id, session_id, external_user_id, channel_id, channel_type,
                    filename, original_filename, mime_type, file_type, size_bytes,
                    file_path, telegram_file_id,
                ),
            )
            conn.commit()
            return int(cursor.lastrowid)

    def get_attachment(self, attachment_id: int) -> Optional[Dict[str, Any]]:
        """Return a single attachment row by id, or None."""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT id, agent_id, session_id, external_user_id, channel_id, channel_type, filename, original_filename, mime_type, file_type, size_bytes, file_path, telegram_file_id, created_at FROM attachments WHERE id = ?", (attachment_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def list_session_attachments(self, session_id: str, agent_id: str) -> List[Dict[str, Any]]:
        """List all attachments for a given (session, agent), newest first."""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, agent_id, session_id, external_user_id, channel_id, channel_type, filename, original_filename, mime_type, file_type, size_bytes, file_path, telegram_file_id, created_at FROM attachments WHERE session_id = ? AND agent_id = ? "
                "ORDER BY created_at DESC, id DESC",
                (session_id, agent_id),
            )
            return [dict(r) for r in cursor.fetchall()]

    def delete_attachment(self, attachment_id: int) -> bool:
        """Delete the attachment row and best-effort remove the file on disk."""
        row = self.get_attachment(attachment_id)
        if not row:
            return False
        # Best-effort file removal first; failures are logged but do not block row removal.
        path = row.get('file_path')
        if path:
            try:
                if os.path.isfile(path):
                    os.remove(path)
            except OSError as e:
                logger.warning("Failed to remove attachment file %s: %s", path, e)
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM attachments WHERE id = ?", (attachment_id,))
            conn.commit()
            return cursor.rowcount > 0

    def cleanup_expired_attachments(self, max_age_days: int = 7) -> Tuple[int, int]:
        """Bulk delete attachments older than max_age_days.

        Returns a (deleted_count, freed_bytes) tuple. File-removal failures are
        logged and counted toward freed_bytes only when the file actually existed
        and got removed.
        """
        if max_age_days is None or max_age_days < 0:
            max_age_days = 7
        deleted = 0
        freed = 0
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, file_path, size_bytes FROM attachments "
                "WHERE created_at < datetime('now', ?)",
                (f"-{int(max_age_days)} days",),
            )
            rows = [dict(r) for r in cursor.fetchall()]
        for row in rows:
            path = row.get('file_path')
            if path:
                try:
                    if os.path.isfile(path):
                        os.remove(path)
                        freed += int(row.get('size_bytes') or 0)
                except OSError as e:
                    logger.warning("Failed to remove expired attachment file %s: %s", path, e)
            with self._connect() as conn:
                cursor = conn.cursor()
                cursor.execute("DELETE FROM attachments WHERE id = ?", (row['id'],))
                conn.commit()
                deleted += cursor.rowcount or 0
        return deleted, freed

    def delete_session_attachments(self,
                                   session_id: str,
                                   agent_id: str,
                                   base_dir: Optional[str] = None) -> Tuple[int, int]:
        """Delete every attachment row for a single session and remove its files.

        Returns a (deleted_rows, freed_bytes) tuple. The on-disk subdirectory
        ``<base_dir>/<agent_id>/<session_id>/`` is also best-effort removed to
        clean up any orphaned files left behind by earlier crashes.
        ``base_dir`` defaults to ``data/attachments`` relative to the current
        working directory.

        ``agent_id`` is required to prevent accidental cross-agent deletion of
        every agent's rows for a shared ``session_id``. Passing a falsy
        ``agent_id`` raises ``ValueError``.
        """
        if not session_id:
            return 0, 0
        if not agent_id:
            raise ValueError(
                "delete_session_attachments requires an agent_id to avoid "
                "cross-agent deletion of session rows."
            )
        deleted = 0
        freed = 0
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, file_path, size_bytes FROM attachments "
                "WHERE session_id = ? AND agent_id = ?",
                (session_id, agent_id),
            )
            rows = [dict(r) for r in cursor.fetchall()]
        for row in rows:
            path = row.get('file_path')
            if path:
                try:
                    if os.path.isfile(path):
                        os.remove(path)
                        freed += int(row.get('size_bytes') or 0)
                except OSError as e:
                    logger.warning(
                        "Failed to remove attachment file %s: %s", path, e
                    )
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM attachments WHERE session_id = ? AND agent_id = ?",
                (session_id, agent_id),
            )
            deleted = cursor.rowcount or 0
            conn.commit()
        # Best-effort wipe of the per-session on-disk subdir to catch any
        # orphaned files (e.g. written before the DB row was committed).
        target_root = base_dir or os.path.join('data', 'attachments')
        session_dir = os.path.join(target_root, agent_id, session_id)
        try:
            if os.path.isdir(session_dir):
                shutil.rmtree(session_dir, ignore_errors=True)
        except OSError as e:
            logger.warning(
                "Failed to remove attachment session dir %s: %s",
                session_dir, e,
            )
        return deleted, freed

    def delete_all_attachments(self, base_dir: Optional[str] = None) -> Tuple[int, int]:
        """Bulk delete every attachment row and best-effort wipe the on-disk tree.

        Returns a (deleted_rows, freed_bytes) tuple. ``base_dir`` defaults to
        ``data/attachments`` relative to the current working directory and is
        only used when individual file paths in the DB are missing or fail to
        remove (e.g. orphaned files left behind by earlier crashes).
        """
        deleted = 0
        freed = 0
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT id, file_path, size_bytes FROM attachments")
            rows = [dict(r) for r in cursor.fetchall()]
        for row in rows:
            path = row.get('file_path')
            if path:
                try:
                    if os.path.isfile(path):
                        os.remove(path)
                        freed += int(row.get('size_bytes') or 0)
                except OSError as e:
                    logger.warning(
                        "Failed to remove attachment file %s: %s", path, e
                    )
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM attachments")
            deleted = cursor.rowcount or 0
            conn.commit()
        # Best-effort wipe of the on-disk tree to clean up any orphaned files
        # (e.g. files written before the DB row was committed, or stale dirs).
        target_dir = base_dir or os.path.join('data', 'attachments')
        try:
            if os.path.isdir(target_dir):
                for entry in os.listdir(target_dir):
                    entry_path = os.path.join(target_dir, entry)
                    try:
                        if os.path.isdir(entry_path):
                            shutil.rmtree(entry_path, ignore_errors=True)
                        elif os.path.isfile(entry_path):
                            os.remove(entry_path)
                    except OSError as e:
                        logger.warning(
                            "Failed to remove attachment path %s: %s",
                            entry_path, e,
                        )
        except OSError as e:
            logger.warning(
                "Failed to wipe attachments base dir %s: %s", target_dir, e
            )
        return deleted, freed

    def get_agent_attachment_config(self, agent_id: str) -> Dict[str, Any]:
        """Resolve the effective attachment configuration for an agent.

        Returns dict: {enabled: bool, max_size_mb: int, supported: bool, model_id: Optional[str]}.
        Resolves model via agents.model_id, falling back to the global default model.
        """
        result = {
            'enabled': False,
            'max_size_mb': 20,
            'supported': False,
            'model_id': None,
        }
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                "SELECT attachments_enabled, attachment_max_size_mb, model_id "
                "FROM agents WHERE id = ?",
                (agent_id,),
            )
            agent_row = cursor.fetchone()
            if not agent_row:
                return result
            result['enabled'] = bool(agent_row['attachments_enabled'])
            try:
                result['max_size_mb'] = int(agent_row['attachment_max_size_mb'] or 20)
            except (TypeError, ValueError):
                result['max_size_mb'] = 20
            # Hard-cap to Telegram bot API limit
            if result['max_size_mb'] > 20:
                result['max_size_mb'] = 20

            model_id = agent_row['model_id']
            model_row = None
            if model_id:
                cursor.execute(
                    "SELECT id, attachments_supported FROM llm_models WHERE id = ?",
                    (model_id,),
                )
                model_row = cursor.fetchone()
            if not model_row:
                cursor.execute(
                    "SELECT id, attachments_supported FROM llm_models "
                    "WHERE is_default = 1 LIMIT 1"
                )
                model_row = cursor.fetchone()
            if model_row:
                result['model_id'] = model_row['id']
                result['supported'] = bool(model_row['attachments_supported'])
        return result
