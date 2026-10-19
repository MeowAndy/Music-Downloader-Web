# -*- coding: utf-8 -*-
"""人声/伴奏分离（原生功能）

后端直接调用本地 vocal-separate 工具的 HTTP 接口（Flask+Spleeter，端口 9999）：
  POST /upload   上传音频（multipart，字段 audio；不接受 m4a，需先转 mp3）
  POST /process  分离（表单字段 model=2stems/4stems/5stems + wav_name=上传返回的文件名）
  返回 urllist 为结果音频直链

分离结果自动保存到本应用 output/ 目录，与下载的文件统一管理。
"""
import hashlib
import os
import json
import re
import shutil
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import PurePosixPath, PureWindowsPath
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import requests

import paths
import owned_processes

BASE_DIR = paths.APP_DIR
OUTPUT_DIR = paths.OUTPUT_DIR

# Exact asset length, not a signed checksum. Prefer GitHub release metadata when available.
VOCAL_RELEASE_URL = 'https://github.com/jianchang512/vocal-separate/releases/download/v0.0.4/vocal-separate-v0.0.4.7z'
VOCAL_METADATA_URL = 'https://api.github.com/repos/jianchang512/vocal-separate/releases/tags/v0.0.4'
VOCAL_RELEASE_SIZE = 785235010
# SHA256 of the official asset; mirror downloads must match it byte for byte.
VOCAL_RELEASE_SHA256 = '2cbf46a0555183353292a7ddfd3d300b0cb845bc252a9505b0786fc201ca59f4'
# GitHub accelerators for machines without a proxy: probed first, used only when faster.
VOCAL_MIRRORS = (
    ('ghproxy.net', 'https://ghproxy.net/'),
    ('gh-proxy.com', 'https://gh-proxy.com/'),
    ('ghfast.top', 'https://ghfast.top/'),
    ('gh.llkk.cc', 'https://gh.llkk.cc/'),
    ('moeyy.xyz', 'https://github.moeyy.xyz/'),
    ('ghproxy.cc', 'https://ghproxy.cc/'),
)
MIRROR_ENV = 'MUSIC_DOWNLOADER_AI_MIRROR'
PROBE_BYTES = 512 * 1024
PROBE_TIMEOUT = (5, 12)
FAST_ENOUGH = 1.5 * 1048576  # 官方源达到该速率就不再探测镜像
PARALLEL_BONUS = 3.0  # 支持 Range 的源可多连接加速，评分时计入该优势
DOWNLOAD_TIMEOUT = (10, 30)  # connect/read, including redirected asset requests
DOWNLOAD_ATTEMPTS = 4
# Accelerated path: several Range connections in parallel, split no finer than this.
DOWNLOAD_CONNECTIONS = 8
MIN_CHUNK = 8 * 1024 * 1024
THREADS_ENV = 'MUSIC_DOWNLOADER_AI_THREADS'
STARTUP_TIMEOUT = 180
_state_lock = threading.RLock()
_engine_lock = threading.Lock()
# Shutdown never takes _engine_lock: it may be held for the full readiness wait.
_launch_lock = threading.RLock()
_cancel_event = threading.Event()
_engine_process = None
_setup_thread = None

_ERRORS = {
    'network': '下载网络请求失败；已保留部分文件，可重试续传。',
    'size': '下载文件大小不符或传输中断；已保留文件，可重试。',
    'integrity': '下载文件与官方校验和不符；已丢弃该来源的文件并尝试其它来源。',
    'range': '服务器续传响应不正确；已保留部分文件，可重试。',
    'archive': '7z 文件无效或校验失败；已保留官方压缩包，请检查后重试。',
    'unsafe': '压缩包含不安全路径或链接，已拒绝解压并保留压缩包。',
    'dependency': '缺少 py7zr，无法安全解压；请安装依赖后重试。',
    'target': '安装目录已存在；不会覆盖或删除，请检查目录后重试。',
    'missing': '未找到人声分离工具 start.exe，请先安装 AI 组件。',
    'exit': 'AI 引擎提前退出；请检查 AI-engine.log 后重试启动。',
    'timeout': 'AI 引擎启动超时（180秒）；请检查 AI-engine.log 后重试。',
    'port': '端口 9999 被非人声分离服务占用；未启动或停止任何其他程序。',
    'io': '本地文件操作失败；请检查权限、磁盘空间和 AI-engine.log。',
    'unexpected': '安装或启动失败；请检查依赖和 AI-engine.log 后重试。',
    'interrupted': '上次安装或启动被中断；可重试，已保留下载文件。',
    'cancelled': '应用正在关闭，已取消 AI 安装或启动。',
}


