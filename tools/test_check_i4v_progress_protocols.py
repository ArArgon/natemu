"""Synthetic protocol controls; these fixtures do not claim native execution."""
import copy
import unittest
from unittest.mock import patch

import check_i4v_progress_protocols as oracle
import check_i4v_schedule as schedule
from test_check_i4v_stack_concurrent import fixture as stack_fixture
from test_i4v_controls_concurrent_check import report_fixture as controls_fixture


def record(cfg, grants, pool_events):
    rng=cfg['seed'];ledger=[];cuts={}
    for key,at,phase,epoch in grants:
        after=schedule.random_step(rng)
        ledger.append(dict(cut=key,phase=phase,epoch=epoch,cohort=1,eligible=1,
            arrived=1,done_before=0,prng_before=rng,prng_after=after,draw=len(ledger),
            chosen=0,claimed=1,trace_before=at,claim_trace_before=at))
        rng=after;cuts[key]=dict(key=key,cohort=1,arrived=1,completed=1,chosen=oracle.NONE,decision=oracle.NONE)
    return dict(case='schedule',config=cfg,summary=dict(magic=schedule.MAGIC,version=1,
        prng=rng,draws=len(ledger),cut_count=len(cuts),decision_count=len(ledger),
        pool_event_count=len(pool_events),full_count=0,readmitted=0,full=[0]*4,retried=[0]*4),
        cuts=list(cuts.values()),decisions=ledger,pool_events=pool_events,
        supervision=dict(host_pid=123,joined=1,completions=1,destroyed=[[0,0]],statuses=[[0,1,0]],
            cancellations=[],error=None,elapsed_ms=1))


def event_for(state,owner,capacity,at):
    after=copy.deepcopy(state);result,serial=oracle.enqueue(after,owner,capacity)
    return dict(owner=owner,ordinal=state['submitted'][owner]+1,capacity=capacity,
        depth=state['length']+int(state['current']['serial']!=0),epoch=state['epoch'],
        result=result,serial=serial,before_digest=oracle.queue_digest(state),
        after_digest=oracle.queue_digest(after),trace_before=at)


def stack_case(ordinal=0):
    import check_i4_pressure
    # The generic architectural fixture permits an eight-head batch. This
    # scheduling fixture additionally models the resident's reservation at
    # seven heads, before creating the actual stack frames/memory effects.
    base=check_i4_pressure.known_result(1)
    start=next(i for i,w in enumerate(base['trace']) if w>>8==23)
    ordinary=next(i for i,w in enumerate(base['trace']) if w>>8==29)
    block=base['trace'][ordinary:ordinary+4];del base['trace'][ordinary:ordinary+4]
    base['trace'][start+35:start+35]=block
    base['maintenance']['intervals'][0].update(heads=7,epoch=9,gpa=0x100000)
    cfg=schedule.configuration(0,1,ordinal)
    with patch.object(check_i4_pressure,'known_result',return_value=base):
        native=stack_fixture(1,1+ordinal%2,actor=cfg["actor"])
    snapshots,states=oracle.stack_states(native,cfg);grants=[];events=[]
    for at,word in enumerate(native['trace']):
        kind=word>>8;phase,_,state,_=snapshots[at]
        if kind in (1,2,9,17,26):
            key={1:oracle.ADMIT,2:oracle.DETACH|1,9:oracle.ACK,17:oracle.RELEASE|1,
                 26:oracle.MAINTENANCE_ACK|state['epoch']}[kind]
            epoch=1 if kind in (1,2) else state['epoch'] if kind==26 else 2
            grants.append((key,at,phase,epoch))
        if kind==23:events.append(event_for(states[at],0,1,at))
    return record(cfg,grants,events),native,cfg


