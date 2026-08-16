#!/usr/bin/env python3
"""
ytdl_tray.py — Aevum download app

Windows: double-click puts it in the system tray and opens the browser UI;
right-click the tray icon to open or quit.
Linux: no tray — launching opens the browser page, and the app shuts down
once the last tab is closed (tracked via a heartbeat from the page).

Downloads video/audio from the ~1800 sites yt-dlp supports.
"""

import os
import re
import sys
import json
import threading
import webbrowser
import time
import subprocess
import shutil
import socket
from pathlib import Path
import urllib.request
from urllib.parse import urlparse, parse_qs

try:
    import winreg  # Windows launch-at-startup setting
except ImportError:
    winreg = None

# ── Tray imports (Windows only) ──────────────────────────────────────────────
# We don't use a tray on Linux: depending on the desktop it either didn't show
# up at all or rendered as a dead flat square. Instead the app is tied to the
# browser tab and exits when the tab closes (see _browser_watchdog below).
if sys.platform == "win32":
    try:
        import pystray
        from PIL import Image, ImageDraw
    except Exception:
        pystray = None
        print("Warning: system tray unavailable — running in browser mode. "
              "(from source, run: pip install pystray pillow flask)", flush=True)
else:
    pystray = None

from flask import Flask, request, jsonify, render_template_string, send_from_directory
import uuid

# ── Flask app ─────────────────────────────────────────────────────────────────
app = Flask(__name__)
download_history = []
history_lock = threading.Lock()
jobs = {}
jobs_lock = threading.Lock()
# Downloads run one at a time. Parallel jobs would not finish any sooner —
# the link is the limit — and each yt-dlp already opens 4 concurrent
# fragments, so running three at once just multiplies the connection count
# and invites throttling. /download appends here; one worker drains it.
job_queue = []
queue_cv = threading.Condition()

PORT = 5000

# The one place the version is written in code. version.txt and installer.iss
# carry it too — build.bat refuses to build when the three disagree, because
# the updater compares this string against the newest release tag and a stale
# constant would either hide a real update or offer one that is already here.
APP_VERSION = "1.2.6"
UPDATE_REPO = "Alonera/Aevum"

# ── Page liveness tracking (on Linux the app lives with the browser tab) ─────
_last_seen = time.time()   # time of the last request from the page
_page_seen = False         # has the page connected at least once?
_clients = {}              # tab id -> last heartbeat time
_clients_lock = threading.Lock()

# Browsers throttle timers in background tabs (Chrome: down to once a minute),
# so the heartbeat alone isn't enough. On a clean close the page sends /bye
# for a fast exit; the heartbeat stays as a long-term fallback.
_EXIT_GRACE = 15     # s — wait this long after the last tab closes
_FIRST_GRACE = 120   # s — if no page connected yet (browser can be slow to start)
_CLIENT_STALE = 90   # s — a tab silent for this long counts as dead
                     #     (a throttled background tab still pings ~every 60s)


# Only the page Aevum itself opened may talk to this server. A browser will
# send a page's requests to the loopback address without complaint — either
# because a hostile site pointed its own name at 127.0.0.1 (DNS rebinding),
# or by aiming a request straight at the port. Two headers give it away and
# neither can be forged from script: Host is the name the page was loaded
# under, Origin is stamped by the browser.
#
# Without this /download is the prize, not the updater: its "dir" comes
# straight from the request, so a foreign page could pick the folder it
# writes into.
def _is_local_host(host: str) -> bool:
    # PORT is settled at startup (find_free_port), so read it per request
    return host in (f"localhost:{PORT}", f"127.0.0.1:{PORT}")


@app.before_request
def _reject_foreign_callers():
    if not _is_local_host(request.host):
        return "forbidden", 403
    origin = request.headers.get("Origin")
    # Absent on plain navigation; present on every fetch the page makes
    if origin and not _is_local_host(origin.split("//", 1)[-1]):
        return "forbidden", 403


# Registered after the guard on purpose: a rejected request is not the page,
# and must not count as a sign of life (on Linux that keeps the app alive).
@app.before_request
def _touch_last_seen():
    global _last_seen, _page_seen
    _last_seen = time.time()
    _page_seen = True


def _user_data_dir() -> str:
    """The per-user folder Aevum may write to: its settings, and yt-dlp."""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(base, "Aevum")
    cfg = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(cfg, "aevum")


def _bin_dir() -> str:
    """Folder with the bundled binaries (PyInstaller extraction dir, or next to this script)."""
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _find_binary(name: str, user_first: bool = True) -> str:
    """Prefer the binary shipped with the app, fall back to PATH.

    yt-dlp is the exception, and it gets looked for in the user's own folder
    first. It is the only bundled piece that argues with YouTube, so it is
    the only one that goes stale: YouTube changes how it hands out media
    URLs, yt-dlp follows within days, and a release of ours takes weeks. A
    copy the user can actually write to means that fix does not have to wait
    for one.

    ffmpeg deliberately does not get the same treatment. It is pinned at 8.0
    because 8.1.x hangs forever on googlevideo (yt-dlp #16546) and takes clip
    downloads with it; a newer one appearing on its own would bring that back.
    """
    sfx = ".exe" if sys.platform == "win32" else ""
    fname = name + sfx
    cands = []
    if user_first and name == "yt-dlp":
        cands.append(os.path.join(_user_data_dir(), "bin", fname))
    cands += [os.path.join(_bin_dir(), fname), os.path.join(_bin_dir(), "bin", fname)]
    for cand in cands:
        if os.path.isfile(cand):
            # Through to the real location, not the link that points at it.
            # yt-dlp is itself a PyInstaller binary, and its bootloader checks
            # that the child it re-launches came from the same executable as
            # its parent. A junction or symlink anywhere in the path makes
            # those two names differ and it refuses to start, with a line
            # about "parent process has different executable" that says
            # nothing to anyone. Measured here: the same file runs fine by its
            # real path and fails through a junction to it.
            cand = os.path.realpath(cand)
            # PyInstaller drops the +x bit of bundled binaries on Linux/macOS — restore it
            if sys.platform != "win32":
                try:
                    os.chmod(cand, 0o755)
                except OSError:
                    pass
            return cand
    return shutil.which(name) or fname


def _clean_env() -> dict:
    """Clean environment for child processes.

    On Linux, PyInstaller points LD_LIBRARY_PATH at its own bundle dir and every
    child inherits it: a browser launched via xdg-open picks up our copies of
    glib/gio and silently crashes, and yt-dlp/ffmpeg can be affected too. The
    bootloader keeps the original in LD_LIBRARY_PATH_ORIG — put it back.
    """
    env = os.environ.copy()
    if getattr(sys, "frozen", False) and sys.platform not in ("win32", "darwin"):
        orig = env.get("LD_LIBRARY_PATH_ORIG")
        if orig:
            env["LD_LIBRARY_PATH"] = orig
        else:
            env.pop("LD_LIBRARY_PATH", None)
    return env


# Bundled yt-dlp + ffmpeg; works without installing anything
YTDLP = _find_binary("yt-dlp")
# The copy inside the package, kept so an updated yt-dlp that turns out to be
# broken has something to fall back to.
BUNDLED_YTDLP = _find_binary("yt-dlp", user_first=False)
_FFMPEG = _find_binary("ffmpeg")
FFPROBE = _find_binary("ffprobe")
FFMPEG_DIR = os.path.dirname(_FFMPEG) if os.path.isfile(_FFMPEG) else ""

