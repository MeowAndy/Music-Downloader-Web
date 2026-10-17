# -*- coding: utf-8 -*-
"""Installer tests use tiny fixtures/mocks: never download AI or launch start.exe."""
import hashlib
import json
import re
import time
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import requests
import vocal_api as vocal


class Response:
    def __init__(self, data=b'', status=200, headers=None, chunks=None, payload=None):
        self.status_code = status
        self.headers = headers or {}
        self.data = data
        self.chunks = chunks
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError('https://secret.invalid/?token=secret-token')

    def iter_content(self, size):
        for item in self.chunks if self.chunks is not None else [self.data]:
            if isinstance(item, Exception):
                raise item
            yield item

    def json(self):
        return self.payload


class RangeSession:
    """Serves one byte string over Range requests, like the release CDN."""

    def __init__(self, content, ignore_range=False, bad_content_range=False):
        self.content = content
        self.ignore_range = ignore_range
        self.bad_content_range = bad_content_range
        self.calls = []

    def get(self, url, stream=True, headers=None, timeout=None):
        requested = (headers or {}).get('Range')
        self.calls.append(requested)
        if self.ignore_range or not requested:
            return Response(self.content, 200, {'Content-Length': str(len(self.content))})
        match = re.fullmatch(r'bytes=(\d+)-(\d*)', requested)
        start = int(match.group(1))
        end = int(match.group(2)) if match.group(2) else len(self.content) - 1
        body = self.content[start:end + 1]
        first = 0 if self.bad_content_range else start
        return Response(body, 206, {
            'Content-Range': 'bytes %d-%d/%d' % (first, end, len(self.content)),
            'Content-Length': str(len(body))})


class FlakyRangeSession(RangeSession):
    """Truncates one segment once, like a connection dropped mid-segment."""

    def __init__(self, content, short_once_start):
        super().__init__(content)
        self.short_once_start = short_once_start
        self.shortened = False

    def get(self, url, stream=True, headers=None, timeout=None):
        requested = (headers or {}).get('Range')
        if (not self.shortened and requested
                and requested.startswith('bytes=%d-' % self.short_once_start)):
            self.shortened = True
            self.calls.append(requested)
            match = re.fullmatch(r'bytes=(\d+)-(\d*)', requested)
            start = int(match.group(1))
            end = int(match.group(2)) if match.group(2) else len(self.content) - 1
            # Matching Content-Range, but a short body and no Content-Length.
            return Response(self.content[start:start + 1], 206, {
                'Content-Range': 'bytes %d-%d/%d' % (start, end, len(self.content))})
        return super().get(url, stream=stream, headers=headers, timeout=timeout)


class SourceSession:
    """Serves per-source probe bodies; unknown sources fail like a blocked host."""

    def __init__(self, mapping):
        self.mapping = mapping
        self.calls = []

    def get(self, url, stream=True, headers=None, timeout=None):
        self.calls.append(url)
        for name, spec in self.mapping.items():
            if name in url:
                body, delay = spec[0], spec[1]
                code = spec[2] if len(spec) > 2 else 206
                time.sleep(delay)
                headers_out = {'Content-Length': str(len(body))}
                if code == 206:
                    headers_out['Content-Range'] = 'bytes 0-%d/%d' % (len(body) - 1, len(body))
                return Response(body, code, headers_out)
        raise requests.ConnectTimeout('unreachable')


