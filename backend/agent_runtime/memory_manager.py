"""
memory_manager.py — LLM-powered long-term memory for agents.

Extract→Deduplicate→Store→Retrieve pattern (inspired by Mem0):
1. Extract salient facts from conversation summary via LLM
2. Deduplicate/merge against existing memories
3. Store to per-agent SQLite memories table (FTS5-indexed, zero dependencies)
4. Retrieve via FTS5 BM25 keyword search at context-build time

Primary + fallback architecture (evomem + FTS5):
- When EVONIC_MEMORY_ENGINE=evomem, evomem is primary, FTS5 is fallback.
- On any evomem failure (timeout, binary missing, bad JSON), falls back to FTS5.
- The summarizer is the single writer to long-term memory + graph. The
  `remember` tool (store_memory) only pins a fact into the running session
  summary; the summarizer later folds it into both systems.

No new pip dependencies — uses existing LLM client, SQLite FTS5 (Python stdlib),
and the evomem static binary via subprocess.
"""

import os
import re
import json
import logging
import threading
from typing import List, Optional

from models.db import db
from backend.llm_client import llm_client, strip_thinking_tags
from backend.agent_runtime.evomem_client import (
    get_engine, search as evomem_search, think as evomem_think,
    graph_query as evomem_graph_query, init_evomem as evomem_init, vlog,
)
from backend.agent_runtime import evomem_writer

logger = logging.getLogger(__name__)

# Search modes per call-site (overridable via env). Passive injection favours
# precision; explicit recall favours maximum recall from the weak hash embedder.
_PASSIVE_SEARCH_MODE = os.environ.get("EVOMEM_SEARCH_MODE_PASSIVE", "conservative")
_RECALL_SEARCH_MODE = os.environ.get("EVOMEM_SEARCH_MODE_RECALL", "tokenmax")

# Cross-session entity coreference uses one LLM call PER extracted entity to
# decide if a variant name ("Robin") is the same as an existing page ("Robin
# Syihab"). On a slow/thinking model that dominates graph-extraction latency, so
# it is OFF by default — exact-slug dedup still merges identical names without an
# LLM call. Set EVOMEM_ENTITY_COREF=1 to re-enable (only worth it on a fast model).
_ENTITY_COREF_LLM = os.environ.get("EVOMEM_ENTITY_COREF", "0").strip().lower() \
    in ("1", "true", "yes", "on")

# Memory categories that describe the user → linked to the canonical user entity
# so the fact becomes graph-adjacent and feeds `think`.
_USER_SCOPED = {"user_info", "preference", "instruction", "decision", "context"}

_EXTRACT_PROMPT = """You are a long-term memory extractor for an AI assistant. Given a conversation summary, extract facts worth remembering in FUTURE conversations.

Rules:
- Only extract facts that are durable and useful across sessions
- Skip ephemeral details (current task status, temporary states, in-progress items)
- Each fact must be a single clear, self-contained sentence in English
- Categories:
  - user_info: user identity, contact info, role (name, phone, email, job title)
  - preference: stated likes/dislikes, communication style, language preference
  - decision: commitments or choices made by the user or agreed upon
  - context: background facts about the user's domain, project, or situation
  - instruction: persistent instructions given to the agent about how to behave
- Return a JSON array only: [{{"content": "...", "category": "..."}}]
- If nothing worth remembering long-term, return: []

Conversation summary:
{summary}

Return only a JSON array, no explanation:"""

_DEDUP_PROMPT = """You are a memory deduplicator. Given new facts and existing memories, decide how to handle each new fact.

Existing memories:
{existing}

New facts to process:
{new_facts}

Rules:
- If a new fact is semantically identical to an existing memory: return null for that entry
- If a new fact UPDATES/CONTRADICTS an existing memory (e.g. user changed phone number): return {{"action": "update", "id": <existing_id>, "content": "<merged content>", "category": "..."}}
- If a new fact is genuinely new (no overlap): return {{"action": "add", "content": "...", "category": "..."}}

Return a JSON array with exactly one entry per new fact (same order as new facts):"""

_DIMENSION_PROMPT = """Given this memory fact, assign a semantic dimension key.

The dimension is a dot-separated path that uniquely identifies WHAT aspect of knowledge this fact describes.
Examples:
- "User prefers Javanese language" → "user.language_preference"
- "User's phone number is 08123456" → "user.phone_number"
- "User's name is Robin" → "user.name"
- "User prefers dark mode" → "user.ui_preference.theme"
- "Always respond in formal tone" → "instruction.tone"
- "User decided to use PostgreSQL for the project" → "decision.database_choice"
- "User works at Acme Corp" → "user.employer"

Rules:
- Use lowercase, dot-separated hierarchy
- First segment is the category: user, preference, decision, context, instruction
- Be specific enough to detect contradictions but general enough to group related facts
- If the fact is too general/vague to assign a clear dimension, return null

Fact: {content}
Category: {category}

Return only the dimension string (e.g. "user.language_preference") or null:"""

