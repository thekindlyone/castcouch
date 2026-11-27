import json, os, sys, collections
res = json.load(open(sys.argv[1]))
OK_V8 = {'h264'}; OK_A = {'aac', 'mp3', 'opus', 'vorbis'}; OK_C = {'.mp4', '.m4v', '.mkv', '.webm'}
rows = []
for p, d, err in res:
    st = d.get('streams', []); ext = os.path.splitext(p)[1].lower()
    v = next((s for s in st if s['codec_type'] == 'video' and not s.get('disposition', {}).get('attached_pic')), None)
    auds = [s for s in st if s['codec_type'] == 'audio']
    a = next((s for s in auds if s.get('disposition', {}).get('default')), auds[0] if auds else None)
    subs = [s for s in st if s['codec_type'] == 'subtitle']
    vc, pf = (v or {}).get('codec_name'), (v or {}).get('pix_fmt', '')
    ac = (a or {}).get('codec_name')
    ten = '10' in pf or '12' in pf
    if vc == 'h264' and not ten: vk = 'ok'
    elif vc == 'hevc': vk = 'hevc'
    else: vk = 'reencode'
    ak = 'ok' if ac in OK_A else ('none' if not a else 'fix')
    if vk == 'reencode': cat = 'video re-encode'
    elif vk == 'hevc': cat = 'HEVC (leave)' if ak == 'ok' else 'HEVC + sound fix'
    elif ak == 'fix': cat = 'sound fix'
    elif ext not in OK_C: cat = 'container only'
    else: cat = 'fine'
    rows.append(dict(path=p, cat=cat, ext=ext, v=f"{vc} {pf} {(v or {}).get('profile','')} {(v or {}).get('width')}x{(v or {}).get('height')}",
                     a=f"{ac} {(a or {}).get('channels')}ch", na=len(auds), ns=len(subs), sub_codecs=sorted({s['codec_name'] for s in subs}),
                     dur=float(d.get('format', {}).get('duration') or 0), size=os.path.getsize(p)))
json.dump(rows, open(sys.argv[2], 'w'))
c = collections.Counter(r['cat'] for r in rows)
for k, n in c.most_common():
    sz = sum(r['size'] for r in rows if r['cat'] == k) / 2**30
    print(f"{n:4d}  {k:18s} {sz:7.1f} GB")
print('\nvideo codecs needing re-encode:', collections.Counter(r['v'].split()[0] + ' ' + r['ext'] for r in rows if r['cat'] == 'video re-encode'))
print('sounds needing a fix:', collections.Counter(r['a'] for r in rows if 'sound' in r['cat']))
print('h264 10-bit:', sum(1 for r in rows if r['v'].startswith('h264') and '10' in r['v'].split()[1]))
print('embedded subtitle formats:', collections.Counter(x for r in rows for x in r['sub_codecs']))
for k in ('video re-encode', 'sound fix', 'container only', 'HEVC + sound fix'):
    print(f'\n-- {k} (smallest first)')
    for r in sorted((r for r in rows if r['cat'] == k), key=lambda r: r['size'])[:6]:
        print(f"  {r['size']/2**30:4.1f} GB {r['dur']/60:5.0f} min | {r['v'][:38]:38s} | {r['a']:10s} | {os.path.relpath(r['path'], '/mnt')[:70]}")
