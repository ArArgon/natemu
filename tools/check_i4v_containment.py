"""Independent frozen-prefix and owned-resource checks for injected VM failures.

Architectural replay stops at the real gate. A stopped failure is not a healthy
completed emulator run; fresh-control validation is a separate required proof.
"""
import copy
import json
from pathlib import Path

from i4v_check import exact, require, uint
from check_i4v_schedule import configuration as schedule_configuration

MAGIC=0x434f4e5441494e01
NONE=(1<<64)-1
GATE,NOTIFY=160,161
WITNESS_FIELDS=set('site owner phase active queued retained acknowledgements servicing lock next_ticket trace_length ready_owners pending_events profile visit completed context source_generation deferred_started producer_ready container_ready helper packet guest frame code'.split())


def configuration(failure,n,actor):
    for value in (failure,n,actor):uint(value,32,'containment configuration')
    require(1<=failure<=5 and (n,actor) in ((1,0),(2,0),(2,1),(4,0),(4,3)),
            'declared twenty-five containment cases')
    return dict(failure=failure,family=4 if (failure,n,actor)==(5,2,0) else 1,n=n,actor=actor)


def scheduler(cfg):
    return next(c for c in (schedule_configuration(cfg['family'],cfg['n'],ordinal)
        for ordinal in range(2 if cfg['family']==4 else 4)) if c['actor']==cfg['actor'])


def mask(owners):return sum(1<<owner for owner in owners)


def trace_of(report):
    coord=report['coordinator'];trace=coord['trace']
    require(type(trace) is list and 0<len(trace)<=768,'complete bounded frozen trace')
    exact(coord['trace_length'],len(trace),'no hidden post-gate trace suffix')
    return trace


def machine(owner,*,partial=False):
    """Failure does not relax the healthy fixture's native admission profile."""
    from check_i1 import resident_pc
    exact(uint(owner['current_el'],64,'current EL'),4,'resident executes at EL1')
    exact(uint(owner['tcr'],64,'TCR'),0x2b5103510,'admitted translation geometry')
    require(uint(owner['actlr'],64,'ACTLR')&2==2,'native ordering enabled')
    mask=0x1005 if partial else 0x1007
    require(uint(owner['sctlr'],64,'SCTLR')&mask==0x1005,'admitted native memory settings')
    for key in (('store_pc',) if partial else ('capture_pc','service_pc')):
        pc=uint(owner[key],64,key)
        require(resident_pc(pc),'actual '+key+' lies in resident text')
        if partial:require(resident_pc(pc+4),'complete STRB/RET helper lies in resident text')


