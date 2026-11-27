import subprocess, sys
kind, src, out = sys.argv[1:4]; points = [float(x) for x in sys.argv[4:]]
run = lambda *a: subprocess.run(list(a), capture_output=True, text=True)
def md5s(f, t, n):
    r = run('ffmpeg', '-v', 'error', '-ss', f'{t:.3f}', '-i', f, '-map', '0:v:0', '-frames:v', str(n), '-f', 'framemd5', '-')
    return [l.split(',')[-1].strip() for l in r.stdout.splitlines() if l and not l.startswith('#')]
for t in points:
    if kind == 'copy':
        a = md5s(src, t - 2, 150); b = md5s(out, t, 48)
        hits = [i for i in range(len(a) - len(b)) if a[i:i + len(b)] == b]
        print(f"  at {t:7.0f} s: converted's 48 frames {'match the original exactly, ' + str(hits[0] - 50) + ' frames from where its seek landed' if hits else 'NOT found in the original nearby'}")
    else:
        best = []
        for lag in (-2, -1, 0, 1, 2):  # shift the converted file by whole frames (25 fps)
            r = run('ffmpeg', '-v', 'info', '-ss', f'{t:.3f}', '-i', src, '-ss', f'{t + lag / 25:.3f}', '-i', out, '-t', '3',
                    '-lavfi', '[0:v]format=yuv420p[a];[1:v]format=yuv420p[b];[a][b]ssim', '-f', 'null', '-').stderr
            s = [l for l in r.splitlines() if 'SSIM' in l and 'All:' in l]
            best.append((float(s[-1].split('All:')[1].split()[0]) if s else 0, lag))
        print(f"  at {t:7.0f} s: SSIM by frame shift " + '  '.join(f"{l:+d}:{v:.3f}" for v, l in best) + f"  -> best {max(best)[1]:+d} frames, {max(best)[0]:.3f}")
