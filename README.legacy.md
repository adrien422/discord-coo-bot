# Discord VPS Integration

Captured: 2026-04-30

Purpose: Discord bridge setup state for the VPS skill, created per Arman's request to keep the planned Discord integration in its own subfolder.

## Application

- Name: Claudex
- Application ID: 1499160835698855966
- Public Key: e1adfdeca37c0dd5cdf4e8b5fd0fda1082e508e21f9822c3f9bcb224654593fc

## Target Discord Server

- Guild ID / Server ID: 1499169248402997379
- General Text Channel ID: 1499169249766277222
- General Voice Channel ID: 1499169249766277223
- COO home channel: `coo-cockpit` / 1499180162057769020

## General Information Fields

- App Icon: unset
- Description: unset
- Tags: unset
- Install Count: 0 Servers, 0 Individual Users
- Authorization Count: 0 Individual Users
- Interactions Endpoint URL: unset
- Linked Roles Verification URL: unset
- Terms of Service URL: unset
- Privacy Policy URL: unset

## Notes

- Bot token is stored locally at `/home/arman/workbench/.discord_claudex.secrets` (`0600`); do not copy it into skill docs or packaged archives.
- Public key is only needed if Discord interactions are received over an HTTP endpoint.
- The Discord bridge is designed as a proactive COO agent, not a normal support bot and not a Telegram clone.

## Current Portal Settings

- Requires OAuth2 Code Grant: off
- Message Content Intent: on
- Presence Intent: on
- Server Members Intent: on

## COO Bridge

Files:

- `discord_coo_bot.py`: Discord Gateway + REST bridge backed by one persistent `tmux` Codex or Claude Code session.
- `bootstrap_discord_env.py`: idempotently creates the real COO Discord environment and writes channel IDs into the local secrets/state files.
- `discord_coo_selfcheck.py`: local regression checks for bridge packaging, pane classification, and event logging.
- `register_cockpit_commands.py`: idempotently registers the guild-scoped `/coo` Discord application command.
- `discord_coo_looptest.py`: repeatable live/synthetic test loop for the Discord bridge and cockpit.
- `run_discord_coo.sh`: loads `/home/arman/workbench/.discord_claudex.secrets` and starts the bridge.
- `discord-coo.service`: systemd unit template for the bridge.
- `watch_discord_coo.sh`: non-root watchdog fallback; systemd is preferred on this VPS.

Runtime paths:

- Agent tmux target: `discord_coo:agent.0`
- Agent workdir: `/home/arman/workbench/discord-coo-workspace`
- Agent Discord environment doc: `/home/arman/workbench/discord-coo-workspace/DISCORD_ENVIRONMENT.md`
- Employee reference inbox: `/home/arman/workbench/discord-coo-workspace/reference/inbox`
- Inbox attention state: `/home/arman/workbench/.discord_coo_state/state.json` key `reference_status`
- Daily transcripts: `/home/arman/workbench/discord-coo-workspace/reference/transcripts`
- Weekly/monthly room factsheets: `/home/arman/workbench/discord-coo-workspace/reference/factsheets`
- Bridge state: `/home/arman/workbench/.discord_coo_state/state.json`
- Bridge log: `/home/arman/workbench/.discord_coo_state/discord_coo.log`

Conversation model:

