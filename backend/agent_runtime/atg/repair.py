"""
ATG minimal-subgraph repair.

On node failure, the repair region is localized through the refinement
history: the lowest common historical ancestor (LCA) of the failed node and
its interface-adjacent nodes bounds the region; within it, validated `done`
nodes are FROZEN (their outputs are reused as boundary inputs) and only the
rest is recompiled by one REPAIR LLM call whose output must preserve the
region's external interface. Everything outside the region is untouched.

Mechanically the region's replaceable nodes are collapsed into a synthetic
composite node and the compiler's interface-preserving `_splice` re-expands
it with the repaired subgraph — the same validated machinery as refinement.
"""
from __future__ import annotations

import json
import logging

from backend.agent_runtime.atg import prompts
from backend.agent_runtime.atg.compiler import (
    CompilationError,
    _LLMCaller,
    _parse_nodes,
    _splice,
)
from backend.agent_runtime.atg.graph import (
    RefinementHistory,
    TaskDAG,
    TaskNode,
    find_placeholders,
    parse_placeholder,
)
from backend.agent_runtime.atg.interfaces import get_interface_catalog

_logger = logging.getLogger(__name__)

_REPAIR_RETRIES = 2


class RepairError(Exception):
    pass


# ── Region computation (pure logic) ──────────────────────────────────────────

def _dependents(dag: TaskDAG, node_id: str) -> set:
    return {nid for nid, n in dag.nodes.items() if node_id in n.deps}


def _downstream(dag: TaskDAG, node_id: str) -> set:
    """All transitive dependents of node_id."""
    result = set()
    frontier = [node_id]
    while frontier:
        current = frontier.pop()
        for dep_id in _dependents(dag, current):
            if dep_id not in result:
                result.add(dep_id)
                frontier.append(dep_id)
    return result


def compute_repair_region(dag: TaskDAG, history: RefinementHistory,
                          failed_node_id: str) -> dict:
    """Locate the minimal repair region for a failed node.

    Returns {'ancestor', 'replace': [ids], 'frozen': [ids]} where `replace`
    is the failed node plus every not-yet-validated node under the LCA, and
    `frozen` are validated (done) nodes in the region whose outputs are kept.
    """
    failed = dag.nodes[failed_node_id]
    adjacent = list(failed.deps) + sorted(_dependents(dag, failed_node_id))
    # Only adjacent nodes sharing refinement lineage bound the region — a
    # top-level neighbor with no common ancestor must not void the LCA.
    failed_chain = set(history.ancestor_chain(failed_node_id))
    related = [a for a in adjacent
               if set(history.ancestor_chain(a)) & failed_chain]
    ancestor = history.lowest_common_historical_ancestor(
        [failed_node_id] + related) or failed_node_id

    if ancestor == failed_node_id:
        region = {failed_node_id}
    else:
        region = {nid for nid in dag.nodes
                  if ancestor in history.ancestor_chain(nid)}
        region.add(failed_node_id)

    frozen = sorted(nid for nid in region
                    if dag.nodes[nid].status in ('done', 'frozen'))
    replace = sorted(region - set(frozen))
    return {'ancestor': ancestor, 'replace': replace, 'frozen': frozen}


# ── Region collapse (pure logic) ─────────────────────────────────────────────

def collapse_region(dag: TaskDAG, replace_ids: list, region_id: str,
                    region_goal: str) -> TaskNode:
    """Collapse the replaceable nodes into one synthetic composite node.

    External consumers are rewired onto the composite; its interface is the
    set of output keys those consumers use. Raises RepairError when two
    replaced nodes export the same key externally (ambiguous collapse).
    """
    replace = set(replace_ids)

    # key -> producing replaced node, from the view of external consumers
    exported = {}
    for nid, node in dag.nodes.items():
        if nid in replace:
            continue
        for inp in node.inputs:
            if inp.get('from_node') in replace:
                key = inp['key']
                if exported.get(key, inp['from_node']) != inp['from_node']:
                    raise RepairError(
                        f"Ambiguous external output '{key}' from region {sorted(replace)}.")
                exported[key] = inp['from_node']
        for ref in find_placeholders(node.args_template):
            parsed = parse_placeholder(ref)
            if parsed and parsed[0] in replace:
                src, key = parsed
                if exported.get(key, src) != src:
                    raise RepairError(
                        f"Ambiguous external output '{key}' from region {sorted(replace)}.")
                exported[key] = src

    # external deps of the region (includes frozen nodes)
    external_deps = sorted({d for nid in replace
                            for d in dag.nodes[nid].deps if d not in replace})

    composite = TaskNode(
        id=region_id, goal=region_goal, tool=None,
        outputs=sorted(exported), deps=external_deps,
        parent_id=dag.nodes[replace_ids[0]].parent_id if replace_ids else None,
        depth=min(dag.nodes[nid].depth for nid in replace) if replace else 0,
    )

    # Rewire external consumers onto the composite (per exported key).
    for nid, node in dag.nodes.items():
        if nid in replace:
            continue
        touched = False
        for inp in node.inputs:
            if inp.get('from_node') in replace:
                inp['from_node'] = region_id
                touched = True
        node.args_template = _redirect_placeholders(
            node.args_template, replace, region_id)
        if touched or any(d in replace for d in node.deps):
            node.deps = [d for d in node.deps if d not in replace]
            if region_id not in node.deps:
                node.deps.append(region_id)

    for nid in replace:
        del dag.nodes[nid]
    dag.add_node(composite)
    return composite