class InstallerError(RuntimeError):
    def __init__(self, code):
        self.code = code if code in _ERRORS else 'unexpected'
        super().__init__(_ERRORS[self.code])


class RangeUnsupported(Exception):
    """Server or proxy answered a Range request with the whole file."""


def _connections():
    """Parallel connection count; override with MUSIC_DOWNLOADER_AI_THREADS (1-16)."""
    try:
        override = int(os.environ.get(THREADS_ENV, '') or 0)
    except ValueError:
        override = 0
    if override:
        return max(1, min(16, override))
    return DOWNLOAD_CONNECTIONS


def _error_code(exc):
    if isinstance(exc, InstallerError):
        return exc.code
    if isinstance(exc, requests.RequestException):
        return 'network'
    if isinstance(exc, ImportError):
        return 'dependency'
    if isinstance(exc, OSError):
        return 'io'
    return 'unexpected'


setup_state = {'downloading': False, 'progress': 0, 'stage': '', 'error': '',
               'error_code': '', 'downloaded_bytes': 0, 'total_bytes': VOCAL_RELEASE_SIZE,
               'action': ''}


def _log(event, code=''):
    """Only controlled stage names/codes, never exception text, URLs or environment values."""
    record = {'time': time.strftime('%Y-%m-%d %H:%M:%S'), 'stage': event}
    if code:
        record.update(error_code=code, explanation=_ERRORS[code])
    try:
        with _state_lock, (BASE_DIR / 'AI-install.log').open('a', encoding='utf-8') as f:
            f.write(json.dumps(record, ensure_ascii=False) + '\n')
    except OSError:
        pass


def _update_state(persist=False, **changes):
    with _state_lock:
        setup_state.update(changes)
        if persist:
            # Unique file + replace prevents a torn state file after a crash.
            tmp = BASE_DIR / ('AI-install-state.' + uuid.uuid4().hex + '.tmp')
            try:
                tmp.write_text(json.dumps(setup_state, ensure_ascii=False), encoding='utf-8')
                os.replace(tmp, BASE_DIR / 'AI-install-state.json')
            except OSError:
                pass
            finally:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass


def _restore_state():
    try:
        data = json.loads((BASE_DIR / 'AI-install-state.json').read_text(encoding='utf-8'))
        code = 'interrupted' if data.get('downloading') else data.get('error_code')
        if code in _ERRORS:
            # Do not trust free text from a previous state file.
            setup_state.update(stage='失败', error_code=code, error=_ERRORS[code])
        for key in ('downloaded_bytes', 'total_bytes'):
            value = data.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                setup_state[key] = value
    except (OSError, ValueError, TypeError, AttributeError):
        pass


_restore_state()


def _failure(exc):
    code = _error_code(exc)
    _update_state(persist=True, stage='失败', error=_ERRORS[code], error_code=code)
    _log('failure', code)


def _find_vocal_tool(root=None):
    """Only discover our installation, including arbitrary official archive nesting."""
    root = Path(root) if root is not None else BASE_DIR / 'vocal-separate'
    if not root.is_dir() or root.is_symlink():
        return None
    for exe in sorted(root.rglob('start.exe'), key=lambda p: (len(p.parts), str(p))):
        if exe.is_file() and not exe.is_symlink() and not any(
                parent.is_symlink() for parent in exe.parents if parent != root.parent):
            return exe.parent
    return None


VOCAL_TOOL_DIR = _find_vocal_tool()
VOCAL_EXE = (VOCAL_TOOL_DIR / 'start.exe') if VOCAL_TOOL_DIR else None
VOCAL_URL = 'http://127.0.0.1:9999'
VOCAL_PORT = 9999
TOOL_EXTS = ('.mp3', '.wav', '.flac', '.mp4', '.avi', '.mkv', '.mpeg', '.mov')
STEM_NAMES = {
    '2stems': ['伴奏', '人声'],
    '4stems': ['鼓声', '贝斯', '其他', '人声'],
    '5stems': ['鼓声', '贝斯', '钢琴', '其他', '人声'],
}


