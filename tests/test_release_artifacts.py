"""Opt-in package checks: AEVUM_RELEASE_DIR points at a fresh local build."""
import hashlib
import json
import os
import subprocess
import time
import uuid
import urllib.request
import zipfile
import io
from pathlib import Path
import pytest
from test_media import media

RELEASE = os.environ.get('AEVUM_RELEASE_DIR')
pytestmark = pytest.mark.skipif(not RELEASE, reason='set AEVUM_RELEASE_DIR to test built artifacts')
ROOT = Path(__file__).resolve().parents[1]


def sha(data):
    return hashlib.sha256(data).hexdigest()


def test_release_hashes_and_source_archive():
    release = Path(RELEASE)
    manifest = json.loads((release / 'build-manifest.json').read_text(encoding='utf-8-sig'))
    assert manifest['version'] == '1.2.7'
    for entry in manifest['artifacts']:
        path = release / entry['file']
        assert path.stat().st_size == entry['bytes']
        assert sha(path.read_bytes()) == entry['sha256']
    # Exercise the existing updater's reader against the actual published file
    # names, not merely the build manifest (which uses relative paths).
    import ytdl_tray as app
    from unittest.mock import patch
    checksums = (release / 'checksums.txt').read_bytes()
    with patch.object(app, '_fetch', side_effect=lambda url: io.BytesIO(checksums)):
        for name in ('Aevum.exe', 'Aevum-Setup.exe'):
            entry = next(item for item in manifest['artifacts'] if Path(item['file']).name == name)
            assert app._published_sha({'assets': [{'name': 'checksums.txt', 'browser_download_url': 'https://example.test/checksums.txt'}]}, name) == entry['sha256']
    for entry in manifest['inputs']:
        assert sha((ROOT / entry['file']).read_bytes()) == entry['sha256']
    with zipfile.ZipFile(release / 'Aevum-1.2.7-source.zip') as source:
        assert sha(source.read('aevum_cookies.py')) == sha((ROOT / 'aevum_cookies.py').read_bytes())
        assert sha(source.read('ytdl_tray.py')) == sha((ROOT / 'ytdl_tray.py').read_bytes())
        assert not any(n.lower().endswith(('cookies.txt', '.sqlite', '.exe')) for n in source.namelist())


def test_frozen_archive_contains_new_module_fonts_and_exact_binaries():
    import marshal
    from PyInstaller.archive.readers import CArchiveReader
    archive = CArchiveReader(str(Path(RELEASE) / 'Portable/Aevum.exe'))
    main_code = marshal.loads(archive.extract('ytdl_tray'))
    assert {'cookies_auto', '_run_cookie_export'} <= set(main_code.co_names)
    html = next(value for value in main_code.co_consts if isinstance(value, str) and 'const EXTRA_TEXT=' in value)
    assert '/cookies/auto' in html and 'cookieBrowserHint' in html
    pyz = archive.open_embedded_archive('PYZ.pyz')
    assert 'aevum_cookies' in pyz.toc
    assert 'pystray._win32' in pyz.toc
    for name in ('yt-dlp.exe', 'ffmpeg.exe', 'ffprobe.exe'):
        assert sha(archive.extract(name)) == sha((ROOT / 'bin' / name).read_bytes())
    for font in (ROOT / 'fonts').glob('*.woff2'):
        key = next(k for k in archive.toc if k.replace('\\', '/') == 'fonts/' + font.name)
        assert sha(archive.extract(key)) == sha(font.read_bytes())


def ping(port):
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/ping', timeout=.3) as response:
            return json.loads(response.read()).get('app') == 'aevum'
    except Exception:
        return False


