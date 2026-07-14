"""
CMP (Context Memory Path) — cross-task context management.

Session-level sibling of ATG (which structures context WITHIN one task):
maintains a graph of task paths rendered as a navigable map, with
interface-preserving path cards, 4-class boundary detection and soft
offload. See .claude/tasks/cmp-implementation.md and the CMP paper.

Public surface:
    is_cmp_enabled(agent)              — flag gate
    render_cmp_section(cmp)            — map + cards block for AgentState.render
    on_turn_boundary(...)              — detector + lifecycle orchestrator (M3)
"""


def is_cmp_enabled(agent: dict) -> bool:
    """CMP applies only with agent-state enabled (paths live on AgentState)
    and never for sub-agents (delegated single-task workers)."""
    if not agent or not agent.get('enable_cmp'):
        return False
    if not agent.get('enable_agent_state'):
        return False
    return not agent.get('is_subagent')


def render_cmp_section(cmp: dict) -> str:
    """Lazy re-export so AgentState.render never pays the import unless used."""
    from backend.agent_runtime.cmp.render import render_cmp_section as _render
    return _render(cmp)
