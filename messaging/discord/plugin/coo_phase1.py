"""
COO Phase 1 Discord listener (multi-tenant).

Behaviors:
  - Agent (Claude) initiates first contact: when the tenant starts for the first
    time, the bot asks Claude to compose an intro DM and sends it to the CEO.
  - Allowlist (devs + CEO/owner) can DM the bot. Their messages are forwarded
    to the agent.
  - Anyone else who DMs the bot has their message saved to inbox_items; the
    agent does NOT see it unless it asks for the inbox.
  - The agent can emit `[[COO_TO user_id=N]] <text>` to direct a DM to any
    user. In Phase 1 the agent's outreach is also restricted to the allowlist
    (DMs to others are saved as scheduled-but-blocked notes for the operator
    to widen the allowlist).
  - Phase 1 is DM-only; channel messages are ignored.

Config is read entirely from environment variables (set by `coo tenant new`):
  DISCORD_CLAUDEX_BOT_TOKEN
  DISCORD_COO_GUILD_ID
  DISCORD_COO_HOME_CHANNEL_ID
  DISCORD_COO_CEO_USER_ID
  DISCORD_COO_TENANT_SLUG
  DISCORD_COO_TENANT_DB     -- path to tenant DB
  DISCORD_COO_PLATFORM_DB   -- path to platform DB
  DISCORD_COO_STATE_DIR
  DISCORD_COO_WORKDIR
  DISCORD_COO_TMUX_SESSION
  DISCORD_COO_RUN_AI
  DISCORD_COO_AGENT_KIND    -- 'claude' or 'codex'
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import discord

logger = logging.getLogger("coo_phase1")

PASTED_INPUT_RE = re.compile(r"\[Pasted text #\d+ \+\d+ lines\]")
COO_TO_RE = re.compile(r"\[\[COO_TO user_id=(\d+)\]\]\s*(.+?)(?=(?:\[\[COO_|$))", re.S)
COO_FIND_MEMBER_RE = re.compile(r"\[\[COO_FIND_MEMBER query=([^\]]+)\]\]")
COO_FACT_RE = re.compile(
    r'\[\[COO_FACT\s+subject="([^"]+)"\s+predicate="([^"]+)"\s+object="([^"]*)"\]\]'
)
COO_COMMITMENT_RE = re.compile(
    r'\[\[COO_COMMITMENT\s+person_id=(\d+)\s+description="([^"]+)"(?:\s+due="([^"]+)")?\]\]'
)
COO_DECISION_RE = re.compile(r"\[\[COO_DECISION\s+(.+?)\]\]", re.S)
_KV_RE = re.compile(r'(\w+)="([^"]*)"')


def _parse_decision_fields(inner: str) -> tuple[str, str, str | None, str | None]:
    """Tolerate Claude's variants on field names."""
    fields = dict(_KV_RE.findall(inner))
    title = fields.get("title") or fields.get("subject") or ""
    body = (
        fields.get("text")
        or fields.get("description")
        or fields.get("decision_text")
        or ""
    )
    rationale = fields.get("rationale") or None
    scope = fields.get("scope") or None
    return title, body, rationale, scope
COO_CLOSE_USER_RE = re.compile(r"\[\[COO_CLOSE\s+user_id=(\d+)\]\]")
PHASE_APPROVAL_RE = re.compile(r"\bapprove\s+phase\s+([2-9])\b", re.I)


TUI_ARTIFACT_RE = re.compile(
    r"⎿\s*Interrupted.*?(?:What should Claude do instead\?|$)",
    re.S,
)


def normalize_message_text(text: str) -> str:
    """Strip Claude Code's TUI word-wrap from a message body.

    The TUI emits lines like:
        Got it — channel manager for short-term
         rentals, you and Adrien. Two quick ones...
    where every continuation line is indented 1-3 spaces. Capturing the pane
    preserves those line breaks. We need to collapse them back into prose.
    Real paragraph breaks (blank line) are preserved. Strips any TUI control
    artifacts that bleed in (e.g. "⎿ Interrupted · What should Claude do
    instead?").
    """
    text = TUI_ARTIFACT_RE.sub("", text)
    paragraphs = re.split(r"\n\s*\n", text.strip())
    cleaned = [re.sub(r"\s+", " ", p).strip() for p in paragraphs]
    return "\n\n".join(p for p in cleaned if p)
COO_NEXT_CONTACT_RE = re.compile(
    r"\[\[COO_NEXT_CONTACT user_id=(\d+) in_seconds=(\d+) reason=([^\]]+)\]\]"
)
COO_CLOSE_RE = re.compile(r"\[\[COO_CLOSE\]\]")
NOOP_RE = re.compile(r"^\s*●?\s*NOOP\s*$", re.M)


# Per-kind agent instructions emitted when a cadence fires.
CADENCE_INSTRUCTIONS = {
    "daily-brief": (
        "Daily brief: scan today's open commitments, overdue items, and "
        "anything new since yesterday. Compose ONE short DM to the CEO with "
        "the 1–2 things most worth knowing today. Skip (reply NOOP) if "
        "nothing is genuinely notable — silence is OK."
    ),
    "weekly-pulse": (
        "Weekly commitment pulse: look at all open commitments in the DB "
        "(query mentally via what you've recorded). For each that's due in "
        "the next 7 days or already overdue, DM the owner asking a single "
        "crisp status question. As replies come back, record outcomes via "
        "[[COO_FACT subject=\"<user_id>\" predicate=\"commitment_status\" "
        "object=\"...\"]]. Add [[COO_NEXT_CONTACT]] markers to chase the "
        "ones still open."
    ),
    "monthly-review": (
        "Monthly review: look at recent metric_values, surface anomalies "
        "(misses, big drops) and one or two follow-ups the leadership team "
        "should take. DM the CEO with the headline numbers + the one "
        "decision you'd ask them to make. Record any decisions reached via "
        "[[COO_FACT subject=\"company\" predicate=\"decision\" "
        "object=\"...\"]]."
    ),
    "quarterly-okr-grade": (
        "Quarterly OKR grading: for each OKR ending this quarter, propose a "
        "grade (0.0–1.0) and short rationale. DM the OKR owner asking for "
        "their own grade and any narrative. Record the final grade via "
        "[[COO_FACT subject=\"<scope>\" predicate=\"okr_grade\" "
        "object=\"<period>:<grade> – <rationale>\"]]."
    ),
    "factsheet-refresh": (
        "Factsheet refresh: regenerate per-person and per-team factsheets "
        "from the latest facts in the DB. Write them to the company-map "
        "directory under the agent's workdir. No DMs to anyone unless "
        "something blocks you."
    ),
    "risk-review": (
        "Risk register review: list all open risks whose last_reviewed_at "
        "is older than their review_cadence (or NULL). For each, DM the "
        "owner asking whether mitigation is on track, status changed, or "
        "the risk can be closed. Update the risk via "
        "[[COO_FACT subject=\"company\" predicate=\"risk_status\" "
        "object=\"<slug>:<status>\"]]."
    ),
}


