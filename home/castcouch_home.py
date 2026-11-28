#!/usr/bin/env python3
"""castcouch home service: plays a video from the phone on the TV, through this PC.

The phone (userscript) sends the page's own HLS stream URL. This service checks it, relays it on the home
network as a plain MPEG-TS playlist, and tells the TV's default Cast receiver to play it.

Upstream requests carry this program's own name and nothing else: no browser identity, Referer or cookies.

Standard library only; Python 3.9+. Run: python castcouch_home.py [--port 8899] [--tv IP[:PORT]]
Always-on mode (restarts the service if it exits or stops answering): python castcouch_home.py --supervise
"""
import argparse
import collections
import html
import ipaddress
import hashlib
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

VERSION = '1.4.1'
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
    """(playlist with pieces renamed 0.ts, 1.ts, ... (or init.mp4 + 0.m4s, ... for fMP4), [absolute piece URLs],
    absolute init URL or None)."""
    if '#EXT-X-BYTERANGE' in text:
        raise Failure('stream-unsupported', 'This stream format (BYTERANGE) is not supported; try another server.')
    for m in re.finditer(r'#EXT-X-KEY:([^\n]*)', text):
        if 'METHOD=NONE' not in m.group(1):
            raise Failure('stream-unsupported', 'This stream is encrypted; try another server.')
    maps = re.findall(r'#EXT-X-MAP:[^\n]*URI="([^"]+)"', text)
    if len(set(maps)) > 1 or ('#EXT-X-MAP' in text and not maps) or (maps and 'BYTERANGE' in text):
        raise Failure('stream-unsupported', 'This stream format (several MAPs) is not supported; try another server.')
    init = urllib.parse.urljoin(base, maps[0]) if maps else None
    ext = 'm4s' if init else 'ts'
    segs, out = [], []
    for line in text.splitlines():
        if line.strip() and not line.startswith('#'):
            out.append('%d.%s' % (len(segs), ext))
            segs.append(urllib.parse.urljoin(base, line.strip()))
        elif line.startswith('#EXT-X-MAP'):
            out.append('#EXT-X-MAP:URI="init.mp4"')
        else:
            out.append(line)
    if not segs:
        raise Failure('stream-unsupported', 'The playlist lists no video.')
    return '\n'.join(out) + '\n', segs, init


def is_fmp4_piece(b):
    """A fragmented-MP4 media piece: boxes such as styp/sidx before a moof, near the start."""
    i = 0
    while i + 8 <= min(len(b), 65536):
        size, kind = struct.unpack('>I', b[i:i + 4])[0], b[i + 4:i + 8]
        if kind == b'moof':
            return True
        if kind not in (b'styp', b'sidx', b'prft', b'emsg', b'free') or size < 8:
            return False
        i += size
    return False


def ts_offset(b):
    """Where MPEG-TS data starts, skipping a disguise header (Polaris adds a 64-byte PNG one); None if not TS."""
    for i in range(min(len(b) - 376, 4096)):
        if b[i] == 0x47 and b[i + 188] == 0x47 and b[i + 376] == 0x47:
            return i
    return None


def to_vtt(text):
    """SRT (or WebVTT) text -> WebVTT the TV can show. Drops styling the TV would print literally."""
    text = (text or '').lstrip('\ufeff').replace('\r\n', '\n').replace('\r', '\n')
    if not text.lstrip().startswith('WEBVTT'):
        text = re.sub(r'(\d{1,2}:\d{2}:\d{2}),(\d{3})', r'\1.\2', text)  # 00:01:02,345 -> 00:01:02.345
        text = 'WEBVTT\n\n' + text.strip() + '\n'
    text = re.sub(r'</?font[^>]*>', '', text, flags=re.I)  # SRT colours
    text = re.sub(r'\{\\[^}]*\}', '', text)  # ASS overrides such as {\an8}
    if '-->' not in text:
        raise Failure('bad-subtitles', 'The subtitle file has no timed lines.', 400)
    return text


