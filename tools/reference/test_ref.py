"""Self-check: python3 -m unittest discover -s tools/reference   (end-to-end part needs Docker + the pinned image).

The end-to-end programs are compared with a tiny independent Python model of ADD, MOV-to-32-bit register and a store.
"""
import json, os, pathlib, shutil, subprocess, sys, unittest
HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import flags, ref, build_slice1

RAW = pathlib.Path(os.environ.get('NATEMU_RAW_ROOT', HERE.parent.parent.parent / 'natemu-raw')) / 'reference-slice1-20261026-01'
QEMU = RAW / 'pin' / 'qemu-x86_64-static'
M64 = 2**64 - 1

def model_add(a, b):                     # 64-bit ADD: result and (CF PF AF ZF SF OF) per the SDM definitions
    r = (a + b) & M64
    f = (a + b > M64) * flags.CF | (bin(r & 0xff).count('1') % 2 == 0) * flags.PF | (((a ^ b ^ r) & 0x10) != 0) * flags.AF
    f |= (r == 0) * flags.ZF | (r >> 63) * flags.SF | ((((a ^ r) & (b ^ r)) >> 63) & 1) * flags.OF
    return r, f

def prog(code_hex, gpr, memory=(), insns=None):   # insns=None deliberately omitted -> rejected by norm
    g = dict(gpr)
    return {'name': 'selfcheck', 'code': code_hex, 'rip': '0x100000', 'gpr': [g.get(i, 0x1000 + i) for i in range(16)], 'rflags': '0x0',
            'memory': [{'addr': hex(a), 'data': d.hex()} for a, d in memory], 'insns': insns}

class Rules(unittest.TestCase):
    def test_undefined_flag_rules(self):
        step = lambda op, **k: flags.step({'op': op, **k}, 0)
        self.assertEqual(step('and'), flags.AF)
        self.assertEqual(step('mul'), flags.SF | flags.ZF | flags.AF | flags.PF)
        self.assertEqual(step('shl', width=32, count=0), 0)                      # count 0: flags untouched
        self.assertEqual(step('shl', width=32, count=1), flags.AF)
        self.assertEqual(step('shl', width=32, count=2), flags.AF | flags.OF)
        self.assertEqual(step('shl', width=64, count=64), 0)                     # masked to 0
        self.assertEqual(step('shl', width=8, count=8), flags.AF | flags.OF | flags.CF)   # SHL/SHR: CF undefined for count >= width
        self.assertEqual(step('shr', width=8, count=9), flags.AF | flags.OF | flags.CF)
        self.assertEqual(step('shl', width=8, count=7), flags.AF | flags.OF)
        self.assertEqual(step('sar', width=8, count=9), flags.AF | flags.OF)       # SAR: CF is the sign bit, defined
        self.assertEqual(step('bt'), flags.OF | flags.SF | flags.AF | flags.PF)    # CF defined, ZF unaffected
        self.assertEqual(step('bsf', src_nonzero=True), flags.CF | flags.OF | flags.SF | flags.AF | flags.PF)
        self.assertEqual(step('div'), flags.ARITH)
        self.assertEqual(step('xchg'), 0)
        self.assertEqual(step('rol', width=32, count=1), 0)
        self.assertEqual(step('rol', width=32, count=2), flags.OF)
        self.assertEqual(flags.analyze([{'op': 'mul'}, {'op': 'add'}]), 0)       # a later write defines them again
    def test_consuming_undefined_flag_is_rejected(self):
        with self.assertRaises(ValueError): flags.analyze([{'op': 'mul'}, {'op': 'setcc', 'cc': 'e'}])
        with self.assertRaises(ValueError): flags.analyze([{'op': 'shr', 'width': 8, 'count': 8}, {'op': 'adc'}])   # random-03 pattern
        self.assertEqual(flags.analyze([{'op': 'shr', 'width': 8, 'count': 8}, {'op': 'cmp'}, {'op': 'adc'}]), 0)
        with self.assertRaises(ValueError): flags.analyze([{'op': 'bsf'}])                                          # zero source not excluded
    def test_layout_checks(self):
        p = ref.norm(prog('4801d8', {}, [(ref.HARNESS, b'x')], insns=[{'op': 'add', 'width': 64}]))
        with self.assertRaises(ValueError): ref.build_elf(p)                      # region overlaps harness
        with self.assertRaises(ValueError): ref.norm(prog('4801d8', {}))          # annotations are mandatory
    def test_annotation_must_match_disassembly(self):
        self.assertTrue(ref.mnemonic_ok({'op': 'movzx'}, 'movzbl') and ref.mnemonic_ok({'op': 'setcc', 'cc': 'ne'}, 'setne'))
        self.assertFalse(ref.mnemonic_ok({'op': 'add'}, 'sub') or ref.mnemonic_ok({'op': 'cmp'}, 'cmpxchg'))
    def test_compare_masks_undefined_bits(self):
        res = {'qemu': {'gpr': [hex(i) for i in range(16)], 'rflags': '0x246', 'memory': []}, 'rflags_compare_mask': hex(flags.ARITH & ~flags.AF)}
        res['qemu']['rip'] = '0x10'
        self.assertEqual(ref.compare(res, list(range(16)), 0x256, 0x10, []), [])        # AF differs but is masked
        self.assertEqual(len(ref.compare(res, list(range(16)), 0x247, 0x10, [])), 1)    # CF differs
        self.assertEqual(len(ref.compare(res, list(range(16)), 0x246, 0x11, [])), 1)    # RIP differs
        res['qemu']['memory'] = [{'addr': '0x10', 'data': '00'}, {'addr': '0x20', 'data': '00'}]
        self.assertEqual(len(ref.compare(res, list(range(16)), 0x246, 0x10, [{'addr': '0x10', 'data': '00'}])), 1)       # missing region
        self.assertEqual(len(ref.compare(res, list(range(16)), 0x246, 0x10, [{'addr': '0x10', 'data': '00'}, {'addr': '0x28', 'data': '00'}])), 1)  # wrong address

