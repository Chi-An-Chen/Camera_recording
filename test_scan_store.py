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

    def test_manual_failure_keeps_previous_result_until_new_success(self):
        record_config.write_json(self.shared, self.result(1))
        with patch.object(discover, 'local_networks', side_effect=OSError('scan unavailable')):
            self.assertTrue(viewer.run_scan())
            self.wait_until_idle()
        self.assertEqual(viewer.scan_snapshot(),
                         dict(self.result(1), scanning=False, error='scan unavailable'))
        record_config.write_json(self.shared, self.result(2))
        self.assertEqual(viewer.scan_snapshot()['error'], '')
