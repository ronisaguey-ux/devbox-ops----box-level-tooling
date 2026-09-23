#!/usr/bin/env python3
"""
OCULUS Progress Log Updater
===========================
Appends one row per day to the oculus_progress.db SQLite WAL journal:
(date TEXT PRIMARY KEY, day INTEGER, logged_at TEXT).
N = days since the project started (2026-04-02, day 125 on 2026-08-05).

Idempotent: if today's date is already present, nothing changes (safe to run
multiple times). Run daily via crontab:

  3 0 * * * python3 \
      /path/to/devbox-ops/host/oculus_progress_log.py >> /tmp/progress_log.log 2>&1
"""
import os
import sqlite3
import sys
from datetime import date, timedelta

DB_FILE = os.environ.get(
    "OCULUS_PROGRESS_DB",
    os.path.join(os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")),
                 "oculus", "progress.db"),
)
START_DATE = date(2026, 4, 2)  # project start — day 125 on 2026-08-05


def _connect():
    """Open the WAL journal and ensure the append-only table exists."""
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS progress_log (
            date TEXT PRIMARY KEY,
            day INTEGER NOT NULL,
            logged_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        )
        """
    )
    return conn


def main():
    today = date.today()
    day = (today - START_DATE).days

    # P1B0R0F1#202: append-only SQLite WAL journal. WAL mode + a single upsert
    # statement give atomic, concurrency-safe writes without the
    # read-modify-write race of the old JSON log; the PRIMARY KEY on date makes
    # the daily append idempotent. No advisory lock file needed.
    os.makedirs(os.path.dirname(DB_FILE) or '.', exist_ok=True)
    conn = _connect()
    try:
        with conn:
            cur = conn.execute(
                "INSERT INTO progress_log (date, day) VALUES (?, ?) "
                "ON CONFLICT(date) DO NOTHING",
                (today.isoformat(), day),
            )
        if cur.rowcount:
            print(f"logged {today.isoformat()} day {day}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
