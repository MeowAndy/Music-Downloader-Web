"""Offline browser-driven QR state and stale-generation regression tests."""
import base64
import concurrent.futures
import importlib
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import netease_api as api
import netease_login as login

IMAGE = 'data:image/png;base64,' + base64.b64encode(b'\x89PNG\r\n\x1a\nexample').decode('ascii')


def envelope(code, cookie=None):
    return {'ok': True, 'body': {'code': code}, 'cookies': cookie or []}


class LoginTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path_patch = patch.object(api.paths, 'APP_DIR', self.root)
        self.path_patch.start()
        self.addCleanup(self.path_patch.stop)
        self.addCleanup(self.temp.cleanup)
        self.manager = login.QRLoginManager()
        self.clock = 1000.0
        self.time_patch = patch.object(login.time, 'time', side_effect=lambda: self.clock)
        self.mono_patch = patch.object(login.time, 'monotonic', side_effect=lambda: self.clock)
        self.time_patch.start()
        self.mono_patch.start()
        self.addCleanup(self.time_patch.stop)
        self.addCleanup(self.mono_patch.stop)

    def start(self):
        with patch.object(api, 'bridge_call', return_value={'body': {'key': 'private-key', 'image': IMAGE}}) as bridge:
            task, data = self.manager.start()
        self.assertEqual(('qr_start', {}), bridge.call_args.args)
        self.assertEqual({'cookie': ''}, bridge.call_args.kwargs)
        return task, data

    def test_import_is_credential_lazy(self):
        with patch.object(Path, 'read_text', side_effect=AssertionError('must not read credentials at import')):
            importlib.reload(login)
        self.assertFalse((self.root / 'netease-auth.json').exists())

    def test_start_only_exposes_image_and_random_id(self):
        task, data = self.start()
        self.assertEqual(IMAGE, data['image'])
        self.assertEqual('waiting', data['status'])
        self.assertEqual(180, data['remaining'])
        self.assertEqual(1180, data['expires_at'])
        self.assertNotIn('private-key', json.dumps(data))
        self.assertNotIn('private-key', repr(task))
        self.assertIsNone(self.manager.status('missing'))

    def test_wait_scan_expire_and_throttle(self):
        task, _ = self.start()
        with patch.object(api, 'bridge_call', side_effect=[envelope(801), envelope(802), envelope(800)]) as bridge:
            self.assertEqual('waiting', self.manager.status(task.task_id)['status'])
            self.clock += 1.49
            self.assertEqual('waiting', self.manager.status(task.task_id)['status'])
            self.assertEqual(1, bridge.call_count)
            self.clock += .01
            self.assertEqual('scanned', self.manager.status(task.task_id)['status'])
            self.clock += 1.5
            self.assertEqual('expired', self.manager.status(task.task_id)['status'])
            self.assertEqual(3, bridge.call_count)
            self.manager.status(task.task_id)
            self.assertEqual(3, bridge.call_count)
        self.assertEqual('', task.key)

    def test_local_expiration_prevents_poll(self):
        task, _ = self.start()
        self.clock += 180
        with patch.object(api, 'bridge_call') as bridge:
            self.assertEqual('expired', self.manager.status(task.task_id)['status'])
            bridge.assert_not_called()

    def test_success_setcookie_optional_metadata_failure_still_persists(self):
        task, _ = self.start()
        with patch.object(api, 'bridge_call', side_effect=[envelope(803, ['MUSIC_U=secret; HttpOnly; Path=/', '__csrf=csrf; Secure']), api.ApiError('NETWORK_ERROR')]):
            data = self.manager.status(task.task_id)
        self.assertEqual('success', data['status'])
        self.assertTrue(api.credential_status()['set'])
        self.assertIn('MUSIC_U=secret', api.get_cookie())
        self.assertNotIn('secret', json.dumps(data))
        self.assertNotIn('csrf', json.dumps(data))
        self.assertEqual({}, data['account'])
        with patch.object(api, 'bridge_call') as bridge:
            self.manager.status(task.task_id)
            bridge.assert_not_called()

    def test_success_body_cookie_with_minimal_account(self):
        task, _ = self.start()
        result = envelope(803)
        result['body']['cookie'] = 'MUSIC_U=body-secret; Path=/; HttpOnly'
        metadata = {'body': {'data': {'code': 200, 'profile': {'userId': 42, 'nickname': 'Name', 'private': 'hide'}}}}
        with patch.object(api, 'bridge_call', side_effect=[result, metadata]) as bridge:
            data = self.manager.status(task.task_id)
        self.assertEqual({'id': '42', 'nickname': 'Name'}, data['account'])
        self.assertEqual(('login_status', {}), bridge.call_args.args)
        self.assertEqual({'cookie': 'MUSIC_U=body-secret'}, bridge.call_args.kwargs)
        self.assertEqual(data['account'], api.credential_status()['account'])

    def test_success_without_music_u_is_not_login(self):
        task, _ = self.start()
        with patch.object(api, 'bridge_call', return_value=envelope(803, ['__csrf=not-login; HttpOnly'])):
            data = self.manager.status(task.task_id)
        self.assertEqual('failed', data['status'])
        self.assertFalse(api.credential_status()['set'])

    def test_transient_poll_failure_retry_is_throttled(self):
        task, _ = self.start()
        with patch.object(api, 'bridge_call', side_effect=[api.ApiError('NETWORK_ERROR'), envelope(802)]) as bridge:
            data = self.manager.status(task.task_id)
            self.assertEqual('waiting', data['status'])
            self.assertTrue(data['error'])
            self.manager.status(task.task_id)
            self.assertEqual(1, bridge.call_count)
            self.clock += 1.5
            self.assertEqual('scanned', self.manager.status(task.task_id)['status'])

    def test_refresh_invalidates_previous_and_tasks_are_bounded(self):
        task, _ = self.start()
        for _ in range(20):
            current, _ = self.start()
        self.assertEqual('cancelled', task.status)
        self.assertLessEqual(len(self.manager._tasks), login.MAX_TASKS)
        self.assertEqual('waiting', current.status)
        self.assertTrue(self.manager.cancel(current.task_id))
        self.assertFalse(self.manager.cancel(current.task_id))
        self.assertIsNone(self.manager.status(current.task_id))

    def test_concurrent_poll_is_serialized_without_duplicate_request(self):
        task, _ = self.start()
        entered = threading.Event()
        release = threading.Event()
        def bridge(*args, **kwargs):
            entered.set()
            release.wait(5)
            return envelope(802)
        with patch.object(api, 'bridge_call', side_effect=bridge) as mock, concurrent.futures.ThreadPoolExecutor(4) as pool:
            first = pool.submit(self.manager.status, task.task_id)
            try:
                self.assertTrue(entered.wait(5))
                others = [pool.submit(self.manager.status, task.task_id) for _ in range(3)]
                for future in others:
                    self.assertEqual('waiting', future.result(2)['status'])
            finally:
                release.set()
            self.assertEqual('scanned', first.result(5)['status'])
            self.assertEqual(1, mock.call_count)

    def test_cancel_logout_refresh_during_poll_prevent_stale_persistence(self):
        for action in ('cancel', 'logout', 'refresh'):
            with self.subTest(action=action):
                self.manager = login.QRLoginManager()
                api.clear_credentials()
                task, _ = self.start()
                entered = threading.Event()
                release = threading.Event()
                def bridge(op, args, **kwargs):
                    if op == 'qr_start':
                        return {'body': {'key': 'new-private-key', 'image': IMAGE}}
                    if op == 'qr_check':
                        entered.set()
                        release.wait(5)
                        return envelope(803, ['MUSIC_U=stale-secret; Path=/'])
                    return {'body': {'code': 200}}
                with patch.object(api, 'bridge_call', side_effect=bridge), concurrent.futures.ThreadPoolExecutor(1) as pool:
                    future = pool.submit(self.manager.status, task.task_id)
                    try:
                        self.assertTrue(entered.wait(5))
                        if action == 'cancel':
                            self.manager.cancel(task.task_id)
                        elif action == 'logout':
                            self.manager.logout()
                        else:
                            self.manager.start()
                    finally:
                        release.set()
                    future.result(5)
                self.assertFalse(api.credential_status()['set'])
                self.assertEqual('cancelled', task.status)

    def test_logout_during_optional_metadata_prevents_persistence(self):
        task, _ = self.start()
        entered = threading.Event()
        release = threading.Event()
        def bridge(op, args, **kwargs):
            if op == 'qr_check':
                return envelope(803, ['MUSIC_U=stale-secret'])
            entered.set()
            release.wait(5)
            return {'body': {'data': {'code': 200, 'profile': {'userId': 1}}}}
        with patch.object(api, 'bridge_call', side_effect=bridge), concurrent.futures.ThreadPoolExecutor(1) as pool:
            future = pool.submit(self.manager.status, task.task_id)
            try:
                self.assertTrue(entered.wait(5))
                self.manager.logout()
            finally:
                release.set()
            future.result(5)
        self.assertFalse(api.credential_status()['set'])

    def test_logout_during_qr_creation_prevents_old_start(self):
        entered = threading.Event()
        release = threading.Event()
        def bridge(*args, **kwargs):
            entered.set()
            release.wait(5)
            return {'body': {'key': 'stale-key', 'image': IMAGE}}
        with patch.object(api, 'bridge_call', side_effect=bridge), concurrent.futures.ThreadPoolExecutor(1) as pool:
            future = pool.submit(self.manager.start)
            try:
                self.assertTrue(entered.wait(5))
                self.manager.logout()
            finally:
                release.set()
            with self.assertRaises(api.ApiError) as error:
                future.result(5)
            self.assertEqual('CANCELLED', error.exception.code)
        self.assertEqual({}, self.manager._tasks)

    def test_bad_qr_image_or_key_is_not_returned(self):
        for body in ({'key': '', 'image': IMAGE}, {'key': 'key', 'image': 'https://evil/qr'},
                     {'key': 'key', 'image': 'data:image/svg+xml;base64,PHN2Zz4='},
                     {'key': 'key', 'image': 'data:image/png;base64,bm90cG5n'}):
            with self.subTest(body=body), patch.object(api, 'bridge_call', return_value={'body': body}):
                with self.assertRaises(api.ApiError):
                    self.manager.start()


if __name__ == '__main__':
    unittest.main()
