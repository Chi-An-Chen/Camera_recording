"""Shared discovery result and nonblocking service scan lock (Linux, Python 3.6+)."""
import errno
import fcntl
import ipaddress
import json
import os

import discover
from record_config import OUT_DIR, write_json

DISCOVERED_FILE = os.path.join(OUT_DIR, "recorder_discovered.json")


def read_result(path):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if (not isinstance(data, dict) or not isinstance(data.get("cameras"), list)
            or not isinstance(data.get("networks"), list)
            or not all(isinstance(net, str) for net in data["networks"])
            or not isinstance(data.get("time", 0), (int, float))):
        raise ValueError("Invalid discovery result")
    for cam in data["cameras"]:
        if (not isinstance(cam, dict) or not isinstance(cam.get("ip"), str)
                or not isinstance(cam.get("status"), str)):
            raise ValueError("Invalid discovery camera")
        ipaddress.IPv4Address(cam["ip"])
        if cam["status"] == "ok" and (not isinstance(cam.get("port"), int)
                                      or not isinstance(cam.get("path"), str)):
            raise ValueError("Invalid discovery stream")
    return data


def completed_since(started, networks):
    try:
        result = read_result(DISCOVERED_FILE)
        return result.get("time", 0) >= started and tuple(sorted(result["networks"])) == networks
    except (OSError, ValueError):
        return False


def try_scan_lock(shared=False):
    """Return the held file (close to release), or None when another scan holds it."""
    lock = open(DISCOVERED_FILE + ".lock", "r" if shared else "a")
    try:
        mode = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
        fcntl.flock(lock, mode | fcntl.LOCK_NB)
        return lock
    except OSError as exc:
        lock.close()
        if exc.errno in (errno.EACCES, errno.EAGAIN):
            return None
        raise


def scan_running():
    try:
        lock = try_scan_lock(shared=True)
    except FileNotFoundError:
        return False
    if lock is None:
        return True
    lock.close()
    return False


def scan_and_save(interfaces=None, stop=None):
    """Run under an exclusive scan lock. Keep the last complete result on shutdown."""
    if interfaces is None:
        interfaces = discover.local_networks()
    result = discover.scan_interfaces(interfaces, workers=16, stop=stop)
    if stop is None or not stop.is_set():
        write_json(DISCOVERED_FILE, result)
        return result
