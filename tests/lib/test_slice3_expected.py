"""tests/lib/slice3_expected.py against QEMU 11.1.2: every program run_slice3_vm's `kicks`, `coherence` and `contain` modes
judge by a whole expected state hashes like the answer QEMU gave when it still decided them
(tools/reference/crosscheck/slice3.json)."""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'tests' / 'lib')); import testenv  # noqa: E401,E402 (tests/README.md)
import json, os, pathlib, sys, tempfile, unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'tools'), str(ROOT / 'tools' / 'reference'), str(ROOT / 'tools' / 'bench' / 'smp')]
os.environ.setdefault('NATEMU_QEMU_PIN', 'qemu-system 11.1.2 src')     # part of an answer's key
import answers, crosscheck, smp  # noqa: E402
import run_slice3_vm as S, slice3_expected as E  # noqa: E402


def cases():
    """The programs with the runner's default sizes (tests/smp/family.toml passes none of them)."""
    out = []
    for n in (1, 2, 4):
        out += [S.hot_program(n, 400_000_000), S.rep_program(n, 3000), S.adversarial_program(n, 2_000_000)]
    out += [S.peer_program(400_000_000), S.unacked_program(4, 1)]
    return out + json.loads((ROOT / 'tools/bench/smp/coherence.json').read_text())


class Slice3Expected(unittest.TestCase):
    def test_every_program_equals_qemus_answer(self):
        self.assertEqual(crosscheck.load('slice3')['pin'], os.environ['NATEMU_QEMU_PIN'])
        with tempfile.TemporaryDirectory() as d:
            stub = smp.build_stub(pathlib.Path(d))
        bad, progs = [], cases()
        for prog in progs:
            key, _ = smp.answer_key(prog, smp.build_image(prog, stub))
            why = crosscheck.agrees('slice3', key, answers.system_projection(E.expected(prog)))
            if why: bad.append(f"{prog['name']}: {why}")
        self.assertEqual(len(progs), 31)
        self.assertEqual(bad, [])

    def test_the_coherence_programs_are_the_generators(self):
        import programs
        self.assertEqual(json.loads((ROOT / 'tools/bench/smp/coherence.json').read_text()), programs.coherence_all())


if __name__ == '__main__': unittest.main()
