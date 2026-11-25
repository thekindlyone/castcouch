// ==UserScript==
// @name         Send to Chrome
// @namespace    https://github.com/thekindlyone/castcouch
// @version      4.1.0
// @description  Opens the embedded video player in Chrome for Android, where it can be cast. Adds a Cast button to the page header.
// @match        *://anipm.pk/*
// @match        *://*.anipm.pk/*
// @match        *://ani.pm/*
// @match        *://*.ani.pm/*
// @match        *://ani.nz/*
// @match        *://*.ani.nz/*
// @match        *://ani.vg/*
// @match        *://*.ani.vg/*
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

function parse(url) {
  try { return new URL(url); } catch { return null; }
}
const isHttp = u => !!u && (u.protocol === 'https:' || u.protocol === 'http:');

function isPlayerUrl(url) {
  const u = parse(url);
  return isHttp(u) && PLAYER_HOSTS.includes(u.host) && u.pathname !== '/';
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
const findPlayer = () => findPlayerFrame()?.src ?? null;

// The player is cross-origin, so ask the copy of this script running inside it to pause.
const PAUSE = 'send-to-chrome:pause';
function pausePlayer() {
  findPlayerFrame()?.contentWindow?.postMessage(PAUSE, '*');
}

function listenForPause() {
  let holdUntil = 0; // also catches a player that was still loading when the button was tapped
  window.addEventListener('message', e => {
    if (e.source !== window.parent || e.data !== PAUSE) return;
    holdUntil = Date.now() + 5000;
    document.querySelectorAll('video').forEach(v => v.pause());
  });
  document.addEventListener('play', e => { if (Date.now() < holdUntil) e.target.pause?.(); }, true);
  for (const t of ['pointerdown', 'keydown']) document.addEventListener(t, e => { if (e.isTrusted) holdUntil = 0; }, true);
}

// A plain link, so the launch comes from a real tap. It is a popover: shown in the top layer (above any
// z-index) and kept outside <body>, so the site does not re-render it. Sits in the gap after the logo.
let button = null;
function updateButton() {
  const frame = findPlayerFrame();
  if (!frame) { button?.remove(); return; }
  if (!button) {
    button = document.createElement('a');
    button.textContent = 'Cast';
    button.title = 'Open player in Chrome';
    button.setAttribute('popover', 'manual');
    button.style.cssText = 'position:fixed;inset:auto;top:10px;left:124px;width:57px;height:36px;margin:0;padding:0;border:0;border-radius:18px;background:#1a73e8;color:#fff;font:600 14px/36px sans-serif;text-align:center;text-decoration:none;box-sizing:border-box;overflow:hidden;z-index:2147483647';
    button.addEventListener('click', pausePlayer);
  }
  button.href = toChromeIntent(frame.src);
  if (!button.isConnected) document.documentElement.append(button);
  if (button.showPopover && !button.matches(':popover-open')) button.showPopover();
  // Never sit on the video (landscape, theatre modes).
  button.style.visibility = 'visible';
  const b = button.getBoundingClientRect(), f = frame.getBoundingClientRect();
  const overlaps = b.left < f.right && b.right > f.left && b.top < f.bottom && b.bottom > f.top;
  // Scrolled under the fixed header is fine: only hide if the video is actually what is drawn there.
  if (overlaps) {
    button.style.visibility = 'hidden';
    const under = document.elementFromPoint((b.left + b.right) / 2, (b.top + b.bottom) / 2);
    if (under !== frame) button.style.visibility = 'visible';
  }
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
    setInterval(updateButton, 1000);
  }
}

if (typeof GM_registerMenuCommand === 'function') main();
