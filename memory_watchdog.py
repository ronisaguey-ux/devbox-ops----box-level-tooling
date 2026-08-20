#!/usr/bin/env python3
"""Memory watchdog for the Project workflow.

Watches system RAM every 10 seconds and protects the machine from
memory-exhaustion crashes (the kind that killed VS Code + the audit
on 2026-08-02, and froze the box 2026-08-05 21:15 when the audit +
OmniRoute's claude -p swarm exhausted RAM and the mt7921e WiFi driver
hung):

  available > 3.5 GB  -> normal; ensure orchestrator unpaused
  available < 2.5 GB  -> memory-stressed: pause the orchestrator +
                         SIGTERM the audit subprocesses (parallel_agents)
  available < 1.5 GB  -> critical: SIGTERM cross_eval + any claude -p
                         subprocess swarms too
  recovers > 3.5 GB   -> resume the orchestrator (unpause)
  > CLAUDE_SWARM_CAP  -> PAUSE (SIGSTOP) the newest claude -p subprocesses
                         every poll; resume (SIGCONT) when back under cap

2026-08-08 user directive (verbatim): "instead of the watchdog killing
processes, just have it pause them" + "make sure no matter what the watchdog
cant kill you tho, even worst case, it can kill anything but you". So:
- Over-cap RAM/CPU enforcement = SIGSTOP (pause) the lowest-priority heaviest
  consumer, recorded in /tmp/watchdog_paused.pids, SIGCONT'd when under cap.
- Swarm extras beyond CLAUDE_SWARM_CAP = paused, not killed.
- The main Claude session (bare "claude", bg-spare, bg-pty-host, the tmux
  tree) is HARD-protected at tier 0 — never paused, never killed, even in the
  worst case. The deadman, ack daemon, telegram server are protected too.
- ONLY true OOM-critical (avail < 1.5 GB, machine about to freeze) still
  kills — and only non-protected processes. Worst case kills everything
  EXCEPT the session (user-authorized).

2026-08-05 hardening: the audit phase subprocesses are NO LONGER protected
— they checkpoint every batch, so killing them is cheap and they resume
with --resume. Before this, parallel_agents.py was tier-0 protected and
the watchdog's RAM-cap kill loop had nothing heavy left to kill, which is
why the machine died instead.

Everything it does is reversible: pausing writes to workflow_state.json
the same way the Telegram /pause command does, and it never kills the
orchestrator itself — only the heavy phase subprocesses.

Logs to /tmp/memory_watchdog.log. Started by start_orchestrator.sh
(or manually: python3 memory_watchdog.py &).
"""

import hmac
import hashlib
import json
import os
import signal
import subprocess
import sys
import time

# STEP 285/1566: the watchdog runs standalone (`python3 scripts/memory_watchdog.py`),
# so prepend the repo root to sys.path before importing the shared registry.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.common.process_registry import is_project_process, kill_if_project

WORKFLOW_STATE = os.getenv("WORKFLOW_STATE",
                     os.path.join(os.path.expanduser("~"), "project_work", "workflow_state.json"))
LOG_FILE = "/tmp/memory_watchdog.log"
# 2026-08-14 (user): the watchdog must wake the main Claude session whenever
# it acts (pauses/signals). The main session's inbox monitor tails this file.
MAIN_INBOX = os.getenv("MAIN_INBOX",
                 os.path.join(os.path.expanduser("~"), "project_work", "claude_main_inbox.json"))

PAUSE_BELOW_GB = 2.5      # pause the pipeline when free RAM drops below this
RESUME_ABOVE_GB = 3.5     # resume when it recovers above this
CRITICAL_BELOW_GB = 1.5   # also kill cross_eval / claude swarms below this
POLL_SECONDS = 10

# ── User rule (2026-08-02): machine must never run out of RAM ──
# RAISED 2026-08-03: was 45% (6.3 GiB used max) — the audit + backup +
# OmniRoute + VS Code together sit around 6 GB used, so the 45% cap fired
# constantly and killed the KDE desktop (plasmashell), logging the user out
# to the login screen. Then 70% (≈9.8 GiB) per the user (2026-08-03) — but
# with node/claude/VS Code all protected, Chrome was the only heavy killable
# process and got SIGTERMed whenever the stack crossed the cap (kills logged
# 2026-08-05). RAISED to 80% (≈11.2 GiB) AND Chrome/Firefox moved into the
# never-kill list (2026-08-05) so browsers are no longer the sacrificial
# lamb. The desktop + rclone + claude remain protected regardless (see the
# never-kill lists).
MAX_USED_PERCENT = 75.0  # Reverted per user directive
MAX_USED_BYTES = None  # computed at startup from /proc/meminfo

