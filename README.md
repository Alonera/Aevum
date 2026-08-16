# Aevum

**Aevum** — a simple, self-contained desktop app to download videos and audio from **any site**: YouTube, Vimeo, Twitter/X, Dailymotion, Twitch, and ~1,800 more (anything [yt-dlp](https://github.com/yt-dlp/yt-dlp) supports). No installation, no dependencies: everything is bundled into a single executable (`.exe` on Windows, AppImage on Linux).

**Free, open source, and completely ad-free** — no ads, no tracking, no bundled extras. Just the download.

> **Aevum runs on your machine. It contacts GitHub, and only for updates.**
> Downloading is entirely local — Aevum talks to the video sites you paste and nowhere else, and your files and history never leave your computer. The single exception: when you open **Settings**, it asks GitHub what the newest release is, and what the newest yt-dlp is, because a program cannot know it is out of date without asking. Those requests carry nothing about you or what you have downloaded — the same public read as opening this page in a browser. Never open Settings and it never asks. No analytics, no accounts, no telemetry, nothing reported to the developer. [Details](#is-it-safe--privacy)

![Aevum](docs/screenshot.png)

## Download

Two ways, both on the [**Releases**](../../releases) page:

- **`Aevum-Setup.exe`** — installer. Installs Aevum, with optional Desktop and Start Menu shortcuts, and an uninstaller. No admin rights needed.
- **`Aevum.exe`** — portable. Just double-click; no install.
- **`Aevum-x86_64.AppImage`** — Linux. Make it executable (`chmod +x`) and run; optionally add it to your app menu from Settings inside the app.

Aevum opens its interface in your browser (`localhost`). On **Windows** it lives in your system tray — right-click the tray icon to open or quit. On **Linux** there is no tray: the app runs while its browser tab is open and quits automatically when you close the tab (an active download keeps it alive until it finishes).

### Settings

Click the ⚙️ gear (next to the language picker) to open Settings. **Launch at startup** (Windows only) makes Aevum start with Windows and wait quietly in the tray (it does *not* pop the window open) — open it from the tray icon whenever you need it. On Linux the Settings panel offers **Add to app menu** instead, which installs Aevum as a regular menu app — it copies the AppImage into `~/.local/share/aevum/`, so you can delete the downloaded file afterwards; unticking removes the copy, menu entry and icon. Shown once on first run; changeable anytime.

### First run — the Windows "unknown publisher" warning

Aevum is not code-signed (a signing certificate costs money), so on the first run Windows **SmartScreen** shows a blue *"Windows protected your PC"* box. This is expected for small independent apps — it does **not** mean the app is unsafe. To run it:

1. Click **More info**.
2. Click **Run anyway**.

You only need to do this once.

### Is it safe? / Privacy

- **Open source** — all the code is in this repository; you can read exactly what it does.
- **Runs locally — with one exception, and here it is** — downloading is entirely local: Aevum talks only to the video sites you paste, and your files never leave your machine. There are no analytics, no accounts and no telemetry, and nothing is reported to the developer. The one thing that is not local is checking for updates, because a program cannot know it is out of date without asking. From 1.2.5, opening Settings makes a single HTTPS request to `api.github.com` for this repository's newest release; that is how the version line knows whether you are behind. From 1.2.6 it makes a second one, to the yt-dlp nightly repository, for the same reason on the Packages line. Both happen only when you open that panel — never at launch, never in the background — and the answers are reused for fifteen minutes. Nothing about you or about what you have downloaded goes with it: it is the same public read as opening the releases page in a browser. Never open Settings and Aevum never contacts GitHub at all.
- **Verify your download** — each release includes `checksums.txt` (SHA‑256). You can confirm the file you downloaded matches. A VirusTotal scan link is provided in the release notes.

## Features

- **Any site** — powered by yt-dlp, works far beyond YouTube.
- **Video or audio** — pick resolution (up to 4K), container (MP4/MKV/WebM, plus an editor-friendly H.264 preset), or extract audio (MP3/M4A/Opus/FLAC/WAV) at your chosen bitrate.
- **Preview before you download** — paste a link and see the title, duration and thumbnail; resolutions the video doesn't offer are dimmed, with size hints on the rest.
- **Clips** — download just a section (start → end). Frame-exact by default at full speed; an optional lossless mode keeps the original stream bytes. Works for audio too.
- **Thumbnail** — save the cover image alongside the video.
- **Subtitles** — embeds the uploader's own subtitles into the file (best-effort; never blocks the download).
- **Playlists** — download a whole playlist into an auto-created folder; endless YouTube Mixes are safely capped.
- **Login-only content** — use your browser's cookies to download from sites where you're signed in (your own account). Firefox works. Chrome, Edge and Brave no longer hand their cookies to any other program, Chrome's own change, and nothing outside Chrome can undo it.
- **Queue** — press Download while one is running and the next link lines up behind it. Every entry gets its own row with the size, speed and time left, and you can drop one back out of the queue.
- **Stop button** — cancel the running download at any time.
- **Updates itself** — Settings names the version you are running and asks GitHub for the newest one when you open the panel. That check is a network request; it is one of only two Aevum makes that are not to a site you pasted. What it fetches is checked against the SHA-256 the release publishes before anything is opened.
- **Keeps yt-dlp current on its own** — the sites keep changing and yt-dlp keeps up, usually within days, while a release here takes weeks. The Packages line in Settings updates it in place, so a download that broke because YouTube moved something can be fixed without waiting for a new Aevum. It goes into your own folder, never the installed package, and one click puts the bundled version back. This is the second of the two requests above.
- **8 languages** — English, Türkçe, Español, Deutsch, Français, Italiano, Português, Русский. Your choice is remembered.
- **Self-contained** — yt-dlp, FFmpeg and ffprobe are bundled; nothing else to install.
- **No ads, no tracking** — no adware, no bundled toolbars, no telemetry. Everything runs locally.

## Legal / usage

Aevum is a general-purpose front-end for yt-dlp. Only download content you have the right to save — your own uploads, Creative Commons material, sites that permit downloading, or your own paid accounts. You are responsible for how you use it. This project does not endorse or enable copyright infringement.

## How it was built (honesty note)

Aevum was **vibe-coded with the help of an AI assistant** — it was written collaboratively with AI rather than hand-coded line by line. I'm stating this openly so no one is misled about how it came to be.

The real heavy lifting is done by two excellent open-source projects — [yt-dlp](https://github.com/yt-dlp/yt-dlp) (the actual downloading) and [FFmpeg](https://ffmpeg.org) (merging/converting). Aevum is essentially a clean, friendly, ad-free wrapper around them.

## Build from source

Requires Python 3.10+.

```bash
pip install flask pystray pillow pyinstaller
# Place yt-dlp.exe and ffmpeg.exe into a bin/ folder next to ytdl_tray.py
python -m PyInstaller --onefile --noconsole --name Aevum ^
  --icon app.ico --version-file version.txt ^
  --hidden-import pystray._win32 --collect-submodules pystray ^
  --add-data "bin/yt-dlp.exe;." --add-data "bin/ffmpeg.exe;." --add-data "fonts;fonts" ytdl_tray.py
```

Or just run `build.bat`.

## Licenses

Aevum's own code is released under the [MIT License](LICENSE). It bundles third-party tools with their own licenses — see [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md). Notably, the bundled FFmpeg build is licensed under the **GPL**.
