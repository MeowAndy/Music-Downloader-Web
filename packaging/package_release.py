"""Make a fresh allowlisted ZIP and checksums; never copy the surrounding dist folder."""
import argparse
import hashlib
import shutil
import zipfile
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('exe', type=Path)
    parser.add_argument('destination', type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    destination = args.destination.resolve()
    destination.mkdir(parents=True, exist_ok=False)
    exe = destination / 'MusicDownloaderWeb.exe'
    shutil.copy2(args.exe, exe)
    entries = [(exe, exe.name), (root / 'packaging/使用说明.txt', '使用说明.txt')]
    entries.extend((path, 'licenses/' + path.name) for path in sorted((root / 'licenses').iterdir()) if path.is_file())
    archive = destination / 'MusicDownloaderWeb-v1.2.0-Windows-x64.zip'
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as output:
        for path, name in entries:
            output.write(path, name)
    with zipfile.ZipFile(archive) as output:
        assert set(output.namelist()) == {name for _, name in entries}
        assert output.testzip() is None
    with (destination / 'SHA256SUMS.txt').open('w', encoding='ascii') as checksums:
        for path in (exe, archive):
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            checksums.write(digest + '  ' + path.name + '\n')
            print(path.name, path.stat().st_size, digest)
    print('RELEASE_DIRECTORY=' + str(destination))


if __name__ == '__main__':
    main()
