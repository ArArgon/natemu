"""Seeded stack/control decisions bound to independent architectural replays.

Queue hashes are recomputed from the complete ring (including stale slots).
They are evidence about real enqueue attempts, not an eligibility oracle.
"""
import copy
from pathlib import Path

from i4v_check import exact, require, uint
from check_i4v_schedule import (configuration, decisions, NONE, ADMIT, DETACH,
    ACK, RELEASE, MAINTENANCE_ACK, SOURCE_ACK, SOURCE_RELEASE)


def mask(owners):
    return sum(1 << owner for owner in owners)


def except_last(all_owners, last, completed):
    early = all_owners ^ (1 << last)
    return all_owners if completed & early == early else early


def queue_digest(state):
    values = [state[k] for k in ('n','ready','serial','head','length')]
    values += [state['current']['cpu'], state['current']['serial']]
    values += [state[k] for k in ('epoch','acknowledgements','total','batch',
        'next_ordinary','ordinary','ordinary_total','closing','finished',
        'duplicate_rejections','maximum_depth','compose_leaf')]
    for item in state['queue'] + state['request_history']:
        values += [item['cpu'], item['serial']]
    for key in ('pending','submitted','completed','intervals','leaf_history'):
        values += state[key]
    digest = 0xcbf29ce484222325
    for value in values:
        uint(value,64,'replayed queue word')
        digest = ((digest ^ value) * 0x100000001b3) & NONE
    return digest


def enqueue(state, owner, capacity):
    """Only successful submissions change the independent queue state."""
    require(owner < state['n'] and not state['finished'], 'live queue owner')
    require(state['pending'][owner] == 0 and owner != state['ordinary'],
            'enqueue has no outstanding identity or active ordinary grant')
    require(state['submitted'][owner] < 9, 'nine-request owner budget')
    require(owner != state['next_ordinary'] or state['batch'] < 8-state['n'],
            'next ordinary owner remains reserved near the eight-head bound')
    depth = state['length'] + bool(state['current']['serial'])
    if depth >= capacity:
        return 131, 0
    state['serial'] += 1
    serial = state['serial']
    state['queue'][(state['head']+state['length']) % state['n']] = dict(cpu=owner,serial=serial)
    state['length'] += 1
    state['pending'][owner] = serial
    state['submitted'][owner] += 1
    state['maximum_depth'] = max(state['maximum_depth'],depth+1)
    return 0, serial


