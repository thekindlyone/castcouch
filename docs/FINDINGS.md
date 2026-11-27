# Findings (2026-11-25)

Measured with Playwright Firefox 159 and Chromium on desktop Linux. One title, episode 1, dub. Phone results are in the next section; everything else is desktop.

## Confirmed by the user on the phone (v4.0.0)

Installing from the GitHub URL, opening Chrome from Firefox for Android, and casting to the Chromecast all work.

## Decision: direct handoff of the player page. No home helper.

Send Chrome the player **iframe's** URL (`https://embed.settlar.io/embed/v1?t=…`), not the media URL.

- Opened cold in headed Chromium (fresh profile, no referrer) it returned 200 and played at 1920 px wide.
- It showed the player's own **"Start casting"** button, and loaded Google's Cast sender SDK. The player's code uses the default media receiver (`CC1AD845`).
- In that cold load the only hosts contacted were `embed.settlar.io` and Cloudflare's beacon (plus gstatic for Cast). The ad scripts are loaded by the outer page, not by the embed.
- The player's host guard script explicitly permits `embed.settlar.io`; nothing is bypassed.
- It keeps subtitles and quality choice, which a bare playlist in Chrome's player would not.
- The top page can read the iframe's `src`, so no request sniffing is needed.

## Servers

| Server | iframe | Stream as seen in the player frame |
|---|---|---|
| default | `embed.settlar.io/embed/v1?t=…` | `media.settlar.io/v1/object/v2.<token>` or `embed.settlar.io/backup/v1/h/<token>.m3u8` |
| Backup | `embed.settlar.io/embed/v1?t=…` (different token) | `embed.settlar.io/backup/v1/h/…` (playlist), `/s/…` (segment), `/v/…` (subtitles) |
| Beta | `beta.cno.to/watch/…&embed=…`, not sandboxed | `beta.cno.to/hls/…`, segments on `flashpod.top` |

Whether Beta can cast is unknown. `megaplay.buzz` is allowed by the site's CSP but was not seen.

## Pop-ups (headed Chromium, fresh profile, no ad blocker, nothing blocked)

Clicked/tapped the video and the player's buttons, with a mouse and with emulated touch, watching for new tabs and main-frame navigation.
Playwright's Chromium starts with no extensions and with Chrome's pop-up blocker off.

| Page | Clicks | Pop-ups | Other |
|---|---|---|---|
| Control: the normal anipm.pk page | 8 | **2** (`velvetcairns.com`), no navigation away | ad scripts ran (`acscdn.com`, `rapidadexchange.com`), so nothing was blocking ads |
| default player page | 15 mouse, 11 touch | 0 | no navigation; hosts: `embed.settlar.io`, Cloudflare beacon, gstatic (Cast) |
| Backup player page | 15 mouse, 11 touch | 0 | as above |
| Beta player page | 13 mouse, 11 touch | 0 | page address was (re)loaded 3 times on its own host; contacted `ap.batspathes.com`, purpose unknown |

Play/pause worked on every page. None of the scripts loaded by the default/Backup pages call `window.open` or contain the ad library's names,
and the embed's CSP only lets scripts load from itself and Google's Cast SDK. Beta's scripts do call `window.open`, in the instances seen for menu items and links.
Limits: one title, short sessions, a desktop user agent. Ads can vary by device, country and time, so Android Chrome still needs checking.

## Stream

- HLS. `media.settlar.io` playlists have **no file extension**, so v3's `\.(m3u8|mp4)` pattern could not match them. Type: `application/vnd.apple.mpegurl`. Segments: `video/mp2t`, `…/v3.<token>.ts`.
- The playlist is fetched from the embed frame itself, not a Worker. An `hls.worker` exists, but the playlist and segments showed up in Resource Timing. The first matching entry is the master; the last is a variant or subtitle playlist.
- Tokens are in the URL. The embed session's `expiresAt` was about 6 hours ahead. Whether they are bound to an IP address was not tested.
- No Referer needed: the bare master URL played in headed Chromium with no referrer. (Headed Firefox downloads it instead; it has no native HLS.)
- `curl` got Cloudflare's "you have been blocked" page (403) for playlist and segment, **with and without** Referer/Origin. Headless Chromium got the same. So the block is about the client, not the Referer.
- Consequence: VLC, `catt`, or a proxy are likely to be refused too (VLC itself was not tested). Getting them through would mean disguising the client to defeat that block, which this project does not do. The player's Cast button is the supported route.
- Not known: whether the Chromecast's own requests are accepted. The player is built to cast, so it should be, but only a real device can show it.

## Why v3 would have failed, and v2's badge

