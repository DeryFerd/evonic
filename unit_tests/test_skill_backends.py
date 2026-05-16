"""
Auto-discovery test runner for skill backend tool functions.

Mirrors test_tool_backends.py but scans skills/*/backend/tools/*.py
for tool modules that define a `test_execute()` function.
"""

import os
import warnings
import importlib.util
import pytest

SKILLS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'skills')
SKIP_FILES = {'__init__.py'}


def _discover_skill_tool_modules():
    if not os.path.isdir(SKILLS_DIR):
        return
    for skill_name in sorted(os.listdir(SKILLS_DIR)):
        tools_dir = os.path.join(SKILLS_DIR, skill_name, 'backend', 'tools')
        if not os.path.isdir(tools_dir):
            continue
        # Add backend dir to sys.path so intra-skill imports (e.g. from cli_helper) work
        backend_dir = os.path.dirname(tools_dir)
        import sys
        inserted = False
        if backend_dir not in sys.path:
            sys.path.insert(0, backend_dir)
            inserted = True
        try:
            for fname in sorted(os.listdir(tools_dir)):
                if not fname.endswith('.py') or fname in SKIP_FILES:
                    continue
                tool_name = fname[:-3]
                path = os.path.join(tools_dir, fname)
                spec = importlib.util.spec_from_file_location(
                    f'skills.{skill_name}.backend.tools.{tool_name}', path
                )
                module = importlib.util.module_from_spec(spec)
                try:
                    spec.loader.exec_module(module)
                    yield f'{skill_name}/{tool_name}', module
                except (ModuleNotFoundError, ImportError) as e:
                    warnings.warn(f'Skipping {tool_name} in skill {skill_name}: {e}')
        finally:
            if inserted and backend_dir in sys.path:
                sys.path.remove(backend_dir)


def _build_params():
    params = []
    for label, module in _discover_skill_tool_modules():
        if hasattr(module, 'test_execute'):
            params.append(pytest.param(module, id=label))
        else:
            params.append(pytest.param(module, id=label, marks=pytest.mark.skip(
                reason=f'{label} has no test_execute()')))
    return params


@pytest.mark.parametrize('module', _build_params())
def test_skill_tool_backend(module):
    module.test_execute()
