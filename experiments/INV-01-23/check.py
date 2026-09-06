"""Independent M23-L1 raw-prefix predicates. Structural outcomes, no timing approval."""
import errno
import json
import signal
from pathlib import Path

EPOCHS=20
CONTROLS=('held','missing-yield','stale-ack','lost-wakeup','placement','ordering','frozen')
FIELDS={'cell','run_id','build_id','actor','seq','epoch','phase','rc','a','b','c','tick'}
PHASES={'config','timebase','start','qos_set','qos_get','cpu','create','wait_call','wait_return','wait_summary','target_acquired','held_publish','held_observed','ready_publish','attempt','busy_first','attempt_summary','release_publish','release_receipt','data_publish','data_read','update','target_unlocked','ack_publish','ack_observed','owner_done_publish','owner_done_observed','epoch_done','withheld','freeze_wait_call','freeze_observed','wrong_wait_clock','wrong_wait_call','wrong_wait_return','helper_work','join','exit'}
COMMON={'start','qos_set','qos_get','cpu','exit'}
WAIT={'wait_call','wait_return','wait_summary'}
ALLOWED={0:COMMON|WAIT|{'config','timebase','create','join','held_observed','release_publish','owner_done_observed','ack_observed','epoch_done','withheld','freeze_observed'},
         1:COMMON|WAIT|{'target_acquired','held_publish','release_receipt','data_publish','target_unlocked','owner_done_publish','wrong_wait_clock','wrong_wait_call','wrong_wait_return'},
         2:COMMON|WAIT|{'held_observed','ready_publish','attempt','busy_first','target_acquired','attempt_summary','data_read','update','target_unlocked','ack_publish','freeze_wait_call'},
         3:COMMON|{'helper_work'}}
ELIGIBLE={'start','target_acquired','held_publish','held_observed','ready_publish','attempt','busy_first','release_publish','release_receipt','target_unlocked','update','ack_publish','owner_done_publish','owner_done_observed','epoch_done','join','exit'}
class Invalid(ValueError):pass

def need(ok,why):
    if not ok:raise Invalid(why)
def uint(x):return type(x) is int and 0<=x<2**64

def decode(text):
    def unique(pairs):
        out={}
        for k,v in pairs:need(k not in out,'duplicate-key');out[k]=v
        return out
    return json.loads(text,object_pairs_hook=unique,parse_constant=lambda _:(_ for _ in ()).throw(Invalid('nonfinite')))

def load(directory,job,run_id,build_id):
    records={};suffix={}
    for actor in range(job['n']+job['h']+1):
        path=Path(directory)/f'actor-{actor:03}.jsonl'
        limit=600 if actor==0 else 200 if actor==1 else 300 if actor<=job['n'] else 8
        if path.exists():need(path.resolve()==path and path.is_file() and path.stat().st_size<=limit*2048+2047,'trace-buffer-bound')
        if path.exists():
            with path.open('rb') as stream:data=stream.read(limit*2048+2048)
            need(len(data)<=limit*2048+2047,'trace-buffer-bound')
        else:data=b''
        need(len(data)<=32*1024*1024,'trace-cap');parts=data.split(b'\n');suffix[actor]=parts.pop()
        need(len(parts)<=limit and len(suffix[actor])<2048,'record-count-bound')
        rows=[]
        for i,line in enumerate(parts):
            need(len(line)<2048,'record-size');r=decode(line)
            need(type(r) is dict and set(r)==FIELDS,'record-fields')
            need(r['run_id']==run_id and r['build_id']==build_id and type(r['cell']) is int and r['cell']==job['index'],'mixed-identity')
            need(type(r['actor']) is int and r['actor']==actor and type(r['seq']) is int and r['seq']==i,'sequence')
            need(type(r['epoch']) is int and -1<=r['epoch']<EPOCHS and r['phase'] in PHASES,'phase')
            need(type(r['rc']) is int and all(uint(r[x]) for x in ('a','b','c','tick')),'domain')
            if rows:need(r['tick']>=rows[-1]['tick'],'actor-time-backward')
            rows.append(r)
        records[actor]=rows
    actual={x.name for x in Path(directory).glob('actor-*.jsonl')}
    need(actual.issubset({f'actor-{i:03}.jsonl' for i in records}),'extra-actor')
    return records,suffix

