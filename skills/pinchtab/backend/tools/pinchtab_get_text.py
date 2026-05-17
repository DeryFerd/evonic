"""pinchtab_get_text — extract plain text content from a browser tab."""

from ._pinchtab_api import _api


def execute(agent: dict, args: dict) -> dict:
    """Extract plain text content from a browser tab.

    Args:
        tab_id: ID of the tab to extract text from.

    Returns:
        The page's text content.
    """
    tab_id = args.get("tab_id", "")
    if not tab_id:
        return {"error": "tab_id is required."}

    result = _api("GET", f"/text?tabId={tab_id}")
    if "error" in result:
        return result

    text = result.get("text", "") if isinstance(result, dict) else str(result)
    return {
        "tab_id": tab_id,
        "text": text,
    }
