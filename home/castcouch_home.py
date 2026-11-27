#!/usr/bin/env python3
"""castcouch home service: plays a video from the phone on the TV, through this PC.

The phone (userscript) sends the page's own HLS stream URL. This service checks it, relays it on the home
network as a plain MPEG-TS playlist, and tells the TV's default Cast receiver to play it.

Upstream requests carry this program's own name and nothing else: no browser identity, Referer or cookies.

Standard library only; Python 3.9+. Run: python castcouch_home.py [--port 8899] [--tv IP[:PORT]]
Always-on mode (restarts the service if it exits or stops answering): python castcouch_home.py --supervise
"""
import argparse
import html
import ipaddress
import json
import logging
import logging.handlers
import os
import re
import secrets
import select
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = '1.0.1'
UA = 'castcouch-relay/' + VERSION  # an honest name of our own; some CDNs refuse Python's default one
RECEIVER = 'CC1AD845'  # Google's Default Media Receiver
NS = 'urn:x-cast:com.google.cast.'
log = logging.getLogger('castcouch')


class Failure(Exception):
    """An error the phone can explain. code is stable; message is for people."""

    def __init__(self, code, message, status=502):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


# ---------------------------------------------------------------- HLS

def fetch(url, timeout=20):
    req = urllib.request.Request(url, headers={'User-Agent': UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        raise Failure('stream-refused', 'The video server (%s) refused the PC (HTTP %d).' % (host(url), e.code))
    except (urllib.error.URLError, OSError) as e:
        raise Failure('stream-unreachable', 'The PC could not reach the video server %s (%s).' % (host(url), getattr(e, 'reason', e)))


def host(url):
    return urllib.parse.urlparse(url).hostname or '?'


def uris(text):
    return [l.strip() for l in text.splitlines() if l.strip() and not l.startswith('#')]


def choose_variant(text, base):
    """URL of the largest variant up to 1080p, or None if this is not a master playlist."""
    if '#EXT-X-STREAM-INF' not in text:
        return None
    lines, best = text.splitlines(), None
    for i, line in enumerate(lines):
        if not line.startswith('#EXT-X-STREAM-INF'):
            continue
        m = re.search(r'RESOLUTION=\d+x(\d+)', line)
        h = int(m.group(1)) if m else 0
        nxt = next((x.strip() for x in lines[i + 1:] if x.strip() and not x.startswith('#')), None)
        if nxt and h <= 1080 and (best is None or h > best[0]):
            best = (h, nxt)
    if best is None:
        raise Failure('stream-unsupported', 'The stream has no version up to 1080p.')
    return urllib.parse.urljoin(base, best[1])


def rewrite(text, base):
    """(playlist with segments renamed 0.ts, 1.ts, ..., [absolute segment URLs])."""
    for tag in ('#EXT-X-MAP', '#EXT-X-BYTERANGE'):
        if tag in text:
            raise Failure('stream-unsupported', 'This stream format (%s) is not supported; try another server.' % tag[7:])
    for m in re.finditer(r'#EXT-X-KEY:([^\n]*)', text):
        if 'METHOD=NONE' not in m.group(1):
            raise Failure('stream-unsupported', 'This stream is encrypted; try another server.')
    segs, out = [], []
    for line in text.splitlines():
        if line.strip() and not line.startswith('#'):
            out.append('%d.ts' % len(segs))
            segs.append(urllib.parse.urljoin(base, line.strip()))
        else:
            out.append(line)
    if not segs:
        raise Failure('stream-unsupported', 'The playlist lists no video.')
    return '\n'.join(out) + '\n', segs


def ts_offset(b):
    """Where MPEG-TS data starts, skipping a disguise header (Polaris adds a 64-byte PNG one); None if not TS."""
    for i in range(min(len(b) - 376, 4096)):
        if b[i] == 0x47 and b[i + 188] == 0x47 and b[i + 376] == 0x47:
            return i
    return None


def prepare(stream):
    """Fetch and check a stream; returns (playlist, segment URLs)."""
    u = urllib.parse.urlparse(stream or '')
    if u.scheme not in ('http', 'https') or not u.netloc:
        raise Failure('bad-request', 'No usable stream address was sent.', 400)
    text = fetch(stream).decode('utf-8', 'replace')
    if not text.lstrip().startswith('#EXTM3U'):
        raise Failure('stream-unsupported', 'That address is not an HLS playlist.')
    variant = choose_variant(text, stream)
    if variant:
        stream, text = variant, fetch(variant).decode('utf-8', 'replace')
    playlist, segs = rewrite(text, stream)
    if ts_offset(fetch(segs[0], timeout=30)) is None:
        raise Failure('stream-unsupported', 'This server sends video the TV relay cannot pass on (not MPEG-TS); try Polaris.')
    return playlist, segs


# ---------------------------------------------------------------- Cast (CASTV2)

def _varint(n):
    out = bytearray()
    while True:
        out.append((n & 127) | (128 if n > 127 else 0))
        n >>= 7
        if not n:
            return bytes(out)


def _field(num, s):
    b = s.encode()
    return bytes([num << 3 | 2]) + _varint(len(b)) + b


def encode(src, dst, ns, payload):
    msg = b'\x08\x00' + _field(2, src) + _field(3, dst) + _field(4, NS + ns) + b'\x28\x00' + _field(6, json.dumps(payload))
    return struct.pack('>I', len(msg)) + msg


def decode(msg):
    fields, i = {}, 0

    def varint():
        nonlocal i
        r = s = 0
        while True:
            c = msg[i]
            i += 1
            r |= (c & 127) << s
            s += 7
            if not c & 128:
                return r
    while i < len(msg):
        tag = varint()
        if tag & 7 == 2:
            n = varint()
            fields[tag >> 3] = msg[i:i + n].decode('utf-8', 'replace')
            i += n
        elif tag & 7 == 0:
            fields[tag >> 3] = varint()
        else:
            break
    ns = fields.get(4, '')
    return {'src': fields.get(2), 'dst': fields.get(3), 'ns': ns[len(NS):] if ns.startswith(NS) else ns,
            'data': json.loads(fields.get(6) or '{}')}


class Cast:
    """One connection to the TV, kept open while our media plays (the receiver may quit when it closes)."""

    def __init__(self, host, port=8009):
        self.host, self.port = host, port
        self.sock, self.alive = None, False
        self.cond = threading.Condition()
        self.replies, self.receiver, self.media = {}, None, None
        self.transport, self.req = None, 0
        self.send_lock = threading.Lock()
        self.last_rx = time.time()

    def connect(self):
        ctx = ssl.create_default_context()
        ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE  # Cast devices present self-signed certificates
        try:
            raw = socket.create_connection((self.host, self.port), timeout=5)
            self.sock = ctx.wrap_socket(raw)
        except OSError as e:
            raise Failure('tv-not-found', 'The PC could not reach the TV at %s (%s). Is the TV on?' % (self.host, e))
        self.sock.settimeout(None)
        self.alive = True
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._beat, daemon=True).start()
        self.send('receiver-0', 'tp.connection', {'type': 'CONNECT'})

    def close(self):
        self.alive = False
        try:
            self.sock.close()
        except Exception:
            pass

    def send(self, dst, ns, payload):
        try:
            with self.send_lock:
                self.sock.sendall(encode('sender-0', dst, ns, payload))
        except OSError as e:
            self.alive = False
            raise Failure('tv-lost', 'Lost the connection to the TV (%s).' % e)

    def _recv(self, n):
        b = b''
        while len(b) < n:
            c = self.sock.recv(n - len(b))
            if not c:
                raise OSError('closed')
            b += c
        return b

    def _frame(self):
        """Next message, or None if none is waiting. Reads and writes share one lock: an SSL socket is not
        safe to read and write from two threads at once (it corrupts the stream)."""
        if not self.sock.pending():
            r, _, _ = select.select([self.sock], [], [], 0.5)
            if not r:
                return None
        with self.send_lock:
            try:
                # Readable can mean TLS housekeeping with no message (e.g. session tickets): don't wait for one.
                self.sock.settimeout(0)
                try:
                    head = self.sock.recv(4)
                except (ssl.SSLWantReadError, BlockingIOError):
                    return None
                if not head:
                    raise OSError('closed')
                self.sock.settimeout(10)  # a message has started; the rest follows at once
                if len(head) < 4:
                    head += self._recv(4 - len(head))
                return decode(self._recv(struct.unpack('>I', head)[0]))
            except socket.timeout:
                raise OSError('message from the TV cut short')
            finally:
                self.sock.settimeout(None)

    def _read(self):
        try:
            while self.alive:
                m = self._frame()
                if m is None:
                    if time.time() - self.last_rx > 20:  # heartbeats unanswered: the TV is gone
                        raise OSError('no answer from the TV for 20 s')
                    continue
                self.last_rx = time.time()
                d = m['data']
                if m['ns'] == 'tp.heartbeat' and d.get('type') == 'PING':
                    self.send(m['src'], 'tp.heartbeat', {'type': 'PONG'})
                    continue
                with self.cond:
                    if d.get('type') == 'RECEIVER_STATUS':
                        self.receiver = d.get('status', {})
                    if d.get('type') == 'MEDIA_STATUS':
                        self.media = (d.get('status') or [None])[0]  # empty: nothing loaded any more
                    if m['ns'] == 'tp.connection' and d.get('type') == 'CLOSE' and m['src'] == self.transport:
                        self.transport, self.media = None, None
                    if 'requestId' in d:
                        self.replies[d['requestId']] = d
                    self.cond.notify_all()
        except Exception as e:
            if self.alive:
                log.info('TV connection ended: %s', e)
        finally:
            self.alive = False
            with self.cond:
                self.cond.notify_all()

    def _beat(self):
        while self.alive:
            time.sleep(5)
            try:
                self.send('receiver-0', 'tp.heartbeat', {'type': 'PING'})
            except Failure:
                return

    def request(self, dst, ns, payload, timeout=15, accept=None):
        with self.cond:
            self.req += 1
            rid = payload['requestId'] = self.req
        self.send(dst, ns, payload)
        end = time.time() + timeout
        with self.cond:
            while True:
                r = self.replies.pop(rid, None)
                if r is not None and (accept is None or accept(r)):
                    return r
                if not self.alive:
                    raise Failure('tv-lost', 'The TV closed the connection.')
                if time.time() > end:
                    raise Failure('tv-timeout', 'The TV did not answer in time.')
                self.cond.wait(0.5)

    def app(self):
        apps = (self.receiver or {}).get('applications') or []
        return apps[0] if apps else None

    def launch(self):
        ours = lambda: (self.app() or {}).get('appId') == RECEIVER and (self.app() or {}).get('transportId')
        if not ours():
            # Replies vary (LAUNCH_STATUS, then RECEIVER_STATUS updates); wait for our player to be listed.
            with self.cond:
                self.req += 1
                rid = self.req
            self.send('receiver-0', 'receiver', {'type': 'LAUNCH', 'appId': RECEIVER, 'requestId': rid})
            end = time.time() + 25
            with self.cond:
                while not ours():
                    err = self.replies.get(rid)
                    if err and err.get('type') in ('LAUNCH_ERROR', 'INVALID_REQUEST'):
                        raise Failure('tv-launch-failed', 'The TV refused to start its video player (%s).' % err.get('reason', err['type']))
                    if time.time() > end or not self.alive:
                        raise Failure('tv-launch-failed', 'The TV did not start its video player.')
                    self.cond.wait(0.5)
                self.replies.pop(rid, None)
        tid = self.app()['transportId']
        if tid != self.transport:
            self.transport = tid
            self.send(tid, 'tp.connection', {'type': 'CONNECT'})

    def load(self, url, title, position):
        r = self.request(self.transport, 'media', {
            'type': 'LOAD', 'autoplay': True, 'currentTime': max(0.0, float(position or 0)),
            'media': {'contentId': url, 'contentType': 'application/x-mpegurl', 'streamType': 'BUFFERED',
                      'hlsSegmentFormat': 'ts', 'hlsVideoSegmentFormat': 'mpeg2_ts',
                      'metadata': {'metadataType': 0, 'title': title or 'castcouch'}}}, timeout=45)
        if r.get('type') != 'MEDIA_STATUS':
            raise Failure('tv-load-failed', 'The TV could not play this stream (%s).' % (r.get('detailedErrorCode') or r.get('type')))
        return self.media

    def control(self, action, seconds=0):
        if not self.transport or not self.media:
            raise Failure('not-playing', 'Nothing from castcouch is playing on the TV.', 409)
        sid = self.media['mediaSessionId']
        if action == 'seek':
            pos = max(0.0, float(self.media.get('currentTime') or 0) + float(seconds))
            payload = {'type': 'SEEK', 'mediaSessionId': sid, 'currentTime': pos}
        elif action in ('pause', 'play', 'stop'):
            payload = {'type': action.upper(), 'mediaSessionId': sid}
        else:
            raise Failure('bad-request', 'Unknown action.', 400)
        self.request(self.transport, 'media', payload, timeout=10)
        return self.media

    def refresh(self):
        if self.transport:
            self.request(self.transport, 'media', {'type': 'GET_STATUS'}, timeout=8)
        return self.media


# ---------------------------------------------------------------- TV discovery (mDNS)

def _local_ipv4():
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    return [ip for ip in ips if not ip.startswith(('127.', '169.254.'))] or ['0.0.0.0']


def discover(timeout=3.0):
    """[(ip, friendly name)] of Cast devices answering on the local network."""
    q = b'\x00\x00\x00\x00\x00\x01\x00\x00\x00\x00\x00\x00'
    for label in ('_googlecast', '_tcp', 'local'):
        q += bytes([len(label)]) + label.encode()
    q += b'\x00\x00\x0c\x80\x01'
    found = {}
    for ip in _local_ipv4():
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.bind((ip, 0))
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(ip) if ip != '0.0.0.0' else b'\0' * 4)
            s.settimeout(0.3)
            s.sendto(q, ('224.0.0.251', 5353))
            end = time.time() + timeout
            while time.time() < end:
                try:
                    data, addr = s.recvfrom(9000)
                except socket.timeout:
                    continue
                m = re.search(rb'fn=([^\x00-\x1f]*)', data)
                found.setdefault(addr[0], m.group(1).decode('utf-8', 'replace') if m else addr[0])
        except OSError as e:
            log.debug('discovery on %s: %s', ip, e)
        finally:
            s.close()
    return sorted(found.items())


