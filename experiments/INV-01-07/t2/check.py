"""M07-T2 proposed raw-first checker. No acceptance or clock qualification."""
import json
import sys
from fractions import Fraction
from pathlib import Path

N=2000
QOS=('user-interactive','background')
WAKE=('mach_wait_until','nanosleep','dispatch_source_timer_leeway0','dispatch_source_timer_strict')
EVENT=('pthread_cond_signal','dispatch_semaphore_signal','pthread_kill_SIGUSR1_spinning')

class Invalid(ValueError): pass

def require(ok,code):
    if not ok: raise Invalid(code)

def integer(n): return type(n) is int and 0<=n<2**64

def distribution(values):
    require(len(values)==N,'sample-count'); values=sorted(values)
    return dict(zip(('min','p50','p99','max'),(values[i] for i in (0,999,1979,1999))))

def quality(row,q):
    expected=33 if q=='user-interactive' else 9
    require(type(row) is list and len(row)==5 and all(type(x) is int for x in row),'qos-domain')
    require(row==[0,0,expected,expected,0],'qos-mismatch')

def read_rows(cell):
    rows=cell['rows'];require(type(rows) is list and len(rows)==N,'sample-count')
    for i,r in enumerate(rows):
        require(type(r) is list and len(r)==8 and all(integer(x) for x in r),'row-domain')
        require(r[0]==i,'row-id')
        if i:require(rows[i-1][6]<=r[4],'row-chronology')
    return rows

