-- Platform-layer schema. One DB per VM at /var/coo/platform/platform.db.
-- Holds the registry of platform developers, tenants, and tenant-app enablement.
-- Tenant data lives in per-tenant DBs and never touches this file.

PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;

CREATE TABLE schema_version (
    version    INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT (datetime('now'))
) STRICT;

-- Platform operators. Static at the platform layer: only Dan and Adrien.
-- Identified by Discord user ID OR Google email so they work across whatever
-- messaging platform a given tenant chose.
CREATE TABLE developers (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    handle                TEXT NOT NULL UNIQUE,
    display_name          TEXT NOT NULL,
    email                 TEXT NOT NULL UNIQUE,
    discord_user_id       INTEGER UNIQUE,
    google_chat_user_id   TEXT UNIQUE,
    is_active             INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0,1)),
    created_at            TEXT NOT NULL DEFAULT (datetime('now'))
) STRICT;

CREATE TABLE tenants (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    slug                TEXT NOT NULL UNIQUE,
    company_name        TEXT NOT NULL,
    messaging_platform  TEXT NOT NULL CHECK (messaging_platform IN ('discord','google-chat')),
    phase               INTEGER NOT NULL DEFAULT 0 CHECK (phase IN (0,1,2,3)),
    status              TEXT NOT NULL DEFAULT 'created'
                          CHECK (status IN ('created','running','paused','archived')),
    schema_version      INTEGER NOT NULL DEFAULT 0,
    code_version        TEXT,
    tenant_dir          TEXT NOT NULL,
    created_by_developer_id INTEGER REFERENCES developers(id),
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    archived_at         TEXT
) STRICT;

-- Which apps are enabled for which tenant, scoped to which team.
-- The team is identified by slug (tenant-side); the platform doesn't care
-- about the team's internal id, only that the integration must enforce scope.
CREATE TABLE tenant_apps (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id         INTEGER NOT NULL REFERENCES tenants(id),
    integration_slug  TEXT NOT NULL,
    scoped_team_slug  TEXT,
    status            TEXT NOT NULL DEFAULT 'connected'
                        CHECK (status IN ('pending','connected','revoked','error')),
    enabled_by_developer_id INTEGER REFERENCES developers(id),
    enabled_at        TEXT NOT NULL DEFAULT (datetime('now')),
    revoked_at        TEXT,
    config_json       TEXT,
    UNIQUE (tenant_id, integration_slug)
) STRICT;

-- Append-only record of platform-level actions: tenant lifecycle, integration changes,
-- migrations, version bumps. Never edited; rotated at archival time.
CREATE TABLE platform_audit (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_developer_id  INTEGER REFERENCES developers(id),
    action              TEXT NOT NULL,
    tenant_id           INTEGER REFERENCES tenants(id),
    payload_json        TEXT,
    occurred_at         TEXT NOT NULL DEFAULT (datetime('now'))
) STRICT;

CREATE INDEX idx_platform_audit_when    ON platform_audit(occurred_at);
CREATE INDEX idx_platform_audit_tenant  ON platform_audit(tenant_id);
CREATE INDEX idx_tenant_apps_tenant     ON tenant_apps(tenant_id);

INSERT INTO schema_version (version) VALUES (1);
