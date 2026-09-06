"""M21-1 pure schedule/phase rules and streaming retained-evidence checker."""
import json
from fractions import Fraction
import math
from pathlib import Path

ARMS={'T1':1800,'T2':900,'T3a':600,'T3b':600,'T4':1200,'T5':300,'C1':300,'C2':300}
CAP=32768
U64=2**64-1

def need(test,reason):
    if not test:raise ValueError(reason)

def schedule():
    return [dict(index=i,arm=a,n=n,seed=seed,window=ARMS[a]) for i,(a,n,seed) in
            enumerate((a,n,s) for a in ARMS for n in (1,2,4,8) for s in (21001,21002,21003))]

def integer(v):return type(v) is int and 0<=v<=U64

def decode(line):
    def unique(pairs):
        out={}
        for k,v in pairs:need(k not in out,'duplicate-key');out[k]=v
        return out
    return json.loads(line,object_pairs_hook=unique,parse_constant=lambda _:(_ for _ in ()).throw(ValueError('nonfinite')))

class Phases:
    """All times injected supervisor awake seconds; no clock reads in this class.

    Receipt() and advance() are serviced serially by the parent. Deadlines close
    before receipt at the same/later instant. Reservations are never inputs.
    """
    def __init__(self,start,window=1800):
        self.end=start+window;self.phase='pre';self.deadline=min(start+120,self.end)
        self.pre=[];self.target=[];self.post=[];self.late=[];self.confirmed=False
        self.association=True;self.bucket=None;self.control_invalid=False;self.complete=False
        self.announcements=[('pre',start)]

    def change(self,phase,now):
        self.phase=phase;self.announcements.append((phase,now))

    def take_announcements(self):
        result=self.announcements;self.announcements=[];return result

    def advance(self,now):
        if self.complete:return
        if now>=self.end:
            self.bucket='unresolved' if self.bucket is None else self.bucket
            self.change('window-ended',now);self.complete=True;return
        if now<self.deadline:return
        if self.phase=='pre':
            if self.pre!=[2,3]:self.control_invalid=True;self.complete=True;self.change('pre-invalid',now)
            else:self.change('await-induction',now);self.deadline=self.end
        elif self.phase=='target':
            self.bucket=target_bucket(self.target,self.association)
            self.change('post',now);self.deadline=min(now+120,self.end)
        elif self.phase=='post':
            self.control_invalid=self.post!=[2,3];self.complete=True;self.change('post-complete',now)

    def receipt(self,kind,now,source=None,confirmed=False,associated=True):
        self.advance(now)
        if kind=='notification':
            if self.phase=='pre' and source in (2,3):self.pre.append(source)
            elif self.phase in ('induction','target') and source in (0,1):self.target.append(source)
            elif self.phase=='post' and source in (2,3):self.post.append(source)
            elif source in (0,1):self.late.append(source)
        elif kind=='induction-start':
            need(self.phase=='await-induction','induction-before-valid-control')
            self.change('induction',now);self.deadline=self.end
        elif kind=='wake-confirmed':
            need(self.phase=='induction','wake-phase')
            self.confirmed=confirmed;self.association=associated
            self.change('target',now);self.deadline=min(now+60,self.end)

    def result(self,registered=True,drained=True):
        if not registered or not drained:return 'instrument-invalid'
        if self.control_invalid:return 'invalid-screen-control'
        if self.phase!='post-complete' or not self.confirmed:return 'unresolved'
        return self.bucket if self.association else 'unresolved'

def target_bucket(values,associated=True):
    if not associated:return 'unresolved'
    if values==[0,1]:return 'bracketed'
    if not values:return 'notification-not-observed-in-window'
    if values in ([0],[1]):return 'incomplete-pair'
    return 'ambiguous'

def budgets(job,awake,continuous,campaign,files,slot_bytes,total_bytes,free):
    if campaign>=36*3600:return 'campaign-time'
    if max(files,default=0)>=256*1024**2:return 'file-output'
    if slot_bytes>=512*1024**2:return 'slot-output'
    if total_bytes>=8*1024**3:return 'campaign-output'
    if free<2*1024**3:return 'free-space'
    if awake>=job['window']+300:return 'awake-timeout'
    if continuous>=job['window']+300+(900 if job['arm']=='T1' else 0):return 'continuous-timeout'
    return None

def stop_reason(results):
    if any(r.get('fatal') for r in results):return 'fatal-outcome'
    if sum(r.get('detectability')=='invalid-screen-control' for r in results)>=2:return 'second-screen-control'
    for arm in ARMS:
        if sum(r.get('arm')==arm and r.get('status')=='timed-out' for r in results)>2:return 'third-arm-timeout'
    return None

