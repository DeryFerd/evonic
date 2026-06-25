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

_GRAPH_EXTRACT_PROMPT = """You build a knowledge graph from a conversation summary. Extract the named entities (people, organizations, projects, places) and the typed relationships between them.

Allowed relation types (use ONLY these): works_at, founded, invested_in, advises, attended, mentions.

Rules:
- Only extract relationships that are explicitly stated and factual (not speculative/planned/negated).
- Use real entity names as they appear (e.g. "Acme Corp", "Robin Syihab"). The user themselves is the entity "User".
- If a relationship doesn't fit one of the allowed types, skip it (or use "mentions" for a loose association).
- Return STRICT JSON only, no prose:
{{"entities": [{{"name": "...", "type": "person|organization|project|place", "aliases": ["..."]}}],
 "relations": [{{"subject": "...", "relation": "works_at", "object": "..."}}]}}
- If nothing to extract, return: {{"entities": [], "relations": []}}

Conversation summary:
{summary}

Return only the JSON object:"""

_CANONICALIZE_PROMPT = """You resolve entity coreference for a knowledge graph.

New entity name: "{name}"
Existing entity pages (candidates): {candidates}

If the new entity name refers to the SAME real-world entity as one of the candidates (e.g. a shorter name, nickname, fuller name, or alias of the same person/organization/project/place), return that candidate's name EXACTLY as written. If it is a different entity, or you are not sure, return null.

Return only the candidate name or null:"""


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


def _try_evomem_store(agent_id: str, content: str, category: str,
                        memory_id: int = None, session_id: str = None) -> bool:
    """Dual-write a memory to evomem as a STRUCTURED note page.

    Writes a `notes/` page (linked to the canonical `entities/user` for
    user-scoped facts so it becomes graph-adjacent), then schedules a debounced
    background sync. Returns True if the page was written.
    """
    engine = get_engine()
    if engine != "evomem":
        return False
    try:
        mentions = None
        if category in _USER_SCOPED:
            evomem_writer.upsert_entity_page(agent_id, "User",
                                               entity_type="person", tags=["user"])
            mentions = ["entities/user"]
        title = f"{category}: {content[:70]}"
        slug = evomem_writer.write_note(
            agent_id, title=title, body=content, tags=[category],
            mentions=mentions, memory_id=memory_id, source=session_id,
        )
        if slug:
            vlog("store[%s]: %s category=%s -> %s", agent_id,
                 ("user-linked" if mentions else "note"), category, slug)
            evomem_writer.mark_dirty(agent_id)
            return True
        return False
    except Exception:
        logger.debug("evomem structured store exception")
        return False


def _canonicalize_entity(name: str, candidates: list,
                         llm_lock: threading.Lock = None) -> Optional[str]:
    """Ask the LLM whether `name` is the same entity as one of `candidates`.

    Returns the matching candidate (exactly as given) or None when it is a
    different entity or the model is unsure. Conservative by design — an
    unsure answer yields a new page rather than a wrong merge.
    """
    if not candidates:
        return None
    prompt = _CANONICALIZE_PROMPT.format(name=name, candidates=json.dumps(candidates))
    try:
        call_kwargs = dict(
            messages=[{"role": "user", "content": prompt}],
            tools=None, temperature=0.0, enable_thinking=False, max_tokens=32,
        )
        if llm_lock:
            with llm_lock:
                result = llm_client.chat_completion(**call_kwargs)
        else:
            result = llm_client.chat_completion(**call_kwargs)
        if not result.get('success'):
            return None
        raw = result['response']['choices'][0]['message']['content']
        raw, _ = strip_thinking_tags(raw)
        raw = raw.strip().strip('"').strip("'")
        if raw.lower() == 'null' or not raw:
            return None
        # Only accept an exact (case-insensitive) match to a candidate.
        for c in candidates:
            if c.lower() == raw.lower():
                return c
        return None
    except Exception:
        return None


def _resolve_existing_entity(agent_id: str, name: str,
                             llm_lock: threading.Lock = None) -> Optional[str]:
    """Resolve `name` to the title of an existing entity page for the SAME
    real-world entity (cross-session coreference), or None to create a new page.

    Searches the synced evomem entity pages; merges only on a strong match
    (identical slug) or an explicit LLM confirmation for differing-slug
    candidates. Defaults to None (new page) when uncertain.
    """
    try:
        res = evomem_search(agent_id, name, limit=5, mode=_RECALL_SEARCH_MODE)
    except Exception:
        return None
    if not res or not isinstance(res.get("hits"), list):
        return None

    name_slug = evomem_writer.slugify(name)
    candidates = []
    for h in res["hits"]:
        if h.get("source_dir") != "entities":
            continue
        title = (h.get("title") or "").strip()
        if not title:
            continue
        # Identical slug => upsert already lands on the same page (not a
        # fragmentation case); adopt the existing title for stable casing.
        if evomem_writer.slugify(title) == name_slug:
            return title
        candidates.append(title)
    if not candidates:
        return None
    return _canonicalize_entity(name, candidates, llm_lock)


