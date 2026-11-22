"""The interp cases' generator, format and image without a VM (docs/restructure/interp-cases.md): the corpus is
deterministic and self-consistent (each segment's hidden part is what its selector gives in its mode, every stop's linear
address is its CS base plus its EIP, memory never overlaps), it covers section 1's list in every mode, the image's table
says what the corpus says, and each answer is the record under the case's masks."""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'tests' / 'lib')); import testenv  # noqa: E401,E402,E702
import functools
import json
import struct
import unittest

import interp_cases as G
import interp_rig as R

num = R.num


@functools.cache
def corpus():
    return G.corpus()


class Corpus(unittest.TestCase):
    def test_deterministic_and_named_once(self):
        a, b = corpus(), G.corpus()
        self.assertEqual(json.dumps(a, sort_keys=True), json.dumps(b, sort_keys=True))
        names = [c['name'] for c in a['cases']]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(len({c['hash'] for c in a['cases']}), len(names), 'two cases with one hash share their answers')

    def test_hidden_parts_follow_the_selectors(self):
        for c in corpus()['cases']:
            st = c['state']
            for s in G.SREGS:
                seg = {k: num(v) for k, v in st['seg'][s].items()}
                want = G.real(seg['sel'], s == 'cs') if c['mode'] in ('R', 'P16') else G.loaded(seg['sel'])
                self.assertEqual(seg, want, (c['name'], s))
            cs_d = num(st['seg']['cs']['attr']) & 0x4000
            self.assertEqual(bool(cs_d), G.MODES[c['mode']]['bits'] == 32, c['name'])
            if c['mode'] in ('P32', 'C32'):       # every starting descriptor is in the GDT, accessed
                for s in G.SREGS:
                    self.assertTrue(G.DESC[num(st['seg'][s]['sel']) & ~7] >> 40 & 1, (c['name'], s))

    def test_control_state_is_the_modes(self):
        for c in corpus()['cases']:
            st, m = c['state'], c['mode']
            cr0, cr4, efer, fl = num(st['cr0']), num(st['cr4']), num(st['efer']), num(st['eflags'])
            self.assertEqual(st['cpl'], 0)
            self.assertEqual(bool(cr0 & G.CR0_PE), m != 'R', c['name'])
            self.assertEqual(bool(cr0 & G.CR0_PG), m == 'C32', c['name'])
            if m == 'C32':
                self.assertTrue(cr4 & G.CR4_PAE and efer & G.EFER_LME and efer & G.EFER_LMA, c['name'])
                self.assertEqual(num(st['cr3']), G.PML4)
            else: self.assertFalse(efer & G.EFER_LMA, c['name'])
            self.assertTrue(fl & 2 and not fl & 0x100 and not fl & 0x30000, (c['name'], hex(fl)))   # bit 1; no TF, RF, VM

    def test_memory_never_overlaps(self):
        common = [(num(b['addr']), len(bytes.fromhex(b['data']))) for b in corpus()['common']]
        for c in corpus()['cases']:
            spans = [(num(m['addr']), len(bytes.fromhex(m['data'])), 'memory') for m in c['memory']]
            spans.append((num(c['code']['addr']), len(bytes.fromhex(c['code']['data'])), 'code'))
            spans += [(num(b['addr']), len(bytes.fromhex(b['data'])), b['what']) for b in c['rig']['bytes']]
            spans += [(a, n, 'common') for a, n in common]
            spans.sort()
            for (a, n, w), (b, _, v) in zip(spans, spans[1:]):
                self.assertLessEqual(a + n, b, f'{c["name"]}: {w} at {a:#x} overlaps {v} at {b:#x}')
            for a, n, w in spans:
                self.assertFalse(a < 0x100000 and a + n > 0xa0000, f'{c["name"]}: {w} in the legacy hole')
                self.assertTrue(w == 'common' or a >= 0x10000, f'{c["name"]}: {w} at {a:#x} in the stub\'s area')

    def test_windows_start_known(self):
        """Every compared byte has a starting value the case states (its memory or a common block)."""
        common = {}
        for b in corpus()['common']:
            a = num(b['addr']); common.update({a + i: x for i, x in enumerate(bytes.fromhex(b['data']))})
        for c in corpus()['cases']:
            mem = set(common)
            for m in c['memory']: mem |= set(range(num(m['addr']), num(m['addr']) + len(bytes.fromhex(m['data']))))
            for w in c['compare']['windows']:
                a = num(w['addr'])
                self.assertTrue(set(range(a, a + w['len'])) <= mem, (c['name'], hex(a)))

    def test_stops_are_cs_base_plus_eip(self):
        for c in corpus()['cases']:
            for s in c['stops']:
                sel, eip, end = num(s['cs']), num(s['eip']), s['mode']
                base = sel << 4 if end == 'R' or (end == 'P16' and sel == num(c['state']['seg']['cs']['sel'])) else G.loaded(sel)['base']
                if end == 'L64': base = 0
                self.assertEqual(num(s['linear']), base + eip, (c['name'], s))
            self.assertIn(c['expect_stop'], [s['id'] for s in c['stops']])
            self.assertEqual(num(c['stops'][0]['linear']), G.CODE + len(bytes.fromhex(c['code']['data'])))

    def test_section_1_is_covered_in_every_mode(self):
        """One case at least per first-version form group and mode (R, P32, C32; P16 has its stretch)."""
        need = ['mov r/m', 'mov r', 'imm', 'moffs', 'movzx', 'lea', *G.ALU, 'test', 'inc', 'dec', 'shl', 'shr', 'sar', 'bt ',
                'bts', 'btr', 'btc', 'bit string', 'lock bts', 'lock xadd', *(f'j{cc} ' for cc in G.CC), 'jmp rel8', 'loop',
                'call', 'ret', 'jmp far ptr16:16', 'jmp far ptr16:32', 'jmp far m16:32', 'retf', 'push r', 'pop r', 'push imm',
                'pushf', 'popf', 'mov ds', 'mov ss', 'mov r16, ', 'lgdt m (o16)', 'lgdt m (o32)', 'lidt', 'mov r32, cr0',
                'mov cr0', 'mov cr3', 'mov cr4', 'rdmsr', 'wrmsr', 'cpuid', 'cli', 'cld', 'pause', 'wbinvd', 'nop']
        for mode in ('R', 'P32', 'C32'):
            forms = [c['form'] for c in corpus()['cases'] if c['mode'] == mode]
            missing = [n for n in need if not any(f.startswith(n) or f'({n}' in f or f' {n}' in f' {f}' for f in forms)]
            self.assertEqual(missing, [], mode)
        p16 = {c['form'] for c in corpus()['cases'] if c['mode'] == 'P16'}
        for n in ('jmp far ptr16:16', 'jmp far ptr16:32', 'jmp far m16:32', 'lgdt m (o32)', 'mov cr0, r32 (clear PE: P16 -> R)'):
            self.assertIn(n, p16)
        ends = {s['mode'] for c in corpus()['cases'] for s in c['stops']}
        self.assertEqual(ends, set(G.END_MODES))


    def test_raw_bytes_decode_as_one_instruction_each(self):
        """A `db` line is one instruction in the case's mode (ndisasm, nasm's disassembler, decodes it so)."""
        import subprocess
        import tempfile
        for c in corpus()['cases']:
            if not any(l.startswith('db ') for l in c['code']['asm']): continue
            lines = [l for l in c['code']['asm'] if not l.endswith(':')]
            with tempfile.NamedTemporaryFile(suffix='.bin') as f:
                f.write(bytes.fromhex(c['code']['data'])); f.flush()
                out = subprocess.run(['ndisasm', '-b', str(G.MODES[c['mode']]['bits']), f.name], capture_output=True, text=True,
                                     check=True).stdout.splitlines()
            self.assertEqual(len([o for o in out if not o.startswith(' ')]), len(lines), (c['name'], out))
            self.assertFalse(any('db 0x' in o for o in out), (c['name'], out))


