"""Official Migu web QR authentication, memory only (restart requires a new scan).

No network, files, workers or credentials are loaded at import. QR_TTL is our
conservative local deadline, not a claim about passport's server-side lifetime.
Only a successful official token exchange AND checkPacMtoken can install auth.
"""
import base64
import binascii
import http.client
import io
import ipaddress
import json
import re
import secrets
import socket
import ssl
import threading
import time
import unicodedata
import uuid
import warnings
from dataclasses import dataclass, field
from http.cookies import SimpleCookie
from urllib.parse import urlencode, urlsplit

from flask import Blueprint, jsonify, request

PASSPORT = 'https://passport.migu.cn'
MUSIC = 'https://c.musicapp.migu.cn'
CREATE_URL = PASSPORT + '/api/qrcWeb/qrcLogin?sourceID=220029'
POLL_URL = PASSPORT + '/api/qrcWeb/qrcquery'
EXCHANGE_URL = MUSIC + '/user/h5/token-validate/v3.0'
CHECK_URL = MUSIC + '/mgateway/api/checkPacMtoken'
INFO_URL = MUSIC + '/user/h5/user-info/v1.0'
LOGOUT_URL = MUSIC + '/mgateway/api/clearPacMtoken'
DOWNLOAD_URL = 'https://app.c.nf.migu.cn/MIGUM2.0/strategy/download-url/by-songid/v1.0'
QR_TTL = 180
POLL_INTERVAL = 1.0  # No more than one upstream poll per second per task.
MAX_TASKS = 8
MAX_OWNERS = 32
OWNER_TTL = 12 * 3600
MAX_BODY = 1024 * 1024
MAX_IMAGE = 512 * 1024
COOKIE = 'migu_login_owner'
PREFIX = '/api/migu/login'
_ACTIVE = {'waiting', 'scanned'}
_MESSAGES = {'waiting': '请使用咪咕官方客户端扫码；二维码采用本地保守有效期。',
             'scanned': '已扫码，请在咪咕客户端确认。', 'success': '咪咕登录成功；重启后需重新扫码。',
             'expired': '二维码已过期，请重新获取。', 'cancelled': '已取消登录。',
             'error': '咪咕登录失败，请重新获取二维码。'}
_ERRORS = {'DEPENDENCY': '咪咕二维码校验需要 Pillow，请安装后重试。',
           'UPSTREAM': '咪咕登录服务返回无效数据，请稍后重试。',
           'NETWORK': '咪咕登录服务连接失败，请稍后重试。',
           'BUSY': '咪咕登录请求繁忙，请稍后重试。', 'CANCELLED': '登录请求已取消。',
           'FORBIDDEN': '登录请求校验失败，请刷新后重试。', 'NOT_FOUND': '登录请求不存在。',
           'INVALID': '登录请求参数无效。', 'LOGIN_REQUIRED': '咪咕登录已失效，请重新扫码。'}
_WIRE_SLOTS = threading.BoundedSemaphore(4)


class LoginError(RuntimeError):
    def __init__(self, code='UPSTREAM', status=502):
        self.code = code if code in _ERRORS else 'UPSTREAM'
        self.status = status
        super().__init__(_ERRORS[self.code])


def _secret(value):
    # Opaque ASCII tokens only; disallow header injection and unbounded input.
    if not isinstance(value, str) or not re.fullmatch(r'[\x21-\x7e]{1,8192}', value):
        raise LoginError()
    return value


def _remaining(deadline):
    left = deadline - time.monotonic()
    if left <= 0:
        raise LoginError('NETWORK')
    return left


class _HTTPS(http.client.HTTPSConnection):
    """Pin all DNS results after rejecting nonpublic addresses; verify TLS SNI."""
    def __init__(self, host, rows, deadline):
        super().__init__(host, timeout=min(6, _remaining(deadline)),
                         context=ssl.create_default_context())
        self.set_debuglevel(0)  # Override even a globally enabled HTTP debug level.
        self._rows, self._deadline = rows, deadline

    def connect(self):
        for family, kind, protocol, _, address in self._rows:
            sock = socket.socket(family, kind, protocol)
            try:
                sock.settimeout(min(6, _remaining(self._deadline)))
                sock.connect(address)
                self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
                return
            except Exception:
                sock.close()
        raise LoginError('NETWORK')


