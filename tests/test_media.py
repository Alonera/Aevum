"""Offline integration with bundled yt-dlp/FFmpeg and synthetic HTTP media."""
import functools
import http.server
import json
import os
import sqlite3
import subprocess
import threading
from pathlib import Path
import pytest
from PIL import Image
import ytdl_tray as app
from test_feedback import env, post

ROOT = Path(__file__).resolve().parents[1]
SUFFIX = '.exe' if os.name == 'nt' else ''
FFMPEG = ROOT / ('bin/ffmpeg' + SUFFIX)
FFPROBE = ROOT / ('bin/ffprobe' + SUFFIX)
YTDLP = ROOT / ('bin/yt-dlp' + SUFFIX)

def run(args):
    r = subprocess.run([str(x) for x in args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=60)
    assert r.returncode == 0, r.stderr[-2000:] + r.stdout[-1000:]
    return r

@pytest.fixture(scope="module")
def media(tmp_path_factory):
    root = tmp_path_factory.mktemp("media-fixtures")
    run([FFMPEG, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
         "sine=frequency=440:duration=2", "-c:a", "aac", "-b:a", "128k", root / "audio.m4a"])
    run([FFMPEG, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
         "testsrc2=size=160x90:rate=15:duration=2", "-an", "-c:v", "libx264",
         "-pix_fmt", "yuv420p", root / "video.mp4"])
    run([FFMPEG, "-hide_banner", "-loglevel", "error", "-i", root / "video.mp4",
         "-i", root / "audio.m4a", "-c", "copy", root / "muxed.mp4"])
    Image.new("RGB", (64, 64), (0, 180, 130)).save(root / "cover.jpg")
    class Handler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            if self.path.startswith("/private/"):
                if "auth=synthetic-only" not in self.headers.get("Cookie", ""):
                    self.send_error(403)
                    return
                self.path = self.path.removeprefix("/private")
            super().do_GET()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Handler, directory=str(root)))
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    yield root, f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()
    worker.join(timeout=3)

def fixture_info(root, url, private=False, muxed=False):
    prefix = url + ("/private" if private else "")
    formats = [
        {"format_id": "audio", "url": prefix + "/audio.m4a", "ext": "m4a",
         "vcodec": "none", "acodec": "mp4a.40.2", "abr": 128},
        {"format_id": "video", "url": prefix + "/video.mp4", "ext": "mp4",
         "vcodec": "avc1.64000a", "acodec": "none", "width": 160, "height": 90, "fps": 15}]
    if muxed:
        formats = [{"format_id": "muxed", "url": prefix + "/muxed.mp4", "ext": "mp4",
                    "vcodec": "avc1.64000a", "acodec": "mp4a.40.2", "width": 160, "height": 90}]
    data = {"id": "fixture", "title": "Synthetic fixture", "duration": 2,
            "extractor": "generic", "extractor_key": "Generic", "webpage_url": url,
            "formats": formats, "thumbnails": [{"url": url + "/cover.jpg", "id": "0", "ext": "jpg"}]}
    path = root / "fixture.info.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path

def info(path):
    return json.loads(run([FFPROBE, "-v", "error", "-show_streams", "-show_format", "-of", "json", path]).stdout)

def packets(path, stream="a"):
    data = json.loads(run([FFPROBE, "-v", "error", "-select_streams", stream,
                          "-show_packets", "-show_data_hash", "sha256",
                          "-show_entries", "packet=data_hash", "-of", "json", path]).stdout)
    return [p["data_hash"] for p in data["packets"]]

@pytest.mark.parametrize("fmt", ["mp3", "m4a", "opus", "flac", "wav", "original"])
def test_real_audio_cover_and_source_preservation(env, monkeypatch, media, tmp_path, fmt):
    root, url = media
    monkeypatch.setattr(app, "YTDLP", str(YTDLP))
    monkeypatch.setattr(app, "FFMPEG_DIR", str(FFMPEG.parent))
    bitrate = "256k" if fmt == "opus" else "320k"
    args = app.build_cmd({"url": url, "mode": "audio", "fmt": fmt, "br": bitrate, "thumb": True}, str(tmp_path))
    run(args[:-1] + ["--load-info-json", fixture_info(root, url)])
    extension = "m4a" if fmt == "original" else fmt
    output = next(tmp_path.rglob("*." + extension))
    metadata = info(output)
    audio = [s for s in metadata["streams"] if s["codec_type"] == "audio"]
    assert len(audio) == 1
    assert list(tmp_path.rglob("*.jpg"))
    if fmt in ("mp3", "m4a", "opus", "flac"):
        assert any(s.get("disposition", {}).get("attached_pic") for s in metadata["streams"])
    if fmt == "mp3":
        assert int(audio[0]["bit_rate"]) == 320000
    if fmt in ("m4a", "original"):
        assert packets(output) == packets(root / "audio.m4a")

def test_real_video_mux_preserves_audio_and_video(env, monkeypatch, media, tmp_path):
    root, url = media
    monkeypatch.setattr(app, "YTDLP", str(YTDLP))
    monkeypatch.setattr(app, "FFMPEG_DIR", str(FFMPEG.parent))
    args = app.build_cmd({"url": url, "mode": "video"}, str(tmp_path))
    run(args[:-1] + ["--load-info-json", fixture_info(root, url)])
    output = next(tmp_path.rglob("*.mp4"))
    assert packets(output) == packets(root / "audio.m4a")
    assert packets(output, "v") == packets(root / "video.mp4", "v")

