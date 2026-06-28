"""
Tests for conversation tail_start logic — ensuring that when a summary exists,
leading assistant/tool messages are handled correctly.

Bug: When the summarizer cuts mid-turn, the tail starts with assistant/tool
messages. The old code skipped ALL leading non-user messages, causing the agent
to lose its own prior response. The fix preserves assistant messages when a
summary exists, but still skips orphaned tool responses.
"""

import os
import sys
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.chatlog import _reconstruct_llm_messages


def _apply_tail_start(conv_msgs, has_summary):
    """Replicate the tail_start logic from runtime.py and prefetch.py."""
    tail_start = 0
    if not has_summary:
        while tail_start < len(conv_msgs) and conv_msgs[tail_start].get('role') != 'user':
            tail_start += 1
    else:
        while tail_start < len(conv_msgs) and conv_msgs[tail_start].get('role') == 'tool':
            tail_start += 1
    return conv_msgs[tail_start:]


# ── _reconstruct_llm_messages tests ───────────────────────────────────────

class TestReconstructLlmMessages:
    """Verify that JSONL entries are correctly converted to LLM message format."""

    def test_orphaned_tool_output_is_dropped(self):
        """Orphaned tool_output entries (no preceding tool_calls) are dropped.

        The LLM API rejects tool messages without a preceding assistant
        message containing matching tool_calls.  This can happen when the
        summary watermark timestamp ties with a tool_call entry.
        """
        entries = [
            {'type': 'tool_output', 'content': '{"result": "ok"}', 'ts': 100},
            {'type': 'user', 'content': 'yes', 'ts': 200},
        ]
        msgs = _reconstruct_llm_messages(entries)
        assert len(msgs) == 1
        assert msgs[0]['role'] == 'user'

    def test_intermediate_becomes_assistant(self):
        entries = [
            {'type': 'intermediate', 'content': 'Found the bug!', 'ts': 100},
            {'type': 'user', 'content': 'fix it', 'ts': 200},
        ]
        msgs = _reconstruct_llm_messages(entries)
        assert msgs[0]['role'] == 'assistant'
        assert msgs[0]['content'] == 'Found the bug!'

    def test_final_becomes_assistant(self):
        entries = [
            {'type': 'final', 'content': 'Here are two bugs...', 'ts': 100},
            {'type': 'user', 'content': 'yes', 'ts': 200},
        ]
        msgs = _reconstruct_llm_messages(entries)
        assert msgs[0]['role'] == 'assistant'
        assert msgs[0]['content'] == 'Here are two bugs...'

    def test_slash_command_user_skipped(self):
        entries = [
            {'type': 'user', 'content': 'hello', 'ts': 100},
            {'type': 'user', 'content': '/status', 'metadata': {'slash_command': True}, 'ts': 200},
            {'type': 'final', 'content': 'hi', 'ts': 300},
        ]
        msgs = _reconstruct_llm_messages(entries)
        roles = [m['role'] for m in msgs]
        assert 'user' in roles
        # The slash command user message should be skipped
        user_msgs = [m for m in msgs if m['role'] == 'user']
        assert len(user_msgs) == 1
        assert user_msgs[0]['content'] == 'hello'


# ── tail_start logic tests ────────────────────────────────────────────────

class TestTailStartWithoutSummary:
    """Without summary, skip all leading non-user messages."""

    def test_leading_assistant_skipped(self):
        msgs = [
            {'role': 'assistant', 'content': 'old response'},
            {'role': 'user', 'content': 'hello'},
            {'role': 'assistant', 'content': 'hi'},
        ]
        result = _apply_tail_start(msgs, has_summary=False)
        assert result[0]['role'] == 'user'
        assert result[0]['content'] == 'hello'
        assert len(result) == 2

    def test_leading_tool_skipped(self):
        msgs = [
            {'role': 'tool', 'content': 'orphan', 'tool_call_id': 'c1'},
            {'role': 'user', 'content': 'hello'},
        ]
        result = _apply_tail_start(msgs, has_summary=False)
        assert len(result) == 1
        assert result[0]['role'] == 'user'

    def test_starts_with_user_unchanged(self):
        msgs = [
            {'role': 'user', 'content': 'hello'},
            {'role': 'assistant', 'content': 'hi'},
        ]
        result = _apply_tail_start(msgs, has_summary=False)
        assert result == msgs

    def test_empty_messages(self):
        assert _apply_tail_start([], has_summary=False) == []


