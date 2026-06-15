"""Verify AgentBridge.send_prompt chunks long pastes into separate tmux invocations.

The 17 KB Discord mission prompt previously crashed `tmux send-keys -l` on
first-launch of a tenant. send_prompt now splits the text at _PASTE_CHUNK
boundaries and submits one Enter at the end.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def _bridge_with_mock(monkeypatch):
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
    bridge = m.AgentBridge(m.Config.from_env())
    bridge._session_exists = lambda: True

    calls: list[list[str]] = []
    class _CR:
        returncode = 0
    def fake_run(cmd, *a, **kw):
        calls.append(list(cmd)); return _CR()
    monkeypatch.setattr(m.subprocess, "run", fake_run)
    monkeypatch.setattr(m.time, "sleep", lambda *_: None)
    return m, bridge, calls


def test_short_prompt_single_chunk(monkeypatch):
    m, bridge, calls = _bridge_with_mock(monkeypatch)
    bridge.send_prompt("hello world", cancel_first=False)
    paste = [c for c in calls if "-l" in c]
    enter = [c for c in calls if c[-1] == "Enter"]
    assert len(paste) == 1
    assert paste[0][-1] == "hello world"
    assert len(enter) == 2     # close-paste + submit


def test_long_prompt_is_chunked(monkeypatch):
    m, bridge, calls = _bridge_with_mock(monkeypatch)
    chunk_sz = bridge._PASTE_CHUNK
    # Build a 4 × chunk_size body so we know exactly how many chunks to expect.
    body = ("x" * chunk_sz) + ("y" * chunk_sz) + ("z" * chunk_sz) + "tail"
    bridge.send_prompt(body, cancel_first=False)
    paste = [c for c in calls if "-l" in c]
    assert len(paste) == 4, f"expected 4 chunks, got {len(paste)}"
    # Pasted-pieces concatenate to the original input exactly.
    rebuilt = "".join(c[-1] for c in paste)
    assert rebuilt == body
    # Single Enter pair at the end submits the whole paste.
    assert calls.count(["tmux", "send-keys", "-t", "t:agent.0", "Enter"]) == 2


def test_chunking_does_not_corrupt_unicode(monkeypatch):
    m, bridge, calls = _bridge_with_mock(monkeypatch)
    body = "ñá" * (bridge._PASTE_CHUNK)     # ~2× chunk_sz of multi-byte chars
    bridge.send_prompt(body, cancel_first=False)
    paste = [c for c in calls if "-l" in c]
    rebuilt = "".join(c[-1] for c in paste)
    assert rebuilt == body, "chunked paste must preserve original text verbatim"
