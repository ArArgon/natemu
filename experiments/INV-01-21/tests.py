"""Injected records/clock values and dummy processes only; never run lifecycle."""
import copy
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import check
import run

ID='0'*32
JOB=dict(index=84,arm='C2',n=1,seed=21001,window=300)

def fixture(job=JOB):
    rows=[dict(schema='M21-1-trace-v1',run_id=ID,build_id=ID,n=job['n'],arm=job['arm'],timebase=[1,1],record_size=120,
               backend='nanosleep-relative',clock='API-defined',leeway='not-exposed',resolutions=[1]*5)]
    for a in range(job['n']+9):
        if a<=job['n']:
            for seq in range(2):
                clocks=[1_000_000_000+seq*400_000_000]*5
                rows.append(dict(row=[a,seq,seq+1 if a==job['n'] else seq,*clocks,25,0,0,25,0,0,-2 if a==job['n'] else -1,0,a+1,0]))
        rows.append(dict(actor=a,reserved=2 if a<=job['n'] else 0,dropped=0,demoted=False,interruptions=0,errors=0,
                         continuous=[7 if a==job['n'] else 4 if a<job['n'] else 0,0,0,0,0,0,0],clocks=[[2 if a<=job['n'] else 0,0,0,0,0,0,0] for _ in range(5)]))
    rows.append(dict(complete=True,invalid=False,reversal=False,drained=True,joined=job['n']+1));return rows

def save(path,rows):path.write_text(''.join(json.dumps(r)+'\n' for r in rows))

