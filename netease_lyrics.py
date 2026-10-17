"""NetEase plaintext lyrics using the shared QQ document, renderer and local cache.

Only normalized lyric fields are persisted; bridge cookies and upstream metadata
never enter the lyric cache. NetEase IDs are namespaced without changing QQ keys.
"""
from __future__ import annotations

import re
from typing import Any

import lyrics_api
import netease_api

LyricError = lyrics_api.LyricError
MAX_FIELD_BYTES = 512 * 1024
MAX_DOCUMENT_BYTES = 1024 * 1024
_FIELDS = ('lyric_lrc', 'translation_lrc', 'romanization_lrc')
_PLACEHOLDERS = {
    '纯音乐请欣赏', '纯音乐请您欣赏', '此歌曲为没有填词的纯音乐请您欣赏',
    '此歌曲为没有填词的纯音乐请欣赏', '纯音乐', 'instrumental',
}


def _text(value: Any) -> str:
    if not isinstance(value, str):
        raise LyricError('网易云歌词字段类型无效')
    try:
        if len(value) > MAX_FIELD_BYTES or len(value.encode('utf-8')) > MAX_FIELD_BYTES:
            raise LyricError('网易云歌词内容过大')
    except UnicodeError:
        raise LyricError('网易云歌词编码无效') from None
    if re.search(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', value):
        raise LyricError('网易云歌词包含无效控制字符')
    return value.lstrip('\ufeff').replace('\r\n', '\n').replace('\r', '\n').strip()


def _fields(values: list[Any]) -> list[str]:
    texts = [_text(value) for value in values]
    if sum(len(value.encode('utf-8')) for value in texts) > MAX_DOCUMENT_BYTES:
        raise LyricError('网易云歌词内容过大')
    return texts


def _field(body: dict[str, Any], name: str) -> str:
    if name not in body:
        return ''
    node = body[name]
    if not isinstance(node, dict):
        raise LyricError('网易云歌词字段类型无效')
    return _text(node.get('lyric', ''))


def _placeholder_only(text: str) -> bool:
    lines = lyrics_api.lrc_to_text(text).splitlines()
    values = [re.sub(r'[\W_]+', '', line).casefold() for line in lines if line.strip()]
    return bool(values) and all(value in _PLACEHOLDERS for value in values)


def _cached_document(sid: str, item: dict[str, Any]) -> lyrics_api.LyricDocument | None:
    """Treat corrupt/old provider entries as misses, never as successful lyrics."""
    data = item.get('document')
    if not isinstance(data, dict) or data.get('song_id') != int(sid):
        return None
    try:
        lyric, translation, romanization = _fields([data.get(key, '') for key in _FIELDS])
        warnings = data.get('warnings', [])
        if (not isinstance(warnings, list) or len(warnings) > 4
                or any(not isinstance(value, str) or len(value) > 200 for value in warnings)):
            return None
        return lyrics_api.LyricDocument(
            mid='', song_id=int(sid), lyric_lrc=lyric, translation_lrc=translation,
            romanization_lrc=romanization, cached=True, warnings=list(warnings))
    except LyricError:
        return None


def get_document(id: str | int, force=False) -> lyrics_api.LyricDocument:
    sid = netease_api._id(id)
    key = 'netease:' + sid
    if not force:
        item = lyrics_api._cache_get('lyrics', key)
        if item:
            doc = _cached_document(sid, item)
            if doc is not None:
                return doc
    # _body verifies the upstream status and preserves sanitized network/auth errors.
    body = netease_api._body('lyric', {'id': sid})
    if not any(name in body for name in ('lrc', 'tlyric', 'romalrc', 'nolyric', 'uncollected')):
        raise LyricError('网易云未返回有效歌词数据')
    for flag in ('nolyric', 'uncollected'):
        if flag in body and not isinstance(body[flag], bool):
            raise LyricError('网易云歌词状态类型无效')
    lyric, translation, romanization = _fields([
        _field(body, 'lrc'), _field(body, 'tlyric'), _field(body, 'romalrc')])
    warnings = []
    if body.get('nolyric') or _placeholder_only(lyric):
        lyric = translation = romanization = ''
        warnings.append('该歌曲为纯音乐，暂无可用歌词')
    elif body.get('uncollected'):
        lyric = translation = romanization = ''
        warnings.append('网易云暂未收录该歌曲歌词')
    elif not lyrics_api.lrc_to_text(lyric):
        # Timestamp/metadata-only responses are negative results, not real lyrics.
        lyric = translation = romanization = ''
        warnings.append('该歌曲暂无可用歌词')
    doc = lyrics_api.LyricDocument(
        mid='', song_id=int(sid), lyric_lrc=lyric, translation_lrc=translation,
        romanization_lrc=romanization, warnings=warnings)
    lyrics_api._cache_put('lyrics', key, {
        'songid': int(sid),
        'document': {
            'song_id': int(sid), 'lyric_lrc': lyric,
            'translation_lrc': translation, 'romanization_lrc': romanization,
            'warnings': warnings,
        },
        'status': 'ok' if doc.has_lyrics else 'empty',
    }, lyrics_api.LYRIC_TTL if doc.has_lyrics else lyrics_api.EMPTY_TTL)
    return doc


def render(id: str | int, fmt='txt', include_translation=False,
           include_romanization=False, preview=False, force=False) -> dict[str, Any]:
    fmt = (fmt or 'txt').lower()
    if fmt not in ('txt', 'lrc'):
        raise LyricError('歌词格式只支持 txt 或 lrc')
    doc = get_document(id, force=force)
    return lyrics_api.render_document(
        doc, fmt, include_translation, include_romanization, preview, source='netease')
