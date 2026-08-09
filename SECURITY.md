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

## Supported versions

The latest release receives fixes.
