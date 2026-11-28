"""Tests for the home service. Run: python3 -m unittest discover -s test -p 'test_home.py' (needs openssl)."""
import json
import os
import socket
import struct
import subprocess
import sys
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'home'))
sys.path.insert(0, HERE)
import castcouch_home as ch  # noqa: E402
from fake_tv import FakeTV  # noqa: E402

PNG = b'\x89PNG\r\n\x1a\n' + b'\0' * 56  # 64-byte disguise header, as Polaris sends
TS = (b'\x47' + b'\x11' * 187) * 20
box = lambda kind, body=b'': struct.pack('>I', 8 + len(body)) + kind + body
INIT = box(b'ftyp', b'iso5\0\0\2\0iso5mp41') + box(b'moov', box(b'mvhd', b'\0' * 100))  # as Nebula sends
PIECE = box(b'styp', b'msdh\0\0\0\0msdh') + box(b'sidx', b'\0' * 44) + box(b'moof', b'\0' * 64) + box(b'mdat', b'\x11' * 900)


def free_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Upstream:
    """Imitation video server. Records the User-Agent of every request."""

    def __init__(self):
        self.agents, self.headers = [], []
        up = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                up.agents.append(self.headers.get('User-Agent'))
                up.headers.append(dict(self.headers))
                p = self.path
                if p == '/master.m3u8':
                    body = ('#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1,RESOLUTION=640x360\nlow.m3u8\n'
                            '#EXT-X-STREAM-INF:BANDWIDTH=9,RESOLUTION=3840x2160\nuhd.m3u8\n'
                            '#EXT-X-STREAM-INF:BANDWIDTH=5,RESOLUTION=1920x1080\nhd/media.m3u8?t=1\n').encode()
                elif p.startswith('/hd/media.m3u8') or p == '/media.m3u8':
                    body = b'#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXTINF:4,\nseg0.png?s=a\n#EXTINF:4,\nhttps://cdn.example/x.png\n#EXT-X-ENDLIST\n'
                elif p.startswith(('/hd/seg0.png', '/seg0.png')):
                    body = PNG + TS
                elif p == '/refused.m3u8':
                    self.send_response(403)
                    self.end_headers()
                    return
                elif p == '/neb/master.m3u8':  # Nebula: fMP4 pieces with image names and types
                    body = b'#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=5,RESOLUTION=1920x1080\n1080p/playlist.jpg\n'
                elif p == '/neb/1080p/playlist.jpg':
                    body = (b'#EXTM3U\n#EXT-X-VERSION:7\n#EXT-X-TARGETDURATION:8\n#EXT-X-MAP:URI="init.mp4"\n'
                            b'#EXTINF:7.9,\nplaylist_000.jpg\n#EXTINF:4,\nplaylist_001.png\n#EXT-X-ENDLIST\n')
                elif p == '/neb/1080p/init.mp4':
                    body = INIT
                elif p.startswith('/neb/1080p/playlist_00'):
                    body = PIECE
                elif p == '/badneb.m3u8':
                    body = b'#EXTM3U\n#EXT-X-MAP:URI="neb/1080p/init.mp4"\n#EXTINF:4,\njunk.bin\n'
                elif p == '/fmp4.m3u8':
                    body = b'#EXTM3U\n#EXT-X-MAP:URI="init.mp4"\n#EXTINF:4,\na.m4s\n'
                elif p == '/enc.m3u8':
                    body = b'#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="k"\n#EXTINF:4,\na.ts\n'
                elif p == '/nots.m3u8':
                    body = b'#EXTM3U\n#EXTINF:4,\njunk.bin\n'
                elif p == '/junk.bin':
                    body = b'not video' * 100
                else:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), H)
        self.base = 'http://127.0.0.1:%d' % self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def call(port, path, body=None, timeout=60):
    req = urllib.request.Request('http://127.0.0.1:%d%s' % (port, path), data=json.dumps(body).encode() if body is not None else None,
                                 headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


class Units(unittest.TestCase):
    def test_ts_offset(self):
        self.assertEqual(ch.ts_offset(PNG + TS), 64)
        self.assertEqual(ch.ts_offset(TS), 0)
        self.assertIsNone(ch.ts_offset(b'\x47' * 10))
        self.assertIsNone(ch.ts_offset(b'x' * 5000))

    def test_choose_variant(self):
        self.assertIsNone(ch.choose_variant('#EXTM3U\n#EXTINF:4,\na.ts\n', 'http://a/'))
        m = '#EXTM3U\n#EXT-X-STREAM-INF:RESOLUTION=1280x720\nb/720.m3u8\n#EXT-X-STREAM-INF:RESOLUTION=1920x1080\n1080.m3u8\n#EXT-X-STREAM-INF:RESOLUTION=3840x2160\n4k.m3u8\n'
        self.assertEqual(ch.choose_variant(m, 'http://a/x/master.m3u8'), 'http://a/x/1080.m3u8')
        with self.assertRaises(ch.Failure):
            ch.choose_variant('#EXTM3U\n#EXT-X-STREAM-INF:RESOLUTION=3840x2160\n4k.m3u8\n', 'http://a/')

    def test_rewrite(self):
        pl, segs, init = ch.rewrite('#EXTM3U\n#EXTINF:4,\nseg.png?a=1\n#EXTINF:4,\n/abs/y.ts\n#EXT-X-ENDLIST', 'https://h/p/m.m3u8?t=2')
        self.assertIsNone(init)
        self.assertEqual(pl, '#EXTM3U\n#EXTINF:4,\n0.ts\n#EXTINF:4,\n1.ts\n#EXT-X-ENDLIST\n')
        self.assertEqual(segs, ['https://h/p/seg.png?a=1', 'https://h/abs/y.ts'])
        ch.rewrite('#EXTM3U\n#EXT-X-KEY:METHOD=NONE\n#EXTINF:4,\na.ts\n', 'http://a/')
        pl, segs, init = ch.rewrite('#EXTM3U\n#EXT-X-MAP:URI="init.mp4?t=1"\n#EXTINF:4,\np_000.jpg\n', 'https://n/h/1080p/playlist.jpg')
        self.assertEqual((pl, segs, init), ('#EXTM3U\n#EXT-X-MAP:URI="init.mp4"\n#EXTINF:4,\n0.m4s\n', ['https://n/h/1080p/p_000.jpg'],
                                            'https://n/h/1080p/init.mp4?t=1'))
        self.assertTrue(ch.is_fmp4_piece(PIECE))
        self.assertFalse(ch.is_fmp4_piece(TS) or ch.is_fmp4_piece(INIT) or ch.is_fmp4_piece(b''))
        for bad in ('#EXT-X-MAP:URI="i"\na.m4s\n#EXT-X-MAP:URI="j"\nb.m4s', '#EXT-X-KEY:METHOD=AES-128,URI="k"\na.ts', '#EXT-X-BYTERANGE:1@0\na.ts', '#EXTM3U\n'):
            with self.assertRaises(ch.Failure) as c:
                ch.rewrite(bad, 'http://a/')
            self.assertEqual(c.exception.code, 'stream-unsupported')

    def test_castv2_roundtrip(self):
        frame = ch.encode('sender-0', 'receiver-0', 'receiver', {'type': 'LAUNCH', 'appId': 'CC1AD845', 'x': 'é' * 200})
        m = ch.decode(frame[4:])
        self.assertEqual((m['src'], m['dst'], m['ns']), ('sender-0', 'receiver-0', 'receiver'))
        self.assertEqual(m['data']['x'], 'é' * 200)

    def test_allowed(self):
        for a in ('192.168.88.3', '10.0.0.2', '127.0.0.1', '172.16.5.5'):
            self.assertTrue(ch.allowed(a), a)
        for a in ('8.8.8.8', '1.2.3.4', 'junk'):
            self.assertFalse(ch.allowed(a), a)


class Service(unittest.TestCase):
    """The real service in a thread, an imitation video server, and the fake TV."""

    def setUp(self):
        self.up, self.tv = Upstream(), FakeTV()
        self.port = free_port()
        self.home = ch.Home(self.port, '127.0.0.1:%d' % self.tv.port)
        self.httpd = ch.ThreadingHTTPServer(('127.0.0.1', self.port), ch.make_handler(self.home))
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        if self.home.cast:
            self.home.cast.close()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.up.close()
        self.tv.close()

    def play(self, path, **extra):
        return call(self.port, '/play', dict({'stream': self.up.base + path, 'title': 'Film', 'position': 61.5}, **extra))

    def test_plays_on_tv_and_controls(self):
        self.assertEqual(call(self.port, '/ping')[1]['service'], 'castcouch')
        status, r = self.play('/master.m3u8')
        self.assertEqual((status, r['ok'], r['state']), (200, True, 'PLAYING'), r)
        fetched = [e for e in self.tv.events if 'fetched' in e][0]
        self.assertTrue(fetched['ts_at_0'], 'the TV must get plain MPEG-TS, PNG header removed')
        self.assertEqual((fetched['position'], fetched['title']), (61.5, 'Film'))
        self.assertTrue(fetched['fetched'].startswith('http://127.0.0.1:%d/s/' % self.port))
        # Upstream saw only our own honest name, and no Referer, Origin or Cookie.
        self.assertEqual(set(self.up.agents), {ch.UA})
        for h in self.up.headers:
            self.assertFalse({'Referer', 'Origin', 'Cookie'} & set(h), h)
        self.assertEqual(call(self.port, '/control', {'action': 'pause'})[1]['state'], 'PAUSED')
        self.assertEqual(call(self.port, '/control', {'action': 'seek', 'seconds': 30})[1]['position'], 92)
        self.assertEqual(call(self.port, '/status')[1]['title'], 'Film')
        self.assertFalse(call(self.port, '/control', {'action': 'stop'})[1]['playing'])
        self.assertEqual([e['type'] for e in self.tv.events if 'type' in e][:3], ['GET_STATUS', 'LAUNCH', 'LOAD'])

    def test_plays_nebula_fmp4(self):
        status, r = self.play('/neb/master.m3u8')
        self.assertEqual((status, r['ok'], r['state']), (200, True, 'PLAYING'), r)
        f = [e for e in self.tv.events if 'fetched' in e][0]
        self.assertEqual((f['hls'], f['init_ok'], f['piece_ok']), (['fmp4', 'fmp4'], True, True), f)
        self.assertEqual(f['piece_type'], 'video/mp4', 'not the image type the server claims')
        base = f['fetched'].rsplit('/', 1)[0]
        with self.assertRaises(urllib.error.HTTPError):  # a .ts name is not served for an fMP4 stream
            urllib.request.urlopen(base + '/0.ts', timeout=5)
        self.assertEqual(set(self.up.agents), {ch.UA})
        for h in self.up.headers:
            self.assertFalse({'Referer', 'Origin', 'Cookie'} & set(h), h)

    def test_second_play_reuses_player(self):
        self.play('/master.m3u8')
        self.assertTrue(self.play('/media.m3u8')[1]['ok'])
        self.assertEqual(sum(1 for e in self.tv.events if e.get('type') == 'LAUNCH'), 1)

    def test_old_session_is_gone(self):
        self.play('/master.m3u8')
        old = [e for e in self.tv.events if 'fetched' in e][0]['fetched']
        self.play('/media.m3u8')
        with self.assertRaises(urllib.error.HTTPError) as c:
            urllib.request.urlopen(old, timeout=5)
        self.assertEqual(c.exception.code, 404)

    def test_errors_explain_themselves(self):
        cases = {'/refused.m3u8': 'stream-refused', '/fmp4.m3u8': 'stream-refused', '/badneb.m3u8': 'stream-unsupported', '/enc.m3u8': 'stream-unsupported',
                 '/nots.m3u8': 'stream-unsupported', '/missing.m3u8': 'stream-refused'}
        for path, code in cases.items():
            status, r = self.play(path)
            self.assertEqual((r['ok'], r['code']), (False, code), path)
            self.assertTrue(r['message'])
        self.assertEqual(call(self.port, '/play', {'stream': 'blob:https://x/1'})[1]['code'], 'bad-request')
        self.assertEqual(call(self.port, '/play', {'stream': 'http://127.0.0.1:1/x.m3u8'})[1]['code'], 'stream-unreachable')
        self.assertEqual(call(self.port, '/control', {'action': 'pause'})[1]['code'], 'not-playing')
        self.assertFalse(any(e.get('type') == 'LOAD' for e in self.tv.events), 'nothing may reach the TV for a bad stream')

    def test_falls_back_to_an_older_stream(self):
        status, r = call(self.port, '/play', {'streams': [self.up.base + '/refused.m3u8', self.up.base + '/master.m3u8'], 'title': 'Film'})
        self.assertTrue(r['ok'], r)
        status, r = call(self.port, '/play', {'streams': [self.up.base + '/refused.m3u8', self.up.base + '/fmp4.m3u8']})
        self.assertEqual(r['code'], 'stream-refused', 'the newest stream\'s error is the one reported')
        self.assertIn('127.0.0.1', r['message'], 'names the server')

    def test_subtitles(self):
        srt = '\ufeff1\r\n00:00:01,500 --> 00:00:03,000\r\n<font color="#ffff00">Hello</font> {\\an8}there\r\n\r\n2\r\n00:01:02,000 --> 00:01:04,250\r\nSecond\r\n'
        status, r = self.play('/master.m3u8', subtitles={'text': srt, 'label': 'English (VETO)'})
        self.assertEqual((r['ok'], r['subtitles']), (True, 'English (VETO)'), r)
        tr = [e for e in self.tv.events if 'track' in e][-1]
        self.assertEqual(tr['active'], [1])
        self.assertTrue(tr['track_type'].startswith('text/vtt'))
        self.assertEqual(tr['track_cors'], '*', 'the TV player needs CORS for subtitle files')
        self.assertTrue(tr['track_text'].startswith('WEBVTT\n\n1\n00:00:01.500 --> 00:00:03.000\nHello there'), tr['track_text'])
        self.assertEqual(call(self.port, '/control', {'action': 'subtitles-off'})[1]['subtitles'], None)
        self.assertEqual(call(self.port, '/control', {'action': 'subtitles-on'})[1]['subtitles'], 'English (VETO)')
        # Replace mid-film: reloaded at the TV's position, under a new file name.
        call(self.port, '/control', {'action': 'seek', 'seconds': 100})
        status, r = call(self.port, '/subtitles', {'subtitles': {'text': 'WEBVTT\n\n00:00.000 --> 00:01.000\nNew', 'label': 'Other'}})
        self.assertEqual(r['subtitles'], 'Other')
        tr2 = [e for e in self.tv.events if 'track' in e][-1]
        self.assertNotEqual(tr2['track']['trackContentId'], tr['track']['trackContentId'])
        self.assertIn('New', tr2['track_text'])
        self.assertEqual([e for e in self.tv.events if 'fetched' in e][-1]['position'], 161.5)
        self.assertFalse(call(self.port, '/subtitles', {'subtitles': None})[1]['has_subtitles'])

    def test_no_subtitles(self):
        self.play('/master.m3u8')
        self.assertFalse([e for e in self.tv.events if 'track' in e])
        self.assertEqual(call(self.port, '/control', {'action': 'subtitles-on'})[1]['code'], 'no-subtitles')
        self.assertEqual(self.play('/master.m3u8', subtitles={'text': 'nothing timed', 'label': 'x'})[1]['code'], 'bad-subtitles')

    def test_restart_keeps_feeding_the_tv(self):
        import tempfile
        state = os.path.join(tempfile.mkdtemp(), 'session.json')
        self.home.state = state
        self.play('/master.m3u8', subtitles={'text': 'WEBVTT\n\n00:00.000 --> 00:01.000\nHi', 'label': 'En'})
        url = [e for e in self.tv.events if 'fetched' in e][-1]['fetched']
        # The service goes away (update, crash) and a new one starts on the same port.
        self.home.cast.close()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.home = ch.Home(self.port, '127.0.0.1:%d' % self.tv.port, state=state)
        self.httpd = ch.ThreadingHTTPServer(('127.0.0.1', self.port), ch.make_handler(self.home))
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        with urllib.request.urlopen(url.replace('p.m3u8', '0.ts'), timeout=10) as r:
            self.assertEqual(r.status, 200, 'the TV can go on fetching the same film')
        st = call(self.port, '/status')[1]
        self.assertEqual((st['playing'], st['title'], st['subtitles']), (True, 'Film', 'En'), st)
        self.assertEqual(call(self.port, '/control', {'action': 'pause'})[1]['state'], 'PAUSED')
        self.assertEqual(sum(1 for e in self.tv.events if e.get('type') == 'LOAD'), 1, 'the TV was not restarted')

    def test_tv_off(self):
        self.tv.close()
        status, r = self.play('/master.m3u8')
        self.assertEqual(r['code'], 'tv-not-found')

    def test_tv_refuses_stream(self):
        self.tv.fail_load = True
        self.assertEqual(self.play('/master.m3u8')[1]['code'], 'tv-load-failed')

    def test_reconnects_after_tv_drops(self):
        self.play('/master.m3u8')
        self.tv.drop()
        time.sleep(0.5)
        self.assertTrue(self.play('/master.m3u8')[1]['ok'])

    def test_recovers_from_a_silent_tv(self):
        self.play('/master.m3u8')
        self.tv.freeze()
        t = time.time()
        self.assertTrue(self.play('/master.m3u8')[1]['ok'])
        self.assertLess(time.time() - t, 15)

    def test_remote_page(self):
        with urllib.request.urlopen('http://127.0.0.1:%d/' % self.port, timeout=5) as r:
            self.assertIn(b'castcouch remote', r.read())


def make_library(root):
    """A small Movies folder: films in their own folders with Subs, one loose film, a sample and a tiny file."""
    def put(rel, data):
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'wb') as f:
            f.write(data)
        return path
    film = bytes(range(256)) * 1200  # 300 KB
    put('Knives.Out.2019.1080p.WEBRip.x264-RARBG/Knives.Out.2019.1080p.WEBRip.x264-RARBG.mp4', film)
    put('Knives.Out.2019.1080p.WEBRip.x264-RARBG/Subs/3_French.srt', b'1\n00:00:01,000 --> 00:00:02,000\nBonjour\n')
    put('Knives.Out.2019.1080p.WEBRip.x264-RARBG/Subs/2_English.srt', b'1\n00:00:01,000 --> 00:00:02,000\nHello\n' * 50)
    put('Knives.Out.2019.1080p.WEBRip.x264-RARBG/Sample/sample.mp4', film)
    put('Oldboy (2003)/Oldboy.2003.mkv', film)
    put('Oldboy (2003)/Oldboy.2003.kor.srt', b'1\n00:00:01,000 --> 00:00:02,000\nKorean\n')
    put('Loose.Film.1999.DVDRip.avi', film)
    put('Other.Film.srt', b'1\n00:00:01,000 --> 00:00:02,000\nNot mine\n')
    put('tiny.mp4', b'x' * 10)
    return film


