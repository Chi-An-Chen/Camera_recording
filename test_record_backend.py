import json
import os
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import discover
import record_cams as recorder
import scan_store


class RecorderTests(unittest.TestCase):
    def test_second_port_after_auth_or_missing_path(self):
        for code in (401, 404):
            def describe(ip, port, path):
                return (200, '', 'm=video') if port == 8554 else (code, '', '')

            with patch.object(discover, 'port_open', return_value=True), \
                    patch.object(discover, 'rtsp_describe', side_effect=describe):
                result = discover.probe_host('192.0.2.1')
            self.assertEqual((result['status'], result['port']), ('ok', 8554))

    def test_startup_network_change_and_periodic_scan(self):
        first = [('192.0.2.0/24', '192.0.2.1')]
        second = [('198.51.100.0/24', '198.51.100.1')]
        stop = threading.Event()
        scans = []

        def scan(interfaces, **kwargs):
            scans.append(interfaces)
            self.assertEqual(kwargs['workers'], 16)
            self.assertIs(kwargs['stop'], stop)
            if len(scans) == 3:
                stop.set()
            return {'networks': [interfaces[0][0]], 'cameras': []}

        with TemporaryDirectory() as tmp:
            output = str(Path(tmp) / 'recorder_discovered.json')
            with patch.object(recorder, 'stop_event', stop), \
                    patch.object(scan_store, 'DISCOVERED_FILE', output), \
                    patch.object(discover, 'local_networks', side_effect=[first, second, second]), \
                    patch.object(discover, 'scan_interfaces', side_effect=scan), \
                    patch.object(recorder.time, 'monotonic', side_effect=[0, 1, 602, 602]):
                recorder.discovery_loop()
            self.assertEqual(scans, [first, second, second])
            self.assertEqual(json.loads(Path(output).read_text())['networks'], [second[0][0]])

    def test_progress_ignores_history_and_tracks_rollover(self):
        with TemporaryDirectory() as tmp:
            old = Path(tmp) / '20260101_090000.mp4'
            old.write_bytes(b'old')
            os.utime(tmp, (100, 100))
            probe = recorder.SegmentProgress(tmp)
            self.assertFalse(probe.advanced())
            current = Path(tmp) / '20260101_091000.mp4'
            current.touch()
            os.utime(tmp, (101, 101))
            self.assertFalse(probe.advanced())
            current.write_bytes(b'data')
            self.assertTrue(probe.advanced())
            self.assertFalse(probe.advanced())
            next_clip = Path(tmp) / '20260101_092000.mp4'
            next_clip.write_bytes(b'next')
            # Jetson ext4 can timestamp two directory changes in the same clock tick.
            os.utime(tmp, (101, 101))
            self.assertFalse(probe.advanced())
            self.assertTrue(probe.advanced(refresh=True))
            next_clip.unlink()
            self.assertFalse(probe.advanced())

    def test_open_files_match_only_this_camera(self):
        with TemporaryDirectory() as tmp:
            directory = str(Path(tmp).resolve())
            fd_dir = Path(directory) / 'fd'
            fd_dir.mkdir()
            clip = str(Path(directory) / '20260101_090000.mp4')
            (fd_dir / '3').symlink_to(clip)
            (fd_dir / '4').symlink_to('/different/20260101_090000.mp4')
            scandir = os.scandir
            with patch.object(os, 'scandir', side_effect=lambda _: scandir(str(fd_dir))):
                self.assertEqual(recorder.open_segments(directory, 1), {clip})
                (fd_dir / '3').unlink()
                self.assertIsNone(recorder.open_segments(directory, 1))

    def test_cleanup_stops_as_soon_as_oldest_frees_enough(self):
        with TemporaryDirectory() as tmp:
            folder = Path(tmp) / '192_0_2_1'
            folder.mkdir()
            oldest = folder / '20260101_090000.mp4'
            newer = folder / '20260101_091000.mp4'
            for clip, stamp in [(newer, 2), (oldest, 1)]:
                clip.touch()
                os.utime(str(clip), (stamp, stamp))

            def usage(_):
                return SimpleNamespace(free=0 if oldest.exists() else 5e9)

            with patch.object(recorder.shutil, 'disk_usage', side_effect=usage):
                recorder.clean_disk(tmp, 5)
            self.assertFalse(oldest.exists())
            self.assertTrue(newer.exists())

    def test_cleanup_protects_three_writers_and_unknown_descriptors(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            writers = {}
            protected = {}
            for pid in range(1, 5):
                folder = root / ('192_0_2_%s' % pid)
                folder.mkdir()
                active = folder / '20260101_092000.mp4'
                active.touch()
                (folder / '20260101_091000.mp4').touch()
                (folder / 'other.mp4').touch()
                writers[str(folder)] = pid
                protected[pid] = {str(active)} if pid < 4 else None
            (root / 'unrelated.mp4').touch()
            with patch.object(recorder, 'writers', writers), \
                    patch.object(recorder, 'open_segments', side_effect=lambda d, p: protected[p]), \
                    patch.object(recorder.shutil, 'disk_usage', return_value=SimpleNamespace(free=0)):
                recorder.clean_disk(str(root), 5)
            for directory, pid in writers.items():
                folder = Path(directory)
                self.assertTrue((folder / '20260101_092000.mp4').exists())
                self.assertTrue((folder / 'other.mp4').exists())
                self.assertEqual((folder / '20260101_091000.mp4').exists(), pid == 4)
            self.assertTrue((root / 'unrelated.mp4').exists())


if __name__ == '__main__':
    unittest.main()
