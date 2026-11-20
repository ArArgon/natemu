"""run_m3's `corpus` verdict without QEMU: mmio_model (and, for the REX-before-66 encoding the model refuses, the SDM's
registers) decides, so a natemu result off by a register, a flag, RIP or a device byte fails (plan
2026-11-18-campaign-onboarding, Part C)."""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'tests' / 'lib')); import testenv  # noqa: E401,E402 (tests/README.md)
import pathlib, sys, tempfile, types, unittest

HERE = pathlib.Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(testenv.ROOT / 'tools' / 'models')]
import run_m3 as R, mmio_model  # noqa: E402


def right(p):
    """The output a correct natemu gives for program `p` (the model's, or the SDM's for the refused encoding)."""
    code, gpr = bytes.fromhex(p['code']), [int(x, 0) for x in p['gpr']]
    data = bytes.fromhex(p['memory'][0]['data'])
    try:
        m = mmio_model.run(code, R.CODE, gpr, int(p['fsbase'], 0), int(p['gsbase'], 0), R.DEV, data + bytes(0x800))
        gpr, rip, data, log = m['gpr'], m['rip'], m['device_bytes'][:0x800], m['accesses']
    except ValueError:
        gpr, rip, log = [p['sdm_rax']] + gpr[1:], R.CODE + len(code), [(0x10, 2, 0, p['sdm_rax'] & 0xffff)]
    return {'stop': 'reached', 'reason': '', 'gpr': [hex(x) for x in gpr], 'rflags': p['rflags'], 'rip': hex(rip),
            'memory': [{'addr': hex(R.DEV), 'data': data.hex()}], 'devices': [{'log': [[0, o, s, d, hex(v)] for o, s, d, v in log]}],
            'mmio': {'completed': len(log)}}


class CorpusVerdict(unittest.TestCase):
    def failures(self, change=lambda name, out: out):
        progs = {p['name']: p for p in R.corpus_programs(3, 7)}
        def driver(host, runtime, fexc, text, work, extra=()):
            name = work.name.rsplit('-', 2)[0]
            return 0, change(name, right(progs[name])), ''
        saved = R.driver
        R.driver = driver
        try:
            with tempfile.TemporaryDirectory() as d:
                return R.corpus(types.SimpleNamespace(programs=3, seed=7, out=pathlib.Path(d), fexc=None, parallel=2), None, None, {})
        finally:
            R.driver = saved

    def test_right_results_pass(self):
        self.assertEqual(self.failures(), [])

    def mutated(self, target, fn):
        def change(name, out):
            return fn(out) if name == target else out
        return self.failures(change)

    def test_a_wrong_register_fails(self):
        f = self.mutated('m3-1', lambda o: dict(o, gpr=[hex(int(o['gpr'][0], 0) ^ 1)] + o['gpr'][1:]))
        self.assertTrue(f and all(x.startswith('m3-1 ') for x in f), f)

    def test_a_changed_flag_fails(self):
        self.assertTrue(self.mutated('m3-0', lambda o: dict(o, rflags=hex(int(o['rflags'], 0) ^ 0x40))))

    def test_a_wrong_rip_fails(self):
        self.assertTrue(self.mutated('m3-2', lambda o: dict(o, rip=hex(int(o['rip'], 0) + 1))))

    def test_a_wrong_device_byte_fails(self):
        self.assertTrue(self.mutated('m3-2', lambda o: dict(o, memory=[{'addr': o['memory'][0]['addr'], 'data': 'ff' + o['memory'][0]['data'][2:]}])))

    def test_rex_before_66_by_the_sdm(self):
        # The model refuses `48 66 8b 03`; the SDM's MOV AX, [RBX] decides: QEMU's 8-byte load (REX.W kept) fails.
        def qemu_like(o):
            rax = int.from_bytes(bytes(range(0x10, 0x18)), 'little')
            return dict(o, gpr=[hex(rax)] + o['gpr'][1:], devices=[{'log': [[0, 0x10, 8, 0, hex(rax)]]}])
        f = self.mutated('m3-rex-before-66', qemu_like)
        self.assertTrue(any('rax' in x for x in f) and any('device log' in x for x in f), f)


if __name__ == '__main__':
    unittest.main()