def _extract_and_store_graph(agent_id: str, summary: str,
                             llm_lock: threading.Lock) -> None:
    """Extract entities + typed relations from a summary and wire the graph.

    Best-effort, runs in the background extraction thread. Any failure is
    swallowed so flat FTS5/note storage is never affected.
    """
    if get_engine() != "evomem":
        return
    try:
        prompt = _GRAPH_EXTRACT_PROMPT.format(summary=summary)
        with llm_lock:
            result = llm_client.chat_completion(
                messages=[{"role": "user", "content": prompt}],
                tools=None, temperature=0.0, enable_thinking=False, max_tokens=1024,
            )
        if not result.get('success'):
            return
        raw = result['response'].get('choices', [{}])[0].get('message', {}).get('content', '')
        raw, _ = strip_thinking_tags(raw)
        raw = _strip_code_fences(raw)
        data = json.loads(raw)
        if not isinstance(data, dict):
            return

        # Map entity name -> slug (so relations can reference the same page).
        name_to_slug = {}
        for ent in data.get("entities", []):
            if not isinstance(ent, dict):
                continue
            name = (ent.get("name") or "").strip()
            if not name:
                continue
            entity_type = ent.get("type", "entity")
            aliases = ent.get("aliases") or []
            # Cross-session coreference: fold a variant name ("Robin") into the
            # existing canonical page ("Robin Syihab") as an alias instead of
            # creating a duplicate node.
            canonical = _resolve_existing_entity(agent_id, name, llm_lock)
            if canonical and evomem_writer.slugify(canonical) != evomem_writer.slugify(name):
                slug = evomem_writer.upsert_entity_page(
                    agent_id, canonical, entity_type=entity_type,
                    aliases=[name, *aliases])
                if slug:
                    name_to_slug[name.lower()] = slug
                    name_to_slug[canonical.lower()] = slug
            else:
                slug = evomem_writer.upsert_entity_page(
                    agent_id, name, entity_type=entity_type, aliases=aliases)
                if slug:
                    name_to_slug[name.lower()] = slug

        wrote_edge = False
        for rel in data.get("relations", []):
            if not isinstance(rel, dict):
                continue
            subj = (rel.get("subject") or "").strip()
            obj = (rel.get("object") or "").strip()
            relation = (rel.get("relation") or "").strip()
            if not subj or not obj or not relation:
                continue
            subj_slug = name_to_slug.get(subj.lower()) or \
                evomem_writer.upsert_entity_page(agent_id, subj)
            obj_slug = name_to_slug.get(obj.lower()) or \
                evomem_writer.upsert_entity_page(agent_id, obj)
            if subj_slug and obj_slug:
                if evomem_writer.add_edge(agent_id, subj_slug, relation, obj_slug,
                                            anchor=obj):
                    wrote_edge = True

        vlog("graph-extract[%s]: %d entities, %d relations%s", agent_id,
             len(name_to_slug), len(data.get("relations", []) or []),
             " (edges wired)" if wrote_edge else "")
        if name_to_slug or wrote_edge:
            evomem_writer.mark_dirty(agent_id)
    except (json.JSONDecodeError, KeyError, ValueError):
        return
    except Exception:
        logger.debug("evomem graph extraction exception (non-fatal)")
        return


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

    # Keep evomem consistent: drop superseded notes from disk so the stale
    # fact stops surfacing via evomem. (delete_note is a no-op if absent.)
    if superseded_ids and get_engine() == "evomem":
        removed = False
        for old_id in superseded_ids:
            if evomem_writer.delete_note(agent_id, old_id):
                removed = True
        if removed:
            evomem_writer.mark_dirty(agent_id)

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

        # Build the knowledge graph (entities + typed edges) from the summary.
        # Independent of the flat-fact storage below; best-effort, off the hot path.
        _extract_and_store_graph(agent_id, summary, llm_lock)

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
                                # Rewrite the evomem note so its content does
                                # not drift from FTS5 (write_note upserts by id).
                                if get_engine() == "evomem":
                                    _try_evomem_store(
                                        agent_id, op['content'].strip(),
                                        op.get('category', 'general'),
                                        memory_id=int(op['id']), session_id=session_id)
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

