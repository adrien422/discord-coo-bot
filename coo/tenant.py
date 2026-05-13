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

    # Per-tenant Claude config (CLAUDE_CONFIG_DIR target). We want:
    #   - shared auth so we don't OAuth per tenant: copy .claude.json from $HOME
    #     and symlink .credentials.json from ~/.claude/.
    #   - shared plugins / skills / statsig: symlink.
    #   - per-tenant projects/ (memory): empty dir, lets Claude create
    #     project-scoped memory under this tenant's workdir without bleeding
    #     across tenants.
    tenant_claude = tenant_dir / ".claude"
    tenant_claude.mkdir(mode=0o700, exist_ok=True)
    home = Path.home()
    if (home / ".claude.json").exists():
        import shutil
        shutil.copy2(home / ".claude.json", tenant_claude / ".claude.json")
        (tenant_claude / ".claude.json").chmod(0o600)
    op_claude = home / ".claude"
    for name in (".credentials.json", "settings.json", "plugins", "skills", "statsig"):
        src = op_claude / name
        link = tenant_claude / name
        if src.exists() and not link.exists():
            try:
                link.symlink_to(src)
            except OSError:
                pass
    # Empty projects/ — per-tenant memory, no cross-tenant contamination.
    (tenant_claude / "projects").mkdir(mode=0o700, exist_ok=True)

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
    run_ai = repo_root() / "messaging" / "discord" / "plugin" / "run_ai.sh"
    secrets_file.write_text(
        f"DISCORD_CLAUDEX_BOT_TOKEN={bot_token}\n"
        f"DISCORD_COO_GUILD_ID={guild_id}\n"
        f"DISCORD_COO_HOME_CHANNEL_ID={home_channel_id}\n"
        f"DISCORD_COO_CEO_USER_ID={ceo_discord_id}\n"
        f"DISCORD_COO_TENANT_SLUG={slug}\n"
        f"DISCORD_COO_TENANT_DB={tenant_db}\n"
        f"DISCORD_COO_PLATFORM_DB={platform_db_path()}\n"
        f"DISCORD_COO_WORKDIR={workdir}\n"
        f"DISCORD_COO_STATE_DIR={state_dir}\n"
        f"DISCORD_COO_TMUX_SESSION=coo_{slug}\n"
        f"DISCORD_COO_AGENT_KIND=claude\n"
        f"DISCORD_COO_RUN_AI={run_ai}\n"
        f"CLAUDE_CONFIG_DIR={tenant_dir}/.claude\n"
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
    bot_script = repo_root() / "messaging" / "discord" / "plugin" / "coo_phase1.py"
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


def _check(label: str, ok: bool, detail: str = "") -> bool:
    mark = click.style("OK  ", fg="green") if ok else click.style("FAIL", fg="red")
    line = f"  [{mark}] {label}"
    if detail:
        line += f"  — {detail}"
    click.echo(line)
    return ok


def _parse_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


@tenant_cmd.command(name="doctor")
@click.argument("slug")
def doctor_cmd(slug: str):
    """Preflight checks before `coo tenant start <slug>`."""
    import shutil

    tenant = _get_tenant(slug)
    tdir = Path(tenant["tenant_dir"])
    secrets_file = tdir / "messaging" / "secrets.env"
    bot_script = repo_root() / "messaging" / "discord" / "plugin" / "coo_phase1.py"
    tenant_db = tdir / "db" / "coo.db"

    click.echo(f"Doctor for tenant {slug!r} ({tenant['company_name']})")
    click.echo(f"Directory: {tdir}\n")

    all_ok = True
    all_ok &= _check("tenant directory exists", tdir.is_dir(), str(tdir))
    all_ok &= _check("tenant DB present", tenant_db.is_file(), str(tenant_db))
    all_ok &= _check(
        "secrets.env present + mode 600",
        secrets_file.is_file() and oct(secrets_file.stat().st_mode)[-3:] == "600",
        f"{oct(secrets_file.stat().st_mode)[-3:] if secrets_file.exists() else 'missing'}",
    )
    all_ok &= _check("bot script present", bot_script.is_file(), str(bot_script))

    env_vars: dict[str, str] = {}
    if secrets_file.is_file():
        env_vars = _parse_env(secrets_file)
        required = [
            "DISCORD_CLAUDEX_BOT_TOKEN",
            "DISCORD_COO_GUILD_ID",
            "DISCORD_COO_HOME_CHANNEL_ID",
            "DISCORD_COO_CEO_USER_ID",
            "DISCORD_COO_TENANT_SLUG",
            "DISCORD_COO_TENANT_DB",
            "DISCORD_COO_PLATFORM_DB",
            "DISCORD_COO_RUN_AI",
            "DISCORD_COO_AGENT_KIND",
            "DISCORD_COO_TMUX_SESSION",
            "DISCORD_COO_WORKDIR",
            "DISCORD_COO_STATE_DIR",
        ]
        missing = [k for k in required if not env_vars.get(k)]
        all_ok &= _check(
            "required env vars set",
            not missing,
            "missing: " + ", ".join(missing) if missing else "all present",
        )

    try:
        import aiohttp  # noqa: F401
        all_ok &= _check("python aiohttp importable", True)
    except ImportError as e:
        all_ok &= _check("python aiohttp importable", False, str(e))

    try:
        import discord  # noqa: F401
        all_ok &= _check("python discord.py importable", True, f"v{discord.__version__}")
    except ImportError as e:
        all_ok &= _check("python discord.py importable", False, str(e))

    tmux_path = shutil.which("tmux")
    all_ok &= _check("tmux on PATH", bool(tmux_path), tmux_path or "not found")

    run_ai = env_vars.get("DISCORD_COO_RUN_AI")
    if run_ai:
        rp = Path(run_ai)
        all_ok &= _check(
            "run_ai script exists + executable",
            rp.is_file() and os.access(rp, os.X_OK),
            run_ai,
        )

    agent_kind = env_vars.get("DISCORD_COO_AGENT_KIND", "claude")
    if agent_kind == "claude":
        bin_path = shutil.which("claude")
        all_ok &= _check("claude binary on PATH", bool(bin_path), bin_path or "not found")
    elif agent_kind == "codex":
        bin_path = shutil.which("codex")
        all_ok &= _check("codex binary on PATH", bool(bin_path), bin_path or "not found")

    pid_file = tdir / "state" / "bot.pid"
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 0)
            click.echo(f"  [{click.style('NOTE', fg='yellow')}] bot already running, pid {pid}")
        except (OSError, ValueError):
            click.echo(f"  [{click.style('NOTE', fg='yellow')}] stale pid file at {pid_file}")

    click.echo("")
    if all_ok:
        click.echo(click.style("All checks passed.", fg="green"))
        click.echo(f"Start with:  coo tenant start {slug}")
    else:
        click.echo(click.style("Some checks failed. Fix the items above before starting.", fg="red"))
        sys.exit(1)


