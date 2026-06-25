"""
_workspace.py — shared workspace path resolution for file tools.
"""
from typing import Optional

import os

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_AGENTS_DIR = os.path.join(_BASE_DIR, 'agents')
_SHARED_AGENTS_DIR = os.path.join(_BASE_DIR, 'shared', 'agents')
_SELF_PREFIX = '/_self/'


def effective_agent_id(agent: dict) -> str:
    """Return the effective agent ID for /_self/ path resolution.

    Sub-agents don't have their own directory under agents/ — they inherit
    their parent's SYSTEM.md, KB files, tool assignments, and skill
    assignments.  This mirrors context.py:_effective_id().
    """
    agent_id = (agent or {}).get('id', '')
    if (agent or {}).get('is_subagent'):
        return (agent or {}).get('parent_id', agent_id)
    return agent_id


def is_self_path(file_path: str) -> bool:
    """Return True if file_path uses the /_self/ virtual prefix."""
    return bool(file_path) and (file_path.startswith(_SELF_PREFIX) or file_path == '/_self')


def missing_slash_self_hint(file_path: str) -> Optional[str]:
    """Return a hint string when file_path starts with '_self/' without leading slash.

    Small models sometimes drop the leading '/' when constructing /_self/... paths.
    This hint tells them to use the correct prefix.
    """
    if file_path and (file_path.startswith('_self/') or file_path == '_self'):
        return (
            f"If you meant to access an agent directory, "
            f"use the prefix `/_self/` (with a leading slash)."
        )
    return None


def resolve_self_path(agent_id: str, file_path: str) -> Optional[str]:
    """Resolve /_self/... to the agent's local directory on the evonic server.

    Returns the absolute local path, or None if the resolved path escapes
    the agent's directory (path traversal / symlink attack prevention).
    """
    rel = file_path[len(_SELF_PREFIX):] if file_path.startswith(_SELF_PREFIX) else ''

    # /_self/artifacts/ resolves to shared/agents/<id>/artifacts/ (not agents/<id>/artifacts/)
    if rel == 'artifacts' or rel.startswith('artifacts/'):
        base_dir = _SHARED_AGENTS_DIR
        safe_root = os.path.realpath(os.path.join(_SHARED_AGENTS_DIR, agent_id, 'artifacts'))
    else:
        base_dir = _AGENTS_DIR
        safe_root = os.path.realpath(os.path.join(_AGENTS_DIR, agent_id))

    abs_path = os.path.normpath(os.path.join(base_dir, agent_id, rel))
    resolved = os.path.realpath(abs_path)
    if not resolved.startswith(safe_root + os.sep) and resolved != safe_root:
        return None
    return resolved


def resolve_workspace_path(agent, file_path: str, fallback_workspace: str) -> str:
    """Resolve a file path to an absolute path, honoring the agent's workspace.

    Rules (in priority order):
    1. If path starts with '/workspace', strip that prefix and join with the
       agent's workspace (or fallback_workspace).  This is the runpy-sandbox
       convention for paths inside a Docker container.
    2. If path is relative (not absolute) and the agent has a workspace set,
       resolve it relative to that workspace.
    3. If the agent has a workspace set, validate that the resolved path stays
       within the workspace boundary using realpath prefix-check.  Absolute
       paths outside the workspace are blocked by returning a non-existent
       path that the caller will report as "file not found".
    4. Otherwise return the path unchanged.
    """
    if not file_path:
        return file_path

    if file_path.startswith('/workspace'):
        workspace_root = (agent or {}).get('workspace') or fallback_workspace
        rel = file_path[len('/workspace'):].lstrip('/')
        resolved = os.path.join(os.path.abspath(workspace_root), rel)
        # Boundary check
        workspace = (agent or {}).get('workspace')
        if workspace:
            try:
                workspace_real = os.path.realpath(workspace)
                path_real = os.path.realpath(resolved)
                if not (path_real == workspace_real or
                        path_real.startswith(workspace_real + os.sep)):
                    return file_path  # block: return unresolvable path
            except (OSError, PermissionError):
                pass
        return resolved

    if not os.path.isabs(file_path):
        workspace = (agent or {}).get('workspace')
        if workspace:
            # Sub-agents are delegated, throwaway tasks — contain their
            # relative-path writes inside the gitignored .scratch/ dir so they
            # never clutter the project root.  Absolute paths pass through
            # unchanged (mirrors the shell cwd redirect for sub-agents).
            base = os.path.abspath(workspace)
            if (agent or {}).get('is_subagent'):
                base = os.path.join(base, '.scratch')
            resolved = os.path.join(base, file_path)
            # Boundary check for relative path traversal (e.g. ../../etc/passwd)
            try:
                workspace_real = os.path.realpath(workspace)
                path_real = os.path.realpath(resolved)
                if not (path_real == workspace_real or
                        path_real.startswith(workspace_real + os.sep)):
                    return file_path  # block: return unresolvable path
            except (OSError, PermissionError):
                pass
            return resolved

    # Absolute path — if agent has a workspace, block escape
    workspace = (agent or {}).get('workspace')
    if workspace and os.path.isabs(file_path):
        try:
            workspace_real = os.path.realpath(workspace)
            path_real = os.path.realpath(file_path)
            if path_real == workspace_real or path_real.startswith(workspace_real + os.sep):
                return file_path
        except (OSError, PermissionError):
            pass
        return file_path

    return file_path


# Extensions treated as "scratch" — agent-authored one-off scripts / temp output.
_SCRATCH_EXTENSIONS = ('.py', '.sh', '.txt', '.json', '.log', '.tmp', '.md', '.csv')


def root_pollution_nudge(agent, host_path: Optional[str]) -> Optional[str]:
    """Return a corrective message if writing host_path would drop a NEW
    scratch-like file directly into the evonic project root.

    Fires only when the agent's effective workspace IS the project root (the
    super agent, its sub-agents, or any empty-workspace agent).  Never blocks
    edits to existing files, files inside a subdirectory, or non-scratch
    extensions.  This is the only enforcing layer that reaches the super agent,
    whose cwd legitimately stays at the project root.
    """
    if not host_path:
        return None
    workspace = (agent or {}).get('workspace') or _BASE_DIR
    base = os.path.realpath(_BASE_DIR)
    try:
        if os.path.realpath(workspace) != base:
            return None  # agent has its own workspace — already isolated
        parent = os.path.realpath(os.path.dirname(os.path.abspath(host_path)))
    except (OSError, PermissionError):
        return None
    if parent != base:
        return None  # not a direct child of the project root
    if os.path.exists(host_path):
        return None  # editing an existing root file is fine
    _, ext = os.path.splitext(host_path)
    if ext.lower() not in _SCRATCH_EXTENSIONS:
        return None
    name = os.path.basename(host_path)
    return (
        f"Refused: '{name}' would be written to the project root, which must stay clean. "
        f"Write throwaway scripts and temp files to /workspace/.scratch (gitignored, "
        f"available as the $SCRATCH env var). Durable project scripts belong in scripts/ "
        f"(migrations in scripts/migrations/). Re-issue the write with one of those paths."
    )
