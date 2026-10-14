#!/usr/bin/env python3
"""
JoeDeck - web backend for a Raspberry Pi digital sign (runs on every Pi).

  /player        full-screen page the Pi's kiosk browser shows on boot (public)
  /api/now       what the player should show right now (public, polled every 5 s)
  /admin         management UI - log in on ANY Pi to manage every sign you have access to
  /api/...       this Pi's content + the shared users / network (login required)
  /d/<id>/...    the same API on another Pi, relayed with a signed request
  /cluster/...   Pi-to-Pi endpoints (signed with the network key)

Content (images, playlists, schedule, display settings) belongs to each Pi.
Users and the list of signs are shared by all Pis (see cluster.py).
Every change is written to this Pi's activity log (see audit.py).
"""
import argparse
import copy
import datetime as dt
import functools
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import uuid

from flask import Flask, Response, g, jsonify, redirect, request, send_from_directory, session
from werkzeug.security import check_password_hash, generate_password_hash

import audit
import cluster
import store
import updates

try:
    from PIL import Image, ImageOps
except ImportError:  # Pillow is optional; without it images are stored as-is.
    Image = None

BASE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(BASE, "static")
MEDIA = store.path("media")
THUMBS = store.path("thumbs")
CONFIG = store.path("config.json")

ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
MAX_EDGE = int(os.environ.get("SIGNAGE_MAX_EDGE", "1920"))  # shrink big photos so the Pi 3 stays smooth
THUMB_EDGE = 360
ORIENTATIONS = ("landscape", "portrait", "landscape-flipped", "portrait-flipped")
ORIENTATION_NAMES = {"landscape": "horizontal", "portrait": "vertical",
                     "landscape-flipped": "horizontal (upside-down)", "portrait-flipped": "vertical (other way)"}
FITS = ("contain", "cover")
TRANSITIONS = ("fade", "slide", "none")
ROLES = ("admin", "editor")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
USER_RE = re.compile(r"^[a-z0-9_.-]{1,32}$")

DEFAULT_CONFIG = {
    "settings": {
        "mode": "playlist",          # "playlist" (default playlist + schedule) or "single"
        "single_media": None,        # media id shown forever when mode == "single"
        "orientation": "landscape",
        "fit": "contain",            # contain = whole image visible, cover = fill screen (crops)
        "background": "#000000",
        "transition_ms": 800,
        "transition_style": "fade",  # fade | slide | none
        "default_duration": 10,
    },
    # images: {id, type:"image", file, thumb, name, width, height, added}
    # websites: {id, type:"web", url, name, zoom, refresh_min, added}
    "media": [],
    "playlists": [{"id": "main", "name": "Main", "items": [], "shuffle": False}],  # items: [{media, duration}]
    "default_playlist": "main",
    "schedules": [],                 # first matching enabled rule wins
    "reload_token": 0,
}

app = Flask(__name__, static_folder=STATIC, static_url_path="/static")
app.config.update(MAX_CONTENT_LENGTH=100 * 1024 * 1024, SESSION_COOKIE_HTTPONLY=True,
                  SESSION_COOKIE_SAMESITE="Lax", PERMANENT_SESSION_LIFETIME=dt.timedelta(days=30))
_lock = threading.RLock()


# =========================================================================== content storage

def load_config():
    with _lock:
        cfg = store.read_json(CONFIG) or copy.deepcopy(DEFAULT_CONFIG)
        for k, v in DEFAULT_CONFIG.items():
            cfg.setdefault(k, copy.deepcopy(v))
        for k, v in DEFAULT_CONFIG["settings"].items():
            cfg["settings"].setdefault(k, v)
        return cfg


def save_config(cfg):
    with _lock:
        store.write_json(CONFIG, cfg)


def mutate(fn):
    """Load config, apply fn(cfg), save, return fn's result - all under the lock."""
    with _lock:
        cfg = load_config()
        result = fn(cfg)
        save_config(cfg)
        return result


# =========================================================================== users & auth

def get_user(name):
    if not name:
        return None
    with cluster.lock:
        rec = cluster.state["users"].get(name)
    if not rec or rec.get("deleted"):
        return None
    return dict(rec, username=name)


def can_access(user, dev_id):
    devs = user.get("devices") or []
    return user["role"] == "admin" or "*" in devs or dev_id in devs


def count_admins():
    with cluster.lock:
        return sum(1 for u in cluster.state["users"].values() if not u.get("deleted") and u.get("role") == "admin")


def _canon_path():
    qs = request.query_string.decode()
    return request.path + ("?" + qs if qs else "")


def _peer_signature():
    """Valid signature from another Pi on our network -> {node, user, client}, else None."""
    if not request.headers.get("X-Signage-Sig"):
        return None
    body = request.get_data(cache=True) if request.method not in ("GET", "HEAD") else b""
    return cluster.verify_request(request.method, _canon_path(), request.headers, body)


def current_user():
    if "user" in g:
        return g.user
    g.user, g.via, g.client_ip = None, None, request.remote_addr
    peer = _peer_signature()
    if peer and peer["user"]:
        g.user = get_user(peer["user"])
        g.via = cluster.device_name(peer["node"])
        g.client_ip = peer["client"] or request.remote_addr
    elif not request.headers.get("X-Signage-Sig"):
        u = get_user(session.get("user"))
        if u and session.get("pw") == u["password_hash"][-16:]:  # password change logs out old sessions
            g.user = u
    return g.user


def auth(level="user"):
    """level: "user" (any login), "device" (user may manage THIS sign), "admin"."""
    def deco(view):
        @functools.wraps(view)
        def wrapper(*args, **kwargs):
            u = current_user()
            if not u:
                if request.path.startswith(("/api/", "/d/")):
                    return jsonify(error="Please log in again"), 401
                return redirect("/login")
            if level == "admin" and u["role"] != "admin":
                return jsonify(error="Only admins can do that"), 403
            if level == "device" and not can_access(u, cluster.node["id"]):
                return jsonify(error="You don't have access to this sign"), 403
            return view(*args, **kwargs)
        return wrapper
    return deco


