"""natemu's side of the interp cases without a VM (run_interp_natemu.py): each case's machine description says the case's
whole state, memory, stops and windows; the judge compares natemu's fields with the right reference under the masks."""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'tests' / 'lib')); import testenv  # noqa: E401,E402,E702
import functools
import unittest

import interp_cases as G
import interp_rig as R
import run_interp_natemu as N

num = R.num


@functools.cache
def corpus():
    return G.corpus()


def starting_fields(case):
    """The fields (interp_rig.fields() form) of a case's starting state: what an empty case must read back."""
    st = case['state']
    mem = {}
    for b in case['memory']:
        a = num(b['addr']); mem.update({a + i: x for i, x in enumerate(bytes.fromhex(b['data']))})
    out = {'stop': 0, **{r: num(st['gpr'][r]) for r in G.GPRS}, 'eflags': num(st['eflags']),
           **{s: num(st['seg'][s]['sel']) for s in G.SREGS}, **{k: num(st[k]) for k in ('cr0', 'cr3', 'cr4', 'efer')},
           'gdtr': [num(st['gdtr']['limit']), num(st['gdtr']['base'])], 'idtr': [num(st['idtr']['limit']), num(st['idtr']['base'])]}
    for i, w in enumerate(case['compare']['windows']):
        out[f'window{i}'] = bytes(mem.get(num(w['addr']) + k, 0) for k in range(w['len'])).hex()
    return out


def parse(text):
    """The description's items: (word, rest) per line."""
    return [tuple(line.split(' ', 1)) if ' ' in line else (line, '') for line in text.splitlines()]


class Description(unittest.TestCase):
    def test_every_case_says_its_whole_state(self):
        doc = corpus()
        for c in doc['cases']:
            items = parse(N.description(c, doc['common']))
            words = [w for w, _ in items]
            self.assertEqual(words[0], 'machine', c['name'])
            mem = [rest for w, rest in items if w == 'memory']
            blocks = [*doc['common'], *c['memory'], c['code']]
            # Exactly those blocks: not the rig's own bytes (capture blobs, P16's prologue).
            self.assertEqual(mem, [f"{num(b['addr']):#x} {b['data']}" for b in blocks if b['data']], c['name'])
            st = c['state']
            one = {w: rest for w, rest in items if w not in ('memory', 'seg', 'stop', 'window')}
            self.assertEqual(one['rip'], hex(num(st['eip'])))
            self.assertEqual(one['rflags'], hex(num(st['eflags'])))
            gpr = [int(v, 16) for v in one['gpr'].split()]
            self.assertEqual(gpr, [num(st['gpr'][r]) for r in G.GPRS] + [0] * 8)
            for k in ('cr0', 'cr3', 'cr4', 'efer'):
                self.assertEqual(int(one[k], 16), num(st[k]), (c['name'], k))
            for t in ('gdtr', 'idtr'):
                self.assertEqual([int(v, 16) for v in one[t].split()], [num(st[t]['base']), num(st[t]['limit'])])
            segs = {rest.split()[0]: [int(v, 16) for v in rest.split()[1:]] for w, rest in items if w == 'seg'}
            self.assertEqual(segs, {s: [num(st['seg'][s][k]) for k in ('sel', 'base', 'limit', 'attr')] for s in G.SREGS})
            stops = [int(rest, 16) for w, rest in items if w == 'stop']
            self.assertEqual(stops, [num(s['linear']) for s in sorted(c['stops'], key=lambda s: s['id'])])
            wins = [rest for w, rest in items if w == 'window']
            self.assertEqual(wins, [f"{num(w['addr']):#x} {w['len']:#x}" for w in c['compare']['windows']])

    def test_selftests_are_64_bit_machine_descriptions(self):
        for name, case, want in N.selftests():
            text = N.description(case, corpus()['common'])
            self.assertIn(f"seg cs 0x28 0x0 0xffffffff {G.loaded(0x28)['attr']:#x}", text, name)
            self.assertIn(f'stop {G.LAND:#x}', text)
            self.assertEqual(set(want), {'stop', *G.GPRS, 'eflags', *G.SREGS, 'cr0', 'cr3', 'cr4', 'efer', 'gdtr', 'idtr',
                                         'window0'})


