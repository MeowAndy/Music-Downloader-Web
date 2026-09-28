# -*- coding: utf-8 -*-
"""音频格式转换（基于 ffmpeg：内置 bin/ → exe旁 → PATH）"""
import json
import subprocess
from pathlib import Path

import paths

BASE_DIR = paths.APP_DIR
OUTPUT_DIR = paths.OUTPUT_DIR
SETTINGS_FILE = BASE_DIR / 'format_settings.json'


def find_ffmpeg():
    return paths.find_exe('ffmpeg.exe')


FFMPEG = find_ffmpeg()


def available():
    return FFMPEG is not None


def get_settings():
    try:
        return json.loads(SETTINGS_FILE.read_text(encoding='utf-8'))
    except Exception:
        return {'output_format': 'original'}  # original | mp3


def save_settings(st):
    SETTINGS_FILE.write_text(json.dumps(st, ensure_ascii=False), encoding='utf-8')


def convert_file(src: Path, fmt: str = 'mp3'):
    """转换音频文件格式，返回输出文件 Path。fmt: mp3/flac/ogg"""
    if not FFMPEG:
        raise RuntimeError('未找到 ffmpeg，无法转换格式')
    if not src.is_file():
        raise RuntimeError('源文件不存在: %s' % src.name)
    out = src.with_suffix('.' + fmt)
    cmd = [FFMPEG, '-y', '-i', str(src)]
    if fmt == 'mp3':
        cmd += ['-codec:a', 'libmp3lame', '-qscale:a', '2']  # VBR 高质量
        # 保留元数据
        cmd += ['-id3v2_version', '3', '-write_id3v1', '1']
    elif fmt == 'flac':
        cmd += ['-codec:a', 'flac']
    elif fmt == 'ogg':
        cmd += ['-codec:a', 'libvorbis', '-qscale:a', '5']
    else:
        raise RuntimeError('不支持的格式: %s' % fmt)
    cmd += ['-loglevel', 'error', str(out)]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=600)
    except subprocess.TimeoutExpired:
        raise RuntimeError('转换超时')
    if r.returncode != 0 or not out.is_file() or out.stat().st_size < 1024:
        err = r.stderr.decode('utf-8', errors='ignore').strip()[-200:]
        raise RuntimeError('转换失败: %s' % err)
    return out