def node_only(view):
    """Pi-to-Pi endpoints: must be signed by a member, and not on behalf of a user."""
    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        peer = _peer_signature()
        if not peer or peer["user"]:
            return jsonify(error="Forbidden"), 403
        return view(*args, **kwargs)
    return wrapper


def log_action(action, detail=""):
    u = g.get("user")
    audit.add({"ts": round(time.time(), 3), "device": cluster.node["id"], "device_name": cluster.my_name(),
               "user": u["username"] if u else "-", "action": action, "detail": detail,
               "ip": g.get("client_ip") or request.remote_addr, "via": g.get("via")})


def bad(msg, code=400):
    return jsonify({"error": msg}), code


# =========================================================================== schedule logic

def _minutes(hhmm):
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def rule_matches(rule, now):
    """Days are 0=Mon..6=Sun. A window whose end <= start runs past midnight
    (00:00-00:00 means all day)."""
    if not rule.get("enabled", True):
        return False
    today = now.date()
    if rule.get("start_date") and today.isoformat() < rule["start_date"]:
        return False
    if rule.get("end_date") and today.isoformat() > rule["end_date"]:
        return False
    days = set(rule.get("days") or range(7))
    t = now.hour * 60 + now.minute
    start, end = _minutes(rule.get("start", "00:00")), _minutes(rule.get("end", "00:00"))
    wd = now.weekday()
    if start < end:
        return wd in days and start <= t < end
    return (wd in days and t >= start) or (((wd - 1) % 7) in days and t < end)


def resolve(cfg, now=None):
    """Decide what should be on screen right now."""
    now = now or dt.datetime.now()
    s = cfg["settings"]
    media = {m["id"]: m for m in cfg["media"]}
    playlists = {p["id"]: p for p in cfg["playlists"]}

    source, pl = None, None
    if s.get("mode") == "single" and s.get("single_media") in media:
        source = "Single item"
        pl = {"id": "__single", "name": media[s["single_media"]]["name"],
              "items": [{"media": s["single_media"], "duration": 0}]}
    else:
        for rule in cfg["schedules"]:
            if rule.get("playlist") in playlists and rule_matches(rule, now):
                source, pl = "Schedule: " + (rule.get("name") or "rule"), playlists[rule["playlist"]]
                break
        if pl is None:
            pl = playlists.get(cfg["default_playlist"]) or (cfg["playlists"][0] if cfg["playlists"] else
                                                            {"id": None, "name": "(none)", "items": []})
            source = "Default playlist"

    items = []
    for it in pl["items"]:
        m = media.get(it.get("media"))
        if not m:
            continue
        item = {"media": m["id"], "name": m["name"],
                "duration": max(1, float(it.get("duration") or s["default_duration"]))}
        if m.get("type") == "web":
            item.update(type="web", url=m["url"], zoom=m.get("zoom", 100), refresh_min=m.get("refresh_min", 0),
                        thumb=None)
        else:
            item.update(type="image", url="/media/" + m["file"],
                        thumb="/thumbs/" + m["thumb"] if m.get("thumb") else "/media/" + m["file"])
        items.append(item)
    return {"source": source, "playlist_id": pl["id"], "playlist_name": pl["name"], "items": items,
            "shuffle": bool(pl.get("shuffle"))}


# =========================================================================== helpers

def ip_addresses():
    try:
        out = subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=2).stdout
        ips = [ip for ip in out.split() if ":" not in ip]
        if ips:
            return ips
    except Exception:
        pass
    ip = cluster.primary_ip()
    return [ip] if ip else []


def process_image(src_path, ext):
    """Fix phone-photo rotation, shrink huge images, make a thumbnail.
    Returns (final_path, thumb_name, width, height)."""
    base = os.path.splitext(os.path.basename(src_path))[0]
    thumb_name = base + ".jpg"
    if Image is None:
        return src_path, None, 0, 0
    try:
        with Image.open(src_path) as im:
            if getattr(im, "is_animated", False):  # keep animated GIF/WebP untouched
                w, h = im.size
                final = src_path
            else:
                im = ImageOps.exif_transpose(im)
                if max(im.size) > MAX_EDGE:
                    im.thumbnail((MAX_EDGE, MAX_EDGE), Image.LANCZOS)
                if ext in (".jpg", ".jpeg", ".bmp"):
                    final = os.path.join(MEDIA, base + ".jpg")
                    im.convert("RGB").save(final, "JPEG", quality=90, optimize=True)
                else:
                    final = os.path.join(MEDIA, base + ext)
                    im.save(final)
                if final != src_path and os.path.exists(src_path):
                    os.remove(src_path)
                w, h = im.size
            with Image.open(final) as t:
                t = ImageOps.exif_transpose(t.convert("RGB"))
                t.thumbnail((THUMB_EDGE, THUMB_EDGE))
                t.save(os.path.join(THUMBS, thumb_name), "JPEG", quality=80)
            return final, thumb_name, w, h
    except Exception:
        if os.path.exists(src_path):
            os.remove(src_path)
        raise ValueError("not a readable image")


def clean_hex(v, default):
    return v if isinstance(v, str) and re.match(r"^#[0-9a-fA-F]{6}$", v) else default


def server_time():
    return dt.datetime.now().strftime("%a %Y-%m-%d %H:%M")


# =========================================================================== pages

@app.route("/")
def index():
    return redirect("/admin")


@app.route("/login")
def login_page():
    return send_from_directory(STATIC, "login.html")


@app.route("/admin")
@auth("user")
def admin_page():
    return send_from_directory(STATIC, "admin.html")


@app.route("/player")
def player_page():
    return send_from_directory(STATIC, "player.html")


@app.route("/media/<path:name>")
def media_file(name):
    resp = send_from_directory(MEDIA, name)
    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"  # filenames are unique
    return resp