def witness(value,label):
    need(type(value) is list and len(value)==7 and all(integer(x) for x in value) and value[6]==0,label)
    reads,reversals,before,after,first,last,_=value
    need(reversals<=max(0,reads-1),label+'-count')
    if reversals:need(reads>=2 and before>after and first<=last<=first+1 and last<reads,label+'-first-pair')
    else:need(value[2:6]==[0,0,0,0],label+'-unexpected-first-pair')
    return value

def trace(path,job,run_id,build_id,window_start=0,window_end=float('inf'),transition_bounds=(),demote_start=None):
    """Stream rows; private data is admissible only with complete footer/drain."""
    last={};selected={};first={};counts={};summaries={};gaps={};background_seen=set();decreases={};first_pairs={};overlap=0;unexplained=0;clock_back=[False]*5;header=None;footer=None;notification_keys=set()
    # Preserve the supervisor's recorded floating boundary exactly; do not
    # additionally round each integer-ns sample when assigning it to a window.
    start_ns=Fraction(window_start)*10**9;end_ns=U64 if window_end==float('inf') else Fraction(window_end)*10**9
    request_ns=None if demote_start is None else max(start_ns,Fraction(demote_start)*10**9)
    bounds_ns=[(Fraction(begin)*10**9,Fraction(end)*10**9) for begin,end in transition_bounds]
    with Path(path).open('rb') as f:
        for line in f:
            need(len(line)<=16384 and line.endswith(b'\n'),'truncated-or-large-record')
            r=decode(line)
            if header is None:
                header=r;need(r.get('schema')=='M21-1-trace-v1' and r.get('run_id')==run_id and r.get('build_id')==build_id and r.get('n')==job['n'] and r.get('arm')==job['arm'],'trace-identity')
                n,d=r['timebase'];need(integer(n) and integer(d) and n>0 and d>0,'timebase');need(0<r['record_size']<=128,'layout');continue
            need(footer is None,'after-footer')
            if 'row' in r:
                v=r['row'];need(type(v) is list and len(v)==18 and all(type(x) is int for x in v),'row-shape')
                a,seq,counter,*_=v;need(0<=a<job['n']+9 and a not in summaries,'actor-order');need(seq==counts.get(a,0) and seq<CAP,'sequence')
                need(all(integer(x) for x in v[2:8]) and v[13]==0 and v[10]==0 and v[16]>0,'record-clock-or-status')
                if a<=job['n']:
                    need(v[8] in (25,9) and v[9]==0 and v[11]==v[8] and v[12]==0,'qos')
                    if v[8]==9 and request_ns is not None and request_ns<=v[7]<=end_ns:background_seen.add(a)
                    need(v[14]==(-2 if a==job['n'] else -1),'actor-source')
                    if a==job['n']:need(counter==seq+1,'timer-fire-counter')
                    elif seq==0:need(counter==0,'worker-initial-counter')
                    decreases.setdefault(a,[0]*5);first_pairs.setdefault(a,[None]*5)
                    if a in last:
                        old=last[a];need(counter>old[2] and v[16]==old[16],'counter-progress-or-actor-identity')
                        for k in range(5):
                            if v[3+k]<old[3+k]:
                                clock_back[k]=True;decreases[a][k]+=1
                                if first_pairs[a][k] is None:first_pairs[a][k]=[old[3+k],v[3+k],old[1],seq]
                    if start_ns<=v[7]<=end_ns:
                        first.setdefault(a,v)
                        if a in selected:
                            old=selected[a]
                            if v[4]>=old[4] and (v[4]-old[4])*n//d>300000000:
                                gaps[a]=gaps.get(a,0)+1
                                if any(old[7]<=begin<=end<=v[7] for begin,end in bounds_ns):overlap+=1
                                else:unexplained+=1
                        selected[a]=v
                else:
                    need(v[14]==a-job['n']-1,'notification-source')
                    notification_keys.add((v[14],seq,v[17]))
                counts[a]=seq+1;last[a]=v
            elif 'actor' in r:
                a=r['actor'];need(integer(a) and a<job['n']+9 and a not in summaries,'actor-summary')
                need(r['reserved']==counts.get(a,0) and r['reserved']<=CAP and r['dropped']==0 and r['errors']==0,'capacity-or-api-error')
                w=witness(r['continuous'],'continuous-witness')
                need(type(r['clocks']) is list and len(r['clocks'])==5,'clock-witnesses')
                if a<=job['n']:
                    count=counts.get(a,0);need(count>0,'missing-actor-records')
                    need(integer(r['interruptions']),'interruption-count')
                    if a==job['n']:
                        minimum=1+3*count+2*r['interruptions'];need(minimum<=w[0]<=minimum+1,'timer-pre-gate-coverage');last_index=count
                    else:
                        need(r['interruptions']==0 and w[0]>=1+count+last[a][2],'worker-pre-gate-coverage');last_index=w[0]-count-1
                    need(w[1]>=decreases[a][1] and (not w[1] or w[5]<=last_index),'continuous-witness-coverage')
                    if w[1]:clock_back[1]=True
                    for k,c in enumerate(r['clocks']):
                        witness(c,'row-clock-witness');need(c[0]==count and c[1]==decreases[a][k],'row-clock-witness-coverage')
                        if c[1]:need(c[2:6]==first_pairs[a][k],'row-clock-first-pair');clock_back[k]=True
                else:
                    need(w==[0]*7 and all(witness(c,'notification-witness')==[0]*7 for c in r['clocks']),'unordered-notification-witness')
                summaries[a]=r
            else:need(r.get('complete') is True,'unexpected-record');footer=r
    need(header is not None and footer is not None and footer.get('drained') is True and footer.get('joined')==job['n']+1 and not footer.get('invalid'),'missing-or-invalid-terminal')
    need(set(summaries)==set(range(job['n']+9)),'missing-actor-summary')
    need(type(footer.get('reversal')) is bool and footer['reversal']==any(summaries[a]['continuous'][1]>0 for a in range(job['n']+1)),'continuous-reversal-summary')
    need(header.get('backend')=='nanosleep-relative' and header.get('clock')=='API-defined' and header.get('leeway')=='not-exposed','timer-contract')
    resolutions=header['resolutions'];need(len(resolutions)==5 and all(integer(v) for v in resolutions),'resolutions')
    if clock_back[1]:gaps={};overlap=unexplained=0
    categories=[]
    for k in range(5):
        if clock_back[k]:categories.append('backward');continue
        deltas=[(selected[a][3+k]-first[a][3+k])*(n if k<2 else 1) for a in selected if selected[a][1]>first[a][1]]
        threshold=resolutions[k]*(d if k<2 else 1)
        categories.append('advanced' if len(deltas)==job['n']+1 and all(v>threshold for v in deltas) else 'equal-without-live-observer' if deltas and all(v==0 for v in deltas) else 'insufficient')
    return dict(clock_backward=clock_back,continuous=categories[1],clock_categories=categories,gaps=gaps,transition_overlap=overlap,unexplained_gaps=unexplained,
                progress='unavailable' if clock_back[1] else 'diagnostic-gap' if gaps else 'no-observed-gap',
                frozen='unreachable',summaries=summaries,notification_keys=notification_keys,background_seen=background_seen)