HTML = """<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Aevum</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='7' fill='%230a0a0e'/%3E%3Ccircle cx='16' cy='16' r='9' fill='none' stroke='%2300e6a0' stroke-width='2'/%3E%3Ccircle cx='23' cy='9' r='3.2' fill='%23e6fff5'/%3E%3Ccircle cx='16' cy='16' r='1.3' fill='%2300e6a0'/%3E%3C/svg%3E">
<style>
@font-face{font-family:'JetBrains Mono';font-style:normal;font-weight:400;font-display:swap;src:url('/fonts/latin-400-normal.woff2') format('woff2');unicode-range:U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+02C6,U+02DA,U+02DC,U+0304,U+0308,U+0329,U+2000-206F,U+2074,U+20AC,U+2122,U+2191,U+2193,U+2212,U+2215,U+FEFF,U+FFFD}
@font-face{font-family:'JetBrains Mono';font-style:normal;font-weight:400;font-display:swap;src:url('/fonts/latin-ext-400-normal.woff2') format('woff2');unicode-range:U+0100-02BA,U+02BD-02C5,U+02C7-02CC,U+02CE-02D7,U+02DD-02FF,U+0304,U+0308,U+0329,U+1D00-1DBF,U+1E00-1E9F,U+1EF2-1EFF,U+2020,U+20A0-20AB,U+20AD-20C0,U+2113,U+2C60-2C7F,U+A720-A7FF}
@font-face{font-family:'JetBrains Mono';font-style:normal;font-weight:400;font-display:swap;src:url('/fonts/cyrillic-400-normal.woff2') format('woff2');unicode-range:U+0301,U+0400-045F,U+0490-0491,U+04B0-04B1,U+2116}
@font-face{font-family:'JetBrains Mono';font-style:normal;font-weight:500;font-display:swap;src:url('/fonts/latin-500-normal.woff2') format('woff2');unicode-range:U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+02C6,U+02DA,U+02DC,U+0304,U+0308,U+0329,U+2000-206F,U+2074,U+20AC,U+2122,U+2191,U+2193,U+2212,U+2215,U+FEFF,U+FFFD}
@font-face{font-family:'JetBrains Mono';font-style:normal;font-weight:500;font-display:swap;src:url('/fonts/latin-ext-500-normal.woff2') format('woff2');unicode-range:U+0100-02BA,U+02BD-02C5,U+02C7-02CC,U+02CE-02D7,U+02DD-02FF,U+0304,U+0308,U+0329,U+1D00-1DBF,U+1E00-1E9F,U+1EF2-1EFF,U+2020,U+20A0-20AB,U+20AD-20C0,U+2113,U+2C60-2C7F,U+A720-A7FF}
@font-face{font-family:'JetBrains Mono';font-style:normal;font-weight:500;font-display:swap;src:url('/fonts/cyrillic-500-normal.woff2') format('woff2');unicode-range:U+0301,U+0400-045F,U+0490-0491,U+04B0-04B1,U+2116}
:root{--accent:0,230,160;--aura1:120,220,90;--aura2:0,230,160;--aura3:60,170,255}
*{box-sizing:border-box;margin:0;padding:0}
body{background:#060606;font-family:'JetBrains Mono',monospace;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:2rem 1rem;overflow:hidden;position:relative;perspective:1400px}
body::before{content:'';position:fixed;inset:-25%;z-index:0;pointer-events:none;background:radial-gradient(38% 38% at 20% 22%,rgba(var(--aura1),0.12),transparent 60%),radial-gradient(34% 34% at 82% 30%,rgba(var(--aura2),0.11),transparent 60%),radial-gradient(44% 44% at 62% 88%,rgba(var(--aura3),0.10),transparent 60%);filter:blur(34px);animation:aurora 20s ease-in-out infinite alternate}
@keyframes aurora{0%{transform:translate(-3%,-2%) scale(1)}50%{transform:translate(3%,2%) scale(1.08)}100%{transform:translate(-2%,3%) scale(1.04)}}
.root{background:rgba(9,9,11,0.76);backdrop-filter:blur(9px);-webkit-backdrop-filter:blur(9px);border-radius:16px;padding:28px 26px 24px;width:100%;max-width:540px;position:relative;overflow:hidden;border:1px solid rgba(255,255,255,0.07);box-shadow:0 24px 70px -20px rgba(0,0,0,0.8),0 0 0 1px rgba(var(--accent),0.03) inset;transition:transform .3s cubic-bezier(.2,.8,.2,1),box-shadow .3s ease;transform-style:preserve-3d;will-change:transform}
.glow{position:absolute;width:340px;height:340px;border-radius:50%;background:radial-gradient(circle,rgba(var(--accent),0.055) 0%,transparent 70%);pointer-events:none;transform:translate(-50%,-50%);transition:left .1s,top .1s;left:50%;top:50%;z-index:0}
.content{position:relative;z-index:1}
#bg{position:fixed;inset:0;width:100vw;height:100vh;z-index:-1;pointer-events:none}
.mark{font-size:11px;letter-spacing:4px;color:rgba(255,255,255,0.42);margin-bottom:22px;display:flex;align-items:center;gap:9px;font-weight:500}
.mark::before{content:'';width:6px;height:6px;border-radius:50%;background:rgba(var(--accent),0.7);box-shadow:0 0 8px rgba(var(--accent),0.6)}
.urow{display:flex;gap:6px;margin-bottom:20px}
.ui{flex:1;background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.09);border-radius:8px;padding:12px 14px;color:rgba(255,255,255,0.9);font-family:'JetBrains Mono',monospace;font-size:12px;line-height:1.2;outline:none;transition:border-color .25s,background .25s,box-shadow .25s;min-width:0;text-overflow:ellipsis}
.ui:focus{border-color:rgba(var(--accent),0.45);background:rgba(var(--accent),0.03);box-shadow:0 0 0 3px rgba(var(--accent),0.06)}
.ui::placeholder{color:rgba(255,255,255,0.17);font-size:11px}
.go{background:rgba(var(--accent),0.1);border:1px solid rgba(var(--accent),0.22);border-radius:8px;color:rgba(var(--accent),0.75);font-family:'JetBrains Mono',monospace;font-size:11px;padding:11px 18px;cursor:pointer;letter-spacing:.5px;transition:background .2s,border-color .2s,color .2s,transform .12s,box-shadow .2s;white-space:nowrap;flex-shrink:0;position:relative;overflow:hidden}
.go::after{content:'';position:absolute;top:0;left:-70%;width:45%;height:100%;background:linear-gradient(120deg,transparent,rgba(255,255,255,0.28),transparent);transform:skewX(-20deg);transition:left .55s ease}
.go:not(:disabled):hover::after{left:135%}
.go:not(:disabled):hover{background:rgba(var(--accent),0.18);border-color:rgba(var(--accent),0.5);color:rgba(var(--accent),1);box-shadow:0 0 18px -2px rgba(var(--accent),0.35)}
.go:not(:disabled):active{transform:scale(0.95)}
.go:disabled{opacity:.25;cursor:not-allowed}
.row{display:flex;align-items:center;gap:6px;margin-bottom:11px;flex-wrap:wrap}
.lbl{font-size:10px;color:rgba(255,255,255,0.38);letter-spacing:.5px;width:66px;flex-shrink:0;white-space:nowrap}
.hint{font-size:10px;color:rgba(255,255,255,0.3);line-height:1.55;padding-left:72px;margin-bottom:6px}
.chips{display:flex;gap:5px;flex-wrap:wrap}
.chip{border-radius:6px;font-family:'JetBrains Mono',monospace;font-size:10px;padding:6px 11px;cursor:pointer;border:1px solid rgba(255,255,255,0.11);background:rgba(255,255,255,0.05);color:rgba(255,255,255,0.55);letter-spacing:.2px;transition:border-color .18s,background .18s,color .18s,transform .12s;line-height:1.4;user-select:none}
.chip:hover{border-color:rgba(255,255,255,0.26);color:rgba(255,255,255,0.85);transform:translateY(-1px)}
.chip:active{transform:scale(0.93)}
.chip.on{background:rgba(var(--accent),0.08);border-color:rgba(var(--accent),0.32);color:rgba(var(--accent),0.88)}
.chip.mode-on{background:rgba(255,255,255,0.08);border-color:rgba(255,255,255,0.2);color:rgba(255,255,255,0.82)}
.chip.tog-on{background:rgba(255,160,50,0.08);border-color:rgba(255,160,50,0.3);color:rgba(255,160,50,0.85)}
.chip.dim{opacity:.3;pointer-events:none}
.sec{overflow:hidden;transition:max-height .35s cubic-bezier(.4,0,.2,1),opacity .3s ease}
.sec.hide{max-height:0!important;opacity:0;pointer-events:none}
.divider{margin:14px 0;border-top:1px solid rgba(255,255,255,0.05)}
.dir-row{display:flex;align-items:center;gap:6px}
.dir-input{flex:1;background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.07);border-radius:7px;padding:8px 10px;color:rgba(255,255,255,0.5);font-family:'JetBrains Mono',monospace;font-size:10px;outline:none;transition:border-color .2s,color .2s;min-width:0}
.dir-input:focus{border-color:rgba(var(--accent),0.3);color:rgba(255,255,255,0.75)}
.dir-input::placeholder{color:rgba(255,255,255,0.15)}
.pw{margin-top:16px;overflow:hidden;max-height:0;opacity:0;transition:max-height .4s,opacity .3s}
.pw.show{max-height:80px;opacity:1}
.pb{height:2px;background:rgba(255,255,255,0.06);border-radius:99px;overflow:hidden;margin-bottom:8px}
.pf{height:100%;background:linear-gradient(90deg,rgba(var(--accent),0.4),rgba(var(--accent),0.75));border-radius:99px;width:0%;transition:width .5s cubic-bezier(.4,0,.2,1);position:relative}
.pf::after{content:'';position:absolute;right:0;top:-2px;width:4px;height:6px;background:rgba(var(--accent),0.95);border-radius:99px;box-shadow:0 0 6px rgba(var(--accent),0.8)}
.pf::before{content:'';position:absolute;inset:0;background:linear-gradient(90deg,transparent,rgba(255,255,255,0.4),transparent);transform:translateX(-100%);animation:shimmer 1.5s linear infinite}
@keyframes shimmer{100%{transform:translateX(100%)}}
.pf.indet{width:35%!important;animation:indet 1.3s cubic-bezier(.4,0,.2,1) infinite}
@keyframes indet{0%{margin-left:-35%}100%{margin-left:100%}}
.prow{display:flex;align-items:center;justify-content:space-between;gap:10px}
.pt{font-size:9px;color:rgba(var(--accent),0.5);letter-spacing:1.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1;min-width:0}
.stop{display:none;flex-shrink:0;background:rgba(255,80,60,0.1);border:1px solid rgba(255,80,60,0.32);color:rgba(255,120,100,0.95);border-radius:6px;font-family:'JetBrains Mono',monospace;font-size:10px;padding:5px 13px;cursor:pointer;transition:background .2s,transform .12s,box-shadow .2s}
.stop:hover{background:rgba(255,80,60,0.2);box-shadow:0 0 14px -3px rgba(255,80,60,0.4)}
.stop:active{transform:scale(0.93)}
.stop.show{display:block}
.note{font-size:10px;color:rgba(var(--accent),0.9);line-height:1.5;margin:-10px 0 12px 2px;transition:color .2s ease}
.note:empty{display:none}
.pcard{display:none;gap:11px;margin:-8px 0 14px;padding:9px;background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.07);border-radius:10px;align-items:center}
.pcard.show{display:flex;animation:fsd .3s ease both}
.pthumb{width:112px;height:63px;object-fit:cover;border-radius:7px;flex-shrink:0;background:rgba(255,255,255,0.05)}
.pinfo{min-width:0;flex:1}
.ptitle{font-size:11px;color:rgba(255,255,255,0.85);line-height:1.45;overflow:hidden;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;margin-bottom:4px}
.pmeta{font-size:9.5px;color:rgba(255,255,255,0.35);letter-spacing:.3px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.clip-in{width:74px;flex:none;text-align:center}
.clip-sep{color:rgba(255,255,255,0.25);font-size:11px}
.hintlet{font-size:9.5px;color:rgba(255,255,255,0.25);letter-spacing:.2px}
.hist{margin-top:18px;padding-top:14px;border-top:1px solid rgba(255,255,255,0.05)}
.hist-lbl{font-size:9px;letter-spacing:2px;color:rgba(255,255,255,0.14);margin-bottom:10px}
.hist-item{display:flex;align-items:center;gap:8px;padding:7px 0;border-bottom:1px solid rgba(255,255,255,0.04);font-size:10px}
.hist-item:last-child{border-bottom:none}
.hist-url{flex:1;color:rgba(255,255,255,0.35);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.hist-meta{color:rgba(255,255,255,0.2);white-space:nowrap;font-size:9px}
.q-item{display:block;padding:7px 0;border-bottom:1px solid rgba(255,255,255,0.04);font-size:10px}
.q-top{display:flex;align-items:center;gap:8px}
.q-name{flex:1;color:rgba(255,255,255,0.6);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.q-name.wait{color:rgba(255,255,255,0.3)}
.q-stat{color:rgba(var(--accent),0.65);white-space:nowrap;font-size:9px}
.q-stat.wait{color:rgba(255,255,255,0.22)}
.q-x{background:none;border:none;color:rgba(255,255,255,0.25);font-family:inherit;font-size:11px;line-height:1;cursor:pointer;padding:0 2px}
.q-x:hover{color:rgba(255,120,100,0.8)}
.q-bar{height:2px;background:rgba(255,255,255,0.06);border-radius:99px;margin-top:5px;overflow:hidden}
.q-fill{height:100%;background:linear-gradient(90deg,rgba(var(--accent),0.4),rgba(var(--accent),0.75));border-radius:99px;width:0%;transition:width .5s cubic-bezier(.4,0,.2,1);position:relative}
.q-fill::after{content:'';position:absolute;right:0;top:-2px;width:4px;height:6px;background:rgba(var(--accent),0.95);border-radius:99px;box-shadow:0 0 6px rgba(var(--accent),0.8)}
.q-fill::before{content:'';position:absolute;inset:0;background:linear-gradient(90deg,transparent,rgba(255,255,255,0.4),transparent);transform:translateX(-100%);animation:shimmer 1.5s linear infinite}
.hist-ok{color:rgba(var(--accent),0.6);font-size:9px;white-space:nowrap}
.hist-err{color:rgba(255,100,80,0.6);font-size:9px;white-space:nowrap}
.br-controls{position:fixed;right:16px;bottom:16px;z-index:60;display:flex;gap:8px;align-items:flex-end}
.langbox{position:relative;font-family:'JetBrains Mono',monospace}
.settingsbox{position:relative;font-family:'JetBrains Mono',monospace}
.settings-panel{position:absolute;right:0;bottom:calc(100% + 8px);width:250px;background:rgba(14,14,18,0.97);backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);border:1px solid rgba(255,255,255,0.1);border-radius:12px;padding:14px 15px;opacity:0;max-height:0;overflow:hidden;transform:translateY(8px) scale(0.97);transform-origin:bottom right;pointer-events:none;transition:opacity .2s ease,transform .22s cubic-bezier(.2,.9,.3,1.25),max-height .28s ease;box-shadow:0 16px 40px -12px rgba(0,0,0,0.85)}
.settings-panel.open{opacity:1;max-height:430px;transform:translateY(0) scale(1);pointer-events:auto}
.settings-title{font-size:10px;letter-spacing:2px;color:rgba(255,255,255,0.85);margin-bottom:13px;text-transform:uppercase}
.settings-row{display:flex;align-items:center;justify-content:space-between;gap:12px;cursor:pointer}
.settings-label{font-size:12px;color:rgba(255,255,255,0.82)}
.settings-hint{font-size:10px;color:rgba(255,255,255,0.4);line-height:1.55;margin-top:9px}
.switch{position:relative;width:38px;height:20px;flex-shrink:0}
.switch input{opacity:0;width:0;height:0;position:absolute}
.switch .slider{position:absolute;inset:0;background:rgba(255,255,255,0.14);border-radius:20px;transition:background .25s}
.switch .slider::before{content:'';position:absolute;width:14px;height:14px;left:3px;top:3px;background:#fff;border-radius:50%;transition:transform .25s}
.switch input:checked + .slider{background:rgb(var(--accent))}
.switch input:checked + .slider::before{transform:translateX(18px)}
.lang-toggle{display:flex;align-items:center;gap:7px;background:rgba(9,9,11,0.82);backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px);border:1px solid rgba(255,255,255,0.12);border-radius:9px;color:rgba(255,255,255,0.7);font-family:inherit;font-size:11px;letter-spacing:.5px;padding:9px 12px;cursor:pointer;transition:border-color .2s,color .2s,box-shadow .2s,transform .12s}
.lang-toggle:hover{border-color:rgba(var(--accent),0.4);color:rgba(var(--accent),0.9);box-shadow:0 0 16px -4px rgba(var(--accent),0.35)}
.lang-toggle:active{transform:scale(0.96)}
.lang-toggle svg{opacity:.75}
.lang-menu{position:absolute;right:0;bottom:calc(100% + 8px);background:rgba(14,14,18,0.97);backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);border:1px solid rgba(255,255,255,0.1);border-radius:10px;padding:6px;min-width:158px;overflow:hidden;opacity:0;max-height:0;transform:translateY(8px) scale(0.97);transform-origin:bottom right;pointer-events:none;transition:opacity .2s ease,transform .22s cubic-bezier(.2,.9,.3,1.25),max-height .25s ease;box-shadow:0 16px 40px -12px rgba(0,0,0,0.85)}
.lang-menu.open{opacity:1;max-height:340px;transform:translateY(0) scale(1);pointer-events:auto}
.lang-opt{display:flex;align-items:center;justify-content:space-between;gap:10px;width:100%;background:transparent;border:none;border-radius:7px;color:rgba(255,255,255,0.62);font-family:inherit;font-size:11px;text-align:left;padding:8px 10px;cursor:pointer;transition:background .15s,color .15s}
.lang-opt:hover{background:rgba(255,255,255,0.06);color:rgba(255,255,255,0.95)}
.lang-opt.active{color:rgba(var(--accent),0.95)}
.lang-opt.active::after{content:'✓';font-size:11px;color:rgba(var(--accent),0.9)}
.lang-opt .lc{font-size:9px;color:rgba(255,255,255,0.28);letter-spacing:1px}
.themebox{position:fixed;left:16px;bottom:16px;z-index:60;font-family:'JetBrains Mono',monospace}
.theme-toggle{display:flex;align-items:center;justify-content:center;background:rgba(9,9,11,0.82);backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px);border:1px solid rgba(255,255,255,0.12);border-radius:9px;padding:9px 10px;cursor:pointer;transition:border-color .2s,box-shadow .2s,transform .12s}
.theme-toggle:hover{border-color:rgba(var(--accent),0.4);box-shadow:0 0 16px -4px rgba(var(--accent),0.35)}
.theme-toggle:active{transform:scale(0.96)}
.theme-swatch{width:15px;height:15px;border-radius:50%;background:rgb(var(--accent));box-shadow:0 0 8px -1px rgba(var(--accent),0.7);transition:background .3s ease}
.theme-menu{position:absolute;left:0;bottom:calc(100% + 8px);background:rgba(14,14,18,0.97);backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);border:1px solid rgba(255,255,255,0.1);border-radius:10px;padding:6px;min-width:150px;overflow:hidden;opacity:0;max-height:0;transform:translateY(8px) scale(0.97);transform-origin:bottom left;pointer-events:none;transition:opacity .2s ease,transform .22s cubic-bezier(.2,.9,.3,1.25),max-height .25s ease;box-shadow:0 16px 40px -12px rgba(0,0,0,0.85)}
.theme-menu.open{opacity:1;max-height:420px;transform:translateY(0) scale(1);pointer-events:auto}
.theme-opt{display:flex;align-items:center;gap:10px;width:100%;background:transparent;border:none;border-radius:7px;color:rgba(255,255,255,0.62);font-family:inherit;font-size:11px;text-align:left;padding:8px 10px;cursor:pointer;transition:background .15s,color .15s}
.theme-opt:hover{background:rgba(255,255,255,0.06);color:rgba(255,255,255,0.95)}
.theme-opt.active{color:rgba(255,255,255,0.95)}
.theme-opt .dot{width:13px;height:13px;border-radius:50%;flex-shrink:0}
.theme-opt.active .dot{box-shadow:0 0 0 2px rgba(255,255,255,0.25)}
.theme-opt .nm{flex:1}
.theme-opt.active .nm::after{content:'✓';margin-left:6px;opacity:.7}
.info-panel{width:320px}
.info-panel.open{max-height:min(460px,calc(100vh - 90px))}
.info-list{overflow-y:auto;max-height:min(375px,calc(100vh - 175px));padding-right:5px}
.info-list::-webkit-scrollbar{width:4px}
.info-list::-webkit-scrollbar-thumb{background:rgba(255,255,255,0.12);border-radius:4px}
.info-item{font-size:10.5px;color:rgba(255,255,255,0.45);line-height:1.6;margin-bottom:9px}
.info-item:last-child{margin-bottom:0}
.info-item b{color:rgba(255,255,255,0.85);font-weight:600}
@keyframes fsd{from{opacity:0;transform:translateY(-5px)}to{opacity:1;transform:translateY(0)}}
@keyframes pop{0%{transform:scale(1)}40%{transform:scale(1.13)}100%{transform:scale(1)}}
.pop{animation:pop .18s ease}
.mark,.urow,.row{animation:fsd .4s ease both}
</style>
</head>
<body>
<canvas id="bg"></canvas>
<div class="root" id="root">
  <div class="glow" id="glow"></div>
  <div class="content">
    <div class="mark">AEVUM</div>
    <div class="urow">
      <input class="ui" id="u" data-i18n-ph="urlPlaceholder" placeholder="Paste a video link — any site works" autocomplete="off" spellcheck="false"/>
      <button class="go" id="gb" disabled onclick="go()" data-i18n="download">Download</button>
    </div>
    <div class="note" id="note"></div>

    <div class="pcard" id="pcard">
      <img class="pthumb" id="pthumb" alt="" loading="lazy"/>
      <div class="pinfo">
        <div class="ptitle" id="ptitle"></div>
        <div class="pmeta" id="pmeta"></div>
      </div>
    </div>

    <div class="row" style="animation-delay:.1s">
      <span class="lbl" data-i18n="mode">Mode</span>
      <div class="chips">
        <button class="chip mode-on" data-g="mode" data-v="video" onclick="pick(this);setMode('video')" data-i18n="video">Video</button>
        <button class="chip"         data-g="mode" data-v="audio" onclick="pick(this);setMode('audio')" data-i18n="audio">Audio</button>
      </div>
    </div>

    <div class="sec" id="vrows" style="max-height:240px;opacity:1">
      <div class="row" style="animation-delay:.16s">
        <span class="lbl" data-i18n="quality">Quality</span>
        <div class="chips">
          <button class="chip"    data-g="vq" data-v="best"  onclick="pick(this)" data-i18n="best">Best</button>
          <button class="chip"    data-g="vq" data-v="4k"    onclick="pick(this)">4K</button>
          <button class="chip"    data-g="vq" data-v="1440p" onclick="pick(this)">1440p</button>
          <button class="chip on" data-g="vq" data-v="1080p" onclick="pick(this)">1080p</button>
          <button class="chip"    data-g="vq" data-v="720p"  onclick="pick(this)">720p</button>
          <button class="chip"    data-g="vq" data-v="480p"  onclick="pick(this)">480p</button>
          <button class="chip"    data-g="vq" data-v="360p"  onclick="pick(this)">360p</button>
        </div>
      </div>
      <div class="row" style="animation-delay:.2s">
        <span class="lbl" data-i18n="container">Container</span>
        <div class="chips">
          <button class="chip on" data-g="cont" data-v="mp4" onclick="pick(this)" title="MP4 container — H.264 where a site offers it, AV1 or VP9 above 1080p">MP4</button>
          <button class="chip"    data-g="cont" data-v="mkv" onclick="pick(this)" title="Any codec, whatever the site offers at its best — good for archiving">MKV</button>
          <button class="chip"    data-g="cont" data-v="mp4h264" onclick="pick(this)" title="An mp4 that really is H.264, for editors — sites stop making it above 1080p">H.264</button>
          <button class="chip"    data-g="cont" data-v="webm" onclick="pick(this)" title="WebM + VP9 preferred">WebM</button>
        </div>
      </div>
      <div class="row" style="animation-delay:.24s">
        <span class="lbl" data-i18n="options">Options</span>
        <div class="chips">
          <button class="chip" id="subsbtn" onclick="toggleFlag('subs',this)" data-i18n="subtitles">Subtitles</button>
          <button class="chip" id="mutebtn" onclick="toggleFlag('mute',this)" data-i18n="mute">Mute</button>
          <button class="chip" id="thumbbtn" onclick="toggleFlag('thumb',this)" data-i18n="thumbnail">Thumbnail</button>
        </div>
      </div>
    </div>

    <div class="sec hide" id="arows" style="max-height:0;opacity:0">
      <div class="row" style="animation-delay:.16s">
        <span class="lbl" data-i18n="format">Format</span>
        <div class="chips">
          <button class="chip on" data-g="fmt" data-v="mp3"  onclick="pick(this)">MP3</button>
          <button class="chip"    data-g="fmt" data-v="m4a"  onclick="pick(this)">M4A</button>
          <button class="chip"    data-g="fmt" data-v="opus" onclick="pick(this)">Opus</button>
          <button class="chip"    data-g="fmt" data-v="flac" onclick="pick(this)">FLAC</button>
          <button class="chip"    data-g="fmt" data-v="wav"  onclick="pick(this)">WAV</button>
        </div>
      </div>
      <div class="row" style="animation-delay:.22s">
        <span class="lbl" data-i18n="bitrate">Bitrate</span>
        <div class="chips">
          <button class="chip"    data-g="br" data-v="best" onclick="pick(this)" data-i18n="source">Source</button>
          <button class="chip"    data-g="br" data-v="320k" onclick="pick(this)">320k</button>
          <button class="chip on" data-g="br" data-v="192k" onclick="pick(this)">192k</button>
          <button class="chip"    data-g="br" data-v="128k" onclick="pick(this)">128k</button>
          <button class="chip"    data-g="br" data-v="96k"  onclick="pick(this)">96k</button>
        </div>
      </div>
    </div>

    <div class="divider"></div>

    <div class="row" style="margin-bottom:6px">
      <span class="lbl" data-i18n="cookies">Cookies</span>
      <div class="chips">
        <button class="chip on" data-g="cookies" data-v="none"    onclick="pick(this)" data-i18n="none">None</button>
        <button class="chip"    data-g="cookies" data-v="chrome"  onclick="pick(this)">Chrome</button>
        <button class="chip"    data-g="cookies" data-v="edge"    onclick="pick(this)">Edge</button>
        <button class="chip"    data-g="cookies" data-v="firefox" onclick="pick(this)">Firefox</button>
        <button class="chip"    data-g="cookies" data-v="brave"   onclick="pick(this)">Brave</button>
      </div>
    </div>
    <div class="hint" data-i18n="cookiesHint">Use your browser's session to download from sites where you must be logged in (your own account). Close that browser first.</div>

    <div class="row" style="margin-top:10px">
      <span class="lbl" data-i18n="playlist">Playlist</span>
      <div class="chips">
        <button class="chip" id="plbtn" onclick="toggleFlag('playlist',this)" data-i18n="downloadPlaylist">Download Playlist</button>
      </div>
    </div>

    <div class="row" style="margin-top:4px">
      <span class="lbl" data-i18n="clip">Clip</span>
      <div class="chips" style="align-items:center;gap:7px">
        <input class="dir-input clip-in" id="clipStart" placeholder="00:00" autocomplete="off" spellcheck="false"/>
        <span class="clip-sep">→</span>
        <input class="dir-input clip-in" id="clipEnd" placeholder="00:00" autocomplete="off" spellcheck="false"/>
        <button class="chip" id="clipllbtn" onclick="toggleFlag('clipLossless',this)" data-i18n="clipLossless" data-i18n-title="clipLosslessHint">Lossless cut</button>
        <span class="hintlet" data-i18n="clipHint">optional — downloads only this section</span>
      </div>
    </div>

    <div class="dir-row" style="margin-top:14px">
      <span class="lbl" data-i18n="folder">Folder</span>
      <input class="dir-input" id="dir" placeholder="~/Downloads"/>
    </div>

    <div class="pw" id="pw">
      <div class="pb"><div class="pf" id="pf"></div></div>
      <div class="prow">
        <div class="pt" id="pt">—</div>
        <button class="stop" id="stopbtn" onclick="cancelJob()" data-i18n="stop">Stop</button>
      </div>
    </div>

    <div class="hist" id="hist-wrap" style="display:none">
      <div class="hist-lbl" data-i18n="downloads">Downloads</div>
      <div id="queue-list"></div>
      <div id="hist-list"></div>
    </div>
  </div>
</div>
<div class="br-controls">
  <div class="settingsbox" id="infobox">
    <div class="settings-panel info-panel" id="infoPanel">
      <div class="settings-title" id="infoTitle">Guide</div>
      <div class="info-list" id="infoList"></div>
    </div>
    <button class="lang-toggle" id="infoToggle" onclick="toggleInfo(event)" aria-label="Guide">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M12 16v-4.5M12 8h.01"/></svg>
    </button>
  </div>
  <div class="settingsbox" id="settingsbox">
    <div class="settings-panel" id="settingsPanel">
      <div class="settings-title" id="settingsTitle">Settings</div>
      <label class="settings-row" id="startupRow">
        <span class="settings-label" id="settingsStartupLabel">Launch at startup</span>
        <span class="switch"><input type="checkbox" id="startupToggle" onchange="setStartup(this.checked)"/><span class="slider"></span></span>
      </label>
      <div class="settings-hint" id="settingsHint"></div>
      <label class="settings-row" id="menuRow" style="display:none;margin-top:13px">
        <span class="settings-label" id="settingsMenuLabel">Add to app menu</span>
        <span class="switch"><input type="checkbox" id="menuToggle" onchange="setMenu(this.checked)"/><span class="slider"></span></span>
      </label>
      <div class="settings-hint" id="settingsMenuHint" style="display:none"></div>
      <div class="settings-row" style="margin-top:14px">
        <span class="settings-label" id="updLine">Aevum</span>
        <button class="chip" id="updBtn" style="display:none;padding:5px 12px;font-size:9px" onclick="applyUpdate()">Update</button>
      </div>
      <div class="settings-hint" id="updHint"></div>
      <div class="settings-row" style="margin-top:13px">
        <span class="settings-label" id="pkgLine">Packages</span>
        <button class="chip" id="pkgBtn" style="display:none;padding:5px 12px;font-size:9px" onclick="applyPkg()">Update</button>
      </div>
      <div class="settings-hint" id="pkgHint"></div>
      <div class="settings-hint"><span id="pkgRevert" style="display:none;cursor:pointer;text-decoration:underline" onclick="revertPkg()"></span></div>
    </div>
    <button class="lang-toggle" id="settingsToggle" onclick="toggleSettings(event)" aria-label="Settings">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="12" cy="12" r="3.2"/><path d="M19.4 15a1.6 1.6 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.6 1.6 0 0 0-2.7 1.1V21a2 2 0 0 1-4 0v-.1a1.6 1.6 0 0 0-2.7-1.1l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.6 1.6 0 0 0 .3-1.8 1.6 1.6 0 0 0-1.4-1H3a2 2 0 0 1 0-4h.1a1.6 1.6 0 0 0 1.4-1 1.6 1.6 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.6 1.6 0 0 0 1.8.3H9a1.6 1.6 0 0 0 1-1.4V3a2 2 0 0 1 4 0v.1a1.6 1.6 0 0 0 1 1.4 1.6 1.6 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.6 1.6 0 0 0-.3 1.8V9a1.6 1.6 0 0 0 1.4 1H21a2 2 0 0 1 0 4h-.1a1.6 1.6 0 0 0-1.4 1z"/></svg>
    </button>
  </div>
  <div class="langbox" id="langbox">
    <div class="lang-menu" id="langMenu"></div>
    <button class="lang-toggle" id="langToggle" onclick="toggleLangMenu(event)">
      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M2 12h20M12 2a15 15 0 0 1 0 20M12 2a15 15 0 0 0 0 20"/></svg>
      <span id="langCode">EN</span>
    </button>
  </div>
</div>
<div class="themebox" id="themebox">
  <div class="theme-menu" id="themeMenu"></div>
  <button class="theme-toggle" id="themeToggle" onclick="toggleThemeMenu(event)">
    <span class="theme-swatch" id="themeSwatch"></span>
  </button>
</div>
<script>
const inp=document.getElementById('u'),gb=document.getElementById('gb');
const pw=document.getElementById('pw'),pf=document.getElementById('pf'),pt=document.getElementById('pt');
const glow=document.getElementById('glow'),root=document.getElementById('root');
const note=document.getElementById('note'),stopbtn=document.getElementById('stopbtn');
let jobId=null,pollTimer=null;
const state={mode:'video',vq:'1080p',cont:'mp4',fmt:'mp3',br:'192k',cookies:'none',subs:false,mute:false,thumb:false,playlist:false,clipLossless:false};
// ── i18n strings ──
const I18N={
 en:{urlPlaceholder:"Paste a video link — any site works",download:"Download",mode:"Mode",video:"Video",audio:"Audio",quality:"Quality",best:"Best",container:"Container",options:"Options",subtitles:"Subtitles",mute:"Mute",format:"Format",bitrate:"Bitrate",source:"Source",cookies:"Cookies",none:"None",cookiesHint:"Use your browser's session to download from sites where you must be logged in (your own account). Close that browser first.",playlist:"Playlist",downloadPlaylist:"Download Playlist",folder:"Folder",history:"History",downloads:"Downloads",queued:"queued",qRemove:"Remove from queue",stop:"Stop",connecting:"connecting...",starting:"starting...",downloading:"Downloading",processing:"processing...",completed:"completed ✓",stopping:"stopping...",stopped:"stopped",errorGeneric:"an error occurred — check the console",connError:"connection error",mixWarn:"⚠ YouTube Mix (radio) is endless — only the first 50 videos will be downloaded.",mixInfo:"ℹ This is a Mix link. Playlist is off, only this video will download.",appClosed:"⚠ Aevum has quit — relaunch the app to continue.",probeLoading:"loading info…",clipDownloading:"downloading clip…",probeNA:"not available",probeVideos:"videos",probeInPlaylist:"playlist link",clip:"Clip",clipHint:"optional — downloads only this section",subsSkipped:"subtitles were unavailable (skipped)",clipLossless:"Lossless cut",clipLosslessHint:"No re-encode: the cut snaps to the nearest keyframe, so the clip may start a few seconds early.",thumbnail:"Thumbnail",h264Cap:"⚠ H.264 only goes up to {h}p on this video — that is what will download.",cookiesLocked:"Chrome keeps its cookies to itself now — Firefox still works, Edge and Brave do not.",h264Unsure:"This site does not say which codec it serves. Aevum will look for H.264 and usually finds it, but cannot promise it.",h264None:"⚠ This video has no H.264. You will get whatever sits in an mp4, which editors may refuse.",formatMissing:"the format you asked for is not available for this video",siteChanged:"the site refused the download three times — try again, or update Packages in Settings"},
 tr:{urlPlaceholder:"Video bağlantısını yapıştır — her site desteklenir",download:"İndir",mode:"Mod",video:"Video",audio:"Ses",quality:"Kalite",best:"En İyi",container:"Biçim",options:"Seçenek",subtitles:"Altyazı",mute:"Sessiz",format:"Format",bitrate:"Bit Hızı",source:"Kaynak",cookies:"Çerezler",none:"Yok",cookiesHint:"Giriş yapman gereken sitelerden (kendi hesabınla) indirmek için tarayıcının oturumunu kullanır. O tarayıcıyı önce kapat.",playlist:"Liste",downloadPlaylist:"Oynatma Listesini İndir",folder:"Klasör",history:"Geçmiş",downloads:"İndirmeler",queued:"sırada",qRemove:"Kuyruktan çıkar",stop:"Durdur",connecting:"bağlanıyor...",starting:"başlatılıyor...",downloading:"İndiriliyor",processing:"işleniyor...",completed:"tamamlandı ✓",stopping:"durduruluyor...",stopped:"durduruldu",errorGeneric:"hata oluştu — konsolu kontrol et",connError:"bağlantı hatası",mixWarn:"⚠ YouTube Mix (radyo) listesi sonsuzdur — ilk 50 video indirilecek.",mixInfo:"ℹ Bu bir Mix bağlantısı. Liste kapalı, yalnızca bu video inecek.",appClosed:"⚠ Aevum kapandı — devam etmek için uygulamayı yeniden başlat.",probeLoading:"bilgi yükleniyor…",clipDownloading:"klip indiriliyor…",probeNA:"mevcut değil",probeVideos:"video",probeInPlaylist:"liste bağlantısı",clip:"Klip",clipHint:"isteğe bağlı — sadece bu aralığı indirir",subsSkipped:"altyazı alınamadı (atlandı)",clipLossless:"Kayıpsız kesim",clipLosslessHint:"Yeniden kodlama yok: kesim en yakın keyframe'e oturur, klip birkaç saniye erken başlayabilir.",thumbnail:"Kapak",h264Cap:"⚠ Bu videoda H.264 en fazla {h}p — inecek olan bu.",cookiesLocked:"Chrome çerezlerini artık başka programlara açmıyor — Firefox çalışıyor, Edge ve Brave açmıyor.",h264Unsure:"Bu site hangi kodeği sunduğunu bildirmiyor. Aevum H.264 arayacak ve genelde buluyor, ama söz veremez.",h264None:"⚠ Bu videoda H.264 yok. mp4 içinde ne varsa onu alacaksın, kurgu programları açmayabilir.",formatMissing:"istediğin biçim bu videoda yok",siteChanged:"site indirmeyi üç kez reddetti — tekrar dene ya da Ayarlar'dan Paketler'i güncelle"},
 es:{urlPlaceholder:"Pega un enlace de vídeo — cualquier sitio funciona",download:"Descargar",mode:"Modo",video:"Vídeo",audio:"Audio",quality:"Calidad",best:"La mejor",container:"Formato",options:"Opciones",subtitles:"Subtítulos",mute:"Silenciar",format:"Formato",bitrate:"Bitrate",source:"Original",cookies:"Cookies",none:"Ninguno",cookiesHint:"Usa la sesión de tu navegador para descargar de sitios donde debes iniciar sesión (tu propia cuenta). Cierra ese navegador primero.",playlist:"Lista",downloadPlaylist:"Descargar lista",folder:"Carpeta",history:"Historial",downloads:"Descargas",queued:"en cola",qRemove:"Quitar de la cola",stop:"Detener",connecting:"conectando...",starting:"iniciando...",downloading:"Descargando",processing:"procesando...",completed:"completado ✓",stopping:"deteniendo...",stopped:"detenido",errorGeneric:"ocurrió un error — revisa la consola",connError:"error de conexión",mixWarn:"⚠ La Mix (radio) de YouTube es infinita — solo se descargarán los primeros 50 vídeos.",mixInfo:"ℹ Es un enlace Mix. La lista está desactivada, solo se descargará este vídeo.",appClosed:"⚠ Aevum se ha cerrado — vuelve a abrir la aplicación para continuar.",probeLoading:"cargando info…",clipDownloading:"descargando clip…",probeNA:"no disponible",probeVideos:"vídeos",probeInPlaylist:"enlace de lista",clip:"Clip",clipHint:"opcional — descarga solo esta sección",subsSkipped:"subtítulos no disponibles (omitidos)",clipLossless:"Corte sin pérdida",clipLosslessHint:"Sin recodificación: el corte se ajusta al fotograma clave más cercano, el clip puede empezar unos segundos antes.",thumbnail:"Miniatura",h264Cap:"⚠ En este vídeo H.264 llega solo a {h}p — eso es lo que se descargará.",cookiesLocked:"Chrome ya no comparte sus cookies — Firefox sí funciona, Edge y Brave no.",h264Unsure:"Este sitio no dice qué códec ofrece. Aevum buscará H.264 y suele encontrarlo, pero no puede prometerlo.",h264None:"⚠ Este vídeo no tiene H.264. Recibirás lo que haya dentro de un mp4, y los editores pueden rechazarlo.",formatMissing:"el formato que pediste no está disponible para este vídeo",siteChanged:"el sitio rechazó la descarga tres veces — inténtalo otra vez o actualiza Paquetes en Ajustes"},
 de:{urlPlaceholder:"Video-Link einfügen — jede Seite funktioniert",download:"Herunterladen",mode:"Modus",video:"Video",audio:"Audio",quality:"Qualität",best:"Beste",container:"Format",options:"Optionen",subtitles:"Untertitel",mute:"Stumm",format:"Format",bitrate:"Bitrate",source:"Quelle",cookies:"Cookies",none:"Keine",cookiesHint:"Nutzt die Sitzung deines Browsers, um von Seiten herunterzuladen, bei denen du angemeldet sein musst (dein eigenes Konto). Schließe diesen Browser zuerst.",playlist:"Playlist",downloadPlaylist:"Playlist herunterladen",folder:"Ordner",history:"Verlauf",downloads:"Downloads",queued:"in Warteschlange",qRemove:"Aus der Warteschlange entfernen",stop:"Stopp",connecting:"verbinde...",starting:"starte...",downloading:"Wird geladen",processing:"verarbeite...",completed:"fertig ✓",stopping:"stoppe...",stopped:"gestoppt",errorGeneric:"ein Fehler ist aufgetreten — Konsole prüfen",connError:"Verbindungsfehler",mixWarn:"⚠ YouTube-Mix (Radio) ist endlos — nur die ersten 50 Videos werden geladen.",mixInfo:"ℹ Dies ist ein Mix-Link. Playlist ist aus, nur dieses Video wird geladen.",appClosed:"⚠ Aevum wurde beendet — starte die App neu, um fortzufahren.",probeLoading:"Infos werden geladen…",clipDownloading:"Clip wird geladen…",probeNA:"nicht verfügbar",probeVideos:"Videos",probeInPlaylist:"Playlist-Link",clip:"Clip",clipHint:"optional — lädt nur diesen Abschnitt",subsSkipped:"Untertitel nicht verfügbar (übersprungen)",clipLossless:"Verlustfreier Schnitt",clipLosslessHint:"Keine Neukodierung: der Schnitt rastet am nächsten Keyframe ein, der Clip kann ein paar Sekunden früher beginnen.",thumbnail:"Vorschaubild",h264Cap:"⚠ Bei diesem Video reicht H.264 nur bis {h}p — das wird geladen.",cookiesLocked:"Chrome gibt seine Cookies nicht mehr heraus — Firefox geht noch, Edge und Brave nicht.",h264Unsure:"Diese Seite nennt ihren Codec nicht. Aevum sucht nach H.264 und findet es meistens, kann es aber nicht zusagen.",h264None:"⚠ Dieses Video hat kein H.264. Du bekommst, was im mp4 steckt — Schnittprogramme lehnen das eventuell ab.",formatMissing:"das gewünschte Format gibt es für dieses Video nicht",siteChanged:"die Seite hat den Download dreimal abgelehnt — versuch es erneut oder aktualisiere Pakete in den Einstellungen"},
 fr:{urlPlaceholder:"Colle un lien vidéo — tous les sites marchent",download:"Télécharger",mode:"Mode",video:"Vidéo",audio:"Audio",quality:"Qualité",best:"Meilleure",container:"Format",options:"Options",subtitles:"Sous-titres",mute:"Muet",format:"Format",bitrate:"Débit",source:"Source",cookies:"Cookies",none:"Aucun",cookiesHint:"Utilise la session de ton navigateur pour télécharger depuis les sites où tu dois être connecté (ton propre compte). Ferme d'abord ce navigateur.",playlist:"Playlist",downloadPlaylist:"Télécharger la playlist",folder:"Dossier",history:"Historique",downloads:"Téléchargements",queued:"en attente",qRemove:"Retirer de la file",stop:"Arrêter",connecting:"connexion...",starting:"démarrage...",downloading:"Téléchargement",processing:"traitement...",completed:"terminé ✓",stopping:"arrêt...",stopped:"arrêté",errorGeneric:"une erreur s'est produite — vérifie la console",connError:"erreur de connexion",mixWarn:"⚠ Le Mix (radio) YouTube est infini — seules les 50 premières vidéos seront téléchargées.",mixInfo:"ℹ C'est un lien Mix. La playlist est désactivée, seule cette vidéo sera téléchargée.",appClosed:"⚠ Aevum s'est fermé — relance l'application pour continuer.",probeLoading:"chargement…",clipDownloading:"téléchargement du clip…",probeNA:"non disponible",probeVideos:"vidéos",probeInPlaylist:"lien de playlist",clip:"Clip",clipHint:"optionnel — télécharge seulement cette section",subsSkipped:"sous-titres indisponibles (ignorés)",clipLossless:"Coupe sans perte",clipLosslessHint:"Pas de réencodage : la coupe s'aligne sur l'image clé la plus proche, le clip peut commencer quelques secondes plus tôt.",thumbnail:"Miniature",h264Cap:"⚠ Sur cette vidéo, H.264 ne dépasse pas {h}p — voilà ce qui sera téléchargé.",cookiesLocked:"Chrome ne partage plus ses cookies — Firefox fonctionne encore, Edge et Brave non.",h264Unsure:"Ce site ne dit pas quel codec il propose. Aevum cherchera du H.264 et le trouve en général, sans pouvoir le garantir.",h264None:"⚠ Cette vidéo na pas de H.264. Tu recevras ce que contient le mp4, que le montage peut refuser.",formatMissing:"le format demandé nexiste pas pour cette vidéo",siteChanged:"le site a refusé le téléchargement trois fois — réessaie ou mets à jour les Paquets dans les Paramètres"},
 it:{urlPlaceholder:"Incolla un link video — funziona con qualsiasi sito",download:"Scarica",mode:"Modalità",video:"Video",audio:"Audio",quality:"Qualità",best:"Migliore",container:"Formato",options:"Opzioni",subtitles:"Sottotitoli",mute:"Muto",format:"Formato",bitrate:"Bitrate",source:"Originale",cookies:"Cookie",none:"Nessuno",cookiesHint:"Usa la sessione del tuo browser per scaricare dai siti dove devi aver effettuato l'accesso (il tuo account). Chiudi prima quel browser.",playlist:"Playlist",downloadPlaylist:"Scarica playlist",folder:"Cartella",history:"Cronologia",downloads:"Download",queued:"in coda",qRemove:"Rimuovi dalla coda",stop:"Ferma",connecting:"connessione...",starting:"avvio...",downloading:"Scaricamento",processing:"elaborazione...",completed:"completato ✓",stopping:"arresto...",stopped:"fermato",errorGeneric:"si è verificato un errore — controlla la console",connError:"errore di connessione",mixWarn:"⚠ Il Mix (radio) di YouTube è infinito — verranno scaricati solo i primi 50 video.",mixInfo:"ℹ Questo è un link Mix. La playlist è disattivata, verrà scaricato solo questo video.",appClosed:"⚠ Aevum si è chiuso — riavvia l'applicazione per continuare.",probeLoading:"caricamento…",clipDownloading:"download della clip…",probeNA:"non disponibile",probeVideos:"video",probeInPlaylist:"link di playlist",clip:"Clip",clipHint:"opzionale — scarica solo questa sezione",subsSkipped:"sottotitoli non disponibili (saltati)",clipLossless:"Taglio senza perdita",clipLosslessHint:"Nessuna ricodifica: il taglio si aggancia al keyframe più vicino, la clip può iniziare qualche secondo prima.",thumbnail:"Miniatura",h264Cap:"⚠ Su questo video H.264 arriva solo a {h}p — sarà questo a scaricarsi.",cookiesLocked:"Chrome non condivide più i suoi cookie — Firefox funziona ancora, Edge e Brave no.",h264Unsure:"Questo sito non dichiara il codec. Aevum cercherà H.264 e di solito lo trova, ma non può prometterlo.",h264None:"⚠ Questo video non ha H.264. Riceverai quello che sta in un mp4, che il montaggio può rifiutare.",formatMissing:"il formato richiesto non esiste per questo video",siteChanged:"il sito ha rifiutato il download tre volte — riprova o aggiorna i Pacchetti nelle Impostazioni"},
 pt:{urlPlaceholder:"Cole um link de vídeo — qualquer site funciona",download:"Baixar",mode:"Modo",video:"Vídeo",audio:"Áudio",quality:"Qualidade",best:"Melhor",container:"Formato",options:"Opções",subtitles:"Legendas",mute:"Mudo",format:"Formato",bitrate:"Bitrate",source:"Original",cookies:"Cookies",none:"Nenhum",cookiesHint:"Usa a sessão do seu navegador para baixar de sites onde você precisa estar logado (sua própria conta). Feche esse navegador primeiro.",playlist:"Playlist",downloadPlaylist:"Baixar playlist",folder:"Pasta",history:"Histórico",downloads:"Downloads",queued:"na fila",qRemove:"Remover da fila",stop:"Parar",connecting:"conectando...",starting:"iniciando...",downloading:"Baixando",processing:"processando...",completed:"concluído ✓",stopping:"parando...",stopped:"parado",errorGeneric:"ocorreu um erro — verifique o console",connError:"erro de conexão",mixWarn:"⚠ O Mix (rádio) do YouTube é infinito — apenas os primeiros 50 vídeos serão baixados.",mixInfo:"ℹ Este é um link Mix. A playlist está desligada, apenas este vídeo será baixado.",appClosed:"⚠ O Aevum foi encerrado — reabra o aplicativo para continuar.",probeLoading:"carregando…",clipDownloading:"baixando clipe…",probeNA:"indisponível",probeVideos:"vídeos",probeInPlaylist:"link de playlist",clip:"Clipe",clipHint:"opcional — baixa só esta seção",subsSkipped:"legendas indisponíveis (ignoradas)",clipLossless:"Corte sem perdas",clipLosslessHint:"Sem recodificação: o corte encaixa no keyframe mais próximo, o clipe pode começar alguns segundos antes.",thumbnail:"Miniatura",h264Cap:"⚠ Neste vídeo o H.264 vai só até {h}p — é isso que será baixado.",cookiesLocked:"O Chrome não entrega mais seus cookies — o Firefox ainda funciona, Edge e Brave não.",h264Unsure:"Este site não informa o códec. O Aevum vai procurar H.264 e normalmente encontra, mas não pode prometer.",h264None:"⚠ Este vídeo não tem H.264. Você receberá o que estiver num mp4, e editores podem recusar.",formatMissing:"o formato que você pediu não existe para este vídeo",siteChanged:"o site recusou o download três vezes — tente de novo ou atualize os Pacotes nas Configurações"},
 ru:{urlPlaceholder:"Вставьте ссылку на видео — подходит любой сайт",download:"Скачать",mode:"Режим",video:"Видео",audio:"Аудио",quality:"Качество",best:"Лучшее",container:"Формат",options:"Опции",subtitles:"Субтитры",mute:"Без звука",format:"Формат",bitrate:"Битрейт",source:"Источник",cookies:"Cookies",none:"Нет",cookiesHint:"Использует сессию вашего браузера для загрузки с сайтов, где нужен вход (ваш аккаунт). Сначала закройте этот браузер.",playlist:"Плейлист",downloadPlaylist:"Скачать плейлист",folder:"Папка",history:"История",downloads:"Загрузки",queued:"в очереди",qRemove:"Убрать из очереди",stop:"Стоп",connecting:"подключение...",starting:"запуск...",downloading:"Загрузка",processing:"обработка...",completed:"готово ✓",stopping:"остановка...",stopped:"остановлено",errorGeneric:"произошла ошибка — проверьте консоль",connError:"ошибка соединения",mixWarn:"⚠ YouTube Mix (радио) бесконечен — будут загружены только первые 50 видео.",mixInfo:"ℹ Это ссылка Mix. Плейлист выключен, будет загружено только это видео.",appClosed:"⚠ Aevum завершил работу — перезапустите приложение, чтобы продолжить.",probeLoading:"загрузка…",clipDownloading:"загрузка клипа…",probeNA:"недоступно",probeVideos:"видео",probeInPlaylist:"ссылка плейлиста",clip:"Клип",clipHint:"необязательно — скачает только этот отрезок",subsSkipped:"субтитры недоступны (пропущены)",clipLossless:"Без перекодирования",clipLosslessHint:"Разрез по ближайшему ключевому кадру — клип может начаться на несколько секунд раньше.",thumbnail:"Обложка",h264Cap:"⚠ У этого видео H.264 доступен только до {h}p — это и скачается.",cookiesLocked:"Chrome больше не отдаёт свои cookies — Firefox ещё работает, Edge и Brave нет.",h264Unsure:"Этот сайт не сообщает кодек. Aevum поищет H.264 и обычно находит, но обещать не может.",h264None:"⚠ У этого видео нет H.264. Вы получите то, что лежит в mp4 — редакторы могут его не принять.",formatMissing:"запрошенный формат недоступен для этого видео",siteChanged:"сайт трижды отклонил загрузку — попробуйте снова или обновите Пакеты в настройках"}
};
const LANGS=[['en','English'],['tr','Türkçe'],['es','Español'],['de','Deutsch'],['fr','Français'],['it','Italiano'],['pt','Português'],['ru','Русский']];
const langMenu=document.getElementById('langMenu'),langCode=document.getElementById('langCode'),langbox=document.getElementById('langbox');
let curLang=localStorage.getItem('vdl_lang')||'en';
function T(k){const L=I18N[curLang]||I18N.en;return L[k]!==undefined?L[k]:(I18N.en[k]!==undefined?I18N.en[k]:k);}
function renderTexts(){document.querySelectorAll('[data-i18n]').forEach(el=>{el.textContent=T(el.dataset.i18n);});document.querySelectorAll('[data-i18n-ph]').forEach(el=>{el.placeholder=T(el.dataset.i18nPh);});document.querySelectorAll('[data-i18n-title]').forEach(el=>{el.title=T(el.dataset.i18nTitle);});}
function buildLangMenu(){langMenu.innerHTML=LANGS.map(([c,n])=>'<button class="lang-opt'+(c===curLang?' active':'')+'" data-lang="'+c+'" onclick="selectLang(\\''+c+'\\')"><span>'+n+'</span><span class="lc">'+c.toUpperCase()+'</span></button>').join('');}
function applyLang(l){curLang=l;localStorage.setItem('vdl_lang',l);document.documentElement.lang=l;langCode.textContent=l.toUpperCase();renderTexts();updateNote();buildLangMenu();buildThemeMenu();renderSettings();renderInfo();}
function selectLang(l){applyLang(l);saveCfg({lang:l});closeLangMenu();}
function toggleLangMenu(e){e.stopPropagation();langMenu.classList.toggle('open');}
function closeLangMenu(){langMenu.classList.remove('open');}
document.addEventListener('click',e=>{if(langbox&&!langbox.contains(e.target))closeLangMenu();});
function statusText(d){const tag=d.item?'['+d.item+'] ':'';const spd=d.speed?' · '+d.speed+' MB/s':'';const tot=d.total?' · '+d.total:'';const eta=d.eta?' · ETA '+d.eta:'';const det=tot+spd+eta;switch(d.code){case 'queued':return T('queued');case 'download':return tag+T('downloading')+' '+(d.progress||0)+'%'+det;case 'clip':return (d.progress>0?T('downloading')+' '+d.progress+'%'+det:T('clipDownloading'));case 'process':return tag+T('processing');case 'start':return T('starting');case 'done':return T('completed')+(d.subswarn?' — '+T('subsSkipped'):'');case 'stopped':return T('stopped');case 'error':return d.cookieerr?T('cookiesLocked'):d.formaterr?T('formatMissing'):d.staleerr?T('siteChanged'):(d.error_line?d.error_line.slice(0,110):T('errorGeneric'));default:return '';}}
// ── info / guide panel ──
const INFO_TEXT={
 en:{title:'Guide',items:[['Video / Audio','download the full video, or just its sound (e.g. MP3).'],['Quality','best stream up to that height. Dimmed = not offered for this video; hover shows the size.'],['MP4','the most widely accepted container. H.264 where a site offers it, AV1 or VP9 above 1080p.'],['MKV','takes any codec, so it gets whatever the site offers at its best. Good for archiving.'],['H.264','an MP4 that really is H.264 — best for editors. Sites stop making it above 1080p.'],['WebM','VP9 — best quality per megabyte.'],['Subtitles',"embeds the uploader's own subtitles into the file (does not apply to auto captions)."],['Mute','video only, no audio track.'],['Thumbnail','saves the cover as jpg next to the video, in their own folder.'],['Cookies','use your browser login for members-only content (your own account).'],['Playlist','downloads the whole list into a numbered folder.'],['Clip','downloads only the chosen range — frame-exact, at full speed.'],['Lossless cut','no re-encode: original quality, but the clip may start a few seconds early.']]},
 tr:{title:'Rehber',items:[['Video / Ses','videonun tamamını ya da yalnız sesini (örn. MP3) indirir.'],['Kalite','o yüksekliğe kadarki en iyi akışı seçer. Soluk = bu videoda yok; üzerine gelince boyut görünür.'],['MP4','en yaygın kabul gören kapsayıcı. Site sunuyorsa H.264, 1080p üstünde AV1 ya da VP9.'],['MKV','her kodeği kabul eder, sitenin sunduğu en iyisini getirir. Arşiv için iyi.'],['H.264','gerçekten H.264 olan bir MP4 — kurgu için en iyisi. Siteler 1080p üstünde üretmiyor.'],['WebM','VP9 — megabayt başına en iyi kalite.'],['Altyazı','yükleyicinin kendi altyazısını dosyaya gömer (otomatik altyazılar için geçerli değildir).'],['Sessiz','yalnız görüntü, ses izi yok.'],['Kapak','kapak görselini jpg olarak videonun yanına, kendi klasörüne kaydeder.'],['Çerezler','üyelere özel içerik için tarayıcı oturumunu kullanır (kendi hesabın).'],['Liste','tüm oynatma listesini numaralı bir klasöre indirir.'],['Klip','yalnız seçtiğin aralığı indirir — kare hassasiyetinde ve tam hızda.'],['Kayıpsız kesim','yeniden kodlama yok: orijinal kalite, ama klip birkaç saniye erken başlayabilir.']]},
 es:{title:'Guía',items:[['Vídeo / Audio','descarga el vídeo completo o solo su sonido (p. ej. MP3).'],['Calidad','el mejor stream hasta esa altura. Atenuado = no disponible; al pasar el cursor se ve el tamaño.'],['MP4','el contenedor más aceptado. H.264 si el sitio lo ofrece, AV1 o VP9 por encima de 1080p.'],['MKV','acepta cualquier códec, así que trae lo mejor que ofrezca el sitio. Bueno para archivar.'],['H.264','un MP4 que de verdad es H.264 — ideal para editores. Los sitios no lo hacen por encima de 1080p.'],['WebM','VP9 — la mejor calidad por megabyte.'],['Subtítulos','incrusta los subtítulos propios del autor en el archivo (no aplica a los subtítulos automáticos).'],['Silenciar','solo imagen, sin pista de audio.'],['Miniatura','guarda la portada como jpg junto al vídeo, en su propia carpeta.'],['Cookies','usa tu sesión del navegador para contenido de miembros (tu propia cuenta).'],['Lista','descarga toda la lista en una carpeta numerada.'],['Clip','descarga solo el rango elegido — exacto al fotograma y a toda velocidad.'],['Corte sin pérdida','sin recodificar: calidad original, pero el clip puede empezar unos segundos antes.']]},
 de:{title:'Anleitung',items:[['Video / Audio','lädt das ganze Video oder nur den Ton (z. B. MP3).'],['Qualität','bester Stream bis zu dieser Höhe. Abgeblendet = nicht verfügbar; Hover zeigt die Größe.'],['MP4','der am breitesten unterstützte Container. H.264 wenn die Seite es anbietet, darüber AV1 oder VP9.'],['MKV','nimmt jeden Codec und holt das Beste, was die Seite anbietet. Gut zum Archivieren.'],['H.264','ein MP4, das wirklich H.264 ist — ideal für den Schnitt. Über 1080p gibt es das nicht mehr.'],['WebM','VP9 — beste Qualität pro Megabyte.'],['Untertitel','bettet die eigenen Untertitel des Uploaders in die Datei ein (gilt nicht für automatische Untertitel).'],['Stumm','nur Bild, keine Tonspur.'],['Vorschaubild','speichert das Cover als jpg neben dem Video, in eigenem Ordner.'],['Cookies','nutzt deinen Browser-Login für Mitgliederinhalte (eigenes Konto).'],['Playlist','lädt die ganze Liste in einen nummerierten Ordner.'],['Clip','lädt nur den gewählten Abschnitt — bildgenau, in voller Geschwindigkeit.'],['Verlustfreier Schnitt','keine Neukodierung: Originalqualität, der Clip kann aber ein paar Sekunden früher beginnen.']]},
 fr:{title:'Guide',items:[['Vidéo / Audio','télécharge la vidéo entière ou seulement le son (ex. MP3).'],['Qualité','meilleur flux jusqu\\'à cette hauteur. Grisé = indisponible ; le survol montre la taille.'],['MP4','le conteneur le plus largement accepté. H.264 quand le site en propose, AV1 ou VP9 au-dessus de 1080p.'],['MKV','accepte tous les codecs et prend le meilleur que le site propose. Bon pour archiver.'],['H.264','un MP4 qui est vraiment du H.264 — idéal pour le montage. Les sites cessent au-dessus de 1080p.'],['WebM','VP9 — la meilleure qualité par mégaoctet.'],['Sous-titres','incruste les sous-titres de l\\'auteur dans le fichier (ne s\\'applique pas aux sous-titres automatiques).'],['Muet','image seule, sans piste audio.'],['Miniature','enregistre la jaquette en jpg à côté de la vidéo, dans leur dossier.'],['Cookies','utilise ta session navigateur pour le contenu réservé (ton propre compte).'],['Playlist','télécharge toute la liste dans un dossier numéroté.'],['Clip','télécharge seulement la plage choisie — précis à l\\'image, à pleine vitesse.'],['Coupe sans perte','pas de réencodage : qualité d\\'origine, mais le clip peut commencer quelques secondes plus tôt.']]},
 it:{title:'Guida',items:[['Video / Audio','scarica il video intero o solo l\\'audio (es. MP3).'],['Qualità','il miglior stream fino a quell\\'altezza. Attenuato = non disponibile; al passaggio mostra la dimensione.'],['MP4','il contenitore più supportato. H.264 se il sito lo offre, AV1 o VP9 oltre i 1080p.'],['MKV','accetta qualsiasi codec e prende il meglio che il sito offre. Buono per archiviare.'],['H.264','un MP4 che è davvero H.264 — ideale per il montaggio. Oltre i 1080p i siti non lo fanno.'],['WebM','VP9 — la migliore qualità per megabyte.'],['Sottotitoli','incorpora i sottotitoli dell\\'autore nel file (non vale per i sottotitoli automatici).'],['Muto','solo immagine, nessuna traccia audio.'],['Miniatura','salva la copertina come jpg accanto al video, in una cartella dedicata.'],['Cookie','usa il login del browser per contenuti riservati (il tuo account).'],['Playlist','scarica l\\'intera lista in una cartella numerata.'],['Clip','scarica solo l\\'intervallo scelto — preciso al fotogramma, a piena velocità.'],['Taglio senza perdita','nessuna ricodifica: qualità originale, ma la clip può iniziare qualche secondo prima.']]},
 pt:{title:'Guia',items:[['Vídeo / Áudio','baixa o vídeo inteiro ou só o som (ex. MP3).'],['Qualidade','o melhor stream até essa altura. Esmaecido = indisponível; passar o mouse mostra o tamanho.'],['MP4','o contêiner mais aceito. H.264 quando o site oferece, AV1 ou VP9 acima de 1080p.'],['MKV','aceita qualquer códec e pega o melhor que o site oferece. Bom para arquivar.'],['H.264','um MP4 que é mesmo H.264 — ideal para editores. Os sites não fazem acima de 1080p.'],['WebM','VP9 — a melhor qualidade por megabyte.'],['Legendas','incorpora as legendas do próprio autor no arquivo (não se aplica às legendas automáticas).'],['Mudo','só imagem, sem faixa de áudio.'],['Miniatura','salva a capa como jpg ao lado do vídeo, em pasta própria.'],['Cookies','usa o login do navegador para conteúdo de membros (sua própria conta).'],['Playlist','baixa a lista inteira numa pasta numerada.'],['Clipe','baixa só o trecho escolhido — preciso ao quadro, em velocidade total.'],['Corte sem perdas','sem recodificação: qualidade original, mas o clipe pode começar alguns segundos antes.']]},
 ru:{title:'Справка',items:[['Видео / Аудио','скачивает всё видео или только звук (напр. MP3).'],['Качество','лучший поток до этой высоты. Тусклый = недоступно; при наведении виден размер.'],['MP4','самый широко поддерживаемый контейнер. H.264 если сайт его даёт, выше 1080p — AV1 или VP9.'],['MKV','принимает любой кодек и берёт лучшее, что есть на сайте. Хорош для архива.'],['H.264','MP4, который действительно H.264 — лучше всего для монтажа. Выше 1080p сайты его не делают.'],['WebM','VP9 — лучшее качество на мегабайт.'],['Субтитры','встраивает субтитры автора в файл (не относится к автоматическим субтитрам).'],['Без звука','только изображение, без звуковой дорожки.'],['Обложка','сохраняет обложку в jpg рядом с видео, в отдельной папке.'],['Cookies','использует вход в браузере для контента по подписке (ваш аккаунт).'],['Плейлист','скачивает весь список в нумерованную папку.'],['Клип','скачивает только выбранный отрезок — точно до кадра, на полной скорости.'],['Без перекодирования','оригинальное качество, но клип может начаться на несколько секунд раньше.']]}
};
const infoPanel=document.getElementById('infoPanel'),infobox=document.getElementById('infobox');
function renderInfo(){const L=INFO_TEXT[curLang]||INFO_TEXT.en;document.getElementById('infoTitle').textContent=L.title;document.getElementById('infoList').innerHTML=L.items.map(([b,t])=>'<div class="info-item"><b>'+b+'</b> — '+t+'</div>').join('');}
function toggleInfo(e){e.stopPropagation();infoPanel.classList.toggle('open');}
function closeInfo(){infoPanel.classList.remove('open');}
document.addEventListener('click',e=>{if(infobox&&!infobox.contains(e.target))closeInfo();});
// ── themes (accent color) ──
const THEME_LIST=[['green','0,230,160'],['blue','96,165,250'],['purple','167,139,250'],['teal','45,212,191'],['amber','251,191,36'],['crimson','248,113,113'],['mono','226,232,240'],['dynamic',null],['calm',null]];
const THEME_NAMES={
 en:{green:'Green',blue:'Blue',purple:'Purple',teal:'Teal',amber:'Amber',crimson:'Crimson',mono:'Mono',dynamic:'Dynamic',calm:'Calm'},
 tr:{green:'Yeşil',blue:'Mavi',purple:'Mor',teal:'Turkuaz',amber:'Kehribar',crimson:'Kızıl',mono:'Mono',dynamic:'Değişken',calm:'Sakin'},
 es:{green:'Verde',blue:'Azul',purple:'Púrpura',teal:'Turquesa',amber:'Ámbar',crimson:'Carmesí',mono:'Mono',dynamic:'Dinámico',calm:'Calma'},
 de:{green:'Grün',blue:'Blau',purple:'Lila',teal:'Türkis',amber:'Bernstein',crimson:'Karmesin',mono:'Mono',dynamic:'Dynamisch',calm:'Ruhig'},
 fr:{green:'Vert',blue:'Bleu',purple:'Violet',teal:'Turquoise',amber:'Ambre',crimson:'Carmin',mono:'Mono',dynamic:'Dynamique',calm:'Calme'},
 it:{green:'Verde',blue:'Blu',purple:'Viola',teal:'Turchese',amber:'Ambra',crimson:'Cremisi',mono:'Mono',dynamic:'Dinamico',calm:'Calmo'},
 pt:{green:'Verde',blue:'Azul',purple:'Roxo',teal:'Turquesa',amber:'Âmbar',crimson:'Carmesim',mono:'Mono',dynamic:'Dinâmico',calm:'Calmo'},
 ru:{green:'Зелёный',blue:'Синий',purple:'Фиолетовый',teal:'Бирюзовый',amber:'Янтарный',crimson:'Багровый',mono:'Моно',dynamic:'Динамический',calm:'Спокойный'}};
function TT(id){const L=THEME_NAMES[curLang]||THEME_NAMES.en;return L[id]||THEME_NAMES.en[id]||id;}
const themeMenu=document.getElementById('themeMenu'),themebox=document.getElementById('themebox');
let accentRGB='0,230,160';
let curTheme=localStorage.getItem('aevum_theme')||'green';
// Two cycling modes share the hue loop: 'dynamic' makes a full lap in ~2
// minutes, 'calm' drifts through it in ~20 — alive, but easy on the eyes.
let dynamicActive=false,dynHue=155,dynSpeed=0.05;
function rgbToHue(r,g,b){r/=255;g/=255;b/=255;const mx=Math.max(r,g,b),mn=Math.min(r,g,b),d=mx-mn;let h=0;if(d){if(mx===r)h=((g-b)/d)%6;else if(mx===g)h=(b-r)/d+2;else h=(r-g)/d+4;h*=60;if(h<0)h+=360;}return h;}
function setAccent(rgb){accentRGB=rgb;const ds=document.documentElement.style;ds.setProperty('--accent',rgb);const p=rgb.split(','),h=rgbToHue(+p[0],+p[1],+p[2]);ds.setProperty('--aura1',hslToRgb(((h-60+360)%360)/360,0.72,0.55));ds.setProperty('--aura2',hslToRgb(h/360,0.72,0.55));ds.setProperty('--aura3',hslToRgb(((h+60)%360)/360,0.72,0.55));}
function hslToRgb(h,s,l){let r,g,b;if(s===0){r=g=b=l;}else{const f=(p,q,t)=>{if(t<0)t+=1;if(t>1)t-=1;if(t<1/6)return p+(q-p)*6*t;if(t<1/2)return q;if(t<2/3)return p+(q-p)*(2/3-t)*6;return p;};const q=l<0.5?l*(1+s):l+s-l*s,p=2*l-q;r=f(p,q,h+1/3);g=f(p,q,h);b=f(p,q,h-1/3);}return Math.round(r*255)+','+Math.round(g*255)+','+Math.round(b*255);}
function applyTheme(t){if(!THEME_LIST.some(x=>x[0]===t))t='green';curTheme=t;localStorage.setItem('aevum_theme',t);dynamicActive=(t==='dynamic'||t==='calm');dynSpeed=(t==='calm')?0.005:0.05;if(!dynamicActive){setAccent(THEME_LIST.find(x=>x[0]===t)[1]);}buildThemeMenu();}
function buildThemeMenu(){themeMenu.innerHTML=THEME_LIST.map(([id,rgb])=>{const dot=rgb?('background:rgb('+rgb+')'):(id==='calm'?'background:conic-gradient(from 0deg,#c9a1a1,#c9c3a1,#a1c9b1,#a1b5c9,#b6a1c9,#c9a1a1)':'background:conic-gradient(from 0deg,#ff5b5b,#ffd93b,#4be08a,#4aa3ff,#a77bff,#ff5b5b)');return '<button class="theme-opt'+(id===curTheme?' active':'')+'" onclick="selectTheme(\\''+id+'\\')"><span class="dot" style="'+dot+'"></span><span class="nm">'+TT(id)+'</span></button>';}).join('');}
function selectTheme(t){applyTheme(t);saveCfg({theme:t});closeThemeMenu();}
function toggleThemeMenu(e){e.stopPropagation();themeMenu.classList.toggle('open');}
function closeThemeMenu(){themeMenu.classList.remove('open');}
document.addEventListener('click',e=>{if(themebox&&!themebox.contains(e.target))closeThemeMenu();});
// ── settings ──
const SETTINGS_TEXT={
 en:{settings:'Settings',startup:'Launch at startup',startupHint:'Aevum starts with the system and waits quietly in the tray — open it whenever you need it.',menu:'Add to app menu',menuHint:'Installs Aevum into your app menu — launch it like a regular app, no terminal needed.',updGet:'Update',updNew:'{v} is out.',updLatest:'This is the newest version.',updManual:'{v} is out - get it from the releases page.',updWorking:'downloading... {p}%',updDone:'Downloaded next to the current file. Close Aevum and swap the two.',updStarted:'Installer started - Aevum is closing.',updFail:'Update failed.',updBusy:'A download is running. Let it finish first.',pkgName:'Packages',pkgLatest:'{v} — up to date.',pkgNew:'{v} is available.',pkgWorking:'updating...',pkgDone:'Updated to {v}.',pkgFail:'Could not update.',pkgRevert:'back to the bundled version'},
 tr:{settings:'Ayarlar',startup:'Başlangıçta aç',startupHint:'Aevum, sistemle birlikte başlar ve tepside sessizce bekler — gerektiğinde açarsın.',menu:'Uygulama menüsüne kur',menuHint:"Aevum'u uygulama menüsüne kurar — terminale gerek kalmadan normal bir uygulama gibi başlatırsın.",updGet:'Güncelle',updNew:'{v} çıktı.',updLatest:'En güncel sürümdesin.',updManual:'{v} çıktı - sürümler sayfasından indir.',updWorking:'iniyor... %{p}',updDone:'Yenisi mevcut dosyanın yanına indi. Aevum kapandıktan sonra ikisini değiştir.',updStarted:'Kurulum başladı - Aevum kapanıyor.',updFail:'Güncelleme başarısız.',updBusy:'Bir indirme sürüyor. Önce onun bitmesini bekle.',pkgName:'Paketler',pkgLatest:'{v} — güncel.',pkgNew:'{v} çıktı.',pkgWorking:'güncelleniyor...',pkgDone:'{v} sürümüne güncellendi.',pkgFail:'Güncellenemedi.',pkgRevert:'pakettekine dön'},
 es:{settings:'Ajustes',startup:'Abrir al inicio',startupHint:'Aevum se inicia con el sistema y espera en la bandeja — ábrelo cuando lo necesites.',menu:'Añadir al menú',menuHint:'Instala Aevum en el menú de aplicaciones — ábrelo como una app normal, sin terminal.',updGet:'Actualizar',updNew:'{v} ya está disponible.',updLatest:'Tienes la última versión.',updManual:'{v} ya está - descárgalo desde la página de versiones.',updWorking:'descargando... {p}%',updDone:'Descargado junto al actual. Cierra Aevum y cambia uno por otro.',updStarted:'Instalador iniciado - Aevum se está cerrando.',updFail:'No se pudo actualizar.',updBusy:'Hay una descarga en curso. Espera a que termine.',pkgName:'Paquetes',pkgLatest:'{v} — al día.',pkgNew:'{v} ya está disponible.',pkgWorking:'actualizando...',pkgDone:'Actualizado a {v}.',pkgFail:'No se pudo actualizar.',pkgRevert:'volver a la versión incluida'},
 de:{settings:'Einstellungen',startup:'Beim Start öffnen',startupHint:'Aevum startet mit dem System und wartet im Infobereich — öffne es bei Bedarf.',menu:'Zum App-Menü hinzufügen',menuHint:'Installiert Aevum ins Anwendungsmenü — starte es wie eine normale App, ohne Terminal.',updGet:'Aktualisieren',updNew:'{v} ist da.',updLatest:'Du hast die neueste Version.',updManual:'{v} ist da - hol es von der Releases-Seite.',updWorking:'lädt... {p}%',updDone:'Neben der aktuellen Datei gespeichert. Aevum schließen und tauschen.',updStarted:'Installer gestartet - Aevum wird beendet.',updFail:'Update fehlgeschlagen.',updBusy:'Ein Download läuft. Warte, bis er fertig ist.',pkgName:'Pakete',pkgLatest:'{v} — aktuell.',pkgNew:'{v} ist verfügbar.',pkgWorking:'wird aktualisiert...',pkgDone:'Auf {v} aktualisiert.',pkgFail:'Aktualisierung fehlgeschlagen.',pkgRevert:'zurück zur mitgelieferten Version'},
 fr:{settings:'Paramètres',startup:'Lancer au démarrage',startupHint:'Aevum démarre avec le système et attend dans la barre — ouvre-le au besoin.',menu:'Ajouter au menu',menuHint:"Installe Aevum dans le menu des applications — lance-le comme une app normale, sans terminal.",updGet:'Mettre à jour',updNew:'{v} est disponible.',updLatest:'Tu as la dernière version.',updManual:'{v} est disponible - récupère-le sur la page des versions.',updWorking:'téléchargement... {p}%',updDone:'Téléchargé à côté du fichier actuel. Ferme Aevum et remplace-le.',updStarted:'Installateur lancé - Aevum se ferme.',updFail:'Mise à jour impossible.',updBusy:'Un téléchargement est en cours. Attends la fin.',pkgName:'Paquets',pkgLatest:'{v} — à jour.',pkgNew:'{v} est disponible.',pkgWorking:'mise à jour...',pkgDone:'Mis à jour vers {v}.',pkgFail:'Mise à jour impossible.',pkgRevert:'revenir à la version fournie'},
 it:{settings:'Impostazioni',startup:"Avvia all'avvio",startupHint:'Aevum si avvia con il sistema e resta nella barra — aprilo quando serve.',menu:'Aggiungi al menu',menuHint:'Installa Aevum nel menu delle applicazioni — avvialo come una normale app, senza terminale.',updGet:'Aggiorna',updNew:'{v} è uscita.',updLatest:'Hai la versione più recente.',updManual:'{v} è uscita - scaricala dalla pagina delle versioni.',updWorking:'download... {p}%',updDone:'Scaricato accanto al file attuale. Chiudi Aevum e sostituiscilo.',updStarted:'Installer avviato - Aevum si sta chiudendo.',updFail:'Aggiornamento non riuscito.',updBusy:'Un download è in corso. Aspetta che finisca.',pkgName:'Pacchetti',pkgLatest:'{v} — aggiornato.',pkgNew:'{v} è disponibile.',pkgWorking:'aggiornamento...',pkgDone:'Aggiornato a {v}.',pkgFail:'Aggiornamento non riuscito.',pkgRevert:'torna alla versione inclusa'},
 pt:{settings:'Configurações',startup:'Abrir ao iniciar',startupHint:'O Aevum inicia com o sistema e espera na bandeja — abra quando precisar.',menu:'Adicionar ao menu',menuHint:'Instala o Aevum no menu de aplicativos — abra como um app normal, sem terminal.',updGet:'Atualizar',updNew:'{v} saiu.',updLatest:'Você tem a versão mais recente.',updManual:'{v} saiu - baixe na página de versões.',updWorking:'baixando... {p}%',updDone:'Baixado ao lado do atual. Feche o Aevum e troque os dois.',updStarted:'Instalador iniciado - o Aevum está fechando.',updFail:'Falha ao atualizar.',updBusy:'Há um download em andamento. Espere terminar.',pkgName:'Pacotes',pkgLatest:'{v} — atualizado.',pkgNew:'{v} está disponível.',pkgWorking:'atualizando...',pkgDone:'Atualizado para {v}.',pkgFail:'Falha ao atualizar.',pkgRevert:'voltar à versão incluída'},
 ru:{settings:'Настройки',startup:'Запуск при старте',startupHint:'Aevum запускается вместе с системой и ждёт в трее — откройте, когда понадобится.',menu:'Добавить в меню',menuHint:'Устанавливает Aevum в меню приложений — запускайте как обычное приложение, без терминала.',updGet:'Обновить',updNew:'Вышла {v}.',updLatest:'У вас последняя версия.',updManual:'Вышла {v} - скачайте со страницы релизов.',updWorking:'загрузка... {p}%',updDone:'Загружено рядом с текущим файлом. Закройте Aevum и замените его.',updStarted:'Установщик запущен - Aevum закрывается.',updFail:'Не удалось обновить.',updBusy:'Идёт загрузка. Дождитесь её окончания.',pkgName:'Пакеты',pkgLatest:'{v} — актуально.',pkgNew:'Доступна {v}.',pkgWorking:'обновление...',pkgDone:'Обновлено до {v}.',pkgFail:'Не удалось обновить.',pkgRevert:'вернуться к версии из пакета'}
};
const settingsPanel=document.getElementById('settingsPanel'),settingsbox=document.getElementById('settingsbox'),settingsTitle=document.getElementById('settingsTitle'),settingsStartupLabel=document.getElementById('settingsStartupLabel'),settingsHint=document.getElementById('settingsHint'),startupToggle=document.getElementById('startupToggle');
const menuRow=document.getElementById('menuRow'),menuToggle=document.getElementById('menuToggle'),settingsMenuLabel=document.getElementById('settingsMenuLabel'),settingsMenuHint=document.getElementById('settingsMenuHint'),startupRow=document.getElementById('startupRow');
function TS(k){const L=SETTINGS_TEXT[curLang]||SETTINGS_TEXT.en;return L[k]||SETTINGS_TEXT.en[k]||k;}
function renderSettings(){settingsTitle.textContent=TS('settings');settingsStartupLabel.textContent=TS('startup');settingsHint.textContent=TS('startupHint');settingsMenuLabel.textContent=TS('menu');settingsMenuHint.textContent=TS('menuHint');renderUpd();renderPkg();}
// ── Updating Aevum itself ──
// The check runs when the panel opens, not at launch: nobody wants a
// download tool phoning home before it has been asked to do anything.
const updLine=document.getElementById('updLine'),updBtn=document.getElementById('updBtn'),updHint=document.getElementById('updHint');
let updInfo=null,updState=null,updTimer=null,updAsked=false;
function renderUpd(){
  if(!updInfo){updLine.textContent='Aevum';updHint.textContent='';updBtn.style.display='none';return;}
  updLine.textContent='Aevum '+updInfo.current;
  updBtn.textContent=TS('updGet');
  if(updState&&updState.stage!=='idle'){
    // An error is a dead end without this: the panel would sit on the
    // message with no way back short of reloading the page.
    updBtn.style.display=(updState.stage==='error'&&updInfo&&updInfo.newer&&updInfo.canApply)?'':'none';
    const s=updState.stage;
    updHint.textContent = s==='download' ? TS('updWorking').replace('{p}',updState.pct||0)
                        : s==='launched' ? TS('updStarted')
                        : s==='done'     ? TS('updDone')
                        : s==='error'    ? (updState.busy?TS('updBusy'):TS('updFail')+(updState.msg?' ('+updState.msg+')':''))
                        : '';
    return;
  }
  if(!updInfo.ok){updHint.textContent='';updBtn.style.display='none';return;}
  if(updInfo.newer){
    updHint.textContent=TS(updInfo.canApply?'updNew':'updManual').replace('{v}',updInfo.latest);
    updBtn.style.display=updInfo.canApply?'':'none';
  }else{updHint.textContent=TS('updLatest');updBtn.style.display='none';}
}
function checkUpdate(){fetch('/update/check').then(r=>r.json()).then(d=>{updInfo=d;renderUpd();}).catch(()=>{});}
function pollUpd(){fetch('/update/status').then(r=>r.json()).then(d=>{updState=d;renderUpd();
  if(d.stage!=='download'&&updTimer){clearInterval(updTimer);updTimer=null;}}).catch(()=>{});}
function applyUpdate(){updBtn.style.display='none';updState={stage:'download',pct:0};renderUpd();
  fetch('/update/apply',{method:'POST',headers:{'X-Aevum':'1'}}).then(r=>r.json()).then(d=>{updState=d;renderUpd();
    if(!updTimer)updTimer=setInterval(pollUpd,800);}).catch(()=>{updState={stage:'error',msg:''};renderUpd();});}
// ── Updating yt-dlp, kept as its own line ──
// Two different things wear two different buttons: this one is the piece
// that goes stale between releases, and it moves on its own schedule.
const pkgLine=document.getElementById('pkgLine'),pkgBtn=document.getElementById('pkgBtn'),pkgHint=document.getElementById('pkgHint'),pkgRevert=document.getElementById('pkgRevert');
let pkgInfo=null,pkgState=null,pkgTimer=null;
function renderPkg(){
  // The version lives in the hint, not next to the name: a yt-dlp version is
  // a date, and "Paketler 2026.07.04" plus a button wraps this narrow panel.
  pkgLine.textContent=TS('pkgName');
  pkgBtn.textContent=TS('updGet');
  pkgRevert.textContent=TS('pkgRevert');
  pkgRevert.style.display=(pkgInfo&&pkgInfo.custom&&(!pkgState||pkgState.stage!=='working'))?'':'none';
  if(pkgState&&pkgState.stage&&pkgState.stage!=='idle'){
    const s=pkgState.stage;
    pkgBtn.style.display=(s==='error')?'':'none';
    pkgHint.textContent = s==='working' ? TS('pkgWorking')
                        : s==='done'    ? TS('pkgDone').replace('{v}',pkgState.version||'')
                        : s==='error'   ? (pkgState.busy?TS('updBusy'):TS('pkgFail')+(pkgState.msg?' ('+pkgState.msg+')':''))
                        : '';
    return;
  }
  if(!pkgInfo||!pkgInfo.ok){pkgHint.textContent='';pkgBtn.style.display='none';return;}
  if(pkgInfo.newer){pkgHint.textContent=TS('pkgNew').replace('{v}',pkgInfo.latest);pkgBtn.style.display='';}
  else{pkgHint.textContent=TS('pkgLatest').replace('{v}',pkgInfo.current||'');pkgBtn.style.display='none';}
}
function checkPkg(){fetch('/packages/check').then(r=>r.json()).then(d=>{pkgInfo=d;renderPkg();}).catch(()=>{});}
function pollPkg(){fetch('/packages/status').then(r=>r.json()).then(d=>{pkgState=d;renderPkg();
  // Also on 'idle', which is how the server reports "nothing to install":
  // without a fresh check the line would keep offering the update it just
  // found was unnecessary.
  if(d.stage!=='working'){if(pkgTimer){clearInterval(pkgTimer);pkgTimer=null;}if(d.stage!=='error')checkPkg();}}).catch(()=>{});}
function applyPkg(){pkgBtn.style.display='none';pkgState={stage:'working'};renderPkg();
  fetch('/packages/apply',{method:'POST',headers:{'X-Aevum':'1'}}).then(r=>r.json()).then(d=>{pkgState=d;renderPkg();
    if(!pkgTimer)pkgTimer=setInterval(pollPkg,900);}).catch(()=>{pkgState={stage:'error',msg:''};renderPkg();});}
// A refusal here arrives as a 409 with a body, which fetch treats as a
// perfectly good answer. Reading only the body would swallow it: the user
// presses the link during a download, nothing happens, nothing is said.
function revertPkg(){fetch('/packages/revert',{method:'POST',headers:{'X-Aevum':'1'}})
  .then(r=>r.json().then(b=>({ok:r.ok,body:b})))
  .then(x=>{if(!x.ok){pkgState={stage:'error',busy:!!x.body.busy,msg:x.body.msg||''};renderPkg();return;}
    pkgState=null;checkPkg();}).catch(()=>{});}
function toggleSettings(e){e.stopPropagation();settingsPanel.classList.toggle('open');
  if(settingsPanel.classList.contains('open')){
    // "Updated to X" has been read by now. Without this it stays on the line
    // for the rest of the session, hiding the version it just installed.
    if(pkgState&&pkgState.stage==='done'){pkgState=null;renderPkg();}
    if(!updAsked){updAsked=true;checkUpdate();checkPkg();}
  }}
function closeSettings(){settingsPanel.classList.remove('open');}
document.addEventListener('click',e=>{if(settingsbox&&!settingsbox.contains(e.target))closeSettings();});
function saveCfg(o){fetch('/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(o)}).catch(()=>{});}
function loadSettings(){fetch('/settings').then(r=>r.json()).then(s=>{startupToggle.checked=!!s.startup;menuToggle.checked=!!s.menu;const cs=s.canStartup!==false;startupRow.style.display=cs?'flex':'none';settingsHint.style.display=cs?'block':'none';menuRow.style.display=s.canMenu?'flex':'none';menuRow.style.marginTop=cs?'13px':'0';settingsMenuHint.style.display=s.canMenu?'block':'none';if(s.lang&&I18N[s.lang]&&s.lang!==curLang)applyLang(s.lang);if(s.theme&&THEME_LIST.some(x=>x[0]===s.theme)&&s.theme!==curTheme)applyTheme(s.theme);}).catch(()=>{});}
function setStartup(on){fetch('/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({startup:on})}).catch(()=>{});}
function setMenu(on){menuToggle.disabled=true;fetch('/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({menu:on})}).then(r=>r.json()).then(s=>{menuToggle.checked=!!s.menu;}).catch(()=>{menuToggle.checked=!on;}).finally(()=>{menuToggle.disabled=false;});}
// ── full-window interactive particle network ──
const cv=document.getElementById('bg'),cx=cv.getContext('2d',{alpha:true});
const REDUCE=!!(window.matchMedia&&window.matchMedia('(prefers-reduced-motion: reduce)').matches);
const REP=130,REP2=REP*REP,CON=110,CON2=CON*CON;   // repel / link distances (canvas space)
let W=0,H=0,scale=1,parts=[];
const ptr={x:-9999,y:-9999,inside:false};
let rL=0,rT=0,rW=1,rH=1;
function cacheRect(){rL=root.offsetLeft;rT=root.offsetTop;rW=root.offsetWidth||1;rH=root.offsetHeight||1;}
function resizeCv(){const cw=window.innerWidth,ch=window.innerHeight;scale=Math.min(1,1500/Math.max(cw,1));W=cv.width=Math.round(cw*scale);H=cv.height=Math.round(ch*scale);cacheRect();}
function idealCount(){return Math.max(28,Math.min(85,Math.floor(window.innerWidth*window.innerHeight/16000)));}
function newPart(){return {x:Math.random()*W,y:Math.random()*H,vx:(Math.random()-.5)*.2,vy:(Math.random()-.5)*.2};}
function initParts(){parts=[];const n=idealCount();for(let i=0;i<n;i++)parts.push(newPart());}
function applyTilt(){
  if(REDUCE)return;
  if(!ptr.inside){root.style.transform='';return;}
  const mx=ptr.x-rL,my=ptr.y-rT;glow.style.left=mx+'px';glow.style.top=my+'px';
  const nx=Math.max(-.5,Math.min(.5,mx/rW-.5)),ny=Math.max(-.5,Math.min(.5,my/rH-.5));
  root.style.transform='rotateX('+(ny*-3.5).toFixed(2)+'deg) rotateY('+(nx*3.5).toFixed(2)+'deg)';
}
function drawScene(){
  cx.clearRect(0,0,W,H);
  const mx=ptr.inside?ptr.x*scale:-99999,my=ptr.inside?ptr.y*scale:-99999;
  cx.lineWidth=1;
  for(let i=0;i<parts.length;i++){const a=parts[i];
    for(let j=i+1;j<parts.length;j++){const b=parts[j],dx=a.x-b.x,dy=a.y-b.y,d2=dx*dx+dy*dy;
      if(d2<CON2){const al=(1-Math.sqrt(d2)/CON)*0.15;cx.strokeStyle='rgba('+accentRGB+','+al+')';cx.beginPath();cx.moveTo(a.x,a.y);cx.lineTo(b.x,b.y);cx.stroke();}}}
  for(const p of parts){const ddx=p.x-mx,ddy=p.y-my,near=(ddx*ddx+ddy*ddy)<REP2;cx.fillStyle=near?'rgba('+accentRGB+',0.85)':'rgba('+accentRGB+',0.45)';cx.beginPath();cx.arc(p.x,p.y,near?2:1.5,0,6.28);cx.fill();}
}
let last=performance.now(),winT=last,winF=0;
function frame(now){
  requestAnimationFrame(frame);
  if(document.hidden){last=now;return;}
  let dt=(now-last)/16.667;last=now;if(dt>3)dt=3;else if(dt<0)dt=0;
  if(dynamicActive){dynHue=(dynHue+dynSpeed*dt)%360;setAccent(hslToRgb(dynHue/360,0.55,0.62));}
  applyTilt();
  const mx=ptr.inside?ptr.x*scale:-99999,my=ptr.inside?ptr.y*scale:-99999;
  for(const p of parts){p.x+=p.vx*dt;p.y+=p.vy*dt;if(p.x<0||p.x>W)p.vx*=-1;if(p.y<0||p.y>H)p.vy*=-1;const dx=p.x-mx,dy=p.y-my,d2=dx*dx+dy*dy;if(d2<REP2){const d=Math.sqrt(d2)||1,f=(REP-d)/REP*1.1*dt;p.x+=dx/d*f;p.y+=dy/d*f;}}
  drawScene();
  // ── adapt to the hardware: shed particles when slow, add back when smooth ──
  winF++;if(now-winT>=1000){const fps=winF*1000/(now-winT);winF=0;winT=now;
    if(fps<45&&parts.length>28)parts.length=Math.max(28,parts.length-8);
    else if(fps>=58&&parts.length<idealCount())for(let k=0;k<6&&parts.length<idealCount();k++)parts.push(newPart());}
}
resizeCv();initParts();
if(REDUCE){drawScene();}else{requestAnimationFrame(frame);}
let rzT=null;window.addEventListener('resize',()=>{if(rzT)return;rzT=setTimeout(()=>{rzT=null;resizeCv();initParts();if(REDUCE)drawScene();},150);});
window.addEventListener('mousemove',e=>{ptr.x=e.clientX;ptr.y=e.clientY;ptr.inside=true;},{passive:true});
window.addEventListener('mouseout',e=>{if(!e.relatedTarget){ptr.inside=false;}},{passive:true});
window.addEventListener('blur',()=>{ptr.inside=false;});
// ── Preview card: probe metadata in the background, never block anything ──
const pcard=document.getElementById('pcard'),pthumb=document.getElementById('pthumb'),ptitle=document.getElementById('ptitle'),pmeta=document.getElementById('pmeta');
let probeTimer=null,probeSeq=0,probeInfo=null;
function fmtDur(s){if(!s)return'';const h=Math.floor(s/3600),m=Math.floor((s%3600)/60),x=Math.floor(s%60);return (h?h+':'+String(m).padStart(2,'0'):m)+':'+String(x).padStart(2,'0');}
function fmtSize(b){if(!b)return'';const mb=b/1048576;return mb>=1024?(mb/1024).toFixed(1)+' GB':Math.round(mb)+' MB';}
function hideCard(){pcard.classList.remove('show');setQualityStates(null);probeInfo=null;updateNote();}
const QMAP={'4k':'2160','1440p':'1440','1080p':'1080','720p':'720','480p':'480','360p':'360'};
function setQualityStates(q){
  document.querySelectorAll('[data-g="vq"]').forEach(btn=>{
    const v=btn.dataset.v;
    if(!q||v==='best'){btn.classList.remove('dim');btn.title='';return;}
    const e=q[QMAP[v]];
    if(!e||!e.ok){btn.classList.add('dim');btn.title=T('probeNA');}
    else{btn.classList.remove('dim');btn.title=e.size?((e.approx?'~':'')+fmtSize(e.size)):'';}
  });
  // If the selected quality just became unavailable, fall back to Best
  const sel=document.querySelector('[data-g="vq"].on');
  if(sel&&sel.classList.contains('dim')){
    const best=document.querySelector('[data-g="vq"][data-v="best"]');
    if(best)pick(best);
  }
}
function runProbe(url){
  const myProbe=++probeSeq;
  // Show the card immediately in a loading state — the metadata itself
  // takes a couple of seconds, so at least the UI reacts instantly
  pthumb.style.display='none';
  ptitle.textContent=T('probeLoading');
  pmeta.textContent='';
  pcard.classList.add('show');
  fetch('/probe',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url})})
    .then(r=>r.ok?r.json():null)
    .then(d=>{
      if(myProbe!==probeSeq)return;
      if(!d||d.error){hideCard();return;}
      if(d.kind==='playlist'){
        pthumb.style.display='none';
        ptitle.textContent=d.title||T('playlist');
        pmeta.textContent=T('playlist')+' · '+d.count+' '+T('probeVideos');
        pcard.classList.add('show');
        setQualityStates(null);
        return;
      }
      if(d.thumbnail){pthumb.src=d.thumbnail;pthumb.style.display='block';}
      else{pthumb.style.display='none';}
      ptitle.textContent=d.title||'';
      const parts=[];
      if(d.uploader)parts.push(d.uploader);
      if(d.duration)parts.push(fmtDur(d.duration));
      if(d.inPlaylist)parts.push(T('probeInPlaylist'));
      pmeta.textContent=parts.join(' · ');
      pcard.classList.add('show');
      setQualityStates(d.qualities);
      probeInfo=d;updateNote();
    }).catch(()=>{});
}
let justPasted=false;
inp.addEventListener('paste',()=>{justPasted=true;});
inp.addEventListener('input',()=>{
  gb.disabled=!inp.value.trim();updateNote();
  hideCard();
  // A new link means a new video — yesterday's clip range would silently
  // cut the wrong section, so the clip fields reset. Other options stay:
  // the user may well want the same quality/format for several videos.
  document.getElementById('clipStart').value='';
  document.getElementById('clipEnd').value='';
  if(probeTimer)clearTimeout(probeTimer);
  const url=inp.value.trim();
  // Only probe things that plausibly are URLs. A pasted link probes right
  // away; the debounce is only there so hand-typing doesn't probe every
  // keystroke.
  // (No backslashes here: this JS lives inside a Python string literal,
  //  where escapes like a backslash-slash would be invalid sequences.)
  const hasProto=/^https?:/i.test(url)&&url.indexOf('://')>0;
  const dot=url.indexOf('.');
  const bareHost=!hasProto&&dot>0&&dot<url.length-1&&url.indexOf(' ')===-1&&url.indexOf('/')>0;
  if(hasProto||bareHost){
    probeTimer=setTimeout(()=>runProbe(url),justPasted?0:350);
  }
  justPasted=false;
});
inp.addEventListener('keydown',e=>{if(e.key==='Enter'&&!gb.disabled)go();});
function updateNote(){const mix=/[?&]list=RD/i.test(inp.value);note.textContent=mix?(state.playlist?T('mixWarn'):T('mixInfo')):h264Note();}
// H.264 is the one chip that promises a codec, so it is the only one that
// can quietly hand back less than was asked for: sites stop making H.264
// above 1080p and serve VP9 or AV1 instead. Finding that out only after the
// file refuses to open in an editor is the whole reason this line exists.
function h264Note(){
  if(state.mode!=='video'||state.cont!=='mp4h264'||!probeInfo||probeInfo.kind!=='video')return'';
  const cap=probeInfo.h264Max||0;
  // Nothing here declares H.264. Two very different situations wear that
  // same face, and saying nothing was wrong for both. If the site declares
  // no codecs at all, the chain will go looking and usually finds real
  // H.264 — worth a quiet word, not an alarm. If it does declare its
  // codecs and none of them is H.264, then the last rungs will hand back
  // whatever sits in an mp4, which can be AV1, under a filename that says
  // h264. That one is the alarm.
  if(!cap)return T(probeInfo.h264Unsure?'h264Unsure':'h264None');
  let want=0;
  if(state.vq==='best'){const q=probeInfo.qualities||{};for(const k in q){if(q[k].ok)want=Math.max(want,+k);}}
  else want=+QMAP[state.vq]||0;
  return (want&&want>cap)?T('h264Cap').replace('{h}',cap):'';
}
function pick(btn){const g=btn.dataset.g;state[g]=btn.dataset.v;document.querySelectorAll('[data-g="'+g+'"]').forEach(b=>b.classList.remove('on','mode-on'));btn.classList.add(g==='mode'?'mode-on':'on');btn.classList.remove('pop');void btn.offsetWidth;btn.classList.add('pop');updateNote();}
function toggleFlag(key,btn){state[key]=!state[key];btn.classList.toggle('tog-on',state[key]);btn.classList.remove('pop');void btn.offsetWidth;btn.classList.add('pop');if(key==='playlist')updateNote();}
function setMode(m){const vr=document.getElementById('vrows'),ar=document.getElementById('arows');if(m==='audio'){requestAnimationFrame(()=>{vr.classList.add('hide');vr.style.maxHeight='0'});ar.classList.remove('hide');ar.style.maxHeight='200px';ar.style.opacity='1';}else{requestAnimationFrame(()=>{ar.classList.add('hide');ar.style.maxHeight='0'});vr.classList.remove('hide');vr.style.maxHeight='240px';vr.style.opacity='1';}
  // Audio clips are always cut exactly (re-encode is free there), so the
  // lossless toggle would be a dead control — hide it in audio mode
  document.getElementById('clipllbtn').style.display=(m==='audio')?'none':'';}
// The link is the bottleneck, so downloads run one at a time. Pressing
// Download while one is busy adds the next link to the queue instead of
// being locked out; the panel below shows every entry with its own bar.
function go(){const url=inp.value.trim();if(!url||gb.disabled)return;const dir=document.getElementById('dir').value.trim();const clipStart=document.getElementById('clipStart').value.trim();const clipEnd=document.getElementById('clipEnd').value.trim();pw.classList.add('show');pt.textContent=T('connecting');pt.style.color='rgba(var(--accent),0.5)';fetch('/download',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url,...state,dir,clipStart,clipEnd})}).then(r=>r.json()).then(()=>{inp.value='';inp.dispatchEvent(new Event('input'));refresh();}).catch(()=>{pt.textContent=T('connError');pt.style.color='rgba(255,100,80,0.8)';});}
function cancelJob(){if(!jobId)return;stopbtn.classList.remove('show');pt.textContent=T('stopping');pt.style.color='rgba(255,150,90,0.9)';fetch('/cancel/'+jobId,{method:'POST',headers:{'X-Aevum':'1'}}).then(()=>refresh());}
// Same endpoint for a job that has not started: the server retires it
// without a process to kill, so it just leaves the queue.
function dropJob(id){fetch('/cancel/'+id,{method:'POST',headers:{'X-Aevum':'1'}}).then(()=>refresh());}
function startPolling(){if(!pollTimer)pollTimer=setInterval(refresh,700);}
function stopPolling(){if(pollTimer){clearInterval(pollTimer);pollTimer=null;}}
// One poll drives everything: the main bar follows whichever job is
// running, and the panel below lists the queue and the finished rows.
function refresh(){fetch('/jobs').then(r=>r.json()).then(d=>{
  renderPanel(d);
  // Own the timer here, not at the call sites: reloading the page mid-queue
  // used to leave the panel frozen on its first snapshot, because only the
  // Download button ever started polling.
  if(d.active.length)startPolling();else stopPolling();
  const run=d.active.find(j=>j.code!=='queued');
  if(run){
    jobId=run.id;stopbtn.classList.add('show');pw.classList.add('show');
    // Clip downloads run silently through ffmpeg (no percent). Show an
    // indeterminate animated bar until real progress arrives.
    const indet=(run.code==='clip'&&(!run.progress||run.progress<=0));
    pf.classList.toggle('indet',indet);
    if(!indet)pf.style.width=(run.progress||0)+'%';
    pt.textContent=statusText(run);
    pt.style.color=(run.code==='process')?'rgba(var(--accent),0.6)':'rgba(var(--accent),0.5)';
  }else if(d.active.length){
    // Nothing running yet, but links are waiting their turn.
    jobId=null;stopbtn.classList.remove('show');pf.classList.remove('indet');
    pf.style.width='0%';pt.textContent=T('queued');pt.style.color='rgba(255,255,255,0.35)';
  }else{
    jobId=null;stopbtn.classList.remove('show');pf.classList.remove('indet');
    if(d.last){
      pt.textContent=statusText(d.last);
      if(d.last.success){pf.style.width='100%';pt.style.color='rgba(var(--accent),0.7)';}
      else if(d.last.code==='stopped'){pf.style.width='0%';pt.style.color='rgba(255,150,90,0.9)';}
      else{pt.style.color='rgba(255,100,80,0.8)';}
    }
  }
}).catch(()=>{});}
function esc(s){return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function qid(j){return String(j.id).replace(/[^a-z0-9]/gi,'');}
function qBits(j){
  const bits=[];
  if(j.item)bits.push('['+j.item+']');
  if(j.code==='queued'){bits.push(T('queued'));}
  else{
    bits.push((j.progress||0)+'%');
    if(j.total)bits.push(j.total);
    if(j.speed)bits.push(j.speed+' MB/s');
    if(j.eta)bits.push('ETA '+j.eta);
  }
  return bits.join(' · ');
}
let qSig='',hSig='';
function renderPanel(d){
  const qw=document.getElementById('queue-list'),hw=document.getElementById('hist-list');
  if(!d.active.length&&!d.past.length){document.getElementById('hist-wrap').style.display='none';qSig=hSig='';return;}
  document.getElementById('hist-wrap').style.display='block';
  // Rebuild only when the rows themselves change. Rewriting the markup on
  // every poll handed each bar a brand new element, and a fresh element is
  // born at its final width with its shimmer restarted — that is why the row
  // bar stepped along while the main one glided. Steady state now just moves
  // the width, so the transition and the shimmer both survive.
  const sig=d.active.map(j=>j.id+(j.code==='queued'?':q':':r')).join(',');
  if(sig!==qSig){
    qSig=sig;
    qw.innerHTML=d.active.map(j=>{
      const wait=(j.code==='queued');
      // Job ids are hex; strip anything else rather than trust it in markup.
      // The remove button carries the id and a delegated listener reads it —
      // an inline onclick would need a quote escape, and HTML is not a raw
      // Python string, so the backslash would be eaten before the browser.
      const id=qid(j);
      const bar=wait?'':'<div class="q-bar"><div class="q-fill" style="width:'+(j.progress||0)+'%"></div></div>';
      return '<div class="q-item" data-row="'+id+'"><div class="q-top"><span class="q-name'+(wait?' wait':'')+'">'+esc(j.title||j.url)+'</span><span class="q-stat'+(wait?' wait':'')+'">'+esc(qBits(j))+'</span><button class="q-x" data-drop="'+id+'" title="'+esc(T('qRemove'))+'">×</button></div>'+bar+'</div>';
    }).join('');
  }else{
    d.active.forEach(j=>{
      const row=qw.querySelector('[data-row="'+qid(j)+'"]');
      if(!row)return;
      row.querySelector('.q-name').textContent=j.title||j.url;
      row.querySelector('.q-stat').textContent=qBits(j);
      const fill=row.querySelector('.q-fill');
      if(fill)fill.style.width=(j.progress||0)+'%';
    });
  }
  // Finished rows only change when something finishes, so leave them alone
  // in between instead of rewriting the list 85 times a minute.
  const hsig=d.past.map(h=>(h.title||h.url)+h.success).join(',');
  if(hsig!==hSig){
    hSig=hsig;
    hw.innerHTML=d.past.map(h=>'<div class="hist-item"><span class="hist-url">'+esc(h.title||h.url)+'</span><span class="hist-meta">'+esc(h.meta)+'</span><span class="'+(h.success?'hist-ok':'hist-err')+'">'+(h.success?'✓':'✗')+'</span></div>').join('');
  }
}
document.getElementById('queue-list').addEventListener('click',e=>{
  const b=e.target.closest('[data-drop]');
  if(b)dropJob(b.getAttribute('data-drop'));
});
applyTheme(curTheme);
applyLang(curLang);
loadSettings();
refresh();
// Deep-link support: /?u=<video url> pre-fills the box and triggers the
// preview probe (also the future hand-off point for a browser extension)
(function(){
  const preUrl=new URLSearchParams(location.search).get('u');
  if(preUrl){inp.value=preUrl;inp.dispatchEvent(new Event('input'));}
})();
// ── liveness signal: on Linux the app quits once the tab closes ──
const CID=Math.random().toString(36).slice(2)+Date.now().toString(36);
let pingFails=0;
function showDead(){if(document.getElementById('deadbar'))return;const b=document.createElement('div');b.id='deadbar';b.textContent=T('appClosed');b.style.cssText='position:fixed;top:14px;left:50%;transform:translateX(-50%);z-index:99;background:rgba(40,12,10,0.95);border:1px solid rgba(255,100,80,0.45);color:rgba(255,150,130,0.95);font-family:inherit;font-size:11px;padding:9px 16px;border-radius:9px;box-shadow:0 10px 30px -8px rgba(0,0,0,0.8)';document.body.appendChild(b);}
setInterval(()=>{fetch('/ping?id='+CID,{cache:'no-store'}).then(()=>{pingFails=0;const b=document.getElementById('deadbar');if(b)b.remove();}).catch(()=>{if(++pingFails>=4)showDead();});},3000);
// announce the tab closing (fast clean exit; after a reload the new page reconnects right away)
window.addEventListener('pagehide',()=>{try{navigator.sendBeacon('/bye?id='+CID);}catch(e){}});
// show the settings panel on first launch (once)
if(!localStorage.getItem('aevum_onboarded')){setTimeout(()=>settingsPanel.classList.add('open'),700);localStorage.setItem('aevum_onboarded','1');}
</script>
</body>
</html>"""


