"""
Tests for plugin-provided agent tools.

Plugins can declare a `tools_file` in plugin.json (a flat JSON array of
function defs, mirroring skills) and ship backends under
plugins/<id>/backend/tools/<fn>.py. Covers:
1. Def loading/tagging (`plugin:<id>:<fn>`), mtime cache, enabled gating.
2. Module loading via the per-plugin `plugin_tools_<id>` namespace package —
   relative imports resolve to the plugin's own dir and never collide
   across plugins (unlike the shared `tools.<name>` spec the skills path uses).
3. Executor routing + authorization for both namespaced and bare tool IDs.
"""

import json
import os
import sys
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.plugin_lifecycle import PluginManager
from backend.tools.registry import ToolRegistry


def _make_plugin(plugins_dir, plugin_id='demo_plugin', fn_name='demo_plugin_tool',
                 helper_value='ONE'):
    plugin_dir = plugins_dir / plugin_id
    (plugin_dir / 'backend' / 'tools').mkdir(parents=True)
    (plugin_dir / 'plugin.json').write_text(json.dumps({
        'id': plugin_id,
        'name': 'Demo Plugin',
        'version': '1.0.0',
        'enabled': True,
        'events': [],
        'tools_file': 'tools.json',
    }))
    (plugin_dir / 'tools.json').write_text(json.dumps([
        {'function': {'name': fn_name, 'description': 'a demo plugin tool',
                      'parameters': {'type': 'object', 'properties': {}}},
         'mock_response': '{"ok": true}'}
    ]))
    (plugin_dir / 'backend' / 'tools' / '_helper.py').write_text(
        f"VALUE = '{helper_value}'\n")
    (plugin_dir / 'backend' / 'tools' / f'{fn_name}.py').write_text(
        "from ._helper import VALUE\n\n\n"
        "def execute(agent, args):\n"
        "    return {'result': VALUE}\n")
    return plugin_dir


@pytest.fixture
def plugins_env(tmp_path, monkeypatch):
    """A PluginManager pointed at a tmp plugins dir with one enabled plugin.

    Cleans up plugin_tools_* namespace modules afterwards — helper submodules
    persist in sys.modules by design, which would leak stale code between
    tests that reuse plugin ids under different tmp dirs.
    """
    plugins_dir = tmp_path / 'plugins'
    plugins_dir.mkdir()
    _make_plugin(plugins_dir)
    monkeypatch.setattr('backend.plugin_lifecycle.PLUGINS_DIR', str(plugins_dir))
    enabled = {'demo_plugin': True}
    monkeypatch.setattr(PluginManager, '_is_plugin_enabled',
                        lambda self, pid: enabled.get(pid, False))
    pm = PluginManager()
    monkeypatch.setattr('backend.plugin_manager.plugin_manager', pm)
    yield pm, plugins_dir, enabled
    for key in [k for k in sys.modules if k.startswith('plugin_tools_')]:
        del sys.modules[key]


class TestPluginToolDefs:
    def test_defs_tagged_with_plugin_origin(self, plugins_env):
        pm, plugins_dir, _ = plugins_env
        defs = pm.get_all_plugin_tool_defs()
        assert len(defs) == 1
        d = defs[0]
        assert d['id'] == 'plugin:demo_plugin:demo_plugin_tool'
        assert d['_plugin_id'] == 'demo_plugin'
        assert d['_plugin_dir'] == str(plugins_dir / 'demo_plugin')
        assert d['function']['name'] == 'demo_plugin_tool'
        assert d['mock_response'] == '{"ok": true}'

    def test_caller_mutations_do_not_pollute_cache(self, plugins_env):
        pm, _, _ = plugins_env
        defs = pm.get_all_plugin_tool_defs()
        defs[0]['function']['name'] = 'HACKED'
        defs[0]['injected'] = True

        fresh = pm.get_all_plugin_tool_defs()
        assert fresh[0]['function']['name'] == 'demo_plugin_tool'
        assert 'injected' not in fresh[0]

    def test_tools_file_edit_invalidates_cache(self, plugins_env):
        pm, plugins_dir, _ = plugins_env
        assert len(pm.get_all_plugin_tool_defs()) == 1  # prime the cache

        tools_path = plugins_dir / 'demo_plugin' / 'tools.json'
        tools = json.loads(tools_path.read_text())
        tools.append({'function': {'name': 'second_tool', 'description': '',
                                   'parameters': {'type': 'object', 'properties': {}}}})
        tools_path.write_text(json.dumps(tools))
        os.utime(tools_path, (os.path.getmtime(tools_path) + 1,) * 2)

        names = [d['function']['name'] for d in pm.get_all_plugin_tool_defs()]
        assert names == ['demo_plugin_tool', 'second_tool']

    def test_disabled_plugin_excluded(self, plugins_env):
        pm, _, enabled = plugins_env
        assert len(pm.get_all_plugin_tool_defs()) == 1
        enabled['demo_plugin'] = False
        assert pm.get_all_plugin_tool_defs() == []

    def test_find_plugin_tool_backend(self, plugins_env):
        pm, plugins_dir, enabled = plugins_env
        path, pid = pm.find_plugin_tool_backend('demo_plugin_tool')
        assert path == str(plugins_dir / 'demo_plugin' / 'backend' / 'tools' / 'demo_plugin_tool.py')
        assert pid == 'demo_plugin'
        # Hint mismatch and disabled plugin both miss
        assert pm.find_plugin_tool_backend('demo_plugin_tool', plugin_id='other') == (None, None)
        enabled['demo_plugin'] = False
        assert pm.find_plugin_tool_backend('demo_plugin_tool') == (None, None)
        # Malicious names rejected
        assert pm.find_plugin_tool_backend('../evil') == (None, None)


