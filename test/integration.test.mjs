import test, { before, after } from 'node:test';
import assert from 'node:assert/strict';
import { firefox } from 'playwright';
import { start, harnessScript } from '../harness/server.mjs';
import { spawn } from 'node:child_process';
import readline from 'node:readline';

// anipm-style site (framed player) and cinejoy-style site (its own player): same server, two names.
const SITE = 'http://localhost:8011/', PAGE = 'http://127.0.0.1:8011/', P = '127.0.0.1:8012';
let stop, browser, tv, service;
const SERVICE = 8013, tvEvents = [];
before(async () => {
  stop = start({ sitePort: 8011, playerPort: 8012 }); browser = await firefox.launch();
  // The real home service and the fake TV (both Python).
  tv = spawn('python3', ['test/fake_tv.py']);
  const lines = readline.createInterface({ input: tv.stdout });
  const port = await new Promise(ok => lines.once('line', l => ok(JSON.parse(l).port)));
  lines.on('line', l => tvEvents.push(JSON.parse(l)));
  service = spawn('python3', ['home/castcouch_home.py', '--bind', '127.0.0.1', '--port', String(SERVICE), '--tv', '127.0.0.1:' + port, '--log', '/tmp/castcouch-test-home.log']);
  for (let i = 0; i < 50; i++) { try { if ((await fetch(`http://127.0.0.1:${SERVICE}/ping`)).ok) break; } catch {} await new Promise(r => setTimeout(r, 100)); }
});
after(async () => { await browser?.close(); await stop?.(); service?.kill(); tv?.kill(); });

// Stand-in for Tampermonkey: same code, in every frame, commands recorded per frame.
async function open(query = '', path = '') {
  const base = path.startsWith('watch/') ? PAGE : SITE;
  const ctx = await browser.newContext({ viewport: { width: 412, height: 860 } });
  await ctx.addInitScript(`window.__cmd={};window.__clip=null;window.__relay='127.0.0.1:${SERVICE}';
    window.GM_registerMenuCommand=(n,f)=>{__cmd[n]=f};window.GM_setClipboard=t=>{__clip=t};
    window.GM_getValue=(k,d)=>k==='relay'?__relay:d;window.GM_setValue=()=>{};
    window.GM_xmlhttpRequest=d=>{const c=new AbortController(),t=setTimeout(()=>{c.abort();d.ontimeout?.()},d.timeout||30000);
      fetch(d.url,{method:d.method,headers:d.headers,body:d.data,signal:c.signal})
        .then(async r=>{clearTimeout(t);d.onload?.({status:r.status,responseText:await r.text()})})
        .catch(e=>{if(!c.signal.aborted){clearTimeout(t);d.onerror?.(e)}})};
    (()=>{${harnessScript(P, 'localhost:8011', '127.0.0.1:8011')}\n})();`);
  const page = await ctx.newPage();
  await page.goto(base + path + '?' + query);
  const frame = page.frames().find(f => f.url().includes(P));
  return { ctx, page, frame };
}
const playing = f => f.waitForFunction(() => document.querySelector('video')?.currentTime > 0.2, null, { timeout: 20000 });
const PLAYER = q => `intent://${P}/embed/v1?t=v1.abc.def&${q}#Intent;scheme=http;package=com.android.chrome;end`;

test('commands are registered in the right frames', async () => {
  const { ctx, page, frame } = await open();
  assert.deepEqual(await page.evaluate(() => Object.keys(__cmd)), ['Open player in Chrome', 'Open player in Chrome (tap a link)']);
  assert.deepEqual(await frame.evaluate(() => Object.keys(__cmd)), ['[player] Copy stream URL']);
  await ctx.close();
  const w = await open('', 'watch/movie/1'); // cinejoy-style: TV only, no Chrome
  assert.deepEqual(await w.page.evaluate(() => Object.keys(__cmd)), ['Play on TV (castcouch service)', 'Set castcouch service address']);
  await w.ctx.close();
});

test('top page: builds the Chrome intent for the cross-origin player', async () => {
  const { ctx, page } = await open();
  assert.equal(await page.evaluate(() => __cmd['Open player in Chrome']()), PLAYER(''));
  await ctx.close();
});

