"""
Tool I/O interface annotations for ATG.

Input schemas come from the live tool registry (OpenAI function defs); this
module only adds what the registry lacks: declared output keys per tool.
Read-only classification is derived from llm_call._READ_ONLY_TOOLS — the
single source of truth for parallel safety — rather than duplicated here.

All Evonic tool backends signal failure with an 'error' key in the result
dict, so 'error' is implicit in every interface and never declared. Unknown
tools get a conservative default: treated as mutating with an opaque result.
"""
from backend.agent_runtime.llm_call import _READ_ONLY_TOOLS

# Declared output keys per tool (key -> type label for compiler prompts).
TOOL_INTERFACES = {
    'read_file':    {'outputs': {'content': 'string'}},
    'write_file':   {'outputs': {'result': 'string'}},
    'str_replace':  {'outputs': {'result': 'string'}},
    'patch':        {'outputs': {'result': 'string', 'hunks_applied': 'integer'}},
    'delete_file':  {'outputs': {'result': 'string', 'deleted': 'string'}},
    'bash':         {'outputs': {'stdout': 'string', 'stderr': 'string', 'exit_code': 'integer'}},
    'runpy':        {'outputs': {'stdout': 'string', 'stderr': 'string', 'exit_code': 'integer'}},
    'calculator':   {'outputs': {'result': 'number'}},
    'find':         {'outputs': {'result': 'string'}},
    'stats':        {'outputs': {'result': 'string'}},
    'tree':         {'outputs': {'result': 'string'}},
    'which':        {'outputs': {'result': 'string'}},
    'get_current_date': {'outputs': {'result': 'string'}},
}

DEFAULT_INTERFACE = {'outputs': {'result': 'any'}}


def get_tool_interface(tool_name: str) -> dict:
    return TOOL_INTERFACES.get(tool_name, DEFAULT_INTERFACE)


def is_read_only(tool_name: str) -> bool:
    return tool_name in _READ_ONLY_TOOLS


def get_interface_catalog(tools: list) -> str:
    """Render the condensed tool catalog for compiler prompts.

    `tools` is the OpenAI function-def list as passed to run_tool_loop.
    One line per tool: name(param: type, optional?) -> {out: type} [read-only|mutating]
    """
    lines = []
    for tool_def in tools or []:
        fn = tool_def.get('function', tool_def) or {}
        name = fn.get('name')
        if not name:
            continue
        params = (fn.get('parameters') or {}).get('properties') or {}
        required = set((fn.get('parameters') or {}).get('required') or [])
        parts = []
        for pname, spec in params.items():
            ptype = (spec or {}).get('type', 'any')
            suffix = '' if pname in required else '?'
            parts.append(f"{pname}{suffix}: {ptype}")
        outputs = get_tool_interface(name)['outputs']
        out_str = ', '.join(f"{k}: {v}" for k, v in outputs.items())
        mode = 'read-only' if is_read_only(name) else 'mutating'
        lines.append(f"- {name}({', '.join(parts)}) -> {{{out_str}}} [{mode}]")
    return '\n'.join(lines)
