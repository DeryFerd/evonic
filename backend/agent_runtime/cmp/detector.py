"""
CMP boundary detector — cost-ordered, precision-first cascade.

Layers (each may short-circuit; cheaper first):
  L1  deterministic heuristics: approval/short-message guard (rule 0),
      explicit path-id mention, anaphora/continuation markers, and
      working-set overlap with the active card.
  L2  complexity gate: a message must be a COMPLEX task to branch at all
      (mirrors the ATG re-arm conservatism).
  L3  LLM 4-class classification (task_classifier.classify_boundary),
      biased to CONTINUE with a strict single-token parse.

The asymmetry driving the design: a false branch severs context the agent
needs (correctness); a missed branch merely delays compaction (tokens).
Any doubt or failure resolves to `continue`. The paper's L2 embedding
layer is future work — no embedding infrastructure exists in Evonic.
"""
from __future__ import annotations

import logging
import re

_logger = logging.getLogger(__name__)

_SHORT_MSG_MAX_WORDS = 6

_PATH_ID_RE = re.compile(r'\bP(\d+)\b')

# Conservative anaphora/continuation markers (EN + ID, incl. code-switched).
# These short-circuit to `continue` — over-matching only delays compaction,
# but determiners/relative pronouns ('that', 'this', 'itu', 'ini') are
# excluded: they are too common in NEW task descriptions and would suppress
# escalation entirely.
_CONTINUATION_MARKERS = {
    'it', 'again', 'tadi', 'tersebut',
    'lanjut', 'lanjutkan', 'continue', 'teruskan',
    'fix', 'perbaiki', 'betulkan', 'benerin',
    'masih', 'belum', 'kenapa', 'why', 'kok',
}

_TOKEN_RE = re.compile(r'[A-Za-z0-9_./-]{5,}')


def _working_set_tokens(active_path: dict) -> set:
    """Identifier-ish tokens from the active card (artifacts + key facts +
    title) used for lexical working-set overlap."""
    corpus = ' '.join(
        (active_path.get('artifacts') or [])
        + (active_path.get('key_facts') or [])
        + [active_path.get('title') or ''])
    return {t.lower() for t in _TOKEN_RE.findall(corpus)}


def _render_cards_for_llm(cmp: dict) -> tuple:
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
    others = '\n\n'.join(card_text(p) for pid, p in sorted(cmp['paths'].items())
                         if pid != cmp['active_id'])
    return map_text, active, others or '(none)'


def detect(cmp: dict, ms, user_text: str) -> dict:
    """Classify a user turn. Returns {'decision', 'target', 'layer'}."""
    text = (user_text or '').strip()
    if not text:
        return {'decision': 'continue', 'target': None, 'layer': 'L1'}
    lower = text.lower()
    words = lower.split()

    # L1 rule 0 — approval words in plan mode / very short follow-ups can
    # NEVER trigger a switch (guards the plan-approval flow).
    if len(words) <= _SHORT_MSG_MAX_WORDS:
        return {'decision': 'continue', 'target': None, 'layer': 'L1'}
    if ms is not None and ms.mode == 'plan' and _is_approval(text):
        return {'decision': 'continue', 'target': None, 'layer': 'L1'}

    # L1 — explicit mention of a known non-active path id is a deliberate
    # return signal ("lanjutkan yang P2 tadi").
    for match in _PATH_ID_RE.finditer(text):
        pid = f"P{match.group(1)}"
        if pid in cmp['paths'] and pid != cmp['active_id']:
            return {'decision': 'return', 'target': pid, 'layer': 'L1'}

    # L1 — anaphora / continuation markers.
    if set(words) & _CONTINUATION_MARKERS:
        return {'decision': 'continue', 'target': None, 'layer': 'L1'}

    # L1 — lexical working-set overlap with the active card.
    ws = _working_set_tokens(cmp['paths'][cmp['active_id']])
    if ws and any(tok.lower() in ws for tok in _TOKEN_RE.findall(text)):
        return {'decision': 'continue', 'target': None, 'layer': 'L1'}

    # L2 — only complex tasks may branch (trivial → stay in flow).
    from backend.task_classifier import classify_boundary, classify_task
    if classify_task(text) != 'complex':
        return {'decision': 'continue', 'target': None, 'layer': 'L2'}

    # L3 — LLM 4-class decision over cards (never transcripts).
    map_text, active_card, other_cards = _render_cards_for_llm(cmp)
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
