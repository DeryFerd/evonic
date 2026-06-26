"""
Generate Knowledge Builder test cases from the LIVE author-docs prompt.

Each test sends the model the exact prompt production uses —
``_AUTHOR_DOCS_PROMPT.format(guidance=_DEFAULT_KB_GUIDANCE, existing=..., source=...)``
(a single user prompt, no system prompt, mirroring ``_author_docs``). Test files
store the rendered prompt so the eval framework can read them statically; re-run
this script after any change to the production prompt to keep them in sync.

    python -m evaluator.test_definitions.knowledge_builder._generate

A drift-guard unit test (unit_tests/test_knowledge_builder_eval.py) re-renders
from the live prompt and fails if the stored prompts fall out of sync.
"""

import json
import os

THIS_DIR = os.path.dirname(os.path.abspath(__file__))


def _existing_text(existing):
    """Mirror the EXISTING-docs rendering in memory_manager._author_docs."""
    return "\n".join(
        f"[{d['slug']}] {d['title']} :: {d['description']}" for d in existing
    ) or "(none yet)"


def render_prompt(existing, source):
    """Render the live author-docs prompt for a scenario's existing docs + source."""
    from backend.agent_runtime.memory_manager import _AUTHOR_DOCS_PROMPT, _DEFAULT_KB_GUIDANCE
    return _AUTHOR_DOCS_PROMPT.format(
        guidance=_DEFAULT_KB_GUIDANCE,
        existing=_existing_text(existing),
        source=source,
    )


