"""
CMP single-pass turn detector — fully LLM-led routing plus card upkeep.

ONE LLM call per turn returns a structured op envelope: the 4-class boundary
route, a delta for the ACTIVE path's card (outcome/new facts/new artifacts)
and, on branches, the new path's name — replacing the former three separate
calls (boundary classification, switch-time card generation, path naming).
The envelope is applied by deterministic code (store.apply_card_delta and
the orchestrator); the LLM never rewrites established node ids, titles,
actions or edges. Keyword-based short-circuits proved unreliable twice in
live use (generic word overlap swallowing a new task; approval particles
like 'oke'/'ya' swallowing a return question), so there are none: situational
knowledge that guards used to encode is passed to the LLM as context instead
— the active path's plan-approval state, the just-delivered reply, and
task-graph progress.

What stays deterministic is only what cannot be a judgment call:
  - empty messages (nothing to classify),
  - validating the LLM's target against the live graph,
  - defaulting to `continue` on any LLM failure or unparseable verdict
    (precision-first: a false branch severs context; a missed branch
    only costs tokens).
"""
from __future__ import annotations

import logging
import re

from backend.agent_runtime.cmp import prompts

_logger = logging.getLogger(__name__)

# A message this short can't express a new task subject → never switch on it
# (retry/ack/continuation). Explicit path-id mentions bypass this rail.
# Kept at 2: three-word messages routinely carry a subject in Indonesian
# ("balik ke laporan", "cek invoice Intan") and must reach the LLM.
_SHORT_MSG_MAX_WORDS = 2

_ROUTES = {'continue', 'return', 'dep_branch', 'indep_branch'}

# Tolerate models fusing the target into the route ("return:A1").
_ROUTE_TARGET_RE = re.compile(r'^(return|dep_branch)[:\s]+([A-Za-z]+\d+)$')

_PID_RE = re.compile(r'\b[A-Z]+\d+\b')


def _render_cards_for_llm(cmp: dict, ms=None, recent_tail: str = '') -> tuple:
    """(map_text, active_card, other_cards) compact text views for the LLM.
    Prompt cost follows the lifecycle tiers: archived paths are title-only
    in the map and contribute no card. Map lines carry the path's
    parentage ("builds on A1") so the LLM can pick correct return /
    dep_branch targets and resolve "back to the parent"."""
    from backend.agent_runtime.cmp.store import path_status
    lines = []
    for pid in sorted(cmp['paths']):
        p = cmp['paths'][pid]
        deps = p.get('depends_on') or []
        dep_note = f", builds on {'+'.join(deps)}" if deps else ''
        if pid == cmp['active_id']:
            marker = f" (ACTIVE{dep_note})"
        elif path_status(p) == 'archived':
            lines.append(f"- {pid}: {p.get('title')} (archived{dep_note})")
            continue
        else:
            marker = f" ({path_status(p)}{dep_note})"
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
                         if pid != cmp['active_id']
                         and path_status(p) != 'archived')
    return map_text, active, others or '(none)'


def _parse_envelope(content: str) -> dict | None:
    """Normalize the LLM's JSON op envelope. Returns None when no usable
    object is present (caller falls back to continue).

    A truncated tail (reasoning burned the token budget mid-object) is
    repaired: the envelope orders route/target/new_path first precisely so
    they survive truncation — losing the card delta costs a turn of card
    freshness, losing the route would mis-file the whole turn."""
    from backend.agent_runtime.llm_json import (complete_truncated_json,
                                                extract_first_json)
    env = extract_first_json(content)
    # No dict, or a dict without a route (a truncated envelope's first
    # COMPLETE object is a nested one, e.g. new_path) → try tail repair.
    if not isinstance(env, dict) or 'route' not in env:
        env = complete_truncated_json(content) or env
    if not isinstance(env, dict):
        return None
    route = str(env.get('route') or '').strip().lower()
    target = str(env.get('target') or '').strip().upper() or None
    fused = _ROUTE_TARGET_RE.match(route)
    if fused:
        route, target = fused.group(1), fused.group(2).upper()
    if route not in _ROUTES:
        return None
    new_path = env.get('new_path') if isinstance(env.get('new_path'), dict) else None
    card = env.get('card') if isinstance(env.get('card'), dict) else None
    return {'route': route, 'target': target, 'new_path': new_path,
            'card_delta': card}


