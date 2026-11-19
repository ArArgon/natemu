#!/usr/bin/env python3
"""RCPPS/RCPSS/RSQRTPS/RSQRTSS and their VEX forms on the inputs whose result the Intel SDM fixes, with the expected
bits written here (no reference machine). One natemu `run-program` per instruction form (each also with one register as
source and destination, `FORM-same`); each input is a case in
OUT/results.json.

    run_approx_special.py --host H --runtime R --fexc F --out OUT

The SDM (RCPPS, RSQRTPS): a 0.0 source gives infinity of its sign; a denormal source is treated as 0.0; RSQRT of a
negative value (not -0.0) gives the floating-point indefinite (0xffc00000); an SNaN source gives the quieted NaN, a
QNaN source itself; RCP flushes tiny results to 0.0 of the source's sign. A source up to 1.11111111110100000000000b *
2^125 never gives a tiny RCP result, one from 1.00000000000110000000001b * 2^126 always does (the SDM prints the two
directions reversed; this is the arithmetic reading). Other results are approximations: |relative error| <= 1.5 *
2^-12, checked as such. The approximation instructions set no MXCSR flags and ignore its rounding control.
"""
import argparse
import json
import pathlib
import subprocess
import sys
from fractions import Fraction as Fr

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE / 'reference'))
import ref  # noqa: E402
import run_slice1_vm as v1  # noqa: E402

INF, NINF, INDEFINITE = 0x7f800000, 0xff800000, 0xffc00000
BOUND = Fr(3, 2) / 2 ** 12
NO_TINY_BELOW = Fr(int('1' + '11111111110100000000000', 2), 2 ** 23) * 2 ** 125
TINY_FROM = Fr(int('100000000000110000000001', 2), 2 ** 23) * 2 ** 126

# (source bits, expected): an int is the exact result; 'approx' a result within the bound; 'approx-or-zero' RCP's
# implementation-defined tiny band (0.0 of the source's sign, or a result within the bound).
RCP = [
    (0x00000000, INF), (0x80000000, NINF),                          # 0.0: infinity of its sign
    (0x00000001, INF), (0x807fffff, NINF), (0x00400000, INF),       # denormals count as 0.0
    (0x7f800000, 0x00000000), (0xff800000, 0x80000000),             # 1/inf = 0
    (0x7fc00000, 0x7fc00000), (0xffc12345, 0xffc12345),             # QNaN: itself
    (0x7fa00000, 0x7fe00000), (0xff800001, 0xffc00001),             # SNaN: quieted
    (0x7f7fffff, 0x00000000), (0xff7fffff, 0x80000000),             # largest finite: tiny, flushed
    (0x7f000000, 0x00000000), (0x7e801000, 0x00000000),             # 2^127; just above the band: flushed
    (0x7e800000, 'approx-or-zero'), (0xfe800000, 'approx-or-zero'),  # 2^126: the band
    (0x7e000000, 'approx'), (0x00800000, 'approx'),                 # 2^125: never tiny; smallest normal: 2^126
    (0x3f800000, 'approx'), (0x40400000, 'approx'), (0xc0a00000, 'approx'), (0x3dcccccd, 'approx'),
]
RSQRT = [
    (0x00000000, INF), (0x80000000, NINF),                          # 0.0: infinity of its sign
    (0x00000001, INF), (0x80000001, NINF), (0x007fffff, INF),       # denormals count as 0.0
    (0x7f800000, 0x00000000), (0xff800000, INDEFINITE),             # +inf: 0; -inf: negative
    (0xbf800000, INDEFINITE), (0xff7fffff, INDEFINITE),             # negative normals
    (0x7fc00000, 0x7fc00000), (0x7fa00000, 0x7fe00000),             # QNaN itself; SNaN quieted
    (0x3f800000, 'approx'), (0x40800000, 'approx'), (0x40000000, 'approx'),
    (0x00800000, 'approx'), (0x7f7fffff, 'approx'), (0x3c23d70a, 'approx'),
]


