import copy
import unittest

import i4v_controls_concurrent_check as concurrent
import i4v_controls_check as scalar
import i4v_pae_model as pae


def legacy_schedule(n=2,preselect=False):
    """A serialized continuation, optionally selected by a different waiter."""
    events=[]
    def emit(kind,owner,index=None):
        events.append(((kind if index is None else kind+index+1)<<8)|owner)
    def ordinary(owner,index):
        emit(0x1000,owner,index)
        if index in (3,6,9,12,14):emit(0x3000,owner,index);return
        memory=index in (1,4,7,10,13,16)
        fault=index in (1,4,7,10,13)
        if memory:emit(1,owner);emit(0x2000,owner,index)
        if fault:
            emit(2,owner);emit(0x5000,owner,index);emit(3,0);emit(4,owner)
        emit(0x4000,owner,index)
        if fault:emit(5,owner)
    for index in range(3):ordinary(0,index)
    emit(0x1000,0,3);emit(90,0)
    for owner in range(1,n):
        ordinary(owner,0)
        emit(0x1000,owner,1);emit(1,owner);emit(0x2000,owner,1);emit(94,owner)
    for owner in range(1,n):
        if n>=3 and owner==n-1:emit(95,owner)
        emit(2,owner);emit(0x5000,owner,1)
    emit(0x3000,0,3);emit(91,0)
    if preselect:emit(96,n-1)
    else:emit(96,1)
    emit(3,0);emit(4,1)
    if preselect:emit(96,1)
    for owner in range(1,n):
        if owner>1:
            if not preselect or owner!=n-1:emit(96,owner)
            emit(4,owner)
        emit(0x4000,owner,1);emit(5,owner);emit(97,owner)
    emit(93,0)
    for index in range(4,17):ordinary(0,index)
    emit(98,0)
    for owner in range(1,n):
        for index in range(2,17):ordinary(owner,index)
        emit(98,owner)
    return events


def single_owner_schedule(family,revision=0):
    """Finite scheduling fixture; instructions stay in their literal programs."""
    program=concurrent.owner_program(family,0,revision)
    instructions=program+([program[0],program[1]] if family==0 else
                          [program[4]] if family==21 else [program[0]] if family==24 else [])
    first={0:3,3:3,21:3,22:7,24:2}[family]
    second={0:6,21:6,22:8,24:3}.get(family)
    final=len(instructions)-(2 if family==0 else 1)
    events=[];mapped=False
    def emit(kind,index=None):events.append((kind if index is None else kind+index+1)<<8)
    for index,insn in enumerate(instructions):
        emit(0x1000,index)
        if index==final and family!=3:
            emit(0x7000,index)
            for event in (114,115,116,117,118,119):emit(event)
            emit(0x1000,index);mapped=False
            insn=scalar.immediate(0x11) if family==0 else b'\x89\x03'
        native=insn[0] in (0xb8,0x89,0x8b) or insn==b'\x48\x8b\x03'
        memory=insn in (b'\x89\x03',b'\x8b\x03',b'\x48\x8b\x03')
        if native:
            if memory:
                emit(1);emit(0x2000,index)
                if not mapped:
                    emit(2);emit(0x5000,index);emit(3);emit(4)
            emit(0x4000,index)
            if memory and not mapped:emit(5)
            if memory:mapped=True
        else:
            if index==first:emit(90)
            if index==second:emit(100)
            if insn[0]==0xea:emit(0x6000,index)
            if insn==b'\x48\xff\x2e':emit(0x6000,index);emit(0x6000,index)
            emit(0x3000,index)
            if insn[0]!=0xe9:mapped=False
            if index==first:emit(92 if family==3 else 91);emit(93)
            if index==second:emit(101)
        if family==21 and index in (4,7):
            for event in ((108,110,112) if index==4 else (109,111,113)):emit(event)
    emit(98)
    return events


