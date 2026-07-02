"""
Panel Plugin — Flask Route Handlers

Provides:
- GET  /plugin/panel/<agent_id>/render       → HTML panel UI with action buttons
- GET  /plugin/panel/<agent_id>/actions      → JSON list of enabled actions
- POST /plugin/panel/<agent_id>/execute/<int:action_id> → Execute action (script/prompt)
- GET  /plugin/panel/<agent_id>/execution/<execution_id>/status → Poll execution status
- GET  /plugin/panel/<agent_id>/log          → Execution log entries

Security:
- Routes use /plugin/panel/ prefix (CSRF-exempt in app.py)
- Requesting user must be authenticated (session-based)
- Agent must have run_as_user set before script execution
- Per-agent mutex prevents concurrent script execution
"""

import json
import os
import re
import threading
import time
import uuid
import subprocess
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request, session

from plugins.panel.db import panel_db_factory

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------------------
# In-memory execution state
# ---------------------------------------------------------------------------

# Per-agent locks to prevent concurrent script execution
_agent_locks: dict = {}
_locks_lock = threading.Lock()

# Execution output buffers: execution_id → {"status", "output_lines": [...], "exit_code", "started_at"}
_execution_buffers: dict = {}
_buffers_lock = threading.Lock()


def _get_agent_lock(agent_id: str) -> threading.Lock:
    """Return a per-agent threading.Lock, creating it if needed."""
    with _locks_lock:
        if agent_id not in _agent_locks:
            _agent_locks[agent_id] = threading.Lock()
        return _agent_locks[agent_id]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now():
    return datetime.now(timezone.utc).isoformat()


def _get_plugin_config():
    """Return the panel plugin config dict."""
    try:
        from backend.plugin_manager import plugin_manager
        return plugin_manager.get_plugin_config("panel")
    except Exception:
        return {}


def _get_max_timeout():
    """Return MAX_SCRIPT_TIMEOUT from plugin config, default 60."""
    cfg = _get_plugin_config()
    try:
        return int(cfg.get("MAX_SCRIPT_TIMEOUT", 60))
    except (TypeError, ValueError):
        return 60


def _check_agent_access(agent_id: str):
    """
    Validate the requesting user can access this agent.

    Returns (agent_dict, error_tuple).
    error_tuple is (json_body, status_code) or None if OK.
    """
    if not session.get("authenticated"):
        return None, ({"error": "Authentication required"}, 401)

    try:
        from models.db import db
        agent = db.get_agent(agent_id)
    except Exception as e:
        return None, ({"error": f"Database error: {str(e)}"}, 500)

    if not agent:
        return None, ({"error": "Agent not found"}, 404)

    return agent, None


# ---------------------------------------------------------------------------
# Blueprint factory
# ---------------------------------------------------------------------------

