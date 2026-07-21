"""
Knowledge Builder Evaluator

Deterministic validator for the "author-docs" JSON that the evomem knowledge
pipeline consumes (see ``_AUTHOR_DOCS_PROMPT`` / ``_author_docs`` in
``backend/agent_runtime/memory_manager.py`` and ``evomem_writer.upsert_doc``).

The model under test is given the real authoring prompt and must return
``{"docs": [ ... ]}``. This evaluator parses that output and scores it on
structure, valid doc types, create/update action + dedup correctness, inline
``[[wiki-links]]``, and the no-trailing-"Relations"-block rule — with NO second
LLM pass, so scores are reproducible across model/training comparisons.
"""

import json
import re
from typing import Any, Dict, List, Optional, Tuple

from .base import BaseEvaluator, EvaluationResult

# Valid doc types come from the writer itself so the eval never drifts from prod.
from backend.agent_runtime.evomem_writer import DOC_TYPES

_LINK_RE = re.compile(r"\[\[[^\]]+\]\]")
_BARE_LINK_LINE = re.compile(r"^\s*(?:[-*]\s*)?\[\[[^\]]+\]\]\s*$")
_RELATIONS_HEADER = re.compile(r"^\s*#*\s*(relations|links|related)\b", re.IGNORECASE)
_VALID_ACTIONS = {"create", "update", "edit", "rename", "dedupe", "dedup"}
_REQUIRED_FIELDS = ("action", "title", "type", "body")
# Op-specific required fields. create/update author prose; edit/rename/dedupe act
# on an existing doc by slug and don't carry title/type/body.
_REQUIRED_BY_ACTION = {
    "create": ("action", "title", "type", "body"),
    "update": ("action", "title", "type", "body"),
    "edit": ("action", "slug", "old_str", "new_str"),
    "rename": ("action", "slug", "new_title"),
    "dedupe": ("action", "slug"),
    "dedup": ("action", "slug"),
}
# Ops that target an existing doc by slug (matched by slug, not title).
_SLUG_KEYED_ACTIONS = {"edit", "rename", "dedupe", "dedup"}


def _norm(s: str) -> str:
    return (s or "").strip().lower()


def _doc_names(d: dict) -> List[str]:
    """A doc's title plus any aliases — the names it can be matched by."""
    names = []
    if isinstance(d, dict):
        if d.get("title"):
            names.append(str(d["title"]))
        for a in (d.get("aliases") or []):
            if isinstance(a, str) and a.strip():
                names.append(a)
    return names


def _find_doc(entity: dict, action: str, docs: list) -> Optional[dict]:
    """Return the doc that fulfils ``entity`` under the expected ``action``, or None.

    A doc fulfils the entity when its ``action`` matches, its title/alias matches
    (case-insensitive, either-direction substring so "Borobudur" matches a doc
    titled "Candi Borobudur"), its ``type`` is one of the expected types (when
    given), and — for updates — its ``slug`` matches the expected slug (when given).
    """
    slug = entity.get("slug")

    # Pass 1: Exact Slug Match (Primary)
    if slug:
        for d in docs:
            if not isinstance(d, dict):
                continue
            if (d.get("action") or "").strip().lower() != action:
                continue
            if (d.get("slug") or "").strip() == slug:
                return d

    # Pass 2: Fuzzy Title/Alias Match (Fallback)
    want = _norm(entity.get("title"))
    if not want:
        return None
    types = entity.get("type")
    if isinstance(types, str):
        types = [types]
    type_set = set(types) if types else None

    for d in docs:
        if not isinstance(d, dict):
            continue
        if (d.get("action") or "").strip() != action:
            continue
        # A doc that explicitly targets a different slug is a different doc —
        # a matching title must not rescue it (e.g. update of "bandung" does
        # not satisfy an expected update of "jakarta").
        d_slug = (d.get("slug") or "").strip()
        if slug and d_slug and d_slug != slug:
            continue
        cands = [_norm(n) for n in _doc_names(d)]
        if not any(want == c or (c and (want in c or c in want)) for c in cands):
            continue
        if type_set is not None and (d.get("type") or "") not in type_set:
            # For update actions: skip type check when type is not provided (slug is the key)
            if not (action == "update" and not d.get("type")):
                continue
        return d
    return None


