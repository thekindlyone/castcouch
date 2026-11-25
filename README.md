# castcouch

Userscript for Firefox for Android: opens an embedded video player in Chrome, where its Cast button works.

**Install** (Tampermonkey, Firefox for Android) – open this URL:
<https://raw.githubusercontent.com/thekindlyone/castcouch/main/src/send-to-chrome.user.js>

Tampermonkey then updates it from the same URL whenever `@version` goes up.

**Use**: start an episode → tap the blue **Cast** button in the page header (right of the logo) → tap the Cast icon in Chrome.
The player in Firefox is paused when you tap it. If the button is missing, the same action is in the Tampermonkey menu.

- [Findings](docs/FINDINGS.md)
- [Phone checklist](docs/PHONE-CHECKLIST.md)

## Development

    npm install && npx playwright install firefox
    npm test            # unit + Playwright Firefox tests against the local harness (needs ffmpeg)
    npm run harness     # site on :8001, player on :8002; serves a harness build of the script