@dataclass
class Config:
    bot_token: str
    guild_id: int
    home_channel_id: int
    ceo_user_id: int
    tenant_slug: str
    tenant_db: Path
    platform_db: Path
    state_dir: Path
    workdir: Path
    tmux_session: str
    run_ai: str
    agent_kind: str

    @classmethod
    def from_env(cls) -> "Config":
        def req(k: str) -> str:
            v = os.environ.get(k)
            if not v:
                raise SystemExit(f"Missing env var: {k}")
            return v

        return cls(
            bot_token=req("DISCORD_CLAUDEX_BOT_TOKEN"),
            guild_id=int(req("DISCORD_COO_GUILD_ID")),
            home_channel_id=int(req("DISCORD_COO_HOME_CHANNEL_ID")),
            ceo_user_id=int(req("DISCORD_COO_CEO_USER_ID")),
            tenant_slug=req("DISCORD_COO_TENANT_SLUG"),
            tenant_db=Path(req("DISCORD_COO_TENANT_DB")),
            platform_db=Path(req("DISCORD_COO_PLATFORM_DB")),
            state_dir=Path(req("DISCORD_COO_STATE_DIR")),
            workdir=Path(req("DISCORD_COO_WORKDIR")),
            tmux_session=req("DISCORD_COO_TMUX_SESSION"),
            run_ai=req("DISCORD_COO_RUN_AI"),
            agent_kind=os.environ.get("DISCORD_COO_AGENT_KIND", "claude"),
        )


def _connect(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db))
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    return conn


def load_allowlist(cfg: Config) -> dict[int, dict]:
    """Allowlist = platform developers + ALL tenant.people with Discord IDs.

    Phase enforcement is at insert-time, not lookup-time: the wizard only
    inserts the CEO in Phase 1; `coo tenant add-person` adds managers in
    Phase 2; staff in Phase 3. So the allowlist naturally grows.

    Returns a map: discord_user_id -> {handle, name, role, tier}
    """
    out: dict[int, dict] = {}

    pconn = _connect(cfg.platform_db)
    for r in pconn.execute(
        "SELECT discord_user_id, handle, display_name FROM developers "
        "WHERE is_active = 1 AND discord_user_id IS NOT NULL"
    ).fetchall():
        out[int(r["discord_user_id"])] = {
            "handle": r["handle"],
            "name": r["display_name"],
            "role": "developer",
            "tier": "developer",
        }
    pconn.close()

    tconn = _connect(cfg.tenant_db)
    for r in tconn.execute(
        "SELECT discord_user_id, slug, display_name, role, access_tier, "
        "       is_content_approver "
        "FROM people WHERE deleted_at IS NULL AND discord_user_id IS NOT NULL"
    ).fetchall():
        out[int(r["discord_user_id"])] = {
            "handle": r["slug"],
            "name": r["display_name"],
            "role": r["role"] or ("ceo" if int(r["discord_user_id"]) == cfg.ceo_user_id else "member"),
            "tier": r["access_tier"],
        }
    tconn.close()
    return out


def load_ceo(cfg: Config) -> dict | None:
    tconn = _connect(cfg.tenant_db)
    row = tconn.execute(
        "SELECT id, slug, display_name, discord_user_id, role "
        "FROM people WHERE discord_user_id = ?",
        (cfg.ceo_user_id,),
    ).fetchone()
    tconn.close()
    return dict(row) if row else None


def load_company_name(cfg: Config) -> str:
    pconn = _connect(cfg.platform_db)
    row = pconn.execute(
        "SELECT company_name FROM tenants WHERE slug = ?", (cfg.tenant_slug,)
    ).fetchone()
    pconn.close()
    return row["company_name"] if row else cfg.tenant_slug


# -------------------- agent bridge --------------------


class AgentBridge:
    """Manages the tmux pane running Claude/Codex and shuttles text in & out."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._target = f"{cfg.tmux_session}:agent.0"
        self._last_response_key: str | None = None

    def ensure_session(self) -> bool:
        """Returns True if a new tmux session was created, False if reused."""
        cfg = self.cfg
        cfg.workdir.mkdir(parents=True, exist_ok=True)
        if self._session_exists():
            logger.info("tmux session %s already exists; reusing", cfg.tmux_session)
            return False
        cmd = f"cd {shlex.quote(str(cfg.workdir))} && exec {shlex.quote(cfg.run_ai)} {shlex.quote(cfg.agent_kind)}"
        subprocess.run(
            [
                "tmux", "new-session", "-d",
                "-s", cfg.tmux_session,
                "-n", "agent",
                "-c", str(cfg.workdir),
                cmd,
            ],
            check=True,
        )
        logger.info("created tmux session %s", cfg.tmux_session)
        time.sleep(2.5)  # let the agent TUI initialise
        return True

    def _session_exists(self) -> bool:
        return (
            subprocess.run(
                ["tmux", "has-session", "-t", self.cfg.tmux_session],
                capture_output=True,
            ).returncode
            == 0
        )

    def send_prompt(self, text: str, cancel_first: bool = False) -> None:
        """Type text into the agent pane and submit it.

        Claude Code uses bracketed-paste mode; sending Enter immediately after
        a multi-line paste gets absorbed by the paste sequence. We send the
        text, sleep, send Enter (closes paste), sleep, send Enter (submits).

        cancel_first=True sends Escape first to cancel any modal/partial input.
        Use ONLY for top-level interrupting events (initial mission). Avoid
        for bridge feedback while Claude may be mid-response — Escape will
        truncate the in-flight reply.
        """
        if not self._session_exists():
            raise RuntimeError("tmux session is gone; cannot send prompt")
        if cancel_first:
            subprocess.run(
                ["tmux", "send-keys", "-t", self._target, "Escape"],
                timeout=5, check=False,
            )
            time.sleep(0.15)
        subprocess.run(
            ["tmux", "send-keys", "-t", self._target, "-l", text],
            timeout=20, check=True,
        )
        time.sleep(0.4)
        subprocess.run(
            ["tmux", "send-keys", "-t", self._target, "Enter"],
            timeout=5, check=True,
        )
        time.sleep(0.4)
        subprocess.run(
            ["tmux", "send-keys", "-t", self._target, "Enter"],
            timeout=5, check=False,
        )

    def capture(self, lines: int = 400) -> str:
        if not self._session_exists():
            return ""
        out = subprocess.run(
            ["tmux", "capture-pane", "-p", "-t", self._target, "-S", f"-{lines}"],
            capture_output=True, text=True, timeout=5,
        )
        return out.stdout

    def latest_response(self) -> str | None:
        """Extract the latest agent reply (block of `●` lines + body) from the pane.

        Dedup is on the *response text* itself; the pane's bytes drift on
        every cursor blink so comparing pane snapshots double-fires.
        """
        text = self.capture()
        if not text:
            return None

        # Strip the input box at the bottom so a queued (unsubmitted) prompt
        # doesn't get mistaken for a reply.
        body = re.split(r"^─{10,}\s*$", text, flags=re.M)
        if len(body) >= 3:
            text = "".join(body[:-2])

        # Remove Claude Code's own TUI prompts that show as `●` blocks —
        # otherwise the rating prompt becomes the "latest" and masks the
        # real agent response.
        text = re.sub(
            r"●\s+How is Claude doing this session\?.*?(?=^●|\Z)",
            "",
            text,
            flags=re.S | re.M,
        )

        marker = list(re.finditer(r"^●\s+(.+)$", text, flags=re.M))
        if not marker:
            return None

        last_idx = marker[-1].start()
        tail = text[last_idx:]
        cut = re.search(r"^(✻|✶|⏵|❯)", tail, flags=re.M)
        response = tail[: cut.start() if cut else len(tail)]
        response = re.sub(r"^●\s*", "", response).strip()

        if not response:
            return None

        # Suppress only if the entire response is just NOOP. A response that
        # CONTAINS NOOP at the top but also chains a [[COO_NEXT_CONTACT]] or
        # other marker still needs to be dispatched so the marker is captured.
        if response.strip() == "NOOP":
            return None

        # Whitespace-normalised key — TUI re-renders cause raw text to drift
        # without the actual response changing.
        dedup_key = re.sub(r"\s+", " ", response).strip()
        if dedup_key == self._last_response_key:
            return None

        self._last_response_key = dedup_key
        return response


# -------------------- prompt templates --------------------


def mission_prompt(cfg: Config, allowlist: dict[int, dict], ceo: dict, company_name: str) -> str:
    devs = [v for v in allowlist.values() if v["role"] == "developer"]
    dev_lines = "\n".join(f"  - {d['name']} ({d['handle']}, developer, role=developer)" for d in devs) or "  (none registered)"
    dev_ids_csv = ", ".join(
        str(uid) for uid, v in allowlist.items() if v["role"] == "developer"
    )

    return f"""You are the persistent COO agent for **{company_name}**.

