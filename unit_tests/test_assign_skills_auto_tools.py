"""
Unit tests for _exec_assign_skills auto-assign of non-lazy skill tools.

When a non-lazy skill is assigned to an agent, all its tools should be
automatically assigned with proper `skill:<skill_id>:<fn_name>` namespaced IDs.
Lazy skills (lazy_tools=true) should be skipped.
"""

import os
import sys
import json
import tempfile
import pytest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.db import db

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

AGENT_ID = 'test_agent_auto'
SKILL_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'skills')


def _make_agent():
    """Create a test agent with some starter tools."""
    if not db.get_agent(AGENT_ID):
        db.create_agent({
            'id': AGENT_ID, 'name': 'Test Agent',
            'system_prompt': '', 'is_super': False,
        })
    db.set_agent_tools(AGENT_ID, ['bash', 'read_file'])
    db.set_agent_skills(AGENT_ID, [])


SCHEDULER_TOOL_DEFS = [
    {'type': 'function', 'function': {'name': 'create_schedule', 'parameters': {}}},
    {'type': 'function', 'function': {'name': 'cancel_schedule', 'parameters': {}}},
    {'type': 'function', 'function': {'name': 'list_schedules', 'parameters': {}}},
]


def _mock_non_lazy_scheduler(mock_sm):
    """Configure a mocked skills_manager with a non-lazy 'scheduler' skill.

    The real scheduler skill is lazy (lazy_tools=true) since task #688, so
    tests exercising the non-lazy auto-assign path must mock it.
    """
    mock_sm.get_skill.return_value = {'id': 'scheduler', 'tools_file': 'tools.json'}
    mock_sm.get_skill_tool_defs.return_value = SCHEDULER_TOOL_DEFS


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestAssignSkillsAutoTools:
    """Auto-assign tools when non-lazy skills are assigned."""

    def test_non_lazy_skill_auto_assigns_tools(self, use_test_database):
        """Assign a non-lazy skill (scheduler) → tools are auto-assigned."""
        _make_agent()

        from backend.tools.super_agent_tools import _exec_assign_skills

        with patch('backend.skills_manager.skills_manager') as mock_sm:
            _mock_non_lazy_scheduler(mock_sm)
            result = _exec_assign_skills({
                'agent_id': AGENT_ID,
                'skill_ids': ['scheduler'],
            })

        assert result.get('success') is True
        assert 'Auto-assigned' in result.get('message', '')
        assert '3 tool(s)' in result.get('message', '')

        tools = db.get_agent_tools(AGENT_ID)
        # Should have original tools + 3 scheduler tools
        assert 'bash' in tools
        assert 'read_file' in tools
        assert 'skill:scheduler:create_schedule' in tools
        assert 'skill:scheduler:cancel_schedule' in tools
        assert 'skill:scheduler:list_schedules' in tools
        assert len(tools) == 5

    def test_lazy_skill_does_not_auto_assign_tools(self, use_test_database):
        """Assign a lazy skill → no tools are auto-assigned."""
        _make_agent()

        from backend.tools.super_agent_tools import _exec_assign_skills

        # Mock skills_manager to return a skill with lazy_tools=true
        with patch('backend.skills_manager.skills_manager') as mock_sm:
            mock_sm.get_skill.return_value = {
                'id': 'lazy_skill', 'lazy_tools': True,
            }
            result = _exec_assign_skills({
                'agent_id': AGENT_ID,
                'skill_ids': ['lazy_skill'],
            })

        assert result.get('success') is True
        assert 'Auto-assigned' not in result.get('message', '')

        tools = db.get_agent_tools(AGENT_ID)
        assert tools == ['bash', 'read_file']  # unchanged

    def test_already_assigned_skill_no_duplicate_tools(self, use_test_database):
        """Re-assigning an already-assigned skill does not duplicate tools."""
        _make_agent()

        from backend.tools.super_agent_tools import _exec_assign_skills

        with patch('backend.skills_manager.skills_manager') as mock_sm:
            _mock_non_lazy_scheduler(mock_sm)

            # First assignment
            _exec_assign_skills({
                'agent_id': AGENT_ID,
                'skill_ids': ['scheduler'],
            })

            tools_after_first = db.get_agent_tools(AGENT_ID)

            # Second assignment — same skill
            result = _exec_assign_skills({
                'agent_id': AGENT_ID,
                'skill_ids': ['scheduler'],
            })

        assert 'already assigned' in result.get('message', '').lower()

        tools_after_second = db.get_agent_tools(AGENT_ID)
        assert tools_after_second == tools_after_first  # no dupes
        # Count scheduler tools appear only once
        scheduler_count = sum(1 for t in tools_after_second if 'scheduler' in t)
        assert scheduler_count == 3

    def test_nonexistent_skill_handled_gracefully(self, use_test_database):
        """Assigning a non-existent skill does not crash."""
        _make_agent()

        from backend.tools.super_agent_tools import _exec_assign_skills

        result = _exec_assign_skills({
            'agent_id': AGENT_ID,
            'skill_ids': ['nonexistent_skill_xyz'],
        })

        assert result.get('success') is True
        assert 'Auto-assigned' not in result.get('message', '')

        tools = db.get_agent_tools(AGENT_ID)
        assert tools == ['bash', 'read_file']  # unchanged

    def test_empty_skills_list_no_changes(self, use_test_database):
        """Empty skill_ids returns early without error."""
        _make_agent()

        from backend.tools.super_agent_tools import _exec_assign_skills

        result = _exec_assign_skills({
            'agent_id': AGENT_ID,
            'skill_ids': [],
        })

        # All already assigned (none requested) → unchanged
        tools = db.get_agent_tools(AGENT_ID)
        assert tools == ['bash', 'read_file']

    def test_mix_of_lazy_and_non_lazy(self, use_test_database):
        """Assigning a mix of lazy and non-lazy skills auto-assigns only non-lazy tools."""
        _make_agent()

        from backend.tools.super_agent_tools import _exec_assign_skills

        with patch('backend.skills_manager.skills_manager') as mock_sm:
            # scheduler is non-lazy, lazy_skill is lazy
            def get_skill_side_effect(skill_id):
                if skill_id == 'scheduler':
                    return {
                        'id': 'scheduler',
                        'tools_file': 'tools.json',
                    }
                elif skill_id == 'lazy_skill':
                    return {
                        'id': 'lazy_skill',
                        'lazy_tools': True,
                    }
                return None

            mock_sm.get_skill.side_effect = get_skill_side_effect

            def get_tool_defs_side_effect(skill_id):
                if skill_id == 'scheduler':
                    return [
                        {'type': 'function', 'function': {'name': 'create_schedule', 'parameters': {}}},
                        {'type': 'function', 'function': {'name': 'cancel_schedule', 'parameters': {}}},
                        {'type': 'function', 'function': {'name': 'list_schedules', 'parameters': {}}},
                    ]
                return []

            mock_sm.get_skill_tool_defs.side_effect = get_tool_defs_side_effect

            result = _exec_assign_skills({
                'agent_id': AGENT_ID,
                'skill_ids': ['scheduler', 'lazy_skill'],
            })

        assert result.get('success') is True
        assert 'Auto-assigned 3 tool(s)' in result.get('message', '')

        tools = db.get_agent_tools(AGENT_ID)
        assert len(tools) == 5  # bash, read_file + 3 scheduler tools
        assert 'skill:scheduler:create_schedule' in tools
        assert 'skill:scheduler:cancel_schedule' in tools
        assert 'skill:scheduler:list_schedules' in tools

    def test_tools_not_duplicated_when_partially_assigned(self, use_test_database):
        """When some scheduler tools are already assigned, only missing ones are added."""
        _make_agent()
        # Pre-assign one scheduler tool manually
        db.set_agent_tools(AGENT_ID, [
            'bash', 'read_file', 'skill:scheduler:create_schedule',
        ])

        from backend.tools.super_agent_tools import _exec_assign_skills

        with patch('backend.skills_manager.skills_manager') as mock_sm:
            _mock_non_lazy_scheduler(mock_sm)
            result = _exec_assign_skills({
                'agent_id': AGENT_ID,
                'skill_ids': ['scheduler'],
            })

        assert result.get('success') is True
        # Only 2 new tools should be auto-assigned (cancel_schedule, list_schedules)
        assert 'Auto-assigned 2 tool(s)' in result.get('message', '')

        tools = db.get_agent_tools(AGENT_ID)
        assert len(tools) == 5
        assert tools.count('skill:scheduler:create_schedule') == 1


class TestAssignSkillsValidation:
    """Input validation tests."""

    def test_missing_agent_id(self, use_test_database):
        from backend.tools.super_agent_tools import _exec_assign_skills
        result = _exec_assign_skills({'skill_ids': ['scheduler']})
        assert 'error' in result

    def test_invalid_agent_id(self, use_test_database):
        from backend.tools.super_agent_tools import _exec_assign_skills
        result = _exec_assign_skills({
            'agent_id': 'nonexistent_agent_999',
            'skill_ids': ['scheduler'],
        })
        assert 'error' in result

    def test_skill_ids_not_a_list(self, use_test_database):
        _make_agent()
        from backend.tools.super_agent_tools import _exec_assign_skills
        result = _exec_assign_skills({
            'agent_id': AGENT_ID,
            'skill_ids': 'not_a_list',
        })
        assert 'error' in result
