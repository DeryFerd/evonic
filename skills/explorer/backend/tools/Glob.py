"""
Glob — find files matching a glob pattern.

Returns a list of matching file paths. Supports ** for recursive matching.
"""
import os
import glob as _glob
from ._utils import _auto_correct_path, _validate_workspace_boundary, _resolve_workspace


def execute(agent: dict, args: dict) -> dict:
    pattern = args.get('pattern', '')
    if not pattern:
        return {'error': 'pattern is required'}

    base_path = args.get('path', '.')
    base_path = _resolve_workspace(agent, base_path)

    # Path auto-correction: if the resolved path doesn't exist, try glob-resolve
    if not os.path.exists(base_path):
        workspace = (agent or {}).get('workspace', '')
        if workspace:
            corrected = _auto_correct_path(base_path, workspace, path_is_dir=True)
            if os.path.exists(corrected):
                base_path = corrected

    # Enforce workspace boundary (blocks path traversal, absolute paths, symlinks)
    workspace = (agent or {}).get('workspace', '')
    if workspace:
        base_path = _validate_workspace_boundary(base_path, workspace)

    if not os.path.exists(base_path):
        return {'error': f'path not found: {base_path}'}
    if not os.path.isdir(base_path):
        return {'error': f'path is not a directory: {base_path}'}

    search = os.path.join(base_path, pattern)
    matches = _glob.glob(search, recursive=True)

    dirs_to_skip = {'.git', 'node_modules', '__pycache__', 'venv', '.venv', 'vendor', 'target', 'build', 'dist'}

    results = []
    for m in sorted(matches):
        skip = False
        for part in os.path.relpath(m, base_path).split(os.sep):
            if part in dirs_to_skip:
                skip = True
                break
        if skip:
            continue
        if os.path.isfile(m):
            rel = os.path.relpath(m, base_path)
            results.append(rel)

    return {
        'files': results,
        'count': len(results),
        'base': os.path.abspath(base_path),
    }
