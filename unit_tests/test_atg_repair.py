"""Tests for ATG repair (region computation, collapse, repair splice) and the
thought-experiment verdicts, using stub executors and a scripted LLM."""

import json
import threading

import pytest

from backend.agent_runtime.atg.executor import run_dag_execution
from backend.agent_runtime.atg.graph import (
    MAX_REPAIR_ATTEMPTS,
    RefinementHistory,
    TaskDAG,
    TaskNode,
)
from backend.agent_runtime.atg.repair import (
    RepairError,
    collapse_region,
    compute_repair_region,
)
from backend.agent_state import AgentState

from unit_tests.test_atg_executor import (
    TOOLS,
    ScriptedLLM,
    ToolStub,
    _node,
    _proceed,
)


def _json_block(nodes):
    return "```json\n" + json.dumps({"nodes": nodes}) + "\n```"


# ── compute_repair_region (pure logic) ───────────────────────────────────────

def _refined_dag_and_history():
    """Coarse (n1, n2, n3) with n2 refined into n2.1 -> n2.2."""
    history = RefinementHistory()
    coarse = TaskDAG(root_goal='g')
    coarse.add_node(_node('n1'))
    coarse.add_node(TaskNode(id='n2', goal='composite', outputs=['content'], deps=['n1']))
    coarse.add_node(_node('n3', deps=['n2'], args={'path': '${n2.content}'}))
    history.record(None, coarse)

    dag = TaskDAG(root_goal='g')
    dag.add_node(_node('n1'))
    dag.add_node(_node('n2.1', deps=['n1'], parent_id='n2', depth=1,
                       args={'path': 'a'}))
    dag.add_node(_node('n2.2', deps=['n2.1'], parent_id='n2', depth=1,
                       args={'path': '${n2.1.content}'}))
    dag.add_node(_node('n3', deps=['n2.2'], args={'path': '${n2.2.content}'}))
    history.record('n2', dag)
    return dag, history


def test_region_for_unrefined_node_is_node_itself():
    dag, history = _refined_dag_and_history()
    region = compute_repair_region(dag, history, 'n1')
    assert region == {'ancestor': 'n1', 'replace': ['n1'], 'frozen': []}


def test_region_traces_refinement_lineage_and_freezes_done():
    dag, history = _refined_dag_and_history()
    dag.nodes['n2.1'].status = 'done'
    dag.nodes['n2.2'].status = 'failed'
    region = compute_repair_region(dag, history, 'n2.2')
    # LCA of n2.2 and its adjacent (n2.1, n3): n2.2's chain is [n2.2, n2],
    # n2.1's chain contains n2 → ancestor n2; region = n2's descendants
    assert region['ancestor'] == 'n2'
    assert region['frozen'] == ['n2.1']
    assert region['replace'] == ['n2.2']


# ── collapse_region (pure logic) ─────────────────────────────────────────────

def test_collapse_rewires_external_consumers():
    dag, _ = _refined_dag_and_history()
    composite = collapse_region(dag, ['n2.1', 'n2.2'], 'n2#r1', 'redo n2')
    assert composite.outputs == ['content']
    assert composite.deps == ['n1']
    assert 'n2.1' not in dag.nodes and 'n2.2' not in dag.nodes
    n3 = dag.nodes['n3']
    assert n3.deps == ['n2#r1']
    assert n3.args_template['path'] == '${n2#r1.content}'
    assert dag.validate() == []


def test_collapse_ambiguous_external_key_raises():
    dag = TaskDAG(root_goal='g')
    dag.add_node(_node('a1', args={'path': 'x'}))
    dag.add_node(_node('a2', args={'path': 'y'}))
    dag.add_node(_node('c', deps=['a1', 'a2'],
                       args={'path': '${a1.content} ${a2.content}'}))
    with pytest.raises(RepairError):
        collapse_region(dag, ['a1', 'a2'], 'r', 'redo')


# ── End-to-end repair through the executor ───────────────────────────────────

def _run_exec(dag, llm, stub, repair_attempts=0):
    ms = AgentState(mode='execute')
    ms.atg = {'status': 'compiled', 'dag': dag.to_dict(),
              'history': {'entries': []}, 'repair_attempts': repair_attempts,
              'stats': {}}
    # seed a coarse history entry so lineage tracing has a snapshot
    history = RefinementHistory()
    history.record(None, dag)
    ms.atg['history'] = history.to_dict()
    agent_context = {
        'id': 'a1', '_db_agent_id': 'a1', 'user_id': 'u1', 'channel_id': None,
        'agent_state': ms,
        '_atg_runtime': {'llm': llm, 'llm_lock': threading.Lock(), 'tools': TOOLS},
    }
    outcome = run_dag_execution(
        agent={'id': 'a1', 'name': 'A'}, agent_context=agent_context, ms=ms,
        stop_event=threading.Event(), builtin_exec=lambda fn, a: None,
        real_exec=stub, chatlog=[], tool_trace=[], timeline=[],
        session_id='sess-r')
    return outcome, ms


