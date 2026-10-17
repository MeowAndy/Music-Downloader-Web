# -*- coding: utf-8 -*-
"""QQ音乐歌词获取、解析、渲染和本地缓存。

本模块只保留当前 Flask 应用需要的 QQ音乐歌词能力，不依赖
163MusicLyrics 的 .NET 桌面程序。歌词内容只写入用户本机的缓存或
下载目录，不把 Cookie、token 或请求头放入缓存。
"""
from __future__ import annotations

import base64
import json
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import paths
import qq_api


CACHE_FILE = paths.APP_DIR / 'lyrics_cache.json'
CACHE_VERSION = 1
SONG_ID_TTL = 7 * 24 * 3600
LYRIC_TTL = 7 * 24 * 3600
EMPTY_TTL = 15 * 60
MAX_CACHE_ENTRIES = 300
MAX_CACHE_BYTES = 8 * 1024 * 1024

_LRC_TAG_RE = re.compile(r'\[(\d+):(\d{1,2})(?:[.:](\d{1,3}))?\]')
_CACHE_LOCK = threading.RLock()


class LyricError(RuntimeError):
    """歌词接口或解析失败。"""


@dataclass(frozen=True)
class LyricLine:
    timestamp_ms: int
    text: str


@dataclass
class LyricDocument:
    mid: str
    song_id: int
    lyric_lrc: str = ''
    translation_lrc: str = ''
    romanization_lrc: str = ''
    cached: bool = False
    warnings: list[str] = field(default_factory=list)

    @property
    def lyric_lines(self) -> list[LyricLine]:
        return parse_lrc(self.lyric_lrc)

    @property
    def translation_lines(self) -> list[LyricLine]:
        return parse_lrc(self.translation_lrc)

    @property
    def romanization_lines(self) -> list[LyricLine]:
        return parse_lrc(self.romanization_lrc)

    @property
    def has_lyrics(self) -> bool:
        return bool(self.lyric_lines or lrc_to_text(self.lyric_lrc))

    @property
    def has_translation(self) -> bool:
        return bool(self.translation_lines or lrc_to_text(self.translation_lrc))

    @property
    def has_romanization(self) -> bool:
        return bool(self.romanization_lines or lrc_to_text(self.romanization_lrc))

    def plain_sections(self, include_translation=False, include_romanization=False) -> dict[str, str]:
        sections = {'lyric': lrc_to_text(self.lyric_lrc)}
        if include_translation and self.has_translation:
            sections['translation'] = lrc_to_text(self.translation_lrc)
        if include_romanization and self.has_romanization:
            sections['romanization'] = lrc_to_text(self.romanization_lrc)
        return sections

    def render_txt(self, include_translation=False, include_romanization=False) -> str:
        """渲染不带时间轴的纯文本歌词。"""
        sections = self.plain_sections(include_translation, include_romanization)
        labels = [('lyric', '原文'), ('translation', '翻译'), ('romanization', '罗马音')]
        blocks = []
        for key, label in labels:
            text = sections.get(key, '').strip()
            if not text:
                continue
            if key == 'lyric' and len(sections) == 1:
                blocks.append(text)
            else:
                blocks.append('[%s]\n%s' % (label, text))
        return '\n\n'.join(blocks).strip() + ('\n' if blocks else '')

    def render_lrc(self, include_translation=False, include_romanization=False) -> str:
        """渲染标准 LRC；可选字段以注释标题分隔，避免混淆主歌词。"""
        blocks = []
        main = normalize_lrc(self.lyric_lrc)
        if main:
            blocks.append('; 原文\n' + main)
        if include_translation and self.has_translation:
            trans = normalize_lrc(self.translation_lrc)
            if trans:
                blocks.append('; 翻译\n' + trans)
        if include_romanization and self.has_romanization:
            roma = normalize_lrc(self.romanization_lrc)
            if roma:
                blocks.append('; 罗马音\n' + roma)
        if not blocks:
            # 某些歌曲只返回无时间轴文本；不要伪造时间标签。
            return self.render_txt(include_translation, include_romanization)
        return '\n\n'.join(blocks).strip() + '\n'


# ---------- 纯文本 / LRC 处理 ----------


