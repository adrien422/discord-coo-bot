import sys

import click

from .db import apply_migration, connect, transaction
from .developer import prompt_developer
from .paths import platform_db_path, platform_dir, platform_home, platform_schema_dir, tenants_dir


@click.command(name="install")
@click.option("--non-interactive", is_flag=True, help="Skip developer registration prompts.")
def install_cmd(non_interactive: bool):
    """Install the platform layer on this VM."""
    home = platform_home()
    pdir = platform_dir()
    dbp = platform_db_path()
    tdir = tenants_dir()

    if dbp.exists():
        click.echo(f"Platform already installed at {pdir}.", err=True)
        click.echo(f"To reinstall, remove {pdir} and run again.", err=True)
        sys.exit(1)

    click.echo(f"Platform home: {home}")
    pdir.mkdir(parents=True, exist_ok=True)
    tdir.mkdir(parents=True, exist_ok=True)
    pdir.chmod(0o750)
    tdir.chmod(0o750)

    schemas = sorted(platform_schema_dir().glob("*.sql"))
    if not schemas:
        click.echo(f"No platform schemas found at {platform_schema_dir()}.", err=True)
        sys.exit(1)

    conn = connect(dbp)
    try:
        with transaction(conn):
            for sql in schemas:
                click.echo(f"Applying {sql.name}")
                apply_migration(conn, sql)
        click.echo(f"Platform DB created at {dbp}")

        if non_interactive:
            click.echo("")
            click.echo("Non-interactive mode: skipping developer registration.")
            click.echo("Add developers with `coo developer add`.")
            return

        click.echo("")
        click.echo("Register the platform developers — typically Dan and Adrien.")
        click.echo("These are the operators who can run tenant lifecycle commands.")
        click.echo("")
        while True:
            click.echo("Adding developer:")
            try:
                prompt_developer(conn)
            except click.exceptions.Abort:
                click.echo("")
                break
            if not click.confirm("Add another developer?", default=False):
                break

        count = conn.execute(
            "SELECT COUNT(*) AS c FROM developers WHERE is_active = 1"
        ).fetchone()["c"]
        click.echo("")
        click.echo(f"Platform install complete. {count} developer(s) registered.")
        click.echo(f"  Platform DB: {dbp}")
        click.echo(f"  Tenants dir: {tdir}")
    finally:
        conn.close()
