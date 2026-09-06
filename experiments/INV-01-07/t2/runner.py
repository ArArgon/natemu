#!/usr/bin/env python3
"""Proposed M07-T2 fixed campaign. Reviews remain human prerequisites."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid

if not __debug__:raise RuntimeError('T2 validation requires assertions enabled')
SOURCE=Path(__file__).resolve().parent
REPO=Path(os.environ.get('NATEMU_T2_REPO',SOURCE.parents[2])).resolve()
MODES={'normal':1800,'dispatch-early':300,'rtt-zero':60,'cadence-duplicate':60}
WALL=2700
CAP=128*1024*1024

def sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
    return h.hexdigest()

def write(path,value):
    with Path(path).open('x') as f:json.dump(value,f,sort_keys=True,allow_nan=False)

def read(path):return json.loads(Path(path).read_text())

def files(root):return {str(p.relative_to(root)):sha(p) for p in sorted(root.rglob('*')) if p.is_file()}

def verify(root,record):
    for name,digest in record.items():
        assert not Path(name).is_absolute() and '..' not in Path(name).parts,'artifact path escape'
        p=root/name
        assert p.resolve()==p and p.is_file() and sha(p)==digest,'artifact mismatch'

def command(args,directory,name,seconds=60):
    with (directory/(name+'.stdout')).open('xb') as out,(directory/(name+'.stderr')).open('xb') as err:
        p=subprocess.run(args,stdout=out,stderr=err,timeout=seconds,check=False)
    assert p.returncode==0,name+' failed'
    return (directory/(name+'.stdout')).read_text().strip()

def environment(directory,tag):
    values={}
    for key,args in {'os':['sw_vers'],'hardware':['sysctl','hw.model','hw.ncpu','hw.memsize','hw.pagesize'],
                     'load':['uptime'],'power':['pmset','-g','batt'],'thermal':['pmset','-g','therm']}.items():
        values[key]=command(args,directory,tag+'-'+key,10)
    return values

def build(parent,run_id):
    directory=parent/'producing-build';directory.mkdir();snapshot=directory/'snapshot';snapshot.mkdir()
    outcome='failed';calls=[]
    try:
        for p in SOURCE.iterdir():
            if p.suffix in ('.py','.c','.swift'):shutil.copy2(p,snapshot/p.name)
        shutil.copy2(REPO/'experiments/INV-01-07/c1/clock_evidence.py',snapshot/'clock_evidence.py')
        sdk=command(['xcrun','--sdk','macosx','--show-sdk-path'],directory,'sdk',10)
        command(['xcrun','--sdk','macosx','--show-sdk-version'],directory,'sdk-version',10)
        command(['xcrun','clang','--version'],directory,'clang-version',10)
        command(['xcrun','swiftc','--version'],directory,'swift-version',10)
        calls=[['xcrun','clang','-std=c11','-O2','-g','-fblocks','-Wall','-Wextra','-Werror','-pthread','-isysroot',sdk,'-DT2_REPO="'+str(REPO)+'"','-c',str(snapshot/'producer.c'),'-o',str(directory/'producer.o')],
               ['xcrun','swiftc','-O','-parse-as-library','-sdk',sdk,'-module-cache-path',str(directory/'swift-cache'),'-c',str(snapshot/'deadline.swift'),'-o',str(directory/'deadline.o')],
               ['xcrun','swiftc',str(directory/'producer.o'),str(directory/'deadline.o'),'-o',str(directory/'timers')],
               ['codesign','--force','--sign','-','--timestamp=none',str(directory/'timers')],
               ['xcrun','otool','-tvV',str(directory/'timers')]]
        for i,args in enumerate(calls):command(args,directory,'build-'+str(i))
        command([sys.executable,'-B',str(snapshot/'test_check.py')],directory,'synthetic',60)
        command([sys.executable,'-B',str(snapshot/'test_runner.py')],directory,'supervision-synthetic',60)
        command([sys.executable,'-B',str(snapshot/'test_signal.py'),'--directory',str(directory/'signal-synthetic')],directory,'signal-synthetic',60)
        command(['codesign','-d','--entitlements',':-',str(directory/'timers')],directory,'entitlements',10)
        # Module cache is a transient compiler cache, not retained evidence.
        # Retain explicit sources, selected public declarations and build logs.
        shutil.rmtree(directory/'swift-cache')
        headers=directory/'sdk-headers';headers.mkdir()
        for name in ('dispatch/time.h','dispatch/source.h','mach/mach_time.h','pthread/qos.h'):
            shutil.copy2(Path(sdk)/'usr/include'/name,headers/name.replace('/','-'))
        interface=Path(sdk)/'usr/lib/swift/Dispatch.swiftmodule/arm64e-apple-macos.swiftinterface'
        shutil.copy2(interface,headers/'Dispatch.swiftinterface')
        outcome='built'
    finally:
        write(directory/'build.json',dict(schema='M07-T2-build-v1',run_id=run_id,outcome=outcome,commands=calls,files=files(directory)))
    return directory

def worker(args):
    parent=Path(args.directory);schedule=read(parent/'schedule.json');run_id=schedule['run_id']
    directory=Path(args.prepared) if args.prepared else build(parent,run_id)
    if not args.prepared:
        os.environ['NATEMU_T2_REPO']=str(REPO)
        os.execv(sys.executable,[sys.executable,'-B',str(directory/'snapshot/runner.py'),'--directory',str(parent),'--clock',str(args.clock),'--worker','--prepared',str(directory),'--no-other-native-benchmarks'])
    packet=read(directory/'build.json');assert packet['outcome']=='built' and packet['run_id']==run_id
    verify(directory,packet['files'])
    sys.path.insert(0,str(directory/'snapshot'));import check,clock_evidence
    members={}
    for mode,limit in MODES.items():
        child=parent/mode;child.mkdir();before=environment(child,'before');write(child/'environment-before.json',before)
        env=dict(os.environ,NATEMU_T2_RUN_ID=run_id)
        argv=[str(directory/'timers'),str(child/'result.json'),mode]
        write(child/'invocation.json',dict(argv=argv,run_id=run_id,build_sha256=sha(directory/'build.json'),clock=str(args.clock)))
        timed=False
        with (child/'stdout').open('xb') as out,(child/'stderr').open('xb') as err:
            p=subprocess.Popen(argv,stdout=out,stderr=err,env=env)
            try:rc=p.wait(timeout=limit)
            except subprocess.TimeoutExpired:timed=True;p.kill();rc=p.wait()
            except BaseException:p.kill();p.wait();raise
        write(child/'process.json',dict(returncode=rc,timeout=timed,limit_seconds=limit))
        after=environment(child,'after');write(child/'environment-after.json',after)
        assert before['os']==after['os'] and before['hardware']==after['hardware'],'host identity changed'
        assert not timed and rc==0,'native child failure'
        data=check.load(child/'result.json');assert data['run_id']==run_id and data['mode']==mode
        context=dict(host_id=socket.gethostname(),host_model=subprocess.check_output(['sysctl','-n','hw.model'],text=True,timeout=10).strip(),
                     os_build=subprocess.check_output(['sw_vers','-buildVersion'],text=True,timeout=10).strip(),architecture=os.uname().machine,
                     timebase=dict(zip(('numer','denom'),data['timebase'])))
        clock=clock_evidence.validate(args.clock,context);assert clock['status']=='context-matched','clock context invalid'
        if mode=='normal':
            result=dict(status='STRUCTURAL_PASS',findings=check.validate(data),clock=clock,clock_context=context,evidence_approval=False)
        else:
            expected={'dispatch-early':'dispatch-early','rtt-zero':'rtt-positive','cadence-duplicate':'cadence-deadline'}[mode]
            try:check.validate(data)
            except check.Invalid as exc:assert str(exc)==expected,'wrong negative oracle'
            else:raise ValueError('injection not detected')
            result=dict(status='EXPECTED_CONTROL_FAILURE',oracle=expected,evidence_approval=False)
        # Serialize completely before retaining findings; no partial prefix.
        serialized=json.dumps(result,default=check.encode,sort_keys=True,allow_nan=False)
        with (child/'check.json').open('x') as out:out.write(serialized)
        verify(directory,packet['files']);write(child/'manifest.json',dict(run_id=run_id,files=files(child)))
        members[mode]=sha(child/'manifest.json')
    write(parent/'campaign.json',dict(schema='M07-T2-campaign-v1',run_id=run_id,members=members,clock=str(args.clock),evidence_approval=False))
    return 0

def supervise(argv,parent,wall,cap):
    start=time.monotonic();timed=limited=False;code=None;child=None;failure=None
    try:
        with (parent/'stdout').open('xb') as out,(parent/'stderr').open('xb') as err:
            child=subprocess.Popen(argv,stdout=out,stderr=err,start_new_session=True)
            while True:
                timed=time.monotonic()-start>=wall;limited=sum(x.stat().st_size for x in parent.rglob('*') if x.is_file())>=cap
                code=child.poll()
                if code is not None or timed or limited:break
                time.sleep(.05)
            try:os.killpg(child.pid,signal.SIGKILL)
            except ProcessLookupError:pass
            code=child.wait()
    except BaseException as exc:
        failure=type(exc).__name__
        if child:
            try:os.killpg(child.pid,signal.SIGKILL)
            except ProcessLookupError:pass
            code=child.wait()
    return dict(returncode=code,timed_out=timed,output_limited=limited,failure=failure),start

def validate_campaign(parent):
    """Retained offline structural derivation; this never grants review approval."""
    parent=Path(parent);receipt=read(parent/'supervisor.json');schedule=read(parent/'schedule.json')
    assert receipt['schema']=='M07-T2-supervisor-v1' and receipt['status']=='completed'
    assert receipt['returncode']==0 and not any(receipt[k] for k in ('timed_out','output_limited','failure'))
    assert receipt['elapsed_seconds']<WALL and sum(x.stat().st_size for x in parent.rglob('*') if x.is_file())<CAP
    assert schedule==dict(schema='M07-T2-schedule-v1',run_id=receipt['run_id'],modes=MODES,wall_seconds=WALL,output_limit_bytes=CAP)
    assert receipt['modes']==dict.fromkeys(MODES,'completed')
    actual=files(parent);actual.pop('supervisor.json');assert actual==receipt['files']
    campaign=read(parent/'campaign.json');assert campaign['schema']=='M07-T2-campaign-v1' and campaign['run_id']==receipt['run_id']
    assert set(campaign['members'])==set(MODES)
    build_dir=parent/'producing-build';build=read(build_dir/'build.json')
    assert build['outcome']=='built' and build['run_id']==receipt['run_id'];verify(build_dir,build['files'])
    import check,clock_evidence
    results={}
    for mode in MODES:
        child=parent/mode;manifest=read(child/'manifest.json')
        assert sha(child/'manifest.json')==campaign['members'][mode] and manifest['run_id']==receipt['run_id'];verify(child,manifest['files'])
        process=read(child/'process.json');assert process==dict(returncode=0,timeout=False,limit_seconds=MODES[mode])
        invocation=read(child/'invocation.json');assert invocation['run_id']==receipt['run_id'] and invocation['build_sha256']==sha(build_dir/'build.json')
        assert invocation['argv']==[str(build_dir/'timers'),str(child/'result.json'),mode] and invocation['clock']==campaign['clock']
        raw=check.load(child/'result.json');assert raw['run_id']==receipt['run_id'] and raw['mode']==mode
        result=read(child/'check.json')
        if mode=='normal':
            assert result['status']=='STRUCTURAL_PASS' and result['findings']==json.loads(json.dumps(check.validate(raw),default=check.encode))
            assert result['clock']==clock_evidence.validate(campaign['clock'],result['clock_context']) and result['clock']['status']=='context-matched'
        else:
            expected={'dispatch-early':'dispatch-early','rtt-zero':'rtt-positive','cadence-duplicate':'cadence-deadline'}[mode]
            try:check.validate(raw)
            except check.Invalid as exc:assert str(exc)==expected
            else:raise ValueError('undetected control')
            assert result['status']=='EXPECTED_CONTROL_FAILURE' and result['oracle']==expected
        results[mode]=result
    return results

def cli():
    p=argparse.ArgumentParser();p.add_argument('--directory',required=True);p.add_argument('--clock',required=True);p.add_argument('--no-other-native-benchmarks',action='store_true');p.add_argument('--worker',action='store_true',help=argparse.SUPPRESS);p.add_argument('--prepared',help=argparse.SUPPRESS);args=p.parse_args()
    assert args.no_other_native_benchmarks,'no-overlap assertion required'
    root=Path(os.environ['NATEMU_RAW_ROOT']).resolve(strict=True);parent=Path(args.directory)
    assert root.is_dir() and root!=Path('/') and not root.is_relative_to(REPO) and not REPO.is_relative_to(root)
    assert parent.is_absolute() and parent.resolve()==parent and parent.is_relative_to(root) and parent!=root and parent.parent.is_dir()
    if args.worker:return worker(args)
    assert not args.prepared;parent.mkdir(mode=0o700);run_id=uuid.uuid4().hex
    write(parent/'schedule.json',dict(schema='M07-T2-schedule-v1',run_id=run_id,modes=MODES,wall_seconds=WALL,output_limit_bytes=CAP))
    argv=[sys.executable,'-B',str(Path(__file__).resolve()),'--directory',str(parent),'--clock',str(args.clock),'--worker','--no-other-native-benchmarks']
    terminal,start=supervise(argv,parent,WALL,CAP)
    states={m:'completed' if (parent/m/'manifest.json').exists() else 'interrupted' if (parent/m).exists() else 'not-run' for m in MODES}
    inventory=files(parent);elapsed=time.monotonic()-start
    size=sum(x.stat().st_size for x in parent.rglob('*') if x.is_file())
    okay=terminal['returncode']==0 and not any(terminal[k] for k in ('timed_out','output_limited','failure')) and elapsed<WALL and size<CAP and all(x=='completed' for x in states.values())
    receipt=dict(schema='M07-T2-supervisor-v1',run_id=run_id,**terminal,
          elapsed_seconds=elapsed,output_bytes_before_receipt=size,modes=states,files=inventory,status='completed' if okay else 'failed',evidence_approval=False)
    if size+len(json.dumps(receipt,sort_keys=True).encode())>=CAP or time.monotonic()-start>=WALL:
        okay=False;receipt['status']='failed'
    write(parent/'supervisor.json',receipt)
    print('T2 campaign structurally completed; independent review required' if okay else 'T2 failed; partial artifacts retained')
    return 0 if okay else 1

if __name__=='__main__':
    try:sys.exit(cli())
    except Exception as exc:print('T2 refused/failed: '+type(exc).__name__,file=sys.stderr);sys.exit(2)
