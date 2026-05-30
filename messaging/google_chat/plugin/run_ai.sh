#!/bin/bash
# Launches the agent CLI inside a tmux pane.
# Called by discord_coo_bot.py via DISCORD_COO_RUN_AI env var.
# First arg matches DISCORD_COO_AGENT_KIND ("claude" or "codex").
set -euo pipefail

# The agent runs unattended in a tmux pane — no human to answer the
# first-run "trust this folder?" / permission prompts. Skip them.
case "${1:-claude}" in
  claude)
    exec "${COO_CLAUDE_BIN:-claude}" --dangerously-skip-permissions
    ;;
  codex)
    exec "${COO_CODEX_BIN:-codex}" --dangerously-bypass-approvals-and-sandbox
    ;;
  *)
    echo "Unknown agent kind: $1 (expected 'claude' or 'codex')" >&2
    exit 2
    ;;
esac
