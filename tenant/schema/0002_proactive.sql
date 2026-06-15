-- Proactive COO layer: KPIs, decisions log, OKRs, task dependencies, risk register,
-- commitments, operating cadences. Without this layer, the agent is a reactive
-- meeting-notes bot. With it, the agent runs the operating rhythm of the company.

PRAGMA foreign_keys = ON;

-- KPI definitions. Values are time-series in metric_values.
CREATE TABLE metrics (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    slug              TEXT NOT NULL UNIQUE,
    name              TEXT NOT NULL,
    description       TEXT,
    scope_kind        TEXT NOT NULL
                        CHECK (scope_kind IN ('company','team','person','workflow')),
    scope_id          INTEGER,
    unit              TEXT,
    target_value      REAL,
    target_direction  TEXT
                        CHECK (target_direction IS NULL
                               OR target_direction IN ('higher','lower','equal')),
    source_app        TEXT,
    source_query      TEXT,
    cadence           TEXT,
    owner_person_id   INTEGER REFERENCES people(id),
    is_active         INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0,1)),
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at        TEXT NOT NULL DEFAULT (datetime('now'))
) STRICT;

CREATE INDEX idx_metrics_scope ON metrics(scope_kind, scope_id);

CREATE TABLE metric_values (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    metric_id    INTEGER NOT NULL REFERENCES metrics(id),
    observed_at  TEXT NOT NULL,
    value        REAL NOT NULL,
    note         TEXT,
    is_anomaly   INTEGER NOT NULL DEFAULT 0 CHECK (is_anomaly IN (0,1)),
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
) STRICT;

CREATE INDEX idx_metric_values_metric_time ON metric_values(metric_id, observed_at);

-- Decisions log: what was decided, why, what alternatives were considered.
-- Reversed via reversed_by_decision_id rather than UPDATE-in-place.
CREATE TABLE decisions (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    title                    TEXT NOT NULL,
    decision_text            TEXT NOT NULL,
    rationale                TEXT,
    alternatives_considered  TEXT,
    decided_by_person_id     INTEGER REFERENCES people(id),
    decided_at               TEXT NOT NULL,
    scope_kind               TEXT
                               CHECK (scope_kind IS NULL
                                      OR scope_kind IN ('company','team','person','workflow')),
    scope_id                 INTEGER,
    source_interview_id      INTEGER REFERENCES interviews(id),
    reversed_by_decision_id  INTEGER REFERENCES decisions(id),
    is_current               INTEGER NOT NULL DEFAULT 1 CHECK (is_current IN (0,1)),
    tags_json                TEXT,
    created_at               TEXT NOT NULL DEFAULT (datetime('now'))
) STRICT;

CREATE INDEX idx_decisions_scope ON decisions(scope_kind, scope_id);
CREATE INDEX idx_decisions_when  ON decisions(decided_at);

CREATE TABLE okrs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    objective         TEXT NOT NULL,
    description       TEXT,
    scope_kind        TEXT NOT NULL CHECK (scope_kind IN ('company','team','person')),
    scope_id          INTEGER,
    period            TEXT NOT NULL,
    owner_person_id   INTEGER REFERENCES people(id),
    status            TEXT NOT NULL DEFAULT 'draft'
                        CHECK (status IN ('draft','active','graded','dropped')),
    grade_text        TEXT,
    grade_value       REAL,
    graded_at         TEXT,
    created_at        TEXT NOT NULL DEFAULT (datetime('now'))
) STRICT;

CREATE INDEX idx_okrs_scope_period ON okrs(scope_kind, scope_id, period);

CREATE TABLE key_results (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    okr_id           INTEGER NOT NULL REFERENCES okrs(id),
    description      TEXT NOT NULL,
    metric_id        INTEGER REFERENCES metrics(id),
    target_value     REAL,
    current_value    REAL,
    last_updated_at  TEXT,
    status           TEXT NOT NULL DEFAULT 'on-track'
                       CHECK (status IN ('on-track','at-risk','off-track','done','dropped')),
    created_at       TEXT NOT NULL DEFAULT (datetime('now'))
) STRICT;

