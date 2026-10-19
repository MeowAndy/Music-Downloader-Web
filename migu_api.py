"""Migu official web transactions, with isolated sessions and verified media.

Web headers verified against music.migu.cn/v5/static/js/@migusdk-617e3513.js.
Only the observed free download gateway and PQ/HQ MP3, SQ FLAC are supported;
no playback fallback, URL quality rewriting, account emulation or decryption.
"""
import http.client
import ipaddress
import json
import os
import re
import socket
import ssl
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests

import paths
import owned_processes

SEARCH_URL = 'https://app.u.nf.migu.cn/pc/resource/song/item/search/v1.0'
RESOURCE_URL = 'https://c.musicapp.migu.cn/MIGUM2.0/v1.0/content/resourceinfo.do'
DOWNLOAD_URL = 'https://app.c.nf.migu.cn/MIGUM2.0/strategy/download-url/by-songid/v1.0'
MAX_JSON = 2 * 1024 * 1024
MAX_LYRICS = 512 * 1024
MAX_DOWNLOAD_BYTES = 256 * 1024 * 1024
MAX_DOWNLOAD_SECONDS = 300
MAX_DURATION = 30 * 60
# Verified public resource rateFormats and official download responses.
_FORMATS = {'standard': ('PQ', '020007', 'mp3'),
            'hq': ('HQ', '020010', 'mp3'), 'sq': ('SQ', '011002', 'flac')}
_QUALITY_NAMES = {'standard': '标准', 'hq': '高品质', 'sq': '无损'}
_DOWNLOAD_SLOTS = threading.BoundedSemaphore(2)
_SLOTS = threading.BoundedSemaphore(4)
_STATE_LOCK = threading.Lock()
_SEARCH_STATE = None
_MESSAGES = {
    'INVALID_INPUT': '咪咕请求参数无效。',
    'NETWORK_ERROR': '咪咕连接失败，请检查网络后重试。',
    'UPSTREAM_ERROR': '咪咕返回了无效数据，请稍后重试。',
    'UPSTREAM_CHANGED': '咪咕搜索接口已发生跳转，当前不能返回歌曲；不是未找到歌曲。',
    'NOT_FOUND': '咪咕未返回该歌曲的资源信息。',
    'DOWNLOAD_UNAVAILABLE': '咪咕官方下载接口未提供可下载的完整音频；不会使用试听或播放地址替代。',
    'PRICE_LOOKUP_FAILED': '咪咕官方下载接口查询歌曲价格失败（200000），当前无法下载；这不是登录提示。',
    'MEMBERSHIP_REQUIRED': '咪咕官方下载接口提示需要开通会员使用此业务（200010）；登录不代表具备该歌曲或音质的下载权限。',
    'LOGIN_REQUIRED': '咪咕登录已失效，请重新扫码；不会使用试听或播放地址替代。',
    'PERMISSION_DENIED': '咪咕未允许下载此音质，不能使用试听或播放地址替代。',
    'COPYRIGHT_UNAVAILABLE': '咪咕提示：应版权方要求，该内容已经无法提供；这不是要求登录的提示。',
    'DOWNLOAD_INVALID': '咪咕音频不完整、格式不符或校验失败。',
    'ENCRYPTED_UNSUPPORTED': '咪咕返回加密音频，当前不支持下载或解密。',
    'VALIDATOR_UNAVAILABLE': '未找到 FFmpeg，无法校验咪咕完整音频；请安装后重试。',
    'STORAGE_ERROR': '无法保存咪咕音频，请检查输出目录权限。',
    'UNSAFE_URL': '咪咕资源地址不在允许范围内。',
    'BUSY': '咪咕请求繁忙，请稍后重试。',
}


class ApiError(RuntimeError):
    def __init__(self, code='UPSTREAM_ERROR', status=502):
        self.code = code if code in _MESSAGES else 'UPSTREAM_ERROR'
        self.status = status
        super().__init__(_MESSAGES[self.code])


def validate_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9]{1,32}', value):
        raise ApiError('INVALID_INPUT', 400)
    return value