Your conversation surface is Discord (this session is bridged to it via tmux).
The listener forwards DMs from allowlisted people to you and relays anything
you emit between [[…]] markers back to them.

# Allowlist (Phase 1)

You may converse with these people only:

  - {ceo['display_name']} (Discord user_id={cfg.ceo_user_id}, role: {ceo.get('role') or 'CEO'}, content authority for {company_name})
{dev_lines}

Anyone else's DMs are silently saved to an inbox you cannot see — do not
address them.

# Phase 1 — explicit deliverables checklist

Phase 1 builds the company map with the CEO. You CANNOT exit Phase 1
until every box below is filled. Work them roughly in order — context
first, then structure, then operating detail. Track progress in your
**internal notes** (plain text, NO `[[COO_TO]]` prefix — those are not
sent to anyone). Refer back to the checklist as you interview.

  [ ] **Company context (do this first)**: what the company actually
        does — product or service, who the customer is, how it makes
        money. Plus stage signals the CEO is willing to share
        (founded when, rough headcount, rough revenue stage,
        funded / bootstrapped). Plus the "why now" / founder story
        if the CEO offers it. You need this before structure makes
        sense.
  [ ] **Departments / functional areas** — names + a one-line
        description of what each one owns.
  [ ] For each department: **manager name + confirmed Discord user_id**
        (or explicit "CEO covers, no separate manager"). Use the
        member-search marker (below) to confirm IDs as soon as a name is
        mentioned. Do NOT exit Phase 1 with unconfirmed managers.
  [ ] Top 3–5 **company priorities this quarter**, with brief reasoning.
  [ ] Top 3–5 **recurring workflows** at company level (onboarding,
        support, billing, releases, …).
  [ ] **Tools / apps** in use, grouped by the workflow they support
        (CRM, support, billing, comms, dev, etc.).
  [ ] Top 3 **risks** the CEO is worried about right now.
  [ ] Significant **decisions made in the last ~90 days**, with rationale.
  [ ] **Headcount + open roles** (target start dates if known).
  [ ] What "good" looks like 6 months out — CEO's own success criteria.

Do NOT ask the CEO to list individual employees. Staff under each manager
is Phase 2 work.

# Finding a Discord member by name

When the CEO names a manager (e.g. "Carlos leads sales"), do NOT just
write "Carlos" to your map and move on. Find them on Discord first by
emitting:

    [[COO_FIND_MEMBER query=Carlos]]

The bridge will reply with `[[BRIDGE_FIND_RESULT query="Carlos"]]` and a
list of matching guild members (display name, username, discord_user_id).
Then offer the candidates back to the CEO and ask which one is the right
person. Once confirmed, save the user_id in your notes alongside that
manager record. If no match is found, ask the CEO for the manager's email
or other contact info.

# How to send a DM

To send a DM to anyone in the allowlist:

    [[COO_TO user_id=<discord_user_id>]] <your message text>

Plain text WITHOUT a `[[COO_TO ...]]` prefix is internal notes and is
NOT sent to anyone — use plain text to track checklist progress, draft
factsheets, or think out loud.

# Self-pacing

After each meaningful exchange, schedule the next follow-up:

    [[COO_NEXT_CONTACT user_id=<id> in_seconds=<int> reason=<short>]]

The bridge schedules the nudge and re-prompts you at that time. Do not
spawn new sessions.

# DM style

Discord, not email. One or two crisp questions per message, plain prose,
no markdown headings, no bullets, no numbered lists inside DMs. The CEO
reads on a phone — treat each message as a 5-second read. Work the
checklist one or two boxes at a time across many messages, not all at
once.

# Markers

  - `[[COO_TO user_id=N]] <text>` — DM to that user
  - `[[COO_FIND_MEMBER query=<name>]]` — search Discord guild for a person
  - `[[COO_NEXT_CONTACT user_id=N in_seconds=I reason=R]]` — schedule a follow-up
  - `[[COO_FACT subject="<id|company|team-slug>" predicate="<pred>" object="<val>"]]`
       — record a fact in the DB. subject is a Discord user_id, the literal
       "company", or a team slug.
  - `[[COO_COMMITMENT person_id=N description="<text>" due="YYYY-MM-DD"]]`
       — record a commitment that person N made. due is optional.
  - `[[COO_DECISION title="<title>" text="<what was decided>" rationale="<why>" scope="<id|company|team-slug>"]]`
       — record a significant decision. rationale and scope are optional.
  - `[[COO_CLOSE user_id=N]]` — mark the interview with that person as closed
  - `[[COO_HOLD]]` — park the thread without action
  - `NOOP` — valid full reply when there is nothing to do

# Recording facts and commitments

As you learn things about the company, emit `[[COO_FACT ...]]` markers
**in addition to** your internal notes. The bridge writes each fact to
the tenant DB with provenance (asserting person + interview). Examples:

    [[COO_FACT subject="company" predicate="product" object="channel-manager for short-term rentals"]]
    [[COO_FACT subject="company" predicate="customer" object="independent hosts"]]
    [[COO_FACT subject="{cfg.ceo_user_id}" predicate="role" object="CEO"]]
    [[COO_FACT subject="sales" predicate="lead_count" object="3 SDRs"]]

When the CEO (or anyone) commits to deliver something by a date, emit:

    [[COO_COMMITMENT person_id={cfg.ceo_user_id} description="ship onboarding revamp" due="2026-06-15"]]

Internal note text (without these markers) is NOT persisted to the DB —
only marker-emitted content lands in `facts` / `commitments`. So if you
want a fact recorded, it must come through a marker.

