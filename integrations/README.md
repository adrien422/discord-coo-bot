# Integrations registry

External app connectors that a tenant can opt into during or after onboarding. Each integration is **scoped to a single team** at enable time — the agent uses the app's data only when consulting for that team and takes actions in the app only on commands from that team's members.

## Layout

Each integration lives in its own subdirectory:

```
integrations/
  <slug>/
    schema.sql          -- per-tenant tables this integration adds (e.g. hubspot_contacts, hubspot_deals)
    oauth.json          -- OAuth client config: scopes, redirect URI, token endpoints
    actions.json        -- whitelist of agent-permitted actions (e.g. "create_note", "update_deal_stage")
    plugin/             -- runtime code: pull data, push actions, schedule sync
    README.md           -- setup notes, scope semantics, what the agent CAN and CAN'T do
```

## Enable flow

1. A manager mentions the app in an interview (or operator runs `coo integration enable <tenant> <slug>`).
2. Wizard asks: "Which team uses <app>?" → records `scoped_team_slug` in `platform.tenant_apps`.
3. OAuth dance via the same SSH-tunnel pattern used for Google.
4. Platform applies `integrations/<slug>/schema.sql` to the tenant's DB.
5. Plugin starts pulling data on its declared cadence; agent gains access to read.

## Initial targets

- `hubspot/` — CRM, sales pipeline, contacts. Default scope: Sales team.
- `goto/` — calls, meetings, recordings. Default scope: Sales or Customer Success.
- `gleap/` — bug reports, feedback, NPS. Default scope: Engineering or Product.

## Schema-flexibility rule

Integrations are how the system gets per-company schema flexibility *without* runtime DDL. Each integration ships a **predefined, reviewed schema**. The agent never invents new tables on its own initiative. New integrations are added through the dev gate (Dan + Adrien tech-approve, then they're available as a registry option).
