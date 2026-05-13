# Architecture

Multi-tenant COO agent platform. One installation runs on a VM and hosts one or more isolated *tenants* — each tenant being a company that the agent serves. The system is operated by two platform developers (Dan Core, Adrien Kelly) who handle bootstrap, schema migrations, and integration enablement; each tenant has its own CEO/owner who is the content authority for that tenant.

> **Status**: this document captures the original design. Most of it is now built — the Phase 1 listener, persistence, cadences, integrations framework, cockpit, memory isolation, phase-unlock handshake. See [README.md](README.md) for the actual command surface and [ROADMAP.md](ROADMAP.md) for what was built when.

This document captures the design. The repo also contains a legacy single-tenant Discord bridge (`discord_coo_bot.py` and surrounding files) that predates this design and is preserved for reference (no longer used by the new platform).

## What's shipped vs designed

Implemented end-to-end:

- Two-layer architecture: `coo platform install` (one per VM) + `coo tenant new` (one per company), platform + tenant DBs separated.
- Per-tenant Claude memory isolation via `CLAUDE_CONFIG_DIR` with shared auth (selective symlinks for `.credentials.json`, `settings.json`, `plugins`, `skills`, `statsig`; per-tenant `projects/`).
- Phase 1 Discord listener (`messaging/discord/plugin/coo_phase1.py`) — agent-initiated DMs, allowlist enforcement, inbox routing, bidirectional bridge to Claude via tmux.
- Markers: `[[COO_TO]]`, `[[COO_FIND_MEMBER]]`, `[[COO_NEXT_CONTACT]]`, `[[COO_CLOSE user_id=N]]`, `[[COO_FACT]]`, `[[COO_COMMITMENT]]`, `[[COO_DECISION]]`, `[[COO_WORKFLOW]]`, `[[COO_TASK]]`, `[[COO_REPORT]]`, `[[COO_INBOX_HANDLE]]`, `[[COO_APP_ACTION]]`.
- Operating cadences: `scheduled_contacts` firing + `cadences` (daily-brief / weekly-pulse / monthly-review / quarterly-okr-grade / factsheet-refresh / risk-review) with cron-driven `next_fire_at`.
- Phase-unlock keyword handshake — developer DM containing "approve phase N" bumps `tenants.phase` and sends Claude a `[[BRIDGE_PHASE_UNLOCKED]]` notice.
- App integration framework — `integrations/<slug>/{manifest.json, schema.sql, plugin/}`, per-tenant OAuth state, sync loop in the bot, action whitelist + team-scoped enforcement. Two integrations ship: `echo` (test) and `hubspot` (real).
- Discord cockpit `/coo` slash commands: status, facts, commitments, decisions, cadences, inbox.
- Operator CLI: `coo platform | developer | tenant | integration` covering install, tenant lifecycle, allowlist widening, cadence visibility, manual cadence firing, metrics, risks, OKRs, workflows, tasks, reports, inbox, tenant summary.

Designed but not yet built:

- Google Chat listener plugin (Discord works; Chat schema column exists but no listener).
- SSH-tunnel OAuth callback automation (currently operator pastes the auth code instead).
- Multi-tenant lifecycle commands like `coo tenant archive`, `coo platform upgrade --pin <tenant>`.
- `coo tenant migrate` for schema rollouts beyond the wizard.

## Two-layer architecture

### Platform layer (one per VM)

```
/var/coo/platform/
  platform.db          -- developers, tenants registry, integration enablement, audit
  schemas/             -- versioned tenant DB migrations + integration schemas
  integrations/        -- registry of app connectors (hubspot, goto, gleap, ...)
  bin/                 -- coo CLI: tenant new|destroy|migrate|stop|start
  coo-platform.service -- long-running orchestrator
```

The platform layer is the **operator surface** — only Dan and Adrien can interact with it. It records who the developers are (verified by Discord user ID and email so it works regardless of which messaging platform a tenant uses), what tenants exist, and which integrations are enabled for which tenant.

### Tenant layer (one per company)

