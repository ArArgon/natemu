#!/usr/bin/env python3
"""Offline model of fast mode on a host without FEAT_AFP (M1, M2) for the i8 and i8b corpora: which programs deviate from QEMU
only through the documented NaN-result classes, and the final state fast mode reaches there (accurate-FP plan chunk 0).

    python3 -I tools/models/afp_fp/predict.py [--expected RAW] [--run OUT_DIR] [--write tools/models/afp_fp/predictions.json]

`sim.py`, `fp.py`, `fpops.py` are an exact x86 AVX/FMA interpreter (rational IEEE arithmetic, MXCSR flags) with switchable NaN and
flush rules (the corpus analysis of November 15: it reproduced natemu's M2-emulation final state on 472 of 472 programs).
`FAST` is the rule set fast mode has now on a host without AFP: the default NaN's sign (a), the two-NaN choice (b2), the
FMA three-NaN order (b3), negation flipping a NaN's sign (c), FMA inf*0 + QNaN (f1) and tininess judged before rounding
(e2: flag only, no corpus program reaches it); DAZ and FTZ are exact since chunk 3a.

The corpora are generated here as tools/reference/gen_i8.py makes them now (GENERATE: the inputs of tests/fp/family.toml; needs
clang); their QEMU answers are read from `RAW/<corpus key>/expected.json` (default: the raw-results folder, tools/raw_root.py).
The model's QEMU rules (fp.Q) must reproduce every QEMU answer, else this exits 1. A program is tagged when the model's
fast-mode state differs from QEMU's answer and every cause is a NaN class: a cause is a class whose switching off alone (from
FAST) or on alone (from the M4's rules) changes the program's final state. Tags come from the model alone, never from a natemu
run. `--run` (optional) is a run of the corpora in M2 emulation (the fp family's i8 and i8b suites with NATEMU_FEXC_FEATURE_OFF=afp:
`OUT_DIR/out/i8/results.json` and `out/i8b/`); it only checks that the model reproduces natemu's final state on every program
(a difference exits 1: a natemu bug or a model gap). The output holds, per tagged program, its classes (registry names) and the
predicted final state (GPRs, RFLAGS, RIP; each memory region as a SHA-256).
"""
import argparse, hashlib, json, pathlib, subprocess, sys, tempfile
HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE.parent.parent / 'reference'))
import flags, fp, fpops, ref  # noqa: F401  (fpops registers the instruction semantics)
from sim import Sim

CORPORA = {'i8': 'i8-corpus-20261030-01', 'i8b': 'i8-corpus-20261030-02'}
# gen_i8.py's arguments for each corpus (tests/fp/family.toml's inputs i8 and i8b)
GENERATE = {'i8-corpus-20261030-01': ['--seed', '20261030', '--random', '60'], 'i8-corpus-20261030-02': []}
GEN_I8 = HERE.parent.parent / 'reference' / 'gen_i8.py'
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


def as_report(result):
    """The final state of a result shaped as `ref.compare` takes, in the shape of a driver report (`differs`' second argument)."""
    q = result['qemu']
    return {'gpr': q['gpr'], 'rflags': q['rflags'], 'rip': q['rip'], 'memory': q['memory']}


def generate(key, folder):
    """Writes the corpus `key` as gen_i8.py makes it now to folder/key/programs.json (the folder name is the predictions' key)."""
    out = pathlib.Path(folder) / key / 'programs.json'
    out.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run([sys.executable, '-B', str(GEN_I8), '-o', str(out)] + GENERATE[key], capture_output=True, text=True)
    if r.returncode: raise RuntimeError(f'gen_i8.py for {key} failed: {r.stderr.strip()[-500:]}')
    return out


def locked_answers(suite, progs, lock, bad):
    """QEMU-shaped results from the model's QEMU rules, each checked against the answer hash locked for the case (testkit's
    normalizer, the same one every run uses); a case locked from real x86 (or not yet answered) is not compared."""
    import tomllib
    sys.path.insert(0, str(HERE.parent.parent.parent))
    from testkit.harness.normalize_programs import answer
    locked = tomllib.loads(lock.read_text())['suite'][suite]
    x86 = set(locked.get('x86_cases', [])) | set(locked.get('model_cases', []))
    exp = {}
    for n, p in progs.items():
        m = model_result(p)
        want = locked['cases'].get(n, '')
        ref_rules = model_result(p, fp.BASE) if n in x86 else m     # x86-answered cases: the model's x86 rules must match
        if want and answer(p, ref_rules['qemu']) != want:
            bad += 1; print(f"MODEL({'x86' if n in x86 else 'QEMU'} rules) != locked answer", suite, n)
        exp[n] = m
    return exp, bad


