# -*- coding: utf-8 -*-
"""Music Downloader Web — QQ音乐 + 全民K歌 下载工具的 Web 版

基于 Li4n0/Music-Downloader 与 T-K-233/KG-Downloader 的功能重写，
接口全部更新为当前可用的版本。
"""
import errno
import os
import re
import tempfile
import traceback
from pathlib import Path

from flask import Flask, jsonify, request, send_file, send_from_directory

import ffmpeg_api
import kg_api
import paths
import qq_api
import qq_login
import lyrics_api
import netease_api
import netease_login
import netease_lyrics
import migu_api
import migu_lyrics
import migu_login
import hifiti_api
import vocal_api
import vip_api

BASE_DIR = paths.APP_DIR
RES_DIR = paths.RES_DIR
OUTPUT_DIR = paths.OUTPUT_DIR

app = Flask(__name__, static_folder=str(RES_DIR / 'static'), static_url_path='/static')
app.config['JSON_AS_ASCII'] = False
app.register_blueprint(migu_login.blueprint)
app.register_blueprint(hifiti_api.blueprint)


@app.get('/api/health')
def api_health():
    import startup
    return jsonify({**startup.health_identity(app),
                    'data_dir': str(BASE_DIR), 'resource_dir': str(RES_DIR)})

QQ_PLAYLIST_RE = re.compile(r'y\.qq\.com/[^\s]*?playlist/(\d+)', re.I)


def err(msg):
    return jsonify({'ok': False, 'error': str(msg)}), 400


@app.get('/')
def index():
    resp = send_from_directory(str(RES_DIR / 'static'), 'index.html')
    # 禁止缓存，保证前端更新后浏览器立即拿到新页面
    resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['Expires'] = '0'
    return resp


@app.get('/app.ico')
def app_icon():
    return send_from_directory(str(RES_DIR), 'app.ico', mimetype='image/x-icon')


# ---------- 网易云音乐 ----------

@app.before_request
def netease_origin_guard():
    if not request.path.startswith(('/api/netease/', '/api/migu/', '/api/hifiti/')):
        return None
    from urllib.parse import urlsplit
    host = urlsplit('http://' + request.host)
    if host.hostname not in ('127.0.0.1', 'localhost', '::1'):
        return jsonify({'ok': False, 'error': '音乐接口仅允许本机访问。'}), 403
    origin = request.headers.get('Origin')
    if origin and origin != request.host_url.rstrip('/'):
        return jsonify({'ok': False, 'error': '请求来源不匹配。'}), 403
    if request.headers.get('Sec-Fetch-Site') == 'cross-site':
        return jsonify({'ok': False, 'error': '不允许跨站访问音乐接口。'}), 403
    return None


def netease_error(exc):
    if isinstance(exc, netease_api.ApiError):
        return jsonify({'ok': False, 'code': exc.code, 'error': str(exc)}), exc.status
    if isinstance(exc, lyrics_api.LyricError):
        return jsonify({'ok': False, 'code': 'LYRIC_ERROR',
                        'error': '网易云歌词暂不可用，请稍后重试。'}), 502
    return jsonify({'ok': False, 'code': 'UPSTREAM_ERROR',
                    'error': '网易云操作失败，请稍后重试。'}), 502


@app.get('/api/netease/search')
def api_netease_search():
    try:
        kw = (request.args.get('kw') or '').strip()
        raw_num = request.args.get('num', '30')
        if not raw_num.isdecimal() or not 1 <= int(raw_num) <= 100:
            raise netease_api.ApiError('INVALID_INPUT', 400)
        return jsonify({'ok': True, 'songs': netease_api.search(kw, num=int(raw_num))})
    except Exception as exc:
        return netease_error(exc)


def _netease_lyric_options(format_param='format'):
    sid = netease_api._id(request.args.get('id') or '')
    fmt = (request.args.get(format_param) or 'txt').lower()
    if fmt not in ('txt', 'lrc'):
        raise netease_api.ApiError('INVALID_INPUT', 400)
    return sid, {
        'fmt': fmt,
        'include_translation': request.args.get('translation', '0') == '1',
        'include_romanization': request.args.get('romanization', '0') == '1',
    }