def status():
    with _STATE_LOCK:
        search_state = _SEARCH_STATE
    return {'capabilities': {'search': search_state is not False, 'download': True,
                             'lyrics': True, 'login': True},
            'search_verified': search_state is True,
            'message': (str(ApiError('UPSTREAM_CHANGED')) if search_state is False else
                        '支持咪咕官方扫码登录、官方下载与原文歌词；登录仅保存在内存，重启需重新扫码，下载权限以官方交易结果为准。')}


def _session():
    session = requests.Session()
    session.trust_env = False
    session.auth = None
    session.headers.clear()
    session.headers.update({'User-Agent': 'Mozilla/5.0', 'Referer': 'https://m.music.migu.cn/',
                            'Accept-Encoding': 'identity', 'channel': '014X031',
                            'subchannel': '014X031', 'activityId': 'MUSIC-WWW',
                            'version': '6.8.8', 'platform': 'H5'})
    return session


def _read(url, params=None, *, limit=MAX_JSON, lyric=False, token=None):
    if token:
        # Credentials may reach only the explicit official download transaction.
        # Never give requests/urllib3 a secret header or query to debug-log.
        if url != DOWNLOAD_URL or lyric:
            raise ApiError('UNSAFE_URL')
        import migu_login
        try:
            return migu_login.authenticated_read(url, params, token)
        except migu_login.LoginError as exc:
            if exc.code == 'LOGIN_REQUIRED':
                raise ApiError('LOGIN_REQUIRED', 401) from None
            raise ApiError('NETWORK_ERROR') from None
    if not _SLOTS.acquire(timeout=5):
        raise ApiError('BUSY', 503)
    try:
        with _session() as session:
            deadline = time.monotonic() + 40
            for _ in range(4):
                if lyric:
                    url = _lyric_url(url)
                session.cookies.clear()
                with session.get(url, params=params, stream=True, timeout=(8, 15),
                                 allow_redirects=False) as response:
                    if response.status_code in (301, 302, 303, 307, 308):
                        if not lyric:
                            raise ApiError('UPSTREAM_CHANGED', 503)
                        location = response.headers.get('Location')
                        if not location:
                            raise ApiError('UPSTREAM_ERROR')
                        url = urljoin(url, location)
                        params = None
                        continue
                    if response.status_code != 200:
                        raise ApiError('NETWORK_ERROR')
                    length = response.headers.get('Content-Length')
                    if length is not None and (not length.isdecimal() or int(length) > limit):
                        raise ApiError('UPSTREAM_ERROR')
                    body = bytearray()
                    for chunk in response.iter_content(64 * 1024):
                        if time.monotonic() > deadline:
                            raise ApiError('NETWORK_ERROR')
                        body.extend(chunk)
                        if len(body) > limit:
                            raise ApiError('UPSTREAM_ERROR')
                    if length is not None and len(body) != int(length):
                        raise ApiError('UPSTREAM_ERROR')
                    return bytes(body)
            raise ApiError('UPSTREAM_ERROR')
    except requests.RequestException:
        raise ApiError('NETWORK_ERROR') from None
    finally:
        _SLOTS.release()


def _json(url, params, *, token=None):
    raw = _read(url, params, token=token) if token else _read(url, params)
    try:
        body = json.loads(raw.decode('utf-8-sig'))
    except (ValueError, UnicodeError):
        raise ApiError('UPSTREAM_ERROR') from None
    if not isinstance(body, (dict, list)):
        raise ApiError('UPSTREAM_ERROR')
    return body


def resource(id):
    sid = validate_id(id)
    body = _json(RESOURCE_URL, {'copyrightId': sid, 'resourceType': 2})
    if not isinstance(body, dict) or str(body.get('code')) != '000000':
        raise ApiError('UPSTREAM_ERROR')
    resources = body.get('resource')
    if not isinstance(resources, list):
        raise ApiError('UPSTREAM_ERROR')
    item = next((v for v in resources if isinstance(v, dict) and str(v.get('copyrightId')) == sid), None)
    if item is None:
        raise ApiError('NOT_FOUND', 404)
    return item


