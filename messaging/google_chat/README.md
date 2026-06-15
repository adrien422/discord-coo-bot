# Google Chat messaging plugin

Parallel to `messaging/discord/`. Runs per tenant, bridging the Claude Code agent (in a tmux pane) to Google Chat over Pub/Sub (inbound) + Chat REST API (outbound).

## How it works

```
            ┌──────────────────────── per-tenant daemon ────────────────────────┐
Google Chat │                                                                   │
   events ──▶── Pub/Sub topic ──▶── subscriber pull ──▶── agent (tmux pane)     │
            │                                              │                    │
            │                                              ▼                    │
Chat REST ◀────  outbound:  DMs / channel posts  ◀── marker dispatch + DB write │
            └───────────────────────────────────────────────────────────────────┘
```

Inbound is **Pub/Sub** (the Chat API publishes every DM / space event to a topic in the GCP project). The daemon subscribes and pulls — no public URL on the VM. Outbound is plain HTTPS to `chat.googleapis.com`.

Auth is one **service account JSON** with two scopes used at runtime:
- `https://www.googleapis.com/auth/pubsub` — pull from the subscription
- `https://www.googleapis.com/auth/chat.bot` (+ `chat.messages`, `chat.spaces`) — post as the Chat app

## Marker conventions

The Discord listener identifies people by numeric `user_id`. Chat uses emails or `users/<id>` resources. `coo_chat.py` defines:

- `CHAT_TO_RE` — `[[COO_TO user_id=<email-or-users/ID>]] <text>` → DM via Chat REST `spaces.findDirectMessage` then `messages.create`.
- `CHAT_CHANNEL_RE` — `[[COO_CHANNEL name=<space-display-name>]] <text>` or `id=<spaces/AAA>` → post in space.

All persistence markers (`COO_FACT`, `COO_COMMITMENT`, `COO_DECISION`, …) and bridge protocol (`AgentBridge`, `_chunk_message`, `NOOP`) are reused from `messaging/discord/plugin/coo_phase1.py`.

## Per-tenant env (set via `secrets.env`)

```
COO_TENANT_SLUG=<slug>
COO_TENANT_DB=/home/<user>/.local/share/coo/tenants/<slug>/db/coo.db
COO_PLATFORM_DB=/home/<user>/.local/share/coo/platform/platform.db
COO_STATE_DIR=/home/<user>/.local/share/coo/tenants/<slug>/state
COO_WORKDIR=/home/<user>/.local/share/coo/tenants/<slug>
COO_TMUX_SESSION=coo_<slug>
COO_RUN_AI=<repo>/messaging/google_chat/plugin/run_ai.sh
COO_AGENT_KIND=claude

COO_CHAT_PROJECT_ID=<gcp-project-id>
COO_CHAT_SUBSCRIPTION=coo-chat-events-sub
COO_CHAT_SA_JSON=/home/<user>/.local/share/coo/tenants/<slug>/iris-bot-sa.json
COO_CHAT_HOME_SPACE=spaces/<id-of-home-space>
COO_CHAT_CEO_EMAIL=<ceo-email-in-workspace>
COO_CHAT_COO_DISPLAY_NAME=Iris
```

## GCP setup (one-time)

1. Enable APIs: Google Chat, Pub/Sub, Sheets, Docs, Drive, Forms, Tasks, Calendar, Gmail.
2. Pub/Sub: create topic `coo-chat-events`. On the topic, grant `chat-api-push@system.gserviceaccount.com` the **Pub/Sub Publisher** role. Create a Pull subscription `coo-chat-events-sub`.
3. Google Chat API → **Configuration**:
   - App name: `Iris` · Description: `The autonomous COO for <Company>.`
   - Functionality: 1:1 messages + Join spaces.
   - Connection: **Cloud Pub/Sub**, topic = `projects/<project>/topics/coo-chat-events`.
   - Visibility: scoped to the people who'll use it.
4. Create a service account `iris-bot` in the project. Grant it `Pub/Sub Subscriber` on the subscription. Download a JSON key — that's `COO_CHAT_SA_JSON`.

## First-contact pattern

The Chat API doesn't reveal a user's resource ID without prior contact. So the **first message must come from the user** (e.g. the CEO DMs the bot). The listener captures the sender's `users/<id>` from that event and stores it in `people.google_chat_user_id`. After that, the agent can DM the user freely.

To pre-seed someone you want the bot to greet later, insert them into `people` with their email — first time they DM in, the chat_user_id backfills automatically.

## Running

The listener is a long-lived daemon. Launch it directly for dev:

```
cd <repo>
set -a; source <tenant-secrets.env>; set +a
python3 messaging/google_chat/plugin/coo_chat.py
```

For production, the tenant CLI writes a systemd-user unit (`coo-chat-tenant@<slug>.service`) that runs this with restart-on-failure.
