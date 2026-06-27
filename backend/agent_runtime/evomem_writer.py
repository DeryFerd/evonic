"""
evomem_writer.py — write structured markdown docs into an agent's evomem.

Evomem treats disk as the source of truth: docs are markdown files, the database
is derived via `sync`. This module writes *rich, inline-linked* docs (the
Obsidian/wiki model) and schedules a debounced `sync` so the graph is built off
the hot path.

Model
-----
- A **doc** is one markdown file with frontmatter (`title`, `type`,
  `description`, optional `tags`/`aliases`, `created`/`updated`) and a body of
  rich prose. Relationships are expressed as **inline** `[[Doc Title]]`
  wiki-links woven into the sentences — never a separate "Relations" list.
- Docs live at the knowledge root or inside a **collection** folder, one level
  under kb/ (e.g. `kb/riset-xyz/`), created on the user's request. Each collection
  has an `index.md` of `type: session` or `type: group` describing it.
- There is no `entities/` directory: an entity like "Jakarta" is just a doc with
  `type: place`. Links resolve to a doc by title/alias anywhere in the vault, so
  callers link by display title, not by path.

All writes are atomic and best-effort; failures are swallowed so the FTS5 memory
pipeline is never affected.
"""

from __future__ import annotations

import os
import re
import logging
import threading
import unicodedata
from datetime import datetime, timezone

from backend.agent_runtime.evomem_client import (
    _get_evomem_dir, init_evomem, sync as _evomem_sync, vlog,
)

logger = logging.getLogger(__name__)

# Debounce window (seconds) for coalescing a burst of writes into one sync.
_SYNC_DEBOUNCE_SECONDS = float(os.environ.get("EVOMEM_SYNC_DEBOUNCE", "2"))

# Typed edge labels evomem can carry on a link (used to populate the `recall`
# graph-traversal edge_type filter). The engine infers these from the sentence
# around an inline link; any other label is stored as a custom edge type.
EDGE_TYPES = {
    "founded", "invested_in", "works_at", "advises", "attended",
    "located_in", "lives_in", "visited", "born_in", "part_of",
    "member_of", "owns", "uses", "knows", "related_to", "mentions",
}

# Doc types accepted by evomem's validator (mirrors Rust validate::VALID_TYPES).
DOC_TYPES = {
    "note", "session", "group", "person", "place", "venue", "event",
    "organization", "company", "product", "contact",
}