def create_blueprint():
    bp = Blueprint("panel", __name__, url_prefix="/plugin/panel")

    # ── GET /plugin/panel/<agent_id>/render ──────────────────────────────

    @bp.route("/<agent_id>/render")
    def panel_render(agent_id: str):
        """Return the interactive panel HTML with action buttons grid."""
        agent, error = _check_agent_access(agent_id)
        if error:
            return jsonify(error[0]), error[1]

        db = panel_db_factory(agent_id)
        actions = db.list_actions()

        # Build ETag from the latest updated_at timestamp
        max_updated = max(
            (a.get("updated_at", "") for a in actions), default=""
        )
        etag = f'"{hash(max_updated or agent_id)}"'

        # Check If-None-Match for 304
        if_none_match = request.headers.get("If-None-Match", "")
        if if_none_match and if_none_match == etag:
            return "", 304

        # Build the HTML
        html = _render_panel_html(agent_id, agent, actions)

        response = bp.make_response(html)
        response.headers["Content-Type"] = "text/html; charset=utf-8"
        response.headers["ETag"] = etag
        response.headers["Cache-Control"] = "no-cache"
        return response

    # ── GET /plugin/panel/<agent_id>/actions ─────────────────────────────

    @bp.route("/<agent_id>/actions")
    def panel_actions(agent_id: str):
        """Return JSON list of enabled actions."""
        agent, error = _check_agent_access(agent_id)
        if error:
            return jsonify(error[0]), error[1]

        db = panel_db_factory(agent_id)
        actions = db.list_actions()

        return jsonify({"actions": actions})

    # ── POST /plugin/panel/<agent_id>/execute/<int:action_id> ────────────

    @bp.route("/<agent_id>/execute/<int:action_id>", methods=["POST"])
    def panel_execute(agent_id: str, action_id: int):
        """Execute a panel action (script or prompt)."""
        agent, error = _check_agent_access(agent_id)
        if error:
            return jsonify(error[0]), error[1]

        db = panel_db_factory(agent_id)
        action = db.get_action(action_id)

        if not action:
            return jsonify({"error": "Action not found"}), 404

        if not action.get("enabled"):
            return jsonify({"error": "Action is disabled"}), 400

        action_type = action["action_type"]

        # ── Prompt action ────────────────────────────────────────────

        if action_type == "prompt":
            try:
                _send_prompt_to_agent(agent_id, action)
            except Exception as e:
                return jsonify({"error": f"Failed to send prompt: {str(e)}"}), 500

            db.log_execution(
                action_id=action_id,
                action_label=action["label"],
                action_type=action_type,
                params_used=None,
                result="Prompt sent to agent session",
                status="success",
            )

            return jsonify({"status": "prompt_sent"})

        # ── Script action ────────────────────────────────────────────

        if action_type == "script":
            # Check params
            params_def = _parse_params(action.get("params"))
            if params_def:
                provided_params = request.get_json(silent=True) or {}
                if "params" in provided_params:
                    provided_params = provided_params["params"]

                missing = [p for p in params_def if p["name"] not in provided_params]
                if missing:
                    return jsonify({
                        "needs_params": True,
                        "params": params_def,
                    }), 200

                user_params = provided_params
            else:
                user_params = {}

            # Security: reject if run_as_user is None
            run_as_user = (agent.get("run_as_user") or "").strip()
            if not run_as_user:
                return jsonify({
                    "error": "Agent has no run_as_user configured. Script execution requires a run_as_user to be set."
                }), 400

            # Mutual exclusion: per-agent lock, non-blocking
            lock = _get_agent_lock(agent_id)
            if not lock.acquire(blocking=False):
                return jsonify({
                    "error": "A script is already running for this agent",
                    "status": "busy",
                }), 409

            # Generate execution ID and buffer entry
            execution_id = str(uuid.uuid4())
            with _buffers_lock:
                _execution_buffers[execution_id] = {
                    "status": "running",
                    "output_lines": [],
                    "exit_code": None,
                    "started_at": _now(),
                }

            # Offload to daemon thread
            thread = threading.Thread(
                target=_run_script,
                args=(agent_id, action, user_params, execution_id, run_as_user, lock),
                daemon=True,
            )
            thread.start()

            return jsonify({
                "status": "queued",
                "execution_id": execution_id,
            }), 202

        return jsonify({"error": f"Unknown action type: {action_type}"}), 400

    # ── GET /plugin/panel/<agent_id>/execution/<execution_id>/status ────

    @bp.route("/<agent_id>/execution/<execution_id>/status")
    def panel_execution_status(agent_id: str, execution_id: str):
        """Poll execution status and output."""
        agent, error = _check_agent_access(agent_id)
        if error:
            return jsonify(error[0]), error[1]

        with _buffers_lock:
            buf = _execution_buffers.get(execution_id)

        if not buf:
            return jsonify({"error": "Execution not found"}), 404

        return jsonify({
            "status": buf["status"],
            "output_lines": list(buf["output_lines"]),
            "exit_code": buf["exit_code"],
        })

    # ── GET /plugin/panel/<agent_id>/log ─────────────────────────────────

    @bp.route("/<agent_id>/log")
    def panel_log(agent_id: str):
        """Return execution log entries from SQLite."""
        agent, error = _check_agent_access(agent_id)
        if error:
            return jsonify(error[0]), error[1]

        db = panel_db_factory(agent_id)
        try:
            log_entries = db.get_log()
        except Exception:
            log_entries = []

        return jsonify({"log": log_entries})

    return bp


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------

