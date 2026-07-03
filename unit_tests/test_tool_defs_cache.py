"""
Regression tests for tool-definition caching.

Covers three bugs found in the 2026-06 performance audit:
1. ToolRegistry.get_all_tool_defs() extended the cached JSON defs list in
   place, duplicating skill defs into the cache on every call.
2. SkillsManager re-read and re-parsed skill.json / tools files from disk on
   every list_skills() / get_all_skill_tool_defs() call; the new mtime cache
   must not leak caller mutations between calls and must pick up file edits.
3. /api/summary/<run_id> only ever found the most recent evaluation run.
"""

import json
import os
import sys
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.skills_manager import SkillsManager
from backend.tools.registry import ToolRegistry


def _make_skill(skills_dir, skill_id='demo_skill', fn_name='demo_tool'):
    skill_dir = skills_dir / skill_id
    skill_dir.mkdir(parents=True)
    (skill_dir / 'skill.json').write_text(json.dumps({
        'id': skill_id,
        'name': 'Demo Skill',
        'version': '1.0.0',
        'tools_file': 'tools.json',
    }))
    (skill_dir / 'tools.json').write_text(json.dumps([
        {'function': {'name': fn_name, 'description': 'a demo tool',
                      'parameters': {'type': 'object', 'properties': {}}}}
    ]))
    return skill_dir


@pytest.fixture
def skills_env(tmp_path, monkeypatch):
    """A SkillsManager pointed at a tmp skills dir with one enabled skill."""
    skills_dir = tmp_path / 'skills'
    _make_skill(skills_dir)
    monkeypatch.setattr('backend.skills_manager.SKILLS_DIR', str(skills_dir))
    sm = SkillsManager()
    from models.db import db
    db.set_setting('skill_enabled:demo_skill', '1')
    return sm, skills_dir


class TestRegistryCacheNotMutated:
    def test_get_all_tool_defs_does_not_grow_json_cache(self, tmp_path, monkeypatch, skills_env):
        sm, _ = skills_env
        defs_dir = tmp_path / 'tooldefs'
        defs_dir.mkdir()
        (defs_dir / 'base_tool.json').write_text(json.dumps(
            {'id': 'base_tool', 'function': {'name': 'base_tool'}}
        ))
        monkeypatch.setattr('backend.tools.registry.TOOL_DEFS_DIR', str(defs_dir))
        monkeypatch.setattr('backend.skills_manager.skills_manager', sm)
        # Keep the test hermetic: real installed plugins may contribute defs
        monkeypatch.setattr(
            'backend.plugin_manager.plugin_manager.get_all_plugin_tool_defs',
            lambda: [])

        registry = ToolRegistry()
        first = registry.get_all_tool_defs()
        second = registry.get_all_tool_defs()
        third = registry.get_all_tool_defs()

        assert len(first) == len(second) == len(third) == 2
        # The JSON-only cache must stay free of skill defs
        assert [d['id'] for d in registry.get_tool_defs_from_json()] == ['base_tool']


class TestSkillsManagerCache:
    def test_list_skills_results_stable_across_calls(self, skills_env):
        sm, _ = skills_env
        first = sm.list_skills()
        second = sm.list_skills()
        assert first == second
        assert first[0]['id'] == 'demo_skill'
        assert first[0]['tool_count'] == 1
        assert first[0]['enabled'] is True

    def test_caller_mutations_do_not_pollute_cache(self, skills_env):
        sm, _ = skills_env
        defs = sm.get_all_skill_tool_defs()
        assert defs[0]['id'] == 'skill:demo_skill:demo_tool'
        # Mutate nested state on the returned defs
        defs[0]['function']['name'] = 'HACKED'
        defs[0]['injected'] = True

        fresh = sm.get_all_skill_tool_defs()
        assert fresh[0]['function']['name'] == 'demo_tool'
        assert 'injected' not in fresh[0]

    def test_tools_file_edit_invalidates_cache(self, skills_env):
        sm, skills_dir = skills_env
        assert sm.list_skills()[0]['tool_count'] == 1  # prime the cache

        tools_path = skills_dir / 'demo_skill' / 'tools.json'
        tools = json.loads(tools_path.read_text())
        tools.append({'function': {'name': 'second_tool', 'description': '',
                                   'parameters': {'type': 'object', 'properties': {}}}})
        tools_path.write_text(json.dumps(tools))
        os.utime(tools_path, (os.path.getmtime(tools_path) + 1,) * 2)

        assert sm.list_skills()[0]['tool_count'] == 2
        names = [d['function']['name'] for d in sm.get_all_skill_tool_defs()]
        assert names == ['demo_tool', 'second_tool']

    def test_disabled_skill_excluded_without_file_change(self, skills_env):
        sm, _ = skills_env
        assert len(sm.get_all_skill_tool_defs()) == 1
        from models.db import db
        db.set_setting('skill_enabled:demo_skill', '0')
        assert sm.get_all_skill_tool_defs() == []


class TestApiSummaryFindsOldRuns:
    def test_summary_for_non_latest_run(self):
        from app import app
        from models.db import db
        old_run = db.create_evaluation_run('model-a')
        new_run = db.create_evaluation_run('model-b')
        assert old_run != new_run

        with app.test_client() as client:
            with client.session_transaction() as sess:
                sess['authenticated'] = True
            resp = client.get(f'/api/summary/{old_run}')
            assert resp.status_code == 200
            assert resp.get_json()['success'] is True

            resp = client.get('/api/summary/999999')
            assert resp.status_code == 404