HEIGHT_MAP = {"4k": "2160", "1440p": "1440", "1080p": "1080",
              "720p": "720", "480p": "480", "360p": "360"}

# Only H.264 needs naming in the filename. It writes .mp4 files, exactly
# like the MP4 button, so without a tag the two land on one name and the
# second download is skipped as already there. MKV and WebM are already
# told apart by their extension and are left to say it themselves.
CONT_TAG = {"mp4h264": "h264"}

MIX_CAP = 50  # YouTube Mix/radio playlists are endless; cap them here


def playlist_id(url: str) -> str:
    try:
        return (parse_qs(urlparse(url).query).get("list", [""])[0]) or ""
    except Exception:
        return ""


def is_mix_playlist(url: str) -> bool:
    # YouTube Mix/radio playlist ids start with "RD" (RDMM, RDCLAK, RDEM...)
    return playlist_id(url).startswith("RD")


_VIMEO_PLAIN = re.compile(r"^https?://(?:www\.)?vimeo\.com/(\d+)/?(?:[?#].*)?$", re.I)


def normalize_url(url: str) -> str:
    """Rewrite links yt-dlp currently cannot open into ones it can.

    Vimeo revoked the anonymous OAuth tokens yt-dlp signs in with, so every
    plain vimeo.com/<id> comes back 401 Unauthorized while the very same
    video opens through its player URL (yt-dlp issue #17271). Only the bare
    numeric form is rewritten — channel, showcase and unlisted links carry
    parts of the path that the player URL would throw away.
    """
    m = _VIMEO_PLAIN.match((url or "").strip())
    return f"https://player.vimeo.com/video/{m.group(1)}" if m else url


