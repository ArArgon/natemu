"""Negative controls for the frame-alias regression oracle; no VM needed."""
import copy
import unittest

from tools import run_frame_alias as runner


class OracleTests(unittest.TestCase):
    def setUp(self):
        self.before = dict(stop='reached', rip='0x100010', fault=['0x0'] * 4,
                           gpr=['0x0'] * 16, rflags='0xad7', xmm=['0x0'] * 32,
                           ymmh=['0x0'] * 32, mxcsr='0x1f80', x87={'top': 7},
                           memory=[dict(addr=hex(runner.DATA), data=bytes(32).hex())], frame_intact=True)

    def fault(self, vector=13):
        out = copy.deepcopy(self.before)
        out.update(stop='guest_fault', fault=[hex(vector), '0x0', '0x0', '0x100010'])
        return out

    def test_rejects_old_alias_success_and_wrong_fault(self):
        case = dict(fault=13)
        self.assertEqual(runner.judge(case, self.before, self.fault(), 0x100010, 0x100013), [])
        self.assertTrue(runner.judge(case, self.before, self.before, 0x100010, 0x100013))
        self.assertTrue(runner.judge(case, self.before, self.fault(12), 0x100010, 0x100013))

    def test_fault_rejects_flags_memory_and_frame_changes(self):
        for key, value in [('rflags', '0xad6'), ('frame_intact', False),
                           ('memory', [dict(addr=hex(runner.DATA), data='01' + bytes(31).hex())])]:
            out = self.fault()
            out[key] = value
            self.assertTrue(runner.judge(dict(fault=13), self.before, out, 0x100010, 0x100013), key)

    def test_success_allows_only_named_effects(self):
        case = dict(fault=None, wantregs={0: 7}, wantmem=[(runner.DATA, 1, 5)])
        out = copy.deepcopy(self.before)
        out['rip'] = '0x100013'
        out['gpr'][0] = '0x7'
        out['memory'][0]['data'] = '05' + bytes(31).hex()
        self.assertEqual(runner.judge(case, self.before, out, 0x100010, 0x100013), [])
        out['rflags'] = '0xad6'
        self.assertTrue(runner.judge(case, self.before, out, 0x100010, 0x100013))
        out['rflags'] = self.before['rflags']
        out['gpr'][1] = '0x1'
        self.assertTrue(runner.judge(case, self.before, out, 0x100010, 0x100013))

    def test_segment_bases_are_architecturally_canonical(self):
        for case in runner.cases():
            for setting in case.get('setup', []):
                if setting.startswith(('fsbase ', 'gsbase ')):
                    value = int(setting.split()[1], 0)
                    self.assertTrue(value < 1 << 47 or value >= (1 << 64) - (1 << 47), setting)


if __name__ == '__main__':
    unittest.main()
