# Roadmap

Tracks what was built and what's next. Original roadmap captured the *design*; this version captures the *shipped state*.

## Shipped

Each row is a self-contained commit on the `multi-tenant-redesign` branch (PR #1). The bot has been running live on a test tenant (`dan-test`) for the duration.

| # | Milestone | What |
|---|---|---|
| 1 | Platform skeleton | `coo platform install`, `developer add/list/remove`, `tenant list`. |
| 2 | Tenant bootstrap | `coo tenant new` wizard: dir + schema + secrets.env + listener config. `start/stop/doctor`. |
| — | Phase 1 listener | `coo_phase1.py` using discord.py 2.7. Tmux-bridged Claude Code agent. Allowlist enforcement. Inbox for non-allowlisted DMs. `[[COO_TO]]` / `[[COO_FIND_MEMBER]]` / `[[COO_NEXT_CONTACT]]` / `[[COO_CLOSE]]` markers. Context-first mission prompt. |
| A | Scheduled-contacts firing | Self-pacing actually works — agent's `[[COO_NEXT_CONTACT]]` rows fire on schedule, agent gets `[[BRIDGE_NUDGE]]`. |
| B | Conversation persistence | Interview rows, on-disk transcripts, `[[COO_FACT]]` / `[[COO_COMMITMENT]]` markers writing to DB with provenance. |
| C | add-person + live allowlist reload | Managers can be added with `coo tenant add-person`; bot reloads allowlist per inbound message. |
| D | Operating cadences | `cadences` + `cadence_runs` tables. Bot's `_cadence_loop` polls every 60s. 6 default cadences seeded. |
| E | Cadence visibility | `coo tenant cadences` (list with next fires), `coo tenant fire-cadence` (force now). |
| F | Metrics ingest | `coo tenant metric-add / metric-record / metrics`. Monthly-review cadence embeds a metric snapshot in the agent prompt. |
| G | Phase-unlock keyword | "approve phase N" from a developer DM advances `tenants.phase` + notifies Claude. |
| H | Decisions log | `[[COO_DECISION]]` marker → `decisions` table. Tolerant parser (accepts `title`/`subject`, `text`/`description`). |
| I | Per-tenant Claude memory | `CLAUDE_CONFIG_DIR` per tenant. Auth carries over via copied `~/.claude.json` + symlinked `.credentials.json` / plugins / skills / statsig. Empty `projects/` for tenant-isolated memory. |
| J | Discord cockpit | `/coo {status,facts,commitments,decisions,cadences,inbox}` ephemeral slash commands. |
| K | Integration framework + HubSpot + Echo | `integrations/<slug>/{manifest.json, schema.sql, plugin/}`. `coo integration enable/disable/list/sync-now/logs`. Bot `_integration_loop`. `[[COO_APP_ACTION]]` marker with manifest whitelist + team-scoped enforcement. HubSpot (real, needs OAuth) + Echo (test, no auth). |
| L | Inbox surfacing | `[[COO_INBOX_HANDLE]]` marker. Daily-brief includes pending inbox items. `/coo inbox` slash command. `coo tenant inbox` CLI. |
| M | Risks + OKRs CLI | `coo tenant risk-add/risks/risk-update`, `coo tenant okr-add/okrs/okr-grade`. Populates the tables so risk-review and quarterly-okr-grade cadences have data. |
| N | Tenant summary | `coo tenant summary <slug>` — full dashboard for one tenant. |
| O | Workflows + tasks + reports | `[[COO_WORKFLOW]]` / `[[COO_TASK]]` / `[[COO_REPORT]]` markers. Reports mirror to disk under `tenants/<slug>/reports/<kind>/`. `coo tenant workflows/tasks/reports`. |
| R | Docs refresh | This file + the new top-level README. ARCHITECTURE.md updated with "what's shipped". |

## Open / not yet built

In rough priority order:

- **Live HubSpot connection.** Operator needs to create a HubSpot Developer Portal app and run `coo integration enable dan-test hubspot --team sales`. Real numbers in the monthly-review cadence.
- **SSH-tunnel OAuth callback automation.** Today the operator pastes the auth code; a localhost listener + `ssh -L` pattern would be cleaner (matches the original Google OAuth design).
- **Google Chat listener plugin.** Schema has the column; no plugin code yet.
- **Multi-tenant lifecycle commands**: `coo tenant archive`, `coo platform upgrade --pin <tenant>`, `coo tenant migrate`.
- **Second-tenant isolation proof**: run a real second tenant on the same VM to validate the isolation claims.
- **Tests.** No test suite yet — most validation has been live-test against `dan-test`.
- **Slash-command expansion**: `/coo risks`, `/coo okrs`, `/coo metrics`.
- **Health check + alerting**: e.g. if the bot is stopped for >5 min, send an out-of-band notice.
- **Real Claude-driven Phase 2 unlock proposal**: agent emits a structured Phase 2 proposal that the operator can accept with one CLI command instead of typing "approve phase 2" by hand.

## What we deliberately are NOT building

- Multi-region / multi-VM federation.
- Web UI for the operator (CLI is sufficient for two operators).
- A separate "customer admin" UI for tenant CEOs (the messaging platform IS the UI).
- Hire / fire / comp / contract-signing workflows (out of scope by design — agent owns observation, memory, cadence; humans own judgment on people and money).
