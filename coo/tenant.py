import click

from .db import connect
from .paths import platform_db_path


@click.group(name="tenant")
def tenant_cmd():
    """Manage tenants."""


@tenant_cmd.command(name="list")
def list_cmd():
    conn = connect(platform_db_path())
    rows = conn.execute(
        "SELECT id, slug, company_name, messaging_platform, phase, status, schema_version "
        "FROM tenants ORDER BY id"
    ).fetchall()
    conn.close()
    if not rows:
        click.echo(
            "No tenants registered. Use `coo tenant new` to onboard one "
            "(Milestone 2 — not yet implemented)."
        )
        return
    click.echo(
        f"{'id':>3}  {'slug':<15}  {'company':<25}  {'messaging':<14}  {'phase':<5}  status"
    )
    for r in rows:
        click.echo(
            f"{r['id']:>3}  {r['slug']:<15}  {r['company_name']:<25}  "
            f"{r['messaging_platform']:<14}  {r['phase']:<5}  {r['status']}"
        )
