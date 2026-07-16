"""
Glob — find files matching a glob pattern.

Returns a list of matching file paths. Supports ** for recursive matching.
"""
import os
from ._utils import _prepare_path, _run_python_json


def execute(agent: dict, args: dict) -> dict:
    pattern = args.get('pattern', '')
    if not pattern:
        return {'error': 'pattern is required'}

    requested = args.get('path', '.')
    try:
        backend, base_path, info = _prepare_path(agent, requested, want_dir=True)
    except PermissionError as exc:
        return {'error': str(exc)}
    except RuntimeError as exc:
        return {'error': f'cannot inspect path: {exc}'}
    if not info['exists']:
        return {'error': f'path not found: {base_path}'}
    if not info['is_dir']:
        return {'error': f'path is not a directory: {base_path}'}

    code = f'''import glob, json, os
base, pattern = {base_path!r}, {pattern!r}
skip = {{'.git', 'node_modules', '__pycache__', 'venv', '.venv', 'vendor', 'target', 'build', 'dist'}}
files = []
for path in sorted(glob.glob(os.path.join(base, pattern), recursive=True)):
    rel = os.path.relpath(path, base)
    if os.path.isfile(path) and not any(part in skip for part in rel.split(os.sep)):
        files.append(rel)
print(json.dumps(files))'''
    try:
        results = _run_python_json(backend, code)
    except RuntimeError as exc:
        return {'error': f'glob failed: {exc}'}

    return {
        'files': results,
        'count': len(results),
        'base': os.path.abspath(base_path),
    }
