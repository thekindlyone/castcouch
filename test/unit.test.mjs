import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const code = fs.readFileSync(new URL('../src/send-to-chrome.user.js', import.meta.url), 'utf8');
const s = vm.runInNewContext(code + '\n;({pagePlayerUrl,isPlayerUrl,pickPlayer,isStreamUrl,pickStream,currentStream,recentStreams,subtitleName,isSubtitleUrl,bestEnglish,toChromeIntent,toVlcIntent})', { URL, TypeError, atob });
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

test('currentStream takes the newest script-requested playlist (the server now playing)', () => {
  const e = (name, initiatorType) => ({ name, initiatorType });
  const old = 'https://lightspeed.st/hls/a/master.m3u8', now = 'https://erebus.cursed.st/p/b.m3u8?e=1';
  assert.equal(s.currentStream([e(old, 'xmlhttprequest'), e('https://lightspeed.st/x.png', 'xmlhttprequest'), e(now, 'fetch'), e('https://erebus.cursed.st/s1.ts', 'xmlhttprequest'), e('https://a.b/sub.m3u8', 'track')]), now);
  assert.equal(s.currentStream([e('blob:https://cinejoy.pk/1', 'video')]), null);
});

test('recentStreams: newest first, no repeats, only script-requested playlists', () => {
  const e = (name, initiatorType = 'xmlhttprequest') => ({ name, initiatorType });
  const a = 'https://lightspeed.st/a.m3u8', b = 'https://erebus.cursed.st/b.m3u8';
  assert.deepEqual([...s.recentStreams([e(a), e('https://x/1.ts'), e(b), e(a), e('https://a.b/sub.m3u8', 'track')])], [a, b]);
  assert.deepEqual([...s.recentStreams([])], []);
});

test('recentStreams: a natively played stream (video request or video.src) counts, newest first', () => {
  const e = (name, initiatorType) => ({ name, initiatorType });
  const light = 'https://lightspeed.st/a.m3u8', polaris = 'https://erebus.cursed.st/playlists/movie/1/1080p.m3u8?token=x';
  assert.deepEqual([...s.recentStreams([e(light, 'xmlhttprequest'), e(polaris, 'video')])], [polaris, light]);
  assert.deepEqual([...s.recentStreams([e(light, 'xmlhttprequest')], 5, polaris)], [polaris, light]);
  assert.deepEqual([...s.recentStreams([e(light, 'xmlhttprequest')], 5, 'blob:https://cinejoy.pk/1')], [light]);
  assert.deepEqual([...s.recentStreams([e('https://a.b/sub.m3u8', 'track'), e('https://a.b/x.m3u8', 'link')])], []);
});

test('subtitles: which addresses count, and their readable names', () => {
  const b64 = Buffer.from('https://mbpimages.chuaxin.com/movie_box/movie/srt/3/7/35837/957659/Shang-Chi.And.The.Legend.Of.The.Ten.Rings.2021.1080p.BluRay.x264-VETO.srt').toString('base64');
  const url = 'https://subs2.wing.st/sub/' + b64;
  assert.equal(s.isSubtitleUrl(url), true);
  assert.equal(s.isSubtitleUrl('https://subs2.wing.st/subtitles?type=movie&tmdb=1'), false);
  assert.equal(s.isSubtitleUrl('https://subs2.wing.st/votes/movie/1'), false);
  assert.equal(s.isSubtitleUrl('http://127.0.0.1:8012/subs/en.srt'), true);
  assert.equal(s.subtitleName(url), 'Shang-Chi.And.The.Legend.Of.The.Ten.Rings.2021.1080p.BluRay.x264-VETO.srt');
  assert.equal(s.subtitleName('http://a/subs/My%20film.srt'), 'My film.srt');
  assert.equal(s.subtitleName('https://subs2.wing.st/sub/@@@'), 'Subtitles');
});

test('bestEnglish: most up-voted English file; list order breaks ties; none without English', () => {
  const list = { subtitles: [
    { url: 'https://s/sub/fr', language: 'fr', display: 'fr.srt', voteRef: 'f' },
    { url: 'https://s/sub/a', language: 'en', display: 'A.srt', voteRef: 'a' },
    { url: 'https://s/sub/b', language: 'en', display: 'B.srt', voteRef: 'b' },
    { url: 'https://s/sub/c', language: 'en', display: 'C.srt', voteRef: 'c' },
  ] };
  assert.equal(s.bestEnglish(list, { votes: { a: { up: 1, down: 0 }, b: { up: 5, down: 1 }, f: { up: 99 } } }).label, 'B.srt');
  assert.equal(s.bestEnglish(list, null).url, 'https://s/sub/a');
  assert.equal(s.bestEnglish({ subtitles: [list.subtitles[0]] }, null), null);
  assert.equal(s.bestEnglish(null, null), null);
});
