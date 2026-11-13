# Aevum 1.2.8

The Linux packages start again on the distributions most people actually run. Nothing else changed.

## Fixed

- **The Linux AppImage and tar.gz did not start on Ubuntu 22.04, Linux Mint 21 or Debian 12.** The 1.2.7 packages were built on a GitHub runner that had quietly moved from Ubuntu 22.04 to 24.04, so they required glibc 2.38 and quit at once with `version 'GLIBC_2.38' not found` on anything older. The AppImage catalog's automated test caught it (AppImage/appimage.github.io#6623); nobody had run the packages on an older system before publishing. The Linux build is now pinned to Ubuntu 22.04, and the packages need glibc 2.35 or newer: Ubuntu 22.04 and later, Mint 21 and later, Debian 12 and later. Debian 11 and Ubuntu 20.04 are still not covered.

## Under the hood

- The build now refuses to ship if any bundled binary asks for a glibc newer than 2.35, in the same way it already refuses an untested ffmpeg. The drift that produced 1.2.7's packages cannot pass unnoticed again.
- During the build, both Linux packages are started inside a Debian 12 container and must answer on their local port, and the bundled yt-dlp and ffmpeg are run there too. This narrows one class of failure; it is not a run on real hardware, and the Linux packages are still not tested on real hardware before release.
- The runner this build is pinned to is itself scheduled for retirement in April 2027. Issue #19 tracks moving the build into a container before then.
- No application changes. The Windows builds are rebuilt from the same source so that every platform reports 1.2.8; they behave exactly as 1.2.7 did. yt-dlp stays at 2026.08.16.020253, FFmpeg at Windows 8.0 and Linux 7.1.

Windows builds are unsigned; Windows may show a SmartScreen warning.
