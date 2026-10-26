"""Corruption controls for frozen native prefixes and owned cleanup evidence."""
import copy
import gzip
import json
from pathlib import Path
import tempfile
import unittest

import check_i4v_containment as oracle
import i4v_controls_concurrent_check as controls
import i4v_controls_check as scalar
from test_i4v_controls_concurrent_check import report_fixture


def frozen_control(failure=3):
    cfg=oracle.configuration(failure,1,0);report=report_fixture(21)
    trace=report['coordinator']['trace']
    last=next(i for i,w in enumerate(trace) if w>>8==(115 if failure==4 else 114))
    trace=trace[:last+1]
    model=controls.ConcurrentReplay(21,1,0,report['vm_nonce'],[report['owners'][0]['capture_pc']],
        source_control='positive',revision=1,maintenance=True,peer_reply_control=True,capacity=1)
    for word in trace:model.step(word)
    trace += [161<<8,160<<8]
    report['coordinator'].update(trace=trace,trace_length=len(trace),phase=model.phase,active=0,queued=0,
        retained=1,acknowledgements=0,servicing=oracle.NONE,new_entries=0,new_references=0,reclaimed=0,
        finished=0,event_pending=[1,0,0,0],event_notifications=[3,0,0,0])
    report['source_after']=model.source;report['data_after']=model.data;report['maintenance']=model.pressure
    for key,table in (('tables','legacy'),('pae_tables','pae'),('mode_tables','ia32e')):
        report[key+'_after']=model.tables[table]
    o=report['owners'][0];owner=model.owners[0]
    o.update(guest_after=owner['guest'],final_frame=owner['frame'],code_after=owner['code'],completed=owner['completed'])
    o['controls'].update(generation=owner['context'],visits=len(owner['visits'])+1,
                         completed=owner['completed'],current=owner['frame'])
    for key in ('walks','invalidations','stale_reply','stale_entry','deferred','late_reply',
                'rebound_reply','old_slot_context','old_controls'):o['controls'][key]=owner[key]
    o['controls']['mapping']=owner['mapping'][0] if owner['mapping'] else 0
    import i4v_pae_model as pae
    o['pae']=pae.empty_state();o['pae'].update(hidden=owner['hidden'],visits=copy.deepcopy(owner['pae_visits']))
    for key in ('attempts','reads','walk_reads','walk_writes','mutations'):o['pae'][key]=owner[key]
    o['pae']['visits'][owner['pending']['index']]=copy.deepcopy(owner['pending']['pae'])
    o['modes']=copy.deepcopy(owner['modes'])
    o['owner'].update(retained_code=1,released_code=0,reclaimed=0)
    pending=copy.deepcopy(owner['pending']['visit'])
    for key in ('after','gpr_after'):pending[key]=[0]*(8 if key=='after' else 4)
    pending['generation_after']=0;pending['frame']=scalar.zero_frame()
    o['visits']=copy.deepcopy(owner['visits'])+[pending]+o['visits'][len(owner['visits'])+1:]
    empty={key:(scalar.zero_frame() if key in ('frame','fault_frame') else
        [0]*len(value) if isinstance(value,list) else 0) for key,value in pending.items()}
    o['visits'][len(owner['visits'])+1:]=[empty]*(19-len(owner['visits']))
    o['lower_leaf_after']=[0]*512
    if owner['mapping']:o['lower_leaf_after'][256]=scalar.descriptor((owner['mapping'][0]-controls.DATA)//4096,not owner['mapping'][1])
    packet=list(owner['source_expected'])
    if failure in (1,2,5):packet[12]+=1
    witness=dict(site=2 if failure==4 else 1,owner=0,phase=model.phase,active=0,queued=0,
        retained=1,acknowledgements=0,servicing=oracle.NONE,lock=10,next_ticket=11,trace_length=len(trace),
        ready_owners=1,pending_events=1,profile=21,visit=len(owner['visits'])+1,completed=owner['completed'],
        context=owner['context'],source_generation=model.source_generation,
        deferred_started=int(failure in (1,2,5)),producer_ready=int(failure==2),container_ready=int(failure==5),helper=[0]*8,
        packet=packet,guest=owner['guest'],frame=owner['frame'],code=owner['code'])
    return copy.deepcopy((report,cfg,witness))


def frozen_partial():
    import check_i4v_partial_progress as partial
    import check_i4v_coherence as coherence
    import i4v_coherence_model as mmu
    cfg=oracle.configuration(5,2,0);sc=oracle.scheduler(cfg)
    tables={0x1000:0x2027,0x2000:0x3027,0x3000:0x4027,0x4800:0x100067,0x4810:0x102027}
    def walk(address,intent):
        result=mmu.walk(tables,mmu.Context('four',0x1000,nxe=True),address,intent)
        out=coherence.zero_walk();out.update(address=address,intent=('read','write','fetch').index(intent),
            read_count=len(result.reads),write_count=len(result.writes),vector=result.vector,error=result.error)
        out['reads'][:len(result.reads)]=[[at,tables[at]] for at in result.reads]
        out['writes'][:len(result.writes)]=list(map(list,result.writes))
        for key in ('gpa','leaf_bytes','writable','dirty','user','executable','wb'):
            out[key]=int(getattr(result.route,key))
        return out
    zero=scalar.zero_frame();owners=[];before=[0xa5]*4096+[0x5a]*4096;after=list(before)
    source=[0xcc]*4096
    for cpu in range(2):
        address=partial.DATA+cpu*256;source[cpu*256:cpu*256+2]=[0xf3,0xa4]
        frame=partial.native_seed(cpu);fault_frame=copy.deepcopy(frame)
        fault_frame['x'][0]=address+1;fault_frame['x'][1]=0x6e+cpu*32;fault_frame['sp']=0x800040052000
        guest=partial.guest(cpu);held=partial.advanced(guest,1);bank=[((i*37+19)&255)^cpu for i in range(512)]
        pc=0x8000400a1000
        fault=dict(frame=fault_frame,esr=(0x25<<26)|64|7,far=address+1,pc=pc,
            spsr=fault_frame['nzcv']|0x3c4,resume_pc=0,value=0x6e+cpu*32,gpa=address+1)
        empty_fault={key:(copy.deepcopy(zero) if key=='frame' else 0) for key in fault}
        walks=[walk(partial.RIP+cpu*256,'fetch'),walk(address,'write'),walk(address+1,'write')]
        if cpu==0:walks.append(walk(partial.RIP+16,'fetch'))
        count=len(walks);walks += [coherence.zero_walk()]*(5-count)
        owners.append(dict(id=cpu,phase=1,reads=2,writes=1,read_values=[0x57+cpu*32,0x6e+cpu*32],
            sampled=0x6e+cpu*32,subaccess=1,source_route=partial.DEVICE+cpu*256+1,
            destination_route=address+1,destination_writable=1,retained=1,released=0,faults=1,retries=0,
            probe_writes=0,probe_route=0,notifications=2,pending=1,event_attempts=1,event_blocked=1,
            delivered=0,delivery_state=[0]*5,walk_count=count,initial=frame,before=guest,held=held,
            resumed=guest,after=held,bank_before=bank,bank_after=bank,native_leaf=[0]*512,
            captured=fault_frame,fault=[fault,empty_fault],walks=walks,store_pc=pc,
            store_bytes=list(bytes.fromhex('01000039c0035fd6')),status=0,detail=0,
            current_el=4,tcr=0x2b5103510,sctlr=0x30d01985,actlr=2))
        after[cpu*256]=0x57+cpu*32
    source[16:19]=[0x48,0x8b,0x03]
    trace=[]
    def emit(event,cpu):trace.append(event<<8|cpu)
    for cpu in (1,0):
        for event in (0x1000,0x1001,1,155,155,140):emit(event,cpu)
    for cpu in (1,0):
        for event in (141,0x1002,141,2,142,152):emit(event,cpu)
    for event in (143,0x1003,160):emit(event,0)
    common=dict(n=2,actor=0,ordinal=0,ready=3,mutation_blocked=1,helper_done=0,released=0,mutated=0,
        acknowledgements=0,admission_blocked=0,opened=0,probes_done=0,delivered=0,finished=0,
        images=1,source_generation=1,destination_generation=1,pending_events=3)
    coord=dict(trace=trace,trace_length=len(trace),phase=1,active=0,queued=3,retained=3,
        servicing=oracle.NONE,acknowledgements=0,new_entries=0,reclaimed=0,finished=0)
    report=dict(case='partial-progress',n=2,ordinal=0,actor=0,vm_nonce=37,owners=owners,
        common=common,coordinator=coord,data_before=before,data_after=after,source_before=source,
        source_after=source,tables_before=partial.table_image(tables),tables_after=partial.table_image(tables),
        images=[bytes(after).hex()]+[bytes(8192).hex()]*2)
    witness=dict(site=3,owner=0,phase=1,active=0,queued=3,retained=3,acknowledgements=0,servicing=oracle.NONE,
        lock=10,next_ticket=11,trace_length=len(trace),ready_owners=3,pending_events=3,profile=14,visit=1,
        completed=1,context=1,source_generation=1,deferred_started=1,producer_ready=0,container_ready=1,
        helper=[2,1,1,1,0x6e,0x500001,0x100001,1],
        packet=controls.request(37,0,1,1,1,4,partial.RIP+16,b'\x48\x8b\x03',1),
        guest=owners[0]['held'],frame=owners[0]['fault'][0]['frame'],code=[0]*640)
    return copy.deepcopy((report,cfg,witness))


class ContainmentTests(unittest.TestCase):
    def test_actual_panic_prefix_and_native_admission_corruptions(self):
        path=Path(__file__).parent/'testdata/i4v_containment_panic_n1.json.gz'
        records=[json.loads(line) for line in gzip.decompress(path.read_bytes()).splitlines() if line.strip()]
        cfg=oracle.configuration(3,1,0)
        self.assertEqual(oracle.check(records,cfg)['prefix'],'validated')
        for key in ('current_el','sctlr','actlr','tcr','service_pc','capture_pc'):
            with self.subTest(key=key):
                changed=copy.deepcopy(records);changed[2]['owners'][0][key]=0
                with self.assertRaises(ValueError):oracle.check(changed,cfg)
        with self.assertRaises(ValueError):oracle.check(records,oracle.configuration(3,2,0))

    def test_both_prefixes_preserve_machine_and_resident_pc_admission(self):
        for fixture,audit,keys in ((frozen_control,oracle.controls_prefix,('capture_pc','service_pc')),
                                  (frozen_partial,oracle.partial_prefix,('store_pc',))):
            for key in ('current_el','sctlr','actlr','tcr'):
                for value in (0,-1,1<<64,True):
                    with self.subTest(fixture=fixture.__name__,key=key,value=value):
                        r,c,w=fixture();r['owners'][-1][key]=value
                        with self.assertRaises(ValueError):audit(r,c,w)
            for key in keys:
                for value in (0,0x800040026000,0x8000400a0001,0x8000400e0000):
                    with self.subTest(fixture=fixture.__name__,key=key,value=value):
                        r,c,w=fixture();r['owners'][-1][key]=value
                        with self.assertRaises(ValueError):audit(r,c,w)
        r,c,w=frozen_partial();r['owners'][0]['store_pc']=0x8000400dfffc
        with self.assertRaises(ValueError):oracle.partial_prefix(r,c,w)
        r,c,w=frozen_control();r['owners'][0]['sctlr']|=2
        with self.assertRaises(ValueError):oracle.controls_prefix(r,c,w)

    def test_partial_cancel_keeps_device_sample_and_committed_prefix(self):
        model=oracle.partial_prefix(*frozen_partial())
        self.assertEqual((model.samples,model.retries,model.retained),([2,2],[0,0],3))
        changes={
            'repeat device read':lambda r,w:r['owners'][0].__setitem__('reads',3),
            'invent second write':lambda r,w:r['owners'][1].__setitem__('writes',2),
            'changed sample':lambda r,w:r['owners'][0].__setitem__('sampled',0x85),
            'advanced RIP':lambda r,w:r['owners'][0]['after'].__setitem__('rip',0x102002),
            'fake resume':lambda r,w:r['owners'][0]['fault'][0].__setitem__('resume_pc',r['owners'][0]['store_pc']),
            'cancelled write':lambda r,w:r['data_after'].__setitem__(1,0x6e),
            'remap':lambda r,w:r['common'].__setitem__('mutated',1),
            'extra fetch bypass':lambda r,w:r['owners'][0]['walks'][3].__setitem__('address',0x102000),
            'early event delivery':lambda r,w:r['owners'][1].__setitem__('delivered',1),
        }
        for name,change in changes.items():
            with self.subTest(name=name):
                r,c,w=frozen_partial();change(r,w)
                with self.assertRaises(ValueError):oracle.partial_prefix(r,c,w)
    def test_exact_twenty_five_rotations_and_one_partial_case(self):
        accepted=[]
        for failure in range(7):
            for n in range(6):
                for actor in range(6):
                    try:accepted.append(oracle.configuration(failure,n,actor))
                    except ValueError:pass
        self.assertEqual(len(accepted),25)
        self.assertEqual([c for c in accepted if c['family']==4],
                         [dict(failure=5,family=4,n=2,actor=0)])
        for config in accepted:
            s=oracle.scheduler(config)
            self.assertEqual((s['family'],s['n'],s['actor']),
                             (config['family'],config['n'],config['actor']))

    def test_full_control_prefix_is_reconstructed_at_source_and_ack_gates(self):
        for failure in range(1,6):
            with self.subTest(failure=failure):
                model=oracle.controls_prefix(*frozen_control(failure))
                self.assertEqual(model.retained,{0})

    def test_truncated_scheduler_still_binds_every_prefix_operation(self):
        import check_i4v_schedule as core
        from test_check_i4v_progress_protocols import record,event_for
        for failure in (3,4):
            native,cfg,witness=frozen_control(failure)
            model=oracle.controls_prefix(native,cfg,witness);sc=oracle.scheduler(cfg)
            snapshots,states,entries=model.progress;trace=native['coordinator']['trace']
            grants=[];events=[]
            for at,word in enumerate(trace):
                kind=word>>8;shot=snapshots[at];state=shot['queue']
                if kind in (1,2):
                    visit=shot['owners'][0]['index']+1
                    key=(core.ADMIT if kind==1 else core.DETACH)|0x10000|(visit<<8)
                    grants.append((key,at,shot['phase'],shot['owners'][0]['context']))
                elif kind in (112,113,26):
                    key=core.ACK|(kind-112) if kind in (112,113) else core.MAINTENANCE_ACK|state['epoch']
                    grants.append((key,at,shot['phase'],kind-111 if kind in (112,113) else state['epoch']))
                if kind==23:events.append(event_for(states[at],0,1,at))
            schedule=record(sc,grants,events)
            self.assertGreater(oracle.scheduling_prefix(schedule,native,cfg,model),0)
            for field in ('phase','epoch','eligible','chosen','trace_before','claimed'):
                bad=copy.deepcopy(schedule);bad['decisions'][0][field]^=1
                with self.subTest(failure=failure,field=field):
                    with self.assertRaises(ValueError):oracle.scheduling_prefix(bad,native,cfg,model)
            bad=copy.deepcopy(schedule);bad['decisions'].pop();bad['summary']['decision_count']-=1
            with self.assertRaises(ValueError):oracle.scheduling_prefix(bad,native,cfg,model)

    def test_prefix_mode_never_weakens_healthy_completion_or_unclaimed_grants(self):
        import check_i4v_schedule as core
        from test_check_i4v_schedule import example
        record,cfg,trace=example()
        record['decisions']=record['decisions'][:2];trace=trace[:2]
        for decision in record['decisions']:decision['arrived']=5
        record['summary'].update(draws=2,decision_count=2,prng=record['decisions'][-1]['prng_after'])
        record['cuts'][0].update(arrived=5,completed=5)
        contract=lambda key,at,done:(7,7,0,1,1)
        self.assertEqual(core.decisions(record,cfg,trace,contract,{core.ADMIT},prefix={core.ADMIT:5}),2)
        with self.assertRaises(ValueError):core.decisions(record,cfg,trace,contract,{core.ADMIT})
        for mutation in ('unclaimed','arrived not consumed','wrong completed set'):
            bad=copy.deepcopy(record);prefix={core.ADMIT:5}
            if mutation=='unclaimed':bad['decisions'][-1]['claimed']=0
            elif mutation=='arrived not consumed':bad['decisions'][-1]['arrived']=7
            else:prefix[core.ADMIT]=1
            with self.subTest(mutation=mutation):
                with self.assertRaises(ValueError):core.decisions(bad,cfg,trace,contract,{core.ADMIT},prefix=prefix)

    def test_frozen_witness_cannot_replace_native_prefix(self):
        changes={
            'guest':lambda r,w:r['owners'][0]['guest_after']['gpr'].__setitem__(0,99),
            'native SIMD':lambda r,w:r['owners'][0]['final_frame']['q'][5].__setitem__(0,99),
            'memory':lambda r,w:r['data_after'].__setitem__(900,99),
            'table':lambda r,w:r['pae_tables_after'].__setitem__(32,99),
            'code':lambda r,w:r['owners'][0]['code_after'].__setitem__(0,99),
            'released reference':lambda r,w:r['owners'][0]['owner'].__setitem__('released_code',1),
            'pretend final completion':lambda r,w:r['owners'][0]['visits'][14].__setitem__('generation_after',8),
            'extra trace':lambda r,w:(r['coordinator']['trace'].append(118<<8),r['coordinator'].__setitem__('trace_length',len(r['coordinator']['trace']))),
            'wrong identity':lambda r,w:w['packet'].__setitem__(6,w['packet'][6]-1),
            'no actual pending request':lambda r,w:r['coordinator'].__setitem__('event_pending',[2,0,0,0]),
            'self-reported released state':lambda r,w:w.__setitem__('retained',0),
        }
        for name,change in changes.items():
            with self.subTest(name=name):
                report,cfg,witness=frozen_control();change(report,witness)
                with self.assertRaises(ValueError):oracle.controls_prefix(report,cfg,witness)

    def test_supervision_preserves_original_cause_and_hard_deadlines(self):
        for failure in range(1,6):
            cfg=oracle.configuration(failure,2,1)
            cause={1:'control worker failed',2:'control reply changed request or emitted body',
                3:'resident owner panic detail=880',4:'containment gate stalled before three-second deadline',
                5:'explicit cancellation at retained native gate'}[failure]
            statuses=[[i,4 if failure==3 and i==1 else 0,880 if failure==3 and i==1 else 0] for i in range(2)]
            s=dict(supervision=dict(host_pid=47,joined=2,completions=2,destroyed=[[0,0],[1,0]],
                statuses=statuses,cancellations=[0],error=cause,elapsed_ms=5000))
            r=dict(host_pid=47,observation=dict(gate_seen=True,gate_elapsed_ms=1000,
                detected_elapsed_ms=3800,stop_elapsed_ms=5001,cause=cause))
            native=dict(owners=[dict(id=i,status=status,detail=detail) for i,status,detail in statuses])
            oracle.supervision(s,r,native,cfg)
            for key,value in (('detected_elapsed_ms',4001),('stop_elapsed_ms',11001),
                              ('cause','cleanup failed'),('gate_seen',False)):
                bad=copy.deepcopy(r);bad['observation'][key]=value
                with self.subTest(failure=failure,key=key):
                    with self.assertRaises(ValueError):oracle.supervision(s,bad,native,cfg)
            for key,value in (('joined',1),('completions',1),('destroyed',[[0,0],[1,1]]),
                              ('error','secondary cleanup error'),('host_pid',99)):
                bad=copy.deepcopy(s);bad['supervision'][key]=value
                with self.subTest(failure=failure,key=key):
                    with self.assertRaises(ValueError):oracle.supervision(bad,r,native,cfg)

    def test_owned_cleanup_requires_reap_and_all_precreated_registrations(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'containers').mkdir()
            token='ab'*16
            records={
                'job.json':dict(schema='natemu-owned-worker-v1',token=token,host_pid=17),
                'worker.json':dict(token=token,pid=71,pgid=71),
                'process.json':dict(token=token,pid=71,group_gone=True,reaped=True),
                'containers/natemu-test.json':dict(token=token,name='natemu-test',state='removed',container_id='cd'*32),
                'cleanup.json':dict(token=token,complete=True,containers=[dict(record='natemu-test.json',name='natemu-test',complete=True)]),
            }
            def save(values):
                for key,value in values.items():(root/key).write_text(json.dumps(value))
            save(records);self.assertEqual(oracle.owned_job(root)['job']['token'],token)
            mutations=(('process.json','reaped',False),('process.json','group_gone',False),
                       ('cleanup.json','complete',False),('cleanup.json','containers',[]),
                       ('containers/natemu-test.json','token','cd'*16),
                       ('containers/natemu-test.json','state','creating'))
            for path,key,value in mutations:
                with self.subTest(path=path,key=key):
                    bad=copy.deepcopy(records);bad[path][key]=value;save(bad)
                    with self.assertRaises(ValueError):oracle.owned_job(root)
            save(records);(root/'worker.json').unlink()
            with self.assertRaises(ValueError):oracle.owned_job(root)


if __name__=='__main__':unittest.main()
