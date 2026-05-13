-- Echo integration tables. Applied to tenant DB when enabled.
CREATE TABLE IF NOT EXISTS echo_pings (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    observed_at   TEXT NOT NULL,
    payload       TEXT NOT NULL
) STRICT;
