"""
Skillset Manager - Load, resolve, and apply agent templates.

Skillsets are JSON files in /workspace/skillsets/ that define pre-configured
agent templates with tools, skills, system prompts, and KB files.
"""

import os
import json
from typing import Any, Dict, List, Optional

SKILLCSETS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'skillsets')


def _load_skillset_files() -> List[Dict[str, Any]]:
    """Load all skillset JSON files from the skillsets directory."""
    skillsets = []
    if not os.path.isdir(SKILLCSETS_DIR):
        return skillsets
    for fname in sorted(os.listdir(SKILLCSETS_DIR)):
        if not fname.endswith('.json'):
            continue
        try:
            with open(os.path.join(SKILLCSETS_DIR, fname), 'r', encoding='utf-8') as f:
                data = json.load(f)
                skillsets.append(data)
        except (json.JSONDecodeError, IOError):
            continue
    return skillsets


def list_skillsets() -> List[Dict[str, Any]]:
    """List all available skillsets (id, name, description only)."""
    all_skillsets = _load_skillset_files()
    return [
        {
            'id': s.get('id', ''),
            'name': s.get('name', ''),
            'description': s.get('description', ''),
            'tools_count': len(s.get('tools', [])),
            'skills_count': len(s.get('skills', [])),
        }
        for s in all_skillsets
    ]


def count_skillsets() -> int:
    """Count skillset JSON files without loading full content.

    Much faster than list_skillsets() — no JSON parsing, just a directory scan.
    """
    if not os.path.isdir(SKILLCSETS_DIR):
        return 0
    return sum(1 for fname in os.listdir(SKILLCSETS_DIR) if fname.endswith('.json'))


def get_skillset(skill_id: str) -> Optional[Dict[str, Any]]:
    """Get a single skillset by its ID."""
    for s in _load_skillset_files():
        if s.get('id') == skill_id:
            return s
    return None


def resolve_skillset(skill_id: str) -> Optional[Dict[str, Any]]:
    """Resolve a skillset's tool names to actual available tool IDs.
    
    Returns the skillset with 'resolved_tools' containing actual tool IDs
    that can be assigned to an agent.
    """
    skillset = get_skillset(skill_id)
    if not skillset:
        return None
    
    tool_names = skillset.get('tools', [])
    
    # Try to resolve against available tools, fall back to raw names on error
    resolved_tools = list(tool_names)
    try:
        from backend.tools import tool_registry
        all_defs = tool_registry.get_all_tool_defs()
        available_names = set()
        for td in all_defs:
            fn = td.get('function', {})
            name = fn.get('name', '')
            if name:
                available_names.add(name)
        resolved_tools = [name for name in tool_names if name in available_names]
    except Exception:
        pass  # Use raw tool names if registry is unavailable
    
    result = dict(skillset)
    result['resolved_tools'] = resolved_tools
    result['unresolved_tools'] = [name for name in tool_names if name not in resolved_tools]
    return result


def update_skillset(skill_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
    """Update a skillset JSON file with new data.

    Returns {'success': True, 'skillset': {...}} on success or {'error': '...'} on failure.
    """
    import glob

    pattern = os.path.join(SKILLCSETS_DIR, '*.json')
    found = False
    for fpath in glob.glob(pattern):
        try:
            with open(fpath, 'r', encoding='utf-8') as f:
                existing = json.load(f)
            if existing.get('id') == skill_id:
                found = True
                # Merge updates
                for key in ('name', 'description', 'system_prompt', 'model', 'tools', 'skills', 'kb_files'):
                    if key in data:
                        existing[key] = data[key]
                # Save back
                with open(fpath, 'w', encoding='utf-8') as f:
                    json.dump(existing, f, indent=2, ensure_ascii=False)
                return {'success': True, 'skillset': existing}
        except (json.JSONDecodeError, IOError):
            continue
    if not found:
        return {'error': f'Skillset \'{skill_id}\' not found.'}
    return {'error': f'Skillset \'{skill_id}\' not found.'}


def apply_skillset(skill_id: str, agent_data: Dict[str, Any]) -> Dict[str, Any]:
    """Apply a skillset template to new agent creation data.
    
    Merges skillset defaults into agent_data, with agent_data taking priority.
    Returns the merged agent creation payload.
    """
    skillset = get_skillset(skill_id)
    if not skillset:
        return {'error': f'Skillset \'{skill_id}\' not found.'}
    
    result = {}
    
    # System prompt from skillset unless overridden
    result['system_prompt'] = agent_data.get('system_prompt', skillset.get('system_prompt', ''))
    
    # Model from skillset unless overridden
    result['model'] = agent_data.get('model', skillset.get('model', '')) or None
    
    # Tools from skillset unless overridden
    if 'tools' in agent_data:
        result['tools'] = agent_data['tools']
    else:
        result['tools'] = skillset.get('tools', [])
    
    # Skills from skillset unless overridden
    if 'skills' in agent_data:
        result['skills'] = agent_data['skills']
    else:
        result['skills'] = skillset.get('skills', [])
    
    # KB files from skillset unless overridden
    if 'kb_files' in agent_data:
        result['kb_files'] = agent_data['kb_files']
    else:
        result['kb_files'] = skillset.get('kb_files', {})
    
    # Agent identity fields (always from agent_data)
    result['id'] = agent_data.get('id', '')
    result['name'] = agent_data.get('name', skillset.get('name', ''))
    result['description'] = agent_data.get('description', skillset.get('description', ''))
    
    return result