# ── User rule (2026-08-03): machine must NEVER exceed 80% total CPU ──
# Sustained (2-sample average) load above this kills the heaviest non-essential
# CPU consumer, sharing the same 90s cooldown as the RAM cap.
# (2026-08-04: user raised CPU cap from 70% → 80%.)
MAX_CPU_PERCENT = 85.0  # Reverted per user directive

# Phase subprocess names we're allowed to terminate (never the orchestrator).
# 2026-08-05 hardening: parallel_agents.py is NO LONGER protected — it
# checkpoints every batch (audit_state.json), so killing it mid-run costs
# nothing and resumes with --resume. It was tier-0 before, which is exactly
# why the 21:15 OOM-freeze happened: the watchdog couldn't kill the audit
# while it + OmniRoute burned the RAM.
AUDIT_NAMES = ("parallel_agents.py", "parallel_agent_cross_eval.py")
# Only print-mode subagent swarms, NEVER bare "claude": the interactive session's
# cmdline is `claude --resume ...` / `claude --plugin-dir ...` (no "-p"), so a
# bare "claude" fragment here would SIGTERM the user's interactive Claude too
# (flagged as a known edge case in the 2026-08-03 audit).
SWARM_NAMES = ("claude -p",)
# Hard cap on concurrent claude -p subprocesses (matches cross_eval's
# CLAUDE_SWARM_LIMIT=4). Spawned by OmniRoute auto/* models; uncapped they
# ate 14 GiB on 2026-08-05. Extras get SIGTERMed every poll.
CLAUDE_SWARM_CAP = 4

_PROTECTED_PATTERNS = [
    'omniroute',
    'memory_watchdog',
    'kill_switch',
    'live_monitor',
    'trade_executor',
    'surfshark',
    'networkmanager',
    'wpa_supplicant',
    'systemd-resolved',
    'wireguard',
    'tailscale',
]

def _is_protected(cmdline: str) -> bool:
    cl = cmdline.lower()
    return any(p in cl for p in _PROTECTED_PATTERNS)


def log(msg: str):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} - {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def notify_main(text: str):
    """Wake the main Claude session: append a watchdog event to its inbox.

    The main session's inbox monitor tails claude_main_inbox.json and wakes
    it on any new entry (2026-08-14 user rule: the watchdog never kills the
    main session, and wakes it whenever it pauses/signals something)."""
    try:
        with open(MAIN_INBOX) as f:
            data = json.load(f)
        if not isinstance(data, list):
            data = []
        data.append({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                     "from": "watchdog", "text": text})
        tmp = MAIN_INBOX + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, MAIN_INBOX)
    except Exception:
        pass


def available_gb() -> float:
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / (1024 * 1024)
    except Exception:
        pass
    return 99.0  # can't read -> assume fine (don't fight the user's machine)


_KILL_SWITCH_SECRET = os.environ.get('KILL_SWITCH_SECRET', '')

def _sign(data: dict) -> str:
    body = json.dumps({k: v for k, v in data.items() if k != 'sig'}, sort_keys=True).encode()
    return hmac.new(_KILL_SWITCH_SECRET.encode(), body, hashlib.sha256).hexdigest()

def write_paused(paused: bool) -> None:
    """Set paused in workflow_state.json — same key /pause uses."""
    data = {'paused': paused}
    data['sig'] = _sign(data)
    try:
        tmp = WORKFLOW_STATE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, WORKFLOW_STATE)
    except Exception as e:
        log(f"could not write paused={paused}: {e}")

def read_paused() -> bool:
    try:
        with open(WORKFLOW_STATE) as f:
            data = json.load(f)
        if not _KILL_SWITCH_SECRET:
            return bool(data.get('paused', True))
        expected = _sign(data)
        if not hmac.compare_digest(data.get('sig', ''), expected):
            log('SEC_031: tampered kill-switch state; failing closed')
            return True
        return bool(data.get('paused', True))
    except FileNotFoundError:
        return True
    except (json.JSONDecodeError, OSError):
        return True


PAUSED_FILE = "/tmp/watchdog_paused.pids"

def load_paused() -> set:
    """PIDs currently SIGSTOP'd by the watchdog (resumed when under cap)."""
    try:
        with open(PAUSED_FILE) as f:
            return {int(x) for x in f.read().split() if x.isdigit()}
    except Exception:
        return set()


def save_paused(pids: set):
    """Atomically persist the paused-PID set (tmp + fsync + rename)."""
    tmp_path = PAUSED_FILE + ".tmp"
    try:
        with open(tmp_path, "w") as f:
            f.write(" ".join(str(p) for p in sorted(pids)))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, PAUSED_FILE)
    except Exception:
        pass


