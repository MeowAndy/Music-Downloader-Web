"""NetEase adapter: isolated Node calls, local credentials and verified downloads."""
import hashlib
import ipaddress
import json
import os
import re
import socket
import shutil
import subprocess
import tempfile
import threading
import time
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests

import paths
import owned_processes
import ncm_api

NODE_BRIDGE = paths.RES_DIR / 'vendor' / 'netease_bridge.js'
QUALITY = {'standard': 'standard', 'hq': 'exhigh', 'sq': 'lossless'}
QUALITY_NAMES = {'standard': '标准', 'hq': '高品质', 'sq': '无损'}
_BRIDGE_SLOTS = threading.BoundedSemaphore(4)
_AUTH_LOCK = threading.RLock()
_OUTPUT_LOCK = threading.Lock()
MAX_DOWNLOAD_BYTES = 1024 * 1024 * 1024
MAX_DOWNLOAD_SECONDS = 300
_CDN_SUFFIXES = ('music.126.net', 'music.163.com', 'vod.126.net')
_COOKIE_NAMES = {'MUSIC_U', '__csrf', 'NMTID', 'MUSIC_A', '_ntes_nuid',
                 '_ntes_nnid', 'WNMCID', 'os', 'appver'}
_MESSAGES = {
    'INVALID_INPUT': '网易云请求参数无效。',
    'NETWORK_ERROR': '网易云连接失败，请检查网络后重试。',
    'UPSTREAM_ERROR': '网易云返回异常，请稍后重试。',
    'LOGIN_REQUIRED': '请先使用自己的网易云音乐账号扫码登录。',
    'PERMISSION_DENIED': '当前账号没有此歌曲的下载权限。',
    'DOWNLOAD_UNAVAILABLE': '网易云未提供可下载的完整音频。',
    'UNSAFE_URL': '音频地址不在允许的网易云音乐 CDN 范围内。',
    'DOWNLOAD_INVALID': '音频下载不完整或校验失败。',
    'STORAGE_ERROR': '无法保存网易云音乐数据，请检查本地目录权限。',
    'BUSY': '网易云请求繁忙，请稍后重试。',
    'CANCELLED': '二维码登录已取消，请重新获取二维码。',
}


class ApiError(RuntimeError):
    def __init__(self, code='UPSTREAM_ERROR', status=502):
        self.code = code if code in _MESSAGES else 'UPSTREAM_ERROR'
        self.status = status
        super().__init__(_MESSAGES[self.code])


def _auth_path():
    return Path(paths.APP_DIR) / 'netease-auth.json'


def parse_cookies(headers=None, body_cookie=''):
    """Read cookie pairs, never Set-Cookie attributes or arbitrary body fields."""
    out = {}
    values = list(headers) if isinstance(headers, (list, tuple)) else []
    if isinstance(body_cookie, str) and len(body_cookie) <= 65536:
        values.append(body_cookie)
    for value in values:
        if not isinstance(value, str) or len(value) > 65536 or '\r' in value or '\n' in value:
            continue
        try:
            jar = SimpleCookie()
            jar.load(value)
            for name, morsel in jar.items():
                if name in _COOKIE_NAMES and morsel.value:
                    out[name] = morsel.value
        except Exception:
            continue
    jar = SimpleCookie()
    for name, value in out.items():
        jar[name] = value
    return '; '.join(m.OutputString() for m in jar.values())


def _has_music_u(cookie):
    try:
        jar = SimpleCookie()
        jar.load(cookie)
        return bool(jar.get('MUSIC_U') and jar['MUSIC_U'].value)
    except Exception:
        return False


def _account(value):
    if not isinstance(value, dict):
        return {}
    result = {}
    uid = str(value.get('id') or value.get('userId') or '')
    if re.fullmatch(r'[0-9]{1,30}', uid):
        result['id'] = uid
    nickname = value.get('nickname')
    if isinstance(nickname, str):
        result['nickname'] = ''.join(c for c in nickname if c.isprintable())[:80]
    return result


def _load_credentials():
    with _AUTH_LOCK:
        try:
            file = _auth_path()
            if file.stat().st_size > 131072:
                return {'cookie': '', 'account': {}}
            data = json.loads(file.read_text(encoding='utf-8'))
            cookie = parse_cookies(body_cookie=data.get('cookie', ''))
            if _has_music_u(cookie):
                return {'cookie': cookie, 'account': _account(data.get('account'))}
        except (OSError, ValueError, TypeError, AttributeError):
            pass
    return {'cookie': '', 'account': {}}


def get_cookie():
    return _load_credentials()['cookie']


def credential_status():
    data = _load_credentials()
    return {'set': bool(data['cookie']), 'account': data['account']}