@unittest.skipUnless(QEMU.exists(), f'pinned QEMU not at {QEMU}')
class EndToEnd(unittest.TestCase):
    def test_against_python_model(self):
        a, b = 0x7fffffffffffffff, 0x0123456789abcdef
        progs = [
            prog('4801d8', {0: a, 3: b}, insns=[{'op': 'add', 'width': 64}]),                                  # add rax, rbx
            prog('89c8' '31d2', {0: 1, 1: 0xffffffffffffffff, 2: 7}, insns=[{'op': 'mov', 'width': 32}, {'op': 'xor', 'width': 32}]),   # mov eax, ecx ; xor edx, edx
            prog('488907', {0: 0x1122334455667788, 7: 0x200008}, [(0x200000, bytes(range(16)))], [{'op': 'mov', 'width': 64}]),          # mov [rdi], rax
            # prefix 3 of: add rax, rbx ; mov [rdi], rax ; xor edx, edx -> the state before the store; prefix 0: the input
            prog('4801d8' '488907' '31d2', {0: a, 3: b, 7: 0x200008}, [(0x200000, bytes(range(16)))], [{'op': 'add', 'width': 64}, {'op': 'mov', 'width': 64}, {'op': 'xor', 'width': 32}]) | {'prefix': 3},
            prog('4801d8', {0: a, 3: b}, insns=[{'op': 'add', 'width': 64}]) | {'prefix': 0},
        ]
        work = RAW / 'selfcheck'; shutil.rmtree(work, ignore_errors=True)
        try: results, _ = ref.run_programs(progs, work, QEMU)
        except subprocess.CalledProcessError as e: self.skipTest(f'docker/image unavailable: {e}')
        q = [r['qemu'] for r in results]
        r, f = model_add(a, b)
        self.assertEqual(int(q[0]['gpr'][0], 16), r); self.assertEqual(int(q[0]['rflags'], 16), f | flags.FIXED)
        self.assertEqual(int(q[0]['gpr'][3], 16), b)                                                           # untouched registers survive capture
        self.assertEqual(int(q[1]['gpr'][0], 16), 0xffffffff)                                                  # 32-bit write zero-extends
        self.assertEqual(int(q[1]['gpr'][2], 16), 0); self.assertEqual(int(q[1]['rflags'], 16), flags.PF | flags.ZF | flags.FIXED)
        self.assertEqual(int(results[1]['undefined_flags'], 16), flags.AF)                                      # xor: AF undefined, masked out
        want = bytearray(range(16)); want[8:16] = (0x1122334455667788).to_bytes(8, 'little')
        self.assertEqual(q[2]['memory'][0]['data'], bytes(want).hex())
        self.assertEqual(int(q[2]['rip'], 16), 0x100003)
        self.assertEqual(ref.compare(results[2], [int(x, 16) for x in q[2]['gpr']], int(q[2]['rflags'], 16), int(q[2]['rip'], 16), q[2]['memory']), [])
        self.assertEqual((int(q[3]['gpr'][0], 16), int(q[3]['gpr'][2], 16), int(q[3]['rip'], 16)), (r, 0x1002, 0x100003))  # add ran, xor did not
        self.assertEqual(q[3]['memory'][0]['data'], bytes(range(16)).hex())                                    # the store did not run
        self.assertEqual((int(q[3]['rflags'], 16), results[3]['prefix'], results[3]['undefined_flags']), (f | flags.FIXED, 3, ref.hx(0)))
        self.assertEqual((int(q[4]['gpr'][0], 16), int(q[4]['rip'], 16), int(q[4]['rflags'], 16)), (a, 0x100000, flags.FIXED))
        with self.assertRaises(ValueError): ref.run_programs([progs[3] | {'prefix': 4}], work, QEMU)           # not an instruction boundary

    def test_segment_bases(self):     # I7: fsbase/gsbase reach QEMU; linear = base + EA mod 2^64 (a negative GS base wraps)
        fs, gs, d = 0x7ffe_0000_1000, 0xffff_8000_dead_0000, bytes(range(32))
        p = prog('64488b03' '65488b0e', {3: (0x200000 - fs) & M64, 6: (0x200008 - gs) & M64}, [(0x200000, d)],
                 [{'op': 'mov', 'width': 64}] * 2) | {'fsbase': hex(fs), 'gsbase': hex(gs)}     # mov rax, fs:[rbx]; mov rcx, gs:[rsi]
        work = RAW / 'selfcheck-segments'; shutil.rmtree(work, ignore_errors=True)
        try: results, _ = ref.run_programs([p], work, QEMU)
        except subprocess.CalledProcessError as e: self.skipTest(f'docker/image unavailable: {e}')
        g = [int(x, 16) for x in results[0]['qemu']['gpr']]
        self.assertEqual((g[0], g[1]), (int.from_bytes(d[:8], 'little'), int.from_bytes(d[8:16], 'little')))
        with self.assertRaises(ValueError): ref.norm(p | {'fsbase': hex(1 << 47)})                             # non-canonical base