def select(rows,phase,e=None):return [r for r in rows if r['phase']==phase and (e is None or r['epoch']==e)]
def one(rows,phase,e):
    found=select(rows,phase,e);need(len(found)==1,'coverage-'+phase);return found[0]
def ordered(rows,phases,e):
    events=[one(rows,p,e) for p in phases];need([r['seq'] for r in events]==sorted(r['seq'] for r in events),'lifecycle');return events

def actor_prefix(rows,actor,job):
    """Check the actual per-actor program order, including killed prefixes.

    This deliberately uses no cross-actor timestamp ordering. A live file scan
    can observe different prefix lengths on different actors.
    """
    n=job['n'];mode=job['mode'];helper=actor>n
    expected=[(-1,p,None) for p in ('start','qos_set','qos_get','cpu')]
    if actor==0:
        expected += [(-1,p,None) for p in ('config','timebase')]
        expected += [(-1,'create',i) for i in range(1,n+job['h']+1)]
    if helper:expected += [(-1,'helper_work',None)]
    else:
        for e in range(EPOCHS):
            if actor==0:
                phases=['held_observed','release_publish','owner_done_observed']
                if mode=='held' and e==2:phases=['held_observed','withheld']
            elif actor==1:
                phases=['target_acquired','held_publish','release_receipt']
                if mode=='lost-wakeup' and e==2:phases += ['wrong_wait_clock','wrong_wait_call','wrong_wait_return']
                phases += ['data_publish','target_unlocked','owner_done_publish']
                if mode=='held' and e==2:phases=phases[:2]
            else:
                phases=['held_observed','ready_publish','cpu','attempt','target_acquired','attempt_summary','data_read','update','target_unlocked','ack_publish']
                if mode=='held' and e==2:phases=phases[:4]
                if mode=='frozen' and actor==2 and e==2:phases=phases[:2]+['freeze_wait_call']
                if mode=='ordering' and e==2:phases=phases[:7]
            expected += [(e,p,None) for p in phases]
            if (mode=='held' and e==2 and actor<=1) or (mode=='frozen' and actor==2 and e==2) or (mode=='ordering' and actor>=2 and e==2):break
            if actor!=0 and mode in ('held','frozen','stale-ack','ordering') and e==2:break
            if actor==0:
                if mode in ('ordering','frozen') and e==2:break
                if mode=='stale-ack' and e==2:
                    expected += [(e,'ack_observed',1)];break
                expected += [(e,'ack_observed',i) for i in range(1,n)]
                expected += [(e,'epoch_done',None)]
        else:
            if actor==0:expected += [(-1,'join',i) for i in range(1,n+job['h']+1)]
            expected += [(e,'wait_summary',None) for e in range(EPOCHS)]
            expected += [(-1,'exit',None)]
    if helper:expected += [(-1,'exit',None)]
    at=0;waiting=None;wait_epochs=set();busy_epochs=set();freeze=False;last={}
    for r in rows:
        record_values(r,actor,job)
        p=r['phase'];e=r['epoch']
        if p in ('wait_call','wait_return'):
            need(not helper and 0<=e<EPOCHS,'wait-role')
            if p=='wait_call':
                need(waiting is None and e not in wait_epochs,'wait-pair')
                prior=last.get(e)
                allowed=prior in ('held_publish','owner_done_publish') if actor==1 else prior in (None,'release_publish','owner_done_observed','withheld','freeze_observed') if actor==0 else prior in (None,'freeze_wait_call')
                # A peer/controller may wait before its next epoch starts, but
                # only after the preceding epoch's own final operation.
                if prior is None:
                    next_event=expected[at] if at<len(expected) else None
                    next_wait=next_event==(e,'held_observed',None)
                    # A peer which completed the failing epoch may already
                    # wait for epoch 3; owner/controller cannot publish it.
                    next_wait |= 2<=actor<=n and e==3 and last.get(2)=='ack_publish' and mode in ('stale-ack','frozen')
                    allowed=allowed and next_wait
                need(allowed,'wait-prerequisite');waiting=e;wait_epochs.add(e)
            else:need(waiting==e,'wait-pair');waiting=None
            continue
        need(waiting is None,'operation-during-wait')
        if p=='busy_first':
            need(2<=actor<=n and job['strategy'] in (0,1) and last.get(e)=='attempt' and e not in busy_epochs,'busy-prerequisite')
            busy_epochs.add(e);continue
        if p=='freeze_observed':
            need(actor==0 and mode=='frozen' and e==2 and last.get(e) in ('release_publish','owner_done_observed') and not freeze,'freeze-prerequisite')
            freeze=True;continue
        need(at<len(expected) and (e,p)==expected[at][:2],'actor-transition-'+p)
        target=expected[at][2]
        if target is not None:need(r['a']==target,'actor-target-'+p)
        if p=='release_receipt':need(e in wait_epochs,'owner-wait-coverage')
        if p=='wrong_wait_call':
            before=one(rows,'wrong_wait_clock',e)
            need(r['b']*10**9+r['c']==before['a']*10**9+before['b']+250000000,'wrong-wait-deadline')
        if p=='attempt_summary':need(bool(r['a'])==(e in busy_epochs),'busy-coverage')
        if p=='wait_summary':need(bool(r['a'])==(e in wait_epochs),'wait-summary-coverage')
        last[e]=p;at+=1