def set_credentials(cookie, account=None):
    cookie = parse_cookies(body_cookie=cookie)
    if not _has_music_u(cookie):
        raise ApiError('LOGIN_REQUIRED', 401)
    with _AUTH_LOCK:
        temp = None
        try:
            file = _auth_path()
            file.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=file.parent,
                                             prefix='.netease-auth-', suffix='.tmp', delete=False) as out:
                temp = out.name
                os.chmod(temp, 0o600)
                json.dump({'cookie': cookie, 'account': _account(account)}, out, ensure_ascii=False)
                out.flush()
                os.fsync(out.fileno())
            os.replace(temp, file)
        except OSError:
            raise ApiError('STORAGE_ERROR', 500) from None
        finally:
            if temp:
                try:
                    os.unlink(temp)
                except OSError:
                    pass


def clear_credentials():
    with _AUTH_LOCK:
        try:
            _auth_path().unlink(missing_ok=True)
        except OSError:
            raise ApiError('STORAGE_ERROR', 500) from None


def bridge_call(op, args=None, cookie=None):
    if not isinstance(op, str) or op not in {'search', 'detail', 'download', 'qr_start', 'qr_check', 'login_status', 'lyric'} or not isinstance(args if args is not None else {}, dict):
        raise ApiError('INVALID_INPUT', 400)
    jar = SimpleCookie()
    jar.load(parse_cookies(body_cookie=get_cookie() if cookie is None else cookie))
    payload = {'op': op, 'args': args or {}, 'cookie': {name: morsel.value for name, morsel in jar.items()}}
    if not _BRIDGE_SLOTS.acquire(timeout=45):
        raise ApiError('BUSY', 503)
    try:
        with tempfile.TemporaryDirectory(prefix='qmkg-netease-') as private:
            # The upstream module reads this at import; never reuse a user's token.
            Path(private, 'anonymous_token').write_text('', encoding='utf-8')
            env = os.environ.copy()
            env.pop('NETEASE_COOKIE', None)
            env.update(TMP=private, TEMP=private, TMPDIR=private)
            result = owned_processes.run(
                [paths.node_exe(), str(NODE_BRIDGE)],
                input=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
                capture_output=True, timeout=45, cwd=private, env=env,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            )
        if result.returncode != 0 or len(result.stdout) > 8 * 1024 * 1024:
            raise ApiError()
        data = json.loads(result.stdout.decode('utf-8'))
        if not isinstance(data, dict):
            raise ApiError()
        if data.get('ok') is not True:
            code = data.get('code')
            if code not in {'NETWORK_ERROR', 'UPSTREAM_ERROR', 'INVALID_INPUT'}:
                code = 'UPSTREAM_ERROR'
            raise ApiError(code, 400 if code == 'INVALID_INPUT' else 502)
        if not isinstance(data.get('body'), dict):
            raise ApiError()
        return data
    except ApiError:
        raise
    except (subprocess.SubprocessError, OSError):
        raise ApiError('NETWORK_ERROR') from None
    except (ValueError, TypeError, UnicodeError):
        raise ApiError() from None
    finally:
        _BRIDGE_SLOTS.release()


def _body(op, args, cookie=None):
    body = bridge_call(op, args, cookie)['body']
    if str(body.get('code')) != '200':
        raise ApiError('LOGIN_REQUIRED' if str(body.get('code')) == '301' else 'UPSTREAM_ERROR',
                       401 if str(body.get('code')) == '301' else 502)
    return body


def _id(value):
    if isinstance(value, bool) or not re.fullmatch(r'[1-9][0-9]{0,19}', str(value)):
        raise ApiError('INVALID_INPUT', 400)
    return str(value)


def _number(value, default=0):
    try:
        return int(value) if not isinstance(value, bool) else default
    except (ValueError, TypeError, OverflowError):
        return default


def _singer(song):
    artists = song.get('ar') or song.get('artists') or []
    if not isinstance(artists, list):
        return ''
    return '/'.join(str(a.get('name') or '') for a in artists if isinstance(a, dict))


