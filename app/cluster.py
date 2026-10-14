"""
Pi-to-Pi networking for JoeDeck.  No central server: every Pi is equal.

* Discovery  - each Pi broadcasts a small UDP "beacon" every few seconds.  Beacons are
               signed with the network key, so only Pis on *your* sign network are trusted.
               Pis that aren't on your network yet show up as "found" so an admin can add them.
* Shared state - users and the list of member signs are copied to every Pi.  Each record
               carries a timestamp; the newest version of a record wins everywhere
               (last-writer-wins), so all Pis converge even after being offline.
* Peer calls - one Pi can act on another on behalf of a logged-in user.  Those requests
               are signed (HMAC-SHA256) with the network key and checked by the receiver.
"""
import hashlib
import hmac
import json
import logging
import os
import secrets
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid

import store

VERSION = "3.0"
DISCOVERY_PORT = int(os.environ.get("SIGNAGE_DISCOVERY_PORT", "47474"))
# extra ports to announce to (only useful when testing several nodes on one machine)
SEND_PORTS = [int(p) for p in os.environ.get("SIGNAGE_DISCOVERY_SEND_PORTS", str(DISCOVERY_PORT)).split(",") if p]
BEACON_INTERVAL = 10
ONLINE_WINDOW = 35       # seconds without a beacon before a sign is shown offline
FOREIGN_WINDOW = 600     # how long a "found" (not yet added) Pi stays listed
MAX_SKEW = 300           # allowed clock difference for signed requests (Pis sync time by NTP)

log = logging.getLogger("signage.cluster")
lock = threading.RLock()
node = None      # this Pi: {id, cluster_id, cluster_key, session_secret, manual_ips}
state = None     # shared: {"users": {name: rec}, "devices": {id: rec}}
port = 80
peers = {}       # verified members heard recently: id -> {ip, port, name, last_seen, digest}
foreign = {}     # Pis on the LAN that are not in our network: id -> {...}
_last_sync = {}
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # LAN only, never via a proxy


# --------------------------------------------------------------------------- persistence

def _node_path():
    return store.path("node.json")


def _state_path():
    return store.path("cluster.json")


def save_node():
    store.write_json(_node_path(), node, 0o600)


def save_state():
    store.write_json(_state_path(), state, 0o600)


def _new_network(n):
    n["cluster_id"] = uuid.uuid4().hex[:10]
    n["cluster_key"] = secrets.token_hex(32)


def init(http_port):
    global node, state, port
    port = http_port
    with lock:
        node = store.read_json(_node_path())
        if not node:
            node = {"id": uuid.uuid4().hex[:10], "session_secret": secrets.token_hex(32), "manual_ips": []}
            _new_network(node)
            save_node()
        node.setdefault("manual_ips", [])
        state = store.read_json(_state_path()) or {}
        state.setdefault("users", {})
        state.setdefault("devices", {})
        me = state["devices"].get(node["id"])
        if not me or me.get("removed"):
            set_record("devices", node["id"], {"name": socket.gethostname(), "ip": primary_ip(),
                                               "port": port, "removed": False}, push=False)
        else:
            save_state()


# --------------------------------------------------------------------------- records

def next_ts():
    """A timestamp newer than every record we hold, even if this Pi's clock is behind."""
    hi = 0.0
    for kind in ("users", "devices"):
        for rec in state[kind].values():
            hi = max(hi, rec.get("updated", 0))
    return round(max(time.time(), hi + 0.001), 3)


def set_record(kind, key, rec, push=True):
    with lock:
        rec = dict(rec)
        rec["updated"] = next_ts()
        rec["by"] = node["id"]
        state[kind][key] = rec
        save_state()
    if push:
        push_async()
    return rec


def update_record(kind, key, push=True, **changes):
    with lock:
        cur = dict(state[kind].get(key) or {})
        cur.update(changes)
        return set_record(kind, key, cur, push=push)


def snapshot():
    with lock:
        return json.loads(json.dumps(state))


def digest():
    with lock:
        return hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()[:16]


