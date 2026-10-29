"""Self-test of check_resident_stack.py: synthetic listings for the call-graph rules, and corruptions of the real
runtime's listing (skipped when the release runtime is not built) that each must be refused."""
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

    def problems(self, text):
        return c.check(text)[1]

    def mutate(self, pattern, replacement, count=1):
        text, n = re.subn(pattern, replacement, self.text, count=count, flags=re.M)
        self.assertEqual(n, count, pattern)
        return text

    def test_the_built_runtime_passes(self):
        self.assertEqual(self.problems(self.text), [])

    def test_corruptions_are_refused(self):
        cases = {
            'a dropped GPR restore': (r'^.*ldr\tx5, \[x16, #0x28\]\n', ''),
            'a restore from the wrong slot': (r'(ldr\tx7, \[x16, #)0x38\]', r'\g<1>0x40]'),
            'a second ERET': (r'^(\s*[0-9a-f]+:\s+)mov\tx16, x0$', r'\1eret'),
            'an unsaved register in a gate': (r'^.*stp\tq30, q31, \[sp, #0x2e0\]\n', ''),
            'an indirect call in reachable code': (r'^(\s*[0-9a-f]+:\s+)mov\tx0, sp$', r'\1blr\tx8'),
            'a stack switch outside the gates': (r'^(\s*[0-9a-f]+:\s+)mov\tx8, sp$', r'\1mov\tsp, x8'),
            'a frame beyond a reserve': (r'(sub\tsp, sp, #)0x5a0$', r'\g<1>0x5000'),
            'a stack use in the dispatcher': (r'^(\s*[0-9a-f]+:\s+)and\tx1, x0, #0xfff$', r'\1str\tx0, [sp, #-0x10]!'),
            'a literal load in the dispatcher': (r'^(\s*[0-9a-f]+:\s+)and\tx1, x0, #0xfff$', r'\1ldr\tx1, 0x1000 <_start>'),
            'a vector to the wrong place': (r'(<vectors>:\n\s*[0-9a-f]+:\s+b\t)0x[0-9a-f]+ <exception_capture>',
                                            r'\g<1>0x8000400a5000 <stop_gate>'),
        }
        for name, (pattern, replacement) in cases.items():
            with self.subTest(name):
                self.assertTrue(self.problems(self.mutate(pattern, replacement)), name)


if __name__ == '__main__':
    unittest.main()