def _wire(method, url, *, form=None, token='', device='', cookie=''):
    """Private no-proxy/no-redirect/no-urllib3 transport; never log request targets.

    Only fixed official transactions are allowed. Passport cookies cannot cross
    into music/download transactions. DNS is resolved once and pinned to TLS.
    """
    connection = timer = None
    if not _WIRE_SLOTS.acquire(blocking=False):
        raise LoginError('BUSY', 503)
    try:
        p = urlsplit(url)
        fixed = url.split('?', 1)[0]
        allowed = {CREATE_URL.split('?', 1)[0], POLL_URL, EXCHANGE_URL,
                   CHECK_URL, INFO_URL, LOGOUT_URL, DOWNLOAD_URL}
        if fixed not in allowed or p.fragment or p.username or p.password:
            raise LoginError()
        passport = fixed.startswith(PASSPORT + '/')
        if ((passport and (token or device)) or (not passport and cookie)
                or (method != ('POST' if passport else 'GET'))):
            raise LoginError()
        rows = socket.getaddrinfo(p.hostname, 443, type=socket.SOCK_STREAM)
        if not rows:
            raise LoginError()
        for row in rows:
            address = ipaddress.ip_address(row[4][0])
            address = getattr(address, 'ipv4_mapped', None) or address
            if not address.is_global or address.is_multicast:
                raise LoginError()
        deadline = time.monotonic() + 15
        connection = _HTTPS(p.hostname, rows, deadline)

        def interrupt():
            sock = connection.sock
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                connection.close()

        timer = threading.Timer(_remaining(deadline), interrupt)
        timer.daemon = True
        timer.start()
        headers = {'User-Agent': 'Mozilla/5.0', 'Accept-Encoding': 'identity',
                   'Connection': 'close', 'Accept': 'application/json',
                   'Referer': (PASSPORT + '/login?sourceid=220029&callbackURL=PostToken'
                               if passport else 'https://m.music.migu.cn/'),
                   'Origin': PASSPORT if passport else 'https://m.music.migu.cn'}
        if not passport:
            headers.update({'channel': '014X031', 'subchannel': '014X031',
                            'activityId': 'MUSIC-WWW', 'version': '6.8.8', 'platform': 'H5'})
        if token:
            headers['pacmtoken'] = _secret(token)
        if device:
            headers['deviceId'] = str(uuid.UUID(device))
        if cookie:
            headers['Cookie'] = 'JSESSIONID=' + _secret(cookie)
        payload = None
        if form is not None:
            payload = urlencode(form).encode('ascii')
            headers['Content-Type'] = 'application/x-www-form-urlencoded; charset=UTF-8'
        connection.request(method, p.path + ('?' + p.query if p.query else ''), payload, headers)
        response = connection.getresponse()
        if response.status != 200:  # In particular, never follow a redirect carrying secrets.
            raise LoginError('NETWORK')
        if response.getheader('Content-Encoding', 'identity').lower() not in ('', 'identity'):
            raise LoginError()
        length = response.getheader('Content-Length')
        if length is not None and (not re.fullmatch(r'[0-9]{1,9}', length) or int(length) > MAX_BODY):
            raise LoginError()
        raw = bytearray()
        while True:
            _remaining(deadline)
            if connection.sock is not None:
                connection.sock.settimeout(min(6, _remaining(deadline)))
            chunk = response.read1(65536)
            if not chunk:
                break
            raw.extend(chunk)
            if len(raw) > MAX_BODY:
                raise LoginError()
        _remaining(deadline)
        if length is not None and len(raw) != int(length):
            raise LoginError()
        return bytes(raw), response.getheaders()
    except LoginError:
        raise
    except Exception:
        raise LoginError('NETWORK') from None
    finally:
        if timer is not None:
            timer.cancel()
        if connection is not None:
            connection.close()
        _WIRE_SLOTS.release()


def _json(method, url, **kwargs):
    raw, headers = _wire(method, url, **kwargs)
    try:
        body = json.loads(raw.decode('utf-8-sig'))
        if not isinstance(body, dict):
            raise ValueError()
        return body, headers
    except (ValueError, UnicodeError):
        raise LoginError() from None


def _header(headers, name):
    values = [v for k, v in headers if k.lower() == name.lower()]
    if len(values) > 1:
        raise LoginError()
    return values[0] if values else ''