_KB_EXTRACT_PROMPT = """You extract durable knowledge from a conversation summary to store in an AI agent's knowledge base (KB) for reuse in future conversations.

{guidance}

Rules:
- Extract self-contained, reusable knowledge items: facts, procedures, decisions, domain/business info, preferences, persistent instructions.
- Skip ephemeral chatter, pleasantries, and transient task status.
- Each item has a short Title (the topic) and Content (1-5 factual sentences, English).
- Group related facts under one item/title instead of splitting hairs.
- Return a JSON array only: [{{"title": "...", "content": "...", "tags": ["..."]}}]
- If nothing is worth keeping long-term, return: []

Conversation summary:
{summary}

Return only the JSON array:"""

_KB_DECIDE_PROMPT = """An AI agent is filing a new knowledge item into its KB. Avoid duplicate information.

New item:
Title: {title}
Content: {content}

Existing related KB pages (most relevant first):
{candidates}

Decide one:
- "skip": the new item is already fully covered by an existing page (adds no new information).
- "merge": it belongs on an existing page (same topic) — give that page's slug.
- "create": it is a genuinely new topic.

Return only JSON: {{"action": "skip|merge|create", "slug": "<existing slug, only if merge>"}}"""

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


def _read_kb_page(agent_id: str, slug: str):
    """Return {'title', 'body'} for a top-level KB page on disk, or None."""
    base = slug.split("/", 1)[-1]
    path = os.path.join(f"agents/{agent_id}/kb", f"{base}.md")
    try:
        with open(path, encoding="utf-8") as f:
            raw = f.read()
    except OSError:
        return None
    fm, body = evomem_writer._parse_frontmatter(raw)
    return {"title": fm.get("title") or base, "body": body.strip()}


def _find_related_kb(agent_id: str, title: str, content: str, limit: int = 6):
    """Find related TOP-LEVEL KB pages (slug without '/') as merge candidates.

    Combines a deterministic title-slug match (so a same-titled page is ALWAYS
    found, regardless of the weak semantic ranker — the main fix for missed
    merges) with two hybrid searches (title+content, then title alone). De-duped
    by slug; the exact slug match is surfaced first.
    """
    out, seen = [], set()

    def _add(slug, title_, snippet):
        slug = (slug or "").strip()
        if not slug or "/" in slug or slug in seen:
            return
        seen.add(slug)
        out.append({"slug": slug, "title": title_ or slug,
                    "snippet": (snippet or "").strip()[:240]})

    # 1) deterministic: a page whose slug == slugify(title) already exists
    tslug = evomem_writer.slugify(title)
    if tslug:
        page = _read_kb_page(agent_id, tslug)
        if page is not None:
            _add(tslug, page["title"], page["body"])

    # 2) semantic: title+content and title-only, merged for recall
    for q in (f"{title}. {content}", title):
        try:
            res = evomem_search(agent_id, q, limit=limit * 2, mode=_RECALL_SEARCH_MODE)
        except Exception:
            res = None
        if res and isinstance(res.get("hits"), list):
            for h in res["hits"]:
                _add(h.get("slug"), h.get("title"), h.get("snippet"))
        if len(out) >= limit:
            break
    return out[:limit]


_MENTION_LINE = re.compile(r"^\s*\[\[[^\]]+\]\]\s*$")


def _strip_trailing_mentions(body: str) -> str:
    """Drop trailing bare [[wiki-link]] lines so new prose isn't appended after them."""
    lines = body.rstrip().split("\n")
    while lines and _MENTION_LINE.match(lines[-1]):
        lines.pop()
    return "\n".join(lines).rstrip()


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


def _merge_into_page(agent_id: str, slug: str, new_content: str, tags: list,
                     mentions: list, llm_lock: threading.Lock) -> bool:
    """Non-destructively append only the NEW delta to an existing KB page.

    Reads the page, asks the LLM for just the information not already present,
    and appends it — existing content is never rewritten, so nothing is lost and
    large pages can't be truncated. Serialised per page. Returns True if the page
    changed; False if already covered, missing, or on error.
    """
    with _kb_page_lock(agent_id, slug):
        page = _read_kb_page(agent_id, slug)
        if page is None:
            return False
        snippet = _kb_llm_text(
            _KB_MERGE_SNIPPET_PROMPT.format(existing=page["body"], content=new_content),
            llm_lock)
        if not snippet or snippet.strip().upper() == "NONE":
            vlog("kb-extract[%s]: %s already covers item (no-op)", agent_id, slug)
            return False
        base = _strip_trailing_mentions(page["body"])
        new_body = (base + "\n\n" + snippet).strip()
        m = [x for x in mentions if x != slug and f"[[{x}]]" not in new_body]
        ok = evomem_writer.upsert_kb_page(
            agent_id, title=page["title"], body=new_body, tags=tags,
            slug=slug, mentions=m)
        if ok:
            vlog("kb-extract[%s]: appended delta to %s", agent_id, slug)
        return bool(ok)


