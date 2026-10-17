"""Offline official Migu login/security tests; no scans, live HTTP or credentials."""
import base64
import contextlib
import http.client
import importlib
import io
import json
import socket
import threading
import time
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from flask import Flask
from PIL import Image

import migu_login as login
import migu_api as api

SECRET = 'test-private-secret-never-public'
KEY = 'test-private-qr-session'
OWNER = 'test-owner'


def image_url(size=(300, 300), fmt='JPEG'):
    out = io.BytesIO()
    Image.new('RGB', size, 'white').save(out, format=fmt)
    return 'data:image/%s;base64,%s' % (fmt.lower(), base64.b64encode(out.getvalue()).decode())


def create_body():
    return {'status': 2000, 'result': {'qrcUrl': image_url(), 'qrc_sessionid': KEY}}


def rows(address='93.184.216.34'):
    return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', (address, 443))]


class LoginTests(unittest.TestCase):
    def setUp(self):
        self.manager = login.QRLoginManager()
        self.app = Flask(__name__)
        self.app.register_blueprint(login.blueprint)
        self.client = self.app.test_client()
        patches = [patch.object(login, 'manager', self.manager),
                   patch.object(login, '_owners', {}),
                   patch.object(login.socket, 'getaddrinfo', side_effect=AssertionError('Offline DNS only')),
                   patch.object(login, '_wire', side_effect=AssertionError('Offline HTTP only'))]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.cleanup_manager)

    def cleanup_manager(self):
        self.manager._auth = None
        self.manager.logout()

    def session(self, client=None):
        client = client or self.client
        response = client.get(login.PREFIX + '/status')
        self.assertEqual(200, response.status_code)
        return {'X-CSRF-Token': response.json['csrf']}

    def start(self):
        with patch.object(login, '_json', return_value=(create_body(), [('Set-Cookie', 'JSESSIONID=passport-cookie; Path=/; Secure')])), \
                patch.object(login.threading, 'Thread'):
            result = self.manager.start(OWNER)
        # The mocked worker does not execute its finally/release.
        self.manager._workers.release()
        return result, self.manager._tasks[result['id']]

    def run_task(self, task, responses):
        task.stop.wait = Mock(return_value=False)
        self.manager._workers.acquire()
        with patch.object(login, '_json', side_effect=responses) as call:
            self.manager._run(task)
        return call

    def success_responses(self):
        return [({'status': 2000, 'result': {'token': SECRET}}, []),
                ({'code': '000000', 'data': {'private': SECRET}}, [('pacmtoken', SECRET)]),
                ({'code': '000000'}, []),
                ({'code': '000000', 'data': {'nickName': '  名字\n<>&\u202e', 'msisdn': SECRET}}, [])]

    def test_status_contract_owner_cookie_csrf_and_no_network(self):
        result = self.client.get(login.PREFIX + '/status')
        self.assertEqual({'ok', 'set', 'nickname', 'csrf', 'persistent'}, set(result.json))
        self.assertFalse(result.json['set'])
        self.assertFalse(result.json['persistent'])
        cookie = result.headers['Set-Cookie']
        self.assertIn('HttpOnly', cookie)
        self.assertIn('SameSite=Strict', cookie)
        self.assertIn('Path=' + login.PREFIX, cookie)
        self.assertNotIn(SECRET, result.get_data(as_text=True))
        self.assertIn('no-store', result.headers['Cache-Control'])
        self.assertEqual(result.json, self.client.get(login.PREFIX + '/status').json)

    def test_post_requires_same_owner_and_csrf(self):
        for route in ('/qr/start', '/qr/cancel', '/logout'):
            self.assertEqual(403, self.client.post(login.PREFIX + route).status_code)
        headers = self.session()
        for route in ('/qr/start', '/qr/cancel', '/logout'):
            for bad in ({}, {'X-CSRF-Token': SECRET}, {'X-CSRF-Token': '中文'}):
                self.assertEqual(403, self.client.post(login.PREFIX + route, headers=bad).status_code)
        other = self.app.test_client()
        self.assertEqual(403, other.post(login.PREFIX + '/logout', headers=headers).status_code)

    def test_standalone_loopback_host_origin_and_fetch_guards(self):
        for headers in ({'Host': 'evil.invalid'}, {'Origin': 'https://evil.invalid'},
                        {'Sec-Fetch-Site': 'cross-site'}, {'Origin': 'null'}):
            self.assertEqual(403, self.client.get(login.PREFIX + '/status', headers=headers).status_code)
        self.assertEqual(403, self.client.get(login.PREFIX + '/status',
                         environ_overrides={'REMOTE_ADDR': '192.168.1.5'}).status_code)
        self.assertEqual(200, self.client.get(login.PREFIX + '/status',
                         headers={'Origin': 'http://localhost'}).status_code)

    def test_start_returns_only_public_contract_and_png(self):
        headers = self.session()
        with patch.object(login, '_json', return_value=(create_body(), [])), \
                patch.object(login.threading, 'Thread'):
            result = self.client.post(login.PREFIX + '/qr/start', headers=headers)
        self.assertEqual(200, result.status_code)
        self.assertEqual({'ok', 'id', 'qr', 'expires_in', 'status'}, set(result.json))
        self.assertEqual('waiting', result.json['status'])
        self.assertTrue(result.json['qr'].startswith('data:image/png;base64,'))
        self.assertTrue(0 < result.json['expires_in'] <= login.QR_TTL)
        self.assertNotIn(KEY, json.dumps(result.json))
        self.assertNotIn(SECRET, json.dumps(result.json))

    def test_status_and_cancel_are_owner_bound_and_status_has_no_io(self):
        headers = self.session()
        owner = self.client.get_cookie(login.COOKIE, path=login.PREFIX).value
        with patch.object(login, '_json', return_value=(create_body(), [])), \
                patch.object(login.threading, 'Thread'):
            result = self.manager.start(owner)
        ident = result['id']
        response = self.client.get(login.PREFIX + '/qr/status?id=' + ident)
        self.assertEqual({'ok', 'id', 'status', 'message'}, set(response.json))
        other = self.app.test_client()
        other_headers = self.session(other)
        self.assertEqual(404, other.get(login.PREFIX + '/qr/status?id=' + ident).status_code)
        self.assertEqual(404, other.post(login.PREFIX + '/qr/cancel', headers=other_headers,
                                        json={'id': ident}).status_code)
        response = self.client.post(login.PREFIX + '/qr/cancel', headers=headers, json={'id': ident})
        self.assertEqual('cancelled', response.json['status'])
        self.assertEqual('', self.manager._tasks[ident].key)

    def test_invalid_task_and_cancel_payload_are_sanitized(self):
        headers = self.session()
        for ident in ('', 'invalid task!', '../x', 'a' * 5000):
            self.assertEqual(400, self.client.get(login.PREFIX + '/qr/status', query_string={'id': ident}).status_code)
        for body in ([], {}, {'id': 123}):
            self.assertEqual(400, self.client.post(login.PREFIX + '/qr/cancel', headers=headers, json=body).status_code)

    def test_waiting_scanned_expired_and_poll_interval(self):
        _, task = self.start()
        call = self.run_task(task, [({'status': 4074}, []), ({'status': 4073}, []), ({'status': 4072}, [])])
        self.assertEqual('expired', task.status)
        self.assertEqual(3, call.call_count)
        self.assertTrue(all(c.args == (login.POLL_INTERVAL,) for c in task.stop.wait.call_args_list))
        self.assertGreaterEqual(login.POLL_INTERVAL, 1)
        self.assertEqual(KEY, call.call_args_list[0].kwargs['form']['qrc_sessionid'])
        self.assertEqual('passport-cookie', call.call_args_list[0].kwargs['cookie'])
        self.assertEqual('', task.key)
        self.assertEqual('', task.cookie)

    def test_success_requires_exchange_header_check_and_sanitizes_identity(self):
        _, task = self.start()
        call = self.run_task(task, self.success_responses())
        self.assertEqual('success', task.status)
        auth = self.manager.get_auth()
        self.assertEqual(SECRET, auth.token)
        self.assertEqual('名字', self.manager.account_status()['nickname'])
        self.assertEqual('2', parse_qs(urlsplit(call.call_args_list[1].args[1]).query)['type'][0])
        self.assertEqual(SECRET, call.call_args_list[2].kwargs['token'])
        self.assertEqual(login.INFO_URL, call.call_args_list[3].args[1])
        self.assertNotIn(SECRET, repr(auth))
        self.assertNotIn(SECRET, repr(task))
        self.assertNotIn(SECRET, json.dumps(self.manager.account_status()))
        self.assertNotIn(SECRET, json.dumps(self.manager.task_status(OWNER, task.id)))

    def test_metadata_outage_is_optional_after_checked_auth(self):
        _, task = self.start()
        responses = self.success_responses()
        responses[-1] = login.LoginError('NETWORK')
        self.run_task(task, responses)
        self.assertEqual('success', task.status)
        self.assertEqual('', self.manager.account_status()['nickname'])

    def test_no_fake_success_on_missing_header_bad_data_or_check_failure(self):
        for modification in ('missing_header', 'empty_data', 'bad_code', 'failed_check', 'no_token'):
            with self.subTest(modification=modification):
                _, task = self.start()
                responses = self.success_responses()
                if modification == 'missing_header': responses[1] = (responses[1][0], [])
                if modification == 'empty_data': responses[1] = ({'code': '000000', 'data': {}}, [('pacmtoken', SECRET)])
                if modification == 'bad_code': responses[1] = ({'code': 'error', 'data': {'x': 1}}, [('pacmtoken', SECRET)])
                if modification == 'failed_check': responses[2] = ({'code': '850001'}, [])
                if modification == 'no_token': responses[0] = ({'status': 2000, 'result': {}}, [])
                self.run_task(task, responses)
                self.assertEqual('error', task.status)
                self.assertFalse(self.manager.get_auth())
                self.assertNotIn(SECRET, json.dumps(self.manager.task_status(OWNER, task.id)))

    def test_network_failure_and_unknown_poll_are_sanitized_terminal_errors(self):
        for response in (OSError(SECRET), login.LoginError('NETWORK'), ({'status': 999, 'message': SECRET}, [])):
            _, task = self.start()
            self.run_task(task, [response])
            self.assertEqual('error', task.status)
            self.assertNotIn(SECRET, json.dumps(self.manager.task_status(OWNER, task.id)))

    def test_local_ttl_expires_without_network(self):
        _, task = self.start()
        task.expires = time.monotonic() - 1
        self.assertEqual('expired', self.manager.task_status(OWNER, task.id)['status'])
        self.assertEqual('', task.key)
        self.assertTrue(task.stop.is_set())

    def test_cancel_logout_and_switch_prevent_late_success(self):
        for action in ('cancel', 'logout', 'switch'):
            with self.subTest(action=action):
                _, task = self.start()
                task.stop.wait = Mock(return_value=False)
                self.manager._workers.acquire()
                def exchange(*args):
                    if action == 'cancel': self.manager.cancel(OWNER, task.id)
                    elif action == 'logout': self.manager.logout()
                    else: self.manager._invalidate()
                    return login._Auth(SECRET, task.generation, task.device), 'late'
                with patch.object(login, '_json', return_value=({'status': 2000, 'result': {'token': SECRET}}, [])), \
                        patch.object(self.manager, '_exchange', side_effect=exchange):
                    self.manager._run(task)
                self.assertFalse(self.manager.get_auth())
                self.assertEqual('cancelled', task.status)

    def test_real_worker_logout_during_exchange_cannot_install_auth(self):
        entered, release, done = threading.Event(), threading.Event(), threading.Event()
        auth = login._Auth(SECRET, 1, str(login.uuid.uuid4()))
        def exchange(task, token):
            entered.set()
            if not release.wait(3):
                raise AssertionError('Test release deadline')
            done.set()
            return auth, 'late'
        def upstream(method, url, **kwargs):
            if url == login.CREATE_URL:
                return create_body(), []
            return {'status': 2000, 'result': {'token': SECRET}}, []
        with patch.object(login, '_json', side_effect=upstream), \
                patch.object(self.manager, '_exchange', side_effect=exchange):
            result = self.manager.start(OWNER)
            self.assertTrue(entered.wait(3))
            self.manager.logout()
            release.set()
            self.assertTrue(done.wait(3))
            deadline = time.monotonic() + 3
            # Wait on the actual bounded worker slot, not an arbitrary sleep.
            self.assertTrue(self.manager._workers.acquire(timeout=max(0, deadline - time.monotonic())))
            self.assertTrue(self.manager._workers.acquire(timeout=max(0, deadline - time.monotonic())))
            self.manager._workers.release()
            self.manager._workers.release()
        self.assertFalse(self.manager.get_auth())
        self.assertEqual('cancelled', self.manager.task_status(OWNER, result['id'])['status'])

    def test_check_rotated_token_is_used_only_after_official_check(self):
        _, task = self.start()
        responses = self.success_responses()
        responses[2] = ({'code': '000000'}, [('pacmtoken', 'checked-rotated-token')])
        call = self.run_task(task, responses)
        self.assertEqual('checked-rotated-token', self.manager.get_auth().token)
        self.assertEqual('checked-rotated-token', call.call_args_list[3].kwargs['token'])

    def test_cancel_during_creation_cannot_start_worker(self):
        def create(*args, **kwargs):
            self.manager.logout()
            return create_body(), []
        with patch.object(login, '_json', side_effect=create), patch.object(login.threading, 'Thread') as worker:
            with self.assertRaises(login.LoginError) as caught:
                self.manager.start(OWNER)
            self.assertEqual('CANCELLED', caught.exception.code)
            worker.assert_not_called()
        self.assertFalse(self.manager.get_auth())

    def test_snapshot_matching_does_not_clear_new_account(self):
        old = login._Auth(SECRET, 1, str(login.uuid.uuid4()))
        new = login._Auth('new-secret', 2, str(login.uuid.uuid4()))
        self.manager._auth = new
        self.assertFalse(self.manager.clear_matching(old))
        self.assertIs(new, self.manager.get_auth())
        self.assertTrue(self.manager.clear_matching(new))
        self.assertFalse(self.manager.get_auth())

    def test_logout_clears_before_network_and_offline_still_succeeds(self):
        self.manager._auth = login._Auth(SECRET, 1, str(login.uuid.uuid4()))
        def clear(*args, **kwargs):
            self.assertFalse(self.manager.get_auth())
            self.assertEqual(login.LOGOUT_URL, args[1])
            raise login.LoginError('NETWORK')
        with patch.object(login, '_json', side_effect=clear):
            self.assertEqual({'ok': True, 'set': False, 'persistent': False}, self.manager.logout())

    def test_bounded_tasks_workers_and_owner_sessions(self):
        for _ in range(login.MAX_TASKS + 3): self.start()
        self.assertEqual(login.MAX_TASKS, len(self.manager._tasks))
        with patch.object(self.manager, '_workers', threading.BoundedSemaphore(0)):
            with self.assertRaises(login.LoginError) as caught: self.manager.start(OWNER)
            self.assertEqual('BUSY', caught.exception.code)
        with patch.object(login, 'MAX_OWNERS', 1):
            self.session()
            self.assertEqual(503, self.app.test_client().get(login.PREFIX + '/status').status_code)

    def test_private_auth_only_exact_download_transaction_and_expiry_matching(self):
        auth = login._Auth(SECRET, 1, str(login.uuid.uuid4()))
        self.manager._auth = auth
        with patch.object(login, '_wire', return_value=(b'{"code":"000000"}', [])) as wire:
            login.authenticated_read(login.DOWNLOAD_URL, {'songId': '1'}, auth)
            self.assertEqual(SECRET, wire.call_args.kwargs['token'])
            for url in (api.SEARCH_URL, api.RESOURCE_URL, login.DOWNLOAD_URL + '?x=1', 'https://evil.invalid/'):
                with self.assertRaises(login.LoginError): login.authenticated_read(url, {}, auth)
            self.assertEqual(1, wire.call_count)
        with patch.object(login, '_wire', return_value=(b'{"code":"850001"}', [])):
            with self.assertRaises(login.LoginError): login.authenticated_read(login.DOWNLOAD_URL, {}, auth)
        self.assertFalse(self.manager.get_auth())

    def test_membership_and_copyright_do_not_clear_auth(self):
        auth = login._Auth(SECRET, 1, str(login.uuid.uuid4()))
        self.manager._auth = auth
        for code in ('200010', '440000', '200000'):
            with patch.object(login, '_wire', return_value=(json.dumps({'code': code}).encode(), [])):
                login.authenticated_read(login.DOWNLOAD_URL, {}, auth)
            self.assertIs(auth, self.manager.get_auth())

    def test_api_auth_is_not_used_for_search_resource_lyrics_or_media(self):
        auth = login._Auth(SECRET, 1, str(login.uuid.uuid4()))
        with patch.object(login, 'authenticated_read', return_value=b'{"code":"000000"}') as read:
            self.assertEqual(b'{"code":"000000"}', api._read(api.DOWNLOAD_URL, {}, token=auth))
            for url in (api.SEARCH_URL, api.RESOURCE_URL, 'https://d.musicapp.migu.cn/song.lrc'):
                with self.assertRaises(api.ApiError): api._read(url, {}, token=auth)
            self.assertEqual(1, read.call_count)
        with patch.object(api, 'resource', return_value={'copyrightId': '1', 'contentId': '2', 'songId': '3'}), \
                patch.object(login, 'get_auth', return_value=auth), \
                patch.object(api, '_json', return_value={'code': '200010'}) as transaction:
            with self.assertRaises(api.ApiError): api.download('1')
            transaction.assert_called_once_with(api.DOWNLOAD_URL, {'songId': '3', 'formatType': 'PQ'}, token=auth)

    def test_image_validation_reencodes_and_rejects_external_or_unsafe_images(self):
        self.assertTrue(login._image(image_url()).startswith('data:image/png;base64,'))
        for value in ('https://passport.migu.cn/qr', 'http://127.0.0.1/qr',
                      'data:image/svg+xml;base64,PHN2Zz4=', 'data:image/jpeg;base64,@@@@',
                      image_url((1, 1)), image_url((1025, 64)),
                      'data:image/png;base64,' + base64.b64encode(b'not-an-image' * 10).decode()):
            with self.assertRaises(login.LoginError): login._image(value)

    def test_missing_pillow_returns_explicit_sanitized_dependency_error(self):
        import sys
        value = image_url()
        with patch.dict(sys.modules, {'PIL': None}):
            with self.assertRaises(login.LoginError) as caught:
                login._image(value)
        self.assertEqual('DEPENDENCY', caught.exception.code)
        self.assertEqual(503, caught.exception.status)
        self.assertIn('Pillow', str(caught.exception))

    def test_invalid_create_is_error_not_fake_qr(self):
        for body in ({'status': 500, 'message': SECRET}, {'status': 2000, 'result': {}},
                     {'status': 2000, 'result': {'qrc_sessionid': KEY, 'qrcUrl': 'https://evil.invalid'}}):
            with patch.object(login, '_json', return_value=(body, [])):
                with self.assertRaises(login.LoginError) as caught: self.manager.start(OWNER)
                self.assertNotIn(SECRET, str(caught.exception))


