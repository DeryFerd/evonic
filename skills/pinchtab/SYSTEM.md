# PinchTab Browser Automation

## Overview
You have access to PinchTab — a browser automation framework that lets you control real Chrome browser instances through an HTTP API. With PinchTab, you can navigate the web, extract page content, interact with elements, take screenshots, and manage browser tabs.

PinchTab runs as a **sidecar process** — you don't manage its lifecycle. It must already be running at the configured host and port (default: `localhost:9867`).

## When to Use PinchTab Tools

- **Web research:** Navigate to documentation, read articles, search for information
- **Content extraction:** Scrape text or structured data from web pages
- **Form interaction:** Fill out and submit web forms
- **QA testing:** Verify web page content, check for elements
- **Screenshots:** Capture visual state of web pages

## How It Works

PinchTab manages Chrome browser instances with tabs. The typical workflow:

1. **Check health** (`pinchtab_health`) — verify the server is reachable
2. **List instances** (`pinchtab_list_instances`) — find available browser instances
3. **Open a tab** (`pinchtab_new_tab`) — create a new tab (optionally navigate to a URL)
4. **Inspect content** — use `pinchtab_snapshot` (accessibility tree, token-efficient) or `pinchtab_get_text` (plain text)
5. **Interact** — use `pinchtab_click` and `pinchtab_type` to interact with elements
6. **Capture visuals** — use `pinchtab_screenshot` when you need to see the page visually

## Tool Reference

### Connection & Discovery
| Tool | Description |
|------|-------------|
| `pinchtab_health` | Check if PinchTab server is reachable and healthy |
| `pinchtab_list_instances` | List all browser instances with their IDs |

### Tab Management
| Tool | Description |
|------|-------------|
| `pinchtab_new_tab` | Open a new tab in a browser instance |

### Navigation & Content
| Tool | Description |
|------|-------------|
| `pinchtab_navigate` | Navigate a tab to a URL |
| `pinchtab_snapshot` | Get accessibility tree (structured, token-efficient) |
| `pinchtab_get_text` | Extract plain text content |

### Interaction
| Tool | Description |
|------|-------------|
| `pinchtab_click` | Click an element by CSS selector or snapshot `ref` |
| `pinchtab_type` | Type text into an input element |

### Scripting
| Tool | Description |
|------|-------------|
| `pinchtab_eval` | Evaluate a JavaScript expression in page context |

### Visual Capture
| Tool | Description |
|------|-------------|
| `pinchtab_screenshot` | Take a screenshot. Returns base64 by default; use `output_mode="file"` to save to disk instead. |

### Screenshot `output_mode`

`pinchtab_screenshot` accepts an optional `output_mode` parameter:

- **`output_mode="inline"`** (default, backward compatible) — returns the screenshot as a base64-encoded string in the JSON response. Example: `{"screenshot": "/9j/4AAQ...", "format": "jpeg"}`.
- **`output_mode="file"`** — decodes the base64 data and writes the screenshot to a temporary file on disk. Returns only the file path and format. Example: `{"file_path": "/tmp/pinchtab_screenshot_<uuid>.jpeg", "format": "jpeg"}`.

**When to use `output_mode="file"`:**
- When the screenshot is large and would flood the context window with base64 data
- When you need to reference the same screenshot multiple times without duplicating base64
- When you are taking many screenshots in a single session

**Important:** The temporary file is NOT deleted after the tool returns — the caller is responsible for reading or processing it. The file persists until the system cleans up `/tmp`.

## Selectors for Click & Type

When using `pinchtab_click` or `pinchtab_type`, the `selector` parameter accepts:

- **Snapshot `ref`** (recommended): Use the `ref` field from `pinchtab_snapshot` results, e.g. `"e21"`, `"e5"`. This is the most reliable way to target elements.
- **CSS selectors**: Standard CSS selectors like `"input"`, `"#submit-btn"`, `".nav-link"`, `"button[type=submit]"`.

**WARNING:** Do NOT use the `nodeId` field from snapshot results as a selector. `nodeId` is Chrome's internal DOM identifier and is not a valid selector. For example, if a snapshot node shows `"ref": "e21", "nodeId": 361`, use `"e21"` — never `"node361"` or `"361"`.

## Handling Dialogs / Modal Overlays

When a page has a modal dialog, the snapshot includes **both** dialog elements and background page elements. Only elements inside the dialog are clickable — background elements will fail with "element is occluded".

To handle this correctly:
1. **Look for `role: "dialog"` nodes** in the snapshot — these indicate a modal overlay.
2. **Only interact with elements inside the dialog** (children of the dialog node at higher depth). Ignore duplicate elements that appear at lower depth in the background.
3. If you see an "occluded" error, take a fresh snapshot and look for the same element inside a dialog node.

## Stale Refs After Actions

Refs from `pinchtab_snapshot` can become invalid after any action that changes the page (type, click, navigate). If you get "ref not found" or "Node is detached" errors:
1. **Always take a fresh snapshot** after type/click/navigate before interacting with new elements.
2. Never reuse refs from a previous snapshot after a page state change.

## Token Efficiency

**Prefer `pinchtab_snapshot` over `pinchtab_screenshot`** for content analysis. The accessibility tree is 5-13x more token-efficient than screenshots. Only use screenshots when you actually need to see the visual layout.

## Safety

- URL navigation is safety-checked: `file://`, `chrome://`, `javascript:`, and `data:` schemes are blocked
- Navigation to localhost/internal IPs is blocked to prevent SSRF
- All PinchTab tools run through Evonic's standard authorization guard — agents must be explicitly assigned these tools

## Configuration

PinchTab connection is configured via environment variables:
- `PINCHTAB_HOST` — hostname (default: `localhost`)
- `PINCHTAB_PORT` — port (default: `9867`)

## Error Handling

If a tool returns an error about PinchTab being unreachable:
- Verify PinchTab is running with: `pinchtab serve`
- Check the host/port configuration
- The `pinchtab_health` tool is the fastest way to diagnose connection issues

## Known Issues & Workarounds

### DNS Resolution Failures ("could not resolve navigation host")

PinchTab's Go bridge performs DNS resolution **before** passing navigation to Chrome. In WSL environments, the DNS proxy (`10.255.255.254`) can time out on AAAA (IPv6) queries for certain CDN-backed domains: **x.com**, **nitter.net**, and others.

Chrome itself resolves these domains without issues (it uses `--dns-servers=8.8.8.8` with async DNS).

**Workaround — JS navigation via `pinchtab_eval`:**

```
# 1. Open a blank tab
pinchtab_new_tab()

# 2. Navigate via JavaScript (bypasses Go's DNS check)
pinchtab_eval(tab_id="<tab_id>", expression="window.location.href = 'https://x.com'")
```

This works because `pinchtab_eval` sends JavaScript directly to Chrome — no Go-side DNS resolution is performed.

### IDPI Scanner Blocking `pinchtab_snapshot`

Some pages — particularly those with code templates, system prompts, or role-based examples (e.g. HuggingFace model pages) — contain text that matches IDPI detection patterns. This produces a false-positive 403:

```
"snapshot blocked by IDPI scanner: exfiltration patterns detected; role-hijack pattern detected"
```

**Workaround:** Fall back to `pinchtab_get_text` for plain text content, or use `pinchtab_eval` to extract specific DOM elements:

```
pinchtab_get_text(tab_id="<tab_id>")
pinchtab_eval(tab_id="<tab_id>", expression="document.querySelector('.content').innerText")
```