# ---------------------------------------------------------------- service

class Home:
    def __init__(self, port, tv=None, tv_name=None):
        self.port, self.tv_fixed, self.tv_name = port, tv, tv_name
        self.tv = None  # (host, port, name)
        self.cast = None
        self.session = None  # {'id', 'playlist', 'segs', 'title', 'page'}
        self.lock = threading.Lock()

    def find_tv(self, fresh=False):
        if self.tv_fixed:
            host, _, port = self.tv_fixed.partition(':')
            self.tv = (host, int(port or 8009), self.tv_name or host)
        elif fresh or not self.tv:
            devices = discover()
            log.info('Cast devices found: %s', devices)
            pick = [d for d in devices if not self.tv_name or d[1] == self.tv_name]
            if not pick:
                raise Failure('tv-not-found', 'The PC found no TV on the network%s. Is the TV on?' % (' named ' + self.tv_name if self.tv_name else ''))
            self.tv = (pick[0][0], 8009, pick[0][1])
        return self.tv

    def connection(self):
        if self.cast and self.cast.alive:
            try:  # the TV may have gone away without closing the connection
                self.cast.request('receiver-0', 'receiver', {'type': 'GET_STATUS'}, timeout=5)
                return self.cast
            except Failure:
                log.info('TV connection is stale; reconnecting')
                self.cast.close()
        for fresh in (False, True):
            host, port, _ = self.find_tv(fresh)
            try:
                self.cast = Cast(host, port)
                self.cast.connect()
                self.cast.request('receiver-0', 'receiver', {'type': 'GET_STATUS'}, timeout=10)
                return self.cast
            except Failure:
                if fresh or self.tv_fixed:
                    raise
                log.info('TV not at %s any more; looking again', host)
        raise Failure('tv-not-found', 'The PC could not find the TV.')

    def lan_address(self, tv_host):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect((tv_host, 8009))
            return s.getsockname()[0]
        finally:
            s.close()

    def play(self, body):
        with self.lock:
            # Newest first: the page's current server. Older ones are fallbacks if that one refuses.
            candidates = [u for u in (body.get('streams') or [body.get('stream')]) if isinstance(u, str)][:6] or [None]
            first = None
            for i, stream in enumerate(candidates):
                try:
                    playlist, segs = prepare(stream)
                    log.info('stream %d of %d accepted: %s', i + 1, len(candidates), host(stream or ''))
                    break
                except Failure as f:
                    log.info('stream %d of %d (%s): %s', i + 1, len(candidates), host(stream or ''), f.code)
                    first = first or f
            else:
                raise first
            cast = self.connection()
            sid = secrets.token_hex(6)
            self.session = {'id': sid, 'playlist': playlist, 'segs': segs, 'title': str(body.get('title') or '')[:200],
                            'page': str(body.get('page') or '')[:500]}
            url = 'http://%s:%d/s/%s/p.m3u8' % (self.lan_address(cast.host), self.port, sid)
            cast.launch()
            media = cast.load(url, self.session['title'], body.get('position') or 0)
            log.info('playing %r (%d segments) on %s', self.session['title'], len(segs), self.tv[2])
            return {'ok': True, 'tv': self.tv[2], 'title': self.session['title'], 'state': (media or {}).get('playerState')}

    def control(self, body):
        with self.lock:
            if not (self.cast and self.cast.alive):
                raise Failure('not-playing', 'Nothing from castcouch is playing on the TV.', 409)
            media = self.cast.control(body.get('action'), body.get('seconds') or 0)
            return self.describe(media)

    def status(self):
        media = None
        if self.cast and self.cast.alive:
            try:
                media = self.cast.refresh()
            except Failure:
                media = None
        return self.describe(media)

    def describe(self, media):
        on = bool(media) and media.get('playerState') not in (None, 'IDLE')
        return {'ok': True, 'playing': on, 'state': (media or {}).get('playerState', 'IDLE'),
                'position': round(float((media or {}).get('currentTime') or 0)),
                'title': (self.session or {}).get('title') if on else None, 'tv': self.tv[2] if self.tv else None}

    def segment(self, sid, n):
        s = self.session
        if not s or s['id'] != sid or not 0 <= n < len(s['segs']):
            raise Failure('gone', 'Unknown segment.', 404)
        b = fetch(s['segs'][n], timeout=30)
        off = ts_offset(b)
        return b[off:] if off else b


