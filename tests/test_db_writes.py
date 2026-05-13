"""Validate the bot's _record_* helpers correctly write to a fresh tenant DB."""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


class _MiniBot:
    """Minimal bot stand-in to invoke COOBot._record_* without Discord."""
    def __init__(self, cfg):
        self.cfg = cfg
        self._tenant_dir = cfg.state_dir.parent


def _seed_basic_tenant(tenant_db: Path) -> None:
    """Seed the tenant DB with a CEO + a team so subject resolution works."""
    conn = sqlite3.connect(str(tenant_db))
    conn.execute("PRAGMA foreign_keys = ON")
    with conn:
        conn.execute("INSERT INTO teams (slug, name) VALUES ('exec', 'Executive')")
        conn.execute(
            "INSERT INTO people (slug, display_name, discord_user_id, team_id, "
            "access_tier, is_content_approver) "
            "VALUES ('ceo', 'CEO', 999, 1, 'admin', 1)"
        )
    conn.close()


@pytest.fixture
def cfg_for(fresh_tenant_db, fresh_platform_db, tmp_path):
    import os
    state = tmp_path / "state"
    state.mkdir()
    os.environ.update({
        "DISCORD_CLAUDEX_BOT_TOKEN": "x",
        "DISCORD_COO_GUILD_ID": "1",
        "DISCORD_COO_HOME_CHANNEL_ID": "1",
        "DISCORD_COO_CEO_USER_ID": "999",
        "DISCORD_COO_TENANT_SLUG": "test",
        "DISCORD_COO_TENANT_DB": str(fresh_tenant_db),
        "DISCORD_COO_PLATFORM_DB": str(fresh_platform_db),
        "DISCORD_COO_STATE_DIR": str(state),
        "DISCORD_COO_WORKDIR": str(tmp_path / "workdir"),
        "DISCORD_COO_TMUX_SESSION": "test",
        "DISCORD_COO_RUN_AI": "/bin/true",
    })
    import importlib
    import messaging.discord.plugin.coo_phase1 as m
    importlib.reload(m)
    _seed_basic_tenant(fresh_tenant_db)
    return m, m.Config.from_env()


def _mini(bot_module, cfg) -> _MiniBot:
    """Attach record methods to a bare object for unit testing."""
    mb = _MiniBot(cfg)
    for name in (
        "_record_fact", "_record_commitment", "_record_decision",
        "_record_workflow", "_record_task", "_record_report",
        "_resolve_subject", "_person_id_for_uid",
    ):
        setattr(mb, name, getattr(bot_module.COOBot, name).__get__(mb))
    return mb


def test_record_fact_inserts(cfg_for):
    m, cfg = cfg_for
    mb = _mini(m, cfg)
    mb._record_fact("company", "founded", "2025", None, None)
    conn = sqlite3.connect(cfg.tenant_db)
    rows = conn.execute(
        "SELECT subject_kind, subject_id, predicate, object_text FROM facts"
    ).fetchall()
    conn.close()
    assert rows == [("company", None, "founded", "2025")]


def test_record_fact_dedups_within_window(cfg_for):
    m, cfg = cfg_for
    mb = _mini(m, cfg)
    mb._record_fact("company", "founded", "2025", None, None)
    mb._record_fact("company", "founded", "2025", None, None)
    conn = sqlite3.connect(cfg.tenant_db)
    n = conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
    conn.close()
    assert n == 1, "freshness dedup should suppress identical fact in 300s"


def test_record_commitment(cfg_for):
    m, cfg = cfg_for
    mb = _mini(m, cfg)
    mb._record_commitment(999, "ship onboarding", "2026-06-01", None)
    conn = sqlite3.connect(cfg.tenant_db)
    row = conn.execute(
        "SELECT description, due_at, status FROM commitments"
    ).fetchone()
    conn.close()
    assert row == ("ship onboarding", "2026-06-01", "open")


def test_record_decision(cfg_for):
    m, cfg = cfg_for
    mb = _mini(m, cfg)
    mb._record_decision("Defer launch", "Push GA to Q4", "Onboarding slipping",
                        "company", None, None)
    conn = sqlite3.connect(cfg.tenant_db)
    row = conn.execute(
        "SELECT title, decision_text, rationale, scope_kind FROM decisions"
    ).fetchone()
    conn.close()
    assert row == ("Defer launch", "Push GA to Q4", "Onboarding slipping", "company")


def test_record_workflow_upserts(cfg_for):
    m, cfg = cfg_for
    mb = _mini(m, cfg)
    mb._record_workflow({"slug": "onboarding", "name": "Customer Onboarding",
                         "description": "first version"}, None)
    mb._record_workflow({"slug": "onboarding", "name": "Customer Onboarding",
                         "description": "updated version"}, None)
    conn = sqlite3.connect(cfg.tenant_db)
    rows = conn.execute("SELECT slug, name, description FROM workflows").fetchall()
    conn.close()
    assert len(rows) == 1
    assert rows[0][2] == "updated version"


def test_record_report_writes_db_and_disk(cfg_for, tmp_path):
    m, cfg = cfg_for
    mb = _mini(m, cfg)
    mb._record_report(
        {"kind": "factsheet-team", "subject": "exec", "title": "Exec team"},
        "Some markdown body about the exec team.",
        None,
    )
    conn = sqlite3.connect(cfg.tenant_db)
    row = conn.execute(
        "SELECT report_kind, subject_kind, subject_id, content_md, file_path, is_current "
        "FROM reports"
    ).fetchone()
    conn.close()
    assert row[0] == "factsheet-team"
    assert row[1] == "team"
    assert row[2] == 1
    assert "exec team" in row[3].lower()
    assert row[5] == 1
    on_disk = Path(row[4])
    assert on_disk.exists()
    assert "exec team" in on_disk.read_text().lower()


def test_resolve_subject_variants(cfg_for):
    m, cfg = cfg_for
    mb = _mini(m, cfg)
    assert mb._resolve_subject("company") == ("company", None)
    # team by slug
    kind, sid = mb._resolve_subject("exec")
    assert kind == "team" and sid == 1
    # person by Discord user_id
    kind, sid = mb._resolve_subject("999")
    assert kind == "person" and sid == 1
