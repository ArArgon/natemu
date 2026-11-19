"""The verdicts of x1, x3, l4a, l4b and s4 without QEMU (plan 2026-11-18-campaign-onboarding, Part C): each runner's own
model or SDM expectation decides, so a natemu result off by one value fails."""
import pathlib, struct, sys, unittest

HERE = pathlib.Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE / 'reference'), str(HERE / 'bench' / 'smp')]
import apic, debug, irqs, timers, programs as P  # noqa: E402
import run_x1, run_x3, run_l4a, run_l4b, entry  # noqa: E402


def memory(data_words, log=(), counts=None):
    """run_x3.Memory of CPU 0: DATA words, log entries (8 words each) and COUNTS {vector: n}."""
    data = b''.join(struct.pack('<Q', w) for w in data_words).ljust(0x1000, b'\0')
    lg = b''.join(b''.join(struct.pack('<Q', w) for w in e) for e in log).ljust(apic.LOG_BYTES, b'\0')
    cn = b''.join(struct.pack('<Q', (counts or {}).get(v, 0)) for v in range(256))
    return run_x3.Memory({apic.DATA(0): data, apic.LOG(0): lg, apic.COUNTS(0): cn})


class X3(unittest.TestCase):
    def test_register_read_backs(self):
        for name, want in run_x3.REGS_WANT.items():
            prog = {'name': name, 'ncpu': 1}
            self.assertEqual(run_x3.model_problems(prog, memory(want)), [])
            for k in (0, len(want) - 1):
                bad = list(want); bad[k] ^= 1
                self.assertTrue(run_x3.model_problems(prog, memory(bad)), (name, k))

    def test_x2apic_fault_cases(self):
        prog = {'name': 'apic-faults', 'ncpu': 1}
        gp = [k + 1 for k, (_, g) in enumerate(apic.FAULT_CASES) if g]
        entry = lambda case: (13, 0x1000 + case, 0, 0, 0, 0, case, 0)
        self.assertEqual(run_x3.model_problems(prog, memory([], [entry(c) for c in gp])), [])
        self.assertTrue(run_x3.model_problems(prog, memory([], [entry(c) for c in gp[1:]])))          # a #GP missing
        self.assertTrue(run_x3.model_problems(prog, memory([], [entry(c) for c in gp + [28]])))       # one too many


class L4a(unittest.TestCase):
    def test_tmr_program(self):
        prog = {'name': 'l4a-tmr-x2@1', 'ncpu': 1}
        d = [0x8054] + [0] * 0x2f + [0, 1 << 20, 0]          # DATA + 0x180 + 8 k: TMR[95:64] in handler k (1, 2)
        right = memory(d, counts={irqs.LEVEL_V: 2})
        self.assertEqual(run_l4a.model_problems(prog, right), [])
        self.assertTrue(run_l4a.model_problems(prog, memory(d, counts={irqs.LEVEL_V: 3})))
        self.assertTrue(run_l4a.model_problems(prog, memory([0xc054] + d[1:], counts={irqs.LEVEL_V: 2})))

    def test_uart_bytes(self):
        edge, regs, dest_x = {'name': 'l4a-edge-x2@1'}, {'name': 'l4a-ioapic-regs-x@1'}, {'name': 'l4a-dest-x@2'}
        self.assertEqual(run_l4a.uart_model_problems(edge, b'E'), [])
        self.assertTrue(run_l4a.uart_model_problems(edge, b'EE'))
        self.assertTrue(run_l4a.uart_model_problems(regs, b'E'))
        self.assertEqual(run_l4a.uart_model_problems(dest_x, b'anything'), [])      # (xapic programs: not judged, as before)


class L4b(unittest.TestCase):
    def test_model_and_unnamed_slots(self):
        words = [timers.RTC_WANT.get(k, 0) for k in range(timers.RAW)]
        mem = memory(words)
        self.assertEqual(timers.check_rtc(mem, True)[0] + run_l4b.unnamed_slots('l4b-rtc', mem), [])
        bad = list(words); bad[9] ^= 1                       # a named slot
        self.assertTrue(timers.check_rtc(memory(bad), True)[0])
        bad = list(words); bad[20] = 1                       # a slot no program writes
        self.assertTrue(run_l4b.unnamed_slots('l4b-rtc', memory(bad)))

    def test_pit_named_slots(self):
        words = [timers.PIT_WANT.get(k, 0) for k in range(timers.RAW)]
        self.assertEqual(run_l4b.unnamed_slots('l4b-pit', memory(words)), [])
        bad = list(words); bad[3] = 0x36
        self.assertTrue(timers.check_pit(memory(bad), False)[0])


class X1(unittest.TestCase):
    def test_plain_program_registers(self):
        prog = entry.sreg_program()
        want = entry.sreg_expected()
        end = run_x1.body_end(prog, int(prog['cpus'][0]['rip'], 0) if isinstance(prog['cpus'][0]['rip'], str) else prog['cpus'][0]['rip'])
        def run(gpr):
            saved = run_x1.driver, run_x1.vm_state
            run_x1.driver = lambda *a, **k: (0, {'cpus': [{'stop': 'reached', 'reason': '', 'rip': hex(end)}]}, '')
            run_x1.vm_state = lambda prog, out: {'cpus': [{'gpr': gpr}]}
            try:
                a = type('A', (), {'host': None, 'runtime': None, 'fexc': None, 'out': pathlib.Path('/nonexistent')})()
                return run_x1.plain_program(a, [], prog, want)
            finally:
                run_x1.driver, run_x1.vm_state = saved
        gpr = [want.get(r, 0) for r in P.REGS]
        self.assertEqual(run(gpr), [])
        bad = list(gpr); bad[P.REGS.index('r11')] ^= 1 << 16
        self.assertEqual(len(run(bad)), 2)                       # both block sizes


class S4(unittest.TestCase):
    def test_dr_registers(self):
        prog = {'name': 's4-dr'}
        want = debug.expect_regs(prog)
        gpr = [want.get(r, 0x1234) for r in P.REGS]
        self.assertEqual(debug.judge_regs(prog, gpr), [])
        bad = list(gpr); bad[P.REGS.index('r8')] |= 1 << 12   # pinned QEMU's DR6 bit 12
        self.assertTrue(debug.judge_regs(prog, bad))


if __name__ == '__main__':
    unittest.main()
