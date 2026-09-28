# -*- coding: utf-8 -*-
"""一键启动 + 自测：检查依赖 → 启动服务 → 接口冒烟测试 → 打开浏览器"""
import importlib
import json
import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

BASE = Path(__file__).parent
os.chdir(BASE)
HOST, PORT = '127.0.0.1', 8899
URL = 'http://%s:%d' % (HOST, PORT)

# 测试用的已知可用样例
KG_SONG_URL = 'https://kg.qq.com/node/play?s=V8usC3VgzPFmYVdd'
PLAYLIST_ID = '7707261125'

results = []  # (名称, 状态, 详情) 状态: ok/warn/fail


def record(name, status, detail=''):
    results.append((name, status, detail))
    mark = {'ok': '[✓]', 'warn': '[!]', 'fail': '[x]'}[status]
    print('  %s %s%s' % (mark, name, (' — ' + detail) if detail else ''))


def section(title):
    print('\n' + title)


# ---------- 1. 依赖检查 ----------

def check_deps():
    section('── 依赖检查 ──')
    for mod, pip_name in [('flask', 'flask'), ('requests', 'requests')]:
        try:
            importlib.import_module(mod)
            record('Python 包 %s' % mod, 'ok')
        except ImportError:
            print('  ... 正在自动安装 %s' % pip_name)
            r = subprocess.run([sys.executable, '-m', 'pip', 'install', pip_name],
                               capture_output=True, timeout=120)
            if r.returncode == 0:
                record('Python 包 %s' % mod, 'ok', '已自动安装')
            else:
                record('Python 包 %s' % mod, 'fail', '安装失败，请手动: pip install %s' % pip_name)
    # Node（歌单接口需要）
    try:
        r = subprocess.run(['node', '--version'], capture_output=True, timeout=10)
        ver = r.stdout.decode(errors='ignore').strip()
        record('Node.js', 'ok', ver)
        return True
    except Exception:
        record('Node.js', 'warn', '未安装，QQ音乐歌单功能不可用（其他功能正常）')
        return False


# ---------- 2. 服务启动 ----------

def port_in_use():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((HOST, PORT)) == 0


def start_server():
    section('── 启动服务 ──')
    if port_in_use():
        record('端口 %d' % PORT, 'ok', '服务已在运行，直接复用')
        return None
    log = open(BASE / 'server.log', 'w', encoding='utf-8')
    proc = subprocess.Popen([sys.executable, 'app.py'], stdout=log, stderr=log)
    for i in range(30):
        if port_in_use():
            record('Flask 服务', 'ok', URL)
            return proc
        if proc.poll() is not None:
            break
        time.sleep(0.5)
    record('Flask 服务', 'fail', '启动失败，查看 server.log')
    return None


# ---------- 3. 接口自测 ----------

def http_get(path, timeout=30):
    import requests
    r = requests.get(URL + path, timeout=timeout)
    return r


