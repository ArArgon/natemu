#!/usr/bin/env python3
"""Offline model of fast mode on a host without FEAT_AFP (M1, M2) for the i8 and i8b corpora: which programs deviate from QEMU
only through the documented NaN-result classes, and the final state fast mode reaches there (accurate-FP plan chunk 0).

    python3 -I tools/models/afp_fp/predict.py --raw NATEMU_RAW --run OUT_DIR [--write predictions.json]

`sim.py`, `fp.py`, `fpops.py` are an exact x86 AVX/FMA interpreter (rational IEEE arithmetic, MXCSR flags) with switchable NaN and
flush rules (the corpus analysis of November 15: it reproduced natemu's M2-emulation final state on 472 of 472 programs).
`FAST` is the rule set fast mode has now on a host without AFP: the default NaN's sign (a), the two-NaN choice (b2), the
FMA three-NaN order (b3), negation flipping a NaN's sign (c), FMA inf*0 + QNaN (f1) and tininess judged before rounding
(e2: flag only, no corpus program reaches it); DAZ and FTZ are exact since chunk 3a. `--run` is a run of the corpora in M2
emulation (`run_suite.py --only i8,i8b` with NATEMU_FEXC_FEATURE_OFF=afp), `OUT_DIR/out/i8/results.json` and `out/i8b/`; the
model must reproduce every program's final state in it, else this exits 1. A program is tagged when it differs from QEMU
in the model and every cause is a NaN class: a cause is a class whose switching off alone (from FAST) or on alone (from the
M4's rules) changes the program's final state. The output holds, per tagged program, its classes (registry names) and the
predicted final state (GPRs, RFLAGS, RIP; each memory region as a SHA-256).
"""
import argparse, hashlib, json, pathlib, sys
HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE.parent.parent / 'reference'))
import fp, fpops, ref  # noqa: F401  (fpops registers the instruction semantics)
from sim import Sim

CORPORA = {'i8': 'i8-corpus-20261030-01', 'i8b': 'i8-corpus-20261030-02'}
FAST_ON = dict(dnan_neg=False, nan2='arm', fma='arm', negflip=True, fma_inf0q=True, tiny_before=True)
NAN_CLASSES = {'a': 'nan-results', 'b2': 'nan-results', 'b3': 'nan-results', 'c': 'nan-results', 'f1': 'fma-invalid-flag'}
OTHER = {'e2': 'band-ue-flag'}      # a cause that is not a NaN class: the program is not tagged
FAST = fp.variant(name='fast', **FAST_ON)


def state(program, mode):
    s = Sim(program, mode).run()
    return (tuple(s.gpr), s.fl, tuple(sorted((a, bytes(d)) for a, d in s.mem.items())))


def causes(program):
    """The classes (model letters) that change the program's final state: off alone from FAST, or on alone from the M4's rules."""
    full, base = state(program, FAST), state(program, fp.BASE)
    out = set()
    for k, kw in fp.CLASSES.items():
        if k not in NAN_CLASSES and k not in OTHER: continue
        off = fp.variant(name='off', **{**FAST_ON, **{a: getattr(fp.BASE, a) for a in kw}})
        if state(program, off) != full or state(program, fp.variant(name='on', **kw)) != base: out.add(k)
    return out


def as_expected(st, exp):
    """A state in the shape of a QEMU result (`ref.compare`'s first argument), with `exp`'s RIP and RFLAGS mask."""
    return {'qemu': {**exp['qemu'], 'gpr': [hex(v) for v in st[0]], 'rflags': hex(st[1]),
                     'memory': [{'addr': hex(a), 'data': d.hex()} for a, d in st[2]]},
            'rflags_compare_mask': exp['rflags_compare_mask']}


def differs(expected, vm):
    """Differences between a result shaped as `ref.compare` takes and the driver's report `vm`."""
    return ref.compare(expected, [ref.num(v) for v in vm['gpr']], ref.num(vm['rflags']), ref.num(vm['rip']), vm['memory'])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--raw', type=pathlib.Path, required=True); ap.add_argument('--run', type=pathlib.Path, required=True)
    ap.add_argument('--write', type=pathlib.Path)
    a = ap.parse_args()
    out, bad = {}, 0
    for c, d in CORPORA.items():
        progs = {p['name']: p for p in json.loads((a.raw / d / 'programs.json').read_text())['programs']}
        exp = {r['name']: r for r in json.loads((a.raw / d / 'expected.json').read_text())['results']}
        res = {r['name']: r for r in json.loads((a.run / 'out' / c / 'results.json').read_text())['results']}
        tagged, failing = {}, 0
        for n, p in progs.items():
            st = state(p, FAST)
            vm = res[n]['vm']
            failing += bool(differs(exp[n], vm))
            mine = differs(as_expected(st, exp[n]), vm)     # the model must reproduce natemu's final state
            if mine:
                bad += 1; print('MODEL != natemu', c, n, mine[:2]); continue
            if not differs(exp[n], vm): continue
            cs = causes(p)
            if cs and cs <= set(NAN_CLASSES):
                out.setdefault(d, {})[n] = {
                    'classes': sorted({NAN_CLASSES[k] for k in cs}), 'model': sorted(cs), 'rip': exp[n]['qemu']['rip'],
                    'gpr': [hex(v) for v in st[0]], 'rflags': hex(st[1]),
                    'memory': [{'addr': hex(a), 'sha256': hashlib.sha256(dd).hexdigest()} for a, dd in st[2]]}
                tagged[n] = 1
        print(f'{c} ({d}): {len(progs)} programs, natemu differs from QEMU in {failing}, tagged {len(tagged)}')
    if a.write: a.write.write_text(json.dumps(out, indent=1, sort_keys=True) + '\n')
    sys.exit(1 if bad else 0)


if __name__ == '__main__': main()
