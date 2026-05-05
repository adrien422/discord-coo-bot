#!/usr/bin/env python3
"""Local checks for the packaged Discord COO bridge."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import asyncio
from pathlib import Path


ROOT = Path("/home/arman/workbench/vps-skill/discord")
SECRETS = Path("/home/arman/workbench/.discord_claudex.secrets")


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> int:
    load_env_file(SECRETS)
    require(bool(os.environ.get("DISCORD_CLAUDEX_BOT_TOKEN")), "missing Discord bot token env")
    subprocess.run([
        sys.executable,
        "-m",
        "py_compile",
        str(ROOT / "discord_coo_bot.py"),
        str(ROOT / "bootstrap_discord_env.py"),
        str(ROOT / "register_cockpit_commands.py"),
        str(ROOT / "discord_coo_looptest.py"),
    ], check=True)

    sys.path.insert(0, str(ROOT))
    import discord_coo_bot as botmod  # noqa: PLC0415

    require(hasattr(botmod.DiscordCOO, "send_to_agent"), "send_to_agent is not a DiscordCOO method")
    require(hasattr(botmod.DiscordCOO, "agent_worker"), "agent_worker is not a DiscordCOO method")
    require(hasattr(botmod.DiscordCOO, "handle_interaction"), "handle_interaction is not a DiscordCOO method")
    require(hasattr(botmod.DiscordCOO, "cockpit_components"), "cockpit_components is not a DiscordCOO method")

    idle_fixture = """
╭────────────────────────────────────────────────╮
│ >_ OpenAI Codex (v0.125.0)                     │
╰────────────────────────────────────────────────╯

› Implement {feature}

  gpt-5.5 xhigh · ~/workbench/discord-coo-workspace



"""
    working_fixture = """
• Working (8m 29s • esc to interrupt)

› Use /skills to list available skills

  gpt-5.5 xhigh · ~/workbench/discord-coo-workspace
"""
    switch_fixture = """
Switch to gpt-5.4-mini for lower credit usage?

› 1. Switch to gpt-5.4-mini
  2. Keep current model
  3. Keep current model (never show again)

  Press enter to confirm or esc to go back
"""
    require(botmod.classify_codex_pane_text(idle_fixture) == "input_ready", "idle Codex fixture misclassified")
    require(botmod.classify_codex_pane_text(working_fixture) == "busy", "working Codex fixture misclassified")
    require(botmod.classify_codex_pane_text(switch_fixture) == "codex_model_switch_prompt", "model-switch prompt misclassified")
    claude_idle_fixture = """
Claude Code

\u276f Draft the status note

\u23f5\u23f5 bypass permissions on (shift+tab to cycle)
"""
    claude_busy_fixture = """
Claude Code

\u276f Draft the status note