def record_values(r,actor,job):
    p=r['phase'];e=r['epoch'];a,b,c=(r[k] for k in ('a','b','c'));n=job['n']
    allowed=(errno.EBUSY,) if p=='busy_first' else (0,errno.ETIMEDOUT) if p=='wrong_wait_return' else (0,)
    need(r['rc'] in allowed,'api-error')
    zero={'qos_set','wait_call','wait_return','target_acquired','target_unlocked','freeze_wait_call','wrong_wait_return','exit'}
    true={'held_publish','ready_publish','release_publish','release_receipt','owner_done_publish','owner_done_observed','withheld','freeze_observed','busy_first'}
    if p in zero:need((a,b,c)==(0,0,0),'value-'+p)
    elif p in true:need((a,b,c)==(1,0,0),'predicate-'+p)
    elif p=='start':need((a,b,c)==(0 if actor==0 else 1 if actor<=n else 2,0 if actor==0 else actor-1 if actor<=n else actor-n-1,0),'actor-role')
    elif p=='qos_get':need((a,b,c)==(9 if job['qos'] else 33,0,9 if job['qos'] else 33),'qos')
    elif p=='cpu':
        need(b==job['cpus'] and c==0,'cpu-domain')
        need(0<=a<job['cpus'] or (job['mode']=='placement' and actor==2 and e==2 and a==job['cpus']),'cpu-range')
    elif p=='held_observed':need((a,b,c)==(e,((1<<n)-1)&~1 if actor==0 else 1,0),'predicate-held_observed')
    elif p=='config':need((a,b,c)==(n,job['h'],job['strategy']),'config')
    elif p=='timebase':need(0<a<2**32 and 0<b<2**32 and c==0,'timebase')
    elif p in ('create','join'):need(1<=a<=n+job['h'] and b==c==0,'actor-target-'+p)
    elif p=='attempt':need((a,b,c)==(job['strategy'],0,0),'strategy')
    elif p=='attempt_summary':
        omitted=int(job['mode']=='missing-yield' and actor==2 and e==2)
        need((b,c)==((a-omitted,omitted) if job['strategy']==1 else (0,0)),'yield-accounting')
        if job['strategy']==2:need(a==0,'park-attempt-count')
    elif p=='data_publish':need((a,b,c)==(e-1 if job['mode']=='ordering' and e==2 else e,0,0),'published-data')
    elif p=='data_read':need((a,b,c)==(e-1 if job['mode']=='ordering' and e==2 else e,e,0),'read-data')
    elif p=='update':need((a,b,c)==(e+1,0,0),'update-count')
    elif p=='ack_publish':need((a,b,c)==(e-1 if job['mode']=='stale-ack' and actor==2 and e==2 else e,0,0),'ack-value')
    elif p=='ack_observed':need(b==(e-1 if job['mode']=='stale-ack' and a==1 and e==2 else e) and c==0,'ack-observation')
    elif p=='epoch_done':need((a,b,c)==(n,0,0),'epoch-workers')
    elif p=='wait_summary':need(a==b and c==0,'wait-accounting')
    elif p=='wrong_wait_clock':need(a<2**63 and b<10**9 and c==0,'wrong-wait-clock') # retained SDK CLOCK_REALTIME == 0
    elif p=='wrong_wait_call':need(a==1 and b<2**63 and c<10**9,'wrong-wait-input')
    elif p=='helper_work':need(c==0,'helper-fields')

