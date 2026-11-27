#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
錄影查看網頁 (只用標準函式庫, Python 3.6+)
  python3 web_viewer.py            -> http://<主機 IP>:8080
  python3 web_viewer.py --port 9000
功能：即時畫面 (原始 H.264 直送瀏覽器；不支援時改 MJPEG)、依攝像頭/日期瀏覽錄影、播放、下載、刪除。
"""
import argparse
import ipaddress
import json
import math
import os
import re
import shutil
import socketserver
import subprocess
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

import record_config as rc
import scan_store
import recording_archive as archive
from recording_health import camera_health

LIVE_FPS = 10          # 即時畫面每秒張數
LIVE_WIDTH = 960       # 即時畫面寬度
LIVE_IDLE_SEC = 10     # 沒人看幾秒後停止解碼 (省 CPU)

LEGACY_DISCOVERED_FILE = os.path.join(rc.OUT_DIR, "discovered.json")

CAM_RE = archive.CAM.pattern
FILE_RE = archive.SEGMENT.pattern


def log(msg):
    print("[{}] {}".format(time.strftime("%Y-%m-%d %H:%M:%S"), msg), flush=True)


# ---------------------------------------------------------------- 網段掃描
_empty_scan = {"time": 0, "seconds": 0, "networks": [], "cameras": []}
_scan_files = {}
_scan_failure = (None, "")
_scan_lock = threading.Lock()


def _load_discovered():
    for path in (scan_store.DISCOVERED_FILE, LEGACY_DISCOVERED_FILE):
        try:
            st = os.stat(path)
            stamp = (st.st_ino, st.st_mtime_ns, st.st_ctime_ns, st.st_size)
            cached = _scan_files.get(path)
            if cached is None or cached[0] != stamp:
                try:
                    data = scan_store.read_result(path)
                    data = {key: data.get(key, default) for key, default in _empty_scan.items()}
                except ValueError:
                    data = None
                cached = _scan_files[path] = (stamp, data)
            if cached[1] is not None:
                return (path, stamp), cached[1]
        except OSError:
            continue
    return None, _empty_scan


def scan_snapshot():
    global _scan_failure
    with _scan_lock:
        error = ""
        try:
            running = scan_store.scan_running()
        except OSError as exc:
            running, error = False, str(exc)
        version, data = _load_discovered()
        if (_scan_failure[1] and version and version[0] == scan_store.DISCOVERED_FILE
                and version != _scan_failure[0]):
            _scan_failure = (None, "")
        return dict(data, scanning=running, error=error or _scan_failure[1])


def _scan_failed(exc):
    global _scan_failure
    with _scan_lock:
        version, _ = _load_discovered()
        _scan_failure = (version, str(exc) or type(exc).__name__)
    log("掃描失敗: {}".format(exc))


def run_scan():
    """手動掃描也與錄影程式共用同一把鎖，正在掃描時直接回應。"""
    global _scan_failure

    def work(lock):
        with lock:
            try:
                res = scan_store.scan_and_save()
                log("掃描 {} 找到 {} 台 ({} 秒)".format(", ".join(res["networks"]),
                                                  len(res["cameras"]), res["seconds"]))
            except Exception as exc:
                _scan_failed(exc)

    lock = None
    try:
        with _scan_lock:
            lock = scan_store.try_scan_lock()
            if lock is None:
                return False
            _scan_failure = (None, "")
            threading.Thread(target=work, args=(lock,), daemon=True).start()
            return True
    except (OSError, RuntimeError) as exc:
        _scan_failed(exc)
        if lock is not None:
            lock.close()
        return False


# ---------------------------------------------------------------- 錄影設定
_cfg_lock = threading.RLock()
_last_config = {"paused": False, "cameras": []}


def rec_config():
    global _last_config
    with _cfg_lock:
        try:
            _last_config = rc.load_config()
        except (ValueError, OSError):
            pass
        return _last_config


def rec_status():
    """A fresh heartbeat proves the recorder reported, independently of media writes."""
    try:
        with open(rc.STATUS_FILE) as f:
            st = json.load(f)
        if not isinstance(st, dict) or not isinstance(st.get("cameras"), dict):
            raise ValueError("invalid status")
        updated = float(st.get("updated", 0))
        if not math.isfinite(updated) or not 0 <= updated <= time.time():
            raise ValueError("invalid heartbeat timestamp")
        for ip, row in st["cameras"].items():
            if not isinstance(row, dict):
                raise ValueError("invalid worker")
            ipaddress.IPv4Address(ip)
        st["updated"] = updated
        st["running"] = time.time() - updated < 15
        return st
    except (OSError, ValueError, TypeError, OverflowError):
        return {"running": False, "cameras": {}}


def update_config(body):
    """Apply a global pause or camera changes in one configuration write."""
    try:
        with rc.config_edit():
            return _update_config(body)
    except OSError as e:
        return {"error": "錄影設定無法鎖定: {}".format(e)}


def _update_config(body):
    global _last_config
    with _cfg_lock:
        try:
            cfg = rc.load_config()
        except (ValueError, OSError) as e:
            return {"error": "錄影設定無法讀取: {}".format(e)}
        if "ips" in body and "ip" in body:
            return {"error": "請使用 ip 或 ips 其中一種"}
        targets = body.get("ips", [body["ip"]] if "ip" in body else [])
        if not isinstance(targets, list) or ("ips" in body and not targets):
            return {"error": "請至少選取一台攝影機"}
        if bool(targets) != ("record" in body) or not (targets or "paused" in body):
            return {"error": "請指定全機暫停設定，或攝影機及其錄影設定"}
        if any(key in body and not isinstance(body[key], bool) for key in ("paused", "record")):
            return {"error": "paused / record 必須為布林值"}
        if targets:
            try:
                if not all(isinstance(ip, str) for ip in targets):
                    raise ValueError()
                ips = {str(ipaddress.IPv4Address(ip)) for ip in targets}
            except ValueError:
                return {"error": "攝影機 IPv4 位址無效"}
            cameras = {cam["ip"]: cam for cam in cfg["cameras"]}
            if body["record"]:
                missing = ips - cameras.keys()
                discovered = discovered_urls() if missing else {}
                for ip in sorted(missing):
                    if ip not in discovered:
                        return {"error": "{} 沒有可用的串流，請重新掃描".format(ip)}
                    cameras[ip] = {"ip": ip, "url": discovered[ip]}
            else:
                for ip in ips:
                    cameras.pop(ip, None)
            cfg["cameras"] = [cameras[ip] for ip in sorted(cameras, key=ipaddress.IPv4Address)]
        if "paused" in body:
            cfg["paused"] = body["paused"]
        try:
            rc.save_config(cfg)
        except (ValueError, OSError) as e:
            return {"error": "錄影設定無法儲存: {}".format(e)}
        if targets:
            log("錄影 {} {}".format("、".join(sorted(ips)), "啟用" if body["record"] else "關閉"))
        if "paused" in body:
            log("錄影 " + ("全部暫停" if cfg["paused"] else "全部開始"))
        _last_config = cfg
        return {"ok": True, "config": {"paused": cfg["paused"],
                                       "cameras": [{"ip": c["ip"]} for c in cfg["cameras"]]}}


def live_summary():
    cfg = rec_config()
    rec = rec_status()
    scan = scan_snapshot()
    found = {cam["ip"]: cam for cam in scan["cameras"]}
    selected = {cam["ip"] for cam in cfg["cameras"]}
    active = rec["cameras"] if rec["running"] else {}
    cameras = []
    now = time.time()
    for ip in sorted(found, key=lambda ip: (ip not in selected, ipaddress.IPv4Address(ip))):
        device = found[ip]
        cameras.append({
            "ip": ip, "name": device.get("name", ""), "record": ip in selected,
            "status": device["status"], "can_preview": ip in selected or device["status"] == "ok",
            "health": camera_health(rc.OUT_DIR, ip, ip in selected, cfg["paused"], rec, now),
        })
    du = shutil.disk_usage(rc.OUT_DIR)
    heartbeat = {k: rec[k] for k in ("running", "updated", "schedule", "in_schedule", "paused", "pid")
                 if k in rec}
    heartbeat["cameras"] = {ip: {"state": row.get("state"), "since": row.get("since")}
                            for ip, row in active.items() if ip in found}
    return dict(time=scan["time"], seconds=scan["seconds"], networks=scan["networks"],
                scanning=scan["scanning"], scan_error=scan["error"],
                cameras=cameras, paused=cfg["paused"], rec=heartbeat, now=now,
                utc_offset=time.localtime(now).tm_gmtoff, disk={"total": du.total, "free": du.free})


def discovered_urls():
    return {c["ip"]: "rtsp://{}:{}{}".format(c["ip"], c["port"], c["path"])
            for c in scan_snapshot()["cameras"] if c["status"] == "ok"}


def rtsp_url(ip):
    for c in rec_config()["cameras"]:
        if c["ip"] == ip:
            return c["url"]
    return discovered_urls().get(ip)


# ---------------------------------------------------------------- 即時畫面
class LiveCam(object):
    """每台攝像頭一個 ffmpeg 解碼程序，輸出 MJPEG；有觀看者才啟動。"""

    def __init__(self, ip):
        self.ip = ip
        self.cond = threading.Condition()
        self.frame = None
        self.seq = 0
        self.viewers = 0
        self.last_view = 0.0
        self.thread = None

    def acquire(self):
        with self.cond:
            self.viewers += 1
            self.last_view = time.time()
            if self.thread is None or not self.thread.is_alive():
                self.thread = threading.Thread(target=self._run, daemon=True)
                self.thread.start()

    def release(self):
        with self.cond:
            self.viewers -= 1
            self.last_view = time.time()

    def _idle(self):
        with self.cond:
            return self.viewers <= 0 and time.time() - self.last_view > LIVE_IDLE_SEC

    def _run(self):
        url = rtsp_url(self.ip)
        if not url:
            return
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
               "-rtsp_transport", "tcp", "-stimeout", "5000000",
               "-fflags", "nobuffer", "-flags", "low_delay",
               "-i", url, "-an",
               "-vf", "fps={},scale={}:-2".format(LIVE_FPS, LIVE_WIDTH),
               "-q:v", "6", "-f", "mpjpeg", "pipe:1"]
        log("live {} 開始解碼".format(self.ip))
        while not self._idle():
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            try:
                self._read(proc)
            finally:
                proc.kill()
                proc.wait()
            with self.cond:
                self.frame = None
                self.cond.notify_all()
            if not self._idle():
                time.sleep(3)  # 斷線，稍後重連
        log("live {} 沒人看，停止解碼".format(self.ip))

    def _read(self, proc):
        f = proc.stdout
        n = 0
        while True:
            length = None
            while True:  # 讀 part header
                line = f.readline()
                if not line:
                    return
                line = line.strip()
                if not line:
                    if length is not None:
                        break
                    continue
                if line.lower().startswith(b"content-length:"):
                    length = int(line.split(b":", 1)[1])
            data = f.read(length)
            if len(data) < length:
                return
            with self.cond:
                self.frame = data
                self.seq += 1
                self.cond.notify_all()
            n += 1
            if n % LIVE_FPS == 0 and self._idle():
                return

    def wait_frame(self, last_seq, timeout=5):
        with self.cond:
            self.cond.wait_for(lambda: self.seq != last_seq, timeout)
            return self.seq, self.frame


_live = {}
_live_lock = threading.Lock()


def live_cam(ip):
    with _live_lock:
        if ip not in _live:
            _live[ip] = LiveCam(ip)
        return _live[ip]


# ---------------------------------------------------------------- 檔案
def archive_cameras():
    selected = {c["ip"] for c in rec_config()["cameras"]}
    found = {c["ip"] for c in scan_snapshot()["cameras"]}
    return [{"dir": ip.replace(".", "_"), "ip": ip, "record": ip in selected}
            for ip in sorted(found, key=ipaddress.IPv4Address)]


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *a):
        pass

    def send_bytes(self, body, ctype, code=200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, obj, code=200):
        self.send_bytes(json.dumps(obj).encode(), "application/json; charset=utf-8", code)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(u.path)
        q = urllib.parse.parse_qs(u.query)
        try:
            if path in self.server.assets:
                return self.send_bytes(*self.server.assets[path])
            if path == "/api/cams":
                du = shutil.disk_usage(rc.OUT_DIR)
                return self.send_json({
                    "cams": archive_cameras(),
                    "disk": {"total": du.total, "free": du.free},
                    "segment": rc.SEGMENT_SEC,
                    "root": os.path.abspath(rc.OUT_DIR), "today": time.strftime("%Y-%m-%d"),
                    "schedule": [getattr(rc, "REC_START", ""), getattr(rc, "REC_END", "")],
                })
            if path == "/api/live":
                return self.send_json(live_summary())
            m = re.fullmatch("/api/files/(" + CAM_RE + ")", path)
            if m:
                return self.send_json(archive.list_files(rc.OUT_DIR, m.group(1)))
            m = re.match(r"^/(fmp4|live)/(\d+\.\d+\.\d+\.\d+)$", path)
            if m:
                if not rtsp_url(m.group(2)):
                    return self.send_bytes(b"unknown camera", "text/plain", 404)
                if m.group(1) == "fmp4":
                    return self.stream_fmp4(m.group(2))
                return self.stream_live(m.group(2))
            m = re.fullmatch("/video/(" + CAM_RE + ")/(" + FILE_RE + ")", path)
            if m:
                return self.send_file(m.group(1), m.group(2), "dl" in q, q.get("id", [None])[0])
            self.send_bytes(b"not found", "text/plain", 404)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        try:
            if path == "/api/scan":
                started = run_scan()
                if not started:
                    scan = scan_snapshot()
                    if not scan["scanning"] and scan["error"]:
                        return self.send_json({"error": scan["error"]}, 503)
                return self.send_json({"started": started})
            if path == "/api/record":
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n).decode() or "{}")
                if not isinstance(body, dict):
                    return self.send_json({"error": "bad request"}, 400)
                return self.send_json(update_config(body))
            if path == "/api/delete":
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n).decode() or "{}")
                if not isinstance(body, dict):
                    return self.send_json({"error": "bad request"}, 400)
                cam = str(body.get("cam", ""))
                names = body.get("names") or []
                if not archive.CAM.fullmatch(cam) or not isinstance(names, list):
                    return self.send_json({"error": "bad request"}, 400)
                expected = body.get("expected")
                if not isinstance(expected, dict):
                    return self.send_json({"error": "請重新整理錄影檔清單後再確認刪除"}, 409)
                result = archive.delete_files(rc.OUT_DIR, cam, names, expected=expected)
                if result["deleted"]:
                    log("刪除 {} 段 ({})".format(len(result["deleted"]), cam))
                return self.send_json(result)
            self.send_bytes(b"not found", "text/plain", 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except ValueError:
            self.send_json({"error": "bad json"}, 400)

    def stream_fmp4(self, ip):
        """直接轉送攝像頭 H.264 (不解碼，幾乎不吃 CPU)，包成 fragmented mp4 給瀏覽器用 MSE 播放。"""
        url = rtsp_url(ip)
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
               "-rtsp_transport", "tcp", "-stimeout", "5000000", "-fflags", "nobuffer",
               "-i", url, "-map", "0:v", "-an", "-c:v", "copy",
               "-f", "mp4", "-movflags", "empty_moov+default_base_moof+frag_every_frame",
               "pipe:1"]
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        try:
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            fd = proc.stdout.fileno()
            while True:
                data = os.read(fd, 65536)
                if not data:
                    break
                self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            proc.kill()
            proc.wait()

    def stream_live(self, ip):
        cam = live_cam(ip)
        cam.acquire()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            with cam.cond:
                seq = cam.seq
            last_ok = time.time()
            while True:
                new_seq, frame = cam.wait_frame(seq)
                changed = new_seq != seq
                seq = new_seq
                if not changed or frame is None:
                    if time.time() - last_ok > 30:   # 30 秒沒畫面就斷開，讓瀏覽器顯示離線並重試
                        return
                    continue
                last_ok = time.time()
                self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                                 + str(len(frame)).encode() + b"\r\n\r\n" + frame + b"\r\n")
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            cam.release()

    def send_file(self, cam, name, download, expected):
        try:
            clip = archive.open_clip(rc.OUT_DIR, cam, name, expected)
        except (OSError, ValueError):
            return self.send_bytes(b"not found", "text/plain", 404)
        with clip:
            self.send_clip_range(clip, cam + "_" + name if download else None)

    def send_clip_range(self, clip, attachment):
        size = os.fstat(clip.fileno()).st_size
        start, end = 0, size - 1
        m = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                if m.group(2):
                    end = min(int(m.group(2)), size - 1)
            elif m.group(2):
                start = max(0, size - int(m.group(2)))
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", "bytes */{}".format(size))
                self.end_headers()
                return
            self.send_response(206)
            self.send_header("Content-Range", "bytes {}-{}/{}".format(start, end, size))
        else:
            self.send_response(200)
        length = end - start + 1
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if attachment:
            self.send_header("Content-Disposition", 'attachment; filename="{}"'.format(attachment))
        self.end_headers()
        clip.seek(start)
        remain = length
        while remain > 0:
            chunk = clip.read(min(1 << 20, remain))
            if not chunk:
                break
            self.wfile.write(chunk)
            remain -= len(chunk)


class Server(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler):
        self.assets = {}
        for route, filename, mime in [
                ("/", "index.html", "text/html"),
                ("/static/styles.css", "styles.css", "text/css"),
                ("/static/app.js", "app.js", "text/javascript")]:
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", filename)
            with open(path, "rb") as f:
                self.assets[route] = (f.read(), mime + "; charset=utf-8")
        super().__init__(address, handler)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8080)
    a = ap.parse_args()
    archive.prepare_writer_checks()
    srv = Server(("0.0.0.0", a.port), Handler)
    log("錄影查看網頁: http://0.0.0.0:{}  (錄影資料夾 {})".format(a.port, rc.OUT_DIR))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
