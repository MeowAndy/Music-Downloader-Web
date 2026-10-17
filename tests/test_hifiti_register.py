"""Offline native signup contract and cancellation tests; never real signup."""
import json
import socket
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from flask import Flask

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import hifiti_api as api

# Synthetic equivalent of public markup; no real cookies, accounts or secrets.
FORM = b'''<form id="form" action="user-create.htm" method="post">
<input type="email" name="email" required><input type="text" name="username">
<input type="password" name="password"><input type="password" name="repeat_password">
<button type="submit">Next</button></form>
<script>postdata.password = $.md5(postdata.password);</script>'''
IDENTITY = b'<script>var uid = 42;</script><header id="header"><a href="user-42.htm">Synthetic</a><a href="user-logout.htm">Logout</a></header>'
PAYLOAD = {'email': 'synthetic@example.invalid', 'username': 'synthetic',
           'password': 'SYNTHETIC_PASSWORD', 'repeat_password': 'SYNTHETIC_PASSWORD'}


class RegisterTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.register_blueprint(api.blueprint)
        self.app.config['TESTING'] = True
        with api._lock:
            api._owners.clear()
            api._login_attempts.clear()
        self.client = self.app.test_client()
        self.csrf = self.client.get('/api/hifiti/login/status').json['csrf']
        # Fail any accidentally unmocked transport before network resolution.
        patch = mock.patch.object(api.socket, 'getaddrinfo', side_effect=AssertionError('No live network'))
        self.dns = patch.start()
        self.addCleanup(patch.stop)

    def register(self, body=None, client=None, headers=None):
        return (client or self.client).post('/api/hifiti/register',
            json=PAYLOAD if body is None else body,
            headers={'X-CSRF-Token': self.csrf} if headers is None else headers)

    def test_exact_single_transaction_hash_repeat_session_and_verified_identity(self):
        calls = []
        def wire(method, target, session, **kwargs):
            calls.append((method, target, session, dict(kwargs.get('form', {}))))
            if method == 'POST':
                self.assertEqual(1, len(session.cookies))
                return b'{"code":0,"message":"SECRET_UPSTREAM"}'
            if target == '/':
                return IDENTITY
            probe = api.Request(api.ORIGIN + target)
            session.cookies.extract_cookies(api._CookieResponse([
                ('Set-Cookie', 'bbs_sid=SYNTHETIC_SESSION; Path=/; Secure')]), probe)
            return FORM
        with mock.patch.object(api, '_wire', side_effect=wire):
            response = self.register()
        self.assertEqual(200, response.status_code)
        self.assertTrue(response.json['registered'])
        self.assertEqual((True, 'Synthetic', False), (response.json['set'], response.json['nickname'], response.json['persistent']))
        self.assertEqual([('GET', '/user-create.htm'), ('POST', '/user-create.htm'), ('GET', '/')], [c[:2] for c in calls])
        self.assertEqual(dict(PAYLOAD, password=api._password_md5(PAYLOAD['password'])), calls[1][3])
        self.assertIs(calls[0][2], calls[1][2])
        self.assertNotIn('SECRET_UPSTREAM', response.get_data(as_text=True))
        self.assertNotIn('SYNTHETIC_PASSWORD', repr(api._owners) + response.get_data(as_text=True))
        self.dns.assert_not_called()

    def test_accepted_unknown_identity_or_followup_failure_remains_accepted(self):
        for identity in (b'<script>var uid = 0;</script>', api.ApiError('NETWORK_ERROR'), api.ApiError('CHALLENGE', 403), RuntimeError('PRIVATE_COOKIE')):
            with self.subTest(identity=type(identity).__name__):
                self.setUp()
                with mock.patch.object(api, '_wire', side_effect=[FORM, b'{"code":"0"}', identity]) as wire:
                    response = self.register()
                self.assertEqual(200, response.status_code)
                self.assertTrue(response.json['registered'])
                self.assertFalse(response.json['set'])
                self.assertEqual('官网已接受注册，请按要求激活后登录', response.json['message'])
                self.assertEqual(1, sum(call.args[0] == 'POST' for call in wire.call_args_list))

    def test_strict_schema_limits_password_equality_before_network(self):
        cases = [[], {}, dict(PAYLOAD, extra=True), dict(PAYLOAD, email=False),
                 dict(PAYLOAD, email='not-email'), dict(PAYLOAD, email='x' * 255),
                 dict(PAYLOAD, username=' '), dict(PAYLOAD, username='x' * 65),
                 dict(PAYLOAD, username='x\n'), dict(PAYLOAD, password=''),
                 dict(PAYLOAD, password='x' * 129, repeat_password='x' * 129),
                 dict(PAYLOAD, repeat_password='DIFFERENT'), dict(PAYLOAD, password='\0', repeat_password='\0')]
        with mock.patch.object(api, '_wire') as wire:
            for body in cases:
                self.assertEqual(400, self.register(body).status_code)
            for raw, content_type in [('bad', 'application/json'), ('x' * 8193, 'application/json'), ('a=b', 'text/plain')]:
                response = self.client.post('/api/hifiti/register', data=raw, content_type=content_type,
                    headers={'X-CSRF-Token': self.csrf})
                self.assertEqual(400, response.status_code)
        wire.assert_not_called()

    def test_csrf_origin_peer_and_owner_required(self):
        with mock.patch.object(api, '_wire') as wire:
            for headers in ({}, {'X-CSRF-Token': 'wrong'}, {'X-CSRF-Token': self.csrf, 'Origin': 'null'},
                            {'X-CSRF-Token': self.csrf, 'Sec-Fetch-Site': 'cross-site'}):
                self.assertEqual(403, self.register(headers=headers).status_code)
            other = self.app.test_client()
            self.assertEqual(403, self.register(client=other).status_code)
            self.assertEqual(403, self.client.post('/api/hifiti/register', json=PAYLOAD,
                headers={'X-CSRF-Token': self.csrf}, environ_overrides={'REMOTE_ADDR': '192.168.1.2'}).status_code)
        wire.assert_not_called()

    def test_preflight_unknown_action_fields_hash_or_challenge_never_posts(self):
        variants = [FORM.replace(b'user-create.htm', b'https://evil.example/'),
                    FORM.replace(b'name="email"', b'name="unknown"'),
                    FORM.replace(b'</form>', b'<input type="hidden" name="csrf" value="SECRET"></form>'),
                    FORM.replace(b'$.md5', b'$.different'),
                    FORM + b'<iframe src="https://example.invalid/captcha"></iframe>',
                    b'<html>Checking your browser</html>']
        for raw in variants:
            self.setUp()
            with mock.patch.object(api, '_wire', return_value=raw) as wire:
                response = self.register()
            self.assertFalse(response.json['ok'])
            self.assertEqual(1, wire.call_count)
            self.assertEqual('GET', wire.call_args.args[0])
            self.assertNotIn('SECRET', response.get_data(as_text=True))

    def test_rejection_unknown_json_html_and_exceptions_sanitized(self):
        variants = [json.dumps({'code': code, 'message': 'SYNTHETIC_PASSWORD PRIVATE_COOKIE'}).encode()
                    for code in (False, None, [], {}, -1, 1, 'email', 'password')]
        variants += [b'<html>PRIVATE_COOKIE</html>', b'<html>Checking your browser PRIVATE_COOKIE</html>',
                     RuntimeError('SYNTHETIC_PASSWORD PRIVATE_COOKIE')]
        for raw in variants:
            self.setUp()
            with mock.patch.object(api, '_wire', side_effect=[FORM, raw]) as wire:
                response = self.register()
            self.assertFalse(response.json['ok'])
            self.assertEqual(2, wire.call_count)
            self.assertNotIn('PRIVATE_COOKIE', response.get_data(as_text=True))
            self.assertNotIn('SYNTHETIC_PASSWORD', response.get_data(as_text=True))
            self.assertFalse(self.client.get('/api/hifiti/login/status').json['set'])

    def test_existing_identity_session_preserved_on_failure_and_acceptance(self):
        for response_body in (b'{"code":1}', b'{"code":0}'):
            self.setUp()
            with api._lock:
                owner = next(iter(api._owners.values()))
                owner.nickname = 'Existing'
                original = owner.session
            sessions = []
            def wire(method, target, session, **kwargs):
                sessions.append(session)
                return response_body if method == 'POST' else IDENTITY if target == '/' else FORM
            with mock.patch.object(api, '_wire', side_effect=wire):
                response = self.register()
            self.assertIs(original, owner.session)
            self.assertTrue(all(s is not original for s in sessions))
            self.assertEqual('Existing', self.client.get('/api/hifiti/login/status').json['nickname'])
            if response_body == b'{"code":0}':
                self.assertEqual((True, 'Existing'), (response.json['set'], response.json['nickname']))

    def test_registration_shares_login_limits_slots_and_busy_state(self):
        with mock.patch.object(api, '_wire', side_effect=api.ApiError('NETWORK_ERROR')) as wire:
            self.register()
            response = self.client.post('/api/hifiti/login', json={'username': 'x', 'password': 'y'}, headers={'X-CSRF-Token': self.csrf})
            self.assertEqual('RATE_LIMITED', response.json['code'])
            self.assertEqual('RATE_LIMITED', self.register().json['code'])
        self.assertEqual(1, wire.call_count)
        for state in ('global', 'owner', 'busy', 'slots'):
            self.setUp()
            owner = next(iter(api._owners.values()))
            if state == 'global': api._login_attempts[:] = [time.monotonic()] * 20
            if state == 'owner': owner.attempts[:] = [time.monotonic() - 3] * 5
            if state == 'busy': owner.login_busy = True
            if state == 'slots':
                api._LOGIN_SLOTS.acquire()
                api._LOGIN_SLOTS.acquire()
            try:
                with mock.patch.object(api, '_wire') as wire:
                    self.assertIn(self.register().json['code'], {'RATE_LIMITED', 'BUSY'})
                wire.assert_not_called()
            finally:
                if state == 'slots':
                    api._LOGIN_SLOTS.release()
                    api._LOGIN_SLOTS.release()

    def test_logout_or_owner_eviction_cancels_preflight_or_late_response(self):
        for phase in ('preflight', 'post', 'identity'):
            for invalidate in ('logout', 'evict'):
                self.setUp()
                entered, release = threading.Event(), threading.Event()
                outputs, calls = [], []
                def wire(method, target, session, **kwargs):
                    calls.append((method, target))
                    current = 'post' if method == 'POST' else 'identity' if target == '/' else 'preflight'
                    if current == phase:
                        entered.set()
                        if not release.wait(3): raise AssertionError('test timeout')
                    return b'{"code":0}' if method == 'POST' else IDENTITY if target == '/' else FORM
                cookie = self.client.get_cookie(api.COOKIE, path=api.PREFIX).value
                def run():
                    client = self.app.test_client()
                    client.set_cookie(api.COOKIE, cookie, path=api.PREFIX)
                    outputs.append(self.register(client=client))
                with mock.patch.object(api, '_wire', side_effect=wire):
                    worker = threading.Thread(target=run)
                    worker.start()
                    try:
                        self.assertTrue(entered.wait(3))
                        self.assertEqual('BUSY', self.register().json['code'])
                        if invalidate == 'logout':
                            self.client.post('/api/hifiti/logout', headers={'X-CSRF-Token': self.csrf})
                        else:
                            with api._lock: api._owners.clear()
                    finally:
                        release.set()
                        worker.join(3)
                self.assertFalse(worker.is_alive())
                self.assertEqual('CANCELLED', outputs[0].json['code'])
                self.assertFalse(self.client.get('/api/hifiti/login/status').json['set'])
                if phase == 'preflight': self.assertEqual([('GET', '/user-create.htm')], calls)

    def test_allowlist_and_signup_referer_urlencoding_without_real_network(self):
        for method in ('GET', 'POST'):
            self.assertTrue(api._allowed_target(method, '/user-create.htm'))
        for target in ('/user-create.htm?x=1', '/user-send_code-user_create.htm', '//evil.example/', '/user-create.htm/'):
            self.assertFalse(api._allowed_target('POST', target))
        response = mock.Mock(status=200)
        response.getheader.side_effect = lambda name, default=None: default
        response.getheaders.return_value = []
        response.read1.side_effect = [b'{"code":0}', b'']
        connection = mock.Mock(sock=None)
        connection.getresponse.return_value = response
        rows = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', 443))]
        with mock.patch.object(api.socket, 'getaddrinfo', return_value=rows), mock.patch.object(api, '_HTTPS', return_value=connection):
            api._wire('POST', '/user-create.htm', api._Session(), form={'email': 'a+b@example.invalid', 'repeat_password': 'a+b&c'})
        args = connection.request.call_args.args
        self.assertEqual(('POST', '/user-create.htm'), args[:2])
        self.assertEqual(api.ORIGIN + '/user-create.htm', args[3]['Referer'])
        self.assertEqual('XMLHttpRequest', args[3]['X-Requested-With'])
        self.assertIn(b'repeat_password=a%2Bb%26c', args[2])


if __name__ == '__main__':
    unittest.main()
