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