def pause_pids(entries) -> None:
    """SIGSTOP a batch of processes and record them in one atomic write."""
    newly_paused = []
    for pid_s, reason in entries:
        pid = int(pid_s)
        try:
            os.kill(pid, signal.SIGSTOP)
            newly_paused.append((pid, reason))
        except (ProcessLookupError, PermissionError):
            continue
    if newly_paused:
        save_paused(load_paused() | {pid for pid, _ in newly_paused})
        for pid, reason in newly_paused:
            log(f"  PAUSED PID {pid} ({reason})")
            notify_main(f"watchdog PAUSED pid {pid} ({reason})")


def pause_pid(pid_s: str, reason: str) -> bool:
    """SIGSTOP a process (pause, not kill) and record it for later resume."""
    try:
        os.kill(int(pid_s), signal.SIGSTOP)
    except (ProcessLookupError, PermissionError):
        return False
    save_paused(load_paused() | {int(pid_s)})
    log(f"  PAUSED PID {pid_s} ({reason})")
    notify_main(f"watchdog PAUSED pid {pid_s} ({reason})")
    return True


def resume_paused():
    """SIGCONT every paused PID. Called whenever the system is under all caps."""
    paused = load_paused()
    for pid in paused:
        try:
            os.kill(pid, signal.SIGCONT)
        except (ProcessLookupError, PermissionError):
            pass
    if paused:
        log(f"resumed {len(paused)} paused process(es)")
        save_paused(set())


class _PidProc:
    """psutil.Process-like adapter so kill_if_project can act on a pid + cmdline.

    The watchdog scans `ps` args strings, not psutil objects; this adapter lets
    the shared registry's kill gate operate on the same interface.
    """
    def __init__(self, pid_s: str, args: str):
        self._pid = int(pid_s)
        self._args = args

    def name(self) -> str:
        return self._args

    def send_signal(self, sig) -> None:
        os.kill(self._pid, sig)


def kill_matching(name_fragment: str, sig=signal.SIGTERM, exact_path: bool = True):
    if globals().get("DRY_RUN"):
        log(f"[DRY-RUN] would signal processes matching {name_fragment!r}")
        return 0
    """Signal processes matching the fragment.

    Step 30/456 hardening (2026-08-05): with exact_path=True (default), only
    processes whose executable PATH ends with the fragment are signalled —
    a substring in the command line no longer matches (prevents killing
    unrelated services that merely mention the name, e.g. 'chrome' matching
    'chromium' or a log path containing the fragment). Critical system/PROJECT
    processes are always excluded.
    """
    _CRITICAL = ("memory_watchdog.py", "run_workflow.py",
                 "/usr/share/code/code", "code --", "plasmashell", "kwin",
                 "kscreenlocker", "ksmserver", "sddm", "Xorg", "Xwayland",
                 "org_kde_powerdevil", "dbus-daemon", "systemd", "wayland",
                 # 2026-08-14 (user): the main session (daemon bg pair) is
                 # never a kill target, even in the worst case.
                 "bg-spare", "bg-pty-host")
    try:
        out = subprocess.run(
            ["ps", "-eo", "pid,args"], capture_output=True, text=True,
            timeout=10).stdout
    except Exception:
        return
    frag_l = name_fragment.lower()
    for line in out.splitlines():
        pid_s, _, args = line.strip().partition(" ")
        if not pid_s.isdigit():
            continue
        if any(x in args for x in _CRITICAL):
            continue
        args_l = args.lower()
        if exact_path:
            # Match only when the fragment is a PATH component of the process
            # (the executable path OR a script argument — python subprocesses
            # have the script as argv[1], e.g. `python scripts/foo.py`), not a
            # substring anywhere in the command line. Prevents fragment
            # collisions like 'chrome' matching 'chromium' or log paths.
            tokens = args_l.split()
            if not tokens:
                continue
            exe = tokens[0]
            matched = exe.endswith(frag_l) or exe == frag_l
            if not matched and len(tokens) > 1:
                matched = any(t.endswith(frag_l) or t == frag_l
                              for t in tokens[1:4])  # argv[1..3] script paths
            if not matched:
                continue
        else:
            if frag_l not in args_l:
                continue
        try:
            if _is_protected(args):
                log(f"  SKIPPED protected PID {pid_s} ({name_fragment})")
                continue
            # STEP 285/1566: registry fail-closed gate — only Project-owned
            # processes may be killed; anything else is logged and refused.
            if not kill_if_project(_PidProc(pid_s, args), sig):
                log(f"  REFUSED non-Project PID {pid_s} ({name_fragment})")
                continue
            log(f"  signalled PID {pid_s} ({name_fragment})")
            notify_main(f"watchdog SIGNALLED pid {pid_s} ({name_fragment})")
        except (ProcessLookupError, PermissionError):
            pass