def _duration(value):
    if isinstance(value, str) and re.fullmatch(r'\d{1,2}:\d{2}:\d{2}', value):
        h, m, s = map(int, value.split(':'))
        return h * 3600 + m * 60 + s if m < 60 and s < 60 else 0
    try:
        return max(0, min(int(value), 24 * 3600))
    except (TypeError, ValueError):
        return 0


def search(kw, num=30):
    global _SEARCH_STATE
    if (not isinstance(kw, str) or not kw.strip() or len(kw) > 200
            or re.search(r'[\x00-\x1f\x7f]', kw) or type(num) is not int or not 1 <= num <= 100):
        raise ApiError('INVALID_INPUT', 400)
    try:
        body = _json(SEARCH_URL, {'text': kw.strip(), 'pageNo': 1, 'pageSize': num})
    except ApiError as exc:
        if exc.code == 'UPSTREAM_CHANGED':
            with _STATE_LOCK:
                _SEARCH_STATE = False
        raise
    songs = body
    if not isinstance(songs, list):
        raise ApiError('UPSTREAM_ERROR')
    result = []
    for item in songs[:num]:
        if not isinstance(item, dict):
            continue
        try:
            sid = validate_id(str(item.get('copyrightId') or ''))
        except ApiError:
            continue
        if not isinstance(item.get('songName'), str):
            continue
        singers = item.get('singerList')
        singer = '/'.join(v['name'] for v in singers if isinstance(v, dict)
                          and isinstance(v.get('name'), str)) if isinstance(singers, list) else ''
        result.append({'source': 'migu', 'id': sid, 'name': item['songName'][:500],
                       'singer': singer[:500],
                       'album': str(item.get('album') or '')[:500],
                       'duration': _duration(item.get('duration') or item.get('length')),
                       'rights_hint': '以官方下载交易结果为准；部分歌曲或音质需会员或暂不可下载',
                       'rights': {'download_hint': False, 'requires_verification': True}})
    with _STATE_LOCK:
        _SEARCH_STATE = True
    return result


def _public_address(value):
    address = ipaddress.ip_address(value)
    # IPv4-mapped IPv6 multicast is not marked is_multicast by ipaddress.
    address = getattr(address, 'ipv4_mapped', None) or address
    return address.is_global and not address.is_multicast


def _lyric_url(value):
    try:
        if not isinstance(value, str) or len(value) > 8192 or any(c.isspace() or ord(c) < 32 for c in value):
            raise ValueError()
        parts = urlsplit(value)
        host = (parts.hostname or '').lower()
        if (parts.scheme != 'https' or host != 'd.musicapp.migu.cn' or parts.port not in (None, 443)
                or parts.username is not None or parts.password is not None):
            raise ValueError()
        addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        if not addresses or any(not _public_address(row[4][0]) for row in addresses):
            raise ValueError()
        return value
    except (ValueError, OSError):
        raise ApiError('UNSAFE_URL') from None


def lyric_text(id):
    item = resource(id)
    url = item.get('lrcUrl')
    if not url:
        return ''
    raw = _read(url, limit=MAX_LYRICS, lyric=True)
    try:
        text = raw.decode('utf-8-sig')
    except UnicodeError:
        raise ApiError('UPSTREAM_ERROR') from None
    if re.search(r'<(?:!doctype|html|script|body)\b', text, re.I):
        raise ApiError('UPSTREAM_ERROR')
    return text


def _download_target(value):
    """Validate every hop and return the exact URL plus pinned public addresses."""
    try:
        if (not isinstance(value, str) or len(value) > 8192
                or any(c.isspace() or ord(c) < 32 or ord(c) >= 127 for c in value)
                or '\\' in value or re.search(r'%(?![0-9a-fA-F]{2})', value)):
            raise ValueError()
        p = urlsplit(value)
        host = (p.hostname or '').lower()
        if (p.scheme not in ('http', 'https') or host != 'dlsdownfree.nf.migu.cn'
                or p.username is not None or p.password is not None or p.fragment
                or p.port not in (None, 80 if p.scheme == 'http' else 443)
                or p.path != '/wlansst' or not p.query):
            raise ValueError()
        rows = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        if not rows or any(not _public_address(row[4][0]) for row in rows):
            raise ValueError()
        return urlunsplit(('https', host, p.path, p.query, '')), rows
    except (ValueError, OSError):
        raise ApiError('UNSAFE_URL') from None


