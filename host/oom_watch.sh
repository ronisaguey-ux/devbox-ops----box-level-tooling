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
      # 09-13 (owner: "it needs to wake u up not me"): the alarm already lands in
      # claude_main_inbox.json above — that is the wake chain
      # (oculus-direct-tg -> claude_main_inbox.json -> oc_wake_watch.sh ->
      # tele_inbox_gate.py -> oc_send.js -> the agent session). It never woke
      # anyone because tele_inbox_gate.py only let `from:"telegram"` through;
      # it now also accepts ALERT sources, so this inbox write IS the wake.
      # No Telegram ping — the owner does not want to be the alarm.
      # Also nudge the session directly, so a broken watcher cannot swallow it.
      if [ -x /home/roni/.local/lib/ocbridge/oc_send.js ]; then
        node /home/roni/.local/lib/ocbridge/oc_send.js \
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