def prepare(stream):
    """Fetch and check a stream; returns (playlist, piece URLs, 'ts' or 'fmp4', init piece URL or None)."""
    u = urllib.parse.urlparse(stream or '')
    if u.scheme not in ('http', 'https') or not u.netloc:
        raise Failure('bad-request', 'No usable stream address was sent.', 400)
    text = fetch(stream).decode('utf-8', 'replace')
    if not text.lstrip().startswith('#EXTM3U'):
        raise Failure('stream-unsupported', 'That address is not an HLS playlist.')
    variant = choose_variant(text, stream)
    if variant:
        stream, text = variant, fetch(variant).decode('utf-8', 'replace')
    playlist, segs, init = rewrite(text, stream)
    if init:  # fMP4 (Nebula): the TV's player takes it as is, given the init piece
        head = fetch(init, timeout=30)
        if b'ftyp' not in head[:64] or b'moov' not in head:
            raise Failure('stream-unsupported', 'The video from this server has no usable start piece; try another server.')
        if not is_fmp4_piece(fetch(segs[0], timeout=30)):
            raise Failure('stream-unsupported', 'This server sends video the TV relay cannot pass on; try another server.')
        return playlist, segs, 'fmp4', init
    if ts_offset(fetch(segs[0], timeout=30)) is None:
        raise Failure('stream-unsupported', 'This server sends video the TV relay cannot pass on (not MPEG-TS); try Polaris.')
    return playlist, segs, 'ts', None


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
        self.has_subtitles = False
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

    def load(self, url, title, position, subtitles=None, content_type='application/x-mpegurl', hls='ts'):
        """subtitles: (vtt url, name) or None. Shown from the start when given."""
        media = {'contentId': url, 'contentType': content_type, 'streamType': 'BUFFERED',
                 'metadata': {'metadataType': 0, 'title': title or 'castcouch'}}
        if content_type == 'application/x-mpegurl':
            media.update(hlsSegmentFormat='fmp4', hlsVideoSegmentFormat='fmp4') if hls == 'fmp4' else \
                media.update(hlsSegmentFormat='ts', hlsVideoSegmentFormat='mpeg2_ts')
        payload = {'type': 'LOAD', 'autoplay': True, 'currentTime': max(0.0, float(position or 0)), 'media': media}
        if subtitles:
            media['tracks'] = [{'trackId': 1, 'type': 'TEXT', 'subtype': 'SUBTITLES', 'trackContentId': subtitles[0],
                                'trackContentType': 'text/vtt', 'name': subtitles[1] or 'Subtitles', 'language': 'und'}]
            media['textTrackStyle'] = {'backgroundColor': '#00000099', 'foregroundColor': '#FFFFFFFF', 'fontScale': 1.0}
            payload['activeTrackIds'] = [1]
        r = self.request(self.transport, 'media', payload, timeout=45)
        if r.get('type') != 'MEDIA_STATUS':
            raise Failure('tv-load-failed', 'The TV could not play this stream (%s).' % (r.get('detailedErrorCode') or r.get('type')))
        self.has_subtitles = bool(subtitles)
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
        elif action in ('subtitles-on', 'subtitles-off'):
            if not self.has_subtitles:
                raise Failure('no-subtitles', 'No subtitles were sent with this video.', 409)
            payload = {'type': 'EDIT_TRACKS_INFO', 'mediaSessionId': sid, 'activeTrackIds': [1] if action == 'subtitles-on' else []}
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
    def __init__(self, port, tv=None, tv_name=None, state=None, library=None, opensubtitles=None):
        self.port, self.tv_fixed, self.tv_name = port, tv, tv_name
        self.library, self.opensubtitles = library or Library([]), opensubtitles
        self.tv = None  # (host, port, name)
        self.cast = None
        self.session = None  # {'id', 'playlist', 'segs', 'title', 'page', 'vtt', ...}
        self.base = None
        self.lock = threading.Lock()
        self.cache = collections.OrderedDict()  # (session id, n) -> bytes: pieces fetched ahead
        self.inflight = {}  # (session id, n) -> Event while a piece downloads
        self.cache_lock = threading.Lock()
        self.timing = []  # (n, seconds from the internet, seconds to the TV or None) since the last summary
        # What is playing is kept on disk, so a restarted service (update, crash) keeps feeding the TV.
        self.state = state
        self.load_state()

    def load_state(self):
        try:
            if self.state and time.time() - os.path.getmtime(self.state) < 12 * 3600:
                with open(self.state, encoding='utf-8') as f:
                    d = json.load(f)
                self.session, self.base = d['session'], d['base']
                log.info('resumed relaying %r', self.session.get('title'))
        except (OSError, ValueError, KeyError) as e:
            log.info('no saved session: %s', e)

    def save_state(self):
        if not self.state:
            return
        tmp = self.state + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump({'session': self.session, 'base': self.base}, f)
        os.replace(tmp, self.state)

    def attach(self):
        """A connection to the TV that knows our media, taking over a player started before a restart."""
        cast = self.connection()
        if not (cast.transport and cast.media):
            app = cast.app()
            if app and app.get('appId') == RECEIVER and app.get('transportId'):
                cast.transport = app['transportId']
                cast.send(cast.transport, 'tp.connection', {'type': 'CONNECT'})
                cast.refresh()
        return cast

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
                    playlist, segs, fmt, init = prepare(stream)
                    log.info('stream %d of %d accepted: %s', i + 1, len(candidates), host(stream or ''))
                    break
                except Failure as f:
                    log.info('stream %d of %d (%s): %s', i + 1, len(candidates), host(stream or ''), f.code)
                    first = first or f
            else:
                diag = body.get('diagnostics')
                if diag:
                    log.info('no usable stream; phone reported: %s', json.dumps(diag)[:3000])
                raise first
            sub = body.get('subtitles') or None
            vtt = to_vtt(sub.get('text')) if isinstance(sub, dict) and sub.get('text') else None
            cast = self.connection()
            sid = secrets.token_hex(6)
            self.session = {'id': sid, 'playlist': playlist, 'segs': segs, 'fmt': fmt, 'init': init,
                            'title': str(body.get('title') or '')[:200],
                            'page': str(body.get('page') or '')[:500], 'vtt': vtt, 'vtt_n': 0,
                            'vtt_name': str((sub or {}).get('label') or '')[:200] if vtt else None}
            self.base = 'http://%s:%d/s/%s/' % (self.lan_address(cast.host), self.port, sid)
            self.session.update(media_url=self.base + 'p.m3u8', ctype='application/x-mpegurl')
            cast.launch()
            media = cast.load(self.session['media_url'], self.session['title'], body.get('position') or 0, self.sub_track(), hls=fmt)
            self.save_state()
            log.info('playing %r (%d %s segments%s) on %s', self.session['title'], len(segs), fmt, ', subtitles' if vtt else '', self.tv[2])
            return dict(self.describe(media), title=self.session['title'])

    def play_local(self, body):
        """A film from the library, read by the TV straight from this PC. English subtitles: a file beside it,
        else OpenSubtitles (if a key is set up). body: {id, position, subtitles: true/false}."""
        with self.lock:
            item = self.library.get(str(body.get('id') or ''))
            vtt, name, source = None, None, None
            if body.get('subtitles', True):
                found = local_subtitles(item['path'], item['own_folder'])
                if found:
                    try:
                        vtt, name, source = to_vtt(read_text(found)), os.path.basename(found), 'beside the film'
                    except Failure:
                        log.info('unusable subtitle file %s', found)
                if not vtt and self.opensubtitles:
                    got = self.opensubtitles.english(item)
                    if got:
                        try:
                            vtt, name, source = to_vtt(got[0]), got[1], 'OpenSubtitles'
                        except Failure:
                            pass
            cast = self.connection()
            sid = secrets.token_hex(6)
            self.base = 'http://%s:%d/s/%s/' % (self.lan_address(cast.host), self.port, sid)
            self.session = {'id': sid, 'kind': 'local', 'path': item['path'], 'title': item['title'], 'page': 'local:' + item['id'],
                            'vtt': vtt, 'vtt_n': 0, 'vtt_name': name, 'segs': [], 'playlist': '',
                            'media_url': self.base + 'video' + item['ext'], 'ctype': VIDEO_TYPES[item['ext']]}
            cast.launch()
            media = cast.load(self.session['media_url'], item['title'], body.get('position') or 0, self.sub_track(), self.session['ctype'])
            self.save_state()
            log.info('playing local %r%s on %s', item['path'], ', subtitles ' + source if vtt else '', self.tv[2])
            return dict(self.describe(media), title=item['title'], subtitles_from=source)

    def sub_track(self):
        s = self.session
        # A new name per version, so the TV does not reuse an older file it cached.
        return (self.base + 'sub%d.vtt' % s['vtt_n'], s['vtt_name']) if s and s['vtt'] else None

    def subtitles(self, body):
        """Replace (or remove) the subtitles of what is playing; the TV reloads at its current position."""
        with self.lock:
            s = self.session
            if not (s and self.attach().media):
                raise Failure('not-playing', 'Nothing from castcouch is playing on the TV.', 409)
            sub = body.get('subtitles') or None
            s['vtt'] = to_vtt(sub.get('text')) if isinstance(sub, dict) and sub.get('text') else None
            s['vtt_name'] = str((sub or {}).get('label') or '')[:200] if s['vtt'] else None
            s['vtt_n'] += 1
            position = self.cast.refresh() and self.cast.media.get('currentTime') or 0
            media = self.cast.load(s['media_url'], s['title'], position, self.sub_track(), s['ctype'], hls=s.get('fmt', 'ts'))
            self.save_state()
            log.info('subtitles %s for %r at %ds', 'replaced' if s['vtt'] else 'removed', s['title'], position)
            return self.describe(media)

    def control(self, body):
        with self.lock:
            if not self.session:
                raise Failure('not-playing', 'Nothing from castcouch is playing on the TV.', 409)
            media = self.attach().control(body.get('action'), body.get('seconds') or 0)
            return self.describe(media)

    def status(self):
        media = None
        try:
            if self.cast and self.cast.alive:
                media = self.cast.refresh()
            elif self.session:  # after a restart: find out whether our player is still going
                media = self.attach().media
        except Failure:
            media = None
        return self.describe(media)

    def describe(self, media):
        on = bool(media) and media.get('playerState') not in (None, 'IDLE')
        s = self.session or {}
        active = (media or {}).get('activeTrackIds')
        return {'ok': True, 'playing': on, 'state': (media or {}).get('playerState', 'IDLE'),
                'position': round(float((media or {}).get('currentTime') or 0)),
                'title': s.get('title') if on else None, 'page': s.get('page') if on else None,
                'subtitles': (s.get('vtt_name') or 'on') if on and s.get('vtt') and (active is None or 1 in active) else None,
                'has_subtitles': bool(on and s.get('vtt')), 'local': s.get('kind') == 'local' if on else None,
                'tv': self.tv[2] if self.tv else None}

    AHEAD = 3  # pieces fetched in advance of the one the TV asks for (about 25 s of Polaris video)

    def segment(self, sid, n):
        """Piece n of the playing film, from the read-ahead cache when possible; starts fetching the next ones."""
        s = self.session
        if s and s['id'] == sid and n == -1 and s.get('init'):  # the fMP4 init piece
            return self._piece(sid, -1, s['init'])
        if not s or s['id'] != sid or not 0 <= n < len(s['segs']):
            raise Failure('gone', 'Unknown segment.', 404)
        for k in range(n + 1, min(n + 1 + self.AHEAD, len(s['segs']))):
            threading.Thread(target=self._ahead, args=(sid, k, s['segs'][k]), daemon=True).start()
        return self._piece(sid, n, s['segs'][n])

    def _ahead(self, sid, n, url):
        try:
            self._piece(sid, n, url)
        except Failure:
            pass  # fetched again when the TV asks for it

    def _piece(self, sid, n, url):
        key = (sid, n)
        with self.cache_lock:
            if key in self.cache:
                self.cache.move_to_end(key)
                return self.cache[key]
            ev = self.inflight.get(key)
            mine = ev is None
            if mine:
                ev = self.inflight[key] = threading.Event()
        if not mine:  # someone is already fetching it
            ev.wait(60)
            with self.cache_lock:
                if key in self.cache:
                    return self.cache[key]
            raise Failure('stream-unreachable', 'A piece of the video could not be fetched.')
        try:
            t = time.time()
            b = fetch(url, timeout=30)
            took = time.time() - t
            if took > 4:
                log.warning('slow internet: piece %d took %.1f s (%.1f MB)', n, took, len(b) / 1e6)
            if n >= 0 and (self.session or {}).get('fmt', 'ts') == 'ts':
                off = ts_offset(b)
                b = b[off:] if off else b
            with self.cache_lock:
                self.cache[key] = b
                while len(self.cache) > 2 * self.AHEAD + 2:
                    self.cache.popitem(last=False)
                self.timing.append([n, took, None])
            return b
        finally:
            with self.cache_lock:
                self.inflight.pop(key, None)
            ev.set()

    def delivered(self, n, seconds, size):
        """Record how long sending piece n to the TV took; log a summary every 20 pieces."""
        if seconds > 4:
            log.warning('slow Wi-Fi to the TV: piece %d took %.1f s to send (%.1f MB)', n, seconds, size / 1e6)
        with self.cache_lock:
            for t in self.timing:
                if t[0] == n and t[2] is None:
                    t[2] = seconds
                    break
            done = [t for t in self.timing if t[2] is not None]
            if len(done) < 20:
                return
            self.timing = [t for t in self.timing if t[2] is None]
        net, tv = [t[1] for t in done], [t[2] for t in done]
        log.info('pieces %d-%d: internet avg %.1f s (max %.1f), to the TV avg %.1f s (max %.1f)',
                 min(t[0] for t in done), max(t[0] for t in done), sum(net) / len(net), max(net), sum(tv) / len(tv), max(tv))


