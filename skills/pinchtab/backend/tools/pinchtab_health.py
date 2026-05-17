"""pinchtab_health — check if the PinchTab server is reachable and healthy."""

from ._pinchtab_api import _api


def execute(agent: dict, args: dict) -> dict:
    """Check PinchTab server health.

    Returns server info if healthy, or an error dict if unreachable.
    """
    result = _api("GET", "/health")
    if "error" in result:
        return result
    return {
        "status": "healthy",
        "server_info": result,
    }
