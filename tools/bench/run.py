#!/usr/bin/env python3
"""One benchmark boot of the bench initramfs, natemu or QEMU (from natemu-raw/bench-20261105-03/scripts/bench.py; the
measurement, verification and record are that script's, natemu now runs as the `natemu` stub with named arguments).

    python3 tools/bench/run.py --out DIR --name NAME --emu natemu|qemu-multi|qemu-single|qemu-multi-max --vcpus N
        --bench BENCH --kernel BZIMAGE --initrd CPIO [--jobs J] [--fexc FEXC] [--natemu STUB] [--runtime FILE]
        [--compile-cache DIR|off] [--cap SECONDS] [--qemu QEMU]

The guest runs the benchmark twice (run 1 cold, run 2 warm), each timed on the host from its NATEMU-BENCH-START line to its
NATEMU-BENCH-END line on the serial console (START2/END2 for run 2; boot excluded) and verified (VERIFY / VERIFY2). A
benchmark with phases (zstd: compress, decompress, verify; 7zip: compress, test; mem: vm, mmap, fork) is also timed per
phase, each from the previous marker to its NATEMU-BENCH-PHASE (PHASE2) line (`phases` / `phases2`: name, host seconds,
the guest's own guest_s and rc). Record fields: `wall_s` / `guest_s` / `phases` / `verified1` are run 1, `wall2_s` /
`guest2_s` / `phases2` / `verified2` run 2, `verified` both runs. Marker times use the end of their serial read;
`timing` retains monotonic read brackets, and `wall_bounds_s` / `wall2_bounds_s` (also on phases) expose polling uncertainty.

The serial file is polled every 50 ms; every 0.5 s `vms.sh` lists foreign VM processes (another natemu stub, natemu-host
or QEMU, not this boot's own children), recorded as `contended`. natemu: the stub (`--natemu`, default
target/release/natemu; its resident `--runtime`, default the one built beside it), `--fexc` required; the compile cache
is `--compile-cache` when given, else the stub's default (its NATEMU_COMPILE_CACHE fallback included). QEMU: TCG
thread=multi or single, the board and CPU model of natemu-raw/qcmp-20261103-01 (`-max`: `-cpu max`). JOBS defaults to
VCPUS. The guest powers off after its last VERIFY line; the run is capped at `--cap` s (default 600). Exit 0 verified, 1
not reached or not verified, 3 verified but contended. Appends one JSON line to DIR/bench.jsonl; serial to
DIR/NAME.serial. Run it under the VM lock (`/usr/bin/lockf -k .../natemu-raw/.vm.lock`) with no other VM running.
"""
import argparse, json, os, pathlib, re, signal, subprocess, sys, time

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
HERE = pathlib.Path(__file__).resolve().parent
QCPU = 'Haswell,-pcid,-hle,-rtm,-invpcid,-tsc-deadline,-erms,-mca,-mce,+sha-ni,+adx,+clflushopt,+clwb,+smep,+umip'   # qcmp-20261103-01's like-for-like model


