// Two-origin imitation of the site: :8001 = page, :8002 = player. Run: npm run harness
// Player options (query string): ?worker=1 (playlist fetched by a Worker), ?blob=1 default hls.js.
// /ref/... mirrors /v1/... but answers 403 without a Referer. ?overlay=1 on the site adds a click-stealing layer.
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import { execFileSync } from 'node:child_process';

const root = path.resolve(import.meta.dirname, '..');
const media = path.join(root, 'harness/media');
// Same sandbox tokens as observed on the real player iframe.
const SANDBOX = 'allow-scripts allow-same-origin allow-presentation allow-orientation-lock';

export function makeMedia() {
  if (fs.existsSync(path.join(media, 'out.m3u8'))) return;
  fs.mkdirSync(media, { recursive: true });
  execFileSync('ffmpeg', ['-loglevel', 'error', '-f', 'lavfi', '-i', 'testsrc=duration=20:size=640x360:rate=25', '-pix_fmt', 'yuv420p', '-g', '50', '-hls_time', '4', '-hls_list_size', '0', path.join(media, 'out.m3u8')]);
}

// The shipped script with harness hosts swapped in, so the real code is what gets tested. pageHost is the
// cinejoy-style site (its own player); siteHost the anipm-style one (framed player). They may be the same.
export function harnessScript(playerHost, siteHost, pageHost = siteHost) {
  const names = [...new Set([siteHost, pageHost, playerHost].map(h => h.split(':')[0]))];
  return fs.readFileSync(path.join(root, 'src/send-to-chrome.user.js'), 'utf8')
    .replace(/^const PLAYER_HOSTS = .*$/m, `const PLAYER_HOSTS = ['${playerHost}'];`)
    .replace(/^const PAGE_PLAYER_HOSTS = .*$/m, `const PAGE_PLAYER_HOSTS = ['${pageHost}'];`)
    .replace(/^\/\/ @(downloadURL|updateURL).*\n/gm, '')
    .replace(/^\/\/ @name .*$/m, '// @name         Send to Chrome (harness)')
    .replace(/^(\/\/ @match .*\n)+/m, names.map(n => `// @match        http://${n}/*\n`).join(''));
}

const playerPage = q => `<!doctype html><title>Player</title><body style="margin:0"><video muted autoplay playsinline style="width:100%"></video>
<script src="/hls.js"></script><script>
const src='${q.get('ref') ? '/ref' : '/v1/object'}/v2.master.abc', v=document.querySelector('video');
function go(u){const h=new Hls({enableWorker:false});h.loadSource(u);h.attachMedia(v);}
${q.get('worker')
    ? `const w=new Worker(URL.createObjectURL(new Blob(["onmessage=async e=>postMessage(await (await fetch(e.data)).text())"])));
       w.onmessage=e=>go(URL.createObjectURL(new Blob([e.data.replace(/^(v3)/gm,location.origin+'/v1/object/$1')],{type:'application/vnd.apple.mpegurl'})));w.postMessage(location.origin+src);`
    : 'go(src);'}
</script>`;

const sitePage = (player, q) => `<!doctype html><meta name=viewport content="width=device-width"><title>Harness site</title>
${q.get('header') ? '<header style="position:fixed;top:0;left:0;right:0;height:56px;background:#333"></header><div style="height:56px"></div>' : ''}
<a href="/send-to-chrome.user.js">install harness userscript</a>
<iframe src="http://${player}/embed/v1?t=v1.abc.def&${q}" sandbox="${SANDBOX}" allow="autoplay; fullscreen" style="width:100%;height:240px;border:0"></iframe>
<nav style="position:fixed;bottom:0;left:0;right:0;height:60px;background:#222;z-index:2147483647"></nav>
${q.get('overlay') ? '<div id="overlay" style="position:fixed;inset:0;z-index:2147483647"></div>' : ''}`;

