"""Tests for ATG re-arm: a new complex task in a finished-ATG session
re-enters the plan/compile cycle; follow-ups never do."""

from unittest.mock import MagicMock, patch

from backend.agent_runtime.atg import maybe_rearm_atg
from backend.agent_state import AgentState

AGENT = {'id': 'a1', 'enable_atg': 1, 'enable_agent_state': 1}
NEW_TASK_TEXT = 'now please build a completely different scraper project in /tmp/scraper'


def _finished_ms(status='done', goal='create todoweb app'):
    ms = AgentState(mode='execute')
    ms.plan_file = 'plan/atg-old.md'
    ms.atg = {'status': status, 'dag': {'root_goal': goal, 'nodes': {}},
              'history': {'entries': []}}
    return ms


def _patch_classifiers(task='complex', continuation='new_task'):
    return patch.multiple('backend.task_classifier',
                          classify_task=MagicMock(return_value=task),
                          classify_continuation=MagicMock(return_value=continuation))


# ── Gates that must block re-arm ─────────────────────────────────────────────

def test_rearm_requires_flag_state_and_text():
    with _patch_classifiers():
        assert not maybe_rearm_atg({}, _finished_ms(), NEW_TASK_TEXT)
        assert not maybe_rearm_atg(AGENT, None, NEW_TASK_TEXT)
        assert not maybe_rearm_atg(AGENT, _finished_ms(), '')


def test_no_rearm_in_plan_mode_or_while_graph_active():
    with _patch_classifiers():
        ms = _finished_ms()
        ms.mode = 'plan'
        assert not maybe_rearm_atg(AGENT, ms, NEW_TASK_TEXT)
        for status in ('compiled', 'executing'):
            assert not maybe_rearm_atg(AGENT, _finished_ms(status), NEW_TASK_TEXT)


def test_no_rearm_without_previous_goal():
    ms = AgentState(mode='execute')
    ms.atg = {'status': 'done', 'dag': {'nodes': {}}}
    with _patch_classifiers():
        assert not maybe_rearm_atg(AGENT, ms, NEW_TASK_TEXT)


def test_no_rearm_for_trivial_message():
    with _patch_classifiers(task='trivial'):
        assert not maybe_rearm_atg(AGENT, _finished_ms(), NEW_TASK_TEXT)


def test_no_rearm_for_continuation():
    with _patch_classifiers(continuation='continuation'):
        ms = _finished_ms()
        assert not maybe_rearm_atg(AGENT, ms, 'the port change broke the build, fix it please')
        assert ms.mode == 'execute'
        assert ms.atg is not None


# ── The re-arm itself ────────────────────────────────────────────────────────

def test_rearm_resets_state_for_new_complex_task():
    ms = _finished_ms()
    with _patch_classifiers():
        assert maybe_rearm_atg(AGENT, ms, NEW_TASK_TEXT)
    assert ms.mode == 'plan'
    assert ms.atg is None
    assert ms.plan_file is None  # execute mode blocked until a fresh compile
    assert ms.auto_trivial is False
    # full cycle re-engaged: save_plan redirect applies again
    from backend.tools.registry import _builtin_save_plan_factory
    _, save_plan = _builtin_save_plan_factory(
        {'id': 'a1', 'enable_atg': True, 'agent_state': ms})
    assert 'compile_task_graph' in save_plan({'filename': 'x.md', 'content': '#'})['error']


def test_rearm_works_after_failed_compilation_state():
    # compile failure stores root_goal at top level (no dag)
    ms = AgentState(mode='execute')
    ms.plan_file = 'plan/manual.md'
    ms.atg = {'status': 'failed', 'error': 'boom', 'root_goal': 'old goal'}
    with _patch_classifiers():
        assert maybe_rearm_atg(AGENT, ms, NEW_TASK_TEXT)
    assert ms.mode == 'plan' and ms.atg is None


def test_continuation_classifier_defaults_safe():
    from backend.task_classifier import classify_continuation
    # short follow-ups never reach the LLM
    assert classify_continuation('goal', 'belum bisa') == 'continuation'
    assert classify_continuation('', NEW_TASK_TEXT) == 'continuation'
    # LLM failure → continuation
    with patch('backend.task_classifier._get_classifier_client') as gc:
        gc.return_value.chat_completion.return_value = {'success': False}
        assert classify_continuation('goal', NEW_TASK_TEXT) == 'continuation'
    # LLM verdicts respected
    for verdict, expected in (('NEW_TASK', 'new_task'), ('CONTINUATION', 'continuation')):
        with patch('backend.task_classifier._get_classifier_client') as gc:
            gc.return_value.chat_completion.return_value = {
                'success': True,
                'response': {'choices': [{'message': {'content': verdict}}]}}
            assert classify_continuation('goal', NEW_TASK_TEXT) == expected
