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
import time
import difflib
import logging
import threading
from datetime import datetime, timezone
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
# decide if a variant name ("Andi") is the same as an existing page ("Andi
# Wijaya"). On a slow/thinking model that dominates graph-extraction latency, so
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
- "User's name is Andi" → "user.name"
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
- If the SOURCE mentions or includes relevant photos/images, embed them inline in the `body` using Markdown: `![brief description](image-url)`. Only include photos that are directly relevant to the document's subject — skip unrelated or generic images.
- Choose a `type` for each document from: note, person, place, venue, event, organization, company, product, contact.
- The user is always referred to as "User".

Deduplication:
- You are given EXISTING documents as `[slug] Title :: description`. If a subject is already covered by one, return action "update" with its `slug` and a `body` containing ONLY the genuinely NEW information to add (its existing prose is preserved). Otherwise return action "create" with a full `body` and a one-line `description`.

Skip ephemeral chatter, pleasantries, and transient/in-progress task status.

Return STRICT JSON only, no prose. Each doc may optionally include an `images` array of relevant photo URLs to attach:
{{"docs": [
  {{"action": "create", "title": "Jakarta", "type": "place", "description": "Capital of Indonesia; User's home city.", "tags": ["place"], "images": [], "body": "Jakarta adalah ibu kota Indonesia. User tinggal di [[Pesanggrahan]]."}},
  {{"action": "update", "slug": "<existing slug>", "title": "...", "type": "note", "description": "...", "tags": ["..."], "images": [], "body": "<only the new prose to append, with inline [[links]]>"}}
]}}
If nothing is worth keeping long-term, return: {{"docs": []}}

EXISTING documents:
{existing}

SOURCE:
{source}

Return only the JSON object:"""


# ---- Knowledge Organizer sub-agent -----------------------------------------
# A librarian sub-agent that organizes incoming knowledge into the vault. It is a
# real agent: its WORKSPACE is the KB directory, so it EXPLORES the vault itself
# with Grep/Glob/Read — we do NOT pre-list docs for it. It returns the write-ops
# the pipeline applies. Single-brace JSON below is literal (NOT .format()'d).
_KB_ORGANIZER_SYSTEM_PROMPT = """You are the Knowledge Organizer — a librarian who keeps an AI assistant's personal knowledge vault tidy. The vault is YOUR WORKSPACE: a directory of Markdown docs, each with YAML frontmatter (title, aliases, type, description, tags) and a prose body with inline [[Wiki Links]]. There is NO pre-made index — use your tools (Glob, Grep, Read) to explore the vault and discover what already exists.

NAMING — the `title` is the subject's PURE canonical name ONLY. Put nicknames, abbreviations, gelar, or short forms in `aliases`, NEVER in the title (the title becomes the filename, so it must stay clean). Examples:
  - title "Abdurrahman Wahid", aliases ["Gus Dur"]   (NOT title "Abdurrahman Wahid (Gus Dur)")
  - title "Susilo Bambang Yudhoyono", aliases ["SBY"]
The aliases let other names still resolve to this one doc.

LINKING IS MANDATORY — every named entity you mention in a `body` MUST be wrapped in an inline [[Wiki Link]]: people, places, organizations, companies, products, venues, events. This is how the knowledge graph connects — a body with no links is WRONG. ESPECIALLY every PERSON's name MUST be a [[link]] — never leave a person as plain text. Link the entity's canonical name, e.g. "User bertemu [[Budi Santoso]] di [[Jakarta]] di kantor [[Nuwaira]]." Do NOT wrap "User" in a link.

IMAGES — there is NO separate images field. If the conversation provides a relevant photo/image URL for a subject, you MUST embed it INLINE in that doc's `body` as Markdown: `![brief description](image-url)` (put it near the top of the body or in the relevant paragraph). An image is saved ONLY if it is in the `body` — a URL placed anywhere else is lost. Only embed photos directly relevant to the subject; skip generic/unrelated ones.

Given the conversation below, file any durable, long-term knowledge into the vault. For each subject:
- EXPLORE to learn whether the vault already has a doc for it. Search by the name AND its likely variants, aliases, abbreviations, or fuller/shorter forms — the SAME real-world entity must live in ONE doc even under a different name. Examples: "Stasiun Gambir" is the doc "Gambir"; "Pak Andi" is "Andi Wijaya"; "BNI" is "Bank Negara Indonesia". Grep is case-insensitive; search `title:`/`aliases:` lines and bodies, and try bare head-nouns / stripped prefixes (Stasiun, Pak/Bu/Mr, PT/CV).
- KNOWN subject → UPDATE its doc: use its real vault slug (its path, no .md), Read the body first, and add ONLY the genuinely-new info.
- NEW subject → CREATE a doc: pure-name title, nicknames in aliases, full prose with inline [[links]] to every other named subject.
- WRONG inline [[link]] or a factual error in an existing doc → EDIT it: action "edit" with the doc's `slug`, a SHORT `old_str` (one phrase, sentence, or a single [[link]] copied verbatim — keep it short and UNIQUE so the fix can't hit the wrong place), and `new_str`. E.g. old_str "[[Gus Dur]]" → new_str "[[Abdurrahman Wahid]]". Edit targets the BODY prose ONLY — do NOT use it to change frontmatter (title/type/aliases/tags) or to blank out a whole doc.
- RETRO-LINK existing docs (IMPORTANT — frequently the MOST valuable work, and easy to miss): once a subject HAS a doc (you just created it, or it already existed), OTHER docs often still mention that subject as PLAIN TEXT because its doc didn't exist when they were written. Your job is to connect them: Grep the vault for the subject's name + aliases, and for each bare mention in another doc's body that is NOT already inside [[ ]], EDIT that doc (action "edit") to wrap it as a [[link]]. Keep `old_str` short and UNIQUE (include a couple of surrounding words so it matches exactly one spot). E.g. old_str "Komisaris Independen Grace Natalie" → new_str "Komisaris Independen [[Grace Natalie]]". Leaving a plain-text mention of an entity that HAS a doc is WRONG — emit one "edit" op per doc that needs linking (it is normal and good for a run to return ONLY edit ops, even when nothing new is created).
- DANGLING [[link]] (a link whose target doc does NOT exist under that exact name) → RECONCILE it: the SAME entity may already live in a doc under a DIFFERENT name (canonical title vs. nickname/abbrev/fuller-or-shorter form). Grep `title:`/`aliases:` and bodies for the linked text and its variants; if you find the matching doc, FIX it — PREFER adding the variant to that doc's `aliases` (an "update" op with its slug + the augmented `aliases` list) so the natural phrasing keeps resolving; otherwise rewrite the link to the doc's canonical title with an "edit" op (e.g. old_str "[[Gus Dur]]" → new_str "[[Abdurrahman Wahid]]"). Only if NO related doc exists and the entity is durable, CREATE one. Don't leave links pointing at nothing.
- WRONG doc name/slug (a typo, OR a slug polluted with a nickname like "abdurrahman-wahid-gus-dur") → RENAME it: action "rename" with the existing `slug` and the corrected PURE `new_title` (move the nickname to aliases). The old name is kept as an alias so existing [[links]] still resolve; you may also include `body` to add new info.
- DUPLICATED CONTENT in an existing doc (the same section, or the whole body, appears TWICE — e.g. it was concatenated to itself) → DEDUPE it: action "dedupe" with just the doc's `slug`. This safely removes verbatim repeated blocks; do NOT try to fix duplication with `edit`.
- Skip ephemeral chatter and transient task status. If a subject is genuinely ambiguous, OMIT it — better to skip than to create a duplicate.

