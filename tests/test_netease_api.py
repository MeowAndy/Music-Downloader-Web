"""Offline tests: no real credentials, subprocesses or HTTP connections."""
import concurrent.futures
import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import netease_api as api


class FakeResponse:
    def __init__(self, payload=b'audio', status=200, headers=None, chunks=None):
        self.status_code = status
        self.headers = headers or {}
        self.payload = payload
        self.chunks = chunks
        self.closed = False

    def iter_content(self, chunk_size):
        yield from self.chunks if self.chunks is not None else [self.payload]

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.headers = {'Cookie': 'bad'}
        self.cookies = SimpleNamespace(clear=lambda: None)
        self.calls = []
        self.trust_env = True
        self.auth = 'bad'

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs, dict(self.headers)))
        return next(self.responses)


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.app_patch = patch.object(api.paths, 'APP_DIR', self.root)
        self.out_patch = patch.object(api.paths, 'OUTPUT_DIR', self.root / 'output')
        self.app_patch.start()
        self.out_patch.start()
        self.addCleanup(self.app_patch.stop)
        self.addCleanup(self.out_patch.stop)
        self.addCleanup(self.temp.cleanup)

    def assertCode(self, code, call, *args, **kwargs):
        with self.assertRaises(api.ApiError) as caught:
            call(*args, **kwargs)
        self.assertEqual(code, caught.exception.code)
        return caught.exception

    def test_lazy_credentials_and_minimal_public_status(self):
        self.assertEqual({'set': False, 'account': {}}, api.credential_status())
        api.set_credentials('MUSIC_U=secret; __csrf=token; Path=/; HttpOnly; Ignore=bad',
                            {'userId': 123, 'nickname': 'Name', 'password': 'hidden'})
        self.assertEqual({'set': True, 'account': {'id': '123', 'nickname': 'Name'}}, api.credential_status())
        self.assertEqual('MUSIC_U=secret; __csrf=token', api.get_cookie())
        self.assertNotIn('secret', json.dumps(api.credential_status()))
        self.assertEqual([self.root / 'netease-auth.json'], list(self.root.iterdir()))
        api.clear_credentials()
        self.assertFalse(api.credential_status()['set'])

    def test_cookie_parser_ignores_attributes_and_supports_body(self):
        cookie = api.parse_cookies(['MUSIC_U=secret; Path=/; HttpOnly; Secure; SameSite=None',
                                    '__csrf=token; Expires=Wed, 21 Oct 2030 07:28:00 GMT'],
                                   'NMTID=other; Path=/')
        self.assertTrue(api._has_music_u(cookie))
        self.assertNotIn('Path', cookie)
        self.assertNotIn('Expires', cookie)
        self.assertIn('NMTID=other', cookie)
        self.assertFalse(api._has_music_u(api.parse_cookies(['bad\r\nMUSIC_U=oops'])))

    def test_corrupt_or_missing_credentials_are_not_exposed(self):
        (self.root / 'netease-auth.json').write_text('{broken', encoding='utf-8')
        self.assertFalse(api.credential_status()['set'])
        (self.root / 'netease-auth.json').write_text('[]', encoding='utf-8')
        self.assertEqual('', api.get_cookie())
        self.assertCode('LOGIN_REQUIRED', api.set_credentials, '__csrf=only')

    def test_atomic_replace_failure_preserves_previous_credentials(self):
        api.set_credentials('MUSIC_U=old')
        with patch.object(api.os, 'replace', side_effect=OSError('SECRET')):
            error = self.assertCode('STORAGE_ERROR', api.set_credentials, 'MUSIC_U=new')
        self.assertNotIn('SECRET', str(error))
        self.assertIn('old', api.get_cookie())
        self.assertEqual(1, len(list(self.root.iterdir())))

    def test_bridge_credentials_only_stdin_and_private_environment(self):
        api.set_credentials('MUSIC_U=private-value')
        locations = []
        def run(command, **kwargs):
            self.assertEqual([api.paths.node_exe(), str(api.NODE_BRIDGE)], command)
            self.assertNotIn('private-value', str(command))
            self.assertEqual(45, kwargs['timeout'])
            self.assertIsInstance(kwargs['input'], bytes)
            self.assertEqual({'op': 'search', 'args': {'keywords': '中文'}, 'cookie': {'MUSIC_U': 'private-value'}},
                             json.loads(kwargs['input'].decode('utf-8')))
            self.assertNotIn('NETEASE_COOKIE', kwargs['env'])
            directory = Path(kwargs['cwd'])
            locations.append(directory)
            self.assertTrue((directory / 'anonymous_token').is_file())
            for key in ('TMP', 'TEMP', 'TMPDIR'):
                self.assertEqual(str(directory), kwargs['env'][key])
            return SimpleNamespace(returncode=0, stdout=b'{"ok":true,"body":{"code":200},"cookies":[]}', stderr=b'')
        with patch.dict(os.environ, {'NETEASE_COOKIE': 'must-not-inherit'}), patch.object(api.subprocess, 'run', side_effect=run):
            self.assertEqual(200, api.bridge_call('search', {'keywords': '中文'})['body']['code'])
        self.assertFalse(locations[0].exists())

    def test_bridge_errors_are_controlled(self):
        cases = [(SimpleNamespace(returncode=1, stdout=b'', stderr=b'SECRET'), 'UPSTREAM_ERROR'),
                 (SimpleNamespace(returncode=0, stdout=b'bad SECRET'), 'UPSTREAM_ERROR'),
                 (SimpleNamespace(returncode=0, stdout=b'{"ok":false,"code":"NETWORK_ERROR","error":"SECRET"}'), 'NETWORK_ERROR'),
                 (SimpleNamespace(returncode=0, stdout=b'{"ok":false,"code":"INVALID_INPUT"}'), 'INVALID_INPUT')]
        for result, code in cases:
            with self.subTest(code=code), patch.object(api.subprocess, 'run', return_value=result):
                self.assertNotIn('SECRET', str(self.assertCode(code, api.bridge_call, 'search', {}, cookie='')))
        for exc in (subprocess.TimeoutExpired('SECRET', 45), OSError('SECRET')):
            with patch.object(api.subprocess, 'run', side_effect=exc):
                self.assertCode('NETWORK_ERROR', api.bridge_call, 'search', {}, cookie='')

    def test_bridge_concurrency_never_exceeds_four(self):
        lock = threading.Lock()
        active = 0
        peak = 0
        four = threading.Event()
        release = threading.Event()
        def run(*args, **kwargs):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(active, peak)
                if active == 4:
                    four.set()
            release.wait(5)
            with lock:
                active -= 1
            return SimpleNamespace(returncode=0, stdout=b'{"ok":true,"body":{}}')
        with patch.object(api.subprocess, 'run', side_effect=run), concurrent.futures.ThreadPoolExecutor(8) as pool:
            futures = [pool.submit(api.bridge_call, 'search', {}, '') for _ in range(8)]
            try:
                self.assertTrue(four.wait(5))
            finally:
                release.set()
            for future in futures:
                future.result(5)
        self.assertEqual(4, peak)

    def test_anonymous_search_normalizes_no_fake_mid(self):
        body = {'code': 200, 'result': {'songs': [
            {'id': 123, 'name': 'Title', 'ar': [{'name': 'Singer'}], 'al': {'name': 'Album'},
             'dt': 123400, 'fee': 1, 'privilege': {'dl': 999000}},
            {'id': 124, 'name': 'Other', 'artists': [{'name': 'Artist'}], 'duration': 2000}]}}
        with patch.object(api, 'bridge_call', return_value={'body': body}) as bridge:
            songs = api.search('词', 2)
        self.assertEqual(('search', {'keywords': '词', 'limit': 2}, None), bridge.call_args.args)
        self.assertEqual('123', songs[0]['id'])
        self.assertEqual('netease', songs[0]['source'])
        self.assertEqual(123.4, songs[0]['duration'])
        self.assertTrue(songs[0]['rights']['requires_verification'])
        self.assertNotIn('mid', songs[0])
        self.assertEqual('Artist', songs[1]['singer'])
        with patch.object(api, 'bridge_call', return_value={'body': {'code': 200, 'result': {'songCount': 0}}}):
            self.assertEqual([], api.search('empty'))
        with patch.object(api, 'bridge_call', return_value={'body': {'code': 500}}):
            self.assertCode('UPSTREAM_ERROR', api.search, 'rejected')

    def test_input_rejected_before_any_bridge_call(self):
        with patch.object(api, 'bridge_call') as bridge:
            for kw, num in [('', 30), ('x', 0), ('x', 101), ('x', True), (None, 10)]:
                self.assertCode('INVALID_INPUT', api.search, kw, num)
            for sid in ('../1', True, '0', '1,2'):
                self.assertCode('INVALID_INPUT', api.download, sid)
            self.assertCode('INVALID_INPUT', api.download, '1', 'master')
            bridge.assert_not_called()

    def detail(self, dl=320000):
        return {'code': 200, 'songs': [{'id': 1, 'name': 'CON', 'ar': [{'name': 'A/B'}]}],
                'privileges': [{'id': 1, 'dl': dl}]}

    def download_body(self, **changes):
        data = {'id': 1, 'code': 200, 'url': 'http://m1.music.126.net/a', 'size': 5,
                'md5': hashlib.md5(b'audio').hexdigest(), 'type': 'mp3', 'level': 'exhigh', 'freeTrialInfo': None,
                'freeTrialPrivilege': {'resConsumable': False, 'userConsumable': False, 'listenType': None}}
        data.update(changes)
        return {'code': 200, 'data': data}

    def test_download_login_and_rights_precede_url_request(self):
        with patch.object(api, '_body', return_value=self.detail(0)) as body:
            self.assertCode('LOGIN_REQUIRED', api.download, '1')
            self.assertEqual(1, body.call_count)
            self.assertEqual('detail', body.call_args.args[0])
        api.set_credentials('MUSIC_U=secret')
        with patch.object(api, '_body', return_value=self.detail(0)) as body:
            error = self.assertCode('PERMISSION_DENIED', api.download, '1')
            self.assertEqual(403, error.status)
            self.assertEqual(1, body.call_count)
            self.assertEqual('detail', body.call_args.args[0])

    def test_free_anonymous_download_allowed_after_authoritative_rights_check(self):
        with patch.object(api, '_body', side_effect=[self.detail(), self.download_body()]) as body, patch.object(api, '_stream', return_value='Free.mp3'):
            result = api.download('1', 'hq')
        self.assertEqual('Free.mp3', result['name'])
        self.assertEqual(('detail', {'ids': '1'}, ''), body.call_args_list[0].args)
        self.assertEqual(('download', {'id': '1', 'level': 'exhigh'}, ''), body.call_args_list[1].args)

    def test_download_rejects_inner_code_trial_url_and_integrity_metadata(self):
        api.set_credentials('MUSIC_U=secret')
        for changes in ({'code': 404}, {'url': ''}, {'freeTrialInfo': {'start': 0}}, {'freeTrialInfo': {}},
                        {'size': 0}, {'size': api.MAX_DOWNLOAD_BYTES + 1}, {'md5': 'bad'}, {'md5': ''},
                        {'id': 2}, {'type': 'exe'}, {'level': 'hires'}):
            with self.subTest(changes=changes), patch.object(api, '_body', side_effect=[self.detail(), self.download_body(**changes)]), patch.object(api, '_stream') as stream:
                with self.assertRaises(api.ApiError):
                    api.download('1', 'hq')
                stream.assert_not_called()

    def test_authoritative_names_quality_and_exact_download_operation(self):
        api.set_credentials('MUSIC_U=secret')
        for quality, level in api.QUALITY.items():
            with patch.object(api, '_body', side_effect=[self.detail(), self.download_body(level=level)]) as body, patch.object(api, '_stream', return_value='CON - A_B.mp3') as stream:
                result = api.download('1', quality)
                self.assertEqual(('download', {'id': '1', 'level': level}, 'MUSIC_U=secret'), body.call_args.args)
                self.assertEqual('CON - A_B', stream.call_args.args[3])
                self.assertEqual({'name': 'CON - A_B.mp3', 'size': 5, 'quality': quality,
                                  'quality_name': api.QUALITY_NAMES[quality], 'lyrics': False}, result)
                json.dumps(result)

    def public_dns(self, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('1.1.1.1', 443))]

    def test_safe_url_approved_suffixes_upgrade_and_public_dns(self):
        with patch.object(api.socket, 'getaddrinfo', side_effect=self.public_dns):
            self.assertEqual('https://m1.music.126.net/a', api._safe_url('http://m1.music.126.net/a'))
            for url in ('https://evil126.net/a', 'https://evil.126.net/a', 'https://music.126.net.evil/a',
                        'https://music.126.net@evil/a', 'https://u:p@music.126.net/a',
                        'https://127.0.0.1/a', 'file:///a', 'https://music.126.net:8888/a'):
                self.assertCode('UNSAFE_URL', api._safe_url, url)
        for address in ('127.0.0.1', '10.0.0.1', '169.254.169.254', '::1', 'fc00::1'):
            with patch.object(api.socket, 'getaddrinfo', return_value=[(socket.AF_INET, 1, 6, '', (address, 443))]):
                self.assertCode('UNSAFE_URL', api._safe_url, 'https://music.126.net/a')

    def test_stream_redirect_sends_no_cookies_and_checks_md5(self):
        session = FakeSession([FakeResponse(status=302, headers={'Location': 'http://m2.music.126.net/b'}),
                               FakeResponse(headers={'Content-Length': '5'})])
        with patch.object(api.requests, 'Session', return_value=session), patch.object(api.socket, 'getaddrinfo', side_effect=self.public_dns):
            name = api._stream('http://m1.music.126.net/a', 5, hashlib.md5(b'audio').hexdigest(), 'test', 'mp3')
        self.assertEqual(b'audio', (self.root / 'output' / name).read_bytes())
        self.assertFalse(session.trust_env)
        self.assertIsNone(session.auth)
        self.assertEqual(2, len(session.calls))
        for url, kwargs, headers in session.calls:
            self.assertTrue(url.startswith('https://'))
            self.assertFalse(kwargs['allow_redirects'])
            self.assertNotIn('Cookie', headers)

    def test_stream_rejects_redirect_to_private_or_unapproved_without_request(self):
        session = FakeSession([FakeResponse(status=302, headers={'Location': 'https://evil.example/a'})])
        with patch.object(api.requests, 'Session', return_value=session), patch.object(api.socket, 'getaddrinfo', side_effect=self.public_dns):
            self.assertCode('UNSAFE_URL', api._stream, 'https://music.126.net/a', 5, '', 'test', 'mp3')
        self.assertEqual(1, len(session.calls))

    def test_stream_removes_partials_on_short_long_md5_or_header_mismatch(self):
        for response, md5 in [(FakeResponse(b'a'), ''), (FakeResponse(b'audio-more'), ''),
                              (FakeResponse(), '0' * 32), (FakeResponse(headers={'Content-Length': '6'}), '')]:
            with self.subTest(md5=md5), patch.object(api.requests, 'Session', return_value=FakeSession([response])), patch.object(api.socket, 'getaddrinfo', side_effect=self.public_dns):
                self.assertCode('DOWNLOAD_INVALID', api._stream, 'https://music.126.net/a', 5, md5, 'test', 'mp3')
            self.assertEqual([], list((self.root / 'output').iterdir()))

    def test_concurrent_outputs_unique_and_windows_names_safe(self):
        directory = self.root / 'output'
        directory.mkdir()
        def commit(i):
            part = directory / ('part-%s' % i)
            part.write_bytes(str(i).encode())
            return api._commit_download(str(part), 'same', 'mp3')
        with concurrent.futures.ThreadPoolExecutor(8) as pool:
            names = list(pool.map(commit, range(16)))
        self.assertEqual(16, len(set(names)))
        self.assertEqual({str(i).encode() for i in range(16)}, {(directory / n).read_bytes() for n in names})
        for name in ('CON', 'nul.txt', 'COM1', 'LPT9', 'aux'):
            self.assertTrue(api._safe_stem(name).startswith('_'))
        self.assertEqual('a_b_c', api._safe_stem('a/b:c .'))


if __name__ == '__main__':
    unittest.main()