_AUTHOR_DOCS_PROMPT = """You are the long-term memory author for an AI assistant. From the SOURCE below (a conversation summary or a single remembered fact), write or update durable knowledge DOCUMENTS for the things worth remembering across future conversations.

{guidance}

How to write a document:
- Each document is rich narrative prose about ONE subject — a person, place, venue, organization, company, product, contact, or a topical note.
- Weave Obsidian-style [[Wiki Links]] INLINE into your sentences for every OTHER named subject you mention (the link text is that subject's display name). The link is part of the prose — NEVER a separate "Relations"/"Links" list at the bottom.
  GOOD: `User jalan-jalan ke [[Jakarta]] makan di [[Ayam Bakar Taliwang Rinjani]] di [[Pesanggrahan]].`
  BAD:  a paragraph with no links, followed by a "Relations:" list of [[...]].
- Choose a `type` for each document from: note, person, place, venue, organization, company, product, contact.
- The user is always referred to as "User".

Deduplication:
- You are given EXISTING documents as `[slug] Title :: description`. If a subject is already covered by one, return action "update" with its `slug` and a `body` containing ONLY the genuinely NEW information to add (its existing prose is preserved). Otherwise return action "create" with a full `body` and a one-line `description`.

Skip ephemeral chatter, pleasantries, and transient/in-progress task status.

Return STRICT JSON only, no prose:
{{"docs": [
  {{"action": "create", "title": "Jakarta", "type": "place", "description": "Capital of Indonesia; User's home city.", "tags": ["place"], "body": "Jakarta adalah ibu kota Indonesia. User tinggal di [[Pesanggrahan]]."}},
  {{"action": "update", "slug": "<existing slug>", "title": "...", "type": "note", "description": "...", "tags": ["..."], "body": "<only the new prose to append, with inline [[links]]>"}}
]}}
If nothing is worth keeping long-term, return: {{"docs": []}}

EXISTING documents:
{existing}

SOURCE:
{source}

Return only the JSON object:"""


def _try_evomem_retrieval(agent_id: str, query: str, limit: int = 8) -> Optional[str]:
    """Try to retrieve memories via evomem hybrid search.

    Returns a formatted markdown string (matching the FTS5 format), or None
    if evomem is unavailable, disabled, or returns no results.
    """
    engine = get_engine()
    if engine != "evomem":
        return None
    try:
        result = evomem_search(agent_id, query, limit, mode=_PASSIVE_SEARCH_MODE)
    except Exception:
        logger.debug("evomem search exception, falling back to FTS5")
        return None
    if not result or not isinstance(result.get("hits"), list) or not result["hits"]:
        vlog("retrieve[%s]: 0 hits (mode=%s) -> FTS5 fallback",
             agent_id, _PASSIVE_SEARCH_MODE)
        return None
    vlog("retrieve[%s]: %d hits (mode=%s)",
         agent_id, len(result["hits"]), _PASSIVE_SEARCH_MODE)
    lines = ["## Memory (Evomem)",
             "Facts remembered from past conversations:"]
    for hit in result["hits"]:
        src = f"{hit.get('source_dir', '?')}/{hit.get('slug', '?')}"
        evidence = hit.get("evidence", "?")
        snippet = (hit.get("snippet") or hit.get("title") or "").strip()
        if snippet:
            lines.append(f"- [{evidence}, {src}] {snippet}")
    return "\n".join(lines)


def _emit_doc_updated(agent_id: str, modified_slugs: list) -> None:
    """Emit a ``doc_updated`` event so the evomem sync listener fires."""
    if not modified_slugs:
        return
    try:
        from backend.event_stream import event_stream
        event_stream.emit('doc_updated', {
            'agent_id': agent_id,
            'modified_slugs': modified_slugs,
        })
        vlog("knowledge[%s]: emitted doc_updated for %d slug(s)", agent_id, len(modified_slugs))
    except Exception:
        logger.debug("_emit_doc_updated failed for %s (non-fatal)", agent_id)


def _get_active_collection(agent_id: str, session_id: str) -> str:
    """Return the active collection folder slug for a session ('' = root)."""
    if not session_id:
        return ""
    try:
        raw = db.get_session_state(session_id, agent_id=agent_id)
        if not raw:
            return ""
        state = json.loads(raw)
        if isinstance(state, dict):
            return (state.get("active_collection") or "").strip("/")
    except Exception:
        pass
    return ""


