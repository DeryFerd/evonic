import sqlite3
from typing import Dict, Any, List, Optional


class ModelsMixin:
    """LLM model CRUD and model selection. Requires self._connect() from the host class."""

    def get_llm_models(self) -> List[Dict[str, Any]]:
        """Return list of all model configs."""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT id, name, type, provider, base_url, api_key, model_name, max_tokens, timeout, thinking, thinking_budget, temperature, enabled, is_default, created_at, updated_at, model_max_concurrent, api_format, vision_supported, attachments_supported FROM llm_models ORDER BY name LIMIT 1000")
            return [dict(row) for row in cursor.fetchall()]

    def get_enabled_llm_models(self) -> List[Dict[str, Any]]:
        """Return list of only enabled model configs (enabled=1)."""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT id, name, type, provider, base_url, api_key, model_name, max_tokens, timeout, thinking, thinking_budget, temperature, enabled, is_default, created_at, updated_at, model_max_concurrent, api_format, vision_supported, attachments_supported FROM llm_models WHERE enabled = 1 ORDER BY name LIMIT 1000")
            return [dict(row) for row in cursor.fetchall()]

    def save_llm_models(self, models_list: List[Dict[str, Any]]) -> None:
        """Persist models to llm_models table."""
        with self._connect() as conn:
            cursor = conn.cursor()
            # Clear existing models
            cursor.execute("DELETE FROM llm_models")
            for m in models_list:
                cursor.execute("""
                    INSERT INTO llm_models (id, name, type, provider, base_url, api_key,
                        model_name, max_tokens, timeout, thinking, thinking_budget,
                        temperature, enabled, is_default, model_max_concurrent, api_format,
                        vision_supported, attachments_supported)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    m.get('id'),
                    m.get('name'),
                    m.get('type'),
                    m.get('provider'),
                    m.get('base_url'),
                    m.get('api_key'),
                    m.get('model_name'),
                    m.get('max_tokens', 32768),
                    m.get('timeout', 60),
                    m.get('thinking', 0),
                    m.get('thinking_budget', 0),
                    m.get('temperature'),
                    m.get('enabled', 1),
                    m.get('is_default', 0),
                    m.get('model_max_concurrent', 1),
                    m.get('api_format', 'openai'),
                    m.get('vision_supported', 0),
                    m.get('attachments_supported', 0),
                ))
            conn.commit()

    def get_default_model(self) -> Optional[Dict[str, Any]]:
        """Return global default model (is_default=1)."""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT id, name, type, provider, base_url, api_key, model_name, max_tokens, timeout, thinking, thinking_budget, temperature, enabled, is_default, created_at, updated_at, model_max_concurrent, api_format, vision_supported, attachments_supported FROM llm_models WHERE is_default = 1 LIMIT 1")
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_model_by_id(self, model_id: str) -> Optional[Dict[str, Any]]:
        """Lookup model by ID."""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT id, name, type, provider, base_url, api_key, model_name, max_tokens, timeout, thinking, thinking_budget, temperature, enabled, is_default, created_at, updated_at, model_max_concurrent, api_format, vision_supported, attachments_supported FROM llm_models WHERE id = ?", (model_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_model_by_model_name(self, model_name: str) -> Optional[Dict[str, Any]]:
        """Lookup model by its model_name field (the API model identifier)."""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT id, name, type, provider, base_url, api_key, model_name, max_tokens, timeout, thinking, thinking_budget, temperature, enabled, is_default, created_at, updated_at, model_max_concurrent, api_format, vision_supported, attachments_supported FROM llm_models WHERE model_name = ? LIMIT 1", (model_name,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_agent_model(self, agent_id: str) -> Optional[Dict[str, Any]]:
        """Return agent's primary model (model_id) or global default or None."""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            # First try agent-specific model
            cursor.execute("SELECT model_id FROM agents WHERE id = ?", (agent_id,))
            row = cursor.fetchone()
            if row and row['model_id']:
                cursor.execute("SELECT id, name, type, provider, base_url, api_key, model_name, max_tokens, timeout, thinking, thinking_budget, temperature, enabled, is_default, created_at, updated_at, model_max_concurrent, api_format, vision_supported, attachments_supported FROM llm_models WHERE id = ?", (row['model_id'],))
                model_row = cursor.fetchone()
                if model_row:
                    return dict(model_row)
            # Fallback to global default
            cursor.execute("SELECT id, name, type, provider, base_url, api_key, model_name, max_tokens, timeout, thinking, thinking_budget, temperature, enabled, is_default, created_at, updated_at, model_max_concurrent, api_format, vision_supported, attachments_supported FROM llm_models WHERE is_default = 1 LIMIT 1")
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_agent_default_model(self, agent_id: str) -> Optional[Dict[str, Any]]:
        """Deprecated alias — use get_agent_model()."""
        return self.get_agent_model(agent_id)

    def set_agent_model(self, agent_id: str, model_id: Optional[str]) -> bool:
        """Set agent's primary model. model_id can be None to clear."""
        with self._connect() as conn:
            cursor = conn.cursor()
            if model_id:
                # Verify model exists
                cursor.execute("SELECT 1 FROM llm_models WHERE id = ?", (model_id,))
                if not cursor.fetchone():
                    return False
            cursor.execute(
                "UPDATE agents SET model_id = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (model_id, agent_id)
            )
            conn.commit()
            return cursor.rowcount > 0

    def set_agent_default_model(self, agent_id: str, model_id: Optional[str]) -> bool:
        """Deprecated alias — use set_agent_model()."""
        return self.set_agent_model(agent_id, model_id)

    def get_agent_fallback_model(self, agent_id: str) -> Optional[Dict[str, Any]]:
        """Return agent's fallback model or None.

        Unlike get_agent_default_model, there is no global fallback —
        if the agent has no fallback configured, returns None.
        """
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT fallback_model_id FROM agents WHERE id = ?", (agent_id,))
            row = cursor.fetchone()
            if row and row["fallback_model_id"]:
                cursor.execute("SELECT id, name, type, provider, base_url, api_key, model_name, max_tokens, timeout, thinking, thinking_budget, temperature, enabled, is_default, created_at, updated_at, model_max_concurrent, api_format, vision_supported, attachments_supported FROM llm_models WHERE id = ?", (row["fallback_model_id"],))
                model_row = cursor.fetchone()
                if model_row:
                    return dict(model_row)
            return None

    def set_agent_fallback_model(self, agent_id: str, model_id: Optional[str]) -> bool:
        """Set agent's fallback model. model_id can be None to clear."""
        with self._connect() as conn:
            cursor = conn.cursor()
            if model_id:
                # Verify model exists
                cursor.execute("SELECT 1 FROM llm_models WHERE id = ?", (model_id,))
                if not cursor.fetchone():
                    return False
            cursor.execute(
                "UPDATE agents SET fallback_model_id = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (model_id, agent_id)
            )
            conn.commit()
            return cursor.rowcount > 0

    def create_model(self, model_data: Dict[str, Any]) -> str:
        """Create a new model. Returns model ID."""
        import uuid
        model_id = model_data.get('id') or str(uuid.uuid4())
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO llm_models (id, name, type, provider, base_url, api_key,
                    model_name, max_tokens, timeout, thinking, thinking_budget,
                    temperature, enabled, is_default, model_max_concurrent, api_format,
                    vision_supported, attachments_supported)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                model_id,
                model_data.get('name'),
                model_data.get('type'),
                model_data.get('provider'),
                model_data.get('base_url'),
                model_data.get('api_key'),
                model_data.get('model_name'),
                model_data.get('max_tokens', 32768),
                model_data.get('timeout', 60),
                model_data.get('thinking', 0),
                model_data.get('thinking_budget', 0),
                model_data.get('temperature'),
                model_data.get('enabled', 1),
                model_data.get('is_default', 0),
                model_data.get('model_max_concurrent', 1),
                model_data.get('api_format', 'openai'),
                model_data.get('vision_supported', 0),
                model_data.get('attachments_supported', 0),
            ))
            conn.commit()
        return model_id

    def update_model(self, model_id: str, model_data: Dict[str, Any]) -> bool:
        """Update an existing model."""
        allowed = {'name', 'type', 'provider', 'base_url', 'api_key', 'model_name',
                   'max_tokens', 'timeout', 'thinking', 'thinking_budget', 'temperature', 'enabled', 'is_default',
                   'model_max_concurrent', 'api_format', 'vision_supported', 'attachments_supported'}
        updates = {k: v for k, v in model_data.items() if k in allowed}
        if not updates:
            return False
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [model_id]
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"UPDATE llm_models SET {set_clause}, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                values
            )
            conn.commit()
            return cursor.rowcount > 0

    def delete_model(self, model_id: str) -> bool:
        """Delete a model."""
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM llm_models WHERE id = ?", (model_id,))
            conn.commit()
            return cursor.rowcount > 0