@tenant_cmd.command(name="add-person")
@click.argument("slug")
def add_person_cmd(slug: str):
    """Add a person (manager, employee, etc.) to a tenant — widens the allowlist.

    Use this when the agent's Phase 2 unlock proposal has been approved and
    the operator (Dan or Adrien) wants to make a new manager DM-reachable.
    The bot picks up the new person on its next inbound message — no restart
    needed.
    """
    _require_platform_installed()
    tenant = _get_tenant(slug)
    tenant_db = Path(tenant["tenant_dir"]) / "db" / "coo.db"
    if not tenant_db.exists():
        click.echo(f"Tenant DB missing at {tenant_db}", err=True)
        sys.exit(1)

    click.echo(f"--- Adding person to tenant {slug!r} ({tenant['company_name']}) ---")
    display_name = click.prompt("Display name").strip()
    discord_user_id_raw = click.prompt("Discord user ID").strip()
    if not discord_user_id_raw.isdigit():
        click.echo("Discord user ID must be numeric.", err=True)
        sys.exit(1)
    discord_user_id = int(discord_user_id_raw)
    role = click.prompt(
        "Role (e.g. 'head of sales', or blank)", default="", show_default=False
    ).strip()
    team_slug = click.prompt(
        "Team slug (e.g. 'sales'; blank for none)", default="", show_default=False
    ).strip()
    access_tier = click.prompt(
        "Access tier",
        type=click.Choice(["admin", "strategic", "manager", "employee"]),
        default="manager",
    )
    is_content_approver_raw = click.prompt(
        "Also mark as content approver? (y/n)", default="n"
    ).strip().lower()
    is_content_approver = 1 if is_content_approver_raw.startswith("y") else 0

    person_slug = _slug_for_name(display_name)

    conn = connect(tenant_db)
    try:
        existing = conn.execute(
            "SELECT id, slug, display_name FROM people WHERE discord_user_id = ?",
            (discord_user_id,),
        ).fetchone()
        if existing:
            click.echo(
                f"A person with Discord ID {discord_user_id} already exists: "
                f"'{existing['display_name']}' (slug={existing['slug']}). Aborting.",
                err=True,
            )
            sys.exit(1)

        with transaction(conn):
            team_id = None
            if team_slug:
                team_row = conn.execute(
                    "SELECT id FROM teams WHERE slug = ?", (team_slug,)
                ).fetchone()
                if team_row:
                    team_id = team_row["id"]
                else:
                    team_name = team_slug.replace("-", " ").title()
                    cur = conn.execute(
                        "INSERT INTO teams (slug, name) VALUES (?, ?)",
                        (team_slug, team_name),
                    )
                    team_id = cur.lastrowid
                    click.echo(f"  created team '{team_slug}' ({team_name})")

            conn.execute(
                "INSERT INTO people (slug, display_name, discord_user_id, role, "
                "  team_id, access_tier, is_content_approver) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    person_slug, display_name, discord_user_id,
                    role or None, team_id, access_tier, is_content_approver,
                ),
            )
    finally:
        conn.close()

    click.echo("")
    click.echo(f"Added {display_name} (slug={person_slug}, tier={access_tier}).")
    click.echo(
        "The bot reloads its allowlist on every message, so they're reachable "
        "as soon as someone DMs the bot (or the agent's next outbound DM)."
    )