def _passport_cookie(headers, old=''):
    for key, value in headers:
        if key.lower() == 'set-cookie':
            jar = SimpleCookie()
            try:
                jar.load(value)
                if 'JSESSIONID' in jar:
                    old = _secret(jar['JSESSIONID'].value)
            except Exception:
                raise LoginError() from None
    return old


def _image(value):
    """The live official response is a JPEG data URL, not a remote image URL.

    Reject remote URLs (no unobserved image-host allowlist), fully decode a bounded
    raster, discard metadata and re-encode PNG before exposing it to the browser.
    """
    try:
        from PIL import Image
    except ImportError:
        raise LoginError('DEPENDENCY', 503) from None
    try:
        if not isinstance(value, str) or len(value) > MAX_IMAGE * 2:
            raise ValueError()
        match = re.fullmatch(r'data:image/(?:jpeg|png);base64,([A-Za-z0-9+/=]+)', value)
        if not match:
            raise ValueError()
        raw = base64.b64decode(match[1], validate=True)
        if not 32 <= len(raw) <= MAX_IMAGE:
            raise ValueError()
        with warnings.catch_warnings():
            warnings.simplefilter('error')
            with Image.open(io.BytesIO(raw)) as image:
                if (image.format not in {'JPEG', 'PNG'} or getattr(image, 'n_frames', 1) != 1
                        or not all(64 <= n <= 1024 for n in image.size)):
                    raise ValueError()
                image.load()
                output = io.BytesIO()
                clean = image.convert('RGB')
                clean.info.clear()
                clean.save(output, format='PNG')
        if len(output.getvalue()) > MAX_IMAGE:
            raise ValueError()
        return 'data:image/png;base64,' + base64.b64encode(output.getvalue()).decode('ascii')
    except Exception:
        raise LoginError() from None


def _nickname(value):
    if not isinstance(value, str):
        return ''
    return ''.join(c for c in value if not unicodedata.category(c).startswith('C')
                   and c not in '<>&').strip()[:80]


@dataclass(frozen=True, repr=False)
class _Auth:
    token: str
    generation: int
    device: str


@dataclass(repr=False)
class _Task:
    id: str
    owner: str
    generation: int
    expires: float
    key: str = ''
    cookie: str = ''
    device: str = field(default_factory=lambda: str(uuid.uuid4()))
    status: str = 'waiting'
    stop: threading.Event = field(default_factory=threading.Event)


