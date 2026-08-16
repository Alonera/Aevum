# Aevum 1.2.6

Downloads that failed with "403 Forbidden", and a way to fix the next one of these without waiting for a release.

## Fixed

- **Downloads failed with `HTTP Error 403: Forbidden`, some of the time.** Nothing was wrong with your connection or with the video. YouTube hands out an address for the file and then refuses that same address, sometimes before the first byte and sometimes a third of the way in. It is the refusal of one address, not of you: ask again and the next one usually works.

  What made it show up now is that there was only one way in left. yt-dlp reaches YouTube by presenting itself as one of several apps, and for a lot of videos every one of those doors had closed except a single one, which is the one being refused at random. Measured on the version 1.2.5 shipped: four of six downloads of the same video failed, and every other door reported no formats at all or a DRM wall.

  Two things changed. Aevum now tries a failed download again, up to three times, because a fresh attempt asks for a fresh address; on the old yt-dlp that took the failure rate of a single attempt from 29% to none in ten downloads of a small file. And the yt-dlp inside this build knows a door that did not exist in the last one, which fetches the video in pieces rather than one long stream and never meets the refusal at all: eighteen downloads, none failed.

  Retrying is a safety net, not the cure. On a large file three attempts still are not always enough, which is why the copy of yt-dlp matters and why the next item exists.

- **The error said nothing you could act on.** `HTTP Error 403: Forbidden` is a line for someone reading a server log. When a download is refused three times in a row Aevum now says the site turned it down and points at where to fix it. Downloads that fail for a reason trying again cannot mend — a private video, a format the site does not have — are not retried and say what they always said, so a hopeless download still fails as fast as it used to.

## New

- **Packages, in Settings.** A second line under the version, with its own button. It updates yt-dlp, which is the part of Aevum that goes out of date on its own: the sites keep changing and it keeps up, usually within days, while a release here takes weeks. Until now the copy inside the package was the only one Aevum would use, so a fix that already existed still meant waiting for a whole new download of Aevum. Now it does not.

  The updated copy goes into your own folder — `%APPDATA%\Aevum\bin` on Windows, `~/.config/aevum/bin` elsewhere — and Aevum prefers it over the bundled one. Nothing else is written and no installer runs. yt-dlp verifies its own download against the hash it publishes; if the result will not run, the old one is put back. "back to the bundled version" undoes the whole thing by deleting one file.

  Checking is a network request, like the version check above it, and it happens for the same reason and under the same rule: only when you open Settings, never at launch. Details are in SECURITY.md.

## Changed

- **The bundled yt-dlp now comes from the nightly channel.** Not a taste for the bleeding edge, a look at the calendar: stable releases have come 25 to 84 days apart, and on the day this was built the newest stable was six weeks old and could not download from YouTube at all. Nightly builds come from the same tree and the same tests, a day behind the fix instead of weeks. The in-app updater follows the same channel, and one click goes back to the copy this build shipped with.

- ffmpeg is deliberately **not** part of this. It is pinned at 8.0 because 8.1.x hangs forever on googlevideo and takes clip downloads with it. It does not argue with the sites, so it does not go stale, and updating it on its own would only reopen a bug we already closed.