def value(b: int) -> Fr:
    e, m = b >> 23 & 0xff, b & 0x7fffff
    v = Fr(m, 2 ** 23) * Fr(2) ** -126 if e == 0 else (1 + Fr(m, 2 ** 23)) * Fr(2) ** (e - 127)
    return -v if b >> 31 else v


def within(kind: str, x: int, v: int) -> bool:
    if v >> 23 & 0xff in (0, 0xff) or (kind == 'rcp' and v >> 31 != x >> 31) or (kind == 'rsqrt' and v >> 31):
        return False
    q = abs(value(v)) * abs(value(x)) if kind == 'rcp' else value(v) ** 2 * value(x)
    return ((1 - BOUND) <= q <= (1 + BOUND)) if kind == 'rcp' else ((1 - BOUND) ** 2 <= q <= (1 + BOUND) ** 2)


def is_nan(b: int) -> bool:
    return b >> 23 & 0xff == 0xff and b & 0x7fffff != 0


def check(kind: str, x: int, want, got: int) -> str | None:
    if isinstance(want, int):
        return None if got == want else f'{got:#010x}, the SDM gives {want:#010x}'
    if want == 'approx-or-zero' and got == (x >> 31) << 31: return None
    return None if within(kind, x, got) else f'{got:#010x} is not within 1.5 * 2^-12 of the true value'


IN, OUT, SCALAR_DEST = 0x200000, 0x200400, 0x200800
FILL = 0x5a5a5a5a      # the upper lanes a scalar form keeps


def program(name: str, table) -> tuple[dict, list]:
    """A straight-line program applying the form `name` to every source of `table` (four lanes per packed operation, one per
    scalar), storing each result at OUT + 16 * i; also returns, per store, which inputs its lanes hold. A name ending in
    `-same` uses one register as source and destination (natemu's code differs: the source dies in the instruction)."""
    form, same = name.removesuffix('-same'), name.endswith('-same')
    srcs = [x for x, _ in table]
    asm, lanes = [], []
    scalar = form.endswith('ss')
    groups = [[x] for x in srcs] if scalar else [srcs[i:i + 4] for i in range(0, len(srcs), 4)]
    data_in = bytearray()
    for g in groups:
        data_in += b''.join(x.to_bytes(4, 'little') for x in (g + [0x3f800000] * 4)[:4])     # pad lanes: 1.0
    for i, g in enumerate(groups):
        src, dst = IN + 16 * i, OUT + 16 * i
        asm.append(f'movdqu xmm0, xmmword ptr [{src}]')
        if same:
            if scalar:   # lane 0 the source, the upper lanes FILL
                asm += [f'movdqu xmm1, xmmword ptr [{SCALAR_DEST}]', 'insertps xmm1, xmm0, 0']
                asm.append(f'{form} xmm1, xmm1, xmm1' if form.startswith('v') else f'{form} xmm1, xmm1')
            else:
                asm += [f'{form} xmm0, xmm0', 'movdqa xmm1, xmm0']
        elif scalar:
            asm.append(f'movdqu xmm1, xmmword ptr [{SCALAR_DEST}]')
            # the upper lanes come from the destination (SSE) or the first source (VEX): both are xmm1 here
            asm.append(f'{form} xmm1, xmm1, xmm0' if form.startswith('v') else f'{form} xmm1, xmm0')
        else:
            asm.append(f'{form} xmm1, xmm0')
        asm.append(f'movdqu xmmword ptr [{dst}], xmm1')
        lanes.append(g)
    code = ref.assemble('\n'.join(asm)).hex()
    memory = [{'addr': hex(IN), 'data': bytes(data_in).ljust(0x400, b'\0').hex()},
              {'addr': hex(OUT), 'data': bytes(16 * len(groups)).hex()},
              {'addr': hex(SCALAR_DEST), 'data': (FILL.to_bytes(4, 'little') * 4).hex()}]
    p = {'name': f'approx-special-{name}', 'code': code, 'rip': '0x100000', 'gpr': ['0x0'] * 16, 'rflags': '0x2',
         'memory': memory, 'insns': [{'op': a.split()[0], 'sse': True} for a in asm], 'sse': True,
         'avx': form.startswith('v')}
    return p, lanes