def merge(remote):
    """Take every record from `remote` that is newer than ours. Returns True if anything changed."""
    changed = False
    with lock:
        for kind in ("users", "devices"):
            for key, rec in ((remote or {}).get(kind) or {}).items():
                if not isinstance(rec, dict) or not isinstance(rec.get("updated"), (int, float)):
                    continue
                cur = state[kind].get(key)
                if cur is None or (rec["updated"], rec.get("by", "")) > (cur.get("updated", 0), cur.get("by", "")):
                    state[kind][key] = rec
                    changed = True
        if changed:
            save_state()
        me = state["devices"].get(node["id"])
    if me and me.get("removed"):
        leave_network()
    return changed


# --------------------------------------------------------------------------- membership

def my_name():
    with lock:
        return (state["devices"].get(node["id"]) or {}).get("name") or socket.gethostname()


def device_name(dev_id):
    with lock:
        return (state["devices"].get(dev_id) or {}).get("name") or dev_id


def is_member(dev_id):
    with lock:
        d = state["devices"].get(dev_id)
        return bool(d and not d.get("removed"))


def online(dev_id):
    if dev_id == node["id"]:
        return True
    with lock:
        p = peers.get(dev_id)
        return bool(p and time.time() - p["last_seen"] < ONLINE_WINDOW)


def devices_view():
    out = []
    with lock:
        for dev_id, d in state["devices"].items():
            if d.get("removed"):
                continue
            p = peers.get(dev_id) or {}
            out.append({"id": dev_id, "name": d.get("name") or dev_id,
                        "ip": p.get("ip") or d.get("ip"), "port": p.get("port") or d.get("port", 80),
                        "online": online(dev_id), "self": dev_id == node["id"],
                        "version": VERSION if dev_id == node["id"] else p.get("version"),
                        "last_seen": time.time() if dev_id == node["id"] else p.get("last_seen")})
    return sorted(out, key=lambda d: (not d["self"], d["name"].lower()))


def foreign_view():
    now = time.time()
    with lock:
        return sorted(({"id": k, **v} for k, v in foreign.items()
                       if now - v["last_seen"] < FOREIGN_WINDOW and not is_member(k)),
                      key=lambda d: d["name"].lower())


def leave_network():
    """Start a fresh network containing only this Pi. Users are kept so logins still work."""
    with lock:
        name = my_name()
        _new_network(node)
        save_node()
        state["devices"] = {}
        peers.clear()
        set_record("devices", node["id"], {"name": name, "ip": primary_ip(), "port": port, "removed": False},
                   push=False)
    log.info("left network; now standalone")


def accept_join(payload):
    """Called on the Pi being added: adopt the network key and shared state of the adder."""
    with lock:
        name = my_name()
        node["cluster_id"] = str(payload["cluster_id"])
        node["cluster_key"] = str(payload["cluster_key"])
        save_node()
        remote = payload.get("state") or {}
        state["users"] = dict(remote.get("users") or {})
        state["devices"] = dict(remote.get("devices") or {})
        peers.clear()
        foreign.clear()
        set_record("devices", node["id"], {"name": name, "ip": primary_ip(), "port": port, "removed": False},
                   push=False)
        a = payload.get("adopter") or {}
        if a.get("id") and a.get("ip"):
            peers[a["id"]] = {"ip": a["ip"], "port": int(a.get("port") or 80), "name": a.get("name"),
                              "last_seen": time.time(), "digest": None}
    push_async()


def adopt(dev_id, username, password):
    """Called on the adding Pi: send our key + state to a found Pi, authorised by its admin login."""
    with lock:
        f = foreign.get(dev_id)
        if not f:
            raise ValueError("That sign is no longer visible on the network")
        body = {"username": username, "password": password, "cluster_id": node["cluster_id"],
                "cluster_key": node["cluster_key"], "state": snapshot(),
                "adopter": {"id": node["id"], "name": my_name(), "ip": primary_ip(), "port": port}}
    status, _, data = http_call(f["ip"], f["port"], "POST", "/cluster/join",
                                body=json.dumps(body).encode(), content_type="application/json", timeout=15)
    reply = json.loads(data or b"{}")
    if status != 200:
        raise ValueError(reply.get("error") or "The sign refused (HTTP %s)" % status)
    merge(reply.get("state"))
    with lock:
        peers[dev_id] = {"ip": f["ip"], "port": f["port"], "name": f["name"], "last_seen": time.time(), "digest": None}
        foreign.pop(dev_id, None)
    push_async()
    return f["name"]


