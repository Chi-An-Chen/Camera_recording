import json
import os
import subprocess
import sys
import threading
import time
import unittest
from tempfile import TemporaryDirectory
from unittest.mock import patch

import discover
import record_cams as recorder
import record_config
import scan_store
import web_viewer as viewer


class SharedScanTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.shared = os.path.join(tmp.name, 'recorder_discovered.json')
        self.legacy = os.path.join(tmp.name, 'discovered.json')
        for p in (patch.object(scan_store, 'DISCOVERED_FILE', self.shared),
                  patch.object(viewer, 'LEGACY_DISCOVERED_FILE', self.legacy),
                  patch.object(viewer, '_scan_files', {}),
                  patch.object(viewer, '_scan_failure', (None, ''))):
            p.start()
            self.addCleanup(p.stop)

    def result(self, stamp):
        return {'time': stamp, 'seconds': 1, 'networks': ['192.0.2.0/24'], 'cameras': []}

    def wait_until_idle(self):
        deadline = time.monotonic() + 3
        while scan_store.scan_running() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertFalse(scan_store.scan_running())

    def test_legacy_fallback_and_cached_atomic_refresh(self):
        camera = {'ip': '192.0.2.10', 'port': 554, 'path': '/h264', 'name': '', 'status': 'ok'}
        record_config.write_json(self.legacy, dict(self.result(1), cameras=[camera], scanning=True))
        with patch.object(json, 'load', wraps=json.load) as loads:
            self.assertEqual(viewer.scan_snapshot()['time'], 1)
            self.assertEqual(viewer.scan_snapshot()['cameras'], [camera])
            self.assertFalse(viewer.scan_snapshot()['scanning'])
            self.assertEqual(loads.call_count, 1)
            record_config.write_json(self.shared, self.result(2))
            self.assertEqual(viewer.scan_snapshot()['time'], 2)
            record_config.write_json(self.shared, self.result(3))
            self.assertEqual(viewer.scan_snapshot()['time'], 3)
            record_config.write_json(self.shared, {})
            self.assertEqual(viewer.scan_snapshot()['time'], 1)
            self.assertEqual(loads.call_count, 4)
            record_config.write_json(self.shared, dict(self.result(4), cameras=[{'ip': 'bad'}]))
            self.assertEqual(viewer.scan_snapshot()['time'], 1)
        self.assertFalse(os.path.exists(self.shared + '.lock'))

    def test_live_list_follows_scan_even_when_old_cameras_are_retrying(self):
        old, current = '192.0.2.1', '192.0.2.2'
        cfg = dict(paused=False, cameras=[dict(ip=old)])
        rec = dict(running=True, cameras={old: dict(state='retrying')})
        with patch.object(viewer, 'rec_config', return_value=cfg), \
                patch.object(viewer, 'rec_status', return_value=rec):
            for ips in ([old, current], [current], []):
                devices = [dict(ip=ip, status='auth') for ip in ips]
                record_config.write_json(self.shared, dict(self.result(1), cameras=devices))
                live = viewer.live_summary()
                self.assertEqual([row['ip'] for row in live['cameras']], ips)
                self.assertEqual(set(live['rec']['cameras']), {old}.intersection(ips))
                self.assertEqual([c['ip'] for c in viewer.archive_cameras()], ips)

    def test_manual_scan_parks_missing_same_subnet_camera_and_keeps_its_files(self):
        a, b = '192.0.2.5', '192.0.2.6'
        root = os.path.dirname(self.shared)
        old_dir = os.path.join(root, a.replace('.', '_'))
        os.mkdir(old_dir)
        clip = os.path.join(old_dir, '20260101_090000.mp4')
        with open(clip, 'wb') as f:
            f.write(b'old clip')
        def result(ips):
            return dict(self.result(time.time()), cameras=[
                dict(ip=ip, port=554, path='/video', status='ok') for ip in ips])

        stop = threading.Event()
        with patch.object(record_config, 'CONFIG_FILE', self.shared + '.config'), \
                patch.object(recorder, 'auto_present', frozenset()), \
                patch.object(recorder, 'stop_event', stop), \
                patch.object(discover, 'local_networks', return_value=[('192.0.2.0/24', '192.0.2.1')]), \
                patch.object(discover, 'scan_interfaces', return_value=result([a, b])) as probe:
            record_config.save_config(dict(cameras=[], auto_record_discovered=True))
            def publish_or_stop(_):
                if a in recorder.auto_present:
                    record_config.write_json(self.shared, result([b]))
                else:
                    stop.set()
            with patch.object(stop, 'wait', side_effect=publish_or_stop):
                recorder.discovery_loop(True, root)
            self.assertEqual(probe.call_count, 1)
            self.assertEqual(recorder.auto_present, {b})
            cfg = record_config.load_config()
            self.assertEqual([c['ip'] for c in cfg['other_network_cameras']], [a])
            self.assertEqual([c['ip'] for c in cfg['cameras']], [b])
            self.assertTrue(os.path.isfile(clip))
            os.unlink(clip)
            os.rmdir(old_dir)
            recorder.select_discovered(result([b]), {a, b})
            self.assertFalse(os.path.exists(old_dir))

    def test_held_recorder_lock_suppresses_manual_scan_in_another_process(self):
        child = """
import json, sys
from unittest.mock import patch
import scan_store, web_viewer as viewer
scan_store.DISCOVERED_FILE, viewer.LEGACY_DISCOVERED_FILE = sys.argv[1:]
with patch.object(scan_store, 'scan_and_save'):
    print(json.dumps([viewer.scan_snapshot()['scanning'], viewer.run_scan()]))
"""
        with scan_store.try_scan_lock():
            result = subprocess.run([sys.executable, '-B', '-c', child, self.shared, self.legacy],
                                    stdout=subprocess.PIPE, check=True, timeout=5)
            self.assertEqual(json.loads(result.stdout.decode()), [True, False])
        self.assertFalse(viewer.scan_snapshot()['scanning'])

    def test_manual_scan_works_alone_and_reports_completion(self):
        entered, release = threading.Event(), threading.Event()
        interfaces = [('192.0.2.0/24', '192.0.2.1')]

        def scan(nets, workers, stop):
            self.assertEqual(nets, interfaces)
            self.assertEqual(workers, 16)
            self.assertIsNone(stop)
            entered.set()
            self.assertTrue(release.wait(2))
            return self.result(7)

        with patch.object(discover, 'local_networks', return_value=interfaces), \
                patch.object(discover, 'scan_interfaces', side_effect=scan) as probe:
            self.assertTrue(viewer.run_scan())
            try:
                self.assertTrue(entered.wait(1))
                self.assertTrue(viewer.scan_snapshot()['scanning'])
                self.assertFalse(viewer.run_scan())
            finally:
                release.set()
                self.wait_until_idle()
            self.assertEqual(probe.call_count, 1)
        self.assertEqual(viewer.scan_snapshot(), dict(self.result(7), scanning=False, error=''))
        self.assertFalse(os.path.exists(self.legacy))

    def test_recorder_reuses_only_a_new_matching_result_after_contention(self):
        network = '192.0.2.0/24'
        interfaces = [(network, '192.0.2.1')]
        for published, stamp, expected_scans in [(network, None, 0), ('198.51.100.0/24', None, 1),
                                                 (network, 1, 1)]:
            stop = threading.Event()
            with self.subTest(published=published, stamp=stamp), scan_store.try_scan_lock() as manual:
                def next_check(_):
                    if manual.closed:
                        stop.set()
                    else:
                        result = dict(self.result(stamp or time.time()), networks=[published])
                        record_config.write_json(self.shared, result)
                        manual.close()

                with patch.object(recorder, 'stop_event', stop), patch.object(stop, 'wait', next_check), \
                        patch.object(discover, 'local_networks', side_effect=[interfaces] * 3), \
                        patch.object(discover, 'scan_interfaces', return_value=self.result(2)) as probe:
                    recorder.discovery_loop()
                self.assertEqual(probe.call_count, expected_scans)

    def test_auto_record_switch_return_pause_and_manual_stop(self):
        a, b, c = '192.0.2.5', '198.51.100.7', '192.0.2.8'
        def result(network, ips):
            return dict(networks=[network], cameras=[
                dict(ip=ip, status='ok', port=8554, path='/video') for ip in ips])
        first = result('192.0.2.0/24', [a, b, c])
        first['cameras'].append(dict(ip='192.0.2.9', status='auth'))
        second = result('198.51.100.0/24', [b])
        original = dict(ip=a, url='rtsp://example.invalid/custom')
        cfg = dict(paused=True, cameras=[original])
        with patch.object(record_config, 'CONFIG_FILE', self.shared + '.config'):
            record_config.save_config(cfg)
            self.assertFalse(recorder.select_discovered(first, set()))
            self.assertEqual(record_config.load_config(), cfg)
            record_config.save_config(dict(cfg, auto_record_discovered=True))
            seen = recorder.select_discovered(first, set())
            stored = record_config.load_config()
            self.assertEqual(seen, {a, c})
            self.assertTrue(stored['paused'])
            self.assertEqual(stored['cameras'][0], original)
            self.assertEqual(stored['cameras'][1]['url'], 'rtsp://192.0.2.8:8554/video')
            self.assertTrue(viewer.update_config({'ip': c, 'record': False})['ok'])
            recorder.select_discovered(first, seen)
            self.assertEqual(record_config.load_config()['cameras'], [original])
            recorder.select_discovered(second, set())
            moved = record_config.load_config()
            self.assertEqual([x['ip'] for x in moved['cameras']], [b])
            self.assertEqual(moved['other_network_cameras'], [original])
            recorder.select_discovered(first, set())
            returned = record_config.load_config()
            self.assertEqual(returned['cameras'], stored['cameras'])
            self.assertEqual([x['ip'] for x in returned['other_network_cameras']], [b])

    def test_manual_failure_keeps_previous_result_until_new_success(self):
        record_config.write_json(self.shared, self.result(1))
        with patch.object(discover, 'local_networks', side_effect=OSError('scan unavailable')):
            self.assertTrue(viewer.run_scan())
            self.wait_until_idle()
        self.assertEqual(viewer.scan_snapshot(),
                         dict(self.result(1), scanning=False, error='scan unavailable'))
        record_config.write_json(self.shared, self.result(2))
        self.assertEqual(viewer.scan_snapshot()['error'], '')