@app.route("/thumbs/<path:name>")
def thumb_file(name):
    resp = send_from_directory(THUMBS, name)
    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp


@app.route("/healthz")
def healthz():
    return "ok"


# =========================================================================== player API (public)

@app.route("/api/now")
def api_now():
    cfg = load_config()
    now = resolve(cfg)
    settings = {k: cfg["settings"][k] for k in ("orientation", "fit", "background", "transition_ms",
                                                 "transition_style")}
    version = hashlib.sha1(json.dumps([settings, now], sort_keys=True).encode()).hexdigest()[:12]
    return jsonify({"version": version, "reload_token": cfg["reload_token"], "settings": settings, "now": now,
                    "info": {"name": cluster.my_name(), "hostname": os.uname().nodename,
                             "ips": ip_addresses(), "port": str(cluster.port)}})


# =========================================================================== login / account

@app.route("/api/login", methods=["POST"])
def api_login():
    body = request.get_json(silent=True) or {}
    name = str(body.get("username", "")).strip().lower()
    u = get_user(name)
    g.user, g.client_ip = None, request.remote_addr
    if not u or not check_password_hash(u["password_hash"], str(body.get("password", ""))):
        time.sleep(1)  # slow down password guessing
        log_action("login failed", "username '%s'" % name[:32])
        return bad("Wrong username or password", 401)
    session.clear()
    session.permanent = True
    session["user"] = name
    session["pw"] = u["password_hash"][-16:]
    g.user = u
    log_action("logged in")
    return jsonify(ok=True)


@app.route("/api/logout", methods=["POST"])
def api_logout():
    session.clear()
    return jsonify(ok=True)


@app.route("/api/me")
@auth("user")
def api_me():
    u = g.user
    return jsonify({"username": u["username"], "role": u["role"], "devices": u.get("devices") or [],
                    "node": {"id": cluster.node["id"], "name": cluster.my_name()}, "version": cluster.VERSION})


@app.route("/api/password", methods=["POST"])
@auth("user")
def api_password():
    body = request.get_json(silent=True) or {}
    u = g.user
    if not check_password_hash(u["password_hash"], str(body.get("current", ""))):
        return bad("Your current password is not correct")
    new = str(body.get("password", ""))
    if len(new) < 6:
        return bad("Use at least 6 characters")
    rec = cluster.update_record("users", u["username"], password_hash=generate_password_hash(new))
    session["pw"] = rec["password_hash"][-16:]
    log_action("changed own password")
    return jsonify(ok=True)


# =========================================================================== users (admin)

def _user_view(name, rec):
    return {"username": name, "role": rec.get("role"), "devices": rec.get("devices") or [],
            "updated": rec.get("updated")}


@app.route("/api/users")
@auth("admin")
def api_users():
    with cluster.lock:
        users = [_user_view(k, v) for k, v in cluster.state["users"].items() if not v.get("deleted")]
    return jsonify(sorted(users, key=lambda u: u["username"]))


def _clean_devices(devs):
    with cluster.lock:
        known = {k for k, d in cluster.state["devices"].items() if not d.get("removed")}
    return sorted({d for d in (devs or []) if d == "*" or d in known})


def _devices_text(devs):
    return "all signs" if "*" in devs else (", ".join(cluster.device_name(d) for d in devs) or "no signs")


@app.route("/api/users", methods=["POST"])
@auth("admin")
def api_user_create():
    b = request.get_json(silent=True) or {}
    name = str(b.get("username", "")).strip().lower()
    if not USER_RE.match(name):
        return bad("Usernames use lowercase letters, numbers, dot, dash or underscore (max 32)")
    if get_user(name):
        return bad("That username already exists")
    if b.get("role") not in ROLES:
        return bad("Pick a role")
    if len(str(b.get("password", ""))) < 6:
        return bad("Password must be at least 6 characters")
    devs = _clean_devices(b.get("devices"))
    cluster.set_record("users", name, {"role": b["role"], "devices": devs,
                                       "password_hash": generate_password_hash(str(b["password"]))})
    log_action("created user", "%s (%s, %s)" % (name, b["role"], _devices_text(devs) if b["role"] == "editor"
                                                 else "all signs"))
    return jsonify(ok=True)


@app.route("/api/users/<name>", methods=["PUT"])
@auth("admin")
def api_user_update(name):
    b = request.get_json(silent=True) or {}
    u = get_user(name)
    if not u:
        return bad("No such user", 404)
    changes, notes = {}, []
    if "role" in b and b["role"] != u["role"]:
        if b["role"] not in ROLES:
            return bad("Pick a role")
        if u["role"] == "admin" and count_admins() <= 1:
            return bad("This is the last admin; make someone else an admin first")
        changes["role"] = b["role"]
        notes.append("role %s → %s" % (u["role"], b["role"]))
    if "devices" in b:
        devs = _clean_devices(b["devices"])
        if devs != sorted(u.get("devices") or []):
            changes["devices"] = devs
            notes.append("signs: " + _devices_text(devs))
    if b.get("password"):
        if len(str(b["password"])) < 6:
            return bad("Password must be at least 6 characters")
        changes["password_hash"] = generate_password_hash(str(b["password"]))
        notes.append("password reset")
    if changes:
        cluster.update_record("users", name, **changes)
        log_action("updated user", "%s: %s" % (name, "; ".join(notes)))
    return jsonify(ok=True)


@app.route("/api/users/<name>", methods=["DELETE"])
@auth("admin")
def api_user_delete(name):
    u = get_user(name)
    if not u:
        return bad("No such user", 404)
    if u["role"] == "admin" and count_admins() <= 1:
        return bad("You can't delete the last admin")
    if name == g.user["username"]:
        return bad("You can't delete yourself")
    # keep a "deleted" marker so the deletion spreads to every Pi
    cluster.set_record("users", name, {"deleted": True, "role": None, "devices": [], "password_hash": ""})
    log_action("deleted user", name)
    return jsonify(ok=True)


