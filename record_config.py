"""Shared recording settings. Importing this module performs no disk I/O."""
import ipaddress
import json
import os
import tempfile
from urllib.parse import urlsplit

OUT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(OUT_DIR, "record_config.json")
STATUS_FILE = os.path.join(OUT_DIR, "record_status.json")
SEGMENT_SEC = 600
REC_START, REC_END = "09:00", "17:00"


def default_url(ip):
    return "rtsp://{}:554/h264".format(ip)


def validate(cfg):
    if not isinstance(cfg, dict) or not isinstance(cfg.get("cameras"), list):
        raise ValueError("cameras must be a list")
    paused = cfg.get("paused", False)
    if not isinstance(paused, bool):
        raise ValueError("paused must be true or false")
    cameras = []
    for index, item in enumerate(cfg["cameras"]):
        ip = item.get("ip") if isinstance(item, dict) else None
        try:
            if not isinstance(ip, str):
                raise ValueError()
            ipaddress.IPv4Address(ip)
            url = item.get("url", default_url(ip))
            if not isinstance(url, str) or any(c.isspace() for c in url):
                raise ValueError()
            parts = urlsplit(url)
            if parts.scheme not in ("rtsp", "rtsps") or not parts.hostname or parts.port == 0:
                raise ValueError()
        except ValueError:
            raise ValueError("cameras[{}] requires IPv4 and a valid RTSP URL".format(index)) from None
        cameras.append(dict(item, url=url))
    return dict(cfg, paused=paused, cameras=cameras)


def write_json(path, obj, create_only=False):
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".recorder-json-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            try:
                mode = os.stat(path).st_mode & 0o777
            except FileNotFoundError:
                pass
            else:
                os.fchmod(f.fileno(), mode)
            json.dump(obj, f, ensure_ascii=False, indent=1)
        if create_only:
            os.link(tmp, path)
        else:
            os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def load_config():
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            return validate(json.load(f))
    except FileNotFoundError:
        cfg = {"paused": False, "cameras": []}
        try:
            write_json(CONFIG_FILE, cfg, create_only=True)
        except FileExistsError:
            with open(CONFIG_FILE, encoding="utf-8") as f:
                return validate(json.load(f))
        return cfg


def save_config(cfg):
    write_json(CONFIG_FILE, validate(cfg))
