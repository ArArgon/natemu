#!/usr/bin/env python3
"""Bounded exploratory execution of sealed emitter bytes; no generator selection."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import random
import resource
import shutil
import struct
import sys
import time
sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
NAMES = ['nop','bare_load','live_load','bare_store','live_store','bare_alias_load','live_alias_load']
RUNS = {'box64':'box64-export-02','fex':'fex-export-01','tcg':'tcg-export-02'}
BASE, STATE, DATA = 0x40000000, 0x40018000, 0x40020000

def load(path, name):
    spec=importlib.util.spec_from_file_location(name,path)
    m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m

def ranges(candidate, name, hardware=False):
    if hardware:
        if candidate == "fex": return (12,12 if name=="nop" else 48 if name.startswith("live") else 16)
        if candidate == "tcg": return (12,120) if name.startswith("live") else (16,16 if name=="nop" else 28)
        return (0,0 if name=="nop" else 56 if name.startswith("live") else 4)
    if candidate=='fex': return (12,12 if name=='nop' else 56 if name.startswith('live') else 20)
    if candidate=='tcg': return (12,128) if name.startswith('live') else (16,16 if name=='nop' else 32)
    return (0, {'nop':0,'bare_load':4,'bare_alias_load':4,'bare_store':12,'live_load':64,'live_alias_load':64,'live_store':68}[name])

def initial(candidate, name, variant, fault):
    regs=[0]*31;vectors=bytearray(512);state=bytearray(4096);data=bytearray(4096)
    rax=DATA;rbx=0x50000000 if fault else DATA
    if fault and 'alias' in name: rax=0x50000000
    rcx=0xffffffffffffffff if variant==0 else 0x7fffffffffffffff
    rdx=1
    xmm0=bytes(range(16));xmm1=bytes(0xa5 ^ i for i in range(16))
    struct.pack_into('<Q',data,0,DATA+128);struct.pack_into('<Q',data,128,DATA)
    def qword(at,v):struct.pack_into('<Q',state,at,v)
    if candidate=='fex':
        regs[26]=1  # Raw PF=0; native inverse carry is initialized by the probe.
        for r,v in ((4,rax),(7,rcx),(5,rdx),(6,rbx),(11,DATA+256)):regs[r]=v
        vectors[256:272]=xmm0;vectors[272:288]=xmm1
    elif candidate=='tcg':
        regs[19]=STATE+16
        for at,v in ((0,rax),(8,rcx),(16,rdx),(24,rbx),(56,DATA+256)):qword(16+at,v)
        state[16+960:16+976]=xmm0;state[16+1024:16+1040]=xmm1
    else:
        # Exact state offsets are checked against the retained producer in execution-bindings.md.
        regs[0]=STATE
        for r,v in ((10,rax),(11,rcx),(12,rdx),(13,rbx),(17,DATA+256)):regs[r]=v
        regs[26]=0x202
        # Independently derived from the exact retained Box64 emitter.
        state[BOX64_XMM0:BOX64_XMM0+16]=xmm0
        state[BOX64_XMM0+16:BOX64_XMM0+32]=xmm1
    return struct.pack('<31Q',*regs)+vectors+state+data

# Exact pinned Box64 emu->xmm offset.
BOX64_XMM0 = 0x90

def generate(source, raw, paired=False):
    provenance=[];assembly=['.text','.p2align 4','.globl _execution_start, _execution_end, _execution_boot_resume, _execution_vectors, _execution_fault_resume','_execution_start:',
      'msr daifset, #15','msr mair_el1,x10','msr ttbr0_el1,x11','msr tcr_el1,x12','dsb sy','isb','tlbi vmalle1','dsb sy','isb','mrs x16,sctlr_el1','orr x16,x16,#1','orr x16,x16,#4','orr x16,x16,#0x1000','msr sctlr_el1,x16','isb','mrs x9,cntfrq_el0','hvc #0x43','_execution_boot_resume:','b _execution_boot_resume']
    header=['struct body {const char *start, *body, *resume;};'];entries=[]
    if BOX64_XMM0<0: raise ValueError('Box64 pinned state binding not finished')
    configurations=[(candidate,False) for candidate in RUNS]
    if paired: configurations += [(candidate,True) for candidate in RUNS]
    for c,(candidate,hardware) in enumerate(configurations):
        run=raw/(candidate+'-hardware-01' if hardware else RUNS[candidate])
        if hardware and json.loads((run/'manifest.json').read_text()).get('ordering_profile')!='hardware-tso-dependent':raise ValueError('hardware profile identity')
        hashes={line.split('  ',1)[1]:line.split('  ',1)[0] for line in (run/'SHA256SUMS').read_text().splitlines()}
        for f,name in enumerate(NAMES):
            i=c*7+f;file=f'outputs/{f:03d}.native' if candidate=='fex' else f'outputs/{name}.native'
            native=(run/file).read_bytes()
            if hashlib.sha256(native).hexdigest()!=hashes[file]:raise ValueError('sealed source hash mismatch')
            lo,hi=ranges(candidate,name,hardware)
            if not 0<=lo<=hi<=len(native) or lo%4 or hi%4:raise ValueError('native range')
            (source/f'{i}.native').write_bytes(native);(source/f'{i}.body').write_bytes(native[lo:hi])
            provenance.append(dict(index=i,ordering_profile='hardware-tso-dependent' if hardware else 'software-ordering',candidate=candidate,fixture=name,run=str(run),path=file,sha256=hashes[file],start=lo,end=hi,body_hex=native[lo:hi].hex()))
            assembly+=['.p2align 4',f'.globl _body_{i}_start, _body_{i}_code, _body_{i}_resume',f'_body_{i}_start:',
              'dsb sy','isb','mrs x30,cntvct_el0','str x30,[sp]','ldr x30,[sp,#8]',f'_body_{i}_code:',f'.incbin "{i}.body"',
              'sub x30,x30,#1',f'cbnz x30,_body_{i}_code','dsb sy','isb','mrs x30,cntvct_el0','hvc #0x44',f'_body_{i}_resume:',f'b _body_{i}_resume']
            header.append(f'extern const char body_{i}_start[], body_{i}_code[], body_{i}_resume[];');entries.append(f'{{body_{i}_start,body_{i}_code,body_{i}_resume}}')
            for variant in range(2):
                for fault in range(2):(source/f'{i}-{variant}-{fault}.input').write_bytes(initial(candidate,name,variant,fault))
    assembly+=['.p2align 11','_execution_vectors:']
    for _ in range(16): assembly+=['b execution_fault','.space 124']
    assembly+=['execution_fault:','hvc #0x46','_execution_fault_resume:','b _execution_fault_resume','_execution_end:']
    header += [f'#define BODY_COUNT {len(entries)}',f'static const struct body bodies[{len(entries)}]={{'+','.join(entries)+'};']
    (source/'execution-generated.S').write_text('\n'.join(assembly)+'\n');(source/'execution-generated.h').write_text('\n'.join(header)+'\n')
    (source/'extraction.json').write_text(json.dumps(provenance,indent=2)+'\n')
    # Correctness admission precedes any long timed batches. All ordering retained.
    admission=[(i,v,n,0) for i in range(len(entries)) for v in range(2) for n in (1,2,3)]
    admission += [(c*7+f,0,1,1) for c in range(len(configurations)) for f in (1,2,3,4,5,6)]
    rng=random.Random(20260920);timing=[]
    for repeat in range(9):
        batch=[(i,repeat%2,n,0) for i in range(len(entries)) for n in (262144,1048576)]
        rng.shuffle(batch);timing+=batch
    for name,rows in [('admission',admission),('timing',timing)]:
        (source/(name+'.schedule')).write_text(''.join('%d %d %d %d\n'%r for r in rows))
    return provenance

def main():
    p=argparse.ArgumentParser();p.add_argument('--name',required=True);p.add_argument('--raw',required=True);p.add_argument('--execute',action='store_true');p.add_argument('--paired-profiles',action='store_true');args=p.parse_args()
    helper=REPO/'experiments/track01/source/run.py';retention=load(helper,'retention_original');run=retention.allocate(os.environ.get('NATEMU_RAW_ROOT'),REPO,args.name);source=run/'source';source.mkdir()
    for path in (HERE/'execute.py',HERE/'execution.c',HERE/'check_execution.py',HERE/'execution-bindings.md',HERE/'README.md',HERE.parent/'native-handoff/entitlements.plist',helper):
        target=source/('retention.py' if path==helper else path.name);shutil.copyfile(path,target)
        if path.read_bytes()!=target.read_bytes():raise ValueError('source changed during snapshot')
    retention=load(source/'retention.py','retention_sealed');record=dict(schema='natemu.track03.extracted-execution.v2',purpose='exploratory',commands=[],status='preparing',started_utc=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),budget=dict(build_seconds=120,execution_seconds=180,cells=42 if args.paired_profiles else 21,vcpus=1,ram_bytes=0x40000))
    try:
        retention.save(run,record)
        # Execute generation from the retained producer, not the mutable worktree.
        producer=load(source/'execute.py','producer');record['extractions']=producer.generate(source,Path(args.raw).resolve(),args.paired_profiles);retention.save(run,record)
        for cmd in (['sw_vers'],['uname','-a'],['sysctl','hw.model'],['xcrun','clang','--version'],['xcrun','--show-sdk-version']):retention.command(run,record,cmd)
        retention.command(run,record,[sys.executable,'-B',source/'check_execution.py','--self-check'])
        retention.command(run,record,['xcrun','clang','-arch','arm64','-c',source/'execution-generated.S','-Wa,-I'+str(source),'-o',run/'guest.o'],timeout=60)
        retention.command(run,record,['xcrun','clang','-arch','arm64','-std=c11','-Wall','-Wextra','-Werror','-O2',source/'execution.c',run/'guest.o','-framework','Hypervisor','-o',run/'probe'],timeout=60)
        retention.command(run,record,['xcrun','otool','-tvV',run/'guest.o'])
        retention.command(run,record,['xcrun','otool','-tvV',run/'probe'])
        if args.execute:
            if resource.getrlimit(resource.RLIMIT_CPU)!=(resource.RLIM_INFINITY,resource.RLIM_INFINITY):raise ValueError('inherited CPU limit')
            retention.command(run,record,['codesign','--force','--sign','-','--entitlements',source/'entitlements.plist',run/'probe'])
            retention.command(run,record,['codesign','--verify','--verbose=2',run/'probe'])
            checker=load(source/'check_execution.py','checker');checker.check_inputs(source);deadline=time.monotonic()+180
            for phase in ('admission','timing'):
                text=retention.command(run,record,[run/'probe',source/(phase+'.schedule'),source],timeout=min(90,max(1,deadline-time.monotonic())),cpu_limit=False)
                rows=[json.loads(line) for line in text.splitlines()]
                record[phase]=checker.check(rows,source,phase);retention.save(run,record)
            record['status']='exploratory_completed'
        else:record['status']='built_not_executed'
    except BaseException as e:record['status']='failed';record['error']=type(e).__name__+': '+str(e);raise
    finally:
        record['finished_utc']=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime());retention.save(run,record);retention.checksums(run);print(run)
if __name__=='__main__':main()
