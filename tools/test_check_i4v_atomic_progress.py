"""Synthetic protocol controls and a retained native development regression."""
import copy
import gzip
import json
from pathlib import Path
import unittest
import check_i4v_atomic_progress as oracle
import check_i4v_ordering as native
from check_i4v_schedule import configuration, random_step, MAGIC, NONE
from test_check_i4v_ordering import report_fixture
from check_i3_paging import guest_seed, copy_live


def fixture(n=2,ordinal=0):
    cfg=configuration(3,n,ordinal)
    cfg['token_seed']=(cfg['seed']&65535) or 1
    report=report_fixture(9,n,cfg['actor'],cfg['token_seed'])
    mask=(1<<n)-1;actor=cfg['actor'];writer=(actor+1)%n
    trace=[];decisions=[];cuts=[];rng=cfg['seed']
    def emit(event,cpu):trace.append((event<<8)|cpu)
    def cut(key,phase,epoch,action,last=None):
        nonlocal rng
        done=0;order=[]
        while done!=mask:
            early=mask^(1<<last) if last is not None else mask
            eligible=(mask if done&early==early else early)&~done
            old=rng;rng=random_step(rng)
            available=[cpu for cpu in range(n) if eligible&(1<<cpu)]
            chosen=available[rng%len(available)]
            decisions.append(dict(cut=key,phase=phase if not callable(phase) else phase(done),epoch=epoch,
                cohort=mask,eligible=eligible,arrived=mask,done_before=done,prng_before=old,prng_after=rng,
                draw=len(decisions),chosen=chosen,claimed=1,trace_before=len(trace),claim_trace_before=len(trace)))
            action(chosen);done|=1<<chosen;order.append(chosen)
        cuts.append(dict(key=key,cohort=mask,arrived=mask,completed=mask,chosen=NONE,decision=NONE))
        return order
    def admitted(trial,cpu):emit(1,cpu);emit(0x1000+trial,cpu)
    cut(oracle.ADMIT,0,1,lambda cpu:admitted(0,cpu))
    def detach(cpu):
        for event in (2,80,80,81):emit(event,cpu)
    queue=cut(oracle.DETACH,lambda done:int(bool(done)),1,detach)
    emit(82,writer);emit(3,0)
    for cpu in queue:
        for event in (4,83,5,84):emit(event,cpu)
    emit(85,writer)
    def ack(cpu):
        if cpu==actor:emit(87,cpu)
        emit(86,cpu)
    cut(oracle.ACK,2,2,ack,actor)
    emit(88,writer)
    for cpu in range(n):emit(89,cpu)
    for trial in range(1,32):
        cut(oracle.ADMIT|trial,0,trial+1,lambda cpu:admitted(trial,cpu))
        for cpu in reversed(range(n)):emit(0x2000+trial,cpu)
    for cpu in range(n):emit(90,cpu)
    schedule=dict(case='schedule',config=cfg,cuts=cuts,decisions=decisions,pool_events=[],
        summary=dict(magic=MAGIC,version=1,prng=rng,draws=len(decisions),cut_count=len(cuts),decision_count=len(decisions),
                     pool_event_count=0,full_count=0,readmitted=0,full=[0]*4,retried=[0]*4),
        supervision=dict(host_pid=123,joined=n,completions=n,destroyed=[[cpu,0] for cpu in range(n)],
                         statuses=[[cpu,1,0] for cpu in range(n)],cancellations=[],error=None,elapsed_ms=100))
    report['image_bytes']=12288
    for trial in range(32):
        original=bytes.fromhex(report['images'][trial]);image=list(original)+[0x5a]*4096
        for cpu in range(n):
            image[8192+cpu*128:8196+cpu*128]=[0]*4
            if trial:
                image[8192+cpu*128:8196+cpu*128]=image[4096+cpu*128:4100+cpu*128]
                image[4096+cpu*128:4100+cpu*128]=[0]*4
        report['images'][trial]=bytes(image).hex()
    report['tables_after'][12288+257*8:12288+258*8]=(0x103067).to_bytes(8,'little')
    for cell in report['owners']:
        cpu=cell['id'];cell['walks']=4;cell['table_reads']=16;cell['routes'][1]=0x103000
        cell['final_native_leaf'][257]+=8192
        first=cell['trials'][0];first['faults']=1
        first['fault'][0]=dict(frame=copy.deepcopy(first['captured']),esr=(0x25<<26)|64|7,
            far=0x101000+cpu*128,pc=native.CODE+16,spsr=0xa00003c4,resume=native.CODE+16)
    first=bytes.fromhex(report['images'][0]);held=bytearray(first)
    for cpu in range(n):held[4096+cpu*128:4100+cpu*128]=bytes(4)
    coord=dict.fromkeys(('scenario alias_profile stack_profile mixed_profile mixed_compiled ready phase active queued head length completed retained new_entries new_completed new_references reclaimed stale_replies stale_entries reuse_blocked translation_wait').split(),0)
    coord.update(participants=n,finished=mask,lock=100,next_ticket=100,servicing=NONE,generation=2,
        acknowledgements=mask,old_eligible=1,published=1,queue=[NONE]*4,event_pending=[2]*n+[0]*(4-n),
        event_notifications=[2]*n+[0]*(4-n),trace_length=len(trace),trace=trace)
    atomic=dict(magic=0x41544f4d50520001,n=n,actor=actor,writer=writer,parked=mask,requested=1,drained=mask,
        mutated=1,acknowledged=mask,blocked=1,opened=1,delivered=mask,snapshot_count=3,coordinator=coord,
        cuts=[held.hex(),first.hex(),first.hex()],tables_before=native.initial_tables(),
        tables_after=copy.deepcopy(report['tables_after']),owners=[])
    def guest(frame,rip=0x102000):
        value=guest_seed('cold');copy_live(value,frame);value['rip']=rip;return value
    for cpu in range(4):
        active=cpu<n;frame=report['owners'][cpu]['initial'] if active else native.zero_frame()
        before=guest(frame,0x102000+cpu*256);completed=copy.deepcopy(before)
        owner=dict(saved_gpa=0,saved_leaf=0,fault_pc=0,resume_pc=0,queued=0,resumed=0,completed=0,ack_leaf=0,new_leaf=0,
            fault_frame=native.zero_frame(),before_guest=before,completed_guest=completed,
            event=dict(consumed=0,deliveries=0,boundary=0,blocked=0,wait_checks=0,shadow=0,priority=0,packet=guest(frame)),
            walk=dict(address=0,gpa=0,leaf_bytes=0,writable=0,dirty=0,read_count=0,writes=0,reads=[[0,0] for _ in range(4)]))
        if active:
            fault=copy.deepcopy(report['owners'][cpu]['trials'][0]['fault'][0]['frame'])
            copy_live(completed,fault);completed['rip']+=4
            attrs=3|(1<<10)|(3<<8)|(3<<53)
            owner.update(saved_gpa=0x101000+cpu*128,saved_leaf=(native.IPA+0x95000)|attrs,
                fault_pc=native.CODE+16,resume_pc=native.CODE+16,queued=1,resumed=1,completed=1,
                new_leaf=(native.IPA+0x97000)|attrs,fault_frame=fault)
            owner['event']=dict(consumed=1,deliveries=1,boundary=4,blocked=34,wait_checks=1,shadow=0,priority=0,packet=completed)
            owner['walk']=dict(address=0x101000+cpu*128,gpa=0x103000+cpu*128,leaf_bytes=4096,writable=1,dirty=1,read_count=4,writes=0,
                reads=[[0x1000,0x2027],[0x2000,0x3027],[0x3000,0x4027],[0x4808,0x103067]])
        atomic['owners'].append(owner)
    report['atomic']=atomic
    return schedule,report,cfg


