# Discord COO Environment

## Phased rollout

| Phase | Status | DM allowlist | Group features | Inbox | Unlock by |
|------|--------|--------------|----------------|-------|-----------|
| 1 — top-down map | **active** | Sean, Na'im, Adrien, Dan (equal access) | OFF | OFF | n/a (default) |
| 2 — manager rollout | locked | + selected managers (tiered access) | partial, as needed | OFF | Adrien + Dan, after agent files an unlock proposal |
| 3 — department staff | locked | + staff under each manager (tiered access) | as needed | as needed | Adrien + Dan |

Phase transition is initiated by the agent: when it judges the map is rich enough it DMs Adrien + Dan with the proposed managers (roles + onboarding order) and the capabilities to unlock; the developers grant the unlock by editing the secrets file and restarting `discord-coo.service`. Sean and Na'im cannot grant phase unlocks.

## Stage 1 — DM-only consultancy mode

- **Group/department features are FLAGGED OFF** (`DISCORD_COO_GROUP_FEATURES_ENABLED=0`). Channel routing, slash/prefix group commands, the cockpit panel, channel factsheets, and proactive channel pulses are all disabled.
- **Reference inbox is FLAGGED OFF** (`DISCORD_COO_INBOX_ENABLED=0`). Ongoing and pending tasks are tracked live in DM conversations, not in the saved inbox.
- **DM-only mode is ON** (`DISCORD_COO_DM_ONLY=1`). The bot listens for DMs from a small allowlist only. All other messages are silently ignored.

### DM allowlist (all equal access)

| Role | Person | Env var |
|------|--------|---------|
| CEO (mapping target) | Sean | `DISCORD_COO_CEO_USER_ID` |
| Owner (mapping target + content authority) | Na'im | `DISCORD_COO_OWNER_USER_ID` |
| Developer (technical approver) | Adrien | included in `DISCORD_COO_DEV_USER_IDS` |
| Developer (technical approver) | Dan | included in `DISCORD_COO_DEV_USER_IDS` |

The full allowlist is `DISCORD_COO_DM_ALLOWLIST` (comma-separated user IDs). Until the IDs are filled in, only the test user that is configured can DM the bot.

### Guild membership

- The bot must remain a member of the existing `Claudex` guild because Discord requires bot+user mutual-server membership for the bot to initiate DMs. The guild's channels are not used in stage 1.

## Mission and workflow

- The agent's mission this stage is the **company map**: structure, departments, members, positions, ongoing tasks/workflows, top priorities. Sourced from interviews with **Sean and Na'im**.
- Company-map artefacts live under `company-map/` (modular):
  - `README.md` — modular layout rules (one folder per department/project).
  - `org-chart.md`, `priorities.md`, `workflows.md` — company-wide content.
  - `factsheet-template.md` — the template Sean asked for; the agent derives it from interviews.
  - `interview-questions.md` — starter cheat sheet so the agent doesn't go in empty-handed.
  - `department-template.md`, `project-template.md` — templates for new sub-folders.
  - `interview-log.jsonl` — append-only log of bot-initiated nudges and self-scheduling decisions.
  - `people/<slug>.md` — per-person factsheets following the template.
  - `departments/<slug>/{department.md, workflows.md, priorities.md}` — one folder per department.
  - `projects/<slug>/project.md` — one folder per project/initiative.
  - `access-tiers.md` — created at phase 2 to record per-role access levels.
- The agent self-paces: after each meaningful exchange it emits `[[COO_NEXT_CONTACT user_id=<discord_user_id> in_seconds=<int> reason=<short>]]`; the bridge stores the marker and at the right moment prompts the live agent session (no new session is spawned).

## DM cockpit (1:1)

Prefix commands available in DM for the allowlist (no slash-command re-registration needed):

- `!coo cockpit` — phase + flag state + map counts + upcoming next-contacts + recent dev-gate proposals.
- `!coo phase` — phase plan.
- `!coo map` — list of company-map files.
- `!coo nextcontacts` — upcoming agent-scheduled DM nudges.
- `!coo proposals` — recent dev-gate doc-change proposals.
- `!coo factsheet <slug>` — read a person factsheet.

## Dev-gate approval (absolute)

Before any file write under `company-map/`, any skill, project, md, or code file:

1. Run `python3 propose_doc_change.py --path … --summary … [--body-file …] [--diff-file …]`.
2. The helper DMs each developer (Adrien + Dan) with the proposal and DMs the owner (Na'im) with the content-review notice.
3. Both devs must reply `approve <id>` (technical green-light); content fitness is not their call.
4. Once both devs approve, Na'im replies `approve <id>` to authorise the content write.
5. Only then may the agent perform the write.

Sean and Na'im cannot bypass the dev technical gate even by insisting. If they push for a code/env change directly, the agent must refuse and route through Adrien and Dan first.

## Toggling features back on

To re-enable the legacy group/inbox surface later, set in the bot env:

- `DISCORD_COO_GROUP_FEATURES_ENABLED=1`
- `DISCORD_COO_INBOX_ENABLED=1`
- `DISCORD_COO_DM_ONLY=0` (if you want the bot to also reach into channels again).

Then restart `discord-coo.service`.
