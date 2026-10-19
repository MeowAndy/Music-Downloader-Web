"""Offline desktop contracts: fake native events, imports, registry and browser only."""
import importlib
import logging
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import desktop
import desktop_bootstrap as bootstrap


class BootstrapTests(unittest.TestCase):
    def test_mode_defaults_and_headless_has_highest_priority(self):
        cases = (
            ([], {}, 'desktop'),
            (['--browser'], {}, 'browser'),
            (['--browser', '--other'], {}, 'browser'),
            ([], {'MUSIC_DOWNLOADER_NO_BROWSER': '1'}, 'headless'),
            (['--browser'], {'MUSIC_DOWNLOADER_NO_BROWSER': '1'}, 'headless'),
            ([], {'MUSIC_DOWNLOADER_NO_BROWSER': '0'}, 'desktop'),
            (['--browser'], {'MUSIC_DOWNLOADER_NO_BROWSER': 'true'}, 'browser'),
        )
        for argv, environ, expected in cases:
            with self.subTest(argv=argv, environ=environ):
                self.assertEqual(bootstrap.mode(argv, environ), expected)
        with mock.patch.object(bootstrap.sys, 'argv', ['program', '--browser']), \
                mock.patch.dict(bootstrap.os.environ, {'MUSIC_DOWNLOADER_NO_BROWSER': '1'}):
            self.assertEqual(bootstrap.mode(), 'headless')

    def run_bootstrap(self, selected='desktop', result=0, failure_at=None, error=None,
                      shutdown_error=None):
        owned, vocal, startup, shell = (mock.Mock() for _ in range(4))
        application = object()
        startup.serve.return_value = result
        vocal.shutdown.side_effect = shutdown_error
        events = []
        owned.initialize.side_effect = lambda: events.append('initialize')
        owned.close.side_effect = lambda: events.append('close')
        if shutdown_error is None:
            vocal.shutdown.side_effect = lambda: events.append('shutdown')
        modules = {'owned_processes': owned, 'app': types.SimpleNamespace(app=application),
                   'startup': startup, 'desktop': shell}

        def load(name):
            events.append('import:' + name)
            if name == failure_at:
                raise error
            if name not in modules:
                raise AssertionError('Unexpected application import: ' + name)
            return modules[name]

        if failure_at == 'initialize':
            owned.initialize.side_effect = error
        if failure_at == 'serve':
            startup.serve.side_effect = error
        with mock.patch.object(bootstrap, 'mode', return_value=selected), \
                mock.patch.object(bootstrap.importlib, 'import_module', side_effect=load), \
                mock.patch.dict(sys.modules, {'vocal_api': vocal}), \
                mock.patch.object(bootstrap, 'report_failure') as report, \
                mock.patch.object(logging.getLogger('pywebview'), 'disabled', False):
            if shutdown_error is not None:
                with self.assertRaises(type(shutdown_error)):
                    bootstrap.main([])
                code = None
            else:
                code = bootstrap.main([])
            self.assertTrue(logging.getLogger('pywebview').disabled)
        return types.SimpleNamespace(code=code, owned=owned, vocal=vocal, startup=startup,
                                     shell=shell, application=application, report=report,
                                     events=events)

    def test_desktop_passes_presenter_and_cleans_owned_resources_in_order(self):
        state = self.run_bootstrap()
        self.assertEqual(state.code, 0)
        state.startup.serve.assert_called_once_with(state.application, presenter=state.shell.run)
        state.report.assert_not_called()
        state.owned.initialize.assert_called_once_with()
        state.vocal.shutdown.assert_called_once_with()
        state.owned.close.assert_called_once_with()
        self.assertLess(state.events.index('initialize'), state.events.index('import:app'))
        self.assertEqual(state.events[-2:], ['shutdown', 'close'])

    def test_browser_and_headless_do_not_import_or_pass_desktop_presenter(self):
        for selected in ('browser', 'headless'):
            with self.subTest(selected=selected):
                state = self.run_bootstrap(selected)
                self.assertEqual(state.code, 0)
                state.startup.serve.assert_called_once_with(state.application)
                self.assertNotIn('import:desktop', state.events)
                state.report.assert_not_called()
                state.vocal.shutdown.assert_called_once_with()
                state.owned.close.assert_called_once_with()

    def test_startup_failure_reports_only_in_interactive_modes_and_cleans_up(self):
        for selected in ('desktop', 'browser', 'headless'):
            with self.subTest(selected=selected):
                state = self.run_bootstrap(selected, result=1)
                self.assertEqual(state.code, 1)
                if selected == 'headless':
                    state.report.assert_not_called()
                else:
                    state.report.assert_called_once_with('StartupCheckFailed')
                self.assertEqual(state.events[-2:], ['shutdown', 'close'])

    def test_import_initialization_and_presenter_errors_are_sanitized_and_cleaned(self):
        unsafe_type = type('Unsafe!Error/Name', (RuntimeError,), {})
        for failure_at in ('owned_processes', 'initialize', 'app', 'startup', 'desktop', 'serve'):
            for error in (unsafe_type('Cookie=SECRET'), SystemExit('SECRET')):
                with self.subTest(failure_at=failure_at, error=type(error).__name__):
                    state = self.run_bootstrap(failure_at=failure_at, error=error)
                    self.assertEqual(state.code, 1)
                    expected = 'SystemExit' if isinstance(error, SystemExit) else 'UnsafeErrorName'
                    state.report.assert_called_once_with(expected)
                    self.assertNotIn('SECRET', str(state.report.call_args))
                    state.vocal.shutdown.assert_called_once_with()
                    if failure_at == 'owned_processes':
                        state.owned.close.assert_not_called()
                    else:
                        state.owned.close.assert_called_once_with()

    def test_headless_exception_never_shows_failure_dialog(self):
        state = self.run_bootstrap('headless', failure_at='app', error=ImportError('SECRET'))
        self.assertEqual(state.code, 1)
        state.report.assert_not_called()
        self.assertEqual(state.events[-2:], ['shutdown', 'close'])

    def test_error_kind_is_bounded(self):
        state = self.run_bootstrap(failure_at='app',
                                   error=type('E' * 100, (RuntimeError,), {})('SECRET'))
        state.report.assert_called_once_with('E' * 60)

    def test_owned_close_even_if_vocal_shutdown_raises(self):
        state = self.run_bootstrap(shutdown_error=RuntimeError('SECRET'))
        state.vocal.shutdown.assert_called_once_with()
        state.owned.close.assert_called_once_with()

    def test_native_failure_explanation_is_safe_and_temp_write_is_optional(self):
        with tempfile.TemporaryDirectory() as directory:
            dialog = mock.Mock(return_value=1)
            with mock.patch.object(bootstrap.tempfile, 'gettempdir', return_value=directory), \
                    mock.patch.object(bootstrap, 'os', types.SimpleNamespace(name='nt')), \
                    mock.patch.object(bootstrap.ctypes, 'windll',
                                      types.SimpleNamespace(user32=types.SimpleNamespace(
                                          MessageBoxW=dialog)), create=True):
                text = bootstrap.report_failure('ImportError')
                saved = (Path(directory) / 'MusicDownloaderWeb-startup-error.txt').read_text(
                    encoding='utf-8')
                self.assertIn('ImportError', saved)
                self.assertIn('--browser', saved)
                self.assertIn(desktop.RUNTIME_URL, saved)
                self.assertNotIn('SECRET', text)
                dialog.assert_called_once_with(None, text, '音乐下载器 — 启动失败', 0x10)
                with mock.patch.object(Path, 'write_text', side_effect=PermissionError('SECRET')):
                    text = bootstrap.report_failure('PermissionError')
                self.assertNotIn('SECRET', text)
                self.assertNotIn('诊断说明', text)
                self.assertEqual(dialog.call_count, 2)


