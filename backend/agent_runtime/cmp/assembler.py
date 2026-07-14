"""
CMP context assembler — segment-scoped history (the offload tier).

The single shared history builder used by BOTH the runtime's context
assembly and the prefetch warmup, so the two can never diverge. When CMP
is active with more than one path, the post-summary history window is
scoped to the ACTIVE path's transcript segments (with a bounded
rehydration tail from its earlier visits); dormant/archived paths are
represented only by their cards and the map. With a single path the
legacy full-history path is byte-identical (callers skip the filter).
"""
from __future__ import annotations

import logging

_logger = logging.getLogger(__name__)

# Rehydration tail: semantic messages kept from the active path's earlier
# (closed) segments when returning to it.
REHYDRATION_TAIL_MESSAGES = 6


def should_filter(cmp_state: dict) -> bool:
    """Filtering only matters once the session has more than one path."""
    return bool(cmp_state and len(cmp_state.get('paths') or {}) > 1
                and cmp_state.get('active_id') in cmp_state['paths'])


def build_history(chatlog, summary_record, cmp_state: dict) -> list:
    """Segment-scoped replacement for chatlog.get_entries_for_llm(after_ts=…).

    Returns reconstructed LLM messages for the active path only.
    """
    active = cmp_state['paths'][cmp_state['active_id']]
    after_ts = summary_record.get('last_message_ts') if summary_record else None
    return chatlog.get_entries_for_llm_segments(
        active.get('segments') or [],
        after_ts=after_ts,
        closed_tail_semantic=REHYDRATION_TAIL_MESSAGES,
    )
