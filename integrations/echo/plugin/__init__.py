"""Echo integration — synthesises one fake metric value per sync.

Used to validate the integration framework end-to-end without needing OAuth
against a real SaaS app.
"""
import json
import random
import sqlite3
from datetime import datetime, timezone


def sync(tenant_db: str, team_slug: str, creds: dict) -> dict:
    """Insert a single echo_ping + a metric_value for 'echo_count'.

    Returns a result dict summarising what was written.
    """
    conn = sqlite3.connect(tenant_db)
    conn.row_factory = sqlite3.Row
    try:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        payload = {"timestamp": now, "noise": random.random()}
        with conn:
            conn.execute(
                "INSERT INTO echo_pings (observed_at, payload) VALUES (?, ?)",
                (now, json.dumps(payload)),
            )
            # Auto-register a metric if missing.
            existing = conn.execute(
                "SELECT id FROM metrics WHERE slug = 'echo_count'"
            ).fetchone()
            if existing is None:
                conn.execute(
                    "INSERT INTO metrics (slug, name, scope_kind, "
                    "  unit, source_app, cadence) "
                    "VALUES ('echo_count', 'Echo ping count', 'company', "
                    "  'count', 'echo', 'manual')"
                )
            metric_id = conn.execute(
                "SELECT id FROM metrics WHERE slug = 'echo_count'"
            ).fetchone()["id"]
            count = conn.execute(
                "SELECT COUNT(*) AS n FROM echo_pings"
            ).fetchone()["n"]
            conn.execute(
                "INSERT INTO metric_values (metric_id, observed_at, value, note) "
                "VALUES (?, ?, ?, ?)",
                (metric_id, now, float(count), f"team={team_slug}"),
            )
    finally:
        conn.close()
    return {"pings": 1, "metric": "echo_count"}


def bounce(creds: dict, **kwargs) -> dict:
    """Mock 'action' that echoes its arguments back."""
    return {"echo": kwargs}
