"""Migu plaintext lyrics with the shared document renderer and namespaced cache."""
from __future__ import annotations

import re
from typing import Any

import lyrics_api
import migu_api

LyricError = lyrics_api.LyricError
MAX_FIELD_BYTES = 512 * 1024
_EMPTY_WARNING = '该歌曲暂无可用歌词'


def _text(value: Any) -> str:
    if not isinstance(value, str):
        raise LyricError('咪咕歌词字段类型无效')
    try:
        if len(value) > MAX_FIELD_BYTES or len(value.encode('utf-8')) > MAX_FIELD_BYTES:
            raise LyricError('咪咕歌词内容过大')
    except UnicodeError:
        raise LyricError('咪咕歌词编码无效') from None
    if re.search(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', value):
        raise LyricError('咪咕歌词包含无效控制字符')
    return value.lstrip('\ufeff').replace('\r\n', '\n').replace('\r', '\n').strip()


def _document(sid: str, lyric: str, *, cached=False) -> lyrics_api.LyricDocument:
    # Shared song_id is a QQ numeric field. Keep the provider ID separately and
    # override rendered identity rather than coercing an alphanumeric/zero-led ID.
    return lyrics_api.LyricDocument(
        mid=sid, song_id=0, lyric_lrc=lyric, cached=cached,
        warnings=[] if lyric else [_EMPTY_WARNING])


def _cached_document(sid: str, item: dict[str, Any]) -> lyrics_api.LyricDocument | None:
    data = item.get('document')
    if not isinstance(data, dict) or data.get('id') != sid:
        return None
    try:
        lyric = _text(data.get('lyric_lrc'))
        if item.get('status') != ('ok' if lyric else 'empty'):
            return None
        if lyric and not lyrics_api.lrc_to_text(lyric):
            return None
        return _document(sid, lyric, cached=True)
    except LyricError:
        return None


def get_document(id: str, force=False) -> lyrics_api.LyricDocument:
    sid = migu_api.validate_id(id)
    key = 'migu:' + sid
    if not force:
        item = lyrics_api._cache_get('lyrics', key)
        if item:
            doc = _cached_document(sid, item)
            if doc is not None:
                return doc
    # Adapter returns '' only for a confirmed empty result; upstream errors must
    # raise. Never decode base64 or turn malformed responses into negative hits.
    lyric = _text(migu_api.lyric_text(sid))
    if lyric and not lyrics_api.lrc_to_text(lyric):
        raise LyricError('咪咕未返回有效歌词数据')
    doc = _document(sid, lyric)
    lyrics_api._cache_put('lyrics', key, {
        'document': {'id': sid, 'lyric_lrc': lyric},
        'status': 'ok' if doc.has_lyrics else 'empty',
    }, lyrics_api.LYRIC_TTL if doc.has_lyrics else lyrics_api.EMPTY_TTL)
    return doc


def render(id: str, fmt='txt', include_translation=False,
           include_romanization=False, preview=False, force=False) -> dict[str, Any]:
    fmt = (fmt or 'txt').lower()
    if fmt not in ('txt', 'lrc'):
        raise LyricError('歌词格式只支持 txt 或 lrc')
    doc = get_document(id, force=force)
    result = lyrics_api.render_document(doc, fmt, preview=preview, source='migu')
    result['id'] = doc.mid
    result['warnings'] = list(result['warnings'])
    if include_translation:
        result['warnings'].append('咪咕暂不支持翻译歌词，仅提供原文')
    if include_romanization:
        result['warnings'].append('咪咕暂不支持罗马音歌词，仅提供原文')
    return result