# ---------------------------------------------------------------- local library

VIDEO_TYPES = {'.mp4': 'video/mp4', '.m4v': 'video/mp4', '.mkv': 'video/x-matroska', '.webm': 'video/webm',
               '.mov': 'video/quicktime', '.avi': 'video/x-msvideo', '.ts': 'video/mp2t'}
MAYBE = {'.avi', '.mov', '.ts'}  # often old codecs the TV's player cannot decode


QUALITY = r'\b(?:480p|576p|720p|1080p|2160p|4k|uhd|imax|bluray|blu-ray|brrip|bdrip|web-?dl|webrip|web|amzn|dvdrip|hdrip|hdtv|x264|x265|h ?264|h ?265|hevc|aac|ddp?5 1|yify)\b'


def nice_title(name):
    """'Knives.Out.2019.1080p.WEBRip.x264-RARBG' -> 'Knives Out (2019)'. With two years ('Wonder.Woman.1984.2020'),
    the last one before the release details is the year; the earlier one belongs to the title."""
    name = re.sub(r'\.(mp4|m4v|mkv|webm|mov|avi|ts)$', '', name, flags=re.I)
    name = re.sub(r'[._]+', ' ', name)
    name = re.sub(r'^\s*(\[[^\]]*\]\s*)+', '', name) or name  # leading release-group tags: [AnimeRG] Boruto
    q = re.search(QUALITY, name, re.I)
    head = name[:q.start()] if q else name
    years = [m for m in re.finditer(r'[\[(]?\b(19[2-9]\d|20[0-4]\d)\b[\])]?', head) if m.start() > 0]
    if years:
        m = years[-1]
        return '%s (%s)' % (re.sub(r'[\s\[(-]+$', '', name[:m.start()]).strip(), m.group(1))
    return re.sub(r'[\s\[(-]+$', '', head).strip() or name


