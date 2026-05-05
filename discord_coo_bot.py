#!/usr/bin/env python3
"""Discord COO bridge for the VPS.

One durable Discord-facing operations agent backed by a single tmux CLI
session. Discord is the workplace surface; the VPS session owns tools,
browser control, files, and future Workspace/accounting integrations.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
import signal
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import aiohttp


BOT_TOKEN = os.environ["DISCORD_CLAUDEX_BOT_TOKEN"]
APPLICATION_ID = os.environ.get("DISCORD_CLAUDEX_APPLICATION_ID", "1499160835698855966")
GUILD_ID = os.environ.get("DISCORD_COO_GUILD_ID", "1499169248402997379")
HOME_CHANNEL_ID = os.environ.get("DISCORD_COO_HOME_CHANNEL_ID", "1499169249766277222")
BASE_CHANNEL_IDS = {
    c.strip()
    for c in os.environ.get("DISCORD_COO_CHANNEL_IDS", HOME_CHANNEL_ID).split(",")
    if c.strip()
}
ADMIN_USER_IDS = {
    c.strip()
    for c in os.environ.get("DISCORD_COO_ADMIN_USER_IDS", "").split(",")
    if c.strip()
}
ADMIN_CHANNEL_IDS = {
    c.strip()
    for c in os.environ.get("DISCORD_COO_ADMIN_CHANNEL_IDS", "").split(",")
    if c.strip()
}

PREFIX = os.environ.get("DISCORD_COO_PREFIX", "!coo")
AGENT_KIND = os.environ.get("DISCORD_COO_AGENT_KIND", "codex").strip().lower()
PROACTIVE_INTERVAL = float(os.environ.get("DISCORD_COO_PROACTIVE_INTERVAL_SECONDS", "3600"))
INBOX_MONITOR_INTERVAL = float(os.environ.get("DISCORD_COO_INBOX_MONITOR_INTERVAL_SECONDS", "300"))
INBOX_ATTENTION_COOLDOWN_SECONDS = float(os.environ.get("DISCORD_COO_INBOX_ATTENTION_COOLDOWN_SECONDS", "300"))
INBOX_ATTENTION_BATCH_SIZE = int(os.environ.get("DISCORD_COO_INBOX_ATTENTION_BATCH_SIZE", "8"))
CONVERSATION_MODE = os.environ.get("DISCORD_COO_CONVERSATION_MODE", "bot_owned").strip().lower()
CONVERSATION_TTL_SECONDS = float(os.environ.get("DISCORD_COO_CONVERSATION_TTL_SECONDS", "86400"))
UNSOLICITED_ACK_COOLDOWN_SECONDS = float(os.environ.get("DISCORD_COO_UNSOLICITED_ACK_COOLDOWN_SECONDS", "900"))
TMUX_SESSION = os.environ.get("DISCORD_COO_TMUX_SESSION", "discord_coo")
TMUX_WINDOW = os.environ.get("DISCORD_COO_TMUX_WINDOW", "agent")
TMUX_TARGET = f"{TMUX_SESSION}:{TMUX_WINDOW}.0"
WORKDIR = Path(os.environ.get("DISCORD_COO_WORKDIR", "/home/arman/workbench/discord-coo-workspace"))
STATE_DIR = Path(os.environ.get("DISCORD_COO_STATE_DIR", "/home/arman/workbench/.discord_coo_state"))
REFERENCE_DIR = Path(os.environ.get("DISCORD_COO_REFERENCE_DIR", str(WORKDIR / "reference" / "inbox")))
TRANSCRIPT_DIR = Path(os.environ.get("DISCORD_COO_TRANSCRIPT_DIR", str(WORKDIR / "reference" / "transcripts")))
FACTSHEET_DIR = Path(os.environ.get("DISCORD_COO_FACTSHEET_DIR", str(WORKDIR / "reference" / "factsheets")))
STATE_FILE = STATE_DIR / "state.json"
EVENT_LOG = STATE_DIR / "events.jsonl"
RUN_AI = os.environ.get("DISCORD_COO_RUN_AI", "/home/arman/workbench/run_ai_persistent.sh")
CODEX_SESSIONS = Path.home() / ".codex" / "sessions"
CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"

DISCORD_API = "https://discord.com/api/v10"
CONTROL_CLOSE = "[[COO_CLOSE]]"
CONTROL_HOLD = "[[COO_HOLD]]"
CONTROL_NO_ACTION = "[[COO_NO_ACTION]]"
MENTION_RE = re.compile(r"<@!?(\d+)>")
REFERENCE_WORKFLOW_STATES = ("pending", "queued", "held", "no-action", "initiated", "failed", "attended")
REFERENCE_QUEUEABLE_STATES = ("pending", "failed")
REFERENCE_STATE_BUTTONS = (
    ("Pending", "pending", 2),
    ("Queued", "queued", 2),
    ("Held", "held", 2),
    ("No Action", "no-action", 2),
    ("Initiated", "initiated", 2),
    ("Failed", "failed", 4),
)
INTERACTION_APPLICATION_COMMAND = 2
INTERACTION_MESSAGE_COMPONENT = 3
INTERACTION_MODAL_SUBMIT = 5
INTERACTION_RESPONSE_CHANNEL_MESSAGE = 4
INTERACTION_RESPONSE_UPDATE_MESSAGE = 7
INTERACTION_RESPONSE_MODAL = 9
EPHEMERAL_FLAG = 64
GATEWAY_INTENTS = (
    1      # GUILDS
    | 512  # GUILD_MESSAGES
    | 1024 # GUILD_MESSAGE_REACTIONS
    | 4096 # DIRECT_MESSAGES
    | 32768 # MESSAGE_CONTENT
)

COO_MISSION = f"""You are Claudex COO, a persistent operations agent living in Discord for Arman's work.

You are one long-running self-managing session on the VPS, not a disposable support bot.
You coordinate departments, track open loops, ask follow-up questions, remind people, summarize work,
and escalate uncertainty to Arman or the responsible manager. You own the conversation lifecycle:
normal employees may reply when you have opened a loop with them, but they do not start new work.
Normal employees must not be able to stop, reset, or steer the agent outside an open COO loop.

Available context:
- Discord messages arrive with tags containing guild_id, channel_id, author, author_id, and message_id.
- You may use VPS tools, files, browser automation, and future Google Workspace/accounting credentials as they are added.
- Current home Discord channel: {HOME_CHANNEL_ID}.
- Unsolicited employee messages are not injected directly. They are saved under {REFERENCE_DIR}.
- Daily per-channel transcripts are saved under {TRANSCRIPT_DIR}.
- Room factsheets are saved under {FACTSHEET_DIR}.

