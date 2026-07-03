# Third-Party Licenses

The distributed `VideoIndirici.exe` bundles the following third-party programs.
They are invoked as separate executables (subprocesses); this project's own
code merely calls them.

## yt-dlp

- Project: https://github.com/yt-dlp/yt-dlp
- License: The Unlicense (public domain)
- Role: performs the actual media extraction/downloading.

## FFmpeg

- Project: https://ffmpeg.org
- Bundled build: gyan.dev "release-essentials" (https://www.gyan.dev/ffmpeg/builds/)
- License: **GNU General Public License, version 3 (GPLv3)**
- Role: merges video/audio streams, extracts audio, embeds subtitles.

### GPL compliance notice

The bundled FFmpeg build is licensed under the GPLv3. Because this application
is distributed together with that GPL binary, redistribution must comply with
the GPL for the FFmpeg component:

- The full GPL license text is available at https://www.gnu.org/licenses/gpl-3.0.html
- FFmpeg source code corresponding to the bundled build is available from
  https://ffmpeg.org/download.html and https://www.gyan.dev/ffmpeg/builds/
- No modifications were made to the FFmpeg binary; it is redistributed as-is.

FFmpeg is a trademark of Fabrice Bellard, originator of the FFmpeg project.

## Fonts

- JetBrains Mono (loaded from Google Fonts at runtime) — SIL Open Font License 1.1.