test('player frame: finds the master playlist although video.src is blob:', async () => {
  const { ctx, frame } = await open();
  await playing(frame);
  assert.match(await frame.evaluate(() => document.querySelector('video').src), /^blob:/);
  assert.equal(await frame.evaluate(() => __cmd['[player] Copy stream URL']()), `http://${P}/v1/object/v2.master.abc`);
  assert.equal(await frame.evaluate(() => __clip), `http://${P}/v1/object/v2.master.abc`);
  await ctx.close();
});

test('Worker-fetched playlist: handoff still works (it does not need the stream URL)', async () => {
  const { ctx, page, frame } = await open('worker=1');
  await playing(frame);
  assert.equal(await page.evaluate(() => __cmd['Open player in Chrome']()), PLAYER('worker=1'));
  // Known limit: Resource Timing cannot see the Worker's request.
  frame.page().on('dialog', d => d.dismiss());
  assert.equal(await frame.evaluate(() => __cmd['[player] Copy stream URL']()), null);
  await ctx.close();
});

test('link panel is on top of, and tappable through, a full-page max-z-index overlay', async () => {
  const { ctx, page } = await open('overlay=1');
  await page.evaluate(() => __cmd['Open player in Chrome (tap a link)']());
  const link = page.locator('#stc-panel a');
  assert.equal(await link.getAttribute('href'), PLAYER('overlay=1'));
  await page.evaluate(() => document.querySelector('#stc-panel a').addEventListener('click', e => { e.preventDefault(); window.__tapped = true; }));
  await link.click({ timeout: 3000 });
  assert.equal(await page.evaluate(() => window.__tapped), true);
  await ctx.close();
});

const BTN = 'a[popover]';
test('button: appears without the menu, outside the video, above an overlay, and pauses the player when tapped', async () => {
  const { ctx, page, frame } = await open('overlay=1&header=1');
  await playing(frame);
  const btn = page.locator(BTN);
  await btn.waitFor({ timeout: 4000 });
  assert.equal(await btn.getAttribute('href'), PLAYER('overlay=1&header=1'));
  const b = await btn.boundingBox(), f = await page.locator('iframe').boundingBox();
  assert.ok(b.y + b.height <= f.y, 'button must end above the video');
  await btn.click({ timeout: 3000 }); // a real click; fails if anything covers it
  await frame.waitForFunction(() => document.querySelector('video').paused, null, { timeout: 3000 });
  await page.waitForTimeout(1500);
  assert.equal(await frame.evaluate(() => document.querySelector('video').paused), true);
  await ctx.close();
});

test('button: comes back if removed, follows the episode, goes away with the player', async () => {
  const { ctx, page } = await open('header=1');
  await page.locator(BTN).waitFor({ timeout: 4000 });
  await page.evaluate(s => document.querySelector(s).remove(), BTN);
  await page.locator(BTN).waitFor({ timeout: 4000 });
  await page.evaluate(() => { document.querySelector('iframe').src += 'ep=2'; });
  await page.waitForFunction(s => document.querySelector(s)?.getAttribute('href').includes('ep=2'), BTN, { timeout: 4000 });
  await page.evaluate(() => document.querySelector('iframe').remove());
  await page.locator(BTN).waitFor({ state: 'detached', timeout: 4000 });
  await ctx.close();
});

test('button: hidden when the video itself is under it', async () => {
  const { ctx, page } = await open();
  await page.locator(BTN).waitFor({ state: 'attached', timeout: 4000 });
  await page.evaluate(() => { document.querySelector('iframe').style.cssText += 'position:fixed;inset:0;height:100%'; });
  await page.locator(BTN).waitFor({ state: 'hidden', timeout: 4000 });
  await ctx.close();
});

