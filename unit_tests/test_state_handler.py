"""Unit tests for the state handler registry and kanban _state_handler."""

import sys
import types
import unittest.mock as mock
import pytest


# ─── State handler registry ───────────────────────────────────────────────────

class TestStateHandlerRegistry:
    def setup_method(self):
        import backend.plugin_manager as pm
        import backend.plugin_hooks as ph
        self._pm = pm
        self._ph = ph
        ph._state_handlers.clear()

    def teardown_method(self):
        self._ph._state_handlers.clear()

    def test_register_handler(self):
        fn = lambda agent_id, session_id, state, label, data: None
        self._pm.register_state_handler('kanban', fn)
        assert self._ph._state_handlers['kanban'] is fn

    def test_register_overwrites_same_namespace(self):
        fn1 = lambda *a: None
        fn2 = lambda *a: None
        self._pm.register_state_handler('kanban', fn1)
        self._pm.register_state_handler('kanban', fn2)
        assert self._ph._state_handlers['kanban'] is fn2

    def test_unregister_removes_handler(self):
        fn = lambda *a: None
        self._pm.register_state_handler('kanban', fn)
        self._pm.unregister_state_handler('kanban')
        assert 'kanban' not in self._ph._state_handlers

    def test_unregister_nonexistent_is_noop(self):
        self._pm.unregister_state_handler('nonexistent')  # should not raise

    def test_dispatch_routes_by_namespace_prefix(self):
        calls = []
        def handler(agent_id, session_id, agent_state, label, data):
            calls.append(label)
            return {'success': True, 'state': 'active', 'message': 'ok'}
        self._pm.register_state_handler('kanban', handler)
        result = self._pm.dispatch_state('agent1', 'sess1', None, 'kanban:pick', {'task_id': '1'})
        assert result['success'] is True
        assert calls == ['kanban:pick']

    def test_dispatch_routes_exact_namespace_match(self):
        called = []
        def handler(agent_id, session_id, agent_state, label, data):
            called.append(True)
            return {'success': True, 'state': 'x', 'message': 'ok'}
        self._pm.register_state_handler('deploy', handler)
        result = self._pm.dispatch_state('a', 's', None, 'deploy', {})
        assert result['success'] is True
        assert called

    def test_dispatch_adds_namespace_to_result(self):
        def handler(*a):
            return {'success': True, 'state': 'x', 'message': 'ok'}
        self._pm.register_state_handler('kanban', handler)
        result = self._pm.dispatch_state('a', 's', None, 'kanban:pick', {})
        assert result['namespace'] == 'kanban'

    def test_dispatch_returns_error_when_no_handler(self):
        result = self._pm.dispatch_state('a', 's', None, 'kanban:pick', {})
        assert 'error' in result
        assert 'kanban:pick' in result['error']

    def test_dispatch_skips_handler_that_returns_none(self):
        def passer(*a):
            return None
        def claimer(*a):
            return {'success': True, 'state': 'x', 'message': 'claimed'}
        self._pm.register_state_handler('kanban', passer)
        self._pm.register_state_handler('deploy', claimer)
        result = self._pm.dispatch_state('a', 's', None, 'deploy:lock', {})
        assert result['success'] is True
        assert result['message'] == 'claimed'

    def test_dispatch_returns_error_on_handler_exception(self):
        def bad_handler(*a):
            raise RuntimeError('handler crashed')
        self._pm.register_state_handler('kanban', bad_handler)
        result = self._pm.dispatch_state('a', 's', None, 'kanban:pick', {})
        assert 'error' in result
        assert 'handler crashed' in result['error']

    def test_get_state_summary_returns_states_from_agent_state(self):
        from backend.agent_state import AgentState
        ms = AgentState()
        ms.set_state('kanban', 'pending', data={'task_id': '5'})
        summary = self._pm.get_state_summary(ms)
        assert 'states' in summary
        assert 'kanban' in summary['states']
        assert summary['states']['kanban']['state'] == 'pending'

    def test_get_state_summary_returns_registered_namespaces(self):
        def fn(*a): return None
        self._pm.register_state_handler('kanban', fn)
        from backend.agent_state import AgentState
        summary = self._pm.get_state_summary(AgentState())
        assert 'kanban' in summary['registered_namespaces']

    def test_get_state_summary_handles_none_agent_state(self):
        summary = self._pm.get_state_summary(None)
        assert summary['states'] == {}


# ─── Kanban _state_handler ────────────────────────────────────────────────────

