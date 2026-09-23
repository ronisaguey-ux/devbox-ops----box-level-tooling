#!/usr/bin/env bash
# 09-09 (roni): 1-min wake watchdog for the main opencode session.
#
# WHY: if the agent hits a hard stop (step cap, provider hiccup, crash) its turn
# ends and the session goes IDLE with no pending user message — nothing resumes
# it. This watchdog re-prompts the session so it continues its task.
#
# HOW: every minute, if the target session is IDLE, the "keepgoing" sentinel is
# ARMED AND UNEXPIRED, **and the last turn ended by hitting the step cap**, POST a
# continue prompt via the opencode API (prompt_async). The wake prompt tells the
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
DEST="${OPENCODE_DEST:-http://127.0.0.1:4096}"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/opencode"
LOG="$STATE_DIR/wake_1min.log"
SENTINEL="$STATE_DIR/keepgoing"
PIN="$STATE_DIR/wake_session_id"
HOURFILE="$STATE_DIR/wake_hourly"
MAX_PER_HOUR="${OPENCODE_WAKE_MAX_PER_HOUR:-60}"
ARM_TTL_S="${OPENCODE_WAKE_ARM_TTL_S:-1800}"   # 30 min default

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
curl -fsS -o /dev/null --max-time 3 "$DEST" 2>/dev/null || exit 0

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
  SID="$(curl -s --max-time 5 "$DEST/session" 2>/dev/null | python3 -c '
import sys,json
try:
    d=json.load(sys.stdin)
except Exception:
    sys.exit(0)
c=[s for s in d if not (s.get("time") or {}).get("archived")
   and ((s.get("tokens") or {}).get("input",0) or s.get("tokens_input",0) or 0)>0]
c.sort(key=lambda s:(s.get("time") or {}).get("updated",0))
print(c[-1]["id"] if c else "")' 2>/dev/null)"
  if [ -n "$SID" ]; then echo "$SID" > "$PIN"; fi
fi
[ -z "$SID" ] && exit 0

# 4. only wake when the session is NOT busy (never interrupt live work)
ST="$(curl -s --max-time 5 "$DEST/session/status" 2>/dev/null | python3 -c "
import sys,json
try: d=json.load(sys.stdin)
except Exception: d={}
print((d.get('$SID') or {}).get('type','idle'))" 2>/dev/null)"
[ "$ST" = "busy" ] && exit 0

# 5. TRIGGER GATE (09-09 roni): only wake when the turn ended by hitting the
#    step cap ("on ur last tool call and u need to do more work"). opencode
#    stores the marker "Maximum steps for this agent have been reached." as a
#    text part in the final assistant message of a cap-truncated turn. Scan only
#    the CURRENT turn (assistant messages after the last user message); a normal
#    stop has no marker -> never wake, no matter how long the session sits idle.
CAP="$(curl -s --max-time 8 "$DEST/session/$SID/message" 2>/dev/null | python3 -c '
import sys, json, re
try:
    msgs = json.load(sys.stdin)
except Exception:
    print("unknown"); sys.exit(0)
li = -1
for i, m in enumerate(msgs):
    if ((m.get("info") or {}).get("role")) == "user":
        li = i
turn = msgs[li+1:] if li >= 0 else msgs
pat = re.compile(r"maximum steps for this agent", re.I)
for m in turn:
    if ((m.get("info") or {}).get("role")) != "assistant":
        continue
    for p in (m.get("parts") or []):
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
curl -s --max-time 20 -X POST "$DEST/session/$SID/prompt_async" \
  -H 'Content-Type: application/json' \
  -d "{\"agent\":\"build\",\"model\":{\"providerID\":\"deepseek\",\"modelID\":\"deepseek-v4-flash-vision-exp\"},\"parts\":[{\"type\":\"text\",\"text\":\"[wake watchdog] Resume and continue your unfinished task from where you stopped. If you are still working on a long task, RE-ARM the watchdog: bash /home/roni/Roni_Workspace/oculus/scripts/opencode_wake_1min.sh --arm . If everything is complete and nothing is pending, DISARM it: bash /home/roni/Roni_Workspace/oculus/scripts/opencode_wake_1min.sh --disarm\"}]}" \
  >/dev/null 2>&1
log "woke session $SID (step-cap hit, sentinel armed, wake #$N this hour)"
