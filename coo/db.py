import sqlite3
from contextlib import contextmanager
from pathlib import Path


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection):
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def applied_versions(conn: sqlite3.Connection) -> set[int]:
    try:
        cur = conn.execute("SELECT version FROM schema_version")
        return {row["version"] for row in cur.fetchall()}
    except sqlite3.OperationalError:
        return set()


def apply_migration(conn: sqlite3.Connection, sql_path: Path) -> None:
    conn.executescript(sql_path.read_text())
