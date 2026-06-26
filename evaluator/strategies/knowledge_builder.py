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
_VALID_ACTIONS = {"create", "update"}
_REQUIRED_FIELDS = ("action", "title", "type", "body")


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
        "structure": 0.30,      # required fields present & non-empty
        "valid_type": 0.20,     # type in DOC_TYPES
        "valid_action": 0.15,   # action create/update (+ slug on update)
        "dedup": 0.20,          # action / slug match the scenario expectation
        "links": 0.10,          # inline [[links]] when required
        "anti_pattern": 0.05,   # no trailing "Relations" block
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

        # Scenario where the right answer is to keep nothing.
        if expected.get("expect_empty"):
            ok = len(docs) == 0
            return EvaluationResult(
                1.0 if ok else 0.0,
                "passed" if ok else "failed",
                {"expected_empty": True, "got_docs": len(docs), "scoring_method": "knowledge_builder"},
            )

        if not docs:
            return EvaluationResult(0.0, "failed",
                                    {"error": "no docs produced", "scoring_method": "knowledge_builder"})

        expect_actions = set(expected.get("expect_actions") or [])
        expect_types = set(expected.get("expect_types") or [])
        existing_slugs = set(expected.get("existing_slugs") or [])
        expect_update_slug = expected.get("expect_update_slug")
        require_links = bool(expected.get("require_links"))

        per_doc: List[Dict[str, Any]] = []
        for d in docs:
            per_doc.append(self._score_doc(d, expect_actions, expect_types,
                                           existing_slugs, expect_update_slug, require_links))

        def avg(key: str) -> float:
            return sum(p[key] for p in per_doc) / len(per_doc)

        components = {
            "structure": avg("structure"),
            "valid_type": avg("valid_type"),
            "valid_action": avg("valid_action"),
            "dedup": avg("dedup"),
            "links": avg("links") if require_links else 1.0,
            "anti_pattern": avg("anti_pattern"),
        }
        score = sum(components[k] * w for k, w in self.WEIGHTS.items())

        # Completeness: penalize producing fewer docs than the scenario needs.
        min_docs = int(expected.get("min_docs", 1))
        if min_docs > 0 and len(docs) < min_docs:
            score *= len(docs) / min_docs

        score = round(score, 3)
        status = "passed" if score >= 0.8 else "partial" if score >= 0.5 else "failed"
        return EvaluationResult(
            score=score,
            status=status,
            details={
                "components": {k: round(v, 3) for k, v in components.items()},
                "num_docs": len(docs),
                "per_doc": per_doc,
                "scoring_method": "knowledge_builder",
            },
            pass2_used=False,
        )

    def _score_doc(self, d: Any, expect_actions, expect_types,
                   existing_slugs, expect_update_slug, require_links) -> Dict[str, Any]:
        if not isinstance(d, dict):
            return {k: 0.0 for k in ("structure", "valid_type", "valid_action", "dedup",
                                     "links", "anti_pattern")} | {"error": "doc is not an object"}

        action = (d.get("action") or "").strip()
        title = (d.get("title") or "").strip()
        doc_type = (d.get("type") or "").strip()
        body = (d.get("body") or "").strip()
        slug = (d.get("slug") or "").strip()

        structure = 1.0 if all(str(d.get(f, "")).strip() for f in _REQUIRED_FIELDS) else 0.0
        valid_type = 1.0 if doc_type in DOC_TYPES else 0.0

        valid_action = 0.0
        if action in _VALID_ACTIONS:
            valid_action = 1.0 if (action == "create" or slug) else 0.5  # update must carry a slug

        # Dedup correctness vs. the scenario's expectation.
        dedup = 1.0
        if expect_actions:
            dedup = 1.0 if action in expect_actions else 0.0
        if action == "update" and dedup > 0:
            if expect_update_slug is not None:
                dedup = 1.0 if slug == expect_update_slug else 0.0
            elif existing_slugs:
                dedup = 1.0 if slug in existing_slugs else 0.0

        links = 1.0 if _LINK_RE.search(body) else 0.0
        anti_pattern = 0.0 if _has_trailing_link_block(body) else 1.0

        return {
            "structure": structure,
            "valid_type": valid_type,
            "valid_action": valid_action,
            "dedup": dedup,
            "links": links,
            "anti_pattern": anti_pattern,
            "action": action,
            "type": doc_type,
            "title": title,
        }