def model_result(program, mode=fp.Q):
    """A result shaped as a QEMU result from the model's rules `mode` (default QEMU's, which reproduce every QEMU answer of both
    corpora): the final state, the RIP after the straight-line code and the RFLAGS mask the reference derives."""
    shape = {'qemu': {'rip': hex(ref.num(program['rip']) + len(program['code']) // 2)},
             'rflags_compare_mask': ref.hx(flags.TRACKED & ~flags.analyze(program['insns']))}
    return {'name': program['name'], **as_expected(state(program, mode), shape)}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--expected', type=pathlib.Path, help='folder holding <corpus key>/expected.json (natemu-raw); by default the '
                    "model's QEMU rules are checked against the answer hashes locked in tests/fp/tests.lock instead")
    ap.add_argument('--lock', type=pathlib.Path, default=HERE.parent.parent.parent / 'tests' / 'fp' / 'tests.lock')
    ap.add_argument('--run', type=pathlib.Path, help='a natemu run in M2 emulation, to check the model against (never a tag source)')
    ap.add_argument('--write', type=pathlib.Path)
    a = ap.parse_args()
    out, bad = {}, 0
    with tempfile.TemporaryDirectory() as tmp:
        for c, d in CORPORA.items():
            progs = {p['name']: p for p in json.loads(generate(d, tmp).read_text())['programs']}
            if a.expected:
                exp = {r['name']: r for r in json.loads((a.expected / d / 'expected.json').read_text())['results']}
            else:
                exp, bad = locked_answers(c, progs, a.lock, bad)
            if set(exp) != set(progs): sys.exit(f'{d}: the QEMU answers name other programs than gen_i8.py makes now')
            res = {r['name']: r for r in json.loads((a.run / 'out' / c / 'results.json').read_text())['results']} if a.run else {}
            tagged, fast_differs, natemu_differs = {}, 0, 0
            for n, p in progs.items():
                st, qemu = state(p, FAST), as_report(exp[n])
                q_diff = differs(as_expected(state(p, fp.Q), exp[n]), qemu)    # the model must reproduce QEMU's answer
                if q_diff:
                    bad += 1; print('MODEL(QEMU rules) != QEMU', c, n, q_diff[:2]); continue
                if n in res:                                                    # and natemu's state, when a run is given
                    natemu_differs += bool(differs(exp[n], res[n]['vm']))
                    mine = differs(as_expected(st, exp[n]), res[n]['vm'])
                    if mine: bad += 1; print('MODEL != natemu', c, n, mine[:2])
                if not differs(as_expected(st, exp[n]), qemu): continue
                fast_differs += 1
                cs = causes(p)
                if cs and cs <= set(NAN_CLASSES):
                    out.setdefault(d, {})[n] = {
                        'classes': sorted({NAN_CLASSES[k] for k in cs}), 'model': sorted(cs), 'rip': exp[n]['qemu']['rip'],
                        'gpr': [hex(v) for v in st[0]], 'rflags': hex(st[1]),
                        'memory': [{'addr': hex(a), 'sha256': hashlib.sha256(dd).hexdigest()} for a, dd in st[2]]}
                    tagged[n] = 1
                else:
                    why = 'the M4 rules give the same state: QEMU departs from x86' if st == state(p, fp.BASE) else 'causes: ' + (', '.join(sorted(cs)) or 'none')
                    print(f'differs from QEMU, not tagged ({why})', c, n)
            print(f"{c} ({d}): {len(progs)} programs, the model's fast mode differs from QEMU in {fast_differs}, tagged {len(tagged)}"
                  + (f'; natemu (--run) differs in {natemu_differs}' if res else ''))
    if a.write: a.write.write_text(json.dumps(out, indent=1, sort_keys=True) + '\n')
    sys.exit(1 if bad else 0)


if __name__ == '__main__': main()
