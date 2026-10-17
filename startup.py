# -*- coding: utf-8 -*-
"""Portable startup and self-tests; importing this module has no side effects.

The caller registers GET /api/health returning jsonify(health_identity(app))
BEFORE calling serve(app). No routes are registered here. Only serve() starts
threads, opens a browser, or writes startup-report.json. diagnostics() performs
explicit checks, but never reads a cookie itself or installs dependencies.
"""
import importlib
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import webbrowser
from pathlib import Path

BUILD_ID = '1.2.0'
APP_ID = 'music-downloader-web'
HOST = '127.0.0.1'
PORT = 8899
INSTANCE_CONFIG_KEY = 'STARTUP_INSTANCE'
DEPENDENCIES = ('flask', 'requests', 'paho.mqtt.client', 'py7zr', 'bs4', 'PIL.Image')
REQUIRED_RESOURCES = (
    'static/index.html', 'vendor/node_bridge.js', 'vendor/netease_bridge.js',
    'vendor/netease/UPSTREAM.json', 'vendor/netease/util/request.js',
    'vendor/netease/module/lyric.js', 'vendor/netease/module/song_download_url_v1.js',
    'vendor/netease/module/login_qr_key.js', 'vendor/netease/module/login_qr_create.js',
    'vendor/netease/module/login_qr_check.js', 'vendor/um/um_decrypt.js',
    'vendor/um/um_worker.js', 'vendor/um/crypto_0.wasm', 'vendor/um/crypto_1.wasm',
    'ncm转mp3.exe',
)


def row(name, status, detail):
    """Details must be controlled text, never cookies/HTTP bodies/exceptions."""
    if status not in ('ok', 'warn', 'fail'):
        raise ValueError('Unknown diagnostic status')
    return {'name': name, 'status': status, 'detail': detail}


def _error_kind(exc):
    # Exception messages can contain headers, URLs, cookies, and response bodies.
    return re.sub(r'[^A-Za-z0-9_]', '', type(exc).__name__)[:60]


def safe_print(text):
    """Work with legacy Windows consoles without changing global encodings."""
    stream = sys.stdout
    if stream is None:  # PyInstaller --windowed
        return
    encoding = getattr(stream, 'encoding', None) or 'utf-8'
    value = str(text).encode(encoding, errors='backslashreplace').decode(encoding)
    try:
        print(value, file=stream, flush=True)
    except (OSError, UnicodeError):
        pass  # A detached console must not prevent the report from being saved.


def check_imports():
    rows = []
    for module in DEPENDENCIES:
        try:
            importlib.import_module(module)
        except Exception as exc:
            rows.append(row('import:' + module, 'fail',
                            '依赖导入失败（%s）；请重新打包并包含此依赖。' % _error_kind(exc)))
        else:
            rows.append(row('import:' + module, 'ok', '依赖导入成功。'))
    return rows


def check_tool(executable, tool, timeout=10):
    """Check ONLY the bundled binary; PATH fallback is not portable validation."""
    executable = Path(executable)
    name = 'tool:' + tool
    try:
        if not executable.is_file():
            return row(name, 'fail', '缺少内置工具 bin/%s.exe。' % tool)
        args = ['--version'] if tool == 'node' else ['-version']
        result = subprocess.run(
            [str(executable), *args], capture_output=True, timeout=timeout,
            creationflags=(getattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000)
                           if os.name == 'nt' else 0),
        )
    except subprocess.TimeoutExpired:
        return row(name, 'fail', '工具版本检查超时（%s 秒）。' % timeout)
    except Exception as exc:
        return row(name, 'fail', '无法运行内置工具（%s）。' % _error_kind(exc))
    if result.returncode != 0:
        return row(name, 'fail', '工具版本命令失败，退出码 %d。' % result.returncode)
    # Do not log arbitrary subprocess output. Keep only a recognized version token.
    output = result.stdout
    if isinstance(output, bytes):
        output = output.decode('utf-8', errors='replace')
    pattern = (r'^v([0-9]+\.[0-9]+\.[0-9]+)\s*$' if tool == 'node'
               else r'^ffmpeg version ([A-Za-z0-9][A-Za-z0-9.+_-]{0,79})(?:\s|$)')
    match = re.search(pattern, output or '', re.MULTILINE)
    if not match:
        return row(name, 'fail', '工具版本输出无法识别，检查未通过。')
    return row(name, 'ok', '内置工具版本：' + match.group(1))


