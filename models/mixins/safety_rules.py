import sqlite3
import uuid
from typing import Dict, Any, List, Optional


class SafetyRuleMixin:
    """CRUD operations for safety_rules + agent_safety_rules tables. Requires self._connect()."""

    # ---- Safety Rules CRUD ----

    def get_safety_rules(self, enabled_only: bool = False) -> List[Dict[str, Any]]:
        """Return all safety rules."""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            # Two explicit branches avoids dynamic f-string for query-plan caching.
            if enabled_only:
                cursor.execute(
                    "SELECT id, name, description, pattern, pattern_type, weight, category, tool_scope, scope, enabled, is_system, created_at, updated_at FROM safety_rules WHERE enabled = 1 "
                    "ORDER BY is_system DESC, weight DESC, name"
                )
            else:
                cursor.execute(
                    "SELECT id, name, description, pattern, pattern_type, weight, category, tool_scope, scope, enabled, is_system, created_at, updated_at FROM safety_rules ORDER BY is_system DESC, weight DESC, name"
                )
            return [dict(row) for row in cursor.fetchall()]

    def get_safety_rules_for_agent(self, agent_id: str, enabled_only: bool = True) -> List[Dict[str, Any]]:
        """Return rules applicable to a given agent: all global + specific rules assigned to this agent."""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            # Two branches avoids dynamic f-string; all table/column names are compile-time constants.
            if enabled_only:
                cursor.execute(
                    "SELECT sr.* FROM safety_rules sr "
                    "WHERE sr.scope = 'global' AND sr.enabled = 1 "
                    "UNION "
                    "SELECT sr.* FROM safety_rules sr "
                    "JOIN agent_safety_rules asr ON asr.rule_id = sr.id "
                    "WHERE asr.agent_id = ? AND sr.scope = 'specific' AND sr.enabled = 1 "
                    "ORDER BY weight DESC, name",
                    (agent_id,)
                )
            else:
                cursor.execute(
                    "SELECT sr.* FROM safety_rules sr "
                    "WHERE sr.scope = 'global' "
                    "UNION "
                    "SELECT sr.* FROM safety_rules sr "
                    "JOIN agent_safety_rules asr ON asr.rule_id = sr.id "
                    "WHERE asr.agent_id = ? AND sr.scope = 'specific' "
                    "ORDER BY weight DESC, name",
                    (agent_id,)
                )
            return [dict(row) for row in cursor.fetchall()]

    def get_safety_rule(self, rule_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT id, name, description, pattern, pattern_type, weight, category, tool_scope, scope, enabled, is_system, created_at, updated_at FROM safety_rules WHERE id = ?", (rule_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def create_safety_rule(self, data: Dict[str, Any]) -> str:
        rule_id = data.get('id') or str(uuid.uuid4())
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO safety_rules (id, name, description, pattern, pattern_type, weight, category,
                    tool_scope, scope, enabled, is_system)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                rule_id,
                data['name'],
                data.get('description', ''),
                data['pattern'],
                data.get('pattern_type', 'regex'),
                data.get('weight', 5),
                data['category'],
                data.get('tool_scope', 'all'),
                data.get('scope', 'global'),
                1 if data.get('enabled', True) else 0,
                1 if data.get('is_system', False) else 0,
            ))
            conn.commit()
        return rule_id

    def update_safety_rule(self, rule_id: str, data: Dict[str, Any]) -> bool:
        allowed = {'name', 'description', 'pattern', 'pattern_type', 'weight',
                   'category', 'tool_scope', 'scope', 'enabled'}
        updates = {k: v for k, v in data.items() if k in allowed}
        if not updates:
            return False
        updates['updated_at'] = 'CURRENT_TIMESTAMP'
        set_clause = ", ".join(
            f"{k} = CURRENT_TIMESTAMP" if v == 'CURRENT_TIMESTAMP' else f"{k} = ?"
            for k, v in updates.items()
        )
        params = [v for v in updates.values() if v != 'CURRENT_TIMESTAMP']
        params.append(rule_id)
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"UPDATE safety_rules SET {set_clause} WHERE id = ? AND is_system = 0",
                params,
            )
            conn.commit()
            return cursor.rowcount > 0

    def delete_safety_rule(self, rule_id: str) -> bool:
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM safety_rules WHERE id = ? AND is_system = 0", (rule_id,))
            conn.commit()
            return cursor.rowcount > 0

    # ---- Agent ↔ Safety Rule assignments ----

    def get_agent_safety_rules(self, agent_id: str) -> List[str]:
        """Return rule IDs assigned to a specific agent."""
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT rule_id FROM agent_safety_rules WHERE agent_id = ?", (agent_id,))
            return [row[0] for row in cursor.fetchall()]

    def set_agent_safety_rules(self, agent_id: str, rule_ids: List[str]) -> None:
        """Replace all safety rule assignments for an agent."""
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM agent_safety_rules WHERE agent_id = ?", (agent_id,))
            for rid in rule_ids:
                cursor.execute(
                    "INSERT OR IGNORE INTO agent_safety_rules (agent_id, rule_id) VALUES (?, ?)",
                    (agent_id, rid),
                )
            conn.commit()

    def get_specific_rules_with_agents(self) -> List[Dict[str, Any]]:
        """Return all specific-scope rules with their assigned agent IDs."""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("""
                SELECT sr.*, GROUP_CONCAT(asr.agent_id) as assigned_agents
                FROM safety_rules sr
                LEFT JOIN agent_safety_rules asr ON asr.rule_id = sr.id
                WHERE sr.scope = 'specific'
                GROUP BY sr.id
                ORDER BY sr.weight DESC, sr.name
            """)
            return [dict(row) for row in cursor.fetchall()]
