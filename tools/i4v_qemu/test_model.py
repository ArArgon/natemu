import copy, importlib.util, pathlib, struct, tempfile, unittest
from model import *

class ModelTests(unittest.TestCase):
    def fixture(self, case):
        g=[0x1200000000000000+i for i in range(16)];g[0]=65553;g[3]=BASE;g[2]=BASE+4096;g[4]=BASE+4104
        if case=='stack': g[2]=g[4]
        m=bytearray([0xa5])*8192;m[:8]=(42).to_bytes(8,'little')
        eg,em=expected(case,g,m)
        return {'case':case,'source':SOURCES[case],'initial_gpr':g,'expected_gpr':eg,'expected_rflags':0x202,'expected_rip':RIP+len(bytes.fromhex(SOURCES[case])),
                'resident_gpr_indices':list(range(16)),'resident_memory_bytes':8192,'memory_before':bytes(m).hex(),'memory_after':em.hex()}
    def test_all_scalar_cases_and_corruptions(self):
        for case in SOURCES:
            p=self.fixture(case);output=struct.pack('<18Q',*p['expected_gpr'],0x202,p['expected_rip'])+bytes.fromhex(p['memory_after'])
            compare(p,output)
            for offset in (0,32,128,136,144,144+8191):
                bad=bytearray(output);bad[offset]^=1
                with self.assertRaises(ValueError):compare(p,bad)
    def test_audit_requires_all_cases(self):
        runner=pathlib.Path(__file__).resolve().parent/'run_i4v_qemu.py'
        if not runner.exists(): runner=runner.parent.parent/'run_i4v_qemu.py'
        spec=importlib.util.spec_from_file_location('comparison_runner',runner)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as tmp:
            root=pathlib.Path(tmp);(root/'cases').mkdir()
            with self.assertRaisesRegex(ValueError,'exact six'): module.audit(root)
            (root/'cases'/'load').mkdir()
            with self.assertRaisesRegex(ValueError,'exact six'): module.audit(root)
    def test_stack_literal(self):
        p=self.fixture('stack');g=p['expected_gpr'];self.assertEqual(g[4],BASE+4092)
        self.assertEqual(g[0],0xffffffffbebdbcbb);self.assertEqual(g[3],0x51526162)
    def test_capture_preserves_before_scratch(self):
        p=self.fixture('load');s=assembly(p);capture=s.split('capture:\n')[1].split('pushfq')[0].splitlines()
        self.assertEqual(capture[:16],[f'mov [0x104000+{i*8}], {r}' for i,r in enumerate(REGS)])
        self.assertEqual(capture[16],'mov rsp, 0x107000')
        self.assertNotIn('call ',s);self.assertNotIn('ret\n',s)
    def test_payload_source_binding(self):
        p=self.fixture('load');data=bytearray(0x7000);data[:8192]=bytes.fromhex(p['memory_before']);data[8192:8195]=bytes.fromhex(p['source'])
        self.assertEqual(elf(data,p)[:4],b'\x7fELF');data[8192]^=1
        with self.assertRaises(ValueError):elf(data,p)
    def test_resident_projection_rejects_corruption(self):
        p=self.fixture('load');before={'gpr':p['initial_gpr'],'rip':RIP,'rflags':0x202}
        after={'gpr':p['expected_gpr'],'rip':RIP+3,'rflags':0x202}
        packet=[0]*16;packet[7:11]=[RIP,64,3,0x038b48]
        r={'status':1,'detail':0,'stage':2,'rip':RIP,'completed':1,'request':packet,
           'memory_before':list(bytes.fromhex(p['memory_before'])[:256]),
           'recovery':{'guest_before':before,'guest_after':after,'memory_after':list(bytes.fromhex(p['memory_after'])[:256])}}
        project('load',r)
        for target,key in (('packet',10),('packet',7),('gpr',0),('gpr',15),('memory',255)):
            bad=copy.deepcopy(r)
            if target=='packet':bad['request'][key]^=1
            elif target=='gpr':bad['recovery']['guest_after']['gpr'][key]^=1
            else:bad['recovery']['memory_after'][key]^=1
            with self.assertRaises(ValueError):project('load',bad)
    def test_all_flags_bits_are_checked(self):
        p=self.fixture('lfence')
        output=struct.pack('<18Q',*p['expected_gpr'],0x202,p['expected_rip'])+bytes.fromhex(p['memory_after'])
        compare(p,output)
        for bit in range(64):
            output=struct.pack('<18Q',*p['expected_gpr'],0x202^(1<<bit),p['expected_rip'])+bytes.fromhex(p['memory_after'])
            with self.assertRaisesRegex(ValueError,'full RFLAGS'):compare(p,output)

if __name__=='__main__':unittest.main()
