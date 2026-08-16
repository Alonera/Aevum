# Security Policy

## Reporting a vulnerability

If you find a security issue in Aevum, please report it **privately** using GitHub's
"Report a vulnerability" feature (the **Security** tab of this repo) rather than opening
a public issue.

Aevum bundles **yt-dlp**, **FFmpeg** and **ffprobe**; vulnerabilities in those tools
should be reported to their own projects.

## The local server

Aevum's interface is a page in your browser, served by a small web server that
listens on `127.0.0.1` only — never on your network, so nothing else on it can
reach Aevum.

That server answers only the page Aevum opened. A request arriving under any
other name, or carrying another site's origin, is refused. So a site you happen
to have open in another tab cannot drive your copy of Aevum — it cannot start a
download, choose where files are written, or trigger an update.

## Updating itself

From 1.2.5 Aevum can fetch its own next release. Worth knowing about it:

- **Checking is a network request.** Aevum cannot tell whether it is out of date on
  its own; it has to ask. Opening Settings makes one HTTPS GET to
  `https://api.github.com/repos/Alonera/Aevum/releases/latest` and compares the tag
  it finds against the version compiled into the running copy. This is the only
  request Aevum makes that is not to a site you pasted, and it is the one thing in
  the app that is not purely local. It sends nothing about you, your machine or your
  downloads — the same public read anyone makes by opening the releases page — but
  it does reach GitHub, and GitHub sees it the way it sees any visitor. The answer
  is cached for fifteen minutes.
- It happens when you open that panel, never at launch and never on a timer, so a
  copy whose Settings you never open never contacts GitHub at all.
- It only ever looks at the releases of this repository.
- Whatever it downloads is checked against the SHA-256 published in that release's
  `checksums.txt` before anything is opened. A file that does not match is deleted
  and nothing runs.
- The builds are unsigned, so the hash is what stands in for a signature. Verifying a
  release yourself against `checksums.txt` is the same check, done by hand.
- Nothing is downloaded or launched without you pressing the button.

## Updating yt-dlp

From 1.2.6 the Packages line in Settings updates the copy of yt-dlp Aevum uses.
It is the piece that goes out of date on its own, because the sites keep
changing and it has to keep up. The same rules apply, with two differences:

- **It asks two more repositories.** Opening Settings reads
  `https://api.github.com/repos/yt-dlp/yt-dlp/releases/latest` and
  `https://api.github.com/repos/yt-dlp/yt-dlp-nightly-builds/releases/latest`
  alongside the check for Aevum's own release, and offers whichever is genuinely
  newer than the copy you are running — stable first when it is ahead. Same shape
  as the other: a public read, nothing about you attached, cached for fifteen
  minutes, and only when the panel is open.
- **yt-dlp updates itself.** Pressing the button copies the bundled binary into
  your own folder — `%APPDATA%\Aevum\bin` on Windows, `~/.config/aevum/bin`
  elsewhere — and runs its own updater, which verifies the download against the
  hash yt-dlp publishes. Aevum then prefers that copy over the one inside the
  package. If the new copy will not run, the old one is put back.
- Nothing outside that one folder is written, and no installer runs.
- **What the hash does and does not prove.** It proves the file arrived intact and
  is the one the yt-dlp project published. It does not mean anyone here has read
  that build. Before 1.2.6 the only yt-dlp Aevum would run was the one pinned into
  the package at build time; now a newer one can arrive between releases, and a bad
  day upstream reaches you without passing through us first. Nightly builds carry
  more of that risk than stable ones, which is part of why stable is taken whenever
  it is the newer of the two: the nightly channel is for the weeks when stable
  cannot do the job, not for their own sake. That is the trade the feature makes, and it is deliberate: a yt-dlp frozen
  for two months stops working against the sites, which is the failure that
  actually happens. It never updates on its own — the button is yours to press —
  and "back to the bundled version" is one click away.
- **Back out with one click.** "back to the bundled version" deletes that file, and
  Aevum returns to the yt-dlp it shipped with.

## Supported versions

The latest release receives fixes.
