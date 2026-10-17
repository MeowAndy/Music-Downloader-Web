"""Bounded, user-initiated Hifiti transactions; credentials live only in memory.

No network at import, index scraping, background jobs, replies, purchases or
challenge bypass. Credentials go only to explicit official HTTPS auth transactions.
"""
import copy
import hashlib
import http.client
import ipaddress
import json
import re
import secrets
import socket
import ssl
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from email.message import Message
from http.cookiejar import CookieJar
from urllib.parse import parse_qsl, quote, urljoin, urlsplit
from bs4.element import Comment, NavigableString, Tag
from urllib.request import Request

from bs4 import BeautifulSoup
from flask import Blueprint, g, jsonify, request

ORIGIN = 'https://www.hifiti.com'
HOST = 'www.hifiti.com'
PREFIX = '/api/hifiti'
COOKIE = 'hifiti_owner'
MAX_BODY = 2 * 1024 * 1024
MAX_LOCAL_BODY = 8192
OWNER_TTL = 12 * 3600
MAX_OWNERS = 32
MAX_TEXT = 80000
_MESSAGES = {
    'INVALID_INPUT': 'HIFITI 请求参数无效。',
    'FORBIDDEN': '本地请求校验失败，请刷新页面后重试。',
    'NETWORK_ERROR': 'HIFITI 连接失败，请稍后手动重试。',
    'UPSTREAM_ERROR': 'HIFITI 返回了无法确认的数据；未将其视为成功。',
    'CHALLENGE': 'HIFITI 要求验证码或浏览器验证，请在官方网站完成；本应用不会绕过验证。',
    'LOGIN_FAILED': 'HIFITI 未确认登录，请检查账号密码或在官方网站查看要求。',
    'LOGIN_UNVERIFIED': 'HIFITI 返回登录响应，但无法确认账号身份；未保存登录状态。',
    'LOGIN_REQUIRED': 'HIFITI 登录已失效，请重新登录。',
    'REGISTER_FAILED': '官网未接受注册，请检查邮箱、用户名和密码，或在官网查看注册要求。',
    'RATE_LIMITED': '账号请求过于频繁，请稍后手动重试。',
    'BUSY': 'HIFITI 请求繁忙，请稍后手动重试。',
    'CANCELLED': '登录已取消，请重新操作。',
    'NOT_FOUND': 'HIFITI 未返回该帖子。',
}


class ApiError(RuntimeError):
    def __init__(self, code='UPSTREAM_ERROR', status=502):
        self.code = code if code in _MESSAGES else 'UPSTREAM_ERROR'
        self.status = status
        super().__init__(_MESSAGES[self.code])


def _remaining(deadline):
    left = deadline - time.monotonic()
    if left <= 0:
        raise ApiError('NETWORK_ERROR')
    return left


class _HTTPS(http.client.HTTPSConnection):
    def __init__(self, rows, deadline):
        super().__init__(HOST, timeout=min(10, _remaining(deadline)),
                         context=ssl.create_default_context())
        self.set_debuglevel(0)  # Suppress even globally enabled http.client debugging.
        self._rows, self._deadline = rows, deadline

    def connect(self):
        for family, kind, protocol, _, address in self._rows:
            sock = socket.socket(family, kind, protocol)
            try:
                sock.settimeout(min(10, _remaining(self._deadline)))
                sock.connect(address)
                self.sock = self._context.wrap_socket(sock, server_hostname=HOST)
                return
            except Exception:
                sock.close()
        raise ApiError('NETWORK_ERROR')


_SLOTS = threading.BoundedSemaphore(2)
_LOGIN_SLOTS = threading.BoundedSemaphore(2)


def _allowed_target(method, target):
    if not isinstance(target, str) or len(target) > 4096 or any(ord(c) < 32 for c in target):
        return False
    p = urlsplit(target)
    if p.scheme or p.netloc or p.fragment or not target.startswith('/'):
        return False
    if method == 'POST':
        return target in {'/user-login.htm', '/user-create.htm'}
    return method == 'GET' and (target in {'/', '/user-login.htm', '/user-create.htm'} or
        bool(re.fullmatch(r'/thread-[1-9][0-9]{0,11}\.htm', target)) or
        bool(re.fullmatch(r'/search(?:-[A-Za-z0-9_%\x27]+(?:-[0-9]+){2,3})?\.htm(?:\?keyword=[A-Za-z0-9%_.~+\-]*)?', target)))


class _CookieResponse:
    def __init__(self, headers):
        self.headers = Message()
        for key, value in headers:
            self.headers.add_header(key, value)

    def info(self):
        return self.headers


@dataclass(repr=False)
class _Session:
    cookies: CookieJar = field(default_factory=CookieJar)

    def clone(self):
        result = _Session()
        for cookie in self.cookies:
            result.cookies.set_cookie(copy.copy(cookie))
        return result


