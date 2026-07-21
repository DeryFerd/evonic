"""
kb_graph tool — query the KB document link graph (1-hop only).

Returns outgoing references, incoming references, dangling links,
and same-tag related documents for a given KB file.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta

from backend.agent_runtime.evomem_client import query_kb_graph


def _format_age(updated_at: str | None) -> str:
    """Format an ISO timestamp as a relative age string."""
    if not updated_at:
        return ""
    try:
        dt = datetime.fromisoformat(updated_at)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        delta = now - dt
        days = delta.days
        if days == 0:
            return "today"
        elif days == 1:
            return "1 day ago"
        else:
            return f"{days} days ago"
    except (ValueError, TypeError):
        return ""


def execute(agent, args: dict) -> dict:
    """Return the link graph for a KB file (1-hop neighbors only).

    Args:
        agent: Agent context dict (contains agent_id).
        args: Must contain 'filename' (string) — KB filename without path.

    Returns:
        dict with 'result' (formatted text) or 'error'.
    """
    filename = (args.get("filename") or "").strip()

    if not filename:
        return {"error": "Missing required parameter: filename"}

    if not filename.endswith(".md"):
        return {
            "error": (
                f"Invalid KB filename: '{filename}'. "
                "KB files must be markdown (.md)."
            )
        }

    agent_id = agent.get("agent_id") or agent.get("id") or ""
    if not agent_id:
        return {"error": "Agent context missing agent_id"}

    graph = query_kb_graph(agent_id, filename)

    if graph is None:
        return {
            "error": (
                f"KB file '{filename}' not found in the knowledge graph. "
                "It may not exist on disk yet, or may not have been synced to evomem."
            )
        }

    # Build formatted output
    lines = []
    remaining = 6
    source = graph["source"]
    lines.append(f"## KB Graph: {filename}\n")

    # Outgoing references section
    outgoing = graph["outgoing"][:remaining]
    remaining -= len(outgoing)
    dangling = graph["outgoing_dangling"][:remaining]
    remaining -= len(dangling)
    total_out = len(outgoing) + len(dangling)

    lines.append(f"→ references ({total_out}):")
    for o in outgoing:
        age = _format_age(o.get("updated_at"))
        title = o.get("title", o["slug"])
        if age:
            lines.append(f"  - {o['slug']} ({title}) — last updated {age}")
        else:
            lines.append(f"  - {o['slug']} ({title})")
    for d in dangling:
        lines.append(f"  - ⚠ dangling: {d} (target page does not exist)")
    if total_out == 0:
        lines.append("  <none>")

    lines.append("")

    # Incoming references section
    incoming = graph["incoming"][:remaining]
    remaining -= len(incoming)
    lines.append(f"↑ referenced by ({len(incoming)}):")
    for inc in incoming:
        title = inc.get("title", inc["slug"])
        lines.append(f"  - {inc['slug']} ({title})")
    if not incoming:
        lines.append("  <none>")

    lines.append("")

    # Same-tag discovery
    same_tag = graph["same_tag_docs"]
    if remaining and same_tag and source.get("tags"):
        tags = source["tags"]
        # Group by tag
        by_tag = {}
        for t in tags:
            by_tag[t] = []
        for doc in same_tag:
            for t in tags:
                if t in (doc.get("tags") or []):
                    by_tag[t].append(doc)

        for tag in tags:
            related = by_tag.get(tag, [])
            if related and remaining:
                lines.append(f"Related by tag [{tag}]:")
                for r in related[:remaining]:
                    lines.append(f"  - {r['slug']} ({r.get('title', r['slug'])})")
                    remaining -= 1
            if not remaining:
                break

    return {"result": "\n".join(lines)}


# Self-test
def test_execute():
    """Verify the function signature and basic error handling."""
    # Missing filename
    result = execute({"agent_id": "test"}, {})
    assert "error" in result
    assert "Missing" in result["error"]

    # Empty filename
    result = execute({"agent_id": "test"}, {"filename": "  "})
    assert "error" in result
    assert "Missing" in result["error"]

    # Non-.md filename
    result = execute({"agent_id": "test"}, {"filename": "notes.txt"})
    assert "error" in result
    assert ".md" in result["error"]

    # Missing agent_id
    result = execute({}, {"filename": "evonic.md"})
    assert "error" in result
    assert "agent_id" in result["error"]
