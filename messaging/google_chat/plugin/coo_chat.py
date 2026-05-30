"""Google Chat listener for the COO agent — parallel to coo_phase1.py.

Pulls events from a Pub/Sub subscription (where the Chat API publishes every
DM / space event for the configured Chat app), forwards user messages to the
agent through the shared tmux bridge, captures the agent's reply, parses its
[[COO_*]] markers, and dispatches them either OUT to Chat (DMs via Chat REST
API, channel posts to spaces) or INTO the tenant DB.

Auth: a service account granted Pub/Sub Subscriber + chat.bot/chat.messages
scopes. JSON key path in env COO_CHAT_SA_JSON.

Per-tenant env (set by systemd from the tenant's secrets.env):
  COO_TENANT_SLUG, COO_TENANT_DB, COO_PLATFORM_DB, COO_STATE_DIR, COO_WORKDIR,
  COO_TMUX_SESSION, COO_RUN_AI, COO_AGENT_KIND,
  COO_CHAT_PROJECT_ID, COO_CHAT_SUBSCRIPTION, COO_CHAT_SA_JSON,
  COO_CHAT_HOME_SPACE, COO_CHAT_CEO_EMAIL, COO_CHAT_COO_DISPLAY_NAME
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
from concurrent.futures import TimeoutError as FuturesTimeoutError
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
from google.api_core import exceptions as g_exc
from google.cloud import pubsub_v1
from google.oauth2 import service_account
from google.auth.transport.requests import Request as GAuthRequest

# Pull the messaging-agnostic bits from the Discord plugin so we don't fork them.
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from messaging.discord.plugin.coo_phase1 import (  # noqa: E402
    AgentBridge,
    normalize_message_text,
    _chunk_message,
    _parse_kv,
    _parse_decision_fields,
    COO_FACT_RE, COO_COMMITMENT_RE, COO_DECISION_RE, COO_WORKFLOW_RE,
    COO_TASK_RE, COO_REPORT_RE, COO_NEXT_CONTACT_RE, COO_CLOSE_RE,
    COO_CLOSE_USER_RE, COO_PERSON_ADD_RE, COO_INBOX_HANDLE_RE,
    COO_APP_ACTION_RE, COO_HTTP_CALL_RE, NOOP_RE,
)

logger = logging.getLogger("coo_chat")

# ----------------------------------------------------------------------------
# Chat-flavoured markers
# Discord's COO_TO_RE expects a numeric user_id. In Chat, the value is the
# user's email (preferred — durable & readable) or `users/<id>` resource.
# ----------------------------------------------------------------------------
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


# ----------------------------------------------------------------------------
# Config from env
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

    project_id: str
    subscription: str
    sa_json: Path
    home_space: str
    ceo_email: str
    coo_name: str

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
            project_id=req("COO_CHAT_PROJECT_ID"),
            subscription=req("COO_CHAT_SUBSCRIPTION"),
            sa_json=Path(req("COO_CHAT_SA_JSON")),
            home_space=req("COO_CHAT_HOME_SPACE"),
            ceo_email=req("COO_CHAT_CEO_EMAIL"),
            coo_name=os.environ.get("COO_CHAT_COO_DISPLAY_NAME", "Iris"),
        )


# ----------------------------------------------------------------------------
# Chat REST client (thin)
# ----------------------------------------------------------------------------
class ChatAPI:
    BASE = "https://chat.googleapis.com/v1"
    SCOPES = [
        "https://www.googleapis.com/auth/chat.bot",
        "https://www.googleapis.com/auth/chat.messages",
        "https://www.googleapis.com/auth/chat.spaces",
    ]

    def __init__(self, sa_json: Path):
        self.creds = service_account.Credentials.from_service_account_file(
            str(sa_json), scopes=self.SCOPES,
        )
        self._lock = threading.Lock()

    def _token(self) -> str:
        with self._lock:
            if not self.creds.valid:
                self.creds.refresh(GAuthRequest())
            return self.creds.token

    def _hdr(self) -> dict:
        return {"Authorization": f"Bearer {self._token()}",
                "Content-Type": "application/json"}

    def post_message(self, space: str, text: str, *, thread_key: str | None = None) -> dict:
        """Post a message to a space (`spaces/AAA`). Returns the created message."""
        url = f"{self.BASE}/{space}/messages"
        body: dict = {"text": text}
        if thread_key:
            body["thread"] = {"threadKey": thread_key}
        resp = requests.post(url, headers=self._hdr(), json=body, timeout=20)
        if resp.status_code // 100 != 2:
            raise RuntimeError(f"Chat post {space} failed ({resp.status_code}): {resp.text[:400]}")
        return resp.json()

    def find_dm(self, user_resource_or_email: str) -> Optional[str]:
        """Resolve a DM space name for a user. Accepts `users/<id>` or an email.
        Returns the space name (`spaces/AAA`) or None if not found."""
        # findDirectMessage expects ?name=users/<user>
        if "@" in user_resource_or_email and not user_resource_or_email.startswith("users/"):
            user = f"users/{user_resource_or_email}"
        else:
            user = user_resource_or_email
            if not user.startswith("users/"):
                user = f"users/{user}"
        url = f"{self.BASE}/spaces/findDirectMessage"
        resp = requests.get(url, headers=self._hdr(),
                            params={"name": user}, timeout=20)
        if resp.status_code == 200:
            return resp.json().get("name")
        if resp.status_code == 404:
            return None
        logger.warning("findDirectMessage(%s) %d: %s", user, resp.status_code, resp.text[:200])
        return None

    def list_spaces(self) -> list[dict]:
        """List spaces the bot is a member of. Used for resolving channel posts
        by name."""
        url = f"{self.BASE}/spaces"
        resp = requests.get(url, headers=self._hdr(), timeout=20)
        if resp.status_code // 100 != 2:
            logger.warning("list spaces %d: %s", resp.status_code, resp.text[:200])
            return []
        return resp.json().get("spaces", [])


# ----------------------------------------------------------------------------
# Tenant DB helpers — minimal, mirror what discord's COOBot does
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


def person_by_chat_id(cfg: Config, chat_user_id: str) -> Optional[sqlite3.Row]:
    conn = _connect(cfg.tenant_db)
    try:
        return conn.execute(
            "SELECT id, display_name, role, email, access_tier, is_content_approver "
            "FROM people WHERE google_chat_user_id = ? AND deleted_at IS NULL",
            (chat_user_id,),
        ).fetchone()
    finally:
        conn.close()


def person_by_email(cfg: Config, email: str) -> Optional[sqlite3.Row]:
    conn = _connect(cfg.tenant_db)
    try:
        return conn.execute(
            "SELECT id, display_name, role, email, access_tier, is_content_approver, "
            "       google_chat_user_id "
            "FROM people WHERE LOWER(email) = LOWER(?) AND deleted_at IS NULL",
            (email,),
        ).fetchone()
    finally:
        conn.close()


def upsert_chat_user_id(cfg: Config, person_id: int, chat_user_id: str) -> None:
    """Backfill a person's google_chat_user_id once we see them on Chat."""
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