def controls_prefix(report,cfg,witness):
    import i4v_controls_concurrent_check as native
    import i4v_controls_check as scalar
    schedule=scheduler(cfg);profile=21 if schedule['seed_ordinal']%2==0 else 24
    exact((report['case'],report['program_revision'],report['profile'],report['configured_vcpus'],
           report['tested_vcpus'],report['actor']),('controls-concurrent',1,profile,cfg['n'],cfg['n'],cfg['actor']),
          'failed VM retains its exact requested control program')
    class Prefix(native.ConcurrentReplay):
        def step(self,word):
            event,cpu=word>>8,word&255
            if event!=116:return super().step(word)
            # This containment profile deliberately withholds the configured
            # actor, rather than the healthy fixture's predecessor owner.
            require(cfg['failure']==4 and cpu!=cfg['actor'] and self.source_generation==2
                and cpu not in self.source_ack,'only nonactor covering source acknowledgements')
            owner=self.owners[cpu];owner['source_generation']=2;owner['mapping']=None
            owner['invalidations']+=1;owner['source_rejections']=1;owner['source_entry_rejected']=1
            current=list(owner['source_expected']);current[5]=2
            reply=scalar.reply(current,owner['pending']['instruction'])
            exact([i for i in range(16) if reply[i]!=owner['source_reply'][i]],[5],
                  'withheld-ack cut retains actual stale source-bound reply')
            self.source_ack.add(cpu)
    n,actor=cfg['n'],cfg['actor'];all_owners=(1<<n)-1
    require(len(report['owners'])==n,'all stopped owner records retained')
    require(uint(report['vm_nonce'],64,'VM incarnation')>0,'nonzero VM incarnation')
    for owner in report['owners']:machine(owner)
    model=Prefix(profile,n,actor,report['vm_nonce'],[o['capture_pc'] for o in report['owners']],
        source_control='positive',revision=1,maintenance=True,peer_reply_control=True,capacity=schedule['pool_limit'])
    import check_i4v_progress_protocols as progress
    trace=trace_of(report);notifications=[];gate=False;snapshots=[];states=[];entries={}
    for word in trace:
        event,cpu=word>>8,word&255
        uint(word,64,'native trace word');require(cpu<n,'native owner belongs to VM')
        require(not gate,'no native protocol action after immutable gate publication')
        snapshot,item=progress.control_observe(model,schedule,word)
        snapshots.append(snapshot);states.append(snapshot['queue'])
        if item is not None:
            key,value=item
            if key in entries:exact(entries[key],value,'one context per retained scheduling cohort')
            entries[key]=value
        if event==NOTIFY:
            exact(cpu,len(notifications),'new containment notification covers each owner once')
            require(model.final_ready==model.all,'all old source references already retained')
            notifications.append(cpu)
        elif event==GATE:
            exact(cpu,actor,'configured failure owner publishes witness')
            exact(notifications,list(range(n)),'gate follows the actual pending-request posts')
            gate=True
        else:
            require(not notifications,'notification and witness publication form one locked transaction')
            model.step(word)
    require(gate and model.final_ready==model.all and model.pressure['finished']==1,
            'failure follows retained real code and completed pressure')
    require(not model.active and not model.queue and model.servicing is None and model.retained==model.all,
            'all code references retained, no in-flight guest transaction fabricated complete')
    expected_acks=all_owners^(1<<actor) if cfg['failure']==4 else 0
    exact(mask(model.source_ack),expected_acks,'precisely the configured acknowledgement is withheld')
    exact(model.source_generation,2 if cfg['failure']==4 else 1,'source mutation occurs only at ACK gate')
    exact((witness['site'],witness['phase'],witness['active'],witness['queued'],witness['retained'],
           witness['acknowledgements'],witness['servicing'],witness['ready_owners'],witness['pending_events']),
          (2 if cfg['failure']==4 else 1,model.phase,0,0,all_owners,expected_acks,NONE,all_owners,all_owners),
          'frozen native authority reconstructed from actual ordered operations')
    before_source,before_data,before_tables=native.initial_memory(profile,n,1)
    for key,value in (('source_before',before_source),('source_after',model.source),
                      ('data_before',before_data),('data_after',model.data)):
        exact(report[key],value,'full frozen '+key)
    for field,key in (('tables','legacy'),('pae_tables','pae'),('mode_tables','ia32e')):
        exact(report[field+'_before'],before_tables[key],'initial '+field)
        exact(report[field+'_after'],model.tables[key],'all ordered '+field+' effects stop at gate')
    exact(report['maintenance'],model.pressure,'all real maintenance request identities and effects')
    for cpu,(observed,owner) in enumerate(zip(report['owners'],model.owners)):
        exact(observed['id'],cpu,'ordered native owners')
        for key,value in (('initial',owner['initial']),('guest_before',owner['initial_guest']),
            ('guest_after',owner['guest']),('final_frame',owner['frame']),('code_before',[0]*640),
            ('code_after',owner['code']),('bank_before',owner['bank']),('bank_after',owner['bank']),
            ('completed',owner['completed'])):
            exact(observed[key],value,f'owner{cpu} frozen {key}')
        state=observed['controls'];pending=owner['pending']
        exact((state['generation'],state['visits'],state['completed'],state['current']),
              (owner['context'],len(owner['visits'])+1,owner['completed'],owner['frame']),
              'no semantic completion or native-frame overwrite after gate')
        for key in ('walks','invalidations','stale_reply','stale_entry','deferred','late_reply',
                    'rebound_reply','old_slot_context','old_controls'):
            exact(state[key],owner[key],'frozen control transport/history '+key)
        exact(state['mapping'],owner['mapping'][0] if owner['mapping'] else 0,'frozen current shadow route')
        import i4v_pae_model as pae
        expected_pae=pae.empty_state()
        expected_pae.update(hidden=owner['hidden'],visits=copy.deepcopy(owner['pae_visits']))
        for key in ('attempts','reads','walk_reads','walk_writes','mutations'):expected_pae[key]=owner[key]
        expected_pae['visits'][pending['index']]=copy.deepcopy(pending['pae'])
        exact(observed['pae'],expected_pae,'every hidden-root snapshot/load/read and pending load lifetime')
        expected_modes=copy.deepcopy(owner['modes'])
        if profile>=22:expected_modes['visits'][pending['index']]=copy.deepcopy(pending['modes'])
        exact(observed['modes'],expected_modes,'complete descriptor/far-transition history and pending prefix')
        for index,expected in enumerate(owner['visits']):
            actual=observed['visits'][index];wanted=copy.deepcopy(expected)
            if wanted['faults']:
                syndrome,far,pc,spsr=actual['exception'];write=wanted['source']&255==0x89
                require(syndrome>>26==0x25 and syndrome>>6&1==int(write)
                    and syndrome&63 in (range(12,16) if wanted['exception'][0]&63==15 else range(4,8))
                    and not syndrome&((1<<7)|(1<<8)|(1<<10)),'direct retained-prefix memory fault')
                exact((far,pc,spsr&0xf00003df),
                      (native.DATA+cpu*256,scalar.CODE+index*32+(4 if write else 0),owner['initial']['nzcv']|0x3c4),
                      'prefix fault operands, site and complete saved flags')
                wanted['exception']=list(actual['exception'])
            exact(actual,wanted,'every completed native/control prefix record')
        actual=observed['visits'][pending['index']]
        for key in ('rip','bytes','kind','source','fetch_gpa','before','generation_before','gpr_before',
                    'flags_before','request','reply','faults','gpa','vector','error','linear'):
            exact(actual[key],pending['visit'][key],'unexecuted retained final instruction '+key)
        for key in ('after','gpr_after'):
            exact(actual[key],[0]*len(actual[key]),'retained instruction has no fabricated '+key)
        exact(actual['generation_after'],0,'unexecuted instruction cannot advance context')
        exact(actual['frame'],scalar.zero_frame(),'unexecuted final instruction has no return frame')
        exact(actual['fault_frame'],scalar.zero_frame(),'unexecuted final instruction has no fabricated fault')
        empty={key:(scalar.zero_frame() if key in ('frame','fault_frame') else
            [0]*len(value) if isinstance(value,list) else 0) for key,value in actual.items()}
        exact(observed['visits'][pending['index']+1:],[empty]*(19-pending['index']),
              'all future instruction slots remain untouched')
        leaves=[0]*512
        if owner['mapping']:leaves[256]=scalar.descriptor((owner['mapping'][0]-native.DATA)//4096,not owner['mapping'][1])
        exact(observed['lower_leaf_after'],leaves,'frozen private data mapping and permission')
        exact(observed['upper_leaf_after'],[0]*512,'no unpublished upper guest mapping')
        exact(observed['owner']['retained_code'],1,'real old code remains pinned')
        exact(observed['owner']['released_code'],0,'no cancelled route release masquerading as completion')
        exact(observed['owner']['reclaimed'],0,'no code reclamation before withheld release')
    owner=model.owners[actor]
    exact((witness['profile'],witness['visit'],witness['completed'],witness['context'],witness['source_generation']),
          (profile,len(owner['visits'])+1,owner['completed'],owner['context'],model.source_generation),
          'witness identifies exact architectural prefix and generations')
    exact((witness['guest'],witness['frame'],witness['code'],witness['helper']),
          (owner['guest'],owner['frame'],owner['code'],[0]*8),'full frozen native/canonical/code witness')
    packet=list(owner['source_expected'])
    if cfg['failure'] in (1,2,5):packet[12]+=1
    exact(witness['packet'],packet,'deferred failure job binds exact current instruction identity')
    coord=report['coordinator']
    for key,value in (('phase',model.phase),('active',0),('queued',0),('retained',all_owners),
        ('acknowledgements',expected_acks),('servicing',NONE),('new_entries',0),('new_references',0),
        ('reclaimed',0),('finished',0),('event_pending',[1]*n+[0]*(4-n)),
        ('event_notifications',[3]*n+[0]*(4-n))):
        exact(coord[key],value,'post-join frozen coordinator '+key)
    model.progress=(snapshots,states+[copy.deepcopy(model.pressure)],entries)
    return model


def partial_prefix(report,cfg,witness):
    import check_i4v_partial_progress as native
    import i4v_controls_concurrent_check as controls
    from check_i1 import resident_pc, state
    exact(cfg,configuration(5,2,0),'one declared partial-helper cancellation')
    sc=scheduler(cfg);actor=cfg['actor'];n=cfg['n'];all_owners=(1<<n)-1
    exact((report['case'],report['n'],report['ordinal'],report['actor']),
          ('partial-progress',n,sc['seed_ordinal'],actor),'actual partial helper fixture')
    require(uint(report['vm_nonce'],64,'VM incarnation')>0,'nonzero VM incarnation')
    require(len(report['owners'])==n,'all stopped helper owners retained')
    for owner in report['owners']:machine(owner,partial=True)
    class Prefix(native.Replay):
        def fault(self,cpu,probe):
            require(not probe,'cancelled helper never reaches its post-remap probe')
            fault=self.r['owners'][cpu]['fault'][0];frame=state(fault['frame'],'retained helper fault')
            far=native.DATA+cpu*256+1;value=0x6e+cpu*32
            exact((fault['far'],fault['gpa'],fault['value'],fault['resume_pc']),
                  (far,far,value,0),'sampled second store remains unresumed on original route')
            exact((frame['x'][0],frame['x'][1]),(far,value),'actual pending STRB operands')
            require(resident_pc(fault['pc']) and fault['esr']>>26==0x25
                and fault['esr']&(1<<6) and fault['esr']&63 in range(4,8)
                and not fault['esr']&((1<<7)|(1<<8)|(1<<10)),'direct absent-shadow store fault')
            exact(fault['spsr']&0xf00003df,frame['nzcv']|0x3c4,'precise native flags and masked EL1t')
            require(0x800040050000<=frame['sp']<=0x800040054000 and frame['sp']%16==0,
                    'saved helper stack belongs to this owner')
    model=Prefix(report,sc)
    model.source[actor*256+16:actor*256+19]=[0x48,0x8b,0x03]
    exact(report['data_before'],model.data,'initial complete device-helper RAM')
    exact(report['source_before'],model.source,'actual compile-only source embedded separately from REP helper')
    exact(report['tables_before'],native.table_image(model.tables),'initial helper tables')
    trace=trace_of(report);gate=False;extra_fetch=False;snapshots=[]
    for word in trace:
        event,cpu=word>>8,word&255
        require(not gate,'cancelled helper has no post-gate guest effects')
        snapshots.append((model.phase,model.acks,model.opened))
        if event==0x1003:
            require(cpu==actor and not extra_fetch and model.held==set(range(n)) and model.mutation_blocked,
                    'compile-only fetch begins after every partial prefix is retained')
            answer=native.validate_walk(report['owners'][actor]['walks'][3],model.tables,
                native.Context('four',0x1000,nxe=True),native.RIP+actor*256+16,'fetch')
            require(answer.route is not None,'compile-only source resolves through current guest tables')
            model.tables=answer.tables;model.walks[actor]+=1;extra_fetch=True
        elif event==GATE:
            exact(cpu,actor,'partial cancellation actor');require(extra_fetch,'actual producer input fetch before cancel')
            gate=True
        else:model.step(word)
    require(gate and model.samples==[2]*n and model.retries==[0]*n and model.held==set(range(n)),
            'two device reads, one committed element and one retained second store per owner')
    exact((witness['site'],witness['phase'],witness['active'],witness['queued'],witness['retained'],
           witness['acknowledgements'],witness['servicing'],witness['ready_owners'],witness['pending_events']),
          (3,1,0,all_owners,all_owners,0,NONE,all_owners,all_owners),'partial retained authority at cancel')
    exact(report['data_after'],model.data,'first stores remain and no second store is fabricated')
    exact(report['source_after'],model.source,'no cancelled source publication')
    exact(report['tables_after'],native.table_image(model.tables),'no cancelled remap')
    exact(report['images'],model.images+[bytes(8192).hex()]*2,'actual held-prefix image and two untouched snapshot slots')
    for cpu,o in enumerate(report['owners']):
        before=native.guest(cpu);held=native.advanced(before,1);frame=native.native_seed(cpu)
        bank=[((i*37+19)&255)^cpu for i in range(512)]
        expected=dict(id=cpu,phase=1,reads=2,writes=1,read_values=[0x57+cpu*32,0x6e+cpu*32],
            sampled=0x6e+cpu*32,subaccess=1,source_route=native.DEVICE+cpu*256+1,
            destination_route=native.DATA+cpu*256+1,destination_writable=1,retained=1,released=0,
            faults=1,retries=0,probe_writes=0,probe_route=0,notifications=2,pending=1,
            event_attempts=1,event_blocked=1,delivered=0,delivery_state=[0]*5,
            walk_count=4 if cpu==actor else 3,initial=frame,before=before,held=held,resumed=before,after=held,
            bank_before=bank,bank_after=bank,native_leaf=[0]*512)
        for key,value in expected.items():exact(o[key],value,'partial frozen owner '+key)
        exact(o['captured'],o['fault'][0]['frame'],'last complete saved helper fault frame')
        exact(o['store_pc'],o['fault'][0]['pc'],'actual helper store site')
        exact(o['store_bytes'],list(bytes.fromhex('01000039c0035fd6')),'actual finite STRB/RET helper')
        require(all(value==0 for key,value in o['fault'][1].items() if key!='frame'),
                'no invented second probe fault')
        zero_frame=dict(x=[0]*31,q=[[0]*16 for _ in range(32)],sp=0,nzcv=0,fpcr=0,fpsr=0)
        exact(o['fault'][1]['frame'],zero_frame,'unused probe frame remains untouched')
    expected_guest=native.advanced(native.guest(actor),1)
    packet=controls.request(report['vm_nonce'],actor,1,1,1,4,native.RIP+actor*256+16,b'\x48\x8b\x03',1)
    exact((witness['profile'],witness['visit'],witness['completed'],witness['context'],witness['source_generation']),
          (14,1,1,1,1),'partial helper phase and exact committed prefix')
    exact(witness['helper'],[2,1,1,1,0x6e+actor*32,native.DEVICE+actor*256+1,native.DATA+actor*256+1,1],
          'captured sample and original pending route')
    exact((witness['guest'],witness['frame'],witness['packet'],witness['code']),
          (expected_guest,report['owners'][actor]['fault'][0]['frame'],packet,[0]*640),
          'frozen partial guest/native/compiler identities')
    for key,value in (('phase',1),('active',0),('queued',all_owners),('retained',all_owners),
                      ('servicing',NONE),('acknowledgements',0),('new_entries',0),('reclaimed',0),('finished',0)):
        exact(report['coordinator'][key],value,'frozen partial coordinator '+key)
    common=dict(n=n,actor=actor,ordinal=sc['seed_ordinal'],ready=all_owners,mutation_blocked=1,
        helper_done=0,released=0,mutated=0,acknowledgements=0,admission_blocked=0,opened=0,
        probes_done=0,delivered=0,finished=0,images=1,source_generation=1,destination_generation=1,
        pending_events=all_owners)
    exact(report['common'],common,'no hidden partial completion, release, remap or event delivery')
    model.progress=snapshots
    return model


def scheduling_prefix(record,native,cfg,model):
    import check_i4v_schedule as core
    import check_i4v_progress_protocols as progress
    sc=scheduler(cfg);all_owners=(1<<cfg['n'])-1;trace=trace_of(native)
    if cfg['family']==1:
        snapshots,states,entries=model.progress
        completed={key:value[0] for key,value in entries.items()}
        completed.update({core.MAINTENANCE_ACK|epoch:all_owners for epoch in range(3,3+9*cfg['n'])})
        if model.family==21:completed.update({core.ACK:all_owners,core.ACK|1:all_owners})
        if cfg['failure']==4 and cfg['n']>1:completed[core.SOURCE_ACK]=all_owners^(1<<cfg['actor'])
        contract=progress.control_contract(sc,model.family,snapshots,entries,
            delayed=cfg['actor'] if cfg['failure']==4 else None)
        count=core.decisions(record,sc,trace,contract,set(completed),prefix=completed)
        progress.pool(record,sc,trace,states,controls=True)
    else:
        completed={core.ADMIT:all_owners,core.DETACH:all_owners}
        def contract(key,at,done):
            phase,acks,opened=model.progress[at]
            require(not opened and acks==0,'partial failure precedes any remap or reopening')
            if key==core.ADMIT:
                exact(phase,0,'helper admission before closing');action=0x1000
            elif key==core.DETACH:
                require(phase in (0,1),'partial store detaches before exclusive recovery');action=2
            else:raise ValueError('unexpected partial prefix scheduling cut')
            return all_owners,all_owners,phase,1,action
        count=core.decisions(record,sc,trace,contract,set(completed),prefix=completed)
        exact(record['pool_events'],[],'partial retained helper has no maintenance pool')
        for key in ('full_count','readmitted'):exact(record['summary'][key],0,'partial '+key)
        for key in ('full','retried'):exact(record['summary'][key],[0]*4,'partial '+key)
    return count


def supervision(schedule,record,native,cfg):
    observed=record['observation'];n=cfg['n'];actor=cfg['actor'];failure=cfg['failure']
    exact(set(observed),set('gate_seen gate_elapsed_ms detected_elapsed_ms stop_elapsed_ms cause'.split()),
          'bounded native-stop observation fields')
    exact(observed['gate_seen'],True,'failure deadline starts from acquired native witness')
    for key in ('gate_elapsed_ms','detected_elapsed_ms','stop_elapsed_ms'):uint(observed[key],64,key)
    gate,detected,stopped=(observed[k] for k in ('gate_elapsed_ms','detected_elapsed_ms','stop_elapsed_ms'))
    require(gate<=detected<=stopped and detected-gate<=3000 and stopped-gate<=10000,
            'original failure detected within three seconds and all owners stopped within ten')
    cause={1:'control worker failed',2:'control reply changed request or emitted body',
        3:'resident owner panic detail=880',4:'containment gate stalled before three-second deadline',
        5:'explicit cancellation at retained native gate'}[failure]
    exact(observed['cause'],cause,'original intended failure cause survives cleanup')
    s=schedule['supervision']
    exact(set(s),set('host_pid joined completions destroyed statuses cancellations error elapsed_ms'.split()),'complete failure supervision')
    exact(s['host_pid'],record['host_pid'],'supervision and witness belong to the same actual host')
    exact((s['joined'],s['completions']),(n,n),'all native owner threads report and join')
    exact(sorted(s['destroyed']),[[i,0] for i in range(n)],'all actual native owner destruction calls succeed')
    expected=[[i,4 if failure==3 and i==actor else 0,880 if failure==3 and i==actor else 0] for i in range(n)]
    exact(s['statuses'],expected,'no cancelled owner is fabricated as successful completion')
    exact([[o['id'],o['status'],o['detail']] for o in native['owners']],expected,'native records match supervisor terminal evidence')
    exact(s['error'],cause,'latched primary failure preserved after every join')
    require(type(s['cancellations']) is list and all(type(v) is int and v==0 for v in s['cancellations']),
            'all requested native exits succeed')
    if failure in (4,5):require(s['cancellations'],'stalled/cancelled native owners receive actual exit requests')
    uint(s['elapsed_ms'],64,'joined supervisor elapsed time')
    require(gate<=s['elapsed_ms']<=stopped,'join snapshot precedes final containment snapshot')


def audit_development(records,expected_config=None):
    require(type(records) is list and len(records)==4,'schedule, immutable witness, native prefix and VM destruction')
    schedule,record,native,destroy=records
    exact(set(record),set('case host_pid config magic version ready command witness observation'.split()),'complete containment report fields')
    require(uint(record['host_pid'],64,'host process identity')>0,'actual host PID')
    exact(record['case'],'containment','failure report kind')
    cfg=configuration(record['config']['failure'],record['config']['n'],record['config']['actor'])
    exact(record['config'],cfg,'declared failure family/rotation')
    if expected_config is not None:exact(cfg,expected_config,'requested failure cell')
    exact(schedule['config'],scheduler(cfg),'failed run uses declared scheduler configuration')
    exact((record['magic'],record['version'],record['ready']),(MAGIC,1,1),'Release-published immutable witness')
    witness=record['witness'];exact(set(witness),WITNESS_FIELDS,'complete frozen witness')
    for key,value in witness.items():
        if key not in ('helper','packet','guest','frame','code'):uint(value,64,'witness '+key)
    exact(witness['owner'],cfg['actor'],'failure actor owns witness')
    exact(witness['trace_length'],len(trace_of(native)),'witness is final native protocol operation')
    require(witness['next_ticket']>witness['lock'],'witness captured while actual ticket lock is held')
    exact(witness['deferred_started'],int(cfg['failure'] in (1,2,5)),'actual deferred job exists only for compiler/cancel failures')
    exact(witness['producer_ready'],int(cfg['failure']==2),'malformed reply begins from completed real compilation')
    exact(witness['container_ready'],int(cfg['failure']==5),'cancellation begins only after actual container creation')
    exact(record['command'],int(cfg['failure'] in (1,2)),'only compiler injection authorizes service continuation')
    if cfg['family']==1:model=controls_prefix(native,cfg,witness)
    else:model=partial_prefix(native,cfg,witness)
    scheduling_prefix(schedule,native,cfg,model)
    supervision(schedule,record,native,cfg)
    exact(destroy,dict(case='vm-destroy',status=0),'actual VM destruction succeeds after owners join')
    if cfg['failure']==3:
        exact(native['coordinator']['lock'],witness['lock'],'locked panic never fabricates an unlock')
    return model


def check(records,expected_config=None):
    audit_development(records,expected_config)
    # Resource cleanup and the separate fresh VM remain mandatory runner checks.
    return dict(config=records[1]['config'],prefix='validated',
                trace_length=records[1]['witness']['trace_length'])


def read_json(path):
    path=Path(path)
    require(path.stat().st_size<=65536,'bounded retained ownership JSON')
    return json.loads(path.read_text())


def owned_job(registry):
    """Read actual per-job journals; no process/container operations here."""
    registry=Path(registry);job=read_json(registry/'job.json');token=job['token']
    require(type(token) is str and len(token)==32 and all(c in '0123456789abcdef' for c in token),
            'unique pre-spawn ownership token')
    exact(job['schema'],'natemu-owned-worker-v1','ownership journal schema')
    process=read_json(registry/'process.json');cleanup=read_json(registry/'cleanup.json')
    for item in (process,cleanup):exact(item['token'],token,'cleanup retains exact job ownership')
    require(process['group_gone'] is True and (process.get('spawned') is False or process.get('reaped') is True),
            'owned process group gone and actual child reaped')
    require(cleanup['complete'] is True,'all owned resources actually cleaned')
    worker=read_json(registry/'worker.json') if (registry/'worker.json').exists() else None
    if worker is not None:
        exact(worker['token'],token,'registered worker identity')
        require(worker['pid']>0 and worker['pgid']==worker['pid'],'dedicated worker process group')
        if 'pid' in process:exact(process['pid'],worker['pid'],'reaped owned group leader')
    else:
        require(process.get('spawned') is False or (process.get('reaped') is True
            and type(process.get('pid')) is int and process['pid']>0 and process.get('pgid')==process['pid']),
            'missing worker registration requires an explicit owning-host child lease/reap acknowledgement')
    entries={item['record']:item for item in cleanup['containers']}
    paths=sorted((registry/'containers').glob('*.json'))
    exact(set(entries),{path.name for path in paths},'cleanup covers every pre-create container registration')
    for path in paths:
        container=read_json(path);entry=entries[path.name]
        exact(container['token'],token,'container registry ownership')
        exact(container['state'],'removed','actual owned container removal acknowledged')
        require(entry['complete'] is True,'container cleanup cannot hide incomplete result')
        exact(entry['name'],container['name'],'cleanup binds registered container identity')
    return dict(job=job,worker=worker,process=process,cleanup=cleanup,
                containers=[read_json(path) for path in paths])


def packet_file(path):
    import struct
    raw=Path(path).read_bytes();exact(len(raw),128,'complete retained sixteen-word packet')
    return list(struct.unpack('<16Q',raw))


def verify_artifacts(records,directory):
    """Bind producing artifacts and cleanup to the actual frozen request prefix."""
    import i4v_controls_worker as worker
    model=audit_development(records);cfg=records[1]['config'];witness=records[1]['witness']
    root=Path(directory);exports=[];tokens=set();containers=0
    compiling_owners=[cfg['actor']] if cfg['family']==4 else range(cfg['n'])
    exact({p.name for p in root.glob('cpu-*')},{f'cpu-{i}' for i in compiling_owners},
          'compiler roots match owners with real requested work')
    for cpu in range(cfg['n']):
        expected=[] if cfg['family']==4 else list(model.owners[cpu]['packets'])
        injected=cpu==cfg['actor'] and cfg['failure'] in (1,2,5)
        if injected:expected.append(witness['packet'])
        owner_root=root/f'cpu-{cpu}';paths=sorted(owner_root.glob('request-*'))
        exact([p.name for p in paths],[f'request-{i:03}' for i in range(len(expected))],
              'every compiler allocation belongs to a replayed resident request')
        for index,(path,request) in enumerate(zip(paths,expected)):
            exact(packet_file(path/'request.bin'),request,'actual worker request belongs to frozen prefix')
            lifecycle=owned_job(path/'.ownership');job=lifecycle['job']
            require(job['token'] not in tokens,'no job ownership identity reused')
            tokens.add(job['token']);containers+=len(lifecycle['containers'])
            exact(job['host_pid'],records[1]['host_pid'],'job reserved by this failed host process')
            last=injected and index==len(expected)-1
            if not last:
                exported=worker.verify_request(path,request)
                exact(packet_file(path/'reply-delivered.bin'),model.owners[cpu]['replies'][index],
                      'every completed prefix job delivered its actual verified reply')
                exports.append((path,exported));continue
            failure=cfg['failure'];registry=path/'.ownership'
            require(not (path/'reply-delivered.bin').exists(),'rejected/cancelled extra job never delivered executable reply')
            if failure==1:
                injection=read_json(registry/'injection.json');death=read_json(registry/'worker-exit.json')
                process=lifecycle['process']
                exact(injection,dict(kind='worker-exit',token=job['token'],pid=process['pid'],
                    pgid=process['pgid'],signal=9),'real signal targets the owned live wrapper')
                exact((death['token'],death['pid'],death['code'],death['status'],death['raw_status'],death['reaped']),
                      (job['token'],injection['pid'],2,9,9,False),'actual non-reaping SIGKILL observation preserved')
                require(not (registry/'result.bin').exists(),'worker exit is abrupt, not a successful payload result')
            elif failure==2:
                injection=read_json(registry/'injection.json')
                exact(injection,dict(kind='malformed',owner=cpu,request=request),'malformation targets exact deferred request')
                case=worker.case_of(tuple(request));spec=worker.CASES[case]
                exported=worker.verify(path/'compile',dict(case=case,guest_rip=request[7],
                    bitness=worker.bitness(request[8]),source_hex=spec['source']))
                import struct
                original=list(struct.unpack('<16Q',worker.make_reply(tuple(request),bytes.fromhex(exported['body_hex']))))
                changed=list(original);changed[0]^=1
                exact(packet_file(path/'reply-before-injection.bin'),original,'malformation starts from actual valid compiler output')
                exact(packet_file(path/'reply-after-injection.bin'),changed,'exact one-word intentional malformed reply')
                exact(packet_file(path/'reply.bin'),changed,'host actually observes corrupted reply')
                require((registry/'result.bin').read_bytes()==bytes(4),'producer completed successfully before malformation')
                exports.append((path,exported))
            else:
                # The cancellation injection must retain a real created
                # container, not merely an unstarted Python process. The host
                # creation-hold handshake supplies its immutable observation.
                observed=read_json(registry/'creation-held.json')
                exact(observed['token'],job['token'],'observed creation belongs to cancelled job')
                exact(observed['schema'],'natemu-owned-worker-v1','observed creation journal schema')
                exact(observed['state'],'created','daemon creation acknowledged before cancellation')
                require((registry/'hold-container').is_file(),'real worker held before starting created container')
                require(observed['container_id'] and any(c.get('container_id')==observed['container_id']
                    and c['name']==observed['name'] for c in lifecycle['containers']),
                    'the actually observed created container is one of the drained owned resources')
                identity=observed['container_id']
                require(type(identity) is str and len(identity)==64 and all(c in '0123456789abcdef' for c in identity),
                        'immutable daemon container identity')
                exact((registry/'container-ready.bin').read_bytes(),
                      b'NATCTR01'+bytes.fromhex(job['token'])+bytes.fromhex(identity)+bytes(8),
                      'actual host handshake binds the same created container and job')
                require(not (registry/'result.bin').exists(),'cancelled held compilation never reports completion')
    require(containers>0,'actual compiler containers were created and cleaned in this failure cell')
    return exports


def verify_fresh(records,fresh_records,fresh_directory,fresh_host_pid=None):
    import check_i4v_schedule as schedule
    cfg=records[1]['config'];expected=scheduler(cfg)
    result=schedule.check(fresh_records,expected)
    require(fresh_records[1]['vm_nonce']!=records[2]['vm_nonce'],'fresh control has a new actual VM incarnation')
    exports=schedule.verify_producer(fresh_records,fresh_directory)
    observed_pid=fresh_records[0]['supervision']['host_pid']
    if fresh_host_pid is not None:exact(fresh_host_pid,observed_pid,'external fresh host identity')
    fresh_host_pid=observed_pid
    jobs=list(Path(fresh_directory).rglob('.ownership/job.json'))
    hosts=set()
    for path in jobs:hosts.add(owned_job(path.parent)['job']['host_pid'])
    if fresh_host_pid is not None:
        require(uint(fresh_host_pid,64,'fresh host PID')>0,'actual fresh host process')
        if hosts:exact(hosts,{fresh_host_pid},'fresh jobs belong to the independently started host')
        hosts.add(fresh_host_pid)
    require(len(hosts)==1 and records[1]['host_pid'] not in hosts,'fresh control runs in a separate clean host process')
    return dict(control=result,compilations=len(exports))
