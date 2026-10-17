"""Offline regression tests for Migu search, metadata and lyrics.

Run from web with PYTHONPATH=. python -m unittest discover -s tests -p test_migu_api.py
No app imports, credentials, live DNS, or HTTP connections are needed.
"""
import json
import socket
import threading
import unittest
from unittest.mock import Mock, patch

import requests

import migu_api as api


SID = '0000123ABC'
LYRIC_URL = 'https://d.musicapp.migu.cn/public/song.lrc'
SECRET = 'private-token-do-not-expose'


class FakeResponse:
    def __init__(self, body=b'', status=200, headers=None, chunks=None):
        self.body = body
        self.status_code = status
        self.headers = headers or {}
        self.chunks = chunks
        self.closed = False
        self.chunk_sizes = []

    @classmethod
    def json(cls, body, **kwargs):
        return cls(json.dumps(body, ensure_ascii=False).encode('utf-8'), **kwargs)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def iter_content(self, size):
        self.chunk_sizes.append(size)
        yield from self.chunks if self.chunks is not None else [self.body]


class FakeSession:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.cookies = requests.cookies.RequestsCookieJar()
        self.cookies.set('foreign-session', SECRET)
        self.calls = []
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs, self.cookies.get_dict()))
        result = next(self.responses)
        if isinstance(result, Exception):
            raise result
        # Simulate upstream setting a cookie; next hop/request must not reuse it.
        self.cookies.set('upstream-session', SECRET)
        return result


def dns_rows(*addresses):
    return [(socket.AF_INET6 if ':' in address else socket.AF_INET,
             socket.SOCK_STREAM, socket.IPPROTO_TCP, '', (address, 443))
            for address in addresses]