def test_real_manual_cookie_repeated_downloads_and_cleanup(env, monkeypatch, media, tmp_path):
    client, store = env
    root, url = media
    monkeypatch.setattr(app, "YTDLP", str(YTDLP))
    monkeypatch.setattr(app, "FFMPEG_DIR", str(FFMPEG.parent))
    raw = b"# Netscape HTTP Cookie File\n127.0.0.1\tFALSE\t/\tFALSE\t0\tauth\tsynthetic-only\n"
    token = store.load("firefox", raw)
    snapshot = store.snapshot("firefox", token)
    fixture = fixture_info(root, url, private=True)
    for i in range(2):
        with store.file(snapshot) as path:
            args = app.build_cmd({"url": url, "mode": "audio", "fmt": "original", "cookies": "firefox"}, str(tmp_path / str(i)), path)
            run(args[:-1] + ["--load-info-json", fixture])
        assert not Path(path).exists()
    assert len(list(tmp_path.rglob("*.m4a"))) == 2
    assert store.snapshot("firefox", token)

def test_original_does_not_convert_muxed_only_source(env, monkeypatch, media, tmp_path):
    root, url = media
    monkeypatch.setattr(app, "YTDLP", str(YTDLP))
    args = app.build_cmd({"url": url, "mode": "audio", "fmt": "original"}, str(tmp_path))
    result = subprocess.run(args[:-1] + ["--load-info-json", str(fixture_info(root, url, muxed=True))],
                            capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert result.returncode != 0
    assert "Requested format is not available" in result.stderr
    assert not list(tmp_path.rglob("*.mp3"))


@pytest.mark.parametrize('schema', [15, 16, 17])
@pytest.mark.parametrize('browser_name', ['firefox', 'zen'])
def test_real_firefox_extraction_with_synthetic_profile(env, monkeypatch, media, tmp_path, schema, browser_name):
    # The real extraction code runs, but never reads the user's browser data.
    root, url = media
    roaming = tmp_path / 'roaming'
    profile = roaming / ('zen/Profiles/test.nondefault' if browser_name == 'zen' else 'Mozilla/Firefox/Profiles/test.nondefault')
    profile.mkdir(parents=True)
    db = profile / 'cookies.sqlite'
    with sqlite3.connect(db) as connection:
        connection.execute('CREATE TABLE moz_cookies (host TEXT, name TEXT, value TEXT, path TEXT, expiry INTEGER, isSecure INTEGER)')
        connection.execute('PRAGMA user_version=' + str(schema))
        expiry = 4102444800 * (1000 if schema >= 16 else 1)
        connection.execute('INSERT INTO moz_cookies VALUES (?, ?, ?, ?, ?, ?)',
                           ('127.0.0.1', 'auth', 'synthetic-only', '/', expiry, 0))
    connection.close()
    before = db.read_bytes()
    monkeypatch.setattr(app, 'YTDLP', str(YTDLP))
    monkeypatch.setattr(app, 'FFMPEG_DIR', str(FFMPEG.parent))
    monkeypatch.setenv('APPDATA', str(roaming))
    if os.name != 'nt':
        # Exercise the real Firefox reader without ever searching a user's
        # Linux home profile. Zen's root mapping has separate pure tests.
        monkeypatch.setattr(app, 'browser_cookie_source', lambda browser: 'firefox:' + str(profile))
    args = app.build_cmd({'url': url, 'mode': 'audio', 'fmt': 'original', 'cookies': browser_name}, str(tmp_path / 'download'))
    isolated = dict(os.environ, APPDATA=str(roaming), LOCALAPPDATA=str(tmp_path / 'local'))
    result = subprocess.run(args[:-1] + ['--load-info-json', str(fixture_info(root, url, private=True))],
                            env=isolated, capture_output=True, text=True, encoding='utf-8', timeout=45)
    assert result.returncode == 0, result.stderr[-2000:]
    assert 'Extracted 1 cookies from firefox' in result.stdout
    assert list((tmp_path / 'download').rglob('*.m4a'))
    assert db.read_bytes() == before
    # The button uses the real yt-dlp export path, then reuses that session jar
    # for two authenticated downloads. All profile roots remain synthetic.
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'local'))
    client, store = env
    response = post(client, '/cookies/auto', json={'browser': browser_name})
    assert response.status_code == 200, response.json
    assert response.json['automatic'] is True
    token = response.json['token']
    snapshot = store.snapshot(browser_name, token)
    for index in range(2):
        with store.file(snapshot) as path:
            args = app.build_cmd({'url': url, 'mode': 'audio', 'fmt': 'original', 'cookies': browser_name},
                                 str(tmp_path / f'auto-{index}'), path)
            assert '--cookies-from-browser' not in args
            result = subprocess.run(args[:-1] + ['--load-info-json', str(fixture_info(root, url, private=True))],
                                    env=isolated, capture_output=True, text=True, encoding='utf-8', timeout=45)
            assert result.returncode == 0, result.stderr[-2000:]
            assert list((tmp_path / f'auto-{index}').rglob('*.m4a'))
        assert not Path(path).exists()
    assert store.snapshot(browser_name, token)
    assert db.read_bytes() == before


def test_real_cookie_auto_missing_profile(env, monkeypatch, tmp_path):
    client, store = env
    monkeypatch.setattr(app, 'YTDLP', str(YTDLP))
    monkeypatch.setenv('APPDATA', str(tmp_path / 'empty-roaming'))
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'empty-local'))
    if os.name != 'nt':
        monkeypatch.setattr(app, 'browser_cookie_source', lambda browser: 'firefox:' + str(tmp_path / 'missing'))
    response = post(client, '/cookies/auto', json={'browser': 'zen'})
    assert response.json == {'error': 'cookieProfileMissing', 'fallback': True}
    assert not store._imports
    assert app._cookie_export_proc is None
