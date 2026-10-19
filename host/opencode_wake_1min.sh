#!/usr/bin/env bash
# 09-09 (roni): 1-min wake watchdog for the main opencode session.
#
# WHY: if the agent hits a hard stop (step cap, provider hiccup, crash) its turn
# ends and the session goes IDLE with no pending user message — nothing resumes
# it. This watchdog re-prompts the session so it continues its task.
#
# HOW: every minute, if the target session is IDLE, the "keepgoing" sentinel is
# ARMED AND UNEXPIRED, **and the last turn ended by hitting the step cap**, POST a
# continue prompt via the opencode API (POST /api/session/{id}/prompt). The wake prompt tells the
# agent to RE-ARM if it is still working (new expiry) or DISARM when the task is
# truly done — so a forgotten sentinel stops waking after the TTL instead of
# spamming forever.
#
# 09-09 (roni) TRIGGER RULE: "only fire the 1 min cron if ur on ur last tool call
# and u need to do more work". So a merely-idle session is NOT woken. opencode
# stores a text part "Maximum steps for this agent have been reached." in the
# final assistant message of a cap-truncated turn; that marker is the ONLY thing
# that arms a wake. No marker => the turn ended on purpose => never wake.
#
#   arm:     opencode_wake_1min.sh --arm      (expiry = now + ARM_TTL_S, default 30m)
#   disarm:  opencode_wake_1min.sh --disarm
#   check:   opencode_wake_1min.sh            (the cron path)
set -u
# cron gives a minimal environment: pin HOME + PATH so curl/python3 resolve.
export HOME="${HOME:-/home/roni}"
export PATH="/usr/local/bin:/usr/bin:/bin:${PATH:-}"
DEST="${OPENCODE_DEST:-http://127.0.0.1:49374}"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/opencode"
LOG="$STATE_DIR/wake_1min.log"
SENTINEL="$STATE_DIR/keepgoing"
PIN="$STATE_DIR/wake_session_id"
HOURFILE="$STATE_DIR/wake_hourly"
MAX_PER_HOUR="${OPENCODE_WAKE_MAX_PER_HOUR:-60}"
ARM_TTL_S="${OPENCODE_WAKE_ARM_TTL_S:-1800}"   # 30 min default

SERVICE_JSON="$STATE_DIR/service.json"
PW=""
if [ -r "$SERVICE_JSON" ]; then
  PW="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("password",""))' "$SERVICE_JSON" 2>/dev/null)"
fi
[ -n "$PW" ] || exit 0
AUTH=(-u "opencode:$PW")

mkdir -p "$STATE_DIR" 2>/dev/null || exit 0
chmod 700 "$STATE_DIR" 2>/dev/null || true
ts(){ date '+%Y-%m-%d %H:%M:%S'; }
log(){ echo "[$(ts)] $*" >> "$LOG"; }

case "${1:-}" in
  --arm)
    echo "$(( $(date +%s) + ARM_TTL_S ))" > "$SENTINEL"
    log "sentinel ARMED (expires in ${ARM_TTL_S}s)"
    exit 0 ;;
  --disarm)
    rm -f "$SENTINEL"
    log "sentinel DISARMED"
    exit 0 ;;
esac

# 1. serve reachable? (never spin when the brain is down)
curl -fsS -o /dev/null --max-time 3 "${AUTH[@]}" "$DEST/api/info" 2>/dev/null || exit 0

# 2. sentinel armed AND unexpired?
[ -f "$SENTINEL" ] || exit 0
EXP="$(cat "$SENTINEL" 2>/dev/null || echo 0)"
if [ "${EXP:-0}" -gt 0 ] 2>/dev/null && [ "$(date +%s)" -ge "$EXP" ]; then
  log "sentinel expired (exp=$EXP) — removing; not waking"
  rm -f "$SENTINEL"
  exit 0
fi

