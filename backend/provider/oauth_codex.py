"""OAuth 2.0 Authorization Code + PKCE flow for OpenAI Codex subscription.

Uses the standard OIDC endpoints discovered from auth.openai.com.
A temporary HTTP server on port 1455 receives the redirect callback.
Token persistence via the providers table.
"""

import base64
import hashlib
import json as _json
import logging
import secrets
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Dict, Optional
from urllib.parse import urlencode, urlparse, parse_qs

import requests

_log = logging.getLogger(__name__)

# Endpoints from Codex CLI source (codex-rs/login/src/auth/)
AUTH_ENDPOINT = "https://auth.openai.com/oauth/authorize"
TOKEN_ENDPOINT = "https://auth.openai.com/oauth/token"
CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"

CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
REDIRECT_URI = "http://localhost:1455/auth/callback"
SCOPES = "openid profile email offline_access api.connectors.read api.connectors.invoke"

# In-memory state for pending auth flows
_pending_auth: Dict[str, Dict] = {}


def _generate_pkce():
    """Generate PKCE code_verifier and code_challenge (S256)."""
    code_verifier = secrets.token_urlsafe(64)[:128]
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return code_verifier, code_challenge


def start_auth_flow(provider_id: str) -> Dict:
    """Start the OAuth PKCE flow.

    Returns dict with auth_url to open in the user's browser.
    Starts a temporary callback listener on port 1455 in a daemon thread.
    """
    code_verifier, code_challenge = _generate_pkce()
    state = secrets.token_urlsafe(32)

    params = {
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPES,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "state": state,
    }

    auth_url = f"{AUTH_ENDPOINT}?{urlencode(params)}"

    _pending_auth[provider_id] = {
        "code_verifier": code_verifier,
        "state": state,
        "auth_code": None,
        "error": None,
        "started_at": time.time(),
    }

    thread = threading.Thread(
        target=_run_callback_server,
        args=(provider_id,),
        daemon=True,
    )
    thread.start()

    return {"auth_url": auth_url, "state": state}


