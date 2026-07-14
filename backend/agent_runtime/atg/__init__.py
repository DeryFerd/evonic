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


# ATG runs that already consumed their graph; a new task may re-arm the cycle.
_REARMABLE_STATUSES = {'done', 'fallback', 'failed'}


def maybe_rearm_atg(agent: dict, ms, user_text: str) -> bool:
    """Re-enter the plan/compile cycle when a NEW complex task arrives in a
    session whose previous ATG run already finished.

    Two-stage classification keeps this conservative: the message must be
    complex (classify_task) AND a new task rather than a follow-up on the
    finished goal (classify_continuation, defaults to continuation). On
    re-arm, mutates ms: clears atg and plan_file (forcing a fresh compile
    before execute mode) and returns to plan mode. Returns True if re-armed.
    """
    if not agent or not agent.get('enable_atg') or ms is None:
        return False
    text = (user_text or '').strip()
    if ms.mode != 'execute' or not text:
        return False
    atg = ms.atg
    if not isinstance(atg, dict) or atg.get('status') not in _REARMABLE_STATUSES:
        return False
    prev_goal = (((atg.get('dag') or {}).get('root_goal'))
                 or atg.get('root_goal') or '').strip()
    if not prev_goal:
        return False

    from backend.task_classifier import classify_continuation, classify_task
    if classify_task(text) != 'complex':
        return False
    if classify_continuation(prev_goal, text) != 'new_task':
        return False

    ms.atg = None
    ms.plan_file = None  # set_mode('execute') stays blocked until a new compile
    ms.mode = 'plan'
    ms.auto_trivial = False
    return True


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
