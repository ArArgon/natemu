import copy
import unittest
from tools import run_frame_randomization as r


class RandomizationOracle(unittest.TestCase):
    def selected(self):
        before = {k: [] for k in r.strict.STATE}
        before.update(gpr=[0] * 16, rflags='0x202', frame_intact=True, guest_state_alias=hex(r.ALIASES[0][1]))
        got = copy.deepcopy(before)
        got.update(stop='reached', rip='0x100010')
        return before, got

    def test_selected_load_is_explicit_deviation(self):
        before, got = self.selected()
        self.assertEqual(r.selected_judge(before, got, 0x100010, r.ALIASES[0][1], False), [])
        got['stop'] = 'guest_fault'
        self.assertTrue(r.selected_judge(before, got, 0x100010, r.ALIASES[0][1], False))

    def test_padding_store_requires_visible_corruption(self):
        before, got = self.selected()
        got['gpr'][0] = r.MARKER
        got['frame_intact'] = False
        self.assertEqual(r.selected_judge(before, got, 0x100010, r.ALIASES[0][1], True), [])
        got['frame_intact'] = True
        self.assertTrue(r.selected_judge(before, got, 0x100010, r.ALIASES[0][1], True))

    def test_selected_does_not_allow_other_state_changes(self):
        before, got = self.selected()
        got['rflags'] = '0x2'
        self.assertTrue(r.selected_judge(before, got, 0x100010, r.ALIASES[0][1], False))

    def test_alias_report_must_match_all_cpus_and_override(self):
        good = {'guest_state_alias': hex(r.ALIASES[0][1])}
        self.assertEqual(r.alias_errors({'cpus': [good] * 4}, r.ALIASES[0][1]), [])
        self.assertTrue(r.alias_errors({'cpus': [good, {'guest_state_alias': hex(r.ALIASES[1][1])}]}))
        self.assertTrue(r.alias_errors(good, r.ALIASES[1][1]))
        for va in (r.START - 4096, r.START + 1, r.END):
            self.assertTrue(r.alias_errors({'guest_state_alias': hex(va)}))


if __name__ == '__main__':
    unittest.main()
