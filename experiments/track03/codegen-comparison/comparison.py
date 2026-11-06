#!/usr/bin/env python3
"""Retained emitted-code comparison; every relocated edge is an explicit adapter."""
import argparse, hashlib, importlib.util, json, os, random, resource, shutil, struct, sys, time
from pathlib import Path
sys.dont_write_bytecode=True
HERE=Path(__file__).resolve().parent
REPO=HERE.parents[2]
STATE_BYTES=32768
RUNS={'box64':'box64-consolidated-01','fex':'fex-consolidated-02','tcg':'tcg-consolidated-01'}
def load(p,n):
 s=importlib.util.spec_from_file_location(n,p);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);return m
def sha(b):return hashlib.sha256(b).hexdigest()
def initial(base,candidate,name,variant,helper_env=None,guarded_fp=False,fault=0):
 old=base.initial(candidate,'live_load',variant,0);blob=bytearray(old[:760])+bytearray(STATE_BYTES)+bytearray(old[760+4096:])
 blob[760:760+4096]=old[760:760+4096]
 if candidate=='tcg' and helper_env is not None:blob[776:776+len(helper_env)]=helper_env
 gprs=[0,0 if variant==0 else 1,1,0x40020000,0x40020100]
 if name=='branch_carry':gprs[1]=(1<<64)-1 if variant==0 else (1<<63)-1
 if name=='integer_div':gprs[1]=100 if variant==0 else (1<<64)-1;gprs[3]=3 if variant==0 else 7
 regs={'box64':[10,11,12,13,17],'fex':[4,7,5,6,11]}
 for j,value in enumerate(gprs):
  off=(regs[candidate][j]*8 if candidate!='tcg' else 776+[0,8,16,24,56][j]);struct.pack_into('<Q',blob,off,value)
 if candidate=='box64':struct.pack_into('<Q',blob,27*8,0x401000)
 v0=bytes(range(16));v1=bytes(i^0xa5 for i in range(16))
 if name.startswith('fp_'):
  x=1.5 if variant==0 else 3.; y=.5 if name=='fp_addsub' else (4. if variant==0 else 9.) if name=='fp_sqrt' else 2.
  v0=struct.pack('<dQ',x,0x8877665544332211);v1=struct.pack('<dQ',y,0x1020304050607080)
  if guarded_fp:
   mxcsr=0x1f80 if fault==2 else 0x1fa0
   struct.pack_into('<I',blob,760+0x7f00,mxcsr)
   if candidate=='tcg':
    struct.pack_into('<H',blob,776+936,0x10);struct.pack_into('<I',blob,776+944,mxcsr)
   if fault==3:v1=struct.pack('<Q',0x7ff8000000000001)+v1[8:]
   if fault==4:
    if name=='fp_sqrt':v1=struct.pack('<d',-y)+v1[8:]
    else:
     v0=struct.pack('<Q',0x7fefffffffffffff)+v0[8:]
     if name=='fp_addsub':v1=struct.pack('<Q',0x7fefffffffffffff)+v1[8:]
 for j,value in enumerate((v0,v1)):
  off=248+(16+j)*16 if candidate=='fex' else 760+0x90+j*16 if candidate=='box64' else 776+960+j*64
  blob[off:off+16]=value
 return blob

def fp_profile(candidate,prefix):
 # The resident owner, not FEX's masked MXCSR field, supplies full controls/status.
 saved,t0,t1=(1,2,3) if candidate=='box64' else (2,0,1)
 return [f'mrs x{saved},nzcv',f'mov x{t0},#0xff00',f'movk x{t0},#0x4001,lsl#16',f'ldr w{t0},[x{t0}]',
         f'ubfx w{t1},w{t0},#6,#10',f'cmp w{t1},#126',f'b.ne {prefix}_fp_profile_reject',f'tbz w{t0},#5,{prefix}_fp_profile_reject']

def fp_checks(candidate,name,prefix,post=False):
 saved,t0,t1=(1,2,3) if candidate=='box64' else (2,0,1)
 dst=24 if candidate=='box64' else 16
 sources=([1] if candidate=='box64' else [17]) if name=='fp_sqrt' else ([0,1] if candidate=='box64' else [16,17])
 a=[]
 for reg in ([dst] if post else sources):
  target=prefix+('_fp_post_reject' if post else '_fp_pre_reject')
  a += [f'fmov x{t0},d{reg}']
  if not post and name=='fp_sqrt':a += [f'tbnz x{t0},#63,{target}']
  a += [f'ubfx x{t0},x{t0},#52,#11',f'sub x{t0},x{t0},#{2 if post else 1}',f'cmp x{t0},#{2044 if post else 2045}',f'b.hi {target}']
 if not post and candidate=='fex':a += ['mov v0.16b,v16.16b']
 return a