def parse_lrc(text: str | None) -> list[LyricLine]:
    """解析 LRC 时间标签，忽略 ti/ar 等元数据标签。"""
    if not text:
        return []
    out: list[LyricLine] = []
    for raw_line in str(text).replace('\r\n', '\n').replace('\r', '\n').split('\n'):
        tags = list(_LRC_TAG_RE.finditer(raw_line))
        if not tags or tags[0].start() != 0:
            continue
        lyric_text = raw_line[tags[-1].end():].strip()
        for tag in tags:
            minute = int(tag.group(1))
            second = int(tag.group(2))
            fraction = tag.group(3) or '0'
            millis = int((fraction + '00')[:3])
            out.append(LyricLine(minute * 60000 + second * 1000 + millis, lyric_text))
    return out


def lrc_to_text(text: str | None) -> str:
    """去掉时间轴和元数据，只保留歌词文本。"""
    if not text:
        return ''
    lines = parse_lrc(text)
    if lines:
        values = [line.text.strip() for line in lines]
    else:
        values = []
        for line in str(text).replace('\r\n', '\n').replace('\r', '\n').split('\n'):
            line = _LRC_TAG_RE.sub('', line).strip()
            if line and not re.match(r'^\[(?:ti|ar|al|by|offset|re|ve|length|id):', line, re.I):
                values.append(line)
    # 只压缩连续空行，不删除重复副歌。
    result = []
    blank = False
    for value in values:
        if not value:
            if result and not blank:
                result.append('')
            blank = True
        else:
            result.append(value)
            blank = False
    while result and not result[-1]:
        result.pop()
    return '\n'.join(result)


