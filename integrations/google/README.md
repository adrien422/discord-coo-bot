# Google Workspace integration

Connects a tenant to a Google account so the COO can:

1. **Mirror the company map** to a Google Sheet — one tab each for Org Chart, Teams, Priorities, Decisions, Commitments, Workflows, Risks. Overwritten on every sync, so it's always current.
2. **Mirror factsheets/reports** to Google Docs (one Doc per current report, in a `Reports/` sub-folder).
3. **Back up transcripts + reports** to a Drive folder (`Transcripts/`).
4. **Read/write Workspace files on command** — `read_file`, `write_doc`, `append_sheet`, `list_drive`.

Everything lives under one Drive folder: **`COO Agent — <Company>`**. The integration is idempotent: each artifact is created once (its Google id is recorded in the tenant DB's `google_artifacts` table) and updated in place afterwards.

No `google-api-python-client` dependency — it calls Google's REST APIs directly via `requests`.

## What you do in Google Cloud (one-time)

1. **Create / pick a project** at <https://console.cloud.google.com>.
2. **Enable APIs**: Google Drive API, Google Sheets API, Google Docs API.
3. **OAuth consent screen**: choose *Internal* (if the Google account is in a Workspace org) or *External* + add yourself as a test user. Add the three scopes (`spreadsheets`, `documents`, `drive`).
4. **Create an OAuth client** → *Web application*. Under **Authorized redirect URIs** add exactly:
   ```
   http://localhost:8090/callback
   ```
   Copy the **Client ID** and **Client secret**.

## Connect it

```bash
coo integration connect <tenant> google --mode plugin --team exec
```

The connect flow then:
1. prints the scopes and asks for the **client_id** / **client_secret**;
2. prints a consent URL and the SSH tunnel command —
   `ssh -L 8090:localhost:8090 <vm>` — open the URL in your **local** browser;
3. after you approve, Google redirects to `localhost:8090/callback?code=…` (it'll show a "can't reach this site" page — that's fine, the code is in the URL). Paste the whole URL (or just the `code` value) back into the prompt;
4. exchanges the code for tokens and saves them to
   `~/.local/share/coo/tenants/<tenant>/integrations/google/credentials.json` (0600).

The bot picks it up on the next integration loop tick, and you can force a first sync:

```bash
coo integration sync-now <tenant> google
# -> sync OK [...]: {"company_map_sheet": "https://docs.google.com/...", "reports_synced": N, "transcripts_backed_up": M}
```

## Credentials

The credentials file holds `client_id`, `client_secret`, `access_token`, `refresh_token`, and `expires_at`. `sync()` refreshes the access token on its own when it expires (the consent URL requests `access_type=offline` + `prompt=consent`, so Google returns a long-lived refresh token) and the framework re-persists the rotated token.

If a connect ever fails with *"Google did not return a refresh_token"*, revoke the prior grant at <https://myaccount.google.com/permissions> and reconnect.

## Scope semantics

Scoped to one team (default `exec`) like every integration. The Sheets/Docs/Drive scopes are full read/write so the agent can both mirror out and read/write Workspace files on command. To narrow it later, swap `drive` for `drive.file` in `manifest.json` (limits the agent to files it created) and reconnect.