def _save_lyric(content, stem, fmt):
    """Publish a complete UTF-8 file atomically, without replacing any existing file."""
    if (not stem or stem in ('.', '..') or re.search(r'[<>:"/\\\\|?*\x00-\x1f]', stem)
            or fmt not in ('txt', 'lrc')):
        raise ValueError('Invalid lyric filename or format')
    directory = Path(OUTPUT_DIR)
    directory.mkdir(parents=True, exist_ok=True)
    temp = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', newline='\n',
                                         dir=directory, prefix='.lyrics-',
                                         suffix='.tmp', delete=False) as out:
            temp = Path(out.name)
            out.write(content)
            out.flush()
            os.fsync(out.fileno())
        size = temp.stat().st_size
        for i in range(10000):
            name = '%s%s.%s' % (stem, ' (%d)' % i if i else '', fmt)
            target = directory / name
            try:
                # link is atomic and exclusive, including across concurrent requests.
                os.link(temp, target)
            except FileExistsError:
                continue
            except OSError as exc:
                # FAT/exFAT and some removable/network drives cannot hard-link.
                unsupported = {errno.ENOSYS, errno.ENOTSUP, errno.EOPNOTSUPP,
                               errno.EXDEV, errno.EPERM}
                if exc.errno not in unsupported and getattr(exc, 'winerror', None) not in (1, 50):
                    raise
                try:
                    reservation = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                except FileExistsError:
                    continue
                try:
                    os.close(reservation)
                    # Replace only the empty file exclusively reserved by this call.
                    # The completed temporary file is on the same filesystem.
                    os.replace(temp, target)
                except BaseException:
                    target.unlink(missing_ok=True)
                    raise
            return {'name': name, 'size': size}
        raise OSError('No available lyric filename')
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def _save_netease_lyric(content, stem, fmt):
    """Compatibility wrapper for existing NetEase callers."""
    try:
        return _save_lyric(content, stem, fmt)
    except ValueError:
        raise netease_api.ApiError('INVALID_INPUT', 400) from None


@app.get('/api/netease/lyrics')
def api_netease_lyrics():
    try:
        sid, options = _netease_lyric_options()
        data = netease_lyrics.render(sid, **options,
                                     preview=request.args.get('preview', '1') == '1')
        return jsonify({'ok': True, **data})
    except Exception as exc:
        return netease_error(exc)


@app.get('/api/netease/lyrics/download')
def api_netease_lyrics_download():
    try:
        sid, options = _netease_lyric_options()
        data = netease_lyrics.render(sid, **options, preview=False)
        if not data.get('has_lyrics'):
            return jsonify({'ok': False, 'code': 'LYRIC_NOT_FOUND',
                            'error': '该歌曲暂无歌词'}), 404
        name = request.args.get('name') or '未知歌曲'
        singer = request.args.get('singer') or ''
        stem = netease_api._safe_stem('%s - %s' % (name, singer) if singer else name)
        saved = _save_netease_lyric(data['content'], stem, options['fmt'])
        return jsonify({'ok': True, **saved, 'format': options['fmt'],
                        'cached': data.get('cached', False),
                        'warnings': data.get('warnings', [])})
    except Exception as exc:
        return netease_error(exc)


@app.get('/api/netease/download')
def api_netease_download():
    try:
        lyrics_enabled = request.args.get('lyrics', '0') == '1'
        if lyrics_enabled:
            # Reject invalid lyric options before starting the audio download.
            sid, options = _netease_lyric_options('lyrics_format')
        result = netease_api.download(request.args.get('id') or '',
                                      request.args.get('quality') or 'standard')
        result = dict(result)
        if lyrics_enabled:
            result['lyrics'] = False
            if result.get('conversion_error'):
                result['lyrics_error'] = '音频转换失败，已保留原文件，未保存附加歌词。'
            else:
                try:
                    data = netease_lyrics.render(sid, **options, preview=False)
                    if not data.get('has_lyrics'):
                        result['lyrics_error'] = '该歌曲暂无歌词'
                    else:
                        # Use the final (possibly converted/deduplicated) audio name,
                        # never the user-supplied song title or pre-conversion name.
                        audio_name = result['name']
                        if (not isinstance(audio_name, str) or not audio_name
                                or re.search(r'[<>:"/\\\\|?*\x00-\x1f]', audio_name)):
                            raise ValueError('Invalid saved audio name')
                        saved = _save_netease_lyric(
                            data['content'], Path(audio_name).stem, options['fmt'])
                        result.update({'lyrics': True, 'lyrics_name': saved['name'],
                                       'lyrics_size': saved['size'],
                                       'lyrics_cached': data.get('cached', False),
                                       'lyrics_warnings': data.get('warnings', [])})
                except Exception:
                    # Auxiliary lyrics must never expose credentials or undo audio.
                    result['lyrics_error'] = '网易云歌词获取或保存失败，请稍后重试。'
        return jsonify({'ok': True, **result})
    except Exception as exc:
        return netease_error(exc)