class QRLoginManager:
    def __init__(self):
        self._lock = threading.RLock()
        self._tasks = {}
        self._generation = 0
        self._auth = None
        self._nickname = ''
        # No executor queue: at most two outstanding create/poll/exchange workers.
        self._workers = threading.BoundedSemaphore(2)

    def get_auth(self):
        with self._lock:
            return self._auth or ''

    def clear_matching(self, auth):
        with self._lock:
            if auth and self._auth is auth:
                self._auth = None
                self._nickname = ''
                self._invalidate()
                return True
            return False

    def account_status(self):
        with self._lock:
            return {'ok': True, 'set': self._auth is not None,
                    'nickname': self._nickname, 'persistent': False}

    def _finish(self, task, status):
        task.status = status
        if status not in _ACTIVE:
            task.key = task.cookie = ''
            task.stop.set()

    def _invalidate(self):
        self._generation += 1
        for task in self._tasks.values():
            if task.status in _ACTIVE:
                self._finish(task, 'cancelled')
        return self._generation

    def _valid(self, task):
        if task.status in _ACTIVE and time.monotonic() >= task.expires:
            self._finish(task, 'expired')
        return (task.generation == self._generation and task.status in _ACTIVE
                and self._tasks.get(task.id) is task)

    def _public(self, task):
        return {'ok': True, 'id': task.id, 'status': task.status,
                'message': _MESSAGES[task.status]}

    def start(self, owner):
        if not self._workers.acquire(blocking=False):
            raise LoginError('BUSY', 503)
        handed_off = False
        try:
            with self._lock:
                generation = self._invalidate()
                # Account switch immediately drops the previous local credential.
                self._auth = None
                self._nickname = ''
                while len(self._tasks) >= MAX_TASKS:
                    self._tasks.pop(next(iter(self._tasks)))
                task = _Task(secrets.token_urlsafe(24), owner, generation,
                             time.monotonic() + QR_TTL)
                self._tasks[task.id] = task
            body, headers = _json('POST', CREATE_URL,
                                  form={'isAsync': 'true', 'sourceid': '220029'})
            result = body.get('result')
            if str(body.get('status')) != '2000' or not isinstance(result, dict):
                raise LoginError()
            key = _secret(result.get('qrc_sessionid'))
            cookie = _passport_cookie(headers)
            image = _image(result.get('qrcUrl'))
            with self._lock:
                if not self._valid(task):
                    raise LoginError('CANCELLED', 409)
                task.key, task.cookie = key, cookie
                # First poll is delayed, subsequent polls are at least one second apart.
                worker = threading.Thread(target=self._run, args=(task,), daemon=True,
                                          name='migu-qr')
                worker.start()
                handed_off = True
                return {'ok': True, 'id': task.id, 'qr': image,
                        'expires_in': max(0, int(task.expires - time.monotonic())),
                        'status': 'waiting'}
        except Exception as exc:
            with self._lock:
                if 'task' in locals() and self._valid(task):
                    self._finish(task, 'error')
            if isinstance(exc, LoginError):
                raise
            raise LoginError() from None
        finally:
            if not handed_off:
                self._workers.release()

    def _run(self, task):
        try:
            while not task.stop.wait(POLL_INTERVAL):
                with self._lock:
                    if not self._valid(task):
                        return
                    key, cookie = task.key, task.cookie
                body, headers = _json('POST', POLL_URL,
                                      form={'isAsync': 'true', 'sourceid': '220029',
                                            'qrc_sessionid': key}, cookie=cookie)
                with self._lock:
                    if not self._valid(task):
                        return
                    task.cookie = _passport_cookie(headers, task.cookie)
                    code = str(body.get('status'))
                    if code in {'4074', '4073'}:
                        task.status = 'scanned' if code == '4073' else 'waiting'
                        continue
                    if code == '4072':
                        self._finish(task, 'expired')
                        return
                    if code != '2000' or not isinstance(body.get('result'), dict):
                        raise LoginError()
                    token = _secret(body['result'].get('token'))
                auth, nickname = self._exchange(task, token)
                with self._lock:
                    if self._valid(task):
                        self._auth, self._nickname = auth, nickname
                        self._finish(task, 'success')
                return
        except Exception:
            # Never expose upstream bodies, exception strings, tokens or QR keys.
            with self._lock:
                if self._valid(task):
                    self._finish(task, 'error')
        finally:
            self._workers.release()

    def _exchange(self, task, token):
        body, headers = _json('GET', EXCHANGE_URL + '?' + urlencode({
            'token': token, 'type': '2', 'sourceId': '220029', 'activityId': 'MUSIC-WWW'}),
                              device=task.device)
        if body.get('code') != '000000' or not isinstance(body.get('data'), dict) or not body['data']:
            raise LoginError()
        pacmtoken = _secret(_header(headers, 'pacmtoken'))
        with self._lock:
            if not self._valid(task):
                raise LoginError('CANCELLED', 409)
        body, headers = _json('GET', CHECK_URL, token=pacmtoken, device=task.device)
        if body.get('code') != '000000':
            raise LoginError('LOGIN_REQUIRED', 401)
        replacement = _header(headers, 'pacmtoken')
        if replacement:
            pacmtoken = _secret(replacement)
        with self._lock:
            if not self._valid(task):
                raise LoginError('CANCELLED', 409)
        nickname = ''
        try:
            body, _ = _json('GET', INFO_URL, token=pacmtoken, device=task.device)
            if body.get('code') == '000000' and isinstance(body.get('data'), dict):
                nickname = _nickname(body['data'].get('nickName'))
        except LoginError:
            pass  # A label outage cannot turn a checked credential into a fake failure.
        return _Auth(pacmtoken, task.generation, task.device), nickname

    def task_status(self, owner, task_id):
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None or task.owner != owner:
                raise LoginError('NOT_FOUND', 404)
            self._valid(task)
            return self._public(task)

    def cancel(self, owner, task_id):
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None or task.owner != owner:
                raise LoginError('NOT_FOUND', 404)
            if task.generation == self._generation:
                self._invalidate()
            if task.status in _ACTIVE:
                self._finish(task, 'cancelled')
            return self._public(task)

    def logout(self):
        with self._lock:
            auth = self._auth
            self._auth = None
            self._nickname = ''
            self._invalidate()
        # Local invalidation happens before I/O and always succeeds, including offline.
        if auth:
            try:
                _json('GET', LOGOUT_URL, token=auth.token, device=auth.device)
            except LoginError:
                pass
        return {'ok': True, 'set': False, 'persistent': False}


