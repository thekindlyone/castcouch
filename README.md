# castcouch

Userscript for Firefox for Android: opens a site's video player in Chrome, where its Cast button works.
Sites: anipm.pk and its ani.* domains, cinejoy.pk.

**Install** (Tampermonkey, Firefox for Android) – open this URL:
<https://raw.githubusercontent.com/thekindlyone/castcouch/main/src/send-to-chrome.user.js>

Tampermonkey then updates it from the same URL whenever `@version` goes up.

**Use**: start an episode or film → tap the blue **Cast** button (anipm: in the page header, right of the logo; cinejoy: top right of the player, shown when paused or just touched) → tap the Cast icon in Chrome.
The player in Firefox is paused when you tap it. If the button is missing, the same action is in the Tampermonkey menu.

### cinejoy → TV through the PC (green **TV** button)

cinejoy's own Cast does not reach the TV, so a small service on the home PC does it instead: the phone sends it the
stream the player is using, the PC relays it on the home network and tells the TV to play it.

- Use the **Polaris** server (Servers menu; or Settings → Playback → Server Order, Polaris first). Other servers refuse
  anyone but cinejoy, and the panel says so.
- Tap **TV** (left of Cast). Firefox pauses, the TV starts where Firefox was, and the panel has Pause/Resume/±30 s/Stop.
  The same controls are at `http://<PC>:8899/` (bookmark it).
- If the PC is off or unreachable, the panel says so and offers *Open in Chrome instead*.
- First tap: Tampermonkey asks to allow the connection to the PC. Allow it ("always").
- PC address: `192.168.88.8:8899` by default; change it in the Tampermonkey menu → *Set castcouch service address*.
  Reserving that address for the PC in the router keeps it from changing.

**Service** (`home/`, Windows, Python 3.9+, no packages): `powershell -ExecutionPolicy Bypass -File home\install.ps1`
installs it to `%USERPROFILE%\castcouch`, starts it, and starts it at every sign-in. A supervisor restarts it if it exits
or stops answering; Task Scheduler restarts the supervisor within 5 minutes. Log: `%USERPROFILE%\castcouch\home.log`.
Remove with `home\uninstall.ps1`. The Wi-Fi must be a *Private* network in Windows (the firewall allows Python there).
Upstream requests carry only the service's own name (`castcouch-relay/…`); no browser identity, Referer or cookies.

- [Findings](docs/FINDINGS.md)
- [Phone checklist](docs/PHONE-CHECKLIST.md)

## Development

    npm install && npx playwright install firefox
    npm test            # unit + Playwright Firefox tests against the local harness, and the home service
                        # tests with a fake TV (needs ffmpeg, python3, openssl)
    npm run harness     # site on :8001, player on :8002; serves a harness build of the script
