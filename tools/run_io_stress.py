#!/usr/bin/env python3
"""Bounded native I/O stress. Normal runs have no scheduling hooks; forced builds are separate.
Each VM repeats real PTE/code DMA, CRC-free exact values, split atomics, read windows,
cancellation and IRQ/HLT cycles. Forced timings are diagnostic, never production rates.
"""
import argparse
import copy
import fcntl
import json
import math
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import run_io_quiet as quiet
import run_io_windows as windows
from io_common import ROOT, VM_LOCK

BASE,CONTROL,FUNCTION=quiet.BASE,quiet.BASE+0x800,quiet.FUNCTION
SENTINEL=0xcafe1234feed5678

def fixture(work,n,cycles,seed,forced):
    quiet.fixture(work)
    old=(work/'program.txt').read_text().splitlines()
    lines=[]
    for line in old:
        if line=='cpu': break
        if line.startswith(('body 0x100000 ', 'body 0x101000 ', 'region ')): continue
        if line.startswith(f'memory {BASE:#x} '):
            b=bytearray(0x820);quiet.word(b,0x818,SENTINEL);line=f'memory {BASE:#x} {b.hex()}'
        lines.append(line)
    ends=[]
    for v in range(n):
        assembly=f'''mov rsp, {0x300ff0+v*0x1000:#x}
mov rdi, {BASE+v*64:#x}
lidt [0x401000]
mov rax, cr4
or eax, 0x80
mov cr4, rax
mov eax, 0x500000
mov cr3, rax
xor r8d, r8d
xor r9d, r9d
xor eax, eax
mov r14, [rax]
mov eax, {FUNCTION:#x}
call rax
mov [rdi+8], r14
mov [rdi+16], rax
mov [rdi], r8
wait:
sti
hlt
mov r8, [{CONTROL:#x}]
cmp r8, -1
je end
xor eax, eax
mov r14, [rax]
mov eax, {FUNCTION:#x}
call rax
mov [rdi+8], r14
mov [rdi+16], rax
xor r9d, r9d
spin:
lock not qword ptr [{BASE+0x43c:#x}]
lock inc qword ptr [{BASE+0x47c:#x}]
inc r9
mov [rdi+32], r9
mov [rdi], r8
cmp r8, [{CONTROL+8:#x}]
jne spin
jmp wait
end:
nop
'''
        (work/f'cpu-{v}.asm').write_text('.intel_syntax noprefix\n'+assembly)
        code=windows.ref.assemble(assembly);start=0x100000+v*0x1000;ends.append(start+len(code))
        lines += [f'body {start:#x} {code.hex()}',f'region {0x300000+v*0x1000:#x} 0x1000']
    for v in range(n): lines+=['cpu',f'rip {0x100000+v*0x1000:#x}','cpl 0','rflags 0x2','if 0','svr 0x1ff']
    (work/'program.txt').write_text('\n'.join(lines)+'\n')
    config={'cpus':n,'cycles':cycles,'seed':seed,'forced':forced,'ends':ends,'ram':0x4000000}
    (work/'fixture.json').write_text(json.dumps(config,indent=2)+'\n');return config

