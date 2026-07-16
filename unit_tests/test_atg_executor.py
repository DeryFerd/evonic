"""Tests for the ATG executor with stub tool executors and a scripted LLM."""

import json
import threading

from backend.agent_runtime.atg.executor import run_dag_execution
from backend.agent_runtime.atg.graph import TaskDAG, TaskNode
from backend.agent_state import AgentState


class ScriptedLLM:
    def __init__(self, responses=()):
        self.responses = list(responses)
        self.calls = []

    def chat_completion(self, messages, tools=None, temperature=None,
                        enable_thinking=True, max_tokens=None, log_file=None):
        self.calls.append(messages)
        if not self.responses:
            raise RuntimeError("unexpected LLM call")
        return {'success': True,
                'response': {'choices': [{'message': {'content': self.responses.pop(0)}}]}}


def _proceed():
    """Thought-experiment response: no objections (empty verdicts = all proceed)."""
    return '```json\n{}\n```'


TOOLS = [
    {'type': 'function', 'function': {
        'name': 'read_file',
        'parameters': {'type': 'object',
                       'properties': {'path': {'type': 'string'}},
                       'required': ['path']}}},
    {'type': 'function', 'function': {
        'name': 'write_file',
        'parameters': {'type': 'object',
                       'properties': {'file_path': {'type': 'string'},
                                      'content': {'type': 'string'}},
                       'required': ['file_path', 'content']}}},
]


class ToolStub:
    """real_exec stub recording calls, threads and concurrency."""

    def __init__(self, results=None, errors=None):
        self.calls = []          # (tool, args, thread_name)
        self.results = results or {}
        self.errors = errors or set()
        self._lock = threading.Lock()
        self._live = 0
        self.max_concurrency = 0
        self.barrier = None

    def __call__(self, tool, args):
        with self._lock:
            self._live += 1
            self.max_concurrency = max(self.max_concurrency, self._live)
            self.calls.append((tool, dict(args), threading.current_thread().name))
        if self.barrier is not None:
            try:
                self.barrier.wait(timeout=5)
            except threading.BrokenBarrierError:
                pass
        try:
            if tool in self.errors or args.get('path') in self.errors:
                return {'error': 'boom'}
            if tool == 'read_file':
                return {'content': f"data:{args.get('path')}"}
            return {'result': 'success'}
        finally:
            with self._lock:
                self._live -= 1


def _node(nid, tool='read_file', deps=None, outputs=None, args=None, **kw):
    return TaskNode(id=nid, goal=f"goal {nid}", tool=tool, deps=deps or [],
                    outputs=outputs if outputs is not None else ['content'],
                    args_template=args if args is not None else {'path': nid},
                    **kw)


def _run(nodes, llm=None, stub=None, stop_event=None, user_id='u1'):
    dag = TaskDAG(root_goal='test goal')
    for n in nodes:
        dag.add_node(n)
    assert dag.validate() == []
    llm = llm or ScriptedLLM()
    stub = stub or ToolStub()
    ms = AgentState(mode='execute')
    ms.atg = {'status': 'compiled', 'dag': dag.to_dict(),
              'history': {'entries': []}, 'repair_attempts': 0, 'stats': {}}
    agent_context = {
        'id': 'a1', '_db_agent_id': 'a1', 'user_id': user_id, 'channel_id': None,
        'agent_state': ms,
        '_atg_runtime': {'llm': llm, 'llm_lock': threading.Lock(), 'tools': TOOLS},
    }
    outcome = run_dag_execution(
        agent={'id': 'a1', 'name': 'A'}, agent_context=agent_context, ms=ms,
        stop_event=stop_event or threading.Event(),
        builtin_exec=lambda fn, args: None, real_exec=stub,
        chatlog=[], tool_trace=[], timeline=[], session_id='sess-1')
    return outcome, ms, stub, llm


def _statuses(ms):
    return {nid: nd['status'] for nid, nd in ms.atg['dag']['nodes'].items()}


# ── Happy paths ──────────────────────────────────────────────────────────────

def test_chain_with_placeholder_resolution_no_llm():
    nodes = [
        _node('n1', args={'path': 'a.txt'}),
        _node('n2', tool='write_file', deps=['n1'], outputs=['result'],
              args={'file_path': 'b.txt', 'content': 'copy: ${n1.content}'}),
    ]
    outcome, ms, stub, llm = _run(nodes, llm=ScriptedLLM([_proceed()]))
    assert outcome.status == 'done'
    assert not outcome.stopped
    # fully bound — only the thought-experiment call for the mutating wave
    assert len(llm.calls) == 1
    assert stub.calls[0][0] == 'read_file'
    assert stub.calls[1][1]['content'] == 'copy: data:a.txt'
    assert _statuses(ms) == {'n1': 'done', 'n2': 'done'}
    assert 'All graph nodes completed' in outcome.summary_for_llm


def test_parallel_read_only_wave():
    stub = ToolStub()
    stub.barrier = threading.Barrier(3)
    nodes = [_node(f'n{i}', args={'path': f'f{i}'}) for i in range(1, 4)]
    outcome, ms, stub, _ = _run(nodes, stub=stub)
    assert outcome.status == 'done'
    assert stub.max_concurrency == 3
    assert ms.atg['stats']['parallel_peak'] == 3


