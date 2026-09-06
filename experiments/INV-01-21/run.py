#!/usr/bin/env python3
"""M21-1 build-only preparation and separately invoked bounded native campaign."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import selectors
import shutil
import signal
import subprocess
import sys
import time
import uuid
import check

HERE=Path(__file__).resolve().parent
REPO=Path(os.environ.get('M21_REPO',HERE.parents[1])).resolve()
MIB=1024**2

def sha(p):
    h=hashlib.sha256()
    with Path(p).open('rb') as f:
        for b in iter(lambda:f.read(MIB),b''):h.update(b)
    return h.hexdigest()

def write(path,value):
    with Path(path).open('x') as f:json.dump(value,f,sort_keys=True,allow_nan=False);f.write('\n')

def read(p):return check.decode(Path(p).read_bytes())

def inventory(root):return {str(p.relative_to(root)):sha(p) for p in sorted(root.rglob('*')) if p.is_file()}

def verify(root,files):
    for name,digest in files.items():
        p=root/name
        check.need(not Path(name).is_absolute() and '..' not in Path(name).parts and p.resolve()==p and sha(p)==digest,'retained-artifact-mismatch')

def destination(path):
    raw=Path(os.environ['NATEMU_RAW_ROOT']).resolve(strict=True);dest=Path(path)
    check.need((REPO/'docs/research/README.md').is_file(),'original-repository-context (set M21_REPO for retained sources)')
    check.need(raw!=Path('/') and not raw.is_relative_to(REPO) and not REPO.is_relative_to(raw),'external-raw-root')
    check.need(dest.is_absolute() and dest.resolve()==dest and dest.is_relative_to(raw) and dest!=raw and dest.parent.is_dir(),'destination')
    check.need(not dest.exists(),'overwrite');return dest

def command(argv,root,name):
    with (root/(name+'.stdout')).open('xb') as out,(root/(name+'.stderr')).open('xb') as err:
        p=subprocess.run(argv,stdout=out,stderr=err,timeout=120)
    check.need(p.returncode==0,'command-failed:'+name)
    return (root/(name+'.stdout')).read_text().strip()

def build(output):
    root=destination(output);root.mkdir();snapshot=root/'snapshot';snapshot.mkdir();identity=uuid.uuid4().hex;commands=[];status='failed'
    try:
        for p in HERE.iterdir():
            if p.suffix in ('.py','.h','.m','.c','.md'):shutil.copy2(p,snapshot/p.name)
        sdk=command(['xcrun','--sdk','macosx','--show-sdk-path'],root,'sdk')
        command(['xcrun','--sdk','macosx','--show-sdk-version'],root,'sdk-version');command(['xcrun','clang','--version'],root,'compiler')
        commands=[['xcrun','clang','-fobjc-arc','-std=gnu11','-O2','-g','-Wall','-Wextra','-Werror','-pthread','-isysroot',sdk,'-DM21_BUILD="'+identity+'"','-framework','Foundation','-framework','AppKit',str(snapshot/'producer.m'),'-o',str(root/'lifecycle')],
                  ['codesign','--force','--sign','-','--timestamp=none',str(root/'lifecycle')],
                  ['codesign','-d','--entitlements',':-',str(root/'lifecycle')],
                  ['xcrun','otool','-tvV',str(root/'lifecycle')],
                  ['xcrun','clang','-std=c11','-O2','-Wall','-Wextra','-Werror','-pthread',str(snapshot/'core_test.c'),'-o',str(root/'core_test')],
                  [str(root/'core_test')],
                  ['xcrun','clang','-fobjc-arc','-std=gnu11','-O2','-Wall','-Wextra','-Werror','-pthread','-DM21_BUILD="'+('0'*32)+'"','-framework','Foundation','-framework','AppKit',str(snapshot/'serializer_test.m'),'-o',str(root/'serializer_test')],
                  [sys.executable,'-B',str(snapshot/'tests.py')],
                  [sys.executable,'-B',str(snapshot/'test_returns.py')]]
        for i,argv in enumerate(commands):command(argv,root,'build-'+str(i))
        status='built'
    finally:write(root/'build.json',dict(schema='M21-1-build-v1',build_id=identity,original_repo=str(REPO),status=status,commands=commands,files=inventory(root),native_executed=False))
    return root

def clocks():return time.clock_gettime(time.CLOCK_UPTIME_RAW),time.clock_gettime(time.CLOCK_MONOTONIC)

def sizes(root):return [p.stat().st_size for p in root.rglob('*') if p.is_file()]

def retained_build(root):
    root=Path(root).resolve(strict=True);packet=read(root/'build.json')
    check.need(packet['schema']=='M21-1-build-v1' and packet['status']=='built','build-status');verify(root,packet['files'])
    check.need(packet['original_repo']==str(REPO),'original-repository-mismatch')
    for name in ('run.py','check.py'):check.need(sha(HERE/name)==sha(root/'snapshot'/name),'executing-consumer-mismatch')
    return packet

def environment(root,label):
    commands={'os':['sw_vers'],'model':['sysctl','-n','hw.model'],'topology':['sysctl','hw.ncpu','hw.nperflevels'],
              'power':['pmset','-g','batt'],'thermal':['pmset','-g','therm'],'load':['uptime']}
    data={k:command(v,root,label+'-'+k) for k,v in commands.items()}
    check.need(data['model']=='Mac16,10' and '26.6.2' in data['os'] and '25G83' in data['os'],'host-drift')
    for level in range(4):
        argv=['sysctl','hw.perflevel'+str(level)+'.physicalcpu','hw.perflevel'+str(level)+'.logicalcpu']
        try:data['perflevel'+str(level)]=command(argv,root,label+'-perflevel'+str(level))
        except ValueError:data['perflevel'+str(level)]='unavailable (retained exit output)'
    data['launch']=dict(pid=os.getpid(),parent=os.getppid(),tty=os.isatty(0),placement='uncontrolled',guest='none',signing='plain local ad-hoc',framework_actors='uncontrolled',app_nap='unobserved')
    write(root/(label+'.json'),data);return data

def marker(path,slot,kind,evidence):
    """Operator writes immutable input, parent later assigns authoritative receipt.
    This command performs no induction and reads no experimental clock.
    """
    root=Path(path).resolve(strict=True);check.need(not (root/'manifest.json').exists(),'campaign-already-terminal');need=read(root/'schedule.json')['jobs'];check.need(0<=slot<len(need),'slot')
    data=read(evidence) if evidence else {};check.need(type(data) is dict,'operator-object')
    out=root/'operator'/uuid.uuid4().hex
    write(out,dict(slot=slot,kind=kind,evidence=data));print(out)

class Recorder:
    def __init__(self,path):self.file=Path(path).open('xb');self.events=[]
    def add(self,event,awake,**fields):
        row=dict(event=event,awake=awake,ordinal=len(self.events),**fields)
        line=(json.dumps(row,separators=(',',':'),allow_nan=False)+'\n').encode()
        check.need(len(self.events)<4096 and len(line)<=256,'controller-record-capacity')
        self.file.write(line);self.file.flush();self.events.append(row);return row
    def close(self):self.file.close()

def operator_inputs(root,slot,seen):
    for p in sorted((root/'operator').iterdir()):
        if p.name in seen:continue
        check.need(p.stat().st_size<=128*1024,'operator-input-size');data=read(p)
        if data['slot']!=slot:continue
        seen.add(p.name);yield p,data

def evidence(root,child,path,data):
    """Retain actual external materials; never manufacture induction success."""
    value=data['evidence'];check.need(type(value) is dict,'evidence')
    files=value.get('files',{});check.need(type(files) is dict and len(files)<=8,'evidence-files')
    saved={}
    for label,name in files.items():
        check.need(label.replace('-','').replace('_','').isalnum(),'evidence-label')
        source=Path(name).resolve(strict=True);check.need(source.is_file() and source.stat().st_size<256*MIB,'evidence-file')
        target=child/(path.name+'-'+label);shutil.copyfile(source,target);saved[label]=dict(file=target.name,sha256=sha(target))
    record=dict(operator_input=str(path.relative_to(root)),input_sha256=sha(path),value=value,retained=saved)
    destination=child/(path.name+'.evidence.json');write(destination,record)
    return destination.name,value,saved

def supervise(argv,child,job,campaign,campaign_start,seen,clock=clocks,resource=None,slot_start=None):
    start_awake,start_cont=slot_start or clock();launch_awake,_=clock();rec=Recorder(child/'receipts.jsonl');p=None;armed=None;phase=None;stop_at=None
    killed=False;reason=None;timeout=False;buffer=b'';notices=0;selector=selectors.DefaultSelector()
    resource=resource or (lambda:(sizes(child),sum(sizes(child)),sum(sizes(campaign)),shutil.disk_usage(campaign).free))
    def phase_service(now,kind=None,**fields):
        if phase is None:return
        if kind is None:phase.advance(now)
        else:phase.receipt(kind,now,**fields)
        for name,when in phase.take_announcements():rec.add('phase-'+name,when)
    try:
        with (child/'stdout.jsonl').open('xb') as out,(child/'stderr').open('xb') as err:
            p=subprocess.Popen(argv,cwd=child,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=err,start_new_session=True)
            os.set_blocking(p.stdout.fileno(),False);selector.register(p.stdout,selectors.EVENT_READ)
            rec.add('launch',start_awake)
            while True:
                now,wall=clock();files,slot_bytes,total,free=resource()
                reason=check.budgets(job,now-start_awake,wall-start_cont,wall-campaign_start,files,slot_bytes,total,free)
                if reason:timeout=reason.endswith('timeout');break
                if armed is None and now-launch_awake>=60:reason='warmup-timeout';timeout=True;break
                if stop_at is not None and now-stop_at>=60:reason='drain-timeout';timeout=True;break
                if p.poll() is not None:
                    # Drain the actual remaining pipe prefix after exit.
                    chunk=p.stdout.read() or b'';out.write(chunk);buffer+=chunk
                else:
                    chunk=b''
                    if selector.select(.02):chunk=os.read(p.stdout.fileno(),65536);out.write(chunk);out.flush();buffer+=chunk
                now,wall=clock();files,slot_bytes,total,free=resource()
                reason=check.budgets(job,now-start_awake,wall-start_cont,wall-campaign_start,files,slot_bytes,total,free)
                if reason:timeout=reason.endswith('timeout');break
                check.need(len(buffer)<=65536,'ipc-line-capacity')
                while b'\n' in buffer:
                    now,wall=clock() # actual parent receipt, after any selector wait
                    phase_service(now)
                    line,buffer=buffer.split(b'\n',1);msg=check.decode(line);kind=msg.pop('event');rec.add(kind,now,**msg)
                    if kind=='armed':
                        check.need(armed is None,'duplicate-armed');armed=now
                        if job['arm']=='T1':phase=check.Phases(armed,job['window']);phase_service(now)
                        if job['arm'] in ('T5','C1'):
                            rec.add('demote-request' if job['arm']=='T5' else 'activity-request',now)
                            p.stdin.write(b'demote\n' if job['arm']=='T5' else b'activity\n');p.stdin.flush()
                    if phase and kind=='notification':phase_service(now,kind,source=msg['source'])
                    if kind=='reversal' and stop_at is None:
                        stop_at=now # producer already entered orderly shutdown
                    if kind=='admission-closed' and stop_at is None:stop_at=now
                if p.poll() is not None:break
                for path,data in operator_inputs(campaign,job['index'],seen):
                    kind=data['kind'];ref,value,saved=evidence(campaign,child,path,data);now,wall=clock()
                    files,slot_bytes,total,free=resource();reason=check.budgets(job,now-start_awake,wall-start_cont,wall-campaign_start,files,slot_bytes,total,free)
                    if reason:timeout=reason.endswith('timeout');break
                    phase_service(now)
                    if kind=='induction-start':
                        check.need(phase is not None and 'assertions' in saved and value.get('method') in ('sudo pmset sleepnow','Apple-menu Sleep'),'induction-evidence')
                        phase_service(now,kind);rec.add(kind,now,evidence=ref)
                    elif kind=='wake-confirmed':
                        check.need(phase is not None and {'independent','pmset-log'}<=set(saved),'wake-evidence')
                        confirmed=value.get('entry') is True and value.get('wake') is True and value.get('records_agree') is True
                        associated=value.get('associated') is True;phase_service(now,kind,confirmed=confirmed,associated=associated)
                        rec.add(kind,now,confirmed=confirmed,associated=associated,evidence=ref)
                    elif kind in ('transition-confirmed','unintended-transition'):
                        check.need(bool(saved),'external-transition-evidence');confirmed=value.get('confirmed') is True;associated=value.get('associated') is True
                        if job['arm'] in ('T3a','T3b'):
                            check.need({'before','after'}<=set(saved) and value.get('route')=='desktop Energy settings','power-induction-route')
                            check.need((value.get('before_state'),value.get('after_state'))==((False,True) if job['arm']=='T3a' else (True,False)),'power-induction-direction')
                        rec.add(kind,now,confirmed=confirmed,associated=associated,evidence=ref)
                    else:raise ValueError('unexpected-active-operator-input')
                if reason:break
                now,wall=clock()
                phase_service(now)
                normal_end=armed is not None and now-armed>=job['window']
                pre_invalid=phase is not None and phase.phase=='pre-invalid'
                if (normal_end or pre_invalid) and stop_at is None:
                    rec.add('normal-stop' if normal_end else 'pre-control-stop',now);p.stdin.write(b'stop\n');p.stdin.flush();stop_at=now
    except BaseException as exc:reason='supervisor-error:'+type(exc).__name__+':'+str(exc)
    finally:
        if p:
            if p.poll() is None:
                killed=True
                try:os.killpg(p.pid,signal.SIGKILL)
                except ProcessLookupError:pass
            code=p.wait();p.stdin.close();p.stdout.close()
        else:code=None
        selector.close();end_awake,end_cont=clock()
        # Parent keeps and hashes incomplete serialized bytes, never repairs them.
        terminal=dict(returncode=code,killed=killed,timeout=timeout,reason=reason,awake_elapsed=end_awake-start_awake,
                      continuous_elapsed=end_cont-start_cont,missing_in_memory_trace=killed or not any(e['event']=='complete' for e in rec.events),ipc_partial_bytes=len(buffer))
        try:rec.add('exit',end_awake,code=code,killed=killed)
        finally:rec.close()
        write(child/'terminal.json',terminal)
    return terminal,rec.events

def campaign(output,prepared):
    root=destination(output);root.mkdir();(root/'operator').mkdir();identity=uuid.uuid4().hex
    first_awake,first_cont=clocks();write(root/'preparation.json',dict(run_id=identity,awake=first_awake,continuous=first_cont,ceiling=36*3600,shared_allowance=7200))
    jobs=check.schedule();write(root/'schedule.json',dict(schema='M21-1-schedule-v1',run_id=identity,jobs=jobs));results=[dict(index=j['index'],arm=j['arm'],status='not-run',reason='unattempted') for j in jobs]
    seen=set();fatal=None;packet=None;shared_used=0
    try:
        packet=retained_build(Path(prepared));shutil.copytree(prepared,root/'producing-build');build_root=root/'producing-build';retained_build(build_root)
        check.need((build_root/'sdk-version.stdout').read_text().strip()=='26.4','SDK-drift')
        write(root/'inputs.json',dict(build_sha256=sha(build_root/'build.json'),build_id=packet['build_id'],budgets=dict(file=256*MIB,slot=512*MIB,campaign=8*1024**3,reserve=2*1024**3),operator_protocol='immutable files with parent receipt order'))
        prep_end_awake,_=clocks();shared_used=prep_end_awake-first_awake;check.need(shared_used<7200,'shared-preparation-budget')
        for job in jobs:
            now,wall=clocks();check.need(wall-first_cont<36*3600,'campaign-time')
            # Operator-dependent slots wait for an explicit original-slot decision.
            decision=None
            if job['arm'] in ('T1','T3a','T3b','T4'):
                print('M21 awaiting ready/unavailable for original slot',job['index'],job['arm'],flush=True)
                while decision is None:
                    now,wall=clocks();sz=sizes(root)
                    check.need(wall-first_cont<36*3600 and sum(sz)<8*1024**3 and max(sz,default=0)<256*MIB and shutil.disk_usage(root).free>=2*1024**3,'campaign-wait-budget')
                    for p,data in operator_inputs(root,job['index'],seen):
                        check.need(data['kind'] in ('ready','unavailable'),'admission-input');decision=(p,data)
                    if decision is None:time.sleep(.1)
                p,data=decision
                if data['kind']=='unavailable':results[job['index']]=dict(index=job['index'],arm=job['arm'],status='not-measured',reason='operator-unavailable',input=p.name);continue
            slot_start=clocks();child=root/f"{job['index']:03d}";child.mkdir()
            if decision is not None:
                ref,_,_=evidence(root,child,*decision);write(child/'admission.json',dict(operator_evidence=ref,slot=job['index']))
            before=environment(child,'before');retained_build(build_root)
            now,wall=clocks();sz=sizes(child)
            admission=check.budgets(job,now-slot_start[0],wall-slot_start[1],wall-first_cont,sz,sum(sz),sum(sizes(root)),shutil.disk_usage(root).free)
            check.need(admission is None,'launch-budget:'+str(admission))
            argv=[str(build_root/'lifecycle'),str(job['n']),job['arm'],str(job['seed']),identity]
            write(child/'invocation.json',dict(job=job,argv=argv,run_id=identity,build_id=packet['build_id'],binary_sha256=sha(build_root/'lifecycle')))
            terminal,events=supervise(argv,child,job,root,first_cont,seen,slot_start=slot_start)
            result=check.classify(child,job,identity,packet['build_id'],terminal,events)
            if terminal.get('reason') and not terminal.get('timeout'):result['fatal']=True
            after=environment(child,'after');check.need(before['model']==after['model'] and before['os']==after['os'],'host-drift');retained_build(build_root)
            now,wall=clocks();sz=sizes(child);post_budget=check.budgets(job,now-slot_start[0],wall-slot_start[1],wall-first_cont,sz,sum(sz),sum(sizes(root)),shutil.disk_usage(root).free)
            if post_budget:result.update(status='instrument-invalid',reason='post-check-'+post_budget,fatal=True)
            write(child/'check.json',result);write(child/'manifest.json',dict(run_id=identity,job=job,files=inventory(child)))
            results[job['index']]=dict(index=job['index'],**result);fatal=check.stop_reason(results)
            if fatal:break
    except BaseException as exc:fatal=type(exc).__name__+':'+str(exc)
    finally:
        final_start,_=clocks()
        for row in results:
            if row['status']=='not-run' and (root/f"{row['index']:03d}").exists():row.update(status='instrument-invalid',reason='interrupted-preparation-or-collection')
        # Hashing/finalization is shared work, inside the explicit shared and
        # campaign ceilings. The terminal manifest records measured overshoot.
        final_files=inventory(root);final_now,final_wall=clocks();sz=sizes(root)
        if shared_used+final_now-final_start>=7200:fatal=fatal or 'shared-finalization-budget'
        if final_wall-first_cont>=36*3600:fatal=fatal or 'campaign-finalization-budget'
        if sum(sz)>=8*1024**3 or max(sz,default=0)>=256*MIB or shutil.disk_usage(root).free<2*1024**3:fatal=fatal or 'finalization-resource-budget'
        write(root/'results.json',dict(run_id=identity,results=results,stop=fatal))
        final_files['results.json']=sha(root/'results.json')
        write(root/'manifest.json',dict(schema='M21-1-campaign-v1',run_id=identity,files=final_files,completed_question=False,timing='unqualified',
                                       operational_continuous_elapsed=final_wall-first_cont,shared_awake_elapsed=shared_used+final_now-final_start,payload_bytes_before_terminal=sum(sz)))
    print(root);return 1 if fatal else 0

def verify_campaign(directory):
    root=Path(directory).resolve(strict=True);manifest=read(root/'manifest.json')
    check.need(manifest['schema']=='M21-1-campaign-v1','campaign-schema')
    files=inventory(root);files.pop('manifest.json');check.need(files==manifest['files'],'campaign-artifact-set')
    plan=read(root/'schedule.json');check.need(plan['jobs']==check.schedule() and plan['run_id']==manifest['run_id'],'schedule-identity')
    states=read(root/'results.json');check.need(states['run_id']==plan['run_id'] and len(states['results'])==96,'denominator')
    build=retained_build(root/'producing-build');fresh=[]
    for job,row in zip(plan['jobs'],states['results']):
        check.need(row['index']==job['index'] and row['arm']==job['arm'],'slot-order')
        child=root/f"{job['index']:03d}"
        if not (child/'terminal.json').exists():continue
        inv=read(child/'invocation.json');check.need(inv['job']==job and inv['run_id']==plan['run_id'] and inv['build_id']==build['build_id'] and inv['binary_sha256']==sha(root/'producing-build/lifecycle'),'child-identity')
        events=[check.decode(line) for line in (child/'receipts.jsonl').read_bytes().splitlines()]
        result=check.classify(child,job,plan['run_id'],build['build_id'],read(child/'terminal.json'),events);fresh.append(dict(index=job['index'],**result))
    return dict(run_id=plan['run_id'],slots=96,fresh_children=fresh,timing='unqualified',completed_question=False)

def main():
    p=argparse.ArgumentParser();s=p.add_subparsers(dest='command',required=True)
    b=s.add_parser('build');b.add_argument('--output',required=True)
    c=s.add_parser('campaign');c.add_argument('--output',required=True);c.add_argument('--build',required=True)
    m=s.add_parser('mark');m.add_argument('--campaign',required=True);m.add_argument('--slot',type=int,required=True);m.add_argument('--kind',required=True);m.add_argument('--evidence')
    v=s.add_parser('verify');v.add_argument('--campaign',required=True)
    a=p.parse_args()
    if a.command=='build':print(build(a.output));return 0
    if a.command=='mark':marker(a.campaign,a.slot,a.kind,a.evidence);return 0
    if a.command=='verify':print(json.dumps(verify_campaign(a.campaign),sort_keys=True));return 0
    return campaign(a.output,a.build)

if __name__=='__main__':sys.exit(main())
