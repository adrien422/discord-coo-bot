# Roadmap

Status: **design phase**. Schema and architecture are committed; runtime code for the platform layer is not yet written. The legacy single-tenant Discord bridge in this repo is the prior implementation.

## Open questions before implementation

1. **Messaging plugins**: Dan has Discord and Google Chat → Claude Code connectors developed elsewhere. They need to be dropped into `messaging/discord/` and `messaging/google-chat/` so the listener-architecture decisions (long-running process? webhook? MCP? polling?) are concrete before the platform CLI is written.
2. **Integration registry first targets**: HubSpot, GoTo, Gleap. Each needs a directory with `schema.sql`, OAuth config, action whitelist, scope-enforcement code.
3. **Tenant code-version policy**: do all tenants on a VM run the same bot version, or can a tenant be pinned to an older version while others upgrade? Affects the platform CLI's `tenant migrate` and `platform upgrade` commands.

## Build order

### Milestone 1 — Platform skeleton

- `platform/schema/0001_platform.sql` (done)
- `coo platform install` CLI: creates `/var/coo/platform/`, applies schema, prompts for developer registration, starts `coo-platform.service`.
- Platform DB CRUD: developers, tenants.

### Milestone 2 — Tenant bootstrap wizard

- `tenant/schema/0001_core.sql` (done)
- `tenant/schema/0002_proactive.sql` (done)
- `tenant/schema/seed.sql` (done)
- `coo tenant new` wizard:
  - slug + company name → directory + Linux user creation
  - Google OAuth via SSH-tunnelled `localhost:8080`
  - messaging-platform pick → listener spin-up
  - CEO identity capture
  - confirmation → DB creation, welcome DM
- systemd template `coo@.service`
- Per-tenant `CLAUDE_CONFIG_DIR` isolation

### Milestone 3 — Phase 1 mapping (CEO interviews)

- DM-only listener (Discord and Chat)
- Interview session lifecycle: open → message capture → close → summary
- Fact extraction pipeline with provenance (every fact ↔ transcript line)
- Factsheet/org-chart/priorities report generation
- `[[COO_NEXT_CONTACT]]` self-pacing
- Phase 1 exit-gate proposal flow (agent → Dan + Adrien)

### Milestone 4 — Phase 2 manager rollout + first integrations

- Allowlist expansion under operator approval
- Department channel watching
- Manager interview cadence
- HubSpot integration: schema, OAuth, scoped to one team, action whitelist
- Goto and Gleap integrations follow the same pattern

### Milestone 5 — Phase 3 staff rollout

- Tier-based access enforcement
- Inbox/queue/follow-up machinery (similar shape to legacy bot but multi-tenant)
- Per-team factsheet refresh cadence

### Milestone 6 — Proactive cadence layer

- Cadence scheduler (cron-driven, reads `cadences` table)
- Weekly commitment check-ins
- Monthly KPI review with anomaly detection
- Quarterly OKR grading
- Risk register review cycle
- Decisions log surfacing

### Milestone 7 — Operator polish

- `coo tenant list / stop / start / migrate / archive`
- `coo integration enable / disable`
- `coo platform upgrade` with per-tenant pinning
- Audit log surfacing across tenants (read-only for developers)

## What we are deliberately NOT building yet

- Multi-region / multi-VM federation
- Web UI for the operator (CLI is sufficient for two operators)
- A separate "customer admin" UI for tenant CEOs (the messaging platform IS the UI)
- Hire/fire / comp / contract-signing workflows (out of scope by design)
