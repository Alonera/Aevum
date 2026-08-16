# Third-Party Licenses

The distributed Aevum builds (`Aevum.exe`, `Aevum-Setup.exe`,
`Aevum-x86_64.AppImage` and `Aevum-linux-x86_64.tar.gz`) bundle the following
third-party programs. They are invoked as separate executables (subprocesses);
this project's own code merely calls them.

## yt-dlp

- Project: https://github.com/yt-dlp/yt-dlp
- License: The Unlicense (public domain)
- Role: performs the actual media extraction/downloading.

## FFmpeg

- Project: https://ffmpeg.org
- Bundled build (Windows): gyan.dev "release-essentials" (https://www.gyan.dev/ffmpeg/builds/)
- Bundled build (Linux): BtbN static GPL build (https://github.com/BtbN/FFmpeg-Builds).
  Releases up to 1.2.5 used the John Van Sickle static build
  (https://johnvansickle.com/ffmpeg/).
- License: **GNU General Public License, version 3 (GPLv3)**
- Binaries shipped: `ffmpeg` and `ffprobe`, both from the build named above.
- Role: ffmpeg merges video/audio streams, extracts audio and embeds
  subtitles; ffprobe reads the metadata written into finished files.

### GPL compliance notice

The bundled FFmpeg build is licensed under the GPLv3. Because this application
is distributed together with that GPL binary, redistribution must comply with
the GPL for the FFmpeg component:

- The full GPL license text is available at https://www.gnu.org/licenses/gpl-3.0.html
- FFmpeg source code corresponding to the bundled build is available from
  https://ffmpeg.org/download.html and https://www.gyan.dev/ffmpeg/builds/
- No modifications were made to the FFmpeg binaries; they are redistributed as-is.

FFmpeg is a trademark of Fabrice Bellard, originator of the FFmpeg project.

## Fonts

- JetBrains Mono — SIL Open Font License 1.1. The woff2 files are bundled in
  `fonts/` and served locally by the app; no request is made to Google Fonts
  or any other host at runtime.