# =========================================================================== network (signs)

@app.route("/api/network")
@auth("user")
def api_network():
    u = g.user
    devs = [d for d in cluster.devices_view() if can_access(u, d["id"])]
    out = {"self": cluster.node["id"], "devices": devs}
    if u["role"] == "admin":
        out["found"] = cluster.foreign_view()
    return jsonify(out)


@app.route("/api/network/probe", methods=["POST"])
@auth("admin")
def api_network_probe():
    b = request.get_json(silent=True) or {}
    ip = str(b.get("ip", "")).strip()
    if not re.match(r"^[A-Za-z0-9.\-]{1,253}$", ip):
        return bad("Enter an IP address or hostname")
    try:
        info = cluster.probe(ip, int(b.get("port") or 80))
    except (OSError, ValueError) as e:
        return bad(str(e) or "Nothing answered at that address")
    return jsonify(info)


@app.route("/api/network/adopt", methods=["POST"])
@auth("admin")
def api_network_adopt():
    b = request.get_json(silent=True) or {}
    try:
        name = cluster.adopt(str(b.get("id", "")), str(b.get("username", "")).strip().lower(),
                             str(b.get("password", "")))
    except (OSError, ValueError) as e:
        return bad(str(e))
    log_action("added sign to network", name)
    return jsonify(ok=True)


@app.route("/api/network/devices/<dev_id>", methods=["PUT"])
@auth("admin")
def api_device_rename(dev_id):
    name = str((request.get_json(silent=True) or {}).get("name", "")).strip()[:40]
    if not name:
        return bad("Name required")
    if not cluster.is_member(dev_id):
        return bad("No such sign", 404)
    old = cluster.device_name(dev_id)
    cluster.update_record("devices", dev_id, name=name)
    log_action("renamed sign", "%s → %s" % (old, name))
    return jsonify(ok=True)


@app.route("/api/network/devices/<dev_id>", methods=["DELETE"])
@auth("admin")
def api_device_remove(dev_id):
    if dev_id == cluster.node["id"]:
        return bad("Remove this sign from another sign's admin page")
    if not cluster.is_member(dev_id):
        return bad("No such sign", 404)
    name = cluster.device_name(dev_id)
    cluster.remove_device(dev_id)
    log_action("removed sign from network", name)
    return jsonify(ok=True)


