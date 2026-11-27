"""Checks a converted film against its original: streams and durations, a full decode, decoded video frames and
audio at five points. Usage: verify.py <kind> <original> <converted>"""
import json, subprocess, sys, numpy as np
kind, src, out = sys.argv[1:4]
run = lambda *a, **k: subprocess.run(list(a), capture_output=True, **k)
probe = lambda f: json.loads(run('ffprobe', '-v', 'error', '-print_format', 'json', '-show_format', '-show_streams', f, text=True).stdout)
ps, po = probe(src), probe(out)
dur = lambda p: float(p['format']['duration'])
print(f"== {kind}: {out.split('/')[-1][:70]}")
print('  streams:', ' + '.join(f"{s['codec_type'][0]}:{s['codec_name']}{('/' + str(s.get('channels'))+'ch') if s['codec_type']=='audio' else ''}{'*' if s.get('disposition',{}).get('default') else ''}" for s in po['streams']))
print(f"  duration: original {dur(ps):.2f} s, converted {dur(po):.2f} s, difference {dur(po)-dur(ps):+.2f} s")
pk = lambda f: int(run('ffprobe', '-v', 'error', '-select_streams', 'v:0', '-count_packets', '-show_entries', 'stream=nb_read_packets', '-of', 'csv=p=0', f, text=True).stdout.strip() or 0)
a, b = pk(src), pk(out)
print(f"  video frames: original {a}, converted {b} ({b-a:+d})")
err = run('ffmpeg', '-v', 'error', '-i', out, '-map', '0', '-f', 'null', '-', text=True).stderr.strip().splitlines()
print(f"  full decode of converted file: {len(err)} errors" + (f" e.g. {err[0][:120]}" if err else ''))
D = min(dur(ps), dur(po))
points = [D * f for f in (0.1, 0.3, 0.5, 0.7, 0.9)]
def frames(f, t, n=48):
    r = run('ffmpeg', '-v', 'error', '-ss', f'{t:.3f}', '-i', f, '-map', '0:v:0', '-frames:v', str(n), '-f', 'framemd5', '-', text=True).stdout
    return [l.split(',')[-1].strip() for l in r.splitlines() if l and not l.startswith('#')]
def pcm(f, t, track, secs=10):
    r = run('ffmpeg', '-v', 'error', '-ss', f'{t:.3f}', '-i', f, '-map', f'0:a:{track}', '-t', str(secs), '-ac', '1', '-ar', '16000', '-f', 'f32le', '-')
    return np.frombuffer(r.stdout, dtype=np.float32)
def match(x, y, maxlag=8000):  # best lag (ms) of y against x, correlation there, level difference (dB)
    n = min(len(x), len(y)); x, y = x[:n] - x[:n].mean(), y[:n] - y[:n].mean()
    if n < 16000 or x.std() < 1e-4: return None
    c = np.fft.irfft(np.fft.rfft(x, 2 * n) * np.conj(np.fft.rfft(y, 2 * n)))
    c = np.concatenate([c[-maxlag:], c[:maxlag + 1]]); i = int(np.argmax(c)); lag = i - maxlag
    xs, ys = (x[lag:], y[:n - lag]) if lag >= 0 else (x[:n + lag], y[-lag:])
    return lag / 16, float(np.corrcoef(xs, ys)[0, 1]), 20 * np.log10((y.std() + 1e-9) / (x.std() + 1e-9))
for t in points:
    line = f"  at {int(t//3600)}:{int(t%3600//60):02d}:{int(t%60):02d}  "
    if kind == 'video':
        r = run('ffmpeg', '-v', 'info', '-ss', f'{t:.3f}', '-i', src, '-ss', f'{t:.3f}', '-i', out, '-t', '3',
                '-lavfi', '[0:v]format=yuv420p[a];[1:v]format=yuv420p[b];[a][b]ssim;[0:v]format=yuv420p[c];[1:v]format=yuv420p[d];[c][d]psnr', '-f', 'null', '-', text=True).stderr
        ssim = [l for l in r.splitlines() if 'SSIM' in l and 'All:' in l]; psnr = [l for l in r.splitlines() if 'PSNR' in l and 'average:' in l]
        line += f"picture SSIM {ssim[-1].split('All:')[1].split()[0] if ssim else '?'}  PSNR {psnr[-1].split('average:')[1].split()[0] if psnr else '?'} dB"
    else:
        fa, fb = frames(src, t), frames(out, t)
        same = sum(x == y for x, y in zip(fa, fb))
        line += f"picture {same}/{len(fa)} decoded frames identical"
    m = match(pcm(src, t, 0), pcm(out, t, 0))
    line += ' | sound ' + (f"offset {m[0]:+.1f} ms, correlation {m[1]:.3f}, level {m[2]:+.1f} dB" if m else 'silent here')
    if kind == 'sound':
        m2 = match(pcm(src, t, 0), pcm(out, t, 1))
        line += f" | kept original track: offset {m2[0]:+.1f} ms, correlation {m2[1]:.3f}" if m2 else ''
    print(line)
