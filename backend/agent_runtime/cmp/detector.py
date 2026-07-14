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

# The approval guard only covers SHORT messages: genuine plan approvals are
# terse ("oke lanjutkan"). Longer sentences that merely contain an approval
# word ("oke, sip, btw yg issue kanban tadi udah solved kah?") are substantive
# and must reach the LLM — seen live swallowing a return-to-path question.
_APPROVAL_GUARD_MAX_WORDS = 8

_PATH_ID_RE = re.compile(r'\bP(\d+)\b')


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
    # Live progress hint: the active path's card is only finalized on switch,
    # so tell the classifier whether the current task already finished — a
    # finished task makes a complex new message far more likely to be a branch.
    if ms is not None and isinstance(getattr(ms, 'atg', None), dict):
        atg_status = ms.atg.get('status')
        if atg_status in ('done', 'fallback', 'failed'):
            active += f"\nstate: this task's work is FINISHED (task graph {atg_status})"
        elif atg_status:
            active += f"\nstate: task graph {atg_status}"
    # What the agent just delivered — a completed deliverable followed by a
    # new imperative is the classic branch signature the cards alone miss
    # (the active card is only finalized on switch).
    if recent_tail:
        active += f"\nlast assistant reply (what was just delivered): {recent_tail}"
    others = '\n\n'.join(card_text(p) for pid, p in sorted(cmp['paths'].items())
                         if pid != cmp['active_id'])
    return map_text, active, others or '(none)'


def detect(cmp: dict, ms, user_text: str, recent_tail: str = '') -> dict:
    """Classify a user turn. Returns {'decision', 'target', 'layer', 'reason'}.

    recent_tail: excerpt of the agent's latest reply — gives L3 the
    just-delivered deliverable that raw (pre-switch) cards don't carry.
    Every decision — including `continue` — is logged with its resolving
    layer and reason, so 'why didn't it branch?' is answerable from the log.
    """
    text = (user_text or '').strip()

    def _done(decision, target, layer, reason):
        _logger.info("CMP detect [%s]: %s%s — %s | active=%s | msg: %.80s",
                     layer, decision, f" -> {target}" if target else '',
                     reason, cmp.get('active_id'), text)
        return {'decision': decision, 'target': target, 'layer': layer,
                'reason': reason}

    if not text:
        return _done('continue', None, 'L1', 'empty message')

    # L1 guard — pure acknowledgements can never open/switch a task.
    if len(text.split()) <= _ACK_MAX_WORDS:
        return _done('continue', None, 'L1',
                     f'ack-length message (<= {_ACK_MAX_WORDS} words)')

    # L1 guard — short approval while the active path awaits plan approval:
    # "ok lanjutkan sesuai plan" must reach the approval check, never a switch.
    if (ms is not None and ms.mode == 'plan'
            and len(text.split()) <= _APPROVAL_GUARD_MAX_WORDS
            and _is_approval(text)):
        return _done('continue', None, 'L1', 'short approval message in plan mode')

    # L1 grounded reference — explicit mention of a known non-active path id
    # is a deliberate return signal ("lanjutkan yang P2 tadi").
    for match in _PATH_ID_RE.finditer(text):
        pid = f"P{match.group(1)}"
        if pid in cmp['paths'] and pid != cmp['active_id']:
            return _done('return', pid, 'L1', 'explicit path id mentioned')

    # L2 — only complex tasks may branch (trivial → stay in flow).
    from backend.task_classifier import classify_boundary, classify_task
    task_class = classify_task(text)
    if task_class != 'complex':
        return _done('continue', None, 'L2',
                     f'classify_task={task_class} (only complex tasks branch)')

    # L3 — LLM 4-class decision over cards (never transcripts).
    map_text, active_card, other_cards = _render_cards_for_llm(cmp, ms, recent_tail)
    result = classify_boundary(map_text, active_card, other_cards, text)
    cmp.setdefault('stats', {})['detector_llm_calls'] = \
        cmp['stats'].get('detector_llm_calls', 0) + 1
    decision, target = result.get('decision'), result.get('target')

    # Validate targets against the live graph; anything off → continue.
    if decision == 'return' and (target not in cmp['paths']
                                 or target == cmp['active_id']):
        return _done('continue', None, 'L3',
                     f'LLM said return:{target} but target is invalid/active')
    if decision == 'dep_branch' and target not in cmp['paths']:
        return _done('indep_branch', None, 'L3',
                     f'LLM said dep_branch:{target} but target unknown — downgraded')
    return _done(decision, target, 'L3', 'LLM verdict')


def _is_approval(text: str) -> bool:
    try:
        from backend.agent_runtime.runtime import AgentRuntime
        return AgentRuntime._is_approval(text)
    except Exception:
        return False
