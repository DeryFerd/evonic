"""
evomem_client.py -- CLI subprocess wrapper for Evomem.

Provides a Python interface to the evomem static binary via subprocess.
On any failure (timeout, non-zero exit, bad JSON, binary missing), returns
None so callers can transparently fall back to the FTS5 pipeline.
"""

from __future__ import annotations

import json
import os
import time
import shutil
import subprocess
import logging

logger = logging.getLogger(__name__)

# Repo root, so `shared/bin/...` resolves regardless of the process working
# directory (otherwise the engine silently downgrades to FTS5 when the server
# is started from anywhere other than the repo root).
_BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _resolve_binary() -> str:
    """Locate the evomem binary.

    Honours EVOMEM_BINARY env override. Paths are resolved against the repo root,
    not the process working directory.
    """
    env = os.environ.get("EVOMEM_BINARY")
    if env:
        return env
    path = os.path.join(_BASE_DIR, "shared", "bin", "evomem")
    if os.path.isfile(path):
        return path
    return os.path.join(_BASE_DIR, "shared", "bin", "evomem")


_EVOMEM_BINARY = _resolve_binary()
_EVOMEM_TIMEOUT = int(os.environ.get("EVOMEM_TIMEOUT", "5"))

# Operational tracing for evomem internals, shared by all evomem modules.
# Set EVOMEM_VERBOSE=1 to emit these traces at INFO level (so they appear in
# normal logs); otherwise they go to DEBUG.
vlogger = logging.getLogger(__name__)
_EVOMEM_VERBOSE = os.environ.get("EVOMEM_VERBOSE", "").strip().lower() in (
    "1", "true", "yes", "on")
if _EVOMEM_VERBOSE and vlogger.level == logging.NOTSET:
    vlogger.setLevel(logging.DEBUG)


def vlog(msg, *args):
    """Emit an evomem operational trace (INFO when EVOMEM_VERBOSE, else DEBUG)."""
    vlogger.log(logging.INFO if _EVOMEM_VERBOSE else logging.DEBUG, msg, *args)


def _summarize(parsed) -> str:
    """Compact one-line description of a parsed evomem JSON result."""
    if not isinstance(parsed, dict):
        return type(parsed).__name__
    for key in ("hits", "facts", "edges"):
        if isinstance(parsed.get(key), list):
            return f"{len(parsed[key])} {key}"
    if "links" in parsed:  # stats
        return f"pages={parsed.get('pages')} links={parsed.get('links')} " \
               f"dangling={parsed.get('dangling_links')}"
    if "links_resolved" in parsed:  # sync
        return f"sync added={parsed.get('added')} updated={parsed.get('updated')} " \
               f"links_resolved={parsed.get('links_resolved')}"
    return "ok"


def get_engine() -> str:
    """Return the active primary memory engine ('evomem' or 'fts5').

    Evomem is the default. It transparently downgrades to FTS5 when the
    binary is missing/not executable, so binary-less deployments keep working.
    EVONIC_MEMORY_ENGINE overrides the default; an unknown value is treated as
    'evomem'.
    """
    engine = os.environ.get("EVONIC_MEMORY_ENGINE", "evomem").strip().lower()
    if engine not in ("evomem", "fts5"):
        engine = "evomem"
    if engine == "evomem" and not is_available():
        return "fts5"
    return engine


def is_available() -> bool:
    """Check whether the evomem binary exists and is executable."""
    return os.path.isfile(_EVOMEM_BINARY) and os.access(_EVOMEM_BINARY, os.X_OK)