// A site that is its own player: full-window hls.js video that toggles on tap, like cinejoy.
// ?ref=1: stream from the Referer-checking path (refused off-site), with a Servers menu whose "Polaris" switches
// to the open path; ?nopolaris=1 offers "Nebula" (open path) instead; ?noswitch=1 leaves the menu out; ?nostream=1: no video loaded yet; ?native=1: the <video> is given the playlist itself (native HLS); ?sub=1: a subtitle file is picked (fetched, as cinejoy does); ?sublist=1: only the subtitle list and votes are loaded.
const pagePlayer = (player, q) => `<!doctype html><meta name=viewport content="width=device-width"><title>Watch</title><body style="margin:0;background:#000"><div id="box">
<video muted autoplay loop playsinline style="position:fixed;inset:0;width:100%;height:100%;z-index:2147483647"></video></div>
${q.get('sublist') ? `<script>fetch('http://${player}/subtitles?type=movie&tmdb=12');fetch('http://${player}/votes/movie/12')</script>` : ''}
${q.get('sub') ? `<track kind="captions" label="English test" src="http://${player}/subs/en.srt"><script>fetch('http://${player}/subs/en.srt')</script>` : ''}
${q.get('noswitch') ? '' : `<button aria-label="Servers" onclick="menu.hidden=false" style="position:fixed;bottom:10px;left:10px;z-index:2147483647">S</button>
<div id="menu" hidden style="position:fixed;bottom:50px;left:10px;z-index:2147483647;background:#fff"><div>Light</div><div onclick="h.loadSource('http://${player}/v1/object/v2.master.${q.get('nopolaris') ? 'neb' : 'abc'}');menu.hidden=true">${q.get('nopolaris') ? 'Nebula' : 'Polaris'}</div></div>`}<script src="http://${player}/hls.js"></script><script>
const v=document.querySelector('video'),h=new Hls({enableWorker:false});${q.get('native') ? `v.src='http://${player}/v1/object/v2.master.abc';` : `${q.get('nostream') ? '' : `h.loadSource('http://${player}/${q.get('ref') ? 'ref/master.m3u8' : 'v1/object/v2.master.abc'}');`}h.attachMedia(v);`}
document.addEventListener('dblclick',()=>box.requestFullscreen());
document.addEventListener('click',()=>v.paused?v.play():v.pause());</script>`;

function send(res, status, type, body) { res.writeHead(status, { 'content-type': type, 'access-control-allow-origin': '*' }); res.end(body); }

export function start({ sitePort = 8001, playerPort = 8002, bind = '127.0.0.1', siteHost, playerHost } = {}) {
  makeMedia();
  const handler = kind => (req, res) => {
    const u = new URL(req.url, 'http://x'), name = req.headers.host.split(':')[0];
    const player = playerHost ?? (name === 'localhost' ? '127.0.0.1' : name) + ':' + playerPort;
    if (kind === 'site') {
      if (u.pathname === '/send-to-chrome.user.js') return send(res, 200, 'text/javascript', harnessScript(player, req.headers.host));
      if (u.pathname.startsWith('/watch/')) return send(res, 200, 'text/html', pagePlayer(player, u.searchParams));
      return send(res, 200, 'text/html', sitePage(player, u.searchParams));
    }
    if (u.pathname === '/embed/v1') return send(res, 200, 'text/html', playerPage(u.searchParams));
    if (u.pathname === '/subtitles') return send(res, 200, 'application/json', JSON.stringify({ total: 3, subtitles: [
      { url: `http://${player}/subs/fr.srt`, language: 'fr', display: 'French.srt', voteRef: 'f' },
      { url: `http://${player}/subs/other.srt`, language: 'en', display: 'Other English.srt', voteRef: 'o' },
      { url: `http://${player}/subs/en.srt`, language: 'en', display: 'Best English.srt', voteRef: 'b' }] }));
    if (u.pathname === '/votes/movie/12') return send(res, 200, 'application/json', JSON.stringify({ votes: { f: { up: 50, down: 0 }, o: { up: 1, down: 0 }, b: { up: 4, down: 1 } } }));
    if (u.pathname === '/subs/en.srt') return send(res, 200, 'text/plain; charset=utf-8', '1\r\n00:00:01,000 --> 00:00:04,000\r\n<i>Hello from the harness</i>\r\n');
    if (u.pathname === '/hls.js') return send(res, 200, 'text/javascript', fs.readFileSync(path.join(root, 'node_modules/hls.js/dist/hls.min.js')));
    const m = u.pathname.match(/^\/(v1\/object|ref)\/(v2\.master\.(?:abc|neb)|master\.m3u8|v3\.out\d+\.ts)$/);
    if (!m) return send(res, 404, 'text/plain', 'not found');
    if (m[1] === 'ref' && !req.headers.referer) return send(res, 403, 'text/plain', 'Referer required');
    if (m[2].startsWith('v2') || m[2] === 'master.m3u8') return send(res, 200, 'application/vnd.apple.mpegurl', fs.readFileSync(path.join(media, 'out.m3u8'), 'utf8').replace(/^out/gm, 'v3.out'));
    return send(res, 200, 'video/mp2t', fs.readFileSync(path.join(media, m[2].slice(3))));
  };
  const servers = [http.createServer(handler('site')).listen(sitePort, bind), http.createServer(handler('player')).listen(playerPort, bind)];
  return () => Promise.all(servers.map(s => new Promise(r => { s.closeAllConnections(); s.close(r); })));
}

if (process.argv[1] === import.meta.filename) {
  start({ bind: process.env.BIND ?? '0.0.0.0' });
  console.log('site http://localhost:8001/  player http://127.0.0.1:8002/');
}
