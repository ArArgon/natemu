"""Independent XCHG postcommit drain/remap and seeded-cut replay.

This proves the recorded finite executions, not universal memory ordering.
The original native frame/token-chain oracle remains strict for ordinary runs.
"""
import copy
import check_i4v_ordering as native
from i4v_atomic_schema import observations
from i4v_check import exact, require, uint, IPA
from check_i3_paging import guest_seed, copy_live

NONE=(1<<64)-1
NEW=0x103000
ADMIT,DETACH,ACK=(i<<32 for i in (1,2,3))
ATTR=3|(1<<10)|(3<<8)|(3<<53)


def guest(frame,rip=None):
    value=guest_seed('cold');copy_live(value,frame)
    if rip is not None:value['rip']=rip
    return value


def remapped_tables():
    result=native.initial_tables()
    result[12288+257*8:12288+258*8]=(NEW|0x67).to_bytes(8,'little')
    return result


def image(config,trial,returns,final,*,stores=True):
    result=native.reset_image(config['n'])+[0x5a]*4096
    for cpu in range(config['n']):result[8192+cpu*128:8196+cpu*128]=[0]*4
    result[:4]=final.to_bytes(4,'little')
    if stores:
        for cpu,value in returns.items():
            at=(8192 if trial else 4096)+cpu*128
            result[at:at+4]=value.to_bytes(4,'little')
    return bytes(result)


class Replay:
    def __init__(self,report,cfg):
        self.r,self.cfg=report,cfg
        self.n=cfg['n'];self.all=(1<<self.n)-1;self.writer=(cfg['actor']+1)%self.n
        self.phase=self.active=self.retained=self.parked=self.requested=self.drained=0
        self.mutated=self.acks=self.blocked=self.opened=self.delivered=self.finished=0
        self.queue=[];self.head=0;self.servicing=NONE
        self.posts=[0]*self.n;self.waits=[0]*self.n
        self.admits=[0]*self.n;self.returns=[0]*self.n
        self.admitted=[0]*32;self.returned=[0]*32
        self.selected=set();self.retried=set();self.completions=set()
        self.snapshots=[];self.follow=[]

    def step(self,word):
        event,cpu=divmod(uint(word,64,'trace'),256)
        require(cpu<self.n,'trace owner')
        bit=1<<cpu
        self.snapshots.append((self.phase,self.acks,self.opened))
        if self.follow:
            exact((event,cpu),self.follow.pop(0),'indivisible protocol trace sequence')
        if event==1:
            index=self.admits[cpu]
            require(index<32 and self.phase==0 and not self.active&bit and self.returns[cpu]==index,
                    'open unique program admission')
            if index:require(self.delivered&bit and self.opened,'new program after owner event boundary')
            self.active|=bit;self.retained|=bit
            self.follow=[(0x1000+index,cpu)]
        elif 0x1000<=event<0x1020:
            index=event-0x1000
            exact(index,self.admits[cpu],'monotonic trial admission')
            require(self.active&bit and not self.admitted[index]&bit,'actual admitted program')
            self.admitted[index]|=bit;self.admits[cpu]+=1
        elif event==2:
            require(self.admitted[0]==self.all and self.active&bit and not self.parked&bit and not self.requested,
                    'first trial result-store fault after all admissions')
            self.active&=~bit;self.parked|=bit;self.queue.append(cpu);self.phase=1
            self.follow=[(80,cpu),(80,cpu),(81,cpu)]
        elif event==80:
            require(self.parked&bit and not self.requested and self.posts[cpu]<2,'two notifications while retained')
            self.posts[cpu]+=1
        elif event==81:
            require(self.parked&bit and self.phase==1 and self.posts[cpu]==2 and self.waits[cpu]==0,
                    'one actual blocked event attempt')
            self.waits[cpu]=1
        elif event==82:
            require(cpu==self.writer and self.parked==self.retained==self.all and not self.active
                    and not self.requested and self.waits==[1]*self.n,'remap requested with all postcommit stores held')
            self.requested=1
        elif event==3:
            require(cpu==0 and self.requested and self.phase==1 and self.active==0,'exclusive drain after closure')
            self.phase=2
        elif event==4:
            require(self.phase==2 and self.servicing==NONE and self.head<len(self.queue),'one retained FIFO selection')
            exact(cpu,self.queue[self.head],'FIFO owner')
            self.servicing=cpu;self.selected.add(cpu)
        elif event==83:
            require(self.servicing==cpu and cpu not in self.retried and not self.mutated,'one old-route STLR retry')
            self.retried.add(cpu)
        elif event==5:
            require(self.servicing==cpu and cpu in self.retried and cpu not in self.completions,'one result-store completion')
            self.completions.add(cpu);self.head+=1;self.servicing=NONE
            self.follow=[(84,cpu)]
        elif event==84:
            require(cpu in self.completions and not self.drained&bit and self.retained&bit,'release completed store pin')
            self.drained|=bit;self.retained&=~bit;self.returned[0]|=bit;self.returns[cpu]=1
        elif event==85:
            require(cpu==self.writer and self.phase==2 and self.drained==self.all and self.retained==self.active==0
                    and self.head==self.n and self.servicing==NONE and not self.mutated,'mutation after every retained effect drains')
            self.mutated=1
        elif event==87:
            require(cpu==self.cfg['actor'] and self.mutated and self.phase==2
                    and self.acks==self.all^bit and not self.blocked,'actual rejected admission before last acknowledgement')
            self.blocked=1;self.follow=[(86,cpu)]
        elif event==86:
            require(self.mutated and self.phase==2 and not self.acks&bit,'one covering local repair acknowledgement')
            if cpu==self.cfg['actor']:require(self.blocked and self.acks==self.all^bit,'delayed owner acknowledges last')
            self.acks|=bit
        elif event==88:
            require(cpu==self.writer and self.phase==2 and self.acks==self.all and self.blocked and not self.opened,
                    'reopen only after all repairs')
            self.phase=0;self.opened=1
        elif event==89:
            require(self.opened and self.phase==0 and self.acks==self.all and self.drained&bit and not self.active&bit
                    and not self.delivered&bit and self.posts[cpu]==2 and self.waits[cpu]==1,
                    'coalesced event at complete canonical boundary without active owner')
            self.delivered|=bit
        elif 0x2001<=event<0x2020:
            index=event-0x2000
            require(self.opened and self.active&bit and self.retained&bit and self.admitted[index]==self.all
                    and self.returned[index-1]==self.all and self.returns[cpu]==index,
                    'whole later program returns after trial/reset barrier')
            self.active&=~bit;self.retained&=~bit;self.returned[index]|=bit;self.returns[cpu]+=1
        elif event==90:
            require(self.returns[cpu]==32 and not self.active&bit and not self.retained&bit and not self.finished&bit,
                    'owner stops after all programs')
            self.finished|=bit
        else:raise ValueError('unknown atomic trace event')

    def finish(self):
        require(not self.follow,'complete final protocol operation')
        exact((self.finished,self.delivered,self.acks,self.drained),(self.all,)*4,'all owners complete every phase')
        exact((self.active,self.retained,self.phase),(0,0,0),'authority drains')
        exact(self.admitted,[self.all]*32,'all real trial entries')
        exact(self.returned,[self.all]*32,'all real trial completions')


