# Aevum 1.2.5

What the format buttons actually give you, and a way to update without going and fetching it.

## Fixed

- **H.264 did not always give you H.264.** When a site had no H.264 stream at the size you asked for, the chain fell back to matching on the container instead of the codec — and an `.mp4` says nothing about what is inside it. VP9 and AV1 came straight through that gap, the exact files the button exists to avoid. Editors played them with sound and no picture, or refused them outright. Every rung of that chain checks the codec now.

  If you downloaded for editing before this release, the files may not be what the button said. VLC's codec panel, or `ffprobe`, will tell you in a second.

- **H.264 also gave up too early.** Some sites serve real H.264 but report the codec as unknown, and the filtered rungs stepped right over it — on Instagram the button failed outright while a working H.264 stream sat there the whole time. It finds those now, and those downloads need no converting afterwards.

- **MP4 handed editors AV1 at 1080p.** Not only at 4K, at 1080p too: the AV1 stream outranks the H.264 one in the default sort, so the button labelled as the safe default was quietly the worst choice for anyone editing. MP4 prefers H.264 wherever a site offers the choice now, and still reaches 4K where AV1 is all there is.

- **MP4 and MKV were the same button.** On every site tested they resolved to the same stream and differed only in the file extension. Now that MP4 prefers H.264, MKV is what it always said it was: any codec, the best the site has, good for archiving.

- **Chrome cookies failed with a line nobody could act on.** From Chrome 127 the key to the cookie jar is tied to Chrome's own binary, so no other program can open it — including this one. All you saw was `Failed to decrypt with DPAPI`. Aevum says what actually happened now, and which browser still works: Firefox does, Edge and Brave are Chrome underneath and do not.

- **Vimeo links did not work at all.** Vimeo revoked the anonymous tokens yt-dlp signs in with, so every `vimeo.com/…` link answered 401 and stopped there. The same video opens through Vimeo's player URL with no account, and that is where plain Vimeo links go now. Channel, showcase and unlisted links are left alone — they carry parts of the address the player URL would drop.

- **WebM gave up on sites that have no WebM in them.** Vimeo serves H.264 and AAC, separately, and nothing else; there was no rung in the WebM chain for that and the download ended on "Requested format is not available". It falls back now, and lands in an mkv where a webm cannot hold what the site offered.

- **Downloading with the thumbnail on could fail on a path you never saw.** That layout writes the title twice, once as the folder and again as the file inside it, and a long title pushed the whole path past the 260 characters Windows will open. Both halves are shorter now.

- **WebM's VP9 test was written wrong.** It matched `vp9` but not `vp09`, which is how sites serving VP9 inside an mp4 spell it. Correcting the test on its own would have made things worse: pairing one of those streams with the AAC track sitting beside it asks for a file ffmpeg cannot build, and the download dies at the merge with part files left behind. WebM now also insists on audio a WebM can legally carry, and steps aside on sites that have none.

- **Any page open in your browser could talk to Aevum.** The window you use is served on a local port, and nothing in the app looked at where a request had come from — a site you happened to be visiting could aim one at that port, and the download request carries the folder to write into. Aevum answers only the page it opened itself now, and turns away anything arriving under another name or from another origin.

## New

- **Aevum can update itself.** Settings names the version you are running and looks for a newer one when you open the panel — not at launch, because a download tool has no business phoning home before it is asked. When there is one, the button fetches it, checks it against the SHA-256 the release publishes, and hands over. Each of the four packages knows how to replace itself: the installed build starts the installer and closes, an AppImage overwrites itself, a portable copy lands beside the old one with the folder open, and a tar.gz unpacks into a versioned folder next to the one you are running so the working copy stays working. Nothing is opened that does not match its published hash, and a file that fails the check is deleted rather than left lying around.

- **A word before you download.** Choose H.264 for a video whose H.264 stops below the size you asked for, and Aevum says so before the download starts rather than quietly handing back less. It also says when a site keeps its codecs to itself — H.264 is usually there and usually found, but not promised — and warns outright when a video has no H.264 at all, where the closest thing to it is whatever happens to be sitting in an mp4.

- **Errors worth reading.** "Requested format is not available" was the whole message when the H.264 option refused rather than hand back a codec it had not promised. It says which choice could not be met now.

## Changed

- **Asking for 1080p on a vertical video handed back 480p.** A filter reading `height <= 1080` means 1080p only while a video is wider than it is tall; on a 1080x1920 short it matched the 480x854 stream instead, because 854 is under 1080, and stopped there. Quality is chosen by the short side now — the number a person means by 1080p, whichever way the video is turned — so a vertical 1080p is 1080x1920 and a vertical 720p is 720x1280. Landscape selection is unchanged at every step.

- **Downloads carry their quality in the name.** `Big Buck Bunny [aqz-KE-bpKQ] 1080p.mp4`. Two qualities of the same video used to resolve to one filename: the second download found the file already sitting there, skipped it, and reported success while the first file stayed put. You could ask for 1080p, be handed nothing, and keep the 720p you already had without ever being told. H.264 adds its own name on top — it writes `.mp4` files exactly like MP4 does, and an extension cannot tell those two apart. MKV and WebM say what they are in the extension already and add nothing. The number is the short side, so it agrees with the chip that was clicked: a 1080x1920 short is 1080p, not 1920p. It is read back off the finished file once there is one, which is also how a video whose site reported no size at all still ends up labelled. Audio downloads keep their plain name.

## H.264 and 4K

H.264 stops at 1080p because that is where the sites stop making it. Above 1080p you get VP9 or AV1, and editors open neither — After Effects refuses AV1 outright and imports VP9 with sound but no picture. There is no 4K H.264 to fetch, by Aevum or anything else. If you need 4K in an editor, download it and convert it yourself.

## Under the hood

- **ffprobe ships now.** yt-dlp reads a finished file's metadata through it, and without it `--add-metadata` gave up with "ffprobe not found" on some sites — a warning you never saw, and metadata that never arrived. It comes from the same archive as the bundled ffmpeg, so the two are the same build. It costs about 34 MB in the download.

- **Stop, and quitting, could hang.** Ending a download kills the process tree with `taskkill`, and nothing put a limit on how long that was allowed to take — it can sit there indefinitely when the process it is ending is stuck waiting on a driver. Closing Aevum went the same way, because it stops running jobs on the way out. It gives up after fifteen seconds now and carries on.

- The guide panel said MP4 "plays everywhere" and called H.264 a guarantee. Neither was true. Both now describe what the buttons actually do, in all eight languages, and MP4 and MKV have the tooltips they were missing.