Operating rules:
- Be concise in Discord. Use direct operational language.
- Track owners, deadlines, blockers, and decisions.
- Before initiating loops during scheduled/manual pulses, inspect the saved employee reference inbox when useful.
- To start a department or employee loop, send a clear Discord message. Mention specific users with <@user_id> when known.
- The bridge only injects human messages that reply to a COO bot message. In admin rooms, the human message must both reply to a COO bot message and mention you.
- If your final response finishes the loop and employees should no longer continue it, include {CONTROL_CLOSE}; the bridge strips this marker before sending.
- When you read saved reference messages but intentionally hold/defer them with no outward action, include {CONTROL_HOLD}; the bridge strips this marker and marks those references held.
- When you read saved reference messages and decide no action is needed, include {CONTROL_NO_ACTION}; the bridge strips this marker and marks those references no-action.
- When a scheduled COO pulse arrives and no public action is useful, reply exactly NOOP.
- Do not reveal secrets, tokens, cookies, or private credentials in Discord.
- If you need a browser/account/tool that is not wired yet, say exactly what credential or setup is missing.
"""


def classify_codex_pane_text(text: str) -> str:
    """Classify Codex TUI text captured from tmux."""
    if not text.strip():
        return "starting"
    visible_lines = [line for line in text.splitlines() if line.strip()]
    bottom = "\n".join(visible_lines[-16:])
    bottom_low = bottom.lower()
    has_prompt = any(line.lstrip().startswith("›") for line in bottom.splitlines())
    if "update available" in bottom_low and "press enter to continue" in bottom_low:
        return "codex_update_prompt"
    if "switch to gpt" in bottom_low and "keep current model" in bottom_low and "press enter" in bottom_low:
        return "codex_model_switch_prompt"
    if "esc to interrupt" in bottom_low or "working (" in bottom_low:
        return "busy"
    if "messages to be submitted after next tool call" in bottom_low:
        return "busy"
    if has_prompt:
        return "input_ready"
    if "openai codex" in text.lower():
        return "busy"
    return "starting"


def classify_claude_pane_text(text: str) -> str:
    """Classify Claude Code TUI text captured from tmux."""
    if not text.strip():
        return "starting"
    lower = text.lower()
    if "quick safety check" in lower or "trust this folder" in lower or "trust the files" in lower:
        return "trust_prompt"
    visible_lines = [line for line in text.splitlines() if line.strip()]
    wider = "\n".join(visible_lines[-30:])
    wider_low = wider.lower()
    has_claude_box = "\u276f" in wider and ("bypass permissions" in wider_low or "shift+tab" in wider_low)
    if has_claude_box:
        if "esc to int" in wider_low or "esc to interrupt" in wider_low:
            return "busy"
        return "input_ready"
    if "bypass permissions on" in lower or "welcome to claude code" in lower:
        return "busy"
    return "starting"


@dataclass
class AgentPrompt:
    text: str
    channel_id: str
    message_id: str | None = None
    author_id: str | None = None
    kind: str = "message"
    queued_at: float = 0.0
    reference_message_ids: list[str] | None = None


class DiscordCOO:
    def __init__(self) -> None:
        self.log = logging.getLogger("discord_coo")
        self.http: aiohttp.ClientSession | None = None
        self.seq: int | None = None
        self.session_id: str | None = None
        self.resume_gateway_url: str | None = None
        self.bot_user_id: str | None = None
        self.queue: asyncio.Queue[AgentPrompt] = asyncio.Queue()
        self.state: dict[str, Any] = self._load_state()
        self.admin_user_ids = set(ADMIN_USER_IDS)
        self.started_at = time.time()
        self.stop_event = asyncio.Event()
        self.channel_names: dict[str, str] = {}

    def _load_state(self) -> dict[str, Any]:
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            return {"offsets": {}, "active_channel_id": HOME_CHANNEL_ID}

    def _save_state(self) -> None:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=2, sort_keys=True))
        tmp.replace(STATE_FILE)

    def _event(self, event_kind: str, **data: Any) -> None:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        if "kind" in data:
            data["payload_kind"] = data.pop("kind")
        row = {"ts": time.time(), "kind": event_kind, **data}
        with EVENT_LOG.open("a") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    async def run(self) -> None:
        self._prepare_workspace()
        self._init_existing_forwarder_offsets()
        created = self.ensure_agent_session()
        async with aiohttp.ClientSession(headers={"User-Agent": "ClaudexDiscordCOO/0.1"}) as session:
            self.http = session
            await self.refresh_application_admins()
            await self.refresh_channel_names()
            if created or not self.state.get("mission_sent_at"):
                await self.queue.put(AgentPrompt(
                    text=COO_MISSION,
                    channel_id=self.home_channel_id(),
                    kind="mission",
                    queued_at=time.time(),
                ))
            tasks = [
                asyncio.create_task(self.agent_worker(), name="agent_worker"),
                asyncio.create_task(self.agent_forwarder(), name="agent_forwarder"),
                asyncio.create_task(self.proactive_loop(), name="proactive_loop"),
                asyncio.create_task(self.inbox_monitor_loop(), name="inbox_monitor_loop"),
                asyncio.create_task(self.gateway_loop(), name="gateway_loop"),
            ]
            try:
                await self.stop_event.wait()
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    def _prepare_workspace(self) -> None:
        WORKDIR.mkdir(parents=True, exist_ok=True)
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        REFERENCE_DIR.mkdir(parents=True, exist_ok=True)
        TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
        FACTSHEET_DIR.mkdir(parents=True, exist_ok=True)
        agents = WORKDIR / "AGENTS.md"
        if not agents.exists():
            agents.write_text(
                "# Discord COO Workspace\n\n"
                "- This directory is the persistent working directory for the Discord COO agent.\n"
                "- Keep operational notes, task ledgers, and integration scratch files here.\n"
                f"- Employee-originated Discord reference messages are saved under `{REFERENCE_DIR}`.\n"
                f"- Daily Discord transcripts are saved under `{TRANSCRIPT_DIR}`.\n"
                f"- Weekly and monthly room factsheets are saved under `{FACTSHEET_DIR}`.\n"
                "- Do not store plaintext secrets here; use `/home/arman/workbench/.discord_claudex.secrets` and future `0600` secret files.\n"
            )

    def ensure_agent_session(self) -> bool:
        if AGENT_KIND not in {"codex", "claude"}:
            raise RuntimeError("DISCORD_COO_AGENT_KIND must be codex or claude")
        if self._tmux_session_exists(TMUX_SESSION):
            actual = self.detect_existing_agent_kind()
            if actual and actual != AGENT_KIND:
                subprocess.run(["tmux", "kill-session", "-t", TMUX_SESSION], timeout=10, check=False)
            else:
                self.state["agent_kind"] = AGENT_KIND
                self._save_state()
                return False
        return self.start_agent_session()

    def start_agent_session(self) -> bool:
        cmd = f"cd {shlex.quote(str(WORKDIR))} && exec {shlex.quote(RUN_AI)} {shlex.quote(AGENT_KIND)}"
        subprocess.run(
            ["tmux", "new-session", "-d", "-s", TMUX_SESSION, "-n", TMUX_WINDOW, "-c", str(WORKDIR), cmd],
            check=True,
            timeout=10,
        )
        self.state["agent_kind"] = AGENT_KIND
        self._save_state()
        self._event("agent_session_created", tmux_session=TMUX_SESSION, workdir=str(WORKDIR), agent_kind=AGENT_KIND)
        return True

    def detect_existing_agent_kind(self) -> str | None:
        try:
            text = self.capture_pane(500).lower()
        except Exception:
            return None
        if "openai codex" in text or "codex" in text:
            return "codex"
        if "claude code" in text or "bypass permissions" in text or "anthropic" in text:
            return "claude"
        return None

    @staticmethod
    def _tmux_session_exists(name: str) -> bool:
        return subprocess.run(["tmux", "has-session", "-t", name], capture_output=True).returncode == 0

    @staticmethod
    def capture_pane(lines: int = 160) -> str:
        result = subprocess.run(
            ["tmux", "capture-pane", "-p", "-t", TMUX_TARGET, "-S", f"-{lines}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.stdout or ""

    def pane_state(self) -> str:
        try:
            text = self.capture_pane(80)
        except Exception:
            return "unknown"
        if AGENT_KIND == "claude":
            return classify_claude_pane_text(text)
        return classify_codex_pane_text(text)

    async def wait_for_input_ready(self) -> None:
        while not self.stop_event.is_set():
            state = self.pane_state()
            if state == "input_ready":
                return
            if state == "trust_prompt":
                subprocess.run(["tmux", "send-keys", "-t", TMUX_TARGET, "Enter"], timeout=5, check=False)
                await asyncio.sleep(0.5)
                continue
            if state == "codex_update_prompt":
                subprocess.run(["tmux", "send-keys", "-t", TMUX_TARGET, "3", "Enter"], timeout=5, check=False)
                await asyncio.sleep(0.5)
                continue
            if state == "codex_model_switch_prompt":
                subprocess.run(["tmux", "send-keys", "-t", TMUX_TARGET, "3", "Enter"], timeout=5, check=False)
                await asyncio.sleep(0.5)
                continue
            await asyncio.sleep(0.5)
        raise asyncio.CancelledError

    async def send_to_agent(self, prompt: str) -> None:
        await self.wait_for_input_ready()
        subprocess.run(["tmux", "send-keys", "-t", TMUX_TARGET, "-l", prompt], timeout=10, check=True)
        await asyncio.sleep(0.25)
        subprocess.run(["tmux", "send-keys", "-t", TMUX_TARGET, "Enter"], timeout=5, check=True)

    async def agent_worker(self) -> None:
        while not self.stop_event.is_set():
            item = await self.queue.get()
            try:
                self.state["active_channel_id"] = item.channel_id
                self.state["active_message_id"] = item.message_id
                self.state["active_author_id"] = item.author_id
                self.state["last_prompt_kind"] = item.kind
                self.state["last_prompt_sent_at"] = time.time()
                self.state["active_reference_message_ids"] = item.reference_message_ids or []
                self._save_state()
                self._event("agent_prompt_send", prompt_kind=item.kind, channel_id=item.channel_id, message_id=item.message_id)
                await self.send_to_agent(item.text)
                if item.kind == "mission":
                    self.state["mission_sent_at"] = time.time()
                    self._save_state()
            except Exception as exc:
                self.log.exception("agent send failed")
                await self.send_discord(
                    item.channel_id,
                    f"COO bridge failed to inject the message into the agent: `{type(exc).__name__}: {exc}`",
                    reference_message_id=item.message_id,
                )
                if item.reference_message_ids:
                    self.mark_reference_messages(item.reference_message_ids, "failed", error=f"{type(exc).__name__}: {exc}")
            finally:
                self.queue.task_done()

    async def proactive_loop(self) -> None:
        if PROACTIVE_INTERVAL <= 0:
            return
        while not self.stop_event.is_set():
            await asyncio.sleep(PROACTIVE_INTERVAL)
            if await self.queue_reference_attention(
                self.state.get("active_channel_id") or self.home_channel_id(),
                None,
                trigger="scheduled_pulse",
                respect_cooldown=False,
            ):
                continue
            prompt = (
                "[scheduled COO pulse]\n"
                "Review your current open loops, known owners, deadlines, and blocked work. "
                f"Check the saved employee reference inbox at {REFERENCE_DIR} if it may contain new signals. "
                "If no public Discord action is useful right now, reply exactly NOOP. "
                "If action is useful, send the concise message that should appear in Discord."
            )
            await self.queue.put(AgentPrompt(
                text=prompt,
                channel_id=self.state.get("active_channel_id") or self.home_channel_id(),
                kind="scheduled_pulse",
                queued_at=time.time(),
            ))
            self._event("scheduled_pulse_queued")

    async def inbox_monitor_loop(self) -> None:
        if INBOX_MONITOR_INTERVAL <= 0:
            return
        while not self.stop_event.is_set():
            await asyncio.sleep(INBOX_MONITOR_INTERVAL)
            try:
                await self.queue_reference_attention(
                    self.home_channel_id(),
                    None,
                    trigger="inbox_monitor",
                    respect_cooldown=True,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                self.log.exception("inbox monitor failed")

    async def refresh_channel_names(self) -> None:
        data = await self.discord_get(f"/guilds/{GUILD_ID}/channels")
        if isinstance(data, list):
            self.channel_names = {str(c.get("id")): str(c.get("name") or c.get("id")) for c in data}

    async def refresh_application_admins(self) -> None:
        data = await self.discord_get("/oauth2/applications/@me")
        discovered: set[str] = set()
        owner = data.get("owner") or {}
        if owner.get("id"):
            discovered.add(str(owner["id"]))
        team = data.get("team") or {}
        for member in team.get("members") or []:
            user = member.get("user") or {}
            if user.get("id"):
                discovered.add(str(user["id"]))
        self.admin_user_ids.update(discovered)
        self.state["application_admin_user_ids"] = sorted(discovered)
        self._save_state()
        self.log.info("Loaded %d Discord COO admin id(s)", len(self.admin_user_ids))

    async def gateway_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                gateway = await self.discord_get("/gateway/bot")
                url = gateway.get("url", "wss://gateway.discord.gg")
                await self.gateway_once(url)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.log.exception("gateway loop failed; reconnecting")
                await asyncio.sleep(5)

    async def gateway_once(self, gateway_url: str) -> None:
        assert self.http is not None
        ws_url = f"{gateway_url}/?v=10&encoding=json"
        async with self.http.ws_connect(ws_url, heartbeat=None, timeout=30) as ws:
            hello = await ws.receive_json()
            interval_ms = float(hello["d"]["heartbeat_interval"])
            heartbeat_task = asyncio.create_task(self.heartbeat_loop(ws, interval_ms / 1000.0))
            identify = {
                "op": 2,
                "d": {
                    "token": BOT_TOKEN,
                    "intents": GATEWAY_INTENTS,
                    "properties": {
                        "os": "linux",
                        "browser": "claudex-vps",
                        "device": "claudex-vps",
                    },
                },
            }
            await ws.send_json(identify)
            try:
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        payload = json.loads(msg.data)
                        await self.handle_gateway_payload(payload, ws)
                    elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        break
            finally:
                heartbeat_task.cancel()
                await asyncio.gather(heartbeat_task, return_exceptions=True)

    async def heartbeat_loop(self, ws: aiohttp.ClientWebSocketResponse, interval: float) -> None:
        while True:
            await asyncio.sleep(interval)
            await ws.send_json({"op": 1, "d": self.seq})

    async def handle_gateway_payload(self, payload: dict[str, Any], ws: aiohttp.ClientWebSocketResponse) -> None:
        if payload.get("s") is not None:
            self.seq = int(payload["s"])
        op = payload.get("op")
        if op == 0:
            event_type = payload.get("t")
            data = payload.get("d") or {}
            if event_type == "READY":
                self.session_id = data.get("session_id")
                self.resume_gateway_url = data.get("resume_gateway_url")
                self.bot_user_id = str((data.get("user") or {}).get("id") or "")
                self.log.info("Discord gateway READY as %s", data.get("user", {}).get("username"))
            elif event_type == "MESSAGE_CREATE":
                await self.handle_message(data)
            elif event_type == "INTERACTION_CREATE":
                await self.handle_interaction(data)
        elif op == 7:
            await ws.close()
        elif op == 9:
            self.session_id = None
            await asyncio.sleep(2)
            await ws.close()
        elif op == 11:
            return

    async def handle_message(self, data: dict[str, Any]) -> None:
        guild_id = str(data.get("guild_id") or "")
        channel_id = str(data.get("channel_id") or "")
        author = data.get("author") or {}
        author_id = str(author.get("id") or "")
        if author.get("bot"):
            return
        if guild_id != GUILD_ID:
            return
        if channel_id not in self.channel_ids():
            return
        content = str(data.get("content") or "").strip()
        message_id = str(data.get("id") or "")
        if not content and not data.get("attachments"):
            return
        self.save_daily_transcript(data, direction="inbound")

        if content.startswith(PREFIX):
            await self.handle_command(data, content[len(PREFIX):].strip())
            return

        is_admin_user = self.is_admin(author_id)
        allowed, reject_reason = self.message_may_reach_agent(data)
        if CONVERSATION_MODE == "bot_owned" and not allowed:
            await self.handle_reference_only_message(data, reject_reason)
            return

        prompt = self.build_agent_prompt(
            data,
            conversation_kind="manager_seed" if is_admin_user else "employee_reply_to_coo_open_loop",
        )
        was_busy = self.pane_state() != "input_ready"
        await self.queue.put(AgentPrompt(
            text=prompt,
            channel_id=channel_id,
            message_id=message_id,
            author_id=author_id,
            kind="discord_message_admin" if is_admin_user else "discord_message_employee_reply",
            queued_at=time.time(),
        ))
        self._event("discord_message_queued", channel_id=channel_id, message_id=message_id, author_id=author_id)
        await self.add_reaction(channel_id, message_id, "👀")
        if was_busy or self.queue.qsize() > 1:
            await self.send_discord(
                channel_id,
                f"Queued for the COO agent. Service queue: {self.queue.qsize()}.",
                reference_message_id=message_id,
            )

    async def handle_interaction(self, data: dict[str, Any]) -> None:
        if str(data.get("guild_id") or "") != GUILD_ID:
            return
        channel_id = str(data.get("channel_id") or "")
        user_id = self.interaction_user_id(data)
        interaction_type = int(data.get("type") or 0)
        try:
            if interaction_type == INTERACTION_APPLICATION_COMMAND:
                await self.handle_application_command(data, user_id, channel_id)
            elif interaction_type == INTERACTION_MESSAGE_COMPONENT:
                await self.handle_component(data, user_id, channel_id)
            elif interaction_type == INTERACTION_MODAL_SUBMIT:
                await self.handle_modal_submit(data, user_id, channel_id)
        except Exception as exc:
            self.log.exception("interaction failed")
            await self.respond_interaction(
                data,
                f"COO cockpit failed: `{type(exc).__name__}: {exc}`",
                ephemeral=True,
            )

    async def handle_application_command(self, data: dict[str, Any], user_id: str, channel_id: str) -> None:
        command = data.get("data") or {}
        if command.get("name") != "coo":
            await self.respond_interaction(data, "Unknown COO command.", ephemeral=True)
            return
        options = command.get("options") or []
        subcommand = str((options[0] if options else {}).get("name") or "cockpit")
        if subcommand == "cockpit":
            await self.respond_interaction(
                data,
                self.cockpit_text(channel_id),
                embeds=self.cockpit_embeds(user_id, channel_id),
                components=self.cockpit_components(user_id, channel_id),
                ephemeral=False,
            )
        elif subcommand == "status":
            await self.respond_interaction(data, self.status_text(), ephemeral=True)
        elif subcommand == "inbox":
            await self.respond_interaction(data, self.inbox_text(), ephemeral=True)
        elif subcommand == "queue":
            if not self.can_run_admin_command(user_id, channel_id):
                await self.respond_interaction(data, "Inbox queue is restricted to admin users in admin rooms.", ephemeral=True)
                return
            await self.respond_interaction(data, self.in_queue_text(), ephemeral=True)
        elif subcommand == "facts":
            await self.respond_interaction(data, self.factsheet_text(channel_id), ephemeral=True)
        elif subcommand == "updatefacts":
            if not self.can_run_admin_command(user_id, channel_id):
                await self.respond_interaction(data, "Factsheet updates are restricted to admin users in admin rooms.", ephemeral=True)
                return
            await self.queue_factsheet_update(channel_id, user_id)
            await self.respond_interaction(data, "Room factsheet update queued.", ephemeral=True)
        elif subcommand == "tags":
            if not self.can_run_admin_command(user_id, channel_id):
                await self.respond_interaction(data, "Tags are restricted to admin users in admin rooms.", ephemeral=True)
                return
            await self.respond_interaction(data, self.tag_summary_text(), ephemeral=True)
        elif subcommand in {"followups", "conversations"}:
            if not self.can_run_admin_command(user_id, channel_id):
                await self.respond_interaction(data, "Open follow-ups are restricted to admin users in admin rooms.", ephemeral=True)
                return
            await self.respond_interaction(data, self.followups_text(), ephemeral=True)
        elif subcommand == "channels":
            lines = [f"- {cid}: #{self.channel_names.get(cid, cid)}" for cid in sorted(self.channel_ids())]
            await self.respond_interaction(data, "Configured COO channels:\n" + "\n".join(lines), ephemeral=True)
        elif subcommand == "pulse":
            if not self.can_run_admin_command(user_id, channel_id):
                await self.respond_interaction(data, "Pulse is restricted to admin users in admin rooms.", ephemeral=True)
                return
            await self.queue_manual_pulse(channel_id, user_id)
            await self.respond_interaction(data, "Manual COO pulse queued.", ephemeral=True)
        elif subcommand == "review":
            if not self.can_run_admin_command(user_id, channel_id):
                await self.respond_interaction(data, "Inbox review is restricted to admin users in admin rooms.", ephemeral=True)
                return
            count = await self.queue_reference_attention(channel_id, user_id, trigger="manual_review", respect_cooldown=False)
            await self.respond_interaction(
                data,
                f"Queued `{count}` pending inbox message(s) for COO attention." if count else "No pending inbox messages need attention.",
                ephemeral=True,
            )
        else:
            await self.respond_interaction(data, "Unknown COO subcommand.", ephemeral=True)

    async def handle_component(self, data: dict[str, Any], user_id: str, channel_id: str) -> None:
        custom_id = str((data.get("data") or {}).get("custom_id") or "")
        if not custom_id.startswith("coo:"):
            return
        action = custom_id.split(":", 1)[1]
        admin_actions = {"seed", "pulse", "interrupt", "compact", "clear", "close", "followups", "conversations", "queue", "review", "tags", "updatefacts"}
        if (action in admin_actions or action.startswith("tag:") or action.startswith("state:")) and not self.can_run_admin_command(user_id, channel_id):
            await self.respond_interaction(data, "Admin COO controls are restricted to admin users in admin rooms.", ephemeral=True)
            return
        if action == "refresh":
            await self.respond_interaction(
                data,
                self.cockpit_text(channel_id),
                embeds=self.cockpit_embeds(user_id, channel_id),
                components=self.cockpit_components(user_id, channel_id),
                response_type=INTERACTION_RESPONSE_UPDATE_MESSAGE,
            )
        elif action == "status":
            await self.respond_interaction(data, self.status_text(), ephemeral=True)
        elif action == "inbox":
            await self.respond_interaction(data, self.inbox_text(), ephemeral=True)
        elif action == "queue":
            await self.respond_interaction(data, self.in_queue_text(), ephemeral=True)
        elif action == "facts":
            await self.respond_interaction(data, self.factsheet_text(channel_id), ephemeral=True)
        elif action == "updatefacts":
            await self.queue_factsheet_update(channel_id, user_id)
            await self.respond_interaction(data, "Room factsheet update queued.", ephemeral=True)
        elif action == "tags":
            await self.respond_interaction(data, self.tag_summary_text(), ephemeral=True)
        elif action.startswith("state:"):
            await self.respond_interaction(data, self.state_filter_text(action.split(":", 1)[1]), ephemeral=True)
        elif action.startswith("tag:"):
            await self.respond_interaction(data, self.tag_filter_text(action.split(":", 1)[1]), ephemeral=True)
        elif action in {"followups", "conversations"}:
            await self.respond_interaction(data, self.followups_text(), ephemeral=True)
        elif action == "channels":
            lines = [f"- {cid}: #{self.channel_names.get(cid, cid)}" for cid in sorted(self.channel_ids())]
            await self.respond_interaction(data, "Configured COO channels:\n" + "\n".join(lines), ephemeral=True)
        elif action == "seed":
            await self.respond_modal(data)
        elif action == "pulse":
            await self.queue_manual_pulse(channel_id, user_id)
            await self.respond_interaction(data, "Manual COO pulse queued.", ephemeral=True)
        elif action == "review":
            count = await self.queue_reference_attention(channel_id, user_id, trigger="manual_button", respect_cooldown=False)
            await self.respond_interaction(
                data,
                f"Queued `{count}` pending inbox message(s) for COO attention." if count else "No pending inbox messages need attention.",
                ephemeral=True,
            )
        elif action == "interrupt":
            subprocess.run(["tmux", "send-keys", "-t", TMUX_TARGET, "Escape"], timeout=5, check=False)
            await self.respond_interaction(data, "Sent Esc to COO agent.", ephemeral=True)
        elif action == "compact":
            asyncio.create_task(self.send_to_control_text("/compact"))
            await self.respond_interaction(data, "Requested agent compact.", ephemeral=True)
        elif action == "clear":
            asyncio.create_task(self.send_to_control_text("/clear"))
            await self.respond_interaction(data, "Requested agent clear.", ephemeral=True)
        elif action == "close":
            closed = self.close_open_conversation(channel_id)
            await self.respond_interaction(
                data,
                "Closed the active COO conversation in this channel." if closed else "No active COO conversation was open in this channel.",
                ephemeral=True,
            )
        else:
            await self.respond_interaction(data, "Unknown COO cockpit action.", ephemeral=True)

    async def handle_modal_submit(self, data: dict[str, Any], user_id: str, channel_id: str) -> None:
        custom_id = str((data.get("data") or {}).get("custom_id") or "")
        if custom_id != "coo:seed_modal":
            return
        if not self.can_run_admin_command(user_id, channel_id):
            await self.respond_interaction(data, "Admin COO controls are restricted to admin users in admin rooms.", ephemeral=True)
            return
        prompt = self.extract_modal_value(data, "prompt").strip()
        if not prompt:
            await self.respond_interaction(data, "Seed prompt was empty.", ephemeral=True)
            return
        await self.queue.put(AgentPrompt(
            text="[admin cockpit seed]\n" + prompt,
            channel_id=channel_id,
            author_id=user_id,
            kind="admin_cockpit_seed",
            queued_at=time.time(),
        ))
        await self.respond_interaction(data, "COO seed queued.", ephemeral=True)

    async def queue_manual_pulse(self, channel_id: str, user_id: str) -> None:
        if await self.queue_reference_attention(channel_id, user_id, trigger="manual_pulse", respect_cooldown=False):
            return
        await self.queue.put(AgentPrompt(
            text="[manual COO cockpit pulse]\nReview current work state. If no public action is useful, reply exactly NOOP.",
            channel_id=channel_id,
            author_id=user_id,
            kind="manual_cockpit_pulse",
            queued_at=time.time(),
        ))

    def cockpit_text(self, channel_id: str) -> str:
        return f"COO Cockpit for `#{self.channel_names.get(channel_id, channel_id)}`"

    def cockpit_embeds(self, user_id: str, channel_id: str) -> list[dict[str, Any]]:
        counts = self.reference_attention_counts()
        pane = self.pane_state()
        is_admin_room = self.can_run_admin_command(user_id, channel_id)
        pending = counts.get("pending", 0)
        queued = counts.get("queued", 0)
        held = counts.get("held", 0)
        no_action = counts.get("no-action", 0)
        initiated = counts.get("initiated", 0)
        failed = counts.get("failed", 0)
        color = 0x2ECC71
        if failed:
            color = 0xE74C3C
        elif pending or queued or held or pane not in {"input_ready", "starting"}:
            color = 0xF1C40F
        fields = [
            {
                "name": "Runtime",
                "value": (
                    f"Pane: `{pane}`\n"
                    f"Agent queue: `{self.queue.qsize()}`\n"
                    f"Uptime: `{self.format_seconds(int(time.time() - self.started_at))}`"
                ),
                "inline": True,
            },
            {
                "name": "Inbox Attention",
                "value": (
                    f"Pending: `{pending}`\n"
                    f"Queued: `{queued}`\n"
                    f"Held: `{held}`\n"
                    f"No action: `{no_action}`\n"
                    f"Initiated: `{initiated}`\n"
                    f"Failed: `{failed}`"
                ),
                "inline": True,
            },
            {
                "name": "Follow-ups",
                "value": (
                    f"Open: `{len(self.state.get('open_conversations') or {})}`\n"
                    f"Mode: `{CONVERSATION_MODE}`\n"
                    f"Access: `{'admin' if is_admin_room else 'standard'}`"
                ),
                "inline": True,
            },
            {
                "name": "Claude Code Automation",
                "value": self.claude_automation_summary(compact=True),
                "inline": False,
            },
            {
                "name": "Reference Tags",
                "value": self.tag_summary_line(),
                "inline": False,
            },
        ]
        return [{
            "title": "Claudex COO Cockpit",
            "description": "Native Discord control panel. Use In Queue for work waiting on the COO agent and Claude Code automation lanes.",
            "color": color,
            "fields": fields,
            "footer": {"text": "Lower rooms are read-only controls. Admin actions require coo-admin, coo-config, or coo-audit."},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }]

    def cockpit_components(self, user_id: str, channel_id: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = [
            {"type": 1, "components": [
                self.button("Refresh", "coo:refresh", 2),
                self.button("Status", "coo:status", 2),
                self.button("Inbox", "coo:inbox", 2),
                self.button("Channels", "coo:channels", 2),
                self.button("Factsheet", "coo:facts", 2),
            ]},
        ]
        if self.can_run_admin_command(user_id, channel_id):
            rows.append({"type": 1, "components": [
                self.button("Seed", "coo:seed", 1),
                self.button("Pulse", "coo:pulse", 1),
                self.button("In Queue", "coo:queue", 2),
                self.button("Review Inbox", "coo:review", 1),
                self.button("Update Facts", "coo:updatefacts", 1),
            ]})
            rows.append({"type": 1, "components": [
                self.button("Open Follow-ups", "coo:followups", 2),
                self.button("Close Follow-up", "coo:close", 2),
                self.button("Interrupt", "coo:interrupt", 4),
                self.button("Compact", "coo:compact", 2),
                self.button("Clear", "coo:clear", 4),
            ]})
            rows.append({"type": 1, "components": [
                self.button("Pending", "coo:state:pending", 2),
                self.button("Queued", "coo:state:queued", 2),
                self.button("Held", "coo:state:held", 2),
                self.button("No Action", "coo:state:no-action", 2),
                self.button("Initiated", "coo:state:initiated", 2),
            ]})
            rows.append({"type": 1, "components": [
                self.button("Failed", "coo:state:failed", 4),
                self.button("Tags", "coo:tags", 2),
            ]})
        return rows

    @staticmethod
    def button(label: str, custom_id: str, style: int) -> dict[str, Any]:
        return {"type": 2, "label": label, "style": style, "custom_id": custom_id}

    async def respond_modal(self, data: dict[str, Any]) -> None:
        body = {
            "type": INTERACTION_RESPONSE_MODAL,
            "data": {
                "custom_id": "coo:seed_modal",
                "title": "Seed COO Work",
                "components": [{
                    "type": 1,
                    "components": [{
                        "type": 4,
                        "custom_id": "prompt",
                        "label": "Prompt for the COO agent",
                        "style": 2,
                        "min_length": 1,
                        "max_length": 1900,
                        "required": True,
                    }],
                }],
            },
        }
        await self.interaction_callback(data, body)

    async def respond_interaction(
        self,
        data: dict[str, Any],
        content: str,
        *,
        ephemeral: bool = False,
        embeds: list[dict[str, Any]] | None = None,
        components: list[dict[str, Any]] | None = None,
        response_type: int = INTERACTION_RESPONSE_CHANNEL_MESSAGE,
    ) -> None:
        payload: dict[str, Any] = {"content": content[:1900], "allowed_mentions": {"parse": []}}
        if ephemeral:
            payload["flags"] = EPHEMERAL_FLAG
        if embeds:
            payload["embeds"] = embeds
        if components:
            payload["components"] = components
        await self.interaction_callback(data, {"type": response_type, "data": payload})

    async def interaction_callback(self, data: dict[str, Any], body: dict[str, Any]) -> None:
        assert self.http is not None
        interaction_id = str(data.get("id") or "")
        token = str(data.get("token") or "")
        async with self.http.post(
            f"{DISCORD_API}/interactions/{interaction_id}/{token}/callback",
            headers={"Content-Type": "application/json", "User-Agent": "ClaudexDiscordCOO/0.1"},
            json=body,
        ) as resp:
            text = await resp.text()
            if resp.status >= 300:
                raise RuntimeError(f"Discord interaction callback failed {resp.status}: {text[:500]}")

    @staticmethod
    def interaction_user_id(data: dict[str, Any]) -> str:
        member = data.get("member") or {}
        user = member.get("user") or data.get("user") or {}
        return str(user.get("id") or "")

    @staticmethod
    def extract_modal_value(data: dict[str, Any], custom_id: str) -> str:
        for row in (data.get("data") or {}).get("components") or []:
            for component in row.get("components") or []:
                if component.get("custom_id") == custom_id:
                    return str(component.get("value") or "")
        return ""

    def build_agent_prompt(self, data: dict[str, Any], conversation_kind: str) -> str:
        channel_id = str(data.get("channel_id") or "")
        author = data.get("author") or {}
        name = author.get("global_name") or author.get("username") or author.get("id")
        attachments = data.get("attachments") or []
        attachment_lines = [
            f"- {a.get('filename') or 'attachment'}: {a.get('url')}"
            for a in attachments
            if a.get("url")
        ]
        channel_name = self.channel_names.get(channel_id, channel_id)
        body = str(data.get("content") or "").strip()
        prompt = (
            f"[discord message guild_id={data.get('guild_id')} channel_id={channel_id} "
            f"channel_name=#{channel_name} author={name} author_id={author.get('id')} "
            f"message_id={data.get('id')} conversation_kind={conversation_kind}]\n\n"
            f"{body}"
        )
        ref_id = str((data.get("message_reference") or {}).get("message_id") or "")
        if ref_id:
            prompt += f"\n\nDiscord reply_to_message_id: {ref_id}"
        if attachment_lines:
            prompt += "\n\nAttachments:\n" + "\n".join(attachment_lines)
        prompt += "\n\nRespond as the persistent COO agent for this workplace Discord."
        return prompt

    async def handle_reference_only_message(self, data: dict[str, Any], reason: str) -> None:
        channel_id = str(data.get("channel_id") or "")
        message_id = str(data.get("id") or "")
        author_id = str((data.get("author") or {}).get("id") or "")
        saved_path = self.save_reference_message(data, reason=reason)
        self._event(
            "employee_message_saved_reference",
            channel_id=channel_id,
            message_id=message_id,
            author_id=author_id,
            path=str(saved_path),
        )
        await self.add_reaction(channel_id, message_id, "🗂️")
        notices = self.state.setdefault("unsolicited_notice_at", {})
        key = f"{channel_id}:{author_id}"
        now = time.time()
        if now - float(notices.get(key, 0)) < UNSOLICITED_ACK_COOLDOWN_SECONDS:
            return
        notices[key] = now
        self._save_state()
        await self.send_discord(
            channel_id,
            self.reference_notice_text(reason),
            reference_message_id=message_id,
            opens_conversation=False,
        )

    @staticmethod
    def reference_notice_text(reason: str) -> str:
        if reason == "admin_room_requires_reply_and_mention":
            return "Saved for COO reference. Admin-room live intake requires replying to a COO message and mentioning the bot."
        if reason == "requires_reply_to_coo_message":
            return "Saved for COO reference. Live COO intake requires replying to a COO message."
        return "Saved for COO reference. The COO will read saved notes and initiate a loop when it decides to act."

    def save_reference_message(self, data: dict[str, Any], reason: str) -> Path:
        channel_id = str(data.get("channel_id") or "unknown-channel")
        channel_name = self.channel_names.get(channel_id, channel_id)
        author = data.get("author") or {}
        author_id = str(author.get("id") or "unknown-user")
        author_name = str(author.get("global_name") or author.get("username") or author_id)
        message_id = str(data.get("id") or f"{int(time.time())}")
        timestamp = str(data.get("timestamp") or datetime.now(timezone.utc).isoformat())
        day = timestamp[:10] if len(timestamp) >= 10 else datetime.now(timezone.utc).strftime("%Y-%m-%d")
        content = str(data.get("content") or "").strip()
        matter_slug = self.slugify(content.splitlines()[0] if content else "attachments")
        tags = self.reference_tags(data, reason, "pending")

        department_dir = REFERENCE_DIR / self.slugify(f"{channel_name}-{channel_id}")
        person_dir = department_dir / "people" / self.slugify(f"{author_name}-{author_id}") / day
        matter_dir = department_dir / "matters" / "uncategorized" / day / f"{matter_slug}-{message_id}"
        person_dir.mkdir(parents=True, exist_ok=True)
        matter_dir.mkdir(parents=True, exist_ok=True)

        record = {
            "reason": reason,
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "guild_id": str(data.get("guild_id") or ""),
            "channel_id": channel_id,
            "channel_name": channel_name,
            "author_id": author_id,
            "author_name": author_name,
            "message_id": message_id,
            "timestamp": timestamp,
            "content": content,
            "attachments": data.get("attachments") or [],
            "message_reference": data.get("message_reference") or {},
            "tags": tags,
        }

        json_path = person_dir / f"{message_id}.json"
        json_path.write_text(json.dumps(record, indent=2, ensure_ascii=False))
        md_path = matter_dir / "message.md"
        attachment_lines = [
            f"- {a.get('filename') or 'attachment'}: {a.get('url')}"
            for a in record["attachments"]
            if a.get("url")
        ]
        md = [
            f"# Discord Reference Message {message_id}",
            "",
            f"- Reason: {reason}",
            "- Workflow state: pending",
            f"- Department/channel: #{channel_name} ({channel_id})",
            f"- Author: {author_name} ({author_id})",
            f"- Timestamp: {timestamp}",
            f"- Tags: {', '.join(tags)}",
            f"- Person JSON: {json_path}",
            "",
            "## Message",
            "",
            content or "(no text)",
        ]
        if attachment_lines:
            md.extend(["", "## Attachments", "", *attachment_lines])
        md_path.write_text("\n".join(md) + "\n")

        index_path = REFERENCE_DIR / "_index.jsonl"
        with index_path.open("a") as f:
            f.write(json.dumps({**record, "person_path": str(json_path), "matter_path": str(md_path)}, ensure_ascii=False) + "\n")
        self.state["last_reference_message_path"] = str(md_path)
        self.mark_reference_messages([message_id], "pending", save=False, saved_at=record["saved_at"], reason=reason, tags=tags)
        self._save_state()
        return md_path

    def reference_statuses(self) -> dict[str, Any]:
        return self.state.setdefault("reference_status", {})

    def reference_tags(self, data: dict[str, Any], reason: str, status: str) -> list[str]:
        channel_id = str(data.get("channel_id") or "unknown-channel")
        channel_name = self.channel_names.get(channel_id, channel_id)
        attachments = data.get("attachments") or []
        tags = {
            "source-inbox",
            f"reason-{self.slugify(reason)}",
            f"channel-{self.slugify(channel_name)}",
            "matter-uncategorized",
        }
        if attachments:
            tags.add("has-attachments")
        if reason == "requires_reply_to_coo_message":
            tags.add("needs-coo-reply")
        if reason == "admin_room_requires_reply_and_mention":
            tags.add("needs-admin-mention")
        return sorted(tags)

    def mark_reference_messages(self, message_ids: list[str], status: str, save: bool = True, **extra: Any) -> None:
        statuses = self.reference_statuses()
        now = datetime.now(timezone.utc).isoformat()
        provided_tags = {
            str(tag)
            for tag in extra.pop("tags", [])
            if not str(tag).startswith(("status-", "state-"))
        }
        timestamp_key = f"{self.slugify(status).replace('-', '_')}_at"
        for message_id in message_ids:
            row = statuses.setdefault(str(message_id), {})
            previous_tags = {str(tag) for tag in row.get("tags") or []}
            previous_tags = {tag for tag in previous_tags if not tag.startswith(("status-", "state-"))}
            row["status"] = status
            row[timestamp_key] = now
            row["tags"] = sorted(previous_tags | provided_tags)
            row.update(extra)
        if save:
            self._save_state()

    def mark_active_reference_messages(self, status: str, **extra: Any) -> None:
        message_ids = [str(m) for m in self.state.get("active_reference_message_ids") or []]
        if not message_ids:
            return
        self.mark_reference_messages(message_ids, status, save=False, **extra)
        self.state["active_reference_message_ids"] = []
        self._save_state()

    def reference_entries(self) -> list[dict[str, Any]]:
        index_path = REFERENCE_DIR / "_index.jsonl"
        if not index_path.exists():
            return []
        statuses = self.reference_statuses()
        entries: list[dict[str, Any]] = []
        seen: set[str] = set()
        for line in index_path.read_text().splitlines():
            try:
                row = json.loads(line)
            except Exception:
                continue
            message_id = str(row.get("message_id") or "")
            if not message_id or message_id in seen:
                continue
            seen.add(message_id)
            status = dict(statuses.get(message_id) or {})
            row["attention_status"] = status.get("status") or "pending"
            row["attention"] = status
            tags = {str(tag) for tag in row.get("tags") or []}
            tags.update(str(tag) for tag in status.get("tags") or [])
            tags = {tag for tag in tags if not tag.startswith(("status-", "state-"))}
            tags.add("source-inbox")
            row["tags"] = sorted(tags)
            row["state_tag"] = f"state-{self.slugify(str(row['attention_status']))}"
            entries.append(row)
        return entries

    def reference_entries_by_status(self, *statuses: str) -> list[dict[str, Any]]:
        wanted = set(statuses)
        return [row for row in self.reference_entries() if row.get("attention_status") in wanted]

    def reference_attention_counts(self) -> dict[str, int]:
        counts = {state: 0 for state in REFERENCE_WORKFLOW_STATES}
        for row in self.reference_entries():
            status = str(row.get("attention_status") or "pending")
            counts[status] = counts.get(status, 0) + 1
        return counts

    def tag_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for row in self.reference_entries():
            for tag in row.get("tags") or []:
                counts[str(tag)] = counts.get(str(tag), 0) + 1
        for channel_id, conv in (self.state.get("open_conversations") or {}).items():
            channel_name = self.channel_names.get(str(channel_id), str(channel_id))
            for tag in {
                "followup-open",
                f"channel-{self.slugify(channel_name)}",
                "targeted" if conv.get("targeted") else "channel-wide",
            }:
                counts[tag] = counts.get(tag, 0) + 1
        return dict(sorted(counts.items()))

    def tag_summary_line(self) -> str:
        counts = self.tag_counts()
        priority = [
            "source-inbox",
            "needs-coo-reply",
            "needs-admin-mention",
            "has-attachments",
            "matter-uncategorized",
            "followup-open",
        ]
        parts = [f"`{tag}` {counts[tag]}" for tag in priority if counts.get(tag)]
        if not parts:
            parts = [f"`{tag}` {count}" for tag, count in list(counts.items())[:8]]
        return " ".join(parts) or "`none`"

    def tag_summary_text(self) -> str:
        counts = self.tag_counts()
        if not counts:
            return "No reference/conversation tags yet."
        lines = ["Reference/conversation tags"]
        for tag, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:40]:
            lines.append(f"- `{tag}`: `{count}`")
        return "\n".join(lines)

    def tag_filter_text(self, tag: str) -> str:
        wanted = self.slugify(tag)
        lines = [f"Tag `{wanted}`"]
        matched = [row for row in self.reference_entries() if wanted in {str(t) for t in row.get("tags") or []}]
        followups = []
        if wanted == "followup-open" or wanted.startswith("channel-"):
            for channel_id, conv in sorted((self.state.get("open_conversations") or {}).items()):
                channel_name = self.channel_names.get(str(channel_id), str(channel_id))
                followup_tags = {"followup-open", f"channel-{self.slugify(channel_name)}"}
                if wanted in followup_tags:
                    followups.append((channel_id, conv))
        if not matched and not followups:
            return f"No cockpit items currently tagged `{wanted}`."
        if matched:
            lines.extend(["", "Inbox/reference:"])
            lines.extend(self.format_reference_entry(row) for row in matched[:10])
        if followups:
            lines.extend(["", "Open follow-ups:"])
            now = time.time()
            for channel_id, conv in followups[:10]:
                remaining = max(0, int(float(conv.get("expires_at", 0)) - now))
                lines.append(f"- `followup-open` #{self.channel_names.get(str(channel_id), str(channel_id))}: expires in {remaining}s")
        return "\n".join(lines)

    def state_filter_text(self, status: str) -> str:
        wanted = self.slugify(status)
        entries = self.reference_entries_by_status(wanted)
        label = wanted.replace("-", " ").title()
        if not entries:
            return f"No inbox/reference messages currently in `{wanted}`."
        lines = [f"{label} inbox/reference messages"]
        lines.extend(self.format_reference_entry(row) for row in entries[:15])
        if len(entries) > 15:
            lines.append(f"...and `{len(entries) - 15}` more.")
        return "\n".join(lines)

    def format_reference_entry(self, row: dict[str, Any]) -> str:
        content = " ".join(str(row.get("content") or "").split())
        if len(content) > 120:
            content = content[:120] + "..."
        status = row.get("attention_status") or "pending"
        tags = " ".join(f"`{tag}`" for tag in (row.get("tags") or [])[:5])
        return (
            f"- `{status}` {tags} #{row.get('channel_name')} / {row.get('author_name')}: "
            f"`{row.get('message_id')}` {content or '(no text)'}"
        )

    def inbox_queue_text(self) -> str:
        counts = self.reference_attention_counts()
        lines = [
            "Inbox workflow states",
            f"- pending: `{counts.get('pending', 0)}`",
            f"- queued: `{counts.get('queued', 0)}`",
            f"- held: `{counts.get('held', 0)}`",
            f"- no-action: `{counts.get('no-action', 0)}`",
            f"- initiated: `{counts.get('initiated', 0)}`",
            f"- failed: `{counts.get('failed', 0)}`",
            f"- inbox: `{REFERENCE_DIR}`",
        ]
        recent: list[dict[str, Any]] = []
        for status in ("pending", "queued", "held", "no-action", "initiated", "failed"):
            recent.extend(self.reference_entries_by_status(status)[:3])
        if recent:
            lines.extend(["", "Recent by workflow state:"])
            lines.extend(self.format_reference_entry(row) for row in recent[:12])
        return "\n".join(lines)

    def in_queue_text(self) -> str:
        lines = [
            "In Queue",
            "",
            "COO Agent",
            f"- live prompt queue: `{self.queue.qsize()}`",
            f"- active kind: `{self.state.get('last_prompt_kind') or 'none'}`",
            f"- active channel: `#{self.channel_names.get(str(self.state.get('active_channel_id') or ''), self.state.get('active_channel_id') or 'none')}`",
            f"- tags: {self.tag_summary_line()}",
            "",
            self.inbox_queue_text(),
            "",
            self.followups_text(),
            "",
            "Claude Code automation equivalents",
            self.claude_automation_summary(compact=False),
        ]
        return "\n".join(lines)

    def claude_automation_summary(self, *, compact: bool) -> str:
        rows = self.claude_automation_rows()
        if compact:
            return "\n".join(f"- {row['name']}: `{row['status']}`" for row in rows)
        return "\n".join(
            f"- {row['name']}: `{row['status']}` via {row['equivalent']} ({row['scope']})"
            for row in rows
        )

    def claude_automation_rows(self) -> list[dict[str, str]]:
        tracked = self.state.get("claude_code_automation") or {}
        bridge_agent = "attached" if AGENT_KIND == "claude" else "not attached: Discord COO bridge currently uses Codex"
        return [
            {
                "name": "Routines",
                "equivalent": "Claude Code cloud Routines, created from web or `/schedule`",
                "scope": "cloud, durable across this VPS",
                "status": str(tracked.get("routines") or "not locally enumerable"),
            },
            {
                "name": "Schedules",
                "equivalent": "CronCreate/CronList scheduled tasks inside a Claude Code session",
                "scope": "session-scoped unless using cloud/desktop scheduled tasks",
                "status": str(tracked.get("schedules") or bridge_agent),
            },
            {
                "name": "Loops",
                "equivalent": "`/loop` bundled skill backed by scheduled tasks",
                "scope": "Claude Code session, restored on resume if unexpired",
                "status": str(tracked.get("loops") or bridge_agent),
            },
            {
                "name": "Monitors",
                "equivalent": "Monitor tool used by dynamic `/loop` to stream background script output",
                "scope": "Claude Code session/background task",
                "status": str(tracked.get("monitors") or bridge_agent),
            },
        ]

    def build_reference_attention_prompt(self, entries: list[dict[str, Any]], trigger: str) -> str:
        lines = [
            f"[{trigger} inbox attention]",
            "Review these saved employee/reference messages and make a workflow decision for each.",
            "If you initiate a Discord follow-up, write the concise message that should appear in Discord.",
            f"If you read them but intentionally hold/defer with no outward action, include {CONTROL_HOLD}.",
            f"If you decide no action is needed, include {CONTROL_NO_ACTION}. If literally no public text is useful, reply exactly NOOP.",
            f"If your response closes an existing loop, include {CONTROL_CLOSE}.",
            f"Reference inbox root: {REFERENCE_DIR}",
            "",
            "Messages:",
        ]
        for row in entries:
            content = str(row.get("content") or "").strip()
            if len(content) > 700:
                content = content[:700] + "... [truncated]"
            lines.extend([
                f"- message_id: {row.get('message_id')}",
                f"  channel: #{row.get('channel_name')} ({row.get('channel_id')})",
                f"  author: {row.get('author_name')} ({row.get('author_id')})",
                f"  timestamp: {row.get('timestamp')}",
                f"  workflow_state: {row.get('attention_status')}",
                f"  tags: {', '.join(row.get('tags') or [])}",
                f"  matter_path: {row.get('matter_path')}",
                f"  person_path: {row.get('person_path')}",
                f"  content: {content or '(no text)'}",
            ])
        lines.append("")
        lines.append("The bridge will mark normal public output as initiated, HOLD as held, NO_ACTION/NOOP as no-action, and errors as failed.")
        return "\n".join(lines)

    async def queue_reference_attention(
        self,
        channel_id: str,
        user_id: str | None,
        *,
        trigger: str,
        respect_cooldown: bool,
    ) -> int:
        pending = self.reference_entries_by_status(*REFERENCE_QUEUEABLE_STATES)
        if not pending:
            return 0
        now = time.time()
        if respect_cooldown and now - float(self.state.get("last_reference_attention_queued_at", 0)) < INBOX_ATTENTION_COOLDOWN_SECONDS:
            return 0
        selected = pending[:INBOX_ATTENTION_BATCH_SIZE]
        message_ids = [str(row["message_id"]) for row in selected]
        batch_id = f"{trigger}-{int(now)}"
        self.mark_reference_messages(
            message_ids,
            "queued",
            save=False,
            batch_id=batch_id,
            queued_trigger=trigger,
        )
        self.state["last_reference_attention_queued_at"] = now
        self._save_state()
        await self.queue.put(AgentPrompt(
            text=self.build_reference_attention_prompt(selected, trigger),
            channel_id=channel_id,
            author_id=user_id,
            kind=f"inbox_attention_{trigger}",
            queued_at=now,
            reference_message_ids=message_ids,
        ))
        self._event("reference_attention_queued", trigger=trigger, count=len(message_ids), batch_id=batch_id)
        return len(message_ids)

    def save_daily_transcript(self, data: dict[str, Any], direction: str) -> Path:
        channel_id = str(data.get("channel_id") or "unknown-channel")
        channel_name = self.channel_names.get(channel_id, channel_id)
        author = data.get("author") or {}
        author_id = str(author.get("id") or "unknown-user")
        author_name = str(author.get("global_name") or author.get("username") or author_id)
        message_id = str(data.get("id") or f"{int(time.time())}")
        timestamp = str(data.get("timestamp") or datetime.now(timezone.utc).isoformat())
        day = timestamp[:10] if len(timestamp) >= 10 else datetime.now(timezone.utc).strftime("%Y-%m-%d")
        content = str(data.get("content") or "").strip()
        attachments = data.get("attachments") or []
        ref_id = str((data.get("message_reference") or {}).get("message_id") or "")

        channel_dir = TRANSCRIPT_DIR / self.slugify(f"{channel_name}-{channel_id}")
        channel_dir.mkdir(parents=True, exist_ok=True)
        md_path = channel_dir / f"{day}.md"
        jsonl_path = channel_dir / f"{day}.jsonl"

        if not md_path.exists():
            md_path.write_text(f"# Discord Transcript: #{channel_name} ({day})\n\n")

        clean_content = " ".join(content.split())
        if len(clean_content) > 1200:
            clean_content = clean_content[:1200] + "... [truncated]"
        attachment_text = ""
        if attachments:
            attachment_text = " attachments=" + ", ".join(
                f"{a.get('filename') or 'attachment'}:{a.get('url')}"
                for a in attachments[:5]
                if a.get("url")
            )
        reply_text = f" reply_to={ref_id}" if ref_id else ""
        line = (
            f"- {timestamp} | {direction} | {author_name} ({author_id})"
            f" | msg={message_id}{reply_text}{attachment_text}\n"
            f"  {clean_content or '(no text)'}\n"
        )
        with md_path.open("a") as f:
            f.write(line)
        with jsonl_path.open("a") as f:
            f.write(json.dumps({
                "direction": direction,
                "guild_id": str(data.get("guild_id") or GUILD_ID),
                "channel_id": channel_id,
                "channel_name": channel_name,
                "author_id": author_id,
                "author_name": author_name,
                "message_id": message_id,
                "reply_to_message_id": ref_id,
                "timestamp": timestamp,
            "content": content,
            "attachments": attachments,
            "tags": [f"transcript-{self.slugify(direction)}", f"channel-{self.slugify(channel_name)}"],
        }, ensure_ascii=False) + "\n")
        self.state["last_transcript_path"] = str(md_path)
        self._save_state()
        return md_path

    def room_factsheet_paths(self, channel_id: str) -> dict[str, Path]:
        now = datetime.now(timezone.utc)
        channel_name = self.channel_names.get(channel_id, channel_id)
        room_dir = FACTSHEET_DIR / self.slugify(f"{channel_name}-{channel_id}")
        iso_year, iso_week, _ = now.isocalendar()
        return {
            "weekly": room_dir / f"{iso_year}-W{iso_week:02d}.md",
            "monthly": room_dir / f"{now.year}-{now.month:02d}.md",
        }

    def ensure_room_factsheet(self, channel_id: str, period: str) -> Path:
        paths = self.room_factsheet_paths(channel_id)
        path = paths[period]
        channel_name = self.channel_names.get(channel_id, channel_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text(
                f"# Room Factsheet: #{channel_name} ({period})\n\n"
                f"- Channel: #{channel_name} ({channel_id})\n"
                f"- Period: {path.stem}\n"
                f"- Updated: never\n"
                f"- Tags: factsheet, period-{period}, channel-{self.slugify(channel_name)}\n\n"
                "## Current Facts\n\n"
                "- No facts recorded yet.\n\n"
                "## Decisions\n\n"
                "- No decisions recorded yet.\n\n"
                "## Open Questions\n\n"
                "- No open questions recorded yet.\n\n"
                "## Source Pointers\n\n"
                f"- Transcript folder: {TRANSCRIPT_DIR / self.slugify(f'{channel_name}-{channel_id}')}\n"
                f"- Reference inbox: {REFERENCE_DIR}\n"
            )
        return path

    def factsheet_text(self, channel_id: str) -> str:
        weekly = self.ensure_room_factsheet(channel_id, "weekly")
        monthly = self.ensure_room_factsheet(channel_id, "monthly")

        def snippet(path: Path) -> str:
            text = path.read_text()
            lines = [line for line in text.splitlines() if line.strip()]
            body = "\n".join(lines[:18])
            if len(body) > 650:
                body = body[:650] + "..."
            return body or "(empty)"

        channel_name = self.channel_names.get(channel_id, channel_id)
        return (
            f"Room factsheets for `#{channel_name}`\n"
            f"- weekly: `{weekly}`\n"
            f"- monthly: `{monthly}`\n\n"
            "Weekly snapshot:\n"
            f"{snippet(weekly)}\n\n"
            "Monthly snapshot:\n"
            f"{snippet(monthly)}"
        )

    async def queue_factsheet_update(self, channel_id: str, user_id: str | None) -> None:
        weekly = self.ensure_room_factsheet(channel_id, "weekly")
        monthly = self.ensure_room_factsheet(channel_id, "monthly")
        channel_name = self.channel_names.get(channel_id, channel_id)
        transcript_dir = TRANSCRIPT_DIR / self.slugify(f"{channel_name}-{channel_id}")
        prompt = (
            "[room factsheet update]\n"
            f"Update the weekly and monthly factsheets for Discord room #{channel_name} ({channel_id}).\n"
            f"Weekly factsheet: {weekly}\n"
            f"Monthly factsheet: {monthly}\n"
            f"Room transcript folder: {transcript_dir}\n"
            f"Reference inbox root: {REFERENCE_DIR}\n\n"
            "Use recorded transcripts, saved reference messages, and existing factsheet content only. "
            "Keep facts concise, separate decisions from open questions, and do not invent missing details. "
            "After updating files, send a short Discord summary; if nothing changed, reply exactly NOOP."
        )
        await self.queue.put(AgentPrompt(
            text=prompt,
            channel_id=channel_id,
            author_id=user_id,
            kind="factsheet_update",
            queued_at=time.time(),
        ))
        self._event("factsheet_update_queued", channel_id=channel_id, weekly=str(weekly), monthly=str(monthly))

    @staticmethod
    def slugify(value: str) -> str:
        cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip())[:80].strip("-._")
        return cleaned or "item"

    async def handle_command(self, data: dict[str, Any], command: str) -> None:
        channel_id = str(data.get("channel_id") or "")
        message_id = str(data.get("id") or "")
        author_id = str((data.get("author") or {}).get("id") or "")
        parts = command.split()
        name = (parts[0].lower() if parts else "help")
        rest = command[len(parts[0]):].strip() if parts else ""
        admin_only = {"interrupt", "compact", "clear", "enter", "pulse", "boot", "send", "watch", "unwatch", "home", "close", "followups", "conversations", "inbox", "queue", "review", "tags", "updatefacts"}
        if name in admin_only and not self.can_run_admin_command(author_id, channel_id):
            await self.send_discord(
                channel_id,
                "Admin COO controls are restricted to configured admin users and admin rooms.",
                reference_message_id=message_id,
            )
            return
        if name in {"help", ""}:
            await self.send_discord(channel_id, self.help_text(), reference_message_id=message_id)
        elif name == "status":
            await self.send_discord(channel_id, self.status_text(), reference_message_id=message_id)
        elif name == "interrupt":
            subprocess.run(["tmux", "send-keys", "-t", TMUX_TARGET, "Escape"], timeout=5, check=False)
            await self.send_discord(channel_id, "Sent Esc to COO agent.", reference_message_id=message_id)
        elif name == "enter":
            subprocess.run(["tmux", "send-keys", "-t", TMUX_TARGET, "Enter"], timeout=5, check=False)
            await self.send_discord(channel_id, "Sent Enter to COO agent.", reference_message_id=message_id)
        elif name == "compact":
            await self.send_to_control_text("/compact")
            await self.send_discord(channel_id, "Requested agent compact.", reference_message_id=message_id)
        elif name == "clear":
            await self.send_to_control_text("/clear")
            await self.send_discord(channel_id, "Requested agent clear.", reference_message_id=message_id)
        elif name == "pulse":
            await self.queue.put(AgentPrompt(
                text="[manual COO pulse]\nReview current work state. If no public action is useful, reply exactly NOOP.",
                channel_id=channel_id,
                message_id=message_id,
                author_id=author_id,
                kind="manual_pulse",
                queued_at=time.time(),
            ))
            await self.send_discord(channel_id, "Manual COO pulse queued.", reference_message_id=message_id)
        elif name == "boot":
            await self.queue.put(AgentPrompt(text=COO_MISSION, channel_id=channel_id, message_id=message_id, kind="mission"))
            await self.send_discord(channel_id, "COO mission prompt queued.", reference_message_id=message_id)
        elif name == "send":
            if not rest:
                await self.send_discord(channel_id, f"Usage: `{PREFIX} send <prompt>`", reference_message_id=message_id)
                return
            await self.queue.put(AgentPrompt(text=rest, channel_id=channel_id, message_id=message_id, author_id=author_id, kind="admin_send"))
            await self.send_discord(channel_id, "Prompt queued.", reference_message_id=message_id)
        elif name == "watch":
            watched = set(self.state.get("watched_channel_ids") or [])
            watched.add(channel_id)
            self.state["watched_channel_ids"] = sorted(watched)
            self._save_state()
            await self.send_discord(channel_id, f"Watching this channel: `#{self.channel_names.get(channel_id, channel_id)}`.", reference_message_id=message_id)
        elif name == "unwatch":
            watched = set(self.state.get("watched_channel_ids") or [])
            watched.discard(channel_id)
            self.state["watched_channel_ids"] = sorted(watched)
            self._save_state()
            await self.send_discord(channel_id, f"Removed this channel from dynamic watch: `#{self.channel_names.get(channel_id, channel_id)}`.", reference_message_id=message_id)
        elif name == "home":
            self.state["home_channel_id"] = channel_id
            self._save_state()
            await self.send_discord(channel_id, f"Home channel set to `#{self.channel_names.get(channel_id, channel_id)}`.", reference_message_id=message_id)
        elif name == "close":
            closed = self.close_open_conversation(channel_id)
            await self.send_discord(
                channel_id,
                "Closed the active COO conversation in this channel." if closed else "No active COO conversation was open in this channel.",
                reference_message_id=message_id,
            )
        elif name in {"followups", "conversations"}:
            await self.send_discord(channel_id, self.followups_text(), reference_message_id=message_id)
        elif name == "inbox":
            await self.send_discord(channel_id, self.inbox_text(), reference_message_id=message_id)
        elif name == "queue":
            await self.send_discord(channel_id, self.inbox_queue_text(), reference_message_id=message_id)
        elif name == "facts":
            await self.send_discord(channel_id, self.factsheet_text(channel_id), reference_message_id=message_id)
        elif name == "updatefacts":
            await self.queue_factsheet_update(channel_id, author_id)
            await self.send_discord(channel_id, "Room factsheet update queued.", reference_message_id=message_id)
        elif name == "tags":
            await self.send_discord(channel_id, self.tag_summary_text(), reference_message_id=message_id)
        elif name == "review":
            count = await self.queue_reference_attention(channel_id, author_id, trigger="manual_command", respect_cooldown=False)
            await self.send_discord(
                channel_id,
                f"Queued `{count}` pending inbox message(s) for COO attention." if count else "No pending inbox messages need attention.",
                reference_message_id=message_id,
            )
        elif name == "channels":
            lines = [f"- {cid}: #{self.channel_names.get(cid, cid)}" for cid in sorted(self.channel_ids())]
            await self.send_discord(channel_id, "Configured COO channels:\n" + "\n".join(lines), reference_message_id=message_id)
        else:
            await self.send_discord(channel_id, f"Unknown command. Try `{PREFIX} help`.", reference_message_id=message_id)

    async def send_to_control_text(self, text: str) -> None:
        await self.wait_for_input_ready()
        subprocess.run(["tmux", "send-keys", "-t", TMUX_TARGET, "-l", text], timeout=5, check=True)
        await asyncio.sleep(0.15)
        subprocess.run(["tmux", "send-keys", "-t", TMUX_TARGET, "Enter"], timeout=5, check=True)

    def is_admin(self, author_id: str) -> bool:
        return author_id in self.admin_user_ids

    def can_run_admin_command(self, author_id: str, channel_id: str) -> bool:
        if not self.is_admin(author_id):
            return False
        return not ADMIN_CHANNEL_IDS or channel_id in ADMIN_CHANNEL_IDS

    def message_may_reach_agent(self, data: dict[str, Any]) -> tuple[bool, str]:
        channel_id = str(data.get("channel_id") or "")
        if not self.is_reply_to_coo_bot_message(data):
            return False, "requires_reply_to_coo_message"
        if channel_id in ADMIN_CHANNEL_IDS and not self.message_mentions_bot(data):
            return False, "admin_room_requires_reply_and_mention"
        return True, "allowed"

    def is_reply_to_coo_bot_message(self, data: dict[str, Any]) -> bool:
        ref_id = str((data.get("message_reference") or {}).get("message_id") or "")
        if not ref_id:
            return False
        referenced = data.get("referenced_message") or {}
        ref_author = referenced.get("author") or {}
        if self.bot_user_id and str(ref_author.get("id") or "") == self.bot_user_id:
            return True
        channel_id = str(data.get("channel_id") or "")
        conv = (self.state.get("open_conversations") or {}).get(channel_id) or {}
        return ref_id in {str(m) for m in conv.get("message_ids") or []}

    def message_mentions_bot(self, data: dict[str, Any]) -> bool:
        if not self.bot_user_id:
            return False
        for user in data.get("mentions") or []:
            if str(user.get("id") or "") == self.bot_user_id:
                return True
        content = str(data.get("content") or "")
        return f"<@{self.bot_user_id}>" in content or f"<@!{self.bot_user_id}>" in content

    def home_channel_id(self) -> str:
        return str(self.state.get("home_channel_id") or HOME_CHANNEL_ID)

    def channel_ids(self) -> set[str]:
        return BASE_CHANNEL_IDS | {str(c) for c in self.state.get("watched_channel_ids") or []}

    def prune_open_conversations(self) -> None:
        conversations = self.state.setdefault("open_conversations", {})
        now = time.time()
        expired = [
            channel_id for channel_id, conv in conversations.items()
            if float(conv.get("expires_at", 0)) <= now
        ]
        for channel_id in expired:
            conversations.pop(channel_id, None)
        if expired:
            self._save_state()

    def register_open_conversation(self, channel_id: str, message_id: str, content: str) -> None:
        self.prune_open_conversations()
        conversations = self.state.setdefault("open_conversations", {})
        conv = conversations.setdefault(channel_id, {
            "opened_at": time.time(),
            "message_ids": [],
            "user_ids": [],
        })
        message_ids = [str(m) for m in conv.get("message_ids") or []]
        message_ids.append(message_id)
        conv["message_ids"] = message_ids[-50:]
        user_ids = {str(u) for u in conv.get("user_ids") or []}
        user_ids.update(MENTION_RE.findall(content))
        conv["user_ids"] = sorted(user_ids)
        conv["expires_at"] = time.time() + CONVERSATION_TTL_SECONDS
        conv["last_bot_message_id"] = message_id
        conv["channel_name"] = self.channel_names.get(channel_id, channel_id)
        conv["targeted"] = bool(user_ids)
        self._save_state()
        self._event("conversation_opened", channel_id=channel_id, message_id=message_id, targeted=bool(user_ids))

    def close_open_conversation(self, channel_id: str) -> bool:
        conversations = self.state.setdefault("open_conversations", {})
        existed = channel_id in conversations
        conversations.pop(channel_id, None)
        if existed:
            self._save_state()
            self._event("conversation_closed", channel_id=channel_id)
        return existed

    def message_is_in_open_conversation(self, data: dict[str, Any]) -> bool:
        self.prune_open_conversations()
        channel_id = str(data.get("channel_id") or "")
        author_id = str((data.get("author") or {}).get("id") or "")
        conv = (self.state.get("open_conversations") or {}).get(channel_id)
        if not conv:
            return False
        ref_id = str((data.get("message_reference") or {}).get("message_id") or "")
        if ref_id and ref_id in {str(m) for m in conv.get("message_ids") or []}:
            return True
        user_ids = {str(u) for u in conv.get("user_ids") or []}
        if user_ids:
            return author_id in user_ids
        return True

    def followups_text(self) -> str:
        self.prune_open_conversations()
        conversations = self.state.get("open_conversations") or {}
        if not conversations:
            return "No open COO follow-ups."
        now = time.time()
        lines = ["Open COO follow-ups:"]
        for channel_id, conv in sorted(conversations.items()):
            remaining = max(0, int(float(conv.get("expires_at", 0)) - now))
            users = ", ".join(f"<@{u}>" for u in conv.get("user_ids") or []) or "channel"
            lines.append(f"- #{self.channel_names.get(channel_id, channel_id)}: {users}, expires in {remaining}s")
        return "\n".join(lines)

    def conversations_text(self) -> str:
        return self.followups_text()

    def inbox_text(self) -> str:
        index_path = REFERENCE_DIR / "_index.jsonl"
        if not index_path.exists():
            return f"No saved employee reference messages yet.\nInbox: `{REFERENCE_DIR}`"
        lines = index_path.read_text().splitlines()
        counts = self.reference_attention_counts()
        recent = []
        for row in self.reference_entries()[-5:]:
            recent.append(
                f"- `{row.get('attention_status')}` #{row.get('channel_name')} / {row.get('author_name')}: `{row.get('message_id')}`"
            )
        return (
            f"Saved employee reference messages: `{len(lines)}`\n"
            f"Pending: `{counts.get('pending', 0)}` | Queued: `{counts.get('queued', 0)}` | Held: `{counts.get('held', 0)}` | No action: `{counts.get('no-action', 0)}` | Initiated: `{counts.get('initiated', 0)}` | Failed: `{counts.get('failed', 0)}`\n"
            f"Inbox: `{REFERENCE_DIR}`\n"
            "Recent:\n" + "\n".join(recent)
        )

    def inbox_count(self) -> int:
        index_path = REFERENCE_DIR / "_index.jsonl"
        if not index_path.exists():
            return 0
        return len(index_path.read_text().splitlines())

    @staticmethod
    def format_seconds(seconds: int) -> str:
        seconds = max(0, seconds)
        days, rem = divmod(seconds, 86400)
        hours, rem = divmod(rem, 3600)
        minutes, _ = divmod(rem, 60)
        if days:
            return f"{days}d {hours}h"
        if hours:
            return f"{hours}h {minutes}m"
        return f"{minutes}m"

    def help_text(self) -> str:
        return (
            f"`{PREFIX} status` - session, queue, pane state\n"
            f"`{PREFIX} pulse` - ask the COO agent for an immediate operational check\n"
            f"`{PREFIX} send <prompt>` - admin direct prompt\n"
            f"`{PREFIX} watch` / `{PREFIX} unwatch` / `{PREFIX} home` - channel setup\n"
            f"`{PREFIX} close` / `{PREFIX} followups` - bot-owned follow-up control\n"
            f"`{PREFIX} inbox` - saved employee reference messages\n"
            f"`{PREFIX} queue` / `{PREFIX} review` - inbox workflow states and immediate review\n"
            f"`{PREFIX} facts` / `{PREFIX} updatefacts` - room factsheet view and refresh\n"
            f"`{PREFIX} tags` - reference/conversation classification tags\n"
            f"`{PREFIX} interrupt` - Esc current agent turn\n"
            f"`{PREFIX} compact` / `{PREFIX} clear` / `{PREFIX} enter`\n"
            f"`{PREFIX} channels` - configured Discord channel IDs"
        )

    def status_text(self) -> str:
        uptime = int(time.time() - self.started_at)
        return (
            "COO bridge status\n"
            f"- app_id: `{APPLICATION_ID}`\n"
            f"- guild: `{GUILD_ID}`\n"
            f"- agent_kind: `{AGENT_KIND}`\n"
            f"- tmux: `{TMUX_TARGET}`\n"
            f"- workdir: `{WORKDIR}`\n"
            f"- home_channel: `{self.home_channel_id()}`\n"
            f"- watched_channels: `{len(self.channel_ids())}`\n"
            f"- admin_ids_loaded: `{len(self.admin_user_ids)}`\n"
            f"- admin_channels: `{len(ADMIN_CHANNEL_IDS)}`\n"
            f"- conversation_mode: `{CONVERSATION_MODE}`\n"
            f"- open_conversations: `{len(self.state.get('open_conversations') or {})}`\n"
            f"- inbox_pending: `{self.reference_attention_counts().get('pending', 0)}`\n"
            f"- inbox_queued: `{self.reference_attention_counts().get('queued', 0)}`\n"
            f"- inbox_held: `{self.reference_attention_counts().get('held', 0)}`\n"
            f"- inbox_no_action: `{self.reference_attention_counts().get('no-action', 0)}`\n"
            f"- inbox_initiated: `{self.reference_attention_counts().get('initiated', 0)}`\n"
            f"- inbox_monitor_interval_s: `{int(INBOX_MONITOR_INTERVAL)}`\n"
            f"- pane_state: `{self.pane_state()}`\n"
            f"- service_queue: `{self.queue.qsize()}`\n"
            f"- proactive_interval_s: `{int(PROACTIVE_INTERVAL)}`\n"
            f"- uptime_s: `{uptime}`"
        )

    async def agent_forwarder(self) -> None:
        while not self.stop_event.is_set():
            try:
                await self.agent_forwarder_tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.log.exception("%s forwarder tick failed", AGENT_KIND)
            await asyncio.sleep(1.0)

    async def codex_forwarder(self) -> None:
        await self.agent_forwarder()

    def _init_existing_forwarder_offsets(self) -> None:
        offsets = self.state.setdefault("offsets", {})
        for path in self._matching_forwarder_files():
            offsets.setdefault(str(path), path.stat().st_size)
        self._save_state()

    def _init_existing_rollout_offsets(self) -> None:
        self._init_existing_forwarder_offsets()

    def _matching_forwarder_files(self) -> list[Path]:
        if AGENT_KIND == "claude":
            return self._matching_claude_transcript_files()
        return self._matching_rollout_files()

    def _matching_rollout_files(self) -> list[Path]:
        if not CODEX_SESSIONS.exists():
            return []
        files = sorted(CODEX_SESSIONS.rglob("rollout-*.jsonl"), key=lambda p: p.stat().st_mtime)
        return [p for p in files if self._rollout_matches_workdir(p)]

    def _matching_claude_transcript_files(self) -> list[Path]:
        project_dir = CLAUDE_PROJECTS / self.claude_project_slug(WORKDIR)
        if not project_dir.exists():
            return []
        return sorted(project_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)

    @staticmethod
    def claude_project_slug(path: Path) -> str:
        return path.resolve().as_posix().replace("/", "-")

    def _rollout_matches_workdir(self, path: Path) -> bool:
        try:
            with path.open() as f:
                for _ in range(80):
                    line = f.readline()
                    if not line:
                        break
                    if str(WORKDIR) in line and '"turn_context"' in line:
                        return True
        except Exception:
            return False
        return False

    async def agent_forwarder_tick(self) -> None:
        offsets = self.state.setdefault("offsets", {})
        changed = False
        for path in self._matching_forwarder_files():
            key = str(path)
            offset = int(offsets.get(key, 0))
            size = path.stat().st_size
            if size < offset:
                offset = 0
            if size == offset:
                continue
            with path.open("rb") as f:
                f.seek(offset)
                chunk = f.read(size - offset)
            offsets[key] = size
            changed = True
            for raw_line in chunk.splitlines():
                try:
                    event = json.loads(raw_line.decode("utf-8"))
                except Exception:
                    continue
                if AGENT_KIND == "claude":
                    await self.forward_claude_event(event)
                else:
                    await self.forward_codex_event(event)
        if changed:
            self._save_state()

    async def codex_forwarder_tick(self) -> None:
        await self.agent_forwarder_tick()

    async def forward_codex_event(self, event: dict[str, Any]) -> None:
        if event.get("type") != "event_msg":
            return
        payload = event.get("payload") or {}
        ptype = payload.get("type")
        if ptype not in {"agent_message", "error"}:
            return
        text = str(payload.get("message") or payload.get("error") or "").strip()
        if not text:
            return
        await self.forward_agent_output(text, str(ptype), source="codex")

    async def forward_claude_event(self, event: dict[str, Any]) -> None:
        if event.get("type") != "assistant" or event.get("isSidechain"):
            return
        message = event.get("message") or {}
        if not isinstance(message, dict) or message.get("role") != "assistant":
            return
        text = self.extract_claude_assistant_text(message)
        if not text:
            return
        await self.forward_agent_output(text, "agent_message", source="claude")

    @staticmethod
    def extract_claude_assistant_text(message: dict[str, Any]) -> str:
        content = message.get("content")
        if isinstance(content, str):
            return content.strip()
        if not isinstance(content, list):
            return ""
        parts = [
            str(item.get("text") or "").strip()
            for item in content
            if isinstance(item, dict) and item.get("type") == "text" and str(item.get("text") or "").strip()
        ]
        return "\n\n".join(parts).strip()

    async def forward_agent_output(self, text: str, ptype: str, *, source: str) -> None:
        text = text.strip()
        if not text:
            return
        if text.upper() == "NOOP":
            self.mark_active_reference_messages("no-action", decision_reason="agent_noop")
            self._event("agent_noop", source=source)
            return
        close_after = CONTROL_CLOSE in text
        hold_after = CONTROL_HOLD in text
        no_action_after = CONTROL_NO_ACTION in text
        text = (
            text
            .replace(CONTROL_CLOSE, "")
            .replace(CONTROL_HOLD, "")
            .replace(CONTROL_NO_ACTION, "")
            .strip()
        )
        channel_id = str(self.state.get("active_channel_id") or self.home_channel_id())
        reference = self.state.get("active_message_id")
        if ptype == "error":
            text = "COO agent error:\n" + text
            self.mark_active_reference_messages("failed", decision_reason="agent_error")
        if text:
            await self.send_discord(
                channel_id,
                text,
                reference_message_id=reference,
                opens_conversation=(ptype != "error" and not close_after and not hold_after and not no_action_after),
            )
        if ptype != "error":
            if hold_after:
                self.mark_active_reference_messages("held", decision_reason="agent_hold")
            elif no_action_after:
                self.mark_active_reference_messages("no-action", decision_reason="agent_no_action")
            else:
                self.mark_active_reference_messages("initiated", decision_reason="agent_message")
        if close_after:
            self.close_open_conversation(channel_id)
        self._event("agent_message_forwarded", channel_id=channel_id, chars=len(text), source=source)

    async def discord_get(self, route: str) -> Any:
        assert self.http is not None
        async with self.http.get(DISCORD_API + route, headers=self.auth_headers()) as resp:
            data = await resp.text()
            if resp.status >= 300:
                raise RuntimeError(f"Discord GET {route} failed {resp.status}: {data[:500]}")
            return json.loads(data) if data else None

    async def send_discord(
        self,
        channel_id: str,
        content: str,
        reference_message_id: str | None = None,
        *,
        opens_conversation: bool = False,
    ) -> None:
        chunks = self._split_discord(content)
        for idx, chunk in enumerate(chunks):
            body: dict[str, Any] = {
                "content": chunk,
                "allowed_mentions": {"parse": ["users"], "replied_user": False},
            }
            if reference_message_id and idx == 0:
                body["message_reference"] = {
                    "message_id": reference_message_id,
                    "channel_id": channel_id,
                    "guild_id": GUILD_ID,
                    "fail_if_not_exists": False,
                }
            created = await self.discord_post(f"/channels/{channel_id}/messages", body)
            if isinstance(created, dict):
                self.save_daily_transcript(created, direction="outbound")
            if opens_conversation and isinstance(created, dict) and created.get("id"):
                self.register_open_conversation(channel_id, str(created["id"]), chunk)

    async def discord_post(self, route: str, body: dict[str, Any]) -> Any:
        assert self.http is not None
        async with self.http.post(DISCORD_API + route, headers=self.auth_headers(json_body=True), json=body) as resp:
            text = await resp.text()
            if resp.status == 429:
                try:
                    retry = float(json.loads(text).get("retry_after", 1.0))
                except Exception:
                    retry = 1.0
                await asyncio.sleep(retry)
                return await self.discord_post(route, body)
            if resp.status >= 300:
                raise RuntimeError(f"Discord POST {route} failed {resp.status}: {text[:500]}")
            return json.loads(text) if text else None

    async def add_reaction(self, channel_id: str, message_id: str, emoji: str) -> None:
        assert self.http is not None
        route = f"/channels/{channel_id}/messages/{message_id}/reactions/{quote(emoji, safe='')}/@me"
        try:
            async with self.http.put(DISCORD_API + route, headers=self.auth_headers()) as resp:
                if resp.status >= 300:
                    self.log.debug("reaction failed %s: %s", resp.status, await resp.text())
        except Exception:
            self.log.debug("reaction failed", exc_info=True)

    @staticmethod
    def _split_discord(content: str) -> list[str]:
        if len(content) <= 1900:
            return [content]
        chunks: list[str] = []
        current = ""
        for line in content.splitlines(True):
            if len(current) + len(line) > 1900:
                chunks.append(current)
                current = ""
            current += line
        if current:
            chunks.append(current)
        return chunks or [content[:1900]]

    @staticmethod
    def auth_headers(json_body: bool = False) -> dict[str, str]:
        headers = {"Authorization": "Bot " + BOT_TOKEN}
        if json_body:
            headers["Content-Type"] = "application/json"
        return headers


def configure_logging() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=os.environ.get("DISCORD_COO_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(STATE_DIR / "discord_coo.log"),
        ],
    )


def main() -> None:
    configure_logging()
    bot = DiscordCOO()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def _stop(*_: Any) -> None:
        bot.stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _stop)
    loop.run_until_complete(bot.run())


if __name__ == "__main__":
    main()
