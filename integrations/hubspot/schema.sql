-- HubSpot integration tables. Applied to tenant DB when enabled.

CREATE TABLE IF NOT EXISTS hubspot_deals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    hubspot_id      TEXT NOT NULL UNIQUE,
    name            TEXT,
    amount          REAL,
    stage           TEXT,
    close_date      TEXT,
    last_synced_at  TEXT NOT NULL DEFAULT (datetime('now'))
) STRICT;

CREATE INDEX IF NOT EXISTS idx_hubspot_deals_stage ON hubspot_deals(stage);

CREATE TABLE IF NOT EXISTS hubspot_contacts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    hubspot_id      TEXT NOT NULL UNIQUE,
    email           TEXT,
    first_name      TEXT,
    last_name       TEXT,
    last_synced_at  TEXT NOT NULL DEFAULT (datetime('now'))
) STRICT;
