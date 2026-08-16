# Aevum 1.2.6

Downloads that failed with "403 Forbidden", and a way to fix the next one of those without waiting for a release.

## Fixed

- **Downloads failed with `HTTP Error 403: Forbidden`, some of the time.** Nothing was wrong with your connection or with the video. YouTube hands out an address for the file and then refuses that same address, sometimes before the first byte and sometimes a third of the way in. It is the refusal of one address, not of you: ask again and the next one usually works.

  What made it show up now is that there was only one way in left. yt-dlp reaches YouTube by presenting itself as one of several apps, and for a lot of videos every one of those doors had closed except a single one — which is the one being refused at random. Measured on the version 1.2.5 shipped: four of six downloads of the same video failed, while every other door reported no formats at all or a DRM wall.

  Two things changed. Aevum tries a failed download again, up to three times, because a fresh attempt asks for a fresh address; on the old yt-dlp that took the failure rate of a single attempt from 29% to none in ten downloads. And the yt-dlp in this build knows a door the last one did not, which fetches the video in pieces rather than one long stream and never meets the refusal at all: eighteen downloads, none failed.

  Retrying is a safety net, not the cure. On a large file three attempts are still sometimes not enough, which is why the copy of yt-dlp matters and why the Packages line below exists.

- **The error said nothing you could act on.** `HTTP Error 403: Forbidden` is a line for someone reading a server log. When a site refuses a download three times in a row, Aevum now says so and points at where to fix it. Failures that trying again cannot mend — a private video, a format the site does not have, a connection that dropped at this end — are not retried and keep their own message, so a hopeless download still fails as fast as it used to.

## New

- **Packages, in Settings.** A second line under the version, with its own button. It updates yt-dlp, which is the part of Aevum that goes out of date on its own: the sites keep changing and it keeps up, usually within days, while a release here takes weeks. Until now the copy inside the package was the only one Aevum would use, so a fix that already existed still meant waiting for a whole new download of Aevum.

  The updated copy goes into your own folder — `%APPDATA%\Aevum\bin` on Windows, `~/.config/aevum/bin` elsewhere — and Aevum prefers it over the bundled one. Nothing else is written and no installer runs. yt-dlp verifies its own download against the hash it publishes; if the result will not run, the old one is put back. "back to the bundled version" undoes the whole thing by deleting one file.

  It watches both of yt-dlp's channels and offers stable whenever stable is newer than what you have, a nightly only when stable is not. So a nightly is never where you stay: the next stable that passes your copy takes you back to it.

  A nightly is cut every day, so that line will usually have something on offer. The note under it says what the button is actually for — unless downloads keep failing, there is nothing there you need.

- **One button per line, and it tells you what it is doing.** Both update lines now carry a small round control that is four things in turn: circling arrows while it looks, a download arrow when it has found something, a ring that fills while it works, and a tick when it is done. A scan that finds nothing ends on the tick too. What it is showing is what pressing it does.

- **A build that cannot install its own update now opens the release page** instead of telling you to go and find it.

## Changed

- **The bundled yt-dlp comes from the nightly channel.** Not a taste for the bleeding edge, a look at the calendar: stable releases have come 25 to 84 days apart, and on the day this was built the newest stable was six weeks old and could not download from YouTube at all. Nightly builds come from the same tree and the same tests, a day behind the fix instead of weeks. From there the in-app updater moves you to stable whenever stable is ahead.

- **Both version lines are filled in before anything is asked of the network.** What you have installed is answerable without leaving the machine, so the panel shows it the moment it opens. The network is only needed for whether something newer exists, which is the question the button asks.

- **The guide has two more entries**, one for each update line, in all eight languages.

## Privacy

Opening Settings now makes three HTTPS requests to `api.github.com`: this repository's newest release, yt-dlp's newest stable, and yt-dlp's newest nightly. They happen only when you open that panel, never at launch, and the answers are reused for fifteen minutes. Nothing about you or about what you have downloaded goes with them. SECURITY.md spells out what the hash check does and does not prove, and why a nightly is a trade worth making rather than a free lunch.

## Under the hood

- ffmpeg is deliberately **not** part of the self-updating. It is pinned at 8.0 because 8.1.x hangs forever on googlevideo and takes clip downloads with it. It does not argue with the sites, so it does not go stale.

- **Two copies of Aevum could run at once.** The port it listens on was picked from a range of sixty, while the check for an already-running copy only looked at the first ten — so a copy that landed on 5012 was invisible to the next launch. One range now.

- Requests that cost something — a process, a call to GitHub — need a header only Aevum's own page sends. That closes a gap where any page open in your browser could have made Aevum spawn yt-dlp, and it applies to the update check that has been there since 1.2.5.

- An update and a download can no longer overlap. Installing or swapping a binary while a download is running would hurt the download; the queue waits the few seconds instead, and the two updaters cannot run at the same time as each other either.

- A yt-dlp reached through a symlinked or redirected folder refused to start with a message about "parent process has different executable". Paths are resolved to their real location now.
