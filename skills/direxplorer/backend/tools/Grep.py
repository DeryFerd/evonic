"""
Grep — regex search across files in a directory using ripgrep.

Returns matching lines grouped by file with line numbers.
"""
import os
import json
import subprocess
from ._utils import _auto_correct_path, _validate_workspace_boundary, _resolve_workspace

_MAX_MATCHES = 500
# Cap total output size (~50KB) to prevent context overflow when the
# explorer sub-agent returns a tool_trace to the delegating agent.
# A single Grep result with long lines can produce 1M+ chars through
# 500 matches x 2KB lines.  50KB keeps ~125 typical 400-char lines
# which is enough for the explorer to reason about.
_MAX_OUTPUT_CHARS = 50_000


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

    # --- Output size cap ---
    # Even with _MAX_MATCHES=500, long line content can produce 1M+ chars
    # of JSON.  Truncate progressively (drop ~20 % of matches per pass)
    # until the serialised output fits within _MAX_OUTPUT_CHARS.
    _result_str = json.dumps(result)
    if len(_result_str) > _MAX_OUTPUT_CHARS:
        _dropped = 0
        while len(_result_str) > _MAX_OUTPUT_CHARS and matches:
            _n_to_drop = max(1, len(matches) // 5)
            for _ in range(_n_to_drop):
                if not matches:
                    break
                matches.pop()
                _dropped += 1
            result['matches'] = matches
            result['total_matches'] = total_matches
            _result_str = json.dumps(result)
        if _dropped > 0:
            result['truncated'] = True
            taken = total_matches - _dropped
            result['note'] = (
                f'Output truncated to {_MAX_OUTPUT_CHARS} chars — '
                f'showing {taken}/{total_matches} matches across '
                f'{len(matches)} file(s). Narrow your search pattern or scope.'
            )

    return result
