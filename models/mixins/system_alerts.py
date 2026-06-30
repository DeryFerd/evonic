import time


class SystemAlertMixin:
    """Persistent system alerts surfaced as banners in the web UI."""

    def emit_system_alert(self, category: str, message: str,
                          level: str = "error", agent_id: str = None):
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO system_alerts (category, agent_id, message, level, created_at, dismissed_at) "
                "VALUES (?, ?, ?, ?, ?, NULL) "
                "ON CONFLICT(category) DO UPDATE SET "
                "  message = excluded.message, level = excluded.level, "
                "  agent_id = excluded.agent_id, created_at = excluded.created_at, "
                "  dismissed_at = NULL",
                (category, agent_id, message, level, time.time()),
            )
            conn.commit()

    def get_active_system_alerts(self) -> list:
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, category, agent_id, message, level, created_at "
                "FROM system_alerts WHERE dismissed_at IS NULL "
                "ORDER BY created_at DESC"
            )
            return [
                {"id": r[0], "category": r[1], "agent_id": r[2],
                 "message": r[3], "level": r[4], "created_at": r[5]}
                for r in cursor.fetchall()
            ]

    def dismiss_system_alert(self, category: str) -> bool:
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE system_alerts SET dismissed_at = ? WHERE category = ? AND dismissed_at IS NULL",
                (time.time(), category),
            )
            conn.commit()
            return cursor.rowcount > 0
