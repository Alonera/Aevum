"""Automatic cookie collection: synthetic data only, no personal browser access."""
import json
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest
import ytdl_tray as app
from test_feedback import env, post, enqueue, VALID, BASE


def test_auto_import_reuses_session_and_keeps_api_private(env, monkeypatch):
    client, store = env
    paths = []
    def export(browser, path):
        assert browser == 'zen'
        paths.append(Path(path))
        Path(path).write_bytes(VALID)
    monkeypatch.setattr(app, '_run_cookie_export', export)
    response = post(client, '/cookies/auto', json={'browser': 'zen'})
    assert response.status_code == 200
    assert response.json['automatic'] is True
    assert set(response.json) == {'token', 'browser', 'automatic'}
    token = response.json['token']
    assert store.snapshot('zen', token)
    assert all(not p.parent.exists() for p in paths)
    assert b'synthetic-only' not in response.data
    for _ in range(2):
        assert enqueue(client, cookies='zen', cookieToken=token).status_code == 200
    assert store.snapshot('zen', token)
    post(client, '/cookies/remove', json={'token': token})
    assert token not in store._imports


@pytest.mark.parametrize('raw,code', [
    (b'# Netscape HTTP Cookie File\n', 'cookieAutoEmpty'),
    (VALID.replace(b'\t0\t', b'\t1\t'), 'cookieAutoEmpty'),
    (b'SQLite format 3\0', 'cookieDatabase'),
    (b'not a cookie export', 'cookieInvalid'),
    (b'x' * (2097152 + 1), 'cookieTooLarge'),
], ids=['empty', 'expired', 'sqlite', 'invalid', 'too-large'])
def test_auto_invalid_export_falls_back_without_leaks(env, monkeypatch, raw, code):
    client, store = env
    paths = []
    def export(browser, path):
        paths.append(Path(path))
        Path(path).write_bytes(raw)
    monkeypatch.setattr(app, '_run_cookie_export', export)
    response = post(client, '/cookies/auto', json={'browser': 'firefox'})
    assert response.json == {'error': code, 'fallback': True}
    assert not store._imports
    assert all(not p.parent.exists() for p in paths)
    assert not app._cookie_dialog_lock.locked()


def test_auto_header_browser_and_dialog_guards(env, monkeypatch):
    client, store = env
    export = Mock()
    monkeypatch.setattr(app, '_run_cookie_export', export)
    assert client.post('/cookies/auto', json={'browser': 'firefox'}, base_url=BASE).status_code == 403
    for browser in ('none', 'bogus', [], None):
        assert post(client, '/cookies/auto', json={'browser': browser}).status_code == 400
    with app._cookie_dialog_lock:
        assert post(client, '/cookies/auto', json={'browser': 'firefox'}).json['error'] == 'cookieBusy'
    export.assert_not_called()


@pytest.mark.parametrize('stderr,returncode,code', [
    ('ERROR: Could not find firefox cookies database', 1, 'cookieProfileMissing'),
    ('ERROR: Failed to decrypt with DPAPI', 1, 'cookieDecrypt'),
    ('WARNING: failed to decrypt cookies with DPAPI', 0, 'cookieDecrypt'),
    ('ERROR: Could not copy Chrome cookie database', 1, 'cookieLocked'),
    ('private diagnostic synthetic-only', 1, 'cookieAutoFailed'),
])
def test_auto_subprocess_failure_is_sanitized(env, monkeypatch, stderr, returncode, code):
    client, store = env
    proc = Mock(returncode=returncode)
    proc.poll.return_value = returncode
    proc.communicate.return_value = (None, stderr)
    monkeypatch.setattr(app.subprocess, 'Popen', Mock(return_value=proc))
    response = post(client, '/cookies/auto', json={'browser': 'firefox'})
    assert response.json == {'error': code, 'fallback': True}
    assert b'synthetic-only' not in response.data
    assert app._cookie_export_proc is None


