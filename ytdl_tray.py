#!/usr/bin/env python3
"""
ytdl_tray.py — Aevum (yt-dlp) İndirme Uygulaması

Windows: çift tıkla → tray'e oturur, tarayıcı açılır; sağ tıkla → Aç / Kapat.
Linux:   tepsi yok — başlatınca tarayıcı açılır, sekmeyi kapatınca uygulama
         da tamamen kapanır (sayfadan gelen kalp atışı kesilince).

yt-dlp'nin desteklediği ~1800 siteden video/ses indirir.
"""

import os
import sys
import json
import threading
import webbrowser
import time
import subprocess
import shutil
import socket
from pathlib import Path
from urllib.parse import urlparse, parse_qs

try:
    import winreg  # Windows başlangıç ayarı için
except ImportError:
    winreg = None

# ── Tray için gerekli import (yalnızca Windows) ──────────────────────────────
# Linux'ta tepsi kullanılmıyor: masaüstü ortamına göre ya hiç çıkmıyor ya da
# işlevsiz düz bir kare görünüyordu. Linux'ta uygulama tarayıcı sekmesine bağlı
# yaşar — sekme kapanınca kendini kapatır (aşağıda _browser_watchdog).
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

# ── Flask uygulaması ─────────────────────────────────────────────────────────
app = Flask(__name__)
download_history = []
history_lock = threading.Lock()
jobs = {}
jobs_lock = threading.Lock()

PORT = 5000

# ── Sayfa yaşam takibi (Linux'ta uygulama tarayıcı sekmesiyle yaşar) ─────────
_last_seen = time.time()   # sayfadan gelen son istek zamanı
_page_seen = False         # sayfa en az bir kez bağlandı mı?
_clients = {}              # sekme kimliği -> son kalp atışı zamanı
_clients_lock = threading.Lock()

# Tarayıcılar arka plan sekmelerinde zamanlayıcıları kısar (Chrome: dakikada 1'e
# kadar). Bu yüzden kalp atışı tek başına yeterli değil: temiz kapanışta sayfa
# /bye gönderir (hızlı çıkış), kalp atışı ise uzun süreli yedek olarak kalır.
_EXIT_GRACE = 15     # sn — son sekme kapandıktan sonra bekleme
_FIRST_GRACE = 120   # sn — sayfa hiç bağlanmadıysa (tarayıcı yavaş açılabilir)
_CLIENT_STALE = 90   # sn — bu kadar süre ping atmayan sekme ölü sayılır
                     #      (arka plan kısıtlamasındaki sekme ~60 sn'de bir atar)


@app.before_request
def _touch_last_seen():
    global _last_seen, _page_seen
    _last_seen = time.time()
    _page_seen = True


def _bin_dir() -> str:
    """Gömülü ikili dosyaların bulunduğu klasör (PyInstaller çıkarma dizini ya da script yanı)."""
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _find_binary(name: str) -> str:
    """Önce uygulamayla gelen gömülü ikiliyi, yoksa PATH'i kullan."""
    sfx = ".exe" if sys.platform == "win32" else ""
    fname = name + sfx
    for cand in (os.path.join(_bin_dir(), fname), os.path.join(_bin_dir(), "bin", fname)):
        if os.path.isfile(cand):
            # PyInstaller Linux/macOS'ta gömülü ikililerin +x iznini düşürür → geri ver
            if sys.platform != "win32":
                try:
                    os.chmod(cand, 0o755)
                except OSError:
                    pass
            return cand
    return shutil.which(name) or fname


def _clean_env() -> dict:
    """Alt süreçler için temiz ortam.

    PyInstaller, Linux'ta LD_LIBRARY_PATH'i kendi bundle klasörüne çevirir ve bu
    tüm alt süreçlere miras kalır: xdg-open ile açılan tarayıcı bizim glib/gio
    kopyalarımızı yükleyip sessizce çöker, yt-dlp/ffmpeg de etkilenebilir.
    Bootloader orijinali LD_LIBRARY_PATH_ORIG'de saklar — onu geri koy.
    """
    env = os.environ.copy()
    if getattr(sys, "frozen", False) and sys.platform not in ("win32", "darwin"):
        orig = env.get("LD_LIBRARY_PATH_ORIG")
        if orig:
            env["LD_LIBRARY_PATH"] = orig
        else:
            env.pop("LD_LIBRARY_PATH", None)
    return env


