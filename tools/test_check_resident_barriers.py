#!/usr/bin/env python3
"""check_resident_barriers.py on small hand-written listings. The checker expects two monomorphisations of `walk_once`:
`..., true>` (barriers on) and `..., false>` (off). A barrier-on instance must (a) carry an `sb`/`csdb` on every read ->
sink path, (b) materialise the mask constant DESCEND_RESERVED (a `movk ..., #0x80` for PS and `#0xfff0000000000` for the
reserved bits) and a `csel ..., xzr` select-to-zero. A store through a non-result-pointer base is a sink; one through the
result pointer (x8, or a register the prologue copied it into) or SP is not. The off instance must carry no barrier."""
import pathlib, sys, unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import check_resident_barriers as cb  # noqa: E402

ON = 'natemu_abi::paging::walk_once::<natemu_abi::guest_memory::GuestTables<()>, true>'
OFF = 'natemu_abi::paging::walk_once::<natemu_abi::guest_memory::GuestTables<()>, false>'
READ = 'bl 0x3000 <<natemu_abi::guest_memory::GuestRam>::load64>'
MASK = ['mov x10, #0xfff0000000000', 'movk x10, #0x80', 'csel x28, x28, xzr, ne']
# A valid on-body: prologue saves the result pointer (x8) into x19; the mask + csdb precede the next read; the leaf SB
# precedes the return-struct store through x19.
ON_GOOD = ['mov x19, x8', READ, 'tbz w1, #0x0, L+9'] + MASK + ['csdb', READ, 'sb', 'str x1, [x19, #0x8]', 'ret']
OFF_GOOD = ['mov x19, x8', READ, READ, 'str x1, [x19]', 'ret']


def one(name, body, at):
    out = [f'{at:016x} <{name}>:']
    for k, ins in enumerate(body):
        for j in range(len(body)):
            ins = ins.replace(f'L+{j}', f'{at + 4 * j:#x} <{name}+{4 * j:#x}>')
        out.append(f'    {at + 4 * k:x}:  \t{ins}')
    return out


def listing(on_body=None, off_body=None):
    return '\n'.join(one(ON, on_body if on_body is not None else ON_GOOD, 0x1000) + ['']
                     + one(OFF, off_body if off_body is not None else OFF_GOOD, 0x5000) + ['']
                     + ['0000000000002000 <other>:', '    2000:  \tret']) + '\n'


class Barriers(unittest.TestCase):
    def problems(self, **kw):
        return cb.check(listing(**kw))[1]

    def test_good_passes(self):
        self.assertEqual(self.problems(), [])

    def test_on_missing_csdb_fails(self):
        self.assertTrue(any('without `sb`' in p for p in self.problems(on_body=[i for i in ON_GOOD if i != 'csdb'])))

    def test_on_missing_ps_bit_fails(self):   # review B1: the mask must include PS (movk #0x80)
        self.assertTrue(any('#0x80' in p for p in self.problems(on_body=[i for i in ON_GOOD if i != 'movk x10, #0x80'])))

    def test_on_missing_reserved_const_fails(self):
        self.assertTrue(any('0xfff0000000000' in p for p in self.problems(on_body=[i for i in ON_GOOD if i != 'mov x10, #0xfff0000000000'])))

    def test_on_missing_select_to_zero_fails(self):
        self.assertTrue(any('csel' in p for p in self.problems(on_body=[i for i in ON_GOOD if i != 'csel x28, x28, xzr, ne'])))

    def test_store_through_non_result_base_is_a_sink(self):
        body = ['mov x19, x8', READ, 'str x1, [x9]'] + MASK + ['csdb', READ, 'sb', 'str x1, [x19]', 'ret']
        self.assertTrue(any('str x1, [x9]' in p for p in self.problems(on_body=body)))

    def test_store_through_result_pointer_is_not_a_sink(self):
        self.assertEqual(self.problems(), [])  # ON_GOOD stores through x19 (= x8) after the sb: fine

    def test_off_with_a_barrier_fails(self):
        self.assertTrue(any('barrier-off instance carries a barrier' in p
                            for p in self.problems(off_body=['mov x19, x8', READ, 'sb', READ, 'ret'])))

    def test_no_instance_fails(self):
        self.assertTrue(cb.check('0000000000001000 <other>:\n    1000:  \tret\n')[1])


if __name__ == '__main__':
    unittest.main()
