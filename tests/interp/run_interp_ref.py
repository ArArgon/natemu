#!/usr/bin/env python3
"""The interp family's `references` case: the boot-time interpreter's per-instruction cases against their two references
(docs/restructure/interp-cases.md). natemu does not run here yet: these are the answers its cases will be judged by.

Locked (every ordinary run): nothing runs. The corpus is generated (tests/interp/interp_cases.py); each case's answer
from QEMU and from KVM comes from tests/interp/tests.lock; a case passes when both are there and agree on every field
both answer, and an empty case (group `Rig`) when its answers are exactly its starting state.

Seeding (TESTKIT_SEED_ANSWERS=1 NATEMU_X86_HOST=user@host, then `natemu-test lock tests/interp --from-run RUN1 RUN2`):
the image runs on the pinned QEMU here and under KVM on the x86 machine; every case's two answers are recorded and
checked the same way. Each run's COM1 output and the KVM machine's description are kept in --out.

    tools/natemu-test run tests/interp -k references
"""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'tests' / 'lib')); import testenv  # noqa: E401,E402,E702
import argparse
import json
import os

import answers
import interp_cases
import interp_rig


def expected_empty(case: dict, ref: str) -> dict:
    """An empty case's answer: its starting state, read back (the rig's own check of its set-up and capture)."""
    st, m = case['state'], interp_rig.num
    rec = {'stop': 0, **{r: m(st['gpr'][r]) for r in interp_cases.GPRS}, 'eflags': m(st['eflags']),
           **{s: m(st['seg'][s]['sel']) for s in interp_cases.SREGS}, 'cr0': m(st['cr0']), 'cr3': m(st['cr3']),
           'cr4': m(st['cr4']), 'efer': m(st['efer'])}
    mem = {}
    for b in case['memory']:
        a = m(b['addr']); mem.update({a + i: x for i, x in enumerate(bytes.fromhex(b['data']))})
    wins = [bytes(mem.get(m(w['addr']) + i, 0) for i in range(w['len'])) for w in case['compare']['windows']]
    import struct
    raw = struct.pack('<I8II6H3IQ', 0, *(rec[r] for r in interp_cases.GPRS), rec['eflags'], *(rec[s] for s in interp_cases.SREGS),
                      rec['cr0'], rec['cr3'], rec['cr4'], rec['efer'])
    raw += struct.pack('<HI2x', m(st['gdtr']['limit']), m(st['gdtr']['base'])) + struct.pack('<HI2x', m(st['idtr']['limit']), m(st['idtr']['base']))
    return interp_rig.answer(case, raw + bytes(interp_rig.REC_SIZE - len(raw)), wins, ref)


def seed(doc, out, log):
    """Run both references; record every case's two answers. Returns {ref: {index: answer}} and the run problems."""
    img = interp_rig.image(doc)
    (out / 'image.sha256').write_text(answers.digest(img) + '\n')
    host = os.environ.get('NATEMU_X86_HOST')
    if not host: sys.exit('seeding the interp references needs the x86 reference machine: NATEMU_X86_HOST=user@host')
    runs, problems = {}, []
    for ref, run in (('qemu', lambda: interp_rig.run_qemu(img, out)), ('kvm', lambda: interp_rig.run_kvm(img, out, host))):
        rc, text, info = run()
        (out / f'{ref}-com1.txt').write_text(text)
        (out / f'{ref}-reference.json').write_text(json.dumps(info, indent=1, sort_keys=True) + '\n')
        got, bad = interp_rig.parse(text, doc)
        if rc != 1: bad.append(f'exit status {rc} (isa-debug-exit 0 is 1; 0x40|vector a fault: (v<<1)|1; 0 a triple fault)')
        problems += [f'{ref}: {b}' for b in bad]
        log(f'{ref}: {len(got)} of {len(doc["cases"])} cases reported, exit {rc}, {info}')
        repro = {'supplier': f'interp-{ref}', 'reference': ref, **({'pin': interp_rig.QEMU_PIN} if ref == 'qemu' else {}),
                 'corpus': f'{interp_cases.__name__} seed {doc["seed"]}', 'args': interp_rig.qemu_args('IMG', 'COM1') if ref == 'qemu'
                 else interp_rig.kvm_args('IMG', 'COM1')}
        if ref == 'kvm': repro.update({k: info.get(k, '?') for k in ('cpu', 'kernel', 'qemu', 'virt')})
        runs[ref] = {}
        for i, c in enumerate(doc['cases']):
            if i not in got: continue
            a = interp_rig.answer(c, *got[i], ref)
            runs[ref][i] = a
            answers.record(interp_rig.key(c, ref, doc['common']), answers.canon(a), {**repro, 'program_sha256': c['hash']})
    return runs, problems


