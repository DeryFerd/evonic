"""
panel_remove_action — remove an action button from an agent's panel.
"""

from plugins.panel.db import PanelDB


def execute(agent: dict, args: dict) -> dict:
    """Remove an action button from an agent's panel.

    Authorization: agent can only remove actions from their own panel,
    unless they are the super agent.

    Args:
        agent_id: The agent ID whose panel the action belongs to.
        action_id: The ID of the action to remove.

    Returns:
        {success: true, action_id: ...} on success
        {success: false, error: "..."} on failure
    """
    agent_id = args.get("agent_id", "").strip()
    action_id = args.get("action_id")

    # ── authorization ──────────────────────────────────────────
    caller_id = agent.get("id", "")
    is_super = agent.get("is_super", False)

    if not is_super and agent_id != caller_id:
        return {
            "success": False,
            "error": "You can only remove actions from your own panel.",
        }

    # ── validation ─────────────────────────────────────────────
    if not agent_id:
        return {"success": False, "error": "agent_id is required."}

    if action_id is None:
        return {"success": False, "error": "action_id is required."}

    if not isinstance(action_id, int):
        try:
            action_id = int(action_id)
        except (ValueError, TypeError):
            return {"success": False, "error": "action_id must be an integer."}

    # ── delete action ──────────────────────────────────────────
    try:
        db = PanelDB(agent_id)
        deleted = db.delete_action(action_id)
        if not deleted:
            return {
                "success": False,
                "error": f"Action with id {action_id} not found for agent '{agent_id}'.",
            }
        try:
            from backend.event_stream import event_stream
            event_stream.emit('panel_updated', {'agent_id': agent_id})
        except Exception:
            pass
        return {"success": True, "action_id": action_id}
    except Exception as e:
        return {"success": False, "error": f"Failed to remove action: {e}"}