REMOTE = '''<!doctype html><meta name=viewport content="width=device-width,initial-scale=1"><title>castcouch remote</title>
<style>body{font:18px sans-serif;margin:0;padding:16px;background:#111;color:#eee}button{display:block;width:100%;margin:10px 0;
padding:18px;font:18px sans-serif;border:0;border-radius:10px;background:#1a73e8;color:#fff}.row{display:flex;gap:10px}
.row button{flex:1}#s{min-height:3em}</style><h2>castcouch remote</h2><div id=s>…</div>
<div class=row><button onclick="c('pause')">Pause</button><button onclick="c('play')">Play</button></div>
<div class=row><button onclick="c('seek',-30)">−30 s</button><button onclick="c('seek',30)">+30 s</button></div>
<button onclick="c('stop')" style="background:#c5221f">Stop</button><script>
const t=s=>{const h=Math.floor(s/3600),m=Math.floor(s/60)%60;return(h?h+':':'')+String(m).padStart(h?2:1,'0')+':'+String(s%60).padStart(2,'0')};
const show=r=>{document.getElementById('s').textContent=r.ok===false?r.message:(r.playing?(r.title||'Video')+' — '+r.state.toLowerCase()+' at '+t(r.position)+' on '+r.tv:'Nothing playing from castcouch.')};
async function c(action,seconds){show(await (await fetch('/control',{method:'POST',body:JSON.stringify({action,seconds})})).json())}
async function poll(){try{show(await (await fetch('/status')).json())}catch(e){document.getElementById('s').textContent='Service not answering.'}}
poll();setInterval(poll,3000)</script>'''