class Image(unittest.TestCase):
    def test_the_table_says_what_the_corpus_says(self):
        doc = corpus()
        img = R.image(doc)
        t = img[R.TABLE - R.IMAGE_BASE:]
        self.assertEqual(t[:8], b'IRCASES1')
        n, common = struct.unpack_from('<II', t, 8)
        self.assertEqual(n, len(doc['cases']))
        ncommon = struct.unpack_from('<I', t, common)[0]
        self.assertEqual([struct.unpack_from('<II', t, common + 4 + 12 * i) for i in range(ncommon)],
                         [(num(b['addr']), len(bytes.fromhex(b['data']))) for b in doc['common']])
        for i in (13, 100, 1000, n - 1):          # (cases 0-11 are empty: no code block)
            c = doc['cases'][i]
            self.assertTrue(c['code']['data'])
            o = struct.unpack_from('<I', t, 16 + 4 * i)[0]
            gpr = struct.unpack_from('<8I', t, o)
            self.assertEqual(gpr[0], num(c['state']['gpr']['eax']))
            self.assertEqual(gpr[4], num(c['rig']['entry_esp']))
            self.assertEqual(struct.unpack_from('<I', t, o + 0x58)[0], num(c['rig']['entry_eip']))
            self.assertEqual(t[o + 0x5c], R.MODE_CODE[c['mode']])
            nm = struct.unpack_from('<I', t, o + 0x60)[0]
            blocks = [struct.unpack_from('<III', t, o + 0x64 + 12 * k) for k in range(nm)]
            code = next(b for b in blocks if b[0] == G.CODE)
            self.assertEqual(t[code[2]:code[2] + code[1]].hex(), c['code']['data'])
            nw = struct.unpack_from('<I', t, o + 0x64 + 12 * nm)[0]
            self.assertEqual(nw, len(c['compare']['windows']))

    def test_capture_blobs(self):
        for end in G.END_MODES:
            a, b = R.capture(end, 0), R.capture(end, 7)
            self.assertEqual(len(a), len(b))
            self.assertIn((7).to_bytes(4, 'little'), b)
        self.assertIn(bytes.fromhex('ff2c25') + R.L64_FP.to_bytes(4, 'little'), R.capture('L64', 0))   # m16:32: AMD has no m16:64
        self.assertNotIn(bytes.fromhex('48ff2c25'), R.capture('L64', 0))


