"""Independent two-byte semantic-helper effects and retained-route replay."""
import copy
from check_i1 import state, resident_pc
from check_i4v_ordering import native_seed, numeric_transport
from check_i3_paging import guest_seed, copy_live
from i4v_check import exact, require, uint, IPA, TCR
from check_i4v_coherence import validate_walk
from i4v_coherence_model import Context

DATA, RIP, DEVICE, NONE = 0x100000, 0x102000, 0x500000, (1 << 64)-1


def guest(cpu):
    frame=native_seed(cpu)
    result=guest_seed('cold');copy_live(result,frame)
    result['gpr'][1]=2;result['gpr'][6]=DEVICE+cpu*256;result['gpr'][7]=DATA+cpu*256
    result['rip']=RIP+cpu*256
    return result


def advanced(before,count):
    result=copy.deepcopy(before)
    result['gpr'][1]-=count;result['gpr'][6]+=count;result['gpr'][7]+=count
    if count==2:result['rip']+=2
    return result


def table_image(tables):
    image=bytearray(16384)
    for at,value in tables.items():image[at-0x1000:at-0x1000+8]=value.to_bytes(8,'little')
    return list(image)


class Replay:
    def __init__(self,report,cfg):
        self.r,self.cfg,self.n=report,cfg,cfg['n'];self.all=(1<<self.n)-1
        self.tables={0x1000:0x2027,0x2000:0x3027,0x3000:0x4027,0x4800:DATA|0x67,0x4810:RIP|0x27}
        self.data=[0xa5]*4096+[0x5a]*4096
        self.source=[0xcc]*4096
        for cpu in range(self.n):self.source[cpu*256:cpu*256+2]=[0xf3,0xa4]
        self.phase=self.active=self.retained=self.queued=self.done=self.ready=self.released=0
        self.acks=self.probes=self.delivered=self.finished=0
        self.mutation_blocked=self.mutated=self.admission_blocked=self.opened=0
        self.queue=[];self.head=0;self.servicing=NONE
        self.walks=[0]*self.n;self.samples=[0]*self.n;self.retries=[0]*self.n
        self.held=set();self.helper_done=set();self.posts=[0]*self.n;self.blocked=[0]*self.n
        self.images=[];self.snapshots=[]

    def fault(self,cpu,probe):
        f=self.r['owners'][cpu]['fault'][int(probe)]
        exact(set(f),set('frame esr far pc spsr resume_pc value gpa'.split()),'complete fault record')
        frame=state(f['frame'],'helper fault frame')
        exact(set(f['frame']),set('x q sp nzcv fpcr fpsr'.split()),'complete captured native roots')
        for key in set(f)-{'frame'}:uint(f[key],64,'fault '+key)
        value=0xd0+cpu if probe else 0x6e+cpu*0x20
        far=DATA+cpu*256+(2 if probe else 1)
        exact((f['far'],f['gpa'],f['value']),(far,far+(4096 if probe else 0),value),'saved fault route and sampled value')
        exact((frame['x'][0],frame['x'][1]),(far,value),'actual STRB operands')
        require(resident_pc(f['pc']) and f['resume_pc']==f['pc'],'retry only original resident store')
        require(f['esr']>>26==0x25 and f['esr']&(1<<6)
                and not f['esr']&((1<<7)|(1<<8)|(1<<10)),'direct same-EL write abort')
        require(f['esr']&63 in (range(12,16) if probe else range(4,8)),'expected absent/read-only shadow fault')
        require(f['spsr']&15==4 and frame['sp']%16==0,'EL1t full-frame recovery')
        exact(frame['nzcv'],f['spsr']&0xf0000000,'saved native flags')
        require(0x800040050000<=frame['sp']<=0x800040054000,'helper continuation within owned native stack')

    def step(self,word):
        event,cpu=divmod(uint(word,64,'trace word'),256);require(cpu<self.n,'configured owner')
        bit=1<<cpu
        self.snapshots.append((self.phase,self.acks,self.opened))
        if 0x1000<=event<0x1005:
            slot=event-0x1000;exact(slot,self.walks[cpu],'ordered helper walk')
            address=(RIP+cpu*256,DATA+cpu*256,DATA+cpu*256+1,DATA+cpu*256,DATA+cpu*256+2)[slot]
            intent=('fetch','write','write','read','write')[slot]
            if slot==2:
                exact(self.samples[cpu],1,'first element completes before second source sample')
                self.data[cpu*256]=0x57+cpu*0x20
            if slot>=3:require(self.mutated==1,'new-route walk after mutation')
            if slot==4:require(self.opened and self.active&bit,'probe fault retains its new admission')
            answer=validate_walk(self.r['owners'][cpu]['walks'][slot],self.tables,
                                 Context('four',0x1000,nxe=True),address,intent)
            require(answer.route is not None,'every declared helper walk succeeds')
            self.tables=answer.tables;self.walks[cpu]+=1
        elif event==1:
            require(self.phase==0 and not self.active&bit,'open unique admission')
            require(self.walks[cpu]==(4 if self.opened else 2),'source/route acquired before entry')
            self.active|=bit;self.retained|=bit
        elif event==155:
            require(self.active&bit and not self.opened and self.posts[cpu]<2,'actual coalesced notification')
            self.posts[cpu]+=1
        elif event==140:
            require(self.active&bit and not self.ready&bit and self.posts[cpu]==2,'helper ready after admission')
            self.ready|=bit
        elif event==141:
            require(self.ready==self.all and self.samples[cpu]<2,'two device samples after all admissions')
            exact(self.walks[cpu],2+self.samples[cpu],'second route held before second sample')
            self.samples[cpu]+=1
        elif event==2:
            require(self.active&bit and not self.queued&bit and self.samples[cpu]==2,'fault detaches sampled second store')
            self.fault(cpu,False);self.active&=~bit;self.queued|=bit;self.queue.append(cpu);self.phase=1
        elif event==142:
            require(self.queued&bit and cpu not in self.held,'retained fault marker')
            self.held.add(cpu)
        elif event==152:
            require(cpu in self.held and not self.done&bit and self.phase==1 and self.posts[cpu]==2,'event blocked during partial helper')
            self.blocked[cpu]+=1
        elif event==143:
            require(cpu==self.cfg['actor'] and self.queued==self.retained==self.all and not self.mutation_blocked,'real mutation rejected with held routes')
            self.mutation_blocked=1;self.images.append(bytes(self.data).hex())
        elif event==3:
            require(cpu==0 and self.active==0 and self.queued==self.all and self.mutation_blocked and self.phase==1,'exclusive FIFO drain')
            self.phase=2
        elif event==4:
            require(self.phase==2 and self.servicing==NONE and self.head<len(self.queue),'one FIFO service')
            exact(cpu,self.queue[self.head],'saved-route FIFO');self.servicing=cpu
        elif event==144:
            if self.opened:
                require(self.active&bit and self.walks[cpu]==5 and self.retries[cpu]==1,'single new-route write repair')
                self.fault(cpu,True)
            else:require(self.servicing==cpu and self.retries[cpu]==0,'single retained-store retry')
            self.retries[cpu]+=1
        elif event==5:
            require(self.servicing==cpu and self.retries[cpu]==1 and not self.done&bit,'one exact second-store commit')
            self.data[cpu*256+1]=0x6e+cpu*0x20
            self.done|=bit;self.head+=1;self.servicing=NONE
        elif event==145:
            require(self.done&bit and cpu not in self.helper_done,'canonical helper completion')
            self.helper_done.add(cpu)
        elif event==146:
            require(cpu in self.helper_done and self.retained&bit and not self.released&bit,'release completed helper route')
            self.retained&=~bit;self.released|=bit
        elif event==147:
            require(cpu==self.cfg['actor'] and self.done==self.released==self.all
                    and self.active==self.retained==0 and self.servicing==NONE and not self.mutated,'remap after exact old effects drain')
            self.images.append(bytes(self.data).hex());self.tables[0x4800]=(DATA+4096)|7;self.mutated=1
        elif event==148:
            delayed=(self.cfg['actor']+self.n-1)%self.n
            require(cpu==delayed and self.phase==2 and self.acks==self.all^bit and not self.admission_blocked,'admission rejected before final acknowledgement')
            self.admission_blocked=1
        elif event==149:
            require(self.mutated and self.walks[cpu]==4 and not self.acks&bit,'covering acknowledgement')
            if cpu==(self.cfg['actor']+self.n-1)%self.n:require(self.admission_blocked==1,'last acknowledgement delayed')
            self.acks|=bit
        elif event==150:
            require(cpu==self.cfg['actor'] and self.acks==self.all and not self.opened,'reopen after all invalidations')
            self.opened=1;self.phase=0;self.queued=0;self.head=0;self.done=0
        elif event==151:
            require(self.opened and self.active&bit and self.retries[cpu]==2 and not self.probes&bit,'exact new-route store')
            self.data[4096+cpu*256+2]=0xd0+cpu;self.probes|=bit;self.active&=~bit;self.retained&=~bit
        elif event==153:
            require(self.probes==self.all and self.active==self.retained==0 and not self.delivered&bit,'canonical event after helpers and probes')
            self.delivered|=bit
        elif event==154:
            require(self.delivered&bit and not self.finished&bit,'one owner exit after handoff');self.finished|=bit
        else:raise ValueError(f'unknown partial-helper trace event {event}')


