#!/usr/bin/env python3
"""Library-path boot smoke (R2's blocker check): the Debian bzImage and installer initrd boot through the boot stub
(`target/release/natemu`, i.e. `natemu::linux::Boot::prepare` then `Prepared::run`) with init replaced by
`rdinit=/bin/busybox -- sh -c "echo MARKER; poweroff -f"`. Busybox's write to /dev/console goes through the 8250 driver,
which waits for COM1's transmit interrupt (IRQ 4 through the I/O APIC), so the marker on serial shows device interrupts
reach the vCPUs after `prepare` returned and its value moved (at R2 they went through a dangling pointer and nothing
user space wrote appeared). Pass at each N: the marker on serial and exit 0 (ACPI S5 from `poweroff -f`).
`cancel` (R2 review: the library's `Prepared::canceller`, which the stub wires to SIGINT): the bzImage alone at 4 vCPUs,
SIGINT 1.5 s in (the kernel is booting), to the stub alone and (`cancel-group`) to its whole process group as a terminal's
Ctrl-C (the fexc servers run in their own groups); pass: the stub ends within 5 s of the signal with exit 2 and the stop
`Cancelled`.
`stdin` (`--serial-stdin` from a pipe): the initrd at 1 vCPU with init `rdinit=/bin/busybox -- sh` on ttyS0, standard input
64 newlines (keystrokes before the 8250 port is opened are lost, as on hardware), `echo hello-$((6*7))` and `poweroff -f`;
pass: a serial line ending in `hello-42` (the echoed input reads `hello-$((6*7))`) and exit 0.
`stdin-quit` (review of b55a0f4): the bzImage alone, 64 bytes piped at once (the kernel is booting: nothing reads COM1,
so they wait), Ctrl-A x 1.5 s in with the pipe still open; pass: the stub ends within 5 s of it with exit 2 and `Cancelled`.
`i386` (`--only i386`, M2 task 3b, not part of the default run): tests/boot/i386_static.asm, a small static 32-bit
program (built here with nasm), appended to the initrd as /natemu-i386 and run by busybox at 1 vCPU; it checks its own
results (INT 0x80 and SYSENTER system calls, thread-local data through GS, segment pushes and pops, PUSHAD/POPAD, ARPL,
BOUND, LDS, x87, signals from #BR, #OF and #DE and from kill, sigreturn, a far call); pass: its lines on serial, exit
status 42 and natemu's exit 0.
`startup` (`--only startup`, M2 Part 2's CPU start-up slice: boot-interpreter plan sections 6 and 8): the bzImage and
initrd through the program driver's `boot-linux` (`--host`, `--runtime`: the framework's build), at each `--vcpus` N
(default 2 and 4), with the ACPI wakeup mailbox (the default) and without it (`--no-mailbox`: each AP waits for INIT and
SIPI, and Linux starts it through its real-mode trampoline, which the boot-time interpreter runs). Busybox mounts /proc
and /sys and prints, as `natemu-KEY=VALUE` lines: the guest's uptime, the CPUs /proc/cpuinfo lists and the online mask;
for each AP n, the online mask after `echo 0` and after `echo 1` to its `online` file (`refused` when the write fails:
Linux disables offlining with a version-0 mailbox, since it cannot start a CPU twice through one); the CPUs listed at
the end; and how many times the kernel took its warm-reset path (`smpboot: Setting warm reset code and vector.`, a
pr_debug turned on by `dyndbg`: once per AP start, each writing CMOS 0FH and the real-mode vector at 467H and restoring
them once the AP is up), then `poweroff -f`. Pass per boot: every CPU online at boot and at the end; without the
mailbox each AP offline and online again, with it offlining refused; the warm-reset path once per AP start; the run
ended by the guest's ACPI S5 (`poweroff`); each AP where Linux 6.12's shutdown leaves it (`native_stop_other_cpus`:
the reboot vector, then `stop_this_cpu`'s HLT loop with IF = 0; 6.12 parks no CPU with INIT), reported `halted`.
Each boot's run time (the driver's `stats.seconds`, VM start to S5), the guest's uptime and the CMOS ports' (70H, 71H)
access counts are reported: 71H's writes grow by two for each warm-reset setup and its restore (0AH, then 0, into 0FH;
natemu's CMOS keeps 0FH as a plain byte, as QEMU's does, and 467H is guest RAM).
`--shadow-pool-pages N` (with `startup`): each vCPU's shadow-table pool is N pages (boot-linux's option; the default, the
whole 16,384-page reservation, never fills), and the script also runs tests/boot/sparse_static.asm (built here with
nasm, appended to the initrd as /natemu-sparse): one page in each of 300 regions 1 GiB apart, stored and read back three
times, which needs about 600 shadow table pages a pass. Each boot must then also show `natemu-sparse-rc=42` and
evictions on some vCPU (the driver's per-vCPU `evictions`): the full-pool reset runs on a real SMP boot
(`boot-startup-small-pool`, N = 285, the least a launch may set; busybox alone never fills it).

    tests/boot/run_boot_smoke.py --fexc FEXC --out DIR [--vcpus 1 4] [--kernel BZIMAGE] [--initrd FILE]
        [--timeout SECONDS] [--only i386]
    tests/boot/run_boot_smoke.py --only startup --host HOST --runtime RUNTIME --fexc FEXC --out DIR [--vcpus 2 4]
        [--shadow-pool-pages N]

The compile cache is off unless NATEMU_COMPILE_CACHE is set (hermetic). Writes DIR/smoke-N.serial, DIR/smoke.json;
exit 1 on a failure."""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'tests' / 'lib')); import testenv  # noqa: E401,E402 (tests/README.md)
import argparse, json, os, pathlib, re, signal, subprocess, sys, time
from raw_root import RAW_ROOT