def _wire(method, target, session, *, form=None, deadline=None):
    """Fixed-host, public-DNS-pinned TLS; no proxies, redirects, retries or logs."""
    if not _allowed_target(method, target) or (form is not None and method != 'POST'):
        raise ApiError()
    if not _SLOTS.acquire(blocking=False):
        raise ApiError('BUSY', 503)
    connection = timer = None
    try:
        deadline = deadline or time.monotonic() + 25
        _remaining(deadline)
        rows = socket.getaddrinfo(HOST, 443, type=socket.SOCK_STREAM)
        if not rows:
            raise ApiError('NETWORK_ERROR')
        for row in rows:
            address = ipaddress.ip_address(row[4][0])
            address = getattr(address, 'ipv4_mapped', None) or address
            if not address.is_global or address.is_multicast:
                raise ApiError('NETWORK_ERROR')
        connection = _HTTPS(rows, deadline)

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
                   'Accept': 'text/html,application/json', 'Connection': 'close'}
        cookie_request = Request(ORIGIN + target)
        session.cookies.add_cookie_header(cookie_request)
        cookie = cookie_request.get_header('Cookie')
        if cookie:
            headers['Cookie'] = cookie
        payload = None
        if form is not None:
            from urllib.parse import urlencode
            payload = urlencode(form).encode('ascii')
            headers.update({'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                            'X-Requested-With': 'XMLHttpRequest', 'Origin': ORIGIN,
                            'Referer': ORIGIN + target})
        connection.request(method, target, payload, headers)
        response = connection.getresponse()
        if response.status in {403, 429, 503}:
            raise ApiError('CHALLENGE', 403)
        if response.status == 404:
            raise ApiError('NOT_FOUND', 404)
        if response.status != 200:  # Never forward credentials/cookies across redirects.
            raise ApiError()
        if response.getheader('Content-Encoding', 'identity').lower() not in {'', 'identity'}:
            raise ApiError()
        length = response.getheader('Content-Length')
        if length is not None and (not re.fullmatch(r'[0-9]{1,9}', length) or int(length) > MAX_BODY):
            raise ApiError()
        raw = bytearray()
        while True:
            _remaining(deadline)
            if connection.sock is not None:
                connection.sock.settimeout(min(10, _remaining(deadline)))
            chunk = response.read1(65536)
            if not chunk:
                break
            raw.extend(chunk)
            if len(raw) > MAX_BODY:
                raise ApiError()
        _remaining(deadline)
        if length is not None and len(raw) != int(length):
            raise ApiError()
        session.cookies.extract_cookies(_CookieResponse(response.getheaders()), cookie_request)
        return bytes(raw)
    except ApiError:
        raise
    except Exception:
        raise ApiError('NETWORK_ERROR') from None
    finally:
        if timer is not None:
            timer.cancel()
        if connection is not None:
            connection.close()
        _SLOTS.release()


def _text(value, limit=500):
    return ''.join(c for c in str(value) if not unicodedata.category(c).startswith('C')
                   or c in '\n\t').strip()[:limit]


def _html(raw):
    if not isinstance(raw, bytes) or len(raw) > MAX_BODY:
        raise ApiError()
    try:
        source = raw.decode('utf-8-sig')
    except UnicodeError:
        raise ApiError() from None
    if re.search(r'(?:cf-chl-|challenge-platform|verify you are human|checking your browser|人机验证|安全验证)', source, re.I):
        raise ApiError('CHALLENGE', 403)
    return BeautifulSoup(source, 'html.parser')


def _hidden(node):
    if not isinstance(node, Tag):
        return False
    style = re.sub(r'\s+', '', node.get('style', '')).lower()
    return (node.has_attr('hidden') or str(node.get('aria-hidden', '')).lower() == 'true'
            or bool(re.search(r'(?:^|;)(?:display:none|visibility:hidden)(?:!important)?(?:;|$)', style))
            or node.name == 'input' and node.get('type', '').lower() in {'hidden', 'password'})


def _visible(node):
    # Explicit invisibility only: .hide-content can contain legitimately revealed data.
    for child in list(node.find_all(True)):
        if child.attrs is not None and (_hidden(child) or child.name in {
                'script', 'style', 'iframe', 'object', 'embed', 'form', 'svg', 'template', 'noscript'}):
            child.decompose()
    for comment in node.find_all(string=lambda s: isinstance(s, Comment)):
        comment.extract()


def _plain(node, separator=''):
    if node is None or any(_hidden(parent) for parent in [node, *node.parents]):
        return ''
    _visible(node)
    value = (re.sub(r'\s+', ' ', ''.join(node.strings)).strip() if separator == ''
             else node.get_text(separator, strip=True))
    return _text(value, MAX_TEXT)