def ensure_channel(cfg: Config, space_name: str, platform_id: str, kind: str = "dm") -> int:
    """Idempotent: row in channels for this space."""
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
                (platform_id, space_name, kind),
            )
            return cur.lastrowid
    finally:
        conn.close()


def ensure_interview(cfg: Config, person_id: int, channel_id: int | None) -> int:
    """Return the open interview id for this person (create if none)."""
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
    """Append a line to the interview's transcript file on disk."""
    conn = _connect(cfg.tenant_db)
    try:
        row = conn.execute(
            "SELECT transcript_path FROM interviews WHERE id = ?", (interview_id,)
        ).fetchone()
        path = Path(row["transcript_path"]) if row and row["transcript_path"] else None
        if not path:
            date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            who_slug = re.sub(r"[^a-z0-9]+", "-", who.lower()).strip("-") or "person"
            path = cfg.state_dir.parent / "transcripts" / date / f"{who_slug}.md"
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


def record_fact(cfg: Config, subject: str, predicate: str, object_text: str,
                asserter_pid: Optional[int], interview_id: Optional[int]) -> None:
    """Insert a fact with 300s freshness dedup on (subject, predicate, object)."""
    subj_kind, subj_id = _resolve_subject(cfg, subject)
    conn = _connect(cfg.tenant_db)
    try:
        dup = conn.execute(
            "SELECT id FROM facts WHERE subject_kind = ? AND "
            "       (subject_id IS ? OR subject_id = ?) AND "
            "       predicate = ? AND object_text = ? AND "
            "       asserted_at > datetime('now', '-300 seconds') AND is_current = 1",
            (subj_kind, subj_id, subj_id, predicate, object_text),
        ).fetchone()
        if dup:
            return
        with conn:
            conn.execute(
                "INSERT INTO facts (subject_kind, subject_id, predicate, object_text, "
                "  asserted_by_person_id, asserted_at, source_interview_id) "
                "VALUES (?, ?, ?, ?, ?, datetime('now'), ?)",
                (subj_kind, subj_id, predicate, object_text, asserter_pid, interview_id),
            )
        logger.info("fact recorded: %s:%s %s = %r", subj_kind, subj_id, predicate, object_text)
    finally:
        conn.close()