# Per-agent debounced-sync timers, guarded by a lock.
_sync_timers: dict = {}
_sync_lock = threading.Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def slugify(name: str) -> str:
    """Deterministic slug from a name: lowercase ascii, dashes, capped length.

    Same input always yields the same slug, so a doc maps to a stable file
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


def _doc_path(agent_id: str, rel_slug: str) -> str:
    """Absolute path to a doc file under the agent's brain dir.

    ``rel_slug`` is a knowledge-root-relative slug that may include folder
    segments (e.g. ``riset-xyz/foo`` -> ``kb/riset-xyz/foo.md``).
    """
    rel = (rel_slug or "").strip("/").replace("..", "")
    return os.path.abspath(os.path.join(_get_evomem_dir(agent_id), f"{rel}.md"))


def _ensure_brain(agent_id: str) -> bool:
    """Make sure the brain DB exists (idempotent). Returns False if unavailable."""
    brain_dir = _get_evomem_dir(agent_id)
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


def _render_doc(title: str, doc_type: str, description: str, tags, aliases,
                created: str, updated: str, body: str) -> str:
    """Render a full doc (frontmatter + body). Body is used verbatim (its inline
    [[wiki-links]] are the graph edges)."""
    lines = ["---",
             f"title: {_yaml_escape(title)}",
             f"type: {doc_type if doc_type in DOC_TYPES else 'note'}",
             f"description: {_yaml_escape(description)}",
             f"tags: {_yaml_list(tags or [])}"]
    if aliases:
        lines.append(f"aliases: {_yaml_list(aliases)}")
    lines.append(f"created: {created}")
    lines.append(f"updated: {updated}")
    lines.append("---")
    return "\n".join(lines) + "\n\n" + (body or "").strip() + "\n"


def upsert_doc(agent_id: str, title: str, body: str, doc_type: str = "note",
               description: str = None, folder: str = "", tags=None,
               aliases=None, slug: str = None) -> str:
    """Create or overwrite a doc. Returns its full slug (``<folder>/<slug>``) or ''.

    ``folder`` places the doc inside a collection (e.g. ``riset-xyz``); empty =
    root. ``body`` is rich prose with inline ``[[Doc Title]]`` links — written
    verbatim, with no appended link block. On an existing doc the original
    ``created`` is kept, ``updated`` is refreshed, and tags/aliases are merged
    (union); the caller supplies the already-merged body.

    ``session``/``group`` are reserved for collection ``index.md`` files (created
    via :func:`create_collection`); a standalone doc must not carry them, so they
    are coerced to ``note`` here — otherwise an LLM type guess could mint a flat
    "session" file that looks like a collection but isn't one.
    """
    if doc_type in ("session", "group"):
        doc_type = "note"
    base = (slug or slugify(title)).strip("/")
    if not base or not _ensure_brain(agent_id):
        return ""
    folder = (folder or "").strip("/")
    rel = f"{folder}/{base}" if folder else base
    path = _doc_path(agent_id, rel)
    tags = list(tags or [])
    aliases = [a for a in (aliases or []) if a and a != title]
    created = _now_iso()

    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                fm, _old = _parse_frontmatter(f.read())
            created = fm.get("created") or created
            tags = sorted(set(tags) | set(fm.get("tags", []) or []))
            aliases = sorted(set(aliases) | set(fm.get("aliases", []) or []))
            if description is None:
                description = fm.get("description")
            # Don't downgrade a typed doc to a plain note on re-write — but never
            # adopt a stale session/group type from a mis-typed flat file.
            existing_type = fm.get("type")
            if (not doc_type or doc_type == "note") and existing_type \
                    and existing_type not in ("session", "group"):
                doc_type = existing_type
        except Exception:
            pass
    description = (description or title).strip()

    try:
        _atomic_write(path, _render_doc(title, doc_type, description, tags,
                                        aliases, created, _now_iso(), body))
        vlog("writer[%s]: upsert_doc %s (type=%s)", agent_id, rel, doc_type)
        return rel
    except Exception as e:
        logger.debug("upsert_doc failed for %s/%s: %s", agent_id, rel, e)
        return ""


def append_to_doc(agent_id: str, slug: str, delta_prose: str) -> bool:
    """Append a new inline-linked paragraph to an existing doc, preserving the
    body and refreshing ``updated``. Returns True if written.
    """
    delta = (delta_prose or "").strip()
    if not delta:
        return False
    path = _doc_path(agent_id, slug)
    if not os.path.exists(path):
        return False
    try:
        with open(path, encoding="utf-8") as f:
            fm, body = _parse_frontmatter(f.read())
        title = fm.get("title") or slug.rsplit("/", 1)[-1]
        doc_type = fm.get("type") or "note"
        description = fm.get("description") or title
        created = fm.get("created") or _now_iso()
        new_body = body.rstrip() + "\n\n" + delta
        _atomic_write(path, _render_doc(title, doc_type, description,
                                        fm.get("tags", []) or [],
                                        fm.get("aliases", []) or [],
                                        created, _now_iso(), new_body))
        vlog("writer[%s]: append_to_doc %s (+%d chars)", agent_id, slug, len(delta))
        return True
    except Exception as e:
        logger.debug("append_to_doc failed for %s/%s: %s", agent_id, slug, e)
        return False


def create_collection(agent_id: str, folder: str, title: str,
                     kind: str = "session", description: str = None) -> str:
    """Create a collection folder (one level under kb/) with an ``index.md`` of
    ``type: session|group``. Idempotent. Returns the folder slug or ''.
    """
    folder = slugify(folder)
    if not folder or kind not in ("session", "group") or not _ensure_brain(agent_id):
        return ""
    path = _doc_path(agent_id, f"{folder}/index")
    description = (description or title).strip()
    if os.path.exists(path):
        return folder  # idempotent: keep existing index
    body = f"{description}\n\n## Contents\n"
    try:
        _atomic_write(path, _render_doc(title, kind, description, [kind], [],
                                        _now_iso(), _now_iso(), body))
        vlog("writer[%s]: create_collection %s (%s)", agent_id, folder, kind)
        return folder
    except Exception as e:
        logger.debug("create_collection failed for %s/%s: %s", agent_id, folder, e)
        return ""


def add_to_collection_index(agent_id: str, folder: str, doc_title: str) -> bool:
    """Append ``- [[doc_title]]`` under the collection index's ``## Contents``
    section (idempotent — skips if the link is already present)."""
    folder = (folder or "").strip("/")
    doc_title = (doc_title or "").strip()
    if not folder or not doc_title:
        return False
    path = _doc_path(agent_id, f"{folder}/index")
    if not os.path.exists(path):
        return False
    link = f"[[{doc_title}]]"
    try:
        with open(path, encoding="utf-8") as f:
            content = f.read()
        if link in content:
            return False
        if "## Contents" in content:
            content = content.rstrip() + f"\n- {link}\n"
        else:
            content = content.rstrip() + f"\n\n## Contents\n- {link}\n"
        _atomic_write(path, content)
        return True
    except Exception as e:
        logger.debug("add_to_collection_index failed for %s/%s: %s", agent_id, folder, e)
        return False


def read_doc(agent_id: str, slug: str) -> dict | None:
    """Read a doc: returns ``{title, body, frontmatter}`` or None if missing.
    Used by the authoring pipeline for dedupe/merge decisions."""
    path = _doc_path(agent_id, slug)
    try:
        with open(path, encoding="utf-8") as f:
            raw = f.read()
    except (FileNotFoundError, OSError):
        return None
    fm, body = _parse_frontmatter(raw)
    return {"title": fm.get("title", ""), "body": body.strip(), "frontmatter": fm}


def rename_doc(agent_id: str, old_slug: str, new_title: str, add_alias: bool = True) -> str:
    """Rename a doc to fix a typo/misspelled name. Returns the new slug, or ''.

    Renames the file (slug follows the new title) inside the same folder and sets
    the new ``title``, preserving body/type/description/tags/created. The OLD title
    is kept as an alias (so existing inline ``[[Old Name]]`` links still resolve)
    unless ``add_alias`` is False. Old file is removed when the slug actually changes.
    """
    doc = read_doc(agent_id, old_slug)
    if doc is None:
        return ""
    fm, body = doc["frontmatter"], doc["body"]
    old_slug = (old_slug or "").strip("/")
    folder, _, _base = old_slug.rpartition("/")  # folder == '' when no slash
    new_base = slugify(new_title)
    if not new_base:
        return ""
    new_rel = f"{folder}/{new_base}" if folder else new_base

    aliases = list(fm.get("aliases") or [])
    old_title = (fm.get("title") or "").strip()
    if add_alias and old_title and old_title.casefold() != new_title.strip().casefold() \
            and old_title not in aliases:
        aliases.append(old_title)

    rel = upsert_doc(agent_id, title=new_title, body=body,
                     doc_type=fm.get("type") or "note", description=fm.get("description"),
                     folder=folder, tags=fm.get("tags") or [], aliases=aliases, slug=new_base)
    if not rel:
        return ""
    if new_rel != old_slug:
        try:
            os.remove(_doc_path(agent_id, old_slug))
        except OSError:
            pass
    vlog("writer[%s]: rename_doc %s -> %s", agent_id, old_slug, new_rel)
    return new_rel


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
        logger.warning("evomem_writer[%s]: sync_now failed", agent_id, exc_info=True)
        return False
