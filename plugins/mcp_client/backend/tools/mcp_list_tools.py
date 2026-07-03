"""
mcp_list_tools.py — list tools exposed by configured MCP servers.

Companion to mcp_call. Servers are configured via the mcp_client plugin's
MCP_SERVERS variable; the plugin's enabled flag gates both tools.
"""

from __future__ import annotations

from typing import Any


def execute(agent: dict, args: dict) -> Any:
    from models.db import db
    if db.get_setting("plugin_enabled:mcp_client") != "1":
        return {"error": "MCP Client plugin is disabled. Enable it in Plugins settings."}

    from ._mcp_lib import MCPError, mcp_manager

    server = args.get("server")
    server = server.strip() if isinstance(server, str) else ""

    try:
        names = [server] if server else mcp_manager.server_names()
    except MCPError as e:
        return {"error": str(e)}

    if not names:
        return {
            "error": "No MCP servers configured. Ask an admin to configure "
                     "MCP_SERVERS in the MCP Client plugin."
        }

    call_timeout, startup_timeout = mcp_manager.get_timeouts()
    results = []
    for name in names:
        try:
            srv = mcp_manager.get(name)
            tools = srv.list_tools(call_timeout, startup_timeout)
            results.append({
                "server": name,
                "tools": [
                    {
                        "name": t.get("name", ""),
                        "description": (t.get("description") or "")[:500],
                        "input_schema": t.get("inputSchema") or {},
                    }
                    for t in tools
                ],
            })
        except MCPError as e:
            results.append({"server": name, "error": str(e)})

    return results[0] if server else {"servers": results}