OUTPUT — STRICT JSON ONLY (no prose, no code fences, no thinking):
{"docs":[
 {"action":"update","slug":"<confirmed existing slug>","title":"<existing title>","type":"<existing type>","description":"<one-line; reuse if unchanged>","tags":["..."],"aliases":["<nickname/abbrev, else omit>"],"body":"<ONLY new prose to append, inline [[Links]] + inline ![desc](url) for any relevant photo; don't restate existing>"},
 {"action":"create","title":"<PURE canonical name, no nickname>","type":"place","description":"<one-line>","tags":["place"],"aliases":["<nickname/abbrev, e.g. Gus Dur / SBY, else omit>"],"body":"<FULL prose, inline [[Links]] for every OTHER named entity, and inline ![desc](url) for any relevant photo>"},
 {"action":"edit","slug":"<existing slug>","old_str":"<EXACT unique text to replace>","new_str":"<replacement text>"},
 {"action":"rename","slug":"<existing slug>","new_title":"<corrected PURE name>","body":"<optional new prose>"},
 {"action":"dedupe","slug":"<existing slug with duplicated content>"}
]}
RULES: the user is always "User"; title=pure name, nicknames→aliases; update=delta-only body + existing slug; create=full body, NO slug; edit=existing slug + UNIQUE old_str + new_str (also used to RETRO-LINK plain-text mentions in other docs to a subject that now has a doc); rename=existing slug + corrected new_title; dedupe=existing slug; there is NO images field — embed any relevant photo INLINE in the body as ![desc](url); valid types: note, person, place, venue, event, organization, company, product, contact (use "event" for a dated happening). BEFORE returning an empty list, do the retro-link check: for every subject that has a doc, Grep other docs for un-linked plain-text mentions and emit "edit" ops to wrap them. Return {"docs": []} ONLY when there is genuinely nothing to create, update, OR link.

