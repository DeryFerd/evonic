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


def render_cmp_section(cmp: dict, agent_name: str = "Agent") -> str:
    """Lazy re-export so AgentState.render never pays the import unless used."""
    from backend.agent_runtime.cmp.render import render_cmp_section as _render
    return _render(cmp, agent_name)


def on_turn_boundary(agent: dict, ms, chatlog, user_text: str):
    """Per-turn CMP orchestration: first-path init, boundary detection,
    path lifecycle (switch/branch), hysteresis. Called from the runtime at
    the re-arm slot; subsumes maybe_rearm_atg for cmp agents (a new branch
    IS the re-arm — fresh plan cycle on its own path).

    Returns the decision dict, or None when CMP does not apply.
    Mutates ms (and ms.cmp) in place; persistence rides the normal
    agent-state persist paths.
    """
    if not is_cmp_enabled(agent) or ms is None:
        return None

    from backend.agent_runtime.cmp import detector, store
    from backend.agent_runtime.cmp.compactor import finalize_active_card

    text = (user_text or '').strip()
    user_ts = _last_user_ts(chatlog)

    # First-path init: adopt the ongoing work (or this first message) as P1.
    if not ms.cmp or not ms.cmp.get('paths'):
        title = _current_work_title(ms) or text[:60] or 'Session start'
        ms.cmp = store.new_cmp(ms, title=title, goal=text[:300],
                               now_ts=user_ts)
        _emit(agent, 'cmp_path_created',
              {'path_id': 'P1', 'title': title, 'initiator': 'auto-init'})
        return {'decision': 'init', 'target': 'P1', 'layer': 'init'}

    decision = detector.detect(ms.cmp, ms, text,
                               recent_tail=_last_final_excerpt(chatlog))
    d, target = decision['decision'], decision.get('target')
    try:
        if d == 'return':
            finalize_active_card(chatlog, ms.cmp, ms)
            store.switch_to(ms.cmp, ms, target, now_ts=user_ts)
            _emit(agent, 'cmp_path_switched',
                  {'to': target, 'initiator': 'detector'})
        elif d in ('dep_branch', 'indep_branch'):
            finalize_active_card(chatlog, ms.cmp, ms)
            record = store.create_path(
                ms.cmp, ms, title=text[:60], goal=text[:300],
                depends_on=[target] if (d == 'dep_branch' and target) else [],
                now_ts=user_ts)
            decision['target'] = record['id']
            _emit(agent, 'cmp_path_created',
                  {'path_id': record['id'], 'depends_on': record['depends_on'],
                   'initiator': 'detector'})
    except Exception:
        import logging
        logging.getLogger(__name__).exception(
            "CMP path operation failed — continuing on the active path")
        decision = {'decision': 'continue', 'target': None, 'layer': 'error'}

    for archived_id in store.tick_hysteresis(ms.cmp):
        _emit(agent, 'cmp_path_archived', {'path_id': archived_id})
    _emit(agent, 'cmp_boundary_decision', decision)
    return decision


def _current_work_title(ms) -> str:
    if isinstance(ms.atg, dict):
        goal = ((ms.atg.get('dag') or {}).get('root_goal')
                or ms.atg.get('root_goal'))
        if goal:
            return str(goal)[:60]
    if ms.plan_file:
        return str(ms.plan_file)[:60]
    return ''


def _last_final_excerpt(chatlog, max_chars: int = 300) -> str:
    """Excerpt of the agent's latest final reply — the just-delivered
    deliverable, fed to the boundary classifier for context."""
    try:
        entry = chatlog.get_last_entry(types=frozenset({'final'}))
        content = (entry or {}).get('content') or ''
        return content[:max_chars]
    except Exception:
        return ''


def _last_user_ts(chatlog):
    try:
        entry = chatlog.get_last_entry(types={'user'})
        if entry and entry.get('ts'):
            return entry['ts']
    except Exception:
        pass
    import time
    return int(time.time() * 1000)


def _emit(agent: dict, event: str, payload: dict) -> None:
    try:
        from backend.event_stream import event_stream
        event_stream.emit(event, {'agent_id': agent.get('id', ''), **payload})
    except Exception:
        pass