DEFAULT_CADENCES = [
    # (slug, name, kind, cron_expr_utc, scope_kind)
    ("daily-brief",         "Daily CEO brief",        "daily-brief",         "0 9 * * *",         "company"),
    ("weekly-pulse",        "Weekly commitments",     "weekly-pulse",        "0 9 * * MON",       "company"),
    ("monthly-review",      "Monthly KPI review",     "monthly-review",      "0 9 1 * *",         "company"),
    ("quarterly-okr-grade", "Quarterly OKR grading",  "quarterly-okr-grade", "0 9 1 1,4,7,10 *",  "company"),
    ("factsheet-refresh",   "Factsheet refresh",      "factsheet-refresh",   "0 6 * * SUN",       "company"),
    ("risk-review",         "Risk register review",   "risk-review",         "0 10 1 * *",        "company"),
]


@tenant_cmd.command(name="seed-cadences")
@click.argument("slug")
def seed_cadences_cmd(slug: str):
    """Insert default operating cadences into the tenant.

    Cadences are the proactive operating rhythm — daily briefs, weekly
    commitment pulses, monthly KPI reviews, quarterly OKR grading, etc.
    The bot's cadence loop fires due rows and prompts the agent to act.
    Cron expressions are UTC. Already-present cadences (by slug) are skipped.
    """
    _require_platform_installed()
    tenant = _get_tenant(slug)
    tenant_db = Path(tenant["tenant_dir"]) / "db" / "coo.db"
    if not tenant_db.exists():
        click.echo(f"Tenant DB missing at {tenant_db}", err=True)
        sys.exit(1)

    try:
        from croniter import croniter
    except ImportError:
        click.echo(
            "croniter missing. Install: pip install --user --break-system-packages croniter",
            err=True,
        )
        sys.exit(1)

    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)

    inserted = 0
    skipped = 0
    conn = connect(tenant_db)
    try:
        with transaction(conn):
            for slug_, name, kind, cron_expr, scope_kind in DEFAULT_CADENCES:
                existing = conn.execute(
                    "SELECT id FROM cadences WHERE slug = ?", (slug_,)
                ).fetchone()
                if existing:
                    skipped += 1
                    click.echo(f"  = {slug_} (already exists)")
                    continue
                next_at = (
                    croniter(cron_expr, now).get_next(datetime)
                    .strftime("%Y-%m-%d %H:%M:%S")
                )
                conn.execute(
                    "INSERT INTO cadences (slug, name, kind, scope_kind, "
                    "  cron_expr, next_fire_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (slug_, name, kind, scope_kind, cron_expr, next_at),
                )
                inserted += 1
                click.echo(f"  + {slug_} (next fire {next_at} UTC)")
    finally:
        conn.close()

    click.echo("")
    click.echo(f"Cadences seeded: inserted={inserted}, skipped={skipped}.")


@tenant_cmd.command(name="cadences")
@click.argument("slug")
def cadences_cmd(slug: str):
    """Show this tenant's cadences and when each fires next."""
    _require_platform_installed()
    tenant = _get_tenant(slug)
    tenant_db = Path(tenant["tenant_dir"]) / "db" / "coo.db"
    conn = connect(tenant_db)
    rows = conn.execute(
        "SELECT id, slug, kind, scope_kind, cron_expr, "
        "       next_fire_at, last_fired_at, is_active "
        "FROM cadences ORDER BY next_fire_at IS NULL DESC, next_fire_at ASC"
    ).fetchall()
    conn.close()
    if not rows:
        click.echo("No cadences. Run `coo tenant seed-cadences <slug>`.")
        return
    click.echo(
        f"{'id':>3}  {'slug':<22}  {'kind':<20}  {'scope':<8}  "
        f"{'next fire (UTC)':<20}  {'last fire (UTC)':<20}  active"
    )
    for r in rows:
        click.echo(
            f"{r['id']:>3}  {r['slug']:<22}  {r['kind']:<20}  "
            f"{r['scope_kind']:<8}  {(r['next_fire_at'] or '-'):<20}  "
            f"{(r['last_fired_at'] or '-'):<20}  {'yes' if r['is_active'] else 'no'}"
        )


@tenant_cmd.command(name="fire-cadence")
@click.argument("slug")
@click.argument("cadence_slug")
def fire_cadence_cmd(slug: str, cadence_slug: str):
    """Force a cadence to fire on the bot's next poll (within 60s)."""
    _require_platform_installed()
    tenant = _get_tenant(slug)
    tenant_db = Path(tenant["tenant_dir"]) / "db" / "coo.db"
    conn = connect(tenant_db)
    try:
        cur = conn.execute(
            "UPDATE cadences SET next_fire_at = datetime('now', '-1 seconds') "
            "WHERE slug = ?",
            (cadence_slug,),
        )
        conn.commit()
    finally:
        conn.close()
    if cur.rowcount == 0:
        click.echo(f"No cadence with slug {cadence_slug!r}.", err=True)
        sys.exit(1)
    click.echo(
        f"Cadence {cadence_slug!r} set to fire immediately. The bot's "
        "_cadence_loop polls every 60s, so it'll fire within a minute."
    )


