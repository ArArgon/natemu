"""Tests for mailbox_model.py (ACPI 6.5 s5.2.12.19). Run: python3 -B -m unittest discover -s tools -p 'test_*.py'"""
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from models import mailbox_model as mm  # noqa: E402


def step(apic_id, vector, command=1):
    return {'apic_id': apic_id, 'vector': vector, 'command': command}


def wake(ap, vec=0x1000):
    return [('os_write', 'apic_id', ap), ('os_write', 'vector', vec), ('os_write', 'command', 1)]


class RunTests(unittest.TestCase):
    def test_each_ap_woken_in_turn(self):
        for n in (2, 3, 4):
            r = mm.run(n, [step(i, 0x1000 * i) for i in range(1, n)])
            self.assertEqual(r['acks'], [True] * (n - 1))
            self.assertEqual(r['starts'], {0: [], **{i: [0x1000 * i] for i in range(1, n)}})
            self.assertEqual(r['mailbox'][0], 0)

    def test_wrong_apic_id_unacked_and_cancelled(self):
        r = mm.run(2, [step(5, 0x1000)])
        self.assertEqual(r['acks'], [False])
        self.assertEqual(r['starts'], {0: [], 1: []})
        self.assertEqual(r['mailbox'], (0, 5, 0x1000))

    def test_reserved_command_ignored(self):
        for c in (2, 3, 0xFFFF):
            r = mm.run(2, [step(1, 0x1000, c), step(1, 0x2000)])
            self.assertEqual(r['acks'], [False, True])
            self.assertEqual(r['starts'][1], [0x2000])

    def test_second_wakeup_unacked(self):
        r = mm.run(2, [step(1, 0x1000), step(1, 0x2000)])
        self.assertEqual(r['acks'], [True, False])
        self.assertEqual(r['starts'][1], [0x1000])

    def test_bsp_id_unacked(self):
        r = mm.run(3, [step(0, 0x1000)])
        self.assertEqual(r['acks'], [False])
        self.assertEqual(r['starts'], {0: [], 1: [], 2: []})


class CheckTraceTests(unittest.TestCase):
    def bad(self, events, n=3):
        self.assertTrue(mm.check_trace(n, events), events)

    def test_conforming(self):
        t = wake(1, 0x1000) + [('os_write', 'vector', 0x9999), ('ap_ack', 1), ('ap_start', 1, 0x1000)]
        t += wake(2, 0x2000) + [('ap_ack', 2), ('ap_start', 2, 0x2000)]
        self.assertEqual(mm.check_trace(3, t), [])
        self.assertEqual(mm.check_trace(3, wake(2) + [('os_write', 'command', 0)] + wake(1)
                                        + [('ap_ack', 1), ('ap_start', 1, 0x1000)]), [])

    def test_ack_or_start_without_wakeup(self):
        self.bad([('ap_ack', 1)])
        self.bad([('ap_start', 1, 0x1000)])

    def test_ack_by_wrong_ap(self):
        self.bad(wake(1) + [('ap_ack', 2)])

    def test_bsp_or_unknown_id(self):
        self.bad(wake(0) + [('ap_ack', 0)])
        self.bad(wake(7) + [('ap_ack', 7)])

    def test_wrong_vector(self):
        self.bad(wake(1, 0x1000) + [('ap_ack', 1), ('ap_start', 1, 0x2000)])
        # vector rewritten after the command must not be picked up
        self.bad(wake(1, 0x1000) + [('os_write', 'vector', 0x2000), ('ap_ack', 1), ('ap_start', 1, 0x2000)])

    def test_start_without_ack(self):
        self.bad(wake(1) + [('ap_start', 1, 0x1000)])

    def test_second_ack_or_start(self):
        ok = wake(1) + [('ap_ack', 1), ('ap_start', 1, 0x1000)]
        self.bad(ok + wake(1, 0x2000) + [('ap_ack', 1)])
        self.bad(ok + [('ap_start', 1, 0x1000)])

    def test_ack_of_reserved_or_noop(self):
        self.bad([('os_write', 'apic_id', 1), ('os_write', 'command', 2), ('ap_ack', 1)])
        self.bad([('os_write', 'apic_id', 1), ('os_write', 'command', 0), ('ap_ack', 1)])

    def test_new_command_while_unacked(self):
        self.bad(wake(1) + [('os_write', 'command', 1)])
        self.bad(wake(1) + [('os_write', 'command', 2)])


if __name__ == '__main__':
    unittest.main()
