"""Tests for the one-shot model id migration to 'provider/model_name' format
and the legacy_id backward-compatibility fallback."""

import pytest

from models.db import db


def _insert_model(model_id, provider, model_name, name=None):
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO llm_models (id, name, type, provider, base_url, model_name) "
            "VALUES (?, ?, 'local', ?, '', ?)",
            (model_id, name or model_name, provider, model_name),
        )
        conn.commit()


def _get_row(where_col, value):
    with db._connect() as conn:
        cur = conn.execute(
            f"SELECT id, legacy_id FROM llm_models WHERE {where_col} = ?", (value,)
        )
        row = cur.fetchone()
        return tuple(row) if row is not None else None


def _run_migration():
    # The fresh test DB already has the v2 marker; clear it so the
    # migration runs against the seeded pre-v2 rows.
    with db._connect() as conn:
        conn.execute("DELETE FROM app_settings WHERE key = 'model_id_format'")
        conn.commit()
    db._init_tables()


UUID_A = "176a5833-b9fd-43de-9d21-029e86d75b7e"
UUID_B = "f03b847e-2dbb-4480-91c9-179aefdd37f0"


class TestMigration:
    def test_renames_uuid_and_setup_ids(self):
        _insert_model(UUID_A, "llama.cpp", "Gemma4-12B")
        _insert_model("setup_ollama", "ollama", "llama3")
        _run_migration()

        assert _get_row("legacy_id", UUID_A) == ("llama.cpp/Gemma4-12B", UUID_A)
        assert _get_row("legacy_id", "setup_ollama") == ("ollama/llama3", "setup_ollama")
        assert db.get_setting("model_id_format") == "v2"

    def test_already_final_id_kept_without_legacy(self):
        _insert_model("llama.cpp/Gemma4-12B", "llama.cpp", "Gemma4-12B")
        _run_migration()

        assert _get_row("id", "llama.cpp/Gemma4-12B") == ("llama.cpp/Gemma4-12B", None)

    def test_collision_gets_suffix(self):
        _insert_model("llama.cpp/Gemma4-12B", "llama.cpp", "Gemma4-12B")
        _insert_model(UUID_A, "llama.cpp", "Gemma4-12B")
        _insert_model(UUID_B, "llama.cpp", "Gemma4-12B")
        _run_migration()

        renamed = {_get_row("legacy_id", UUID_A)[0], _get_row("legacy_id", UUID_B)[0]}
        assert renamed == {"llama.cpp/Gemma4-12B-2", "llama.cpp/Gemma4-12B-3"}

    def test_cascades_to_agents_and_settings(self):
        _insert_model(UUID_A, "llama.cpp", "Gemma4-12B")
        _insert_model(UUID_B, "openrouter", "qwen/qwen3.6-plus")
        db.create_agent({"id": "mig_agent", "name": "Mig", "system_prompt": ""})
        with db._connect() as conn:
            conn.execute(
                "UPDATE agents SET model_id = ?, fallback_model_id = ? WHERE id = 'mig_agent'",
                (UUID_A, UUID_B),
            )
            conn.commit()
        db.set_setting("task_classifier_model_id", UUID_A)
        db.set_setting("vision_model_id", UUID_B)
        _run_migration()

        with db._connect() as conn:
            row = conn.execute(
                "SELECT model_id, fallback_model_id FROM agents WHERE id = 'mig_agent'"
            ).fetchone()
        assert tuple(row) == ("llama.cpp/Gemma4-12B", "openrouter/qwen/qwen3.6-plus")
        assert db.get_setting("task_classifier_model_id") == "llama.cpp/Gemma4-12B"
        assert db.get_setting("vision_model_id") == "openrouter/qwen/qwen3.6-plus"

    def test_idempotent_and_ids_stable_after_edit(self):
        _insert_model(UUID_A, "llama.cpp", "Gemma4-12B")
        _run_migration()
        new_id = _get_row("legacy_id", UUID_A)[0]

        # Second startup: no-op
        db._init_tables()
        assert _get_row("legacy_id", UUID_A)[0] == new_id

        # Editing model_name must NOT re-rename the id on later startups
        db.update_model(new_id, {"model_name": "Gemma4-27B"})
        db._init_tables()
        assert db.get_model_by_id(new_id)["model_name"] == "Gemma4-27B"
        assert _get_row("legacy_id", UUID_A)[0] == new_id