def allowed(addr):
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback


def make_handler(home):
    class Handler(BaseHTTPRequestHandler):
        server_version = 'castcouch/' + VERSION
        protocol_version = 'HTTP/1.1'

        def log_message(self, fmt, *args):
            log.debug('%s %s', self.client_address[0], fmt % args)

        def reply(self, status, body, ctype='application/json'):
            if isinstance(body, dict):
                body = json.dumps(body)
            if isinstance(body, str):
                body = body.encode()
            self.send_response(status)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Access-Control-Allow-Private-Network', 'true')
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            if self.command != 'HEAD':
                self.wfile.write(body)

        def guard(self, fn):
            if not allowed(self.client_address[0]):
                return self.reply(403, {'ok': False, 'code': 'forbidden', 'message': 'Home network only.'})
            try:
                fn()
            except Failure as f:
                log.info('%s %s -> %s: %s', self.command, self.path[:40], f.code, f.message)
                self.reply(f.status, {'ok': False, 'code': f.code, 'message': f.message})
            except ConnectionError:  # the caller gave up or went away
                pass
            except Exception as e:
                log.exception('unexpected error')
                self.reply(500, {'ok': False, 'code': 'internal', 'message': 'The home service hit an error: %s' % e})

        def body(self):
            n = int(self.headers.get('Content-Length') or 0)
            if n > 65536:
                raise Failure('bad-request', 'Request too large.', 413)
            try:
                return json.loads(self.rfile.read(n) or b'{}')
            except ValueError:
                raise Failure('bad-request', 'Request was not JSON.', 400)

        def do_OPTIONS(self):
            self.send_response(204)
            for k, v in (('Access-Control-Allow-Origin', '*'), ('Access-Control-Allow-Headers', '*'),
                         ('Access-Control-Allow-Methods', 'GET, POST, OPTIONS'), ('Access-Control-Allow-Private-Network', 'true'),
                         ('Content-Length', '0')):
                self.send_header(k, v)
            self.end_headers()

        def do_GET(self):
            self.guard(self.get)

        do_HEAD = do_GET

        def do_POST(self):
            self.guard(self.post)

        def get(self):
            path = urllib.parse.urlparse(self.path).path
            if path == '/':
                return self.reply(200, REMOTE, 'text/html; charset=utf-8')
            if path == '/ping':
                return self.reply(200, {'ok': True, 'service': 'castcouch', 'version': VERSION, 'pid': os.getpid(),
                                        'tv': home.tv[2] if home.tv else None})
            if path == '/status':
                return self.reply(200, home.status())
            m = re.fullmatch(r'/s/([0-9a-f]+)/(p\.m3u8|(\d+)\.ts)', path)
            if not m:
                return self.reply(404, {'ok': False, 'code': 'not-found', 'message': 'Not found.'})
            s = home.session
            if not s or s['id'] != m.group(1):
                return self.reply(404, {'ok': False, 'code': 'gone', 'message': 'That video is no longer being relayed.'})
            if m.group(2) == 'p.m3u8':
                return self.reply(200, s['playlist'], 'application/vnd.apple.mpegurl')
            self.reply(200, home.segment(m.group(1), int(m.group(3))), 'video/mp2t')

        def post(self):
            path = urllib.parse.urlparse(self.path).path
            if path == '/play':
                return self.reply(200, home.play(self.body()))
            if path == '/control':
                return self.reply(200, home.control(self.body()))
            self.reply(404, {'ok': False, 'code': 'not-found', 'message': 'Not found.'})
    return Handler