def _refresh_tool():
    global VOCAL_TOOL_DIR, VOCAL_EXE
    with _state_lock:
        VOCAL_TOOL_DIR = _find_vocal_tool()
        VOCAL_EXE = VOCAL_TOOL_DIR / 'start.exe' if VOCAL_TOOL_DIR else None


def running(timeout=(1, 2)):
    """Read-only identity check, not merely an open TCP port; never invoke /process."""
    try:
        with _session() as session:
            with session.get(VOCAL_URL + '/', timeout=timeout, stream=True,
                             allow_redirects=False) as response:
                if response.status_code != 200:
                    return False
                content = bytearray()
                for chunk in response.iter_content(8192):
                    content.extend(chunk)
                    if len(content) > 131072:
                        return False
                html = content.decode('utf-8', errors='replace').lower()
                return all(marker in html for marker in (
                    'vocal separate', 'github.com/jianchang512/vocal-separate',
                    '/upload', '/process', 'wav_name', '2stems'))
    except requests.RequestException:
        return False


def _port_in_use():
    # Occupancy is only a reason NOT to launch, never evidence of readiness.
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(('127.0.0.1', VOCAL_PORT)) == 0


def _check_cancelled():
    if _cancel_event.is_set():
        raise InstallerError('cancelled')


def shutdown():
    """Cancel future work and stop only our engine tree; never wait for startup.

    The launch lock covers just process creation/publication, not HTTP readiness
    or the 180-second engine lock. A reused external service has no owned handle.
    Call before owned_processes.close() during desktop teardown. Terminal.
    """
    global _engine_process
    _cancel_event.set()
    with _launch_lock:
        process, _engine_process = _engine_process, None
        if process is not None:
            owned_processes.terminate(process)


def _start_hidden():
    """Return the owned process; retain engine output, suppress console/browser windows."""
    with _launch_lock:
        _check_cancelled()
        env = os.environ.copy()
        env['BROWSER'] = str(paths.RES_DIR / 'vendor' / 'nop.cmd')
        with (BASE_DIR / 'AI-engine.log').open('ab') as log:
            return owned_processes.popen(
                [str(VOCAL_EXE)], cwd=str(VOCAL_TOOL_DIR), env=env, own_tree=True,
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)


def ensure_running():
    """Serialize startup, reuse a still-live owned process, wait up to 180 seconds."""
    global _engine_process
    _check_cancelled()
    while not _engine_lock.acquire(timeout=0.1):
        _check_cancelled()
    try:
        try:
            _check_cancelled()
            ready = running()
            _check_cancelled()
            if ready:
                _update_state(persist=True, stage='完成', error='', error_code='')
                return
            _refresh_tool()
            if not VOCAL_EXE:
                raise InstallerError('missing')
            # Do slow port/HTTP checks outside the lock shutdown needs.
            if _engine_process is None or _engine_process.poll() is not None:
                if _port_in_use():
                    raise InstallerError('port')
                _update_state(persist=True, stage='启动AI引擎', error='', error_code='')
                _log('engine-start')
                with _launch_lock:
                    _check_cancelled()
                    if _engine_process is not None:
                        # A launcher can exit while its owned descendants remain.
                        owned_processes.terminate(_engine_process)
                    _engine_process = _start_hidden()
            process = _engine_process
            deadline = time.monotonic() + STARTUP_TIMEOUT
            while time.monotonic() < deadline:
                _check_cancelled()
                if process.poll() is not None:
                    raise InstallerError('exit')
                remaining = max(0.001, deadline - time.monotonic())
                ready = running(timeout=(min(1, remaining), min(2, remaining)))
                _check_cancelled()
                if ready:
                    _update_state(persist=True, stage='完成', error='', error_code='')
                    _log('engine-ready')
                    return
                _cancel_event.wait(min(0.5, max(0, deadline - time.monotonic())))
            _check_cancelled()
            raise InstallerError('timeout')
        except Exception as exc:
            _failure(exc)
            raise InstallerError(_error_code(exc)) from None
    finally:
        _engine_lock.release()