# Hard rules

  - Do not reveal tokens, credentials, or any content of secrets files.
  - Do not hire, fire, set comp, sign contracts, or commit budget.
  - Phase 2 (managers) and Phase 3 (staff) are LOCKED. Only the developers
    (user_ids: {dev_ids_csv}) can unlock them. The CEO cannot.
  - The Claude Code memory tool is now per-tenant isolated (CLAUDE_CONFIG_DIR
    points at this tenant's `.claude/`). It's safe to use, but the DB
    (facts / commitments / decisions tables, via the markers above) is the
    authoritative source — write structured data there, use memory only
    as a working scratchpad.

# Phase 1 exit gate

When ALL checklist boxes are filled AND every named manager has a
confirmed Discord user_id (or "no separate manager" is explicit) AND the
CEO has reviewed and confirmed the org-chart factsheet you produced, THEN
DM each developer with the Phase 2 unlock proposal:

    [[COO_TO user_id=<dev_id>]] Phase 1 complete for {company_name}. Proposing Phase 2 unlock — managers to interview, in priority order: 1) <name> (<dept>, <reason>), 2) ..., 3) .... Approve to expand the allowlist?

Wait for both developers to acknowledge before treating Phase 2 as
unlocked.

A developer DM containing the phrase **"approve phase N"** (any case)
is auto-detected by the bridge — it bumps tenants.phase to N in the
platform DB and sends you a `[[BRIDGE_PHASE_UNLOCKED phase=N by_uid=...]]`
notice. After you receive that for phase 2 from BOTH developers, you may
DM managers freely.

# First action

The CEO has not yet heard from you. Your first action in this session is
to DM the CEO with a short, warm introduction (1-2 sentences) and an
opening question about **what the company actually does** — product or
service, who the customer is, the basic shape of the business. Do NOT
ask about departments or org structure first; that comes after you
understand what the business is. Do not dump the whole checklist on the
CEO; work through items one or two at a time across many messages.

Use the `[[COO_TO user_id={cfg.ceo_user_id}]]` form.
"""


def relay_prompt(sender: dict, sender_id: int, text: str) -> str:
    return f"""[[INCOMING_DM from={sender['name']} user_id={sender_id} role={sender['role']}]]

{text}