```
/var/coo/tenants/<slug>/
  db/coo.db                -- this tenant's data
  transcripts/             -- append-only daily transcript files (source of truth for raw conversation)
  reports/                 -- factsheet markdown mirrors
  google/credentials.json  -- this tenant's Google OAuth tokens (encrypted at rest)
  messaging/secrets        -- Discord bot token or Chat service-account key
  skills/                  -- tenant-specific Claude Code skills
  mcp/                     -- tenant-specific MCP servers
  .claude/                 -- CLAUDE_CONFIG_DIR for the tenant's Claude sessions
  systemd/coo@<slug>.service
```

Tenants are isolated by **process + filesystem + env**, not by `tenant_id` columns. Two tenants on the same VM never share a DB file or a Claude config directory. Each runs as its own Linux user (`coo-<slug>`).

## Bootstrap flow (six stages)

### Stage 0 — Platform install (one-time per VM)

`coo platform install` creates `/var/coo/platform/`, applies `platform/schema/0001_platform.sql`, registers Dan and Adrien as developers (interactive: prompt for Discord user IDs and emails), installs the schema and integration registries, and starts `coo-platform.service`.

### Stage 1 — Tenant onboarding wizard (per company)

Triggered by `coo tenant new`, run by Dan or Adrien from their local PC over SSH. Wizard verifies the runner is a registered platform developer, then:

1. **Slug + company name**: e.g. `acme` / "Acme Inc." Creates `/var/coo/tenants/acme/` and a Linux user `coo-acme`.
2. **Google account connection**:
   - Wizard prints required APIs (Sheets, Docs, Drive, Chat, …) and instructs the operator to create a Google Cloud project + OAuth consent screen via web UI on their PC.
   - Operator runs `ssh -L 8080:localhost:8080 <vm-host>` from PC.
   - Wizard starts an HTTP listener on the VM's `:8080`, prints an OAuth URL listing every required scope.
   - Operator opens URL on PC browser, signs in as the tenant's Google admin, consents.
   - Google redirects to `localhost:8080/callback` → SSH tunnel forwards to VM → wizard captures the token.
   - Token written to `/var/coo/tenants/acme/google/credentials.json`, encrypted at rest.
3. **Messaging platform choice**: Discord or Google Chat.
   - **Discord**: wizard asks for bot token, guild ID, home channel ID. Registers slash commands. Spins up listener.
   - **Google Chat**: uses Google credentials from step 2. Asks for Chat space ID. Spins up listener.
4. **CEO / owner identity**: "Who is the CEO of <Company Name>?" If Discord, ask for Discord user ID; if Chat, ask for email. Records this person in the tenant's `people` table with `is_content_approver = 1`.
5. **Confirmation**: "Ready to start the system for <Company Name>? I will create the tenant database, generate the workspace folders, post a welcome DM to the CEO, and begin Phase 1." Operator types `yes`.
6. **System creation**: applies `tenant/schema/0001_core.sql` and `0002_proactive.sql` to `db/coo.db`, runs `seed.sql`, generates the on-disk workspace, enables `coo@<slug>.service`, sends the welcome DM.

### Stage 2 — Phase 1: top-down mapping (DM-only with CEO)

Agent runs interview-style DMs **only with the CEO** and any additional founders the CEO names (and the operator approves). No team channels, no inbox, no broadcast.

Every claim becomes a row in `facts` with `source_interview_id` + transcript line. Every artifact (org chart, factsheets, priorities) is generated as a `report` row with the markdown body in the DB and a mirrored file on disk under `company-map/`. Agent self-paces with `[[COO_NEXT_CONTACT]]` markers.

**Phase 1 exit gate**: agent decides the map is rich enough, then DMs Dan + Adrien with a proposal — which managers to add to Phase 2, recommended onboarding order, what capabilities to unlock. **Only Dan + Adrien can approve.** The CEO cannot.

### Stage 3 — Phase 2: manager rollout

Allowlist expands. Department channels become watched. Agent interviews each manager. App integrations begin: when a manager mentions HubSpot/GoTo/Gleap/etc., the agent surfaces a proposal to Dan + Adrien. App integration is **scoped to a single team** at connection time — the agent will only use that app's data when consulting for that team and will only take actions in the app on commands from that team's members. Phase 2 deliverable: per-tenant `access-tiers` configuration.

