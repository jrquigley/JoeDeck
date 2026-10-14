"""Tiny helpers for crash-safe JSON files in the data directory."""
import json
import os

DATA = os.environ.get("SIGNAGE_DATA", "/var/lib/signage")


def path(*parts):
    return os.path.join(DATA, *parts)


def read_json(p, default=None):
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def write_json(p, obj, mode=None):
    """Write to a temp file then rename, so a power cut never leaves a half-written file."""
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    if mode is not None:
        os.chmod(tmp, mode)
    os.replace(tmp, p)
