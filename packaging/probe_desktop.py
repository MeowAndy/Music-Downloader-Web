"""Real source WebView2 smoke test, with isolated data and no account import."""
import ctypes
from ctypes import wintypes
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    import paths
    destination = Path(tempfile.mkdtemp(prefix='portable-test-desktop-source-', dir=ROOT))
    paths.APP_DIR = destination
    paths.OUTPUT_DIR = destination / 'output'
    paths.OUTPUT_DIR.mkdir()
    with wave.open(str(paths.OUTPUT_DIR / 'desktop-probe.wav'), 'wb') as stream:
        stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(8000)
        stream.writeframes(b'\0\0' * 8000)
    import owned_processes
    owned_processes.initialize()
    import app
    import desktop
    import startup
    import webview
    from unittest.mock import patch
    checks = []
    errors = []
    urls = []
    original = webview.create_window
    original_serve = startup.bind_server
    port = []

    def bind(application):
        server = original_serve(application)
        port.append(server.server_port)
        return server

    def create(*args, **kwargs):
        window = original(*args, **kwargs)

        verified = threading.Event()
        def verify():
            if verified.is_set():
                return
            verified.set()
            try:
                value = window.evaluate_js("({title:document.title,panels:document.querySelectorAll('.layout > .panel').length,bridge:!!window.chrome?.webview, file:!!document.querySelector('input[type=file]'),copy:!!navigator.clipboard})")
                checks.append({'page': value})
                assert value['panels'] == 4 and value['file'] and value['copy']
                icon = window.evaluate_js("({exists:!!document.querySelector('#appSplash img'),loaded:!!document.querySelector('#appSplash img')?.naturalWidth,src:document.querySelector('#appSplash img')?.getAttribute('src')})")
                checks.append({'opening_icon': icon})
                for _ in range(70):
                    if window.evaluate_js("!document.getElementById('appSplash')"):
                        break
                    time.sleep(.1)
                else:
                    raise AssertionError('splash did not dismiss')
                checks.append({'splash_removed': True})
                window.evaluate_js("fetch('/app.ico').then(r=>{document.body.dataset.iconStatus=String(r.status)});fetch('/api/health').then(r=>r.json()).then(j=>document.body.dataset.testBuild=j.build_id)")
                for _ in range(50):
                    if window.evaluate_js('document.body.dataset.testBuild') == startup.BUILD_ID:
                        break
                    time.sleep(.1)
                else:
                    raise AssertionError('same-origin fetch')
                checks.append({'same_origin_fetch': True})
                assert window.evaluate_js('document.body.dataset.iconStatus') == '200'
                geometry_script = """(() => ({width:innerWidth,height:innerHeight,documentWidth:document.documentElement.scrollWidth,documentHeight:document.documentElement.scrollHeight,panels:[...document.querySelectorAll('.layout > .panel')].map(p=>{let r=p.getBoundingClientRect();return {top:r.top,bottom:r.bottom,width:r.width}})}))()"""
                geometry = window.evaluate_js(geometry_script)
                assert geometry['documentWidth'] <= geometry['width'] + 1
                if geometry['width'] >= 1200 and geometry['height'] > 760:
                    assert all(p['bottom'] <= geometry['height'] for p in geometry['panels'])
                    assert geometry['documentHeight'] <= geometry['height'] + 2
                checks.append({'initial_workspace_geometry': geometry})
                for size in ((1000, 800), (780, 580), (1500, 920)):
                    window.resize(*size)
                    time.sleep(.4)
                    measured = window.evaluate_js(geometry_script)
                    assert measured['documentWidth'] <= measured['width'] + 1
                    checks.append({'resized_geometry': measured})
                window.evaluate_js("let a=document.createElement('a');a.href='https://www.hifiti.com/';a.target='_blank';document.body.append(a);a.click()")
                for _ in range(30):
                    if urls:
                        break
                    time.sleep(.1)
                assert urls == ['https://www.hifiti.com/']
                checks.append({'external_browser_intercept': True})
                window.evaluate_js("location.href='https://example.com/'")
                for _ in range(30):
                    if len(urls) > 1:
                        break
                    time.sleep(.1)
                assert window.get_current_url().startswith('http://127.0.0.1:')
                assert urls[-1] == 'https://example.com/'
                checks.append({'main_navigation_blocked': True})
                from System import Action
                settings_values = []
                def read_settings():
                    settings = window.native.webview.CoreWebView2.Settings
                    settings_values.extend([settings.IsWebMessageEnabled, settings.AreHostObjectsAllowed])
                window.native.Invoke(Action(read_settings))
                assert settings_values == [False, False], settings_values
                checks.append({'native_bridge_disabled': True})
                def dialog_probe(script, expected):
                    import os
                    user = ctypes.windll.user32
                    found = threading.Event()
                    titles = []
                    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
                    def inspect(hwnd, _):
                        pid = wintypes.DWORD()
                        user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                        name = ctypes.create_unicode_buffer(256)
                        title = ctypes.create_unicode_buffer(256)
                        user.GetClassNameW(hwnd, name, 256)
                        user.GetWindowTextW(hwnd, title, 256)
                        if pid.value == os.getpid() and name.value == '#32770' and user.IsWindowVisible(hwnd):
                            titles.append(title.value)
                            found.set()
                            user.PostMessageW(hwnd, 0x10, 0, 0)
                        return True
                    callback = callback_type(inspect)
                    def cancel_dialog():
                        until = time.monotonic() + 10
                        while time.monotonic() < until and not found.is_set():
                            user.EnumWindows(callback, 0)
                            time.sleep(.1)
                    watcher = threading.Thread(target=cancel_dialog, daemon=True)
                    watcher.start()
                    window.evaluate_js(script)
                    watcher.join(12)
                    assert found.is_set(), expected + ' dialog not shown'
                    checks.append({expected: titles})
                dialog_probe("let a=document.createElement('a');a.href='/files/desktop-probe.wav';a.download='desktop-probe.wav';document.body.append(a);a.click()", 'native_save_dialog')
                dialog_probe("document.querySelector('input[type=file]').click()", 'native_upload_dialog')
            except Exception as exc:
                errors.append(type(exc).__name__)
            finally:
                ctypes.windll.user32.PostMessageW(wintypes.HWND(int(window.native.Handle.ToInt64())), 0x10, 0, 0)
        window.events.loaded += verify
        return window

    try:
        with patch.object(webview, 'create_window', side_effect=create), patch.object(desktop.webbrowser, 'open', side_effect=lambda url: urls.append(url) or True), patch.object(startup, 'bind_server', side_effect=bind):
            code = startup.serve(app.app, presenter=desktop.run)
        assert code == 0 and not errors and len(checks) >= 5
        import socket
        for number in port:
            with socket.socket() as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                sock.bind(('127.0.0.1', number))
        checks.append({'closed_window_released_port': True})
    finally:
        app.vocal_api.shutdown()
        owned_processes.close()
        (destination / 'desktop-probe.json').write_text(json.dumps({'checks': checks, 'errors': errors}, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'directory': str(destination), 'checks': checks, 'errors': errors}, ensure_ascii=False))


if __name__ == '__main__':
    main()
