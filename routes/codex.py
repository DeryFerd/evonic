"""Codex OAuth routes — PKCE flow, status, disconnect."""

from flask import Blueprint, jsonify, request

from models.db import db
from backend.provider.oauth_codex import (
    start_auth_flow,
    check_auth_status,
    exchange_code_for_tokens,
    store_tokens,
    get_valid_token,
    clear_tokens,
)

codex_bp = Blueprint("codex", __name__)


def _find_codex_provider():
    """Find the first provider with auth_type='oauth' or api_format='codex'."""
    for p in db.get_providers():
        if p.get("auth_type") == "oauth" or p.get("api_format") == "codex":
            return p
    return None


@codex_bp.route("/api/codex/status", methods=["GET"])
def codex_status():
    provider = _find_codex_provider()
    if not provider:
        return jsonify({"connected": False, "provider_id": None})

    token = get_valid_token(db, provider["id"])
    connected = token is not None
    expires_at = provider.get("token_expires_at", 0) or 0

    return jsonify({
        "connected": connected,
        "provider_id": provider["id"],
        "expires_at": expires_at,
    })


@codex_bp.route("/api/codex/connect", methods=["POST"])
def codex_connect():
    """Start the OAuth PKCE flow — returns an auth URL to open in the browser."""
    provider = _find_codex_provider()
    if not provider:
        return jsonify({"error": "No Codex provider configured. Create one first."}), 404

    result = start_auth_flow(provider["id"])
    return jsonify({
        "success": True,
        "auth_url": result["auth_url"],
    })


@codex_bp.route("/api/codex/poll", methods=["POST"])
def codex_poll():
    """Poll to check if the user has completed OAuth authorization."""
    provider = _find_codex_provider()
    if not provider:
        return jsonify({"status": "error", "error": "No Codex provider"}), 404

    pid = provider["id"]
    status = check_auth_status(pid)

    if status["status"] == "code_received":
        tokens = exchange_code_for_tokens(pid)
        if "error" in tokens:
            return jsonify({"status": "error", "error": tokens["error"]})

        store_tokens(db, pid, tokens)
        return jsonify({"status": "complete"})

    if status["status"] == "error":
        return jsonify({"status": "error", "error": status.get("error", "Unknown error")})

    if status["status"] == "expired":
        return jsonify({"status": "expired", "error": "Authorization timed out. Please try again."})

    if status["status"] == "no_pending":
        return jsonify({"status": "error", "error": "No pending authorization. Start the flow again."})

    return jsonify({"status": "pending"})


@codex_bp.route("/api/codex/disconnect", methods=["POST"])
def codex_disconnect():
    provider = _find_codex_provider()
    if not provider:
        return jsonify({"success": False, "error": "No Codex provider found"}), 404

    clear_tokens(db, provider["id"])
    return jsonify({"success": True})
