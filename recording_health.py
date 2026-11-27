"""Recording evidence for the viewer, including older recorder heartbeats."""
import ipaddress
import os
import re
import stat
import threading
from collections import namedtuple


FRESH_SEC = 15
SEGMENT_NAME = re.compile(r"[0-9]{8}_[0-9]{6}\.mp4")
_Sample = namedtuple('_Sample', 'directory_stamp file identity size sampled last_write worker')
_samples = {}  # camera directory -> last foreground observation
_lock = threading.Lock()


def _health(state, source=None, file=None, size=None, last_write=None):
    return dict(state=state, source=source, file=file, size=size, last_write=last_write)


def _sample_file(directory, now, expected, worker):
    with _lock:
        previous = _samples.get(directory)
        try:
            folder = os.stat(directory, follow_symlinks=False)
            if not stat.S_ISDIR(folder.st_mode):
                raise NotADirectoryError(directory)
            stamp = (folder.st_dev, folder.st_ino, folder.st_mtime_ns)
            if previous and previous.directory_stamp == stamp:
                name = previous.file
            else:
                with os.scandir(directory) as entries:
                    name = max((e.name for e in entries if SEGMENT_NAME.fullmatch(e.name)
                                and e.is_file(follow_symlinks=False)), default=None)
            identity = size = None
            if name:
                item = os.stat(os.path.join(directory, name), follow_symlinks=False)
                if not stat.S_ISREG(item.st_mode):
                    raise OSError('segment replaced')
                identity = (item.st_dev, item.st_ino) if item.st_ino else None
                size = item.st_size
        except OSError:
            _samples.pop(directory, None)
            return _health('pending_write' if expected else 'unknown')

        comparable = (identity is not None and previous and previous.file == name
                      and previous.identity == identity and size >= previous.size
                      and (worker is None or previous.worker == worker)
                      and 0 <= now - previous.sampled < FRESH_SEC)
        last_write = None
        if comparable:
            last_write = now if size > previous.size else previous.last_write
        _samples[directory] = _Sample(stamp, name, identity, size, now, last_write, worker)

    if last_write is not None and 0 <= now - last_write < FRESH_SEC:
        state = 'writing'
    elif name and not comparable:
        state = 'confirming'
    else:
        state = 'pending_write' if expected else 'unknown'
    return _health(state, 'filesystem' if name else None, name, size, last_write)


def camera_health(out_dir, ip, selected, paused, rec, now):
    """Combine requested settings, a fresh worker state and observed file growth."""
    try:
        directory = os.path.join(out_dir, str(ipaddress.IPv4Address(ip)).replace('.', '_'))
    except ValueError:
        return _health('unknown')
    running = rec.get('running') is True
    workers = rec.get('cameras')
    row = workers.get(ip) if running and isinstance(workers, dict) else None
    row = row if isinstance(row, dict) else {}
    state = row.get('state')
    wanted = selected and not paused
    modern = all(key in row for key in ('file', 'size', 'last_write'))

    if running and isinstance(workers, dict):
        file = row.get('file')
        file = file if isinstance(file, str) and SEGMENT_NAME.fullmatch(file) else None
        size = row.get('size')
        size = size if file and type(size) is int and size >= 0 else None
        last = row.get('last_write')
        last = last if size and type(last) in (float, int) and 0 <= last <= now else None

        result = None
        if state == 'closing' or (not wanted and row and state not in ('waiting', 'error')):
            result = _health('closing', 'recorder', file, size, last)
        elif not wanted:
            result = _health('paused' if selected else 'stopped', 'recorder')
        elif state in ('waiting', 'error') or (modern and state == 'connecting'):
            result = _health('retrying' if state == 'error' else state, 'recorder')
        elif modern and state in ('recording', 'pending_write'):
            writing = state == 'recording' and last is not None and now - last < FRESH_SEC
            result = _health('writing' if writing else 'pending_write', 'recorder', file, size, last)
        elif not row and rec.get('in_schedule') is False:
            result = _health('waiting', 'recorder')
        elif not row and rec.get('progress_version') == 1:
            result = _health('confirming', 'recorder')
        if result is not None:
            # A new or interrupted worker must start a new filesystem comparison.
            with _lock:
                _samples.pop(directory, None)
            return result

    worker = (rec.get('pid'), state, row.get('since')) if running else None
    sampled = _sample_file(directory, now, expected=running and wanted, worker=worker)
    if state == 'connecting' and sampled['state'] != 'writing':
        return _health('connecting', 'recorder')
    return sampled
