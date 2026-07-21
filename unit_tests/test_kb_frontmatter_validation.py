"""
Tests for KB-file frontmatter validation (mandatory title/description/type).

See backend/agent_runtime/context.py::validate_kb_frontmatter.
"""
import pytest

from backend.agent_runtime.context import validate_kb_frontmatter, KB_VALID_TYPES


def _doc(title="My Title", description="A summary", type_="note", body="\nBody.\n"):
    return (
        "---\n"
        f"title: \"{title}\"\n"
        f"description: \"{description}\"\n"
        f"type: {type_}\n"
        "---\n" + body
    )


def test_valid_frontmatter_passes():
    assert validate_kb_frontmatter(_doc()) is None


@pytest.mark.parametrize("type_", KB_VALID_TYPES)
def test_each_valid_type_passes(type_):
    assert validate_kb_frontmatter(_doc(type_=type_)) is None


def test_missing_frontmatter_block_rejected():
    err = validate_kb_frontmatter("# Just a heading\n\nNo frontmatter here.")
    assert err and "frontmatter" in err.lower()


def test_unterminated_frontmatter_rejected():
    err = validate_kb_frontmatter("---\ntitle: \"X\"\ndescription: \"Y\"\ntype: note\n")
    assert err and "Unterminated" in err


def test_missing_title_rejected():
    doc = "---\ndescription: \"Y\"\ntype: note\n---\nbody"
    err = validate_kb_frontmatter(doc)
    assert err and "title" in err


def test_missing_description_rejected():
    doc = "---\ntitle: \"X\"\ntype: note\n---\nbody"
    err = validate_kb_frontmatter(doc)
    assert err and "description" in err


def test_missing_type_rejected():
    doc = "---\ntitle: \"X\"\ndescription: \"Y\"\n---\nbody"
    err = validate_kb_frontmatter(doc)
    assert err and "type" in err


def test_empty_value_rejected():
    doc = "---\ntitle: \"\"\ndescription: \"Y\"\ntype: note\n---\nbody"
    err = validate_kb_frontmatter(doc)
    assert err and "title" in err


def test_invalid_type_rejected():
    err = validate_kb_frontmatter(_doc(type_="log"))
    assert err and "log" in err and "note" in err


def test_message_lists_all_missing_fields():
    doc = "---\ntype: note\n---\nbody"
    err = validate_kb_frontmatter(doc)
    assert "title" in err and "description" in err
