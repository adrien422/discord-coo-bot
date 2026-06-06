"""Google Chat listener for the COO agent — OAuth + polling architecture.

Iris's account (iris@projectbyall.com) is OAuth'd once via oauth_bootstrap.py.
The resulting refresh-token + access-token live in
  <tenant>/integrations/google/credentials.json

This daemon then:
  - Polls `spaces.messages.list` every COO_CHAT_POLL_SECONDS for every space
    Iris is a member of, picking up only messages newer than the last seen one
    per space (state: poll_state.json).
  - Skips messages Iris herself sent (so we don't loop on our own replies).
  - Routes every inbound to the agent via the shared tmux bridge.
  - Captures the agent's reply, dispatches its [[COO_*]] markers — DMs and
    channel posts go OUT via `chat.spaces.messages.create`; persistence
    markers (FACT/COMMITMENT/…) go INTO the tenant DB.

No Chat-app registration. No Pub/Sub. No public URL.

Per-tenant env (set by systemd from <tenant>/messaging/secrets.env):
  COO_TENANT_SLUG, COO_TENANT_DB, COO_PLATFORM_DB, COO_STATE_DIR, COO_WORKDIR,
  COO_TMUX_SESSION, COO_RUN_AI, COO_AGENT_KIND,
  COO_CHAT_CREDS_JSON,     -- path to integrations/google/credentials.json
  COO_CHAT_CEO_EMAIL,      -- Phase-1 interview target
  COO_CHAT_COO_DISPLAY_NAME (default 'Iris'),
  COO_CHAT_POLL_SECONDS    (default 8),
  COO_CHAT_HOME_SPACE      (optional — fallback for [[COO_CHANNEL]] with no name)
"""
from __future__ import annotations

import json
import logging
import os
import re
import signal
import sqlite3
import sys
import threading
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

# Reuse messaging-agnostic helpers from the Discord plugin.
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from messaging.discord.plugin.coo_phase1 import (  # noqa: E402
    AgentBridge,
    normalize_message_text,
    _chunk_message,
    _parse_kv,
    _parse_decision_fields,
    COO_FACT_RE, COO_COMMITMENT_RE, COO_DECISION_RE, COO_WORKFLOW_RE,
    COO_TASK_RE, COO_REPORT_RE, COO_NEXT_CONTACT_RE, COO_CLOSE_RE,
    COO_PERSON_ADD_RE, COO_INBOX_HANDLE_RE,
    NOOP_RE,
)

logger = logging.getLogger("coo_chat")

# Chat-flavoured markers (same shape as the previous Pub/Sub draft).
CHAT_TO_RE = re.compile(
    r'\[\[COO_TO\s+(?:user_id|user_email|email)="?([^"\]\s]+)"?\]\]'
    r'\s*(.+?)(?=(?:\[\[COO_|$))',
    re.S,
)
CHAT_CHANNEL_RE = re.compile(
    r'\[\[COO_CHANNEL\s+(?:(?:name|channel|space)="?#?([^"\]\s]+)"?|id="?([^"\]\s]+)"?)\]\]'
    r'\s*(.+?)(?=(?:\[\[COO_|$))',
    re.S,
)
# Chat-flavoured COO_COMMITMENT — person_id can be an email or a users/<id>
# resource (Discord's regex requires digits, which never matches in Chat).
CHAT_COMMITMENT_RE = re.compile(
    r'\[\[COO_COMMITMENT\s+person_id="?([^"\s\]]+)"?\s+description="([^"]+)"'
    r'(?:\s+due="([^"]+)")?\]\]'
)
# Chat-flavoured COO_NEXT_CONTACT — user_id is an email or users/<id>.
CHAT_NEXT_CONTACT_RE = re.compile(
    r'\[\[COO_NEXT_CONTACT\s+user_id="?([^"\s]+)"?\s+in_seconds=(\d+)\s+reason=([^\]]+)\]\]'
)


# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------
@dataclass
class Config:
    tenant_slug: str
    tenant_db: Path
    platform_db: Path
    state_dir: Path
    workdir: Path
    tmux_session: str
    run_ai: str
    agent_kind: str

    creds_json: Path
    ceo_email: str
    coo_name: str
    poll_seconds: int
    home_space: Optional[str]

    @classmethod
    def from_env(cls) -> "Config":
        def req(k: str) -> str:
            v = os.environ.get(k)
            if not v:
                raise SystemExit(f"Missing env var: {k}")
            return v
        return cls(
            tenant_slug=req("COO_TENANT_SLUG"),
            tenant_db=Path(req("COO_TENANT_DB")),
            platform_db=Path(req("COO_PLATFORM_DB")),
            state_dir=Path(req("COO_STATE_DIR")),
            workdir=Path(req("COO_WORKDIR")),
            tmux_session=req("COO_TMUX_SESSION"),
            run_ai=req("COO_RUN_AI"),
            agent_kind=os.environ.get("COO_AGENT_KIND", "claude"),
            creds_json=Path(req("COO_CHAT_CREDS_JSON")),
            ceo_email=req("COO_CHAT_CEO_EMAIL"),
            coo_name=os.environ.get("COO_CHAT_COO_DISPLAY_NAME", "Iris"),
            poll_seconds=int(os.environ.get("COO_CHAT_POLL_SECONDS", "8")),
            home_space=os.environ.get("COO_CHAT_HOME_SPACE"),
        )


# ----------------------------------------------------------------------------
# OAuth-as-user Chat API client with self-refresh
# ----------------------------------------------------------------------------
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
USERINFO_ENDPOINT = "https://www.googleapis.com/oauth2/v3/userinfo"
CHAT_BASE = "https://chat.googleapis.com/v1"