@app.get('/api/netease/login/status')
def api_netease_login_status():
    try:
        return jsonify({'ok': True, **netease_api.credential_status()})
    except Exception as exc:
        return netease_error(exc)


@app.post('/api/netease/login/qr/start')
def api_netease_qr_start():
    try:
        _task, data = netease_login.manager.start()
        return jsonify({'ok': True, **data})
    except Exception as exc:
        return netease_error(exc)


@app.get('/api/netease/login/qr/status')
def api_netease_qr_status():
    try:
        task_id = (request.args.get('id') or '').strip()
        if not task_id or len(task_id) > 128:
            raise netease_api.ApiError('INVALID_INPUT', 400)
        data = netease_login.manager.status(task_id)
        if data is None:
            return jsonify({'ok': False, 'error': '网易云登录任务不存在或已失效。'}), 404
        return jsonify({'ok': True, **data})
    except Exception as exc:
        return netease_error(exc)


@app.post('/api/netease/login/qr/cancel')
def api_netease_qr_cancel():
    try:
        body = request.get_json(silent=True) or {}
        task_id = body.get('id', '') if isinstance(body, dict) else ''
        if not isinstance(task_id, str) or len(task_id) > 128:
            raise netease_api.ApiError('INVALID_INPUT', 400)
        netease_login.manager.cancel(task_id)
        return jsonify({'ok': True})
    except Exception as exc:
        return netease_error(exc)


@app.post('/api/netease/logout')
def api_netease_logout():
    try:
        netease_login.manager.logout()
        return jsonify({'ok': True, 'set': False})
    except Exception as exc:
        return netease_error(exc)


# ---------- 咪咕音乐 ----------


def migu_error(exc):
    if isinstance(exc, migu_api.ApiError):
        return jsonify({'ok': False, 'code': exc.code, 'error': str(exc)}), exc.status
    if isinstance(exc, lyrics_api.LyricError):
        return jsonify({'ok': False, 'code': 'LYRIC_ERROR',
                        'error': '咪咕歌词暂不可用，请稍后重试。'}), 502
    return jsonify({'ok': False, 'code': 'UPSTREAM_ERROR',
                    'error': '咪咕操作失败，请稍后重试。'}), 502


@app.get('/api/migu/status')
def api_migu_status():
    try:
        return jsonify({'ok': True, **migu_api.status()})
    except Exception as exc:
        return migu_error(exc)


@app.get('/api/migu/search')
def api_migu_search():
    try:
        kw = (request.args.get('kw') or '').strip()
        raw_num = request.args.get('num', '30')
        if (not kw or len(raw_num) > 3 or not raw_num.isascii()
                or not raw_num.isdecimal() or not 1 <= int(raw_num) <= 100):
            raise migu_api.ApiError('INVALID_INPUT', 400)
        return jsonify({'ok': True, 'songs': migu_api.search(kw, num=int(raw_num))})
    except Exception as exc:
        return migu_error(exc)


def _migu_lyric_options(format_param='format'):
    sid = migu_api.validate_id(request.args.get('id') or '')
    fmt = (request.args.get(format_param) or 'txt').lower()
    if fmt not in ('txt', 'lrc'):
        raise migu_api.ApiError('INVALID_INPUT', 400)
    return sid, {
        'fmt': fmt,
        'include_translation': request.args.get('translation', '0') == '1',
        'include_romanization': request.args.get('romanization', '0') == '1',
    }


