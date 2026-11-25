import test, { before, after } from 'node:test';
import assert from 'node:assert/strict';
import { firefox } from 'playwright';
import { start, harnessScript } from '../harness/server.mjs';

const SITE = 'http://localhost:8011/', P = '127.0.0.1:8012';
let stop, browser;
before(async () => { stop = start({ sitePort: 8011, playerPort: 8012 }); browser = await firefox.launch(); });
after(async () => { await browser?.close(); await stop?.(); });

// Stand-in for Tampermonkey: same code, in every frame, commands recorded per frame.
async function open(query = '') {
  const ctx = await browser.newContext({ viewport: { width: 412, height: 860 } });
  await ctx.addInitScript(`window.__cmd={};window.__clip=null;
    window.GM_registerMenuCommand=(n,f)=>{__cmd[n]=f};window.GM_setClipboard=t=>{__clip=t};
    (()=>{${harnessScript(P, 'localhost:8011')}\n})();`);
  const page = await ctx.newPage();
  await page.goto(SITE + '?' + query);
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
