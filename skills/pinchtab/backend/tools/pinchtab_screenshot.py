"""pinchtab_screenshot — take a screenshot of a browser tab."""

import base64
import uuid

from ._pinchtab_api import _api


def execute(agent: dict, args: dict) -> dict:
    """Take a screenshot of a browser tab.

    Uses GET /screenshot?tabId=X shorthand endpoint.

    Args:
        tab_id: ID of the tab to screenshot.
        full_page: If true, capture the full scrollable page (default: false).
        output_mode: "inline" (default) returns base64 in JSON;
                     "file" writes to a temp file and returns the path.

    Returns:
        With output_mode="inline": {"tab_id": ..., "format": ..., "screenshot": "base64..."}
        With output_mode="file":   {"tab_id": ..., "format": ..., "file_path": "/tmp/..."}
    """
    tab_id = args.get("tab_id", "")
    full_page = args.get("full_page", False)
    output_mode = args.get("output_mode", "inline")

    if not tab_id:
        return {"error": "tab_id is required."}

    if output_mode not in ("inline", "file"):
        return {"error": f"Invalid output_mode '{output_mode}'. Must be 'inline' or 'file'."}

    params = f"tabId={tab_id}"
    if full_page:
        params += "&full_page=true"

    result = _api("GET", f"/screenshot?{params}")
    if "error" in result:
        return result

    img_format = result.get("format", "jpeg")
    img_b64 = result.get("base64", "")

    if output_mode == "file":
        ext = img_format if img_format in ("jpeg", "png") else "jpeg"
        filename = f"/tmp/pinchtab_screenshot_{uuid.uuid4().hex}.{ext}"
        with open(filename, "wb") as f:
            f.write(base64.b64decode(img_b64))
        return {
            "tab_id": tab_id,
            "full_page": full_page,
            "format": img_format,
            "file_path": filename,
        }

    return {
        "tab_id": tab_id,
        "full_page": full_page,
        "format": img_format,
        "screenshot": img_b64,
    }
