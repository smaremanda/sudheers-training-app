#!/usr/bin/env python3
"""
Strava → Google Sheets sync.

Unlike the original strava_sync.py (which read/wrote a local markdown file and a
.strava_tokens.json file), this version is stateless on disk:

  - OAuth tokens live in the `Config` tab of the sheet (key/value rows).
  - Actuals are written into the `Plan` tab (columns H:K) for empty rows only.

This lets the sync run on Railway, where there is no persistent filesystem.
The access token is refreshed as needed and the new token written back to Config.
"""
import json
from datetime import datetime, date, timedelta
from urllib.parse import urlencode
from urllib.request import urlopen, Request
from urllib.error import HTTPError

STRAVA_TOKEN_URL = "https://www.strava.com/oauth/token"
STRAVA_API_BASE  = "https://www.strava.com/api/v3"

# Sheet Plan-tab column indices (0-based)
C_DATE, C_ACT_MILES, C_ACT_ELEV, C_ACT_TIME, C_NOTES = 0, 7, 8, 9, 10


# ── Config tab token store ────────────────────────────────────────────────────
def _read_config(svc, sheet_id, config_tab):
    res = svc.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"{config_tab}!A1:B50").execute()
    cfg = {}
    for row in res.get("values", []):
        if row and len(row) >= 2:
            cfg[row[0].strip()] = row[1].strip()
    return cfg


def _write_tokens(svc, sheet_id, config_tab, access_token, refresh_token, expires_at):
    values = [
        ["key", "value"],
        ["access_token", access_token],
        ["refresh_token", refresh_token],
        ["expires_at", str(expires_at)],
    ]
    svc.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"{config_tab}!A1",
        valueInputOption="RAW",
        body={"values": values}).execute()


def _refresh(svc, sheet_id, config_tab, cfg, client_id, client_secret):
    data = urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "refresh_token",
        "refresh_token": cfg["refresh_token"],
    }).encode()
    req = Request(STRAVA_TOKEN_URL, data=data, method="POST")
    with urlopen(req) as resp:
        tok = json.loads(resp.read())
    _write_tokens(svc, sheet_id, config_tab,
                  tok["access_token"], tok["refresh_token"], tok["expires_at"])
    return tok["access_token"]


def _valid_access_token(svc, sheet_id, config_tab, client_id, client_secret):
    cfg = _read_config(svc, sheet_id, config_tab)
    if not cfg.get("refresh_token"):
        raise RuntimeError("No Strava refresh_token in Config tab — run auth first.")
    try:
        expires_at = int(cfg.get("expires_at", "0"))
    except ValueError:
        expires_at = 0
    if expires_at < datetime.utcnow().timestamp() + 60:
        return _refresh(svc, sheet_id, config_tab, cfg, client_id, client_secret)
    return cfg["access_token"]


# ── Strava API ────────────────────────────────────────────────────────────────
def _fetch_activities(access_token, after, before):
    after_ts  = int(datetime(after.year, after.month, after.day).timestamp())
    before_ts = int(datetime(before.year, before.month, before.day, 23, 59, 59).timestamp())
    out, page = [], 1
    headers = {"Authorization": f"Bearer {access_token}"}
    while True:
        url = (f"{STRAVA_API_BASE}/athlete/activities"
               f"?after={after_ts}&before={before_ts}&per_page=100&page={page}")
        try:
            with urlopen(Request(url, headers=headers)) as resp:
                batch = json.loads(resp.read())
        except HTTPError as e:
            raise RuntimeError(f"Strava API {e.code}: {e.read().decode()[:200]}")
        if not batch:
            break
        out.extend(batch)
        page += 1
    return out


def _activity_description(access_token, activity_id):
    url = f"{STRAVA_API_BASE}/activities/{activity_id}"
    try:
        with urlopen(Request(url, headers={"Authorization": f"Bearer {access_token}"})) as resp:
            return (json.loads(resp.read()).get("description") or "").strip()
    except HTTPError:
        return ""


# ── Conversions ───────────────────────────────────────────────────────────────
def _miles(m):  return round(m / 1609.344, 2)
def _ft(m):     return int(m * 3.28084)
def _min(s):    return round(s / 60)


# ── Main sync ─────────────────────────────────────────────────────────────────
def sync(svc, sheet_id, plan_tab, config_tab, client_id, client_secret, days=7):
    """Pull the last `days` of activities, fill empty Plan rows. Returns count updated."""
    if not client_id or not client_secret:
        raise RuntimeError("Missing STRAVA_CLIENT_ID / STRAVA_CLIENT_SECRET.")

    access_token = _valid_access_token(svc, sheet_id, config_tab, client_id, client_secret)

    today = date.today()
    after = today - timedelta(days=days)
    activities = _fetch_activities(access_token, after, today)

    # Group by local date, aggregate same-day efforts.
    by_date = {}
    for a in activities:
        d = datetime.fromisoformat(a["start_date_local"].replace("Z", "")).date()
        by_date.setdefault(d, []).append(a)

    # Read current Plan rows.
    res = svc.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"{plan_tab}!A2:K1000").execute()
    rows = res.get("values", [])

    updates = []   # batched value updates
    updated = 0
    for i, row in enumerate(rows):
        row = list(row) + [""] * (11 - len(row))
        try:
            d = datetime.strptime(row[C_DATE].strip(), "%Y-%m-%d").date()
        except ValueError:
            continue
        if d not in by_date:
            continue
        # Only fill empty rows (don't clobber manual logs / earlier syncs).
        if row[C_ACT_MILES].strip() not in ("", "-"):
            continue

        day_acts = by_date[d]
        miles = sum(_miles(a.get("distance", 0)) for a in day_acts)
        elev  = sum(_ft(a.get("total_elevation_gain", 0)) for a in day_acts)
        mins  = sum(_min(a.get("moving_time", 0)) for a in day_acts)

        parts = []
        for a in day_acts:
            if a.get("name"):
                parts.append(a["name"].strip())
            desc = _activity_description(access_token, a["id"])
            if desc:
                parts.append(desc)
        note = " — ".join(parts)
        note = " ".join(note.splitlines()).strip()

        existing_note = row[C_NOTES].strip()
        final_note = existing_note if existing_note and existing_note != "-" else note

        sheet_row = i + 2  # header offset
        updates.append({
            "range": f"{plan_tab}!H{sheet_row}:K{sheet_row}",
            "values": [[str(miles), str(elev), str(mins), final_note]],
        })
        updated += 1

    if updates:
        svc.spreadsheets().values().batchUpdate(
            spreadsheetId=sheet_id,
            body={"valueInputOption": "USER_ENTERED", "data": updates}).execute()

    return updated
