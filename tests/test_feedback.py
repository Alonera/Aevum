import io
import json
import threading
from pathlib import Path
from unittest.mock import Mock

import pytest
import aevum_cookies as cookies
import ytdl_tray as app

VALID = b"# Netscape HTTP Cookie File\n.example.test\tTRUE\t/\tTRUE\t0\tauth\tsynthetic-only\n"
HEADERS = {"X-Aevum": "1"}
BASE = "http://localhost:5000"


@pytest.fixture
def env(monkeypatch, tmp_path):
    store = cookies.SessionCookies()
    monkeypatch.setattr(app, "_session_cookies", store)
    monkeypatch.setattr(app, "jobs", {})
    monkeypatch.setattr(app, "job_queue", [])
    monkeypatch.setattr(app, "download_history", [])
    monkeypatch.setattr(app, "_probe_cache", {})
    monkeypatch.setattr(app, "_probe_proc", None)
    monkeypatch.setattr(app, "_cookie_export_proc", None)
    monkeypatch.setattr(app, "_maintenance_busy", threading.Event())
    monkeypatch.setattr(app, "_shutting_down", threading.Event())
    monkeypatch.setattr(app, "_cookie_dialog_lock", threading.Lock())
    monkeypatch.setattr(app, "_kill_current_probe", Mock())
    monkeypatch.setattr(app, "PORT", 5000)
    monkeypatch.setattr(app, "_default_download_dir", lambda: str(tmp_path / "downloads"))
    yield app.app.test_client(), store
    store.cleanup()


def post(client, route, **kwargs):
    return client.post(route, base_url=BASE, headers=HEADERS, **kwargs)


def enqueue(client, **options):
    return post(client, "/download", json={"url": "https://example.test/video", **options})


@pytest.mark.parametrize("raw,code", [
    (b"SQLite format 3\0garbage", "cookieDatabase"),
    (b'{"cookies":[]}', "cookieInvalid"),
    (b"# Netscape HTTP Cookie File\n", "cookieEmpty"),
    (VALID.replace(b"\t0\t", b"\t1\t"), "cookieExpired"),
    (VALID.replace(b"\tTRUE\t/", b"\tFALSE\t/"), "cookieInvalid"),
    (VALID.replace(b"\t0\t", b"\tNaN\t"), "cookieInvalid"),
    (b"x" * (cookies.MAX_COOKIE_BYTES + 1), "cookieTooLarge"),
], ids=['sqlite', 'json', 'empty', 'expired', 'domain-flag', 'expiry', 'too-large'])
def test_invalid_cookie_files(raw, code):
    with pytest.raises(cookies.CookieError, match=code):
        cookies.validate_cookies(raw)


def test_valid_bom_http_only_and_session_expiry():
    raw = b"\xef\xbb\xbf" + VALID.replace(b".example", b"#HttpOnly_.example").replace(b"\n", b"\r\n")
    canonical = cookies.validate_cookies(raw)
    assert b"\t0\t" not in canonical
    assert b"#HttpOnly_" in canonical
    assert canonical.startswith(b"# Netscape")


def test_session_snapshot_and_independent_temporary_jars():
    store = cookies.SessionCookies()
    token = store.load("firefox", VALID)
    snapshot = store.snapshot("firefox", token)
    with pytest.raises(cookies.CookieError):
        store.snapshot("chrome", token)
    store.remove(token)
    with pytest.raises(cookies.CookieError):
        store.snapshot("firefox", token)
    with store.file(snapshot) as first, store.file(snapshot) as second:
        assert first != second
        Path(first).write_bytes(b"synthetic subprocess mutation")
        assert b"synthetic-only" in Path(second).read_bytes()
        directory = Path(second).parent
    assert not directory.exists()
    store.cleanup()
    with pytest.raises(cookies.CookieError):
        store.load("firefox", VALID)


def test_cleanup_removes_live_temporary_file():
    store = cookies.SessionCookies()
    snapshot = store.snapshot("firefox", store.load("firefox", VALID))
    with store.file(snapshot) as path:
        store.cleanup()
        assert not Path(path).exists()
    assert not store._imports


