"""Tests for initsipi_model.py: natemu's INIT/SIPI protocol refines the architectural reference in every scenario, each
deliberate break of it is reported, and the reference's outcomes are the hand-derived ones (rules A1-A8, W1-W4 in the
model's docstring)."""
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from models import initsipi_model as m  # noqa: E402

V = m.V


def ends(name):
    """The reference's outcomes of a scenario."""
    return m.outcomes(name)


def per_cpu(name, cpu):
    """Each outcome's (how it ended, the CPU's observation) for the reference."""
    return {(o[0], o[1][cpu]) if o[0] != 'stop' else (('stop', o[1], o[2]), o[3][cpu]) for o in ends(name)}


class Protocol(unittest.TestCase):
    def test_refines_the_reference_everywhere(self):
        for name in m.SCENARIOS:
            self.assertEqual(m.check(name), [], name)

    def test_each_break_is_reported(self):
        # (variant, a scenario whose check reports it): every check is shown to fail when it should.
        for variant, name in (('sipi-needs-wfs', 'sipi-before-init-taken'), ('init-two-steps', 'init-in-mailbox'),
                              ('init-keeps-sipi', 'init-sipi-init'), ('hlt-ignores-init', 'hlt-init-not-sipi'),
                              ('wfs-not-idle', 'never-started'), ('wfs-reported-halted', 'park-with-init'),
                              ('nmi-wakes-wfs', 'nmi-held'), ('fixed-held-in-wfs', 'fixed-in-wfs')):
            self.assertNotEqual(m.check(name, (variant,)), [], (variant, name))
        self.assertEqual(sorted(m.VARIANTS), sorted(['sipi-needs-wfs', 'init-two-steps', 'init-keeps-sipi',
                                                     'hlt-ignores-init', 'wfs-not-idle', 'wfs-reported-halted',
                                                     'nmi-wakes-wfs', 'fixed-held-in-wfs']))

    def test_state_invariants_name_the_rule(self):
        self.assertTrue(any(v.startswith('J1') for v in m.check('init-in-mailbox', ('init-two-steps',))))
        self.assertTrue(any(v.startswith('J4') for v in m.check('hlt-init-not-sipi', ('hlt-ignores-init',))))


class Senders(unittest.TestCase):
    def test_linux(self):
        # INIT, INIT de-assert (no effect, A7), SIPI, SIPI: one start; the second SIPI finds it running (A8).
        self.assertEqual(ends('linux'), {('end', (('halted', (('boot', 5),), 0, 0),
                                                  ('halted', ((('sipi', V), 5),), 0, 0)))})

    def test_kvm_unit_tests_back_to_back(self):
        # W2: the SIPI sent before the INIT is taken is honoured: each AP starts once at vector 0.
        (o,) = ends('kvm-unit-tests')
        self.assertEqual([c[1] for c in o[1][1:]], [((('sipi', 0), 4),)] * 2)

    def test_edk2_rounds(self):
        self.assertEqual({c[1] for _, c in per_cpu('edk2', 1)}, {((('sipi', 0x87), 4),) * 2})
        # Back to back, an INIT may stop the first round anywhere; the last round always runs.
        self.assertEqual({c[1][-1] for _, c in per_cpu('edk2-back-to-back', 1)}, {(('sipi', 0x87), 4)})


class Rules(unittest.TestCase):
    def test_sipi_before_the_init_is_taken(self):
        for name in ('sipi-before-init-taken', 'sipi-during-host-request', 'init-in-mailbox', 'init-in-translated-hlt',
                     'init-in-interpreted-code'):
            self.assertEqual({c[1][-1] for _, c in per_cpu(name, 1)}, {(('sipi', V), 4)}, name)

    def test_sipi_to_a_cpu_not_waiting_is_discarded(self):
        self.assertEqual({c[0] for _, c in per_cpu('sipi-then-init', 1)}, {'wait-for-sipi'})   # W4
        self.assertTrue(all(('sipi', V) not in [r[0] for r in c[1]] for _, c in per_cpu('sipi-then-init', 1)))
        self.assertEqual(ends('hlt-init-not-sipi'), {('end', (('halted', (('boot', 3),), 0, 0),
                                                              ('halted', ((('sipi', V), 4),), 0, 0)))})
        self.assertTrue(all(c[1][-1] == ('reset', 1) for _, c in per_cpu('bsp-init', 0)))   # A5; its SIPI dropped

    def test_nmi(self):
        # A1: held in wait-for-SIPI, the first event after the SIPI: a typed stop before the trampoline's first
        # instruction; held for good without a SIPI.
        self.assertEqual(ends('nmi-in-wfs'), {('stop', 1, 'nmi', (((('boot', 2),), 0, 0), ((), 0, 0)))})
        self.assertEqual({c[0] for _, c in per_cpu('nmi-held', 1)}, {'wait-for-sipi'})
        # A6: one pending when the INIT is taken may have been delivered or is dropped; the one after it is held.
        self.assertEqual({(how, c[1]) for how, c in per_cpu('nmi-around-init', 1)},   # (runs, NMIs, interrupts)
                         {(('stop', 1, 'nmi'), 0), (('stop', 1, 'nmi'), 1)})
        self.assertEqual(ends('nmi-in-interpreted-hlt'), {('stop', 1, 'nmi', (((('boot', 1),), 0, 0), ((), 0, 0)))})

    def test_fixed_interrupts(self):
        # A2: a software-disabled APIC (after INIT) accepts none, so none is delivered after the start.
        self.assertEqual({c[3] for _, c in per_cpu('fixed-in-wfs', 1)}, {0})
        self.assertEqual({c[3] for _, c in per_cpu('fixed-around-init', 1)}, {0, 1})

    def test_a_second_init(self):
        # W1: the CPU ends waiting, whatever its trampoline had run.
        self.assertEqual({c[0] for _, c in per_cpu('init-sipi-init', 1)}, {'wait-for-sipi'})

    def test_end_rule(self):
        # The run ends with the BSP halted and the others waiting for a SIPI, reported as such (not finished).
        self.assertEqual(ends('never-started'), {('end', (('halted', (('boot', 1),), 0, 0),
                                                          ('wait-for-sipi', (), 0, 0)))})
        self.assertEqual(ends('park-with-init'), {('end', (('halted', (('boot', 1),), 0, 0),
                                                           ('wait-for-sipi', (), 0, 0), ('wait-for-sipi', (), 0, 0)))})


class InitState(unittest.TestCase):
    def test_init_column(self):
        s = m.init_state()
        self.assertEqual((s['cr0'], s['rflags'], s['rip'], s['seg']['cs'][0], s['shape']['cs'][0], s['efer']),
                         (0x60000010, 2, 0xFFF0, 0xF000, 0xFFFF0000, 0))
        self.assertEqual((s['gpr']['rdx'], s['gpr']['rax'], s['dr6'], s['dr7'], s['apic']['svr']),
                         (0x306C3, 0, 0xFFFF0FF0, 0x400, 0xFF))
        self.assertEqual(m.init_state(cd_nw=0)['cr0'], 0x10)
        t = m.init_state(sipi=V)
        self.assertEqual((t['seg']['cs'][0], t['shape']['cs'], t['rip']), (V << 8, (V << 12, 0xFFFF), 0))


if __name__ == '__main__':
    unittest.main()