def test_export_is_local_and_excludes_updater(env, monkeypatch, tmp_path):
    proc = Mock(returncode=0)
    proc.poll.return_value = 0
    def communicate(**kwargs):
        proc.poll.return_value = None
        info = json.loads(kwargs['input'])
        assert info['entries'] == [] and info['_type'] == 'playlist'
        assert 'url' not in info
        assert not app._claim_maintenance()
        proc.poll.return_value = 0
        return None, ''
    proc.communicate.side_effect = communicate
    popen = Mock(return_value=proc)
    monkeypatch.setattr(app.subprocess, 'Popen', popen)
    app._run_cookie_export('firefox', str(tmp_path / 'cookies.txt'))
    args = popen.call_args.args[0]
    assert '--no-clean-info-json' in args and '--ignore-config' in args
    assert args[-2:] == ['--load-info-json', '-']
    assert '--no-plugin-dirs' in args and '--skip-download' in args
    assert popen.call_args.kwargs['stdout'] == subprocess.DEVNULL
    assert app._cookie_export_proc is None
    assert app._claim_maintenance()
    popen.reset_mock()
    with pytest.raises(app.CookieError, match='cookieMaintenance'):
        app._run_cookie_export('firefox', str(tmp_path / 'cookies.txt'))
    popen.assert_not_called()


def test_export_timeout_kills_process_and_releases_gate(env, monkeypatch, tmp_path):
    proc = Mock(pid=12345, returncode=-1)
    proc.poll.return_value = -1
    proc.communicate.side_effect = [subprocess.TimeoutExpired('synthetic', 1), (None, '')]
    monkeypatch.setattr(app.subprocess, 'Popen', Mock(return_value=proc))
    kill = Mock()
    monkeypatch.setattr(app, 'kill_process_tree', kill)
    with pytest.raises(app.CookieError, match='cookieAutoTimeout'):
        app._run_cookie_export('firefox', str(tmp_path / 'cookies.txt'), timeout=1)
    kill.assert_called_once_with(12345)
    assert app._cookie_export_proc is None


def test_failed_os_termination_keeps_maintenance_blocked(env, monkeypatch, tmp_path):
    proc = Mock(pid=12345, returncode=None)
    proc.poll.return_value = None
    proc.communicate.side_effect = subprocess.TimeoutExpired('synthetic', 1)
    monkeypatch.setattr(app.subprocess, 'Popen', Mock(return_value=proc))
    monkeypatch.setattr(app, 'kill_process_tree', Mock())
    with pytest.raises(app.CookieError, match='cookieAutoTimeout'):
        app._run_cookie_export('firefox', str(tmp_path / 'cookies.txt'), timeout=1)
    assert app._cookie_export_proc is proc
    assert not app._claim_maintenance()
    proc.poll.return_value = -1
    assert app._claim_maintenance()
    assert app._cookie_export_proc is None


def test_shutdown_kills_export_before_cookie_cleanup(env, monkeypatch):
    client, store = env
    proc = Mock(pid=12345)
    proc.poll.return_value = None
    monkeypatch.setattr(app, '_cookie_export_proc', proc)
    events = []
    monkeypatch.setattr(app, 'kill_process_tree', lambda pid: events.append(('kill', pid)))
    cleanup = store.cleanup
    def clean():
        events.append(('cleanup',))
        cleanup()
    monkeypatch.setattr(store, 'cleanup', clean)
    monkeypatch.setattr(app.os, '_exit', lambda code: events.append(('exit', code)))
    app._shutdown_now()
    assert events[:3] == [('kill', 12345), ('cleanup',), ('exit', 0)]


def test_manual_fallback_starts_at_downloads_not_raw_database(env, monkeypatch, tmp_path):
    client, store = env
    downloads = tmp_path / 'downloads'
    downloads.mkdir()
    monkeypatch.setattr(app, '_IS_WINDOWS', True)
    picker = Mock(return_value=None)
    monkeypatch.setattr(app, 'select_cookie_file', picker)
    result = post(client, '/cookies/select', json={'browser': 'zen', 'exported': True, 'lang': 'tr'})
    picker.assert_called_once_with(str(downloads), 'tr')
    assert result.json == {'cancelled': True, 'warning': ''}
