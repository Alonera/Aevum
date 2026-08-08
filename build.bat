@echo off
REM Build Aevum: portable exe (Portable\) + installer (Setup\)
REM Requirements: pip install flask pystray pillow pyinstaller  +  Inno Setup (ISCC on PATH)
REM Place yt-dlp.exe, ffmpeg.exe and ffprobe.exe into the bin\ folder first.
REM ffmpeg: use 8.0 essentials (gyan.dev). Do NOT bundle 8.1.x - its HTTP
REM range requests hang forever on googlevideo, which breaks clip downloads
REM (yt-dlp issue #16546). 8.0 and 7.x are fine.
REM ffprobe ships in the same archive as ffmpeg - take it from there so the
REM two are the same build. yt-dlp reads metadata through it, and without it
REM --add-metadata gives up with "ffprobe not found" on some sites.

cd /d "%~dp0"

if not exist "bin\yt-dlp.exe" echo MISSING: bin\yt-dlp.exe && pause && exit /b 1
if not exist "bin\ffmpeg.exe" echo MISSING: bin\ffmpeg.exe && pause && exit /b 1
if not exist "bin\ffprobe.exe" echo MISSING: bin\ffprobe.exe && pause && exit /b 1

REM The updater compares APP_VERSION against the newest release tag, so a
REM constant left behind would hide a real update or offer one already here.
REM Refuse to build while the three places disagree.
for /f "tokens=2 delims== " %%v in ('findstr /b /c:"APP_VERSION = " ytdl_tray.py') do set "PYVER=%%~v"
for /f "tokens=3" %%v in ('findstr /b /c:"#define AppVersion" installer.iss') do set "ISSVER=%%~v"
if not "%PYVER%"=="%ISSVER%" echo VERSION MISMATCH: ytdl_tray.py=%PYVER% installer.iss=%ISSVER% && pause && exit /b 1
findstr /c:"'FileVersion', '%PYVER%.0'" version.txt >nul || (echo VERSION MISMATCH: version.txt is not %PYVER% && pause && exit /b 1)
echo Version %PYVER% - ytdl_tray.py, installer.iss and version.txt agree.

echo [1/3] Building portable Aevum.exe ...
python -m PyInstaller --onefile --noconsole --clean --name "Aevum" ^
  --icon "app.ico" --version-file "version.txt" ^
  --hidden-import "pystray._win32" --collect-submodules pystray ^
  --add-data "bin/yt-dlp.exe;." --add-data "bin/ffmpeg.exe;." --add-data "bin/ffprobe.exe;." ^
  --add-data "fonts;fonts" ytdl_tray.py || (echo BUILD FAILED & pause & exit /b 1)

echo [2/3] Moving portable exe to Portable\ ...
if not exist "Portable" mkdir "Portable"
move /y "dist\Aevum.exe" "Portable\Aevum.exe" >nul

echo [3/3] Building installer to Setup\ ...
if not exist "Setup" mkdir "Setup"
ISCC.exe installer.iss || echo (Inno Setup / ISCC not found on PATH - skipped installer)

rmdir /s /q build >nul 2>&1
del /q Aevum.spec >nul 2>&1

echo.
echo Done.
echo   Portable : Portable\Aevum.exe
echo   Installer: Setup\Aevum-Setup.exe
pause
