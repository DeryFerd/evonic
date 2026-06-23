"""
evomem_writer.py — write structured markdown into an agent's evomem.

Evomem treats disk as the source of truth: pages are markdown files, the
database is derived via `sync`. The CLI `capture` command only writes flat,
unlinked notes to inbox/ — which leaves the knowledge graph empty. This module
writes *structured* pages instead:

- entity pages (entities/<slug>.md) with frontmatter + a `## Relationships`
  section of typed blockquote edges,
- note/fact pages (notes/<slug>.md) with `[[entities/...]]` wiki-links,

then schedules a debounced `sync` so the graph (typed edges) is built off the
hot path. All writes are atomic and best-effort; any failure is swallowed so
the FTS5 memory pipeline is never affected.

Typed edges recognised by evomem (from a page body):
- explicit blockquote: `> **works_at:** [Acme](entities/acme)` — edge_type is
  the lowercase label, deterministic.
- `[[entities/slug]]` wiki-link — creates a `mentions` edge.

Slugs are source_dir-prefixed (e.g. `entities/robin`, `notes/x`); link targets
must include the prefix.
"""

import os
import re
import logging
import threading
import unicodedata
from datetime import datetime, timezone

from backend.agent_runtime.evomem_client import (
    _get_brain_dir, init_evomem, sync as _evomem_sync, vlog,
)

logger = logging.getLogger(__name__)

# Debounce window (seconds) for coalescing a burst of writes into one sync.
_SYNC_DEBOUNCE_SECONDS = float(os.environ.get("EVOMEM_SYNC_DEBOUNCE", "2"))

# Edge types evomem understands (others fall back to a plain mention).
EDGE_TYPES = {"founded", "invested_in", "works_at", "advises", "attended", "mentions"}

# Per-agent debounced-sync timers, guarded by a lock.
_sync_timers: dict = {}
_sync_lock = threading.Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def slugify(name: str) -> str:
    """Deterministic slug from a name: lowercase ascii, dashes, capped length.

    Same input always yields the same slug, so an entity maps to a stable file
    (dedup by construction). Returns '' if nothing usable remains.
    """
    if not name:
        return ""
    norm = unicodedata.normalize("NFKD", name)
    norm = norm.encode("ascii", "ignore").decode("ascii")
    norm = norm.lower()
    norm = re.sub(r"[^a-z0-9]+", "-", norm)
    norm = norm.strip("-")
    return norm[:60].strip("-")


def _brain_path(agent_id: str, source_dir: str, slug: str) -> str:
    """Absolute path to a page file under the agent's brain dir."""
    bare = slug.split("/", 1)[-1]  # strip any source_dir prefix
    return os.path.abspath(os.path.join(_get_brain_dir(agent_id), source_dir, f"{bare}.md"))


def _ensure_brain(agent_id: str) -> bool:
    """Make sure the brain DB exists (idempotent). Returns False if unavailable."""
    brain_dir = _get_brain_dir(agent_id)
    if os.path.isdir(brain_dir) and os.path.exists(os.path.join(brain_dir, ".evomem.db")):
        return True
    return init_evomem(agent_id)


