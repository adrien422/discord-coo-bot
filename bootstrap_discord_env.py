#!/usr/bin/env python3
"""Create the Discord COO workspace channels idempotently."""

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
DEFAULT_STATE = Path("/home/arman/workbench/.discord_coo_state/state.json")
CHANNEL_MAP = Path("/home/arman/workbench/vps-skill/discord/channels.json")
DEFAULT_WORKDIR = Path("/home/arman/workbench/discord-coo-workspace")
REQUIRED_PERMISSION_BITS = 268553296
VIEW_CHANNEL = 1024
ADD_REACTIONS = 64
MANAGE_CHANNELS = 16
MANAGE_MESSAGES = 8192
SEND_MESSAGES = 2048
EMBED_LINKS = 16384
ATTACH_FILES = 32768
READ_MESSAGE_HISTORY = 65536
ROOM_BITS = VIEW_CHANNEL | ADD_REACTIONS | SEND_MESSAGES | EMBED_LINKS | ATTACH_FILES | READ_MESSAGE_HISTORY
ADMIN_ROOM_BITS = ROOM_BITS | MANAGE_CHANNELS

LAYOUT = [
    {
        "category": "COO Control",
        "channels": [
            ("coo-cockpit", "COO-owned command channel for manager-seeded work."),
            ("coo-decisions", "Decision log and final calls from the COO agent."),
            ("coo-escalations", "Escalations that need Arman or manager attention."),
            ("coo-pulses", "Scheduled COO check-ins and proactive summaries."),
        ],
    },
    {
        "category": "Departments",
        "channels": [
            ("operations", "Operational blockers, handoffs, and daily execution signals."),
            ("accounting", "Accounting and finance signals for COO review."),
            ("sales", "Sales pipeline notes, blockers, and follow-up needs."),
            ("support", "Customer or guest support issues needing coordination."),
            ("tech", "Engineering, automation, infrastructure, and AI tooling work."),
            ("people", "Hiring, staffing, HR, and internal coordination notes."),
        ],
    },
    {
        "category": "Employee Inbox",
        "channels": [
            ("employee-notes", "Employee notes saved to the COO reference inbox unless a COO loop is open."),
            ("blockers", "Employee blockers saved for COO triage."),
            ("handoffs", "Shift, ownership, and cross-department handoff notes."),
        ],
    },
    {
        "category": "Strategic Staff",
        "access": "strategic",
        "channels": [
            ("strategy", "Locked strategic planning and executive context."),
            ("leadership", "Locked leadership coordination with the COO agent."),
            ("finance-strategy", "Locked finance-sensitive strategy and planning."),
        ],
    },
    {
        "category": "COO Administration",
        "access": "admin",
        "channels": [
            ("coo-admin", "Locked admin management room for the COO agent."),
            ("coo-config", "Locked COO configuration and integration changes."),
            ("coo-audit", "Locked bridge, agent, and operational audit notes."),
        ],
    },
    {
        "category": "Cockpits",
        "access": "manager",
        "channels": [
            ("executive-cockpit", "Locked executive cockpit for strategic COO work."),
            ("manager-cockpit", "Locked manager cockpit for department coordination."),
            ("department-cockpit", "Locked department-level cockpit for operational summaries."),
        ],
    },
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


def shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def update_env_file(path: Path, updates: dict[str, str]) -> None:
    original = path.read_text().splitlines() if path.exists() else []
    seen: set[str] = set()
    rewritten: list[str] = []
    pattern = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=")
    for line in original:
        match = pattern.match(line.strip())
        if not match:
            rewritten.append(line)
            continue
        key = match.group(1)
        if key in updates:
            rewritten.append(f"export {key}={shell_quote(updates[key])}")
            seen.add(key)
        else:
            rewritten.append(line)
    for key, value in updates.items():
        if key not in seen:
            rewritten.append(f"export {key}={shell_quote(value)}")
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("\n".join(rewritten) + "\n")
    tmp.chmod(0o600)
    tmp.replace(path)


def request_json(method: str, route: str, token: str, body: dict | None = None) -> object:
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


def parse_csv_ids(value: str | None) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


def discover_application_admins(token: str) -> list[str]:
    data = request_json("GET", "/oauth2/applications/@me", token)
    if not isinstance(data, dict):
        return []
    admins: list[str] = []
    owner = data.get("owner") or {}
    if owner.get("id"):
        admins.append(str(owner["id"]))
    team = data.get("team") or {}
    for member in team.get("members") or []:
        user = member.get("user") or {}
        if user.get("id"):
            admins.append(str(user["id"]))
    return list(dict.fromkeys(admins))


def discover_bot_user_id(token: str) -> str:
    data = request_json("GET", "/users/@me", token)
    if not isinstance(data, dict) or not data.get("id"):
        raise RuntimeError("Could not discover bot user id")
    return str(data["id"])


def permission_overwrites(env: dict[str, str], token: str, access: str | None) -> list[dict] | None:
    if not access:
        return None
    admin_users = list(dict.fromkeys([
        *discover_application_admins(token),
        *parse_csv_ids(env.get("DISCORD_COO_ADMIN_USER_IDS")),
    ]))
    bot_user_id = discover_bot_user_id(token)
    strategic_users = parse_csv_ids(env.get("DISCORD_COO_STRATEGIC_USER_IDS"))
    manager_users = parse_csv_ids(env.get("DISCORD_COO_MANAGER_USER_IDS"))
    admin_roles = parse_csv_ids(env.get("DISCORD_COO_ADMIN_ROLE_IDS"))
    strategic_roles = parse_csv_ids(env.get("DISCORD_COO_STRATEGIC_ROLE_IDS"))
    manager_roles = parse_csv_ids(env.get("DISCORD_COO_MANAGER_ROLE_IDS"))
    guild_id = env["DISCORD_COO_GUILD_ID"]

    overwrites = [
        {"id": guild_id, "type": 0, "deny": str(VIEW_CHANNEL), "allow": "0"},
        {"id": bot_user_id, "type": 1, "allow": str(ADMIN_ROOM_BITS), "deny": "0"},
    ]

    def allow_users(user_ids: list[str], bits: int) -> None:
        for user_id in user_ids:
            overwrites.append({"id": user_id, "type": 1, "allow": str(bits), "deny": "0"})

    def allow_roles(role_ids: list[str], bits: int) -> None:
        for role_id in role_ids:
            overwrites.append({"id": role_id, "type": 0, "allow": str(bits), "deny": "0"})

    if access == "admin":
        allow_users(admin_users, ADMIN_ROOM_BITS)
        allow_roles(admin_roles, ADMIN_ROOM_BITS)
    elif access == "strategic":
        allow_users([*admin_users, *strategic_users], ROOM_BITS)
        allow_roles([*admin_roles, *strategic_roles], ROOM_BITS)
    elif access == "manager":
        allow_users([*admin_users, *strategic_users, *manager_users], ROOM_BITS)
        allow_roles([*admin_roles, *strategic_roles, *manager_roles], ROOM_BITS)
    else:
        raise RuntimeError(f"Unknown access class: {access}")

    deduped: dict[tuple[str, int], dict] = {}
    for overwrite in overwrites:
        key = (str(overwrite["id"]), int(overwrite["type"]))
        existing = deduped.get(key)
        if existing:
            existing["allow"] = str(int(existing["allow"]) | int(overwrite["allow"]))
            existing["deny"] = str(int(existing["deny"]) | int(overwrite["deny"]))
        else:
            deduped[key] = dict(overwrite)
    return list(deduped.values())


def normalize_overwrites(overwrites: list[dict] | None) -> dict[tuple[str, int], tuple[int, int]]:
    normalized: dict[tuple[str, int], tuple[int, int]] = {}
    for overwrite in overwrites or []:
        key = (str(overwrite.get("id")), int(overwrite.get("type", 0)))
        allow = int(str(overwrite.get("allow", "0")))
        deny = int(str(overwrite.get("deny", "0")))
        normalized[key] = (allow, deny)
    return normalized


def overwrites_match(current: list[dict] | None, desired: list[dict] | None) -> bool:
    return normalize_overwrites(current) == normalize_overwrites(desired)


def install_url(application_id: str) -> str:
    return (
        "https://discord.com/oauth2/authorize"
        f"?client_id={application_id}"
        f"&permissions={REQUIRED_PERMISSION_BITS}"
        "&integration_type=0"
        "&scope=bot+applications.commands"
    )


def find_category(channels: list[dict], name: str) -> dict | None:
    wanted = name.casefold()
    for channel in channels:
        if channel.get("type") == 4 and str(channel.get("name", "")).casefold() == wanted:
            return channel
    return None


def find_text_channel(channels: list[dict], name: str, parent_id: str | None = None) -> dict | None:
    for channel in channels:
        if channel.get("type") != 0 or channel.get("name") != name:
            continue
        if parent_id is None or str(channel.get("parent_id") or "") == parent_id:
            return channel
    return None


def load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    tmp.replace(path)


def write_environment_doc(workdir: Path, layout: dict, home_channel_id: str, admin_channel_ids: list[str], watched: list[str]) -> Path:
    workdir.mkdir(parents=True, exist_ok=True)
    text_channels = layout["text_channels"]
    categories = layout["categories"]
    lines = [
        "# Discord COO Environment",
        "",
        f"- Guild ID: {layout['guild_id']}",
        f"- Home channel: coo-cockpit ({home_channel_id})",
        f"- Watched channel count: {len(watched)}",
        f"- Admin command channels: {', '.join(admin_channel_ids)}",
        "",
        "## Categories",
        "",
    ]
    for category, category_id in sorted(categories.items()):
        lines.append(f"- {category}: {category_id}")
    lines.extend(["", "## Text Channels", ""])
    for name, channel_id in sorted(text_channels.items()):
        lines.append(f"- #{name}: {channel_id}")
    lines.extend([
        "",
        "## Operating Notes",
        "",
        "- General department and inbox channels accept employee messages; unsolicited messages are saved to the reference inbox.",
        "- Locked Strategic Staff, COO Administration, and Cockpits rooms are for higher-trust work.",
        "- Admin COO controls are restricted to coo-admin, coo-config, and coo-audit.",
        "- Human messages reach the COO agent only when they reply to a COO bot message; admin rooms additionally require mentioning the bot.",
        "- Daily per-channel transcripts live under reference/transcripts and include clear sender names.",
        "- Saved reference messages are tracked as pending, queued, attended, or failed in the inbox attention queue.",
        "- The inbox monitor periodically queues pending/failed reference messages for COO attention.",
        "- `/coo cockpit` opens the Discord-native cockpit panel. Admin buttons appear only in admin rooms.",
        "- `/coo queue` shows unattended/queued inbox messages; `/coo review` immediately queues pending inbox messages for attention.",
        "- Use Discord messages for workplace output; the tmux Codex/Claude pane remains headless backend state.",
    ])
    path = workdir / "DISCORD_ENVIRONMENT.md"
    path.write_text("\n".join(lines) + "\n")
    return path


def main() -> int:
    secrets_file = Path(os.environ.get("DISCORD_COO_SECRETS_FILE", str(DEFAULT_SECRETS)))
    env = {**load_env_file(secrets_file), **os.environ}
    token = env.get("DISCORD_CLAUDEX_BOT_TOKEN")
    guild_id = env.get("DISCORD_COO_GUILD_ID")
    application_id = env.get("DISCORD_CLAUDEX_APPLICATION_ID", "1499160835698855966")
    workdir = Path(env.get("DISCORD_COO_WORKDIR", str(DEFAULT_WORKDIR)))
    if not token or not guild_id:
        print("Missing DISCORD_CLAUDEX_BOT_TOKEN or DISCORD_COO_GUILD_ID", file=sys.stderr)
        return 2

    channels = request_json("GET", f"/guilds/{guild_id}/channels", token)
    if not isinstance(channels, list):
        raise RuntimeError("Discord channel list response was not a list")

    created: list[str] = []
    category_ids: dict[str, str] = {}
    text_ids: dict[str, str] = {}

    try:
        for index, group in enumerate(LAYOUT, start=10):
            category_name = group["category"]
            access = group.get("access")
            overwrites = permission_overwrites(env, token, access)
            category = find_category(channels, category_name)
            if category is None:
                body = {
                    "name": category_name,
                    "type": 4,
                    "position": index,
                }
                if overwrites is not None:
                    body["permission_overwrites"] = overwrites
                category = request_json("POST", f"/guilds/{guild_id}/channels", token, body)
                channels.append(category)
                created.append(f"category:{category_name}")
            elif overwrites is not None and not overwrites_match(category.get("permission_overwrites"), overwrites):
                category = request_json("PATCH", f"/channels/{category['id']}", token, {
                    "permission_overwrites": overwrites,
                })
            category_id = str(category["id"])
            category_ids[category_name] = category_id

            for offset, (channel_name, topic) in enumerate(group["channels"]):
                channel = find_text_channel(channels, channel_name, category_id)
                if channel is None:
                    body = {
                        "name": channel_name,
                        "type": 0,
                        "parent_id": category_id,
                        "position": offset,
                        "topic": topic,
                    }
                    if overwrites is not None:
                        body["permission_overwrites"] = overwrites
                    channel = request_json("POST", f"/guilds/{guild_id}/channels", token, body)
                    channels.append(channel)
                    created.append(f"text:{channel_name}")
                elif overwrites is not None and not overwrites_match(channel.get("permission_overwrites"), overwrites):
                    channel = request_json("PATCH", f"/channels/{channel['id']}", token, {
                        "parent_id": category_id,
                        "topic": topic,
                        "permission_overwrites": overwrites,
                    })
                text_ids[channel_name] = str(channel["id"])
    except RuntimeError as exc:
        if "Missing Permissions" in str(exc):
            print("Discord refused channel creation or locked-room permission update.", file=sys.stderr)
            print("For locked rooms, Discord may require Manage Channels plus Manage Roles.", file=sys.stderr)
            print("Reinstall/authorize with this URL, then rerun bootstrap_discord_env.py:", file=sys.stderr)
            print(install_url(application_id), file=sys.stderr)
            return 13
        raise

    existing_general = next((str(c["id"]) for c in channels if c.get("type") == 0 and c.get("name") == "general"), "")
    watched = [cid for cid in [existing_general, *text_ids.values()] if cid]
    home_channel_id = text_ids.get("coo-cockpit") or existing_general

    admin_channel_ids = [
        cid for cid in [
            text_ids.get("coo-admin", ""),
            text_ids.get("coo-config", ""),
            text_ids.get("coo-audit", ""),
        ] if cid
    ]

    update_env_file(secrets_file, {
        "DISCORD_COO_HOME_CHANNEL_ID": home_channel_id,
        "DISCORD_COO_CHANNEL_IDS": ",".join(dict.fromkeys(watched)),
        "DISCORD_COO_ADMIN_CHANNEL_IDS": ",".join(admin_channel_ids),
    })

    state = load_state(DEFAULT_STATE)
    state["home_channel_id"] = home_channel_id
    state["watched_channel_ids"] = list(dict.fromkeys(watched))
    state["discord_channel_layout"] = {
        "guild_id": guild_id,
        "categories": category_ids,
        "text_channels": text_ids,
    }
    env_doc = write_environment_doc(workdir, state["discord_channel_layout"], home_channel_id, admin_channel_ids, watched)
    state["discord_environment_doc"] = str(env_doc)
    save_state(DEFAULT_STATE, state)

    CHANNEL_MAP.write_text(json.dumps(state["discord_channel_layout"], indent=2, sort_keys=True) + "\n")
    print(json.dumps({
        "created": created,
        "home_channel_id": home_channel_id,
        "watched_channel_count": len(watched),
        "channel_map": str(CHANNEL_MAP),
        "environment_doc": str(env_doc),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