# Gömülü (bundled) yt-dlp + ffmpeg; hiçbir kurulum gerektirmeden çalışır
YTDLP = _find_binary("yt-dlp")
_FFMPEG = _find_binary("ffmpeg")
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
.prow{display:flex;align-items:center;justify-content:space-between;gap:10px}
.pt{font-size:9px;color:rgba(var(--accent),0.5);letter-spacing:1.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1;min-width:0}
.stop{display:none;flex-shrink:0;background:rgba(255,80,60,0.1);border:1px solid rgba(255,80,60,0.32);color:rgba(255,120,100,0.95);border-radius:6px;font-family:'JetBrains Mono',monospace;font-size:10px;padding:5px 13px;cursor:pointer;transition:background .2s,transform .12s,box-shadow .2s}
.stop:hover{background:rgba(255,80,60,0.2);box-shadow:0 0 14px -3px rgba(255,80,60,0.4)}
.stop:active{transform:scale(0.93)}
.stop.show{display:block}
.note{font-size:10px;color:rgba(255,180,80,0.9);line-height:1.5;margin:-10px 0 12px 2px;transition:color .2s ease}
.note:empty{display:none}
.hist{margin-top:18px;padding-top:14px;border-top:1px solid rgba(255,255,255,0.05)}
.hist-lbl{font-size:9px;letter-spacing:2px;color:rgba(255,255,255,0.14);margin-bottom:10px}
.hist-item{display:flex;align-items:center;gap:8px;padding:7px 0;border-bottom:1px solid rgba(255,255,255,0.04);font-size:10px}
.hist-item:last-child{border-bottom:none}
.hist-url{flex:1;color:rgba(255,255,255,0.35);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.hist-meta{color:rgba(255,255,255,0.2);white-space:nowrap;font-size:9px}
.hist-ok{color:rgba(var(--accent),0.6);font-size:9px;white-space:nowrap}
.hist-err{color:rgba(255,100,80,0.6);font-size:9px;white-space:nowrap}
.br-controls{position:fixed;right:16px;bottom:16px;z-index:60;display:flex;gap:8px;align-items:flex-end}
.langbox{position:relative;font-family:'JetBrains Mono',monospace}
.settingsbox{position:relative;font-family:'JetBrains Mono',monospace}
.settings-panel{position:absolute;right:0;bottom:calc(100% + 8px);width:250px;background:rgba(14,14,18,0.97);backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);border:1px solid rgba(255,255,255,0.1);border-radius:12px;padding:14px 15px;opacity:0;max-height:0;overflow:hidden;transform:translateY(8px) scale(0.97);transform-origin:bottom right;pointer-events:none;transition:opacity .2s ease,transform .22s cubic-bezier(.2,.9,.3,1.25),max-height .28s ease;box-shadow:0 16px 40px -12px rgba(0,0,0,0.85)}
.settings-panel.open{opacity:1;max-height:340px;transform:translateY(0) scale(1);pointer-events:auto}
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
.theme-menu.open{opacity:1;max-height:260px;transform:translateY(0) scale(1);pointer-events:auto}
.theme-opt{display:flex;align-items:center;gap:10px;width:100%;background:transparent;border:none;border-radius:7px;color:rgba(255,255,255,0.62);font-family:inherit;font-size:11px;text-align:left;padding:8px 10px;cursor:pointer;transition:background .15s,color .15s}
.theme-opt:hover{background:rgba(255,255,255,0.06);color:rgba(255,255,255,0.95)}
.theme-opt.active{color:rgba(255,255,255,0.95)}
.theme-opt .dot{width:13px;height:13px;border-radius:50%;flex-shrink:0}
.theme-opt.active .dot{box-shadow:0 0 0 2px rgba(255,255,255,0.25)}
.theme-opt .nm{flex:1}
.theme-opt.active .nm::after{content:'✓';margin-left:6px;opacity:.7}
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
          <button class="chip on" data-g="cont" data-v="mp4" onclick="pick(this)">MP4</button>
          <button class="chip"    data-g="cont" data-v="mkv" onclick="pick(this)">MKV</button>
        </div>
      </div>
      <div class="row" style="animation-delay:.24s">
        <span class="lbl" data-i18n="options">Options</span>
        <div class="chips">
          <button class="chip" id="subsbtn" onclick="toggleFlag('subs',this)" data-i18n="subtitles">Subtitles</button>
          <button class="chip" id="mutebtn" onclick="toggleFlag('mute',this)" data-i18n="mute">Mute</button>
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
      <div class="hist-lbl" data-i18n="history">History</div>
      <div id="hist-list"></div>
    </div>
  </div>