class LocalFilms(unittest.TestCase):
    def test_titles(self):
        for name, want in [('Knives.Out.2019.1080p.WEBRip.x264-RARBG', 'Knives Out (2019)'), ('Oldboy (2003)', 'Oldboy (2003)'),
                           ('Serenity[2005][Aka.Firefly]DvDrip[Eng]-aXXo', 'Serenity (2005)'), ('John Rambo', 'John Rambo'),
                           ('The.Man.From.Nowhere.720p.BDRip.x264.mkv', 'The Man From Nowhere'), ('12 Angry Men(1957)', '12 Angry Men (1957)'),
                           ('Wonder.Woman.1984.2020.IMAX.1080p.WEB.H264-NOGRP.mkv', 'Wonder Woman 1984 (2020)'),
                           ('Blade.Runner.2049.2017.1080p.mkv', 'Blade Runner 2049 (2017)'), ('1917.2019.720p.mp4', '1917 (2019)'),
                           ('Jalsa 2022 Hindi 720p AMZN WEBRip AAC 5.1 ESubs x264', 'Jalsa (2022)'), ('[AnimeRG] Boruto 2015 1080p', 'Boruto (2015)')]:
            self.assertEqual(ch.nice_title(name), want, name)

    def test_generic_names_and_quality(self):
        self.assertTrue(ch.generic_name('part2.mp4') and ch.generic_name('CD1.avi') and not ch.generic_name('Partisan.2015.mp4'))
        self.assertEqual((ch.quality('X.2019.1080p.WEB.mkv'), ch.quality('X.mkv')), ('1080p', None))

    def test_scan_and_subtitle_choice(self):
        import tempfile
        root = tempfile.mkdtemp()
        make_library(root)
        lib = ch.Library([root], min_size=1000)
        items = {i['title']: i for i in lib.listing()}
        self.assertEqual(sorted(items), ['Knives Out (2019)', 'Loose Film (1999)', 'Oldboy (2003)'])
        self.assertTrue(items['Knives Out (2019)']['subs'])
        self.assertFalse(items['Oldboy (2003)']['subs'], 'a Korean file is not English')
        self.assertFalse(items['Loose Film (1999)']['subs'], 'a loose film does not take another film\'s file')
        self.assertTrue(items['Loose Film (1999)']['maybe'])
        k = lib.get(items['Knives Out (2019)']['id'])
        self.assertTrue(ch.local_subtitles(k['path'], k['own_folder']).endswith('2_English.srt'))
        self.assertEqual(ch.Library([root], min_size=1000).listing()[0]['id'], lib.listing()[0]['id'], 'ids are stable')
        with self.assertRaises(ch.Failure):
            lib.get('nope')

    def test_opensubtitles_hash(self):
        import tempfile, struct
        path = os.path.join(tempfile.mkdtemp(), 'f.bin')
        data = os.urandom(300000)
        open(path, 'wb').write(data)
        want = len(data)
        for chunk in (data[:65536], data[-65536:]):
            want = (want + sum(struct.unpack('<8192Q', chunk))) % 2**64
        self.assertEqual(ch.opensubtitles_hash(path), '%016x' % want)
        open(path, 'wb').write(b'x' * 1000)
        self.assertIsNone(ch.opensubtitles_hash(path))