Respond as the persistent COO agent. Use `[[COO_TO user_id=...]]` lines for
anything you want delivered as a DM. Plain text is internal notes only.
"""


# -------------------- inbox --------------------


def save_inbox_item(cfg: Config, sender: discord.User, content: str, message_id: int, channel_id: int) -> None:
    tconn = _connect(cfg.tenant_db)
    try:
        # Ensure the channel row exists (for the FK)
        row = tconn.execute(
            "SELECT id FROM channels WHERE platform_channel_id = ?",
            (str(channel_id),),
        ).fetchone()
        if row:
            ch_id = row["id"]
        else:
            cur = tconn.execute(
                "INSERT INTO channels (platform_channel_id, name, kind, is_watched) "
                "VALUES (?, ?, 'dm', 0)",
                (str(channel_id), f"DM with {sender.name}"),
            )
            ch_id = cur.lastrowid

        # Person row may not exist; that's fine — sender_person_id is nullable.
        person_row = tconn.execute(
            "SELECT id FROM people WHERE discord_user_id = ?", (sender.id,)
        ).fetchone()
        person_id = person_row["id"] if person_row else None

        tags = json.dumps(
            [
                "source-inbox",
                "channel-dm",
                f"discord-username-{sender.name}",
            ]
        )
        tconn.execute(
            "INSERT INTO inbox_items (platform_message_id, channel_id, sender_person_id, "
            "content, received_at, workflow_state, tags_json) "
            "VALUES (?, ?, ?, ?, datetime('now'), 'pending', ?)",
            (str(message_id), ch_id, person_id, content, tags),
        )
        tconn.commit()
        logger.info("saved inbox item from %s (uid=%s)", sender.name, sender.id)
    finally:
        tconn.close()


# -------------------- the bot --------------------


class COOBot(discord.Client):
    def __init__(self, cfg: Config):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.dm_messages = True
        intents.members = True
        super().__init__(intents=intents)
        self.cfg = cfg
        self.allowlist = load_allowlist(cfg)
        self.ceo = load_ceo(cfg)
        self.company = load_company_name(cfg)
        self.bridge = AgentBridge(cfg)
        self._capture_task: asyncio.Task | None = None
        self._schedule_task: asyncio.Task | None = None
        self._cadence_task: asyncio.Task | None = None
        self.first_start_marker = cfg.state_dir / "first_start_done"
        self._delivered_path = cfg.state_dir / "delivered.json"
        self._last_delivered: dict[int, str] = self._load_delivered()
        self._send_lock = asyncio.Lock()
        # tenant dir (parent of state_dir) — for placing transcripts on disk
        self._tenant_dir = cfg.state_dir.parent
        # The most recent person who DM'd the bot. Used to attribute facts /
        # commitments emitted by the agent in the response that follows.
        self._last_asserter_person_id: int | None = None

    def _load_delivered(self) -> dict[int, str]:
        if not self._delivered_path.exists():
            return {}
        try:
            data = json.loads(self._delivered_path.read_text())
            return {int(k): v for k, v in data.items()}
        except Exception:
            logger.exception("failed to load delivered.json; starting empty")
            return {}

    def _save_delivered(self) -> None:
        try:
            self._delivered_path.write_text(json.dumps(self._last_delivered))
        except Exception:
            logger.exception("failed to persist delivered.json")

    async def _send_to_agent(self, text: str, cancel_first: bool = False) -> None:
        """Serialised paste into Claude's pane; prevents concurrent paste collisions.

        After each send, clear the response-level dedup so the next response
        Claude produces gets dispatched — even if textually identical to the
        previous one. Per-marker dedup downstream prevents duplicate side-effects.
        """
        async with self._send_lock:
            await asyncio.to_thread(self.bridge.send_prompt, text, cancel_first)
            self.bridge._last_response_key = None

    # ----- conversation persistence -----

    def _ensure_interview(self, person_id: int) -> int | None:
        """Return the open interview id for this person, opening one if needed."""
        conn = _connect(self.cfg.tenant_db)
        try:
            row = conn.execute(
                "SELECT id FROM interviews WHERE person_id = ? AND status = 'open' "
                "ORDER BY id DESC LIMIT 1",
                (person_id,),
            ).fetchone()
            if row:
                return int(row["id"])
            slug_row = conn.execute(
                "SELECT slug FROM people WHERE id = ?", (person_id,)
            ).fetchone()
            slug = slug_row["slug"] if slug_row else f"person-{person_id}"
            day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            transcript_path = self._tenant_dir / "transcripts" / day / f"{slug}.md"
            transcript_path.parent.mkdir(parents=True, exist_ok=True)
            cur = conn.execute(
                "INSERT INTO interviews (person_id, started_at, status, purpose, "
                "  transcript_path) "
                "VALUES (?, datetime('now'), 'open', 'phase-1', ?)",
                (person_id, str(transcript_path)),
            )
            conn.commit()
            return int(cur.lastrowid)
        except Exception:
            logger.exception("ensure_interview failed for person_id=%s", person_id)
            return None
        finally:
            conn.close()

    def _append_transcript(
        self, interview_id: int, kind: str, sender: str, text: str
    ) -> None:
        conn = _connect(self.cfg.tenant_db)
        try:
            row = conn.execute(
                "SELECT transcript_path FROM interviews WHERE id = ?",
                (interview_id,),
            ).fetchone()
        finally:
            conn.close()
        if not row or not row["transcript_path"]:
            return
        path = Path(row["transcript_path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with path.open("a", encoding="utf-8") as f:
            f.write(f"## {ts} — {kind} — {sender}\n\n{text}\n\n")

    def _person_id_for_uid(self, discord_user_id: int) -> int | None:
        conn = _connect(self.cfg.tenant_db)
        try:
            row = conn.execute(
                "SELECT id FROM people WHERE discord_user_id = ?",
                (discord_user_id,),
            ).fetchone()
        finally:
            conn.close()
        return int(row["id"]) if row else None

    def _close_interview(self, person_id: int) -> None:
        conn = _connect(self.cfg.tenant_db)
        try:
            with conn:
                conn.execute(
                    "UPDATE interviews SET status = 'closed', ended_at = datetime('now') "
                    "WHERE person_id = ? AND status = 'open'",
                    (person_id,),
                )
        finally:
            conn.close()
        logger.info("closed interviews for person_id=%s", person_id)

    def _resolve_subject(self, subject: str) -> tuple[str, int | None]:
        """Parse a [[COO_FACT subject="..."]] value into (subject_kind, subject_id)."""
        subject = subject.strip()
        if subject.lower() == "company":
            return "company", None
        if subject.isdigit():
            # discord user_id → look up tenant person
            conn = _connect(self.cfg.tenant_db)
            try:
                row = conn.execute(
                    "SELECT id FROM people WHERE discord_user_id = ?", (int(subject),)
                ).fetchone()
            finally:
                conn.close()
            return ("person", int(row["id"])) if row else ("person", None)
        # team slug
        conn = _connect(self.cfg.tenant_db)
        try:
            row = conn.execute(
                "SELECT id FROM teams WHERE slug = ?", (subject,)
            ).fetchone()
        finally:
            conn.close()
        return ("team", int(row["id"])) if row else ("team", None)

    def _record_fact(
        self,
        subject: str,
        predicate: str,
        obj: str,
        asserter_person_id: int | None,
        interview_id: int | None,
    ) -> None:
        subject_kind, subject_id = self._resolve_subject(subject)
        conn = _connect(self.cfg.tenant_db)
        try:
            with conn:
                # Freshness dedup: same subject+predicate+object in last 5 min skip
                recent = conn.execute(
                    "SELECT id FROM facts "
                    "WHERE subject_kind = ? AND subject_id IS ? AND predicate = ? "
                    "AND object_text = ? "
                    "AND created_at > datetime('now', '-300 seconds')",
                    (subject_kind, subject_id, predicate, obj),
                ).fetchone()
                if recent:
                    return
                conn.execute(
                    "INSERT INTO facts (subject_kind, subject_id, predicate, "
                    "  object_text, asserted_by_person_id, asserted_at, "
                    "  source_interview_id, is_current) "
                    "VALUES (?, ?, ?, ?, ?, datetime('now'), ?, 1)",
                    (
                        subject_kind, subject_id, predicate, obj,
                        asserter_person_id, interview_id,
                    ),
                )
            logger.info(
                "fact recorded: %s:%s %s = %r",
                subject_kind, subject_id, predicate, obj,
            )
        except Exception:
            logger.exception("record_fact failed")
        finally:
            conn.close()

    def _record_decision(
        self,
        title: str,
        text: str,
        rationale: str | None,
        scope: str | None,
        asserter_person_id: int | None,
        interview_id: int | None,
    ) -> None:
        scope_kind: str | None = None
        scope_id: int | None = None
        if scope:
            kind, sid = self._resolve_subject(scope)
            scope_kind, scope_id = kind, sid
        conn = _connect(self.cfg.tenant_db)
        try:
            with conn:
                recent = conn.execute(
                    "SELECT id FROM decisions WHERE title = ? AND decision_text = ? "
                    "AND created_at > datetime('now', '-300 seconds')",
                    (title, text),
                ).fetchone()
                if recent:
                    return
                conn.execute(
                    "INSERT INTO decisions (title, decision_text, rationale, "
                    "  decided_by_person_id, decided_at, scope_kind, scope_id, "
                    "  source_interview_id, is_current) "
                    "VALUES (?, ?, ?, ?, datetime('now'), ?, ?, ?, 1)",
                    (title, text, rationale, asserter_person_id,
                     scope_kind, scope_id, interview_id),
                )
            logger.info("decision recorded: %r (scope=%s)", title, scope_kind)
        except Exception:
            logger.exception("record_decision failed")
        finally:
            conn.close()

    def _unlock_phase(self, new_phase: int, approver_uid: int) -> bool:
        """Bump the tenant's phase in platform.tenants if the new phase is higher."""
        pconn = _connect(self.cfg.platform_db)
        try:
            row = pconn.execute(
                "SELECT id, phase FROM tenants WHERE slug = ?",
                (self.cfg.tenant_slug,),
            ).fetchone()
            if not row:
                return False
            if row["phase"] >= new_phase:
                return False
            with pconn:
                pconn.execute(
                    "UPDATE tenants SET phase = ? WHERE id = ?",
                    (new_phase, row["id"]),
                )
                pconn.execute(
                    "INSERT INTO platform_audit (action, tenant_id, payload_json) "
                    "VALUES ('phase_unlocked', ?, ?)",
                    (row["id"], json.dumps({
                        "from_phase": row["phase"],
                        "to_phase": new_phase,
                        "approver_uid": approver_uid,
                    })),
                )
            return True
        finally:
            pconn.close()

    def _record_commitment(
        self,
        target_uid: int,
        description: str,
        due: str | None,
        interview_id: int | None,
    ) -> None:
        conn = _connect(self.cfg.tenant_db)
        try:
            row = conn.execute(
                "SELECT id FROM people WHERE discord_user_id = ?", (target_uid,)
            ).fetchone()
            if not row:
                logger.warning("commitment for unknown uid=%s; skipping", target_uid)
                return
            with conn:
                recent = conn.execute(
                    "SELECT id FROM commitments "
                    "WHERE person_id = ? AND description = ? "
                    "AND created_at > datetime('now', '-300 seconds')",
                    (row["id"], description),
                ).fetchone()
                if recent:
                    return
                conn.execute(
                    "INSERT INTO commitments (person_id, description, due_at, "
                    "  source_interview_id, status) "
                    "VALUES (?, ?, ?, ?, 'open')",
                    (row["id"], description, due, interview_id),
                )
            logger.info(
                "commitment recorded: uid=%s due=%s '%s'",
                target_uid, due or "—", description[:60],
            )
        except Exception:
            logger.exception("record_commitment failed")
        finally:
            conn.close()

    async def setup_hook(self) -> None:
        self._session_was_new = self.bridge.ensure_session()

    async def on_ready(self) -> None:
        logger.info("Discord ready as %s (id=%s)", self.user, self.user.id if self.user else "?")
        if not self.ceo:
            logger.error("No CEO row in tenant DB. Cannot proceed.")
            return

        if self._session_was_new:
            # Fresh tmux + agent. Send full mission, mark first start.
            await self._send_initial_mission()
            self.first_start_marker.write_text(str(int(time.time())))
        else:
            # Bot restarted but agent is still alive. Prime dedup so we
            # don't re-deliver the last response, then send an amendment.
            await asyncio.to_thread(self.bridge.latest_response)
            await self._send_amendment()

        self._capture_task = asyncio.create_task(self._capture_loop())
        self._schedule_task = asyncio.create_task(self._schedule_loop())
        self._cadence_task = asyncio.create_task(self._cadence_loop())

    async def _send_initial_mission(self) -> None:
        prompt = mission_prompt(self.cfg, self.allowlist, self.ceo, self.company)
        logger.info("Sending initial mission prompt to agent")
        await self._send_to_agent(prompt, cancel_first=True)

    async def _send_amendment(self) -> None:
        """Send a short guidance update when the bot restarts on a live agent."""
        amendment = (
            "[[BRIDGE_NOTICE]] The Discord listener restarted. You are still "
            "connected; do NOT re-introduce yourself. Continue the conversation "
            "where you left off.\n\n"
            "Refined Phase 1 guidance:\n"
            "  - Phase 1 maps DEPARTMENTS and the MANAGER who leads each, plus "
            "company priorities and main workflows.\n"
            "  - Do NOT ask the CEO to enumerate individual employees or staff "
            "under managers. Staff get mapped in Phase 2 by interviewing each "
            "manager directly.\n"
            "  - Keep DM messages short, plain prose, no bullets/numbered lists "
            "inside DMs. One or two crisp questions per message.\n\n"
            "Acknowledge silently with NOOP and continue when the next user "
            "message arrives."
        )
        logger.info("Sending mission amendment to live agent")
        await self._send_to_agent(amendment, cancel_first=False)

    async def on_message(self, message: discord.Message) -> None:
        if message.author == self.user:
            return
        if not isinstance(message.channel, discord.DMChannel):
            return  # Phase 1: DM-only

        sender_id = message.author.id
        content = message.content or ""

        # Reload allowlist from DB on every message so that `coo tenant
        # add-person` lands without a restart.
        self.allowlist = await asyncio.to_thread(load_allowlist, self.cfg)

        if sender_id in self.allowlist:
            sender = self.allowlist[sender_id]
            logger.info("DM from allowlisted %s (uid=%s)", sender["name"], sender_id)

            # Phase unlock: a developer DM containing "approve phase N" advances
            # tenants.phase. Both devs aren't required at the bot level — the
            # mission prompt tells Claude to wait for both before treating it
            # as unlocked, but the platform DB tracks the latest phase reached.
            if sender.get("tier") == "developer":
                m = PHASE_APPROVAL_RE.search(content)
                if m:
                    new_phase = int(m.group(1))
                    unlocked = await asyncio.to_thread(
                        self._unlock_phase, new_phase, sender_id,
                    )
                    if unlocked:
                        logger.info(
                            "phase advanced to %d by uid=%s",
                            new_phase, sender_id,
                        )
                        await self._send_to_agent(
                            f"[[BRIDGE_PHASE_UNLOCKED phase={new_phase} "
                            f"by_uid={sender_id} by_name=\"{sender['name']}\"]]\n\n"
                            f"Developer {sender['name']} has approved unlocking "
                            f"to Phase {new_phase}. You may now act on "
                            f"that-phase capabilities (e.g. DM managers for "
                            f"Phase 2). Continue the conversation; respond NOOP "
                            f"if no immediate action.",
                            cancel_first=False,
                        )
            # Resolve to tenant person row; open/find interview; persist message.
            person_id = await asyncio.to_thread(self._person_id_for_uid, sender_id)
            if person_id is not None:
                self._last_asserter_person_id = person_id
                iid = await asyncio.to_thread(self._ensure_interview, person_id)
                if iid is not None:
                    await asyncio.to_thread(
                        self._append_transcript, iid, "user", sender["name"], content
                    )
            prompt = relay_prompt(sender, sender_id, content)
            await self._send_to_agent(prompt, cancel_first=False)
        else:
            logger.info("DM from non-allowlist user %s (uid=%s) -> inbox", message.author.name, sender_id)
            save_inbox_item(self.cfg, message.author, content, message.id, message.channel.id)
            # Silent: do not reply, do not forward to agent.

    async def _capture_loop(self) -> None:
        """Poll the agent pane for new responses and dispatch them."""
        await asyncio.sleep(3)  # let the agent finish boot
        while not self.is_closed():
            try:
                response = await asyncio.to_thread(self.bridge.latest_response)
                if response:
                    await self._dispatch_response(response)
            except Exception:
                logger.exception("capture loop error")
            await asyncio.sleep(2)

    async def _dispatch_response(self, response: str) -> None:
        # Reload allowlist so newly-added managers can be the agent's DM target.
        self.allowlist = await asyncio.to_thread(load_allowlist, self.cfg)
        sent_any = False
        for m in COO_TO_RE.finditer(response):
            target_id = int(m.group(1))
            text = normalize_message_text(m.group(2))
            if not text:
                continue
            if target_id not in self.allowlist:
                logger.warning(
                    "agent tried to DM uid=%s outside allowlist; dropping in Phase 1",
                    target_id,
                )
                continue
            if self._last_delivered.get(target_id) == text:
                logger.info(
                    "skipping duplicate DM to uid=%s (%d chars, identical to last)",
                    target_id, len(text),
                )
                continue
            try:
                user = await self.fetch_user(target_id)
                await user.send(text)
                self._last_delivered[target_id] = text
                self._save_delivered()
                logger.info("delivered agent DM to uid=%s (%d chars)", target_id, len(text))
                sent_any = True
                # Persist the outbound DM into the recipient's interview transcript.
                recipient_pid = await asyncio.to_thread(self._person_id_for_uid, target_id)
                if recipient_pid is not None:
                    iid = await asyncio.to_thread(self._ensure_interview, recipient_pid)
                    if iid is not None:
                        await asyncio.to_thread(
                            self._append_transcript, iid, "agent", "COO", text
                        )
            except Exception:
                logger.exception("failed to DM uid=%s", target_id)

        # Active interview for fact/commitment attribution (most recent DM sender)
        active_iid: int | None = None
        if self._last_asserter_person_id is not None:
            active_iid = await asyncio.to_thread(
                self._ensure_interview, self._last_asserter_person_id
            )

        fact_count = 0
        for m in COO_FACT_RE.finditer(response):
            await asyncio.to_thread(
                self._record_fact,
                m.group(1), m.group(2), m.group(3),
                self._last_asserter_person_id, active_iid,
            )
            fact_count += 1

        commit_count = 0
        for m in COO_COMMITMENT_RE.finditer(response):
            target_uid = int(m.group(1))
            await asyncio.to_thread(
                self._record_commitment,
                target_uid, m.group(2), m.group(3), active_iid,
            )
            commit_count += 1

        decision_count = 0
        for m in COO_DECISION_RE.finditer(response):
            title, body, rationale, scope = _parse_decision_fields(m.group(1))
            if not title or not body:
                continue
            await asyncio.to_thread(
                self._record_decision,
                title, body, rationale, scope,
                self._last_asserter_person_id, active_iid,
            )
            decision_count += 1

        for m in COO_CLOSE_USER_RE.finditer(response):
            uid = int(m.group(1))
            pid = await asyncio.to_thread(self._person_id_for_uid, uid)
            if pid is not None:
                await asyncio.to_thread(self._close_interview, pid)

        if fact_count or commit_count or decision_count:
            logger.info(
                "persisted markers: facts=%d commitments=%d decisions=%d",
                fact_count, commit_count, decision_count,
            )

        for m in COO_NEXT_CONTACT_RE.finditer(response):
            uid, secs, reason = int(m.group(1)), int(m.group(2)), m.group(3).strip()
            self._record_scheduled_contact(uid, secs, reason)

        queries = [
            m.group(1).strip().strip('"').strip("'")
            for m in COO_FIND_MEMBER_RE.finditer(response)
        ]
        if queries:
            asyncio.create_task(self._handle_find_members_batch(queries))

        if not sent_any and not COO_NEXT_CONTACT_RE.search(response) and not queries:
            logger.debug("agent reply with no actionable markers (internal note)")

    async def _handle_find_members_batch(self, queries: list[str]) -> None:
        # Wait briefly so any in-flight CEO reply finishes composing before we
        # paste the bridge response back into Claude's input.
        await asyncio.sleep(8)

        guild = self.get_guild(self.cfg.guild_id)
        if guild is None:
            try:
                guild = await self.fetch_guild(self.cfg.guild_id)
            except Exception:
                logger.exception("could not fetch guild %s", self.cfg.guild_id)
                guild = None

        results: list[tuple[str, list]] = []
        for q in queries:
            members: list = []
            if guild is not None:
                try:
                    members = await guild.query_members(query=q, limit=10)
                except Exception:
                    logger.exception("query_members failed for %r", q)
                if not members:
                    qlow = q.lower()
                    members = [
                        m for m in guild.members
                        if qlow in (m.display_name or "").lower()
                        or qlow in (m.name or "").lower()
                        or qlow in (m.global_name or "").lower()
                    ][:10]
            logger.info("returning %d member match(es) for %r", len(members), q)
            results.append((q, members))

        sections = []
        for q, members in results:
            if not members:
                sections.append(
                    f'query "{q}": no match in the guild. Ask the CEO for an '
                    f"email or other contact; record the name without a Discord ID."
                )
            else:
                lines = [
                    f"  - {m.display_name or m.name} (@{m.name}, discord_user_id={m.id})"
                    for m in members
                ]
                sections.append(
                    f'query "{q}": {len(members)} match(es)\n' + "\n".join(lines)
                )

        notice = (
            "[[BRIDGE_FIND_RESULT]]\n\n"
            + "\n\n".join(sections)
            + "\n\nFor each query: confirm with the CEO which discord_user_id "
            "matches the manager they named. Save the confirmed user_id beside "
            "the manager's record. Reply NOOP if you have nothing else to say "
            "right now; otherwise continue your reply to the CEO."
        )
        await self._send_to_agent(notice, cancel_first=False)

    # ----- operating cadences (proactive rhythm) -----

    async def _cadence_loop(self) -> None:
        """Every 60s, fire any cadences whose next_fire_at has elapsed."""
        await asyncio.sleep(30)  # offset from schedule_loop so they don't sync-storm
        while not self.is_closed():
            try:
                await self._fire_due_cadences()
            except Exception:
                logger.exception("cadence loop error")
            await asyncio.sleep(60)

    async def _fire_due_cadences(self) -> None:
        rows = await asyncio.to_thread(self._claim_due_cadences)
        for row in rows:
            await self._send_cadence_to_agent(row)
            await asyncio.to_thread(self._close_cadence_run, row["run_id"])

    def _claim_due_cadences(self) -> list[dict]:
        """Atomically pick due cadences, compute next fire, open a run row."""
        try:
            from croniter import croniter as _croniter
        except ImportError:
            return []
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)

        conn = _connect(self.cfg.tenant_db)
        try:
            with conn:
                rows = conn.execute(
                    "SELECT id, slug, name, kind, scope_kind, scope_id, cron_expr "
                    "FROM cadences "
                    "WHERE is_active = 1 "
                    "  AND (next_fire_at IS NULL OR next_fire_at <= datetime('now')) "
                    "ORDER BY next_fire_at IS NULL DESC, next_fire_at ASC "
                    "LIMIT 3"
                ).fetchall()
                claimed: list[dict] = []
                for r in rows:
                    try:
                        next_at = (
                            _croniter(r["cron_expr"], now).get_next(datetime)
                            .strftime("%Y-%m-%d %H:%M:%S")
                        )
                    except Exception:
                        logger.exception("bad cron_expr for cadence %s", r["slug"])
                        next_at = None
                    conn.execute(
                        "UPDATE cadences "
                        "SET last_fired_at = datetime('now'), next_fire_at = ? "
                        "WHERE id = ?",
                        (next_at, r["id"]),
                    )
                    cur = conn.execute(
                        "INSERT INTO cadence_runs (cadence_id, started_at, status) "
                        "VALUES (?, datetime('now'), 'running')",
                        (r["id"],),
                    )
                    conn.execute(
                        "INSERT INTO audit_log (actor_kind, action, target_kind, "
                        "  target_id, payload_json) "
                        "VALUES ('system', 'cadence_fired', 'cadence', ?, ?)",
                        (r["id"], json.dumps({"slug": r["slug"], "kind": r["kind"]})),
                    )
                    row = dict(r)
                    row["run_id"] = cur.lastrowid
                    claimed.append(row)
            return claimed
        finally:
            conn.close()

    def _close_cadence_run(self, run_id: int) -> None:
        conn = _connect(self.cfg.tenant_db)
        try:
            with conn:
                conn.execute(
                    "UPDATE cadence_runs "
                    "SET finished_at = datetime('now'), status = 'succeeded' "
                    "WHERE id = ?",
                    (run_id,),
                )
        finally:
            conn.close()

    def _cadence_context(self, kind: str) -> str:
        """Pull relevant DB context for the agent based on cadence kind."""
        conn = _connect(self.cfg.tenant_db)
        try:
            if kind in ("weekly-pulse", "daily-brief"):
                rows = conn.execute(
                    "SELECT c.description, c.due_at, c.status, p.display_name, "
                    "       p.discord_user_id "
                    "FROM commitments c JOIN people p ON p.id = c.person_id "
                    "WHERE c.status = 'open' "
                    "ORDER BY c.due_at IS NULL, c.due_at ASC LIMIT 20"
                ).fetchall()
                if not rows:
                    return "Open commitments: (none recorded)"
                lines = [
                    f"  - {r['display_name']} (uid={r['discord_user_id']}): "
                    f"\"{r['description']}\" due {r['due_at'] or '—'}"
                    for r in rows
                ]
                return "Open commitments:\n" + "\n".join(lines)
            if kind == "monthly-review":
                rows = conn.execute(
                    "SELECT m.slug, m.name, m.unit, m.target_value, m.target_direction, "
                    "       (SELECT value FROM metric_values WHERE metric_id = m.id "
                    "        ORDER BY observed_at DESC LIMIT 1) AS latest, "
                    "       (SELECT observed_at FROM metric_values WHERE metric_id = m.id "
                    "        ORDER BY observed_at DESC LIMIT 1) AS latest_at, "
                    "       (SELECT COUNT(*) FROM metric_values WHERE metric_id = m.id "
                    "        AND is_anomaly = 1) AS anomalies "
                    "FROM metrics m WHERE m.is_active = 1 ORDER BY m.id"
                ).fetchall()
                if not rows:
                    return "Metrics: (none defined yet — agent has no quantitative data)"
                lines = []
                for r in rows:
                    target = (
                        f" target {r['target_direction']} {r['target_value']}"
                        if r["target_value"] is not None else ""
                    )
                    anom = f" [{r['anomalies']} anomalies]" if r["anomalies"] else ""
                    lines.append(
                        f"  - {r['slug']} ({r['name']}): latest "
                        f"{r['latest']}{r['unit'] or ''} at {r['latest_at']}"
                        f"{target}{anom}"
                    )
                return "Metrics snapshot:\n" + "\n".join(lines)
            if kind == "risk-review":
                rows = conn.execute(
                    "SELECT slug, title, likelihood, impact, status, "
                    "       last_reviewed_at, review_cadence "
                    "FROM risks WHERE status IN ('open', 'mitigated') "
                    "ORDER BY last_reviewed_at IS NULL DESC, last_reviewed_at ASC"
                ).fetchall()
                if not rows:
                    return "Risk register: (empty)"
                lines = [
                    f"  - {r['slug']}: {r['title']} "
                    f"[likelihood={r['likelihood']}, impact={r['impact']}, "
                    f"status={r['status']}, last_reviewed={r['last_reviewed_at'] or 'never'}]"
                    for r in rows
                ]
                return "Open risks:\n" + "\n".join(lines)
        finally:
            conn.close()
        return ""

    async def _send_cadence_to_agent(self, row: dict) -> None:
        kind = row["kind"]
        instructions = CADENCE_INSTRUCTIONS.get(
            kind,
            "Take whatever action this cadence is for, based on the kind name.",
        )
        context = await asyncio.to_thread(self._cadence_context, kind)
        notice = (
            f"[[BRIDGE_CADENCE run_id={row['run_id']} kind={kind} "
            f"scope={row['scope_kind']}]]\n\n"
            f"A scheduled operating cadence has fired.\n"
            f"  - Cadence: {row['name']} (slug={row['slug']})\n"
            f"  - Kind:    {kind}\n"
            f"  - Scope:   {row['scope_kind']}\n\n"
            f"{instructions}\n\n"
            + (f"DB snapshot:\n{context}\n\n" if context else "")
            + "Take whatever action is appropriate now (DMs, [[COO_FACT]] / "
            "[[COO_COMMITMENT]] / [[COO_DECISION]] markers to record results, "
            "[[COO_NEXT_CONTACT]] to chase later). Reply NOOP if there's "
            "nothing to do this cycle."
        )
        logger.info(
            "firing cadence slug=%s kind=%s run_id=%s",
            row["slug"], kind, row["run_id"],
        )
        await self._send_to_agent(notice, cancel_first=False)

    async def _schedule_loop(self) -> None:
        """Every 60s, fire any scheduled_contacts that are due."""
        await asyncio.sleep(20)  # let initial mission/intro settle
        while not self.is_closed():
            try:
                await self._fire_due_contacts()
            except Exception:
                logger.exception("schedule loop error")
            await asyncio.sleep(60)

    async def _fire_due_contacts(self) -> None:
        """Pull pending rows whose fire_at has elapsed; nudge the agent for each."""
        rows = await asyncio.to_thread(self._claim_due_contacts)
        for row in rows:
            await self._send_nudge_to_agent(row)

    def _claim_due_contacts(self) -> list[dict]:
        """Atomically claim due rows by flipping pending -> fired in one txn."""
        conn = _connect(self.cfg.tenant_db)
        try:
            with conn:
                rows = conn.execute(
                    "SELECT sc.id, sc.person_id, sc.reason, sc.fire_at, "
                    "       p.discord_user_id, p.display_name, p.role "
                    "FROM scheduled_contacts sc "
                    "JOIN people p ON p.id = sc.person_id "
                    "WHERE sc.status = 'pending' "
                    "  AND sc.fire_at <= datetime('now') "
                    "  AND p.deleted_at IS NULL "
                    "  AND p.discord_user_id IS NOT NULL "
                    "ORDER BY sc.fire_at ASC "
                    "LIMIT 5"
                ).fetchall()
                ids = [r["id"] for r in rows]
                for rid in ids:
                    conn.execute(
                        "UPDATE scheduled_contacts "
                        "SET status = 'fired', fired_at = datetime('now') "
                        "WHERE id = ? AND status = 'pending'",
                        (rid,),
                    )
                    conn.execute(
                        "INSERT INTO audit_log (actor_kind, action, target_kind, "
                        "  target_id, payload_json) "
                        "VALUES ('system', 'scheduled_contact_fired', "
                        "  'scheduled_contact', ?, ?)",
                        (rid, json.dumps({"reason": next(r["reason"] for r in rows if r["id"] == rid)})),
                    )
            return [dict(r) for r in rows]
        finally:
            conn.close()

    async def _send_nudge_to_agent(self, row: dict) -> None:
        uid = int(row["discord_user_id"])
        notice = (
            f"[[BRIDGE_NUDGE user_id={uid}]]\n\n"
            f"A scheduled contact you set has come due.\n"
            f"  - Person:  {row['display_name']} (role: {row['role'] or 'unknown'})\n"
            f"  - Reason:  {row['reason']}\n"
            f"  - Was due: {row['fire_at']}\n\n"
            "Decide what to do now: send a DM with `[[COO_TO user_id=...]] ...` "
            "if it's time to follow up, or reply NOOP if no action is needed. "
            "You may also chain another `[[COO_NEXT_CONTACT ...]]` if you want "
            "to push the follow-up further out."
        )
        logger.info(
            "firing scheduled_contact id=%s person_id=%s reason=%s",
            row["id"], row["person_id"], row["reason"],
        )
        await self._send_to_agent(notice, cancel_first=False)

    def _record_scheduled_contact(self, uid: int, secs: int, reason: str) -> None:
        tconn = _connect(self.cfg.tenant_db)
        try:
            person_row = tconn.execute(
                "SELECT id FROM people WHERE discord_user_id = ?", (uid,)
            ).fetchone()
            if not person_row:
                logger.warning("scheduled contact for unknown uid=%s; skipping", uid)
                return
            # Freshness dedup: same (person, reason) within 60s is a re-emission
            # of the same marker, not a real new schedule.
            recent = tconn.execute(
                "SELECT id FROM scheduled_contacts "
                "WHERE person_id = ? AND reason = ? "
                "AND created_at > datetime('now', '-60 seconds')",
                (person_row["id"], reason),
            ).fetchone()
            if recent:
                logger.debug(
                    "dedup: skipping duplicate scheduled_contact uid=%s reason=%r",
                    uid, reason,
                )
                return
            tconn.execute(
                "INSERT INTO scheduled_contacts (person_id, fire_at, reason, status) "
                "VALUES (?, datetime('now', ?), ?, 'pending')",
                (person_row["id"], f"+{secs} seconds", reason),
            )
            tconn.commit()
            logger.info("scheduled contact uid=%s in %ss: %s", uid, secs, reason)
        finally:
            tconn.close()


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logging.getLogger("discord").setLevel(logging.WARNING)


def main() -> None:
    configure_logging()
    cfg = Config.from_env()
    cfg.state_dir.mkdir(parents=True, exist_ok=True)

    bot = COOBot(cfg)
    bot.run(cfg.bot_token, log_handler=None)


if __name__ == "__main__":
    main()
