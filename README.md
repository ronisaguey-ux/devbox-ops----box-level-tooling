# devbox-ops

Small, hardened operational tools for a personal Linux dev box: RAM/CPU
guardian, LLM-router watchdog, VPN control, and encrypted-Drive backups.
All battle-tested in production since 2026-08 (including surviving an
out-of-memory crash that killed the whole app stack — the RAM guardian is
the reason the box came back).

## Components

| File | What it does |
|---|---|
| `memory_watchdog.py` | Watches system RAM every 10s. Over budget → **pauses** (SIGSTOP) the lowest-priority heaviest process instead of killing it; only true OOM-critical state still SIGTERMs, and never touches protected processes (session, desktop, browsers). Also caps parallel `claude -p` swarm count |
| `omniroute_watchdog.py` | Keeps an [OmniRoute](https://github.com/kgantsov/omniroute)-style LLM gateway alive: probes `/api/v1/models`, restarts on repeated failure. Kills **only** processes whose cwd is the gateway dir (never other apps) |
| `ss_ctrl.py` | Programmatic Surfshark VPN control through the app's CDP/`__ipcProxy__` bridge: `status`, `connect`, `disconnect`, `rotate`, `countries` — with egress-IP verification on every operation |
| `rclone_backup.sh` | Cron-friendly backup of a project tree to an rclone remote (Drive/S3/anything), `--update` + tps-limited to avoid quota 403s |

## Setup

```bash
# RAM guardian (autostart it — it's the reason the box survives)
python3 memory_watchdog.py &

# Gateway watchdog
python3 omniroute_watchdog.py &

# VPN control (requires the Surfshark GUI running with CDP port 9222)
python3 ss_ctrl.py status
python3 ss_ctrl.py rotate          # disconnect → random country → connect
python3 ss_ctrl.py connect us      # fastest server in the US

# Backups (cron: 47 1 * * * rclone_backup.sh)
./rclone_backup.sh
```

## Environment

| Variable | Used by | Default |
|---|---|---|
| `WORKFLOW_STATE` | memory_watchdog | Path of the pipeline state file to pause/resume (`~/.claude/channels/telegram/workflow_state.json`) |
| `OMNIROUTE_DIR` | omniroute_watchdog, memory_watchdog | Directory of the gateway (`~/OmniRoute`) |
| `OMNIROUTE_PORT` | omniroute_watchdog | Gateway port (`20128`) |
| `OMNIROUTE_PROBE_URL` | omniroute_watchdog | Full probe URL (defaults to `http://localhost:$OMNIROUTE_PORT/api/v1/models`) |
| `REPO_DIR` | rclone_backup | Directory to back up (`$HOME/project`) |
| `REMOTE` / `REMOTE_PATH` | rclone_backup | rclone remote name + target path (`backup:workspace/project`) |
| `CLAUDE_SWARM_CAP` | memory_watchdog | Max parallel `claude -p` subprocesses (`4`) |

## How memory_watchdog behaves

```
available > 3.5 GB   → normal; ensure pipeline unpaused
available < 2.5 GB   → stressed: pause pipeline + SIGTERM audit subprocesses
available < 1.5 GB   → critical: SIGTERM heavy phase processes (never the session)
over CLAUDE_SWARM_CAP → SIGSTOP the newest claude -p subprocesses; SIGCONT when under
```

Everything is reversible: pauses write to the workflow state file the same
way a manual `/pause` command would, and the watchdog logs every action to
`/tmp/memory_watchdog.log`.

## Design notes (learned the hard way)

- **Pause, don't kill**: a SIGSTOP'd process costs ~0 RAM and resumes
  instantly; killing browsers to save RAM logs the user out of their desktop
  (real incident 2026-08-03).
- **Never match by cmdline substring**: `pkill -f next.*dev` kills every
  Next.js server on the box. Both watchdogs match by **process cwd via
  `/proc/<pid>/cwd`** (real incident 2026-08-03/06).
- **`ps -eo cwd` lies** (renders `-` on this box) — always read `/proc`
  directly (incident 2026-08-06).
- **Grace windows beat restart loops**: a dev server can take 2-5 min to
  compile before its port binds; a 90s grace caused a watchdog restart-loop
  (incident 2026-08-06).
- **Backups must be tps-limited**: parallel rclone to Drive triggers 403
  quota errors; `--tpslimit 10` keeps it clean.

## License

MIT
