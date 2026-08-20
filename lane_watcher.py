#!/usr/bin/env python3
"""PROJECT execution-lane watcher (2026-08-17 user directive).

When the DeepSeek webchat lane gets rate-limited, auto-switch the plan
executor to the OmniRoute 5-agent team lane; probe DeepSeek with a tiny
"pong" every 10 min until the rate limit clears; then switch back to
webchat-primary.

Lane state lives in audits_plans/execution_lane.env:
    export OMNIROUTE_ATTEMPTS=0   # webchat-primary (deepseek webchat expert)
    export OMNIROUTE_ATTEMPTS=3   # OmniRoute 5-agent team first (3 attempts)
start_orchestrator.sh sources it, so EVERY restart (incl. stack_supervisor's
60s respawn) honors the active lane.

To flip the lane this watcher: (1) rewrites execution_lane.env, (2) brings
OmniRoute up (flip->omniroute) or down (flip->webchat), (3) kills the
orchestrator + executor, (4) relaunches via start_orchestrator.sh so the
resume picks up the new lane immediately.

Run: nohup <venv>/bin/python scripts/lane_watcher.py >>/tmp/lane_watcher.log 2>&1 &
"""

import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request

ORCH_DIR = os.getenv("ORCHESTRATOR_DIR",
                        os.path.join(os.path.expanduser("~"), "project"))
WORK_DIR = os.getenv("WORK_DIR",
                       os.path.join(os.path.expanduser("~"), "project_work"))
LANE_FILE = os.path.join(WORK_DIR, "execution_lane.env")
EXEC_LOG = os.path.join(WORK_DIR, "plan_execution.log")
WATCHDOG_PY = os.path.join(ORCH_DIR, "scripts", "omniroute_watchdog.py")
VENV_PY = os.path.join(ORCH_DIR, ".venv", "bin", "python")
ORCH_SH = os.path.join(ORCH_DIR, "workflow_orchestrator", "start_orchestrator.sh")
LOG_FILE = "/tmp/lane_watcher.log"

WEBCHAT_URL = "http://127.0.0.1:8080/v1/chat/completions"
OMNIROUTE_PROBE = "http://localhost:20128/api/v1/models"

# Only signatures that UNEQUIVOCALLY mean the DeepSeek ACCOUNT is
# rate-limited flip the lane:
#   - "Messages too frequent"  DeepSeek's own account-cooldown message
#   - "rate_limit_reached"     the API's explicit rate-limit error
# Deliberately NOT here (2026-08-18 false-positive fix):
#   - raw "429"                the OmniRoute team lane writes "[BAN] ... HTTP 429"
#   - "rate-limit signature detected"   the executor's backoff line, written on
#                              ANY of rate_limit/too frequent/webchat
#                              unavailable/server disconnected/429 — i.e. a
#                              killed or hung gateway triggers it too
#   - "webchat unavailable" / "server disconnected" / "Stream idle timeout"
#                              connection/stuck-tab signals. A gateway that was
#                              killed (or a tab stuck in phantom generation) is
#                              NOT an account rate-limit — this caused the
#                              2026-08-17 false positive that flipped the lane
#                              while the user was talking to DeepSeek fine.
RATE_LIMIT_SIGS = (
    "Messages too frequent",
    "rate_limit_reached",
)

SCAN_WINDOW_BYTES = 200 * 1024  # scan last ~200KB of the exec log
RATE_WINDOW = 300  # only count a rate-limit line written within this many seconds
FLIP_GRACE = 5  # seconds between killing the orchestrator and relaunching
ORCH_START_WAIT = 45  # seconds to wait for the orchestrator to come back

