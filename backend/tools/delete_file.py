"""Backend implementation for the delete_file tool — permanently deletes a file."""

import logging
import os

logger = logging.getLogger(__name__)

from backend.tools._workspace import resolve_workspace_path

try:
    from config import SANDBOX_WORKSPACE as _WORKSPACE_ROOT
except ImportError:
    _WORKSPACE_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))


def execute(agent, args: dict) -> dict:
    file_path = args.get('file_path')
    display_path = file_path

    if not file_path:
        return {'error': "Missing required argument: 'file_path'"}

    from backend.tools._workspace import (
        is_self_path, resolve_self_path, effective_agent_id,
    )
    agent_id = effective_agent_id(agent or {})

    if agent_id and is_self_path(file_path):
        local_path = resolve_self_path(agent_id, file_path)
        if not local_path:
            return {'error': "Access denied — path escapes agent directory."}
        if not os.path.isfile(local_path):
            return {'error': f"File not found: {display_path}"}
        try:
            os.remove(local_path)
        except PermissionError:
            return {'error': f"Permission denied: {display_path}"}
        except OSError as e:
            return {'error': str(e)}
        if '/kb/' in local_path:
            try:
                from backend.agent_runtime.evomem_writer import mark_dirty
                mark_dirty(agent_id)
            except Exception as e:
                logger.warning("delete_file[%s]: failed to schedule evomem sync: %s",
                               agent_id, e)
        return {'result': 'success', 'deleted': display_path}

    file_path = resolve_workspace_path(agent, file_path, _WORKSPACE_ROOT)

    if not os.path.isfile(file_path):
        return {'error': f"File not found: {display_path}"}

    try:
        os.remove(file_path)
    except PermissionError:
        return {'error': f"Permission denied: {display_path}"}
    except OSError as e:
        return {'error': str(e)}

    if '/kb/' in file_path:
        try:
            from backend.agent_runtime.evomem_writer import mark_dirty
            aid = (agent or {}).get('id', '')
            if (agent or {}).get('is_subagent'):
                aid = (agent or {}).get('parent_id', aid)
            mark_dirty(aid)
        except Exception as e:
            logger.warning("delete_file: failed to schedule evomem sync: %s", e)

    return {'result': 'success', 'deleted': display_path}