def test_mutating_nodes_serial_in_id_order():
    nodes = [
        _node('n2', tool='write_file', outputs=['result'],
              args={'file_path': 'b', 'content': 'y'}),
        _node('n1', tool='write_file', outputs=['result'],
              args={'file_path': 'a', 'content': 'x'}),
    ]
    outcome, ms, stub, _ = _run(nodes, llm=ScriptedLLM([_proceed()]))
    assert outcome.status == 'done'
    assert stub.max_concurrency == 1
    assert [c[1]['file_path'] for c in stub.calls] == ['a', 'b']  # id order


def test_composite_node_bound_via_llm():
    bind = '```json\n' + json.dumps(
        {"tool": "read_file", "args": {"path": "picked.txt"}}) + '\n```'
    nodes = [TaskNode(id='n1', goal='inspect the config file', tool=None,
                      outputs=['content'])]
    outcome, ms, stub, llm = _run(nodes, llm=ScriptedLLM([bind]))
    assert outcome.status == 'done'
    assert len(llm.calls) == 1
    assert stub.calls[0][:2] == ('read_file', {'path': 'picked.txt'})
    # localized context: bind prompt never includes conversation history
    assert 'inspect the config file' in llm.calls[0][1]['content']


def test_unresolved_placeholder_falls_back_to_llm_bind():
    bind = '```json\n' + json.dumps(
        {"tool": "write_file",
         "args": {"file_path": "out", "content": "resolved"}}) + '\n```'
    nodes = [
        _node('n1', outputs=['weird_key'], args={'path': 'a'}),
        _node('n2', tool='write_file', deps=['n1'], outputs=['result'],
              args={'file_path': 'out', 'content': '${n1.weird_key}'}),
    ]
    outcome, _, stub, llm = _run(nodes, llm=ScriptedLLM([bind, _proceed()]))
    assert outcome.status == 'done'
    assert len(llm.calls) == 2  # bind + thought experiment
    assert stub.calls[1][1]['content'] == 'resolved'


# ── Failure / stop / approval ────────────────────────────────────────────────

def test_bare_string_error_result_detected_as_failure():
    # read_file returns bare 'Error: ...' strings — must count as node failure
    class StringErrorStub(ToolStub):
        def __call__(self, tool, args):
            super().__call__(tool, args)
            return "Error: File not found."

    nodes = [_node('n1', args={'path': 'a'})]
    outcome, ms, _, _ = _run(nodes, stub=StringErrorStub())
    assert outcome.status == 'fallback'
    assert _statuses(ms)['n1'] == 'failed'
    assert 'File not found' in ms.atg['dag']['nodes']['n1']['record']['error']


def test_node_failure_skips_downstream_and_falls_back():
    stub = ToolStub(errors={'bad'})
    nodes = [
        _node('n1', args={'path': 'bad'}),
        _node('n2', tool='write_file', deps=['n1'], outputs=['result'],
              args={'file_path': 'o', 'content': '${n1.content}'}),
    ]
    outcome, ms, stub, _ = _run(nodes, stub=stub)
    assert outcome.status == 'fallback'
    assert _statuses(ms) == {'n1': 'failed', 'n2': 'skipped'}
    assert 'FAILED' in outcome.summary_for_llm
    assert 'continue the task manually' in outcome.summary_for_llm


def test_stop_event_mid_execution():
    stop = threading.Event()

    class StoppingStub(ToolStub):
        def __call__(self, tool, args):
            stop.set()  # user hits /stop while wave 1 runs
            return super().__call__(tool, args)

    nodes = [
        _node('n1', args={'path': 'a'}),
        _node('n2', deps=['n1'], args={'path': '${n1.content}'}),
    ]
    outcome, ms, stub, _ = _run(nodes, stub=StoppingStub(), stop_event=stop)
    assert outcome.stopped
    assert _statuses(ms)['n2'] == 'skipped'
    assert len(stub.calls) == 1  # n2 never executed


def test_approval_required_auto_rejected_for_api_session():
    class ApprovalStub(ToolStub):
        def __call__(self, tool, args):
            super().__call__(tool, args)
            return {'level': 'requires_approval', 'reasons': ['dangerous']}

    nodes = [_node('n1', tool='write_file', outputs=['result'],
                   args={'file_path': 'x', 'content': 'y'})]
    outcome, ms, _, _ = _run(nodes, stub=ApprovalStub(), user_id='api:client',
                             llm=ScriptedLLM([_proceed()]))
    assert outcome.status == 'fallback'
    assert _statuses(ms)['n1'] == 'failed'
    assert 'rejected' in ms.atg['dag']['nodes']['n1']['record']['error']


def test_state_persisted_in_ms_atg():
    nodes = [_node('n1', args={'path': 'a'})]
    outcome, ms, _, _ = _run(nodes)
    assert ms.atg['status'] == 'done'
    rec = ms.atg['dag']['nodes']['n1']['record']
    assert rec['resolved_args'] == {'path': 'a'}
    assert 'data:a' in rec['output_excerpt']
    assert ms.atg['stats']['waves_executed'] == 1