def _release_size(session):
    try:
        with session.get(VOCAL_METADATA_URL, timeout=DOWNLOAD_TIMEOUT) as response:
            response.raise_for_status()
            assets = response.json()['assets']
            for asset in assets:
                size = asset.get('size')
                if (asset.get('name') == 'vocal-separate-v0.0.4.7z'
                        and isinstance(size, int) and not isinstance(size, bool) and size > 0):
                    _log('release-metadata-size')
                    return size
    except (requests.RequestException, ValueError, KeyError, TypeError, AttributeError):
        pass
    _log('release-known-size-fallback')
    return VOCAL_RELEASE_SIZE


def _validate_members(z):
    seen = set()
    for member in z.list():
        name = member.filename.replace('\\', '/')
        parts = name.split('/')
        if (not name or PurePosixPath(name).is_absolute() or PureWindowsPath(name).drive
                or any(p in ('', '.', '..') or ':' in p or '\x00' in p
                       or p.endswith((' ', '.')) for p in parts)
                or any(re.fullmatch(r'(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?', p)
                       for p in parts)
                or name.casefold() in seen
                or getattr(member, 'is_symlink', False)
                or not (member.is_directory or getattr(member, 'is_file', True))):
            raise InstallerError('unsafe')
        seen.add(name.casefold())
    # Older py7zr FileInfo versions do not expose link types; inspect archive records too.
    for member in z.files:
        if (any(getattr(member, key, False) for key in ('is_symlink', 'is_junction', 'is_socket'))
                or not (member.is_directory or getattr(member, 'is_file', True))):
            raise InstallerError('unsafe')


def _validate_archive(archive):
    _update_state(persist=True, stage='校验7z压缩包（大文件可能需要数分钟）')
    _log('archive-verify')
    try:
        import py7zr
        with py7zr.SevenZipFile(archive, 'r') as z:
            _validate_members(z)
            if z.testzip() is not None:
                raise InstallerError('archive')
    except (InstallerError, ImportError):
        raise
    except Exception:
        raise InstallerError('archive') from None


def _sources():
    """Official URL first, then accelerators; mirrors are marked as untrusted."""
    candidates = [{'name': 'official', 'url': VOCAL_RELEASE_URL, 'mirror': False, 'ranged': None}]
    override = (os.environ.get(MIRROR_ENV) or '').strip()
    if override:
        url = override.replace('{url}', VOCAL_RELEASE_URL) if '{url}' in override else override
        candidates.append({'name': 'custom', 'url': url, 'mirror': True, 'ranged': None})
    for name, prefix in VOCAL_MIRRORS:
        candidates.append({'name': name, 'url': prefix + VOCAL_RELEASE_URL,
                           'mirror': True, 'ranged': None})
    return candidates


def _probe(session, url):
    """One small ranged read to measure a source; None when it is unusable."""
    headers = {'Accept-Encoding': 'identity', 'Range': 'bytes=0-%d' % (PROBE_BYTES - 1)}
    started = time.monotonic()
    try:
        with session.get(url, stream=True, headers=headers, timeout=PROBE_TIMEOUT) as response:
            response.raise_for_status()
            total = 0
            for piece in response.iter_content(65536):
                total += len(piece)
                if total >= PROBE_BYTES or time.monotonic() - started > 15:
                    break
            elapsed = max(time.monotonic() - started, 0.001)
            if not total:
                return None
            return total / elapsed, response.status_code == 206
    except Exception:
        return None


def _ordered_sources(session):
    """Fastest reachable source first. Best effort: failures keep the default order."""
    candidates = _sources()
    official = _probe(session, candidates[0]['url'])
    if official:
        candidates[0]['rate'], candidates[0]['ranged'] = official
        if official[0] >= FAST_ENOUGH:
            _log('source-official-fast')
            return candidates
    else:
        _log('source-unreachable:official')
    ranked = []
    if official:
        ranked.append(candidates[0])
    mirrors = [item for item in candidates[1:]]
    if mirrors:
        _update_state(stage='测速选择下载源')
        with ThreadPoolExecutor(max_workers=len(mirrors), thread_name_prefix='vocal-probe') as pool:
            pending = {pool.submit(_probe, session, item['url']): item for item in mirrors}
            for item in as_completed(pending):
                source = pending[item]
                try:
                    probe = item.result()
                except Exception:
                    probe = None
                if probe:
                    source['rate'], source['ranged'] = probe
                    ranked.append(source)
                else:
                    _log('source-unreachable:' + source['name'])
    if not ranked:
        return candidates
    # 单连接测速会低估支持 Range 的源：它还能用多连接继续放大吞吐。
    ranked.sort(key=lambda item: item['rate'] * (PARALLEL_BONUS if item['ranged'] else 1.0),
                reverse=True)
    _log('source-picked:' + ranked[0]['name'])
    ranked.extend(item for item in candidates if item not in ranked)
    return ranked