def run(args, form, kind, table, work: pathlib.Path) -> list[dict]:
    p, lanes = program(form, table)
    work.mkdir(parents=True, exist_ok=True)
    (work / 'program.txt').write_text(v1.description(p))
    r = subprocess.run([args.host, args.runtime, 'run-program', args.fexc, work / 'program.txt', work, *v1.CODE_SHAPE],
                       capture_output=True, text=True)
    want = dict(table)
    if r.returncode != 0:
        return [{'name': f'{form}/{x:#010x}', 'ok': False, 'problems': [f'natemu-host exit {r.returncode}: {r.stderr[-300:]}']}
                for x, _ in table]
    vm = json.loads(r.stdout)
    if vm.get('stop') != 'reached':
        return [{'name': f'{form}/{x:#010x}', 'ok': False, 'problems': [f"stopped {vm.get('stop')} {vm.get('reason')}"]}
                for x, _ in table]
    out = next(bytes.fromhex(m['data']) for m in vm['memory'] if ref.num(m['addr']) == OUT)
    fast_without_afp = vm.get('fp') == 'fast' and vm.get('afp') is False
    res = []
    for i, g in enumerate(lanes):
        words = [int.from_bytes(out[16 * i + 4 * j:16 * i + 4 * j + 4], 'little') for j in range(4)]
        for j, x in enumerate(g):
            why = check(kind, x, want[x], words[j])
            r = {'name': f'{form}/{x:#010x}', 'ok': why is None, 'problems': [why] if why else []}
            if why and fast_without_afp and isinstance(want[x], int) and is_nan(want[x]) and is_nan(words[j]):
                # documented: fast mode without AFP gives NaNs Arm's sign and choice (afp-fallback-plan.md, nan-results)
                r.update(outcome='deviated', problems=[why + ' (documented deviation nan-results: fast mode, no AFP)'])
            res.append(r)
        if form.removesuffix('-same').endswith('ss') and words[1:] != [FILL] * 3:
            res.append({'name': f'{form}/{g[0]:#010x}/upper', 'ok': False,
                        'problems': [f'upper lanes {[hex(w) for w in words[1:]]}, the destination kept {FILL:#x}']})
    return res


FORMS = [('rcpps', 'rcp', RCP), ('vrcpps', 'rcp', RCP), ('rcpss', 'rcp', RCP), ('vrcpss', 'rcp', RCP),
         ('rsqrtps', 'rsqrt', RSQRT), ('vrsqrtps', 'rsqrt', RSQRT), ('rsqrtss', 'rsqrt', RSQRT), ('vrsqrtss', 'rsqrt', RSQRT)]
FORMS += [(f + '-same', k, t) for f, k, t in FORMS]


def self_check() -> None:
    assert check('rcp', 0x3f800000, 'approx', 0x3f800000) is None and check('rcp', 0x3f800000, 'approx', 0x3f801000)
    assert check('rcp', 0x7e800000, 'approx-or-zero', 0) is None and check('rcp', 0x00000001, INF, 0x7f000000)
    assert check('rsqrt', 0x40800000, 'approx', 0x3f000000) is None
    assert TINY_FROM > NO_TINY_BELOW and NO_TINY_BELOW < 2 ** 126 < TINY_FROM


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    for n in ('host', 'runtime', 'fexc', 'out'): ap.add_argument('--' + n, type=pathlib.Path)
    ap.add_argument('--self-check', action='store_true')
    a = ap.parse_args()
    self_check()
    if a.self_check: print('self-check passed'); return 0
    a.out.mkdir(parents=True, exist_ok=True)
    results = [r for form, kind, table in FORMS for r in run(a, form, kind, table, a.out / form)]
    (a.out / 'results.json').write_text(json.dumps({'results': results}, indent=1) + '\n')
    bad = [r for r in results if not r['ok'] and r.get('outcome') != 'deviated']
    for r in bad: print(f"FAIL {r['name']}: {'; '.join(r['problems'])}")
    print(f'{len(results) - len(bad)} of {len(results)} as the SDM requires')
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
