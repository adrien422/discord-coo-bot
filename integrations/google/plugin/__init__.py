"""Google Workspace integration.

Talks to Google's REST APIs directly via ``requests`` (no google-api-python-client
dependency). The integration framework calls, in order:

  oauth_url(client_id, redirect_uri, state)            -> str   (connect: build consent URL)
  exchange_code(client_id, client_secret, ru, code)    -> dict  (connect: code -> creds)
  sync(tenant_db, team_slug, creds)                    -> dict  (cadence + sync-now)

plus the agent-facing action functions (read_file, write_doc, append_sheet,
list_drive) for live read/write on command.

Credentials dict (persisted by the framework at
tenants/<slug>/integrations/google/credentials.json) carries everything sync
needs to refresh on its own — including client_id/client_secret — because sync()
only receives ``creds``:

  {client_id, client_secret, access_token, refresh_token, expires_at, scope, token_type}

If sync refreshes the access token it returns it under ``creds_refreshed`` so the
framework re-persists it.
"""
from __future__ import annotations

import html
import json
import re
import sqlite3
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

import requests

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
DRIVE_FILES = "https://www.googleapis.com/drive/v3/files"
DRIVE_UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"
SHEETS = "https://sheets.googleapis.com/v4/spreadsheets"

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive",
]

FOLDER_MIME = "application/vnd.google-apps.folder"
DOC_MIME = "application/vnd.google-apps.document"
SHEET_MIME = "application/vnd.google-apps.spreadsheet"

_TIMEOUT = 30


# --------------------------------------------------------------------------- #
# OAuth (pure URL/expiry logic + the two network steps)
# --------------------------------------------------------------------------- #
def oauth_url(client_id: str, redirect_uri: str, state: str) -> str:
    """Build the Google consent URL. access_type=offline + prompt=consent are
    required to be handed a refresh_token (Google omits it otherwise)."""
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
    }
    return AUTH_ENDPOINT + "?" + urllib.parse.urlencode(params)


def _expiry_from(expires_in: int, *, now: float | None = None) -> int:
    """Absolute unix expiry from a relative expires_in, minus a 60s safety margin."""
    base = time.time() if now is None else now
    return int(base + max(0, int(expires_in) - 60))


def exchange_code(client_id: str, client_secret: str, redirect_uri: str,
                  code: str) -> dict:
    """Trade an authorization code for tokens. Returns the full creds dict the
    framework persists."""
    resp = requests.post(TOKEN_ENDPOINT, data={
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }, timeout=_TIMEOUT)
    if resp.status_code != 200:
        raise RuntimeError(f"token exchange failed ({resp.status_code}): {resp.text}")
    tok = resp.json()
    if "refresh_token" not in tok:
        raise RuntimeError(
            "Google did not return a refresh_token. Revoke prior access at "
            "https://myaccount.google.com/permissions and reconnect (the consent "
            "URL already requests offline access + prompt=consent)."
        )
    return {
        "client_id": client_id,
        "client_secret": client_secret,
        "access_token": tok["access_token"],
        "refresh_token": tok["refresh_token"],
        "expires_at": _expiry_from(tok.get("expires_in", 3600)),
        "scope": tok.get("scope", " ".join(SCOPES)),
        "token_type": tok.get("token_type", "Bearer"),
    }


def _ensure_token(creds: dict) -> tuple[dict, bool]:
    """Return (creds, refreshed?). Refreshes the access token if expired/missing."""
    if creds.get("access_token") and time.time() < creds.get("expires_at", 0):
        return creds, False
    resp = requests.post(TOKEN_ENDPOINT, data={
        "refresh_token": creds["refresh_token"],
        "client_id": creds["client_id"],
        "client_secret": creds["client_secret"],
        "grant_type": "refresh_token",
    }, timeout=_TIMEOUT)
    if resp.status_code != 200:
        raise RuntimeError(f"token refresh failed ({resp.status_code}): {resp.text}")
    tok = resp.json()
    updated = dict(creds)
    updated["access_token"] = tok["access_token"]
    updated["expires_at"] = _expiry_from(tok.get("expires_in", 3600))
    if tok.get("refresh_token"):  # Google occasionally rotates it
        updated["refresh_token"] = tok["refresh_token"]
    return updated, True


