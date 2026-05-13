"""CLI commands for managing app integrations.

  coo integration list [<tenant>]
  coo integration enable <tenant> <slug> --team <team>
  coo integration disable <tenant> <slug>
  coo integration sync-now <tenant> <slug>
  coo integration logs <tenant> <slug>

Integrations are registered at the platform layer (one registry mirroring the
repo's integrations/ directory) and enabled per-tenant with a specific
scoped_team_slug. The bot picks up enabled integrations on its next loop tick.
"""
from __future__ import annotations

import importlib.util
import json
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path

import click

from .db import apply_migration, connect, transaction
from .paths import platform_db_path, repo_root
from .tenant import _get_tenant, _require_platform_installed


def _integrations_dir() -> Path:
    return repo_root() / "integrations"


def _load_manifest(slug: str) -> dict:
    path = _integrations_dir() / slug / "manifest.json"
    if not path.exists():
        click.echo(f"No integration {slug!r} in registry ({path}).", err=True)
        sys.exit(1)
    return json.loads(path.read_text())


def _load_plugin(slug: str):
    init = _integrations_dir() / slug / "plugin" / "__init__.py"
    if not init.exists():
        click.echo(f"Integration {slug!r} has no plugin/__init__.py", err=True)
        sys.exit(1)
    spec = importlib.util.spec_from_file_location(f"_integrations_{slug}", init)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _creds_path(tenant_dir: Path, slug: str) -> Path:
    d = tenant_dir / "integrations" / slug
    d.mkdir(parents=True, mode=0o700, exist_ok=True)
    return d / "credentials.json"


def _load_creds(tenant_dir: Path, slug: str) -> dict | None:
    p = _creds_path(tenant_dir, slug)
    return json.loads(p.read_text()) if p.exists() else None


def _save_creds(tenant_dir: Path, slug: str, creds: dict) -> None:
    p = _creds_path(tenant_dir, slug)
    p.write_text(json.dumps(creds))
    p.chmod(0o600)


@click.group(name="integration")
def integration_cmd():
    """Manage app integrations (HubSpot, GoTo, Gleap, …)."""


@integration_cmd.command(name="list")
@click.argument("slug", required=False)
def list_cmd(slug: str | None):
    """List integrations enabled per tenant.

    If SLUG given, only show for that tenant. Otherwise list everything.
    """
    _require_platform_installed()
    conn = connect(platform_db_path())
    q = (
        "SELECT ta.tenant_id, t.slug AS tenant, ta.integration_slug, "
        "       ta.scoped_team_slug, ta.status, ta.enabled_at, ta.revoked_at "
        "FROM tenant_apps ta JOIN tenants t ON t.id = ta.tenant_id"
    )
    args: list = []
    if slug:
        q += " WHERE t.slug = ?"
        args.append(slug)
    q += " ORDER BY t.slug, ta.integration_slug"
    rows = conn.execute(q, args).fetchall()
    conn.close()

    if not rows:
        click.echo("No integrations enabled."
                   + (" Use `coo integration enable <tenant> <slug>`." if not slug else ""))
        return
    click.echo(f"{'tenant':<15}  {'integration':<14}  {'team':<14}  {'status':<10}  enabled")
    for r in rows:
        click.echo(
            f"{r['tenant']:<15}  {r['integration_slug']:<14}  "
            f"{r['scoped_team_slug'] or '-':<14}  {r['status']:<10}  {r['enabled_at']}"
        )


