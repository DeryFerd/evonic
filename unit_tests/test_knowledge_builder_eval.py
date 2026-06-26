"""
Tests for the Knowledge Builder eval domain.

Covers:
- KnowledgeBuilderEvaluator scoring (good vs. malformed / invalid-type / no-links /
  wrong-dedup / expect-empty / trailing-"Relations"-block).
- Drift guard: stored test prompts stay identical to the live _AUTHOR_DOCS_PROMPT.
- Domain/evaluator registration & discovery.
"""

import json
import os

import pytest

from evaluator.strategies.knowledge_builder import (
    KnowledgeBuilderEvaluator,
    _has_trailing_link_block,
)
from evaluator.domain_evaluators import get_evaluator
from evaluator.test_loader import test_loader
from evaluator.test_definitions.knowledge_builder import _generate


@pytest.fixture
def ev():
    return KnowledgeBuilderEvaluator()


# ---------------------------------------------------------------- scoring ----

def _good_create():
    return json.dumps({"docs": [{
        "action": "create", "title": "Sari", "type": "person",
        "description": "Product manager at Evonic.", "tags": ["person"],
        "body": "Sari adalah product manager di [[Evonic]], bekerja bersama User.",
    }]})


GOOD_EXPECT = {"min_docs": 1, "expect_actions": ["create"],
               "expect_types": ["person", "organization", "company", "note", "place"],
               "require_links": True}


def test_good_create_scores_high(ev):
    r = ev.evaluate(_good_create(), GOOD_EXPECT, level=2)
    assert r.score >= 0.8
    assert r.status == "passed"
    assert r.pass2_used is False


def test_invalid_json_fails(ev):
    r = ev.evaluate("not json at all", GOOD_EXPECT, level=2)
    assert r.score == 0.0
    assert r.status == "failed"


def test_code_fenced_json_is_parsed(ev):
    fenced = "```json\n" + _good_create() + "\n```"
    r = ev.evaluate(fenced, GOOD_EXPECT, level=2)
    assert r.score >= 0.8


def test_invalid_type_penalized(ev):
    bad = json.dumps({"docs": [{
        "action": "create", "title": "Sari", "type": "human",  # not a valid DOC_TYPE
        "description": "PM.", "body": "Sari di [[Evonic]].",
    }]})
    good = ev.evaluate(_good_create(), GOOD_EXPECT, level=2).score
    assert ev.evaluate(bad, GOOD_EXPECT, level=2).score < good


def test_missing_links_when_required_penalized(ev):
    no_links = json.dumps({"docs": [{
        "action": "create", "title": "Sari", "type": "person",
        "description": "PM.", "body": "Sari adalah product manager.",
    }]})
    good = ev.evaluate(_good_create(), GOOD_EXPECT, level=2).score
    assert ev.evaluate(no_links, GOOD_EXPECT, level=2).score < good


def test_update_dedup_slug(ev):
    expect = {"min_docs": 1, "expect_actions": ["update"],
              "existing_slugs": ["jakarta"], "expect_update_slug": "jakarta"}
    right = json.dumps({"docs": [{
        "action": "update", "slug": "jakarta", "title": "Jakarta", "type": "place",
        "description": "Capital.", "body": "User membuka kantor baru di sini.",
    }]})
    wrong = json.dumps({"docs": [{
        "action": "update", "slug": "bandung", "title": "Jakarta", "type": "place",
        "description": "Capital.", "body": "User membuka kantor baru di sini.",
    }]})
    assert ev.evaluate(right, expect, level=4).score > ev.evaluate(wrong, expect, level=4).score


def test_expect_empty(ev):
    expect = {"expect_empty": True}
    assert ev.evaluate('{"docs": []}', expect, level=5).score == 1.0
    non_empty = json.dumps({"docs": [{"action": "create", "title": "X", "type": "note",
                                       "description": "d", "body": "b"}]})
    assert ev.evaluate(non_empty, expect, level=5).score == 0.0


def test_missing_docs_key_fails(ev):
    assert ev.evaluate('{"items": []}', GOOD_EXPECT, level=2).score == 0.0


def test_trailing_relations_block_detected():
    assert _has_trailing_link_block("Sari is a PM.\n\n[[Evonic]]\n[[User]]") is True
    assert _has_trailing_link_block("## Relations\n[[Evonic]]") is True
    assert _has_trailing_link_block("Sari works at [[Evonic]] with User.") is False


# ----------------------------------------------------------- drift guard ----

def test_stored_prompts_match_live_author_docs_prompt():
    """Every generated test prompt must equal a fresh render from the live prompt.

    If this fails, the production _AUTHOR_DOCS_PROMPT changed — re-run:
        python -m evaluator.test_definitions.knowledge_builder._generate
    """
    base = os.path.dirname(os.path.abspath(_generate.__file__))
    for sc in _generate.SCENARIOS:
        path = os.path.join(base, f"level_{sc['level']}", f"{sc['id']}.json")
        assert os.path.exists(path), f"missing generated test: {path}"
        with open(path, encoding="utf-8") as f:
            stored = json.load(f)
        expected_prompt = _generate.render_prompt(sc["existing"], sc["source"])
        assert stored["prompt"] == expected_prompt, (
            f"{sc['id']} prompt is stale — regenerate test cases")


# --------------------------------------------------------- registration ----

def test_domain_discovered():
    domain_ids = {d.id for d in test_loader.scan_domains()}
    assert "knowledge_builder" in domain_ids


def test_evaluator_resolves():
    ev = get_evaluator("knowledge_builder", evaluator_type="knowledge_builder")
    assert isinstance(ev, KnowledgeBuilderEvaluator)
    assert ev.uses_pass2 is False
