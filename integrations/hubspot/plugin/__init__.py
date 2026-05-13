"""HubSpot integration plugin.

Pulls deals + contacts via the HubSpot CRM v3 API and writes them to
per-tenant hubspot_* tables. Computes a small set of metric_values
('pipeline_value', 'open_deal_count', 'mean_deal_size') so the
monthly-review cadence has real numbers.

Requires the operator to set up a HubSpot Developer Portal app and
complete the OAuth handshake via the enable wizard. Tokens are
auto-refreshed by the plugin when expiry is near.
"""
import json
import sqlite3
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

TOKEN_URL = "https://api.hubapi.com/oauth/v1/token"
DEALS_URL = "https://api.hubapi.com/crm/v3/objects/deals"
CONTACTS_URL = "https://api.hubapi.com/crm/v3/objects/contacts"


def oauth_url(client_id: str, redirect_uri: str, state: str) -> str:
    manifest = json.load(open(Path(__file__).parent.parent / "manifest.json"))
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": " ".join(manifest["oauth"]["scopes"]),
        "state": state,
    }
    return f"{manifest['oauth']['authorize_url']}?{urllib.parse.urlencode(params)}"


def exchange_code(client_id: str, client_secret: str, redirect_uri: str, code: str) -> dict:
    data = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "code": code,
    }).encode()
    req = urllib.request.Request(
        TOKEN_URL, data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req) as resp:
        body = json.loads(resp.read())
    return {
        "access_token": body["access_token"],
        "refresh_token": body["refresh_token"],
        "expires_at": int(time.time()) + int(body["expires_in"]),
        "client_id": client_id,
        "client_secret": client_secret,
    }


def refresh(creds: dict) -> dict:
    data = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "client_id": creds["client_id"],
        "client_secret": creds["client_secret"],
        "refresh_token": creds["refresh_token"],
    }).encode()
    req = urllib.request.Request(
        TOKEN_URL, data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req) as resp:
        body = json.loads(resp.read())
    creds = dict(creds)
    creds["access_token"] = body["access_token"]
    creds["expires_at"] = int(time.time()) + int(body["expires_in"])
    if "refresh_token" in body:
        creds["refresh_token"] = body["refresh_token"]
    return creds


def _ensure_fresh(creds: dict) -> dict:
    """Refresh access_token if it expires within 60s."""
    if int(creds.get("expires_at", 0)) - int(time.time()) < 60:
        return refresh(creds)
    return creds


def _api_get(url: str, token: str, params: dict | None = None) -> dict:
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def sync(tenant_db: str, team_slug: str, creds: dict) -> dict:
    """Pull deals + contacts, upsert into per-tenant tables, write metric values."""
    creds = _ensure_fresh(creds)
    token = creds["access_token"]

    deals_body = _api_get(DEALS_URL, token, {
        "limit": 100,
        "properties": "dealname,amount,dealstage,closedate",
    })
    contacts_body = _api_get(CONTACTS_URL, token, {
        "limit": 100,
        "properties": "email,firstname,lastname",
    })

    conn = sqlite3.connect(tenant_db)
    conn.row_factory = sqlite3.Row
    deals = deals_body.get("results", [])
    contacts = contacts_body.get("results", [])
    pipeline_value = 0.0
    open_deal_count = 0
    closed_won_amounts: list[float] = []
    try:
        with conn:
            for d in deals:
                props = d.get("properties", {})
                amount = float(props.get("amount") or 0.0)
                stage = props.get("dealstage") or ""
                conn.execute(
                    "INSERT INTO hubspot_deals (hubspot_id, name, amount, stage, close_date, last_synced_at) "
                    "VALUES (?, ?, ?, ?, ?, datetime('now')) "
                    "ON CONFLICT(hubspot_id) DO UPDATE SET "
                    "  name = excluded.name, amount = excluded.amount, "
                    "  stage = excluded.stage, close_date = excluded.close_date, "
                    "  last_synced_at = excluded.last_synced_at",
                    (d["id"], props.get("dealname"), amount, stage, props.get("closedate")),
                )
                low = stage.lower()
                if "closedwon" in low.replace(" ", ""):
                    closed_won_amounts.append(amount)
                elif "closedlost" not in low.replace(" ", ""):
                    pipeline_value += amount
                    open_deal_count += 1

            for c in contacts:
                props = c.get("properties", {})
                conn.execute(
                    "INSERT INTO hubspot_contacts (hubspot_id, email, first_name, last_name, last_synced_at) "
                    "VALUES (?, ?, ?, ?, datetime('now')) "
                    "ON CONFLICT(hubspot_id) DO UPDATE SET "
                    "  email = excluded.email, first_name = excluded.first_name, "
                    "  last_name = excluded.last_name, last_synced_at = excluded.last_synced_at",
                    (c["id"], props.get("email"), props.get("firstname"), props.get("lastname")),
                )

            now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            mean_size = (
                sum(closed_won_amounts) / len(closed_won_amounts)
                if closed_won_amounts else 0.0
            )
            for slug, value, note in (
                ("pipeline_value", pipeline_value, f"team={team_slug}"),
                ("open_deal_count", float(open_deal_count), f"team={team_slug}"),
                ("mean_deal_size", mean_size, f"team={team_slug} (closed-won)"),
            ):
                existing = conn.execute(
                    "SELECT id FROM metrics WHERE slug = ?", (slug,)
                ).fetchone()
                if existing is None:
                    conn.execute(
                        "INSERT INTO metrics (slug, name, scope_kind, unit, source_app) "
                        "VALUES (?, ?, 'team', '$', 'hubspot')",
                        (slug, slug.replace("_", " ").title()),
                    )
                metric_id = conn.execute(
                    "SELECT id FROM metrics WHERE slug = ?", (slug,)
                ).fetchone()["id"]
                conn.execute(
                    "INSERT INTO metric_values (metric_id, observed_at, value, note) "
                    "VALUES (?, ?, ?, ?)",
                    (metric_id, now, value, note),
                )
    finally:
        conn.close()

    return {
        "deals_synced": len(deals),
        "contacts_synced": len(contacts),
        "pipeline_value": pipeline_value,
        "open_deal_count": open_deal_count,
        "creds_refreshed": creds,
    }


def create_note(creds: dict, *, deal_id: str, body: str) -> dict:
    creds = _ensure_fresh(creds)
    data = json.dumps({
        "properties": {"hs_note_body": body, "hs_timestamp": int(time.time() * 1000)},
        "associations": [{
            "to": {"id": deal_id},
            "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 214}],
        }],
    }).encode()
    req = urllib.request.Request(
        "https://api.hubapi.com/crm/v3/objects/notes",
        data=data, method="POST",
        headers={
            "Authorization": f"Bearer {creds['access_token']}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def update_deal_stage(creds: dict, *, deal_id: str, stage: str) -> dict:
    creds = _ensure_fresh(creds)
    data = json.dumps({"properties": {"dealstage": stage}}).encode()
    req = urllib.request.Request(
        f"https://api.hubapi.com/crm/v3/objects/deals/{deal_id}",
        data=data, method="PATCH",
        headers={
            "Authorization": f"Bearer {creds['access_token']}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())
