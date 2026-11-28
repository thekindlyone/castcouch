// ==UserScript==
// @name         Send to Chrome
// @namespace    https://github.com/thekindlyone/castcouch
// @version      4.7.1
// @description  anipm: opens the player in Chrome for Android, where it can be cast (Cast button). cinejoy: plays on the TV through the castcouch service on the home PC (castcouch button).
// @match        *://anipm.pk/*
// @match        *://*.anipm.pk/*
// @match        *://ani.pm/*
// @match        *://*.ani.pm/*
// @match        *://ani.nz/*
// @match        *://*.ani.nz/*
// @match        *://ani.vg/*
// @match        *://*.ani.vg/*
// @match        *://cinejoy.pk/*
// @match        *://www.cinejoy.pk/*
// @match        *://embed.settlar.io/*
// @match        *://beta.cno.to/*
// @run-at       document-start
// @grant        GM_registerMenuCommand
// @grant        GM_setClipboard
// @grant        GM_xmlhttpRequest
// @grant        GM_getValue
// @grant        GM_setValue
// @connect      192.168.88.8
// @connect      *
// @homepageURL  https://github.com/thekindlyone/castcouch
// @downloadURL  https://raw.githubusercontent.com/thekindlyone/castcouch/main/src/send-to-chrome.user.js
// @updateURL    https://raw.githubusercontent.com/thekindlyone/castcouch/main/src/send-to-chrome.user.js
// ==/UserScript==

// Hosts (with port, if any) that serve the player inside an <iframe>. The harness rewrites this line.
const PLAYER_HOSTS = ['embed.settlar.io', 'beta.cno.to', 'megaplay.buzz'];
// Sites whose own watch page is the player (full-window <video>, no iframe). Harness rewrites this too.
const PAGE_PLAYER_HOSTS = ['cinejoy.pk', 'www.cinejoy.pk'];

function parse(url) {
  try { return new URL(url); } catch { return null; }
}
const isHttp = u => !!u && (u.protocol === 'https:' || u.protocol === 'http:');

function isPlayerUrl(url) {
  const u = parse(url);
  return isHttp(u) && PLAYER_HOSTS.includes(u.host) && u.pathname !== '/';
}

// The watch page itself, without its fragment, or null if this is not a watch page.
function pagePlayerUrl(href) {
  const u = parse(href);
  if (!isHttp(u) || !PAGE_PLAYER_HOSTS.includes(u.host) || !/^\/watch\/./.test(u.pathname)) return null;
  u.hash = '';
  return u.href;
}

// frames: [{src, area}] -> src of the largest player frame, or null.
function pickPlayer(frames) {
  return frames.filter(f => isPlayerUrl(f.src)).sort((a, b) => b.area - a.area)[0]?.src ?? null;
}

// A playlist or whole video file; never a segment, subtitle or blob: URL.
function isStreamUrl(url) {
  const u = parse(url);
  if (!isHttp(u)) return false;
  if (/\.(m3u8|mp4|mpd)$/i.test(u.pathname)) return true;
  // Extensionless opaque objects; segments end in .ts.
  if (/^\/v1\/object\/v2\.[^/]+$/.test(u.pathname)) return !/\.(ts|vtt|m4s)$/i.test(u.pathname);
  return /^\/backup\/v1\/h\//.test(u.pathname);
}

// entries: [{name, initiatorType}] in request order. The master playlist is requested first;
// subtitle <track>s ("other"/"track") can share its URL shape.
function pickStream(entries) {
  const c = entries.filter(e => isStreamUrl(e.name));
  return (c.find(e => /^(fetch|xmlhttprequest|video)$/.test(e.initiatorType)) ?? c[0])?.name ?? null;
}

function toIntent(url, pkg) {
  const u = parse(url);
  if (!isHttp(u)) throw new TypeError('not an http(s) URL: ' + url);
  // "#" starts the Intent section, so a fragment in the URL cannot be carried.
  return `intent://${u.host}${u.pathname}${u.search}#Intent;scheme=${u.protocol.slice(0, -1)};package=${pkg};end`;
}
const toChromeIntent = url => toIntent(url, 'com.android.chrome');
const toVlcIntent = url => toIntent(url, 'org.videolan.vlc');