def check_native(report,cfg):
    exact(cfg['family'],3,'atomic scheduling family')
    native.bind_requested(report,9,cfg['n'],cfg['actor'],cfg['token_seed'])
    evidence=observations(report)
    summary=native.audit_development(report,atomic_mode=True)
    exact(summary['native_faults'],cfg['n']+31,'all first faults and later actor faults')
    a=report['atomic'];n=cfg['n'];mask=(1<<n)-1
    trace=a['coordinator']['trace'];model=Replay(report,cfg)
    for word in trace:model.step(word)
    model.finish()
    config=native.configuration(9,n,cfg['actor'],cfg['token_seed'])
    returns={cpu:report['owners'][cpu]['trials'][0]['captured']['x'][4] for cpu in range(n)}
    final=int.from_bytes(evidence['trials'][0][:4],'little')
    exact(evidence['cuts'],[image(config,0,returns,final,stores=False),image(config,0,returns,final),image(config,0,returns,final)],
          'coherent images before drain, after old effects, and after mapping-only mutation')
    for name,want in dict(magic=0x41544f4d50520001,n=n,actor=cfg['actor'],writer=model.writer,parked=mask,
                          requested=1,drained=mask,mutated=1,acknowledged=mask,blocked=1,opened=1,
                          delivered=mask,snapshot_count=3).items():exact(a[name],want,'atomic '+name)
    exact(a['tables_before'],native.initial_tables(),'actual table bytes before mutation')
    exact(a['tables_after'],remapped_tables(),'only result leaf changes')
    for cpu,owner in enumerate(a['owners']):
        active=cpu<n
        frame=native.owner_initial(config,cpu) if active else native.zero_frame()
        before=guest(frame,native.RIP+cpu*256)
        completed=copy.deepcopy(before)
        expected=dict(saved_gpa=0,saved_leaf=0,fault_pc=0,resume_pc=0,queued=0,resumed=0,completed=0,
                      ack_leaf=0,new_leaf=0,fault_frame=native.zero_frame(),before_guest=before,completed_guest=completed,
                      event=dict(consumed=0,deliveries=0,boundary=0,blocked=0,wait_checks=0,shadow=0,priority=0,packet=guest(frame)),
                      walk=dict(address=0,gpa=0,leaf_bytes=0,writable=0,dirty=0,read_count=0,writes=0,reads=[[0,0] for _ in range(4)]))
        if active:
            fault=report['owners'][cpu]['trials'][0]['fault'][0]
            copy_live(completed,fault['frame']);completed['rip']+=4
            expected.update(saved_gpa=native.RESULTS+cpu*128,saved_leaf=(IPA+0x95000)|ATTR,
                            fault_pc=native.CODE+16,resume_pc=native.CODE+16,queued=1,resumed=1,completed=1,
                            new_leaf=(IPA+0x97000)|ATTR,fault_frame=fault['frame'])
            expected['event']=dict(consumed=1,deliveries=1,boundary=4,blocked=34,wait_checks=1,shadow=0,priority=0,packet=completed)
            expected['walk']=dict(address=native.RESULTS+cpu*128,gpa=NEW+cpu*128,leaf_bytes=4096,writable=1,dirty=1,
                                  read_count=4,writes=0,reads=[[0x1000,0x2027],[0x2000,0x3027],[0x3000,0x4027],[0x4808,0x103067]])
        exact(owner,expected,'complete retained owner state '+str(cpu))
    coord=dict.fromkeys(('scenario alias_profile stack_profile mixed_profile mixed_compiled ready phase active queued head length completed retained new_entries new_completed new_references reclaimed stale_replies stale_entries reuse_blocked translation_wait').split(),0)
    lock=uint(a['coordinator']['lock'],64,'final ticket lock');require(lock>0,'coordinator used')
    coord.update(participants=n,finished=mask,lock=lock,next_ticket=lock,servicing=NONE,generation=2,
                 acknowledgements=mask,old_eligible=1,published=1,queue=[NONE]*4,
                 event_pending=[2]*n+[0]*(4-n),event_notifications=[2]*n+[0]*(4-n),trace_length=len(trace),trace=trace)
    exact(a['coordinator'],coord,'complete final authority and event state')
    return model,summary