def _xn_encode(value):
    # Official xn.urlencode: encodeURIComponent plus these seven characters,
    # then percent signs become underscores. Apostrophes remain literal.
    encoded = quote(value, safe="'")
    for ch in '_-.~!*()':
        encoded = encoded.replace(ch, '%%%02X' % ord(ch))
    return encoded.replace('%', '_')


def _password_md5(value):
    # Official md5.js hashes UTF-16 code units (CESU-8), after CRLF folding.
    value = value.replace('\r\n', '\n')
    units = value.encode('utf-16-le', errors='surrogatepass')
    raw = ''.join(chr(units[i] | units[i + 1] << 8) for i in range(0, len(units), 2))
    return hashlib.md5(raw.encode('utf-8', errors='surrogatepass')).hexdigest()


def _safe_url(value):
    if not isinstance(value, str) or len(value) > 4096 or any(c.isspace() or ord(c) < 32 for c in value) or '\\' in value:
        return ''
    try:
        result = urljoin(ORIGIN + '/', value)
        p = urlsplit(result)
        host = (p.hostname or '').lower().rstrip('.')
        if p.scheme not in {'http', 'https'} or not host or p.username or p.password:
            return ''
        if (p.port not in {None, 80, 443} or '.' not in host or '%' in host or
                host.endswith(('.localhost', '.local', '.internal', '.test', '.invalid'))):
            return ''
        try:
            address = ipaddress.ip_address(host)
            if not address.is_global or address.is_multicast:
                return ''
        except ValueError:
            # Reject browser-interpreted abbreviated/octal/hex IPv4 literals too.
            if re.fullmatch(r'(?:0[xX][0-9a-fA-F]+|[0-9]+)(?:\.(?:0[xX][0-9a-fA-F]+|[0-9]+))*', host) or ':' in host:
                return ''
        secret_keys = {'auth', 'token', 'session', 'sid', 'password', 'access_token',
                       'auth_token', 'bbs_token', 'bbs_sid', 'cookie', 'csrf', 'authorization'}
        if any(key.lower() in secret_keys for key, _ in parse_qsl(p.query) + parse_qsl(p.fragment)):
            return ''  # Do not strip/rewrite signed URLs into a different resource.
        if host == HOST and re.search(r'(?:user-(?:login|logout)|post-(?:create|update)|thread-(?:pay|buy))', p.path):
            return ''
        return result
    except ValueError:
        return ''


def _page_uid(soup):
    # Only an explicit top-level site declaration, never a post author's uid.
    for script in soup.select('script:not([src])'):
        match = re.search(r'(?:^|;)\s*var\s+uid\s*=\s*([0-9]+)\s*;', script.get_text())
        if match:
            return int(match.group(1))
    return None


def _identity(soup):
    uid = _page_uid(soup)
    header = soup.select_one('#header')
    if not uid or header is None:
        return ''
    links = header.select('a[href]')
    logout = any(re.fullmatch(r'(?:\./|/)?user-logout\.htm', a.get('href', '')) for a in links)
    for profile in links:
        href = profile.get('href', '')
        own_profile = bool(re.fullmatch(r'(?:\./|/)?user-' + str(uid) + r'\.htm', href))
        own_menu = bool(re.fullmatch(r'(?:\./|/)?my\.htm', href))
        if (own_profile or own_menu) and logout:
            name = _text(profile.get_text('', strip=True), 80)
            if name:
                return name
    return ''