def kill_process_tree(pid: int):
    try:
        if sys.platform == "win32":
            # Bounded, because this runs on the Stop button and on the way
            # out. taskkill can sit there when the target is stuck in a
            # driver call, and without a limit it takes the quit path with
            # it — the except below turns a timeout into a shrug.
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           creationflags=subprocess.CREATE_NO_WINDOW,
                           capture_output=True, timeout=15)
        else:
            os.killpg(os.getpgid(pid), 15)
    except Exception:
        pass


def build_video_format(vq: str, container: str, mute: bool) -> str:
    """Site-agnostic format selector. Falls back to muxed streams where split ones aren't offered.

    Clips use the same selectors as full downloads. An earlier version pushed
    progressive (pre-muxed) streams first for clips because split sections
    seemed dramatically slower — the real culprit turned out to be the
    bundled ffmpeg 8.1.x hanging on googlevideo range requests (see
    research-clip.md). With a good ffmpeg, split sections seek by range in
    seconds, and progressive streams (usually 360p only) would just cost
    quality.
    """
    # No size filter here at all — build_format_sort() decides the size.
    #
    # A filter cannot say what "1080p" means. [height<=1080] reads as 1080p
    # only while the video is wider than it is tall. On a 1080x1920 short it
    # matches the 480x854 rung instead, because 854 is under 1080, and the
    # user who asked for 1080p is handed 480p without a word. Adding a width
    # rung after it did not help: the height rung already matched, so the
    # chain stopped there. Measured on a real vertical video — 1080p gave
    # 480x854 and 720p gave 360x640.
    #
    # yt-dlp's "res" sort key is the smaller of the two dimensions, which is
    # exactly what a person means by 1080p, on either orientation. So the
    # chains below choose the codec and the container, and the sort chooses
    # the size.
    def chain(*rungs, tail="best"):
        """Rungs in order of preference, then one unfiltered fallback."""
        return "/".join([r.replace("{c}", "") for r in rungs] + [tail])

    if mute:
        # video only
        return chain("bestvideo{c}", "best{c}")
    if container == "mp4":
        # A container choice, not a codec one. The avc1 tie-break lives in
        # build_format_sort() instead, so 1080p lands on H.264 without
        # capping 4K, where AV1 is all the sites make.
        return chain("bestvideo{c}[ext=mp4]+bestaudio[ext=m4a]",
                     "bestvideo{c}+bestaudio",
                     "best{c}")
    if container == "mp4h264":
        # The only option that promises a codec, so every rung has to keep
        # that promise. An [ext=mp4] rung used to sit second in this chain
        # and let VP9 and AV1 straight through — the container name says
        # nothing about what is inside it.
        # The last two rungs are for sites that ship H.264 without saying so:
        # Instagram's progressive streams really are avc1, but report both
        # their codec and their height as unknown, so every filtered rung
        # above steps over them.
        return chain("bestvideo{c}[vcodec~='^(avc1|h264)']+bestaudio[ext=m4a]",
                     "bestvideo{c}[vcodec~='^(avc1|h264)']+bestaudio",
                     "best{c}[vcodec~='^(avc1|h264)']",
                     "best{c}[ext=mp4]",
                     tail="best[ext=mp4]")
    if container == "webm":
        # user preference: VP9 quality; webm-native streams first.
        # Both spellings matter: YouTube reports "vp9", while sites serving
        # VP9 inside an mp4 report "vp09.00.31..." and the old ^=vp9 test
        # quietly missed every one of them.
        # Webm-legal pairings first: a webm holds VP8/VP9/AV1 beside Opus or
        # Vorbis and nothing else, and asking ffmpeg for anything wider ends
        # the merge on "Conversion failed!". The rungs below that one are for
        # sites carrying no such pairing — Vimeo's HLS is H.264 and AAC all
        # the way down, and it publishes video and audio separately, so a
        # chain ending at "best" found nothing at all and the download died.
        # build_cmd names mkv as the second container, which is what those
        # last two rungs land in.
        return chain("bestvideo{c}[ext=webm]+bestaudio[ext=webm]",
                     "bestvideo{c}[vcodec~='^vp0?9']+bestaudio[acodec~='^(opus|vorbis)']",
                     "bestvideo{c}[vcodec~='^vp0?9']+bestaudio",
                     "bestvideo{c}+bestaudio",
                     "best{c}")
    return chain("bestvideo{c}+bestaudio", "best{c}")