def _run(brain_dir: str, args: list, timeout: int = None,
         expect_json: bool = True) -> dict:
    """Run evomem CLI and return parsed JSON, or None on any failure.

    Some commands (e.g. `init`) print a plain-text confirmation even with
    --json; pass expect_json=False for those to return the raw stdout string
    without logging a spurious JSON warning.
    """
    if timeout is None:
        timeout = _EVOMEM_TIMEOUT
    cmd = [_EVOMEM_BINARY, "--brain", brain_dir, "--json"] + args
    cmd_desc = " ".join(str(a) for a in args)
    vlog("run: %s (brain=%s)", cmd_desc, brain_dir)
    t0 = time.time()
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        dt_ms = (time.time() - t0) * 1000
        if result.returncode != 0:
            logger.warning("evomem exited with code %d: %s", result.returncode, result.stderr.strip()[:200])
            return None
        if not result.stdout.strip():
            vlog("run: %s -> empty (%.0fms)", cmd_desc, dt_ms)
            return None
        if not expect_json:
            vlog("run: %s -> text ok (%.0fms)", cmd_desc, dt_ms)
            return result.stdout.strip()
        parsed = json.loads(result.stdout)
        vlog("run: %s -> %s (%.0fms)", cmd_desc, _summarize(parsed), dt_ms)
        return parsed
    except subprocess.TimeoutExpired:
        logger.warning("evomem subprocess timed out after %ds", timeout)
        return None
    except json.JSONDecodeError:
        logger.warning("evomem returned invalid JSON")
        return None
    except FileNotFoundError:
        logger.warning("evomem binary not found at %s", _EVOMEM_BINARY)
        return None
    except Exception as e:
        logger.warning("evomem subprocess error: %s", e)
        return None


def _get_brain_dir(agent_id: str) -> str:
    """Return the evomem directory path for a given agent."""
    return f"agents/{agent_id}/brain"


def _get_kb_dir(agent_id: str) -> str:
    """Return the KB directory path for a given agent.

    KB files live at agents/<id>/kb/ and are mirrored into the brain's
    kb/ subdirectory before sync so the evomem binary can scan them.
    """
    return f"agents/{agent_id}/kb"


def _mirror_kb_files(agent_id: str) -> dict:
    """Mirror KB files from agents/<id>/kb/ into brain/kb/ for sync.

    Copies new/changed files, removes stale ones (deleted from kb/ source),
    and returns a stats dict: {copied, removed, unchanged}.

    Walks the KB source directory recursively so files in subdirectories
    are mirrored with their relative paths preserved. Only .md files are
    copied.

    The evomem binary scans all .md files under the brain directory, so
    mirroring KB files into brain/kb/ makes them visible to the sync engine.
    Content hash comparison avoids unnecessary writes.

    When the KB source directory does not exist, any stale brain/kb/
    copies are cleaned up so the next sync soft-deletes the pages.
    """
    brain_dir = _get_brain_dir(agent_id)
    kb_dir = _get_kb_dir(agent_id)
    brain_kb_dir = os.path.join(brain_dir, "kb")

    stats = {"copied": 0, "removed": 0, "unchanged": 0}

    # ---- No KB source directory: clean up any stale brain/kb/ copies ----
    if not os.path.isdir(kb_dir):
        if os.path.isdir(brain_kb_dir):
            # Walk brain/kb/ bottom-up to remove all .md files and empty dirs
            for dirpath, _dirnames, filenames in os.walk(brain_kb_dir, topdown=False):
                for filename in filenames:
                    if filename.endswith(".md"):
                        os.remove(os.path.join(dirpath, filename))
                        stats["removed"] += 1
                # Remove directory if empty (bottom-up ensures children first)
                try:
                    os.rmdir(dirpath)
                except OSError:
                    pass
        return stats

    # ---- Ensure brain/kb/ directory exists ----
    os.makedirs(brain_kb_dir, exist_ok=True)

    # Collect existing brain/kb/ files as relative paths
    brain_kb_files: set = set()
    if os.path.isdir(brain_kb_dir):
        for dirpath, _dirnames, filenames in os.walk(brain_kb_dir):
            for filename in filenames:
                if filename.endswith(".md"):
                    rel_path = os.path.relpath(
                        os.path.join(dirpath, filename), brain_kb_dir)
                    brain_kb_files.add(rel_path)

    # ---- Copy new or changed KB files (recursively) ----
    kb_files: set = set()
    for dirpath, _dirnames, filenames in os.walk(kb_dir):
        for filename in sorted(filenames):
            if not filename.endswith(".md"):
                continue
            src = os.path.join(dirpath, filename)
            rel_path = os.path.relpath(src, kb_dir)
            kb_files.add(rel_path)
            dst = os.path.join(brain_kb_dir, rel_path)

            if os.path.exists(dst):
                # Compare content to avoid unnecessary writes
                try:
                    with open(src, "rb") as f:
                        src_content = f.read()
                    with open(dst, "rb") as f:
                        dst_content = f.read()
                    if src_content == dst_content:
                        stats["unchanged"] += 1
                        continue
                except OSError:
                    pass  # fall through to copy

            # Ensure parent directory exists for nested files
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
            stats["copied"] += 1
            vlog("kb_mirror[%s]: copied %s", agent_id, rel_path)

    # ---- Remove stale files (deleted from kb/ source) ----
    for rel_path in sorted(brain_kb_files - kb_files):
        os.remove(os.path.join(brain_kb_dir, rel_path))
        stats["removed"] += 1
        vlog("kb_mirror[%s]: removed stale %s", agent_id, rel_path)

    # ---- Remove empty subdirectories from brain/kb/ ----
    if os.path.isdir(brain_kb_dir):
        for dirpath, _dirnames, _filenames in os.walk(brain_kb_dir, topdown=False):
            if dirpath == brain_kb_dir:
                continue  # keep the root kb/ directory
            try:
                os.rmdir(dirpath)
            except OSError:
                pass  # directory not empty, skip

    if stats["copied"] or stats["removed"]:
        vlog("kb_mirror[%s]: copied=%d removed=%d unchanged=%d",
             agent_id, stats["copied"], stats["removed"], stats["unchanged"])

    return stats


