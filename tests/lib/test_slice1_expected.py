"""tests/lib/slice1_expected.py against QEMU 11.1.2: every built-in probe of tests/faults/run_slice1_faults.py whose prefix it covers
hashes like QEMU's prefix answer at the case's RIP (tools/reference/crosscheck/slice1.json), and it covers all but a
few (the rest keep QEMU's answers)."""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'tests' / 'lib')); import testenv  # noqa: E401,E402 (tests/README.md)
import pathlib, sys, unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'tools'), str(ROOT / 'tools' / 'reference')]
import answers, crosscheck, ref, gen_slice1 as gen  # noqa: E402
import run_slice1_faults as F, slice1_expected as E  # noqa: E402


def probe_cases():
    """(name, program, lines, probed index, case RIP, answer key of QEMU's prefix run) as run_slice1_faults.expectations."""
    for name, regs, lines, k, mut in F.probes(True):
        rip = mut.get('rip', gen.CODE)
        p, (_, offs) = F.probe_program(name, regs, lines, rip), F.offsets(lines)
        p.update({b: hex(mut[b]) for b in ('fsbase', 'gsbase') if b in mut})
        p.update({b: mut[b] for b in ('cpu', 'sse') if b in mut})
        for i, ann in mut.get('insns', {}).items(): p['insns'][i] = ann
        mut = {m: v for m, v in mut.items() if m not in ('fsbase', 'gsbase', 'cpu', 'sse', 'insns')}
        if mut.get('nofault'): continue
        if k is None: k = next(i for i in range(len(lines)) if F.layout(p, mut).fetch(rip + offs[i], offs[i + 1] - offs[i]))
        here = rip if rip + offs[-1] <= 0x3_0000_0000 else gen.CODE + (rip & (F.PAGE - 1))
        job = ref.norm(dict(p, prefix=offs[k], rip=hex(here)))
        key, _ = answers.user_key(job, ref.qemu_args(job))
        yield name, p, lines, k, rip + offs[k], f'{key}/rip/{rip + offs[k]:#x}'


class Slice1Expected(unittest.TestCase):
    def test_every_covered_probe_equals_qemus_answer(self):
        bad, covered, total = [], 0, 0
        for name, p, lines, k, at, key in probe_cases():
            total += 1
            r = E.prefix(p, lines, k, at)
            if r is None: continue
            covered += 1
            q = r['qemu']
            why = crosscheck.agrees('slice1', key, answers.user_projection(q['gpr'], q['rflags'], q['rip'], q['memory'], ref.num(r['rflags_compare_mask'])))
            if why: bad.append(f'{name}: {why}')
        self.assertEqual(bad, [])
        self.assertGreaterEqual(covered, total - 20, f'{covered} of {total} probes covered')


    def test_every_access_form_program_equals_qemus_answers(self):
        """The paging programs at every placement: each prefix and full run QEMU's answer covered (moved by `relocate`)
        and the events derived from it."""
        import json
        recorded = crosscheck.load('slice1')['answers']
        bad, seen = [], 0
        for p in F.paging_programs():
            code, offs = F.offsets(p['asm'])
            at = [i for i, line in enumerate(p['asm']) if '[' in line or line.split()[0] in F.IMPLICIT_ACCESS]
            for where, d in F.PLACEMENTS:
                q, _ = F.relocate(p, [None], [], *d)
                rip = ref.num(q['rip'])
                for i in at + [len(p['asm'])]:
                    job = ref.norm(dict(p, prefix=offs[i]) if i < len(p['asm']) else dict(p))
                    key = f"{answers.user_key(job, ref.qemu_args(job))[0]}/relocated/{d[0]:#x}/{d[1]:#x}/{d[2]:#x}"
                    r = E.program_state(q, i, rip + offs[i])
                    if r is None: bad.append(f"{p['name']}@{where}/{i}: not covered"); continue
                    if key in recorded:
                        s = r['qemu']
                        why = crosscheck.agrees('slice1', key, answers.user_projection(s['gpr'], s['rflags'], s['rip'], s['memory'], ref.num(r['rflags_compare_mask'])))
                        if why: bad.append(f"{p['name']}@{where}/{i}: {why}")
                        seen += 1
                    if i < len(p['asm']):
                        text, nxt = q['asm'][i], rip + offs[i + 1]
                        ek = f'{key}#events/{nxt:#x}/{text}'
                        if ek in recorded:
                            v = json.loads(answers.canon(['ok', F.reference_events(r, text, F.bases(q), nxt)]))
                            why = crosscheck.agrees('slice1', ek, answers.canon(v))
                            if why: bad.append(f"{p['name']}@{where}/{i} events: {why}")
                            seen += 1
        self.assertEqual(bad, [])
        self.assertGreater(seen, 500)


if __name__ == '__main__': unittest.main()
