# Aevum 1.2.7

The audio stream you actually have, a cover to go with it, and a clearer way to bring your own signed-in session.

## New

- **Cover art for audio downloads.** The existing Thumbnail option is now available in Audio mode too. MP3, M4A, Opus and FLAC can carry the cover inside the file; Original and WAV keep it as a separate JPG. The site still has to provide an image. Adding a cover alone does not re-encode the audio.

- **Original audio.** A new format choice keeps the available audio-only stream without converting it. Its codec and quality come from the source. If a site only offers audio already joined to video, choose one of the converted formats instead. Stream-copy clips may start or end a little away from the requested boundary.

- **A cookie button that tries the browser first.** Select Chrome, Edge, Firefox, Brave or Zen, then press the small button at the end of the row. Aevum asks yt-dlp to read that browser's cookies and shows a check when it has collected a usable jar. This step is local: no video is downloaded and no network request is needed to collect the cookies.

  If the profile cannot be found, the database is locked, decryption fails, the jar is empty or the operation times out, the reason stays visible and a file picker opens. On Windows it starts in Downloads when available. If the web browser blocks an asynchronous picker, a small “Select cookies.txt” button remains available.

- **Manual cookies.txt import.** The fallback accepts an exported Netscape-format cookie file, not Chrome's raw Cookies database or Firefox/Zen's cookies.sqlite. The helper under the row explains the difference for the selected browser. A check confirms the file was read; it does not promise that the account can access every video.

- **Zen, next to Brave.** It uses yt-dlp's Firefox reader with Zen's own profile directory. It does not silently borrow the session from Firefox.

## Fixed

- **A preview could disagree with the download about login.** The preview did not receive the browser-cookie selection. Both paths now use the same authentication state, and cached previews are separated by browser and imported session.

- **An old reply could overwrite a new selection.** Changing browser or URL invalidates pending results. Removing imported cookies returns new requests to normal browser extraction; downloads already accepted into the queue retain their own snapshot.

- **Cookie failures looked like generic download errors.** Login, age verification, locked databases and decryption failures now have distinct messages. Retrying is not used to conceal an authentication failure.

- **Cancellation could release the update gate too soon.** An accepted job remains protected while its process is being started. Previews and cookie extraction are coordinated with maintenance, so an updater cannot replace yt-dlp underneath them.

- **Failed requests could clear the pasted link.** The URL now remains available to correct the options and try again.

- **Different audio requests could collide on one filename.** Audio format, bitrate and clip range distinguish the output names.

- **The highest Opus bitrate failed for mono sources.** The upper option is now 256k instead of 320k; Auto leaves the encoder's default in charge.

## Changed

- **Bitrate labels say what they can promise.** MP3 still offers 320k, but it is an encoding target, not evidence that the source contained that much detail. VBR best and Auto replace the misleading source-quality label where appropriate. Converting to FLAC or WAV cannot restore information missing from a lossy source.

- **All new messages are available in eight languages**, including cookie status, error details and audio hints. The Windows file dialog and installer are localized too. The existing compact layout and subdued helper text are preserved.

- **Aevum's selected options take precedence over external yt-dlp configuration.** Download and preview calls ignore external config and persistent cache settings. Authentication-bearing info JSON is not embedded in downloaded files.

## Privacy and limits

Collected and imported cookies stay available for the current backend session, including between downloads. They are not saved into settings or download history and are not returned to the page. Each child process receives its own temporary copy because yt-dlp may write back to its jar; that copy is removed after the operation.

Quitting Aevum clears the session and attempts to remove remaining temporary copies. On Windows, closing only the browser tab leaves the tray application running. A force-kill or power loss cannot guarantee cleanup, but a later launch never restores the previous session's cookies. Your original exported file is not changed or deleted.

Automatic collection may read cookies for multiple sites in the selected browser profile. Keep manual exports private and export only what you need. Chromium's Windows encryption is not bypassed: if it prevents automatic reading, use your own exported cookies.txt. Account permissions and age-verification requirements still apply.

## Under the hood

- The Windows and Linux builds use the same pinned yt-dlp release, 2026.08.16.020253. FFmpeg remains on the previously supported lines: Windows 8.0 and Linux 7.1, with the required encoders checked.
- Regression coverage includes synthetic authenticated downloads, Firefox/Zen cookie databases, audio covers and packet-preserving muxing, queue/cancellation, maintenance, session cleanup and all eight UI languages.
- Release checksums keep the filename-first format understood by existing Aevum updaters. Windows and Linux assets are assembled only when their source commit and source hashes agree.

Windows builds are unsigned; Windows may show a SmartScreen warning.
