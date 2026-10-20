"""
Shared media library.

Every image and website is a record in the network's shared state (cluster.py), so all
signs see the same library. Each sign keeps its own copy of every image file, so a sign
keeps playing even if the network drops. Files are copied from other signs in the
background as soon as a new record appears, and on demand if the player asks for one
that hasn't arrived yet.
"""
import hashlib
import logging
import os
import threading
import time

import cluster
import store

MEDIA = store.path("media")
THUMBS = store.path("thumbs")
log = logging.getLogger("signage.media")
_name_locks = {}
_locks_guard = threading.Lock()


def _safe(name):
    return bool(name) and name == os.path.basename(name) and not name.startswith(".")


def all_media(include_deleted=False):
    with cluster.lock:
        return {k: dict(v, id=k) for k, v in cluster.state["media"].items()
                if include_deleted or not v.get("deleted")}


def get(mid):
    with cluster.lock:
        rec = cluster.state["media"].get(mid)
    return dict(rec, id=mid) if rec and not rec.get("deleted") else None


def file_info(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return os.path.getsize(path), h.hexdigest()[:16]


def add(mid, rec):
    rec = dict(rec)
    rec.pop("id", None)
    rec.setdefault("origin", cluster.node["id"])
    rec["deleted"] = False
    return cluster.set_record("media", mid, rec)


def update(mid, **changes):
    return cluster.update_record("media", mid, **changes)


def delete(mid):
    m = get(mid)
    if not m:
        return None
    # keep a small "deleted" marker so the deletion reaches every sign
    cluster.set_record("media", mid, {"deleted": True, "type": m.get("type"), "name": m.get("name"),
                                      "file": m.get("file"), "thumb": m.get("thumb")})
    remove_local(m)
    return m


def remove_local(m):
    for d, name in ((MEDIA, m.get("file")), (THUMBS, m.get("thumb"))):
        if _safe(name or ""):
            try:
                os.remove(os.path.join(d, name))
            except FileNotFoundError:
                pass


def local_path(kind, name):
    return os.path.join(MEDIA if kind == "media" else THUMBS, name)


def has_local(m):
    return m.get("type") == "web" or bool(m.get("file") and os.path.exists(local_path("media", m["file"])))


def _record_for(kind, name):
    key = "file" if kind == "media" else "thumb"
    for m in all_media().values():
        if m.get(key) == name:
            return m
    return None


def _sources(origin):
    with cluster.lock:
        online = [(i, p["ip"], p["port"]) for i, p in cluster.peers.items() if cluster.online(i)]
    online.sort(key=lambda x: x[0] != origin)  # the sign that uploaded it first
    return [(ip, port) for _, ip, port in online]


def fetch(kind, name):
    """Copy media/<name> or thumbs/<name> from another sign. True if the file is now here."""
    if not _safe(name):
        return False
    dest = local_path(kind, name)
    if os.path.exists(dest):
        return True
    rec = _record_for(kind, name)
    if not rec:
        return False
    with _locks_guard:
        lk = _name_locks.setdefault(kind + "/" + name, threading.Lock())
    with lk:
        if os.path.exists(dest):
            return True
        for ip, port in _sources(rec.get("origin")):
            try:
                status, _, data = cluster.http_call(ip, port, "GET", "/%s/%s" % (kind, name), qs="local=1", timeout=60)
            except OSError:
                continue
            if status != 200 or not data:
                continue
            if kind == "media" and rec.get("sha"):
                if hashlib.sha256(data).hexdigest()[:16] != rec["sha"]:
                    log.warning("checksum mismatch for %s from %s", name, ip)
                    continue
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            tmp = dest + ".part"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, dest)
            log.info("copied %s/%s from %s", kind, name, ip)
            return True
    return False


def sync_once():
    for m in all_media(include_deleted=True).values():
        if m.get("deleted"):
            remove_local(m)
            continue
        if m.get("type") == "web":
            continue
        for kind, key in (("media", "file"), ("thumbs", "thumb")):
            name = m.get(key)
            if name and not os.path.exists(local_path(kind, name)):
                fetch(kind, name)


def _loop():
    time.sleep(5)
    while True:
        try:
            sync_once()
        except Exception:
            log.exception("media sync failed")
        cluster.changed_event.wait(60)
        cluster.changed_event.clear()
        time.sleep(2)  # let a burst of changes settle


def start():
    threading.Thread(target=_loop, daemon=True, name="media-sync").start()


def migrate_from_config(cfg):
    """v3.0 kept media per sign: move it into the shared library (once). Returns True if cfg changed."""
    items = cfg.get("media") or []
    if not items:
        return False
    with cluster.lock:
        existing = set(cluster.state["media"])
    for m in items:
        if m.get("id") in existing:
            continue
        rec = {k: v for k, v in m.items() if k != "id"}
        rec.setdefault("type", "image")
        if rec["type"] == "image" and rec.get("file") and os.path.exists(local_path("media", rec["file"])):
            rec["size"], rec["sha"] = file_info(local_path("media", rec["file"]))
        add(m["id"], rec)
    cfg["media"] = []
    return True