def _set_active_collection(agent_id: str, session_id: str, folder: str) -> None:
    """Set the active collection folder for a session (read-merge-write so other
    session-state keys — mode/tasks/plan_file — are preserved)."""
    if not session_id:
        return
    try:
        raw = db.get_session_state(session_id, agent_id=agent_id)
        state = {}
        if raw:
            try:
                state = json.loads(raw)
            except (ValueError, TypeError):
                state = {}
        if not isinstance(state, dict):
            state = {}
        state["active_collection"] = (folder or "").strip("/")
        db.upsert_session_state(session_id, json.dumps(state), agent_id=agent_id)
    except Exception:
        logger.debug("set_active_collection failed for %s", agent_id)


def create_collection_tool(agent_id: str, session_id: str, name: str,
                          kind: str = "session", description: str = "") -> dict:
    """Create a collection folder and make it active. Backs the `create_collection`
    built-in tool."""
    if get_engine() != "evomem":
        return {"error": "Collections require the evomem memory engine."}
    if not (name or "").strip():
        return {"error": "A collection name is required."}
    kind = kind if kind in ("session", "group") else "session"
    folder = evomem_writer.create_collection(
        agent_id, folder=name, title=name.strip(), kind=kind,
        description=(description or "").strip())
    if not folder:
        return {"error": "Failed to create the collection."}
    _set_active_collection(agent_id, session_id, folder)
    evomem_writer.mark_dirty(agent_id)
    return {
        "result": (f"Collection '{folder}' ({kind}) is created and now active — "
                   "durable knowledge you save will be filed inside it."),
        "folder": folder, "kind": kind,
    }


def switch_collection_tool(agent_id: str, session_id: str, name: str) -> dict:
    """Switch the active collection (or 'root'). Backs the `switch_collection` tool."""
    name = (name or "").strip()
    if name.lower() in ("", "root", "/"):
        _set_active_collection(agent_id, session_id, "")
        return {"result": "Active collection set to root (top-level knowledge base)."}
    folder = evomem_writer.slugify(name)
    if not folder or evomem_writer.read_doc(agent_id, f"{folder}/index") is None:
        return {"error": f"No collection named '{name}'. Create it with create_collection first."}
    _set_active_collection(agent_id, session_id, folder)
    return {"result": f"Active collection is now '{folder}'.", "folder": folder}


def _list_existing_docs(agent_id: str, folder: str = "", limit: int = 80) -> list:
    """List existing docs as dedupe candidates: [{slug, title, description}].

    Walks the agent's kb/ dir, prioritising the active collection folder and root
    docs (the most likely merge targets). Skips inbox/ and hidden files.
    """
    kb_dir = f"agents/{agent_id}/kb"
    if not os.path.isdir(kb_dir):
        return []
    folder = (folder or "").strip("/")
    prio, rest = [], []
    for root, dirs, fnames in os.walk(kb_dir):
        dirs[:] = [d for d in dirs if not d.startswith('.') and d != 'inbox']
        for fn in fnames:
            if not fn.endswith('.md') or fn.startswith('.'):
                continue
            full = os.path.join(root, fn)
            rel = os.path.relpath(full, kb_dir).replace(os.sep, '/')
            slug = rel[:-3]
            try:
                with open(full, encoding='utf-8') as f:
                    fm, _ = evomem_writer._parse_frontmatter(f.read())
            except OSError:
                continue
            title = fm.get('title') or slug.rsplit('/', 1)[-1]
            desc = (fm.get('description') or '')[:160]
            entry = {"slug": slug, "title": title, "description": desc}
            seg = slug.split('/', 1)[0] if '/' in slug else ''
            if (folder and seg == folder) or '/' not in slug:
                prio.append(entry)
            else:
                rest.append(entry)
    return (prio + rest)[:limit]


def _resolve_doc_slug(agent_id: str, slug_hint: str, title: str, folder: str):
    """Resolve to an existing doc slug to update, or None to create a new doc.

    Tries the model's slug hint, then the title-slug inside the active folder,
    then the title-slug at root — the first that exists on disk wins.
    """
    folder = (folder or "").strip("/")
    cands = []
    if slug_hint:
        cands.append(slug_hint.strip("/"))
    tslug = evomem_writer.slugify(title)
    if tslug:
        if folder:
            cands.append(f"{folder}/{tslug}")
        cands.append(tslug)
    for c in cands:
        if c and evomem_writer.read_doc(agent_id, c) is not None:
            return c
    return None


def _doc_delta(agent_id: str, slug: str, new_body: str,
               llm_lock: threading.Lock):
    """Return only the genuinely-new prose to append to an existing doc, or None
    if the doc already covers it (idempotency guard)."""
    doc = evomem_writer.read_doc(agent_id, slug)
    if doc is None:
        return new_body
    snippet = _kb_llm_text(
        _KB_MERGE_SNIPPET_PROMPT.format(existing=doc["body"], content=new_body),
        llm_lock)
    if not snippet or snippet.strip().upper() == "NONE":
        return None
    return snippet