def locked(doc):
    """Every case's two answers from the lock; one it lacks is reported missing (not seeded), as is every answer when
    there is no lock yet."""
    runs = {'qemu': {}, 'kvm': {}}
    have = pathlib.Path(os.environ.get('NATEMU_ANSWERS_LOCK', '')).is_file()
    for ref in runs:
        for i, c in enumerate(doc['cases']):
            k = interp_rig.key(c, ref, doc['common'])
            e = answers.find(k) if have else None
            if e is None: answers.Missing(k); continue
            if e.value is None: continue          # a hash-only answer: the case reports it as missing a value
            runs[ref][i] = json.loads(e.value)
    return runs


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--out', required=True, type=pathlib.Path)
    ap.add_argument('--seed', type=int, default=interp_cases.SEED)
    a = ap.parse_args(argv)
    a.out.mkdir(parents=True, exist_ok=True)
    doc = interp_cases.corpus(a.seed)
    (a.out / 'cases.json').write_text(json.dumps(doc, indent=1, sort_keys=True) + '\n')
    log = lambda s: print(s, flush=True)
    log(f'{len(doc["cases"])} cases, corpus sha256 {interp_rig.corpus_digest(doc)}')
    mode = answers.mode()
    if mode == 'seed': runs, problems = seed(doc, a.out, log)
    elif mode == 'locked': runs, problems = locked(doc), []
    else: sys.exit('run through the framework (locked answers) or seed: NATEMU_ANSWERS=live has no reference to compare')
    results, failed = [], 0
    for i, c in enumerate(doc['cases']):
        q, k = runs['qemu'].get(i), runs['kvm'].get(i)
        diffs = []
        if q is None: diffs.append('no QEMU answer')
        if k is None: diffs.append('no KVM answer')
        if q is not None and k is not None: diffs += interp_rig.disagreements(c, q, k)
        for ref, got in (('qemu', q), ('kvm', k)):     # where the generator meant the case to stop
            if got is not None and 'stop' in got and got['stop'] != c['expect_stop']:
                diffs.append(f'{ref} stopped at stop {got["stop"]}, the case means stop {c["expect_stop"]}')
        if c['group'] == 'Rig':
            for ref, got in (('qemu', q), ('kvm', k)):
                want = expected_empty(c, ref)
                if got is not None and got != want:
                    diffs += [f'{ref} {n}: {interp_rig._show(got.get(n))}, the starting state has {interp_rig._show(want.get(n))}'
                              for n in want if got.get(n) != want.get(n)]
        failed += bool(diffs)
        results.append({'name': c['name'], 'outcome': 'fail' if diffs else 'match', 'diffs': diffs})
    for p in problems: results.append({'name': 'run', 'outcome': 'fail', 'diffs': [p]}); failed += 1
    (a.out / 'results.json').write_text(json.dumps({'results': results}, indent=1) + '\n')
    for r in results:
        if r['outcome'] == 'fail': log(f'FAIL {r["name"]}: {"; ".join(r["diffs"])[:600]}')
    log(f'{len(results) - failed} of {len(results)} pass')
    sys.exit(1 if failed else 0)


if __name__ == '__main__':
    testenv.require_framework(__file__)
    main()
