"""Regression guards: CMP additions must not change behavior when unused."""

import json

from backend.agent_state import AgentState


def test_default_state_has_no_cmp():
    assert AgentState().cmp is None


def test_old_format_deserializes_without_cmp_key():
    old = json.dumps({"mode": "execute", "tasks": [], "next_task_id": 1,
                      "plan_file": None, "states": {}, "focus": False,
                      "focus_reason": None, "auto_trivial": False,
                      "atg": None})
    ms = AgentState.deserialize(old)
    assert ms.cmp is None


def test_render_without_cmp_flag_unchanged():
    ms = AgentState()
    ms.cmp = {"active_id": "P1", "paths": {"P1": {"id": "P1", "title": "t",
                                                  "status": "active"}}}
    # cmp present but flag off → no CMP section (unflagged agents unaffected
    # even if state carries a leftover cmp dict)
    assert "Session Paths" not in ms.render()
    assert "Session Paths" in ms.render(cmp_enabled=True)


def test_render_with_flag_but_no_cmp_unchanged():
    assert "Session Paths" not in AgentState().render(cmp_enabled=True)


def test_persist_split_carries_cmp():
    from backend.agent_runtime.llm_loop import _persist_agent_state_split
    from models.db import db

    db.create_agent({'id': 'cmp_test_agent', 'name': 'C', 'system_prompt': ''})
    ms = AgentState()
    ms.cmp = {"version": 1, "active_id": "P1", "next_id": 2, "paths": {},
              "stats": {}}
    _persist_agent_state_split(ms, 'cmp_test_agent', 'sess-c1')
    data = json.loads(db.get_session_state('sess-c1', agent_id='cmp_test_agent'))
    assert data['cmp'] == ms.cmp


def test_chat_state_api_exposes_cmp_map():
    import json as _json
    from app import app
    from models.db import db
    from backend.agent_runtime.cmp import store

    db.create_agent({'id': 'cmp_api_agent', 'name': 'C', 'system_prompt': ''})
    ms = AgentState(mode='execute')
    ms.cmp = store.new_cmp(ms, title='website task', goal='build it', now_ts=1000)
    store.create_path(ms.cmp, ms, 'invoice task', depends_on=['P1'], now_ts=2000)
    from backend.agent_runtime.llm_loop import _persist_agent_state_split
    _persist_agent_state_split(ms, 'cmp_api_agent', 'sess-api-1')

    with app.test_client() as client:
        with client.session_transaction() as sess:
            sess['authenticated'] = True
        res = client.get('/api/agents/cmp_api_agent/chat/state?session_id=sess-api-1')
        assert res.status_code == 200
        data = res.get_json()
    cmp = data.get('cmp')
    assert cmp and cmp['active_id'] == 'P2'
    assert 'flowchart TD' in cmp['mermaid']
    ids = [p['id'] for p in cmp['paths']]
    assert ids == ['P1', 'P2']
    assert cmp['paths'][1]['depends_on'] == ['P1']
    # cards carry what the modal needs, nothing sensitive extra
    assert set(cmp['paths'][0]) == {'id', 'title', 'status', 'goal', 'outcome',
                                    'key_facts', 'artifacts', 'depends_on',
                                    'last_active'}


def test_chat_state_api_no_cmp_key_when_absent():
    from app import app
    from models.db import db

    db.create_agent({'id': 'cmp_api_agent2', 'name': 'C', 'system_prompt': ''})
    from backend.agent_runtime.llm_loop import _persist_agent_state_split
    _persist_agent_state_split(AgentState(), 'cmp_api_agent2', 'sess-api-2')

    with app.test_client() as client:
        with client.session_transaction() as sess:
            sess['authenticated'] = True
        res = client.get('/api/agents/cmp_api_agent2/chat/state?session_id=sess-api-2')
        assert res.status_code == 200
        assert 'cmp' not in res.get_json()


def test_clear_resets_cmp_key():
    # /clear writes a full-replace session_data including explicit cmp None
    import inspect
    from backend import slash_commands
    src = inspect.getsource(slash_commands)
    assert "'cmp': None" in src