def _author_docs(agent: dict, session_id: str, source_text: str,
                 llm_lock: threading.Lock) -> None:
    """Unified knowledge authoring: turn a summary or a remembered fact into
    rich, inline-linked docs in the active collection.

    One LLM call authors/updates docs (each rich prose about one subject, with
    inline ``[[Doc Title]]`` links). New docs are created via ``upsert_doc`` in
    the active collection folder (root by default); existing docs are appended to
    (delta only). Emits ``doc_updated`` so the evomem sync wires the graph from
    the inline links. Best-effort, runs in a background thread.
    """
    agent_id = agent['id']
    if get_engine() != 'evomem':
        return
    try:
        folder = _get_active_collection(agent_id, session_id)
        existing = _list_existing_docs(agent_id, folder)
        existing_text = "\n".join(
            f"[{d['slug']}] {d['title']} :: {d['description']}" for d in existing
        ) or "(none yet)"
        # Fixed authoring guidance for deterministic output — the agent's
        # summarize_prompt governs *summary style*, not *what to file as knowledge*.
        prompt = _AUTHOR_DOCS_PROMPT.format(
            guidance=_DEFAULT_KB_GUIDANCE, existing=existing_text, source=source_text)
        data = _kb_llm_json(prompt, llm_lock)
        if not isinstance(data, dict):
            return
        docs = data.get('docs')
        if not isinstance(docs, list) or not docs:
            return

        modified, created, updated = [], 0, 0
        for d in docs[:12]:
            if not isinstance(d, dict):
                continue
            title = (d.get('title') or '').strip()
            body = (d.get('body') or '').strip()
            if not title or not body:
                continue
            doc_type = (d.get('type') or 'note').strip()
            description = (d.get('description') or '').strip()
            tags = [t for t in (d.get('tags') or []) if isinstance(t, str) and t.strip()]
            slug_hint = (d.get('slug') or '').strip()

            base_slug = evomem_writer.slugify(title)
            lock_key = (f"{folder}/{base_slug}" if folder else base_slug)
            with _kb_page_lock(agent_id, lock_key):
                target = _resolve_doc_slug(agent_id, slug_hint, title, folder)
                if target:
                    delta = _doc_delta(agent_id, target, body, llm_lock)
                    if delta and evomem_writer.append_to_doc(agent_id, target, delta):
                        modified.append(target)
                        updated += 1
                else:
                    rel = evomem_writer.upsert_doc(
                        agent_id, title=title, body=body, doc_type=doc_type,
                        description=description, folder=folder, tags=tags)
                    if rel:
                        modified.append(rel)
                        created += 1
                        if folder:
                            evomem_writer.add_to_collection_index(agent_id, folder, title)

        if modified:
            evomem_writer.mark_dirty(agent_id)
            _emit_doc_updated(agent_id, modified)
        logger.info("[MemoryManager] author_docs[%s]: %d created, %d updated (folder=%s)",
                    agent_id, created, updated, folder or 'root')
    except Exception:
        logger.debug("author_docs exception (non-fatal) for %s", agent_id)


def process_knowledge(agent: dict, session_id: str, summary: str,
                      llm_lock: threading.Lock) -> None:
    """Author/update rich, inline-linked knowledge docs from a conversation
    summary. Triggered on ``summary_updated``; runs in the background extraction
    thread. Best-effort; non-fatal on any error."""
    _author_docs(agent, session_id, summary, llm_lock)


def _extract_dimension(content: str, category: str,
                       llm_lock: threading.Lock = None) -> Optional[str]:
    """Use LLM to extract a semantic dimension key from a memory fact."""
    prompt = _DIMENSION_PROMPT.format(content=content, category=category)
    try:
        call_kwargs = dict(
            messages=[{"role": "user", "content": prompt}],
            tools=None, temperature=0.0, enable_thinking=False, max_tokens=64,
        )
        if llm_lock:
            with llm_lock:
                result = llm_client.chat_completion(**call_kwargs)
        else:
            result = llm_client.chat_completion(**call_kwargs)

        if not result.get('success'):
            return None
        raw = result['response']['choices'][0]['message']['content'].strip()
        raw, _ = strip_thinking_tags(raw)
        raw = raw.strip().strip('"').strip("'")
        if raw.lower() == 'null' or not raw:
            return None
        if not all(c.isalnum() or c in '._' for c in raw):
            return None
        return raw
    except Exception:
        return None


def _backfill_null_dimensions(agent_id: str, llm_lock: threading.Lock = None) -> None:
    """Backfill dimension for active memories that have dimension=NULL.

    This is a lazy migration: called during conflict detection so that
    pre-existing memories (stored before the dimension feature) become
    visible to dimension-based conflict lookups.
    """
    null_mems = db.get_null_dimension_memories(agent_id)
    for m in null_mems:
        dim = _extract_dimension(m['content'], m.get('category', 'general'), llm_lock)
        if dim:
            db.update_memory(agent_id, m['id'], m['content'],
                             m.get('category'), dimension=dim)


