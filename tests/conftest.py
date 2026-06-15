"""Shared pytest fixtures for the COO platform smoke tests."""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture
def platform_schema() -> Path:
    return REPO_ROOT / "platform" / "schema"


@pytest.fixture
def tenant_schema() -> Path:
    return REPO_ROOT / "tenant" / "schema"


@pytest.fixture
def fresh_platform_db(tmp_path: Path, platform_schema: Path) -> Path:
    db = tmp_path / "platform.db"
    conn = sqlite3.connect(str(db))
    for sql in sorted(platform_schema.glob("*.sql")):
        conn.executescript(sql.read_text())
    conn.close()
    return db


@pytest.fixture
def fresh_tenant_db(tmp_path: Path, tenant_schema: Path) -> Path:
    """Apply 0001_core.sql + 0002_proactive.sql + seed.sql to a fresh DB."""
    db = tmp_path / "tenant.db"
    conn = sqlite3.connect(str(db))
    schemas = sorted(p for p in tenant_schema.glob("*.sql") if p.name != "seed.sql")
    for sql in schemas:
        conn.executescript(sql.read_text())
    conn.executescript((tenant_schema / "seed.sql").read_text())
    conn.close()
    return db


@pytest.fixture
def bot_module():
    """Import the Phase 1 listener with stubbed env vars."""
    os.environ.update({
        "DISCORD_CLAUDEX_BOT_TOKEN": "test-token",
        "DISCORD_COO_GUILD_ID": "1",
        "DISCORD_COO_HOME_CHANNEL_ID": "1",
        "DISCORD_COO_CEO_USER_ID": "1",
        "DISCORD_COO_TENANT_SLUG": "test",
        "DISCORD_COO_TENANT_DB": "/tmp/test_tenant.db",
        "DISCORD_COO_PLATFORM_DB": "/tmp/test_platform.db",
        "DISCORD_COO_STATE_DIR": "/tmp/test_state",
        "DISCORD_COO_WORKDIR": "/tmp/test_workdir",
        "DISCORD_COO_TMUX_SESSION": "test",
        "DISCORD_COO_RUN_AI": "/bin/true",
    })
    import importlib
    import messaging.discord.plugin.coo_phase1 as m
    importlib.reload(m)
    return m