class TestLegacyFallback:
    def test_get_model_by_id_resolves_legacy(self):
        _insert_model(UUID_A, "llama.cpp", "Gemma4-12B")
        _run_migration()

        model = db.get_model_by_id(UUID_A)
        assert model is not None
        assert model["id"] == "llama.cpp/Gemma4-12B"
        assert model["legacy_id"] == UUID_A

    def test_set_agent_model_canonicalizes_legacy_id(self):
        _insert_model(UUID_A, "llama.cpp", "Gemma4-12B")
        _run_migration()
        db.create_agent({"id": "leg_agent", "name": "Leg", "system_prompt": ""})

        assert db.set_agent_model("leg_agent", UUID_A) is True
        with db._connect() as conn:
            row = conn.execute(
                "SELECT model_id FROM agents WHERE id = 'leg_agent'"
            ).fetchone()
        assert row[0] == "llama.cpp/Gemma4-12B"

    def test_set_agent_fallback_model_canonicalizes_legacy_id(self):
        _insert_model(UUID_A, "llama.cpp", "Gemma4-12B")
        _run_migration()
        db.create_agent({"id": "leg_agent2", "name": "Leg2", "system_prompt": ""})

        assert db.set_agent_fallback_model("leg_agent2", UUID_A) is True
        with db._connect() as conn:
            row = conn.execute(
                "SELECT fallback_model_id FROM agents WHERE id = 'leg_agent2'"
            ).fetchone()
        assert row[0] == "llama.cpp/Gemma4-12B"


class TestCreateModel:
    def test_generates_provider_model_name_id(self):
        new_id = db.create_model(
            {"name": "G", "type": "local", "provider": "llama.cpp",
             "model_name": "Gemma4-12B"}
        )
        assert new_id == "llama.cpp/Gemma4-12B"
        assert db.get_model_by_id(new_id)["model_max_concurrent"] == 3

    def test_explicit_model_max_concurrent_is_preserved(self):
        new_id = db.create_model(
            {"name": "G", "type": "local", "provider": "llama.cpp",
             "model_name": "Gemma4-27B", "model_max_concurrent": 7}
        )
        assert db.get_model_by_id(new_id)["model_max_concurrent"] == 7

    def test_duplicate_auto_id_gets_suffix(self):
        first = db.create_model(
            {"name": "G", "type": "local", "provider": "llama.cpp",
             "model_name": "Gemma4-12B"}
        )
        second = db.create_model(
            {"name": "G copy", "type": "local", "provider": "llama.cpp",
             "model_name": "Gemma4-12B"}
        )
        assert first == "llama.cpp/Gemma4-12B"
        assert second == "llama.cpp/Gemma4-12B-2"

    def test_explicit_duplicate_id_raises(self):
        db.create_model(
            {"id": "custom-id", "name": "A", "type": "local",
             "provider": "llama.cpp", "model_name": "m1"}
        )
        with pytest.raises(ValueError):
            db.create_model(
                {"id": "custom-id", "name": "B", "type": "local",
                 "provider": "llama.cpp", "model_name": "m2"}
            )

    def test_new_id_cannot_shadow_legacy_alias(self):
        _insert_model(UUID_A, "llama.cpp", "Gemma4-12B")
        _run_migration()
        # UUID_A now lives in legacy_id; a new model must not claim it
        with pytest.raises(ValueError):
            db.create_model(
                {"id": UUID_A, "name": "X", "type": "local",
                 "provider": "llama.cpp", "model_name": "other"}
            )