def _store_with_conflict_detection(agent_id: str, session_id: str, content: str,
                                   category: str, llm_lock: threading.Lock = None,
                                   dimension: str = None) -> dict:
    """Store a memory with dimension extraction and conflict detection.

    If dimension is not provided, extracts it via LLM.
    If an existing active memory shares the same dimension, supersedes it.
    Backfills NULL-dimension memories lazily so pre-existing records are
    included in conflict detection.
    """
    if dimension is None:
        dimension = _extract_dimension(content, category, llm_lock)

    superseded_ids = []
    if dimension:
        # Backfill any pre-existing memories that lack a dimension
        _backfill_null_dimensions(agent_id, llm_lock)

        existing = db.get_memories_by_dimension(agent_id, dimension)
        superseded_ids = [m['id'] for m in existing]

    memory_id = db.add_memory(agent_id, content, category, session_id, dimension)

    for old_id in superseded_ids:
        db.supersede_memory(agent_id, old_id, memory_id)

    return {"id": memory_id, "dimension": dimension, "superseded": superseded_ids}


def extract_and_store_memories(agent: dict, session_id: str, summary: str,
                                llm_lock: threading.Lock) -> None:
    """Extract memorable facts from a conversation summary and persist them.

    Runs in a background thread after summarization. Non-fatal on any error.
    """
    agent_id = agent['id']
    try:
        # Step 1: Extract facts from summary
        prompt = _EXTRACT_PROMPT.format(summary=summary)
        with llm_lock:
            result = llm_client.chat_completion(
                messages=[{"role": "user", "content": prompt}],
                tools=None,
                temperature=0.0,
                enable_thinking=False,
                max_tokens=1024,
            )
        if not result.get('success'):
            return

        choice = result['response'].get('choices', [{}])[0]
        if choice.get('finish_reason') == 'length':
            return

        raw = choice.get('message', {}).get('content', '')
        raw, _ = strip_thinking_tags(raw)
        raw = _strip_code_fences(raw)

        try:
            facts = json.loads(raw)
        except json.JSONDecodeError:
            return
        if not isinstance(facts, list) or not facts:
            return

        # Filter out malformed entries
        facts = [f for f in facts
                 if isinstance(f, dict) and f.get('content', '').strip()]
        if not facts:
            return

        # Step 2: Get existing memories for deduplication
        existing = db.get_all_memories(agent_id)

        if existing:
            existing_text = "\n".join(
                f"[id={m['id']}] ({m['category']}) {m['content']}"
                for m in existing[:60]  # cap to avoid huge prompts
            )
            new_text = "\n".join(
                f"- ({f.get('category', 'general')}) {f['content']}"
                for f in facts
            )
            dedup_prompt = _DEDUP_PROMPT.format(
                existing=existing_text,
                new_facts=new_text,
            )
            with llm_lock:
                dedup_result = llm_client.chat_completion(
                    messages=[{"role": "user", "content": dedup_prompt}],
                    tools=None,
                    temperature=0.0,
                    enable_thinking=False,
                    max_tokens=1024,
                )
            if dedup_result.get('success'):
                dedup_choice = dedup_result['response'].get('choices', [{}])[0]
                dedup_raw = dedup_choice.get('message', {}).get('content', '')
                dedup_raw, _ = strip_thinking_tags(dedup_raw)
                dedup_raw = _strip_code_fences(dedup_raw)
                try:
                    operations = json.loads(dedup_raw)
                    if isinstance(operations, list):
                        for op in operations:
                            if op is None:
                                continue
                            action = op.get('action')
                            if action == 'add' and op.get('content', '').strip():
                                _store_with_conflict_detection(
                                    agent_id, session_id, op['content'].strip(),
                                    op.get('category', 'general'), llm_lock)
                            elif action == 'update' and op.get('id') and op.get('content', '').strip():
                                dim = _extract_dimension(op['content'].strip(),
                                                         op.get('category', 'general'), llm_lock)
                                db.update_memory(agent_id, int(op['id']),
                                                 op['content'].strip(), op.get('category'),
                                                 dimension=dim)
                        return  # dedup handled all facts
                except (json.JSONDecodeError, KeyError, ValueError):
                    pass  # fall through to simple add

        # No existing memories or dedup failed: add all new facts directly
        for fact in facts:
            _store_with_conflict_detection(
                agent_id, session_id, fact['content'].strip(),
                fact.get('category', 'general'), llm_lock)

    except Exception as e:
        print(f"[MemoryManager] Extraction failed for agent {agent_id} (non-fatal): {e}")


# ─── KB knowledge extraction (summarizer → curated KB pages) ─────────────────

_DEFAULT_KB_GUIDANCE = (
    "By default, capture EVERYTHING important and durable that could help in "
    "future conversations."
)