def test_browser_directory_nondefault_and_multiple(tmp_path):
    local = tmp_path / "Local"
    root = local / "Google" / "Chrome" / "User Data"
    profile = root / "Profile 2" / "Network"
    profile.mkdir(parents=True)
    env = {"LOCALAPPDATA": str(local)}
    assert cookies.browser_directory("chrome", env) == str(profile)
    (root / "Default").mkdir()
    assert cookies.browser_directory("chrome", env) == str(root)
    assert cookies.browser_directory("edge", env) is None
    assert cookies.browser_directory("firefox", {}) is None
    assert cookies.browser_directory("bogus", env) is None


@pytest.mark.parametrize('platform,relative', [
    ('win32', 'roaming/zen/Profiles'),
    ('win32', 'roaming/ZenBrowser/Profiles'),
    ('darwin', 'Library/Application Support/zen/Profiles'),
    ('linux', '.zen'),
    ('linux', 'custom-config/zen'),
    ('linux', '.var/app/app.zen_browser.zen/zen'),
    ('linux', '.var/app/app.zen_browser.zen/.zen'),
])
def test_zen_platform_profile_roots(tmp_path, platform, relative):
    profile_root = tmp_path / relative
    profile_root.mkdir(parents=True)
    env = {'HOME': str(tmp_path), 'USERPROFILE': str(tmp_path),
           'APPDATA': str(tmp_path / 'roaming'), 'XDG_CONFIG_HOME': str(tmp_path / 'custom-config')}
    assert cookies.zen_profile_root(env, platform) == profile_root


def test_zen_cookie_mapping_missing_profile_and_manual_override(monkeypatch, tmp_path):
    monkeypatch.setenv('APPDATA', str(tmp_path))
    monkeypatch.setattr(cookies.sys, 'platform', 'win32')
    root = tmp_path / 'zen/Profiles'
    assert app._cookie_args('zen') == ['--cookies-from-browser', 'firefox:' + str(root)]
    assert cookies.browser_directory('zen') is None
    (root / 'id.Profile 2').mkdir(parents=True)
    assert cookies.browser_directory('zen') == str(root / 'id.Profile 2')
    (root / 'another.release').mkdir()
    assert cookies.browser_directory('zen') == str(root)
    assert app._cookie_args('zen', 'manual.txt') == ['--cookies', 'manual.txt']
    assert app._cookie_args('firefox') == ['--cookies-from-browser', 'firefox']


def test_zen_manual_cookie_is_not_shared_with_other_browsers(env):
    client, store = env
    reply = post(client, '/cookies/upload?browser=zen', data=VALID)
    assert reply.status_code == 200
    token = reply.json['token']
    assert store.snapshot('zen', token)
    with pytest.raises(cookies.CookieError, match='cookieStale'):
        store.snapshot('firefox', token)
    assert enqueue(client, cookies='zen', cookieToken=token).status_code == 200
    assert post(client, '/cookies/remove', json={'token': token}).status_code == 200
    with pytest.raises(cookies.CookieError, match='cookieStale'):
        store.snapshot('zen', token)


@pytest.mark.parametrize("browser", ["none", "chrome", "edge", "firefox", "brave"])
def test_existing_browser_arguments(browser):
    command = app.build_cmd({"url": "https://example.test/v", "cookies": browser}, "out")
    assert "--ignore-config" in command and "--no-cache-dir" in command
    assert "--cookies" not in command
    assert ("--cookies-from-browser" in command) == (browser != "none")
    if browser != "none":
        assert command[command.index("--cookies-from-browser") + 1] == browser


