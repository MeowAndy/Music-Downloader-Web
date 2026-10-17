"""Copy-only NCM extraction using the supplied, pinned native converter."""
import hashlib
import os
import shutil
import subprocess
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path

import paths

TOOL_NAME = 'ncm转mp3.exe'
TOOL_SHA256 = 'a1f6f6ce87500b7b1f2a89dbf85b13e81d327eea4641daf8afe0ab840f2c518c'
NCM_MAGIC = b'CTENFDAM'
MAX_BYTES = 1024 * 1024 * 1024
_SLOTS = threading.BoundedSemaphore(2)


class ConversionError(RuntimeError):
    pass


def is_ncm(source):
    with Path(source).open('rb') as stream:
        return stream.read(8) == NCM_MAGIC


def _tool():
    # Never resolve this unsigned converter through PATH or a writable account directory.
    tool = paths.RES_DIR / TOOL_NAME
    try:
        if hashlib.sha256(tool.read_bytes()).hexdigest() != TOOL_SHA256:
            raise ConversionError('NCM 转换工具校验失败。')
    except OSError:
        raise ConversionError('未找到内置 NCM 转换工具。') from None
    return tool


def _run(command, cwd, timeout):
    try:
        result = subprocess.run(command, cwd=cwd, stdin=subprocess.DEVNULL,
                                capture_output=True, timeout=timeout, shell=False,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except subprocess.TimeoutExpired:
        raise ConversionError('NCM 转换或音频验证超时。') from None
    except OSError:
        raise ConversionError('无法启动 NCM 转换或音频验证工具。') from None
    if result.returncode:
        raise ConversionError('NCM 转换或音频验证失败。')
    return result


def _validate_audio(audio, cwd):
    extension = audio.suffix.lower()
    with audio.open('rb') as stream:
        head = stream.read(10)
    if extension == '.flac':
        valid = head.startswith(b'fLaC')
    else:
        valid = head.startswith(b'ID3') or (len(head) >= 2 and head[0] == 255 and head[1] & 224 == 224)
    if not valid:
        raise ConversionError('NCM 转换结果不是有效的 MP3／FLAC。')
    ffmpeg = paths.find_exe('ffmpeg.exe')
    if not ffmpeg:
        raise ConversionError('未找到 FFmpeg，无法验证 NCM 转换结果。')
    result = _run([ffmpeg, '-hide_banner', '-nostdin', '-v', 'error', '-xerror',
                   '-err_detect', 'explode', '-i', str(audio), '-map', '0:a:0',
                   '-vn', '-progress', 'pipe:1', '-f', 'null', '-'], cwd, 180)
    if not any(line.startswith(b'out_time_us=') and line[12:].isdigit()
               and int(line[12:]) > 0 for line in result.stdout.splitlines()):
        raise ConversionError('NCM 转换结果没有可播放的音频。')


@contextmanager
def extracted_audio(source):
    """Yield a validated temporary MP3/FLAC; never modify the source file."""
    source = Path(source)
    try:
        if not source.is_file() or not 0 < source.stat().st_size <= MAX_BYTES or not is_ncm(source):
            raise ConversionError('文件不是有效的 NCM。')
        tool = _tool()
        if not _SLOTS.acquire(timeout=5):
            raise ConversionError('NCM 转换繁忙，请稍后重试。')
        try:
            with tempfile.TemporaryDirectory(prefix='music-ncm-') as work:
                cwd = Path(work)
                output = cwd / 'output'
                output.mkdir()
                staged = cwd / 'input.ncm'
                shutil.copyfile(source, staged)
                _run([str(tool), '-o', str(output), str(staged)], cwd, 120)
                files = list(output.iterdir())
                if (len(files) != 1 or files[0].is_symlink() or not files[0].is_file()
                        or files[0].suffix.lower() not in {'.mp3', '.flac'}
                        or not 1024 <= files[0].stat().st_size <= MAX_BYTES):
                    raise ConversionError('NCM 转换未生成唯一有效的 MP3／FLAC。')
                _validate_audio(files[0], cwd)
                yield files[0]
        finally:
            _SLOTS.release()
    except ConversionError:
        raise
    except OSError:
        raise ConversionError('NCM 转换文件读写失败。') from None