def _resolve_subject(cfg: Config, subject: str) -> tuple[str, Optional[int]]:
    """'company' -> (company, None); '<team-slug>' -> (team, id);
    '<email-or-chat-id>' -> (person, id)."""
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


def record_commitment(cfg: Config, who_email_or_id: str, description: str,
                      due: Optional[str], interview_id: Optional[int]) -> None:
    kind, sid = _resolve_subject(cfg, who_email_or_id)
    if kind != "person" or sid is None:
        logger.warning("commitment for unknown person %r — skipped", who_email_or_id)
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
# Mission prompt — Iris-flavoured opener
# ----------------------------------------------------------------------------
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

To post in a server channel/space:

    [[COO_CHANNEL name=<space-name>]] <text>

(or `id=<spaces/AAA>`). Failure comes back as [[BRIDGE_CHANNEL_RESULT ok=false]].

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
  - [[COO_WORKFLOW slug="<slug>" name="<name>" description="<text>" owner_team="<team-slug>"]]
  - [[COO_TASK title="<text>" owner_person_id="<email>" status="pending|active|blocked|done|dropped" due="YYYY-MM-DD"]]
  - [[COO_REPORT kind=factsheet-team|factsheet-person|org-chart|priorities subject="<email|company|team-slug>" title="<text>"]]
      <markdown body>
      [[/COO_REPORT]]
  - [[COO_PERSON_ADD user_id="<email>" name="<display>" role="<role>" team="<team-slug>" access_tier="manager|employee|strategic|admin"]]
  - [[COO_INBOX_HANDLE id=<N> state=attended|held|no-action|queued note="<short>"]]
  - NOOP (whole-reply silence)

# Voice

