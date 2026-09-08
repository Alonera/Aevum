"""Assemble verified release assets locally. Never pushes, tags or publishes."""
import argparse
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WINDOWS = ('Aevum.exe', 'Aevum-Setup.exe')
LINUX = ('Aevum-x86_64.AppImage', 'Aevum-linux-x86_64.tar.gz')
SOURCE_FILES = ('ytdl_tray.py', 'aevum_cookies.py', 'version.txt', 'installer.iss',
                'build-linux.sh', 'build-release.ps1', 'prepare-release.py', 'release-notes.md')


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def checksum_text(records):
    """The v1.2.6 updater requires basename FIRST, hash LAST (not GNU format)."""
    lines = []
    seen = set()
    for name, sha in records:
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', name) or name in seen:
            raise ValueError('Invalid or duplicate asset basename')
        if not re.fullmatch(r'[0-9a-f]{64}', sha):
            raise ValueError('Invalid SHA-256')
        seen.add(name)
        lines.append(f'{name}  {sha}')
    return '\n'.join(lines) + '\n'


def provenance(root=ROOT):
    def git(*args):
        return subprocess.check_output(['git', '-C', str(root), *args], text=True).strip()
    version = re.search(r'^APP_VERSION = "([0-9.]+)"', (root / 'ytdl_tray.py').read_text(encoding='utf-8'), re.M)[1]
    # Compare logical source bytes so a Windows CRLF checkout and Linux LF
    # checkout identify the same source. Refuse uncommitted packaged inputs.
    hashes = {}
    for name in SOURCE_FILES:
        data = (root / name).read_bytes().replace(b'\r\n', b'\n')
        committed = subprocess.check_output(['git', '-C', str(root), 'show', 'HEAD:' + name]).replace(b'\r\n', b'\n')
        if data != committed:
            raise ValueError(f'Commit packaged source before building: {name}')
        hashes[name] = hashlib.sha256(data).hexdigest()
    return {'version': version, 'commit': git('rev-parse', 'HEAD'), 'files': hashes}


def record(path, relative=None):
    path = Path(path)
    return {'file': relative or path.name, 'bytes': path.stat().st_size, 'sha256': digest(path)}


def write_json(path, data):
    Path(path).write_text(json.dumps(data, indent=2) + '\n', encoding='utf-8')


def stamp_windows(directory):
    path = Path(directory) / 'build-manifest.json'
    manifest = json.loads(path.read_text(encoding='utf-8-sig'))
    manifest['source'] = provenance()
    manifest['sourceIncludesUncommittedChanges'] = False
    write_json(path, manifest)


def linux_manifest():
    manifest = {'source': provenance(), 'artifacts': [record(ROOT / name) for name in LINUX],
                'ytDlp': subprocess.check_output([str(ROOT / 'bin/yt-dlp'), '--version'], text=True).strip(),
                'binaries': [record(ROOT / 'bin' / name) for name in ('yt-dlp', 'ffmpeg', 'ffprobe')]}
    write_json(ROOT / 'linux-build-manifest.json', manifest)
    (ROOT / 'checksums-linux.txt').write_text(checksum_text((item['file'], item['sha256']) for item in manifest['artifacts']), encoding='ascii')


def checked_asset(directory, entry):
    root = Path(directory).resolve()
    path = (root / entry['file']).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError('Missing or unsafe asset path')
    if path.stat().st_size != entry['bytes'] or digest(path) != entry['sha256']:
        raise ValueError(f'Asset does not match its manifest: {path.name}')
    return path


def assemble(windows, linux, output):
    output = Path(output).resolve()
    if output.exists():
        raise ValueError('Output already exists; use a new directory')
    if not output.is_relative_to(ROOT / 'releases'):
        raise ValueError('Output must be inside this checkout releases directory')
    win = json.loads((Path(windows) / 'build-manifest.json').read_text(encoding='utf-8-sig'))
    lin = json.loads((Path(linux) / 'linux-build-manifest.json').read_text(encoding='utf-8'))
    expected = provenance()
    if win.get('source') != expected or lin.get('source') != expected:
        raise ValueError('Windows, Linux and checkout must have identical committed source')
    if win['ytDlp'] != lin['ytDlp']:
        raise ValueError('Windows and Linux must bundle the same yt-dlp version')
    files = []
    for directory, manifest, names in ((windows, win, WINDOWS), (linux, lin, LINUX)):
        for name in names:
            matches = [item for item in manifest['artifacts'] if Path(item['file']).name == name]
            if len(matches) != 1:
                raise ValueError(f'Expected exactly one artifact: {name}')
            files.append(checked_asset(directory, matches[0]))
    output.mkdir(parents=True)
    for path in files:
        shutil.copy2(path, output / path.name)
    records = [record(output / name) for name in (*WINDOWS, *LINUX)]
    checksums = checksum_text((item['file'], item['sha256']) for item in records)
    (output / 'checksums.txt').write_text(checksums, encoding='ascii')
    notes = (ROOT / 'release-notes.md').read_text(encoding='utf-8')
    notes += '\n## Checksums (SHA-256)\n\n```\n' + checksums + '```\n\n'
    notes += 'VirusTotal hash lookups: ' + ' · '.join(
        f"[{item['file']}](https://www.virustotal.com/gui/file/{item['sha256']})" for item in records)
    notes += '\n\nThese links identify the files; they are not a claim that scans have completed.\n'
    (output / 'RELEASE-NOTES.md').write_text(notes, encoding='utf-8')
    write_json(output / 'release-manifest.json', {'source': expected, 'ytDlp': win['ytDlp'],
               'artifacts': records, 'published': False, 'signed': False})
    print(f'Release candidate assembled (NOT published): {output}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    windows = sub.add_parser('stamp-windows')
    windows.add_argument('directory', type=Path)
    sub.add_parser('linux-manifest')
    release = sub.add_parser('assemble')
    release.add_argument('--windows', type=Path, required=True)
    release.add_argument('--linux', type=Path, required=True)
    release.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.command == 'stamp-windows':
        stamp_windows(args.directory)
    elif args.command == 'linux-manifest':
        linux_manifest()
    else:
        assemble(args.windows, args.linux, args.output)
