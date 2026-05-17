"""pinchtab_screenshot — take a screenshot of a browser tab."""

from ._pinchtab_api import _api


def execute(agent: dict, args: dict) -> dict:
    """Take a screenshot of a browser tab.

    Uses GET /screenshot?tabId=X shorthand endpoint.

    Args:
        tab_id: ID of the tab to screenshot.
        full_page: If true, capture the full scrollable page (default: false).

    Returns:
        Screenshot result with base64-encoded image data.
    """
    tab_id = args.get("tab_id", "")
    full_page = args.get("full_page", False)

    if not tab_id:
        return {"error": "tab_id is required."}

    params = f"tabId={tab_id}"
    if full_page:
        params += "&full_page=true"

    result = _api("GET", f"/screenshot?{params}")
    if "error" in result:
        return result
    return {
        "tab_id": tab_id,
        "full_page": full_page,
        "format": result.get("format", "jpeg"),
        "screenshot": result.get("base64", ""),
    }