def _render_panel_html(agent_id: str, agent: dict, actions: list) -> str:
    """Build the full HTML page for the panel UI."""

    actions_json = json.dumps(actions)
    agent_name = agent.get("name", agent_id)

    # Escape for embedding in JS string
    agent_name_escaped = agent_name.replace("\\", "\\\\").replace("'", "\\'")

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Panel — {_escape_html(agent_name)}</title>
<script src="https://cdn.tailwindcss.com"></script>
<style>
  .btn-script {{ background-color: #4f46e5; }}
  .btn-script:hover {{ background-color: #4338ca; }}
  .btn-prompt {{ background-color: #059669; }}
  .btn-prompt:hover {{ background-color: #047857; }}
  .btn-disabled {{ opacity: 0.5; cursor: not-allowed; }}
  #output {{ font-family: 'Fira Code', 'Cascadia Code', 'Consolas', monospace; font-size: 13px; }}
</style>
</head>
<body class="bg-gray-900 text-gray-100 min-h-screen">
<div class="max-w-4xl mx-auto p-4 sm:p-6">

  <h2 class="text-xl font-bold mb-4 text-white">
    Panel — <span class="text-indigo-400">{_escape_html(agent_name)}</span>
  </h2>

  <!-- Action Buttons Grid -->
  <div id="actions-grid" class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3 mb-6">
"""

    for action in actions:
        label = _escape_html(action["label"])
        aid = action["id"]
        atype = action["action_type"]
        enabled = action.get("enabled", True)

        if atype == "script":
            color_class = "bg-indigo-600 hover:bg-indigo-700"
        else:
            color_class = "bg-emerald-600 hover:bg-emerald-700"

        disabled_attr = ""
        disabled_class = ""
        if not enabled:
            disabled_attr = "disabled"
            disabled_class = " opacity-50 cursor-not-allowed"

        html += f"""    <button
      class="panel-btn {color_class} text-white font-medium py-3 px-4 rounded-lg transition-colors{disabled_class}"
      data-action-id="{aid}"
      data-action-type="{atype}"
      data-label="{label}"
      {disabled_attr}
    >{label}</button>
"""

    html += """  </div>

  <!-- Params Modal -->
  <div id="params-modal" class="hidden fixed inset-0 bg-black bg-opacity-60 flex items-center justify-center z-50">
    <div class="bg-gray-800 border border-gray-700 rounded-xl p-6 w-full max-w-md mx-4">
      <h3 id="params-modal-title" class="text-lg font-semibold mb-4">Enter Parameters</h3>
      <div id="params-modal-body" class="space-y-3 mb-4"></div>
      <div class="flex justify-end gap-3">
        <button id="params-cancel" class="px-4 py-2 bg-gray-600 hover:bg-gray-500 text-white rounded-lg transition-colors">Cancel</button>
        <button id="params-submit" class="px-4 py-2 bg-indigo-600 hover:bg-indigo-700 text-white rounded-lg transition-colors">Execute</button>
      </div>
    </div>
  </div>

  <!-- Output Area -->
  <div id="output-container" class="hidden">
    <div class="flex items-center justify-between mb-2">
      <h3 class="text-sm font-semibold text-gray-300 uppercase tracking-wide">Execution Output</h3>
      <span id="status-badge" class="text-xs px-2 py-1 rounded-full bg-yellow-600 text-white">Running...</span>
    </div>
    <pre id="output" class="bg-gray-950 border border-gray-700 rounded-lg p-4 overflow-x-auto text-gray-300 whitespace-pre-wrap max-h-96 overflow-y-auto"></pre>
  </div>

</div>

<script>
(function() {{
  'use strict';

  const AGENT_ID = '{agent_id}';
  const actions = {actions_json};

  let currentExecutionId = null;
  let pollTimer = null;
  let pendingAction = null;

  // ── Button click handler ──────────────────────────────────────────

  document.querySelectorAll('.panel-btn:not([disabled])').forEach(btn => {{
    btn.addEventListener('click', () => {{
      const actionId = parseInt(btn.dataset.actionId);
      const actionType = btn.dataset.actionType;
      const action = actions.find(a => a.id === actionId);
      if (!action) return;

      if (actionType === 'prompt') {{
        executeAction(actionId, null);
      }} else if (actionType === 'script') {{
        // Check if the action has params
        if (action.params) {{
          let paramsDef;
          try {{
            paramsDef = typeof action.params === 'string'
              ? JSON.parse(action.params)
              : action.params;
          }} catch(e) {{
            paramsDef = [];
          }}

          if (paramsDef && paramsDef.length > 0) {{
            showParamsModal(actionId, action.label, paramsDef);
            return;
          }}
        }}
        executeAction(actionId, {{}});
      }}
    }});
  }});

  // ── Params modal ──────────────────────────────────────────────────

  function showParamsModal(actionId, label, paramsDef) {{
    pendingAction = {{ actionId, paramsDef }};
    document.getElementById('params-modal-title').textContent = label;
    const body = document.getElementById('params-modal-body');
    body.innerHTML = '';

    paramsDef.forEach(p => {{
      const div = document.createElement('div');
      div.innerHTML = `
        <label class="block text-sm text-gray-400 mb-1">${{_escapeHTML(p.label || p.name)}}</label>
        <input type="text" name="param_${{p.name}}" class="w-full bg-gray-700 border border-gray-600 rounded-lg px-3 py-2 text-white focus:outline-none focus:border-indigo-500" placeholder="${{_escapeHTML(p.description || '')}}">
      `;
      body.appendChild(div);
    }});

    document.getElementById('params-modal').classList.remove('hidden');
  }}

  document.getElementById('params-cancel').addEventListener('click', () => {{
    document.getElementById('params-modal').classList.add('hidden');
    pendingAction = null;
  }});

  document.getElementById('params-submit').addEventListener('click', () => {{
    if (!pendingAction) return;
    const params = {{}};
    pendingAction.paramsDef.forEach(p => {{
      const input = document.querySelector(`input[name="param_${{p.name}}"]`);
      if (input) params[p.name] = input.value;
    }});
    document.getElementById('params-modal').classList.add('hidden');
    executeAction(pendingAction.actionId, params);
    pendingAction = null;
  }});

  // ── Execute action ────────────────────────────────────────────────

  async function executeAction(actionId, params) {{
    // Disable buttons during execution
    document.querySelectorAll('.panel-btn').forEach(b => b.disabled = true);

    // Show output area
    const outputContainer = document.getElementById('output-container');
    const outputEl = document.getElementById('output');
    const statusBadge = document.getElementById('status-badge');

    outputContainer.classList.remove('hidden');
    outputEl.textContent = '';
    statusBadge.textContent = 'Running...';
    statusBadge.className = 'text-xs px-2 py-1 rounded-full bg-yellow-600 text-white';

    try {{
      const body = params !== null ? JSON.stringify({{ params: params }}) : JSON.stringify({{}});
      const resp = await fetch(`/plugin/panel/${{AGENT_ID}}/execute/${{actionId}}`, {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: body,
      }});

      const data = await resp.json();

      if (data.needs_params) {{
        // Server says params needed — but we should not get here since we check client-side
        outputEl.textContent = 'Error: Parameters required but not provided.';
        statusBadge.textContent = 'Error';
        statusBadge.className = 'text-xs px-2 py-1 rounded-full bg-red-600 text-white';
        document.querySelectorAll('.panel-btn').forEach(b => b.disabled = false);
        return;
      }}

      if (resp.status === 409) {{
        outputEl.textContent = data.error || 'A script is already running for this agent.';
        statusBadge.textContent = 'Busy';
        statusBadge.className = 'text-xs px-2 py-1 rounded-full bg-red-600 text-white';
        document.querySelectorAll('.panel-btn').forEach(b => b.disabled = false);
        return;
      }}

      if (data.status === 'prompt_sent') {{
        outputEl.textContent = '✓ Prompt sent to agent session.';
        statusBadge.textContent = 'Sent';
        statusBadge.className = 'text-xs px-2 py-1 rounded-full bg-emerald-600 text-white';
        document.querySelectorAll('.panel-btn').forEach(b => b.disabled = false);
        return;
      }}

      if (data.status === 'queued' && data.execution_id) {{
        currentExecutionId = data.execution_id;
        pollExecution(data.execution_id);
        return;
      }}

      // Unexpected response
      outputEl.textContent = JSON.stringify(data, null, 2);
      statusBadge.textContent = 'Done';
      statusBadge.className = 'text-xs px-2 py-1 rounded-full bg-gray-600 text-white';
      document.querySelectorAll('.panel-btn').forEach(b => b.disabled = false);
    }} catch (err) {{
      outputEl.textContent = 'Network error: ' + err.message;
      statusBadge.textContent = 'Error';
      statusBadge.className = 'text-xs px-2 py-1 rounded-full bg-red-600 text-white';
      document.querySelectorAll('.panel-btn').forEach(b => b.disabled = false);
    }}
  }}

  // ── Poll execution status ─────────────────────────────────────────

  async function pollExecution(executionId) {{
    const outputEl = document.getElementById('output');
    const statusBadge = document.getElementById('status-badge');

    if (pollTimer) clearInterval(pollTimer);

    let seenLines = 0;

    pollTimer = setInterval(async () => {{
      try {{
        const resp = await fetch(`/plugin/panel/${{AGENT_ID}}/execution/${{executionId}}/status`);
        if (!resp.ok) {{
          clearInterval(pollTimer);
          outputEl.textContent = 'Error polling execution status.';
          statusBadge.textContent = 'Error';
          statusBadge.className = 'text-xs px-2 py-1 rounded-full bg-red-600 text-white';
          document.querySelectorAll('.panel-btn').forEach(b => b.disabled = false);
          return;
        }}

        const data = await resp.json();

        // Append new lines
        if (data.output_lines && data.output_lines.length > seenLines) {{
          const newLines = data.output_lines.slice(seenLines);
          outputEl.textContent += newLines.join('');
          seenLines = data.output_lines.length;
          // Auto-scroll
          const container = outputEl.parentElement;
          if (container) container.scrollTop = container.scrollHeight;
        }}

        if (data.status === 'completed') {{
          clearInterval(pollTimer);
          statusBadge.textContent = `Completed (exit: ${{data.exit_code}})`;
          statusBadge.className = 'text-xs px-2 py-1 rounded-full bg-emerald-600 text-white';
          document.querySelectorAll('.panel-btn').forEach(b => b.disabled = false);
        }} else if (data.status === 'error') {{
          clearInterval(pollTimer);
          statusBadge.textContent = `Error (exit: ${{data.exit_code}})`;
          statusBadge.className = 'text-xs px-2 py-1 rounded-full bg-red-600 text-white';
          document.querySelectorAll('.panel-btn').forEach(b => b.disabled = false);
        }}
      }} catch (err) {{
        clearInterval(pollTimer);
        outputEl.textContent += '\\n[Polling error: ' + err.message + ']';
        statusBadge.textContent = 'Error';
        statusBadge.className = 'text-xs px-2 py-1 rounded-full bg-red-600 text-white';
        document.querySelectorAll('.panel-btn').forEach(b => b.disabled = false);
      }}
    }}, 500); // Poll every 500ms
  }}

  // ── Utility ───────────────────────────────────────────────────────

  function _escapeHTML(str) {{
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
  }}
}})();
</script>
</body>
</html>"""

    return html


def _escape_html(text: str) -> str:
    """Escape text for safe HTML embedding."""
    return (
        text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
            .replace("'", "&#39;")
    )


# ---------------------------------------------------------------------------
# Script execution (daemon thread)
# ---------------------------------------------------------------------------

def _run_script(agent_id: str, action: dict, user_params: dict,
                execution_id: str, run_as_user: str, lock: threading.Lock):
    """
    Execute a script action in a background daemon thread.

    Captures stdout/stderr line-by-line and stores in the in-memory buffer.
    Logs the result to SQLite upon completion.
    """
    try:
        content = action.get("content", "")

        # Substitute {{param}} placeholders
        for key, value in user_params.items():
            content = content.replace(f"{{{{{key}}}}}", str(value))

        max_timeout = _get_max_timeout()

        # Build the command — run via sudo -u <run_as_user>
        cmd = ["sudo", "-E", "-u", run_as_user, "bash", "-s"]

        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env={**os.environ, "HOME": f"/home/{run_as_user}"},
        )

        # Feed the script content
        proc.stdin.write(content)
        proc.stdin.close()

        # Read output line-by-line with timeout
        start_time = time.time()
        output_lines = []

        try:
            while True:
                elapsed = time.time() - start_time
                if elapsed >= max_timeout:
                    proc.kill()
                    output_lines.append(f"\n[Script terminated after {max_timeout}s timeout]\n")
                    break

                # Use a short timeout so we can check the overall timeout
                line = None
                try:
                    line = proc.stdout.readline()
                except Exception:
                    break

                if not line:
                    # Check if process has finished
                    if proc.poll() is not None:
                        break
                    # No output yet, wait a bit
                    time.sleep(0.05)
                    continue

                output_lines.append(line)

                # Update the in-memory buffer for polling
                with _buffers_lock:
                    buf = _execution_buffers.get(execution_id)
                    if buf:
                        buf["output_lines"].append(line)

        except Exception:
            pass

        # Wait for process to finish (with remaining timeout)
        try:
            remaining = max_timeout - (time.time() - start_time)
            if remaining > 0:
                proc.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            output_lines.append("\n[Script terminated after timeout]\n")

        exit_code = proc.returncode

        # Read any remaining output
        try:
            for line in proc.stdout:
                output_lines.append(line)
                with _buffers_lock:
                    buf = _execution_buffers.get(execution_id)
                    if buf:
                        buf["output_lines"].append(line)
        except Exception:
            pass

        # Update buffer status
        status = "completed" if exit_code == 0 else "error"
        with _buffers_lock:
            buf = _execution_buffers.get(execution_id)
            if buf:
                buf["status"] = status
                buf["exit_code"] = exit_code

        # Log to SQLite
        try:
            db = panel_db_factory(agent_id)
            db.log_execution(
                action_id=action["id"],
                action_label=action["label"],
                action_type="script",
                params_used=json.dumps(user_params) if user_params else None,
                result="".join(output_lines[-500:]),  # Last 500 lines
                status=status,
            )
        except Exception:
            pass  # Best-effort logging

    except Exception as e:
        with _buffers_lock:
            buf = _execution_buffers.get(execution_id)
            if buf:
                buf["status"] = "error"
                buf["output_lines"].append(f"\n[Execution error: {str(e)}]\n")
                buf["exit_code"] = -1

    finally:
        lock.release()


# ---------------------------------------------------------------------------
# Prompt sending
# ---------------------------------------------------------------------------

def _send_prompt_to_agent(agent_id: str, action: dict):
    """Send a prompt action to the agent's main session."""
    # This sends a user message to the agent's default session.
    # The message is processed as if it came from the web UI.
    try:
        from models.db import db
        from models.chat import chat_db

        # Get or create a default web session for this agent
        session_id = chat_db.get_or_create_session(
            agent_id=agent_id,
            external_user_id="panel",
            channel_id="web",
        )

        content = action.get("content", "")
        if content:
            db.add_chat_message(session_id, "user", content)
    except Exception:
        raise


def _parse_params(params):
    """Parse params field from action. Returns list of param defs or None."""
    if not params:
        return None
    if isinstance(params, str):
        try:
            parsed = json.loads(params)
        except (json.JSONDecodeError, TypeError):
            return None
        return parsed if isinstance(parsed, list) else None
    if isinstance(params, list):
        return params
    return None
