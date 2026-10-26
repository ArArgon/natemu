import copy
import unittest
import gzip
import json
from pathlib import Path
import check_i4v_partial_progress as p
from check_i4v_schedule import configuration


class PartialSemantics(unittest.TestCase):
    def test_retained_native_report_and_partial_effect_corruptions(self):
        path=Path(__file__).with_name('testdata')/'i4v_partial_n2.json.gz'
        schedule,report=[json.loads(line) for line in gzip.decompress(path.read_bytes()).splitlines()]
        cfg=configuration(4,2,0)
        p.check(report,cfg)
        changes={
            'pending event':lambda r:r['common'].__setitem__('pending_events',1),
            'extra device read':lambda r:r['owners'][0].__setitem__('reads',3),
            'changed retained sample':lambda r:r['owners'][0].__setitem__('sampled',0),
            'changed retained route':lambda r:r['owners'][0].__setitem__('destination_route',0),
            'changed canonical prefix':lambda r:r['owners'][0]['held']['gpr'].__setitem__(1,0),
        }
        for name,change in changes.items():
            with self.subTest(name=name):
                altered=copy.deepcopy(report);change(altered)
                with self.assertRaises(ValueError):p.check(altered,cfg)
        for duplicate in (False,True):
            altered=copy.deepcopy(report);trace=altered['coordinator']['trace']
            at=next(i for i,word in enumerate(trace) if word>>8==152)
            if duplicate:trace.insert(at,trace[at])
            else:trace.pop(at)
            altered['coordinator']['trace_length']=len(trace)
            with self.assertRaises(ValueError):p.check(altered,cfg)

    def test_partial_prefix_keeps_rip_until_second_store(self):
        for cpu in range(4):
            before=p.guest(cpu);original=copy.deepcopy(before)
            held=p.advanced(before,1);done=p.advanced(before,2)
            self.assertEqual(before,original)
            self.assertEqual((held['rip'],held['gpr'][1]),(before['rip'],1))
            self.assertEqual((done['rip'],done['gpr'][1]),(before['rip']+2,0))
            for reg in (6,7):
                self.assertEqual(held['gpr'][reg],before['gpr'][reg]+1)
                self.assertEqual(done['gpr'][reg],before['gpr'][reg]+2)
            for field in ('rflags','ymm','x87','x87_regs','mxcsr','control','segments'):
                self.assertEqual(done[field],before[field])

    def test_unearned_effects_and_delivery_are_rejected(self):
        for event in (1,2,3,4,5,140,141,142,143,144,145,146,147,148,149,150,151,152,153,154,155):
            with self.subTest(event=event):
                model=p.Replay({'owners':[]},configuration(4,2,0))
                with self.assertRaises(ValueError):model.step(event<<8)

    def test_foreign_owner_is_not_a_scheduling_choice(self):
        model=p.Replay({'owners':[]},configuration(4,1,0))
        with self.assertRaises(ValueError):model.step((141<<8)|1)

    def test_initial_tables_map_source_separately_from_destination(self):
        model=p.Replay({'owners':[]},configuration(4,4,1))
        image=p.table_image(model.tables)
        self.assertEqual(len(image),16384)
        self.assertEqual(int.from_bytes(bytes(image[0x3800:0x3808]),'little'),p.DATA|0x67)
        self.assertEqual(int.from_bytes(bytes(image[0x3810:0x3818]),'little'),p.RIP|0x27)


if __name__=='__main__':unittest.main()
