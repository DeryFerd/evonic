"""
CMP context assembler — segment-scoped history (the offload tier).

The single shared history builder used by BOTH the runtime's context
assembly and the prefetch warmup, so the two can never diverge. When CMP
is active with more than one path, the post-summary history window loads:

  - the ACTIVE path's transcript segments (open segment in full, earlier
    visits trimmed to a rehydration tail), and
  - its DEPENDENCY ANCESTORS' transcripts (bounded tail each) — a child
    task keeps its parents' detail in memory because it consumes their
    results (e.g. invoice B2 needs report A1 loaded, not just its card).

Everything else — siblings, unrelated paths — is represented only by IPPC
cards and the map. With a single path the legacy full-history path is
byte-identical (callers skip the filter).
"""
from __future__ import annotations

import logging

from backend.agent_runtime.cmp.store import dependency_ancestors

_logger = logging.getLogger(__name__)

# Rehydration tail: semantic messages kept from the active path's earlier
# (closed) segments when returning to it.
REHYDRATION_TAIL_MESSAGES = 6
# Bounded per-ancestor transcript tail (ancestors bounded by
# dependency_ancestors: depth <= 2, max 4 paths).
ANCESTOR_TAIL_MESSAGES = 4


def should_filter(cmp_state: dict) -> bool:
    """Filtering only matters once the session has more than one path."""
    return bool(cmp_state and len(cmp_state.get('paths') or {}) > 1
                and cmp_state.get('active_id') in cmp_state['paths'])


def build_history(chatlog, summary_record, cmp_state: dict) -> list:
    """Segment-scoped replacement for chatlog.get_entries_for_llm(after_ts=…).

    Returns reconstructed LLM messages for the active path plus bounded
    tails of its dependency ancestors.
    """
    active_id = cmp_state['active_id']
    active = cmp_state['paths'][active_id]
    after_ts = summary_record.get('last_message_ts') if summary_record else None
    ancestor_groups = [
        (cmp_state['paths'][aid].get('segments') or [], ANCESTOR_TAIL_MESSAGES)
        for aid in dependency_ancestors(cmp_state, active_id)
        if aid in cmp_state['paths']
    ]
    return chatlog.get_entries_for_llm_segments(
        active.get('segments') or [],
        after_ts=after_ts,
        closed_tail_semantic=REHYDRATION_TAIL_MESSAGES,
        extra_groups=ancestor_groups or None,
    )