def search(kw, num=30):
    if not isinstance(kw, str) or not kw.strip() or len(kw) > 200:
        raise ApiError('INVALID_INPUT', 400)
    if isinstance(num, bool) or not isinstance(num, int) or not 1 <= num <= 100:
        raise ApiError('INVALID_INPUT', 400)
    body = _body('search', {'keywords': kw.strip(), 'limit': num})
    result = body.get('result')
    if not isinstance(result, dict):
        raise ApiError()
    songs = result.get('songs', [])
    if not isinstance(songs, list):
        raise ApiError()
    out = []
    for song in songs[:num]:
        if not isinstance(song, dict):
            continue
        try:
            sid = _id(song.get('id'))
        except ApiError:
            continue
        privilege = song.get('privilege') or {}
        if not isinstance(privilege, dict):
            privilege = {}
        album = song.get('al') or song.get('album') or {}
        out.append({'source': 'netease', 'id': sid, 'name': str(song.get('name') or ''),
                    'singer': _singer(song), 'album': str(album.get('name') or '') if isinstance(album, dict) else '',
                    'duration': max(0, _number(song.get('dt', song.get('duration')))) / 1000,
                    'fee': _number(song.get('fee')),
                    'rights_hint': ('当前账号可下载（以实际接口为准）' if _number(privilege.get('dl')) > 0
                                    else '下载可能需要登录或会员'),
                    'rights': {'download_hint': _number(privilege.get('dl')) > 0,
                               'requires_verification': True}})
    return out


def _safe_url(url):
    try:
        if not isinstance(url, str) or len(url) > 8192 or any(c.isspace() or ord(c) < 32 for c in url):
            raise ValueError()
        parts = urlsplit(url)
        host = (parts.hostname or '').lower()
        if (parts.scheme not in ('http', 'https') or parts.username is not None or parts.password is not None
                or parts.port not in (None, 80, 443) or not any(host == suffix or host.endswith('.' + suffix) for suffix in _CDN_SUFFIXES)):
            raise ValueError()
        addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
            raise ValueError()
        return urlunsplit(('https', host, parts.path or '/', parts.query, ''))
    except (ValueError, OSError):
        raise ApiError('UNSAFE_URL', 502) from None


def _safe_stem(value):
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', '_', str(value)).strip(' .')[:160].rstrip(' .')
    if not value:
        value = 'NetEase'
    if re.match(r'^(CON|PRN|AUX|NUL|COM[0-9¹²³]|LPT[0-9¹²³])(?:\.|$)', value, re.I):
        value = '_' + value
    return value


def _commit_download(part, stem, extension):
    directory = Path(paths.OUTPUT_DIR)
    with _OUTPUT_LOCK:
        for n in range(10000):
            name = '%s%s.%s' % (stem, ' (%s)' % n if n else '', extension)
            target = directory / name
            try:
                # Reserve a unique name even when another thread chooses the same title.
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                continue
            os.close(fd)
            try:
                os.replace(part, target)
            except OSError:
                target.unlink(missing_ok=True)
                raise
            return name
    raise ApiError('STORAGE_ERROR', 500)


def _stream(url, size, md5, stem, extension):
    part = None
    response = None
    try:
        directory = Path(paths.OUTPUT_DIR)
        directory.mkdir(parents=True, exist_ok=True)
        with requests.Session() as session:
            session.trust_env = False
            session.headers.clear()
            session.headers.update({'User-Agent': 'MusicDownloaderWeb/1.0', 'Accept-Encoding': 'identity'})
            session.auth = None
            deadline = time.monotonic() + MAX_DOWNLOAD_SECONDS
            for _ in range(6):
                url = _safe_url(url)
                session.cookies.clear()
                response = session.get(url, stream=True, timeout=(10, 30), allow_redirects=False)
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get('Location', '')
                    response.close()
                    if not location:
                        raise ApiError('UNSAFE_URL')
                    url = urljoin(url, location)
                    continue
                break
            else:
                raise ApiError('UNSAFE_URL')
            if response.status_code != 200:
                raise ApiError('NETWORK_ERROR')
            length = response.headers.get('Content-Length')
            if length is not None and _number(length, -1) != size:
                raise ApiError('DOWNLOAD_INVALID')
            if response.headers.get('Content-Encoding', 'identity').lower() not in ('', 'identity'):
                raise ApiError('DOWNLOAD_INVALID')
            digest = hashlib.md5()
            total = 0
            with tempfile.NamedTemporaryFile(dir=directory, prefix='.netease-', suffix='.part', delete=False) as out:
                part = out.name
                for chunk in response.iter_content(chunk_size=128 * 1024):
                    if time.monotonic() > deadline:
                        raise ApiError('NETWORK_ERROR')
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > size or total > MAX_DOWNLOAD_BYTES:
                        raise ApiError('DOWNLOAD_INVALID')
                    digest.update(chunk)
                    out.write(chunk)
                if total != size or (md5 and digest.hexdigest().lower() != md5.lower()):
                    raise ApiError('DOWNLOAD_INVALID')
                out.flush()
                os.fsync(out.fileno())
        detected_ncm = ncm_api.is_ncm(part)
        if extension == 'ncm' and not detected_ncm:
            raise ApiError('DOWNLOAD_INVALID')
        return _commit_download(part, stem, 'ncm' if detected_ncm else extension)
    except ApiError:
        raise
    except requests.RequestException:
        raise ApiError('NETWORK_ERROR') from None
    except OSError:
        raise ApiError('STORAGE_ERROR', 500) from None
    finally:
        if response is not None:
            response.close()
        if part:
            try:
                os.unlink(part)
            except OSError:
                pass


