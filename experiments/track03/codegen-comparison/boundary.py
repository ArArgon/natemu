#!/usr/bin/env python3
"""Finite guard/resident-boundary sensitivity; adapters are handwritten, bodies are not."""
import argparse
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
sys.dont_write_bytecode=True
HERE=Path(__file__).resolve().parent
REPO=HERE.parents[2]

def load(path,name):
    s=importlib.util.spec_from_file_location(name,path);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);return m

def guard(address,size,reject):
    return [f'sbfx x1,{address},#0,#48',f'eor x1,x1,{address}',f'add x2,{address},#{size-1}',f'eor x2,x2,{address}','orr x1,x1,x2,lsr#47',f'cbnz x1,{reject}']

def normalize(candidate):
    # Return canonical six arithmetic flags in x9. All source recipes remain intact.
    if candidate=='fex':
        a=['ldr x8,[sp,#96]','ubfx x9,x8,#29,#1','eor x9,x9,#1']
        for sourcebit,destbit in ((30,6),(31,7),(28,11)):
            a += [f'ubfx x2,x8,#{sourcebit},#1',f'orr x9,x9,x2,lsl#{destbit}']
        a += ['eor x2,x26,x27','and x2,x2,#16','orr x9,x9,x2','mov x3,x26']
    else:
        if candidate=='box64':
            a=['ldr x1,[x0,#0x450]','ldr x2,[x0,#0x458]','ldr x3,[x0,#0x460]']
        else:
            a=['ldr x3,[x19,#272]','ldr x2,[x19,#280]','sub x1,x3,x2']
        a += ['cmp x3,x1','cset x9,lo','cmp x3,#0','cset x8,eq','orr x9,x9,x8,lsl#6',
              'lsr x8,x3,#63','orr x9,x9,x8,lsl#7','eor x8,x1,x2','mvn x18,x8',
              'eor x1,x1,x3','and x18,x18,x1','lsr x18,x18,#63','orr x9,x9,x18,lsl#11',
              'eor x8,x8,x3','and x8,x8,#16','orr x9,x9,x8']
    return a+['eor x2,x3,x3,lsr#4','eor x2,x2,x2,lsr#2','eor x2,x2,x2,lsr#1','mvn x2,x2','and x2,x2,#1','orr x9,x9,x2,lsl#2','str x9,[sp,#224]']

def semantic_words(candidate):
    # Each entry produces one word in x2. Helpers observe the same semantic order.
    if candidate=='box64':
        return [[f'mov x2,x{r}'] for r in (10,11,12,13,17)]+[['mov x2,x9']]+[[f'ldr x2,[x0,#{off}]'] for off in (0x90,0x98,0xa0,0xa8)]
    if candidate=='fex':
        return [[f'mov x2,x{r}'] for r in (4,7,5,6,11)]+[['mov x2,x9']]+[[f'umov x2,v{r}.d[{lane}]'] for r in (16,17) for lane in (0,1)]
    return [[f'ldr x2,[x19,#{off}]'] for off in (0,8,16,24,56)]+[['mov x2,x9']]+[[f'ldr x2,[x19,#{off}]'] for off in (960,968,1024,1032)]

def helper(candidate,mode):
    a=[f'_boundary_{candidate}_{mode}:']+normalize(candidate)
    words=semantic_words(candidate)
    if mode==2:
        for i,word in enumerate(words):a += word+[f'str x2,[sp,#{128+i*8}]']
        words=[[f'ldr x2,[sp,#{128+i*8}]'] for i in range(10)]
    a += ['mov x1,#0']
    for word in words:a += word+['ror x1,x1,#7','eor x1,x1,x2']
    a += ['ldr x2,[sp,#112]','add x2,x2,x1','str x2,[sp,#112]',
          'ldr x2,[sp,#104]','add x2,x2,#1','str x2,[sp,#104]']
    if mode==2:
        # Declared volatile clobbers. Original deferred flag recipes are callee-preserved.
        for r in (0,4,5,6,7,10,11,12,13,17):a += [f'mov x{r},#0']
        a += ['movi v16.16b,#0','movi v17.16b,#0']
        if candidate=='fex':
            for i,r in enumerate((4,7,5,6,11)):a += [f'ldr x{r},[sp,#{128+i*8}]']
            a += ['ldr q16,[sp,#176]','ldr q17,[sp,#192]']
        elif candidate=='box64':
            a += ['ldr x0,[sp,#80]']
            for i,r in enumerate((10,11,12,13,17)):a += [f'ldr x{r},[sp,#{128+i*8}]']
        # TCG's guest state is resident and x19 is preserved; it needs no reload.
    return a+['ret']


