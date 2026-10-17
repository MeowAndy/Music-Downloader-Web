# -*- coding: utf-8 -*-
"""QQ音乐手机客户端二维码登录适配。

QQ音乐没有面向第三方的稳定扫码登录 SDK，本模块把当前网页使用的
手机客户端二维码流程隔离在 Flask 业务之外。登录凭据只在本地内存中
短暂流转，成功后仅把现有 QQMusic 请求需要的 Cookie 写入 qq_api。
"""
import base64
import json
import re
import secrets
import ssl
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import requests

import qq_api

try:
    import paho.mqtt.client as mqtt
    from paho.mqtt.client import CallbackAPIVersion
    from paho.mqtt.properties import Properties
    from paho.mqtt.packettypes import PacketTypes
except ImportError:  # pragma: no cover - exercised only on minimal installs
    mqtt = None
    CallbackAPIVersion = None
    Properties = None
    PacketTypes = None


CGI_URL = 'https://u.y.qq.com/cgi-bin/musicu.fcg'
QR_TTL = 180
MQTT_HOST = 'mu.y.qq.com'
MQTT_PORT = 443
MQTT_PATH = '/ws/handshake'
UA = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                  'AppleWebKit/537.36 (KHTML, like Gecko) '
                  'Chrome/120.0.0.0 Safari/537.36',
    'Referer': 'https://y.qq.com/',
    'Origin': 'https://y.qq.com',
}


class QRLoginError(RuntimeError):
    """二维码登录失败。"""


@dataclass
class _LoginTask:
    task_id: str
    qrcode_id: str
    image: str
    expires_at: float
    status: str = 'waiting'
    uin: str = ''
    error: str = ''
    subscribe_mid: int | None = field(default=None, repr=False)
    redirect_path: str = field(default='', repr=False)
    redirecting: bool = field(default=False, repr=False)
    connected: threading.Event = field(default_factory=threading.Event, repr=False)
    subscribed: threading.Event = field(default_factory=threading.Event, repr=False)
    client: Any = field(default=None, repr=False)
    thread: threading.Thread | None = field(default=None, repr=False)
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def public(self) -> dict[str, Any]:
        with self.lock:
            status = self.status
            if status == 'waiting' and time.time() >= self.expires_at:
                status = 'expired'
            return {
                'id': self.task_id,
                'status': status,
                'uin': self.uin,
                'expires_at': int(self.expires_at),
                'remaining': max(0, int(self.expires_at - time.time())),
                'error': self.error,
            }