def extract_and_store_kb(agent: dict, session_id: str, summary: str,
                          llm_lock: threading.Lock) -> None:
    """Extract durable knowledge from a summary and file it into the agent's KB.

    For each knowledge item: find related KB pages, then skip (duplicate),
    merge into an existing page, or create a new linked page. Pages connect via
    bare `[[slug]]` wiki-links. Runs in a background thread; non-fatal on error.
    Requires the evomem engine (the KB lives in the evomem knowledge root).
    """
    agent_id = agent['id']
    if get_engine() != "evomem":
        return
    try:
        guidance = (agent.get('summarize_prompt') or '').strip() or _DEFAULT_KB_GUIDANCE
        items = _kb_llm_json(
            _KB_EXTRACT_PROMPT.format(guidance=guidance, summary=summary), llm_lock)
        if not isinstance(items, list) or not items:
            return
        items = [it for it in items
                 if isinstance(it, dict) and it.get('content', '').strip()
                 and it.get('title', '').strip()][:10]

        wrote = False
        for it in items:
            title = it['title'].strip()
            content = it['content'].strip()
            tags = [t for t in (it.get('tags') or []) if isinstance(t, str) and t.strip()]

            related = _find_related_kb(agent_id, title, content, limit=6)
            decision = None
            if related:
                cand_text = "\n".join(
                    f"[{r['slug']}] {r['title']} :: {r['snippet']}" for r in related)
                decision = _kb_llm_json(
                    _KB_DECIDE_PROMPT.format(title=title, content=content,
                                             candidates=cand_text),
                    llm_lock)
            action = (decision or {}).get('action', 'create')
            mentions = [r['slug'] for r in related][:4]

            if action == 'skip':
                vlog("kb-extract[%s]: skip duplicate %r", agent_id, title[:50])
                continue

            # Merge into the chosen page (append-only, locked).
            if action == 'merge' and (decision or {}).get('slug'):
                if _merge_into_page(agent_id, decision['slug'].strip(),
                                    content, tags, mentions, llm_lock):
                    wrote = True
                continue

            # Create — guard the title slug so a concurrent or colliding page is
            # appended to rather than clobbered (atomic check-then-write).
            cslug = evomem_writer.slugify(title)
            if not cslug:
                continue
            with _kb_page_lock(agent_id, cslug):
                if _read_kb_page(agent_id, cslug) is None:
                    if evomem_writer.upsert_kb_page(
                            agent_id, title=title, body=content, tags=tags,
                            mentions=mentions):
                        vlog("kb-extract[%s]: created %r (links=%d)",
                             agent_id, title[:50], len(mentions))
                        wrote = True
                    continue
            # A page already exists under this slug → append-merge instead.
            if _merge_into_page(agent_id, cslug, content, tags, mentions, llm_lock):
                wrote = True

        if wrote:
            evomem_writer.mark_dirty(agent_id)

    except Exception as e:
        print(f"[MemoryManager] KB extraction failed for agent {agent_id} (non-fatal): {e}")


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

    Rather than writing to long-term memory directly, this appends the fact to
    the session's running summary so it is (a) immediately visible in the
    agent's context for the rest of the session and (b) later folded into
    long-term memory + the knowledge graph by the background summarizer.
    Summarization is incremental (it feeds the existing summary back into the
    prompt as "Existing summary to update"), so the noted fact survives and is
    picked up by extract_and_store_memories / _extract_and_store_graph.

    No LLM call and no direct long-term write happen here — that work belongs to
    the summarizer, the single writer to FTS5/evomem/graph.
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
        return {"result": "Noted for this session.", "content": content}
    except Exception as e:
        return {"error": f"Failed to note fact: {e}"}


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
    """Brain-layer synthesis over memory. Backs the `think` built-in tool.

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
    """Traverse the knowledge graph from an entity. Backs the `graph_query` tool.

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

        # Keep evomem consistent: drop the structured note from disk and
        # schedule a sync so the page is soft-deleted from the index. Without
        # this the "forgotten" fact would still surface via evomem.
        resp = {
            "result": "Memory forgotten.",
            "id": memory_id,
            "content": target_memory['content'],
            "category": target_memory['category'],
        }
        if get_engine() == "evomem":
            if evomem_writer.delete_note(effective_agent_id, memory_id):
                evomem_writer.mark_dirty(effective_agent_id)
                resp["evomem"] = "removed"
        return resp
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