def foreign(me):
    """Foreign VM processes (`vms.sh`): not `me` and not one of its children."""
    found = set()
    for line in subprocess.run(['sh', HERE / 'vms.sh'], capture_output=True, text=True).stdout.splitlines():
        pid, ppid, command = (line.split(None, 2) + [''])[:3]
        if int(pid) != me and int(ppid) != me: found.add(f'{pid} {command.strip()[:160]}')
    return found


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    for name in ('--out', '--name', '--emu', '--vcpus', '--bench', '--kernel', '--initrd'): ap.add_argument(name, required=True)
    ap.add_argument('--jobs')
    ap.add_argument('--ram', type=int, default=2048, help='installed guest RAM in MiB (default: 2048)')
    ap.add_argument('--fexc')
    ap.add_argument('--natemu', default=str(ROOT / 'target/release/natemu'))
    ap.add_argument('--runtime')
    ap.add_argument('--compile-cache')
    ap.add_argument('--cap', type=float, default=600)
    ap.add_argument('--qemu', default='/opt/homebrew/bin/qemu-system-x86_64')
    a = ap.parse_args()
    if a.ram < 2 or a.ram % 2: ap.error('--ram must be a positive multiple of 2 MiB')
    out, name, emu, n, bench, cap = a.out, a.name, a.emu, a.vcpus, a.bench, a.cap
    jobs = a.jobs or n
    cmdline = f'console=ttyS0 earlyprintk=serial mitigations=off nokaslr bench={bench} jobs={jobs}'
    os.makedirs(out, exist_ok=True)
    serial = f'{out}/{name}.serial'
    if os.path.exists(serial): os.remove(serial)
    env = dict(os.environ)
    env.pop('NATEMU_PERF_LOG', None)
    if emu == 'natemu':
        if not a.fexc: ap.error('--emu natemu needs --fexc')
        cmd = [a.natemu, *(['--runtime', a.runtime] if a.runtime else []), '--fexc', a.fexc, '--kernel', a.kernel, '--initrd', a.initrd,
               '--cmdline', cmdline, '--vcpus', n, '--ram', str(a.ram << 20), '--serial', serial, *(['--compile-cache', a.compile_cache] if a.compile_cache else [])]
    else:
        cmd = [a.qemu, '-nodefaults', '-machine', 'pc,hpet=off,i8042=off,vmport=off', '-accel', f"tcg,thread={emu.split('-')[1]}",
               '-cpu', 'max' if emu.endswith('-max') else QCPU, '-m', str(a.ram), '-smp', n, '-kernel', a.kernel, '-initrd', a.initrd, '-append', cmdline,
               '-display', 'none', '-monitor', 'none', '-serial', f'file:{serial}', '-no-reboot']
    t0 = time.monotonic()
    p = subprocess.Popen(cmd, env=env, stdin=subprocess.DEVNULL, stdout=open(f'{out}/{name}.stdout', 'w'), stderr=open(f'{out}/{name}.stderr', 'w'))
    # Trailing spaces distinguish START/START2; timestamps are read ends, not pre-process-query times.
    M = {k: None for k in ('START ', 'END ', 'START2 ', 'END2 ')}; text = ''; others = set(); last = 0.0; seen = [[], []]
    bounds = {}; phase_bounds = [[], []]; previous_read_start = t0

    def read_serial():
        nonlocal text, previous_read_start
        before = time.monotonic()
        try:
            with open(serial, errors='replace') as f: data = f.read()
        except FileNotFoundError:
            return
        after = time.monotonic()
        # A marker may arrive during the previous read; its start is the conservative lower bound.
        bracket = dict(lower_s=previous_read_start, upper_s=after, read_start_s=before)
        previous_read_start = before
        text = data[:data.rfind('\n') + 1]   # defer partial lines until their terminating newline arrives
        for k in M:
            if M[k] is None and 'NATEMU-BENCH-' + k in text:
                M[k] = after; bounds[k] = bracket
        for i, tag in enumerate(('PHASE ', 'PHASE2 ')):
            added = text.count('NATEMU-BENCH-' + tag) - len(seen[i])
            seen[i] += [after] * added; phase_bounds[i] += [bracket] * added

    while time.monotonic() - t0 < cap and p.poll() is None:
        time.sleep(0.05)
        now = time.monotonic()
        if now - last >= 0.5:
            others |= foreign(p.pid); last = now
        read_serial()
    if p.poll() is None:
        p.send_signal(signal.SIGINT)
        try: p.wait(10)
        except subprocess.TimeoutExpired: p.kill(); p.wait()
    read_serial()   # the process can append its last markers just before exiting
    total = time.monotonic() - t0

    def elapsed_bounds(a, b):
        return [b['lower_s'] - a['upper_s'], b['upper_s'] - a['lower_s']] if a is not None and b is not None else None

    def run(sfx, start, end, seen, phase_bounds):
        m = re.search(r'NATEMU-BENCH-END%s \S+ \S+ rc=(\d+) guest_s=([0-9.]+)' % sfx, text)
        marks = [start] + seen
        brackets = [bounds.get('START' + sfx + ' ')] + phase_bounds
        ph = [dict(name=p[0], wall_s=round(seen[i] - marks[i], 3) if start is not None and i < len(seen) else None,
                   wall_bounds_s=elapsed_bounds(brackets[i], phase_bounds[i]), rc=int(p[1]), guest_s=float(p[2]))
              for i, p in enumerate(re.findall(r'NATEMU-BENCH-PHASE%s \S+ \S+ name=(\S+) rc=(\d+) guest_s=([0-9.]+)' % sfx, text))]
        return dict(reached=end is not None, verified=('NATEMU-BENCH-VERIFY%s ok' % sfx) in text and ('NATEMU-BENCH-VERIFY%s FAIL' % sfx) not in text,
                    wall_s=round(end - start, 3) if start is not None and end is not None else None,
                    wall_bounds_s=elapsed_bounds(bounds.get('START' + sfx + ' '), bounds.get('END' + sfx + ' ')), guest_s=float(m.group(2)) if m else None,
                    rc_bench=int(m.group(1)) if m else None, phases=ph)
    r1 = run('', M['START '], M['END '], seen[0], phase_bounds[0]); r2 = run('2', M['START2 '], M['END2 '], seen[1], phase_bounds[1])
    verified = r1['verified'] and r2['verified']
    start = M['START ']
    rec = dict(name=name, emu=emu, vcpus=int(n), ram_mib=a.ram, bench=bench, jobs=int(jobs), reached=r1['reached'] and r2['reached'], verified=verified,
               verified1=r1['verified'], verified2=r2['verified'], wall_s=r1['wall_s'], wall2_s=r2['wall_s'],
               wall_bounds_s=r1['wall_bounds_s'], wall2_bounds_s=r2['wall_bounds_s'], guest_s=r1['guest_s'],
               guest2_s=r2['guest_s'], rc_bench=r1['rc_bench'], rc_bench2=r2['rc_bench'], phases=r1['phases'], phases2=r2['phases'],
               boot_to_start_s=round(start - t0, 3) if start is not None else None,
               timing=dict(clock='monotonic', marker_time='read_end', markers={k.strip(): v for k, v in bounds.items()},
                           phases=phase_bounds[0], phases2=phase_bounds[1]),
               total_s=round(total, 3), cpus=re.findall(r'cpus=(\d+)', text)[:1], contended=sorted(others), rc=p.returncode,
               cmd=[str(c) for c in cmd], stderr=pathlib.Path(f'{out}/{name}.stderr').read_text()[-300:])
    with open(f'{out}/bench.jsonl', 'a') as f: f.write(json.dumps(rec) + '\n')
    print(json.dumps({k: rec[k] for k in ('name', 'verified', 'wall_s', 'wall2_s', 'guest_s', 'guest2_s', 'phases', 'phases2', 'boot_to_start_s', 'total_s', 'contended')}))
    sys.exit(1 if not verified else 3 if others else 0)


if __name__ == '__main__':
    main()
