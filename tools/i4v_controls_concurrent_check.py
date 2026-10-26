#!/usr/bin/env python3
"""Independent oracle for bounded concurrent guest-control execution.

One shared memory image is replayed in global trace order. Each owner retains
its own control tuple and hidden PAE snapshot; no guest result is normalized.
Strict admission requires the current source, maintenance and event contract.
Historical development audits retain explicitly narrower, non-acceptance scopes.
"""
import copy
import argparse
import json
from pathlib import Path

import i4v_controls_check as scalar
import i4v_modes_model as modes
import i4v_pae_model as pae
import check_i4
from i4v_off_check import guest_seed
from i4v_check import exact,require,uint

FAMILIES=(0,21,24,22,3)
SOURCE=scalar.SOURCE
DATA=scalar.DATA
NO_CPU=(1<<64)-1


def empty_interval():
    return dict(ordinal=0,heads=0,epoch=0,gpa=0,before=[0]*256,after=[0]*256,
                faults=0,esr=0,far=0,pc=0,spsr=0,initial=scalar.zero_frame(),
                captured=scalar.zero_frame(),dirty_frame=scalar.zero_frame())


def empty_maintenance(n):
    empty=lambda:dict(cpu=NO_CPU,serial=0)
    return dict(n=n,ready=0,serial=0,head=0,length=0,queue=[empty() for _ in range(4)],
        current=empty(),pending=[0]*4,submitted=[0]*4,completed=[0]*4,epoch=2,acknowledgements=0,
        total=0,batch=0,next_ordinary=0,ordinary=NO_CPU,intervals=[0]*4,ordinary_total=0,
        closing=0,finished=0,duplicate_rejections=0,maximum_depth=0,leaf_history=[0]*36,
        request_history=[empty() for _ in range(36)],compose_leaf=0)


def owner_program(family,owner,revision=0):
    require(family in FAMILIES and 0<=owner<4,'bounded concurrent control family/owner')
    require(revision in (0,1),'declared concurrent-only program revision')
    instructions=list(scalar.program(family))
    if revision==1:
        if family==21:instructions[8:8]=[scalar.immediate(v) for v in (0,0x100,0x1020)]
        if family==22:instructions[12:12]=[scalar.immediate(v) for v in (0,0x100,0x10011)]
        if family==24:instructions[7:7]=[scalar.immediate(0)]
        if family in (21,24):
            instructions[-1]=b'\xe9'+((16 if family==21 else 0)-sum(map(len,instructions))).to_bytes(4,'little',signed=True)
    delta=owner*256
    for index,instruction in enumerate(instructions):
        if instruction[:1]==b'\xea':
            target=int.from_bytes(instruction[1:5],'little')+delta
            instructions[index]=b'\xea'+target.to_bytes(4,'little')+instruction[5:]
    return instructions


def instruction_at(family,owner,rip,revision=0):
    cursor=SOURCE+owner*256
    for instruction in owner_program(family,owner,revision):
        if cursor==rip:return instruction
        cursor+=len(instruction)
    raise ValueError('RIP does not identify an admitted complete instruction')


def metadata(owner):
    require(0<=owner<4,'bounded metadata owner')
    return DATA+0x800+owner*256,DATA+0x840+owner*256


def request(vm,owner,incarnation,source_generation,context,mode,rip,instruction,serial):
    require(0<=owner<4 and vm>0 and incarnation>0 and source_generation>0 and context>0
            and serial>0 and mode in range(5),'bounded compile identity')
    require(SOURCE+owner*256<=rip and rip+len(instruction)<=SOURCE+(owner+1)*256,
            'compile source must fit its complete owner window')
    native=scalar.body(instruction)
    return [0x49345651,1,vm,owner,incarnation,source_generation,context,rip,mode,
            int.from_bytes(instruction,'little'),len(instruction),rip,serial,len(native),0,0]


def snapshot(guest,hidden,context,source_generation,completed,route,tr):
    return dict(control=list(guest['control']),cs=list(guest['segments'][0]),tr=list(tr),
                hidden=list(hidden['entries']),hidden_generation=hidden['generation'],
                context=context,source_generation=source_generation,rip=guest['rip'],
                completed=completed,gpr=list(guest['gpr'][:4]),route=route)


def decode_event(word,n):
    uint(word,64,'trace word')
    owner=word&255;event=word>>8
    require(1<=n<=4 and owner<n,'trace owner must be a participant')
    if event<256:return event,owner,None
    kind=event&0xf000;visit=event&0xfff
    require(kind in (0x1000,0x2000,0x3000,0x4000,0x5000,0x6000,0x7000)
            and event==kind+visit and 1<=visit<=20,'known ordered instruction event')
    return kind,owner,visit-1


def unchanged_peers(before,after,actor,n):
    require(len(before)==len(after)==4,'full bounded owner snapshot arrays')
    for owner in range(n):
        if owner!=actor:
            exact(after[owner],before[owner],f'CPU{owner} state across local actor transaction')


def table_images(family):
    require(family in FAMILIES,'bounded family')
    return dict(legacy=scalar.tables(family),pae=pae.tables(13) if family>=13 else [0]*20480,
                ia32e=modes.tables() if family>=22 else [0]*16384)


def initial_owner(family,owner):
    frame,bank,_=check_i4.seed(owner)
    for native in (4,6,7,5,10,11,9,8):frame['x'][native]&=0xffffffff
    frame['x'][11]=DATA+128
    if family>=22:
        frame['x'][7]=0xc0000080;frame['x'][5]=0;frame['x'][10]=metadata(owner)[1]
    guest=guest_seed(frame,SOURCE+owner*256)
    guest['control'][0]=0x10011;guest['control'][2]=0x1020 if family>=13 else 0x1000
    if family==24:guest['control'][2:5]=[0x6000,0x620,0x100]
    mode=modes.empty_state()
    if family>=22:mode.update(gdtr=[metadata(owner)[0],0x37],tr=[0x28,0,0x67,0x8b],pointer=metadata(owner)[1])
    return frame,bank,guest,mode


def initial_memory(family,n,revision=0):
    require(family in FAMILIES and 1<=n<=4,'bounded shared memory fixture')
    source=[0xcc]*4096;data=[0xa5]*4096+[0x5a]*4096
    for owner in range(n):
        program=owner_program(family,owner,revision);raw=b''.join(program)
        source[owner*256:owner*256+len(raw)]=raw
        for page,sentinel in ((0,0x11220000),(4096,0x33440000)):
            at=page+owner*256;data[at:at+4]=(sentinel+owner).to_bytes(4,'little')
        if family>=22:
            target=SOURCE+owner*256;cursor=target
            for instruction in program:
                cursor+=len(instruction)
                if instruction==b'\x48\xff\x2e':target=cursor;break
            gdt,pointer=metadata(owner)
            for page in (0,4096):
                base=page+gdt-DATA;data[base:base+0x38]=[0]*0x38
                for selector,value in ((8,0x00cf9b000000ffff),(0x10,0x00cf93000000ffff),
                                       (0x18,0x00af9b000000ffff),(0x28,0x00008b0000000067)):
                    pae.qword(data,base+selector,value)
                at=page+pointer-DATA
                data[at:at+10]=target.to_bytes(8,'little')+b'\x08\x00'
    return source,data,table_images(family)


def route(tables,guest,hidden,address,write=False,counts=None):
    """Resolve one architectural access against current shared bytes."""
    control=guest['control']
    active=modes.mode(control[0],control[3],control[4],guest['segments'][0])
    if active==0:return address,None
    if counts is not None:counts['walks']+=1
    if active==1:
        return scalar.translate(tables['legacy'],control[0],control[2],address,write)
    if active==2:
        return pae.walk(tables['pae'],hidden['entries'],address,write,bool(control[0]&0x10000),counts=counts)
    return modes.walk(tables['ia32e'],address,write,bool(control[0]&0x10000))


def control_transition(tables,guest,hidden,generation,instruction,tr_type=11):
    """Stage a literal MOV-CR transaction, including hidden-root publication."""
    before=guest['control'];value=guest['gpr'][0]
    next_tuple,error=scalar.control(instruction,value,before[0],before[2],before[3],0)
    if error:return None,error,[],None
    cr0,cr3,cr4=next_tuple
    efer=before[4]
    long=bool(efer&0x400 and guest['segments'][0][3]&0x2000)
    selected=(instruction[-1]>>3)&7
    if selected==4 and efer&0x400 and (cr4^before[3])&32:
        return None,13,[],None
    if selected==0:
        if not cr0&0x80000000:
            if long:return None,13,[],None
            efer&=~0x400
        elif efer&0x100:
            if not cr4&32 or (not before[0]&0x80000000 and
                (guest['segments'][0][3]&0x2000 or tr_type in (1,3))):
                return None,13,[],None
            efer|=0x400
    staged=copy.deepcopy(guest);staged['control'][0]=cr0
    staged['control'][2:5]=[cr3,cr4,efer]
    candidate=copy.deepcopy(hidden);addresses=[]
    if not efer&0x100 and pae.needs_reload((before[0],before[2],before[3]),next_tuple,selected):
        addresses,entries,error=pae.load(tables['pae'],cr3)
        if error:return None,error,addresses,None
        candidate=dict(entries=entries,generation=hidden['generation']+1)
    staged['rip']+=len(instruction)
    return (staged,candidate,generation+1),None,addresses,next_tuple


