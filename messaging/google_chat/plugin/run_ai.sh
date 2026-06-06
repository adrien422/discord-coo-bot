#!/bin/bash
# Launches the agent CLI inside a tmux pane.
# Called by discord_coo_bot.py via DISCORD_COO_RUN_AI env var.
# First arg matches DISCORD_COO_AGENT_KIND ("claude" or "codex").
set -euo pipefail

# The agent runs unattended in a tmux pane — no human to answer the
# first-run "trust this folder?" / permission prompts. Skip them.
# Optional per-tenant MCP config (e.g. Google Analytics). When COO_MCP_CONFIG
# is set and the file exists, launch Claude with it so MCP tools are available.
MCP_ARGS=()
if [ -n "${COO_MCP_CONFIG:-}" ] && [ -f "${COO_MCP_CONFIG}" ]; then
  MCP_ARGS=(--mcp-config "${COO_MCP_CONFIG}")
fi

case "${1:-claude}" in
  claude)
    exec "${COO_CLAUDE_BIN:-claude}" --dangerously-skip-permissions "${MCP_ARGS[@]}"
    ;;
  codex)
    exec "${COO_CODEX_BIN:-codex}" --dangerously-bypass-approvals-and-sandbox
    ;;
  *)
    echo "Unknown agent kind: $1 (expected 'claude' or 'codex')" >&2
    exit 2
    ;;
esac
