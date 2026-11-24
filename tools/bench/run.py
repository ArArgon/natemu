#!/usr/bin/env python3
"""One benchmark boot of the bench initramfs, natemu or QEMU (from natemu-raw/bench-20261105-03/scripts/bench.py; the
measurement, verification and record are that script's, natemu now runs as the `natemu` stub with named arguments).

    python3 tools/bench/run.py --out DIR --name NAME --emu natemu|qemu-multi|qemu-single|qemu-multi-max --vcpus N
        --bench BENCH --kernel BZIMAGE --initrd CPIO [--jobs J] [--fexc FEXC] [--natemu STUB] [--runtime FILE]
        [--compile-cache DIR|off] [--cap SECONDS] [--qemu QEMU] [--append WORDS] [--perf-log] [--natemu-set KEY=VALUE ...]
        [--sample-us N]

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
is `--compile-cache` when given, else the stub's default (its NATEMU_COMPILE_CACHE fallback included); each
`--natemu-set KEY=VALUE` is passed on as `--set KEY=VALUE`. QEMU: TCG
thread=multi or single, the board and CPU model of natemu-raw/qcmp-20261103-01 (`-max`: `-cpu max`). JOBS defaults to
VCPUS. The guest powers off after its last VERIFY line; the whole boot (both runs) is capped at `--cap` s (default 600): a
capped boot is recorded (`capped`, `capped_in`: the run it was in, `at_least_s`: how long that run had lasted). Exit 0 verified
(or QEMU capped: a result, "> cap"), 1 not reached or not verified (natemu capped included), 3 contended. A QEMU record
names its version, machine type and CPU model (`qemu`). Appends one JSON line to DIR/bench.jsonl; serial to
DIR/NAME.serial. Run it under the VM lock (`/usr/bin/lockf -k .../natemu-raw/.vm.lock`) with no other VM running.

`--sample-us N` (natemu; a profile, not a timing: pass a resident built with its `perf` and `profile` features as --runtime)
samples where each vCPU is every N us (natemu `--profile DIR/NAME --profile-us N`: DIR/NAME.samples and DIR/NAME.objects,
docs/restructure/el0-sampler-plan.md), adds `natemu.profile=1` to the kernel command line (the guest's sign that the control
port is there) and implies --perf-log. With --perf-log, every NATEMU-BENCH line is stamped on arrival in DIR/NAME.markers.tsv
(CLOCK_UPTIME_RAW ns, the timing log's and the samples' clock, then the line), from which tests/bench/sample_report.py cuts the
boot into phases.
"""
import argparse, json, os, pathlib, re, signal, subprocess, sys, time

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
HERE = pathlib.Path(__file__).resolve().parent
QMACHINE = 'pc-i440fx-11.1'   # what `pc` named in QEMU 11.1.2 (the November 14 round); pinned, as tests/io/run_io5.py's
QCPU = 'Haswell,-pcid,-hle,-rtm,-invpcid,-tsc-deadline,-erms,-mca,-mce,+sha-ni,+adx,+clflushopt,+clwb,+smep,+umip'   # qcmp-20261103-01's like-for-like model


def foreign(me):
    """Foreign VM processes (`vms.sh`): not `me` and not one of its children."""
    found = set()
    for line in subprocess.run(['sh', HERE / 'vms.sh'], capture_output=True, text=True).stdout.splitlines():
        pid, ppid, command = (line.split(None, 2) + [''])[:3]
        if int(pid) != me and int(ppid) != me: found.add(f'{pid} {command.strip()[:160]}')
    return found


def fp_traps(stderr):
    """Summed over vCPUs: traps, steps and `hist` (cause * 8 + class; causes ufe, ioe, other, fma-check; classes add, mul, div, fma,
    cvt, fcmp, fcvtz, other) as {cause: {class: n}} with only the non-zero cells; None if natemu printed no line (no FP trap counts)."""
    lines = re.findall(r'natemu-fp-traps vcpu \d+ traps (\d+) steps (\d+) hist \[([\d, ]+)\]', stderr)
    if not lines: return None
    hist = [sum(int(l[2].split(',')[i]) for l in lines) for i in range(32)]
    names = ['ufe', 'ioe', 'other', 'check']; classes = ['add', 'mul', 'div', 'fma', 'cvt', 'fcmp', 'fcvtz', 'other']
    return dict(traps=sum(int(l[0]) for l in lines), steps=sum(int(l[1]) for l in lines),
                by={names[c]: {classes[i % 8]: hist[i] for i in range(8 * c, 8 * c + 8) if hist[i]} for c in range(4)})