class TestTailStartWithSummary:
    """With summary, keep assistant messages but skip orphaned tool responses."""

    def test_leading_assistant_preserved(self):
        """The core fix: assistant messages after summary cut are kept."""
        msgs = [
            {'role': 'assistant', 'content': 'Want me to apply these fixes?'},
            {'role': 'user', 'content': 'yes'},
        ]
        result = _apply_tail_start(msgs, has_summary=True)
        assert len(result) == 2
        assert result[0]['role'] == 'assistant'
        assert result[0]['content'] == 'Want me to apply these fixes?'
        assert result[1]['role'] == 'user'
        assert result[1]['content'] == 'yes'

    def test_orphaned_tool_responses_skipped(self):
        """Tool messages without preceding tool_calls are skipped."""
        msgs = [
            {'role': 'tool', 'content': '{"result": "ok"}', 'tool_call_id': 'c1'},
            {'role': 'tool', 'content': '{"result": "ok"}', 'tool_call_id': 'c2'},
            {'role': 'assistant', 'content': 'Found the bug'},
            {'role': 'user', 'content': 'fix it'},
        ]
        result = _apply_tail_start(msgs, has_summary=True)
        assert len(result) == 2
        assert result[0]['role'] == 'assistant'
        assert result[1]['role'] == 'user'

    def test_tool_after_assistant_preserved(self):
        """Tool messages that follow an assistant with tool_calls are kept."""
        msgs = [
            {'role': 'assistant', 'content': '', 'tool_calls': [
                {'id': 'c1', 'type': 'function', 'function': {'name': 'read_file', 'arguments': '{}'}}
            ]},
            {'role': 'tool', 'content': 'file contents', 'tool_call_id': 'c1'},
            {'role': 'user', 'content': 'thanks'},
        ]
        result = _apply_tail_start(msgs, has_summary=True)
        assert len(result) == 3
        assert result[0]['role'] == 'assistant'
        assert result[1]['role'] == 'tool'

    def test_starts_with_user_unchanged(self):
        msgs = [
            {'role': 'user', 'content': 'hello'},
            {'role': 'assistant', 'content': 'hi'},
        ]
        result = _apply_tail_start(msgs, has_summary=True)
        assert result == msgs

    def test_empty_messages(self):
        assert _apply_tail_start([], has_summary=True) == []


# ── End-to-end: JSONL entries → reconstruct → tail_start ──────────────────

class TestEndToEndTailReconstruction:
    """Simulate the real scenario: JSONL entries from after the summary watermark
    are reconstructed into LLM messages, then tail_start is applied."""

    def test_mid_turn_cut_preserves_final_response(self):
        """Reproduces the original bug: summarizer cuts after an intermediate,
        leaving tool_output + thinking + intermediate + final + user in the tail.
        The agent's final response must be preserved."""
        entries = [
            # Orphaned tool_output (tool_call was before the summary watermark)
            {'type': 'tool_output', 'content': '{"stdout": "found it"}', 'ts': 100},
            # Agent's thinking (not visible in final messages but processed)
            {'type': 'thinking', 'content': 'Now I see the full picture', 'ts': 101},
            # Agent's final response with fix proposal
            {'type': 'final', 'content': 'Here are two bugs. Want me to apply these fixes?', 'ts': 102},
            # User agrees
            {'type': 'user', 'content': 'yes', 'ts': 200},
        ]

        msgs = _reconstruct_llm_messages(entries)
        result = _apply_tail_start(msgs, has_summary=True)

        # The orphaned tool response should be skipped
        assert result[0]['role'] == 'assistant'
        assert 'Want me to apply these fixes?' in result[0]['content']
        # The assistant message should carry reasoning from thinking entry
        assert result[0].get('reasoning_content') == 'Now I see the full picture'
        # User's "yes" follows
        assert result[1]['role'] == 'user'
        assert result[1]['content'] == 'yes'

    def test_mid_turn_cut_with_tool_chain(self):
        """Tail starts with assistant tool_calls + tool responses + final + user.
        Everything should be preserved."""
        entries = [
            # Agent calls a tool
            {'type': 'intermediate', 'content': 'Let me check:', 'ts': 100},
            {'type': 'tool_call', 'function': 'read_file', 'params': {'path': '/a.py'},
             'id': 'c1', 'ts': 101},
            {'type': 'tool_output', 'content': 'file contents here',
             'tool_call_id': 'c1', 'ts': 102},
            # Agent responds
            {'type': 'final', 'content': 'Found the issue.', 'ts': 103},
            # User replies
            {'type': 'user', 'content': 'great', 'ts': 200},
        ]

        msgs = _reconstruct_llm_messages(entries)
        result = _apply_tail_start(msgs, has_summary=True)

        roles = [m['role'] for m in result]
        assert roles == ['assistant', 'tool', 'assistant', 'user']

    def test_no_summary_still_skips_leading_assistant(self):
        """Without summary, the old behavior is preserved."""
        entries = [
            {'type': 'final', 'content': 'old response', 'ts': 100},
            {'type': 'user', 'content': 'hello', 'ts': 200},
            {'type': 'final', 'content': 'hi', 'ts': 300},
        ]

        msgs = _reconstruct_llm_messages(entries)
        result = _apply_tail_start(msgs, has_summary=False)

        assert result[0]['role'] == 'user'
        assert result[0]['content'] == 'hello'
        assert len(result) == 2


