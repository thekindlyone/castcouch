"""One-time batch: convert each film that the TV can't play, check the copy against the original, and only if every
check passes delete the original and give the copy the original's name (<stem>.mp4, so subtitle files still match).
A film that fails any check keeps its original and its .castcouch.mp4 copy stays beside it for a look.

Usage: batch.py <rows.json from classify.py> <log.jsonl>     (resumable: films already logged as done are skipped)
"""
import json, os, shutil, subprocess, sys, time, urllib.request
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
KIND = {'video re-encode': 'video', 'sound fix': 'sound', 'HEVC + sound fix': 'sound', 'container only': 'container'}
run = lambda *a, **k: subprocess.run(list(a), capture_output=True, **k)


def probe(f):
    return json.loads(run('ffprobe', '-v', 'error', '-print_format', 'json', '-show_format', '-show_streams', f, text=True).stdout)


def fps(p):
    v = next(s for s in p['streams'] if s['codec_type'] == 'video')
    n, d = (v.get('avg_frame_rate') or v['r_frame_rate']).split('/')
    return float(n) / float(d) if float(d) else 25.0


def packets(f):
    r = run('ffprobe', '-v', 'error', '-select_streams', 'v:0', '-count_packets', '-show_entries', 'stream=nb_read_packets',
            '-of', 'csv=p=0', f, text=True).stdout.strip()
    return int(r.split(',')[0] or 0)


def md5s(f, t, n):
    r = run('ffmpeg', '-v', 'error', '-ss', f'{max(t, 0):.3f}', '-i', f, '-map', '0:v:0', '-frames:v', str(n), '-f', 'framemd5', '-', text=True).stdout
    return [l.split(',')[-1].strip() for l in r.splitlines() if l and not l.startswith('#')]


def ssim(src, out, t, shift):
    r = run('ffmpeg', '-v', 'info', '-ss', f'{t:.3f}', '-i', src, '-ss', f'{t + shift:.3f}', '-i', out, '-t', '3', '-lavfi',
            '[0:v]format=yuv420p[a];[1:v]format=yuv420p[b];[a][b]ssim', '-f', 'null', '-', text=True).stderr
    s = [l for l in r.splitlines() if 'SSIM' in l and 'All:' in l]
    return float(s[-1].split('All:')[1].split()[0]) if s else 0.0


def pcm(f, t, track=0, secs=10):
    r = run('ffmpeg', '-v', 'error', '-ss', f'{t:.3f}', '-i', f, '-map', f'0:a:{track}', '-t', str(secs), '-ac', '1', '-ar', '16000', '-f', 'f32le', '-')
    return np.frombuffer(r.stdout, dtype=np.float32)


def match(x, y, maxlag=8000):
    """(lag ms, correlation) of y against x, or None where it's too quiet to tell."""
    n = min(len(x), len(y)); x, y = x[:n] - x[:n].mean(), y[:n] - y[:n].mean()
    if n < 16000 or x.std() < 1e-3 or y.std() < 1e-3:
        return None
    c = np.fft.irfft(np.fft.rfft(x, 2 * n) * np.conj(np.fft.rfft(y, 2 * n)))
    c = np.concatenate([c[-maxlag:], c[:maxlag + 1]]); lag = int(np.argmax(c)) - maxlag
    xs, ys = (x[lag:], y[:n - lag]) if lag >= 0 else (x[:n + lag], y[-lag:])
    return lag / 16, float(np.corrcoef(xs, ys)[0, 1])


