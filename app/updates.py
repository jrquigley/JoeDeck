"""Checks GitHub for a newer JoeDeck release and starts the updater."""
import json
import logging
import os
import subprocess
import threading
import time
import urllib.request

import store

BASE = os.path.dirname(os.path.abspath(__file__))
CHECK_EVERY = 6 * 3600
log = logging.getLogger("signage.updates")

state = {"latest": None, "checked": None, "error": None, "notes": "", "url": ""}
_lock = threading.Lock()


def repo():
    try:
        with open(os.path.join(BASE, "release.json")) as f:
            return json.load(f).get("repo") or ""
    except (OSError, ValueError):
        return ""


def api_url():
    return os.environ.get("SIGNAGE_RELEASE_API") or "https://api.github.com/repos/%s/releases/latest" % repo()


def vtuple(v):
    out = []
    for part in str(v or "").lstrip("vV").split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        out.append(int(digits) if digits else 0)
    return tuple(out + [0] * (3 - len(out)))


def is_newer(latest, current):
    return bool(latest) and vtuple(latest) > vtuple(current)


def check():
    if not repo() and not os.environ.get("SIGNAGE_RELEASE_API"):
        with _lock:
            state.update(error="No GitHub repository configured", checked=time.time())
        return dict(state)
    req = urllib.request.Request(api_url(), headers={"User-Agent": "joedeck", "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
        with _lock:
            state.update(latest=str(data.get("tag_name", "")).lstrip("vV") or None, error=None,
                         notes=str(data.get("body") or "")[:4000], url=data.get("html_url") or "",
                         checked=time.time())
    except Exception as e:  # offline, rate-limited, no releases yet...
        msg = "No releases published yet" if "404" in str(e) else "Couldn't reach GitHub (%s)" % str(e)[:80]
        with _lock:
            state.update(error=msg, checked=time.time())
    return dict(state)


def install_status():
    """What the updater last reported, if it's recent."""
    s = store.read_json(store.path("update-status.json")) or {}
    if not s or time.time() - s.get("at", 0) > 3 * 3600:
        return None
    return s


def start_update():
    cmd = ([] if os.environ.get("SIGNAGE_NO_SUDO") else ["sudo", "-n"]) + [
        os.environ.get("SIGNAGE_UPDATER", "/opt/signage/update.sh")]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout or "updater failed").strip()[-300:])


def _loop():
    time.sleep(60)  # let the network come up after boot
    while True:
        check()
        time.sleep(CHECK_EVERY)


def start():
    threading.Thread(target=_loop, daemon=True, name="update-check").start()
