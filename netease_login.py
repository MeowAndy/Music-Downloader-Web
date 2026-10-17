"""Browser-driven NetEase QR login; no background polling or public credentials."""
import base64
import re
import secrets
import threading
import time
from dataclasses import dataclass, field

import netease_api

QR_TTL = 180
POLL_INTERVAL = 1.5
MAX_TASKS = 8
_ACTIVE = {'waiting', 'scanned'}


@dataclass
class _LoginTask:
    task_id: str
    key: str = field(repr=False)
    image: str = field(repr=False)
    generation: int
    expires_at: float
    status: str = 'waiting'
    account: dict = field(default_factory=dict)
    error: str = ''
    last_poll: float = float('-inf')
    poll_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def public(self):
        return {'id': self.task_id, 'status': self.status,
                'expires_at': int(self.expires_at),
                'remaining': max(0, int(self.expires_at - time.time())),
                'account': dict(self.account), 'error': self.error}


def _image(value):
    if not isinstance(value, str) or len(value) > 2 * 1024 * 1024:
        raise netease_api.ApiError()
    match = re.fullmatch(r'data:image/(png|jpeg);base64,([A-Za-z0-9+/=\r\n]+)', value)
    if not match:
        raise netease_api.ApiError()
    try:
        raw = base64.b64decode(match[2].replace('\r', '').replace('\n', ''), validate=True)
        if not raw or not (raw.startswith(b'\x89PNG\r\n\x1a\n') or raw.startswith(b'\xff\xd8\xff')):
            raise ValueError()
    except ValueError:
        raise netease_api.ApiError() from None
    return 'data:image/%s;base64,%s' % (match[1], base64.b64encode(raw).decode('ascii'))


class QRLoginManager:
    def __init__(self):
        self._tasks = {}
        self._lock = threading.RLock()
        self._generation = 0

    def _invalidate(self):
        self._generation += 1
        for task in self._tasks.values():
            if task.status in _ACTIVE:
                task.status = 'cancelled'
                task.key = ''
        return self._generation

    def _valid(self, task):
        if task.status in _ACTIVE and time.time() >= task.expires_at:
            task.status = 'expired'
            task.key = ''
        return (self._tasks.get(task.task_id) is task
                and task.generation == self._generation and task.status in _ACTIVE)

    def start(self):
        with self._lock:
            generation = self._invalidate()
        envelope = netease_api.bridge_call('qr_start', {}, cookie='')
        body = envelope['body']
        if 'code' in body and str(body['code']) != '200':
            raise netease_api.ApiError()
        key = body.get('key')
        if not isinstance(key, str) or not key or len(key) > 2048:
            raise netease_api.ApiError()
        image = _image(body.get('image'))
        task = _LoginTask(secrets.token_urlsafe(18), key, image, generation, time.time() + QR_TTL)
        with self._lock:
            if generation != self._generation:
                raise netease_api.ApiError('CANCELLED', 409)
            while len(self._tasks) >= MAX_TASKS:
                self._tasks.pop(next(iter(self._tasks)))
            self._tasks[task.task_id] = task
            data = task.public()
            data['image'] = task.image
            return task, data

    def get(self, task_id):
        with self._lock:
            return self._tasks.get(task_id)

    def status(self, task_id):
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return None
            if not self._valid(task):
                return task.public()
        # Only one browser request may perform upstream polling for a task.
        if not task.poll_lock.acquire(blocking=False):
            with self._lock:
                return task.public()
        try:
            with self._lock:
                if not self._valid(task) or time.monotonic() - task.last_poll < POLL_INTERVAL:
                    return task.public()
                task.last_poll = time.monotonic()
                key = task.key
            try:
                envelope = netease_api.bridge_call('qr_check', {'key': key}, cookie='')
                body = envelope['body']
                code = str(body.get('code'))
                cookie = ''
                account = {}
                if code == '803':
                    cookie = netease_api.parse_cookies(envelope.get('cookies'), body.get('cookie', ''))
                    if not netease_api._has_music_u(cookie):
                        raise netease_api.ApiError('LOGIN_REQUIRED', 401)
                    with self._lock:
                        if not self._valid(task):
                            return task.public()
                    # Account labels are optional; a metadata outage must not discard MUSIC_U.
                    try:
                        metadata = netease_api.bridge_call('login_status', {}, cookie=cookie)['body']
                        data = metadata.get('data', metadata)
                        if isinstance(data, dict) and str(data.get('code', 200)) == '200':
                            account = netease_api._account(data.get('profile') or data.get('account'))
                    except Exception:
                        account = {}
                with self._lock:
                    if not self._valid(task):
                        return task.public()
                    task.error = ''
                    if code == '800':
                        task.status = 'expired'
                        task.key = ''
                    elif code == '801':
                        task.status = 'waiting'
                    elif code == '802':
                        task.status = 'scanned'
                    elif code == '803':
                        # Same lock as refresh/cancel/logout: stale work can never persist.
                        netease_api.set_credentials(cookie, account)
                        task.account = account
                        task.status = 'success'
                        task.key = ''
                    else:
                        task.status = 'failed'
                        task.key = ''
                        task.error = str(netease_api.ApiError())
            except netease_api.ApiError as exc:
                with self._lock:
                    if self._valid(task):
                        task.error = str(exc)
                        if exc.code not in {'NETWORK_ERROR', 'BUSY'}:
                            task.status = 'failed'
                            task.key = ''
            with self._lock:
                return task.public()
        finally:
            task.poll_lock.release()

    def cancel(self, task_id):
        with self._lock:
            task = self._tasks.pop(task_id, None)
            if task is None:
                return False
            if task.generation == self._generation:
                self._invalidate()
            task.status = 'cancelled'
            task.key = ''
            return True

    def logout(self):
        with self._lock:
            self._invalidate()
            self._tasks.clear()
            netease_api.clear_credentials()


manager = QRLoginManager()
