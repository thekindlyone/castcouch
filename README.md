# castcouch

Userscript for Firefox for Android: opens an embedded video player in Chrome, where its Cast button works.

**Install** (Tampermonkey, Firefox for Android) – open this URL:
<https://raw.githubusercontent.com/thekindlyone/castcouch/main/src/send-to-chrome.user.js>

Tampermonkey then updates it from the same URL whenever `@version` goes up.

**Use**: start an episode → Tampermonkey menu → *Open player in Chrome* → tap the Cast button in Chrome.
If nothing happens, use *Open player in Chrome (tap a link)*.

- [Findings](docs/FINDINGS.md)
- [Phone checklist](docs/PHONE-CHECKLIST.md)

## Development

    npm install && npx playwright install firefox
    npm test            # unit + Playwright Firefox tests against the local harness (needs ffmpeg)
    npm run harness     # site on :8001, player on :8002; serves a harness build of the script