class QRLoginManager:
    """管理本机一次性二维码登录任务。"""

    def __init__(self):
        self._tasks: dict[str, _LoginTask] = {}
        self._lock = threading.RLock()

    def start(self) -> tuple[_LoginTask, dict[str, Any]]:
        if mqtt is None:
            raise QRLoginError('缺少 paho-mqtt 依赖，请先运行 pip install paho-mqtt')
        self.cleanup()
        # 同一台本地应用只保留一个可用二维码，避免扫码结果串到旧页面。
        with self._lock:
            old_ids = list(self._tasks)
        for old_id in old_ids:
            self.cancel(old_id)
        task_id = secrets.token_urlsafe(18)
        image, qrcode_id = _create_qrcode()
        task = _LoginTask(
            task_id=task_id,
            qrcode_id=qrcode_id,
            image=image,
            expires_at=time.time() + QR_TTL,
        )
        with self._lock:
            self._tasks[task_id] = task
        try:
            thread = threading.Thread(target=self._run_mqtt, args=(task,),
                                      name='qqmusic-qr-login', daemon=True)
            task.thread = thread
            thread.start()
        except Exception:
            self.cancel(task_id)
            raise
        out = task.public()
        out['image'] = task.image
        return task, out

    def get(self, task_id: str) -> _LoginTask | None:
        with self._lock:
            return self._tasks.get(task_id)

    def status(self, task_id: str) -> dict[str, Any] | None:
        task = self.get(task_id)
        if task is None:
            return None
        data = task.public()
        if data['status'] == 'expired':
            self._stop(task)
        return data

    def cancel(self, task_id: str) -> bool:
        with self._lock:
            task = self._tasks.pop(task_id, None)
        if task is None:
            return False
        with task.lock:
            if task.status not in ('success', 'expired', 'cancelled', 'failed'):
                task.status = 'cancelled'
        self._stop(task)
        return True

    def logout(self) -> None:
        qq_api.set_cookie('')
        qq_api._apply_cookie()

    def cleanup(self) -> None:
        now = time.time()
        with self._lock:
            tasks = list(self._tasks.items())
        for task_id, task in tasks:
            if now >= task.expires_at and task.status in ('waiting', 'scanned'):
                with task.lock:
                    task.status = 'expired'
                with self._lock:
                    self._tasks.pop(task_id, None)
                self._stop(task)

    def _run_mqtt(self, task: _LoginTask) -> None:
        client = None
        try:
            props = Properties(PacketTypes.CONNECT)
            props.AuthenticationMethod = 'pass'
            props.UserProperty = [
                ('tmeAppID', 'qqmusic'),
                ('business', 'management'),
                ('hashTag', task.qrcode_id),
                ('clientTag', 'management.user'),
                ('userID', task.qrcode_id),
            ]
            # QQ 的 MQTT 服务可能先返回 Server moved，再要求改用带节点的握手路径。
            for _attempt in range(4):
                with task.lock:
                    if task.status in ('success', 'cancelled', 'expired', 'failed'):
                        return
                    task.redirecting = False
                    task.connected.clear()
                    task.subscribed.clear()
                client = _new_mqtt_client(task, self)
                task.client = client
                path = task.redirect_path or MQTT_PATH
                client.ws_set_options(path=path, headers=UA)
                client.tls_set_context(ssl.create_default_context())
                client.connect(MQTT_HOST, MQTT_PORT, keepalive=45, clean_start=True,
                               properties=props)
                # connect() 只建立 TCP/WS，CONNACK 和 SUBACK 由 loop 线程处理。
                client.loop_start()
                if not task.connected.wait(12):
                    raise QRLoginError('二维码服务连接超时，请检查网络后重试')
                with task.lock:
                    redirected = task.redirecting
                    terminal = task.status in ('failed', 'cancelled', 'expired')
                if redirected and not terminal:
                    client.loop_stop()
                    client.disconnect()
                    client = None
                    task.client = None
                    continue
                if terminal:
                    return
                if not task.subscribed.wait(12):
                    raise QRLoginError('二维码状态订阅超时，请稍后重试')
                with task.lock:
                    if task.redirecting and task.status not in ('failed', 'cancelled', 'expired'):
                        client.loop_stop()
                        client.disconnect()
                        client = None
                        task.client = None
                        continue
                while True:
                    with task.lock:
                        terminal = task.status in ('success', 'cancelled', 'expired', 'failed')
                    if terminal or time.time() >= task.expires_at:
                        if time.time() >= task.expires_at and not terminal:
                            with task.lock:
                                task.status = 'expired'
                        return
                    time.sleep(0.25)
            raise QRLoginError('二维码服务重定向次数过多，请稍后重试')
        except Exception as exc:
            with task.lock:
                if task.status not in ('success', 'cancelled', 'expired'):
                    task.status = 'failed'
                    task.error = _safe_error(exc)
        finally:
            try:
                if client is not None:
                    client.loop_stop()
                    client.disconnect()
            except Exception:
                pass
            task.client = None

    def _on_connect(self, task: _LoginTask, client, _userdata, _flags, reason_code, properties):
        if getattr(reason_code, 'is_failure', False):
            reference = _server_reference(properties)
            if int(getattr(reason_code, 'value', -1)) in (0x9c, 0x9d) and reference:
                if _set_redirect(task, reference):
                    # paho 已经完成了本次 CONNACK；让工作线程按新 path 重建连接。
                    task.connected.set()
                    try:
                        client.disconnect()
                    except Exception:
                        pass
                    return
            with task.lock:
                task.status = 'failed'
                task.error = _reason_error('服务端拒绝连接', reason_code, properties)
            task.connected.set()
            return
        task.connected.set()
        props = Properties(PacketTypes.SUBSCRIBE)
        props.UserProperty = [('authorization', 'tmelogin'), ('pubsub', 'unicast')]
        result, mid = client.subscribe(
            'management.qrcode_login/' + task.qrcode_id,
            qos=0,
            properties=props,
        )
        if result != mqtt.MQTT_ERR_SUCCESS:
            with task.lock:
                task.status = 'failed'
                task.error = '二维码状态订阅请求失败（%s）' % mqtt.error_string(result)
            task.subscribed.set()
            return
        with task.lock:
            task.subscribe_mid = mid

    def _on_connect_fail(self, task: _LoginTask, _client, _userdata):
        with task.lock:
            if task.status not in ('success', 'cancelled', 'expired'):
                task.status = 'failed'
                task.error = '网络/TLS/WebSocket 连接失败，请检查网络后重试'
        task.connected.set()

    def _on_subscribe(self, task: _LoginTask, _client, _userdata, _mid, reason_codes, _properties):
        codes = list(reason_codes or [])
        rejected = next((code for code in codes if getattr(code, 'is_failure', False)), None)
        if rejected is not None:
            with task.lock:
                task.status = 'failed'
                task.error = _reason_error('服务端拒绝订阅', rejected, None)
        # 无论 SUBACK 成功还是拒绝，都唤醒工作线程处理结果。
        task.subscribed.set()

    def _on_disconnect(self, task: _LoginTask, _client, _userdata, _disconnect_flags,
                       reason_code, _properties):
        with task.lock:
            if task.status in ('waiting', 'scanned') and time.time() < task.expires_at:
                if getattr(reason_code, 'is_failure', False):
                    task.status = 'failed'
                    task.error = _reason_error('二维码服务连接断开', reason_code, None)
        task.connected.set()
        task.subscribed.set()

    def _on_message(self, task: _LoginTask, _client, _userdata, message):
        if time.time() >= task.expires_at:
            with task.lock:
                task.status = 'expired'
            self._stop(task)
            return
        try:
            payload = json.loads(message.payload.decode('utf-8'))
        except Exception:
            return
        event_type = _message_type(message)
        if event_type == 'scanned':
            with task.lock:
                if task.status == 'waiting':
                    task.status = 'scanned'
            return
        if event_type in ('canceled', 'cancelled'):
            with task.lock:
                task.status = 'cancelled'
            self._stop(task)
            return
        if event_type == 'timeout':
            with task.lock:
                task.status = 'expired'
            self._stop(task)
            return
        if event_type == 'loginFailed':
            with task.lock:
                task.status = 'failed'
                task.error = 'QQ音乐确认登录失败'
            self._stop(task)
            return
        if event_type != 'cookies' or not isinstance(payload, dict):
            return
        try:
            uin, key = _cookie_event_values(payload)
            credential = _login_with_qr(task.qrcode_id, uin, key)
            musicid = str(credential.get('musicid') or credential.get('str_musicid') or uin)
            musickey = str(credential.get('musickey') or credential.get('musicKey') or key)
            if not musicid or not musickey:
                raise QRLoginError('登录响应缺少有效凭据')
            cookie = 'uin=%s; qqmusic_uin=%s; qqmusic_key=%s; qm_keyst=%s' % (
                musicid, musicid, musickey, musickey)
            # set_cookie 只落盘 Cookie，不把完整凭据返回给浏览器。
            qq_api.set_cookie(cookie)
            qq_api._apply_cookie()
            with task.lock:
                task.uin = musicid
                task.status = 'success'
            self._stop(task, disconnect=False)
        except Exception as exc:
            with task.lock:
                task.status = 'failed'
                task.error = _safe_error(exc)
            self._stop(task)

    def _stop(self, task: _LoginTask, disconnect: bool = True) -> None:
        client = task.client
        if disconnect and client is not None:
            try:
                client.disconnect()
            except Exception:
                pass


