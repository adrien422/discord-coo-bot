# Messaging plugins

A tenant uses exactly one messaging platform — Discord or Google Chat — chosen during the bootstrap wizard. Each platform has a listener plugin that bridges its events to the tenant's agent.

## Layout

```
messaging/
  discord/
    plugin/             -- listener: gateway connection, event normalization, slash commands
    README.md
  google-chat/
    plugin/             -- listener: webhook or pubsub, event normalization
    README.md
```

## Drop-in connectors

Dan has Discord and Google Chat → Claude Code connectors developed elsewhere. They will be dropped into the corresponding `plugin/` directories. The architecture document treats them as black boxes that emit a small set of normalized events:

- `message_received(channel_id, user_id, content, message_id, attachments)`
- `command_invoked(channel_id, user_id, command, args, message_id)`
- `dm_opened(user_id, channel_id)`
- `member_joined(channel_id, user_id)`

The platform layer doesn't care which messaging platform a tenant uses; the rest of the system works against the normalized events.

## Per-tenant isolation

Each tenant runs its own listener instance under its own Linux user (`coo-<slug>`). Bot tokens / service-account keys live in `/var/coo/tenants/<slug>/messaging/secrets` (mode `0600`). Two tenants on the same VM are completely isolated — no shared listener process, no shared credential store.
