# Discord COO Workspace

- Persistent working directory for the Claudex Discord COO agent.
- Three-phase rollout. Currently in **PHASE 1 — top-down company mapping**.

## Phase plan

### Phase 1 (active) — top-down map
- DM-only mode. Group/department/inbox features flagged OFF.
- DM allowlist (all equal-access in this phase):
  - Sean — CEO — primary mapping interview target.
  - Na'im — owner — primary mapping interview target + final content authority.
  - Adrien — developer — technical approver only.
  - Dan — developer — technical approver only.
- Mission: build the company map (structure, departments, members, roles, tasks, workflows, top priorities) by interviewing Sean and Na'im.
- Second deliverable: the factsheet template Sean asked for (`company-map/factsheet-template.md`); per-person factsheets under `company-map/people/<slug>.md` follow the template.

### Phase 2 (locked) — manager rollout
- Agent decides when phase-1 map is rich enough.
- Agent then DMs Adrien + Dan asking for the unlock, including: which managers to add (roles + reasoning), prioritised onboarding order (whose workflows/tasks come first), and what capabilities to unlock (expand DM allowlist, possibly re-enable specific group features).
- Sean and Na'im cannot grant the unlock. Only Adrien + Dan can; they enact it via env-var changes + service restart.
- Phase-2 deliverable: `company-map/access-tiers.md` defining the per-role access tiers (equal access ends here).

### Phase 3 (locked) — department staff rollout
- Gradual rollout to staff under each manager, same unlock pattern.
- Tiered access continues to be delegated by role.

## Workspace layout (modular)

- `company-map/`
  - `README.md` — modular layout rules.
  - `org-chart.md`, `priorities.md`, `workflows.md` — company-wide content only.
  - `factsheet-template.md`, `interview-questions.md`, `department-template.md`, `project-template.md` — starter templates.
  - `interview-log.jsonl` — append-only record of self-scheduling decisions.
  - `people/<slug>.md` — per-person factsheets.
  - `departments/<slug>/` — one folder per department (`department.md`, `workflows.md`, `priorities.md`).
  - `projects/<slug>/` — one folder per project/initiative.
  - `access-tiers.md` — created in phase 2.
- `reference/transcripts/` — daily Discord transcripts (still on).
- `reference/inbox/` — inbox flagged off; not used in stage 1.

## Self-pacing

- Agent emits `[[COO_NEXT_CONTACT user_id=<id> in_seconds=<int> reason=<short>]]`. The bridge strips the marker, records the schedule, and prompts THIS same agent session at the right time. No new sessions are spawned.

## Dev gate (absolute)

- No file write or env/code change without going through `propose_doc_change.py`: Adrien + Dan must both technical-approve, then Na'im content-approves.
- Sean and Na'im cannot bypass the dev technical gate.

## Secrets

- Do not store plaintext secrets here; use `/home/arman/workbench/.discord_claudex.secrets` (mode `0600`).