def quality(f):
    m = re.search(r'(?<![a-z0-9])(480p|576p|720p|1080p|2160p|4k)(?![a-z0-9])', f, re.I)
    return m.group(1).lower() if m else None


def generic_name(f):
    """'part2.mp4', 'CD1.avi', 'movie.mkv': a file name that says nothing about the film."""
    return re.fullmatch(r'(part|cd|disc|disk|pt)[ ._-]?\d+|movie|video|film|\d+', os.path.splitext(f)[0].strip(), re.I) is not None


class Library:
    """Films under the given folders. Ids are stable (from the path) so the phone never sends a path."""

    def __init__(self, roots, min_size=50 * 2**20):
        self.roots, self.min_size = [r for r in roots if r], min_size
        self.items, self.scanned, self.lock = {}, 0, threading.Lock()

    def scan(self, force=False):
        with self.lock:
            if not force and time.time() - self.scanned < 600:
                return self.items
            items = {}
            for root in self.roots:
                for d, dirs, files in os.walk(root, onerror=lambda e: None):
                    dirs[:] = [x for x in dirs if 'sample' not in x.lower()]
                    videos = [f for f in files if os.path.splitext(f)[1].lower() in VIDEO_TYPES and 'sample' not in f.lower()]
                    for f in videos:
                        path = os.path.join(d, f)
                        try:
                            size = os.path.getsize(path)
                        except OSError:
                            continue
                        if size < self.min_size:
                            continue
                        folder = os.path.basename(d) if os.path.normcase(d) != os.path.normcase(root) else ''
                        one = folder and len(videos) == 1
                        title = nice_title(folder if one or (folder and generic_name(f)) else f)
                        part = re.search(r'(?:part|cd|disc|disk|pt)[ ._-]?(\d+)', f, re.I) if generic_name(f) else None
                        if part and not one:
                            title += ' – part %s' % part.group(1)
                        iid = hashlib.sha1(os.path.normcase(path).encode('utf-8', 'replace')).hexdigest()[:12]
                        items[iid] = {'id': iid, 'title': title, 'file': f, 'path': path, 'size': size, 'root': root,
                                      'ext': os.path.splitext(f)[1].lower(), 'own_folder': bool(folder) and len(videos) <= 2}
            self.items, self.scanned = items, time.time()
            log.info('library: %d films in %s', len(items), self.roots)
            return items

    def get(self, iid):
        item = self.scan().get(iid) or self.scan(force=True).get(iid)
        if not item:
            raise Failure('not-found', 'That film is not in the library any more.', 404)
        return item

    def listing(self, force=False):
        out = [{'id': i['id'], 'title': i['title'], 'file': i['file'], 'size': i['size'], 'drive': i['root'][:2],
                'quality': quality(i['file']),
                'maybe': i['ext'] in MAYBE, 'subs': bool(local_subtitles(i['path'], i['own_folder']))}
               for i in self.scan(force).values()]
        return sorted(out, key=lambda x: x['title'].lower())