def test_failed_node_repaired_on_first_attempt():
    dag = TaskDAG(root_goal='read then write')
    dag.add_node(_node('n1', args={'path': 'bad'}))       # will fail
    dag.add_node(_node('n2', tool='write_file', deps=['n1'], outputs=['result'],
                       args={'file_path': 'o', 'content': '${n1.content}'}))

    repaired = _json_block([
        {"id": "1", "goal": "read the correct file", "tool": "read_file",
         "args_template": {"path": "good"}, "outputs": ["content"], "deps": []},
    ])
    stub = ToolStub(errors={'bad'})
    llm = ScriptedLLM([repaired, _proceed()])
    outcome, ms = _run_exec(dag, llm, stub)

    assert outcome.status == 'done'
    assert ms.atg['repair_attempts'] == 1
    assert ms.atg['stats']['repairs'] == 1
    # the repaired node replaced n1 and fed n2
    write_args = [c[1] for c in stub.calls if c[0] == 'write_file'][0]
    assert write_args['content'] == 'data:good'
    node_ids = set(ms.atg['dag']['nodes'])
    assert 'n1' not in node_ids
    assert any(nid.startswith('n1#r1.') for nid in node_ids)
    # history gained a repair snapshot
    assert ms.atg['history']['entries'][-1]['refined_node_id'] == 'n1'


def test_repair_exhaustion_falls_back():
    dag = TaskDAG(root_goal='g')
    dag.add_node(_node('n1', args={'path': 'bad'}))
    stub = ToolStub(errors={'bad', 'good'})
    # both the original and the repaired node fail; second repair budget is
    # exhausted after MAX_REPAIR_ATTEMPTS
    repaired = _json_block([
        {"id": "1", "goal": "retry read", "tool": "read_file",
         "args_template": {"path": "good"}, "outputs": ["content"], "deps": []},
    ])
    llm = ScriptedLLM([repaired] * MAX_REPAIR_ATTEMPTS)
    outcome, ms = _run_exec(dag, llm, stub)
    assert outcome.status == 'fallback'
    assert ms.atg['repair_attempts'] == MAX_REPAIR_ATTEMPTS
    assert 'continue the task manually' in outcome.summary_for_llm


def test_repair_llm_garbage_falls_back_immediately():
    dag = TaskDAG(root_goal='g')
    dag.add_node(_node('n1', args={'path': 'bad'}))
    stub = ToolStub(errors={'bad'})
    llm = ScriptedLLM(['garbage'] * 3)  # repair retries exhausted
    outcome, ms = _run_exec(dag, llm, stub)
    assert outcome.status == 'fallback'
    assert ms.atg['repair_attempts'] == 0  # only counted on success


# ── Thought experiment verdicts ──────────────────────────────────────────────

def test_thought_abort_marks_node_failed_and_triggers_repair_path():
    dag = TaskDAG(root_goal='g')
    dag.add_node(_node('n1', tool='write_file', outputs=['result'],
                       args={'file_path': '/etc/passwd', 'content': 'x'}))
    abort = '```json\n' + json.dumps(
        {"n1": {"verdict": "abort", "reason": "would overwrite a system file"}}) + '\n```'
    # after abort → repair attempt gets garbage → fallback
    llm = ScriptedLLM([abort, 'garbage', 'garbage', 'garbage'])
    stub = ToolStub()
    outcome, ms = _run_exec(dag, llm, stub)
    assert outcome.status == 'fallback'
    assert stub.calls == []  # never executed
    rec = ms.atg['dag']['nodes']['n1']['record']
    assert 'aborted by pre-execution check' in rec['error']
    assert 'system file' in rec['error']


def test_thought_revise_rebinds_arguments():
    dag = TaskDAG(root_goal='g')
    dag.add_node(_node('n1', tool='write_file', outputs=['result'],
                       args={'file_path': 'out.txt', 'content': 'wrong'}))
    revise = '```json\n' + json.dumps(
        {"n1": {"verdict": "revise", "reason": "content should be 'right'"}}) + '\n```'
    rebind = '```json\n' + json.dumps(
        {"tool": "write_file",
         "args": {"file_path": "out.txt", "content": "right"}}) + '\n```'
    llm = ScriptedLLM([revise, rebind])
    stub = ToolStub()
    outcome, ms = _run_exec(dag, llm, stub)
    assert outcome.status == 'done'
    assert stub.calls[0][1]['content'] == 'right'
    # reviewer feedback was passed into the re-bind prompt
    assert "content should be 'right'" in llm.calls[1][1]['content']


def test_thought_malformed_response_proceeds():
    dag = TaskDAG(root_goal='g')
    dag.add_node(_node('n1', tool='write_file', outputs=['result'],
                       args={'file_path': 'o', 'content': 'x'}))
    llm = ScriptedLLM(['not json'])
    outcome, _ = _run_exec(dag, llm, ToolStub())
    assert outcome.status == 'done'  # simulator failure never blocks execution
