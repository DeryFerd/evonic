"""
CMP boundary detector — LLM-led, with minimal grounded guards.

The LLM (task_classifier.classify_boundary, 4-class) is the primary
decision-maker: keyword heuristics for topic decisions proved unreliable
in live use (false continues from generic word overlap), so L1 keeps only
deterministic rules that are grounded or safety-critical, never topical:

  L1  guards: empty/ack-length messages, approval words while a plan is
      awaiting approval (a switch here would corrupt the approval flow),
      and an explicit non-active path-id mention ("P2") which is a
      grounded return reference, not a guess.
  L2  complexity gate (classify_task, itself LLM-backed when uncertain):
      trivial messages never branch — the paper's granularity policy;
      not every small request becomes a path.
  L3  classify_boundary over the map + cards (never transcripts), biased
      to CONTINUE with a strict single-token parse.

The asymmetry still applies: a false branch severs context (correctness),
a missed branch costs tokens. Any doubt or failure resolves to `continue`.
"""
from __future__ import annotations

import logging
import re

_logger = logging.getLogger(__name__)

# Pure acknowledgements ("ok", "ya", "thanks") — never a new task.
_ACK_MAX_WORDS = 3

_PATH_ID_RE = re.compile(r'\bP(\d+)\b')


def _render_cards_for_llm(cmp: dict, ms=None) -> tuple:
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
    # Live progress hint: the active path's card is only finalized on switch,
    # so tell the classifier whether the current task already finished — a
    # finished task makes a complex new message far more likely to be a branch.
    if ms is not None and isinstance(getattr(ms, 'atg', None), dict):
        atg_status = ms.atg.get('status')
        if atg_status in ('done', 'fallback', 'failed'):
            active += f"\nstate: this task's work is FINISHED (task graph {atg_status})"
        elif atg_status:
            active += f"\nstate: task graph {atg_status}"
    others = '\n\n'.join(card_text(p) for pid, p in sorted(cmp['paths'].items())
                         if pid != cmp['active_id'])
    return map_text, active, others or '(none)'


def detect(cmp: dict, ms, user_text: str) -> dict:
    """Classify a user turn. Returns {'decision', 'target', 'layer'}."""
    text = (user_text or '').strip()
    if not text:
        return {'decision': 'continue', 'target': None, 'layer': 'L1'}

    # L1 guard — pure acknowledgements can never open/switch a task.
    if len(text.split()) <= _ACK_MAX_WORDS:
        return {'decision': 'continue', 'target': None, 'layer': 'L1'}

    # L1 guard — approval words while the active path awaits plan approval:
    # "ok lanjutkan sesuai plan" must reach the approval check, never a switch.
    if ms is not None and ms.mode == 'plan' and _is_approval(text):
        return {'decision': 'continue', 'target': None, 'layer': 'L1'}

    # L1 grounded reference — explicit mention of a known non-active path id
    # is a deliberate return signal ("lanjutkan yang P2 tadi").
    for match in _PATH_ID_RE.finditer(text):
        pid = f"P{match.group(1)}"
        if pid in cmp['paths'] and pid != cmp['active_id']:
            return {'decision': 'return', 'target': pid, 'layer': 'L1'}

    # L2 — only complex tasks may branch (trivial → stay in flow).
    from backend.task_classifier import classify_boundary, classify_task
    if classify_task(text) != 'complex':
        return {'decision': 'continue', 'target': None, 'layer': 'L2'}

    # L3 — LLM 4-class decision over cards (never transcripts).
    map_text, active_card, other_cards = _render_cards_for_llm(cmp, ms)
    result = classify_boundary(map_text, active_card, other_cards, text)
    cmp.setdefault('stats', {})['detector_llm_calls'] = \
        cmp['stats'].get('detector_llm_calls', 0) + 1
    decision, target = result.get('decision'), result.get('target')

    # Validate targets against the live graph; anything off → continue.
    if decision == 'return' and (target not in cmp['paths']
                                 or target == cmp['active_id']):
        decision, target = 'continue', None
    if decision == 'dep_branch' and target not in cmp['paths']:
        decision, target = 'indep_branch', None
    return {'decision': decision, 'target': target, 'layer': 'L3'}


def _is_approval(text: str) -> bool:
    try:
        from backend.agent_runtime.runtime import AgentRuntime
        return AgentRuntime._is_approval(text)
    except Exception:
        return False
