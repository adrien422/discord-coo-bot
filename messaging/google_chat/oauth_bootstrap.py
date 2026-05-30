#!/usr/bin/env python3
"""One-shot OAuth bootstrap for the Chat user account (e.g. iris@projectbyall.com).

Runs the standard SSH-tunnel OAuth dance:

  1. Operator opens an SSH local-forward `ssh -L 8090:localhost:8090 <vm>`
  2. This script prints a consent URL — operator opens it in their LOCAL browser
  3. Operator approves; Google redirects to http://localhost:8090/callback
  4. The script (running on the VM) captures the auth code from that redirect
  5. Trades it for an access + refresh token, writes
     <tenant_dir>/integrations/google/credentials.json  (chmod 600)

Scopes requested cover the full set Ivan asked for: Chat, Sheets, Docs, Drive,
Forms, Tasks, Calendar, Gmail (modify). The same credentials.json is then read
by both coo_chat.py (polling Chat) and the existing `google` integration
plugin (for Sheets/Docs/Drive sync).

Usage on the VM:

    set -a; source <tenant>/messaging/secrets.env; set +a
    python3 messaging/google_chat/oauth_bootstrap.py <tenant_dir>
"""
from __future__ import annotations

import http.server
import json
import os
import secrets as _secrets
import socketserver
import sys
import threading
import time
import urllib.parse
from pathlib import Path

import requests

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
REDIRECT_URI = "http://localhost:8090/callback"

SCOPES = [
    # Chat — read DMs/spaces and post as the user
    "https://www.googleapis.com/auth/chat.messages",
    "https://www.googleapis.com/auth/chat.spaces",
    "https://www.googleapis.com/auth/chat.memberships.readonly",
    # Workspace data
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/forms.body",
    "https://www.googleapis.com/auth/forms.responses.readonly",
    "https://www.googleapis.com/auth/tasks",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/gmail.modify",
    # Identity (so we can resolve users/me)
    "https://www.googleapis.com/auth/userinfo.email",
    "openid",
]


class _Handler(http.server.BaseHTTPRequestHandler):
    captured: dict = {}

    def log_message(self, *args, **kwargs):  # silence the default access log
        return

    def do_GET(self):  # noqa: N802
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if "code" in qs:
            _Handler.captured["code"] = qs["code"][0]
            _Handler.captured["state"] = qs.get("state", [""])[0]
            body = (b"<!doctype html><meta charset=utf-8>"
                    b"<h1 style='font-family:sans-serif'>OAuth complete.</h1>"
                    b"<p>You can close this tab and return to the terminal.</p>")
        elif "error" in qs:
            _Handler.captured["error"] = qs["error"][0]
            body = (b"<!doctype html><meta charset=utf-8>"
                    b"<h1 style='font-family:sans-serif'>OAuth error.</h1>"
                    b"<pre>" + qs["error"][0].encode() + b"</pre>")
        else:
            body = b"unknown"
        self.send_response(200 if "error" not in qs else 400)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _load_client_creds(slug: str) -> tuple[str, str]:
    """Find client_id / client_secret. Env wins; otherwise ~/.config/coo/<slug>-google-oauth.env."""
    cid = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "").strip()
    csec = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "").strip()
    if not (cid and csec):
        cfg = Path.home() / ".config" / "coo" / f"{slug}-google-oauth.env"
        if cfg.exists():
            for line in cfg.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())
            cid = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "").strip()
            csec = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "").strip()
    if not (cid and csec):
        sys.exit(
            "Set GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET in env, "
            f"or place them in ~/.config/coo/{slug}-google-oauth.env"
        )
    return cid, csec


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit("Usage: oauth_bootstrap.py <tenant_dir>")
    tenant_dir = Path(sys.argv[1]).resolve()
    if not tenant_dir.is_dir():
        sys.exit(f"Not a directory: {tenant_dir}")
    slug = tenant_dir.name
    cid, csec = _load_client_creds(slug)

    state = _secrets.token_urlsafe(16)
    url = AUTH_ENDPOINT + "?" + urllib.parse.urlencode({
        "client_id": cid,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
    })

    bar = "─" * 72
    print(bar)
    print(f"OAuth bootstrap for {slug}")
    print(bar)
    print()
    print("1. From your LOCAL machine, open an SSH tunnel (if not already):")
    print()
    print("     ssh -L 8090:localhost:8090 <this-vm>")
    print()
    print("2. Open this URL in your LOCAL browser and approve all the scopes:")
    print()
    print(f"     {url}")
    print()
    print("Waiting for the redirect on http://localhost:8090/callback ...")
    print("(timeout 5 minutes)")

    server = socketserver.TCPServer(("127.0.0.1", 8090), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    deadline = time.time() + 300
    while time.time() < deadline and not (_Handler.captured.get("code") or _Handler.captured.get("error")):
        time.sleep(0.4)
    server.shutdown(); server.server_close()

    if _Handler.captured.get("error"):
        sys.exit(f"OAuth error: {_Handler.captured['error']}")
    if not _Handler.captured.get("code"):
        sys.exit("Timed out — no redirect received.")
    if _Handler.captured.get("state") != state:
        sys.exit("State mismatch — aborting.")

    print("\nExchanging code for tokens ...")
    resp = requests.post(TOKEN_ENDPOINT, data={
        "code": _Handler.captured["code"],
        "client_id": cid,
        "client_secret": csec,
        "redirect_uri": REDIRECT_URI,
        "grant_type": "authorization_code",
    }, timeout=30)
    if resp.status_code != 200:
        sys.exit(f"Token exchange failed ({resp.status_code}): {resp.text[:500]}")
    tok = resp.json()
    if "refresh_token" not in tok:
        sys.exit(
            "Google did NOT return a refresh_token. Revoke prior grants at "
            "https://myaccount.google.com/permissions then rerun this script."
        )

    creds = {
        "client_id": cid,
        "client_secret": csec,
        "access_token": tok["access_token"],
        "refresh_token": tok["refresh_token"],
        "expires_at": int(time.time() + int(tok.get("expires_in", 3600)) - 60),
        "scope": tok.get("scope", " ".join(SCOPES)),
        "token_type": tok.get("token_type", "Bearer"),
    }

    out = tenant_dir / "integrations" / "google" / "credentials.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(creds, indent=2))
    out.chmod(0o600)

    print(f"\nDone. Credentials written to {out} (0600).")
    print(f"Granted scopes: {tok.get('scope','(see file)')}")


if __name__ == "__main__":
    main()
