"""Validate that schemas apply cleanly and contain the expected tables."""
from __future__ import annotations

import sqlite3


def _tables(db_path) -> set[str]:
    conn = sqlite3.connect(str(db_path))
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    conn.close()
    return {r[0] for r in rows}


def test_platform_schema_applies(fresh_platform_db):
    """Platform schema should produce the core registry tables."""
    tables = _tables(fresh_platform_db)
    expected = {"developers", "tenants", "tenant_apps", "platform_audit",
                "schema_version"}
    assert expected.issubset(tables), f"missing: {expected - tables}"


def test_platform_schema_has_mode_column(fresh_platform_db):
    """Migration 0002 must add the `mode` column to tenant_apps."""
    conn = sqlite3.connect(str(fresh_platform_db))
    cols = [r[1] for r in conn.execute("PRAGMA table_info(tenant_apps)")]
    conn.close()
    assert "mode" in cols, "tenant_apps.mode missing (migration 0002 didn't apply)"


def test_tenant_schema_applies(fresh_tenant_db):
    """Tenant core + proactive + seed apply cleanly with all expected tables."""
    tables = _tables(fresh_tenant_db)
    core = {"people", "teams", "channels", "interviews", "facts", "workflows",
            "tasks", "priorities", "reports", "followups", "scheduled_contacts",
            "transcripts", "doc_proposals", "inbox_items", "custom_fields",
            "audit_log", "system_config", "schema_version"}
    proactive = {"metrics", "metric_values", "decisions", "okrs", "key_results",
                 "task_dependencies", "risks", "commitments", "cadences",
                 "cadence_runs"}
    expected = core | proactive
    assert expected.issubset(tables), f"missing: {expected - tables}"


def test_tenant_seed_populates_system_config(fresh_tenant_db):
    conn = sqlite3.connect(str(fresh_tenant_db))
    rows = dict(conn.execute("SELECT key, value FROM system_config"))
    conn.close()
    assert rows.get("current_phase") == "0"
    assert "messaging_platform" in rows


def test_check_constraints_reject_bad_values(fresh_tenant_db):
    """STRICT tables with CHECK constraints should reject invalid enums."""
    conn = sqlite3.connect(str(fresh_tenant_db))
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        try:
            conn.execute(
                "INSERT INTO people (slug, display_name, access_tier) "
                "VALUES ('x', 'X', 'invalid_tier')"
            )
            conn.commit()
            raise AssertionError("CHECK should have rejected invalid access_tier")
        except sqlite3.IntegrityError:
            pass  # expected
    finally:
        conn.close()