def build_format_sort(vq: str, container: str) -> str:
    """Which stream wins, once the chains have said which ones are eligible.

    "res" is yt-dlp's name for the smaller of a stream's two dimensions,
    so res:1080 means 1080p to a viewer whichever way the video is turned —
    1920x1080 and 1080x1920 both qualify. Giving it a value asks for the
    closest match at or below it, which is what a quality chip means.

    MP4 adds a codec tie-break after that. It has to come second: put the
    codec first and a 4K download settles for the 1080p H.264 stream,
    because H.264 is where the sites stop.
    """
    h = HEIGHT_MAP.get(vq)
    # Size always leads, even with no cap to aim at. "Best" has no entry in
    # HEIGHT_MAP, so this list used to start with the codec on that one
    # setting — and yt-dlp puts user keys ahead of its own, which made H.264
    # outrank resolution and handed Best a 1080p stream off a 4K video. The
    # bare "res" sorts by size without asking for a particular one.
    keys = [f"res:{h}" if h else "res"]
    if container == "mp4":
        keys.append("vcodec:avc1")
    return ",".join(keys)


def _parse_timestamp(text: str):
    """'90', '1:30' or '1:02:03' -> seconds; None when empty or malformed."""
    text = (text or "").strip()
    if not text:
        return None
    parts = text.split(":")
    if len(parts) > 3 or not all(p.isdigit() and p != "" for p in parts):
        return None
    seconds = 0
    for p in parts:
        seconds = seconds * 60 + int(p)
    return seconds