def _sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _discard_download(part):
    part.unlink(missing_ok=True)
    _clear_chunks(part)


def _verify_integrity(path, source):
    """The official HTTPS URL is the trust anchor; mirrors must match its SHA256."""
    _update_state(stage='校验下载文件')
    if _sha256(path) == VOCAL_RELEASE_SHA256:
        _log('integrity-match')
        return
    if source['mirror']:
        _log('integrity-mismatch:' + source['name'])
        raise InstallerError('integrity')
    _log('integrity-official-changed')


def _chunk_file(part, start):
    return part.with_name('%s.chunk-%d' % (part.name, start))


def _clear_chunks(part):
    for leftover in BASE_DIR.glob(part.name + '.chunk-*'):
        leftover.unlink(missing_ok=True)


def _fetch_chunk(session, path, url, start, end, expected, report):
    """Fetch [start, end) into path, resuming from its current size.

    The file is a segment of the official archive, never part of .part, so a
    truncated segment only costs its own retry. report(size) publishes this
    segment's on-disk size, which keeps the overall progress monotonic.
    """
    length = end - start
    have = path.stat().st_size if path.exists() else 0
    if have > length:
        path.unlink()
        have = 0
    if have == length:
        return
    headers = {'Accept-Encoding': 'identity',
               'Range': 'bytes=%d-%d' % (start + have, end - 1)}
    with session.get(url, stream=True, headers=headers,
                     timeout=DOWNLOAD_TIMEOUT) as response:
        response.raise_for_status()
        if response.status_code == 200:
            # Whole file instead of the requested segment: unusable, fall back.
            raise RangeUnsupported()
        if response.status_code != 206:
            raise InstallerError('range')
        if response.headers.get('Content-Encoding', 'identity').lower() != 'identity':
            raise InstallerError('range')
        match = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)', response.headers.get('Content-Range', ''))
        if not match:
            raise InstallerError('range')
        first, last, total = map(int, match.groups())
        if first != start + have or last != end - 1 or total != expected:
            raise InstallerError('range')
        want = end - start - have
        header_length = response.headers.get('Content-Length')
        if header_length is not None and (not header_length.isdigit() or int(header_length) != want):
            raise InstallerError('size')
        received = 0
        with path.open('ab' if have else 'wb') as stream:
            for piece in response.iter_content(1024 * 1024):
                _check_cancelled()
                if not piece:
                    continue
                if received + len(piece) > want:
                    raise InstallerError('size')
                stream.write(piece)
                received += len(piece)
                report(have + received)
            stream.flush()
            os.fsync(stream.fileno())
        if received != want or path.stat().st_size != length:
            raise InstallerError('size')


