"""
llm_call.py — LLM call preparation: tool classification & parallel execution primitives.

Part of the diet llm_loop.py refactor (Layout C / Pipeline).
"""

# ── Tool classification for parallel execution ──────────────────────────────

_READ_ONLY_TOOLS: frozenset = frozenset({
    'read_file', 'calculator', 'find', 'stats', 'tree',
    'which',
})

_ALWAYS_SERIAL_TOOLS: frozenset = frozenset({
    'use_skill', 'unload_skill', 'write_file', 'patch',
    'str_replace', 'runpy', 'bash', 'remember', 'recall',
    'send_notification', 'clear_log_file',
})

_MAX_PARALLEL_TOOL_WORKERS = 6

# ── Tool execution core ──────────────────────────────────────────────────────

def _execute_tool_core(fn_name: str, args: dict,
                       builtin_exec, real_exec) -> dict:
    """Execute a single tool call — pure execution, no side-effects.

    This is the parallelisable core. Guard checks, approval handling,
    use_skill/unload_skill injections, DB writes, event emits — all of
    those remain in the serial post-processing phase.
    """
    try:
        result = builtin_exec(fn_name, args)
        if result is None:
            result = real_exec(fn_name, args)
        return result
    except Exception as e:
        return {'error': f'Tool execution error: {e}'}
