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

    tools/with-env python3 tools/run_boot_smoke.py --fexc FEXC --out DIR [--vcpus 1 4] [--kernel BZIMAGE] [--initrd FILE]
        [--timeout SECONDS]

The compile cache is off unless NATEMU_COMPILE_CACHE is set (hermetic). Writes DIR/smoke-N.serial, DIR/smoke.json;
exit 1 on a failure."""
import argparse, json, os, pathlib, signal, subprocess, sys, time

ROOT = pathlib.Path(__file__).resolve().parent.parent
RAW = pathlib.Path(os.environ.get('NATEMU_RAW_ROOT', ROOT.parent / 'natemu-raw')) / 'kernel-census-20261028-01/dl'
MARKER = 'natemu-boot-smoke-ok'
CMDLINE = f'console=ttyS0 earlyprintk=serial mitigations=off nokaslr rdinit=/bin/busybox -- sh -c "echo {MARKER}; poweroff -f"'


def build():
    """The stub alone (`-p natemu`: no `testing` feature) and the resident; the stub signed with the entitlements."""
    cargo = ['cargo', '+1.97.0', 'build', '--offline', '--locked', '--release']
    subprocess.run(cargo + ['-p', 'natemu', '--bin', 'natemu'], cwd=ROOT, check=True)
    subprocess.run(cargo + ['-p', 'natemu-runtime', '--target', 'aarch64-unknown-none'], cwd=ROOT, check=True)
    target = pathlib.Path(os.environ.get('CARGO_TARGET_DIR', ROOT / 'target'))
    stub = target / 'release/natemu'
    subprocess.run(['codesign', '--force', '--sign', '-', '--entitlements', ROOT / 'crates/host/entitlements.plist', stub], check=True)
    return stub, target / 'aarch64-unknown-none/release/natemu-runtime'


def boot(stub, runtime, a, n):
    serial = a.out / f'smoke-{n}.serial'
    cmd = [stub, runtime, a.fexc, a.kernel, '--initrd', a.initrd, '--cmdline', CMDLINE, '--vcpus', str(n), '--serial', serial]
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
    cmd = [stub, runtime, a.fexc, a.kernel, '--cmdline', 'console=ttyS0 earlyprintk=serial mitigations=off nokaslr', '--vcpus', '4',
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--fexc', required=True)
    ap.add_argument('--out', required=True, type=pathlib.Path)
    ap.add_argument('--vcpus', type=int, nargs='+', default=[1, 4])
    ap.add_argument('--kernel', default=RAW / 'linux')
    ap.add_argument('--initrd', default=RAW / 'initrd.gz')
    ap.add_argument('--timeout', type=float, default=120)
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault('NATEMU_COMPILE_CACHE', 'off')
    stub, runtime = build()
    results = []
    for n in a.vcpus:
        r = boot(stub, runtime, a, n)
        print(json.dumps(r), flush=True)
        results.append(r)
    for group in (False, True):
        r = cancel(stub, runtime, a, group)
        print(json.dumps(r), flush=True)
        results.append(r)
    (a.out / 'smoke.json').write_text(json.dumps(results, indent=1) + '\n')
    ok = all(r['ok'] for r in results)
    print(f'boot smoke: {sum(r["ok"] for r in results)}/{len(results)} ' + ('ok' if ok else 'FAILED'))
    sys.exit(0 if ok else 1)


if __name__ == '__main__':
    main()