def pool(schedule, cfg, trace, states, *, controls=False):
    """Associate every successful enqueue and every first FULL with real cuts.

    states[i] is queue state immediately before trace[i]. A controls success
    performs its duplicate probe before logging 23; stack logs that probe at 24.
    """
    n, capacity = cfg['n'],cfg['pool_limit']
    full, retried, admitted = [0]*4,[0]*4,set()
    previous, successful_at, first_full = 0,set(),None
    for event in schedule['pool_events']:
        exact(set(event),set('owner ordinal capacity depth epoch result serial before_digest after_digest trace_before'.split()),'pool event fields')
        for key,value in event.items():uint(value,64,'pool '+key)
        at,owner = event['trace_before'],event['owner']
        require(previous <= at < len(trace) and at not in successful_at,
                'queue journal order; successful enqueue immediately emits its trace')
        previous=at
        require(owner<n,'queue participant belongs to this VM')
        before=copy.deepcopy(states[at])
        exact(before['ready'],(1<<n)-1,'pool attempts begin after all owners reach pressure')
        ordinal=before['submitted'][owner]+1
        exact((event['ordinal'],event['capacity'],event['depth'],event['epoch']),
              (ordinal,capacity,before['length']+int(before['current']['serial']!=0),before['epoch']),
              'attempt binds original owner ordinal, capacity, depth and epoch')
        exact(event['before_digest'],queue_digest(before),'complete queue state before attempt')
        after=copy.deepcopy(before)
        result,serial=enqueue(after,owner,capacity)
        exact((event['result'],event['serial']),(result,serial),'actual bounded enqueue outcome')
        exact(event['after_digest'],queue_digest(after),'complete queue state after attempt')
        bit=1<<(ordinal-1)
        if result==131:
            require(not full[owner]&bit,'only the first FULL poll for each identity is journaled')
            exact(after,before,'FULL preserves every queue word and original identity')
            full[owner]|=bit
            if first_full is None:first_full=at
        else:
            exact(trace[at],(23<<8)|owner,'successful enqueue binds its real trace operation')
            require((owner,ordinal) not in admitted,'original request admitted exactly once')
            admitted.add((owner,ordinal));successful_at.add(at)
            if full[owner]&bit:retried[owner]|=bit
            if controls:after['duplicate_rejections']+=1
            exact(after,states[at+1],'native queue after successful admission and duplicate control')
    exact(successful_at,{i for i,w in enumerate(trace) if w>>8==23},'all real enqueues have lifecycle evidence')
    exact(admitted,{(owner,ordinal) for owner in range(n) for ordinal in range(1,10)},
          'every owner submits and completes nine original identities')
    exact(retried,full,'every capacity-rejected identity is eventually readmitted')
    summary=schedule['summary']
    exact((summary['full'],summary['retried']),(full,retried),'exact rejected/readmitted identity bitmaps')
    count=sum(bin(word).count('1') for word in full)
    exact((summary['full_count'],summary['readmitted']),(count,count),'exact lifecycle counts')
    exact(bool(count),n>1,'reduced multi-owner pool forces real capacity exhaustion')
    if n>1:
        require(first_full<=next(i for i,w in enumerate(trace) if w>>8==25),
                'real FULL rejection precedes the first protected head')
    final=states[-1]
    exact(final['completed'],[9]*n+[0]*(4-n),'readmitted identities actually complete')
    exact(final['maximum_depth'],capacity,'actual capacity reached')
    return count


def stack_states(report,cfg):
    from i4v_controls_concurrent_check import empty_maintenance
    trace=report['trace'];n=cfg['n'];all_owners=(1<<n)-1
    state=empty_maintenance(n);phase=0;acks=0
    posted=admitted=detached=released=0
    snapshots=[];states=[]
    start=next(i for i,w in enumerate(trace) if w>>8==23)
    for at,word in enumerate(trace):
        kind,owner=word>>8,word&255
        if at==start:
            state['ready']=all_owners
            # The strict stack oracle proves replacement writes to this page
            # precede pressure; they leave A/D set in the shared leaf.
            state['compose_leaf']=0x101067
        states.append(copy.deepcopy(state));snapshots.append((phase,acks,copy.deepcopy(state),
            dict(posted=posted,admitted=admitted,detached=detached,released=released)))
        if at<start:
            if kind==18:posted|=1<<owner
            elif kind==1:admitted|=1<<owner
            elif kind==2:phase=1;detached|=1<<owner
            elif kind==6:released|=1<<owner
            elif kind==3:phase=2
            elif kind==8:phase=4
            elif kind==16:phase=3
            elif kind==9:acks|=1<<owner
            elif kind==10:phase=0
            continue
        if kind==23:
            exact(enqueue(state,owner,cfg['pool_limit'])[0],0,'trace enqueue has capacity')
        elif kind==24:state['duplicate_rejections']+=1
        elif kind==25:
            require(state['current']['serial']==0 and state['length']>0,'one FIFO maintenance head')
            state['current']=copy.deepcopy(state['queue'][state['head']])
            exact(state['current']['cpu'],owner,'FIFO mutation identity')
            state['head']=(state['head']+1)%n;state['length']-=1
            state['epoch']+=1;state['acknowledgements']=0;phase=2
        elif kind==26:state['acknowledgements']|=1<<owner
        elif kind==27:
            exact(state['acknowledgements'],all_owners,'head completes only after covering acknowledgements')
            current=state['current'];exact(current['cpu'],owner,'completion owner')
            state['request_history'][state['total']]=copy.deepcopy(current)
            state['leaf_history'][state['total']]=(0x100000+(4096 if (state['total']+1)%2==0 else 0))|0x27
            state['pending'][owner]=0;state['completed'][owner]+=1
            state['total']+=1;state['batch']+=1;state['current']=dict(cpu=NONE,serial=0);phase=1
        elif kind==29:state['ordinary']=owner;state['closing']=0;phase=0
        elif kind==28:state['closing']=1;phase=1
        elif kind==30:
            state['intervals'][owner]+=1;state['ordinary_total']+=1;state['batch']=0
            state['next_ordinary']=(owner+1)%n;state['ordinary']=NONE
            if state['total']==9*n:state['finished']=1
            phase=0 if state['finished'] else 1
        elif kind!=31:raise ValueError('unmodeled stack maintenance event')
    states.append(copy.deepcopy(state))
    return snapshots,states