class FakeOpenSubtitles:
    """api.opensubtitles.com in miniature: search (by moviehash or query) and download."""

    def __init__(self):
        self.calls, fake = [], self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def answer(self, obj, ctype='application/json'):
                body = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
                self.send_response(200)
                self.send_header('Content-Type', ctype)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                fake.calls.append(('GET', self.path, self.headers.get('Api-Key'), self.headers.get('User-Agent')))
                if self.path.startswith('/subtitles'):
                    q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                    if 'moviehash' in q:
                        return self.answer({'data': []})  # no fingerprint match: fall back to the title
                    return self.answer({'data': [
                        {'attributes': {'release': 'Worse', 'download_count': 5, 'files': [{'file_id': 1}]}},
                        {'attributes': {'release': 'Loose.Film.1999.BluRay', 'download_count': 900, 'from_trusted': True, 'files': [{'file_id': 2}]}}]})
                if self.path == '/file/2.srt':
                    return self.answer(b'1\r\n00:00:05,000 --> 00:00:07,000\r\nFrom OpenSubtitles\r\n', 'application/x-subrip')

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                fake.calls.append(('POST', self.path, body))
                self.answer({'link': '%s/file/%d.srt' % (fake.base, body['file_id']), 'remaining': 19})
        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), H)
        self.base = 'http://127.0.0.1:%d' % self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()