def replay_legacy_trace(family,n,actor,trace,captures,vm):
    """Replay the initial local-control cut, not the unfinished full campaign.

    The trace supplies scheduling only. Guest instructions, routes, frame
    contents, memory effects, and compiler identities are independently derived.
    Later protocol events are deliberately rejected until modeled separately.
    """
    require(family in (0,3) and 1<=n<=4 and 0<=actor<n,'initial legacy cut scope')
    require(len(captures)==n,'one capture address per owner')
    source,data,tables=initial_memory(family,n)
    owners=[]
    for cpu in range(n):
        frame,bank,guest,mode=initial_owner(family,cpu)
        owners.append(dict(guest=guest,frame=frame,bank=bank,hidden=dict(entries=[0]*4,generation=0),
            context=1,completed=0,visits=[],pending=None,route=0,mapping=None,walks=0,
            packets=[],replies=[],code=[0]*640,entered=copy.deepcopy(frame),expected=[0]*16,
            deferred=[0]*16,late=[0]*16,rebound=[0]*16,before_local=None,after_local=None,
            faults=0,finished=False,admitted=False,detached=False))
    active=set();queue=[];servicing=None;phase=0;local=0;held=set();drained=set()
    peers=set(range(n))-{actor}

    def snap(cpu):
        s=owners[cpu]
        return snapshot(s['guest'],s['hidden'],s['context'],1,s['completed'],s['route'],[0]*4)

    def reopen():
        nonlocal phase
        if not queue and not active and servicing is None:phase=0

    for word in trace:
        kind,cpu,index=decode_event(word,n);s=owners[cpu];g=s['guest']
        if kind==0x1000:
            require(not s['finished'] and s['pending'] is None,'fetch follows one completed prior instruction')
            exact(index,len(s['visits']),'ordered owner visits')
            require(index<(17 if family==0 else 4),'bounded instruction prefix')
            insn=instruction_at(family,cpu,g['rip'])
            gpa,error=route(tables,g,s['hidden'],g['rip'],counts=s)
            exact(error,None,'legacy source fetch')
            exact(source[gpa-SOURCE:gpa-SOURCE+len(insn)],list(insn),'actual source bytes at resolved GPA')
            native=insn[0] in (0xb8,0x89,0x8b)
            v=dict(rip=g['rip'],bytes=len(insn),kind=0 if native else 2 if insn[0]==0xe9 else 1,
                source=int.from_bytes(insn,'little'),fetch_gpa=gpa,before=list(g['control']),after=None,
                generation_before=s['context'],generation_after=None,gpr_before=g['gpr'][:4],gpr_after=None,
                flags_before=g['rflags'],flags_after=g['rflags'],exception=[0]*4,request=[0]*16,reply=[0]*16,
                entries=int(native),faults=0,gpa=0,memory_before=[0]*16,memory_after=[0]*16,
                vector=0,error=0,linear=0,frame=None,fault_frame=scalar.zero_frame())
            s['pending']=dict(instruction=insn,visit=v,index=index,admitted=False,fault=False)
            if native:
                p=request(vm,cpu,1,1,s['context'],int(bool(g['control'][0]&0x80000000)),
                          g['rip'],insn,len(s['packets'])+1)
                q=scalar.reply(p,insn);s['packets'].append(p);s['replies'].append(q);s['expected']=p
                v['request'],v['reply']=p,q
                image=scalar.body(insn)+scalar.RET;s['code'][index*32:index*32+len(image)]=image
                entered=copy.deepcopy(s['frame']);entered['x'][16]=scalar.CODE+index*32
                entered['x'][30]=captures[cpu];s['pending']['entry']=entered;s['entered']=entered
                if family==0 and index==0:
                    p=list(p);p[12]+=1;s['deferred']=p;s['packets'].append(p)
                    s['replies'].append(scalar.reply(p,insn));s['expected']=p
                if family==0 and index==15:
                    s['late']=scalar.reply(s['deferred'],owner_program(family,cpu)[0])
                    s['rebound']=list(s['late']);s['rebound'][12]=v['request'][12]
                    expected_reply=scalar.reply(v['request'],insn)
                    exact([i for i in range(16) if s['rebound'][i]!=expected_reply[i]],[6],
                          'ABA rejection remains necessary with only old context different')
        elif kind in (0x2000,0x3000,0x4000,0x5000):
            p=s['pending'];require(p is not None,'instruction event requires pending fetch')
            exact(index,p['index'],'instruction event belongs to current owner visit')
            v=p['visit'];insn=p['instruction'];memory=insn in (b'\x89\x03',b'\x8b\x03')
            if kind==0x2000:
                require(memory and cpu in active and not p['admitted'],'one admitted native memory access')
                gpa,error=route(tables,g,s['hidden'],DATA+cpu*256,insn[0]==0x89,counts=s)
                exact(error,None,'admitted legacy data permission')
                s['route']=gpa;p['admitted']=True
                p['needs_fault']=s['mapping'] is None or (insn[0]==0x89 and not s['mapping'][1])
                p['permission']=s['mapping'] is not None
                p['writable']=not g['control'][0]&0x80000000 or bool(
                    scalar.word(tables['legacy'],(g['control'][2]&0xfffff000)+256*4)&64)
                v['gpa']=gpa;v['memory_before']=data[gpa-DATA:gpa-DATA+16]
            elif kind==0x5000:
                require(memory and p['admitted'] and p['needs_fault'] and not p['fault']
                        and s['detached'],'exactly one native fault for a missing/RO mapping')
                p['fault']=True;v['faults']=1;s['faults']+=1;v['fault_frame']=copy.deepcopy(p['entry'])
                v['exception']=[(0x25<<26)|(64 if insn[0]==0x89 else 0)|(15 if p['permission'] else 7),
                    DATA+cpu*256,scalar.CODE+index*32+(4 if insn[0]==0x89 else 0),p['entry']['nzcv']|0x3c4]
                if cpu in peers and index==1:s['before_local']=snap(cpu)
            else:
                if kind==0x4000:
                    exact(v['kind'],0,'native completion only for generated scalar instruction')
                    current=copy.deepcopy(p['entry'])
                    if memory:
                        require(p['admitted'],'native memory completion has retained route')
                        if cpu in peers and index==1:
                            require(s['after_local'] is not None,'held peer cannot execute before its local isolation check')
                        exact(p['fault'],p['needs_fault'],'required native repair occurred')
                        if p['fault']:exact(servicing,cpu,'native fault completes only at FIFO service head')
                        at=v['gpa']-DATA
                        if insn[0]==0x89:data[at:at+4]=current['x'][4].to_bytes(4,'little')
                        else:current['x'][4]=int.from_bytes(bytes(data[at:at+4]),'little')
                        v['memory_after']=data[at:at+16]
                        if p['fault']:s['mapping']=(v['gpa'],p['writable'])
                        else:active.remove(cpu);s['admitted']=False;reopen()
                    else:
                        require(insn[0]==0xb8,'admitted immediate scalar')
                        current['x'][4]=int.from_bytes(insn[1:],'little')
                    s['frame']=current;g['gpr'][0]=current['x'][4];g['rip']+=len(insn)
                    s['completed']+=1
                elif insn[0]==0xe9:
                    g['rip']=(g['rip']+5+int.from_bytes(insn[1:],'little',signed=True))&0xffffffff
                    s['completed']+=1
                else:
                    result,error,addresses,_=control_transition(tables,g,s['hidden'],s['context'],insn)
                    exact(addresses,[],'legacy local commit does not load PAE roots')
                    if error:
                        exact((family,index,error),(3,3,13),'only declared rejected CR0 operation')
                        v['vector']=error
                    else:
                        s['guest'],s['hidden'],s['context']=result;g=s['guest']
                        s['mapping']=None;s['completed']+=1
                v.update(after=list(g['control']),generation_after=s['context'],
                         gpr_after=g['gpr'][:4],frame=copy.deepcopy(s['frame']))
                s['visits'].append(v);s['pending']=None
        elif kind==1:
            require(phase==0 and cpu not in active and not s['admitted'],'admission while OPEN')
            active.add(cpu);s['admitted']=True
        elif kind==2:
            require(cpu in active and cpu not in queue and phase in (0,1),'detach exactly one admitted route')
            active.remove(cpu);queue.append(cpu);s['detached']=True;phase=1
        elif kind==3:
            require(cpu==0 and phase==1 and not active and queue,'exclusive only after active routes detach')
            phase=2
        elif kind==4:
            require(phase==2 and servicing is None and queue and queue[0]==cpu,'FIFO repair service')
            servicing=cpu
        elif kind==5:
            require(servicing==cpu and queue[0]==cpu and s['pending'] is None,'release only after native completion')
            queue.pop(0);servicing=None;s['detached']=False;s['admitted']=False;reopen()
        elif kind==90:
            require(cpu==actor and local==0 and s['completed']==3,'actor reaches designated local transaction')
            s['before_local']=snap(cpu);local=1
        elif kind==94:
            require(cpu in peers and local==1 and cpu not in held and s['pending']['index']==1,
                    'each peer holds its first generated store')
            held.add(cpu)
        elif kind==95:
            require(n>=3 and cpu==(actor+n-1)%n and held==peers and len(queue)==n-2,
                    'last native owner deliberately enters after other peers detach')
        elif kind in (91,92):
            require(cpu==actor and local==1 and held==peers and set(queue)==peers,
                    'actor transaction crosses every retained detached peer access')
            exact(kind,92 if family==3 else 91,'local commit versus precise rejection')
            s['after_local']=snap(cpu);local=2
        elif kind==96:
            require(cpu in peers and local==2 and s['before_local'] is not None,'peer resumes after actor transaction')
            s['after_local']=snap(cpu);exact(s['after_local'],s['before_local'],'peer state and route locally isolated')
        elif kind==97:
            require(cpu in peers and local==2 and s['completed']==2 and not s['detached'],
                    'held store commits once before peer drain')
            drained.add(cpu)
        elif kind==93:
            require(cpu==actor and local==2 and drained==peers,'actor continues only after retained peers complete')
            local=3
        elif kind==98:
            require(s['pending'] is None and not s['admitted'] and not s['finished'],'terminal owner has no active instruction')
            exact((len(s['visits']),s['completed']),(17,17) if family==0 else (4,3),'complete bounded guest prefix')
            s['finished']=True
        else:
            raise ValueError(f'event {kind} is outside the initial legacy development replay')
    require(all(s['finished'] for s in owners) and local==3 and not active and not queue
            and servicing is None,'complete development trace and drained native routes')
    return dict(owners=owners,source=source,data=data,tables=tables,trace_events=len(trace),
                scope='initial legacy local-control development cut; not full campaign acceptance')