ROOT = pathlib.Path(__file__).resolve().parents[2]
RAW = RAW_ROOT / 'kernel-census-20261028-01/dl'
MARKER = 'natemu-boot-smoke-ok'
CMDLINE = f'console=ttyS0 earlyprintk=serial mitigations=off nokaslr rdinit=/bin/busybox -- sh -c "echo {MARKER}; poweroff -f"'


def build():
    """The stub alone (`-p natemu-cli`: no `testing` feature) and the resident; the stub signed with the entitlements."""
    cargo = ['cargo', '+1.97.0', 'build', '--offline', '--locked', '--release']
    subprocess.run(cargo + ['-p', 'natemu-cli', '--bin', 'natemu'], cwd=ROOT, check=True)
    subprocess.run(cargo + ['-p', 'natemu-runtime', '--target', 'aarch64-unknown-none'], cwd=ROOT, check=True)
    target = pathlib.Path(os.environ.get('CARGO_TARGET_DIR', ROOT / 'target'))
    stub = target / 'release/natemu'
    subprocess.run(['codesign', '--force', '--sign', '-', '--entitlements', ROOT / 'crates/host/entitlements.plist', stub], check=True)
    return stub, target / 'aarch64-unknown-none/release/natemu-runtime'


def boot(stub, runtime, a, n):
    serial = a.out / f'smoke-{n}.serial'
    cmd = [stub, '--runtime', runtime, '--fexc', a.fexc, '--kernel', a.kernel, '--initrd', a.initrd, '--cmdline', CMDLINE, '--vcpus', str(n), '--serial', serial]
    t0 = time.monotonic()
    p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    try:
        _, err = p.communicate(timeout=a.timeout)
    except subprocess.TimeoutExpired:
        p.send_signal(signal.SIGINT)
        try:
            _, err = p.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            p.kill()
            _, err = p.communicate()
    text = serial.read_text(errors='replace') if serial.exists() else ''
    # The marker as busybox prints it: on a line of its own (the command line echoed in the kernel log is not).
    printed = any(line.strip() == MARKER for line in text.splitlines())
    return dict(vcpus=n, rc=p.returncode, marker=printed, ok=printed and p.returncode == 0,
                wall_s=round(time.monotonic() - t0, 2), stop=err.strip().splitlines()[-1:] if err else [])


