# Preparing an Aevum release

Preparation is separate from publication. Pushing a `v*` tag runs the Linux
workflow but, since 1.2.8, attaches nothing to any release; the release is
assembled and published by hand (see Publication boundary). Still do not push a
tag merely to test a build: a tag is a statement that this commit shipped.

## Source and tests

1. Work on a release-preparation branch. Keep APP_VERSION, installer.iss and
   version.txt in agreement. Update release-notes.md in English, distinguishing
   supported behavior from platform/account limitations.
2. Commit the packaged source before building. The release assembler checks the
   Windows and Linux commit plus normalized source hashes. It rejects changed
   inputs, mixed commits, different yt-dlp versions and damaged/missing assets.
3. Build and test with **CPython 3.12**, the same minor the Linux job gets from
   actions/setup-python, not whatever newer Python the machine happens to have.
   The 1.2.8 Windows build used 3.12.10; record the exact version here each
   release. build-release.ps1 calls plain `python`, so put that interpreter
   first on PATH for the session, for example
   `$env:PATH = "$env:LOCALAPPDATA\Programs\Python\Python312;$env:PATH"`, and
   check `python --version` before starting. Run the source regression tests
   with it and the bundled media tools:

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
  branch (`gh workflow run build.yml --ref <branch>`). The run uploads an Actions
  artifact and nothing else. The workflow tests source/UI/media, builds AppImage
  and tar.gz, refuses the build if any bundled binary needs a glibc newer than
  2.35, starts both packages inside a Debian 12 container, then exercises both
  packages against synthetic authenticated media before uploading.
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
python prepare-release.py assemble --windows '<Windows output>' --linux '<downloaded Linux artifact>' --output 'releases/v1.2.8-candidate'
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

Manual assembly is the official path, and the only one. The Windows half is
built on a developer machine, so no workflow can ever produce all four assets,
and a workflow that re-attached freshly built Linux assets to a release would
replace binaries whose hashes are already in checksums.txt. The attach step was
therefore removed in 1.2.8 (the v1.2.7 tag run had to be cancelled for exactly
that reason). Publish in this order:

1. Assemble the candidate and review it as above.
2. Create the GitHub release from the reviewed commit with all four assets,
   checksums.txt and the final notes in one step, for example
   `gh release create v1.2.8 --target <commit> --notes-file <RELEASE-NOTES.md> <the five files>`.
   That creates the tag; the tag push then runs the Linux workflow once more,
   which only proves the tagged commit builds and uploads an artifact.
3. Confirm old-version update discovery selects the expected asset for each
   platform after publication.

Do not create a latest release until all four packages, checksums and final
notes are in place.

Windows builds are unsigned. A Windows test does not validate Linux, and a
synthetic authentication test does not prove access to a real age-gated account.

## Decisions on record

Why things are the way they are, so the next release does not re-derive them.

- **Linux runner pinned to `ubuntu-22.04` (1.2.8).** A PyInstaller build takes
  the glibc of its build machine as its floor. 1.2.7 was built on
  `ubuntu-latest`, which had become Ubuntu 24.04 (glibc 2.39); the packages
  needed GLIBC_2.38 and did not start on Ubuntu 22.04, Mint 21 or Debian 12
  (AppImage/appimage.github.io#6623). 22.04 is the oldest Ubuntu GitHub offers.
  `ubuntu-latest` is never a fallback: it is moving on to 26.04, so its floor
  only rises.
- **The pin has an expiry.** GitHub deprecated `ubuntu-22.04` on 2026-09-17,
  browns it out (jobs deliberately fail) in March and April 2027 and retires it
  on 2027-04-17 (actions/runner-images#14254). Issue #19 is the plan: keep the
  job on a current runner, run only the PyInstaller build inside an
  `ubuntu:22.04` container, keep every test step (playwright included) on the
  host. Deadline: green before the first brownout in March 2027.
- **glibc guard, ceiling 2.35.** After the build, every ELF file in dist/Aevum
  is read with objdump and the build fails if any needs a GLIBC symbol version
  above 2.35. It exists so the 1.2.7 drift fails in CI instead of on a user's
  desktop. The ceiling moves only when the floor is moved on purpose, and the
  comment in build.yml says why each time.
- **Debian 12 startup step.** Both packages are started in a `debian:12`
  container (glibc 2.36) with `--tray`, `BROWSER=/bin/true` and a private
  `XDG_CONFIG_HOME`, must answer `/ping`, and are stopped with SIGTERM; the
  bundled yt-dlp and ffmpeg are run there too. It replays the catalog's
  complaint before publishing. It narrows one failure class and is not a run
  on real hardware; the release notes keep saying so.
- **Windows is built with CPython 3.12.** Same minor as the Linux job. Using
  the machine's newest Python (3.14 at the time) would mean two platforms built
  from the same commit with different interpreters, which nobody had tested.
- **Tag pushes attach nothing.** See Publication boundary. The tag trigger stays
  so the shipped commit is proven to build.