def judge(report,c):
    errors=[]
    def need(ok,why):
        if not ok:errors.append(why)
    io=report['io_stress'];cpus=report['cpus'];n=c['cpus'];cycles=c['cycles']
    for k in ('cpus','cycles','seed','forced'):need(io[k]==c[k],f'{k} binding')
    need(io['instrumented']==c['forced'],'normal run must be uninstrumented / forced must be instrumented')
    need(io['active_reads']==cycles and io['mixed_reads']==cycles and io['windows']>=7*cycles+2,'every cycle verified while active plus quiet window sequence')
    need(all(type(io[k]) is int and 0<=io[k]<2**64 for k in ('split_baseline','split_final','split_operations','counter_baseline','counter_final')),'unsigned nonoverflowing split counters')
    if not c['forced']:need(io['split_baseline']==0 and io['counter_baseline']==0,'zero normal baseline')
    need(io['split_baseline'] in (0,2**64-1) and io['split_final']==(io['split_baseline'] ^ ((2**64-1) if io['split_operations']%2 else 0)) and io['split_operations']>=cycles*n,'quiescent cumulative split parity')
    need(io['counter_final']-io['counter_baseline']==io['split_operations'],'exact shared counter increment count')
    need(0<io['seconds']<110 and math.isfinite(io['cycles_per_second']) and io['cycles_per_second']>0 and abs(io['cycles_per_second']*io['seconds']-cycles)<0.001,'bounded elapsed/rate')
    need(io['canceled_epochs']==(cycles if c['forced'] else 0),'announced epoch accounting')
    need(len(cpus)==n,'participant count')
    for v,cpu in enumerate(cpus):
        regs=[int(x,0) if isinstance(x,str) else x for x in cpu['gpr']]
        need(cpu['stop']=='reached' and int(cpu['rip'],0)==c['ends'][v],f'CPU{v} clean end')
        need(regs[8]==2**64-1 and regs[9]>0,f'CPU{v} final progress')
        need(regs[14]==(quiet.A if cycles%2==0 else quiet.B),f'CPU{v} final data')
        need(regs[10]>=cycles+1,f'CPU{v} real IRQs')
        need(cpu['stats']['halts']>=cycles+1 and cpu['stats']['splits']>=cycles,f'CPU{v} repeated HLT/split')
        need(cpu['frame_intact'] and not cpu['tampered'],f'CPU{v} frame')
    if c['forced']:
        events=io['forced_evidence'];by={e['stage']:e for e in events}
        names=['late-shadow-exact-ack','real-peer-shadows','late-shadow-served','request-before-quiet','withdraw-before-request','request-before-withdraw','quiet-withdrawn-before-grant','withdrawal-checkpoint','cancel-lost-after-grant','hook-counts']
        need([e['stage'] for e in events]==names,'all forced stages in order')
        for name in names:
            e=by[name]
            if name=='hook-counts':
                need(len(e['hits'])==n+1 and e['hits'][0]>=6 and e['hits'][1]>=1 and e['hits'][-1]>=2 and e['timeouts']==[0]*(n+1),'observed hooks/no timeout');continue
            if name=='late-shadow-served':
                need(e['late_after']>e['late_before'] and e['resets_after']>e['resets_before'],'actual late shadow reset service');continue
            need(e['copy_pending'] and e['released']<e['request'] and e['released_after']<e['request'],'positive unfinished epoch '+name)
        for name in ['late-shadow-exact-ack','real-peer-shadows']:
            need(by[name]['ack'][0]==by[name]['request'] and by[name]['ack'][1]<by[name]['request'],'late split unfinished '+name)
        for name in ['request-before-quiet','withdraw-before-request','quiet-withdrawn-before-grant']:
            need(by[name]['quiet'][0]==0 and by[name]['ack'][0]<by[name]['request'],'unacknowledged boundary '+name)
        need(by['request-before-withdraw']['quiet'][0]==1,'host admitted actual quiet')
        need(by['withdrawal-checkpoint']['ack'][0]==by['withdrawal-checkpoint']['request'],'withdrawal exact checkpoint')
        need(len(io['announced_cancellations'])==cycles,'every forced cycle has announced cancellation')
        for item in io['announced_cancellations']:
            e=item['completed'];before=item['pending']
            need(item['cancel_won'] and before['copy_pending'] and before['released']<before['request'] and before['released_after']<before['request'] and before['request']==e['request'] and before['written']==e['written'] and before['tables_written']==e['tables_written'],'announced cancellation pending/effect invariants')
            need(e['stage']=='announced-canceled' and not e['copy_pending'] and e['released']==e['request']==e['released_after'] and e['tables_written']<=e['written']<e['request'],'canceled epoch release without write')
    else:need(io['forced_evidence'] is None and io['announced_cancellations']==[],'no forced hooks in normal stress')
    return errors

def memory_judge(text,c,expected_pattern=None,expected_counter=None):
    rows=[l[len('io-memory: '):] for l in text.splitlines() if l.startswith('io-memory: ')]
    if len(rows)!=1:return ['missing/duplicate post-VM RAM record']
    memories=json.loads(rows[0]);errors=[]
    if len(memories)!=c['cpus']:return ['RAM participant count']
    for entries in memories:
        found=[e for e in entries if int(e['addr'],0)==BASE]
        if len(found)!=1:errors.append('shared RAM record count');continue
        b=bytes.fromhex(found[0]['data'])
        if len(b)!=0x820:errors.append('shared RAM size');continue
        w=lambda i:int.from_bytes(b[i:i+8],'little')
        if w(0x800)!=2**64-1 or w(0x808)!=c['cycles'] or w(0x818)!=SENTINEL:errors.append('control/cancel sentinel RAM')
        if w(0x43c) not in (0,2**64-1):errors.append('torn split RAM')
        if expected_pattern is not None and w(0x43c)!=expected_pattern:errors.append('final RAM/parity record mismatch')
        if expected_counter is not None and w(0x47c)!=expected_counter:errors.append('final RAM/counter record mismatch')
        for v in range(c['cpus']):
            if (w(v*64),w(v*64+8),w(v*64+16))!=(c['cycles'],quiet.A if c['cycles']%2==0 else quiet.B,c['cycles']+1) or not w(v*64+32):errors.append(f'CPU{v} final per-cycle RAM')
    return errors

