"""A stand-in Cast receiver for tests: CASTV2 over TLS, with Google's default receiver as its only app.

On LOAD it fetches the playlist and its first segment from the given URL, as the real TV does, and answers
PLAYING only if the segment is MPEG-TS from byte 0. Events are recorded in .events (and printed as JSON lines
when run as a script: python fake_tv.py [port] [--fail-load]).
"""
import json
import os
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'home'))
from castcouch_home import NS, decode, encode  # noqa: E402


def make_cert(d):
    key, crt = os.path.join(d, 'k.pem'), os.path.join(d, 'c.pem')
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1', '-subj', '/CN=fake-tv',
                    '-keyout', key, '-out', crt], check=True, capture_output=True)
    return crt, key


class FakeTV:
    def __init__(self, port=0, fail_load=False, echo=False):
        self.events, self.fail_load, self.echo = [], fail_load, echo
        self.app = None  # None or our receiver
        self.media = None
        self.tmp = tempfile.TemporaryDirectory()
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(*make_cert(self.tmp.name))
        self.ctx = ctx
        self.srv = socket.socket()
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(('127.0.0.1', port))
        self.srv.listen(5)
        self.port = self.srv.getsockname()[1]
        self.conns, self.frozen = [], set()
        threading.Thread(target=self.accept, daemon=True).start()

    def event(self, **e):
        self.events.append(e)
        if self.echo:
            print(json.dumps(e), flush=True)

    def accept(self):
        while True:
            try:
                c, _ = self.srv.accept()
                c = self.ctx.wrap_socket(c, server_side=True)
            except OSError:
                return
            self.conns.append(c)
            threading.Thread(target=self.serve, args=(c,), daemon=True).start()

    def close(self):
        try:
            self.srv.shutdown(socket.SHUT_RDWR)  # unblocks accept(); close() alone leaves it listening
        except OSError:
            pass
        self.srv.close()
        self.drop()
        self.tmp.cleanup()

    def drop(self):
        """Cut every connection, as a TV that restarts would."""
        for c in self.conns:
            try:
                c.shutdown(socket.SHUT_RDWR)
                c.close()
            except OSError:
                pass
        self.conns = []

    def freeze(self):
        """Existing connections stop answering but stay open, like a TV that lost power or Wi-Fi."""
        self.frozen.update(id(c) for c in self.conns)

    def receiver_status(self, rid=0):
        apps = [{'appId': 'CC1AD845', 'displayName': 'Default Media Receiver', 'sessionId': 's-1', 'transportId': 'web-1'}] if self.app else []
        return {'type': 'RECEIVER_STATUS', 'requestId': rid, 'status': {'applications': apps}}

    def media_status(self, rid=0):
        return {'type': 'MEDIA_STATUS', 'requestId': rid, 'status': [self.media] if self.media else []}

    def serve(self, c):
        def send(src, ns, payload):
            c.sendall(encode(src, 'sender-0', ns, payload))

        def recv(n):
            b = b''
            while len(b) < n:
                x = c.recv(n - len(b))
                if not x:
                    raise OSError('closed')
                b += x
            return b
        try:
            while True:
                m = decode(recv(struct.unpack('>I', recv(4))[0]))
                if id(c) in self.frozen:
                    continue
                d, ns, rid = m['data'], m['ns'], m['data'].get('requestId', 0)
                t = d.get('type')
                if ns == 'tp.heartbeat':
                    if t == 'PING':
                        send(m['dst'], ns, {'type': 'PONG'})
                    continue
                if ns == 'tp.connection':
                    continue
                self.event(ns=ns, type=t)
                if ns == 'receiver' and t == 'GET_STATUS':
                    send('receiver-0', ns, self.receiver_status(rid))
                elif ns == 'receiver' and t == 'LAUNCH':
                    send('receiver-0', ns, {'type': 'LAUNCH_STATUS', 'launchRequestId': rid, 'status': 'USER_ALLOWED'})
                    self.app = True
                    send('receiver-0', ns, self.receiver_status(0))
                elif ns == 'media' and t == 'LOAD':
                    url = d['media']['contentId']
                    try:
                        pl = urllib.request.urlopen(url, timeout=10).read().decode()
                        first = [l for l in pl.splitlines() if l and not l.startswith('#')][0]
                        seg = urllib.request.urlopen(urllib.parse.urljoin(url, first), timeout=10).read()
                        ok = seg[:1] == b'\x47' and not self.fail_load
                        self.event(fetched=url, segment_bytes=len(seg), ts_at_0=seg[:1] == b'\x47', position=d.get('currentTime'), title=d['media']['metadata'].get('title'))
                    except Exception as e:
                        ok = False
                        self.event(fetch_error=str(e))
                    if ok:
                        self.media = {'mediaSessionId': 1, 'playerState': 'PLAYING', 'currentTime': d.get('currentTime', 0)}
                        send('web-1', ns, self.media_status(rid))
                    else:
                        send('web-1', ns, {'type': 'LOAD_FAILED', 'requestId': rid})
                elif ns == 'media' and t in ('PAUSE', 'PLAY', 'STOP', 'SEEK', 'GET_STATUS'):
                    if self.media and t == 'SEEK':
                        self.media['currentTime'] = d['currentTime']
                    if self.media and t in ('PAUSE', 'PLAY'):
                        self.media['playerState'] = 'PAUSED' if t == 'PAUSE' else 'PLAYING'
                    if t == 'STOP':
                        self.media = None
                    send('web-1', ns, self.media_status(rid))
        except (OSError, ValueError):
            return


if __name__ == '__main__':
    tv = FakeTV(int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 0, '--fail-load' in sys.argv, echo=True)
    print(json.dumps({'port': tv.port}), flush=True)
    threading.Event().wait()