def _brain_db_exists(brain_dir: str) -> bool:
    """Check whether the evomem database exists (either .evomem.db or .evobrain.db).

    The evomem binary internally creates .evobrain.db, but the Python code
    references .evomem.db. This helper checks for both so the brain is
    considered initialised when either file is present.
    """
    return (
        os.path.isfile(os.path.join(brain_dir, ".evomem.db")) or
        os.path.isfile(os.path.join(brain_dir, ".evobrain.db"))
    )


def init_evomem(agent_id: str) -> bool:
    """Initialize a new evomem directory for the agent. Returns True on success."""
    brain_dir = _get_brain_dir(agent_id)
    if not is_available():
        return False
    if os.path.isdir(brain_dir) and _brain_db_exists(brain_dir):
        return True
    os.makedirs(brain_dir, exist_ok=True)
    # `init` prints a plain-text confirmation even with --json, so verify success
    # by the presence of the database file rather than a parsed JSON result.
    _run(brain_dir, ["init"], expect_json=False)

    # The evomem binary internally creates .evobrain.db, but the Python code
    # references .evomem.db after commit ead2b69. Create a symlink to bridge
    # the mismatch without rebuilding the binary or reverting the rename.
    evobrain_db = os.path.join(brain_dir, ".evobrain.db")
    evomem_db = os.path.join(brain_dir, ".evomem.db")
    if os.path.isfile(evobrain_db) and not os.path.isfile(evomem_db):
        os.symlink(evobrain_db, evomem_db)
        vlog("created symlink .evomem.db -> .evobrain.db in %s", brain_dir)

    return _brain_db_exists(brain_dir)