function findPlayerFrame() {
  const frames = [...document.querySelectorAll('iframe')].map(el => {
    const r = el.getBoundingClientRect();
    return { el, src: el.src, area: r.width * r.height };
  });
  const src = pickPlayer(frames);
  return src && frames.filter(f => f.src === src).sort((a, b) => b.area - a.area)[0].el;
}
const findPlayer = () => pagePlayerUrl(location.href) ?? findPlayerFrame()?.src ?? null;

// Pauses this document's videos, and keeps them paused for a few seconds in case the player was still
// loading or reacts to the same tap. Any later touch or key gives control back to the viewer.
let holdUntil = 0;
function pauseHere() {
  holdUntil = Date.now() + 5000;
  document.querySelectorAll('video').forEach(v => v.pause());
}
function watchHold() {
  document.addEventListener('play', e => { if (Date.now() < holdUntil) e.target.pause?.(); }, true);
  for (const t of ['pointerdown', 'keydown']) document.addEventListener(t, e => { if (e.isTrusted) holdUntil = 0; }, true);
}

// A framed player is cross-origin, so ask the copy of this script running inside it to pause.
const PAUSE = 'send-to-chrome:pause';
function pausePlayer() {
  if (pagePlayerUrl(location.href)) pauseHere();
  else findPlayerFrame()?.contentWindow?.postMessage(PAUSE, '*');
}
function listenForPause() {
  watchHold();
  window.addEventListener('message', e => { if (e.source === window.parent && e.data === PAUSE) pauseHere(); });
}

// A plain link, so the launch comes from a real tap. It is a popover: shown in the top layer (above any
// z-index) and kept outside <body> except in fullscreen, so the site does not re-render it.
let button = null, tvButton = null;
let lastTouch = 0;
function makeButton() {
  const b = document.createElement('a');
  b.textContent = 'Cast';
  b.title = 'Open player in Chrome';
  b.setAttribute('popover', 'manual');
  b.style.cssText = 'position:fixed;inset:auto;top:10px;width:57px;height:36px;margin:0;padding:0;border:0;border-radius:18px;background:#1a73e8;color:#fff;font:600 14px/36px sans-serif;text-align:center;text-decoration:none;box-sizing:border-box;overflow:hidden;z-index:2147483647';
  b.addEventListener('click', pausePlayer);
  return b;
}

// Full-window player: behave like its controls, i.e. visible when paused or recently touched.
function pageButtonVisible() {
  const v = document.querySelector('video');
  return !v || v.paused || Date.now() - lastTouch < 4000;
}

// Framed player: hide only if the video itself is what is drawn under the button.
function coversFrame(frame) {
  const b = button.getBoundingClientRect(), f = frame.getBoundingClientRect();
  if (b.left >= f.right || b.right <= f.left || b.top >= f.bottom || b.bottom <= f.top) return false;
  button.style.visibility = 'hidden';
  return document.elementFromPoint((b.left + b.right) / 2, (b.top + b.bottom) / 2) === frame;
}

// cinejoy-style sites: Chrome's Cast does not reach the TV there, so this is their only button.
function makeTvButton() {
  const b = document.createElement('button');
  b.textContent = 'castcouch';
  b.title = 'Play on the TV through the castcouch service on the PC';
  b.setAttribute('popover', 'manual');
  b.style.cssText = 'position:fixed;inset:auto;top:10px;right:16px;height:36px;margin:0;padding:0 16px;border:0;border-radius:18px;background:#188038;color:#fff;font:600 14px/36px sans-serif;text-align:center;box-sizing:border-box;z-index:2147483647';
  // Its taps are its own: a site that toggles play on any tap must not see them.
  for (const t of ['click', 'pointerdown', 'pointerup']) b.addEventListener(t, e => e.stopPropagation());
  b.addEventListener('click', playOnTv);
  return b;
}

function updateButton() {
  const pageUrl = pagePlayerUrl(location.href);
  // Firefox draws a fullscreen element over everything outside it, even popovers. A fullscreen <video>
  // or <iframe> cannot hold the button, so there it is simply not available.
  const fs = pageUrl ? document.fullscreenElement : null;
  const home = fs && !/^(VIDEO|IFRAME)$/.test(fs.tagName) ? fs : document.documentElement;
  const place = b => {
    if (b.parentNode !== home) home.append(b);
    if (b.showPopover && !b.matches(':popover-open')) b.showPopover();
  };
  if (pageUrl) {
    button?.remove();
    if (!pageButtonVisible()) { tvButton?.remove(); return; }
    tvButton ??= makeTvButton();
    place(tvButton);
    return;
  }
  tvButton?.remove();
  const frame = findPlayerFrame();
  if (!frame) { button?.remove(); return; }
  button ??= makeButton();
  button.style.left = '124px'; // gap between the logo and the header icons
  button.style.right = 'auto';
  button.href = toChromeIntent(frame.src);
  place(button);
  button.style.visibility = coversFrame(frame) ? 'hidden' : 'visible';
}

