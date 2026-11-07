"""Source-only negative controls for independent instruction-gap predicates."""
import copy
import unittest
from tools import run_instruction_gaps as r


class InstructionOracle(unittest.TestCase):
    def output(self,c,cpl=0):
        effects=c['effects'];regs=list(c['regs'])
        for i,v in effects.get('regs',{}).items():regs[i]=v
        at=c.get('base',r.CODE)+len(c.get('prefix',b'\x90'))
        f=c['fault'];fault=[f[0],f[1],f[2]|(4 if cpl==3 and f[0]==14 else 0),c.get('fault_at',at)] if f else [0]*4
        before=dict(xmm=['0x0']*32,ymmh=['0x0']*32,mxcsr=c.get('mxcsr',0x1f80),x87={'top':0})
        got=dict(before,stop='guest_fault' if f else 'reached',rip=at+len(c['code']),fault=fault,gpr=regs,rflags=c['wantflags'],
                 memory=[dict(addr=a,data=bytes(b).hex()) for a,b in effects.get('memory',c['memory']).items()],frame_intact=True)
        if 'mxcsr'in effects:got['mxcsr']=effects['mxcsr']
        return before,got

    def test_precise_fault_and_partial_store_negative_controls(self):
        c=next(c for c in r.enter_cases() if c['name']=='enter-64-fault-chain-read')
        before,got=self.output(c);self.assertEqual(r.judge(c,0,before,got),[])
        for field in (0,2,3):
            bad=copy.deepcopy(got);bad['fault'][field]^=1;self.assertTrue(r.judge(c,0,before,bad))
        for field in ('gpr','rflags','memory'):
            bad=copy.deepcopy(got)
            if field=='gpr':bad['gpr'][4]+=8
            elif field=='rflags':bad[field]^=1
            else:
                raw=bytearray.fromhex(bad['memory'][0]['data']);raw[0x2ff8]^=1;bad['memory'][0]['data']=raw.hex()
            self.assertTrue(r.judge(c,0,before,bad),field)

    def test_enter_partial_store_alias_and_probe_width(self):
        m=r.memory_image();sp=r.DATA+0x3000
        x=r.enter_model(m,sp,sp,8,2,0)
        # First push overwrites the chain source; the subsequent copy must observe that push.
        self.assertEqual(r.bytes_at(x['memory'],sp-16,8),sp.to_bytes(8,'little'))
        self.assertEqual(x['regs'],{4:sp-24,5:sp-8})
        c=next(c for c in r.additional_cases() if c['name']=='enter64-final-byte-before-unmapped')
        self.assertIsNone(c['fault']);self.assertEqual(c['effects']['regs'][4],r.DATA+0x1fff)

    def test_conversion_flags_and_memory_both_checked(self):
        c=next(c for c in r.half_cases() if c['name']=='cvt-tie-even-rc0-lanes4')
        before,got=self.output(c);self.assertFalse(r.judge(c,0,before,got))
        bad=copy.deepcopy(got);bad['mxcsr']^=0x20;self.assertTrue(r.judge(c,0,before,bad))
        bad=copy.deepcopy(got);raw=bytearray.fromhex(bad['memory'][0]['data']);raw[0x2000]^=1;bad['memory'][0]['data']=raw.hex()
        self.assertTrue(r.judge(c,0,before,bad))

    def test_unique_case_names_and_nonoverlapping_initial_memory(self):
        cases=list(r.cases());self.assertEqual(len(cases),516)
        self.assertEqual(len({c['name'] for c in cases}),len(cases))
        for c in cases:
            spans=sorted((a,a+len(b)) for a,b in c['memory'].items())
            self.assertTrue(all(a[1]<=b[0] for a,b in zip(spans,spans[1:])),c['name'])
            self.assertTrue(all(r.canonical(int(s.split()[1],0)) for s in c['setup'] if s.startswith(('fsbase ','gsbase '))),c['name'])


if __name__=='__main__':unittest.main()
