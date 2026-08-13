#!/usr/bin/env python3
"""OmniRoute watchdog — keeps the API gateway (port 20128) alive.

The audit (parallel_agents.py) and cross-eval depend on OmniRoute at
http://localhost:20128/api/v1. If it dies, every agent call fails and the
audit produces a false '0 findings' report (fixed by the hard-fail guard).

This watchdog:
  - Probes http://localhost:20128/api/v1/models every 30s
  - If the port is dead or the probe fails twice in a row, restarts OmniRoute
    via `npm run dev` in $OMNIROUTE_DIR
  - Logs every probe/restart to /tmp/omniroute_watchdog.log

Run: python3 scripts/omniroute_watchdog.py &   (or via start_orchestrator.sh)
"""

import os
import signal
import subprocess
import time
import urllib.request

OMNIROUTE_DIR = os.getenv("OMNIROUTE_DIR", os.path.join(os.path.expanduser("~"), "OmniRoute"))
PORT = os.getenv("OMNIROUTE_PORT", "20128")
PROBE_URL = os.getenv("OMNIROUTE_PROBE_URL", f"http://localhost:{PORT}/api/v1/models")
LOG_FILE = "/tmp/omniroute_watchdog.log"
POLL_SECONDS = 30
CONSECUTIVE_FAILS_TO_RESTART = 2
# Next.js dev compile on this laptop can take 2-5 min before the port binds.
# A 90s grace caused a restart-loop: watchdog spawned a fresh instance while
# the previous one was still compiling, and the newcomer died on port
# contention. (2026-08-06)
STARTUP_GRACE = 300


def log(msg: str):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} - {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def probe() -> bool:
    try:
        with urllib.request.urlopen(ProbeRequest(PROBE_URL), timeout=8) as r:
            return r.status == 200
    except Exception:
        return False


class ProbeRequest(urllib.request.Request):
    def __init__(self, url):
        super().__init__(url, headers={"User-Agent": "omniroute-watchdog/1.0"})


def is_running() -> bool:
    """True if any process is listening on port 20128."""
    try:
        out = subprocess.run(
            ["ss", "-tlnp"], capture_output=True, text=True, timeout=10).stdout
        return f":{PORT}" in out
    except Exception:
        return False


def _kill_omniroute_only():
    """SIGKILL only processes whose cwd is the OmniRoute directory.

    The old `pkill -9 -f "next.*dev"` matched EVERY Next.js dev/production
    server on the machine (blast radius flagged in the 2026-08-03 audit).
    Narrow it to OmniRoute's own node processes so other apps are untouched.

    2026-08-06: `ps -eo pid,cwd,args` renders "-" for the cwd column on every
    process on this box, so the ps-based match never fired and every restart
    crash-looped on the stuck port-holder. Read /proc/<pid>/cwd directly.
    """
    killed = 0
    try:
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            pid = int(entry)
            try:
                cwd = os.readlink(f"/proc/{pid}/cwd")
            except OSError:
                continue
            if os.path.realpath(cwd) != os.path.realpath(OMNIROUTE_DIR):
                continue
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    cmdline = f.read().replace(b"\0", b" ").decode(errors="replace")
            except OSError:
                continue
            if "node" in cmdline or "npm" in cmdline:
                try:
                    os.kill(pid, signal.SIGKILL)
                    killed += 1
                    log(f"  killed stale OmniRoute pid {pid}")
                except (ProcessLookupError, PermissionError):
                    pass
    except Exception as e:
        log(f"  _kill_omniroute_only error: {e}")
    if killed == 0:
        log("  _kill_omniroute_only: no stale OmniRoute processes found")
    # Belt-and-braces: free the port if something still holds it.
    try:
        subprocess.run(["fuser", "-k", "-n", "tcp", PORT],
                       capture_output=True, timeout=10)
    except Exception:
        pass


def start_omniroute():
    log("OmniRoute DOWN — restarting via npm run dev")
    try:
        # Kill only OmniRoute's own stale node processes (never other apps)
        _kill_omniroute_only()
    except Exception:
        pass
    time.sleep(3)
    try:
        # Server logs to a file (not devnull) — post-boot crashes were
        # undiagnosable on 2026-08-06 without them. Append, like LOG_FILE.
        server_log = open("/tmp/omniroute_dev.log", "a")
        proc = subprocess.Popen(
            ["npm", "run", "dev"],
            cwd=OMNIROUTE_DIR,
            stdout=server_log, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        log(f"OmniRoute launched (pid {proc.pid}) — waiting {STARTUP_GRACE}s for port...")
        return proc
    except Exception as e:
        log(f"FAILED to launch OmniRoute: {e}")
        return None


def main():
    log("=== OmniRoute watchdog started ===")
    fails = 0
    # Grace period on startup so a compiling dev server isn't killed instantly
    time.sleep(30)
    while True:
        time.sleep(POLL_SECONDS)
        if probe():
            if fails >= CONSECUTIVE_FAILS_TO_RESTART:
                log("OK — OmniRoute recovered")
            fails = 0
            continue
        fails += 1
        log(f"probe failed ({fails}/{CONSECUTIVE_FAILS_TO_RESTART})")
        if fails >= CONSECUTIVE_FAILS_TO_RESTART:
            start_omniroute()
            fails = 0
            time.sleep(STARTUP_GRACE)


if __name__ == "__main__":
    main()
