# Phone checklist

1. Remove the old "Send to Chrome" script from Tampermonkey.
2. Open the install URL from the README in Firefox; Tampermonkey should offer to install v4.0.0.
   If it shows plain text instead: Tampermonkey → Utilities → Install from URL.
3. Open an episode, wait until it plays, and use the default or **Backup** server (these had no pop-ups in testing; Beta is less certain).
4. Tampermonkey menu: expect *Open player in Chrome*, *Open player in Chrome (tap a link)* and, from the player frame, *[player] Copy stream URL*.
5. Tap *Open player in Chrome*. Chrome opens with the player → go to 7.
6. Nothing happened? Tap *…(tap a link)*, then the blue link. Still nothing? Use *Copy player link* and paste it into Chrome.
   Firefox → Settings → "Open links in apps" must not be set to "Never".
7. In Chrome: does it play? Any pop-ups? Phone on the same Wi-Fi as the Chromecast, is the Cast button there?
8. Cast. Does the TV play for more than a minute? (This is the one thing that could still fail server-side.)
9. Repeat 5–8 with the Beta server.

If 8 fails on the TV but 7 works: Android's own screen cast (Quick Settings → Cast) from Chrome or Firefox needs no site support.

## cinejoy.pk

1. Start a film or episode. The Cast button is top right when paused or just after a touch, and goes away while watching.
2. Tap it: Firefox pauses, Chrome opens the same title. In Chrome, tap the player and use its own Cast icon.
3. Does it play on the TV for more than a minute? Any pop-ups in Chrome?
