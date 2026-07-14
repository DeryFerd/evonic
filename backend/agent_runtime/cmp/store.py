"""
CMP (Context Memory Path) — path store.

Pure functions over the `ms.cmp` dict (persisted inside session_state).
No I/O, no LLM calls. Each path record is a "path card" plus transcript
segments (chatlog ts ranges) plus a snapshot of the per-task AgentState
fields (mode/plan_file/auto_trivial/atg) — restoring these on switch is
what fixes the single-slot `ms.atg` limitation.
"""
from __future__ import annotations

import time

CMP_VERSION = 1
MAX_PATHS = 20
CMP_DORMANT_TURNS_K = 3

# Card field caps (interface-preserving but bounded)
TITLE_MAX = 60
GOAL_MAX = 300
OUTCOME_MAX = 300
KEY_FACTS_MAX = 6
KEY_FACT_CHARS = 200
ARTIFACTS_MAX = 8

VALID_STATUSES = {"active", "dormant", "archived"}

# AgentState fields snapshotted per path (the single-slot per-task state)
SNAPSHOT_FIELDS = ("mode", "plan_file", "auto_trivial", "atg")


def _now_ms() -> int:
    return int(time.time() * 1000)


def clamp_card_fields(card: dict) -> dict:
    """Enforce field caps in place; returns the card for chaining."""
    card["title"] = str(card.get("title") or "")[:TITLE_MAX]
    card["goal"] = str(card.get("goal") or "")[:GOAL_MAX]
    card["outcome"] = str(card.get("outcome") or "")[:OUTCOME_MAX]
    card["key_facts"] = [str(f)[:KEY_FACT_CHARS]
                         for f in (card.get("key_facts") or [])[:KEY_FACTS_MAX]]
    card["artifacts"] = [str(a)[:KEY_FACT_CHARS]
                         for a in (card.get("artifacts") or [])[:ARTIFACTS_MAX]]
    return card


def new_cmp(ms, title: str, goal: str = "", now_ts: int = None) -> dict:
    """Initialize the cmp dict with the first path, adopting the current
    AgentState per-task fields as that path's live state."""
    now_ts = now_ts if now_ts is not None else _now_ms()
    cmp = {
        "version": CMP_VERSION,
        "active_id": "P1",
        "next_id": 2,
        "paths": {
            "P1": _new_path_record("P1", title, goal, now_ts),
        },
        "stats": {"switches": 0, "branches": 0, "detector_llm_calls": 0},
    }
    return cmp


def _new_path_record(pid: str, title: str, goal: str, now_ts: int,
                     depends_on: list = None) -> dict:
    # Segments use exclusive-start semantics (after_ts < ts <= up_to_ts, the
    # chatlog range convention), cut at user_entry_ts - 1 so the triggering
    # user entry always belongs to the newly opened segment.
    return clamp_card_fields({
        "id": pid,
        "title": title,
        "status": "active",
        "goal": goal,
        "outcome": "",
        "key_facts": [],
        "artifacts": [],
        "depends_on": list(depends_on or []),
        "segments": [[now_ts - 1, None]],
        "last_active": now_ts,
        "dormant_turns": 0,
        "card_stale": True,
        # per-task AgentState snapshot; None while the path is active
        # (the live values are on ms) — filled on switch-away.
        "mode": None,
        "plan_file": None,
        "auto_trivial": False,
        "atg": None,
    })


def active_path(cmp: dict) -> dict:
    return cmp["paths"][cmp["active_id"]]


def valid_targets(cmp: dict) -> list:
    """[(id, title)] of switchable (non-active) paths, newest first."""
    out = [(p["id"], p["title"]) for p in cmp["paths"].values()
           if p["id"] != cmp["active_id"]]
    return sorted(out, key=lambda t: t[0], reverse=True)


def create_path(cmp: dict, ms, title: str, goal: str = "",
                depends_on: list = None, now_ts: int = None) -> dict:
    """Create a new path, switch to it (branch semantics: the new path
    starts a fresh plan cycle — mode plan, no plan_file, no atg).

    Returns the new path record. Raises ValueError on invalid depends_on.
    """
    depends_on = list(depends_on or [])
    unknown = [d for d in depends_on if d not in cmp["paths"]]
    if unknown:
        raise ValueError(f"Unknown dependency path(s): {unknown}. "
                         f"Valid: {sorted(cmp['paths'])}")
    now_ts = now_ts if now_ts is not None else _now_ms()

    _suspend_active(cmp, ms, now_ts)

    pid = f"P{cmp['next_id']}"
    cmp["next_id"] += 1
    record = _new_path_record(pid, title, goal, now_ts, depends_on=depends_on)
    cmp["paths"][pid] = record
    cmp["active_id"] = pid

    # Fresh plan cycle on ms (the maybe_rearm_atg mutations)
    ms.mode = "plan"
    ms.plan_file = None
    ms.auto_trivial = False
    ms.atg = None

    cmp["stats"]["branches"] = cmp["stats"].get("branches", 0) + 1
    enforce_caps(cmp)
    return record


