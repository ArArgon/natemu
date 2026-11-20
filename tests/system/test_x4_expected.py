"""run_x4.trampoline_expected against QEMU 11.1.2: at 2, 3 and 4 CPUs the expected state hashes like QEMU's answer under
the runner's IOPL mask (the `no-iopl` view it compared; tools/reference/crosscheck/x4.json)."""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'tests' / 'lib')); import testenv  # noqa: E401,E402 (tests/README.md)
import os, pathlib, sys, tempfile, unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'tools'), str(ROOT / 'tools' / 'reference'), str(ROOT / 'tools' / 'bench' / 'smp')]
os.environ.setdefault('NATEMU_QEMU_PIN', 'qemu-system 11.1.2 src')     # part of an answer's key
import answers, crosscheck, smp  # noqa: E402
import run_x4 as X  # noqa: E402


class X4Expected(unittest.TestCase):
    def test_trampoline_equals_qemus_answer(self):
        with tempfile.TemporaryDirectory() as d:
            stub = smp.build_stub(pathlib.Path(d))
        for n in (2, 3, 4):
            q = X.trampoline_program(n, False)
            key, _ = smp.answer_key(q, smp.build_image(q, stub))
            # (The expectation holds IOPL clear already: the runner's mask changes nothing in it.)
            self.assertIsNone(crosscheck.agrees('x4', key + '/no-iopl', answers.system_projection(X.trampoline_expected(q))), n)


if __name__ == '__main__': unittest.main()