def build_cmd(data: dict, output_dir: str) -> list:
    url  = normalize_url(data["url"])
    mode = data.get("mode", "video")
    is_playlist = bool(data.get("playlist"))
    want_thumb = bool(data.get("thumb"))

    # The same video at two qualities, or as MP4 and then as H.264, used to
    # land on one filename. yt-dlp saw the file already sitting there,
    # skipped it and exited cleanly, so the app reported success while the
    # first file stayed where it was. Height and button name keep them
    # apart. The height is written conditionally because sites that report
    # none — Instagram's progressive streams for one — would otherwise get
    # "NAp" glued to every filename. Audio carries neither.
    if mode == "video":
        tag = CONT_TAG.get(data.get("cont", "mp4"), "")
        vtag = "%(height& {}p|)s" + (f" {tag}" if tag else "")
    else:
        vtag = ""
    # Two sections of one video are two different files, and without the
    # range in the name they are one: the second download finds the first
    # already there, skips it, and reports success. Same for the muted cut,
    # which is a different file again.
    cs = _parse_timestamp(data.get("clipStart", ""))
    ce = _parse_timestamp(data.get("clipEnd", ""))
    # Same correction build_cmd applies further down: an end at or before
    # the start is dropped. Without it here the name promised a range the
    # file does not contain — "60-30" on a file that runs 60s to the end.
    if ce is not None and cs is not None and ce <= cs:
        ce = None
    if mode == "video" and (cs is not None or ce is not None):
        vtag += f" {cs if cs is not None else 0}-{ce if ce is not None else 'end'}"
    if mode == "video" and data.get("mute"):
        vtag += " mute"

    if is_playlist:
        # Create a subfolder named after the playlist, number the files inside
        out = os.path.join(output_dir,
                           "%(playlist_title|Playlist)s",
                           f"%(autonumber)03d - %(title).150B [%(id)s]{vtag}.%(ext)s")
    elif want_thumb:
        # Thumbnail selected: give this download its own folder so the video
        # and its cover image don't clutter the main folder alongside others.
        # The folder stays unsuffixed so two versions of one video share it.
        # This is the one layout that spends the title twice, once on the
        # folder and again on the file, so a long title used to push the
        # whole path past the 260 characters Windows will open — the
        # download failed on a path nobody could see. Half the budget each
        # keeps the worst case inside it.
        out = os.path.join(output_dir,
                           "%(title).90B [%(id)s]",
                           f"%(title).90B [%(id)s]{vtag}.%(ext)s")
    else:
        out = os.path.join(output_dir, f"%(title).180B [%(id)s]{vtag}.%(ext)s")

    cmd = [YTDLP, "--newline", "--add-metadata", "--no-mtime",
           "--retries", "10", "--fragment-retries", "10",
           "--concurrent-fragments", "4", "-o", out]

    # Use the bundled ffmpeg (needed for merging, audio extraction, subtitle embedding)
    if FFMPEG_DIR:
        cmd += ["--ffmpeg-location", FFMPEG_DIR]

    # Playlist: grab the whole list, don't stop on one broken video
    if is_playlist:
        cmd += ["--yes-playlist", "--ignore-errors"]
        # Mix/radio lists are endless, so keep a sane cap
        if is_mix_playlist(url):
            cmd += ["--playlist-end", str(MIX_CAP)]
    else:
        cmd.append("--no-playlist")

    # Browser cookies (for sites that require a login)
    cookies = data.get("cookies", "none")
    if cookies and cookies != "none":
        cmd += ["--cookies-from-browser", cookies]

    # Save the video's thumbnail alongside the download (as jpg)
    if want_thumb:
        cmd += ["--write-thumbnail", "--convert-thumbnails", "jpg"]

    # Clip: download only a section (for grabbing short scenes to edit).
    # Invalid/empty fields simply mean "no bound on that side".
    clip_start = _parse_timestamp(data.get("clipStart", ""))
    clip_end   = _parse_timestamp(data.get("clipEnd", ""))
    if clip_end is not None and clip_start is not None and clip_end <= clip_start:
        clip_end = None  # nonsensical range: keep the start, drop the end
    is_clip_dl = clip_start is not None or clip_end is not None
    # "Lossless cut" keeps the original stream bytes (no re-encode) but the
    # cut snaps back to the nearest keyframe — the clip can start a few
    # seconds early. Audio clips ignore it: an audio re-encode is free and
    # a copy cut drags in up to ~10 s of extra sound (webm cluster snap).
    clip_lossless = bool(data.get("clipLossless")) and mode == "video"
    if is_clip_dl:
        section = "*%s-%s" % (clip_start or 0,
                              clip_end if clip_end is not None else "inf")
        cmd += ["--download-sections", section]
        # --force-keyframes-at-cuts makes the cut EXACT by re-encoding the
        # section. On its own ffmpeg picks the DEFAULT encoder — for webm
        # that is libvpx-vp9 at ~0.2x realtime, i.e. hours for a long clip —
        # so the video branch below pins a fast encoder that keeps up with
        # the download (measured: exact cut at the same wall time as a
        # stream copy; see research-clip.md).
        if not clip_lossless:
            cmd += ["--force-keyframes-at-cuts"]

    if mode == "video":
        vq   = data.get("vq", "1080p")
        cont = data.get("cont", "mp4")
        mute = bool(data.get("mute", False))
        fmt  = build_video_format(vq, cont, mute)
        sort = build_format_sort(vq, cont)
        # UI values -> real container names (mp4h264 is an mp4 on disk)
        merge_container = {"mp4h264": "mp4"}.get(cont, cont)
        # Naming a second container lets yt-dlp keep the streams it found and
        # pick something that fits them. Only webm needs it: it accepts one
        # narrow set of codecs, and on sites offering nothing from that set
        # the merge used to fail outright rather than fall back. mkv takes
        # anything. merge_container stays the plain name below, because the
        # subtitle format and the clip encoder both branch on it.
        merge_arg = "webm/mkv" if merge_container == "webm" else merge_container
        cmd += ["-f", fmt, "--merge-output-format", merge_arg]
        if sort:
            cmd += ["-S", sort]
        if is_clip_dl and not clip_lossless:
            # Fast encoders for the exact-cut re-encode, matched to the
            # target container. libvpx realtime mode is ~35x faster than
            # its default and x264 veryfast runs ~12x realtime — both
            # outpace the download, so the exact cut adds no wall time.
            if merge_container == "webm":
                enc = "-c:v libvpx-vp9 -deadline realtime -cpu-used 8 -row-mt 1 -b:v 2M -c:a libopus"
            else:
                enc = "-c:v libx264 -preset veryfast -crf 22 -c:a aac"
            cmd += ["--downloader-args", "ffmpeg:" + enc]
        if data.get("subs"):
            # Subtitles are best-effort. YouTube's subtitle endpoint often
            # returns HTTP 429; --ignore-errors keeps that from aborting the
            # whole download (the video still lands, run_job flags the skip).
            # Uploader-provided subtitles ONLY — auto captions are noise the
            # user explicitly does not want, so --write-auto-subs stays out.
            # The wildcard still catches regional variants (en-GB etc.).
            # The subtitle format must match the container — webm only takes
            # WebVTT (srt embeds used to fail silently there) — and
            # no-keep-subs drops the sidecar file once the embed succeeds.
            sub_fmt = "vtt" if merge_container == "webm" else "srt"
            cmd += ["--embed-subs", "--write-subs",
                    "--sub-langs", "tr.*,en.*",
                    "--convert-subs", sub_fmt,
                    "--compat-options", "no-keep-subs", "--ignore-errors"]
        cmd.append(url)
        return cmd

    # ── audio mode ──
    afmt = data.get("fmt", "mp3")
    br   = data.get("br", "192k")
    cmd += ["-x", "--audio-format", afmt]
    # Bitrate makes no sense for lossless formats; only apply it to lossy ones
    if afmt not in ("flac", "wav"):
        cmd += ["--audio-quality", "0" if br == "best" else br.upper()]
    cmd.append(url)
    return cmd


def history_meta(data: dict) -> str:
    is_clip = (_parse_timestamp(data.get("clipStart", "")) is not None or
               _parse_timestamp(data.get("clipEnd", "")) is not None)
    if data.get("mode") == "video":
        cont_label = {"mp4h264": "mp4·h264"}.get(data.get("cont", ""), data.get("cont", ""))
        parts = [data.get("vq", ""), cont_label]
        if data.get("mute"):
            parts.append("muted")
        if data.get("subs"):
            parts.append("subs")
        if is_clip:
            parts.append("clip")
        return " ".join(p for p in parts if p)
    meta = f"{data.get('fmt','')} {data.get('br','')}".strip()
    return meta + (" clip" if is_clip else "")


_FFMPEG_TIME_RE = re.compile(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)")
_DL_SPEED_RE = re.compile(r"at\s+([\d.]+)(K|M|G)iB/s")
# Same yt-dlp progress line the speed regex reads:
#   [download]  45.2% of  123.45MiB at  4.90MiB/s ETA 00:18
# The total can be prefixed with "~" when yt-dlp is only estimating it.
_DL_ETA_RE = re.compile(r"ETA\s+((?:\d+:)?\d+:\d+)")
_DL_TOTAL_RE = re.compile(r"of\s+~?\s*([\d.]+)(K|M|G)iB")
# ffmpeg progress lines carry no rate, but their growing size= lets us
# compute one (clip downloads run through ffmpeg, not yt-dlp's reporter)
_FF_SIZE_RE = re.compile(r"size=\s*(\d+)(KiB|kB)")

# Failures worth simply running again. YouTube hands out a media URL and then
# refuses it, sometimes before the first byte and sometimes halfway through,
# and the refusal belongs to that URL: a second run extracts a fresh one and
# usually walks straight through. Measured on the version that shipped in
# 1.2.5: 29% of single attempts failed, and 0 of 10 downloads failed when
# allowed three. yt-dlp itself will not do this — a 403 means "this URL is
# void", so it gives up rather than hammer a dead link, and it is right to.
# Retrying is our job because we can afford a whole new extraction.
#
# Only failures that a new attempt could plausibly fix belong here. A private
# video, a refused format, a bad link: those are final, and putting them in
# this list would only make a hopeless download take three times as long to
# admit it. 429 is left out on purpose — being told to slow down is not an
# invitation to try again two seconds later.
_RETRYABLE_RE = re.compile(
    r"HTTP Error (?:403|408|5\d\d)"
    r"|Connection reset|Read timed out|timed out|Connection aborted"
    r"|Remote end closed connection|IncompleteRead|Unable to download webpage",
    re.I)
_MAX_ATTEMPTS = 3
_RETRY_PAUSE = 2.0


def _clip_duration_seconds(data: dict):
    """Length of the requested clip in seconds, or None if not a bounded clip."""
    start = _parse_timestamp(data.get("clipStart", ""))
    end = _parse_timestamp(data.get("clipEnd", ""))
    if end is not None and start is not None and end > start:
        return end - start
    if end is not None and (start is None or start == 0):
        return end
    return None


def _short_and_long(path: str):
    """The two dimensions of a finished file, smaller first. (0, 0) if unread.

    Nothing before this point knows them. A template can only write
    %(height)s, which is the long side of a vertical video, and sites that
    report no height at all — Instagram's progressive copies — leave it
    blank. The finished file always knows, and ffprobe ships with us now.
    """
    try:
        r = subprocess.run([FFPROBE, "-v", "error", "-select_streams", "v:0",
                            "-show_entries", "stream=width,height",
                            "-of", "csv=p=0:s=x", path],
                           capture_output=True, text=True, timeout=20,
                           env=_clean_env(),
                           creationflags=subprocess.CREATE_NO_WINDOW if _IS_WINDOWS else 0)
        w, _, h = (r.stdout or "").strip().partition("x")
        if w.isdigit() and h.isdigit():
            return min(int(w), int(h)), max(int(w), int(h))
    except Exception:
        pass
    return 0, 0


_VIDEO_EXT = (".mp4", ".mkv", ".webm", ".mov", ".avi", ".flv")


def _file_just_written(output_dir: str, since: float) -> str:
    """The video this job produced, found by looking rather than by parsing.

    Reading the path out of yt-dlp's own output does not survive a title
    with a character the console cannot spell: by the time the line reaches
    us the character is already a question mark, and no amount of decoding
    brings it back. Forcing UTF-8 on the child does not help either — it is
    mangled before it is written. The folder, on the other hand, holds the
    real name. Downloads run one at a time, so the newest video file
    written since this job started is this job's file.
    """
    # One level down and no further. The templates put a file either in the
    # download folder or in a single folder of their own — thumbnail and
    # playlist downloads — so anything deeper belongs to somebody else, and
    # walking a whole archive tree to find our own file is work nobody
    # asked for. Measured at 1 ms on a normal folder against nearly three
    # seconds on a tree of sixty thousand files.
    best, best_t = "", since - 1

    def scan(d, depth):
        nonlocal best, best_t
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        if e.is_dir():
                            if depth:
                                scan(e.path, depth - 1)
                        elif e.name.lower().endswith(_VIDEO_EXT):
                            # stat() here comes from the directory listing
                            # itself on Windows, so it costs nothing extra.
                            t = e.stat().st_mtime
                            if t >= since and t > best_t:
                                best, best_t = e.path, t
                    except OSError:
                        continue
        except OSError:
            pass

    scan(output_dir, 1)
    return best


def _fix_quality_in_name(path: str) -> str:
    """Rewrite the size in the filename to the short side, once it is known.

    The download itself has to run under a name yt-dlp can tell apart from
    the last one, or it skips the file and calls that success — which is
    why the template still writes the height. This puts the honest number
    in afterwards: 1080p for a 1080x1920 short, matching both the chip that
    was clicked and the quality the sort selected.
    """
    if not path or not os.path.isfile(path):
        return path
    short, long_ = _short_and_long(path)
    if not short or short == long_:
        return path
    # Everything below works on the filename alone, and only on the part
    # after the id. Searching the whole path found the folder's own id in
    # the thumbnail layout — which builds a directory that does not exist —
    # and a plain search-and-replace rewrote titles: a video called
    # "Epic 1920p Edit" had its title edited instead of its tag.
    folder, name = os.path.split(path)
    head, ext = os.path.splitext(name)
    # The last bracket, not the first: a title carrying its own "[Official]"
    # would otherwise be taken for the id and the tag written into the middle
    # of it. The template puts [%(id)s] after the title and the vtag has no
    # brackets in it, so the id is always the one at the end.
    ids = list(re.finditer(r"\[[A-Za-z0-9_-]{6,}\]", head))
    if not ids:
        return path
    m = ids[-1]
    want, tail = f" {short}p", head[m.end():]
    tail = (re.sub(r"^ \d+p", want, tail, count=1)
            if re.match(r"^ \d+p", tail) else want + tail)
    new = os.path.join(folder, head[:m.end()] + tail + ext)
    if new == path or os.path.exists(new):
        # Something is already there — two chips can land on one stream, so
        # this is usually the same file twice. Either way it is not ours to
        # delete; leaving the download under its own name loses nothing.
        return path
    try:
        os.replace(path, new)
        return new
    except OSError:
        return path


def _record_history(job_id: str, data: dict, output_dir: str, success: bool):
    with jobs_lock:
        title = jobs[job_id].get("title", "")
    with history_lock:
        download_history.insert(0, {
            "url": data["url"], "title": title, "meta": history_meta(data),
            "dir": output_dir, "success": success,
        })
        # the page shows 20 entries; don't hoard the rest forever
        del download_history[20:]


def _fail_job(job_id: str, data: dict, output_dir: str, error_line: str):
    """A job that died before yt-dlp could report anything itself.

    It still has to leave the queue, land in the list as a failed row, and
    carry a finish time — without one it would sort as the oldest result and
    the bar would keep showing some earlier success instead of this error.
    "started" matters for the same reason: it separates a job that was
    attempted from one the user dropped out of the queue, and only attempted
    jobs count as a result. A failure this early is still an attempt.
    """
    with jobs_lock:
        jobs[job_id].update({"done": True, "success": False, "code": "error",
                             "started": True, "progress": 0, "speed": "", "eta": "",
                             "finished_at": time.time(), "error_line": error_line})
    _record_history(job_id, data, output_dir, False)


def run_job(job_id: str, data: dict, output_dir: str):
    # Everything runs inside the try: a job that raises before it is marked
    # done would sit in the list as a phantom "downloading" row forever, and
    # on Linux the watchdog would never let the app exit.
    try:
        cmd = build_cmd(data, output_dir)
        clip_dur = _clip_duration_seconds(data)
        # A section download runs through ffmpeg, which stays SILENT (no percent
        # lines) for the whole transfer. Mark the job so the UI shows an
        # indeterminate "downloading clip" state instead of a frozen 0%.
        is_clip = (_parse_timestamp(data.get("clipStart", "")) is not None or
                   _parse_timestamp(data.get("clipEnd", "")) is not None)
        with jobs_lock:
            jobs[job_id]["lines"].append("$ " + " ".join(cmd))
            jobs[job_id]["started"] = True
            if is_clip:
                jobs[job_id]["code"] = "clip"
        progress, code, item = 0, ("clip" if is_clip else "download"), ""
        want_subs = bool(data.get("subs")) and data.get("mode", "video") == "video"
        mode = data.get("mode", "video")
        started_at = time.time()
        # Three tries, because YouTube's refusals are per-URL and a fresh
        # extraction usually gets a URL it will honour (see _RETRYABLE_RE).
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            subs_embedded = False
            ff_last_bytes, ff_last_t = None, 0.0
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, encoding="utf-8", errors="replace", bufsize=1,
                                    env=_clean_env(),
                                    # POSIX: own process group, else kill_process_tree's
                                    # killpg would hit Aevum's group and take the app down
                                    start_new_session=sys.platform != "win32",
                                    creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
            with jobs_lock:
                jobs[job_id]["proc"] = proc
                # Stop pressed in the gap between leaving the queue and getting a
                # process: there was nothing to kill then, so kill it now.
                already_cancelled = jobs[job_id].get("cancelled", False)
            if already_cancelled:
                kill_process_tree(proc.pid)
            for line in proc.stdout:
                line = line.rstrip()
                if not line:
                    continue
                if "Downloading item" in line:
                    # yt-dlp: "[download] Downloading item 3 of 28"
                    try:
                        seg = line.split("Downloading item", 1)[1].split()
                        item = f"{seg[0]}/{seg[2]}"
                    except IndexError:
                        pass
                if "[download]" in line and "%" in line:
                    try:
                        pct = float(line.split("%")[0].split()[-1])
                        progress = min(int(pct), 99)
                        code = "download"
                    except (ValueError, IndexError):
                        pass
                    ms = _DL_SPEED_RE.search(line)
                    if ms:
                        val = float(ms.group(1)) * {"K": 1024, "M": 1024**2, "G": 1024**3}[ms.group(2)]
                        with jobs_lock:
                            jobs[job_id]["speed"] = f"{val / 1e6:.1f}"
                    me = _DL_ETA_RE.search(line)
                    if me:
                        with jobs_lock:
                            jobs[job_id]["eta"] = me.group(1)
                    mt = _DL_TOTAL_RE.search(line)
                    if mt:
                        tot = float(mt.group(1)) * {"K": 1024, "M": 1024**2, "G": 1024**3}[mt.group(2)]
                        with jobs_lock:
                            jobs[job_id]["total"] = (f"{tot / 1e9:.2f} GB" if tot >= 1e9
                                                     else f"{tot / 1e6:.1f} MB")
                if "Destination:" in line or "has already been downloaded" in line:
                    # No preview card was probed for this link (deep link, or
                    # Enter before the debounce fired), so take the name yt-dlp
                    # chose. "...f137.mp4" is one half of a split stream — the
                    # format suffix is not part of the title.
                    raw = (line.split("Destination:", 1)[1] if "Destination:" in line
                           else line.split("] ", 1)[-1].rsplit(" has already", 1)[0])
                    stem = re.sub(r"\.f\d+$", "", Path(raw.strip()).stem)
                    # Our own name template ends in " [<id>]" — drop it so this
                    # reads like the title the preview card would have supplied
                    stem = re.sub(r"\s*\[[A-Za-z0-9_-]{6,}\]$", "", stem).strip()
                    if stem:
                        with jobs_lock:
                            if not jobs[job_id].get("title"):
                                jobs[job_id]["title"] = stem[:200]
                if "[EmbedSubtitle]" in line and "Embedding" in line:
                    # Positive proof a subtitle track went into the file; the
                    # warning below only fires when this never happened.
                    subs_embedded = True
                if any(x in line for x in ["[Merger]", "[VideoConvertor]", "[ExtractAudio]",
                                              "[EmbedSubtitle]", "[Metadata]", "[FixupM"]):
                    progress, code = 94, "process"
                elif "time=" in line and code != "process":
                    # Section/clip downloads can run through ffmpeg, which reports
                    # elapsed "time=HH:MM:SS" instead of a percentage. Convert it
                    # against the clip length so the bar moves instead of freezing.
                    m = _FFMPEG_TIME_RE.search(line)
                    if m:
                        elapsed = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
                        if clip_dur and clip_dur > 0:
                            progress = min(int(elapsed / clip_dur * 100), 99)
                        else:
                            # Unknown length: at least prove it's working (creep up)
                            progress = min(progress + 1, 95)
                        code = "download"
                    # Rate from the growth of size= between progress lines.
                    # A ~3s window smooths ffmpeg's bursty writes, and a zero
                    # rate never overwrites the last real one (brief stalls
                    # would otherwise make the number flicker 0.0/1.8/0.0).
                    msz = _FF_SIZE_RE.search(line)
                    if msz:
                        cur = int(msz.group(1)) * (1024 if msz.group(2) == "KiB" else 1000)
                        now = time.monotonic()
                        if ff_last_bytes is None or cur < ff_last_bytes:
                            ff_last_bytes, ff_last_t = cur, now
                        elif now - ff_last_t >= 3.0:
                            rate = (cur - ff_last_bytes) / (now - ff_last_t) / 1e6
                            if rate >= 0.05:
                                with jobs_lock:
                                    jobs[job_id]["speed"] = f"{rate:.1f}"
                            ff_last_bytes, ff_last_t = cur, now
                with jobs_lock:
                    lines = jobs[job_id]["lines"]
                    lines.append(line)
                    # A big playlist emits tens of thousands of lines; the page
                    # never shows them and the error scan only needs the tail.
                    if len(lines) > 500:
                        cut = len(lines) - 400
                        del lines[:cut]
                        jobs[job_id]["read_idx"] = max(0, jobs[job_id]["read_idx"] - cut)
                    jobs[job_id]["progress"] = progress
                    jobs[job_id]["code"] = code
                    jobs[job_id]["item"] = item
            proc.wait()
            with jobs_lock:
                cancelled = jobs[job_id].get("cancelled", False)
            success = proc.returncode == 0 and not cancelled
            if success or cancelled or attempt == _MAX_ATTEMPTS:
                break
            # Only the tail matters. A playlist that lost one video to a 403
            # an hour ago and is failing now for its own reasons should not
            # be read as a retryable failure because that line is still in
            # the buffer.
            with jobs_lock:
                tail = jobs[job_id]["lines"][-40:]
            if not any(_RETRYABLE_RE.search(l) for l in tail):
                break
            with jobs_lock:
                jobs[job_id]["lines"].append(
                    "[aevum] attempt %d failed, trying again" % attempt)
                jobs[job_id].update({"progress": 0, "speed": "", "eta": "",
                                     "code": "clip" if is_clip else "download"})
            progress = 0
            # A breath between attempts, but Stop must not have to sit
            # through it: the button is the one thing that should always
            # feel immediate.
            waited = 0.0
            while waited < _RETRY_PAUSE:
                time.sleep(0.1)
                waited += 0.1
                with jobs_lock:
                    if jobs[job_id].get("cancelled", False):
                        break
            with jobs_lock:
                if jobs[job_id].get("cancelled", False):
                    cancelled, success = True, False
                    break
        if success and mode == "video" and not data.get("playlist"):
            # The template wrote the height, which is the long side of a
            # vertical video and blank where the site reported none. Now
            # that there is a file, ffprobe can say what it really is.
            _fix_quality_in_name(_file_just_written(output_dir, started_at))
        with jobs_lock:
            jobs[job_id].update({"done": True, "success": success,
                                 "progress": 100 if success else 0,
                                 "speed": "", "eta": "", "finished_at": time.time()})
            jobs[job_id]["code"] = "done" if success else ("stopped" if cancelled else "error")
            if success and want_subs and not subs_embedded:
                # Subs were asked for but nothing got embedded — no uploader
                # subtitles on this video, or the endpoint throttled us
                jobs[job_id]["subswarn"] = True
            if not success and not cancelled:
                errs = [l for l in jobs[job_id]["lines"]
                        if "ERROR" in l or "error:" in l.lower()]
                jobs[job_id]["error_line"] = errs[-1][:140] if errs else ""
                # From Chrome 127 the cookie key is bound to Chrome's own
                # binary, so nothing else can open the jar and yt-dlp can
                # only report "Failed to decrypt with DPAPI". Edge and Brave
                # are the same browser underneath and fail the same way. The
                # browser came out of our own menu, so name the one that
                # still works instead of passing the raw line through.
                if any("DPAPI" in l for l in jobs[job_id]["lines"]):
                    jobs[job_id]["cookieerr"] = True
                # yt-dlp says "Requested format is not available" and leaves
                # it there. The user picked a chip, not a format string, so
                # say it in those terms — the H.264 chip refuses rather than
                # hand back a codec it did not promise, and that refusal is
                # the message worth showing.
                if any("Requested format is not available" in l
                       for l in jobs[job_id]["lines"]):
                    jobs[job_id]["formaterr"] = True
                # Three attempts and the site still would not hand the file
                # over. One 403 is weather; three in a row is the sign that
                # the site has moved and the copy of yt-dlp in this build
                # does not know the new way yet. Raw "HTTP Error 403" tells
                # the user nothing they can act on, so say that instead.
                if any(_RETRYABLE_RE.search(l) for l in jobs[job_id]["lines"][-60:]):
                    jobs[job_id]["staleerr"] = True
        with jobs_lock:
            title = jobs[job_id].get("title", "")
        _record_history(job_id, data, output_dir, success)
    except FileNotFoundError:
        _fail_job(job_id, data, output_dir, "yt-dlp not found — reinstall Aevum")
    except Exception as e:
        _fail_job(job_id, data, output_dir, str(e)[:140])