\u23f5\u23f5 bypass permissions on (shift+tab to cycle) · esc to interrupt
"""
    claude_trust_fixture = "Quick safety check\nDo you trust this folder?"
    require(botmod.classify_claude_pane_text(claude_idle_fixture) == "input_ready", "idle Claude fixture misclassified")
    require(botmod.classify_claude_pane_text(claude_busy_fixture) == "busy", "busy Claude fixture misclassified")
    require(botmod.classify_claude_pane_text(claude_trust_fixture) == "trust_prompt", "Claude trust prompt misclassified")

    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        botmod.STATE_DIR = tmpdir
        botmod.STATE_FILE = tmpdir / "state.json"
        botmod.EVENT_LOG = tmpdir / "events.jsonl"
        instance = botmod.DiscordCOO()
        instance._event("selfcheck_event", kind="payload_kind")
        event = json.loads(botmod.EVENT_LOG.read_text().splitlines()[-1])
        require(event["kind"] == "selfcheck_event", "event kind was overwritten by payload")
        require(event["payload_kind"] == "payload_kind", "payload kind was not preserved")
        instance.admin_user_ids = {"admin-user"}
        botmod.ADMIN_CHANNEL_IDS = {"admin-room"}
        require(instance.can_run_admin_command("admin-user", "admin-room"), "admin room command should be allowed")
        require(not instance.can_run_admin_command("admin-user", "public-room"), "admin command should be room-scoped")
        require(not instance.can_run_admin_command("employee", "admin-room"), "non-admin should not run admin command")
        instance.bot_user_id = "bot-user"
        instance.state["open_conversations"] = {
            "public-room": {"message_ids": ["bot-message"]},
            "admin-room": {"message_ids": ["admin-bot-message"]},
        }
        public_reply = {
            "channel_id": "public-room",
            "message_reference": {"message_id": "bot-message"},
            "referenced_message": {"author": {"id": "bot-user"}},
            "content": "replying here",
        }
        admin_reply_no_mention = {
            "channel_id": "admin-room",
            "message_reference": {"message_id": "admin-bot-message"},
            "referenced_message": {"author": {"id": "bot-user"}},
            "content": "replying without mention",
            "mentions": [],
        }
        admin_reply_with_mention = {
            "channel_id": "admin-room",
            "message_reference": {"message_id": "admin-bot-message"},
            "referenced_message": {"author": {"id": "bot-user"}},
            "content": "<@bot-user> replying with mention",
            "mentions": [{"id": "bot-user"}],
        }
        direct_message = {"channel_id": "public-room", "content": "not a reply"}
        require(instance.message_may_reach_agent(public_reply)[0], "public/lower room reply should reach agent")
        require(not instance.message_may_reach_agent(admin_reply_no_mention)[0], "admin room reply without mention should not reach agent")
        require(instance.message_may_reach_agent(admin_reply_with_mention)[0], "admin room reply plus mention should reach agent")
        require(not instance.message_may_reach_agent(direct_message)[0], "non-reply message should not reach agent")
        cockpit_public = instance.cockpit_components("admin-user", "public-room")
        cockpit_admin = instance.cockpit_components("admin-user", "admin-room")
        require(any(c.get("custom_id") == "coo:status" for row in cockpit_public for c in row["components"]), "cockpit missing status button")
        require(not any(c.get("custom_id") == "coo:seed" for row in cockpit_public for c in row["components"]), "public cockpit should not expose seed button")
        require(not any(c.get("custom_id") == "coo:followups" for row in cockpit_public for c in row["components"]), "public cockpit should not expose open follow-ups")
        require(not any(c.get("custom_id") == "coo:queue" for row in cockpit_public for c in row["components"]), "public cockpit should not expose inbox queue")
        require(any(c.get("custom_id") == "coo:seed" for row in cockpit_admin for c in row["components"]), "admin cockpit missing seed button")
        require(any(c.get("custom_id") == "coo:followups" for row in cockpit_admin for c in row["components"]), "admin cockpit missing open follow-ups")
        require(any(c.get("custom_id") == "coo:queue" for row in cockpit_admin for c in row["components"]), "admin cockpit missing inbox queue")
        require(any(c.get("custom_id") == "coo:review" for row in cockpit_admin for c in row["components"]), "admin cockpit missing review inbox")
        require(any(c.get("custom_id") == "coo:state:pending" for row in cockpit_admin for c in row["components"]), "admin cockpit missing pending state filter")
        require(any(c.get("custom_id") == "coo:state:held" for row in cockpit_admin for c in row["components"]), "admin cockpit missing held state filter")
        require(any(c.get("custom_id") == "coo:state:no-action" for row in cockpit_admin for c in row["components"]), "admin cockpit missing no-action state filter")
        require(any(c.get("custom_id") == "coo:facts" for row in cockpit_admin for c in row["components"]), "cockpit missing factsheet button")
        require(any(c.get("custom_id") == "coo:updatefacts" for row in cockpit_admin for c in row["components"]), "admin cockpit missing update factsheet button")
        modal_data = {
            "data": {"components": [{
                "components": [{"custom_id": "prompt", "value": "seed this"}],
            }]},
        }
        require(instance.extract_modal_value(modal_data, "prompt") == "seed this", "modal prompt extraction failed")
        cockpit_text = instance.cockpit_text("admin-room")
        require("COO Cockpit" in cockpit_text, "cockpit text failed to render")
        cockpit_embeds = instance.cockpit_embeds("admin-user", "admin-room")
        require(cockpit_embeds and cockpit_embeds[0]["title"] == "Claudex COO Cockpit", "cockpit embed failed to render")
        embed_field_names = {field["name"] for field in cockpit_embeds[0]["fields"]}
        require({"Runtime", "Inbox Attention", "Follow-ups", "Claude Code Automation", "Reference Tags"} <= embed_field_names, "cockpit embed missing sections")
        require("Routines" in instance.claude_automation_summary(compact=False), "Claude Code routines lane missing")
        require("Schedules" in instance.claude_automation_summary(compact=False), "Claude Code schedules lane missing")
        require("Loops" in instance.claude_automation_summary(compact=False), "Claude Code loops lane missing")
        require("Monitors" in instance.claude_automation_summary(compact=False), "Claude Code monitors lane missing")
        botmod.REFERENCE_DIR = tmpdir / "reference" / "inbox"
        botmod.FACTSHEET_DIR = tmpdir / "reference" / "factsheets"
        reference_path = instance.save_reference_message({
            "guild_id": "guild",
            "channel_id": "public-room",
            "id": "reference-1",
            "timestamp": "2026-04-30T00:00:00+00:00",
            "content": "reference message pending attention",
            "author": {"id": "user-1", "username": "Sender Name"},
            "attachments": [],
        }, "selfcheck")
        require(reference_path.exists(), "reference message was not saved")
        require(instance.reference_attention_counts()["pending"] == 1, "reference message was not marked pending")
        reference_entries = instance.reference_entries()
        require(reference_entries[0]["state_tag"] == "state-pending", "reference entry missing pending state tag")
        require("status-pending" not in reference_entries[0]["tags"], "workflow state leaked into classification tags")
        require("source-inbox" in reference_entries[0]["tags"], "reference entry missing source tag")
        require("pending" in instance.inbox_queue_text(), "inbox queue text missing pending status")
        require("source-inbox" in instance.tag_summary_text(), "tag summary missing inbox tag")
        require("reference-1" in instance.state_filter_text("pending"), "state filter missing pending reference")
        factsheet_text = instance.factsheet_text("public-room")
        require("Room factsheets" in factsheet_text, "factsheet text missing title")
        require((botmod.FACTSHEET_DIR).exists(), "factsheet directory was not created")
        queued_count = asyncio.run(instance.queue_reference_attention("admin-room", "admin-user", trigger="selfcheck", respect_cooldown=False))
        require(queued_count == 1, "pending reference was not queued for attention")
        require(instance.reference_attention_counts()["queued"] == 1, "reference message was not marked queued")
        require(instance.reference_entries()[0]["state_tag"] == "state-queued", "reference entry missing queued state")
        require("status-queued" not in instance.reference_entries()[0]["tags"], "queued state leaked into classification tags")
        queued_item = instance.queue.get_nowait()
        require(queued_item.reference_message_ids == ["reference-1"], "queued prompt missing reference ids")
        in_queue_text = instance.in_queue_text()
        require("In Queue" in in_queue_text, "in queue text missing title")
        require("Claude Code automation equivalents" in in_queue_text, "in queue text missing Claude Code lanes")
        instance.mark_reference_messages(["reference-1"], "held")
        require("reference-1" in instance.state_filter_text("held"), "held state filter missing reference")
        asyncio.run(instance.queue_factsheet_update("public-room", "admin-user"))
        factsheet_item = instance.queue.get_nowait()
        require(factsheet_item.kind == "factsheet_update", "factsheet update queued with wrong kind")
        botmod.TRANSCRIPT_DIR = tmpdir / "transcripts"
        transcript_path = instance.save_daily_transcript({
            "guild_id": "guild",
            "channel_id": "public-room",
            "id": "message-1",
            "timestamp": "2026-04-30T00:00:00+00:00",
            "content": "daily transcript line",
            "author": {"id": "user-1", "username": "Sender Name"},
        }, "inbound")
        require(transcript_path.exists(), "daily transcript was not written")
        require("Sender Name (user-1)" in transcript_path.read_text(), "transcript missing clear sender name")
        original_agent_kind = botmod.AGENT_KIND
        original_claude_projects = botmod.CLAUDE_PROJECTS
        original_workdir = botmod.WORKDIR
        try:
            botmod.AGENT_KIND = "claude"
            botmod.CLAUDE_PROJECTS = tmpdir / "claude-projects"
            botmod.WORKDIR = tmpdir / "discord-coo-workspace"
            project_dir = botmod.CLAUDE_PROJECTS / botmod.DiscordCOO.claude_project_slug(botmod.WORKDIR)
            project_dir.mkdir(parents=True)
            transcript = project_dir / "session.jsonl"
            transcript.write_text(json.dumps({
                "type": "assistant",
                "cwd": str(botmod.WORKDIR),
                "message": {"role": "assistant", "content": [{"type": "text", "text": "old ignored"}]},
            }) + "\n")
            instance2 = botmod.DiscordCOO()
            instance2.state["active_channel_id"] = "public-room"
            instance2._init_existing_forwarder_offsets()
            sent: list[str] = []

            async def fake_send_discord(channel_id: str, content: str, reference_message_id: str | None = None, *, opens_conversation: bool = False) -> None:
                sent.append(content)

            instance2.send_discord = fake_send_discord  # type: ignore[method-assign]
            with transcript.open("a") as f:
                f.write(json.dumps({
                    "type": "assistant",
                    "cwd": str(botmod.WORKDIR),
                    "message": {"role": "assistant", "content": [
                        {"type": "thinking", "thinking": "hidden"},
                        {"type": "text", "text": "claude forwarded"},
                    ]},
                }) + "\n")
            asyncio.run(instance2.agent_forwarder_tick())
            require(sent == ["claude forwarded"], "Claude transcript forwarder did not forward assistant text once")
        finally:
            botmod.AGENT_KIND = original_agent_kind
            botmod.CLAUDE_PROJECTS = original_claude_projects
            botmod.WORKDIR = original_workdir

    import bootstrap_discord_env as bootstrap  # noqa: PLC0415

    current = [
        {"id": "role", "type": 0, "allow": "1024", "deny": "0"},
        {"id": "user", "type": 1, "allow": "2048", "deny": "0"},
    ]
    desired = [
        {"id": "user", "type": 1, "allow": "2048", "deny": "0"},
        {"id": "role", "type": 0, "allow": "1024", "deny": "0"},
    ]
    require(bootstrap.overwrites_match(current, desired), "permission overwrite comparison should ignore order")

    print("discord COO selfcheck OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
