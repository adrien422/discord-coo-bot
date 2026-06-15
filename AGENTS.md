# COO agent — phase plan

This document is **tenant-neutral**. Every tenant goes through the same phase plan during onboarding. Concrete identities (CEO, managers, employees) are populated by the bootstrap wizard and ongoing interviews — never hardcoded here.

For the full architecture, see `ARCHITECTURE.md`. For the implementation roadmap, see `ROADMAP.md`.

## Phase 0 — created, not started

Tenant DB and workspace exist; no interviews yet. The wizard transitions to Phase 1 immediately after the operator confirms.

## Phase 1 — top-down map (DM-only)

- DM-only mode. Group / department / inbox features OFF.
- Allowlist starts with **one person**: the CEO/owner registered during onboarding (`is_content_approver = 1`).
- Mission: build the company map (structure, departments, members, roles, tasks, workflows, top priorities) by interviewing the CEO. The CEO can name additional founders to add to the allowlist; the operator (Dan or Adrien) approves the additions.
- Deliverables (each generated as a `report` row, mirrored to disk under `company-map/`):
  - org chart
  - per-person factsheets (one per person mentioned)
  - top priorities
  - workflows for the major processes the CEO describes
  - factsheet template

## Phase 2 — manager rollout (locked)

- Agent decides when the Phase 1 map is rich enough.
- Agent then DMs **the platform developers (Dan and Adrien)** with a proposal: which managers to add, recommended onboarding order, what capabilities to unlock.
- The CEO **cannot** grant the Phase 2 unlock. Only Dan and Adrien can; they enact it via `coo tenant ...` commands and a service restart.
- Phase-2 deliverable: `access-tiers` configuration defining what each role tier can see and do.
- App integrations begin in this phase. When a manager mentions an external app (HubSpot, GoTo, Gleap, …), the agent surfaces a proposal to the operators; on approval the integration is enabled scoped to that manager's team.

## Phase 3 — department staff rollout (locked)

- Gradual rollout to staff under each manager. Same proposal-and-unlock pattern.
- Tier-based access enforced: managers see `coo-admin`-style commands for their team, employees see read-only views.
- Inbox / queue / follow-up machinery turns on.

## Steady state

After Phase 3, the tenant runs continuously. Two modes operate in parallel:

- **Reactive**: respond to DMs, file inbox messages, generate factsheets on request.
- **Proactive** (cadence loop): weekly commitment check-ins, monthly KPI review, quarterly OKR grading, continuous risk and blocker scanning. This is what makes the agent a COO instead of a meeting-notes bot.

## Self-pacing

- Agent emits `[[COO_NEXT_CONTACT user_id=<id> in_seconds=<int> reason=<short>]]`. The bridge strips the marker, records a row in `scheduled_contacts`, and prompts THIS same agent session at the right time. No new sessions are spawned.

## Dev gate (absolute)

- Two sides:
  - **Tech approval**: always Dan and Adrien (both required), recorded against `platform.developers`.
  - **Content approval**: the tenant's CEO or designated approver, recorded against `tenant.people` with `is_content_approver = 1`.
- Any code, schema, env, or content change goes through `doc_proposals`. Tech ≠ content: Dan and Adrien cannot unilaterally edit a tenant's company-map content; the CEO cannot bypass the tech gate.
- Sean and Na'im are NOT hardcoded anywhere — they were specific people in an early single-tenant version of this bot. Each tenant has its own CEO discovered at onboarding.

## Secrets

- Per-tenant secrets at `/var/coo/tenants/<slug>/messaging/secrets` and `.../google/credentials.json`, mode `0600`. Never in repo, never in transcripts, never in DB rows.