def test_manual_overrides_browser_without_exposing_file_in_payload(env, tmp_path):
    client, store = env
    loaded = post(client, "/cookies/upload?browser=firefox", data=VALID)
    token = loaded.json["token"]
    assert "synthetic-only" not in loaded.get_data(as_text=True)
    response = enqueue(client, cookies="firefox", cookieToken=token)
    assert response.status_code == 200
    data = app.jobs[response.json["job_id"]]["data"]
    with store.file(data["_cookie"]) as path:
        command = app.build_cmd(data, str(tmp_path), path)
        assert command[command.index("--cookies") + 1] == path
        assert "--cookies-from-browser" not in command
    for route in ("/jobs", "/history", "/settings"):
        if route == "/settings":
            continue
        assert "synthetic-only" not in client.get(route, base_url=BASE).get_data(as_text=True)
    assert enqueue(client, cookies="chrome", cookieToken=token).status_code == 400


def test_native_select_cancel_invalid_and_missing_profile(env, monkeypatch, tmp_path):
    client, store = env
    monkeypatch.setattr(app, "_IS_WINDOWS", True)
    monkeypatch.setattr(app, "browser_directory", lambda _: None)
    picker = Mock(return_value=None)
    monkeypatch.setattr(app, "select_cookie_file", picker)
    reply = post(client, "/cookies/select", json={"browser": "firefox"})
    assert reply.json["cancelled"] and reply.json["warning"] == "cookieFolderMissing"
    picker.assert_called_once_with(None, 'en')
    source = tmp_path / "cookie export.txt"
    source.write_bytes(VALID)
    picker.return_value = str(source)
    reply = post(client, "/cookies/select", json={"browser": "firefox"})
    assert reply.status_code == 200 and reply.json["token"]
    assert source.read_bytes() == VALID
    source.write_bytes(b"SQLite format 3\0")
    reply = post(client, "/cookies/select", json={"browser": "firefox"})
    assert reply.json["error"] == "cookieDatabase"
    assert not app._cookie_dialog_lock.locked()


def test_nonwindows_picker_fallback_and_double_dialog(env, monkeypatch):
    client, store = env
    monkeypatch.setattr(app, "_IS_WINDOWS", False)
    assert post(client, "/cookies/select", json={"browser": "chrome"}).json["fallback"]
    monkeypatch.setattr(app, "_IS_WINDOWS", True)
    app._cookie_dialog_lock.acquire()
    try:
        assert post(client, "/cookies/select", json={"browser": "chrome"}).status_code == 409
    finally:
        app._cookie_dialog_lock.release()


def test_cookie_routes_require_header_and_valid_selection(env):
    client, store = env
    assert client.post("/cookies/upload?browser=firefox", data=VALID, base_url=BASE).status_code == 403
    assert post(client, "/cookies/upload?browser=none", data=VALID).status_code == 400
    assert client.post("/cookies/remove", json={"token": "x"}, base_url=BASE).status_code == 403
    assert post(client, "/cookies/select", json={"browser": "unknown"}).status_code == 400
    assert client.post("/cookies/upload?browser=firefox", data=VALID, base_url="http://evil.test:5000", headers=HEADERS).status_code == 403


def test_browser_switch_clear_keeps_already_queued_jobs(env):
    client, store = env
    token = store.load("firefox", VALID)
    first = enqueue(client, cookies="firefox", cookieToken=token).json["job_id"]
    second = enqueue(client, cookies="firefox", cookieToken=token).json["job_id"]
    assert post(client, "/cookies/remove", json={"token": token}).status_code == 200
    assert app.jobs[first]["data"]["_cookie"] == app.jobs[second]["data"]["_cookie"]
    assert enqueue(client, cookies="firefox", cookieToken=token).status_code == 400
    assert enqueue(client, cookies="firefox").status_code == 200
    assert app.job_queue[:2] == [first, second]