def check(report,cfg):
    numeric_transport(report)
    exact(set(report),set('case n ordinal actor vm_nonce owners common images coordinator data_before data_after source_before source_after tables_before tables_after'.split()),'partial report fields')
    exact((report['case'],report['n'],report['ordinal'],report['actor']),('partial-progress',cfg['n'],cfg['seed_ordinal'],cfg['actor']),'requested partial cell')
    require(uint(report['vm_nonce'],64,'VM identity')>0,'fresh nonzero VM identity')
    require(len(report['owners'])==cfg['n'],'one result per owner')
    model=Replay(report,cfg)
    exact(report['tables_before'],table_image(model.tables),'initial tables')
    exact(report['data_before'],model.data,'initial physical data')
    exact(report['source_before'],model.source,'initial source')
    trace=report['coordinator']['trace'];require(0<len(trace)<=768,'bounded trace')
    for word in trace:model.step(word)
    require(model.finished==model.delivered==model.probes==model.all and model.active==model.retained==0,'all helpers terminate after precise effects')
    model.images.append(bytes(model.data).hex())
    exact(report['images'],model.images,'held/committed/remapped complete physical images')
    exact(report['data_after'],model.data,'final exact physical effects')
    exact(report['tables_after'],table_image(model.tables),'final table effects')
    exact(report['source_after'],model.source,'source remains unchanged')
    for cpu,o in enumerate(report['owners']):
        frame=native_seed(cpu);bank=[((i*37+19)&255)^cpu for i in range(512)]
        before=guest(cpu);held=advanced(before,1);after=advanced(before,2)
        expected=dict(id=cpu,phase=3,reads=2,writes=2,read_values=[0x57+cpu*32,0x6e+cpu*32],
            sampled=0x6e+cpu*32,subaccess=1,source_route=DEVICE+cpu*256+1,
            destination_route=DATA+cpu*256+1,destination_writable=1,retained=0,released=1,
            faults=2,retries=2,probe_writes=1,probe_route=DATA+4096+cpu*256+2,
            notifications=2,pending=0,event_attempts=model.blocked[cpu]+1,event_blocked=model.blocked[cpu],delivered=1,
            delivery_state=[after['rip'],0,after['gpr'][6],after['gpr'][7],after['rflags']],
            walk_count=5,status=1,detail=0,current_el=4,tcr=TCR,initial=frame,
            before=before,held=held,resumed=held,after=after,bank_before=bank,bank_after=bank)
        exact(set(o),set(expected)|set('walks fault captured actlr sctlr native_leaf store_pc store_bytes'.split()),'complete helper owner fields')
        exact(model.blocked[cpu],1,'one actual blocked event attempt per helper')
        require(len(o['walks'])==5 and len(o['fault'])==2,'complete walk/fault records')
        for key,value in expected.items():exact(o[key],value,'owner '+key)
        require(uint(o['actlr'],64,'ACTLR')&2 and uint(o['sctlr'],64,'SCTLR')&0x1005==0x1005,'native memory profile')
        exact(o['captured'],o['fault'][1]['frame'],'last captured fault frame')
        exact(o['fault'][0]['pc'],o['fault'][1]['pc'],'same actual helper store instruction')
        exact(o['store_pc'],o['fault'][0]['pc'],'recorded helper instruction address')
        exact(o['store_bytes'],list(bytes.fromhex('01000039c0035fd6')),'observed STRB W1,[X0]; RET')
        leaves=[0]*512;leaves[256]=(IPA+0x95000)|3|(1<<10)|(3<<8)|(3<<53)
        exact(o['native_leaf'],leaves,'fresh writable shadow mapping')
    common=dict(n=cfg['n'],actor=cfg['actor'],ordinal=cfg['seed_ordinal'],
        ready=model.all,mutation_blocked=1,helper_done=model.all,released=model.all,
        mutated=1,acknowledgements=model.all,admission_blocked=1,opened=1,
        probes_done=model.all,delivered=model.all,finished=model.all,images=7,
        source_generation=1,destination_generation=2,pending_events=0)
    exact(report['common'],common,'complete shared helper state')
    coord=dict.fromkeys(('scenario alias_profile stack_profile mixed_profile mixed_compiled ready phase active queued head length completed retained new_entries new_completed new_references reclaimed stale_replies stale_entries reuse_blocked translation_wait').split(),0)
    lock=uint(report['coordinator']['lock'],64,'final ticket lock')
    require(lock>0,'coordinator actually used')
    coord.update(participants=cfg['n'],finished=model.all,lock=lock,next_ticket=lock,
        servicing=NONE,generation=1,acknowledgements=model.all,old_eligible=1,published=1,
        queue=model.queue+[NONE]*(4-cfg['n']),event_pending=[0]*4,event_notifications=[0]*4,
        trace_length=len(trace),trace=trace)
    exact(report['coordinator'],coord,'all owner references and authority drain')
    return model
