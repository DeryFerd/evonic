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
- Scripts run as the agent's run_as_user (via sudo), or as the system
  default user (the server's own process user) when run_as_user is unset
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

        from flask import make_response as flask_make_response
        response = flask_make_response(html, 200)
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

            execution_id, err = start_script_execution(agent_id, agent, action, user_params)
            if err == "busy":
                return jsonify({
                    "error": "A script is already running for this agent",
                    "status": "busy",
                }), 409

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
    """Build the panel UI as an HTML body fragment.

    Returned fragment is injected into the Panel tab of agent_detail.html via
    innerHTML (scripts are re-created by setPluginTabHTML so they execute).
    Must NOT be a full document and must NOT load external scripts.
    """

    actions_json = json.dumps(actions)
    agent_name = agent.get("name", agent_id)

    panel_css = """<style>
  .pnl-wrap { display:grid; grid-template-columns:minmax(240px,320px) 1fr; gap:1rem; padding:1rem; align-items:start; }
  .pnl-left { display:flex; flex-direction:column; gap:.75rem; min-width:0; }
  .pnl-title { font-size:1.05rem; font-weight:700; margin:0; }
  .pnl-title span { color:#818cf8; }
  .pnl-search-wrap { position:relative; }
  .pnl-search-wrap > svg { position:absolute; left:.625rem; top:50%; transform:translateY(-50%); width:1rem; height:1rem; color:#9ca3af; pointer-events:none; }
  .pnl-search { width:100%; box-sizing:border-box; padding:.5rem .75rem .5rem 2rem; border-radius:.5rem; border:1px solid #d1d5db; background:#fff; color:#111827; font-size:.8125rem; }
  .pnl-search:focus { outline:none; border-color:#6366f1; box-shadow:0 0 0 3px rgba(99,102,241,.15); }
  .pnl-btn-grid { display:grid; grid-template-columns:1fr 1fr; gap:.5rem; }
  .panel-btn { display:flex; align-items:center; gap:.5rem; color:#fff; font-weight:500; font-size:.8125rem; padding:.625rem .75rem; border-radius:.5rem; border:none; cursor:pointer; text-align:left; width:100%; box-sizing:border-box; transition:background-color .15s, transform .05s; }
  .panel-btn:active { transform:translateY(1px); }
  .panel-btn .pnl-dot { width:.5rem; height:.5rem; border-radius:9999px; flex:0 0 auto; background:rgba(255,255,255,.75); }
  .panel-btn .pnl-label { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .btn-script { background-color:#4f46e5; }
  .btn-script:hover { background-color:#4338ca; }
  .btn-prompt { background-color:#059669; }
  .btn-prompt:hover { background-color:#047857; }
  .btn-disabled { opacity:.5; cursor:not-allowed; }
  .pnl-hidden { display:none !important; }
  .pnl-empty { color:#6b7280; font-size:.8125rem; padding:1rem; text-align:center; border:1px dashed #d1d5db; border-radius:.5rem; }
  .pnl-btn-grid .pnl-empty { grid-column:1 / -1; }
  .pnl-term { display:flex; flex-direction:column; background:#0f172a; border:1px solid #1e293b; border-radius:.75rem; overflow:hidden; height:72vh; min-height:360px; }
  .pnl-term-head { display:flex; align-items:center; justify-content:space-between; gap:.5rem; padding:.5rem .75rem; background:#1e293b; border-bottom:1px solid #334155; }
  .pnl-term-head-left { display:flex; align-items:center; gap:.5rem; }
  .pnl-term-dots { display:flex; gap:.375rem; }
  .pnl-term-dots i { width:.6rem; height:.6rem; border-radius:9999px; display:block; }
  .pnl-term-title { color:#94a3b8; font-size:.72rem; font-weight:700; text-transform:uppercase; letter-spacing:.06em; }
  .pnl-badge { font-size:.68rem; padding:.125rem .5rem; border-radius:9999px; font-weight:600; color:#fff; white-space:nowrap; }
  .pnl-badge.idle { background:#475569; }
  .pnl-badge.running { background:#ca8a04; }
  .pnl-badge.done { background:#059669; }
  .pnl-badge.error { background:#dc2626; }
  .pnl-clear { font-size:.68rem; color:#94a3b8; background:transparent; border:1px solid #334155; border-radius:.375rem; padding:.2rem .5rem; cursor:pointer; }
  .pnl-clear:hover { color:#e2e8f0; border-color:#475569; }
  .pnl-term-body { flex:1; overflow-y:auto; padding:.75rem 1rem; margin:0; font-family:'Fira Code','Cascadia Code','Consolas',Menlo,Monaco,monospace; font-size:12.5px; line-height:1.5; color:#e2e8f0; white-space:pre-wrap; word-break:break-word; }
  .pnl-cmd { color:#818cf8; font-weight:600; }
  .pnl-muted { color:#64748b; }
  .pnl-ok { color:#34d399; }
  .pnl-err { color:#f87171; }
  .pnl-modal { position:fixed; inset:0; background:rgba(0,0,0,.6); display:flex; align-items:center; justify-content:center; z-index:50; padding:1rem; }
  .pnl-modal-card { background:#1e293b; border:1px solid #334155; border-radius:.75rem; padding:1.25rem; width:100%; max-width:28rem; box-sizing:border-box; }
  .pnl-modal-title { color:#f1f5f9; font-size:1rem; font-weight:600; margin:0 0 1rem; }
  .pnl-field { margin-bottom:.75rem; }
  .pnl-field label { display:block; color:#94a3b8; font-size:.8125rem; margin-bottom:.25rem; }
  .pnl-field input { width:100%; box-sizing:border-box; background:#0f172a; border:1px solid #334155; border-radius:.5rem; padding:.5rem .75rem; color:#f1f5f9; font-size:.8125rem; }
  .pnl-field input:focus { outline:none; border-color:#6366f1; }
  .pnl-modal-actions { display:flex; justify-content:flex-end; gap:.5rem; margin-top:.5rem; }
  .pnl-btn-sec, .pnl-btn-pri { padding:.5rem 1rem; border-radius:.5rem; font-size:.8125rem; font-weight:500; cursor:pointer; border:none; color:#fff; }
  .pnl-btn-sec { background:#475569; }
  .pnl-btn-sec:hover { background:#334155; }
  .pnl-btn-pri { background:#4f46e5; }
  .pnl-btn-pri:hover { background:#4338ca; }
  .dark .pnl-search { background:#374151; border-color:#4b5563; color:#f3f4f6; }
  .dark .pnl-empty { border-color:#4b5563; color:#9ca3af; }
  @media (max-width:1023px) {
    .pnl-wrap { grid-template-columns:1fr; }
    .pnl-term { height:60vh; }
  }
  @media (max-width:640px) {
    .pnl-btn-grid { grid-template-columns:1fr; }
  }
</style>
"""

    html = panel_css + f"""<div class="pnl-wrap">
  <div class="pnl-left">
    <h2 class="pnl-title">Panel — <span>{_escape_html(agent_name)}</span></h2>
    <div class="pnl-search-wrap">
      <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" d="m21 21-4.3-4.3m1.8-4.7a6.5 6.5 0 1 1-13 0 6.5 6.5 0 0 1 13 0Z"/></svg>
      <input id="pnl-search" class="pnl-search" type="search" placeholder="Filter buttons..." autocomplete="off">
    </div>
    <div id="pnl-btn-grid" class="pnl-btn-grid">
"""

    for action in actions:
        label = _escape_html(action["label"])
        label_lower = _escape_html((action["label"] or "").lower())
        aid = action["id"]
        atype = action["action_type"]
        enabled = action.get("enabled", True)

        color_class = "btn-script" if atype == "script" else "btn-prompt"

        disabled_attr = ""
        disabled_class = ""
        if not enabled:
            disabled_attr = "disabled"
            disabled_class = " btn-disabled"

        html += f"""      <button class="panel-btn {color_class}{disabled_class}" data-action-id="{aid}" data-action-type="{atype}" data-label="{label}" data-search="{label_lower}" {disabled_attr}><span class="pnl-dot"></span><span class="pnl-label">{label}</span></button>
"""

    if not actions:
        html += '      <div class="pnl-empty">No actions yet. This agent has not added any panel buttons.</div>\n'

    html += """    </div>
    <div id="pnl-nomatch" class="pnl-empty pnl-hidden">No buttons match your search.</div>
  </div>

  <div class="pnl-term">
    <div class="pnl-term-head">
      <div class="pnl-term-head-left">
        <span class="pnl-term-dots"><i style="background:#ef4444"></i><i style="background:#eab308"></i><i style="background:#22c55e"></i></span>
        <span class="pnl-term-title">Output</span>
        <span id="pnl-badge" class="pnl-badge idle">idle</span>
      </div>
      <button id="pnl-clear" class="pnl-clear" type="button">Clear</button>
    </div>
    <div id="pnl-term-body" class="pnl-term-body"><span class="pnl-muted"># Ready — click a button to run it.</span></div>
  </div>
</div>

<div id="pnl-modal" class="pnl-modal pnl-hidden">
  <div class="pnl-modal-card">
    <h3 id="pnl-modal-title" class="pnl-modal-title">Enter Parameters</h3>
    <div id="pnl-modal-body"></div>
    <div class="pnl-modal-actions">
      <button id="pnl-cancel" class="pnl-btn-sec" type="button">Cancel</button>
      <button id="pnl-submit" class="pnl-btn-pri" type="button">Execute</button>
    </div>
  </div>
</div>
"""

    panel_script = """<script>
(function() {
  'use strict';

  const AGENT_ID = '__AGENT_ID__';
  const actions = __ACTIONS_JSON__;

  let currentExecutionId = null;
  let pollTimer = null;
  let pendingAction = null;
  let termFresh = true;

  const termBody = document.getElementById('pnl-term-body');
  const badge = document.getElementById('pnl-badge');

  function termAppend(text, cls) {
    if (termFresh) { termBody.innerHTML = ''; termFresh = false; }
    const span = document.createElement('span');
    if (cls) span.className = cls;
    span.textContent = text;
    termBody.appendChild(span);
    termBody.scrollTop = termBody.scrollHeight;
  }

  function setBadge(state, text) {
    badge.className = 'pnl-badge ' + state;
    badge.textContent = text;
  }

  function setButtonsDisabled(state) {
    document.querySelectorAll('.panel-btn').forEach(function(b) {
      if (!b.classList.contains('btn-disabled')) b.disabled = state;
    });
  }

  // ── Search filter ─────────────────────────────────────────────────
  const searchInput = document.getElementById('pnl-search');
  const btnGrid = document.getElementById('pnl-btn-grid');
  const noMatch = document.getElementById('pnl-nomatch');
  if (searchInput) {
    searchInput.addEventListener('input', function() {
      const q = searchInput.value.trim().toLowerCase();
      const btns = btnGrid.querySelectorAll('.panel-btn');
      let visible = 0;
      btns.forEach(function(btn) {
        const match = !q || (btn.dataset.search || '').indexOf(q) !== -1;
        btn.classList.toggle('pnl-hidden', !match);
        if (match) visible++;
      });
      noMatch.classList.toggle('pnl-hidden', !(btns.length > 0 && visible === 0));
    });
  }

  // ── Clear terminal ────────────────────────────────────────────────
  const clearBtn = document.getElementById('pnl-clear');
  if (clearBtn) {
    clearBtn.addEventListener('click', function() {
      termBody.innerHTML = '<span class="pnl-muted"># Ready — click a button to run it.</span>';
      termFresh = true;
      setBadge('idle', 'idle');
    });
  }

  // ── Button click handler ──────────────────────────────────────────
  document.querySelectorAll('.panel-btn:not([disabled])').forEach(function(btn) {
    btn.addEventListener('click', function() {
      const actionId = parseInt(btn.dataset.actionId);
      const actionType = btn.dataset.actionType;
      const action = actions.find(function(a) { return a.id === actionId; });
      if (!action) return;

      if (actionType === 'prompt') {
        executeAction(actionId, null);
        return;
      }

      let paramsDef = [];
      if (action.params) {
        try {
          paramsDef = typeof action.params === 'string' ? JSON.parse(action.params) : action.params;
        } catch (e) { paramsDef = []; }
      }
      if (paramsDef && paramsDef.length > 0) {
        showParamsModal(actionId, action.label, paramsDef);
        return;
      }
      executeAction(actionId, {});
    });
  });

  // ── Params modal ──────────────────────────────────────────────────
  function showParamsModal(actionId, label, paramsDef) {
    pendingAction = { actionId: actionId, paramsDef: paramsDef };
    document.getElementById('pnl-modal-title').textContent = label;
    const body = document.getElementById('pnl-modal-body');
    body.innerHTML = '';
    paramsDef.forEach(function(p) {
      const field = document.createElement('div');
      field.className = 'pnl-field';
      const lab = document.createElement('label');
      lab.textContent = p.label || p.name;
      const inp = document.createElement('input');
      inp.type = p.type === 'password' ? 'password' : (p.type === 'number' ? 'number' : 'text');
      inp.name = 'param_' + p.name;
      if (p.default) inp.value = p.default;
      field.appendChild(lab);
      field.appendChild(inp);
      body.appendChild(field);
    });
    document.getElementById('pnl-modal').classList.remove('pnl-hidden');
  }

  document.getElementById('pnl-cancel').addEventListener('click', function() {
    document.getElementById('pnl-modal').classList.add('pnl-hidden');
    pendingAction = null;
  });

  document.getElementById('pnl-submit').addEventListener('click', function() {
    if (!pendingAction) return;
    const params = {};
    pendingAction.paramsDef.forEach(function(p) {
      const input = document.querySelector('input[name="param_' + p.name + '"]');
      if (input) params[p.name] = input.value;
    });
    document.getElementById('pnl-modal').classList.add('pnl-hidden');
    executeAction(pendingAction.actionId, params);
    pendingAction = null;
  });

  // ── Execute action ────────────────────────────────────────────────
  async function executeAction(actionId, params) {
    const action = actions.find(function(a) { return a.id === actionId; });
    const label = action ? action.label : ('action ' + actionId);

    setButtonsDisabled(true);
    termAppend('\\n$ ' + label + '\\n', 'pnl-cmd');
    setBadge('running', 'running');

    try {
      const body = params !== null ? JSON.stringify({ params: params }) : JSON.stringify({});
      const resp = await fetch('/plugin/panel/' + AGENT_ID + '/execute/' + actionId, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: body
      });
      const data = await resp.json();

      if (data.needs_params) {
        termAppend('Error: parameters required but not provided.\\n', 'pnl-err');
        setBadge('error', 'error');
        setButtonsDisabled(false);
        return;
      }
      if (resp.status === 409) {
        termAppend((data.error || 'A script is already running for this agent.') + '\\n', 'pnl-err');
        setBadge('error', 'busy');
        setButtonsDisabled(false);
        return;
      }
      if (data.status === 'prompt_sent') {
        termAppend('✓ Prompt sent to agent session.\\n', 'pnl-ok');
        setBadge('done', 'sent');
        setButtonsDisabled(false);
        return;
      }
      if (data.status === 'queued' && data.execution_id) {
        currentExecutionId = data.execution_id;
        pollExecution(data.execution_id);
        return;
      }
      termAppend(JSON.stringify(data, null, 2) + '\\n', 'pnl-muted');
      setBadge('done', 'done');
      setButtonsDisabled(false);
    } catch (err) {
      termAppend('Network error: ' + err.message + '\\n', 'pnl-err');
      setBadge('error', 'error');
      setButtonsDisabled(false);
    }
  }

  // ── Poll execution status ─────────────────────────────────────────
  async function pollExecution(executionId) {
    if (pollTimer) clearInterval(pollTimer);
    let seenLines = 0;

    pollTimer = setInterval(async function() {
      try {
        const resp = await fetch('/plugin/panel/' + AGENT_ID + '/execution/' + executionId + '/status');
        if (!resp.ok) {
          clearInterval(pollTimer);
          termAppend('Error polling execution status.\\n', 'pnl-err');
          setBadge('error', 'error');
          setButtonsDisabled(false);
          return;
        }
        const data = await resp.json();

        if (data.output_lines && data.output_lines.length > seenLines) {
          termAppend(data.output_lines.slice(seenLines).join(''));
          seenLines = data.output_lines.length;
        }

        if (data.status === 'completed') {
          clearInterval(pollTimer);
          termAppend('\\n[completed · exit ' + data.exit_code + ']\\n', 'pnl-muted');
          setBadge('done', 'completed (' + data.exit_code + ')');
          setButtonsDisabled(false);
        } else if (data.status === 'error') {
          clearInterval(pollTimer);
          termAppend('\\n[error · exit ' + data.exit_code + ']\\n', 'pnl-err');
          setBadge('error', 'error (' + data.exit_code + ')');
          setButtonsDisabled(false);
        }
      } catch (err) {
        clearInterval(pollTimer);
        termAppend('\\n[polling error: ' + err.message + ']\\n', 'pnl-err');
        setBadge('error', 'error');
        setButtonsDisabled(false);
      }
    }, 500);
  }
})();
</script>"""

    html += panel_script.replace('__AGENT_ID__', agent_id).replace('__ACTIONS_JSON__', actions_json)

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

def start_script_execution(agent_id: str, agent: dict, action: dict,
                           user_params: dict):
    """Start a script action in a background daemon thread.

    Returns (execution_id, err). err is None on success, or:
    "busy" — a script is already running for this agent.

    If the agent has no run_as_user configured, the script runs as the system
    default user (the server's own process user), without sudo.
    """
    run_as_user = (agent.get("run_as_user") or "").strip()

    # Mutual exclusion: per-agent lock, non-blocking
    lock = _get_agent_lock(agent_id)
    if not lock.acquire(blocking=False):
        return None, "busy"

    execution_id = str(uuid.uuid4())
    with _buffers_lock:
        _execution_buffers[execution_id] = {
            "status": "running",
            "output_lines": [],
            "exit_code": None,
            "started_at": _now(),
        }

    thread = threading.Thread(
        target=_run_script,
        args=(agent_id, action, user_params, execution_id, run_as_user, lock),
        daemon=True,
    )
    thread.start()

    return execution_id, None


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

        # Build the command. With a run_as_user, drop privileges via sudo.
        # Without one, fall back to the system default user (the server's own
        # process user) by running bash directly — no sudo.
        if run_as_user:
            cmd = ["sudo", "-E", "-u", run_as_user, "bash", "-s"]
            env = {**os.environ, "HOME": f"/home/{run_as_user}"}
        else:
            cmd = ["bash", "-s"]
            env = {**os.environ}

        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
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

def _send_prompt_to_agent(agent_id: str, action: dict, session_id: str = None,
                          params: dict = None):
    """Deliver a prompt action to the agent and trigger a turn (async)."""
    content = action.get("content", "")
    for key, value in (params or {}).items():
        content = content.replace(f"{{{{{key}}}}}", str(value))
    if not content:
        return

    def _deliver():
        from backend.agent_runtime.notifier import notify_agent
        kwargs = dict(agent_id=agent_id, tag="Panel", message=content,
                      dedup=False, trigger_llm=True)
        if session_id:
            kwargs["session_id"] = session_id
        else:
            kwargs["external_user_id"] = "panel"
            kwargs["channel_id"] = None
        notify_agent(**kwargs)

    # notify_agent(trigger_llm=True) blocks until the LLM turn completes —
    # deliver from a daemon thread so callers (HTTP route, slash handler)
    # return immediately.
    threading.Thread(target=_deliver, daemon=True).start()


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


# ---------------------------------------------------------------------------
# Slash command: /panel
# ---------------------------------------------------------------------------
# Registered here (not in a handler.py) so the slash handler shares this
# module's in-memory execution state (_execution_buffers, per-agent locks)
# with the Flask routes — plugin_lifecycle loads this file under a synthetic
# package name, so importing plugins.panel.routes elsewhere would create a
# second module instance with separate state.

def _slug(s: str) -> str:
    return re.sub(r"\s+", "-", (s or "").strip().lower())


def _panel_slash_handler(session_id, agent_id, external_user_id, channel_id, args):
    """Handle /panel (list actions) and /panel:<action> (execute) from chat."""
    db = panel_db_factory(agent_id)
    actions = [a for a in db.list_actions() if a.get("enabled")]

    sub = (args or "").strip()
    if not sub:
        if not actions:
            return "No panel actions configured for this agent."
        lines = ["**Panel actions:**"]
        for a in actions:
            lines.append(f"- `/panel:{_slug(a['label'])}` — {a['label']} ({a['action_type']})")
        return "\n".join(lines)

    target = _slug(sub.split()[0])
    action = next((a for a in actions if _slug(a["label"]) == target), None)
    if not action:
        return f"No panel action matching '{target}'. Use `/panel` to list actions."

    if action["action_type"] == "prompt":
        _send_prompt_to_agent(agent_id, action, session_id=session_id)
        db.log_execution(action_id=action["id"], action_label=action["label"],
                         action_type="prompt", params_used=None,
                         result="Prompt sent via /panel", status="success")
        return f"Panel action **{action['label']}** sent to agent."

    # Script action
    if _parse_params(action.get("params")):
        return (f"Action **{action['label']}** requires parameters — "
                f"run it from the Panel tab instead.")

    from models.db import db as models_db
    agent = models_db.get_agent(agent_id)
    if not agent:
        return f"Agent '{agent_id}' not found."

    execution_id, err = start_script_execution(agent_id, agent, action, {})
    if err == "busy":
        return (f"Cannot run **{action['label']}**: a script is already "
                "running for this agent.")

    # Bounded wait so short scripts return their output right in chat;
    # longer scripts keep running in the background thread.
    deadline = time.time() + min(_get_max_timeout(), 30)
    while time.time() < deadline:
        with _buffers_lock:
            buf = dict(_execution_buffers.get(execution_id) or {})
        if buf.get("status") in ("completed", "error"):
            output = "".join(buf.get("output_lines", []))[-2000:]
            return (f"**{action['label']}** finished (exit {buf.get('exit_code')}):\n"
                    f"```\n{output or '(no output)'}\n```")
        time.sleep(0.5)
    return (f"**{action['label']}** is still running in the background — "
            "the result will be recorded in the panel execution log.")


try:
    from backend.slash_commands import command_registry
    command_registry.register(
        "panel",
        _panel_slash_handler,
        "Execute panel actions — /panel to list, /panel:<action> to run",
    )
except Exception as _e:
    from backend.logging_config import get_logger
    get_logger(__name__).error("Failed to register /panel slash command: %s", _e)
