#!/usr/bin/env python3
"""
One-time / re-runnable populate script for the Fat Dog Google Sheet.

Reads Fat_Dog_2026_Training_Plan.md (the source of truth for the *plan*) and
pushes every dated row into the `Plan` tab of the Google Sheet, using ISO dates
(YYYY-MM-DD) so the app can match days reliably.

It also:
  - ensures a `Config` tab exists (key/value store for Strava OAuth tokens)
  - migrates tokens from ../.strava_tokens.json into Config on first run
    (only if Config doesn't already have an access_token)

Run:
  python populate_sheet.py                 # full refresh of Plan + ensure Config
  python populate_sheet.py --plan-only     # only rewrite the Plan tab
  python populate_sheet.py --dry-run       # show what would be written

Existing actuals in the sheet are PRESERVED by default: if a row in the sheet
already has actual data and the markdown does not, the sheet value wins. Use
--force-plan to let the markdown overwrite everything.
"""
import argparse
import json
import re
import sys
from datetime import datetime, date
from pathlib import Path

from google.oauth2 import service_account
from googleapiclient.discovery import build

# ── Paths & config ──────────────────────────────────────────────────────────
HERE          = Path(__file__).parent
PROJECT_DIR   = HERE.parent                       # the "Fat Dog 120 - Training Plan" folder
PLAN_FILE     = PROJECT_DIR / "Fat_Dog_2026_Training_Plan.md"
TOKENS_FILE   = PROJECT_DIR / ".strava_tokens.json"
CREDS_FILE    = PROJECT_DIR / "sudheers-training-6e0b92f73bc7.json"

SHEET_ID      = "1eUiLCexi_GIw_KVeliambvQLSLM07IABR6sH9PkxLxM"
PLAN_TAB      = "Plan"
CONFIG_TAB    = "Config"
SCOPES        = ["https://www.googleapis.com/auth/spreadsheets"]

# Markdown table column indices (0-based, pipe-separated)
MD_DATE, MD_DAY, MD_PHASE, MD_TYPE, MD_DESC, MD_PLANNED, MD_CROSS, \
    MD_ACT_MILES, MD_ACT_ELEV, MD_ACT_TIME, MD_NOTES = range(11)

HEADER = ["Date", "Day", "Phase", "Workout Type", "Description",
          "Planned Miles", "Cross-Train", "Actual Miles", "Actual Elev",
          "Actual Time", "Notes"]

# ── Markdown parsing (mirrors strava_sync.py) ─────────────────────────────────

def parse_table_row(line):
    if not line.startswith("|"):
        return None
    cells = [c.strip() for c in line.split("|")]
    return cells[1:-1] if len(cells) > 2 else None


def parse_plan_date(raw):
    raw = raw.strip().strip("*")
    for fmt in ("%b %d", "%B %d"):
        try:
            d = datetime.strptime(raw, fmt)
            return date(2026, d.month, d.day)
        except ValueError:
            continue
    return None


def clean(cell):
    """Strip markdown bold/emphasis markers used for emphasis in the table."""
    return cell.replace("**", "").strip()


def read_plan_rows():
    rows = []
    for line in PLAN_FILE.read_text(encoding="utf-8").splitlines():
        cells = parse_table_row(line)
        if not cells or len(cells) < 11:
            continue
        d = parse_plan_date(cells[MD_DATE])
        if not d:
            continue
        rows.append([
            d.isoformat(),
            clean(cells[MD_DAY]),
            clean(cells[MD_PHASE]),
            clean(cells[MD_TYPE]),
            cells[MD_DESC].strip(),          # keep description formatting as-is
            clean(cells[MD_PLANNED]),
            clean(cells[MD_CROSS]),
            clean(cells[MD_ACT_MILES]),
            clean(cells[MD_ACT_ELEV]),
            clean(cells[MD_ACT_TIME]),
            cells[MD_NOTES].strip(),
        ])
    rows.sort(key=lambda r: r[0])
    return rows

# ── Sheets helpers ────────────────────────────────────────────────────────────

def get_service():
    creds = service_account.Credentials.from_service_account_file(
        str(CREDS_FILE), scopes=SCOPES)
    return build("sheets", "v4", credentials=creds)


def get_tab_map(svc):
    meta = svc.spreadsheets().get(spreadsheetId=SHEET_ID).execute()
    return {s["properties"]["title"]: s["properties"]["sheetId"]
            for s in meta["sheets"]}


