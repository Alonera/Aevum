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

- It only ever looks at the releases of this repository, and only when you open
  Settings — never on its own at launch.
- Whatever it downloads is checked against the SHA-256 published in that release's
  `checksums.txt` before anything is opened. A file that does not match is deleted
  and nothing runs.
- The builds are unsigned, so the hash is what stands in for a signature. Verifying a
  release yourself against `checksums.txt` is the same check, done by hand.
- Nothing is downloaded or launched without you pressing the button.

## Supported versions

The latest release receives fixes.
