"""The verdicts of x1, x3, l4a, l4b and s4 without QEMU (plan 2026-11-18-campaign-onboarding, Part C): each runner's own
model or SDM expectation decides, so a natemu result off by one value fails."""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'tests' / 'lib')); import testenv  # noqa: E401,E402 (tests/README.md)
import pathlib, struct, sys, unittest

HERE = pathlib.Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(testenv.ROOT / 'tools' / 'bench' / 'smp')]
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

    # apic-prio-x2 and apic-cr8 CPU 0 logs as natemu wrote them (November 19): (vector, RIP, PPR, ISR[127:64],
    # IRR[127:64], TPR, R12, TSC).
    PRIO_LOG = [(0x71, 0x11009c, 0x70, 1 << 49, 0x200020002, 0x50, 2), (0x61, 0x11009c, 0x60, 1 << 33, 0x20002, 0x50, 2),
                (0x51, 0x1100b3, 0x50, 1 << 17, 2, 0x40, 3), (0x41, 0x1100ca, 0x40, 2, 0, 0, 4), (0x42, 0x1100e1, 0x40, 4, 0, 0, 5),
                (0x62, 0x11027b, 0x60, 0x400000004, 1 << 18, 0, 5), (0x52, 0x11027b, 0x50, 0x40004, 0, 0, 5),
                (0x43, 0x110102, 0x40, 8, 0, 0, 7), (0x63, 0x1102ca, 0x60, 0x800000008, 0, 0, 7)]

    def test_prio_and_cr8_deliveries(self):
        prog, labels = apic.prio_program('x2')
        log = [e + (1000 + k,) for k, e in enumerate(self.PRIO_LOG)]
        data = [0, 0, 0, 8, 8]
        self.assertEqual(run_x3.model_problems(prog, memory(data, log), labels[0]), [])
        for field, delta in ((4, 1 << 1), (5, 0x10), (1, 2)):      # an IRR bit, the TPR, the frame RIP
            for k in (0, 5):
                bad = list(log); bad[k] = bad[k][:field] + (bad[k][field] ^ delta,) + bad[k][field + 1:]
                self.assertTrue(run_x3.model_problems(prog, memory(data, bad), labels[0]), (field, k))
        prog, labels = apic.cr8_program()
        log = [(0x55, 0x110079, 0x50, 1 << 21, 0, 0x40, 2, 1000)]
        self.assertEqual(run_x3.model_problems(prog, memory([0x50, 7, 0x7f, 0], log), labels[0]), [])
        for field, delta in ((4, 1 << 21), (5, 0x10), (1, 1)):
            bad = [log[0][:field] + (log[0][field] ^ delta,) + log[0][field + 1:]]
            self.assertTrue(run_x3.model_problems(prog, memory([0x50, 7, 0x7f, 0], bad), labels[0]), field)


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

    # x1-entry@1's CPU 0 log as natemu wrote it (November 19; every frame delivery_model's): (vector, error, RIP, CS,
    # RFLAGS, RSP, SS, RIP slot, x1, x2); a paranoid entry's x1 is the GS base it found.
    ENTRY_LOG = [
        (0x6, -1, 0x286007, 0x23, 0x10002, 0x28a000, 0x1b, 0x2833c8, 0x100, 0x0), (0x3, -1, 0x28600a, 0x23, 0x2, 0x28a000, 0x1b, 0x2847c8, 0x100, 0x0),
        (0x80, -1, 0x28600c, 0x23, 0x2, 0x28a000, 0x1b, 0x284bc8, 0x100, 0x0), (0xd, 0x0, 0x286013, 0x23, 0x10002, 0x28a000, 0x1b, 0x2833c8, 0x100, 0x0),
        (0xe, 0x5, 0x28601d, 0x23, 0x10002, 0x28a000, 0x1b, 0x2833c8, 0x100, 0x110000), (0x0, -1, 0x286032, 0x23, 0x10046, 0x28a000, 0x1b, 0x2833c8, 0x100, 0x0),
        (0x1, -1, 0x286035, 0x23, 0x10046, 0x28a000, 0x1b, 0x283fc8, 0x282600, 0x1000000), (0x1, -1, 0x286040, 0x23, 0x146, 0x28a000, 0x1b, 0x283fc8, 0x282600, 0x1000000),
        (0xd, 0x92, 0x286047, 0x23, 0x10046, 0x28a000, 0x1b, 0x2833c8, 0x100, 0x0), (0x8, 0x0, 0x286050, 0x23, 0x46, 0x28a000, 0x1b, 0x2837c8, 0x282600, 0x1000000),
        (0x81, -1, 0x286082, 0x23, 0x46, 0x28a000, 0x1b, 0x284fc8, 0x100, 0x0), (0x2, -1, 0x1100be, 0x8, 0x46, 0x3e1800, 0x0, 0x283bc8, 0x282400, 0x1000000),
        (0x2, -1, 0x1100be, 0x8, 0x46, 0x3e1800, 0x0, 0x283bc8, 0x282400, 0x1000000), (0x12, -1, 0x1100c1, 0x8, 0x46, 0x3e1800, 0x0, 0x2843c8, 0x282400, 0x1000000),
        (0x6, -1, 0x1100c9, 0x8, 0x10046, 0x3e17f8, 0x0, 0x3e17c8, 0x100, 0x0), (0x1, -1, 0x1100cd, 0x8, 0x46, 0x3e1800, 0x0, 0x283fc8, 0x282400, 0x1000000),
        (0x8, 0x0, 0x1100d7, 0x8, 0x46, 0x3e1800, 0x0, 0x2837c8, 0x282600, 0x1000000)]

    def test_entry_gs_bases(self):
        prog = {'ncpu': 1}
        d = P.SYS_D(0)
        def problems(log):
            data = b''.join(struct.pack('<10Q', *(w & (1 << 64) - 1 for w in e)) for e in log).ljust(entry.RES - entry.LOG, b'\0')
            data += struct.pack('<3Q', 0, d + entry.U, 0x200)
            return run_x1.model_problems(prog, {d + entry.LOG: data.ljust(0x1000, b'\0')})
        self.assertEqual(problems(self.ENTRY_LOG), [])
        k, u = d + entry.K, d + entry.U
        self.assertEqual({e[8] for e in self.ENTRY_LOG if e[0] in entry.PARANOID}, {k, u})
        for i in (6, 9, 11, 16):                  # CPL 3 #DB (U), CPL 3 #DF (U), CPL 0 NMI (K), the #DF between SWAPGS (U)
            bad = list(self.ENTRY_LOG)
            e = bad[i]
            bad[i] = e[:8] + ({k: u, u: k}[e[8]],) + e[9:]
            self.assertEqual(len(problems(bad)), 1, i)
        swapped = [e[:8] + ({k: u, u: k}[e[8]],) + e[9:] if e[0] in entry.PARANOID else e for e in self.ENTRY_LOG]
        self.assertEqual(len(problems(swapped)), 8)        # every paranoid event


