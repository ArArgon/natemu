"""M06-M1 v3 event/state/ownership checker; pure synthetic or retained input.

Log serialization is not cross-thread execution order: successful sends may
be logged after a worker resumes. Commit waits for their completion. Raw
ARM general-state words are compared in full; SDK-decoded PC/SP remain
explicit instrument outputs, not guessed authentication-bit masks.
"""
import collections
import copy
import json
import struct

ROUTINE_ID=2406
ARM_THREAD_STATE64_COUNT=68
SENTINEL=0x1864186418641864

class Invalid(Exception): pass

def parse(text):
    if not text or not text.endswith('\n'): raise Invalid('EMPTY_OR_TRUNCATED_TRACE')
    rows=[json.loads(line) for line in text.splitlines()]
    if not all(isinstance(r,dict) and isinstance(r.get('kind'),str) for r in rows):
        raise Invalid('RECORD_SHAPE')
    return rows

def by_kind(rows,kind): return [r for r in rows if r.get('kind')==kind]
def _rows_to_text(rows): return ''.join(json.dumps(r)+'\n' for r in rows)
def _mutate(rows,kind,**fields):
    rows=copy.deepcopy(rows)
    for r in by_kind(rows,kind): r.update(fields)
    return rows
def _remove(rows,kind): return [r for r in rows if r['kind']!=kind]
def _duplicate(rows,kind):
    rows=copy.deepcopy(rows); rows.append(copy.deepcopy(by_kind(rows,kind)[0])); return rows

def two_pass_check(text):
    try: return _check(parse(text))
    except (KeyError,TypeError,ValueError,IndexError,struct.error) as exc: return ['RECORD_FIELDS:'+str(exc)]