def cancel(stub, runtime, a, group=False):
    """`group`: SIGINT to the stub's whole process group, as a terminal's Ctrl-C does (the fexc servers must not get it)."""
    name = 'cancel-group' if group else 'cancel'
    serial = a.out / f'{name}.serial'
    cmd = [stub, '--runtime', runtime, '--fexc', a.fexc, '--kernel', a.kernel, '--cmdline', 'console=ttyS0 earlyprintk=serial mitigations=off nokaslr', '--vcpus', '4',
           '--serial', serial]
    p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, start_new_session=group)
    time.sleep(1.5)
    running = p.poll() is None
    t0 = time.monotonic()
    if group:
        os.killpg(p.pid, signal.SIGINT)
    else:
        p.send_signal(signal.SIGINT)
    try:
        _, err = p.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        p.kill()
        _, err = p.communicate()
    stop = err.strip().splitlines()[-1:] if err else []
    sent = serial.stat().st_size if serial.exists() else 0
    ok = running and p.returncode == 2 and stop == ['natemu: Cancelled'] and sent > 0
    return dict(case=name, vcpus=4, rc=p.returncode, serial_bytes=sent, ok=ok, after_signal_s=round(time.monotonic() - t0, 2), stop=stop)


def stdin_quit(stub, runtime, a):
    serial = a.out / 'stdin-quit.serial'
    cmd = [stub, '--runtime', runtime, '--fexc', a.fexc, '--kernel', a.kernel, '--cmdline', 'console=ttyS0 earlyprintk=serial mitigations=off nokaslr', '--serial', serial,
           '--serial-stdin']
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    p.stdin.write('a' * 64)
    p.stdin.flush()
    time.sleep(1.5)
    running = p.poll() is None
    t0 = time.monotonic()
    p.stdin.write('\x01x')
    p.stdin.flush()
    try:
        rc = p.wait(timeout=5)
    except subprocess.TimeoutExpired:
        p.kill()
        rc = p.wait()
    err = p.stderr.read()
    p.stdin.close()
    stop = err.strip().splitlines()[-1:] if err else []
    ok = running and rc == 2 and stop == ['natemu: Cancelled']
    return dict(case='stdin-quit', vcpus=1, rc=rc, ok=ok, after_escape_s=round(time.monotonic() - t0, 2), stop=stop)


