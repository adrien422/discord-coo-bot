#!/usr/bin/env python3
"""Google Workspace CLI for the COO agent (Iris).

The agent runs inside a tmux pane with bash access. This script lets her
actually USE the Workspace scopes her account was granted — Gmail, Sheets,
Docs, Drive, Calendar, Tasks — by shelling out to it. It reuses the same
OAuth credentials.json the Chat listener uses (auto-refreshing the token).

Credentials path resolution (first hit wins):
  1. $COO_CHAT_CREDS_JSON
  2. ./integrations/google/credentials.json under the tenant workdir (cwd)

Usage (the agent calls these via Bash):
  python3 <path>/google_tools.py gmail-list [--query Q] [--max N]
  python3 <path>/google_tools.py gmail-read <message_id>
  python3 <path>/google_tools.py gmail-send --to A --subject S --body B
  python3 <path>/google_tools.py sheets-read <spreadsheet_id> <A1range>
  python3 <path>/google_tools.py sheets-append <spreadsheet_id> <tab> --row "a,b,c"
  python3 <path>/google_tools.py doc-read <document_id>
  python3 <path>/google_tools.py drive-list [--query Q] [--max N]
  python3 <path>/google_tools.py drive-read <file_id>
  python3 <path>/google_tools.py calendar-list [--max N] [--days D]
  python3 <path>/google_tools.py tasks-list [--max N]

All output is JSON on stdout. Errors go to stderr with a non-zero exit.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
from email.mime.text import MIMEText
from pathlib import Path

import requests

TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"


def _creds_path() -> Path:
    env = os.environ.get("COO_CHAT_CREDS_JSON")
    if env and Path(env).exists():
        return Path(env)
    local = Path.cwd() / "integrations" / "google" / "credentials.json"
    if local.exists():
        return local
    sys.exit("Could not find credentials.json (set COO_CHAT_CREDS_JSON).")


def _token() -> str:
    p = _creds_path()
    creds = json.loads(p.read_text())
    if creds.get("access_token") and time.time() < creds.get("expires_at", 0):
        return creds["access_token"]
    r = requests.post(TOKEN_ENDPOINT, data={
        "refresh_token": creds["refresh_token"],
        "client_id": creds["client_id"],
        "client_secret": creds["client_secret"],
        "grant_type": "refresh_token",
    }, timeout=20)
    if r.status_code != 200:
        sys.exit(f"token refresh failed ({r.status_code}): {r.text[:300]}")
    tok = r.json()
    creds["access_token"] = tok["access_token"]
    creds["expires_at"] = int(time.time() + int(tok.get("expires_in", 3600)) - 60)
    if tok.get("refresh_token"):
        creds["refresh_token"] = tok["refresh_token"]
    p.write_text(json.dumps(creds, indent=2)); p.chmod(0o600)
    return creds["access_token"]


def _hdr() -> dict:
    return {"Authorization": f"Bearer {_token()}"}


def _get(url, **kw):
    r = requests.get(url, headers=_hdr(), timeout=30, **kw)
    if r.status_code // 100 != 2:
        sys.exit(f"GET {url} -> {r.status_code}: {r.text[:300]}")
    return r.json() if r.content else {}


def _post(url, **kw):
    r = requests.post(url, headers=_hdr(), timeout=30, **kw)
    if r.status_code // 100 != 2:
        sys.exit(f"POST {url} -> {r.status_code}: {r.text[:300]}")
    return r.json() if r.content else {}


def out(obj) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False))


# --------------------------- Gmail ---------------------------
def gmail_list(a):
    params = {"maxResults": a.max}
    if a.query:
        params["q"] = a.query
    data = _get("https://gmail.googleapis.com/gmail/v1/users/me/messages", params=params)
    msgs = []
    for m in data.get("messages", [])[:a.max]:
        full = _get(f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{m['id']}",
                    params={"format": "metadata",
                            "metadataHeaders": ["From", "Subject", "Date"]})
        hdrs = {h["name"]: h["value"] for h in full.get("payload", {}).get("headers", [])}
        msgs.append({"id": m["id"], "from": hdrs.get("From"),
                     "subject": hdrs.get("Subject"), "date": hdrs.get("Date"),
                     "snippet": full.get("snippet")})
    out({"messages": msgs})


def _decode_part(part):
    body = part.get("body", {})
    data = body.get("data")
    if data:
        return base64.urlsafe_b64decode(data + "===").decode("utf-8", "replace")
    text = ""
    for p in part.get("parts", []) or []:
        if p.get("mimeType", "").startswith("text/plain"):
            text += _decode_part(p)
    if not text:
        for p in part.get("parts", []) or []:
            text += _decode_part(p)
    return text


def gmail_read(a):
    full = _get(f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{a.message_id}",
                params={"format": "full"})
    payload = full.get("payload", {})
    hdrs = {h["name"]: h["value"] for h in payload.get("headers", [])}
    out({"id": a.message_id, "from": hdrs.get("From"), "to": hdrs.get("To"),
         "subject": hdrs.get("Subject"), "date": hdrs.get("Date"),
         "body": _decode_part(payload)[:20000]})


def gmail_send(a):
    msg = MIMEText(a.body)
    msg["to"] = a.to
    msg["subject"] = a.subject
    if a.cc:
        msg["cc"] = a.cc
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    res = _post("https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
                json={"raw": raw})
    out({"sent": True, "id": res.get("id")})


# --------------------------- Sheets ---------------------------
def sheets_read(a):
    import urllib.parse
    rng = urllib.parse.quote(a.range)
    data = _get(f"https://sheets.googleapis.com/v4/spreadsheets/{a.spreadsheet_id}/values/{rng}")
    out({"range": data.get("range"), "values": data.get("values", [])})


def sheets_append(a):
    import urllib.parse
    rng = urllib.parse.quote(a.tab)
    row = [c.strip() for c in a.row.split(",")]
    res = _post(
        f"https://sheets.googleapis.com/v4/spreadsheets/{a.spreadsheet_id}/values/{rng}:append",
        params={"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"},
        json={"values": [row]})
    out({"appended": res.get("updates", {})})


# --------------------------- Docs ---------------------------
def doc_read(a):
    data = _get(f"https://docs.googleapis.com/v1/documents/{a.document_id}")
    text = []
    for el in data.get("body", {}).get("content", []):
        para = el.get("paragraph")
        if not para:
            continue
        for pe in para.get("elements", []):
            tr = pe.get("textRun")
            if tr and tr.get("content"):
                text.append(tr["content"])
    out({"title": data.get("title"), "text": "".join(text)[:20000]})


# --------------------------- Drive ---------------------------
def drive_list(a):
    params = {"pageSize": a.max, "fields": "files(id,name,mimeType,modifiedTime,webViewLink)",
              "orderBy": "modifiedTime desc"}
    clauses = ["trashed = false"]
    if a.query:
        clauses.append(f"name contains '{a.query}'")
    params["q"] = " and ".join(clauses)
    data = _get("https://www.googleapis.com/drive/v3/files", params=params)
    out({"files": data.get("files", [])})


def drive_read(a):
    meta = _get(f"https://www.googleapis.com/drive/v3/files/{a.file_id}",
                params={"fields": "name,mimeType"})
    mime = meta.get("mimeType", "")
    if mime.startswith("application/vnd.google-apps"):
        export = "text/plain" if "spreadsheet" not in mime else "text/csv"
        r = requests.get(f"https://www.googleapis.com/drive/v3/files/{a.file_id}/export",
                         headers=_hdr(), params={"mimeType": export}, timeout=60)
    else:
        r = requests.get(f"https://www.googleapis.com/drive/v3/files/{a.file_id}",
                         headers=_hdr(), params={"alt": "media"}, timeout=60)
    if r.status_code // 100 != 2:
        sys.exit(f"drive read {r.status_code}: {r.text[:200]}")
    out({"name": meta.get("name"), "mimeType": mime, "text": r.text[:20000]})


# --------------------------- Calendar ---------------------------
def calendar_list(a):
    import datetime
    now = datetime.datetime.utcnow().isoformat() + "Z"
    params = {"maxResults": a.max, "orderBy": "startTime", "singleEvents": "true",
              "timeMin": now}
    data = _get("https://www.googleapis.com/calendar/v3/calendars/primary/events", params=params)
    evs = [{"summary": e.get("summary"),
            "start": e.get("start"), "end": e.get("end"),
            "attendees": [at.get("email") for at in e.get("attendees", [])]}
           for e in data.get("items", [])]
    out({"events": evs})


# --------------------------- Tasks ---------------------------
def tasks_list(a):
    lists = _get("https://tasks.googleapis.com/tasks/v1/users/@me/lists")
    result = {}
    for tl in lists.get("items", [])[:5]:
        t = _get(f"https://tasks.googleapis.com/tasks/v1/lists/{tl['id']}/tasks",
                 params={"maxResults": a.max})
        result[tl["title"]] = [{"title": x.get("title"), "status": x.get("status"),
                                "due": x.get("due")} for x in t.get("items", [])]
    out({"task_lists": result})


def main() -> None:
    p = argparse.ArgumentParser(prog="google_tools")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("gmail-list"); s.add_argument("--query"); s.add_argument("--max", type=int, default=10); s.set_defaults(fn=gmail_list)
    s = sub.add_parser("gmail-read"); s.add_argument("message_id"); s.set_defaults(fn=gmail_read)
    s = sub.add_parser("gmail-send"); s.add_argument("--to", required=True); s.add_argument("--subject", required=True); s.add_argument("--body", required=True); s.add_argument("--cc"); s.set_defaults(fn=gmail_send)
    s = sub.add_parser("sheets-read"); s.add_argument("spreadsheet_id"); s.add_argument("range"); s.set_defaults(fn=sheets_read)
    s = sub.add_parser("sheets-append"); s.add_argument("spreadsheet_id"); s.add_argument("tab"); s.add_argument("--row", required=True); s.set_defaults(fn=sheets_append)
    s = sub.add_parser("doc-read"); s.add_argument("document_id"); s.set_defaults(fn=doc_read)
    s = sub.add_parser("drive-list"); s.add_argument("--query"); s.add_argument("--max", type=int, default=15); s.set_defaults(fn=drive_list)
    s = sub.add_parser("drive-read"); s.add_argument("file_id"); s.set_defaults(fn=drive_read)
    s = sub.add_parser("calendar-list"); s.add_argument("--max", type=int, default=10); s.add_argument("--days", type=int, default=7); s.set_defaults(fn=calendar_list)
    s = sub.add_parser("tasks-list"); s.add_argument("--max", type=int, default=20); s.set_defaults(fn=tasks_list)

    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
