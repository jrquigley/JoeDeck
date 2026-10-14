"""Append-only activity log (one JSON object per line), kept to the newest MAX_ENTRIES."""
import json
import os
import threading
import time

import store

MAX_ENTRIES = 5000
_lock = threading.Lock()
_count = None


def _path():
    return store.path("audit.jsonl")


def _ensure_count():
    global _count
    if _count is None:
        try:
            with open(_path(), "rb") as f:
                _count = sum(1 for _ in f)
        except FileNotFoundError:
            _count = 0


def add(entry):
    global _count
    entry.setdefault("ts", round(time.time(), 3))
    line = json.dumps(entry, separators=(",", ":")) + "\n"
    with _lock:
        _ensure_count()
        os.makedirs(store.DATA, exist_ok=True)
        with open(_path(), "a", encoding="utf-8") as f:
            f.write(line)
        _count += 1
        if _count > MAX_ENTRIES * 1.2:
            _trim()


def _trim():
    global _count
    with open(_path(), "r", encoding="utf-8") as f:
        lines = f.readlines()[-MAX_ENTRIES:]
    tmp = _path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.writelines(lines)
    os.replace(tmp, _path())
    _count = len(lines)


def read(limit=500):
    """Newest first."""
    with _lock:
        try:
            with open(_path(), "r", encoding="utf-8") as f:
                lines = f.readlines()
        except FileNotFoundError:
            return []
    out = []
    for line in reversed(lines):
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
        if len(out) >= limit:
            break
    return out
