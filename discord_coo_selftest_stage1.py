#!/usr/bin/env python3
"""Stage-1 self-test for the Discord COO bot.

Exercises the new flag/routing/scheduler/dev-gate code paths with fake user IDs and
in-memory state, without needing real Discord credentials or live messages.

Run: python3 discord_coo_selftest_stage1.py
Exit non-zero if any assertion fails.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


# Set required env BEFORE importing the bot module so the module-level constants pick them up.
FAKE_TOKEN = "FAKE.bot.token"  # not validated at import time
TMP_ROOT = tempfile.mkdtemp(prefix="discord-coo-selftest-")
WORKDIR = Path(TMP_ROOT) / "workspace"
STATE_DIR = Path(TMP_ROOT) / "state"

os.environ["DISCORD_CLAUDEX_BOT_TOKEN"] = FAKE_TOKEN
os.environ["DISCORD_CLAUDEX_APPLICATION_ID"] = "111"
os.environ["DISCORD_COO_GUILD_ID"] = "999"
os.environ["DISCORD_COO_HOME_CHANNEL_ID"] = "888"
os.environ["DISCORD_COO_CHANNEL_IDS"] = "888"
os.environ["DISCORD_COO_ADMIN_CHANNEL_IDS"] = ""
os.environ["DISCORD_COO_ADMIN_USER_IDS"] = ""
os.environ["DISCORD_COO_GROUP_FEATURES_ENABLED"] = "0"
os.environ["DISCORD_COO_INBOX_ENABLED"] = "0"
os.environ["DISCORD_COO_DM_ONLY"] = "1"
os.environ["DISCORD_COO_DM_ALLOWLIST"] = "1001,1002,1003,1004"
os.environ["DISCORD_COO_DEV_USER_IDS"] = "1001,1002"
os.environ["DISCORD_COO_OWNER_USER_ID"] = "1003"
os.environ["DISCORD_COO_CEO_USER_ID"] = "1004"
os.environ["DISCORD_COO_WORKDIR"] = str(WORKDIR)
os.environ["DISCORD_COO_STATE_DIR"] = str(STATE_DIR)
os.environ["DISCORD_COO_INTERVIEW_TICK_SECONDS"] = "0.05"

sys.path.insert(0, str(Path(__file__).resolve().parent))

import discord_coo_bot as bot  # noqa: E402


def make_bot():
    obj = bot.DiscordCOO()
    obj._prepare_workspace()
    obj.bot_user_id = "777"
    obj.channel_names = {}
    return obj


class TestFlags(unittest.TestCase):
    def test_flags_parsed(self):
        self.assertFalse(bot.GROUP_FEATURES_ENABLED)
        self.assertFalse(bot.INBOX_ENABLED)
        self.assertTrue(bot.DM_ONLY)
        self.assertEqual(bot.DM_ALLOWLIST, {"1001", "1002", "1003", "1004"})
        self.assertEqual(bot.DEV_USER_IDS, {"1001", "1002"})
        self.assertEqual(bot.OWNER_USER_ID, "1003")
        self.assertEqual(bot.CEO_USER_ID, "1004")


class TestWorkspaceScaffold(unittest.TestCase):
    def test_company_map_layout(self):
        b = make_bot()
        cm = bot.COMPANY_MAP_DIR
        self.assertTrue((cm / "README.md").exists())
        self.assertTrue((cm / "org-chart.md").exists())
        self.assertTrue((cm / "priorities.md").exists())
        self.assertTrue((cm / "workflows.md").exists())
        self.assertTrue((cm / "factsheet-template.md").exists())
        self.assertTrue((cm / "interview-questions.md").exists())
        self.assertTrue((cm / "department-template.md").exists())
        self.assertTrue((cm / "project-template.md").exists())
        self.assertTrue((cm / "people").is_dir())
        self.assertTrue((cm / "departments").is_dir())
        self.assertTrue((cm / "projects").is_dir())


class TestNextContactMarker(unittest.TestCase):
    def test_marker_strip_and_record(self):
        b = make_bot()
        msg = (
            "Hello Sean.\n\n"
            "[[COO_NEXT_CONTACT user_id=1004 in_seconds=120 reason=follow up on org chart]]\n"
            "Talk soon."
        )
        cleaned, markers = b._record_next_contact(msg)
        self.assertNotIn("COO_NEXT_CONTACT", cleaned)
        self.assertIn("Hello Sean", cleaned)
        self.assertIn("Talk soon", cleaned)
        self.assertEqual(len(markers), 1)
        self.assertEqual(markers[0]["user_id"], "1004")
        self.assertEqual(markers[0]["in_seconds"], 120)
        contacts = b.state["next_contacts"]
        self.assertIn("1004", contacts)
        due_at = float(contacts["1004"]["due_at"])
        self.assertGreater(due_at, time.time() + 60)

    def test_minimum_seconds_floor(self):
        b = make_bot()
        msg = "[[COO_NEXT_CONTACT user_id=1003 in_seconds=5 reason=now]]"
        _, markers = b._record_next_contact(msg)
        self.assertEqual(markers[0]["in_seconds"], 30)


class TestConversationKind(unittest.TestCase):
    def test_kind_for_owner(self):
        b = make_bot()
        self.assertEqual(b._conversation_kind_for("1003", True, True), "dm_owner")
        self.assertEqual(b._conversation_kind_for("1004", True, True), "dm_ceo")
        self.assertEqual(b._conversation_kind_for("1001", True, True), "dm_developer")
        self.assertEqual(b._conversation_kind_for("9999", True, True), "dm_allowlisted")


class TestAdminGate(unittest.TestCase):
    def test_dm_allowlist_treated_as_admin(self):
        b = make_bot()
        self.assertTrue(b.is_admin("1001"))
        self.assertTrue(b.is_admin("1003"))
        self.assertFalse(b.is_admin("9999"))
        self.assertTrue(b.can_run_admin_command("1001", "channel-irrelevant"))


class TestDmCockpitText(unittest.TestCase):
    def test_cockpit_renders(self):
        b = make_bot()
        txt = b.dm_cockpit_text()
        self.assertIn("DM cockpit", txt)
        self.assertIn("Phase: 1", txt)
        self.assertIn("Group features: off", txt)
        self.assertIn("Inbox: off", txt)
        self.assertIn("DM allowlist size: 4", txt)
        self.assertIn("!coo phase", txt)

    def test_phase_text(self):
        b = make_bot()
        self.assertIn("Phase 1", b.dm_phase_text())
        self.assertIn("Phase 2", b.dm_phase_text())
        self.assertIn("Phase 3", b.dm_phase_text())

    def test_factsheet_missing(self):
        b = make_bot()
        out = b.dm_factsheet_text("nonexistent-person")
        self.assertIn("No factsheet", out)


class TestHandleMessageRouting(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.b = make_bot()
        self.b._save_state = mock.Mock()
        self.b.send_discord = mock.AsyncMock()
        self.b.add_reaction = mock.AsyncMock()
        self.b.save_daily_transcript = mock.Mock()
        self.b.handle_command = mock.AsyncMock()

    async def test_guild_message_ignored_in_dm_only(self):
        await self.b.handle_message({
            "guild_id": "999",
            "channel_id": "888",
            "id": "1",
            "author": {"id": "1001", "bot": False},
            "content": "hello from guild",
        })
        self.assertEqual(self.b.queue.qsize(), 0)
        self.b.send_discord.assert_not_awaited()

    async def test_dm_from_non_allowlisted_ignored(self):
        await self.b.handle_message({
            "channel_id": "dm1",
            "id": "1",
            "author": {"id": "9999", "bot": False},
            "content": "hi from stranger",
        })
        self.assertEqual(self.b.queue.qsize(), 0)

    async def test_dm_from_owner_queued(self):
        await self.b.handle_message({
            "channel_id": "dm1",
            "id": "1",
            "author": {"id": "1003", "bot": False},
            "content": "hi from owner",
        })
        self.assertEqual(self.b.queue.qsize(), 1)
        item = await self.b.queue.get()
        self.assertEqual(item.author_id, "1003")
        self.assertIn("dm_owner", item.text)

    async def test_dm_prefix_command_routes_to_handle_command(self):
        await self.b.handle_message({
            "channel_id": "dm1",
            "id": "1",
            "author": {"id": "1001", "bot": False},
            "content": f"{bot.PREFIX} cockpit",
        })
        self.b.handle_command.assert_awaited_once()


class TestDmCockpitCommandsViaPrefix(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.b = make_bot()
        self.sent = []
        async def fake_send(channel_id, content, **kw):
            self.sent.append((channel_id, content))
        self.b.send_discord = fake_send
        self.b._save_state = mock.Mock()

    async def _run(self, command):
        await self.b.handle_command(
            {"channel_id": "dm1", "id": "1", "author": {"id": "1001"}},
            command,
        )

    async def test_cockpit_phase_map_proposals_nextcontacts_factsheet(self):
        for cmd in ("cockpit", "phase", "map", "nextcontacts", "proposals"):
            await self._run(cmd)
        await self._run("factsheet sean-ceo")
        self.assertEqual(len(self.sent), 6)
        self.assertIn("DM cockpit", self.sent[0][1])
        self.assertIn("Phase 1", self.sent[1][1])
        self.assertIn("README.md", self.sent[2][1])
        self.assertIn("scheduled next-contacts", self.sent[3][1].lower())
        self.assertIn("no proposals", self.sent[4][1].lower())
        self.assertIn("no factsheet", self.sent[5][1].lower())

    async def test_group_only_command_blocked(self):
        await self._run("watch")
        self.assertIn("flagged off", self.sent[0][1])

    async def test_inbox_only_command_blocked(self):
        await self._run("inbox")
        self.assertIn("flagged off", self.sent[0][1])


class TestSchedulerTickQueuesPrompt(unittest.IsolatedAsyncioTestCase):
    async def test_due_contacts_queue(self):
        b = make_bot()
        b._save_state = mock.Mock()
        b._dm_channel_for_user = mock.AsyncMock(return_value="dm-channel-1")
        b.state["next_contacts"] = {
            "1003": {"due_at": time.time() - 1, "reason": "test", "queued": False}
        }
        await b.interview_scheduler_tick()
        self.assertEqual(b.queue.qsize(), 1)
        item = await b.queue.get()
        self.assertEqual(item.kind, "interview_scheduled")
        self.assertEqual(item.channel_id, "dm-channel-1")
        self.assertTrue(b.state["next_contacts"]["1003"]["queued"])

    async def test_not_due_does_not_queue(self):
        b = make_bot()
        b._save_state = mock.Mock()
        b._dm_channel_for_user = mock.AsyncMock(return_value="dm-channel-1")
        b.state["next_contacts"] = {
            "1003": {"due_at": time.time() + 600, "reason": "later", "queued": False}
        }
        await b.interview_scheduler_tick()
        self.assertEqual(b.queue.qsize(), 0)


class TestProposeDocChangeArgParsing(unittest.TestCase):
    def test_help_runs(self):
        import subprocess
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve().parent / "propose_doc_change.py"), "--help"],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("--path", result.stdout)
        self.assertIn("--summary", result.stdout)


class TestMissionTextHasPhases(unittest.TestCase):
    def test_dm_mission_mentions_phases(self):
        text = bot.COO_DM_MISSION
        self.assertIn("PHASE 1", text)
        self.assertIn("PHASE 2", text)
        self.assertIn("PHASE 3", text)
        self.assertIn("Sean", text)
        self.assertIn("Na'im", text)
        self.assertIn("Adrien", text)
        self.assertIn("Dan", text)


def main():
    try:
        result = unittest.main(argv=[sys.argv[0], "-v"], exit=False).result
        if result.wasSuccessful():
            print(f"\nselftest OK ({result.testsRun} tests)")
            return 0
        print(f"\nselftest FAILED ({len(result.failures)} fail / {len(result.errors)} err)")
        return 1
    finally:
        shutil.rmtree(TMP_ROOT, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
