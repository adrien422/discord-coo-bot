import sqlite3

import click

from .db import connect, transaction
from .paths import platform_db_path


def prompt_developer(conn: sqlite3.Connection) -> None:
    handle = click.prompt("  Handle (e.g. dan-core)").strip()
    display_name = click.prompt("  Display name").strip()
    email = click.prompt("  Email").strip()
    discord_id_raw = click.prompt(
        "  Discord user ID (optional, integer)", default="", show_default=False
    ).strip()
    google_chat_id = click.prompt(
        "  Google Chat user ID (optional)", default="", show_default=False
    ).strip()

    discord_id = int(discord_id_raw) if discord_id_raw else None
    google_chat = google_chat_id or None

    with transaction(conn):
        conn.execute(
            "INSERT INTO developers (handle, display_name, email, discord_user_id, google_chat_user_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (handle, display_name, email, discord_id, google_chat),
        )
    click.echo(f"  Registered {handle}.")


@click.group(name="developer")
def developer_cmd():
    """Manage platform developers."""


@developer_cmd.command(name="list")
def list_cmd():
    conn = connect(platform_db_path())
    rows = conn.execute(
        "SELECT id, handle, display_name, email, discord_user_id, google_chat_user_id, is_active "
        "FROM developers ORDER BY id"
    ).fetchall()
    conn.close()
    if not rows:
        click.echo("No developers registered.")
        return
    for r in rows:
        active = "" if r["is_active"] else "  [INACTIVE]"
        click.echo(
            f"{r['id']:>3}  {r['handle']:<20}  {r['display_name']:<25}  {r['email']}{active}"
        )


@developer_cmd.command(name="add")
def add_cmd():
    conn = connect(platform_db_path())
    try:
        prompt_developer(conn)
    finally:
        conn.close()


@developer_cmd.command(name="remove")
@click.argument("handle")
def remove_cmd(handle: str):
    conn = connect(platform_db_path())
    with transaction(conn):
        cur = conn.execute(
            "UPDATE developers SET is_active = 0 WHERE handle = ?", (handle,)
        )
    conn.close()
    if cur.rowcount == 0:
        click.echo(f"No developer with handle {handle!r}.", err=True)
        raise SystemExit(1)
    click.echo(f"Deactivated {handle}.")
