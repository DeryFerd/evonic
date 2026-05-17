"""pinchtab_snapshot — get the accessibility tree snapshot of a tab.

This is the most token-efficient way to inspect page content
(5-13x cheaper than screenshots in token consumption).
"""

from ._pinchtab_api import _api


def execute(agent: dict, args: dict) -> dict:
    """Get the accessibility tree snapshot of a browser tab.

    The snapshot contains the page's accessibility tree — a structured
    representation of all elements, their roles, labels, and states.
    This is ideal for LLM-based content analysis because it's compact
    and already structured.

    Args:
        tab_id: ID of the tab to snapshot.

    Returns:
        The accessibility tree as a structured dict.
    """
    tab_id = args.get("tab_id", "")
    if not tab_id:
        return {"error": "tab_id is required."}

    result = _api("GET", f"/snapshot?tabId={tab_id}")
    if "error" in result:
        return result
    return {
        "tab_id": tab_id,
        "snapshot": result,
    }
