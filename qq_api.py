# -*- coding: utf-8 -*-
"""QQ音乐 API：搜索 / vkey / 歌单（歌单走 Node 签名桥接）"""
import json
import random
import subprocess
import time
from pathlib import Path

import requests

import paths

UA = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36',
    'Referer': 'https://y.qq.com/',
}
MUSICU_URL = 'https://u.y.qq.com/cgi-bin/musicu.fcg'
VKEY_URL = 'https://u.y.qq.com/cgi-bin/musicu.fcg'
NODE_BRIDGE = paths.RES_DIR / 'vendor' / 'node_bridge.js'

# 音质档位: (档位名, 文件名前缀模板, 扩展名, 对应的size字段)
QUALITIES = {
    'standard': ('C400%s.m4a', '.m4a', 'size_128mp3'),
    'hq': ('M800%s.mp3', '.mp3', 'size_320mp3'),
    'sq': ('F000%s.flac', '.flac', 'size_flac'),
    'master': ('AI00%s.flac', '.flac', 'size_master'),
}
QUALITY_FALLBACK = ['master', 'sq', 'hq', 'standard']


def extract_file_info(song):
    """从搜索/歌单的歌曲对象提取各音质可用性"""
    f = song.get('file') or {}
    media_mid = f.get('media_mid') or ''
    size_new = f.get('size_new') or []
    return {
        'media_mid': media_mid,
        'standard': bool(f.get('size_128mp3') or f.get('size_m4a')),
        'hq': bool(f.get('size_320mp3')),
        'sq': bool(f.get('size_flac')),
        'master': bool(len(size_new) > 0 and size_new[0]),
    }


_session = requests.Session()
_session.headers.update(UA)

# 用户登录 cookie（搜索接口需要登录态，从网页版 y.qq.com 复制）
COOKIE_FILE = paths.APP_DIR / 'qq_cookie.txt'


def get_cookie():
    try:
        return COOKIE_FILE.read_text(encoding='utf-8').strip()
    except Exception:
        return ''


def set_cookie(cookie):
    cookie = (cookie or '').strip()
    if cookie:
        COOKIE_FILE.write_text(cookie, encoding='utf-8')
    elif COOKIE_FILE.exists():
        COOKIE_FILE.unlink()


def _apply_cookie():
    c = get_cookie()
    if c:
        _session.headers['Cookie'] = c
    else:
        _session.headers.pop('Cookie', None)


_apply_cookie()


def _post_musicu(body, timeout=12):
    r = _session.post(MUSICU_URL, json=body, timeout=timeout)
    r.raise_for_status()
    return r.json()


def search(keyword, num=30, page=1):
    """在 QQ音乐乐库搜索歌曲，接口偶尔返回空结果，自动重试"""
    body = {
        'comm': {'uin': 0, 'format': 'json', 'ct': 24, 'cv': 0},
        'req_1': {
            'method': 'DoSearchForQQMusicDesktop',
            'module': 'music.search.SearchCgiService',
            'param': {'search_type': 0, 'query': keyword, 'page_num': page, 'num_per_page': num},
        },
    }
    last_err = None
    for i in range(3):
        try:
            j = _post_musicu(body)
            songs = j['req_1']['data']['body']['song']['list']
            if songs:
                out = []
                for s in songs:
                    info = extract_file_info(s)
                    out.append({
                        'name': s.get('name') or s.get('songname') or '',
                        'singer': '/'.join(x['name'] for x in s.get('singer', [])),
                        'mid': s.get('mid') or s.get('songmid') or '',
                        'album': (s.get('album') or {}).get('name', ''),
                        'duration': s.get('interval') or 0,
                        'pay_play': (s.get('pay') or {}).get('pay_play', 0),
                        'file': info,
                    })
                return out
            # 接口偶尔返回空列表，间隔后重试
            time.sleep(0.6 + 0.4 * i)
        except Exception as e:  # 网络/解析错误，重试
            last_err = e
            time.sleep(0.8)
    if last_err:
        raise RuntimeError('搜索失败: %s' % last_err)
    if not get_cookie():
        raise RuntimeError('QQ音乐搜索现在需要登录。请点击右上角「登录设置」，'
                           '粘贴网页版 y.qq.com 登录后的 Cookie')
    return []


