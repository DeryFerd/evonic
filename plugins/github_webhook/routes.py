"""
GitHub Webhook Plugin — Flask Route Handlers

Endpoint:
  POST /webhook/github_webhook  — receives GitHub webhooks
"""

import hashlib
import hmac
import json
import logging
import os
import re
import threading

from flask import Blueprint, jsonify, request

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
_logger = logging.getLogger("github_webhook")
PLUGIN_ID = "github_webhook"


def _plugin_log(level: str, message: str):
    """Log a message to the plugin-scoped log (visible in plugin detail > Logs tab)."""
    try:
        from backend.plugin_manager import plugin_manager
        plugin_manager.add_log(PLUGIN_ID, level, message)
    except Exception:
        pass


def _log(level: str, message: str):
    """Log to both the Python logger and the plugin-scoped log."""
    getattr(_logger, level, _logger.info)(message)
    _plugin_log(level, message)


# Event handlers map event type -> (agent_key, prompt_key, extract_fn, allowed_actions, filter_key)
EVENT_HANDLERS = {
    "release": ("RELEASE_AGENT_ID", "RELEASE_PROMPT", "_extract_release", ("published",), "RELEASE_FILTERS"),
    "pull_request": ("PR_AGENT_ID", "PR_PROMPT", "_extract_pr", ("opened", "reopened", "closed", "edited"), "PR_FILTERS"),
    "issues": ("ISSUES_AGENT_ID", "ISSUES_PROMPT", "_extract_issues", ("opened", "reopened", "closed", "edited"), "ISSUES_FILTERS"),
    "push": ("PUSH_AGENT_ID", "PUSH_PROMPT", "_extract_push", None, "PUSH_FILTERS"),
    "workflow_run": ("WORKFLOW_RUN_AGENT_ID", "WORKFLOW_RUN_PROMPT", "_extract_workflow_run", None, "WORKFLOW_RUN_FILTERS"),
    "check_suite": ("CHECK_SUITE_AGENT_ID", "CHECK_SUITE_PROMPT", "_extract_check_suite", None, "CHECK_SUITE_FILTERS"),
    "status": ("STATUS_AGENT_ID", "STATUS_PROMPT", "_extract_status", None, "STATUS_FILTERS"),
}


def _get_config(key):
    """Get plugin config variable."""
    from backend.plugin_manager import plugin_manager
    cfg = plugin_manager.get_plugin_config("github_webhook")
    return cfg.get(key, "")