# 2 scenarios per level, levels 1-5. `existing` + `source` feed the live prompt;
# `expected` drives KnowledgeBuilderEvaluator scoring.
SCENARIOS = [
    # ---- Level 1: single create, simple subject, links optional ----
    {
        "id": "kb_create_person_1", "level": 1,
        "name": "Create person doc",
        "description": "Author a single person doc from a simple fact.",
        "existing": [],
        "source": "User bertemu Budi, seorang backend engineer di Tokopedia.",
        "expected": {"min_docs": 1, "expect_actions": ["create"],
                     "expect_types": ["person"], "require_links": False},
    },
    {
        "id": "kb_create_place_1", "level": 1,
        "name": "Create place doc",
        "description": "Author a single place doc from a simple fact.",
        "existing": [],
        "source": "User tinggal di Bandung, sebuah kota di Jawa Barat.",
        "expected": {"min_docs": 1, "expect_actions": ["create"],
                     "expect_types": ["place"], "require_links": False},
    },

    # ---- Level 2: create that mentions other subjects -> inline links required ----
    {
        "id": "kb_create_links_1", "level": 2,
        "name": "Create with inline links",
        "description": "A fact mentioning multiple subjects must weave inline [[links]].",
        "existing": [],
        "source": "User bekerja sebagai engineer di Evonic, sebuah perusahaan AI, "
                  "bersama rekannya Sari yang menjadi product manager.",
        "expected": {"min_docs": 1,
                     "expect_actions": ["create"],
                     "expect_types": ["person", "organization", "company", "note", "place"],
                     "require_links": True},
    },
    {
        "id": "kb_create_event_1", "level": 2,
        "name": "Create event with links",
        "description": "An event mentioning a place and host should link them inline.",
        "existing": [],
        "source": "User menghadiri konferensi AI Summit di Jakarta yang diselenggarakan "
                  "oleh Nusantara Tech.",
        "expected": {"min_docs": 1,
                     "expect_actions": ["create"],
                     "expect_types": ["note", "session", "organization", "company", "place", "venue"],
                     "require_links": True},
    },

    # ---- Level 3: richer source -> multiple docs with cross-links ----
    {
        "id": "kb_multidoc_trip_1", "level": 3,
        "name": "Multiple docs from a trip",
        "description": "A richer narrative should yield several linked docs.",
        "existing": [],
        "source": "User berlibur ke Yogyakarta, mengunjungi Candi Borobudur, dan makan "
                  "gudeg di Warung Bu Tjitro bersama temannya Andi.",
        "expected": {"min_docs": 2,
                     "expect_actions": ["create"],
                     "expect_types": ["person", "place", "venue", "note", "organization", "company"],
                     "require_links": True},
    },
    {
        "id": "kb_multidoc_startup_1", "level": 3,
        "name": "Multiple docs from a startup story",
        "description": "Founder, company, and product should each be a linked doc.",
        "existing": [],
        "source": "User mendirikan startup bernama Larisin yang membuat produk POS bernama "
                  "LarisinKasir untuk UMKM di Indonesia.",
        "expected": {"min_docs": 2,
                     "expect_actions": ["create"],
                     "expect_types": ["person", "company", "organization", "product", "note", "place"],
                     "require_links": True},
    },

    # ---- Level 4: dedup -> update an existing doc with delta only ----
    {
        "id": "kb_update_place_1", "level": 4,
        "name": "Update existing place",
        "description": "New info about an already-known subject must be an update, not a duplicate.",
        "existing": [{"slug": "jakarta", "title": "Jakarta",
                      "description": "Capital of Indonesia; User's home city."}],
        "source": "User membuka kantor cabang baru perusahaannya di Jakarta tahun ini.",
        "expected": {"min_docs": 1, "expect_actions": ["update"],
                     "existing_slugs": ["jakarta"], "expect_update_slug": "jakarta",
                     "require_links": False},
    },
    {
        "id": "kb_update_person_1", "level": 4,
        "name": "Update existing person",
        "description": "A new fact about a known person updates that doc by slug.",
        "existing": [{"slug": "budi", "title": "Budi",
                      "description": "Backend engineer at Tokopedia."}],
        "source": "Budi baru saja dipromosikan menjadi engineering lead di Tokopedia.",
        "expected": {"min_docs": 1, "expect_actions": ["update"],
                     "existing_slugs": ["budi"], "expect_update_slug": "budi",
                     "require_links": False},
    },

    # ---- Level 5: edge cases ----
    {
        "id": "kb_skip_chatter_1", "level": 5,
        "name": "Skip ephemeral chatter",
        "description": "Pure pleasantries have nothing durable; the answer is no docs.",
        "existing": [],
        "source": "User menyapa, menanyakan kabar, kami bertukar salam, lalu User "
                  "mengucapkan terima kasih dan pamit.",
        "expected": {"expect_empty": True},
    },
    {
        "id": "kb_mixed_create_update_1", "level": 5,
        "name": "Mixed create + update",
        "description": "One known subject updated, one new subject created, both linked.",
        "existing": [{"slug": "jakarta", "title": "Jakarta",
                      "description": "Capital of Indonesia; User's home city."}],
        "source": "User memperkenalkan investornya, Rina, dari Sequoia, dan menyebut "
                  "bahwa kantornya di Jakarta kini punya 50 karyawan.",
        "expected": {"min_docs": 2,
                     "expect_actions": ["create", "update"],
                     "existing_slugs": ["jakarta"],
                     "require_links": True},
    },
]


def build_test(scenario):
    """Return the on-disk test JSON dict for a scenario."""
    return {
        "id": scenario["id"],
        "name": scenario["name"],
        "description": scenario["description"],
        "prompt": render_prompt(scenario["existing"], scenario["source"]),
        "expected": scenario["expected"],
        "evaluator_id": "knowledge_builder",
        "timeout_ms": 30000,
        "weight": 1.0,
        "enabled": True,
    }


def main():
    written = []
    for sc in SCENARIOS:
        level_dir = os.path.join(THIS_DIR, f"level_{sc['level']}")
        os.makedirs(level_dir, exist_ok=True)
        path = os.path.join(level_dir, f"{sc['id']}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(build_test(sc), f, ensure_ascii=False, indent=2)
            f.write("\n")
        written.append(path)
    print(f"Wrote {len(written)} test files:")
    for p in written:
        print("  " + os.path.relpath(p, THIS_DIR))


if __name__ == "__main__":
    main()