def verify(kind, src, out):
    """Returns (passed, facts). Every check must pass."""
    ps, po = probe(src), probe(out)
    f, bad = {}, []
    d0, d1 = float(ps['format']['duration']), float(po['format']['duration'])
    f['duration'] = [round(d0, 2), round(d1, 2)]
    if abs(d1 - d0) > 1.0:
        bad.append(f'duration differs by {d1 - d0:+.2f} s')
    a, b = packets(src), packets(out)
    f['frames'] = [a, b]
    if abs(b - a) > max(3, a * 0.0005):
        bad.append(f'frame count {a} -> {b}')
    errs = run('ffmpeg', '-v', 'error', '-i', out, '-map', '0', '-f', 'null', '-', text=True).stderr.strip().splitlines()
    f['decode_errors'] = len(errs)
    if errs:
        bad.append(f'{len(errs)} decode errors, e.g. {errs[0][:100]}')
    D, rate = min(d0, d1), fps(ps)
    pics, sounds = [], []
    for t in (D * x for x in (0.1, 0.3, 0.5, 0.7, 0.9)):
        if kind == 'video':
            best = max((ssim(src, out, t, k / rate), k) for k in (-2, -1, 0, 1, 2))
            pics.append(round(best[0], 3))
            if best[0] < 0.93:
                bad.append(f'picture SSIM {best[0]:.3f} at {t:.0f} s')
        else:
            orig, conv = md5s(src, t - 2, int(rate * 6) + 48), md5s(out, t, 48)
            hit = len(conv) == 48 and any(orig[i:i + 48] == conv for i in range(len(orig) - 47))
            pics.append(hit)
            if not hit:
                bad.append(f'picture frames not found in original at {t:.0f} s')
        m = match(pcm(src, t), pcm(out, t))
        sounds.append(m and [m[0], round(m[1], 3)])
        if m and (abs(m[0]) > 45 or m[1] < 0.9):
            bad.append(f'sound offset {m[0]:+.0f} ms / correlation {m[1]:.3f} at {t:.0f} s')
    f['picture'], f['sound'] = pics, sounds
    if sum(1 for s in sounds if s) < 3:
        bad.append('sound too quiet to compare at 3 of 5 points')
    f['problems'] = bad
    return not bad, f


SERVICE = 'http://192.168.88.8:8899/status'


def tv_busy():
    """True while the TV plays a film from this PC's library (that file may be one we are about to delete)."""
    try:
        with urllib.request.urlopen(SERVICE, timeout=5) as r:
            st = json.loads(r.read())
        return bool(st.get('playing') and st.get('local'))
    except Exception:
        return False  # service down: nothing can be playing from it


def main():
    rows, logp = json.load(open(sys.argv[1])), sys.argv[2]
    done = set()
    if os.path.exists(logp):
        done = {json.loads(l)['path'] for l in open(logp) if l.strip()}
    todo = sorted((r for r in rows if r['cat'] in KIND and r['path'] not in done), key=lambda r: r['size'])
    print(f'{len(todo)} films to do', flush=True)
    for r in todo:
        src, kind = r['path'], KIND[r['cat']]
        stem = os.path.splitext(src)[0]
        out, final = stem + '.castcouch.mp4', stem + '.mp4'
        rec = dict(path=src, kind=kind, size=r['size'], started=time.strftime('%Y-%m-%d %H:%M:%S'))
        if not os.path.exists(src):
            rec.update(result='original missing')
        elif final != src and os.path.exists(final):
            rec.update(result=f'skipped: {os.path.basename(final)} already exists')
        elif shutil.disk_usage(os.path.dirname(src)).free < r['size'] * 1.6 + 2 * 2**30:
            rec.update(result='skipped: not enough free space')
        else:
            t = time.time()
            if not os.path.exists(out):
                c = run('bash', os.path.join(HERE, 'convert.sh'), kind, src, text=True)
                if c.returncode or not os.path.exists(out):
                    rec.update(result='conversion failed', error=(c.stderr or c.stdout)[-500:])
            rec['convert_s'] = round(time.time() - t)
            if 'result' not in rec:
                t = time.time()
                ok, facts = verify(kind, src, out)
                rec.update(verify_s=round(time.time() - t), checks=facts, out_size=os.path.getsize(out))
                if ok:
                    waited = 0
                    while tv_busy():  # never pull a file from under the TV; wait for the film to end
                        time.sleep(60); waited += 60
                    rec['waited_for_tv_s'] = waited
                    os.remove(src)  # permanent, as agreed: only after every check passed
                    os.replace(out, final)
                    rec.update(result='replaced', final=final)
                else:
                    rec.update(result='kept original: checks failed')
        rec['ended'] = time.strftime('%Y-%m-%d %H:%M:%S')
        with open(logp, 'a') as fh:
            fh.write(json.dumps(rec) + '\n')
        print(f"{rec['ended']}  {rec['result']:34s} {kind:9s} {os.path.basename(src)[:70]}"
              + (f"  ({'; '.join(rec['checks']['problems'])})" if rec.get('checks', {}).get('problems') else ''), flush=True)


if __name__ == '__main__':
    main()