def parse_search(raw, keyword, page=1):
    soup = _html(raw)
    listing = soup.select_one('.threadlist, .postlist')
    if listing is None:
        raise ApiError()
    posts = 'postlist' in listing.get('class', [])
    items = []
    for row in listing.select('li.post[data-pid]' if posts else 'li.thread[data-tid]'):
        if posts:
            title_link = row.select_one('.message h6 > a[href]')
            if title_link is None:
                continue
            match = re.fullmatch(r'(?:\./|/)?thread-([1-9][0-9]{0,11})\.htm', title_link.get('href', ''))
            if not match:
                continue
            tid = match.group(1)
            title = _plain(title_link)
            message = row.select_one('.message')
            if message.h6:
                message.h6.decompose()
            items.append({'id': tid, 'pid': _text(row.get('data-pid', ''), 20),
                          'title': title, 'url': ORIGIN + '/thread-' + tid + '.htm',
                          'author': _plain(row.select_one('.username > a')),
                          'created': _plain(row.select_one('.date')),
                          'last_reply': '', 'views': '', 'tags': [],
                          'excerpt': _plain(message)[:1000]})
            continue
        tid = row.get('data-tid', '')
        if not re.fullmatch(r'[1-9][0-9]{0,11}', tid):
            continue
        subject = row.select_one('.subject')
        title_link = subject.select_one('a[href]') if subject else None
        if title_link is None:
            continue
        url = urljoin(ORIGIN + '/', title_link.get('href', ''))
        if url != ORIGIN + '/thread-' + tid + '.htm':
            continue
        author = row.select_one('.haya-post-info-username .username')
        created = row.select_one('.haya-post-info-create-date, .date')
        last = row.select_one('.haya-post-info-last-date, .last_date')
        views = row.select_one('.haya-post-info-views, .views')
        if views is None:
            icon = row.select_one('.icon-eye')
            views = icon.parent if icon else None
        if last is None and author is not None:
            info = row.select_one('.haya-post-info-username')
            sibling = info.find_next_sibling('span') if info else None
            if sibling:
                dates = sibling.find_all('span', recursive=False)
                last = dates[-1] if dates else None
        tags = [_plain(tag) for tag in subject.select('.pan-name')]
        # Tags often sit beside, not inside, the title anchor.
        title = _plain(title_link)
        items.append({'id': tid, 'title': title, 'url': url,
                      'author': _plain(author), 'created': _plain(created),
                      'last_reply': _plain(last), 'views': _plain(views), 'tags': tags})
    page_numbers = [page]
    has_next = False
    for link in soup.select('.pagination .page-link'):
        label = link.get_text('', strip=True)
        href = link.get('href', '')
        numeric = re.fullmatch(r'(?:\.\.\.)?([0-9]+)', label)
        if numeric and 0 < int(numeric.group(1)) <= 100000:
            page_numbers.append(int(numeric.group(1)))
        if re.search(r'(?:下一页|下页|»|›|▶|next)', label, re.I) and href and href != '#':
            has_next = True
    pages = max(page_numbers)
    # A total is returned only when the site actually publishes one.
    total = None
    for node in soup.select('.haya-post-info-total, .search-total, .card-header, .alert-info'):
        match = re.search(r'(?:共|结果|找到)[^0-9]{0,8}([0-9,]+)\s*(?:条|个|篇)', node.get_text(' ', strip=True))
        if match:
            total = int(match.group(1).replace(',', ''))
            break
    return {'ok': True, 'keyword': keyword, 'page': page, 'pages': pages,
            'total': total, 'has_next': has_next or pages > page, 'items': items}


_CODE = re.compile(r'(?:提取码|访问码|密码)\s*(?:为|是)?\s*[:：=]?\s*([A-Za-z0-9]{3,16})(?![A-Za-z0-9])')
_URL_TEXT = re.compile(r'https?://[^\s<>\"\'\u3000-\u9fff]+', re.I)
_RESOURCE_LABEL = re.compile(r'链接|地址|下载|提取码|访问码|密码|复制(?:链接|地址)|copy\s+link', re.I)


def _url_code(url):
    """Only known share providers: never reinterpret arbitrary auth parameters."""
    parts = urlsplit(url)
    host = (parts.hostname or '').lower()
    providers = {'pan.baidu.com', 'yun.baidu.com', 'pan.quark.cn', 'www.alipan.com',
                 'www.aliyundrive.com', 'www.123pan.com', 'www.123865.com',
                 'cloud.189.cn', 'pan.xunlei.com'}
    if host not in providers or not re.match(r'^/(?:s/|t/|web/share)', parts.path):
        return ''
    codes = {value for key, value in parse_qsl(parts.query) + parse_qsl(parts.fragment)
             if key.lower() in {'pwd', 'passcode'} and re.fullmatch(r'[A-Za-z0-9]{3,16}', value)}
    if host in {'pan.baidu.com', 'yun.baidu.com'} and re.fullmatch(r'[A-Za-z0-9]{4}', parts.fragment):
        codes.add(parts.fragment)
    return next(iter(codes)) if len(codes) == 1 else ''


