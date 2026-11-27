#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
多攝像頭 RTSP 錄影 -> 分段 mp4 (Python 3.6+, 需要 ffmpeg)

要錄哪幾台、是否全部暫停，存在 record_config.json，可以直接在網頁上切換，
本程式每 2 秒讀一次設定，自動開始/停止對應攝像頭的錄影 (不用重啟)。
第一次執行時會建立空白攝影機名單，可在網頁選取要錄影的鏡頭。

用法:
  python3 record_cams.py                              # 依 record_config.json 錄影
  python3 record_cams.py 192.0.2.10 192.0.2.11        # 範例 IP，請換成實際攝影機
  python3 record_cams.py --segment 300 --out ./recordings
  python3 record_cams.py --start 08:30 --end 18:00    # 改錄影時段 (預設 09:00~17:00)
  python3 record_cams.py --always                     # 24 小時錄

Ctrl+C 停止，會正常收尾 mp4 檔。
"""
import argparse
import fcntl
import ipaddress
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time

import discover
import scan_store

from record_config import (OUT_DIR, REC_END, REC_START, SEGMENT_SEC, STATUS_FILE,
                           default_url, load_config, write_json)
from recording_health import FRESH_SEC, SEGMENT_NAME

MIN_FREE_GB = 5
RETRY_SEC = 5
STALL_SEC = 30
CAM_DIR = re.compile(r"[0-9]{1,3}(?:_[0-9]{1,3}){3}")
writers = {}                 # absolute camera directory -> child PID
writers_lock = threading.Lock()

stop_event = threading.Event()
status = {}                  # ip -> current worker state and segment evidence
status_lock = threading.Lock()


def log(name, msg):
    print("[{}] [{}] {}".format(time.strftime("%Y-%m-%d %H:%M:%S"), name, msg), flush=True)


def set_status(ip, state, error="", file=None, size=None, last_write=None):
    with status_lock:
        old = status.get(ip, {})
        since = old.get("since") if old.get("state") == state else time.time()
        status[ip] = {"state": state, "since": since, "error": error,
                      "file": file, "size": size, "last_write": last_write}


# ---------------------------------------------------------------- 錄影
def _minutes(hhmm):
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def in_schedule(start, end):
    """現在是否在錄影時段內；支援跨午夜 (例如 22:00~06:00)。"""
    if start == end:
        return True
    t = time.localtime()
    now, s, e = t.tm_hour * 60 + t.tm_min, _minutes(start), _minutes(end)
    return s <= now < e if s < e else (now >= s or now < e)


def build_cmd(url, out_pattern, segment_sec):
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
        "-rtsp_transport", "tcp",
        "-stimeout", "5000000",          # 5 秒連不上/沒資料就退出 (單位 µs)
        "-i", url,
        "-map", "0:v", "-c", "copy",     # 直接複製 H.264，不重新編碼，幾乎不吃 CPU
        "-f", "segment",
        "-segment_time", str(segment_sec),
        "-segment_atclocktime", "1",     # 對齊時鐘切檔: 09:00, 09:10, 09:20 ...
        "-segment_format", "mp4",
        "-segment_format_options", "movflags=+faststart",
        "-reset_timestamps", "1",
        "-strftime", "1",
        out_pattern,
    ]


def discovery_loop():
    previous = None
    deadline = 0
    waiting_since = None
    while not stop_event.is_set():
        try:
            interfaces = discover.local_networks()
            networks = tuple(sorted(str(net) for net, _ in interfaces))
            if networks and (networks != previous or time.monotonic() >= deadline):
                attempted = time.time()
                lock = scan_store.try_scan_lock()
                if lock is None:
                    if waiting_since is None:
                        waiting_since = attempted
                else:
                    with lock:
                        if waiting_since is None or not scan_store.completed_since(waiting_since, networks):
                            scan_store.scan_and_save(interfaces, stop_event)
                    if stop_event.is_set():
                        return
                    waiting_since = None
                    previous = networks
                    deadline = time.monotonic() + 600
                    continue  # check immediately for a change during the scan
            else:
                waiting_since = None
                previous = networks
        except (OSError, ValueError, subprocess.SubprocessError) as e:
            log("discovery", str(e))
        stop_event.wait(5)


class SegmentProgress:
    """Re-list files only when directory entries change; poll the current file's size."""
    def __init__(self, directory):
        self.directory = directory
        self.dir_time = None
        self.names = set()
        self.path = None
        self.sample = self.read()  # baseline before starting this FFmpeg

    def read(self):
        stamp = os.stat(self.directory).st_mtime_ns
        if stamp != self.dir_time:
            self.dir_time = stamp
            with os.scandir(self.directory) as entries:
                names = {e.name for e in entries if SEGMENT_NAME.fullmatch(e.name)
                         and e.is_file(follow_symlinks=False)}
            new = names - self.names
            if new:
                self.path = os.path.join(self.directory, max(new))
            elif self.path and os.path.basename(self.path) not in names:
                self.path = None
            self.names = names
        if self.path:
            try:
                item = os.stat(self.path, follow_symlinks=False)
                if stat.S_ISREG(item.st_mode):
                    identity = (item.st_dev, item.st_ino) if item.st_ino else None
                    return self.path, item.st_size, identity
            except FileNotFoundError:
                pass
            self.dir_time = None
        return None, 0, None

    def advanced(self, refresh=False):
        if refresh:
            self.dir_time = None  # coarse filesystem timestamps can hide a new entry
        old_path, old_size, old_identity = self.sample
        self.sample = self.read()
        path, size, identity = self.sample
        return size > 0 and (path != old_path or identity != old_identity or size > old_size)


