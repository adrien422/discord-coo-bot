import json
import os
import re
import signal
import subprocess
import sys
from pathlib import Path

import click

from .db import apply_migration, connect, transaction
from .paths import platform_db_path, repo_root, tenant_schema_dir, tenants_dir

SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{1,30}$")


def _get_tenant(slug: str) -> dict:
    conn = connect(platform_db_path())
    row = conn.execute(
        "SELECT id, slug, company_name, messaging_platform, phase, status, tenant_dir "
        "FROM tenants WHERE slug = ?",
        (slug,),
    ).fetchone()
    conn.close()
    if not row:
        click.echo(f"No tenant with slug {slug!r}.", err=True)
        sys.exit(1)
    return dict(row)


def _require_platform_installed():
    if not platform_db_path().exists():
        click.echo("Platform not installed. Run `coo platform install` first.", err=True)
        sys.exit(1)


def _slug_for_name(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s or "person"


@click.group(name="tenant")
def tenant_cmd():
    """Manage tenants."""


@tenant_cmd.command(name="list")
def list_cmd():
    _require_platform_installed()
    conn = connect(platform_db_path())
    rows = conn.execute(
        "SELECT id, slug, company_name, messaging_platform, phase, status, schema_version, tenant_dir "
        "FROM tenants ORDER BY id"
    ).fetchall()
    conn.close()
    if not rows:
        click.echo("No tenants registered. Use `coo tenant new` to onboard one.")
        return
    click.echo(
        f"{'id':>3}  {'slug':<15}  {'company':<25}  {'messaging':<10}  {'phase':<5}  {'status':<10}  pid"
    )
    for r in rows:
        pid_file = Path(r["tenant_dir"]) / "state" / "bot.pid"
        pid = "-"
        if pid_file.exists():
            try:
                p = int(pid_file.read_text().strip())
                os.kill(p, 0)
                pid = str(p)
            except (OSError, ValueError):
                pid = "stale"
        click.echo(
            f"{r['id']:>3}  {r['slug']:<15}  {r['company_name']:<25}  "
            f"{r['messaging_platform']:<10}  {r['phase']:<5}  {r['status']:<10}  {pid}"
        )


@tenant_cmd.command(name="new")
def new_cmd():
    """Bootstrap a new tenant interactively."""
    _require_platform_installed()

    click.echo("--- Tenant identity ---")
    slug = click.prompt("Tenant slug (lowercase letters, digits, hyphens)").strip()
    if not SLUG_RE.match(slug):
        click.echo("Invalid slug. Must match ^[a-z][a-z0-9-]{1,30}$", err=True)
        sys.exit(1)

    tenant_dir = tenants_dir() / slug
    if tenant_dir.exists():
        click.echo(f"Tenant directory already exists: {tenant_dir}", err=True)
        sys.exit(1)

    company_name = click.prompt("Company name").strip()

    click.echo("\n--- Discord setup ---")
    bot_token = click.prompt("Discord bot token", hide_input=True).strip()
    guild_id = click.prompt("Discord guild ID").strip()
    home_channel_id = click.prompt("Home channel ID (where the COO posts)").strip()

    click.echo("\n--- CEO identity ---")
    ceo_name = click.prompt("CEO display name").strip()
    ceo_discord_id = click.prompt("CEO Discord user ID").strip()
    if not ceo_discord_id.isdigit():
        click.echo("Discord user ID must be numeric.", err=True)
        sys.exit(1)
    ceo_email = click.prompt("CEO email (optional)", default="", show_default=False).strip()
    ceo_slug = _slug_for_name(ceo_name)

    click.echo(f"\nAbout to create tenant {slug!r} ({company_name}).")
    click.echo(f"  Directory:   {tenant_dir}")
    click.echo(f"  CEO:         {ceo_name} (Discord ID {ceo_discord_id})")
    if not click.confirm("Proceed?", default=True):
        sys.exit(0)

    tenant_dir.mkdir(parents=True)
    for sub in ("db", "transcripts", "reports", "state", "company-map/people", "company-map/factsheets"):
        (tenant_dir / sub).mkdir(parents=True, exist_ok=True)
    (tenant_dir / "messaging").mkdir(mode=0o700, exist_ok=True)
    tenant_dir.chmod(0o750)

    tenant_db = tenant_dir / "db" / "coo.db"
    schemas = sorted(p for p in tenant_schema_dir().glob("*.sql") if p.name != "seed.sql")
    seed = tenant_schema_dir() / "seed.sql"

    conn = connect(tenant_db)
    try:
        with transaction(conn):
            for sql in schemas:
                click.echo(f"Applying {sql.name}")
                apply_migration(conn, sql)
            apply_migration(conn, seed)

            conn.execute("INSERT INTO teams (slug, name) VALUES ('exec', 'Executive')")
            ceo_email_val = ceo_email or None
            conn.execute(
                "INSERT INTO people (slug, display_name, email, discord_user_id, role, "
                "team_id, access_tier, is_content_approver, is_phase1_interview_target) "
                "VALUES (?, ?, ?, ?, 'CEO', 1, 'admin', 1, 1)",
                (ceo_slug, ceo_name, ceo_email_val, int(ceo_discord_id)),
            )
            ceo_pid = conn.execute(
                "SELECT id FROM people WHERE slug = ?", (ceo_slug,)
            ).fetchone()["id"]

            conn.execute(
                "UPDATE system_config SET value = ? WHERE key = 'messaging_platform'",
                ("discord",),
            )
            conn.execute(
                "UPDATE system_config SET value = ? WHERE key = 'home_channel_platform_id'",
                (home_channel_id,),
            )
            conn.execute(
                "UPDATE system_config SET value = '1' WHERE key = 'current_phase'"
            )
            conn.execute(
                "UPDATE system_config SET value = ? WHERE key = 'ceo_person_id'",
                (str(ceo_pid),),
            )
    finally:
        conn.close()

    secrets_file = tenant_dir / "messaging" / "secrets.env"
    workdir = tenant_dir / "state" / "workdir"
    workdir.mkdir(parents=True, exist_ok=True)
    state_dir = tenant_dir / "state"
    secrets_file.write_text(
        f"DISCORD_CLAUDEX_BOT_TOKEN={bot_token}\n"
        f"DISCORD_COO_GUILD_ID={guild_id}\n"
        f"DISCORD_COO_HOME_CHANNEL_ID={home_channel_id}\n"
        f"DISCORD_COO_CEO_USER_ID={ceo_discord_id}\n"
        f"DISCORD_COO_OWNER_USER_ID={ceo_discord_id}\n"
        f"DISCORD_COO_DM_ALLOWLIST={ceo_discord_id}\n"
        f"DISCORD_COO_DM_ONLY=1\n"
        f"DISCORD_COO_GROUP_FEATURES_ENABLED=0\n"
        f"DISCORD_COO_INBOX_ENABLED=0\n"
        f"DISCORD_COO_WORKDIR={workdir}\n"
        f"DISCORD_COO_STATE_DIR={state_dir}\n"
        f"DISCORD_COO_TMUX_SESSION=coo_{slug}\n"
    )
    secrets_file.chmod(0o600)

    pconn = connect(platform_db_path())
    try:
        with transaction(pconn):
            pconn.execute(
                "INSERT INTO tenants (slug, company_name, messaging_platform, phase, "
                "status, schema_version, tenant_dir) "
                "VALUES (?, ?, 'discord', 1, 'created', 2, ?)",
                (slug, company_name, str(tenant_dir)),
            )
            tid = pconn.execute(
                "SELECT id FROM tenants WHERE slug = ?", (slug,)
            ).fetchone()["id"]
            pconn.execute(
                "INSERT INTO platform_audit (action, tenant_id, payload_json) "
                "VALUES ('tenant_created', ?, ?)",
                (tid, json.dumps({"company": company_name, "platform": "discord"})),
            )
    finally:
        pconn.close()

    click.echo("")
    click.echo(f"Tenant {slug!r} created.")
    click.echo(f"  Dir:     {tenant_dir}")
    click.echo(f"  DB:      {tenant_db}")
    click.echo(f"  Secrets: {secrets_file}")
    click.echo(f"  Phase:   1 (DM-only mapping with CEO)")
    click.echo("")
    click.echo(f"Start the bot with:  coo tenant start {slug}")


@tenant_cmd.command(name="start")
@click.argument("slug")
def start_cmd(slug: str):
    """Launch the tenant's Discord bot as a child process."""
    tenant = _get_tenant(slug)
    tdir = Path(tenant["tenant_dir"])
    secrets_file = tdir / "messaging" / "secrets.env"
    bot_script = repo_root() / "discord_coo_bot.py"
    pid_file = tdir / "state" / "bot.pid"
    log_file = tdir / "state" / "bot.log"

    if not bot_script.exists():
        click.echo(f"Bot script missing: {bot_script}", err=True)
        sys.exit(1)
    if not secrets_file.exists():
        click.echo(f"Secrets file missing: {secrets_file}", err=True)
        sys.exit(1)

    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 0)
            click.echo(f"Already running (pid {pid}).")
            return
        except (OSError, ValueError):
            pid_file.unlink(missing_ok=True)

    env = os.environ.copy()
    for line in secrets_file.read_text().splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()

    proc = subprocess.Popen(
        [sys.executable, str(bot_script)],
        env=env,
        stdout=open(log_file, "a"),
        stderr=subprocess.STDOUT,
        cwd=str(tdir),
        start_new_session=True,
    )
    pid_file.write_text(str(proc.pid))

    pconn = connect(platform_db_path())
    with transaction(pconn):
        pconn.execute("UPDATE tenants SET status = 'running' WHERE slug = ?", (slug,))
    pconn.close()

    click.echo(f"Started {slug} (pid {proc.pid}). Log: {log_file}")


@tenant_cmd.command(name="stop")
@click.argument("slug")
def stop_cmd(slug: str):
    """Stop the tenant's Discord bot."""
    tenant = _get_tenant(slug)
    pid_file = Path(tenant["tenant_dir"]) / "state" / "bot.pid"
    if not pid_file.exists():
        click.echo("Not running.")
        return
    try:
        pid = int(pid_file.read_text().strip())
    except ValueError:
        pid_file.unlink(missing_ok=True)
        click.echo("Stale PID file; cleaned up.")
        return
    try:
        os.kill(pid, signal.SIGTERM)
        click.echo(f"Sent SIGTERM to {pid}.")
    except ProcessLookupError:
        click.echo("Process already gone.")
    pid_file.unlink(missing_ok=True)

    pconn = connect(platform_db_path())
    with transaction(pconn):
        pconn.execute("UPDATE tenants SET status = 'paused' WHERE slug = ?", (slug,))
    pconn.close()
