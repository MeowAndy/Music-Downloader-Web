# -*- coding: utf-8 -*-
"""人声/伴奏分离（原生功能）

后端直接调用本地 vocal-separate 工具的 HTTP 接口（Flask+Spleeter，端口 9999）：
  POST /upload   上传音频（multipart，字段 audio；不接受 m4a，需先转 mp3）
  POST /process  分离（表单字段 model=2stems/4stems/5stems + wav_name=上传返回的文件名）
  返回 urllist 为结果音频直链

分离结果自动保存到本应用 output/ 目录，与下载的文件统一管理。
"""
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import requests

import paths

BASE_DIR = paths.APP_DIR
OUTPUT_DIR = paths.OUTPUT_DIR


def _find_vocal_tool():
    """查找人声分离工具目录：exe旁 vocal-separate/ → 本机默认路径"""
    candidates = [
        BASE_DIR / 'vocal-separate',
        Path(r'E:\本地大模型音频提取伴奏人声'),
    ]
    for d in candidates:
        if (d / 'start.exe').is_file():
            return d
    return None


VOCAL_TOOL_DIR = _find_vocal_tool()
VOCAL_EXE = (VOCAL_TOOL_DIR / 'start.exe') if VOCAL_TOOL_DIR else None
VOCAL_URL = 'http://127.0.0.1:9999'
VOCAL_PORT = 9999

# 工具接受的扩展名（m4a 不在其中，需先转 mp3）
TOOL_EXTS = ('.mp3', '.wav', '.flac', '.mp4', '.avi', '.mkv', '.mpeg', '.mov')

# 各模型输出的分轨名（与工具返回的 data 对应）
STEM_NAMES = {
    '2stems': ['伴奏', '人声'],
    '4stems': ['鼓声', '贝斯', '其他', '人声'],
    '5stems': ['鼓声', '贝斯', '钢琴', '其他', '人声'],
}


def running():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(('127.0.0.1', VOCAL_PORT)) == 0


def ensure_running():
    """确保工具在运行（未运行则启动并等待就绪）"""
    if running():
        return
    if not VOCAL_EXE:
        raise RuntimeError('未找到人声分离工具（vocal-separate）。'
                           '可将工具文件夹放到本程序旁边的 vocal-separate/ 目录，'
                           '或保持默认安装路径')
    subprocess.Popen([str(VOCAL_EXE)], cwd=str(VOCAL_TOOL_DIR))
    for _ in range(60):
        if running():
            # 等服务真正可响应
            try:
                requests.get(VOCAL_URL + '/', timeout=3,
                             proxies={'http': None, 'https': None})
                return
            except Exception:
                pass
        time.sleep(0.5)
    raise RuntimeError('人声分离工具启动超时，请手动双击 start.exe 后重试')


def _session():
    s = requests.Session()
    s.proxies = {'http': None, 'https': None}
    s.trust_env = False
    return s


def _prepare_file(src: Path) -> Path:
    """工具不接受 m4a，需要先转成 mp3（同目录临时文件，避免跨盘）"""
    if src.suffix.lower() in TOOL_EXTS:
        return src
    import ffmpeg_api
    if not ffmpeg_api.available():
        raise RuntimeError('m4a 文件需先转 mp3 才能分离，但未找到 ffmpeg')
    tmp = src.with_name('_vocal_tmp_' + src.stem + '.mp3')
    cmd = [ffmpeg_api.FFMPEG, '-y', '-i', str(src),
           '-codec:a', 'libmp3lame', '-qscale:a', '2', str(tmp), '-loglevel', 'error']
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=600)
    except subprocess.TimeoutExpired:
        raise RuntimeError('m4a 转 mp3 超时')
    if r.returncode != 0 or not tmp.is_file() or tmp.stat().st_size < 1024:
        raise RuntimeError('m4a 转 mp3 失败: %s' % r.stderr.decode(errors='ignore')[-150:])
    return tmp


def separate_file(src: Path, model='2stems'):
    """分离一个音频文件，结果保存到 output/，返回结果列表"""
    if model not in STEM_NAMES:
        raise RuntimeError('不支持的模型: %s' % model)
    ensure_running()
    if not src.is_file():
        raise RuntimeError('文件不存在: %s' % src.name)

    prepared = _prepare_file(src)
    s = _session()
    t0 = time.time()

    # 1. 上传
    with open(prepared, 'rb') as f:
        r = s.post(VOCAL_URL + '/upload',
                   files={'audio': (prepared.name, f, 'application/octet-stream')},
                   timeout=600)
    j = r.json() if 'json' in r.headers.get('Content-Type', '') else {}
    if j.get('code') != 0:
        raise RuntimeError('上传失败: %s' % j.get('msg', r.text[:100]))

    # 2. 分离
    r2 = s.post(VOCAL_URL + '/process',
                data={'model': model, 'wav_name': j['data']}, timeout=3600)
    j2 = r2.json() if 'json' in r2.headers.get('Content-Type', '') else {}
    if j2.get('code') != 0:
        raise RuntimeError('分离失败: %s' % str(j2.get('msg', r2.text[:150])))

    # 3. 下载结果到 output/
    stem_names = j2.get('data') or STEM_NAMES[model]
    base = src.stem
    results = []
    for url, stem in zip(j2['urllist'], stem_names):
        try:
            rr = s.get(url, timeout=600)
            rr.raise_for_status()
            out_name = '%s - %s.wav' % (base, stem)
            out = OUTPUT_DIR / out_name
            i = 1
            while out.exists():
                out = OUTPUT_DIR / ('%s - %s (%d).wav' % (base, stem, i))
                i += 1
            out.write_bytes(rr.content)
            results.append({'name': out.name, 'size': len(rr.content), 'stem': stem})
        except Exception:
            pass  # 单个分轨下载失败不影响其他

    # 清理临时转换文件
    if prepared != src and prepared.exists():
        prepared.unlink()

    return {'results': results, 'elapsed': round(time.time() - t0, 1)}