@tenant_cmd.command(name="metric-add")
@click.argument("slug")
def metric_add_cmd(slug: str):
    """Define a new KPI / metric to track for the tenant."""
    _require_platform_installed()
    tenant = _get_tenant(slug)
    tenant_db = Path(tenant["tenant_dir"]) / "db" / "coo.db"

    metric_slug = click.prompt("Metric slug (e.g. 'mrr', 'pipeline-coverage')").strip()
    name = click.prompt("Display name").strip()
    description = click.prompt(
        "Short description", default="", show_default=False
    ).strip()
    scope_kind = click.prompt(
        "Scope", type=click.Choice(["company", "team", "person", "workflow"]),
        default="company",
    )
    unit = click.prompt("Unit (e.g. '$', '%', 'count')", default="", show_default=False).strip()
    target_raw = click.prompt(
        "Target value (blank for none)", default="", show_default=False
    ).strip()
    target_value = float(target_raw) if target_raw else None
    target_direction = None
    if target_value is not None:
        target_direction = click.prompt(
            "Target direction", type=click.Choice(["higher", "lower", "equal"]),
            default="higher",
        )

    conn = connect(tenant_db)
    try:
        existing = conn.execute(
            "SELECT id FROM metrics WHERE slug = ?", (metric_slug,)
        ).fetchone()
        if existing:
            click.echo(f"Metric slug {metric_slug!r} already exists.", err=True)
            sys.exit(1)
        with transaction(conn):
            conn.execute(
                "INSERT INTO metrics (slug, name, description, scope_kind, "
                "  unit, target_value, target_direction) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (metric_slug, name, description or None, scope_kind,
                 unit or None, target_value, target_direction),
            )
    finally:
        conn.close()
    click.echo(f"Added metric {metric_slug!r}.")


@tenant_cmd.command(name="metric-record")
@click.argument("slug")
@click.argument("metric_slug")
@click.argument("value", type=float)
@click.option("--note", default="", help="Optional note attached to this data point.")
def metric_record_cmd(slug: str, metric_slug: str, value: float, note: str):
    """Record an observed value for a metric (one data point in the time series)."""
    _require_platform_installed()
    tenant = _get_tenant(slug)
    tenant_db = Path(tenant["tenant_dir"]) / "db" / "coo.db"
    conn = connect(tenant_db)
    try:
        m = conn.execute(
            "SELECT id, target_value, target_direction FROM metrics WHERE slug = ?",
            (metric_slug,),
        ).fetchone()
        if not m:
            click.echo(f"No metric with slug {metric_slug!r}.", err=True)
            sys.exit(1)
        is_anomaly = 0
        if m["target_value"] is not None and m["target_direction"]:
            if m["target_direction"] == "higher" and value < m["target_value"] * 0.7:
                is_anomaly = 1
            elif m["target_direction"] == "lower" and value > m["target_value"] * 1.3:
                is_anomaly = 1
        with transaction(conn):
            conn.execute(
                "INSERT INTO metric_values (metric_id, observed_at, value, note, is_anomaly) "
                "VALUES (?, datetime('now'), ?, ?, ?)",
                (m["id"], value, note or None, is_anomaly),
            )
    finally:
        conn.close()
    click.echo(
        f"Recorded {metric_slug}={value}"
        + (" (ANOMALY)" if is_anomaly else "")
        + (f' — "{note}"' if note else "")
    )


@tenant_cmd.command(name="metrics")
@click.argument("slug")
def metrics_cmd(slug: str):
    """Show all metrics + their most recent observed value."""
    _require_platform_installed()
    tenant = _get_tenant(slug)
    tenant_db = Path(tenant["tenant_dir"]) / "db" / "coo.db"
    conn = connect(tenant_db)
    rows = conn.execute(
        "SELECT m.slug, m.name, m.unit, m.target_value, m.target_direction, "
        "       (SELECT value FROM metric_values WHERE metric_id = m.id "
        "        ORDER BY observed_at DESC LIMIT 1) AS latest, "
        "       (SELECT observed_at FROM metric_values WHERE metric_id = m.id "
        "        ORDER BY observed_at DESC LIMIT 1) AS latest_at, "
        "       (SELECT COUNT(*) FROM metric_values WHERE metric_id = m.id "
        "        AND is_anomaly = 1) AS anomaly_count "
        "FROM metrics m WHERE m.is_active = 1 ORDER BY m.id"
    ).fetchall()
    conn.close()
    if not rows:
        click.echo("No metrics defined yet. Run `coo tenant metric-add <slug>`.")
        return
    for r in rows:
        target = (
            f" (target {r['target_direction']} {r['target_value']})"
            if r["target_value"] is not None else ""
        )
        anomalies = f" [{r['anomaly_count']} anomalies]" if r["anomaly_count"] else ""
        latest = (
            f"{r['latest']}{r['unit'] or ''} @ {r['latest_at']}"
            if r["latest"] is not None else "no data yet"
        )
        click.echo(f"  {r['slug']:<22} {r['name']:<30} → {latest}{target}{anomalies}")