def pressure_fixture(family=21):
    """Single-owner alternative schedule: seven heads, real work, two heads."""
    model=concurrent.ConcurrentReplay(family,1,0,71,[scalar.PRIVATE+0x80],revision=1,maintenance=True)
    trace=[]
    def emit(kind,index=None):
        word=(kind if index is None else kind+index+1)<<8
        model.step(word);trace.append(word)
    program=concurrent.owner_program(family,0,1)
    instructions=program+([program[0],program[1]] if family==0 else
                          [program[4]] if family==21 else [program[0]] if family==24 else [])
    gate={0:7,21:8,22:9,24:4}.get(family)
    for index,insn in enumerate(instructions):
        if gate is not None and index>=gate and not model.pressure['finished'] and model.pressure['ordinary']==concurrent.NO_CPU:
            for _ in range(7 if model.pressure['total']==0 else 2):
                for event in (23,25,26):emit(event)
            emit(29)
        emit(0x1000,index)
        if index==model.final_visit:
            emit(0x7000,index)
            for event in (114,115,116,117,118,119):emit(event)
            emit(0x1000,index)
            insn=scalar.immediate(0x11) if family==0 else b'\x89\x03'
        native=model.owners[0]['pending']['visit']['kind']==0
        memory=insn in (b'\x89\x03',b'\x8b\x03',b'\x48\x8b\x03')
        if native:
            if memory:emit(1);emit(0x2000,index)
            if model.owners[0]['event_due']:emit(20)
            fault=memory and model.owners[0]['pending']['needs_fault']
            if fault:
                for event in (2,):emit(event)
                emit(0x5000,index);emit(3);emit(4)
            emit(0x4000,index)
            if fault:emit(5)
            if model.pressure['ordinary']==0:
                emit(30)
                if model.owners[0]['event_due']:emit(20)
        else:
            if index==model.first_cut:emit(90)
            if index==model.second_cut:emit(100)
            if insn[0]==0xea:emit(0x6000,index)
            if insn==b'\x48\xff\x2e':emit(0x6000,index);emit(0x6000,index)
            emit(0x3000,index)
            if index==model.first_cut:emit(92 if family==3 else 91);emit(93)
            if index==model.second_cut:emit(101)
        if family==21 and index in (4,7):
            for event in ((108,110,112) if index==4 else (109,111,113)):emit(event)
    emit(98)
    if family==3:emit(20)
    # Replay from scratch; fixture construction never inserts reported guest
    # values into the architectural reference.
    model=concurrent.ConcurrentReplay(family,1,0,71,[scalar.PRIVATE+0x80],revision=1,maintenance=True).run(trace)
    return model,trace


