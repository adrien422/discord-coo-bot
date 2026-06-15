# COO Agent Platform

A multi-tenant operations agent for small companies. One installation on a VM hosts one or more isolated tenants — each tenant is a company with its own database, Discord (or Google Chat, later) integration, scoped app connections (HubSpot etc.), and a persistent Claude Code agent acting as a fractional COO.

The agent does what a real COO does: maps the company through structured interviews with the CEO and managers, tracks commitments and decisions with full provenance, runs an operating cadence (daily / weekly / monthly / quarterly), and surfaces problems early. It never makes hiring, firing, comp, or contract decisions — those stay with humans.

Status: alpha. End-to-end working on a single test tenant; not production-deployed.

For the design background, see [ARCHITECTURE.md](ARCHITECTURE.md). For the build sequence, see [ROADMAP.md](ROADMAP.md).

## At a glance

- **Two layers**: a platform layer (one per VM, owned by Dan + Adrien) and a tenant layer (one per company).
- **Discord-only for messaging today**. Google Chat is wired into the schema but the listener plugin is not built yet.
- **Phase 1 → 2 → 3 rollout**: CEO interviews first (Phase 1), then per-team manager interviews (Phase 2), then staff (Phase 3). Phases advance through a keyword handshake — only platform developers can unlock.
- **Markers + cadences + cockpit**. The agent emits `[[COO_*]]` markers to persist facts, commitments, decisions, workflows, tasks, factsheets, follow-up nudges. Cron-driven cadences fire on schedule. `/coo` Discord slash commands let any allowlisted user inspect state.

## Quick start

Prereqs (Debian / Ubuntu shown):

```
sudo apt install -y git python3-pip python3-aiohttp tmux sqlite3
pip install --user --break-system-packages discord.py croniter
```

