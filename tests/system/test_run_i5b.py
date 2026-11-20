"""run_i5b's `faults` verdict without QEMU: the SDM rule of each case (`want`) alone decides, so a wrong natemu fault
fails the case and QEMU is never asked (plan 2026-11-18-campaign-onboarding, Part C)."""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'tests' / 'lib')); import testenv  # noqa: E401,E402 (tests/README.md)
import pathlib, sys, tempfile, types, unittest

HERE = pathlib.Path(__file__).resolve().parent
sys.path[:0] = [str(HERE)]
import run_i5b as R  # noqa: E402


def cpu(stop, rip, fault=None):
    return {'stop': stop, 'reason': stop, 'rip': hex(rip), 'fault': [hex(x) for x in fault] if fault else [],
            'gpr': ['0x0'] * 16, 'xmm': [], 'mxcsr': '0x1f80', 'x87': {}}


class FaultVerdict(unittest.TestCase):
    def verdict(self, case_name, full):
        """run_faults on one case with natemu's run of the whole program given by `full(at)` (its prefix run reaches the
        instruction): (problems, how often QEMU ran)."""
        case = next(c for c in R.fault_cases() if c['name'] == case_name)
        pre = list(case['prefix']) + case['lines'][:-1]
        at = R.CODE + len(R.ref.assemble('\n'.join(pre)))
        calls, qemu_runs = [], []
        def natemu(a, host, runtime, text, work, limit=None):
            calls.append(text)
            return 0, {'cpus': [full(at) if len(calls) % 2 else cpu('reached', at)]}, ''
        saved = R.fault_cases, R.natemu, R.qemu, R.s3.vm_state
        R.fault_cases = lambda: [case]
        R.natemu = natemu
        R.qemu = lambda *a, **k: qemu_runs.append(1)
        R.s3.vm_state = lambda prog, out: {'memory': [{'data': b'same'}]}
        try:
            with tempfile.TemporaryDirectory() as d:
                results = []
                bad = R.run_faults(types.SimpleNamespace(only=None, out=pathlib.Path(d), no_qemu=False, fexc=None), None, None, results)
        finally:
            R.fault_cases, R.natemu, R.qemu, R.s3.vm_state = saved
        self.assertEqual(bad, int(bool(results[0]['problems'])))
        return results[0]['problems'], len(qemu_runs)

    def test_the_rule_passes_the_right_fault(self):
        # XGETBV with ECX = 1: #GP(0) at the instruction (SDM Vol.2 XGETBV).
        self.assertEqual(self.verdict('xgetbv-ecx1', lambda at: cpu('guest_fault', at, (13, 0, 0, at))), ([], 0))

    def test_the_rule_fails_a_wrong_fault(self):
        problems, runs = self.verdict('xgetbv-ecx1', lambda at: cpu('guest_fault', at, (6, 0, 0, at)))
        self.assertTrue(problems and all('want (13, 0' in p for p in problems), problems)
        self.assertEqual(runs, 0)

    def test_the_rule_fails_a_missing_fault(self):
        problems, _ = self.verdict('xgetbv-ecx1', lambda at: cpu('reached', at + 3))
        self.assertTrue(problems)

    def test_ss_by_the_rule_where_qemu_gave_gp(self):
        # QEMU 11.1.2 raises #GP(0) here; the rule (the AMD machine's, the default segment SS) is #SS(0) and decides alone.
        ok, _ = self.verdict('segment-ds-rbp-load-noncanonical', lambda at: cpu('guest_fault', at, (12, 0, 0, at)))
        gp, _ = self.verdict('segment-ds-rbp-load-noncanonical', lambda at: cpu('guest_fault', at, (13, 0, 0, at)))
        self.assertEqual(ok, [])
        self.assertTrue(gp)

    def test_nm_under_ts_by_the_rule(self):
        # QEMU 11.1.2 never raises #NM for XSAVE under CR0.TS; the SDM does.
        ok, _ = self.verdict('ts-xsave', lambda at: cpu('guest_fault', at, (7, 0, 0, at)))
        ran, _ = self.verdict('ts-xsave', lambda at: cpu('reached', at + 4))
        self.assertEqual(ok, [])
        self.assertTrue(ran)


if __name__ == '__main__':
    unittest.main()
