# castcouch

Userscript for Firefox for Android: opens a site's video player in Chrome, where its Cast button works.
Sites: anipm.pk and its ani.* domains, cinejoy.pk.

**Install** (Tampermonkey, Firefox for Android) – open this URL:
<https://raw.githubusercontent.com/thekindlyone/castcouch/main/src/send-to-chrome.user.js>

Tampermonkey then updates it from the same URL whenever `@version` goes up.

**anipm**: start an episode → tap the blue **Cast** button (page header, right of the logo) → tap the Cast icon in Chrome.
The player in Firefox is paused when you tap it. If the button is missing, the same action is in the Tampermonkey menu.

### cinejoy → TV through the PC (green **castcouch** button)

cinejoy's own Cast does not reach the TV, so a small service on the home PC does it instead: the phone sends it the
stream the player is using, the PC relays it on the home network and tells the TV to play it.

- Start a film and tap **castcouch** (top right of the player, shown when paused or just touched). It is the only
  button on cinejoy: Chrome's Cast does not reach the TV there.
- Only the **Polaris** server plays outside cinejoy. If the player is on another server, the script switches it to
  Polaris itself (through the page's own Servers menu) and sends that. Putting Polaris first in Settings → Playback →
  Server Order skips the switch.
- Firefox pauses, the TV starts where you were, and the panel has Pause/Resume/±30 s/Stop.
- **Subtitles**: English subtitles are chosen automatically (the most up-voted English file in cinejoy's own list;
  Tampermonkey menu → *English subtitles automatically: on/off*). Or pick them in the player's own Subtitles menu first; castcouch sends that file (SRT, converted to
  WebVTT) and the TV starts with them on. The panel switches them off/on, and *Use the subtitles picked in the player*
  swaps in a different file mid-film (the TV reloads at its position). Tapping castcouch while that film is on the TV
  opens these controls instead of starting over.
  The same controls are at `http://<PC>:8899/` (bookmark it).
- If the PC is off or unreachable, the panel says so.
- First tap: Tampermonkey asks to allow the connection to the PC. Allow it ("always").
- PC address: `192.168.88.8:8899` by default; change it in the Tampermonkey menu → *Set castcouch service address*.
  Reserving that address for the PC in the router keeps it from changing.

**Service** (`home/`, Windows, Python 3.9+, no packages): `powershell -ExecutionPolicy Bypass -File home\install.ps1`
installs it to `%USERPROFILE%\castcouch`, starts it, and starts it at every sign-in. A supervisor restarts it if it exits
or stops answering; Task Scheduler restarts the supervisor within 5 minutes. Log: `%USERPROFILE%\castcouch\home.log`.
Remove with `home\uninstall.ps1`. The Wi-Fi must be a *Private* network in Windows (the firewall allows Python there).
Upstream requests carry only the service's own name (`castcouch-relay/…`); no browser identity, Referer or cookies.

### Your own films → TV

Open `http://192.168.88.8:8899/` on the phone (bookmark it): what is playing, its controls, and every film in the
folders listed in `%USERPROFILE%\castcouch\config.json` (`"library"`), with search. Tap a film to play it on the TV;
the TV reads the file straight from the PC (no conversion), starting with English subtitles if *English subtitles* is
ticked: a `.srt` beside the film or in its `Subs` folder, else OpenSubtitles.

- Most files (H.264 video, AAC sound, MP4 or MKV) should play directly. AVI/MOV files are marked *may not play*; files
  with AC3/DTS sound may play without sound. Nothing is converted yet (that would need ffmpeg on the PC).
- **OpenSubtitles**: make a free account at opensubtitles.com, create an API consumer (Profile → API consumers), and put
  its key in `config.json` as `"opensubtitles_api_key"`. Then restart the service (sign out and in, or run the
  installer again). Without a key this step is skipped. Matches by the file's fingerprint first, then by title and
  year; downloads are cached in `%USERPROFILE%\castcouch\subtitles` (free accounts have a daily download limit).
- Installing: `install.ps1 -Library D:\Movies,E:\Movies` sets the folders.

- [Findings](docs/FINDINGS.md)
- [Phone checklist](docs/PHONE-CHECKLIST.md)

## Development

    npm install && npx playwright install firefox
    npm test            # unit + Playwright Firefox tests against the local harness, and the home service
                        # tests with a fake TV (needs ffmpeg, python3, openssl)
    npm run harness     # site on :8001, player on :8002; serves a harness build of the script
