#!/usr/bin/env python3
"""Repeatable loop tests for the Discord COO bridge.

The live Discord API can prove bot auth, guild/channel access, command
registration, and message send/read/delete. Discord does not let a bot invoke
its own slash commands or impersonate a human reply, so those gateway payloads
are exercised directly against the bridge handlers with synthetic events.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


ROOT = Path("/home/arman/workbench/vps-skill/discord")
DEFAULT_SECRETS = Path("/home/arman/workbench/.discord_claudex.secrets")
DEFAULT_STATE = Path("/home/arman/workbench/.discord_coo_state/state.json")
DISCORD_API = "https://discord.com/api/v10"


class LoopFailure(RuntimeError):
    pass


def load_env_file(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    pattern = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = pattern.match(line)
        if not match:
            continue
        key, value = match.groups()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        env[key] = value
    return env


def require(condition: bool, message: str) -> None:
    if not condition:
        raise LoopFailure(message)


def request_json(
    method: str,
    route: str,
    token: str,
    body: object | None = None,
    *,
    expect_json: bool = True,
) -> Any:
    payload = None if body is None else json.dumps(body).encode("utf-8")
    headers = {
        "Authorization": "Bot " + token,
        "User-Agent": "ClaudexDiscordCOOLoopTest/0.1",
    }
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = Request(DISCORD_API + route, data=payload, headers=headers, method=method)
    while True:
        try:
            with urlopen(req, timeout=30) as response:
                text = response.read().decode("utf-8")
                if not expect_json or not text:
                    return {}
                return json.loads(text)
        except HTTPError as exc:
            text = exc.read().decode("utf-8", "replace")
            if exc.code == 429:
                try:
                    retry_after = float(json.loads(text).get("retry_after", 1.0))
                except Exception:
                    retry_after = 1.0
                time.sleep(retry_after)
                continue
            raise LoopFailure(f"Discord {method} {route} failed {exc.code}: {text[:500]}") from exc


def parse_csv_ids(value: str | None) -> list[str]:
    return [part.strip() for part in (value or "").split(",") if part.strip()]


def compile_packaged_scripts() -> None:
    subprocess.run([
        sys.executable,
        "-m",
        "py_compile",
        str(ROOT / "discord_coo_bot.py"),
        str(ROOT / "bootstrap_discord_env.py"),
        str(ROOT / "register_cockpit_commands.py"),
        str(ROOT / "discord_coo_selfcheck.py"),
        str(ROOT / "discord_coo_looptest.py"),
    ], check=True)


def assert_systemd_active() -> dict[str, Any]:
    active = subprocess.run(
        ["systemctl", "is-active", "discord-coo.service"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    proc_count = subprocess.run(
        ["pgrep", "-caf", "[d]iscord_coo_bot.py"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    require(active == "active", f"discord-coo.service is not active: {active!r}")
    require(proc_count == "1", f"expected exactly one discord_coo_bot.py process, got {proc_count!r}")
    return {"service": active, "process_count": int(proc_count)}


def live_discord_checks(env: dict[str, str], *, keep_live_messages: bool) -> dict[str, Any]:
    token = env["DISCORD_CLAUDEX_BOT_TOKEN"]
    app_id = env["DISCORD_CLAUDEX_APPLICATION_ID"]
    guild_id = env["DISCORD_COO_GUILD_ID"]
    home_channel_id = env["DISCORD_COO_HOME_CHANNEL_ID"]
    watched_channel_ids = set(parse_csv_ids(env.get("DISCORD_COO_CHANNEL_IDS")))
    admin_channel_ids = set(parse_csv_ids(env.get("DISCORD_COO_ADMIN_CHANNEL_IDS")))

    bot_user = request_json("GET", "/users/@me", token)
    require(str(bot_user.get("id")) == app_id, "bot user id does not match application id")

    gateway = request_json("GET", "/gateway/bot", token)
    require(bool(gateway.get("url")), "gateway/bot did not return a websocket URL")

    channels = request_json("GET", f"/guilds/{guild_id}/channels", token)
    require(isinstance(channels, list), "guild channel list was not a list")
    channel_ids = {str(channel.get("id")) for channel in channels}
    missing_watched = sorted(watched_channel_ids - channel_ids)
    missing_admin = sorted(admin_channel_ids - channel_ids)
    require(not missing_watched, "watched channels missing from guild: " + ",".join(missing_watched))
    require(not missing_admin, "admin channels missing from guild: " + ",".join(missing_admin))

    commands = request_json("GET", f"/applications/{app_id}/guilds/{guild_id}/commands", token)
    command_map = {str(command.get("name")): command for command in commands if isinstance(command, dict)}
    require("coo" in command_map, "guild command /coo is not registered")
    coo_options = {str(option.get("name")) for option in command_map["coo"].get("options") or []}
    required_options = {"cockpit", "status", "inbox", "queue", "facts", "updatefacts", "tags", "review", "followups", "channels", "pulse"}
    require(required_options <= coo_options, "registered /coo command is missing options")

    marker = f"looptest-{int(time.time() * 1000)}"
    created = request_json("POST", f"/channels/{home_channel_id}/messages", token, {
        "content": f"COO loop test probe `{marker}`. This message should be deleted automatically.",
        "allowed_mentions": {"parse": []},
    })
    message_id = str(created.get("id") or "")
    require(message_id, "live message send did not return a message id")
    fetched = request_json("GET", f"/channels/{home_channel_id}/messages/{message_id}", token)
    require(marker in str(fetched.get("content") or ""), "live message fetch did not return the marker")
    request_json(
        "PUT",
        f"/channels/{home_channel_id}/messages/{message_id}/reactions/{quote('✅', safe='')}/@me",
        token,
        expect_json=False,
    )
    if not keep_live_messages:
        request_json("DELETE", f"/channels/{home_channel_id}/messages/{message_id}", token, expect_json=False)

    return {
        "bot": f"{bot_user.get('username')}#{bot_user.get('discriminator')}",
        "guild_channels": len(channels),
        "watched_channels": len(watched_channel_ids),
        "admin_channels": len(admin_channel_ids),
        "command_options": sorted(coo_options),
        "probe_message_id": message_id,
        "probe_deleted": not keep_live_messages,
    }


def import_bot_module(env: dict[str, str]) -> Any:
    os.environ.update(env)
    sys.path.insert(0, str(ROOT))
    if "discord_coo_bot" in sys.modules:
        return importlib.reload(sys.modules["discord_coo_bot"])
    return importlib.import_module("discord_coo_bot")


async def synthetic_gateway_and_interaction_checks(env: dict[str, str]) -> dict[str, Any]:
    botmod = import_bot_module(env)
    app_id = env["DISCORD_CLAUDEX_APPLICATION_ID"]
    guild_id = env["DISCORD_COO_GUILD_ID"]
    home_channel_id = env["DISCORD_COO_HOME_CHANNEL_ID"]
    admin_channel_id = parse_csv_ids(env.get("DISCORD_COO_ADMIN_CHANNEL_IDS"))[0]
    admin_user_id = parse_csv_ids(env.get("DISCORD_COO_ADMIN_USER_IDS"))[0] if parse_csv_ids(env.get("DISCORD_COO_ADMIN_USER_IDS")) else "admin-user"
    employee_user_id = "employee-user"

    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        botmod.STATE_DIR = tmpdir / "state"
        botmod.STATE_FILE = botmod.STATE_DIR / "state.json"
        botmod.EVENT_LOG = botmod.STATE_DIR / "events.jsonl"
        botmod.REFERENCE_DIR = tmpdir / "reference" / "inbox"
        botmod.TRANSCRIPT_DIR = tmpdir / "reference" / "transcripts"
        botmod.FACTSHEET_DIR = tmpdir / "reference" / "factsheets"
        botmod.ADMIN_CHANNEL_IDS = {admin_channel_id}
        botmod.BASE_CHANNEL_IDS = {home_channel_id, admin_channel_id}

        instance = botmod.DiscordCOO()
        instance.bot_user_id = app_id
        instance.admin_user_ids = {admin_user_id}
        instance.channel_names = {
            home_channel_id: "coo-cockpit",
            admin_channel_id: "coo-admin",
        }
        instance.state["open_conversations"] = {
            home_channel_id: {"message_ids": ["bot-msg-public"], "expires_at": time.time() + 3600},
            admin_channel_id: {"message_ids": ["bot-msg-admin"], "expires_at": time.time() + 3600},
        }

        sent_messages: list[dict[str, Any]] = []
        reactions: list[tuple[str, str, str]] = []
        callbacks: list[dict[str, Any]] = []

        async def fake_send_discord(
            channel_id: str,
            content: str,
            reference_message_id: str | None = None,
            *,
            opens_conversation: bool = False,
        ) -> None:
            sent_messages.append({
                "channel_id": channel_id,
                "content": content,
                "reference_message_id": reference_message_id,
                "opens_conversation": opens_conversation,
            })

        async def fake_add_reaction(channel_id: str, message_id: str, emoji: str) -> None:
            reactions.append((channel_id, message_id, emoji))

        async def fake_interaction_callback(data: dict[str, Any], body: dict[str, Any]) -> None:
            callbacks.append(body)

        instance.send_discord = fake_send_discord  # type: ignore[method-assign]
        instance.add_reaction = fake_add_reaction  # type: ignore[method-assign]
        instance.interaction_callback = fake_interaction_callback  # type: ignore[method-assign]
        instance.pane_state = lambda: "input_ready"  # type: ignore[method-assign]

        def message(
            message_id: str,
            channel_id: str,
            author_id: str,
            content: str,
            *,
            reply_to: str | None = None,
            mention_bot: bool = False,
        ) -> dict[str, Any]:
            data: dict[str, Any] = {
                "guild_id": guild_id,
                "channel_id": channel_id,
                "id": message_id,
                "timestamp": "2026-04-30T00:00:00+00:00",
                "content": content,
                "author": {
                    "id": author_id,
                    "username": "Synthetic User",
                    "global_name": "Synthetic User",
                    "bot": False,
                },
                "attachments": [],
                "mentions": [{"id": app_id}] if mention_bot else [],
            }
            if reply_to:
                data["message_reference"] = {"message_id": reply_to}
                data["referenced_message"] = {"author": {"id": app_id, "bot": True}}
            return data

        await instance.handle_message(message(
            "m-reference",
            home_channel_id,
            employee_user_id,
            "saved only, not a reply",
        ))
        require(instance.queue.qsize() == 0, "unsolicited public message should not queue")
        require((botmod.REFERENCE_DIR / "_index.jsonl").exists(), "reference-only message was not saved")

        await instance.handle_message(message(
            "m-public-reply",
            home_channel_id,
            employee_user_id,
            "reply that should queue",
            reply_to="bot-msg-public",
        ))
        require(instance.queue.qsize() == 1, "public reply to bot message did not queue")
        queued_public = await instance.queue.get()
        require(queued_public.kind == "discord_message_employee_reply", "public reply queued with wrong kind")

        await instance.handle_message(message(
            "m-admin-no-mention",
            admin_channel_id,
            admin_user_id,
            "admin reply without mention should be saved",
            reply_to="bot-msg-admin",
        ))
        require(instance.queue.qsize() == 0, "admin reply without mention should not queue")

        await instance.handle_message(message(
            "m-admin-with-mention",
            admin_channel_id,
            admin_user_id,
            f"<@{app_id}> admin reply with mention should queue",
            reply_to="bot-msg-admin",
            mention_bot=True,
        ))
        require(instance.queue.qsize() == 1, "admin reply with mention did not queue")
        queued_admin = await instance.queue.get()
        require(queued_admin.kind == "discord_message_admin", "admin reply queued with wrong kind")

        await instance.handle_message(message(
            "m-admin-command-denied",
            home_channel_id,
            admin_user_id,
            "!coo send should be denied outside admin room",
        ))
        require(sent_messages and "restricted" in sent_messages[-1]["content"], "admin command was not room-scoped")

        await instance.handle_message(message(
            "m-admin-command-send",
            admin_channel_id,
            admin_user_id,
            "!coo send synthetic admin prompt",
        ))
        require(instance.queue.qsize() == 1, "admin !coo send did not queue in admin room")
        queued_send = await instance.queue.get()
        require(queued_send.kind == "admin_send", "admin !coo send queued with wrong kind")

        interaction_base = {
            "guild_id": guild_id,
            "channel_id": admin_channel_id,
            "id": "interaction-id",
            "token": "interaction-token",
            "member": {"user": {"id": admin_user_id}},
        }
        await instance.handle_interaction({
            **interaction_base,
            "type": botmod.INTERACTION_APPLICATION_COMMAND,
            "data": {"name": "coo", "options": [{"name": "cockpit"}]},
        })
        require(callbacks[-1]["type"] == botmod.INTERACTION_RESPONSE_CHANNEL_MESSAGE, "/coo cockpit did not return a channel message")
        require(callbacks[-1]["data"].get("embeds"), "/coo cockpit did not include embeds")
        require(callbacks[-1]["data"]["embeds"][0].get("title") == "Claudex COO Cockpit", "/coo cockpit embed title is wrong")
        require(any(
            field.get("name") == "Claude Code Automation"
            for field in callbacks[-1]["data"]["embeds"][0].get("fields") or []
        ), "/coo cockpit embed missing Claude Code automation field")
        require(any(
            field.get("name") == "Reference Tags"
            for field in callbacks[-1]["data"]["embeds"][0].get("fields") or []
        ), "/coo cockpit embed missing reference tags field")
        require(any(
            component.get("custom_id") == "coo:queue"
            for row in callbacks[-1]["data"].get("components") or []
            for component in row.get("components") or []
        ), "cockpit missing In Queue button")
        require(any(
            component.get("custom_id") == "coo:seed"
            for row in callbacks[-1]["data"].get("components") or []
            for component in row.get("components") or []
        ), "admin cockpit missing seed button")
        require(any(
            component.get("custom_id") == "coo:followups"
            for row in callbacks[-1]["data"].get("components") or []
            for component in row.get("components") or []
        ), "admin cockpit missing open follow-ups button")
        require(any(
            component.get("custom_id") == "coo:queue"
            for row in callbacks[-1]["data"].get("components") or []
            for component in row.get("components") or []
        ), "admin cockpit missing inbox queue button")
        require(any(
            component.get("custom_id") == "coo:review"
            for row in callbacks[-1]["data"].get("components") or []
            for component in row.get("components") or []
        ), "admin cockpit missing review inbox button")
        require(any(
            component.get("custom_id") == "coo:state:pending"
            for row in callbacks[-1]["data"].get("components") or []
            for component in row.get("components") or []
        ), "admin cockpit missing pending state filter button")
        require(any(
            component.get("custom_id") == "coo:state:held"
            for row in callbacks[-1]["data"].get("components") or []
            for component in row.get("components") or []
        ), "admin cockpit missing held state filter button")
        require(any(
            component.get("custom_id") == "coo:facts"
            for row in callbacks[-1]["data"].get("components") or []
            for component in row.get("components") or []
        ), "cockpit missing factsheet button")
        require(any(
            component.get("custom_id") == "coo:updatefacts"
            for row in callbacks[-1]["data"].get("components") or []
            for component in row.get("components") or []
        ), "admin cockpit missing update facts button")

        await instance.handle_interaction({
            **interaction_base,
            "type": botmod.INTERACTION_MESSAGE_COMPONENT,
            "data": {"custom_id": "coo:seed"},
        })
        require(callbacks[-1]["type"] == botmod.INTERACTION_RESPONSE_MODAL, "seed button did not open modal")

        await instance.handle_interaction({
            **interaction_base,
            "type": botmod.INTERACTION_MODAL_SUBMIT,
            "data": {
                "custom_id": "coo:seed_modal",
                "components": [{"components": [{"custom_id": "prompt", "value": "modal seed prompt"}]}],
            },
        })
        require(instance.queue.qsize() == 1, "seed modal did not queue work")
        queued_modal = await instance.queue.get()
        require(queued_modal.kind == "admin_cockpit_seed", "seed modal queued with wrong kind")

        await instance.handle_interaction({
            **interaction_base,
            "type": botmod.INTERACTION_MESSAGE_COMPONENT,
            "data": {"custom_id": "coo:queue"},
        })
        require("In Queue" in callbacks[-1]["data"]["content"], "in queue button did not render queue")
        require("Claude Code automation equivalents" in callbacks[-1]["data"]["content"], "in queue button missing Claude Code automation lanes")
        require("pending" in callbacks[-1]["data"]["content"], "in queue button missing pending state")

        await instance.handle_interaction({
            **interaction_base,
            "type": botmod.INTERACTION_MESSAGE_COMPONENT,
            "data": {"custom_id": "coo:state:pending"},
        })
        require("Pending inbox/reference messages" in callbacks[-1]["data"]["content"], "pending state filter did not render")
        require("m-reference" in callbacks[-1]["data"]["content"], "pending state filter missing saved reference")

        await instance.handle_interaction({
            **interaction_base,
            "type": botmod.INTERACTION_MESSAGE_COMPONENT,
            "data": {"custom_id": "coo:facts"},
        })
        require("Room factsheets" in callbacks[-1]["data"]["content"], "factsheet button did not render")

        await instance.handle_interaction({
            **interaction_base,
            "type": botmod.INTERACTION_MESSAGE_COMPONENT,
            "data": {"custom_id": "coo:updatefacts"},
        })
        require(instance.queue.qsize() == 1, "update facts button did not queue work")
        queued_facts = await instance.queue.get()
        require(queued_facts.kind == "factsheet_update", "update facts queued with wrong kind")

        await instance.handle_interaction({
            **interaction_base,
            "type": botmod.INTERACTION_MESSAGE_COMPONENT,
            "data": {"custom_id": "coo:review"},
        })
        require(instance.queue.qsize() == 1, "review inbox button did not queue attention")
        queued_review = await instance.queue.get()
        require(queued_review.kind == "inbox_attention_manual_button", "review inbox queued with wrong kind")
        require(queued_review.reference_message_ids, "review inbox queued without reference ids")

        public_user_interaction = {
            **interaction_base,
            "channel_id": home_channel_id,
            "member": {"user": {"id": employee_user_id}},
            "type": botmod.INTERACTION_MESSAGE_COMPONENT,
            "data": {"custom_id": "coo:seed"},
        }
        await instance.handle_interaction(public_user_interaction)
        require("restricted" in callbacks[-1]["data"]["content"], "public seed button was not denied")

        await instance.handle_interaction({
            **interaction_base,
            "channel_id": home_channel_id,
            "member": {"user": {"id": employee_user_id}},
            "type": botmod.INTERACTION_APPLICATION_COMMAND,
            "data": {"name": "coo", "options": [{"name": "cockpit"}]},
        })
        require(not any(
            component.get("custom_id") == "coo:followups"
            for row in callbacks[-1]["data"].get("components") or []
            for component in row.get("components") or []
        ), "public cockpit exposed open follow-ups button")
        require(not any(
            component.get("custom_id") == "coo:queue"
            for row in callbacks[-1]["data"].get("components") or []
            for component in row.get("components") or []
        ), "public cockpit exposed inbox queue button")
        require(not any(
            str(component.get("custom_id") or "").startswith("coo:state:")
            for row in callbacks[-1]["data"].get("components") or []
            for component in row.get("components") or []
        ), "public cockpit exposed state filter buttons")

        await instance.forward_codex_event({
            "type": "event_msg",
            "payload": {"type": "agent_message", "message": "synthetic outbound [[COO_CLOSE]]"},
        })
        require(sent_messages[-1]["content"] == "synthetic outbound", "COO_CLOSE marker was not stripped")

        original_agent_kind = botmod.AGENT_KIND
        original_claude_projects = botmod.CLAUDE_PROJECTS
        original_workdir = botmod.WORKDIR
        try:
            botmod.AGENT_KIND = "claude"
            botmod.CLAUDE_PROJECTS = tmpdir / "claude-projects"
            botmod.WORKDIR = tmpdir / "discord-coo-workspace"
            project_dir = botmod.CLAUDE_PROJECTS / botmod.DiscordCOO.claude_project_slug(botmod.WORKDIR)
            project_dir.mkdir(parents=True)
            transcript = project_dir / "claude-session.jsonl"
            transcript.write_text(json.dumps({
                "type": "assistant",
                "cwd": str(botmod.WORKDIR),
                "message": {"role": "assistant", "content": [{"type": "text", "text": "old claude output"}]},
            }) + "\n")
            instance._init_existing_forwarder_offsets()
            before = len(sent_messages)
            with transcript.open("a") as f:
                f.write(json.dumps({
                    "type": "assistant",
                    "cwd": str(botmod.WORKDIR),
                    "message": {"role": "assistant", "content": [{"type": "text", "text": "synthetic claude outbound"}]},
                }) + "\n")
            await instance.agent_forwarder_tick()
            require(len(sent_messages) == before + 1, "Claude forwarder did not send new assistant text")
            require(sent_messages[-1]["content"] == "synthetic claude outbound", "Claude forwarder sent wrong content")
        finally:
            botmod.AGENT_KIND = original_agent_kind
            botmod.CLAUDE_PROJECTS = original_claude_projects
            botmod.WORKDIR = original_workdir

        transcript_files = list(botmod.TRANSCRIPT_DIR.rglob("*.md"))
        require(transcript_files, "synthetic messages did not write transcripts")

        return {
            "queued_paths": ["public_reply", "admin_reply_mention", "admin_command", "seed_modal", "inbox_review"],
            "reference_messages": len((botmod.REFERENCE_DIR / "_index.jsonl").read_text().splitlines()),
            "transcript_files": len(transcript_files),
            "captured_callbacks": len(callbacks),
            "captured_reactions": len(reactions),
        }


async def run_iteration(env: dict[str, str], args: argparse.Namespace, iteration: int) -> dict[str, Any]:
    compile_packaged_scripts()
    systemd = assert_systemd_active() if not args.skip_systemd else {"skipped": True}
    live = live_discord_checks(env, keep_live_messages=args.keep_live_messages) if not args.skip_live else {"skipped": True}
    synthetic = await synthetic_gateway_and_interaction_checks(env)
    return {
        "iteration": iteration,
        "systemd": systemd,
        "live_discord": live,
        "synthetic_gateway_interactions": synthetic,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Loop-test the Discord COO bridge.")
    parser.add_argument("--iterations", type=int, default=3, help="number of complete test iterations")
    parser.add_argument("--delay", type=float, default=1.0, help="seconds between iterations")
    parser.add_argument("--secrets-file", default=str(DEFAULT_SECRETS), help="Discord COO secrets env file")
    parser.add_argument("--skip-systemd", action="store_true", help="skip systemd/process checks")
    parser.add_argument("--skip-live", action="store_true", help="skip live Discord REST checks")
    parser.add_argument("--keep-live-messages", action="store_true", help="do not delete live probe messages")
    return parser.parse_args()


async def amain() -> int:
    args = parse_args()
    require(args.iterations > 0, "--iterations must be positive")
    env = {**load_env_file(Path(args.secrets_file)), **os.environ}
    required = [
        "DISCORD_CLAUDEX_BOT_TOKEN",
        "DISCORD_CLAUDEX_APPLICATION_ID",
        "DISCORD_COO_GUILD_ID",
        "DISCORD_COO_HOME_CHANNEL_ID",
        "DISCORD_COO_CHANNEL_IDS",
        "DISCORD_COO_ADMIN_CHANNEL_IDS",
    ]
    missing = [key for key in required if not env.get(key)]
    require(not missing, "missing required env: " + ", ".join(missing))

    results = []
    for iteration in range(1, args.iterations + 1):
        started = time.time()
        result = await run_iteration(env, args, iteration)
        result["duration_s"] = round(time.time() - started, 3)
        results.append(result)
        print(json.dumps({"ok": True, **result}, indent=2, sort_keys=True))
        if iteration != args.iterations:
            await asyncio.sleep(args.delay)
    print(json.dumps({
        "ok": True,
        "iterations": len(results),
        "live_messages_deleted": not args.keep_live_messages and not args.skip_live,
    }, indent=2, sort_keys=True))
    return 0


def main() -> int:
    try:
        return asyncio.run(amain())
    except (LoopFailure, subprocess.CalledProcessError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
