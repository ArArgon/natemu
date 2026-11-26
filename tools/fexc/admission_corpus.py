#!/usr/bin/env python3
"""Builds the admission corpus (crates/host/tests/admission-corpus): objects fexc made for the requests of one recorded
boot, with each request's guest bytes, RIP and mode, for the host's `admission::corpus::recorded_corpus` test, which admits
every one (and mutations of it) and compares each result with the digest kept beside the corpus.

    admission_corpus.py --fexc FEXC --stream REQUESTS.stream --out crates/host/tests/admission-corpus/boot.corpus
        [--cpl0 N] [--cpl3 N]
    admission_corpus.py --fexc FEXC --gathers --out crates/host/tests/admission-corpus/gather.corpus

The stream is NATEMU_FEXC_RECORD's format (tools/fexc/replay.py): the `fexc-replay` case records one (1 vCPU, the Debian
kernel to a busybox power-off, with boot-i386's 32-bit program); `tools/natemu-test run tests/static -k "fexc-replay and
not order" --keep-outputs` keeps it as `out/fexc-replay/requests.stream` in the run's folder. Kept: every request for
32-bit code, and every k-th of the 64-bit ones at CPL 0 and at CPL 3 (k per kind, so that about N of each are kept), each
RIP and bytes once, whose reply holds an object. The corpus is fixed bytes once written: admission is a function of them,
so the test needs neither fexc nor a boot, and a new fexc changes nothing it checks. The file (zlib-compressed): b'NATADMC1', u32 count, then
per entry u8 flags (1 32-bit code, 2 --cpl0, 4 --fs-limited, 8 --gs-limited, 16 --bulk), u64 RIP, u32 source length,
u32 object length, the source (the request's bytes up to the reply's extent), the object.

--gathers (gather plan): the recorded boot runs no AVX2 gather, so its corpus holds no inline gather. This mode compiles
GATHER_BLOCKS instead, each at CPL 3 and at CPL 0 with the arguments the host sends for a 64-bit block (HOST_ARGS; the
recorded boot's), always with --bulk, under which fexc compiles a gather inside the block (FexcGatherLoad): every reply
must hold an inline gather's element load, or the mode fails. FEXC is the framework's build (testkit.artifacts.fexc)."""
import argparse, hashlib, pathlib, struct, sys, zlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import replay  # noqa: E402

MAGIC = b'NATADMC1'


def requests(stream):
    with open(stream, 'rb') as f:
        while head := f.read(4):
            rec = head + f.read(struct.unpack('<I', head)[0])
            n = struct.unpack('<I', rec[4:8])[0]
            args = rec[8:8 + n].split(b'\0')
            yield rec, args, rec[8 + n:]


def flags_of(args):
    bits32 = b'--bitness' in args and args[args.index(b'--bitness') + 1] == b'32'
    return (bits32 * 1 | (b'--cpl0' in args) * 2 | (b'--fs-limited' in args) * 4 | (b'--gs-limited' in args) * 8
            | (b'--bulk' in args) * 16)


# The arguments of the recorded boot's 64-bit requests (crates/host/src/execution.rs `body_with`), without --rip, at CPL 3;
# CPL 0 adds --cpl0 --tsc. Every gather request also has --bulk.
HOST_ARGS = [b'--profile', b'slice1'] + [x for f in ('sve128', 'sve256', 'rpres', 'wfxt', 'rng', 'cssc', 'afp', 'mops', 'ecv',
                                                     'clzero', 'svebitperm', 'float_exceptions', 'prefer_zva_for_vzero')
                                         for x in (b'--feature', f.encode() + b'=0')] + [b'--ext-conds', b'4', b'--push-run', b'--narrow-cmp']

# 64-bit blocks with AVX2 gathers (llvm-mc -x86-asm-syntax=intel), every element count of both element widths among them
# (4 bytes: 2, 4, 8; 8 bytes: 2, 4): a loop like llvmpipe's texture fetch, gathers with other loads beside them (a 4-byte
# vector load, a 4-byte general one), with and without a base, a displacement, an index scale; and the sources of
# gather-dd-bulk and gather-qq-bulk, the admission tests' fixtures (crates/host/tests/admission). Name, page offset of the
# RIP, bytes.
GATHER_BLOCKS = [
    # vpgatherdd ymm0, [rdi + ymm1*4], ymm2
    ('gather-dd-bulk', 0x000, 'c4e26d90048f'),
    # vpgatherqq ymm0, [ymm1*8 + 0x40], ymm2
    ('gather-qq-bulk', 0x000, 'c4e2ed9104cd40000000'),
    # 1: vpgatherdd ymm3, [rsi + ymm4*4], ymm5; vpaddd ymm0, ymm0, ymm3; add rsi, 32; dec ecx; jnz 1b
    ('loop', 0x740, 'c4e255901ca6 c5fdfec3 4883c620 ffc9 75ee'),
    # vmovss xmm6, [rdx]; vpgatherdd xmm0, [rdi + xmm1*4], xmm2; vgatherqpd ymm7, [rax + ymm8*8 + 0x10], ymm9; ret
    ('beside-load', 0x123, 'c5fa1032 c4e26990048f c4a2b5937cc010 c3'),
    # vpgatherqq ymm0, [ymm1*8 + 0x40], ymm2; vpgatherdq ymm10, [rbx + xmm11*8], ymm12;
    # vgatherdps ymm13, [rcx + ymm14*4 - 8], ymm15; jmp rax
    ('three', 0xfe0, 'c4e2ed9104cd40000000 c4229d9014db c42205926cb1f8 ffe0'),
    # vpgatherqd xmm0, [r8 + ymm1*4], xmm2; vgatherqps xmm3, [r9 + xmm4*2], xmm5; vgatherdpd xmm6, [r10 + xmm7*8], xmm8;
    # mov eax, [r11]; ret
    ('halves', 0x3c8, 'c4c26d910488 c4c251931c61 c4c2b93234fa 418b03 c3'),
]
GATHER_RIPS = {False: 0x7f3a_5c21_4000, True: 0xffff_ffff_8160_2000}