test('pause: holds a player that starts late, until the viewer touches it; ignores other senders', async () => {
  const { ctx, page, frame } = await open('header=1');
  await playing(frame);
  await frame.evaluate(() => window.postMessage('send-to-chrome:pause', '*')); // not from the parent
  await page.waitForTimeout(300);
  assert.equal(await frame.evaluate(() => document.querySelector('video').paused), false);
  await page.evaluate(() => document.querySelector('iframe').contentWindow.postMessage('send-to-chrome:pause', '*'));
  await frame.waitForFunction(() => document.querySelector('video').paused);
  await frame.evaluate(() => document.querySelector('video').play().catch(() => {}));
  await page.waitForTimeout(300);
  assert.equal(await frame.evaluate(() => document.querySelector('video').paused), true);
  await page.mouse.click(200, 200);
  await frame.evaluate(() => document.querySelector('video').play().catch(() => {}));
  await page.waitForTimeout(300);
  assert.equal(await frame.evaluate(() => document.querySelector('video').paused), false);
  await ctx.close();
});

const TV = 'button[popover]';
const showTv = async page => { await page.mouse.move(150, 420); await page.locator(TV).waitFor({ timeout: 3000 }); };

test('page player: only the castcouch button (no Chrome Cast), top right, and its tap does not toggle the player', async () => {
  const { ctx, page } = await open('', 'watch/tv/1/2/3');
  await playing(page);
  await showTv(page);
  assert.equal(await page.locator(TV).innerText(), 'castcouch');
  assert.equal(await page.locator(BTN).count(), 0, 'no Chrome Cast link on this site');
  const b = await page.locator(TV).boundingBox();
  assert.ok(b.x + b.width > 412 - 30 && b.y < 30, 'top right: ' + JSON.stringify(b));
  await page.evaluate(() => { window.__relay = '127.0.0.1:1'; }); // keep the TV out of this one
  await page.locator(TV).click({ timeout: 3000 });
  await page.locator('#stc-tv').waitFor({ timeout: 5000 });
  assert.equal(await page.evaluate(() => document.querySelector('video').paused), false, 'the tap must not reach the page');
  await ctx.close();
});

test('page player: button hides while watching, returns on touch and when paused, gone off the watch page', async () => {
  const { ctx, page } = await open('', 'watch/movie/1');
  await playing(page);
  const btn = page.locator(TV);
  await btn.waitFor({ state: 'detached', timeout: 8000 });
  await page.mouse.move(120, 420);
  await btn.waitFor({ timeout: 500 });
  await btn.waitFor({ state: 'detached', timeout: 8000 });
  await page.evaluate(() => document.querySelector('video').pause());
  await btn.waitFor({ timeout: 2500 });
  await page.evaluate(() => history.pushState(null, '', '/movie/1'));
  await btn.waitFor({ state: 'detached', timeout: 2500 });
  await ctx.close();
});

test('page player: button is on top in fullscreen, and after leaving it', async () => {
  const { ctx, page } = await open('', 'watch/movie/1');
  await playing(page);
  const onTop = () => page.evaluate(s => { const r = document.querySelector(s)?.getBoundingClientRect(); return !!r && document.elementFromPoint(r.x + 20, r.y + 15) === document.querySelector(s); }, TV);
  await page.mouse.dblclick(100, 300);
  await page.waitForFunction(() => document.fullscreenElement);
  await page.waitForTimeout(500);
  await page.mouse.move(120, 320);
  assert.equal(await onTop(), true);
  await page.evaluate(() => document.exitFullscreen());
  await page.waitForFunction(() => !document.fullscreenElement);
  await page.mouse.move(130, 330);
  await page.waitForTimeout(300);
  assert.equal(await onTop(), true);
  await ctx.close();
});

test('no player yet: explains instead of failing', async () => {
  const { ctx, page } = await open();
  await page.evaluate(() => document.querySelector('iframe').remove());
  assert.equal(await page.evaluate(() => __cmd['Open player in Chrome']()), null);
  await page.getByText('No player found').waitFor({ timeout: 3000 });
  await ctx.close();
});

test('harness Referer mode: 403 without, 200 with', async () => {
  const u = `http://${P}/ref/v2.master.abc`;
  assert.equal((await fetch(u)).status, 403);
  assert.equal((await fetch(u, { headers: { Referer: 'http://x/' } })).status, 200);
});