</div>
<div class="br-controls">
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
const state={mode:'video',vq:'1080p',cont:'mp4',fmt:'mp3',br:'192k',cookies:'none',subs:false,mute:false,playlist:false};
// ── çok dilli metinler (i18n) ──
const I18N={
 en:{urlPlaceholder:"Paste a video link — any site works",download:"Download",mode:"Mode",video:"Video",audio:"Audio",quality:"Quality",best:"Best",container:"Container",options:"Options",subtitles:"Subtitles",mute:"Mute",format:"Format",bitrate:"Bitrate",source:"Source",cookies:"Cookies",none:"None",cookiesHint:"Use your browser's session to download from sites where you must be logged in (your own account). Close that browser first.",playlist:"Playlist",downloadPlaylist:"Download Playlist",folder:"Folder",history:"History",stop:"Stop",connecting:"connecting...",starting:"starting...",downloading:"Downloading",processing:"processing...",completed:"completed ✓",stopping:"stopping...",stopped:"stopped",errorGeneric:"an error occurred — check the console",connError:"connection error",mixWarn:"⚠ YouTube Mix (radio) is endless — only the first 50 videos will be downloaded.",mixInfo:"ℹ This is a Mix link. Playlist is off, only this video will download.",appClosed:"⚠ Aevum has quit — relaunch the app to continue."},
 tr:{urlPlaceholder:"Video bağlantısını yapıştır — her site desteklenir",download:"İndir",mode:"Mod",video:"Video",audio:"Ses",quality:"Kalite",best:"En İyi",container:"Biçim",options:"Seçenek",subtitles:"Altyazı",mute:"Sessiz",format:"Format",bitrate:"Bit Hızı",source:"Kaynak",cookies:"Çerezler",none:"Yok",cookiesHint:"Giriş yapman gereken sitelerden (kendi hesabınla) indirmek için tarayıcının oturumunu kullanır. O tarayıcıyı önce kapat.",playlist:"Liste",downloadPlaylist:"Oynatma Listesini İndir",folder:"Klasör",history:"Geçmiş",stop:"Durdur",connecting:"bağlanıyor...",starting:"başlatılıyor...",downloading:"İndiriliyor",processing:"işleniyor...",completed:"tamamlandı ✓",stopping:"durduruluyor...",stopped:"durduruldu",errorGeneric:"hata oluştu — konsolu kontrol et",connError:"bağlantı hatası",mixWarn:"⚠ YouTube Mix (radyo) listesi sonsuzdur — ilk 50 video indirilecek.",mixInfo:"ℹ Bu bir Mix bağlantısı. Liste kapalı, yalnızca bu video inecek.",appClosed:"⚠ Aevum kapandı — devam etmek için uygulamayı yeniden başlat."},
 es:{urlPlaceholder:"Pega un enlace de vídeo — cualquier sitio funciona",download:"Descargar",mode:"Modo",video:"Vídeo",audio:"Audio",quality:"Calidad",best:"La mejor",container:"Formato",options:"Opciones",subtitles:"Subtítulos",mute:"Silenciar",format:"Formato",bitrate:"Bitrate",source:"Original",cookies:"Cookies",none:"Ninguno",cookiesHint:"Usa la sesión de tu navegador para descargar de sitios donde debes iniciar sesión (tu propia cuenta). Cierra ese navegador primero.",playlist:"Lista",downloadPlaylist:"Descargar lista",folder:"Carpeta",history:"Historial",stop:"Detener",connecting:"conectando...",starting:"iniciando...",downloading:"Descargando",processing:"procesando...",completed:"completado ✓",stopping:"deteniendo...",stopped:"detenido",errorGeneric:"ocurrió un error — revisa la consola",connError:"error de conexión",mixWarn:"⚠ La Mix (radio) de YouTube es infinita — solo se descargarán los primeros 50 vídeos.",mixInfo:"ℹ Es un enlace Mix. La lista está desactivada, solo se descargará este vídeo.",appClosed:"⚠ Aevum se ha cerrado — vuelve a abrir la aplicación para continuar."},
 de:{urlPlaceholder:"Video-Link einfügen — jede Seite funktioniert",download:"Herunterladen",mode:"Modus",video:"Video",audio:"Audio",quality:"Qualität",best:"Beste",container:"Format",options:"Optionen",subtitles:"Untertitel",mute:"Stumm",format:"Format",bitrate:"Bitrate",source:"Quelle",cookies:"Cookies",none:"Keine",cookiesHint:"Nutzt die Sitzung deines Browsers, um von Seiten herunterzuladen, bei denen du angemeldet sein musst (dein eigenes Konto). Schließe diesen Browser zuerst.",playlist:"Playlist",downloadPlaylist:"Playlist herunterladen",folder:"Ordner",history:"Verlauf",stop:"Stopp",connecting:"verbinde...",starting:"starte...",downloading:"Wird geladen",processing:"verarbeite...",completed:"fertig ✓",stopping:"stoppe...",stopped:"gestoppt",errorGeneric:"ein Fehler ist aufgetreten — Konsole prüfen",connError:"Verbindungsfehler",mixWarn:"⚠ YouTube-Mix (Radio) ist endlos — nur die ersten 50 Videos werden geladen.",mixInfo:"ℹ Dies ist ein Mix-Link. Playlist ist aus, nur dieses Video wird geladen.",appClosed:"⚠ Aevum wurde beendet — starte die App neu, um fortzufahren."},
 fr:{urlPlaceholder:"Colle un lien vidéo — tous les sites marchent",download:"Télécharger",mode:"Mode",video:"Vidéo",audio:"Audio",quality:"Qualité",best:"Meilleure",container:"Format",options:"Options",subtitles:"Sous-titres",mute:"Muet",format:"Format",bitrate:"Débit",source:"Source",cookies:"Cookies",none:"Aucun",cookiesHint:"Utilise la session de ton navigateur pour télécharger depuis les sites où tu dois être connecté (ton propre compte). Ferme d'abord ce navigateur.",playlist:"Playlist",downloadPlaylist:"Télécharger la playlist",folder:"Dossier",history:"Historique",stop:"Arrêter",connecting:"connexion...",starting:"démarrage...",downloading:"Téléchargement",processing:"traitement...",completed:"terminé ✓",stopping:"arrêt...",stopped:"arrêté",errorGeneric:"une erreur s'est produite — vérifie la console",connError:"erreur de connexion",mixWarn:"⚠ Le Mix (radio) YouTube est infini — seules les 50 premières vidéos seront téléchargées.",mixInfo:"ℹ C'est un lien Mix. La playlist est désactivée, seule cette vidéo sera téléchargée.",appClosed:"⚠ Aevum s'est fermé — relance l'application pour continuer."},
 it:{urlPlaceholder:"Incolla un link video — funziona con qualsiasi sito",download:"Scarica",mode:"Modalità",video:"Video",audio:"Audio",quality:"Qualità",best:"Migliore",container:"Formato",options:"Opzioni",subtitles:"Sottotitoli",mute:"Muto",format:"Formato",bitrate:"Bitrate",source:"Originale",cookies:"Cookie",none:"Nessuno",cookiesHint:"Usa la sessione del tuo browser per scaricare dai siti dove devi aver effettuato l'accesso (il tuo account). Chiudi prima quel browser.",playlist:"Playlist",downloadPlaylist:"Scarica playlist",folder:"Cartella",history:"Cronologia",stop:"Ferma",connecting:"connessione...",starting:"avvio...",downloading:"Scaricamento",processing:"elaborazione...",completed:"completato ✓",stopping:"arresto...",stopped:"fermato",errorGeneric:"si è verificato un errore — controlla la console",connError:"errore di connessione",mixWarn:"⚠ Il Mix (radio) di YouTube è infinito — verranno scaricati solo i primi 50 video.",mixInfo:"ℹ Questo è un link Mix. La playlist è disattivata, verrà scaricato solo questo video.",appClosed:"⚠ Aevum si è chiuso — riavvia l'applicazione per continuare."},
 pt:{urlPlaceholder:"Cole um link de vídeo — qualquer site funciona",download:"Baixar",mode:"Modo",video:"Vídeo",audio:"Áudio",quality:"Qualidade",best:"Melhor",container:"Formato",options:"Opções",subtitles:"Legendas",mute:"Mudo",format:"Formato",bitrate:"Bitrate",source:"Original",cookies:"Cookies",none:"Nenhum",cookiesHint:"Usa a sessão do seu navegador para baixar de sites onde você precisa estar logado (sua própria conta). Feche esse navegador primeiro.",playlist:"Playlist",downloadPlaylist:"Baixar playlist",folder:"Pasta",history:"Histórico",stop:"Parar",connecting:"conectando...",starting:"iniciando...",downloading:"Baixando",processing:"processando...",completed:"concluído ✓",stopping:"parando...",stopped:"parado",errorGeneric:"ocorreu um erro — verifique o console",connError:"erro de conexão",mixWarn:"⚠ O Mix (rádio) do YouTube é infinito — apenas os primeiros 50 vídeos serão baixados.",mixInfo:"ℹ Este é um link Mix. A playlist está desligada, apenas este vídeo será baixado.",appClosed:"⚠ O Aevum foi encerrado — reabra o aplicativo para continuar."},
 ru:{urlPlaceholder:"Вставьте ссылку на видео — подходит любой сайт",download:"Скачать",mode:"Режим",video:"Видео",audio:"Аудио",quality:"Качество",best:"Лучшее",container:"Формат",options:"Опции",subtitles:"Субтитры",mute:"Без звука",format:"Формат",bitrate:"Битрейт",source:"Источник",cookies:"Cookies",none:"Нет",cookiesHint:"Использует сессию вашего браузера для загрузки с сайтов, где нужен вход (ваш аккаунт). Сначала закройте этот браузер.",playlist:"Плейлист",downloadPlaylist:"Скачать плейлист",folder:"Папка",history:"История",stop:"Стоп",connecting:"подключение...",starting:"запуск...",downloading:"Загрузка",processing:"обработка...",completed:"готово ✓",stopping:"остановка...",stopped:"остановлено",errorGeneric:"произошла ошибка — проверьте консоль",connError:"ошибка соединения",mixWarn:"⚠ YouTube Mix (радио) бесконечен — будут загружены только первые 50 видео.",mixInfo:"ℹ Это ссылка Mix. Плейлист выключен, будет загружено только это видео.",appClosed:"⚠ Aevum завершил работу — перезапустите приложение, чтобы продолжить."}
};
const LANGS=[['en','English'],['tr','Türkçe'],['es','Español'],['de','Deutsch'],['fr','Français'],['it','Italiano'],['pt','Português'],['ru','Русский']];
const langMenu=document.getElementById('langMenu'),langCode=document.getElementById('langCode'),langbox=document.getElementById('langbox');
let curLang=localStorage.getItem('vdl_lang')||'en';
function T(k){const L=I18N[curLang]||I18N.en;return L[k]!==undefined?L[k]:(I18N.en[k]!==undefined?I18N.en[k]:k);}
function renderTexts(){document.querySelectorAll('[data-i18n]').forEach(el=>{el.textContent=T(el.dataset.i18n);});document.querySelectorAll('[data-i18n-ph]').forEach(el=>{el.placeholder=T(el.dataset.i18nPh);});}
function buildLangMenu(){langMenu.innerHTML=LANGS.map(([c,n])=>'<button class="lang-opt'+(c===curLang?' active':'')+'" data-lang="'+c+'" onclick="selectLang(\\''+c+'\\')"><span>'+n+'</span><span class="lc">'+c.toUpperCase()+'</span></button>').join('');}
function applyLang(l){curLang=l;localStorage.setItem('vdl_lang',l);document.documentElement.lang=l;langCode.textContent=l.toUpperCase();renderTexts();updateNote();buildLangMenu();buildThemeMenu();renderSettings();}
function selectLang(l){applyLang(l);saveCfg({lang:l});closeLangMenu();}
function toggleLangMenu(e){e.stopPropagation();langMenu.classList.toggle('open');}
function closeLangMenu(){langMenu.classList.remove('open');}
document.addEventListener('click',e=>{if(langbox&&!langbox.contains(e.target))closeLangMenu();});
function statusText(d){const tag=d.item?'['+d.item+'] ':'';switch(d.code){case 'download':return tag+T('downloading')+' '+(d.progress||0)+'%';case 'process':return tag+T('processing');case 'start':return T('starting');case 'done':return T('completed');case 'stopped':return T('stopped');case 'error':return d.error_line?d.error_line.slice(0,110):T('errorGeneric');default:return '';}}
// ── temalar (renk vurgusu) ──
const THEME_LIST=[['green','0,230,160'],['blue','96,165,250'],['purple','167,139,250'],['dynamic',null]];
const THEME_NAMES={en:{green:'Green',blue:'Blue',purple:'Purple',dynamic:'Dynamic'},tr:{green:'Yeşil',blue:'Mavi',purple:'Mor',dynamic:'Değişken'},es:{green:'Verde',blue:'Azul',purple:'Púrpura',dynamic:'Dinámico'},de:{green:'Grün',blue:'Blau',purple:'Lila',dynamic:'Dynamisch'},fr:{green:'Vert',blue:'Bleu',purple:'Violet',dynamic:'Dynamique'},it:{green:'Verde',blue:'Blu',purple:'Viola',dynamic:'Dinamico'},pt:{green:'Verde',blue:'Azul',purple:'Roxo',dynamic:'Dinâmico'},ru:{green:'Зелёный',blue:'Синий',purple:'Фиолетовый',dynamic:'Динамический'}};
function TT(id){const L=THEME_NAMES[curLang]||THEME_NAMES.en;return L[id]||THEME_NAMES.en[id]||id;}
const themeMenu=document.getElementById('themeMenu'),themebox=document.getElementById('themebox');
let accentRGB='0,230,160';
let curTheme=localStorage.getItem('aevum_theme')||'green';
let dynamicActive=false,dynHue=155;
function rgbToHue(r,g,b){r/=255;g/=255;b/=255;const mx=Math.max(r,g,b),mn=Math.min(r,g,b),d=mx-mn;let h=0;if(d){if(mx===r)h=((g-b)/d)%6;else if(mx===g)h=(b-r)/d+2;else h=(r-g)/d+4;h*=60;if(h<0)h+=360;}return h;}
function setAccent(rgb){accentRGB=rgb;const ds=document.documentElement.style;ds.setProperty('--accent',rgb);const p=rgb.split(','),h=rgbToHue(+p[0],+p[1],+p[2]);ds.setProperty('--aura1',hslToRgb(((h-60+360)%360)/360,0.72,0.55));ds.setProperty('--aura2',hslToRgb(h/360,0.72,0.55));ds.setProperty('--aura3',hslToRgb(((h+60)%360)/360,0.72,0.55));}
function hslToRgb(h,s,l){let r,g,b;if(s===0){r=g=b=l;}else{const f=(p,q,t)=>{if(t<0)t+=1;if(t>1)t-=1;if(t<1/6)return p+(q-p)*6*t;if(t<1/2)return q;if(t<2/3)return p+(q-p)*(2/3-t)*6;return p;};const q=l<0.5?l*(1+s):l+s-l*s,p=2*l-q;r=f(p,q,h+1/3);g=f(p,q,h);b=f(p,q,h-1/3);}return Math.round(r*255)+','+Math.round(g*255)+','+Math.round(b*255);}
function applyTheme(t){if(!THEME_LIST.some(x=>x[0]===t))t='green';curTheme=t;localStorage.setItem('aevum_theme',t);dynamicActive=(t==='dynamic');if(!dynamicActive){setAccent(THEME_LIST.find(x=>x[0]===t)[1]);}buildThemeMenu();}
function buildThemeMenu(){themeMenu.innerHTML=THEME_LIST.map(([id,rgb])=>{const dot=rgb?('background:rgb('+rgb+')'):('background:conic-gradient(from 0deg,#ff5b5b,#ffd93b,#4be08a,#4aa3ff,#a77bff,#ff5b5b)');return '<button class="theme-opt'+(id===curTheme?' active':'')+'" onclick="selectTheme(\\''+id+'\\')"><span class="dot" style="'+dot+'"></span><span class="nm">'+TT(id)+'</span></button>';}).join('');}
function selectTheme(t){applyTheme(t);saveCfg({theme:t});closeThemeMenu();}
function toggleThemeMenu(e){e.stopPropagation();themeMenu.classList.toggle('open');}
function closeThemeMenu(){themeMenu.classList.remove('open');}
document.addEventListener('click',e=>{if(themebox&&!themebox.contains(e.target))closeThemeMenu();});
// ── ayarlar ──
const SETTINGS_TEXT={
 en:{settings:'Settings',startup:'Launch at startup',startupHint:'Aevum starts with the system and waits quietly in the tray — open it whenever you need it.',menu:'Add to app menu',menuHint:'Installs Aevum into your app menu — launch it like a regular app, no terminal needed.'},
 tr:{settings:'Ayarlar',startup:'Başlangıçta aç',startupHint:'Aevum, sistemle birlikte başlar ve tepside sessizce bekler — gerektiğinde açarsın.',menu:'Uygulama menüsüne kur',menuHint:"Aevum'u uygulama menüsüne kurar — terminale gerek kalmadan normal bir uygulama gibi başlatırsın."},
 es:{settings:'Ajustes',startup:'Abrir al inicio',startupHint:'Aevum se inicia con el sistema y espera en la bandeja — ábrelo cuando lo necesites.',menu:'Añadir al menú',menuHint:'Instala Aevum en el menú de aplicaciones — ábrelo como una app normal, sin terminal.'},
 de:{settings:'Einstellungen',startup:'Beim Start öffnen',startupHint:'Aevum startet mit dem System und wartet im Infobereich — öffne es bei Bedarf.',menu:'Zum App-Menü hinzufügen',menuHint:'Installiert Aevum ins Anwendungsmenü — starte es wie eine normale App, ohne Terminal.'},
 fr:{settings:'Paramètres',startup:'Lancer au démarrage',startupHint:'Aevum démarre avec le système et attend dans la barre — ouvre-le au besoin.',menu:'Ajouter au menu',menuHint:"Installe Aevum dans le menu des applications — lance-le comme une app normale, sans terminal."},
 it:{settings:'Impostazioni',startup:"Avvia all'avvio",startupHint:'Aevum si avvia con il sistema e resta nella barra — aprilo quando serve.',menu:'Aggiungi al menu',menuHint:'Installa Aevum nel menu delle applicazioni — avvialo come una normale app, senza terminale.'},
 pt:{settings:'Configurações',startup:'Abrir ao iniciar',startupHint:'O Aevum inicia com o sistema e espera na bandeja — abra quando precisar.',menu:'Adicionar ao menu',menuHint:'Instala o Aevum no menu de aplicativos — abra como um app normal, sem terminal.'},
 ru:{settings:'Настройки',startup:'Запуск при старте',startupHint:'Aevum запускается вместе с системой и ждёт в трее — откройте, когда понадобится.',menu:'Добавить в меню',menuHint:'Устанавливает Aevum в меню приложений — запускайте как обычное приложение, без терминала.'}
};
const settingsPanel=document.getElementById('settingsPanel'),settingsbox=document.getElementById('settingsbox'),settingsTitle=document.getElementById('settingsTitle'),settingsStartupLabel=document.getElementById('settingsStartupLabel'),settingsHint=document.getElementById('settingsHint'),startupToggle=document.getElementById('startupToggle');
const menuRow=document.getElementById('menuRow'),menuToggle=document.getElementById('menuToggle'),settingsMenuLabel=document.getElementById('settingsMenuLabel'),settingsMenuHint=document.getElementById('settingsMenuHint'),startupRow=document.getElementById('startupRow');
function TS(k){const L=SETTINGS_TEXT[curLang]||SETTINGS_TEXT.en;return L[k]||SETTINGS_TEXT.en[k]||k;}
function renderSettings(){settingsTitle.textContent=TS('settings');settingsStartupLabel.textContent=TS('startup');settingsHint.textContent=TS('startupHint');settingsMenuLabel.textContent=TS('menu');settingsMenuHint.textContent=TS('menuHint');}
function toggleSettings(e){e.stopPropagation();settingsPanel.classList.toggle('open');}
function closeSettings(){settingsPanel.classList.remove('open');}
document.addEventListener('click',e=>{if(settingsbox&&!settingsbox.contains(e.target))closeSettings();});
function saveCfg(o){fetch('/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(o)}).catch(()=>{});}
function loadSettings(){fetch('/settings').then(r=>r.json()).then(s=>{startupToggle.checked=!!s.startup;menuToggle.checked=!!s.menu;const cs=s.canStartup!==false;startupRow.style.display=cs?'flex':'none';settingsHint.style.display=cs?'block':'none';menuRow.style.display=s.canMenu?'flex':'none';menuRow.style.marginTop=cs?'13px':'0';settingsMenuHint.style.display=s.canMenu?'block':'none';if(s.lang&&I18N[s.lang]&&s.lang!==curLang)applyLang(s.lang);if(s.theme&&THEME_LIST.some(x=>x[0]===s.theme)&&s.theme!==curTheme)applyTheme(s.theme);}).catch(()=>{});}
function setStartup(on){fetch('/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({startup:on})}).catch(()=>{});}
function setMenu(on){menuToggle.disabled=true;fetch('/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({menu:on})}).then(r=>r.json()).then(s=>{menuToggle.checked=!!s.menu;}).catch(()=>{menuToggle.checked=!on;}).finally(()=>{menuToggle.disabled=false;});}
// ── tüm pencereyi kaplayan etkileşimli parçacık ağı ──
const cv=document.getElementById('bg'),cx=cv.getContext('2d',{alpha:true});
const REDUCE=!!(window.matchMedia&&window.matchMedia('(prefers-reduced-motion: reduce)').matches);
const REP=130,REP2=REP*REP,CON=110,CON2=CON*CON;   // itme / bağlantı mesafeleri (canvas uzayı)
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
  if(dynamicActive){dynHue=(dynHue+0.05*dt)%360;setAccent(hslToRgb(dynHue/360,0.55,0.62));}
  applyTilt();
  const mx=ptr.inside?ptr.x*scale:-99999,my=ptr.inside?ptr.y*scale:-99999;
  for(const p of parts){p.x+=p.vx*dt;p.y+=p.vy*dt;if(p.x<0||p.x>W)p.vx*=-1;if(p.y<0||p.y>H)p.vy*=-1;const dx=p.x-mx,dy=p.y-my,d2=dx*dx+dy*dy;if(d2<REP2){const d=Math.sqrt(d2)||1,f=(REP-d)/REP*1.1*dt;p.x+=dx/d*f;p.y+=dy/d*f;}}
  drawScene();
  // ── donanıma uyum: yavaşsa parçacık azalt, akıcıysa geri ekle ──
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
inp.addEventListener('input',()=>{gb.disabled=!inp.value.trim();updateNote();});
inp.addEventListener('keydown',e=>{if(e.key==='Enter'&&!gb.disabled)go();});
function updateNote(){const mix=/[?&]list=RD/i.test(inp.value);note.textContent=(mix&&state.playlist)?T('mixWarn'):(mix&&!state.playlist)?T('mixInfo'):'';}
function pick(btn){const g=btn.dataset.g;state[g]=btn.dataset.v;document.querySelectorAll('[data-g="'+g+'"]').forEach(b=>b.classList.remove('on','mode-on'));btn.classList.add(g==='mode'?'mode-on':'on');btn.classList.remove('pop');void btn.offsetWidth;btn.classList.add('pop');}
function toggleFlag(key,btn){state[key]=!state[key];btn.classList.toggle('tog-on',state[key]);btn.classList.remove('pop');void btn.offsetWidth;btn.classList.add('pop');if(key==='playlist')updateNote();}
function setMode(m){const vr=document.getElementById('vrows'),ar=document.getElementById('arows');if(m==='audio'){requestAnimationFrame(()=>{vr.classList.add('hide');vr.style.maxHeight='0'});ar.classList.remove('hide');ar.style.maxHeight='200px';ar.style.opacity='1';}else{requestAnimationFrame(()=>{ar.classList.add('hide');ar.style.maxHeight='0'});vr.classList.remove('hide');vr.style.maxHeight='240px';vr.style.opacity='1';}}
function go(){const url=inp.value.trim();if(!url||gb.disabled)return;const dir=document.getElementById('dir').value.trim();gb.disabled=true;pw.classList.add('show');stopbtn.classList.add('show');pf.style.width='5%';pt.textContent=T('connecting');pt.style.color='rgba(var(--accent),0.5)';fetch('/download',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url,...state,dir})}).then(r=>r.json()).then(d=>{jobId=d.job_id;poll();}).catch(()=>{gb.disabled=false;stopbtn.classList.remove('show');pt.textContent=T('connError');pt.style.color='rgba(255,100,80,0.8)';});}
function cancelJob(){if(!jobId)return;stopbtn.classList.remove('show');pt.textContent=T('stopping');pt.style.color='rgba(255,150,90,0.9)';fetch('/cancel/'+jobId,{method:'POST'});}
function poll(){if(pollTimer)clearInterval(pollTimer);pollTimer=setInterval(()=>{fetch('/status/'+jobId).then(r=>r.json()).then(d=>{if(d.progress!==undefined)pf.style.width=d.progress+'%';pt.textContent=statusText(d);if(!d.done)pt.style.color=(d.code==='process')?'rgba(var(--accent),0.6)':'rgba(var(--accent),0.5)';if(d.done){clearInterval(pollTimer);gb.disabled=false;stopbtn.classList.remove('show');if(d.success){pf.style.width='100%';pt.style.color='rgba(var(--accent),0.7)';}else if(d.code==='stopped'){pf.style.width='0%';pt.style.color='rgba(255,150,90,0.9)';}else{pt.style.color='rgba(255,100,80,0.8)';}loadHistory();}});},700);}
function loadHistory(){fetch('/history').then(r=>r.json()).then(items=>{if(!items.length)return;document.getElementById('hist-wrap').style.display='block';document.getElementById('hist-list').innerHTML=items.map(h=>`<div class="hist-item"><span class="hist-url">${h.url}</span><span class="hist-meta">${h.meta}</span><span class="${h.success?'hist-ok':'hist-err'}">${h.success?'✓':'✗'}</span></div>`).join('');});}
applyTheme(curTheme);
applyLang(curLang);
loadSettings();
loadHistory();
// ── yaşam sinyali: Linux'ta sekme kapanınca uygulama kendini kapatır ──
const CID=Math.random().toString(36).slice(2)+Date.now().toString(36);
let pingFails=0;
function showDead(){if(document.getElementById('deadbar'))return;const b=document.createElement('div');b.id='deadbar';b.textContent=T('appClosed');b.style.cssText='position:fixed;top:14px;left:50%;transform:translateX(-50%);z-index:99;background:rgba(40,12,10,0.95);border:1px solid rgba(255,100,80,0.45);color:rgba(255,150,130,0.95);font-family:inherit;font-size:11px;padding:9px 16px;border-radius:9px;box-shadow:0 10px 30px -8px rgba(0,0,0,0.8)';document.body.appendChild(b);}
setInterval(()=>{fetch('/ping?id='+CID,{cache:'no-store'}).then(()=>{pingFails=0;const b=document.getElementById('deadbar');if(b)b.remove();}).catch(()=>{if(++pingFails>=4)showDead();});},3000);
// sekme kapanırken haber ver (hızlı, temiz çıkış; yenilemede yeni sayfa hemen geri bağlanır)
window.addEventListener('pagehide',()=>{try{navigator.sendBeacon('/bye?id='+CID);}catch(e){}});
// ilk açılışta ayarları göster (bir kez)
if(!localStorage.getItem('aevum_onboarded')){setTimeout(()=>settingsPanel.classList.add('open'),700);localStorage.setItem('aevum_onboarded','1');}
</script>
</body>
</html>"""


HEIGHT_MAP = {"4k": "2160", "1440p": "1440", "1080p": "1080",
              "720p": "720", "480p": "480", "360p": "360"}

MIX_CAP = 50  # YouTube Mix/radyo listeleri sonsuzdur; bu kadarla sınırla


def playlist_id(url: str) -> str:
    try:
        return (parse_qs(urlparse(url).query).get("list", [""])[0]) or ""
    except Exception:
        return ""


def is_mix_playlist(url: str) -> bool:
    # YouTube Mix/radyo listelerinin kimliği "RD" ile başlar (RDMM, RDCLAK, RDEM...)
    return playlist_id(url).startswith("RD")


def kill_process_tree(pid: int):
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           creationflags=subprocess.CREATE_NO_WINDOW,
                           capture_output=True)
        else:
            os.killpg(os.getpgid(pid), 15)
    except Exception:
        pass


def build_video_format(vq: str, container: str, mute: bool) -> str:
    """Siteden bağımsız biçim seçici. Ayrık akış yoksa birleşik akışa düşer."""
    h = HEIGHT_MAP.get(vq)
    hc = f"[height<={h}]" if h else ""
    if mute:
        # yalnızca görüntü
        return f"bestvideo{hc}/best{hc}/best"
    if container == "mp4":
        # Önce mp4/m4a uyumlu akışlar, sonra en iyi, sonra birleşik
        return (f"bestvideo{hc}[ext=mp4]+bestaudio[ext=m4a]/"
                f"bestvideo{hc}+bestaudio/"
                f"best{hc}/best")
    return f"bestvideo{hc}+bestaudio/best{hc}/best"


def build_cmd(data: dict, output_dir: str) -> list:
    url  = data["url"]
    mode = data.get("mode", "video")
    is_playlist = bool(data.get("playlist"))

    if is_playlist:
        # Liste adında bir alt klasör oluştur, içine sıralı numarayla indir
        out = os.path.join(output_dir,
                           "%(playlist_title|Playlist)s",
                           "%(autonumber)03d - %(title).150B [%(id)s].%(ext)s")
    else:
        out = os.path.join(output_dir, "%(title).180B [%(id)s].%(ext)s")

    cmd = [YTDLP, "--newline", "--add-metadata", "--no-mtime",
           "--retries", "10", "--fragment-retries", "10",
           "--concurrent-fragments", "4", "-o", out]

    # Gömülü ffmpeg'i kullan (birleştirme, ses çıkarma, altyazı gömme için gerekli)
    if FFMPEG_DIR:
        cmd += ["--ffmpeg-location", FFMPEG_DIR]

    # Oynatma listesi: tüm listeyi indir, bozuk bir videoda durma
    if is_playlist:
        cmd += ["--yes-playlist", "--ignore-errors"]
        # Mix/radyo listeleri sonsuz olduğundan makul bir sınır koy
        if is_mix_playlist(url):
            cmd += ["--playlist-end", str(MIX_CAP)]
    else:
        cmd.append("--no-playlist")

    # Tarayıcı çerezleri (giriş gerektiren siteler için)
    cookies = data.get("cookies", "none")
    if cookies and cookies != "none":
        cmd += ["--cookies-from-browser", cookies]

    if mode == "video":
        vq   = data.get("vq", "1080p")
        cont = data.get("cont", "mp4")
        mute = bool(data.get("mute", False))
        fmt  = build_video_format(vq, cont, mute)
        cmd += ["-f", fmt, "--merge-output-format", cont]
        if data.get("subs"):
            # Altyazı = en iyi çaba. YouTube altyazı uç noktası sık sık HTTP 429
            # döndürür; --ignore-errors bunun indirmeyi iptal etmesini engeller
            # (video yine iner). --convert-subs srt daha uyumlu gömme sağlar.
            cmd += ["--embed-subs", "--write-auto-subs", "--write-subs",
                    "--sub-langs", "tr,en", "--convert-subs", "srt",
                    "--ignore-errors"]
        cmd.append(url)
        return cmd

    # ── ses modu ──
    afmt = data.get("fmt", "mp3")
    br   = data.get("br", "192k")
    cmd += ["-x", "--audio-format", afmt]
    # Kayıpsız biçimlerde bitrate anlamsız; yalnızca kayıplılara uygula
    if afmt not in ("flac", "wav"):
        cmd += ["--audio-quality", "0" if br == "best" else br.upper()]
    cmd.append(url)
    return cmd


def history_meta(data: dict) -> str:
    if data.get("mode") == "video":
        parts = [data.get("vq", ""), data.get("cont", "")]
        if data.get("mute"):
            parts.append("muted")
        if data.get("subs"):
            parts.append("subs")
        return " ".join(p for p in parts if p)
    return f"{data.get('fmt','')} {data.get('br','')}".strip()


def run_job(job_id: str, data: dict, output_dir: str):
    cmd = build_cmd(data, output_dir)
    with jobs_lock:
        jobs[job_id]["lines"].append("$ " + " ".join(cmd))
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace", bufsize=1,
                                env=_clean_env(),
                                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
        with jobs_lock:
            jobs[job_id]["proc"] = proc
        progress, code, item = 0, "download", ""
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
            elif any(x in line for x in ["[Merger]", "[VideoConvertor]", "[ExtractAudio]",
                                          "[EmbedSubtitle]", "[Metadata]", "[FixupM"]):
                progress, code = 94, "process"
            with jobs_lock:
                jobs[job_id]["lines"].append(line)
                jobs[job_id]["progress"] = progress
                jobs[job_id]["code"] = code
                jobs[job_id]["item"] = item
        proc.wait()
        with jobs_lock:
            cancelled = jobs[job_id].get("cancelled", False)
        success = proc.returncode == 0 and not cancelled
        with jobs_lock:
            jobs[job_id].update({"done": True, "success": success,
                                 "progress": 100 if success else 0})
            jobs[job_id]["code"] = "done" if success else ("stopped" if cancelled else "error")
            if not success and not cancelled:
                errs = [l for l in jobs[job_id]["lines"]
                        if "ERROR" in l or "error:" in l.lower()]
                jobs[job_id]["error_line"] = errs[-1][:140] if errs else ""
        with history_lock:
            download_history.insert(0, {
                "url": data["url"], "meta": history_meta(data),
                "dir": output_dir, "success": success,
            })
    except FileNotFoundError:
        with jobs_lock:
            jobs[job_id].update({"done": True, "success": False, "code": "error",
                                 "error_line": "yt-dlp not found — reinstall Aevum"})
    except Exception as e:
        with jobs_lock:
            jobs[job_id].update({"done": True, "success": False, "code": "error",
                                 "error_line": str(e)[:140]})


@app.route("/")
def index():
    return render_template_string(HTML)


_IS_WINDOWS = sys.platform == "win32"


def _default_download_dir() -> str:
    # Linux/macOS: XDG "İndirilenler" klasörünü kullan (yoksa ~/Downloads)
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


@app.route("/download", methods=["POST"])
def download_route():
    data = request.json or {}
    url  = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "empty URL"}), 400
    raw_dir    = data.get("dir", "").strip()
    output_dir = str(Path(raw_dir).expanduser()) if raw_dir else _default_download_dir()
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    job_id = str(uuid.uuid4())[:8]
    with jobs_lock:
        jobs[job_id] = {"done": False, "success": False, "lines": [], "read_idx": 0,
                        "progress": 0, "code": "start", "item": "", "error_line": "",
                        "output_dir": output_dir}
    threading.Thread(target=run_job, args=(job_id, data, output_dir), daemon=True).start()
    return jsonify({"job_id": job_id})


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
                    "output_dir": job["output_dir"]})


@app.route("/cancel/<job_id>", methods=["POST"])
def cancel_route(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return jsonify({"error": "not found"}), 404
        job["cancelled"] = True
        proc = job.get("proc")
    if proc and proc.poll() is None:
        kill_process_tree(proc.pid)
    return jsonify({"ok": True})


@app.route("/history")
def history_route():
    with history_lock:
        return jsonify(download_history[:20])


@app.route("/fonts/<path:name>")
def fonts_route(name):
    # Uygulamayla gelen yazı tipleri (internet gerektirmez)
    return send_from_directory(os.path.join(_bin_dir(), "fonts"), name, max_age=31536000)


@app.route("/ping")
def ping_route():
    # Sayfanın kalp atışı; before_request _last_seen'i de günceller
    cid = request.args.get("id")
    if cid:
        with _clients_lock:
            _clients[cid] = time.time()
    return jsonify({"ok": True})


@app.route("/bye", methods=["POST", "GET"])
def bye_route():
    # Sekme kapanırken sendBeacon ile gelir → hızlı ve temiz çıkış sağlar.
    # Sayfa yenilemede de tetiklenir ama yeni sayfa hemen yeniden ping atar;
    # watchdog _EXIT_GRACE beklediği için yenileme uygulamayı kapatmaz.
    cid = request.args.get("id")
    if cid:
        with _clients_lock:
            _clients.pop(cid, None)
    return jsonify({"ok": True})


# ── Kalıcı ayarlar (dil, tema): sunucu tarafında config.json ────────────────
# localStorage porta bağlıdır: 5000 doluyken 5001'e düşülürse tarayıcı bunu
# ayrı site sayar ve seçimler kaybolur. Bu yüzden dil/tema sunucuda saklanır.

_cfg_lock = threading.Lock()


def _config_file() -> str:
    if _IS_WINDOWS:
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(base, "Aevum", "config.json")
    cfg = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(cfg, "aevum", "config.json")


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


# ── Ayarlar: başlangıçta otomatik açılma (Windows: registry, Linux: autostart) ──
_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_RUN_NAME = "Aevum"


def _launch_target() -> str:
    # En kalıcı hedef menüye kurulu kopya; sonra $APPIMAGE; sonra frozen exe; sonra dev
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
    # ── Linux: ~/.config/autostart/aevum.desktop yaz/sil ──
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


# ── Linux: uygulama menüsüne kurulum ("indir, kur, kalsın") ──────────────────
# AppImage'ı ~/.local/share/aevum/ altına kopyalar, menüye .desktop + ikon yazar;
# sonrasında terminal gerekmeden menüden normal bir uygulama gibi başlar.

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
    # 1) PyInstaller paketi  2) AppImage kök dizini ($APPDIR)  3) script yanı
    cands = [os.path.join(_bin_dir(), "aevum.png"),
             os.path.join(os.path.dirname(os.path.abspath(__file__)), "aevum.png")]
    if os.environ.get("APPDIR"):
        cands.insert(1, os.path.join(os.environ["APPDIR"], "aevum.png"))
    for cand in cands:
        if os.path.isfile(cand):
            return cand
    return ""


def _refresh_menu_caches():
    """Masaüstü ortamının menü/ikon önbelleğini tazele (varsa; hata önemsiz)."""
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
    # kurulu kopyaya işaret eden autostart girdisi kırık kalmasın
    auto = _linux_autostart_file()
    if os.path.isfile(auto):
        try:
            with open(auto, encoding="utf-8") as f:
                if _installed_appimage() in f.read():
                    os.remove(auto)
        except OSError:
            pass
    _refresh_menu_caches()


@app.route("/settings")
def settings_get():
    cfg = _load_config()
    return jsonify({
        "startup": get_startup_enabled(),
        "menu": get_menu_installed(),
        "canMenu": sys.platform.startswith("linux"),
        # Başlangıçta açılma tepsiye oturmayı gerektirir → yalnızca Windows
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
    # Dil/tema tercihi: porttan bağımsız kalıcılık için sunucuda sakla
    updates = {}
    if isinstance(data.get("lang"), str) and data["lang"]:
        updates["lang"] = data["lang"][:8]
    if isinstance(data.get("theme"), str) and data["theme"]:
        updates["theme"] = data["theme"][:16]
    if updates:
        try:
            _save_config(updates)
        except OSError:
            pass  # config yazılamasa da uygulama çalışmaya devam etsin
    return jsonify({"ok": True, "startup": get_startup_enabled(),
                    "menu": get_menu_installed()})


# ── Tray ikonu oluştur ───────────────────────────────────────────────────────
def create_icon_image():
    """Aevum yörünge ikonu (tepsi): yeşil halka + yıldız + merkez çekirdek"""
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
    """Tarayıcıyı temiz ortamla aç (webbrowser modülü kirli LD_LIBRARY_PATH geçirir)."""
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
    # Kapatırken arka planda çalışan indirmeleri de durdur
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
    # Çalışan indirme süreçleri varsa (takılı kalmış vb.) onları da kapat
    with jobs_lock:
        procs = [j.get("proc") for j in jobs.values()]
    for p in procs:
        if p and p.poll() is None:
            kill_process_tree(p.pid)
    os._exit(0)


def _browser_watchdog():
    """Linux (tepsisiz mod): son sekme kapanınca uygulamayı kapat.

    Sekmeler her 3 sn'de /ping atar ve kapanırken /bye gönderir. Kapanış
    tespiti: canlı sekme kalmadıysa ve son istekten _EXIT_GRACE geçtiyse çık.
    /bye kaybolursa (çökme, elektrik) sekme _CLIENT_STALE sonrası ölü sayılır.
    Aktif bir indirme sürerken asla çıkılmaz — indirme biter, sayfa hâlâ
    kapalıysa o zaman kapanılır.
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
        # Sayfa hiç bağlanmadıysa (tarayıcı yavaş açılıyor olabilir) uzun bekle
        grace = _EXIT_GRACE if _page_seen else _FIRST_GRACE
        if now - _last_seen > grace:
            print("Browser tab closed — shutting down Aevum.", flush=True)
            _shutdown_now()


def main():
    global PORT
    # Port meşgulse boş bir port bul (uygulamanın açılmama sorununu önler)
    PORT = find_free_port(5000)
    flask_thread = threading.Thread(target=start_flask, daemon=True)
    flask_thread.start()
    time.sleep(1.2)

    url = f"http://localhost:{PORT}"
    print(f"Aevum is running -> {url}", flush=True)

    # ── Linux: tepsisiz mod — tarayıcı açılır, sekme kapanınca uygulama kapanır ──
    if not _IS_WINDOWS:
        # Eski sürümün "başlangıçta aç" girdisi tepsisiz modda anlamsız; temizle
        try:
            os.remove(_linux_autostart_file())
        except OSError:
            pass
        # Menüye kurulu değilse yol göster (AppImage)
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

    # ── Windows: tepsi modu ──
    # --startup ile başlatıldıysa (açılışta otomatik) sayfayı açma, sadece tepside dur
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
            # Tepsi başlatılamadı — çökme, sunucuyu ayakta tut.
            print(f"No tray icon ({e.__class__.__name__}); use the browser page, "
                  f"press Ctrl+C to quit.", flush=True)

    # Tepsisiz mod: sunucuyu ayakta tut ki indirmeler yine çalışsın.
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