def remove_device(dev_id):
    """Tombstone a member. If it is online it is told to leave now; otherwise it finds out when it next syncs."""
    addr = peer_addr(dev_id)
    update_record("devices", dev_id, removed=True)
    if addr:
        try:
            call_peer(dev_id, "POST", "/cluster/leave", timeout=5, addr=addr)
        except OSError:
            pass
    with lock:
        peers.pop(dev_id, None)


# --------------------------------------------------------------------------- signing

def _mac(key_hex, parts):
    return hmac.new(bytes.fromhex(key_hex), "\n".join(parts).encode(), hashlib.sha256).hexdigest()


def _body_hash(body):
    return hashlib.sha256(body or b"").hexdigest()


def signed_headers(method, canon_path, body, user="", client_ip=""):
    ts = str(int(time.time()))
    sig = _mac(node["cluster_key"], [method.upper(), canon_path, ts, user, node["id"], _body_hash(body)])
    return {"X-Signage-Node": node["id"], "X-Signage-Time": ts, "X-Signage-User": user,
            "X-Signage-Client": client_ip, "X-Signage-Sig": sig}


def verify_request(method, canon_path, headers, body):
    """Returns {"node", "user", "client"} for a correctly signed request from a member, else None."""
    sig = headers.get("X-Signage-Sig", "")
    ts = headers.get("X-Signage-Time", "0")
    try:
        if abs(time.time() - int(ts)) > MAX_SKEW:
            return None
    except ValueError:
        return None
    sender, user = headers.get("X-Signage-Node", ""), headers.get("X-Signage-User", "")
    expected = _mac(node["cluster_key"], [method.upper(), canon_path, ts, user, sender, _body_hash(body)])
    if not hmac.compare_digest(sig, expected):
        return None
    return {"node": sender, "user": user, "client": headers.get("X-Signage-Client", "")}


# --------------------------------------------------------------------------- HTTP to other Pis

def peer_addr(dev_id):
    with lock:
        p = peers.get(dev_id)
        if p:
            return p["ip"], p["port"]
        d = state["devices"].get(dev_id)
        if d and d.get("ip"):
            return d["ip"], d.get("port", 80)
    return None


def http_call(ip, p, method, path, qs="", body=b"", content_type=None, headers=None, timeout=10):
    url = "http://%s:%s%s%s" % (ip, p, path, "?" + qs if qs else "")
    h = dict(headers or {})
    if content_type:
        h["Content-Type"] = content_type
    req = urllib.request.Request(url, data=None if method in ("GET", "HEAD") else (body or b""),
                                 method=method, headers=h)
    try:
        with _opener.open(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()
    except urllib.error.URLError as e:
        raise OSError(str(e.reason))


def call_peer(dev_id, method, path, qs="", body=b"", content_type=None, user="", client_ip="",
              timeout=15, addr=None):
    addr = addr or peer_addr(dev_id)
    if not addr:
        raise OSError("unknown address")
    canon = path + ("?" + qs if qs else "")
    hdrs = signed_headers(method, canon, body if method not in ("GET", "HEAD") else b"", user, client_ip)
    return http_call(addr[0], addr[1], method, path, qs, body, content_type, hdrs, timeout)


def probe(ip, p=80):
    """Look for a JoeDeck device at a given address (for networks where broadcasts don't get through)."""
    status, _, data = http_call(ip, p, "GET", "/cluster/hello", timeout=5)
    if status != 200:
        raise OSError("No JoeDeck device answered at %s:%s" % (ip, p))
    info = json.loads(data)
    with lock:
        if ip not in node["manual_ips"]:
            node["manual_ips"].append(ip)
            save_node()
        if info.get("cluster") == node["cluster_id"]:
            peers[info["id"]] = {"ip": ip, "port": p, "name": info.get("name"), "last_seen": time.time(), "digest": None}
            sync_async(info["id"], force=True)
        elif info.get("id") != node["id"]:
            foreign[info["id"]] = {"ip": ip, "port": p, "name": info.get("name") or ip, "last_seen": time.time(),
                                   "version": info.get("version")}
    return info


def hello():
    return {"app": "pisignage", "id": node["id"], "name": my_name(), "cluster": node["cluster_id"],
            "port": port, "version": VERSION}


# --------------------------------------------------------------------------- sync

def sync_with(dev_id, force=False):
    now = time.time()
    if not force and now - _last_sync.get(dev_id, 0) < 3:
        return
    _last_sync[dev_id] = now
    body = json.dumps(snapshot()).encode()
    try:
        status, _, data = call_peer(dev_id, "POST", "/cluster/state", body=body,
                                    content_type="application/json", timeout=8)
        if status == 200:
            merge(json.loads(data))
    except (OSError, ValueError) as e:
        log.debug("sync with %s failed: %s", dev_id, e)


def sync_async(dev_id, force=False):
    threading.Thread(target=sync_with, args=(dev_id, force), daemon=True).start()


def push_async():
    """Tell every online member about our latest state right away."""
    with lock:
        ids = [i for i in peers if online(i)]
    for i in ids:
        sync_async(i, force=True)


# --------------------------------------------------------------------------- discovery

def primary_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        pass
    try:
        out = subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=2).stdout.split()
        return next((ip for ip in out if ":" not in ip), "")
    except Exception:
        return ""


