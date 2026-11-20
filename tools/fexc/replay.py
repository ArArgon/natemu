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
several hundred MiB: it stays in DIR, which the framework deletes after a pass.

--program32 FILE.asm (M2): the recording boot also runs that static 32-bit program (assembled with nasm, appended to
the initrd as run_boot_smoke.py's i386 case does), so the stream mixes 64-bit and 32-bit requests. NEW answers them all,
in the recorded order, from one process; OLD answers only the 64-bit ones, since the compiler on `main` refuses 32-bit
code until M2 merges (then OLD can answer them too: drop the filter). So the check also shows that a 64-bit reply does
not depend on 32-bit requests answered before it by the same compiler process. The program must print its `natemu-i386-ok`, and a stream without a 32-bit request fails."""
import argparse, os, pathlib, struct, subprocess, sys

MARKER = 'natemu-replay-recorded'
CMDLINE = f'console=ttyS0 earlyprintk=serial mitigations=off nokaslr rdinit=/bin/busybox -- sh -c "echo {MARKER}; poweroff -f"'


def newc(name, mode, data):
    """An uncompressed initramfs (cpio newc) holding one file."""
    out = b''
    for k, (n, m, d) in enumerate([(name, mode, data), ('TRAILER!!!', 0, b'')]):
        head = b'070701' + b''.join(b'%08X' % f for f in [k + 1, m, 0, 0, 1, 0, len(d), 0, 0, 0, 0, len(n) + 1, 0]) + n.encode() + b'\0'
        out += head + bytes(-len(head) % 4) + d + bytes(-len(d) % 4)
    return out


def record(a):
    stream = a.out / 'requests.stream'
    stream.unlink(missing_ok=True)
    cmdline = CMDLINE
    if a.program32:
        subprocess.run(['nasm', '-f', 'bin', '-o', a.out / 'program32', a.program32], check=True)
        base = pathlib.Path(a.initrd).read_bytes()
        a.initrd = a.out / 'initrd-32'
        a.initrd.write_bytes(base + bytes(-len(base) % 4) + newc('program32', 0o100755, (a.out / 'program32').read_bytes()))
        cmdline = CMDLINE.replace('echo ', '/program32; echo ')
    env = dict(os.environ, NATEMU_FEXC_RECORD=str(stream))
    r = subprocess.run([a.stub, '--runtime', a.runtime, '--fexc', a.fexc, '--kernel', a.kernel, '--initrd', a.initrd, '--cmdline', cmdline,
                        '--vcpus', '1', '--serial', a.out / 'record.serial', '--compile-cache', 'off'],
                       env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, timeout=a.timeout)
    serial = (a.out / 'record.serial').read_text(errors='replace') if (a.out / 'record.serial').exists() else ''
    if r.returncode or not any(line.strip() == MARKER for line in serial.splitlines()):
        sys.exit(f'recording boot failed: exit {r.returncode}, {r.stderr.strip()[-400:]}')
    if a.program32 and not any(line.strip() == 'natemu-i386-ok' for line in serial.splitlines()):
        sys.exit('recording boot: the 32-bit program did not print natemu-i386-ok')
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
    ap.add_argument('--program32', help='a static 32-bit program (nasm source) the recording boot also runs')
    a = ap.parse_args()
    if not a.stream:
        if not all((a.stub, a.runtime, a.kernel, a.initrd, a.out)): ap.error('--stream, or --stub --runtime --kernel --initrd --out')
        a.out.mkdir(parents=True, exist_ok=True)
    stream = a.stream or record(a)
    old, new = serve(a.old), serve(a.fexc)
    n = differ = n32 = 0
    with open(stream, 'rb') as f:
        while head := f.read(4):
            rec = head + f.read(struct.unpack('<I', head)[0])
            n += 1
            args = rec[8:8 + struct.unpack('<I', rec[4:8])[0]]
            if a.program32 and b'--bitness\x0032\0' in args:        # NEW only: main's compiler refuses 32-bit code
                n32 += 1; answer(new, rec); continue
            if answer(old, rec) != answer(new, rec):
                differ += 1
                if differ <= 5:
                    print(f'request {n} differs: ' + args.replace(b'\0', b' ').decode(errors='replace'))
    for p in (old, new): p.stdin.close(); p.wait()
    same = os.path.realpath(a.old) == os.path.realpath(a.fexc)
    print(f'replay: {n} requests ({stream.stat().st_size >> 20} MiB), {differ} differences' + (' (OLD is NEW: the self-check only)' if same else '') +
          (f'; {n32} of them for 32-bit code, answered by NEW only' if a.program32 else ''))
    return 1 if differ or not n or (a.program32 and not n32) else 0


if __name__ == '__main__':
    sys.exit(main())
