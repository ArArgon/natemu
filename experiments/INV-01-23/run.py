#!/usr/bin/env python3
"""M23-L1 retained-source campaign. No review/HEAD approval parser."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import random
import shutil
import signal
import subprocess
import sys
import time
import uuid
import check

if not __debug__:raise RuntimeError('M23 requires validation assertions enabled')
SOURCE=Path(__file__).resolve().parent
REPO=Path(os.environ.get('NATEMU_M23_REPO',SOURCE.parents[1])).resolve()
WALL=10800
CAP=1024**3
RESERVE=2*1024**3

def schedule(cpus):
    assert type(cpus) is int and 1<=cpus<=64
    jobs=[dict(n=n,strategy=s,qos=q,h=h,seed=seed,mode='normal',cpus=cpus)
          for n in (1,2,4,8) for s in range(3) for q in range(2) for h in (0,cpus,2*cpus) for seed in (23001,23002,23003)]
    random.Random(23000).shuffle(jobs)
    jobs += [dict(n=n,strategy=1 if mode=='missing-yield' else 2,qos=1,h=cpus,seed=seed,mode=mode,cpus=cpus)
             for mode in check.CONTROLS for n in (2,4,8) for seed in (23001,23002,23003)]
    return [dict(index=i,**job) for i,job in enumerate(jobs)]

def sha(path):
    digest=hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''):digest.update(block)
    return digest.hexdigest()
def inventory(root):return {str(p.relative_to(root)):sha(p) for p in sorted(root.rglob('*')) if p.is_file()}
def verify(root,files):
    for name,digest in files.items():
        assert not Path(name).is_absolute() and '..' not in Path(name).parts
        p=root/name;assert p.resolve()==p and p.is_file() and sha(p)==digest,'artifact mismatch'
def write(path,data):
    text=json.dumps(data,sort_keys=True,allow_nan=False)
    with Path(path).open('x') as f:f.write(text)
def read(path):return check.decode(Path(path).read_bytes())
def usage(root):return sum(p.stat().st_size for p in root.rglob('*') if p.is_file())

def command(args,directory,label):
    with (directory/(label+'.stdout')).open('xb') as out,(directory/(label+'.stderr')).open('xb') as err:
        env=dict(os.environ,M23_BUILD_VALIDATION='1',TMPDIR=str(directory/'synthetic-tmp')) if label=='build-4' else None
        p=subprocess.run(args,stdout=out,stderr=err,timeout=10,check=False,env=env)
    assert p.returncode==0,label+' failed'
    return (directory/(label+'.stdout')).read_text().strip()

def environment(directory,label):
    data={}
    for name,args in {'os':['sw_vers'],'hardware':['sysctl','hw.model','hw.ncpu','hw.memsize'],
                      'load':['uptime'],'power':['pmset','-g','batt'],'thermal':['pmset','-g','therm']}.items():
        data[name]=command(args,directory,label+'-'+name)
    write(directory/(label+'.json'),data);return data

def build(parent,run_id):
    root=parent/'producing-build';root.mkdir();snapshot=root/'snapshot';snapshot.mkdir();build_id=uuid.uuid4().hex;calls=[];status='failed'
    try:
        for p in SOURCE.iterdir():
            if p.suffix in ('.py','.c','.md'):shutil.copy2(p,snapshot/p.name)
        sdk=command(['xcrun','--sdk','macosx','--show-sdk-path'],root,'sdk')
        command(['xcrun','--sdk','macosx','--show-sdk-version'],root,'sdk-version')
        command(['xcrun','clang','--version'],root,'compiler')
        calls=[['xcrun','clang','-std=c11','-O2','-g','-Wall','-Wextra','-Werror','-pthread','-isysroot',sdk,
                '-DM23_REPO="'+str(REPO)+'"','-DM23_BUILD="'+build_id+'"',str(snapshot/'producer.c'),'-o',str(root/'liveness')],
               ['codesign','--force','--sign','-','--timestamp=none',str(root/'liveness')],
               ['codesign','-d','--entitlements',':-',str(root/'liveness')],
               ['xcrun','otool','-tvV',str(root/'liveness')],
               [sys.executable,'-B',str(snapshot/'tests.py')]]
        (root/'synthetic-tmp').mkdir()
        for i,args in enumerate(calls):command(args,root,'build-'+str(i))
        headers=root/'sdk-headers';headers.mkdir()
        for name in ('pthread.h','pthread/qos.h','sys/resource.h','mach/mach_time.h','_time.h'):
            shutil.copy2(Path(sdk)/'usr/include'/name,headers/name.replace('/','-'))
        for name in ('pthread_cond_wait.3','pthread_cond_timedwait.3'):
            shutil.copy2(Path(sdk)/'usr/share/man/man3'/name,headers/name)
        status='built'
    finally:write(root/'build.json',dict(schema='M23-L1-build-v1',run_id=run_id,build_id=build_id,status=status,commands=calls,files=inventory(root),native_campaign_executed=False))
    return root

def progress_keys(records,job):
    keys=set()
    for actor,rows in records.items():
        check.actor_prefix(rows,actor,job)
        if actor>job['n']:continue # valid helper work still never renews dependencies
        for r in rows:
            phase=r['phase']
            if phase not in check.ELIGIBLE:continue
            key=(actor,r['epoch'],phase,r['a'] if phase=='join' else 0)
            check.need(key not in keys,'duplicate-progress');keys.add(key)
            if phase=='join':check.one(records[r['a']],'exit',-1)
    return keys

def supervise(argv,directory,wall,cap,idle=None,job=None,identity=None):
    start=last=time.monotonic();seen=set();process=None;error=None;timed=no_progress=limited=disk_low=kill_requested=False
    try:
        with (directory/'stdout').open('xb') as out,(directory/'stderr').open('xb') as err:
            process=subprocess.Popen(argv,stdout=out,stderr=err,start_new_session=job is None)
            while True:
                now=time.monotonic();timed=now-start>=wall
                limited=usage(directory)>=cap;disk_low=shutil.disk_usage(directory).free<RESERVE
                if job:
                    rows,_=check.load(directory,job,*identity);keys=progress_keys(rows,job)
                    check.need(seen.issubset(keys),'progress-prefix-regressed')
                    if keys-seen:last=now
                    seen=keys
                no_progress=idle is not None and now-last>=idle
                code=process.poll()
                if code is not None or timed or no_progress or limited or disk_low:break
                time.sleep(.05)
    except BaseException as exc:error=type(exc).__name__
    finally:
        if process:
            if job is None:
                try:os.killpg(process.pid,signal.SIGKILL)
                except ProcessLookupError:pass
            elif process.poll() is None:process.kill();kill_requested=True
            code=process.wait()
        else:code=None
    return dict(returncode=code,timed_out=timed,no_progress=no_progress,output_limited=limited,disk_low=disk_low,error=error,kill_requested=kill_requested,elapsed_seconds=time.monotonic()-start)

def worker(args):
    parent=Path(args.directory);plan=read(parent/'plan.json');run_id=plan['run_id']
    root=Path(args.prepared) if args.prepared else build(parent,run_id)
    if not args.prepared:
        os.environ['NATEMU_M23_REPO']=str(REPO)
        os.execv(sys.executable,[sys.executable,'-B',str(root/'snapshot/run.py'),'--directory',str(parent),'--worker','--prepared',str(root),'--no-other-native-benchmarks'])
    packet=read(root/'build.json');assert packet['status']=='built' and packet['run_id']==run_id;verify(root,packet['files'])
    assert plan['jobs']==schedule(plan['cpus']);members=[]
    for job in plan['jobs']:
        name=f"{job['index']:03}-{job['mode']}";child=parent/name;child.mkdir()
        before=environment(child,'before')
        current=int(command(['sysctl','-n','hw.ncpu'],child,'cpu-count'))
        if current!=plan['cpus']:
            write(child/'unavailable.json',dict(reason='CPU count changed',expected=plan['cpus'],observed=current,job=job));raise ValueError('CPU count changed')
        argv=[str(root/'liveness'),str(child),str(job['n']),str(job['strategy']),str(job['qos']),str(job['h']),job['mode'],run_id,str(job['seed']),str(job['cpus'])]
        write(child/'invocation.json',dict(argv=argv,job=job,run_id=run_id,build_id=packet['build_id'],build_sha256=sha(root/'build.json')))
        os.environ['NATEMU_M23_CELL_ID']=str(job['index'])
        terminal=supervise(argv,child,30,CAP,idle=5,job=job,identity=(run_id,packet['build_id']))
        write(child/'process.json',terminal)
        after=environment(child,'after');assert before['hardware']==after['hardware'] and before['os']==after['os']
        assert not any(terminal[k] for k in ('output_limited','disk_low','error')),'supervision failure'
        rows,suffix=check.load(child,job,run_id,packet['build_id'])
        q=9 if job['qos'] else 33
        unavailable=[r for actor in rows.values() for r in actor if (r['phase']=='qos_set' and r['rc']!=0) or (r['phase']=='qos_get' and (r['rc']!=0 or r['a']!=q or r['b']!=0))]
        if unavailable:
            write(child/'unavailable.json',dict(reason='requested QoS unavailable',observations=unavailable,job=job));raise ValueError('QoS unavailable')
        result=check.validate(rows,job,terminal,suffix)
        # Kernel-written complete prefixes survive process death. Flush regular
        # files after reap; retain partial suffix bytes unchanged in their files.
        for path in child.glob('actor-*.jsonl'):
            with path.open('rb') as stream:os.fsync(stream.fileno())
        write(child/'check.json',result);verify(root,packet['files'])
        write(child/'manifest.json',dict(job=job,run_id=run_id,build_id=packet['build_id'],files=inventory(child)))
        members.append(dict(directory=name,sha256=sha(child/'manifest.json')))
    write(parent/'campaign.json',dict(schema='M23-L1-campaign-v1',run_id=run_id,build_id=packet['build_id'],members=members,timing='unqualified',evidence_approval=False))

def validate_campaign(directory):
    root=Path(directory);outer=read(root/'supervisor.json');plan=read(root/'plan.json')
    assert outer['schema']=='M23-L1-supervisor-v1' and outer['status']=='complete'
    assert outer['terminal']['returncode']==0 and not any(outer['terminal'][k] for k in ('timed_out','no_progress','output_limited','disk_low','error'))
    assert usage(root)<CAP and outer['terminal']['elapsed_seconds']<WALL
    actual=inventory(root);actual.pop('supervisor.json');assert actual==outer['files']
    assert plan['schema']=='M23-L1-plan-v1' and plan['run_id']==outer['run_id'] and plan['jobs']==schedule(plan['cpus'])
    assert plan['wall_seconds']==WALL and plan['output_limit']==CAP
    build_root=root/'producing-build';build=read(build_root/'build.json');verify(build_root,build['files'])
    assert build['status']=='built' and build['run_id']==plan['run_id']
    campaign=read(root/'campaign.json');assert campaign['schema']=='M23-L1-campaign-v1' and campaign['run_id']==plan['run_id'] and campaign['build_id']==build['build_id']
    assert len(campaign['members'])==279
    results=[]
    for job,member in zip(plan['jobs'],campaign['members']):
        name=f"{job['index']:03}-{job['mode']}";child=root/name
        assert member['directory']==name and outer['states'][name]=='complete' and sha(child/'manifest.json')==member['sha256']
        manifest=read(child/'manifest.json');assert manifest['job']==job and manifest['run_id']==plan['run_id'] and manifest['build_id']==build['build_id'];verify(child,manifest['files'])
        call=read(child/'invocation.json');assert call['job']==job and call['run_id']==plan['run_id'] and call['build_id']==build['build_id'] and call['build_sha256']==sha(build_root/'build.json')
        expected=[str(build_root/'liveness'),str(child),str(job['n']),str(job['strategy']),str(job['qos']),str(job['h']),job['mode'],plan['run_id'],str(job['seed']),str(job['cpus'])]
        assert call['argv']==expected
        terminal=read(child/'process.json');assert not any(terminal[k] for k in ('output_limited','disk_low','error'))
        rows,suffix=check.load(child,job,plan['run_id'],build['build_id']);result=check.validate(rows,job,terminal,suffix)
        assert result==read(child/'check.json');results.append(result)
    return results

def cli():
    parser=argparse.ArgumentParser();parser.add_argument('--directory',required=True);parser.add_argument('--no-other-native-benchmarks',action='store_true');parser.add_argument('--worker',action='store_true',help=argparse.SUPPRESS);parser.add_argument('--prepared',help=argparse.SUPPRESS);args=parser.parse_args()
    assert args.no_other_native_benchmarks
    raw=Path(os.environ['NATEMU_RAW_ROOT']).resolve(strict=True);root=Path(args.directory)
    assert raw.is_dir() and raw!=Path('/') and not raw.is_relative_to(REPO) and not REPO.is_relative_to(raw)
    assert root.is_absolute() and root.resolve()==root and root.is_relative_to(raw) and root!=raw and root.parent.is_dir()
    if args.worker:return worker(args)
    assert not args.prepared;root.mkdir(mode=0o700);assert shutil.disk_usage(root).free>=RESERVE
    start=time.monotonic()
    cpus=int(command(['sysctl','-n','hw.ncpu'],root,'cpu-count'))
    if not 1<=cpus<=64:
        write(root/'unavailable.json',dict(reason='CPU count outside declared 1..64',observed=cpus));raise ValueError('CPU count unavailable')
    jobs=schedule(cpus);run_id=uuid.uuid4().hex
    write(root/'plan.json',dict(schema='M23-L1-plan-v1',run_id=run_id,cpus=cpus,jobs=jobs,wall_seconds=WALL,output_limit=CAP))
    terminal=supervise([sys.executable,'-B',str(Path(__file__).resolve()),'--directory',str(root),'--worker','--no-other-native-benchmarks'],root,max(.001,WALL-(time.monotonic()-start)),CAP)
    states={f"{j['index']:03}-{j['mode']}":'complete' if (root/f"{j['index']:03}-{j['mode']}"/'manifest.json').exists() else 'interrupted' if (root/f"{j['index']:03}-{j['mode']}").exists() else 'not-run' for j in jobs}
    files=inventory(root);size=usage(root)
    okay=terminal['returncode']==0 and not any(terminal[k] for k in ('timed_out','no_progress','output_limited','disk_low','error')) and all(s=='complete' for s in states.values())
    receipt=dict(schema='M23-L1-supervisor-v1',run_id=run_id,terminal=terminal,states=states,files=files,status='complete' if okay else 'failed',bytes_before_receipt=size,evidence_approval=False)
    if time.monotonic()-start>=WALL or size+len(json.dumps(receipt).encode())>=CAP:okay=False;receipt['status']='failed'
    write(root/'supervisor.json',receipt)
    print('M23 structural campaign complete; independent findings required' if okay else 'M23 stopped; partial/not-run evidence retained')
    return 0 if okay else 1

if __name__=='__main__':
    try:sys.exit(cli())
    except Exception as exc:print('M23 refused/failed: '+type(exc).__name__,file=sys.stderr);sys.exit(2)