test('TV: plays on the TV through the home service, pauses Firefox, and the controls work', async () => {
  const { ctx, page } = await open('', 'watch/movie/7');
  await playing(page);
  await page.evaluate(() => { document.querySelector('video').currentTime = 5; });
  await showTv(page);
  tvEvents.length = 0;
  await page.locator(TV).click({ timeout: 3000 });
  await page.locator('#stc-tv').getByText('Playing on').waitFor({ timeout: 30000 });
  const got = tvEvents.find(e => e.fetched);
  assert.ok(got, 'the TV fetched the relayed stream');
  assert.equal(got.ts_at_0, true);
  assert.equal(got.title, 'Watch');
  assert.ok(got.position >= 4, 'starts where Firefox was: ' + got.position);
  assert.equal(await page.evaluate(() => document.querySelector('video').paused), true);
  await page.locator('#stc-tv').getByRole('button', { name: 'Pause' }).click();
  await page.locator('#stc-tv').getByText(/paused at/).waitFor({ timeout: 5000 });
  await page.locator('#stc-tv').getByRole('button', { name: 'Stop' }).click();
  await page.locator('#stc-tv').getByText('Stopped.').waitFor({ timeout: 5000 });
  assert.equal(await page.locator('#stc-tv a').getAttribute('href'), `http://127.0.0.1:${SERVICE}/`);
  await ctx.close();
});

test('TV: service not running → says so, leaves Firefox playing, no Chrome offer', async () => {
  const { ctx, page } = await open('', 'watch/movie/7');
  await playing(page);
  await page.evaluate(() => { window.__relay = '127.0.0.1:1'; });
  await showTv(page);
  await page.locator(TV).click();
  await page.locator('#stc-tv').getByText("Can't reach the castcouch service at 127.0.0.1:1").waitFor({ timeout: 10000 });
  assert.equal(await page.locator('#stc-tv a').count(), 0);
  await page.locator('#stc-tv').getByRole('button', { name: 'Try again' }).waitFor();
  assert.equal(await page.evaluate(() => document.querySelector('video').paused), false);
  await ctx.close();
});

test('TV: a server that refuses outsiders → switches the player to Polaris by itself and plays that', async () => {
  const { ctx, page } = await open('ref=1', 'watch/movie/8');
  await playing(page);
  await page.evaluate(() => { document.querySelector('video').currentTime = 6; });
  tvEvents.length = 0;
  const r = await page.evaluate(() => __cmd['Play on TV (castcouch service)']());
  assert.equal(r.ok, true, JSON.stringify(r));
  const got = tvEvents.find(e => e.fetched);
  assert.ok(got && got.ts_at_0, 'the TV played the Polaris stream');
  assert.ok(got.position >= 5, 'starts where the viewer was before the switch rewound it: ' + got.position);
  assert.match(await page.evaluate(() => performance.getEntriesByType('resource').map(e => e.name).join(' ')), /v1\/object\/v2\.master\.abc/);
  await ctx.close();
});

test('TV: refused and no way to switch → explains how to pick Polaris, nothing sent to the TV', async () => {
  const { ctx, page } = await open('ref=1&noswitch=1', 'watch/movie/8');
  await playing(page);
  tvEvents.length = 0;
  const r = await page.evaluate(() => __cmd['Play on TV (castcouch service)']());
  assert.equal(r.code, 'switch-failed');
  await page.locator('#stc-tv').getByText(/refused the PC/).waitFor({ timeout: 3000 });
  await page.locator('#stc-tv').getByText(/pick Polaris, then tap castcouch again/).waitFor({ timeout: 3000 });
  assert.equal(tvEvents.some(e => e.type === 'LOAD'), false);
  await ctx.close();
});

test('TV: before any video has loaded → asks to start the film first', async () => {
  const { ctx, page } = await open('nostream=1', 'watch/movie/9');
  const r = await page.evaluate(() => __cmd['Play on TV (castcouch service)']());
  assert.equal(r.code, 'no-stream');
  await page.locator('#stc-tv').getByText('Start the film, then tap castcouch again').waitFor({ timeout: 3000 });
  await ctx.close();
});