def get_song_url(mid, media_mid, quality='sq', file_info=None):
    """获取指定音质的歌曲直链。

    media_mid: 歌曲文件的媒体ID（搜索结果 file.media_mid，与 songmid 不同）
    quality: standard/hq/sq/master
    file_info: 各音质可用性（用于降级），None 则只试请求的档位
    返回 (url, 实际档位) 或 (None, None)
    """
    if quality not in QUALITIES:
        quality = 'sq'
    # 降级链: 请求的档位 → 逐级向下
    chain = [quality]
    if file_info:
        for q in QUALITY_FALLBACK:
            if q != quality and file_info.get(q):
                chain.append(q)
    else:
        chain += [q for q in QUALITY_FALLBACK if q != quality]

    guid = str(int(random.random() * 2147483647) * int(time.time() * 1000) % 10000000000)
    fnames = [QUALITIES[q][0] % media_mid for q in chain]
    body = {
        'comm': {'uin': 0, 'format': 'json', 'ct': 24, 'cv': 0},
        'req_0': {
            'module': 'vkey.GetVkeyServer',
            'method': 'CgiGetVkey',
            'param': {
                'guid': guid,
                'songmid': [mid] * len(fnames),
                'songtype': [0] * len(fnames),
                'uin': '0',
                'loginflag': 1,
                'platform': '20',
                'filename': fnames,
            },
        },
    }
    j = _post_musicu(body)
    data = j['req_0']['data']
    sip = data.get('sip') or ['http://dl.stream.qqmusic.qq.com/']
    prefix = sip[-1] if sip else 'http://dl.stream.qqmusic.qq.com/'
    if prefix.startswith('http://'):
        prefix = prefix.replace('http://', 'https://', 1)
    # 优先返回请求档位；purl 为空或 404 时用降级档位
    candidates = []
    for info, q in zip(data.get('midurlinfo', []), chain):
        purl = info.get('purl') or ''
        if purl:
            candidates.append((prefix + purl, q))
    if not candidates:
        return None, None
    # 第一个候选先验证可用（HEAD 请求），不可用则试下一个
    for url, q in candidates:
        try:
            r = _session.head(url, timeout=10, allow_redirects=True)
            if r.status_code == 200:
                return url, q
        except Exception:
            continue
    return candidates[0][0], candidates[0][1]


def get_song_urls(mids):
    """获取歌曲直链（标准音质，兼容旧调用），返回 {mid: url}"""
    if isinstance(mids, str):
        mids = [mids]
    mids = [m for m in mids if m]
    if not mids:
        return {}
    guid = str(int(random.random() * 2147483647) * int(time.time() * 1000) % 10000000000)
    body = {
        'comm': {'uin': 0, 'format': 'json', 'ct': 24, 'cv': 0},
        'req_0': {
            'module': 'vkey.GetVkeyServer',
            'method': 'CgiGetVkey',
            'param': {
                'guid': guid,
                'songmid': mids,
                'songtype': [0] * len(mids),
                'uin': '0',
                'loginflag': 1,
                'platform': '20',
            },
        },
    }
    j = _post_musicu(body)
    data = j['req_0']['data']
    sip = data.get('sip') or ['http://dl.stream.qqmusic.qq.com/']
    prefix = sip[-1] if sip else 'http://dl.stream.qqmusic.qq.com/'
    if prefix.startswith('http://'):
        prefix = prefix.replace('http://', 'https://', 1)
    urls = {}
    for info in data.get('midurlinfo', []):
        purl = info.get('purl') or ''
        if purl:
            urls[info['songmid']] = prefix + purl
    return urls


def get_playlist(disstid, song_num=500):
    """获取 QQ音乐歌单详情（走 Node 签名桥接）"""
    if not paths.has_node():
        raise RuntimeError('未找到 Node.js，歌单功能不可用')
    cmd = [paths.node_exe(), str(NODE_BRIDGE), 'playlist', str(disstid), str(song_num)]
    # Windows 下 node 输出编码
    try:
        res = subprocess.run(cmd, capture_output=True, timeout=30)
    except subprocess.TimeoutExpired:
        raise RuntimeError('歌单请求超时')
    if res.returncode != 0:
        err = res.stderr.decode('utf-8', errors='ignore').strip()
        raise RuntimeError('歌单请求失败: %s' % err)
    out = res.stdout.decode('utf-8', errors='ignore').strip()
    return json.loads(out)
