#!/usr/bin/env python3
"""bench.py OUT NAME EMU VCPUS BENCH [JOBS]: one benchmark boot of the bench initramfs, natemu or QEMU. The guest runs the
benchmark twice (run 1 cold, run 2 warm), each timed on the host from its NATEMU-BENCH-START line to its NATEMU-BENCH-END line
on the serial console (START2/END2 for run 2; boot excluded) and verified (VERIFY / VERIFY2). A benchmark with phases (zstd:
compress, decompress, verify; 7zip: compress, test; mem: vm, mmap, fork) is also timed per phase, each from the previous marker
to its NATEMU-BENCH-PHASE (PHASE2) line (`phases` / `phases2`: name, host seconds, the guest's own guest_s and rc).
Record fields: `wall_s` / `guest_s` / `phases` / `verified1` are run 1 (their meaning in older records), `wall2_s` / `guest2_s`
/ `phases2` / `verified2` run 2, `verified` both runs (run 1 alone for a log without a run 2).

Same harness shape as natemu-raw/qcmp-20261103-01/boot.py (like-for-like): the serial file polled every 50 ms; a check
every 0.5 s for a foreign VM process (another natemu-host, natemu stub or QEMU), recorded as `contended`. EMU: natemu
(`natemu-host boot-linux` from BOOT_HOST/BOOT_RUNTIME/BOOT_FEXC; NATEMU_COMPILE_CACHE as the caller's environment has
it), qemu-multi / qemu-single (TCG thread=multi / single, qcmp's board and CPU model), qemu-multi-max (the same with -cpu max). JOBS defaults to VCPUS. The guest
powers off after its last VERIFY line; the run is capped at CAP s (env BENCH_CAP, default 600). Exit 0 verified, 1 not
reached or not verified, 3 verified but contended. Appends one JSON line to OUT/bench.jsonl; serial to OUT/NAME.serial.
"""
import json, os, re, signal, subprocess, sys, time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '..'))
from raw_root import RAW_ROOT                                       # tools/raw_root.py: NATEMU_RAW_ROOT, else beside the checkout
RAW = str(RAW_ROOT)
K = f'{RAW}/kernel-census-20261028-01/dl/linux'                     # Debian 6.12.107 bzImage
I = os.environ.get('BENCH_INITRD', f'{RAW}/bench-20261105-03/out/bench.cpio')
QEMU = '/opt/homebrew/bin/qemu-system-x86_64'
QCPU = 'Haswell,-pcid,-hle,-rtm,-invpcid,-tsc-deadline,-erms,-mca,-mce,+sha-ni,+adx,+clflushopt,+clwb,+smep,+umip'   # qcmp-20261103-01's like-for-like model (+sha-ni Nov 3; +adx,+clflushopt,+clwb Nov 4; +smep (Haswell's already),+umip Nov 4; TCG has no xsavec)
out, name, emu, n, bench = sys.argv[1:6]
jobs = sys.argv[6] if len(sys.argv) > 6 else n
cap = float(os.environ.get('BENCH_CAP', '600'))
cmdline = f'console=ttyS0 earlyprintk=serial mitigations=off nokaslr bench={bench} jobs={jobs}'
os.makedirs(out, exist_ok=True)
serial = f'{out}/{name}.serial'
if os.path.exists(serial): os.remove(serial)
env = dict(os.environ)
env.pop('NATEMU_PERF_LOG', None)
if emu == 'natemu':
    cmd = [env['BOOT_HOST'], env['BOOT_RUNTIME'], 'boot-linux', env['BOOT_FEXC'], K, '--initrd', I, '--cmdline', cmdline,
           '--vcpus', n, '--serial', serial, '--watchdog-ms', '60000']
else:
    cmd = [QEMU, '-nodefaults', '-machine', 'pc,hpet=off,i8042=off,vmport=off', '-accel', f"tcg,thread={emu.split('-')[1]}",
           '-cpu', 'max' if emu.endswith('-max') else QCPU, '-m', '2048', '-smp', n, '-kernel', K, '-initrd', I, '-append', cmdline, '-display', 'none',
           '-monitor', 'none', '-serial', f'file:{serial}', '-no-reboot']