def atomic(schedule,report,cfg):
    from check_i4v_schedule import decisions
    model,summary=check_native(report,cfg)
    trace=report['atomic']['coordinator']['trace'];mask=model.all
    def contract(key,at,done):
        phase,acks,opened=model.snapshots[at]
        kind,index=key>>32,key&0xffffffff
        eligible=mask
        if kind==1:
            require(index<32,'bounded admission cut');epoch,action=index+1,1
        elif kind==2:
            exact(index,0,'single first-trial detach cut');epoch,action=1,2
        elif kind==3:
            exact(index,0,'single mutation acknowledgement cut');epoch=2
            early=mask^(1<<cfg['actor'])
            eligible=mask if acks&early==early else early
            action=87 if acks==early else 86
        else:raise ValueError('unexpected atomic scheduling cut')
        return mask,eligible,phase,epoch,action
    count=decisions(schedule,cfg,trace,contract,{ADMIT|i for i in range(32)}|{DETACH,ACK})
    for d in schedule['decisions']:
        if d['cut']>>32==1:
            at=d['claim_trace_before']
            require(at+1<len(trace),'actual admission marker')
            exact(trace[at+1],((0x1000+(d['cut']&0xffffffff))<<8)|d['chosen'],'decision belongs to actual trial')
    exact(schedule['pool_events'],[],'atomic retained cohort has no reduced maintenance pool')
    for name in ('full_count','readmitted'):exact(schedule['summary'][name],0,name)
    for name in ('full','retried'):exact(schedule['summary'][name],[0]*4,name)
    return dict(summary,decisions=count,retained_stores=cfg['n'],remaps=1,later_remapped_trials=31)