def serve(args):
    home = Home(args.port, args.tv, args.tv_name)
    try:
        httpd = ThreadingHTTPServer((args.bind, args.port), make_handler(home))
    except OSError as e:
        log.error('cannot listen on port %d: %s', args.port, e)
        return 3
    httpd.daemon_threads = True
    log.info('castcouch home %s listening on %s:%d (pid %d)', VERSION, args.bind, args.port, os.getpid())
    def first_look():  # in the background: finding the TV takes seconds and must not delay answering
        try:
            home.find_tv()
            log.info('TV: %s', home.tv)
        except Failure as f:
            log.info('no TV yet: %s', f.message)
    threading.Thread(target=first_look, daemon=True).start()
    httpd.serve_forever()
    return 0


# ---------------------------------------------------------------- supervisor

def ping(port):
    """The answering service's pid, or None."""
    try:
        with urllib.request.urlopen('http://127.0.0.1:%d/ping' % port, timeout=5) as r:
            d = json.loads(r.read())
            return d.get('pid') if d.get('ok') is True else None
    except Exception:
        return None


def listening(port):
    try:
        socket.create_connection(('127.0.0.1', port), timeout=2).close()
        return True
    except OSError:
        return False


def kill(pid):
    try:
        os.kill(pid, 9 if os.name != 'nt' else 1)  # Windows: any signal number terminates
    except OSError:
        pass


