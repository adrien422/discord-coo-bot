"""The capture stability gate: don't dispatch a reply until it stops changing."""
from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def _bridge(monkeypatch_pane):
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
    b = m.AgentBridge(m.Config.from_env())
    # Replace capture() with a controllable fake pane.
    b._fake = {"text": ""}
    b.capture = lambda lines=400: b._fake["text"]
    return b


def _pane(body: str, done: bool = True) -> str:
    """Render a fake pane with a `●` reply block, optionally followed by the
    'done' status line."""
    tail = "\n✻ Cooked for 3s\n" if done else "\n"
    return f"● {body}{tail}\n────────────\n❯ \n────────────\n"


def test_partial_reply_not_dispatched_until_stable():
    b = _bridge(None)
    # Poll 1: a partial reply mid-stream.
    b._fake["text"] = _pane("Full Sean thread so far: Me:", done=False)
    assert b.latest_response() is None, "partial should not dispatch on first sight"
    # Poll 2: it grew (still streaming).
    b._fake["text"] = _pane("Full Sean thread so far: Me: ... Sean:", done=False)
    assert b.latest_response() is None, "still-growing reply must not dispatch"
    # Poll 3: complete + stable.
    final = "Full Sean thread so far: Me: ... Sean: ... done."
    b._fake["text"] = _pane(final, done=True)
    assert b.latest_response() is None, "first sight of final is not yet stable"
    # Poll 4: unchanged → now stable → dispatch.
    out = b.latest_response()
    assert out is not None and "done." in out


def test_stable_reply_dispatched_once():
    b = _bridge(None)
    b._fake["text"] = _pane("Hello there", done=True)
    assert b.latest_response() is None        # first sight
    out = b.latest_response()                  # stable → dispatch
    assert out == "Hello there"
    # Subsequent identical polls must not re-dispatch.
    assert b.latest_response() is None
    assert b.latest_response() is None


def test_noop_never_dispatched():
    b = _bridge(None)
    b._fake["text"] = _pane("NOOP", done=True)
    assert b.latest_response() is None
    assert b.latest_response() is None


def test_prime_suppresses_existing_reply():
    b = _bridge(None)
    b._fake["text"] = _pane("a pre-restart reply", done=True)
    b.prime()  # mark current pane as already-handled
    # Even across stable polls, it must not be dispatched.
    assert b.latest_response() is None
    assert b.latest_response() is None
    # But a NEW reply still dispatches (after stabilising).
    b._fake["text"] = _pane("a brand new reply", done=True)
    assert b.latest_response() is None          # first sight
    assert b.latest_response() == "a brand new reply"