def _verify_signature(payload_body: bytes) -> bool:
    """Verify GitHub HMAC-SHA256 signature.

    Returns True only if WEBHOOK_SECRET is configured AND signature matches.
    NEVER accepts unauthenticated requests.
    """
    secret = _get_config("WEBHOOK_SECRET")
    if not secret:
        _log("error", "WEBHOOK_SECRET is not configured — rejecting webhook")
        return False

    signature_header = request.headers.get("X-Hub-Signature-256", "")
    if not signature_header.startswith("sha256="):
        return False

    expected = signature_header[7:]  # strip "sha256="
    computed = hmac.new(
        secret.encode("utf-8"),
        payload_body,
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(computed, expected)


def _render_template(template: str, data: dict) -> str:
    """Render a template string with {{variable}} placeholders."""
    result = template
    for key, value in data.items():
        placeholder = "{{" + key + "}}"
        result = result.replace(placeholder, str(value) if value else "")
    return result


# ==================== Filter Helpers ====================

def _resolve_field(data: dict, field_path: str):
    """Resolve a dot-notation path to a value in the JSON payload.

    e.g. "pull_request.state" -> data["pull_request"]["state"]
    Returns None if any part of the path is missing.
    """
    parts = field_path.strip().split(".")
    current = data
    for part in parts:
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return None
    return current


def _evaluate_filters(filters_json_str: str, data: dict) -> bool:
    """Evaluate JSON filter conditions against the webhook payload.

    filters_json_str: JSON array of filter objects, e.g.
        [{"field": "action", "match": "equals", "value": "opened"}]

    Returns True if all filters pass (or no filters defined).
    Malformed JSON returns True (skip gracefully, preserve current behavior).
    """
    if not filters_json_str or not filters_json_str.strip():
        return True

    try:
        filters = json.loads(filters_json_str)
    except (json.JSONDecodeError, TypeError):
        _log("warn", "Malformed filter JSON — skipping filter evaluation")
        return True

    if not isinstance(filters, list) or not filters:
        return True

    for f in filters:
        if not isinstance(f, dict):
            _log("warn", "Invalid filter entry (not an object) — skipping filter evaluation")
            return True

        field = f.get("field", "")
        match_type = f.get("match", "equals")
        match_value = f.get("value", "")

        resolved = _resolve_field(data, field)
        resolved_str = str(resolved) if resolved is not None else ""
        _log("info", "  filter: %s → '%s'  (match=%s, want='%s')" % (field, resolved_str, match_type, match_value))

        if match_type == "equals":
            if resolved_str != str(match_value):
                _log("info", "  filter FAIL: %s='%s' != '%s'" % (field, resolved_str, match_value))
                return False
        elif match_type == "regex":
            try:
                if not re.search(str(match_value), resolved_str):
                    _log("info", "  filter FAIL (regex): '%s' !~ /%s/" % (resolved_str, match_value))
                    return False
            except re.error as e:
                _log("warn", "  filter SKIP: invalid regex '%s': %s" % (match_value, e))
                return True
        else:
            _log("warn", "  filter SKIP: unknown match type '%s'" % match_type)
            return True

    _log("info", "  all %d filter(s) passed" % len(filters))
    return True


# ==================== Extractors ====================

def _extract_release(data: dict) -> dict:
    """Extract template variables from a release event."""
    release = data.get("release", {})
    repo = data.get("repository", {})
    return {
        "tag_name": release.get("tag_name", ""),
        "name": release.get("name", ""),
        "body": release.get("body", ""),
        "html_url": release.get("html_url", ""),
        "repository": repo.get("full_name", ""),
        "action": data.get("action", ""),
    }


def _extract_pr(data: dict) -> dict:
    """Extract template variables from a pull_request event."""
    pr = data.get("pull_request", {})
    repo = data.get("repository", {})
    return {
        "title": pr.get("title", ""),
        "body": pr.get("body", ""),
        "html_url": pr.get("html_url", ""),
        "state": pr.get("state", ""),
        "repository": repo.get("full_name", ""),
        "action": data.get("action", ""),
    }


def _extract_issues(data: dict) -> dict:
    """Extract template variables from an issues event."""
    issue = data.get("issue", {})
    repo = data.get("repository", {})
    return {
        "title": issue.get("title", ""),
        "body": issue.get("body", ""),
        "html_url": issue.get("html_url", ""),
        "state": issue.get("state", ""),
        "repository": repo.get("full_name", ""),
        "action": data.get("action", ""),
    }


def _extract_push(data: dict) -> dict:
    """Extract template variables from a push event."""
    repo = data.get("repository", {})
    commits = data.get("commits", [])
    return {
        "ref": data.get("ref", ""),
        "commits_count": str(len(commits)),
        "repository": repo.get("full_name", ""),
        "compare": data.get("compare_url", ""),
        "action": "push",
    }


def _extract_workflow_run(data: dict) -> dict:
    """Extract template variables from a workflow_run event."""
    run = data.get("workflow_run", {})
    repo = data.get("repository", {})
    return {
        "workflow_name": run.get("name", ""),
        "status": run.get("status", ""),
        "conclusion": run.get("conclusion", ""),
        "head_branch": run.get("head_branch", ""),
        "head_sha": run.get("head_sha", ""),
        "html_url": run.get("html_url", ""),
        "repository": repo.get("full_name", ""),
    }


def _extract_check_suite(data: dict) -> dict:
    """Extract template variables from a check_suite event."""
    cs = data.get("check_suite", {})
    repo = data.get("repository", {})
    app = cs.get("app", {})
    return {
        "app_name": app.get("name", ""),
        "status": cs.get("status", ""),
        "conclusion": cs.get("conclusion", ""),
        "head_branch": cs.get("head_branch", ""),
        "head_sha": cs.get("head_sha", ""),
        "html_url": cs.get("html_url", ""),
        "repository": repo.get("full_name", ""),
    }


def _extract_status(data: dict) -> dict:
    """Extract template variables from a status event."""
    repo = data.get("repository", {})
    return {
        "sha": data.get("sha", ""),
        "state": data.get("state", ""),
        "context": data.get("context", ""),
        "description": data.get("description", ""),
        "target_url": data.get("target_url", ""),
        "repository": repo.get("full_name", ""),
    }


# ==================== Dispatch ====================

def _handle_event(event: str, data: dict):
    """Dispatch an event to the appropriate handler."""
    handler = EVENT_HANDLERS.get(event)
    if handler is None:
        _log("info", "No handler for event type: %s" % event)
        return

    agent_key, prompt_key, extractor_name, allowed_actions, filter_key = handler

    # Check action filter (if applicable)
    action = data.get("action", "")
    if allowed_actions is not None:
        if not action or action not in allowed_actions:
            _log("info", "Event %s action=%s — ignoring (allowed: %s)" % (event, action or "<none>", allowed_actions))
            return

    # Get config
    agent_id = _get_config(agent_key)
    if not agent_id:
        #_log("info", "Event %s — %s not configured, skipping" % (event, agent_key))
        return

    # Evaluate custom filters (if configured)
    filters_config = _get_config(filter_key)
    if filters_config and filters_config.strip():
        _log("info", "Event %s — checking filters [%s]: %s" % (event, filter_key, filters_config))
        if not _evaluate_filters(filters_config, data):
            _log("info", "Event %s — filters blocked notification to agent=%s" % (event, agent_id))
            return
    else:
        _log("info", "Event %s — no filters configured for %s" % (event, filter_key))

    prompt_template = _get_config(prompt_key)
    if not prompt_template:
        _log("warn", "Event %s — %s not configured" % (event, prompt_key))
        return

    # Extract variables
    extractor = globals().get(extractor_name)
    if extractor is None:
        _log("error", "Extractor %s not found" % extractor_name)
        return

    template_vars = extractor(data)
    rendered = _render_template(prompt_template, template_vars)

    _log("info", "Notifying agent=%s about %s event" % (agent_id, event))

    # Fire-and-forget in background thread
    def _notify():
        try:
            from backend.agent_runtime import agent_runtime
            agent_runtime.handle_message(
                agent_id=agent_id,
                external_user_id="__webhook__github",
                message=rendered,
                channel_id=None,
            )
            _log("info", "Agent %s notified for %s event" % (agent_id, event))
        except Exception as e:
            _log("error", "Failed to notify agent %s: %s" % (agent_id, e))

    threading.Thread(target=_notify, daemon=True).start()


# ==================== Blueprint ====================

def create_blueprint():
    """Create the Flask Blueprint for this plugin."""
    bp = Blueprint(
        "github_webhook_plugin",
        __name__,
        url_prefix="/webhook/github_webhook",
    )

    @bp.route("/", methods=["POST"])
    def webhook():
        """Main webhook endpoint — verifies HMAC and routes by event type."""
        # Verify signature
        payload_body = request.get_data()
        if not _verify_signature(payload_body):
            _log("warn", "Invalid signature from %s" % request.remote_addr)
            return jsonify({"error": "Invalid signature"}), 403

        # Parse event type
        event = request.headers.get("X-GitHub-Event", "")
        if not event:
            return jsonify({"error": "Missing X-GitHub-Event header"}), 400

        # Parse payload
        try:
            data = json.loads(payload_body)
        except json.JSONDecodeError:
            return jsonify({"error": "Invalid JSON payload"}), 400

        # Route by event type
        if event == "ping":
            _log("info", "Ping received from GitHub")
            return jsonify({"msg": "pong"})

        _log("info", "Received %s event from GitHub" % event)
        _handle_event(event, data)
        return jsonify({"msg": "ok", "event": event})

    return bp
