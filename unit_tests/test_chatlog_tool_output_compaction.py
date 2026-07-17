"""Regression: history reconstruction must never re-inflate tool outputs.

The chatlog stores tool outputs RAW (for the UI detail view) while the live
turn sends a compressed/truncated version to the LLM. Reconstruction used to
inline the raw content verbatim, so one oversized output made the NEXT
turn's prompt explode (live: a 281k-char Explore result took a 16k-token
prompt to 339k and tripped the provider's context limit).
"""

from config import AGENT_MAX_TOOL_RESULT_CHARS
from models.chatlog import _reconstruct_llm_messages


def _entries(output_content):
    return [
        {'type': 'user', 'ts': 1, 'content': 'jalankan explorer'},
        {'type': 'tool_call', 'ts': 2, 'function': 'Explore',
         'params': {'query': 'x'}, 'id': 'c1'},
        {'type': 'tool_output', 'ts': 3, 'content': output_content,
         'tool_call_id': 'c1', 'function': 'Explore'},
        {'type': 'final', 'ts': 4, 'content': 'selesai'},
    ]


def _tool_messages(entries):
    return [m for m in _reconstruct_llm_messages(entries)
            if m.get('role') == 'tool']


def test_oversized_tool_output_truncated_on_reconstruction():
    # ~280k chars of prose (spaces/newlines — not base64-shaped), the size
    # class of the live Explore output that blew up the next turn
    huge = 'hasil scan: baris temuan panjang di file sumber\n' * 6000
    (tool_msg,) = _tool_messages(_entries(huge))
    content = tool_msg['content']
    # capped at the same ceiling the live turn uses, plus the truncation note
    assert len(content) <= AGENT_MAX_TOOL_RESULT_CHARS + 100
    assert '[truncated —' in content
    assert content.startswith('hasil scan:')  # head preserved


def test_small_tool_output_passes_through_untouched():
    (tool_msg,) = _tool_messages(_entries('ls output: ok'))
    assert tool_msg['content'] == 'ls output: ok'


def test_filter_resistant_blob_still_capped():
    # even content the base64 filter leaves alone must obey the hard cap
    blob = 'QUJDRA==' * 40_000  # ~320k chars, padding mid-string defeats the filter
    (tool_msg,) = _tool_messages(_entries(blob))
    assert len(tool_msg['content']) <= AGENT_MAX_TOOL_RESULT_CHARS + 100