def _resources(content):
    """Bounded visible-text stream; no scripts, arbitrary attributes or fetches."""
    chunks, events = [], []
    size = 0

    def append(value):
        nonlocal size
        value = value[:max(0, MAX_TEXT - size)]
        start = size
        chunks.append(value)
        size += len(value)
        return start, size

    def resource(value, label):
        url = _safe_url(value)
        start, end = append(label)
        if url and len(events) < 300:
            events.append((start, end, url, _text(label, 200) or url))

    def walk(node):
        if size >= MAX_TEXT:
            return
        if isinstance(node, NavigableString):
            value = re.sub(r'\s+', ' ', str(node))
            cursor = 0
            for match in _URL_TEXT.finditer(value):
                append(value[cursor:match.start()])
                raw = match.group().rstrip('.,;:!?，。；：！）)]}')
                resource(raw, raw)
                append(match.group()[len(raw):])
                cursor = match.end()
            append(value[cursor:])
            return
        if not isinstance(node, Tag):
            return
        block = node.name in {'p', 'div', 'li', 'section', 'article', 'blockquote', 'br', 'tr'}
        if block:
            append('\n')
        if node.name == 'a' and node.has_attr('href'):
            resource(node.get('href'), node.get_text(' ', strip=True) or node.get('href', ''))
        elif node.name in {'input', 'textarea'}:
            # Read-only text controls with a visible, explicit local label only.
            context = node.parent.get_text(' ', strip=True)[:500]
            if (node.has_attr('readonly') and node.get('type', 'text').lower() == 'text'
                    and _RESOURCE_LABEL.search(context)):
                append(' ' + (node.get('value', '') if node.name == 'input' else node.get_text()) + ' ')
        else:
            for child in node.children:
                walk(child)
            label = node.get_text(' ', strip=True)
            if (node.name in {'button', 'span', 'a'} and label and len(label) <= 80
                    and _RESOURCE_LABEL.search(label)):
                # These two explicit copy-text contracts only, not data-* enumeration.
                value = node.get('data-clipboard-text', node.get('data-copy-text', ''))
                if isinstance(value, str) and len(value) <= 4096:
                    if _safe_url(value) and value.startswith(('https://', 'http://')):
                        resource(value, label)
                    elif re.fullmatch(r'[A-Za-z0-9]{3,16}', value) and re.search(r'提取码|访问码|密码', label):
                        append(' ' + label + '：' + value)
        if block:
            append('\n')

    walk(content)
    stream = ''.join(chunks)
    # Read-only values are visible text too, but not part of ordinary .strings.
    occupied = [(start, end) for start, end, _, _ in events]
    for match in _URL_TEXT.finditer(stream):
        if any(start <= match.start() < end for start, end in occupied):
            continue
        url = _safe_url(match.group().rstrip('.,;:!?，。；：！）)]}'))
        if url and len(events) < 300:
            events.append((match.start(), match.end(), url, url))
    events.sort(key=lambda event: event[0])
    result, by_url = [], {}
    for _, _, url, label in events:
        if url not in by_url and len(result) < 100:
            item = {'url': url, 'label': label, 'code': ''}
            by_url[url] = item
            result.append(item)
    codes = {url: set(filter(None, [_url_code(url)])) for url in by_url}
    ambiguous = set()
    previous = 0
    for match in _CODE.finditer(stream):
        # URL parameters/anchor labels are not standalone code declarations.
        if any(start <= match.start() < end for start, end, _, _ in events):
            continue
        nearby = [(start, end, url) for start, end, url, _ in events
                  if previous <= start < match.start() and match.start() - end <= 160
                  and stream[end:match.start()].count('\n') <= 2]
        if not nearby:
            # Repeated/conflicting labels beside one resource must not silently
            # choose the first code; still never reach across distant paragraphs.
            nearby = [(start, end, url) for start, end, url, _ in events
                      if end <= match.start() and match.start() - end <= 160
                      and stream[end:match.start()].count('\n') <= 2]
        urls = {url for _, _, url in nearby}
        if len(urls) == 1:
            url = next(iter(urls))
            if url in codes:
                codes[url].add(match.group(1))
        elif len(urls) > 1:
            ambiguous.update(urls)
        previous = match.end()
    for url, item in by_url.items():
        if url not in ambiguous and len(codes[url]) == 1:
            item['code'] = next(iter(codes[url]))
    return result


def parse_thread(raw, tid):
    soup = _html(raw)
    content = soup.select_one('#thread-content.message[isfirst="1"]')
    if content is None:
        raise ApiError()
    title = soup.select_one('.thread-subject, h4.break-all, h1')
    if title is None:
        title = soup.title
    if any(_hidden(parent) for parent in [content, *content.parents]):
        content.clear()
    _visible(content)
    resources = _resources(content)
    links = [{'url': item['url'], 'label': item['label']} for item in resources]
    restricted = any(re.search(
        r'(?:本帖|此帖|本部分).{0,12}(?:隐藏内容|受限内容)|'
        r'(?:请|需要|必须|仅限|后才).{0,16}(?:回复|登录|购买|付费).{0,16}(?:查看|可见|解锁)|'
        r'(?:回复|登录|购买|付费)\s*(?:后|后才|才能)\s*(?:查看|可见)|'
        r'(?:权限不足|积分不足|无权查看)', warning.get_text('', strip=True))
        for warning in content.select('.alert-warning, .alert-danger, .hide-content'))
    text = _plain(content, '\n')
    notice = ('帖子含受限内容；仅显示当前账号可见部分，不自动回复、购买或消耗积分。' if restricted else '')
    if len(text) >= MAX_TEXT:
        notice = (notice + ' 正文过长，已截断显示。').strip()
    return {'ok': True, 'id': tid, 'title': _plain(title),
            'url': ORIGIN + '/thread-' + tid + '.htm', 'text': text,
            'links': links, 'resources': resources, 'notice': notice}