def stack_contract(cfg,trace,snapshots):
    n=cfg['n'];all_owners=(1<<n)-1;last=(cfg['actor']+n-1)%n;early=all_owners^(1<<last)
    required={ADMIT,DETACH|1,ACK,RELEASE|1}|{MAINTENANCE_ACK|epoch for epoch in range(3,3+9*n)}
    if early:required|={DETACH,RELEASE}
    def contract(key,at,done):
        phase,acks,state,owners=snapshots[at];kind,value=key>>32,key&0xffffffff
        cohort=eligible=all_owners
        if kind==1:
            exact(value,0,'initial stack entry');exact(phase,0,'initial entry while open')
            exact(owners['posted'],all_owners,'initial event posting precedes entry decisions')
            exact(owners['admitted'],done,'entry grants match actual admitted owners')
            epoch=1
            # Relative event roles perform these mandatory operations inside
            # the entry grant; physical owner IDs remain unchanged.
            owner=trace[at]&255
            role=(owner+n-cfg['actor'])%n
            action=22 if role==1 else 20 if role==3 else 1
        elif kind in (2,4):
            require(value in (0,1),'early/delayed stack cohort')
            cohort=eligible=(1<<last) if value else early
            epoch,action=(1,2) if kind==2 else (2,17)
            require(phase in ((0,1) if kind==2 else (0,)),'retained stack grant phase')
            exact(owners['admitted'],all_owners,'all old grants exist before retained work')
            exact(owners['detached' if kind==2 else 'released']&cohort,done,
                  'retained cohort grants match actual ownership changes')
        elif kind==3:
            exact(value,0,'replacement acknowledgement cut');exact(phase,3,'replacement is invalidating')
            eligible=except_last(all_owners,last,acks);epoch,action=2,9
            exact(acks,done,'scheduled acknowledgement binds completed invalidations')
        elif kind==5:
            exact(value,state['epoch'],'current maintenance head epoch')
            require(state['current']['serial']!=0 and phase==2,'acknowledge a live exclusive head')
            eligible=except_last(all_owners,value%n,state['acknowledgements']);epoch,action=value,26
            exact(state['acknowledgements'],done,'maintenance grant binds actual acknowledgement mask')
        else:raise ValueError('unknown stack scheduling cut')
        return cohort,eligible,phase,epoch,action
    return contract,required


def stack(schedule,report,cfg):
    import check_i4v_stack_concurrent as native
    exact(cfg,configuration(0,cfg['n'],cfg['seed_ordinal']),'stack requested configuration')
    native.check(report,cfg['n'],1+cfg['seed_ordinal']%2,1,capacity=cfg['pool_limit'],actor=cfg['actor'])
    trace=report['trace'];snapshots,states=stack_states(report,cfg)
    contract,required=stack_contract(cfg,trace,snapshots)
    count=decisions(schedule,cfg,trace,contract,required)
    return dict(decisions=count,capacity_full=pool(schedule,cfg,trace,states))


