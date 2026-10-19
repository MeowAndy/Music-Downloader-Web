"""Exercise a copied EXE in a fresh directory without importing user credentials."""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
import wave
from pathlib import Path

import requests


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('exe', type=Path)
    parser.add_argument('--online', action='store_true')
    parser.add_argument('--ncm-sample', type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    import sys
    sys.path.insert(0, str(root))
    from startup import BUILD_ID
    destination = Path(tempfile.mkdtemp(prefix='portable-test-' + BUILD_ID + '-中文 空目录-', dir=root))
    exe = destination / args.exe.name
    shutil.copy2(args.exe, exe)
    env = os.environ.copy()
    env.update(PATH=str(Path(os.environ['SystemRoot']) / 'System32'),
               MUSIC_DOWNLOADER_NO_BROWSER='1', PYTHONPATH='')
    env.pop('NETEASE_COOKIE', None)
    session = requests.Session()
    session.trust_env = False
    results = []

    def check(name, valid, detail='', online=False):
        results.append({'name': name, 'passed': bool(valid), 'detail': detail, 'online': online})
        print(('PASS ' if valid else 'FAIL ') + name + ': ' + str(detail), flush=True)

    def run(command, **kwargs):
        return subprocess.run(command, capture_output=True, timeout=90,
                              creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), **kwargs)

    process = None
    try:
        with (destination / 'acceptance-runtime.log').open('wb') as log:
            process = subprocess.Popen([str(exe)], cwd=destination, env=env, stdout=log, stderr=subprocess.STDOUT)
            report_path = destination / 'startup-report.json'
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline and not report_path.exists():
                if process.poll() is not None:
                    raise RuntimeError('Packaged app exited before startup report')
                time.sleep(.5)
            report = json.loads(report_path.read_text(encoding='utf-8'))
            url = report['url']
            identity = session.get(url + '/api/health', timeout=10).json()
            resources = Path(identity['resource_dir'])
            check('fresh-startup', report['ok'] and identity['build_id'] == BUILD_ID, BUILD_ID + '; diagnostics without failures')
            check('isolated-data-and-instance', Path(identity['data_dir']) == destination and identity['instance'] == report['instance'] and resources != root, 'new writable folder, unique instance, extracted resources')
            check('no-account-distributed', not any((destination / p).exists() for p in ['qq_cookie.txt', 'netease-auth.json']), 'no account files copied or embedded')
            checks = [('/api/cookie', 'qq'), ('/api/netease/login/status', 'netease'), ('/api/migu/login/status', 'migu'), ('/api/hifiti/login/status', 'hifiti')]
            for route, name in checks:
                data = session.get(url + route, timeout=15).json()
                check('anonymous-' + name, data.get('ok') is True and data.get('set') is False, 'anonymous status only')
            page = session.get(url + '/', timeout=10).text
            source_page = (root / 'static/index.html').read_text(encoding='utf-8')
            check('exact-current-frontend', page == source_page and 'HiFiTi渠道' in page, 'current four-panel UI and native account modal bundled')
            r = session.get(url + '/api/search', params={'kw': '周深'}, timeout=10)
            check('qq-login-required', r.status_code == 401 and r.json().get('code') == 'LOGIN_REQUIRED', 'fresh accounts must scan before QQ search')
            for route in ['/api/migu/status', '/api/format_status', '/api/vocal/status']:
                data = session.get(url + route, timeout=15).json()
                check(route, data.get('ok') is True, 'status returned; no AI installation requested')
            with wave.open(str(destination / 'output/portable-tone.wav'), 'wb') as wav:
                wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(44100)
                wav.writeframes(b'\0\0' * 44100)
            converted = session.post(url + '/api/convert_file', json={'name':'portable-tone.wav', 'to':'mp3'}, timeout=30).json()
            audio = destination / 'output' / converted.get('name', 'missing')
            decoded = run([str(resources / 'bin/ffmpeg.exe'), '-v', 'error', '-xerror', '-i', str(audio), '-f', 'null', '-'], env=env)
            check('bundled-ffmpeg-convert-decode', converted.get('ok') and converted.get('size', 0) > 1024 and decoded.returncode == 0, 'generated one-second WAV converted and fully decoded')
            with tempfile.TemporaryDirectory(prefix='portable-node-selftest-') as private:
                node_env = dict(env, TMP=private, TEMP=private, TMPDIR=private)
                result = run([str(resources / 'bin/node.exe'), str(resources / 'vendor/netease_bridge.js')], input=b'{"op":"selftest"}', cwd=private, env=node_env)
                body = json.loads(result.stdout)
                check('bundled-netease-dependency-selftest', result.returncode == 0 and body.get('ok') and body.get('body', {}).get('ready'), 'real bundled Node imports all exposed modules and generates QR PNG offline')
            tool = resources / 'ncm转mp3.exe'
            tool_hash = hashlib.sha256(tool.read_bytes()).hexdigest()
            check('bundled-ncm-pinned', tool_hash == 'a1f6f6ce87500b7b1f2a89dbf85b13e81d327eea4641daf8afe0ab840f2c518c', 'official 1.5.1 unchanged binary')
            if args.ncm_sample:
                original = hashlib.sha256(args.ncm_sample.read_bytes()).hexdigest()
                with tempfile.TemporaryDirectory(prefix='portable-ncm-copy-') as private:
                    work = Path(private); sample = work / 'input.ncm'; shutil.copy2(args.ncm_sample, sample)
                    output = work / 'out'; output.mkdir()
                    result = run([str(tool), '-o', str(output), str(sample)], cwd=work, env=env)
                    files = list(output.iterdir())
                    valid = result.returncode == 0 and len(files) == 1
                    if valid:
                        result = run([str(resources / 'bin/ffmpeg.exe'), '-v', 'error', '-xerror', '-i', str(files[0]), '-f', 'null', '-'], env=env)
                        valid = result.returncode == 0
                    check('bundled-ncm-real-sample', valid and hashlib.sha256(args.ncm_sample.read_bytes()).hexdigest() == original, 'copied authorized sample converted and fully decoded; original unchanged')
            if args.online:
                for route, query, name, key in [
                    ('/api/netease/search', {'kw':'复刻回忆'}, 'netease-real-search', 'songs'),
                    ('/api/migu/search', {'kw':'周深 泪桥'}, 'migu-real-search', 'songs'),
                    ('/api/hifiti/search', {'kw':'周深 泪桥'}, 'hifiti-real-search', 'items'),
                    ('/api/playlist', {'id':'7707261125'}, 'qq-node-real-playlist', 'songs'),
                ]:
                    try:
                        response = session.get(url + route, params=query, timeout=70)
                        data = response.json()
                        check(name, data.get('ok') is True and bool(data.get(key)), 'HTTP %d; %d results' % (response.status_code, len(data.get(key, []))), online=True)
                    except Exception as exc:
                        check(name, False, type(exc).__name__, online=True)
                for name, start, status, cancel, csrf_route in [
                    ('qq', '/api/login/qr/start', '/api/login/qr/status', '/api/login/qr/cancel', None),
                    ('netease', '/api/netease/login/qr/start', '/api/netease/login/qr/status', '/api/netease/login/qr/cancel', None),
                    ('migu', '/api/migu/login/qr/start', '/api/migu/login/qr/status', '/api/migu/login/qr/cancel', '/api/migu/login/status'),
                ]:
                    task = None; headers = {}
                    try:
                        if csrf_route:
                            headers['X-CSRF-Token'] = session.get(url + csrf_route, timeout=10).json()['csrf']
                        data = session.post(url + start, headers=headers, timeout=60).json()
                        task = data.get('id')
                        image = data.get('image', data.get('qr', ''))
                        valid = data.get('ok') is True and task and image.startswith('data:image/')
                        check(name + '-real-qr-created', valid, 'official QR created; no scan/confirmation performed', online=True)
                        if task:
                            poll = session.get(url + status, params={'id':task}, timeout=40).json()
                            check(name + '-real-qr-wait', poll.get('ok') is True and poll.get('status') in ('waiting','scanned'), str(poll.get('status', poll.get('code', 'unavailable'))), online=True)
                    except Exception as exc:
                        check(name + '-real-qr', False, type(exc).__name__, online=True)
                    finally:
                        if task:
                            try:
                                response = session.post(url + cancel, json={'id':task}, headers=headers, timeout=15)
                                check(name + '-qr-cancel', response.ok and response.json().get('ok') is True, 'own test task cancelled', online=True)
                            except Exception as exc:
                                check(name + '-qr-cancel', False, type(exc).__name__, online=True)
    finally:
        if process is not None and process.poll() is None:
            subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], capture_output=True, timeout=20)
            process.wait(timeout=20)
        summary = {'version':'1.2.0', 'same_host_not_clean_machine':True,
                   'only_exe_copied':True, 'credentials_imported':False,
                   'developer_tools_removed_from_path':True, 'results':results}
        (destination / 'acceptance-summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
        print('ACCEPTANCE_DIRECTORY=' + str(destination), flush=True)
    return 0 if results and all(r['passed'] for r in results if not r['online']) else 1


if __name__ == '__main__':
    raise SystemExit(main())
