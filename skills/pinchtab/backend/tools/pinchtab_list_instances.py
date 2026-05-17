"""pinchtab_list_instances — list all browser instances managed by PinchTab."""

from ._pinchtab_api import _api


def execute(agent: dict, args: dict) -> dict:
    """List all PinchTab browser instances.

    Returns a list of instances with their IDs, profiles, and status.
    """
    result = _api("GET", "/instances")
    if "error" in result:
        return result
    instances = result if isinstance(result, list) else result.get("instances", [])
    return {
        "count": len(instances),
        "instances": instances,
    }
