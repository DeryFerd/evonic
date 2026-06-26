#!/usr/bin/env python3
"""
rebuild_evomem.py — clean-migrate an agent's evomem brain to the doc model.

The knowledge base is now a flat Obsidian-style vault: every markdown file is a
DOC with inline ``[[wiki-links]]``; there is no ``entities/`` or ``notes/``
directory, and the graph is rebuilt from the inline links by ``sync``.

For each agent it:
  1. removes the legacy ``entities/`` and ``notes/`` directories (the old
     stub-entity / per-fact note model),
  2. deletes the index databases (``.evomem.db``, legacy ``.evobrain.db``) so the
     schema is recreated fresh (page→doc rename),
  3. re-inits the brain and runs a single ``sync`` to rebuild the graph from the
     remaining inline-linked docs,
  4. prints brain stats before/after.

Existing top-level KB docs are preserved. Safe to re-run (idempotent).

Usage:
    python3 scripts/rebuild_evomem.py --all
    python3 scripts/rebuild_evomem.py --agent siwa
"""

import os
import re
import sys
import shutil
import argparse

# Ensure repo root on path when run directly.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.db import db
from backend.agent_runtime import evomem_writer as W
from backend.agent_runtime.evomem_client import (
    is_available, init_evomem, _run, _get_evomem_dir,
)

_LEGACY_DIRS = ("entities", "notes")
_DB_FILES = (".evomem.db", ".evomem.db-wal", ".evomem.db-shm", ".evobrain.db")


def _stats(agent_id: str) -> dict:
    brain_dir = _get_evomem_dir(agent_id)
    if not os.path.exists(os.path.join(brain_dir, ".evomem.db")):
        return {}
    return _run(brain_dir, ["stats"]) or {}


def _fmt_stats(s: dict) -> str:
    if not s:
        return "(no brain)"
    return (f"docs={s.get('docs', 0)} chunks={s.get('chunks', 0)} "
            f"links={s.get('links', 0)} dangling={s.get('dangling_links', 0)}")


def migrate_agent(agent_id: str) -> None:
    before = _stats(agent_id)
    brain_dir = _get_evomem_dir(agent_id)
    if not os.path.isdir(brain_dir):
        print(f"  [{agent_id}] no kb dir — skipping")
        return

    # 1) Drop legacy type-based dirs.
    removed_dirs = []
    for d in _LEGACY_DIRS:
        p = os.path.join(brain_dir, d)
        if os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)
            removed_dirs.append(d)

    # 2) Fix mis-typed standalone docs: session/group are collection markers
    #    (a folder + index.md created via create_collection), so a flat top-level
    #    `<slug>.md` carrying type session|group is a bad type guess, not a real
    #    collection. Coerce its frontmatter type to `note` in place.
    retyped = []
    for fn in sorted(os.listdir(brain_dir)):
        if not fn.endswith(".md") or fn.startswith("."):
            continue
        src = os.path.join(brain_dir, fn)
        if not os.path.isfile(src):
            continue
        try:
            with open(src, encoding="utf-8") as fh:
                raw = fh.read()
            fm, _ = W._parse_frontmatter(raw)
        except OSError:
            continue
        if (fm.get("type") or "").strip() not in ("session", "group"):
            continue
        new_raw = re.sub(r'(?m)^(type:\s*)["\']?(session|group)["\']?\s*$',
                         r'\1note', raw, count=1)
        if new_raw != raw:
            with open(src, "w", encoding="utf-8") as fh:
                fh.write(new_raw)
            retyped.append(fn[:-3])

    # 3) Delete index DBs so the new (doc) schema is created fresh.
    for f in _DB_FILES:
        p = os.path.join(brain_dir, f)
        try:
            if os.path.exists(p):
                os.remove(p)
        except OSError:
            pass

    # 4) Re-init + rebuild the graph from the remaining inline-linked docs.
    if not init_evomem(agent_id):
        print(f"  [{agent_id}] could not init brain — skipping")
        return
    ok = W.sync_now(agent_id)
    after = _stats(agent_id)
    print(f"  [{agent_id}] removed={removed_dirs or '-'} "
          f"retyped={retyped or '-'} sync={'ok' if ok else 'FAILED'}")
    print(f"      before: {_fmt_stats(before)}")
    print(f"      after:  {_fmt_stats(after)}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Clean-migrate evomem brains to the doc model.")
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--agent", help="Migrate a single agent by id.")
    g.add_argument("--all", action="store_true", help="Migrate every agent.")
    args = parser.parse_args()

    if not is_available():
        print("evomem binary not available — aborting.")
        return 1

    if args.agent:
        agent_ids = [args.agent]
    else:
        agent_ids = [a["id"] for a in db.get_agents()]

    print(f"Migrating {len(agent_ids)} agent(s) to the doc model…")
    for aid in agent_ids:
        migrate_agent(aid)
    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