def test_frozen_silent_single_instance_launch(tmp_path):
    # Never close or reuse the user's running server for download tests.
    if not any(ping(port) for port in range(5000, 5010)):
        pytest.skip('No existing Aevum; use the isolated live smoke test instead')
    exe = Path(RELEASE) / 'Portable/Aevum.exe'
    isolated = dict(os.environ, APPDATA=str(tmp_path / 'roaming'),
                    LOCALAPPDATA=str(tmp_path / 'local'), TEMP=str(tmp_path), TMP=str(tmp_path))
    result = subprocess.run([str(exe), '--tray'], env=isolated, timeout=35,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    assert result.returncode == 0
    assert not list(tmp_path.glob('_MEI*'))
    assert not list(tmp_path.glob('aevum-cookies-*'))


def test_frozen_live_download_cookie_queue_and_shutdown(tmp_path, media):
    if any(ping(port) for port in range(5000, 5010)):
        pytest.skip('An existing Aevum is running; never interrupt the user instance')
    import ctypes
    from ctypes import wintypes
    import psutil
    import sqlite3
    _, media_url = media
    exe = (Path(RELEASE) / 'Portable/Aevum.exe').resolve()
    isolated = dict(os.environ, APPDATA=str(tmp_path / 'roaming'),
                    LOCALAPPDATA=str(tmp_path / 'local'), TEMP=str(tmp_path), TMP=str(tmp_path))
    profile = tmp_path / 'roaming/Mozilla/Firefox/Profiles/synthetic.nondefault'
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
    process = subprocess.Popen([str(exe), '--tray'], env=isolated,
                               creationflags=subprocess.CREATE_NO_WINDOW)
    owned = set()
    def remember_children():
        if process.poll() is None:
            for child in psutil.Process(process.pid).children(recursive=True):
                if Path(child.exe()).resolve() == exe:
                    owned.add(child.pid)
    def close_owned():
        remember_children()
        api = ctypes.WinDLL('user32')
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        api.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
        api.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        api.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        @callback_type
        def visit(hwnd, _):
            pid = wintypes.DWORD()
            thread = api.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value in owned:
                api.PostThreadMessageW(thread, 0x0012, 0, 0)  # exit this test's tray loop normally
            return True
        api.EnumWindows(visit, 0)
    port = None
    try:
        for _ in range(150):
            remember_children()
            for pid in owned:
                for connection in psutil.Process(pid).net_connections(kind='tcp'):
                    if connection.status == psutil.CONN_LISTEN and connection.laddr.ip == '127.0.0.1':
                        port = connection.laddr.port
            if port and ping(port):
                break
            assert process.poll() is None, 'Frozen application exited before serving'
            time.sleep(.1)
        assert port and ping(port), 'Frozen server did not start'
        base = f'http://127.0.0.1:{port}'
        def api(path, payload=None, raw=None):
            body = raw if raw is not None else json.dumps(payload).encode() if payload is not None else None
            headers = {'X-Aevum': '1', 'Content-Type': 'application/octet-stream' if raw is not None else 'application/json'}
            with urllib.request.urlopen(urllib.request.Request(base + path, data=body, headers=headers), timeout=35) as response:
                return json.loads(response.read())
        def download(index, private=False, token=''):
            return api('/download', {'url': media_url + ('/private' if private else '') + '/audio.m4a',
                       'mode': 'audio', 'fmt': 'mp3', 'br': '320k',
                       'cookies': 'firefox' if token else 'none', 'cookieToken': token,
                       'dir': str(tmp_path / ('İndirme ' + str(index)))})['job_id']
        first = download(0)
        cookie = b'# Netscape HTTP Cookie File\n127.0.0.1\tFALSE\t/\tFALSE\t0\tauth\tsynthetic-only\n'
        token = api('/cookies/upload?browser=firefox', raw=cookie)['token']
        second_job = download(1, True, token)
        collected = api('/cookies/auto', {'browser': 'firefox'})
        assert collected['automatic'] is True
        assert 'synthetic-only' not in json.dumps(collected)
        ids = [first, second_job, download(2, True, collected['token'])]
        for job_id in ids:
            for _ in range(200):
                status = api('/status/' + job_id)
                assert 'synthetic-only' not in json.dumps(status)
                if status['done']:
                    break
                time.sleep(.1)
            assert status['done'] and status['success'], status
        assert len(list(tmp_path.rglob('*.mp3'))) == 3
        assert 'synthetic-only' not in json.dumps(api('/history'))
        assert not list(tmp_path.glob('aevum-cookies-*'))
        second = subprocess.run([str(exe), '--tray'], env=isolated,
                                creationflags=subprocess.CREATE_NO_WINDOW, timeout=35)
        assert second.returncode == 0 and process.poll() is None
        close_owned()
        assert process.wait(timeout=15) == 0
        assert not list(tmp_path.glob('_MEI*'))
        assert not list(tmp_path.glob('aevum-cookies-*'))
    finally:
        if process.poll() is None:
            close_owned()
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                # Last resort: only exact children of this test's Popen.
                for child in psutil.Process(process.pid).children(recursive=True):
                    if child.pid in owned and Path(child.exe()).resolve() == exe:
                        child.kill()
                process.kill()
                process.wait(timeout=5)


@pytest.mark.skipif(os.environ.get('AEVUM_INSTALLER_TEST') != '1', reason='opt-in isolated install/uninstall')
def test_isolated_installer_roundtrip():
    import winreg
    compiler = Path(os.environ['LOCALAPPDATA']) / 'Programs/Inno Setup 6/ISCC.exe'
    if not compiler.is_file():
        pytest.skip('Inno Setup compiler not found')
    release = Path(RELEASE).resolve()
    qa = release / '_qa' / ('installer-' + uuid.uuid4().hex)
    qa.mkdir(parents=True)
    install = (qa / 'installed').resolve()
    assert install.is_relative_to(qa.resolve()) and qa.is_relative_to(release)
    production_id = '{A3E9F1C2-7B4D-4E6A-9C21-AEV000000001}'
    test_id = '{' + str(uuid.uuid4()).upper() + '}'
    assert test_id != production_id
    def uninstall_record(app_id):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                'Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\' + app_id + '_is1') as key:
                return {name: winreg.QueryValueEx(key, name)[0] for name in
                        ('DisplayName', 'DisplayVersion', 'InstallLocation', 'UninstallString')}
        except FileNotFoundError:
            return None
    before = uninstall_record(production_id)
    assert uninstall_record(test_id) is None
    # Exact production script/payload; only the install identity/name differ.
    # This prevents touching an existing Aevum installation or its registry key.
    script = (ROOT / 'installer.iss').read_text(encoding='utf-8-sig')
    script = script.replace('AppId={' + production_id, 'AppId={' + test_id)
    script = script.replace('#define AppName "Aevum"', '#define AppName "Aevum QA"')
    assert 'AppId={' + test_id in script and 'AppId={' + production_id not in script
    script = script.replace('[Setup]', '[Setup]\nSourceDir=' + str(ROOT))
    generated_script = qa / 'installer-test.iss'
    generated_script.write_text(script, encoding='utf-8-sig')
    compile_result = subprocess.run([str(compiler), '/Q', '/DAppSource=' + str(release / 'Portable/Aevum.exe'),
                                     '/O' + str(qa), str(generated_script)], cwd=ROOT,
                                    capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=90)
    assert compile_result.returncode == 0, compile_result.stdout + compile_result.stderr
    flags = ['/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/SP-']
    uninstaller = install / 'unins000.exe'
    try:
        result = subprocess.run([str(qa / 'Aevum-Setup.exe'), *flags, '/NOCLOSEAPPLICATIONS',
                                 '/NORESTARTAPPLICATIONS', '/TASKS=', '/NOICONS', '/LANG=turkish',
                                 '/DIR=' + str(install), '/LOG=' + str(qa / 'install.log')],
                                creationflags=subprocess.CREATE_NO_WINDOW, timeout=90)
        assert result.returncode == 0
        assert sha((install / 'Aevum.exe').read_bytes()) == sha((release / 'Portable/Aevum.exe').read_bytes())
        assert (install / 'LICENSE').is_file()
        assert (install / 'THIRD_PARTY_LICENSES.md').is_file()
        assert uninstall_record(test_id)['DisplayName'] == 'Aevum QA'
    finally:
        # Only this test's validated, newly created install is ever uninstalled.
        if uninstaller.is_file():
            assert uninstaller.resolve().is_relative_to(qa.resolve())
            removed = subprocess.run([str(uninstaller), *flags[:3], '/LOG=' + str(qa / 'uninstall.log')],
                                     creationflags=subprocess.CREATE_NO_WINDOW, timeout=90)
            assert removed.returncode == 0
    assert not (install / 'Aevum.exe').exists()
    assert uninstall_record(test_id) is None
    assert uninstall_record(production_id) == before