// Modal <dialog>s are drawn in the top layer, above any z-index the site or an ad uses.
function dialog(id) {
  document.getElementById(id)?.remove();
  const d = document.createElement('dialog');
  d.id = id;
  d.style.cssText = 'position:fixed;margin:auto;max-width:90vw;padding:20px;border:0;border-radius:12px;background:#fff;color:#111;font:16px sans-serif';
  const row = (tag, text, props, parent = d) => {
    const e = Object.assign(document.createElement(tag), props);
    e.textContent = text;
    e.style.cssText = 'display:block;box-sizing:border-box;width:100%;margin:8px 0;padding:14px;border:0;border-radius:8px;background:#1a73e8;color:#fff;text-align:center;text-decoration:none;font:16px sans-serif';
    parent.append(e);
    return e;
  };
  const show = () => { (document.body || document.documentElement).append(d); if (!d.open) d.showModal(); };
  return { d, row, show };
}

function showPanel(url) {
  const { d, row, show } = dialog('stc-panel');
  if (url) {
    row('a', 'Open player in Chrome', { href: toChromeIntent(url), onclick: pausePlayer });
    row('button', 'Copy player link', { onclick: () => GM_setClipboard(url) });
  } else {
    d.append('No player found. Start an episode first.');
  }
  row('button', 'Close', { onclick: () => d.remove() });
  show();
}

// ---- TV: play through the castcouch home service on the PC (home/castcouch_home.py) ----

const RELAY_DEFAULT = '192.168.88.8:8899';
const relayAddress = () => (typeof GM_getValue === 'function' && GM_getValue('relay', '')) || RELAY_DEFAULT;

// Playlists the page's player requested, newest first (the newest is the server playing now), without repeats.
// Requests by script (hls.js) or by the <video> itself (a browser that plays HLS natively, as Firefox for Android
// can); not subtitles, links or images. videoSrc, the address the video is playing, comes first if it is a stream.
function recentStreams(entries, max = 5, videoSrc = null) {
  const c = entries.filter(e => isStreamUrl(e.name) && /^(fetch|xmlhttprequest|video|other|)$/.test(e.initiatorType)).map(e => e.name);
  if (isStreamUrl(videoSrc)) c.push(videoSrc);
  return [...new Set(c.reverse())].slice(0, max);
}
const currentStream = entries => recentStreams(entries)[0] ?? null;

