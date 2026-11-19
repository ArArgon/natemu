"""tools/k1_expected.py against QEMU 11.1.2: every K1 program the runner judges by a whole expected state, at 1-4 CPUs,
hashes like the answer QEMU gave when it still decided them (tools/reference/crosscheck/k1.json)."""
import os, pathlib, sys, tempfile, unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / 'tools'), str(ROOT / 'tools' / 'reference'), str(ROOT / 'tools' / 'bench' / 'smp')]
os.environ.setdefault('NATEMU_QEMU_PIN', 'qemu-system 11.1.2 src')     # part of an answer's key
import answers, crosscheck, smp  # noqa: E402
import k1_expected as E, run_k1 as K  # noqa: E402


class K1Expected(unittest.TestCase):
    def test_every_case_equals_qemus_answer(self):
        self.assertEqual(crosscheck.load('k1')['pin'], os.environ['NATEMU_QEMU_PIN'])
        K.set_link_cap(K.LINK_CAP)
        with tempfile.TemporaryDirectory() as d:
            stub = smp.build_stub(pathlib.Path(d))
        bad, count = [], 0
        for n in (1, 2, 3, 4):
            for name in E.names():
                prog = K.adv_program(name[4:], n) if name.startswith('adv-') else K.program(name, n, 50)
                key, _ = smp.answer_key(prog, smp.build_image(prog, stub))
                why = crosscheck.agrees('k1', key, answers.system_projection(E.expected(name, n, 50, prog)))
                if why: bad.append(f'{name}@{n}: {why}')
                count += 1
        self.assertEqual(count, 160)
        self.assertEqual(bad, [])


if __name__ == '__main__': unittest.main()
