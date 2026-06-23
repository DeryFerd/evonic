"""Explore — spawn a read-only explorer sub-agent to investigate a directory.

The explorer runs independently with its own (centrally configured) model,
system prompt, and tools, confined to the target path, and reports its findings
back to the caller's session via agent messaging.
"""

import os
import logging

_logger = logging.getLogger(__name__)

# Limits mirror agent_messaging.injected_system_vars validation.
_MAX_CONTEXT_VARS = 10
_MAX_VAR_VALUE_LEN = 1024

EXPLORER_TASK_DIRECTIVE = (
    "You are an explorer sub-agent. Investigate the directory you have been given "
    "(your file tools are confined to it) and report a clear, concise summary of "
    "your findings back to whoever delegated this task. Do NOT make a plan or ask "
    "for approval — explore directly until you have an answer.\n\n--- EXPLORE ---\n"
)


def _sanitize_context_vars(raw) -> tuple:
    """Return (vars, error). Coerce to flat str→str, enforce limits."""
    if raw is None:
        return {}, None
    if not isinstance(raw, dict):
        return None, "context_vars must be an object (key→value pairs)."
    if len(raw) > _MAX_CONTEXT_VARS:
        return None, f"context_vars may have at most {_MAX_CONTEXT_VARS} keys."
    clean = {}
    for k, v in raw.items():
        key = str(k)
        val = str(v) if v is not None else ""
        if len(val) > _MAX_VAR_VALUE_LEN:
            return None, f"context_vars['{key}'] exceeds {_MAX_VAR_VALUE_LEN} characters."
        clean[key] = val
    return clean, None


def execute(agent: dict, args: dict) -> dict:
    from models.db import db
    from backend.subagent_manager import subagent_manager
    from backend.skills_manager import skills_manager
    from backend.agent_runtime import explorer
    from backend.agent_runtime.notifier import notify_agent
    from backend.agent_report_to import resolve_report_to_for_subagent_spawn
    from backend.tools._workspace import resolve_workspace_path

    parent_id = agent.get('id', '')
    if not parent_id:
        return {'error': 'Cannot determine the calling agent ID from context.'}

    # No nested exploration: explorers and sub-agents cannot spawn explorers.
    if agent.get('is_explorer') or agent.get('is_subagent'):
        return {'error': 'Sub-agents and explorers cannot spawn explorers.'}

    raw_path = (args.get('path') or '').strip()
    if not raw_path:
        return {'error': 'A "path" is required. Use Explore({path: "/abs/dir", ...}).'}

    # Resolve like the other file tools: the sandbox alias '/workspace' and
    # relative paths map to the caller's workspace; absolute host paths pass
    # through unchanged (exploring outside the workspace is the whole point).
    caller_ws = agent.get('workspace') or ''
    path = os.path.abspath(resolve_workspace_path(agent, raw_path, caller_ws))
    if not os.path.isdir(path):
        suffix = f' (resolved to: {path})' if path != raw_path else ''
        return {'error': f'path is not an existing directory: {raw_path}{suffix}'}

    context_vars, cv_err = _sanitize_context_vars(args.get('context_vars'))
    if cv_err:
        return {'error': cv_err}

    # Explorers run with the DirExplorer worker skill's read-only tools.
    if not explorer.worker_skill_enabled():
        return {'error': (
            f"The '{explorer.WORKER_SKILL_ID}' (DirExplorer) skill must be enabled — "
            f"explorer sub-agents use its Grep/Read/Glob tools to do the work."
        )}

    parent_agent = db.get_agent(parent_id)
    if not parent_agent:
        return {'error': f'Calling agent "{parent_id}" not found in DB.'}

    # Resolve config + tool set from the skill settings.
    skill_cfg = skills_manager.get_skill_config(explorer.SKILL_ID)
    explorer_tool_ids, tool_err = explorer.resolve_tool_ids(skill_cfg.get('tool_ids', ''))
    if tool_err:
        return {'error': tool_err}

    def _build(explorer_id: str) -> dict:
        return explorer.build_config(
            parent_agent, explorer_id, path, skill_cfg, explorer_tool_ids,
        )

    try:
        explorer_id = subagent_manager.spawn_explorer(parent_agent, _build)
    except ValueError as e:
        return {'error': str(e)}

    parent_name = parent_agent.get('name', parent_id)
    report_to_id, report_to_channel_id = resolve_report_to_for_subagent_spawn(
        parent_id,
        agent.get('user_id', ''),
        agent.get('channel_id', '') or '',
    )

    result = notify_agent(
        agent_id=explorer_id,
        tag=f"AGENT/{parent_name}",
        message=f"{EXPLORER_TASK_DIRECTIVE}Target directory: {path}",
        external_user_id=f"__agent__{parent_id}",
        channel_id=None,
        dedup=False,
        trigger_llm=True,
        metadata={
            'agent_message': True,
            'from_agent_id': parent_id,
            'from_agent_name': parent_name,
            'agent_message_depth': 1,
            'subagent_spawn': True,
            'injected_system_vars': context_vars,
            'report_to_id': report_to_id,
            'report_to_channel_id': report_to_channel_id,
        },
    )

    _logger.info(
        "Explorer %s spawned by %s for path=%s (notify_result=%s)",
        explorer_id, parent_id, path, result,
    )

    return {
        'explorer_id': explorer_id,
        'path': path,
        'message': (
            f"Explorer '{explorer_id}' spawned to investigate '{path}'. "
            f"It will explore independently and report its findings back to you "
            f"via agent messaging."
        ),
    }
