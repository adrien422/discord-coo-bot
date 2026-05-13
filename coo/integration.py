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
        "SELECT ta.tenant_id, t.slug AS tenant, ta.integration_slug, ta.mode, "
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
        click.echo("No integrations connected."
                   + (" Use `coo integration connect <tenant> <slug> --team T --mode M`." if not slug else ""))
        return
    click.echo(
        f"{'tenant':<15}  {'integration':<14}  {'mode':<8}  "
        f"{'team':<14}  {'status':<10}  enabled"
    )
    for r in rows:
        click.echo(
            f"{r['tenant']:<15}  {r['integration_slug']:<14}  "
            f"{(r['mode'] or '-'):<8}  {r['scoped_team_slug'] or '-':<14}  "
            f"{r['status']:<10}  {r['enabled_at']}"
        )


@integration_cmd.command(name="connect")
@click.argument("tenant_slug")
@click.argument("integration_slug")
@click.option("--team", required=True, help="Team slug this integration is scoped to.")
@click.option(
    "--mode",
    type=click.Choice(["mcp", "http", "plugin", "manual"]),
    default="plugin",
    help="How the agent reaches this app: mcp (preferred when an MCP server "
    "exists), http (generic HTTP+auth client), plugin (hand-written plugin "
    "under integrations/<slug>/), manual (record-only, no automation).",
)
def connect_cmd(tenant_slug: str, integration_slug: str, team: str, mode: str):
    """Connect an app to a tenant — four modes for dynamic integration.

    The agent learns what apps a company uses during interviews and records
    them as facts. The operator runs this to wire one up. Most apps should
    use --mode mcp or --mode http; --mode plugin is for narrow cases that
    need hand-written code; --mode manual records the app as informational
    only.
    """
    _require_platform_installed()
    tenant = _get_tenant(tenant_slug)
    tenant_dir = Path(tenant["tenant_dir"])
    tenant_db = tenant_dir / "db" / "coo.db"

    # Plugin-mode requires a hand-written manifest. Other modes don't.
    manifest: dict = {}
    if mode == "plugin":
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

    config: dict = {}
    creds: dict = {}

    # Per-mode setup.
    if mode == "plugin":
        schema_path = _integrations_dir() / integration_slug / "schema.sql"
        if schema_path.exists():
            tconn = connect(tenant_db)
            try:
                with transaction(tconn):
                    apply_migration(tconn, schema_path)
            finally:
                tconn.close()
            click.echo(f"  applied {integration_slug}/schema.sql")
        if manifest.get("needs_oauth"):
            plugin = _load_plugin(integration_slug)
            click.echo("")
            click.echo(f"--- {manifest['name']} OAuth setup ---")
            for s in manifest.get("oauth", {}).get("scopes", []):
                click.echo(f"  scope: {s}")
            click.echo("Redirect URI: http://localhost:8090/callback")
            client_id = click.prompt("client_id").strip()
            client_secret = click.prompt("client_secret", hide_input=True).strip()
            url = plugin.oauth_url(
                client_id, "http://localhost:8090/callback",
                secrets.token_urlsafe(16),
            )
            click.echo("\n1) ssh -L 8090:localhost:8090 <vm>")
            click.echo("2) Open in browser:")
            click.echo(f"   {url}")
            click.echo("3) Paste the `code` query parameter (or full callback URL):")
            code_or_url = click.prompt("code").strip()
            if "code=" in code_or_url:
                import urllib.parse
                qs = urllib.parse.parse_qs(urllib.parse.urlparse(code_or_url).query)
                code = qs.get("code", [code_or_url])[0]
            else:
                code = code_or_url
            try:
                creds = plugin.exchange_code(
                    client_id, client_secret,
                    "http://localhost:8090/callback", code,
                )
            except Exception as e:
                click.echo(f"OAuth code exchange failed: {e}", err=True)
                sys.exit(1)
            _save_creds(tenant_dir, integration_slug, creds)

    elif mode == "http":
        click.echo("--- Generic HTTP connection setup ---")
        base_url = click.prompt("Base URL (e.g. https://api.example.com/v1)").strip()
        auth_scheme = click.prompt(
            "Auth scheme",
            type=click.Choice(["bearer", "api_key_header", "basic", "none"]),
            default="bearer",
        )
        token = click.prompt("Token / API key (hidden)", hide_input=True).strip()
        header_name = ""
        if auth_scheme == "api_key_header":
            header_name = click.prompt("Header name", default="X-API-Key")
        click.echo(
            "Whitelist allowed actions/queries (one path pattern per line; "
            "method:path; blank line to finish). Example:  GET:/contacts"
        )
        actions: list[str] = []
        while True:
            line = click.prompt("  pattern", default="", show_default=False).strip()
            if not line:
                break
            actions.append(line)
        config = {
            "base_url": base_url.rstrip("/"),
            "auth_scheme": auth_scheme,
            "header_name": header_name or None,
            "actions": actions,
        }
        creds = {"token": token}
        _save_creds(tenant_dir, integration_slug, creds)

    elif mode == "mcp":
        click.echo("--- MCP server setup ---")
        click.echo(
            "Per-tenant MCP servers go in the tenant's CLAUDE_CONFIG_DIR "
            "(~/.local/share/coo/tenants/<slug>/.claude/mcp.json or similar)."
        )
        command = click.prompt(
            "MCP server command (e.g. 'npx -y @hubspot/mcp-server')"
        ).strip()
        env_lines = click.prompt(
            "Env vars as KEY=value (comma-separated, e.g. HUBSPOT_TOKEN=xxx,FOO=bar)",
            default="", show_default=False,
        ).strip()
        env: dict[str, str] = {}
        if env_lines:
            for pair in env_lines.split(","):
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    env[k.strip()] = v.strip()
        config = {"command": command, "env": env}
        # Write/merge MCP config under tenant's .claude/
        mcp_cfg = tenant_dir / ".claude" / ".mcp.json"
        mcp_cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg_data = {}
        if mcp_cfg.exists():
            try:
                cfg_data = json.loads(mcp_cfg.read_text())
            except Exception:
                cfg_data = {}
        cfg_data.setdefault("mcpServers", {})[integration_slug] = {
            "command": command.split()[0],
            "args": command.split()[1:],
            "env": env,
        }
        mcp_cfg.write_text(json.dumps(cfg_data, indent=2))
        mcp_cfg.chmod(0o600)
        click.echo(f"  wrote MCP config to {mcp_cfg}")
        click.echo(
            "  NOTE: agent must be restarted (`coo tenant stop` then `start`) "
            "for Claude Code to pick up the new MCP server."
        )

    elif mode == "manual":
        click.echo("Mode 'manual' — recording the app as informational only.")

    pconn = connect(platform_db_path())
    try:
        with transaction(pconn):
            if existing:
                pconn.execute(
                    "UPDATE tenant_apps SET status = 'connected', mode = ?, "
                    "  scoped_team_slug = ?, config_json = ?, "
                    "  enabled_at = datetime('now'), revoked_at = NULL "
                    "WHERE id = ?",
                    (mode, team, json.dumps(config) if config else None, existing["id"]),
                )
            else:
                pconn.execute(
                    "INSERT INTO tenant_apps (tenant_id, integration_slug, mode, "
                    "  scoped_team_slug, status, config_json) "
                    "VALUES (?, ?, ?, ?, 'connected', ?)",
                    (tenant["id"], integration_slug, mode, team,
                     json.dumps(config) if config else None),
                )
            pconn.execute(
                "INSERT INTO platform_audit (action, tenant_id, payload_json) "
                "VALUES ('integration_connected', ?, ?)",
                (tenant["id"], json.dumps({
                    "slug": integration_slug, "team": team, "mode": mode,
                })),
            )
    finally:
        pconn.close()

    click.echo("")
    click.echo(
        f"Connected {integration_slug!r} for {tenant_slug!r} "
        f"(mode={mode}, team={team})."
    )
    if mode == "mcp":
        click.echo("Restart the bot so Claude Code picks up the new MCP server.")
    elif mode in ("plugin", "http"):
        click.echo("Bot will use it on the next integration loop tick.")


# Back-compat alias: `coo integration enable` → `connect --mode plugin`
@integration_cmd.command(name="enable", hidden=True)
@click.argument("tenant_slug")
@click.argument("integration_slug")
@click.option("--team", required=True)
@click.pass_context
def enable_alias(ctx, tenant_slug, integration_slug, team):
    """Deprecated alias for `connect --mode plugin`."""
    ctx.invoke(
        connect_cmd,
        tenant_slug=tenant_slug, integration_slug=integration_slug,
        team=team, mode="plugin",
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
