#!/usr/bin/env python3
"""
Check Whoop for the most recent workout (any non-sleep activity).
Exit 1 if the last workout ended more than STALE_DAYS days ago.

Required env vars:
  WHOOP_CLIENT_ID
  WHOOP_CLIENT_SECRET

Optional:
  WHOOP_REDIRECT_URI   default: http://localhost:8080/callback
                       must match a redirect URI registered in the Whoop
                       developer dashboard for this client.

Tokens are cached in ~/.whoop_tokens.json (mode 600). The first run opens
a browser for consent; subsequent runs silently refresh.
"""

import json
import os
import secrets
import sys
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

AUTH_URL = "https://api.prod.whoop.com/oauth/oauth2/auth"
TOKEN_URL = "https://api.prod.whoop.com/oauth/oauth2/token"
API_BASE = "https://api.prod.whoop.com/developer"
SCOPES = "read:workout offline"
TOKEN_FILE = Path.home() / ".whoop_tokens.json"
STALE_DAYS = 2
LOOKBACK_DAYS = 30


def _post_form(url, data):
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


def _get_json(url, access_token):
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {access_token}"}
    )
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


def _capture_code(redirect_uri, expected_state):
    parsed = urllib.parse.urlparse(redirect_uri)
    host = parsed.hostname or "localhost"
    port = parsed.port or 8080
    path = parsed.path or "/"
    captured = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if urllib.parse.urlparse(self.path).path != path:
                self.send_response(404)
                self.end_headers()
                return
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            captured["code"] = (q.get("code") or [None])[0]
            captured["state"] = (q.get("state") or [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Whoop auth complete. You can close this tab.")

        def log_message(self, *_args, **_kwargs):
            pass

    server = HTTPServer((host, port), Handler)
    try:
        while "code" not in captured:
            server.handle_request()
    finally:
        server.server_close()

    if captured.get("state") != expected_state:
        sys.exit("OAuth state mismatch - aborting.")
    return captured["code"]


def _authorize(client_id, client_secret, redirect_uri):
    state = secrets.token_urlsafe(16)
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": SCOPES,
        "state": state,
    }
    url = f"{AUTH_URL}?{urllib.parse.urlencode(params)}"
    print("Opening browser for Whoop authorization...")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    print(f"If the browser did not open, visit:\n  {url}\n")
    code = _capture_code(redirect_uri, state)
    return _post_form(
        TOKEN_URL,
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "client_secret": client_secret,
        },
    )


def _refresh(client_id, client_secret, refresh_token):
    return _post_form(
        TOKEN_URL,
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": SCOPES,
        },
    )


def _save_tokens(tok):
    TOKEN_FILE.write_text(json.dumps(tok))
    try:
        os.chmod(TOKEN_FILE, 0o600)
    except OSError:
        pass


def _load_tokens():
    if TOKEN_FILE.exists():
        return json.loads(TOKEN_FILE.read_text())
    return None


def get_access_token(client_id, client_secret, redirect_uri):
    cached = _load_tokens()
    if cached and cached.get("refresh_token"):
        try:
            tok = _refresh(client_id, client_secret, cached["refresh_token"])
            _save_tokens(tok)
            return tok["access_token"]
        except Exception as e:
            print(f"Refresh failed ({e}); re-authorizing.")
    tok = _authorize(client_id, client_secret, redirect_uri)
    _save_tokens(tok)
    return tok["access_token"]


def _parse_iso(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def latest_workout_end(access_token):
    start = (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )
    url = f"{API_BASE}/v2/activity/workout?limit=25&start={urllib.parse.quote(start)}"
    data = _get_json(url, access_token)
    records = data.get("records") if isinstance(data, dict) else data
    if not records:
        return None
    ends = [_parse_iso(r["end"]) for r in records if r.get("end")]
    return max(ends) if ends else None


def main():
    client_id = os.environ.get("WHOOP_CLIENT_ID")
    client_secret = os.environ.get("WHOOP_CLIENT_SECRET")
    redirect_uri = os.environ.get(
        "WHOOP_REDIRECT_URI", "http://localhost:8080/callback"
    )
    if not client_id or not client_secret:
        sys.exit("Set WHOOP_CLIENT_ID and WHOOP_CLIENT_SECRET.")

    token = get_access_token(client_id, client_secret, redirect_uri)
    last_end = latest_workout_end(token)

    now = datetime.now(timezone.utc)
    if last_end is None:
        print(
            f"No workouts found in the last {LOOKBACK_DAYS} days - "
            f"exceeds {STALE_DAYS}-day threshold."
        )
        sys.exit(1)

    age = now - last_end
    days = age.total_seconds() / 86400
    msg_time = last_end.isoformat()
    if age > timedelta(days=STALE_DAYS):
        print(
            f"Last workout ended {days:.1f} days ago ({msg_time}) - "
            f"exceeds {STALE_DAYS}-day threshold."
        )
        sys.exit(1)
    print(
        f"Last workout ended {days:.1f} days ago ({msg_time}) - "
        f"within {STALE_DAYS}-day threshold."
    )


if __name__ == "__main__":
    main()
