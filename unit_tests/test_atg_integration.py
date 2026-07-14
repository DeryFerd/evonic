"""End-to-end: run_tool_loop executes a compiled ATG graph, then the plain
loop composes the final answer from the injected summary."""

import threading
from unittest.mock import MagicMock, patch

from backend.agent_runtime.atg.graph import TaskDAG, TaskNode
from backend.agent_state import AgentState


def _compiled_atg():
    dag = TaskDAG(root_goal="summarize a and b into out")
    dag.add_node(TaskNode(id='n1', goal='read a', tool='read_file',
                          args_template={'path': 'a.txt'}, outputs=['content']))
    dag.add_node(TaskNode(id='n2', goal='read b', tool='read_file',
                          args_template={'path': 'b.txt'}, outputs=['content']))
    dag.add_node(TaskNode(id='n3', goal='write summary', tool='write_file',
                          args_template={'file_path': 'out.md',
                                         'content': '${n1.content}\n${n2.content}'},
                          outputs=['result'], deps=['n1', 'n2']))
    assert dag.validate() == []
    return {'status': 'compiled', 'dag': dag.to_dict(),
            'history': {'entries': []}, 'repair_attempts': 0, 'stats': {}}


def _final(content):
    return {'success': True, 'duration_ms': 10,
            'response': {'choices': [{'message': {'content': content,
                                                  'tool_calls': None},
                                      'finish_reason': 'stop'}]}}


def test_run_tool_loop_drives_atg_branch():
    from backend.agent_runtime import llm_loop as _loop

    ms = AgentState(mode='execute')
    ms.plan_file = None
    ms.atg = _compiled_atg()

    executed = []

    def real_exec(fn_name, args):
        executed.append((fn_name, dict(args)))
        if fn_name == 'read_file':
            return {'content': f"text-of-{args['path']}"}
        return {'result': 'success'}

    llm = MagicMock()
    llm.chat_completion.side_effect = [
        _final('```json\n{}\n```'),  # thought experiment for the write wave
        _final('Done: files merged into out.md'),
    ]

    mock_db = MagicMock()
    mock_db.get_setting.side_effect = lambda key, default=None: default or '0'
    mock_db.get_agent_default_model.return_value = None
    mock_db.get_agent_model.return_value = None
    mock_db.get_agent_state.return_value = None
    mock_db.get_session_state.return_value = None
    mock_tr = MagicMock()
    mock_tr.get_builtin_executor.return_value = lambda n, a: None
    mock_tr.get_real_executor.return_value = real_exec

    agent = {'id': 'atg_agent', 'name': 'T', 'model': None,
             'send_intermediate_responses': False, 'summarize_threshold': 0,
             'builtin_tools_enabled': True, 'enable_atg': 1,
             'enable_agent_state': 1}
    agent_context = {'id': 'atg_agent', '_db_agent_id': 'atg_agent',
                     'user_id': 'u1', 'channel_id': None, 'is_super': False,
                     'agent_state': ms, 'enable_atg': True}

    with patch.object(_loop, 'db', mock_db), \
         patch.object(_loop, 'tool_registry', mock_tr), \
         patch.object(_loop, 'LLMClient', return_value=llm), \
         patch.object(_loop, 'llm_client', llm):
        result, tool_trace, timeline = _loop.run_tool_loop(
            agent=agent,
            agent_context=agent_context,
            messages=[{"role": "system", "content": "sys"},
                      {"role": "user", "content": "do the approved plan"}],
            tools=[],
            session_id='sess-atg-1',
            llm_lock=threading.Lock(),
            stop_event=threading.Event(),
            session_skill_mds={},
            session_skill_tools={},
            llm_log_path=None,
        )

    # All three nodes executed; write saw the resolved placeholder contents
    assert ('read_file', {'path': 'a.txt'}) in executed
    assert ('read_file', {'path': 'b.txt'}) in executed
    write_calls = [args for fn, args in executed if fn == 'write_file']
    assert write_calls and 'text-of-a.txt' in write_calls[0]['content']
    assert 'text-of-b.txt' in write_calls[0]['content']

    # DAG state completed and the plain loop composed the final answer
    assert ms.atg['status'] == 'done'
    assert {nd['status'] for nd in ms.atg['dag']['nodes'].values()} == {'done'}
    assert 'files merged' in str(result)

    # Two LLM calls: thought experiment + the final answer — no tool rounds
    assert llm.chat_completion.call_count == 2
    msgs = llm.chat_completion.call_args[1]['messages']
    summary_msgs = [m for m in msgs
                    if m['role'] == 'user' and '[SYSTEM] [ATG]' in str(m.get('content'))]
    assert summary_msgs, "ATG summary must be injected before the final LLM call"
    # strict chat templates reject non-leading system messages; leading ones
    # (position 0..n before the first non-system) are merged by llm_client.
    first_non_system = next(i for i, m in enumerate(msgs) if m['role'] != 'system')
    assert all(m['role'] != 'system' for m in msgs[first_non_system:])

    # tool_trace/timeline populated like Phase 3 would
    assert len(tool_trace) == 3
    assert any(t.get('type') == 'tool_call' for t in timeline)

    # per-run stats recorded for A/B evaluation
    stats = [t for t in timeline if t.get('type') == 'atg_stats']
    assert stats and stats[0]['status'] == 'done'
    assert stats[0]['waves_executed'] == 2
    assert stats[0]['parallel_peak'] == 2
    assert stats[0]['thought_checks'] == 1


def test_run_tool_loop_ignores_atg_when_flag_off():
    from backend.agent_runtime import llm_loop as _loop

    ms = AgentState(mode='execute')
    ms.atg = _compiled_atg()  # present but agent not flagged

    llm = MagicMock()
    llm.chat_completion.side_effect = [_final('plain answer')]
    mock_db = MagicMock()
    mock_db.get_setting.side_effect = lambda key, default=None: default or '0'
    mock_db.get_agent_default_model.return_value = None
    mock_db.get_agent_model.return_value = None
    mock_db.get_agent_state.return_value = None
    mock_db.get_session_state.return_value = None
    mock_tr = MagicMock()
    mock_tr.get_builtin_executor.return_value = lambda n, a: None
    real_exec = MagicMock(return_value=None)
    mock_tr.get_real_executor.return_value = real_exec

    agent = {'id': 'a2', 'name': 'T', 'model': None,
             'send_intermediate_responses': False, 'summarize_threshold': 0}
    agent_context = {'id': 'a2', 'user_id': 'u1', 'channel_id': None,
                     'is_super': False, 'agent_state': ms}

    with patch.object(_loop, 'db', mock_db), \
         patch.object(_loop, 'tool_registry', mock_tr), \
         patch.object(_loop, 'LLMClient', return_value=llm), \
         patch.object(_loop, 'llm_client', llm):
        result, _, _ = _loop.run_tool_loop(
            agent=agent, agent_context=agent_context,
            messages=[{"role": "system", "content": "sys"},
                      {"role": "user", "content": "hi"}],
            tools=[], session_id='sess-atg-2',
            llm_lock=threading.Lock(), stop_event=threading.Event(),
            session_skill_mds={}, session_skill_tools={}, llm_log_path=None)

    assert 'plain answer' in str(result)
    assert ms.atg['status'] == 'compiled'  # untouched
    real_exec.assert_not_called()
    # no ATG summary injected
    for m in llm.chat_completion.call_args[1]['messages']:
        assert '[ATG]' not in str(m.get('content'))