def _run_callback_server(provider_id: str):
    """Run a temporary HTTP server on port 1455 to receive the OAuth callback."""
    pending = _pending_auth.get(provider_id)
    if not pending:
        return

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)

            code = params.get("code", [None])[0]
            recv_state = params.get("state", [None])[0]
            error = params.get("error", [None])[0]

            if error:
                desc = params.get("error_description", [error])[0]
                pending["error"] = desc
                self._respond(f"Authentication failed: {desc}")
                return

            if recv_state != pending["state"]:
                pending["error"] = "State mismatch"
                self._respond("Authentication failed (state mismatch).")
                return

            if code:
                pending["auth_code"] = code
                self._respond("Authentication successful! You can close this tab.")
            else:
                pending["error"] = "No authorization code received"
                self._respond("Authentication failed.")

        def _respond(self, message):
            html = (
                "<!DOCTYPE html><html><head><title>Evonic - Codex Auth</title></head>"
                '<body style="font-family:system-ui;display:flex;justify-content:center;'
                'align-items:center;height:100vh;margin:0;background:#f9fafb">'
                f'<div style="text-align:center;padding:2rem"><h2>{message}</h2>'
                "<p>Returning to Evonic...</p>"
                "<script>setTimeout(function(){window.close()},2000)</script>"
                "</div></body></html>"
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(html.encode())

        def log_message(self, format, *args):
            pass

    try:
        server = HTTPServer(("127.0.0.1", 1455), Handler)
        server.timeout = 300
        server.handle_request()
    except OSError as e:
        _log.error("Callback server port 1455 unavailable: %s", e)
        pending["error"] = f"Port 1455 unavailable: {e}"
    except Exception as e:
        _log.error("Callback server error: %s", e)
        pending["error"] = str(e)


def check_auth_status(provider_id: str) -> Dict:
    """Check if the OAuth callback has been received."""
    pending = _pending_auth.get(provider_id)
    if not pending:
        return {"status": "no_pending"}

    if pending.get("error"):
        error = pending["error"]
        _pending_auth.pop(provider_id, None)
        return {"status": "error", "error": error}

    if pending.get("auth_code"):
        return {"status": "code_received"}

    if time.time() - pending["started_at"] > 300:
        _pending_auth.pop(provider_id, None)
        return {"status": "expired"}

    return {"status": "pending"}


def exchange_code_for_tokens(provider_id: str) -> Dict:
    """Exchange the authorization code for access + refresh tokens."""
    pending = _pending_auth.get(provider_id)
    if not pending or not pending.get("auth_code"):
        return {"error": "No authorization code available"}

    resp = requests.post(
        TOKEN_ENDPOINT,
        data={
            "grant_type": "authorization_code",
            "client_id": CLIENT_ID,
            "code": pending["auth_code"],
            "redirect_uri": REDIRECT_URI,
            "code_verifier": pending["code_verifier"],
        },
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        timeout=15,
    )

    _pending_auth.pop(provider_id, None)

    if resp.status_code != 200:
        return {"error": f"Token exchange failed (HTTP {resp.status_code}): {resp.text[:300]}"}

    data = resp.json()
    if "access_token" not in data:
        return {"error": "No access_token in response"}

    return {
        "access_token": data["access_token"],
        "refresh_token": data.get("refresh_token", ""),
        "expires_in": data.get("expires_in", 3600),
    }


def extract_account_id(access_token: str) -> str:
    """Extract ChatGPT account ID from JWT access token claims."""
    try:
        parts = access_token.split(".")
        if len(parts) < 2:
            return ""
        payload = parts[1]
        padding = 4 - len(payload) % 4
        if padding != 4:
            payload += "=" * padding
        claims = _json.loads(base64.urlsafe_b64decode(payload))
        auth = claims.get("https://api.openai.com/auth", {})
        if isinstance(auth, dict):
            return auth.get("chatgpt_account_id", "")
        return ""
    except Exception:
        return ""


def refresh_access_token(refresh_token: str) -> Dict:
    """Use a refresh token to get a new access token."""
    resp = requests.post(
        TOKEN_ENDPOINT,
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": CLIENT_ID,
        },
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        timeout=15,
    )

    if resp.status_code != 200:
        return {"error": f"Token refresh failed (HTTP {resp.status_code}): {resp.text[:200]}"}

    data = resp.json()
    if "access_token" not in data:
        return {"error": "No access_token in refresh response"}

    return {
        "access_token": data["access_token"],
        "refresh_token": data.get("refresh_token", refresh_token),
        "expires_in": data.get("expires_in", 3600),
    }


def store_tokens(db, provider_id: str, tokens: Dict) -> bool:
    """Persist OAuth tokens to the provider row."""
    expires_at = int(time.time()) + int(tokens.get("expires_in", 3600))
    return db.update_provider(provider_id, {
        "api_key": tokens["access_token"],
        "refresh_token": tokens.get("refresh_token", ""),
        "token_expires_at": expires_at,
        "auth_type": "oauth",
    })


def get_valid_token(db, provider_id: str) -> Optional[str]:
    """Return a valid access token, refreshing if near expiry."""
    provider = db.get_provider(provider_id)
    if not provider or provider.get("auth_type") != "oauth":
        return None

    access_token = provider.get("api_key")
    expires_at = provider.get("token_expires_at") or 0
    refresh_tok = provider.get("refresh_token") or ""

    if access_token and expires_at > time.time() + 300:
        return access_token

    if not refresh_tok:
        return None

    result = refresh_access_token(refresh_tok)
    if "error" in result:
        _log.warning("Codex token refresh failed for provider %s: %s", provider_id, result["error"])
        return None

    store_tokens(db, provider_id, result)
    return result["access_token"]


def clear_tokens(db, provider_id: str) -> bool:
    """Disconnect: clear all OAuth tokens from the provider."""
    return db.update_provider(provider_id, {
        "api_key": "",
        "refresh_token": "",
        "token_expires_at": 0,
        "auth_type": "api_key",
    })