CREATE INDEX idx_key_results_okr ON key_results(okr_id);

CREATE TABLE task_dependencies (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id               INTEGER NOT NULL REFERENCES tasks(id),
    blocked_by_task_id    INTEGER REFERENCES tasks(id),
    blocked_by_person_id  INTEGER REFERENCES people(id),
    blocked_by_team_id    INTEGER REFERENCES teams(id),
    blocked_by_external   TEXT,
    note                  TEXT,
    resolved_at           TEXT,
    created_at            TEXT NOT NULL DEFAULT (datetime('now'))
) STRICT;

CREATE INDEX idx_task_dependencies_task ON task_dependencies(task_id);

CREATE TABLE risks (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    slug                TEXT NOT NULL UNIQUE,
    title               TEXT NOT NULL,
    description         TEXT,
    category            TEXT,
    likelihood          TEXT CHECK (likelihood IS NULL OR likelihood IN ('low','medium','high')),
    impact              TEXT CHECK (impact     IS NULL OR impact     IN ('low','medium','high')),
    mitigation          TEXT,
    owner_person_id     INTEGER REFERENCES people(id),
    review_cadence      TEXT,
    last_reviewed_at    TEXT,
    status              TEXT NOT NULL DEFAULT 'open'
                          CHECK (status IN ('open','mitigated','accepted','closed')),
    source_interview_id INTEGER REFERENCES interviews(id),
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
) STRICT;

CREATE INDEX idx_risks_status ON risks(status);

-- Specialised fact: a person committed to deliver something by a date.
-- Closing as 'missed' is what feeds the weekly accountability cadence.
CREATE TABLE commitments (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id               INTEGER NOT NULL REFERENCES people(id),
    description             TEXT NOT NULL,
    due_at                  TEXT,
    related_task_id         INTEGER REFERENCES tasks(id),
    related_okr_id          INTEGER REFERENCES okrs(id),
    source_interview_id     INTEGER REFERENCES interviews(id),
    source_transcript_path  TEXT,
    source_transcript_line  INTEGER,
    status                  TEXT NOT NULL DEFAULT 'open'
                              CHECK (status IN ('open','met','missed','renegotiated','cancelled')),
    closed_at               TEXT,
    closed_note             TEXT,
    created_at              TEXT NOT NULL DEFAULT (datetime('now'))
) STRICT;

CREATE INDEX idx_commitments_person_status ON commitments(person_id, status);
CREATE INDEX idx_commitments_due_open      ON commitments(due_at) WHERE status = 'open';

-- The operating rhythm. cron_expr drives the proactive scheduler; cadence_runs
-- captures each invocation for audit + retry.
CREATE TABLE cadences (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    slug           TEXT NOT NULL UNIQUE,
    name           TEXT NOT NULL,
    kind           TEXT NOT NULL
                     CHECK (kind IN ('weekly-pulse','monthly-review','quarterly-okr-grade',
                                     'daily-brief','commitment-check','risk-review',
                                     'factsheet-refresh','custom')),
    scope_kind     TEXT NOT NULL CHECK (scope_kind IN ('company','team','person')),
    scope_id       INTEGER,
    cron_expr      TEXT NOT NULL,
    next_fire_at   TEXT,
    last_fired_at  TEXT,
    is_active      INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0,1)),
    config_json    TEXT,
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at     TEXT NOT NULL DEFAULT (datetime('now'))
) STRICT;

CREATE INDEX idx_cadences_next ON cadences(next_fire_at) WHERE is_active = 1;

CREATE TABLE cadence_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    cadence_id   INTEGER NOT NULL REFERENCES cadences(id),
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    status       TEXT NOT NULL DEFAULT 'running'
                   CHECK (status IN ('running','succeeded','failed','skipped')),
    summary      TEXT,
    output_json  TEXT,
    error        TEXT
) STRICT;

CREATE INDEX idx_cadence_runs_cadence ON cadence_runs(cadence_id, started_at);

INSERT INTO schema_version (version) VALUES (2);
