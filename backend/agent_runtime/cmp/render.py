"""
CMP — session map + card rendering for LLM context injection.

Renders the cmp dict into the "### Session Paths (CMP)" block that
AgentState.render() includes each turn. Tiers (paper §4.2-4.3):
  - map (Mermaid): always, topology + status + position only
  - active path: full card
  - dependency ancestors of the active path: compact cards (pinned)
  - dormant paths: one-line cards
  - archived paths: map node only
Hard cap keeps the whole section bounded regardless of session length.
"""
from __future__ import annotations

from backend.agent_runtime.cmp.store import dependency_ancestors

RENDER_MAX_CHARS = 4000

_STATUS_LABEL = {
    "active": "ACTIVE",
    "dormant": "dormant",
    "archived": "archived",
}


def _node_label(path: dict, active: bool) -> str:
    title = path.get("title") or path["id"]
    status = "ACTIVE" if active else _STATUS_LABEL.get(path.get("status"), "?")
    outcome = (path.get("outcome") or "").strip()
    suffix = f": {outcome[:40]}" if outcome and not active else ""
    return f'{path["id"]}["{title[:48]} [{status}]{suffix}"]'


def render_map(cmp: dict) -> str:
    """Compact Mermaid flowchart of the session graph."""
    lines = ["flowchart TD", "  U((user))"]
    for pid in sorted(cmp["paths"]):
        path = cmp["paths"][pid]
        lines.append(f"  U --> {_node_label(path, pid == cmp['active_id'])}")
    for pid in sorted(cmp["paths"]):
        for dep in cmp["paths"][pid].get("depends_on") or []:
            if dep in cmp["paths"]:
                lines.append(f"  {pid} -. depends .-> {dep}")
    return "\n".join(lines)


def _full_card(path: dict) -> list:
    lines = [f"**{path['id']} — {path.get('title') or '(untitled)'}** "
             f"(active)"]
    if path.get("goal"):
        lines.append(f"- goal: {path['goal']}")
    if path.get("outcome"):
        lines.append(f"- outcome so far: {path['outcome']}")
    for fact in path.get("key_facts") or []:
        lines.append(f"- {fact}")
    if path.get("artifacts"):
        lines.append(f"- artifacts: {', '.join(path['artifacts'])}")
    if path.get("depends_on"):
        lines.append(f"- depends on: {', '.join(path['depends_on'])}")
    return lines


def _compact_card(path: dict, reason: str) -> list:
    lines = [f"**{path['id']} — {path.get('title') or '(untitled)'}** ({reason})"]
    if path.get("goal"):
        lines.append(f"- goal: {path['goal']}")
    if path.get("outcome"):
        lines.append(f"- outcome: {path['outcome']}")
    for fact in (path.get("key_facts") or [])[:3]:
        lines.append(f"- {fact}")
    if path.get("artifacts"):
        lines.append(f"- artifacts: {', '.join(path['artifacts'][:4])}")
    return lines


def _one_line_card(path: dict) -> str:
    summary = path.get("outcome") or path.get("goal") or ""
    return f"- {path['id']} {path.get('title') or ''} — {summary[:100]} (dormant)"


def render_cmp_section(cmp: dict) -> str:
    """Full CMP block for the agent-state system message."""
    if not cmp or not cmp.get("paths"):
        return ""
    active_id = cmp["active_id"]
    lines = ["```mermaid", render_map(cmp), "```", ""]

    active = cmp["paths"].get(active_id)
    if active:
        lines.extend(_full_card(active))

    ancestors = dependency_ancestors(cmp, active_id)
    for dep_id in ancestors:
        lines.append("")
        lines.extend(_compact_card(cmp["paths"][dep_id], "dependency of active path"))

    dormant = [p for pid, p in sorted(cmp["paths"].items())
               if p.get("status") == "dormant"
               and pid != active_id and pid not in ancestors]
    if dormant:
        lines.append("")
        lines.append("Other recent paths:")
        lines.extend(_one_line_card(p) for p in dormant)

    lines.append("")
    lines.append("Use switch_path(path_id) to resume another path, or "
                 "new_path(title) to start an unrelated task as its own path.")

    text = "\n".join(lines)
    if len(text) > RENDER_MAX_CHARS:
        text = text[:RENDER_MAX_CHARS] + "\n…[session map truncated]"
    return text
