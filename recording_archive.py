"""Inspect conventional FFmpeg MP4 segments and delete only verified, closed files."""
import errno
import fcntl
import os
import re
import signal
import stat
import struct
import subprocess
import sys
import threading
from collections import OrderedDict
from contextlib import contextmanager


CAM = re.compile(r"[0-9]{1,3}(?:_[0-9]{1,3}){3}")
SEGMENT = re.compile(r"[0-9]{8}_[0-9]{6}\.mp4")
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
_complete_cache = OrderedDict()
_cache_lock = threading.Lock()


def prepare_writer_checks():
    """Call from the viewer's main thread before serving threaded requests."""
    if sys.platform.startswith("linux") and signal.getsignal(signal.SIGIO) == signal.SIG_DFL:
        signal.signal(signal.SIGIO, signal.SIG_IGN)


@contextmanager
def _camera(root, cam):
    if not isinstance(cam, str) or not CAM.fullmatch(cam):
        raise ValueError("invalid camera")
    root_fd = os.open(root, DIR_FLAGS)
    try:
        camera_fd = os.open(cam, DIR_FLAGS, dir_fd=root_fd)
        try:
            yield root_fd, camera_fd
        finally:
            os.close(camera_fd)
    finally:
        os.close(root_fd)


def _regular_stat(camera_fd, name):
    if not isinstance(name, str) or not SEGMENT.fullmatch(name):
        raise ValueError("invalid segment")
    info = os.stat(name, dir_fd=camera_fd, follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("not a regular segment")
    return info


def _open_at(camera_fd, name):
    fd = os.open(name, FILE_FLAGS, dir_fd=camera_fd)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("not a regular segment")
        return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise


def open_clip(root, cam, name, expected=None):
    """Open a regular clip inside the requested camera; the caller closes it."""
    with _camera(root, cam) as (_, camera_fd):
        _regular_stat(camera_fd, name)
        clip = _open_at(camera_fd, name)
        if expected is not None and expected != _identity(os.fstat(clip.fileno())):
            clip.close()
            raise FileNotFoundError("segment changed")
        return clip


def _identity(info):
    return "{:x}-{:x}-{:x}-{:x}-{:x}".format(
        info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _has_trailer(fd, length):
    """Read only box headers; an unfinished recorder mdat can span to EOF."""
    offset = 0
    found = set()
    for _ in range(64):
        if offset == length:
            return {b"ftyp", b"moov", b"mdat"} <= found
        header = os.pread(fd, 16, offset)
        if len(header) < 8:
            return False
        size, kind = struct.unpack_from(">I4s", header)
        header_size = 8
        if size == 1:
            if len(header) < 16:
                return False
            size = struct.unpack_from(">Q", header, 8)[0]
            header_size = 16
        if size < header_size or size > length - offset or kind == b"moof":
            return False
        if size > header_size:
            found.add(kind)
        offset += size
    return False


def _complete(fd, info, fresh):
    version = _identity(info)
    if not fresh:
        with _cache_lock:
            if version in _complete_cache:
                _complete_cache.move_to_end(version)
                return True
    if not _has_trailer(fd, info.st_size) or _identity(os.fstat(fd)) != version:
        return False
    with _cache_lock:
        _complete_cache[version] = True
        if len(_complete_cache) > 4096:
            _complete_cache.popitem(last=False)
    return True


def _mac_writers(directory, camera_fd):
    """One lsof snapshot for this camera; our directory fd must be visible."""
    try:
        result = subprocess.run(
            ["/usr/sbin/lsof", "-nP", "-FfaDi", "+d", directory],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)
        if result.returncode not in (0, 1) or result.stderr:
            return None
        directory_stat = os.fstat(camera_fd)
        sentinel = (directory_stat.st_dev, directory_stat.st_ino)
        seen_directory = False
        writers = set()
        pid = number = access = device = None
        for field in result.stdout.splitlines():
            tag, value = field[:1], field[1:]
            if tag == b"p":
                pid = int(value)
            elif tag == b"f":
                number, access, device = value, None, None
            elif tag == b"a":
                access = value
            elif tag == b"D":
                device = int(value, 0)
            elif tag == b"i" and device is not None:
                key = (device, int(value))
                if key == sentinel and pid == os.getpid() and number == str(camera_fd).encode():
                    seen_directory = True
                if access != b"r":
                    writers.add(key)
        return writers if seen_directory else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def _writer_state(fd, info, writers):
    if sys.platform == "darwin":
        if writers is None or info.st_uid != os.geteuid():
            return "unverified"
        return "open" if (info.st_dev, info.st_ino) in writers else ""
    if not sys.platform.startswith("linux") or signal.getsignal(signal.SIGIO) != signal.SIG_IGN:
        return "unverified"
    try:
        # A read lease conflicts with every writable descriptor, including stalled FFmpeg.
        fcntl.fcntl(fd, fcntl.F_SETLEASE, fcntl.F_RDLCK)
        return ""
    except BlockingIOError:
        return "open"
    except (AttributeError, OSError):
        return "unverified"


def _error_reason(error):
    if isinstance(error, FileNotFoundError):
        return "missing"
    if isinstance(error, (ValueError, NotADirectoryError)) or getattr(error, "errno", None) == errno.ELOOP:
        return "invalid"
    if isinstance(error, BlockingIOError):
        return "open"
    return "io_error"


def _inspect(camera_fd, name, info, writers, delete_in=None):
    deleting = delete_in is not None
    version = _identity(info)
    with _open_at(camera_fd, name) as clip:
        fd = clip.fileno()
        if _identity(os.fstat(fd)) != version:
            return "changed"
        reason = _writer_state(fd, info, writers)
        if reason:
            return reason
        if not _complete(fd, info, fresh=deleting):
            return "incomplete"
        if deleting:
            root_fd, cam = delete_in
            current = os.stat(cam, dir_fd=root_fd, follow_symlinks=False)
            if not os.path.samestat(current, os.fstat(camera_fd)):
                return "changed"
        if _identity(_regular_stat(camera_fd, name)) != version:
            return "changed"
        if sys.platform.startswith("linux"):
            # The kernel reports a pending conflicting open before releasing our lease.
            try:
                lease = fcntl.fcntl(fd, fcntl.F_GETLEASE)
            except (AttributeError, OSError):
                return "unverified"
            if lease != fcntl.F_RDLCK:
                return "open"
        if deleting:
            os.unlink(name, dir_fd=camera_fd)
        return ""


def _snapshot(directory, camera_fd):
    if sys.platform == "darwin":
        return _mac_writers(directory, camera_fd)
    if threading.current_thread() is threading.main_thread():
        prepare_writer_checks()
    return None


def list_files(root, cam):
    records = []
    try:
        with _camera(root, cam) as (_, camera_fd):
            files = []
            for name in sorted(os.listdir(camera_fd), reverse=True):
                try:
                    files.append((name, _regular_stat(camera_fd, name)))
                except (FileNotFoundError, ValueError):
                    continue
            writers = _snapshot(os.path.join(root, cam), camera_fd) if files else None
            for name, info in files:
                try:
                    reason = _inspect(camera_fd, name, info, writers)
                except (OSError, ValueError) as error:
                    reason = _error_reason(error)
                    if reason in ("missing", "invalid"):
                        continue
                records.append({"name": name, "size": info.st_size, "mtime": info.st_mtime,
                                "day": "{}-{}-{}".format(name[:4], name[4:6], name[6:8]),
                                "identity": _identity(info), "recording": bool(reason),
                                "closed": not reason, "reason": reason})
    except (FileNotFoundError, NotADirectoryError):
        pass
    except OSError as error:
        if error.errno != errno.ELOOP:
            raise
    return records


def delete_files(root, cam, names, expected=None):
    """Delete this fixed name list; a supplied snapshot must match every target."""
    if not isinstance(names, (tuple, list)) or not all(isinstance(n, str) for n in names):
        raise ValueError("invalid segment list")
    if expected is not None and not isinstance(expected, dict):
        raise ValueError("invalid segment identities")
    names = list(OrderedDict.fromkeys(names))
    deleted, reasons = [], {}
    try:
        with _camera(root, cam) as (root_fd, camera_fd):
            files = []
            for name in names:
                try:
                    info = _regular_stat(camera_fd, name)
                    if expected is not None and expected.get(name) != _identity(info):
                        reasons[name] = "changed"
                    else:
                        files.append((name, info))
                except (OSError, ValueError) as error:
                    reasons[name] = _error_reason(error)
            writers = _snapshot(os.path.join(root, cam), camera_fd) if files else None
            for name, info in files:
                try:
                    reason = _inspect(camera_fd, name, info, writers, delete_in=(root_fd, cam))
                except (OSError, ValueError) as error:
                    reason = _error_reason(error)
                if reason:
                    reasons[name] = reason
                else:
                    deleted.append(name)
    except (OSError, ValueError) as error:
        removed = set(deleted)
        for name in names:
            if name not in removed:
                reasons.setdefault(name, _error_reason(error))
    return {"deleted": deleted, "skipped": [name for name in names if name in reasons],
            "reasons": reasons}
