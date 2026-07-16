"""Tests for the turn-concurrency gates — especially the parent↔same-model
sub-agent deadlock mitigation (paused_model_gate)."""

import threading
import time

from backend.agent_runtime.concurrency import (
    ConcurrencyGate,
    ConcurrencyManager,
    paused_model_gate,
    _tls,
)


def test_gate_basic_capacity():
    g = ConcurrencyGate(1, name='m')
    g.acquire()
    assert g.is_at_capacity()
    g.release()
    assert not g.is_at_capacity()


def test_gate_zero_is_unlimited():
    g = ConcurrencyGate(0, name='m')
    for _ in range(50):
        g.acquire()
    assert not g.is_at_capacity()


def test_paused_model_gate_noop_without_tls():
    _tls.model_gate = None
    with paused_model_gate():   # must not raise
        pass


def test_parent_child_same_model_no_deadlock():
    """The reported scenario: parent holds the max-1 model gate and blocks in a
    sync sub-agent tool; the child needs the SAME gate. paused_model_gate must
    let the child run, then the parent re-acquires. Without the pause this
    deadlocks forever."""
    gate = ConcurrencyGate(1, name='model:evomodel')
    child_ran = threading.Event()
    child_done = threading.Event()

    # Parent turn acquires the model gate and stashes it (as turn_gate does).
    gate.acquire()
    _tls.model_gate = gate

    def child():
        gate.acquire()          # needs the SAME max-1 gate the parent holds
        child_ran.set()
        time.sleep(0.05)        # simulate the child's LLM work
        gate.release()
        child_done.set()

    t = threading.Thread(target=child)

    with paused_model_gate():   # parent releases its permit while "blocking"
        t.start()
        # If the pause works, the child acquires and runs within the window.
        assert child_ran.wait(timeout=5), "child never acquired the gate — DEADLOCK"
        assert child_done.wait(timeout=5)

    t.join(timeout=5)
    # Parent re-acquired on exit; balanced accounting.
    assert gate.capacity_details == {'active': 1, 'max': 1}
    gate.release()
    _tls.model_gate = None


def test_manager_explorer_gets_own_agent_gate_but_shared_model_gate():
    """A sub-agent shares the parent's MODEL gate (same model_id) but has its
    own AGENT gate — so only the model gate can contend, which paused_model_gate
    covers."""
    mgr = ConcurrencyManager()
    parent_model = mgr._get_model_gate('evomodel')
    child_model = mgr._get_model_gate('evomodel')
    assert parent_model is child_model          # same model → same gate
    parent_agent = mgr._get_agent_gate('aisyah')
    child_agent = mgr._get_agent_gate('aisyah_explorer_1')
    assert parent_agent is not child_agent      # distinct agent gates
