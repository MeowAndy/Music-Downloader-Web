# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

root = Path(SPECPATH)
private_names = {'qq_cookie.txt', 'netease-auth.json', 'anonymous_token', 'neapi_key',
                 'vip_state.json', 'format_settings.json', 'lyrics_cache.json',
                 'startup-report.json', 'AI-install-state.json'}
datas = []
for directory in ('static', 'vendor', 'bin', 'licenses'):
    for path in sorted((root / directory).rglob('*')):
        if path.is_symlink():
            raise ValueError('Release resources must not contain links')
        if not path.is_file():
            continue
        if path.name in private_names or path.suffix.lower() in {'.log', '.tmp', '.pyc'} or path.name.startswith('.env'):
            if path.relative_to(root).as_posix() == 'vendor/netease/node_modules/nwsapi/dist/lint.log':
                continue
            raise ValueError('Private runtime data found in release resources')
        if '__pycache__' in path.parts or 'output' in path.relative_to(root).parts:
            raise ValueError('Runtime directory found in release resources')
        datas.append((str(path), str(path.parent.relative_to(root))))
datas.append((str(root / 'ncm转mp3.exe'), '.'))

a = Analysis(
    [str(root / 'app.py')], pathex=[str(root)], binaries=[], datas=datas,
    hiddenimports=['paho.mqtt.client', 'paho.mqtt.properties',
                   'paho.mqtt.packettypes', 'py7zr', 'PIL.Image', 'bs4'],
    hookspath=[], hooksconfig={}, runtime_hooks=[], excludes=[],
    noarchive=False, optimize=0,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [], name='MusicDownloaderWeb',
    debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
    upx_exclude=[], runtime_tmpdir=None, console=True,
    disable_windowed_traceback=False, argv_emulation=False, target_arch=None,
    codesign_identity=None, entitlements_file=None,
    icon=[str(root / 'app.ico')], version=str(root / 'packaging/windows-version.txt'),
)