class S4(unittest.TestCase):
    def test_dr_registers(self):
        prog = {'name': 's4-dr'}
        want = debug.expect_regs(prog)
        gpr = [want.get(r, 0x1234) for r in P.REGS]
        self.assertEqual(debug.judge_regs(prog, gpr), [])
        bad = list(gpr); bad[P.REGS.index('r8')] |= 1 << 12   # pinned QEMU's DR6 bit 12
        self.assertTrue(debug.judge_regs(prog, bad))

    def test_quiet_twin(self):
        gpr = list(range(16))
        mem = {debug.LOG: b'\1' * 0x10, debug.PTRS: bytes(0x40), debug.DATA: bytes(range(0x80)), debug.W: bytes(0x1000)}
        quiet = {'gpr': gpr, 'memory': mem}
        same = lambda **kw: {'gpr': kw.get('gpr', gpr), 'memory': {**mem, **kw.get('memory', {})}}
        self.assertEqual(debug.quiet_problems('s4-d-stack', same(), quiet), [])
        # the log, the log cursor and a handler frame on s4-d-stack's dead stack may differ
        cursor = bytearray(0x40); cursor[0x20] = 0x30
        frame = bytearray(0x1000); frame[debug.Z - debug.W - 0x40] = 1
        self.assertEqual(debug.quiet_problems('s4-d-stack', same(memory={debug.LOG: b'\2' * 0x10, debug.PTRS: bytes(cursor), debug.W: bytes(frame)}), quiet), [])
        self.assertTrue(debug.quiet_problems('s4-d-reps', same(memory={debug.W: bytes(frame)}), quiet))   # not its dead stack
        data = bytearray(range(0x80)); data[0x41] ^= 1
        self.assertTrue(debug.quiet_problems('s4-step', same(memory={debug.DATA: bytes(data)}), quiet))
        bad = list(gpr); bad[P.REGS.index('rsi')] += 1
        self.assertTrue(debug.quiet_problems('s4-step', same(gpr=bad), quiet))

    def test_quiet_twin_programs(self):
        for name in debug.QUIET:
            loud, quiet = debug.program(name), debug.program(name, quiet_twin=True)
            self.assertEqual(loud['params'], quiet['params'])
            self.assertEqual(len(loud['bodies'][0]['code']), len(quiet['bodies'][0]['code']))


if __name__ == '__main__':
    unittest.main()
