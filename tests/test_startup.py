# -*- coding: utf-8 -*-
"""Offline startup tests: no app import, real cookie reads, tools or HTTP calls."""
import importlib
import io
import json
import os
import socket
import subprocess
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import requests
import startup


class StartupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.resource = self.root / 'resources'
        self.data = self.root / 'data'
        self.output = self.data / 'output'
        for directory in (self.resource, self.data, self.output):
            directory.mkdir(parents=True, exist_ok=True)
        self.paths = types.SimpleNamespace(RES_DIR=self.resource, APP_DIR=self.data,
                                           OUTPUT_DIR=self.output)

    def fixture_files(self):
        for name in (*startup.REQUIRED_RESOURCES, 'bin/node.exe', 'bin/ffmpeg.exe'):
            path = self.resource / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'test fixture')

    def diag(self, qq=None, network=True):
        with mock.patch.object(startup, 'check_imports', return_value=[]), \
                mock.patch.object(startup, 'check_tool', side_effect=lambda p, t: startup.row('tool:' + t, 'ok', 'mocked')):
            return startup.diagnostics(self.paths, qq, network=network)

    def test_import_is_safe(self):
        # Reload with all side-effect entry points trapped; no paths/qq_api import.
        with mock.patch.object(importlib, 'import_module', side_effect=AssertionError('import side effect')), \
                mock.patch.object(subprocess, 'run', side_effect=AssertionError('tool side effect')), \
                mock.patch.object(startup.threading.Thread, 'start', side_effect=AssertionError('thread side effect')), \
                mock.patch.object(startup.webbrowser, 'open', side_effect=AssertionError('browser side effect')), \
                mock.patch.object(tempfile, 'NamedTemporaryFile', side_effect=AssertionError('file side effect')):
            importlib.reload(startup)
        self.assertEqual(startup.BUILD_ID, '1.3.0-desktop')

    def test_import_checks_all_dependencies_and_redacts_errors(self):
        def load(name):
            if name == 'paho.mqtt.client':
                raise ImportError('Cookie=SECRET')
            return mock.Mock()
        with mock.patch.object(startup.importlib, 'import_module', side_effect=load) as loader:
            rows = startup.check_imports()
        self.assertEqual([call.args[0] for call in loader.call_args_list], list(startup.DEPENDENCIES))
        self.assertEqual([r['status'] for r in rows],
                         ['fail' if name == 'paho.mqtt.client' else 'ok'
                          for name in startup.DEPENDENCIES])
        self.assertNotIn('SECRET', json.dumps(rows))

    def test_missing_resources_fail(self):
        rows = self.diag(network=False)
        resources = [r for r in rows if r['name'].startswith('resource:')]
        self.assertEqual(len(resources), len(startup.REQUIRED_RESOURCES))
        self.assertTrue(all(r['status'] == 'fail' for r in resources))

    def test_present_resources_and_writable_directories(self):
        self.fixture_files()
        rows = self.diag(network=False)
        for name in ('resource:static/index.html', 'resource:vendor/node_bridge.js',
                     'data:writable', 'output:writable'):
            self.assertEqual(next(r['status'] for r in rows if r['name'] == name), 'ok')
        self.assertEqual(list(self.output.iterdir()), [])

    def test_missing_login_is_warning_no_read_no_import_no_search(self):
        qq = mock.Mock()
        with mock.patch.object(Path, 'read_text', side_effect=AssertionError('cookie read')):
            rows = self.diag(qq)
        qq.search.assert_not_called()
        login = next(r for r in rows if r['name'] == 'login')
        self.assertEqual(login['status'], 'warn')
        self.assertIn('扫码登录', login['detail'])
        self.assertFalse(any(r['name'] == 'network:search' for r in rows))

    def test_empty_cookie_file_does_not_trigger_search(self):
        (self.data / 'qq_cookie.txt').touch()
        qq = mock.Mock()
        rows = self.diag(qq)
        qq.search.assert_not_called()
        self.assertEqual(next(r['status'] for r in rows if r['name'] == 'login'), 'warn')

    def test_search_uses_authenticated_interface_without_reading_cookie(self):
        (self.data / 'qq_cookie.txt').write_bytes(b'SECRET')
        qq = mock.Mock()
        qq.search.return_value = [{'mid': 'song', 'name': 'SECRET SONG CONTENT'}]
        with mock.patch.object(Path, 'read_text', side_effect=AssertionError('cookie read')):
            rows = self.diag(qq)
        qq.search.assert_called_once_with('周深', num=3)
        qq.get_playlist.assert_not_called()
        self.assertEqual(next(r['status'] for r in rows if r['name'] == 'network:search'), 'ok')
        self.assertNotIn('SECRET', json.dumps(rows))

    def test_search_empty_malformed_and_exception_are_not_passed(self):
        (self.data / 'qq_cookie.txt').write_bytes(b'SECRET')
        for value in ([], {}, [{'name': 'no mid'}], RuntimeError('Cookie=SECRET')):
            with self.subTest(value=type(value).__name__):
                qq = mock.Mock()
                if isinstance(value, Exception):
                    qq.search.side_effect = value
                else:
                    qq.search.return_value = value
                rows = self.diag(qq)
                self.assertEqual(next(r['status'] for r in rows if r['name'] == 'network:search'), 'fail')
                self.assertNotIn('SECRET', json.dumps(rows))

    def test_network_disabled_is_explicit_warning(self):
        (self.data / 'qq_cookie.txt').write_bytes(b'SECRET')
        qq = mock.Mock()
        rows = self.diag(qq, network=False)
        qq.search.assert_not_called()
        self.assertEqual(next(r['status'] for r in rows if r['name'] == 'network:search'), 'warn')

    def test_unique_probe_preserves_existing_files(self):
        sentinel = self.data / '.startup-probe-existing'
        sentinel.write_bytes(b'keep me')
        for _ in range(2):
            self.assertEqual(startup.check_writable(self.data)['status'], 'ok')
        self.assertEqual(sentinel.read_bytes(), b'keep me')
        self.assertEqual(sorted(p.name for p in self.data.iterdir()), [sentinel.name, 'output'])

    def test_probe_failure_and_cleanup_failure_are_failures(self):
        self.assertEqual(startup.check_writable(self.root / 'absent')['status'], 'fail')
        with mock.patch.object(startup.tempfile, 'NamedTemporaryFile', side_effect=PermissionError('SECRET')):
            result = startup.check_writable(self.data)
        self.assertEqual(result['status'], 'fail')
        self.assertNotIn('SECRET', result['detail'])
        context = mock.MagicMock()
        context.__exit__.side_effect = PermissionError('SECRET')
        with mock.patch.object(startup.tempfile, 'NamedTemporaryFile', return_value=context), \
                mock.patch.object(startup.os, 'fsync'):
            self.assertEqual(startup.check_writable(self.data)['status'], 'fail')

    def tool_result(self, result=None, exception=None):
        path = self.resource / 'node.exe'
        path.touch()
        with mock.patch.object(startup.subprocess, 'run', return_value=result, side_effect=exception) as run:
            row = startup.check_tool(path, 'node', timeout=4)
        return row, run

    def test_missing_tool_does_not_use_path_or_execute(self):
        with mock.patch.object(startup.subprocess, 'run') as run:
            result = startup.check_tool(self.root / 'missing.exe', 'node')
        self.assertEqual(result['status'], 'fail')
        run.assert_not_called()

    def test_tool_version_and_windows_no_window(self):
        path = self.resource / 'node.exe'
        path.touch()
        with mock.patch.object(startup.os, 'name', 'nt'), \
                mock.patch.object(startup.subprocess, 'run', return_value=types.SimpleNamespace(
                    returncode=0, stdout=b'v22.14.0\n')) as run:
            result = startup.check_tool(path, 'node', timeout=4)
        self.assertEqual(result['status'], 'ok')
        self.assertIn('22.14.0', result['detail'])
        self.assertEqual(run.call_args.args[0], [str(path), '--version'])
        self.assertEqual(run.call_args.kwargs['timeout'], 4)
        self.assertEqual(run.call_args.kwargs['creationflags'], 0x08000000)

    def test_ffmpeg_version_command_and_safe_output(self):
        path = self.resource / 'ffmpeg.exe'
        path.touch()
        with mock.patch.object(startup.subprocess, 'run', return_value=types.SimpleNamespace(
                returncode=0, stdout=b'ffmpeg version 7.1-full_build Copyright\nSECRET\xff')) as run:
            result = startup.check_tool(path, 'ffmpeg')
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(run.call_args.args[0][1], '-version')
        self.assertNotIn('SECRET', result['detail'])

    def test_tool_nonzero_timeout_launch_error_and_invalid_version(self):
        cases = (
            (types.SimpleNamespace(returncode=9, stdout=b'SECRET'), None),
            (None, subprocess.TimeoutExpired(['node'], 4, output=b'SECRET')),
            (None, OSError('SECRET')),
            (types.SimpleNamespace(returncode=0, stdout=b'SECRET\xff'), None),
        )
        for result, error in cases:
            with self.subTest(error=type(error).__name__):
                row, _ = self.tool_result(result, error)
                self.assertEqual(row['status'], 'fail')
                self.assertNotIn('SECRET', row['detail'])

    def test_report_is_safe_json_and_failure_not_passed(self):
        rows = [startup.row('login', 'warn', 'Not tested.'), startup.row('tool', 'fail', 'Missing.')]
        path = startup.save_report(self.data, self.resource, rows, instance='test', url='http://127.0.0.1:12345')
        report = json.loads(path.read_text(encoding='utf-8'))
        self.assertFalse(report['ok'])
        self.assertEqual(report['build_id'], startup.BUILD_ID)
        self.assertEqual(report['data_dir'], str(self.data))
        self.assertEqual(report['resource_dir'], str(self.resource))
        self.assertFalse(list(self.data.glob('.startup-report-*')))

    def test_failed_atomic_report_preserves_old_report_and_cleans_temp(self):
        target = self.data / 'startup-report.json'
        target.write_bytes(b'previous report')
        with mock.patch.object(startup.os, 'replace', side_effect=PermissionError):
            with self.assertRaises(PermissionError):
                startup.save_report(self.data, self.resource, [])
        self.assertEqual(target.read_bytes(), b'previous report')
        self.assertFalse(list(self.data.glob('.startup-report-*')))

    def test_safe_console_handles_non_ascii_and_windowed(self):
        stream = io.TextIOWrapper(io.BytesIO(), encoding='ascii')
        with mock.patch.object(startup.sys, 'stdout', stream):
            startup.safe_print('周深')
        stream.seek(0)
        self.assertIn('\\u', stream.read())
        with mock.patch.object(startup.sys, 'stdout', None):
            startup.safe_print('windowed')

    def bind_factories(self):
        # Import before mocking os.name, which is shared with the stdlib.
        import werkzeug.serving
        return (
            ('posix', 'werkzeug.serving.make_server', {'threaded': True}),
            ('nt', 'startup._make_windows_server', {}),
        )

    def test_bind_preferred_port_synchronously_threaded(self):
        for platform, target, options in self.bind_factories():
            with self.subTest(platform=platform):
                app, server = mock.Mock(), mock.Mock()
                with mock.patch.object(startup.os, 'name', platform), \
                        mock.patch(target, return_value=server) as make:
                    self.assertIs(startup.bind_server(app), server)
                make.assert_called_once_with('127.0.0.1', 8899, app, **options)

    def test_bind_oserror_and_werkzeug_systemexit_choose_port_zero(self):
        for platform, target, options in self.bind_factories():
            for error in (OSError('occupied'), SystemExit(1)):
                with self.subTest(platform=platform, error=type(error).__name__):
                    app, server = mock.Mock(), mock.Mock()
                    with mock.patch.object(startup.os, 'name', platform), \
                            mock.patch(target, side_effect=[error, server]) as make, \
                            mock.patch.object(startup, 'safe_print'):
                        self.assertIs(startup.bind_server(app), server)
                    self.assertEqual(make.call_args_list, [
                        mock.call('127.0.0.1', 8899, app, **options),
                        mock.call('127.0.0.1', 0, app, **options)])

    def test_bind_failure_on_port_zero_is_not_retried(self):
        for platform, target, _ in self.bind_factories():
            for error in (OSError('occupied'), SystemExit(1)):
                with self.subTest(platform=platform, error=type(error).__name__):
                    with mock.patch.object(startup.os, 'name', platform), \
                            mock.patch(target, side_effect=error) as make:
                        with self.assertRaises(type(error)):
                            startup.bind_server(mock.Mock(), preferred_port=0)
                    self.assertEqual(make.call_count, 1)

    @unittest.skipUnless(os.name == 'nt', 'Windows exclusive socket binding')
    def test_windows_real_listener_collision_falls_back_to_own_exclusive_socket(self):
        from werkzeug.serving import ThreadedWSGIServer
        original_reuse = (ThreadedWSGIServer.allow_reuse_address,
                          ThreadedWSGIServer.allow_reuse_port)
        # Use ephemeral ports only: leave any actual installer on 8899 untouched.
        for reuse in (False, True):
            with self.subTest(listener_reuse_address=reuse), \
                    socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, int(reuse))
                listener.bind((startup.HOST, 0))
                listener.listen()
                listener.settimeout(2)
                occupied_port = listener.getsockname()[1]
                with mock.patch.object(startup, 'safe_print') as notice, \
                        mock.patch('werkzeug.serving.make_server',
                                   side_effect=AssertionError('Windows must not use reuse factory')):
                    server = startup.bind_server(mock.Mock(), occupied_port)
                self.addCleanup(server.server_close)
                notice.assert_called_once()
                self.assertNotEqual(server.server_port, occupied_port)
                self.assertEqual(server.server_port, server.socket.getsockname()[1])
                self.assertIsInstance(server, ThreadedWSGIServer)
                self.assertTrue(server.multithread)
                self.assertTrue(server.daemon_threads)
                self.assertFalse(server.allow_reuse_address)
                self.assertEqual(server.socket.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR), 0)
                self.assertEqual(server.socket.getsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE), 1)
                # Both sockets are already listening synchronously, on distinct ports.
                for bound in (listener, server.socket):
                    bound.settimeout(2)
                    with socket.create_connection(bound.getsockname(), timeout=2):
                        connection, _ = bound.accept()
                        connection.close()
                # Even an explicit SO_REUSEADDR client cannot overlap our socket.
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as overlap:
                    overlap.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    with self.assertRaises(OSError):
                        overlap.bind((startup.HOST, server.server_port))
                with mock.patch.object(startup, 'safe_print'):
                    second = startup.bind_server(mock.Mock(), server.server_port)
                self.addCleanup(second.server_close)
                self.assertNotIn(second.server_port, (occupied_port, server.server_port))
                self.assertEqual(second.socket.getsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE), 1)
        self.assertEqual((ThreadedWSGIServer.allow_reuse_address,
                          ThreadedWSGIServer.allow_reuse_port), original_reuse)

    def response(self, status=200, payload=None, content=b'page', content_type='text/html'):
        response = mock.Mock(status_code=status, content=content,
                             headers={'Content-Type': content_type})
        response.json.return_value = payload
        return response

    def health(self):
        return self.response(payload={'ok': True, 'app': startup.APP_ID,
                                      'build_id': startup.BUILD_ID, 'instance': 'own-instance'})

    def ready(self, responses, **kwargs):
        session = mock.MagicMock()
        session.__enter__.return_value = session
        session.get.side_effect = responses
        with mock.patch.object(requests, 'Session', return_value=session):
            rows = startup.ready_checks('http://127.0.0.1:32100', 'own-instance', **kwargs)
        return rows, session

    def test_ready_identity_home_files_and_missing_lyrics(self):
        rows, session = self.ready([
            self.health(), self.response(),
            self.response(payload={'ok': True, 'files': []}),
            self.response(status=400, payload={'ok': False, 'error': 'missing mid'}),
        ])
        self.assertEqual([r['status'] for r in rows], ['ok'] * 4)
        self.assertFalse(session.trust_env)
        self.assertEqual([call.args[0] for call in session.get.call_args_list], [
            'http://127.0.0.1:32100/api/health', 'http://127.0.0.1:32100/',
            'http://127.0.0.1:32100/api/files', 'http://127.0.0.1:32100/api/lyrics'])
        for call in session.get.call_args_list:
            self.assertFalse(call.kwargs['allow_redirects'])
            self.assertEqual(call.kwargs['timeout'], 2)

    def test_health_stale_identity_never_runs_other_checks(self):
        for field in ('app', 'build_id', 'instance', 'ok'):
            with self.subTest(field=field):
                response = self.health()
                response.json.return_value[field] = 'stale'
                rows, session = self.ready([response])
                self.assertEqual(rows[0]['status'], 'fail')
                self.assertEqual(session.get.call_count, 1)

    def test_health_nonjson_status_redirect_and_timeout_fail(self):
        malformed = self.response()
        malformed.json.side_effect = ValueError('SECRET')
        for response in (malformed, self.response(404), self.response(302),
                         requests.Timeout('SECRET')):
            with self.subTest(response=type(response).__name__):
                rows, _ = self.ready([response], timeout=0)
                self.assertEqual(rows[0]['status'], 'fail')
                self.assertNotIn('SECRET', json.dumps(rows))

    def test_health_transient_connection_error_retries(self):
        with mock.patch.object(startup.time, 'sleep'):
            rows, session = self.ready([
                requests.ConnectionError('not ready'), self.health(), self.response(),
                self.response(payload={'ok': True, 'files': []}),
                self.response(status=400, payload={'ok': False}),
            ])
        self.assertEqual(session.get.call_count, 5)
        self.assertTrue(all(r['status'] == 'ok' for r in rows))

    def test_http_shape_or_status_failures_not_passed(self):
        rows, _ = self.ready([
            self.health(), self.response(500),
            self.response(payload={'ok': True, 'files': 'not a list'}),
            self.response(status=200, payload={'ok': True}),
        ])
        self.assertEqual([r['status'] for r in rows], ['ok', 'fail', 'fail', 'fail'])

    def test_health_helper_reads_instance_from_app_config(self):
        app = types.SimpleNamespace(config={startup.INSTANCE_CONFIG_KEY: 'unique'})
        self.assertEqual(startup.health_identity(app), {'ok': True, 'app': startup.APP_ID,
                         'build_id': startup.BUILD_ID, 'instance': 'unique'})

    def test_optional_vocal_never_downloads_or_claims_ready(self):
        vocal = mock.Mock(VOCAL_EXE=None)
        self.assertEqual(startup.start_optional_vocal(vocal)['status'], 'warn')
        vocal.auto_start_async.assert_not_called()
        vocal.start_setup_async.assert_not_called()
        executable = self.data / 'installed-start.exe'
        executable.touch()
        vocal.VOCAL_EXE = executable
        result = startup.start_optional_vocal(vocal)
        self.assertEqual(result['status'], 'warn')
        vocal.auto_start_async.assert_called_once_with()
        vocal.start_setup_async.assert_not_called()

    def mocked_serve(self, smoke, *, no_browser=True, join_error=KeyboardInterrupt(), diag=None,
                     presenter=None):
        app = types.SimpleNamespace(config={})
        server = mock.Mock(server_port=32100)
        thread = mock.Mock()
        thread.is_alive.return_value = True
        def join(timeout=None):
            if timeout != 5:
                raise join_error
        thread.join.side_effect = join
        events = []
        def ready(url, instance):
            self.assertEqual(url, 'http://127.0.0.1:32100')
            self.assertEqual(instance, app.config[startup.INSTANCE_CONFIG_KEY])
            events.append('ready')
            return smoke
        def browser(url):
            events.append('browser')
            return True
        def present(url):
            events.append('presenter')
            return presenter(url)
        with mock.patch.object(startup.importlib, 'import_module', return_value=self.paths), \
                mock.patch.object(startup, 'diagnostics', return_value=diag or []), \
                mock.patch.object(startup, 'bind_server', return_value=server), \
                mock.patch.object(startup.threading, 'Thread', return_value=thread), \
                mock.patch.object(startup, 'ready_checks', side_effect=ready), \
                mock.patch.object(startup, 'start_optional_vocal', return_value=startup.row('vocal', 'warn', 'not tested')) as vocal, \
                mock.patch.object(startup.webbrowser, 'open', side_effect=browser) as opener, \
                mock.patch.object(startup, 'safe_print'), \
                mock.patch.dict(os.environ, {'MUSIC_DOWNLOADER_NO_BROWSER': '1' if no_browser else '0'}):
            code = startup.serve(app, presenter=present if presenter is not None else None)
        return code, server, thread, opener, vocal, events

    def test_serve_ctrlc_shutdown_and_no_browser_environment(self):
        code, server, thread, browser, _, events = self.mocked_serve([startup.row('http:health', 'ok', 'own')])
        self.assertEqual(code, 0)
        thread.start.assert_called_once_with()
        server.shutdown.assert_called_once_with()
        server.server_close.assert_called_once_with()
        browser.assert_not_called()
        self.assertEqual(events, ['ready'])
        report = json.loads((self.data / 'startup-report.json').read_text(encoding='utf-8'))
        self.assertEqual(report['url'], 'http://127.0.0.1:32100')
        self.assertTrue(report['ok'])

    def test_browser_only_after_ready_checks(self):
        code, _, _, browser, _, events = self.mocked_serve([startup.row('http:health', 'ok', 'own')], no_browser=False)
        self.assertEqual(code, 0)
        browser.assert_called_once_with('http://127.0.0.1:32100')
        self.assertEqual(events, ['ready', 'browser'])

    def test_presenter_runs_only_after_readiness_and_return_closes_server(self):
        presenter = mock.Mock()
        code, server, thread, browser, vocal, events = self.mocked_serve(
            [startup.row('http:health', 'ok', 'own')], no_browser=False,
            presenter=presenter)
        self.assertEqual(code, 0)
        self.assertEqual(events, ['ready', 'presenter'])
        presenter.assert_called_once_with('http://127.0.0.1:32100')
        browser.assert_not_called()
        vocal.assert_called_once_with()
        server.shutdown.assert_called_once_with()
        thread.join.assert_called_once_with(timeout=5)
        server.server_close.assert_called_once_with()
        report = json.loads((self.data / 'startup-report.json').read_text(encoding='utf-8'))
        self.assertTrue(report['ok'])
        self.assertEqual(next(r['status'] for r in report['rows'] if r['name'] == 'desktop'), 'ok')

    def test_presenter_never_runs_when_any_readiness_check_fails(self):
        for endpoint in ('http:health', 'http:/', 'http:/api/files', 'http:/api/lyrics'):
            with self.subTest(endpoint=endpoint):
                presenter = mock.Mock()
                code, server, _, browser, vocal, events = self.mocked_serve(
                    [startup.row('http:health', 'ok', 'own'),
                     startup.row(endpoint, 'fail', 'not ready')],
                    no_browser=False, presenter=presenter)
                self.assertEqual(code, 1)
                self.assertEqual(events, ['ready'])
                presenter.assert_not_called()
                browser.assert_not_called()
                vocal.assert_not_called()
                server.shutdown.assert_called_once_with()
                server.server_close.assert_called_once_with()

    def test_presenter_failure_saves_sanitized_report_and_always_closes_server(self):
        for error in (RuntimeError('Cookie=SECRET https://private.invalid/?token=SECRET'),
                      SystemExit('SECRET')):
            with self.subTest(error=type(error).__name__):
                presenter = mock.Mock(side_effect=error)
                code, server, thread, browser, _, events = self.mocked_serve(
                    [startup.row('http:health', 'ok', 'own')], no_browser=False,
                    presenter=presenter)
                self.assertEqual(code, 1)
                self.assertEqual(events, ['ready', 'presenter'])
                browser.assert_not_called()
                server.shutdown.assert_called_once_with()
                thread.join.assert_called_once_with(timeout=5)
                server.server_close.assert_called_once_with()
                text = (self.data / 'startup-report.json').read_text(encoding='utf-8')
                self.assertNotIn('SECRET', text)
                self.assertNotIn('private.invalid', text)
                report = json.loads(text)
                self.assertFalse(report['ok'])
                failure = report['rows'][-1]
                self.assertEqual((failure['name'], failure['status']), ('startup', 'fail'))
                self.assertIn(type(error).__name__, failure['detail'])

    def test_no_browser_environment_overrides_presenter(self):
        presenter = mock.Mock()
        code, server, _, browser, _, events = self.mocked_serve(
            [startup.row('http:health', 'ok', 'own')], presenter=presenter)
        self.assertEqual(code, 0)
        self.assertEqual(events, ['ready'])
        presenter.assert_not_called()
        browser.assert_not_called()
        server.shutdown.assert_called_once_with()
        server.server_close.assert_called_once_with()
        report = json.loads((self.data / 'startup-report.json').read_text(encoding='utf-8'))
        self.assertFalse(any(r['name'] == 'desktop' for r in report['rows']))
        self.assertEqual(next(r['status'] for r in report['rows'] if r['name'] == 'browser'), 'warn')

    def test_presenter_return_preserves_existing_diagnostic_failures(self):
        presenter = mock.Mock()
        code, server, _, browser, _, events = self.mocked_serve(
            [startup.row('http:health', 'ok', 'own')], no_browser=False,
            presenter=presenter, diag=[startup.row('tool:node', 'fail', 'Missing.')])
        self.assertEqual(code, 1)
        self.assertEqual(events, ['ready', 'presenter'])
        browser.assert_not_called()
        server.shutdown.assert_called_once_with()
        server.server_close.assert_called_once_with()
        report = json.loads((self.data / 'startup-report.json').read_text(encoding='utf-8'))
        self.assertFalse(report['ok'])

    def test_failed_smoke_stops_server_without_browser_or_optional_vocal(self):
        code, server, _, browser, vocal, _ = self.mocked_serve([startup.row('http:health', 'fail', 'stale')], no_browser=False)
        self.assertEqual(code, 1)
        browser.assert_not_called()
        vocal.assert_not_called()
        server.shutdown.assert_called_once()
        server.server_close.assert_called_once()
        report = json.loads((self.data / 'startup-report.json').read_text(encoding='utf-8'))
        self.assertFalse(report['ok'])

    def test_bind_failure_writes_failed_report_without_browser(self):
        app = types.SimpleNamespace(config={})
        with mock.patch.object(startup.importlib, 'import_module', return_value=self.paths), \
                mock.patch.object(startup, 'diagnostics', return_value=[]), \
                mock.patch.object(startup, 'bind_server', side_effect=SystemExit(1)), \
                mock.patch.object(startup.webbrowser, 'open') as browser, \
                mock.patch.object(startup, 'safe_print'):
            self.assertEqual(startup.serve(app), 1)
        browser.assert_not_called()
        report = json.loads((self.data / 'startup-report.json').read_text(encoding='utf-8'))
        self.assertFalse(report['ok'])
        self.assertEqual(report['rows'][-1]['name'], 'startup')

    def test_interrupted_diagnostics_not_reported_success(self):
        app = types.SimpleNamespace(config={})
        with mock.patch.object(startup.importlib, 'import_module', return_value=self.paths), \
                mock.patch.object(startup, 'diagnostics', side_effect=KeyboardInterrupt), \
                mock.patch.object(startup, 'bind_server') as bind, \
                mock.patch.object(startup, 'safe_print'):
            self.assertEqual(startup.serve(app), 1)
        bind.assert_not_called()
        report = json.loads((self.data / 'startup-report.json').read_text(encoding='utf-8'))
        self.assertEqual(report['rows'][-1]['status'], 'fail')
        self.assertFalse(report['ok'])
        self.assertIsNone(report['url'])

    def test_diagnostic_failures_survive_successful_smoke(self):
        code, _, _, _, _, _ = self.mocked_serve([startup.row('http:health', 'ok', 'own')],
                                               diag=[startup.row('tool:node', 'fail', 'Missing.')])
        self.assertEqual(code, 1)
        report = json.loads((self.data / 'startup-report.json').read_text(encoding='utf-8'))
        self.assertFalse(report['ok'])


if __name__ == '__main__':
    unittest.main()