_KB_MERGE_SNIPPET_PROMPT = """An existing KB page already holds some knowledge. You are given new information. Output ONLY the part of the new information that is NOT already present on the page, as a concise markdown snippet (a short sentence or a few bullet points) ready to append to the page.

Rules:
- Do NOT restate anything already on the page.
- Do NOT rewrite or return the existing content — output only the genuinely new delta.
- If the new information is already fully covered by the page, output exactly: NONE

Existing page body:
{existing}

New information:
{content}

New delta to append (or NONE):"""


def _kb_llm_json(prompt: str, llm_lock: threading.Lock, max_tokens: int = None):
    """Run a JSON-returning LLM call; return parsed value or None on any issue.

    max_tokens defaults to None (the model's configured default) — a thinking
    model spends tokens on reasoning before the answer, so a small cap makes the
    call finish on length with no parseable output.
    """
    try:
        with llm_lock:
            result = llm_client.chat_completion(
                messages=[{"role": "user", "content": prompt}],
                tools=None, temperature=0.0, enable_thinking=False,
                max_tokens=max_tokens)
        if not result.get('success'):
            return None
        choice = result['response'].get('choices', [{}])[0]
        if choice.get('finish_reason') == 'length':
            return None
        raw = choice.get('message', {}).get('content', '')
        raw, _ = strip_thinking_tags(raw)
        raw = _strip_code_fences(raw)
        return json.loads(raw)
    except (json.JSONDecodeError, KeyError, ValueError):
        return None
    except Exception:
        return None


def _kb_llm_text(prompt: str, llm_lock: threading.Lock, max_tokens: int = None):
    """Run a free-text LLM call; return stripped text or None.

    max_tokens defaults to None (model default) so a thinking model has room to
    reason before producing the answer (a small cap finishes on length).
    """
    try:
        with llm_lock:
            result = llm_client.chat_completion(
                messages=[{"role": "user", "content": prompt}],
                tools=None, temperature=0.0, enable_thinking=False,
                max_tokens=max_tokens)
        if not result.get('success'):
            return None
        choice = result['response'].get('choices', [{}])[0]
        if choice.get('finish_reason') == 'length':
            return None
        raw = choice.get('message', {}).get('content', '')
        raw, _ = strip_thinking_tags(raw)
        raw = _strip_code_fences(raw).strip()
        return raw or None
    except Exception:
        return None


# Per-(agent, slug) re-entrant locks serialise read-modify-write on a single KB
# page so concurrent background extractions can't lost-update each other.
_page_locks: dict = {}
_page_locks_guard = threading.Lock()


def _kb_page_lock(agent_id: str, slug: str) -> threading.RLock:
    key = (agent_id, slug)
    with _page_locks_guard:
        lk = _page_locks.get(key)
        if lk is None:
            lk = _page_locks[key] = threading.RLock()
        return lk


def extract_and_store_kb(agent: dict, session_id: str, summary: str,
                          llm_lock: threading.Lock) -> None:
    """File durable knowledge from a remembered fact (or summary) into the KB.

    Backs the `remember` tool path (``store_memory`` → ``_extract_from_fact_async``).
    Routes through the unified rich-doc author so an explicitly-remembered fact
    becomes a rich, inline-linked doc — the same model the summary pipeline uses.
    Runs in a background thread; non-fatal on error.
    """
    _author_docs(agent, session_id, summary, llm_lock)


def get_memories_for_context(agent_id: str, messages: list,
                              limit: int = 8) -> Optional[str]:
    """Retrieve relevant memories for injection into the LLM context.

    Primary + fallback architecture:
    1. If EVONIC_MEMORY_ENGINE=evomem: try evomem hybrid search first.
       On any failure, transparently fall back to FTS5 pipeline.
    2. Otherwise: use FTS5 BM25 keyword search (existing behaviour).

    Returns a formatted markdown string or None if no memories exist.
    """
    try:
        query = _extract_last_user_query(messages)

        # === Primary: evomem ===
        if get_engine() == "evomem" and query:
            evomem_result = _try_evomem_retrieval(agent_id, query, limit)
            if evomem_result:
                return evomem_result
            logger.debug("evomem retrieval returned nothing, falling back to FTS5")

        # === Fallback: FTS5 ===
        memories: List[dict] = []

        if query:
            fts_query = _sanitize_fts_query(query)
            if fts_query:
                try:
                    memories = db.search_memories(agent_id, fts_query, limit)
                except Exception:
                    pass  # FTS5 can fail on unusual query syntax — fall through

        if not memories:
            memories = db.get_recent_memories(agent_id, limit)

        if not memories:
            return None

        lines = ["## Memory",
                 "Facts remembered from past conversations:"]
        for m in memories:
            lines.append(f"- [{m['category']}] {m['content']}")
        return "\n".join(lines)

    except Exception as e:
        print(f"[MemoryManager] Context retrieval failed for agent {agent_id} (non-fatal): {e}")
        return None


