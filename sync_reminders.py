#!/usr/bin/env python3
"""
Rebuild the seven `Training - <Day>` iOS Reminders lists from WEEKLY_PROGRAM.

Single source of truth: app.py WEEKLY_PROGRAM dict.
Run locally (needs osascript + Reminders.app):
    python sync_reminders.py

Idempotent: clears and recreates each `Training - *` list every run. Touches
only lists matching that prefix — other Reminders lists are left alone.
"""
import subprocess
import sys

from app import WEEKLY_PROGRAM

LIST_PREFIX = "Training"
DAYS = [("MON", "Mon"), ("TUE", "Tue"), ("WED", "Wed"), ("THU", "Thu"),
        ("FRI", "Fri"), ("SAT", "Sat"), ("SUN", "Sun")]


def osa(script: str) -> str:
    """Run an AppleScript snippet and return stdout (or raise on error)."""
    r = subprocess.run(["osascript", "-e", script],
                       capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"osascript failed:\n  script: {script}\n  err: {r.stderr.strip()}")
    return r.stdout.strip()


def _q(s: str) -> str:
    """Escape a string for inclusion inside AppleScript double quotes."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def ensure_list(name: str):
    osa(f'tell application "Reminders" to '
        f'if not (exists list "{_q(name)}") then '
        f'make new list with properties {{name:"{_q(name)}"}}')


def clear_list(name: str):
    osa(f'tell application "Reminders" to '
        f'tell list "{_q(name)}" to delete every reminder')


def add_reminder(list_name: str, title: str, body: str):
    osa(f'tell application "Reminders" to '
        f'tell list "{_q(list_name)}" to '
        f'make new reminder with properties '
        f'{{name:"{_q(title)}", body:"{_q(body)}"}}')


def sync():
    print(f"Syncing WEEKLY_PROGRAM → '{LIST_PREFIX} - <Day>' Reminders lists")
    total = 0
    for key, label in DAYS:
        block = WEEKLY_PROGRAM.get(key, {"exercises": []})
        name = f"{LIST_PREFIX} - {label}"
        ensure_list(name)
        clear_list(name)
        for ex in block.get("exercises", []):
            title = f'{ex["name"]} — {ex["scheme"]}'
            body = ex.get("notes", "")
            add_reminder(name, title, body)
        n = len(block.get("exercises", []))
        total += n
        print(f"  {name}: {n} items")
    print(f"Done. {total} reminders across {len(DAYS)} lists.")


if __name__ == "__main__":
    sync()
