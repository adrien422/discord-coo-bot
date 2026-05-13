-- Core tenant schema. One DB per tenant at /var/coo/tenants/<slug>/db/coo.db.
-- Universal across all companies. Per-company flexibility happens in custom_fields,
-- not via runtime DDL.

PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;

CREATE TABLE schema_version (
    version    INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT (datetime('now'))
) STRICT;

CREATE TABLE system_config (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    notes      TEXT
) STRICT;

-- Teams must exist before people can reference them.
CREATE TABLE teams (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    slug            TEXT NOT NULL UNIQUE,
    name            TEXT NOT NULL,
    parent_team_id  INTEGER REFERENCES teams(id),
    lead_person_id  INTEGER,
    description     TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    deleted_at      TEXT
) STRICT;

-- Humans in this tenant. Distinct from platform.developers (Dan/Adrien).
-- The CEO/owner is the person with is_content_approver = 1.
CREATE TABLE people (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    slug                        TEXT NOT NULL UNIQUE,
    display_name                TEXT NOT NULL,
    discord_user_id             INTEGER UNIQUE,
    discord_username            TEXT,
    google_chat_user_id         TEXT UNIQUE,
    email                       TEXT,
    role                        TEXT,
    team_id                     INTEGER REFERENCES teams(id),
    access_tier                 TEXT NOT NULL DEFAULT 'employee'
                                  CHECK (access_tier IN ('admin','strategic','manager','employee')),
    is_content_approver         INTEGER NOT NULL DEFAULT 0 CHECK (is_content_approver IN (0,1)),
    is_phase1_interview_target  INTEGER NOT NULL DEFAULT 0 CHECK (is_phase1_interview_target IN (0,1)),
    notes                       TEXT,
    created_at                  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at                  TEXT NOT NULL DEFAULT (datetime('now')),
    deleted_at                  TEXT
) STRICT;

CREATE INDEX idx_people_team ON people(team_id);

-- Discord channel ID or Chat space ID stored as TEXT — Chat space IDs are not numeric.
CREATE TABLE channels (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    platform_channel_id  TEXT NOT NULL UNIQUE,
    name                 TEXT NOT NULL,
    kind                 TEXT NOT NULL
                            CHECK (kind IN ('admin','config','audit','home','department','dm','general')),
    department_team_id   INTEGER REFERENCES teams(id),
    is_admin_room        INTEGER NOT NULL DEFAULT 0 CHECK (is_admin_room IN (0,1)),
    is_watched           INTEGER NOT NULL DEFAULT 1 CHECK (is_watched IN (0,1)),
    created_at           TEXT NOT NULL DEFAULT (datetime('now')),
    deleted_at           TEXT
) STRICT;

CREATE TABLE interviews (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id       INTEGER NOT NULL REFERENCES people(id),
    channel_id      INTEGER REFERENCES channels(id),
    started_at      TEXT NOT NULL,
    ended_at        TEXT,
    status          TEXT NOT NULL DEFAULT 'open'
                      CHECK (status IN ('open','closed','scheduled')),
    purpose         TEXT,
    summary         TEXT,
    transcript_path TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
) STRICT;

CREATE INDEX idx_interviews_person ON interviews(person_id);
CREATE INDEX idx_interviews_status ON interviews(status);

-- Atomic claims with provenance. Heart of the company-map knowledge representation.
-- Corrections form a chain via superseded_by_fact_id rather than UPDATE-in-place.
CREATE TABLE facts (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_kind                TEXT NOT NULL
                                  CHECK (subject_kind IN ('person','team','workflow','company')),
    subject_id                  INTEGER,
    predicate                   TEXT NOT NULL,
    object_text                 TEXT,
    object_kind                 TEXT
                                  CHECK (object_kind IS NULL
                                         OR object_kind IN ('person','team','task','workflow')),
    object_id                   INTEGER,
    confidence                  REAL NOT NULL DEFAULT 1.0,
    source_interview_id         INTEGER REFERENCES interviews(id),
    source_message_platform_id  TEXT,
    source_transcript_path      TEXT,
    source_transcript_line      INTEGER,
    asserted_by_person_id       INTEGER REFERENCES people(id),
    asserted_at                 TEXT NOT NULL,
    superseded_by_fact_id       INTEGER REFERENCES facts(id),
    is_current                  INTEGER NOT NULL DEFAULT 1 CHECK (is_current IN (0,1)),
    created_at                  TEXT NOT NULL DEFAULT (datetime('now'))
) STRICT;

