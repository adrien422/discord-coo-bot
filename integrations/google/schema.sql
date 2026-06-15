-- Google Workspace integration tables. Applied to the tenant DB on connect.
--
-- The integration is idempotent: it creates each Drive artifact (root folder,
-- mirror spreadsheet, sub-folders, per-report Docs, per-transcript files) once,
-- records its Google file id here, and updates the same artifact on every later
-- sync rather than creating duplicates.

CREATE TABLE IF NOT EXISTS google_artifacts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL
                  CHECK (kind IN ('root_folder','folder','spreadsheet','doc','file')),
    -- Logical key, unique within a kind. e.g. 'root', 'reports', 'transcripts',
    -- 'company-map', 'report:123', 'transcript:/abs/path.md'.
    ref_key     TEXT NOT NULL,
    file_id     TEXT NOT NULL,          -- Google Drive / Sheets / Docs id
    web_link    TEXT,                   -- human-openable URL
    -- Fingerprint of the source content last pushed, so unchanged artifacts are
    -- skipped on the next sync (e.g. report.generated_at, transcript mtime).
    source_sig  TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (kind, ref_key)
) STRICT;
