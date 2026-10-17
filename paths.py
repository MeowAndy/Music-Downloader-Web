# -*- coding: utf-8 -*-
"""路径解析：兼容源码运行与 PyInstaller 打包

- RES_DIR: 只读资源（static/vendor/bin），打包后在解包目录
- APP_DIR: 可写数据（output/设置文件），打包后在 exe 旁边
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

def writable_dir(directory):
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=directory):
            pass
        return True
    except OSError:
        return False


if getattr(sys, 'frozen', False):
    RES_DIR = Path(sys._MEIPASS)
    EXE_DIR = Path(sys.executable).parent
    APP_DIR = EXE_DIR
    if not writable_dir(APP_DIR) or not writable_dir(APP_DIR / 'output'):
        APP_DIR = Path(os.environ.get('LOCALAPPDATA') or Path.home()) / 'MusicDownloaderWeb'
else:
    RES_DIR = Path(__file__).parent
    EXE_DIR = RES_DIR
    APP_DIR = RES_DIR

OUTPUT_DIR = APP_DIR / 'output'
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# 外部工具的查找位置（打包内置优先，其次 exe 旁 bin/，最后 PATH）
_BIN_CANDIDATES = [RES_DIR / 'bin', APP_DIR / 'bin']


def find_exe(name):
    """查找可执行文件：内置 bin/ → exe旁bin/ → PATH"""
    for d in _BIN_CANDIDATES:
        p = d / name
        if p.is_file():
            return str(p)
    found = shutil.which(name)
    return found  # None 表示没找到


def node_exe():
    """Node.js 可执行文件路径（找不到返回 'node' 交由系统报错）"""
    return find_exe('node.exe') or 'node'


def has_node():
    return find_exe('node.exe') is not None
