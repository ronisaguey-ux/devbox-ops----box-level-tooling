#!/usr/bin/env bash
# rclone_backup.sh — owned, supervised repo -> Google Drive backup.
#
# WHY: previously a one-off shell (no owner, no supervisor, no retry hygiene).
# The telegram-monitor scheduler rewrites .claude/scheduled_tasks.json every
# few minutes — during a 30+ min sync rclone's copy stat races it and burns a
# retry cycle (ERROR "source file is being updated"). Those files are
# ephemeral session state, NOT source of truth — excluded. Same for .git/logs/**
# (the reflog): it changes on EVERY git write, so a sync racing a push/commit
# always fails on it — the objects it references are what matter, and they're
# backed up separately.
#
# Usage: nightly via crontab (see comment below), safe to re-run (--update).
#   47 1 * * * $HOME/scripts/rclone_backup.sh >> /tmp/rclone_backup.log 2>&1
set -u
CONF="$HOME/.config/rclone/rclone-new.conf"
SRC="${REPO_DIR:-$HOME/project}"
DST="${REMOTE:-backup}:${REMOTE_PATH:-workspace/project}"
LOG="/tmp/rclone_backup.log"

# --tpslimit 10: cap Drive API transactions/sec so the full-tree listing (no
# --fast-list) can't blow the per-minute 'Queries' quota. 2026-08-09: without
# it, every run tripped 403 rateLimitExceeded and the retry wrapper burned all
# 3 attempts twice; throttled runs complete fine (delta syncs need ~hundreds
# of ops -> seconds at 10 tps).
rclone --config "$CONF" sync "$SRC" "$DST" --update --tpslimit 10 \
  --exclude 'node_modules/**' \
  --exclude '__pycache__/**' \
  --exclude '.venv*/**' \
  --exclude 'graphify-out/**' \
  --exclude 'live/live_state.json' \
  --exclude '*.pyc' \
  --exclude '.env' \
  --exclude '.claude/scheduled_tasks*' \
  --exclude '.git/logs/**' \
  --log-file "$LOG" --log-level INFO

rc=$?
if [[ $rc -eq 0 ]]; then
  echo "$(date '+%Y-%m-%d %H:%M:%S') rclone_backup.sh OK" >> "$LOG"
else
  echo "$(date '+%Y-%m-%d %H:%M:%S') rclone_backup.sh FAILED rc=$rc" >> "$LOG"
fi
exit $rc