@tenant_cmd.command(name="risk-add")
@click.argument("slug")
def risk_add_cmd(slug: str):
    """Add a risk to the tenant's risk register."""
    _require_platform_installed()
    tenant = _get_tenant(slug)
    tenant_db = Path(tenant["tenant_dir"]) / "db" / "coo.db"

    risk_slug = click.prompt("Risk slug (e.g. 'cash-runway')").strip()
    title = click.prompt("Title").strip()
    description = click.prompt("Description (blank ok)", default="", show_default=False).strip()
    category = click.prompt(
        "Category", default="operational",
        type=click.Choice(["operational", "financial", "compliance",
                          "key-person", "security", "market"]),
    )
    likelihood = click.prompt(
        "Likelihood", default="medium", type=click.Choice(["low", "medium", "high"])
    )
    impact = click.prompt(
        "Impact", default="medium", type=click.Choice(["low", "medium", "high"])
    )
    mitigation = click.prompt("Mitigation plan (blank ok)", default="", show_default=False).strip()
    review_cadence = click.prompt(
        "Review cadence", default="monthly",
        type=click.Choice(["weekly", "monthly", "quarterly"]),
    )

    conn = connect(tenant_db)
    try:
        existing = conn.execute("SELECT id FROM risks WHERE slug = ?", (risk_slug,)).fetchone()
        if existing:
            click.echo(f"Risk slug {risk_slug!r} already exists.", err=True)
            sys.exit(1)
        with transaction(conn):
            conn.execute(
                "INSERT INTO risks (slug, title, description, category, "
                "  likelihood, impact, mitigation, review_cadence, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open')",
                (risk_slug, title, description or None, category,
                 likelihood, impact, mitigation or None, review_cadence),
            )
    finally:
        conn.close()
    click.echo(f"Added risk {risk_slug!r}.")


@tenant_cmd.command(name="risks")
@click.argument("slug")
def risks_cmd(slug: str):
    """List all risks for the tenant."""
    _require_platform_installed()
    tenant = _get_tenant(slug)
    tenant_db = Path(tenant["tenant_dir"]) / "db" / "coo.db"
    conn = connect(tenant_db)
    rows = conn.execute(
        "SELECT slug, title, category, likelihood, impact, status, "
        "       last_reviewed_at, review_cadence "
        "FROM risks ORDER BY status, slug"
    ).fetchall()
    conn.close()
    if not rows:
        click.echo("No risks recorded. Use `coo tenant risk-add <slug>`.")
        return
    click.echo(
        f"{'slug':<22}  {'category':<14}  {'lkhd':<6} {'imp':<6} {'status':<10}  last review"
    )
    for r in rows:
        click.echo(
            f"{r['slug']:<22}  {r['category'] or '-':<14}  "
            f"{r['likelihood'] or '-':<6} {r['impact'] or '-':<6} "
            f"{r['status']:<10}  {r['last_reviewed_at'] or 'never'}"
        )


@tenant_cmd.command(name="risk-update")
@click.argument("slug")
@click.argument("risk_slug")
@click.option("--status", type=click.Choice(["open", "mitigated", "accepted", "closed"]))
@click.option("--reviewed", is_flag=True, help="Stamp last_reviewed_at = now.")
def risk_update_cmd(slug: str, risk_slug: str, status: str | None, reviewed: bool):
    """Update a risk's status or mark it reviewed."""
    _require_platform_installed()
    tenant = _get_tenant(slug)
    tenant_db = Path(tenant["tenant_dir"]) / "db" / "coo.db"
    sets = []
    args: list = []
    if status:
        sets.append("status = ?")
        args.append(status)
    if reviewed:
        sets.append("last_reviewed_at = datetime('now')")
    if not sets:
        click.echo("Pass --status and/or --reviewed.", err=True)
        sys.exit(1)
    args.append(risk_slug)
    conn = connect(tenant_db)
    try:
        with transaction(conn):
            cur = conn.execute(
                f"UPDATE risks SET {', '.join(sets)}, updated_at = datetime('now') "
                f"WHERE slug = ?", args
            )
    finally:
        conn.close()
    if cur.rowcount == 0:
        click.echo(f"No risk with slug {risk_slug!r}.", err=True)
        sys.exit(1)
    click.echo(f"Updated risk {risk_slug!r}.")