def test_probe_uses_auth_and_separate_cache(env, monkeypatch):
    client, store = env
    token = store.load("firefox", VALID)
    observed = []
    def probe(args, timeout=25):
        observed.append(list(args))
        if "--cookies" in args:
            assert b"synthetic-only" in Path(args[args.index("--cookies") + 1]).read_bytes()
        return {"title": "Fixture", "formats": [], "duration": 1}
    monkeypatch.setattr(app, "_run_probe_json", probe)
    url = "https://example.test/video"
    assert post(client, "/probe", json={"url": url}).status_code == 200
    assert post(client, "/probe", json={"url": url, "cookies": "firefox"}).status_code == 200
    assert post(client, "/probe", json={"url": url, "cookies": "firefox", "cookieToken": token}).status_code == 200
    assert len(observed) == 3
    assert "--cookies-from-browser" in observed[1] and "--cookies" in observed[2]
    assert not Path(observed[2][observed[2].index("--cookies") + 1]).exists()
    post(client, "/probe", json={"url": url, "cookies": "firefox", "cookieToken": token})
    assert len(observed) == 3


def test_maintenance_drains_probe_and_blocks_new_launch(env, monkeypatch):
    client, store = env
    assert app._claim_maintenance()
    app._kill_current_probe.assert_called_once()
    spawn = Mock()
    monkeypatch.setattr(app.subprocess, "Popen", spawn)
    assert app._run_probe_json(["unused"]) is None
    spawn.assert_not_called()
    assert not app._claim_maintenance()
    reply = enqueue(client)
    assert reply.status_code == 200
    assert app.jobs[reply.json["job_id"]]["code"] == "queued"


def test_package_revert_rechecks_jobs_after_gate(env, monkeypatch):
    client, store = env
    original = app._claim_maintenance
    def race():
        claimed = original()
        enqueue(client)
        return claimed
    monkeypatch.setattr(app, "_claim_maintenance", race)
    remove = Mock()
    monkeypatch.setattr(app.os, "remove", remove)
    assert post(client, "/packages/revert").status_code == 409
    remove.assert_not_called()
    assert not app._maintenance_busy.is_set()


def test_cancel_queued_releases_snapshot_but_claimed_job_stays_active(env):
    client, store = env
    token = store.load("firefox", VALID)
    jid = enqueue(client, cookies="firefox", cookieToken=token).json["job_id"]
    assert post(client, "/cancel/" + jid).status_code == 200
    assert app.jobs[jid]["done"] and "_cookie" not in app.jobs[jid]["data"]
    jid = enqueue(client).json["job_id"]
    app.jobs[jid]["claimed"] = True
    post(client, "/cancel/" + jid)
    assert not app.jobs[jid]["done"] and app.jobs[jid]["cancelled"]


def test_run_job_reuses_session_and_cleans_each_process_file(env, monkeypatch):
    client, store = env
    token = store.load("firefox", VALID)
    paths = []
    class Process:
        pid = 101
        returncode = 0
        stdout = io.StringIO("[download] 100% of 1.00MiB at 1.00MiB/s ETA 00:00\n")
        def wait(self): return 0
    def spawn(args, **kwargs):
        path = args[args.index("--cookies") + 1]
        assert Path(path).exists()
        paths.append(path)
        return Process()
    monkeypatch.setattr(app.subprocess, "Popen", spawn)
    for _ in range(2):
        jid = enqueue(client, mode="audio", cookies="firefox", cookieToken=token).json["job_id"]
        job = app.jobs[jid]
        app.run_job(jid, job["data"], job["output_dir"])
        assert job["success"]
        assert "_cookie" not in job["data"]
        assert "synthetic-only" not in json.dumps(client.get("/status/" + jid, base_url=BASE).json)
    assert len(set(paths)) == 2
    assert not any(Path(p).exists() for p in paths)
    assert store.snapshot("firefox", token)


@pytest.mark.parametrize("message,expected", [
    ("ERROR: Sign in to confirm your age", "cookieAge"),
    ("ERROR: Failed to decrypt with DPAPI", "cookieDecrypt"),
    ("ERROR: Could not copy Chrome cookie database", "cookieLocked"),
    ("ERROR: Login required", "cookieLogin"),
    ("ERROR: Requested format is not available", ""),
])
def test_auth_root_causes_are_distinct(message, expected):
    assert app._auth_failure([message]) == expected