def generate(source,raw,helper=None,guarded_fp=False):
 base=load(source/'execute.py','original_producer');guards=load(source/'boundary.py','guard_producer')
 fixtures=json.loads((source/'comparison-fixtures.json').read_text())['Instructions'];names=['nop']+list(fixtures)
 # Reuse only the permanent native boot sequence, not the old body selection.
 # The existing retained boundary build contains the exact common boot preamble.
 retained=raw/'boundary-execution-01/source/execution-generated.S'
 sums=dict(line.split('  ',1)[::-1] for line in (retained.parents[1]/'SHA256SUMS').read_text().splitlines())
 assert sha(retained.read_bytes())==sums['source/execution-generated.S']
 preamble=retained.read_text().split('.globl _body_0_start')[0]
 a=[preamble];cases=[];entries=[];header=['#define BOUNDARY_PROBE 1','#define COMPARISON_PROBE 1','#define CODE_LIMIT 0x10000u','#define STATE_BYTES 32768u','#define PROBE_FPCR 6u','struct body {const char *start,*body,*resume,*reject_scalar,*reject_vector;unsigned base_index;};']
 if guarded_fp:header += ['#define GUARDED_FP_PROBE 1']
 helper_symbols={};helper_env=None
 if helper:
  # Helper producer owns exact linked layout and initialized upstream CPU state.
  layout=json.loads((helper/'layout.json').read_text());helper_symbols={row.split()[2]:int(row.split()[0],16) for row in (helper/'symbols.txt').read_text().splitlines() if len(row.split())==3};helper_env=(helper/'env.bin').read_bytes()
  parent=helper.parent;checks=dict(line.split('  ',1)[::-1] for line in (parent/'SHA256SUMS').read_text().splitlines())
  for name in ('helpers.bin','layout.json','env.bin','symbols.txt'):
   assert sha((helper/name).read_bytes())==checks['outputs/'+name]
  assert layout['env_size']==len(helper_env) and len(helper_env)<STATE_BYTES-16 and layout['xmm0']==960 and layout['xmm1']==1024
  (source/'helper-symbols.json').write_text(json.dumps(helper_symbols,indent=2)+'\n')
  shutil.copyfile(helper/'helpers.bin',source/'helpers.bin');shutil.copyfile(helper/'layout.json',source/'helper-layout.json');shutil.copyfile(helper/'env.bin',source/'helper-env.bin')
 for c,(candidate,runname) in enumerate(RUNS.items()):
  run=raw/runname;sums=dict(line.split('  ',1)[::-1] for line in (run/'SHA256SUMS').read_text().splitlines())
  rawfixtures=json.loads((run/'source/fixtures.json').read_text())['Instructions'];assert {n:r['ExpectedBytes'] for n,r in rawfixtures.items()}=={n:r['ExpectedBytes'] for n,r in fixtures.items()}
  for k,name in enumerate(names):
   i=c*7+k;prefix=f'_body_{i}';path=run/'outputs'/(f'{k:03}.native' if candidate=='fex' else name+'.native');data=path.read_bytes();assert sha(data)==sums[str(path.relative_to(run))]
   rawname=f'{candidate}-{name}.native';(source/rawname).write_bytes(data)
   available=not(candidate=='tcg' and (name.startswith('fp_') or name=='integer_div') and not helper)
   entry=0x10000 if candidate=='fex' else 0x401000;size=1 if name=='nop' else len(bytes.fromhex(fixtures[name]['ExpectedBytes']))
   record=dict(index=i,base_index=c*7,candidate=candidate,fixture=name,available=available,status='bound' if available else 'unbound-actual-helper',entry=entry,guest_bytes=size,raw_path=rawname,raw_sha256=sha(data),fpcr=6,relocations=[])
   fp_guard=guarded_fp and candidate!='tcg' and name.startswith('fp_')
   if guarded_fp:record['guarded_fp']=True
   if fp_guard:record['reject_label']=prefix+'_fp_reject_resume'
   cases.append(record)
   for variant in (0,1):
    for fault in ((0,2,3,4) if fp_guard else (0,)):(source/f'{i}-{variant}-{fault}.input').write_bytes(initial(base,candidate,name,variant,helper_env,guarded_fp,fault))
   header.append(f'extern const char body_{i}_start[],body_{i}_code[],body_{i}_resume[];')
   reject=f'body_{i}_fp_reject_resume' if fp_guard else f'body_{i}_resume'
   if fp_guard:header.append(f'extern const char {reject}[];')
   entries.append(f'{{body_{i}_start,body_{i}_code,body_{i}_resume,{reject},{reject},{c*7}}}')
   a += ['.p2align 4',f'.globl {prefix}_start,{prefix}_code,{prefix}_resume',prefix+'_start:']
   for reg,off in ((1,128),(2,136),(8,144),(9,152),(29,160)):a += [f'str x{reg},[sp,#{off}]']
   a += ['ldr x29,[sp,#8]','dsb sy','isb','mrs x30,cntvct_el0','str x30,[sp]',prefix+'_code:']
   if fp_guard:a += fp_profile(candidate,prefix)
   if not available:a += ['hvc #0x49']
   scalar=None;patch={};ranges=[];literal_patches={}
   if name=='nop':pass
   elif candidate=='box64':
    if name.startswith('fp_'):ranges=[(0,20 if name=='fp_sqrt' else 28)]
    elif name.startswith('branch_'):
     zero=name=='branch_zero';end=44 if zero else 72;ranges=[(0,end)];scalar=4 if zero else 24;patch[36 if zero else 64]=('conditional',prefix+'_false')
    else:ranges=[(0,16),(92,132)];patch[12]=('conditional',prefix+'_divfast')
   elif candidate=='fex':
    if name.startswith('fp_'):ranges=[(12,16 if name=='fp_sqrt' else 20)]
    elif name.startswith('branch_'):
     zero=name=='branch_zero';end=32 if zero else 48;ranges=[(12,end)];scalar=16 if zero else 24;patch[end-4]=('conditional',prefix+'_true')
    else:ranges=[(12,28),(68,84)];patch[24]=('conditional',prefix+'_divfast')
   elif name.startswith('branch_'):
    zero=name=='branch_zero';ranges=[(12,108 if zero else 140)];scalar=32 if zero else 44
    patch.update({88 if zero else 120:('branch',prefix+'_false'),104 if zero else 136:('branch',prefix+'_true')})
   elif available:
    ranges=[(8,56 if name=='fp_sqrt' else 76 if name=='integer_div' else 84)]
    if name in ('fp_addsub','fp_muldiv'):
     helpers=['helper_addsd','helper_subsd'] if name=='fp_addsub' else ['helper_mulsd','helper_divsd'];literal_patches={28:helpers[0],60:helpers[1]};patch[80]=('branch',prefix+'_done')
    elif name=='fp_sqrt':literal_patches={32:'helper_sqrtsd'};patch[52]=('branch',prefix+'_done')
    else:literal_patches={48:'helper_divq_EAX'};patch[72]=('branch',prefix+'_done')
   record['ranges']=ranges
   for segment,(lo,hi) in enumerate(ranges):
    if segment:
     a += [f'b {prefix}_reject',prefix+'_divfast:']
    for off in range(lo,hi,4):
     word=struct.unpack_from('<I',data,off)[0]
     is_fp=fp_guard and off in ((8,) if name=='fp_sqrt' and candidate=='box64' else (8,16) if candidate=='box64' else (12,) if name=='fp_sqrt' else (12,16))
     if is_fp:a += fp_checks(candidate,name,prefix)
     if off==scalar:
      assert word&0xfffffc00 in (0xf9400000,0xf87f6800)
      a += guards.guard('x'+str((word>>5)&31),8,prefix+'_reject')
      record['scalar_raw_offset']=off
     if off in patch:
      kind,target=patch[off];record['relocations'].append(dict(offset=off,word=word,kind=kind,target=target))
      if kind=='branch':assert word&0xfc000000==0x14000000;a += [f'b {target}']
      else:
       assert word&0xff000010==0x54000000 or word&0x7e000000==0x34000000
       if word&0xff000010==0x54000000:
        condition=('eq','ne','hs','lo','mi','pl','vs','vc','hi','ls','ge','lt','gt','le','al','nv')[word&15];a += [f'b.{condition} {target}']
       else:
        opcode='cbnz' if word&(1<<24) else 'cbz';register=('x' if word&(1<<31) else 'w')+str(word&31);a += [f'{opcode} {register},{target}']
     elif off in literal_patches:
      target=literal_patches[off];assert word&0xff00001f==0x58000010
      a += [f'ldr x16,{prefix}_{target}'];record['relocations'].append(dict(offset=off,word=word,kind='literal',symbol=target))
     else:a += [f'.incbin "{rawname}",{off},4']
     if is_fp:a += fp_checks(candidate,name,prefix,post=True)
   if fp_guard:a += [f'msr nzcv,x{1 if candidate=="box64" else 2}']
   if name.startswith('branch_'):
    if candidate=='box64':a += [f'b {prefix}_true',prefix+'_false:',f'.incbin "{rawname}",{64 if name=="branch_zero" else 92},4',f'b {prefix}_false_count']
    elif candidate=='fex':a += [f'b {prefix}_false',prefix+'_false:',f'b {prefix}_false_count']
    else:a += [prefix+'_false:',f'b {prefix}_false_count']
    a += [prefix+'_true:','ldr x8,[sp,#72]','add x8,x8,#1','str x8,[sp,#72]','mov x8,#1',f'b {prefix}_branch_count',prefix+'_false_count:','mov x8,#0',prefix+'_branch_count:','str x8,[sp,#88]',f'mov x9,#{size}', 'add x9,x9,x8,lsl#4',f'mov x8,#{entry&65535}',f'movk x8,#{entry>>16},lsl#16','add x9,x9,x8',f'b {prefix}_count']
   a += [prefix+'_done:',f'mov x9,#{(entry+size)&65535}',f'movk x9,#{(entry+size)>>16},lsl#16',prefix+'_count:','str x9,[sp,#80]','ldr x8,[sp,#64]','add x8,x8,#1','str x8,[sp,#64]','sub x29,x29,#1',f'cbnz x29,{prefix}_code','dsb sy','isb','mrs x30,cntvct_el0']
   for reg,off in ((1,128),(2,136),(8,144),(9,152),(29,160)):a += [f'ldr x{reg},[sp,#{off}]']
   a += ['hvc #0x44',prefix+'_resume:',f'b {prefix}_resume',prefix+'_reject:','hvc #0x48',f'b {prefix}_reject']
   if fp_guard:
    a += [prefix+'_fp_post_reject:']
    if candidate=='fex':a += ['mov v16.16b,v0.16b']
    a += [prefix+'_fp_pre_reject:']
    if candidate=='box64':a += ['str q0,[x0,#0x90]']
    a += [prefix+'_fp_profile_reject:',f'msr nzcv,x{1 if candidate=="box64" else 2}','msr fpsr,xzr','dsb sy','isb','mrs x30,cntvct_el0']
    for reg,off in ((1,128),(2,136),(8,144),(9,152),(29,160)):a += [f'ldr x{reg},[sp,#{off}]']
    a += ['hvc #0x47',f'.globl {prefix}_fp_reject_resume',prefix+'_fp_reject_resume:',f'b {prefix}_fp_reject_resume']
   for target in literal_patches.values():a += ['.p2align 3',prefix+'_'+target+':',f'.quad {helper_symbols[target]}']
 a += ['.p2align 11','_execution_vectors:']
 for _ in range(16):a += ['b execution_fault','.space 124']
 a += ['execution_fault:','hvc #0x46','_execution_fault_resume:','b _execution_fault_resume']
 if helper:a += ['.org 0x8000','.incbin "helpers.bin"']
 a += ['_execution_end:'];header += ['#define BODY_COUNT 21','static const struct body bodies[BODY_COUNT]={'+','.join(entries)+'};']
 (source/'execution-generated.S').write_text('\n'.join(a)+'\n');(source/'execution-generated.h').write_text('\n'.join(header)+'\n');(source/'comparison-cases.json').write_text(json.dumps(cases,indent=2)+'\n')
 fmt=dict(state_bytes=STATE_BYTES,fpcr=6,boundary_start=32,boundary_bytes=224,counters=dict(completed=64,taken=72,rip=80,outcome=88),api_base=25,api_per_sample=140)
 if guarded_fp:fmt['guarded_fp']=True
 (source/'comparison-format.json').write_text(json.dumps(fmt,indent=2)+'\n')
 admission=[(e['index'],v,n,0) for e in cases if e['available'] for v in (0,1) for n in (1,2,3,64)]
 if guarded_fp:admission += [(e['index'],v,1,f) for e in cases if e['candidate']!='tcg' and e['fixture'].startswith('fp_') for v in (0,1) for f in (2,3,4)]
 timing=[];rng=random.Random(20260922)
 for repeat in range(7):
  rows=[(e['index'],repeat%2,n,0) for e in cases if e['available'] for n in (65536,262144)];rng.shuffle(rows);timing+=rows
 for phase,rows in [('admission',admission),('timing',timing)]: (source/(phase+'.schedule')).write_text(''.join('%d %d %d %d\n'%r for r in rows))
 return cases