class TestMissingToolResponses:
    """Tests for the case where agent interrupted before recording tool outputs."""

    def test_missing_tool_response_gets_placeholder(self):
        """An assistant(tool_calls) with no matching tool_output gets a synthetic error response."""
        entries = [
            {'type': 'user', 'content': 'do something', 'ts': 100},
            {'type': 'tool_call', 'function': 'bash', 'params': {'cmd': 'ls'}, 'id': 'tc1', 'ts': 200},
            # Agent crashed — no tool_output recorded
            {'type': 'user', 'content': 'still there?', 'ts': 300},
        ]

        msgs = _reconstruct_llm_messages(entries)
        # Should be: user, assistant(tool_calls:[tc1]), tool(tc1 placeholder), user
        roles = [m['role'] for m in msgs]
        assert roles == ['user', 'assistant', 'tool', 'user']
        asst = msgs[1]
        assert len(asst['tool_calls']) == 1
        assert asst['tool_calls'][0]['id'] == 'tc1'
        tool_resp = msgs[2]
        assert tool_resp['tool_call_id'] == 'tc1'
        assert 'interrupted' in tool_resp['content']

    def test_partial_tool_responses_get_placeholders(self):
        """When only some tool responses are present, placeholders fill the gaps."""
        entries = [
            {'type': 'user', 'content': 'do two things', 'ts': 100},
            {'type': 'tool_call', 'function': 'bash', 'params': {'cmd': 'a'}, 'id': 'tc1', 'ts': 200},
            {'type': 'tool_call', 'function': 'bash', 'params': {'cmd': 'b'}, 'id': 'tc2', 'ts': 201},
            {'type': 'tool_output', 'content': 'result a', 'tool_call_id': 'tc1', 'ts': 300},
            # tc2 output missing — agent died before recording it
            {'type': 'user', 'content': 'next', 'ts': 400},
        ]

        msgs = _reconstruct_llm_messages(entries)
        roles = [m['role'] for m in msgs]
        assert roles == ['user', 'assistant', 'tool', 'tool', 'user']
        tool_ids = {m['tool_call_id'] for m in msgs if m['role'] == 'tool'}
        assert tool_ids == {'tc1', 'tc2'}

    def test_complete_tool_responses_unchanged(self):
        """Normal case with all tool responses present is not affected."""
        entries = [
            {'type': 'user', 'content': 'go', 'ts': 100},
            {'type': 'tool_call', 'function': 'bash', 'params': {'cmd': 'x'}, 'id': 'tc1', 'ts': 200},
            {'type': 'tool_output', 'content': 'ok', 'tool_call_id': 'tc1', 'ts': 300},
            {'type': 'final', 'content': 'done', 'ts': 400},
        ]

        msgs = _reconstruct_llm_messages(entries)
        roles = [m['role'] for m in msgs]
        assert roles == ['user', 'assistant', 'tool', 'assistant']
        assert msgs[2]['content'] == 'ok'