@tenant_cmd.command(name="okr-add")
@click.argument("slug")
def okr_add_cmd(slug: str):
    """Add a quarterly objective with key results."""
    _require_platform_installed()
    tenant = _get_tenant(slug)
    tenant_db = Path(tenant["tenant_dir"]) / "db" / "coo.db"

    objective = click.prompt("Objective").strip()
    description = click.prompt("Description (blank ok)", default="", show_default=False).strip()
    scope_kind = click.prompt(
        "Scope", default="company",
        type=click.Choice(["company", "team", "person"]),
    )
    scope_id: int | None = None
    if scope_kind != "company":
        scope_raw = click.prompt(
            "Scope id (team slug, or Discord user id for person)"
        ).strip()
        conn = connect(tenant_db)
        try:
            if scope_kind == "team":
                row = conn.execute("SELECT id FROM teams WHERE slug = ?", (scope_raw,)).fetchone()
            else:
                row = conn.execute(
                    "SELECT id FROM people WHERE discord_user_id = ?",
                    (int(scope_raw),),
                ).fetchone()
        finally:
            conn.close()
        if not row:
            click.echo(f"No {scope_kind} matches {scope_raw!r}.", err=True)
            sys.exit(1)
        scope_id = int(row["id"])
    period = click.prompt("Period (e.g. 'q3-2026')").strip()

    conn = connect(tenant_db)
    try:
        with transaction(conn):
            cur = conn.execute(
                "INSERT INTO okrs (objective, description, scope_kind, scope_id, "
                "  period, status) "
                "VALUES (?, ?, ?, ?, ?, 'active')",
                (objective, description or None, scope_kind, scope_id, period),
            )
            okr_id = cur.lastrowid

            click.echo("Add key results (Ctrl-C when done):")
            n = 0
            while True:
                try:
                    kr_desc = click.prompt("  KR description").strip()
                except click.exceptions.Abort:
                    click.echo("")
                    break
                target_raw = click.prompt(
                    "  KR target value (blank ok)", default="", show_default=False
                ).strip()
                target = float(target_raw) if target_raw else None
                conn.execute(
                    "INSERT INTO key_results (okr_id, description, target_value, status) "
                    "VALUES (?, ?, ?, 'on-track')",
                    (okr_id, kr_desc, target),
                )
                n += 1
                if not click.confirm("  Add another KR?", default=True):
                    break
    finally:
        conn.close()
    click.echo(f"Added OKR (id={okr_id}) with {n} key result(s).")


@tenant_cmd.command(name="okrs")
@click.argument("slug")
def okrs_cmd(slug: str):
    """List OKRs and their key results."""
    _require_platform_installed()
    tenant = _get_tenant(slug)
    tenant_db = Path(tenant["tenant_dir"]) / "db" / "coo.db"
    conn = connect(tenant_db)
    okrs = conn.execute(
        "SELECT id, objective, scope_kind, scope_id, period, status, "
        "       grade_text, grade_value "
        "FROM okrs ORDER BY period DESC, id DESC"
    ).fetchall()
    if not okrs:
        click.echo("No OKRs defined. Use `coo tenant okr-add <slug>`.")
        conn.close()
        return
    for o in okrs:
        grade = (
            f"  graded: {o['grade_value']} — {o['grade_text']}"
            if o["grade_value"] is not None else ""
        )
        click.echo(
            f"\n#{o['id']}  [{o['scope_kind']}] {o['period']}  {o['status']}\n"
            f"  Objective: {o['objective']}{grade}"
        )
        krs = conn.execute(
            "SELECT description, target_value, current_value, status "
            "FROM key_results WHERE okr_id = ? ORDER BY id",
            (o["id"],),
        ).fetchall()
        for k in krs:
            tgt = f" (target {k['target_value']})" if k["target_value"] is not None else ""
            cur = f" → {k['current_value']}" if k["current_value"] is not None else ""
            click.echo(f"    - [{k['status']:<10}] {k['description']}{tgt}{cur}")
    conn.close()


@tenant_cmd.command(name="okr-grade")
@click.argument("slug")
@click.argument("okr_id", type=int)
@click.argument("grade_value", type=float)
@click.option("--text", default="", help="Short narrative of how grading went.")
def okr_grade_cmd(slug: str, okr_id: int, grade_value: float, text: str):
    """Grade an OKR 0.0–1.0 with optional narrative."""
    _require_platform_installed()
    tenant = _get_tenant(slug)
    tenant_db = Path(tenant["tenant_dir"]) / "db" / "coo.db"
    if not 0.0 <= grade_value <= 1.0:
        click.echo("grade_value must be between 0.0 and 1.0.", err=True)
        sys.exit(1)
    conn = connect(tenant_db)
    try:
        with transaction(conn):
            cur = conn.execute(
                "UPDATE okrs SET status = 'graded', grade_value = ?, "
                "  grade_text = ?, graded_at = datetime('now') "
                "WHERE id = ?",
                (grade_value, text or None, okr_id),
            )
    finally:
        conn.close()
    if cur.rowcount == 0:
        click.echo(f"No OKR with id={okr_id}.", err=True)
        sys.exit(1)
    click.echo(f"Graded OKR #{okr_id}: {grade_value} — {text or '(no text)'}")


