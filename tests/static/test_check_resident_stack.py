"""Self-test of check_resident_stack.py: synthetic listings for the call-graph rules, and corruptions of the real
runtime's listing (skipped when the release runtime is not built) that each must be refused."""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'tests' / 'lib')); import testenv  # noqa: E401,E402 (tests/README.md)
import pathlib, re, subprocess, sys, unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import check_resident_stack as c  # noqa: E402

TOPS = {'STACK_TOP': 0x8000_4005_4000, 'EXCEPTION_STACK_TOP': 0x8000_4006_4000}


def listing(funcs):
    """An objdump-style listing of {name: [instruction]} at consecutive addresses."""
    out, addr = [], 0x1000
    for name, insns in funcs.items():
        out.append(f'{addr:016x} <{name}>:')
        for i in insns:
            out.append(f'{addr:x}:  \t{i}')
            addr += 4
    return '\n'.join(out)


def graph(funcs):
    return c.analyse(*c.parse(listing(funcs)), TOPS)


class Synthetic(unittest.TestCase):
    def test_depth_sums_frames_along_the_deepest_call(self):
        info = graph({'a': ['sub sp, sp, #0x20', 'bl 0x1010 <b>', 'bl 0x1018 <c>', 'ret'],
                      'b': ['stp x29, x30, [sp, #-0x30]!', 'ret'],
                      'c': ['sub sp, sp, #0x10', 'add sp, sp, #0x10', 'ret']})
        d, path, problems = c.depth(info, 'a')
        self.assertEqual((d, [f for f, _ in path], problems), (0x50, ['a', 'b'], []))

    def test_recursion_is_refused(self):
        info = graph({'a': ['bl 0x1008 <b>', 'ret'], 'b': ['b 0x1000 <a>', 'ret']})
        self.assertTrue(any('recursion' in p for p in c.depth(info, 'a')[2]))

    def test_unlisted_indirect_branch_and_dynamic_stack_are_refused(self):
        for bad in ('blr x8', 'br x3', 'sub sp, sp, x8', 'mov sp, x9'):
            info = graph({'a': ['bl 0x1008 <b>', 'ret'], 'b': [bad, 'ret']})
            self.assertTrue(c.depth(info, 'a')[2], bad)
        # Unreachable code is not judged.
        info = graph({'a': ['ret'], 'b': ['blr x8', 'ret']})
        self.assertEqual(c.depth(info, 'a')[2], [])

    def test_erratum_veneer_returns_after_the_displaced_instruction(self):
        # lld (`--fix-cortex-a53-843419`, the target's default) moves an instruction at a page's last 8 bytes into a veneer
        # named after its address, which branches back to the next one; only that return is accepted.
        for back, ok in (('0x1008 <a+0x8>', True), ('0x1004 <a+0x4>', False)):
            info = graph({'a': ['add x0, x0, #0x1', 'b 0x100c <__CortexA53843419_1004>', 'ret'],
                          '__CortexA53843419_1004': ['ldr x0, [x0]', f'b {back}']})
            self.assertEqual(not any('middle' in p for p in c.depth(info, 'a')[2]), ok, back)
        # The return must land in the function whose `b` at the displaced address entered the veneer.
        info = graph({'a': ['add x0, x0, #0x1', 'nop', 'ret'], '__CortexA53843419_1004': ['ldr x0, [x0]', 'b 0x1008 <a+0x8>'],
                      'b': ['b 0x100c <__CortexA53843419_1004>', 'ret']})
        self.assertTrue(any('middle' in p for p in c.depth(info, 'b')[2]))

    def test_branch_into_another_function_body_is_refused(self):
        info = graph({'a': ['b 0x1008 <b+0x4>'], 'b': ['sub sp, sp, #0x10', 'ret']})
        self.assertTrue(any('middle' in p for p in c.depth(info, 'a')[2]))


RUNTIME = c.DEFAULT