def local_subtitles(video, own_folder=True):
    """The best English-looking .srt/.vtt beside a film: in its folder, or in Subs/Subtitles folders under it.
    A film lying loose in a big folder (own_folder=False) only gets files named after it."""
    d, stem = os.path.dirname(video), os.path.splitext(os.path.basename(video))[0].lower()
    subdirs = []
    try:
        names = os.listdir(d)
    except OSError:
        return None
    found = [os.path.join(d, f) for f in names if os.path.splitext(f)[1].lower() in ('.srt', '.vtt')]
    if own_folder:
        for x in names:
            if re.fullmatch(r'subs?|subtitles?', x, re.I) and os.path.isdir(os.path.join(d, x)):
                for base, dirs, files in os.walk(os.path.join(d, x)):
                    found += [os.path.join(base, f) for f in files if os.path.splitext(f)[1].lower() in ('.srt', '.vtt')]
    else:
        found = [f for f in found if os.path.basename(f).lower().startswith(stem)]

    def score(path):
        n = re.sub(r'[._\-\[\]()]+', ' ', os.path.relpath(path, d).lower())
        other = re.search(r'\b(fre|french|spa|spanish|ger|german|ita|italian|por|portuguese|rus|russian|chi|chinese|'
                          r'ara|arabic|hin|hindi|kor|korean|jpn|japanese|dut|dutch|swe|swedish|tur|turkish|pol|polish|heb|hebrew|gre|greek)\b', n)
        sc = (5 if re.search(r'\b(en|eng|english)\b', n) else 0) - (5 if other else 0)
        sc += 3 if os.path.basename(path).lower().startswith(stem) else 0
        sc -= 4 if 'forced' in n else 0
        try:
            sc += min(os.path.getsize(path), 200000) / 200000.0  # a fuller file beats a stub
        except OSError:
            pass
        return sc
    found = [f for f in found if score(f) > -1]
    return max(found, key=score) if found else None


