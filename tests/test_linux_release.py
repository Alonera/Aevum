"""Opt-in Linux package smoke tests: own processes and synthetic profiles only."""
import json
import os
import signal
import socket
import sqlite3
import subprocess
import tarfile
import time
import urllib.request
from pathlib import Path

import pytest
from test_media import media

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(os.name == 'nt' or os.environ.get('AEVUM_LINUX_ARTIFACTS') != '1',
                               reason='opt-in built Linux packages')


def test_linux_tarball_structure_and_cookie_module():
    from PyInstaller.archive.readers import CArchiveReader
    with tarfile.open(ROOT / 'Aevum-linux-x86_64.tar.gz') as archive:
        names = archive.getnames()
        assert 'Aevum/Aevum' in names
        assert all(not n.startswith('/') and '..' not in Path(n).parts for n in names)
        assert not any(n.endswith(('cookies.txt', 'cookies.sqlite')) for n in names)
        for binary in ('yt-dlp', 'ffmpeg', 'ffprobe'):
            assert any(n.endswith('/' + binary) for n in names)
    archive = CArchiveReader(str(ROOT / 'dist/Aevum/Aevum'))
    assert 'aevum_cookies' in archive.open_embedded_archive('PYZ.pyz').toc


@pytest.mark.parametrize('kind', ['tarball', 'appimage'])
def test_linux_frozen_auth_queue_and_shutdown(tmp_path, media, kind):
    # Do not send test downloads to a previously running instance.
    with socket.socket() as check:
        try:
            check.bind(('127.0.0.1', 5000))
        except OSError:
            pytest.skip('Port 5000 is occupied; never reuse an existing app')
    if (Path.home() / '.zen').exists():
        pytest.skip('A personal Zen profile exists; never collect personal test data')
    config = tmp_path / 'config'
    profile = config / 'zen/Profiles/synthetic.nondefault'
    profile.mkdir(parents=True)
    connection = sqlite3.connect(profile / 'cookies.sqlite')
    try:
        connection.execute('CREATE TABLE moz_cookies (host TEXT, name TEXT, value TEXT, path TEXT, expiry INTEGER, isSecure INTEGER)')
        connection.execute('PRAGMA user_version=17')
        connection.execute('INSERT INTO moz_cookies VALUES (?, ?, ?, ?, ?, ?)',
                           ('127.0.0.1', 'auth', 'synthetic-only', '/', 4102444800000, 0))
        connection.commit()
    finally:
        connection.close()
    env = dict(os.environ, XDG_CONFIG_HOME=str(config), TMPDIR=str(tmp_path),
               BROWSER='/bin/true', APPIMAGE_EXTRACT_AND_RUN='1')
    binary = ROOT / ('dist/Aevum/Aevum' if kind == 'tarball' else 'Aevum-x86_64.AppImage')
    process = subprocess.Popen([str(binary), '--tray'], env=env, start_new_session=True,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = 'http://127.0.0.1:5000'
    def api(path, payload=None, raw=None):
        body = raw if raw is not None else json.dumps(payload).encode() if payload is not None else None
        headers = {'X-Aevum': '1', 'Content-Type': 'application/octet-stream' if raw is not None else 'application/json'}
        with urllib.request.urlopen(urllib.request.Request(base + path, data=body, headers=headers), timeout=35) as response:
            return json.loads(response.read())
    try:
        for _ in range(200):
            assert process.poll() is None, 'Package exited before serving'
            try:
                if api('/ping')['app'] == 'aevum':
                    break
            except Exception:
                time.sleep(.1)
        else:
            pytest.fail('Package server did not start')
        assert api('/settings')['version'] == '1.2.7'
        _, url = media
        def download(index, token=''):
            return api('/download', {'url': url + ('/private' if token else '') + '/audio.m4a',
                       'mode': 'audio', 'fmt': 'mp3', 'br': '320k',
                       'cookies': 'zen' if token else 'none', 'cookieToken': token,
                       'dir': str(tmp_path / ('download-' + str(index)))})['job_id']
        first = download(0)
        collected = api('/cookies/auto', {'browser': 'zen'})
        assert collected['automatic'] is True
        ids = [first, download(1, collected['token']), download(2, collected['token'])]
        for job_id in ids:
            for _ in range(300):
                status = api('/status/' + job_id)
                assert 'synthetic-only' not in json.dumps(status)
                if status['done']:
                    break
                time.sleep(.1)
            assert status['done'] and status['success'], status
        assert len(list(tmp_path.rglob('*.mp3'))) == 3
        assert 'synthetic-only' not in json.dumps(api('/history'))
        assert not list(tmp_path.glob('aevum-cookies-*'))
        # The app handles SIGTERM and cleans its session; the AppImage launcher
        # shares the owned process group with the actual packaged backend.
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=20)
        assert not list(tmp_path.glob('aevum-cookies-*'))
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