def audit_initial_legacy_cut(record,producer_directory=None):
    """Audit a retained development cut without granting campaign acceptance."""
    family=record['profile'];n=record['configured_vcpus'];actor=record['actor'];vm=record['vm_nonce']
    exact(record['case'],'controls-concurrent','development report kind')
    exact(record['tested_vcpus'],n,'all requested owners executed')
    require(type(vm) is int and 0<vm<1<<64,'nonzero bounded VM identity')
    require(len(record['owners'])==n,'one complete record per owner')
    captures=[o['capture_pc'] for o in record['owners']]
    for o in record['owners']:
        require(scalar.resident_pc(o['capture_pc']) and scalar.resident_pc(o['service_pc']),
                'capture and live compiler service belong to resident text')
    c=record['coordinator'];length=c['trace_length']
    require(type(length) is int and 0<length<=len(c['trace']),'bounded complete development trace')
    answer=replay_legacy_trace(family,n,actor,c['trace'][:length],captures,vm)
    before_source,before_data,before_tables=initial_memory(family,n)
    for key,wanted in (('source_before',before_source),('source_after',answer['source']),
                       ('data_before',before_data),('data_after',answer['data'])):
        exact(record[key],wanted,key)
    for report,model in (('tables','legacy'),('pae_tables','pae'),('mode_tables','ia32e')):
        exact(record[report+'_before'],before_tables[model],report+' initial complete image')
        exact(record[report+'_after'],answer['tables'][model],report+' shared ordered A/D effects')
    for cpu,(o,s) in enumerate(zip(record['owners'],answer['owners'])):
        initial,bank,guest,_=initial_owner(family,cpu)
        for key,wanted in (('id',cpu),('profile',family),('rip',SOURCE+cpu*256),('vm_nonce',vm),
                           ('status',1),('detail',0),('live',1),('current_el',4),('tcr',0x2b5103510),
                           ('requests',len(s['packets'])),('completed',s['completed']),
                           ('stage',2 if family==0 else 3),('operand',0),('width',0),
                           ('initial',initial),('entered',s['entered']),('final_frame',s['frame']),
                           ('guest_before',guest),('guest_after',s['guest']),('bank_before',bank),('bank_after',bank),
                           ('code_before',[0]*640),('code_after',s['code']),('compiler_requests',s['packets'])):
            exact(o[key],wanted,f'CPU{cpu}.{key}')
        require(o['sctlr']&0x1007==0x1005 and o['actlr']&2==2,'native execution and ordering settings')
        expected_state=dict(profile=family,cpl=0,generation=s['context'],visits=len(s['visits']),
            completed=s['completed'],transfers=len(s['packets']),serial=len(s['packets']),walks=s['walks'],
            invalidations=s['context']-1,mapping=s['mapping'][0] if s['mapping'] else 0,
            fixture_mutated=0,warm_before_reload=0,stale_reply=2 if family==0 else 0,
            stale_entry=int(family==0),expected=s['expected'],deferred=s['deferred'],late_reply=s['late'],
            rebound_reply=s['rebound'],old_slot_context=int(family==0),initial=initial,current=s['frame'],
            old_controls=guest['control'])
        exact(o['controls'],expected_state,f'CPU{cpu} context/cache/pending state')
        require(len(o['visits'])==20,'all visit slots retained')
        last_exception=[0]*4
        for index,wanted in enumerate(s['visits']):
            observed=o['visits'][index];wanted=copy.deepcopy(wanted)
            if wanted['faults']:
                esr,far,pc,spsr=observed['exception'];write=wanted['source']&255==0x89
                exact(esr>>26,0x25,'same-EL native data fault')
                exact(esr>>6&1,int(write),'native fault read/write intent')
                require(esr&63 in (range(12,16) if wanted['exception'][0]&63==15 else range(4,8)),
                        'native fault class follows prior mapping presence/rights')
                exact(esr&((1<<10)|(1<<8)|(1<<7)),0,'native fault has valid direct FAR')
                exact([far,pc],[DATA+cpu*256,scalar.CODE+index*32+(4 if write else 0)],'exact generated memory site')
                exact(spsr&0xf00003df,initial['nzcv']|0x3c4,'fault flags and native exception mode')
                wanted['exception']=list(observed['exception']);last_exception=list(observed['exception'])
            exact(observed,wanted,f'CPU{cpu} visit{index+1} full prefix and memory effect')
        empty={key:(scalar.zero_frame() if key in ('frame','fault_frame') else
                    [0]*len(value) if isinstance(value,list) else 0) for key,value in s['visits'][0].items()}
        exact(o['visits'][len(s['visits']):],[empty]*(20-len(s['visits'])),'unused visit slots remain zero')
        exact([o[key] for key in ('esr','far','elr','spsr')],last_exception,'last actual native fault')
        leaves=[0]*512
        if s['mapping']:leaves[256]=scalar.descriptor((s['mapping'][0]-DATA)//4096,not s['mapping'][1])
        exact(o['lower_leaf_after'],leaves,'only derived operand native mapping remains')
        exact(o['upper_leaf_after'],[0]*512,'no invented upper native data mapping')
        exact(o['pae'],pae.empty_state(),'legacy owner has no hidden PAE activity')
        exact(o['modes'],modes.empty_state(),'legacy owner has no long-mode metadata activity')
        for key in ('before_local','after_local'):
            exact(o['owner'][key],s[key],f'CPU{cpu} {key} architectural snapshot')
            exact(record['concurrent'][key.split('_')[0]][cpu],s[key],f'shared CPU{cpu} {key} snapshot')
        exact(o['owner']['initial'],snapshot(guest,dict(entries=[0]*4,generation=0),1,1,0,0,[0]*4),
              'owner initial canonical snapshot')
        exact(o['owner']['metadata_gpas'],[[0]*2 for _ in range(20)],'no unperformed metadata routes')
        exact(o['owner']['ttbr1'],0x40017000+cpu*0x100000,'private native upper root identity')
        if producer_directory is not None:
            from i4v_controls_worker import verify_compilations
            verify_compilations(Path(producer_directory)/f'cpu-{cpu}',s['packets'],s['replies'])
    unchanged_peers(record['concurrent']['before'],record['concurrent']['after'],actor,n)
    return dict(scope=answer['scope'],owners=n,visits=sum(len(s['visits']) for s in answer['owners']),
                native_entries=sum(v['entries'] for s in answer['owners'] for v in s['visits']),
                native_faults=sum(s['faults'] for s in answer['owners']),
                walks=sum(s['walks'] for s in answer['owners']),
                compilations=sum(len(s['packets']) for s in answer['owners']),trace_events=length)


class ConcurrentReplay:
    """Shared bytes and private architectural state, driven only by scheduling.

    ``none`` and ``retire`` preserve the explicitly incomplete development
    contracts preceding the positive current-entry/source rejection control.
    They are never substitutes for full campaign acceptance.
    """
    def __init__(self,family,n,actor,vm,captures,source_control='positive',revision=0,maintenance=False,peer_reply_control=True):
        require(family in FAMILIES and 1<=n<=4 and 0<=actor<n,'bounded concurrent family')
        require(source_control in ('none','retire','positive'),'explicit source-control evidence scope')
        require(len(captures)==n,'one native capture per owner')
        self.family,self.n,self.actor,self.vm=family,n,actor,vm
        self.source_control=source_control
        self.peer_reply_control=peer_reply_control
        self.revision=revision
        self.maintenance=maintenance;self.pressure=empty_maintenance(n)
        self.maintenance_started=0;self.maintenance_leaves=[[0]*5 for _ in range(36)]
        self.source,self.data,self.tables=initial_memory(family,n,revision)
        self.all=set(range(n));self.peers=self.all-{actor}
        self.active=set();self.queue=[];self.servicing=None;self.phase=0;self.retained=set()
        self.native_queue=[NO_CPU]*4
        self.local=0;self.held=set();self.drained=set();self.reply_held=set();self.reply_executed=set();self.second=0
        self.pae_ready=[set(),set()];self.pae_ack=[set(),set()];self.pae_published=set()
        self.final_ready=set();self.source_generation=1;self.source_ack=set();self.released=set();self.reclaimed=set()
        self.events=0;self.owners=[]
        self.first_memory={0:1,3:1,21:4,22:9,24:0}[family]
        self.first_cut={0:3,3:3,21:3,22:7,24:2}[family]
        self.second_cut={0:6,21:6,22:8,24:3}.get(family)
        self.final_visit={0:15,21:11,22:14,24:10}.get(family)
        self.limit={0:17,3:4,21:12,22:15,24:11}[family]
        if revision:
            extra={0:0,3:0,21:3,22:3,24:1}[family]
            self.limit+=extra
            if self.final_visit is not None:self.final_visit+=extra
        for cpu in range(n):
            frame,bank,guest,mode=initial_owner(family,cpu)
            self.owners.append(dict(guest=guest,frame=frame,initial=copy.deepcopy(frame),bank=bank,
                initial_guest=copy.deepcopy(guest),hidden=dict(entries=[0]*4,generation=0),context=1,
                source_generation=1,completed=0,visits=[],pending=None,route=0,writable=0,mapping=None,
                walks=0,walk_reads=0,walk_writes=0,attempts=0,reads=0,mutations=0,invalidations=0,
                packets=[],replies=[],code=[0]*640,entered=copy.deepcopy(frame),expected=[0]*16,
                deferred=[0]*16,late_reply=[0]*16,rebound_reply=[0]*16,old_slot_context=0,
                old_controls=list(guest['control']),stale_reply=0,stale_entry=0,
                before_local=None,after_local=None,before_peer_reply=None,after_peer_reply=None,
                peer_expected=[0]*16,peer_reply=[0]*16,source_expected=[0]*16,source_reply=[0]*16,
                code_before_retire=[0]*640,code_after_reclaim=[0]*640,cached_before_source=0,
                source_entry_rejected=0,source_rejections=0,reuse_blocked=0,retained_code=0,
                local_controls=0,local_cut=0,peer_reply_cut=0,peer_reply_executed=0,
                maintenance_acks=[[0]*6 for _ in range(36)],interval_visits=[0]*4,semantic_admissions=0,
                intervals=[empty_interval() for _ in range(4)],event_boundary=False,event_due=False,
                event=dict(consumed=0,deliveries=0,boundary=0,blocked=int(maintenance),
                    shadow=int(cpu==1),priority=4 if cpu==2 else 0,packet=copy.deepcopy(guest)),
                capture=captures[cpu],faults=0,finished=False,admitted=False,detached=False,
                pae_visits=[pae.empty_visit() for _ in range(20)],modes=mode,
                metadata_gpas=[[0]*2 for _ in range(20)]))

    def snap(self,cpu):
        s=self.owners[cpu]
        return snapshot(s['guest'],s['hidden'],s['context'],s['source_generation'],
                        s['completed'],s['route'],s['modes']['tr'])

    def resolve(self,cpu,address,write=False):
        s=self.owners[cpu]
        gpa,error=route(self.tables,s['guest'],s['hidden'],address,write,counts=s)
        exact(error,None,'admitted concurrent route must resolve')
        return gpa

    def writable(self,cpu,address):
        s=self.owners[cpu];g=s['guest'];control=g['control']
        active=modes.mode(control[0],control[3],control[4],g['segments'][0])
        if active==0:return True
        if active==1:
            directory=(control[2]&0xfffff000)-0x1000+(address>>22)*4
            pde=scalar.word(self.tables['legacy'],directory)
            leaf=scalar.word(self.tables['legacy'],(pde&0xfffff000)-0x1000+((address>>12)&1023)*4)
            allowed=bool(pde&leaf&2)
        elif active==2:
            base=s['hidden']['entries'][address>>30]&0xfffffff000
            pde=pae.qword(self.tables['pae'],base-0x1000+((address>>21)&511)*8)
            leaf=pae.qword(self.tables['pae'],(pde&0xfffffff000)-0x1000+((address>>12)&511)*8)
            allowed=bool(pde&leaf&2)
        else:
            base=0x6000;allowed=True
            for shift in (39,30,21,12):
                leaf=pae.qword(self.tables['ia32e'],base-0x6000+((address>>shift)&511)*8)
                allowed=allowed and bool(leaf&2);base=leaf&0xfffffff000
        return bool(leaf&64 and (allowed or not control[0]&0x10000))

    def publish(self,cpu):
        s=self.owners[cpu];p=s['pending'];at=p['index']*32
        exact(s['code'][at:at+32],[0]*32,'publish only into a blank generation-owned slot')
        body=scalar.body(p['instruction'])+scalar.RET;s['code'][at:at+len(body)]=body

    def compile(self,cpu,replacement=False):
        s=self.owners[cpu];g=s['guest'];p=s['pending'];index=p['index'];insn=p['instruction']
        active=modes.mode(g['control'][0],g['control'][3],g['control'][4],g['segments'][0])
        packet=request(self.vm,cpu,1,s['source_generation'],s['context'],active,g['rip'],insn,len(s['packets'])+1)
        response=scalar.reply(packet,insn);s['packets'].append(packet);s['replies'].append(response);s['expected']=packet
        p['visit'].update(request=packet,reply=response,entries=1)
        if not replacement and index==self.final_visit and self.family in (0,21,24):
            exact(g['control'],s['old_controls'],'ABA replays the same actual control tuple')
            require(s['context']!=s['old_slot_context'],'ABA must use a new context generation')
            s['late_reply']=scalar.reply(s['deferred'],owner_program(self.family,cpu)[4 if self.family==21 else 0])
            rebound=list(s['late_reply']);rebound[12]=packet[12];s['rebound_reply']=rebound
            exact([i for i in range(16) if rebound[i]!=response[i]],[6],'only old context differs after serial rebound')
            s['stale_reply']=2;s['stale_entry']=1
        if index!=self.final_visit or self.source_control=='none' or replacement:self.publish(cpu)
        defer_at=4 if self.family==21 else 0 if self.family in (0,24) else -1
        if not replacement and index==defer_at:
            s['old_slot_context']=s['context'];s['old_controls']=list(g['control'])
            deferred=list(packet);deferred[12]+=1;s['deferred']=deferred
            s['packets'].append(deferred);s['replies'].append(scalar.reply(deferred,insn));s['expected']=deferred
        entered=copy.deepcopy(s['frame']);entered['x'][16]=scalar.CODE+index*32;entered['x'][30]=s['capture']
        p['entry']=entered

    def fetch(self,cpu,index):
        s=self.owners[cpu];g=s['guest'];refetch=s['pending'] is not None
        if refetch:
            require(index==self.final_visit and cpu in self.reclaimed and s['source_generation']==2,
                    'only the same pending instruction may refetch after source retirement')
            p=s['pending'];exact(p['index'],index,'source refetch preserves visit identity')
            insn=scalar.immediate(0x11) if self.family==0 else b'\x89\x03'
            p['instruction']=insn
        else:
            require(not s['finished'] and index==len(s['visits']) and index<self.limit,'ordered bounded owner fetch')
            insn=instruction_at(self.family,cpu,g['rip'],self.revision)
            v=dict(rip=g['rip'],bytes=len(insn),kind=0 if insn[0] in (0xb8,0x89,0x8b) or insn==b'\x48\x8b\x03' else
                2 if insn[0]==0xe9 else 4 if insn==b'\x0f\x30' else 5 if insn[0]==0xea or insn==b'\x48\xff\x2e' else 1,
                source=0,fetch_gpa=0,before=list(g['control']),after=None,generation_before=s['context'],generation_after=None,
                gpr_before=g['gpr'][:4],gpr_after=None,flags_before=g['rflags'],flags_after=g['rflags'],exception=[0]*4,
                request=[0]*16,reply=[0]*16,entries=0,faults=0,gpa=0,memory_before=[0]*16,memory_after=[0]*16,
                vector=0,error=0,linear=0,frame=None,fault_frame=scalar.zero_frame())
            pv=pae.empty_visit();pv.update(before=copy.deepcopy(s['hidden']),attempts_before=s['attempts'],reads_before=s['reads'])
            mv=modes.empty_visit();mv.update(cs_before=list(g['segments'][0]),mode_before=modes.mode(
                g['control'][0],g['control'][3],g['control'][4],g['segments'][0]))
            p=dict(instruction=insn,visit=v,index=index,admitted=False,fault=False,pae=pv,modes=mv,metadata_reads=0)
            s['pending']=p
        gpa=self.resolve(cpu,g['rip']);exact(self.source[gpa-SOURCE:gpa-SOURCE+len(insn)],list(insn),'resolved current source bytes')
        p['visit'].update(source=int.from_bytes(insn,'little'),fetch_gpa=gpa)
        if p['visit']['kind']==0:
            self.compile(cpu,refetch)
            if insn[0]==0xb8:self.interval_start(cpu)
        elif not refetch and self.maintenance:s['semantic_admissions']+=1

    def admit(self,cpu):
        s=self.owners[cpu];p=s['pending'];insn=p['instruction'];v=p['visit']
        require(insn in (b'\x89\x03',b'\x8b\x03',b'\x48\x8b\x03') and cpu in self.active
                and not p['admitted'],'one admitted generated memory access')
        write=insn[0]==0x89;address=DATA+cpu*256;gpa=self.resolve(cpu,address,write)
        s['route']=gpa;s['writable']=int(self.writable(cpu,address));p['admitted']=True
        p['needs_fault']=s['mapping'] is None or (write and not s['mapping'][1]);p['permission']=s['mapping'] is not None
        v['gpa']=gpa;v['memory_before']=self.data[gpa-DATA:gpa-DATA+16]
        self.interval_start(cpu)

    def fault(self,cpu):
        s=self.owners[cpu];p=s['pending'];v=p['visit'];write=p['instruction'][0]==0x89
        require(p['admitted'] and p['needs_fault'] and not p['fault'] and s['detached'],'one necessary native fault')
        p['fault']=True;v['faults']=1;s['faults']+=1;v['fault_frame']=copy.deepcopy(p['entry'])
        v['exception']=[(0x25<<26)|(64 if write else 0)|(15 if p['permission'] else 7),DATA+cpu*256,
            scalar.CODE+p['index']*32+(4 if write else 0),p['entry']['nzcv']|0x3c4]
        g=s['guest'];mask=(1<<64)-1 if modes.mode(g['control'][0],g['control'][3],g['control'][4],g['segments'][0])==4 else 0xffffffff
        for guest,native in ((0,4),(1,7),(2,5),(3,6)):g['gpr'][guest]=p['entry']['x'][native]&mask
        if cpu in self.peers and p['index']==self.first_memory:s['before_local']=self.snap(cpu)

    def metadata_read(self,cpu):
        s=self.owners[cpu];p=s['pending'];mv=p['modes'];g=s['guest'];insn=p['instruction']
        require(p['visit']['kind']==5,'metadata read belongs to an actual far transfer')
        self.semantic_cut_ready(cpu)
        if insn==b'\x48\xff\x2e' and p['metadata_reads']==0:
            address=g['gpr'][6];gpa=self.resolve(cpu,address);value=self.data[gpa-DATA:gpa-DATA+10]
            mv.update(pointer_address=address,pointer_bytes=value+[0]*6)
            p['target']=int.from_bytes(bytes(value[:8]),'little');p['selector']=int.from_bytes(bytes(value[8:]),'little')
            s['modes']['pointer_reads']+=1;s['metadata_gpas'][p['index']][0]=gpa
        else:
            exact(p['metadata_reads'],1 if insn==b'\x48\xff\x2e' else 0,'one descriptor after optional actual pointer')
            if insn[0]==0xea:
                p['target']=int.from_bytes(insn[1:5],'little');p['selector']=int.from_bytes(insn[5:],'little')
            selector=p['selector'];require(selector in (8,0x18),'admitted code selector')
            address=s['modes']['gdtr'][0]+selector;gpa=self.resolve(cpu,address);desc=pae.qword(self.data,gpa-DATA)
            mv.update(descriptor_address=address,descriptor=desc);p['descriptor']=desc
            s['modes']['descriptor_reads']+=1;s['metadata_gpas'][p['index']][1]=gpa
        s['modes']['reads']+=1;p['metadata_reads']+=1

    def semantic_cut_ready(self,cpu):
        if cpu!=self.actor:return
        index=self.owners[cpu]['pending']['index']
        if index==self.first_cut:
            require(self.local==1 and self.held==self.peers and set(self.queue)==self.peers
                    and not self.active and self.servicing is None
                    and all(self.owners[peer]['before_local'] is not None for peer in self.peers),
                    'actual first actor transaction occurs only across every detached retained peer')
        if index==self.second_cut and self.peer_reply_control:
            require(self.second==1 and self.reply_held==self.peers
                    and all(self.owners[peer]['pending'] is not None and
                            self.owners[peer]['pending']['visit']['request']==self.owners[peer]['peer_expected']
                            for peer in self.peers),
                    'actual second actor transaction occurs only across held still-current peer replies')

    def complete(self,cpu,native):
        s=self.owners[cpu];p=s['pending'];v=p['visit'];g=s['guest'];insn=p['instruction']
        exact(native,v['kind']==0,'native versus semantic completion')
        if native:
            current=copy.deepcopy(p['entry']);s['entered']=copy.deepcopy(current)
            if insn[0]==0xb8:current['x'][4]=int.from_bytes(insn[1:],'little')
            else:
                require(p['admitted'],'native memory operation retains an admitted route')
                if cpu in self.peers and p['index']==self.first_memory:
                    require(s['after_local'] is not None,'retained peer executes only after its own local isolation check')
                exact(p['fault'],p['needs_fault'],'native mapping repaired iff needed')
                if p['fault']:exact(self.servicing,cpu,'faulting body resumes at FIFO head')
                at=v['gpa']-DATA;width=8 if insn[0]==0x48 else 4
                if insn[0]==0x89:self.data[at:at+4]=(current['x'][4]&0xffffffff).to_bytes(4,'little')
                else:current['x'][4]=int.from_bytes(bytes(self.data[at:at+width]),'little')
                v['memory_after']=self.data[at:at+16]
                if p['fault']:s['mapping']=(v['gpa'],bool(s['writable']))
                else:self.active.remove(cpu);s['admitted']=False;self.reopen()
            s['frame']=current
            mask=(1<<64)-1 if modes.mode(g['control'][0],g['control'][3],g['control'][4],g['segments'][0])==4 else 0xffffffff
            for guest,register in ((0,4),(1,7),(2,5),(3,6)):g['gpr'][guest]=current['x'][register]&mask
            g['rip']+=len(insn);s['completed']+=1
        elif insn[0]==0xe9:
            g['rip']=(g['rip']+5+int.from_bytes(insn[1:],'little',signed=True))&0xffffffff;s['completed']+=1
        else:
            self.semantic_cut_ready(cpu)
            error=None
            if v['kind']==1:
                result,error,addresses,_=control_transition(self.tables,g,s['hidden'],s['context'],insn)
                if addresses:
                    s['attempts']+=1;s['reads']+=len(addresses);p['pae']['load_addresses']=addresses
                if not error:s['guest'],s['hidden'],s['context']=result;g=s['guest']
            elif v['kind']==4:
                exact(g['gpr'][1:3],[0xc0000080,0],'actual EFER MSR selection')
                value=g['gpr'][0];require(not value&~0xd01 and not (g['control'][0]&0x80000000 and (value^g['control'][4])&0x100),
                                        'valid finite EFER transition')
                g['control'][4]=(value&~0x400)|(g['control'][4]&0x400);g['rip']+=2;s['context']+=1
            else:
                exact(p['metadata_reads'],2 if insn==b'\x48\xff\x2e' else 1,'complete actual far metadata reads')
                desc=p['descriptor'];attr=((desc>>40)&255)|((desc>>40)&0xf000)
                require(attr&0x98==0x98 and attr&0x6000!=0x6000,'present legal code descriptor')
                g['segments'][0]=[p['selector'],0,0xffffffff,attr];g['rip']=p['target'];s['context']+=1
            if error:
                exact((self.family,p['index'],error),(3,3,13),'only declared terminal local-control rejection')
                v['vector']=error
            else:
                s['mapping']=None;s['invalidations']+=1;s['local_controls']+=1;s['completed']+=1
        v.update(after=list(g['control']),generation_after=s['context'],gpr_after=g['gpr'][:4],frame=copy.deepcopy(s['frame']))
        if native and self.maintenance and self.pressure['ordinary']==cpu:
            out=s['intervals'][self.pressure['intervals'][cpu]]
            require(out['ordinal']!=0,'actual ordinary instruction has a captured entry')
            out.update(captured=copy.deepcopy(s['frame']),after=self.data[out['gpa']-DATA:out['gpa']-DATA+256])
            if v['faults']:
                out['faults']=v['faults'];out['dirty_frame']=copy.deepcopy(v['fault_frame'])
                out['esr'],out['far'],out['pc'],out['spsr']=v['exception']
        if self.family>=13:
            p['pae'].update(after=copy.deepcopy(s['hidden']),attempts_after=s['attempts'],reads_after=s['reads'])
            s['pae_visits'][p['index']]=p['pae']
        if self.family>=22:
            p['modes'].update(cs_after=list(g['segments'][0]),mode_after=modes.mode(g['control'][0],g['control'][3],g['control'][4],g['segments'][0]))
            s['modes']['visits'][p['index']]=p['modes']
        s['visits'].append(v);s['pending']=None

    def reopen(self):
        if not self.queue and not self.active and self.servicing is None:self.phase=0


    def table_leaves(self):
        if self.family<13:
            return [scalar.word(self.tables['legacy'],0x1400),scalar.word(self.tables['legacy'],0x3400),0,0,0]
        return [0,0,pae.qword(self.tables['pae'],0x2800),pae.qword(self.tables['pae'],0x4800),
                pae.qword(self.tables['ia32e'],0x3800) if self.family>=22 else 0]

    def event_blockers(self,cpu):
        s=self.owners[cpu];e=s['event']
        semantic=s['pending'] is not None and s['pending']['visit']['kind']!=0
        return (2 if self.phase!=0 else 0)|(4 if not s['guest']['rflags']&0x200 else 0)|(
            8 if e['shadow'] else 0)|(16 if e['priority']>=4 else 0)|(
            32 if s['completed']==0 else 0)|(64 if cpu in self.active or semantic else 0)

    def event_attempt(self,cpu):
        s=self.owners[cpu];e=s['event']
        if e['deliveries']:return
        blockers=self.event_blockers(cpu)
        e['blocked']|=blockers
        if blockers==0:s['event_due']=True

    def interval_start(self,cpu):
        if not self.maintenance or self.pressure['ordinary']!=cpu:return
        s=self.owners[cpu];p=s['pending'];m=self.pressure;slot=m['intervals'][cpu]
        exact(s['intervals'][slot]['ordinal'],0,'one generated instruction per ordinary grant')
        gpa=s['route'] if p['instruction'][0]!=0xb8 else self.resolve(cpu,DATA+cpu*256)
        s['intervals'][slot].update(ordinal=m['ordinary_total']+1,heads=m['total'],epoch=m['epoch'],gpa=gpa,
            initial=copy.deepcopy(p['entry']),before=self.data[gpa-DATA:gpa-DATA+256])
        s['interval_visits'][slot]=p['index']+1
        self.event_attempt(cpu)

    def pressure_event(self,kind,cpu):
        require(self.maintenance and self.family!=3,'maintenance must be in its declared concurrent scope')
        m=self.pressure;s=self.owners[cpu]
        if not self.maintenance_started:
            ready=self.pae_ack[1]==self.all if self.family==21 else self.second==2 and self.reply_executed==self.peers
            require(ready and not any(o['pending'] for o in self.owners),'all owners enter pressure between actual instructions')
            self.maintenance_started=1;m['ready']=(1<<self.n)-1
            m['compose_leaf']=self.table_leaves()[0 if self.family<13 else 2]
        if kind==23:
            require(m['pending'][cpu]==0 and m['ordinary']!=cpu and m['submitted'][cpu]<9 and not m['finished'],
                    'one bounded outstanding request per owner')
            require(m['length']<self.n,'bounded FIFO capacity')
            m['serial']+=1;m['submitted'][cpu]+=1;m['pending'][cpu]=m['serial']
            m['queue'][(m['head']+m['length'])%self.n]=dict(cpu=cpu,serial=m['serial']);m['length']+=1
            m['duplicate_rejections']+=1;m['maximum_depth']=max(m['maximum_depth'],m['length']+int(m['current']['serial']!=0))
        elif kind==25:
            require(m['current']['serial']==0 and m['ordinary']==NO_CPU and m['length']>0 and m['batch']<8
                    and not self.active and not self.queue and self.servicing is None
                    and not any(o['pending'] for o in self.owners),'bounded protected head excludes native/semantic execution')
            current=m['queue'][m['head']];exact(current['cpu'],cpu,'maintenance FIFO owner')
            exact(m['pending'][cpu],current['serial'],'maintenance retains original request serial')
            m['head']=(m['head']+1)%self.n;m['length']-=1;m['current']=dict(current)
            m['epoch']+=1;m['acknowledgements']=0;self.phase=2
            leaves=self.table_leaves()
            invert=lambda value:(DATA+4096 if value&0xfffffff000==DATA else DATA)|7
            if self.family<13:
                scalar.word(self.tables['legacy'],0x1400,invert(leaves[0]));scalar.word(self.tables['legacy'],0x3400,invert(leaves[1]))
            else:
                pae.qword(self.tables['pae'],0x2800,invert(leaves[2]));pae.qword(self.tables['pae'],0x4800,invert(leaves[3]))
                if self.family>=22:pae.qword(self.tables['ia32e'],0x3800,invert(leaves[4]))
        elif kind==26:
            require(m['current']['serial']!=0 and m['ordinary']==NO_CPU and not m['acknowledgements']&(1<<cpu)
                    and not self.active and not any(o['pending'] for o in self.owners),'one isolated acknowledgement per protected head')
            if cpu==m['epoch']%self.n:exact(m['acknowledgements'],((1<<self.n)-1)^(1<<cpu),'last acknowledgement deliberately delayed')
            s['mapping']=None;s['invalidations']+=1;gpa=self.resolve(cpu,DATA+cpu*256)
            s['mapping']=(gpa,False);s['route']=gpa;s['writable']=0
            g=s['guest'];active=modes.mode(g['control'][0],g['control'][3],g['control'][4],g['segments'][0])
            s['maintenance_acks'][m['total']]=[m['epoch'],gpa,active,s['context'],s['hidden']['generation'],
                                              scalar.descriptor((gpa-DATA)//4096,True)]
            m['acknowledgements']|=1<<cpu
            if m['acknowledgements']==(1<<self.n)-1:
                leaves=self.table_leaves();self.maintenance_leaves[m['total']]=leaves
                m['leaf_history'][m['total']]=leaves[0 if self.family<13 else 2]
                current=m['current'];m['request_history'][m['total']]=dict(current)
                m['pending'][current['cpu']]=0;m['completed'][current['cpu']]+=1;m['total']+=1;m['batch']+=1
                m['current']=dict(cpu=NO_CPU,serial=0);self.phase=1
        elif kind==29:
            require(m['ordinary']==NO_CPU and m['current']['serial']==0 and m['pending'][cpu]==0 and 1<=m['batch']<=8
                    and not self.active and not self.queue and not any(o['pending'] for o in self.owners),
                    'ordinary grant excludes every protected/native/semantic head')
            exact(cpu,m['ordinary_total']%self.n,'round-robin ordinary progress')
            require(m['intervals'][cpu]<4,'bounded per-owner ordinary storage')
            m['ordinary']=cpu;m['closing']=1;self.phase=0
        elif kind==30:
            require(m['ordinary']==cpu and s['pending'] is None and not s['admitted'] and m['closing']==1,
                    'ordinary grant completes only after one actual generated instruction')
            out=s['intervals'][m['intervals'][cpu]];require(out['ordinal']!=0,'recorded real ordinary execution')
            exact(s['interval_visits'][m['intervals'][cpu]],len(s['visits']),'interval names its actual completed visit')
            m['intervals'][cpu]+=1;m['ordinary_total']+=1;m['ordinary']=NO_CPU;m['batch']=0
            m['next_ordinary']=(cpu+1)%self.n
            if m['total']==9*self.n:m['finished']=1
            self.phase=0;s['event_boundary']=True;self.event_attempt(cpu)
        else:raise ValueError('unknown pressure event')


    def step(self,word):
        kind,cpu,index=decode_event(word,self.n);s=self.owners[cpu];self.events+=1
        if self.maintenance and s['event_due'] and kind in (0x3000,0x4000,0x5000):
            raise ValueError('eligible event packet must be handed off before the owner executes further')
        if kind in (23,25,26,29,30):self.pressure_event(kind,cpu);return
        if kind in (20,22):
            require(self.maintenance,'event handoff must be in its declared concurrent scope')
            e=s['event']
            if all(o['finished'] for o in self.owners):s['event_boundary']=True;self.event_attempt(cpu)
            if kind==22:
                require(s['event_boundary'] and (e['shadow']!=0 or e['priority']!=0),'expire only the declared event fixture at a boundary')
                self.event_attempt(cpu);e['shadow']=0;e['priority']=0;self.event_attempt(cpu)
            else:
                require(s['event_due'] and not e['deliveries'],'one eligible complete canonical event handoff')
                exact(self.event_blockers(cpu),0,'event handoff rechecks current exclusion and canonical eligibility predicates')
                e.update(consumed=1,deliveries=1,boundary=s['completed'],packet=copy.deepcopy(s['guest']));s['event_due']=False
            return
        if kind==0x1000:
            if self.maintenance_started and not self.pressure['finished']:
                exact(self.pressure['ordinary'],cpu,'only granted owner may advance while maintenance is pending')
            if cpu==self.actor:
                require(self.local!=2,'actor cannot advance before retained peers drain')
                require(self.second!=2 or self.reply_executed==self.peers,'actor waits for current peer replies to execute')
            if self.family==21:
                for barrier,completed in enumerate((5,8)):
                    if s['completed']==completed:
                        exact(self.pae_ack[barrier],self.all,'shared root publication acknowledged before next fetch')
            self.fetch(cpu,index);return
        if kind in (0x2000,0x3000,0x4000,0x5000,0x6000,0x7000):
            require(s['pending'] is not None,'instruction event has one pending fetched instruction')
            exact(index,s['pending']['index'],'event current instruction index')
            if kind==0x2000:self.admit(cpu)
            elif kind==0x5000:self.fault(cpu)
            elif kind==0x6000:self.metadata_read(cpu)
            elif kind==0x7000:
                require(self.source_control=='positive' and index==self.final_visit and s['source_generation']==1
                        and s['cached_before_source']==0,'actual current-source positive entry control')
                self.publish(cpu);s['cached_before_source']=1
            else:self.complete(cpu,kind==0x4000)
            return
        if kind==1:
            require(self.phase==0 and cpu not in self.active and not s['admitted'],'admit only while OPEN')
            self.active.add(cpu)
            if s['source_generation']==1:self.retained.add(cpu)
            s['admitted']=True
        elif kind==2:
            require(cpu in self.active and cpu not in self.queue and self.phase in (0,1),'detach one admitted route')
            self.native_queue[len(self.queue)]=cpu
            self.active.remove(cpu);self.queue.append(cpu);s['detached']=True;self.phase=1
        elif kind==3:
            require(cpu==0 and self.phase==1 and not self.active and self.queue,'exclusive only after admitted routes detach')
            self.phase=2
        elif kind==4:
            require(self.phase==2 and self.servicing is None and self.queue and self.queue[0]==cpu,'FIFO native repair head')
            # Any waiter can designate the FIFO head. Only the designated
            # owner may resume, after its own event96 isolation check.
            self.servicing=cpu
        elif kind==5:
            require(self.servicing==cpu and self.queue[0]==cpu and s['pending'] is None,'release only completed native head')
            self.queue.pop(0);self.servicing=None;s['detached']=False;s['admitted']=False;self.reopen()
        elif kind==90:
            require(cpu==self.actor and self.local==0 and s['completed']==self.first_cut,'designated actor local transaction')
            s['before_local']=self.snap(cpu);s['local_cut']=1;self.local=1
        elif kind==94:
            require(cpu in self.peers and self.local==1 and cpu not in self.held
                    and s['pending']['index']==self.first_memory,'one first generated memory access retained per peer')
            self.held.add(cpu);s['retained_code']=1
        elif kind==95:
            require(self.n>=3 and cpu==(self.actor+self.n-1)%self.n and self.held==self.peers
                    and len(self.queue)==self.n-2,'last retained native peer deliberately delayed')
        elif kind in (91,92):
            require(cpu==self.actor and self.local==1 and self.held==self.peers and set(self.queue)==self.peers,
                    'actor commits/rejects across every detached retained peer route')
            exact(kind,92 if self.family==3 else 91,'commit versus rejected transaction')
            s['after_local']=self.snap(cpu);s['local_cut']=2;self.local=2
        elif kind==96:
            require(cpu in self.peers and self.local==2 and s['before_local'] is not None,'retained peer resumes after actor transaction')
            s['after_local']=self.snap(cpu);s['local_cut']=2
            exact(s['after_local'],s['before_local'],'local actor transition preserves all peer state and route')
        elif kind==97:
            require(cpu in self.peers and self.local==2 and s['completed']==self.first_memory+1 and not s['detached'],
                    'one retained generated operation completes before drain')
            self.drained.add(cpu)
        elif kind==93:
            require(cpu==self.actor and self.local==2 and self.drained==self.peers,'actor continues after all retained peers complete')
            self.local=3
        elif kind==99:
            require(self.peer_reply_control and cpu in self.peers and self.family!=3 and cpu not in self.reply_held and s['pending'] is not None
                    and s['pending']['index']>self.first_memory and s['pending']['visit']['entries']==1,
                    'hold the next actual ordinary compiler reply before publication')
            s['peer_expected']=list(s['pending']['visit']['request']);s['peer_reply']=list(s['pending']['visit']['reply'])
            s['before_peer_reply']=self.snap(cpu);s['peer_reply_cut']=1;self.reply_held.add(cpu)
        elif kind==100:
            require(cpu==self.actor and self.local==3 and self.second==0 and self.reply_held==self.peers
                    and s['completed']==self.second_cut,'second actor transaction waits for all still-current peer replies')
            self.second=1;s['local_cut']=3
        elif kind==101:
            require(cpu==self.actor and self.second==1 and s['completed']==self.second_cut+1,
                    'second local semantic transaction actually committed')
            self.second=2;s['local_cut']=4
        elif kind==102:
            require(cpu in self.reply_held and self.second==2 and s['peer_reply_cut']==1,'current peer reply resumes after actor commit')
            s['after_peer_reply']=self.snap(cpu);s['peer_reply_cut']=2
            exact(s['after_peer_reply'],s['before_peer_reply'],'current peer compiler identity/state locally isolated')
            exact(s['pending']['visit']['request'],s['peer_expected'],'still-due request remains current')
        elif kind==103:
            require(cpu in self.reply_held and cpu not in self.reply_executed and s['peer_reply_cut']==2
                    and s['pending'] is None,'accepted current peer reply actually executed')
            exact(s['visits'][-1]['request'],s['peer_expected'],'native completion belongs to held current reply')
            s['peer_reply_executed']=1;self.reply_executed.add(cpu)
        elif kind in (108,109):
            barrier=kind-108
            require(self.family==21 and s['completed']==(5,8)[barrier] and s['pending'] is None
                    and not s['admitted'] and cpu not in self.pae_ready[barrier],
                    'each PAE owner reaches the complete pre-mutation guest prefix')
            self.pae_ready[barrier].add(cpu)
        elif kind in (110,111):
            barrier=kind-110
            require(self.family==21 and cpu==self.actor and self.pae_ready[barrier]==self.all
                    and barrier not in self.pae_published and not self.active and not self.queue and self.servicing is None,
                    'shared raw PDPT mutation only after every admitted route drains')
            pae.qword(self.tables['pae'],0x20,(0x4001,0x2001)[barrier]);s['mutations']+=1
            self.pae_published.add(barrier);self.phase=2
        elif kind in (112,113):
            barrier=kind-112
            require(barrier in self.pae_published and cpu not in self.pae_ack[barrier],'one acknowledgement after shared PDPT change')
            # Raw table mutation itself never reloads any private hidden roots.
            exact(s['hidden'],dict(entries=[(0x2001,0x4001)[barrier],0,0,0],generation=barrier+1),
                  'shared root bytes do not silently replace a CPU hidden snapshot')
            self.pae_ack[barrier].add(cpu)
            if self.pae_ack[barrier]==self.all:self.phase=0
        elif kind==114:
            require(self.source_control!='none' and self.family!=3 and cpu not in self.final_ready and s['pending'] is not None
                    and s['pending']['index']==self.final_visit and s['source_generation']==1,
                    'all source-cut owners hold an actual old-source final compilation')
            if self.maintenance:exact(self.pressure['finished'],1,'source mutation follows real sustained ordinary progress')
            if self.source_control=='positive':exact(s['cached_before_source'],1,'old current-source entry was positively eligible')
            s['source_expected']=list(s['pending']['visit']['request']);s['source_reply']=list(s['pending']['visit']['reply'])
            s['code_before_retire']=list(s['code']);s['retained_code']=1;self.final_ready.add(cpu)
        elif kind==115:
            require(cpu==self.actor and self.source_generation==1 and self.final_ready==self.all and not self.active
                    and not self.queue and self.servicing is None and self.retained==self.all,
                    'shared source writer excludes admitted operations and retains every old code reference')
            self.phase=2;self.source_generation=2
            replacement=scalar.immediate(0x11) if self.family==0 else b'\x89\x03'
            offset=0 if self.family in (0,24) else 16 if self.family==21 else sum(map(len,owner_program(self.family,0,self.revision)))-2
            for owner in range(self.n):self.source[owner*256+offset:owner*256+offset+len(replacement)]=replacement
        elif kind==116:
            require(self.source_generation==2 and cpu not in self.source_ack,'one source acknowledgement per owner')
            if cpu==(self.actor+self.n-1)%self.n:exact(self.source_ack,self.all-{cpu},'delayed source acknowledgement comes last')
            s['source_generation']=2;s['mapping']=None;s['invalidations']+=1;s['source_rejections']=1
            if self.source_control=='positive':s['source_entry_rejected']=1
            current=list(s['source_expected']);current[5]=2
            expected_reply=scalar.reply(current,s['pending']['instruction'])
            exact([i for i in range(16) if expected_reply[i]!=s['source_reply'][i]],[5],
                  'actual old compiler reply differs only in source generation before fresh request')
            self.source_ack.add(cpu)
        elif kind==117:
            require(self.source_ack==self.all and self.retained and not s['reuse_blocked'],
                    'code reuse blocked while an old code reference remains')
            exact(s['code'],s['code_before_retire'],'old code bytes remain intact until reference release')
            s['reuse_blocked']=1
        elif kind==118:
            require(self.source_ack==self.all and cpu in self.retained and s['reuse_blocked'],
                    'release one retained code reference after all source acknowledgements')
            if cpu==(self.actor+self.n-1)%self.n:exact(self.retained,{cpu},'delayed code owner releases last')
            self.retained.remove(cpu);self.released.add(cpu)
            if not self.retained:self.phase=0
        elif kind==119:
            require(self.released==self.all and not self.retained and cpu not in self.reclaimed,
                    'all old references released before any owner erases old code')
            s['code']=[0]*640;s['code_after_reclaim']=[0]*640;self.reclaimed.add(cpu)
        elif kind==98:
            require(s['pending'] is None and not s['admitted'] and not s['finished'],'finished owner has no active native operation')
            exact((len(s['visits']),s['completed']),(self.limit,self.limit-int(self.family==3)),'complete bounded concurrent program')
            if self.source_control!='none' and self.family!=3:require(cpu in self.reclaimed,'source-cut owner retired old code before finishing')
            s['finished']=True
        else:raise ValueError(f'concurrent event {kind} is not modeled; full acceptance remains disabled')

    def run(self,trace):
        for word in trace:self.step(word)
        require(all(s['finished'] for s in self.owners) and self.local==3 and not self.active
                and not self.queue and self.servicing is None,'complete development trace with drained routes')
        if self.family!=3 and self.peer_reply_control:
            exact(self.second,2,'second local transaction completed');exact(self.reply_executed,self.peers,'every held current peer reply executed')
        if self.family==21:
            exact(self.pae_published,{0,1},'both shared PDPT mutations executed')
            exact(self.pae_ack,[self.all,self.all],'every owner acknowledges both raw root changes')
        if self.source_control!='none' and self.family!=3:
            exact(self.reclaimed,self.all,'all private old code reclaimed')
        if self.maintenance:
            for s in self.owners:exact(s['event']['deliveries'],1,'each owner receives one canonical event handoff')
            m=self.pressure
            if self.family==3:exact(m,empty_maintenance(self.n),'rejected-control profile does not fabricate pressure progress')
            else:
                exact(m['submitted'],[9]*self.n+[0]*(4-self.n),'nine real requests per owner')
                exact(m['completed'],m['submitted'],'every original request completes')
                exact(m['finished'],1,'bounded pressure finishes after generated ordinary work')
                require(m['current']['serial']==0 and m['ordinary']==NO_CPU and m['length']==0
                        and m['pending']==[0]*4 and m['maximum_depth']==self.n,'all queue slots exercised and fully drained')
                require(m['ordinary_total']>=(m['total']+7)//8 and all(m['intervals'][:self.n]),
                        'eight-head bound and ordinary progress for every owner')
        return self


def audit_development(record,producer_directory=None,source_control='positive',revision=0,maintenance=False,peer_reply_control=True):
    """Exact architectural/source evidence audit; maintenance acceptance is separate."""
    family,n,actor,vm=(record[k] for k in ('profile','configured_vcpus','actor','vm_nonce'))
    exact(record['case'],'controls-concurrent','concurrent report identity')
    exact(record['tested_vcpus'],n,'complete configured owner set')
    require(type(vm) is int and 0<vm<1<<64,'nonzero bounded VM incarnation')
    require(len(record['owners'])==n,'one result per configured owner')
    captures=[o['capture_pc'] for o in record['owners']]
    require(all(scalar.resident_pc(pc) for pc in captures),'all native capture PCs lie in resident text')
    coord=record['coordinator'];length=coord['trace_length']
    require(type(length) is int and 0<length<=len(coord['trace']),'bounded recorded schedule')
    model=ConcurrentReplay(family,n,actor,vm,captures,source_control,revision,maintenance,peer_reply_control).run(coord['trace'][:length])
    before_source,before_data,before_tables=initial_memory(family,n,revision)
    for key,value in (('source_before',before_source),('source_after',model.source),
                      ('data_before',before_data),('data_after',model.data)):
        exact(record[key],value,key)
    for field,key in (('tables','legacy'),('pae_tables','pae'),('mode_tables','ia32e')):
        exact(record[field+'_before'],before_tables[key],field+' full initial image')
        exact(record[field+'_after'],model.tables[key],field+' exact shared ordered effects')
    zero_snapshot=snapshot(dict(control=[0]*8,segments=[[0]*4],rip=0,gpr=[0]*4),
                           dict(entries=[0]*4,generation=0),0,0,0,0,[0]*4)
    shared_before=[copy.deepcopy(zero_snapshot) for _ in range(4)];shared_after=copy.deepcopy(shared_before)
    for cpu,(o,s) in enumerate(zip(record['owners'],model.owners)):
        initial=s['initial'];guest=s['initial_guest'];bank=s['bank']
        require(scalar.resident_pc(o['service_pc']),'live compiler service PC lies in resident text')
        for key,value in (('id',cpu),('case','controls32'),('profile',family),('rip',SOURCE+cpu*256),('vm_nonce',vm),
            ('status',1),('detail',0),('live',1),('current_el',4),('tcr',0x2b5103510),('requests',len(s['packets'])),
            ('request',[0]*16),('reply',[0]*16),('completed',s['completed']),('stage',3 if family==3 else 2),
            ('operand',0),('width',0),('initial',initial),('entered',s['entered']),('final_frame',s['frame']),
            ('guest_before',guest),('guest_after',s['guest']),('bank_before',bank),('bank_after',bank),
            ('code_before',[0]*640),('code_after',s['code']),('compiler_requests',s['packets'])):
            exact(o[key],value,f'CPU{cpu}.{key}')
        require(o['sctlr']&0x1007==0x1005 and o['actlr']&2==2,'native mapping/ordering/unaligned settings')
        state=dict(profile=family,cpl=0,generation=s['context'],visits=len(s['visits']),completed=s['completed'],
            transfers=len(s['packets']),serial=len(s['packets']),walks=s['walks'],invalidations=s['invalidations'],
            mapping=s['mapping'][0] if s['mapping'] else 0,fixture_mutated=0,warm_before_reload=0,
            stale_reply=s['stale_reply'],stale_entry=s['stale_entry'],expected=s['expected'],deferred=s['deferred'],
            late_reply=s['late_reply'],rebound_reply=s['rebound_reply'],old_slot_context=s['old_slot_context'],
            initial=initial,current=s['frame'],old_controls=s['old_controls'])
        exact(o['controls'],state,f'CPU{cpu} all context/cache/compiler/progress state')
        ps=pae.empty_state();ps.update(hidden=s['hidden'],visits=s['pae_visits'])
        for key in ('attempts','reads','walk_reads','walk_writes','mutations'):ps[key]=s[key]
        exact(o['pae'],ps,f'CPU{cpu} hidden-root lifecycle and individual shared-table accesses')
        exact(o['modes'],s['modes'],f'CPU{cpu} complete descriptor/pointer and CS/mode history')
        require(len(o['visits'])==20,'all bounded instruction records present')
        last_exception=[0]*4
        for index,wanted in enumerate(s['visits']):
            wanted=copy.deepcopy(wanted);observed=o['visits'][index]
            if wanted['faults']:
                esr,far,pc,spsr=observed['exception'];write=wanted['source']&255==0x89
                exact(esr>>26,0x25,'same-EL native data abort');exact(esr>>6&1,int(write),'native access direction')
                require(esr&63 in (range(12,16) if wanted['exception'][0]&63==15 else range(4,8)),
                        'fault class agrees with prior native leaf presence and rights')
                exact(esr&((1<<10)|(1<<8)|(1<<7)),0,'direct native fault has valid FAR')
                exact([far,pc],[DATA+cpu*256,scalar.CODE+index*32+(4 if write else 0)],'actual generated memory site')
                exact(spsr&0xf00003df,initial['nzcv']|0x3c4,'native exception mode/flags')
                wanted['exception']=list(observed['exception']);last_exception=list(observed['exception'])
            exact(observed,wanted,f'CPU{cpu} visit{index+1}: full guest/native prefix and exact effects')
        empty={key:(scalar.zero_frame() if key in ('frame','fault_frame') else
                    [0]*len(value) if isinstance(value,list) else 0) for key,value in s['visits'][0].items()}
        exact(o['visits'][len(s['visits']):],[empty]*(20-len(s['visits'])),'unused instruction records')
        exact([o[key] for key in ('esr','far','elr','spsr')],last_exception,'last actual native fault association')
        leaf=[0]*512
        if s['mapping']:leaf[256]=scalar.descriptor((s['mapping'][0]-DATA)//4096,not s['mapping'][1])
        exact(o['lower_leaf_after'],leaf,'exact final private native data route/rights')
        exact(o['upper_leaf_after'],[0]*512,'no unintended native upper mappings')
        upper=[0]*512;upper[256]=0x40018003+cpu*0x100000
        exact(o['upper_root_after'],upper,'private native upper table root')
        owner=dict(enabled=1,ttbr1=0x40017000+cpu*0x100000,incarnation=1,source_generation=s['source_generation'],
            admitted=0,route=s['route'],writable=s['writable'],detached=0,local_cut=s['local_cut'],
            peer_reply_cut=s['peer_reply_cut'],peer_reply_executed=s['peer_reply_executed'],local_controls=s['local_controls'],
            source_rejections=s['source_rejections'],binding_rejections=0,retained_code=s['retained_code'],
            reuse_blocked=s['reuse_blocked'],released_code=int(cpu in model.released),reclaimed=int(cpu in model.reclaimed),
            maintenance_intervals=model.pressure['intervals'][cpu],source_expected=s['source_expected'],source_reply=s['source_reply'],
            peer_expected=s['peer_expected'],peer_reply=s['peer_reply'],initial=snapshot(guest,dict(entries=[0]*4,generation=0),
                1,1,0,0,s['modes']['tr']),before_local=s['before_local'],after_local=s['after_local'],
            before_peer_reply=s['before_peer_reply'] or zero_snapshot,after_peer_reply=s['after_peer_reply'] or zero_snapshot,
            metadata_gpas=s['metadata_gpas'],code_before_retire=s['code_before_retire'],code_after_reclaim=s['code_after_reclaim'])
        if source_control=='positive' or 'cached_before_source' in o['owner']:
            owner.update(cached_before_source=s['cached_before_source'],source_entry_rejected=s['source_entry_rejected'])
        if maintenance:
            owner.update(maintenance_acks=s['maintenance_acks'],interval_visits=s['interval_visits'],semantic_admissions=s['semantic_admissions'])
        # Early retained development snapshots precede the code-retirement
        # fields. They have a separately selected scope, never full admission.
        for key,value in owner.items():
            if source_control=='none' and key in ('reuse_blocked','code_before_retire','code_after_reclaim') and key not in o['owner']:continue
            exact(o['owner'][key],value,f'CPU{cpu} owner.{key}')
        shared_before[cpu]=s['before_local'];shared_after[cpu]=s['after_local']
        recovery=dict(scenario=0,native_fpcr=initial['fpcr'],native_fpsr=initial['fpsr'],faults=0,walks=0,retries=0,
            vector=13 if family==3 else 0,error=0,linear=0,rip=s['visits'][-1]['rip'] if family==3 else 0,
            control=[0x1000,0,1,0,0],traces=[])
        exact(o['recovery'],recovery,'canonical local exception and independent native fault records')
        if maintenance:
            require(len(o['intervals'])==4,'all per-owner ordinary interval slots retained')
            for slot,out in enumerate(s['intervals']):
                wanted=copy.deepcopy(out)
                if out['faults']:
                    visit=s['interval_visits'][slot]-1
                    wanted['esr'],wanted['far'],wanted['pc'],wanted['spsr']=o['visits'][visit]['exception']
                exact(o['intervals'][slot],wanted,'real ordinary interval full frames, actual fault and complete memory effect')
            waits=uint(o['event']['wait_checks'],64,'compiler BUSY event checks')
            require(waits>0,'actual asynchronous compiler wait checks the pending event')
            exact(o['event'],dict(s['event'],wait_checks=waits),'exact canonical event packet and predicate history')
        if producer_directory is not None:
            from i4v_controls_worker import verify_compilations
            verify_compilations(Path(producer_directory)/f'cpu-{cpu}',s['packets'],s['replies'])
    mask=lambda owners:sum(1<<cpu for cpu in owners)
    shared=dict(n=n,profile=family,actor=actor,phase=3,ready=0,held=mask(model.held),local_done=1,
        drained=mask(model.drained),peer_compiling=mask(model.reply_held),second_commit=int(model.second==2),
        peer_executed=mask(model.reply_executed),final_ready=mask(model.final_ready),source_generation=model.source_generation,
        source_acknowledgements=mask(model.source_ack),finished=(1<<n)-1,before=shared_before,after=shared_after,
        pae_ready=list(map(mask,model.pae_ready)),pae_acknowledgements=list(map(mask,model.pae_ack)),pae_published=mask(model.pae_published))
    if maintenance:
        shared.update(semantic_active=0,maintenance_started=model.maintenance_started,maintenance_leaves=model.maintenance_leaves)
        exact(record['maintenance'],model.pressure,'complete FIFO request identity, bounded fairness and table-head history')
        exact(record['program_revision'],revision,'explicit concurrent-only source revision')
        all_bits=(1<<n)-1;changed=model.source_generation==2
        wanted_coord=dict(participants=n,scenario=5,alias_profile=0,stack_profile=0,mixed_profile=0,mixed_compiled=0,
            ready=all_bits,finished=0,phase=0,active=0,queued=0,head=0,length=0,servicing=NO_CPU,completed=0,
            retained=mask(model.retained),generation=model.source_generation,acknowledgements=mask(model.source_ack),
            new_entries=all_bits if changed else 0,new_completed=all_bits if changed else 0,new_references=0,
            reclaimed=int(changed),old_eligible=int(not changed),published=1,stale_replies=0,stale_entries=0,
            reuse_blocked=sum(s['reuse_blocked'] for s in model.owners),translation_wait=0,trace_length=model.events,
            queue=model.native_queue,event_pending=[2]*n+[0]*(4-n),event_notifications=[2]*n+[0]*(4-n))
        require(len(coord['trace'])==length<=768,'complete trace fits the declared bounded coordinator storage')
        exact(uint(coord['lock'],64,'released coordinator lock'),uint(coord['next_ticket'],64,'coordinator ticket tail'),
              'all acquired coordinator authority released')
        for key,value in wanted_coord.items():exact(coord[key],value,'coordinator '+key)
    for key,value in shared.items():
        if source_control=='none' and key=='pae_acknowledgements' and key not in record['concurrent']:continue
        exact(record['concurrent'][key],value,'shared '+key)
    return dict(scope=f'development architecture/source audit ({source_control}, program revision {revision}, maintenance {maintenance}); not full campaign acceptance',
        owners=n,visits=sum(len(s['visits']) for s in model.owners),completed=sum(s['completed'] for s in model.owners),
        native_entries=sum(v['entries'] for s in model.owners for v in s['visits']),native_faults=sum(s['faults'] for s in model.owners),
        compilations=sum(len(s['packets']) for s in model.owners),walks=sum(s['walks'] for s in model.owners),
        root_loads=sum(s['attempts'] for s in model.owners),root_reads=sum(s['reads'] for s in model.owners),
        lower_reads=sum(s['walk_reads'] for s in model.owners),lower_writes=sum(s['walk_writes'] for s in model.owners),
        metadata_reads=sum(s['modes']['reads'] for s in model.owners),trace_events=model.events,
        maintenance_heads=model.pressure['total'],ordinary_intervals=model.pressure['ordinary_total'],
        event_deliveries=sum(s['event']['deliveries'] for s in model.owners))

def check(record,expected_profile=None,expected_vcpus=None,expected_actor=None,expected_revision=1):
    """Admit only the complete current bounded concurrent-control contract."""
    require(type(record) is dict,'expected one concurrent-control report object')
    fields={'case','program_revision','profile','configured_vcpus','tested_vcpus','actor','vm_nonce',
        'concurrent','coordinator','maintenance','owners','source_before','source_after','data_before','data_after',
        'tables_before','tables_after','pae_tables_before','pae_tables_after','mode_tables_before','mode_tables_after'}
    exact(set(record),fields,'complete current report fields')
    exact(record['case'],'controls-concurrent','concurrent control report kind')
    profile=uint(record['profile'],32,'control family');require(profile in FAMILIES,'admitted control family')
    n=uint(record['configured_vcpus'],32,'configured participants');require(1<=n<=4,'bounded participant set')
    exact(uint(record['tested_vcpus'],32,'tested participants'),n,'all configured participants tested')
    actor=uint(record['actor'],32,'local actor');require(actor<n,'actor belongs to configured participant set')
    revision=uint(record['program_revision'],32,'program revision');exact(revision,1,'current complete program revision')
    require(uint(record['vm_nonce'],64,'VM incarnation')>0,'nonzero VM incarnation')
    exact(uint(expected_revision,32,'requested program revision'),1,'supported requested revision')
    for observed,wanted,label in ((profile,expected_profile,'family'),(n,expected_vcpus,'participant count'),
                                  (actor,expected_actor,'actor')):
        if wanted is not None:
            uint(wanted,32,'requested '+label);exact(observed,wanted,'requested '+label)
    for key in ('concurrent','coordinator','maintenance'):
        require(type(record[key]) is dict,key+' must be an object')
    require(type(record['owners']) is list and len(record['owners'])==n,'one ordered owner result per participant')
    try:
        require(type(record['coordinator']['trace']) is list,'ordered trace must be an array')
        for owner in record['owners']:
            require(type(owner) is dict,'owner result must be an object')
            for key in ('id','capture_pc','service_pc','sctlr','actlr'):uint(owner[key],64,'owner '+key)
            for key in ('owner','controls','pae','modes','event','recovery'):
                require(type(owner[key]) is dict,'owner '+key+' must be an object')
        result=audit_development(record,source_control='positive',revision=1,maintenance=True,peer_reply_control=True)
    except (KeyError,IndexError,TypeError) as error:
        raise ValueError(f'malformed concurrent-control report: {error}') from error
    result['scope']='complete bounded concurrent-control case; not whole-emulator validation'
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('reports',nargs='+',type=Path)
    parser.add_argument('--profile',type=int,choices=FAMILIES)
    parser.add_argument('--vcpus',type=int,choices=range(1,5))
    parser.add_argument('--actor',type=int,choices=range(4))
    parser.add_argument('--revision',type=int,choices=(1,),default=1)
    args=parser.parse_args()
    for path in args.reports:
        try:
            check(json.loads(path.read_text()),expected_profile=args.profile,
                  expected_vcpus=args.vcpus,expected_actor=args.actor,expected_revision=args.revision)
        except (OSError,ValueError,TypeError,KeyError) as error:
            raise SystemExit(f'{path}: FAIL: {error}') from error
        print(f'{path}: PASS (bounded concurrent control program)')


if __name__=='__main__':main()