def _entity_satisfied(entity: dict, action: str, docs: list) -> bool:
    """True if some doc fulfils ``entity`` under the expected ``action``."""
    return _find_doc(entity, action, docs) is not None


def _strip_code_fences(text: str) -> str:
    """Remove a leading/trailing ``` or ```json fence if present."""
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z0-9]*\s*", "", t)
        if t.endswith("```"):
            t = t[:-3]
    return t.strip()


def _parse_json_obj(response: str) -> Tuple[Optional[dict], str]:
    """Best-effort parse of the model output into a JSON object.

    Tolerant of code fences and surrounding prose. Returns (obj, error)."""
    if not response or not response.strip():
        return None, "empty response"
    raw = _strip_code_fences(response)
    try:
        obj = json.loads(raw)
        return (obj, "") if isinstance(obj, dict) else (None, "top-level JSON is not an object")
    except json.JSONDecodeError as e:
        first = raw.find("{")
        last = raw.rfind("}")
        if first != -1 and last > first:
            try:
                obj = json.loads(raw[first:last + 1])
                return (obj, "") if isinstance(obj, dict) else (None, "top-level JSON is not an object")
            except json.JSONDecodeError:
                pass
        return None, f"invalid JSON: {e}"


def _has_trailing_link_block(body: str) -> bool:
    """True if the body ends in a bare ``[[link]]`` list or a "Relations:" block —
    the anti-pattern the authoring prompt explicitly forbids."""
    lines = [ln for ln in body.rstrip().split("\n")]
    if not lines:
        return False
    trailing_bare = 0
    for ln in reversed(lines):
        if not ln.strip():
            continue
        if _RELATIONS_HEADER.match(ln):
            return True
        if _BARE_LINK_LINE.match(ln):
            trailing_bare += 1
            continue
        break
    # Two or more consecutive bare link lines at the end == a "Relations" list.
    return trailing_bare >= 2