@dataclass(repr=False)
class _Owner:
    csrf: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    touched: float = field(default_factory=time.monotonic)
    session: _Session = field(default_factory=_Session)
    nickname: str = ''
    generation: int = 0
    login_busy: bool = False
    attempts: list = field(default_factory=list)


_owners = {}
_lock = threading.RLock()
_login_attempts = []
blueprint = Blueprint('hifiti', __name__, url_prefix=PREFIX)


def _loopback_guard():
    try:
        if urlsplit(request.host_url).hostname not in {'localhost', '127.0.0.1', '::1'}:
            raise ValueError()
        if request.remote_addr and not ipaddress.ip_address(request.remote_addr).is_loopback:
            raise ValueError()
        origin = request.headers.get('Origin')
        if origin is not None and origin != request.host_url.rstrip('/'):
            raise ValueError()
        if request.headers.get('Sec-Fetch-Site', '') not in {'', 'same-origin', 'none'}:
            raise ValueError()
    except ValueError:
        raise ApiError('FORBIDDEN', 403) from None


def _owner(create=False):
    _loopback_guard()
    value = request.cookies.get(COOKIE, '')
    now = time.monotonic()
    with _lock:
        for key in list(_owners):
            if now - _owners[key].touched >= OWNER_TTL:
                _owners[key].generation += 1
                del _owners[key]
        owner = _owners.get(value)
        if owner is None:
            if not create:
                raise ApiError('FORBIDDEN', 403)
            if len(_owners) >= MAX_OWNERS:
                raise ApiError('BUSY', 503)
            value = secrets.token_urlsafe(32)
            owner = _owners[value] = _Owner()
            g.hifiti_new_owner = value
        owner.touched = now
        if request.method == 'POST':
            csrf = request.headers.get('X-CSRF-Token', '')
            if not re.fullmatch(r'[A-Za-z0-9_-]{43}', csrf) or not secrets.compare_digest(csrf, owner.csrf):
                raise ApiError('FORBIDDEN', 403)
        return value, owner


def _status(owner):
    return {'ok': True, 'set': bool(owner.nickname), 'nickname': owner.nickname,
            'csrf': owner.csrf, 'persistent': False}


@blueprint.after_request
def _private(response):
    response.headers.update({'Cache-Control': 'no-store, private', 'Pragma': 'no-cache',
                             'X-Content-Type-Options': 'nosniff',
                             'Cross-Origin-Resource-Policy': 'same-origin'})
    if getattr(g, 'hifiti_new_owner', ''):
        response.set_cookie(COOKIE, g.hifiti_new_owner, httponly=True, samesite='Strict',
                            secure=request.is_secure, path=PREFIX)
    return response


@blueprint.errorhandler(ApiError)
def _error(exc):
    return jsonify(ok=False, code=exc.code, error=str(exc)), exc.status


@blueprint.errorhandler(Exception)
def _unexpected(_exc):
    # Never pass a transport/login exception or body into Flask's debug logger.
    return _error(ApiError())


@blueprint.get('/login/status')
def login_status():
    _, owner = _owner(create=True)
    with _lock:
        return jsonify(_status(owner))


def _credentials():
    if request.mimetype != 'application/json' or (request.content_length or 0) > MAX_LOCAL_BODY:
        raise ApiError('INVALID_INPUT', 400)
    raw = request.stream.read(MAX_LOCAL_BODY + 1)
    if len(raw) > MAX_LOCAL_BODY:
        raise ApiError('INVALID_INPUT', 400)
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeError):
        raise ApiError('INVALID_INPUT', 400) from None
    if not isinstance(body, dict) or set(body) != {'username', 'password'}:
        raise ApiError('INVALID_INPUT', 400)
    username, password = body['username'], body['password']
    if (not isinstance(username, str) or not isinstance(password, str) or
            not 1 <= len(username) <= 120 or not 1 <= len(password) <= 256 or
            any(unicodedata.category(c).startswith('C') for c in username) or '\x00' in password):
        raise ApiError('INVALID_INPUT', 400)
    return username, password