def controls_case(ordinal=0):
    cfg=schedule.configuration(1,1,ordinal);native=controls_fixture(21 if ordinal%2==0 else 24)
    trace,snapshots,states,entries=oracle.control_states(native,cfg);grants=[];events=[]
    for at,word in enumerate(trace):
        kind=word>>8;shot=snapshots[at];state=shot['queue']
        if kind in (1,2):
            visit=shot['owners'][0]['index']+1
            key=(oracle.ADMIT if kind==1 else oracle.DETACH)|0x10000|(visit<<8)
            grants.append((key,at,shot['phase'],shot['owners'][0]['context']))
        elif kind in (112,113,116,118,26):
            key={112:oracle.ACK,113:oracle.ACK|1,116:oracle.SOURCE_ACK,
                 118:oracle.SOURCE_RELEASE,26:oracle.MAINTENANCE_ACK|state['epoch']}[kind]
            epoch=kind-111 if kind in (112,113) else state['epoch'] if kind==26 else 2
            grants.append((key,at,shot['phase'],epoch))
        if kind==23:events.append(event_for(states[at],0,1,at))
    return record(cfg,grants,events),native,cfg


def reduced_pool_case(n=3,ordinal=0):
    """Queue-only simulation: actual identity reuse, ring wrap and FIFO drain."""
    from i4v_controls_concurrent_check import empty_maintenance
    cfg=schedule.configuration(0,n,ordinal);capacity=cfg['pool_limit']
    state=empty_maintenance(n);state.update(ready=(1<<n)-1,compose_leaf=0x101067)
    trace=[];states=[];events=[];full=[0]*4;retried=[0]*4
    def emit(kind,owner,change):
        states.append(copy.deepcopy(state));trace.append((kind<<8)|owner);change()
    def finish_head():
        current=state['current'];owner=current['cpu'];index=state['total']
        state['request_history'][index]=dict(current)
        state['leaf_history'][index]=(0x100000+(4096 if (index+1)%2==0 else 0))|0x27
        state['pending'][owner]=0;state['completed'][owner]+=1
        state['total']+=1;state['batch']+=1;state['current']=dict(cpu=oracle.NONE,serial=0)
    for _ in range(300):
        if state['finished']:break
        for owner in range(n):
            if state['pending'][owner] or state['submitted'][owner]>=9:continue
            if owner==state['next_ordinary'] and state['batch']>=8-n:continue
            event=event_for(state,owner,capacity,len(trace));bit=1<<(event['ordinal']-1)
            if event['result']==131:
                if not full[owner]&bit:events.append(event);full[owner]|=bit
            else:
                events.append(event)
                if full[owner]&bit:retried[owner]|=bit
                emit(23,owner,lambda:oracle.enqueue(state,owner,capacity))
                emit(24,owner,lambda:state.update(duplicate_rejections=state['duplicate_rejections']+1))
        next_owner=state['next_ordinary']
        ordinary=state['batch'] and not state['pending'][next_owner] and (
            state['batch']>=8 or state['total']==n*9 or (not state['length'] and
            all(owner==next_owner or state['completed'][owner]==9 for owner in range(n))))
        if ordinary:
            emit(29,next_owner,lambda:state.update(ordinary=next_owner,closing=0))
            emit(28,(next_owner+1)%n,lambda:state.update(closing=1))
            emit(31,next_owner,lambda:None)
            def finish():
                state['intervals'][next_owner]+=1;state['ordinary_total']+=1
                state.update(ordinary=oracle.NONE,batch=0,next_ordinary=(next_owner+1)%n,
                             finished=int(state['total']==n*9))
            emit(30,next_owner,finish)
        elif state['length']:
            owner=state['queue'][state['head']]['cpu']
            def begin():
                state['current']=dict(state['queue'][state['head']])
                state.update(head=(state['head']+1)%n,length=state['length']-1,
                             epoch=state['epoch']+1,acknowledgements=0)
            emit(25,owner,begin)
            last=state['epoch']%n
            for cpu in [i for i in range(n) if i!=last]+[last]:
                emit(26,cpu,lambda:state.update(acknowledgements=state['acknowledgements']|(1<<cpu)))
            emit(27,owner,finish_head)
        else:raise AssertionError('synthetic queue made no progress')
    assert state['finished']
    states.append(copy.deepcopy(state))
    count=sum(bin(word).count('1') for word in full)
    evidence=dict(pool_events=events,summary=dict(full=full,retried=retried,full_count=count,readmitted=count))
    return evidence,cfg,trace,states