def foreign(me):
    pids = set()
    for x in ('natemu-host', 'natemu', 'qemu-system-x86_64'):
        pids |= {int(p) for p in subprocess.run(['pgrep', '-x', x], capture_output=True, text=True).stdout.split()}
    found = set()
    for q in pids - {me}:
        info = subprocess.run(['ps', '-o', 'ppid=,command=', '-p', str(q)], capture_output=True, text=True).stdout.split(None, 1)
        if info and int(info[0]) != me: found.add(f'{q} {info[1].strip()[:160] if len(info) > 1 else ""}')
    return found
t0 = time.monotonic()
p = subprocess.Popen(cmd, env=env, stdout=open(f'{out}/{name}.stdout', 'w'), stderr=open(f'{out}/{name}.stderr', 'w'))
# markers carry a trailing space so START does not match START2; each is the host time the line first appeared
M = {k: None for k in ('START ', 'END ', 'START2 ', 'END2 ')}; text = ''; others = set(); last = 0.0; seen = [[], []]   # PHASE / PHASE2 host times
while time.monotonic() - t0 < cap and p.poll() is None:
    time.sleep(0.05)
    now = time.monotonic()
    if now - last >= 0.5:
        others |= foreign(p.pid); last = now
    try:
        text = open(serial, errors='replace').read()
    except FileNotFoundError:
        continue
    for k in M:
        if M[k] is None and 'NATEMU-BENCH-' + k in text: M[k] = now
    for i, tag in enumerate(('PHASE ', 'PHASE2 ')):
        seen[i] += [now] * (text.count('NATEMU-BENCH-' + tag) - len(seen[i]))
if p.poll() is None:
    p.send_signal(signal.SIGINT)
    try: p.wait(10)
    except subprocess.TimeoutExpired: p.kill(); p.wait()
total = time.monotonic() - t0
text = open(serial, errors='replace').read() if os.path.exists(serial) else ''
def run(sfx, start, end, seen):
    m = re.search(r'NATEMU-BENCH-END%s \S+ \S+ rc=(\d+) guest_s=([0-9.]+)' % sfx, text)
    marks = [start] + seen
    ph = [dict(name=p[0], wall_s=round(seen[i] - marks[i], 3) if start and i < len(seen) else None, rc=int(p[1]), guest_s=float(p[2]))
          for i, p in enumerate(re.findall(r'NATEMU-BENCH-PHASE%s \S+ \S+ name=(\S+) rc=(\d+) guest_s=([0-9.]+)' % sfx, text))]
    return dict(reached=end is not None, verified=('NATEMU-BENCH-VERIFY%s ok' % sfx) in text and ('NATEMU-BENCH-VERIFY%s FAIL' % sfx) not in text,
                wall_s=round(end - start, 3) if start and end else None, guest_s=float(m.group(2)) if m else None,
                rc_bench=int(m.group(1)) if m else None, phases=ph)
r1 = run('', M['START '], M['END '], seen[0]); r2 = run('2', M['START2 '], M['END2 '], seen[1])
verified = r1['verified'] and r2['verified']
start = M['START ']; end = M['END ']
rec = dict(name=name, emu=emu, vcpus=int(n), bench=bench, jobs=int(jobs), reached=r1['reached'] and r2['reached'], verified=verified,
           verified1=r1['verified'], verified2=r2['verified'], wall_s=r1['wall_s'], wall2_s=r2['wall_s'], guest_s=r1['guest_s'],
           guest2_s=r2['guest_s'], rc_bench=r1['rc_bench'], rc_bench2=r2['rc_bench'], phases=r1['phases'], phases2=r2['phases'],
           boot_to_start_s=round(start - t0, 3) if start else None,
           total_s=round(total, 3), cpus=re.findall(r'cpus=(\d+)', text)[:1], contended=sorted(others), rc=p.returncode,
           cmd=cmd, stderr=open(f'{out}/{name}.stderr').read()[-300:])
open(f'{out}/bench.jsonl', 'a').write(json.dumps(rec) + '\n')
print(json.dumps({k: rec[k] for k in ('name', 'verified', 'wall_s', 'wall2_s', 'guest_s', 'guest2_s', 'phases', 'phases2', 'boot_to_start_s', 'total_s', 'contended')}))
sys.exit(1 if not verified else 3 if others else 0)