def _check(rows):
    fails=[]
    def require(ok,reason):
        if not ok: fails.append(reason)
    def one(items,reason):
        require(len(items)==1,reason)
        return items[0] if len(items)==1 else {}
    allowed={'meta','summary','register','arm','wire','receive','identity','repair','reply','send',
             'forward_call','resume','commit','disarm','snapshot','snapshot_tuple','disposal',
             'teardown_restore','restoration_proof','worker_parked','worker_exit','admission_schedule'}
    require(all(r['kind'] in allowed for r in rows),'UNKNOWN_RECORD')
    meta=one(by_kind(rows,'meta'),'META_COUNT')
    summary=one(by_kind(rows,'summary'),'SUMMARY_COUNT')
    require(meta.get('schema')=='M06-M1-trace-v3','TRACE_VERSION')
    n=meta['n']; total=meta['warmup']+meta['checked']; forwarded=meta['path']=='forward'
    require(n in (1,2,4,8) and total>0,'SAMPLE_PLAN')
    require(rows[0]['kind']=='meta' and rows[-1]['kind']=='summary','TRACE_BOUNDARY')
    require(not by_kind(rows,'terminal') and not by_kind(rows,'negative_trigger'),'TERMINAL_IN_POSITIVE')
    registrations={}
    for row in by_kind(rows,'register'):
        require(row['worker'] not in registrations,'DUPLICATE_REGISTER');registrations[row['worker']]=row
    require(set(registrations)==set(range(n)),'WORKERS_MISSING')
    event_kinds={'arm','wire','receive','identity','repair','reply','send','forward_call','resume','commit','disarm'}
    events=collections.defaultdict(list)
    for index,row in enumerate(rows):
        if row['kind'] in event_kinds: events[(row['worker'],row['event'])].append((index,row))
    expected={(w,e) for w in range(n) for e in range(total+int(forwarded))}
    require(set(events)==expected,'EVENT_COVERAGE')
    for (worker,event),indexed in events.items():
        local=[r for _,r in indexed]; proof=event==total
        reg=registrations[worker]
        arm=one(by_kind(local,'arm'),'ARM_COUNT')
        resume=one(by_kind(local,'resume'),'RESUME_COUNT')
        commit=one(by_kind(local,'commit'),'COMMIT_COUNT')
        disarm=one(by_kind(local,'disarm'),'DISARM_COUNT')
        require(arm.get('proof')==proof and arm.get('warmup')==(event<meta['warmup']),'EVENT_PHASE')
        require(arm['nonce']==event+1,'NONCE_SEQUENCE')
        require(arm['access']==meta['access'],'ACCESS_IDENTITY')
        positions={id(r):i for i,r in indexed}
        require(positions[id(arm)]<positions[id(resume)]<positions[id(commit)]<positions[id(disarm)],'EVENT_ORDER')
        expected_roles={'prior'} if proof else ({'front','prior'} if forwarded else {'front'})
        require({r['role'] for r in by_kind(local,'receive')}==expected_roles,'ROUTING_ROLES')
        for role in expected_roles:
            stage={k:one([r for r in local if r['kind']==k and r.get('role')==role],k.upper()+'_COUNT')
                   for k in ('wire','receive','identity','reply','send')}
            wire,received,identity,reply,sent=(stage[k] for k in ('wire','receive','identity','reply','send'))
            port=reg['prior_recv_port'] if role=='prior' else reg['recv_port']
            require(received['port']==port and port!=0,'RECEIVE_PORT')
            require(received['routine_id']==2406,'WRONG_ROUTINE_ID')
            require(received['exception']==1 and received['codeCnt']==2 and received['code']==[2,arm['address']],'FAULT_CLASS_ADDRESS')
            require(received['flavor']==6 and received['old_stateCnt']==68,'STATE_SHAPE')
            require(wire['size']==336 and wire['ndr_exact'] is True and wire['trailer_size']==8 and
                    wire['bits']&0x80000000==0 and wire['bits']&31==18 and wire['reply_right']!=0,'WIRE_ENVELOPE')
            raw=bytes.fromhex(wire['raw'])
            require(len(raw)==336 and raw[24:32]==b'\0\0\0\0\1\0\0\0','RAW_WIRE_LENGTH_NDR')
            bits,size,remote,local_port,voucher,routine=struct.unpack_from('<6I',raw)
            require((bits,size,remote,local_port,routine)==(wire['bits'],wire['size'],wire['reply_right'],port,2406),'RAW_WIRE_HEADER')
            exception,codes=struct.unpack_from('<2I',raw,32)
            code0,code1,flavor,count=struct.unpack_from('<2Q2I',raw,40)
            require((exception,codes,code0,code1,flavor,count)==(1,2,2,arm['address'],6,68),'RAW_WIRE_FIELDS')
            require(list(struct.unpack_from('<68I',raw,64))==received['state'],'RAW_STATE_DECODE')
            state=received['state']
            require(len(state)==68 and all(type(x) is int and 0<=x<2**32 for x in state),'STATE_WORDS')
            def reg64(index): return state[2*index]|(state[2*index+1]<<32)
            require(identity['valid'] is True,'IDENTITY_REJECTED')
            require(identity['pc']==arm['expected_pc'],'IDENTITY_PC')
            require(identity['pc']==reg64(32) and identity['sp']==reg64(31),'RAW_PC_SP')
            require(arm['sp_lo']<=identity['sp']<arm['sp_hi'],'IDENTITY_SP')
            require(identity['cookie']==reg64(9)==reg64(19)==arm['cookie'],'IDENTITY_COOKIE')
            require(identity['nonce']==reg64(10)==reg64(20)==arm['nonce'],'IDENTITY_NONCE')
            require(reg64(21)==SENTINEL and reg64(22)==identity['sp'],'DELIVERED_SENTINELS')
            require(reply['kr']==0 and reply['flavor']==6 and reply['new_stateCnt']==68 and reply['state']==state,'CHANGED_GENERAL_STATE')
            require(sent['kr']==0 and sent['disposition']=='move-send-once','SEND_COMPLETION')
            require(positions[id(arm)]<positions[id(wire)]<positions[id(received)]<positions[id(identity)]<positions[id(reply)]<positions[id(resume)],'SERVER_ORDER')
            require(positions[id(reply)]<positions[id(sent)]<positions[id(commit)],'SEND_BEFORE_COMMIT')
            repair_rows=[r for r in local if r['kind']=='repair' and r['role']==role]
            if role=='prior' or not forwarded:
                repair=one(repair_rows,'REPAIR_COUNT')
                require(repair['mprotect_result']==repair['errno']==0 and repair['skipped'] is False and
                        repair['address']==arm['address'] and repair['length']==arm['page_size'],'REPAIR_RESULT')
                require(positions[id(identity)]<positions[id(repair)]<positions[id(reply)],'REPAIR_ORDER')
            else: require(not repair_rows,'FRONT_REPAIRED_FORWARDED')
        forwards=by_kind(local,'forward_call')
        if forwarded and not proof:
            fwd=one(forwards,'FORWARD_COUNT')
            prior=next(r for r in local if r['kind']=='reply' and r['role']=='prior')
            require(fwd['prior_kr']==0 and fwd['prior_flavor']==6 and fwd['prior_stateCnt']==68 and fwd['state']==prior['state'],'FORWARD_STATE')
            front_identity=next(r for r in local if r['kind']=='identity' and r['role']=='front')
            prior_wire=next(r for r in local if r['kind']=='wire' and r['role']=='prior')
            front_reply=next(r for r in local if r['kind']=='reply' and r['role']=='front')
            require(positions[id(front_identity)]<positions[id(prior_wire)]<positions[id(prior)]<positions[id(fwd)]<positions[id(front_reply)],'FORWARD_ORDER')
        else: require(not forwards,'UNEXPECTED_FORWARD')
        require(resume['data_ok'] is True and resume['data']==arm['expected_data'],'RESUMED_DATA')
        require(resume['sentinels_ok'] is True and resume['cookie']==arm['cookie'] and resume['nonce']==arm['nonce'] and resume['sentinel']==SENTINEL,'RESUMED_SENTINELS')
        require(resume['sp_ok'] is True and resume['sp_before']==resume['sp_after'] and arm['sp_lo']<=resume['sp_before']<arm['sp_hi'],'RESUMED_SP')
    # Snapshot rows own one acquired reference per non-null tuple. Compare
    # semantic mask/handler/behavior/flavor, then require exact successful disposal.
    snapshots={}; disposals=collections.Counter();expected_disposals=set()
    for row in by_kind(rows,'snapshot'):
        key=row['worker'],row['label'];require(key not in snapshots,'DUPLICATE_SNAPSHOT')
        tuples=[r for r in rows if r['kind']=='snapshot_tuple' and (r['worker'],r['label'])==key]
        require(len(tuples)==row['count'] and {r['index'] for r in tuples}==set(range(row['count'])),'SNAPSHOT_TUPLES')
        snapshots[key]=tuples
        for t in tuples:
            if t['port']:
                right=row['label']+'-'+str(t['index'])
                expected_disposals.add((row['worker'],right))
                for disposed in by_kind(rows,'disposal'):
                    if (disposed['worker'],disposed['right'])==(row['worker'],right):
                        require(rows.index(t)<rows.index(disposed),'SNAPSHOT_DISPOSED_BEFORE_INSPECTION')
    def semantic(key):
        result={}
        for t in snapshots[key]:
            for bit in range(32):
                if t['mask']&(1<<bit):
                    require(bit==1,'UNRELATED_MASK')
                    result[bit]=(t['port'],t['behavior'],t['flavor']) if t['port'] else (0,0,0)
        return result.get(1,(0,0,0))
    require(semantic((-1,'task-initial'))==semantic((-1,'task-final')),'TASK_PORT_CHANGE')
    for w in range(n):
        require(semantic((w,'initial'))==(0,0,0),'UNKNOWN_PRIOR')
        require(semantic((w,'initial'))==semantic((w,'restored-initial')),'RESTORATION_MISMATCH')
        labels=['restored-initial']
        if forwarded:
            require(semantic((w,'saved-prior'))==semantic((w,'restored-prior'))==(registrations[w]['prior_recv_port'], -2147483646,6),'PRIOR_RESTORATION')
            labels+=['restored-prior']
            proof=one([r for r in rows if r['kind']=='restoration_proof' and r['worker']==w],'RESTORATION_PROOF_COUNT')
            require(proof['event']==total,'RESTORATION_PROOF_EVENT')
        parked=one([r for r in rows if r['kind']=='worker_parked' and r['worker']==w],'WORKER_PARKED_COUNT')
        exited=one([r for r in rows if r['kind']=='worker_exit' and r['worker']==w],'WORKER_EXIT_COUNT')
        require(parked['completed']==total and parked['disarmed'] is True,'PARKED_STATE')
        last_disarm=next(r for r in rows if r['kind']=='disarm' and r['worker']==w and r['event']==total-1)
        require(rows.index(last_disarm)<rows.index(parked),'PARKED_BEFORE_COMPLETION')
        initial_restore=next(r for r in rows if r['kind']=='teardown_restore' and r['worker']==w and r['label']=='restored-initial')
        for disposed in by_kind(rows,'disposal'):
            if disposed['worker']!=w: continue
            fixed=disposed['right'] in ('send','receive','thread_self','data_page','prior_send','prior_receive')
            if fixed or disposed['right'].startswith(('initial-','saved-prior-')):
                require(rows.index(initial_restore)<rows.index(disposed)<rows.index(exited),'DISPOSAL_LIFETIME')

        for label in labels:
            restore=one([r for r in rows if r['kind']=='teardown_restore' and r['worker']==w and r['label']==label],'RESTORE_COUNT')
            require(restore['alive'] is True and restore['disarmed'] is True and restore['semantic_equal'] is True,'RESTORATION_MISMATCH')
            require(rows.index(parked)<rows.index(restore)<rows.index(exited),'RESTORE_LIFETIME')
        if forwarded:
            restored=next(r for r in rows if r['kind']=='teardown_restore' and r['worker']==w and r['label']=='restored-prior')
            proof_arm=next(r for r in rows if r['kind']=='arm' and r['worker']==w and r['event']==total)
            proof_disarm=next(r for r in rows if r['kind']=='disarm' and r['worker']==w and r['event']==total)
            require(rows.index(restored)<rows.index(proof_arm)<rows.index(proof_disarm)<rows.index(proof)<rows.index(initial_restore),'RESTORED_EVENT_ORDER')
        for right in ('send','receive','thread_self','data_page')+ (('prior_send','prior_receive') if forwarded else ()):
            expected_disposals.add((w,right))
        local=[r for r in rows if r.get('worker')==w and r['kind'] in ('arm','disarm')]
        require([r['kind'] for r in local]==['arm','disarm']*(total+int(forwarded)),'DOUBLE_OUTSTANDING_FAULT')
    if meta['topology']=='serial':expected_disposals.add((-1,'port_set'))
    if meta['jit']:expected_disposals.add((-1,'jit_page'))
    for row in by_kind(rows,'disposal'):
        disposals[row['worker'],row['right']]+=1;require(row['kr']==0,'DISPOSAL_FAILURE')
    require(set(disposals)==expected_disposals and all(v==1 for v in disposals.values()),'UNRELEASED_OR_DOUBLE_RIGHT')
    require(summary['status']=='completed' and summary['zero_unclassified'] is True and summary['events_checked']==n*meta['checked'] and summary['events_warmup']==n*meta['warmup'] and summary['proof_events']==n*int(forwarded) and summary['commits_total']==len(events),'SUMMARY_COUNTS')
    return fails