class MiguApiTests(unittest.TestCase):
    def setUp(self):
        # A missed fixture must fail, never fall through to a real connection.
        patches = [
            patch.object(requests.sessions.Session, 'request',
                         side_effect=AssertionError('Live HTTP is forbidden')),
            patch.object(api.socket, 'getaddrinfo',
                         return_value=dns_rows('93.184.216.34')),
            patch.object(api, '_SEARCH_STATE', None),
            patch.object(api, '_SLOTS', threading.BoundedSemaphore(4)),
        ]
        self.network_guard, self.dns, _, _ = [p.start() for p in patches]
        for p in patches:
            self.addCleanup(p.stop)

    def transport(self, *responses):
        session = FakeSession(responses)
        p = patch.object(api, '_session', return_value=session)
        p.start()
        self.addCleanup(p.stop)
        return session

    def assertCode(self, expected, function, *args, status=None, **kwargs):
        with self.assertRaises(api.ApiError) as caught:
            function(*args, **kwargs)
        self.assertEqual(expected, caught.exception.code)
        if status is not None:
            self.assertEqual(status, caught.exception.status)
        self.assertNotIn(SECRET, str(caught.exception))
        return caught.exception

    def resource_response(self, **fields):
        return FakeResponse.json({'code': '000000', 'resource': [
            {'copyrightId': SID, 'contentId': '600929000000338479', **fields}]})

    def test_search_normalizes_and_preserves_string_leading_zero_ids(self):
        response = FakeResponse.json([
            {'copyrightId': SID, 'songName': '歌名', 'singerList': [{'name': '歌手'}],
             'album': '专辑', 'duration': '01:02:03', 'url': SECRET,
             'isDownload': '1'},
            {'copyrightId': '00000001', 'songName': 'Second', 'length': '123'},
            {'copyrightId': 456, 'songName': 'Numeric upstream ID'},
        ])
        session = self.transport(response)
        songs = api.search('  中文  ', 3)
        self.assertEqual([SID, '00000001', '456'], [s['id'] for s in songs])
        self.assertEqual({'source': 'migu', 'id': SID, 'name': '歌名',
                          'singer': '歌手', 'album': '专辑', 'duration': 3723,
                          'rights_hint': '以官方下载交易结果为准；部分歌曲或音质需会员或暂不可下载',
                          'rights': {'download_hint': False,
                                     'requires_verification': True}}, songs[0])
        self.assertEqual(123, songs[1]['duration'])
        self.assertNotIn(SECRET, json.dumps(songs))
        self.assertEqual(api.SEARCH_URL, session.calls[0][0])
        self.assertEqual({'text': '中文', 'pageNo': 1, 'pageSize': 3},
                         session.calls[0][1]['params'])

    def test_search_skips_unusable_rows_and_bounds_fields_and_count(self):
        body = [None, [], 'bad', {},
                {'copyrightId': '../bad', 'songName': 'bad'},
                {'copyrightId': SID, 'songName': None},
                {'copyrightId': SID, 'songName': 'n' * 600,
                 'singerList': [{'name': 's' * 600}], 'album': 'a' * 600},
                {'copyrightId': 'next', 'songName': 'Excluded'}]
        self.transport(FakeResponse.json(body))
        songs = api.search('query', 7)
        self.assertEqual(1, len(songs))
        for key in ('name', 'singer', 'album'):
            self.assertEqual(500, len(songs[0][key]))

    def test_duration_normalization(self):
        for value, expected in [('00:03:10', 190), ('12:60:00', 0),
                                ('01:00:60', 0), ('nope', 0), (None, 0),
                                ({}, 0), (-1, 0), (999999, 86400)]:
            with self.subTest(value=value):
                self.assertEqual(expected, api._duration(value))

    def test_invalid_search_input_rejected_before_network(self):
        cases = [(None, 30), (1, 30), ('', 30), (' \t', 30), ('x' * 201, 30),
                 ('a\nsecret', 30), ('a\x00b', 30), ('a\x7fb', 30),
                 ('q', True), ('q', '3'), ('q', None), ('q', 0),
                 ('q', -1), ('q', 101), ('q', 1.5)]
        with patch.object(api, '_session') as factory:
            for query, count in cases:
                with self.subTest(query=query, count=count):
                    self.assertCode('INVALID_INPUT', api.search, query, count, status=400)
            factory.assert_not_called()
        self.network_guard.assert_not_called()

    def test_invalid_ids_and_quality_rejected_before_network(self):
        invalid_ids = [None, 123, True, '', 'x' * 33, 'a b', '../x', 'a/b',
                       'x?token=y', '全角', 'a\n', [], {}]
        with patch.object(api, '_session') as factory:
            for sid in invalid_ids:
                for function in (api.resource, api.lyric_text, api.download):
                    with self.subTest(id=sid, function=function.__name__):
                        self.assertCode('INVALID_INPUT', function, sid, status=400)
            for quality in (None, '', 'lossless', 'PQ', [], True):
                self.assertCode('INVALID_INPUT', api.download, SID, quality, status=400)
            factory.assert_not_called()
        self.network_guard.assert_not_called()

    def test_old_search_redirect_is_changed_not_empty_results(self):
        for code in (301, 302, 303, 307, 308):
            with self.subTest(status=code):
                session = self.transport(FakeResponse(status=code, headers={
                    'Location': 'https://music.migu.cn/v4/search?token=' + SECRET}))
                self.assertCode('UPSTREAM_CHANGED', api.search, 'song', status=503)
                self.assertEqual(1, len(session.calls))
                self.assertFalse(session.calls[0][1]['allow_redirects'])
                self.assertFalse(api.status()['capabilities']['search'])
                self.assertFalse(api.status()['search_verified'])
                self.assertIn('不是未找到', api.status()['message'])

    def test_empty_search_is_valid_and_recovers_previous_changed_state(self):
        with patch.object(api, '_SEARCH_STATE', False):
            self.transport(FakeResponse.json([]))
            self.assertEqual([], api.search('no results'))
            state = api.status()
            self.assertTrue(state['capabilities']['search'])
            self.assertTrue(state['search_verified'])

    def test_malformed_json_and_search_schema_are_controlled_errors(self):
        bodies = [b'{broken ' + SECRET.encode(), b'\xff', b'null', b'1',
                  b'"string"', b'{}', b'{"musics":null}', b'{"musics":{}}',
                  b'{"musics":"not a list"}']
        for body in bodies:
            with self.subTest(body=body):
                self.transport(FakeResponse(body))
                self.assertCode('UPSTREAM_ERROR', api.search, 'song')
                self.assertFalse(api.status()['search_verified'])

    def test_utf8_bom_search_json_supported(self):
        self.transport(FakeResponse(b'\xef\xbb\xbf[]'))
        self.assertEqual([], api.search('query'))

    def test_status_is_offline_download_and_official_login_supported(self):
        with patch.object(api, '_session') as factory:
            for state in (None, False, True):
                with self.subTest(state=state), patch.object(api, '_SEARCH_STATE', state):
                    result = api.status()
                    self.assertEqual({'search': state is not False, 'download': True,
                                      'lyrics': True, 'login': True}, result['capabilities'])
                    self.assertEqual(state is True, result['search_verified'])
                    self.assertNotIn(SECRET, json.dumps(result))
            factory.assert_not_called()
        self.network_guard.assert_not_called()

    def test_network_failure_does_not_invent_verified_search(self):
        for prior in (None, False, True):
            with self.subTest(prior=prior), patch.object(api, '_SEARCH_STATE', prior):
                self.transport(requests.Timeout(SECRET))
                self.assertCode('NETWORK_ERROR', api.search, 'q')
                self.assertEqual(prior is True, api.status()['search_verified'])
                self.assertEqual(prior is not False, api.status()['capabilities']['search'])

    def test_resource_requires_exact_success_code_and_id(self):
        session = self.transport(FakeResponse.json({'code': '000000', 'resource': [
            {'copyrightId': '123ABC', 'songName': 'Wrong'},
            {'copyrightId': SID, 'songName': 'Exact'}]}))
        self.assertEqual('Exact', api.resource(SID)['songName'])
        self.assertEqual(api.RESOURCE_URL, session.calls[0][0])
        self.assertEqual({'copyrightId': SID, 'resourceType': 2},
                         session.calls[0][1]['params'])
        for code in (None, 0, 200, '0', '000000 ', 'SUCCESS'):
            with self.subTest(code=code):
                self.transport(FakeResponse.json({'code': code, 'resource': [
                    {'copyrightId': SID}]}))
                self.assertCode('UPSTREAM_ERROR', api.resource, SID)

    def test_resource_missing_or_malformed_never_uses_first_or_other_id(self):
        for resources in ([], [None, {}, {'copyrightId': 'other'}],
                          [{'copyrightId': 123}], [{'copyrightId': '123ABC'}]):
            with self.subTest(resources=resources):
                self.transport(FakeResponse.json({'code': '000000', 'resource': resources}))
                self.assertCode('NOT_FOUND', api.resource, SID, status=404)
        for resources in (None, {}, 'not a list', 1):
            with self.subTest(resources=resources):
                self.transport(FakeResponse.json({'code': '000000', 'resource': resources}))
                self.assertCode('UPSTREAM_ERROR', api.resource, SID)

    def test_current_official_search_sample_and_multiple_singers(self):
        self.assertEqual('https://app.u.nf.migu.cn/pc/resource/song/item/search/v1.0',
                         api.SEARCH_URL)
        self.transport(FakeResponse.json([{
            'songName': '泪桥', 'copyrightId': '69905307315',
            'singerList': [{'id': '1', 'name': '古巨基'}, None,
                           {'id': '2', 'name': '周深'}, {'name': None}],
            'duration': 257, 'album': '声生不息',
            'contentId': '600929000000338479', 'songId': 'song-id',
            'audioFormats': [{'url': 'https://audio.invalid/' + SECRET}],
            'lrcUrl': LYRIC_URL,
        }]))
        song = api.search('泪桥', 1)[0]
        self.assertEqual(('69905307315', '泪桥', '古巨基/周深', 257),
                         (song['id'], song['name'], song['singer'], song['duration']))
        self.assertFalse(song['rights']['download_hint'])
        self.assertTrue(song['rights']['requires_verification'])
        for field in ('url', 'audioFormats', 'contentId', 'songId', 'lrcUrl'):
            self.assertNotIn(field, song)

    def test_resource_rejects_raw_list_even_though_search_accepts_it(self):
        self.transport(FakeResponse.json([{'copyrightId': SID}]))
        self.assertCode('UPSTREAM_ERROR', api.resource, SID)

    def test_exact_https_lyric_host_and_public_dns_allowed(self):
        for url in (LYRIC_URL, 'https://d.musicapp.migu.cn:443/a.lrc?x=1',
                    'https://D.MUSICAPP.MIGU.CN/a.lrc'):
            with self.subTest(url=url):
                self.assertEqual(url, api._lyric_url(url))
        self.dns.assert_called_with('d.musicapp.migu.cn', 443, type=socket.SOCK_STREAM)
        self.dns.return_value = dns_rows('2606:4700:4700::1111')
        self.assertEqual(LYRIC_URL, api._lyric_url(LYRIC_URL))

    def test_unsafe_lyric_urls_rejected_without_http(self):
        urls = [None, 1, '', 'http://d.musicapp.migu.cn/a',
                '//d.musicapp.migu.cn/a', 'file:///etc/passwd',
                'https://127.0.0.1/a', 'https://[::1]/a',
                'https://d.musicapp.migu.cn.evil.invalid/a',
                'https://evil-d.musicapp.migu.cn/a',
                'https://d.musicapp.migu.cn./a', 'https://musicapp.migu.cn/a',
                'https://c.musicapp.migu.cn/a',
                'https://user:password@d.musicapp.migu.cn/a',
                'https://d.musicapp.migu.cn@evil.invalid/a',
                'https://d.musicapp.migu.cn:80/a',
                'https://d.musicapp.migu.cn:bad/a',
                'https://d.musicapp.migu.cn/a\nb',
                'https://d.musicapp.migu.cn/a b',
                'https://d.musicapp.migu.cn/\x00', LYRIC_URL + 'x' * 8192]
        for url in urls:
            with self.subTest(url=url):
                session = self.transport()
                self.assertCode('UNSAFE_URL', api._read, url, lyric=True)
                self.assertEqual([], session.calls)
        self.dns.assert_not_called()

    def test_lyric_dns_must_have_only_public_addresses(self):
        for addresses in [(), ('127.0.0.1',), ('10.0.0.1',), ('172.16.0.1',),
                          ('192.168.1.1',), ('169.254.169.254',), ('0.0.0.0',),
                          ('224.0.0.1',), ('::1',), ('fc00::1',), ('fe80::1',),
                          ('::ffff:127.0.0.1',), ('::ffff:224.0.0.1',),
                          ('93.184.216.34', '10.0.0.1')]:
            with self.subTest(addresses=addresses):
                self.dns.return_value = dns_rows(*addresses)
                session = self.transport()
                self.assertCode('UNSAFE_URL', api._lyric_url, LYRIC_URL)
                self.assertEqual([], session.calls)
        self.dns.side_effect = socket.gaierror(SECRET)
        self.assertCode('UNSAFE_URL', api._lyric_url, LYRIC_URL)

    def test_lyric_redirect_target_checked_before_next_request(self):
        for target in ('https://evil.invalid/a', 'http://d.musicapp.migu.cn/a',
                       'https://127.0.0.1/a', '//evil.invalid/a',
                       'https://user@d.musicapp.migu.cn/a'):
            with self.subTest(target=target):
                session = self.transport(FakeResponse(status=302, headers={'Location': target}))
                self.assertCode('UNSAFE_URL', api._read, LYRIC_URL, lyric=True)
                self.assertEqual([LYRIC_URL], [c[0] for c in session.calls])

    def test_allowed_relative_redirect_revalidates_dns_and_drops_cookies(self):
        first = FakeResponse(status=302, headers={'Location': '/next.lrc'})
        last = FakeResponse(b'[00:01.00]line')
        session = self.transport(first, last)
        self.assertEqual(b'[00:01.00]line', api._read(LYRIC_URL, lyric=True))
        self.assertEqual([LYRIC_URL, 'https://d.musicapp.migu.cn/next.lrc'],
                         [c[0] for c in session.calls])
        self.assertEqual(2, self.dns.call_count)
        self.assertTrue(all(cookies == {} for _, _, cookies in session.calls))
        self.assertTrue(first.closed and last.closed and session.closed)
        self.assertTrue(all(not c[1]['allow_redirects'] for c in session.calls))

    def test_public_to_private_dns_change_on_redirect_is_rejected(self):
        self.dns.side_effect = [dns_rows('93.184.216.34'), dns_rows('127.0.0.1')]
        session = self.transport(FakeResponse(status=307, headers={'Location': '/next.lrc'}))
        self.assertCode('UNSAFE_URL', api._read, LYRIC_URL, lyric=True)
        self.assertEqual(1, len(session.calls))

    def test_redirect_loop_and_missing_location_are_bounded(self):
        session = self.transport(*[FakeResponse(status=302, headers={'Location': '/again'})
                                   for _ in range(4)])
        self.assertCode('UPSTREAM_ERROR', api._read, LYRIC_URL, lyric=True)
        self.assertEqual(4, len(session.calls))
        self.transport(FakeResponse(status=302))
        self.assertCode('UPSTREAM_ERROR', api._read, LYRIC_URL, lyric=True)

    def test_session_factory_disables_ambient_auth_and_uses_fresh_sessions(self):
        first, second = api._session(), api._session()
        self.addCleanup(first.close)
        self.addCleanup(second.close)
        self.assertIsNot(first, second)
        self.assertIsNot(first.cookies, second.cookies)
        for session in (first, second):
            self.assertFalse(session.trust_env)
            self.assertIsNone(session.auth)
            self.assertNotIn('Cookie', session.headers)
            self.assertNotIn('Authorization', session.headers)
            self.assertEqual('identity', session.headers['Accept-Encoding'])
            self.assertEqual('014X031', session.headers['channel'])
            self.assertEqual('6.8.8', session.headers['version'])
            self.assertEqual('H5', session.headers['platform'])
            self.assertEqual({}, session.cookies.get_dict())
        first.cookies.set('other-platform', SECRET)
        self.assertEqual({}, second.cookies.get_dict())
        self.network_guard.assert_not_called()

    def test_each_read_creates_and_closes_an_independent_session(self):
        sessions = [FakeSession([FakeResponse(b'one')]), FakeSession([FakeResponse(b'two')])]
        with patch.object(api, '_session', side_effect=sessions) as factory:
            self.assertEqual(b'one', api._read(api.SEARCH_URL))
            self.assertEqual(b'two', api._read(api.RESOURCE_URL))
            self.assertEqual(2, factory.call_count)
        for session in sessions:
            self.assertTrue(session.closed)
            self.assertEqual({}, session.calls[0][2])

    def test_http_timeouts_streaming_and_bounded_chunks_are_explicit(self):
        response = FakeResponse(b'1234', headers={'Content-Length': '4'})
        session = self.transport(response)
        self.assertEqual(b'1234', api._read(api.SEARCH_URL, {'q': 'x'}, limit=4))
        self.assertEqual({'params': {'q': 'x'}, 'stream': True,
                          'timeout': (8, 15), 'allow_redirects': False}, session.calls[0][1])
        self.assertEqual([64 * 1024], response.chunk_sizes)
        self.assertTrue(response.closed and session.closed)

    def test_header_and_stream_size_limits_and_length_mismatches(self):
        responses = [FakeResponse(b'', headers={'Content-Length': '5'}),
                     FakeResponse(b'', headers={'Content-Length': '-1'}),
                     FakeResponse(b'', headers={'Content-Length': 'garbage ' + SECRET}),
                     FakeResponse(b'123', headers={'Content-Length': '4'}),
                     FakeResponse(b'1234', headers={'Content-Length': '3'}),
                     FakeResponse(chunks=[b'12', b'345']),
                     FakeResponse(b'12345')]
        for response in responses:
            with self.subTest(headers=response.headers, chunks=response.chunks):
                session = self.transport(response)
                self.assertCode('UPSTREAM_ERROR', api._read, api.SEARCH_URL, limit=4)
                self.assertTrue(response.closed and session.closed)

    def test_requests_failures_and_http_errors_do_not_expose_raw_secrets(self):
        for error in (requests.Timeout(SECRET), requests.ConnectionError(SECRET),
                      requests.exceptions.SSLError(SECRET)):
            self.transport(error)
            self.assertCode('NETWORK_ERROR', api.search, 'query')
        for code in (400, 401, 403, 404, 429, 500):
            self.transport(FakeResponse(SECRET.encode(), status=code))
            self.assertCode('NETWORK_ERROR', api.search, 'query')
        response = FakeResponse()
        response.iter_content = Mock(side_effect=requests.ConnectionError(SECRET))
        session = self.transport(response)
        self.assertCode('NETWORK_ERROR', api._read, api.SEARCH_URL)
        self.assertTrue(response.closed and session.closed)

    def test_deadline_is_enforced_without_sleeping(self):
        self.transport(FakeResponse(chunks=[b'a']))
        with patch.object(api.time, 'monotonic', side_effect=[100, 141]):
            self.assertCode('NETWORK_ERROR', api._read, api.SEARCH_URL)

    def test_busy_gate_never_opens_session_and_slot_released_on_failure(self):
        gate = Mock()
        gate.acquire.return_value = False
        with patch.object(api, '_SLOTS', gate), patch.object(api, '_session') as factory:
            self.assertCode('BUSY', api._read, api.SEARCH_URL, status=503)
            gate.acquire.assert_called_once_with(timeout=5)
            gate.release.assert_not_called()
            factory.assert_not_called()
        gate.reset_mock()
        gate.acquire.return_value = True
        self.transport(requests.Timeout(SECRET))
        with patch.object(api, '_SLOTS', gate):
            self.assertCode('NETWORK_ERROR', api._read, api.SEARCH_URL)
            gate.release.assert_called_once_with()

    def test_lyric_text_uses_only_exact_resource_and_lyric_size_bound(self):
        lyric = '\ufeff[00:01.00]中文歌词\n'
        session = self.transport(self.resource_response(lrcUrl=LYRIC_URL),
                                 FakeResponse(lyric.encode('utf-8')))
        self.assertEqual('[00:01.00]中文歌词\n', api.lyric_text(SID))
        self.assertEqual([api.RESOURCE_URL, LYRIC_URL], [c[0] for c in session.calls])
        with patch.object(api, 'resource', return_value={'lrcUrl': LYRIC_URL}), \
                patch.object(api, '_read', return_value=b'line') as read:
            self.assertEqual('line', api.lyric_text(SID))
            read.assert_called_once_with(LYRIC_URL, limit=api.MAX_LYRICS, lyric=True)

    def test_missing_lyric_is_empty_without_additional_request(self):
        for fields in ({}, {'lrcUrl': None}, {'lrcUrl': ''}):
            session = self.transport(self.resource_response(**fields))
            self.assertEqual('', api.lyric_text(SID))
            self.assertEqual([api.RESOURCE_URL], [c[0] for c in session.calls])
            self.dns.assert_not_called()

    def test_lyric_rejects_html_and_invalid_encoding(self):
        for body in (b'<!DOCTYPE html><html>login</html>', b'<HTML>login</HTML>',
                     b'  <script>alert(1)</script>', b'prefix<BODY>error</BODY>',
                     b'\xff' + SECRET.encode()):
            with self.subTest(body=body):
                self.transport(self.resource_response(lrcUrl=LYRIC_URL), FakeResponse(body))
                self.assertCode('UPSTREAM_ERROR', api.lyric_text, SID)

    def test_oversized_lyric_rejected_before_body_read(self):
        response = FakeResponse(headers={'Content-Length': str(api.MAX_LYRICS + 1)})
        self.transport(self.resource_response(lrcUrl=LYRIC_URL), response)
        self.assertCode('UPSTREAM_ERROR', api.lyric_text, SID)
        self.assertEqual([], response.chunk_sizes)

    def test_unknown_error_codes_are_sanitized(self):
        error = api.ApiError(SECRET)
        self.assertEqual('UPSTREAM_ERROR', error.code)
        self.assertNotIn(SECRET, str(error))


if __name__ == '__main__':
    unittest.main()
