"""
Grep — regex search across files in a directory using ripgrep.

Returns matching lines grouped by file with line numbers.
"""
import os
import json
import subprocess
from ._utils import _auto_correct_path, _validate_workspace_boundary, _resolve_workspace

_MAX_MATCHES = 500


def execute(agent: dict, args: dict) -> dict:
    pattern = args.get('pattern', '')
    if not pattern:
        return {'error': 'pattern is required'}

    search_path = args.get('path', '.')
    include = args.get('include', '')

    search_path = _resolve_workspace(agent, search_path)

    # Path auto-correction: if the resolved path doesn't exist, try glob-resolve
    if not os.path.exists(search_path):
        workspace = (agent or {}).get('workspace', '')
        if workspace:
            corrected = _auto_correct_path(search_path, workspace, path_is_dir=True)
            if os.path.exists(corrected):
                search_path = corrected

    # Enforce workspace boundary (blocks path traversal, absolute paths, symlinks)
    workspace = (agent or {}).get('workspace', '')
    if workspace:
        search_path = _validate_workspace_boundary(search_path, workspace)

    if not os.path.exists(search_path):
        return {'error': f'path not found: {search_path}'}

    # Build ripgrep command
    cmd = ['rg', '--json', '--no-heading', '--max-count', str(_MAX_MATCHES)]

    if include:
        cmd.extend(['--glob', include])

    cmd.append(pattern)
    cmd.append(search_path)

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30
        )
    except FileNotFoundError:
        return {'error': 'ripgrep (rg) is not installed'}
    except subprocess.TimeoutExpired:
        return {'error': 'search timed out after 30 seconds'}

    # Parse ripgrep JSON output
    matches_by_file: dict = {}
    total_matches = 0
    truncated = False

    for line in result.stdout.strip().split('\n'):
        if not line:
            continue
        try:
            entry = json.loads(line)
            typ = entry.get('type', '')

            if typ == 'match':
                match_data = entry.get('data', {})
                file_abs = match_data.get('path', {}).get('text', '')
                line_num = match_data.get('line_number', 0)
                line_text = match_data.get('lines', {}).get('text', '').rstrip('\n')

                if total_matches >= _MAX_MATCHES:
                    truncated = True
                    break

                if file_abs not in matches_by_file:
                    matches_by_file[file_abs] = []

                matches_by_file[file_abs].append({
                    'line': line_num,
                    'content': line_text,
                })
                total_matches += 1

        except (json.JSONDecodeError, KeyError):
            continue

    # Convert to sorted output
    matches = []
    for file_abs in sorted(matches_by_file.keys()):
        if os.path.isdir(search_path):
            rel = os.path.relpath(file_abs, search_path)
        else:
            rel = file_abs
        matches.append({'file': rel, 'matches': matches_by_file[file_abs]})

    result = {'matches': matches, 'total_matches': total_matches}
    if truncated:
        result['truncated'] = True
        result['note'] = f'Results truncated at {_MAX_MATCHES} matches. Narrow your search.'

    return result