def _redirect_placeholders(value, replace: set, region_id: str):
    from backend.agent_runtime.atg.graph import PLACEHOLDER_RE

    if isinstance(value, str):
        def sub(m):
            parsed = parse_placeholder(m.group(1))
            if parsed and parsed[0] in replace:
                return '${%s.%s}' % (region_id, parsed[1])
            return m.group(0)
        return PLACEHOLDER_RE.sub(sub, value)
    if isinstance(value, dict):
        return {k: _redirect_placeholders(v, replace, region_id) for k, v in value.items()}
    if isinstance(value, list):
        return [_redirect_placeholders(v, replace, region_id) for v in value]
    return value


# ── Repair driver (one LLM call + validated splice) ─────────────────────────

def attempt_repair(dag: TaskDAG, history: RefinementHistory,
                   failed_node_id: str, llm, llm_lock, tools: list,
                   attempt: int, log_file: str = None) -> TaskDAG:
    """Build and splice a repaired subgraph. Returns the new DAG.

    Raises RepairError / CompilationError when repair is not possible —
    callers degrade to the plain-loop fallback.
    """
    failed = dag.nodes.get(failed_node_id)
    if failed is None:
        raise RepairError(f"Unknown failed node '{failed_node_id}'.")

    region = compute_repair_region(dag, history, failed_node_id)
    region_id = f"{region['ancestor']}#r{attempt}"
    region_goal = (dag.nodes[region['ancestor']].goal
                   if region['ancestor'] in dag.nodes and region['ancestor'] not in region['replace']
                   else failed.goal)

    work = TaskDAG.from_dict(dag.to_dict())
    work.version = dag.version
    failed_error = failed.record.get('error')
    failed_args = failed.record.get('resolved_args')
    failed_tool = failed.tool

    composite = collapse_region(work, region['replace'], region_id,
                                region_goal)

    external = []
    for dep in composite.deps:
        producer = work.nodes.get(dep)
        if producer:
            external.append(f"{dep} (outputs: {producer.outputs})")

    caller = _LLMCaller(llm, llm_lock, log_file=log_file)
    system = prompts.COMPILE_SYSTEM.format(
        catalog=get_interface_catalog(tools), schema=prompts.GRAPH_JSON_SCHEMA)
    user = prompts.REPAIR_USER.format(
        failed_goal=failed.goal,
        tool=failed_tool or '(unbound)',
        args=json.dumps(failed_args or {}, default=str)[:500],
        error=failed_error or 'unknown',
        region_goal=region_goal,
        region_id=region_id,
        external=', '.join(external) or '(none)',
        outputs=composite.outputs,
        schema=prompts.GRAPH_JSON_SCHEMA)

    last_error = None
    for _ in range(1 + _REPAIR_RETRIES):
        prompt = user if last_error is None else (
            user + prompts.COMPILE_RETRY_SUFFIX.format(errors=last_error))
        try:
            obj = caller.json_call(system, prompt)
            candidate = TaskDAG.from_dict(work.to_dict())
            candidate.version = work.version
            id_map = {str(nd.get('id')): f"{region_id}.{nd.get('id')}"
                      for nd in obj['nodes']
                      if isinstance(nd, dict) and nd.get('id')}
            children = _parse_nodes(obj, id_map=id_map, parent_id=region['ancestor'],
                                    depth=composite.depth)
            _splice(candidate, region_id, children)
            errors = candidate.validate()
            if errors:
                raise CompilationError('; '.join(errors))
            candidate.version = dag.version + 1
            return candidate
        except CompilationError as e:
            last_error = str(e)
            _logger.warning("ATG repair attempt for %s failed: %s",
                            failed_node_id, e)
    raise RepairError(f"Repair of '{failed_node_id}' failed: {last_error}")