def run_tests(has_node):
    import requests
    section('── 接口自测 ──')

    # 首页
    try:
        r = http_get('/', timeout=10)
        record('首页页面', 'ok' if r.status_code == 200 and 'Music Downloader' in r.text else 'fail',
               'HTTP %d' % r.status_code)
    except Exception as e:
        record('首页页面', 'fail', str(e)[:60])

    # Cookie 状态
    cookie_set = False
    try:
        j = http_get('/api/cookie', timeout=10).json()
        cookie_set = j.get('set')
        record('QQ音乐登录 Cookie', 'ok' if cookie_set else 'warn',
               ('已登录 QQ %s' % j.get('uin')) if cookie_set else '未设置，搜索功能不可用（页面右上角可设置）')
    except Exception as e:
        record('QQ音乐登录 Cookie', 'fail', str(e)[:60])

    # 文件列表
    try:
        j = http_get('/api/files', timeout=10).json()
        record('已下载文件接口', 'ok', '%d 个文件' % len(j.get('files', [])))
    except Exception as e:
        record('已下载文件接口', 'fail', str(e)[:60])

    # QQ音乐搜索（需要 cookie）
    if cookie_set:
        try:
            j = http_get('/api/search?kw=%E5%91%A8%E6%B7%B1', timeout=60).json()
            n = len(j.get('songs', []))
            if j.get('ok') and n:
                record('QQ音乐搜索', 'ok', '关键词「周深」返回 %d 首' % n)
            else:
                record('QQ音乐搜索', 'warn', '接口返回空（可能被临时限流，稍后重试）')
        except Exception as e:
            record('QQ音乐搜索', 'fail', str(e)[:60])
    else:
        record('QQ音乐搜索', 'warn', '跳过（未配置 Cookie）')

    # QQ音乐歌单（需要 Node）
    if has_node:
        try:
            j = http_get('/api/playlist?id=%s' % PLAYLIST_ID, timeout=60).json()
            if j.get('ok'):
                record('QQ音乐歌单', 'ok', '「%s」%d 首' % (j.get('name', '')[:20], j.get('song_count', 0)))
            else:
                record('QQ音乐歌单', 'fail', j.get('error', '')[:60])
        except Exception as e:
            record('QQ音乐歌单', 'fail', str(e)[:60])
    else:
        record('QQ音乐歌单', 'warn', '跳过（未安装 Node.js）')

    # 全民K歌单曲解析
    try:
        r = requests.post(URL + '/api/parse', json={'url': KG_SONG_URL}, timeout=30)
        j = r.json()
        if j.get('ok') and j.get('type') == 'kg_song':
            d = j['data']
            record('全民K歌解析', 'ok', '「%s」- %s' % (d.get('song_name'), d.get('nick')))
        else:
            record('全民K歌解析', 'fail', j.get('error', '')[:60])
    except Exception as e:
        record('全民K歌解析', 'fail', str(e)[:60])

    # VIP mflac 转换模块（需要 um-web 工具 + Node）
    try:
        j = http_get('/api/vip/status', timeout=10).json()
        if j.get('ok'):
            detail = '%d 个加密文件待手动转换' % len(j.get('files', []))
            if not j.get('watch_folder'):
                detail += ' · 未找到mflac目录，可在页面中设置'
            record('mflac转换模块', 'ok', detail)
        else:
            record('mflac转换模块', 'fail', j.get('error', '')[:60])
    except Exception as e:
        record('mflac转换模块', 'fail', str(e)[:60])

    # 格式转换（ffmpeg）
    try:
        j = http_get('/api/format_status', timeout=10).json()
        if j.get('ffmpeg'):
            record('格式转换 ffmpeg', 'ok', '支持下载转MP3 / 文件转MP3')
        else:
            record('格式转换 ffmpeg', 'warn', '未找到 ffmpeg，转换功能不可用')
    except Exception as e:
        record('格式转换 ffmpeg', 'fail', str(e)[:60])

    # 人声分离工具
    try:
        j = http_get('/api/vocal/status', timeout=10).json()
        if j.get('running'):
            record('人声分离工具', 'ok', 'AI引擎运行中（2/4/5分轨）')
        else:
            record('人声分离工具', 'warn', '未启动（首次使用时会自动启动，约10秒）')
    except Exception as e:
        record('人声分离工具', 'fail', str(e)[:60])


# ---------- 汇总 ----------

def summary(proc):
    section('── 结果汇总 ──')
    fails = [r for r in results if r[1] == 'fail']
    warns = [r for r in results if r[1] == 'warn']
    if not fails:
        print('  自测通过！%d 项正常，%d 项提醒' % (
            len(results) - len(warns) - len(fails), len(warns)))
    else:
        print('  有 %d 项失败，%d 项提醒（详见上方）' % (len(fails), len(warns)))
    print('\n  浏览器打开: %s   （关闭本窗口或按 Ctrl+C 停止服务）' % URL)
    try:
        webbrowser.open(URL)
    except Exception:
        pass
    # 保持服务运行
    if proc:
        try:
            proc.wait()
        except KeyboardInterrupt:
            proc.terminate()
    else:
        try:
            input('\n  按回车退出...')
        except (KeyboardInterrupt, EOFError):
            pass


def main():
    print('=' * 46)
    print('  Music Downloader Web · 一键启动自测')
    print('=' * 46)
    has_node = check_deps()
    proc = start_server()
    if proc is None and not port_in_use():
        print('\n  服务未能启动，自测中止。')
        input('按回车退出...')
        return
    run_tests(has_node)
    summary(proc)


if __name__ == '__main__':
    main()
