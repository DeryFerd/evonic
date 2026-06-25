import sqlite3
import json
from typing import Dict, Any, List, Optional


class ToolsMixin:
    """Tool definition CRUD operations. Requires self._connect() from the host class."""

    def get_tools(self) -> List[Dict[str, Any]]:
        """Get all tools"""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT id, name, description, function_def, mock_response, mock_response_type, path, created_at, updated_at FROM tools ORDER BY name")
            results = []
            for row in cursor.fetchall():
                d = dict(row)
                if d.get('function_def'):
                    d['function_def'] = json.loads(d['function_def'])
                if d.get('mock_response') and d.get('mock_response_type', 'json') == 'json':
                    try:
                        d['mock_response'] = json.loads(d['mock_response'])
                    except (json.JSONDecodeError, TypeError):
                        pass
                results.append(d)
            return results

    def get_tool(self, tool_id: str) -> Optional[Dict[str, Any]]:
        """Get a single tool by ID"""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT id, name, description, function_def, mock_response, mock_response_type, path, created_at, updated_at FROM tools WHERE id = ?", (tool_id,))
            row = cursor.fetchone()
            if not row:
                return None
            d = dict(row)
            if d.get('function_def'):
                d['function_def'] = json.loads(d['function_def'])
            if d.get('mock_response') and d.get('mock_response_type', 'json') == 'json':
                try:
                    d['mock_response'] = json.loads(d['mock_response'])
                except (json.JSONDecodeError, TypeError):
                    pass
            return d

    def upsert_tool(self, tool: Dict[str, Any]) -> str:
        """Insert or update a tool"""
        with self._connect() as conn:
            cursor = conn.cursor()
            function_def = json.dumps(tool['function_def']) if isinstance(tool.get('function_def'), dict) else tool.get('function_def')
            mock_response = tool.get('mock_response')
            if isinstance(mock_response, (dict, list)):
                mock_response = json.dumps(mock_response)
            cursor.execute("""
                INSERT INTO tools (id, name, description, function_def, mock_response, mock_response_type, path, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    description = excluded.description,
                    function_def = excluded.function_def,
                    mock_response = excluded.mock_response,
                    mock_response_type = excluded.mock_response_type,
                    path = excluded.path,
                    updated_at = CURRENT_TIMESTAMP
            """, (
                tool['id'], tool.get('name'), tool.get('description'),
                function_def, mock_response,
                tool.get('mock_response_type', 'json'), tool.get('path')
            ))
            conn.commit()
        return tool['id']

    def delete_tool(self, tool_id: str) -> bool:
        """Delete a tool"""
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM tools WHERE id = ?", (tool_id,))
            conn.commit()
            return cursor.rowcount > 0