def control_observe(model,cfg,word):
    """Capture replay state before an action, without consulting scheduler logs."""
    kind,owner=word>>8,word&255;all_owners=(1<<cfg['n'])-1
    peers=all_owners^(1<<cfg['actor']);delayed=(cfg['actor']+cfg['n']-1)%cfg['n']
    before=copy.deepcopy(model.pressure)
    if kind==23 and not model.maintenance_started:
        before['ready']=all_owners;before['compose_leaf']=model.table_leaves()[2]
    owners=[dict(index=o['pending']['index'] if o['pending'] else None,
        context=o['context'],admitted=o['admitted'],detached=o['detached'],local_cut=o['local_cut'])
        for o in model.owners]
    snapshot=dict(phase=model.phase,queue=before,owners=owners,source_ack=mask(model.source_ack),
                  released=mask(model.released),pae_ack=list(map(mask,model.pae_ack)))
    item=None
    if kind in (1,2):
        o=model.owners[owner];require(o['pending'] is not None,'scheduled real instruction')
        visit=o['pending']['index']+1
        first=owner!=cfg['actor'] and o['local_cut']==0 and visit==model.first_memory+1
        if kind==1 and first:key,cohort=ADMIT|0x100|visit,peers
        elif kind==2 and first and cfg['n']>=3 and owner!=delayed:
            key,cohort=DETACH|0x100|visit,peers^(1<<delayed)
        else:key,cohort=(ADMIT if kind==1 else DETACH)|0x10000|(visit<<8)|owner,1<<owner
        item=(key,(cohort,o['context'],kind,visit))
    return snapshot,item


def control_states(report,cfg):
    import i4v_controls_concurrent_check as native
    model=native.ConcurrentReplay(report['profile'],cfg['n'],cfg['actor'],report['vm_nonce'],
        [owner['capture_pc'] for owner in report['owners']],source_control='positive',revision=1,
        maintenance=True,peer_reply_control=True,capacity=cfg['pool_limit'])
    trace=report['coordinator']['trace'][:report['coordinator']['trace_length']]
    states=[];snapshots=[];required={}
    for word in trace:
        snapshot,item=control_observe(model,cfg,word)
        states.append(snapshot['queue']);snapshots.append(snapshot)
        if item is not None:
            key,item=item
            if key in required:exact(required[key],item,'shared cut has one architectural context')
            required[key]=item
        model.step(word)
    states.append(copy.deepcopy(model.pressure))
    return trace,snapshots,states,required


def control_contract(cfg,profile,snapshots,entries,delayed=None):
    n=cfg['n'];all_owners=(1<<n)-1
    if delayed is None:delayed=(cfg['actor']+n-1)%n
    def contract(key,at,done):
        snapshot=snapshots[at];phase=snapshot['phase'];state=snapshot['queue']
        kind,value=key>>32,key&0xffffffff;cohort=eligible=all_owners
        if kind in (1,2):
            cohort,epoch,action,visit=entries[key];eligible=cohort
            require(phase in ((0,) if kind==1 else (0,1)),'native entry/fault uses actual coordinator phase')
            reached=mask(owner for owner,o in enumerate(snapshot['owners'])
                if cohort&(1<<owner) and o['index']==visit-1 and o['context']==epoch)
            completed=mask(owner for owner,o in enumerate(snapshot['owners'])
                if reached&(1<<owner) and o['admitted' if kind==1 else 'detached'])
            exact(completed,done,'grants correspond to replayed admission/detach ownership')
            require(reached&~done,'at least one real caller has reached this instruction cut')
            # A singleton grant is already at its actual fetched instruction.
            # Shared first-peer cuts may select a not-yet-arrived peer, whose
            # eventual context/visit is separately proven by the full replay.
            if cohort and cohort & (cohort-1)==0:
                owner=cohort.bit_length()-1;o=snapshot['owners'][owner]
                exact((o['index'],o['context']),(visit-1,epoch),'single-owner live instruction identity')
                exact(o['admitted'],kind==2,'entry versus retained fault ownership')
                require(not o['detached'],'cannot grant a detached instruction again')
        elif kind==3:
            require(profile==21 and value in (0,1),'actual shared PAE root publication')
            exact(phase,2,'PAE acknowledgement while exclusive');epoch,action=value+1,112+value
            exact(snapshot['pae_ack'][value],done,'PAE grant matches actual acknowledgement history')
        elif kind==5:
            exact(value,state['epoch'],'current maintenance epoch')
            require(state['current']['serial']!=0 and phase==2,'live exclusive maintenance head')
            eligible=except_last(all_owners,value%n,state['acknowledgements']);epoch,action=value,26
            exact(state['acknowledgements'],done,'maintenance grant matches actual acknowledgements')
        elif kind in (6,7):
            exact(value,0,'source lifecycle cut');exact(phase,2,'source ownership remains exclusive')
            completed=snapshot['source_ack'] if kind==6 else snapshot['released']
            exact(completed,done,'source grant matches actual ownership history')
            eligible=except_last(all_owners,delayed,completed);epoch,action=2,116 if kind==6 else 118
        else:raise ValueError('unknown control scheduling cut')
        return cohort,eligible,phase,epoch,action
    return contract


