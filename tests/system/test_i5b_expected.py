"""tests/system/i5b_expected.py against QEMU 11.1.2: every I5b image program's expected state, under the runner's masks, hashes
like the masked view of QEMU's answer that the runner compared (tools/reference/crosscheck/i5b.json)."""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'tests' / 'lib')); import testenv  # noqa: E401,E402 (tests/README.md)
import os, pathlib, sys, tempfile, unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
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

    def test_masked_x87_de_is_derived_for_fxsave_full_stack_alone(self):
        # The only program whose expected state the documented deviation changes: the prelude's FLD m64 of 2^-1060, then
        # FXSAVE; the deviated state differs only in the saved FSW's DE (byte +0x1002, 02H against 00H).
        tagged = {prog['name']: E.deviation(prog, mask) for prog, mask in I.image_cases() if E.deviation(prog, mask)}
        self.assertEqual(tagged, {'fxsave-full-stack': 'masked-x87-de'})
        prog, mask = next(c for c in I.image_cases() if c[0]['name'] == 'fxsave-full-stack')
        exact, deviated = (E.expected(prog, mask, t)['memory'][0]['data'] for t in (None, 'masked-x87-de'))
        self.assertEqual([(k, exact[k], deviated[k]) for k in range(len(exact)) if exact[k] != deviated[k]], [(0x1002, 0x02, 0x00)])


if __name__ == '__main__': unittest.main()
