"""pinchtab_type — type text into an input element in a browser tab."""

from ._pinchtab_api import _api


def execute(agent: dict, args: dict) -> dict:
    """Type text into an element in a browser tab.

    Uses the unified /action endpoint with kind=type.
    The selector supports CSS selectors, accessibility node refs,
    XPath, or semantic queries.

    Args:
        tab_id: ID of the tab.
        selector: CSS selector or accessibility node ID of the input element.
        text: The text to type.

    Returns:
        Type result from PinchTab.
    """
    tab_id = args.get("tab_id", "")
    selector = args.get("selector", "")
    text = args.get("text", "")

    if not tab_id:
        return {"error": "tab_id is required."}
    if not selector:
        return {"error": "selector is required."}
    if not text:
        return {"error": "text is required."}

    result = _api("POST", "/action", {
        "tabId": tab_id,
        "kind": "type",
        "selector": selector,
        "text": text,
    })
    if "error" in result:
        return result
    return {
        "message": f"Typed '{text}' into '{selector}'.",
        "result": result,
    }
