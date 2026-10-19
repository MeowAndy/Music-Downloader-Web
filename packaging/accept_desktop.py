"""Actual windowed EXE lifecycle checks; no credential/profile import."""
import argparse
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import time

import psutil
import requests


def windows_for(pids):
    user = ctypes.windll.user32
    result = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd, _):
        pid = wintypes.DWORD()
        user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pids and user.IsWindowVisible(hwnd):
            title = ctypes.create_unicode_buffer(300)
            classname = ctypes.create_unicode_buffer(256)
            user.GetWindowTextW(hwnd, title, 300)
            user.GetClassNameW(hwnd, classname, 256)
            result.append((hwnd, title.value, classname.value))
        return True
    user.EnumWindows(callback_type(visit), 0)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('exe', type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    destination = Path(tempfile.mkdtemp(prefix='portable-test-desktop-中文 空目录-', dir=root))
    exe = destination / args.exe.name
    shutil.copy2(args.exe, exe)
    env = dict(os.environ, PATH=str(Path(os.environ['SystemRoot']) / 'System32'), PYTHONPATH='')
    env.pop('MUSIC_DOWNLOADER_NO_BROWSER', None)
    env.pop('NETEASE_COOKIE', None)
    session = requests.Session()
    session.trust_env = False
    results = []
    report_path = destination / 'startup-report.json'
    blocker = socket.socket()
    blocker.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    try:
        blocker.bind(('127.0.0.1', 8899))
        blocker.listen()
        forced_collision = True
    except OSError:
        blocker.close()
        blocker = None
        forced_collision = True
    try:
        for iteration in range(3):
            if report_path.exists():
                report_path.unlink()
            process = subprocess.Popen([str(exe)], cwd=destination, env=env)
            descendants = []
            try:
                deadline = time.monotonic() + 100
                target = None
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise AssertionError('EXE exited before window')
                    descendants = psutil.Process(process.pid).children(recursive=True)
                    visible = windows_for({process.pid, *(child.pid for child in descendants)})
                    assert not any(cls == 'ConsoleWindowClass' for _, _, cls in visible)
                    target = next((hwnd for hwnd, title, _ in visible if title == '音乐下载器'), None)
                    if target and report_path.exists() and any('msedgewebview2' in child.name().lower() for child in descendants):
                        break
                    time.sleep(.25)
                assert target, 'No native app window'
                report = json.loads(report_path.read_text(encoding='utf-8'))
                assert report['ok']
                url = report['url']
                assert not url.endswith(':8899'), 'Port fallback not used'
                identity = session.get(url + '/api/health', timeout=10).json()
                assert identity['instance'] == report['instance'] and Path(identity['data_dir']) == destination
                assert identity['build_id'] == '1.3.0-desktop'
                for route in ('/api/cookie', '/api/netease/login/status', '/api/migu/login/status', '/api/hifiti/login/status'):
                    assert session.get(url + route, timeout=10).json().get('set') is False
                time.sleep(2)
                descendants = psutil.Process(process.pid).children(recursive=True)
                assert any('msedgewebview2' in child.name().lower() for child in descendants)
                ctypes.windll.user32.PostMessageW(wintypes.HWND(target), 0x10, 0, 0)
                assert process.wait(timeout=25) == 0
                _, alive = psutil.wait_procs(descendants, timeout=10)
                assert not alive, 'Owned processes survived window close'
                port = int(url.rsplit(':', 1)[1])
                with socket.socket() as released:
                    released.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                    released.bind(('127.0.0.1', port))
                results.append({'iteration': iteration + 1, 'native_window': True, 'no_console': True,
                                'webview2_child': True, 'anonymous': True, 'port_fallback': True,
                                'graceful_exit': True, 'all_descendants_exited': True, 'port_released': True})
            finally:
                if process.poll() is None:
                    subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], capture_output=True)
        assert not any((destination / name).exists() for name in ('qq_cookie.txt', 'netease-auth.json'))
    finally:
        if blocker is not None:
            blocker.close()
        (destination / 'desktop-acceptance.json').write_text(json.dumps(results, indent=2), encoding='utf-8')
    print(json.dumps({'directory': str(destination), 'results': results}, ensure_ascii=False))


if __name__ == '__main__':
    main()