def _migu_lyric_stem(value):
    stem = re.sub(r'[<>:"/\\\\|?*\x00-\x1f\x7f]', '_', value).strip(' .')[:160].rstrip(' .')
    stem = stem or 'Migu'
    if re.match(r'^(CON|PRN|AUX|NUL|COM[0-9¹²³]|LPT[0-9¹²³])(?:\.|$)', stem, re.I):
        stem = '_' + stem
    return stem


@app.get('/api/migu/lyrics')
def api_migu_lyrics():
    try:
        sid, options = _migu_lyric_options()
        data = migu_lyrics.render(sid, **options,
                                  preview=request.args.get('preview', '1') == '1')
        return jsonify({'ok': True, **data})
    except Exception as exc:
        return migu_error(exc)


@app.get('/api/migu/lyrics/download')
def api_migu_lyrics_download():
    try:
        sid, options = _migu_lyric_options()
        data = migu_lyrics.render(sid, **options, preview=False)
        if not data.get('has_lyrics'):
            return jsonify({'ok': False, 'code': 'LYRIC_NOT_FOUND',
                            'error': '该歌曲暂无歌词'}), 404
        name = request.args.get('name') or '未知歌曲'
        singer = request.args.get('singer') or ''
        stem = _migu_lyric_stem('%s - %s' % (name, singer) if singer else name)
        saved = _save_lyric(data['content'], stem, options['fmt'])
        return jsonify({'ok': True, **saved, 'format': options['fmt'],
                        'cached': data.get('cached', False),
                        'warnings': data.get('warnings', [])})
    except Exception as exc:
        return migu_error(exc)


@app.get('/api/migu/download')
def api_migu_download():
    try:
        sid = migu_api.validate_id(request.args.get('id') or '')
        lyrics_enabled = request.args.get('lyrics', '0') == '1'
        if lyrics_enabled:
            sid, options = _migu_lyric_options('lyrics_format')
        result = dict(migu_api.download(sid, request.args.get('quality') or 'standard'))
        if lyrics_enabled:
            result['lyrics'] = False
            if result.get('conversion_error'):
                result['lyrics_error'] = '音频转换失败，已保留原文件，未保存附加歌词。'
            else:
                try:
                    data = migu_lyrics.render(sid, **options, preview=False)
                    if not data.get('has_lyrics'):
                        result['lyrics_error'] = '该歌曲暂无歌词'
                        result['lyrics_warnings'] = data.get('warnings', [])
                    else:
                        audio_name = result['name']
                        if (not isinstance(audio_name, str) or not audio_name
                                or re.search(r'[<>:"/\\\\|?*\x00-\x1f]', audio_name)):
                            raise ValueError('Invalid saved audio name')
                        saved = _save_lyric(data['content'], Path(audio_name).stem, options['fmt'])
                        result.update({'lyrics': True, 'lyrics_name': saved['name'],
                                       'lyrics_size': saved['size'],
                                       'lyrics_cached': data.get('cached', False),
                                       'lyrics_warnings': data.get('warnings', [])})
                except Exception:
                    result['lyrics_error'] = '咪咕歌词获取或保存失败，请稍后重试。'
        return jsonify({'ok': True, **result})
    except Exception as exc:
        return migu_error(exc)


# ---------- QQ音乐 ----------

@app.get('/api/cookie')
def api_cookie_get():
    c = qq_api.get_cookie()
    uin = ''
    m = re.search(r'uin=(\d+)', c)
    if m:
        uin = m.group(1)
    return jsonify({'ok': True, 'set': bool(c), 'uin': uin})


@app.post('/api/cookie')
def api_cookie_set():
    # 保留旧接口，便于已有本地配置升级；页面不再暴露 Cookie 输入框。
    body = request.get_json(silent=True) or {}
    cookie = (body.get('cookie') or '').strip()
    if cookie and 'uin' not in cookie:
        return err('Cookie 格式不对，应包含 uin=... 和 qqmusic_key=... 字段')
    qq_api.set_cookie(cookie)
    qq_api._apply_cookie()
    return jsonify({'ok': True, 'set': bool(cookie)})


@app.post('/api/login/qr/start')
def api_qr_login_start():
    try:
        _task, data = qq_login.manager.start()
        return jsonify({'ok': True, **data})
    except Exception as e:
        return err(e)


