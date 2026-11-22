"""Tests for initsipi_expected.py (the INIT and SIPI cases' judge) and run_initsipi.py's descriptions."""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'tests' / 'lib')); import testenv  # noqa: E401,E402 (tests/README.md)
import unittest
import initsipi_expected as e
import run_initsipi as r
from models import initsipi_model as m

V = m.V


def only(name, regime='entry'):
    """The scenario's projected reference outcomes."""
    return {e.project(name, o, regime) for o in e.reference(name, regime)}


class EntryRegime(unittest.TestCase):
    def test_natemu_protocol_refines_the_reference_on_the_entry_scenarios(self):
        # The cut scenarios are scenarios too: natemu's protocol reaches only outcomes the reference permits, J1-J4 hold.
        for name in r.SCENARIOS:
            sc = e.entry_scenario(m.SCENARIOS[name])
            impl, ref = m.explore(sc), m.explore(sc, spec=True)
            self.assertEqual(impl['violations'], [], name)
            self.assertLessEqual(impl['outcomes'], ref['outcomes'], name)

    def test_what_the_cases_must_show(self):
        done = lambda v: ('done', (('sipi', v),))
        self.assertEqual(only('sipi-sipi'), {('end', (('halted', ()), done(V)))})            # the second SIPI ignored
        self.assertEqual(only('kvm-unit-tests'), {('end', (('halted', ()), done(0), done(0)))})
        self.assertEqual(only('linux'), {('end', (('halted', ()), done(V)))})
        self.assertEqual(only('edk2'), {('end', (('halted', ()), done(0x87)))})
        self.assertEqual(only('one-cpu'), {('end', (('halted', ()),))})
        self.assertEqual(only('never-started'), {('end', (('halted', ()), ('wait-for-sipi', ())))})
        self.assertEqual(only('nmi-in-wfs'), {('stop', 1, 'nmi')})                           # held, then first
        self.assertEqual(only('nmi-held'), {('end', (('halted', ()), ('wait-for-sipi', ())))})
        self.assertEqual(only('nmi-init-in-wfs'), {('end', (('halted', ()), done(V)))})       # dropped by the INIT
        self.assertEqual(only('sipi-then-init'), {('end', (('halted', ()), ('wait-for-sipi', ())))})
        self.assertEqual(only('bsp-init'), {('end', (('done', ('reset',)), ('halted', ())))})
        self.assertEqual(only('init-in-mailbox'), {('end', (('halted', ()), done(V)))})
        self.assertEqual(only('park-with-init'), {('end', (('halted', ()), ('wait-for-sipi', ()), ('wait-for-sipi', ())))})
        # The second INIT ends the AP waiting (W1), unless the start reached its entry first (done there: it takes no
        # INIT after that in this regime).
        self.assertEqual({p[1][1] for p in only('init-sipi-init')}, {('wait-for-sipi', ()), done(V)})

    def test_the_judge(self):
        ok = ('end', (('halted', ()), ('done', (('sipi', V),))))
        self.assertEqual(e.judge('sipi-sipi', ok, 'entry'), [])
        self.assertEqual(e.judge('sipi-sipi', ('end', (('halted', ()), e.ANY)), 'entry'), [])     # cancelled
        # Started at the second vector, never started, reported halted, or a stop where the run must end: refused.
        for bad in (('end', (('halted', ()), ('done', (('sipi', m.V2),)))), ('end', (('halted', ()), ('wait-for-sipi', ()))),
                    ('end', (('halted', ()), ('halted', ()))), ('stop', 1, 'nmi'), ('end', (('halted', ()),))):
            self.assertNotEqual(e.judge('sipi-sipi', bad, 'entry'), [], bad)
        self.assertEqual(e.judge('nmi-in-wfs', ('stop', 1, 'nmi'), 'entry'), [])
        self.assertNotEqual(e.judge('nmi-in-wfs', ('end', (('halted', ()), ('done', (('sipi', V),)))), 'entry'), [])
        self.assertNotEqual(e.judge('never-started', ('end', (('halted', ()), ('halted', ()))), 'entry'), [])


class InterpreterRegime(unittest.TestCase):
    def test_starts_count_when_they_signalled(self):
        self.assertEqual(only('edk2', 'interpreter'), {('end', (('halted', ()), ('halted', (('sipi', 0x87),) * 2)))})
        self.assertEqual(only('linux', 'interpreter'), {('end', (('halted', ()), ('halted', (('sipi', V),))))})
        # The last round always runs; an earlier one may be stopped before it signals.
        self.assertEqual({p[1][1] for p in only('edk2-back-to-back', 'interpreter')},
                         {('halted', (('sipi', 0x87),)), ('halted', (('sipi', 0x87),) * 2)})


class Descriptions(unittest.TestCase):
    def test_vectors(self):
        self.assertEqual(r.vectors(m.SCENARIOS['sipi-sipi'], 'entry'), {V: 0x08, m.V2: 0x09})
        self.assertEqual(r.vectors(m.SCENARIOS['kvm-unit-tests'], 'entry'), {0: 0})
        self.assertEqual(r.vectors(m.SCENARIOS['kvm-unit-tests'], 'interpreter'), {0: 0x08})

    def test_icr_words(self):
        self.assertIn('mov eax, 0x4500', r.icr('init', 1, None, False))
        self.assertIn('mov edx, 1', r.icr('init', 1, None, False))
        self.assertIn('mov eax, 0xc4608', r.icr('sipi', 'others', 8, False))
        self.assertIn('mov dword [rax], 0xc4600', r.icr('sipi', 'others', 0, True))       # xAPIC, vector 0
        self.assertIn('mov dword [rax + 0x10], 0x1000000', r.icr('init', 1, None, True))
        self.assertIn('mov eax, 0x8500', r.icr('deassert', 1, None, False))

    def test_every_scenario_has_a_program(self):
        for name in r.SCENARIOS:
            text, _ = r.source(name, 'entry')
            self.assertIn('start:', text, name)
            self.assertEqual(text.count('\nap') + text.startswith('ap'),
                             sum(1 for c in m.SCENARIOS[name]['cpus'][1:] if c.get('ctx') != 'wfs'), name)


if __name__ == '__main__':
    unittest.main()
