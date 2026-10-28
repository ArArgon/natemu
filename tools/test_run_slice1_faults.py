"""The fault runner's paging model (`Machine`) on hand-derived SDM vol. 3 ch. 4 cases."""
import pathlib, sys, unittest

HERE = pathlib.Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE / 'reference')]
import ref, run_slice1_faults as F  # noqa: E402

CODE, DATA = 0x100000, 0x200000


def machine(**mut): return F.Machine({CODE}, {DATA}, mut)


class MachineTests(unittest.TestCase):
    def test_error_codes(self):
        self.assertEqual(machine(unmap=[DATA]).walk(DATA, 'w'), (None, 6))                 # U|W, P=0
        self.assertEqual(machine(readonly=[DATA]).walk(DATA, 'w'), (None, 7))              # protection
        self.assertEqual(machine(cpl=0, readonly=[DATA]).walk(DATA, 'w'), (None, 3))       # CPL0, WP=1
        self.assertEqual(machine(entry=[(DATA, 1, 0, F.US)]).walk(DATA, 'r'), (None, 5))   # supervisor page at CPL3
        self.assertEqual(machine(entry=[(DATA, 1, 1 << 51, 0)]).walk(DATA, 'r'), (None, 13))      # RSVD|U|P
        self.assertEqual(machine(entry=[(DATA, 3, F.M64 & ~1, 1)]).walk(DATA, 'r'), (None, 4))   # not present: rest ignored
        self.assertEqual(machine().fetch(DATA, 1), (14, DATA, 0x15))                       # NX data page: I|U|P
        self.assertEqual(machine(nxe=0).fetch(DATA, 1), None)                               # no NX without NXE
        self.assertEqual(machine(nxe=0, entry=[(CODE, 2, F.XD, 0)]).fetch(CODE, 1), (14, CODE, 13))   # XD reserved, no I
        self.assertEqual(machine(cpl=0, smep=1).fetch(CODE, 1), (14, CODE, 0x11))           # SMEP: user code at CPL0
        self.assertEqual(machine(entry=[(CODE, 4, F.PS, 0)]).fetch(CODE, 1), (14, CODE, 0x1d))     # PS in a PML4E

    def test_permissions_and_ad(self):
        m = machine(cpl=0, wp=0, readonly=[DATA], entry=[(DATA, 1, 0, F.US)])
        self.assertEqual(m.walk(DATA + 8, 'w'), ((1, DATA >> 12), 0))                       # WP=0: supervisor write ok
        self.assertEqual((m.a, m.d), ({(1, DATA >> 12)}, {(1, DATA >> 12)}))
        m = machine(entry=[(DATA, 2, 0, F.RW)])                                              # parent read-only
        self.assertEqual((m.walk(DATA, 'r')[1], m.walk(DATA, 'w')[1]), (0, 7))
        self.assertEqual((m.a, m.d), ({(1, DATA >> 12)}, set()))

    def test_large_leaf(self):
        m = machine(large=[DATA])
        self.assertEqual(m.walk(0x3ff008, 'w'), ((2, 1), 0))                                # the whole 2 MiB is present
        self.assertEqual((m.leaf(DATA), m.d), ((2, 1), {(2, 1)}))
        self.assertEqual(machine(large=[DATA], entry=[(DATA, 2, 1 << 13, 0)]).walk(DATA, 'r'), (None, 13))
        self.assertRaises(ValueError, machine, large=[DATA], entry=[(DATA, 1, 0, F.P)])

    def test_xchg_writes_its_memory_operand_in_either_position(self):
        for line in ('xchg qword ptr [rbx], rax', 'xchg rax, qword ptr [rbx]'):
            self.assertEqual(F.events(line, F.REGS, 0), [('access', F.REGS['rbx'], 8, True, False)])

    def test_stack_accesses_across_the_canonical_gap(self):
        for a, name, line in F.SS_SPAN:
            evs = F.events(line, dict(F.REGS, rsp=a), 0)
            self.assertEqual(F.first_fault(evs, machine(), lambda *_: bytes(8)), (12, 0, 0), (hex(a), line))


class AccessFormTests(unittest.TestCase):
    def test_every_program_faults_at_its_access_in_every_placement(self):
        # From the initial state alone (the prelude only changes rcx, rdx and flags): unmapping the leaf of each page the
        # access touches (CPL 3) must stop at the access instruction, not at the fetch or the prelude's store.
        for p in F.paging_programs():
            offs = F.offsets(p['asm'])[1]
            for where, deltas in F.PLACEMENTS:
                q, _ = F.relocate(p, [], [], *deltas)
                regs, rip = dict(zip(ref.REGS, (ref.num(v) for v in q['gpr']))), ref.num(q['rip'])
                evs = {k: F.events(l, regs, rip + offs[k + 1]) for k, l in enumerate(q['asm']) if '[' in l or l.split()[0] in ('push', 'pop', 'ret')}
                regions = [(ref.num(m['addr']), bytes.fromhex(m['data'])) for m in q['memory']]
                def read(k):
                    return lambda a, n: next(d[a - b:a - b + n] for b, d in regions if b <= a < b + len(d))
                code_pages = set(F.pages(rip, offs[-1]))
                touched = {pg for e in evs[p['access']] if e[0] == 'access' for pg in (e[1] & ~4095, (e[1] + e[2] - 1) & ~4095)}
                for pg in touched:
                    fault, i, fetched, _ = F.run_case(q, offs, evs, read, F.cell(F.LEAF_VARIANTS[0], pg, code_pages)[1])
                    self.assertEqual((bool(fault), i, fetched), (True, p['access'], False), (p['name'], where, hex(pg)))

    def test_ret_returns_to_the_next_instruction(self):
        p = next(p for p in F.paging_programs() if p['name'] == 'paging-ret')
        _, offs = F.offsets(p['asm'])
        stack = bytes.fromhex(p['memory'][1]['data'])
        self.assertEqual(int.from_bytes(stack[0x1000:0x1008], 'little'), ref.num(p['rip']) + offs[len(F.PRELUDE) + 1])
        q, _ = F.relocate(p, [], [], F.HIGH, 0, 2 * F.SLOT)
        self.assertEqual(int.from_bytes(bytes.fromhex(q['memory'][1]['data'])[0x1000:0x1008], 'little'), ref.num(q['rip']) + offs[len(F.PRELUDE) + 1])


if __name__ == '__main__': unittest.main()