def self_check():
    for n in (2,4):
        with tempfile.TemporaryDirectory() as d:fixture(Path(d),n,512,123,False)
    # Functional oracle controls remain useful without launching either resident flavor.
    c={'cpus':2,'cycles':2,'seed':123,'forced':False,'ends':[100,200]}
    cpus=[]
    for end in c['ends']:
        g=[0]*16;g[8]=2**64-1;g[9]=1;g[10]=3;g[14]=quiet.A
        cpus.append({'gpr':g,'rip':hex(end),'stop':'reached','stats':{'halts':3,'splits':2},'frame_intact':True,'tampered':False})
    good={'cpus':cpus,'io_stress':dict(cpus=2,cycles=2,seed=123,forced=False,instrumented=False,active_reads=2,mixed_reads=2,windows=16,canceled_epochs=0,seconds=1.,cycles_per_second=2.,forced_evidence=None,announced_cancellations=[],split_baseline=0,split_final=0,split_operations=4,counter_baseline=0,counter_final=4)}
    assert not judge(good,c),judge(good,c)
    for field,value in [('active_reads',1),('mixed_reads',1),('instrumented',True),('cycles',1),('windows',1),('split_final',2**64-1),('split_operations',3),('counter_final',3),('counter_baseline',1),('counter_final',2**64+4),('split_operations',2**64+4)]:
        bad=copy.deepcopy(good);bad['io_stress'][field]=value;assert judge(bad,c)
    for reg,value in [(8,0),(9,0),(10,0),(14,quiet.B)]:
        bad=copy.deepcopy(good);bad['cpus'][0]['gpr'][reg]=value;assert judge(bad,c)
    b=bytearray(0x820)
    for v in range(2):
        for off,value in [(0,2),(8,quiet.A),(16,3),(32,1)]:quiet.word(b,v*64+off,value)
    quiet.word(b,0x800,2**64-1);quiet.word(b,0x808,2);quiet.word(b,0x818,SENTINEL)
    def text(b):return 'io-memory: '+json.dumps([[dict(addr=hex(BASE),data=b.hex())]]*2)
    quiet.word(b,0x47c,4)
    assert not memory_judge(text(b),c,0,4)
    bad=bytearray(b);bad[0x47c]^=1;assert memory_judge(text(bad),c,0,4)
    for offset in [0,8,16,32,0x800,0x808,0x818]:
        bad=bytearray(b);bad[offset]^=1;assert memory_judge(text(bad),c)
    fc=copy.deepcopy(c);fc['forced']=True
    fg=copy.deepcopy(good);fio=fg['io_stress'];fio['forced']=True;fio['instrumented']=True;fio['canceled_epochs']=2
    def event(name,ack=None,q=None):
        return dict(stage=name,request=10,released=9,released_after=9,written=8,tables_written=8,ack=ack or [9,9],quiet=q or [0,1],copy_pending=True)
    events=[event('late-shadow-exact-ack',[10,9]),event('real-peer-shadows',[10,9]),
            dict(stage='late-shadow-served',late_before=0,late_after=1,resets_before=1,resets_after=2),
            event('request-before-quiet'),event('withdraw-before-request'),event('request-before-withdraw',q=[1,1]),
            event('quiet-withdrawn-before-grant'),event('withdrawal-checkpoint',[10,9]),event('cancel-lost-after-grant'),
            dict(stage='hook-counts',hits=[6,1,2],timeouts=[0,0,0])]
    fio['forced_evidence']=events
    pending=event('announced-cancel-pending');completed=copy.deepcopy(pending)
    completed.update(stage='announced-canceled',released=10,released_after=10,copy_pending=False)
    fio['announced_cancellations']=[dict(pending=copy.deepcopy(pending),completed=copy.deepcopy(completed),cancel_won=True) for _ in range(2)]
    assert not judge(fg,fc),judge(fg,fc)
    for index,key,value in [(0,'ack',[9,9]),(1,'ack',[10,10]),(1,'copy_pending',False),(2,'late_after',0),(2,'resets_after',1),
                            (3,'released_after',10),(4,'quiet',[1,1]),(5,'quiet',[0,1]),(7,'ack',[9,9]),(9,'timeouts',[0,1,0]),(9,'hits',[0,1,2])]:
        bad=copy.deepcopy(fg);bad['io_stress']['forced_evidence'][index][key]=value;assert judge(bad,fc),(index,key)
    for key,value in [('written',9),('tables_written',9),('released_after',9),('copy_pending',True)]:
        bad=copy.deepcopy(fg);bad['io_stress']['announced_cancellations'][0]['completed'][key]=value;assert judge(bad,fc),key
    bad=copy.deepcopy(fg);bad['io_stress']['announced_cancellations'][0]['cancel_won']=False;assert judge(bad,fc)
    print('N2/N4 assembled; normal, forced-stage, parity, cancellation, progress and RAM negative controls passed; no build/VM')

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--self-check',action='store_true');p.add_argument('--prepare-only',action='store_true');p.add_argument('--forced',action='store_true')
    p.add_argument('--cpus',type=int,choices=(2,4),default=2);p.add_argument('--cycles',type=int);p.add_argument('--seed',type=int,default=0x15a4e35)
    for name in ('host','runtime','fexc','out'):p.add_argument('--'+name,type=Path)
    p.add_argument('--lock',type=Path,default=VM_LOCK)
    a=p.parse_args()
    if a.self_check:self_check();return 0
    if a.cycles is None:a.cycles=64 if a.forced else 512
    if not 0<a.cycles<=(64 if a.forced else 512) or not 0<a.seed<2**64:p.error('cycle/seed bounds')
    if a.out is None:p.error('--out required')
    if not a.prepare_only and any(getattr(a,k) is None for k in ('host','runtime','fexc')):p.error('run requires host/runtime/fexc')
    a.out=a.out.resolve();a.out.mkdir(parents=True,exist_ok=False);c=fixture(a.out,a.cpus,a.cycles,a.seed,a.forced)
    sources=[Path(__file__),Path(quiet.__file__),Path(windows.__file__),ROOT/'tools/reference/ref.py',ROOT/'tools/reference/flags.py']
    manifest={'fixture':c,'sources':{str(x):windows.digest(x) for x in sources},'limits':{'vm':0 if a.prepare_only else 1,'seconds':120},'binaries':{}}
    for k in ('host','runtime','fexc'):
        if getattr(a,k):
            f=getattr(a,k).resolve(strict=True);setattr(a,k,f);manifest['binaries'][k]={'path':str(f),'sha256':windows.digest(f)}
    (a.out/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    if a.prepare_only:return 0
    command=['/usr/bin/time','-lp',str(a.host),str(a.runtime),'run-program',str(a.fexc),str(a.out/'program.txt'),str(a.out),'--io-stress','--io-stress-cycles',str(a.cycles),'--io-stress-seed',str(a.seed),'--watchdog-ms','110000','--ram',hex(c['ram']),'--code-shape']
    if a.forced:command+=['--io-stress-forced']
    (a.out/'command.json').write_text(json.dumps(command,indent=2)+'\n')
    start=time.monotonic();timeout=False
    env=dict(os.environ);env.pop("NATEMU_PERF_LOG",None)
    with a.lock.open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        with (a.out/'stdout.log').open('wb') as out,(a.out/'stderr.log').open('wb') as err:
            process=subprocess.Popen(command,stdin=subprocess.DEVNULL,stdout=out,stderr=err,env=env,start_new_session=True)
            try:process.wait(timeout=120)
            except subprocess.TimeoutExpired:timeout=True;windows.stop_group(process)
            except BaseException:windows.stop_group(process);raise
    errors=[];report=None;text=(a.out/'stderr.log').read_text(errors='replace')
    if timeout or process.returncode:errors.append('process failure/timeout')
    if f'teardown: vcpus {a.cpus} joined {a.cpus} destroyed {a.cpus} vm_destroy 0 children none' not in text.splitlines():errors.append('clean teardown absent')
    try:report=json.loads((a.out/'stdout.log').read_text());errors+=judge(report,c);errors+=memory_judge(text,c,report['io_stress']['split_final'],report['io_stress']['counter_final'])
    except (KeyError,IndexError,ValueError,TypeError) as e:errors.append('invalid output: '+str(e))
    timing={k:float(v) for k,v in re.findall(r'^\s*(real|user|sys)\s+([0-9.]+)\s*$',text,re.M)}
    if set(timing)!={'real','user','sys'}:errors.append('missing host CPU timing')
    result={'pass':not errors,'errors':errors,'timeout':timeout,'returncode':process.returncode,'elapsed':time.monotonic()-start,'host_time':timing,'io_stress':report.get('io_stress') if isinstance(report,dict) else None}
    (a.out/'result.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result),flush=True);return int(bool(errors))
if __name__=='__main__':raise SystemExit(main())
