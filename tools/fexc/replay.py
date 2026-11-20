#!/usr/bin/env python3
"""The replay gate (tools/fexc/README.md, "Replay gate") as one check: a change to fexc, the shim, the FEX fork or the
feature tables that must not change 64-bit output is shown by sending the requests a boot made to the compiler before the
change and to the one after it, and comparing every reply whole (status, report, object, extent, args_read).

    replay.py --fexc NEW --old OLD --stub NATEMU --runtime RESIDENT --kernel BZIMAGE --initrd FILE --out DIR
    replay.py --fexc NEW --old OLD --stream FILE              (a stream recorded earlier, NATEMU_FEXC_RECORD's format)

Without --stream it records one first: the Debian kernel and installer initrd booted at 1 vCPU to a busybox `poweroff -f`
(run_boot_smoke.py's boot) with the persistent cache off, so every request reaches the compiler, under
NATEMU_FEXC_RECORD (the host appends each request body as it posts it: u32 length, then the body). Both compilers then
answer the stream as `fexc --serve` over the pipe (the channel transport has its own check, test_channel.py).
Exit 1 unless the stream holds requests and no reply differs; OLD and NEW the same binary is the self-check. The stream is
several hundred MiB: it stays in DIR, which the framework deletes after a pass."""
import argparse, os, pathlib, struct, subprocess, sys

MARKER = 'natemu-replay-recorded'
CMDLINE = f'console=ttyS0 earlyprintk=serial mitigations=off nokaslr rdinit=/bin/busybox -- sh -c "echo {MARKER}; poweroff -f"'


def record(a):
    stream = a.out / 'requests.stream'
    stream.unlink(missing_ok=True)
    env = dict(os.environ, NATEMU_FEXC_RECORD=str(stream))
    r = subprocess.run([a.stub, '--runtime', a.runtime, '--fexc', a.fexc, '--kernel', a.kernel, '--initrd', a.initrd, '--cmdline', CMDLINE,
                        '--vcpus', '1', '--serial', a.out / 'record.serial', '--compile-cache', 'off'],
                       env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, timeout=a.timeout)
    serial = (a.out / 'record.serial').read_text(errors='replace') if (a.out / 'record.serial').exists() else ''
    if r.returncode or not any(line.strip() == MARKER for line in serial.splitlines()):
        sys.exit(f'recording boot failed: exit {r.returncode}, {r.stderr.strip()[-400:]}')
    return stream


def serve(fexc):
    return subprocess.Popen([fexc, '--serve'], stdin=subprocess.PIPE, stdout=subprocess.PIPE)


def answer(p, record):
    p.stdin.write(record); p.stdin.flush()
    head = p.stdout.read(4)
    if len(head) < 4: sys.exit('a compiler ended while answering')
    return p.stdout.read(struct.unpack('<I', head)[0])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--fexc', required=True); ap.add_argument('--old', required=True)
    ap.add_argument('--stream', type=pathlib.Path)
    ap.add_argument('--stub'); ap.add_argument('--runtime'); ap.add_argument('--kernel'); ap.add_argument('--initrd')
    ap.add_argument('--out', type=pathlib.Path); ap.add_argument('--timeout', type=int, default=600)
    a = ap.parse_args()
    if not a.stream:
        if not all((a.stub, a.runtime, a.kernel, a.initrd, a.out)): ap.error('--stream, or --stub --runtime --kernel --initrd --out')
        a.out.mkdir(parents=True, exist_ok=True)
    stream = a.stream or record(a)
    old, new = serve(a.old), serve(a.fexc)
    n = differ = 0
    with open(stream, 'rb') as f:
        while head := f.read(4):
            rec = head + f.read(struct.unpack('<I', head)[0])
            n += 1
            if answer(old, rec) != answer(new, rec):
                differ += 1
                if differ <= 5:
                    args = rec[8:8 + struct.unpack('<I', rec[4:8])[0]].replace(b'\0', b' ').decode(errors='replace')
                    print(f'request {n} differs: {args}')
    for p in (old, new): p.stdin.close(); p.wait()
    same = os.path.realpath(a.old) == os.path.realpath(a.fexc)
    print(f'replay: {n} requests ({stream.stat().st_size >> 20} MiB), {differ} differences' + (' (OLD is NEW: the self-check only)' if same else ''))
    return 1 if differ or not n else 0


if __name__ == '__main__':
    sys.exit(main())
