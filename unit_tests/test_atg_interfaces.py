"""Tests for ATG tool interface annotations."""

from backend.agent_runtime.atg.interfaces import (
    DEFAULT_INTERFACE,
    TOOL_INTERFACES,
    get_interface_catalog,
    get_tool_interface,
    is_read_only,
)
from backend.agent_runtime.llm_call import _READ_ONLY_TOOLS


def test_read_only_derived_from_llm_call():
    # Single source of truth: parallel-safety classification must match the loop's.
    for name in _READ_ONLY_TOOLS:
        assert is_read_only(name)
    assert not is_read_only('write_file')
    assert not is_read_only('bash')
    assert not is_read_only('unknown_tool_xyz')


def test_unknown_tool_gets_conservative_default():
    iface = get_tool_interface('some_plugin_tool')
    assert iface == DEFAULT_INTERFACE
    assert not is_read_only('some_plugin_tool')  # conservative: mutating


def test_known_tool_outputs():
    assert get_tool_interface('read_file')['outputs'] == {'content': 'string'}
    assert 'exit_code' in get_tool_interface('bash')['outputs']


def test_catalog_renders_registry_schemas():
    tools = [
        {'type': 'function', 'function': {
            'name': 'read_file',
            'description': 'Read a file',
            'parameters': {
                'type': 'object',
                'properties': {
                    'path': {'type': 'string'},
                    'offset': {'type': 'integer'},
                },
                'required': ['path'],
            },
        }},
        {'type': 'function', 'function': {
            'name': 'mystery_tool',
            'parameters': {'type': 'object', 'properties': {}},
        }},
    ]
    catalog = get_interface_catalog(tools)
    lines = catalog.splitlines()
    assert len(lines) == 2
    assert 'read_file(path: string, offset?: integer) -> {content: string} [read-only]' in lines[0]
    assert 'mystery_tool() -> {result: any} [mutating]' in lines[1]


def test_catalog_with_real_builtin_defs():
    # Builtin tool defs from the live registry must render without error.
    from backend.tools import tool_registry
    tools = tool_registry.get_builtin_tools({'id': 'test-agent', 'is_super': False})
    catalog = get_interface_catalog(tools)
    assert catalog  # non-empty
    for line in catalog.splitlines():
        assert line.startswith('- ')
        assert ' -> {' in line


def test_catalog_empty_and_malformed_inputs():
    assert get_interface_catalog([]) == ''
    assert get_interface_catalog(None) == ''
    # Missing name is skipped rather than crashing
    assert get_interface_catalog([{'function': {}}]) == ''


def test_all_declared_interfaces_have_outputs():
    for name, iface in TOOL_INTERFACES.items():
        assert iface.get('outputs'), f"{name} has no outputs declared"