def inspect(records,job,complete=False):
    """Validate available complete records before deriving any cause-specific result."""
    n=job['n'];q=9 if job['qos'] else 33;faults=[]
    for actor,rows in records.items():
        if not rows:need(not complete,'missing-actor');continue
        actor_prefix(rows,actor,job)
        need(rows[0]['phase']=='start','actor-start')
        for phase in ('start','qos_set','qos_get','exit'):
            need(len(select(rows,phase))<=1,'duplicate-'+phase)
        role=0 if actor==0 else 1 if actor<=n else 2
        index=0 if actor==0 else actor-1 if actor<=n else actor-n-1
        need(rows[0]['a']==role and rows[0]['b']==index,'actor-role')
        for r in rows:
            phase=r['phase'];e=r['epoch']
            category=0 if actor==0 else 1 if actor==1 else 2 if actor<=n else 3
            need(phase in ALLOWED[category],'actor-operation')
            allowed=(0,errno.EBUSY) if phase=='busy_first' else (0,errno.ETIMEDOUT) if phase=='wrong_wait_return' else (0,)
            need(r['rc'] in allowed,'api-error')
            if phase=='wait_summary':need(r['a']==r['b'] and r['c']==0,'wait-accounting')
            if phase=='qos_get':need(r['a']==r['c']==q and r['b']==0,'qos')
            if phase=='cpu' and not(0<=r['a']<job['cpus'] and r['b']==job['cpus']):faults.append(('placement',actor,e))
            if phase=='busy_first':need(r['rc']==errno.EBUSY and r['a']==1,'busy-evidence')
            if phase=='attempt':need(r['a']==job['strategy'],'strategy')
            if phase=='attempt_summary':
                need(actor>=2 and actor<=n,'summary-actor')
                if job['strategy']==1:
                    if r['b']!=r['a'] or r['c']!=0:faults.append(('missing-yield',actor,e))
                else:need(r['b']==r['c']==0,'non-yield')
                if r['a']:one(rows,'busy_first',e)
            if phase=='data_publish':need(r['a']==(e-1 if job['mode']=='ordering' and e==2 else e),'published-data')
            if phase=='data_read' and (r['a']!=e or r['b']!=e):faults.append(('ordering',actor,e))
            if phase=='update':need(r['a']==e+1,'update-count')
            if phase=='ack_observed' and r['b']!=e:faults.append(('stale-ack',int(r['a'])+1,e))
            if phase=='wrong_wait_return':
                receipt,before,call,ret,publish=ordered(rows,['release_receipt','wrong_wait_clock','wrong_wait_call','wrong_wait_return','data_publish'],e)
                need(call['a']==1 and before['b']<10**9 and call['c']<10**9,'wrong-wait-input')
                need(call['b']*10**9+call['c']==before['a']*10**9+before['b']+250000000,'wrong-wait-deadline')
                faults.append(('lost-wakeup',actor,e))
        for e in range(EPOCHS):
            for phase in PHASES-{'wait_call','wait_return','cpu','create','join','ack_observed'}:
                need(len(select(rows,phase,e))<=1,'duplicate-transition')
        # Every completed ordinary wait must have the immediately preceding
        # matching call in this actor's wait sequence. A killed prefix may end in a call.
        wait_events=[r for r in rows if r['phase'] in ('wait_call','wait_return')]
        for i,r in enumerate(wait_events):
            need(r['phase']==('wait_call' if i%2==0 else 'wait_return'),'wait-pair')
            if i%2:need(r['epoch']==wait_events[i-1]['epoch'],'wait-epoch')
        if complete:need(len(wait_events)%2==0 and len(select(rows,'exit'))==1,'actor-incomplete')
    ctl=records[0];owner=records[1]
    for joined in select(ctl,'join'):
        one(records[joined['a']],'exit',-1)
    # Final-only cross-actor evidence edges. Do not apply these to a live scan:
    # each file can advance between reads without a global snapshot lock.
    for e in range(EPOCHS):
        if select(ctl,'held_observed',e):
            one(owner,'held_publish',e)
            for actor in range(2,n+1):one(records[actor],'ready_publish',e)
        if select(owner,'release_receipt',e):one(ctl,'release_publish',e)
        if select(ctl,'owner_done_observed',e):one(owner,'owner_done_publish',e)
        for ack in select(ctl,'ack_observed',e):
            need(one(records[ack['a']+1],'ack_publish',e)['a']==ack['b'],'ack-observation')
        for actor in range(2,n+1):
            rows=records[actor]
            if select(rows,'held_observed',e):one(owner,'held_publish',e)
            if select(rows,'data_read',e):ordered(owner,['data_publish','target_unlocked'],e)
    finished=select(ctl,'epoch_done');need([r['epoch'] for r in finished]==list(range(len(finished))),'epoch-sequence')
    for done in finished:
        e=done['epoch'];need(done['a']==n,'epoch-workers')
        ordered(ctl,['held_observed','release_publish','owner_done_observed','epoch_done'],e)
        ordered(owner,['target_acquired','held_publish','release_receipt','data_publish','target_unlocked','owner_done_publish'],e)
        receipt=one(owner,'release_receipt',e)
        need(any(r['seq']<receipt['seq'] for r in select(owner,'wait_call',e)) and any(r['seq']<receipt['seq'] for r in select(owner,'wait_return',e)),'owner-wait-coverage')
        need(one(ctl,'held_observed',e)['a']==e and one(ctl,'held_observed',e)['b']==((1<<n)-1)&~1,'ready-observed')
        acknowledgements=select(ctl,'ack_observed',e);need([r['a'] for r in acknowledgements]==list(range(1,n)),'ack-coverage')
        for actor in range(2,n+1):
            rows=records[actor]
            ordered(rows,['held_observed','ready_publish','attempt','target_acquired','attempt_summary','data_read','update','target_unlocked','ack_publish'],e)
            need(one(rows,'held_observed',e)['a']==e,'held-epoch')
            # The controller's read of the slot is the independent publication
            # observation; a pre-store ack record alone does not complete an epoch.
            need(one(rows,'ack_publish',e)['a']==acknowledgements[actor-2]['b'],'ack-observation')
    if complete:
        cfg=one(ctl,'config',-1);need((cfg['a'],cfg['b'],cfg['c'])==(n,job['h'],job['strategy']),'config')
        tb=one(ctl,'timebase',-1);need(0<tb['a']<2**32 and 0<tb['b']<2**32,'timebase')
        need([r['a'] for r in select(ctl,'create')]==list(range(1,n+job['h']+1)),'create-coverage')
        need(len(finished)==EPOCHS,'epoch-coverage')
        need([r['a'] for r in select(ctl,'join')]==list(range(1,n+job['h']+1)),'join-coverage')
        for actor,rows in records.items():
            ordered(rows,['start','qos_set','qos_get','cpu'],-1)
            if 2<=actor<=n:
                for e in range(20):one(rows,'cpu',e)
            if actor>n:one(rows,'helper_work',-1)
            else:
                for e in range(20):
                    summary=one(rows,'wait_summary',e)
                    need(bool(summary['a'])==bool(select(rows,'wait_call',e)),'wait-summary-coverage')
    return faults