CREATE INDEX idx_facts_subject  ON facts(subject_kind, subject_id);
CREATE INDEX idx_facts_current  ON facts(is_current) WHERE is_current = 1;

CREATE TABLE workflows (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    slug                TEXT NOT NULL UNIQUE,
    name                TEXT NOT NULL,
    description         TEXT,
    owner_team_id       INTEGER REFERENCES teams(id),
    owner_person_id     INTEGER REFERENCES people(id),
    steps_json          TEXT,
    inputs              TEXT,
    outputs             TEXT,
    cadence             TEXT,
    source_interview_id INTEGER REFERENCES interviews(id),
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now')),
    deleted_at          TEXT
) STRICT;

CREATE TABLE tasks (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    title               TEXT NOT NULL,
    description         TEXT,
    owner_person_id     INTEGER REFERENCES people(id),
    owner_team_id       INTEGER REFERENCES teams(id),
    status              TEXT NOT NULL DEFAULT 'pending'
                          CHECK (status IN ('pending','active','blocked','done','dropped')),
    priority            INTEGER,
    due_at              TEXT,
    workflow_id         INTEGER REFERENCES workflows(id),
    parent_task_id      INTEGER REFERENCES tasks(id),
    source_interview_id INTEGER REFERENCES interviews(id),
    notes               TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now')),
    closed_at           TEXT
) STRICT;

CREATE INDEX idx_tasks_owner_person ON tasks(owner_person_id);
CREATE INDEX idx_tasks_status        ON tasks(status);

CREATE TABLE priorities (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    scope_kind          TEXT NOT NULL CHECK (scope_kind IN ('company','team','person')),
    scope_id            INTEGER,
    title               TEXT NOT NULL,
    description         TEXT,
    rank                INTEGER NOT NULL,
    period              TEXT NOT NULL,
    source_interview_id INTEGER REFERENCES interviews(id),
    is_current          INTEGER NOT NULL DEFAULT 1 CHECK (is_current IN (0,1)),
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    superseded_at       TEXT
) STRICT;

CREATE INDEX idx_priorities_scope ON priorities(scope_kind, scope_id);

-- Generated artifacts: per-person factsheets, per-team factsheets, weekly/monthly
-- summaries. content_md is the canonical body; file_path mirrors it on disk.
CREATE TABLE reports (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    report_kind                 TEXT NOT NULL
                                  CHECK (report_kind IN ('factsheet-person','factsheet-team',
                                                         'weekly','monthly','org-chart','priorities')),
    subject_kind                TEXT
                                  CHECK (subject_kind IS NULL
                                         OR subject_kind IN ('person','team','company','channel')),
    subject_id                  INTEGER,
    title                       TEXT,
    content_md                  TEXT NOT NULL,
    period_start                TEXT,
    period_end                  TEXT,
    file_path                   TEXT,
    generated_at                TEXT NOT NULL DEFAULT (datetime('now')),
    generated_by_interview_id   INTEGER REFERENCES interviews(id),
    superseded_by_report_id     INTEGER REFERENCES reports(id),
    is_current                  INTEGER NOT NULL DEFAULT 1 CHECK (is_current IN (0,1))
) STRICT;

CREATE INDEX idx_reports_subject       ON reports(subject_kind, subject_id);
CREATE INDEX idx_reports_kind_current  ON reports(report_kind, is_current);

CREATE TABLE followups (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id                   INTEGER REFERENCES people(id),
    channel_id                  INTEGER REFERENCES channels(id),
    anchor_message_platform_id  TEXT,
    topic                       TEXT NOT NULL,
    status                      TEXT NOT NULL DEFAULT 'open'
                                  CHECK (status IN ('open','closed','expired')),
    targeted                    INTEGER NOT NULL DEFAULT 1 CHECK (targeted IN (0,1)),
    tags_json                   TEXT,
    opened_at                   TEXT NOT NULL DEFAULT (datetime('now')),
    closed_at                   TEXT
) STRICT;

CREATE INDEX idx_followups_status ON followups(status);

-- Self-paced contact schedule from [[COO_NEXT_CONTACT]] markers.
CREATE TABLE scheduled_contacts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id     INTEGER NOT NULL REFERENCES people(id),
    fire_at       TEXT NOT NULL,
    reason        TEXT,
    interview_id  INTEGER REFERENCES interviews(id),
    status        TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','fired','cancelled')),
    fired_at      TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
) STRICT;

CREATE INDEX idx_scheduled_contacts_pending
    ON scheduled_contacts(status, fire_at) WHERE status = 'pending';

