#!/usr/bin/env python3
"""Register Discord COO cockpit slash commands for the target guild."""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen


DISCORD_API = "https://discord.com/api/v10"
DEFAULT_SECRETS = Path("/home/arman/workbench/.discord_claudex.secrets")


COMMANDS = [
    {
        "name": "coo",
        "description": "Open and operate the Claudex COO cockpit.",
        "type": 1,
        "options": [
            {
                "type": 1,
                "name": "cockpit",
                "description": "Open a Discord-native COO cockpit panel.",
            },
            {
                "type": 1,
                "name": "status",
                "description": "Show COO bridge status.",
            },
            {
                "type": 1,
                "name": "inbox",
                "description": "Show saved employee reference inbox status.",
            },
            {
                "type": 1,
                "name": "queue",
                "description": "Show agent queue, inbox queue, follow-ups, and Claude Code automation lanes.",
            },
            {
                "type": 1,
                "name": "facts",
                "description": "Show this room's weekly and monthly COO factsheets.",
            },
            {
                "type": 1,
                "name": "updatefacts",
                "description": "Queue a COO update for this room's factsheets.",
            },
            {
                "type": 1,
                "name": "tags",
                "description": "Show reference and conversation classification tags.",
            },
            {
                "type": 1,
                "name": "review",
                "description": "Queue pending inbox messages for COO attention.",
            },
            {
                "type": 1,
                "name": "followups",
                "description": "Show open COO follow-ups from an admin room.",
            },
            {
                "type": 1,
                "name": "channels",
                "description": "Show watched Discord channel IDs.",
            },
            {
                "type": 1,
                "name": "pulse",
                "description": "Queue an immediate COO pulse from an admin room.",
            },
        ],
    }
]


def load_env_file(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    pattern = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = pattern.match(line)
        if not match:
            continue
        key, value = match.groups()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        env[key] = value
    return env


def request_json(method: str, route: str, token: str, body: object | None = None) -> object:
    payload = None if body is None else json.dumps(body).encode("utf-8")
    headers = {
        "Authorization": "Bot " + token,
        "User-Agent": "ClaudexDiscordCOO/0.1",
    }
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = Request(DISCORD_API + route, data=payload, headers=headers, method=method)
    while True:
        try:
            with urlopen(req, timeout=30) as response:
                text = response.read().decode("utf-8")
                return json.loads(text) if text else {}
        except HTTPError as exc:
            text = exc.read().decode("utf-8", "replace")
            if exc.code == 429:
                try:
                    retry_after = float(json.loads(text).get("retry_after", 1.0))
                except Exception:
                    retry_after = 1.0
                time.sleep(retry_after)
                continue
            raise RuntimeError(f"Discord {method} {route} failed {exc.code}: {text[:500]}") from exc


def main() -> int:
    secrets_file = Path(os.environ.get("DISCORD_COO_SECRETS_FILE", str(DEFAULT_SECRETS)))
    env = {**load_env_file(secrets_file), **os.environ}
    token = env.get("DISCORD_CLAUDEX_BOT_TOKEN")
    app_id = env.get("DISCORD_CLAUDEX_APPLICATION_ID")
    guild_id = env.get("DISCORD_COO_GUILD_ID")
    if not token or not app_id or not guild_id:
        print("Missing DISCORD_CLAUDEX_BOT_TOKEN, DISCORD_CLAUDEX_APPLICATION_ID, or DISCORD_COO_GUILD_ID", file=sys.stderr)
        return 2
    result = request_json("PUT", f"/applications/{app_id}/guilds/{guild_id}/commands", token, COMMANDS)
    print(json.dumps({
        "registered": [cmd.get("name") for cmd in result] if isinstance(result, list) else result,
        "guild_id": guild_id,
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
