#include "q34.h"
#ifdef Q30_FIXTURE_CHECK
extern u64 q32_fixture_read(const char *name);
extern void q32_fixture_write(const char *name,u64 value);
extern void q32_fixture_poll(void);
extern struct shared *q31_fixture_shared;
#define R(reg) q32_fixture_read(#reg)
#define W(reg,x) q32_fixture_write(#reg,(u64)(x))
#define NATIVE(text) q32_fixture_poll()
#define S q31_fixture_shared
#else
#define R(reg) ({u64 x;__asm__ volatile("mrs %0," #reg:"=r"(x));x;})
#define W(reg,x) __asm__ volatile("msr " #reg ",%0"::"r"((u64)(x)):"memory")
#define NATIVE(text) __asm__ volatile(text:::"memory")
#define S ((volatile struct shared *)SHARED_PA)
#endif
#define LD(p) __atomic_load_n((p),__ATOMIC_ACQUIRE)
#define ST(p,x) __atomic_store_n((p),(x),__ATOMIC_RELEASE)
#define E(type,...) QLOG(QE_Q32,(type),__VA_ARGS__)
enum { READ_DATA=1,WRITE_DATA=2,REPAIR=6,FETCH=7,REP_READ=8,REP_WRITE=9 };
static unsigned handler_sti[MAX_CPUS]; // Immutable per-fixture handler recipe.
extern u64 q32_hold(u64 *ready,u64 *ack,u64 wanted,u64 deadline);
extern u64 q32_wait(u64 *ready,u64 *ack,u64 wanted,u64 deadline);
extern char q32_hold_begin[],q32_hold_end[];
extern char q32_wait_begin[],q32_wait_end[];
extern char q32_wait_after[];
static u64 id(void){return R(tpidr_el1);}
static struct q32_cpu *cpu(void){return &Q32->cpu[id()];}
static struct q31_arch *arch(void){return &Q31->cpu[id()].arch;}
static u64 base(void){return q31_base();}
static u64 irq_lock(void){u64 saved=R(daif);W(daif,saved|128);return saved;}
static void irq_unlock(u64 saved){W(daif,saved);}
static void check(int ok,u64 reason){if(!ok)Q32->failure=reason;q30_check(ok,0x3200+reason);}
static u64 bit(u64 *map,u64 vector){return (map[vector/64]>>(vector%64))&1;}
static void set(u64 *map,u64 vector){map[vector/64]|=1UL<<(vector%64);}
static void clear(u64 *map,u64 vector){map[vector/64]&=~(1UL<<(vector%64));}
static u64 highest(u64 *map){for(u64 v=255;v>=32;v--)if(bit(map,v))return v;return 0;}
static u64 eligible(void){
    struct q32_cpu *c=cpu();
    if(!(arch()->flags&512)||c->shadow)return 0;
    u64 vector=highest(c->irr),service=highest(c->isr),priority=c->tpr&0xf0;
    if((service&0xf0)>priority)priority=service&0xf0;
    return vector&&(vector&0xf0)>priority?vector:0;
}
static u64 read_ok(u64 address,unsigned action){
    struct q31_access_result r=q31_memory(address,Q_LOAD,8,0,action);check(!r.status,1);return r.value;
}
static void write_ok(u64 address,u64 value,unsigned action){
    struct q31_access_result r=q31_memory(address,Q_STORE,8,value,action);check(!r.status,2);
}
static void pending(u64 vector){
    u64 saved=irq_lock();check(vector>=32&&vector<=255,3);u64 old=bit(cpu()->irr,vector);set(cpu()->irr,vector);
    // CONTROL1 is an explicit software APIC input, never native-route evidence.
    E(Q32_CONTROL,1,vector,old,cpu()->boundaries,0,0,0);
    irq_unlock(saved);
}
static void emit(u64 source,u64 target,u64 vector,u64 route){
    struct q32_cpu *t=&Q32->cpu[target];u64 sender=id();
    check(source>=1&&source<=3&&target<Q32->ncpus,4);
    t->vectors[source-1][sender]=vector;
    u64 sequence=LD(&t->emitted[source-1][sender])+1;
    E(Q32_EMIT,source,sender,target,sequence,vector,route,0);
    ST(&t->emitted[source-1][sender],sequence);
    if(source==Q32_PEER){
        u64 word=((u64)Q32_SGI<<24)|(route?1UL<<40:1UL<<target);
        W(icc_sgi1r_el1,word);
        NATIVE("isb");
        E(Q32_CONTROL,13,sender,target,sequence,word,route,0);
        ST(&t->sgi_sent[sender],sequence);
    }else if(source==Q32_TIMER){
        W(cntp_tval_el0,S->counter_hz/1000);W(cntp_ctl_el0,1);
        NATIVE("isb");
    }else ST(&t->spi_request,sequence);
}
int q32_irq(u64 intid,u64 elr,u64 spsr){
    if(LD(&Q32->phase)!=1&&LD(&Q33->phase)!=1&&LD(&Q34->phase)!=1)return 0;
    u64 source=intid==S->timer_intid?Q32_TIMER:intid==Q32_SGI?Q32_PEER:
        intid==Q32->spi_base+id()?Q32_HOST:0;
    if(!source)return 0;
    struct q32_cpu *c=cpu();int work=0;c->raw_irq[source-1]++;
    for(u64 sender=0;sender<Q32->ncpus;sender++)
        work|=LD(&c->emitted[source-1][sender])>c->accepted[source-1][sender];
    if(!work){
        c->empty_irq[source-1]++;
        if(source!=Q32_TIMER){
            check(c->empty_irq[source-1]<=4096,54);
            E(Q32_CONTROL,8,source,intid,c->raw_irq[source-1],c->empty_irq[source-1],c->stage,c->spi_deasserted);
        }
        return 1;
    }
    u64 number=++c->native_count;
    c->last_elr=elr;c->last_spsr=spsr;c->last_stage=c->stage;c->wake_generation++;
    if((elr>=(u64)q32_hold_begin&&elr<(u64)q32_hold_end)||
       (elr>=(u64)q32_wait_begin&&elr<(u64)q32_wait_end))c->interior_count++;
    E(Q32_NATIVE,source,intid,elr,spsr,c->stage,number,id());
    for(u64 sender=0;sender<Q32->ncpus;sender++){
        u64 previous=c->accepted[source-1][sender],next=LD(&c->emitted[source-1][sender]);
        if(next==previous)continue;
        u64 vector=c->vectors[source-1][sender],old=bit(c->irr,vector);
        check(vector>=32&&vector<=255,5);set(c->irr,vector);c->accepted[source-1][sender]=next;
        E(Q32_ACCEPT,source,sender,previous,next,vector,old,number);
    }
    if(source==Q32_HOST)ST(&c->spi_deassert,c->spi_request);
    NATIVE("sev"); // Closes IRQ-before-WFE lost-wake window.
    return 1;
}
static void receive(u64 source,u64 stage,u64 vector){
    struct q32_cpu *c=cpu();u64 before=c->native_count;
    u64 daif=irq_lock();c->stage=stage;ST(&c->ready,0);
    if(source!=Q32_PEER)emit(source,id(),vector,0);
    ST(&c->hook_target,1); // No more Q30 gate changes before native hold.
    u64 deadline=R(cntvct_el0)+S->counter_hz;
    u64 ok=stage==Q32_HALT_WAIT?q32_wait(&c->ready,&c->native_count,before+1,deadline):
        q32_hold(&c->ready,&c->native_count,before+1,deadline);
    W(daif,daif);ST(&c->ready,0);
    check(ok&&c->native_count==before+1&&c->last_stage==stage,6);
    u64 lo=stage==Q32_HALT_WAIT?(u64)q32_wait_begin:(u64)q32_hold_begin;
    u64 hi=stage==Q32_HALT_WAIT?(u64)q32_wait_end:(u64)q32_hold_end;
    check(c->last_elr>=lo&&c->last_elr<hi,7);
    c->stage=0;ST(&c->hook_target,0);
}
void q32_hook(u64 stage){
    if((LD(&Q32->phase)!=1&&LD(&Q33->phase)!=1&&LD(&Q34->phase)!=1)||cpu()->hook_stage!=stage)return;
    struct q32_cpu *c=cpu();c->hook_stage=0;
    receive(c->hook_source,stage,c->hook_vector);
}
static void eoi(void){
    struct q32_cpu *c=cpu();u64 vector=highest(c->isr);check(vector,8);
    arch()->rip=base()+0x3460;arch()->gpr[QR_RDI]=base()+0xc0f0;arch()->gpr[QR_RAX]=0;
    q31_begin(QI_EOI,8,3);write_ok(arch()->gpr[QR_RDI],0,WRITE_DATA);
    u64 saved=irq_lock();clear(c->isr,vector);
    q31_retire();E(Q32_EOI,vector,highest(c->isr),c->tpr,c->boundaries,0,0,0);irq_unlock(saved);
}
static u64 dispatch_kind(int partial_progress){
    struct q32_cpu *c=cpu();u64 saved=irq_lock(),vector=eligible();
    E(Q32_BOUNDARY,++c->boundaries,arch()->rip,arch()->flags,c->shadow,c->tpr,vector,c->halted);
    q31_state_snapshot(QS_PROGRESS);if(!vector){irq_unlock(saved);return 0;}
    clear(c->irr,vector);set(c->isr,vector);c->halted=0;c->shadow=0;
    irq_unlock(saved);
    if(partial_progress){
        check(Q31->cpu[id()].metadata.instruction==QI_REP&&arch()->gpr[QR_RCX]>0,68);
        E(Q32_CONTROL,12,1,arch()->rip,arch()->gpr[QR_RCX],arch()->gpr[QR_RSI],arch()->gpr[QR_RDI],c->boundaries);
    }
    u64 result=q31_interrupt(vector,partial_progress);
    E(Q32_DISPATCH,c->boundaries,vector,arch()->gpr[QR_RSP],Q31->cpu[id()].depth,result<32,c->native_count,Q31->cpu[id()].metadata.generation);
    if(result!=vector)return result;
    c->guest_count++;
    u64 slot=base()+0xc200+Q31->cpu[id()].depth*32;
    write_ok(slot,arch()->gpr[QR_RAX],REPAIR);write_ok(slot+8,arch()->gpr[QR_RDI],REPAIR);
    arch()->gpr[QR_RDI]=base()+0xc100;arch()->gpr[QR_RAX]=read_ok(base()+0xc100,REPAIR)+1;
    q31_begin(QI_STORE,8,3);write_ok(arch()->gpr[QR_RDI],arch()->gpr[QR_RAX],WRITE_DATA);q31_retire();
    c->handler_effects++;return vector;
}
static u64 dispatch(void){return dispatch_kind(0);}
static void step(u64 instruction);
static void finish(u64 vector){
    u64 slot=base()+0xc200+Q31->cpu[id()].depth*32;
    // The immutable fixture recipe names the optional contiguous STI/NOP;
    // the short branch bytes are fetched and checked before the EOI operation.
    if(handler_sti[id()]&&arch()->rip==base()+0x3403){step(QI_STI);step(QI_NOP);}
    q31_begin(QI_JMP,1,2);
    struct q31_access_result jump=q31_memory(arch()->rip,Q_FETCH8,1,0,FETCH);
    struct q31_access_result offset=q31_memory(arch()->rip+1,Q_FETCH8,1,0,FETCH);
    check(!jump.status&&!offset.status&&jump.value==0xeb&&offset.value<128,70);
    u64 target=arch()->rip+2+offset.value;check(target==base()+0x3460,71);q31_retire_at(target);
    eoi();arch()->gpr[QR_RAX]=read_ok(slot,REPAIR);arch()->gpr[QR_RDI]=read_ok(slot+8,REPAIR);
    q31_state_snapshot(QS_PROGRESS);
    q31_return(vector,0);
}
static void service(void){u64 v;while((v=dispatch())){check(v>=32,9);finish(v);}}
static void step(u64 instruction){
    struct q32_cpu *c=cpu();u64 oldif=!!(arch()->flags&512),oldshadow=c->shadow;
    if(instruction==QI_MOVSS)arch()->gpr[QR_RAX]=0x10;
    q31_begin(instruction,1,instruction==QI_MOVSS?2:1);
    if((instruction==QI_CLI||instruction==QI_STI||instruction==QI_HLT)&&(arch()->cs&3)){
        check(q31_raise(13,0,0)==13,10);c->shadow=0;q31_return(13,1);
        check(!!(arch()->flags&512)==oldif,11);return;
    }
    if(instruction==QI_CLI)arch()->flags&=~512UL;
    if(instruction==QI_STI)arch()->flags|=512;
    if(instruction==QI_MOVSS){
        u64 descriptor=read_ok(base()+0x10010,3);check(descriptor==0x00cf93000000ffffUL,12);arch()->ss=0x10;
    }
    c->shadow=instruction==QI_MOVSS||(instruction==QI_STI&&!oldif)?1:0;
    q31_retire();if(instruction==QI_HLT)c->halted=1;
    E(Q32_SHADOW,instruction,oldif,!!(arch()->flags&512),oldshadow,c->shadow,c->boundaries,0);
}
static void q32_fixture(u64 serial,unsigned group,unsigned variant){
    q31_reuse_fixture(serial);
    handler_sti[id()]=group==5&&variant==1;
    struct q32_cpu *c=cpu();check(!highest(c->irr)&&!highest(c->isr)&&!Q31->cpu[id()].depth,13);
    c->shadow=c->tpr=c->halted=c->stage=c->ready=c->hook_stage=c->hook_target=0;
    const u64 vectors[]={0x41,0x4f,0x71};
    for(unsigned i=0;i<3;i++)q31_set_gate(vectors[i],base()+0x3400,8,0,14);
    write_ok(base()+0x3400,group==5&&variant==1?0x59eb90fb078948UL:0x5beb078948UL,REPAIR);
    write_ok(base()+0x3460,0x078948,REPAIR);
    write_ok(base()+0x3500,0xfa,REPAIR);write_ok(base()+0x3510,0xfb,REPAIR);
    write_ok(base()+0x3520,0xd08e,REPAIR);write_ok(base()+0x3530,0xf4,REPAIR);
    write_ok(base()+0x3540,0x90,REPAIR);write_ok(base()+0xc100,0,REPAIR);
    write_ok(base()+0x3550,0x07c10f48,REPAIR);write_ok(base()+0x3560,0xc48948,REPAIR);
    u64 program=0x90;
    if(group==3)program=variant==0?0x90fbfa:variant==1?0xfa:0xfb;
    if(group==4){const u64 programs[]={0x90fb,0xfb,0x90fbfafb,0xf4fb,0xc48948d08eUL,0x078b48fb};program=programs[variant];}
    if(group==7)program=variant==3?0xf4fa:variant==2?0xfdebf4:0xf4;
    write_ok(base()+0x3500,program,REPAIR);arch()->rip=base()+0x3500;
}
static void current_fault_from(int already_started){
    u64 v=base();arch()->gpr[QR_RDI]=v+0xd000;
    if(!already_started)q31_begin(QI_LOAD,8,3);
    struct q31_access_result r=q31_memory(v+0xd000,Q_LOAD,8,0,READ_DATA);
    check(r.status==14&&r.error==0,14);check(q31_raise(14,0,r.address)==14,15);
    u64 shadow=cpu()->shadow;cpu()->shadow=0;
    E(Q32_SHADOW,QI_LOAD,1,0,shadow,0,cpu()->boundaries,1);
    q31_map(0xd000,7);q31_return(14,1);
}
static void current_fault(void){current_fault_from(0);}
static void retry_load(void){
    q31_begin(QI_LOAD,8,3);struct q31_access_result r=q31_memory(arch()->gpr[QR_RDI],Q_LOAD,8,0,READ_DATA);
    check(!r.status,16);arch()->gpr[QR_RAX]=r.value;q31_retire();
}
static void native_case(unsigned group,unsigned variant){
    u64 source=variant+1;
    if(group==1){receive(source,Q32_INTERIOR,0x41);service();return;}
    arch()->rip=base()+0x3550;arch()->gpr[QR_RDI]=base()+0xc000;arch()->gpr[QR_RAX]=1;
    q31_begin(QI_RMW,8,4);u64 old=read_ok(arch()->gpr[QR_RDI],READ_DATA),sum=old+1;
    receive(source,Q32_INTERIOR,0x41);check(arch()->gpr[QR_RAX]==1,17);
    write_ok(arch()->gpr[QR_RDI],sum,WRITE_DATA);
    u64 parity=sum&255;parity^=parity>>4;parity^=parity>>2;parity^=parity>>1;
    arch()->gpr[QR_RAX]=old;arch()->flags=(arch()->flags&~0x8d5UL)|(sum<old)|
        ((!(parity&1))<<2)|((old^1^sum)&16)|((!sum)<<6)|((sum>>63)<<7)|
        (((~(old^1)&(old^sum))>>63)<<11);
    q31_retire();service();
}
static void mask_case(unsigned variant){
    if(variant){q31_set_privilege(3);step(variant==1?QI_CLI:QI_STI);q31_set_privilege(0);return;}
    step(QI_CLI);receive(Q32_TIMER,Q32_INTERIOR,0x41);check(!dispatch(),18);
    step(QI_STI);check(!dispatch(),19);step(QI_NOP);service();
}
static void shadow_case(unsigned variant){
    if(variant!=1)arch()->flags&=~512UL;
    if(variant==4)arch()->flags|=512;
    if(variant==5)q31_map(0xd000,0);
    pending(0x41);
    if(variant==4){
        step(QI_MOVSS);check(!dispatch(),20);
        arch()->gpr[QR_RAX]=base()+0x5fe9;
        q31_begin(QI_MOVSP,8,3);arch()->gpr[QR_RSP]=arch()->gpr[QR_RAX];q31_retire();cpu()->shadow=0;
        E(Q32_SHADOW,QI_MOVSP,1,1,1,0,cpu()->boundaries,0);service();return;
    }
    step(QI_STI);
    if(variant==1){service();return;}
    check(!dispatch(),21);
    if(variant==2){step(QI_CLI);check(!dispatch(),22);step(QI_STI);step(QI_NOP);service();}
    else if(variant==3){step(QI_HLT);service();check(!cpu()->halted,23);}
    else if(variant==5){current_fault();service();retry_load();}
    else {step(QI_NOP);service();}
}
static void priority_case(unsigned variant){
    if(variant==0){pending(0x41);pending(0x4f);pending(0x71);service();return;}
    if(variant==2){
        E(Q32_CONTROL,4,cpu()->tpr,0x50,cpu()->boundaries,0,0,0);cpu()->tpr=0x50;
        pending(0x41);pending(0x71);service();check(bit(cpu()->irr,0x41),24);
        E(Q32_CONTROL,4,cpu()->tpr,0,cpu()->boundaries,0,0,0);cpu()->tpr=0;service();return;
    }
    pending(0x41);u64 first=dispatch();check(first==0x41,25);
    step(QI_STI);check(!dispatch(),72);step(QI_NOP);
    pending(0x4f);check(!dispatch(),26);pending(0x71);
    u64 nested=dispatch();check(nested==0x71,27);finish(nested);
    check(!dispatch(),28);finish(first);service();
}
static void halt_case(unsigned variant){
    struct q32_cpu *c=cpu();
    if(variant==3)step(QI_CLI);
    for(unsigned trial=1;trial<=(variant==2?Q32_HALT_TRIALS:1);trial++){
        if(variant==0)receive(Q32_TIMER,Q32_INTERIOR,0x41);
        // Every subtrial is a real HLT/IRQ/frame/IRET episode. An early native
        // arrival is serviced before rearming; no pending eligible IRQ is ignored.
        step(QI_HLT);E(Q32_HALT,1,c->wake_generation,eligible(),!!(arch()->flags&512),arch()->rip,c->native_count,c->stage);
        if(variant==1)receive(Q32_TIMER,Q32_INTERIOR,0x41);
        E(Q32_HALT,2,c->wake_generation,eligible(),!!(arch()->flags&512),arch()->rip,c->native_count,c->stage);
        if(variant>=2)receive(Q32_TIMER,Q32_HALT_WAIT,0x41);
        int post_wait=cpu()->last_elr==(u64)q32_wait_after;
        if(variant==2)E(Q32_CONTROL,15,trial,c->last_elr,(u64)q32_wait_after,post_wait,arch()->rip,c->native_count);
        E(Q32_HALT,3,c->wake_generation,eligible(),!!(arch()->flags&512),arch()->rip,c->native_count,c->stage);
        if(variant==3){
            check(c->halted&&!dispatch()&&bit(c->irr,0x41),29);
            u64 request=c->diagnostic_request+1;c->diagnostic_case=Q32->cases+1;
            E(Q32_CONTROL,14,request,c->diagnostic_case,arch()->flags,arch()->rip,c->irr[1],c->halted);
            ST(&c->diagnostic_request,request);u64 deadline=R(cntvct_el0)+S->counter_hz;
            while(LD(&c->diagnostic_ack)!=request){check(R(cntvct_el0)<deadline,74);q30_poll();NATIVE("yield");}
            E(Q32_CONTROL,2,c->halted,arch()->flags,arch()->rip,c->native_count,request,c->diagnostic_ack);
            clear(c->irr,0x41);c->halted=0;return;
        }
        service();check(!c->halted,30);
        if(variant!=2||post_wait)return;
        // Exhaustion is retained after servicing the eighth early arrival.
        check(trial<Q32_HALT_TRIALS,53);
        q31_begin(QI_JMP,1,2);
        struct q31_access_result opcode=q31_memory(arch()->rip,Q_FETCH8,1,0,FETCH);
        struct q31_access_result offset=q31_memory(arch()->rip+1,Q_FETCH8,1,0,FETCH);
        check(!opcode.status&&!offset.status&&opcode.value==0xeb&&offset.value==0xfd&&arch()->rip==base()+0x3501,77);
        q31_retire_at(arch()->rip-1); // Actual EB FD: next RIP + signed(-3).
    }
}
static void shim_case(unsigned variant){
    struct q32_cpu *c=cpu();u64 v=base();
    c->hook_stage=variant==0?Q32_REPAIR:variant==1?Q32_TRACKING:Q32_EXCEPTION;
    c->hook_source=Q32_TIMER;c->hook_vector=0x41;
    if(variant==1){q31_map(0xd000,7);}
    else {
        if(variant==2)q31_map(0xd000,0);else q30_invalidate(v+0xd000,0x32);
        arch()->rip=v+0x3000;arch()->gpr[QR_RDI]=v+0xd000;q31_begin(QI_LOAD,8,3);
        struct q31_access_result r=q31_memory(v+0xd000,Q_LOAD,8,0,READ_DATA);
        if(variant==2){check(r.status==14,31);check(q31_raise(14,r.error,r.address)==14,32);q31_map(0xd000,7);q31_return(14,1);}
        else {check(!r.status,33);arch()->gpr[QR_RAX]=r.value;q31_retire();}
    }
    check(!c->hook_stage&&bit(c->irr,0x41),34);service();if(variant==2)retry_load();
}
static void precedence_case(unsigned variant){
    u64 v=base();
    if(variant==2){
        q31_set_gate(0x41,v+0x3400,0x1008,0,14);q31_set_gate(14,v+0x3100,8,1,14);
        q31_map(0x11000,0);q31_set_privilege(3);pending(0x41);u64 result=dispatch();
        check(result==14&&bit(cpu()->isr,0x41)&&arch()->cr2==v+0x11008,35);
        // CONTROL3: accepted IRQ aborted by descriptor PF; no invented IRQ
        // requeue, ISR rollback, EOI or IRET. End this diagnostic context.
        E(Q32_CONTROL,3,0x41,14,arch()->cr2,arch()->gpr[QR_RSP],Q31->cpu[id()].depth,0);
        Q31->cpu[id()].depth=0;clear(cpu()->isr,0x41);return;
    }
    q31_map(0xd000,0);
    if(variant==0){
        arch()->rip=v+0xd000;pending(0x41);service();q31_begin(QI_FETCH,1,1);
        struct q31_access_result r=q31_memory(v+0xd000,Q_FETCH8,1,0,FETCH);
        check(r.status==14&&r.error==16,36);check(q31_raise(14,16,r.address)==14,37);
        q31_map(0xd000,7);q31_return(14,1);
    }else {
        arch()->rip=v+0x3000;arch()->gpr[QR_RDI]=v+0xd000;q31_begin(QI_LOAD,8,3);
        receive(Q32_TIMER,Q32_INTERIOR,0x41);current_fault_from(1);service();retry_load();
    }
}
static void string_case(unsigned variant){
    u64 v=base(),destination=v+0xdffc;int reverse=variant==1;
    write_ok(v+0x3570,0xa4f3,REPAIR);
    for(u64 i=0;i<8;i++){
        struct q31_access_result a=q31_memory(v+0xc400+i,Q_STORE,1,0xa0+i,REPAIR);
        struct q31_access_result b=q31_memory(destination+i,Q_STORE,1,0,REPAIR);check(!a.status&&!b.status,38);
    }
    arch()->rip=v+0x3570;arch()->gpr[QR_RSI]=v+0xc400+(reverse?7:0);
    arch()->gpr[QR_RDI]=destination+(reverse?7:0);arch()->gpr[QR_RCX]=variant==2?0:8;
    if(reverse)arch()->flags|=1024;
    q31_begin(QI_REP,1,2);unsigned progress=0;int interrupted=0,repaired=0;
    while(arch()->gpr[QR_RCX]){
        if(progress==4&&!interrupted){
            receive(Q32_TIMER,Q32_INTERIOR,0x41);
            u64 irq=dispatch_kind(1);check(irq==0x41,73);finish(irq);interrupted=1;
            if(variant==0)q31_map(0xe000,0);
            q31_begin(QI_REP,1,2);
        }
        struct q31_access_result a=q31_memory(arch()->gpr[QR_RSI],Q_LOAD,1,0,REP_READ);check(!a.status,39);
        struct q31_access_result b=q31_memory(arch()->gpr[QR_RDI],Q_STORE,1,a.value,REP_WRITE);
        if(b.status){
            check(variant==0&&!repaired&&b.status==14&&b.error==2,40);
            check(q31_raise(14,b.error,b.address)==14,41);q31_map(0xe000,7);q31_return(14,1);
            repaired=1;q31_begin(QI_REP,1,2);continue;
        }
        arch()->gpr[QR_RSI]+=reverse?-1UL:1;arch()->gpr[QR_RDI]+=reverse?-1UL:1;
        arch()->gpr[QR_RCX]--;progress++;q31_state_snapshot(QS_PROGRESS);
    }
    q31_retire();check(variant==2?!interrupted:interrupted,42);
    for(u64 i=0;i<8;i++){
        struct q31_access_result r=q31_memory(destination+i,Q_LOAD,1,0,REPAIR);
        check(!r.status&&r.value==(variant==2?0:0xa0+i),43);
    }
}
static void collapsed_case(unsigned variant){
    if(variant==1){
        cpu()->spi_level=1;receive(Q32_HOST,Q32_INTERIOR,0x41);
        u64 deadline=R(cntvct_el0)+S->counter_hz;
        while(LD(&cpu()->spi_deasserted)!=cpu()->spi_request){check(R(cntvct_el0)<deadline,44);q30_poll();}
        service();u64 start=q30_timer_count();
        while(q30_timer_count()<start+1){check(R(cntvct_el0)<deadline,55);q30_poll();}
        u64 before=cpu()->raw_irq[Q32_HOST-1];
        E(Q32_CONTROL,10,Q32_HOST,before,q30_timer_count(),cpu()->spi_deasserted,0,0);
        while(q30_timer_count()<start+3){check(R(cntvct_el0)<deadline,56);q30_poll();}
        u64 after=cpu()->raw_irq[Q32_HOST-1];check(after==before,57);
        E(Q32_CONTROL,9,Q32_HOST,before,after,start,q30_timer_count(),cpu()->spi_deasserted);
        cpu()->spi_level=0;return;
    }
    if(Q32->ncpus<3){E(Q32_CONTROL,5,3,Q32->ncpus,0,0,0,0);return;}
    // Mask native acknowledgement while both distinct sender mailboxes publish.
    u64 saved=irq_lock();cpu()->stage=Q32_INTERIOR;ST(&cpu()->hook_target,1);ST(&cpu()->ready,1);
    u64 deadline=R(cntvct_el0)+S->counter_hz;
    u64 one=(id()+1)%Q32->ncpus,two=(id()+2)%Q32->ncpus;
    while(LD(&cpu()->emitted[Q32_PEER-1][one])==cpu()->accepted[Q32_PEER-1][one]||
          LD(&cpu()->emitted[Q32_PEER-1][two])==cpu()->accepted[Q32_PEER-1][two]||
          LD(&cpu()->sgi_sent[one])!=LD(&cpu()->emitted[1][one])||
          LD(&cpu()->sgi_sent[two])!=LD(&cpu()->emitted[1][two]))
        check(R(cntvct_el0)<deadline,45);
    // CONTROL6 documents both payloads before native acknowledgement resumes.
    E(Q32_CONTROL,6,one,two,cpu()->emitted[1][one],cpu()->emitted[1][two],0,0);
    u64 before=cpu()->native_count;irq_unlock(saved);
    while(cpu()->native_count==before){check(R(cntvct_el0)<deadline,46);NATIVE("yield");}
    ST(&cpu()->ready,0);ST(&cpu()->hook_target,0);cpu()->stage=0;service();
}
static void code_case(unsigned remote){
    u64 v=base(),page=remote?0x28c000:0x280000+id()*0x20000+0xc000;
    u64 va=v+(remote?0x1b020:0xc020);
    write_ok(va-16,0x078948,REPAIR);write_ok(va+16,0x078b48,REPAIR);
    write_ok(va,0xc30000002ab8UL,REPAIR);q30_code_track(page,1);
    check(q31_code_dispatch(va,page)==42,47);q31_code_acquire(va,page);
    u64 generation=q31_code_generation(page);
    arch()->rip=remote?va+16:va-16;arch()->gpr[QR_RDI]=remote?v+0xd000:va;
    if(!remote)arch()->gpr[QR_RAX]=0xc30000002bb8UL;
    q31_begin(remote?QI_LOAD:QI_CODE_STORE,8,3);
    if(remote){
        ST(&cpu()->hook_target,1); // Let the peer write while this old pin lives.
        u64 deadline=R(cntvct_el0)+S->counter_hz;
        while(q31_code_generation(page)==generation||LD(&Q30->gate)){
            check(R(cntvct_el0)<deadline,52);q30_poll();
        }
        receive(Q32_PEER,Q32_INTERIOR,0x41);
        struct q31_access_result r=q31_memory(v+0xd000,Q_LOAD,8,0,READ_DATA);check(!r.status,48);
        arch()->gpr[QR_RAX]=r.value;
    }else{
        cpu()->hook_stage=Q32_CODE_WRITE;cpu()->hook_source=Q32_TIMER;cpu()->hook_vector=0x41;
        write_ok(va,0xc30000002bb8UL,WRITE_DATA);check(!cpu()->hook_stage,49);
    }
    q31_retire();check(q31_code_generation(page)>generation,50);service();
    q31_code_release();q31_code_serialize(va);check(q31_code_dispatch(va,page)==43,51);
    q30_code_track(page,0);
}
static void broadcast_case(unsigned route,unsigned repetition){
    if(Q32->ncpus!=4){E(Q32_CONTROL,5,4,Q32->ncpus,0,0,0,0);return;}
    u64 leader=repetition%4,role=(id()+4-leader)%4;
    u64 before=cpu()->native_count;
    if(role==1)arch()->flags&=~512UL;
    if(role==2){write_ok(arch()->rip,0xf4,REPAIR);step(QI_HLT);}
    q30_barrier();
    if(id()==leader){
        u64 deadline=R(cntvct_el0)+S->counter_hz;
        for(u64 target=0;target<4;target++)if(target!=leader)
            while(!LD(&Q32->cpu[target].ready)){check(R(cntvct_el0)<deadline,58);NATIVE("yield");}
        u64 saved=irq_lock(),mask=15UL&~(1UL<<leader);
        // Publish all target payloads before the single hardware broadcast.
        for(u64 target=0;target<4;target++){
            struct q32_cpu *t=&Q32->cpu[target];u64 sequence=LD(&t->emitted[1][leader])+1;
            t->vectors[1][leader]=0x41;E(Q32_EMIT,Q32_PEER,leader,target,sequence,0x41,route,1);
            ST(&t->emitted[1][leader],sequence);
        }
        u64 word=((u64)Q32_SGI<<24)|(route?1UL<<40:mask);
        u64 self=((u64)Q32_SGI<<24)|(1UL<<leader);
        E(Q32_CONTROL,7,leader,route,mask,word,self,4);
        W(icc_sgi1r_el1,word);W(icc_sgi1r_el1,self);NATIVE("isb");
        for(u64 target=0;target<4;target++){
            struct q32_cpu *t=&Q32->cpu[target];u64 sequence=LD(&t->emitted[1][leader]);
            E(Q32_CONTROL,13,leader,target,sequence,target==leader?self:word,route,0);
            ST(&t->sgi_sent[leader],sequence);
        }
        irq_unlock(saved);
        while(cpu()->native_count==before){check(R(cntvct_el0)<deadline,59);NATIVE("yield");}
    }else if(role==3){
        cpu()->hook_stage=Q32_TRACKING;cpu()->hook_source=Q32_PEER;cpu()->hook_vector=0x41;
        q31_map(0xd000,7);check(!cpu()->hook_stage,60);
    }else receive(Q32_PEER,role==2?Q32_HALT_WAIT:Q32_INTERIOR,0x41);
    q30_barrier();
    if(role==1)check(!dispatch()&&bit(cpu()->irr,0x41),61);else service();
    q30_barrier();
    if(role==1){
        // Explicit contiguous STI;NOP program releases this target later.
        write_ok(base()+0x3580,0x90fb,REPAIR);arch()->rip=base()+0x3580;
        step(QI_STI);check(!dispatch(),62);step(QI_NOP);service();
    }
    q30_barrier();check(cpu()->native_count==before+1,63);
}
static void controller(unsigned group,unsigned variant,u64 target){
    int send=(group==1||group==2)?variant==1:(group==12&&variant==1);
    int collapse=group==6&&variant==0&&Q32->ncpus>=3;
    u64 sender=(target+1)%Q32->ncpus;
    if(!(send&&id()==sender)&&!(collapse&&(id()==sender||id()==(target+2)%Q32->ncpus)))return;
    struct q32_cpu *t=&Q32->cpu[target];u64 deadline=R(cntvct_el0)+S->counter_hz;
    // Prior setup may need this CPU's shootdown acknowledgement. After the
    // target publishes hook_target, no gate is entered before its hold.
    while(!LD(&t->hook_target)){check(R(cntvct_el0)<deadline,64);q30_poll();NATIVE("yield");}
    if(group==12){
        arch()->rip=base()+0x2ff0;arch()->gpr[QR_RDI]=base()+0x1b020;arch()->gpr[QR_RAX]=0xc30000002bb8UL;
        q31_begin(QI_STORE,8,3);write_ok(arch()->gpr[QR_RDI],arch()->gpr[QR_RAX],WRITE_DATA);q31_retire();
    }
    while(!LD(&t->ready)){check(R(cntvct_el0)<deadline,65);NATIVE("yield");}
    emit(Q32_PEER,target,0x41,0);
}
void q32_run(u64 number){
    check(number==id()&&number<MAX_CPUS&&Q32->magic==Q32_MAGIC&&Q32->version==Q32_VERSION&&
          Q32->ncpus==Q31->ncpus&&Q31->phase==2,66);
    cpu()->started=1;cpu()->timer_start=q30_timer_count();
    E(Q32_CONTROL,11,R(icc_ctlr_el1),S->timer_intid,Q32->spi_base+id(),Q32_SGI,S->cpu[id()].redistributor,R(mpidr_el1));
    q30_barrier();if(id()==0)ST(&Q32->phase,1);q30_barrier();
    static const unsigned variants[]={3,3,3,6,3,2,4,3,3,3,2,2};u64 serial=0;
    for(unsigned group=1;group<=12;group++)for(unsigned rep=0;rep<Q32_REPETITIONS;rep++)
        for(unsigned variant=0;variant<variants[group-1];variant++){
            ++serial;q30_barrier();
            if(id()==0)E(Q32_CASE,group,variant,rep,1,serial,~0UL,0);
            q30_barrier();q32_fixture(serial,group,variant);q30_barrier();
            if(group==11)broadcast_case(variant,rep);
            else for(u64 target=0;target<Q32->ncpus;target++){
                if(id()==target){
                    if(group<=2)native_case(group,variant);
                    else if(group==3)mask_case(variant);
                    else if(group==4)shadow_case(variant);
                    else if(group==5)priority_case(variant);
                    else if(group==6)collapsed_case(variant);
                    else if(group==7)halt_case(variant);
                    else if(group==8)shim_case(variant);
                    else if(group==9)precedence_case(variant);
                    else if(group==10)string_case(variant);
                    else code_case(variant);
                }else controller(group,variant,target);
                q30_barrier();
            }
            check(!highest(cpu()->irr)&&!highest(cpu()->isr)&&!cpu()->halted&&!Q31->cpu[id()].depth,67);
            q31_state_snapshot(QS_PROGRESS);cpu()->cases++;
            E(Q32_END,serial,group,variant,rep,cpu()->guest_count,cpu()->native_count,cpu()->boundaries);
            q30_barrier();if(id()==0){E(Q32_CASE,group,variant,rep,0,serial,~0UL,0);Q32->cases++;}
            q30_barrier();
        }
    cpu()->timer_end=q30_timer_count();cpu()->done=1;
    q30_barrier();if(id()==0)ST(&Q32->phase,2);q30_barrier();
}
void q32_reuse_fixture(u64 serial){q32_fixture(serial,0,0);}
void q32_receive(u64 source,u64 stage,u64 vector){receive(source,stage,vector);}
void q32_service(void){service();}
void q32_emit(u64 source,u64 target,u64 vector){emit(source,target,vector,0);}

void q32_service_change(u64 address,u64 value){
    u64 vector=dispatch();check(vector>=32,75);
    // Fixture supplied a contiguous second MOV followed by the normal EOI branch.
    arch()->gpr[QR_RDI]=address;arch()->gpr[QR_RAX]=value;
    q31_begin(QI_STORE,8,3);write_ok(address,value,WRITE_DATA);q31_retire();
    finish(vector);check(!dispatch(),76);
}

void q32_enable_interrupts(void){step(QI_STI);step(QI_NOP);}