class LocalService(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.dir = tempfile.mkdtemp()
        self.root = os.path.join(self.dir, 'Movies')
        self.film = make_library(self.root)
        self.os = FakeOpenSubtitles()
        self.tv = FakeTV()
        self.port = free_port()
        self.home = ch.Home(self.port, '127.0.0.1:%d' % self.tv.port, state=os.path.join(self.dir, 'session.json'),
                            library=ch.Library([self.root], min_size=1000),
                            opensubtitles=ch.OpenSubtitles('test-key', os.path.join(self.dir, 'subtitles'), self.os.base))
        self.httpd = ch.ThreadingHTTPServer(('127.0.0.1', self.port), ch.make_handler(self.home))
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.films = {f['title']: f for f in call(self.port, '/library')[1]['items']}

    def tearDown(self):
        if self.home.cast:
            self.home.cast.close()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.os.httpd.shutdown()
        self.tv.close()

    def test_plays_a_film_with_its_english_subtitles(self):
        r = call(self.port, '/play-local', {'id': self.films['Knives Out (2019)']['id'], 'position': 30})[1]
        self.assertEqual((r['ok'], r['subtitles'], r['subtitles_from']), (True, '2_English.srt', 'beside the film'), r)
        got = [e for e in self.tv.events if 'fetched' in e][-1]
        self.assertEqual((got['file_status'], got['file_type'], got['file_bytes'], got['position']), (206, 'video/mp4', 1024, 30))
        self.assertIn('Hello', [e for e in self.tv.events if 'track' in e][-1]['track_text'])
        url = got['fetched']
        # Byte ranges, as players read and seek: middle, the last bytes, beyond the end.
        req = urllib.request.Request(url, headers={'Range': 'bytes=1000-1009'})
        with urllib.request.urlopen(req, timeout=5) as r2:
            self.assertEqual((r2.status, r2.read(), r2.headers['Content-Range']), (206, self.film[1000:1010], 'bytes 1000-1009/%d' % len(self.film)))
        with urllib.request.urlopen(urllib.request.Request(url, headers={'Range': 'bytes=-5'}), timeout=5) as r2:
            self.assertEqual(r2.read(), self.film[-5:])
        with self.assertRaises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(urllib.request.Request(url, headers={'Range': 'bytes=%d-' % len(self.film)}), timeout=5)
        self.assertEqual(e.exception.code, 416)
        with urllib.request.urlopen(url, timeout=5) as r2:
            self.assertEqual(r2.read(), self.film)
        self.assertEqual(call(self.port, '/status')[1]['local'], True)

    def test_mkv_and_no_local_english_uses_opensubtitles(self):
        r = call(self.port, '/play-local', {'id': self.films['Loose Film (1999)']['id']})[1]
        self.assertEqual((r['subtitles'], r['subtitles_from']), ('Loose.Film.1999.BluRay', 'OpenSubtitles'), r)
        self.assertIn('From OpenSubtitles', [e for e in self.tv.events if 'track' in e][-1]['track_text'])
        searches = [c for c in self.os.calls if c[0] == 'GET' and c[1].startswith('/subtitles')]
        self.assertIn('moviehash=', searches[0][1], 'fingerprint first')
        self.assertIn('query=Loose+Film', searches[1][1])
        self.assertIn('year=1999', searches[1][1])
        self.assertEqual(searches[0][2], 'test-key')
        self.assertTrue(searches[0][3].startswith('castcouch v'))
        self.assertEqual([c[2] for c in self.os.calls if c[0] == 'POST'], [{'file_id': 2}], 'the most downloaded, trusted one')
        n = len(self.os.calls)
        call(self.port, '/play-local', {'id': self.films['Loose Film (1999)']['id']})
        self.assertEqual(len(self.os.calls), n, 'second time from the cache')

    def test_without_subtitles_and_type(self):
        r = call(self.port, '/play-local', {'id': self.films['Oldboy (2003)']['id'], 'subtitles': False})[1]
        self.assertEqual(r['has_subtitles'], False)
        self.assertEqual([e for e in self.tv.events if 'fetched' in e][-1]['content_type'], 'video/x-matroska')
        self.assertEqual(call(self.port, '/play-local', {'id': 'nope'})[1]['code'], 'not-found')

    def test_restart_keeps_serving_the_file(self):
        call(self.port, '/play-local', {'id': self.films['Knives Out (2019)']['id']})
        url = [e for e in self.tv.events if 'fetched' in e][-1]['fetched']
        self.home.cast.close()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.home = ch.Home(self.port, '127.0.0.1:%d' % self.tv.port, state=os.path.join(self.dir, 'session.json'))
        self.httpd = ch.ThreadingHTTPServer(('127.0.0.1', self.port), ch.make_handler(self.home))
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        with urllib.request.urlopen(urllib.request.Request(url, headers={'Range': 'bytes=0-9'}), timeout=5) as r:
            self.assertEqual(r.read(), self.film[:10])


class Supervisor(unittest.TestCase):
    def test_restarts_a_crashed_service(self):
        port = free_port()
        log = os.path.join(os.environ.get('TMPDIR', '/tmp'), 'cc-sup-%d.log' % port)
        p = subprocess.Popen([sys.executable, os.path.join(HERE, '..', 'home', 'castcouch_home.py'), '--supervise', '--port', str(port),
                              '--tv', '127.0.0.1:1', '--log', log, '--check', '0.5'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            def pid(deadline):
                end = time.time() + deadline
                while time.time() < end:
                    try:
                        return call(port, '/ping', timeout=1)[1]['pid']
                    except Exception:
                        time.sleep(0.2)
                self.fail('service did not answer')
            first = pid(10)
            os.kill(first, 9)
            second = pid(15)
            self.assertNotEqual(first, second)
            # A replacement supervisor watches the running service instead of starting another.
            p.kill()
            p.wait()
            p = subprocess.Popen([sys.executable, os.path.join(HERE, '..', 'home', 'castcouch_home.py'), '--supervise', '--port', str(port),
                                  '--tv', '127.0.0.1:1', '--log', log, '--check', '0.5'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(2)
            self.assertEqual(pid(2), second)
            os.kill(second, 9)  # now it must bring the service back itself
            third = pid(15)
            self.assertNotIn(third, (first, second))
            # A second supervisor must not start another copy.
            q = subprocess.run([sys.executable, os.path.join(HERE, '..', 'home', 'castcouch_home.py'), '--supervise', '--port', str(port), '--log', log],
                               timeout=10, capture_output=True)
            self.assertEqual(q.returncode, 0)
            self.assertEqual(pid(2), third)
        finally:
            p.kill()
            p.wait()
            try:
                pid_now = call(port, '/ping', timeout=1)[1]['pid']
                os.kill(pid_now, 9)
            except Exception:
                pass
            if os.path.exists(log):
                os.remove(log)


if __name__ == '__main__':
    unittest.main()
