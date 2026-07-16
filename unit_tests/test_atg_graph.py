"""Tests for the ATG graph data model (pure logic, no LLM/DB)."""

import json

import pytest

from backend.agent_runtime.atg.graph import (
    MAX_DEPTH,
    MAX_NODES,
    NODE_OUTPUT_EXCERPT_CHARS,
    RefinementHistory,
    TaskDAG,
    TaskNode,
    find_placeholders,
    parse_placeholder,
)


def _node(nid, tool='read_file', deps=None, outputs=None, **kw):
    return TaskNode(id=nid, goal=f"goal {nid}", tool=tool,
                    deps=deps or [], outputs=outputs or ['content'], **kw)


def _dag(*nodes):
    dag = TaskDAG(root_goal="test goal")
    for n in nodes:
        dag.add_node(n)
    return dag


# ── Construction ─────────────────────────────────────────────────────────────

def test_add_duplicate_node_raises():
    dag = _dag(_node('n1'))
    with pytest.raises(ValueError):
        dag.add_node(_node('n1'))


def test_composite_node():
    n = TaskNode(id='n1', goal='do something')
    assert n.is_composite
    assert not _node('n2').is_composite


# ── Validation ───────────────────────────────────────────────────────────────

def test_valid_chain():
    dag = _dag(_node('n1'), _node('n2', deps=['n1']))
    assert dag.validate() == []
    assert dag.is_executable()


def test_unknown_dep():
    dag = _dag(_node('n1', deps=['nope']))
    assert any('unknown node' in e for e in dag.validate())


def test_self_dep():
    dag = _dag(_node('n1', deps=['n1']))
    assert any('depends on itself' in e for e in dag.validate())


def test_cycle_detected():
    dag = _dag(_node('n1', deps=['n2']), _node('n2', deps=['n1']))
    assert any('cycle' in e.lower() for e in dag.validate())


def test_node_count_limit():
    dag = TaskDAG(root_goal="big")
    for i in range(MAX_NODES + 1):
        dag.add_node(_node(f'n{i}'))
    assert any('max' in e for e in dag.validate())


def test_depth_limit():
    dag = _dag(_node('n1', depth=MAX_DEPTH + 1))
    assert any('depth' in e for e in dag.validate())


def test_input_interface_validation():
    producer = _node('n1', outputs=['content'])
    ok = _node('n2', deps=['n1'],
               inputs=[{'name': 'src', 'from_node': 'n1', 'key': 'content'}])
    assert _dag(producer, ok).validate() == []

    bad_key = _node('n3', deps=['n1'],
                    inputs=[{'name': 'src', 'from_node': 'n1', 'key': 'missing'}])
    errors = _dag(_node('n1', outputs=['content']), bad_key).validate()
    assert any('undeclared output' in e for e in errors)

    missing_dep = _node('n4',
                        inputs=[{'name': 'src', 'from_node': 'n1', 'key': 'content'}])
    errors = _dag(_node('n1', outputs=['content']), missing_dep).validate()
    assert any('does not declare it in deps' in e for e in errors)


def test_placeholder_validation():
    producer = _node('n1', outputs=['content'])
    ok = _node('n2', deps=['n1'],
               args_template={'text': 'prefix ${n1.content} suffix'})
    assert _dag(producer, ok).validate() == []

    bad = _node('n3', deps=['n1'], args_template={'text': '${n1.nope}'})
    errors = _dag(_node('n1', outputs=['content']), bad).validate()
    assert any('undeclared output' in e for e in errors)

    unknown = _node('n4', args_template={'text': '${ghost.content}'})
    errors = _dag(unknown).validate()
    assert any('unknown node' in e for e in errors)


def test_placeholder_parsing_with_dotted_node_ids():
    # node ids contain dots ("n1.2") — last dot separates the output key
    assert parse_placeholder('n1.2.content') == ('n1.2', 'content')
    assert parse_placeholder('nodots') is None
    assert find_placeholders({'a': '${n1.content}', 'b': ['${n2.stdout}', 3]}) == [
        'n1.content', 'n2.stdout']


# ── Waves (topological scheduling) ───────────────────────────────────────────

def test_waves_chain():
    dag = _dag(_node('n1'), _node('n2', deps=['n1']), _node('n3', deps=['n2']))
    assert dag.waves() == [['n1'], ['n2'], ['n3']]