@blueprint.post('/login')
def login():
    key, owner = _owner()
    username, password = _credentials()
    now = time.monotonic()
    with _lock:
        owner.attempts[:] = [t for t in owner.attempts if now - t < 300]
        _login_attempts[:] = [t for t in _login_attempts if now - t < 300]
        if owner.login_busy:
            raise ApiError('BUSY', 503)
        if (len(owner.attempts) >= 5 or len(_login_attempts) >= 20 or
                owner.attempts and now - owner.attempts[-1] < 2):
            raise ApiError('RATE_LIMITED', 429)
        if not _LOGIN_SLOTS.acquire(blocking=False):
            raise ApiError('BUSY', 503)
        owner.attempts.append(now)
        _login_attempts.append(now)
        owner.generation += 1
        generation = owner.generation
        owner.nickname = ''
        owner.session = _Session()
        owner.login_busy = True
    candidate = _Session()
    try:
        deadline = time.monotonic() + 25
        _html(_wire('GET', '/user-login.htm', candidate, deadline=deadline))
        with _lock:
            if owner.generation != generation or _owners.get(key) is not owner:
                raise ApiError('CANCELLED', 409)
        digest = _password_md5(password)
        password = ''
        raw = _wire('POST', '/user-login.htm', candidate,
                    form={'email': username, 'password': digest}, deadline=deadline)
        username = digest = ''
        try:
            result = json.loads(raw)
        except (ValueError, UnicodeError):
            _html(raw)  # Distinguish a browser challenge from unknown auth data.
            raise ApiError('LOGIN_FAILED', 401) from None
        code = result.get('code') if isinstance(result, dict) else None
        if not (type(code) is int and code == 0 or type(code) is str and code == '0'):
            raise ApiError('LOGIN_FAILED', 401)
        with _lock:
            if owner.generation != generation or _owners.get(key) is not owner:
                raise ApiError('CANCELLED', 409)
        nickname = _identity(_html(_wire('GET', '/', candidate, deadline=deadline)))
        if not nickname:
            raise ApiError('LOGIN_UNVERIFIED', 502)
        with _lock:
            if owner.generation != generation or _owners.get(key) is not owner:
                raise ApiError('CANCELLED', 409)
            owner.session, owner.nickname = candidate, nickname
            return jsonify(_status(owner))
    finally:
        username = password = ''
        with _lock:
            owner.login_busy = False
        _LOGIN_SLOTS.release()


def _registration_input():
    # Local safety bounds, not claims about the official server's password policy.
    if request.mimetype != 'application/json' or (request.content_length or 0) > MAX_LOCAL_BODY:
        raise ApiError('INVALID_INPUT', 400)
    raw = request.stream.read(MAX_LOCAL_BODY + 1)
    if len(raw) > MAX_LOCAL_BODY:
        raise ApiError('INVALID_INPUT', 400)
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeError):
        raise ApiError('INVALID_INPUT', 400) from None
    if not isinstance(body, dict) or set(body) != {'email', 'username', 'password', 'repeat_password'}:
        raise ApiError('INVALID_INPUT', 400)
    if any(not isinstance(value, str) for value in body.values()):
        raise ApiError('INVALID_INPUT', 400)
    if (not 1 <= len(body['email']) <= 254 or not 1 <= len(body['username']) <= 64
            or not 1 <= len(body['password']) <= 128
            or body['password'] != body['repeat_password']
            or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', body['email'])
            or not body['username'].strip()
            or any(unicodedata.category(c).startswith('C') for c in ''.join(body.values()))):
        raise ApiError('INVALID_INPUT', 400)
    return body


def _registration_form(raw):
    """Recognize only the observed single-step form; never guess new challenges."""
    soup = _html(raw)
    form = soup.select_one('form#form')
    if (form is None or str(form.get('method', '')).lower() != 'post'
            or form.get('action') not in {'user-create.htm', '/user-create.htm', ORIGIN + '/user-create.htm'}):
        raise ApiError()
    if soup.select_one('iframe, [class*="captcha"], [id*="captcha"], #sendcode'):
        raise ApiError('CHALLENGE', 403)
    inputs = form.select('input, select, textarea')
    expected = {'email': 'email', 'username': 'text', 'password': 'password', 'repeat_password': 'password'}
    if len(inputs) != 4 or {node.get('name') for node in inputs} != set(expected):
        raise ApiError()
    if any(node.name != 'input' or node.get('type', 'text').lower() != expected[node['name']]
           or node.has_attr('disabled') for node in inputs):
        raise ApiError()
    scripts = '\n'.join(node.get_text() for node in soup.select('script:not([src])'))
    if not re.search(r'postdata\.password\s*=\s*\$\.md5\(postdata\.password\)', scripts):
        raise ApiError()