@unittest.skipUnless(RUNTIME.exists(), 'release runtime not built')
class Binary(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = subprocess.run(['objdump', '-d', '-C', '--no-show-raw-insn', str(RUNTIME)], check=True,
                                  capture_output=True, text=True).stdout

    def mutate(self, pattern, replacement, count=1):
        text, n = re.subn(pattern, replacement, self.text, count=count, flags=re.M)
        self.assertEqual(n, count, pattern)
        return text

    def test_the_built_runtime_passes(self):
        self.assertEqual(self.problems(self.text), [])

    def problems(self, text, profile=False):
        return c.check(text, profile=profile)[1]

    def resume_time_store(self, store, at=None):
        """The EL0 sampler's resume-time store (`profile` builds) put into this runtime's resume_exception before `mov x16, x0`,
        its literal (at 0xfff000) the address `at` (default: Head::resume_time in the study part's first page, 0x7000 into
        the lookup area: a page-aligned offset)."""
        tops = c.constants()[0]
        at = tops['LOOKUP'][0] + 0x7000 + tops['HEAD_RESUME_TIME'] if at is None else at
        text = self.mutate(r'(<resume_exception>:\n(?:.*\n)*?)(\s*[0-9a-f]+:\s+)(mov\tx16, x0)$',
                           rf'\1\2mrs\tx10, cntvct_el0\n\2ldr\tx9, 0xfff000 <x>\n\2{store}\n\2\3')
        return text + f'\n  fff000: 00 00 00 00 \t.word\t{at & 0xffffffff:#010x}\n  fff004: 00 00 00 00 \t.word\t{at >> 32:#010x}\n'

    def test_the_samplers_resume_time_store_only_in_a_profile_build_and_only_to_resume_time(self):
        tops = c.constants()[0]
        self.assertEqual(self.problems(self.resume_time_store('str\tx10, [x9]'), profile=True), [])
        self.assertTrue(self.problems(self.resume_time_store('str\tx10, [x9]')))                   # not a profile build
        self.assertTrue(self.problems(self.text, profile=True))                                     # a profile build without it
        self.assertTrue(self.problems(self.resume_time_store('str\tx11, [x9]'), profile=True))
        self.assertTrue(self.problems(self.resume_time_store('str\tx10, [x8]'), profile=True))
        lo, hi = tops['LOOKUP']
        for wrong in (lo + 0x7000 + tops['HEAD_RESUME_TIME'] - 8, hi + tops['HEAD_RESUME_TIME'], tops['EXCEPTION_STACK_TOP']):
            with self.subTest(hex(wrong)):
                self.assertTrue(self.problems(self.resume_time_store('str\tx10, [x9]', wrong), profile=True))

    def test_corruptions_are_refused(self):
        cases = {
            'a dropped GPR restore': (r'^.*ldp\tx4, x5, \[x16, #0x20\]\n', ''),
            'a restore from the wrong slot': (r'(ldp\tx6, x7, \[x16, #)0x30\]', r'\g<1>0x40]'),
            # Trip-cost plan T1, T2 (step 1.1): a pair from x12's slot; the base x16 restored before x17.
            'a pair from the wrong slot': (r'(ldp\tx14, x15, \[x16, #)0x70\]', r'\g<1>0x60]'),
            # A conditional skip landing anywhere but right after what it skips (the alignment checks, X2).
            'the restore\'s alignment skip landing elsewhere': (r'(cbz\tx3, )0x[0-9a-f]+( <resume_exception\+0x[0-9a-f]+>)', r'\g<1>0x1000\2'),
            'a gate\'s alignment skip landing elsewhere': (r'(<exception_capture>:\n(?:.*\n)*?.*\ttbz\tw9, #0x1, )0x[0-9a-f]+', r'\g<1>0x1000'),
            'x16 before x17': (r'^(\s*[0-9a-f]+:\s+)ldr\tx17, \[x16, #0x88\]$', r'\1ldr\tx16, [x16, #0x80]'),
            'a second ERET': (r'^(\s*[0-9a-f]+:\s+)mov\tx16, x0$', r'\1eret'),
            'an unsaved register in a gate': (r'^.*stp\tq30, q31, \[sp, #0x2e0\]\n', ''),
            'an indirect call in reachable code': (r'^(\s*[0-9a-f]+:\s+)mov\tx0, sp$', r'\1blr\tx8'),
            'a stack switch outside the gates': (r'^(\s*[0-9a-f]+:\s+)mov\tx8, sp$', r'\1mov\tsp, x8'),
            # `returned`'s frame (its size follows the state's layout), made larger than the exception stack (80 KiB).
            'a frame beyond a reserve': (r'(<natemu_runtime::execution::returned>:\n(?:.*\n)*?.*\tsub\tsp, sp, #)0x[0-9a-f]+$', r'\g<1>0x14000'),
            'a stack use in the dispatcher': (r'^(\s*[0-9a-f]+:\s+)and\tx1, x0, #0x7fff$', r'\1str\tx0, [sp, #-0x10]!'),
            'a literal load in the dispatcher': (r'^(\s*[0-9a-f]+:\s+)and\tx1, x0, #0x7fff$', r'\1ldr\tx1, 0x1000 <_start>'),
            'a vector to the wrong place': (r'(<vectors>:\n\s*[0-9a-f]+:\s+b\t)0x[0-9a-f]+ <exception_capture>',
                                            r'\g<1>0x8000400a5000 <irq_gate>'),
        }
        for name, (pattern, replacement) in cases.items():
            with self.subTest(name):
                self.assertTrue(self.problems(self.mutate(pattern, replacement)), name)


if __name__ == '__main__':
    unittest.main()
