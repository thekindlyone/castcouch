# castcouch

Userscript for Firefox for Android: opens a site's video player in Chrome, where its Cast button works.
Sites: anipm.pk and its ani.* domains, cinejoy.pk.

**Install** (Tampermonkey, Firefox for Android) – open this URL:
<https://raw.githubusercontent.com/thekindlyone/castcouch/main/src/send-to-chrome.user.js>

Tampermonkey then updates it from the same URL whenever `@version` goes up.

**Use**: start an episode or film → tap the blue **Cast** button (anipm: in the page header, right of the logo; cinejoy: top right of the player, shown when paused or just touched) → tap the Cast icon in Chrome.
The player in Firefox is paused when you tap it. If the button is missing, the same action is in the Tampermonkey menu.

- [Findings](docs/FINDINGS.md)
- [Phone checklist](docs/PHONE-CHECKLIST.md)

## Development

    npm install && npx playwright install firefox
    npm test            # unit + Playwright Firefox tests against the local harness (needs ffmpeg)
    npm run harness     # site on :8001, player on :8002; serves a harness build of the script
