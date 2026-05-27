#!/usr/bin/env python3
"""
Fat Dog 120 Training Dashboard — Flask + Google Sheets backend.

Data model (Google Sheet, `Plan` tab, header in row 1, data from row 2):
  A Date (ISO)  B Day  C Phase  D Workout Type  E Description
  F Planned Miles  G Cross-Train  H Actual Miles  I Actual Elev
  J Actual Time  K Notes

`Config` tab is a key/value store (A=key, B=value) holding Strava OAuth tokens.

Env vars:
  GOOGLE_CREDENTIALS  service-account JSON (string). Falls back to local file.
  SHEET_ID            spreadsheet id (defaults to the Fat Dog sheet)
  APP_PIN             if set, the app requires this PIN to access
  SECRET_KEY          Flask session secret
  STRAVA_CLIENT_ID / STRAVA_CLIENT_SECRET   Strava app credentials
"""
import os
import json
from datetime import datetime, timedelta, date
from functools import wraps
from pathlib import Path

from flask import Flask, send_from_directory, jsonify, request, session
from google.oauth2 import service_account
from googleapiclient.discovery import build
from dotenv import load_dotenv

import strava_sheets

HERE = Path(__file__).parent
load_dotenv(HERE / ".env")
# Also pick up Strava client creds from the project .env one level up
load_dotenv(HERE.parent / ".env")

app = Flask(__name__, static_folder=".", static_url_path="")
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-in-prod")
app.permanent_session_lifetime = timedelta(days=30)

APP_PIN     = os.environ.get("APP_PIN", "")
SHEET_ID    = os.environ.get("SHEET_ID", "1eUiLCexi_GIw_KVeliambvQLSLM07IABR6sH9PkxLxM")
PLAN_TAB    = "Plan"
CONFIG_TAB  = "Config"
SCOPES      = ["https://www.googleapis.com/auth/spreadsheets"]
LOCAL_CREDS = HERE.parent / "sudheers-training-6b0b31ea0f48.json"

# Sheet column indices (0-based)
C_DATE, C_DAY, C_PHASE, C_TYPE, C_DESC, C_PLANNED, C_CROSS, \
    C_ACT_MILES, C_ACT_ELEV, C_ACT_TIME, C_NOTES = range(11)