class KnowledgeBuilderEvaluator(BaseEvaluator):
    """Scores author-docs JSON against the evomem writer's contract."""

    # Component weights (sum to 1.0).
    WEIGHTS = {
        "structure": 0.18,      # required fields present & non-empty
        "valid_type": 0.12,     # type in DOC_TYPES
        "valid_action": 0.10,   # action create/update (+ slug on update)
        "coverage": 0.35,       # expected subjects surfaced with the right action/slug
        "links": 0.12,          # inline [[links]] when required
        "anti_pattern": 0.05,   # no trailing "Relations" block
        "thumbnail": 0.08,      # thumbnail set for entities that have an image
    }

    def __init__(self, domain: str = "knowledge_builder"):
        self.domain = domain

    @property
    def name(self) -> str:
        return "knowledge_builder"

    @property
    def uses_pass2(self) -> bool:
        return False

    def evaluate(self, response: str, expected: Any, level: int, prompt: str = "") -> EvaluationResult:
        expected = expected if isinstance(expected, dict) else {}
        data, err = _parse_json_obj(response)
        if data is None:
            return EvaluationResult(0.0, "failed", {"error": err, "scoring_method": "knowledge_builder"})

        docs = data.get("docs")
        if not isinstance(docs, list):
            return EvaluationResult(0.0, "failed",
                                    {"error": "missing 'docs' list", "scoring_method": "knowledge_builder"})

        # Expected docs grouped by action, e.g. {"create": [...], "update": [...],
        # "edit": [...], "rename": [...], "dedupe": [...]}.
        ea = expected.get("expect_actions") or {}
        action_groups = [(a, ea.get(a) or [])
                         for a in ("create", "update", "edit", "rename", "dedupe")]
        total_expected = sum(len(ents) for _, ents in action_groups)
        require_links = bool(expected.get("require_links"))

        # An explicit, all-empty expectation means: keep nothing (e.g. pure chatter).
        if ea and total_expected == 0:
            ok = len(docs) == 0
            return EvaluationResult(
                1.0 if ok else 0.0,
                "passed" if ok else "failed",
                {"expected_empty": True, "got_docs": len(docs), "scoring_method": "knowledge_builder"},
            )

        if not docs:
            return EvaluationResult(0.0, "failed",
                                    {"error": "no docs produced", "scoring_method": "knowledge_builder"})

        per_doc = [self._score_doc(d, require_links) for d in docs]

        def avg(key: str) -> float:
            return sum(p[key] for p in per_doc) / len(per_doc)

        # Coverage: expected subjects that surfaced with the right action (and slug).
        missing = []
        satisfied = 0
        for action, ents in action_groups:
            for e in ents:
                if _entity_satisfied(e, action, docs):
                    satisfied += 1
                else:
                    missing.append({"action": action, "title": e.get("title") or e.get("slug")})
        coverage = satisfied / total_expected if total_expected else 1.0

        # Thumbnail: expected entities flagged ``"thumbnail": true`` whose matching
        # doc sets a non-empty ``thumbnail``. Skipped (1.0) when none are flagged,
        # mirroring how ``links`` is skipped when not required.
        thumb_expected = 0
        thumb_satisfied = 0
        for action, ents in action_groups:
            for e in ents:
                if not e.get("thumbnail"):
                    continue
                thumb_expected += 1
                doc = _find_doc(e, action, docs)
                if doc and str(doc.get("thumbnail") or "").strip():
                    thumb_satisfied += 1
        thumbnail = (thumb_satisfied / thumb_expected) if thumb_expected else 1.0

        components = {
            "structure": avg("structure"),
            "valid_type": avg("valid_type"),
            "valid_action": avg("valid_action"),
            "coverage": coverage,
            "links": avg("links") if require_links else 1.0,
            "anti_pattern": avg("anti_pattern"),
            "thumbnail": thumbnail,
        }
        score = round(sum(components[k] * w for k, w in self.WEIGHTS.items()), 3)
        status = "passed" if score >= 0.8 else "partial" if score >= 0.5 else "failed"
        return EvaluationResult(
            score=score,
            status=status,
            details={
                "components": {k: round(v, 3) for k, v in components.items()},
                "num_docs": len(docs),
                "satisfied_entities": satisfied,
                "expected_entities": total_expected,
                "thumbnail_satisfied": thumb_satisfied,
                "thumbnail_expected": thumb_expected,
                "missing": missing,
                "missing_entities": [m["title"] for m in missing],
                "per_doc": per_doc,
                "scoring_method": "knowledge_builder",
            },
            pass2_used=False,
        )

    def _score_doc(self, d: Any, require_links) -> Dict[str, Any]:
        """Per-doc structural quality (independent of which entities are expected)."""
        if not isinstance(d, dict):
            return {"structure": 0.0, "valid_type": 0.0, "valid_action": 0.0,
                    "links": 0.0, "anti_pattern": 0.0, "error": "doc is not an object"}

        action = (d.get("action") or "").strip().lower()
        doc_type = (d.get("type") or "").strip()
        body = (d.get("body") or "").strip()
        slug = (d.get("slug") or "").strip()
        authoring = action in ("create", "update")  # ops that write prose + a typed doc

        # Structure: op-specific required fields all present & non-empty.
        req = _REQUIRED_BY_ACTION.get(action, _REQUIRED_FIELDS)
        structure = 1.0 if all(str(d.get(f, "")).strip() for f in req) else 0.0
        # A no-op edit (old_str == new_str) changes nothing — treat as invalid.
        if action == "edit" and \
                str(d.get("old_str", "")).strip() == str(d.get("new_str", "")).strip():
            structure = 0.0

        # type / links / anti-pattern only apply to authoring ops; N/A → 1.0.
        valid_type = (1.0 if doc_type in DOC_TYPES else 0.0) if authoring else 1.0
        valid_action = 0.0
        if action in _VALID_ACTIONS:
            valid_action = 1.0 if (action == "create" or slug) else 0.5  # others must carry a slug
        links = (1.0 if _LINK_RE.search(body) else 0.0) if authoring else 1.0
        anti_pattern = (0.0 if _has_trailing_link_block(body) else 1.0) if authoring else 1.0

        return {
            "structure": structure,
            "valid_type": valid_type,
            "valid_action": valid_action,
            "links": links,
            "anti_pattern": anti_pattern,
            "action": action,
            "type": doc_type,
            "title": (d.get("title") or "").strip(),
        }
