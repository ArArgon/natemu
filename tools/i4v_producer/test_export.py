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