You ARE {cfg.coo_name}. Not an AI playing a COO. Lead with the question or the
call. Imperative when you have a view ("Pushing GA to Q4. Push back if I'm
wrong."). Names, not "you all". One or two sentences default; longer only when
announcing a decision, escalating, or writing a factsheet.

Start now: open a DM to **{ceo_name}** ({ceo_email}). Introduce yourself
briefly (your name, your role at {company}, what you do), then open Phase 1
with the company-context question.
"""


# ----------------------------------------------------------------------------
# The listener / dispatcher
# ----------------------------------------------------------------------------
class ChatListener:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.bridge = AgentBridge(_pretend_discord_cfg(cfg))
        self.chat = ChatAPI(cfg.sa_json)
        self.company = load_company_name(cfg)
        # uid -> (last text, ts) — same dedup-window scheme as Discord
        self._last_delivered: dict[str, tuple[str, float]] = {}
        self._delivered_path = cfg.state_dir / "delivered.json"
        self._load_delivered()
        self._send_lock = threading.Lock()
        self._last_asserter_pid: Optional[int] = None
        self._stop = threading.Event()
        # Cache: email -> chat_user_id (filled as users DM in)
        self._email_to_user: dict[str, str] = {}
        # Cache: lower(name) -> space
        self._space_by_name: dict[str, dict] = {}

    DEDUP_WINDOW_SECONDS = 90

    # ---- persistence of dedup state ----
    def _load_delivered(self) -> None:
        if not self._delivered_path.exists():
            return
        try:
            data = json.loads(self._delivered_path.read_text())
            self._last_delivered = {k: (v[0], float(v[1])) for k, v in data.items()}
        except Exception:
            logger.exception("failed to load delivered.json")

    def _save_delivered(self) -> None:
        try:
            self._delivered_path.parent.mkdir(parents=True, exist_ok=True)
            self._delivered_path.write_text(json.dumps(self._last_delivered))
        except Exception:
            logger.exception("failed to save delivered.json")

    # ---- chat space resolution ----
    def _resolve_space(self, name_or_id: str) -> Optional[str]:
        if name_or_id.startswith("spaces/"):
            return name_or_id
        key = name_or_id.lower().lstrip("#")
        if key in self._space_by_name:
            return self._space_by_name[key].get("name")
        for sp in self.chat.list_spaces():
            self._space_by_name[(sp.get("displayName") or "").lower()] = sp
        sp = self._space_by_name.get(key)
        return sp.get("name") if sp else None

    # ---- inbound: Pub/Sub event handlers ----
    def handle_event(self, evt: dict) -> None:
        et = evt.get("type")
        if et == "MESSAGE":
            self._on_message(evt)
        elif et == "ADDED_TO_SPACE":
            self._on_added(evt)
        elif et == "REMOVED_FROM_SPACE":
            logger.info("removed from %s", (evt.get("space") or {}).get("name"))
        else:
            logger.info("unhandled event type=%s", et)

    def _on_message(self, evt: dict) -> None:
        msg = evt.get("message") or {}
        sender = msg.get("sender") or {}
        space = evt.get("space") or {}
        space_name = space.get("name") or ""
        user_resource = sender.get("name") or ""    # e.g. "users/123"
        email = sender.get("email") or ""
        display = sender.get("displayName") or email or "Unknown"
        text = msg.get("argumentText") or msg.get("text") or ""

        if not text.strip():
            return

        # Cache email <-> user_resource for outbound DM resolution later.
        if email and user_resource:
            self._email_to_user[email.lower()] = user_resource

        person = (person_by_chat_id(self.cfg, user_resource) if user_resource else None)
        if person is None and email:
            person = person_by_email(self.cfg, email)
            if person and user_resource:
                upsert_chat_user_id(self.cfg, person["id"], user_resource)
        if person is None:
            # TODO inbox path. For v1 we log and bail.
            logger.info("DM from %s <%s> NOT in org chart — ignoring for v1",
                        display, email)
            return

        logger.info("DM from %s <%s> — relaying to agent", display, email)
        self._last_asserter_pid = person["id"]

        channel_id = ensure_channel(self.cfg, display, space_name, kind="dm")
        interview_id = ensure_interview(self.cfg, person["id"], channel_id)
        append_transcript(self.cfg, interview_id, "user", display, text)

        prompt = (
            f"[[INCOMING_DM from={display} email={email} role={person['role'] or '—'}]]\n\n"
            f"  {text}\n\n"
            f"Respond as the persistent COO agent. Use `[[COO_TO user_id=<email>]]` "
            f"for replies that should go to Chat. Plain text is internal notes only."
        )
        with self._send_lock:
            self.bridge.send_prompt(prompt, cancel_first=False)

    def _on_added(self, evt: dict) -> None:
        """Bot was added to a DM or space — opportunity to greet."""
        space = evt.get("space") or {}
        user = evt.get("user") or {}
        email = (user.get("email") or "").lower()
        logger.info("added to space=%s by %s", space.get("name"), email)
        # Send a nudge into the agent so it knows to introduce itself
        if email:
            prompt = (
                f"[[BRIDGE_NOTICE]] You were just added to a Chat DM by {email}. "
                f"If this is the CEO ({self.cfg.ceo_email}), open Phase 1 now — "
                f"emit [[COO_TO user_id={email}]] with your intro + the first "
                f"company-context question."
            )
            with self._send_lock:
                self.bridge.send_prompt(prompt, cancel_first=False)

    # ---- outbound: dispatch agent reply ----
    def dispatch_response(self, response: str) -> None:
        sent_any = False

        # 1. DMs via [[COO_TO user_id=<email>]] <text>
        for m in CHAT_TO_RE.finditer(response):
            target = m.group(1)
            text = normalize_message_text(m.group(2))
            if not text:
                continue
            sent_any |= self._send_dm(target, text)

        # 2. Channel posts via [[COO_CHANNEL name=… | id=…]] <text>
        for m in CHAT_CHANNEL_RE.finditer(response):
            name, sid, raw = m.group(1), m.group(2), m.group(3)
            text = normalize_message_text(raw)
            if not text:
                continue
            sent_any |= self._post_channel(name or sid, text)

        # Active interview (for fact / commitment attribution)
        active_iid: Optional[int] = None
        if self._last_asserter_pid is not None:
            active_iid = ensure_interview(self.cfg, self._last_asserter_pid, None)

        # 3. Persistence markers
        for m in COO_FACT_RE.finditer(response):
            record_fact(self.cfg, m.group(1), m.group(2), m.group(3),
                        self._last_asserter_pid, active_iid)
        for m in COO_COMMITMENT_RE.finditer(response):
            # COO_COMMITMENT in coo_phase1 expects numeric person_id; we
            # accept email here via our own loose parsing.
            kv = _parse_kv(m.group(0)[2:-2])
            who = kv.get("person_id") or kv.get("user_id") or kv.get("email") or ""
            desc = kv.get("description", "")
            due = kv.get("due")
            if who and desc:
                record_commitment(self.cfg, who, desc, due, active_iid)
        for m in COO_DECISION_RE.finditer(response):
            title, body, rationale, scope = _parse_decision_fields(m.group(1))
            if title and body:
                record_decision(self.cfg, title, body, rationale, scope, active_iid)
        # TODO: workflow / task / report / person_add / inbox_handle / app_action /
        # http_call / scheduled_contact — fill in after the round-trip works.

        if not sent_any and "NOOP" not in response.upper():
            logger.debug("agent reply had no actionable markers")

    def _send_dm(self, target_email_or_resource: str, text: str) -> bool:
        # Dedup
        prev = self._last_delivered.get(target_email_or_resource)
        if prev and prev[0] == text and (time.time() - prev[1]) < self.DEDUP_WINDOW_SECONDS:
            logger.info("skipping duplicate DM to %s (within %ds)",
                        target_email_or_resource, self.DEDUP_WINDOW_SECONDS)
            return False
        space = self.chat.find_dm(target_email_or_resource)
        if not space:
            logger.warning("could not resolve DM space for %s", target_email_or_resource)
            self.bridge.send_prompt(
                f"[[BRIDGE_NOTICE]] Could not open a DM to {target_email_or_resource}. "
                f"They may need to message you first, or you may have the wrong address.",
                cancel_first=False,
            )
            return False
        try:
            for chunk in _chunk_message(text, limit=3500):
                self.chat.post_message(space, chunk)
            self._last_delivered[target_email_or_resource] = (text, time.time())
            self._save_delivered()
            logger.info("delivered chat DM to %s (%d chars)",
                        target_email_or_resource, len(text))
            return True
        except Exception:
            logger.exception("failed to DM %s", target_email_or_resource)
            return False

    def _post_channel(self, name_or_id: str, text: str) -> bool:
        space = self._resolve_space(name_or_id)
        if not space:
            logger.warning("space not found: %s", name_or_id)
            self.bridge.send_prompt(
                f"[[BRIDGE_CHANNEL_RESULT ok=false]] Could not find space "
                f"'{name_or_id}'. Tell the user, or ask for the exact space name. "
                f"Reply NOOP if no further action.",
                cancel_first=False,
            )
            return False
        try:
            for chunk in _chunk_message(text, limit=3500):
                self.chat.post_message(space, chunk)
            logger.info("posted in space %s (%d chars)", space, len(text))
            return True
        except Exception:
            logger.exception("post_channel failed for %s", space)
            return False

    # ---- capture loop ----
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

    # ---- main entry ----
    def run(self) -> None:
        # First-start: spin up the agent and deliver the mission prompt.
        new_session = self.bridge.ensure_session()
        first_mark = self.cfg.state_dir / "first_start_done"
        if new_session or not first_mark.exists():
            ceo = person_by_email(self.cfg, self.cfg.ceo_email)
            ceo_name = ceo["display_name"] if ceo else self.cfg.ceo_email
            prompt = chat_mission_prompt(
                self.cfg, self.company, ceo_name, self.cfg.ceo_email,
            )
            with self._send_lock:
                self.bridge.send_prompt(prompt, cancel_first=False)
            self.cfg.state_dir.mkdir(parents=True, exist_ok=True)
            first_mark.write_text(str(int(time.time())))
            logger.info("sent initial mission prompt to agent")
        else:
            # Bot restart on a live agent — prime so we don't re-dispatch the
            # pre-restart reply, then deliver a short amendment.
            self.bridge.prime()
            with self._send_lock:
                self.bridge.send_prompt(
                    "[[BRIDGE_NOTICE]] The Chat listener restarted. You are still "
                    "connected; do NOT re-introduce yourself. Continue the conversation.",
                    cancel_first=False,
                )

        # Start capture loop in a background thread.
        cap_t = threading.Thread(target=self.run_capture, daemon=True)
        cap_t.start()

        # Pub/Sub streaming pull (blocks until shutdown).
        creds = service_account.Credentials.from_service_account_file(
            str(self.cfg.sa_json),
            scopes=["https://www.googleapis.com/auth/pubsub"],
        )
        subscriber = pubsub_v1.SubscriberClient(credentials=creds)
        sub_path = subscriber.subscription_path(self.cfg.project_id, self.cfg.subscription)

        def callback(message: pubsub_v1.subscriber.message.Message) -> None:
            try:
                evt = json.loads(message.data.decode("utf-8"))
                self.handle_event(evt)
                message.ack()
            except Exception:
                logger.exception("event handler failed; nack")
                message.nack()

        flow = pubsub_v1.types.FlowControl(max_messages=20)
        future = subscriber.subscribe(sub_path, callback=callback, flow_control=flow)
        logger.info("subscribed to %s — waiting for Chat events", sub_path)

        def _shutdown(signum, frame):
            logger.info("signal %s — shutting down", signum)
            self._stop.set()
            future.cancel()
        signal.signal(signal.SIGTERM, _shutdown)
        signal.signal(signal.SIGINT, _shutdown)

        try:
            future.result()
        except (FuturesTimeoutError, KeyboardInterrupt):
            pass
        finally:
            try:
                subscriber.close()
            except Exception:
                pass
            self._stop.set()


# ----------------------------------------------------------------------------
# AgentBridge expects a discord-style Config (with .tmux_session, .workdir,
# .run_ai, .agent_kind). We satisfy that shape with a duck-typed object.
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


# ----------------------------------------------------------------------------
def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    cfg = Config.from_env()
    listener = ChatListener(cfg)
    listener.run()


if __name__ == "__main__":
    main()
