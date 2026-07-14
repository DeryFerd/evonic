"""Tests for the CMP per-turn orchestrator (on_turn_boundary) and its
coexistence with the ATG re-arm."""

from unittest.mock import MagicMock, patch

from backend.agent_runtime.cmp import on_turn_boundary, store
from backend.agent_state import AgentState

AGENT = {'id': 'a1', 'enable_cmp': 1, 'enable_agent_state': 1}
LONG_NEW_TASK = ('please build a completely new scraper project under /tmp/scraper '
                 'that collects product prices daily')


class FakeChatlog:
    def __init__(self, user_ts=5000):
        self.user_ts = user_ts

    def get_last_entry(self, types=None):
        return {'type': 'user', 'ts': self.user_ts}

    def get_entries_between_ts(self, a, b):
        return []

    def get_entries_after_ts(self, a):
        return []


def _detect(decision, target=None, layer='L3'):
    return patch('backend.agent_runtime.cmp.detector.detect',
                 return_value={'decision': decision, 'target': target,
                               'layer': layer})


# ── Gates / init ─────────────────────────────────────────────────────────────

def test_none_when_not_enabled():
    assert on_turn_boundary({}, AgentState(), FakeChatlog(), 'x') is None
    assert on_turn_boundary({'enable_cmp': 1}, AgentState(), FakeChatlog(), 'x') is None
    assert on_turn_boundary({'enable_cmp': 1, 'enable_agent_state': 1,
                             'is_subagent': True}, AgentState(),
                            FakeChatlog(), 'x') is None


def test_first_turn_initializes_p1_from_current_work():
    ms = AgentState(mode='execute')
    ms.atg = {'status': 'executing', 'dag': {'root_goal': 'build the website'}}
    result = on_turn_boundary(AGENT, ms, FakeChatlog(user_ts=7000), 'lanjutkan')
    assert result['decision'] == 'init'
    p1 = ms.cmp['paths']['A1']
    assert p1['title'] == 'build the website'
    assert p1['segments'] == [[6999, None]]
    # init adopts, never disturbs, the live task state
    assert ms.atg['status'] == 'executing'


# ── Decisions drive path ops ─────────────────────────────────────────────────

def _session_with_two_paths():
    ms = AgentState(mode='execute')
    ms.cmp = store.new_cmp(ms, title='website', now_ts=1000)
    store.create_path(ms.cmp, ms, 'server config', now_ts=2000)
    ms.mode = 'execute'
    return ms


def test_continue_only_ticks_hysteresis():
    ms = _session_with_two_paths()
    with _detect('continue'):
        result = on_turn_boundary(AGENT, ms, FakeChatlog(), 'lanjut saja')
    assert result['decision'] == 'continue'
    assert ms.cmp['active_id'] == 'A2'
    assert ms.cmp['paths']['A1']['dormant_turns'] == 1


def test_return_switches_and_finalizes_card():
    ms = _session_with_two_paths()
    ms.plan_file = 'plan/server.md'
    with _detect('return', 'A1'), \
         patch('backend.agent_runtime.cmp.compactor.generate_card',
               return_value={'title': 'website', 'goal': 'g', 'outcome': 'o',
                             'key_facts': [], 'artifacts': []}) as gen:
        result = on_turn_boundary(AGENT, ms, FakeChatlog(user_ts=9000),
                                  'balik ke website yang tadi ya')
    assert result['decision'] == 'return'
    assert ms.cmp['active_id'] == 'A1'
    gen.assert_called_once()  # outgoing card finalized before the switch
    # P2 snapshotted with its plan file
    assert ms.cmp['paths']['A2']['plan_file'] == 'plan/server.md'


def test_dep_branch_creates_dependent_plan_mode_path():
    ms = _session_with_two_paths()
    ms.atg = {'status': 'done'}
    with _detect('dep_branch', 'A1'):
        result = on_turn_boundary(AGENT, ms, FakeChatlog(user_ts=9000),
                                  'sekarang buatkan invoice untuk client A dari project itu')
    assert result['decision'] == 'dep_branch'
    new_id = result['target']
    assert ms.cmp['paths'][new_id]['depends_on'] == ['A1']
    # fresh plan cycle (this IS the re-arm)
    assert ms.mode == 'plan' and ms.atg is None and ms.plan_file is None


def test_indep_branch_creates_independent_path():
    ms = _session_with_two_paths()
    with _detect('indep_branch'):
        result = on_turn_boundary(AGENT, ms, FakeChatlog(), LONG_NEW_TASK)
    assert ms.cmp['paths'][result['target']]['depends_on'] == []


def test_path_op_failure_degrades_to_continue():
    ms = _session_with_two_paths()
    with _detect('return', 'Z9'):  # invalid target slipped through
        result = on_turn_boundary(AGENT, ms, FakeChatlog(), 'x' * 50)
    assert result['decision'] == 'continue'
    assert ms.cmp['active_id'] == 'A2'  # untouched


def test_hysteresis_archives_at_k():
    ms = _session_with_two_paths()
    with _detect('continue'):
        for _ in range(store.CMP_DORMANT_TURNS_K):
            on_turn_boundary(AGENT, ms, FakeChatlog(), 'continue this work please')
    assert ms.cmp['paths']['A1']['status'] == 'archived'


# ── Coexistence with ATG-only re-arm (runtime routing) ───────────────────────

def test_atg_only_agents_still_use_rearm():
    # the runtime gate: cmp handled → skip re-arm; cmp off → re-arm runs.
    # Verified structurally: on_turn_boundary returns None for non-cmp agents,
    # so _cmp_handled stays False and the ATG block executes.
    ms = AgentState(mode='execute')
    ms.atg = {'status': 'done', 'dag': {'root_goal': 'g'}}
    assert on_turn_boundary({'id': 'a1', 'enable_atg': 1}, ms,
                            FakeChatlog(), LONG_NEW_TASK) is None