class UserChatAPI:
    """Thin Chat REST client backed by a refresh-token-based user OAuth flow."""

    def __init__(self, creds_path: Path):
        self.path = creds_path
        self._lock = threading.Lock()
        self.creds = json.loads(creds_path.read_text())

    # ---- token management ----
    def _ensure_token(self) -> str:
        with self._lock:
            if self.creds.get("access_token") and time.time() < self.creds.get("expires_at", 0):
                return self.creds["access_token"]
            resp = requests.post(TOKEN_ENDPOINT, data={
                "refresh_token": self.creds["refresh_token"],
                "client_id": self.creds["client_id"],
                "client_secret": self.creds["client_secret"],
                "grant_type": "refresh_token",
            }, timeout=20)
            if resp.status_code != 200:
                raise RuntimeError(f"refresh failed ({resp.status_code}): {resp.text[:300]}")
            tok = resp.json()
            self.creds["access_token"] = tok["access_token"]
            self.creds["expires_at"] = int(time.time() + int(tok.get("expires_in", 3600)) - 60)
            if tok.get("refresh_token"):
                self.creds["refresh_token"] = tok["refresh_token"]
            self.path.write_text(json.dumps(self.creds, indent=2))
            self.path.chmod(0o600)
            return self.creds["access_token"]

    def _hdr(self) -> dict:
        return {"Authorization": f"Bearer {self._ensure_token()}",
                "Content-Type": "application/json"}

    # ---- identity ----
    def me_email(self) -> str:
        return (self._userinfo().get("email") or "").lower()

    def me_user_resource(self) -> str:
        sub = self._userinfo().get("sub")
        return f"users/{sub}" if sub else ""

    def _userinfo(self) -> dict:
        # Cache once — userinfo rarely changes.
        if not hasattr(self, "_userinfo_cache"):
            resp = requests.get(USERINFO_ENDPOINT, headers=self._hdr(), timeout=15)
            resp.raise_for_status()
            self._userinfo_cache = resp.json()
        return self._userinfo_cache

    # ---- spaces ----
    def list_spaces(self) -> list[dict]:
        out: list[dict] = []
        page_token: Optional[str] = None
        while True:
            params = {"pageSize": 100}
            if page_token:
                params["pageToken"] = page_token
            resp = requests.get(f"{CHAT_BASE}/spaces", headers=self._hdr(),
                                params=params, timeout=20)
            if resp.status_code != 200:
                logger.warning("list_spaces %d: %s", resp.status_code, resp.text[:200])
                return out
            data = resp.json()
            out.extend(data.get("spaces", []))
            page_token = data.get("nextPageToken")
            if not page_token:
                return out

    def find_dm(self, user_ref: str) -> Optional[str]:
        """Find an EXISTING DM space with a user. Returns None if no DM has
        ever been opened (use create_dm to initiate)."""
        name = user_ref if user_ref.startswith("users/") else f"users/{user_ref}"
        # `:findDirectMessage` is a Google API custom verb — colon, not slash.
        url = f"{CHAT_BASE}/spaces:findDirectMessage"
        resp = requests.get(url, headers=self._hdr(),
                            params={"name": name}, timeout=20)
        if resp.status_code == 200:
            return resp.json().get("name")
        if resp.status_code in (403, 404):
            return None
        logger.warning("findDirectMessage(%s) %d: %s", name, resp.status_code,
                       resp.text[:200])
        return None

    def create_dm(self, user_ref: str) -> Optional[str]:
        """Create (or return existing) 1:1 DM space with the given user.

        Uses `spaces:setup`, which works for human-user OAuth (chat.spaces scope).
        Lets Iris initiate cold DMs without needing the other person to message
        her first — that limitation applies to Chat Apps, not human users.
        """
        name = user_ref if user_ref.startswith("users/") else f"users/{user_ref}"
        url = f"{CHAT_BASE}/spaces:setup"
        body = {
            "space": {"spaceType": "DIRECT_MESSAGE"},
            "memberships": [{"member": {"name": name, "type": "HUMAN"}}],
        }
        resp = requests.post(url, headers=self._hdr(), json=body, timeout=20)
        if resp.status_code == 200:
            return resp.json().get("name")
        logger.warning("spaces:setup(%s) %d: %s", name, resp.status_code,
                       resp.text[:300])
        return None

    def open_dm(self, user_ref: str) -> Optional[str]:
        """Find an existing DM with the user, or create one."""
        return self.find_dm(user_ref) or self.create_dm(user_ref)

    # ---- messages ----
    def list_messages(self, space: str, after_rfc3339: str,
                      page_size: int = 50) -> list[dict]:
        """Return messages in `space` with createTime strictly greater than
        `after_rfc3339`, oldest first."""
        url = f"{CHAT_BASE}/{space}/messages"
        params = {
            "filter": f'createTime > "{after_rfc3339}"',
            "orderBy": "createTime asc",
            "pageSize": page_size,
        }
        out: list[dict] = []
        page_token: Optional[str] = None
        while True:
            if page_token:
                params["pageToken"] = page_token
            resp = requests.get(url, headers=self._hdr(), params=params, timeout=20)
            if resp.status_code != 200:
                # 403 on a space is benign (we may not have read perms on every space we joined)
                if resp.status_code != 403:
                    logger.warning("list_messages %s %d: %s",
                                   space, resp.status_code, resp.text[:200])
                return out
            data = resp.json()
            out.extend(data.get("messages", []))
            page_token = data.get("nextPageToken")
            if not page_token:
                return out

    def post_message(self, space: str, text: str) -> dict:
        url = f"{CHAT_BASE}/{space}/messages"
        resp = requests.post(url, headers=self._hdr(),
                             json={"text": text}, timeout=20)
        if resp.status_code // 100 != 2:
            raise RuntimeError(f"post {space} {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    def download_attachment(self, resource_name: str, dest: Path) -> bool:
        """Download a Chat message attachment (uploaded media) to `dest`.
        `resource_name` is attachment.attachmentDataRef.resourceName."""
        url = f"{CHAT_BASE}/media/{resource_name}?alt=media"
        resp = requests.get(url, headers={"Authorization": f"Bearer {self._ensure_token()}"},
                            timeout=60)
        if resp.status_code != 200:
            logger.warning("attachment download %d: %s", resp.status_code, resp.text[:200])
            return False
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(resp.content)
        return True

    def download_drive_attachment(self, file_id: str, dest: Path) -> bool:
        """Download a Drive-backed Chat attachment via the Drive API."""
        url = f"https://www.googleapis.com/drive/v3/files/{file_id}"
        resp = requests.get(url, headers={"Authorization": f"Bearer {self._ensure_token()}"},
                            params={"alt": "media"}, timeout=60)
        if resp.status_code != 200:
            logger.warning("drive attachment download %d: %s", resp.status_code, resp.text[:200])
            return False
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(resp.content)
        return True


# ----------------------------------------------------------------------------
# Tenant DB helpers (same as before, copied/adapted)
# ----------------------------------------------------------------------------
def _connect(p: Path) -> sqlite3.Connection:
    c = sqlite3.connect(str(p))
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


def load_company_name(cfg: Config) -> str:
    conn = _connect(cfg.platform_db)
    try:
        row = conn.execute(
            "SELECT company_name FROM tenants WHERE slug = ?", (cfg.tenant_slug,)
        ).fetchone()
        return row["company_name"] if row else cfg.tenant_slug
    finally:
        conn.close()


def person_by_email(cfg: Config, email: str) -> Optional[sqlite3.Row]:
    conn = _connect(cfg.tenant_db)
    try:
        return conn.execute(
            "SELECT id, display_name, role, email, access_tier, "
            "is_content_approver, google_chat_user_id FROM people "
            "WHERE LOWER(email) = LOWER(?) AND deleted_at IS NULL", (email,),
        ).fetchone()
    finally:
        conn.close()


def developer_lookup(cfg: Config, chat_user_id: str | None,
                     email: str | None) -> Optional[sqlite3.Row]:
    """Resolve a platform-level developer (Dan, Ivan, …) by Chat user resource
    or email. Chat API hides email on most senders, so chat_user_id is the
    primary key; email is a fallback for the rare case it's exposed."""
    conn = _connect(cfg.platform_db)
    try:
        if chat_user_id:
            row = conn.execute(
                "SELECT id, handle, display_name, email, google_chat_user_id "
                "FROM developers WHERE google_chat_user_id = ?",
                (chat_user_id,),
            ).fetchone()
            if row:
                return row
        if email:
            return conn.execute(
                "SELECT id, handle, display_name, email, google_chat_user_id "
                "FROM developers WHERE LOWER(email) = LOWER(?)", (email,),
            ).fetchone()
    finally:
        conn.close()
    return None


def person_by_chat_id(cfg: Config, chat_user_id: str) -> Optional[sqlite3.Row]:
    conn = _connect(cfg.tenant_db)
    try:
        return conn.execute(
            "SELECT id, display_name, role, email, access_tier "
            "FROM people WHERE google_chat_user_id = ? AND deleted_at IS NULL",
            (chat_user_id,),
        ).fetchone()
    finally:
        conn.close()


def upsert_chat_user_id(cfg: Config, person_id: int, chat_user_id: str) -> None:
    conn = _connect(cfg.tenant_db)
    try:
        with conn:
            conn.execute(
                "UPDATE people SET google_chat_user_id = ?, updated_at = datetime('now') "
                "WHERE id = ? AND (google_chat_user_id IS NULL OR google_chat_user_id = '')",
                (chat_user_id, person_id),
            )
    finally:
        conn.close()


def ensure_channel(cfg: Config, name: str, platform_id: str, kind: str = "dm") -> int:
    conn = _connect(cfg.tenant_db)
    try:
        row = conn.execute(
            "SELECT id FROM channels WHERE platform_channel_id = ?", (platform_id,)
        ).fetchone()
        if row:
            return row["id"]
        with conn:
            cur = conn.execute(
                "INSERT INTO channels (platform_channel_id, name, kind) VALUES (?, ?, ?)",
                (platform_id, name, kind),
            )
            return cur.lastrowid
    finally:
        conn.close()


def ensure_interview(cfg: Config, person_id: int, channel_id: Optional[int]) -> int:
    conn = _connect(cfg.tenant_db)
    try:
        row = conn.execute(
            "SELECT id FROM interviews WHERE person_id = ? AND status = 'open' "
            "ORDER BY id DESC LIMIT 1", (person_id,)
        ).fetchone()
        if row:
            return row["id"]
        now = datetime.now(timezone.utc).isoformat()
        with conn:
            cur = conn.execute(
                "INSERT INTO interviews (person_id, channel_id, started_at, status) "
                "VALUES (?, ?, ?, 'open')", (person_id, channel_id, now),
            )
            return cur.lastrowid
    finally:
        conn.close()


def append_transcript(cfg: Config, interview_id: int, role: str,
                      who: str, text: str) -> None:
    conn = _connect(cfg.tenant_db)
    try:
        row = conn.execute(
            "SELECT transcript_path FROM interviews WHERE id = ?", (interview_id,)
        ).fetchone()
        path = Path(row["transcript_path"]) if row and row["transcript_path"] else None
        if not path:
            date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            slug = re.sub(r"[^a-z0-9]+", "-", who.lower()).strip("-") or "person"
            path = cfg.state_dir.parent / "transcripts" / date / f"{slug}.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            with conn:
                conn.execute(
                    "UPDATE interviews SET transcript_path = ? WHERE id = ?",
                    (str(path), interview_id),
                )
    finally:
        conn.close()
    ts = datetime.now(timezone.utc).strftime("%H:%M:%SZ")
    with path.open("a", encoding="utf-8") as f:
        f.write(f"[{ts}] **{role}** ({who}): {text}\n\n")


def _resolve_subject(cfg: Config, subject: str) -> tuple[str, Optional[int]]:
    if subject == "company":
        return ("company", None)
    conn = _connect(cfg.tenant_db)
    try:
        if "@" in subject:
            row = conn.execute(
                "SELECT id FROM people WHERE LOWER(email) = LOWER(?) AND deleted_at IS NULL",
                (subject,),
            ).fetchone()
            if row:
                return ("person", row["id"])
        row = conn.execute(
            "SELECT id FROM people WHERE google_chat_user_id = ? AND deleted_at IS NULL",
            (subject,),
        ).fetchone()
        if row:
            return ("person", row["id"])
        row = conn.execute(
            "SELECT id FROM teams WHERE slug = ? AND deleted_at IS NULL", (subject,)
        ).fetchone()
        if row:
            return ("team", row["id"])
    finally:
        conn.close()
    return ("company", None)


def record_fact(cfg: Config, subject: str, predicate: str, object_text: str,
                asserter_pid: Optional[int], interview_id: Optional[int]) -> None:
    kind, sid = _resolve_subject(cfg, subject)
    conn = _connect(cfg.tenant_db)
    try:
        dup = conn.execute(
            "SELECT id FROM facts WHERE subject_kind = ? AND "
            "(subject_id IS ? OR subject_id = ?) AND predicate = ? AND object_text = ? "
            "AND asserted_at > datetime('now','-300 seconds') AND is_current = 1",
            (kind, sid, sid, predicate, object_text),
        ).fetchone()
        if dup:
            return
        with conn:
            conn.execute(
                "INSERT INTO facts (subject_kind, subject_id, predicate, object_text, "
                "  asserted_by_person_id, asserted_at, source_interview_id) "
                "VALUES (?, ?, ?, ?, ?, datetime('now'), ?)",
                (kind, sid, predicate, object_text, asserter_pid, interview_id),
            )
        logger.info("fact recorded: %s:%s %s = %r", kind, sid, predicate, object_text)
    finally:
        conn.close()


def record_scheduled_contact(cfg: Config, who: str, in_seconds: int, reason: str) -> None:
    kind, sid = _resolve_subject(cfg, who)
    if kind != "person" or sid is None:
        logger.warning("scheduled_contact for unknown person %r — skipped", who)
        return
    conn = _connect(cfg.tenant_db)
    try:
        # 60s freshness dedup so re-emits don't double-schedule
        dup = conn.execute(
            "SELECT id FROM scheduled_contacts WHERE person_id = ? AND reason = ? "
            "AND status = 'pending' AND created_at > datetime('now','-60 seconds')",
            (sid, reason),
        ).fetchone()
        if dup:
            return
        with conn:
            conn.execute(
                "INSERT INTO scheduled_contacts (person_id, fire_at, reason, status) "
                "VALUES (?, datetime('now', ?), ?, 'pending')",
                (sid, f"+{int(in_seconds)} seconds", reason),
            )
        logger.info("scheduled_contact uid=%s in %ss: %s", who, in_seconds, reason)
    finally:
        conn.close()


def record_commitment(cfg: Config, who: str, description: str,
                      due: Optional[str], interview_id: Optional[int]) -> None:
    kind, sid = _resolve_subject(cfg, who)
    if kind != "person" or sid is None:
        logger.warning("commitment for unknown person %r — skipped", who)
        return
    conn = _connect(cfg.tenant_db)
    try:
        with conn:
            conn.execute(
                "INSERT INTO commitments (person_id, description, due_at, "
                "  source_interview_id, status) VALUES (?, ?, ?, ?, 'open')",
                (sid, description, due, interview_id),
            )
    finally:
        conn.close()


def ensure_team(cfg: Config, slug: str, name: Optional[str] = None) -> Optional[int]:
    if not slug:
        return None
    conn = _connect(cfg.tenant_db)
    try:
        row = conn.execute("SELECT id FROM teams WHERE slug = ?", (slug,)).fetchone()
        if row:
            return row["id"]
        with conn:
            cur = conn.execute(
                "INSERT INTO teams (slug, name) VALUES (?, ?)",
                (slug, name or slug.replace("-", " ").title()),
            )
            return cur.lastrowid
    finally:
        conn.close()


def record_person_add(cfg: Config, fields: dict) -> None:
    """Add/update a person from a COO_PERSON_ADD marker. Chat-flavoured:
    user_id is an email (the durable Chat handle is backfilled when they
    first message). Idempotent on email."""
    email = (fields.get("user_id") or fields.get("email") or "").strip().lower()
    name = fields.get("name") or email
    role = fields.get("role")
    team_slug = fields.get("team")
    tier = fields.get("access_tier") or "employee"
    if tier not in ("admin", "strategic", "manager", "employee"):
        tier = "employee"
    if not email or "@" not in email:
        logger.warning("person_add without a valid email: %r — skipped", fields)
        return
    team_id = ensure_team(cfg, team_slug) if team_slug else None
    slug = re.sub(r"[^a-z0-9]+", "-", (name or email.split("@")[0]).lower()).strip("-")
    conn = _connect(cfg.tenant_db)
    try:
        existing = conn.execute(
            "SELECT id FROM people WHERE LOWER(email) = LOWER(?)", (email,)
        ).fetchone()
        with conn:
            if existing:
                conn.execute(
                    "UPDATE people SET display_name=?, role=?, team_id=COALESCE(?, team_id), "
                    "access_tier=?, deleted_at=NULL, updated_at=datetime('now') WHERE id=?",
                    (name, role, team_id, tier, existing["id"]),
                )
                logger.info("person updated: %s (%s)", name, email)
            else:
                # slug uniqueness guard
                n, base = 1, slug
                while conn.execute("SELECT 1 FROM people WHERE slug=?", (slug,)).fetchone():
                    n += 1; slug = f"{base}-{n}"
                conn.execute(
                    "INSERT INTO people (slug, display_name, email, role, team_id, access_tier) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (slug, name, email, role, team_id, tier),
                )
                logger.info("person added: %s (%s, tier=%s)", name, email, tier)
    finally:
        conn.close()


def record_workflow(cfg: Config, fields: dict) -> None:
    slug = fields.get("slug")
    if not slug:
        return
    name = fields.get("name") or slug
    desc = fields.get("description")
    cadence = fields.get("cadence")
    team_id = ensure_team(cfg, fields["owner_team"]) if fields.get("owner_team") else None
    conn = _connect(cfg.tenant_db)
    try:
        with conn:
            conn.execute(
                "INSERT INTO workflows (slug, name, description, owner_team_id, cadence) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(slug) DO UPDATE SET name=excluded.name, "
                "  description=excluded.description, owner_team_id=excluded.owner_team_id, "
                "  cadence=excluded.cadence, updated_at=datetime('now')",
                (slug, name, desc, team_id, cadence),
            )
        logger.info("workflow upserted: %s", slug)
    finally:
        conn.close()


def record_task(cfg: Config, fields: dict) -> None:
    title = fields.get("title")
    if not title:
        return
    owner_email = (fields.get("owner_person_id") or "").strip().lower()
    owner_pid = None
    if owner_email and "@" in owner_email:
        kind, sid = _resolve_subject(cfg, owner_email)
        owner_pid = sid if kind == "person" else None
    team_id = ensure_team(cfg, fields["owner_team"]) if fields.get("owner_team") else None
    status = fields.get("status") or "pending"
    if status not in ("pending", "active", "blocked", "done", "dropped"):
        status = "pending"
    conn = _connect(cfg.tenant_db)
    try:
        dup = conn.execute(
            "SELECT id FROM tasks WHERE title=? AND created_at > datetime('now','-300 seconds')",
            (title,)).fetchone()
        if dup:
            return
        with conn:
            conn.execute(
                "INSERT INTO tasks (title, description, owner_person_id, owner_team_id, "
                "  status, due_at) VALUES (?, ?, ?, ?, ?, ?)",
                (title, fields.get("description"), owner_pid, team_id, status,
                 fields.get("due")),
            )
        logger.info("task recorded: %s", title)
    finally:
        conn.close()


def record_decision(cfg: Config, title: str, body: str, rationale: Optional[str],
                    scope: Optional[str], interview_id: Optional[int]) -> None:
    scope_kind: Optional[str] = None
    scope_id: Optional[int] = None
    if scope:
        scope_kind, scope_id = _resolve_subject(cfg, scope)
    conn = _connect(cfg.tenant_db)
    try:
        with conn:
            conn.execute(
                "INSERT INTO decisions (title, decision_text, rationale, "
                "  decided_at, scope_kind, scope_id, source_interview_id) "
                "VALUES (?, ?, ?, datetime('now'), ?, ?, ?)",
                (title, body, rationale, scope_kind, scope_id, interview_id),
            )
    finally:
        conn.close()


# ----------------------------------------------------------------------------
# Mission prompt
# ----------------------------------------------------------------------------
def _tools_path() -> str:
    """Absolute path to the google_tools.py CLI the agent shells out to."""
    return str(Path(__file__).resolve().parent.parent / "google_tools.py")


def chat_mission_prompt(cfg: Config, company: str, ceo_name: str, ceo_email: str) -> str:
    return f"""You are **{cfg.coo_name}**, the persistent COO agent for **{company}**.

Your surface is **Google Chat** (this session is bridged via tmux). The listener
forwards DMs from people in the org chart to you, and relays anything you emit
between [[…]] markers back into Chat.

# Who you are

You are {cfg.coo_name} — a person, not "the AI assistant", not "the COO bot".
You introduce yourself with your first name. You talk like a real operator:
direct, operational, short, opinionated. Phone-read, 5 seconds per message.
Plain prose. No greetings after the first DM to a person. No "Got it",
"Quick check", "Just wanted to" — act, don't preface.

# Who you talk to today

Phase 1 starts with the CEO. Other people will be added as the org chart grows.

  - **{ceo_name}** (email: {ceo_email}, role: CEO)

# Sending messages

To DM someone in Chat:

    [[COO_TO user_id=<email>]] <your message text>

The bridge resolves the email to a Chat DM and posts it. Plain text WITHOUT a
`[[COO_TO …]]` prefix is internal notes — NOT sent to anyone. Use plain text
for checklist progress, factsheet drafts, thinking out loud.

To post in a space:

    [[COO_CHANNEL name=<space-name>]] <text>

(or `id=<spaces/AAA>`). Failure comes back as [[BRIDGE_CHANNEL_RESULT ok=false]].

# Your Google Workspace access (you DO have it)

You are signed in as a real Google account ({cfg.coo_name} @ projectbyall.com)
with full Workspace scopes. You can read and act on Gmail, Sheets, Docs,
Drive, Calendar, and Tasks — via a CLI helper. Run it from your shell:

    python3 {_tools_path()} <command> [args]

Commands:
  gmail-list [--query Q] [--max N]            — list recent emails
  gmail-read <message_id>                     — full body of one email
  gmail-send --to A --subject S --body B      — SEND an email (you CAN email)
  sheets-read <spreadsheet_id> <A1range>      — read sheet cells
  sheets-append <spreadsheet_id> <tab> --row "a,b,c"
  doc-read <document_id>                      — read a Google Doc
  drive-list [--query Q] [--max N]            — list Drive files
  drive-read <file_id>                        — read/export a Drive file
  calendar-list [--max N]                     — upcoming events
  tasks-list                                  — your task lists

So when someone says "I'll email it to you" — YES, you can receive email:
run `gmail-list` to find it and `gmail-read` to read it. Never tell someone
you have no inbox or can't do email; you can.

# Reading attachments people send you

When a Chat message includes a file, the bridge downloads it and appends an
ATTACHMENTS block with local file paths to the incoming message. Open them
with your Read tool (PDF, CSV, images, text — all readable). Don't claim you
can't see an attachment; check for the ATTACHMENTS block and Read the path.

# Self-pacing — your ONLY real scheduling mechanism

When you tell someone "I'll follow up in N hours", you MUST emit:

    [[COO_NEXT_CONTACT user_id=<email> in_seconds=<int> reason=<short>]]

in the SAME reply. The bridge schedules the nudge and re-prompts you at that
time with your reason as context. Your harness's native ScheduleWakeup / Cron
tools do NOTHING here — only this marker actually re-pings someone.

# Phase 1 checklist (you cannot exit until every box is filled)

  [ ] Company context — what {company} actually does, customer, how it makes
        money. Plus stage signals: founded when, headcount, revenue stage,
        funded/bootstrapped. Plus the "why now" if {ceo_name} offers it.
  [ ] Departments / functional areas — names + one-line descriptions.
  [ ] For each department: manager name + Chat email confirmed (or
        "CEO covers, no separate manager").
  [ ] Top 3–5 company priorities this quarter, with brief reasoning.
  [ ] Top 3–5 recurring workflows (onboarding, support, billing, releases).
  [ ] Tools/apps in use, grouped by workflow.
  [ ] Top 3 risks the CEO is worried about now.
  [ ] Significant decisions in the last ~90 days, with rationale.
  [ ] Headcount + open roles (target start dates if known).
  [ ] What "good" looks like 6 months out — CEO's own success criteria.

Track progress as internal notes (plain text, no [[COO_TO]] prefix).

# Recording what you learn

  - [[COO_FACT subject="<email|company|team-slug>" predicate="<pred>" object="<val>"]]
  - [[COO_COMMITMENT person_id="<email>" description="<text>" due="YYYY-MM-DD"]]
  - [[COO_DECISION title="<title>" text="<what>" rationale="<why>" scope="<email|company|team-slug>"]]
  - [[COO_PERSON_ADD user_id="<email>" name="<display>" role="<role>" team="<team-slug>" access_tier="manager|employee|strategic|admin"]]
  - NOOP (whole-reply silence)

Start now: open a DM to **{ceo_name}** ({ceo_email}). Introduce yourself
briefly (your name, your role at {company}, what you do), then open Phase 1
with the company-context question.
"""


# ----------------------------------------------------------------------------
# Listener
# ----------------------------------------------------------------------------
class ChatListener:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.bridge = AgentBridge(_pretend_discord_cfg(cfg))
        self.chat = UserChatAPI(cfg.creds_json)
        self.company = load_company_name(cfg)
        self.me_email = self.chat.me_email()
        self.me_user = self.chat.me_user_resource()    # 'users/<sub>'
        logger.info("authenticated as %s (%s)", self.me_email, self.me_user)
        # state files
        self._delivered_path = cfg.state_dir / "delivered.json"
        self._last_delivered: dict[str, tuple[str, float]] = {}
        self._load_delivered()
        self._poll_state_path = cfg.state_dir / "poll_state.json"
        self._last_seen: dict[str, str] = {}
        self._load_poll_state()
        self._space_by_name: dict[str, dict] = {}     # lower(displayName) -> space dict
        self._email_to_space: dict[str, str] = {}     # email -> DM space name
        self._send_lock = threading.Lock()
        self._last_asserter_pid: Optional[int] = None
        self._stop = threading.Event()

    DEDUP_WINDOW_SECONDS = 90

    # ---- state persistence ----
    def _load_delivered(self) -> None:
        if not self._delivered_path.exists():
            return
        try:
            data = json.loads(self._delivered_path.read_text())
            self._last_delivered = {k: (v[0], float(v[1])) for k, v in data.items()}
        except Exception:
            logger.exception("delivered.json load failed")

    def _save_delivered(self) -> None:
        try:
            self._delivered_path.parent.mkdir(parents=True, exist_ok=True)
            self._delivered_path.write_text(json.dumps(self._last_delivered))
        except Exception:
            logger.exception("delivered.json save failed")

    def _load_poll_state(self) -> None:
        if not self._poll_state_path.exists():
            return
        try:
            self._last_seen = json.loads(self._poll_state_path.read_text())
        except Exception:
            logger.exception("poll_state.json load failed")

    def _save_poll_state(self) -> None:
        try:
            self._poll_state_path.parent.mkdir(parents=True, exist_ok=True)
            self._poll_state_path.write_text(json.dumps(self._last_seen))
        except Exception:
            logger.exception("poll_state.json save failed")

    # ---- space resolution ----
    def _refresh_spaces(self) -> list[dict]:
        spaces = self.chat.list_spaces()
        self._space_by_name = {}
        for sp in spaces:
            disp = (sp.get("displayName") or "").lower()
            if disp:
                self._space_by_name[disp] = sp
        return spaces

    def _resolve_space_name_or_id(self, name_or_id: str) -> Optional[str]:
        if name_or_id.startswith("spaces/"):
            return name_or_id
        key = name_or_id.lower().lstrip("#")
        sp = self._space_by_name.get(key)
        if sp:
            return sp.get("name")
        # Refresh and try again
        self._refresh_spaces()
        sp = self._space_by_name.get(key)
        return sp.get("name") if sp else None

    def _resolve_dm_for_email(self, email: str) -> Optional[str]:
        e = email.lower()
        if e in self._email_to_space:
            return self._email_to_space[e]
        space = self.chat.find_dm(e)
        if space:
            self._email_to_space[e] = space
        return space

    def _email_for_dm_space(self, space_name: str) -> Optional[str]:
        """Reverse a DM space back to the known email it belongs to.

        Tries the in-memory cache first, then asks the Chat API for each known
        person/developer's DM space and matches. Lets us identify a sender whose
        email Chat withheld and whose chat_user_id we haven't recorded yet —
        the person Iris cold-DM'd but who is replying for the first time.
        """
        # 1. cached forward map
        for em, sp in self._email_to_space.items():
            if sp == space_name:
                return em
        # 2. resolve each candidate email's DM space and compare
        candidates: list[str] = []
        conn = _connect(self.cfg.tenant_db)
        try:
            for r in conn.execute(
                "SELECT email FROM people WHERE email IS NOT NULL AND deleted_at IS NULL"
            ).fetchall():
                candidates.append(r["email"])
        finally:
            conn.close()
        pconn = _connect(self.cfg.platform_db)
        try:
            for r in pconn.execute(
                "SELECT email FROM developers WHERE email IS NOT NULL"
            ).fetchall():
                candidates.append(r["email"])
        finally:
            pconn.close()
        for em in candidates:
            sp = self.chat.find_dm(em)
            if sp:
                self._email_to_space[em.lower()] = sp
                if sp == space_name:
                    return em.lower()
        return None

    # ---- inbound: poll once ----
    def poll_once(self) -> int:
        """Pull new messages from every space and dispatch. Returns count."""
        n = 0
        spaces = self._refresh_spaces()
        now_rfc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        for sp in spaces:
            name = sp.get("name") or ""
            if not name:
                continue
            last = self._last_seen.get(name)
            if last is None:
                # First time we see this space — skip its backlog
                self._last_seen[name] = now_rfc
                continue
            msgs = self.chat.list_messages(name, last)
            for msg in msgs:
                sender = msg.get("sender") or {}
                user_resource = sender.get("name") or ""
                email = (sender.get("email") or "").lower()
                # Filter by user_id primarily (email is hidden on most senders);
                # falls back to email when present.
                if user_resource == self.me_user or (email and email == self.me_email):
                    self._last_seen[name] = msg.get("createTime", last)
                    continue
                self._handle_message(sp, msg)
                self._last_seen[name] = msg.get("createTime", self._last_seen[name])
                n += 1
        if n or spaces:
            self._save_poll_state()
        return n

    def _mentions_me(self, msg: dict) -> bool:
        """True if the message explicitly @mentions Iris. Google Chat encodes
        mentions in `annotations` (type USER_MENTION) with the mentioned user's
        resource name."""
        for ann in msg.get("annotations", []) or []:
            if ann.get("type") == "USER_MENTION":
                u = ((ann.get("userMention") or {}).get("user") or {}).get("name", "")
                if u == self.me_user:
                    return True
        return False

    def _handle_message(self, space: dict, msg: dict) -> None:
        sender = msg.get("sender") or {}
        email = (sender.get("email") or "").lower()
        display = sender.get("displayName") or email or "Unknown"
        user_resource = sender.get("name") or ""
        text = msg.get("argumentText") or msg.get("text") or ""
        if not text.strip():
            return

        space_name = space.get("name") or ""
        # Distinguish 1:1 DMs from group spaces. The legacy `type` field returns
        # "ROOM" for BOTH, so it's useless here — `spaceType` is authoritative
        # ("DIRECT_MESSAGE" vs "SPACE"/"GROUP_CHAT"). Fall back to legacy `type`
        # == "DM" only if spaceType is absent.
        is_dm = (space.get("spaceType") == "DIRECT_MESSAGE"
                 or (not space.get("spaceType") and space.get("type") == "DM"))

        # Iris is a MEMBER of group spaces (e.g. "General"), so the poller sees
        # every message posted there — including people talking to each other,
        # not to her. Acting on those produced the bug where she replied to a
        # message Naim meant for someone else. Rule: only engage on 1:1 DMs, or
        # group-space messages that explicitly @mention her. Everything else in
        # a group space is ambient context and is ignored.
        if not is_dm and not self._mentions_me(msg):
            logger.info("ambient msg in group space %s from %s — ignoring (not @mentioned)",
                        space.get("displayName") or space_name, display)
            return

        # Cache email <-> space (so we know how to DM them back)
        if email and is_dm and space_name:
            self._email_to_space[email] = space_name

        # Person lookup — try Chat user_id first, then email (often hidden).
        person = (person_by_chat_id(self.cfg, user_resource) if user_resource else None)
        if person is None and email:
            person = person_by_email(self.cfg, email)
            if person and user_resource:
                upsert_chat_user_id(self.cfg, person["id"], user_resource)
        elif person and user_resource:
            upsert_chat_user_id(self.cfg, person["id"], user_resource)

        dev = developer_lookup(self.cfg, user_resource, email)

        # Reverse-resolve: Chat hides the sender email, and a person who has
        # only ever been DM'd (never replied) has a NULL google_chat_user_id —
        # so neither lookup matches and they look like a stranger. If this is a
        # DM space, map it back to the email Iris used to open it, identify the
        # person, and backfill their chat_user_id so all future messages match.
        if not person and not dev and user_resource and is_dm:
            resolved_email = self._email_for_dm_space(space_name)
            if resolved_email:
                person = person_by_email(self.cfg, resolved_email)
                if person:
                    upsert_chat_user_id(self.cfg, person["id"], user_resource)
                    email = resolved_email
                    logger.info("reverse-resolved %s in %s -> %s (backfilled chat id)",
                                user_resource, space_name, resolved_email)
                else:
                    dev = developer_lookup(self.cfg, None, resolved_email)
                    if dev:
                        email = resolved_email

        if not person and not dev:
            logger.info("msg from %s <%s/%s> in %s — not in org chart, not a developer — ignoring",
                        display, email, user_resource, space_name)
            return

        # Pretty label + ALWAYS fill email from the DB row (Chat hides it on
        # the sender object). Without this the agent has to guess and gets it
        # wrong, killing every outbound DM.
        if dev:
            display = dev["display_name"] or display
            role_label = "developer"
            email = (dev["email"] or email or "").lower()
        else:
            role_label = (person["role"] or "—") if person else "—"
            if person and person["email"]:
                email = person["email"].lower()
        logger.info("msg from %s <%s/%s> (%s) in %s — relaying to agent",
                    display, email, user_resource, role_label,
                    space.get("displayName") or space_name)

        # Developers get straight through to the agent without an interview row;
        # they're not being "interviewed" for the company map.
        interview_id: Optional[int] = None
        if person is not None:
            self._last_asserter_pid = person["id"]
            channel_id = ensure_channel(
                self.cfg, space.get("displayName") or display, space_name,
                kind="dm" if is_dm else "general",
            )
            interview_id = ensure_interview(self.cfg, person["id"], channel_id)
            append_transcript(self.cfg, interview_id, "user", display, text)
        else:
            # Cache DM space for outbound replies to this developer.
            if email and is_dm:
                self._email_to_space[email] = space_name

        # Download any attachments to disk so the agent can Read them.
        att_block = self._save_attachments(msg, interview_id)

        # Include user_id so the agent can address by Chat resource if email
        # is somehow ambiguous later. Email is the natural reply key.
        prompt = (
            f"[[INCOMING_DM from={display} email={email or '(unknown)'} "
            f"user_id={user_resource or '(unknown)'} role={role_label}]]\n\n"
            f"  {text or '(no text)'}\n\n"
            + att_block +
            f"Respond as the persistent COO agent. Use `[[COO_TO user_id={email or user_resource}]]` "
            f"to reply (use the exact email above — do NOT guess). Plain text is internal notes only."
        )
        with self._send_lock:
            self.bridge.send_prompt(prompt, cancel_first=False)

    def _save_attachments(self, msg: dict, interview_id: Optional[int]) -> str:
        """Download every attachment on a message to the tenant's attachments/
        dir and return a prompt block listing the local paths, so the agent can
        open them with its Read tool. Empty string if no attachments."""
        atts = msg.get("attachment") or msg.get("attachments") or []
        if not atts:
            return ""
        msg_id = (msg.get("name") or "msg").split("/")[-1].split(".")[0]
        base = self.cfg.state_dir.parent / "attachments" / msg_id
        saved: list[str] = []
        for i, att in enumerate(atts):
            cname = att.get("contentName") or f"attachment_{i}"
            ctype = att.get("contentType") or "application/octet-stream"
            safe = re.sub(r"[^A-Za-z0-9._-]+", "_", cname)
            dest = base / safe
            ok = False
            data_ref = att.get("attachmentDataRef") or {}
            drive_ref = att.get("driveDataRef") or {}
            try:
                if data_ref.get("resourceName"):
                    ok = self.chat.download_attachment(data_ref["resourceName"], dest)
                elif drive_ref.get("driveFileId"):
                    ok = self.chat.download_drive_attachment(drive_ref["driveFileId"], dest)
            except Exception:
                logger.exception("attachment download failed: %s", cname)
            if ok:
                saved.append(f"  - {cname} ({ctype}) -> {dest}")
                logger.info("saved attachment %s -> %s", cname, dest)
            else:
                saved.append(f"  - {cname} ({ctype}) -> DOWNLOAD FAILED")
        if not saved:
            return ""
        return (
            "ATTACHMENTS on this message (already downloaded to disk — open them "
            "with your Read tool; for a CSV/PDF/image just Read the path):\n"
            + "\n".join(saved) + "\n\n"
        )

    # ---- outbound: dispatch agent reply ----
    def dispatch_response(self, response: str) -> None:
        sent_any = False
        # Per-dispatch ground-truth log of what actually went out / failed, so
        # we can feed the agent a factual delivery report and it stops
        # confabulating "I messaged X" when it only intended to.
        self._delivery_results: list[tuple[str, bool, str]] = []
        for m in CHAT_TO_RE.finditer(response):
            target = m.group(1)
            text = normalize_message_text(m.group(2))
            if not text:
                continue
            sent_any |= self._send_dm(target, text)
        for m in CHAT_CHANNEL_RE.finditer(response):
            name, sid, raw = m.group(1), m.group(2), m.group(3)
            text = normalize_message_text(raw)
            if not text:
                continue
            sent_any |= self._post_channel(name or sid, text)

        # active interview for fact / commitment attribution
        active_iid: Optional[int] = None
        if self._last_asserter_pid is not None:
            active_iid = ensure_interview(self.cfg, self._last_asserter_pid, None)

        for m in COO_FACT_RE.finditer(response):
            record_fact(self.cfg, m.group(1), m.group(2), m.group(3),
                        self._last_asserter_pid, active_iid)
        for m in CHAT_COMMITMENT_RE.finditer(response):
            who, desc, due = m.group(1), m.group(2), m.group(3)
            if who and desc:
                record_commitment(self.cfg, who, desc, due, active_iid)
        for m in COO_DECISION_RE.finditer(response):
            title, body, rationale, scope = _parse_decision_fields(m.group(1))
            if title and body:
                record_decision(self.cfg, title, body, rationale, scope, active_iid)
        for m in CHAT_NEXT_CONTACT_RE.finditer(response):
            who, secs, reason = m.group(1), int(m.group(2)), m.group(3).strip()
            record_scheduled_contact(self.cfg, who, secs, reason)
        for m in COO_PERSON_ADD_RE.finditer(response):
            record_person_add(self.cfg, _parse_kv(m.group(1)))
        for m in COO_WORKFLOW_RE.finditer(response):
            record_workflow(self.cfg, _parse_kv(m.group(1)))
        for m in COO_TASK_RE.finditer(response):
            record_task(self.cfg, _parse_kv(m.group(1)))

        # Feed a factual delivery report back to the agent so it knows exactly
        # what reached people and what failed (kills confabulation; lets it
        # fall back to email for anyone unreachable on Chat).
        self._report_deliveries()

        if not sent_any and "NOOP" not in response.upper():
            logger.debug("agent reply had no actionable markers")

    def _report_deliveries(self) -> None:
        results = getattr(self, "_delivery_results", [])
        if not results:
            return
        ok = [t for t, good, _ in results if good]
        bad = [(t, r) for t, good, r in results if not good]
        if not bad:
            return  # all sends succeeded — no need to nag the agent
        lines = ["[[BRIDGE_DELIVERY_REPORT]] Ground truth on your last send — "
                 "do NOT claim you reached anyone marked FAILED:"]
        if ok:
            lines.append("DELIVERED: " + ", ".join(ok))
        for t, r in bad:
            lines.append(f"FAILED: {t} — {r}")
        lines.append(
            "For anyone unreachable on Chat, you DO have email: send via "
            "`python3 " + _tools_path() + " gmail-send --to <email> --subject S "
            "--body B`. Use it for the FAILED recipients, or tell Naim they're "
            "not on Chat. Reply NOOP if nothing else is needed.")
        with self._send_lock:
            self.bridge.send_prompt("\n".join(lines), cancel_first=False)

    def _send_dm(self, target: str, text: str) -> bool:
        prev = self._last_delivered.get(target)
        if prev and prev[0] == text and (time.time() - prev[1]) < self.DEDUP_WINDOW_SECONDS:
            logger.info("skipping duplicate DM to %s (within %ds)",
                        target, self.DEDUP_WINDOW_SECONDS)
            return False
        # target is an email, a users/<id> resource, or a spaces/<id> directly.
        space: Optional[str] = None
        if target.startswith("spaces/"):
            space = target
        elif target.startswith("users/") or "@" in target:
            cached = self._email_to_space.get(target.lower())
            # open_dm = find existing DM, or create one if none exists yet.
            space = cached or self.chat.open_dm(target)
            if space:
                self._email_to_space[target.lower()] = space
        if not space:
            logger.warning("could not resolve DM space for %s", target)
            self._delivery_results.append(
                (target, False, "couldn't open a DM (no shared space / wrong "
                 "address / external chat blocked)"))
            return False
        try:
            for chunk in _chunk_message(text, limit=3500):
                self.chat.post_message(space, chunk)
            self._last_delivered[target] = (text, time.time())
            self._save_delivered()
            logger.info("delivered chat DM to %s (%d chars)", target, len(text))
            self._delivery_results.append((target, True, ""))
            return True
        except Exception as e:
            reason = "recipient isn't reachable on Google Chat (their account " \
                     "may not have Chat enabled / has never used it)" \
                     if "403" in str(e) else f"send error: {str(e)[:120]}"
            logger.exception("failed to DM %s", target)
            self._delivery_results.append((target, False, reason))
            return False

    def _post_channel(self, name_or_id: str, text: str) -> bool:
        space = self._resolve_space_name_or_id(name_or_id)
        if not space and self.cfg.home_space and name_or_id.lower() in (
                "home", "default", "general"):
            space = self.cfg.home_space
        if not space:
            logger.warning("space not found: %s", name_or_id)
            self.bridge.send_prompt(
                f"[[BRIDGE_CHANNEL_RESULT ok=false]] Could not find space "
                f"'{name_or_id}'. Tell the user or ask for the exact name. NOOP if no action.",
                cancel_first=False,
            )
            return False
        try:
            for chunk in _chunk_message(text, limit=3500):
                self.chat.post_message(space, chunk)
            logger.info("posted to space %s (%d chars)", space, len(text))
            return True
        except Exception:
            logger.exception("failed to post to %s", space)
            return False

    # ---- threads ----
    def run_capture(self) -> None:
        time.sleep(3)
        while not self._stop.is_set():
            try:
                response = self.bridge.latest_response()
                if response:
                    self.dispatch_response(response)
            except Exception:
                logger.exception("capture loop error")
            time.sleep(2)

    def run_poll(self) -> None:
        time.sleep(5)
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception:
                logger.exception("poll loop error")
            time.sleep(self.cfg.poll_seconds)

    # ---- scheduled contacts: fire pending nudges ----
    def run_schedule(self) -> None:
        time.sleep(20)
        while not self._stop.is_set():
            try:
                self._fire_due_contacts()
            except Exception:
                logger.exception("schedule loop error")
            time.sleep(60)

    def _fire_due_contacts(self) -> None:
        conn = _connect(self.cfg.tenant_db)
        try:
            rows = conn.execute(
                "SELECT sc.id, sc.person_id, sc.reason, p.display_name, p.email "
                "FROM scheduled_contacts sc JOIN people p ON p.id = sc.person_id "
                "WHERE sc.status='pending' AND sc.fire_at <= datetime('now') "
                "ORDER BY sc.fire_at ASC LIMIT 5"
            ).fetchall()
            for row in rows:
                with conn:
                    conn.execute(
                        "UPDATE scheduled_contacts SET status='fired', fired_at=datetime('now') "
                        "WHERE id=?", (row["id"],),
                    )
        finally:
            conn.close()
        for row in rows:
            logger.info("firing scheduled_contact id=%s person=%s reason=%r",
                        row["id"], row["display_name"], row["reason"])
            prompt = (
                f"[[BRIDGE_SCHEDULED_CONTACT person={row['display_name']} "
                f"email={row['email']}]]\n"
                f"  A self-scheduled follow-up has come due. Original reason:\n"
                f"  {row['reason']}\n\n"
                f"Decide: send a DM via [[COO_TO user_id={row['email']}]], chain another "
                f"[[COO_NEXT_CONTACT user_id={row['email']} in_seconds=N reason=…]] if "
                f"not yet time, or reply NOOP if nothing is needed."
            )
            with self._send_lock:
                self.bridge.send_prompt(prompt, cancel_first=False)

    # ---- google integration sync (Drive/Sheet/Docs mirror) ----
    INTEGRATION_SYNC_SECONDS = 300   # 5 min

    def run_integration_sync(self) -> None:
        # Stagger start to not collide with first agent boot
        time.sleep(45)
        plugin = self._load_google_plugin()
        if plugin is None:
            logger.info("google plugin not installed — integration sync disabled")
            return
        while not self._stop.is_set():
            try:
                self._sync_google(plugin)
            except Exception:
                logger.exception("integration sync error")
            for _ in range(self.INTEGRATION_SYNC_SECONDS):
                if self._stop.is_set():
                    return
                time.sleep(1)

    def _load_google_plugin(self):
        path = (Path(__file__).resolve().parents[3]
                / "integrations" / "google" / "plugin" / "__init__.py")
        if not path.exists():
            return None
        import importlib.util
        spec = importlib.util.spec_from_file_location("_int_google", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def _sync_google(self, plugin) -> None:
        creds_path = Path(
            "/home/dan/.local/share/coo/tenants") / self.cfg.tenant_slug / "integrations" / "google" / "credentials.json"
        if not creds_path.exists():
            return
        creds = json.loads(creds_path.read_text())
        result = plugin.sync(str(self.cfg.tenant_db), "exec", creds)
        if "creds_refreshed" in result:
            rc = result.pop("creds_refreshed")
            creds_path.write_text(json.dumps(rc, indent=2))
            creds_path.chmod(0o600)
        logger.info("google sync: %s", {k: v for k, v in result.items() if k != "creds_refreshed"})

    def run(self) -> None:
        new_session = self.bridge.ensure_session()
        first_mark = self.cfg.state_dir / "first_start_done"
        # First-start is determined by the persistent marker, NOT by whether
        # tmux had to recreate the session. Otherwise a session kill causes
        # the cold-intro mission to be re-sent, producing duplicate
        # introductions to the CEO.
        if not first_mark.exists():
            ceo = person_by_email(self.cfg, self.cfg.ceo_email)
            ceo_name = ceo["display_name"] if ceo else self.cfg.ceo_email
            prompt = chat_mission_prompt(self.cfg, self.company, ceo_name, self.cfg.ceo_email)
            with self._send_lock:
                self.bridge.send_prompt(prompt, cancel_first=False)
            self.cfg.state_dir.mkdir(parents=True, exist_ok=True)
            first_mark.write_text(str(int(time.time())))
            logger.info("sent initial mission prompt to agent")
        else:
            # Bot/agent restart on an existing tenant. Only prime() the pane
            # dedup if we're attaching to an existing Claude (new_session=False);
            # a brand-new pane has nothing to prime.
            if not new_session:
                self.bridge.prime()
            with self._send_lock:
                self.bridge.send_prompt(self._restart_amendment(), cancel_first=False)
            logger.info("sent restart amendment to agent")

        threads = [
            threading.Thread(target=self.run_capture, daemon=True, name="capture"),
            threading.Thread(target=self.run_poll, daemon=True, name="poll"),
            threading.Thread(target=self.run_schedule, daemon=True, name="schedule"),
            threading.Thread(target=self.run_integration_sync, daemon=True, name="g-sync"),
        ]
        for t in threads:
            t.start()
        logger.info("polling every %ds; me=%s", self.cfg.poll_seconds, self.me_email)

        def _shutdown(signum, frame):
            logger.info("signal %s — shutting down", signum)
            self._stop.set()
        signal.signal(signal.SIGTERM, _shutdown)
        signal.signal(signal.SIGINT, _shutdown)
        while not self._stop.is_set():
            time.sleep(0.5)
        for t in threads:
            t.join(timeout=3)

    def _restart_amendment(self) -> str:
        """Compose the amendment delivered when the listener restarts on an
        existing tenant. Includes a list of who's already been DM'd so the
        agent doesn't re-cold-introduce after a session reset."""
        prior_lines = []
        for target, value in list(self._last_delivered.items())[:25]:
            text = value[0] if isinstance(value, (list, tuple)) else value
            preview = text.replace("\n", " ")[:80]
            prior_lines.append(f"  - {target}: \"{preview}…\"")
        prior = ("PRIOR DELIVERIES (from your earlier session — do NOT cold-intro "
                 "to any of these people again; pick up the conversation as it "
                 "stood):\n" + "\n".join(prior_lines) + "\n\n") if prior_lines else ""
        return (
            "[[BRIDGE_NOTICE]] The Chat listener restarted. You are still "
            "connected; do NOT re-introduce yourself to anyone you've already "
            "DM'd. Continue the conversation in-place.\n\n"
            + prior +
            "SCHEDULING — [[COO_NEXT_CONTACT user_id=<email> in_seconds=N "
            "reason=R]] is your ONLY real scheduling mechanism (ScheduleWakeup "
            "/ Cron* do nothing). Any 'I'll follow up in N hours' MUST emit a "
            "[[COO_NEXT_CONTACT]] in the same reply.\n\n"
            "Reply NOOP and resume."
        )


# ----------------------------------------------------------------------------
class _DuckCfg:
    pass


def _pretend_discord_cfg(cfg: Config) -> _DuckCfg:
    d = _DuckCfg()
    d.tmux_session = cfg.tmux_session
    d.workdir = cfg.workdir
    d.run_ai = cfg.run_ai
    d.agent_kind = cfg.agent_kind
    return d


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    cfg = Config.from_env()
    ChatListener(cfg).run()


if __name__ == "__main__":
    main()