def download(id, quality='sq'):
    sid = _id(id)
    if not isinstance(quality, str) or quality not in QUALITY:
        raise ApiError('INVALID_INPUT', 400)
    cookie = get_cookie()
    detail = _body('detail', {'ids': sid}, cookie)
    songs = detail.get('songs')
    privileges = detail.get('privileges')
    if not isinstance(songs, list) or not isinstance(privileges, list):
        raise ApiError()
    song = next((s for s in songs if isinstance(s, dict) and str(s.get('id')) == sid), None)
    privilege = next((p for p in privileges if isinstance(p, dict) and str(p.get('id')) == sid), None)
    if song is None or privilege is None:
        raise ApiError()
    if _number(privilege.get('dl')) <= 0:
        raise ApiError('PERMISSION_DENIED' if _has_music_u(cookie) else 'LOGIN_REQUIRED',
                       403 if _has_music_u(cookie) else 401)
    body = _body('download', {'id': sid, 'level': QUALITY[quality]}, cookie)
    data = body.get('data')
    if isinstance(data, list):
        data = next((x for x in data if isinstance(x, dict) and str(x.get('id')) == sid), None)
    if not isinstance(data, dict) or ('id' in data and str(data['id']) != sid):
        raise ApiError('DOWNLOAD_UNAVAILABLE', 403)
    if str(data.get('code')) in {'-105', '301', '401', '403'}:
        raise ApiError('PERMISSION_DENIED' if _has_music_u(cookie) else 'LOGIN_REQUIRED',
                       403 if _has_music_u(cookie) else 401)
    if str(data.get('code')) != '200' or not isinstance(data.get('url'), str) or not data['url'] or data.get('freeTrialInfo') is not None:
        raise ApiError('DOWNLOAD_UNAVAILABLE', 403)
    size = _number(data.get('size'))
    if not 0 < size <= MAX_DOWNLOAD_BYTES:
        raise ApiError('DOWNLOAD_INVALID')
    md5 = data.get('md5') or ''
    if not isinstance(md5, str) or not re.fullmatch(r'[a-fA-F0-9]{32}', md5):
        raise ApiError('DOWNLOAD_INVALID')
    extension = str(data.get('type') or '').lower()
    if extension not in {'mp3', 'flac', 'm4a', 'aac', 'wav', 'ncm'}:
        raise ApiError('DOWNLOAD_UNAVAILABLE')
    level = data.get('level') or QUALITY[quality]
    actual_quality = next((q for q, v in QUALITY.items() if v == level), None)
    if actual_quality is None:
        raise ApiError('DOWNLOAD_UNAVAILABLE')
    title = str(song.get('name') or sid)
    singer = _singer(song)
    stem = _safe_stem('%s - %s' % (title, singer) if singer else title)
    name = _stream(data['url'], size, md5, stem, extension)
    result = {'name': name, 'size': size, 'quality': actual_quality,
              'quality_name': QUALITY_NAMES[actual_quality], 'lyrics': False}
    if Path(name).suffix.lower() == '.ncm':
        source = Path(paths.OUTPUT_DIR) / name
        try:
            with ncm_api.extracted_audio(source) as audio:
                converted_size = audio.stat().st_size
                # The converter uses a different filesystem for its private workspace.
                with tempfile.NamedTemporaryFile(dir=paths.OUTPUT_DIR, prefix='.netease-',
                                                 suffix='.part', delete=False) as out:
                    converted_part = Path(out.name)
                    try:
                        with audio.open('rb') as stream:
                            shutil.copyfileobj(stream, out)
                        out.flush()
                        os.fsync(out.fileno())
                    except BaseException:
                        out.close()
                        converted_part.unlink(missing_ok=True)
                        raise
                try:
                    converted_name = _commit_download(converted_part, stem, audio.suffix[1:].lower())
                finally:
                    converted_part.unlink(missing_ok=True)
            result.update(name=converted_name, size=converted_size, converted=True,
                          format=Path(converted_name).suffix[1:], original_name=name)
        except (ncm_api.ConversionError, OSError, ApiError):
            result['conversion_error'] = 'NCM 文件已保存，但转换失败；请检查转换工具和 FFmpeg 后重试。'
    return result