def generate(source,raw):
    base=load(source/'execute.py','base_producer');oracle=load(source/'check_execution.py','base_oracle')
    emission=source/'emission';emission.mkdir();original=base.generate(emission,raw,True);oracle.check_inputs(emission)
    preamble=(emission/'execution-generated.S').read_text().split('.globl _body_0_start')[0]
    a=[preamble];meta=[];header=['#define BOUNDARY_PROBE 1','#define CODE_LIMIT 0x10000u','struct body {const char *start,*body,*resume,*reject_scalar,*reject_vector;unsigned base_index;};'];entries=[]
    configs=[(0,0,0),(1,0,0)]+[(1,mode,k) for mode in (1,2) for k in (1,4,16,64)]
    for c,candidate in enumerate(base.RUNS):
        for f in (2,4,6):
            base_index=21+c*7+f;native=(emission/f'{base_index}.body').read_bytes();name=base.NAMES[f]
            scalar=[36,16,48 if f==4 else 44][c];vector=[48,28,96][c]
            scalar_reg=['x13' if f==4 else 'x10' if f==6 else 'x13','x6' if f==4 else 'x4' if f==6 else 'x6','x23' if f==4 else 'x22'][c]
            vector_reg=['x17','x11','x22'][c]
            for guarded,mode,k in configs:
                i=len(meta);prefix=f'_body_{i}';parts=[]
                # Guard insertion never changes any original emitter byte.
                for start,end in ((0,scalar),(scalar,vector),(vector,len(native))):
                    if guarded and start in (scalar,vector):parts += guard(scalar_reg if start==scalar else vector_reg,8 if start==scalar else 16,prefix+('_reject_scalar' if start==scalar else '_reject_vector'))
                    if end>start:parts += [f'.incbin "emission/{base_index}.body",{start},{end-start}']
                scalar_offset=scalar+(24 if guarded else 0)
                vector_offset=vector+(48 if guarded else 0)
                meta.append(dict(index=i,base_index=base_index,candidate=candidate,fixture=name,guarded=guarded,mode=mode,k=k,scalar_offset=scalar_offset,vector_offset=vector_offset,scalar_reg=scalar_reg,vector_reg=vector_reg))
                a += ['.p2align 4',f'.globl {prefix}_start,{prefix}_code,{prefix}_resume,{prefix}_scalar_resume,{prefix}_vector_resume',prefix+'_start:']
                for j,r in enumerate((1,2,3,8,9,18)):a += [f'str x{r},[sp,#{32+j*8}]']
                a += ['str x29,[sp,#240]',f'mov x29,#{k}','str x0,[sp,#80]','dsb sy','isb','mrs x30,cntvct_el0','str x30,[sp]','ldr x30,[sp,#8]',prefix+'_code:']+parts
                if mode:
                    if k>1:a += ['sub x29,x29,#1',f'cbnz x29,{prefix}_skip']
                    a += ['str x30,[sp,#88]','mrs x1,nzcv','str x1,[sp,#96]',f'bl _boundary_{candidate}_{mode}',
                          'ldr x1,[sp,#96]','msr nzcv,x1','ldr x30,[sp,#88]',f'mov x29,#{k}',prefix+'_skip:']
                a += ['sub x30,x30,#1',f'cbnz x30,{prefix}_code','dsb sy','isb','mrs x30,cntvct_el0']
                for j,r in enumerate((1,2,3,8,9,18)):a += [f'ldr x{r},[sp,#{32+j*8}]']
                a += ['ldr x29,[sp,#240]','hvc #0x44',prefix+'_resume:',f'b {prefix}_resume']
                for suffix,site in (('scalar',1),('vector',2)):
                    a += [prefix+'_reject_'+suffix+':',f'mov x1,#{site}','str x1,[sp,#232]','ldr x29,[sp,#240]','hvc #0x47',prefix+'_'+suffix+'_resume:',f'b {prefix}_{suffix}_resume']
                header.append(f'extern const char body_{i}_start[],body_{i}_code[],body_{i}_resume[],body_{i}_scalar_resume[],body_{i}_vector_resume[];')
                entries.append(f'{{body_{i}_start,body_{i}_code,body_{i}_resume,body_{i}_scalar_resume,body_{i}_vector_resume,{base_index%21}}}')
                for variant in (0,1):
                    for fault in range(8):
                        blob=bytearray(base.initial(candidate,name,variant,1 if fault==1 else 0))
                        if fault>=2:
                            address={2:1<<47,3:(1<<47)-4,4:1<<47,5:(1<<47)-8,6:(1<<64)-4,7:(1<<64)-8}[fault]
                            vector_bad=fault in (4,5,7)
                            if c==2:
                                off=56 if vector_bad else 0 if f==6 else 24;struct.pack_into('<Q',blob,760+16+off,address)
                            else:
                                reg=([17,11][c] if vector_bad else ([10,4][c] if f==6 else [13,6][c]));struct.pack_into('<Q',blob,reg*8,address)
                        (source/f'{i}-{variant}-{fault}.input').write_bytes(blob)
    for candidate in base.RUNS:
        for mode in (1,2):a += helper(candidate,mode)
    a += ['.p2align 11','_execution_vectors:']
    for _ in range(16):a += ['b execution_fault','.space 124']
    a += ['execution_fault:','hvc #0x46','_execution_fault_resume:','b _execution_fault_resume','_execution_end:']
    header += [f'#define BODY_COUNT {len(meta)}','static const struct body bodies[BODY_COUNT]={'+','.join(entries)+'};']
    (source/'execution-generated.S').write_text('\n'.join(a)+'\n');(source/'execution-generated.h').write_text('\n'.join(header)+'\n');(source/'boundary-cases.json').write_text(json.dumps(meta,indent=2)+'\n')
    admission=[]
    for e in meta:
        admission += [(e['index'],v,n,0) for v in (0,1) for n in (1,3,64)]
        if e['mode']==0:admission += [(e['index'],0,1,fault) for fault in range(1,8 if e['guarded'] else 2)]
    timing=[];rng=random.Random(20260921)
    for repeat in range(7):
        batch=[(e['index'],repeat%2,n,0) for e in meta for n in (65536,262144)];rng.shuffle(batch);timing+=batch
    for phase,rows in (('admission',admission),('timing',timing)):(source/(phase+'.schedule')).write_text(''.join('%d %d %d %d\n'%r for r in rows))
    return meta