def _download_parallel(session, part, source, offset, expected, count):
    """Fetch the missing tail over `count` connections, then append it in order.

    Completed segments are kept, so a retry (or a restarted app) resumes them.
    Segments are appended only after every one of them verified, which keeps
    .part a valid prefix of the official file at all times.
    """
    remaining = expected - offset
    span = remaining // count
    ranges = []
    position = offset
    for index in range(count):
        end = expected if index == count - 1 else position + span
        ranges.append((position, end))
        position = end
    starts = {start for start, _ in ranges}
    for leftover in BASE_DIR.glob(part.name + '.chunk-*'):
        try:
            start = int(leftover.name.rsplit('-', 1)[1])
        except (IndexError, ValueError):
            start = None
        if start not in starts:
            leftover.unlink(missing_ok=True)
    have = {}
    for start, end in ranges:
        segment = _chunk_file(part, start)
        size = segment.stat().st_size if segment.exists() else 0
        if size > end - start:
            segment.unlink()
            size = 0
        have[start] = size
    _log('download-parallel-%d' % count)
    _update_state(stage='下载中（%d 个连接加速%s）' % (
        count, '' if source['name'] == 'official' else ' · ' + source['name']))

    def publish():
        with _state_lock:
            downloaded = offset + sum(have.values())
            setup_state['downloaded_bytes'] = downloaded
            setup_state['progress'] = min(100, int(downloaded * 100 / expected))

    def reporter(start):
        def report(size):
            have[start] = size
            publish()
        return report

    publish()
    with ThreadPoolExecutor(max_workers=count, thread_name_prefix='vocal-chunk') as pool:
        pending = [pool.submit(_fetch_chunk, session, _chunk_file(part, start), source['url'],
                               start, end, expected, reporter(start)) for start, end in ranges]
        failures = []
        for item in as_completed(pending):
            error = item.exception()
            if error is not None:
                failures.append(error)
    for failure in failures:
        if isinstance(failure, RangeUnsupported):
            raise failure
    if failures:
        raise failures[0]
    _update_state(stage='合并下载分片')
    with part.open('ab') as stream:
        for start, end in ranges:
            segment = _chunk_file(part, start)
            with segment.open('rb') as piece:
                shutil.copyfileobj(piece, stream, 1024 * 1024)
            segment.unlink(missing_ok=True)
    _update_state(downloaded_bytes=part.stat().st_size, progress=100)


def _download_sequential(session, part, source, offset, expected):
    """Single connection from `offset`; Range ignored means restart from zero."""
    headers = {'Accept-Encoding': 'identity'}
    if offset:
        headers['Range'] = 'bytes=%d-' % offset
    with session.get(source['url'], stream=True, headers=headers,
                     timeout=DOWNLOAD_TIMEOUT) as response:
        response.raise_for_status()
        if response.headers.get('Content-Encoding', 'identity').lower() != 'identity':
            raise InstallerError('range')
        if response.status_code == 206:
            match = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)',
                                 response.headers.get('Content-Range', ''))
            if not match:
                raise InstallerError('range')
            start, end, total = map(int, match.groups())
            if start != offset or total != expected or not start <= end < total:
                raise InstallerError('range')
            response_size = end - start + 1
        elif response.status_code == 200:
            # Range ignored: discard only partial content and restart, never append.
            offset = 0
            response_size = expected
        else:
            raise InstallerError('range')
        length = response.headers.get('Content-Length')
        if length is not None and (not length.isdigit() or int(length) != response_size):
            raise InstallerError('size')
        received = 0
        with part.open('ab' if offset else 'wb') as f:
            _update_state(downloaded_bytes=offset)
            for chunk in response.iter_content(1024 * 1024):
                _check_cancelled()
                if not chunk:
                    continue
                if received + len(chunk) > response_size:
                    raise InstallerError('size')
                f.write(chunk)
                received += len(chunk)
                count = offset + received
                _update_state(downloaded_bytes=count, progress=int(count * 100 / expected))
            f.flush()
            os.fsync(f.fileno())
        if received != response_size or part.stat().st_size != expected:
            raise InstallerError('size')