@blueprint.post('/register')
def register():
    key, owner = _owner()
    form = _registration_input()
    now = time.monotonic()
    with _lock:
        # Registration and login share limits, slots and per-owner exclusion.
        owner.attempts[:] = [t for t in owner.attempts if now - t < 300]
        _login_attempts[:] = [t for t in _login_attempts if now - t < 300]
        if owner.login_busy:
            raise ApiError('BUSY', 503)
        if (len(owner.attempts) >= 5 or len(_login_attempts) >= 20 or
                owner.attempts and now - owner.attempts[-1] < 2):
            raise ApiError('RATE_LIMITED', 429)
        if not _LOGIN_SLOTS.acquire(blocking=False):
            raise ApiError('BUSY', 503)
        owner.attempts.append(now)
        _login_attempts.append(now)
        owner.generation += 1
        generation = owner.generation
        owner.login_busy = True
    candidate = _Session()  # Never send an existing logged-in account's cookies.
    try:
        deadline = time.monotonic() + 25
        _registration_form(_wire('GET', '/user-create.htm', candidate, deadline=deadline))
        with _lock:
            if owner.generation != generation or _owners.get(key) is not owner:
                raise ApiError('CANCELLED', 409)
        form['password'] = _password_md5(form['password'])
        # Official script hashes password only; repeat_password is raw over TLS.
        raw = _wire('POST', '/user-create.htm', candidate, form=form, deadline=deadline)
        form.clear()
        try:
            result = json.loads(raw)
        except (ValueError, UnicodeError):
            _html(raw)
            raise ApiError('REGISTER_FAILED', 400) from None
        code = result.get('code') if isinstance(result, dict) else None
        if not (type(code) is int and code == 0 or type(code) is str and code == '0'):
            raise ApiError('REGISTER_FAILED', 400)
        with _lock:
            if owner.generation != generation or _owners.get(key) is not owner:
                raise ApiError('CANCELLED', 409)
        # An accepted response does not prove activation or login. Verification
        # is GET-only and failure must not invite duplicate signup submission.
        nickname = ''
        try:
            nickname = _identity(_html(_wire('GET', '/', candidate, deadline=deadline)))
        except Exception:
            # Acceptance is irreversible; never turn a verification/parser failure
            # into an apparent rejection that could encourage duplicate signup.
            pass
        with _lock:
            if owner.generation != generation or _owners.get(key) is not owner:
                raise ApiError('CANCELLED', 409)
            if nickname and not owner.nickname:
                owner.generation += 1  # Invalidate reads begun before identity adoption.
                owner.session, owner.nickname = candidate, nickname
            data = _status(owner)
            data.update(registered=True, message='官网已接受注册，请按要求激活后登录')
            return jsonify(data)
    finally:
        form.clear()
        with _lock:
            owner.login_busy = False
        _LOGIN_SLOTS.release()


@blueprint.post('/logout')
def logout():
    _, owner = _owner()
    with _lock:
        owner.generation += 1
        owner.nickname = ''
        owner.session = _Session()
        return jsonify(_status(owner))


def _read_owned(key, owner, target, parse):
    with _lock:
        candidate, generation = owner.session.clone(), owner.generation
    raw = _wire('GET', target, candidate)
    uid = _page_uid(_html(raw))
    data = parse(raw)
    with _lock:
        # Parsing may yield to logout, another login, owner eviction or expiry.
        # Validate and serialize the response under the same final lock.
        if owner.generation != generation or _owners.get(key) is not owner:
            raise ApiError('CANCELLED', 409)
        expired = bool(owner.nickname and uid == 0)
        if expired:
            owner.generation += 1
            owner.nickname = ''
            owner.session = _Session()
        else:
            owner.session = candidate
        data['auth'] = {'set': bool(owner.nickname), 'nickname': owner.nickname,
                        'expired': expired}
        return jsonify(data)


def _number(value, maximum):
    if not isinstance(value, str) or not re.fullmatch(r'[1-9][0-9]{0,11}', value) or int(value) > maximum:
        raise ApiError('INVALID_INPUT', 400)
    return int(value)


@blueprint.get('/search')
def search():
    key, owner = _owner(create=True)
    keyword = request.args.get('kw', '')
    page = _number(request.args.get('page', '1'), 10000)
    scope, exact = request.args.get('scope', 'title'), request.args.get('exact', '0')
    if (not keyword.strip() or len(keyword) > 120 or any(unicodedata.category(c).startswith('C') for c in keyword)
            or scope not in {'title', 'post'} or exact not in {'0', '1'}):
        raise ApiError('INVALID_INPUT', 400)
    # Keep original keyword, including meaningful spaces and punctuation.
    if page == 1 and scope == 'title' and exact == '0':
        target = '/search.htm?keyword=' + quote(keyword, safe='')
    else:
        target = '/search-' + _xn_encode(keyword) + '-' + ('1' if scope == 'title' else '0') + '-' + exact
        if page != 1:
            target += '-' + str(page)
        target += '.htm'
    return _read_owned(key, owner, target, lambda raw: parse_search(raw, keyword, page))


@blueprint.get('/thread')
def thread():
    key, owner = _owner(create=True)
    tid = str(_number(request.args.get('id'), 999999999999))
    return _read_owned(key, owner, '/thread-' + tid + '.htm', lambda raw: parse_thread(raw, tid))