class RecordingState(dict):
    """Records published progress to prove it never moves backwards."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.history = []

    def __setitem__(self, key, value):
        if key == 'downloaded_bytes':
            self.history.append(value)
        super().__setitem__(key, value)


class VocalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        for name, value in [('BASE_DIR', self.base), ('VOCAL_TOOL_DIR', None),
                            ('VOCAL_EXE', None), ('_engine_process', None),
                            ('_setup_thread', None), ('_state_lock', threading.RLock()),
                            ('_engine_lock', threading.Lock()),
                            ('setup_state', dict(downloading=False, progress=0, stage='',
                                                 error='', error_code='', downloaded_bytes=0,
                                                 total_bytes=vocal.VOCAL_RELEASE_SIZE, action=''))]:
            patcher = mock.patch.object(vocal, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        # Fail closed if a test accidentally reaches a real HTTP request/process.
        patcher = mock.patch.object(requests.Session, 'get', side_effect=AssertionError('real network forbidden'))
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(vocal.subprocess, 'Popen', side_effect=AssertionError('real engine forbidden'))
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(vocal, '_port_in_use', return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        # Downloads default to the official source; source selection has its own tests.
        self.real_ordered_sources = vocal._ordered_sources
        patcher = mock.patch.object(vocal, '_ordered_sources',
                                    side_effect=lambda session: vocal._sources()[:1])
        patcher.start()
        self.addCleanup(patcher.stop)

    def tool(self):
        tool = self.base / 'vocal-separate' / 'outer' / 'inner'
        tool.mkdir(parents=True)
        (tool / 'start.exe').write_bytes(b'test-only')
        vocal._refresh_tool()
        return tool

    def download(self, responses, expected=8):
        session = mock.Mock()
        session.get.side_effect = responses
        with mock.patch.object(vocal, '_validate_archive') as validate, \
                mock.patch.object(vocal.time, 'sleep'):
            archive = vocal._download_archive(session, expected)
        return archive, session, validate

    def test_range_ignored_restarts_instead_of_appending(self):
        (self.base / 'vocal-separate.7z.part').write_bytes(b'old')
        archive, session, _ = self.download([Response(b'abcdefgh', headers={'Content-Length': '8'})])
        self.assertEqual(archive.read_bytes(), b'abcdefgh')
        self.assertEqual(session.get.call_args.kwargs['headers']['Range'], 'bytes=3-')
        self.assertEqual(session.get.call_args.kwargs['timeout'], (10, 30))
        self.assertTrue(session.get.call_args.kwargs['stream'])
        self.assertFalse((self.base / 'vocal-separate.7z.part').exists())

    def test_truncated_stream_resumes_valid_range(self):
        archive, session, _ = self.download([
            Response(b'abc', headers={'Content-Length': '8'}),
            Response(b'defgh', 206, {'Content-Length': '5', 'Content-Range': 'bytes 3-7/8'})])
        self.assertEqual(archive.read_bytes(), b'abcdefgh')
        self.assertEqual(session.get.call_args_list[1].kwargs['headers']['Range'], 'bytes=3-')
        self.assertEqual(vocal.setup_status()['downloaded_bytes'], 8)
        self.assertEqual(vocal.setup_status()['total_bytes'], 8)

    def test_network_retry_retains_received_bytes(self):
        secret = 'https://user:password@proxy.invalid/?token=secret-token'
        archive, session, _ = self.download([
            requests.ConnectTimeout(secret),
            Response(chunks=[b'abc', requests.ConnectionError(secret)]),
            Response(b'defgh', 206, {'Content-Range': 'bytes 3-7/8'})])
        self.assertEqual(archive.read_bytes(), b'abcdefgh')
        self.assertEqual(session.get.call_count, 3)
        text = (self.base / 'AI-install.log').read_text(encoding='utf-8')
        self.assertNotIn(secret, text)
        self.assertNotIn('password', text)
        self.assertNotIn('secret-token', text)
        self.assertIn('network', text)

    def test_bad_content_ranges_never_append_or_publish(self):
        for content_range in ('bytes 0-4/8', 'bytes 3-7/9', 'bytes 3-8/8', 'nonsense', ''):
            with self.subTest(content_range=content_range):
                part = self.base / 'vocal-separate.7z.part'
                part.write_bytes(b'abc')
                session = mock.Mock()
                session.get.return_value = Response(b'defgh', 206, {'Content-Range': content_range})
                with mock.patch.object(vocal.time, 'sleep'), self.assertRaises(vocal.InstallerError):
                    vocal._download_archive(session, 8)
                self.assertEqual(part.read_bytes(), b'abc')
                self.assertFalse((self.base / 'vocal-separate.7z').exists())
                self.assertEqual(session.get.call_count, vocal.DOWNLOAD_ATTEMPTS)

    def test_permanent_truncation_keeps_part_without_publishing(self):
        session = mock.Mock()
        session.get.return_value = Response(b'abc')  # ignores each resume request
        with mock.patch.object(vocal.time, 'sleep'), self.assertRaises(vocal.InstallerError):
            vocal._download_archive(session, 8)
        self.assertEqual((self.base / 'vocal-separate.7z.part').read_bytes(), b'abc')
        self.assertFalse((self.base / 'vocal-separate.7z').exists())

    def test_oversized_response_does_not_write_excess_bytes(self):
        session = mock.Mock()
        session.get.return_value = Response(b'123456789')
        with mock.patch.object(vocal.time, 'sleep'), self.assertRaises(vocal.InstallerError):
            vocal._download_archive(session, 8)
        self.assertEqual((self.base / 'vocal-separate.7z.part').stat().st_size, 0)

    def test_existing_official_archive_size_and_validity(self):
        archive = self.base / 'vocal-separate.7z'
        archive.write_bytes(b'abcdefgh')
        session = mock.Mock()
        with mock.patch.object(vocal, '_validate_archive') as validate:
            self.assertEqual(vocal._download_archive(session, 8), archive)
        validate.assert_called_once_with(archive)
        session.get.assert_not_called()
        with mock.patch.object(vocal, '_validate_archive', side_effect=vocal.InstallerError('archive')):
            with self.assertRaises(vocal.InstallerError):
                vocal._download_archive(session, 8)
        self.assertEqual(archive.read_bytes(), b'abcdefgh')
        archive.write_bytes(b'too large')
        with self.assertRaises(vocal.InstallerError):
            vocal._download_archive(session, 8)
        session.get.assert_not_called()

    def test_complete_part_validated_before_atomic_publish(self):
        part = self.base / 'vocal-separate.7z.part'
        part.write_bytes(b'abcdefgh')
        archive, session, validate = self.download([])
        validate.assert_called_once_with(part)
        self.assertEqual(archive.read_bytes(), b'abcdefgh')
        session.get.assert_not_called()

    def test_legacy_truncated_official_archive_resumes(self):
        (self.base / 'vocal-separate.7z').write_bytes(b'abc')
        archive, session, _ = self.download([
            Response(b'defgh', 206, {'Content-Range': 'bytes 3-7/8'})])
        self.assertEqual(archive.read_bytes(), b'abcdefgh')
        self.assertEqual(session.get.call_args.kwargs['headers']['Range'], 'bytes=3-')

    def test_metadata_size_preferred_and_fallback_exact(self):
        session = mock.Mock()
        session.get.return_value = Response(payload={'assets': [
            {'name': 'vocal-separate-v0.0.4.7z', 'size': 123}]})
        self.assertEqual(vocal._release_size(session), 123)
        self.assertEqual(session.get.call_args.kwargs['timeout'], vocal.DOWNLOAD_TIMEOUT)
        session.get.side_effect = requests.Timeout('secret-token')
        self.assertEqual(vocal._release_size(session), 785235010)

    def test_nested_discovery_and_no_outside_fallback(self):
        outside = self.base / 'developer-tool'
        outside.mkdir()
        (outside / 'start.exe').write_bytes(b'not ours')
        self.assertIsNone(vocal._find_vocal_tool())
        tool = self.tool()
        self.assertEqual(vocal._find_vocal_tool(), tool)
        self.assertEqual(vocal.VOCAL_EXE, tool / 'start.exe')

    def test_malicious_archive_members_rejected(self):
        names = ('../evil', 'a/../../evil', '/absolute', 'C:/evil', r'C:\evil',
                 r'\\server\share\evil', r'..\evil', 'file:stream', 'a//b',
                 './file', 'NUL.txt', 'dir/COM1', 'file.', 'file ', 'a\x00b')
        for name in names:
            with self.subTest(name=name):
                z = mock.Mock(files=[])
                z.list.return_value = [SimpleNamespace(filename=name, is_directory=False,
                                                       is_file=True, is_symlink=False)]
                with self.assertRaises(vocal.InstallerError) as caught:
                    vocal._validate_members(z)
                self.assertEqual(caught.exception.code, 'unsafe')
        z.list.return_value = [SimpleNamespace(filename='safe', is_directory=False,
                                               is_file=False, is_symlink=True)]
        with self.assertRaises(vocal.InstallerError):
            vocal._validate_members(z)
        z.list.return_value = [SimpleNamespace(filename=n, is_directory=False,
                                               is_file=True, is_symlink=False) for n in ('File', 'file')]
        with self.assertRaises(vocal.InstallerError):
            vocal._validate_members(z)

    def fixture(self):
        import py7zr
        archive = self.base / 'vocal-separate.7z'
        with py7zr.SevenZipFile(archive, 'w') as z:
            z.writestr(b'test fixture, not executable', 'outer/inner/start.exe')
        return archive

    def test_real_tiny_archive_staged_and_nested_install_published(self):
        archive = self.fixture()
        vocal._validate_archive(archive)
        vocal._install_archive(archive)
        self.assertEqual(vocal.VOCAL_TOOL_DIR, self.base / 'vocal-separate' / 'outer' / 'inner')
        self.assertTrue(archive.is_file())
        self.assertEqual(list(self.base.glob('.vocal-staging-*')), [])

    def test_extraction_failure_preserves_official_archive_and_cleans_staging(self):
        import py7zr
        archive = self.fixture()
        before = archive.read_bytes()
        with mock.patch.object(py7zr.SevenZipFile, 'extractall', side_effect=RuntimeError('secret-token')):
            with self.assertRaises(RuntimeError):
                vocal._install_archive(archive)
        self.assertEqual(archive.read_bytes(), before)
        self.assertFalse((self.base / 'vocal-separate').exists())
        self.assertEqual(list(self.base.glob('.vocal-staging-*')), [])

    def test_existing_install_never_overwritten(self):
        archive = self.fixture()
        tool = self.tool()
        before = (tool / 'start.exe').read_bytes()
        with self.assertRaises(vocal.InstallerError) as caught:
            vocal._install_archive(archive)
        self.assertEqual(caught.exception.code, 'target')
        self.assertEqual((tool / 'start.exe').read_bytes(), before)
        self.assertTrue(archive.exists())

    def test_target_created_during_extraction_never_overwritten(self):
        import py7zr
        archive = self.fixture()
        extract = py7zr.SevenZipFile.extractall

        def race(z, path):
            extract(z, path)
            target = self.base / 'vocal-separate'
            target.mkdir()
            (target / 'owner.txt').write_text('existing install')

        with mock.patch.object(py7zr.SevenZipFile, 'extractall', race):
            with self.assertRaises(vocal.InstallerError):
                vocal._install_archive(archive)
        self.assertEqual((self.base / 'vocal-separate' / 'owner.txt').read_text(), 'existing install')
        self.assertTrue(archive.exists())

    def test_setup_startup_failure_can_retry_without_downloading(self):
        self.tool()
        with mock.patch.object(vocal, 'running', return_value=False), \
                mock.patch.object(vocal, 'ensure_running', side_effect=vocal.InstallerError('exit')), \
                mock.patch.object(vocal, '_download_archive') as download:
            self.assertEqual(vocal.start_setup_async(), 'started')
            vocal._setup_thread.join(5)
            self.assertFalse(vocal._setup_thread.is_alive())
            status = vocal.setup_status()
            self.assertTrue(status['tool_found'])
            self.assertTrue(status['error'])
            self.assertEqual(status['error_code'], 'exit')
            self.assertFalse(status['downloading'])
            with mock.patch.object(vocal, 'ensure_running') as retry:
                self.assertEqual(vocal.start_setup_async(), 'started')
                vocal._setup_thread.join(5)
                retry.assert_called_once_with()
            download.assert_not_called()
        self.assertEqual(vocal.setup_status()['error'], '')

    def test_concurrent_setup_creates_only_one_worker(self):
        self.tool()
        gate = threading.Event()
        entered = threading.Event()

        def engine():
            entered.set()
            gate.wait(5)

        results = []
        with mock.patch.object(vocal, 'running', return_value=False), \
                mock.patch.object(vocal, 'ensure_running', side_effect=engine) as start:
            callers = [threading.Thread(target=lambda: results.append(vocal.start_setup_async()))
                       for _ in range(16)]
            try:
                for t in callers:
                    t.start()
                for t in callers:
                    t.join(5)
                self.assertTrue(entered.wait(5))
                self.assertEqual(results.count('started'), 1)
                self.assertEqual(results.count('busy'), 15)
                self.assertEqual(start.call_count, 1)
            finally:
                gate.set()
                if vocal._setup_thread:
                    vocal._setup_thread.join(5)

    def test_already_ready_action_has_no_worker(self):
        self.tool()
        with mock.patch.object(vocal, 'running', return_value=True):
            self.assertEqual(vocal.start_setup_async(), 'ready')
        self.assertIsNone(vocal._setup_thread)
        self.assertFalse(vocal.setup_status()['downloading'])

    def test_readiness_requires_vocal_identity_not_tcp_or_generic_html(self):
        session = mock.MagicMock()
        session.__enter__.return_value = session
        good = (b'<title>Vocal Separate v0.0.4</title>github.com/jianchang512/vocal-separate '
                b"url:'/upload';url:'/process';wav_name;2stems")
        with mock.patch.object(vocal, '_session', return_value=session):
            for body, expected in ((b'<title>other server</title>', False), (good, True),
                                   (good.replace(b'/process', b'/other'), False)):
                session.get.return_value = Response(body)
                self.assertEqual(vocal.running(), expected)
            session.get.return_value = Response(good, status=302)
            self.assertFalse(vocal.running())
        self.assertFalse(session.get.call_args.kwargs['allow_redirects'])

    def test_early_owned_process_exit_and_retry(self):
        self.tool()
        dead = mock.Mock()
        dead.poll.return_value = 1
        live = mock.Mock()
        live.poll.return_value = None
        with mock.patch.object(vocal, 'running', side_effect=[False, False, True]), \
                mock.patch.object(vocal, '_start_hidden', side_effect=[dead, live]) as start:
            with self.assertRaises(vocal.InstallerError) as caught:
                vocal.ensure_running()
            self.assertEqual(caught.exception.code, 'exit')
            vocal.ensure_running()
        self.assertEqual(start.call_count, 2)
        self.assertEqual(vocal.setup_status()['error'], '')

    def test_unrelated_port_is_not_started_or_stopped(self):
        self.tool()
        with mock.patch.object(vocal, 'running', return_value=False), \
                mock.patch.object(vocal, '_port_in_use', return_value=True), \
                mock.patch.object(vocal, '_start_hidden') as start:
            with self.assertRaises(vocal.InstallerError) as caught:
                vocal.ensure_running()
        self.assertEqual(caught.exception.code, 'port')
        start.assert_not_called()

    def test_live_owned_process_not_duplicated_after_timeout(self):
        self.tool()
        process = mock.Mock()
        process.poll.return_value = None
        with mock.patch.object(vocal, 'running', return_value=False), \
                mock.patch.object(vocal, 'STARTUP_TIMEOUT', 0), \
                mock.patch.object(vocal, '_start_hidden', return_value=process) as start:
            for _ in range(2):
                with self.assertRaises(vocal.InstallerError) as caught:
                    vocal.ensure_running()
                self.assertEqual(caught.exception.code, 'timeout')
        start.assert_called_once_with()

    def test_concurrent_engine_start_is_serialized(self):
        self.tool()
        process = mock.Mock()
        process.poll.return_value = None
        with mock.patch.object(vocal, 'running', side_effect=[False, True, True]), \
                mock.patch.object(vocal, '_start_hidden', return_value=process) as start:
            threads = [threading.Thread(target=vocal.ensure_running) for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(5)
                self.assertFalse(t.is_alive())
        start.assert_called_once_with()

    def test_hidden_start_returns_process_and_logs_output(self):
        self.tool()
        process = mock.Mock()
        with mock.patch.object(vocal.subprocess, 'Popen', return_value=process) as popen:
            self.assertIs(vocal._start_hidden(), process)
        kwargs = popen.call_args.kwargs
        self.assertEqual(kwargs['env']['BROWSER'], str(vocal.paths.RES_DIR / 'vendor' / 'nop.cmd'))
        self.assertEqual(kwargs['stderr'], vocal.subprocess.STDOUT)
        self.assertEqual(Path(kwargs['stdout'].name), self.base / 'AI-engine.log')
        self.assertEqual(kwargs['cwd'], str(vocal.VOCAL_TOOL_DIR))

    def test_failure_persisted_without_exception_secrets_and_restored(self):
        secret = 'https://user:password@proxy.invalid/?token=secret-token'
        vocal._failure(requests.Timeout(secret))
        state_path = self.base / 'AI-install-state.json'
        persisted = json.loads(state_path.read_text(encoding='utf-8'))
        self.assertEqual(persisted['error_code'], 'network')
        self.assertFalse(list(self.base.glob('AI-install-state.*.tmp')))
        for path in (state_path, self.base / 'AI-install.log'):
            text = path.read_text(encoding='utf-8')
            for token in ('https://', 'password', 'secret-token', 'proxy.invalid'):
                self.assertNotIn(token, text)
        vocal.setup_state['error'] = ''
        vocal._restore_state()
        self.assertEqual(vocal.setup_state['error_code'], 'network')
        state_path.write_text(json.dumps({'downloading': True, 'error': secret}), encoding='utf-8')
        vocal._restore_state()
        self.assertEqual(vocal.setup_state['error_code'], 'interrupted')
        self.assertNotIn(secret, vocal.setup_state['error'])

    def test_installer_respects_proxies_local_engine_bypasses_them(self):
        with vocal._download_session() as download, vocal._session() as local:
            self.assertTrue(download.trust_env)
            self.assertFalse(local.trust_env)

    def test_no_own_install_downloads_even_when_service_already_ready(self):
        archive = self.base / 'vocal-separate.7z'
        session = mock.MagicMock()
        session.__enter__.return_value = session
        with mock.patch.object(vocal, 'running', return_value=True), \
                mock.patch.object(vocal, '_download_session', return_value=session), \
                mock.patch.object(vocal, '_release_size', return_value=8), \
                mock.patch.object(vocal, '_download_archive', return_value=archive) as download, \
                mock.patch.object(vocal, '_install_archive', side_effect=lambda path: self.tool()) as install, \
                mock.patch.object(vocal, 'ensure_running') as engine:
            self.assertEqual(vocal.start_setup_async(), 'started')
            vocal._setup_thread.join(5)
            self.assertFalse(vocal._setup_thread.is_alive())
        download.assert_called_once_with(session, 8)
        install.assert_called_once_with(archive)
        engine.assert_called_once_with()

    def test_archive_crc_verification_exposes_separate_stage(self):
        archive = self.fixture()
        vocal._validate_archive(archive)
        self.assertIn('校验', vocal.setup_status()['stage'])
        self.assertIn('archive-verify', (self.base / 'AI-install.log').read_text(encoding='utf-8'))

    def test_real_malicious_archive_rejected_before_extraction(self):
        import py7zr
        archive = self.base / 'malicious.7z'
        # Only bypass the writer's guard to construct a malicious input fixture.
        with mock.patch('py7zr.py7zr.check_archive_path', return_value=True), \
                py7zr.SevenZipFile(archive, 'w') as z:
            z.writestr(b'never extract', '../escaped.txt')
        with mock.patch.object(py7zr.SevenZipFile, 'extractall') as extract:
            with self.assertRaises(vocal.InstallerError) as caught:
                vocal._install_archive(archive)
        self.assertEqual(caught.exception.code, 'unsafe')
        extract.assert_not_called()
        self.assertTrue(archive.exists())
        self.assertFalse((self.base / 'escaped.txt').exists())
        self.assertEqual(list(self.base.glob('.vocal-staging-*')), [])

    def test_auto_start_failure_is_observable(self):
        self.tool()
        with mock.patch.object(vocal, 'start_setup_async', side_effect=RuntimeError('secret-token')):
            vocal.auto_start_async()
        self.assertEqual(vocal.setup_status()['error_code'], 'unexpected')
        self.assertNotIn('secret-token', (self.base / 'AI-install.log').read_text(encoding='utf-8'))

    # ---------- 多连接加速下载 ----------

    def accelerated(self, session, expected=8, min_chunk=2):
        with mock.patch.object(vocal, '_connections', return_value=vocal.DOWNLOAD_CONNECTIONS), \
                mock.patch.object(vocal, 'MIN_CHUNK', min_chunk), \
                mock.patch.object(vocal, '_validate_archive') as validate, \
                mock.patch.object(vocal.time, 'sleep'):
            archive = vocal._download_archive(session, expected)
        return archive, validate

    def test_parallel_segments_download_and_assemble(self):
        session = RangeSession(b'abcdefgh')
        archive, validate = self.accelerated(session)
        self.assertEqual(archive.read_bytes(), b'abcdefgh')
        self.assertEqual(sorted(session.calls),
                         ['bytes=0-1', 'bytes=2-3', 'bytes=4-5', 'bytes=6-7'])
        validate.assert_called_once_with(self.base / 'vocal-separate.7z.part')
        self.assertFalse((self.base / 'vocal-separate.7z.part').exists())
        self.assertEqual(list(self.base.glob('*.chunk-*')), [])
        log = (self.base / 'AI-install.log').read_text(encoding='utf-8')
        self.assertIn('download-parallel-4', log)
        self.assertEqual(vocal.setup_status()['downloaded_bytes'], 8)
        self.assertEqual(vocal.setup_status()['progress'], 100)

    def test_parallel_resume_keeps_existing_prefix(self):
        (self.base / 'vocal-separate.7z.part').write_bytes(b'abc')
        session = RangeSession(b'abcdefgh')
        archive, _ = self.accelerated(session)
        self.assertEqual(archive.read_bytes(), b'abcdefgh')
        self.assertEqual(sorted(session.calls), ['bytes=3-4', 'bytes=5-7'])

    def test_parallel_reuses_completed_segment_files(self):
        (self.base / 'vocal-separate.7z.part').write_bytes(b'abc')
        (self.base / 'vocal-separate.7z.part.chunk-3').write_bytes(b'de')
        session = RangeSession(b'abcdefgh')
        archive, _ = self.accelerated(session)
        self.assertEqual(archive.read_bytes(), b'abcdefgh')
        self.assertEqual(session.calls, ['bytes=5-7'])

    def test_parallel_resumes_partial_segment(self):
        (self.base / 'vocal-separate.7z.part').write_bytes(b'abc')
        (self.base / 'vocal-separate.7z.part.chunk-3').write_bytes(b'd')  # 1 of 2 bytes
        session = RangeSession(b'abcdefgh')
        archive, _ = self.accelerated(session)
        self.assertEqual(archive.read_bytes(), b'abcdefgh')
        self.assertEqual(sorted(session.calls), ['bytes=4-4', 'bytes=5-7'])

    def test_parallel_discards_segments_already_inside_part(self):
        # A crash between appending and deleting a segment must never append it twice.
        (self.base / 'vocal-separate.7z.part').write_bytes(b'abcde')
        (self.base / 'vocal-separate.7z.part.chunk-3').write_bytes(b'de')
        session = RangeSession(b'abcdefgh')
        archive, _ = self.accelerated(session, min_chunk=1)
        self.assertEqual(archive.read_bytes(), b'abcdefgh')
        self.assertEqual(sorted(session.calls), ['bytes=5-5', 'bytes=6-6', 'bytes=7-7'])
        self.assertFalse((self.base / 'vocal-separate.7z.part.chunk-3').exists())

    def test_parallel_falls_back_when_range_is_ignored(self):
        session = RangeSession(b'abcdefgh', ignore_range=True)
        archive, _ = self.accelerated(session)
        self.assertEqual(archive.read_bytes(), b'abcdefgh')
        log = (self.base / 'AI-install.log').read_text(encoding='utf-8')
        self.assertIn('download-parallel-4', log)  # attempted, then detected
        self.assertIn('download-range-unsupported', log)

    def test_parallel_bad_content_range_never_publishes_or_creates_segments(self):
        (self.base / 'vocal-separate.7z.part').write_bytes(b'abc')
        session = RangeSession(b'abcdefgh', bad_content_range=True)
        with mock.patch.object(vocal, '_connections', return_value=vocal.DOWNLOAD_CONNECTIONS), \
                mock.patch.object(vocal, 'MIN_CHUNK', 2), \
                mock.patch.object(vocal.time, 'sleep'), self.assertRaises(vocal.InstallerError):
            vocal._download_archive(session, 8)
        self.assertEqual((self.base / 'vocal-separate.7z.part').read_bytes(), b'abc')
        self.assertFalse((self.base / 'vocal-separate.7z').exists())
        self.assertEqual(list(self.base.glob('*.chunk-*')), [])

    def test_small_remainder_stays_on_single_connection(self):
        (self.base / 'vocal-separate.7z.part').write_bytes(b'abc')
        session = RangeSession(b'abcdefgh')
        archive, _ = self.accelerated(session, min_chunk=vocal.MIN_CHUNK)
        self.assertEqual(archive.read_bytes(), b'abcdefgh')
        self.assertEqual(session.calls, ['bytes=3-'])
        self.assertNotIn('download-parallel-', (self.base / 'AI-install.log').read_text(encoding='utf-8'))

    def test_parallel_progress_never_moves_backwards_after_truncated_segment(self):
        state = RecordingState(downloading=False, progress=0, stage='', error='', error_code='',
                               downloaded_bytes=0, total_bytes=vocal.VOCAL_RELEASE_SIZE, action='')
        session = FlakyRangeSession(b'abcdefgh', short_once_start=2)
        with mock.patch.object(vocal, 'setup_state', state):
            archive, _ = self.accelerated(session)
        self.assertEqual(archive.read_bytes(), b'abcdefgh')
        self.assertEqual(state['downloaded_bytes'], 8)
        self.assertEqual(state.history, sorted(state.history))
        self.assertIn('bytes=2-3', session.calls)   # first try, truncated
        self.assertIn('bytes=3-3', session.calls)   # resumed from the segment file

    def test_connections_override_is_clamped(self):
        with mock.patch.dict(vocal.os.environ, {vocal.THREADS_ENV: '3'}):
            self.assertEqual(vocal._connections(), 3)
        with mock.patch.dict(vocal.os.environ, {vocal.THREADS_ENV: '99'}):
            self.assertEqual(vocal._connections(), 16)
        with mock.patch.dict(vocal.os.environ, {vocal.THREADS_ENV: 'abc'}):
            self.assertEqual(vocal._connections(), vocal.DOWNLOAD_CONNECTIONS)

    # ---------- 无代理时的下载源选择与校验 ----------

    def test_sources_include_official_then_mirrors_and_env_override(self):
        with mock.patch.dict(vocal.os.environ, {}, clear=False):
            vocal.os.environ.pop(vocal.MIRROR_ENV, None)
            entries = vocal._sources()
        self.assertEqual(entries[0]['name'], 'official')
        self.assertFalse(entries[0]['mirror'])
        self.assertEqual(entries[0]['url'], vocal.VOCAL_RELEASE_URL)
        self.assertIn('ghproxy.net', [item['name'] for item in entries[1:]])
        self.assertTrue(all(item['mirror'] for item in entries[1:]))
        self.assertTrue(all(item['url'].endswith(vocal.VOCAL_RELEASE_URL) for item in entries[1:]))
        with mock.patch.dict(vocal.os.environ, {vocal.MIRROR_ENV: 'https://mirror.example/{url}'}):
            custom = vocal._sources()[1]
        self.assertEqual(custom['name'], 'custom')
        self.assertEqual(custom['url'], 'https://mirror.example/' + vocal.VOCAL_RELEASE_URL)
        with mock.patch.dict(vocal.os.environ, {vocal.MIRROR_ENV: 'http://127.0.0.1:9/file.7z'}):
            self.assertEqual(vocal._sources()[1]['url'], 'http://127.0.0.1:9/file.7z')

    def test_probe_measures_rate_and_range_support(self):
        session = SourceSession({'example.invalid': (b'x' * vocal.PROBE_BYTES, 0.01)})
        rate, ranged = vocal._probe(session, 'https://example.invalid/file.7z')
        self.assertGreater(rate, 0)
        self.assertTrue(ranged)
        blocked = mock.Mock()
        blocked.get.side_effect = requests.ConnectTimeout('secret-token')
        self.assertIsNone(vocal._probe(blocked, 'https://example.invalid/file.7z'))
        empty = mock.Mock()
        empty.get.return_value = Response(b'', 206, {})
        self.assertIsNone(vocal._probe(empty, 'https://example.invalid/file.7z'))

    def test_ordered_sources_prefers_fastest_reachable(self):
        candidates = [
            {'name': 'official', 'url': 'https://official.invalid/f.7z', 'mirror': False, 'ranged': None},
            {'name': 'ghproxy.net', 'url': 'https://ghproxy.net/f.7z', 'mirror': True, 'ranged': None},
            {'name': 'gh-proxy.com', 'url': 'https://gh-proxy.com/f.7z', 'mirror': True, 'ranged': None},
        ]
        session = SourceSession({'ghproxy.net': (b'f' * vocal.PROBE_BYTES, 0.01),
                                 'gh-proxy.com': (b's' * vocal.PROBE_BYTES, 0.4)})
        with mock.patch.object(vocal, '_sources', return_value=candidates):
            ranked = self.real_ordered_sources(session)
        self.assertEqual([item['name'] for item in ranked],
                         ['ghproxy.net', 'gh-proxy.com', 'official'])
        log = (self.base / 'AI-install.log').read_text(encoding='utf-8')
        self.assertIn('source-picked:ghproxy.net', log)
        self.assertIn('source-unreachable:official', log)

    def test_ordered_sources_skips_mirror_probe_when_official_is_fast(self):
        candidates = [
            {'name': 'official', 'url': 'https://official.invalid/f.7z', 'mirror': False, 'ranged': None},
            {'name': 'ghproxy.net', 'url': 'https://ghproxy.net/f.7z', 'mirror': True, 'ranged': None},
        ]
        session = SourceSession({'official.invalid': (b'f' * vocal.PROBE_BYTES, 0.001)})
        with mock.patch.object(vocal, '_sources', return_value=candidates):
            ranked = self.real_ordered_sources(session)
        self.assertEqual([item['name'] for item in ranked], ['official', 'ghproxy.net'])
        self.assertEqual(len(session.calls), 1)  # 官方够快就不再探测镜像
        self.assertIn('source-official-fast',
                      (self.base / 'AI-install.log').read_text(encoding='utf-8'))

    def test_range_capable_source_beats_slightly_faster_mirror(self):
        body = b'f' * vocal.PROBE_BYTES
        candidates = [
            {'name': 'official', 'url': 'https://official.invalid/f.7z', 'mirror': False, 'ranged': None},
            {'name': 'ghproxy.net', 'url': 'https://ghproxy.net/f.7z', 'mirror': True, 'ranged': None},
        ]
        # 官方约1MB/s且支持Range（可多连接放大），镜像约2MB/s但只返回200
        session = SourceSession({'official.invalid': (body, 0.5),
                                 'ghproxy.net': (body, 0.25, 200)})
        with mock.patch.object(vocal, '_sources', return_value=candidates):
            ranked = self.real_ordered_sources(session)
        self.assertEqual(ranked[0]['name'], 'official')
        self.assertTrue(ranked[0]['ranged'])
        self.assertFalse(ranked[1]['ranged'])

    def test_much_faster_mirror_still_wins(self):
        body = b'f' * vocal.PROBE_BYTES
        candidates = [
            {'name': 'official', 'url': 'https://official.invalid/f.7z', 'mirror': False, 'ranged': None},
            {'name': 'ghproxy.net', 'url': 'https://ghproxy.net/f.7z', 'mirror': True, 'ranged': None},
        ]
        # 官方约1MB/s，镜像约10MB/s：明显更快时仍选镜像
        session = SourceSession({'official.invalid': (body, 0.5),
                                 'ghproxy.net': (body, 0.05, 200)})
        with mock.patch.object(vocal, '_sources', return_value=candidates):
            ranked = self.real_ordered_sources(session)
        self.assertEqual(ranked[0]['name'], 'ghproxy.net')

    def test_mirror_content_mismatch_is_discarded_and_source_rotated(self):
        sources = [
            {'name': 'ghproxy.net', 'url': 'https://ghproxy.net/f.7z', 'mirror': True, 'ranged': True},
            {'name': 'official', 'url': 'https://official.invalid/f.7z', 'mirror': False, 'ranged': True},
        ]
        session = RangeSession(b'abcdefgh')
        with mock.patch.object(vocal, '_ordered_sources', return_value=sources), \
                mock.patch.object(vocal, 'MIN_CHUNK', 2), \
                mock.patch.object(vocal, '_validate_archive') as validate, \
                mock.patch.object(vocal.time, 'sleep'):
            archive = vocal._download_archive(session, 8)
        self.assertEqual(archive.read_bytes(), b'abcdefgh')
        validate.assert_called_once()
        self.assertEqual(session.calls.count('bytes=0-1'), 2)  # 镜像字节被丢弃后从零重下
        self.assertEqual(list(self.base.glob('*.chunk-*')), [])
        log = (self.base / 'AI-install.log').read_text(encoding='utf-8')
        self.assertIn('download-source:ghproxy.net', log)
        self.assertIn('integrity-mismatch:ghproxy.net', log)
        self.assertIn('download-source:official', log)

    def test_official_hash_change_is_a_warning_not_a_failure(self):
        session = RangeSession(b'abcdefgh')
        archive, _ = self.accelerated(session)
        self.assertEqual(archive.read_bytes(), b'abcdefgh')
        self.assertIn('integrity-official-changed',
                      (self.base / 'AI-install.log').read_text(encoding='utf-8'))

    def test_matching_official_hash_is_logged(self):
        digest = hashlib.sha256(b'abcdefgh').hexdigest()
        session = RangeSession(b'abcdefgh')
        with mock.patch.object(vocal, 'VOCAL_RELEASE_SHA256', digest):
            archive, _ = self.accelerated(session)
        self.assertEqual(archive.read_bytes(), b'abcdefgh')
        log = (self.base / 'AI-install.log').read_text(encoding='utf-8')
        self.assertIn('integrity-match', log)
        self.assertNotIn('integrity-official-changed', log)


if __name__ == '__main__':
    unittest.main()
