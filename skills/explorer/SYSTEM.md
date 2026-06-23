# Explorer

The `explorer` skill lets you delegate read-only investigation of any directory
or codebase — including paths **outside your own workspace** — to a temporary
explorer sub-agent.

## When to use

- You need to understand the structure or contents of a directory you don't
  have direct access to (e.g. another project at `/home/www/web-a`).
- You want to search/read across a large tree without filling your own context
  with the raw file dumps — the explorer does the digging and reports a summary.
- You want to investigate several locations at once — call `Explore` multiple
  times to run explorers in parallel.

## How to use

Call `Explore` with the absolute path to explore. Optionally pass `context_vars`
to fill `{{placeholders}}` in the explorer's configured system prompt (for
example, the specific question you want answered).

```
Explore({"path": "/home/www/web-a", "context_vars": {"query": "where is auth handled?"}})
```

- `path` (required): an existing directory. It becomes the explorer's root —
  the explorer's Grep/Read/Glob are confined to it. Accepts an absolute host
  path (including outside your workspace), `/workspace` (your own workspace), or
  a path relative to your workspace.
- `context_vars` (optional): flat key→value pairs injected into the explorer's
  system prompt via `{{key}}` placeholders.

The call returns immediately with the explorer's ID. The explorer runs
independently and **reports its findings back to your session automatically**
when finished — you do not need to poll it.

The explorer is read-only (it cannot modify files) and is automatically cleaned
up after it finishes / goes idle.
