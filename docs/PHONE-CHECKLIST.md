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

## cinejoy → TV through the PC (v4.3.0)

1. Tampermonkey updates the script to 4.3.0 (or: Tampermonkey → Utilities → Check for updates).
2. Phone on the home Wi-Fi. Open `http://192.168.88.8:8899/ping` once: it should say `"service": "castcouch"`.
3. Start a film, open Servers, pick **Polaris**, let it play a few seconds.
4. Tap the green **TV** button (top right, left of Cast). Allow Tampermonkey's connection prompt ("Always allow").
5. Expect: Firefox pauses, the panel says "Playing on 65PUT7908/79", the TV plays from the same point.
6. Try Pause, Forward 30 s, Stop in the panel. Try `http://192.168.88.8:8899/` as a remote.
7. On another server (e.g. the default one): the panel should explain and suggest Polaris; the TV is left alone.
8. Turn the phone's Wi-Fi off and tap TV: the panel should say it can't reach the service and offer Chrome.
9. Does the film play to the end? (Polaris links may expire; how long they last is not known.)

### v4.4.0 changes

- cinejoy now has only the green **castcouch** button (no Chrome Cast).
- Start the film on any server and tap castcouch: on a server other than Polaris the panel should say it is switching
  to Polaris, then "Playing on …", and the TV starts where you were.

### v4.5.0: subtitles

1. In the player, Subtitles → English → pick a file. Then tap castcouch.
2. Expect "Subtitles: on (<file name>)" in the panel and subtitles on the TV. Readable size? In sync?
3. Subtitles off / on in the panel. Then close the panel, pick another file in the player, tap castcouch: the panel
   should show the controls (not restart); *Use the subtitles picked in the player* should switch the TV to it.
