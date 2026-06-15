"""Tests for the chat-flavoured marker regexes in the Google Chat listener."""
from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def _load():
    os.environ.update({
        # Discord-side env (Chat module shares some helpers via import)
        "DISCORD_CLAUDEX_BOT_TOKEN": "x", "DISCORD_COO_GUILD_ID": "1",
        "DISCORD_COO_HOME_CHANNEL_ID": "1", "DISCORD_COO_CEO_USER_ID": "1",
        "DISCORD_COO_TENANT_SLUG": "t", "DISCORD_COO_TENANT_DB": "/tmp/t",
        "DISCORD_COO_PLATFORM_DB": "/tmp/p", "DISCORD_COO_STATE_DIR": "/tmp",
        "DISCORD_COO_WORKDIR": "/tmp", "DISCORD_COO_TMUX_SESSION": "t",
        "DISCORD_COO_RUN_AI": "/bin/true",
    })
    import importlib
    from messaging.google_chat.plugin import coo_chat
    importlib.reload(coo_chat)
    return coo_chat


def test_chat_to_marker_accepts_email():
    m = _load()
    mm = m.CHAT_TO_RE.search('[[COO_TO user_id=naim@zeevou.com]] Welcome.')
    assert mm is not None
    assert mm.group(1) == "naim@zeevou.com"
    assert mm.group(2).strip() == "Welcome."


def test_chat_to_marker_accepts_user_resource():
    m = _load()
    mm = m.CHAT_TO_RE.search('[[COO_TO user_id=users/123456789]] hi')
    assert mm is not None
    assert mm.group(1) == "users/123456789"


def test_chat_to_marker_accepts_quoted_email():
    m = _load()
    mm = m.CHAT_TO_RE.search('[[COO_TO user_id="naim@zeevou.com"]] body')
    assert mm is not None
    assert mm.group(1) == "naim@zeevou.com"


def test_chat_channel_marker_aliases():
    m = _load()
    cases = {
        "[[COO_CHANNEL name=General]] hello": ("General", None, "hello"),
        '[[COO_CHANNEL name="#General"]] hi': ("General", None, "hi"),
        "[[COO_CHANNEL space=General]] alias-space": ("General", None, "alias-space"),
        "[[COO_CHANNEL id=spaces/AAQAHpy_xHA]] by id": (None, "spaces/AAQAHpy_xHA", "by id"),
    }
    for text, (name, sid, body) in cases.items():
        mm = m.CHAT_CHANNEL_RE.search(text)
        assert mm is not None, text
        assert mm.group(1) == name
        assert mm.group(2) == sid
        assert mm.group(3).strip() == body


def test_chat_to_does_not_match_dm_with_only_text():
    m = _load()
    assert m.CHAT_TO_RE.search("plain internal note") is None


def test_chat_to_and_channel_coexist():
    m = _load()
    text = ('[[COO_TO user_id=naim@zeevou.com]] hello there '
            '[[COO_CHANNEL name=General]] team announcement')
    to = m.CHAT_TO_RE.search(text)
    ch = m.CHAT_CHANNEL_RE.search(text)
    assert to.group(1) == "naim@zeevou.com"
    assert to.group(2).strip() == "hello there"
    assert ch.group(1) == "General"
    assert ch.group(3).strip() == "team announcement"