def _atomic_write(path: str, content: str) -> None:
    """Write content atomically (temp file in same dir + os.replace)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, path)


def _yaml_escape(s: str) -> str:
    """Quote a scalar for YAML frontmatter (double-quoted, escaped)."""
    s = (s or "").replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
    return f'"{s}"'


def _yaml_list(items) -> str:
    """Render a flow-style YAML list of scalars."""
    return "[" + ", ".join(_yaml_escape(str(i)) for i in items) + "]"


def _parse_frontmatter(text: str):
    """Split a markdown doc into (frontmatter_dict, body).

    Minimal YAML: scalar and flow-list values only. Unknown structure is kept
    as a raw string so we never lose data on rewrite.
    """
    fm, body = {}, text
    m = re.match(r"^---\n(.*?)\n---\n?(.*)$", text, re.DOTALL)
    if not m:
        return fm, text
    raw, body = m.group(1), m.group(2)
    for line in raw.splitlines():
        if not line.strip() or ":" not in line:
            continue
        key, _, val = line.partition(":")
        key, val = key.strip(), val.strip()
        if val.startswith("[") and val.endswith("]"):
            inner = val[1:-1].strip()
            items = []
            if inner:
                items = [p.strip().strip('"').strip("'") for p in inner.split(",")]
            fm[key] = [i for i in items if i]
        else:
            fm[key] = val.strip('"').strip("'")
    return fm, body


def _render_entity(fm: dict, body: str) -> str:
    lines = ["---"]
    lines.append(f"title: {_yaml_escape(fm.get('title', ''))}")
    lines.append(f"type: {fm.get('type', 'entity')}")
    lines.append(f"tags: {_yaml_list(fm.get('tags', []))}")
    lines.append(f"aliases: {_yaml_list(fm.get('aliases', []))}")
    lines.append(f"created: {fm.get('created', _now_iso())}")
    lines.append("---")
    return "\n".join(lines) + "\n" + body.lstrip("\n")


def upsert_entity_page(agent_id: str, name: str, entity_type: str = "entity",
                       aliases=None, tags=None, summary=None) -> str:
    """Create or merge an entity page. Returns its slug ('entities/<slug>') or ''.

    If the page exists, aliases/tags are merged into existing frontmatter
    (union) instead of clobbering — this is the dedup mechanism.
    """
    slug = slugify(name)
    if not slug or not _ensure_brain(agent_id):
        return ""
    full_slug = f"entities/{slug}"
    path = _brain_path(agent_id, "entities", slug)
    aliases = [a for a in (aliases or []) if a and a != name]
    tags = list(tags or [])

    try:
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                fm, body = _parse_frontmatter(f.read())
            fm.setdefault("title", name)
            fm["type"] = fm.get("type") or entity_type
            fm["aliases"] = sorted(set(fm.get("aliases", []) or []) | set(aliases))
            fm["tags"] = sorted(set(fm.get("tags", []) or []) | set(tags) | {"entity"})
            _atomic_write(path, _render_entity(fm, body))
            vlog("writer[%s]: entity merge %s (aliases=%d)",
                 agent_id, full_slug, len(fm["aliases"]))
        else:
            fm = {
                "title": name,
                "type": entity_type,
                "tags": sorted(set(tags) | {"entity"}),
                "aliases": sorted(set(aliases)),
                "created": _now_iso(),
            }
            body = f"\n{summary or name}.\n"
            _atomic_write(path, _render_entity(fm, body))
            vlog("writer[%s]: entity create %s", agent_id, full_slug)
        return full_slug
    except Exception as e:
        logger.debug("upsert_entity_page failed for %s/%s: %s", agent_id, slug, e)
        return ""


def add_edge(agent_id: str, subject_slug: str, edge_type: str,
             object_slug: str, anchor: str = None) -> bool:
    """Append a typed blockquote edge to the subject entity page (idempotent).

    `subject_slug`/`object_slug` are full slugs ('entities/...'). Unknown edge
    types fall back to 'mentions'. Returns True if the edge is present after the
    call.
    """
    edge_type = edge_type if edge_type in EDGE_TYPES else "mentions"
    subj_bare = subject_slug.split("/", 1)[-1]
    path = _brain_path(agent_id, "entities", subj_bare)
    if not os.path.exists(path):
        # Subject must exist as an entity page to host the edge.
        if not upsert_entity_page(agent_id, subj_bare.replace("-", " ")):
            return False
    anchor = anchor or object_slug.split("/", 1)[-1].replace("-", " ")
    edge_line = f"> **{edge_type}:** [{anchor}]({object_slug})"
    try:
        with open(path, encoding="utf-8") as f:
            content = f.read()
        if edge_line in content:
            vlog("writer[%s]: edge exists %s --%s--> %s",
                 agent_id, subject_slug, edge_type, object_slug)
            return True
        if "## Relationships" in content:
            content = content.rstrip() + "\n" + edge_line + "\n"
        else:
            content = content.rstrip() + "\n\n## Relationships\n" + edge_line + "\n"
        _atomic_write(path, content)
        vlog("writer[%s]: edge add %s --%s--> %s",
             agent_id, subject_slug, edge_type, object_slug)
        return True
    except Exception as e:
        logger.debug("add_edge failed for %s (%s): %s", agent_id, subject_slug, e)
        return False


def write_note(agent_id: str, title: str, body: str, tags=None,
               mentions=None, memory_id=None, source: str = None) -> str:
    """Write a note/fact page with [[wiki-link]] mentions. Returns slug or ''.

    `memory_id` is recorded in frontmatter so re-runs upsert the same file
    (idempotent backfill). `mentions` is a list of full entity slugs appended
    as `[[entities/...]]` so the note wires `mentions` edges and is graph-adjacent.
    """
    # Stable slug: prefer memory id for idempotency, else derive from title.
    base = f"mem-{memory_id}" if memory_id is not None else slugify(title)
    if not base or not _ensure_brain(agent_id):
        return ""
    path = _brain_path(agent_id, "notes", base)

    mention_links = ""
    for m in (mentions or []):
        if m:
            mention_links += f"\n[[{m}]]"

    fm = ["---", f"title: {_yaml_escape(title)}", "type: note",
          f"tags: {_yaml_list(tags or [])}", f"created: {_now_iso()}"]
    if memory_id is not None:
        fm.append(f"memory_id: {int(memory_id)}")
    if source:
        fm.append(f"source: {_yaml_escape(source)}")
    fm.append("---")
    doc = "\n".join(fm) + "\n\n" + body.strip() + mention_links + "\n"
    try:
        _atomic_write(path, doc)
        vlog("writer[%s]: note write notes/%s (mentions=%d)",
             agent_id, base, len(mentions or []))
        return f"notes/{base}"
    except Exception as e:
        logger.debug("write_note failed for %s: %s", agent_id, e)
        return ""


def delete_note(agent_id: str, memory_id) -> bool:
    """Remove a note page (by memory id) from disk so the next sync soft-deletes
    it in evomem. Returns True if a file was actually removed.

    Pure (does not schedule a sync) — mirror of write_note; the caller marks
    the brain dirty. Entity pages and edges are left intact (they are shared
    across facts).
    """
    if memory_id is None:
        return False
    path = _brain_path(agent_id, "notes", f"mem-{memory_id}")
    try:
        if os.path.exists(path):
            os.remove(path)
            vlog("writer[%s]: note delete notes/mem-%s", agent_id, memory_id)
            return True
        return False
    except Exception as e:
        logger.debug("delete_note failed for %s mem-%s: %s", agent_id, memory_id, e)
        return False


def _do_sync(agent_id: str) -> None:
    with _sync_lock:
        _sync_timers.pop(agent_id, None)
    logger.info("evomem_writer[%s]: debounced sync firing", agent_id)
    try:
        ok = _evomem_sync(agent_id)
        logger.info("evomem_writer[%s]: sync completed (ok=%s)", agent_id, ok)
    except Exception as e:
        logger.warning("evomem_writer[%s]: debounced sync failed: %s", agent_id, e)


def mark_dirty(agent_id: str) -> None:
    """Schedule a debounced background sync for the agent, coalescing bursts."""
    with _sync_lock:
        existing = _sync_timers.get(agent_id)
        if existing is not None:
            existing.cancel()
        timer = threading.Timer(_SYNC_DEBOUNCE_SECONDS, _do_sync, args=(agent_id,))
        timer.daemon = True
        _sync_timers[agent_id] = timer
        timer.start()
    logger.info("evomem_writer[%s]: sync scheduled in %.1fs%s",
                agent_id, _SYNC_DEBOUNCE_SECONDS,
                " (coalesced)" if existing is not None else "")


def sync_now(agent_id: str) -> bool:
    """Synchronous sync (used by the backfill script). Cancels any pending timer."""
    with _sync_lock:
        existing = _sync_timers.pop(agent_id, None)
        if existing is not None:
            existing.cancel()
    vlog("writer[%s]: sync now (synchronous)", agent_id)
    try:
        return _evomem_sync(agent_id)
    except Exception:
        return False
