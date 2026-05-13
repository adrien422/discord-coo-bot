import click

from . import __version__
from .developer import developer_cmd
from .install import install_cmd
from .integration import integration_cmd
from .tenant import tenant_cmd


@click.group()
@click.version_option(__version__)
def cli():
    """coo — multi-tenant COO agent platform CLI."""


@cli.group(name="platform")
def platform_grp():
    """Platform-layer operations."""


platform_grp.add_command(install_cmd)
cli.add_command(developer_cmd)
cli.add_command(tenant_cmd)
cli.add_command(integration_cmd)


@platform_grp.command(name="health")
def platform_health_cmd():
    """Scan all tenants — quick health summary, one row per tenant."""
    from .db import connect
    from .paths import platform_db_path
    from .tenant import _systemd_available, _systemctl_user, _systemd_unit_name
    import os
    from pathlib import Path

    if not platform_db_path().exists():
        click.echo("Platform not installed.", err=True)
        return
    sd_available = _systemd_available()
    conn = connect(platform_db_path())
    rows = conn.execute(
        "SELECT slug, company_name, phase, status, tenant_dir FROM tenants "
        "ORDER BY id"
    ).fetchall()
    conn.close()
    if not rows:
        click.echo("No tenants.")
        return
    click.echo(
        f"{'slug':<15}  {'phase':<5}  {'expected':<10}  {'runtime':<20}  health"
    )
    for r in rows:
        slug = r["slug"]
        runtime = "-"
        problem = ""
        if sd_available:
            res = _systemctl_user("is-active", _systemd_unit_name(slug))
            state = res.stdout.strip()
            runtime = f"systemd:{state}"
            if r["status"] == "running" and state != "active":
                problem = " (status=running but systemd " + state + ")"
        else:
            pid_file = Path(r["tenant_dir"]) / "state" / "bot.pid"
            if pid_file.exists():
                try:
                    pid = int(pid_file.read_text().strip())
                    os.kill(pid, 0)
                    runtime = f"pid:{pid}"
                except (OSError, ValueError):
                    runtime = "stale"
                    if r["status"] == "running":
                        problem = " (status=running but pid stale)"
        health = click.style("OK", fg="green") if not problem else click.style(
            f"DEGRADED{problem}", fg="yellow"
        )
        click.echo(
            f"{slug:<15}  {r['phase']:<5}  {r['status']:<10}  "
            f"{runtime:<20}  {health}"
        )


if __name__ == "__main__":
    cli()
