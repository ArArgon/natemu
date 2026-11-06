"""Runs inside the pinned container: times QEMU Linux-user on prepared ELFs (timing is taken here, so docker start/attach overhead is excluded)."""
import hashlib, json, pathlib, subprocess, time
job = json.load(open('/work/jobs.json'))
out = pathlib.Path('/work/out'); out.mkdir(exist_ok=True)
res = {'runs': [], 'counts': {}}
for r in job['runs']:
    t = time.perf_counter()
    p = subprocess.run(['/qemu'] + job['flags'] + [r['elf']], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=600)
    dt = time.perf_counter() - t
    e = {'id': r['id'], 't': dt, 'rc': p.returncode, 'sha': hashlib.sha256(p.stdout).hexdigest()}
    if r.get('keep'): (out / (r['id'] + '.out')).write_bytes(p.stdout)
    res['runs'].append(e)
for c in job['counts']:        # dynamic instruction count: one TB per instruction (-singlestep), one "Trace" line per executed TB
    subprocess.run(['/qemu'] + job['flags'] + ['-d', 'exec,nochain', '-singlestep', '-D', '/tmp/log', c['elf']], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=600)
    res['counts'][c['id']] = sum(1 for line in open('/tmp/log') if line.startswith('Trace'))
    pathlib.Path('/tmp/log').unlink()
json.dump(res, open('/work/results.json', 'w'))