@tenant_cmd.command(name="summary")
@click.argument("slug")
def summary_cmd(slug: str):
    """Full dashboard for a tenant — phase, people, facts, commitments, etc."""
    _require_platform_installed()
    tenant = _get_tenant(slug)
    tenant_db = Path(tenant["tenant_dir"]) / "db" / "coo.db"

    def section(title: str):
        click.echo("")
        click.echo(click.style(f"━━ {title}", fg="cyan"))

    pconn = connect(platform_db_path())
    t = pconn.execute(
        "SELECT phase, status, schema_version, created_at FROM tenants WHERE slug = ?",
        (slug,),
    ).fetchone()
    pconn.close()
    click.echo(click.style(
        f"\n  Tenant: {slug}  ({tenant['company_name']})", bold=True
    ))
    click.echo(
        f"  Phase: {t['phase']}   Status: {t['status']}   "
        f"Schema: v{t['schema_version']}   Created: {t['created_at']}"
    )

    conn = connect(tenant_db)
    try:
        people = conn.execute(
            "SELECT p.slug, p.display_name, p.role, p.access_tier, t.slug AS team "
            "FROM people p LEFT JOIN teams t ON p.team_id = t.id "
            "WHERE p.deleted_at IS NULL ORDER BY p.id"
        ).fetchall()
        section(f"People ({len(people)})")
        for p in people:
            team = f"  ({p['team']})" if p["team"] else ""
            click.echo(
                f"  {p['display_name']} — {p['role'] or '?'}{team}  [tier={p['access_tier']}]"
            )

        facts = conn.execute(
            "SELECT subject_kind, subject_id, predicate, object_text "
            "FROM facts WHERE is_current = 1 ORDER BY id DESC LIMIT 15"
        ).fetchall()
        section(f"Recent facts ({len(facts)} shown)")
        for f in facts:
            sid = f":{f['subject_id']}" if f["subject_id"] else ""
            click.echo(f"  • {f['subject_kind']}{sid}  {f['predicate']} = {f['object_text']}")

        commits = conn.execute(
            "SELECT c.description, c.due_at, p.display_name "
            "FROM commitments c JOIN people p ON p.id = c.person_id "
            "WHERE c.status = 'open' ORDER BY c.due_at"
        ).fetchall()
        section(f"Open commitments ({len(commits)})")
        for c in commits:
            click.echo(f"  • {c['display_name']}: {c['description']}  (due {c['due_at'] or '—'})")

        decisions = conn.execute(
            "SELECT title, decision_text, decided_at FROM decisions "
            "WHERE is_current = 1 ORDER BY decided_at DESC LIMIT 5"
        ).fetchall()
        section(f"Recent decisions ({len(decisions)} shown)")
        for d in decisions:
            click.echo(f"  • [{d['decided_at']}] {d['title']} — {d['decision_text']}")

        risks = conn.execute(
            "SELECT slug, title, likelihood, impact, status FROM risks "
            "WHERE status IN ('open', 'mitigated') ORDER BY id"
        ).fetchall()
        section(f"Open risks ({len(risks)})")
        for r in risks:
            click.echo(
                f"  • {r['slug']}: {r['title']}  "
                f"[lkhd={r['likelihood']}, imp={r['impact']}, {r['status']}]"
            )

        okrs = conn.execute(
            "SELECT id, objective, period, status FROM okrs "
            "WHERE status IN ('draft', 'active') ORDER BY period DESC LIMIT 5"
        ).fetchall()
        section(f"Active OKRs ({len(okrs)})")
        for o in okrs:
            click.echo(f"  • #{o['id']}  [{o['period']}]  {o['objective']}  ({o['status']})")

        metrics = conn.execute(
            "SELECT m.slug, m.unit, "
            "       (SELECT value FROM metric_values WHERE metric_id = m.id "
            "        ORDER BY observed_at DESC LIMIT 1) AS latest, "
            "       (SELECT observed_at FROM metric_values WHERE metric_id = m.id "
            "        ORDER BY observed_at DESC LIMIT 1) AS at "
            "FROM metrics m WHERE m.is_active = 1 ORDER BY m.id"
        ).fetchall()
        section(f"Metrics ({len(metrics)})")
        for m in metrics:
            if m["latest"] is None:
                click.echo(f"  • {m['slug']}: no data yet")
            else:
                click.echo(f"  • {m['slug']}: {m['latest']}{m['unit'] or ''} @ {m['at']}")

        cadences = conn.execute(
            "SELECT slug, kind, next_fire_at FROM cadences "
            "WHERE is_active = 1 AND next_fire_at IS NOT NULL "
            "ORDER BY next_fire_at LIMIT 5"
        ).fetchall()
        section(f"Next cadences ({len(cadences)} shown)")
        for c in cadences:
            click.echo(f"  • {c['slug']} ({c['kind']}) @ {c['next_fire_at']} UTC")

        pending_nudges = conn.execute(
            "SELECT COUNT(*) AS n FROM scheduled_contacts WHERE status = 'pending'"
        ).fetchone()["n"]
        click.echo("")
        click.echo(f"  Pending nudges: {pending_nudges}")
    finally:
        conn.close()