def controls(schedule,report,cfg):
    import i4v_controls_concurrent_check as native
    exact(cfg,configuration(1,cfg['n'],cfg['seed_ordinal']),'controls requested configuration')
    profile=21 if cfg['seed_ordinal']%2==0 else 24
    native.check(report,profile,cfg['n'],cfg['actor'],capacity=cfg['pool_limit'])
    trace,snapshots,states,entries=control_states(report,cfg)
    required=set(entries)|{SOURCE_ACK,SOURCE_RELEASE}|{MAINTENANCE_ACK|e for e in range(3,3+9*cfg['n'])}
    if profile==21:required|={ACK,ACK|1}
    contract=control_contract(cfg,profile,snapshots,entries)
    count=decisions(schedule,cfg,trace,contract,required)
    return dict(decisions=count,capacity_full=pool(schedule,cfg,trace,states,controls=True))


def verify_producer(records,directory):
    schedule,report=records;directory=Path(directory);exports=[]
    if schedule['config']['family']==0:
        import i4v_stack_worker as worker
        worker.verify_compilations(directory,report['requests'])
        actor=schedule['config']['actor']
        exact((report['actor'],report['delayed_owner']),(actor,(actor+schedule['config']['n']-1)%schedule['config']['n']),'producer fixture roles')
        for request in report['requests']:exact(request[3],actor,'actual compiler request owner')
        owner=next(o for o in report['stacks'] if o['id']==actor)
        for index,request in enumerate(report['requests']):
            path=directory/f'request-{index:03}'
            export=worker.verify_transfers(path,worker.requests_for(tuple(request)),owner['replies'][index])
            exports.append((path,export))
    else:
        exact(schedule['config']['family'],1,'stack/control producer association')
        import i4v_controls_worker as worker
        exact(len(list(directory.glob('cpu-*'))),schedule['config']['n'],'one compiler root per owner')
        for owner in report['owners']:
            root=directory/f"cpu-{owner['id']}"
            pairs=[(v['request'],v['reply']) for v in owner['visits'] if v['entries']]
            pairs.append((owner['controls']['deferred'],owner['controls']['late_reply']))
            if owner['owner']['source_rejections']:
                pairs.append((owner['owner']['source_expected'],owner['owner']['source_reply']))
            pairs.sort(key=lambda pair:pair[0][12])
            requests=[p[0] for p in pairs]
            exact(requests,owner['compiler_requests'],'compiler jobs bind every actual resident delivery')
            exact(len(requests),owner['requests'],'actual compiler job count')
            worker.verify_compilations(root,requests,[p[1] for p in pairs])
            for index,request in enumerate(requests):
                path=root/f'request-{index:03}'
                exports.append((path,worker.verify_request(path,request)))
    return exports