def validate(records,job,terminal,suffix):
    mode=job['mode'];full=mode in ('normal','missing-yield','lost-wakeup','placement')
    need(not any(terminal[k] for k in ('output_limited','disk_low','error')),'supervision-failure')
    need(terminal['returncode']==0 if full else terminal['returncode']!=0,'native-exit')
    if full:need(not terminal['timed_out'] and not terminal['no_progress'] and not any(suffix.values()),'incomplete')
    faults=inspect(records,job,complete=full)
    if mode=='normal':need(not faults,'normal-oracle');return {'status':'STRUCTURAL_PASS','timing':'unqualified','intervals_ticks':[dict(epoch=e,warmup=e<2,release_to_receipt=one(records[1],'release_receipt',e)['tick']-one(records[0],'release_publish',e)['tick'],receipt_to_unlock=one(records[1],'target_unlocked',e)['tick']-one(records[1],'release_receipt',e)['tick']) for e in range(EPOCHS)]}
    need(len(select(records[0],'epoch_done'))>=2,'control-warmups')
    if mode in ('held','frozen'):
        need(terminal['no_progress'] and terminal['kill_requested'] and terminal['returncode']==-signal.SIGKILL and not terminal['timed_out'] and not faults,'control-timeout')
        one(records[0],'held_observed',2)
        if mode=='held':
            one(records[0],'withheld',2);need(not select(records[0],'release_publish',2),'withheld-release')
            ordered(records[1],['target_acquired','held_publish','wait_call'],2)
            need(not select(records[1],'target_unlocked',2),'withheld-owner')
        else:
            ordered(records[0],['release_publish','owner_done_observed'],2)
            one(records[0],'freeze_observed',2);ordered(records[2],['held_observed','ready_publish','freeze_wait_call'],2)
            # The ordinary trace retains only its first wait pair per epoch.
            # It can precede held_observed; the distinct freeze call record and
            # controller's protected receipt prove the later frozen wait path.
            one(records[2],'wait_call',2)
            ordered(records[1],['release_receipt','target_unlocked'],2)
            need(not select(records[2],'attempt',2) and not select(records[2],'ack_publish',2),'frozen-peer')
    else:
        need(faults and all(f[0]==mode and f[2]==2 for f in faults),'wrong-control-oracle')
        if mode in ('missing-yield','stale-ack','placement'):need(all(f[1]==2 for f in faults),'control-victim')
        if mode=='lost-wakeup':need(faults==[(mode,1,2)],'control-owner')
        if mode=='missing-yield':
            first=one(records[2],'busy_first',2);summary=one(records[2],'attempt_summary',2)
            need(first['rc']==errno.EBUSY and summary['a']>0 and summary['c']==1 and summary['b']+1==summary['a'],'yield-exposure')
        if mode in ('stale-ack','ordering'):need(terminal['returncode']==2 and not terminal['timed_out'] and not terminal['no_progress'],'control-exit')
    if not full:
        need(len(select(records[0],'epoch_done'))==2,'control-completed-epochs')
        need(not any(r['epoch']>2 and r['phase'] not in WAIT for rows in records.values() for r in rows),'control-after-failure')
    return {'status':'EXPECTED_CONTROL_FAILURE','oracle':mode,'timing':'unqualified'}
