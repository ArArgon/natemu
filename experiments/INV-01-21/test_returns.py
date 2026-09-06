"""M21-C1–C5: focused author regressions from the independent review.

All records/clocks are fabricated; the only executed processes are pipe dummies.
"""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import check
import run
from tests import fixture,save,JOB,ID

def events(extra=(),stop='normal-stop',stop_time=301):
    before=[e for e in extra if e['event']!='activity-end'];after=[e for e in extra if e['event']=='activity-end']
    values=[dict(event='main-qos',requested=25,set=0,get=0,got=25,relative=0),
            dict(event='registered',sources=list(range(8)),owned_bytes=70*run.MIB),dict(event='armed'),
            *before,dict(event=stop),dict(event='admission-closed',removed=list(range(8))),*after,dict(event='complete')]
    return [dict(e,ordinal=i,awake=e.get('awake',0 if i<3 else 1 if i<3+len(before) else stop_time)) for i,e in enumerate(values)]

def classified(rows=None,ev=None,job=JOB):
    with tempfile.TemporaryDirectory() as directory:
        save(Path(directory)/'trace.jsonl',fixture(job) if rows is None else rows)
        return check.classify(directory,job,ID,ID,dict(returncode=0,killed=False),events() if ev is None else ev)

def reversal(rows,k):
    rows[2]['row'][3+k]=900000000
    rows[3]['clocks'][k]=[2,1,1000000000,900000000,0,1,0]
    if k==1:rows[3]['continuous']=[4,1,1000000000,900000000,0,1,0];rows[-1]['reversal']=True