@app.get('/api/login/qr/status')
def api_qr_login_status():
    task_id = (request.args.get('id') or '').strip()
    if not task_id:
        return err('缺少登录任务 ID')
    data = qq_login.manager.status(task_id)
    if data is None:
        return jsonify({'ok': False, 'error': '登录任务不存在或已失效'}), 404
    return jsonify({'ok': True, **data})


@app.post('/api/login/qr/cancel')
def api_qr_login_cancel():
    body = request.get_json(silent=True) or {}
    task_id = (body.get('id') or '').strip()
    if task_id:
        qq_login.manager.cancel(task_id)
    return jsonify({'ok': True})


@app.post('/api/logout')
def api_logout():
    qq_login.manager.logout()
    return jsonify({'ok': True, 'set': False})


@app.get('/api/search')
def api_search():
    kw = (request.args.get('kw') or '').strip()
    num = int(request.args.get('num') or 30)
    if not kw:
        return err('请输入搜索关键词')
    try:
        songs = qq_api.search(kw, num=num)
        return jsonify({'ok': True, 'songs': songs})
    except qq_api.SearchError as e:
        status = 401 if e.code == 'LOGIN_REQUIRED' else 502
        return jsonify({'ok': False, 'code': e.code, 'error': str(e)}), status
    except Exception as e:
        return err(e)


@app.get('/api/lyrics')
def api_lyrics():
    mid = (request.args.get('mid') or '').strip()
    fmt = (request.args.get('format') or 'txt').lower()
    include_translation = request.args.get('translation', '0') == '1'
    include_romanization = request.args.get('romanization', '0') == '1'
    preview = request.args.get('preview', '1') == '1'
    if not mid:
        return err('缺少歌曲 mid')
    if fmt not in ('txt', 'lrc'):
        return err('歌词格式只支持 txt 或 lrc')
    try:
        data = lyrics_api.render(mid, fmt=fmt,
                                 include_translation=include_translation,
                                 include_romanization=include_romanization,
                                 preview=preview)
        return jsonify({'ok': True, **data})
    except lyrics_api.LyricError as e:
        return jsonify({'ok': False, 'error': str(e), 'code': 'LYRIC_ERROR'}), 502
    except Exception as e:
        return err(e)


@app.get('/api/lyrics/download')
def api_lyrics_download():
    mid = (request.args.get('mid') or '').strip()
    name = request.args.get('name') or '未知歌曲'
    singer = request.args.get('singer') or ''
    fmt = (request.args.get('format') or 'txt').lower()
    include_translation = request.args.get('translation', '0') == '1'
    include_romanization = request.args.get('romanization', '0') == '1'
    if not mid:
        return err('缺少歌曲 mid')
    if fmt not in ('txt', 'lrc'):
        return err('歌词格式只支持 txt 或 lrc')
    try:
        data = lyrics_api.render(mid, fmt=fmt,
                                 include_translation=include_translation,
                                 include_romanization=include_romanization,
                                 preview=False)
        if not data.get('has_lyrics'):
            return jsonify({'ok': False, 'error': '该歌曲暂无歌词', 'code': 'LYRIC_NOT_FOUND'}), 404
        stem = kg_api.sanitize_filename('%s - %s' % (name, singer)) if singer else kg_api.sanitize_filename(name)
        path = unique_path(OUTPUT_DIR / (stem + '.' + fmt))
        path.write_text(data['content'], encoding='utf-8')
        return jsonify({'ok': True, 'name': path.name, 'size': path.stat().st_size,
                        'format': fmt, 'cached': data.get('cached', False),
                        'warnings': data.get('warnings', [])})
    except lyrics_api.LyricError as e:
        return jsonify({'ok': False, 'error': str(e), 'code': 'LYRIC_ERROR'}), 502
    except Exception as e:
        return err(e)


@app.get('/api/song_url')
def api_song_url():
    mid = request.args.get('mid') or ''
    if not mid:
        return err('缺少 mid')
    try:
        urls = qq_api.get_song_urls([mid])
        if mid in urls:
            return jsonify({'ok': True, 'url': urls[mid]})
        return jsonify({'ok': False, 'error': '该歌曲为付费歌曲，无法免费获取（仅支持免费/试听曲目）'})
    except Exception as e:
        return err(e)