class Answers(unittest.TestCase):
    def test_masks_and_who_answers(self):
        c = next(x for x in corpus()['cases'] if x['form'].startswith('cpuid'))
        rec = bytes(range(R.REC_SIZE))
        q, k = R.answer(c, rec, [b'\x01'] * len(c['compare']['windows']), 'qemu'), R.answer(c, rec, [b'\x01'] * len(c['compare']['windows']), 'kvm')
        for r in ('eax', 'ebx', 'ecx', 'edx'): self.assertNotIn(r, q)       # natemu's profile answers CPUID's outputs
        self.assertEqual(q['eflags'], struct.unpack_from('<I', rec, 0x24)[0] & num(c['compare']['eflags']))
        self.assertEqual(R.disagreements(c, q, k), [])
        k2 = dict(k, esi=k['esi'] ^ 1)
        self.assertEqual(len(R.disagreements(c, q, k2)), 1)
        far = next(x for x in corpus()['cases'] if x['form'] == 'jmp far ptr16:32' and x['mode'] == 'P32')
        only = [f for f, v in far['compare']['by'].items() if v == 'kvm']
        self.assertTrue(only)
        a = R.answer(far, rec, [b'\x02'] * len(far['compare']['windows']), 'qemu')
        b = R.answer(far, rec, [b'\x03'] * len(far['compare']['windows']), 'kvm')
        self.assertTrue(all(f not in a for f in only) and all(f in b for f in only))
        self.assertTrue(all(d.split(':')[0] in far['compare']['by'] or d.startswith('window') for d in R.disagreements(far, a, b)))

    def test_keys_follow_what_decides_the_answer(self):
        doc = corpus(); c, common = doc['cases'][5], doc['common']
        self.assertEqual(R.key(c, 'qemu', common), R.key(dict(c, name='other', note='x'), 'qemu', common))
        self.assertNotEqual(R.key(c, 'qemu', common), R.key(c, 'kvm', common))
        self.assertNotEqual(R.key(c, 'qemu', common), R.key(dict(c, hash='0' * 64), 'qemu', common))
        moved = [dict(common[0], data=common[0]['data'][:-2] + 'ff'), *common[1:]]
        self.assertNotEqual(R.key(c, 'qemu', common), R.key(c, 'qemu', moved))

    def test_the_rig_names_the_frameworks_references(self):
        sys.path.insert(0, str(R.ROOT / 'tools/bench/smp'))
        import smp
        self.assertEqual(R.QEMU_CPU, smp.SYSTEM_CPU)
        try:
            from testkit import artifacts
            from testkit.harness import campaign
        except ImportError:
            self.skipTest('testkit not importable here')
        self.assertEqual(R.KVM_IMAGE, artifacts.BENCH_BUILDER)
        self.assertEqual(R.KVM_CPU, campaign.REFERENCE_CPU)
        self.assertEqual(R.snapshot(), campaign._snap())


if __name__ == '__main__':
    unittest.main()
