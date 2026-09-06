"""Fabricated complete state/ownership traces. Never host evidence."""
import copy
import struct
from check import good_trace_rows, by_kind


def valid_trace(n=1,path='direct',topology='serial',access='load',total=1):
    template=good_trace_rows();meta=copy.deepcopy(template[0]);meta.update(n=n,path=path,topology=topology,access=access,checked=total)
    rows=[meta,dict(kind='snapshot',worker=-1,label='task-initial',count=0)]
    def snapshot(w,label,port=0):
        out=[dict(kind='snapshot',worker=w,label=label,count=int(bool(port)))]
        if port:out.append(dict(kind='snapshot_tuple',worker=w,label=label,index=0,mask=2,port=port,behavior=-2147483646,flavor=6))
        return out
    for w in range(n):
        rows+=snapshot(w,'initial')
        if path=='forward':rows+=snapshot(w,'saved-prior',200+w)
        rows.append(dict(kind='register',worker=w,recv_port=100+w,prior_recv_port=200+w if path=='forward' else 0,published=True))
    def event(w,e,proof=False):
        base={k:copy.deepcopy(by_kind(template,k)[0]) for k in ('arm','wire','receive','identity','repair','reply','send','resume','commit','disarm')}
        for r in base.values():r.update(worker=w,event=e)
        cookie=w+1;nonce=e+1;address=4096*(w+1);pc=16384 if access=='load' else 16388;sp=8192
        arm=base['arm'];arm.update(proof=proof,access=access,address=address,cookie=cookie,nonce=nonce,expected_pc=pc)
        base['identity'].update(cookie=cookie,nonce=nonce,pc=pc)
        base['resume'].update(cookie=cookie,nonce=nonce)
        state=base['receive']['state']
        for register,value in ((9,cookie),(10,nonce),(19,cookie),(20,nonce),(31,sp),(32,pc)):
            state[2*register:2*register+2]=[value&0xffffffff,value>>32]
        base['repair']['address']=address
        def server(role):
            values={k:copy.deepcopy(base[k]) for k in ('wire','receive','identity','repair','reply','send')}
            port=200+w if role=='prior' else 100+w
            for r in values.values():r['role']=role
            values['receive'].update(port=port,code=[2,address],state=state)
            values['reply']['state']=state
            values['wire']['raw']=(struct.pack('<6I',18,336,300+w,port,0,2406)+b'\0\0\0\0\1\0\0\0'+struct.pack('<2I2Q2I',1,2,2,address,6,68)+struct.pack('<68I',*state)).hex()
            values['wire']['reply_right']=300+w
            return values
        front=server('prior' if proof else 'front')
        result=[arm]+[front[k] for k in ('wire','receive','identity')]
        if path=='forward' and not proof:
            prior=server('prior');result += [prior[k] for k in ('wire','receive','identity','repair','reply','send')]
            result.append(dict(kind='forward_call',worker=w,event=e,prior_kr=0,prior_flavor=6,prior_stateCnt=68,state=state))
        else:result.append(front['repair'])
        result += [front['reply'],base['resume'],front['send'],base['commit'],base['disarm']]
        return result
    for e in range(total):
        for w in range(n):rows+=event(w,e)
    for w in range(n):rows.append(dict(kind='worker_parked',worker=w,completed=total,disarmed=True))
    if path=='forward':
        for w in range(n):
            rows+=snapshot(w,'restored-prior',200+w)
            rows += [dict(kind='teardown_restore',worker=w,label='restored-prior',semantic_equal=True,alive=True,disarmed=True),dict(kind='disposal',worker=w,right='restored-prior-0',kr=0)]
            rows+=event(w,total,True);rows.append(dict(kind='restoration_proof',worker=w,event=total))
    for w in range(n):
        rows+=snapshot(w,'restored-initial');rows.append(dict(kind='teardown_restore',worker=w,label='restored-initial',semantic_equal=True,alive=True,disarmed=True))
        rights=['send','receive','thread_self','data_page']+(['saved-prior-0','prior_send','prior_receive'] if path=='forward' else [])
        for right in rights:rows.append(dict(kind='disposal',worker=w,right=right,kr=0))
        rows.append(dict(kind='worker_exit',worker=w))
    if topology=='serial':rows.append(dict(kind='disposal',worker=-1,right='port_set',kr=0))
    rows+=snapshot(-1,'task-final')
    rows.append(dict(kind='summary',status='completed',zero_unclassified=True,events_checked=n*total,events_warmup=0,proof_events=n*(path=='forward'),commits_total=n*(total+(path=='forward'))))
    return rows