def stdin(stub, runtime, a):
    serial = a.out / 'stdin.serial'
    cmdline = 'console=ttyS0 earlyprintk=serial mitigations=off nokaslr rdinit=/bin/busybox -- sh'
    cmd = [stub, '--runtime', runtime, '--fexc', a.fexc, '--kernel', a.kernel, '--initrd', a.initrd, '--cmdline', cmdline, '--serial', serial, '--serial-stdin']
    t0 = time.monotonic()
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    try:
        _, err = p.communicate('\n' * 64 + 'echo hello-$((6*7))\npoweroff -f\n', timeout=a.timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        _, err = p.communicate()
    text = serial.read_text(errors='replace') if serial.exists() else ''
    printed = any(line.rstrip().endswith('hello-42') for line in text.splitlines())
    return dict(case='stdin', vcpus=1, rc=p.returncode, marker=printed, ok=printed and p.returncode == 0,
                wall_s=round(time.monotonic() - t0, 2), stop=err.strip().splitlines()[-1:] if err else [])


def newc(files):
    """An uncompressed initramfs (cpio newc) of `files`: (name, mode, bytes)."""
    out = b''
    for k, (name, mode, data) in enumerate(list(files) + [('TRAILER!!!', 0, b'')]):
        fields = [k + 1, mode, 0, 0, 1, 0, len(data), 0, 0, 0, 0, len(name) + 1, 0]
        head = b'070701' + b''.join(b'%08X' % f for f in fields) + name.encode() + b'\0'
        out += head + bytes(-len(head) % 4) + data + bytes(-len(data) % 4)
    return out


def i386(stub, runtime, a):
    prog = a.out / 'natemu-i386'
    subprocess.run(['nasm', '-f', 'bin', '-o', prog, ROOT / 'tests/boot/i386_static.asm'], check=True)
    initrd = a.out / 'initrd-i386'
    base = pathlib.Path(a.initrd).read_bytes()
    initrd.write_bytes(base + bytes(-len(base) % 4) + newc([('natemu-i386', 0o100755, prog.read_bytes())]))
    serial = a.out / 'i386.serial'
    cmdline = ('console=ttyS0 earlyprintk=serial mitigations=off nokaslr rdinit=/bin/busybox -- sh -c '
               '"/natemu-i386; echo natemu-i386-rc=$?; poweroff -f"')
    cmd = [stub, '--runtime', runtime, '--fexc', a.fexc, '--kernel', a.kernel, '--initrd', initrd, '--cmdline', cmdline, '--serial', serial]
    t0 = time.monotonic()
    p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    try:
        _, err = p.communicate(timeout=a.timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        _, err = p.communicate()
    lines = [line.strip() for line in (serial.read_text(errors='replace') if serial.exists() else '').splitlines()]
    want = ['natemu-i386: int80', 'natemu-i386: sysenter', 'natemu-i386-ok', 'natemu-i386-rc=42']
    seen = [w for w in want if w in lines]
    return dict(case='i386', vcpus=1, rc=p.returncode, seen=seen, ok=seen == want and p.returncode == 0,
                wall_s=round(time.monotonic() - t0, 2), stop=err.strip().splitlines()[-1:] if err else [],
                last=[line for line in lines if line][-3:])


# `startup`: busybox's script (one argument of `sh -c`: no double quote inside) and the kernel's dynamic debug for the
# warm-reset message. (`[w]arm`: the kernel's log holds the command line too, which this pattern does not match.)
STARTUP_SCRIPT = ('mount -t proc proc /proc; mount -t sysfs sys /sys; read up rest < /proc/uptime; echo natemu-uptime=$up; '
                  'echo natemu-cpus=$(grep -c ^processor /proc/cpuinfo) natemu-online=$(cat /sys/devices/system/cpu/online); '
                  'for c in /sys/devices/system/cpu/cpu[1-9]*; do n=${c##*cpu}; '
                  'if echo 0 > $c/online; then echo natemu-off-$n=$(cat /sys/devices/system/cpu/online); else echo natemu-off-$n=refused; fi; '
                  'if echo 1 > $c/online; then echo natemu-on-$n=$(cat /sys/devices/system/cpu/online); else echo natemu-on-$n=refused; fi; '
                  'done; echo natemu-cpus-end=$(grep -c ^processor /proc/cpuinfo); '
                  "echo natemu-warm-reset=$(dmesg | grep -c 'Setting [w]arm reset code'); echo natemu-script=done; poweroff -f")
STARTUP_CMDLINE = ('console=ttyS0 earlyprintk=serial mitigations=off nokaslr dyndbg="file arch/x86/kernel/smpboot.c +p" '
                   f'rdinit=/bin/busybox -- sh -c "{STARTUP_SCRIPT}"')


def cpu_list(cpus):
    """Linux's form of a CPU mask (`0-1`, `0,2-3`)."""
    out, cpus = [], sorted(cpus)
    while cpus:
        lo = hi = cpus.pop(0)
        while cpus and cpus[0] == hi + 1:
            hi = cpus.pop(0)
        out.append(f'{lo}' if lo == hi else f'{lo}-{hi}')
    return ','.join(out)


def startup_fields(text):
    """The script's `natemu-KEY=VALUE` words on serial (the kernel's echo of its command line, whose words hold `$`, is
    not one)."""
    return {k: v for k, v in re.findall(r'natemu-([a-z0-9-]+)=([^\s$;()]+)(?=\s|$)', text)}


def startup_judge(n, mailbox, rep, fields):
    """The differences of one `startup` boot from what it must show (module doc)."""
    every = cpu_list(range(n))
    want = {'cpus': str(n), 'online': every, 'cpus-end': str(n), 'script': 'done'}
    for c in range(1, n):
        want[f'off-{c}'] = 'refused' if mailbox else cpu_list(set(range(n)) - {c})
        want[f'on-{c}'] = every
    # do_boot_cpu takes the warm-reset path at every AP start (smpboot.c, x86_platform.legacy.warm_reset), mailbox or not.
    want['warm-reset'] = str((n - 1) * (1 if mailbox else 2))
    diffs = [f'{k}: {fields.get(k)!r}, want {v!r}' for k, v in want.items() if fields.get(k) != v]
    if rep.get('stop') != 'poweroff':
        diffs.append(f"run ended {rep.get('stop')!r} {rep.get('reason', '')!r}, want poweroff")
    for v, c in enumerate(rep.get('vcpus', [])[1:], 1):
        if c.get('waiting') != 'halted':
            diffs.append(f"vCPU {v}: waiting {c.get('waiting')!r} at the end (stop {c.get('stop')!r}), want halted (stop_this_cpu)")
    return diffs


def startup(a, n, mailbox):
    """One `startup` boot: the driver's report, the script's fields, the judge's differences, the run's time."""
    pool = a.shadow_pool_pages
    name = f"startup-{'mailbox' if mailbox else 'no-mailbox'}-n{n}{f'-pool{pool}' if pool else ''}"
    serial = a.out / f'{name}.serial'
    initrd, cmdline = a.initrd, STARTUP_CMDLINE
    if pool:
        # The sparse program (tests/boot/sparse_static.asm) in the initrd, run by the script before its end.
        prog = a.out / 'natemu-sparse'
        subprocess.run(['nasm', '-f', 'bin', '-o', prog, ROOT / 'tests/boot/sparse_static.asm'], check=True)
        initrd = a.out / 'initrd-sparse'
        base = pathlib.Path(a.initrd).read_bytes()
        initrd.write_bytes(base + bytes(-len(base) % 4) + newc([('natemu-sparse', 0o100755, prog.read_bytes())]))
        cmdline = STARTUP_CMDLINE.replace('echo natemu-script=done;', '/natemu-sparse; echo natemu-sparse-rc=$?; echo natemu-script=done;')
        assert cmdline != STARTUP_CMDLINE
    cmd = [a.host, a.runtime, 'boot-linux', a.fexc, a.kernel, '--initrd', initrd, '--cmdline', cmdline,
           '--vcpus', str(n), '--serial', serial, *([] if mailbox else ['--no-mailbox']),
           *(['--shadow-pool-pages', str(pool)] if pool else [])]
    t0 = time.monotonic()
    try:
        r = subprocess.run([str(c) for c in cmd], capture_output=True, text=True, timeout=a.timeout)
        out, err, rc = r.stdout, r.stderr, r.returncode
    except subprocess.TimeoutExpired as e:
        text = lambda b: b.decode(errors='replace') if isinstance(b, bytes) else b or ''
        out, err, rc = text(e.stdout), text(e.stderr), 'timeout'
    wall = round(time.monotonic() - t0, 2)
    (a.out / f'{name}.json').write_text(out)
    (a.out / f'{name}.stderr').write_text(err)
    try:
        rep = json.loads(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        rep = {'stop': f'driver exit {rc}', 'reason': err.strip()[-400:]}
    text = serial.read_text(errors='replace') if serial.exists() else ''
    fields = startup_fields(text)
    diffs = startup_judge(n, mailbox, rep, fields)
    # A small pool (`--shadow-pool-pages`): the sparse program read back every page, and its vCPU filled the pool (the
    # full reset ran) while the boot still passed.
    evictions = [c.get('evictions') for c in rep.get('vcpus', [])]
    if pool:
        if fields.get('sparse-rc') != '42':
            diffs.append(f"sparse-rc: {fields.get('sparse-rc')!r}, want '42' (tests/boot/sparse_static.asm)")
        if not sum(e or 0 for e in evictions):
            diffs.append(f'evictions {evictions!r} with a pool of {pool} pages, want some (the full-pool reset untested)')
    cmos = {x['port']: [x['reads'], x['writes']] for x in rep.get('ports', []) if x['port'] in ('0x70', '0x71')}
    return dict(case=name, vcpus=n, mailbox=mailbox, ok=not diffs, diffs=diffs, wall_s=wall, evictions=evictions,
                run_s=rep.get('stats', {}).get('seconds'), uptime_s=fields.get('uptime'),
                waiting=[c.get('waiting') for c in rep.get('vcpus', [])], cmos_ports=cmos, fields=fields,
                last=[line for line in text.splitlines() if line.strip()][-5:] if diffs else [])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--fexc', required=True)
    ap.add_argument('--out', required=True, type=pathlib.Path)
    ap.add_argument('--vcpus', type=int, nargs='+', help='default 1 4 (startup: 2 4)')
    ap.add_argument('--kernel', default=RAW / 'linux')
    ap.add_argument('--initrd', default=RAW / 'initrd.gz')
    ap.add_argument('--timeout', type=float, default=120)
    ap.add_argument('--only', choices=['i386', 'startup'])
    ap.add_argument('--host', help='startup: the program driver (natemu-host)')
    ap.add_argument('--runtime', help='startup: the resident')
    ap.add_argument('--shadow-pool-pages', type=int, help="startup: each vCPU's shadow pool in pages (boot-linux's); each "
                    'boot must then also have filled it at least once (vCPU 0\'s evictions)')
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault('NATEMU_COMPILE_CACHE', 'off')
    if a.only == 'startup':
        if not (a.host and a.runtime): raise SystemExit('--only startup needs --host and --runtime')
        results = []
        for n in a.vcpus or [2, 4]:
            for mailbox in (True, False):
                r = startup(a, n, mailbox)
                print(json.dumps({k: v for k, v in r.items() if k != 'fields'}), flush=True)
                results.append(r)
        (a.out / 'smoke.json').write_text(json.dumps(results, indent=1) + '\n')
        (a.out / 'results.json').write_text(json.dumps({'results': [
            {'name': r['case'], 'outcome': 'match' if r['ok'] else 'fail', 'diffs': r['diffs']} for r in results]}, indent=1) + '\n')
        for r in results:
            print(f"{r['case']}: {'pass' if r['ok'] else 'FAIL ' + '; '.join(r['diffs'])} (run {r['run_s']} s, "
                  f"guest uptime {r['uptime_s']} s at the script, wall {r['wall_s']} s, APs {r['waiting'][1:]})", flush=True)
        sys.exit(0 if all(r['ok'] for r in results) else 1)
    stub, runtime = build()
    results = []
    for n in (a.vcpus or [1, 4]) if not a.only else []:
        r = boot(stub, runtime, a, n)
        print(json.dumps(r), flush=True)
        results.append(r)
    for group in (False, True) if not a.only else ():
        r = cancel(stub, runtime, a, group)
        print(json.dumps(r), flush=True)
        results.append(r)
    for case in (stdin, stdin_quit) if not a.only else (i386,):
        r = case(stub, runtime, a)
        print(json.dumps(r), flush=True)
        results.append(r)
    (a.out / 'smoke.json').write_text(json.dumps(results, indent=1) + '\n')
    (a.out / 'results.json').write_text(json.dumps({'results': [   # per-case rows for the framework
        {'name': f"{r.get('case', 'boot')}-n{r['vcpus']}", 'outcome': 'match' if r['ok'] else 'fail', 'diffs': [] if r['ok'] else [json.dumps(r)]}
        for r in results]}, indent=1) + '\n')
    ok = all(r['ok'] for r in results)
    print(f'boot smoke: {sum(r["ok"] for r in results)}/{len(results)} ' + ('ok' if ok else 'FAILED'))
    sys.exit(0 if ok else 1)


if __name__ == '__main__':
    testenv.require_framework(__file__)
    main()