def supervise(args):
    """Keeps one service answering: restarts it when it exits or stops answering for 3 minutes. A service
    left running by an earlier supervisor is watched, not duplicated."""
    try:  # one supervisor at a time
        lock = socket.socket()
        lock.bind(('127.0.0.1', args.port - 1))
    except OSError:
        log.info('another supervisor is already running')
        return 0
    exe = sys.executable
    if os.name == 'nt' and os.path.basename(exe).lower() == 'pythonw.exe':
        # python.exe is the program the firewall allows; started without a console window.
        exe = os.path.join(os.path.dirname(exe), 'python.exe')
    cmd = [exe, os.path.abspath(__file__), '--port', str(args.port), '--bind', args.bind, '--log', args.log]
    if args.tv:
        cmd += ['--tv', args.tv]
    if args.tv_name:
        cmd += ['--tv-name', args.tv_name]
    flags = 0x08000000 if os.name == 'nt' else 0  # CREATE_NO_WINDOW
    limit = max(1, int(180 / args.check))  # checks without an answer before a restart
    delay = 2
    while True:
        orphan = ping(args.port)
        if orphan:  # someone else's child is serving: watch it
            log.info('supervisor: watching running service pid %d', orphan)
            misses = 0
            while misses < limit:
                time.sleep(args.check)
                if ping(args.port):
                    misses = 0
                elif not listening(args.port):
                    break  # gone, not hung
                else:
                    misses += 1
            if misses >= limit:
                log.warning('supervisor: service pid %d not answering, ending it', orphan)
                kill(orphan)
                time.sleep(2)
            else:
                log.warning('supervisor: service pid %d has gone', orphan)
            continue
        started = time.time()
        child = subprocess.Popen(cmd, creationflags=flags, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        log.info('supervisor: started service pid %d', child.pid)
        misses = 0
        while child.poll() is None:
            time.sleep(args.check)
            if time.time() - started < 20:
                continue
            misses = 0 if ping(args.port) else misses + 1
            if misses >= limit:
                log.warning('supervisor: service not answering, restarting it')
                child.kill()
                child.wait()
        log.warning('supervisor: service exited with code %s', child.returncode)
        delay = 2 if time.time() - started > 60 else min(delay * 2, 60)
        time.sleep(delay)


def default_log():
    base = os.environ.get('LOCALAPPDATA') or os.path.expanduser('~/.local/state')
    return os.path.join(base, 'castcouch', 'home.log')


def main(argv=None):
    p = argparse.ArgumentParser(description='castcouch home service')
    p.add_argument('--port', type=int, default=8899)
    p.add_argument('--bind', default='0.0.0.0')
    p.add_argument('--tv', help='TV address as IP[:PORT]; default: find it on the network')
    p.add_argument('--tv-name', help='use the Cast device with this name')
    p.add_argument('--log', default=default_log())
    p.add_argument('--supervise', action='store_true', help='keep the service running')
    p.add_argument('--check', type=float, default=30, help=argparse.SUPPRESS)
    args = p.parse_args(argv)
    os.makedirs(os.path.dirname(args.log), exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(args.log, maxBytes=1_000_000, backupCount=3, encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s %(process)d %(levelname)s %(message)s'))
    log.addHandler(handler)
    if sys.stderr:
        log.addHandler(logging.StreamHandler())
    log.setLevel(logging.INFO)
    return supervise(args) if args.supervise else serve(args)


if __name__ == '__main__':
    sys.exit(main())