def check_writable(directory, name='data:writable'):
    """Create and delete only our uniquely named probe, never an existing file."""
    try:
        with tempfile.NamedTemporaryFile(prefix='.startup-probe-', dir=str(directory)) as probe:
            probe.write(b'portable-startup-probe')
            probe.flush()
            os.fsync(probe.fileno())
    except Exception as exc:
        return row(name, 'fail', '目录写入或探针清理失败（%s）。' % _error_kind(exc))
    return row(name, 'ok', '独立临时文件写入及清理成功，未修改已有文件。')


def diagnostics(paths_module=None, qq_module=None, *, network=True):
    """Return safe rows. Supply mocked modules to run without cookie/network I/O.

    Cookie presence is metadata-only (nonempty regular file). An authenticated
    search uses qq_api's existing cookie handling; cookie validity is not assumed.
    Playlist and lyric network requests are intentionally not startup checks.
    """
    paths_module = paths_module if paths_module is not None else importlib.import_module('paths')
    resource_dir = Path(paths_module.RES_DIR)
    data_dir = Path(paths_module.APP_DIR)
    rows = check_imports()
    for relative in REQUIRED_RESOURCES:
        try:
            present = (resource_dir / relative).is_file()
        except OSError:
            present = False
        rows.append(row('resource:' + relative, 'ok' if present else 'fail',
                        '内置资源存在。' if present else '内置资源缺失或不可访问。'))
    rows.extend(check_tool(resource_dir / 'bin' / (tool + '.exe'), tool)
                for tool in ('node', 'ffmpeg'))
    rows.append(check_writable(data_dir))
    output_dir = Path(paths_module.OUTPUT_DIR)
    if output_dir != data_dir:
        rows.append(check_writable(output_dir, 'output:writable'))
    cookie_path = data_dir / 'qq_cookie.txt'
    try:
        cookie_present = cookie_path.is_file() and cookie_path.stat().st_size > 0
    except OSError as exc:
        cookie_present = False
        rows.append(row('login:metadata', 'warn', '无法检查登录文件状态（%s）。' % _error_kind(exc)))
    if not cookie_present:
        rows.append(row('login', 'warn', '尚未登录。请打开「登录设置」扫码登录；本次未测试搜索。'))
    else:
        rows.append(row('login', 'warn', '已有登录文件，但不代表登录有效；诊断不直接读取或记录 Cookie 内容。'))
        if network:
            try:
                qq_module = qq_module if qq_module is not None else importlib.import_module('qq_api')
                songs = qq_module.search('周深', num=3)
                valid = (isinstance(songs, list) and bool(songs)
                         and all(isinstance(song, dict) and song.get('mid') for song in songs))
                rows.append(row('network:search', 'ok' if valid else 'fail',
                                '已登录搜索返回有效歌曲。' if valid
                                else '搜索结果为空或无效；请重新扫码登录。'))
            except Exception as exc:
                rows.append(row('network:search', 'fail',
                                '搜索检查失败（%s）；请检查网络并重新扫码登录。' % _error_kind(exc)))
        else:
            rows.append(row('network:search', 'warn', '已明确禁用启动搜索检查；本次未测试。'))
    rows.append(row('network:playlist/lyrics', 'warn', '启动时不检查歌单和歌词在线获取功能。'))
    return rows


def health_identity(app):
    """Health endpoint contract; call from the endpoint registered by app main."""
    return {'ok': True, 'app': APP_ID, 'build_id': BUILD_ID,
            'instance': app.config.get(INSTANCE_CONFIG_KEY)}


