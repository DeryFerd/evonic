"""
panel_list_actions — list all action buttons on an agent's panel.
"""

from plugins.panel.db import PanelDB


def execute(agent: dict, args: dict) -> dict:
    """List all action buttons on an agent's panel.

    Authorization: agent can only list their own panel's actions,
    unless they are the super agent.

    Args:
        agent_id: The agent ID whose panel actions to list.

    Returns:
        {success: true, actions: [...]} on success
        {success: false, error: "..."} on failure
    """
    agent_id = args.get("agent_id", "").strip()

    # ── authorization ──────────────────────────────────────────
    caller_id = agent.get("id", "")
    is_super = agent.get("is_super", False)

    if not is_super and agent_id != caller_id:
        return {
            "success": False,
            "error": "You can only list your own panel actions.",
        }

    # ── validation ─────────────────────────────────────────────
    if not agent_id:
        return {"success": False, "error": "agent_id is required."}

    # ── list actions ───────────────────────────────────────────
    try:
        db = PanelDB(agent_id)
        actions = db.list_actions()
        return {"success": True, "actions": actions}
    except Exception as e:
        return {"success": False, "error": f"Failed to list actions: {e}"}
