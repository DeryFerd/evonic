"""
Auto-discovery test runner for backend tool functions.

Scans backend/tools/*.py for tool modules that define a `test_execute()` function
and runs them. Tools without `test_execute()` are skipped.
"""

import os
import sys
import importlib.util
import pytest

TOOLS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'backend', 'tools')
SKIP_FILES = {'__init__.py', 'registry.py'}


def _discover_tool_modules():
    """Find all tool .py files and load them, yielding (name, module) pairs."""
    for fname in sorted(os.listdir(TOOLS_DIR)):
        if not fname.endswith('.py') or fname in SKIP_FILES:
            continue
        name = fname[:-3]
        modname = f'backend.tools.{name}'
        # Reuse the already-imported module: replacing the sys.modules entry
        # with a fresh copy would strand other tests' patch.object() calls on
        # the old module object while production code imports the new one.
        if modname in sys.modules:
            yield name, sys.modules[modname]
            continue
        path = os.path.join(TOOLS_DIR, fname)
        spec = importlib.util.spec_from_file_location(modname, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        # Register in sys.modules so patches in test_execute() resolve correctly.
        # Without this, unittest.mock.patch(“backend.tools.foo.bar“) would
        # resolve to a *different* module object and the patch would not apply.
        sys.modules[spec.name] = module
        yield name, module


def _build_params():
    """Build pytest parametrize list from discovered tools."""
    params = []
    for name, module in _discover_tool_modules():
        if hasattr(module, 'test_execute'):
            params.append(pytest.param(module, id=name))
        else:
            params.append(pytest.param(module, id=name, marks=pytest.mark.skip(
                reason=f'{name} has no test_execute()')))
    return params


@pytest.mark.parametrize('module', _build_params())
def test_tool_backend(module):
    module.test_execute()
