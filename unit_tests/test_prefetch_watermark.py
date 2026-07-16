"""Regression tests for summary-watermark-aware prefetch invalidation (task #687)."""
from __future__ import annotations

import threading
import time

from backend.agent_runtime.prefetch import TurnPrefetcher, _PrefetchEntry


def _entry(session_id="s1", watermark=None, age=0.0):
    return _PrefetchEntry(
        session_id=session_id,
        agent_id="a1",
        messages=[],
        tools=[],
        system_prompt="sp",
        agent_context={},
        summary_watermark=watermark,
        timestamp=time.time() - age,
    )


# -- try_get watermark invariants ------------------------------------

def test_unchanged_watermark_hit():
    pf = TurnPrefetcher()
    pf._cache["s1"] = _entry(watermark=1000)
    result = pf.try_get("s1", summary_watermark=1000)
    assert result is not None
    assert result.summary_watermark == 1000


def test_advanced_watermark_evicts():
    pf = TurnPrefetcher()
    pf._cache["s1"] = _entry(watermark=1000)
    result = pf.try_get("s1", summary_watermark=2000)
    assert result is None
    assert "s1" not in pf._cache


def test_watermark_removed_evicts():
    pf = TurnPrefetcher()
    pf._cache["s1"] = _entry(watermark=1000)
    result = pf.try_get("s1", summary_watermark=None)
    assert result is None
    assert "s1" not in pf._cache


def test_watermark_newly_created_evicts():
    pf = TurnPrefetcher()
    pf._cache["s1"] = _entry(watermark=None)
    result = pf.try_get("s1", summary_watermark=1000)
    assert result is None
    assert "s1" not in pf._cache


def test_both_none_hit():
    pf = TurnPrefetcher()
    pf._cache["s1"] = _entry(watermark=None)
    result = pf.try_get("s1", summary_watermark=None)
    assert result is not None


def test_missing_entry_returns_none():
    pf = TurnPrefetcher()
    result = pf.try_get("no-such-session", summary_watermark=100)
    assert result is None


def test_ttl_expired_evicts():
    pf = TurnPrefetcher()
    pf._cache["s1"] = _entry(watermark=500, age=60)
    result = pf.try_get("s1", summary_watermark=500)
    assert result is None
    assert "s1" not in pf._cache


def test_agent_id_mismatch_still_hit():
    """try_get does NOT filter by agent_id -- that check is in the runtime caller."""
    pf = TurnPrefetcher()
    e = _entry(watermark=1000)
    e.agent_id = "other-agent"
    pf._cache["s1"] = e
    result = pf.try_get("s1", summary_watermark=1000)
    assert result is not None


# -- _PrefetchEntry construction -------------------------------------

def test_watermark_stored():
    e = _PrefetchEntry(
        session_id="s1", agent_id="a1",
        messages=[], tools=[], system_prompt="sp",
        agent_context={}, summary_watermark=12345,
    )
    assert e.summary_watermark == 12345


def test_default_watermark_is_none():
    e = _PrefetchEntry(
        session_id="s1", agent_id="a1",
        messages=[], tools=[], system_prompt="sp",
        agent_context={},
    )
    assert e.summary_watermark is None


# -- thread safety ---------------------------------------------------

def test_concurrent_try_get_no_crash():
    pf = TurnPrefetcher()
    for i in range(50):
        pf._cache[f"s{i}"] = _entry(watermark=i)

    errors = []

    def worker(start, count):
        for i in range(start, start + count):
            try:
                pf.try_get(f"s{i}", summary_watermark=i)
            except Exception as exc:
                errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=(s, 10))
        for s in range(0, 50, 10)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
