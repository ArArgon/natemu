"""tools/slice2_expected.py against QEMU 11.1.2: each loop probe's interpreted state hashes like the answer QEMU gave for
its unrolled form (RIP not compared, as the runner compares; tools/reference/crosscheck/slice2.json)."""
import pathlib, sys, unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / 'tools'), str(ROOT / 'tools' / 'reference')]
import answers, crosscheck, ref  # noqa: E402
import run_slice2_probes as S, slice2_expected as E  # noqa: E402


class Slice2Expected(unittest.TestCase):
    def test_every_loop_probe_equals_qemus_answer(self):
        for p in S.PROBES:
            _, reference, _ = S.build(p)
            q = ref.norm(reference)
            key, _ = answers.user_key(q, ref.qemu_args(q))
            r = E.expected(p)['qemu']
            proj = answers.user_projection(r['gpr'], r['rflags'], 0, r['memory'], E.MASK)
            self.assertIsNone(crosscheck.agrees('slice2', key + '/rip-not-compared', proj), p['name'])

    def test_add_chain_agrees_with_the_interpreter(self):
        """The two derivations (add_chain's closed form, this interpreter) on a short chain."""
        program, result, _ = S.add_chain(5, 7)
        probe = {'name': 'x', 'gpr': program['gpr'], 'memory': [],
                 'main': [('loop', 'ecx', 7, [f'add rax, {0x10000 + 977 * i:#x}' for i in range(5)])]}
        got = E.expected(probe)['qemu']
        self.assertEqual([ref.num(x) for x in got['gpr']], [ref.num(x) for x in result['qemu']['gpr']])
        self.assertEqual(ref.num(got['rflags']) & 0x8d5, ref.num(result['qemu']['rflags']) & 0x8d5)


if __name__ == '__main__': unittest.main()
