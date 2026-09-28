# -*- coding: utf-8 -*-
"""全民K歌 API：单曲解析 / 主页作品列表 / 音频下载"""
import json
import re

import requests

UA = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36',
    'Referer': 'https://kg.qq.com/',
}

_session = requests.Session()
_session.headers.update(UA)

# 支持的链接形态：
#   https://kg.qq.com/node/play?s=xxx
#   https://kg2.qq.com/node/play?s=xxx
#   https://node.kg.qq.com/play?s=xxx
#   https://static-play.kg.qq.com/node/xxx/play_v2?s=xxx&...
#   https://kg.qq.com/node/personal?uid=xxx
SONG_URL_RE = re.compile(r'(?:kg2?|node\.kg|static-play\.kg)\.qq\.com/[^\s]*?(?:play)\??', re.I)
PERSONAL_URL_RE = re.compile(r'kg\.qq\.com/(?:node/)?personal', re.I)


def _fetch(url, timeout=15):
    r = _session.get(url, timeout=timeout)
    r.raise_for_status()
    return r


def _parse_data_json(html):
    """从页面提取 window.__DATA__ = {...}; 的 JSON"""
    marker = 'window.__DATA__ = '
    i = html.find(marker)
    if i < 0:
        raise RuntimeError('页面中没有找到数据（作品可能已被删除或链接无效）')
    start = html.index('{', i)
    end = html.find('; </script>', start)
    if end < 0:
        end = html.find('</script>', start)
    raw = html[start:end].strip()
    if raw.endswith(';'):
        raw = raw[:-1]
    return json.loads(raw)


def parse_song(url):
    """解析全民K歌单曲页，返回歌曲信息"""
    r = _fetch(url)
    data = _parse_data_json(r.text)
    if data.get('rawCode') not in (0, None):
        raise RuntimeError(data.get('rawMessage') or '作品不存在或已删除')
    d = data.get('detail') or {}
    if not d.get('playurl'):
        # 纯MV作品没有音频
        if d.get('playurl_video'):
            raise RuntimeError('该作品是纯视频作品，暂不支持音频下载')
        raise RuntimeError('该作品没有可下载的音频')
    return {
        'shareid': data.get('shareid') or '',
        'song_name': d.get('song_name') or '未知歌曲',
        'nick': d.get('nick') or '未知用户',
        'singer_name': d.get('singer_name') or '',
        'score': d.get('score') or 0,
        'cover': (d.get('cover') or '').replace('http://', 'https://'),
        'playurl': d.get('playurl'),
    }


def parse_personal(url):
    """解析全民K歌个人主页，返回用户信息和作品列表（网页版仅前8首）"""
    r = _fetch(url)
    data = _parse_data_json(r.text)
    if data.get('rawCode') not in (0, None):
        raise RuntimeError(data.get('rawMessage') or '主页不存在')
    d = data.get('data') or {}
    works = []
    for u in d.get('ugclist') or []:
        works.append({
            'shareid': u.get('shareid') or '',
            'title': u.get('title') or '未知作品',
            'time': u.get('time') or 0,
            'play_count': u.get('play_count') or 0,
            'score_rank': u.get('score_rank') or 0,
            'cover': (u.get('avatar') or '').replace('http://', 'https://'),
        })
    return {
        'nickname': d.get('nickname') or d.get('kgnick') or '未知用户',
        'head': (d.get('head_img_url') or '').replace('http://', 'https://'),
        'level': d.get('level') or 0,
        'levelname': d.get('levelname') or '',
        'follower': d.get('follower') or 0,
        'ugc_total': d.get('ugc_total_count') or 0,
        'works': works,
    }


def parse_link(url):
    """识别链接类型并解析"""
    url = url.strip()
    if PERSONAL_URL_RE.search(url):
        return {'type': 'kg_personal', 'data': parse_personal(url)}
    if SONG_URL_RE.search(url) or 'play?s=' in url or 'play_v2?s=' in url:
        return {'type': 'kg_song', 'data': parse_song(url)}
    raise RuntimeError('无法识别的链接，请粘贴全民K歌作品/主页分享链接')


def download_audio(playurl, timeout=60):
    """下载音频内容，返回 bytes"""
    r = _session.get(playurl, timeout=timeout)
    r.raise_for_status()
    if len(r.content) < 1024:
        raise RuntimeError('音频下载失败（内容过短，链接可能已过期，请重新解析）')
    return r.content


def sanitize_filename(name):
    """清理文件名中的非法字符"""
    for ch in '\\/:*?"<>|':
        name = name.replace(ch, '')
    return name.strip().strip('.')[:120] or '未命名'