def system_counts(stderr):
    """Summed over vCPUs: the system instructions the resident ran, by kind (hex, natemu_abi::execution::system), the stack
    alignment check's firings (`stack`) and the linker entries through a split block's cold code (`cold_exits`, split-check
    plan step A; None from a natemu that does not print it); None if natemu printed no line."""
    lines = re.findall(r'natemu-system-counts vcpu \d+ stack (\d+) kinds (\S*)(?: cold_exits (\d+))?', stderr)
    if not lines: return None
    kinds = {}
    for _, ks, _ in lines:
        for kv in filter(None, ks.split(',')):
            k, v = kv.split(':'); kinds[k] = kinds.get(k, 0) + int(v)
    cold = [int(l[2]) for l in lines if l[2]]
    return dict(stack=sum(int(l[0]) for l in lines), kinds=kinds, cold_exits=sum(cold) if len(cold) == len(lines) else None)


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
    ap.add_argument('--perf-log', action='store_true', help='natemu: write its timing log to OUT/NAME.perf (a profile, not a timing: pass a `perf` resident as --runtime)')
    ap.add_argument('--append', default='', help='more words for the kernel command line (e.g. vdso32=0: 32-bit programs then make their system calls by INT 0x80)')
    ap.add_argument('--qemu', default='/opt/homebrew/bin/qemu-system-x86_64')
    ap.add_argument('--natemu-set', action='append', default=[], metavar='KEY=VALUE', help='natemu: passed on as --set KEY=VALUE (the bench harness turns the platform devices off with it)')
    ap.add_argument('--sample-us', type=int, help='natemu: sample every vCPU every N us into OUT/NAME.samples and OUT/NAME.objects (a profile; implies --perf-log; pass a `perf,profile` resident as --runtime)')
    ap.add_argument('--sample-no-objects', action='store_true', help='with --sample-us: natemu writes no answers to OUT/NAME.objects (--profile-objects=false: what writing them costs)')
    a = ap.parse_args()
    if a.sample_us is not None:
        if a.emu != 'natemu' or a.sample_us < 1: ap.error('--sample-us N: natemu only, N at least 1')
        a.perf_log = True
    if a.ram < 2 or a.ram % 2: ap.error('--ram must be a positive multiple of 2 MiB')
    out, name, emu, n, bench, cap = a.out, a.name, a.emu, a.vcpus, a.bench, a.cap
    jobs = a.jobs or n
    cmdline = f'console=ttyS0 earlyprintk=serial mitigations=off nokaslr bench={bench} jobs={jobs}' + (' ' + a.append if a.append else '') + \
        (' natemu.profile=1' if a.sample_us else '')
    os.makedirs(out, exist_ok=True)
    serial = f'{out}/{name}.serial'
    if os.path.exists(serial): os.remove(serial)
    env = dict(os.environ)
    env.pop('NATEMU_PERF_LOG', None)
    env['NATEMU_FP_TRAP_COUNTS'] = '1'   # natemu prints its trapped-FP counts on stderr at the end (whole boot, both runs)
    if emu == 'natemu':
        if not a.fexc: ap.error('--emu natemu needs --fexc')
        cmd = [a.natemu, *(['--runtime', a.runtime] if a.runtime else []), '--fexc', a.fexc, '--kernel', a.kernel, '--initrd', a.initrd,
               '--cmdline', cmdline, '--vcpus', n, '--ram', str(a.ram << 20), '--serial', serial, *(['--compile-cache', a.compile_cache] if a.compile_cache else []), *(['--perf-log', f'{out}/{name}.perf'] if a.perf_log else []),
               *(w for kv in a.natemu_set for w in ('--set', kv)),
               *(['--profile', f'{out}/{name}', '--profile-us', str(a.sample_us)] if a.sample_us else []),
               *(['--profile-objects=false'] if a.sample_us and a.sample_no_objects else [])]
    else:
        cmd = [a.qemu, '-nodefaults', '-machine', f'{QMACHINE},hpet=off,i8042=off,vmport=off', '-accel', f"tcg,thread={emu.split('-')[1]}",
               '-cpu', 'max' if emu.endswith('-max') else QCPU, '-m', str(a.ram), '-smp', n, '-kernel', a.kernel, '-initrd', a.initrd, '-append', cmdline,
               '-display', 'none', '-monitor', 'none', '-serial', f'file:{serial}', '-no-reboot']
    t0 = time.monotonic()
    p = subprocess.Popen(cmd, env=env, stdin=subprocess.DEVNULL, stdout=open(f'{out}/{name}.stdout', 'w'), stderr=open(f'{out}/{name}.stderr', 'w'))
    # Trailing spaces distinguish START/START2; timestamps are read ends, not pre-process-query times.
    M = {k: None for k in ('START ', 'END ', 'START2 ', 'END2 ')}; text = ''; others = set(); last = 0.0; seen = [[], []]
    bounds = {}; phase_bounds = [[], []]; previous_read_start = t0
    # Profile runs (--perf-log, --sample-us): each NATEMU-BENCH line's arrival on the timing log's and the samples' clock
    # (CLOCK_UPTIME_RAW ns), which cut them into phases.
    stamps = open(f'{out}/{name}.markers.tsv', 'w') if a.perf_log else None; stamped = 0

    def read_serial():
        nonlocal text, previous_read_start, stamped
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
        if stamps:
            lines = text.splitlines(); ns = time.clock_gettime_ns(time.CLOCK_UPTIME_RAW)
            for line in lines[stamped:]:
                if 'NATEMU-BENCH-' in line: stamps.write(f'{ns}\t{line[line.index("NATEMU-BENCH-"):].strip()}\n')
            stamped = len(lines); stamps.flush()
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
    capped = p.poll() is None      # the cap is on the whole boot (both runs), not on one run
    capped_at = time.monotonic()
    if capped:
        p.send_signal(signal.SIGINT)
        try: p.wait(10)
        except subprocess.TimeoutExpired: p.kill(); p.wait()
    read_serial()   # the process can append its last markers just before exiting
    total = time.monotonic() - t0
    if stamps: stamps.close()

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
    if a.sample_us: rec['sample_us'] = a.sample_us
    rec['fp_traps'] = fp_traps(pathlib.Path(f'{out}/{name}.stderr').read_text())
    rec['system_counts'] = system_counts(pathlib.Path(f'{out}/{name}.stderr').read_text())
    if emu != 'natemu':
        rec['qemu'] = dict(version=subprocess.run([a.qemu, '--version'], capture_output=True, text=True).stdout.splitlines()[0], machine=QMACHINE, cpu=cmd[cmd.index('-cpu') + 1])
    # A boot stopped by the cap: which run it was in and how long that run had lasted, a lower bound on its time ("> cap").
    rec['capped'] = capped; rec['cap_s'] = cap
    if capped:
        k = 'START2 ' if M['START2 '] is not None else 'START '
        rec['capped_in'] = None if M[k] is None else 'run 2' if k == 'START2 ' else 'run 1'
        rec['at_least_s'] = None if M[k] is None else round(capped_at - M[k], 1)
    rec['outputs'] = re.findall(r'NATEMU-BENCH-OUTPUT\S* (.*)', text)    # what a benchmark without a pinned hash printed
    with open(f'{out}/bench.jsonl', 'a') as f: f.write(json.dumps(rec) + '\n')
    print(json.dumps({k: rec.get(k) for k in ('name', 'verified', 'capped', 'capped_in', 'at_least_s', 'wall_s', 'wall2_s', 'guest_s', 'guest2_s', 'phases', 'phases2', 'boot_to_start_s', 'total_s', 'contended')}))
    # A QEMU baseline too slow for its cap is a result (recorded above), not a failure: if its benchmark had started and no
    # run that ended failed its verification. A boot that never reached START, or a failed run 1, is a failure as ever.
    if capped and emu != 'natemu' and M['START '] is not None and (not r1['reached'] or r1['verified']):
        sys.exit(3 if others else 0)
    sys.exit(1 if not verified else 3 if others else 0)


if __name__ == '__main__':
    main()