def good_trace_rows(worker=0,event=0):
    """Small valid direct fixture; no native fault, port, executable mapping."""
    assert worker==event==0
    state=[0]*68
    for reg,value in {9:1,10:1,19:1,20:1,21:SENTINEL,22:8192,31:8192,32:16384}.items():state[2*reg:2*reg+2]=[value&0xffffffff,value>>32]
    rows=[dict(kind='meta',schema='M06-M1-trace-v3',investigation='INV-01-06-M1',n=1,jit=0,access='load',topology='serial',path='direct',seed=60601,warmup=0,checked=1,negative='',arm='',routine_id=2406)]
    for w,label in [(-1,'task-initial'),(0,'initial')]:rows.append(dict(kind='snapshot',worker=w,label=label,count=0))
    rows.append(dict(kind='register',worker=0,recv_port=100,prior_recv_port=0,published=True))
    common=dict(worker=0,event=0); role=dict(common,role='front')
    rows += [dict(common,kind='arm',proof=False,warmup=False,address=4096,page_size=4096,access='load',cookie=1,nonce=1,expected_pc=16384,sp_lo=4096,sp_hi=12288,expected_data=17),
      dict(role,kind='wire',size=336,bits=18,ndr_exact=True,trailer_size=8,reply_right=200),
      dict(role,kind='receive',port=100,routine_id=2406,exception=1,codeCnt=2,code=[2,4096],flavor=6,old_stateCnt=68,state=state),
      dict(role,kind='identity',pc=16384,sp=8192,cookie=1,nonce=1,valid=True),
      dict(role,kind='repair',address=4096,length=4096,mprotect_result=0,errno=0,skipped=False),
      dict(role,kind='reply',kr=0,flavor=6,new_stateCnt=68,state=state),
      dict(common,kind='resume',data_ok=True,data=17,sentinels_ok=True,cookie=1,nonce=1,sentinel=SENTINEL,sp_ok=True,sp_before=8192,sp_after=8192),
      dict(role,kind='send',kr=0,disposition='move-send-once'),
      dict(common,kind='commit'),dict(common,kind='disarm'),
      dict(kind='worker_parked',worker=0,completed=1,disarmed=True),
      dict(kind='snapshot',worker=0,label='restored-initial',count=0),
      dict(kind='teardown_restore',worker=0,label='restored-initial',semantic_equal=True,alive=True,disarmed=True)]
    for right in ('send','receive','thread_self','data_page'):rows.append(dict(kind='disposal',worker=0,right=right,kr=0))
    rows += [dict(kind='worker_exit',worker=0),dict(kind='disposal',worker=-1,right='port_set',kr=0),dict(kind='snapshot',worker=-1,label='task-final',count=0),dict(kind='summary',events_checked=1,events_warmup=0,commits_total=1,proof_events=0,zero_unclassified=True,status='completed')]
    received=by_kind(rows,'receive')[0];wire=by_kind(rows,'wire')[0]
    raw=struct.pack('<6I',18,336,200,100,0,2406)+b'\0\0\0\0\1\0\0\0'+struct.pack('<2I2Q2I',1,2,2,4096,6,68)+struct.pack('<68I',*received['state'])
    wire['raw']=raw.hex()
    return rows


