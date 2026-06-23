"""
Read — read a text file and return its content with 1-based line numbers.

Supports pagination via the `offset` parameter for large files.
Mirrors the core read_file tool behavior but with FastContext naming convention.
"""
import os
from ._utils import _auto_correct_path, _validate_workspace_boundary, _resolve_workspace

_MAX_FILE_SIZE = 400 * 1024
_CHUNK_CHARS = 8000


def execute(agent: dict, args: dict) -> dict:
    file_path = args.get('file_path', '')
    if not file_path:
        return {'error': 'file_path is required'}

    file_path = _resolve_workspace(agent, file_path)

    # Path auto-correction: if the resolved path doesn't exist, try glob-resolve
    if not os.path.exists(file_path):
        workspace = (agent or {}).get('workspace', '')
        if workspace:
            corrected = _auto_correct_path(file_path, workspace, path_is_dir=False)
            if os.path.exists(corrected):
                file_path = corrected

    # Enforce workspace boundary (blocks path traversal, absolute paths, symlinks)
    workspace = (agent or {}).get('workspace', '')
    if workspace:
        file_path = _validate_workspace_boundary(file_path, workspace)

    if not os.path.exists(file_path):
        return {'error': f'file not found: {file_path}'}
    if os.path.isdir(file_path):
        return {'error': f'path is a directory, not a file: {file_path}'}

    file_size = os.path.getsize(file_path)
    if file_size > _MAX_FILE_SIZE:
        return {'error': f'file size ({file_size / 1024:.1f}KB) exceeds 400KB limit'}

    offset = int(args.get('offset', 1))

    try:
        with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
            lines = f.readlines()
    except PermissionError:
        return {'error': 'permission denied'}
    except Exception as e:
        return {'error': f'cannot read file: {e}'}

    if not lines:
        return {'content': '(empty file)', 'total_lines': 0}

    total_lines = len(lines)
    filename = os.path.basename(file_path)
    file_size_kb = file_size / 1024

    start_idx = max(0, min(offset - 1, total_lines - 1))

    output_lines = []
    chars = 0
    end_idx = start_idx
    for i in range(start_idx, total_lines):
        line_str = f'{i + 1}: {lines[i].rstrip()}'
        if chars + len(line_str) + 1 > _CHUNK_CHARS and output_lines:
            break
        output_lines.append(line_str)
        chars += len(line_str) + 1
        end_idx = i + 1

    shown_start = start_idx + 1
    shown_end = end_idx

    header = f'[File: {filename} | {total_lines} lines | {file_size_kb:.1f}KB | showing lines {shown_start}-{shown_end}]'
    content_block = '\n'.join(output_lines)

    if shown_end < total_lines:
        remaining = total_lines - shown_end
        footer = f'\n[...{remaining} lines remaining. Use offset={shown_end + 1} to continue.]'
        full_text = f'{header}\n\n{content_block}{footer}'
    else:
        full_text = f'{header}\n\n{content_block}'

    return {
        'content': full_text,
        'file': filename,
        'total_lines': total_lines,
        'shown_start': shown_start,
        'shown_end': shown_end,
    }
