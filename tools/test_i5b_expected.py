"""tools/i5b_expected.py against QEMU 11.1.2: every I5b image program's expected state, under the runner's masks, hashes
like the masked view of QEMU's answer that the runner compared (tools/reference/crosscheck/i5b.json)."""
import os, pathlib, sys, tempfile, unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / 'tools'), str(ROOT / 'tools' / 'reference'), str(ROOT / 'tools' / 'bench' / 'smp')]
os.environ.setdefault('NATEMU_QEMU_PIN', 'qemu-system 11.1.2 src')     # part of an answer's key
import answers, crosscheck, smp  # noqa: E402
import i5b_expected as E, run_i5b as I  # noqa: E402


class I5bExpected(unittest.TestCase):
    def test_every_image_program_equals_qemus_answer(self):
        with tempfile.TemporaryDirectory() as d:
            stub = smp.build_stub(pathlib.Path(d))
        cases = I.image_cases()
        for prog, mask in cases:
            key, _ = smp.answer_key(prog, smp.build_image(prog, stub), cpu=I.QCPU)
            label = 'masked/' + ','.join(f'{off}+{n}' for off, n, *_ in mask)
            self.assertIsNone(crosscheck.agrees('i5b', f'{key}/{label}', answers.system_projection(E.expected(prog, mask))), prog['name'])
        self.assertEqual(len(cases), 46)


if __name__ == '__main__': unittest.main()