def _make_windows_server(host, port, app):
    from werkzeug.serving import ThreadedWSGIServer

    class ExclusiveThreadedWSGIServer(ThreadedWSGIServer):
        allow_reuse_address = False
        allow_reuse_port = False

        def server_bind(self):
            # Windows SO_REUSEADDR can steal an already-listening socket's port.
            # Set exclusivity before bind without mutating Werkzeug's global class.
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            super().server_bind()

    return ExclusiveThreadedWSGIServer(host, port, app)


def bind_server(app, preferred_port=PORT):
    """Synchronously own the bound socket; port 0 avoids a free-port TOCTOU race."""
    from werkzeug.serving import make_server
    factory = _make_windows_server if os.name == 'nt' else make_server
    options = {} if os.name == 'nt' else {'threaded': True}
    try:
        return factory(HOST, preferred_port, app, **options)
    except (OSError, SystemExit):
        if preferred_port == 0:
            raise
        safe_print('端口 %d 不可用，将使用系统分配的新端口（不复用已有服务）。' % preferred_port)
        return factory(HOST, 0, app, **options)


def ready_checks(base_url, instance, *, timeout=10, request_timeout=2):
    """Local-only smoke tests, with proxy/env settings and redirects disabled."""
    import requests
    rows = []
    deadline = time.monotonic() + timeout
    with requests.Session() as session:
        session.trust_env = False
        while True:
            try:
                response = session.get(base_url + '/api/health',
                                       timeout=request_timeout, allow_redirects=False)
            except requests.RequestException as exc:
                if time.monotonic() < deadline:
                    time.sleep(0.1)
                    continue
                return [row('http:health', 'fail', '本地健康检查请求超时或失败（%s）。' % _error_kind(exc))]
            try:
                identity = response.json()
            except (ValueError, TypeError):
                identity = None
            expected = {'app': APP_ID, 'build_id': BUILD_ID, 'instance': instance}
            valid = (bool(instance) and response.status_code == 200
                     and isinstance(identity, dict) and identity.get('ok') is True
                     and all(identity.get(key) == value for key, value in expected.items()))
            if not valid:
                return [row('http:health', 'fail', '健康检查身份不匹配或响应无效；不会打开浏览器。')]
            rows.append(row('http:health', 'ok', '本实例的应用、版本及实例身份已验证。'))
            break
        for endpoint in ('/', '/api/files', '/api/lyrics'):
            try:
                response = session.get(base_url + endpoint, timeout=request_timeout,
                                       allow_redirects=False)
                if endpoint == '/':
                    valid = (response.status_code == 200 and bool(response.content)
                             and 'text/html' in response.headers.get('Content-Type', '').lower())
                else:
                    payload = response.json()
                    valid = isinstance(payload, dict)
                    if endpoint == '/api/files':
                        valid = (valid and response.status_code == 200
                                 and payload.get('ok') is True and isinstance(payload.get('files'), list))
                    else:
                        valid = (valid and response.status_code == 400 and payload.get('ok') is False)
                rows.append(row('http:' + endpoint, 'ok' if valid else 'fail',
                                '本地接口响应符合预期。' if valid else '接口状态码或响应格式不符合预期。'))
            except Exception as exc:
                rows.append(row('http:' + endpoint, 'fail', '本地接口冒烟检查失败（%s）。' % _error_kind(exc)))
    return rows