def main():
    p=argparse.ArgumentParser();p.add_argument('--name',required=True);p.add_argument('--raw',required=True);p.add_argument('--execute',action='store_true');args=p.parse_args()
    frozen=REPO/'experiments/track01/source/run.py';r=load(frozen,'r');run=r.allocate(os.environ.get('NATEMU_RAW_ROOT'),REPO,args.name);source=run/'source';source.mkdir()
    record=dict(schema='natemu.track03.boundary.v1',purpose='exploratory',status='preparing',commands=[],started_utc=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()))
    try:
        for name in ('boundary.py','check_boundary.py','execute.py','execution.c','check_execution.py','README.md','execution-bindings.md'):shutil.copyfile(HERE/name,source/name)
        shutil.copyfile(frozen,source/'retention.py');shutil.copyfile(HERE.parent/'native-handoff/entitlements.plist',source/'entitlements.plist');r=load(source/'retention.py','sealed_retention');r.save(run,record)
        producer=load(source/'boundary.py','sealed_producer');record['cases']=producer.generate(source,Path(args.raw).resolve());r.save(run,record)
        checker=load(source/'check_boundary.py','boundary_oracle');record['inputs']=checker.check_inputs(source);r.save(run,record)
        for cmd in (['sw_vers'],['uname','-a'],['sysctl','hw.model'],['xcrun','clang','--version'],['xcrun','--show-sdk-version']):r.command(run,record,cmd)
        r.command(run,record,[sys.executable,'-B',source/'check_boundary.py','--self-check'])
        r.command(run,record,['xcrun','clang','-arch','arm64','-c',source/'execution-generated.S','-Wa,-I'+str(source),'-o',run/'guest.o'],timeout=60)
        r.command(run,record,['xcrun','clang','-arch','arm64','-std=c11','-Wall','-Wextra','-Werror','-O2',source/'execution.c',run/'guest.o','-framework','Hypervisor','-o',run/'probe'],timeout=60)
        for binary in ('guest.o','probe'):r.command(run,record,['xcrun','otool','-tvV',run/binary])
        if args.execute:
            if resource.getrlimit(resource.RLIMIT_CPU)!=(resource.RLIM_INFINITY,resource.RLIM_INFINITY):raise ValueError('inherited CPU limit')
            r.command(run,record,['codesign','--force','--sign','-','--entitlements',source/'entitlements.plist',run/'probe']);r.command(run,record,['codesign','--verify','--verbose=2',run/'probe'])
            deadline=time.monotonic()+180
            for phase in ('admission','timing'):
                text=r.command(run,record,[run/'probe',source/(phase+'.schedule'),source],timeout=min(90,max(1,deadline-time.monotonic())),cpu_limit=False)
                record[phase]=checker.check([json.loads(l) for l in text.splitlines()],source,phase);r.save(run,record)
            record['status']='exploratory_completed'
        else:record['status']='built_not_executed'
    except BaseException as e:record['status']='failed';record['error']=repr(e);raise
    finally:record['finished_utc']=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime());r.save(run,record);r.checksums(run);print(run)
if __name__=='__main__':main()