-- Daily transcripts live on disk; one row per file.
CREATE TABLE transcripts (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id        INTEGER NOT NULL REFERENCES channels(id),
    date              TEXT NOT NULL,
    file_path         TEXT NOT NULL UNIQUE,
    line_count        INTEGER NOT NULL DEFAULT 0,
    last_appended_at  TEXT,
    created_at        TEXT NOT NULL DEFAULT (datetime('now'))
) STRICT;

CREATE INDEX idx_transcripts_channel_date ON transcripts(channel_id, date);

-- Dev gate: the proposal flow for any code/structure/content change.
-- Tech approvers reference platform.developers (Dan + Adrien) — stored as IDs only;
-- the application joins across the two DBs. Content approver is a tenant-side person.
CREATE TABLE doc_proposals (
    id                              INTEGER PRIMARY KEY AUTOINCREMENT,
    proposed_by_person_id           INTEGER REFERENCES people(id),
    proposed_by_agent               INTEGER NOT NULL DEFAULT 0 CHECK (proposed_by_agent IN (0,1)),
    target_path                     TEXT NOT NULL,
    change_kind                     TEXT NOT NULL CHECK (change_kind IN ('create','modify','delete')),
    diff_text                       TEXT NOT NULL,
    summary                         TEXT,
    status                          TEXT NOT NULL DEFAULT 'pending'
                                      CHECK (status IN ('pending','tech-approved','content-approved',
                                                        'applied','rejected','cancelled')),
    tech_approver_1_developer_id    INTEGER,
    tech_approver_1_at              TEXT,
    tech_approver_2_developer_id    INTEGER,
    tech_approver_2_at              TEXT,
    content_approver_person_id      INTEGER REFERENCES people(id),
    content_approver_at             TEXT,
    rejected_by_party               TEXT
                                      CHECK (rejected_by_party IS NULL
                                             OR rejected_by_party IN ('tech','content')),
    rejected_reason                 TEXT,
    applied_at                      TEXT,
    applied_commit_sha              TEXT,
    created_at                      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at                      TEXT NOT NULL DEFAULT (datetime('now'))
) STRICT;

CREATE INDEX idx_doc_proposals_status ON doc_proposals(status);

-- Saved employee reference messages (Phase 2/3 feature; included now to avoid migration later).
CREATE TABLE inbox_items (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    platform_message_id   TEXT UNIQUE,
    channel_id            INTEGER NOT NULL REFERENCES channels(id),
    sender_person_id      INTEGER REFERENCES people(id),
    content               TEXT NOT NULL,
    received_at           TEXT NOT NULL,
    workflow_state        TEXT NOT NULL DEFAULT 'pending'
                            CHECK (workflow_state IN ('pending','queued','held','no-action',
                                                      'initiated','failed','attended')),
    tags_json             TEXT,
    saved_path            TEXT,
    attended_at           TEXT,
    created_at            TEXT NOT NULL DEFAULT (datetime('now'))
) STRICT;

CREATE INDEX idx_inbox_state ON inbox_items(workflow_state);

-- Per-tenant flexibility without runtime DDL. Sparse key/value attached to any subject.
CREATE TABLE custom_fields (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_kind  TEXT NOT NULL
                    CHECK (subject_kind IN ('person','team','workflow','task','company')),
    subject_id    INTEGER,
    field_key     TEXT NOT NULL,
    field_value   TEXT,
    value_kind    TEXT NOT NULL DEFAULT 'text'
                    CHECK (value_kind IN ('text','number','date','json','bool')),
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (subject_kind, subject_id, field_key)
) STRICT;

CREATE INDEX idx_custom_fields_subject ON custom_fields(subject_kind, subject_id);

-- Append-only DB-state changes (separate from transcripts, which capture conversation).
CREATE TABLE audit_log (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_kind          TEXT NOT NULL CHECK (actor_kind IN ('agent','human','system','platform')),
    actor_person_id     INTEGER REFERENCES people(id),
    actor_developer_id  INTEGER,
    action              TEXT NOT NULL,
    target_kind         TEXT,
    target_id           INTEGER,
    payload_json        TEXT,
    occurred_at         TEXT NOT NULL DEFAULT (datetime('now'))
) STRICT;

CREATE INDEX idx_audit_log_target    ON audit_log(target_kind, target_id);
CREATE INDEX idx_audit_log_occurred  ON audit_log(occurred_at);

INSERT INTO schema_version (version) VALUES (1);