@app.route("/d/<dev_id>/<path:rest>", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
@auth("user")
def relay(dev_id, rest):
    """Run an API call on another Pi, signed on behalf of the logged-in user."""
    if not rest.startswith(("api/", "thumbs/", "media/")):
        return bad("Not allowed", 403)
    if not can_access(g.user, dev_id):
        return bad("You don't have access to that sign", 403)
    if not cluster.is_member(dev_id):
        return bad("Unknown sign", 404)
    body = request.get_data() if request.method not in ("GET", "HEAD") else b""
    try:
        status, headers, data = cluster.call_peer(
            dev_id, request.method, "/" + rest, request.query_string.decode(), body, request.content_type,
            user=g.user["username"], client_ip=request.remote_addr,
            timeout=180 if rest == "api/upload" else 15)
    except OSError:
        return bad("“%s” isn't reachable right now. Is it switched on?" % cluster.device_name(dev_id), 502)
    resp = Response(data, status, content_type=headers.get("Content-Type"))
    if headers.get("Cache-Control"):
        resp.headers["Cache-Control"] = headers["Cache-Control"]
    return resp


# =========================================================================== Pi-to-Pi

@app.route("/cluster/hello")
def cluster_hello():
    return jsonify(cluster.hello())


@app.route("/cluster/state", methods=["POST"])
@node_only
def cluster_state():
    network_before = cluster.node["cluster_id"]
    cluster.merge(request.get_json(silent=True) or {})
    if cluster.node["cluster_id"] != network_before:
        return jsonify({})  # we were just removed: don't leak our new standalone state back
    return jsonify(cluster.snapshot())


@app.route("/cluster/leave", methods=["POST"])
@node_only
def cluster_leave():
    g.user, g.client_ip = None, request.remote_addr
    log_action("left network", "removed by an admin")
    cluster.leave_network()
    return jsonify(ok=True)


@app.route("/cluster/join", methods=["POST"])
def cluster_join():
    b = request.get_json(silent=True) or {}
    u = get_user(str(b.get("username", "")).strip().lower())
    if not u or u["role"] != "admin" or not check_password_hash(u["password_hash"], str(b.get("password", ""))):
        time.sleep(1)
        return bad("Wrong username or password for that sign (use an admin login from that sign)", 403)
    if not all(k in b for k in ("cluster_id", "cluster_key", "state", "adopter")):
        return bad("Incomplete request")
    try:
        bytes.fromhex(str(b["cluster_key"]))
    except ValueError:
        return bad("Bad key")
    g.user, g.client_ip = u, request.remote_addr
    log_action("joined network", "added by sign “%s”" % str((b.get("adopter") or {}).get("name", "?"))[:40])
    cluster.accept_join(b)
    return jsonify(ok=True, id=cluster.node["id"], name=cluster.my_name(), state=cluster.snapshot())


@app.route("/cluster/enroll", methods=["POST"])
def cluster_enroll():
    """A brand-new Pi joins during installation, using an admin login of this network."""
    b = request.get_json(silent=True) or {}
    u = get_user(str(b.get("username", "")).strip().lower())
    if not u or u["role"] != "admin" or not check_password_hash(u["password_hash"], str(b.get("password", ""))):
        time.sleep(1)
        return bad("Wrong admin username or password for the sign network", 403)
    if b.get("verify_only"):
        return jsonify(ok=True, name=cluster.my_name())
    info = b.get("node") or {}
    g.user, g.client_ip = u, request.remote_addr
    log_action("added sign to network", "“%s” (during its installation)" % str(info.get("name", "?"))[:40])
    return jsonify(ok=True, cluster_id=cluster.node["cluster_id"], cluster_key=cluster.node["cluster_key"],
                   state=cluster.snapshot(),
                   adopter={"id": cluster.node["id"], "name": cluster.my_name(), "ip": cluster.primary_ip(),
                            "port": cluster.port})


# =========================================================================== Wi-Fi (admin, per sign)

WIFI_HELPER = os.environ.get("SIGNAGE_WIFI_HELPER", os.path.join(BASE, "wifi_helper.py"))


def wifi_call(payload, timeout=120):
    cmd = ([] if os.environ.get("SIGNAGE_NO_SUDO") else ["sudo", "-n"]) + [WIFI_HELPER]
    try:
        p = subprocess.run(cmd, input=json.dumps(payload), capture_output=True, text=True, timeout=timeout)
        res = json.loads(p.stdout or "{}")
    except subprocess.TimeoutExpired:
        raise ValueError("The Wi-Fi change took too long")
    except (ValueError, OSError):
        raise ValueError("Wi-Fi settings aren't available on this device")
    if not res.get("ok"):
        raise ValueError(res.get("error") or "Wi-Fi command failed")
    return res


@app.route("/api/wifi")
@auth("admin")
def api_wifi():
    try:
        st = wifi_call({"cmd": "status"}, 30)
        lst = wifi_call({"cmd": "list"}, 30)
    except ValueError as e:
        return bad(str(e))
    return jsonify({"status": st, "saved": lst["saved"]})


@app.route("/api/wifi/scan")
@auth("admin")
def api_wifi_scan():
    try:
        return jsonify(wifi_call({"cmd": "scan"}, 60)["networks"])
    except ValueError as e:
        return bad(str(e))


@app.route("/api/wifi/networks", methods=["POST"])
@auth("admin")
def api_wifi_add():
    b = request.get_json(silent=True) or {}
    ssid = str(b.get("ssid", "")).strip()
    try:
        wifi_call({"cmd": "add", "ssid": ssid, "password": str(b.get("password") or ""),
                   "hidden": bool(b.get("hidden")), "priority": _int(b.get("priority"), 10),
                   "connect": bool(b.get("connect"))})
    except ValueError as e:
        if b.get("connect"):
            log_action("Wi-Fi switch failed", "%s: %s" % (ssid, e))
        return bad(str(e))
    log_action("saved Wi-Fi network", ssid + (" and switched to it" if b.get("connect") else ""))
    return jsonify(ok=True)


@app.route("/api/wifi/connect", methods=["POST"])
@auth("admin")
def api_wifi_connect():
    ssid = str((request.get_json(silent=True) or {}).get("ssid", ""))
    try:
        wifi_call({"cmd": "connect", "ssid": ssid})
    except ValueError as e:
        log_action("Wi-Fi switch failed", "%s: %s" % (ssid, e))
        return bad(str(e))
    log_action("switched Wi-Fi network", ssid)
    return jsonify(ok=True)


@app.route("/api/wifi/delete", methods=["POST"])
@auth("admin")
def api_wifi_delete():
    ssid = str((request.get_json(silent=True) or {}).get("ssid", ""))
    try:
        wifi_call({"cmd": "delete", "ssid": ssid})
    except ValueError as e:
        return bad(str(e))
    log_action("removed Wi-Fi network", ssid)
    return jsonify(ok=True)


# =========================================================================== software updates

@app.route("/api/update")
@auth("user")
def api_update():
    st = dict(updates.state)
    return jsonify({"current": cluster.VERSION, "latest": st["latest"], "repo": updates.repo(),
                    "available": updates.is_newer(st["latest"], cluster.VERSION),
                    "checked": st["checked"], "error": st["error"], "notes": st["notes"], "url": st["url"],
                    "status": updates.install_status()})


@app.route("/api/update/check", methods=["POST"])
@auth("admin")
def api_update_check():
    updates.check()
    return api_update()


@app.route("/api/update/apply", methods=["POST"])
@auth("admin")
def api_update_apply():
    try:
        updates.start_update()
    except Exception as e:
        return bad("Couldn't start the update: %s" % e, 500)
    log_action("started software update", "from v%s" % cluster.VERSION)
    return jsonify(ok=True)


# =========================================================================== this sign's content (device level)

@app.route("/api/state")
@auth("device")
def api_state():
    cfg = load_config()
    out = copy.deepcopy(cfg)
    out["now"] = resolve(cfg)
    out["server_time"] = server_time()
    out["device"] = {"id": cluster.node["id"], "name": cluster.my_name(), "ips": ip_addresses(),
                     "hostname": os.uname().nodename, "port": cluster.port}
    return jsonify(out)


@app.route("/api/summary")
@auth("device")
def api_summary():
    cfg = load_config()
    now = resolve(cfg)
    return jsonify({"id": cluster.node["id"], "name": cluster.my_name(), "server_time": server_time(),
                    "orientation": cfg["settings"]["orientation"], "source": now["source"],
                    "playlist_name": now["playlist_name"], "count": len(now["items"]),
                    "thumb": now["items"][0]["thumb"] if now["items"] else None,
                    "first_type": now["items"][0]["type"] if now["items"] else None,
                    "version": cluster.VERSION,
                    "media_count": len(cfg["media"])})


@app.route("/api/log")
@auth("device")
def api_log():
    try:
        limit = max(1, min(2000, int(request.args.get("limit", 500))))
    except ValueError:
        limit = 500
    u = g.user
    entries = audit.read(limit)
    if u["role"] != "admin":  # editors don't see logins or user-management activity
        entries = [e for e in entries if e.get("action") not in
                   ("logged in", "login failed", "created user", "updated user", "deleted user")]
    return jsonify(entries)


@app.route("/api/upload", methods=["POST"])
@auth("device")
def api_upload():
    files = request.files.getlist("files")
    if not files:
        return bad("No files received")
    add_to = request.form.get("playlist")
    added, errors = [], []
    os.makedirs(MEDIA, exist_ok=True)
    os.makedirs(THUMBS, exist_ok=True)
    for f in files:
        name = os.path.basename(f.filename or "image")
        ext = os.path.splitext(name)[1].lower()
        if ext not in ALLOWED_EXT:
            errors.append("%s: unsupported type" % name)
            continue
        mid = uuid.uuid4().hex[:12]
        raw = os.path.join(MEDIA, mid + ext)
        f.save(raw)
        try:
            final, thumb, w, h = process_image(raw, ext)
        except ValueError as e:
            errors.append("%s: %s" % (name, e))
            continue
        added.append({"id": mid, "file": os.path.basename(final), "thumb": thumb,
                      "name": os.path.splitext(name)[0][:80], "width": w, "height": h,
                      "added": int(time.time())})

    def apply(cfg):
        cfg["media"].extend(added)
        for p in cfg["playlists"]:
            if add_to and p["id"] == add_to:
                p["items"].extend({"media": m["id"], "duration": cfg["settings"]["default_duration"]}
                                  for m in added)
                return p["name"]
    pl_name = mutate(apply)
    if added:
        log_action("uploaded images", ", ".join(m["name"] for m in added) +
                   (" (added to playlist “%s”)" % pl_name if pl_name else ""))
    return jsonify({"added": added, "errors": errors})


URL_RE = re.compile(r"^https?://[^\s/$.?#][^\s]*$", re.I)


def _int(v, default):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def _web_fields(body, cur=None):
    """Validate website settings from a request body. Returns a dict of changes."""
    out = {}
    if "url" in body or cur is None:
        url = str(body.get("url", "")).strip()
        if url and "://" not in url:
            url = "https://" + url
        if not URL_RE.match(url) or len(url) > 2000:
            raise ValueError("Enter a full web address, like https://example.com/menu")
        out["url"] = url
    if "zoom" in body or cur is None:
        out["zoom"] = max(25, min(400, _int(body.get("zoom"), 100)))
    if "refresh_min" in body or cur is None:
        out["refresh_min"] = max(0, min(1440, _int(body.get("refresh_min"), 0)))
    return out


@app.route("/api/web", methods=["POST"])
@auth("device")
def api_web_add():
    body = request.get_json(silent=True) or {}
    try:
        fields = _web_fields(body)
    except ValueError as e:
        return bad(str(e))
    name = str(body.get("name") or "").strip()[:80] or re.sub(r"^https?://(www\.)?", "", fields["url"])[:60]
    item = {"id": uuid.uuid4().hex[:12], "type": "web", "name": name, "added": int(time.time()), **fields}

    def apply(cfg):
        cfg["media"].append(item)
        for p in cfg["playlists"]:
            if body.get("playlist") and p["id"] == body["playlist"]:
                p["items"].append({"media": item["id"], "duration": cfg["settings"]["default_duration"]})
                return p["name"]
    pl_name = mutate(apply)
    log_action("added website", "%s (%s)%s" % (name, fields["url"],
                                               " to playlist “%s”" % pl_name if pl_name else ""))
    return jsonify(item)


@app.route("/api/media/<mid>", methods=["PATCH"])
@auth("device")
def api_media_edit(mid):
    body = request.get_json(silent=True) or {}

    def apply(cfg):
        m = next((m for m in cfg["media"] if m["id"] == mid), None)
        if not m:
            return None, []
        notes = []
        if "name" in body:
            name = str(body["name"]).strip()[:80]
            if not name:
                raise ValueError("Name required")
            if name != m["name"]:
                notes.append("renamed “%s” → “%s”" % (m["name"], name))
                m["name"] = name
        if m.get("type") == "web":
            for k, v in _web_fields(body, cur=m).items():
                if m.get(k) != v:
                    notes.append("%s: %s → %s" % ({"url": "address", "zoom": "zoom %",
                                                   "refresh_min": "refresh minutes"}[k], m.get(k), v))
                    m[k] = v
        return m, notes
    try:
        m, notes = mutate(apply)
    except ValueError as e:
        return bad(str(e))
    if m is None:
        return bad("Not found", 404)
    if notes:
        log_action("edited " + ("website" if m.get("type") == "web" else "image"),
                   "%s: %s" % (m["name"], "; ".join(notes)))
    return jsonify(m)


@app.route("/api/media/<mid>", methods=["DELETE"])
@auth("device")
def api_media_delete(mid):
    def apply(cfg):
        m = next((m for m in cfg["media"] if m["id"] == mid), None)
        if not m:
            return None
        cfg["media"].remove(m)
        for p in cfg["playlists"]:
            p["items"] = [i for i in p["items"] if i["media"] != mid]
        if cfg["settings"].get("single_media") == mid:
            cfg["settings"]["single_media"] = None
            cfg["settings"]["mode"] = "playlist"
        return m
    m = mutate(apply)
    if not m:
        return bad("Not found", 404)
    for path in (os.path.join(MEDIA, m.get("file") or ""), os.path.join(THUMBS, m.get("thumb") or "")):
        if os.path.isfile(path):
            os.remove(path)
    log_action("deleted " + ("website" if m.get("type") == "web" else "image"), m["name"])
    return jsonify({"ok": True})


@app.route("/api/settings", methods=["PUT"])
@auth("device")
def api_settings():
    body = request.get_json(silent=True) or {}

    def apply(cfg):
        s = cfg["settings"]
        before = dict(s)
        if body.get("mode") in ("playlist", "single"):
            s["mode"] = body["mode"]
        if "single_media" in body:
            ids = {m["id"] for m in cfg["media"]}
            s["single_media"] = body["single_media"] if body["single_media"] in ids else None
        if body.get("orientation") in ORIENTATIONS:
            s["orientation"] = body["orientation"]
        if body.get("fit") in FITS:
            s["fit"] = body["fit"]
        if "background" in body:
            s["background"] = clean_hex(body["background"], s["background"])
        if body.get("transition_style") in TRANSITIONS:
            s["transition_style"] = body["transition_style"]
        if "transition_ms" in body:
            s["transition_ms"] = max(0, min(5000, int(body["transition_ms"])))
        if "default_duration" in body:
            s["default_duration"] = max(1, min(86400, float(body["default_duration"])))
        if s["mode"] == "single" and not s["single_media"]:
            s["mode"] = "playlist"
        names = {m["id"]: m["name"] for m in cfg["media"]}
        notes = []
        for k in s:
            if s[k] != before.get(k):
                if k == "orientation":
                    notes.append("orientation → %s" % ORIENTATION_NAMES[s[k]])
                elif k == "single_media":
                    continue
                elif k == "mode":
                    notes.append("showing one item: %s" % names.get(s["single_media"], "?") if s[k] == "single"
                                 else "back to slideshow/schedule")
                else:
                    notes.append("%s: %s → %s" % (k.replace("_", " "), before.get(k), s[k]))
        if s["mode"] == "single" and before.get("mode") == "single" and s["single_media"] != before.get("single_media"):
            notes.append("showing one item: %s" % names.get(s["single_media"], "?"))
        return s, notes
    try:
        s, notes = mutate(apply)
    except (TypeError, ValueError):
        return bad("Invalid value")
    if notes:
        log_action("changed display settings", "; ".join(notes))
    return jsonify(s)


def _clean_items(items, media_ids, default_duration):
    out = []
    for it in items or []:
        if isinstance(it, dict) and it.get("media") in media_ids:
            try:
                d = float(it.get("duration") or default_duration)
            except (TypeError, ValueError):
                d = default_duration
            out.append({"media": it["media"], "duration": max(1, min(86400, round(d, 1)))})
    return out


def _describe_items(old, new, names):
    old_ids, new_ids = [i["media"] for i in old], [i["media"] for i in new]
    notes = []
    added = [names.get(m, "?") for m in new_ids if m not in old_ids]
    removed = [names.get(m, "?") for m in old_ids if m not in new_ids]
    if added:
        notes.append("added " + ", ".join(added))
    if removed:
        notes.append("removed " + ", ".join(removed))
    if not added and not removed and len(old_ids) == len(new_ids):
        if old_ids != new_ids:
            notes.append("reordered")
        changed = ["%s %gs → %gs" % (names.get(a["media"], "?"), a["duration"], b["duration"])
                   for a, b in zip(old, new) if a["media"] == b["media"] and a["duration"] != b["duration"]]
        if changed:
            notes.append("times: " + ", ".join(changed[:6]) + (" …" if len(changed) > 6 else ""))
    return notes


@app.route("/api/playlists", methods=["POST"])
@auth("device")
def api_playlist_create():
    name = str((request.get_json(silent=True) or {}).get("name") or "New playlist").strip()[:60]
    pl = {"id": uuid.uuid4().hex[:8], "name": name, "items": [], "shuffle": False}
    mutate(lambda cfg: cfg["playlists"].append(pl))
    log_action("created playlist", name)
    return jsonify(pl)


@app.route("/api/playlists/<pid>", methods=["PUT"])
@auth("device")
def api_playlist_update(pid):
    body = request.get_json(silent=True) or {}

    def apply(cfg):
        pl = next((p for p in cfg["playlists"] if p["id"] == pid), None)
        if not pl:
            return None, []
        notes = []
        if "name" in body and str(body["name"]).strip() and str(body["name"]).strip()[:60] != pl["name"]:
            notes.append("renamed from “%s”" % pl["name"])
            pl["name"] = str(body["name"]).strip()[:60]
        if "items" in body:
            new = _clean_items(body["items"], {m["id"] for m in cfg["media"]}, cfg["settings"]["default_duration"])
            notes += _describe_items(pl["items"], new, {m["id"]: m["name"] for m in cfg["media"]})
            pl["items"] = new
        if "shuffle" in body and bool(body["shuffle"]) != bool(pl.get("shuffle")):
            pl["shuffle"] = bool(body["shuffle"])
            notes.append("shuffle " + ("on" if pl["shuffle"] else "off"))
        if body.get("make_default") and cfg["default_playlist"] != pid:
            cfg["default_playlist"] = pid
            notes.append("made default")
        return pl, notes
    pl, notes = mutate(apply)
    if not pl:
        return bad("Not found", 404)
    if notes:
        log_action("edited playlist", "“%s”: %s" % (pl["name"], "; ".join(notes)))
    return jsonify(pl)


@app.route("/api/playlists/<pid>", methods=["DELETE"])
@auth("device")
def api_playlist_delete(pid):
    def apply(cfg):
        if len(cfg["playlists"]) <= 1:
            return "You need at least one playlist", None
        pl = next((p for p in cfg["playlists"] if p["id"] == pid), None)
        if not pl:
            return "Not found", None
        cfg["playlists"].remove(pl)
        cfg["schedules"] = [r for r in cfg["schedules"] if r.get("playlist") != pid]
        if cfg["default_playlist"] == pid:
            cfg["default_playlist"] = cfg["playlists"][0]["id"]
        return None, pl["name"]
    err, name = mutate(apply)
    if err:
        return bad(err)
    log_action("deleted playlist", name)
    return jsonify({"ok": True})


@app.route("/api/schedules", methods=["PUT"])
@auth("device")
def api_schedules():
    rules = (request.get_json(silent=True) or {}).get("schedules")
    if not isinstance(rules, list):
        return bad("Expected a list of rules")

    def apply(cfg):
        pids = {p["id"]: p["name"] for p in cfg["playlists"]}
        clean = []
        for r in rules:
            if not isinstance(r, dict) or r.get("playlist") not in pids:
                raise ValueError("Each rule needs a valid playlist")
            start, end = str(r.get("start", "00:00")), str(r.get("end", "00:00"))
            if not TIME_RE.match(start) or not TIME_RE.match(end):
                raise ValueError("Times must be HH:MM (24h)")
            sd, ed = r.get("start_date") or None, r.get("end_date") or None
            for d in (sd, ed):
                if d and not DATE_RE.match(str(d)):
                    raise ValueError("Dates must be YYYY-MM-DD")
            days = sorted({int(d) for d in (r.get("days") or []) if str(d).isdigit() and 0 <= int(d) <= 6})
            clean.append({"id": str(r.get("id") or uuid.uuid4().hex[:8])[:16],
                          "name": str(r.get("name") or "").strip()[:60],
                          "playlist": r["playlist"], "days": days, "start": start, "end": end,
                          "start_date": sd, "end_date": ed, "enabled": bool(r.get("enabled", True))})
        cfg["schedules"] = clean
        dn = "MTWTFSS"
        return clean, ["%s%s: %s %s %s–%s" % ("" if c["enabled"] else "(off) ", c["name"] or "rule",
                                                pids[c["playlist"]], "".join(dn[d] for d in c["days"]),
                                                c["start"], c["end"]) for c in clean]
    try:
        clean, desc = mutate(apply)
    except ValueError as e:
        return bad(str(e))
    log_action("saved schedule", "; ".join(desc) if desc else "no rules")
    return jsonify(clean)


@app.route("/api/reload-display", methods=["POST"])
@auth("device")
def api_reload_display():
    def apply(cfg):
        cfg["reload_token"] = cfg.get("reload_token", 0) + 1
    mutate(apply)
    log_action("refreshed screen")
    return jsonify({"ok": True})


@app.route("/api/reboot", methods=["POST"])
@auth("admin")
def api_reboot():
    log_action("rebooted sign")
    try:
        subprocess.Popen(["sudo", "-n", "/sbin/reboot"])
    except Exception as e:
        return bad("Reboot failed: %s" % e, 500)
    return jsonify({"ok": True})


# =========================================================================== startup

def migrate_and_bootstrap():
    """Upgrade v1 data (single password) and make sure at least one admin exists."""
    cfg = store.read_json(CONFIG)
    old_hash = cfg.pop("admin_password_hash", None) if cfg else None
    if cfg is not None and old_hash is not None:
        save_config(cfg)
    with cluster.lock:
        has_users = any(not u.get("deleted") for u in cluster.state["users"].values())
    if not has_users:
        cluster.set_record("users", "admin", {"role": "admin", "devices": ["*"],
                                              "password_hash": old_hash or generate_password_hash("admin")},
                           push=False)
        if not old_hash:
            print("WARNING: created user 'admin' with password 'admin' - change it!")


def cli_join(args):
    host, _, p = args.join.strip().partition(":")
    body = {"username": args.join_user.strip().lower(), "password": os.environ.get("SIGNAGE_JOIN_PASSWORD", ""),
            "verify_only": args.check_join,
            "node": {"id": cluster.node["id"], "name": cluster.my_name(), "ip": cluster.primary_ip(),
                     "port": cluster.port}}
    try:
        status, _, data = cluster.http_call(host, int(p or 80), "POST", "/cluster/enroll",
                                            body=json.dumps(body).encode(), content_type="application/json",
                                            timeout=20)
        reply = json.loads(data or b"{}")
    except (OSError, ValueError) as e:
        print("Couldn't reach a JoeDeck sign at %s (%s)" % (args.join, e))
        return 2
    if status != 200:
        print(reply.get("error") or "The sign refused (HTTP %s)" % status)
        return 3
    if args.check_join:
        print("OK - login accepted by “%s”." % reply.get("name"))
        return 0
    cluster.accept_join(reply)
    cluster.sync_with(reply["adopter"]["id"], force=True)
    audit.add({"device": cluster.node["id"], "device_name": cluster.my_name(), "user": body["username"],
               "action": "joined network", "detail": "during installation, via “%s”" % reply["adopter"]["name"],
               "ip": None})
    print("Joined the sign network through “%s”." % reply["adopter"]["name"])
    return 0


def main():
    ap = argparse.ArgumentParser(description="JoeDeck server")
    ap.add_argument("--port", type=int, default=int(os.environ.get("SIGNAGE_PORT", "80")))
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--name", help="set this sign's display name and exit")
    ap.add_argument("--join", metavar="HOST[:PORT]",
                    help="join an existing sign network (admin password in env SIGNAGE_JOIN_PASSWORD) and exit")
    ap.add_argument("--join-user", default="admin")
    ap.add_argument("--check-join", action="store_true", help="with --join: only check the login")
    ap.add_argument("--set-password", metavar="PASSWORD",
                    help="create/reset the 'admin' user with this password and exit (also unlocks a forgotten login)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    os.makedirs(MEDIA, exist_ok=True)
    os.makedirs(THUMBS, exist_ok=True)
    cluster.init(args.port)
    if args.name:
        cluster.update_record("devices", cluster.node["id"], push=False, name=args.name.strip()[:40])
        print("Sign name set to “%s”." % cluster.my_name())
    if args.join:
        sys.exit(cli_join(args))
    if args.name:
        return
    if args.set_password is not None:
        cluster.set_record("users", "admin", {"role": "admin", "devices": ["*"],
                                              "password_hash": generate_password_hash(args.set_password)},
                           push=False)
        audit.add({"device": cluster.node["id"], "device_name": cluster.my_name(), "user": "(console)",
                   "action": "reset admin password", "detail": "from the Pi's command line", "ip": None})
        print("User 'admin' now has the new password.")
        return
    if not os.path.exists(CONFIG):
        save_config(copy.deepcopy(DEFAULT_CONFIG))
    migrate_and_bootstrap()
    app.secret_key = cluster.node["session_secret"]
    cluster.start()
    updates.start()

    try:
        from waitress import serve
        print("JoeDeck on http://%s:%d (waitress)" % (args.host, args.port))
        serve(app, host=args.host, port=args.port, threads=8)
    except ImportError:
        app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
