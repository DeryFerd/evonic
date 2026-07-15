"""
CMP boundary detector — fully LLM-led.

Every user message is routed by task_classifier.classify_boundary (4-class,
one small LLM call over the map + cards — never transcripts). Keyword-based
short-circuits proved unreliable twice in live use (generic word overlap
swallowing a new task; approval particles like 'oke'/'ya' swallowing a
return question), so there are none: situational knowledge that guards used
to encode is passed to the LLM as context instead — the active path's
plan-approval state, the just-delivered reply, and task-graph progress.

What stays deterministic is only what cannot be a judgment call:
  - empty messages (nothing to classify),
  - validating the LLM's target against the live graph,
  - defaulting to `continue` on any LLM failure or unparseable verdict
    (precision-first: a false branch severs context; a missed branch
    only costs tokens).
"""
from __future__ import annotations

import logging

_logger = logging.getLogger(__name__)

# A message this short can't express a new task subject → never switch on it
# (retry/ack/continuation). Explicit path-id mentions bypass this rail.
_SHORT_MSG_MAX_WORDS = 3


def _render_cards_for_llm(cmp: dict, ms=None, recent_tail: str = '') -> tuple:
    """(map_text, active_card, other_cards) compact text views for L3."""
    lines = []
    for pid in sorted(cmp['paths']):
        p = cmp['paths'][pid]
        marker = ' (ACTIVE)' if pid == cmp['active_id'] else f" ({p.get('status')})"
        lines.append(f"- {pid}: {p.get('title')}{marker} — {p.get('outcome') or p.get('goal') or ''}")
    map_text = '\n'.join(lines)

    def card_text(p):
        parts = [f"{p['id']}: {p.get('title')}",
                 f"goal: {p.get('goal') or ''}",
                 f"outcome: {p.get('outcome') or ''}"]
        parts.extend(p.get('key_facts') or [])
        if p.get('artifacts'):
            parts.append('artifacts: ' + ', '.join(p['artifacts']))
        return '\n'.join(parts)

    active = card_text(cmp['paths'][cmp['active_id']])
    # Situational context the removed guards used to encode — the LLM needs
    # it to route approval replies and detect finished-deliverable branches.
    if ms is not None:
        if ms.mode == 'plan':
            active += ("\nstate: this path's plan is AWAITING USER APPROVAL — "
                       "approval or consent replies mean CONTINUE")
        if isinstance(getattr(ms, 'atg', None), dict):
            atg_status = ms.atg.get('status')
            if atg_status in ('done', 'fallback', 'failed'):
                active += f"\nstate: this task's work is FINISHED (task graph {atg_status})"
            elif atg_status:
                active += f"\nstate: task graph {atg_status}"
    if recent_tail:
        active += f"\nlast assistant reply (what was just delivered): {recent_tail}"
    others = '\n\n'.join(card_text(p) for pid, p in sorted(cmp['paths'].items())
                         if pid != cmp['active_id'])
    return map_text, active, others or '(none)'


def detect(cmp: dict, ms, user_text: str, recent_tail: str = '') -> dict:
    """Classify a user turn. Returns {'decision', 'target', 'layer', 'reason'}.

    recent_tail: excerpt of the agent's latest reply — gives the classifier
    the just-delivered deliverable that raw (pre-switch) cards don't carry.
    Every decision — including `continue` — is logged with its reason, so
    'why didn't it branch?' is answerable from the log.
    """
    text = (user_text or '').strip()

    def _done(decision, target, layer, reason):
        _logger.info("CMP detect [%s]: %s%s — %s | active=%s | msg: %.80s",
                     layer, decision, f" -> {target}" if target else '',
                     reason, cmp.get('active_id'), text)
        return {'decision': decision, 'target': target, 'layer': layer,
                'reason': reason}

    if not text:
        return _done('continue', None, 'guard', 'empty message')

    # Safety rail (NOT topic matching): a very short message with no explicit
    # path-id cannot express a new task subject, so it can only be a retry /
    # acknowledgement / continuation of the active work ("coba lagi", "ok",
    # "lanjut", "ulangi"). Switching paths is a high-cost, silent action —
    # never justify it on a near-zero-signal message. This prevents an
    # interrupted-turn retry from being misread as a return to another path
    # (live: 'coba lagi' during an active B3 turn switched to A3).
    import re as _re
    _mentions_pid = _re.search(r'\b[A-Z]\d+\b', text)
    if len(text.split()) <= _SHORT_MSG_MAX_WORDS and not _mentions_pid:
        return _done('continue', None, 'guard',
                     f'short message (<= {_SHORT_MSG_MAX_WORDS} words), no path id — '
                     'insufficient signal to switch')

    from backend.task_classifier import classify_boundary
    map_text, active_card, other_cards = _render_cards_for_llm(cmp, ms, recent_tail)
    result = classify_boundary(map_text, active_card, other_cards, text)
    cmp.setdefault('stats', {})['detector_llm_calls'] = \
        cmp['stats'].get('detector_llm_calls', 0) + 1
    decision, target = result.get('decision'), result.get('target')

    # Validate targets against the live graph; anything off → continue.
    if decision == 'return' and (target not in cmp['paths']
                                 or target == cmp['active_id']):
        return _done('continue', None, 'LLM',
                     f'LLM said return:{target} but target is invalid/active')
    if decision == 'dep_branch' and target not in cmp['paths']:
        return _done('indep_branch', None, 'LLM',
                     f'LLM said dep_branch:{target} but target unknown — downgraded')
    return _done(decision, target, 'LLM', 'LLM verdict')
