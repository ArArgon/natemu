"""Fabricated actor traces and dummy processes only; never invoke liveness."""
import copy
import errno
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
import check
import run

RUN='1'*32;BUILD='2'*32

def fixture(n=2,h=0,mode='normal'):
    job=dict(index=0,n=n,h=h,cpus=2,strategy=1 if mode=='missing-yield' else 2,qos=1,seed=23001,mode=mode)
    if mode!='normal':job['h']=h=2
    rows={i:[] for i in range(n+h+1)}
    def add(actor,e,phase,a=0,b=0,c=0,rc=0):
        r=dict(run_id=RUN,build_id=BUILD,cell=0,actor=actor,seq=len(rows[actor]),epoch=e,phase=phase,rc=rc,a=a,b=b,c=c,tick=len(rows[actor])+1)
        rows[actor].append(r);return r
    for i in rows:
        role=0 if i==0 else 1 if i<=n else 2;index=0 if i==0 else i-1 if i<=n else i-n-1
        add(i,-1,'start',role,index);add(i,-1,'qos_set');add(i,-1,'qos_get',9,0,9);add(i,-1,'cpu',0,2)
    add(0,-1,'config',n,h,job['strategy']);add(0,-1,'timebase',125,3)
    for i in range(1,n+h+1):add(0,-1,'create',i)
    for e in range(20):
        add(0,e,'held_observed',e,((1<<n)-1)&~1);add(0,e,'release_publish',1);add(0,e,'owner_done_observed',1)
        for phase in ('target_acquired','held_publish','wait_call','wait_return','release_receipt','data_publish','target_unlocked','owner_done_publish'):
            add(1,e,phase,e if phase=='data_publish' else 1 if phase in ('held_publish','release_receipt','owner_done_publish') else 0)
        for i in range(2,n+1):
            add(i,e,'held_observed',e,1);add(i,e,'ready_publish',1);add(i,e,'cpu',0,2);add(i,e,'attempt',job['strategy'])
            if job['strategy']==1:add(i,e,'busy_first',1,rc=errno.EBUSY)
            add(i,e,'target_acquired');add(i,e,'attempt_summary',1 if job['strategy']==1 else 0,1 if job['strategy']==1 else 0)
            add(i,e,'data_read',e,e);add(i,e,'update',e+1);add(i,e,'target_unlocked');add(i,e,'ack_publish',e);add(0,e,'ack_observed',i-1,e)
        add(0,e,'epoch_done',n)
    for i in range(n+1,n+h+1):add(i,-1,'helper_work',100,987)
    for i in range(1,n+1):
        for e in range(20):add(i,e,'wait_summary',1 if i==1 else 0,1 if i==1 else 0)
    for i in range(1,n+h+1):add(i,-1,'exit');add(0,-1,'join',i)
    for e in range(20):add(0,e,'wait_summary')
    add(0,-1,'exit')
    terminal=dict(returncode=0,timed_out=False,no_progress=False,output_limited=False,disk_low=False,error=None,kill_requested=False)
    def get(actor,phase,e=2):return check.one(rows[actor],phase,e)
    if mode=='missing-yield':get(2,'attempt_summary')['b']=0;get(2,'attempt_summary')['c']=1
    if mode=='placement':get(2,'cpu')['a']=2
    if mode=='lost-wakeup':
        start=rows[1].index(get(1,'data_publish'))
        generated=[]
        for phase,a,b,c,rc in [('wrong_wait_clock',100,900000000,0,0),('wrong_wait_call',1,101,150000000,0),('wrong_wait_return',0,0,0,errno.ETIMEDOUT)]:
            generated.append(dict(run_id=RUN,build_id=BUILD,cell=0,actor=1,seq=0,epoch=2,phase=phase,rc=rc,a=a,b=b,c=c,tick=0))
        rows[1][start:start]=generated
    if mode in ('stale-ack','ordering','held','frozen'):
        for i in rows:rows[i]=[r for r in rows[i] if (0<=r['epoch']<=2 and r['phase']!='wait_summary') or (r['epoch']==-1 and r['phase'] not in ('exit','join','helper_work'))]
        rows[0]=[r for r in rows[0] if not(r['epoch']==2 and r['phase']=='epoch_done')]
        terminal['returncode']=2
    if mode=='stale-ack':
        get(2,'ack_publish')['a']=1;check.select(rows[0],'ack_observed',2)[0]['b']=1
        rows[0]=[r for r in rows[0] if not(r['epoch']==2 and r['phase']=='ack_observed' and r['a']!=1)]
    if mode=='ordering':
        get(1,'data_publish')['a']=1
        rows[0]=[r for r in rows[0] if not(r['epoch']==2 and r['phase']=='ack_observed')]
        for i in range(2,n+1):
            get(i,'data_read')['a']=1
            rows[i]=[r for r in rows[i] if r['epoch']!=2 or r['phase'] not in ('update','target_unlocked','ack_publish')]
        # Only one peer owns the target when its failed read terminates the
        # process; other peers may still be blocked at their target attempt.
        for i in range(3,n+1):rows[i]=[r for r in rows[i] if r['epoch']!=2 or r['phase'] in ('held_observed','ready_publish','cpu','attempt')]
    if mode in ('held','frozen'):
        terminal.update(returncode=-9,no_progress=True,kill_requested=True)
        if mode=='held':
            rows[0]=[r for r in rows[0] if r['epoch']!=2 or r['phase']=='held_observed'];add(0,2,'withheld',1)
            rows[1]=[r for r in rows[1] if r['epoch']!=2 or r['phase'] in ('target_acquired','held_publish','wait_call')]
            for i in range(2,n+1):rows[i]=[r for r in rows[i] if r['epoch']!=2 or r['phase'] in ('held_observed','ready_publish','cpu','attempt')]
        else:
            rows[0]=[r for r in rows[0] if r['epoch']!=2 or r['phase'] in ('held_observed','release_publish','owner_done_observed')];add(0,2,'freeze_observed',1)
            rows[2]=[r for r in rows[2] if r['epoch']!=2 or r['phase'] in ('held_observed','ready_publish')];add(2,2,'freeze_wait_call');add(2,2,'wait_call')
    for i in rows:
        for seq,r in enumerate(rows[i]):r['seq']=seq;r['tick']=seq+1
    return job,rows,terminal,{i:b'' for i in rows}