def store_memory(agent_id: str, session_id: str, content: str,
                 category: str = 'general') -> dict:
    """Pin a fact into the running session summary. Backs the `remember` tool.

    Two things happen:
    1. The fact is appended to the session's running summary so it is immediately
       visible in the agent's context for the rest of the session.
    2. It is filed into long-term KB + knowledge graph DIRECTLY (in the
       background), so an explicitly-remembered fact becomes a KB page / entity
       node regardless of whether the incremental summarizer later compacts the
       noted bullet away. Dedup in the extractor prevents duplicates with the
       summary-driven pass.
    """
    content = content.strip()
    if not content:
        return {"error": "Memory content cannot be empty."}
    if not session_id:
        return {"error": "No active session to note this fact in."}
    try:
        tag = "" if category in (None, "", "general") else f", {category}"
        bullet = f"- (noted{tag}) {content}"
        rec = db.get_summary(session_id, agent_id=agent_id)
        if rec and rec.get('summary'):
            new_summary = rec['summary'].rstrip() + "\n" + bullet
            db.upsert_summary(session_id, new_summary,
                              rec.get('last_message_id') or 0,
                              rec.get('message_count') or 0,
                              agent_id=agent_id,
                              last_message_ts=rec.get('last_message_ts'))
        else:
            db.upsert_summary(session_id, bullet, 0, 0, agent_id=agent_id)
        # File the explicit fact into KB/graph directly (background, best-effort).
        _extract_from_fact_async(agent_id, session_id, content)
        return {"result": "Noted for this session.", "content": content}
    except Exception as e:
        return {"error": f"Failed to note fact: {e}"}


def _extract_from_fact_async(agent_id: str, session_id: str, content: str) -> None:
    """Run KB + graph extraction on a single remembered fact, in the background.

    Lets an explicitly `remember`-ed fact become a KB page / entity node without
    depending on it surviving summary compaction. Non-fatal on any error.
    """
    if get_engine() != "evomem":
        return

    def _run():
        try:
            from backend.agent_runtime.runtime import AgentRuntime
            from backend.llm_usage_events import usage_context
            agent = db.get_agent(agent_id)
            if not agent:
                return
            with usage_context('memory', agent_id, agent.get('name'), session_id):
                extract_and_store_kb(
                    agent, session_id, content,
                    AgentRuntime._llm_serializer._llm_lock)
        except Exception as e:
            print(f"[MemoryManager] remember-extract failed for {agent_id} "
                  f"(non-fatal): {e}")

    threading.Thread(target=_run, daemon=True).start()


def search_memories(agent_id: str, query: str, limit: int = 10) -> dict:
    """Search memories by keyword. Used by the `recall` built-in tool.

    Primary + fallback: tries evomem first if configured, falls back to FTS5.
    """
    try:
        # === Primary: evomem ===
        engine = get_engine()
        if engine == "evomem":
            evomem_result = evomem_search(agent_id, query, limit,
                                              mode=_RECALL_SEARCH_MODE)
            if evomem_result and isinstance(evomem_result.get("hits"), list):
                hits = evomem_result["hits"]
                if hits:
                    return {
                        "engine": "evomem",
                        "memories": [
                            {"id": h.get("slug"),
                             "content": h.get("snippet") or h.get("title"),
                             "category": h.get("source_dir") or "evomem",
                             "created_at": h.get("updated_at"),
                             "evidence": h.get("evidence"),
                             "score": h.get("score")}
                            for h in hits
                        ],
                        "count": len(hits),
                    }

        # === Fallback: FTS5 ===
        fts_query = _sanitize_fts_query(query)
        if fts_query:
            memories = db.search_memories(agent_id, fts_query, limit)
        else:
            memories = db.get_recent_memories(agent_id, limit)

        if not memories:
            return {"result": "No memories found.", "memories": [], "count": 0}
        return {
            "memories": [
                {"id": m['id'], "content": m['content'],
                 "category": m['category'], "created_at": m['created_at']}
                for m in memories
            ],
            "count": len(memories),
        }
    except Exception as e:
        return {"error": f"Memory search failed: {e}"}


def synthesize_memory(agent_id: str, query: str) -> dict:
    """Brain-layer synthesis over memory. Backs `recall(mode='think')`.

    Returns composed facts (with citations) plus knowledge gaps. Falls back to
    a plain keyword search when evomem is unavailable or has nothing to say.
    """
    try:
        if get_engine() == "evomem":
            result = evomem_think(agent_id, query, mode="balanced")
            if result and isinstance(result.get("facts"), list) and result["facts"]:
                facts = [
                    {"fact": (f.get("lead") or f.get("title") or "").strip(),
                     "source": f.get("slug", "?"),
                     "evidence": f.get("evidence", "?")}
                    for f in result["facts"]
                ]
                gaps = [g.get("message", "") for g in result.get("gaps", [])
                        if isinstance(g, dict) and g.get("message")]
                vlog("think[%s]: %d facts, %d gaps for %r",
                     agent_id, len(facts), len(gaps), query[:60])
                return {"engine": "evomem", "query": query,
                        "facts": facts, "gaps": gaps, "count": len(facts)}
        # Fallback: keyword search
        vlog("think[%s]: no synthesis -> keyword fallback for %r", agent_id, query[:60])
        return search_memories(agent_id, query)
    except Exception as e:
        return {"error": f"Synthesis failed: {e}"}