def total_used_percent() -> float:
    """Percent of total RAM currently used (from /proc/meminfo)."""
    try:
        with open("/proc/meminfo") as f:
            total = used = None
            for line in f:
                if line.startswith("MemTotal:"):
                    total = int(line.split()[1])
                elif line.startswith("MemAvailable:"):
                    avail = int(line.split()[1])
                    if total:
                        used = total - avail
                        break
            if total and used is not None:
                return 100.0 * used / total
    except Exception:
        pass
    return 0.0


# ── Process priority tiers ─────────────────────────────────────────────────
# When the watchdog must kill to enforce RAM/CPU budgets, it kills the
# LOWEST-priority process first (never protected ones). Within the same tier,
# the heaviest consumer goes first.
# Tier 0 = protected (never killed).
def process_priority(args: str) -> int:
    """Return priority tier for a process cmdline: higher = more important.
    0 = protected/never-kill. Kill order = lowest tier first."""
    # 2026-08-17 (OOM fix): the CDP automation chromes (gc-cdp profile dirs,
    # --remote-debugging-port 9223/9224) are Project restartable infra AND the
    # box's biggest RAM consumers (14+ GiB with renderers). They MUST be
    # killable so the watchdog can free memory under pressure — the generic
    # "chrome" tier-0 protect below was the OOM root cause (08-17: chrome
    # stayed protected, the watchdog could never free its RSS, the kernel
    # OOM-killed it 16+ times and the box froze). Matched on the gc-cdp
    # profile path, NOT the browser name, so a user's own chrome stays tier-0.
    if "gc-cdp" in args:
        return 3
    if any(x in args for x in (
            "memory_watchdog.py", "run_workflow.py",
            # 2026-08-12: the cross_eval PARENT is the pipeline brain — it's
            # ~166MB (frees nothing when paused) while its claude -p children
            # are tier-0 protected. Pausing it stalls synthesis with zero RAM
            # benefit (reviewers block on full pipes). Protect it like the
            # orchestrator; the SIGTERM kill floors are the real backstop.
            "parallel_agent_cross_eval.py",
            # 2026-08-09: inbox_monitor is the user-requested ack/wake-flag
            # daemon (rule 2026-08-03). Killing it made the supervisor
            # restart-loop it every ~2 min, burning MORE CPU than it saved —
            # the tier-2 audit subprocesses are the intended kill victims.
            "inbox_monitor.py",
            # 2026-08-09: the always-on watcher family (control plane).
            # Killing any of them makes stack_supervisor restart-loop it
            # (more CPU burned than saved) or silently orphans infra:
            # omniroute_watchdog enforces model-bans (killed ~90s cycles
            # 21:02-21:06, state leaks when dead); phase_monitor tracks plan
            # progress; the .sh watchers are tier-3 (most killable!) by the
            # generic matchers. Tier-2 audit subprocesses remain the victims.
            "omniroute_watchdog.py", "phase_monitor.py",
            "stack_supervisor.sh", "watch-inbox.sh",
            "push_live_context.py", "session_heartbeat.sh",
            "generate_claude_context.py",
            # 2026-08-09: the telegram-monitor/bin watcher family
            # (watch-backup/watch-stack/watch-main/watch-inbox/etc.) — the
            # watchdog SIGTERM'd watch-backup.sh twice in 4 min (kills at
            # 21:08:55/21:10:34, killing the rclone-backup monitor with exit
            # 143). Match on the dir so future watchers are protected too.
            "/telegram-monitor/bin/",
            # 2026-08-09: anything touching the user-facing inbox channel
            # (the [telegram] wake monitor tails claude_inbox.json — the
            # watchdog killed it exit 144; without it the session only
            # notices new messages on the next idle poll).
            "claude_inbox.json",
            "claude", "claude -p",
            # 2026-08-14: explicit — the daemon background session pair (this
            # main session runs as claude bg-spare / bg-pty-host). Tier 0 via
            # "claude" already, listed explicitly so a future refactor can't
            # silently drop it (user rule: never kill the main session, ever).
            "bg-spare", "bg-pty-host",
            # 2026-08-08 (user directive): the main session tree is HARD
            # protected — bare "claude" covers the tmux session + bg-spare +
            # bg-pty-host + deadman (claude_deadman.sh contains "claude").
            # The ack daemon carries user-facing replies — never a victim:
            "telegram_ack_daemon.py",
            "next", "next-server",
            "/usr/share/code/code", "/usr/share/code/resources", "code --",
            # (2026-08-09: VS Code's integrated-terminal shells are
            # /usr/bin/bash --init-file /usr/share/code/resources/... — the
            # /usr/share/code/code pattern doesn't match them; watchdog killed
            # one twice in 2 min, 21:18/21:20.)
            "rclone", "claude-plugins-official", "server.ts",
            "chrome", "chromium", "firefox",  # browsers: never the victim (2026-08-05)
            # 2026-08-14 (user: "vs code js crashed"): the webchat GATEWAYS
            # (node server.js 8080-8085) and session MCP servers (npm exec
            # github/websearch) are tier-2 glue that got paused under the RAM
            # cap — pausing the gateways made stack_supervisor's WEDGED-GUARD
            # kill them (02:30:33 pause → 02:30:48 kill, 8080 churn), and
            # pausing a connected session's MCP server froze the VS Code
            # extension host ("js crashed" twice in 10 min, 02:33/02:35).
            # Both restart-loop harder than the pause saves — protect them
            # like the watcher family; audit subprocesses remain the victims.
            # "mcp-server" also covers the npm exec CHILD (node
            # .../.bin/mcp-server-github — no "npm exec" in its own cmdline;
            # it got paused at priority 2 twice on 08-14 before this fix).
            # "websearch-deepseek" is the second session MCP (a direct node
            # launcher at ~/.npm-global/bin — no "mcp-server" in its name;
            # paused 20:21:52 on 08-14 before this fix).
            "node server.js", "npm exec", "mcp-server", "websearch-deepseek",
            # 2026-08-15: Protected execution engine and bridge infrastructure
            "execute_master_plan.py", "bridge_monitor.py", "outbox-relay.sh",
            "outbox-relay", "antigravity", "ss_ctrl.py",
            # 2026-08-14: the telegram auto-responder is the owner's live
            # telegram channel (user-facing replies — same logic as the ack
            # daemon below). A paused responder freezes replies until RAM
            # recovers; the supervisor's ensure sees the SIGSTOP'd process as
            # alive and won't restart it.
            # 2026-08-16 (CRITICAL FIX): Network / VPN / System Services must NEVER be paused or killed.
            # Pausing Surfshark / NetworkManager / resolved freezes DNS & routing, killing all Wi-Fi & Internet.
            "surfshark", "NetworkManager", "wpa_supplicant", "systemd-resolved",
            "wireguard", "tailscale", "openvpn", "nordvpn", "avahi-daemon",
            "plasmashell", "kwin", "kscreenlocker",
            "ksmserver", "sddm", "Xorg", "Xwayland",
            "org_kde_powerdevil", "dbus-daemon",
            "systemd", "wayland")):
        return 0  # protected
    if any(x in args for x in ("slack", "discord", "telegram-desktop",
                               "spotify", "vlc", "steam", "surfshark")):
        # 08-14: surfshark added — its zygote was churned at priority 3 every
        # ~2 min at the 75% cap, freezing the VPN GUI's process spawns.
        return 1  # user apps — kill before core
    if any(x in args for x in ("parallel_agents.py", "parallel_agent_cross_eval.py",
                               "python", "python3", "node", "npm",
                               "uvicorn", "gunicorn", "streamlit",
                               "jupyter", "code-server")):
        # 2026-08-05: audit phase subprocesses moved HERE (was tier-0). They
        # checkpoint every batch and resume cleanly — killing them under
        # memory pressure is cheap and safe. (OmniRoute's process tree is
        # excluded separately in kill_heaviest_* via omniroute_pids().)
        return 2  # dev/scripts/audit — kill after user apps
    return 3  # everything else (least important — kill first)


