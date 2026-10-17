"""Build into a new directory; never package a used application/data directory."""
import hashlib
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import ncm_api
import startup


def main():
    for relative in (*startup.REQUIRED_RESOURCES, 'bin/node.exe', 'bin/ffmpeg.exe',
                     'app.ico', 'packaging/windows-version.txt',
                     'licenses/ncmdump-MIT.txt', 'licenses/node-LICENSE.txt',
                     'licenses/ffmpeg-GPLv3.txt'):
        if not (ROOT / relative).is_file():
            raise SystemExit('Missing release input: ' + relative)
    ncm_api._tool()
    destination = ROOT / 'release-artifacts' / ('v' + startup.BUILD_ID + '-' + datetime.now().strftime('%Y%m%d-%H%M%S'))
    destination.mkdir(parents=True, exist_ok=False)
    subprocess.run([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean',
                    '--distpath', str(destination / 'dist'), '--workpath', str(destination / 'build'),
                    str(ROOT / 'MusicDownloaderWeb.spec')], cwd=ROOT, check=True)
    exe = destination / 'dist' / 'MusicDownloaderWeb.exe'
    from PyInstaller.archive.readers import CArchiveReader
    archive = CArchiveReader(str(exe))
    names = [name.replace('\\', '/') for name in archive.toc]
    forbidden = {'qq_cookie.txt', 'netease-auth.json', 'anonymous_token', 'neapi_key',
                 'lyrics_cache.json', 'vip_state.json', 'startup-report.json'}
    for name in names:
        path = Path(name)
        if path.name in forbidden or 'output' in path.parts or 'vocal-separate' in path.parts:
            raise SystemExit('Private/runtime resource in EXE')
    for relative in startup.REQUIRED_RESOURCES:
        assert relative in names, relative
    summary = {'version': startup.BUILD_ID, 'exe_bytes': exe.stat().st_size,
               'sha256': hashlib.sha256(exe.read_bytes()).hexdigest(),
               'embedded_entries': len(names), 'private_runtime_entries': 0}
    (destination / 'build-summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    print(json.dumps({'directory': str(destination), **summary}, indent=2))


if __name__ == '__main__':
    main()