- v3: the extension pattern above, plus it navigated from inside the player frame. That iframe is sandboxed `allow-scripts allow-same-origin allow-presentation allow-orientation-lock`, i.e. without top-navigation or popups. Inferred, not proven: desktop Firefox refuses `intent:` everywhere as an unknown protocol, so the sandbox's effect could not be isolated.
- v2: **cause not found.** Its source was not in the handoff. A badge rebuilt from the description, injected without Tampermonkey, was visible in both frames, was never removed over ~30 s, and raised no error. That argues against the site removing it, and points at the script or Tampermonkey.
- Seen with ads unblocked: a full-page ad layer (`#dontfoid`) covered the badge in hit tests and swallowed clicks. v4's fallback uses a modal `<dialog>` (top layer), which stays above it.

## v4.1.0 button and pause

- The button is a popover (top layer) outside `<body>`, at x=124-181, y=10-46, in the gap between the logo and the header icons. The header is fixed and 56 px tall; the video starts below it. Checked on the real page at 360 and 412 px wide and in landscape.
- It is re-created if removed, and hidden if the video itself is drawn under it.
- Tapping it posts a message to the copy of the script inside the player frame, which pauses the video. On the real site (default and Backup) it stayed paused for the 7 s observed.
- With ads unblocked, the tap also opened an ad tab, as any click on that page does. An ad blocker is still needed in Firefox.

## Not tested

The v4.1.0 button and pause in real Tampermonkey on the phone, where the v2 badge failed for an unknown reason. The button and pause on the Beta server's own player (`beta.cno.to`).

# cinejoy.pk (2026-11-25, v4.2.0)

- No iframe: the watch page is the player (`/watch/movie/<id>`, `/watch/tv/<id>/<season>/<episode>`), an hls.js `blob:` video filling the window. The site sends `frame-ancestors 'none'`. Tested server: the default one only.
- **Handed to Chrome: the watch page URL.** Opened cold in headed, unblocked Chromium it played and showed its own "Cast to Device" button, with Google's Cast SDK loaded and reporting `NO_DEVICES_AVAILABLE` (no Chromecast on the test machine).
- The bare stream (`lightspeed.st/hls/…/master.m3u8`, segments named `.png`) returned 404 when opened cold in Chromium; why was not investigated. Not used.
- Pop-ups: 0 in 7 mouse clicks and 7 emulated taps, nothing blocked, no new tabs. Unlike anipm's embed, this is the full site page, and the site's own notice says "Ads are disabled for now", so ads may come back, and would then appear in Chrome. Player code is obfuscated, so it was not checked for pop-up calls.
- The watch page reloaded its own address a few times during load in Chromium.
- Playback position is not carried over; Chrome starts where Chrome last was, or at the beginning.
- Button: top right, the empty corner at 360, 412 and 860 px wide. Visible when paused or within 4 s of a touch. Firefox covers everything outside a fullscreen element, so in fullscreen the button is moved inside it. Pausing held for 6.5 s on a film and an episode, and in fullscreen on a film.
- Not tested: the phone, a real Chromecast, other servers.

## Casting on the real TV (v4.2.0)

Reported by the user: Firefox → Chrome works and the Cast session starts (title and poster appear), but the TV stays on the Cast icon.
The receiver (Google default, `CC1AD845`) runs on another origin, so each server was tested from a foreign-origin page in headed Chromium. Two titles per server (Polaris: four). `localhost` stood in for the receiver; the sender's actual load request was not captured.

| Server | Foreign-origin fetch | Opened cold | Segments |
|---|---|---|---|
| Light (default) | fails | 404 | fMP4 named `.png` |
| Nebula | fails | 200 | fMP4 (`image/jpeg` playlist) |
| Sirius | fails | 404 | not checked |
| Polaris | **200, plays with hls.js (4 of 4 titles, 1080p)** | 200 | MPEG-TS behind a 64-byte PNG header, `image/png` |

- Likely cause: the default server only answers the cinejoy page itself. Not worked around here; that would mean faking the page's identity.
- Polaris was the only candidate, and **the user reports it sticks on the Cast icon too**.
- Follow-up, one title, Chromium's native HLS player (control: the plain harness stream played): Polaris direct failed (error 4). The same playlist relayed by a local server, with segment URLs rewritten to its own `/s.ts`, **played, both with and without the PNG header**. So the header alone is not the cause; the relay also changed the segments' address and origin, and which of these matters was not isolated. Chromium is not the TV, so the TV's cause is still unproven.
- That relay was a plain Node client with no added headers; playlist and segments answered 200. So a home relay for Polaris looks feasible without disguising anything. Not tested against the TV.
- The TV (65PUT7908/79) is a Google TV model according to Philips' product page title; its browser options were not tested.
- The choice resets on reload, but cinejoy has Settings → Playback → Server Order (stored per browser).

## Home relay test on the real TV (2026-11-28)