def capture(agent_id: str, text: str, category: str = "general") -> dict:
    """Capture a fact/thought into the agent's evomem.

    Returns dict with {slug, path} or None on failure.
    """
    brain_dir = _get_brain_dir(agent_id)
    if not os.path.isdir(brain_dir) or not os.path.exists(os.path.join(brain_dir, ".evomem.db")):
        if not init_evomem(agent_id):
            return None
    # Build a safe title: strip YAML-breaking characters (brackets, quotes, colons)
    safe_title = (f"{category}: {text[:80]}"
                  .replace("[", "(").replace("]", ")")
                  .replace('"', "").replace("'", "")
                  .replace(":", " -"))
    result = _run(brain_dir, ["capture", "--title", safe_title, text])
    if not result:
        return None
    # capture output is plain text in JSON mode: "captured -> slug (path)"
    return {"text": text, "category": category, "raw": result}


def search(agent_id: str, query: str, limit: int = 8,
           mode: str = "balanced", timeout: int = None) -> dict:
    """Search the agent's evomem with hybrid retrieval.

    mode is one of 'conservative' | 'balanced' | 'tokenmax'.
    Returns the full JSON response (with 'hits' array) or None on failure.
    """
    brain_dir = _get_brain_dir(agent_id)
    if not os.path.isdir(brain_dir) or not os.path.exists(os.path.join(brain_dir, ".evomem.db")):
        return None
    return _run(brain_dir, ["search", "--mode", mode, "--limit", str(limit), query],
                timeout=timeout)


def think(agent_id: str, query: str, mode: str = "balanced",
          timeout: int = None) -> dict:
    """Brain-layer synthesis with gap analysis.

    Returns the full JSON response ({facts, gaps, ...}) or None on failure.
    """
    brain_dir = _get_brain_dir(agent_id)
    if not os.path.isdir(brain_dir) or not os.path.exists(os.path.join(brain_dir, ".evomem.db")):
        return None
    return _run(brain_dir, ["think", "--mode", mode, query], timeout=timeout)


def graph_query(agent_id: str, start: str, edge: str = None,
                hops: int = 2, timeout: int = None) -> dict:
    """Traverse typed edges from a start page (slug, title, or alias).

    Returns {start, edges:[{src_slug, dst_slug, edge_type, hop}], cached} or
    None on failure. `edge` optionally filters by edge type.
    """
    brain_dir = _get_brain_dir(agent_id)
    if not os.path.isdir(brain_dir) or not os.path.exists(os.path.join(brain_dir, ".evomem.db")):
        return None
    args = ["graph-query", "--hops", str(hops), start]
    if edge:
        args[1:1] = ["--edge", edge]  # insert before positional start
    return _run(brain_dir, args, timeout=timeout)


def sync(agent_id: str) -> bool:
    """Re-sync markdown files into the database. Returns True on success.

    Before running the evomem binary sync, this mirrors KB files from
    agents/<id>/kb/ into the brain's kb/ subdirectory so they are picked
    up by the sync engine with source_dir='kb'.  Stale copies (files
    deleted from the KB directory) are removed so the sync engine
    soft-deletes the corresponding pages.
    """
    brain_dir = _get_brain_dir(agent_id)
    if not os.path.isdir(brain_dir) or not os.path.exists(os.path.join(brain_dir, ".evomem.db")):
        return False

    # Mirror KB files into brain/kb/ so the binary scans them
    kb_stats = _mirror_kb_files(agent_id)

    result = _run(brain_dir, ["sync"]) is not None

    if result and (kb_stats["copied"] or kb_stats["removed"]):
        vlog("sync[%s]: kb mirror stats copied=%d removed=%d unchanged=%d",
             agent_id, kb_stats["copied"], kb_stats["removed"], kb_stats["unchanged"])

    return result