class Rules(unittest.TestCase):
    def test_schedule_and_envelopes(self):
        jobs=check.schedule();self.assertEqual(len(jobs),96);self.assertEqual(sum(j['window'] for j in jobs),72000)
        self.assertEqual(sum(j['window']+300+(900 if j['arm']=='T1' else 0) for j in jobs),111600)
        self.assertEqual(jobs[0],dict(index=0,arm='T1',n=1,seed=21001,window=1800));self.assertEqual(jobs[-1]['seed'],21003)
        for arm in check.ARMS:
            self.assertEqual(sum(j['arm']==arm for j in jobs),12)
        j=jobs[0];baseline=[j,1,1,1,[],0,0,3*1024**3]
        self.assertIsNone(check.budgets(*baseline))
        for index,value,expected in [(1,2100,'awake-timeout'),(2,3000,'continuous-timeout'),(3,129600,'campaign-time'),(4,[256*run.MIB],'file-output'),(5,512*run.MIB,'slot-output'),(6,8*1024**3,'campaign-output'),(7,0,'free-space')]:
            args=baseline.copy();args[index]=value;self.assertEqual(check.budgets(*args),expected)
        # Resumed host jumps continuous time, awake remains inside its ceiling.
        self.assertEqual(check.budgets(j,100,3001,3001,[],0,0,3*1024**3),'continuous-timeout')

    def phase(self,targets):
        p=check.Phases(0);p.receipt('notification',1,2);p.receipt('notification',2,3);p.advance(120)
        p.receipt('induction-start',121)
        for i,s in enumerate(targets):p.receipt('notification',122+i,s)
        p.receipt('wake-confirmed',200,confirmed=True,associated=True);p.advance(260)
        p.receipt('notification',261,2);p.receipt('notification',262,3);p.advance(380);return p

    def test_target_and_controls(self):
        for target,expected in [([0,1],'bracketed'),([],'notification-not-observed-in-window'),([0],'incomplete-pair'),([1],'incomplete-pair'),([1,0],'ambiguous'),([0,1,1],'ambiguous')]:
            p=self.phase(target);self.assertEqual(p.result(),expected)
            p.receipt('notification',400,1);self.assertEqual(p.result(),expected);self.assertEqual(p.late, [1])
        self.assertEqual(self.phase([]).result(registered=False),'instrument-invalid')
        self.assertEqual(self.phase([]).result(drained=False),'instrument-invalid')
        for pre in ([],[2],[3,2],[2,3,3]):
            p=check.Phases(0)
            for i,s in enumerate(pre):p.receipt('notification',i+1,s)
            p.advance(120);self.assertEqual(p.result(),'invalid-screen-control')
            with self.assertRaises(ValueError):p.receipt('induction-start',121)
        p=self.phase([]);p.association=False;self.assertEqual(p.result(),'unresolved')
        p=check.Phases(0);p.pre=[2,3];p.advance(120);p.receipt('induction-start',121);p.receipt('wake-confirmed',1790,confirmed=True,associated=True);p.advance(1800);self.assertEqual(p.result(),'unresolved')
        p=check.Phases(0);p.pre=[2,3];p.advance(120);p.receipt('induction-start',121);p.receipt('wake-confirmed',200,confirmed=True,associated=True);p.advance(260);p.advance(380);self.assertEqual(p.result(),'invalid-screen-control')

    def test_stream_mutations_and_censoring(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'trace.jsonl';base=fixture();save(p,base);r=check.trace(p,JOB,ID,ID)
            self.assertEqual(r['progress'],'diagnostic-gap');self.assertEqual(r['continuous'],'advanced');self.assertEqual(r['frozen'],'unreachable')
            # Identical raw 400 ms gaps have identical categories regardless of
            # a hidden scheduling or transition cause absent from the evidence.
            self.assertEqual(check.trace(p,JOB,ID,ID)['progress'],r['progress'])
            self.assertEqual(check.trace(p,JOB,ID,ID,1.0,1.2)['progress'],'no-observed-gap')
            mutations=[]
            x=copy.deepcopy(base);x.pop(1);mutations.append(x)
            x=copy.deepcopy(base);x[1]['row'][1]=1;mutations.append(x)
            x=copy.deepcopy(base);x[1]['row'][10]=1;mutations.append(x)
            x=copy.deepcopy(base);x[1]['row'][9]=1;mutations.append(x)
            x=copy.deepcopy(base);x[1]['row'][11]=9;mutations.append(x)
            x=copy.deepcopy(base);x[0]['timebase']=[0,1];mutations.append(x)
            x=copy.deepcopy(base);x[3]['dropped']=1;mutations.append(x)
            x=copy.deepcopy(base);x[3]['reserved']=32769;mutations.append(x)
            x=copy.deepcopy(base);x[3]['continuous'][-1]=1;mutations.append(x)
            x=copy.deepcopy(base);x[-1]['drained']=False;mutations.append(x)
            mutations.append(base[:-1])
            for x in mutations:
                save(p,x)
                with self.assertRaises((ValueError,KeyError)):check.trace(p,JOB,ID,ID)
            save(p,base);p.write_bytes(p.read_bytes()[:-2])
            with self.assertRaises(ValueError):check.trace(p,JOB,ID,ID)
            x=copy.deepcopy(base);x[1]['row'][4]=1000;x[2]['row'][4]=1100;x[3]['continuous']=[4,1,1000,900,0,1,0];x[-1]['reversal']=True;save(p,x);r=check.trace(p,JOB,ID,ID)
            self.assertEqual(r['continuous'],'backward');self.assertEqual(r['progress'],'unavailable')

    def test_real_dummy_kill_and_denominator(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'operator').mkdir();child=root/'000';child.mkdir();ticks=iter((i*40,i*40) for i in range(20))
            terminal,events=run.supervise([sys.executable,'-c','import sys; sys.stdin.read()'],child,JOB,root,0,set(),
                clock=lambda:next(ticks),resource=lambda:([],0,0,3*1024**3))
            self.assertTrue(terminal['killed']);self.assertLess(terminal['returncode'],0);self.assertTrue(terminal['missing_in_memory_trace'])
            result=check.classify(child,JOB,ID,ID,terminal,events);self.assertEqual(result['finding'],'none')
            rows=[dict(index=j['index'],arm=j['arm'],status='not-run') for j in check.schedule()];rows[0]=dict(result,index=0)
            self.assertEqual(len(rows),96);self.assertEqual(sum(r['status']=='not-run' for r in rows),95)
            self.assertIsNone(check.stop_reason([dict(arm='T1',status='timed-out')]*2));self.assertEqual(check.stop_reason([dict(arm='T1',status='timed-out')]*3),'third-arm-timeout')

    def test_completed_oracles_and_unsolicited_notification(self):
        def events(extra=()):
            after=[e for e in extra if e['event']=='activity-end'];extra=[e for e in extra if e['event']!='activity-end']
            if any(e['event']=='activity-begin' for e in extra):extra.insert(0,dict(event='activity-request'))
            items=[dict(event='main-qos',requested=25,set=0,get=0,got=25,relative=0),dict(event='registered',sources=list(range(8)),owned_bytes=70*run.MIB),dict(event='armed'),*extra,dict(event='normal-stop'),dict(event='admission-closed',removed=list(range(8))),*after,dict(event='complete')]
            return [dict(v,ordinal=i,awake=0 if i<3 else 1 if i<3+len(extra) else 301) for i,v in enumerate(items)]
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'trace.jsonl';base=fixture();save(p,base);terminal=dict(returncode=0,killed=False)
            self.assertEqual(check.classify(d,JOB,ID,ID,terminal,events())['status'],'completed')
            e=events();e.pop(3)
            for i,r in enumerate(e):r['ordinal']=i
            self.assertEqual(check.classify(d,JOB,ID,ID,terminal,e)['status'],'instrument-invalid')
            c1=dict(JOB,arm='C1');save(p,fixture(c1))
            self.assertTrue(check.classify(d,c1,ID,ID,terminal,events([dict(event='activity-begin',token=True)]))['fatal'])
            self.assertEqual(check.classify(d,c1,ID,ID,terminal,events([dict(event='activity-begin',token=True),dict(event='activity-end')]))['detectability'],'self-request-ended')
            t5=dict(JOB,arm='T5');save(p,fixture(t5));self.assertTrue(check.classify(d,t5,ID,ID,terminal,events([dict(event='demote-published')]))['fatal'])
            x=fixture();index=next(i for i,r in enumerate(x) if r.get('actor')==2);x[index]['reserved']=1
            x.insert(index,dict(row=[2,0,0,*([1000000000]*5),0,0,0,25,0,0,0,1,99,0]));save(p,x)
            e=events([dict(event='notification',source=0,seq=0,receipt=0,value=1)])
            self.assertEqual(check.classify(d,JOB,ID,ID,terminal,e)['status'],'mixed-condition')
            e[3]['receipt']=1;self.assertEqual(check.classify(d,JOB,ID,ID,terminal,e)['status'],'instrument-invalid')

    def test_identity_roots_and_marker_capacity(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d).resolve();out=root/'new'
            with patch.dict(os.environ,{'NATEMU_RAW_ROOT':str(root)}):
                self.assertEqual(run.destination(str(out)),out);out.mkdir()
                with self.assertRaises(ValueError):run.destination(str(out))
            p=root/'source';p.write_text('first');files={'source':run.sha(p)};run.verify(root,files);p.write_text('changed')
            with self.assertRaises(ValueError):run.verify(root,files)
            with patch.dict(os.environ,{'NATEMU_RAW_ROOT':str(run.REPO)}):
                with self.assertRaises(ValueError):run.destination(str(run.REPO/'unsafe'))
            recorder=run.Recorder(root/'receipts')
            with self.assertRaises(ValueError):recorder.add('huge',0,text='x'*256)
            recorder.close()

    def test_dummy_resource_stops(self):
        for files,free,expected in [([256*run.MIB],3*1024**3,'file-output'),([],0,'free-space')]:
            with tempfile.TemporaryDirectory() as d:
                root=Path(d);(root/'operator').mkdir();child=root/'000';child.mkdir()
                terminal,events=run.supervise([sys.executable,'-c','import sys; sys.stdin.read()'],child,JOB,root,0,set(),clock=lambda:(0,0),resource=lambda:(files,0,0,free))
                self.assertTrue(terminal['killed']);self.assertEqual(terminal['reason'],expected);self.assertLess(terminal['returncode'],0)
                self.assertEqual(check.classify(child,JOB,ID,ID,terminal,events)['finding'],'none')

    def test_real_serializer_and_kill_prefix(self):
        exe=Path(__file__).resolve().parent.parent/'serializer_test'
        if not exe.exists():self.skipTest('retained build executes compiled serializer fixture')
        for mode in ('complete','before','during'):
            with tempfile.TemporaryDirectory() as d:
                p=subprocess.Popen([str(exe),mode],cwd=d,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True)
                if mode=='complete':out,err=p.communicate(timeout=5);self.assertEqual(p.returncode,0,err);self.assertEqual(check.trace(Path(d)/'trace.jsonl',JOB,ID,ID)['progress'],'diagnostic-gap')
                else:
                    selector=selectors.DefaultSelector();selector.register(p.stdout,selectors.EVENT_READ)
                    self.assertTrue(selector.select(5));self.assertEqual(p.stdout.readline(),b'prefix-ready\n');selector.close()
                    os.killpg(p.pid,signal.SIGKILL);p.communicate(timeout=5);self.assertLess(p.returncode,0)
                    trace=Path(d)/'trace.jsonl'
                    if mode=='before':self.assertFalse(trace.exists())
                    else:
                        self.assertGreater(trace.stat().st_size,0)
                        with self.assertRaises(ValueError):check.trace(trace,JOB,ID,ID)

if __name__=='__main__':unittest.main()