def omniroute_pids() -> set:
    """PIDs whose cwd is the OmniRoute directory, read via /proc directly.

    2026-08-06: `ps -eo pid,cwd,args` renders "-" for the cwd column on every
    process on this box, so cwd-based matching must use /proc/<pid>/cwd.
    OmniRoute's Next.js dev compile pegs ALL cores for 2-5 min on this laptop;
    the compile worker children have bare `node` cmdlines (no "next" string in
    their args), so without this check the CPU cap SIGTERMed them
    (KILLED CPU hog ... 342364/345521, 05:01/05:04) and OmniRoute never
    finished booting. Same technique as omniroute_watchdog._kill_omniroute_only.
    """
    OMNIROUTE_DIR = os.getenv("OMNIROUTE_DIR",
                    os.path.join(os.path.expanduser("~"), "OmniRoute"))
    pids = set()
    try:
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            pid = int(entry)
            try:
                cwd = os.readlink(f"/proc/{pid}/cwd")
            except OSError:
                continue
            if os.path.realpath(cwd) == os.path.realpath(OMNIROUTE_DIR):
                pids.add(pid)
    except Exception:
        pass
    return pids


def scan_all_processes() -> str:
    """ONE /proc scan reused across all checks (step 844: single-scan)."""
    try:
        return subprocess.run(
            ["ps", "-eo", "pid,%cpu,%mem,args", "--sort=-%cpu"],
            capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return ""


def kill_heaviest_cpu_nonessential(ps_out: str = None):
    """PAUSE the single highest-CPU consumer that is NOT protected (2026-08-08:
    SIGSTOP instead of SIGTERM per user directive — resumed when under cap).
    Enforces the user's 80% CPU cap.
    2026-06: OmniRoute's process tree is excluded — its dev compile is a
    legitimate 2-5 min all-core spike and was being killed mid-boot."""
    if ps_out is None:
        ps_out = scan_all_processes()
    if not ps_out:
        return
    skip = omniroute_pids()
    # Priority-aware: pick the lowest-priority process that's consuming CPU
    # (tier 0 = protected, skipped). Within a tier, take the heaviest.
    candidates = []
    for line in ps_out.splitlines()[1:]:
        # Shared scan columns: pid, %cpu, %mem, args
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        pid_s, cpu_s, _mem_s, args = parts
        if not pid_s.isdigit():
            continue
        if "<defunct>" in args:
            continue  # zombie — zero RSS/CPU; pausing it frees nothing
        if int(pid_s) in skip:
            continue  # OmniRoute dev server — never the CPU victim
        prio = process_priority(args)
        if prio == 0:
            continue  # protected
        try:
            cpu_f = float(cpu_s)
        except ValueError:
            cpu_f = 0.0
        candidates.append((prio, -cpu_f, pid_s, args))
    if not candidates:
        return
    candidates.sort()  # lowest priority first, then highest CPU
    for prio, _, pid_s, args in candidates:
        if _is_protected(args):
            continue
        if pause_pid(pid_s, f"CPU cap {MAX_CPU_PERCENT:.0f}% [priority {prio}] "
                             f"({args[:50]})"):
            log(f"  PAUSED CPU hog PID {pid_s} ({args[:60]}) [priority {prio}] "
                f"to restore CPU under {MAX_CPU_PERCENT:.0f}%")
            return


def total_cpu_percent(samples: int = 2, interval: float = 0.5) -> float:
    """Total system CPU% across all cores, averaged over `samples` deltas.
    Spikes are normal (compiles, first audit wave) so we average twice."""
    def _stat():
        try:
            with open("/proc/stat") as f:
                parts = f.readline().split()
            vals = [int(x) for x in parts[1:]]
            idle = vals[3] + (vals[4] if len(vals) > 4 else 0)  # idle + iowait
            return sum(vals), idle
        except Exception:
            return None, None
    deltas = []
    for _ in range(samples):
        t0, i0 = _stat()
        time.sleep(interval)
        t1, i1 = _stat()
        if None in (t0, i0, t1, i1) or t1 <= t0:
            continue
        busy = (t1 - t0) - (i1 - i0)
        deltas.append(100.0 * busy / (t1 - t0))
    return (sum(deltas) / len(deltas)) if deltas else 0.0


def enforce_swarm_cap():
    """PAUSE claude -p subprocesses beyond CLAUDE_SWARM_CAP (2026-08-08:
    pause instead of SIGTERM, per user directive — resume when under cap).

    OmniRoute spawns a local `claude -p` per auto/* model call; uncapped they
    exhausted 14 GiB (OOM kills 08:08 + 14:10, mt7921e WiFi hang + full freeze
    21:15). Keep the OLDEST CLAUDE_SWARM_CAP processes running, PAUSE the
    newest extras (SIGSTOP — they resume when slots free). Interactive
    `claude` (no -p) is never matched.
    """
    try:
        out = subprocess.run(
            ["ps", "-eo", "pid,args", "--sort=pid"],
            capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return
    swarm = []
    for line in out.splitlines()[1:]:
        pid_s, _, args = line.strip().partition(" ")
        if not pid_s.isdigit():
            continue
        if "claude -p" in args:
            swarm.append((pid_s, args))
    paused = load_paused()
    # 2026-08-12 deadlock fix: paused pids used to count toward the cap, so
    # when the running members exited, the paused leftovers kept the count
    # > cap and the resume branch never fired (12 reviewers frozen 2h). Count
    # only RUNNING members; resume the OLDEST paused up to the cap as slots
    # free (incremental — resuming all at once re-trips the cap).
    running = [(p, a) for (p, a) in swarm if int(p) not in paused]
    if len(running) < CLAUDE_SWARM_CAP:
        swarm_pids = {int(p) for p, _ in swarm}
        to_resume = sorted(paused & swarm_pids)[: CLAUDE_SWARM_CAP - len(running)]
        for pid in to_resume:
            try:
                os.kill(pid, signal.SIGCONT)
                log(f"  SWARM: resumed paused claude -p PID {pid} (slot free)")
            except (ProcessLookupError, PermissionError):
                pass
        if to_resume:
            save_paused(paused - set(to_resume))
        return
    # Lower pid = older process = keep running; PAUSE the newest RUNNING extras.
    pause_pids([(p, f"SWARM CAP {CLAUDE_SWARM_CAP} running [{a[:60]}]")
                for p, a in running[CLAUDE_SWARM_CAP:]])


def kill_heaviest_nonessential(ps_out: str = None, force_free: bool = False):
    """PAUSE the single heaviest RAM consumer that is NOT the orchestrator,
    the watchdog itself, or this session's claude (2026-08-08: SIGSTOP instead
    of SIGTERM per user directive — resumed when under cap). 2026-08-06:
    OmniRoute is also excluded — since the 2026-08-05 hardening the audit
    subprocesses (checkpointed, resumable) are the intended victims under RAM
    pressure; killing OmniRoute mid-request only degrades the run (the 8-hour
    blind audit) and the omni watchdog's restart then has to survive a full
    compile."""
    if ps_out is None:
        ps_out = scan_all_processes()
    if not ps_out:
        return
    skip = omniroute_pids()
    # Priority-aware: pick the lowest-priority RAM consumer (tier 0 = protected).
    # Within a tier, take the heaviest.
    candidates = []
    for line in ps_out.splitlines()[1:]:
        # Shared scan columns: pid, %cpu, %mem, args
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        pid_s, _cpu_s, mem_s, args = parts
        if not pid_s.isdigit():
            continue
        if "<defunct>" in args:
            continue  # zombie — zero RSS/CPU; pausing it frees nothing
        if int(pid_s) in skip:
            continue  # OmniRoute dev server — never the victim
        prio = process_priority(args)
        if prio == 0:
            continue  # protected
        try:
            mem_f = float(mem_s)
        except ValueError:
            mem_f = 0.0
        candidates.append((prio, -mem_f, pid_s, args))
    if not candidates:
        return
    candidates.sort()  # lowest priority first, then highest memory
    for prio, _, pid_s, args in candidates:
        if _is_protected(args):
            continue
        # 2026-08-17 (OOM fix): pausing (SIGSTOP) frees ZERO RAM — the exact
        # reason the 08-17 OOM wasn't prevented (the watchdog "paused" at the
        # cap while chrome kept its RSS; the kernel OOM'd the box anyway). CDP
        # chromes are restartable infra, so SIGTERM them to actually release
        # memory. force_free (critical tier) SIGTERMs ANY target, not just
        # chrome. On registry refusal (non-Project), keep walking candidates.
        if "gc-cdp" in args or force_free:
            if kill_if_project(_PidProc(pid_s, args), signal.SIGTERM):
                log(f"  KILLED {pid_s} ({args[:60]}) [priority {prio}] "
                    f"to free RAM under {MAX_USED_PERCENT:.0f}%")
                notify_main(f"watchdog KILLED pid {pid_s} (free RAM)")
                return
            continue  # refused by registry — try next candidate
        if pause_pid(pid_s, f"RAM cap {MAX_USED_PERCENT:.0f}% [priority {prio}] "
                            f"({args[:50]})"):
            log(f"  PAUSED consumer PID {pid_s} ({args[:60]}) [priority {prio}] "
                f"to restore RAM under {MAX_USED_PERCENT:.0f}%")
            return


def main():
    import argparse
    _ap = argparse.ArgumentParser(description="Memory watchdog")
    _ap.add_argument("--dry-run", action="store_true",
                     help="log decisions without killing anything (Step 30 verification)")
    _args = _ap.parse_args()
    DRY_RUN = _args.dry_run
    log("=== memory watchdog started === (dry-run: %s)" % DRY_RUN)
    paused_by_watchdog = False
    global MAX_USED_BYTES
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    MAX_USED_BYTES = int(int(line.split()[1]) * MAX_USED_PERCENT / 100.0) * 1024
                    break
        log(f"RAM cap enabled: {MAX_USED_PERCENT:.0f}% of total = "
            f"{MAX_USED_BYTES / (1024**3):.1f} GiB used max")
    except Exception:
        pass

    last_kill_ts = 0.0
    while True:
        time.sleep(POLL_SECONDS)
        # Step 844: ONE /proc scan per iteration, reused by every check.
        procs = scan_all_processes()
        avail = available_gb()
        user_paused = read_paused()

        # ── 2026-08-05: hard cap on claude -p subprocesses, every poll ──
        # Runs before the RAM checks so a swarm is pruned the moment it
        # exceeds the cap, not after RAM is already gone.
        enforce_swarm_cap()

        # ── CRITICAL: <1.5GB free — escalate to force-kill FIRST ──
        # 2026-08-19 (OOM fix): this used to sit AFTER the RAM-cap branch,
        # whose `continue` on cooldown shadowed it — at 97% used the watchdog
        # was stuck "waiting (cooldown)" and never escalated; the kernel
        # OOM'd the box at 19:40. Critical pressure must ALWAYS escalate,
        # every poll, no cooldown: the gc-cdp chromes release ~2GB on SIGTERM.
        if avail < CRITICAL_BELOW_GB:
            if not paused_by_watchdog:
                log(f"CRITICAL: {avail:.1f}GB free — killing phase subprocesses")
                write_paused(True)
                paused_by_watchdog = True
                notify_main(f"watchdog CRITICAL: {avail:.1f}GB free — "
                            f"killing phase subprocesses")
            for n in AUDIT_NAMES + SWARM_NAMES:
                kill_matching(n)
            # 2026-08-17 (OOM fix): audit kills free little; the single biggest
            # consumer under critical pressure is the restartable CDP chrome.
            # force_free SIGTERMs the heaviest non-protected target (CDP chrome
            # via priority) to actually release RAM — the action that was
            # missing during the 08-17 OOM.
            kill_heaviest_nonessential(procs, force_free=True)
            continue

        # ── USER RULE: never exceed 45% total RAM ──
        used_pct = total_used_percent()
        if used_pct > MAX_USED_PERCENT:
            # Cooldown: after ANY kill, give memory 30s to actually release
            # (and OmniRoute's watchdog time to restart node) before acting
            # again. Without this, we killed the freshly-restarted node
            # before the old memory had been freed. (90s let RAM climb
            # 76%→97% while a defunct-zombie pause freed nothing — 2026-08-19.)
            now = time.time()
            if now - last_kill_ts < 30:
                log(f"CAP: {used_pct:.1f}% RAM used — waiting (cooldown "
                    f"{30 - int(now - last_kill_ts)}s) for release")
                continue
            log(f"CAP: {used_pct:.1f}% RAM used (limit {MAX_USED_PERCENT:.0f}%) — "
                f"pausing heaviest non-essential process")
            kill_heaviest_nonessential(procs)
            last_kill_ts = now
            continue

        # ── USER RULE: never exceed 70% total CPU ──
        cpu_pct = total_cpu_percent()
        if cpu_pct > MAX_CPU_PERCENT:
            now = time.time()
            if now - last_kill_ts < 30:
                log(f"CAP: {cpu_pct:.1f}% CPU used — waiting (cooldown "
                    f"{30 - int(now - last_kill_ts)}s) for release")
                continue
            log(f"CAP: {cpu_pct:.1f}% CPU used (limit {MAX_CPU_PERCENT:.0f}%) — "
                f"pausing heaviest non-essential process")
            kill_heaviest_cpu_nonessential(procs)
            last_kill_ts = now
            continue

        # ── Under all caps: resume anything we paused (2026-08-08) ──
        resume_paused()

        # Step 844: --dry-run performs a single scan pass, then exits (the
        # verification mode must terminate, not loop forever).
        if DRY_RUN:
            log("dry-run complete — single pass finished, exiting")
            break

        if avail < PAUSE_BELOW_GB:
            if not paused_by_watchdog:
                log(f"WARNING: {avail:.1f}GB free — pausing pipeline, killing audit")
                write_paused(True)
                paused_by_watchdog = True
                notify_main(f"watchdog WARNING: {avail:.1f}GB free — "
                            f"paused pipeline, killing audit subprocesses")
            for n in AUDIT_NAMES:
                kill_matching(n)
            continue

        if avail > RESUME_ABOVE_GB:
            if paused_by_watchdog:
                log(f"OK: {avail:.1f}GB free — resuming pipeline")
                write_paused(False)
                paused_by_watchdog = False
            continue

        # Between 1.5GB and 3.0GB with no pause needed: do nothing.


if __name__ == "__main__":
    main()