class Judge(unittest.TestCase):
    def test_empty_cases_judged_against_their_starting_state(self):
        for c in (c for c in corpus()['cases'] if c['group'] == 'Rig'):
            f = starting_fields(c)
            locked = {ref: R.project(c, f, ref) for ref in G.REFERENCES}
            self.assertEqual(N.judge(c, f, locked), [], c['name'])
            self.assertEqual(N.judge(c, {**f, 'eax': f['eax'] ^ 1}, locked), ['eax: natemu 0x%x, qemu 0x%x' % (f['eax'] ^ 1, f['eax'])])
            self.assertTrue(N.judge(c, {**f, 'stop': None}, locked))
            self.assertTrue(N.judge(c, {**f, 'window0': '00' * 64}, locked) or f['window0'] == '00' * 64)
            self.assertEqual(N.judge(c, None, locked), ['no machine output'])
            self.assertTrue(N.judge(c, f, {'qemu': None, 'kvm': None}))

    def test_masks_and_the_reference_each_field_has(self):
        doc = corpus()
        undef = next(c for c in doc['cases'] if num(c['compare']['eflags']) != G.EFLAGS_MASK)
        f = starting_fields(undef)
        locked = {ref: R.project(undef, f, ref) for ref in G.REFERENCES}
        bit = G.EFLAGS_MASK & ~num(undef['compare']['eflags'])
        self.assertEqual(N.judge(undef, {**f, 'eflags': f['eflags'] ^ (bit & -bit)}, locked), [], 'an undefined flag')
        kvm = next(c for c in doc['cases'] if 'kvm' in c['compare']['by'].values())
        field = next(k for k, v in kvm['compare']['by'].items() if v == 'kvm')
        f = starting_fields(kvm)
        locked = {'qemu': {**R.project(kvm, f, 'qemu')}, 'kvm': {**R.project(kvm, f, 'kvm')}}
        self.assertNotIn(field, locked['qemu'])
        locked['kvm'][field] = 'ff' * kvm['compare']['windows'][int(field[6:])]['len']
        self.assertEqual(N.judge(kvm, f, locked)[0].split(':')[0], field, 'judged against KVM')
        prof = next(c for c in doc['cases'] if 'profile' in c['compare']['by'].values())
        f = starting_fields(prof)
        locked = {ref: R.project(prof, f, ref) for ref in G.REFERENCES}
        reg = next(k for k, v in prof['compare']['by'].items() if v == 'profile')
        self.assertEqual(N.judge(prof, {**f, reg: f[reg] ^ 1}, locked), [], 'profile fields are not compared here')


class TypedStops(unittest.TestCase):
    """The judging of how a run ended: a typed stop with its record, a stop reached, a pending case's stop."""

    def out(self, name, native0=0, fault=(0, 0, 0)):
        return {'stop': 'resident', 'reason': f'{name} status=1 detail=0', 'native': [hex(native0), '0x0', '0x0'],
                'fault': [hex(x) for x in (*fault, 0x10100)]}

    def test_a_typed_stop_with_its_record(self):
        want = {'resident': 'interp_fault', 'fault': [14, 0x11000, 0]}
        self.assertEqual(N.typed_stop(self.out('interp_fault', fault=(14, 0x11000, 0)), want), [])
        self.assertTrue(N.typed_stop(self.out('interp_fault', fault=(13, 0, 0)), want))
        self.assertTrue(N.typed_stop(self.out('interp_fault_x', fault=(14, 0x11000, 0)), want), 'a name, not a prefix')
        self.assertTrue(N.typed_stop({'stop': 'reached', 'reason': 'stop 0'}, want))
        self.assertEqual(N.typed_stop({'stop': 'reached', 'reason': 'stop 0'}, {'stop': 0}), [])
        self.assertTrue(N.typed_stop(self.out('interp_fault'), {'stop': 0}), 'a stop where none is wanted')

    def test_pending_is_a_resident_operation_not_built(self):
        for detail, name in N.PENDING_DETAILS.items():
            self.assertEqual(N.pending_detail(self.out('interp_unsupported', detail << 24 | 0x3e03)), name)
        self.assertEqual(N.pending_detail(self.out('interp_unsupported', 0x3e03)), '', 'a form outside the first version')
        self.assertEqual(N.pending_detail(self.out('interp_fault', 1 << 24)), '')
        self.assertEqual(N.pending_detail({'stop': 'reached', 'reason': 'stop 0'}), '')

    def test_the_pending_list_names_corpus_cases(self):
        names = N.read_pending(pathlib.Path(N.__file__).with_name('natemu-pending.txt'))
        self.assertLessEqual(names, {c['name'] for c in corpus()['cases']})

    def test_interpreted_selftests_are_machine_descriptions(self):
        for name, case, want in N.interp_selftests():
            text = N.description(case, corpus()['common'])
            self.assertTrue(text.startswith('machine\n'), name)
            self.assertIn(f"rip {num(case['state']['eip']):#x}", text)
            self.assertTrue(case['stops'] and case['code']['data'], name)
            self.assertTrue(set(want) <= {'stop', 'resident', 'fault', *G.GPRS, 'eflags', 'window0'}, name)


if __name__ == '__main__':
    unittest.main()