- Setup (prototype, since replaced by `home/castcouch_home.py`): a Node relay in WSL (plain client, no added headers; rewrote the playlist to its own segment names and dropped the 64-byte PNG header), a Python forwarder on Windows (Wi-Fi port → WSL), and a minimal CASTV2 sender (default receiver `CC1AD845`, `application/x-mpegurl`, TS segments).
- Windows network profile had to be Private (the existing python.exe allow rule is Private only). The phone reached the relay over Wi-Fi.
- cinejoy Polaris, Shang-Chi, 1080p H.264 Main / AAC: **the TV played it**: `BUFFERING → PLAYING`, position counting to 36 s, 8 requests from `CrKey/1.56.5 DeviceType/AndroidTV`, all 200. **Confirmed by the user: picture and sound on the TV.**
- Polaris stream links expire: one 2 days old returned 403. Each play needs a fresh link.
- lightflix not tried through the relay.

## Home service and TV button (2026-11-28, service 1.0.0, script 4.3.0)

- `home/castcouch_home.py` runs on Windows (Python 3.9). Windows Python's default `Python-urllib` name gets Cloudflare
  `error code: 1010` (403) from Polaris; its own name `castcouch-relay/1.0` gets 200. No other headers are sent.
- The phone supplies the stream URL (the newest playlist the page requested); a link obtained in Firefox played from
  the PC, so the link is not tied to the browser that fetched it.
- Bugs found by the fake-TV tests, all in the Cast connection: reading and writing one TLS socket from two threads
  corrupts it ("bad record mac"); TLS housekeeping makes the socket look readable with no message, which had stalled
  every first connection by 10 s; an empty MEDIA_STATUS after Stop must clear the state; a TV that goes silent without
  closing is now detected (status probe before each play; 20 s without heartbeat answers).
- Windows quirk: tools started from the Claude desktop app had writes to `AppData\Local` redirected to a private copy,
  invisible to Task Scheduler (task error 0x8007010B). The service therefore lives in `%USERPROFILE%\castcouch`.
- Recovery, tested on the PC: service killed → back in ~6 s; supervisor killed → service keeps running and a new
  supervisor (task restart, or the 5-minute trigger) adopts it; adopted service killed → back in ~10 s.
- End to end on the real site, Firefox (Playwright, headed) with the real script, real service, fake TV: default server
  → "refused (HTTP 404)" with Polaris advice and nothing sent to the TV; Polaris → TV loaded the relayed 1080p stream at
  Firefox's position, Firefox paused, ±30 s worked, no pop-ups. Not yet tried from the phone on the real TV.
- The PC never sleeps on mains power. It does not sign in automatically, so after a reboot the service starts at sign-in.

## v4.3.1 / v4.4.0 (2026-11-28)

- Phone, first try (4.3.0): the service was reached (6 requests in its log) but every stream was refused with 404
  although the player showed Polaris. Which server was sent was not logged. Likely cause: the script read Firefox's
  resource list, which keeps only the first ~250 entries. 4.3.1 collects playlists with a PerformanceObserver and
  sends the newest few; the service tries them in order and names the refusing server. With the list forced to 50
  entries on the real site, Polaris was still found and played (fake TV).
- The service cannot find the Polaris link itself: cinejoy gets stream links through an encrypted request
  (`api2.wing.st/g`, built by a WebAssembly module), and Polaris links carry a token. Reproducing that outside the
  page would mean defeating that protection; not done.
- 4.4.0 instead switches the page's own player to Polaris (its Servers menu) when the current server is refused, waits
  for the new playlist and sends it, keeping the position from before the switch. Real site, Firefox, fake TV: one tap
  on the default server → Light refused (404) → switched → Polaris played from 5:03. cinejoy has only the castcouch
  button now.

## v4.4.1 (2026-11-28)

- Phone with 4.3.1: the service log named only `lightspeed.st` (Light), 4–5 addresses per try, never Polaris
  (`erebus.cursed.st`), though the player showed Polaris. Desktop Firefox records Polaris as an `xmlhttprequest`.
  Unconfirmed guess: on Android, cinejoy hands Polaris to the `<video>` itself (Firefox for Android can play HLS
  natively), a request type the script ignored. 4.4.1 also accepts `video`/`other` requests and the video's own
  address, and on failure sends a summary of what the page loaded (hosts, request types, extensions; no paths or
  tokens) that the service logs.

## Subtitles (v4.5.0, service 1.1.0, 2026-11-28)

- Phone with 4.4.1 played The Batman on the TV: Light refused, switched to Polaris by itself (service log).
- cinejoy lists subtitles from `subs2.wing.st/subtitles?type=movie&tmdb=<id>` and, when one is picked, fetches it
  from `subs2.wing.st/sub/<base64 of the original file URL>`: SRT, `text/plain`, CORS `*`. It adds hundreds of
  `<track>` elements (all `hidden`) and draws cues itself, so the picked file is recognised by that fetch.
- The script re-reads the picked file in the page and sends its text; the service converts SRT to WebVTT (drops
  `<font>` and `{\an8}`-style tags) and serves it with CORS `*` next to the stream; LOAD carries it as text track 1,
  active. Real site + fake TV: VETO BluRay English file arrived as WebVTT and was switched on. Not yet seen on the TV.
