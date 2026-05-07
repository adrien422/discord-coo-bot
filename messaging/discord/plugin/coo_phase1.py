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
from pathlib import Path

import discord

logger = logging.getLogger("coo_phase1")

PASTED_INPUT_RE = re.compile(r"\[Pasted text #\d+ \+\d+ lines\]")
COO_TO_RE = re.compile(r"\[\[COO_TO user_id=(\d+)\]\]\s*(.+?)(?=(?:\[\[COO_|$))", re.S)


def normalize_message_text(text: str) -> str:
    """Strip Claude Code's TUI word-wrap from a message body.

    The TUI emits lines like:
        Got it — channel manager for short-term
         rentals, you and Adrien. Two quick ones...
    where every continuation line is indented 1-3 spaces. Capturing the pane
    preserves those line breaks. We need to collapse them back into prose.
    Real paragraph breaks (blank line) are preserved.
    """
    paragraphs = re.split(r"\n\s*\n", text.strip())
    cleaned = [re.sub(r"\s+", " ", p).strip() for p in paragraphs]
    return "\n\n".join(p for p in cleaned if p)
COO_NEXT_CONTACT_RE = re.compile(
    r"\[\[COO_NEXT_CONTACT user_id=(\d+) in_seconds=(\d+) reason=([^\]]+)\]\]"
)
COO_CLOSE_RE = re.compile(r"\[\[COO_CLOSE\]\]")
NOOP_RE = re.compile(r"^\s*●?\s*NOOP\s*$", re.M)


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
    """Allowlist = platform developers + tenant CEO + tenant content_approvers.

    Returns a map: discord_user_id -> {handle, name, role}
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
        }
    pconn.close()

    tconn = _connect(cfg.tenant_db)
    for r in tconn.execute(
        "SELECT discord_user_id, slug, display_name, role, is_content_approver "
        "FROM people WHERE deleted_at IS NULL AND discord_user_id IS NOT NULL"
    ).fetchall():
        if r["is_content_approver"] or int(r["discord_user_id"]) == cfg.ceo_user_id:
            out[int(r["discord_user_id"])] = {
                "handle": r["slug"],
                "name": r["display_name"],
                "role": r["role"] or "ceo",
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

    def send_prompt(self, text: str) -> None:
        """Type text into the agent pane and submit it.

        Claude Code uses bracketed-paste mode; sending Enter immediately after
        a multi-line paste gets absorbed by the paste sequence. We send the
        text, sleep, send Enter (closes paste), sleep, send Enter (submits).
        """
        if not self._session_exists():
            raise RuntimeError("tmux session is gone; cannot send prompt")
        # Cancel any pending modal / partial input.
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

        marker = list(re.finditer(r"^●\s+(.+)$", text, flags=re.M))
        if not marker:
            return None

        last_idx = marker[-1].start()
        tail = text[last_idx:]
        cut = re.search(r"^(✻|✶|⏵|❯)", tail, flags=re.M)
        response = tail[: cut.start() if cut else len(tail)]
        response = re.sub(r"^●\s*", "", response).strip()

        if not response or NOOP_RE.match(response):
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
    dev_lines = "\n".join(f"  - {d['name']} ({d['handle']}, developer)" for d in devs) or "  (none registered)"

    return f"""You are the persistent COO agent for **{company_name}**.

Your conversation surface is Discord (this session is bridged to it via tmux).
The Discord listener forwards any DM from an allowlisted person to you, and
relays anything you say in this pane back to them.

# Allowlist (Phase 1)

You may converse with these people only:

  - {ceo['display_name']} (Discord user_id={cfg.ceo_user_id}, role: {ceo.get('role') or 'CEO'}, content authority for {company_name})
{dev_lines}

Anyone else's DMs are saved to an inbox you cannot see; do not address them.

# Mission

You are in **Phase 1 — TOP-DOWN company mapping**. Phase 1 is structural,
not staffing. Your job is to learn the SHAPE of the company from the CEO,
not every individual employee.

What to learn from the CEO in Phase 1:

  1. **Departments / functional areas** (e.g. Engineering, Sales, Ops, Support).
  2. **The manager who leads each department** — their name, role, and how
     to reach them (Discord ID, email, or other identifier the CEO knows).
  3. **Top company priorities right now** (the 3-5 things that matter most
     this quarter).
  4. **The most important workflows or processes** at company-level (3-5,
     e.g. "how a new customer gets onboarded", "how releases ship").

What NOT to ask the CEO in Phase 1:

  - Do NOT ask the CEO to list or enumerate every individual employee.
  - Do NOT ask the CEO for staff-level detail under each manager. That is
    Phase 2 work.

