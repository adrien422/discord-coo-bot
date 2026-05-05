#!/bin/bash
set -euo pipefail

BOT_SCRIPT="/home/arman/workbench/vps-skill/discord/discord_coo_bot.py"
RUNNER="/home/arman/workbench/vps-skill/discord/run_discord_coo.sh"
STATE_DIR="/home/arman/workbench/.discord_coo_state"
LOG_FILE="$STATE_DIR/watchdog.log"

mkdir -p "$STATE_DIR"

if pgrep -u "$(id -u)" -f "python3 $BOT_SCRIPT" >/dev/null; then
  exit 0
fi

echo "$(date -Is) starting Discord COO bridge" >> "$LOG_FILE"
setsid -f "$RUNNER" >> "$LOG_FILE" 2>&1 < /dev/null
