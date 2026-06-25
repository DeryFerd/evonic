# DirExplorer — Codebase Exploration Tools

DirExplorer provides three read-only tools for exploring a codebase: **Read**, **Grep**, and **Glob**. These tools are designed for agents that need to inspect project files, search for patterns, and discover file structures without write access.

## Tools

| Tool | Purpose |
|------|---------|
| **Read** | Read a text file with 1-based line numbers and pagination |
| **Grep** | Regex search across files using ripgrep |
| **Glob** | Find files matching a glob pattern (supports `**`) |

## Workspace Restriction

When an agent is assigned a **workspace** directory, all three tools enforce a strict boundary: the agent cannot access any file or directory outside its workspace. This is a security hardening that blocks three attack vectors:

| Attack Vector | Example | Blocked By |
|---------------|---------|------------|
| Relative path traversal | `../../etc/passwd` | `os.path.realpath` prefix check |
| Absolute path escape | `/etc/shadow` | Workspace boundary validation |
| Symlink attacks | Symlink inside workspace pointing to `/etc/` | `os.path.realpath` resolves symlinks |

### How It Works

The `_validate_workspace_boundary()` function in `backend/tools/_utils.py` performs the check:

1. Resolves both the requested path and the workspace to their canonical absolute paths using `os.path.realpath()` (which follows symlinks)
2. Checks whether the resolved path is equal to or a subpath of the workspace
3. Raises `PermissionError` if the path escapes the workspace

This is called after path resolution and auto-correction but before any file I/O operation (`open()`, `subprocess.run()`, `glob.glob()`).

### Behavior by Agent Type

- **Agent with workspace**: All Read, Grep, and Glob operations are restricted to the workspace directory. Any attempt to escape results in an access-denied error.
- **Agent without workspace**: No restriction — tools can access any path the process has permission to read.

## Requirements

- **ripgrep** (`rg`) must be installed for Grep to work
- Python 3.8+
