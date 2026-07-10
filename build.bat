@echo off
REM Build Aevum: portable exe (Portable\) + installer (Setup\)
REM Requirements: pip install flask pystray pillow pyinstaller  +  Inno Setup (ISCC on PATH)
REM Place yt-dlp.exe and ffmpeg.exe into the bin\ folder first.
REM ffmpeg: use 8.0 essentials (gyan.dev). Do NOT bundle 8.1.x - its HTTP
REM range requests hang forever on googlevideo, which breaks clip downloads
REM (yt-dlp issue #16546). 8.0 and 7.x are fine.

cd /d "%~dp0"

if not exist "bin\yt-dlp.exe" echo MISSING: bin\yt-dlp.exe && pause && exit /b 1
if not exist "bin\ffmpeg.exe" echo MISSING: bin\ffmpeg.exe && pause && exit /b 1

echo [1/3] Building portable Aevum.exe ...
python -m PyInstaller --onefile --noconsole --clean --name "Aevum" ^
  --icon "app.ico" --version-file "version.txt" ^
  --hidden-import "pystray._win32" --collect-submodules pystray ^
  --add-data "bin/yt-dlp.exe;." --add-data "bin/ffmpeg.exe;." --add-data "fonts;fonts" ytdl_tray.py || (echo BUILD FAILED & pause & exit /b 1)

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
