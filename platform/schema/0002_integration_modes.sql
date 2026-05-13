-- Add `mode` to tenant_apps to support the dynamic integration model:
--   mcp     — install / configure an MCP server in the tenant's CLAUDE_CONFIG_DIR.
--             Agent uses tools natively via Claude Code's MCP machinery.
--   http    — generic HTTP client. Operator provides base URL + auth scheme
--             + named queries / action whitelist in config_json. Agent emits
--             [[COO_HTTP_CALL]] markers.
--   plugin  — hand-written integration plugin under integrations/<slug>/plugin/.
--             Used only when MCP and generic-HTTP aren't enough.
--   manual  — informational only. Recorded as a fact in the tenant DB, no
--             automated access.

ALTER TABLE tenant_apps ADD COLUMN mode TEXT NOT NULL DEFAULT 'plugin';

INSERT INTO schema_version (version) VALUES (2);