@integration_cmd.command(name="enable")
@click.argument("tenant_slug")
@click.argument("integration_slug")
@click.option("--team", required=True, help="Team slug this integration is scoped to.")
def enable_cmd(tenant_slug: str, integration_slug: str, team: str):
    """Enable an integration for a tenant, scoped to a team."""
    _require_platform_installed()
    tenant = _get_tenant(tenant_slug)
    tenant_dir = Path(tenant["tenant_dir"])
    tenant_db = tenant_dir / "db" / "coo.db"
    manifest = _load_manifest(integration_slug)

    pconn = connect(platform_db_path())
    existing = pconn.execute(
        "SELECT id, status FROM tenant_apps "
        "WHERE tenant_id = ? AND integration_slug = ?",
        (tenant["id"], integration_slug),
    ).fetchone()
    if existing and existing["status"] in ("pending", "connected"):
        click.echo(
            f"{integration_slug!r} is already {existing['status']} for {tenant_slug!r}.",
            err=True,
        )
        pconn.close()
        sys.exit(1)
    pconn.close()

    # Apply schema first so the tables exist before any sync attempts.
    schema_path = _integrations_dir() / integration_slug / "schema.sql"
    if schema_path.exists():
        tconn = connect(tenant_db)
        try:
            with transaction(tconn):
                apply_migration(tconn, schema_path)
        finally:
            tconn.close()
        click.echo(f"  applied {integration_slug}/schema.sql")

    creds: dict = {}
    if manifest.get("needs_oauth"):
        plugin = _load_plugin(integration_slug)
        click.echo("")
        click.echo(f"--- {manifest['name']} OAuth setup ---")
        click.echo(
            "1) Create an app in the integration's developer portal with these scopes:"
        )
        for s in manifest.get("oauth", {}).get("scopes", []):
            click.echo(f"     - {s}")
        click.echo(
            "2) Set the redirect URI to:  http://localhost:8090/callback"
        )
        client_id = click.prompt("3) Paste the client_id").strip()
        client_secret = click.prompt("4) Paste the client_secret", hide_input=True).strip()
        redirect_uri = "http://localhost:8090/callback"
        state = secrets.token_urlsafe(16)
        url = plugin.oauth_url(client_id, redirect_uri, state)
        click.echo("")
        click.echo("5) From your PC, run an SSH tunnel:")
        click.echo("     ssh -L 8090:localhost:8090 <vm-host>")
        click.echo("6) Open this URL in your browser and complete the consent:")
        click.echo(f"     {url}")
        click.echo("7) When you hit the localhost:8090/callback page, copy the `code` "
                   "query-string value (or the entire callback URL) and paste it here.")
        code_or_url = click.prompt("Pasted code or URL").strip()
        if "code=" in code_or_url:
            import urllib.parse
            parsed = urllib.parse.urlparse(code_or_url)
            qs = urllib.parse.parse_qs(parsed.query)
            code = qs.get("code", [code_or_url])[0]
        else:
            code = code_or_url
        try:
            creds = plugin.exchange_code(client_id, client_secret, redirect_uri, code)
        except Exception as e:
            click.echo(f"OAuth code exchange failed: {e}", err=True)
            sys.exit(1)
        _save_creds(tenant_dir, integration_slug, creds)
        click.echo("  tokens saved to credentials.json (mode 0600)")

    pconn = connect(platform_db_path())
    try:
        with transaction(pconn):
            if existing:
                pconn.execute(
                    "UPDATE tenant_apps SET status = 'connected', "
                    "  scoped_team_slug = ?, enabled_at = datetime('now'), "
                    "  revoked_at = NULL WHERE id = ?",
                    (team, existing["id"]),
                )
            else:
                pconn.execute(
                    "INSERT INTO tenant_apps (tenant_id, integration_slug, "
                    "  scoped_team_slug, status) "
                    "VALUES (?, ?, ?, 'connected')",
                    (tenant["id"], integration_slug, team),
                )
            pconn.execute(
                "INSERT INTO platform_audit (action, tenant_id, payload_json) "
                "VALUES ('integration_enabled', ?, ?)",
                (tenant["id"], json.dumps({"slug": integration_slug, "team": team})),
            )
    finally:
        pconn.close()

    click.echo("")
    click.echo(
        f"Enabled {integration_slug!r} for {tenant_slug!r} scoped to team {team!r}."
    )
    click.echo(
        "The bot will pick it up on its next integration loop "
        f"(cadence: {manifest.get('sync_cadence_seconds', 'unset')}s)."
    )


