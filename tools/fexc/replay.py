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


MAGIC, STUB_KINDS, SPLIT = b'NFXOBJ\0\x01', {6, 7, 11, 14, 15, 16, 17, 19, 20, 21, 22, 23}, 7
STADDB = 0x383f001f   # staddb wzr, [xN]: the base in bits 9:5
PROBING = 0x3942d380  # ldrb w0, [x28, #0xb4]: a probing split stub's first word


def only_probes(old, new):
    """M2 task 4, page-crossing stores without the trap: an intended change of 64-bit output, bounded here. None when the
    two replies differ only as that change allows, else what else differs. Allowed: split stubs that are the probes
    (eleven words: fexc_sites.inc FexcEmitStubs) where OLD has `brk #2`, hence later stubs and the
    words after them at other offsets, and the header's sizes and table offsets. Everything else must be byte-identical:
    the bytes around the object, the header's other fields, the code before the first stub (every block's main line),
    each stub that is no split stub, the words after the stubs, every site record but a stub's native offset, the PC
    table and the relocations."""
    u32 = lambda b, o: struct.unpack_from('<I', b, o)[0]
    # A reply's payload: code, json_len, object_len, extent, args_read, json, object (fexc.cpp, --serve).
    if len(old) < 20 or len(new) < 20 or old[:8] != new[:8] or old[12:20] != new[12:20] or u32(old, 0) != 0: return 'the status, report, extent or args_read differ'
    at = 20 + u32(old, 4)
    if old[20:at] != new[20:at] or old[at:at + 8] != MAGIC or new[at:at + 8] != MAGIC: return 'the report differs, or no object'
    ho, hn = old[at:at + 128], new[at:at + 128]
    # (The header's sizes and offsets that lie behind the stubs move: total_size, code_size, end_marker_offset,
    # code_region_end and the three tables' offsets; what they locate is compared below.)
    for o in range(0, 128, 4):
        if o not in (16, 44, 52, 56, 64, 72, 80) and u32(hn, o) != u32(ho, o): return f'header word +{o}'
    if len(old) != at + u32(old, 8) or len(new) != at + u32(new, 8) or u32(old, 8) != u32(ho, 16) or u32(new, 8) != u32(hn, 16): return 'the object lengths'
    oo, on = old[at:], new[at:]
    table = lambda b, h, o, size: [b[u32(h, o) + size * k:u32(h, o) + size * (k + 1)] for k in range(u32(h, o + 4))]
    so, sn = table(oo, ho, 64, 40), table(on, hn, 64, 40)
    if table(oo, ho, 72, 12) != table(on, hn, 72, 12): return 'the PC table differs'
    if len(so) != len(sn): return 'the number of sites differs'
    co, cn = oo[u32(ho, 40):][:u32(ho, 44)], on[u32(hn, 40):][:u32(hn, 44)]
    stubs = [(u32(x, 0), u32(y, 0), x[20]) for x, y in zip(so, sn) if x[20] in STUB_KINDS]
    for x, y in zip(so, sn):
        if x[4:] != y[4:] or (x[20] not in STUB_KINDS and x[:4] != y[:4]): return 'a site record differs'
    if not stubs: return 'the code differs in a block with no stub'
    # (The code's first word is FEX's block header, `udf` of the code's size: it moves with the stubs.)
    if stubs[0][0] != stubs[0][1] or u32(co, 0) >> 16 or u32(cn, 0) >> 16: return 'the main line differs'
    moved = {o: n for o, n, _ in stubs}
    for at in range(4, stubs[0][0], 4):
        x, y = u32(co, at), u32(cn, at)
        if x == y: continue
        # A branch to a stub behind a split stub: the same branch, to that stub where it is now.
        for mask, value, lo, bits in ((0x7e000000, 0x36000000, 5, 14), (0x7e000000, 0x34000000, 5, 19), (0xff000010, 0x54000000, 5, 19), (0xfc000000, 0x14000000, 0, 26)):
            field = ((1 << bits) - 1) << lo
            if x & mask == value and x & ~field == y & ~field:
                to = lambda w: at + 4 * (((w & field) >> lo) - (1 << bits if w >> (lo + bits - 1) & 1 else 0))
                if moved.get(to(x)) == to(y): break
        else:
            return f'the main line differs at +{at:#x}: {x:08x} | {y:08x}'
    for k, (o, n, kind) in enumerate(stubs):
        word = u32(cn, n)
        if kind == SPLIT and word == PROBING:
            a = u32(cn, n + 24) >> 5 & 31
            if (u32(cn, n + 24) & ~0x3e0 != STADDB or u32(cn, n + 28) & 0xffc003ff != 0x91000000 | a << 5 or u32(cn, n + 32) != STADDB or
                    u32(cn, n + 36) >> 26 != 5 or u32(cn, n + 40) != 0xd4200040 or u32(co, o) != 0xd4200040):
                return 'a probing stub of another form'
            size = 44
        else:
            if u32(co, o) != word: return 'a stub that is no probing split stub differs'
            size = 4
        if (stubs[k + 1][1] if k + 1 < len(stubs) else n + size) != n + size or (stubs[k + 1][0] if k + 1 < len(stubs) else o + 4) != o + 4:
            return 'the stubs are not one after the other'
    # Behind the stubs: zero words up to an alignment, then FEX's tail (a literal, with its relocation): the same bytes at
    # another offset. Relocations: the same, that one moved as far as the tail did.
    tail = [stubs[-1][0] + 4, stubs[-1][1] + (44 if u32(cn, stubs[-1][1]) == PROBING and stubs[-1][2] == SPLIT else 4)]
    for k, c in enumerate((co, cn)):
        while tail[k] + 4 <= len(c) and u32(c, tail[k]) == 0: tail[k] += 4
    # (FEX's JITCodeTail starts with the code's size, which grows with the stubs; the rest is the same.)
    # (and zero bytes pad the code to its alignment)
    if co[tail[0] + 4:].rstrip(b'\0') != cn[tail[1] + 4:].rstrip(b'\0') or u32(cn, tail[1]) < u32(co, tail[0]):
        return 'the words after the stubs differ: ' + ' '.join(f'{u32(co, o):x}' for o in range(tail[0], len(co) - 3, 4)) + ' | ' + ' '.join(f'{u32(cn, o):x}' for o in range(tail[1], len(cn) - 3, 4))
    ro, rn = table(oo, ho, 80, 16), table(on, hn, 80, 16)
    if len(ro) != len(rn) or any(x[4:] != y[4:] or u32(y, 0) - u32(x, 0) != (tail[1] - tail[0] if u32(x, 0) >= stubs[0][0] else 0) for x, y in zip(ro, rn)):
        return 'the relocations differ'
    return None


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
    n = differ = n32 = probes = 0
    with open(stream, 'rb') as f:
        while head := f.read(4):
            rec = head + f.read(struct.unpack('<I', head)[0])
            n += 1
            args = rec[8:8 + struct.unpack('<I', rec[4:8])[0]]
            if a.program32 and b'--bitness\x0032\0' in args:        # NEW only: main's compiler refuses 32-bit code
                n32 += 1; answer(new, rec); continue
            ro, rn = answer(old, rec), answer(new, rec)
            if ro != rn and (why := only_probes(ro, rn)) is not None:
                differ += 1
                if differ <= 5:
                    print(f'request {n} differs ({why[:400]}): ' + args[:60].replace(b'\0', b' ').decode(errors='replace'))
            elif ro != rn:
                probes += 1
    for p in (old, new): p.stdin.close(); p.wait()
    same = os.path.realpath(a.old) == os.path.realpath(a.fexc)
    print(f'replay: {n} requests ({stream.stat().st_size >> 20} MiB), {differ} differences' +
          (f'; {probes} more replies differ only by split stubs that probe (M2 task 4: only_probes)' if probes else '') + (' (OLD is NEW: the self-check only)' if same else '') +
          (f'; {n32} of them for 32-bit code, answered by NEW only' if a.program32 else ''))
    return 1 if differ or not n or (a.program32 and not n32) else 0


if __name__ == '__main__':
    sys.exit(main())