class Tests(unittest.TestCase):
    def reject_written(self,job,rows,terminal,suffix,pattern=None):
        """Exercise the same complete-prefix loader and consumers as the parent."""
        with tempfile.TemporaryDirectory() as name:
            root=Path(name).resolve()
            for actor,data in rows.items():
                for i,r in enumerate(data):r['seq']=i;r['tick']=i+1
                (root/f'actor-{actor:03}.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in data))
            data,suffix=check.load(root,job,RUN,BUILD)
            with self.assertRaisesRegex(check.Invalid,pattern or '.'):
                run.progress_keys(data,job);check.validate(data,job,terminal,suffix)

    def test_c1_predicate_and_api_fields(self):
        for actor,phase,field,value in [(1,'release_receipt','a',0),(0,'owner_done_observed','a',0),
                (1,'held_publish','a',0),(2,'held_observed','b',0),(2,'ready_publish','a',0),
                (0,'release_publish','a',0),(0,'held_observed','b',0),(1,'owner_done_publish','a',0),
                (1,'target_acquired','rc',1),(1,'wait_return','a',1),(2,'ack_publish','a',0)]:
            job,rows,t,s=fixture();check.one(rows[actor],phase,2)[field]=value
            self.reject_written(job,rows,t,s)
        for phase,field,value in [('wrong_wait_clock','c',1),('wrong_wait_call','a',0),
                                  ('wrong_wait_call','c',150000001),('wrong_wait_return','rc',1)]:
            job,rows,t,s=fixture(mode='lost-wakeup');check.one(rows[1],phase,2)[field]=value
            self.reject_written(job,rows,t,s)

    def test_c2_controls_require_actual_cause_prefix(self):
        causes={'held':(1,'held_publish'),'missing-yield':(2,'busy_first'),'stale-ack':(2,'ack_publish'),
                'lost-wakeup':(1,'wrong_wait_return'),'placement':(2,'cpu'),
                'ordering':(1,'data_publish'),'frozen':(2,'wait_call')}
        for mode,(actor,phase) in causes.items():
            for n in (2,4,8):
                job,rows,t,s=fixture(n=n,mode=mode)
                rows[actor]=[r for r in rows[actor] if not(r['epoch']==2 and r['phase']==phase)]
                self.reject_written(job,rows,t,s)
        for mode,actors in [('held',(1,2)),('stale-ack',(2,)),('ordering',(1,))]:
            job,rows,t,s=fixture(mode=mode)
            for actor in actors:rows[actor]=[r for r in rows[actor] if r['epoch']!=2]
            self.reject_written(job,rows,t,s)

    def test_c3_lifecycle_and_terminal(self):
        for phase in ('exit','join'):
            job,rows,t,s=fixture();actor=1 if phase=='exit' else 0
            event=check.select(rows[actor],phase)[0];rows[actor].remove(event);rows[actor].insert(4,event)
            self.reject_written(job,rows,t,s,'actor-transition')
        job,rows,t,s=fixture();rows[1]=[r for r in rows[1] if r['phase']!='exit']
        self.reject_written(job,rows,t,s,'coverage-exit')
        for mode in ('held','frozen'):
            for changes in (dict(returncode=-15),dict(returncode=2),dict(kill_requested=False),
                            dict(timed_out=True),dict(output_limited=True),dict(disk_low=True),dict(error='Invalid')):
                job,rows,t,s=fixture(mode=mode);t.update(changes);self.reject_written(job,rows,t,s)

    def test_c4_invalid_progress_and_legitimate_prefixes(self):
        job,rows,_,_=fixture()
        for actor,phase,epoch,target in ((0,'join',-1,999),(2,'join',-1,1),(2,'ack_publish',19,19)):
            prefix={a:[copy.deepcopy(rs[0])] for a,rs in rows.items()}
            prefix[actor].append(dict(prefix[actor][0],seq=1,phase=phase,epoch=epoch,a=target,b=0,c=0))
            with self.assertRaises(check.Invalid):run.progress_keys(prefix,job)
        job,rows,_,_=fixture(mode='held')
        rows[2].append(dict(rows[2][-1],phase='target_acquired',a=0))
        with self.assertRaises(check.Invalid):run.progress_keys(rows,job)
        job,rows,t,s=fixture(n=4,mode='frozen')
        rows[3].append(dict(rows[3][-1],phase='wait_call',epoch=3,a=0))
        check.validate(rows,job,t,s);run.progress_keys(rows,job)
        job,rows,_,_=fixture()
        # Any per-actor prefix of a valid trace remains valid. This permits
        # asynchronous file scans without inventing cross-thread clock order.
        for actor,data in rows.items():
            for end in range(len(data)+1):check.actor_prefix(data[:end],actor,job)
        for mode in ('normal','frozen'):
            job,rows,t,s=fixture(mode=mode)
            for actor in (0,2):
                at=rows[actor].index(check.one(rows[actor],'held_observed',2))
                template=rows[actor][at]
                rows[actor]=[r for r in rows[actor] if not(r['epoch']==2 and r['phase'] in ('wait_call','wait_return'))]
                rows[actor][at:at]=[dict(template,phase=p,a=0,b=0,c=0) for p in ('wait_call','wait_return')]
                if mode=='normal':
                    summary=check.one(rows[actor],'wait_summary',2);summary['a']=summary['b']=3
            for data in rows.values():
                for seq,r in enumerate(data):r['seq']=seq;r['tick']=seq+1
            check.validate(rows,job,t,s);run.progress_keys(rows,job)
        for strategy in (0,1):
            job,rows,t,s=fixture();job['strategy']=strategy;check.one(rows[0],'config',-1)['c']=strategy
            for e in range(20):
                event=check.one(rows[2],'attempt',e);event['a']=strategy
                at=rows[2].index(event)+1
                rows[2].insert(at,dict(event,phase='busy_first',rc=errno.EBUSY,a=1))
                summary=check.one(rows[2],'attempt_summary',e);summary['a']=5;summary['b']=5 if strategy else 0
            for data in rows.values():
                for seq,r in enumerate(data):r['seq']=seq;r['tick']=seq+1
            self.assertEqual(check.validate(rows,job,t,s)['status'],'STRUCTURAL_PASS');run.progress_keys(rows,job)

    def test_invalid_progress_stops_dummy_without_renewal(self):
        job,rows,_,_=fixture(n=1)
        bad=dict(rows[0][0],seq=1,phase='join',a=999)
        with tempfile.TemporaryDirectory() as name:
            root=Path(name).resolve()
            source="import sys,time;f=open(sys.argv[1]+'/actor-000.jsonl','w');f.write(sys.argv[2]);f.flush();time.sleep(10)"
            result=run.supervise([sys.executable,'-c',source,str(root),'\n'.join(json.dumps(r) for r in (rows[0][0],bad))+'\n'],root,2,1024*1024,idle=.4,job=job,identity=(RUN,BUILD))
            self.assertEqual(result['error'],'Invalid');self.assertFalse(result['no_progress']);self.assertFalse(result['timed_out'])
            self.assertTrue(result['kill_requested']);self.assertEqual(result['returncode'],-9)

    def test_full_matrix_and_all_controls(self):
        for n in (1,2,4,8):
            job,rows,terminal,suffix=fixture(n=n);self.assertEqual(check.validate(rows,job,terminal,suffix)['status'],'STRUCTURAL_PASS');run.progress_keys(rows,job)
        for mode in check.CONTROLS:
            for n in (2,4,8):
                job,rows,terminal,suffix=fixture(n=n,mode=mode)
                self.assertEqual(check.validate(rows,job,terminal,suffix)['oracle'],mode);run.progress_keys(rows,job)

    def test_exact_schedule(self):
        jobs=run.schedule(10);self.assertEqual(len(jobs),279);self.assertEqual(jobs,run.schedule(10))
        self.assertEqual(sum(j['mode']=='normal' for j in jobs),216)
        self.assertEqual([j['mode'] for j in jobs[216::9]],list(check.CONTROLS))
        self.assertEqual(max(j['n']+j['h']+1 for j in run.schedule(64)),137)
        for j in jobs[216:]:self.assertEqual(j['strategy'],1 if j['mode']=='missing-yield' else 2)

    def test_negative_requires_real_complete_witness(self):
        job,rows,t,s=fixture(mode='lost-wakeup');rows[1]=[r for r in rows[1] if r['phase']!='wrong_wait_return']
        with self.assertRaises(check.Invalid):check.validate(rows,job,t,s)
        job,rows,t,s=fixture(mode='frozen');rows[0]=[r for r in rows[0] if not(r['phase']=='owner_done_observed' and r['epoch']==2)]
        with self.assertRaises(check.Invalid):check.validate(rows,job,t,s)
        job,rows,t,s=fixture(mode='missing-yield');check.one(rows[2],'attempt_summary',2)['a']=0
        with self.assertRaises(check.Invalid):check.validate(rows,job,t,s)
        job,rows,t,s=fixture(mode='held');t['no_progress']=False;t['timed_out']=True
        with self.assertRaises(check.Invalid):check.validate(rows,job,t,s)

    def test_missing_duplicate_stale_clock_and_identity(self):
        job,rows,t,s=fixture()
        for mutate in ('missing','duplicate','stale','sequence'):
            bad=copy.deepcopy(rows)
            if mutate=='missing':bad[2]=[r for r in bad[2] if not(r['phase']=='update' and r['epoch']==19)]
            if mutate=='duplicate':bad[2].append(copy.deepcopy(check.one(bad[2],'ack_publish',19)))
            if mutate=='stale':check.one(bad[2],'data_read',19)['a']=18
            if mutate=='sequence':check.one(bad[2],'update',19)['seq']=0
            with self.assertRaises(check.Invalid):check.validate(bad,job,t,s)
        with tempfile.TemporaryDirectory() as name:
            root=Path(name).resolve()
            for actor,data in rows.items():(root/f'actor-{actor:03}.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in data))
            loaded,suffix=check.load(root,job,RUN,BUILD);self.assertEqual(loaded,rows)
            path=root/'actor-002.jsonl';path.write_bytes(path.read_bytes()+b'{"partial":')
            _,suffix=check.load(root,job,RUN,BUILD);self.assertEqual(suffix[2],b'{"partial":')
            with self.assertRaises(check.Invalid):check.validate(rows,job,t,suffix)
            with self.assertRaises(check.Invalid):check.load(root,job,'f'*32,BUILD)
            bad=copy.deepcopy(rows[2]);bad[2]['tick']=0;path.write_text(''.join(json.dumps(r)+'\n' for r in bad))
            with self.assertRaises(check.Invalid):check.load(root,job,RUN,BUILD)

    def test_progress_excludes_helpers_and_duplicates(self):
        job,rows,_,_=fixture(h=2);first=run.progress_keys(rows,job)
        check.one(rows[job['n']+1],'helper_work',-1)['a']+=1;self.assertEqual(first,run.progress_keys(rows,job))
        rows[2].append(copy.deepcopy(check.one(rows[2],'ack_publish',19)))
        with self.assertRaises(check.Invalid):run.progress_keys(rows,job)

    def test_idle_timeout_retains_committed_prefix_and_partial_suffix(self):
        job,rows,_,_=fixture(n=1,h=1)
        with tempfile.TemporaryDirectory() as name:
            root=Path(name).resolve()
            source="import sys,time;f=open(sys.argv[1]+'/actor-000.jsonl','wb');f.write(sys.argv[2].encode()+b'\\n');f.flush();f.write(b'{\"unfinished\":');f.flush();exec('while True: print(\"unrelated bytes\",flush=True);time.sleep(.01)')"
            result=run.supervise([sys.executable,'-c',source,str(root),json.dumps(rows[0][0])],root,2,1024*1024,idle=.2,job=job,identity=(RUN,BUILD))
            self.assertTrue(result['no_progress']);self.assertFalse(result['timed_out']);self.assertIsNone(result['error'])
            retained,suffix=check.load(root,job,RUN,BUILD)
            self.assertEqual(retained[0],[rows[0][0]]);self.assertEqual(suffix[0],b'{"unfinished":')
            self.assertGreater((root/'stdout').stat().st_size,0)

    def test_outer_completion_is_required(self):
        with tempfile.TemporaryDirectory() as name:
            root=Path(name).resolve()
            with self.assertRaises(FileNotFoundError):run.validate_campaign(root)
            run.write(root/'supervisor.json',dict(schema='M23-L1-supervisor-v1',status='failed'))
            run.write(root/'plan.json',{})
            with self.assertRaises(AssertionError):run.validate_campaign(root)

    def test_dummy_timeout_cap_and_group_cleanup(self):
        with tempfile.TemporaryDirectory() as name:
            root=Path(name).resolve();source="import subprocess,sys,time;subprocess.Popen([sys.executable,'-c',\"import time,sys;f=open(sys.argv[1],'a');exec('while True: f.write(\\\"x\\\");f.flush();time.sleep(.01)')\",sys.argv[1]+'/beat']);time.sleep(30)"
            result=run.supervise([sys.executable,'-c',source,str(root)],root,.25,1024*1024)
            self.assertTrue(result['timed_out']);size=(root/'beat').stat().st_size;time.sleep(.08);self.assertEqual(size,(root/'beat').stat().st_size)
        with tempfile.TemporaryDirectory() as name:
            root=Path(name).resolve();result=run.supervise([sys.executable,'-c',"import sys,time;open(sys.argv[1]+'/big','wb').write(b'x'*8192);time.sleep(30)",str(root)],root,2,4096)
            self.assertTrue(result['output_limited'])

if __name__=='__main__':
    suite=unittest.defaultTestLoader.loadTestsFromTestCase(Tests)
    if os.environ.get('M23_BUILD_VALIDATION')=='1':
        suite=unittest.TestSuite(t for t in suite if t._testMethodName!='test_dummy_timeout_cap_and_group_cleanup')
    result=unittest.TextTestRunner().run(suite);sys.exit(not result.wasSuccessful())