def _download_archive(session, expected):
    """Resume only verified byte ranges. Publish a validated exact-size archive atomically.

    Large remainders use several parallel Range connections; anything smaller, or a
    server/proxy that ignores Range, uses the single-stream path.
    """
    archive = BASE_DIR / 'vocal-separate.7z'
    part = BASE_DIR / 'vocal-separate.7z.part'
    _update_state(total_bytes=expected)
    if archive.exists():
        size = archive.stat().st_size
        if size == expected:
            _update_state(downloaded_bytes=size, progress=100)
            _validate_archive(archive)
            _log('reuse-official-archive')
            return archive
        if size > expected or part.exists():
            raise InstallerError('size')
        os.replace(archive, part)  # recover an interrupted legacy downloader
    sources = _ordered_sources(session)
    last_error = InstallerError('size')
    failures = 0
    while failures < DOWNLOAD_ATTEMPTS:
        _check_cancelled()
        source = sources[0]
        offset = part.stat().st_size if part.exists() else 0
        if offset > expected:
            raise InstallerError('size')
        if offset == expected:
            _update_state(downloaded_bytes=offset, progress=100)
            _verify_integrity(part, source)
            _validate_archive(part)
            os.replace(part, archive)
            _clear_chunks(part)
            return archive
        _update_state(downloaded_bytes=offset, progress=int(offset * 100 / expected))
        _log('download-resume' if offset else 'download-start')
        _log('download-source:' + source['name'])
        try:
            remaining = expected - offset
            connections = (min(_connections(), remaining // MIN_CHUNK)
                           if source['ranged'] is not False else 0)
            if connections >= 2:
                try:
                    _download_parallel(session, part, source, offset, expected, connections)
                except RangeUnsupported:
                    source['ranged'] = False
                    _log('download-range-unsupported')
                    offset = part.stat().st_size if part.exists() else 0
                    _download_sequential(session, part, source, offset, expected)
            else:
                _download_sequential(session, part, source, offset, expected)
            if part.stat().st_size != expected:
                raise InstallerError('size')
            _verify_integrity(part, source)
            _validate_archive(part)
            os.replace(part, archive)
            _clear_chunks(part)
            _log('download-complete')
            return archive
        except (requests.RequestException, InstallerError) as exc:
            if isinstance(exc, InstallerError) and exc.code not in ('range', 'size', 'integrity'):
                raise
            failures += 1
            last_error = exc
            _log('download-retry', _error_code(exc))
            if isinstance(exc, InstallerError) and exc.code == 'integrity':
                # 镜像内容不可信：丢弃已下载字节，改用其它来源重下
                _discard_download(part)
            if len(sources) > 1:
                sources.append(sources.pop(0))
            if failures < DOWNLOAD_ATTEMPTS:
                # Retain the legacy sleep for ordinary retries; cancellation is
                # checked before every subsequent network attempt.
                _check_cancelled()
                time.sleep(min(2 ** failures, 4))
    raise InstallerError(_error_code(last_error)) from None


def _install_archive(archive):
    target = BASE_DIR / 'vocal-separate'
    if target.exists() or target.is_symlink():
        raise InstallerError('target')
    staging = Path(tempfile.mkdtemp(prefix='.vocal-staging-', dir=BASE_DIR))
    try:
        import py7zr
        # Validate immediately before extraction; no unsafe system tar fallback.
        with py7zr.SevenZipFile(archive, 'r') as z:
            _validate_members(z)
            z.extractall(staging)
        if not _find_vocal_tool(staging):
            raise InstallerError('missing')
        if target.exists() or target.is_symlink():
            raise InstallerError('target')
        staging.rename(target)
        _refresh_tool()
        _log('install-published')
    finally:
        if staging.exists():
            shutil.rmtree(staging)  # only our unique temporary directory
    # Keep the official archive, including on extraction/startup failure.


def _setup_worker():
    try:
        _check_cancelled()
        _refresh_tool()
        if not VOCAL_EXE:
            if (BASE_DIR / 'vocal-separate').exists():
                raise InstallerError('target')
            _update_state(persist=True, stage='准备下载', error='', error_code='')
            with _download_session() as session:
                expected = _release_size(session)
                _update_state(persist=True, stage='下载中')
                archive = _download_archive(session, expected)
            _check_cancelled()
            _update_state(persist=True, stage='验证并解压中', progress=100)
            _log('extract-start')
            _install_archive(archive)
        ensure_running()
        _check_cancelled()
        _update_state(persist=True, stage='完成', progress=100, error='', error_code='')
    except Exception as exc:
        _failure(exc)
    finally:
        _update_state(persist=True, downloading=False)


def start_setup_async():
    """Return started/busy/ready. Installed-but-failed tools retry startup, not download."""
    global _setup_thread
    with _state_lock:
        _check_cancelled()
        if setup_state['downloading']:
            return 'busy'
        # Reserve before checking readiness so concurrent callers cannot create workers.
        _update_state(persist=True, downloading=True, action='started')
    try:
        _refresh_tool()
        ready = VOCAL_EXE and running()
        _check_cancelled()
        if ready:
            _update_state(persist=True, downloading=False, action='ready', stage='完成',
                          error='', error_code='')
            return 'ready'
        _update_state(persist=True, stage='准备启动' if VOCAL_EXE else '准备下载',
                      error='', error_code='')
        _log('setup-requested')
        thread = threading.Thread(target=_setup_worker, daemon=True, name='vocal-setup')
        with _launch_lock:
            _check_cancelled()
            with _state_lock:
                _setup_thread = thread
            thread.start()
        return 'started'
    except Exception as exc:
        _failure(exc)
        _update_state(persist=True, downloading=False)
        raise InstallerError(_error_code(exc)) from None


def auto_start_async():
    """Start only existing installs, through the same single-worker reservation."""
    if _cancel_event.is_set():
        return None
    _refresh_tool()
    if VOCAL_EXE:
        try:
            return start_setup_async()
        except Exception as exc:
            _failure(exc)
    return None


def setup_status():
    with _state_lock:
        return dict(setup_state, tool_found=VOCAL_EXE is not None,
                    tool_dir=str(VOCAL_TOOL_DIR) if VOCAL_TOOL_DIR else '')


def _download_session():
    # Respect corporate/environment HTTPS proxies for GitHub; never log their values.
    s = requests.Session()
    s.trust_env = True
    return s


def _session():
    s = requests.Session()
    s.proxies = {'http': None, 'https': None}
    s.trust_env = False
    return s


def _prepare_file(src: Path) -> Path:
    """工具不接受 m4a，需要先转成 mp3（同目录临时文件，避免跨盘）"""
    if src.suffix.lower() in TOOL_EXTS:
        return src
    import ffmpeg_api
    if not ffmpeg_api.available():
        raise RuntimeError('m4a 文件需先转 mp3 才能分离，但未找到 ffmpeg')
    tmp = src.with_name('_vocal_tmp_' + src.stem + '.mp3')
    cmd = [ffmpeg_api.FFMPEG, '-y', '-i', str(src),
           '-codec:a', 'libmp3lame', '-qscale:a', '2', str(tmp), '-loglevel', 'error']
    try:
        r = owned_processes.run(cmd, capture_output=True, timeout=600)
    except subprocess.TimeoutExpired:
        raise RuntimeError('m4a 转 mp3 超时')
    if r.returncode != 0 or not tmp.is_file() or tmp.stat().st_size < 1024:
        raise RuntimeError('m4a 转 mp3 失败: %s' % r.stderr.decode(errors='ignore')[-150:])
    return tmp


def separate_file(src: Path, model='2stems'):
    """分离一个音频文件，结果保存到 output/，返回结果列表"""
    if model not in STEM_NAMES:
        raise RuntimeError('不支持的模型: %s' % model)
    ensure_running()
    if not src.is_file():
        raise RuntimeError('文件不存在: %s' % src.name)

    prepared = _prepare_file(src)
    s = _session()
    t0 = time.time()

    # 1. 上传
    with open(prepared, 'rb') as f:
        r = s.post(VOCAL_URL + '/upload',
                   files={'audio': (prepared.name, f, 'application/octet-stream')},
                   timeout=600)
    j = r.json() if 'json' in r.headers.get('Content-Type', '') else {}
    if j.get('code') != 0:
        raise RuntimeError('上传失败: %s' % j.get('msg', r.text[:100]))

    # 2. 分离
    r2 = s.post(VOCAL_URL + '/process',
                data={'model': model, 'wav_name': j['data']}, timeout=3600)
    j2 = r2.json() if 'json' in r2.headers.get('Content-Type', '') else {}
    if j2.get('code') != 0:
        raise RuntimeError('分离失败: %s' % str(j2.get('msg', r2.text[:150])))

    # 3. 下载结果到 output/
    stem_names = j2.get('data') or STEM_NAMES[model]
    base = src.stem
    results = []
    for url, stem in zip(j2['urllist'], stem_names):
        try:
            rr = s.get(url, timeout=600)
            rr.raise_for_status()
            out_name = '%s - %s.wav' % (base, stem)
            out = OUTPUT_DIR / out_name
            i = 1
            while out.exists():
                out = OUTPUT_DIR / ('%s - %s (%d).wav' % (base, stem, i))
                i += 1
            out.write_bytes(rr.content)
            results.append({'name': out.name, 'size': len(rr.content), 'stem': stem})
        except Exception:
            pass  # 单个分轨下载失败不影响其他

    # 清理临时转换文件
    if prepared != src and prepared.exists():
        prepared.unlink()

    return {'results': results, 'elapsed': round(time.time() - t0, 1)}