def switch_to(cmp: dict, ms, target_id: str, now_ts: int = None) -> dict:
    """Atomic card-first switch: suspend active (close segment + snapshot ms
    fields), then activate target (restore snapshot + open segment).

    The caller is responsible for finalizing the outgoing card BEFORE
    calling this (compactor), per the paper's ordering. Raises ValueError
    on unknown/active target.
    """
    if target_id not in cmp["paths"]:
        raise ValueError(f"Unknown path '{target_id}'. Valid: "
                         + ", ".join(f"{i} ({t})" for i, t in valid_targets(cmp)))
    if target_id == cmp["active_id"]:
        raise ValueError(f"Path '{target_id}' is already active.")
    now_ts = now_ts if now_ts is not None else _now_ms()

    _suspend_active(cmp, ms, now_ts)

    target = cmp["paths"][target_id]
    # Restore the per-task AgentState fields
    ms.mode = target.get("mode") or "execute"
    ms.plan_file = target.get("plan_file")
    ms.auto_trivial = bool(target.get("auto_trivial"))
    ms.atg = target.get("atg")
    # Clear the stored snapshot while live (avoids stale double-truth)
    target["mode"] = None
    target["plan_file"] = None
    target["auto_trivial"] = False
    target["atg"] = None

    target["status"] = "active"
    target["dormant_turns"] = 0
    target["last_active"] = now_ts
    target["segments"].append([now_ts - 1, None])
    cmp["active_id"] = target_id

    cmp["stats"]["switches"] = cmp["stats"].get("switches", 0) + 1
    return target


def _suspend_active(cmp: dict, ms, now_ts: int) -> dict:
    """Close the active path's open segment and snapshot ms per-task fields."""
    path = active_path(cmp)
    _close_open_segment(path, now_ts)
    path["mode"] = ms.mode
    path["plan_file"] = ms.plan_file
    path["auto_trivial"] = ms.auto_trivial
    path["atg"] = ms.atg
    path["status"] = "dormant"
    path["dormant_turns"] = 0
    path["last_active"] = now_ts
    path["card_stale"] = True
    return path


def _close_open_segment(path: dict, now_ts: int) -> None:
    segments = path.get("segments") or []
    if segments and segments[-1][1] is None:
        # cut just before the incoming user entry (turn boundary — the user
        # entry that triggered the switch is already in the chatlog)
        segments[-1][1] = max(now_ts - 1, segments[-1][0])


def tick_hysteresis(cmp: dict) -> list:
    """Increment dormant counters; dormant paths reaching K become archived.
    Returns the list of newly archived path ids."""
    archived = []
    for path in cmp["paths"].values():
        if path["status"] == "dormant":
            path["dormant_turns"] = path.get("dormant_turns", 0) + 1
            if path["dormant_turns"] >= CMP_DORMANT_TURNS_K:
                path["status"] = "archived"
                archived.append(path["id"])
    if archived:
        enforce_caps(cmp)
    return archived


def enforce_caps(cmp: dict) -> None:
    """Prune oldest archived paths beyond MAX_PATHS to stub records
    (map node + transcript ref survive; atg snapshot dropped)."""
    # Archived paths always drop their atg snapshot (facts live in the card).
    for path in cmp["paths"].values():
        if path["status"] == "archived" and path.get("atg") is not None:
            path["atg"] = None

    if len(cmp["paths"]) <= MAX_PATHS:
        return
    archived = sorted(
        (p for p in cmp["paths"].values() if p["status"] == "archived"),
        key=lambda p: p.get("last_active", 0))
    to_prune = len(cmp["paths"]) - MAX_PATHS
    for path in archived[:to_prune]:
        stub = {k: path[k] for k in
                ("id", "title", "status", "outcome", "segments", "depends_on")}
        stub.update({"goal": "", "key_facts": [], "artifacts": [],
                     "last_active": path.get("last_active", 0),
                     "dormant_turns": 0, "card_stale": False,
                     "mode": None, "plan_file": None,
                     "auto_trivial": False, "atg": None})
        cmp["paths"][path["id"]] = stub


def dependency_ancestors(cmp: dict, path_id: str, max_depth: int = 2,
                         max_count: int = 4) -> list:
    """Transitive depends_on ancestors of a path (BFS, bounded)."""
    seen = []
    frontier = list(cmp["paths"].get(path_id, {}).get("depends_on") or [])
    depth = 0
    while frontier and depth < max_depth and len(seen) < max_count:
        next_frontier = []
        for dep in frontier:
            if dep in cmp["paths"] and dep not in seen and dep != path_id:
                seen.append(dep)
                if len(seen) >= max_count:
                    break
                next_frontier.extend(cmp["paths"][dep].get("depends_on") or [])
        frontier = next_frontier
        depth += 1
    return seen
