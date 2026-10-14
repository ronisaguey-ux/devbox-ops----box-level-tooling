#!/bin/bash
# OOM alarm + auto-heal (08-30, user: "a bunch of ooms happened, why didnt u
# wake up and solve em"). 13:02 miss chain: kernel killed 2 chrome procs inside
# oculus-chrome-hl.service; my monitor never delivered because (a) it was a
# session-scoped Monitor and the RAM watchdog had PAUSED the harness node the
# same window (17:00:16/46Z), and (b) its grep was case-sensitive "Out of
# memory" while the kernel line says "Memory cgroup out of memory".
#
# FIX: run under systemd (oculus-oom-watch.service, Restart=always) so the
# alarm lives OUTSIDE the harness; on every cgroup OOM kill it
#   1. appends to claude_main_inbox.json  (survives anything)
#   2. appends to /tmp/main_wake.log       (the session's persistent tail)
#   3. restarts the killed oculus unit     (auto-heal, deduped 30s)
INBOX="${OOM_WATCH_INBOX:-$HOME/Roni_workspace/audits_plans/claude_main_inbox.json}"
# 2026-10-14 (owner: "a bunch of ooms occurred and they never woke u up"). The wake
# chain moved to the bridge: oc_wake_watch.sh reads the JSONL inbox below, NOT
# claude_main_inbox.json. Writing only the array meant 123 OOM alerts landed in a file
# nothing watches - the alarm fired and no one was woken. The array write stays (it is
# history), and every alert is ALSO appended here as one JSONL line, which is the file
# the watcher and tele_inbox_gate.py actually read.
ALERT_INBOX="${OOM_WATCH_ALERT_INBOX:-/tmp/clone_inbox.jsonl}"
WAKE=/tmp/main_wake.log
DEDUP=/tmp/oom_watch_last_restart

journalctl -k -f -n 0 -o cat 2>/dev/null \
  | grep --line-buffered -iE 'oom-kill|out of memory|killed process' \
  | while read -r line; do
      ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
      unit="$(echo "$line" | grep -oE '/oculus[a-z0-9-]*\.service' | head -1 | sed 's|/||')"
      python3 - "$INBOX" "$ts" "$unit" "$line" <<'EOF'
import json, sys, os
inbox, ts, unit, line = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
msgs = []
if os.path.exists(inbox):
    try:
        with open(inbox) as fh:
            msgs = json.load(fh)
        if not isinstance(msgs, list):
            msgs = []
    except Exception:
        msgs = []
msgs.append({"ts": ts, "from": "oom-watch",
             "text": (f"OOM KILL {unit}: " if unit else "OOM KILL: ") + line[:200]})
tmp = inbox + ".tmp"
with open(tmp, "w") as f:
    json.dump(msgs[-500:], f)
os.replace(tmp, inbox)
EOF
      printf '%s [oom-watch] OOM KILL%s: %s\n' \
        "$(date '+%Y-%m-%d %H:%M:%S')" "${unit:+ $unit}" "${line:0:140}" >> "$WAKE"
      # The WAKE: one JSONL line in the inbox the watcher polls. flock so a concurrent
      # writer (the poller, another alert) cannot interleave a half line, and the text
      # carries the unit so the alarm is actionable without reading the kernel line.
      python3 - "$ALERT_INBOX" "$ts" "$unit" "$line" <<'EOF'
import json, os, sys
import fcntl
inbox, ts, unit, line = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
rec = {
    "ts": int(__import__("time").time()),
    "from": "oom-watch",
    "sender": "oom-watch",
    "text": ("OOM KILL " + (unit or "unknown unit") + ": " + line[:200]),
    "alert": True,
}
try:
    with open(inbox, "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        fh.write(json.dumps(rec) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
        fcntl.flock(fh, fcntl.LOCK_UN)
except Exception as e:
    sys.stderr.write("oom-watch: could not append the alert to %s: %s\n" % (inbox, e))
EOF
      # 09-13 (owner: "it needs to wake u up not me"): tele_inbox_gate.py accepts the
      # ALERT sources (oom-watch et al) as well as `telegram`, so the JSONL line above
      # IS the wake. No Telegram ping — the owner does not want to be the alarm.
      # Also nudge the session directly, so a broken watcher cannot swallow it.
      # --async is required: without it oc_send waits on the prompt and can block an
      # idle session instead of queueing (2026-10-01 bridge rewrite).
      if [ -x /home/roni/.local/lib/ocbridge/oc_send.js ]; then
        node /home/roni/.local/lib/ocbridge/oc_send.js --async \
          "⚠️ OOM-WATCH: kernel killed ${unit:-a process} — auto-heal has restarted it. Investigate the RAM budget." \
          >/dev/null 2>&1 &
      fi

      if [ -n "$unit" ] && ! systemctl --user is-active --quiet "$unit" 2>/dev/null; then
        now="$(date +%s)"
        last="$(cat "$DEDUP" 2>/dev/null || echo 0)"
        case "$last" in
          ''|*[!0-9]*) last=0 ;;
        esac
        if [ $((now - last)) -gt 30 ]; then
          systemctl --user restart "$unit" 2>/dev/null \
            && printf '%s [oom-watch] restarted %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$unit" >> "$WAKE"
          echo "$now" > "$DEDUP"
        fi
      fi
    done