# ── Auth ────────────────────────────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not APP_PIN:                       # no PIN configured → open access
            return f(*args, **kwargs)
        if not session.get("authenticated"):
            return jsonify({"error": "unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated


# ── Sheets plumbing ───────────────────────────────────────────────────────────
def get_service():
    creds_json = os.environ.get("GOOGLE_CREDENTIALS")
    if creds_json:
        info = json.loads(creds_json)
        creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    else:
        creds = service_account.Credentials.from_service_account_file(
            str(LOCAL_CREDS), scopes=SCOPES)
    return build("sheets", "v4", credentials=creds)


def get_all_rows():
    svc = get_service()
    res = svc.spreadsheets().values().get(
        spreadsheetId=SHEET_ID, range=f"{PLAN_TAB}!A2:K1000").execute()
    return res.get("values", [])


def row_to_dict(row, row_number):
    row = list(row) + [""] * (11 - len(row))
    return {
        "row_number":      row_number,           # 1-based sheet row (incl header)
        "date":            row[C_DATE].strip(),
        "day":             row[C_DAY].strip(),
        "phase":           row[C_PHASE].strip(),
        "type":            row[C_TYPE].strip(),
        "desc":            row[C_DESC].strip(),
        "planned_miles":   row[C_PLANNED].strip(),
        "cross":           row[C_CROSS].strip(),
        "actual_miles":    row[C_ACT_MILES].strip(),
        "actual_elev":     row[C_ACT_ELEV].strip(),
        "actual_time":     row[C_ACT_TIME].strip(),
        "notes":           row[C_NOTES].strip(),
    }


def week_window(anchor_iso=None):
    """Mon–Sun window containing the anchor date (defaults to today)."""
    anchor = datetime.strptime(anchor_iso, "%Y-%m-%d").date() if anchor_iso else date.today()
    monday = anchor - timedelta(days=anchor.weekday())
    return monday, monday + timedelta(days=6), anchor


# ── Routes ────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return send_from_directory(".", "index.html")


@app.route("/api/auth", methods=["POST"])
def api_auth():
    if not APP_PIN:
        return jsonify({"status": "ok"})
    data = request.json or {}
    if str(data.get("pin", "")).strip() == str(APP_PIN).strip():
        session.permanent = True
        session["authenticated"] = True
        return jsonify({"status": "ok"})
    return jsonify({"error": "wrong pin"}), 401


@app.route("/api/me")
def api_me():
    """Tells the frontend whether a PIN is required and whether we're authed."""
    return jsonify({
        "pin_required": bool(APP_PIN),
        "authenticated": (not APP_PIN) or bool(session.get("authenticated")),
    })


@app.route("/api/logout", methods=["POST"])
def api_logout():
    session.clear()
    return jsonify({"status": "ok"})


@app.route("/api/week")
@login_required
def api_week():
    monday, sunday, anchor = week_window(request.args.get("date"))
    rows = get_all_rows()
    week = []
    for i, row in enumerate(rows):
        if not row or not row[0].strip():
            continue
        try:
            d = datetime.strptime(row[0].strip(), "%Y-%m-%d").date()
        except ValueError:
            continue
        if monday <= d <= sunday:
            rec = row_to_dict(row, i + 2)
            rec["is_today"] = (d == anchor)
            rec["is_past"]  = (d < anchor)
            week.append(rec)
    week.sort(key=lambda r: r["date"])
    return jsonify({"week": week, "today": anchor.isoformat()})


@app.route("/api/log", methods=["POST"])
@login_required
def api_log():
    data = request.json or {}
    row_number = data.get("row_number")
    if not row_number:
        return jsonify({"error": "row_number required"}), 400

    miles = str(data.get("actual_miles", ""))
    elev  = str(data.get("actual_elev", ""))
    tmin  = str(data.get("actual_time", ""))
    notes = str(data.get("notes", ""))

    svc = get_service()
    svc.spreadsheets().values().update(
        spreadsheetId=SHEET_ID,
        range=f"{PLAN_TAB}!H{row_number}:K{row_number}",
        valueInputOption="USER_ENTERED",
        body={"values": [[miles, elev, tmin, notes]]},
    ).execute()
    return jsonify({"ok": True, "row": row_number})


@app.route("/api/strava-sync", methods=["POST"])
@login_required
def api_strava_sync():
    try:
        svc = get_service()
        updated = strava_sheets.sync(
            svc, SHEET_ID, PLAN_TAB, CONFIG_TAB,
            client_id=os.environ.get("STRAVA_CLIENT_ID", ""),
            client_secret=os.environ.get("STRAVA_CLIENT_SECRET", ""),
            days=7,
        )
        return jsonify({"ok": True, "updated": updated})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ── Strength workouts ──────────────────────────────────────────────────────────
ANCHOR = {
    "label": "Anchor — every session (~6–8 min)",
    "exercises": [
        {"name": "Cobras", "scheme": "3×20", "notes": "Spine warm-up. Press up, gentle extension."},
        {"name": "Left hip stretch", "scheme": "1×60 sec", "notes": "PT-prescribed. Left side only."},
        {"name": "Tibialis anterior raises", "scheme": "3×20 · 2-1-2", "notes": "Shin prevention. Add band when easy."},
        {"name": "Calf raises (straight knee)", "scheme": "3×15 · 3-1-3", "notes": "Gastroc. Add 5–10 lb when easy."},
        {"name": "Calf raises (bent knee)", "scheme": "3×15 · 3-1-3", "notes": "Soleus. Add 5–10 lb when easy."},
    ],
}

STRENGTH = {
    "MON": {
        "title": "Monday — Posterior Chain + Hips/Glutes",
        "duration": "~30 min",
        "exercises": [
            {"name": "Single-leg RDL", "scheme": "3×10 each · 2-1-2", "notes": "Hamstring hip-hinge. Light dumbbell."},
            {"name": "Step-ups (loaded)", "scheme": "3×10 each", "notes": "Glute/quad. Add weight or height."},
            {"name": "Single-leg glute bridge", "scheme": "3×12 each · 2-1-2", "notes": "Glute max. Slow, controlled."},
            {"name": "Hip abduction band walks", "scheme": "3×15 each dir", "notes": "Glute med / hip stability."},
            {"name": "Side plank", "scheme": "3×40 sec each", "notes": "Lateral core. Progress to 60 sec."},
            {"name": "Pallof press", "scheme": "3×12 each side · 2-1-2", "notes": "Anti-rotation core."},
        ],
    },
    "TUE": {
        "title": "Tuesday — Quads + Knee + Balance (Downhill Prep)",
        "duration": "~30 min",
        "exercises": [
            {"name": "Bulgarian split squat", "scheme": "3×8 each", "notes": "Main quad/glute load."},
            {"name": "Reverse Nordic", "scheme": "3×8 · slow", "notes": "Quad eccentric, knees-over-toes. Start bodyweight."},
            {"name": "Step-downs", "scheme": "3×10 each · 3-1-1", "notes": "Eccentric quad, downhill-specific. Lower slowly."},
            {"name": "Spanish squat / wall sit w/ heel raise", "scheme": "3×40 sec", "notes": "Isometric quad, knee-friendly."},
            {"name": "Pistol taps / MOBO board", "scheme": "3×8 each · or 2×60 sec", "notes": "Pick one. Single-leg balance."},
        ],
    },
    "THU": {
        "title": "Thursday — Mini Maintenance",
        "duration": "~15–20 min (after easy run)",
        "exercises": [
            {"name": "Single-leg glute bridge", "scheme": "3×10 each · 2-1-2", "notes": "Glute max."},
            {"name": "Banded clamshells", "scheme": "2×15 each", "notes": "Glute med."},
            {"name": "Reverse step-up / wall sit", "scheme": "2–3 sets", "notes": "One quad/knee touch."},
            {"name": "Side plank", "scheme": "2×30 sec each", "notes": "Core."},
        ],
    },
    "REHAB": {
        "title": "Pre-Run Rehab Warm-up",
        "duration": "10–12 min, before any run",
        "exercises": [
            {"name": "Ankle dorsiflexion wall drill", "scheme": "2×10 each leg", "notes": "Heel down, knee tracks over toe."},
            {"name": "Hip 90/90 mobility", "scheme": "2×60 sec each side", "notes": "Both sides."},
            {"name": "Banded clamshells", "scheme": "2×15 each side", "notes": "Glute med activation."},
            {"name": "Single-leg glute bridge", "scheme": "2×10 each", "notes": "Slow, controlled."},
            {"name": "Sciatic nerve floss", "scheme": "2×10 each leg", "notes": "Rest days only. Gentle, not a stretch."},
        ],
    },
}


@app.route("/api/strength/<session_key>")
@login_required
def api_strength(session_key):
    key = session_key.upper()
    if key not in STRENGTH:
        return jsonify({"error": f"unknown session: {session_key}"}), 404
    block = dict(STRENGTH[key])
    block["anchor"] = ANCHOR
    return jsonify(block)


# ── PWA ─────────────────────────────────────────────────────────────────────
@app.route("/manifest.json")
def manifest():
    return jsonify({
        "name": "Fat Dog 120 Training",
        "short_name": "Fat Dog",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#0d1117",
        "theme_color": "#e8843c",
        "icons": [
            {"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml"},
            {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
        ],
    })


@app.route("/sw.js")
def sw():
    from flask import Response
    return Response(
        "self.addEventListener('fetch', e => e.respondWith(fetch(e.request)));",
        mimetype="application/javascript")


if __name__ == "__main__":
    import socket
    try:
        ip = socket.gethostbyname(socket.gethostname())
    except Exception:
        ip = "your-mac-ip"
    print("\n  Fat Dog 120 Training Dashboard")
    print(f"    Local:  http://127.0.0.1:5002")
    print(f"    Phone:  http://{ip}:5002")
    print("\n    Ctrl+C to stop.\n")
    app.run(host="0.0.0.0", port=5002, debug=True)
