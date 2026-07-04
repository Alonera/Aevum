#!/usr/bin/env bash
# Build Aevum for Linux: PyInstaller -> AppDir -> AppImage + tar.gz
#
# Requires: python3 with  flask pystray pillow pyinstaller pycairo PyGObject
#           plus  bin/yt-dlp  and  bin/ffmpeg  (Linux binaries), and internet
#           (to download appimagetool). The GitHub Actions workflow sets all this up.
set -euo pipefail
cd "$(dirname "$0")"

[ -f bin/yt-dlp ] || { echo "MISSING: bin/yt-dlp (Linux binary)"; exit 1; }
[ -f bin/ffmpeg ] || { echo "MISSING: bin/ffmpeg (Linux binary)"; exit 1; }
chmod +x bin/yt-dlp bin/ffmpeg

echo "[1/4] PyInstaller build..."
rm -rf build dist Aevum.spec
pyinstaller --noconfirm --clean --name Aevum \
  --add-data "bin/yt-dlp:." \
  --add-data "bin/ffmpeg:." \
  --add-data "fonts:fonts" \
  --hidden-import "pystray._xorg" \
  --collect-submodules "Xlib" \
  ytdl_tray.py

echo "[2/4] AppDir..."
APPDIR=AppDir
rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/bin" "$APPDIR/usr/share/applications" \
         "$APPDIR/usr/share/icons/hicolor/256x256/apps"
cp -a dist/Aevum/. "$APPDIR/usr/bin/"
cp aevum.png     "$APPDIR/aevum.png"
cp aevum.desktop "$APPDIR/aevum.desktop"
cp aevum.desktop "$APPDIR/usr/share/applications/aevum.desktop"
cp aevum.png     "$APPDIR/usr/share/icons/hicolor/256x256/apps/aevum.png"
cat > "$APPDIR/AppRun" <<'EOF'
#!/bin/bash
HERE="$(dirname "$(readlink -f "${0}")")"
exec "$HERE/usr/bin/Aevum" "$@"
EOF
chmod +x "$APPDIR/AppRun"

echo "[3/4] tar.gz..."
tar -czf Aevum-linux-x86_64.tar.gz -C dist Aevum

echo "[4/4] AppImage..."
if [ ! -f appimagetool ]; then
  curl -L -o appimagetool \
    https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage
  chmod +x appimagetool
fi
export APPIMAGE_EXTRACT_AND_RUN=1   # run without FUSE (needed on CI)
ARCH=x86_64 ./appimagetool "$APPDIR" Aevum-x86_64.AppImage

echo "Done: Aevum-x86_64.AppImage + Aevum-linux-x86_64.tar.gz"