def test_audio_formats_cover_and_original():
    base = {"url": "https://example.test/v", "mode": "audio", "thumb": True}
    for fmt in ("mp3", "m4a", "opus", "flac"):
        cmd = app.build_cmd({**base, "fmt": fmt}, "out")
        assert "--embed-thumbnail" in cmd
        assert cmd[cmd.index("-f") + 1] == "bestaudio/best"
    wav = app.build_cmd({**base, "fmt": "wav"}, "out")
    assert "--embed-thumbnail" not in wav and "--write-thumbnail" in wav
    assert "--audio-quality" not in wav
    original = app.build_cmd({**base, "fmt": "original", "clipStart": "5", "clipEnd": "10"}, "out")
    assert original[original.index("-f") + 1] == "bestaudio"
    assert not set(("-x", "--audio-quality", "--embed-thumbnail", "--force-keyframes-at-cuts")) & set(original)
    assert "--download-sections" in original


def test_audio_variants_and_clips_have_distinct_names():
    def name(**options):
        args = app.build_cmd({"url": "https://example.test/v", "mode": "audio", **options}, "out")
        return args[args.index("-o") + 1]
    assert name(br="128k") != name(br="320k")
    assert name(clipStart="0", clipEnd="5") != name(clipStart="5", clipEnd="10")
    assert name(fmt="original") != name(fmt="m4a")


def test_shutdown_cleans_before_exit(env, monkeypatch):
    client, store = env
    token = store.load("firefox", VALID)
    snapshot = store.snapshot("firefox", token)
    exited = []
    monkeypatch.setattr(app.os, "_exit", lambda code: exited.append(code))
    with store.file(snapshot) as path:
        app._shutdown_now()
        assert not Path(path).exists()
        assert app._shutting_down.is_set() and exited == [0]
    assert not store._imports


def test_single_instance_path_does_not_start_workers(env, monkeypatch):
    opened = []
    monkeypatch.setattr(app, "_find_running_instance", lambda: "http://localhost:5003")
    monkeypatch.setattr(app, "_open_url", opened.append)
    monkeypatch.setattr(app.signal, "signal", Mock())
    start = Mock()
    monkeypatch.setattr(app.threading, "Thread", start)
    app.main()
    assert opened == ["http://localhost:5003"]
    start.assert_not_called()

def test_queue_fifo_skips_canceled_and_waits_for_maintenance(env, monkeypatch):
    client, store = env
    canceled = enqueue(client).json["job_id"]
    first = enqueue(client).json["job_id"]
    second = enqueue(client).json["job_id"]
    post(client, "/cancel/" + canceled)
    app._maintenance_busy.set()
    pauses = []
    monkeypatch.setattr(app.time, "sleep", lambda seconds: (pauses.append(seconds), app._maintenance_busy.clear()))
    order = []
    class Finished(Exception): pass
    def work(jid, data, output):
        assert not app._maintenance_busy.is_set()
        assert app.jobs[jid]["claimed"]
        order.append(jid)
        if len(order) == 2:
            raise Finished()
    monkeypatch.setattr(app, "run_job", work)
    with pytest.raises(Finished):
        app._queue_worker()
    assert order == [first, second] and pauses


def test_cancel_between_spawn_and_publication_cleans_cookie_file(env, monkeypatch):
    client, store = env
    token = store.load("firefox", VALID)
    jid = enqueue(client, mode="audio", cookies="firefox", cookieToken=token).json["job_id"]
    job = app.jobs[jid]
    job["claimed"] = True
    paths = []
    class Process:
        pid = 123
        returncode = 0
        stdout = io.StringIO("")
        def wait(self): return 0
    def spawn(args, **kwargs):
        paths.append(args[args.index("--cookies") + 1])
        post(client, "/cancel/" + jid)
        return Process()
    monkeypatch.setattr(app.subprocess, "Popen", spawn)
    kill = Mock()
    monkeypatch.setattr(app, "kill_process_tree", kill)
    app.run_job(jid, job["data"], job["output_dir"])
    kill.assert_called_once_with(123)
    assert job["code"] == "stopped" and not job["success"]
    assert not Path(paths[0]).exists()
    assert store.snapshot("firefox", token)


