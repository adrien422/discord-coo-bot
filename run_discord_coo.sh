#!/bin/bash
set -euo pipefail

SECRETS_FILE="${DISCORD_COO_SECRETS_FILE:-/home/arman/workbench/.discord_claudex.secrets}"
LOCK_FILE="${DISCORD_COO_LOCK_FILE:-/home/arman/workbench/.discord_coo_state/discord_coo.lock}"

if [ ! -r "$SECRETS_FILE" ]; then
  echo "Missing readable Discord COO secrets file: $SECRETS_FILE" >&2
  exit 1
fi

set -a
. "$SECRETS_FILE"
set +a

mkdir -p "$(dirname "$LOCK_FILE")"
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo "Discord COO bridge is already running; lock held at $LOCK_FILE" >&2
  exit 0
fi

exec /usr/bin/python3 /home/arman/workbench/vps-skill/discord/discord_coo_bot.py
