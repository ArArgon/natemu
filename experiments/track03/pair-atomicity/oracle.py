#!/usr/bin/env python3
"""Pure oracle; samples are HVC register exports, never host reads of live RAM."""
import copy
import json
import sys
BASE = 0x40000000
N = 8000000
TCR = 16 | (1<<8) | (1<<10) | (3<<12) | (1<<23) | (2<<32)
DESC = BASE+0x10000 | 3 | (1<<10) | (3<<8) | (3<<53)
MASK = (1<<64)-1

def check(rows):
    layouts=[r for r in rows if r.get('kind')=='layout']
    if len(layouts)!=1 or rows[0]!=layouts[0] or rows[-1]!={'kind':'complete'}:
        raise ValueError('layout/completion identity')
    l=layouts[0]
    if l['mode'] not in ('control','paired') or type(l['writer']) is not int or l['writer'] not in (0,1):
        raise ValueError('cell identity')
    if (l['iterations'],l['bytes'],l['tcr'],l['descriptor']) != (N,0x24000,TCR,DESC):
        raise ValueError('layout configuration')
    for key in ('boot_resume','done_resume','load_pc','store_pc','split_low_pc','split_high_pc'):
        if type(l[key]) is not int or not BASE<=l[key]<BASE+4096 or l[key]%4:
            raise ValueError('native site')
    if len({l[k] for k in ('boot_resume','done_resume','load_pc','store_pc','split_low_pc','split_high_pc')})!=6:
        raise ValueError('aliased native sites')
    api=[r for r in rows if r.get('kind')=='api']
    if not api or any(type(r.get('rc')) is not int or r['rc']!=0 for r in api):
        raise ValueError('API failure')
    for call,count in (('hv_vm_create(NULL)',1),('hv_vm_destroy()',1),('hv_vcpu_create(&v,&out,NULL)',2),('hv_vcpu_destroy(v)',2),('hv_vcpu_run(v)',4)):
        if sum(r.get('call')==call for r in api)!=count: raise ValueError('API lifecycle count')
    boots=[r for r in rows if r.get('kind')=='boot']
    results=[r for r in rows if r.get('kind')=='result']
    if [r.get('cpu') for r in boots]!=[0,1] or [r.get('cpu') for r in results]!=[0,1]:
        raise ValueError('CPU identity')
    for b in boots:
        if (int(b['mmfr2'],16)>>32)&15 !=1 or [int(x,16) for x in b['values']] != [2,255,TCR,0x30d01985,DESC,DESC]:
            raise ValueError('native feature/setup')
    values=[]
    for r in results:
        if r['pc']!=l['done_resume'] or r['syndrome']>>26!=0x16 or r['syndrome']&0xffff!=0x44:
            raise ValueError('return checkpoint')
        v=[int(x,16) for x in r['regs']]
        if len(v)!=8 or any(x<0 or x>MASK for x in v): raise ValueError('register export')
        values.append(v)
    w=values[l['writer']]; r=values[1-l['writer']]
    if w[1:]!=[0]*7 or r[6:]!=[0,0] or r[1]>r[0] or r[2]>r[0]:
        raise ValueError('counter consistency')
    if l['mode']=='control':
        if w[0]!=1 or r[:6]!=[1,1,1,1,MASK,1]: raise ValueError('forced torn control missing')
        return dict(status='torn_control_detected',reads=1,changes=1,torn=1,writes=1)
    if r[2]:
        if (r[3]^r[4])==MASK: raise ValueError('bad-pair diagnostic not torn')
        return dict(status='torn_pair_observed',reads=r[0],changes=r[1],torn=r[2],writes=w[0])
    if r[3:5]!=[0,0] or r[5]>N or w[0]!=N: raise ValueError('completion/diagnostic consistency')
    status='finite_no_tearing_observed' if r[0]>=10000 and r[1]>=1000 else 'inconclusive_insufficient_progress'
    return dict(status=status,reads=r[0],changes=r[1],torn=0,writes=w[0])

def model(mode,writer):
    l=dict(kind='layout',mode=mode,writer=writer,iterations=N,bytes=0x24000,tcr=TCR,descriptor=DESC,
           boot_resume=BASE+128,done_resume=BASE+512,load_pc=BASE+200,store_pc=BASE+300,split_low_pc=BASE+240,split_high_pc=BASE+260)
    rows=[l]
    for call,count in (('hv_vm_create(NULL)',1),('hv_vm_destroy()',1),('hv_vcpu_create(&v,&out,NULL)',2),('hv_vcpu_destroy(v)',2),('hv_vcpu_run(v)',4)):
        rows += [dict(kind='api',call=call,rc=0) for _ in range(count)]
    rows += [dict(kind='boot',cpu=i,mmfr2='1001001102001011',values=[f'{v:016x}' for v in [2,255,TCR,0x30d01985,DESC,DESC]]) for i in (0,1)]
    w=[1 if mode=='control' else N]+[0]*7
    r=[1,1,1,1,MASK,1,0,0] if mode=='control' else [10000,1000,0,0,0,N,0,0]
    rows += [dict(kind='result',cpu=i,pc=l['done_resume'],syndrome=(0x16<<26)|0x44,regs=[f'{v:016x}' for v in (w if i==writer else r)]) for i in (0,1)]
    return rows+[dict(kind='complete')]

def self_check():
    for mode,writer in [('control',0),('paired',0),('paired',1)]:
        rows=model(mode,writer); assert check(rows)['status']==('torn_control_detected' if mode=='control' else 'finite_no_tearing_observed')
        for index,key,value in [(0,'iterations',1),(0,'descriptor',0),(0,'load_pc',BASE+1),(1,'rc',1)]:
            bad=copy.deepcopy(rows);bad[index][key]=value
            try: check(bad)
            except ValueError: pass
            else: raise AssertionError('mutant accepted')
    rows=model('paired',0); r=next(x for x in rows if x.get('kind')=='result' and x['cpu']==1)
    r['regs'][1]='0'; assert check(rows)['status']=='inconclusive_insufficient_progress'
    r['regs'][2]='1';r['regs'][3]='1';r['regs'][4]=f'{MASK:016x}'
    assert check(rows)['status']=='torn_pair_observed'
    print('3 models,12 rejected mutations,progress/torn classifications passed; no native execution')
if __name__=='__main__':
    if sys.argv[1:]==['--self-check']: self_check()
    else: print(json.dumps(check([json.loads(x) for x in sys.stdin if x.strip()]),sort_keys=True))