def _maybe_convert(fpath, fmt):
    """按需转换格式，返回实际要发送的文件路径"""
    if not fmt or fmt in ('original', fpath.suffix.lstrip('.')):
        return fpath
    if not ffmpeg_api.available():
        return fpath  # 无 ffmpeg 时返回原格式
    try:
        return ffmpeg_api.convert_file(fpath, fmt)
    except Exception:
        return fpath  # 转换失败回退原格式


@app.get('/api/download')
def api_download():
    """下载 QQ音乐歌曲：保存到 output/，quality=standard/hq/sq/master，返回 JSON"""
    mid = request.args.get('mid') or ''
    name = request.args.get('name') or '未知歌曲'
    singer = request.args.get('singer') or ''
    media_mid = request.args.get('media_mid') or ''
    quality = (request.args.get('quality') or 'sq').lower()
    lyrics_enabled = request.args.get('lyrics', '0') == '1'
    lyrics_format = (request.args.get('lyrics_format') or 'txt').lower()
    include_translation = request.args.get('translation', '0') == '1'
    include_romanization = request.args.get('romanization', '0') == '1'
    if not mid:
        return err('缺少 mid')
    if quality not in qq_api.QUALITIES:
        return err('不支持的音质: %s' % quality)
    if lyrics_enabled and lyrics_format not in ('txt', 'lrc'):
        return err('歌词格式只支持 txt 或 lrc')
    try:
        if not media_mid:
            # 兼容：没有 media_mid 时用标准音质
            urls = qq_api.get_song_urls([mid])
            if mid not in urls:
                return err('该歌曲为付费歌曲，无法下载（需在客户端下载后转换）')
            url, actual_q = urls[mid], 'standard'
        else:
            url, actual_q = qq_api.get_song_url(mid, media_mid, quality)
            if not url:
                return err('该歌曲无法获取下载链接（可能为付费歌曲且登录态失效）')
        audio = requests_get(url)
        ext = qq_api.QUALITIES[actual_q][1]
        fname = kg_api.sanitize_filename('%s - %s%s' % (name, singer, ext)) if singer else \
            kg_api.sanitize_filename('%s%s' % (name, ext))
        fpath = unique_path(OUTPUT_DIR / fname)
        fpath.write_bytes(audio)
        qnames = {'standard': '标准128k', 'hq': 'HQ 320k', 'sq': 'SQ无损', 'master': '臻品母带'}
        out = {
            'ok': True, 'name': fpath.name, 'size': fpath.stat().st_size,
            'quality': actual_q, 'quality_name': qnames.get(actual_q, actual_q),
            'lyrics': False,
        }
        if lyrics_enabled:
            try:
                lyric_data = lyrics_api.render(
                    mid, fmt=lyrics_format,
                    include_translation=include_translation,
                    include_romanization=include_romanization,
                    preview=False,
                )
                if lyric_data.get('has_lyrics'):
                    lyric_path = fpath.with_suffix('.' + lyrics_format)
                    lyric_path.write_text(lyric_data['content'], encoding='utf-8')
                    out.update({'lyrics': True, 'lyrics_name': lyric_path.name,
                                'lyrics_size': lyric_path.stat().st_size,
                                'lyrics_cached': lyric_data.get('cached', False),
                                'lyrics_warnings': lyric_data.get('warnings', [])})
                else:
                    out.update({'lyrics_error': '该歌曲暂无歌词',
                                'lyrics_warnings': lyric_data.get('warnings', [])})
            except Exception as lyric_error:
                # 歌词是附加能力，不能让已完成的音频下载失败。
                out['lyrics_error'] = str(lyric_error)[:200]
        return jsonify(out)
    except Exception as e:
        return err(e)


@app.get('/api/playlist')
def api_playlist():
    """QQ音乐歌单详情。支持直接传 disstid 或完整歌单链接"""
    q = (request.args.get('id') or '').strip()
    if not q:
        return err('缺少歌单ID或链接')
    m = QQ_PLAYLIST_RE.search(q)
    disstid = m.group(1) if m else re.sub(r'\D', '', q)
    if not disstid:
        return err('无法从链接中提取歌单ID')
    try:
        data = qq_api.get_playlist(disstid)
        return jsonify({'ok': True, **data})
    except Exception as e:
        return err(e)


