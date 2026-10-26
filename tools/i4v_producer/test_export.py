import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import unittest

spec=importlib.util.spec_from_file_location('i4v_export',Path(__file__).with_name('export.py'))
export=importlib.util.module_from_spec(spec);spec.loader.exec_module(export)
DATA=Path(__file__).with_name('testdata')

class ExportChecks(unittest.TestCase):
    def test_real_retained_outputs(self):
        for name,case,site_count in [('load-high','fex_memory_data64',1),('push-high','fex_push_immediates64',5)]:
            result=export.verify(DATA/name,dict(case=case,guest_rip=0xffff800000102000))
            self.assertEqual(len(result['sites']),site_count)
            self.assertEqual(result['entry_gprs']['rsp'],8)

    def test_protected32_steps_keep_actual_guest_rip_and_memory_sites(self):
        offsets=(0,5,10,12,17,22,24,29,35)
        for step,offset in enumerate(offsets):
            case=f'fex_moffs_load32_step{3 if step==6 else step}'
            result=export.verify(DATA/f'step-{step}-low',dict(case=case,guest_rip=0x102000+offset,bitness=32))
            self.assertEqual(bytes.fromhex(result['source_hex']),bytes.fromhex(export.CASES['fex_moffs_load32']['source'])[offset:(*offsets,37)[step+1]])
            self.assertEqual(len(result['sites']),int(step in (2,4,7)))
            for site in result['sites']:
                self.assertEqual((site['guest_rip'],site['guest_offset'],site['native_offset']),(0x102000+offset,0,4))
            if step in (2,7):
                shifted=export.verify(DATA/f'step-{step}-shifted',dict(case=case,guest_rip=0x102080+offset))
                self.assertEqual(shifted['body_hex'],result['body_hex'])
        self.assertEqual(export.verify(DATA/'step-3-low')['body_hex'],export.verify(DATA/'step-6-low')['body_hex'])

    def test_step_source_site_and_requested_rip_mismatches_reject(self):
        self.mutated('step-2-low','outputs/001.native',lambda b:b[:16]+bytes([b[16]^1])+b[17:])
        self.mutated('step-7-low','source.x86',lambda b:b[1:])
        self.mutated('step-2-low','producer.log',lambda b:b.replace(b'guest_offset=0 native_offset=12',b'guest_offset=1 native_offset=12'))
        with self.assertRaises(ValueError):export.verify(DATA/'step-7-low',dict(guest_rip=0x102000))
        with self.assertRaises(ValueError):export.verify(DATA/'step-7-low',dict(case='fex_moffs_load32_step4'))

    def test_actual_protected32_object_and_sparse_pc_map(self):
        result=export.verify(DATA/'load32',dict(case='fex_moffs_load32',guest_rip=0x102000,bitness=32))
        self.assertEqual(len(bytes.fromhex(result['body_hex'])),64)
        self.assertEqual([(s['native_offset'],s['guest_offset'],s['width'],s['write']) for s in result['sites']],
                         [(16,10,4,True),(24,17,4,False),(48,29,2,False)])
        self.assertEqual(result['guest_pc'],[dict(guest_offset=0,native_offset=4),dict(guest_offset=10,native_offset=20)])

    def test_protected32_modes_source_and_body_are_bound(self):
        def mode(raw):
            record=json.loads(raw);record['bitness']=64
            return json.dumps(record).encode()
        self.mutated('load32','compiler.json',mode)
        self.mutated('load32','producer.log',lambda b:b.replace(b'NATEMU_MODE bitness=32',b'NATEMU_MODE bitness=64'))
        self.mutated('load32','input.bin',lambda b:(64).to_bytes(8,'little')+b[8:])
        self.mutated('load32','source.x86',lambda b:b[:-1]+bytes([b[-1]^1]))
        self.mutated('load32','outputs/001.native',lambda b:b[:28]+bytes([b[28]^1])+b[29:])
        with self.assertRaises(ValueError): export.verify(DATA/'load32',dict(bitness=64))

    def test_protected32_rejects_source_window_outside_32_bits(self):
        for rip in (0x100000000,0xffffffff-35,0xffff800000102000):
            def change(raw):
                record=json.loads(raw);record['guest_rip']=rip
                return json.dumps(record).encode()
            self.mutated('load32','compiler.json',change)

    def test_actual_alias_objects_bind_both_guest_rips(self):
        for kind in ('xor','or'):
            for half,rip in [('low',0x102000),('high',0xffff800000102000)]:
                result=export.verify(DATA/(kind+'-'+half),dict(case='fex_alias_'+kind+'64',guest_rip=rip))
                self.assertEqual([site['native_offset'] for site in result['sites']],[16,36])
                self.assertEqual(len(bytes.fromhex(result['body_hex'])),44)

    def mutated(self,name,relative,mutator):
        with tempfile.TemporaryDirectory() as tmp:
            directory=Path(tmp)/name;shutil.copytree(DATA/name,directory)
            path=directory/relative;path.write_bytes(mutator(path.read_bytes()))
            with self.assertRaises(ValueError): export.verify(directory)

    def test_changed_native_instruction_rejected(self):
        def change(raw): return raw[:12]+bytes([raw[12]^1])+raw[13:]
        self.mutated('load-high','outputs/001.native',change)

    def test_replacement_stack_is_a_distinct_actual_emission(self):
        original=bytes.fromhex(export.CASES['fex_push_immediates64']['body'])
        for half,rip in [('low',0x102000),('high',0xffff800000102000)]:
            result=export.verify(DATA/('replacement-'+half),dict(case='fex_push_immediates64_changed',guest_rip=rip))
            changed=bytes.fromhex(result['body_hex'])
            self.assertEqual([i for i,(a,b) in enumerate(zip(original,changed)) if a!=b],[12])
            self.assertEqual(changed[12],0x74)

    def test_guest_pc_mismatch_rejected(self):
        self.mutated('push-high','producer.log',lambda b:b.replace(b'guest_offset=5 native_offset=24',b'guest_offset=6 native_offset=24'))

    def test_guest_rip_mismatch_rejected(self):
        self.mutated('load-high','producer.log',lambda b:b.replace(b'NATEMU_ENTRY index=1 guest_rip=18446603336222253056',b'NATEMU_ENTRY index=1 guest_rip=65536'))

    def test_changed_request_source_rejected(self):
        self.mutated('load-high','input.bin',lambda b:b[:-1]+b'\x04')

    def test_changed_delivery_rejected(self):
        self.mutated('load-high','body.bin',lambda b:b[:-1]+bytes([b[-1]^1]))

    def test_incomplete_lifecycle_rejected(self):
        def change(raw):
            record=json.loads(raw);record['commands'].pop()
            return json.dumps(record).encode()
        self.mutated('load-high','compiler.json',change)

    def test_requested_identity_rejected(self):
        with self.assertRaises(ValueError): export.verify(DATA/'load-high',dict(guest_rip=0x102000))

if __name__=='__main__': unittest.main()