def graph_lookup(agent_id: str, entity: str, edge_type: str = None,
                 hops: int = 2) -> dict:
    """Traverse the knowledge graph from an entity. Backs `recall(mode='graph')`.

    Resolves a name/alias to a start slug via search, then follows typed edges.
    """
    try:
        if get_engine() != "evomem":
            return {"error": "Knowledge graph is only available with the evomem engine."}
        start = (entity or "").strip()
        if not start:
            return {"error": "An entity name is required."}
        # Resolve a free-text name/alias to a page slug (skip if already a slug).
        if "/" not in start:
            hit = evomem_search(agent_id, start, limit=1, mode=_RECALL_SEARCH_MODE)
            if hit and hit.get("hits"):
                start = hit["hits"][0].get("slug", start)
        vlog("graph[%s]: traverse from %r (edge=%s hops=%d)",
             agent_id, start, edge_type or "*", hops)
        result = evomem_graph_query(agent_id, start, edge=edge_type, hops=hops)
        if not result or not isinstance(result.get("edges"), list) or not result["edges"]:
            vlog("graph[%s]: no connections from %r", agent_id, start)
            return {"start": start, "edges": [], "count": 0,
                    "result": "No connections found in the knowledge graph."}
        edges = [
            {"from": e.get("src_slug"), "edge": e.get("edge_type"),
             "to": e.get("dst_slug"), "hop": e.get("hop")}
            for e in result["edges"]
        ]
        vlog("graph[%s]: %d edges from %r", agent_id, len(edges), start)
        return {"start": result.get("start", start),
                "edges": edges, "count": len(edges)}
    except Exception as e:
        return {"error": f"Graph lookup failed: {e}"}


def forget_memory(agent_id: str, memory_id: int, target_agent_id: str = None,
                  is_super: bool = False) -> dict:
    """Soft-delete a specific memory. Used by the `forget_memory` built-in tool.

    Regular agents can only delete their own memories. Super agents can
    specify a target_agent_id to delete another agent's memory.
    """
    try:
        # Determine whose memory we're operating on
        effective_agent_id = target_agent_id if target_agent_id else agent_id

        # Authorization: only super agents can delete another agent's memories
        if target_agent_id and target_agent_id != agent_id and not is_super:
            return {
                "error": (
                    f"Cannot delete memory belonging to agent '{target_agent_id}'. "
                    "Only super agents can delete another agent's memories."
                )
            }

        # Verify the memory exists and belongs to the effective agent
        memories = db.get_all_memories(effective_agent_id, include_expired=True)
        target_memory = None
        for m in memories:
            if m['id'] == memory_id:
                target_memory = m
                break

        if not target_memory:
            return {
                "error": (
                    f"Memory {memory_id} not found for agent '{effective_agent_id}'."
                )
            }

        if target_memory.get('expired'):
            return {
                "error": f"Memory {memory_id} is already deleted.",
                "id": memory_id,
            }

        db.expire_memory(effective_agent_id, memory_id)

        # The FTS memories table is the source of truth for remembered facts;
        # expiring the row stops it surfacing. Knowledge docs are durable
        # authored content and are not auto-deleted here.
        return {
            "result": "Memory forgotten.",
            "id": memory_id,
            "content": target_memory['content'],
            "category": target_memory['category'],
        }
    except Exception as e:
        return {"error": f"Failed to forget memory: {e}"}


# ---- Helpers ----

def _strip_code_fences(text: str) -> str:
    text = text.strip()
    if text.startswith('```'):
        text = text.split('\n', 1)[-1]
        text = text.rsplit('```', 1)[0]
    return text.strip()


def _extract_last_user_query(messages: list) -> Optional[str]:
    for msg in reversed(messages):
        if msg.get('role') == 'user':
            content = msg.get('content')
            if isinstance(content, str) and content.strip():
                return content[:300]
    return None


def _sanitize_fts_query(query: str) -> str:
    """Build a safe FTS5 query from free text (avoid syntax errors)."""
    # Keep words longer than 2 chars, strip FTS5 special chars
    import re
    words = re.findall(r'[a-zA-Z0-9\u00C0-\u024F]{3,}', query)
    return ' '.join(words[:15])  # cap to avoid overly long queries
