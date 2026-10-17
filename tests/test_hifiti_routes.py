"""Isolated Flask blueprint tests: no app side effects, secrets or network."""
import json
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from flask import Flask
from test_hifiti_api import SYNTHETIC_AUTHORIZED_THREAD

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import hifiti_api as api

FIXTURES = ROOT / 'tests' / 'fixtures' / 'hifiti'
IDENTITY = b'<script>var uid = 42; var gid = 1;</script><header id="header"><a href="user-42.htm">Alice</a><a href="user-logout.htm">Logout</a></header>'


class HifitiRoutesTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.register_blueprint(api.blueprint)
        self.app.config['TESTING'] = True
        with api._lock:
            api._owners.clear()
            api._login_attempts.clear()
        self.client = self.app.test_client()
        self.csrf = self.client.get('/api/hifiti/login/status').json['csrf']

    def login(self, client=None, csrf=None, **kwargs):
        return (client or self.client).post('/api/hifiti/login',
            headers={'X-CSRF-Token': csrf or self.csrf},
            json={'username': 'local_user', 'password': 'SECRET_PASSWORD'}, **kwargs)

    def fake_login(self, code=0, identity=IDENTITY):
        return mock.patch.object(api, '_wire', side_effect=[b'<form></form>', json.dumps({'code': code}).encode(), identity])

    def test_synthetic_login_cookie_thread_revealed_resources_and_auth_snapshot(self):
        # Synthetic login/server responses only; _wire is fully mocked, no network.
        calls = []
        def wire(method, target, session, **kwargs):
            calls.append((method, target))
            probe = api.Request(api.ORIGIN + target)
            session.cookies.add_cookie_header(probe)
            cookie = probe.get_header('Cookie', '')
            if method == 'POST':
                session.cookies.extract_cookies(api._CookieResponse([
                    ('Set-Cookie', 'bbs_token=SYNTHETIC_COOKIE_ONLY; Path=/; Secure; HttpOnly')]), probe)
                return b'{"code":0}'
            if target == '/':
                self.assertEqual('bbs_token=SYNTHETIC_COOKIE_ONLY', cookie)
                return IDENTITY
            if target == '/thread-1.htm':
                self.assertEqual('bbs_token=SYNTHETIC_COOKIE_ONLY', cookie)
                return SYNTHETIC_AUTHORIZED_THREAD
            return b'<form></form>'
        with mock.patch.object(api, '_wire', side_effect=wire), mock.patch.object(api.socket, 'getaddrinfo') as dns:
            self.assertTrue(self.login().json['set'])
            response = self.client.get('/api/hifiti/thread?id=1')
        dns.assert_not_called()
        self.assertEqual(200, response.status_code)
        self.assertEqual({'set': True, 'nickname': 'Alice', 'expired': False}, response.json['auth'])
        self.assertEqual(['ab12', 'cd34', ''], [item['code'] for item in response.json['resources']])
        self.assertEqual(3, len(response.json['links']))
        self.assertEqual('', response.json['notice'])
        self.assertNotIn('SYNTHETIC_COOKIE_ONLY', response.get_data(as_text=True))
        self.assertEqual([('GET', '/user-login.htm'), ('POST', '/user-login.htm'),
                          ('GET', '/'), ('GET', '/thread-1.htm')], calls)

    def test_missing_uid_is_unknown_and_explicit_uid_zero_expires_thread_auth(self):
        with self.fake_login():
            self.login()
        unknown = SYNTHETIC_AUTHORIZED_THREAD.replace(b'<script>var uid = 42;</script>', b'')
        with mock.patch.object(api, '_wire', return_value=unknown):
            data = self.client.get('/api/hifiti/thread?id=1').json
        self.assertEqual({'set': True, 'nickname': 'Alice', 'expired': False}, data['auth'])
        expired = SYNTHETIC_AUTHORIZED_THREAD.replace(b'uid = 42', b'uid = 0')
        with mock.patch.object(api, '_wire', return_value=expired):
            data = self.client.get('/api/hifiti/thread?id=1').json
        self.assertEqual({'set': False, 'nickname': '', 'expired': True}, data['auth'])
        self.assertFalse(self.client.get('/api/hifiti/login/status').json['set'])
        with api._lock:
            self.assertTrue(all(len(owner.session.cookies) == 0 for owner in api._owners.values()))
        with mock.patch.object(api, '_wire', return_value=expired):
            self.assertFalse(self.client.get('/api/hifiti/thread?id=1').json['auth']['expired'])

    def test_logout_or_expiry_during_parse_cancels_stale_account_content(self):
        for invalidate in ('logout', 'expiry'):
            with self.subTest(invalidate=invalidate):
                self.setUp()
                with self.fake_login():
                    self.login()
                entered, release = threading.Event(), threading.Event()
                outputs = []
                parse = api.parse_thread
                def delayed_parse(raw, tid):
                    data = parse(raw, tid)
                    entered.set()
                    if not release.wait(3):
                        raise RuntimeError('test timeout')
                    return data
                cookie = self.client.get_cookie(api.COOKIE, path=api.PREFIX).value
                def run():
                    client = self.app.test_client()
                    client.set_cookie(api.COOKIE, cookie, path=api.PREFIX)
                    outputs.append(client.get('/api/hifiti/thread?id=1'))
                def wire(method, target, session, **kwargs):
                    return ((FIXTURES / 'search.html').read_bytes() if target.startswith('/search')
                            else SYNTHETIC_AUTHORIZED_THREAD)
                with mock.patch.object(api, '_wire', side_effect=wire), mock.patch.object(api, 'parse_thread', side_effect=delayed_parse):
                    worker = threading.Thread(target=run)
                    worker.start()
                    try:
                        self.assertTrue(entered.wait(3))
                        if invalidate == 'logout':
                            self.client.post('/api/hifiti/logout', headers={'X-CSRF-Token': self.csrf})
                        else:
                            self.assertTrue(self.client.get('/api/hifiti/search?kw=x').json['auth']['expired'])
                    finally:
                        release.set()
                        worker.join(3)
                self.assertFalse(worker.is_alive())
                self.assertEqual('CANCELLED', outputs[0].json['code'])
                self.assertNotIn('resources', outputs[0].json)
                self.assertNotIn('GuiFixture', outputs[0].get_data(as_text=True))

    def test_status_memory_only_owner_cookie_and_private_headers(self):
        client = self.app.test_client()
        response = client.get('/api/hifiti/login/status')
        self.assertEqual({'ok': True, 'set': False, 'nickname': '', 'csrf': response.json['csrf'], 'persistent': False}, response.json)
        cookie = response.headers['Set-Cookie']
        self.assertIn('HttpOnly', cookie)
        self.assertIn('SameSite=Strict', cookie)
        self.assertIn('Path=/api/hifiti', cookie)
        self.assertNotIn('Expires=', cookie)
        self.assertEqual('no-store, private', response.headers['Cache-Control'])
        self.assertEqual('nosniff', response.headers['X-Content-Type-Options'])
        with mock.patch.object(api, '_wire') as wire:
            self.client.get('/api/hifiti/login/status')
        wire.assert_not_called()

    def test_login_preflight_exact_md5_transaction_and_status(self):
        with self.fake_login() as wire:
            response = self.login()
        self.assertEqual(200, response.status_code)
        self.assertEqual((True, 'Alice', False), (response.json['set'], response.json['nickname'], response.json['persistent']))
        self.assertEqual([('GET', '/user-login.htm'), ('POST', '/user-login.htm'), ('GET', '/')],
                         [call.args[:2] for call in wire.call_args_list])
        self.assertEqual({'email': 'local_user', 'password': api._password_md5('SECRET_PASSWORD')}, wire.call_args_list[1].kwargs['form'])
        self.assertNotIn('local_user', repr(api._owners))
        self.assertNotIn('SECRET_PASSWORD', str(response.json) + repr(api._owners))
        self.assertTrue(self.client.get('/api/hifiti/login/status').json['set'])

    def test_cookie_clients_never_share_authenticated_account(self):
        other = self.app.test_client()
        other_csrf = other.get('/api/hifiti/login/status').json['csrf']
        with self.fake_login():
            self.assertTrue(self.login().json['set'])
        self.assertFalse(other.get('/api/hifiti/login/status').json['set'])
        self.assertNotEqual(self.csrf, other_csrf)
        with api._lock:
            sessions = [owner.session for owner in api._owners.values()]
        self.assertIsNot(sessions[0], sessions[1])
        self.assertIsNot(sessions[0].cookies, sessions[1].cookies)
        response = other.post('/api/hifiti/logout', headers={'X-CSRF-Token': self.csrf})
        self.assertEqual(403, response.status_code)
        self.assertTrue(self.client.get('/api/hifiti/login/status').json['set'])

    def test_invalid_unknown_auth_does_not_mark_success_or_echo_message(self):
        for code in (1, -1, False, None, [], {}, 'unknown'):
            with self.subTest(code=code):
                self.setUp()
                with self.fake_login(code):
                    response = self.login()
                self.assertEqual('LOGIN_FAILED', response.json['code'])
                self.assertFalse(self.client.get('/api/hifiti/login/status').json['set'])
        self.setUp()
        with self.fake_login(identity=(FIXTURES / 'search.html').read_bytes()):
            response = self.login()
        self.assertEqual('LOGIN_UNVERIFIED', response.json['code'])
        self.assertFalse(self.client.get('/api/hifiti/login/status').json['set'])
        self.setUp()
        with self.fake_login(code='0'):
            self.assertTrue(self.login().json['set'])

    def test_secret_exception_and_unknown_html_redacted(self):
        for failure in (RuntimeError('SECRET_PASSWORD username cookie'), api.ApiError('CHALLENGE', 403)):
            self.setUp()
            with mock.patch.object(api, '_wire', side_effect=failure):
                response = self.login()
            self.assertFalse(response.json['ok'])
            self.assertNotIn('SECRET_PASSWORD', response.get_data(as_text=True))
        self.setUp()
        with mock.patch.object(api, '_wire', side_effect=[b'<form/>', b'<html>Checking your browser SECRET_PASSWORD</html>']):
            response = self.login()
        self.assertEqual('CHALLENGE', response.json['code'])

    def test_login_schema_and_body_bound_before_network(self):
        payloads = [None, [], {}, {'username': 'x', 'password': 'p', 'save': True},
                    {'username': '', 'password': 'p'}, {'username': 2, 'password': 'p'},
                    {'username': 'x', 'password': False}, {'username': 'x', 'password': 'p' * 257},
                    {'username': 'x\n', 'password': 'p'}, {'username': 'x', 'password': '\0'}]
        with mock.patch.object(api, '_wire') as wire:
            for payload in payloads:
                response = self.client.post('/api/hifiti/login', data=json.dumps(payload),
                    content_type='application/json', headers={'X-CSRF-Token': self.csrf})
                self.assertEqual(400, response.status_code)
            for raw, content_type in [('x' * (api.MAX_LOCAL_BODY + 1), 'application/json'), ('bad', 'application/json'), ('a=b', 'application/x-www-form-urlencoded')]:
                self.assertEqual(400, self.client.post('/api/hifiti/login', data=raw, content_type=content_type,
                                                     headers={'X-CSRF-Token': self.csrf}).status_code)
        wire.assert_not_called()

    def test_local_origin_csrf_and_peer_guards_before_network(self):
        with mock.patch.object(api, '_wire') as wire:
            for path in ('/api/hifiti/login/status', '/api/hifiti/search?kw=x', '/api/hifiti/thread?id=1'):
                for kwargs in ({'base_url': 'http://evil.example'}, {'headers': {'Origin': 'https://evil.example'}},
                        {'headers': {'Sec-Fetch-Site': 'cross-site'}}, {'environ_overrides': {'REMOTE_ADDR': '192.168.1.2'}}):
                    self.assertEqual(403, self.client.get(path, **kwargs).status_code)
            for headers in ({}, {'X-CSRF-Token': 'wrong'}, {'X-CSRF-Token': self.csrf, 'Origin': 'null'}):
                self.assertEqual(403, self.client.post('/api/hifiti/login', json={'username': 'x', 'password': 'y'}, headers=headers).status_code)
                self.assertEqual(403, self.client.post('/api/hifiti/logout', headers=headers).status_code)
            unknown = self.app.test_client()
            self.assertEqual(403, unknown.post('/api/hifiti/logout', headers={'X-CSRF-Token': self.csrf}).status_code)
        wire.assert_not_called()

    def test_anonymous_search_routes_preserve_keyword_and_site_options(self):
        source = (FIXTURES / 'search.html').read_bytes()
        for query, target in [({'kw': ' 周深 泪桥-A '}, '/search.htm?keyword=%20%E5%91%A8%E6%B7%B1%20%E6%B3%AA%E6%A1%A5-A%20'),
                ({'kw': 'A-B', 'scope': 'post', 'exact': '1', 'page': '2009'}, '/search-A_2DB-0-1-2009.htm'),
                ({'kw': 'A-B', 'scope': 'title', 'exact': '1'}, '/search-A_2DB-1-1.htm')]:
            with mock.patch.object(api, '_wire', return_value=source) as wire:
                response = self.client.get('/api/hifiti/search', query_string=query)
            self.assertEqual(200, response.status_code)
            self.assertEqual(query['kw'], response.json['keyword'])
            self.assertEqual(target, wire.call_args.args[1])
            self.assertEqual(1, wire.call_count)

    def test_invalid_search_thread_inputs_are_bounded(self):
        with mock.patch.object(api, '_wire') as wire:
            for query in ({'kw': ''}, {'kw': ' '}, {'kw': 'x' * 121}, {'kw': 'x\n'},
                          {'kw': 'x', 'scope': 'all'}, {'kw': 'x', 'exact': 'true'},
                          {'kw': 'x', 'page': '10001'}, {'kw': 'x', 'page': '0'}, {'kw': 'x', 'page': '１'}):
                self.assertEqual(400, self.client.get('/api/hifiti/search', query_string=query).status_code)
            for tid in ('', '0', '-1', '../x', '１２', '1?buy=1', '9' * 13):
                self.assertEqual(400, self.client.get('/api/hifiti/thread', query_string={'id': tid}).status_code)
        wire.assert_not_called()

    def test_thread_only_fetches_requested_official_page(self):
        with mock.patch.object(api, '_wire', return_value=(FIXTURES / 'thread-104813.html').read_bytes()) as wire:
            response = self.client.get('/api/hifiti/thread?id=104813')
        self.assertEqual(200, response.status_code)
        self.assertEqual([], response.json['links'])
        self.assertEqual(('GET', '/thread-104813.htm'), wire.call_args.args[:2])
        self.assertEqual(1, wire.call_count)

    def test_logout_is_local_no_network_and_session_expiry_clears(self):
        with self.fake_login():
            self.login()
        with mock.patch.object(api, '_wire') as wire:
            response = self.client.post('/api/hifiti/logout', headers={'X-CSRF-Token': self.csrf})
        self.assertFalse(response.json['set'])
        wire.assert_not_called()
        with api._lock:
            for owner in api._owners.values():
                owner.touched = time.monotonic() - api.OWNER_TTL - 1
        self.assertEqual(403, self.client.post('/api/hifiti/logout', headers={'X-CSRF-Token': self.csrf}).status_code)
        status = self.client.get('/api/hifiti/login/status').json
        self.assertFalse(status['set'])
        self.assertNotEqual(self.csrf, status['csrf'])

    def test_late_login_cannot_restore_after_logout(self):
        entered, release = threading.Event(), threading.Event()
        outputs = []
        def wire(method, target, session, **kwargs):
            if method == 'POST':
                entered.set()
                if not release.wait(3):
                    raise RuntimeError('test timeout')
                return b'{"code":0}'
            return IDENTITY if target == '/' else b'<form/>'
        cookie = self.client.get_cookie(api.COOKIE, path=api.PREFIX).value
        def run():
            client = self.app.test_client()
            client.set_cookie(api.COOKIE, cookie, path=api.PREFIX)
            outputs.append(self.login(client=client))
        with mock.patch.object(api, '_wire', side_effect=wire):
            thread = threading.Thread(target=run)
            thread.start()
            self.assertTrue(entered.wait(3))
            self.assertFalse(self.client.post('/api/hifiti/logout', headers={'X-CSRF-Token': self.csrf}).json['set'])
            release.set()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual('CANCELLED', outputs[0].json['code'])
        self.assertFalse(self.client.get('/api/hifiti/login/status').json['set'])

    def test_upstream_anonymous_identity_invalidates_local_auth(self):
        with self.fake_login():
            self.login()
        with mock.patch.object(api, '_wire', return_value=(FIXTURES / 'search.html').read_bytes()):
            response = self.client.get('/api/hifiti/search?kw=x')
        self.assertEqual(200, response.status_code)
        self.assertEqual({'set': False, 'nickname': '', 'expired': True}, response.json['auth'])
        self.assertFalse(self.client.get('/api/hifiti/login/status').json['set'])

    def test_rate_limits_owner_global_and_two_login_slots(self):
        with mock.patch.object(api, '_wire', side_effect=api.ApiError('NETWORK_ERROR')) as wire:
            self.login()
            self.assertEqual(429, self.login().status_code)
        self.assertEqual(1, wire.call_count)
        self.setUp()
        with api._lock:
            api._login_attempts[:] = [time.monotonic()] * 20
        with mock.patch.object(api, '_wire') as wire:
            self.assertEqual('RATE_LIMITED', self.login().json['code'])
        wire.assert_not_called()
        self.setUp()
        api._LOGIN_SLOTS.acquire()
        api._LOGIN_SLOTS.acquire()
        try:
            with mock.patch.object(api, '_wire') as wire:
                self.assertEqual('BUSY', self.login().json['code'])
            wire.assert_not_called()
        finally:
            api._LOGIN_SLOTS.release()
            api._LOGIN_SLOTS.release()


if __name__ == '__main__':
    unittest.main()
