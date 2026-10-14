#!/usr/bin/python3 -I
"""
JoeDeck Wi-Fi helper - runs as root (via a narrow sudo rule) and talks to NetworkManager.
Reads ONE JSON command on stdin, prints ONE JSON result on stdout. Passwords never appear
on a command line visible to other users, and are never printed back.

  {"cmd": "status"}
  {"cmd": "scan"}
  {"cmd": "list"}                                    saved Wi-Fi networks
  {"cmd": "add", "ssid": "...", "password": "...", "hidden": false, "priority": 10, "connect": false}
  {"cmd": "connect", "ssid": "..."}                  switch now; rolls back if it fails
  {"cmd": "delete", "ssid": "..."}
"""
import json
import os
import re
import subprocess
import sys

NMCLI = os.environ.get("SIGNAGE_NMCLI", "/usr/bin/nmcli")
PREFIX = "signage-wifi-"


class HelperError(Exception):
    pass


def nm(*args, timeout=40, check=True):
    try:
        p = subprocess.run([NMCLI, *args], capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise HelperError("Wi-Fi settings need NetworkManager (standard on Raspberry Pi OS Bookworm and newer).")
    except subprocess.TimeoutExpired:
        raise HelperError("NetworkManager took too long to answer.")
    if check and p.returncode != 0:
        raise HelperError((p.stderr or p.stdout).strip().splitlines()[-1] if (p.stderr or p.stdout).strip()
                          else "nmcli failed")
    return p.stdout


def split_terse(line):
    """nmcli -t output: fields separated by ':' with '\\:' escaping."""
    out, cur, esc = [], "", False
    for ch in line:
        if esc:
            cur += ch
            esc = False
        elif ch == "\\":
            esc = True
        elif ch == ":":
            out.append(cur)
            cur = ""
        else:
            cur += ch
    out.append(cur)
    return out


def wifi_device():
    for line in nm("-t", "-f", "DEVICE,TYPE,STATE", "device").splitlines():
        dev, typ, state = (split_terse(line) + ["", "", ""])[:3]
        if typ == "wifi":
            return dev
    raise HelperError("No Wi-Fi adapter found on this device.")


def saved():
    out = []
    for line in nm("-t", "-f", "NAME,TYPE,AUTOCONNECT-PRIORITY,ACTIVE", "connection", "show").splitlines():
        name, typ, prio, active = (split_terse(line) + ["", "", "", ""])[:4]
        if typ not in ("802-11-wireless", "wifi"):
            continue
        ssid = nm("-g", "802-11-wireless.ssid", "connection", "show", name, check=False).strip() or name
        out.append({"name": name, "ssid": ssid, "priority": int(prio or 0), "active": active == "yes"})
    return sorted(out, key=lambda c: (-c["active"], -c["priority"], c["ssid"].lower()))


def find_saved(ssid):
    for c in saved():
        if c["ssid"] == ssid:
            return c
    return None


def status():
    dev = None
    try:
        dev = wifi_device()
    except HelperError:
        pass
    active = next((c for c in saved() if c["active"]), None)
    signal = None
    if dev:
        for line in nm("-t", "-f", "ACTIVE,SSID,SIGNAL", "device", "wifi", "list", "--rescan", "no",
                       check=False).splitlines():
            a, s, sig = (split_terse(line) + ["", "", ""])[:3]
            if a == "yes":
                signal = int(sig or 0)
    wired = False
    for line in nm("-t", "-f", "TYPE,STATE", "device").splitlines():
        typ, state = (split_terse(line) + ["", ""])[:2]
        if typ == "ethernet" and state == "connected":
            wired = True
    return {"wifi_device": dev, "ssid": active["ssid"] if active else None, "signal": signal, "wired": wired}


def scan():
    wifi_device()
    seen = {}
    for line in nm("-t", "-f", "SSID,SIGNAL,SECURITY", "device", "wifi", "list", "--rescan", "yes",
                   timeout=30).splitlines():
        ssid, sig, sec = (split_terse(line) + ["", "", ""])[:3]
        if ssid and (ssid not in seen or int(sig or 0) > seen[ssid]["signal"]):
            seen[ssid] = {"ssid": ssid, "signal": int(sig or 0), "secure": bool(sec.strip() and sec != "--")}
    return sorted(seen.values(), key=lambda n: -n["signal"])


def connect(ssid):
    target = find_saved(ssid)
    if not target:
        raise HelperError("That network isn't saved on this sign.")
    previous = next((c for c in saved() if c["active"]), None)
    p = subprocess.run([NMCLI, "--wait", "30", "connection", "up", target["name"]],
                       capture_output=True, text=True, timeout=60)
    if p.returncode != 0:
        if previous and previous["name"] != target["name"]:
            subprocess.run([NMCLI, "--wait", "30", "connection", "up", previous["name"]],
                           capture_output=True, text=True, timeout=60)
        raise HelperError("Couldn't connect to “%s” (wrong password or out of range). Stayed on the previous network."
                          % ssid)
    return {"connected": ssid}


def add(ssid, password, hidden=False, priority=10, connect_now=False):
    if not ssid or len(ssid.encode()) > 32:
        raise HelperError("Network name must be 1-32 characters.")
    if password and not (8 <= len(password) <= 63):
        raise HelperError("Wi-Fi passwords are 8-63 characters (leave empty for an open network).")
    dev = wifi_device()
    existing = find_saved(ssid)
    if existing and not existing["active"]:
        nm("connection", "delete", existing["name"])  # replace it with fresh settings
        existing = None
    if existing:  # in use right now: update it in place so the sign stays connected
        args = ["connection", "modify", existing["name"]]
    else:
        name = PREFIX + re.sub(r"[^A-Za-z0-9_.-]", "_", ssid)[:40]
        args = ["connection", "add", "type", "wifi", "con-name", name, "ifname", dev, "ssid", ssid]
    args += ["connection.autoconnect", "yes", "connection.autoconnect-priority", str(int(priority)),
             "802-11-wireless.hidden", "yes" if hidden else "no"]
    if password:
        args += ["wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", password]
    nm(*args)
    result = {"saved": ssid}
    if connect_now:
        try:
            result.update(connect(ssid))
        except HelperError:
            if not existing:  # don't keep a brand-new network that didn't work (probably a typo)
                nm("connection", "delete", args[args.index("con-name") + 1], check=False)
            raise
    return result


def delete(ssid):
    c = find_saved(ssid)
    if not c:
        raise HelperError("That network isn't saved on this sign.")
    if c["active"] and not status()["wired"]:
        raise HelperError("This sign is using that network right now. Connect it to another network first.")
    nm("connection", "delete", c["name"])
    return {"deleted": ssid}


def main():
    try:
        req = json.loads(sys.stdin.read() or "{}")
        cmd = req.get("cmd")
        if cmd == "status":
            res = status()
        elif cmd == "scan":
            res = {"networks": scan()}
        elif cmd == "list":
            res = {"saved": [{k: c[k] for k in ("ssid", "priority", "active")} for c in saved()]}
        elif cmd == "add":
            res = add(str(req.get("ssid", "")), str(req.get("password", "")), bool(req.get("hidden")),
                      int(req.get("priority", 10)), bool(req.get("connect")))
        elif cmd == "connect":
            res = connect(str(req.get("ssid", "")))
        elif cmd == "delete":
            res = delete(str(req.get("ssid", "")))
        else:
            raise HelperError("Unknown command")
        print(json.dumps({"ok": True, **res}))
    except HelperError as e:
        print(json.dumps({"ok": False, "error": str(e)}))
    except Exception as e:  # never leak a traceback with secrets in it
        print(json.dumps({"ok": False, "error": "Wi-Fi helper failed: %s" % type(e).__name__}))


if __name__ == "__main__":
    main()