- Discord is headless: the tmux agent pane is hidden backend state, not a Discord terminal view.
- Default mode is `bot_owned`.
- Admins/managers can seed work with commands from admin rooms.
- Normal employees can send messages, but unsolicited messages are saved into the VPS reference inbox instead of being fed directly into the CLI agent.
- In lower-right rooms, a human message reaches the COO agent only when it replies to a COO bot message.
- In admin rooms (`coo-admin`, `coo-config`, `coo-audit`), a human message reaches the COO agent only when it replies to a COO bot message and mentions the bot.
- All human messages in watched channels are saved into daily per-channel transcripts with clear sender names.
- Saved reference messages are organized by Discord channel/department, person, date, and uncategorized matter folder.
- Saved reference messages carry classification tags in their JSON/Markdown records, such as `source-inbox`, `reason-requires-reply-to-coo-message`, `needs-coo-reply`, `needs-admin-mention`, `has-attachments`, `matter-uncategorized`, and `channel-*`.
- Workflow state is deliberately separate from tags and is browsable from cockpit buttons: `pending`, `queued`, `held`, `no-action`, `initiated`, `failed`, plus legacy `attended`.
- Open follow-up conversations also carry conversation tags such as `followup-open`, `targeted`, `channel-wide`, and `channel-*`.
- Inbox monitor wakes every `DISCORD_COO_INBOX_MONITOR_INTERVAL_SECONDS` and queues pending/failed saved messages for COO attention, rate-limited by `DISCORD_COO_INBOX_ATTENTION_COOLDOWN_SECONDS`.
- Scheduled/manual COO pulses first drain pending inbox attention before running a general pulse.
- The COO can finish a loop by including `[[COO_CLOSE]]`; the bridge strips that marker before posting to Discord.
- The COO can mark saved references as held with `[[COO_HOLD]]`, or no-action with `[[COO_NO_ACTION]]` or `NOOP`; the bridge strips markers and updates state without mixing those states into file tags.
- Each room has weekly and monthly factsheets generated under `reference/factsheets`; cockpit users can view them and admins can queue an agent update.
- The bridge discovers the Discord application owner as an initial admin. Extra admins can be added with `DISCORD_COO_ADMIN_USER_IDS`.
- Locked-room access can be widened later with `DISCORD_COO_ADMIN_USER_IDS`, `DISCORD_COO_STRATEGIC_USER_IDS`, `DISCORD_COO_MANAGER_USER_IDS`, or matching `*_ROLE_IDS`.
- Admin COO commands are restricted to `coo-admin`, `coo-config`, and `coo-audit`.

Commands:

- `/coo cockpit`: open the Discord-native embed cockpit panel with buttons.
- `/coo status`, `/coo inbox`, `/coo channels`, `/coo facts`: read-only cockpit views.
- `/coo queue`: show the In Queue tab: live agent queue, unattended/queued inbox messages, open follow-ups, and Claude Code automation lanes; admin user plus admin room required.
- `/coo tags`: show reference/conversation classification tags and counts; admin user plus admin room required.
- `/coo updatefacts`: queue a room factsheet refresh; admin user plus admin room required.
- `/coo review`: immediately queue pending inbox messages for COO attention; admin user plus admin room required.
- `/coo followups`: show open COO follow-ups; admin user plus admin room required.
- `/coo pulse`: queue a manual COO pulse; admin user plus admin room required.
- `!coo status`: bridge/session status.
- `!coo channels`: watched channel IDs.
- `!coo followups`: active bot-owned follow-ups; `!coo conversations` is retained as a compatibility alias.
- `!coo inbox`: saved employee reference message count and recent entries.
- `!coo queue`: In Queue view for pending/queued/held/no-action/initiated/failed inbox state, open follow-ups, and Claude Code automation lanes.
- `!coo facts` / `!coo updatefacts`: view or refresh the current room's weekly/monthly factsheets.
- `!coo tags`: reference/conversation classification tags and counts.
- `!coo review`: immediately queue pending inbox messages for COO attention.
- `!coo pulse`: queue an immediate COO review.
- `!coo send <prompt>`: admin direct prompt to the COO agent.
- `!coo watch` / `!coo unwatch`: add/remove the current channel from watched channels.
- `!coo home`: make the current channel the home channel.
- `!coo close`: close the active COO loop in the current channel.
- `!coo interrupt`, `!coo enter`, `!coo compact`, `!coo clear`: admin control commands for the agent session.

Start/test:

```bash
/home/arman/workbench/vps-skill/discord/discord_coo_selfcheck.py
/home/arman/workbench/vps-skill/discord/bootstrap_discord_env.py
/home/arman/workbench/vps-skill/discord/register_cockpit_commands.py
/home/arman/workbench/vps-skill/discord/discord_coo_looptest.py --iterations 3 --delay 1
timeout 30 /home/arman/workbench/vps-skill/discord/run_discord_coo.sh
sudo cp /home/arman/workbench/vps-skill/discord/discord-coo.service /etc/systemd/system/discord-coo.service
sudo systemctl daemon-reload
sudo systemctl enable --now discord-coo.service
systemctl status discord-coo --no-pager
```

Loop-test coverage:

- Live Discord REST: bot auth, gateway URL, guild channels, watched/admin channel existence, `/coo` command registration, message send/fetch/reaction/delete in `coo-cockpit`.
- Local synthetic gateway events: unsolicited employee save-only path, public reply-to-bot live intake, admin reply without mention rejection, admin reply plus mention live intake, `!coo send` room scoping, embed cockpit render, admin-only Open Follow-ups visibility, In Queue visibility, browsable workflow-state buttons, classification tag view, room factsheet view/update queueing, Codex and Claude Code forwarder parsing, Claude Code automation lanes, review-inbox queueing, seed modal, modal queueing, public admin-control denial, transcript/reference writes, and `[[COO_CLOSE]]` stripping.
- Limit: Discord does not allow a bot to invoke its own slash command or impersonate a real human account, so those user-originated events are tested by direct handler payloads while command registration is verified against Discord live.

## Claude Code Automation Notes

Inspected on this VPS: `claude` is `/home/arman/.local/bin/claude`, a wrapper that defaults sessions to `--dangerously-skip-permissions`; `claude-native` points at the actual native binary. Relevant local capabilities are `--print`, `--continue`, `--resume`, `--output-format json|stream-json`, `--input-format stream-json`, `--max-turns`, `--permission-mode`, `--append-system-prompt`, `--system-prompt`, `--agent`, `--agents`, `agents`, custom commands under `~/.claude/commands`, hooks in `~/.claude/settings.json`, and Chrome integration flags.

The live service still defaults to Codex, but `DISCORD_COO_AGENT_KIND=claude` now starts a persistent Claude Code pane and forwards completed Claude assistant turns from `~/.claude/projects/<cwd-slug>/*.jsonl`.

- Headless routines: systemd timer/cron can invoke `claude --print --continue --output-format json` or `stream-json` against a focused inbox prompt, then post summarized output into Discord.
- Persistent Claude pane: set `DISCORD_COO_AGENT_KIND=claude`; the bridge tails Claude transcript JSONL and uses the same Discord output, `[[COO_CLOSE]]`, `[[COO_HOLD]]`, and `[[COO_NO_ACTION]]` handling as Codex.

Cockpit mapping:

- Routines: Claude Code cloud routines, created in the web UI or from the CLI with `/schedule`; durable outside this VPS, not locally enumerable by the current bot token.
- Schedules: Claude Code session scheduled tasks via `CronCreate`/`CronList`; visible to a Claude Code-backed bridge and shown as not attached while the live service uses Codex.
- Loops: Claude Code `/loop` bundled skill backed by scheduled tasks; session-scoped and restored on resume if unexpired.
- Monitors: Claude Code Monitor tool used by dynamic `/loop` for background script output; session-scoped/background task.

Official docs used for this decision: Claude Code CLI reference, common workflows, hooks, slash commands, SDK slash commands, and subagents.

## Discord Environment Layout

`bootstrap_discord_env.py` created this idempotent server structure. The latest regression pass reran with `created: []`.

- `COO Control` / 1499180160421986377: `coo-cockpit` 1499180162057769020, `coo-decisions` 1499180163026653245, `coo-escalations` 1499180164091871292, `coo-pulses` 1499180165115281551
- `Departments` / 1499180166478303312: `operations` 1499180168848216128, `accounting` 1499180170035335311, `sales` 1499180171620515917, `support` 1499180173021679778, `tech` 1499180174430830767, `people` 1499180176175534321
- `Employee Inbox` / 1499180177379430581: `employee-notes` 1499180178633654343, `blockers` 1499180180088819832, `handoffs` 1499180180894257255
- `Strategic Staff` / 1499181670438277250 locked, 3 overwrites: `strategy` 1499181671839170794, `leadership` 1499181673000992849, `finance-strategy` 1499181675001675959
- `COO Administration` / 1499181678319374358 locked, 3 overwrites: `coo-admin` 1499181679980445797, `coo-config` 1499181681779802112, `coo-audit` 1499181682941493299
- `Cockpits` / 1499181686238347514 locked, 3 overwrites: `executive-cockpit` 1499181687479734384, `manager-cockpit` 1499181688956260492, `department-cockpit` 1499181689774145598

The bootstrapper sets `coo-cockpit` as `DISCORD_COO_HOME_CHANNEL_ID`, updates `DISCORD_COO_CHANNEL_IDS`, sets `DISCORD_COO_ADMIN_CHANNEL_IDS` to the locked admin rooms, writes `channels.json`, mirrors the watched channels into `/home/arman/workbench/.discord_coo_state/state.json`, and writes `/home/arman/workbench/discord-coo-workspace/DISCORD_ENVIRONMENT.md` for the persistent COO agent.

