## Aevum v1.1.0 — Linux support

Aevum now runs on Linux, tested on real hardware (Debian-based Linux).

### Download

- **`Aevum-x86_64.AppImage`** — recommended. `chmod +x` and run; no terminal needed after that.
- **`Aevum-linux-x86_64.tar.gz`** — portable tarball, no FUSE required: extract and run `./Aevum/Aevum`.

Self-contained — yt-dlp and FFmpeg are bundled.

### How it behaves on Linux

- No system tray: launching Aevum opens its page in your default browser, and **closing the tab shuts the app down** (an active download always finishes first).
- **Add to app menu** (Settings → gear icon, or `--install`): installs Aevum as a regular menu app. This **copies the AppImage** into `~/.local/share/aevum/`, so you can safely **delete the downloaded AppImage afterwards** — the menu entry keeps working. Untick it (or run `--uninstall`) to remove the copy, menu entry and icon completely.
- Language and theme choices persist across launches.

### Fixes since the beta

- Cleaned `LD_LIBRARY_PATH` for child processes so bundled yt-dlp/FFmpeg run correctly on every distro.
- App lifetime verified: the server really stops after the tab closes (grace period for reconnects, then clean exit).
- **Stop button (Linux):** cancelling a download used to kill the whole app, not just the download — downloads now run in their own process group, so Stop only stops the download. (Assets refreshed 2026-07-05 with this fix.)

Windows users: nothing new here — stay on **v1.0.0** builds (`Aevum-Setup.exe` / `Aevum.exe`), they are unchanged.

---

## Aevum v1.0.0

A simple, self-contained desktop app to download videos and audio from **any site** (YouTube, Vimeo, Twitter/X, Twitch, and ~1,800 more). No installation prerequisites — Python, yt-dlp and FFmpeg are all bundled inside.

### Download

- **`Aevum-Setup.exe`** — recommended. Installer with optional Desktop / Start Menu shortcuts and an uninstaller. No admin rights needed.
- **`Aevum.exe`** — portable. Just double-click; nothing to install.

### First run — Windows warning

Aevum is not code-signed, so Windows SmartScreen shows *"Windows protected your PC"* the first time. This is normal for small independent apps. Click **More info → Run anyway**. You only need to do it once.

### Features

- Any site (powered by yt-dlp) · video up to 4K (MP4/MKV) or audio (MP3/M4A/Opus/FLAC/WAV)
- Subtitles, playlists (auto folder), login-cookies, stop button
- 8 languages, 4 themes, launch-at-startup option
- No ads, no tracking, no bundled extras — fully offline-capable UI (bundled font), everything runs locally

### Safety

- Open source — read the code in this repo.
- `checksums.txt` (SHA-256) below lets you verify your download.
- VirusTotal scan: <!-- paste your VirusTotal link here -->

### License

MIT (own code). Bundles yt-dlp (Unlicense) and FFmpeg (GPL) — see THIRD_PARTY_LICENSES.md.