def drain_errors(pipe, tail):
    with pipe:
        while True:
            chunk = os.read(pipe.fileno(), 4096)
            if not chunk:
                return
            tail.extend(chunk)
            del tail[:-4096]


def finish_child(proc):
    if proc.poll() is None:
        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def _record_attempt(ip, url, ev, args, cam_dir):
    pattern = os.path.join(cam_dir, "%Y%m%d_%H%M%S.mp4")
    progress = SegmentProgress(cam_dir)
    with writers_lock:
        proc = subprocess.Popen(build_cmd(url, pattern, args.segment),
                                stdin=subprocess.DEVNULL, stderr=subprocess.PIPE)
        writers[cam_dir] = proc.pid
    tail = bytearray()
    reader = threading.Thread(target=drain_errors, args=(proc.stderr, tail), daemon=True)
    off_hours = False
    failure = ""
    name = size = last_write = None
    last_progress = time.monotonic()
    try:
        reader.start()
        while proc.poll() is None:
            off_hours = not in_schedule(args.start, args.end)
            if off_hours or ev.wait(1):
                break
            old_path, old_size, old_identity = progress.sample
            advanced = progress.advanced(refresh=time.monotonic() - last_progress >= STALL_SEC)
            path, current_size, identity = progress.sample
            same_file = (path, identity) == (old_path, old_identity)
            if not same_file or current_size < old_size:
                name = os.path.basename(path) if path else None
                last_write = None
            if advanced:
                # A new header feeds the watchdog. Only later growth confirms writing.
                if name and same_file and identity is not None and current_size > old_size:
                    last_write = time.time()
                name = os.path.basename(path)
                last_progress = time.monotonic()
            elapsed = time.monotonic() - last_progress
            size = current_size if name else None
            state = "pending_write" if name else "connecting"
            if last_write is not None and elapsed < FRESH_SEC:
                state = "recording"
            set_status(ip, state, file=name, size=size, last_write=last_write)
            if elapsed >= STALL_SEC:
                failure = "{} 無寫入進展 {:.0f} 秒".format(path or cam_dir, elapsed)
                break
    finally:
        set_status(ip, "closing", file=name, size=size, last_write=last_write)
        try:
            finish_child(proc)
        except ProcessLookupError:
            proc.wait()
        if reader.ident is not None:
            reader.join()
        else:
            proc.stderr.close()
        with writers_lock:
            writers.pop(cam_dir, None)
    return off_hours, proc.returncode, failure or tail.decode(errors="ignore").strip()