class EventHook:
    """Small .NET-like event that supports +=, -= and deterministic dispatch."""
    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self

    def __isub__(self, handler):
        self.handlers.remove(handler)
        return self

    def fire(self, *args):
        for handler in list(self.handlers):
            handler(*args)


class DesktopTests(unittest.TestCase):
    URL = 'http://127.0.0.1:32100'

    def test_url_validation_accepts_only_credential_free_http_and_https(self):
        accepted = (self.URL, self.URL + '/?q=hello%20world#lyrics',
                    'https://example.invalid/path', 'http://[::1]:8899/')
        rejected = (None, 123, b'https://example.invalid', '', '/relative', '//example.invalid',
                    'javascript:alert(1)', 'file:///C:/private', 'data:text/html,test',
                    'mailto:user@example.invalid', 'http://', 'http://[::1',
                    'http://example.invalid:bad', 'http://example.invalid:65536',
                    'http://example.invalid:0', 'https://user@example.invalid',
                    'https://user:password@example.invalid', 'https://@example.invalid',
                    'https://example.invalid/a b', '\nhttps://example.invalid',
                    'https://example.invalid/\t', 'https://example.invalid/\x00',
                    'https://example.invalid/' + 'a' * 8192)
        for value in accepted:
            with self.subTest(value=value):
                self.assertTrue(desktop.valid_external_url(value))
        for value in rejected:
            with self.subTest(value=repr(value)[:80]):
                self.assertFalse(desktop.valid_external_url(value))

    def test_main_frame_allowlist_is_exact_scheme_host_and_port(self):
        for value in (self.URL, self.URL + '/', self.URL + '/login?next=/files#top'):
            self.assertTrue(desktop.local_navigation(value, self.URL), value)
        for value in ('https://127.0.0.1:32100/', 'http://127.0.0.1:32101/',
                      'http://localhost:32100/', 'http://127.0.0.1:32100.evil.invalid/',
                      'http://127.0.0.1:32100@evil.invalid/', 'http://127.0.0.1/',
                      'file:///C:/private', '/api/files', None):
            with self.subTest(value=value):
                self.assertFalse(desktop.local_navigation(value, self.URL))

    def test_runtime_registry_probes_both_hives_and_views_without_tools(self):
        registry = mock.Mock(HKEY_CURRENT_USER=1, HKEY_LOCAL_MACHINE=2,
                             KEY_READ=4, KEY_WOW64_32KEY=8, KEY_WOW64_64KEY=16)
        handle = mock.MagicMock()
        registry.OpenKey.return_value = handle
        with mock.patch.dict(sys.modules, {'winreg': registry}):
            for value in ('85.0', 'not-a-version', None):
                with self.subTest(value=value):
                    registry.reset_mock()
                    registry.QueryValueEx.return_value = (value, 1)
                    self.assertFalse(desktop.runtime_available())
                    self.assertEqual([(c.args[0], c.args[3]) for c in registry.OpenKey.call_args_list],
                                     [(1, 12), (1, 20), (2, 12), (2, 20)])
            registry.OpenKey.side_effect = OSError('SECRET')
            self.assertFalse(desktop.runtime_available())
            registry.OpenKey.side_effect = None
            registry.QueryValueEx.side_effect = [('85.0', 1), ('86.0.1', 1)]
            self.assertTrue(desktop.runtime_available())

    def native_fixture(self):
        events = types.SimpleNamespace(before_show=EventHook(), loaded=EventHook(), closed=EventHook())
        core = types.SimpleNamespace(NewWindowRequested=EventHook(),
                                     Settings=types.SimpleNamespace(IsWebMessageEnabled=True,
                                                                    AreHostObjectsAllowed=True))
        browser = types.SimpleNamespace(on_new_window_request=mock.Mock())
        core.NewWindowRequested += browser.on_new_window_request
        control = types.SimpleNamespace(CoreWebView2=core, NavigationStarting=EventHook(),
                                        CoreWebView2InitializationCompleted=EventHook())
        native = types.SimpleNamespace(browser=browser, webview=control)
        window = mock.Mock(events=events)
        webview = types.ModuleType('webview')
        webview.settings = {}
        webview.create_window = mock.Mock(return_value=window)
        webview.start = mock.Mock()
        guilib = types.ModuleType('webview.guilib')
        guilib.initialize = mock.Mock(return_value=types.SimpleNamespace(renderer='edgechromium'))
        return types.SimpleNamespace(window=window, events=events, core=core, browser=browser,
                                     control=control, native=native, webview=webview, guilib=guilib)

    def run_native(self, state, action=None, *, initialized=True, loaded=True, close=False,
                   runtime=True):
        loaded_event = mock.Mock()
        loaded_event.is_set.return_value = loaded
        closed_event = mock.Mock()
        closed_event.is_set.return_value = close
        closed_event.wait.return_value = close
        # Access before start() is a bug: pywebview has not constructed the native control yet.
        def get_native():
            self.assertTrue(get_native.started, 'native accessed before before_show')
            return state.native
        get_native.started = False
        native_property = mock.PropertyMock(side_effect=get_native)

        def start(watchdog, **kwargs):
            get_native.started = True
            state.events.before_show.fire()
            self.assertEqual(len(state.control.NavigationStarting.handlers), 1)
            self.assertEqual(len(state.control.CoreWebView2InitializationCompleted.handlers), 1)
            state.control.CoreWebView2InitializationCompleted.fire(
                None, types.SimpleNamespace(IsSuccess=initialized))
            if loaded:
                state.events.loaded.fire()
            if close:
                state.events.closed.fire()
            if action is not None:
                action(state)
            watchdog()
        state.webview.start.side_effect = start
        modules = {'webview': state.webview, 'webview.guilib': state.guilib,
                   'paths': types.SimpleNamespace(RES_DIR=Path('offline-resources'))}
        with mock.patch.dict(sys.modules, modules), \
                mock.patch.object(desktop, 'runtime_available', return_value=runtime), \
                mock.patch.object(desktop.threading, 'Event', side_effect=[loaded_event, closed_event]), \
                mock.patch.object(type(state.window), 'native', native_property, create=True), \
                mock.patch.object(desktop.webbrowser, 'open') as opener, \
                mock.patch('time.monotonic', side_effect=[0, 36]):
            desktop.run(self.URL)
        return opener, loaded_event, closed_event

    def test_missing_runtime_and_wrong_renderer_never_create_window_or_fallback(self):
        for runtime, renderer in ((False, 'edgechromium'), (True, 'mshtml'), (True, 'cef')):
            with self.subTest(runtime=runtime, renderer=renderer):
                state = self.native_fixture()
                state.guilib.initialize.return_value.renderer = renderer
                with self.assertRaisesRegex(RuntimeError, 'WebView2'):
                    self.run_native(state, runtime=runtime)
                state.webview.create_window.assert_not_called()
                state.webview.start.assert_not_called()
                if runtime:
                    state.guilib.initialize.assert_called_once_with('edgechromium')
                else:
                    state.guilib.initialize.assert_not_called()

    def test_before_show_attaches_native_guards_and_disables_message_bridge(self):
        state = self.native_fixture()
        opener, loaded, closed = self.run_native(state)
        self.assertEqual(state.webview.settings, {
            'ALLOW_DOWNLOADS': True, 'ALLOW_FILE_URLS': False,
            'OPEN_EXTERNAL_LINKS_IN_BROWSER': False, 'IGNORE_SSL_ERRORS': False})
        state.guilib.initialize.assert_called_once_with('edgechromium')
        state.webview.create_window.assert_called_once_with(
            '音乐下载器', self.URL, width=1500, height=920, min_size=(760, 560),
            background_color='#10121a')
        self.assertNotIn('js_api', state.webview.create_window.call_args.kwargs)
        self.assertFalse(state.core.Settings.IsWebMessageEnabled)
        self.assertFalse(state.core.Settings.AreHostObjectsAllowed)
        self.assertNotIn(state.browser.on_new_window_request, state.core.NewWindowRequested.handlers)
        self.assertEqual(len(state.core.NewWindowRequested.handlers), 1)
        options = state.webview.start.call_args.kwargs
        self.assertEqual(options['gui'], 'edgechromium')
        self.assertFalse(options['debug'])
        self.assertTrue(options['private_mode'])
        loaded.set.assert_called_once_with()
        loaded.is_set.assert_called_once_with()
        closed.wait.assert_not_called()
        closed.set.assert_called_once_with()
        state.window.destroy.assert_not_called()
        opener.assert_not_called()

    def test_native_main_frame_and_new_window_guards(self):
        state = self.native_fixture()
        external = 'https://example.invalid/music'
        def exercise(state):
            for target in (self.URL + '/files', external, 'javascript:alert(1)',
                           'file:///C:/private', 'https://user:SECRET@example.invalid'):
                args = types.SimpleNamespace(Uri=target, Cancel=False)
                state.control.NavigationStarting.fire(None, args)
                self.assertEqual(args.Cancel, target != self.URL + '/files')
                popup = types.SimpleNamespace(Uri=target, Handled=False)
                state.core.NewWindowRequested.fire(None, popup)
                self.assertTrue(popup.Handled)
        opener, _, _ = self.run_native(state, exercise)
        self.assertEqual(opener.call_args_list, [mock.call(external), mock.call(external)])

    def test_external_browser_failure_does_not_escape_native_callback(self):
        state = self.native_fixture()
        def exercise(state):
            with mock.patch.object(desktop.webbrowser, 'open', side_effect=OSError('SECRET')):
                args = types.SimpleNamespace(Uri='https://example.invalid', Cancel=False)
                state.control.NavigationStarting.fire(None, args)
                self.assertTrue(args.Cancel)
        self.run_native(state, exercise)

    def test_initialization_failure_and_load_timeout_destroy_and_raise_controlled_error(self):
        for initialized, loaded, message in ((False, True, 'WebView2 初始化失败'),
                                             (True, False, '桌面页面未能在限定时间内就绪')):
            with self.subTest(initialized=initialized, loaded=loaded):
                state = self.native_fixture()
                with self.assertRaisesRegex(RuntimeError, message):
                    self.run_native(state, initialized=initialized, loaded=loaded)
                state.window.destroy.assert_called_once_with()

    def test_closed_window_does_not_trigger_load_timeout(self):
        state = self.native_fixture()
        _, _, closed = self.run_native(state, loaded=False, close=True)
        state.window.destroy.assert_not_called()
        self.assertEqual(closed.set.call_count, 2)


if __name__ == '__main__':
    unittest.main()
