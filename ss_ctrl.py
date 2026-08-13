#!/usr/bin/env python3
"""ss_ctrl.py — programmatic Surfshark control via the app's IPC bridge (CDP).

The Surfshark GUI exposes __ipcProxy__ (renderer -> main process IPC). The
daemon accepts ONE app client, so the GUI must be the only instance running
(launch via ss_cdp_real.sh — real profile so the account is logged in).

Usage:
  ss_ctrl.py status                 egress IP + UI connect state
  ss_ctrl.py connect [CC]           connect (fastest, or fastest-in-country CC)
  ss_ctrl.py disconnect
  ss_ctrl.py rotate [CC...]         disconnect + random country + connect
  ss_ctrl.py countries              list available country codes
"""
import json, random, subprocess, sys, time, urllib.request
import websocket

CDP = "http://localhost:9222"


def page_ws():
    with urllib.request.urlopen(CDP + "/json", timeout=5) as r:
        targets = json.load(r)
    for t in targets:
        if t.get("type") == "page":
            return t["webSocketDebuggerUrl"]
    raise SystemExit("Surfshark CDP not reachable — is the app running? (launch it with the remote-debugging port 9222 enabled)")


class App:
    def __init__(self):
        self.ws = websocket.create_connection(page_ws(), timeout=30)
        self.pid = 0

    def _eval(self, expr, await_promise=True):
        self.pid += 1
        self.ws.send(json.dumps({"id": self.pid, "method": "Runtime.evaluate",
                                 "params": {"expression": expr, "returnByValue": True,
                                            "awaitPromise": await_promise}}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == self.pid:
                res = msg.get("result", {})
                if "exceptionDetails" in res:
                    return {"__exc": res["exceptionDetails"].get("text", "exception")}
                v = res.get("result", {})
                if v.get("value") is not None:
                    return v["value"]
                return v if v else None

    def _loc_list(self):
        r = self._eval("window.__loclist ? (window.__loclist.locations || []) : null")
        if not r:
            self._eval("(() => { window.__loclist = null; window.__ipcProxy__.vpnLocationList.onLocationListChanged(d => { window.__loclist = d; }); window.__ipcProxy__.vpnLocationList.refreshLocationList(); return 1; })()")
            time.sleep(4)
            r = self._eval("window.__loclist ? (window.__loclist.locations || []) : null")
        return r or []

    def countries(self):
        locs = self._loc_list()
        ccs = sorted({l.get("countryCode") for l in locs if l.get("countryCode")})
        return ccs

    def connect(self, cc=None):
        if cc:
            locs = self._loc_list()
            cand = [l for l in locs if l.get("countryCode", "").upper() == cc.upper()]
            if not cand:
                raise SystemExit(f"country {cc} not in location list")
            t = random.choice(cand)
            setting = {"type": "fastest",
                       "target": {"id": t["id"], "type": t.get("type", "generic"),
                                  "countryCode": t["countryCode"]}}
        else:
            setting = {"type": "suggest", "target": "fastest"}
        # intent:quick_connect is required — bare {setting} silently no-ops
        r = self._eval(f"window.__ipcProxy__.shark.connect({{setting:{json.dumps(setting)},intent:'quick_connect'}}).then(()=>'ok',e=>'ERR:'+e.message)")
        return r

    def disconnect(self):
        return self._eval("window.__ipcProxy__.shark.disconnect().then(()=>'ok',e=>'ERR:'+e.message)")

    def button(self):
        b = self._eval("(()=>{const b=document.querySelector('SPAN.aAecZ');return b?b.textContent.trim():'n/a'})()")
        return b


def egress():
    try:
        out = subprocess.run(["curl", "-s", "-m", "8", "https://ipinfo.io/ip"],
                             capture_output=True, text=True, timeout=12).stdout.strip()
        return out or "?"
    except Exception:
        return "?"


def main():
    args = sys.argv[1:]
    cmd = args[0] if args else "status"
    app = App()
    if cmd == "status":
        print(f"egress={egress()} ui={app.button()}")
    elif cmd == "connect":
        cc = args[1] if len(args) > 1 else None
        print("before:", egress())
        r = app.connect(cc)
        print("call:", r)
        time.sleep(12)
        print("after:", egress())
    elif cmd == "disconnect":
        r = app.disconnect()
        time.sleep(6)
        print("call:", r, "| egress:", egress())
    elif cmd == "rotate":
        ccs = args[1:] or None
        home = egress()
        app.disconnect()
        time.sleep(4)
        ok = False
        for attempt in range(3):
            cc = random.choice(ccs) if ccs else None
            r = app.connect(cc)
            time.sleep(12)
            after = egress()
            # a real VPN exit: not the home IP, and changed from previous exit
            if after and after != home and (attempt == 0 or after != before):
                ok = True
                break
            before = after
        print(f"rotate {'OK' if ok else 'FAIL'} home={home} -> {after} (attempts={attempt + 1})")
    elif cmd == "countries":
        print(" ".join(app.countries()))
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
