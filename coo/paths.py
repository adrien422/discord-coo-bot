import os
from pathlib import Path


def platform_home() -> Path:
    if env := os.environ.get("COO_HOME"):
        return Path(env)
    if os.geteuid() == 0:
        return Path("/var/coo")
    return Path.home() / ".local" / "share" / "coo"


def platform_dir() -> Path:
    return platform_home() / "platform"


def platform_db_path() -> Path:
    return platform_dir() / "platform.db"


def tenants_dir() -> Path:
    return platform_home() / "tenants"


def repo_root() -> Path:
    # coo/paths.py -> repo_root/coo/paths.py
    return Path(__file__).resolve().parent.parent


def platform_schema_dir() -> Path:
    return repo_root() / "platform" / "schema"


def tenant_schema_dir() -> Path:
    return repo_root() / "tenant" / "schema"
