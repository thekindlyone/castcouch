// Opens a page in plain Playwright Firefox and logs requests from every frame.
// usage: node tools/probe.mjs <url> <out.json> [seconds]
import { firefox } from 'playwright';
import fs from 'node:fs';
const [url, out, secs = '25'] = process.argv.slice(2);
const b = await firefox.launch();
const p = await (await b.newContext()).newPage();
const log = [];
p.context().on('request', async r => {
  let frame = null; try { frame = r.frame().url(); } catch {}
  const e = { url: r.url(), type: r.resourceType(), method: r.method(), frame };
  log.push(e);
  try { e.headers = await r.allHeaders(); e.status = (await r.response())?.status(); } catch {}
});
p.on('worker', w => log.push({ worker: w.url() }));
p.on('console', m => m.type() === 'error' && log.push({ console: m.text().slice(0, 300) }));
p.on('pageerror', e => log.push({ pageerror: String(e).slice(0, 300) }));
const res = await p.goto(url, { waitUntil: 'domcontentloaded', timeout: 45000 });
await p.waitForTimeout(+secs * 1000);
console.log('status', res?.status(), p.url());
for (const f of p.frames()) console.log('frame', f.url(), '|', (await f.title().catch(() => '')), '|', (await f.locator('body').innerText({ timeout: 2000 }).catch(() => '')).replace(/\s+/g, ' ').slice(0, 300));
console.log('links', (await p.$$eval('a[href]', a => a.map(x => x.getAttribute('href')))).filter(h => /ani|watch/i.test(h)).slice(0, 15));
await p.screenshot({ path: out.replace(/json$/, 'png') });
fs.writeFileSync(out, JSON.stringify(log, null, 1));
const hosts = {}; for (const e of log) if (e.url) { const h = new URL(e.url).host || e.url.slice(0, 20); hosts[h] = (hosts[h] || 0) + 1; }
console.log(hosts);
await b.close();
