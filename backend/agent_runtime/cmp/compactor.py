"""
CMP compactor — interface-preserving path cards.

Single-shot card generation at path switch (MVP; incremental drafting is
future work). The card schema is fixed and field-capped; the LLM is
constrained to fill it, never to summarize freely. Deterministic facts are
lifted from ATG execution state (node records including failure causes)
independent of LLM quality, and a mechanical fallback card guarantees that
card generation NEVER blocks a path switch.
"""
from __future__ import annotations

import json
import logging
import re

from backend.agent_runtime.cmp import prompts
from backend.agent_runtime.cmp.store import clamp_card_fields

_logger = logging.getLogger(__name__)

_TRANSCRIPT_MAX_CHARS = 8000
_JSON_RE = re.compile(r'\{.*\}', re.DOTALL)

# Mutating tools whose path-like args count as artifacts.
_ARTIFACT_TOOLS = {'write_file', 'str_replace', 'patch', 'delete_file'}
_ARTIFACT_ARG_KEYS = ('file_path', 'path')


def collect_path_entries(chatlog, path: dict) -> list:
    """All chatlog entries belonging to the path's segments (ts ranges)."""
    entries = []
    for start, end in path.get('segments') or []:
        try:
            if end is None:
                entries.extend(chatlog.get_entries_after_ts(start))
            else:
                entries.extend(chatlog.get_entries_between_ts(start, end))
        except Exception:
            _logger.exception("CMP: failed reading chatlog segment [%s, %s]",
                              start, end)
    return entries


def _format_transcript(entries: list) -> str:
    from backend.agent_runtime.summarizer import _format_entries_for_summary
    text = _format_entries_for_summary(entries)
    if len(text) > _TRANSCRIPT_MAX_CHARS:
        # tail-biased: the latest turns matter most for resuming
        text = "…[earlier turns omitted]\n" + text[-_TRANSCRIPT_MAX_CHARS:]
    return text


def lift_atg_facts(atg_state) -> tuple:
    """Deterministic (key_facts, artifacts, goal) from ATG execution state —
    independent of LLM summarization quality."""
    facts, artifacts = [], []
    goal = ""
    if not isinstance(atg_state, dict):
        return facts, artifacts, goal
    dag = atg_state.get('dag') or {}
    goal = dag.get('root_goal') or atg_state.get('root_goal') or ""
    status = atg_state.get('status')
    if status:
        facts.append(f"task graph status: {status}")
    for nid in sorted(dag.get('nodes') or {}):
        node = dag['nodes'][nid]
        record = node.get('record') or {}
        if node.get('status') == 'failed' and record.get('error'):
            facts.append(f"step {nid} ({str(node.get('goal', ''))[:40]}) "
                         f"failed: {str(record['error'])[:120]}")
        if node.get('tool') in _ARTIFACT_TOOLS:
            args = record.get('resolved_args') or node.get('args_template') or {}
            for key in _ARTIFACT_ARG_KEYS:
                value = args.get(key)
                if isinstance(value, str) and value and value not in artifacts:
                    artifacts.append(value)
    return facts, artifacts, goal


def _format_atg_block(atg_state) -> str:
    if not isinstance(atg_state, dict):
        return "(none)"
    dag = atg_state.get('dag') or {}
    lines = [f"status: {atg_state.get('status')}",
             f"goal: {dag.get('root_goal') or atg_state.get('root_goal') or ''}"]
    for nid in sorted(dag.get('nodes') or {}):
        node = dag['nodes'][nid]
        record = node.get('record') or {}
        detail = record.get('error') or (record.get('output_excerpt') or '')[:80]
        lines.append(f"- {nid} [{node.get('status')}] {node.get('tool') or '?'}: "
                     f"{str(node.get('goal', ''))[:60]} — {detail}")
    return "\n".join(lines)


def _mechanical_card(path: dict, entries: list, atg_state) -> dict:
    """Deterministic fallback card — no LLM involved."""
    first_user = next((e.get('content', '') for e in entries
                       if e.get('type') == 'user'), '')
    last_final = next((e.get('content', '') for e in reversed(entries)
                       if e.get('type') in ('final', 'intermediate')), '')
    facts, artifacts, goal = lift_atg_facts(atg_state)
    return {
        'title': path.get('title') or str(first_user)[:60],
        'action': path.get('action') or '',
        'goal': path.get('goal') or goal or str(first_user)[:300],
        'outcome': str(last_final)[:300],
        'key_facts': facts,
        'artifacts': artifacts,
    }


def generate_card(chatlog, path: dict, atg_state=None) -> dict:
    """Produce card fields for a path. NEVER raises; always returns a dict
    with the fixed schema (LLM-filled or mechanical fallback), with
    deterministic ATG facts merged in either way."""
    entries = []
    try:
        entries = collect_path_entries(chatlog, path)
        transcript = _format_transcript(entries)
        if not transcript.strip():
            return clamp_card_fields(_mechanical_card(path, entries, atg_state))

        from backend.task_classifier import _get_classifier_client
        client = _get_classifier_client('cmp_model_id')
        import time as _time
        _t0 = _time.time()
        response = client.chat_completion(
            [{"role": "system", "content": prompts.CARD_SYSTEM},
             {"role": "user", "content": prompts.CARD_USER.format(
                 transcript=transcript,
                 atg_block=_format_atg_block(atg_state))}],
            tools=None, temperature=0.0, enable_thinking=False,
            max_tokens=600,
        )
        if not response.get('success'):
            raise ValueError(response.get('error_type') or 'LLM call failed')
        msg = (response.get('response', {}).get('choices') or [{}])[0].get('message', {})
        content = (msg.get('content') or msg.get('reasoning_content') or '').strip()
        m = _JSON_RE.search(content)
        card = json.loads(m.group(0)) if m else None
        if not isinstance(card, dict):
            raise ValueError('no JSON object in card response')

        # Merge deterministic ATG facts (never lost to a weak summary)
        atg_facts, atg_artifacts, atg_goal = lift_atg_facts(atg_state)
        facts = list(card.get('key_facts') or [])
        for fact in atg_facts:
            if fact not in facts:
                facts.append(fact)
        card['key_facts'] = facts
        arts = list(card.get('artifacts') or [])
        for art in atg_artifacts:
            if art not in arts:
                arts.append(art)
        card['artifacts'] = arts
        if not card.get('goal'):
            card['goal'] = path.get('goal') or atg_goal
        if not card.get('title'):
            card['title'] = path.get('title')
        _logger.info("CMP card generated for %s (model=%s, %.1fs, %d facts, %d artifacts)",
                     path.get('id'), getattr(client, 'model', None),
                     _time.time() - _t0, len(card['key_facts']), len(card['artifacts']))
        return clamp_card_fields(card)
    except Exception as e:
        _logger.warning("CMP card generation fell back to mechanical: %s", e)
        return clamp_card_fields(_mechanical_card(path, entries, atg_state))


def finalize_active_card(chatlog, cmp: dict, ms) -> None:
    """Refresh the ACTIVE path's card before it is suspended (switch/branch).
    Uses the live ms.atg (not yet snapshotted). Never raises."""
    try:
        path = cmp['paths'][cmp['active_id']]
        if not path.get('card_stale', True):
            return
        card = generate_card(chatlog, path, atg_state=ms.atg)
        for key in ('title', 'action', 'goal', 'outcome', 'key_facts', 'artifacts'):
            if card.get(key):
                path[key] = card[key]
        path['card_stale'] = False
    except Exception:
        _logger.exception("CMP finalize_active_card failed — switch proceeds")
