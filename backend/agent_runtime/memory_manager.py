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
- Dual-write: store_memory() writes to both systems when evomem is enabled.

No new pip dependencies — uses existing LLM client, SQLite FTS5 (Python stdlib),
and the evomem static binary via subprocess.
"""

import os
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
            slug = evomem_writer.upsert_entity_page(
                agent_id, name, entity_type=ent.get("type", "entity"),
                aliases=ent.get("aliases") or [],
            )
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
    """Directly store a memory with conflict detection. Used by the `remember` built-in tool.

    When EVONIC_MEMORY_ENGINE=evomem, also dual-writes to the agent's evomem.
    """
    content = content.strip()
    if not content:
        return {"error": "Memory content cannot be empty."}
    try:
        result = _store_with_conflict_detection(agent_id, session_id, content, category)
        resp = {"result": "Memory stored.", "id": result['id'],
                "content": content, "category": category}
        if result['superseded']:
            resp["superseded_ids"] = result['superseded']
            resp["result"] = f"Memory stored (superseded {len(result['superseded'])} older memory/ies)."

        # Dual-write to evomem (non-blocking, best-effort)
        if get_engine() == "evomem":
            evomem_ok = _try_evomem_store(agent_id, content, category,
                                              memory_id=result['id'],
                                              session_id=session_id)
            if evomem_ok:
                resp["evomem"] = "stored"
            else:
                logger.debug("evomem dual-write failed for agent %s", agent_id)

        return resp
    except Exception as e:
        return {"error": f"Failed to store memory: {e}"}


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
