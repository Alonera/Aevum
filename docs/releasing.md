# Preparing an Aevum release

Preparation is separate from publication. Do not push a version tag merely to
test a build: the existing `v*` tag workflow attaches Linux assets to a release.

## Source and tests

1. Work on a release-preparation branch. Keep APP_VERSION, installer.iss and
   version.txt in agreement. Update release-notes.md in English, distinguishing
   supported behavior from platform/account limitations.
2. Commit the packaged source before building. The release assembler checks the
   Windows and Linux commit plus normalized source hashes. It rejects changed
   inputs, mixed commits, different yt-dlp versions and damaged/missing assets.
3. Run source regression tests with Python 3.12+ and the bundled media tools:

   ```powershell
   $env:AEVUM_NATIVE_TEST='1'
   python -B -m pytest -q -p no:cacheprovider -p no:faulthandler tests --tb=short
   ```

   Native Windows dialogs are opt-in. Package tests are also opt-in; skipped
   package tests are not evidence that a package works. Tests use synthetic
   cookie profiles and a loopback media server, not personal account data.

## Build candidates without publishing

- Windows: `powershell -NoProfile -ExecutionPolicy Bypass -File build-release.ps1`.
  The script refuses existing output directories and writes the portable EXE,
  installer, source ZIP, checksums and source provenance under releases/.
- Linux: push the preparation branch, then run **Build Linux** manually on that
  branch (`gh workflow run build.yml --ref <branch>`). A manual branch run uploads
  an Actions artifact; it does not execute the tag-only release attachment step.
  The workflow tests source/UI/media, builds AppImage and tar.gz, then exercises
  both Linux packages against synthetic authenticated media before uploading.
- Download the successful run's `aevum-linux` artifact into a new local directory.
  Keep its manifests and JUnit reports with the candidate.
- Verify the Windows candidate:

   ```powershell
   $env:AEVUM_RELEASE_DIR='<new Windows output directory>'
   $env:AEVUM_INSTALLER_TEST='1'
   python -B -m pytest -q -p no:cacheprovider tests/test_release_artifacts.py --tb=short -rs
   ```

   If a user instance is already running, the live frozen test deliberately
   skips instead of sending downloads to it or closing it. Record that limit.

## Assemble and review

```powershell
python prepare-release.py assemble --windows '<Windows output>' --linux '<downloaded Linux artifact>' --output 'releases/v1.2.7-candidate'
```

The output is flat, with the four names the existing in-app updater expects:
Aevum.exe, Aevum-Setup.exe, Aevum-x86_64.AppImage and Aevum-linux-x86_64.tar.gz.
It also contains checksums.txt, RELEASE-NOTES.md and a local release manifest.

**checksums.txt must contain `asset-basename  sha256` per line.** Existing Aevum
versions cannot read GNU `hash  filename` lines or names prefixed by Portable/.
Changing only the new application's reader would not repair upgrades from the
old installed application. Check the actual distributed checksum file with the
old reader, not just with the build manifest.

Review the four hashes and source provenance. VirusTotal URLs in generated notes
are hash lookups, not proof that a scan ran or that an asset is harmless.
Do not publish private QA notes, cookie exports, local profiles or build caches.

## Publication boundary

Only publish after the release owner approves the complete candidate. Keep the
reviewed source commit and its exact four assets together; do not rebuild from
a moving branch or refresh only one asset after writing the checksums.

The existing tag workflow can create a release as soon as the tag is pushed.
Before that step, decide how the complete set will be attached without exposing
an incomplete latest release. Do not create a latest release until all four
packages, checksums and final notes are in place. Confirm old-version update
discovery selects the expected asset for each platform after publication.

Windows builds are unsigned. A Windows test does not validate Linux, and a
synthetic authentication test does not prove access to a real age-gated account.