def save_report(data_dir, resource_dir, rows, *, instance=None, url=None):
    """Atomically save our controlled diagnostics; no request/tool bodies included."""
    report = {'app': APP_ID, 'build_id': BUILD_ID, 'instance': instance,
              'data_dir': str(data_dir), 'resource_dir': str(resource_dir), 'url': url,
              'ok': not any(item['status'] == 'fail' for item in rows), 'rows': rows}
    target = Path(data_dir) / 'startup-report.json'
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8',
                                         prefix='.startup-report-', suffix='.tmp',
                                         dir=str(data_dir), delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(report, stream, ensure_ascii=True, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    return target


def start_optional_vocal(vocal_module=None):
    try:
        vocal_module = vocal_module if vocal_module is not None else importlib.import_module('vocal_api')
        executable = getattr(vocal_module, 'VOCAL_EXE', None)
        if not executable or not Path(executable).is_file():
            return row('vocal', 'warn', '未安装可选 AI 组件；不会自动下载或安装。')
        vocal_module.auto_start_async()
        return row('vocal', 'warn', '已请求后台启动现有 AI 组件；尚未验证其就绪状态。')
    except Exception as exc:
        return row('vocal', 'warn', '可选 AI 组件自动启动不可用（%s）；未启动安装下载。' % _error_kind(exc))


def serve(app):
    """Run until Ctrl+C. Return 0 only if all diagnostics have no failures.

    Network/optional failures are reported but do not prevent access to the local
    UI. A failed local smoke test DOES prevent browser opening and stops the server.
    This does not replace the app's main guard; the caller invokes it explicitly.
    """
    paths_module = importlib.import_module('paths')
    data_dir, resource_dir = Path(paths_module.APP_DIR), Path(paths_module.RES_DIR)
    instance = uuid.uuid4().hex
    app.config[INSTANCE_CONFIG_KEY] = instance
    safe_print('音乐下载器 Web 版本：' + BUILD_ID)
    safe_print('数据目录：' + str(data_dir))
    safe_print('资源目录：' + str(resource_dir))
    rows = []
    server = thread = None
    url = None
    thread_errors = []
    locally_ready = False

    def run_server():
        try:
            server.serve_forever(poll_interval=0.1)
        except Exception as exc:
            thread_errors.append(_error_kind(exc))

    def report():
        for item in rows:
            safe_print('[%s] %s - %s' % (item['status'], item['name'], item['detail']))
        try:
            target = save_report(data_dir, resource_dir, rows, instance=instance, url=url)
            safe_print('启动诊断报告：' + str(target))
        except Exception as exc:
            rows.append(row('report', 'fail', '无法保存启动报告（%s）。' % _error_kind(exc)))
            safe_print('[fail] ' + rows[-1]['detail'])
        safe_print('诊断结果：' + ('未通过（详见报告）' if any(r['status'] == 'fail' for r in rows)
                                     else '通过（警告项不代表相关功能已验证）'))

    try:
        rows.extend(diagnostics(paths_module))
        server = bind_server(app)
        url = 'http://%s:%d' % (HOST, server.server_port)
        safe_print('本实例服务地址：' + url)
        thread = threading.Thread(target=run_server, daemon=True, name='music-web-server')
        thread.start()
        smoke = ready_checks(url, instance)
        rows.extend(smoke)
        if any(item['status'] == 'fail' for item in smoke):
            report()
            return 1
        locally_ready = True
        rows.append(start_optional_vocal())
        if os.environ.get('MUSIC_DOWNLOADER_NO_BROWSER') == '1':
            rows.append(row('browser', 'warn', 'MUSIC_DOWNLOADER_NO_BROWSER=1，已禁用自动打开浏览器。'))
        else:
            try:
                opened = webbrowser.open(url)
                rows.append(row('browser', 'ok' if opened else 'warn',
                                '已请求打开浏览器。' if opened else '浏览器未接受打开请求；请手动打开上述地址。'))
            except Exception as exc:
                rows.append(row('browser', 'warn', '请手动打开上述服务地址（%s）。' % _error_kind(exc)))
        report()
        safe_print('按 Ctrl+C 停止本实例服务。')
        while thread.is_alive():
            thread.join(0.5)
        rows.append(row('server', 'fail', '服务意外停止' +
                        (' (%s).' % thread_errors[0] if thread_errors else '.')))
        report()
        return 1
    except KeyboardInterrupt:
        safe_print('正在停止本实例服务。')
        if not locally_ready:
            rows.append(row('startup', 'fail', '启动检查已中断；尚未验证本地服务就绪状态。'))
            report()
            return 1
        return 1 if any(item['status'] == 'fail' for item in rows) else 0
    except (Exception, SystemExit) as exc:
        rows.append(row('startup', 'fail', '启动失败（%s）。' % _error_kind(exc)))
        report()
        return 1
    finally:
        if server is not None:
            # shutdown() deadlocks if serve_forever() never started or already exited.
            if thread is not None and thread.is_alive():
                server.shutdown()
                thread.join(timeout=5)
            server.server_close()
