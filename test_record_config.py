import contextlib
import fcntl
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import discover
import record_config as config
import record_cams as recorder
import web_viewer as viewer


class ConfigTests(unittest.TestCase):
    def test_batch_record_is_atomic_and_preserves_other_settings(self):
        existing = {'ip': '192.0.2.1', 'url': 'rtsp://example.invalid/first', 'name': 'custom'}
        ips = ['192.0.2.2', '192.0.2.3']
        original = {'paused': True, 'cameras': [existing]}
        with TemporaryDirectory() as tmp, patch.object(config, 'CONFIG_FILE', tmp + '/settings'), \
                patch.object(viewer, 'discovered_urls', return_value={ip: config.default_url(ip) for ip in ips}), \
                patch.object(viewer, '_last_config', {}):
            config.save_config(original)
            with patch.object(config, 'save_config', wraps=config.save_config) as save:
                for bad in ([ips[0], '192.0.2.99'], [ips[0], 'invalid'], [], ips[0]):
                    self.assertIn('error', viewer.update_config({'ips': bad, 'record': True}))
                save.assert_not_called()
                self.assertEqual(config.load_config(), original)
                self.assertTrue(viewer.update_config({'ips': ips + ips, 'record': True})['ok'])
                self.assertEqual(save.call_count, 1)
            cfg = config.load_config()
            self.assertTrue(cfg['paused'])
            self.assertEqual(cfg['cameras'], [existing] + [{'ip': ip, 'url': config.default_url(ip)} for ip in ips])

    def test_creation_legacy_and_empty_selection(self):
        with TemporaryDirectory() as tmp, patch.object(config, 'CONFIG_FILE', tmp + '/settings'):
            self.assertEqual(config.load_config(), {'paused': False, 'cameras': []})
            Path(config.CONFIG_FILE).write_text('{"cameras":[{"ip":"192.0.2.4"}]}')
            cfg = config.load_config()
            self.assertFalse(cfg['paused'])
            self.assertEqual(cfg['cameras'][0]['url'], 'rtsp://192.0.2.4:554/h264')
            config.save_config({'paused': True, 'cameras': []})
            self.assertEqual(config.load_config(), {'paused': True, 'cameras': []})

    def test_invalid_inputs_and_last_selection(self):
        good = {'paused': True, 'cameras': []}
        with TemporaryDirectory() as tmp, patch.object(config, 'CONFIG_FILE', tmp + '/settings'), \
                patch.object(viewer, '_last_config', good):
            for bad in ([], {}, {'cameras': None}, {'paused': 'false', 'cameras': []},
                        {'cameras': [{'ip': 'invalid'}]},
                        {'cameras': [{'ip': '192.0.2.1', 'url': 'file:///tmp/video'}]}):
                Path(config.CONFIG_FILE).write_text(json.dumps(bad))
                with self.assertRaises(ValueError):
                    config.load_config()
                self.assertEqual(viewer.rec_config(), good)
                self.assertIn('error', viewer.update_config({'paused': False}))
                self.assertEqual(json.loads(Path(config.CONFIG_FILE).read_text()), bad)

    def test_viewer_heartbeat_expires_independently_of_worker_state(self):
        with TemporaryDirectory() as tmp, patch.object(config, 'STATUS_FILE', tmp + '/status'), \
                patch.object(viewer.time, 'time', return_value=1000):
            path = Path(config.STATUS_FILE)
            self.assertFalse(viewer.rec_status()['running'])
            for stamp, running in [(999, True), (985, False), (1200, False), (float('nan'), False)]:
                path.write_text(json.dumps({'updated': stamp, 'cameras': {'192.0.2.1': {'state': 'recording'}}}))
                result = viewer.rec_status()
                self.assertIs(result['running'], running)
                json.dumps(result, allow_nan=False)

    def test_status_and_config_recovery_log_once(self):
        stop = threading.Event()
        good = {'paused': True, 'cameras': []}
        args = SimpleNamespace(ips=[], start='00:00', end='00:00')
        with patch.object(recorder, 'stop_event', stop), \
                patch.object(recorder, 'load_config', side_effect=[good, ValueError(), ValueError(), good]), \
                patch.object(recorder, 'write_json', side_effect=[OSError(), OSError(), None, None]) as writes, \
                patch.object(recorder.time, 'sleep', side_effect=lambda _: stop.set() if writes.call_count == 3 else None), \
                patch.object(recorder, 'log') as log:
            recorder.supervise(args)
        self.assertTrue(all(args[1]['paused'] for args, _ in writes.call_args_list))
        messages = [args[1] for args, _ in log.call_args_list]
        for prefix in ('沿用上一份', '錄影設定讀取已恢復', '狀態檔寫入失敗', '狀態檔寫入已恢復'):
            self.assertEqual(sum(s.startswith(prefix) for s in messages), 1)

    def test_second_recorder_exits_before_start(self):
        with TemporaryDirectory() as tmp, open(os.path.join(tmp, '.record_cams.lock'), 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            p = subprocess.run([sys.executable, '-B', recorder.__file__, '--out', tmp],
                               stderr=subprocess.PIPE, timeout=3)
            self.assertNotEqual(p.returncode, 0)
            self.assertIn('已有錄影程序', p.stderr.decode())

    def test_discovery_json_only(self):
        output = io.StringIO()
        result = {'networks': [], 'cameras': [], 'seconds': 0}
        with patch.object(discover, 'scan', return_value=result) as scan, contextlib.redirect_stdout(output):
            discover.main(['--json', '192.0.2.0/24'])
        self.assertEqual(json.loads(output.getvalue()), result)
        scan.assert_called_once_with(['192.0.2.0/24'])