def test_auth_failure_is_not_retried_and_headers_are_redacted(env, monkeypatch):
    client, store = env
    jid = enqueue(client, mode="audio").json["job_id"]
    class Process:
        pid = 124
        returncode = 1
        stdout = io.StringIO("[debug] Cookie: auth=synthetic-only\nERROR: HTTP Error 403: Sign in to confirm your age\n")
        def wait(self): return 1
    spawn = Mock(return_value=Process())
    monkeypatch.setattr(app.subprocess, "Popen", spawn)
    job = app.jobs[jid]
    app.run_job(jid, job["data"], job["output_dir"])
    assert spawn.call_count == 1
    assert job["autherr"] == "cookieAge" and not job.get("staleerr")
    assert "synthetic-only" not in json.dumps(client.get("/status/" + jid, base_url=BASE).json)


@pytest.mark.parametrize("route", ["/packages/apply", "/update/apply", "/packages/revert"])
def test_updaters_refuse_pending_downloads_before_network(env, monkeypatch, route):
    client, store = env
    monkeypatch.setattr(app, "install_kind", lambda: "portable")
    network = Mock(side_effect=AssertionError("unexpected network access"))
    monkeypatch.setattr(app, "_latest_release", network)
    monkeypatch.setattr(app, "_pick_channel", network)
    enqueue(client)
    response = post(client, route)
    assert response.status_code == 409
    assert not app._maintenance_busy.is_set()
    network.assert_not_called()


def test_source_opus_constraints_and_invalid_api_input(env):
    client, store = env
    assert enqueue(client, mode="audio", fmt="opus", br="320k").status_code == 400
    assert enqueue(client, mode="audio", fmt="opus", br="256k").status_code == 200
    automatic = app.build_cmd({"url": "https://example.test/v", "mode": "audio", "fmt": "opus", "br": "best"}, "out")
    assert "--audio-quality" not in automatic
    assert post(client, "/cookies/select", json={"browser": []}).status_code == 400
    assert enqueue(client, dir=[]).status_code == 400


def test_cookie_parser_never_logs_rejected_cookie_contents(capsys):
    broken = VALID.replace(b"\tTRUE\t/", b"\tFALSE\t/")
    with pytest.raises(cookies.CookieError) as error:
        cookies.validate_cookies(broken)
    captured = capsys.readouterr()
    assert "synthetic-only" not in str(error.value) + captured.out + captured.err


def test_process_exit_removes_session_file():
    import subprocess
    import sys
    code = (
        "import ytdl_tray as a;"
        "s=a._session_cookies;"
        "t=s.load('firefox',b'# Netscape HTTP Cookie File\\n.example.test\\tTRUE\\t/\\tTRUE\\t0\\tauth\\tsynthetic-only\\n');"
        "c=s.file(s.snapshot('firefox',t));"
        "print(c.__enter__(),flush=True)"
    )
    result = subprocess.run([sys.executable, "-B", "-c", code], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    path = Path(result.stdout.strip().splitlines()[-1])
    assert not path.exists() and not path.parent.exists()


def test_video_format_selection_matches_original_repository():
    import ast
    import subprocess
    source = subprocess.check_output(["git", "show", "353a843:ytdl_tray.py"], text=True, encoding="utf-8")
    tree = ast.parse(source)
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef)
             and n.name in ("build_video_format", "build_format_sort")]
    original = {"HEIGHT_MAP": app.HEIGHT_MAP}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "<original-format-functions>", "exec"), original)
    for quality in ("best", "4k", "1080p", "720p"):
        for container in ("mp4", "mp4h264", "mkv", "webm"):
            for mute in (False, True):
                assert app.build_video_format(quality, container, mute) == original["build_video_format"](quality, container, mute)
            assert app.build_format_sort(quality, container) == original["build_format_sort"](quality, container)
