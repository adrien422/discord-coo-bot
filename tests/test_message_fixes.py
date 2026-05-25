"""Tests for the message-delivery fixes: chunking, busy-gate, time-boxed
dedup format, channel-post marker parsing."""
from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def _module():
    os.environ.update({
        "DISCORD_CLAUDEX_BOT_TOKEN": "x", "DISCORD_COO_GUILD_ID": "1",
        "DISCORD_COO_HOME_CHANNEL_ID": "1", "DISCORD_COO_CEO_USER_ID": "1",
        "DISCORD_COO_TENANT_SLUG": "t", "DISCORD_COO_TENANT_DB": "/tmp/t.db",
        "DISCORD_COO_PLATFORM_DB": "/tmp/p.db", "DISCORD_COO_STATE_DIR": "/tmp",
        "DISCORD_COO_WORKDIR": "/tmp", "DISCORD_COO_TMUX_SESSION": "t",
        "DISCORD_COO_RUN_AI": "/bin/true",
    })
    import importlib
    import messaging.discord.plugin.coo_phase1 as m
    importlib.reload(m)
    return m


m = _module()


def _bridge():
    b = m.AgentBridge(m.Config.from_env())
    b._fake = {"text": ""}
    b.capture = lambda lines=400: b._fake["text"]
    return b


def _pane(body: str, busy: bool = False) -> str:
    """Fake pane. busy=True includes the live 'esc to interrupt' status line."""
    status = "✶ Cooking… (12s · esc to interrupt)\n" if busy else "✻ Done\n"
    return f"● {body}\n{status}\n────────────\n❯ \n────────────\n"


# --- chunking -------------------------------------------------------------- #
def test_chunk_short_text_single_piece():
    assert m._chunk_message("hello") == ["hello"]
    assert m._chunk_message("") == []


def test_chunk_long_text_respects_limit_and_preserves_content():
    para = ("Sentence number {} here. ".format(i) for i in range(400))
    text = "".join(para)
    chunks = m._chunk_message(text, limit=500)
    assert len(chunks) > 1
    assert all(len(c) <= 500 for c in chunks)
    # No content lost (ignoring whitespace differences at the joins).
    joined = " ".join(chunks).split()
    assert joined == text.split()


def test_chunk_breaks_on_paragraph_boundary():
    text = "A" * 300 + "\n\n" + "B" * 300
    chunks = m._chunk_message(text, limit=400)
    assert chunks[0] == "A" * 300
    assert chunks[1] == "B" * 300


# --- busy gate ------------------------------------------------------------- #
def test_is_busy_detects_live_status_line():
    b = _bridge()
    assert b._is_busy(_pane("partial reply", busy=True)) is True
    assert b._is_busy(_pane("done reply", busy=False)) is False


def test_is_busy_ignores_stale_scrollback():
    b = _bridge()
    # 'esc to interrupt' only at the very top, then 40 lines of other content.
    text = "old: esc to interrupt\n" + "\n".join(f"line {i}" for i in range(40))
    assert b._is_busy(text) is False


def test_latest_response_held_while_busy_then_dispatched():
    b = _bridge()
    # Busy: even stable across polls, must not dispatch.
    b._fake["text"] = _pane("Yes, I can see it. Those are separate from", busy=True)
    assert b.latest_response() is None
    assert b.latest_response() is None
    # Generation finishes (full text, no busy line). First sight not yet stable.
    b._fake["text"] = _pane("Yes, I can see it. Those are separate from the COO scheduler.", busy=False)
    assert b.latest_response() is None
    out = b.latest_response()
    assert out is not None and out.endswith("COO scheduler.")


# --- time-boxed dedup persistence format ----------------------------------- #
def test_delivered_load_handles_legacy_and_new(tmp_path):
    import json
    os.environ["DISCORD_COO_STATE_DIR"] = str(tmp_path)
    mod = _module()
    bot_cls = mod.COOBot
    # Write a mixed file: legacy bare-string + new [text, ts].
    (tmp_path / "delivered.json").write_text(json.dumps({
        "111": "legacy text",
        "222": ["new text", 1700000000.0],
    }))
    # Build just enough of the bot to call _load_delivered.
    inst = bot_cls.__new__(bot_cls)
    inst._delivered_path = tmp_path / "delivered.json"
    loaded = bot_cls._load_delivered(inst)
    assert loaded[111] == ("legacy text", 0.0)
    assert loaded[222] == ("new text", 1700000000.0)


# --- channel-post marker --------------------------------------------------- #
def test_channel_marker_parses_name_id_and_hash():
    cases = {
        "[[COO_CHANNEL name=connected]] Hello team": ("connected", None, "Hello team"),
        '[[COO_CHANNEL name="#connected"]] Hi all': ("connected", None, "Hi all"),
        "[[COO_CHANNEL id=12345]] Posting this": (None, "12345", "Posting this"),
    }
    for text, (name, cid, body) in cases.items():
        mm = m.COO_CHANNEL_RE.search(text)
        assert mm is not None, text
        assert mm.group(1) == name
        assert mm.group(2) == cid
        assert mm.group(3).strip() == body


def test_channel_and_dm_markers_coexist():
    text = ("[[COO_TO user_id=9]] dm body "
            "[[COO_CHANNEL name=connected]] channel body")
    dm = m.COO_TO_RE.search(text)
    ch = m.COO_CHANNEL_RE.search(text)
    assert dm.group(2).strip() == "dm body"
    assert ch.group(1) == "connected" and ch.group(3).strip() == "channel body"
