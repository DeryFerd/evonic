#!/usr/bin/env python3
"""
rebuild_evomem.py — backfill existing long-term memories into a structured,
graph-enabled evomem brain.

For each agent it:
  1. inits the brain (idempotent),
  2. writes every memory (memories table) as a structured `notes/` page, linked
     to the canonical `entities/user` for user-scoped facts,
  3. optionally (--with-graph) runs LLM entity/relation extraction to materialize
     entity pages + typed edges,
  4. runs a single `sync`,
  5. prints brain stats before/after (links 0 -> N confirms the graph built).

Idempotent: notes are keyed by `memory_id`, entities by deterministic slug, so
re-running upserts rather than duplicates.

Usage:
    python3 scripts/rebuild_evomem.py --all [--with-graph]
    python3 scripts/rebuild_evomem.py --agent siwa [--with-graph]
"""

import os
import sys
import argparse
import threading

# Ensure repo root on path when run directly.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.db import db
from backend.agent_runtime import evomem_writer as W
from backend.agent_runtime.evomem_client import (
    is_available, init_evomem, _run, _get_brain_dir,
)
from backend.agent_runtime.memory_manager import _USER_SCOPED, _extract_and_store_graph


def _stats(agent_id: str) -> dict:
    brain_dir = _get_brain_dir(agent_id)
    if not os.path.exists(os.path.join(brain_dir, ".evomem.db")):
        return {}
    return _run(brain_dir, ["stats"]) or {}


def _fmt_stats(s: dict) -> str:
    if not s:
        return "(no brain)"
    return (f"pages={s.get('pages', 0)} chunks={s.get('chunks', 0)} "
            f"links={s.get('links', 0)} dangling={s.get('dangling_links', 0)}")


def rebuild_agent(agent_id: str, with_graph: bool) -> None:
    before = _stats(agent_id)
    if not init_evomem(agent_id):
        print(f"  [{agent_id}] could not init brain — skipping")
        return

    memories = db.get_all_memories(agent_id)
    if not memories:
        print(f"  [{agent_id}] no memories — skipping")
        return

    # Make sure the canonical user entity exists for [[entities/user]] links.
    W.upsert_entity_page(agent_id, "User", entity_type="person", tags=["user"])

    written = 0
    for m in memories:
        content = (m.get("content") or "").strip()
        if not content:
            continue
        category = m.get("category") or "general"
        mentions = ["entities/user"] if category in _USER_SCOPED else None
        slug = W.write_note(
            agent_id,
            title=f"{category}: {content[:70]}",
            body=content,
            tags=[category],
            mentions=mentions,
            memory_id=m.get("id"),
            source=m.get("session_id"),
        )
        if slug:
            written += 1

    if with_graph:
        # Build one summary from all memories and extract entities/relations.
        summary = "\n".join(f"- {m.get('content', '')}" for m in memories
                            if m.get("content"))
        _extract_and_store_graph(agent_id, summary, threading.Lock())

    # One synchronous sync at the end (cancels any pending debounced timer).
    ok = W.sync_now(agent_id)
    after = _stats(agent_id)
    print(f"  [{agent_id}] wrote {written} notes, sync={'ok' if ok else 'FAILED'}")
    print(f"      before: {_fmt_stats(before)}")
    print(f"      after:  {_fmt_stats(after)}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill memories into evomem.")
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--agent", help="Rebuild a single agent by id.")
    g.add_argument("--all", action="store_true", help="Rebuild every agent.")
    parser.add_argument("--with-graph", action="store_true",
                        help="Run LLM entity/relation extraction to build typed edges.")
    args = parser.parse_args()

    if not is_available():
        print("evomem binary not available — aborting.")
        return 1

    if args.agent:
        agent_ids = [args.agent]
    else:
        agent_ids = [a["id"] for a in db.get_agents()]

    print(f"Rebuilding {len(agent_ids)} agent(s), with_graph={args.with_graph}")
    for aid in agent_ids:
        rebuild_agent(aid, args.with_graph)
    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