class WireTests(unittest.TestCase):
    def response(self, raw=b'{"status":4074}', status=200, headers=None):
        response = Mock()
        response.status = status
        mapping = headers or {}
        response.getheader.side_effect = lambda name, default=None: mapping.get(name, default)
        response.getheaders.return_value = list(mapping.items())
        response.read1.side_effect = [raw, b'']
        return response

    @contextlib.contextmanager
    def wire(self, response):
        connection = Mock()
        connection.getresponse.return_value = response
        with patch.object(login.socket, 'getaddrinfo', return_value=rows()) as dns, \
                patch.object(login, '_HTTPS', return_value=connection), \
                patch.object(login.threading, 'Timer') as timer:
            yield connection, dns, timer

    def test_no_debug_or_urllib3_token_logging_and_no_cookie_cross_host(self):
        response = self.response()
        with self.wire(response) as (connection, _, _), self.assertNoLogs('urllib3', level='DEBUG'):
            login._wire('GET', login.EXCHANGE_URL + '?token=' + SECRET, device=str(login.uuid.uuid4()))
            self.assertIn(SECRET, connection.request.call_args.args[1])
            headers = connection.request.call_args.args[3]
            self.assertNotIn('Cookie', headers)
            self.assertNotIn('pacmtoken', headers)
        with self.wire(response) as (connection, _, _):
            with self.assertRaises(login.LoginError): login._wire('GET', login.CHECK_URL, cookie=SECRET)
            connection.request.assert_not_called()

    def test_secret_header_only_official_request_and_transport_closes(self):
        with self.wire(self.response()) as (connection, _, timer):
            login._wire('GET', login.DOWNLOAD_URL + '?songId=1', token=SECRET)
            self.assertEqual(SECRET, connection.request.call_args.args[3]['pacmtoken'])
            self.assertNotIn('Cookie', connection.request.call_args.args[3])
            connection.close.assert_called_once()
            timer.return_value.cancel.assert_called_once()

    def test_dns_private_mixed_and_unallowlisted_targets_rejected(self):
        for address in ('127.0.0.1', '169.254.169.254', '224.0.0.1', '::1', '::ffff:127.0.0.1'):
            with self.wire(self.response()) as (connection, dns, _):
                dns.return_value = rows(address)
                with self.assertRaises(login.LoginError): login._wire('GET', login.CHECK_URL)
                connection.request.assert_not_called()
        for url in ('https://evil.invalid/', 'http://c.musicapp.migu.cn/mgateway/api/checkPacMtoken',
                    login.CHECK_URL + '#x', login.CHECK_URL.replace('.cn/', '.cn:443/')):
            with self.wire(self.response()) as (connection, _, _):
                with self.assertRaises(login.LoginError): login._wire('GET', url, token=SECRET)
                connection.request.assert_not_called()

    def test_redirect_bounds_length_encoding_and_network_errors_are_sanitized(self):
        for response in (self.response(status=302, headers={'Location': 'https://evil.invalid/?token=' + SECRET}),
                         self.response(headers={'Content-Length': str(login.MAX_BODY + 1)}),
                         self.response(headers={'Content-Length': '999'}),
                         self.response(headers={'Content-Encoding': 'gzip'}),
                         self.response(raw=b'x' * (login.MAX_BODY + 1))):
            with self.wire(response) as (connection, _, _):
                with self.assertRaises(login.LoginError) as caught: login._wire('GET', login.CHECK_URL, token=SECRET)
                self.assertNotIn(SECRET, str(caught.exception))
                self.assertEqual(1, connection.request.call_count)
                connection.close.assert_called_once()
        with self.wire(self.response()) as (connection, _, _):
            connection.request.side_effect = OSError(SECRET)
            with self.assertRaises(login.LoginError) as caught: login._wire('GET', login.CHECK_URL, token=SECRET)
            self.assertNotIn(SECRET, str(caught.exception))

    def test_pinned_tls_and_debug_zero_even_global_debug_enabled(self):
        sock, context = Mock(), Mock()
        context.wrap_socket.return_value = sock
        with patch.object(login.ssl, 'create_default_context', return_value=context), \
                patch.object(login.socket, 'socket', return_value=sock), \
                patch.object(http.client.HTTPConnection, 'debuglevel', 1):
            connection = login._HTTPS('passport.migu.cn', rows(), time.monotonic() + 10)
            self.assertEqual(0, connection.debuglevel)
            connection.connect()
            sock.connect.assert_called_once_with(('93.184.216.34', 443))
            context.wrap_socket.assert_called_once_with(sock, server_hostname='passport.migu.cn')
            connection.close()

    def test_passport_cookie_scope_and_duplicate_token_header_rejected(self):
        self.assertEqual('abc', login._passport_cookie([('Set-Cookie', 'JSESSIONID=abc; HttpOnly; Secure')]))
        self.assertEqual('', login._passport_cookie([('Set-Cookie', 'foreign=' + SECRET)]))
        with self.assertRaises(login.LoginError): login._header([('pacmtoken', 'one'), ('PacMtoken', 'two')], 'pacmtoken')
        for value in ('bad\r\nheader', '', None, 'x' * 8193):
            with self.assertRaises(login.LoginError): login._secret(value)

    def test_import_has_no_network_workers_or_files(self):
        # Execute an independent module namespace, without disturbing live singletons.
        spec = importlib.util.spec_from_file_location('migu_login_import_probe', login.__file__)
        module = importlib.util.module_from_spec(spec)
        import sys
        with patch.dict(sys.modules, {spec.name: module}), \
                patch.object(socket, 'getaddrinfo', side_effect=AssertionError('Import DNS')), \
                patch.object(threading.Thread, 'start', side_effect=AssertionError('Import worker')), \
                patch('builtins.open', side_effect=AssertionError('Import file IO')):
            spec.loader.exec_module(module)
        self.assertFalse(module.manager.get_auth())
        self.assertEqual({}, module.manager._tasks)


if __name__ == '__main__':
    unittest.main()