def validate(data):
    require(data['schema']=='M07-T2-raw-v1' and data['complete'] is True,'raw-schema')
    mode=data['mode'];require(mode in ('normal','dispatch-early','rtt-zero','cadence-duplicate'),'mode')
    require(type(data['run_id']) is str and len(data['run_id'])==32,'run-id')
    require(type(data['timebase']) is list and len(data['timebase'])==2 and all(integer(x) and 0<x<2**32 for x in data['timebase']),'timebase')
    tick=Fraction(*data['timebase']);cells=data['cells'];require(type(cells) is list,'cells')
    expected={('wakeup',m,q,t) for m in WAKE for q in QOS for t in (100000,1000000,10000000)}
    expected|={('cadence','mach_wait_until',q,n) for q in QOS for n in (2,4,8)}
    expected|={('event',m,q,0) for m in EVENT for q in QOS}
    if mode!='normal': expected={ {'dispatch-early':('wakeup',WAKE[3],QOS[0],100000),'rtt-zero':('event',EVENT[2],QOS[0],0),'cadence-duplicate':('cadence',WAKE[0],QOS[0],2)}[mode] }
    keys=[(c['kind'],c['mechanism'],c['qos'],c.get('target_ns',c.get('workers',0))) for c in cells]
    require(len(keys)==len(expected) and set(keys)==expected,'matrix')
    noise_p99=[]
    for name in ('calibration_pre','calibration_post'):
        noise=data[name];require(type(noise) is list and len(noise)==10000,'calibration-count')
        deltas=[]
        for i,row in enumerate(noise):
            require(type(row) is list and len(row)==3 and all(integer(x) for x in row),'calibration-domain')
            require(row[0]==i and row[2]>=row[1],'calibration-order')
            if i:require(noise[i-1][2]<=row[1],'calibration-chronology')
            deltas.append(row[2]-row[1])
        noise_p99.append(sorted(deltas)[9899])
    floor=max(100,20*max(noise_p99))*tick
    findings=[]
    for c in cells:
        kind,q=c['kind'],c['qos']
        if kind=='wakeup':
            quality(c['qos_record'],q);dispatch=c['mechanism'].startswith('dispatch')
            if dispatch:
                require(type(c['callback_qos']) is list and len(c['callback_qos'])==N,'callback-qos-coverage')
                for observation in c['callback_qos']:quality(observation,q)
            errors=[]
            for _,requested,deadline,token,begin,wake,end,events in read_rows(c):
                require(0<begin<=end and begin<=wake<=end,'wakeup-order')
                require(requested==int(begin*tick)+c['target_ns'],'requested-reference')
                if dispatch:
                    require(token not in (0,2**64-1) and abs(deadline-requested)<=tick,'dispatch-representation')
                    require(events==1,'dispatch-event-count')
                else:
                    require(token==0 and events==0,'non-dispatch-token')
                    if c['mechanism']=='mach_wait_until':
                        interval=(c['target_ns']*tick.denominator+tick.numerator-1)//tick.numerator
                        require(deadline==int((begin+interval)*tick),'mach-deadline')
                    else:require(deadline==requested,'nanosleep-request')
                error=Fraction(int(wake*tick)-deadline)
                require(error>=0,'dispatch-early' if dispatch else 'early-wake')
                errors.append(error)
            findings.append(dict(kind=kind,mechanism=c['mechanism'],qos=q,target_ns=c['target_ns'],error_ns=distribution(errors)))
        elif kind=='cadence':
            n=c['workers'];require(c['period_ns']==1000000 and integer(c['epoch_tick']),'cadence-configuration')
            require(len(c['threads'])==n and [w['id'] for w in c['threads']]==list(range(n)),'worker-coverage')
            workers=[]
            for w in c['threads']:
                quality(w['qos_record'],q);require(integer(w['start_tick']) and integer(w['end_tick']),'worker-domain')
                errors=[];late_phase=0;previous=None;gaps=[]
                for i,requested,deadline,token,begin,wake,end,events in read_rows(w):
                    ideal=(i+1)*1000000
                    offset=(ideal*tick.denominator+tick.numerator-1)//tick.numerator
                    require(deadline==int((c['epoch_tick']+offset)*tick) and requested==int(c['epoch_tick']*tick)+ideal,'cadence-deadline')
                    require(token==events==0 and w['start_tick']<=begin<=wake==end<=w['end_tick'],'cadence-order')
                    error=wake*tick-deadline;require(error>=0,'cadence-early');errors.append(error);late_phase+=error>=1000000
                    if previous is not None:gaps.append((wake-previous)*tick)
                    previous=wake
                interval=(previous-c['epoch_tick'])*tick;require(interval>0,'cadence-interval')
                workers.append(dict(id=w['id'],error_ns=distribution(errors),delivered=N,elapsed_ns=interval,
                    achieved_events_per_s=Fraction(N*10**9,interval),missed_phase_boundaries=late_phase,
                    max_delivery_gap_ns=max(gaps),median_error_below_250us=distribution(errors)['p50']<250000))
            findings.append(dict(kind=kind,qos=q,workers=workers))
        else:
            quality(c['sender_qos'],q);quality(c['target_qos'],q);roundtrips=[];oneway=[]
            for _,requested,deadline,token,begin,wake,end,events in read_rows(c):
                require(requested==deadline==token==0 and events==1,'event-row')
                require(end>begin,'rtt-positive')
                # Cross-thread differences remain diagnostics, not an integrity
                # proof/calibrated one-way estimate. Same-sender RTT is primary.
                roundtrips.append((end-begin)*tick);oneway.append((wake-begin)*tick)
            findings.append(dict(kind=kind,mechanism=c['mechanism'],qos=q,
                                 roundtrip_ns=distribution(roundtrips),oneway_diagnostic_ns=distribution(oneway)))
    require(mode=='normal','injection-not-detected')
    return {"cells":findings,"instrument_floor_ns":floor,"timing_qualification":"pending interval-specific findings review"}

def encode(v):
    if isinstance(v,Fraction):return {'numerator':v.numerator,'denominator':v.denominator}
    raise TypeError()

def load(path):
    def pairs(items):
        value={}
        for k,v in items:require(k not in value,'duplicate-key');value[k]=v
        return value
    return json.loads(Path(path).read_text(),object_pairs_hook=pairs,parse_constant=lambda _:(_ for _ in ()).throw(Invalid('nonfinite')))

if __name__=='__main__':
    try: output=json.dumps({'structural_findings':validate(load(sys.argv[1])),'evidence_approval':False},default=encode)
    except Exception as exc:print(json.dumps({'status':'INVALID','type':type(exc).__name__}));sys.exit(1)
    print(output)
