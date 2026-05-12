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