def record_loop(ip, url, ev, args):
    """錄一台攝像頭，直到 ev 被設定。"""
    cam_dir = os.path.realpath(os.path.join(args.out, ip.replace(".", "_")))
    try:
        while not ev.is_set():
            if not in_schedule(args.start, args.end):
                log(ip, "不在錄影時段 {}~{}，等待中".format(args.start, args.end))
                set_status(ip, "waiting")
                while not ev.wait(5) and not in_schedule(args.start, args.end):
                    pass
                continue
            log(ip, "開始錄影 -> {}".format(cam_dir))
            set_status(ip, "connecting")
            try:
                os.makedirs(cam_dir, exist_ok=True)
                off_hours, code, err = _record_attempt(ip, url, ev, args, cam_dir)
            except OSError as e:
                off_hours, code, err = False, None, str(e)
            if ev.is_set():
                break
            if off_hours:
                log(ip, "到達 {}，停止錄影".format(args.end))
                continue
            err = re.sub(r"(?i)(rtsps?://)[^/\s]*@", r"\1***@", err)
            log(ip, "ffmpeg 結束 (code {}) {}；{} 秒後重連".format(code, err[-300:], RETRY_SEC))
            set_status(ip, "error", err[-200:] or "連線中斷")
            ev.wait(RETRY_SEC)
    finally:
        with status_lock:
            status.pop(ip, None)
        log(ip, "已停止錄影")


def open_segments(directory, pid):
    """Linux FFmpeg descriptors. Unknown ownership protects the entire directory."""
    try:
        paths = set()
        with os.scandir("/proc/{}/fd".format(pid)) as entries:
            for entry in entries:
                try:
                    target = os.readlink(entry.path)
                except FileNotFoundError:
                    continue
                if os.path.dirname(target) == directory and SEGMENT_NAME.fullmatch(os.path.basename(target)):
                    paths.add(target)
        return paths or None
    except OSError:
        return None


def clean_disk(out_dir, min_free_gb):
    out_dir = os.path.realpath(out_dir)

    def low_space():
        return shutil.disk_usage(out_dir).free < min_free_gb * 1e9

    if not low_space():
        return
    candidates = []
    with os.scandir(out_dir) as dirs:
        for cam in dirs:
            if not CAM_DIR.fullmatch(cam.name) or not cam.is_dir(follow_symlinks=False):
                continue
            with os.scandir(cam.path) as entries:
                for entry in entries:
                    if SEGMENT_NAME.fullmatch(entry.name) and entry.is_file(follow_symlinks=False):
                        try:
                            st = entry.stat(follow_symlinks=False)
                            candidates.append((st.st_mtime_ns, entry.path, st.st_ino, st.st_size))
                        except FileNotFoundError:
                            continue
    for stamp, path, inode, size in sorted(candidates):
        if not low_space():
            return
        directory = os.path.dirname(path)
        with writers_lock:
            if directory in writers:
                opened = open_segments(directory, writers[directory])
                if opened is None or path in opened:
                    continue
            try:
                st = os.stat(path, follow_symlinks=False)
                if (stamp, inode, size) != (st.st_mtime_ns, st.st_ino, st.st_size):
                    continue
                os.remove(path)
            except FileNotFoundError:
                continue
        log("cleanup", "空間不足，刪除 " + path)
    if low_space():
        log("cleanup", "空間仍不足，沒有可安全刪除的片段")


def cleanup_loop(out_dir, min_free_gb):
    while not stop_event.wait(60):
        try:
            clean_disk(out_dir, min_free_gb)
        except OSError as e:
            log("cleanup", "錯誤: {}".format(e))