_LINE_TS_RE = re.compile(r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]")
last_flip_ts = 0.0  # epoch; rate-limit lines older than the last flip are ignored


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} - {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def read_lane() -> str:
    """Current lane: 'webchat' or 'omniroute' (default webchat)."""
    try:
        with open(LANE_FILE, "r") as f:
            if "OMNIROUTE_ATTEMPTS=3" in f.read():
                return "omniroute"
    except Exception:
        pass
    return "webchat"


def write_lane(lane: str) -> None:
    val = "3" if lane == "omniroute" else "0"
    tmp = LANE_FILE + ".tmp"
    with open(tmp, "w") as f:
        f.write(f"# written by lane_watcher.py (do not edit by hand)\nexport OMNIROUTE_ATTEMPTS={val}\n")
    os.replace(tmp, LANE_FILE)
    log(f"lane state -> {lane} (OMNIROUTE_ATTEMPTS={val})")


def pong_deepseek() -> bool:
    """Tiny probe through the DeepSeek webchat gateway.

    True = healthy (HTTP 200 + content, no rate-limit signature).
    False = rate-limited / unavailable.
    """
    payload = {
        "model": "anymodel",
        "messages": [{"role": "user", "content": "pong"}],
        "max_tokens": 5,
    }
    req = urllib.request.Request(
        WEBCHAT_URL, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            body = r.read().decode("utf-8", "replace")
            if r.status != 200:
                return False
            low = body.lower()
            if any(sig in low for sig in ("rate_limit", "too frequent",
                                          "unavailable", "disconnected")):
                return False
            try:
                content = json.loads(body)["choices"][0]["message"].get("content")
            except Exception:
                return False
            return bool(content and content.strip())
    except urllib.error.HTTPError as e:
        log(f"pong HTTPError {e.code}")
        return False
    except TimeoutError:
        # A timed-out pong = gateway busy or tab stuck (mid-step, mutex held,
        # phantom generation) — NOT an account rate-limit. Expected and
        # harmless on the omniroute lane; never an escalation signal.
        log("pong timeout (gateway busy/stuck) — not an account rate-limit")
        return False
    except Exception as e:
        log(f"pong error {type(e).__name__}: {e}")
        return False


def scan_log_for_rate_limit() -> bool:
    """Scan the tail of plan_execution.log for a RECENT webchat rate-limit.

    Only lines written within RATE_WINDOW seconds, and after the last lane
    flip, count — stale team-lane BAN lines and old rate-limits must never
    trigger a flip (or a flip-flop loop right after switching back).
    """
    now = time.time()
    try:
        size = os.path.getsize(EXEC_LOG)
        with open(EXEC_LOG, "rb") as f:
            f.seek(max(0, size - SCAN_WINDOW_BYTES))
            tail = f.read().decode("utf-8", "replace")
    except Exception:
        return False
    for line in tail.splitlines():
        if not any(sig in line for sig in RATE_LIMIT_SIGS):
            continue
        m = _LINE_TS_RE.search(line)
        if not m:
            continue
        try:
            ts = time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"))
        except ValueError:
            continue
        if ts >= last_flip_ts and (now - ts) <= RATE_WINDOW:
            return True
    return False


def proc_running(pattern: str) -> bool:
    try:
        out = subprocess.run(["pgrep", "-f", pattern],
                             capture_output=True, text=True, timeout=10)
        return out.returncode == 0 and bool(out.stdout.strip())
    except Exception:
        return False


def kill_orchestrator_and_executor() -> None:
    """Kill the orchestrator + executor (the lane-flip restart point)."""
    for pat in (r"execute_master_[o]culus_plan\.py", r"run_oculus_[w]orkflow\.py"):
        try:
            out = subprocess.run(["pgrep", "-f", pat],
                                 capture_output=True, text=True, timeout=10)
            for pid in out.stdout.split():
                try:
                    os.kill(int(pid), signal.SIGTERM)
                except ProcessLookupError:
                    pass
        except Exception:
            pass
    time.sleep(2)
    # SIGKILL any survivors (they checkpoint state on every step; resume is safe)
    for pat in (r"execute_master_[o]culus_plan\.py", r"run_oculus_[w]orkflow\.py"):
        try:
            out = subprocess.run(["pgrep", "-f", pat],
                                 capture_output=True, text=True, timeout=10)
            for pid in out.stdout.split():
                try:
                    os.kill(int(pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass
        except Exception:
            pass


def relaunch_orchestrator() -> bool:
    """Relaunch via start_orchestrator.sh (same flags the supervisor uses)."""
    try:
        subprocess.Popen(
            ["bash", ORCH_SH, "--phase", "execution", "--resume"],
            cwd=ORCH_DIR, start_new_session=True,
            stdout=open("/tmp/orch_lane_switch.log", "a"),
            stderr=subprocess.STDOUT)
    except Exception as e:
        log(f"relaunch failed: {e}")
        return False
    # wait for the new orchestrator to appear
    deadline = time.time() + ORCH_START_WAIT
    while time.time() < deadline:
        if proc_running(r"run_oculus_[w]orkflow\.py"):
            return True
        time.sleep(2)
    log("orchestrator did not reappear after relaunch")
    return False


def omniroute_up() -> bool:
    try:
        with urllib.request.urlopen(
                urllib.request.Request(OMNIROUTE_PROBE,
                                       headers={"User-Agent": "lane-watcher/1.0"}),
                timeout=8) as r:
            return r.status == 200
    except Exception:
        return False


def start_omniroute() -> bool:
    """Start the OmniRoute watchdog (self-heals the proxy; 300s compile grace)."""
    if proc_running(r"omniroute_[w]atchdog\.py"):
        log("omniroute watchdog already running")
    else:
        try:
            subprocess.Popen([VENV_PY, WATCHDOG_PY], cwd=ORCH_DIR,
                             start_new_session=True,
                             stdout=open("/tmp/omniroute_watchdog.log", "a"),
                             stderr=subprocess.STDOUT)
            log("started omniroute watchdog")
        except Exception as e:
            log(f"failed to start omniroute watchdog: {e}")
    # wait for the proxy to actually serve (up to ~6 min incl. compile grace)
    deadline = time.time() + 360
    while time.time() < deadline:
        if omniroute_up():
            log("omniroute is serving on :20128")
            return True
        time.sleep(5)
    log("omniroute did not come up within 360s")
    return False


def stop_omniroute() -> None:
    """Kill the watchdog, then node/esbuild processes bound to OmniRoute."""
    for pat in (r"omniroute_[w]atchdog\.py",):
        try:
            subprocess.run(["pkill", "-f", pat], capture_output=True, timeout=10)
        except Exception:
            pass
    time.sleep(1)
    # kill listeners on 20128 (the proxy) and esbuild children of OmniRoute
    try:
        out = subprocess.run(["ss", "-tlnp"], capture_output=True, text=True, timeout=10)
        for line in out.stdout.splitlines():
            if ":20128" in line and "pid=" in line:
                m = re.search(r"pid=(\d+)", line)
                if m:
                    try:
                        os.kill(int(m.group(1)), signal.SIGKILL)
                    except (ProcessLookupError, ValueError):
                        pass
    except Exception:
        pass
    try:
        out = subprocess.run(["pgrep", "-f", "OmniRoute"], capture_output=True, text=True, timeout=10)
        for pid in out.stdout.split():
            try:
                os.kill(int(pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
    except Exception:
        pass
    log("omniroute stopped (watchdog + node)")


def flip(lane: str) -> None:
    """Switch the active lane and restart the executor under it."""
    global last_flip_ts
    log(f"FLIP -> {lane}")
    write_lane(lane)
    kill_orchestrator_and_executor()  # pause execution first (state is checkpointed)
    if lane == "omniroute":
        ok = start_omniroute()
        if not ok:
            log("aborting flip: omniroute unavailable; reverting to webchat")
            write_lane("webchat")
            relaunch_orchestrator()
            return
    time.sleep(FLIP_GRACE)
    relaunch_orchestrator()
    if lane == "webchat":
        stop_omniroute()
    last_flip_ts = time.time()
    log(f"FLIP complete -> {lane}")


def main() -> None:
    global last_flip_ts
    log("lane_watcher started (webchat-primary default)")
    # Fresh start: never count stale log lines. Only lines written AFTER this
    # watcher came up (and within RATE_WINDOW) can trigger a flip.
    last_flip_ts = time.time()
    lane = read_lane()
    log(f"initial lane: {lane}")
    last_pong = 0.0
    last_rate_check = 0.0
    while True:
        lane = read_lane()
        now = time.time()
        if lane == "webchat":
            # Detect a DeepSeek rate limit as the executor logs it; flip to team.
            if now - last_rate_check >= 30:
                last_rate_check = now
                if proc_running(r"execute_master_[o]culus_plan\.py"):
                    if scan_log_for_rate_limit():
                        log("rate-limit signature in exec log -> flipping to omniroute")
                        flip("omniroute")
            time.sleep(15)
        else:
            # On the omniroute lane: pong DeepSeek every 10 min; flip back when healthy.
            if now - last_pong >= 600:
                last_pong = now
                if pong_deepseek():
                    log("deepseek pong OK -> flipping back to webchat")
                    flip("webchat")
                else:
                    log("deepseek still rate-limited/unavailable; staying on omniroute")
            time.sleep(60)


if __name__ == "__main__":
    main()