class ProtocolTests(unittest.TestCase):
    def test_rotated_stack_cut_cohorts_follow_actual_trace_roles(self):
        for n in range(2,5):
            for ordinal in range(4):
                cfg=schedule.configuration(0,n,ordinal);actor=cfg['actor'];last=(actor+n-1)%n
                native=stack_fixture(n,1+ordinal%2,actor=actor)
                # Only the actual initial-cut prefix is relevant here; the
                # older architectural fixture uses a different legal pressure
                # reservation policy. Reduced-pool replay is covered below.
                start=next(i for i,w in enumerate(native['trace']) if w>>8==23)
                prefix={'trace':native['trace'][:start+1]}
                snapshots,_=oracle.stack_states(prefix,cfg)
                trace=native['trace'];contract,required=oracle.stack_contract(cfg,trace,snapshots)
                all_owners=(1<<n)-1;early=all_owners^(1<<last)
                at=next(i for i,w in enumerate(trace) if w>>8==2 and w&255==last)
                self.assertEqual(contract(oracle.DETACH|1,at,0)[:2],(1<<last,1<<last))
                at=next(i for i,w in enumerate(trace) if w>>8==2 and w&255!=last)
                self.assertEqual(contract(oracle.DETACH,at,0)[:2],(early,early))
                at=next(i for i,w in enumerate(trace) if w>>8==9)
                self.assertEqual(contract(oracle.ACK,at,0)[:2],(all_owners,early))
                at=next(i for i,w in enumerate(trace) if w>>8==9 and w&255==last)
                self.assertEqual(contract(oracle.ACK,at,early)[:2],(all_owners,all_owners))
                at=next(i for i,w in enumerate(trace) if w>>8==17 and w&255==last)
                self.assertEqual(contract(oracle.RELEASE|1,at,0)[:2],(1<<last,1<<last))
                for cpu in range(n):
                    role=(cpu+n-actor)%n;action=22 if role==1 else 20 if role==3 else 1
                    at=next(i for i,w in enumerate(trace) if w==(action<<8|cpu))
                    self.assertEqual(contract(oracle.ADMIT,at,snapshots[at][3]['admitted'])[-1],action)
                self.assertIn(oracle.DETACH,required)

    def test_rotated_stack_producer_binds_actual_actor_transfers(self):
        import i4v_stack_worker as worker
        cfg=schedule.configuration(0,4,2);native=stack_fixture(4,1,actor=2)
        with patch.object(worker,'verify_compilations') as verify, \
             patch.object(worker,'verify_transfers',return_value={'verified':True}) as transfers:
            exports=oracle.verify_producer([{'config':cfg},native],'/unused-synthetic')
            self.assertEqual(len(exports),3)
            self.assertEqual(verify.call_args.args[1],native['requests'])
            for index,call in enumerate(transfers.call_args_list):
                self.assertTrue(all(request[3]==2 for request in call.args[1]))
                self.assertEqual(call.args[2],native['stacks'][2]['replies'][index])
            bad=copy.deepcopy(native);bad['requests'][0][3]=0
            with self.assertRaises(ValueError):oracle.verify_producer([{'config':cfg},bad],'/unused-synthetic')

    def test_complete_single_owner_stack_and_both_control_programs(self):
        for ordinal in range(4):
            with self.subTest(family='stack',ordinal=ordinal):
                self.assertEqual(oracle.stack(*stack_case(ordinal))['capacity_full'],0)
            with self.subTest(family='controls',ordinal=ordinal):
                self.assertEqual(oracle.controls(*controls_case(ordinal))['capacity_full'],0)

    def test_decision_corruption_is_bound_to_real_operations(self):
        for factory,check in ((stack_case,oracle.stack),(controls_case,oracle.controls)):
            original,native,cfg=factory()
            for field in ('phase','epoch','cohort','eligible','trace_before','claim_trace_before'):
                with self.subTest(family=cfg['family'],field=field):
                    bad=copy.deepcopy(original);bad['decisions'][0][field]^=1
                    with self.assertRaises(ValueError):check(bad,native,cfg)
            bad=copy.deepcopy(original);bad['decisions'].pop()
            bad['summary']['decision_count']-=1
            with self.assertRaises(ValueError):check(bad,native,cfg)

    def test_full_queue_digest_not_just_scalar_counters(self):
        from i4v_controls_concurrent_check import empty_maintenance
        state=empty_maintenance(3);state['ready']=7;state['compose_leaf']=0x101067
        base=oracle.queue_digest(state)
        for kind in ('queue','request_history','pending','submitted','completed','intervals','leaf_history'):
            altered=copy.deepcopy(state)
            if kind in ('queue','request_history'):altered[kind][-1]['serial']=1
            else:altered[kind][-1]=1
            self.assertNotEqual(oracle.queue_digest(altered),base,kind)

    def test_full_preserves_identity_and_readmission_uses_same_ordinal(self):
        from i4v_controls_concurrent_check import empty_maintenance
        state=empty_maintenance(3);state['ready']=7
        self.assertEqual(oracle.enqueue(state,0,1),(0,1))
        before=copy.deepcopy(state)
        self.assertEqual(oracle.enqueue(state,2,1),(131,0))
        self.assertEqual(state,before)
        # Draining the original head frees exactly one slot; the rejected
        # owner has neither a serial nor an incremented submitted counter.
        state['head']=1;state['length']=0;state['pending'][0]=0;state['completed'][0]=1
        self.assertEqual(state['submitted'][2],0)
        self.assertEqual(oracle.enqueue(state,2,1),(0,2))
        self.assertEqual((state['submitted'][2],state['pending'][2]),(1,2))

    def test_reduced_pool_full_to_readmission_complete_lifecycle(self):
        for n in (2,3,4):
            for ordinal in range(4):
                with self.subTest(n=n,ordinal=ordinal):
                    fixture=reduced_pool_case(n,ordinal)
                    self.assertGreater(oracle.pool(*fixture),0)
        original,cfg,trace,states=reduced_pool_case()
        full_index=next(i for i,e in enumerate(original['pool_events']) if e['result']==131)
        for mutation in ('counter','identity','equal forged hashes','missing readmission','duplicate FULL','late FULL'):
            bad=copy.deepcopy(original);event=bad['pool_events'][full_index]
            if mutation=='counter':event['serial']=1
            elif mutation=='identity':event['ordinal']+=1
            elif mutation=='equal forged hashes':event['before_digest']=event['after_digest']=0
            elif mutation=='duplicate FULL':bad['pool_events'].insert(full_index,dict(event))
            elif mutation=='late FULL':event['trace_before']=len(trace)-1
            else:
                idx=next(i for i,e in enumerate(bad['pool_events']) if e['result']==0 and
                    (e['owner'],e['ordinal'])==(event['owner'],event['ordinal']))
                del bad['pool_events'][idx]
            with self.subTest(mutation=mutation):
                with self.assertRaises(ValueError):oracle.pool(bad,cfg,trace,states)

    def test_pool_journal_corruptions(self):
        for factory,check in ((stack_case,oracle.stack),(controls_case,oracle.controls)):
            original,native,cfg=factory()
            for field in ('owner','ordinal','capacity','depth','epoch','result','serial',
                          'before_digest','after_digest','trace_before'):
                with self.subTest(family=cfg['family'],field=field):
                    bad=copy.deepcopy(original);bad['pool_events'][0][field]^=1
                    with self.assertRaises(ValueError):check(bad,native,cfg)
            for mutation in ('missing','duplicate','reported full'):
                bad=copy.deepcopy(original)
                if mutation=='missing':bad['pool_events'].pop();bad['summary']['pool_event_count']-=1
                elif mutation=='duplicate':
                    bad['pool_events'].insert(0,copy.deepcopy(bad['pool_events'][0]));bad['summary']['pool_event_count']+=1
                else:bad['summary']['full'][0]=1;bad['summary']['retried'][0]=1
                with self.subTest(family=cfg['family'],mutation=mutation):
                    with self.assertRaises(ValueError):check(bad,native,cfg)

    def test_real_architectural_evidence_stays_required(self):
        s,n,c=stack_case();n['cells'][0]['captured']['x'][8]^=1
        with self.assertRaises(ValueError):oracle.stack(s,n,c)
        s,n,c=controls_case();n['owners'][0]['visits'][0]['generation_before']+=1
        with self.assertRaises(ValueError):oracle.controls(s,n,c)


if __name__=='__main__':unittest.main()
