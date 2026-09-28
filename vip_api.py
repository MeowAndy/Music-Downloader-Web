# -*- coding: utf-8 -*-
"""VIP 歌曲 mflac 解密转换 + QQ音乐下载目录自动监控

流程：QQ音乐客户端（登录VIP账号）下载付费歌曲 → VipSongsDownload 目录出现 .mflac
→ 本模块自动检测并调用 Node 解密（um_decrypt.js, Unlock Music WASM）→
输出可用 flac 到 web/output/
"""
import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path

import paths

BASE_DIR = paths.APP_DIR
UM_DECRYPT = paths.RES_DIR / 'vendor' / 'um' / 'um_decrypt.js'
NODE = paths.node_exe()
OUTPUT_DIR = paths.OUTPUT_DIR
STATE_FILE = BASE_DIR / 'vip_state.json'

# 支持的加密扩展名
CRYPT_EXTS = ('.mflac', '.mflac0', '.mflach', '.mgg', '.mgg0', '.mgg1', '.mggl',
              '.mmp4', '.qmcflac', '.qmcogg', '.qmc0', '.qmc2', '.qmc3',
              '.qmc4', '.qmc6', '.qmc8')

# 默认监控目录：自动探测 QQ音乐客户端的下载目录
DEFAULT_FOLDERS = [
    Path.home() / 'Music' / 'VipSongsDownload',
    Path.home() / 'Music' / 'QQ音乐下载',
    Path('D:/qqmusicdownload'),
]


def _default_watch_folder():
    for f in DEFAULT_FOLDERS:
        if f.is_dir():
            return str(f)
    return ''


def _load_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding='utf-8'))
    except Exception:
        return {}


def _save_state(st):
    try:
        STATE_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding='utf-8')
    except Exception:
        pass


class VipMonitor:
    """mflac 手动转换（无后台线程，按需扫描/转换）"""

    def __init__(self):
        st = _load_state()
        self.watch_folder = st.get('watch_folder') or _default_watch_folder()
        self._lock = threading.Lock()
        self.converting = None   # 正在转换的文件名
        self.history = []        # 转换记录 [{name, title, artist, ok, error, time}]

    def set_folder(self, folder):
        with self._lock:
            self.watch_folder = folder
            self._persist()
        return self.status()

    def _persist(self):
        _save_state({'watch_folder': self.watch_folder})

    def _output_exists(self, crypt_name):
        """检查加密文件是否已有对应的转换输出"""
        base = re.sub(r'\.(mflac|mgg|qmc)\w*$', '', crypt_name, flags=re.I)
        return any((OUTPUT_DIR / (base + ext)).exists()
                   for ext in ('.flac', '.mp3', '.ogg', '.mp4', '.m4a', '.wav'))

    def convert_all(self):
        """转换目录中所有未转换的加密文件，返回 (成功数, 失败数)"""
        files = [f for f in self.list_folder() if not f['converted']]
        ok, fail = 0, 0
        for f in files:
            try:
                r = self.convert_now(f['path'])
                self._add_history(f['name'], r['title'], r['artist'], True, '')
                ok += 1
            except Exception as e:
                self._add_history(f['name'], '', '', False, str(e)[:120])
                fail += 1
        return ok, fail

    def _convert(self, mflac_path):
        """调用 Node 解密（子进程隔离，防崩溃）"""
        if not paths.has_node():
            raise RuntimeError('未找到 Node.js，无法解密 mflac')
        cmd = [NODE, str(UM_DECRYPT), mflac_path, str(OUTPUT_DIR)]
        try:
            res = subprocess.run(cmd, capture_output=True, timeout=600)
        except subprocess.TimeoutExpired:
            raise RuntimeError('转换超时（10分钟）')
        out = res.stdout.decode('utf-8', errors='ignore').strip()
        # stdout 里找 JSON 行
        for line in out.splitlines():
            line = line.strip()
            if line.startswith('{') and '"ok"' in line:
                j = json.loads(line)
                if j.get('ok'):
                    return j
                raise RuntimeError(j.get('error', '解密失败'))
        err = res.stderr.decode('utf-8', errors='ignore').strip()[-200:]
        raise RuntimeError(err or '解密失败')

    def _add_history(self, name, title, artist, ok, error):
        with self._lock:
            self.history.insert(0, {
                'name': name, 'title': title, 'artist': artist,
                'ok': ok, 'error': error, 'time': time.strftime('%H:%M:%S'),
            })
            del self.history[50:]  # 只保留最近50条

    def convert_now(self, path):
        """手动立即转换单个文件"""
        p = Path(path)
        if not p.is_file():
            raise RuntimeError('文件不存在')
        if not p.name.lower().endswith(CRYPT_EXTS):
            raise RuntimeError('不支持的文件格式（支持 mflac/mgg/qmc 系列）')
        return self._convert(str(p))

    def list_folder(self):
        """列出目录中的加密文件及转换状态（按需扫描）"""
        folder = Path(self.watch_folder) if self.watch_folder else None
        files = []
        if folder and folder.is_dir():
            for f in sorted(folder.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
                if f.is_file() and f.name.lower().endswith(CRYPT_EXTS):
                    files.append({'name': f.name, 'size': f.stat().st_size,
                                  'path': str(f), 'converted': self._output_exists(f.name)})
        return files

    def status(self):
        return {
            'watch_folder': self.watch_folder,
            'folder_exists': bool(self.watch_folder) and Path(self.watch_folder).is_dir(),
            'converting': dict(self.converting) if self.converting else None,
            'history': self.history[:20],
        }


monitor = VipMonitor()