def supervise(args):
    """依設定檔 (或命令列 IP) 啟動/停止各攝像頭的錄影執行緒。"""
    workers = {}    # ip -> (thread, event, url)
    stopping = []   # 正在收尾的執行緒
    cfg = {"paused": False, "cameras": []}
    last_paused = None
    config_error = status_error = False
    while True:
        if args.ips:
            cfg = {"paused": False, "cameras": [{"ip": ip, "url": default_url(ip)} for ip in args.ips]}
        else:
            try:
                cfg = load_config()
                if config_error:
                    log("main", "錄影設定讀取已恢復")
                config_error = False
            except (ValueError, OSError) as e:
                if not config_error:
                    log("main", "沿用上一份錄影設定: {}".format(e))
                config_error = True
        if stop_event.is_set():
            want = {}
        else:
            want = {} if cfg["paused"] else {c["ip"]: c["url"] for c in cfg["cameras"]}
        if cfg["paused"] != last_paused:
            if cfg["paused"]:
                log("main", "全部暫停錄影")
            last_paused = cfg["paused"]
        stopping = [t for t in stopping if t.is_alive()]
        busy = set()
        for ip, (t, ev, url) in list(workers.items()):
            if ip not in want or want[ip] != url or not t.is_alive():
                ev.set()
                stopping.append(t)
                del workers[ip]
        for t in stopping:
            busy.add(t.name)
        for ip, url in want.items():
            if ip not in workers and ip not in busy:   # 等舊的收尾完才重開
                ev = threading.Event()
                t = threading.Thread(target=record_loop, args=(ip, url, ev, args), name=ip)
                t.start()
                workers[ip] = (t, ev, url)
        try:
            with status_lock:
                st = dict(status)
            write_json(STATUS_FILE, {
                "updated": time.time(), "pid": os.getpid(), "paused": cfg["paused"],
                "schedule": [args.start, args.end], "in_schedule": in_schedule(args.start, args.end),
                "progress_version": 1, "cameras": st,
            })
            if status_error:
                log("main", "狀態檔寫入已恢復")
            status_error = False
        except OSError as e:
            if not status_error:
                log("main", "狀態檔寫入失敗: {}".format(e))
            status_error = True
        if stop_event.is_set() and not workers and not stopping:
            break
        time.sleep(0.5 if stop_event.is_set() else 2)


def main():
    p = argparse.ArgumentParser(description="RTSP 攝像頭錄影成 mp4")
    p.add_argument("ips", nargs="*", help="只錄這些 IP (不給就依 record_config.json)")
    p.add_argument("--segment", type=int, default=SEGMENT_SEC, help="每段秒數")
    p.add_argument("--out", default=OUT_DIR, help="輸出資料夾")
    p.add_argument("--start", default=REC_START, help="每天開始錄影時間 HH:MM")
    p.add_argument("--end", default=REC_END, help="每天停止錄影時間 HH:MM")
    p.add_argument("--always", action="store_true", help="忽略時段，24 小時錄影")
    p.add_argument("--min-free-gb", type=float, default=MIN_FREE_GB)
    args = p.parse_args()
    if args.always:
        args.start = args.end = "00:00"
    for v in (args.start, args.end):
        if not re.match(r"^([01]?\d|2[0-3]):[0-5]\d$", v):
            sys.exit("時間格式錯誤: {} (要 HH:MM)".format(v))
    if not shutil.which("ffmpeg"):
        sys.exit("找不到 ffmpeg")

    for ip in args.ips:
        try:
            ipaddress.IPv4Address(ip)
        except ValueError:
            p.error("無效 IPv4: {}".format(ip))
    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, ".record_cams.lock"), "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            sys.exit("已有錄影程序使用此輸出目錄: " + args.out)
        run(args)


def run(args):
    signal.signal(signal.SIGINT, lambda *_: stop_event.set())
    signal.signal(signal.SIGTERM, lambda *_: stop_event.set())
    threading.Thread(target=discovery_loop, daemon=True).start()
    threading.Thread(target=cleanup_loop, args=(args.out, args.min_free_gb), daemon=True).start()
    supervise(args)
    try:
        os.remove(STATUS_FILE)
    except OSError:
        pass
    log("main", "結束")


if __name__ == "__main__":
    main()
