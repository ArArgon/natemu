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

    tests/boot/run_boot_smoke.py --fexc FEXC --out DIR [--vcpus 1 4] [--kernel BZIMAGE] [--initrd FILE]
        [--timeout SECONDS] [--only i386]

The compile cache is off unless NATEMU_COMPILE_CACHE is set (hermetic). Writes DIR/smoke-N.serial, DIR/smoke.json;
exit 1 on a failure."""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'tests' / 'lib')); import testenv  # noqa: E401,E402 (tests/README.md)
import argparse, json, os, pathlib, signal, subprocess, sys, time
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--fexc', required=True)
    ap.add_argument('--out', required=True, type=pathlib.Path)
    ap.add_argument('--vcpus', type=int, nargs='+', default=[1, 4])
    ap.add_argument('--kernel', default=RAW / 'linux')
    ap.add_argument('--initrd', default=RAW / 'initrd.gz')
    ap.add_argument('--timeout', type=float, default=120)
    ap.add_argument('--only', choices=['i386'])
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault('NATEMU_COMPILE_CACHE', 'off')
    stub, runtime = build()
    results = []
    for n in a.vcpus if not a.only else []:
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