manager = QRLoginManager()


def get_auth():
    """Private atomic immutable snapshot for the explicit download transaction only."""
    return manager.get_auth()


def authenticated_read(url, params, auth):
    if (url != DOWNLOAD_URL or not isinstance(auth, _Auth)
            or manager.get_auth() is not auth):
        raise LoginError('LOGIN_REQUIRED', 401)
    raw, _ = _wire('GET', url + '?' + urlencode(params or {}),
                   token=auth.token, device=auth.device)
    try:
        body = json.loads(raw.decode('utf-8-sig'))
        # 850001 was observed from the official checkPacMtoken without auth.
        # Do not reinterpret membership, copyright or arbitrary failures as expiry.
        if isinstance(body, dict) and body.get('code') == '850001':
            manager.clear_matching(auth)
            raise LoginError('LOGIN_REQUIRED', 401)
    except (ValueError, UnicodeError):
        raise LoginError() from None
    return raw


blueprint = Blueprint('migu_login', __name__, url_prefix=PREFIX)
_owners = {}
_owner_lock = threading.Lock()


def _loopback_guard():
    try:
        host = urlsplit(request.host_url).hostname
        if host not in {'localhost', '127.0.0.1', '::1'}:
            raise ValueError()
        if request.remote_addr and not ipaddress.ip_address(request.remote_addr).is_loopback:
            raise ValueError()
        origin = request.headers.get('Origin')
        if origin is not None and origin != request.host_url.rstrip('/'):
            raise ValueError()
        if request.headers.get('Sec-Fetch-Site', '') not in {'', 'same-origin', 'none'}:
            raise ValueError()
    except ValueError:
        raise LoginError('FORBIDDEN', 403) from None


def _owner(create=False):
    _loopback_guard()
    value = request.cookies.get(COOKIE, '')
    now = time.monotonic()
    with _owner_lock:
        for key in list(_owners):
            if now - _owners[key][1] >= OWNER_TTL:
                del _owners[key]
        entry = _owners.get(value)
        fresh = False
        if entry is None:
            if not create:
                raise LoginError('FORBIDDEN', 403)
            if len(_owners) >= MAX_OWNERS:
                raise LoginError('BUSY', 503)
            value, fresh = secrets.token_urlsafe(32), True
            entry = (secrets.token_urlsafe(32), now)
        _owners[value] = (entry[0], now)
        if request.method == 'POST':
            supplied = request.headers.get('X-CSRF-Token', '')
            if (not re.fullmatch(r'[A-Za-z0-9_-]{43}', supplied)
                    or not secrets.compare_digest(supplied, entry[0])):
                raise LoginError('FORBIDDEN', 403)
        return value, entry[0], fresh


@blueprint.after_request
def _private_response(response):
    response.headers['Cache-Control'] = 'no-store, private'
    response.headers['Pragma'] = 'no-cache'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Cross-Origin-Resource-Policy'] = 'same-origin'
    return response


@blueprint.errorhandler(LoginError)
def _error(exc):
    return jsonify(ok=False, code=exc.code, message=str(exc)), exc.status


@blueprint.get('/status')
def status():
    owner, csrf, fresh = _owner(create=True)
    response = jsonify(**manager.account_status(), csrf=csrf)
    if fresh:
        response.set_cookie(COOKIE, owner, httponly=True, samesite='Strict',
                            secure=request.is_secure, path=PREFIX)
    return response


@blueprint.post('/qr/start')
def qr_start():
    owner, _, _ = _owner()
    return jsonify(manager.start(owner))


def _task_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{32}', value):
        raise LoginError('INVALID', 400)
    return value


@blueprint.get('/qr/status')
def qr_status():
    owner, _, _ = _owner()
    return jsonify(manager.task_status(owner, _task_id(request.args.get('id'))))


@blueprint.post('/qr/cancel')
def qr_cancel():
    owner, _, _ = _owner()
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise LoginError('INVALID', 400)
    return jsonify(manager.cancel(owner, _task_id(body.get('id'))))


@blueprint.post('/logout')
def logout():
    _owner()
    return jsonify(manager.logout())