def _beacon():
    with lock:
        b = {"app": "pisignage", "proto": 1, "id": node["id"], "name": my_name(), "port": port,
             "cluster": node["cluster_id"], "digest": digest(), "ts": int(time.time()), "version": VERSION}
        b["sig"] = _mac(node["cluster_key"], [json.dumps(b, sort_keys=True)])
    return json.dumps(b).encode()


def _refresh_self():
    ip = primary_ip()
    with lock:
        me = state["devices"].get(node["id"]) or {}
        stale = ip and (me.get("ip") != ip or me.get("port") != port)
    if stale:
        update_record("devices", node["id"], ip=ip, port=port)


def _sender():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    while True:
        try:
            _refresh_self()
            data = _beacon()
            with lock:
                ips = {"255.255.255.255"} | set(node["manual_ips"])
                ips |= {d.get("ip") for k, d in state["devices"].items()
                        if k != node["id"] and not d.get("removed") and d.get("ip")}
                ips |= {p["ip"] for p in peers.values()}
            for ip in ips:
                for p in SEND_PORTS:
                    try:
                        s.sendto(data, (ip, p))
                    except OSError:
                        pass
        except Exception:
            log.exception("beacon failed")
        time.sleep(BEACON_INTERVAL)


def handle_beacon(b, ip):
    if not isinstance(b, dict) or b.get("app") != "pisignage" or not b.get("id") or b.get("id") == node["id"]:
        return
    sig = b.pop("sig", "")
    now = time.time()
    try:
        p = int(b.get("port") or 80)
    except (TypeError, ValueError):
        return
    trusted = (b.get("cluster") == node["cluster_id"] and
               hmac.compare_digest(str(sig), _mac(node["cluster_key"], [json.dumps(b, sort_keys=True)])))
    with lock:
        if trusted:
            peers[b["id"]] = {"ip": ip, "port": p, "name": b.get("name"), "last_seen": now, "digest": b.get("digest"),
                              "version": str(b.get("version") or "")[:20] or None}
            foreign.pop(b["id"], None)
        else:
            foreign[b["id"]] = {"ip": ip, "port": p, "name": str(b.get("name") or ip)[:60], "last_seen": now,
                                "version": str(b.get("version") or "")[:20] or None}
    if trusted and b.get("digest") != digest():
        sync_async(b["id"])


def _listener():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("", DISCOVERY_PORT))
    while True:
        try:
            data, (ip, _) = s.recvfrom(4096)
            handle_beacon(json.loads(data), ip)
        except Exception:
            log.debug("bad beacon", exc_info=True)


def start():
    for target in (_listener, _sender):
        threading.Thread(target=target, daemon=True, name=target.__name__).start()
