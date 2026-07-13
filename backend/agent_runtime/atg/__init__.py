"""
ATG (Atomic Task Graph) — training-free DAG planning/execution layer.

Based on arXiv 2607.01942; see plan/atg-paper-analysis.md and
.claude/tasks/atg-implementation.md.

Public surface consumed by llm_loop:
    is_atg_eligible(agent, ms) — gate check (flag + complex classification)
    run_dag_execution(...)     — dependency-aware DAG execution
"""
from backend.agent_runtime.atg.graph import (  # noqa: F401
    RefinementHistory,
    TaskDAG,
    TaskNode,
)


def run_dag_execution(*args, **kwargs):
    """Lazy import so merely gating on is_atg_eligible never loads the executor."""
    from backend.agent_runtime.atg.executor import run_dag_execution as _run
    return _run(*args, **kwargs)


def is_atg_eligible(agent: dict, ms) -> bool:
    """True when the ATG path applies to this turn.

    Requires the per-agent enable_atg flag, a live AgentState (implies
    enable_agent_state), and a task classified complex (auto_trivial False).
    """
    if not agent or not agent.get('enable_atg'):
        return False
    if ms is None:
        return False
    return not getattr(ms, 'auto_trivial', False)
