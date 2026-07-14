"""Tests for the ATG compiler with a scripted (mocked) LLM."""

import json
import threading

import pytest

from backend.agent_runtime.atg.compiler import (
    CompilationError,
    _extract_json,
    compile_task_graph,
    render_markdown,
)
from backend.agent_runtime.atg.graph import MAX_COMPILE_LLM_CALLS


class ScriptedLLM:
    """Returns canned responses in order; records the prompts it saw."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def chat_completion(self, messages, tools=None, temperature=None,
                        enable_thinking=True, max_tokens=None, log_file=None):
        self.calls.append(messages)
        if not self.responses:
            raise AssertionError("ScriptedLLM ran out of responses")
        return {'success': True,
                'response': {'choices': [{'message': {'content': self.responses.pop(0)}}]}}


def _json_block(nodes):
    return "```json\n" + json.dumps({"nodes": nodes}) + "\n```"


LOCK = threading.Lock()

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


def _compile(responses, goal="read a and write b"):
    llm = ScriptedLLM(responses)
    dag, history = compile_task_graph(goal, TOOLS, llm, LOCK)
    return dag, history, llm


# ── Coarse pass ──────────────────────────────────────────────────────────────

def test_happy_path_atomic_graph():
    coarse = _json_block([
        {"id": "n1", "goal": "read a", "tool": "read_file",
         "args_template": {"path": "a.txt"}, "outputs": ["content"], "deps": []},
        {"id": "n2", "goal": "write b", "tool": "write_file",
         "args_template": {"file_path": "b.txt", "content": "${n1.content}"},
         "outputs": ["result"], "deps": ["n1"]},
    ])
    dag, history, llm = _compile([coarse])
    assert len(llm.calls) == 1
    assert sorted(dag.nodes) == ['n1', 'n2']
    assert dag.waves() == [['n1'], ['n2']]
    assert len(history.entries) == 1
    assert history.entries[0]['refined_node_id'] is None


def test_malformed_json_retried_with_feedback():
    good = _json_block([{"id": "n1", "goal": "read", "tool": "read_file",
                         "args_template": {"path": "a"}, "outputs": ["content"],
                         "deps": []}])
    dag, _, llm = _compile(["not json at all", good])
    assert len(llm.calls) == 2
    # error feedback appended to the retry prompt
    assert 'previous output was invalid' in llm.calls[1][1]['content']
    assert list(dag.nodes) == ['n1']


def test_invalid_graph_retried_then_fails():
    cyclic = _json_block([
        {"id": "n1", "goal": "a", "tool": "read_file", "outputs": [], "deps": ["n2"]},
        {"id": "n2", "goal": "b", "tool": "read_file", "outputs": [], "deps": ["n1"]},
    ])
    with pytest.raises(CompilationError):
        _compile([cyclic, cyclic, cyclic])


# ── Refinement ───────────────────────────────────────────────────────────────

def _coarse_with_composite():
    return _json_block([
        {"id": "n1", "goal": "read config", "tool": "read_file",
         "args_template": {"path": "cfg"}, "outputs": ["content"], "deps": []},
        {"id": "n2", "goal": "analyze and summarize sources", "tool": None,
         "outputs": ["content"], "deps": ["n1"]},
        {"id": "n3", "goal": "write summary", "tool": "write_file",
         "args_template": {"file_path": "out.md", "content": "${n2.content}"},
         "outputs": ["result"], "deps": ["n2"]},
    ])


def test_composite_refinement_preserves_interface():
    refinement = _json_block([
        {"id": "1", "goal": "read source one", "tool": "read_file",
         "args_template": {"path": "s1"}, "outputs": ["content"], "deps": []},
        {"id": "2", "goal": "merge with config", "tool": "write_file",
         "args_template": {"file_path": "merged",
                           "content": "${1.content} ${n1.content}"},
         "outputs": ["content"], "deps": ["1", "n1"]},
    ])
    dag, history, llm = _compile([_coarse_with_composite(), refinement])
    assert len(llm.calls) == 2
    assert sorted(dag.nodes) == ['n1', 'n2.1', 'n2.2', 'n3']
    # children are namespaced, lineage recorded
    assert dag.get('n2.1').parent_id == 'n2'
    assert dag.get('n2.2').depth == 1
    # sibling placeholder namespaced; external ref kept
    assert dag.get('n2.2').args_template['content'] == '${n2.1.content} ${n1.content}'
    # consumer n3 rewired to the child producing 'content'
    assert dag.get('n3').deps == ['n2.2']
    assert dag.get('n3').args_template['content'] == '${n2.2.content}'
    assert dag.validate() == []
    # history: coarse + 1 refinement
    assert len(history.entries) == 2
    assert history.entries[1]['refined_node_id'] == 'n2'
    assert history.ancestor_chain('n2.2') == ['n2.2', 'n2']


def test_interface_violation_retried():
    bad = _json_block([  # missing required output 'content'
        {"id": "1", "goal": "x", "tool": "read_file",
         "args_template": {"path": "s"}, "outputs": ["other"], "deps": []},
    ])
    good = _json_block([
        {"id": "1", "goal": "x", "tool": "read_file",
         "args_template": {"path": "s"}, "outputs": ["content"], "deps": []},
    ])
    dag, _, llm = _compile([_coarse_with_composite(), bad, good])
    assert len(llm.calls) == 3
    assert 'n2.1' in dag.nodes
    assert dag.validate() == []


def test_refinement_exhaustion_keeps_node_atomic():
    bad = _json_block([
        {"id": "1", "goal": "x", "tool": "read_file",
         "args_template": {"path": "s"}, "outputs": ["other"], "deps": []},
    ])
    dag, history, _ = _compile([_coarse_with_composite(), bad, bad, bad])
    # composite n2 survives untouched; executor will free-bind it
    assert dag.get('n2').is_composite
    assert dag.get('n3').deps == ['n2']
    assert len(history.entries) == 1  # only the coarse pass recorded
    assert dag.validate() == []


def test_compile_call_budget_enforced():
    # Coarse pass eats 3 calls (2 failures + success), leaving budget for
    # refinements; a graph full of composites must stop at the cap, not loop.
    composites = [{"id": f"n{i}", "goal": f"c{i}", "tool": None,
                   "outputs": [], "deps": []} for i in range(1, 6)]
    bad = "garbage"
    responses = [_json_block(composites)] + [bad] * (MAX_COMPILE_LLM_CALLS * 2)
    llm = ScriptedLLM(responses)
    dag, _ = compile_task_graph("goal", TOOLS, llm, LOCK)
    assert len(llm.calls) <= MAX_COMPILE_LLM_CALLS
    assert all(dag.get(f"n{i}").is_composite for i in range(1, 6))


def test_refinement_bailout_after_first_failed_node():
    """A refinement that fails all attempts disables refinement for the rest
    of the compile — the backbone clearly can't do it, and every further try
    would burn a full LLM call for nothing (seen live: 118s compiles)."""
    composites = [{"id": f"n{i}", "goal": f"c{i}", "tool": None,
                   "outputs": [], "deps": []} for i in range(1, 5)]
    llm = ScriptedLLM([_json_block(composites)] + ["garbage"] * 10)
    dag, _ = compile_task_graph("goal", TOOLS, llm, LOCK)
    # 1 coarse + (1 + _REFINE_RETRIES) failed refinement attempts, then bail
    assert len(llm.calls) == 3
    assert all(dag.get(f"n{i}").is_composite for i in range(1, 5))


def test_compile_calls_are_token_capped():
    from backend.agent_runtime.atg.compiler import _COMPILE_MAX_TOKENS

    class RecordingLLM(ScriptedLLM):
        def __init__(self, responses):
            super().__init__(responses)
            self.max_tokens_seen = []

        def chat_completion(self, messages, tools=None, temperature=None,
                            enable_thinking=True, max_tokens=None, log_file=None):
            self.max_tokens_seen.append(max_tokens)
            return super().chat_completion(messages, tools, temperature,
                                           enable_thinking, max_tokens, log_file)

    coarse = _json_block([{"id": "n1", "goal": "read", "tool": "read_file",
                           "args_template": {"path": "a"}, "outputs": ["content"],
                           "deps": []}])
    llm = RecordingLLM([coarse])
    compile_task_graph("goal", TOOLS, llm, LOCK)
    assert llm.max_tokens_seen == [_COMPILE_MAX_TOKENS]


# ── Helpers ──────────────────────────────────────────────────────────────────

def test_extract_json_variants():
    obj = {"nodes": []}
    assert _extract_json(f"```json\n{json.dumps(obj)}\n```") == obj
    assert _extract_json(f"prefix {json.dumps(obj)} suffix") == obj
    with pytest.raises(CompilationError):
        _extract_json("no json here")
    with pytest.raises(CompilationError):
        _extract_json('{"not_nodes": 1}')


def test_render_markdown():
    coarse = _json_block([
        {"id": "n1", "goal": "read a", "tool": "read_file",
         "args_template": {"path": "a"}, "outputs": ["content"], "deps": []},
        {"id": "n2", "goal": "read b", "tool": "read_file",
         "args_template": {"path": "b"}, "outputs": ["content"], "deps": []},
        {"id": "n3", "goal": "combine", "tool": None,
         "outputs": [], "deps": ["n1", "n2"]},
    ])
    dag, history, _ = _compile([coarse, "garbage", "garbage", "garbage"])
    md = render_markdown(dag, history)
    assert "# Task Graph (ATG)" in md
    assert "Wave 1 (parallel)" in md
    assert "**n1** `read_file`: read a" in md
    assert "_composite (LLM-bound)_" in md