def _download_url(value):
    return _download_target(value)[0]


class _PinnedHTTPS(http.client.HTTPSConnection):
    """TLS verifies the official hostname; connect only to prevalidated addresses.

    stdlib HTTP sends the opaque request target verbatim, unlike requests/urllib3
    which normalize escapes and may log signed URLs at DEBUG level.
    """
    def __init__(self, host, rows, deadline):
        super().__init__(host, timeout=min(8, _remaining(deadline)),
                         context=ssl.create_default_context())
        self._rows = rows
        self._deadline = deadline
        self.set_debuglevel(0)

    def connect(self):
        for family, kind, protocol, _, address in self._rows:
            connection = socket.socket(family, kind, protocol)
            try:
                connection.settimeout(min(8, _remaining(self._deadline)))
                connection.connect(address)
                self.sock = self._context.wrap_socket(connection, server_hostname=self.host)
                return
            except ApiError:
                connection.close()
                raise
            except OSError:
                connection.close()
        raise ApiError('NETWORK_ERROR')


class _GatewaySession:
    def __enter__(self):
        self.connection = None
        self.timer = None
        return self

    def _expire(self):
        connection = self.connection
        connection_socket = connection.sock if connection is not None else None
        if connection_socket is not None:
            try:
                connection_socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()

    def __exit__(self, *args):
        if self.timer is not None:
            self.timer.cancel()
        if self.connection is not None:
            self.connection.close()

    def get(self, url, rows, deadline):
        try:
            if self.timer is None:
                # Also interrupt slow-drip response headers (socket idle timeouts
                # alone are not an overall transfer deadline).
                self.timer = threading.Timer(_remaining(deadline), self._expire)
                self.timer.daemon = True
                self.timer.start()
            if self.connection is not None:
                self.connection.close()
            parts = urlsplit(url)
            self.connection = _PinnedHTTPS(parts.hostname, rows, deadline)
            self.connection.request('GET', parts.path + '?' + parts.query,
                                    headers={'User-Agent': 'Mozilla/5.0',
                                             'Accept-Encoding': 'identity', 'Connection': 'close'})
            remaining = _remaining(deadline)
            connection_socket = self.connection.sock
            if connection_socket is None:
                raise ApiError('NETWORK_ERROR')
            connection_socket.settimeout(min(15, remaining))
            return self.connection.getresponse()
        except (OSError, http.client.HTTPException):
            raise ApiError('NETWORK_ERROR') from None

    def chunks(self, response, deadline):
        while True:
            try:
                connection_socket = self.connection.sock
                if connection_socket is not None:
                    connection_socket.settimeout(min(15, _remaining(deadline)))
                # read1 avoids an indefinitely dribbling peer keeping a large read alive.
                chunk = response.read1(64 * 1024)
            except (OSError, http.client.HTTPException):
                raise ApiError('NETWORK_ERROR') from None
            _remaining(deadline)
            if not chunk:
                return
            yield chunk


def _safe_stem(value):
    value = re.sub(r'[<>:"/\\\\|?*\x00-\x1f\x7f]', '_', str(value)).strip(' .')[:120].rstrip(' .')
    if not value:
        value = 'Migu'
    if re.match(r'^(CON|PRN|AUX|NUL|COM[0-9¹²³]|LPT[0-9¹²³])(?:\.|$)', value, re.I):
        value = '_' + value
    return value


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ApiError('NETWORK_ERROR')
    return remaining


