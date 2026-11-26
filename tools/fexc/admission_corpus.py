#!/usr/bin/env python3
"""Builds the admission corpus (crates/host/tests/admission-corpus): objects fexc made for the requests of one recorded
boot, with each request's guest bytes, RIP and mode, for the host's `admission::tests::recorded_corpus` test, which admits
every one (and mutations of it) and compares each result with the digest kept beside the corpus.

    admission_corpus.py --fexc FEXC --stream REQUESTS.stream --out crates/host/tests/admission-corpus/boot.corpus
        [--cpl0 N] [--cpl3 N]

The stream is NATEMU_FEXC_RECORD's format (tools/fexc/replay.py): the `fexc-replay` case records one (1 vCPU, the Debian
kernel to a busybox power-off, with boot-i386's 32-bit program); `tools/natemu-test run tests/static -k "fexc-replay and
not order" --keep-outputs` keeps it as `out/fexc-replay/requests.stream` in the run's folder. Kept: every request for
32-bit code, and every k-th of the 64-bit ones at CPL 0 and at CPL 3 (k per kind, so that about N of each are kept), each
RIP and bytes once, whose reply holds an object. The corpus is fixed bytes once written: admission is a function of them,
so the test needs neither fexc nor a boot, and a new fexc changes nothing it checks. The file (zlib-compressed): b'NATADMC1', u32 count, then
per entry u8 flags (1 32-bit code, 2 --cpl0, 4 --fs-limited, 8 --gs-limited, 16 --bulk), u64 RIP, u32 source length,
u32 object length, the source (the request's bytes up to the reply's extent), the object."""
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


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--fexc', required=True); ap.add_argument('--stream', required=True, type=pathlib.Path)
    ap.add_argument('--out', required=True, type=pathlib.Path)
    ap.add_argument('--cpl0', type=int, default=1000); ap.add_argument('--cpl3', type=int, default=600)
    a = ap.parse_args()
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
    body = MAGIC + struct.pack('<I', len(out)) + b''.join(out)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_bytes(zlib.compress(body, 9))
    n32 = sum(e[0] & 1 for e in out); n0 = sum(e[0] & 2 != 0 for e in out)
    print(f'{len(out)} objects ({n32} 32-bit, {n0} CPL 0, {len(out) - n32 - n0} CPL 3 64-bit), {len(body) >> 10} KiB, '
          f'{a.out.stat().st_size >> 10} KiB compressed: {a.out}')


if __name__ == '__main__':
    main()