# --------------------------------------------------------------------------- #
# Thin Google REST helpers
# --------------------------------------------------------------------------- #
def _hdr(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _check(resp: requests.Response, what: str) -> dict:
    if resp.status_code // 100 != 2:
        raise RuntimeError(f"{what} failed ({resp.status_code}): {resp.text[:400]}")
    return resp.json() if resp.content else {}


def _drive_find(token: str, name: str, parent: str | None, mime: str | None) -> dict | None:
    """Find a non-trashed Drive file by exact name (optionally within a parent)."""
    clauses = [f"name = '{name.replace(chr(39), chr(92) + chr(39))}'", "trashed = false"]
    if parent:
        clauses.append(f"'{parent}' in parents")
    if mime:
        clauses.append(f"mimeType = '{mime}'")
    resp = requests.get(DRIVE_FILES, headers=_hdr(token), params={
        "q": " and ".join(clauses),
        "fields": "files(id,name,webViewLink)",
        "pageSize": 1,
    }, timeout=_TIMEOUT)
    files = _check(resp, "drive search").get("files", [])
    return files[0] if files else None


def _drive_create_folder(token: str, name: str, parent: str | None) -> dict:
    body = {"name": name, "mimeType": FOLDER_MIME}
    if parent:
        body["parents"] = [parent]
    resp = requests.post(DRIVE_FILES, headers=_hdr(token),
                         params={"fields": "id,name,webViewLink"},
                         json=body, timeout=_TIMEOUT)
    return _check(resp, "create folder")


def _drive_upload(token: str, name: str, parent: str | None, data: bytes,
                  mime: str, *, convert_to: str | None = None,
                  file_id: str | None = None) -> dict:
    """Multipart create (or media-update) a Drive file. convert_to triggers
    Google's import conversion (e.g. text/html -> Google Doc)."""
    meta: dict = {"name": name}
    if convert_to:
        meta["mimeType"] = convert_to
    if parent and not file_id:
        meta["parents"] = [parent]
    boundary = "coo-boundary-7f3a9"
    body = (
        f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
        + json.dumps(meta)
        + f"\r\n--{boundary}\r\nContent-Type: {mime}\r\n\r\n"
    ).encode() + data + f"\r\n--{boundary}--\r\n".encode()
    headers = _hdr(token)
    headers["Content-Type"] = f"multipart/related; boundary={boundary}"
    url = f"{DRIVE_UPLOAD}/{file_id}" if file_id else DRIVE_UPLOAD
    method = requests.patch if file_id else requests.post
    resp = method(url, headers=headers,
                  params={"uploadType": "multipart", "fields": "id,name,webViewLink"},
                  data=body, timeout=_TIMEOUT)
    return _check(resp, "drive upload")


# --------------------------------------------------------------------------- #
# Artifact registry (idempotency)
# --------------------------------------------------------------------------- #
def _ensure_artifacts_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS google_artifacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
            ref_key TEXT NOT NULL, file_id TEXT NOT NULL, web_link TEXT,
            source_sig TEXT, created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE (kind, ref_key)) STRICT""")


def _artifact(conn: sqlite3.Connection, kind: str, ref_key: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM google_artifacts WHERE kind = ? AND ref_key = ?",
        (kind, ref_key),
    ).fetchone()


def _remember(conn: sqlite3.Connection, kind: str, ref_key: str, file_id: str,
              web_link: str | None, source_sig: str | None = None) -> None:
    with conn:
        conn.execute(
            "INSERT INTO google_artifacts (kind, ref_key, file_id, web_link, source_sig) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(kind, ref_key) DO UPDATE SET "
            "  file_id = excluded.file_id, web_link = excluded.web_link, "
            "  source_sig = excluded.source_sig, updated_at = datetime('now')",
            (kind, ref_key, file_id, web_link, source_sig),
        )


def _ensure_folder(token: str, conn: sqlite3.Connection, kind: str, ref_key: str,
                   name: str, parent: str | None) -> str:
    """Return the Drive folder id for (kind, ref_key), creating it once. Re-finds
    by name if the recorded id was trashed/removed."""
    row = _artifact(conn, kind, ref_key)
    if row:
        return row["file_id"]
    found = _drive_find(token, name, parent, FOLDER_MIME)
    info = found or _drive_create_folder(token, name, parent)
    _remember(conn, kind, ref_key, info["id"], info.get("webViewLink"))
    return info["id"]


# --------------------------------------------------------------------------- #
# Pure renderers (testable without network or DB)
# --------------------------------------------------------------------------- #
def _company_map_rows(conn: sqlite3.Connection) -> dict[str, list[list[str]]]:
    """Build the per-tab rows for the company-map spreadsheet from the tenant DB.
    Each value is [header_row, *data_rows]."""
    def q(sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        return conn.execute(sql, args).fetchall()

    people = [["Name", "Role", "Team", "Access", "Approver", "Email", "Discord"]]
    for r in q("""SELECT p.display_name, COALESCE(p.role,'') role,
                         COALESCE(t.name,'') team, p.access_tier,
                         p.is_content_approver, COALESCE(p.email,'') email,
                         COALESCE(p.discord_username,'') du
                  FROM people p LEFT JOIN teams t ON t.id = p.team_id
                  WHERE p.deleted_at IS NULL ORDER BY p.access_tier, p.display_name"""):
        people.append([r["display_name"], r["role"], r["team"], r["access_tier"],
                       "yes" if r["is_content_approver"] else "", r["email"], r["du"]])

    teams = [["Team", "Lead", "Description"]]
    for r in q("""SELECT t.name, COALESCE(p.display_name,'') lead,
                         COALESCE(t.description,'') descr
                  FROM teams t LEFT JOIN people p ON p.id = t.lead_person_id
                  WHERE t.deleted_at IS NULL ORDER BY t.name"""):
        teams.append([r["name"], r["lead"], r["descr"]])

    priorities = [["Scope", "Rank", "Title", "Period", "Description"]]
    for r in q("""SELECT scope_kind, rank, title, period, COALESCE(description,'') d
                  FROM priorities WHERE is_current = 1
                  ORDER BY scope_kind, rank"""):
        priorities.append([r["scope_kind"], str(r["rank"]), r["title"],
                           r["period"], r["d"]])

    decisions = [["Date", "Title", "Decision", "Rationale"]]
    for r in q("""SELECT decided_at, title, decision_text, COALESCE(rationale,'') rat
                  FROM decisions WHERE is_current = 1 ORDER BY decided_at DESC"""):
        decisions.append([r["decided_at"], r["title"], r["decision_text"], r["rat"]])

    commitments = [["Who", "Commitment", "Due", "Status"]]
    for r in q("""SELECT p.display_name, c.description, COALESCE(c.due_at,'') due,
                         c.status
                  FROM commitments c JOIN people p ON p.id = c.person_id
                  ORDER BY c.status, c.due_at"""):
        commitments.append([r["display_name"], r["description"], r["due"], r["status"]])

    workflows = [["Workflow", "Owner team", "Cadence", "Description"]]
    for r in q("""SELECT w.name, COALESCE(t.name,'') team, COALESCE(w.cadence,'') cad,
                         COALESCE(w.description,'') d
                  FROM workflows w LEFT JOIN teams t ON t.id = w.owner_team_id
                  WHERE w.deleted_at IS NULL ORDER BY w.name"""):
        workflows.append([r["name"], r["team"], r["cad"], r["d"]])

    risks = [["Title", "Likelihood", "Impact", "Status", "Mitigation"]]
    for r in q("""SELECT title, COALESCE(likelihood,'') l, COALESCE(impact,'') i,
                         status, COALESCE(mitigation,'') m
                  FROM risks ORDER BY status, title"""):
        risks.append([r["title"], r["l"], r["i"], r["status"], r["m"]])

    # Facts — the heart of the company map, incl. per-person enrichment pulled
    # from connected apps (HubSpot, Gleap, ClickUp, Zeevou…). Resolve the
    # subject to a readable name so person-linked facts are obvious.
    facts = [["Subject", "Predicate", "Value", "Asserted"]]
    for r in q("""SELECT f.subject_kind,
                         CASE f.subject_kind
                           WHEN 'person'  THEN COALESCE(p.display_name, 'person#'||f.subject_id)
                           WHEN 'team'    THEN COALESCE(t.name, 'team#'||f.subject_id)
                           WHEN 'company' THEN 'company'
                           ELSE f.subject_kind END subj,
                         f.predicate, COALESCE(f.object_text,'') obj, f.asserted_at
                  FROM facts f
                  LEFT JOIN people p ON f.subject_kind='person' AND p.id=f.subject_id
                  LEFT JOIN teams  t ON f.subject_kind='team'   AND t.id=f.subject_id
                  WHERE f.is_current = 1
                  ORDER BY f.subject_kind, subj, f.predicate"""):
        facts.append([r["subj"], r["predicate"], r["obj"], r["asserted_at"]])

    return {
        "Org Chart": people, "Teams": teams, "Priorities": priorities,
        "Facts": facts, "Decisions": decisions, "Commitments": commitments,
        "Workflows": workflows, "Risks": risks,
    }


def _md_to_html(md: str, title: str) -> str:
    """Minimal Markdown -> HTML so Drive can import factsheets as Google Docs.
    Handles headings, bold, bullet lists, and paragraphs — enough for our reports."""
    out: list[str] = [f"<h1>{html.escape(title)}</h1>"]
    in_list = False
    for raw in md.splitlines():
        line = raw.rstrip()
        if not line.strip():
            if in_list:
                out.append("</ul>")
                in_list = False
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            if in_list:
                out.append("</ul>"); in_list = False
            level = min(len(m.group(1)) + 1, 6)
            out.append(f"<h{level}>{_inline(m.group(2))}</h{level}>")
            continue
        m = re.match(r"^\s*[-*]\s+(.*)$", line)
        if m:
            if not in_list:
                out.append("<ul>"); in_list = True
            out.append(f"<li>{_inline(m.group(1))}</li>")
            continue
        if in_list:
            out.append("</ul>"); in_list = False
        out.append(f"<p>{_inline(line)}</p>")
    if in_list:
        out.append("</ul>")
    return "<html><body>" + "\n".join(out) + "</body></html>"


def _inline(text: str) -> str:
    esc = html.escape(text)
    esc = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", esc)
    esc = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"<i>\1</i>", esc)
    return esc


# --------------------------------------------------------------------------- #
# Sheets mirror
# --------------------------------------------------------------------------- #
def _ensure_sheet(token: str, conn: sqlite3.Connection, parent: str, title: str) -> str:
    row = _artifact(conn, "spreadsheet", "company-map")
    if row:
        return row["file_id"]
    found = _drive_find(token, title, parent, SHEET_MIME)
    if found:
        _remember(conn, "spreadsheet", "company-map", found["id"], found.get("webViewLink"))
        return found["id"]
    created = _check(requests.post(SHEETS, headers=_hdr(token),
                                   json={"properties": {"title": title}},
                                   timeout=_TIMEOUT), "create spreadsheet")
    sid = created["spreadsheetId"]
    # Move it into the COO root folder (it lands in My Drive root by default).
    requests.patch(f"{DRIVE_FILES}/{sid}", headers=_hdr(token),
                   params={"addParents": parent, "removeParents": "root",
                           "fields": "id"}, timeout=_TIMEOUT)
    link = f"https://docs.google.com/spreadsheets/d/{sid}"
    _remember(conn, "spreadsheet", "company-map", sid, link)
    return sid


def _sync_sheet_tabs(token: str, sid: str, tabs: dict[str, list[list[str]]]) -> None:
    """Ensure each named tab exists, then overwrite its contents."""
    meta = _check(requests.get(f"{SHEETS}/{sid}", headers=_hdr(token),
                               params={"fields": "sheets.properties"},
                               timeout=_TIMEOUT), "get spreadsheet")
    existing = {s["properties"]["title"]: s["properties"]["sheetId"]
                for s in meta.get("sheets", [])}
    add_requests = [{"addSheet": {"properties": {"title": name}}}
                    for name in tabs if name not in existing]
    if add_requests:
        _check(requests.post(f"{SHEETS}/{sid}:batchUpdate", headers=_hdr(token),
                             json={"requests": add_requests}, timeout=_TIMEOUT),
               "add sheets")
    # Clear then write each tab.
    for name, rows in tabs.items():
        _check(requests.post(f"{SHEETS}/{sid}/values/{urllib.parse.quote(name)}:clear",
                             headers=_hdr(token), json={}, timeout=_TIMEOUT),
               f"clear {name}")
        _check(requests.put(
            f"{SHEETS}/{sid}/values/{urllib.parse.quote(name)}!A1",
            headers=_hdr(token), params={"valueInputOption": "RAW"},
            json={"values": rows or [[""]]}, timeout=_TIMEOUT), f"write {name}")
    # Drop the default 'Sheet1' if we never used it.
    if "Sheet1" in existing and "Sheet1" not in tabs and len(existing) >= 1:
        try:
            _check(requests.post(f"{SHEETS}/{sid}:batchUpdate", headers=_hdr(token),
                                 json={"requests": [{"deleteSheet": {
                                     "sheetId": existing["Sheet1"]}}]},
                                 timeout=_TIMEOUT), "delete Sheet1")
        except RuntimeError:
            pass  # can't delete the only remaining sheet — harmless


# --------------------------------------------------------------------------- #
# Reports -> Docs and transcripts -> Drive
# --------------------------------------------------------------------------- #
def _mirror_reports(token: str, conn: sqlite3.Connection, folder: str) -> int:
    n = 0
    rows = conn.execute(
        "SELECT id, report_kind, title, content_md, generated_at "
        "FROM reports WHERE is_current = 1 ORDER BY generated_at DESC LIMIT 200"
    ).fetchall()
    for r in rows:
        ref = f"report:{r['id']}"
        sig = str(r["generated_at"])
        prior = _artifact(conn, "doc", ref)
        if prior and prior["source_sig"] == sig:
            continue
        title = r["title"] or f"{r['report_kind']} #{r['id']}"
        body = _md_to_html(r["content_md"] or "", title).encode()
        info = _drive_upload(token, title, folder if not prior else None, body,
                             "text/html", convert_to=DOC_MIME,
                             file_id=prior["file_id"] if prior else None)
        _remember(conn, "doc", ref, info["id"], info.get("webViewLink"), sig)
        n += 1
    return n


def _backup_transcripts(token: str, conn: sqlite3.Connection, folder: str) -> int:
    n = 0
    rows = conn.execute(
        "SELECT file_path, last_appended_at FROM transcripts "
        "ORDER BY date DESC LIMIT 400"
    ).fetchall()
    for r in rows:
        path = Path(r["file_path"])
        if not path.exists():
            continue
        ref = f"transcript:{path}"
        sig = str(r["last_appended_at"] or path.stat().st_mtime)
        prior = _artifact(conn, "file", ref)
        if prior and prior["source_sig"] == sig:
            continue
        data = path.read_bytes()
        info = _drive_upload(token, path.name, folder if not prior else None, data,
                             "text/markdown",
                             file_id=prior["file_id"] if prior else None)
        _remember(conn, "file", ref, info["id"], info.get("webViewLink"), sig)
        n += 1
    return n


# --------------------------------------------------------------------------- #
# Entry points
# --------------------------------------------------------------------------- #
def _company_name(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT value FROM system_config WHERE key = 'company_name'"
    ).fetchone()
    return (row["value"] if row else None) or "Company"


def sync(tenant_db: str, team_slug: str, creds: dict) -> dict:
    """Mirror the company map to Sheets, factsheets to Docs, and back up
    transcripts + reports to Drive. Idempotent."""
    creds, refreshed = _ensure_token(creds)
    token = creds["access_token"]
    conn = sqlite3.connect(tenant_db)
    conn.row_factory = sqlite3.Row
    try:
        _ensure_artifacts_table(conn)
        company = _company_name(conn)
        root = _ensure_folder(token, conn, "root_folder", "root",
                              f"COO Agent — {company}", None)
        sheet_id = _ensure_sheet(token, conn, root, f"{company} — Company Map")
        _sync_sheet_tabs(token, sheet_id, _company_map_rows(conn))
        reports_folder = _ensure_folder(token, conn, "folder", "reports",
                                        "Reports", root)
        n_docs = _mirror_reports(token, conn, reports_folder)
        tx_folder = _ensure_folder(token, conn, "folder", "transcripts",
                                   "Transcripts", root)
        n_tx = _backup_transcripts(token, conn, tx_folder)
        sheet_link = (_artifact(conn, "spreadsheet", "company-map") or {})["web_link"]
    finally:
        conn.close()
    result = {
        "company_map_sheet": sheet_link,
        "reports_synced": n_docs,
        "transcripts_backed_up": n_tx,
        "team": team_slug,
    }
    if refreshed:
        result["creds_refreshed"] = creds
    return result


# --- agent-facing actions (live read/write on command) --------------------- #
def read_file(creds: dict, file_id: str, **_) -> dict:
    """Export a Drive file as plain text (Docs/Sheets/text)."""
    creds, _r = _ensure_token(creds)
    token = creds["access_token"]
    meta = _check(requests.get(f"{DRIVE_FILES}/{file_id}", headers=_hdr(token),
                               params={"fields": "name,mimeType"}, timeout=_TIMEOUT),
                  "get file meta")
    mime = meta.get("mimeType", "")
    if mime.startswith("application/vnd.google-apps"):
        export = "text/plain" if "spreadsheet" not in mime else "text/csv"
        resp = requests.get(f"{DRIVE_FILES}/{file_id}/export", headers=_hdr(token),
                            params={"mimeType": export}, timeout=_TIMEOUT)
    else:
        resp = requests.get(f"{DRIVE_FILES}/{file_id}", headers=_hdr(token),
                            params={"alt": "media"}, timeout=_TIMEOUT)
    if resp.status_code // 100 != 2:
        raise RuntimeError(f"read failed ({resp.status_code}): {resp.text[:300]}")
    return {"name": meta.get("name"), "mimeType": mime, "text": resp.text}


def write_doc(creds: dict, title: str, markdown: str, folder_id: str | None = None,
              **_) -> dict:
    """Create a new Google Doc from Markdown. Returns its id + link."""
    creds, _r = _ensure_token(creds)
    info = _drive_upload(creds["access_token"], title, folder_id,
                         _md_to_html(markdown, title).encode(), "text/html",
                         convert_to=DOC_MIME)
    return {"id": info["id"], "link": info.get("webViewLink")}


def append_sheet(creds: dict, spreadsheet_id: str, tab: str, rows: list[list],
                 **_) -> dict:
    """Append rows to a sheet tab."""
    creds, _r = _ensure_token(creds)
    out = _check(requests.post(
        f"{SHEETS}/{spreadsheet_id}/values/{urllib.parse.quote(tab)}:append",
        headers=_hdr(creds["access_token"]),
        params={"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"},
        json={"values": rows}, timeout=_TIMEOUT), "append values")
    return {"updated": out.get("updates", {})}


def list_drive(creds: dict, query: str = "", folder_id: str | None = None,
               limit: int = 25, **_) -> dict:
    """List Drive files matching a name fragment (optionally within a folder)."""
    creds, _r = _ensure_token(creds)
    clauses = ["trashed = false"]
    if query:
        clauses.append(f"name contains '{query.replace(chr(39), chr(92) + chr(39))}'")
    if folder_id:
        clauses.append(f"'{folder_id}' in parents")
    out = _check(requests.get(DRIVE_FILES, headers=_hdr(creds["access_token"]), params={
        "q": " and ".join(clauses),
        "fields": "files(id,name,mimeType,webViewLink,modifiedTime)",
        "pageSize": max(1, min(limit, 100)),
        "orderBy": "modifiedTime desc",
    }, timeout=_TIMEOUT), "list drive")
    return {"files": out.get("files", [])}