def _validate_media(part, extension, duration, deadline, ffmpeg):
    with open(part, 'rb') as audio:
        signature = audio.read(4)
    if not (signature == b'fLaC' if extension == 'flac' else
            signature[:3] == b'ID3' or (len(signature) == 4 and signature[0] == 255
                                      and signature[1] & 0xe0 == 0xe0)):
        raise ApiError('DOWNLOAD_INVALID')
    # Force the expected demuxer, forbid network protocols, and decode all audio.
    # Progress is local and bounded by runtime, never return raw tool output.
    with tempfile.TemporaryFile() as progress:
        try:
            result = owned_processes.run(
                [ffmpeg, '-nostdin', '-hide_banner', '-loglevel', 'error',
                 '-xerror', '-err_detect', 'explode', '-protocol_whitelist', 'file,pipe',
                 '-f', extension, '-i', str(part), '-map', '0:a:0', '-vn', '-sn', '-dn',
                 '-stats_period', '10', '-progress', 'pipe:1', '-nostats', '-f', 'null', '-'],
                stdin=subprocess.DEVNULL, stdout=progress, stderr=subprocess.DEVNULL,
                timeout=_remaining(deadline),
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except subprocess.TimeoutExpired:
            raise ApiError('DOWNLOAD_INVALID') from None
        except OSError:
            raise ApiError('VALIDATOR_UNAVAILABLE', 503) from None
        _remaining(deadline)
        progress.seek(0)
        raw = progress.read(65537)
    times = re.findall(rb'^out_time_us=(\d+)\s*$', raw, re.M)
    actual = int(times[-1]) / 1000000 if times else 0
    if (result.returncode != 0 or len(raw) > 65536 or b'progress=end' not in raw
            or abs(actual - duration) > max(2, duration * 0.05)):
        raise ApiError('DOWNLOAD_INVALID')


def _commit_download(part, stem, extension):
    # Hard-link publication is atomic and exclusive across processes, unlike an
    # empty reservation followed by replace (which briefly exposes an empty file).
    for n in range(10000):
        name = '%s%s.%s' % (stem, ' (%s)' % n if n else '', extension)
        try:
            os.link(part, Path(paths.OUTPUT_DIR) / name)
            return name
        except FileExistsError:
            continue
    raise ApiError('STORAGE_ERROR', 500)


def _stream(url, size, stem, extension, duration, ffmpeg):
    part = None
    try:
        directory = Path(paths.OUTPUT_DIR)
        directory.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + MAX_DOWNLOAD_SECONDS
        # Separate credential-free HTTPS transport with pinned DNS, no logging,
        # no automatic redirects/retries, and no signed-query normalization.
        with _GatewaySession() as session:
            for _ in range(5):
                url, addresses = _download_target(url)
                _remaining(deadline)
                with session.get(url, addresses, deadline) as response:
                    _remaining(deadline)
                    if response.status in (301, 302, 303, 307, 308):
                        location = response.headers.get('Location')
                        if not location:
                            raise ApiError('UNSAFE_URL')
                        url = urljoin(url, location)
                        continue
                    if response.status != 200:
                        raise ApiError('NETWORK_ERROR')
                    length = response.headers.get('Content-Length')
                    if length is not None and (not re.fullmatch(r'[0-9]{1,12}', length)
                                               or int(length) != size):
                        raise ApiError('DOWNLOAD_INVALID')
                    if response.headers.get('Content-Encoding', 'identity').lower() not in ('', 'identity'):
                        raise ApiError('DOWNLOAD_INVALID')
                    total = 0
                    with tempfile.NamedTemporaryFile(dir=directory, prefix='.migu-',
                                                     suffix='.part', delete=False) as out:
                        part = out.name
                        for chunk in session.chunks(response, deadline):
                            _remaining(deadline)
                            total += len(chunk)
                            if total > size or total > MAX_DOWNLOAD_BYTES:
                                raise ApiError('DOWNLOAD_INVALID')
                            out.write(chunk)
                        _remaining(deadline)
                        if total != size:
                            raise ApiError('DOWNLOAD_INVALID')
                        out.flush()
                        os.fsync(out.fileno())
                    break
            else:
                raise ApiError('UNSAFE_URL')
        _validate_media(part, extension, duration, deadline, ffmpeg)
        _remaining(deadline)
        return _commit_download(part, stem, extension)
    except ApiError:
        raise
    except (requests.RequestException, http.client.HTTPException, TimeoutError, ConnectionError, ssl.SSLError):
        raise ApiError('NETWORK_ERROR') from None
    except OSError:
        raise ApiError('STORAGE_ERROR', 500) from None
    except (ValueError, TypeError):
        raise ApiError('DOWNLOAD_INVALID') from None
    finally:
        if part:
            try:
                os.unlink(part)
            except OSError:
                pass


def download(id, quality='standard'):
    sid = validate_id(id)
    if not isinstance(quality, str) or quality not in _FORMATS:
        raise ApiError('INVALID_INPUT', 400)
    if not _DOWNLOAD_SLOTS.acquire(timeout=5):
        raise ApiError('BUSY', 503)
    try:
        item = resource(sid)
        for field in ('copyrightId', 'contentId', 'songId'):
            value = item.get(field)
            if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9]{1,32}', value):
                raise ApiError('UPSTREAM_ERROR')
        if item['copyrightId'] != sid:
            raise ApiError('UPSTREAM_ERROR')
        rate = _FORMATS[quality][0]
        # This explicit transaction is the authority, not can-listen, isDownload,
        # codeRate flags, player URLs or metadata price/VIP hints.
        import migu_login
        auth = migu_login.get_auth()
        params = {'songId': item['songId'], 'formatType': rate}
        body = _json(DOWNLOAD_URL, params, token=auth) if auth else _json(DOWNLOAD_URL, params)
        if not isinstance(body, dict):
            raise ApiError('UPSTREAM_ERROR')
        code = str(body.get('code'))
        if code == '200000':
            raise ApiError('PRICE_LOOKUP_FAILED', 503)
        if code == '200010':
            raise ApiError('MEMBERSHIP_REQUIRED', 403)
        if code == '440000':
            raise ApiError('COPYRIGHT_UNAVAILABLE', 410)
        if code != '000000':
            raise ApiError('DOWNLOAD_UNAVAILABLE', 503)
        data = body.get('data')
        if not isinstance(data, dict):
            raise ApiError('UPSTREAM_ERROR')
        if any(data.get(field) != item[field] for field in ('copyrightId', 'contentId', 'songId')):
            raise ApiError('DOWNLOAD_INVALID')
        if data.get('encryptionType') != '0':
            raise ApiError('ENCRYPTED_UNSUPPORTED')
        # A grant may be a lower quality than requested. Describe only the
        # evidenced formatId/suffix pair, never relabel granted MP3 as lossless.
        actual_quality = next((q for q, (_, fid, ext) in _FORMATS.items()
                               if data.get('formatId') == fid and data.get('suffix') == ext), None)
        if actual_quality is None:
            raise ApiError('DOWNLOAD_INVALID')
        actual_rate, _, extension = _FORMATS[actual_quality]
        if 'formatType' in data and data['formatType'] != actual_rate:
            raise ApiError('DOWNLOAD_INVALID')
        raw_size = data.get('size')
        if not isinstance(raw_size, str) or not re.fullmatch(r'[0-9]{1,12}', raw_size):
            raise ApiError('DOWNLOAD_INVALID')
        size = int(raw_size)
        duration = _duration(item.get('duration') or item.get('length'))
        if not 10 <= duration <= MAX_DURATION or not max(1024, duration * 1000) <= size <= MAX_DOWNLOAD_BYTES:
            raise ApiError('DOWNLOAD_INVALID')
        ffmpeg = paths.find_exe('ffmpeg.exe')
        if not ffmpeg:
            raise ApiError('VALIDATOR_UNAVAILABLE', 503)
        title = item.get('songName') or item.get('name') or sid
        singer = item.get('singer')
        if not isinstance(singer, str):
            singers = item.get('singers') or item.get('singerList')
            singer = '/'.join(v['name'] for v in singers if isinstance(v, dict)
                              and isinstance(v.get('name'), str)) if isinstance(singers, list) else ''
        stem = _safe_stem('%s - %s' % (title, singer) if singer else title)
        name = _stream(data.get('url'), size, stem, extension, duration, ffmpeg)
        return {'name': name, 'size': size, 'quality': actual_quality,
                'quality_name': _QUALITY_NAMES[actual_quality], 'format': extension, 'lyrics': False}
    finally:
        _DOWNLOAD_SLOTS.release()