class Returns(unittest.TestCase):
    def test_c2_lifecycle_and_valid_trailing_receipts(self):
        self.assertEqual(classified()['status'],'completed')
        for other in (4,5):
            ev=events();ev[0],ev[other]=dict(ev[other],awake=0),dict(ev[0],awake=301)
            for i,e in enumerate(ev):e['ordinal']=i
            self.assertEqual(classified(ev=ev)['reason'],'lifecycle-order')
        ev=events();ev[1],ev[4]=dict(ev[4],awake=0),dict(ev[1],awake=301)
        for i,e in enumerate(ev):e['ordinal']=i
        self.assertEqual(classified(ev=ev)['reason'],'lifecycle-order')
        rows=fixture();index=next(i for i,r in enumerate(rows) if r.get('actor')==2);rows[index]['reserved']=1
        rows.insert(index,dict(row=[2,0,0,*([1000000000]*5),0,0,0,25,0,0,0,1,99,0]))
        ev=events();ev.insert(-1,dict(event='notification',source=0,seq=0,receipt=0,value=1,awake=301))
        for i,e in enumerate(ev):e['ordinal']=i
        self.assertEqual(classified(rows,ev)['status'],'mixed-condition')
        c1=dict(JOB,arm='C1');extra=[dict(event='activity-request'),dict(event='activity-begin',token=True),dict(event='activity-end')]
        self.assertEqual(classified(ev=events(extra),job=c1)['detectability'],'self-request-ended')
        ev=events(extra);a=next(i for i,e in enumerate(ev) if e['event']=='activity-end');b=next(i for i,e in enumerate(ev) if e['event']=='admission-closed');ev[a],ev[b]=ev[b],ev[a]
        for i,e in enumerate(ev):e['ordinal']=i
        self.assertEqual(classified(ev=ev,job=c1)['reason'],'activity-control')
        early=events(stop='pre-control-stop',stop_time=120)
        self.assertEqual(classified(ev=early,job=dict(JOB,arm='T1',window=1800))['detectability'],'invalid-screen-control')
        rows=fixture();reversal(rows,1)
        early=events(stop='reversal',stop_time=2)
        result=classified(rows,early);self.assertEqual(result['continuous'],'backward');self.assertTrue(result['fatal'])

    def test_c3_t5_window_and_exact_cutoff(self):
        job=dict(JOB,arm='T5');extra=[dict(event='demote-request'),dict(event='demote-published')]
        for second,expected in ((1.4,'completed'),(300,'completed'),(300.000000001,'instrument-invalid'),(302,'instrument-invalid')):
            rows=fixture(job)
            for r in rows:
                if 'row' in r and r['row'][0]<=job['n'] and r['row'][1]==1:
                    r['row'][3:8]=[int(second*1e9)]*5;r['row'][8]=r['row'][11]=9
                if 'actor' in r and r['actor']<=job['n']:r['demoted']=True
            result=classified(rows,events(extra),job);self.assertEqual(result['status'],expected,result)
            if second==302:self.assertEqual(result['reason'],'self-qos-control')
        # Raw read-back at the cutoff is admitted; publication receipt at the
        # cutoff is too late to establish an in-window control request.
        ev=events([dict(event='demote-request'),dict(event='demote-published',awake=300)])
        self.assertEqual(classified(rows,ev,job)['reason'],'demotion-publication-window')

    def test_c4_missing_and_inconsistent_witnesses(self):
        base=fixture()
        changes=[]
        rows=copy.deepcopy(base)
        for r in rows:
            if 'actor' in r:r['continuous']=[0]*7;r['clocks']=[[0]*7 for _ in range(5)]
        changes.append(rows)
        for label,value in [('continuous',[2,0,0,0,0,0,0]),('continuous',[4,4,1000,900,0,1,0]),('continuous',[4,1,1000,900,0,3,0]),('continuous',[4,0,1000,900,0,1,0])]:
            rows=copy.deepcopy(base);rows[3][label]=value;changes.append(rows)
        rows=copy.deepcopy(base);rows[3]['clocks'][0]=[1,0,0,0,0,0,0];changes.append(rows)
        rows=copy.deepcopy(base);rows[3]['clocks'][0]=[2,1,1000,900,0,1,0];changes.append(rows)
        rows=copy.deepcopy(base);rows[6]['continuous'][0]=9;changes.append(rows)
        for rows in changes:self.assertEqual(classified(rows)['status'],'instrument-invalid')
        sparse=copy.deepcopy(base);sparse[2]['row'][2]=1000000;sparse[3]['continuous'][0]=1000004
        self.assertEqual(classified(sparse)['status'],'completed')
        interrupted=copy.deepcopy(base);interrupted[6]['interruptions']=1;interrupted[6]['continuous'][0]=9
        self.assertEqual(classified(interrupted)['status'],'completed')
        sparse=copy.deepcopy(base);sparse[1]['row'][4]=1000;sparse[2]['row'][4]=1100;sparse[3]['continuous']=[4,1,1000,900,0,1,0];sparse[-1]['reversal']=True
        self.assertEqual(classified(sparse)['continuous'],'backward')

    def test_c5_every_ordered_clock_stops_without_promotion(self):
        for k in range(5):
            rows=fixture();reversal(rows,k);result=classified(rows)
            self.assertEqual(result['clock_backward'],[i==k for i in range(5)])
            self.assertTrue(result['fatal']);self.assertEqual(check.stop_reason([result]),'fatal-outcome')
            self.assertEqual(result['continuous']=='backward',k==1)

    def test_c1_actual_supervisor_callback_and_operator_cutoffs(self):
        # The fake input generator services both cutoffs from either an actual
        # pipe receipt or an operator receipt; clocks and captures are injected.
        for first in ('callback','operator'):
            with tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);(root/'operator').mkdir();child=root/'000';child.mkdir()
                state={'now':0,'calls':0,'stage':0};original_decode=check.decode
                def clock():
                    state['calls']+=1
                    if state['calls']>200:state['now']=1801
                    return state['now'],state['now']
                def decode(line):
                    msg=original_decode(line)
                    if msg.get('event')=='notification' and msg.get('source')==7:state['now']=121
                    if msg.get('event')=='notification' and msg.get('source')==6:state['stage']=1
                    if msg.get('event')=='notification' and msg.get('source')==4:state['now']=190
                    return msg
                def inputs(_root,_slot,_seen):
                    # On callback case source6 was first serviced at121; on
                    # operator case the input itself is first at121.
                    if state['stage']==1:
                        state['stage']=2;state['now']=121
                        yield root/'induction',dict(kind='induction-start')
                        state['now']=130
                        yield root/'wake',dict(kind='wake-confirmed')
                        if first=='operator':
                            state['now']=190;yield root/'other',dict(kind='unintended-transition')
                        else:(root/'post-ready').write_text('synthetic')
                def evidence(_root,_child,path,_data):
                    if path.name=='induction':return 'synthetic',{'method':'Apple-menu Sleep'},{'assertions':{}}
                    if path.name=='wake':return 'synthetic',dict(entry=True,wake=True,records_agree=True,associated=True),{'independent':{},'pmset-log':{}}
                    return 'synthetic',dict(confirmed=False,associated=False),{'context':{}}
                sources=[2,3,7,6] if first=='callback' else [2,3,6]
                messages=[{'event':'armed'},*({'event':'notification','source':s,'seq':0,'receipt':i,'value':1} for i,s in enumerate(sources))]
                # Each pair is one short pipe write so the trigger's decode
                # advances the injected clock before the next buffered receipt.
                source="import sys,json,pathlib\nsys.stdout.write(''.join(json.dumps(m)+'\\n' for m in json.loads(sys.argv[1])));sys.stdout.flush()\n"
                if first=='callback':
                    source+="while not pathlib.Path(sys.argv[2]).exists(): pass\nsys.stdout.write(''.join(json.dumps(dict(event='notification',source=s,seq=1,receipt=4+i,value=1))+'\\n' for i,s in enumerate((4,0))));sys.stdout.flush()\n"
                source+="sys.stdin.readline();print('{\"event\":\"complete\"}',flush=True)"
                with patch.object(check,'decode',decode),patch.object(run,'operator_inputs',inputs),patch.object(run,'evidence',evidence):
                    terminal,ev=run.supervise([sys.executable,'-c',source,json.dumps(messages),str(root/'post-ready')],child,dict(index=0,arm='T1',n=1,seed=21001,window=1800),root,0,set(),clock=clock,resource=lambda:([],0,0,3*1024**3))
                phases=[e['event'] for e in ev if e['event'].startswith('phase-')]
                self.assertEqual(phases.count('phase-await-induction'),1,(phases,terminal));self.assertEqual(phases.count('phase-post'),1,(phases,terminal))
                if first=='callback':
                    post=next(e for e in ev if e['event']=='phase-post');late=next(e for e in ev if e.get('source')==0)
                    self.assertEqual(late['awake'],190);self.assertLess(post['ordinal'],late['ordinal'])
                    p=check.Phases(0)
                    for e in ev:
                        if e['event'] in ('notification','induction-start','wake-confirmed'):p.receipt(e['event'],e['awake'],e.get('source'),e.get('confirmed',False),e.get('associated',False))
                        else:p.advance(e['awake'])
                    self.assertEqual(p.bucket,'notification-not-observed-in-window');self.assertIn(0,p.late)

if __name__=='__main__':unittest.main()
