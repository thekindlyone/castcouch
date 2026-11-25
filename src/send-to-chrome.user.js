// ==UserScript==
// @name         Send to Chrome
// @namespace    https://github.com/thekindlyone/castcouch
// @version      4.2.0
// @description  Opens the video player in Chrome for Android, where it can be cast. Adds a Cast button to the page.
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
let button = null;
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

function updateButton() {
  const pageUrl = pagePlayerUrl(location.href);
  const frame = pageUrl ? null : findPlayerFrame();
  const url = pageUrl ?? frame?.src;
  if (!url || (pageUrl && !pageButtonVisible())) { button?.remove(); return; }
  button ??= makeButton();
  button.style.left = pageUrl ? 'auto' : '124px'; // framed: gap between the logo and the header icons
  button.style.right = pageUrl ? '16px' : 'auto';
  button.href = toChromeIntent(url);
  // Firefox draws a fullscreen element over everything outside it, even popovers. A fullscreen <video>
  // or <iframe> cannot hold the button, so there it is simply not available.
  const fs = pageUrl ? document.fullscreenElement : null;
  const home = fs && !/^(VIDEO|IFRAME)$/.test(fs.tagName) ? fs : document.documentElement;
  if (button.parentNode !== home) home.append(button);
  if (button.showPopover && !button.matches(':popover-open')) button.showPopover();
  button.style.visibility = 'visible';
  if (frame) button.style.visibility = coversFrame(frame) ? 'hidden' : 'visible';
}

// Modal <dialog>s are drawn in the top layer, above any z-index the site or an ad uses.
function showPanel(url) {
  document.getElementById('stc-panel')?.remove();
  const d = document.createElement('dialog');
  d.id = 'stc-panel';
  d.style.cssText = 'position:fixed;margin:auto;max-width:90vw;padding:20px;border:0;border-radius:12px;background:#fff;color:#111;font:16px sans-serif';
  const row = (tag, text, props) => {
    const e = Object.assign(document.createElement(tag), props);
    e.textContent = text;
    e.style.cssText = 'display:block;box-sizing:border-box;width:100%;margin:8px 0;padding:14px;border:0;border-radius:8px;background:#1a73e8;color:#fff;text-align:center;text-decoration:none;font:16px sans-serif';
    d.append(e);
  };
  if (url) {
    row('a', 'Open player in Chrome', { href: toChromeIntent(url), onclick: pausePlayer });
    row('button', 'Copy player link', { onclick: () => GM_setClipboard(url) });
  } else {
    d.append('No player found. Start an episode first.');
  }
  row('button', 'Close', { onclick: () => d.remove() });
  (document.body || document.documentElement).append(d);
  d.showModal();
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
    GM_registerMenuCommand('Open player in Chrome', openInChrome);
    GM_registerMenuCommand('Open player in Chrome (tap a link)', () => { const u = findPlayer(); showPanel(u); return u; });
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
