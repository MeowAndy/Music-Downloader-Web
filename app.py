# -*- coding: utf-8 -*-
"""Music Downloader Web — QQ音乐 + 全民K歌 下载工具的 Web 版

基于 Li4n0/Music-Downloader 与 T-K-233/KG-Downloader 的功能重写，
接口全部更新为当前可用的版本。
"""
import re
import traceback
from pathlib import Path

from flask import Flask, jsonify, request, send_file, send_from_directory

import ffmpeg_api
import kg_api
import paths
import qq_api
import vocal_api
import vip_api

BASE_DIR = paths.APP_DIR
RES_DIR = paths.RES_DIR
OUTPUT_DIR = paths.OUTPUT_DIR

app = Flask(__name__, static_folder=str(RES_DIR / 'static'), static_url_path='/static')
app.config['JSON_AS_ASCII'] = False

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
    body = request.get_json(silent=True) or {}
    cookie = (body.get('cookie') or '').strip()
    if cookie and 'uin' not in cookie:
        return err('Cookie 格式不对，应包含 uin=... 和 qqmusic_key=... 字段')
    qq_api.set_cookie(cookie)
    qq_api._apply_cookie()
    return jsonify({'ok': True, 'set': bool(cookie)})


@app.get('/api/search')
def api_search():
    kw = (request.args.get('kw') or '').strip()
    num = int(request.args.get('num') or 30)
    if not kw:
        return err('请输入搜索关键词')
    try:
        songs = qq_api.search(kw, num=num)
        return jsonify({'ok': True, 'songs': songs})
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
    if not mid:
        return err('缺少 mid')
    if quality not in qq_api.QUALITIES:
        return err('不支持的音质: %s' % quality)
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
        return jsonify({'ok': True, 'name': fpath.name, 'size': fpath.stat().st_size,
                        'quality': actual_q, 'quality_name': qnames.get(actual_q, actual_q)})
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
        vocal_api.start_setup_async()
        return jsonify({'ok': True})
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
    import socket
    import threading
    import webbrowser

    print('=' * 46)
    print('  Music Downloader Web')
    print('  下载目录: %s' % OUTPUT_DIR)
    print('  关闭本窗口即停止服务')
    print('=' * 46)

    def _open_browser():
        try:
            webbrowser.open('http://127.0.0.1:8899')
        except Exception:
            pass

    # 端口被占用说明已有实例在运行，直接打开浏览器
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        already = s.connect_ex(('127.0.0.1', 8899)) == 0
    if already:
        print('服务已在运行，直接打开浏览器...')
        _open_browser()
    else:
        # 隐身自动拉起人声分离工具（不弹窗口/浏览器）
        vocal_api.auto_start_async()
        threading.Timer(2.0, _open_browser).start()
        app.run(host='127.0.0.1', port=8899, debug=False)
