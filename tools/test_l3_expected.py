"""tools/l3_expected.py against QEMU 11.1.2: each L3 `qemu` program's expected state and COM1 output hash like the answers
QEMU gave (tools/reference/crosscheck/l3.json)."""
import os, pathlib, sys, tempfile, unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / 'tools'), str(ROOT / 'tools' / 'reference'), str(ROOT / 'tools' / 'bench' / 'smp')]
os.environ.setdefault('NATEMU_QEMU_PIN', 'qemu-system 11.1.2 src')     # part of an answer's key
import answers, crosscheck, ports, smp  # noqa: E402
import l3_expected as E  # noqa: E402


class L3Expected(unittest.TestCase):
    def test_every_program_equals_qemus_answer(self):
        with tempfile.TemporaryDirectory() as d:
            stub = smp.build_stub(pathlib.Path(d))
        progs = [ports.uart_program(), ports.unassigned_program(), ports.strings_program()] + [ports.protection_program(n) for n in ports.IO_CONFIGS]
        for p in progs:
            q = p['qemu']
            key, _ = smp.answer_key(q, smp.build_image(q, stub), extra=['-serial', 'file:PATH'])
            state, sent = E.expected(p)
            self.assertIsNone(crosscheck.agrees('l3', key, answers.system_projection(state)), q['name'])
            self.assertIsNone(crosscheck.agrees('l3', key + '/uart', answers.canon(sent.hex())), q['name'])


if __name__ == '__main__': unittest.main()