# ---------- 全民K歌 ----------

@app.post('/api/parse')
def api_parse():
    """解析链接：全民K歌单曲 / 主页 / QQ音乐歌单"""
    body = request.get_json(silent=True) or {}
    url = (body.get('url') or '').strip()
    if not url:
        return err('请输入链接')
    try:
        m = QQ_PLAYLIST_RE.search(url)
        if m:
            data = qq_api.get_playlist(m.group(1))
            return jsonify({'ok': True, 'type': 'qq_playlist', 'data': data})
        result = kg_api.parse_link(url)
        return jsonify({'ok': True, **result})
    except Exception as e:
        return err(e)


@app.get('/api/download_kg')
def api_download_kg():
    """下载全民K歌作品：保存到 output/，返回 JSON"""
    url = (request.args.get('url') or '').strip()
    fmt = (request.args.get('format') or 'original').lower()
    if not url:
        return err('缺少作品链接')
    try:
        song = kg_api.parse_song(url)
        audio = kg_api.download_audio(song['playurl'])
        fname = kg_api.sanitize_filename('%s - %s.m4a' % (song['song_name'], song['nick']))
        fpath = unique_path(OUTPUT_DIR / fname)
        fpath.write_bytes(audio)
        out = _maybe_convert(fpath, fmt)
        return jsonify({'ok': True, 'name': out.name, 'size': out.stat().st_size,
                        'format': out.suffix.lstrip('.')})
    except Exception as e:
        return err(e)


# ---------- VIP歌曲 mflac 转换（手动按需） ----------

@app.get('/api/vip/status')
def api_vip_status():
    """mflac 文件列表（按需扫描，无后台监控）"""
    s = vip_api.monitor.status()
    s['files'] = vip_api.monitor.list_folder()
    return jsonify({'ok': True, **s})


@app.post('/api/vip/convert')
def api_vip_convert():
    """手动转换单个 mflac 文件"""
    body = request.get_json(silent=True) or {}
    path = (body.get('path') or '').strip()
    if not path:
        return err('缺少文件路径')
    try:
        r = vip_api.monitor.convert_now(path)
        return jsonify({'ok': True, **r})
    except Exception as e:
        return err(e)


@app.post('/api/vip/convert_all')
def api_vip_convert_all():
    """转换目录中所有未转换的 mflac"""
    try:
        ok, fail = vip_api.monitor.convert_all()
        return jsonify({'ok': True, 'success': ok, 'fail': fail})
    except Exception as e:
        return err(e)


@app.post('/api/vip/settings')
def api_vip_settings():
    """设置 mflac 所在目录"""
    body = request.get_json(silent=True) or {}
    if 'watch_folder' in body:
        folder = (body.get('watch_folder') or '').strip()
        if folder and not Path(folder).is_dir():
            return err('目录不存在: %s' % folder)
        vip_api.monitor.set_folder(folder)
    s = vip_api.monitor.status()
    return jsonify({'ok': True, **s})


# ---------- 格式转换 ----------

@app.post('/api/convert_file')
def api_convert_file():
    """把 output/ 中已有的文件转换为指定格式（服务端完成，返回 JSON）"""
    body = request.get_json(silent=True) or {}
    name = (body.get('name') or '').strip()
    fmt = (body.get('to') or 'mp3').lower()
    if not name:
        return err('缺少文件名')
    src = OUTPUT_DIR / name
    if not src.is_file():
        return err('文件不存在')
    try:
        out = ffmpeg_api.convert_file(src, fmt)
        return jsonify({'ok': True, 'name': out.name, 'size': out.stat().st_size})
    except Exception as e:
        return err(e)


@app.get('/api/format_status')
def api_format_status():
    return jsonify({'ok': True, 'ffmpeg': ffmpeg_api.available(),
                    'settings': ffmpeg_api.get_settings()})


@app.post('/api/open_folder')
def api_open_folder():
    """在资源管理器中打开下载文件夹"""
    import os
    import subprocess
    try:
        if os.name == 'nt':
            os.startfile(str(OUTPUT_DIR))  # noqa
        else:
            subprocess.Popen(['xdg-open', str(OUTPUT_DIR)])
        return jsonify({'ok': True, 'folder': str(OUTPUT_DIR)})
    except Exception as e:
        return err(e)