def test_waves_diamond():
    dag = _dag(
        _node('n1'),
        _node('n2', deps=['n1']),
        _node('n3', deps=['n1']),
        _node('n4', deps=['n2', 'n3']),
    )
    assert dag.waves() == [['n1'], ['n2', 'n3'], ['n4']]


def test_waves_wide():
    dag = _dag(*[_node(f'n{i}') for i in range(5)])
    assert dag.waves() == [[f'n{i}' for i in range(5)]]


def test_waves_skip_terminal_nodes():
    n1 = _node('n1')
    n1.status = 'done'
    dag = _dag(n1, _node('n2', deps=['n1']))
    assert dag.waves() == [['n2']]


def test_waves_cycle_returns_partial():
    dag = _dag(_node('n1'), _node('n2', deps=['n3']), _node('n3', deps=['n2']))
    assert dag.waves() == [['n1']]


# ── Records & truncation ─────────────────────────────────────────────────────

def test_record_result_truncates_output():
    n = _node('n1')
    n.record_result(resolved_args={'path': 'x'},
                    output='a' * (NODE_OUTPUT_EXCERPT_CHARS + 500))
    excerpt = n.record['output_excerpt']
    assert len(excerpt) < NODE_OUTPUT_EXCERPT_CHARS + 100
    assert 'truncated' in excerpt
    assert n.record['resolved_args'] == {'path': 'x'}


def test_record_result_serializes_dict_output():
    n = _node('n1')
    n.record_result(output={'content': 'hello'})
    assert 'hello' in n.record['output_excerpt']


# ── Serialization ────────────────────────────────────────────────────────────

def test_dag_round_trip():
    n1 = _node('n1', outputs=['content'])
    n1.status = 'done'
    n1.record_result(output='data', resolved_args={'path': 'f.txt'})
    n2 = _node('n2', deps=['n1'], parent_id='n0', depth=1,
               args_template={'text': '${n1.content}'},
               inputs=[{'name': 'src', 'from_node': 'n1', 'key': 'content'}])
    dag = _dag(n1, n2)
    dag.version = 3

    restored = TaskDAG.from_dict(json.loads(json.dumps(dag.to_dict())))
    assert restored.version == 3
    assert restored.root_goal == 'test goal'
    assert restored.get('n1').status == 'done'
    assert restored.get('n1').record['output_excerpt'] == 'data'
    assert restored.get('n2').parent_id == 'n0'
    assert restored.get('n2').deps == ['n1']
    assert restored.to_dict() == dag.to_dict()


def test_structure_only_drops_runtime_state():
    n1 = _node('n1')
    n1.status = 'failed'
    n1.record_result(error='boom')
    d = _dag(n1).to_dict(structure_only=True)
    assert 'status' not in d['nodes']['n1']
    assert 'record' not in d['nodes']['n1']


# ── RefinementHistory ────────────────────────────────────────────────────────

def _history_with_refinement():
    """Coarse graph (n1, n2) then n2 refined into n2.1 -> n2.2."""
    hist = RefinementHistory()
    coarse = _dag(_node('n1'), TaskNode(id='n2', goal='composite'))
    hist.record(None, coarse)
    refined = _dag(
        _node('n1'),
        _node('n2.1', parent_id='n2', depth=1),
        _node('n2.2', deps=['n2.1'], parent_id='n2', depth=1),
    )
    hist.record('n2', refined)
    return hist


def test_ancestor_chain():
    hist = _history_with_refinement()
    assert hist.ancestor_chain('n2.2') == ['n2.2', 'n2']
    assert hist.ancestor_chain('n1') == ['n1']


def test_lowest_common_historical_ancestor():
    hist = _history_with_refinement()
    assert hist.lowest_common_historical_ancestor(['n2.1', 'n2.2']) == 'n2'
    assert hist.lowest_common_historical_ancestor(['n2.2']) == 'n2.2'
    assert hist.lowest_common_historical_ancestor(['n1', 'n2.2']) is None
    assert hist.lowest_common_historical_ancestor([]) is None


def test_history_round_trip():
    hist = _history_with_refinement()
    restored = RefinementHistory.from_dict(json.loads(json.dumps(hist.to_dict())))
    assert len(restored.entries) == 2
    assert restored.entries[1]['refined_node_id'] == 'n2'
    assert restored.ancestor_chain('n2.2') == ['n2.2', 'n2']
