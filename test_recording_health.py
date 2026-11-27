import os
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

import record_cams as recorder
from recording_health import camera_health


class HealthTests(unittest.TestCase):
    ip = '192.0.2.1'
    name = '20260101_090000.mp4'

    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name
        self.folder = Path(self.root) / self.ip.replace('.', '_')
        self.clip = self.folder / self.name

    def health(self, rec, now, selected=True, paused=False):
        return camera_health(self.root, self.ip, selected, paused, rec, now)

    def test_modern_heartbeat_needs_recent_positive_write(self):
        row = dict(state='recording', file=self.name, size=64, last_write=100)
        rec = dict(running=True, progress_version=1, cameras={self.ip: row})
        with patch('recording_health.os.stat', side_effect=AssertionError('unexpected disk poll')):
            self.assertEqual(self.health(rec, 114), dict(state='writing', file=self.name, size=64,
                                                        last_write=100, source='recorder'))
            self.assertEqual(self.health(rec, 115)['state'], 'pending_write')
            self.assertEqual(self.health(rec, 99)['state'], 'pending_write')
            row['size'] = 0
            self.assertIsNone(self.health(rec, 101)['last_write'])
            self.assertEqual(self.health(rec, 101)['state'], 'pending_write')
            for raw, state in [('connecting', 'connecting'), ('waiting', 'waiting'),
                               ('error', 'retrying'), ('pending_write', 'pending_write')]:
                row['state'] = raw
                self.assertEqual(self.health(rec, 101)['state'], state)

    def test_configuration_waits_for_closure(self):
        row = dict(state='recording', file=self.name, size=28, last_write=20)
        rec = dict(running=True, progress_version=1, cameras={self.ip: row})
        self.assertEqual(self.health(rec, 21, paused=True)['state'], 'closing')
        self.assertEqual(self.health(rec, 21, selected=False)['state'], 'closing')
        row['state'] = 'closing'
        self.assertEqual(self.health(rec, 21)['state'], 'closing')
        self.assertEqual(self.health(rec, 21)['file'], self.name)
        for idle in ('waiting', 'error'):
            row['state'] = idle
            self.assertEqual(self.health(rec, 21, paused=True)['state'], 'paused')
            self.assertEqual(self.health(rec, 21, selected=False)['state'], 'stopped')
        del rec['cameras'][self.ip]
        self.assertEqual(self.health(rec, 21, paused=True)['state'], 'paused')
        self.assertEqual(self.health(rec, 21, selected=False)['state'], 'stopped')
        self.assertIsNone(self.health(rec, 21, selected=False)['file'])
        self.assertEqual(self.health(rec, 21)['state'], 'confirming')

    def test_legacy_requires_growth_and_scans_directory_only_on_change(self):
        rec = dict(running=True, cameras={self.ip: dict(state='recording')})
        self.assertEqual(self.health(rec, 100)['state'], 'pending_write')
        self.folder.mkdir()
        self.clip.write_bytes(b'recently closed')
        with patch('recording_health.os.scandir', wraps=os.scandir) as scan:
            first = self.health(rec, 101)
            self.assertEqual((first['state'], first['last_write']), ('confirming', None))
            self.assertEqual(self.health(rec, 102)['state'], 'pending_write')
            with self.clip.open('ab') as f:
                f.write(b'!')
            self.assertEqual(self.health(rec, 103), dict(state='writing', source='filesystem',
                                                        file=self.name, size=16, last_write=103))
            self.assertEqual(self.health(rec, 104)['last_write'], 103)
            self.assertEqual(scan.call_count, 1)
            next_clip = self.folder / '20260101_091000.mp4'
            next_clip.write_bytes(b'already nonempty')
            os.utime(str(self.folder), (200, 200))
            rolled = self.health(rec, 105)
            self.assertEqual((rolled['file'], rolled['state'], rolled['last_write']),
                             (next_clip.name, 'confirming', None))
            self.assertEqual(scan.call_count, 2)
            next_clip.write_bytes(b'already nonempty + growing')
            self.assertEqual(self.health(rec, 106)['state'], 'writing')
            next_clip.write_bytes(b'already nonempty + growing during a long pause')
            gap = self.health(rec, 121)
            self.assertEqual((gap['state'], gap['last_write']), ('confirming', None))

    def test_stale_heartbeat_requires_same_inode_and_delta_even_after_stop_request(self):
        row = dict(state='recording', file=self.name, size=99, last_write=101)
        rec = dict(running=False, progress_version=1, cameras={self.ip: row})
        self.assertEqual(self.health(rec, 101)['state'], 'unknown')
        self.folder.mkdir()
        self.clip.write_bytes(b'old')
        os.utime(str(self.clip), (1, 1))
        self.assertEqual(self.health(rec, 102)['state'], 'confirming')
        self.assertEqual(self.health(rec, 103)['state'], 'unknown')
        replacement = self.folder / 'replacement'
        replacement.write_bytes(b'new but larger')
        os.replace(str(replacement), str(self.clip))
        self.assertEqual(self.health(rec, 104)['state'], 'confirming')
        self.clip.write_bytes(b'new but larger and growing')
        progress = self.health(rec, 105, selected=False)
        self.assertEqual((progress['state'], progress['source']), ('writing', 'filesystem'))
        self.clip.write_bytes(b'x')
        self.assertEqual(self.health(rec, 106)['state'], 'confirming')
        self.assertIsNone(self.health(rec, 107)['last_write'])
        self.clip.unlink()
        self.assertEqual(self.health(rec, 108, paused=True)['state'], 'unknown')

    def test_legacy_connecting_can_detect_growth_but_a_reconnect_needs_new_samples(self):
        row = dict(state='connecting', since=95)
        rec = dict(running=True, pid=123, cameras={self.ip: row})
        self.folder.mkdir()
        self.clip.write_bytes(b'header')
        self.assertEqual(self.health(rec, 100)['state'], 'connecting')
        self.clip.write_bytes(b'header and frames')
        self.assertEqual(self.health(rec, 101)['state'], 'writing')
        row['since'] = 102
        self.assertEqual(self.health(rec, 102)['state'], 'connecting')
        rec['running'] = False
        self.assertEqual(self.health(rec, 103)['state'], 'unknown')
        self.clip.write_bytes(b'header and frames continue')
        self.assertEqual(self.health(rec, 104)['state'], 'writing')

    def test_worker_progress_reconnect_and_release(self):
        self.folder.mkdir()
        self.clip.write_bytes(b'history')
        new = self.folder / '20260101_091000.mp4'
        next_clip = self.folder / '20260101_092000.mp4'
        args = SimpleNamespace(out=self.root, start='00:00', end='00:00', segment=600)
        clock = SimpleNamespace(now=1000, stopped=False)
        event = Mock()
        event.is_set.side_effect = lambda: clock.stopped
        steps = iter(['history', 'empty', 'grow', 'unchanged', 'age', 'disconnect',
                      'restart', 'next', 'replace', 'grow2', 'truncate', 'regrow', 'stop'])
        snapshots = {}
        processes, closing = [], []

        def launch(*args, **kwargs):
            proc = Mock(pid=len(processes) + 1, returncode=None)
            proc.poll.side_effect = lambda: proc.returncode
            processes.append(proc)
            return proc

        def wait(seconds):
            if seconds == recorder.RETRY_SEC:
                snapshots['retry'] = recorder.status[self.ip]
                return False
            self.assertEqual(seconds, 1)
            step = next(steps)
            snapshots[step] = recorder.status[self.ip]
            clock.now += 15 if step == 'age' else 1
            if step == 'empty':
                new.touch()
                os.utime(str(self.folder), (200, 200))
            elif step == 'grow':
                new.write_bytes(b'data')
            elif step == 'disconnect':
                processes[-1].returncode = 1
            elif step == 'next':
                next_clip.write_bytes(b'new data')
                os.utime(str(self.folder), (300, 300))
            elif step == 'replace':
                replacement = self.folder / 'replacement'
                replacement.write_bytes(b'longer replacement')
                os.replace(str(replacement), str(next_clip))
            elif step == 'grow2':
                with next_clip.open('ab') as f:
                    f.write(b'!')
            elif step == 'truncate':
                next_clip.write_bytes(b'x')
            elif step == 'regrow':
                next_clip.write_bytes(b'xy')
            elif step == 'stop':
                clock.stopped = True
            return clock.stopped

        def finish(proc):
            closing.append(recorder.status[self.ip])
            self.assertEqual(recorder.writers[str(self.folder.resolve())], proc.pid)
            if proc.returncode is None:
                proc.returncode = 0

        event.wait.side_effect = wait
        with patch.object(recorder, 'status', {}), patch.object(recorder, 'writers', {}), \
                patch.object(recorder, 'log'), patch.object(recorder, 'drain_errors'), \
                patch.object(recorder, 'finish_child', side_effect=finish), \
                patch.object(recorder.time, 'time', side_effect=lambda: clock.now), \
                patch.object(recorder.time, 'monotonic', side_effect=lambda: clock.now), \
                patch.object(recorder.subprocess, 'Popen', side_effect=launch):
            recorder.record_loop(self.ip, 'rtsp://example.invalid', event, args)
            self.assertEqual(recorder.status, {})
            self.assertEqual(recorder.writers, {})
        def fields(row):
            return tuple(row[key] for key in ('state', 'file', 'size', 'last_write'))
        self.assertEqual(fields(snapshots['empty']), ('connecting', None, None, None))
        self.assertEqual(fields(snapshots['grow']), ('pending_write', new.name, 0, None))
        written = ('recording', new.name, 4, 1003)
        self.assertEqual(fields(snapshots['unchanged']), written)
        self.assertEqual(fields(snapshots['age']), written)
        self.assertEqual(fields(snapshots['disconnect']), ('pending_write', new.name, 4, 1003))
        self.assertEqual(fields(snapshots['retry']), ('error', None, None, None))
        self.assertEqual(fields(snapshots['restart']), ('connecting', None, None, None))
        self.assertEqual(fields(snapshots['next']), ('connecting', None, None, None))
        self.assertEqual(fields(snapshots['replace']), ('pending_write', next_clip.name, 8, None))
        self.assertEqual(fields(snapshots['grow2']), ('pending_write', next_clip.name, 18, None))
        self.assertEqual(fields(snapshots['truncate']), ('recording', next_clip.name, 19, 1024))
        self.assertEqual(fields(snapshots['regrow']), ('pending_write', next_clip.name, 1, None))
        self.assertEqual(fields(closing[-1]), ('closing', next_clip.name, 2, 1026))

    def test_header_only_at_29_seconds_feeds_watchdog_without_proving_recording(self):
        self.folder.mkdir()
        args = SimpleNamespace(start='00:00', end='00:00', segment=600)
        clock = SimpleNamespace(now=0)
        steps = iter([29, 30, 58, 59])
        observed = []

        def tick(_):
            if clock.now:
                row = recorder.status[self.ip]
                observed.append((clock.now, row['state'], row['size'], row['last_write']))
            clock.now = next(steps)
            if clock.now == 29:
                self.clip.write_bytes(b'ftyp')
            return False

        proc = Mock(pid=123, returncode=0)
        proc.poll.return_value = None
        with patch.object(recorder, 'status', {}), patch.object(recorder, 'writers', {}), \
                patch.object(recorder.time, 'monotonic', side_effect=lambda: clock.now), \
                patch.object(recorder, 'drain_errors'), patch.object(recorder, 'finish_child'), \
                patch.object(recorder.subprocess, 'Popen', return_value=proc):
            _, _, error = recorder._record_attempt(self.ip, '', Mock(wait=tick), args, str(self.folder))
        self.assertEqual(observed, [(t, 'pending_write', 4, None) for t in (29, 30, 58)])
        self.assertIn('30 秒', error)

    def test_snapshot_versions_empty_worker_list(self):
        stop = threading.Event()
        stop.set()
        args = SimpleNamespace(ips=[], start='00:00', end='00:00')
        with patch.object(recorder, 'stop_event', stop), patch.object(recorder, 'status', {}), \
                patch.object(recorder, 'load_config', return_value=dict(paused=True, cameras=[])), \
                patch.object(recorder, 'log'), patch.object(recorder, 'write_json') as write:
            recorder.supervise(args)
        snapshot = write.call_args[0][1]
        self.assertEqual((snapshot['progress_version'], snapshot['cameras']), (1, {}))


if __name__ == '__main__':
    unittest.main()