@tenant_cmd.command(name="workflows")
@click.argument("slug")
def workflows_cmd(slug: str):
    """List workflows recorded for the tenant."""
    _require_platform_installed()
    tenant = _get_tenant(slug)
    tenant_db = Path(tenant["tenant_dir"]) / "db" / "coo.db"
    conn = connect(tenant_db)
    rows = conn.execute(
        "SELECT w.slug, w.name, w.description, w.cadence, "
        "       t.slug AS team_slug, p.display_name AS owner_name "
        "FROM workflows w "
        "LEFT JOIN teams t ON t.id = w.owner_team_id "
        "LEFT JOIN people p ON p.id = w.owner_person_id "
        "WHERE w.deleted_at IS NULL ORDER BY w.slug"
    ).fetchall()
    conn.close()
    if not rows:
        click.echo("No workflows recorded.")
        return
    for r in rows:
        owner = r["team_slug"] or r["owner_name"] or "—"
        cadence = f"  ({r['cadence']})" if r["cadence"] else ""
        click.echo(f"  {r['slug']:<22} owner={owner}{cadence}")
        if r["description"]:
            click.echo(f"    {r['description']}")


@tenant_cmd.command(name="tasks")
@click.argument("slug")
@click.option("--status", help="Filter by status.")
@click.option("--limit", default=25, type=int)
def tasks_cmd(slug: str, status: str | None, limit: int):
    """List tasks for the tenant."""
    _require_platform_installed()
    tenant = _get_tenant(slug)
    tenant_db = Path(tenant["tenant_dir"]) / "db" / "coo.db"
    conn = connect(tenant_db)
    q = (
        "SELECT t.id, t.title, t.status, t.due_at, "
        "       p.display_name AS owner_name, tm.slug AS team_slug "
        "FROM tasks t "
        "LEFT JOIN people p ON p.id = t.owner_person_id "
        "LEFT JOIN teams tm ON tm.id = t.owner_team_id"
    )
    args: list = []
    if status:
        q += " WHERE t.status = ?"
        args.append(status)
    q += " ORDER BY t.due_at IS NULL, t.due_at ASC, t.id DESC LIMIT ?"
    args.append(limit)
    rows = conn.execute(q, args).fetchall()
    conn.close()
    if not rows:
        click.echo("No tasks recorded.")
        return
    for r in rows:
        owner = r["owner_name"] or r["team_slug"] or "—"
        due = f"  due {r['due_at']}" if r["due_at"] else ""
        click.echo(f"  #{r['id']:<4} [{r['status']:<9}] {r['title']}  ({owner}){due}")


@tenant_cmd.command(name="reports")
@click.argument("slug")
@click.option("--kind", help="Filter by report_kind.")
def reports_cmd(slug: str, kind: str | None):
    """List current reports / factsheets recorded for the tenant."""
    _require_platform_installed()
    tenant = _get_tenant(slug)
    tenant_db = Path(tenant["tenant_dir"]) / "db" / "coo.db"
    conn = connect(tenant_db)
    q = (
        "SELECT id, report_kind, subject_kind, subject_id, title, "
        "       file_path, generated_at "
        "FROM reports WHERE is_current = 1"
    )
    args: list = []
    if kind:
        q += " AND report_kind = ?"
        args.append(kind)
    q += " ORDER BY generated_at DESC"
    rows = conn.execute(q, args).fetchall()
    conn.close()
    if not rows:
        click.echo("No current reports.")
        return
    for r in rows:
        subj = f"{r['subject_kind']}:{r['subject_id']}" if r["subject_kind"] else "—"
        click.echo(
            f"  #{r['id']:<4} [{r['report_kind']:<20}] {r['title']}  "
            f"subj={subj}  @ {r['generated_at']}\n"
            f"    {r['file_path']}"
        )


@tenant_cmd.command(name="inbox")
@click.argument("slug")
@click.option("--state", help="Filter by workflow_state (pending|queued|held|attended|...).")
@click.option("--limit", default=20, type=int)
def inbox_cmd(slug: str, state: str | None, limit: int):
    """Show inbox items (DMs from non-allowlist users)."""
    _require_platform_installed()
    tenant = _get_tenant(slug)
    tenant_db = Path(tenant["tenant_dir"]) / "db" / "coo.db"
    conn = connect(tenant_db)
    q = (
        "SELECT i.id, i.received_at, i.workflow_state, "
        "       COALESCE(p.display_name, '(unknown)') AS sender, "
        "       substr(i.content, 1, 100) AS preview "
        "FROM inbox_items i LEFT JOIN people p ON p.id = i.sender_person_id"
    )
    args: list = []
    if state:
        q += " WHERE i.workflow_state = ?"
        args.append(state)
    q += " ORDER BY i.received_at DESC LIMIT ?"
    args.append(limit)
    rows = conn.execute(q, args).fetchall()
    conn.close()
    if not rows:
        click.echo("Inbox is empty" + (f" for state={state}" if state else "") + ".")
        return
    for r in rows:
        click.echo(
            f"  #{r['id']:<4} [{r['workflow_state']:<9}] {r['received_at']}  "
            f"{r['sender']}: {r['preview']}"
        )


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