@app.post('/api/clear_files')
def api_clear_files():
    """清空下载文件夹中的所有文件"""
    removed = 0
    for f in OUTPUT_DIR.iterdir():
        if f.is_file():
            try:
                f.unlink()
                removed += 1
            except Exception:
                pass
    return jsonify({'ok': True, 'removed': removed})


# ---------- 人声/伴奏分离（原生功能） ----------

@app.get('/api/vocal/status')
def api_vocal_status():
    return jsonify({'ok': True, 'running': vocal_api.running(),
                    'setup': vocal_api.setup_status(),
                    'models': {'2stems': '人声+伴奏', '4stems': '+鼓+贝斯+其他',
                               '5stems': '+鼓+贝斯+钢琴+其他'}})


@app.post('/api/vocal/start')
def api_vocal_start():
    try:
        vocal_api.ensure_running()
        return jsonify({'ok': True})
    except Exception as e:
        return err(e)


@app.post('/api/vocal/setup')
def api_vocal_setup():
    """下载并安装官方 AI 组件（首次使用，749MB）"""
    try:
        action = vocal_api.start_setup_async()
        return jsonify({'ok': True, 'action': action})
    except Exception as e:
        return err(e)


@app.post('/api/vocal/separate')
def api_vocal_separate():
    """分离 output/ 中的音频文件（m4a 自动转 mp3 后分离），结果保存回 output/"""
    body = request.get_json(silent=True) or {}
    name = (body.get('name') or '').strip()
    model = (body.get('model') or '2stems').strip()
    if not name:
        return err('缺少文件名')
    src = OUTPUT_DIR / name
    if not src.is_file():
        return err('文件不存在: %s' % name)
    try:
        r = vocal_api.separate_file(src, model)
        return jsonify({'ok': True, **r})
    except Exception as e:
        return err(e)


@app.post('/api/vocal/separate_upload')
def api_vocal_separate_upload():
    """上传本地音频文件并分离，结果保存到 output/"""
    f = request.files.get('file')
    model = (request.form.get('model') or '2stems').strip()
    if not f or not f.filename:
        return err('请选择文件')
    name = kg_api.sanitize_filename(f.filename)
    tmp = OUTPUT_DIR / ('_vocal_up_' + name)
    f.save(str(tmp))
    try:
        r = vocal_api.separate_file(tmp, model)
        # 结果文件名去掉临时前缀
        for res in r.get('results', []):
            if res['name'].startswith('_vocal_up_'):
                new_name = res['name'][len('_vocal_up_'):]
                try:
                    (OUTPUT_DIR / res['name']).rename(OUTPUT_DIR / new_name)
                    res['name'] = new_name
                except Exception:
                    pass
        return jsonify({'ok': True, **r})
    except Exception as e:
        return err(e)
    finally:
        if tmp.exists():
            tmp.unlink()


# ---------- 通用 ----------

@app.get('/api/files')
def api_files():
    """列出已下载的文件"""
    files = []
    for f in sorted(OUTPUT_DIR.glob('*'), key=lambda x: x.stat().st_mtime, reverse=True):
        if f.is_file():
            files.append({'name': f.name, 'size': f.stat().st_size})
    return jsonify({'ok': True, 'files': files})


@app.get('/files/<path:name>')
def serve_file(name):
    return send_from_directory(str(OUTPUT_DIR), name, as_attachment=True)


def unique_path(p: Path) -> Path:
    """文件名去重：name.m4a -> name (1).m4a"""
    if not p.exists():
        return p
    stem, suffix = p.stem, p.suffix
    i = 1
    while True:
        cand = p.parent / ('%s (%d)%s' % (stem, i, suffix))
        if not cand.exists():
            return cand
        i += 1


def requests_get(url, timeout=60):
    import requests
    r = requests.get(url, headers=kg_api.UA, timeout=timeout, stream=True)
    r.raise_for_status()
    content = b''
    for chunk in r.iter_content(1024 * 256):
        if chunk:
            content += chunk
    return content


if __name__ == '__main__':
    import startup
    startup.serve(app)