// The browser keeps only the first ~250 resource entries; a player's segments can fill that before the viewer
// switches servers. An observer sees every request, so collect from the start.
let lastSubtitle = null; // the subtitle file the player last loaded: the one picked in its Subtitles menu
let subtitleList = null, subtitleVotes = null; // the lists the page loads, reused to choose English ourselves
const seen = [], traffic = new Map(); // traffic: "host type .ext" -> count, for diagnostics (no paths or tokens)
// cinejoy moves from film to film without loading a new page, so what was seen belongs to the film it was seen on:
// on a new address, forget the old film's streams and subtitles (or the TV gets the previous film).
let filmPath = globalThis.location?.pathname, filmSince = 0;
function checkFilm() {
  if (location.pathname === filmPath) return;
  filmPath = location.pathname;
  filmSince = performance.now();
  seen.length = 0;
  lastSubtitle = subtitleList = subtitleVotes = null;
}
function watchStreams() {
  try {
    setInterval(checkFilm, 200); // notice the move before the new film's requests start
    new PerformanceObserver(list => {
      checkFilm();
      for (const e of list.getEntries()) {
        if (e.startTime < filmSince) continue; // the previous film's, arriving late
        if (isStreamUrl(e.name)) seen.push({ name: e.name, initiatorType: e.initiatorType });
        if (isSubtitleUrl(e.name)) lastSubtitle = e.name;
        else if (/\/subtitles\?/.test(e.name)) subtitleList = e.name; // the page's list of every subtitle file
        else if (/\/votes\//.test(e.name)) subtitleVotes = e.name;
        const u = parse(e.name), ext = (u?.pathname.match(/\.(\w{2,5})$/) || [, '-'])[1];
        const k = `${u?.host} ${e.initiatorType} .${ext}`;
        if (traffic.size < 60 || traffic.has(k)) traffic.set(k, (traffic.get(k) || 0) + 1);
      }
    }).observe({ type: 'resource', buffered: true });
  } catch {}
}
// A single subtitle file (cinejoy: subs2.wing.st/sub/<id>, SRT), not the list of them.
function isSubtitleUrl(url) {
  const u = parse(url);
  return isHttp(u) && (/\.(srt|vtt)$/i.test(u.pathname) || (/(^|\.)subs?\d*\./i.test(u.host) && /^\/sub\/./.test(u.pathname)));
}

// A readable name: cinejoy's address ends in the original file's address, base64-encoded.
function subtitleName(url) {
  const last = parse(url)?.pathname.split('/').pop() || '';
  try {
    const inner = atob(decodeURIComponent(last).replace(/-/g, '+').replace(/_/g, '/'));
    const name = decodeURIComponent(parse(inner)?.pathname.split('/').pop() || '');
    if (/\.(srt|vtt)$/i.test(name)) return name;
  } catch {}
  return /\.(srt|vtt)$/i.test(last) ? decodeURIComponent(last) : 'Subtitles';
}

// list: the page's subtitle list ({subtitles: [{url, language, display, voteRef}]}); votes: {votes: {ref: {up, down}}}.
// The English file with the best vote balance; the list's own order breaks ties. Null if there is none.
function bestEnglish(list, votes) {
  const score = x => { const v = votes?.votes?.[x.voteRef]; return v ? (v.up || 0) - (v.down || 0) : 0; };
  const en = (list?.subtitles || []).map((x, i) => ({ x, i })).filter(({ x }) => /^en/i.test(x.language || '') && x.url);
  en.sort((a, b) => score(b.x) - score(a.x) || a.i - b.i);
  return en[0] ? { url: en[0].x.url, label: en[0].x.display || subtitleName(en[0].x.url) } : null;
}

const autoEnglish = () => typeof GM_getValue !== 'function' || GM_getValue('autoEnglish', true) !== false;
function toggleAutoEnglish() {
  GM_setValue('autoEnglish', !autoEnglish());
  alert(`English subtitles chosen automatically: ${autoEnglish() ? 'on' : 'off'}.\nSubtitles picked in the player are always used first.`);
}

const getJson = async url => { try { const r = await fetch(url, { signal: AbortSignal.timeout(10000) }); return r.ok ? await r.json() : null; } catch { return null; } };

// The subtitles to send: the ones picked in the player, else (if allowed) the best English file. Null for none.
async function pickedSubtitles(auto = true) {
  checkFilm();
  let url = lastSubtitle, label = null;
  if (!url && auto && autoEnglish() && subtitleList) {
    const best = bestEnglish(await getJson(subtitleList), subtitleVotes && await getJson(subtitleVotes));
    if (best) ({ url, label } = best);
  }
  if (!url) return null;
  try {
    const r = await fetch(url, { signal: AbortSignal.timeout(10000) });
    if (!r.ok) return null;
    const track = [...document.querySelectorAll('track')].find(t => t.src === url);
    return { text: await r.text(), label: label || track?.label || subtitleName(url), auto: url !== lastSubtitle };
  } catch { return null; }
}

const streamEntries = () => {
  checkFilm();
  return seen.length ? seen : performance.getEntriesByType('resource').filter(e => e.startTime >= filmSince);
};
const streamsNow = () => recentStreams(streamEntries(), 5, document.querySelector('video')?.currentSrc);

// What the page loaded, without addresses: sent along so the PC's log can show why a stream was not found.
function diagnostics() {
  const v = document.querySelector('video'), src = parse(v?.currentSrc || '');
  return {
    script: '4.7.1', ua: navigator.userAgent.slice(0, 160),
    video: src ? `${src.protocol}//${src.host}${/\.m3u8$/i.test(src.pathname) ? ' (.m3u8)' : ''}` : 'none',
    traffic: [...traffic].sort((a, b) => b[1] - a[1]).map(([k, n]) => `${k} x${n}`).slice(0, 40),
  };
}

// Always resolves, to the service's reply or to an {ok:false} that says what went wrong.
function relayCall(path, body, timeout = 10000) {
  const addr = relayAddress();
  const down = `Can't reach the castcouch service at ${addr}. Is the PC on, and are the PC and this phone on the home Wi-Fi?`;
  return new Promise(resolve => GM_xmlhttpRequest({
    method: body ? 'POST' : 'GET', url: `http://${addr}${path}`, timeout,
    headers: { 'Content-Type': 'application/json' }, data: body ? JSON.stringify(body) : undefined,
    onload: r => {
      try { resolve(JSON.parse(r.responseText)); }
      catch { resolve({ ok: false, code: 'bad-reply', message: `Something at ${addr} answered, but not the castcouch service (HTTP ${r.status}).` }); }
    },
    onerror: () => resolve({ ok: false, code: 'relay-unreachable', message: down }),
    ontimeout: () => resolve({ ok: false, code: 'relay-timeout', message: down + ' (no answer in time)' }),
  }));
}

const FALLBACK_SERVERS = ['Polaris', 'Nebula']; // cinejoy servers whose streams play outside the site, best first
const MANUAL = `Open the player's Servers menu, pick ${FALLBACK_SERVERS.join(' or ')}, then tap castcouch again. (Settings → Playback → Server Order with ${FALLBACK_SERVERS[0]} first makes it the default.)`;
const TV_ADVICE = { 'stream-refused': MANUAL, 'stream-unsupported': MANUAL, 'stream-unreachable': 'Try again in a moment.', 'switch-failed': MANUAL };
const sleep = ms => new Promise(r => setTimeout(r, ms));

// Picks a server in the page's own Servers menu, as the viewer would. False if the menu or server isn't found.
async function switchServer(name) {
  const label = e => ((e.getAttribute('aria-label') || '') + ' ' + (e.innerText || '')).trim();
  const menu = [...document.querySelectorAll('button')].filter(e => /^servers?\b/i.test(label(e))).pop();
  if (!menu) return false;
  menu.click();
  for (let i = 0; i < 10; i++) {
    await sleep(300);
    const item = [...document.querySelectorAll('button,[role=menuitem],[role=option],li,div')].filter(e => (e.innerText || '').trim() === name).pop();
    if (item) { item.click(); return true; }
  }
  menu.click(); // not offered for this film: close the menu again
  return false;
}

// Resolves to true once the player requests a playlist it had not before.
async function newStream(before, ms) {
  for (const end = Date.now() + ms; Date.now() < end; await sleep(500))
    if (streamsNow().some(u => !before.has(u))) return true;
  return false;
}
const clock = s => { s = Math.max(0, Math.round(s)); const h = Math.floor(s / 3600), m = Math.floor(s / 60) % 60; return (h ? h + ':' + String(m).padStart(2, '0') : m) + ':' + String(s % 60).padStart(2, '0'); };

function tvPanel(r) {
  const { d, row, show } = dialog('stc-tv');
  const line = text => { const p = document.createElement('p'); p.style.margin = '4px 0 12px'; p.textContent = text; d.append(p); return p; };
  if (r.busy) {
    line(r.message);
  } else if (r.ok) {
    const st = line(`Playing on ${r.tv || 'the TV'}: ${r.title || 'video'}`);
    const sub = line('');
    const showSubs = x => { sub.textContent = x.has_subtitles ? `Subtitles: ${x.subtitles ? 'on (' + x.subtitles + ')' : 'off'}` : 'Subtitles: none sent'; subBtn.textContent = x.subtitles ? 'Subtitles off' : 'Subtitles on'; subBtn.style.display = x.has_subtitles ? 'block' : 'none'; };
    const update = x => {
      st.textContent = x.ok ? (x.playing ? `${x.state.toLowerCase()} at ${clock(x.position)} on ${x.tv}` : 'Stopped.') : x.message;
      if (x.ok) showSubs(x);
    };
    const act = (label, action, seconds) => row('button', label, { onclick: async () => update(await relayCall('/control', { action, seconds })) });
    act('Pause', 'pause'); act('Resume', 'play'); act('Back 30 s', 'seek', -30); act('Forward 30 s', 'seek', 30);
    const subBtn = row('button', 'Subtitles', { onclick: async () => update(await relayCall('/control', { action: subBtn.textContent === 'Subtitles off' ? 'subtitles-off' : 'subtitles-on' })) });
    row('button', 'Use the subtitles picked in the player', {
      onclick: async () => {
        sub.textContent = 'Sending subtitles…';
        const picked = await pickedSubtitles(false);
        if (!picked) { sub.textContent = 'No subtitles picked in the player yet: close this, choose them under Subtitles in the player, then come back.'; return; }
        update(await relayCall('/subtitles', { subtitles: picked }, 30000));
      },
    });
    act('Stop', 'stop');
    row('button', 'Start again from the phone\'s position', { onclick: () => playOnTv(true) });
    row('a', 'Remote control page', { href: `http://${relayAddress()}/`, target: '_blank' });
    showSubs(r);
  } else {
    line(r.message);
    if (TV_ADVICE[r.code]) line(TV_ADVICE[r.code]);
    row('button', 'Try again', { onclick: playOnTv });
  }
  if (!r.busy) row('button', 'Close', { onclick: () => d.remove() });
  show();
}

function sendToTv(streams, position, subtitles) {
  return relayCall('/play', {
    stream: streams[0], streams, page: location.href, position, diagnostics: diagnostics(), subtitles,
    title: document.title.replace(/\s*[-|–]\s*cinejoy\s*$/i, ''),
  }, 60000);
}

async function playOnTv(restart = false) {
  // Already on the TV from this page: show its controls rather than starting over.
  if (restart !== true) {
    const now = await relayCall('/status', null, 4000);
    if (now.ok && now.playing && now.page === location.href) { tvPanel(now); return now; }
  }
  const streams = streamsNow();
  if (!streams.length) { const r = { ok: false, code: 'no-stream', message: 'No video stream found yet. Start the film, then tap castcouch again.' }; tvPanel(r); return r; }
  tvPanel({ busy: true, message: 'Sending to the TV…' });
  const position = document.querySelector('video')?.currentTime || 0; // now: switching servers rewinds the player
  const subtitles = await pickedSubtitles();
  let r = await sendToTv(streams, position, subtitles);
  // This server only plays inside the site: switch the player to one that plays anywhere, then send that.
  // Not every film has every server, so each fallback is tried in turn.
  const tried = [], firstError = r.message;
  for (const name of FALLBACK_SERVERS) {
    if (r.ok || !/^stream-(refused|unsupported)$|^switch-failed$/.test(r.code)) break;
    tvPanel({ busy: true, message: `That server won't play on the TV. Switching the player to ${name}…` });
    const before = new Set(streamsNow());
    tried.push(name);
    if (await switchServer(name) && await newStream(before, 20000)) {
      tvPanel({ busy: true, message: `Sending ${name} to the TV…` });
      r = await sendToTv(streamsNow(), position, subtitles);
    } else {
      r = { ok: false, code: 'switch-failed', message: `${firstError} Switching the player to ${tried.join(', then ')} by itself didn't work.` };
    }
  }
  if (r.ok) pauseHere();
  tvPanel(r);
  return r;
}

function setRelay() {
  const a = prompt('Address of the PC running the castcouch service (IP:port)', relayAddress());
  if (a != null) GM_setValue('relay', a.trim());
}

function openInChrome() {
  const url = findPlayer();
  if (!url) { showPanel(null); return null; }
  const intent = toChromeIntent(url);
  pausePlayer();
  location.href = intent;
  return intent;
}

function main() {
  if (PLAYER_HOSTS.includes(location.host)) {
    listenForPause();
    // The player iframe is sandboxed without top-navigation/popups, so it cannot launch apps itself.
    GM_registerMenuCommand('[player] Copy stream URL', () => {
      const s = pickStream(performance.getEntriesByType('resource'));
      if (s) GM_setClipboard(s);
      else alert('No stream seen in ' + location.host);
      return s;
    });
  } else if (window.top === window) {
    if (PAGE_PLAYER_HOSTS.includes(location.host)) {
      watchStreams();
      GM_registerMenuCommand('Play on TV (castcouch service)', playOnTv);
      GM_registerMenuCommand('Set castcouch service address', setRelay);
      GM_registerMenuCommand('English subtitles automatically: on/off', toggleAutoEnglish);
    } else {
      GM_registerMenuCommand('Open player in Chrome', openInChrome);
      GM_registerMenuCommand('Open player in Chrome (tap a link)', () => { const u = findPlayer(); showPanel(u); return u; });
    }
    watchHold();
    for (const t of ['pointerdown', 'pointermove', 'keydown']) {
      document.addEventListener(t, () => {
        lastTouch = Date.now();
        if (pagePlayerUrl(location.href)) updateButton();
      }, true);
    }
    document.addEventListener('fullscreenchange', updateButton);
    setInterval(updateButton, 1000);
  }
}

if (typeof GM_registerMenuCommand === 'function') main();