def normalize_lrc(text: str | None) -> str:
    """将 LRC 规范为 [mm:ss.xx]文本，便于保存和播放器兼容。"""
    lines = parse_lrc(text)
    if not lines:
        return ''
    out = []
    for line in sorted(enumerate(lines), key=lambda item: (item[1].timestamp_ms, item[0])):
        item = line[1]
        minutes, rest = divmod(max(0, item.timestamp_ms), 60000)
        seconds, millis = divmod(rest, 1000)
        out.append('[%02d:%02d.%02d]%s' % (minutes, seconds, millis // 10, item.text))
    return '\n'.join(out)


# ---------- QQ接口和缓存 ----------


def _empty_cache() -> dict[str, Any]:
    return {'version': CACHE_VERSION, 'song_ids': {}, 'lyrics': {}}


def _load_cache() -> dict[str, Any]:
    try:
        data = json.loads(CACHE_FILE.read_text(encoding='utf-8'))
        if not isinstance(data, dict) or data.get('version') != CACHE_VERSION:
            return _empty_cache()
        if not isinstance(data.get('song_ids'), dict) or not isinstance(data.get('lyrics'), dict):
            return _empty_cache()
        return data
    except Exception:
        return _empty_cache()


def _prune_cache(data: dict[str, Any], now: float) -> None:
    for group in ('song_ids', 'lyrics'):
        entries = data[group]
        for key, value in list(entries.items()):
            if not isinstance(value, dict) or float(value.get('expires_at', 0)) <= now:
                entries.pop(key, None)
        if len(entries) > MAX_CACHE_ENTRIES:
            old = sorted(entries.items(), key=lambda item: float(item[1].get('fetched_at', 0)))
            for key, _value in old[:len(entries) - MAX_CACHE_ENTRIES]:
                entries.pop(key, None)


def _save_cache(data: dict[str, Any]) -> None:
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(data, ensure_ascii=False, separators=(',', ':'))
    if len(encoded.encode('utf-8')) > MAX_CACHE_BYTES:
        # 先删除最老歌词记录，再尝试写入，避免缓存无限增长。
        lyrics = data.get('lyrics', {})
        old = sorted(lyrics.items(), key=lambda item: float(item[1].get('fetched_at', 0)))
        while old and len(encoded.encode('utf-8')) > MAX_CACHE_BYTES:
            lyrics.pop(old.pop(0)[0], None)
            encoded = json.dumps(data, ensure_ascii=False, separators=(',', ':'))
    tmp = CACHE_FILE.with_suffix('.tmp')
    tmp.write_text(encoded, encoding='utf-8')
    tmp.replace(CACHE_FILE)


def _cache_get(group: str, key: str) -> dict[str, Any] | None:
    now = time.time()
    with _CACHE_LOCK:
        data = _load_cache()
        item = data.get(group, {}).get(key)
        if isinstance(item, dict) and float(item.get('expires_at', 0)) > now:
            return item
        return None


def _cache_put(group: str, key: str, value: dict[str, Any], ttl: int) -> None:
    now = time.time()
    with _CACHE_LOCK:
        data = _load_cache()
        value = dict(value)
        value['fetched_at'] = now
        value['expires_at'] = now + ttl
        data[group][key] = value
        _prune_cache(data, now)
        try:
            _save_cache(data)
        except OSError:
            # 缓存不可写时不影响歌词预览/下载。
            pass


def _response_data(body: Any, preferred: str | None = None) -> dict[str, Any]:
    if not isinstance(body, dict):
        return {}
    keys = [preferred] if preferred else []
    keys += ['req_0', 'req_1', 'req', 'songinfo', 'data']
    for key in keys:
        if not key:
            continue
        node = body.get(key)
        if isinstance(node, dict):
            data = node.get('data')
            if isinstance(data, dict):
                return data
            if key == 'data':
                return node
    for node in body.values():
        if isinstance(node, dict) and isinstance(node.get('data'), dict):
            return node['data']
    return {}


def _response_code(body: Any, preferred: str | None = None) -> tuple[int, int]:
    top = body.get('code', 0) if isinstance(body, dict) else 0
    node_code = 0
    if isinstance(body, dict):
        for key in ([preferred] if preferred else []) + ['req_0', 'req_1', 'req', 'songinfo']:
            node = body.get(key)
            if isinstance(node, dict) and 'code' in node:
                node_code = node.get('code') or 0
                break
    try:
        return int(top), int(node_code)
    except (TypeError, ValueError):
        return -1, -1


def _post(body: dict[str, Any]) -> dict[str, Any]:
    # 复用 qq_api 的 Session，自动带上扫码登录后的 Cookie。
    return qq_api._post_musicu(body, timeout=15)


def resolve_song_id(mid: str, force: bool = False) -> int:
    mid = str(mid or '').strip()
    if not mid:
        raise LyricError('缺少歌曲 mid')
    if not force:
        item = _cache_get('song_ids', mid)
        if item:
            try:
                return int(item['songid'])
            except (KeyError, TypeError, ValueError):
                pass
    body = {
        'comm': {'uin': 0, 'format': 'json', 'ct': 24, 'cv': 0},
        'songinfo': {
            'module': 'music.pf_song_detail_svr',
            'method': 'get_song_detail_yqq',
            'param': {'song_mid': mid},
        },
    }
    try:
        payload = _post(body)
    except Exception as exc:
        raise LyricError('获取歌曲信息失败：%s' % exc) from exc
    top_code, node_code = _response_code(payload, 'songinfo')
    if top_code != 0 or node_code != 0:
        raise LyricError('歌曲信息接口返回错误（%s/%s）' % (top_code, node_code))
    data = _response_data(payload, 'songinfo')
    track = data.get('track_info') or {}
    response_mid = str(track.get('mid') or track.get('songmid') or '')
    try:
        song_id = int(track.get('id') or 0)
    except (TypeError, ValueError):
        song_id = 0
    if not song_id or (response_mid and response_mid != mid):
        raise LyricError('无法匹配歌曲的数字 ID')
    _cache_put('song_ids', mid, {'songid': song_id, 'mid': mid}, SONG_ID_TTL)
    return song_id


def _decode_field(value: Any) -> str:
    """解析明文或 Base64 LRC；QRC 十六进制内容留待扩展，不伪造结果。"""
    if value is None:
        return ''
    raw = str(value).strip()
    if not raw or raw in ('-', 'null'):
        return ''
    if _looks_like_lrc(raw) or raw.startswith('<'):
        return raw
    compact = ''.join(raw.split())
    try:
        decoded = base64.b64decode(compact + '=' * (-len(compact) % 4), validate=True)
        text = decoded.decode('utf-8-sig')
        if _looks_like_lrc(text) or text.strip():
            return text
    except Exception:
        pass
    return raw if _looks_like_lrc(raw) else ''


def _looks_like_lrc(value: str) -> bool:
    return bool(_LRC_TAG_RE.search(value)) or bool(re.search(r'\[(?:ti|ar|al|by):', value, re.I))


def get_document(mid: str, force: bool = False) -> LyricDocument:
    mid = str(mid or '').strip()
    if not mid:
        raise LyricError('缺少歌曲 mid')
    if not force:
        cached = _cache_get('lyrics', mid)
        if cached and isinstance(cached.get('document'), dict):
            d = cached['document']
            return LyricDocument(mid=mid, song_id=int(d.get('song_id') or 0),
                                 lyric_lrc=d.get('lyric_lrc') or '',
                                 translation_lrc=d.get('translation_lrc') or '',
                                 romanization_lrc=d.get('romanization_lrc') or '',
                                 cached=True, warnings=list(d.get('warnings') or []))
    song_id = resolve_song_id(mid, force=force)
    body = {
        'comm': {'ct': 19, 'cv': 1859, 'uin': 0},
        'req_0': {
            'module': 'music.musichallSong.PlayLyricInfo',
            'method': 'GetPlayLyricInfo',
            'param': {
                'format': 'json', 'crypt': 0, 'interval': 0,
                'lrc_t': 0, 'qrc': 0, 'qrc_t': 0,
                'roma': 1, 'roma_t': 0, 'songID': song_id,
                'trans': 1, 'trans_t': 0, 'type': -1,
            },
        },
    }
    try:
        payload = _post(body)
    except Exception as exc:
        raise LyricError('获取歌词失败：%s' % exc) from exc
    top_code, node_code = _response_code(payload, 'req_0')
    if top_code != 0 or node_code != 0:
        raise LyricError('歌词接口返回错误（%s/%s）' % (top_code, node_code))
    data = _response_data(payload, 'req_0')
    lyric = _decode_field(data.get('lyric'))
    translation = _decode_field(data.get('trans'))
    romanization = _decode_field(data.get('roma'))
    warnings = []
    if not lyric:
        warnings.append('该歌曲暂无可用歌词')
    if data.get('qrc') and not lyric:
        warnings.append('歌曲仅返回逐字歌词，当前版本暂不支持解码')
    doc = LyricDocument(mid=mid, song_id=song_id, lyric_lrc=lyric,
                        translation_lrc=translation, romanization_lrc=romanization,
                        warnings=warnings)
    ttl = LYRIC_TTL if doc.has_lyrics else EMPTY_TTL
    _cache_put('lyrics', mid, {
        'songid': song_id,
        'document': {
            'song_id': song_id,
            'lyric_lrc': lyric,
            'translation_lrc': translation,
            'romanization_lrc': romanization,
            'warnings': warnings,
        },
        'status': 'ok' if doc.has_lyrics else 'empty',
    }, ttl)
    return doc


def render(mid: str, fmt='txt', include_translation=False, include_romanization=False,
           preview=False, force=False) -> dict[str, Any]:
    fmt = (fmt or 'txt').lower()
    if fmt not in ('txt', 'lrc'):
        raise LyricError('歌词格式只支持 txt 或 lrc')
    doc = get_document(mid, force=force)
    return render_document(doc, fmt, include_translation, include_romanization, preview)


def render_document(doc: LyricDocument, fmt='txt', include_translation=False,
                    include_romanization=False, preview=False, *, source='qq') -> dict[str, Any]:
    """Render a provider document; preserve the legacy QQ response identity."""
    fmt = (fmt or 'txt').lower()
    if fmt not in ('txt', 'lrc'):
        raise LyricError('歌词格式只支持 txt 或 lrc')
    identity = ({'mid': doc.mid, 'songid': doc.song_id} if source == 'qq'
                else {'source': source, 'id': str(doc.song_id)})
    content = (doc.render_lrc if fmt == 'lrc' else doc.render_txt)(
        include_translation, include_romanization)
    sections = doc.plain_sections(include_translation, include_romanization)
    total_lines = len(content.splitlines()) if content else 0
    truncated = False
    if preview:
        content, truncated = _truncate(content, max_chars=1600, max_lines=30)
        sections = {key: _truncate(value, max_chars=900, max_lines=20)[0]
                    for key, value in sections.items() if value}
    return {
        **identity,
        'format': fmt,
        'content': content,
        'sections': sections,
        'has_lyrics': doc.has_lyrics,
        'has_translation': doc.has_translation,
        'has_romanization': doc.has_romanization,
        'cached': doc.cached,
        'warnings': doc.warnings,
        'total_lines': total_lines,
        'truncated': truncated,
    }


def _truncate(text: str, max_chars: int, max_lines: int) -> tuple[str, bool]:
    lines = text.splitlines()
    truncated = len(lines) > max_lines
    lines = lines[:max_lines]
    out = '\n'.join(lines)
    if len(out) > max_chars:
        out = out[:max_chars].rstrip() + '…'
        truncated = True
    elif truncated:
        out += '\n…'
    return out, truncated
