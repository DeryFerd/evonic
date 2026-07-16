"""Tests for the CMP path store (pure dict ops, no I/O/LLM)."""

import json

import pytest

from backend.agent_runtime.cmp import store
from backend.agent_state import AgentState


def _ms(mode='execute', plan_file='plan/p1.md', atg=None):
    ms = AgentState(mode=mode)
    ms.plan_file = plan_file
    ms.atg = atg
    return ms


def _init(ms=None, ts=1000):
    ms = ms or _ms()
    ms.cmp = store.new_cmp(ms, title='first task', goal='do the first thing',
                           now_ts=ts)
    return ms


# ── Init / create ────────────────────────────────────────────────────────────

def test_new_cmp_first_path():
    ms = _init(ts=1000)
    cmp = ms.cmp
    assert cmp['active_id'] == 'A1'
    p1 = cmp['paths']['A1']
    assert p1['status'] == 'active'
    assert p1['segments'] == [[999, None]]
    assert p1['title'] == 'first task'


def test_create_path_branches_and_resets_task_state():
    atg = {'status': 'done', 'dag': {'root_goal': 'g'}}
    ms = _init(_ms(mode='execute', plan_file='plan/old.md', atg=atg), ts=1000)
    record = store.create_path(ms.cmp, ms, 'invoice task', goal='make invoice',
                               depends_on=['A1'], now_ts=2000)

    assert record['id'] == 'B1'  # dependency child → next level letter
    assert ms.cmp['active_id'] == 'B1'
    assert record['depends_on'] == ['A1']
    assert record['segments'] == [[1999, None]]

    # old path suspended: segment closed at turn boundary, state snapshotted
    p1 = ms.cmp['paths']['A1']
    assert p1['status'] == 'dormant'
    assert p1['segments'] == [[999, 1999]]
    assert p1['mode'] == 'execute'
    assert p1['plan_file'] == 'plan/old.md'
    assert p1['atg'] == atg

    # ms reset to a fresh plan cycle (the re-arm mutations)
    assert ms.mode == 'plan'
    assert ms.plan_file is None
    assert ms.atg is None
    assert ms.auto_trivial is False


def test_create_path_unknown_dependency_raises():
    ms = _init()
    with pytest.raises(ValueError):
        store.create_path(ms.cmp, ms, 'x', depends_on=['Z9'])


# ── Switch / snapshot round-trip ─────────────────────────────────────────────

def test_switch_restores_task_state_round_trip():
    atg1 = {'status': 'executing', 'dag': {'root_goal': 'task one',
                                           'nodes': {'n1': {'status': 'done'}}}}
    ms = _init(_ms(mode='execute', plan_file='plan/one.md', atg=atg1), ts=1000)
    store.create_path(ms.cmp, ms, 'task two', now_ts=2000)
    ms.mode = 'execute'
    ms.plan_file = 'plan/two.md'
    ms.atg = {'status': 'compiled', 'dag': {'root_goal': 'task two'}}

    target = store.switch_to(ms.cmp, ms, 'A1', now_ts=3000)

    # P1's full state restored verbatim — fixes the single-slot ms.atg
    assert ms.mode == 'execute'
    assert ms.plan_file == 'plan/one.md'
    assert ms.atg == atg1
    # stored snapshot cleared while live (no double truth)
    assert target['atg'] is None and target['plan_file'] is None
    # A2 snapshotted
    p2 = ms.cmp['paths']['A2']
    assert p2['status'] == 'dormant'
    assert p2['plan_file'] == 'plan/two.md'
    assert p2['atg']['status'] == 'compiled'
    # segments: P1 reopened, P2 closed
    assert ms.cmp['paths']['A1']['segments'] == [[999, 1999], [2999, None]]
    assert p2['segments'] == [[1999, 2999]]
    assert ms.cmp['active_id'] == 'A1'


