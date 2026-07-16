"""End-to-end CMP flow over a real chatlog: init → branch → offload →
return → rehydration, driving on_turn_boundary + the shared assembler."""

import pytest


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """The single-pass turn op is a real LLM call — fail it fast so any
    unmocked detect() falls back mechanically (no network in tests)."""
    monkeypatch.setattr('backend.agent_runtime.cmp.detector._call_turn_llm',
                        lambda *a, **k: None)


from unittest.mock import patch

from backend.agent_runtime.cmp import assembler, on_turn_boundary
from backend.agent_state import AgentState
from models.chatlog import chatlog_manager

AGENT = {'id': 'cmp_e2e_agent', 'enable_cmp': 1, 'enable_agent_state': 1}
NEW_TASK = ('please build a completely different invoice generator project '
            'under /tmp/invoices with pdf export support')
RETURN_MSG = 'sekarang balik dulu ke pekerjaan website company profile yang sebelumnya'


def test_full_session_lifecycle():
    log = chatlog_manager.get('cmp_e2e_agent', 'sess-e2e')
    ms = AgentState(mode='execute')

    # ── Turn 1: first contact initializes P1 ────────────────────────────────
    log.append({'type': 'user', 'ts': 1000, 'content': 'build the company website',
                'session_id': 's'})
    result = on_turn_boundary(AGENT, ms, log, 'build the company website')
    assert result['decision'] == 'init'
    log.append({'type': 'final', 'ts': 1500, 'content': 'website scaffolded',
                'session_id': 's'})

    # ── Turn 2: independent branch → P2, P1 offloaded ───────────────────────
    log.append({'type': 'user', 'ts': 2000, 'content': NEW_TASK, 'session_id': 's'})
    with patch('backend.agent_runtime.cmp.detector.detect',
               return_value={'decision': 'indep_branch', 'target': None,
                             'layer': 'LLM',
                             'card_delta': {'new_facts': ['repo: /tmp/web'],
                                            'new_artifacts': ['/tmp/web']}}):
        result = on_turn_boundary(AGENT, ms, log, NEW_TASK)
    assert result['decision'] == 'indep_branch'
    p2 = result['target']
    assert ms.cmp['active_id'] == p2
    assert ms.mode == 'plan'  # fresh plan cycle (the re-arm)
    log.append({'type': 'final', 'ts': 2500, 'content': 'invoice plan drafted',
                'session_id': 's'})

    # offload: history now scoped to P2 only
    msgs = assembler.build_history(log, None, ms.cmp)
    text = ' '.join(str(m.get('content')) for m in msgs)
    assert 'invoice' in text
    assert 'build the company website' not in text
    # but P1's card (with key facts) is still rendered in the state message
    rendered = ms.render(cmp_enabled=True)
    assert 'Session Paths (CMP)' in rendered
    assert 'repo: /tmp/web' in rendered or 'website' in rendered

    # ── Turn 3: return to P1 → rehydration ──────────────────────────────────
    log.append({'type': 'user', 'ts': 3000, 'content': RETURN_MSG, 'session_id': 's'})
    with patch('backend.agent_runtime.cmp.detector.detect',
               return_value={'decision': 'return', 'target': 'A1',
                             'layer': 'LLM'}):
        result = on_turn_boundary(AGENT, ms, log, RETURN_MSG)
    assert result['decision'] == 'return'
    assert ms.cmp['active_id'] == 'A1'
    assert ms.mode == 'execute'  # P1's snapshot restored

    msgs = assembler.build_history(log, None, ms.cmp)
    text = ' '.join(str(m.get('content')) for m in msgs)
    assert RETURN_MSG in text                     # open segment
    assert 'website scaffolded' in text           # rehydration tail
    assert 'invoice plan drafted' not in text     # P2 offloaded

    # hysteresis bookkeeping ticked throughout
    assert ms.cmp['paths'][p2]['status'] == 'preserved'
    assert ms.cmp['stats']['switches'] >= 1
