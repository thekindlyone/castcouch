"""Tests for the home service. Run: python3 -m unittest discover -s test -p 'test_home.py' (needs openssl)."""
import json
import os
import socket
import subprocess
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'home'))
sys.path.insert(0, HERE)
import castcouch_home as ch  # noqa: E402
from fake_tv import FakeTV  # noqa: E402

PNG = b'\x89PNG\r\n\x1a\n' + b'\0' * 56  # 64-byte disguise header, as Polaris sends
TS = (b'\x47' + b'\x11' * 187) * 20


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
        pl, segs = ch.rewrite('#EXTM3U\n#EXTINF:4,\nseg.png?a=1\n#EXTINF:4,\n/abs/y.ts\n#EXT-X-ENDLIST', 'https://h/p/m.m3u8?t=2')
        self.assertEqual(pl, '#EXTM3U\n#EXTINF:4,\n0.ts\n#EXTINF:4,\n1.ts\n#EXT-X-ENDLIST\n')
        self.assertEqual(segs, ['https://h/p/seg.png?a=1', 'https://h/abs/y.ts'])
        ch.rewrite('#EXTM3U\n#EXT-X-KEY:METHOD=NONE\n#EXTINF:4,\na.ts\n', 'http://a/')
        for bad in ('#EXT-X-MAP:URI="i"\na.m4s', '#EXT-X-KEY:METHOD=AES-128,URI="k"\na.ts', '#EXT-X-BYTERANGE:1@0\na.ts', '#EXTM3U\n'):
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
        cases = {'/refused.m3u8': 'stream-refused', '/fmp4.m3u8': 'stream-unsupported', '/enc.m3u8': 'stream-unsupported',
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