Your ENTIRE reply MUST be the JSON object and nothing else: start with `{` and end with `}`. Do NOT write any reasoning, notes, "Key findings", explanations, or commentary before or after the JSON. Begin your reply with `{` immediately."""


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


def _kb_doc_listing(agent_id: str, limit: int = 400):
    """Walk the server-local KB and return ``(listing_text, name_to_slug)``.

    ``listing_text``: one line per doc — ``[slug] Title (aka a1; a2) :: description`` —
    shown to the resolver (and the legacy author) as the COMPLETE set of existing
    docs (replaces the old top-80 ranked search that hid docs from the LLM).
    ``name_to_slug``: ``{casefold(title|alias): slug}`` — the create-collision guard
    that keeps an exact title/alias dupe from being minted even if the resolver errs.
    """
    kb_dir = f"agents/{agent_id}/kb"
    if not os.path.isdir(kb_dir):
        return "(none yet)", {}
    lines, name_to_slug, count = [], {}, 0
    for root, dirs, fnames in os.walk(kb_dir):
        dirs[:] = [d for d in dirs if not d.startswith('.') and d != 'inbox']
        for fn in sorted(fnames):
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
            title = (fm.get('title') or slug.rsplit('/', 1)[-1]).strip()
            desc = (fm.get('description') or '')[:160]
            aliases = fm.get('aliases') or []
            if isinstance(aliases, str):
                aliases = [aliases]
            aliases = [a for a in aliases if isinstance(a, str) and a.strip()]
            alias_str = f" (aka {'; '.join(aliases)})" if aliases else ""
            lines.append(f"[{slug}] {title}{alias_str} :: {desc}")
            if title:
                name_to_slug.setdefault(title.casefold(), slug)
            for a in aliases:
                name_to_slug.setdefault(a.casefold(), slug)
            count += 1
            if count >= limit:
                return ("\n".join(lines), name_to_slug)
    return ("\n".join(lines) or "(none yet)", name_to_slug)


def _resolve_doc_slug2(agent_id: str, slug_hint: str, title: str, folder: str,
                       name_to_slug: dict = None):
    """Resolve to an existing doc slug to update, or None to create a new doc.

    Tries, in order: the model's slug hint → an exact title/alias match in
    ``name_to_slug`` → the title-slug inside the active folder → the title-slug at
    root. The first that exists on disk wins. Adds title/alias resolution that the
    old slug-only ``_resolve_doc_slug`` lacked (the duplicate-doc root cause).
    """
    folder = (folder or "").strip("/")
    cands = []
    if slug_hint:
        cands.append(slug_hint.strip("/"))
    if name_to_slug:
        hit = name_to_slug.get((title or "").strip().casefold())
        if hit:
            cands.append(hit)
    tslug = evomem_writer.slugify(title)
    if tslug:
        if folder:
            cands.append(f"{folder}/{tslug}")
        cands.append(tslug)
    seen = set()
    for c in cands:
        if c and c not in seen:
            seen.add(c)
            if evomem_writer.read_doc(agent_id, c) is not None:
                return c
    return None


def _iter_balanced_json(text: str):
    """Yield every balanced top-level ``{...}`` substring in ``text``.

    String/escape-aware, so braces inside string literals don't break balancing.
    Lets us pull the real JSON object out of prose that also contains braces
    (e.g. "Here's the result: {...}. I've filed it.")."""
    depth, start, in_str, esc = 0, -1, False, False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == '\\':
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == '{':
            if depth == 0:
                start = i
            depth += 1
        elif ch == '}' and depth > 0:
            depth -= 1
            if depth == 0 and start != -1:
                yield text[start:i + 1]
                start = -1


# Smart/curly double-quotes LLMs sometimes emit for the JSON structure itself
# (e.g. {“docs“:[]}), which json.loads rejects. Normalized only as a fallback.
_SMART_DQUOTES = {0x201C: '"', 0x201D: '"', 0x201E: '"', 0x201F: '"', 0x2033: '"'}


def _loads_lenient(s: str):
    """json.loads, tolerant of the most common LLM JSON slips:
    - literal control chars (newlines/tabs) INSIDE string values — very common for
      multi-line ``body``/``old_str`` content — handled via ``strict=False``;
    - a trailing comma before ``}``/``]``;
    - smart/curly double-quotes used for the JSON structure (tried LAST so valid
      JSON with curly quotes inside string values is parsed verbatim first).
    """
    smart = s.translate(_SMART_DQUOTES)
    variants = (
        s,
        re.sub(r',(\s*[}\]])', r'\1', s),
        smart,
        re.sub(r',(\s*[}\]])', r'\1', smart),
    )
    for variant in variants:
        try:
            return json.loads(variant, strict=False)
        except (json.JSONDecodeError, ValueError):
            continue
    return None


def _coerce_docs(data):
    """Normalize parsed JSON into a list of doc-ops, or None if it isn't one.

    Accepts the canonical ``{"docs": [...]}``, a bare ``[...]`` array of ops, or a
    single bare op object — so a model that drops the wrapper still works."""
    if isinstance(data, dict):
        if isinstance(data.get("docs"), list):
            return data["docs"]
        if data.get("action") or data.get("title"):  # a single, unwrapped op
            return [data]
    if isinstance(data, list) and all(isinstance(x, dict) for x in data):
        return data
    return None


def _reconstruct_json(text: str) -> str:
    """Best-effort RECONSTRUCTION of malformed JSON (last resort) — rebuilds a
    parseable string from the first top-level ``{``/``[`` to its close, fixing the
    common ways an LLM breaks JSON:

    - unescaped inner double-quotes inside string values (escaped via a lookahead:
      a ``"`` is a closing quote only if the next non-space char is ``,:}]`` or EOF,
      otherwise it's an inner quote);
    - stray literal control chars (newline/tab/CR) inside strings -> escaped;
    - trailing commas -> dropped;
    - TRUNCATED output -> any unterminated string and still-open ``{[`` are closed.

    Leading prose and anything after the top-level container are ignored. Returns a
    candidate string (not guaranteed valid) for one more parse attempt.
    """
    start = next((k for k, c in enumerate(text) if c in '{['), -1)
    if start == -1:
        return ''
    out, stack, in_str, esc, started = [], [], False, False, False
    i, n = start, len(text)
    _ctrl = {'\n': '\\n', '\r': '\\r', '\t': '\\t'}
    while i < n:
        c = text[i]
        if in_str:
            if esc:
                out.append(c); esc = False
            elif c == '\\':
                out.append(c); esc = True
            elif c == '"':
                j = i + 1
                while j < n and text[j] in ' \t\r\n':
                    j += 1
                if j >= n or text[j] in ',:}]':
                    out.append('"'); in_str = False          # closing quote
                else:
                    out.append('\\"')                        # inner quote -> escape
            elif c in _ctrl:
                out.append(_ctrl[c])                          # escape literal control char
            else:
                out.append(c)
        elif c == '"':
            in_str = True; out.append(c)
        elif c in '{[':
            stack.append(c); started = True; out.append(c)
        elif c in '}]':
            if stack:
                stack.pop()
            out.append(c)
            if started and not stack:
                break                                        # top-level container closed
        else:
            out.append(c)
        i += 1
    if in_str:
        out.append('"')                                      # close an unterminated string
    repaired = re.sub(r',(\s*[}\]])', r'\1', ''.join(out))   # drop trailing commas
    while stack:                                             # close truncated containers
        repaired += ']' if stack.pop() == '[' else '}'
    return repaired


def _parse_organizer_docs(findings: str):
    """Extract the ``docs`` list of write-ops from the organizer's free-text answer.

    Robust to thinking tags, code fences, surrounding prose, multiple JSON objects,
    trailing commas, literal control chars, and a missing ``{"docs": ...}`` wrapper.
    As a LAST resort, reconstructs malformed/truncated JSON. Returns the list
    (possibly empty) on success, or ``None`` if nothing usable can be recovered.
    """
    if not findings or not isinstance(findings, str):
        return None
    raw, _ = strip_thinking_tags(findings)
    raw = _strip_code_fences(raw).strip()
    # 1) Try the whole payload, then each balanced {...} object embedded in prose.
    for candidate in (raw, *_iter_balanced_json(raw)):
        data = _loads_lenient(candidate)
        if data is not None:
            docs = _coerce_docs(data)
            if docs is not None:
                return docs
    # 2) Last resort: reconstruct malformed/truncated JSON and parse that.
    data = _loads_lenient(_reconstruct_json(raw))
    if data is not None:
        docs = _coerce_docs(data)
        if docs is not None:
            return docs
    return None


def _kb_organizer_infra_ok() -> bool:
    """True if the DirExplorer worker skill (the organizer's tools) is enabled."""
    try:
        from backend.agent_runtime import explorer
        return explorer.worker_skill_enabled()
    except Exception:
        return False


def _format_recent_messages(recent_messages, n: int = 6, max_chars: int = 4000) -> str:
    """Format the last ``n`` conversation turns as 'User: ... / Assistant: ...' text.

    Gives the organizer the verbatim recent turns (the summarizer's ``tail_messages``,
    not yet folded into the summary) so it doesn't lose context that a thin summary or
    single fact would miss.
    """
    if not recent_messages:
        return ""
    lines = []
    for m in recent_messages[-n:]:
        if not isinstance(m, dict):
            continue
        role = m.get('role') or ('user' if m.get('type') == 'user' else 'assistant')
        content = (m.get('content') or '').strip()
        if not content:
            continue
        lines.append(f"{'User' if role == 'user' else 'Assistant'}: {content}")
    text = "\n".join(lines)
    return text[-max_chars:] if len(text) > max_chars else text


# ── KB organizer delta: feed only what's new since the organizer last filed ──
# The rolling summary is LLM-regenerated (and re-lists every entity) on each
# summarization, so handing the organizer the whole thing every run buries the
# genuinely-new bits. Instead we diff the current summary against the snapshot
# the organizer last successfully processed (an organizer-OWN cursor, robust to
# the debounce skips), and feed only the changed regions + a little context.

def _delta_enabled() -> bool:
    return os.environ.get('EVOMEM_KB_ORGANIZER_DELTA', 'on').strip().lower() \
        not in ('0', 'off', 'no', 'false')


def _delta_context() -> int:
    """Lines of surrounding context to keep around each changed hunk."""
    try:
        return max(0, int(os.environ.get('EVOMEM_KB_ORGANIZER_DELTA_CONTEXT', '2')))
    except (TypeError, ValueError):
        return 2


def _delta_min_score() -> int:
    """Minimum delta score (count of genuinely-new content lines) required to
    bother spawning the organizer. Below this, the run is skipped and the cursor
    is left un-advanced so small changes accumulate until they're worth a run."""
    try:
        return max(0, int(os.environ.get('EVOMEM_KB_ORGANIZER_DELTA_MIN', '3')))
    except (TypeError, ValueError):
        return 3


def _summary_delta(prev: str, curr: str, context: int):
    """Return ``(delta_text, score)`` describing what's new in ``curr`` vs ``prev``.

    ``delta_text`` is the inserted/replaced regions of ``curr``, each prefixed with
    its nearest preceding markdown header and padded with ``context`` lines, hunks
    joined by an ``…`` separator. ``score`` counts the new-side lines whose stripped
    content is non-empty AND absent from ``prev`` — so pure reordering / re-listing
    of existing entities does not inflate it.

    ``prev`` empty (cold start) → ``("", 0)``: the caller feeds the full summary and
    skips the score gate so the vault still gets seeded.
    """
    if not (prev or "").strip() or not (curr or "").strip():
        return "", 0

    prev_lines = prev.splitlines()
    curr_lines = curr.splitlines()
    prev_set = {ln.strip() for ln in prev_lines if ln.strip()}

    sm = difflib.SequenceMatcher(None, prev_lines, curr_lines, autojunk=False)
    score = 0
    ranges = []  # (start, end) on curr_lines, context-padded
    for tag, _i1, _i2, j1, j2 in sm.get_opcodes():
        if tag not in ('insert', 'replace'):
            continue
        for ln in curr_lines[j1:j2]:
            s = ln.strip()
            if s and s not in prev_set:
                score += 1
        ranges.append((max(0, j1 - context), min(len(curr_lines), j2 + context)))

    if not ranges:
        return "", score

    # Merge overlapping/adjacent padded ranges.
    ranges.sort()
    merged = [list(ranges[0])]
    for s, e in ranges[1:]:
        if s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])

    def _header_above(idx):
        for k in range(idx - 1, -1, -1):
            if curr_lines[k].lstrip().startswith('#'):
                return curr_lines[k]
        return None

    out = []
    last_header = None  # the section header most recently emitted
    for s, e in merged:
        chunk = curr_lines[s:e]
        hdr = _header_above(s)
        if out:
            out.append("…")  # separator between non-adjacent hunks
        # Prepend the section header only when it differs from the one already
        # emitted — otherwise every hunk inside the same section would repeat
        # "## Entities & Relationships" (the bug seen in organizer prompts).
        if hdr and hdr != last_header and hdr not in chunk:
            out.append(hdr)
        out.extend(chunk)
        inner_headers = [ln for ln in chunk if ln.lstrip().startswith('#')]
        if inner_headers:
            last_header = inner_headers[-1]
        elif hdr:
            last_header = hdr

    return ("\n".join(out).strip(), score)


def _organized_snapshot_key(agent_id: str, session_id: str) -> str:
    return f"kb_organizer_last_summary:{agent_id}:{session_id}"


def _load_organized_snapshot(agent_id: str, session_id: str) -> str:
    """Summary the organizer last successfully processed for this (agent, session)."""
    try:
        return db.get_setting(_organized_snapshot_key(agent_id, session_id)) or ""
    except Exception:  # noqa: BLE001 — best-effort; treat as cold start
        return ""


def _save_organized_snapshot(agent_id: str, session_id: str, summary: str) -> None:
    """Advance the organizer cursor to the full current summary (the new baseline)."""
    try:
        db.set_setting(_organized_snapshot_key(agent_id, session_id), summary or "")
    except Exception as e:  # noqa: BLE001 — best-effort, never break the pipeline
        logger.debug("[MemoryManager] could not save organizer snapshot: %s", e)


def _recent_cursor_key(agent_id: str, session_id: str) -> str:
    return f"kb_organizer_last_recent_ts:{agent_id}:{session_id}"


def _load_recent_cursor(agent_id: str, session_id: str) -> float:
    """Timestamp of the newest conversation turn already shown to the organizer."""
    try:
        return float(db.get_setting(_recent_cursor_key(agent_id, session_id)) or 0)
    except (TypeError, ValueError, Exception):  # noqa: BLE001 — best-effort
        return 0.0


def _save_recent_cursor(agent_id: str, session_id: str, ts: float) -> None:
    try:
        db.set_setting(_recent_cursor_key(agent_id, session_id), str(ts))
    except Exception as e:  # noqa: BLE001 — best-effort, never break the pipeline
        logger.debug("[MemoryManager] could not save recent cursor: %s", e)


def _log_organizer_diff(agent_id, agent_name, session_id, prev_summary, curr_summary,
                        delta_text, score, recent_messages, fresh_recent,
                        last_recent_ts) -> None:
    """Append the RAW diff (before it's compiled into the prompt) to a log file so
    the diff quality can be analysed: the unified summary diff (prev filing →
    current), the extracted/context-padded delta we feed, and which recent turns
    were selected as new. Best-effort. Path via EVOMEM_KB_ORGANIZER_PROMPT_LOG
    (default 'logs/organizer_prompt.diff'; set empty/off to disable)."""
    rel = os.environ.get('EVOMEM_KB_ORGANIZER_PROMPT_LOG', 'logs/organizer_prompt.diff')
    if not rel or rel.strip().lower() in ('0', 'off', 'no', 'false'):
        return
    try:
        base = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        path = rel if os.path.isabs(rel) else os.path.join(base, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        ts = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')

        udiff = "\n".join(difflib.unified_diff(
            (prev_summary or "").splitlines(),
            (curr_summary or "").splitlines(),
            fromfile="summary@last_filing", tofile="summary@now", lineterm="",
        )) or "(no summary change)"

        n_total = len([m for m in (recent_messages or []) if isinstance(m, dict)])
        n_fresh = len(fresh_recent or [])
        recent_lines = "\n".join(
            f"  [{m.get('ts')}] {m.get('role')}: {(m.get('content') or '')[:200]}"
            for m in (fresh_recent or [])
        ) or "  (no new turns)"

        block = (
            "=" * 80 + "\n"
            f"{ts} | agent={agent_id} ({agent_name}) | session={session_id}\n"
            f"summary: {len(prev_summary or '')}→{len(curr_summary or '')} chars | "
            f"delta_score={score} | recent: {n_fresh} new / {n_total} total "
            f"(cursor_ts={last_recent_ts})\n"
            + "=" * 80 + "\n"
            "----- RAW SUMMARY DIFF (prev filing → current) -----\n" + udiff + "\n\n"
            "----- EXTRACTED DELTA FED TO ORGANIZER (context-padded) -----\n"
            + (delta_text or "(empty — full summary used)") + "\n\n"
            "----- NEW RECENT TURNS FED -----\n" + recent_lines + "\n\n"
        )
        with open(path, 'a', encoding='utf-8') as f:
            f.write(block)
    except Exception as e:  # noqa: BLE001 — best-effort, never break the pipeline
        logger.debug("[MemoryManager] could not write organizer diff log: %s", e)


def _new_recent_messages(recent_messages, last_ts: float):
    """Return only conversation turns newer than ``last_ts`` (turns the organizer
    hasn't seen yet), plus the max ts across ALL turns so the caller can advance
    the cursor. Turns without a ts are always kept (can't tell if they're new)."""
    fresh, max_ts = [], last_ts
    for m in (recent_messages or []):
        if not isinstance(m, dict):
            continue
        ts = m.get('ts') or 0
        if ts > max_ts:
            max_ts = ts
        if ts == 0 or ts > last_ts:
            fresh.append(m)
    return fresh, max_ts


# Per-agent organizer concurrency control: at most ONE organizer sub-agent runs
# per agent at a time (no double-spawn), rate-limited to one run per debounce
# window (default 60s; EVOMEM_KB_ORGANIZER_DEBOUNCE_SECONDS to tune, 0 disables).
_organizer_running: set = set()
_organizer_last_start: dict = {}
_organizer_guard = threading.Lock()


def _organizer_debounce_seconds() -> float:
    try:
        return max(0.0, float(os.environ.get('EVOMEM_KB_ORGANIZER_DEBOUNCE_SECONDS', '60')))
    except (TypeError, ValueError):
        return 60.0


def _organizer_try_claim(agent_id: str, debounce_s: float) -> bool:
    """Claim the single per-agent organizer slot. Returns False (skip) if one is
    already running for this agent or the last run started within the debounce
    window; otherwise marks it running + records the start and returns True."""
    now = time.monotonic()
    with _organizer_guard:
        if agent_id in _organizer_running:
            return False  # an instance is already running for this agent
        if debounce_s > 0 and (now - _organizer_last_start.get(agent_id, 0.0)) < debounce_s:
            return False  # ran too recently
        _organizer_running.add(agent_id)
        _organizer_last_start[agent_id] = now
        return True


def _organizer_release(agent_id: str) -> None:
    with _organizer_guard:
        _organizer_running.discard(agent_id)


def _read_last_assistant_message(agent_id: str, session_id: str):
    """Return the last non-empty assistant message in a session — the GROUND TRUTH
    of the organizer's final output. The ``final_answer`` event field can carry an
    intermediate/short turn ("Now I'll build the JSON...") instead of the real ops,
    so this is the reliable source."""
    if not session_id:
        return None
    try:
        from models.db import db
        msgs = db.get_session_messages(session_id, limit=30, agent_id=agent_id) or []
        for m in reversed(msgs):
            if m.get('role') == 'assistant' and (m.get('content') or '').strip():
                return m['content']
    except Exception:
        logger.debug("read last assistant failed for %s/%s", agent_id, session_id, exc_info=True)
    return None


def _organizer_docs_from(agent_id, explorer_id, session_id, event_answer):
    """Parse the organizer's ops: prefer the final_answer event text, else fall back
    to the session's last assistant message (the event field is unreliable)."""
    docs = _parse_organizer_docs(event_answer)
    if docs is not None:
        return docs
    db_ans = _read_last_assistant_message(explorer_id, session_id)
    if db_ans and db_ans != (event_answer or ''):
        logger.info("[MemoryManager] organizer[%s]: event answer (%d chars) unparsed; using "
                    "session last-assistant (%d chars)", agent_id, len(event_answer or ''), len(db_ans))
        return _parse_organizer_docs(db_ans)
    return None


def _spawn_kb_organizer(agent: dict, source_text: str, recent_text: str = "",
                        source_label: str = "SUMMARY / NOTE (what to file)"):
    """Run the Knowledge Organizer sub-agent over the agent's server-local KB and
    return the parsed ``docs`` list of write-ops, or ``None`` on any failure/timeout.

    The organizer is a real agent: its workspace is ``agents/{id}/kb`` (resolved via
    the ``/_self/kb`` virtual path), so it EXPLORES the vault with Grep/Read/Glob —
    we hand it the recent conversation + the latest note, NOT a pre-built doc list.
    Read-only, confined to the KB dir; reuses the explorer sub-agent machinery.

    Deadlock-safety: the caller MUST NOT hold an ``_llm_lock`` permit here — the
    spawned sub-agent's own LLM loop acquires the same BoundedSemaphore.
    """
    import threading as _threading
    try:
        from backend.subagent_manager import subagent_manager
        from backend.agent_runtime import explorer
        from backend.agent_runtime.notifier import notify_agent
        from backend.tools._workspace import resolve_self_path, effective_agent_id
        from backend.event_stream import event_stream
    except Exception:
        return None

    agent_id = agent.get('id', '')
    if not agent_id:
        return None

    # Server-local KB path (works even when the agent runs on a remote/tunnel
    # workplace — direxplorer Grep/Read/Glob do in-process I/O on the server).
    kb_abs = resolve_self_path(effective_agent_id(agent), '/_self/kb')
    if not kb_abs or not os.path.isdir(kb_abs):
        return None

    tool_ids, tool_err = explorer.resolve_tool_ids('')
    if tool_err:
        return None

    # Generous default: the organizer is a tool-heavy agent loop on a possibly-slow
    # local model. This runs in a background daemon thread (debounced + single-
    # instance), so a long blocking wait is fine — and far better than discarding a
    # result that arrives a few seconds late. Tune via EVOMEM_KB_ORGANIZER_TIMEOUT.
    timeout = int(os.environ.get('EVOMEM_KB_ORGANIZER_TIMEOUT', '600'))
    skill_cfg = {
        'system_prompt': _KB_ORGANIZER_SYSTEM_PROMPT,
        'model_id': os.environ.get('EVOMEM_KB_ORGANIZER_MODEL') or None,
    }

    def _build(eid):
        cfg = explorer.build_config(agent, eid, kb_abs, skill_cfg, tool_ids)
        # Force server-local execution regardless of the parent's workplace.
        cfg['workplace_id'] = None
        cfg['sandbox_enabled'] = 0
        cfg['run_as_user'] = None
        # Tag as the KB organizer: a distinct identity from the Explore-tool
        # explorer so (a) token usage can be categorized clearly by the token
        # monitor and (b) we can diverge its behaviour later. The read-only
        # tool restriction itself comes from the generic explorer rule in
        # build_tools (all explorers get only direxplorer tools).
        cfg['is_kb_organizer'] = True
        cfg['name'] = f"{agent.get('name', agent_id)} · kb organizer"
        return cfg

    try:
        explorer_id = subagent_manager.spawn_explorer(agent, _build, id_kind='organizer')
    except Exception:
        return None

    parent_name = agent.get('name', agent_id)
    # The organizer is a SILENT background process — it must NOT report its JSON
    # back to the user's chat. Passing no report_to makes the auto-forward skip.
    report_to_id, report_to_channel_id = None, None

    answer_box, done = {}, _threading.Event()

    def _on_done(data):
        if data.get('agent_id') == explorer_id:
            answer_box['answer'] = data.get('answer', '')
            done.set()

    event_stream.on('final_answer', _on_done)
    try:
        parts = []
        if recent_text and recent_text.strip():
            parts.append("RECENT CONVERSATION (latest turns):\n" + recent_text.strip())
        if source_text and source_text.strip():
            parts.append(f"{source_label}:\n" + source_text.strip())
        task_msg = ("\n\n".join(parts) + '\n----------\n' + "\n\nExplore the vault (your workspace) with "
                    "Grep/Glob/Read and file any durable knowledge from the conversation "
                    "above into the right docs. EXTRACT every named entity — person, "
                    "place, organization, company, product, venue, event — and for any "
                    "that does NOT yet have its own doc, CREATE one (don't just append a "
                    "mention to another doc). Then WIRE UP LINKS: when you Read an "
                    "existing doc, look for named entities mentioned as PLAIN TEXT that "
                    "are not yet wrapped in [[ ]] — wrap each one in a [[wiki-link]] via "
                    "an 'edit' op (create the target doc first if it doesn't exist). "
                    "Also RECONCILE dangling [[links]]: if a link doesn't resolve, the "
                    "target may already exist under a different name — check and fix it "
                    "(add the variant as an alias, or repoint the link to the canonical "
                    "doc). Output ONLY the JSON object.")
        # Let notify auto-create/resolve the session (get_or_create_session). A
        # made-up session_id is rejected as "not found", so we must NOT force one.
        # Within a run each spawn has a fresh agent_id (organizer_1/2/...) -> a fresh
        # session anyway; the DB-read fallback handles capture even if reused.
        res = notify_agent(
            agent_id=explorer_id, tag=f"AGENT/{parent_name}", message=task_msg,
            external_user_id=f"__agent__{agent_id}", channel_id=None, dedup=False,
            trigger_llm=True,
            metadata={'agent_message': True, 'from_agent_id': agent_id,
                      'from_agent_name': parent_name, 'agent_message_depth': 1,
                      'subagent_spawn': True, 'injected_system_vars': {},
                      'report_to_id': report_to_id,
                      'report_to_channel_id': report_to_channel_id})
        session_id = res.get('session_id')
        if not res.get('success') or not session_id:
            return None
        if not done.wait(timeout=timeout):
            logger.warning("[MemoryManager] organizer[%s]: timed out after %ds — result "
                           "discarded (raise EVOMEM_KB_ORGANIZER_TIMEOUT)", agent_id, timeout)
            return None
        # The event 'answer' can be an intermediate turn; fall back to the session's
        # last assistant message (ground truth).
        docs = _organizer_docs_from(agent_id, explorer_id, session_id, answer_box.get('answer', ''))
        if docs is not None:
            return docs
        ans = answer_box.get('answer', '') or ''
        logger.warning("[MemoryManager] organizer[%s]: could not parse ops JSON (event %d "
                       "chars); retrying. head=%r", agent_id, len(ans), ans[:300])
        # One retry: re-prompt the same session for clean JSON.
        done.clear()
        answer_box.clear()
        res2 = notify_agent(
            agent_id=explorer_id, tag=f"AGENT/{parent_name}",
            message=("Your previous answer was not valid JSON. Reply with ONLY the "
                     'JSON object {"docs":[...]} — no prose, no code fences.'),
            session_id=session_id, dedup=False, trigger_llm=True,
            metadata={'agent_message': True, 'from_agent_id': agent_id,
                      'agent_message_depth': 1})
        if not res2.get('success') or not done.wait(timeout=timeout):
            return None
        docs = _organizer_docs_from(agent_id, explorer_id, session_id, answer_box.get('answer', ''))
        if docs is None:
            logger.warning("[MemoryManager] organizer[%s]: ops JSON still unparseable after "
                           "retry", agent_id)
        return docs
    except Exception:
        logger.debug("kb organizer failed for %s", agent_id, exc_info=True)
        return None
    finally:
        event_stream.off('final_answer', _on_done)
        # Do NOT destroy here: the agent loop may still have trailing activity
        # (a final context build), and rmtree'ing its DB mid-flight throws
        # "unable to open database file". Idle cleanup reaps it safely; the
        # single-instance + debounce guard keeps the per-parent count low.


_TITLE_PAREN_ALIAS_RE = re.compile(r'\s*[\(\[]([^)\]]{1,60})[\)\]]\s*$')


def _split_title_aliases(title: str):
    """Split a trailing parenthetical nickname off a title so the slug stays the
    PURE name. 'Abdurrahman Wahid (Gus Dur)' -> ('Abdurrahman Wahid', ['Gus Dur']);
    'Susilo Bambang Yudhoyono (SBY)' -> ('Susilo Bambang Yudhoyono', ['SBY']).
    Returns (clean_title, [aliases]); a whole-parenthetical title is left as-is.
    """
    title = (title or '').strip()
    m = _TITLE_PAREN_ALIAS_RE.search(title)
    if not m:
        return title, []
    clean = title[:m.start()].strip()
    if not clean:
        return title, []
    aliases = [a.strip() for a in re.split(r'[;,/]', m.group(1)) if a.strip()]
    return clean, aliases


def _author_docs(agent: dict, session_id: str, source_text: str,
                 llm_lock: threading.Lock, recent_messages=None) -> None:
    """Organize a conversation into the agent's knowledge vault.

    By default a Knowledge Organizer sub-agent EXPLORES the vault itself (Grep/Read/
    Glob over its workspace) and returns the write-ops (create/update with inline
    ``[[links]]``); new docs are created via ``upsert_doc`` in the active collection
    folder (root by default), existing docs are appended to (delta only). Emits
    ``doc_updated`` so the evomem sync wires the graph. Best-effort, background thread.

    ``recent_messages`` is the summarizer's ``tail_messages`` (latest turns) handed to
    the organizer as conversation context so freshly-mentioned info isn't lost.
    """
    agent_id = agent['id']
    if get_engine() != 'evomem':
        return
    try:
        folder = _get_active_collection(agent_id, session_id)

        # Organize strategy. ON (default): the Knowledge Organizer sub-agent — a real
        # agent whose workspace IS the KB — explores the vault itself and returns
        # write-ops, spawned with NO llm_lock held (deadlock-safe). On failure we SKIP
        # (never fall through to a duplicate-prone author). OFF/legacy (or DirExplorer
        # unavailable): a single, non-agentic author call that needs the doc list
        # pre-built (it can't explore).
        mode = os.environ.get('EVOMEM_KB_ORGANIZER', 'on').strip().lower()
        use_organizer = mode not in ('0', 'off', 'no', 'legacy')
        docs = None
        name_to_slug = {}
        if use_organizer and _kb_organizer_infra_ok():
            # Diff the current summary against what the organizer last filed and
            # feed only that delta (+ context). Skip the spawn when too little is
            # new — leaving the cursor un-advanced so small changes accumulate.
            delta_mode = _delta_enabled()
            prev_snapshot = _load_organized_snapshot(agent_id, session_id) if delta_mode else ""
            if delta_mode:
                delta_text, delta_score = _summary_delta(
                    prev_snapshot, source_text, _delta_context())
                if prev_snapshot and delta_score < _delta_min_score():
                    logger.debug("[MemoryManager] author_docs[%s]: delta score %d < %d; "
                                 "skipping organizer (not enough new to file)",
                                 agent_id, delta_score, _delta_min_score())
                    return
            else:
                delta_text, delta_score = "", 0

            # One organizer per agent at a time, debounced — never double-spawn.
            if not _organizer_try_claim(agent_id, _organizer_debounce_seconds()):
                logger.debug("[MemoryManager] author_docs[%s]: organizer busy or "
                             "debounced; skipping this run", agent_id)
                return
            # Like the summary, feed only the conversation turns NEW since the last
            # filing (the tail re-sends the same turns every run otherwise).
            recent_max_ts = 0
            last_recent_ts = 0
            if delta_mode:
                last_recent_ts = _load_recent_cursor(agent_id, session_id)
                fresh_recent, recent_max_ts = _new_recent_messages(
                    recent_messages, last_recent_ts)
                recent_for_feed = fresh_recent
            else:
                recent_for_feed = recent_messages

            # Dump the RAW diff (before it's compiled into the prompt) for analysis.
            _log_organizer_diff(agent_id, agent.get('name', agent_id), session_id,
                                prev_snapshot, source_text, delta_text, delta_score,
                                recent_messages, recent_for_feed, last_recent_ts)
            try:
                recent_text = _format_recent_messages(recent_for_feed)
                if delta_mode and delta_text.strip():
                    feed_text, source_label = delta_text, "WHAT'S NEW SINCE LAST FILING"
                else:
                    feed_text, source_label = source_text, "SUMMARY / NOTE (what to file)"
                docs = _spawn_kb_organizer(agent, feed_text, recent_text,
                                           source_label=source_label)
            finally:
                _organizer_release(agent_id)
            if docs is None:
                logger.info("[MemoryManager] author_docs[%s]: organizer returned nothing "
                            "(skipping to avoid duplicates)", agent_id)
                return
            # Organizer ran (returned ops, possibly empty) — advance both cursors to
            # this point so the next run diffs the summary AND the recent turns.
            if delta_mode:
                _save_organized_snapshot(agent_id, session_id, source_text)
                if recent_max_ts > _load_recent_cursor(agent_id, session_id):
                    _save_recent_cursor(agent_id, session_id, recent_max_ts)
        else:
            # Legacy author can't explore — give it the full vault listing + a
            # title/alias map for alias-aware resolution.
            listing_text, name_to_slug = _kb_doc_listing(agent_id)
            prompt = _AUTHOR_DOCS_PROMPT.format(
                guidance=_DEFAULT_KB_GUIDANCE, existing=listing_text, source=source_text)
            data = _kb_llm_json(prompt, llm_lock)
            if not isinstance(data, dict):
                return
            docs = data.get('docs')
        if not isinstance(docs, list) or not docs:
            return

        modified, created, updated, renamed, edited, deduped = [], 0, 0, 0, 0, 0
        for d in docs[:12]:
            if not isinstance(d, dict):
                continue
            action = (d.get('action') or '').strip().lower()
            title = (d.get('title') or '').strip()
            body = (d.get('body') or '').strip()
            slug_hint = (d.get('slug') or '').strip()

            # Dedupe: clean a doc whose content got duplicated (self-concatenation).
            if action in ('dedupe', 'dedup'):
                if slug_hint:
                    with _kb_page_lock(agent_id, slug_hint):
                        if evomem_writer.dedupe_doc(agent_id, slug_hint):
                            modified.append(slug_hint)
                            deduped += 1
                continue

            # Edit: surgical exact-unique-match replacement (fix a wrong [[link]]).
            if action in ('edit', 'patch'):
                old_str = d.get('old_str') or d.get('old') or ''
                new_str = d.get('new_str') or d.get('new') or ''
                if slug_hint and old_str:
                    with _kb_page_lock(agent_id, slug_hint):
                        if evomem_writer.replace_in_doc(agent_id, slug_hint, old_str, new_str):
                            modified.append(slug_hint)
                            edited += 1
                continue

            # Rename: fix a typo'd / alias-polluted doc name.
            if action == 'rename':
                new_title, _ = _split_title_aliases((d.get('new_title') or '').strip())
                if not slug_hint or not new_title:
                    continue
                with _kb_page_lock(agent_id, slug_hint):
                    new_rel = evomem_writer.rename_doc(agent_id, slug_hint, new_title)
                    if new_rel:
                        renamed += 1
                        modified.append(new_rel)
                        if body:  # also fold in any new info from the same op
                            cur = (evomem_writer.read_doc(agent_id, new_rel) or {}).get('body') or ''
                            if body not in cur:
                                evomem_writer.append_to_doc(agent_id, new_rel, body)
                continue

            if not title or not body:
                continue
            # Keep the slug = PURE name: strip a trailing parenthetical nickname into
            # aliases, and merge any aliases the model supplied.
            title, paren_aliases = _split_title_aliases(title)
            aliases = [a for a in (d.get('aliases') or []) if isinstance(a, str) and a.strip()]
            aliases = list(dict.fromkeys(aliases + paren_aliases))
            doc_type = (d.get('type') or 'note').strip()
            description = (d.get('description') or '').strip()
            tags = [t for t in (d.get('tags') or []) if isinstance(t, str) and t.strip()]

            base_slug = evomem_writer.slugify(title)
            lock_key = (f"{folder}/{base_slug}" if folder else base_slug)
            with _kb_page_lock(agent_id, lock_key):
                # Collision guard: resolve by slug hint -> title/alias -> slug. A
                # create whose title/alias already exists becomes an update here, so
                # a duplicate is never minted even if the resolver mislabels the op.
                target = _resolve_doc_slug2(agent_id, slug_hint, title, folder, name_to_slug)
                if target:
                    # The resolver already returns delta-only prose; guard against
                    # re-appending content the doc already contains (no LLM call).
                    existing_doc = evomem_writer.read_doc(agent_id, target)
                    existing_body = (existing_doc or {}).get('body') or ''
                    delta = None if (body and body in existing_body) else body
                    if delta and evomem_writer.append_to_doc(agent_id, target, delta):
                        modified.append(target)
                        updated += 1
                else:
                    rel = evomem_writer.upsert_doc(
                        agent_id, title=title, body=body, doc_type=doc_type,
                        description=description, folder=folder, tags=tags, aliases=aliases)
                    if rel:
                        modified.append(rel)
                        created += 1
                        if folder:
                            evomem_writer.add_to_collection_index(agent_id, folder, title)

        if modified:
            evomem_writer.mark_dirty(agent_id)
            _emit_doc_updated(agent_id, modified)
        logger.info("[MemoryManager] author_docs[%s]: %d created, %d updated, %d renamed, "
                    "%d edited, %d deduped (folder=%s)", agent_id, created, updated, renamed,
                    edited, deduped, folder or 'root')
    except Exception:
        logger.debug("author_docs exception (non-fatal) for %s", agent_id)


def process_knowledge(agent: dict, session_id: str, summary: str,
                      llm_lock: threading.Lock, recent_messages=None) -> None:
    """Author/update rich, inline-linked knowledge docs from a conversation.
    Triggered on ``summary_updated``; runs in the background extraction thread.
    ``recent_messages`` is the summarizer's ``tail_messages`` (latest turns) passed
    to the organizer as context. Best-effort; non-fatal on any error."""
    _author_docs(agent, session_id, summary, llm_lock, recent_messages=recent_messages)


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