### Stage 4 — Phase 3: staff rollout

Bridge becomes a real workplace agent. Inbox/queue/follow-up machinery turns on. Tier-based access kicks in.

### Stage 5 — Steady state (reactive + proactive)

Two modes run in parallel:

**Reactive**: respond when spoken to, file inbox messages, generate factsheets on request. (What the legacy single-tenant bot already does.)

**Proactive** — the operating cadence loop. Wakes on schedule, runs the rhythm:
- Weekly commitment check-ins ("last week you committed to X — where are we?")
- Monthly KPI review (anomaly detection on metrics)
- Quarterly OKR grading + retrospective
- Continuous risk register and blocker scanning
- Decisions log maintenance

Proactive mode is what makes this a COO instead of a meeting-notes bot.

## Dev gate

Two sides:

- **Tech approval** (always Dan + Adrien, both required) — for any code, schema, env, or structural change. Recorded against `platform.developers`. Tenants cannot bypass.
- **Content approval** (per-tenant, the CEO or designated approver) — for any change to that tenant's company-map content. Recorded against `tenant.people` with `is_content_approver = 1`.

Flow: an agent or operator opens a `doc_proposals` row → tech side approves (both Dan and Adrien) → content side approves (CEO) → applied. Either side can reject; rejection records who and why.

**Dan and Adrien can never unilaterally edit a tenant's content.** Tech ≠ content. This keeps the system honest if it's deployed as a product.

## Per-app team scoping

When an external app is connected (HubSpot, GoTo, Gleap, …), the operator wizard asks **which team uses this app**. The integration is recorded in `platform.tenant_apps` with `scoped_team_slug`. Two enforcement rules:

1. The agent uses that app's data only when consulting for or managing the scoped team.
2. The agent takes actions in that app only on commands from members of the scoped team.

Stops sales pipeline data leaking into engineering channels and stops engineering from accidentally triggering sales workflows.

## Schema flexibility

Three interpretations of "dynamic":

| Level | Allowed? | Mechanism |
|---|---|---|
| Different *content* per tenant | Yes | Each tenant has its own DB; no shared tables |
| Per-tenant ad-hoc fields | Yes | `custom_fields(subject_kind, subject_id, key, value, value_kind)` table |
| Live `CREATE TABLE` / `ALTER TABLE` by the agent | **No** | Schema changes only via versioned migrations approved through the dev gate |

Letting an LLM run live DDL on production data is how DBs get unrecoverably corrupted. Core schema is fixed; integrations bring their own predefined schemas; flexibility happens in `custom_fields`.

## What the agent does NOT do

Hard guardrails:
- Hiring decisions, firing decisions, performance ratings, comp.
- Signing contracts, committing budget, negotiating with vendors.
- PIP / termination conversations.

Agent owns observation, memory, cadence, and surfacing. Humans own judgment on people and money.

## Operator commands (Dan + Adrien)

| Command | Effect |
|---|---|
| `coo platform install` | One-time VM setup |
| `coo tenant new` | Run the bootstrap wizard for a new company |
| `coo tenant list` | All tenants and their status |
| `coo tenant migrate <slug> --to <N>` | Apply a tenant schema migration |
| `coo tenant stop / start <slug>` | Control a tenant's bot service |
| `coo integration enable <slug> <app> --team <team-slug>` | Connect an app post-onboarding |
| `coo platform upgrade` | Roll a new bot version across tenants |

## Repo layout

```
/                         -- this repo
  ARCHITECTURE.md         -- this file
  ROADMAP.md              -- phased rollout + open questions
  AGENTS.md               -- tenant-neutral phase plan
  README.md               -- legacy single-tenant Discord bridge readme
  platform/
    schema/0001_platform.sql
  tenant/
    schema/0001_core.sql
    schema/0002_proactive.sql
    schema/seed.sql
  integrations/           -- per-app schema + plugin registry (HubSpot, GoTo, Gleap, …)
  messaging/              -- Discord and Google Chat listener plugins
  discord_coo_bot.py      -- legacy single-tenant bot (predates this design)
  bootstrap_discord_env.py, channels.json, *.sh, *.service  -- legacy ops files
```