If a department has only the CEO + a few co-founders, that's fine — record
them. But for any department with a real manager, your map for THAT department
ends at "manager = Name" until Phase 2 unlocks.

Phase 1 deliverables:
  - Per-department factsheet (lead manager, top priorities, main workflows).
  - Per-manager factsheet (name, role, department).
  - Org chart at the company → department → manager level.
  - Top company priorities.

When the top-level map feels complete (departments + their managers + priorities
+ main workflows), STOP interviewing the CEO. Then send Dan and Adrien (the
developers) a Phase 2 unlock proposal listing the managers you'd interview
next, in priority order, with reasoning.

Conversation operations:
  - Self-pace your follow-ups using `[[COO_NEXT_CONTACT]]` markers.
  - Keep messages short — Discord, not email.
  - Plain prose, no markdown headings, no numbered checklists, no bullet lists
    inside DMs. Discord renders bullets fine but they read like a survey form.
    Ask one or two crisp questions per message.

# How to send a DM

To send a DM to anyone in the allowlist, emit a line of the form:

    [[COO_TO user_id=<discord_user_id>]] <your message text>

Example:

    [[COO_TO user_id={cfg.ceo_user_id}]] Hi {ceo['display_name'].split()[0]}, I'm the COO agent for {company_name}. Mind if I take 20 minutes to map how the company runs?

Multiple `[[COO_TO ...]]` blocks in one response are allowed. Plain text
without a `[[COO_TO ...]]` prefix is treated as internal notes and is NOT
sent to anyone.

# Self-pacing

After any meaningful exchange decide when (in seconds) you would like to
follow up next. Emit:

    [[COO_NEXT_CONTACT user_id=<id> in_seconds=<int> reason=<short>]]

The bridge schedules the nudge and re-prompts you at the right time.

# Markers

  - `[[COO_CLOSE]]` — close the current DM thread with that person
  - `[[COO_HOLD]]` — park the thread without action
  - `NOOP` — valid full reply when there is nothing to do

# Hard rules

  - Do not reveal tokens, credentials, or any content of secrets files.
  - Do not hire, fire, set comp, sign contracts, or commit budget.
  - Phase 2 (managers) and Phase 3 (staff) are LOCKED. Only Dan or Adrien
    can unlock them. The CEO cannot.
  - If a developer (Dan, Adrien) tells you to do something, that command
    has the highest weight.

# First action

The CEO has not yet heard from you. Your first action in this session is to
DM the CEO with a short, warm introduction (1-3 sentences) and a single
opening question to start the company map. Use the `[[COO_TO user_id={cfg.ceo_user_id}]]` form.
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
        self.first_start_marker = cfg.state_dir / "first_start_done"
        self._delivered_path = cfg.state_dir / "delivered.json"
        self._last_delivered: dict[int, str] = self._load_delivered()

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

    async def _send_initial_mission(self) -> None:
        prompt = mission_prompt(self.cfg, self.allowlist, self.ceo, self.company)
        logger.info("Sending initial mission prompt to agent")
        await asyncio.to_thread(self.bridge.send_prompt, prompt)

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
        await asyncio.to_thread(self.bridge.send_prompt, amendment)

    async def on_message(self, message: discord.Message) -> None:
        if message.author == self.user:
            return
        if not isinstance(message.channel, discord.DMChannel):
            return  # Phase 1: DM-only

        sender_id = message.author.id
        content = message.content or ""

        if sender_id in self.allowlist:
            sender = self.allowlist[sender_id]
            logger.info("DM from allowlisted %s (uid=%s)", sender["name"], sender_id)
            prompt = relay_prompt(sender, sender_id, content)
            await asyncio.to_thread(self.bridge.send_prompt, prompt)
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
            except Exception:
                logger.exception("failed to DM uid=%s", target_id)

        for m in COO_NEXT_CONTACT_RE.finditer(response):
            uid, secs, reason = int(m.group(1)), int(m.group(2)), m.group(3).strip()
            self._record_scheduled_contact(uid, secs, reason)

        if not sent_any and not COO_NEXT_CONTACT_RE.search(response):
            logger.debug("agent reply with no COO_TO markers (internal note)")

    def _record_scheduled_contact(self, uid: int, secs: int, reason: str) -> None:
        tconn = _connect(self.cfg.tenant_db)
        try:
            person_row = tconn.execute(
                "SELECT id FROM people WHERE discord_user_id = ?", (uid,)
            ).fetchone()
            if not person_row:
                logger.warning("scheduled contact for unknown uid=%s; skipping", uid)
                return
            fire_at = (
                "datetime('now', ?)"
            )
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