class TestPluginToolModuleLoading:
    def test_load_with_and_without_hint(self, plugins_env):
        registry = ToolRegistry()
        module = registry._load_tool_module('demo_plugin_tool', plugin_id='demo_plugin')
        assert module is not None
        assert module.execute({}, {}) == {'result': 'ONE'}
        assert 'plugin_tools_demo_plugin._helper' in sys.modules

        # Bare-id path: no hint → core miss → skill miss → plugin search hit
        module2 = registry._load_tool_module('demo_plugin_tool')
        assert module2 is not None
        assert module2.execute({}, {}) == {'result': 'ONE'}

    def test_no_collision_between_plugins(self, plugins_env):
        _, plugins_dir, enabled = plugins_env
        _make_plugin(plugins_dir, plugin_id='other_plugin',
                     fn_name='other_plugin_tool', helper_value='TWO')
        enabled['other_plugin'] = True

        registry = ToolRegistry()
        mod_a = registry._load_tool_module('demo_plugin_tool', plugin_id='demo_plugin')
        mod_b = registry._load_tool_module('other_plugin_tool', plugin_id='other_plugin')
        # Each plugin's _helper resolved from its own dir despite the same filename
        assert mod_a.execute({}, {}) == {'result': 'ONE'}
        assert mod_b.execute({}, {}) == {'result': 'TWO'}
        assert 'plugin_tools_demo_plugin._helper' in sys.modules
        assert 'plugin_tools_other_plugin._helper' in sys.modules

    def test_registry_get_all_tool_defs_includes_plugins(self, plugins_env, tmp_path, monkeypatch):
        defs_dir = tmp_path / 'tooldefs'
        defs_dir.mkdir()
        (defs_dir / 'base_tool.json').write_text(json.dumps(
            {'id': 'base_tool', 'function': {'name': 'base_tool'}}
        ))
        monkeypatch.setattr('backend.tools.registry.TOOL_DEFS_DIR', str(defs_dir))

        registry = ToolRegistry()
        ids = [d.get('id') for d in registry.get_all_tool_defs()]
        assert 'base_tool' in ids
        assert 'plugin:demo_plugin:demo_plugin_tool' in ids
        # The JSON-only cache must stay free of plugin defs
        assert [d['id'] for d in registry.get_tool_defs_from_json()] == ['base_tool']


class TestPluginToolExecutor:
    def test_namespaced_and_bare_ids_route_and_authorize(self, plugins_env):
        registry = ToolRegistry()

        ex = registry.get_real_executor(
            {'assigned_tool_ids': ['plugin:demo_plugin:demo_plugin_tool']})
        assert ex('demo_plugin_tool', {}) == {'result': 'ONE'}

        ex_bare = registry.get_real_executor(
            {'assigned_tool_ids': ['demo_plugin_tool']})
        assert ex_bare('demo_plugin_tool', {}) == {'result': 'ONE'}

    def test_unassigned_tool_blocked(self, plugins_env):
        registry = ToolRegistry()
        ex = registry.get_real_executor({'assigned_tool_ids': []})
        result = ex('demo_plugin_tool', {})
        assert result.get('blocked_by') == 'authorization'