@app.route("/")
def index():
    return render_template_string(HTML)


_IS_WINDOWS = sys.platform == "win32"


def _default_download_dir() -> str:
    # Linux/macOS: use the XDG downloads folder (else ~/Downloads)
    if not _IS_WINDOWS:
        try:
            out = subprocess.run(["xdg-user-dir", "DOWNLOAD"],
                                 capture_output=True, text=True, timeout=3)
            d = out.stdout.strip()
            if d and os.path.isdir(d):
                return d
        except (FileNotFoundError, OSError, subprocess.SubprocessError):
            pass
    return str(Path.home() / "Downloads")


# ── Probe: fetch metadata without downloading (preview card) ─────────────────
# Design rule: the probe NEVER blocks anything. It runs in the request
# thread, one at a time — a newer probe or a starting download kills the
# in-flight one, because preview data is advisory and must not compete
# with an actual download for bandwidth or CPU.

_probe_proc_lock = threading.Lock()
_probe_proc = None
_probe_cache = {}          # url -> summary dict (bounded, insertion-ordered)
_PROBE_CACHE_MAX = 10

# UI quality buckets, ascending (a format of height H belongs to the
# smallest bucket >= H; heights above 2160 are ignored as exotic)
_QUALITY_BUCKETS = [360, 480, 720, 1080, 1440, 2160]


def _kill_current_probe():
    global _probe_proc
    with _probe_proc_lock:
        proc = _probe_proc
        _probe_proc = None
    if proc and proc.poll() is None:
        kill_process_tree(proc.pid)


def _run_probe_json(args, timeout=25):
    """Run yt-dlp and parse its single-line JSON output (None on failure)."""
    global _probe_proc
    _kill_current_probe()
    try:
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                text=True, encoding="utf-8", errors="replace",
                                env=_clean_env(),
                                # own group on POSIX so killpg can't hit Aevum
                                start_new_session=sys.platform != "win32",
                                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
    except OSError:
        return None
    with _probe_proc_lock:
        _probe_proc = proc
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_process_tree(proc.pid)
        return None
    finally:
        with _probe_proc_lock:
            if _probe_proc is proc:
                _probe_proc = None
    if proc.returncode != 0 or not out:
        return None
    try:
        return json.loads(out)
    except ValueError:
        return None


def _summarize_video_info(info, in_playlist: bool) -> dict:
    """Boil a yt-dlp -J dump down to what the preview card needs."""
    best_audio = 0
    best_in_bucket = {}    # bucket -> {"size": int, "approx": bool}
    h264_max = 0           # tallest stream the site actually labels H.264
    h264_unsure = False    # ...and whether anything refuses to say

    for f in info.get("formats") or []:
        size = f.get("filesize") or f.get("filesize_approx") or 0
        approx = not f.get("filesize")
        vcodec = f.get("vcodec")
        acodec = f.get("acodec")
        width  = f.get("width")
        raw_h  = f.get("height")
        # The short side is what "720p" means to whoever is watching. Going
        # by height alone filed a 720x1280 reel under 1440p, so the chips
        # offered qualities the video never had and dimmed the one it did —
        # and the same number decides the H.264 notice, which has to agree
        # with what build_video_format will actually pick.
        height = min(raw_h, width) if raw_h and width else raw_h

        # The same two rules the H.264 chain in build_video_format runs on,
        # so the warning and the download can never disagree. An mp4 that
        # declares nothing counts as a maybe, not a no: Instagram's
        # progressive streams are avc1 and say so nowhere.
        if vcodec and str(vcodec).startswith(("avc1", "h264")):
            h264_max = max(h264_max, height or 0)
        elif not vcodec and not acodec and f.get("ext") == "mp4":
            h264_unsure = True

        if vcodec and vcodec != "none" and height:
            prev = 0
            for bucket in _QUALITY_BUCKETS:
                if prev < height <= bucket:
                    cur = best_in_bucket.get(bucket)
                    if cur is None or size > cur["size"]:
                        best_in_bucket[bucket] = {"size": size, "approx": approx}
                    break
                prev = bucket
        elif (not vcodec or vcodec == "none") and acodec and acodec != "none":
            if size > best_audio:
                best_audio = size

    qualities = {}
    for bucket in _QUALITY_BUCKETS:
        entry = best_in_bucket.get(bucket)
        if entry:
            total = (entry["size"] + best_audio) if entry["size"] else 0
            qualities[str(bucket)] = {"ok": True, "size": total,
                                      "approx": entry["approx"] or not total}
        else:
            qualities[str(bucket)] = {"ok": False, "size": 0, "approx": True}

    return {
        "kind": "video",
        "title": (info.get("title") or "")[:200],
        "uploader": (info.get("uploader") or info.get("channel") or "")[:100],
        "duration": int(info.get("duration") or 0),
        "thumbnail": info.get("thumbnail") or "",
        "qualities": qualities,
        "h264Max": h264_max,
        "h264Unsure": h264_unsure,
        "inPlaylist": in_playlist,
    }


def _probe_cache_put(url: str, summary: dict):
    if len(_probe_cache) >= _PROBE_CACHE_MAX:
        _probe_cache.pop(next(iter(_probe_cache)))
    _probe_cache[url] = summary


@app.route("/probe", methods=["POST"])
def probe_route():
    data = request.json or {}
    url = normalize_url((data.get("url") or "").strip())
    if not url:
        return jsonify({"error": "empty URL"}), 400

    cached = _probe_cache.get(url)
    if cached:
        return jsonify(cached)

    pid = playlist_id(url)
    is_pure_playlist = ("/playlist" in url) or (pid and "v=" not in url)

    if is_pure_playlist:
        # Cheap flat pass: titles only, no per-video format scan
        info = _run_probe_json([YTDLP, "-J", "--flat-playlist", "--no-warnings",
                                "--socket-timeout", "15", url],
                               timeout=20)
        if not info:
            return jsonify({"error": "probe failed"}), 502
        summary = {
            "kind": "playlist",
            "title": (info.get("title") or "")[:200],
            "count": int(info.get("playlist_count") or len(info.get("entries") or [])),
        }
        _probe_cache_put(url, summary)
        return jsonify(summary)

    info = _run_probe_json([YTDLP, "-J", "--no-playlist", "--no-warnings",
                            "--socket-timeout", "15", url])
    if not info:
        return jsonify({"error": "probe failed"}), 502
    summary = _summarize_video_info(info, in_playlist=bool(pid))
    _probe_cache_put(url, summary)
    return jsonify(summary)


def _queue_worker():
    """Runs queued downloads strictly one after another."""
    while True:
        with queue_cv:
            while not job_queue:
                queue_cv.wait()
            job_id = job_queue.pop(0)
        with jobs_lock:
            job = jobs.get(job_id)
            # Cancelled while it was still waiting: cancel_route already
            # marked it done, so there is nothing left to run.
            if job is None or job.get("cancelled") or job["done"]:
                continue
            data, output_dir = job["data"], job["output_dir"]
            job["code"] = "start"
        run_job(job_id, data, output_dir)


@app.route("/download", methods=["POST"])
def download_route():
    data = request.json or {}
    url  = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "empty URL"}), 400
    # A real download outranks preview metadata — free the bandwidth
    _kill_current_probe()
    raw_dir    = data.get("dir", "").strip()
    output_dir = str(Path(raw_dir).expanduser()) if raw_dir else _default_download_dir()
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    job_id = str(uuid.uuid4())[:8]
    # The preview card has almost always probed this URL already, so the
    # title is a cache hit; without it the row just falls back to the URL.
    # Keyed the way the probe stored it. A Vimeo link is rewritten on its
    # way to yt-dlp, so the raw address the user pasted is not the one the
    # card was filed under, and looking it up unchanged loses the title.
    cached = _probe_cache.get(normalize_url(url)) or {}
    with jobs_lock:
        # The tray app can run for weeks — drop old finished jobs so the
        # dict (and the output lines each one holds) can't grow forever
        done_ids = [jid for jid, j in jobs.items() if j["done"]]
        for jid in done_ids[:-20]:
            del jobs[jid]
        jobs[job_id] = {"done": False, "success": False, "lines": [], "read_idx": 0,
                        "progress": 0, "code": "queued", "item": "", "error_line": "",
                        "output_dir": output_dir, "url": url,
                        "title": (cached.get("title") or "")[:200],
                        "meta": history_meta(data), "data": data}
    with queue_cv:
        job_queue.append(job_id)
        queue_cv.notify()
    return jsonify({"job_id": job_id})


@app.route("/jobs")
def jobs_route():
    """Everything the downloads panel shows: what is waiting, what is
    running, and — from the history — what already finished."""
    with jobs_lock:
        active = [{"id": jid, "title": j.get("title", ""), "url": j.get("url", ""),
                   "meta": j.get("meta", ""), "code": j.get("code", "queued"),
                   "progress": j.get("progress", 0), "speed": j.get("speed", ""),
                   "eta": j.get("eta", ""), "total": j.get("total", ""),
                   "item": j.get("item", ""), "error_line": j.get("error_line", ""),
                   "subswarn": j.get("subswarn", False),
                   "cookieerr": j.get("cookieerr", False),
                   "formaterr": j.get("formaterr", False),
                   "staleerr": j.get("staleerr", False)}
                  for jid, j in jobs.items() if not j["done"]]
        # What the main bar keeps showing once the queue empties, with its
        # exact error / "subtitles skipped" wording. Only jobs that actually
        # ran count: dropping something from the queue is not a download
        # result and must not overwrite the last real one.
        finished = [j for j in jobs.values() if j["done"] and j.get("started")]
        last = None
        if finished:
            f = max(finished, key=lambda j: j.get("finished_at", 0.0))
            last = {"code": f.get("code", "done"), "success": f["success"],
                    "progress": f.get("progress", 0), "item": f.get("item", ""),
                    "error_line": f.get("error_line", ""),
                    "subswarn": f.get("subswarn", False),
                    "cookieerr": f.get("cookieerr", False),
                    "formaterr": f.get("formaterr", False),
                    "staleerr": f.get("staleerr", False)}
    with history_lock:
        past = list(download_history[:20])
    return jsonify({"active": active, "last": last, "past": past})


@app.route("/status/<job_id>")
def status_route(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return jsonify({"error": "not found"}), 404
        idx = job["read_idx"]
        new_lines = job["lines"][idx:]
        job["read_idx"] = len(job["lines"])
    return jsonify({"done": job["done"], "success": job["success"], "new_lines": new_lines,
                    "progress": job["progress"], "code": job.get("code", "download"),
                    "item": job.get("item", ""), "error_line": job.get("error_line", ""),
                    "speed": job.get("speed", ""), "subswarn": job.get("subswarn", False),
                    "cookieerr": job.get("cookieerr", False),
                    "formaterr": job.get("formaterr", False),
                    "staleerr": job.get("staleerr", False),
                    "output_dir": job["output_dir"]})


@app.route("/cancel/<job_id>", methods=["POST"])
def cancel_route(job_id):
    # Same shape as /update/apply: a POST with no body is a CORS simple
    # request, so any page open in the browser can reach it. Stopping a
    # download is a smaller thing to have done to you than running an
    # installer, but there is no reason to leave it open either.
    if request.headers.get("X-Aevum") != "1":
        return jsonify({"error": "bad request"}), 403
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return jsonify({"error": "not found"}), 404
        job["cancelled"] = True
        proc = job.get("proc")
        # Still waiting its turn: no process to kill, so retire it here.
        # The worker skips anything already marked done.
        if proc is None and not job["done"]:
            job.update({"done": True, "success": False, "code": "stopped",
                        "progress": 0, "speed": "", "eta": ""})
    if proc and proc.poll() is None:
        kill_process_tree(proc.pid)
    return jsonify({"ok": True})


@app.route("/history")
def history_route():
    with history_lock:
        return jsonify(download_history[:20])


@app.route("/fonts/<path:name>")
def fonts_route(name):
    # Fonts shipped with the app (no internet needed)
    return send_from_directory(os.path.join(_bin_dir(), "fonts"), name, max_age=31536000)


@app.route("/ping")
def ping_route():
    # Heartbeat from the page; before_request also refreshes _last_seen
    cid = request.args.get("id")
    if cid:
        with _clients_lock:
            _clients[cid] = time.time()
    # "app" marks this port as Aevum so a second launch can find us and
    # reuse this instance instead of stacking new ones (see main()).
    return jsonify({"ok": True, "app": "aevum"})


@app.route("/bye", methods=["POST", "GET"])
def bye_route():
    # Sent via sendBeacon as the tab closes — gives a fast, clean exit.
    # A page reload triggers it too, but the new page pings again right away;
    # since the watchdog waits _EXIT_GRACE, a reload never kills the app.
    cid = request.args.get("id")
    if cid:
        with _clients_lock:
            _clients.pop(cid, None)
    return jsonify({"ok": True})


# ── Persistent settings (language, theme): server-side config.json ──────────
# localStorage is tied to the port: when 5000 is busy and we fall back to 5001,
# the browser treats that as a different site and the choices vanish. So the
# language/theme picks live on the server instead.

_cfg_lock = threading.Lock()


def _config_file() -> str:
    return os.path.join(_user_data_dir(), "config.json")


def _load_config() -> dict:
    try:
        with open(_config_file(), encoding="utf-8") as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_config(updates: dict):
    with _cfg_lock:
        cfg = _load_config()
        cfg.update(updates)
        path = _config_file()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f)


# ── Setting: launch at startup (Windows: registry, Linux: autostart) ─────────
_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_RUN_NAME = "Aevum"


def _launch_target() -> str:
    # Most durable target first: menu-installed copy, then $APPIMAGE, then frozen exe, then dev
    if sys.platform.startswith("linux") and os.path.isfile(_installed_appimage()):
        return f'"{_installed_appimage()}" --startup'
    if os.environ.get("APPIMAGE"):
        return f'"{os.environ["APPIMAGE"]}" --startup'
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --startup'
    return f'"{sys.executable}" "{os.path.abspath(__file__)}" --startup'


def _linux_autostart_file() -> str:
    cfg = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(cfg, "autostart", "aevum.desktop")


def get_startup_enabled() -> bool:
    if _IS_WINDOWS:
        if not winreg:
            return False
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as k:
                val, _ = winreg.QueryValueEx(k, _RUN_NAME)
                return bool(val)
        except (FileNotFoundError, OSError):
            return False
    return os.path.isfile(_linux_autostart_file())


def set_startup_enabled(enabled: bool):
    if _IS_WINDOWS:
        if not winreg:
            return
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as k:
            if enabled:
                winreg.SetValueEx(k, _RUN_NAME, 0, winreg.REG_SZ, _launch_target())
            else:
                try:
                    winreg.DeleteValue(k, _RUN_NAME)
                except FileNotFoundError:
                    pass
        return
    # ── Linux: write/remove ~/.config/autostart/aevum.desktop ──
    path = _linux_autostart_file()
    if enabled:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        content = (
            "[Desktop Entry]\n"
            "Type=Application\n"
            "Name=Aevum\n"
            "Comment=Download video and audio from any site\n"
            f"Exec={_launch_target()}\n"
            "Terminal=false\n"
            "X-GNOME-Autostart-enabled=true\n"
        )
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
    else:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass


# ── Linux: install into the app menu ("download, install, done") ─────────────
# Copies the AppImage under ~/.local/share/aevum/, writes a .desktop entry and
# an icon into the menu; after that it launches like a regular app, no terminal.

def _xdg_data_home() -> str:
    return os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")


def _menu_desktop_file() -> str:
    return os.path.join(_xdg_data_home(), "applications", "aevum.desktop")


def _menu_icon_file() -> str:
    return os.path.join(_xdg_data_home(), "icons", "hicolor", "256x256", "apps", "aevum.png")


def _installed_appimage() -> str:
    return os.path.join(_xdg_data_home(), "aevum", "Aevum.AppImage")


def get_menu_installed() -> bool:
    return os.path.isfile(_menu_desktop_file())


def _source_icon() -> str:
    # 1) PyInstaller bundle  2) AppImage root ($APPDIR)  3) next to the script
    cands = [os.path.join(_bin_dir(), "aevum.png"),
             os.path.join(os.path.dirname(os.path.abspath(__file__)), "aevum.png")]
    if os.environ.get("APPDIR"):
        cands.insert(1, os.path.join(os.environ["APPDIR"], "aevum.png"))
    for cand in cands:
        if os.path.isfile(cand):
            return cand
    return ""


def _refresh_menu_caches():
    """Refresh the desktop's menu/icon caches (when the tools exist; failures are fine)."""
    for cmd in (["update-desktop-database", os.path.dirname(_menu_desktop_file())],
                ["gtk-update-icon-cache", "-f", "-t",
                 os.path.join(_xdg_data_home(), "icons", "hicolor")]):
        exe = shutil.which(cmd[0])
        if exe:
            try:
                subprocess.run([exe] + cmd[1:], env=_clean_env(), check=False,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=15)
            except (OSError, subprocess.TimeoutExpired):
                pass