def get_kb_graph_metadata(agent_id: str) -> dict | None:
    """Query evomem for KB pages with link-graph metadata.

    Returns a dict with:
      pages: {slug: {slug, title, tags, updated_at, incoming_slugs, outgoing_slugs}}
      target_updated_at: {slug: updated_at_str} for all outgoing link targets
    Returns None if the brain DB does not exist.
    """
    import sqlite3
    brain_dir = _get_brain_dir(agent_id)
    db_path = os.path.join(brain_dir, ".evomem.db")
    if not os.path.isfile(db_path):
        vlog("get_kb_graph_metadata: brain DB not found at %s", db_path)
        return None

    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        pages = {}
        all_outgoing = set()

        rows = conn.execute("""
            SELECT p.slug, p.title, p.tags, p.updated_at,
                   (SELECT COUNT(*) FROM links WHERE dst_slug = p.slug AND dst_page_id IS NOT NULL) as incoming_count,
                   (SELECT GROUP_CONCAT(src.slug) FROM links l JOIN pages src ON l.src_page_id = src.id WHERE l.dst_slug = p.slug) as incoming_slugs,
                   (SELECT GROUP_CONCAT(dst.slug) FROM links l JOIN pages dst ON l.dst_page_id = dst.id WHERE l.src_page_id = p.id) as outgoing_slugs
            FROM pages p WHERE p.source_dir = 'kb' AND p.deleted_at IS NULL
            ORDER BY p.slug
        """).fetchall()

        for row in rows:
            slug = row["slug"]
            tags_raw = row["tags"] or "[]"
            try:
                tags = json.loads(tags_raw) if isinstance(tags_raw, str) else tags_raw
                if not isinstance(tags, list):
                    tags = []
            except (json.JSONDecodeError, TypeError):
                tags = []
            incoming_slugs = [s.strip() for s in row["incoming_slugs"].split(",") if s.strip()] if row["incoming_slugs"] else []
            outgoing_slugs = [s.strip() for s in row["outgoing_slugs"].split(",") if s.strip()] if row["outgoing_slugs"] else []

            pages[slug] = {
                "slug": slug,
                "title": row["title"],
                "tags": tags,
                "updated_at": row["updated_at"],
                "incoming_count": row["incoming_count"],
                "incoming_slugs": incoming_slugs,
                "outgoing_slugs": outgoing_slugs,
            }
            all_outgoing.update(outgoing_slugs)

        # Fetch updated_at for all outgoing link targets (for staleness computation)
        target_updated_at = {}
        if all_outgoing:
            placeholders = ",".join("?" for _ in all_outgoing)
            target_rows = conn.execute(
                f"SELECT slug, updated_at FROM pages WHERE slug IN ({placeholders}) AND deleted_at IS NULL",
                list(all_outgoing),
            ).fetchall()
            for tr in target_rows:
                target_updated_at[tr["slug"]] = tr["updated_at"]

        conn.close()
        vlog("get_kb_graph_metadata: %d KB pages, %d link targets", len(pages), len(target_updated_at))
        return {"pages": pages, "target_updated_at": target_updated_at}

    except Exception:
        logger.warning("get_kb_graph_metadata failed for agent %s", agent_id, exc_info=True)
        return None


def get_evomem_db_mtime(agent_id: str) -> float:
    """Return the mtime of the evomem DB file, or 0.0 if it doesn't exist.

    Used for cache invalidation: when the evomem DB changes (sync runs),
    the system prompt KB listing should be rebuilt.
    """
    brain_dir = _get_brain_dir(agent_id)
    db_path = os.path.join(brain_dir, ".evomem.db")
    try:
        return os.stat(db_path).st_mtime
    except OSError:
        return 0.0