def read_text(path):
    raw = open(path, 'rb').read(5_000_000)
    for enc in ('utf-8-sig', 'cp1252', 'latin-1'):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode('utf-8', 'replace')


# ---------------------------------------------------------------- OpenSubtitles (official API, your own key)

def opensubtitles_hash(path):
    """OpenSubtitles' file fingerprint: size + the 64-bit little-endian sums of the first and last 64 KiB."""
    size = os.path.getsize(path)
    if size < 131072:
        return None
    h = size
    with open(path, 'rb') as f:
        for pos in (0, size - 65536):
            f.seek(pos)
            for (word,) in struct.iter_unpack('<Q', f.read(65536)):
                h = (h + word) & 0xFFFFFFFFFFFFFFFF
    return '%016x' % h


class OpenSubtitles:
    """English subtitles from api.opensubtitles.com with the key in config.json. Downloads are cached."""

    def __init__(self, key, cache_dir, base='https://api.opensubtitles.com/api/v1'):
        self.key, self.cache_dir, self.base = key, cache_dir, base

    def _call(self, path, data=None):
        req = urllib.request.Request(self.base + path, data=json.dumps(data).encode() if data is not None else None,
                                     headers={'Api-Key': self.key, 'User-Agent': 'castcouch v' + VERSION,
                                              'Content-Type': 'application/json', 'Accept': 'application/json'})
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())

    def english(self, item):
        """(text, name) or None. Prefers a fingerprint match (made for this exact file), then the title."""
        cached = os.path.join(self.cache_dir, item['id'] + '.srt')
        if os.path.exists(cached):
            return read_text(cached), open(cached + '.name', encoding='utf-8').read() if os.path.exists(cached + '.name') else 'OpenSubtitles'
        try:
            fp = opensubtitles_hash(item['path'])
            m = re.match(r'(.*) \((\d{4})\)$', item['title'])
            q = {'languages': 'en', 'moviehash': fp} if fp else {}
            found = self._call('/subtitles?' + urllib.parse.urlencode(q)).get('data', []) if fp else []
            if not found:
                q = {'languages': 'en', 'query': m.group(1) if m else item['title']}
                if m:
                    q['year'] = m.group(2)
                found = self._call('/subtitles?' + urllib.parse.urlencode(q)).get('data', [])
            def rank(x):
                a = x.get('attributes', {})
                return (bool(a.get('moviehash_match')), not a.get('foreign_parts_only'), not a.get('hearing_impaired'),
                        bool(a.get('from_trusted')), a.get('download_count') or 0)
            found = [x for x in found if x.get('attributes', {}).get('files')]
            if not found:
                log.info('OpenSubtitles: nothing in English for %r', item['title'])
                return None
            best = max(found, key=rank)['attributes']
            link = self._call('/download', {'file_id': best['files'][0]['file_id']})
            with urllib.request.urlopen(urllib.request.Request(link['link'], headers={'User-Agent': 'castcouch v' + VERSION}), timeout=20) as r:
                raw = r.read()
            name = best.get('release') or best['files'][0].get('file_name') or 'OpenSubtitles'
            os.makedirs(self.cache_dir, exist_ok=True)
            with open(cached, 'wb') as f:
                f.write(raw)
            with open(cached + '.name', 'w', encoding='utf-8') as f:
                f.write(name)
            log.info('OpenSubtitles: %r for %r (%s downloads left today)', name, item['title'], link.get('remaining'))
            return read_text(cached), name
        except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
            log.warning('OpenSubtitles failed for %r: %s', item['title'], e)
            return None