def test_switch_to_unknown_or_active_raises():
    ms = _init()
    with pytest.raises(ValueError):
        store.switch_to(ms.cmp, ms, 'Z9')
    with pytest.raises(ValueError):
        store.switch_to(ms.cmp, ms, 'A1')  # already active
    # error message lists valid targets
    store.create_path(ms.cmp, ms, 'second')
    try:
        store.switch_to(ms.cmp, ms, 'Z9')
    except ValueError as e:
        assert 'A1' in str(e)


# ── Hysteresis / caps ────────────────────────────────────────────────────────

def test_hysteresis_archives_after_k_turns():
    ms = _init(ts=1000)
    store.create_path(ms.cmp, ms, 'second', now_ts=2000)
    p1 = ms.cmp['paths']['A1']
    archived = []
    for _ in range(store.CMP_DORMANT_TURNS_K):
        archived = store.tick_hysteresis(ms.cmp)
    assert p1['status'] == 'archived'
    assert archived == ['A1']
    # archived paths drop their atg snapshot
    assert p1['atg'] is None


def test_return_resets_dormant_counter():
    ms = _init(ts=1000)
    store.create_path(ms.cmp, ms, 'second', now_ts=2000)
    store.tick_hysteresis(ms.cmp)
    assert ms.cmp['paths']['A1']['dormant_turns'] == 1
    store.switch_to(ms.cmp, ms, 'A1', now_ts=3000)
    assert ms.cmp['paths']['A1']['dormant_turns'] == 0
    assert ms.cmp['paths']['A1']['status'] == 'active'


def test_caps_prune_oldest_archived_to_stubs():
    ms = _init(ts=1000)
    for i in range(store.MAX_PATHS + 3):
        store.create_path(ms.cmp, ms, f'task {i}', now_ts=2000 + i)
        for path in ms.cmp['paths'].values():
            if path['status'] == 'dormant':
                path['status'] = 'archived'
    assert len(ms.cmp['paths']) == store.MAX_PATHS + 4  # before enforcement pass
    store.enforce_caps(ms.cmp)
    assert len(ms.cmp['paths']) == store.MAX_PATHS + 4  # stubs replace, not delete
    # earliest archived became a stub (map node + segments survive)
    p1 = ms.cmp['paths']['A1']
    assert p1['goal'] == '' and p1['atg'] is None
    assert p1['segments']  # transcript ref survives


def test_card_field_caps():
    card = store.clamp_card_fields({
        'title': 'x' * 100, 'goal': 'g' * 500, 'outcome': 'o' * 500,
        'key_facts': [f'f{i}' * 200 for i in range(10)],
        'artifacts': [f'a{i}' for i in range(20)],
    })
    assert len(card['title']) == store.TITLE_MAX
    assert len(card['goal']) == store.GOAL_MAX
    assert len(card['key_facts']) == store.KEY_FACTS_MAX
    assert all(len(f) <= store.KEY_FACT_CHARS for f in card['key_facts'])
    assert len(card['artifacts']) == store.ARTIFACTS_MAX


# ── Serialization ────────────────────────────────────────────────────────────

def test_cmp_survives_agent_state_round_trip():
    ms = _init(_ms(atg={'status': 'done'}), ts=1000)
    store.create_path(ms.cmp, ms, 'second', now_ts=2000)
    restored = AgentState.deserialize(ms.serialize())
    assert restored.cmp == json.loads(json.dumps(ms.cmp))
    assert restored.cmp['active_id'] == 'A2'


def test_dependency_ancestors_bounded():
    ms = _init(ts=1000)
    store.create_path(ms.cmp, ms, 'b', depends_on=['A1'], now_ts=2000)
    store.create_path(ms.cmp, ms, 'c', depends_on=['B1'], now_ts=3000)
    assert store.dependency_ancestors(ms.cmp, 'C1') == ['B1', 'A1']
    assert store.dependency_ancestors(ms.cmp, 'C1', max_depth=1) == ['B1']
    assert store.dependency_ancestors(ms.cmp, 'A1') == []
