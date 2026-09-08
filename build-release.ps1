param(
    [string]$OutputDirectory,
    [string]$Iscc,
    [switch]$SkipTests
)
# Build a fresh, isolated Windows release; never delete/replace old artifacts.
$ErrorActionPreference = 'Stop'
$repoRoot = $PSScriptRoot
Set-Location -LiteralPath $repoRoot
$appText = Get-Content -LiteralPath 'ytdl_tray.py' -Raw -Encoding UTF8
$version = [regex]::Match($appText, '(?m)^APP_VERSION = "([0-9.]+)"').Groups[1].Value
if (-not $version) { throw 'APP_VERSION missing' }
$installerText = Get-Content -LiteralPath 'installer.iss' -Raw -Encoding UTF8
$resourceText = Get-Content -LiteralPath 'version.txt' -Raw -Encoding UTF8
if (-not $installerText.Contains('#define AppVersion "' + $version + '"') -or
    -not $resourceText.Contains("'FileVersion', '$version.0'")) { throw 'Version mismatch' }
if (-not $OutputDirectory) {
    $OutputDirectory = Join-Path $repoRoot ('releases\' + $version + '-windows-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
}
$targetRoot = [IO.Path]::GetFullPath($OutputDirectory)
if (-not $targetRoot.StartsWith($repoRoot.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Release output must be inside this working copy.'
}
if (Test-Path -LiteralPath $targetRoot) { throw 'Output already exists; choose a NEW directory.' }
if (-not $Iscc) {
    $compiler = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    if ($compiler) { $Iscc = $compiler.Source }
    else {
        $candidates = @(
            (Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 6\ISCC.exe'),
            (Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6\ISCC.exe'),
            (Join-Path $env:ProgramFiles 'Inno Setup 6\ISCC.exe'))
        $Iscc = $candidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
    }
}
if (-not $Iscc -or -not (Test-Path -LiteralPath $Iscc)) { throw 'Inno Setup compiler not found.' }
foreach ($binary in 'yt-dlp.exe','ffmpeg.exe','ffprobe.exe') {
    if (-not (Test-Path -LiteralPath (Join-Path $repoRoot "bin\$binary"))) { throw "Missing bin\$binary" }
}
if (-not $SkipTests) {
    & python -B -m pytest -q -p no:cacheprovider tests --tb=short
    if ($LASTEXITCODE -ne 0) { throw 'Tests failed; no release built.' }
}
$portable = Join-Path $targetRoot 'Portable'
$setup = Join-Path $targetRoot 'Setup'
$work = Join-Path $targetRoot '_build'
New-Item -ItemType Directory -Path $portable,$setup,$work | Out-Null
# Keep PyInstaller's cache scoped to this new release as well.
$previousCache = $env:PYINSTALLER_CONFIG_DIR
try {
    $env:PYINSTALLER_CONFIG_DIR = Join-Path $work 'cache'
    & python -m PyInstaller --onefile --noconsole --noconfirm --name Aevum `
        --distpath $portable --workpath (Join-Path $work 'work') --specpath $work `
        --icon (Join-Path $repoRoot 'app.ico') --version-file (Join-Path $repoRoot 'version.txt') `
        --hidden-import pystray._win32 --collect-submodules pystray `
        --add-data ((Join-Path $repoRoot 'bin/yt-dlp.exe') + ';.') `
        --add-data ((Join-Path $repoRoot 'bin/ffmpeg.exe') + ';.') `
        --add-data ((Join-Path $repoRoot 'bin/ffprobe.exe') + ';.') `
        --add-data ((Join-Path $repoRoot 'fonts') + ';fonts') (Join-Path $repoRoot 'ytdl_tray.py')
    if ($LASTEXITCODE -ne 0) { throw 'Portable build failed.' }
} finally { $env:PYINSTALLER_CONFIG_DIR = $previousCache }
$exe = Join-Path $portable 'Aevum.exe'
if (-not (Test-Path -LiteralPath $exe)) { throw 'Portable EXE missing.' }
& $Iscc ('/DAppSource=' + $exe) ('/O' + $setup) (Join-Path $repoRoot 'installer.iss')
if ($LASTEXITCODE -ne 0) { throw 'Installer build failed.' }
$installer = Join-Path $setup 'Aevum-Setup.exe'
if (-not (Test-Path -LiteralPath $installer)) { throw 'Installer EXE missing.' }
foreach ($name in 'LICENSE','THIRD_PARTY_LICENSES.md','README.md','SECURITY.md') {
    Copy-Item -LiteralPath (Join-Path $repoRoot $name) -Destination $targetRoot
}
# Explicit source allowlist: no config, private profile, temp jars or old EXEs.
$sourceStage = Join-Path $work 'source'
New-Item -ItemType Directory -Path $sourceStage | Out-Null
foreach ($name in 'ytdl_tray.py','aevum_cookies.py','app.ico','version.txt','installer.iss',
                    'build.bat','build-release.ps1','build-linux.sh','prepare-release.py','release-notes.md','LICENSE',
                    'THIRD_PARTY_LICENSES.md','README.md','SECURITY.md') {
    Copy-Item -LiteralPath (Join-Path $repoRoot $name) -Destination $sourceStage
}
Copy-Item -LiteralPath (Join-Path $repoRoot 'fonts') -Destination $sourceStage -Recurse
New-Item -ItemType Directory -Path (Join-Path $sourceStage 'tests') | Out-Null
Get-ChildItem -LiteralPath (Join-Path $repoRoot 'tests') -Filter '*.py' |
    Copy-Item -Destination (Join-Path $sourceStage 'tests')
New-Item -ItemType Directory -Path (Join-Path $sourceStage 'docs') | Out-Null
foreach ($name in 'releasing.md','screenshot.png') {
    $document = Join-Path $repoRoot ('docs\' + $name)
    if (Test-Path -LiteralPath $document) {
        Copy-Item -LiteralPath $document -Destination (Join-Path $sourceStage 'docs')
    }
}
$sourceZip = Join-Path $targetRoot "Aevum-$version-source.zip"
Compress-Archive -Path (Join-Path $sourceStage '*') -DestinationPath $sourceZip
$records = foreach ($path in $exe,$installer,$sourceZip) {
    $item = Get-Item -LiteralPath $path
    [ordered]@{file=$path.Substring($targetRoot.Length + 1).Replace('\','/'); bytes=$item.Length;
        sha256=(Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()}
}
$inputs = foreach ($name in 'ytdl_tray.py','aevum_cookies.py','installer.iss','version.txt',
                              'bin/yt-dlp.exe','bin/ffmpeg.exe','bin/ffprobe.exe') {
    [ordered]@{file=$name; sha256=(Get-FileHash -LiteralPath $name -Algorithm SHA256).Hash.ToLowerInvariant()}
}
$manifest = [ordered]@{
    version=$version; platform='windows-x86_64'; builtUtc=[DateTime]::UtcNow.ToString('o');
    sourceBaseCommit=(& git rev-parse HEAD); sourceIncludesUncommittedChanges=$true;
    python=(& python --version); pyinstaller=(& python -m PyInstaller --version);
    ytDlp=(& '.\bin\yt-dlp.exe' --version); artifacts=@($records); inputs=@($inputs);
    signed=$false; published=$false
}
$manifest | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (Join-Path $targetRoot 'build-manifest.json') -Encoding UTF8
# v1.2.6 and older read the asset basename first, then its hash. Keep this
# wire format even though GNU sha256sum normally puts the hash first.
$records | ForEach-Object { [IO.Path]::GetFileName($_.file) + '  ' + $_.sha256 } |
    Set-Content -LiteralPath (Join-Path $targetRoot 'checksums.txt') -Encoding ASCII
& python prepare-release.py stamp-windows $targetRoot
if ($LASTEXITCODE -ne 0) { throw 'Source provenance verification failed; do not publish this build.' }
Write-Output "Built $version -> $targetRoot"