REMOTE = '''<!doctype html><meta name=viewport content="width=device-width,initial-scale=1"><title>castcouch remote</title>
<style>body{font:17px sans-serif;margin:0;padding:12px;background:#111;color:#eee}button{margin:4px 0;padding:14px;font:16px sans-serif;
border:0;border-radius:10px;background:#1a73e8;color:#fff}.row{display:flex;gap:8px}.row button{flex:1}#now{background:#1d1d1d;
border-radius:12px;padding:12px;margin-bottom:12px}#s{min-height:2.4em}input[type=search]{width:100%;box-sizing:border-box;padding:12px;
font:17px sans-serif;border-radius:10px;border:0;margin:6px 0}.film{padding:12px 6px;border-bottom:1px solid #2a2a2a}.film b{display:block}
.film small{color:#999}.tag{display:inline-block;font-size:12px;padding:1px 6px;border-radius:6px;margin-left:6px;background:#333}
.warn{background:#7a4a00}#msg{color:#f6ae2d;min-height:1.2em}label{display:block;margin:6px 0}</style>
<h2 style="margin:4px 0 10px">castcouch remote</h2>
<div id=now><div id=s>…</div>
<div class=row><button onclick="c('pause')">Pause</button><button onclick="c('play')">Play</button></div>
<div class=row><button onclick="c('seek',-30)">−30 s</button><button onclick="c('seek',30)">+30 s</button></div>
<div class=row><button onclick="c('subtitles-on')">Subtitles on</button><button onclick="c('subtitles-off')">Subtitles off</button></div>
<button onclick="c('stop')" style="background:#c5221f;width:100%">Stop</button></div>
<div id=lib><b>Your films</b> <small id=count></small>
<input type=search id=q placeholder="Search" oninput="draw()">
<label><input type=checkbox id=subs checked> English subtitles (beside the film<span id=os></span>)</label>
<div id=msg></div><div id=list></div><button onclick="load(1)" style="background:#333;width:100%">Look for new films</button></div>
<script>
const t=s=>{const h=Math.floor(s/3600),m=Math.floor(s/60)%60;return(h?h+':':'')+String(m).padStart(h?2:1,'0')+':'+String(s%60).padStart(2,'0')};
const $=id=>document.getElementById(id);
const show=r=>{$('s').textContent=r.ok===false?r.message:(r.playing?(r.title||'Video')+' — '+r.state.toLowerCase()+' at '+t(r.position)+' on '+r.tv+(r.has_subtitles?(r.subtitles?' (subtitles on)':' (subtitles off)'):''):'Nothing playing from castcouch.')};
async function c(action,seconds){show(await (await fetch('/control',{method:'POST',body:JSON.stringify({action,seconds})})).json())}
async function poll(){try{show(await (await fetch('/status')).json())}catch(e){$('s').textContent='Service not answering.'}}
let films=[];
async function load(refresh){$('msg').textContent=refresh?'Looking…':'';try{const r=await (await fetch('/library'+(refresh?'?refresh=1':''))).json();
 films=r.items||[];$('count').textContent=films.length?'('+films.length+')':'';$('os').textContent=r.opensubtitles?', else OpenSubtitles':'';
 if(!r.folders||!r.folders.length)$('msg').textContent='No film folders set up on the PC.';else $('msg').textContent='';draw()}catch(e){$('msg').textContent='Could not load the library.'}}
function draw(){const q=$('q').value.toLowerCase().trim(),L=$('list');L.textContent='';
 for(const f of films.filter(f=>!q||(f.title+' '+f.file).toLowerCase().includes(q)).slice(0,2000)){
  const d=document.createElement('div');d.className='film';const b=document.createElement('b');b.textContent=f.title;d.append(b);
  const sm=document.createElement('small');sm.textContent=[f.drive,f.quality,(f.size/2**30).toFixed(1)+' GB'].filter(Boolean).join(' · ');d.append(sm);
  if(f.subs){const x=document.createElement('span');x.className='tag';x.textContent='subtitles';d.append(x)}
  if(f.maybe){const x=document.createElement('span');x.className='tag warn';x.textContent='may not play';d.append(x)}
  d.onclick=()=>play(f);L.append(d)}}
async function play(f){$('msg').textContent='Starting '+f.title+' on the TV…';window.scrollTo(0,0);
 try{const r=await (await fetch('/play-local',{method:'POST',body:JSON.stringify({id:f.id,subtitles:$('subs').checked})})).json();
  $('msg').textContent=r.ok?'':r.message;if(r.ok){show(r);if($('subs').checked&&!r.has_subtitles)$('msg').textContent='Playing, but no English subtitles were found.'}}
 catch(e){$('msg').textContent='The service did not answer.'}}
poll();setInterval(poll,3000);load(0)</script>'''


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
            if n > 4_000_000:  # subtitle files ride along
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
            if path == '/library':
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                return self.reply(200, {'ok': True, 'items': home.library.listing(force='refresh' in q),
                                        'folders': home.library.roots, 'opensubtitles': bool(home.opensubtitles)})
            m = re.fullmatch(r'/s/([0-9a-f]+)/(p\.m3u8|(\d+)\.(?:ts|m4s)|init\.mp4|sub\d+\.vtt|video\.\w+)', path)
            if not m:
                return self.reply(404, {'ok': False, 'code': 'not-found', 'message': 'Not found.'})
            s = home.session
            if not s or s['id'] != m.group(1):
                return self.reply(404, {'ok': False, 'code': 'gone', 'message': 'That video is no longer being relayed.'})
            if m.group(2).startswith('video'):
                if s.get('kind') != 'local':
                    return self.reply(404, {'ok': False, 'code': 'not-found', 'message': 'Not found.'})
                return self.send_file(s['path'], s['ctype'])
            if m.group(2).startswith('sub'):
                if not s.get('vtt'):
                    return self.reply(404, {'ok': False, 'code': 'gone', 'message': 'No subtitles.'})
                return self.reply(200, s['vtt'], 'text/vtt; charset=utf-8')
            if m.group(2) == 'p.m3u8':
                return self.reply(200, s['playlist'], 'application/vnd.apple.mpegurl')
            n = -1 if m.group(2) == 'init.mp4' else int(m.group(3))
            fmp4 = m.group(2).endswith(('.m4s', '.mp4'))
            if fmp4 != (s.get('fmt') == 'fmp4'):
                return self.reply(404, {'ok': False, 'code': 'not-found', 'message': 'Not found.'})
            body, t = home.segment(m.group(1), n), time.time()
            self.reply(200, body, 'video/mp4' if fmp4 else 'video/mp2t')
            if n >= 0:
                home.delivered(n, time.time() - t, len(body))

        def send_file(self, path, ctype):
            """The file, or the byte range the player asks for (players read a film in ranges and seek with them)."""
            size = os.path.getsize(path)
            start, end, status = 0, size - 1, 200
            m = re.fullmatch(r'bytes=(\d*)-(\d*)', self.headers.get('Range', '').strip())
            if m and (m.group(1) or m.group(2)):
                if m.group(1):
                    start = int(m.group(1))
                    end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
                else:  # the last N bytes
                    start = max(0, size - int(m.group(2)))
                if start >= size or start > end:
                    self.send_response(416)
                    self.send_header('Content-Range', 'bytes */%d' % size)
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                    return
                status = 206
            self.send_response(status)
            self.send_header('Content-Type', ctype)
            self.send_header('Accept-Ranges', 'bytes')
            self.send_header('Content-Length', str(end - start + 1))
            if status == 206:
                self.send_header('Content-Range', 'bytes %d-%d/%d' % (start, end, size))
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            if self.command == 'HEAD':
                return
            with open(path, 'rb') as f:
                f.seek(start)
                left = end - start + 1
                while left > 0:
                    chunk = f.read(min(1 << 20, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)

        def post(self):
            path = urllib.parse.urlparse(self.path).path
            if path == '/play':
                return self.reply(200, home.play(self.body()))
            if path == '/play-local':
                return self.reply(200, home.play_local(self.body()))
            if path == '/subtitles':
                return self.reply(200, home.subtitles(self.body()))
            if path == '/control':
                return self.reply(200, home.control(self.body()))
            self.reply(404, {'ok': False, 'code': 'not-found', 'message': 'Not found.'})
    return Handler


def serve(args):
    here = os.path.dirname(args.log)
    config = {}
    try:
        with open(args.config or os.path.join(here, 'config.json'), encoding='utf-8-sig') as f:
            config = json.load(f)
    except (OSError, ValueError) as e:
        log.info('no config.json (%s); defaults used', e)
    key = config.get('opensubtitles_api_key')
    home = Home(args.port, args.tv, args.tv_name, args.state or os.path.join(here, 'session.json'),
                Library(args.library or config.get('library') or []),
                OpenSubtitles(key, os.path.join(here, 'subtitles')) if key else None)
    threading.Thread(target=home.library.scan, daemon=True).start()
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
            if home.session:
                media = home.attach().media
                log.info('TV player after restart: %s', (media or {}).get('playerState', 'none'))
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
    for folder in args.library or []:
        cmd += ['--library', folder]
    if args.config:
        cmd += ['--config', args.config]
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
    p.add_argument('--state', help='where to keep what is playing (default: session.json beside the log)')
    p.add_argument('--library', action='append', help='a folder of films (repeatable); default: "library" in config.json')
    p.add_argument('--config', help='settings file (default: config.json beside the log)')
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