def _call_turn_llm(cmp: dict, ms, text: str, recent_tail: str,
                   recent_dialogue: str, initializing: bool) -> dict | None:
    """The single per-turn LLM call. Returns the normalized envelope, or
    None on any failure (call error, no JSON, unknown route)."""
    from backend.task_classifier import _get_classifier_client, classifier_chat
    map_text, active_card, other_cards = _render_cards_for_llm(cmp, ms, recent_tail)
    dialogue_block = (f"## Recent conversation (for context)\n{recent_dialogue}\n\n"
                      if recent_dialogue else "")
    user_prompt = prompts.TURN_USER.format(
        map_text=map_text, active_card=active_card, other_cards=other_cards,
        dialogue_block=dialogue_block, user_text=text[:4000],
        init_note=prompts.TURN_INIT_NOTE if initializing else "")
    client = _get_classifier_client('cmp_model_id')
    messages = [{"role": "system", "content": prompts.TURN_SYSTEM},
                {"role": "user", "content": user_prompt}]
    import time as _time
    _t0 = _time.time()
    content = ''
    # Two budgets: reasoning-heavy models sometimes burn the whole budget on
    # implicit CoT and cut the envelope mid-object (finish_reason=length with
    # partial content — passes through as success). Repair usually recovers
    # the route; when even repair fails, one retry at a doubled budget.
    for budget in (1024, 2048):
        response = classifier_chat(client, messages, max_tokens=budget,
                                   log_label="CMP turn",
                                   source="cmp", archive_category="turn")
        _dur = _time.time() - _t0
        if not response.get('success'):
            _logger.warning("CMP turn LLM call failed [%s] (model=%s, %.1fs) — "
                            "defaulting to continue",
                            response.get('error_type'),
                            getattr(client, 'model', None), _dur)
            return None
        choice = (response.get('response', {}).get('choices') or [{}])[0]
        msg = choice.get('message', {})
        content = (msg.get('content') or msg.get('reasoning_content') or '').strip()
        env = _parse_envelope(content)
        if env is not None:
            _logger.info("CMP turn verdict: %s%s (model=%s, %.1fs, delta=%s, named=%s)",
                         env['route'], f" -> {env['target']}" if env['target'] else '',
                         getattr(client, 'model', None), _dur,
                         bool(env['card_delta']), bool(env['new_path']))
            return env
        if choice.get('finish_reason') != 'length' or budget != 1024:
            break
        _logger.info("CMP turn envelope truncated beyond repair at "
                     "max_tokens=%d — retrying with a doubled budget", budget)
    _logger.warning("CMP turn envelope unparseable (model=%s, %.1fs) — "
                    "defaulting to continue. Raw: %.120s",
                    getattr(client, 'model', None), _time.time() - _t0,
                    content or '(empty)')
    return None


def detect(cmp: dict, ms, user_text: str, recent_tail: str = '',
           recent_dialogue: str = '', initializing: bool = False) -> dict:
    """Classify a user turn in one LLM pass. Returns {'decision', 'target',
    'layer', 'reason', 'new_path', 'card_delta'}.

    recent_tail: excerpt of the agent's latest reply — the just-delivered
    deliverable that raw (pre-switch) cards don't carry; also the substance
    for the card delta.
    recent_dialogue: the last few user↔agent turns, so terse messages
    ('coba lagi', 'yang itu aja') are grounded in what just happened.
    initializing: first message of the session — routing is moot (the only
    path was just created from this message); the call names it instead.
    Every decision — including `continue` — is logged with its reason, so
    'why didn't it branch?' is answerable from the log.
    """
    text = (user_text or '').strip()

    def _done(decision, target, layer, reason, new_path=None, card_delta=None):
        _logger.info("CMP detect [%s]: %s%s — %s | active=%s | msg: %.80s",
                     layer, decision, f" -> {target}" if target else '',
                     reason, cmp.get('active_id'), text)
        return {'decision': decision, 'target': target, 'layer': layer,
                'reason': reason, 'new_path': new_path,
                'card_delta': card_delta}

    if not text:
        return _done('continue', None, 'guard', 'empty message')

    # Safety rail (NOT topic matching): a message this short with no explicit
    # path-id cannot express a new task subject, so it can only be a retry /
    # acknowledgement / continuation of the active work ("coba lagi", "ok",
    # "lanjut", "ulangi ya"). Switching paths is a high-cost, silent action —
    # never justify it on a near-zero-signal message. This prevents an
    # interrupted-turn retry from being misread as a return to another path
    # (live: 'coba lagi' during an active B3 turn switched to A3). Longer
    # messages — including 3-word switch commands like "balik ke laporan" —
    # go to the LLM.
    if (len(text.split()) <= _SHORT_MSG_MAX_WORDS
            and not _PID_RE.search(text) and not initializing):
        return _done('continue', None, 'guard',
                     f'short message (<= {_SHORT_MSG_MAX_WORDS} words), no path id — '
                     'insufficient signal to switch')

    try:
        env = _call_turn_llm(cmp, ms, text, recent_tail, recent_dialogue,
                             initializing)
    except Exception:
        _logger.warning("CMP turn call raised — defaulting to continue",
                        exc_info=True)
        env = None
    cmp.setdefault('stats', {})['detector_llm_calls'] = \
        cmp['stats'].get('detector_llm_calls', 0) + 1
    if env is None:
        return _done('continue', None, 'LLM', 'LLM failure/unparseable — default')

    decision, target = env['route'], env['target']
    new_path, card_delta = env['new_path'], env['card_delta']
    if initializing:
        # Routing is moot on the first message; only the naming is used.
        return _done('continue', None, 'LLM', 'init naming pass',
                     new_path=new_path, card_delta=None)

    # Validate targets against the live graph; anything off → continue.
    # The card delta stays valid either way (it describes the active path).
    if decision == 'return' and (target not in cmp['paths']
                                 or target == cmp['active_id']):
        return _done('continue', None, 'LLM',
                     f'LLM said return:{target} but target is invalid/active',
                     card_delta=card_delta)
    if decision == 'dep_branch' and target not in cmp['paths']:
        return _done('indep_branch', None, 'LLM',
                     f'LLM said dep_branch:{target} but target unknown — downgraded',
                     new_path=new_path, card_delta=card_delta)
    return _done(decision, target, 'LLM', 'LLM verdict',
                 new_path=new_path, card_delta=card_delta)