def gathers(fexc):
    """The corpus entries of GATHER_BLOCKS (the stream mode's entry format), each at CPL 3 and at CPL 0."""
    server = replay.serve(fexc)
    out = []
    for cpl0 in (False, True):
        for k, (name, offset, text) in enumerate(GATHER_BLOCKS):
            data = bytes.fromhex(text.replace(' ', ''))
            rip = GATHER_RIPS[cpl0] + 0x1000 * k + offset
            args = HOST_ARGS[:2] + [b'--rip', b'%#x' % rip] + HOST_ARGS[2:] + [b'--cpl0', b'--tsc'] * cpl0 + [b'--bulk']
            body = b''.join(x + b'\0' for x in args)
            body = struct.pack('<I', len(body)) + body + data
            reply = replay.answer(server, struct.pack('<I', len(body)) + body)
            code, json_len, obj_len, extent, _ = struct.unpack_from('<5I', reply, 0)
            if code or not obj_len or not replay.has_inline_gather(reply):
                sys.exit(f'{name} at {rip:#x}: no object with an inline gather (status {code}, object {obj_len} bytes)')
            obj = reply[20 + json_len:20 + json_len + obj_len]
            src = data[:min(len(data), extent)]
            out.append(struct.pack('<BQII', cpl0 * 2 | 16, rip, len(src), len(obj)) + src + obj)
    server.stdin.close(); server.wait()
    return out


def write(path, out):
    body = MAGIC + struct.pack('<I', len(out)) + b''.join(out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(zlib.compress(body, 9))
    n32 = sum(e[0] & 1 for e in out); n0 = sum(e[0] & 2 != 0 for e in out)
    print(f'{len(out)} objects ({n32} 32-bit, {n0} CPL 0, {len(out) - n32 - n0} CPL 3 64-bit), {len(body) >> 10} KiB, '
          f'{path.stat().st_size >> 10} KiB compressed: {path}')


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--fexc', required=True); ap.add_argument('--stream', type=pathlib.Path)
    ap.add_argument('--gathers', action='store_true', help='compile GATHER_BLOCKS instead of reading a stream')
    ap.add_argument('--out', required=True, type=pathlib.Path)
    ap.add_argument('--cpl0', type=int, default=1000); ap.add_argument('--cpl3', type=int, default=600)
    a = ap.parse_args()
    if a.gathers == bool(a.stream): ap.error('one of --stream and --gathers')
    if a.gathers:
        return write(a.out, gathers(a.fexc))
    kind = lambda fl: 'bits32' if fl & 1 else 'cpl0' if fl & 2 else 'cpl3'
    total = {'bits32': 0, 'cpl0': 0, 'cpl3': 0}
    for _, args, _ in requests(a.stream): total[kind(flags_of(args))] += 1
    every = {'bits32': 1, 'cpl0': max(1, total['cpl0'] // a.cpl0), 'cpl3': max(1, total['cpl3'] // a.cpl3)}
    nth = {'bits32': 0, 'cpl0': 0, 'cpl3': 0}
    server = replay.serve(a.fexc)
    seen, out = set(), []
    for rec, args, data in requests(a.stream):
        fl = flags_of(args)
        nth[kind(fl)] += 1
        if nth[kind(fl)] % every[kind(fl)]: continue
        rip = int(args[args.index(b'--rip') + 1], 0)
        reply = replay.answer(server, rec)
        code, json_len, obj_len, extent, _ = struct.unpack_from('<5I', reply, 0)
        obj = reply[20 + json_len:20 + json_len + obj_len]
        src = data[:min(len(data), extent)]
        key = hashlib.sha256(struct.pack('<BQ', fl, rip) + src + obj).digest()
        if code or not obj_len or key in seen: continue
        seen.add(key)
        out.append(struct.pack('<BQII', fl, rip, len(src), len(obj)) + src + obj)
    server.stdin.close(); server.wait()
    write(a.out, out)


if __name__ == '__main__':
    main()
