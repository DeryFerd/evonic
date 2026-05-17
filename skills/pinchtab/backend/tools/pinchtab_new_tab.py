"""pinchtab_new_tab — open a new browser tab in a PinchTab instance."""

from ._pinchtab_api import _api


def execute(agent: dict, args: dict) -> dict:
    """Open a new tab in a PinchTab instance.

    Uses POST /tab shorthand for default routing (no instance_id needed).
    Optionally accepts instance_id to target a specific instance via
    POST /instances/{id}/tabs/open.

    Args:
        instance_id: Optional ID of the browser instance to create a tab in.
        url: Optional URL to navigate the new tab to.

    Returns:
        The new tab's info including its tab_id.
    """
    url = args.get("url", "")
    instance_id = args.get("instance_id", "")

    body = {"action": "new"}
    if url:
        body["url"] = url

    if instance_id:
        result = _api("POST", f"/instances/{instance_id}/tabs/open", body)
    else:
        result = _api("POST", "/tab", body)

    if "error" in result:
        return result
    return {
        "message": "Tab created successfully.",
        "tab": result,
    }