class AtomicProgress(unittest.TestCase):
    def test_retained_four_owner_large_seed_native_report(self):
        path=Path(__file__).with_name('testdata')/'i4v_atomic_n4_seed2.json.gz'
        schedule,report=[json.loads(line) for line in gzip.decompress(path.read_bytes()).splitlines()]
        result=oracle.atomic(schedule,report,configuration(3,4,2))
        self.assertEqual(result['native_faults'],35)
        self.assertEqual(result['later_remapped_trials'],31)
        altered=copy.deepcopy(report)
        altered['atomic']['owners'][0]['resume_pc']-=4
        with self.assertRaises(ValueError):oracle.atomic(schedule,altered,configuration(3,4,2))

    def test_all_declared_counts_rotations_and_seeds(self):
        for n in range(1,5):
            for ordinal in range(4):
                with self.subTest(n=n,ordinal=ordinal):
                    s,r,c=fixture(n,ordinal);result=oracle.atomic(s,r,c)
                    self.assertEqual(result['native_faults'],n+31)
                    self.assertEqual(result['decisions'],34*n)

    def test_ordinary_oracle_does_not_admit_composition(self):
        _,r,_=fixture()
        with self.assertRaises(ValueError):native.check(r)
        ordinary=report_fixture(9,2,0,37)
        self.assertEqual(native.check(ordinary)['native_faults'],32)

    def test_each_retained_owner_and_full_state_are_bound(self):
        s,r,c=fixture(4,3)
        paths=[]
        for cpu in range(4):
            paths.extend([('atomic','owners',cpu,k) for k in ('saved_gpa','saved_leaf','fault_pc','resume_pc','queued','resumed','completed','ack_leaf','new_leaf')])
            paths.extend([('atomic','owners',cpu,'event',k) for k in ('consumed','deliveries','boundary','blocked','wait_checks')])
            paths.extend([('atomic','owners',cpu,'walk',k) for k in ('gpa','read_count','writes')])
            paths.extend([('atomic','owners',cpu,'completed_guest','gpr',0),('atomic','owners',cpu,'event','packet','rip'),
                          ('atomic','owners',cpu,'fault_frame','x',30),('owners',cpu,'trials',0,'fault',0,'pc'),
                          ('owners',cpu,'trials',31,'captured','q',31,15)])
        for path in paths:
            with self.subTest(path=path):
                bad=copy.deepcopy(r);target=bad
                for k in path[:-1]:target=target[k]
                target[path[-1]]^=1
                with self.assertRaises(ValueError):oracle.atomic(s,bad,c)

    def test_physical_page_effects_and_guard_bytes(self):
        s,r,c=fixture(3,1)
        for group,indices in (('images',(0,1,31)),('cuts',(0,1,2))):
            for index in indices:
                for at in (0,4096,8192,12287):
                    bad=copy.deepcopy(r);target=bad if group=='images' else bad['atomic']
                    data=bytearray.fromhex(target[group][index]);data[at]^=1;target[group][index]=data.hex()
                    with self.subTest(group=group,index=index,at=at),self.assertRaises(ValueError):oracle.atomic(s,bad,c)

    def test_reordering_cannot_release_or_mutate_early(self):
        s,r,c=fixture(4,0);trace=r['atomic']['coordinator']['trace']
        def position(event):return next(i for i,w in enumerate(trace) if w>>8==event)
        for event,before in ((82,81),(85,84),(86,85),(88,87),(89,88),(90,0x201f),(83,4)):
            bad=copy.deepcopy(r);t=bad['atomic']['coordinator']['trace'];word=t.pop(position(event));t.insert(position(before),word)
            with self.subTest(event=event),self.assertRaises(ValueError):oracle.atomic(s,bad,c)
        for event in (81,83,84,87,89):
            bad=copy.deepcopy(r);t=bad['atomic']['coordinator']['trace'];del t[position(event)]
            bad['atomic']['coordinator']['trace_length']=len(t)
            with self.subTest(deleted=event),self.assertRaises(ValueError):oracle.atomic(s,bad,c)

    def test_scheduler_identity_eligibility_and_claims(self):
        s,r,c=fixture(3,2)
        for index in (0,3,6,len(s['decisions'])-1):
            for field in ('eligible','phase','epoch','chosen','claimed','prng_after','claim_trace_before','cut'):
                bad=copy.deepcopy(s);bad['decisions'][index][field]^=1
                with self.subTest(index=index,field=field),self.assertRaises(ValueError):oracle.atomic(bad,r,c)

    def test_later_actor_fault_not_silently_dropped(self):
        s,r,c=fixture(2,0);t=r['owners'][c['actor']]['trials'][31]
        t['faults']=0;t['fault']=[native.zero_fault(),native.zero_fault()]
        with self.assertRaises(ValueError):oracle.atomic(s,r,c)


if __name__=='__main__':unittest.main()