def demo():
    good=good_trace_rows();assert two_pass_check(_rows_to_text(good))==[]
    cases={'summary-only':[good[0],good[-1]],'missing delivery':_remove(good,'receive'),'duplicate commit':_duplicate(good,'commit'),'invalid identity':_mutate(good,'identity',valid=False),'failed disposal':_mutate(good,'disposal',kr=5),'bad resumed state':_mutate(good,'resume',sp_ok=False,sentinels_ok=False)}
    for kind,field,value in [('identity','pc',0),('identity','sp',0),('identity','cookie',0),('identity','nonce',0),('receive','port',999),('receive','code',[1,4096]),('receive','old_stateCnt',67),('receive','flavor',5),('wire','ndr_exact',False),('wire','size',335),('wire','trailer_size',0),('reply','state',[0]*68),('send','kr',5),('teardown_restore','alive',False)]:
        cases[kind+field]=_mutate(good,kind,**{field:value})
    for moved,target in [('arm','resume'),('disposal','arm'),('worker_parked','arm')]:
        bad=copy.deepcopy(good);row=next(r for r in bad if r['kind']==moved);bad.remove(row)
        bad.insert(next(i for i,r in enumerate(bad) if r['kind']==target),row)
        cases['misordered '+moved]=bad
    for name,bad in cases.items():assert two_pass_check(_rows_to_text(bad)),name
    print('synthetic checker:',len(cases),'negative fixtures and one valid trace passed; no host evidence')

if __name__=='__main__':demo()
