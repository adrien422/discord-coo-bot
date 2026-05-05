#!/usr/bin/env python3
"""Dev-gate doc-change proposal helper for the Discord COO agent.

Stage 1 contract: the agent must NOT write or edit files in the workspace, skill
directories, or company-map without first proposing the change to the developers
(Adrien + Dan) and then the owner. This script sends the proposal as a DM to each
required reviewer and records the proposal under the bot state directory.

Usage:
  propose_doc_change.py --path PATH --summary TEXT [--body-file FILE] [--diff-file FILE]

The script:
  1. Reads the bot token + reviewer user IDs from the secrets env.
  2. Opens a DM channel to each developer and owner.
  3. Sends the proposal text.
  4. Appends a JSON record under state_dir/proposals/<id>.json.

Approvals are observed by the bot: both developers must DM 'approve <id>' and then
the owner must DM 'approve <id>' before the agent should perform the write.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DISCORD_API = "https://discord.com/api/v10"
STATE_DIR = Path(os.environ.get("DISCORD_COO_STATE_DIR", "/home/arman/workbench/.discord_coo_state"))
PROPOSALS_DIR = STATE_DIR / "proposals"


def _env_required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        print(f"missing env var: {name}", file=sys.stderr)
        sys.exit(2)
    return value


def _id_set(name: str) -> list[str]:
    return [c.strip() for c in os.environ.get(name, "").split(",") if c.strip()]


def _api(method: str, route: str, body: dict | None, token: str) -> dict:
    req = urllib.request.Request(
        DISCORD_API + route,
        method=method,
        headers={
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
            "User-Agent": "ClaudexDiscordCOO/proposal/0.1",
        },
        data=(json.dumps(body).encode("utf-8") if body else None),
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = resp.read().decode("utf-8") or "{}"
            return json.loads(payload) if payload.strip() else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        raise SystemExit(f"discord {method} {route} failed {exc.code}: {detail}")


def _open_dm(user_id: str, token: str) -> str:
    data = _api("POST", "/users/@me/channels", {"recipient_id": user_id}, token)
    return str(data.get("id") or "")


def _send(channel_id: str, content: str, token: str) -> None:
    _api("POST", f"/channels/{channel_id}/messages", {"content": content}, token)


def main() -> None:
    parser = argparse.ArgumentParser(description="File a doc-change proposal to the dev gate.")
    parser.add_argument("--path", required=True, help="Target file path (relative or absolute).")
    parser.add_argument("--summary", required=True, help="One-line summary of the change.")
    parser.add_argument("--body-file", help="Optional file containing the proposed full content.")
    parser.add_argument("--diff-file", help="Optional file containing a unified diff against the current file.")
    args = parser.parse_args()

    token = _env_required("DISCORD_CLAUDEX_BOT_TOKEN")
    devs = _id_set("DISCORD_COO_DEV_USER_IDS")
    owner = os.environ.get("DISCORD_COO_OWNER_USER_ID", "").strip()
    if not devs:
        sys.exit("DISCORD_COO_DEV_USER_IDS is empty; cannot file a dev-gate proposal.")

    PROPOSALS_DIR.mkdir(parents=True, exist_ok=True)
    proposal_id = f"prop-{int(time.time())}-{os.getpid()}"
    record = {
        "id": proposal_id,
        "path": args.path,
        "summary": args.summary,
        "created_at": time.time(),
        "dev_ids": devs,
        "owner_id": owner,
        "approvals": {},
        "status": "pending",
    }
    if args.body_file:
        record["body"] = Path(args.body_file).read_text()
    if args.diff_file:
        record["diff"] = Path(args.diff_file).read_text()

    # Trim very large bodies for DM delivery; agent retains the full version on disk.
    body_excerpt = (record.get("body") or "")[:1500]
    diff_excerpt = (record.get("diff") or "")[:1500]

    dev_message = (
        f"**Doc-change proposal `{proposal_id}` — DEV gate**\n"
        f"Path: `{args.path}`\n"
        f"Summary: {args.summary}\n"
        + (f"\n**Diff (excerpt):**\n```\n{diff_excerpt}\n```\n" if diff_excerpt else "")
        + (f"\n**Body (excerpt):**\n```\n{body_excerpt}\n```\n" if body_excerpt and not diff_excerpt else "")
        + f"\nReply `approve {proposal_id}` or `reject {proposal_id} <reason>`."
    )
    owner_message = (
        f"**Doc-change proposal `{proposal_id}` — OWNER content review (pending dev green-light)**\n"
        f"Path: `{args.path}`\n"
        f"Summary: {args.summary}\n"
        + (f"\n**Body (excerpt):**\n```\n{body_excerpt}\n```\n" if body_excerpt else "")
        + f"\nThe two developers must approve this first; once they do, please reply "
        f"`approve {proposal_id}` to authorise the write."
    )

    delivery: dict[str, str] = {}
    for dev_id in devs:
        ch = _open_dm(dev_id, token)
        if ch:
            _send(ch, dev_message, token)
            delivery[dev_id] = ch
    if owner:
        ch = _open_dm(owner, token)
        if ch:
            _send(ch, owner_message, token)
            delivery[owner] = ch

    record["delivery"] = delivery
    (PROPOSALS_DIR / f"{proposal_id}.json").write_text(json.dumps(record, indent=2))
    print(proposal_id)


if __name__ == "__main__":
    main()
