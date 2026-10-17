"""Offline official-download regressions; synthetic media only, no song downloads."""
import concurrent.futures
import copy
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

import requests

import migu_api as api
from test_migu_api import FakeResponse, FakeSession, dns_rows, SECRET

SID = '13200500009'
CONTENT = '600930000001596231'
SONG = '1143529167'
URL = 'http://dlsdownfree.nf.migu.cn/wlansst?pars=a%2Bb%2F%3D+opaque&x=1&x=2'
HTTPS_URL = URL.replace('http:', 'https:', 1)
ITEM = {'copyrightId': SID, 'contentId': CONTENT, 'songId': SONG,
        'songName': 'Synthetic', 'singer': 'Test', 'length': '00:03:33',
        'isDownload': '0', 'codeRate': {'PQ': {'isCodeRateDownload': '0'}},
        'url': 'https://evil.invalid/player?token=' + SECRET}
SIZE = 3409504
DATA = {'copyrightId': SID, 'contentId': CONTENT, 'songId': SONG,
        'suffix': 'mp3', 'url': URL, 'size': str(SIZE), 'formatId': '020007',
        'encryptionType': '0', 'lrcUrl': SECRET}


def media(size=SIZE, extension='mp3'):
    return (b'fLaC' if extension == 'flac' else b'ID3\x04') + b'\0' * (size - 4)


class FakeGateway(FakeSession):
    def get(self, url, addresses, deadline):
        self.cookies.clear()
        response = super().get(url, addresses=addresses, deadline=deadline,
                               allow_redirects=False)
        response.status = response.status_code
        return response

    def chunks(self, response, deadline):
        return response.iter_content(64 * 1024)


class MiguDownloadTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        patches = [
            patch.object(api.paths, 'OUTPUT_DIR', self.directory),
            patch.object(requests.sessions.Session, 'request',
                         side_effect=AssertionError('Live HTTP forbidden')),
            patch.object(api.socket, 'getaddrinfo', return_value=dns_rows('93.184.216.34')),
            patch.object(api, '_DOWNLOAD_SLOTS', threading.BoundedSemaphore(2)),
            patch.object(api.paths, 'find_exe', return_value='fake-ffmpeg.exe'),
            patch.object(api.subprocess, 'run', side_effect=self.decode),
        ]
        self.output, self.guard, self.dns, _, self.find, self.decoder = [p.start() for p in patches]
        for p in patches:
            self.addCleanup(p.stop)

    def decode(self, command, **kwargs):
        kwargs['stdout'].write(b'out_time_us=213000000\nprogress=end\n')
        return subprocess.CompletedProcess(command, 0)

    def transport(self, *responses):
        # Metadata uses isolated web sessions; media uses a separate pinned HTTPS
        # connection with no cookies at all. Share fixtures, not credentials.
        session = FakeSession(responses)
        gateway = FakeGateway([])
        gateway.responses = session.responses
        gateway.calls = session.calls
        session.gateway = gateway
        patches = [patch.object(api, '_session', return_value=session),
                   patch.object(api, '_GatewaySession', return_value=gateway)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return session

    def transaction(self, data=None, item=None, body=None, response=None):
        return self.transport(
            FakeResponse.json({'code': '000000', 'resource': [copy.deepcopy(item or ITEM)]}),
            FakeResponse.json(body if body is not None else {'code': '000000', 'data': data or DATA}),
            response or FakeResponse(media(), headers={'Content-Length': str(SIZE)}))

    def assertCode(self, code, function, *args, status=None, **kwargs):
        with self.assertRaises(api.ApiError) as caught:
            function(*args, **kwargs)
        self.assertEqual(code, caught.exception.code)
        if status is not None:
            self.assertEqual(status, caught.exception.status)
        self.assertNotIn(SECRET, str(caught.exception))
        self.assertNotIn('pars=', str(caught.exception))
        return caught.exception

    def assertClean(self):
        self.assertEqual([], list(self.directory.iterdir()))

    def test_official_transaction_and_zero_metadata_flags_do_not_gate_download(self):
        session = self.transaction()
        result = api.download(SID)
        self.assertEqual({'name': 'Synthetic - Test.mp3', 'size': SIZE,
                          'quality': 'standard', 'quality_name': '标准',
                          'format': 'mp3', 'lyrics': False}, result)
        self.assertEqual(media(), (self.directory / result['name']).read_bytes())
        self.assertEqual([api.RESOURCE_URL, api.DOWNLOAD_URL, HTTPS_URL], [c[0] for c in session.calls])
        self.assertEqual({'songId': SONG, 'formatType': 'PQ'}, session.calls[1][1]['params'])
        self.assertTrue(all(cookies == {} for _, _, cookies in session.calls))
        self.assertTrue(all(not options['allow_redirects'] for _, options, _ in session.calls))
        self.assertNotIn(SECRET, str(result))
        self.assertEqual(1, len(list(self.directory.iterdir())))
        self.decoder.assert_called_once()
        command = self.decoder.call_args.args[0]
        self.assertIn('-xerror', command)
        self.assertEqual('file,pipe', command[command.index('-protocol_whitelist') + 1])
        self.assertGreater(self.decoder.call_args.kwargs['timeout'], 0)
        self.assertIs(self.decoder.call_args.kwargs['stderr'], subprocess.DEVNULL)

    def test_each_requested_quality_requires_exact_format_without_url_rewrites(self):
        for quality, rate, fid, ext in [('standard', 'PQ', '020007', 'mp3'),
                                        ('hq', 'HQ', '020010', 'mp3'),
                                        ('sq', 'SQ', '011002', 'flac')]:
            with self.subTest(quality=quality):
                data = {**DATA, 'formatId': fid, 'suffix': ext, 'formatType': rate}
                session = self.transaction(data=data, response=FakeResponse(media(extension=ext)))
                result = api.download(SID, quality)
                self.assertEqual(quality, result['quality'])
                self.assertEqual(ext, result['format'])
                self.assertEqual(HTTPS_URL, session.calls[-1][0])
                self.assertEqual({'songId': SONG, 'formatType': rate}, session.calls[1][1]['params'])

    def test_granted_mp3_is_never_reported_as_requested_lossless(self):
        session = self.transaction()
        result = api.download(SID, 'sq')
        self.assertEqual({'songId': SONG, 'formatType': 'SQ'}, session.calls[1][1]['params'])
        self.assertEqual(('standard', '标准', 'mp3'),
                         (result['quality'], result['quality_name'], result['format']))
        self.assertTrue(result['name'].endswith('.mp3'))

    def test_official_resource_name_and_singers_used_for_filename(self):
        item = {k: v for k, v in ITEM.items() if k not in ('songName', 'singer')}
        item.update(name='泪桥', singers=[{'name': '古巨基'}, None, {'name': '周深'}])
        self.transaction(item=item)
        result = api.download(SID)
        self.assertEqual('泪桥 - 古巨基_周深.mp3', result['name'])

    def test_target_price_failure_membership_copyright_unknown_are_controlled(self):
        cases = [('200000', 'PRICE_LOOKUP_FAILED', 503), (200000, 'PRICE_LOOKUP_FAILED', 503),
                 ('200010', 'MEMBERSHIP_REQUIRED', 403), ('440000', 'COPYRIGHT_UNAVAILABLE', 410)]
        cases += [(c, 'DOWNLOAD_UNAVAILABLE', 503) for c in (None, 0, '000000 ', 'unknown', '403')]
        for code, expected, status in cases:
            with self.subTest(code=code):
                item = {**ITEM, 'copyrightId': '69905307315',
                        'contentId': '600929000000338479', 'songId': '1140574869'}
                session = self.transaction(item=item, body={'code': code, 'info': SECRET, 'url': URL})
                error = self.assertCode(expected, api.download, item['copyrightId'], status=status)
                self.assertNotIn('请先登录', str(error))
                self.assertEqual(2, len(session.calls))
                self.assertEqual({'songId': item['songId'], 'formatType': 'PQ'}, session.calls[1][1]['params'])
                self.dns.assert_not_called()
                self.assertClean()

    def test_resource_ids_must_be_valid_and_returned_ids_match_exactly(self):
        for field in ('copyrightId', 'contentId', 'songId'):
            for value in (None, '', 123, True, 'x' * 33, '../bad'):
                with self.subTest(field=field, value=value):
                    item = {**ITEM, field: value}
                    with patch.object(api, 'resource', return_value=item), patch.object(api, '_json') as transaction:
                        self.assertCode('UPSTREAM_ERROR', api.download, SID)
                        transaction.assert_not_called()
            for value in (None, '', 'other', int(ITEM[field]), '0' + ITEM[field]):
                data = {**DATA, field: value}
                session = self.transaction(data=data)
                self.assertCode('DOWNLOAD_INVALID', api.download, SID)
                self.assertEqual(2, len(session.calls))
        self.assertClean()

    def test_malformed_transaction_schema_is_rejected(self):
        for body in ([], 'bad', 123, None, {'code': '000000'}, {'code': '000000', 'data': []}):
            with self.subTest(body=body), patch.object(api, 'resource', return_value=ITEM), \
                    patch.object(api, '_json', return_value=body):
                self.assertCode('UPSTREAM_ERROR', api.download, SID)
        self.assertClean()

    def test_encryption_and_format_mismatches_fail_before_transfer(self):
        for value in (None, '', 0, 1, '1', 'unknown'):
            self.transaction(data={**DATA, 'encryptionType': value})
            self.assertCode('ENCRYPTED_UNSUPPORTED', api.download, SID)
        for changes in ({'suffix': 'flac'}, {'suffix': '.mp3'}, {'suffix': 'MP3'},
                        {'suffix': '../mp3'}, {'suffix': None}, {'formatId': '011002'},
                        {'formatId': 20007}, {'formatId': None}, {'formatType': 'HQ'},
                        {'formatType': None}):
            with self.subTest(changes=changes):
                self.transaction(data={**DATA, **changes})
                self.assertCode('DOWNLOAD_INVALID', api.download, SID)
        self.dns.assert_not_called()
        self.assertClean()

    def test_advertised_size_and_resource_duration_are_bounded(self):
        for size in (None, 0, SIZE, '', '-1', '0', '1.5', '9' * 5000,
                     '100', str(api.MAX_DOWNLOAD_BYTES + 1), '0' * 13):
            self.transaction(data={**DATA, 'size': size})
            self.assertCode('DOWNLOAD_INVALID', api.download, SID)
        for duration in ('00:00:00', '00:00:09', '00:31:00', None, 'nope'):
            self.transaction(item={**ITEM, 'length': duration})
            self.assertCode('DOWNLOAD_INVALID', api.download, SID)
        self.dns.assert_not_called()
        self.assertClean()

    def test_missing_validator_fails_closed_before_audio_transfer(self):
        session = self.transaction()
        self.find.return_value = None
        self.assertCode('VALIDATOR_UNAVAILABLE', api.download, SID, status=503)
        self.assertEqual(2, len(session.calls))
        self.assertClean()

    def test_http_upgrade_preserves_opaque_query_and_exact_gateway(self):
        self.assertEqual(HTTPS_URL, api._download_url(URL))
        self.assertEqual(HTTPS_URL, api._download_url(HTTPS_URL))
        self.dns.assert_called_with('dlsdownfree.nf.migu.cn', 443, type=socket.SOCK_STREAM)

    def test_unsafe_urls_and_unobserved_paths_never_open_http(self):
        bad = [None, 123, '', 'ftp://dlsdownfree.nf.migu.cn/wlansst?pars=x',
               '//dlsdownfree.nf.migu.cn/wlansst?pars=x',
               'https://dlsdownfree.nf.migu.cn.evil.invalid/wlansst?pars=x',
               'https://sub.dlsdownfree.nf.migu.cn/wlansst?pars=x',
               'https://dlsdownfree.nf.migu.cn./wlansst?pars=x',
               'https://127.0.0.1/wlansst?pars=x', 'https://[::1]/wlansst?pars=x',
               HTTPS_URL.replace('https://', 'https://user:password@'),
               HTTPS_URL.replace('.cn/', '.cn:80/'), URL.replace('.cn/', '.cn:443/'),
               HTTPS_URL.replace('.cn/', '.cn:bad/'), HTTPS_URL + '#fragment',
               HTTPS_URL + '\n', HTTPS_URL + ' ', HTTPS_URL + '\x7f', HTTPS_URL + '\\x',
               HTTPS_URL + '%zz', HTTPS_URL + '中',
               HTTPS_URL + 'x' * 8192, 'https://dlsdownfree.nf.migu.cn/wlansst',
               HTTPS_URL.replace('/wlansst', '/other'), HTTPS_URL.replace('/wlansst', '/a/../wlansst')]
        for url in bad:
            with self.subTest(url=url):
                session = self.transport()
                self.assertCode('UNSAFE_URL', api._stream, url, SIZE, 'x', 'mp3', 213, 'ffmpeg')
                self.assertEqual([], session.calls)
        self.assertClean()

    def test_dns_empty_private_mixed_multicast_and_errors_rejected(self):
        for addresses in [(), ('127.0.0.1',), ('10.0.0.1',), ('169.254.169.254',),
                          ('224.0.0.1',), ('ff02::1',), ('::1',), ('fc00::1',),
                          ('fe80::1',), ('0.0.0.0',), ('::ffff:127.0.0.1',),
                          ('::ffff:224.0.0.1',),
                          ('93.184.216.34', '192.168.0.1')]:
            with self.subTest(addresses=addresses):
                self.dns.return_value = dns_rows(*addresses)
                self.assertCode('UNSAFE_URL', api._download_url, URL)
        self.dns.side_effect = socket.gaierror(SECRET)
        self.assertCode('UNSAFE_URL', api._download_url, URL)

    def test_redirects_checked_every_hop_and_cookies_removed(self):
        first = FakeResponse(status=302, headers={'Location': '/wlansst?pars=second%2Bopaque'})
        last = FakeResponse(media())
        session = self.transport(first, last)
        name = api._stream(URL, SIZE, 'x', 'mp3', 213, 'ffmpeg')
        self.assertEqual('x.mp3', name)
        self.assertEqual([HTTPS_URL, 'https://dlsdownfree.nf.migu.cn/wlansst?pars=second%2Bopaque'],
                         [c[0] for c in session.calls])
        self.assertEqual(2, self.dns.call_count)
        self.assertTrue(first.closed and last.closed and session.gateway.closed)
        self.assertTrue(all(not cookies for _, _, cookies in session.calls))

    def test_redirect_to_unsafe_destination_and_dns_change_are_rejected(self):
        for target in ('https://evil.invalid/x?token=' + SECRET,
                       'ftp://dlsdownfree.nf.migu.cn/wlansst?pars=x',
                       'https://user@dlsdownfree.nf.migu.cn/wlansst?pars=x', '/elsewhere'):
            session = self.transport(FakeResponse(status=302, headers={'Location': target}))
            self.assertCode('UNSAFE_URL', api._stream, URL, SIZE, 'x', 'mp3', 213, 'ffmpeg')
            self.assertEqual(1, len(session.calls))
        self.dns.side_effect = [dns_rows('93.184.216.34'), dns_rows('127.0.0.1')]
        session = self.transport(FakeResponse(status=307, headers={'Location': '/wlansst?pars=next'}))
        self.assertCode('UNSAFE_URL', api._stream, URL, SIZE, 'x', 'mp3', 213, 'ffmpeg')
        self.assertEqual(1, len(session.calls))
        self.assertClean()

    def test_redirect_loop_and_missing_location_are_bounded(self):
        session = self.transport(*[FakeResponse(status=302, headers={'Location': URL}) for _ in range(5)])
        self.assertCode('UNSAFE_URL', api._stream, URL, SIZE, 'x', 'mp3', 213, 'ffmpeg')
        self.assertEqual(5, len(session.calls))
        self.transport(FakeResponse(status=302))
        self.assertCode('UNSAFE_URL', api._stream, URL, SIZE, 'x', 'mp3', 213, 'ffmpeg')
        self.assertClean()

    def test_header_body_bounds_partial_http_encoding_and_stream_errors_cleanup(self):
        cases = [(FakeResponse(media(), headers={'Content-Length': str(SIZE - 1)}), 'DOWNLOAD_INVALID'),
                 (FakeResponse(headers={'Content-Length': str(SIZE + 1)}), 'DOWNLOAD_INVALID'),
                 (FakeResponse(headers={'Content-Length': '9' * 5000}), 'DOWNLOAD_INVALID'),
                 (FakeResponse(headers={'Content-Length': '-1'}), 'DOWNLOAD_INVALID'),
                 (FakeResponse(headers={'Content-Encoding': 'gzip'}), 'DOWNLOAD_INVALID'),
                 (FakeResponse(chunks=[media(SIZE - 1)]), 'DOWNLOAD_INVALID'),
                 (FakeResponse(chunks=[media(), b'x']), 'DOWNLOAD_INVALID'),
                 (FakeResponse(chunks=[]), 'DOWNLOAD_INVALID'),
                 (FakeResponse(status=206), 'NETWORK_ERROR'),
                 (FakeResponse(status=403), 'NETWORK_ERROR'),
                 (requests.Timeout(SECRET), 'NETWORK_ERROR')]
        response = FakeResponse()
        response.iter_content = Mock(side_effect=requests.ConnectionError(SECRET))
        cases.append((response, 'NETWORK_ERROR'))
        for response, code in cases:
            with self.subTest(code=code, response=response):
                session = self.transport(response)
                self.assertCode(code, api._stream, URL, SIZE, 'x', 'mp3', 213, 'ffmpeg')
                self.assertTrue(session.gateway.closed)
                if isinstance(response, FakeResponse):
                    self.assertTrue(response.closed)
                self.assertClean()

    def test_media_signature_decode_errors_and_snippets_never_publish(self):
        for payload in (b'<html>' + b'\0' * (SIZE - 6), media(extension='flac')):
            self.transport(FakeResponse(payload))
            self.assertCode('DOWNLOAD_INVALID', api._stream, URL, SIZE, 'x', 'mp3', 213, 'ffmpeg')
            self.assertClean()
        for seconds, returncode, end in [(30, 0, True), (0, 0, True), (300, 0, True),
                                         (213, 1, True), (213, 0, False)]:
            def decode(command, **kwargs):
                kwargs['stdout'].write(('out_time_us=%s\n%s' %
                                        (seconds * 1000000, 'progress=end\n' if end else '')).encode())
                return subprocess.CompletedProcess(command, returncode)
            self.decoder.side_effect = decode
            self.transport(FakeResponse(media()))
            self.assertCode('DOWNLOAD_INVALID', api._stream, URL, SIZE, 'x', 'mp3', 213, 'ffmpeg')
            self.assertClean()

    def test_decode_timeout_and_missing_executable_are_sanitized(self):
        for exc, code in [(subprocess.TimeoutExpired(SECRET, 1), 'DOWNLOAD_INVALID'),
                          (OSError(SECRET), 'VALIDATOR_UNAVAILABLE')]:
            self.decoder.side_effect = exc
            self.transport(FakeResponse(media()))
            self.assertCode(code, api._stream, URL, SIZE, 'x', 'mp3', 213, 'ffmpeg')
            self.assertClean()

    def test_deadline_before_request_during_stream_and_after_decode(self):
        for ticks in ([100, 401], [100, 100, 100, 401],
                      [100, 100, 100, 100, 100, 100, 401]):
            self.transport(FakeResponse(media()))
            with patch.object(api.time, 'monotonic', side_effect=ticks):
                self.assertCode('NETWORK_ERROR', api._stream, URL, SIZE, 'x', 'mp3', 213, 'ffmpeg')
            self.assertClean()

    def test_download_concurrency_gate_and_release_on_failure(self):
        gate = Mock()
        gate.acquire.return_value = False
        with patch.object(api, '_DOWNLOAD_SLOTS', gate), patch.object(api, 'resource') as resource:
            self.assertCode('BUSY', api.download, SID, status=503)
            resource.assert_not_called()
            gate.acquire.assert_called_once_with(timeout=5)
            gate.release.assert_not_called()
        gate.reset_mock()
        gate.acquire.return_value = True
        with patch.object(api, '_DOWNLOAD_SLOTS', gate), \
                patch.object(api, 'resource', side_effect=api.ApiError('NETWORK_ERROR')):
            self.assertCode('NETWORK_ERROR', api.download, SID)
            gate.release.assert_called_once_with()

    def test_safe_names_and_exclusive_atomic_publication_across_threads(self):
        for raw, expected in [('CON', '_CON'), ('../x\\y', '_x_y'), ('...', 'Migu'),
                              ('COM1.txt', '_COM1.txt'), ('A:B?C', 'A_B_C')]:
            self.assertEqual(expected, api._safe_stem(raw))
        existing = self.directory / 'same.mp3'
        existing.write_bytes(b'original')
        def publish(i):
            part = self.directory / ('temp%s.part' % i)
            part.write_bytes(str(i).encode())
            try:
                return api._commit_download(part, 'same', 'mp3')
            finally:
                part.unlink()
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            names = list(pool.map(publish, range(20)))
        self.assertEqual(20, len(set(names)))
        self.assertEqual(b'original', existing.read_bytes())
        self.assertEqual({str(i).encode() for i in range(20)},
                         {(self.directory / name).read_bytes() for name in names})
        self.assertFalse(list(self.directory.glob('*.part')))

    def test_atomic_publish_failure_and_disk_errors_clean_temporary_files(self):
        for operation in ('link', 'fsync'):
            self.transport(FakeResponse(media()))
            with patch.object(api.os, operation, side_effect=OSError(SECRET)):
                self.assertCode('STORAGE_ERROR', api._stream, URL, SIZE, 'x', 'mp3', 213, 'ffmpeg', status=500)
            self.assertClean()
        self.transport(FakeResponse(media()))
        with patch.object(api.tempfile, 'NamedTemporaryFile', side_effect=OSError(SECRET)):
            self.assertCode('STORAGE_ERROR', api._stream, URL, SIZE, 'x', 'mp3', 213, 'ffmpeg')
        self.assertClean()


class GatewayTransportTests(unittest.TestCase):
    """Exercise the real credential-free transport with sockets kept offline."""
    def test_exact_opaque_query_no_credentials_and_no_debug_logs(self):
        url = 'https://dlsdownfree.nf.migu.cn/wlansst?pars=%61%2b%2f%3d+A&x=1&x=2'
        rows = dns_rows('93.184.216.34')
        connection = Mock()
        response = Mock()
        connection.getresponse.return_value = response
        with patch.object(api, '_PinnedHTTPS', return_value=connection) as constructor, \
                self.assertNoLogs('urllib3', level='DEBUG'):
            with api._GatewaySession() as session:
                self.assertIs(response, session.get(url, rows, time.monotonic() + 30))
            constructor.assert_called_once()
        args, kwargs = connection.request.call_args
        self.assertEqual(('GET', '/wlansst?pars=%61%2b%2f%3d+A&x=1&x=2'), args)
        self.assertEqual({'User-Agent': 'Mozilla/5.0', 'Accept-Encoding': 'identity',
                          'Connection': 'close'}, kwargs['headers'])
        connection.close.assert_called_once()

    def test_connect_uses_pinned_dns_and_tls_official_hostname(self):
        connection = Mock()
        tls = Mock()
        tls.wrap_socket.return_value = connection
        with patch.object(api.ssl, 'create_default_context', return_value=tls), \
                patch.object(api.socket, 'socket', return_value=connection), \
                patch.object(api.socket, 'getaddrinfo', side_effect=AssertionError('DNS re-resolution forbidden')):
            https = api._PinnedHTTPS('dlsdownfree.nf.migu.cn', dns_rows('93.184.216.34'),
                                    time.monotonic() + 30)
            https.connect()
            self.assertEqual(0, https.debuglevel)
            connection.connect.assert_called_once_with(('93.184.216.34', 443))
            tls.wrap_socket.assert_called_once_with(connection, server_hostname='dlsdownfree.nf.migu.cn')
            https.close()

    def test_deadline_timer_interrupts_socket_and_read1_is_bounded(self):
        connection = Mock()
        response = Mock()
        response.read1.side_effect = [b'ID3x', b'']
        with patch.object(api, '_PinnedHTTPS', return_value=connection), \
                patch.object(api.threading, 'Timer') as timer:
            with api._GatewaySession() as session:
                session.get(HTTPS_URL, dns_rows('93.184.216.34'), time.monotonic() + 30)
                self.assertEqual([b'ID3x'], list(session.chunks(response, time.monotonic() + 30)))
                timer.call_args.args[1]()
                connection.sock.shutdown.assert_called_once_with(socket.SHUT_RDWR)
            timer.return_value.cancel.assert_called_once()
        self.assertTrue(all(call.args == (64 * 1024,) for call in response.read1.call_args_list))

    def test_transport_errors_hide_signed_url_and_close(self):
        for exception in (OSError(SECRET), api.http.client.HTTPException(SECRET)):
            connection = Mock()
            connection.request.side_effect = exception
            with patch.object(api, '_PinnedHTTPS', return_value=connection):
                with self.assertRaises(api.ApiError) as caught:
                    with api._GatewaySession() as session:
                        session.get(HTTPS_URL, dns_rows('93.184.216.34'), time.monotonic() + 30)
                self.assertEqual('NETWORK_ERROR', caught.exception.code)
                self.assertNotIn(SECRET, str(caught.exception))
                connection.close.assert_called_once()


class RealSyntheticDecodeTests(unittest.TestCase):
    """Exercise the packaged FFmpeg with generated silence, not copyrighted media."""
    def test_full_mp3_and_flac_decode_and_short_snippet_rejected(self):
        ffmpeg = api.paths.find_exe('ffmpeg.exe')
        if not ffmpeg:
            self.skipTest('FFmpeg not installed')
        with tempfile.TemporaryDirectory() as directory:
            for extension in ('mp3', 'flac'):
                part = Path(directory) / ('synthetic.' + extension)
                result = subprocess.run([ffmpeg, '-nostdin', '-v', 'error', '-f', 'lavfi',
                                         '-i', 'anullsrc=r=44100:cl=stereo', '-t', '12',
                                         str(part)], capture_output=True, timeout=20)
                self.assertEqual(0, result.returncode)
                api._validate_media(part, extension, 12, time.monotonic() + 20, ffmpeg)
                with self.assertRaises(api.ApiError) as caught:
                    api._validate_media(part, extension, 213, time.monotonic() + 20, ffmpeg)
                self.assertEqual('DOWNLOAD_INVALID', caught.exception.code)


if __name__ == '__main__':
    unittest.main()
