#!/usr/bin/env python3
"""Probe this Mac's cores for the test framework's scheduler: the layout it detects, and how fast CPU work and disk work
run on a performance core, on an efficiency core as the framework confines work there (`taskpolicy -c background`,
testkit/runloop.py), and on an efficiency core with normal disk I/O priority (`-c background -d important`).

    python3 tools/probe_cores.py [--seconds S]

Run it on an otherwise idle machine (it measures time; `tools/natemu-test status` shows whether tests are running).
Everything it writes goes to a temporary folder, removed at the end. Prints a table and one JSON line.
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import time

CPU = r'''
import hashlib, time, sys
t = time.perf_counter(); n = 0; b = b'x' * 4096
while time.perf_counter() - t < float(sys.argv[1]):
    for _ in range(2000): b = hashlib.sha256(b).digest() * 128
    n += 1
print(n / (time.perf_counter() - t))
'''
DISK = r'''
import os, sys, time
d, size = sys.argv[1], 256 << 20
f = os.path.join(d, 'probe.bin'); chunk = os.urandom(1 << 20)
t = time.perf_counter()
with open(f, 'wb') as h:
    for _ in range(size >> 20): h.write(chunk)
    h.flush(); os.fsync(h.fileno())
w = time.perf_counter() - t
os.system('purge >/dev/null 2>&1')
t = time.perf_counter()
with open(f, 'rb', buffering=0) as h:
    while h.read(4 << 20): pass
r = time.perf_counter() - t
os.unlink(f)
print((size >> 20) / w, (size >> 20) / r)
'''
MODES = {
    'performance core (unconfined)': [],
    'efficiency core, as the framework runs it': ['taskpolicy', '-c', 'background'],
    'efficiency core, normal disk priority': ['taskpolicy', '-c', 'background', '-d', 'important'],
}


def sysctl(*names):
    out = {}
    for n in names:
        r = subprocess.run(['sysctl', '-n', n], capture_output=True, text=True)
        out[n] = r.stdout.strip() if r.returncode == 0 else None
    return out


def run(prefix, code, *args):
    r = subprocess.run(prefix + [sys.executable, '-c', code, *map(str, args)], capture_output=True, text=True)
    if r.returncode != 0: raise RuntimeError((r.stderr or r.stdout).strip()[-500:])
    return [float(x) for x in r.stdout.split()]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--seconds', type=float, default=5.0, help='length of each CPU measurement (default 5)')
    a = ap.parse_args()
    s = sysctl('machdep.cpu.brand_string', 'hw.perflevel0.physicalcpu', 'hw.perflevel1.physicalcpu', 'hw.memsize')
    print(f"{s['machdep.cpu.brand_string']}: {s['hw.perflevel0.physicalcpu']} performance + "
          f"{s['hw.perflevel1.physicalcpu'] or 0} efficiency cores, {int(s['hw.memsize'] or 0) / 2**30:.0f} GiB")
    res = {}
    with tempfile.TemporaryDirectory() as d:
        for name, prefix in MODES.items():
            cpu = run(prefix, CPU, a.seconds)[0]
            w, r = run(prefix, DISK, d)
            res[name] = {'cpu': cpu, 'disk_write_mib_s': w, 'disk_read_mib_s': r}
    base = res['performance core (unconfined)']
    print(f"{'':44} {'CPU':>8} {'write MiB/s':>12} {'read MiB/s':>11}")
    for name, v in res.items():
        print(f"{name:44} {v['cpu'] / base['cpu']:7.2f}x {v['disk_write_mib_s']:12.0f} {v['disk_read_mib_s']:11.0f}")
    print(json.dumps({'chip': s['machdep.cpu.brand_string'], 'p': s['hw.perflevel0.physicalcpu'],
                      'e': s['hw.perflevel1.physicalcpu'], 'modes': res}))
    return 0


if __name__ == '__main__':
    sys.exit(main())