def classify(directory,job,run_id,build_id,terminal,events):
    base=dict(arm=job['arm'],timing='unqualified',frozen='unreachable',completed_question=False)
    if terminal.get('returncode')!=0 or terminal.get('killed') or terminal.get('timeout'):
        return dict(base,status='timed-out' if terminal.get('timeout') else 'instrument-invalid',missing_in_memory_trace=terminal.get('missing_in_memory_trace',True),
                    finding='none',fatal=not terminal.get('timeout',False),reason=terminal.get('reason','child-exit'))
    try:
        need(all(e.get('ordinal')==i and type(e.get('awake')) in (int,float) and math.isfinite(e['awake']) and e['awake']>=0 for i,e in enumerate(events)),'receipt-order')
        need(all(a['awake']<=b['awake'] for a,b in zip(events,events[1:])),'receipt-clock-order')
        armed=[e for e in events if e['event']=='armed'];need(len(armed)==1,'armed')
        def one(kind):
            found=[e for e in events if e['event']==kind];need(len(found)==1,'lifecycle-'+kind);return found[0]
        window_start=armed[0]['awake'];window_end=window_start+job['window']
        requests=[e for e in events if e['event']=='demote-request']
        demote_start=requests[0]['awake'] if len(requests)==1 else None
        starts=[e['awake'] for e in events if e['event']=='induction-start'];wakes=[e['awake'] for e in events if e['event']=='wake-confirmed' and e.get('confirmed') and e.get('associated')]
        bounds=[(starts[0],wakes[0])] if len(starts)==len(wakes)==1 else []
        raw=trace(Path(directory)/'trace.jsonl',job,run_id,build_id,window_start,window_end,bounds,demote_start)
        registered=[e for e in events if e['event']=='registered'];need(len(registered)==1 and registered[0].get('sources')==list(range(8)),'registration')
        need(0<registered[0].get('owned_bytes',0)<=128*1024**2,'allocation-account')
        main=[e for e in events if e['event']=='main-qos'];need(len(main)==1 and all(main[0].get(k)==v for k,v in {'requested':25,'set':0,'get':0,'got':25,'relative':0}.items()),'main-qos')
        received=[(e['source'],e['seq'],e['receipt']) for e in events if e['event']=='notification']
        need(len(received)==len(set(received)) and set(received)==raw.pop('notification_keys'),'receipt-raw-association')
        need([v[2] for v in received]==list(range(len(received))),'notification-receipt-order')
        need(sum(e['event']=='complete' for e in events)==1 and sum(e['event']=='admission-closed' and e.get('removed')==list(range(8)) for e in events)==1,'drain-registration')
        closed=one('admission-closed');complete=one('complete')
        need(main[0]['ordinal']<registered[0]['ordinal']<armed[0]['ordinal']<closed['ordinal']<complete['ordinal'],'lifecycle-order')
        need(all(registered[0]['ordinal']<e['ordinal']<complete['ordinal'] for e in events if e['event']=='notification'),'notification-lifetime-order')
        need(all(e['ordinal']>complete['ordinal'] for e in events if e['event']=='exit'),'exit-order')
        endings=[e for e in events if e['event']=='normal-stop'];early=[e for e in events if e['event']=='pre-control-stop']
        reversals=[e for e in events if e['event']=='reversal']
        need(len(endings)<=1 and len(early)<=1 and len(reversals)<=1 and not(endings and early),'stop-cardinality')
        stops=endings+early+reversals
        need(all(armed[0]['ordinal']<e['ordinal']<closed['ordinal'] for e in stops),'stop-lifecycle-order')
        need((len(endings)==1 and endings[0]['awake']>=window_end) or (job['arm']=='T1' and len(early)==1) or (raw['clock_backward'][1] and len(reversals)==1),'observation-window')
        need(not reversals or raw['clock_backward'][1],'unsupported-reversal-receipt')
        arm=job['arm'];detect='not-applicable';fatal=any(raw['clock_backward'])
        if arm=='T1':
            p=Phases(armed[0]['awake'],job['window'])
            for e in events:
                if e['ordinal']<=armed[0]['ordinal']:continue
                if e['event'] in ('notification','induction-start','wake-confirmed'):p.receipt(e['event'],e['awake'],e.get('source'),e.get('confirmed',False),e.get('associated',False))
                else:p.advance(e['awake'])
            detect=p.result()
            if early:need(p.phase=='pre-invalid' and early[0]['awake']>=window_start+120,'early-control-stop')
        elif arm=='T5':
            request=one('demote-request');published=one('demote-published')
            need(armed[0]['ordinal']<request['ordinal']<published['ordinal']<closed['ordinal'] and window_start<=request['awake']<=published['awake']<window_end,'demotion-publication-window')
            need(all(raw['summaries'][a]['demoted'] for a in range(job['n']+1)) and raw['background_seen']==set(range(job['n']+1)),'self-qos-control');detect='self-request-acknowledged'
        elif arm=='C1':
            request=one('activity-request');begin=one('activity-begin');end=one('activity-end')
            need(armed[0]['ordinal']<request['ordinal']<begin['ordinal']<closed['ordinal']<end['ordinal']<complete['ordinal'] and begin.get('token') is True and begin['awake']<window_end,'activity-control');detect='self-request-ended'
        elif arm=='T2':detect='not-measured';need(registered[0].get('frontmost_self')==0,'background-launch-unverified')
        elif arm in ('T3a','T3b','T4'):
            confirmations=[e for e in events if e['event']=='transition-confirmed' and e.get('confirmed') and e.get('associated')]
            detect='diagnostic-observation' if len(confirmations)==1 else 'not-measured'
        raw.pop('summaries');raw.pop('background_seen');status='completed'
        if detect in ('not-measured','unresolved'):status='not-measured'
        if detect=='invalid-screen-control':status='instrument-invalid'
        unsolicited=[e for e in events if e['event']=='notification']
        if arm=='C2' and unsolicited:status='mixed-condition'
        if any(e['event']=='unintended-transition' and e.get('confirmed') for e in events):status='mixed-condition'
        return dict(base,**raw,status=status,detectability=detect,fatal=fatal,missing_in_memory_trace=False)
    except (ValueError,KeyError,TypeError,OSError) as exc:
        return dict(base,status='instrument-invalid',reason=str(exc),fatal=True,finding='none')