def ensure_tabs(svc):
    tabs = get_tab_map(svc)
    requests = []
    # Rename a lone default "Sheet1" to Plan, else add Plan if missing.
    if PLAN_TAB not in tabs:
        if "Sheet1" in tabs and len(tabs) == 1:
            requests.append({"updateSheetProperties": {
                "properties": {"sheetId": tabs["Sheet1"], "title": PLAN_TAB},
                "fields": "title"}})
        else:
            requests.append({"addSheet": {"properties": {"title": PLAN_TAB}}})
    if CONFIG_TAB not in tabs:
        requests.append({"addSheet": {"properties": {"title": CONFIG_TAB}}})
    if requests:
        svc.spreadsheets().batchUpdate(
            spreadsheetId=SHEET_ID, body={"requests": requests}).execute()
    return get_tab_map(svc)


def read_existing_actuals(svc):
    """Map ISO date -> [miles, elev, time, notes] for rows already in the sheet."""
    try:
        res = svc.spreadsheets().values().get(
            spreadsheetId=SHEET_ID, range=f"{PLAN_TAB}!A2:K1000").execute()
    except Exception:
        return {}
    out = {}
    for row in res.get("values", []):
        if not row:
            continue
        row = row + [""] * (11 - len(row))
        date_key = row[0].strip()
        actuals = [row[7].strip(), row[8].strip(), row[9].strip(), row[10].strip()]
        if any(actuals):
            out[date_key] = actuals
    return out


def write_plan(svc, rows, preserve_actuals):
    if preserve_actuals:
        existing = read_existing_actuals(svc)
        for r in rows:
            has_md_actual = any(r[7:11])
            if not has_md_actual and r[0] in existing:
                r[7:11] = existing[r[0]]
    body = {"values": [HEADER] + rows}
    svc.spreadsheets().values().update(
        spreadsheetId=SHEET_ID,
        range=f"{PLAN_TAB}!A1",
        valueInputOption="RAW",
        body=body).execute()


def seed_config(svc, force=False):
    """Migrate Strava tokens into the Config tab if not already present."""
    res = svc.spreadsheets().values().get(
        spreadsheetId=SHEET_ID, range=f"{CONFIG_TAB}!A1:B50").execute()
    existing = {r[0]: (r[1] if len(r) > 1 else "")
                for r in res.get("values", []) if r}

    if existing.get("access_token") and not force:
        print("  Config already has Strava tokens — leaving as-is.")
        return

    if not TOKENS_FILE.exists():
        print("  No .strava_tokens.json found — skipping token seed.")
        return

    tok = json.loads(TOKENS_FILE.read_text())
    values = [
        ["key", "value"],
        ["access_token", tok.get("access_token", "")],
        ["refresh_token", tok.get("refresh_token", "")],
        ["expires_at", str(tok.get("expires_at", 0))],
    ]
    svc.spreadsheets().values().update(
        spreadsheetId=SHEET_ID,
        range=f"{CONFIG_TAB}!A1",
        valueInputOption="RAW",
        body={"values": values}).execute()
    print("  Seeded Config tab with Strava tokens.")

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan-only", action="store_true", help="only rewrite Plan tab")
    ap.add_argument("--dry-run", action="store_true", help="print, don't write")
    ap.add_argument("--force-plan", action="store_true",
                    help="let markdown overwrite existing sheet actuals")
    ap.add_argument("--force-config", action="store_true",
                    help="re-seed Strava tokens even if Config has them")
    args = ap.parse_args()

    rows = read_plan_rows()
    print(f"Parsed {len(rows)} dated rows from {PLAN_FILE.name}")
    print(f"  Range: {rows[0][0]} → {rows[-1][0]}")

    if args.dry_run:
        for r in rows[:3] + rows[-3:]:
            print("   ", r[0], r[1], r[3][:40])
        print("Dry run — nothing written.")
        return

    svc = get_service()
    ensure_tabs(svc)
    write_plan(svc, rows, preserve_actuals=not args.force_plan)
    print(f"  Wrote {len(rows)} rows to '{PLAN_TAB}' tab.")

    if not args.plan_only:
        seed_config(svc, force=args.force_config)

    print("Done.")


if __name__ == "__main__":
    main()
