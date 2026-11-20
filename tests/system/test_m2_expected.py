"""tests/system/m2_expected.py against QEMU 11.1.2: `pat`, `mtrr` and `uc` at 1 and 2 CPUs hash like the answers QEMU gave
(tools/reference/crosscheck/m2.json)."""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'tests' / 'lib')); import testenv  # noqa: E401,E402 (tests/README.md)
import os, pathlib, sys, tempfile, unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'tools'), str(ROOT / 'tools' / 'reference'), str(ROOT / 'tools' / 'bench' / 'smp')]
os.environ.setdefault('NATEMU_QEMU_PIN', 'qemu-system 11.1.2 src')     # part of an answer's key
import answers, crosscheck, smp  # noqa: E402
import m2_expected as E, run_m2 as X  # noqa: E402


class M2Expected(unittest.TestCase):
    def test_every_program_equals_qemus_answer(self):
        with tempfile.TemporaryDirectory() as d:
            stub = smp.build_stub(pathlib.Path(d))
        for n in (1, 2):
            for name in X.PROGRAMS:
                prog = X.program(name, n)
                key, _ = smp.answer_key(prog, smp.build_image(prog, stub))
                self.assertIsNone(crosscheck.agrees('m2', key, answers.system_projection(E.expected(name, n, prog))), f'{name}@{n}')


if __name__ == '__main__': unittest.main()