def held_trace(point,n=2,path='direct',topology='perworker',access='load',worker=0,total=1):
    rows=valid_trace(n,path,topology,access,total)
    # The native hold campaign admits the selected worker first in round 0.
    first=[r for r in rows if r.get('event')==0]
    start=rows.index(first[0])
    for r in first:rows.remove(r)
    ordered=[]
    for offset in range(n):ordered += [r for r in first if r.get('worker')==(worker+offset)%n]
    rows[start:start]=ordered
    config=dict(kind='hold_configuration',point=point,worker=worker,controller_threads=1);rows.insert(1,config)
    for arm in reversed(by_kind(rows,'arm')):
        rows.insert(rows.index(arm)+1,dict(kind='fault_attempt',worker=arm['worker'],event=arm['event']))
    for exited in reversed(by_kind(rows,'worker_exit')):rows.insert(rows.index(exited),dict(kind='join_attempt',worker=exited['worker']))
    teardown=point in ('teardown-before-restore','before-right-disposal')
    role='worker'
    def matching(kind,role=None,event=0):return next(r for r in rows if r['kind']==kind and r.get('worker')==worker and r.get('event')==event and (role is None or r.get('role')==role))
    if point=='worker-armed-before-fault':target=matching('fault_attempt')
    elif point=='server-after-receive':target=matching('wire','front');role='front'
    elif point=='server-before-repair':role='prior' if path=='forward' else 'front';target=matching('repair',role)
    elif point=='server-before-reply':target=matching('reply','front');role='front'
    elif point=='front-before-forward':target=matching('wire','prior');role='front'
    elif point=='prior-before-reply':target=matching('reply','prior');role='prior'
    elif point=='worker-resumed-before-disarm':target=matching('commit')
    elif point=='teardown-before-restore':target=next(r for r in rows if r['kind']=='snapshot' and r.get('worker')==worker and r['label']=='restored-initial')
    else:target=next(r for r in rows if r['kind']=='disposal' and r['worker']==worker and r['right']=='send')
    entered=dict(kind='hold_enter',worker=worker,event=total-1+(path=='forward') if teardown else 0,point=point,role=role,outstanding=0 if teardown else 1)
    extra=[entered]
    if point=='server-after-receive':
        extra.insert(0,dict(kind='received_message',worker=worker,event=0,role='front',size=target['size'],raw=target['raw']))
    if teardown:
        join=next(r for r in rows if r['kind']=='join_attempt' and r['worker']==worker);rows.remove(join);extra.append(join)
        extra.append(dict(kind='hold_competitor',worker=worker,operation='join',target=worker))
    else:
        extra += [dict(kind='teardown_request',worker=worker),dict(kind='teardown_deferred',worker=worker,outstanding=1,completed=0)]
        if n>1:
            other=(worker+1)%n
            competing=[r for r in rows if (r.get('worker'),r.get('event'))==(other,0)]
            if topology=='serial':competing=competing[:2] # arm + actual fault-attempt; server remains queued
            for r in competing:rows.remove(r)
            extra+=competing
            extra.append(dict(kind='hold_competitor',worker=worker,operation='fault',target=other,completed=int(topology=='perworker'),independent_server=topology=='perworker'))
        else:extra.append(dict(kind='hold_competitor',worker=worker,operation='teardown',target=worker))
    extra += [dict(kind='hold_release',worker=worker,point=point),dict(kind='hold_exit',worker=worker,event=entered['event'],point=point)]
    at=rows.index(target);rows[at:at]=extra
    return rows,dict(kind='hold',hold=dict(hold=point,worker=worker))
