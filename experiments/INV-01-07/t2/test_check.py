"""Author synthetic fixtures; no native timer execution or measurement."""
import copy
import json
import unittest
from fractions import Fraction
import check

Q=[0,0,33,33,0]

def base(mode):
    return dict(schema='M07-T2-raw-v1',mode=mode,run_id='1'*32,timebase=[1,1],complete=True,
                calibration_pre=[[i,100,100] for i in range(10000)],calibration_post=[[i,100,100] for i in range(10000)],cells=[])

def wake(mode='normal'):
    return dict(kind='wakeup',mechanism=check.WAKE[3],qos=check.QOS[0],target_ns=100000,qos_record=Q,callback_qos=[Q[:] for _ in range(2000)],
                rows=[[i,1100000+i*20000000,1100000+i*20000000,4567,1000000+i*20000000,1100250+i*20000000,1100300+i*20000000,1] for i in range(2000)])

def event():
    return dict(kind='event',mechanism=check.EVENT[2],qos=check.QOS[0],target_qos=Q,sender_qos=Q,
                rows=[[i,0,0,0,1000000+i*200,1000030+i*200,1000100+i*200,1] for i in range(2000)])

def cadence():
    return dict(kind='cadence',mechanism=check.WAKE[0],qos=check.QOS[0],workers=2,period_ns=1000000,epoch_tick=1000000,
        threads=[dict(id=n,start_tick=900000,end_tick=2001001000,qos_record=Q,
            rows=[[i,1000000+(i+1)*1000000,1000000+(i+1)*1000000,0,1000000+i*1000000+100,1000000+(i+1)*1000000+100,1000000+(i+1)*1000000+100,0] for i in range(2000)]) for n in range(2)])

class Checks(unittest.TestCase):
    def rejected(self,data,code):
        with self.assertRaises(check.Invalid) as e:check.validate(data)
        self.assertEqual(str(e.exception),code)

    def test_each_runtime_control_hits_named_guard(self):
        d=base('dispatch-early');d['cells']=[wake()];d['cells'][0]['rows'][0][5]=1099999
        self.rejected(d,'dispatch-early')
        d=base('rtt-zero');d['cells']=[event()];d['cells'][0]['rows'][0][6]=1000000
        self.rejected(d,'rtt-positive')
        d=base('cadence-duplicate');d['cells']=[cadence()];d['cells'][0]['threads'][0]['rows'][1][2]=2000000
        self.rejected(d,'cadence-deadline')

    def test_unmodified_control_is_not_negative_success(self):
        for mode,make in [('dispatch-early',wake),('rtt-zero',event),('cadence-duplicate',cadence)]:
            d=base(mode);d['cells']=[make()];self.rejected(d,'injection-not-detected')

    def test_strict_opaque_token_never_used_as_nanoseconds(self):
        d=base('dispatch-early');d['cells']=[wake()];d['cells'][0]['rows'][0][3]=987654321
        self.rejected(d,'injection-not-detected')
        d['cells'][0]['rows'][0][2]=987654321;self.rejected(d,'dispatch-representation')

    def test_boundary_quantiles_and_bool_counts(self):
        self.assertEqual(check.distribution(list(range(2000))),dict(min=0,p50=999,p99=1979,max=1999))
        d=base('rtt-zero');d['cells']=[event()];d['cells'][0]['rows'][0][0]=False
        self.rejected(d,'row-domain')
        d=base('rtt-zero');d['cells']=[event()];d['cells'][0]['rows'].pop();self.rejected(d,'sample-count')

    def test_qos_clock_calibration_and_cadence_mutations(self):
        d=base('cadence-duplicate');d['cells']=[cadence()];d['cells'][0]['threads'][1]['qos_record']=[0,0,9,9,0]
        self.rejected(d,'qos-mismatch')
        d=base('dispatch-early');d['cells']=[wake()];d['calibration_post'][9999][2]=99
        self.rejected(d,'calibration-order')
        d=base('cadence-duplicate');d['cells']=[cadence()];d['cells'][0]['threads'][0]['rows'][4][6]-=1
        self.rejected(d,'cadence-order')

    def complete(self):
        d=base('normal')
        for m in check.WAKE:
            for q in check.QOS:
                for target in (100000,1000000,10000000):
                    c=wake();c.update(mechanism=m,qos=q,target_ns=target)
                    c['qos_record']=[0,0,33 if q==check.QOS[0] else 9,33 if q==check.QOS[0] else 9,0];c['callback_qos']=[c['qos_record'][:] for _ in range(2000)]
                    for r in c['rows']:
                        r[1]=r[2]=r[4]+target;r[5]=r[2]+250;r[6]=r[5]+50
                        if not m.startswith('dispatch'):r[3]=r[7]=0
                    d['cells'].append(c)
        for n in (2,4,8):
            for q in check.QOS:
                c=cadence();c.update(workers=n,qos=q);c['threads']=[copy.deepcopy(c['threads'][0]) for _ in range(n)]
                for i,w in enumerate(c['threads']):w['id']=i;w['qos_record']=[0,0,33 if q==check.QOS[0] else 9,33 if q==check.QOS[0] else 9,0]
                d['cells'].append(c)
        for m in check.EVENT:
            for q in check.QOS:
                c=event();c.update(mechanism=m,qos=q,target_qos=[0,0,33 if q==check.QOS[0] else 9,33 if q==check.QOS[0] else 9,0]);c['sender_qos']=c['target_qos'][:];d['cells'].append(c)
        return d

    def test_complete_matrix_and_late_failure_transaction(self):
        d=self.complete()
        result=check.validate(d);self.assertEqual(len(result['cells']),36)
        self.assertEqual(result['instrument_floor_ns'],100)
        first=[x for x in result['cells'] if x['kind']=='cadence'][0]['workers'][0]
        self.assertEqual(first['achieved_events_per_s'],Fraction(2000*10**9,2000000100))
        d['cells'][-1]['rows'][-1][6]=1000000;self.rejected(d,'rtt-positive')

    def test_impossible_serial_chronology(self):
        baseline=self.complete();self.assertEqual(len(check.validate(baseline)['cells']),36)
        for kind in ('cadence','wakeup','event'):
            d=copy.deepcopy(baseline);c=next(c for c in d['cells'] if c['kind']==kind)
            rows=c['threads'][0]['rows'] if kind=='cadence' else c['rows']
            rows[0][6]=rows[1][6]+1000
            if kind!='event':rows[0][5]=rows[0][6]
            self.rejected(d,'row-chronology')
        for phase in ('calibration_pre','calibration_post'):
            d=copy.deepcopy(baseline);d[phase][0][1:]=[1000000,1000001]
            self.rejected(d,'calibration-chronology')

if __name__=='__main__':unittest.main()