def _new_mqtt_client(task: _LoginTask, manager: QRLoginManager):
    client = mqtt.Client(
        callback_api_version=CallbackAPIVersion.VERSION2,
        client_id='qmkg-' + secrets.token_hex(8),
        protocol=mqtt.MQTTv5,
        transport='websockets',
        reconnect_on_failure=False,
    )
    client.on_connect = lambda c, u, f, r, p: manager._on_connect(task, c, u, f, r, p)
    client.on_connect_fail = lambda c, u: manager._on_connect_fail(task, c, u)
    client.on_disconnect = lambda c, u, f, r, p: manager._on_disconnect(task, c, u, f, r, p)
    client.on_subscribe = lambda c, u, m, rs, p: manager._on_subscribe(task, c, u, m, rs, p)
    client.on_message = lambda c, u, m: manager._on_message(task, c, u, m)
    client.reconnect_delay_set(min_delay=1, max_delay=3)
    return client


def _server_reference(properties) -> str:
    value = getattr(properties, 'ServerReference', '') if properties is not None else ''
    return str(value or '').strip()


def _set_redirect(task: _LoginTask, reference: str) -> bool:
    """把服务端节点地址转为握手路径，仅接受安全的 IP:port 引用。"""
    if not re.fullmatch(r'[A-Za-z0-9_.:-]{3,80}', reference):
        return False
    if ':' not in reference:
        return False
    with task.lock:
        if task.redirect_path:
            # 已经重定向过后仍被重定向，最多允许三次尝试。
            count = getattr(task, '_redirect_count', 0) + 1
        else:
            count = 1
        if count > 3:
            return False
        task._redirect_count = count
        task.redirect_path = MQTT_PATH.rstrip('/') + '/' + reference
        task.redirecting = True
    return True