def main():
 p=argparse.ArgumentParser();p.add_argument('--name',required=True);p.add_argument('--raw',required=True,type=Path);p.add_argument('--helpers',type=Path);p.add_argument('--execute',action='store_true');p.add_argument('--guarded-fp',action='store_true');p.add_argument('--purpose',choices=('exploratory','decision-evidence'),default='exploratory');p.add_argument('--rebuild-from',type=Path);args=p.parse_args()
 r=load(REPO/'experiments/track01/source/run.py','retention');run=r.allocate(os.environ.get('NATEMU_RAW_ROOT'),REPO,args.name);source=run/'source';source.mkdir();record=dict(schema='natemu.track03.comparison.v2' if args.guarded_fp else 'natemu.track03.comparison.v1',purpose=args.purpose,status='preparing',commands=[],started_utc=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()))
 try:
  if args.rebuild_from:
   origin=args.rebuild_from.resolve();checks=dict(line.split('  ',1)[::-1] for line in (origin/'SHA256SUMS').read_text().splitlines())
   names={p.name for p in (origin/'source').iterdir() if p.is_file()};assert {'source/'+n for n in names}=={n for n in checks if n.startswith('source/')}
   for name in names:
    data=(origin/'source'/name).read_bytes();assert sha(data)==checks['source/'+name];(source/name).write_bytes(data)
   assert (HERE/'comparison.py').read_bytes()==(source/'comparison.py').read_bytes(), 'rebuild driver differs from retained producer'
   assert sha((origin/'manifest.json').read_bytes())==checks['manifest.json']
   old=json.loads((origin/'manifest.json').read_text());assert old['purpose']==args.purpose and old['status'] in ('exploratory_completed','decision_evidence_completed')
   record['rebuild_from']=str(origin);record['origin_manifest_sha256']=checks['manifest.json'];record['cases']=json.loads((source/'comparison-cases.json').read_text())
   assert bool(record['cases'][0].get('guarded_fp'))==args.guarded_fp
  else:
   for name in ('comparison.py','comparison-fixtures.json','consolidated-bindings.json','consolidated-bindings.md','check_comparison.py','check_execution.py','execute.py','boundary.py','execution.c','README.md'):shutil.copyfile(HERE/name,source/name)
   shutil.copyfile(REPO/'experiments/track01/source/run.py',source/'retention.py');shutil.copyfile(HERE.parent/'native-handoff/entitlements.plist',source/'entitlements.plist');producer=load(source/'comparison.py','sealed_comparison');record['cases']=producer.generate(source,args.raw.resolve(),args.helpers.resolve() if args.helpers else None,args.guarded_fp)
  r=load(source/'retention.py','sealed_retention');r.save(run,record)
  checker=load(source/'check_comparison.py','comparison_oracle');record['inputs']=checker.check_inputs(source);r.save(run,record)
  for cmd in (['sw_vers'],['uname','-a'],['sysctl','hw.model'],['xcrun','clang','--version'],['xcrun','--show-sdk-version']):r.command(run,record,cmd)
  r.command(run,record,['xcrun','clang','-arch','arm64','-c',source/'execution-generated.S','-Wa,-I'+str(source),'-o',run/'guest.o'],timeout=60)
  r.command(run,record,['xcrun','clang','-arch','arm64','-std=c11','-Wall','-Wextra','-Werror','-O2',source/'execution.c',run/'guest.o','-framework','Hypervisor','-o',run/'probe'],timeout=60)
  for binary in ('guest.o','probe'):r.command(run,record,['xcrun','otool','-tvV',run/binary])
  if args.execute:
   if resource.getrlimit(resource.RLIMIT_CPU)!=(resource.RLIM_INFINITY,resource.RLIM_INFINITY):raise ValueError('inherited CPU limit')
   r.command(run,record,['codesign','--force','--sign','-','--entitlements',source/'entitlements.plist',run/'probe']);r.command(run,record,['codesign','--verify','--verbose=2',run/'probe']);deadline=time.monotonic()+180
   for phase in ('admission','timing'):
    output=r.command(run,record,[run/'probe',source/(phase+'.schedule'),source],timeout=min(90,max(1,deadline-time.monotonic())),cpu_limit=False);record[phase]=checker.check([json.loads(l) for l in output.splitlines()],source,phase);r.save(run,record)
   record['status']='exploratory_completed' if args.purpose=='exploratory' else 'decision_evidence_completed'
  else:record['status']='built_not_executed'
 except BaseException as e:record['status']='failed';record['error']=repr(e);raise
 finally:record['finished_utc']=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime());r.save(run,record);r.checksums(run);print(run)
if __name__=='__main__':main()