# 3. resolve the target session: pinned file, else newest non-archived with tokens>0
SID=""
if [ -f "$PIN" ]; then SID="$(cat "$PIN" 2>/dev/null)"; fi
if [ -z "$SID" ]; then
  SID="$(curl -s --max-time 5 "${AUTH[@]}" "$DEST/api/session?limit=50" 2>/dev/null | python3 -c '
import sys,json
try:
    d=json.load(sys.stdin).get("data",[])
except Exception:
    sys.exit(0)
c=[s for s in d if ((s.get("tokens") or {}).get("input",0) or 0)>0
   and s.get("outcome") != "archived"]
c.sort(key=lambda s:(s.get("time") or {}).get("updated",0))
print(c[-1]["id"] if c else "")' 2>/dev/null)"
  if [ -n "$SID" ]; then echo "$SID" > "$PIN"; fi
fi
[ -z "$SID" ] && exit 0


# 5. TRIGGER GATE (09-09 roni): only wake when the turn ended by hitting the
#    step cap ("on ur last tool call and u need to do more work"). opencode
#    stores the marker "Maximum steps for this agent have been reached." as a
#    text part in the final assistant message of a cap-truncated turn. Scan only
#    the CURRENT turn (assistant messages after the last user message); a normal
#    stop has no marker -> never wake, no matter how long the session sits idle.
CAP="$(curl -s --max-time 8 "${AUTH[@]}" "$DEST/api/session/$SID/message?limit=60" 2>/dev/null | python3 -c '
import sys, json, re
try:
    msgs = json.load(sys.stdin).get("data", [])
except Exception:
    print("unknown"); sys.exit(0)
li = -1
for i, m in enumerate(msgs):
    if m.get("type") == "user":
        li = i
turn = msgs[li+1:] if li >= 0 else msgs
pat = re.compile(r"maximum steps for this agent", re.I)
for m in turn:
    if m.get("type") != "assistant":
        continue
    for p in (m.get("content") or []):
        if p.get("type") == "text" and pat.search(p.get("text") or ""):
            print("cap"); sys.exit(0)
print("normal")' 2>/dev/null)"
if [ "$CAP" != "cap" ]; then
  log "last turn ended normally (no step-cap marker) — not waking"
  exit 0
fi

# 6. hourly cap
HOUR="$(date '+%Y%m%d%H')"; N=0
if [ -f "$HOURFILE" ]; then
  read -r FH FN < "$HOURFILE" 2>/dev/null || true
  if [ "${FH:-}" = "$HOUR" ]; then N="${FN:-0}"; fi
fi
if [ "$N" -ge "$MAX_PER_HOUR" ] 2>/dev/null; then
  log "hourly cap reached ($N) — not waking"
  exit 0
fi
N=$((N + 1)); echo "$HOUR $N" > "$HOURFILE"

# 7. wake: async continue prompt (same agent/model as the live session)
WAKE_TEXT="[wake watchdog] Resume and continue your unfinished task from where you stopped. If you are still working on a long task, RE-ARM the watchdog: bash /home/roni/Roni_workspace/devbox-ops----box-level-tooling/host/opencode_wake_1min.sh --arm . If everything is complete and nothing is pending, DISARM it: bash /home/roni/Roni_workspace/devbox-ops----box-level-tooling/host/opencode_wake_1min.sh --disarm"
python3 - "$DEST" "$PW" "$SID" "$WAKE_TEXT" >/dev/null 2>&1 <<'PYEOF'
import base64, json, sys, urllib.request
dest, pw, sid, text = sys.argv[1:5]
req = urllib.request.Request(f"{dest}/api/session/{sid}/prompt",
    data=json.dumps({"text": text}).encode(),
    headers={"Content-Type": "application/json",
             "Authorization": "Basic " + base64.b64encode(f"opencode:{pw}".encode()).decode()},
    method="POST")
urllib.request.urlopen(req, timeout=20).read()
PYEOF
log "woke session $SID (step-cap hit, sentinel armed, wake #$N this hour)"