def install_menu_entry() -> None:
    appimage = os.environ.get("APPIMAGE")
    if appimage and os.path.isfile(appimage):
        target = _installed_appimage()
        os.makedirs(os.path.dirname(target), exist_ok=True)
        if os.path.abspath(appimage) != os.path.abspath(target):
            shutil.copy2(appimage, target)
        os.chmod(target, 0o755)
        exec_line = f'"{target}"'
    elif getattr(sys, "frozen", False):
        exec_line = f'"{sys.executable}"'
    else:
        exec_line = f'"{sys.executable}" "{os.path.abspath(__file__)}"'

    icon_src = _source_icon()
    if icon_src:
        os.makedirs(os.path.dirname(_menu_icon_file()), exist_ok=True)
        shutil.copy2(icon_src, _menu_icon_file())

    path = _menu_desktop_file()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    content = (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Aevum\n"
        "Comment=Download video and audio from any site\n"
        "Comment[tr]=Her siteden video ve ses indir\n"
        f"Exec={exec_line}\n"
        "Icon=aevum\n"
        "Terminal=false\n"
        "Categories=Network;FileTransfer;AudioVideo;\n"
        "StartupNotify=false\n"
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    _refresh_menu_caches()


def uninstall_menu_entry() -> None:
    for p in (_menu_desktop_file(), _menu_icon_file()):
        try:
            os.remove(p)
        except FileNotFoundError:
            pass
    try:
        os.remove(_installed_appimage())
    except FileNotFoundError:
        pass
    try:
        os.rmdir(os.path.dirname(_installed_appimage()))
    except OSError:
        pass
    # don't leave an autostart entry pointing at the removed copy
    auto = _linux_autostart_file()
    if os.path.isfile(auto):
        try:
            with open(auto, encoding="utf-8") as f:
                if _installed_appimage() in f.read():
                    os.remove(auto)
        except OSError:
            pass
    _refresh_menu_caches()


# ── Updating Aevum itself ───────────────────────────────────────────────────
# Aevum ships as four packages and each one replaces itself differently, so
# the first job is working out which is running. Whatever gets downloaded is
# checked against the release's own checksums.txt before anything is opened:
# this is an unsigned binary pulled over the network and then executed, and
# that hash is the only thing between a real release and whatever else
# answered the request.

_update_state = {"stage": "idle", "pct": 0, "msg": "", "path": ""}
_update_lock = threading.Lock()
_update_cache = {"at": 0.0, "data": None}

_UPDATE_ASSET = {"setup": "Aevum-Setup.exe",
                 "portable": "Aevum.exe",
                 "appimage": "Aevum-x86_64.AppImage",
                 "tarball": "Aevum-linux-x86_64.tar.gz"}


def _app_dir() -> str:
    """Where this copy actually lives — not PyInstaller's extraction dir."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _update_dir(kind: str) -> str:
    """Where an update may be written, which is not always where we run.

    An AppImage runs out of a read-only squashfs mount, so _app_dir() there
    points at /tmp/.mount_XXXX/usr/bin and nothing can be written next to
    it — not the part file, not the probe. The file it was launched from is
    the real neighbour, and the environment hands us its path.
    """
    if kind == "appimage":
        launched = os.environ.get("APPIMAGE")
        if launched:
            return os.path.dirname(os.path.abspath(launched))
    return _app_dir()


def _version_tuple(s: str) -> tuple:
    """'v1.2.10' -> (1, 2, 10). Anything unreadable sorts oldest.

    Reading stops at the first thing that is not part of the number, so
    1.2.5-rc1 counts as 1.2.5 rather than as four components — otherwise a
    release candidate outranks the release it is a candidate for, and
    anyone already on 1.2.5 gets offered a downgrade. Trailing zeros go
    too, so 1.2.4.0 and 1.2.4 are the same version, which they are.
    """
    core = re.match(r"\d+(?:\.\d+)*", (s or "").lstrip("vV"))
    if not core:
        return (0,)
    parts = [int(n) for n in core.group(0).split(".")]
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def install_kind() -> str:
    """Which package this copy came from, because each updates differently."""
    if os.environ.get("APPIMAGE"):
        return "appimage"
    if not getattr(sys, "frozen", False):
        return "source"
    if _IS_WINDOWS:
        # Inno leaves its uninstaller beside the exe. A portable copy is alone.
        return "setup" if os.path.isfile(
            os.path.join(_app_dir(), "unins000.exe")) else "portable"
    # Frozen, on Linux, not an AppImage: whoever unpacked the tar.gz.
    return "tarball"


def _fetch(url: str, timeout: int = 30):
    req = urllib.request.Request(
        url, headers={"User-Agent": f"Aevum/{APP_VERSION}"})
    return urllib.request.urlopen(req, timeout=timeout)


def _latest_release() -> dict:
    """Newest published release. Cached, so opening Settings twice is free."""
    now = time.time()
    if _update_cache["data"] and now - _update_cache["at"] < 900:
        return _update_cache["data"]
    with _fetch(f"https://api.github.com/repos/{UPDATE_REPO}/releases/latest") as r:
        data = json.loads(r.read().decode("utf-8", "replace"))
    _update_cache.update(at=now, data=data)
    return data


def _asset_url(rel: dict, name: str) -> str:
    for a in rel.get("assets") or []:
        if a.get("name") == name:
            return a.get("browser_download_url") or ""
    return ""


def _published_sha(rel: dict, name: str) -> str:
    """The release's checksums.txt, written as 'name  sha256' per line."""
    url = _asset_url(rel, "checksums.txt")
    if not url:
        return ""
    with _fetch(url) as r:
        for line in r.read().decode("utf-8", "replace").splitlines():
            parts = line.split()
            # Two shapes in the wild: "name  hash", which is what the
            # releases carry, and "name  SHA256: hash", which is what
            # build.bat writes locally. Taking the last field covers both,
            # and a length check keeps a stray line from passing for one.
            if len(parts) >= 2 and parts[0] == name and len(parts[-1]) == 64:
                return parts[-1].lower()
    return ""


def _set_update(**kw):
    with _update_lock:
        _update_state.update(kw)


def _do_update(rel: dict, kind: str, name: str, ver: str):
    """Download, verify, then hand over. Never overwrites a running binary."""
    work = _update_dir(kind)
    tmp = os.path.join(work, name + ".part")
    try:
        # Ask the folder whether it will take a file before spending ninety
        # megabytes finding out. A copy installed somewhere read-only fails
        # here in a sentence the user can act on, rather than at the last
        # line with a stack error and a path in it.
        try:
            probe = os.path.join(work, ".aevum-write-test")
            with open(probe, "wb"):
                pass
            os.remove(probe)
        except OSError:
            _set_update(stage="error", msg="cannot write to " + work)
            return

        # A tar.gz unpacks into a folder named after the incoming version,
        # and that folder gets cleared first. Should the running copy happen
        # to live in one already named that — someone who renamed it, most
        # likely — clearing it would delete the program doing the clearing.
        # Checked here rather than at the unpack, so it costs nothing
        # instead of ninety megabytes.
        if kind == "tarball":
            here = os.path.abspath(_app_dir())
            out = os.path.abspath(os.path.join(
                os.path.dirname(work) or ".", f"Aevum-{ver}"))
            if here == out or here.startswith(out + os.sep):
                _set_update(stage="error",
                            msg="the folder for the new version is the one running")
                return

        want = _published_sha(rel, name)
        if not want:
            _set_update(stage="error", msg="no checksum published for " + name)
            return
        _set_update(stage="download", pct=0, msg="")
        import hashlib
        h = hashlib.sha256()
        with _fetch(_asset_url(rel, name), timeout=60) as r, open(tmp, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            got = 0
            while True:
                chunk = r.read(262144)
                if not chunk:
                    break
                f.write(chunk)
                h.update(chunk)
                got += len(chunk)
                if total:
                    _set_update(pct=min(99, int(got * 100 / total)))
        if h.hexdigest().lower() != want:
            os.remove(tmp)
            _set_update(stage="error", msg="checksum mismatch")
            return

        if kind == "appimage":
            # Replacing the file a running AppImage was mounted from is safe:
            # the mount already holds its own copy. It takes effect on the
            # next launch, which is why this one does not relaunch itself.
            target = os.environ.get("APPIMAGE") or os.path.join(work, name)
            os.replace(tmp, target)
            os.chmod(target, 0o755)
            _set_update(stage="done", pct=100, path=target)
            return

        if kind == "tarball":
            # The tarball holds one Aevum/ directory, which is the directory
            # this process is running out of. Unpacking over it would pull
            # files out from under a running program, so it goes into a
            # versioned folder alongside instead: the new copy is ready to
            # run and the old one still works if it is not.
            import tarfile
            out = os.path.join(os.path.dirname(work) or ".",
                               f"Aevum-{ver}")
            shutil.rmtree(out, ignore_errors=True)
            os.makedirs(out, exist_ok=True)
            with tarfile.open(tmp, "r:gz") as tf:
                try:
                    tf.extractall(out, filter="data")
                except TypeError:
                    # filter= arrived in 3.12. The archive is one we published
                    # and just checksummed, so the older call is acceptable.
                    tf.extractall(out)
            os.remove(tmp)
            _set_update(stage="done", pct=100, path=out)
            return

        if kind == "setup":
            # Named after the version so a half-finished download from an
            # earlier attempt can never be the thing that gets launched.
            final = os.path.join(work, f"Aevum-Setup-{ver}.exe")
            if os.path.exists(final):
                os.remove(final)
            os.replace(tmp, final)
            # The installer replaces the files this process is running from,
            # so it has to start and then be left alone. Inno upgrades in
            # place: same AppId, settings and shortcuts survive.
            subprocess.Popen([final], close_fds=True, env=_clean_env())
            _set_update(stage="launched", pct=100, path=final)
            threading.Timer(1.5, lambda: os._exit(0)).start()
            return

        # Portable. The version goes in the name for a plain reason: the asset
        # is called Aevum.exe and so is the exe this process is running from,
        # so writing it under its own name would mean deleting ourselves —
        # which Windows refuses, and the update would fail every time.
        # Replacing a running exe needs a helper that outlives it, and
        # self-replacing binaries are what antivirus heuristics are built to
        # catch, so the new copy lands beside the old one and the folder
        # opens on it: the swap, minus hunting for the download.
        stem, ext = os.path.splitext(name)
        final = os.path.join(work, f"{stem}-{ver}{ext}")
        if os.path.exists(final):
            os.remove(final)
        os.replace(tmp, final)
        _set_update(stage="done", pct=100, path=final)
        if _IS_WINDOWS:
            subprocess.Popen(["explorer", f"/select,{final}"], close_fds=True)
    except Exception as e:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        _set_update(stage="error", msg=str(e)[:120])


@app.route("/update/check")
def update_check():
    kind = install_kind()
    try:
        rel = _latest_release()
    except Exception:
        return jsonify({"ok": False, "current": APP_VERSION, "kind": kind})
    latest = (rel.get("tag_name") or "").lstrip("vV")
    name = _UPDATE_ASSET.get(kind, "")
    return jsonify({
        "ok": True,
        "current": APP_VERSION,
        "latest": latest,
        "newer": _version_tuple(latest) > _version_tuple(APP_VERSION),
        "kind": kind,
        # Without the checksum file there is nothing to verify against and
        # the download would refuse at the last step, so do not offer it.
        "canApply": bool(name and _asset_url(rel, name)
                         and _asset_url(rel, "checksums.txt")),
        "page": rel.get("html_url") or "",
    })


@app.route("/update/apply", methods=["POST"])
def update_apply():
    # A POST with no body and no content type is a CORS "simple request":
    # any page open in the browser can send it to localhost without asking
    # permission first. This one downloads an installer, runs it and then
    # exits the app, which is not something a stranger gets to decide. A
    # header the page adds itself cannot be forged that way — setting one
    # turns the request into a preflighted call, and nothing here answers
    # a preflight.
    if request.headers.get("X-Aevum") != "1":
        return jsonify({"stage": "error", "msg": "bad request"}), 403
    # The setup path starts the installer and exits the app a second and a
    # half later. Anything still downloading would be orphaned mid-file with
    # no window left to say so, and the queue behind it lost.
    with jobs_lock:
        if any(not j["done"] for j in jobs.values()):
            return jsonify({"stage": "error", "busy": True,
                            "msg": "a download is running"}), 409
    kind = install_kind()
    name = _UPDATE_ASSET.get(kind, "")
    if not name:
        return jsonify({"stage": "error", "msg": "no package for this build"}), 400
    with _update_lock:
        if _update_state["stage"] in ("download", "launched"):
            return jsonify(dict(_update_state))
        # Claim the slot inside the same lock that just tested it. Asking
        # GitHub for the release takes long enough that a second click, or a
        # second tab, walks through the gap and starts its own download into
        # the same part file.
        _update_state.update(stage="download", pct=0, msg="", path="")
    def _fail(msg, code):
        _set_update(stage="error", msg=msg)
        return jsonify({"stage": "error", "msg": msg}), code
    try:
        rel = _latest_release()
    except Exception as e:
        return _fail(str(e)[:120], 502)
    latest = (rel.get("tag_name") or "").lstrip("vV")
    if _version_tuple(latest) <= _version_tuple(APP_VERSION):
        return _fail("already up to date", 400)
    threading.Thread(target=_do_update, args=(rel, kind, name, latest), daemon=True).start()
    return jsonify(dict(_update_state))


@app.route("/update/status")
def update_status():
    with _update_lock:
        return jsonify(dict(_update_state))


# ── Keeping yt-dlp current ───────────────────────────────────────────────────
# Everything else in this build can sit still. yt-dlp cannot: it is the piece
# that talks to the sites, and the sites keep changing the locks. Shipping a
# whole 120 MB release every time YouTube turns a key is no way to live, so
# the newer copy goes into the user's own folder, where _find_binary looks
# first and no installer is needed.
#
# There is no download-and-verify code below, because yt-dlp already has some:
# it fetches the build, checks it against the hash the release publishes and
# replaces itself. All that is missing is somewhere writable to do it, which
# is the copy this makes.
#
# The nightly channel, not stable, and that is a measured choice rather than a
# taste for the bleeding edge. Stable releases have come 25 to 84 days apart,
# and the fix for whatever the site changed this week is in the tree the day
# after. On 2026-08-16 the newest stable was six weeks old and could not
# download from YouTube at all; the nightly of that morning could.
PKG_REPO = "yt-dlp/yt-dlp-nightly-builds"
PKG_CHANNEL = "nightly"

_pkg_lock = threading.Lock()
_pkg_state = {"stage": "idle", "msg": "", "version": ""}
_pkg_cache = {"at": 0.0, "tag": ""}


def _user_ytdlp() -> str:
    """Where an updated yt-dlp lives, which is never inside the package."""
    return os.path.join(_user_data_dir(), "bin",
                        "yt-dlp" + (".exe" if _IS_WINDOWS else ""))


def _ytdlp_version() -> str:
    try:
        out = subprocess.run(
            [YTDLP, "--version"], capture_output=True, text=True, timeout=20,
            env=_clean_env(),
            creationflags=subprocess.CREATE_NO_WINDOW if _IS_WINDOWS else 0)
        return (out.stdout or "").strip().splitlines()[0].strip()
    except (OSError, subprocess.SubprocessError, IndexError):
        return ""


def _latest_pkg_tag() -> str:
    """Newest nightly tag. Cached like the app's own check, for the same reason."""
    now = time.time()
    if _pkg_cache["tag"] and now - _pkg_cache["at"] < 900:
        return _pkg_cache["tag"]
    with _fetch(f"https://api.github.com/repos/{PKG_REPO}/releases/latest") as r:
        tag = (json.loads(r.read().decode("utf-8", "replace")).get("tag_name") or "")
    _pkg_cache.update(at=now, tag=tag)
    return tag


def _set_pkg(**kw):
    with _pkg_lock:
        _pkg_state.update(kw)


def _do_pkg_update():
    """Copy yt-dlp somewhere writable, then let it update itself."""
    global YTDLP
    dest = _user_ytdlp()
    previous = YTDLP
    before = _ytdlp_version()
    created = False
    try:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        if not os.path.isfile(dest):
            # Copy to one side and move it into place, rather than writing
            # 17 MB straight to the name the app will trust from now on. A
            # copy cut short — no disk, no power — would otherwise leave half
            # a binary sitting where _find_binary looks first, and every
            # download after that would fail with nothing to explain it.
            staging = dest + ".part"
            shutil.copy2(BUNDLED_YTDLP, staging)
            if not _IS_WINDOWS:
                os.chmod(staging, 0o755)
            os.replace(staging, dest)
            created = True
        out = subprocess.run(
            [dest, "--update-to", PKG_CHANNEL], capture_output=True, text=True,
            timeout=300, env=_clean_env(),
            creationflags=subprocess.CREATE_NO_WINDOW if _IS_WINDOWS else 0)
        YTDLP = dest
        ver = _ytdlp_version()
        if out.returncode != 0 or not ver:
            # Either the update refused or what it left behind will not run.
            # A yt-dlp that cannot answer --version cannot download either,
            # so put the packaged one back rather than leave the app holding
            # a binary it cannot use.
            #
            # The copy this call made is cleared away too. It is byte for byte
            # the packaged one, so keeping it breaks nothing — but from the
            # next launch on it is what _find_binary finds, and Settings would
            # offer to undo an update that never happened.
            YTDLP = previous
            if created or not ver:
                try:
                    os.remove(dest)
                except OSError:
                    pass
                YTDLP = BUNDLED_YTDLP
            msg = (out.stderr or out.stdout or "").strip().splitlines()
            _set_pkg(stage="error", msg=(msg[-1][:120] if msg else "update failed"))
            return
        _pkg_cache.update(at=0.0, tag="")
        if ver == before:
            # Pressing the button on an already-current copy succeeds without
            # installing anything, and "updated to X" would be a plain
            # untruth. Idle lets the line say what it says the rest of the
            # time, which by then is that this is the newest one. A copy made
            # for an update that turned out to be unnecessary goes away with
            # it, rather than sitting there offering to be undone.
            if created:
                try:
                    os.remove(dest)
                except OSError:
                    pass
                YTDLP = previous
            _set_pkg(stage="idle", msg="", version=ver)
            return
        _set_pkg(stage="done", msg="", version=ver)
    except Exception as e:
        YTDLP = previous
        _set_pkg(stage="error", msg=str(e)[:120])


@app.route("/packages/check")
def packages_check():
    cur = _ytdlp_version()
    custom = os.path.isfile(_user_ytdlp())
    try:
        tag = _latest_pkg_tag()
    except Exception:
        return jsonify({"ok": False, "current": cur, "custom": custom})
    return jsonify({"ok": True, "current": cur, "latest": tag,
                    "newer": _version_tuple(tag) > _version_tuple(cur),
                    "custom": custom})


@app.route("/packages/apply", methods=["POST"])
def packages_apply():
    # Same reasoning as /update/apply: without a header of our own this is a
    # CORS simple request and any open page could start it.
    if request.headers.get("X-Aevum") != "1":
        return jsonify({"stage": "error", "msg": "bad request"}), 403
    # Windows will not let a running binary be overwritten, and on Linux
    # swapping it mid-download is no better an idea. Wait for the queue.
    with jobs_lock:
        if any(not j["done"] for j in jobs.values()):
            return jsonify({"stage": "error", "busy": True,
                            "msg": "a download is running"}), 409
    with _pkg_lock:
        if _pkg_state["stage"] == "working":
            return jsonify(dict(_pkg_state))
        _pkg_state.update(stage="working", msg="", version="")
    threading.Thread(target=_do_pkg_update, daemon=True).start()
    with _pkg_lock:
        return jsonify(dict(_pkg_state))


@app.route("/packages/status")
def packages_status():
    with _pkg_lock:
        return jsonify(dict(_pkg_state))


@app.route("/packages/revert", methods=["POST"])
def packages_revert():
    """Back to the copy the app shipped with.

    The whole point of the nightly channel is getting fixes early, and early
    is also where a bad build lives. Deleting one file is the way back, and
    it needs to be one click rather than a paragraph of instructions.
    """
    global YTDLP
    if request.headers.get("X-Aevum") != "1":
        return jsonify({"stage": "error", "msg": "bad request"}), 403
    with jobs_lock:
        if any(not j["done"] for j in jobs.values()):
            return jsonify({"stage": "error", "busy": True,
                            "msg": "a download is running"}), 409
    try:
        if os.path.isfile(_user_ytdlp()):
            os.remove(_user_ytdlp())
    except OSError as e:
        return jsonify({"stage": "error", "msg": str(e)[:120]}), 500
    YTDLP = BUNDLED_YTDLP
    _set_pkg(stage="idle", msg="", version="")
    return jsonify({"stage": "idle", "current": _ytdlp_version()})


@app.route("/settings")
def settings_get():
    cfg = _load_config()
    return jsonify({
        "startup": get_startup_enabled(),
        "menu": get_menu_installed(),
        "canMenu": sys.platform.startswith("linux"),
        # Launch-at-startup needs a tray to sit in — Windows only
        "canStartup": _IS_WINDOWS,
        "lang": cfg.get("lang", ""),
        "theme": cfg.get("theme", ""),
    })


@app.route("/settings", methods=["POST"])
def settings_set():
    data = request.json or {}
    if "menu" in data and sys.platform.startswith("linux"):
        try:
            if data["menu"]:
                install_menu_entry()
            else:
                uninstall_menu_entry()
        except OSError as e:
            return jsonify({"ok": False, "error": str(e)}), 500
    if "startup" in data and _IS_WINDOWS:
        try:
            set_startup_enabled(bool(data["startup"]))
        except OSError as e:
            return jsonify({"ok": False, "error": str(e)}), 500
    # Language/theme choice: stored server-side so it survives port changes
    updates = {}
    if isinstance(data.get("lang"), str) and data["lang"]:
        updates["lang"] = data["lang"][:8]
    if isinstance(data.get("theme"), str) and data["theme"]:
        updates["theme"] = data["theme"][:16]
    if updates:
        try:
            _save_config(updates)
        except OSError:
            pass  # even if the config can't be written, keep the app running
    return jsonify({"ok": True, "startup": get_startup_enabled(),
                    "menu": get_menu_installed()})


# ── Tray icon ────────────────────────────────────────────────────────────────
def create_icon_image():
    """Aevum orbit icon (tray): green ring + star + core dot"""
    import math
    S = 64
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse([0, 0, S - 1, S - 1], fill=(10, 10, 14, 255))
    cx = cy = S // 2
    R = int(S * 0.30)
    w = max(2, int(S * 0.06))
    d.ellipse([cx - R, cy - R, cx + R, cy + R], outline=(0, 230, 160, 235), width=w)
    cr = max(1, int(S * 0.03))
    d.ellipse([cx - cr, cy - cr, cx + cr, cy + cr], fill=(0, 230, 160, 220))
    ang = math.radians(-48)
    sx = cx + R * math.cos(ang)
    sy = cy + R * math.sin(ang)
    sr = max(2, int(S * 0.11))
    d.ellipse([sx - sr, sy - sr, sx + sr, sy + sr], fill=(230, 255, 245, 255))
    return img


def _open_url(url: str):
    """Open the browser with a clean env (the webbrowser module passes the polluted LD_LIBRARY_PATH along)."""
    if sys.platform.startswith("linux"):
        opener = shutil.which("xdg-open")
        if opener:
            try:
                subprocess.Popen([opener, url], env=_clean_env(),
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True)
                return
            except OSError:
                pass
    webbrowser.open(url)


def open_browser(icon=None, item=None):
    _open_url(f"http://localhost:{PORT}")


def quit_app(icon, item):
    # Also stop any downloads (and a preview probe) still running
    _kill_current_probe()
    with jobs_lock:
        procs = [j.get("proc") for j in jobs.values()]
    for p in procs:
        if p and p.poll() is None:
            kill_process_tree(p.pid)
    icon.stop()
    os._exit(0)


def find_free_port(start: int = 5000) -> int:
    for p in range(start, start + 60):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    return start


def start_flask():
    import logging
    log = logging.getLogger("werkzeug")
    log.setLevel(logging.ERROR)
    app.run(host="127.0.0.1", port=PORT, debug=False, use_reloader=False)


def _shutdown_now():
    # Kill any download processes still around (stuck ones included)
    _kill_current_probe()
    with jobs_lock:
        procs = [j.get("proc") for j in jobs.values()]
    for p in procs:
        if p and p.poll() is None:
            kill_process_tree(p.pid)
    os._exit(0)


def _browser_watchdog():
    """Linux (no-tray mode): shut the app down once the last tab is gone.

    Tabs ping /ping every 3s and send /bye when closing. We exit when no live
    tab remains and the last request is older than _EXIT_GRACE. If a /bye gets
    lost (crash, power cut), the tab counts as dead after _CLIENT_STALE.
    We never exit while a download is running — it finishes first, and if the
    page is still gone afterwards, then we quit.
    """
    while True:
        time.sleep(3)
        with jobs_lock:
            active = any(not j["done"] for j in jobs.values())
        if active:
            continue
        now = time.time()
        with _clients_lock:
            for cid, ts in list(_clients.items()):
                if now - ts > _CLIENT_STALE:
                    del _clients[cid]
            alive = bool(_clients)
        if alive:
            continue
        # Be patient if no page ever connected (the browser may be slow to open)
        grace = _EXIT_GRACE if _page_seen else _FIRST_GRACE
        if now - _last_seen > grace:
            print("Browser tab closed — shutting down Aevum.", flush=True)
            _shutdown_now()


def _find_running_instance():
    """URL of an already-running Aevum on the usual ports, or None."""
    import urllib.request
    for p in range(5000, 5010):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{p}/ping", timeout=0.4) as r:
                if json.loads(r.read().decode()).get("app") == "aevum":
                    return f"http://localhost:{p}"
        except Exception:
            continue
    return None


def main():
    global PORT
    # Single instance: launching Aevum while it already runs used to stack a
    # second server (and tray icon) on the next port, one per launch. Reuse
    # the running one instead — just bring its page up and leave.
    existing = _find_running_instance()
    if existing:
        print(f"Aevum already running -> {existing}", flush=True)
        # Silent launches (login autostart / tray-only) shouldn't pop a tab
        if "--startup" not in sys.argv and "--tray" not in sys.argv:
            _open_url(existing)
        return
    # If the port is busy, pick a free one (avoids the app silently not opening)
    PORT = find_free_port(5000)
    threading.Thread(target=_queue_worker, daemon=True).start()
    flask_thread = threading.Thread(target=start_flask, daemon=True)
    flask_thread.start()
    time.sleep(1.2)

    url = f"http://localhost:{PORT}"
    print(f"Aevum is running -> {url}", flush=True)
    # Say which binaries were resolved. When running from source without a
    # bin\ folder the lookup silently falls back to PATH, and a PATH ffmpeg
    # of the wrong version once masqueraded as a clip bug (research-clip.md).
    print(f"using yt-dlp: {YTDLP}", flush=True)
    print(f"using ffmpeg: {os.path.join(FFMPEG_DIR, 'ffmpeg') if FFMPEG_DIR else 'from PATH'}", flush=True)

    # ── Linux: no-tray mode — browser opens, app exits when the tab closes ──
    if not _IS_WINDOWS:
        # The old version's launch-at-startup entry makes no sense without a tray; clean it up
        try:
            os.remove(_linux_autostart_file())
        except OSError:
            pass
        # Not installed into the menu yet — point the way (AppImage)
        if os.environ.get("APPIMAGE") and not get_menu_installed():
            print("Tip: to install Aevum into your app menu, open Settings > "
                  "'Add to app menu' on the page, or run: "
                  f"\"{os.environ['APPIMAGE']}\" --install", flush=True)
        _open_url(url)
        threading.Thread(target=_browser_watchdog, daemon=True).start()
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            _shutdown_now()
        return

    # ── Windows: tray mode ──
    # Started with --startup (login autostart): don't open the page, just sit in the tray
    tray_only = "--startup" in sys.argv or "--tray" in sys.argv
    if not tray_only:
        _open_url(url)

    if pystray is not None:
        try:
            icon_image = create_icon_image()
            menu = pystray.Menu(
                pystray.MenuItem("Open Aevum", open_browser, default=True),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Quit Aevum", quit_app),
            )
            icon = pystray.Icon("aevum", icon_image, "Aevum", menu)
            icon.run()
            return
        except Exception as e:
            # Tray failed to start — don't crash, keep the server up.
            print(f"No tray icon ({e.__class__.__name__}); use the browser page, "
                  f"press Ctrl+C to quit.", flush=True)

    # No-tray fallback: keep the server alive so downloads still work.
    if tray_only:
        _open_url(url)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        os._exit(0)


if __name__ == "__main__":
    if sys.platform.startswith("linux") and "--uninstall" in sys.argv:
        uninstall_menu_entry()
        print("Aevum removed from the app menu.", flush=True)
        sys.exit(0)
    if sys.platform.startswith("linux") and "--install" in sys.argv:
        install_menu_entry()
        print("Aevum installed to the app menu — you can now launch it from there.",
              flush=True)
        sys.exit(0)
    main()