@unittest.skipUnless(QEMU.exists(), f'pinned QEMU not at {QEMU}')
class ControlFlow(unittest.TestCase):
    """Flow programs: the executed path (from QEMU's TB trace) decides the instruction count and the undefined flags."""
    def flow(self, name, lines):
        text = []
        for i, (t, _) in enumerate(lines): text += [f'I{i}:', t]
        code, labels = ref.assemble('\n'.join(text) + '\nend:', labels=True)
        return {'name': name, 'flow': True, 'code': code.hex(), 'rip': '0x100000', 'gpr': [0x1000 + i for i in range(16)], 'rflags': '0x0',
                'memory': [], 'insns': [a for _, a in lines], 'offsets': [labels[f'I{i}'] for i in range(len(lines))]}

    def test_loops_and_executed_path_flags(self):
        mov = lambda n: (f'mov ecx, {n}', {'op': 'mov', 'width': 32})
        loop = self.flow('loop', [('xor eax, eax', {'op': 'xor', 'width': 32}), mov(3), ('L1: add eax, 5', {'op': 'add', 'width': 32}),
                                  ('loop L1', {'op': 'loop'})])
        def cond(op, compare):
            return self.flow(op, [('xor eax, eax', {'op': 'xor', 'width': 32}), mov(4), ('L1: inc eax', {'op': 'inc', 'width': 32}),
                                  (f'cmp eax, {compare}', {'op': 'cmp', 'width': 32}), (f'{op} L1', {'op': op})])
        skipped = self.flow('skipped', [('mul rdx', {'op': 'mul', 'width': 64}), ('jmp L1', {'op': 'jmp'}), ('add rax, rbx', {'op': 'add', 'width': 64}),
                                        ('L1: mov rdx, rdx', {'op': 'mov', 'width': 64})])
        work = RAW / 'selfcheck-flow'; shutil.rmtree(work, ignore_errors=True)
        try: results, _ = ref.run_programs([loop, cond('loope', 'eax'), cond('loope', 2), cond('loopne', 3), skipped], work, QEMU)
        except subprocess.CalledProcessError as e: self.skipTest(f'docker/image unavailable: {e}')
        got = [(int(r['qemu']['gpr'][0], 16), int(r['qemu']['gpr'][1], 16), r['executed_insns']) for r in results]
        self.assertEqual(got[0], (15, 0, 2 + 3 * 2))
        self.assertEqual(got[1], (4, 0, 2 + 4 * 3))      # loope while equal: all four trips
        self.assertEqual(got[2], (1, 3, 2 + 3))          # eax = 1 != 2: leaves after one trip, rcx = 3
        self.assertEqual(got[3], (3, 1, 2 + 3 * 3))      # loopne while different: leaves when eax = 3
        # the jump skipped the `add`: mul's undefined SF ZF AF PF stay undefined at the end (a static analysis would say none)
        self.assertEqual((results[4]['executed_insns'], int(results[4]['undefined_flags'], 16)), (3, flags.SF | flags.ZF | flags.AF | flags.PF))

    def test_leaving_the_code_and_returning_is_refused(self):
        # jmp to a stub in a data page that jumps back: instructions after re-entry would be dropped from the path
        p = self.flow('leave-code', [('lea rbx, [rip + back]', {'op': 'lea', 'width': 64}), ('imul rdx, rdx', {'op': 'imul', 'width': 64}),
                                     ('mov eax, 0x200000', {'op': 'mov', 'width': 32}), ('jmp rax', {'op': 'jmp'}),
                                     ('back: cmp ecx, 1', {'op': 'cmp', 'width': 32})])
        p['memory'] = [{'addr': '0x200000', 'data': 'ffe3' + '00' * 0xffe}]
        work = RAW / 'selfcheck-leave'; shutil.rmtree(work, ignore_errors=True)
        try: ref.run_programs([p], work, QEMU)
        except subprocess.CalledProcessError as e: self.skipTest(f'docker/image unavailable: {e}')
        except ValueError as e: self.assertIn('left its code', str(e))
        else: self.fail('a program that leaves its code and returns must be refused')

    def test_branch_on_undefined_flag_is_rejected(self):
        with self.assertRaises(ValueError): flags.analyze([{'op': 'mul'}, {'op': 'jcc', 'cc': 'e'}])
        with self.assertRaises(ValueError): flags.analyze([{'op': 'mul'}, {'op': 'loope'}])
        self.assertEqual(flags.analyze([{'op': 'mul'}, {'op': 'cmp'}, {'op': 'jcc', 'cc': 'e'}, {'op': 'loope'}]), 0)
        self.assertEqual(flags.analyze([{'op': 'and'}, {'op': 'loopne'}]), flags.AF)       # `and` leaves ZF defined

class Slice1Fixtures(unittest.TestCase):
    def test_fixtures_build_and_pins_hold(self):    # builds missing/stale fixtures; skips only if clang/Docker/QEMU are unavailable
        try: build_slice1.build()
        except build_slice1.Unavailable as e: self.skipTest(f'slice1 fixtures not buildable: {e}')
        except subprocess.CalledProcessError as e: self.skipTest(f'slice1 fixture build failed to run: {e}')
        self.assertEqual(sorted(json.loads(build_slice1.RECORD.read_text())['sha256']), ['expected.json', 'programs.json'])

if __name__ == '__main__': unittest.main()
