#!/usr/bin/env python3
"""
One-time migration: consolidate each agent's evomem `brain/` into `kb/`.

Old layout:  agents/<id>/brain/{.evomem.db, entities/, notes/, kb/ (mirror)}
New layout:  agents/<id>/kb/{.evomem.db, *.md, entities/, notes/}

For each agent that still has a brain/ dir:
  - move brain/entities -> kb/entities and brain/notes -> kb/notes (merge;
    existing kb/ copies win),
  - discard brain/kb (a stale mirror of kb/) and the old brain/.evomem.db,
  - delete the brain/ dir,
  - re-init + sync kb/ so kb/.evomem.db is rebuilt from the unified layout.

Idempotent: agents already migrated (no brain/) are skipped.

Usage (from the repo root):
    python scripts/migrate_brain_to_kb.py [agent_id ...]   # default: all agents
"""
import os
import sys
import shutil

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from backend.agent_runtime.evomem_client import sync as evomem_sync, is_available

AGENTS_DIR = "agents"


def _merge_move(src: str, dst: str) -> int:
    """Move entries from src into dst, keeping any pre-existing dst entries."""
    if not os.path.isdir(src):
        return 0
    os.makedirs(dst, exist_ok=True)
    moved = 0
    for name in os.listdir(src):
        s = os.path.join(src, name)
        d = os.path.join(dst, name)
        if os.path.exists(d):
            continue  # an existing kb/ copy wins
        shutil.move(s, d)
        moved += 1
    return moved


def migrate_agent(agent_id: str) -> bool:
    """Migrate one agent. Returns True if it had a brain/ dir to migrate."""
    brain = os.path.join(AGENTS_DIR, agent_id, "brain")
    kb = os.path.join(AGENTS_DIR, agent_id, "kb")
    if not os.path.isdir(brain):
        return False

    os.makedirs(kb, exist_ok=True)
    moved_e = _merge_move(os.path.join(brain, "entities"),
                          os.path.join(kb, "entities"))
    moved_n = _merge_move(os.path.join(brain, "notes"),
                          os.path.join(kb, "notes"))
    # Drop the whole brain/ (stale kb mirror + old index + leftovers).
    shutil.rmtree(brain, ignore_errors=True)
    # Rebuild the index inside kb/ (sync re-inits when the db is missing).
    ok = evomem_sync(agent_id) if is_available() else False
    print(f"  {agent_id}: moved entities+{moved_e} notes+{moved_n} synced={ok}")
    return True


def main(argv) -> int:
    if not os.path.isdir(AGENTS_DIR):
        print(f"No {AGENTS_DIR}/ directory here — run from the repo root.")
        return 1
    ids = argv or sorted(
        d for d in os.listdir(AGENTS_DIR)
        if os.path.isdir(os.path.join(AGENTS_DIR, d))
    )
    migrated = sum(1 for aid in ids if migrate_agent(aid))
    print(f"Migrated {migrated} agent(s); {len(ids) - migrated} already consolidated.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
