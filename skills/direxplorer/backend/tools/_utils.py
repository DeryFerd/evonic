"""
Shared utilities for FastContext tools — Grep, Glob, Read.

Provides _auto_correct_path for hallucinated-path fallback resolution.
"""
import os
import glob as _glob


def _auto_correct_path(requested_path: str, workspace: str, path_is_dir: bool = False) -> str:
    """If the requested path doesn't exist, try glob-resolving from workspace root.

    The FastContext model often hallucinates paths (e.g. 'skills/' instead of
    'evonic/skills/'). This fallback searches for a suffix match. Returns the
    original path if nothing is found.
    """
    if os.path.exists(requested_path):
        return requested_path

    if not os.path.isdir(workspace):
        return requested_path

    basename = os.path.basename(requested_path.rstrip(os.sep)) or requested_path.rstrip(os.sep)

    pattern = os.path.join(workspace, '**', basename)
    matches = _glob.glob(pattern, recursive=True)

    if path_is_dir:
        matches = [m for m in matches if os.path.isdir(m)]
    else:
        matches = [m for m in matches if os.path.isfile(m)]

    return sorted(matches)[0] if matches else requested_path


def _resolve_workspace(agent: dict, path: str) -> str:
    """Resolve a file path against the agent's workspace.

    Handles three cases:
    1. /workspace sandbox prefix → maps to agent's host workspace
       (e.g. /workspace/skills → /home/robin/dev/evonic/skills)
    2. Relative paths → joins with agent's workspace
    3. Absolute paths → returns os.path.abspath (boundary check done separately)
    """
    if path.startswith('/workspace'):
        workspace_root = (agent or {}).get('workspace', '')
        rel = path[len('/workspace'):].lstrip('/')
        resolved = os.path.join(os.path.abspath(workspace_root), rel)
        return resolved

    workspace = (agent or {}).get('workspace', '')
    if workspace and not os.path.isabs(path):
        return os.path.join(os.path.abspath(workspace), path)
    return os.path.abspath(path)


def _validate_workspace_boundary(resolved_path: str, workspace: str) -> str:
    """Validate that resolved_path stays within the workspace boundary.

    Uses os.path.realpath to resolve all symlinks and canonicalize both paths,
    then checks whether the resolved path is equal to or a subpath of the
    workspace. This blocks three attack vectors:

    1. Relative path traversal (``../../etc/passwd``)
    2. Absolute path escape (``/etc/shadow``)
    3. Symlink attacks (symlink inside workspace pointing to outside)

    Returns the resolved canonical path on success. Raises PermissionError if
    the path escapes the workspace.

    This function is a no-op for agents without a workspace set.
    """
    workspace_real = os.path.realpath(workspace)
    path_real = os.path.realpath(resolved_path)
    if path_real == workspace_real or path_real.startswith(workspace_real + os.sep):
        return path_real
    raise PermissionError("Access denied: path escapes workspace")