Plus [Claude Code](https://claude.com/cai/download/cli) installed at `~/.local/bin/claude` and logged in once on the operator's account.

Clone the repo:

```
git clone https://github.com/adrien422/discord-coo-bot.git
cd discord-coo-bot
```

Install the platform layer (one-time, on this VM):

```
python3 -m coo.cli platform install
# Register yourself (and Adrien) as platform developers when prompted.
```

Bootstrap a tenant:

```
python3 -m coo.cli tenant new
# Prompts: slug, company name, Discord bot token, guild id, home channel id,
# CEO display name, CEO Discord user id, CEO email.
```

Preflight + start:

```
python3 -m coo.cli tenant doctor <slug>
python3 -m coo.cli tenant start <slug>
```

The bot connects to Discord, spawns a tmux session running Claude Code per-tenant (with isolated `CLAUDE_CONFIG_DIR`), sends the initial mission to the agent, and the agent composes its first DM to the CEO.

Seed the operating cadences (optional but recommended):

```
python3 -m coo.cli tenant seed-cadences <slug>
```

## Command reference

### Platform layer

```
coo platform install [--non-interactive]
coo developer add | list | remove <handle>
```

### Tenant lifecycle

```
coo tenant new                   # interactive bootstrap wizard
coo tenant list                  # all tenants, with live PID column
coo tenant doctor <slug>         # preflight checks before start
coo tenant start <slug>          # launch the listener
coo tenant stop <slug>           # SIGTERM the listener (tmux survives)
coo tenant summary <slug>        # dashboard: phase, facts, commitments,
                                 #   decisions, risks, OKRs, cadences, etc.
```

### Tenant data

```
coo tenant add-person <slug>          # widen the allowlist (managers / staff)
coo tenant cadences <slug>            # list cadences with next fire times
coo tenant fire-cadence <slug> <c>    # force a cadence to fire on next poll
coo tenant metric-add <slug>          # define a KPI
coo tenant metric-record <slug> <m> <v>
coo tenant metrics <slug>             # list with latest values + anomalies
coo tenant risk-add <slug>            # add to risk register
coo tenant risks <slug>
coo tenant risk-update <slug> <r> [--status X] [--reviewed]
coo tenant okr-add <slug>             # quarterly objective + key results
coo tenant okrs <slug>
coo tenant okr-grade <slug> <id> <grade> [--text N]
coo tenant workflows <slug>
coo tenant tasks <slug> [--status X] [--limit N]
coo tenant reports <slug> [--kind X]
coo tenant inbox <slug> [--state X] [--limit N]
coo tenant seed-cadences <slug>       # install the 6 default cadences
```

### Integrations (HubSpot, Echo, …)

```
coo integration list [<tenant>]
coo integration enable <tenant> <slug> --team <team>
coo integration disable <tenant> <slug>
coo integration sync-now <tenant> <slug>
coo integration logs <tenant> [<slug>]
```

### Discord cockpit (used by allowlisted users in Discord)

```
/coo status       # phase, counters, next cadence
/coo facts [subject]
/coo commitments
/coo decisions
/coo cadences
/coo inbox
```

All cockpit replies are ephemeral (only the invoker sees them).

## How the agent works

The Phase 1 listener (`messaging/discord/plugin/coo_phase1.py`) is one long-running Python process per tenant. It bridges Discord to a tmux pane running Claude Code, with these loops:

- **Capture**: reads Claude's pane every 2s, dispatches new responses, parses `[[COO_*]]` markers (FACT, COMMITMENT, DECISION, WORKFLOW, TASK, REPORT, NEXT_CONTACT, FIND_MEMBER, CLOSE, INBOX_HANDLE, APP_ACTION).
- **Schedule**: every 60s, fires `scheduled_contacts` rows whose `fire_at <= now` — nudging the agent to follow up.
- **Cadence**: every 60s, runs due cadences (`daily-brief`, `weekly-pulse`, `monthly-review`, `quarterly-okr-grade`, `factsheet-refresh`, `risk-review`) and feeds the agent a DB snapshot relevant to that kind.
- **Integration**: every 60s, runs `plugin.sync()` for each connected integration whose cadence has elapsed (e.g. HubSpot every 15 min).

The agent talks to the CEO/managers via DMs forwarded both ways. Non-allowlisted DMs land in `inbox_items` and surface during the daily brief.

## Adding a new app integration

Three steps:

1. **Build the plugin** in `integrations/<slug>/`:
   - `manifest.json` — declares OAuth scopes, sync cadence, action whitelist.
   - `schema.sql` — tables this integration adds to the tenant DB.
   - `plugin/__init__.py` — implements `oauth_url`, `exchange_code`, `refresh`, `sync(tenant_db, team_slug, creds)`, plus any named actions referenced in the manifest.
2. **Enable for a tenant**: `coo integration enable <tenant> <slug> --team <team>`. The wizard walks the operator through the OAuth handshake (currently paste-the-code form; SSH-tunnel callback automation is planned).
3. **Sync runs automatically** on the manifest's cadence. The agent can also emit `[[COO_APP_ACTION slug=X action=Y args='{...}']]` to take actions, gated on the asserter being a member of the integration's scoped team (or the CEO, or a developer).

Two integrations ship today:
- `echo` — fake test integration, validates the framework without external auth.
- `hubspot` — real plugin (deals + contacts pull, pipeline metrics, create_note + update_deal_stage). Requires operator to create a HubSpot Developer Portal app to actually enable.

## Repository layout

```
coo/                              Platform CLI (Python package)
  cli.py                          entry point: coo {platform,developer,tenant,integration}
  install.py / developer.py
  tenant.py                       tenant lifecycle + data CLI
  integration.py                  app integration CLI
  db.py / paths.py
platform/schema/                  Platform DB schema (one per VM)
  0001_platform.sql               developers, tenants, tenant_apps, platform_audit
tenant/schema/                    Per-tenant DB schema (applied at bootstrap)
  0001_core.sql                   16 core tables: people, teams, channels, interviews,
                                  facts, workflows, tasks, priorities, reports,
                                  followups, scheduled_contacts, transcripts,
                                  doc_proposals, inbox_items, custom_fields, audit_log
  0002_proactive.sql              metrics, decisions, okrs, key_results,
                                  task_dependencies, risks, commitments, cadences,
                                  cadence_runs
  seed.sql                        bootstrap config (current_phase=0)
messaging/discord/plugin/         Phase 1 Discord listener
  coo_phase1.py                   the bot — ~1700 lines
  run_ai.sh                       launches the agent CLI inside tmux
integrations/                     App integration catalog (one dir per app)
  echo/                           test integration (no OAuth)
  hubspot/                        real integration (real plugin code)
ARCHITECTURE.md                   design background
ROADMAP.md                        build sequence
README.legacy.md                  the original single-tenant bot's README (preserved)
discord_coo_bot.py                legacy single-tenant bot (predates this design;
                                  not used by the new platform; preserved for reference)
```

## What the agent does NOT do

Hard guardrails baked into the mission prompt:

- No hiring, firing, performance rating, or comp decisions.
- No contract signing or budget commitments.
- No PIP / termination conversations.
- No reading or writing of secrets files.

The agent owns observation, memory, cadence, and surfacing. Humans own judgment on people and money.

## Operators

This is a two-developer project: Dan Core and Adrien Kelly. Only registered platform developers can bootstrap tenants, enable integrations, advance phases, or run schema migrations. Tenant CEOs are the content authority for their own company-map data.

## License

Not yet declared.
