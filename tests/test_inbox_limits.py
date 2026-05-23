"""Rate-limit + ack-throttle for non-org-chart DMs."""
from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def _bot_with_state(tmp_path: Path):
    """Construct a minimal COOBot stand-in with just the inbox-limit machinery."""
    os.environ.update({
        "DISCORD_CLAUDEX_BOT_TOKEN": "x", "DISCORD_COO_GUILD_ID": "1",
        "DISCORD_COO_HOME_CHANNEL_ID": "1", "DISCORD_COO_CEO_USER_ID": "1",
        "DISCORD_COO_TENANT_SLUG": "t",
        "DISCORD_COO_TENANT_DB": str(tmp_path / "t.db"),
        "DISCORD_COO_PLATFORM_DB": str(tmp_path / "p.db"),
        "DISCORD_COO_STATE_DIR": str(tmp_path), "DISCORD_COO_WORKDIR": str(tmp_path),
        "DISCORD_COO_TMUX_SESSION": "t", "DISCORD_COO_RUN_AI": "/bin/true",
    })
    import importlib
    import messaging.discord.plugin.coo_phase1 as m
    importlib.reload(m)

    class _Mini:
        pass
    mb = _Mini()
    mb.cfg = m.Config.from_env()
    mb._inbox_state_path = mb.cfg.state_dir / "inbox_state.json"
    mb._inbox_state = m.COOBot._load_inbox_state.__get__(mb)()
    # Bind limit constants + methods
    for name in ("INBOX_SAVES_PER_DAY", "INBOX_ACKS_PER_DAY", "INBOX_WINDOW_SECONDS"):
        setattr(mb, name, getattr(m.COOBot, name))
    for name in ("_save_inbox_state", "_check_inbox_limits", "_load_inbox_state"):
        setattr(mb, name, getattr(m.COOBot, name).__get__(mb))
    return mb


def test_first_dm_allows_save_and_ack(tmp_path):
    bot = _bot_with_state(tmp_path)
    allow, ack = bot._check_inbox_limits(12345)
    assert allow is True
    assert ack is True


def test_second_dm_saves_but_skips_ack(tmp_path):
    bot = _bot_with_state(tmp_path)
    bot._check_inbox_limits(12345)  # first
    allow, ack = bot._check_inbox_limits(12345)  # second
    assert allow is True, "second save in 24h should still be allowed"
    assert ack is False, "second ack in 24h should be throttled"


def test_rate_limit_kicks_in_after_n_saves(tmp_path):
    bot = _bot_with_state(tmp_path)
    for _ in range(bot.INBOX_SAVES_PER_DAY):
        allow, _ = bot._check_inbox_limits(99)
        assert allow is True
    # 21st save should be blocked
    allow, _ = bot._check_inbox_limits(99)
    assert allow is False, "save past the daily cap should be dropped"


def test_different_uids_independent(tmp_path):
    bot = _bot_with_state(tmp_path)
    for _ in range(bot.INBOX_SAVES_PER_DAY):
        bot._check_inbox_limits(100)
    # uid 100 is capped, but uid 200 should be untouched
    allow, ack = bot._check_inbox_limits(200)
    assert allow is True
    assert ack is True


def test_state_persists_across_load(tmp_path):
    bot1 = _bot_with_state(tmp_path)
    bot1._check_inbox_limits(777)
    # New bot instance, same state dir
    bot2 = _bot_with_state(tmp_path)
    allow, ack = bot2._check_inbox_limits(777)
    assert ack is False, "ack should still be throttled after a 'restart'"
