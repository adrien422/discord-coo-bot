"""Unit tests for the Google Workspace integration plugin.

Network calls are mocked; we test the pure logic (OAuth URL, token expiry,
Markdown->HTML, company-map row building) and the token exchange/refresh
handling around the network boundary.
"""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
import urllib.parse
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def _load_plugin():
    init = REPO_ROOT / "integrations" / "google" / "plugin" / "__init__.py"
    spec = importlib.util.spec_from_file_location("_google_plugin", init)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


g = _load_plugin()


# --- OAuth URL + expiry ---------------------------------------------------- #
def test_oauth_url_requests_offline_refresh_token():
    url = g.oauth_url("cid.apps.googleusercontent.com", "http://localhost:8090/callback", "st8")
    parsed = urllib.parse.urlparse(url)
    qs = urllib.parse.parse_qs(parsed.query)
    assert parsed.netloc == "accounts.google.com"
    assert qs["access_type"] == ["offline"]
    assert qs["prompt"] == ["consent"]          # forces a refresh_token
    assert qs["state"] == ["st8"]
    assert "spreadsheets" in qs["scope"][0]
    assert "documents" in qs["scope"][0]
    assert "drive" in qs["scope"][0]


def test_expiry_subtracts_safety_margin():
    assert g._expiry_from(3600, now=1000) == 1000 + 3600 - 60
    assert g._expiry_from(0, now=1000) == 1000      # never negative offset


# --- token exchange / refresh (network mocked) ----------------------------- #
class _Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
        self.content = b"x"
        self.text = str(payload)

    def json(self):
        return self._payload


def test_exchange_code_returns_full_creds(monkeypatch):
    monkeypatch.setattr(g.requests, "post", lambda *a, **k: _Resp(200, {
        "access_token": "at", "refresh_token": "rt", "expires_in": 3600,
        "scope": "s", "token_type": "Bearer",
    }))
    creds = g.exchange_code("cid", "secret", "http://localhost:8090/callback", "code123")
    assert creds["access_token"] == "at"
    assert creds["refresh_token"] == "rt"
    assert creds["client_id"] == "cid" and creds["client_secret"] == "secret"
    assert creds["expires_at"] > 0


def test_exchange_code_errors_without_refresh_token(monkeypatch):
    monkeypatch.setattr(g.requests, "post", lambda *a, **k: _Resp(200, {
        "access_token": "at", "expires_in": 3600,
    }))
    with pytest.raises(RuntimeError, match="refresh_token"):
        g.exchange_code("cid", "secret", "ru", "code")


def test_ensure_token_skips_refresh_when_valid(monkeypatch):
    monkeypatch.setattr(g.requests, "post", lambda *a, **k: pytest.fail("should not refresh"))
    creds = {"access_token": "at", "expires_at": g.time.time() + 999}
    out, refreshed = g._ensure_token(creds)
    assert refreshed is False and out is creds


def test_ensure_token_refreshes_when_expired(monkeypatch):
    monkeypatch.setattr(g.requests, "post", lambda *a, **k: _Resp(200, {
        "access_token": "new-at", "expires_in": 3600,
    }))
    creds = {"access_token": "old", "expires_at": 0, "refresh_token": "rt",
             "client_id": "cid", "client_secret": "sec"}
    out, refreshed = g._ensure_token(creds)
    assert refreshed is True
    assert out["access_token"] == "new-at"
    assert out["refresh_token"] == "rt"          # preserved when not rotated


# --- Markdown -> HTML ------------------------------------------------------ #
def test_md_to_html_handles_headings_bold_lists():
    md = "# Title\n\nSome **bold** text.\n\n- one\n- two\n\n## Section"
    out = g._md_to_html(md, "Doc Title")
    assert "<h1>Doc Title</h1>" in out
    assert "<b>bold</b>" in out
    assert out.count("<li>") == 2
    assert "<ul>" in out and "</ul>" in out
    assert "<h3>Section</h3>" in out             # ## -> h3 (level+1)


def test_inline_escapes_html():
    assert "&lt;script&gt;" in g._inline("<script>")


# --- company-map rows (real tenant DB) ------------------------------------- #
def test_company_map_rows_from_seeded_db(fresh_tenant_db):
    conn = sqlite3.connect(str(fresh_tenant_db))
    conn.row_factory = sqlite3.Row
    with conn:
        conn.execute("INSERT INTO system_config (key, value) VALUES ('company_name', 'Acme')")
        conn.execute("INSERT INTO teams (slug, name, description) VALUES ('exec', 'Executive', 'Leadership')")
        conn.execute(
            "INSERT INTO people (slug, display_name, role, team_id, access_tier, "
            "is_content_approver, email, discord_username) "
            "VALUES ('ceo', 'Sean', 'CEO', 1, 'admin', 1, 's@x.com', 'sean#1')"
        )
        conn.execute(
            "INSERT INTO people (slug, display_name, access_tier, deleted_at) "
            "VALUES ('gone', 'Removed Person', 'employee', datetime('now'))"
        )
        conn.execute(
            "INSERT INTO commitments (person_id, description, due_at, status) "
            "VALUES (1, 'Ship v1', '2026-06-01', 'open')"
        )
    tabs = g._company_map_rows(conn)
    assert g._company_name(conn) == "Acme"
    conn.close()

    assert set(["Org Chart", "Teams", "Priorities", "Decisions",
                "Commitments", "Workflows", "Risks"]).issubset(tabs)
    org = tabs["Org Chart"]
    assert org[0][0] == "Name"                    # header
    names = [r[0] for r in org[1:]]
    assert "Sean" in names
    assert "Removed Person" not in names          # soft-deleted excluded
    assert tabs["Commitments"][1][1] == "Ship v1"