def query_kb_graph(agent_id: str, filename: str) -> dict | None:
    """Query evomem for a single KB page's 1-hop link graph.

    Returns a dict with:
      source: {slug, title, tags, updated_at}
      outgoing: [{slug, title, updated_at}]
      incoming: [{slug, title}]
      outgoing_dangling: [slug]
      same_tag_docs: [{slug, title, tags}]
    Returns None if the brain DB does not exist or the page is not found.
    """
    import sqlite3
    brain_dir = _get_brain_dir(agent_id)
    db_path = os.path.join(brain_dir, ".evomem.db")
    if not os.path.isfile(db_path):
        vlog("query_kb_graph: brain DB not found at %s", db_path)
        return None

    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

        # Look up the source page
        page_row = conn.execute(
            "SELECT id, slug, title, tags, updated_at FROM pages "
            "WHERE slug = ? AND source_dir = 'kb' AND deleted_at IS NULL",
            (filename,),
        ).fetchone()

        if not page_row:
            conn.close()
            return None

        page_id = page_row["id"]
        tags_raw = page_row["tags"] or "[]"
        try:
            source_tags = json.loads(tags_raw) if isinstance(tags_raw, str) else tags_raw
            if not isinstance(source_tags, list):
                source_tags = []
        except (json.JSONDecodeError, TypeError):
            source_tags = []

        source = {
            "slug": page_row["slug"],
            "title": page_row["title"],
            "tags": source_tags,
            "updated_at": page_row["updated_at"],
        }

        # Outgoing resolved links
        out_rows = conn.execute(
            "SELECT dst.slug, dst.title, dst.updated_at FROM links l "
            "JOIN pages dst ON l.dst_page_id = dst.id "
            "WHERE l.src_page_id = ? AND dst.source_dir = 'kb' AND dst.deleted_at IS NULL "
            "ORDER BY dst.slug",
            (page_id,),
        ).fetchall()
        outgoing = [
            {"slug": r["slug"], "title": r["title"], "updated_at": r["updated_at"]}
            for r in out_rows
        ]

        # Outgoing dangling links
        dangling_rows = conn.execute(
            "SELECT dst_slug FROM links "
            "WHERE src_page_id = ? AND dst_page_id IS NULL "
            "ORDER BY dst_slug",
            (page_id,),
        ).fetchall()
        outgoing_dangling = [r["dst_slug"] for r in dangling_rows]

        # Incoming links
        in_rows = conn.execute(
            "SELECT src.slug, src.title FROM links l "
            "JOIN pages src ON l.src_page_id = src.id "
            "WHERE l.dst_slug = ? AND l.dst_page_id IS NOT NULL "
            "AND src.source_dir = 'kb' AND src.deleted_at IS NULL "
            "ORDER BY src.slug",
            (filename,),
        ).fetchall()
        incoming = [{"slug": r["slug"], "title": r["title"]} for r in in_rows]

        # Same-tag docs
        same_tag_docs = []
        if source_tags:
            # Build OR conditions for each tag
            placeholders = ",".join("?" for _ in source_tags)
            tag_rows = conn.execute(
                f"SELECT slug, title, tags FROM pages "
                f"WHERE source_dir = 'kb' AND deleted_at IS NULL AND slug != ? "
                f"AND ({' OR '.join('tags LIKE ?' for _ in source_tags)}) "
                f"ORDER BY slug",
                [filename] + [f"%{t}%" for t in source_tags],
            ).fetchall()
            for r in tag_rows:
                t_raw = r["tags"] or "[]"
                try:
                    t_list = json.loads(t_raw) if isinstance(t_raw, str) else t_raw
                    if not isinstance(t_list, list):
                        t_list = []
                except (json.JSONDecodeError, TypeError):
                    t_list = []
                same_tag_docs.append({
                    "slug": r["slug"],
                    "title": r["title"],
                    "tags": t_list,
                })

        conn.close()
        vlog("query_kb_graph: %s -> %d outgoing, %d incoming, %d dangling, %d same-tag",
             filename, len(outgoing), len(incoming), len(outgoing_dangling),
             len(same_tag_docs))
        return {
            "source": source,
            "outgoing": outgoing,
            "incoming": incoming,
            "outgoing_dangling": outgoing_dangling,
            "same_tag_docs": same_tag_docs,
        }

    except Exception:
        logger.warning("query_kb_graph failed for agent %s, file %s",
                       agent_id, filename, exc_info=True)
        return None
