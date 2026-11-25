import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const code = fs.readFileSync(new URL('../src/send-to-chrome.user.js', import.meta.url), 'utf8');
const s = vm.runInNewContext(code + '\n;({pagePlayerUrl,isPlayerUrl,pickPlayer,isStreamUrl,pickStream,toChromeIntent,toVlcIntent})', { URL, TypeError });
const M = 'https://media.settlar.io/v1/object/';

test('isStreamUrl', () => {
  for (const u of ['https://a.b/x.m3u8', 'https://a.b/x.M3U8?t=1', 'https://a.b/x.mp4#t=3', 'http://a.b:8080/x.mpd', M + 'v2.aa.bb-_c.dd', 'https://embed.settlar.io/backup/v1/h/aHR0'])
    assert.equal(s.isStreamUrl(u), true, u);
  for (const u of ['blob:https://a.b/123', 'data:video/mp4,x', 'https://a.b/x.ts', 'https://a.b/p?u=x.m3u8', M + 'v3.aa.bb.ts', M + 'v2.aa.ts', 'https://embed.settlar.io/backup/v1/s/aHR0', 'nonsense', '', undefined])
    assert.equal(s.isStreamUrl(u), false, String(u));
});

test('pickStream takes the first script-requested playlist, not the last or a track', () => {
  const e = (name, initiatorType) => ({ name, initiatorType });
  assert.equal(s.pickStream([e(M + 'v2.sub', 'other'), e(M + 'v2.master', 'fetch'), e(M + 'v2.variant', 'xmlhttprequest'), e(M + 'v3.seg.ts', 'xmlhttprequest')]), M + 'v2.master');
  assert.equal(s.pickStream([e('blob:https://a.b/1', 'video')]), null);
  assert.equal(s.pickStream([]), null);
});

test('pickPlayer takes the largest allow-listed https frame', () => {
  const E = 'https://embed.settlar.io/embed/v1?t=';
  assert.equal(s.pickPlayer([{ src: E + 'small', area: 1 }, { src: E + 'big', area: 9 }, { src: 'https://ads.example/x', area: 99 }, { src: '', area: 99 }]), E + 'big');
  assert.equal(s.pickPlayer([{ src: 'about:blank', area: 9 }]), null);
  assert.equal(s.isPlayerUrl('https://embed.settlar.io.evil.example/embed'), false);
  assert.equal(s.isPlayerUrl('https://embed.settlar.io/'), false);
});

test('intents', () => {
  assert.equal(s.toChromeIntent('https://a.b/p/x.m3u8?t=1&u=2'), 'intent://a.b/p/x.m3u8?t=1&u=2#Intent;scheme=https;package=com.android.chrome;end');
  assert.equal(s.toVlcIntent('http://a.b:8002/x.mp4'), 'intent://a.b:8002/x.mp4#Intent;scheme=http;package=org.videolan.vlc;end');
  assert.equal(s.toChromeIntent('https://a.b/x?q=a%20b;c%23d#frag'), 'intent://a.b/x?q=a%20b;c%23d#Intent;scheme=https;package=com.android.chrome;end');
  for (const u of ['blob:https://a.b/1', 'javascript:alert(1)', 'x']) assert.throws(() => s.toChromeIntent(u));
});

test('pagePlayerUrl', () => {
  assert.equal(s.pagePlayerUrl('https://cinejoy.pk/watch/tv/1399/1/1?x=1#t'), 'https://cinejoy.pk/watch/tv/1399/1/1?x=1');
  for (const u of ['https://cinejoy.pk/movie/423', 'https://cinejoy.pk/watch/', 'https://cinejoy.pk.evil.example/watch/movie/1', 'https://anipm.pk/watch/x'])
    assert.equal(s.pagePlayerUrl(u), null, u);
});