def report_fixture(family=21):
    """Serialize the independently constructed schedule into the host schema."""
    from test_i4v_controls_check import fixture
    model,trace=pressure_fixture(family);s=model.owners[0]
    source,data,tables=concurrent.initial_memory(family,1,1)
    r=dict(case='controls-concurrent',profile=family,program_revision=1,configured_vcpus=1,tested_vcpus=1,
           actor=0,vm_nonce=71,source_before=source,source_after=model.source,data_before=data,data_after=model.data,
           coordinator=dict(trace=trace,trace_length=len(trace)),maintenance=model.pressure)
    for field,key in (('tables','legacy'),('pae_tables','pae'),('mode_tables','ia32e')):
        r[field+'_before']=tables[key];r[field+'_after']=model.tables[key]
    o=fixture(family)
    o.update(id=0,rip=scalar.SOURCE,vm_nonce=71,capture_pc=scalar.PRIVATE+0x80,
        initial=s['initial'],entered=s['entered'],final_frame=s['frame'],guest_before=s['initial_guest'],guest_after=s['guest'],
        bank_before=s['bank'],bank_after=s['bank'],code_after=s['code'],compiler_requests=s['packets'],
        requests=len(s['packets']),completed=s['completed'],stage=3 if family==3 else 2)
    st=o['controls'];st.update(generation=s['context'],visits=len(s['visits']),completed=s['completed'],
        transfers=len(s['packets']),serial=len(s['packets']),walks=s['walks'],invalidations=s['invalidations'],
        mapping=s['mapping'][0] if s['mapping'] else 0,stale_reply=s['stale_reply'],stale_entry=s['stale_entry'],
        expected=s['expected'],deferred=s['deferred'],late_reply=s['late_reply'],rebound_reply=s['rebound_reply'],
        old_slot_context=s['old_slot_context'],old_controls=s['old_controls'],initial=s['initial'],current=s['frame'])
    o['visits']=copy.deepcopy(s['visits'])
    empty={key:(scalar.zero_frame() if key in ('frame','fault_frame') else
                [0]*len(value) if isinstance(value,list) else 0) for key,value in s['visits'][0].items()}
    o['visits'] += [copy.deepcopy(empty) for _ in range(20-len(s['visits']))]
    faults=[v for v in s['visits'] if v['faults']]
    for key,value in zip(('esr','far','elr','spsr'),faults[-1]['exception'] if faults else [0]*4):o[key]=value
    o['lower_leaf_after']=[0]*512
    if s['mapping']:o['lower_leaf_after'][256]=scalar.descriptor((s['mapping'][0]-scalar.DATA)//4096,not s['mapping'][1])
    o['upper_root_after']=[0]*512;o['upper_root_after'][256]=0x40018003;o['upper_leaf_after']=[0]*512
    o['pae']=pae.empty_state();o['pae'].update(hidden=s['hidden'],visits=s['pae_visits'])
    for key in ('attempts','reads','walk_reads','walk_writes','mutations'):o['pae'][key]=s[key]
    o['modes']=s['modes'];zero=concurrent.snapshot(dict(control=[0]*8,segments=[[0]*4],rip=0,gpr=[0]*4),
        dict(entries=[0]*4,generation=0),0,0,0,0,[0]*4)
    o['owner']=dict(enabled=1,ttbr1=0x40017000,incarnation=1,source_generation=s['source_generation'],admitted=0,
        route=s['route'],writable=s['writable'],detached=0,local_cut=s['local_cut'],peer_reply_cut=0,peer_reply_executed=0,
        local_controls=s['local_controls'],source_rejections=s['source_rejections'],cached_before_source=s['cached_before_source'],
        source_entry_rejected=s['source_entry_rejected'],binding_rejections=0,retained_code=s['retained_code'],
        reuse_blocked=s['reuse_blocked'],released_code=int(0 in model.released),reclaimed=int(0 in model.reclaimed),
        maintenance_intervals=model.pressure['intervals'][0],maintenance_acks=s['maintenance_acks'],interval_visits=s['interval_visits'],
        semantic_admissions=s['semantic_admissions'],source_expected=s['source_expected'],source_reply=s['source_reply'],
        peer_expected=[0]*16,peer_reply=[0]*16,metadata_gpas=s['metadata_gpas'],code_before_retire=s['code_before_retire'],
        code_after_reclaim=s['code_after_reclaim'],initial=concurrent.snapshot(s['initial_guest'],dict(entries=[0]*4,generation=0),
        1,1,0,0,s['modes']['tr']),before_local=s['before_local'],after_local=s['after_local'],before_peer_reply=zero,after_peer_reply=zero)
    o['event']=dict(s['event'],wait_checks=1);o['intervals']=s['intervals']
    o['recovery']=dict(scenario=0,native_fpcr=s['initial']['fpcr'],native_fpsr=s['initial']['fpsr'],faults=0,walks=0,retries=0,
        vector=13 if family==3 else 0,error=0,linear=0,rip=s['visits'][-1]['rip'] if family==3 else 0,control=[0x1000,0,1,0,0],traces=[])
    mask=lambda values:sum(1<<cpu for cpu in values)
    r['concurrent']=dict(n=1,profile=family,actor=0,phase=3,ready=0,held=0,local_done=1,drained=0,peer_compiling=0,
        second_commit=int(model.second==2),peer_executed=0,final_ready=mask(model.final_ready),source_generation=model.source_generation,
        source_acknowledgements=mask(model.source_ack),finished=1,pae_ready=list(map(mask,model.pae_ready)),
        pae_acknowledgements=list(map(mask,model.pae_ack)),pae_published=mask(model.pae_published),
        before=[s['before_local']]+[zero]*3,after=[s['after_local']]+[zero]*3,semantic_active=0,
        maintenance_started=model.maintenance_started,maintenance_leaves=model.maintenance_leaves)
    r['owners']=[o]
    changed=model.source_generation==2
    r['coordinator'].update(participants=1,scenario=5,alias_profile=0,stack_profile=0,mixed_profile=0,mixed_compiled=0,
        ready=1,finished=0,lock=20,next_ticket=20,phase=0,active=0,queued=0,head=0,length=0,servicing=concurrent.NO_CPU,
        completed=0,retained=mask(model.retained),generation=model.source_generation,acknowledgements=mask(model.source_ack),
        new_entries=int(changed),new_completed=int(changed),new_references=0,reclaimed=int(changed),old_eligible=int(not changed),
        published=1,stale_replies=0,stale_entries=0,reuse_blocked=s['reuse_blocked'],translation_wait=0,queue=model.native_queue,
        event_pending=[2,0,0,0],event_notifications=[2,0,0,0])
    return copy.deepcopy(r)


class ConcurrentReferenceTests(unittest.TestCase):
    def test_owner_seeds_and_complete_shared_fixture(self):
        for family in concurrent.FAMILIES:
            source,data,tables=concurrent.initial_memory(family,4)
            self.assertEqual((len(source),len(data)),(4096,8192))
            for owner in range(4):
                frame,bank,guest,mode=concurrent.initial_owner(family,owner)
                self.assertEqual(frame['x'][6],scalar.DATA+owner*256)
                self.assertEqual(frame['x'][11],scalar.DATA+128)
                self.assertEqual(guest['rip'],scalar.SOURCE+owner*256)
                self.assertEqual(guest['rflags'],0x202)
                self.assertEqual(bank[0],19^owner)
                self.assertEqual(int.from_bytes(bytes(data[owner*256:owner*256+4]),'little'),0x11220000+owner)
                self.assertEqual(int.from_bytes(bytes(data[4096+owner*256:4100+owner*256]),'little'),0x33440000+owner)
                if family>=22:
                    gdt,pointer=concurrent.metadata(owner)
                    self.assertEqual(frame['x'][10],pointer)
                    self.assertEqual(guest['gpr'][6],pointer)
                    self.assertEqual(mode['gdtr'],[gdt,0x37])
                    self.assertEqual(data[gdt-scalar.DATA:gdt-scalar.DATA+0x38],
                                     data[4096+gdt-scalar.DATA:4096+gdt-scalar.DATA+0x38])
                    self.assertEqual(data[pointer-scalar.DATA:pointer-scalar.DATA+10],
                                     data[4096+pointer-scalar.DATA:4096+pointer-scalar.DATA+10])
    def test_relocated_far_targets_and_relative_backedges(self):
        for family in concurrent.FAMILIES:
            for owner in range(4):
                offset=0
                for original,relocated in zip(scalar.program(family),concurrent.owner_program(family,owner)):
                    rip=scalar.SOURCE+owner*256+offset
                    self.assertEqual(concurrent.instruction_at(family,owner,rip),relocated)
                    if original[:1]==b'\xea':
                        self.assertEqual(int.from_bytes(relocated[1:5],'little'),
                                         int.from_bytes(original[1:5],'little')+owner*256)
                    else:self.assertEqual(original,relocated)
                    offset+=len(original)
                self.assertLess(offset,256)

    def test_shared_ad_and_private_hidden_roots(self):
        tables=concurrent.table_images(21)
        guest=scalar.guest_before(21);guest['control'][0]=0x80010011;guest['control'][3]=0x620
        old=dict(entries=[0x2001,0,0,0],generation=1)
        peer=copy.deepcopy(old)
        pae.qword(tables['pae'],0x20,0x4001)
        guest['gpr'][0]=0x1020
        changed,error,addresses,_=concurrent.control_transition(tables,guest,old,3,b'\x0f\x22\xd8')
        self.assertIsNone(error);self.assertEqual(addresses,[0x1020,0x1028,0x1030,0x1038])
        actor,new,epoch=changed
        self.assertEqual(epoch,4)
        self.assertEqual(concurrent.route(tables,actor,new,scalar.DATA+256),(scalar.DATA+4096+256,None))
        self.assertEqual(concurrent.route(tables,guest,peer,scalar.DATA+512),(scalar.DATA+512,None))
        self.assertEqual(pae.qword(tables['pae'],0x20),0x4001)
        self.assertEqual(pae.qword(tables['pae'],0x2800),scalar.DATA|0x27)
        self.assertEqual(pae.qword(tables['pae'],0x4800),(scalar.DATA+4096)|0x27)
        self.assertEqual(old,peer)

    def test_compile_identity_and_disjoint_metadata(self):
        instruction=bytes.fromhex('8b03')
        for owner in range(4):
            rip=scalar.SOURCE+owner*256
            p=concurrent.request(71,owner,1,2,5,3,rip,instruction,9)
            self.assertEqual(p,[0x49345651,1,71,owner,1,2,5,rip,3,0x038b,2,rip,9,8,0,0])
            gdt,pointer=concurrent.metadata(owner)
            self.assertEqual((gdt,pointer),(scalar.DATA+0x800+owner*256,scalar.DATA+0x840+owner*256))
            self.assertGreaterEqual(gdt,scalar.DATA+4*256)
            self.assertLess(pointer+10,scalar.DATA+4096)
            with self.assertRaises(ValueError):
                concurrent.request(71,owner,1,2,5,3,rip+255,instruction,9)

    def test_snapshot_does_not_alias_owner_state(self):
        guest=scalar.guest_before(21);hidden=dict(entries=[0x2001,0,0,0],generation=1)
        snapshot=concurrent.snapshot(guest,hidden,3,1,4,scalar.DATA,[0]*4)
        guest['control'][0]=0;hidden['entries'][0]=0
        self.assertEqual(snapshot['control'][0],0x10011)
        self.assertEqual(snapshot['hidden'][0],0x2001)

    def test_ordered_trace_encoding_and_peer_isolation(self):
        for owner in range(4):
            for kind in (0x1000,0x2000,0x3000,0x4000,0x5000,0x6000,0x7000):
                self.assertEqual(concurrent.decode_event(((kind+20)<<8)|owner,4),(kind,owner,19))
        for word in ((0x8001<<8),(0x1000<<8),(0x1015<<8),(0x1001<<8)|4):
            with self.assertRaises(ValueError):concurrent.decode_event(word,4)
        guest=scalar.guest_before(21);hidden=dict(entries=[0x2001,0,0,0],generation=1)
        before=[concurrent.snapshot(guest,hidden,3,1,4,scalar.DATA+i*256,[0]*4) for i in range(4)]
        after=copy.deepcopy(before);after[0]['context']+=1
        concurrent.unchanged_peers(before,after,0,4)
        for field,value in (('context',4),('hidden_generation',2),('route',scalar.DATA+4096),
                            ('completed',5),('source_generation',2)):
            corrupted=copy.deepcopy(after);corrupted[2][field]=value
            with self.subTest(field=field),self.assertRaises(ValueError):
                concurrent.unchanged_peers(before,corrupted,0,4)

    def test_failed_control_preserves_owner_and_peer(self):
        tables=concurrent.table_images(3);guest=scalar.guest_before(3)
        guest['gpr'][0]=0x80010010
        snapshot=copy.deepcopy(guest);hidden=dict(entries=[0]*4,generation=0)
        result,error,addresses,_=concurrent.control_transition(tables,guest,hidden,1,b'\x0f\x22\xc0')
        self.assertEqual((result,error,addresses),(None,13,[]));self.assertEqual(guest,snapshot)

    def test_identity_bypasses_shared_tables_and_no_partial_acceptance(self):
        tables=concurrent.table_images(22);guest=scalar.guest_before(22)
        before=copy.deepcopy(tables)
        self.assertEqual(concurrent.route(tables,guest,dict(entries=[0]*4,generation=0),scalar.DATA+768),
                         (scalar.DATA+768,None))
        self.assertEqual(tables,before)
        with self.assertRaises(ValueError):concurrent.check({})

    def test_shared_legacy_trace_derives_full_effects_and_aba_packets(self):
        answer=concurrent.replay_legacy_trace(0,2,0,legacy_schedule(),[scalar.PRIVATE+0x80]*2,71)
        self.assertEqual(answer['trace_events'],150)
        for cpu,s in enumerate(answer['owners']):
            self.assertEqual((len(s['visits']),s['completed'],s['faults'],s['walks'],len(s['packets'])),
                             (17,17,5,12,13))
            self.assertEqual(s['guest']['rip'],scalar.SOURCE+cpu*256+7)
            self.assertEqual(s['guest']['gpr'][0],0x10011)
            self.assertEqual(s['visits'][7]['gpr_after'][0],0x33440000+cpu)
            self.assertEqual(s['visits'][1]['fault_frame']['x'][4],0x10011)
            self.assertEqual(s['visits'][1]['memory_before'][:4],list((0x11220000+cpu).to_bytes(4,'little')))
            self.assertEqual(answer['data'][cpu*256:cpu*256+4],[17,0,1,0])
            self.assertEqual(s['rebound'][6],1)
            self.assertEqual(s['visits'][15]['reply'][6],5)
        self.assertEqual(answer['owners'][1]['before_local'],answer['owners'][1]['after_local'])
        self.assertEqual(answer['owners'][0]['before_local']['context'],1)
        self.assertEqual(answer['owners'][0]['after_local']['context'],2)
        self.assertEqual(scalar.word(answer['tables']['legacy'],0x1400),scalar.DATA|0x27)
        self.assertEqual(scalar.word(answer['tables']['legacy'],0x3400),(scalar.DATA+4096)|0x27)

    def test_development_replay_rejects_fabricated_cut_or_memory_order(self):
        original=legacy_schedule()
        corruptions=[]
        # Missing detached route, repeated generated completion, and an owner
        # resuming service before the actor's local transaction are distinct.
        missing=list(original);missing.remove((0x5002<<8)|1);corruptions.append(missing)
        duplicate=list(original);at=duplicate.index((0x4002<<8)|1)
        duplicate.insert(at,(0x4002<<8)|1);corruptions.append(duplicate)
        early=list(original);early.remove((96<<8)|1);corruptions.append(early)
        incomplete=list(original);incomplete.pop();corruptions.append(incomplete)
        new_phase=list(original);new_phase.insert(-1,(114<<8)|1);corruptions.append(new_phase)
        for trace in corruptions:
            with self.assertRaises(ValueError):
                concurrent.replay_legacy_trace(0,2,0,trace,[scalar.PRIVATE+0x80]*2,71)
        with self.assertRaises(ValueError):
            concurrent.replay_legacy_trace(22,2,0,original,[scalar.PRIVATE+0x80]*2,71)

    def test_actual_semantic_commit_cannot_precede_retained_peer_cut(self):
        original=legacy_schedule()
        model=concurrent.ConcurrentReplay(0,2,0,71,[scalar.PRIVATE+0x80]*2,source_control='none',peer_reply_control=False)
        model.run(original)
        moved=list(original);commit=0x3004<<8;moved.remove(commit)
        moved.insert(moved.index(90<<8)+1,commit)
        with self.assertRaises(ValueError):
            concurrent.ConcurrentReplay(0,2,0,71,[scalar.PRIVATE+0x80]*2,source_control='none',peer_reply_control=False).run(moved)
        # Even after the peer queues, the actual native fault record must
        # precede the actor transaction; a later notification is insufficient.
        moved=list(original);moved.remove(commit)
        moved.insert(moved.index((2<<8)|1)+1,commit)
        with self.assertRaises(ValueError):
            concurrent.ConcurrentReplay(0,2,0,71,[scalar.PRIVATE+0x80]*2,source_control='none',peer_reply_control=False).run(moved)

    def test_fifo_preselection_is_allowed_but_early_peer_execution_is_not(self):
        trace=legacy_schedule(3,preselect=True)
        self.assertLess(trace.index((4<<8)|1),trace.index((96<<8)|1))
        concurrent.ConcurrentReplay(0,3,0,71,[scalar.PRIVATE+0x80]*3,
            source_control='none',peer_reply_control=False).run(trace)
        concurrent.replay_legacy_trace(0,3,0,trace,[scalar.PRIVATE+0x80]*3,71)
        invalid=list(trace);check=(96<<8)|1;invalid.remove(check)
        invalid.insert(invalid.index((0x4002<<8)|1)+1,check)
        with self.assertRaises(ValueError):
            concurrent.ConcurrentReplay(0,3,0,71,[scalar.PRIVATE+0x80]*3,
                source_control='none',peer_reply_control=False).run(invalid)
        with self.assertRaises(ValueError):
            concurrent.replay_legacy_trace(0,3,0,invalid,[scalar.PRIVATE+0x80]*3,71)

    def test_concurrent_only_program_revision_preserves_original_inputs(self):
        for family,extra in ((0,0),(3,0),(21,3),(22,3),(24,1)):
            old=concurrent.owner_program(family,0)
            revised=concurrent.owner_program(family,0,1)
            self.assertEqual(old,scalar.program(family))
            self.assertEqual(len(revised),len(old)+extra)
            if family in (21,24):
                self.assertEqual(sum(map(len,revised))+int.from_bytes(revised[-1][1:],'little',signed=True),
                                 16 if family==21 else 0)
        self.assertEqual(concurrent.owner_program(21,0,1)[8:11],[scalar.immediate(v) for v in (0,0x100,0x1020)])
        self.assertEqual(concurrent.owner_program(22,0,1)[12:15],[scalar.immediate(v) for v in (0,0x100,0x10011)])
        self.assertEqual(concurrent.owner_program(24,0,1)[7],scalar.immediate(0))

    def test_literal_single_owner_programs_with_source_and_hidden_root_cuts(self):
        for revision in (0,1):
            for family in concurrent.FAMILIES:
                with self.subTest(family=family,revision=revision):
                    model=concurrent.ConcurrentReplay(family,1,0,71,[scalar.PRIVATE+0x80],revision=revision)
                    model.run(single_owner_schedule(family,revision));s=model.owners[0]
                    if family==3:
                        self.assertEqual((s['completed'],s['guest']['control'][0],s['visits'][-1]['vector']),
                                         (3,0x10011,13))
                        self.assertEqual(model.source_generation,1)
                    else:
                        self.assertEqual((s['source_rejections'],s['cached_before_source'],s['source_entry_rejected']),
                                         (1,1,1))
                        self.assertEqual(model.reclaimed,{0});self.assertEqual(s['code_after_reclaim'],[0]*640)
                        value={0:0x11,21:0x1020,22:0x10011,24:0x10011}[family]
                        self.assertEqual(int.from_bytes(bytes(model.data[:4]),'little'),value)
                        old_insn=scalar.immediate(0x10011) if family==0 else b'\x8b\x03'
                        offset=model.final_visit*32;native=scalar.body(old_insn)+scalar.RET
                        self.assertEqual(s['code_before_retire'][offset:offset+len(native)],list(native))
                        self.assertEqual(s['source_expected'][5],1)
                        self.assertEqual(s['visits'][model.final_visit]['request'][5],2)
                    if family==21:
                        self.assertEqual((s['attempts'],s['reads'],s['mutations']),(3,12,2))
                        self.assertEqual(s['hidden'],dict(entries=[0x2001,0,0,0],generation=3))
                        self.assertEqual(s['visits'][7]['gpr_after'][0],0x33440000)
                        self.assertEqual(pae.qword(model.tables['pae'],0x20),0x2001)
                        self.assertEqual(pae.qword(model.tables['pae'],0x2800),scalar.DATA|0x67)
                    if family>=22:
                        self.assertEqual((s['modes']['reads'],s['modes']['pointer_reads'],s['modes']['descriptor_reads']),(3,1,2))
                        self.assertEqual(s['guest']['segments'][0],[8,0,0xffffffff,0xc09b])

    def test_source_reclamation_and_hidden_snapshot_corruption_controls(self):
        schedule=single_owner_schedule(21)
        for missing in ((0x700c<<8),(116<<8),(118<<8),(112<<8)):
            broken=list(schedule);broken.remove(missing)
            with self.subTest(missing=missing),self.assertRaises(ValueError):
                concurrent.ConcurrentReplay(21,1,0,71,[scalar.PRIVATE+0x80]).run(broken)
        # A raw PDPT write must not mutate the hidden roots. Code cannot change
        # while an admitted old reference still blocks reclamation.
        for event,corrupt in ((110,lambda s:s['hidden']['entries'].__setitem__(0,0x4001)),
                              (114,lambda s:s['code'].__setitem__(0,255))):
            model=concurrent.ConcurrentReplay(21,1,0,71,[scalar.PRIVATE+0x80])
            at=schedule.index(event<<8)
            for word in schedule[:at+1]:model.step(word)
            corrupt(model.owners[0])
            with self.subTest(event=event),self.assertRaises(ValueError):model.run(schedule[at+1:])

    def test_full_pressure_report_all_families_and_corruption_controls(self):
        for family in concurrent.FAMILIES:
            with self.subTest(family=family):
                r=report_fixture(family)
                result=concurrent.audit_development(r,revision=1,maintenance=True)
                self.assertEqual(result['maintenance_heads'],0 if family==3 else 9)
                self.assertEqual(result['ordinary_intervals'],0 if family==3 else 2)
                self.assertEqual(result['event_deliveries'],1)
        original=report_fixture()
        changes=[(('owners',0,'owner','maintenance_acks',0,1),original['owners'][0]['owner']['maintenance_acks'][0][1]^4096),
                 (('owners',0,'owner','maintenance_acks',0,4),99),
                 (('owners',0,'owner','interval_visits',0),1),
                 (('owners',0,'intervals',0,'initial','x',4),99),
                 (('owners',0,'intervals',0,'before',255),99),
                 (('owners',0,'event','packet','control',2),0),
                 (('owners',0,'event','packet','rflags'),2),
                 (('owners',0,'event','deliveries'),2),
                 (('owners',0,'pae','hidden','generation'),1),
                 (('maintenance','request_history',0,'serial'),99),
                 (('maintenance','duplicate_rejections'),0),
                 (('coordinator','event_notifications',0),1),
                 (('coordinator','new_references'),1),
                 (('coordinator','active'),1),
                 (('program_revision',),0),
                 (('concurrent','maintenance_leaves',0,2),0),
                 (('owners',0,'owner','code_before_retire',0),99),
                 (('owners',0,'owner','code_after_reclaim',0),99)]
        for path,value in changes:
            corrupted=copy.deepcopy(original);target=corrupted
            for key in path[:-1]:target=target[key]
            target[path[-1]]=value
            with self.subTest(path=path),self.assertRaises(ValueError):
                concurrent.audit_development(corrupted,revision=1,maintenance=True)

    def test_event_handoff_rechecks_current_exclusion(self):
        for family in (0,22,24):
            r=report_fixture(family);trace=r['coordinator']['trace'];delivery=trace.index(20<<8)
            later_head=next(i for i in range(delivery+1,len(trace)) if trace[i]==25<<8)
            trace.insert(later_head+1,trace.pop(delivery))
            with self.subTest(family=family),self.assertRaises(ValueError):
                concurrent.audit_development(r,revision=1,maintenance=True)

    def test_strict_complete_admission_and_external_bindings(self):
        for family in concurrent.FAMILIES:
            result=concurrent.check(report_fixture(family),expected_profile=family,
                expected_vcpus=1,expected_actor=0,expected_revision=1)
            self.assertEqual(result['event_deliveries'],1)
        original=report_fixture(22)
        for arguments in (dict(expected_profile=21),dict(expected_vcpus=2),dict(expected_actor=1),
                          dict(expected_revision=0),dict(expected_actor=False)):
            with self.subTest(arguments=arguments),self.assertRaises(ValueError):
                concurrent.check(original,**arguments)
        for key,value in (('profile',True),('configured_vcpus',True),('tested_vcpus',1.0),
                          ('actor',False),('program_revision',0),('vm_nonce',0),('owners',{}),
                          ('coordinator',[]),('case','controls32')):
            bad=copy.deepcopy(original);bad[key]=value
            with self.subTest(key=key,value=value),self.assertRaises(ValueError):concurrent.check(bad)
        for mutation in (lambda r:r.pop('program_revision'),lambda r:r.update(unknown_evidence=1),
                         lambda r:r['owners'][0].update(capture_pc=True),
                         lambda r:r['owners'][0].update(owner=[]),
                         lambda r:r['owners'][0]['owner'].update(cached_before_source=0),
                         lambda r:r['owners'][0]['event'].update(deliveries=0),
                         lambda r:r['maintenance'].update(finished=0)):
            bad=copy.deepcopy(original);mutation(bad)
            with self.assertRaises(ValueError):concurrent.check(bad)


if __name__=='__main__':unittest.main()