def _message_type(message) -> str:
    props = getattr(message, 'properties', None)
    for item in getattr(props, 'UserProperty', None) or []:
        if isinstance(item, (tuple, list)) and len(item) == 2 and item[0] == 'type':
            return str(item[1])
    return ''


def _create_qrcode() -> tuple[str, str]:
    payload = {
        'comm': {'ct': 23, 'cv': 0},
        'req_0': {
            'module': 'music.login.LoginServer',
            'method': 'CreateQRCode',
            'param': {'tmeAppID': 'qqmusic', 'ct': 11, 'cv': 20090008},
        },
    }
    try:
        r = requests.post(CGI_URL, json=payload, headers=UA, timeout=15)
        r.raise_for_status()
        body = r.json()
    except Exception as exc:
        raise QRLoginError('获取二维码失败：%s' % _safe_error(exc)) from exc
    data = _response_data(body)
    image = str(data.get('qrcode') or '')
    qrcode_id = str(data.get('qrcodeID') or data.get('qrcodeId') or '')
    if not image or not qrcode_id:
        raise QRLoginError('QQ音乐二维码接口返回格式异常')
    if not image.startswith('data:image/'):
        try:
            image = 'data:image/png;base64,' + base64.b64encode(
                base64.b64decode(image.split(',')[-1])
            ).decode('ascii')
        except Exception as exc:
            raise QRLoginError('二维码图片格式异常') from exc
    return image, qrcode_id


def _login_with_qr(qrcode_id: str, uin: str, key: str) -> dict[str, Any]:
    payload = {
        'comm': {'ct': 11, 'cv': 20090008, 'tmeLoginType': 6},
        'req_0': {
            'module': 'music.login.LoginServer',
            'method': 'Login',
            'param': {'musicid': int(uin), 'qrCodeID': qrcode_id, 'token': key},
        },
    }
    r = requests.post(CGI_URL, json=payload, headers=UA, timeout=15)
    r.raise_for_status()
    body = r.json()
    code = body.get('code', 0)
    if code not in (0, '0'):
        raise QRLoginError('QQ音乐登录失败（错误码 %s）' % code)
    node_code = (body.get('req_0') or {}).get('code', 0)
    if node_code not in (0, '0'):
        raise QRLoginError('QQ音乐登录被拒绝（错误码 %s）' % node_code)
    data = _response_data(body)
    return data if isinstance(data, dict) else {}


def _response_data(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        return {}
    # musicu.fcg 的登录接口通常把业务对象放在 req_0.data；兼容直接 data。
    req = body.get('req_0')
    if isinstance(req, dict) and isinstance(req.get('data'), dict):
        return req['data']
    data = body.get('data')
    return data if isinstance(data, dict) else body


def _cookie_event_values(payload: dict[str, Any]) -> tuple[str, str]:
    cookies = payload.get('cookies') or {}
    if not isinstance(cookies, dict):
        raise QRLoginError('扫码消息格式异常')
    def value(name):
        item = cookies.get(name, '')
        return str(item.get('value', '')) if isinstance(item, dict) else str(item)
    uin = value('qqmusic_uin') or value('uin')
    key = value('qqmusic_key') or value('qm_keyst')
    if not uin.isdigit() or not key:
        raise QRLoginError('扫码消息缺少登录凭据')
    return uin, key


def _reason_error(prefix: str, reason_code, properties=None) -> str:
    """把 MQTT 原因码转成不泄露凭据的简短诊断。"""
    value = getattr(reason_code, 'value', None)
    label = str(reason_code)
    if value is None:
        return '%s：%s' % (prefix, label[:100])
    detail = ''
    reason = getattr(properties, 'ReasonString', None) if properties is not None else None
    if reason:
        detail = '，%s' % str(reason)[:100]
    return '%s：%s（0x%02x%s）' % (prefix, label[:80], int(value), detail)


def _safe_error(exc: Exception) -> str:
    text = str(exc).replace('\r', ' ').replace('\n', ' ').strip()
    # 不让异常文本意外包含 Cookie 或 MQTT 载荷。
    return text[:180] or exc.__class__.__name__


manager = QRLoginManager()