class TestKanbanStateHandler:
    """Tests for _state_handler in the kanban plugin handler.

    Loads the handler module in isolation — stubs out scheduler,
    agent_runtime, and plugin_manager side-effects.
    """

    def setup_method(self):
        # Stub heavy dependencies before import
        if 'backend.agent_runtime' not in sys.modules:
            sys.modules['backend.agent_runtime'] = types.ModuleType('backend.agent_runtime')

        if 'backend.plugin_manager' not in sys.modules:
            mod = types.ModuleType('backend.plugin_manager')
            mod.register_message_interceptor = lambda fn: None
            mod.register_builtin_suppressor = lambda fn: None
            mod.register_state_handler = lambda ns, fn: None
            mod.register_tool_guard = lambda fn: None
            sys.modules['backend.plugin_manager'] = mod
        else:
            # Patch register calls to no-ops so re-registration is harmless
            pm = sys.modules['backend.plugin_manager']
            pm.register_state_handler = lambda ns, fn: None

        if 'plugins.kanban.handler' in sys.modules:
            self._h = sys.modules['plugins.kanban.handler']
        else:
            with mock.patch('plugins.kanban.handler._setup_scheduler'):
                import plugins.kanban.handler as h
                self._h = h

        # Reset state before each test
        self._h._pending_tasks.clear()
        self._h._active_tasks.clear()
        self._h._progress_reminder_armed.clear()

    def _call(self, label, data=None, agent_id='agent1', task_status='in-progress', autopilot=False):
        """Call _state_handler with kanban_db.get() mocked to return a task."""
        from backend.agent_state import AgentState
        ms = AgentState(mode='execute')
        task_id = (data or {}).get('task_id', 'task-1')
        fake_task = {'id': task_id, 'status': task_status}
        autopilot_value = '1' if autopilot else '0'
        with mock.patch('plugins.kanban.db.kanban_db') as mock_db, \
             mock.patch('models.db.db') as mock_db2, \
             mock.patch.object(self._h, '_load_config', return_value={}):
            mock_db.get.return_value = fake_task
            mock_db.get_unmet_dependencies.return_value = []
            mock_db2.get_setting.return_value = autopilot_value
            return self._h._state_handler(agent_id, 'sess1', ms, label, data)

    # ── kanban:pick ───────────────────────────────────────────────────────────

    def test_pick_returns_success(self):
        result = self._call('kanban:pick', {'task_id': 'task-1'})
        assert result['result'] == 'success'

    def test_pick_state_is_pending(self):
        result = self._call('kanban:pick', {'task_id': 'task-1'})
        assert result['state'] == 'pending'

    def test_pick_sets_pending_tasks_dict(self):
        self._call('kanban:pick', {'task_id': 'task-7'})
        assert self._h._pending_tasks.get('agent1') == 'task-7'

    def test_pick_sets_allowed_tools(self):
        result = self._call('kanban:pick', {'task_id': 'task-1'})
        # autopilot=OFF (default): allowed_tools is KANBAN_APPROVAL_PENDING_TOOLS
        assert result['allowed_tools'] is not None
        assert 'state' in result['allowed_tools']
        assert 'kanban_get_task' in result['allowed_tools']
        assert 'kanban_search_tasks' in result['allowed_tools']

    def test_pick_missing_task_id_returns_error(self):
        result = self._call('kanban:pick', {})
        assert result['result'] == 'error'
        assert 'task_id' in result['message']

    def test_pick_missing_data_returns_error(self):
        result = self._call('kanban:pick', None)
        assert result['result'] == 'error'

    def test_pick_blocked_when_already_active_on_different_task(self):
        self._h._active_tasks['agent1'] = 'task-old'
        result = self._call('kanban:pick', {'task_id': 'task-new'})
        assert result['result'] == 'error'
        assert 'task-old' in result['message']

    def test_pick_allowed_when_active_on_same_task(self):
        """Re-picking the same task is allowed (idempotent)."""
        self._h._active_tasks['agent1'] = 'task-1'
        result = self._call('kanban:pick', {'task_id': 'task-1'})
        assert result['result'] == 'success'

    def test_pick_does_not_affect_other_agent(self):
        self._call('kanban:pick', {'task_id': 'task-1'}, agent_id='agent1')
        assert 'agent2' not in self._h._pending_tasks

    # ── kanban:activate ───────────────────────────────────────────────────────

    def test_activate_returns_success_when_in_progress(self):
        result = self._call('kanban:activate', {'task_id': 'task-1'}, task_status='in-progress', autopilot=True)
        assert result['result'] == 'success'

    def test_activate_state_is_active(self):
        result = self._call('kanban:activate', {'task_id': 'task-1'}, task_status='in-progress', autopilot=True)
        assert result['state'] == 'active'

    def test_activate_clears_pending_sets_active(self):
        self._h._pending_tasks['agent1'] = 'task-1'
        self._call('kanban:activate', {'task_id': 'task-1'}, task_status='in-progress', autopilot=True)
        assert 'agent1' not in self._h._pending_tasks
        assert self._h._active_tasks.get('agent1') == 'task-1'

    def test_activate_no_tool_restrictions(self):
        result = self._call('kanban:activate', {'task_id': 'task-1'}, task_status='in-progress', autopilot=True)
        assert result.get('allowed_tools') is None
        assert result.get('blocked_tools') is None

    def test_activate_auto_promotes_todo_to_in_progress(self):
        # kanban:activate now auto-promotes todo→in-progress instead of blocking
        result = self._call('kanban:activate', {'task_id': 'task-1'}, task_status='todo', autopilot=True)
        assert result['result'] == 'success'

    def test_activate_allowed_when_task_already_done(self):
        """If task is 'done', activation is fine (agent may have done it in same turn)."""
        result = self._call('kanban:activate', {'task_id': 'task-1'}, task_status='done', autopilot=True)
        assert result['result'] == 'success'

    def test_activate_missing_task_id_returns_error(self):
        result = self._call('kanban:activate', {})
        assert result['result'] == 'error'

    def test_activate_blocked_when_dep_check_raises(self):
        """Dependency check failure → block activation (fail closed)."""
        from backend.agent_state import AgentState
        ms = AgentState(mode='execute')
        with mock.patch('plugins.kanban.db.kanban_db') as mock_db, \
             mock.patch('models.db.db') as mock_db2:
            mock_db.get.side_effect = Exception('db unavailable')
            mock_db.get_unmet_dependencies.side_effect = Exception('db unavailable')
            mock_db2.get_setting.return_value = '1'  # autopilot=ON
            result = self._h._state_handler('agent1', 'sess1', ms, 'kanban:activate', {'task_id': 'task-1'})
        assert result['result'] == 'error'
        assert 'dependency check failed' in result['message']

    def test_activate_allowed_when_task_lookup_raises(self):
        """Deps OK but task-validation DB lookup fails → allow activation (fail open)."""
        from backend.agent_state import AgentState
        ms = AgentState(mode='execute')
        with mock.patch('plugins.kanban.db.kanban_db') as mock_db, \
             mock.patch('models.db.db') as mock_db2:
            mock_db.get_unmet_dependencies.return_value = []
            mock_db.get.side_effect = Exception('db unavailable')
            mock_db2.get_setting.return_value = '1'  # autopilot=ON
            result = self._h._state_handler('agent1', 'sess1', ms, 'kanban:activate', {'task_id': 'task-1'})
        assert result['result'] == 'success'

    # ── kanban:finish ─────────────────────────────────────────────────────────

    def test_finish_returns_success_when_done(self):
        self._h._active_tasks['agent1'] = 'task-1'
        result = self._call('kanban:finish', {'task_id': 'task-1'}, task_status='done')
        assert result['result'] == 'success'

    def test_finish_clears_active_and_pending(self):
        self._h._active_tasks['agent1'] = 'task-1'
        self._h._pending_tasks['agent1'] = 'task-1'
        self._call('kanban:finish', {'task_id': 'task-1'}, task_status='done')
        assert 'agent1' not in self._h._active_tasks
        assert 'agent1' not in self._h._pending_tasks

    def test_finish_returns_empty_state_to_trigger_clear(self):
        self._h._active_tasks['agent1'] = 'task-1'
        result = self._call('kanban:finish', {'task_id': 'task-1'}, task_status='done')
        assert result['state'] == ''

    def test_finish_blocked_when_task_not_done(self):
        self._h._active_tasks['agent1'] = 'task-1'
        result = self._call('kanban:finish', {'task_id': 'task-1'}, task_status='in-progress')
        assert result['result'] == 'error'
        assert 'done' in result['message']

    def test_finish_blocked_message_includes_task_id(self):
        result = self._call('kanban:finish', {'task_id': 'my-task'}, task_status='in-progress')
        assert 'my-task' in result['message']

    def test_finish_missing_task_id_returns_error(self):
        result = self._call('kanban:finish', {})
        assert result['result'] == 'error'

    def test_finish_allowed_when_db_raises(self):
        """DB unavailable → allow finish (fail open)."""
        from backend.agent_state import AgentState
        ms = AgentState(mode='execute')
        with mock.patch('plugins.kanban.db.kanban_db') as mock_db, \
             mock.patch.object(self._h, '_load_config', return_value={}):
            mock_db.get.side_effect = Exception('db gone')
            result = self._h._state_handler('agent1', 'sess1', ms, 'kanban:finish', {'task_id': 'task-1'})
        assert result['result'] == 'success'

    # ── unknown label ─────────────────────────────────────────────────────────

    def test_returns_none_for_unrecognised_label(self):
        result = self._call('kanban:unknown_action', {'task_id': 'task-1'})
        assert result is None

    def test_returns_none_for_different_namespace(self):
        result = self._call('deploy:lock', {})
        assert result is None