Required invite if channel creation or locked-room permission updates fail with `Missing Permissions`:

```text
https://discord.com/oauth2/authorize?client_id=1499160835698855966&permissions=268553296&integration_type=0&scope=bot+applications.commands
```

Discord API note: modifying channel settings requires `MANAGE_CHANNELS`; modifying permission overwrites may require `MANAGE_ROLES`, and Discord only permits allow/deny bits the bot itself has. See Discord's channel resource docs: https://docs.discord.com/developers/resources/channel

Discord app note: the cockpit uses gateway-delivered interactions, so no public Interactions Endpoint URL is required. Discord documents Gateway and outgoing-webhook interaction delivery as mutually exclusive, and `/coo` is registered as a guild application command. See https://docs.discord.com/developers/interactions/overview and https://docs.discord.com/developers/interactions/application-commands

## Test Status

- 2026-04-29 22:13 UTC: Bot token validated via `/users/@me` and `/gateway/bot`.
- 2026-04-29 22:13 UTC: Bot listed guild channels for `1499169248402997379`.
- 2026-04-29 22:13 UTC: Bot posted to General Text Channel; message ID `1499171900302295121`.
- 2026-04-30 UTC: Implemented MVP proactive COO bridge with bot-owned conversation gating and saved employee reference inbox.
- 2026-04-30 UTC: Foreground smoke reached Discord Gateway, injected the COO mission into `discord_coo:agent.0`, and forwarded the first COO message to General.
- 2026-04-30 UTC: Installed and started `discord-coo.service`; exactly one bridge process remained after cleanup.
- 2026-04-30 UTC: Added selfcheck coverage for the event-log key collision, Codex pane classifier blank-row case, model-switch prompt, and method indentation.
- 2026-04-30 UTC: Added idempotent Discord environment bootstrap. First live run was blocked by Discord `Missing Permissions`; use the invite URL above, then rerun `bootstrap_discord_env.py`.
- 2026-04-30 UTC: After re-authorization, `bootstrap_discord_env.py` created 3 categories and 13 text channels, set `coo-cockpit` as home, and the service restarted cleanly with one active bridge process.
- 2026-04-30 UTC: Added locked `Strategic Staff`, `COO Administration`, and `Cockpits` categories, restricted admin commands to the admin rooms, and added regression guards for idempotent permission-overwrite comparison.
- 2026-04-30 UTC: Verified the locked categories/channels have permission overwrites, wrote `DISCORD_ENVIRONMENT.md` into the COO workspace, and confirmed the bridge lock prevents a duplicate process.
- 2026-04-30 UTC: Tightened live intake so lower rooms require replying to a COO bot message and admin rooms require reply plus mention; added daily per-channel transcripts and regression guards for all intake cases.
- 2026-04-30 UTC: Added Discord-native `/coo` cockpit app surface with slash subcommands, button controls, an admin seed modal, and selfcheck coverage.
- 2026-04-30 UTC: Added `discord_coo_looptest.py` and passed 3 live loop iterations against Discord plus synthetic gateway/interaction handler coverage.
- 2026-04-30 UTC: Renamed user-facing cockpit loop language to Open Follow-ups and hid it from lower-room cockpit panels.
- 2026-04-30 UTC: Added inbox attention states, `/coo queue`, `/coo review`, admin cockpit Inbox Queue/Review Inbox buttons, and an automatic inbox monitor loop.
- 2026-04-30 UTC: Reworked cockpit into an embed panel and added an In Queue tab that maps COO queue state to Claude Code routines, schedules, loops, and monitors.
- 2026-04-30 UTC: Added cockpit tracking tags, persisted inbox tags, tag summary/filter buttons, and `/coo tags`.
- 2026-04-30 UTC: Split workflow states from classification tags, added browsable Pending/Queued/Held/No Action/Initiated/Failed cockpit buttons, added `[[COO_HOLD]]` and `[[COO_NO_ACTION]]`, and added weekly/monthly room factsheets with cockpit view/update controls.
- 2026-04-30 UTC: Added `DISCORD_COO_AGENT_KIND=claude` support, Claude pane classification/trust-prompt dismissal, Claude transcript JSONL forwarding, and regression coverage proving Claude assistant turns reach Discord through the same conversation forwarder.
