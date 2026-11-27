import json, os, subprocess, sys, re
from concurrent.futures import ThreadPoolExecutor
roots = ['/mnt/d/Movies', '/mnt/e/Movies']
exts = {'.mp4', '.m4v', '.mkv', '.avi', '.mov', '.webm', '.ts'}
files = []
for r in roots:
    for d, ds, fs in os.walk(r):
        ds[:] = [x for x in ds if 'sample' not in x.lower()]
        for f in fs:
            p = os.path.join(d, f)
            if os.path.splitext(f)[1].lower() in exts and 'sample' not in f.lower() and os.path.getsize(p) >= 50 * 2**20:
                files.append(p)
def probe(p):
    try:
        out = subprocess.run(['ffprobe', '-v', 'error', '-print_format', 'json', '-show_format', '-show_streams', p],
                             capture_output=True, timeout=120, text=True)
        return p, json.loads(out.stdout or '{}'), out.stderr[-300:]
    except Exception as e:
        return p, {}, str(e)
with ThreadPoolExecutor(6) as ex:
    res = list(ex.map(probe, files))
json.dump(res, open(sys.argv[1], 'w'))
print(len(res), 'probed;', sum(1 for r in res if not r[1].get('streams')), 'unreadable')