@integration_cmd.command(name="disable")
@click.argument("tenant_slug")
@click.argument("integration_slug")
def disable_cmd(tenant_slug: str, integration_slug: str):
    """Revoke an integration. Per-app tables and historical data are preserved."""
    _require_platform_installed()
    tenant = _get_tenant(tenant_slug)
    pconn = connect(platform_db_path())
    try:
        with transaction(pconn):
            cur = pconn.execute(
                "UPDATE tenant_apps SET status = 'revoked', "
                "  revoked_at = datetime('now') "
                "WHERE tenant_id = ? AND integration_slug = ?",
                (tenant["id"], integration_slug),
            )
            if cur.rowcount:
                pconn.execute(
                    "INSERT INTO platform_audit (action, tenant_id, payload_json) "
                    "VALUES ('integration_disabled', ?, ?)",
                    (tenant["id"], json.dumps({"slug": integration_slug})),
                )
    finally:
        pconn.close()
    if cur.rowcount == 0:
        click.echo(f"No active enablement of {integration_slug!r} for {tenant_slug!r}.", err=True)
        sys.exit(1)
    click.echo(f"Revoked {integration_slug!r} for {tenant_slug!r}.")


@integration_cmd.command(name="sync-now")
@click.argument("tenant_slug")
@click.argument("integration_slug")
def sync_now_cmd(tenant_slug: str, integration_slug: str):
    """Force an immediate sync for a given (tenant, integration) pair.

    Runs the plugin's sync() in the operator's process (not the bot's). Useful
    for testing. The bot will continue its own cadence in parallel.
    """
    _require_platform_installed()
    tenant = _get_tenant(tenant_slug)
    tenant_dir = Path(tenant["tenant_dir"])
    tenant_db = tenant_dir / "db" / "coo.db"

    pconn = connect(platform_db_path())
    row = pconn.execute(
        "SELECT scoped_team_slug, status FROM tenant_apps "
        "WHERE tenant_id = ? AND integration_slug = ?",
        (tenant["id"], integration_slug),
    ).fetchone()
    pconn.close()
    if not row or row["status"] != "connected":
        click.echo(f"{integration_slug!r} not connected for {tenant_slug!r}.", err=True)
        sys.exit(1)

    plugin = _load_plugin(integration_slug)
    creds = _load_creds(tenant_dir, integration_slug) or {}
    started = datetime.now(timezone.utc).isoformat()
    try:
        result = plugin.sync(str(tenant_db), row["scoped_team_slug"], creds)
    except Exception as e:
        click.echo(f"Sync failed: {e}", err=True)
        sys.exit(1)
    # If the plugin refreshed creds, persist them.
    if isinstance(result, dict) and "creds_refreshed" in result:
        _save_creds(tenant_dir, integration_slug, result.pop("creds_refreshed"))
    click.echo(f"sync OK [{started}]: {json.dumps(result)}")


@integration_cmd.command(name="logs")
@click.argument("tenant_slug")
@click.argument("integration_slug", required=False)
def logs_cmd(tenant_slug: str, integration_slug: str | None):
    """Show recent integration sync events from the audit log."""
    _require_platform_installed()
    tenant = _get_tenant(tenant_slug)
    pconn = connect(platform_db_path())
    q = (
        "SELECT action, payload_json, occurred_at FROM platform_audit "
        "WHERE tenant_id = ? AND action LIKE 'integration_%' "
        "ORDER BY occurred_at DESC LIMIT 50"
    )
    rows = pconn.execute(q, (tenant["id"],)).fetchall()
    pconn.close()
    if not rows:
        click.echo("No integration audit entries yet.")
        return
    for r in rows:
        payload = json.loads(r["payload_json"]) if r["payload_json"] else {}
        if integration_slug and payload.get("slug") != integration_slug:
            continue
        click.echo(f"  {r['occurred_at']}  {r['action']}  {payload}")
