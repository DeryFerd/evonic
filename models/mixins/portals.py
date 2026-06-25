import sqlite3
import uuid
from typing import Dict, Any, List, Optional


class PortalMixin:
    """Portal CRUD. Requires self._connect() from the host class."""

    # ==================== Portals ====================

    def get_portals(self) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT id, agent_id, name, virtual_path, backend_type, backend_config, real_path, status, error_msg, created_at, updated_at FROM portals ORDER BY name")
            return [dict(row) for row in cursor.fetchall()]

    def get_agent_portals(self, agent_id: str) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, agent_id, name, virtual_path, backend_type, backend_config, real_path, status, error_msg, created_at, updated_at FROM portals WHERE agent_id = ? ORDER BY virtual_path",
                (agent_id,),
            )
            return [dict(row) for row in cursor.fetchall()]

    def get_portal(self, portal_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT id, agent_id, name, virtual_path, backend_type, backend_config, real_path, status, error_msg, created_at, updated_at FROM portals WHERE id = ?", (portal_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def create_portal(self, data: Dict[str, Any]) -> str:
        portal_id = data.get("id") or uuid.uuid4().hex[:12]
        import json
        backend_config = data.get("backend_config", {})
        if isinstance(backend_config, dict):
            backend_config = json.dumps(backend_config)
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO portals (id, agent_id, name, virtual_path,
                                     backend_type, backend_config, real_path, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'disconnected')
                """,
                (
                    portal_id,
                    data["agent_id"],
                    data["name"],
                    data["virtual_path"],
                    data["backend_type"],
                    backend_config,
                    data["real_path"],
                ),
            )
            conn.commit()
        return portal_id

    def update_portal(self, portal_id: str, data: Dict[str, Any]) -> bool:
        import json
        allowed = {"name", "virtual_path", "backend_type", "backend_config",
                   "real_path", "status", "error_msg"}
        updates = {k: v for k, v in data.items() if k in allowed}
        if "backend_config" in updates and isinstance(updates["backend_config"], dict):
            updates["backend_config"] = json.dumps(updates["backend_config"])
        if not updates:
            return False
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [portal_id]
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"UPDATE portals SET {set_clause}, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                values,
            )
            conn.commit()
            return cursor.rowcount > 0

    def delete_portal(self, portal_id: str) -> bool:
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM portals WHERE id = ?", (portal_id,))
            conn.commit()
            return cursor.rowcount > 0

    def update_portal_status(self, portal_id: str, status: str,
                              error_msg: Optional[str] = None) -> None:
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE portals SET status = ?, error_msg = ?,
                updated_at = CURRENT_TIMESTAMP WHERE id = ?
                """,
                (status, error_msg, portal_id),
            )
            conn.commit()
