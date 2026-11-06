#include "q34.h"
#ifdef Q30_FIXTURE_CHECK
extern struct shared *q31_fixture_shared;
extern u64 q32_fixture_read(const char *);
extern void q32_fixture_write(const char *,u64);
extern void q32_fixture_poll(void);
extern u64 q33_fixture_execute(struct q33_block *,u64);
extern unsigned char *q33_fixture_code(u64 address);
#define CODE(address) q33_fixture_code(address)
#define R(reg) q32_fixture_read(#reg)
#define W(reg,x) q32_fixture_write(#reg,(u64)(x))
#define NATIVE(text) q32_fixture_poll()
#define S q31_fixture_shared
#else
#define R(reg) ({u64 x;__asm__ volatile("mrs %0," #reg:"=r"(x));x;})
#define W(reg,x) __asm__ volatile("msr " #reg ",%0"::"r"((u64)(x)):"memory")
#define NATIVE(text) __asm__ volatile(text:::"memory")
#define S ((volatile struct shared *)SHARED_PA)
#define CODE(address) ((unsigned char *)(address))
#endif
#define LD(p) __atomic_load_n((p),__ATOMIC_ACQUIRE)
#define ST(p,x) __atomic_store_n((p),(x),__ATOMIC_RELEASE)
#define E(type,...) QLOG(QE_Q33,(type),__VA_ARGS__)
static u64 number(void){return R(tpidr_el1);}
static struct q33_cpu *cpu(void){return &Q33->cpu[number()];}
static struct q31_arch *arch(void){return &Q31->cpu[number()].arch;}
static u64 base(void){return q31_base();}
static void check(int ok,u64 why){if(!ok)Q33->failure=why;q30_check(ok,0x3300+why);}
static u64 read_ok(u64 va,unsigned kind,unsigned width){
    struct q31_access_result r=q31_memory(va,kind,width,0,kind==Q_FETCH8?7:6);check(!r.status,1);return r.value;
}
static void write_ok(u64 va,u64 value){
    struct q31_access_result r=q31_memory(va,Q_STORE,8,value,6);check(!r.status,2);
}
static void page_map(u64 va,u64 page,u64 flags){
    write_ok(base()+0x18000+((va>>12)&511)*8,page|flags);q30_invalidate(va,0x33);
}
static void poll(void){q30_poll();cpu()->progress++;NATIVE("yield");}
static u64 source_word(u64 operand){return (operand<<16)|0x0548;}
static void exchange(struct q33_packet *p){
    q30_poll();check(!Q30->cpu[number()].active&&!Q30->cpu[number()].request.inflight,3);
    cpu()->transport_active=1;
    // CONTROL1 marks the actual helper call; CONTROL2 follows register copy and maintenance polling.
    E(Q33_CONTROL,1,p->word[0],p->word[1],p->word[2],Q30->epoch,Q30->gate,Q30->lock);
    int delayed_timer=cpu()->group==6&&p->word[0]==QT_PULL&&
        p->word[2]==0&&(p->word[11]&QF_DELAY)&&cpu()->phase==1;
    u64 irq_state=0,sequence=0;
    if(delayed_timer){
        irq_state=R(daif);W(daif,irq_state|128);Q32->cpu[number()].stage=7;
        u64 sender=cpu()->variant==1?(number()+1)%Q33->ncpus:number();
        sequence=Q32->cpu[number()].emitted[cpu()->variant][sender]+1;
        ST(&cpu()->phase,3);ST(&Q32->cpu[number()].ready,1);
        if(cpu()->variant!=1)q32_emit(cpu()->variant+1,number(),0x41);
        E(Q33_CONTROL,8,sequence,R(cntp_cval_el0),R(cntvct_el0),p->word[1],p->word[2],0);
    }
    q33_transfer(p);cpu()->transport_active=0;
    if(delayed_timer){
        for(u64 chunk=0;chunk<4;chunk++)E(Q33_CONTROL,11,chunk,p->word[chunk*4],p->word[chunk*4+1],
            p->word[chunk*4+2],p->word[chunk*4+3],cpu()->transaction);
        W(daif,irq_state);
        u64 deadline=R(cntvct_el0)+S->counter_hz;
        while(Q32->cpu[number()].accepted[cpu()->variant][cpu()->variant==1?(number()+1)%Q33->ncpus:number()]!=sequence){check(R(cntvct_el0)<deadline,61);q30_poll();NATIVE("yield");}
        ST(&Q32->cpu[number()].ready,0);Q32->cpu[number()].stage=0;
        E(Q33_CONTROL,9,sequence,p->word[1],p->word[10],Q32->cpu[number()].last_elr,Q32->cpu[number()].last_spsr,0);
        q32_service_change(base()+0xe020,source_word(90+number()));cpu()->interrupts++;
        write_ok(base()+0x3400,0x5beb078948UL);
    }
    q30_poll();E(Q33_CONTROL,2,p->word[0],p->word[1],p->word[2],p->word[10],Q30->epoch,Q30->gate);
}
static struct q33_packet packet(u64 op,u64 txn,u64 offset,u64 length,u64 total,u64 generation,u64 flags){
    struct q33_packet p={{op,txn,offset,length,0,0,0,0,total,generation,0,flags,
        cpu()->group,cpu()->variant,cpu()->repetition,number()}};return p;
}
static int reply_valid(const struct q33_packet *p,u64 txn,u64 offset,u64 length,u64 total,u64 generation){
    return p->word[0]==QT_PULL&&p->word[1]==txn&&p->word[2]==offset&&
        p->word[3]==length&&p->word[8]==total&&p->word[9]==generation&&
        p->word[10]==QT_OK&&p->word[15]==number();
}
static void abandon(u64 txn,u64 why,u64 offset,u64 expected,u64 actual,u64 generation){
    cpu()->rejections++;E(Q33_REJECT,txn,why,offset,expected,actual,generation,0);
    struct q33_packet p=packet(QT_CANCEL,txn,offset,0,0,generation,0);exchange(&p);
    check(p.word[10]==QT_DONE,4);
}
static struct q33_block *reserve(u64 length){
    if(LD(&Q33->allocated)>=cpu()->allocation_limit){
        __atomic_fetch_add(&Q33->pool_exhaustions,1,__ATOMIC_RELAXED);
        E(Q33_CONTROL,6,cpu()->allocation_limit,LD(&Q33->allocated),Q33->pool_bytes,length,0,0);return 0;
    }
    u64 index=__atomic_fetch_add(&Q33->allocated,1,__ATOMIC_RELAXED);
    u64 bytes=(length+83)&~63UL,offset=__atomic_fetch_add(&Q33->pool_bytes,bytes,__ATOMIC_RELAXED);
    check(index<Q33_OBJECTS&&offset+bytes<=Q33_POOL_END-Q33_POOL,5);
    struct q33_block *b=&Q33->blocks[index];
    *b=(struct q33_block){.id=index+1,.address=Q33_POOL+offset+20,.length=length};return b;
}
__attribute__((noinline)) static void publish(struct q33_block *b){
    u64 ctr=R(ctr_el0),dc=4UL<<((ctr>>16)&15),ic=4UL<<(ctr&15);
    check(dc>=4&&dc<=2048&&ic>=4&&ic<=2048,6);
#ifndef Q30_FIXTURE_CHECK
    for(u64 va=b->address&~(dc-1);va<b->address+b->length;va+=dc)
        __asm__ volatile(".globl _q33_publish_dc\n_q33_publish_dc:\ndc cvau,%0"::"r"(va):"memory");
    NATIVE(".globl _q33_publish_dsb1\n_q33_publish_dsb1:\ndsb ish");
    for(u64 va=b->address&~(ic-1);va<b->address+b->length;va+=ic)
        __asm__ volatile(".globl _q33_publish_ic\n_q33_publish_ic:\nic ivau,%0"::"r"(va):"memory");
    NATIVE(".globl _q33_publish_dsb2\n_q33_publish_dsb2:\ndsb ish\n.globl _q33_publish_isb\n_q33_publish_isb:\nisb");
#endif
    E(Q33_PUBLISH,b->id,b->address,b->length,b->generation,dc,ic,b->transaction);
    ST(&b->valid,1);cpu()->publishes++;
}
static u64 generated_enter(struct q33_block *,u64,u64);
static u64 generated_finish(struct q33_block *,u64,u64);
static int source_read(u64 rip,u64 page,u64 kind,u64 *word){
    *word=0;arch()->rip=rip;q31_begin(QI_FETCH,1,kind==1?6:3);
    for(u64 i=0;i<(kind==1?8UL:3UL);i++){
        struct q31_access_result r=q31_memory(rip+i,Q_FETCH8,1,0,7);
        if(r.status||(Q30->cpu[number()].request.gpa&~4095UL)!=page)return 0;
        *word|=r.value<<(8*i);
    }
    return 1;
}
static struct q33_block *produce(u64 rip,u64 page,u64 kind,u64 length,u64 flags){
    u64 generation=q31_code_generation(page),source=0;
    if(!source_read(rip,page,kind,&source)||generation!=q31_code_generation(page)){
        cpu()->rejections++;E(Q33_REJECT,0,5,rip,page,source,generation,0);return 0;
    }
    struct q33_request request={rip,Q30->cpu[number()].context.cr3,page,generation,
        source,(source>>16)&0xffffffffUL,kind,length,++cpu()->transaction,number(),
        (cpu()->group==11?Q34->serial:Q33->cases+1),(cpu()->repetition+1)*17+number()};
    u64 txn=request.identity;cpu()->requests++;
    E(Q33_REQUEST,txn,rip,page,generation,source,kind,length);
    struct q33_packet p=packet(QT_BEGIN,txn,0,0,sizeof(request),generation,flags);exchange(&p);
    check(p.word[10]==QT_OK,7);
    const u64 *words=(const u64 *)&request;
    for(u64 offset=0;offset<sizeof(request);offset+=Q33_CHUNK){
        p=packet(QT_REQUEST,txn,offset,Q33_CHUNK,sizeof(request),generation,flags);
        for(unsigned j=0;j<4;j++)p.word[4+j]=words[offset/8+j];
        E(Q33_SEND,txn,offset,Q33_CHUNK,p.word[4],p.word[5],p.word[6],p.word[7]);
        exchange(&p);check(p.word[10]==QT_OK,8);
    }
    struct q33_block *b=reserve(length);
    if(!b){abandon(txn,6,0,cpu()->allocation_limit,Q33->allocated,generation);return 0;}
    b->rip=rip;b->page=page;b->generation=generation;b->source=source;b->operand=request.operand;
    b->kind=kind;b->transaction=txn;b->enter=(u64)generated_enter;b->finish=(u64)generated_finish;
    u64 waits=0;
    for(u64 offset=0;offset<length;){
        u64 take=length-offset<Q33_CHUNK?length-offset:Q33_CHUNK;
        p=packet(QT_PULL,txn,offset,take,length,generation,flags);exchange(&p);
        if(p.word[10]==QT_WAIT){
            check(++waits<=1,9);
            if(cpu()->group==10&&cpu()->variant==2){
                // Controlled shutdown cancels an actual pending transaction; no payload is consumed.
                abandon(txn,7,offset,0,p.word[10],generation);return 0;
            }
            if(flags&(QF_STALE|QF_PARTIAL)){
                ST(&cpu()->waiting,1);u64 deadline=R(cntvct_el0)+S->counter_hz;
                while(!LD(&cpu()->release)){check(R(cntvct_el0)<deadline,19);poll();}
                ST(&cpu()->waiting,0);
            }
            poll();continue;
        }
        if(!reply_valid(&p,txn,offset,take,length,generation)){
            abandon(txn,1,offset,offset,p.word[2],generation);return 0;
        }
        E(Q33_RECEIVE,txn,offset,take,p.word[4],p.word[5],p.word[6],p.word[7]);
        unsigned char *dst=CODE(b->address+offset);
        for(u64 i=0;i<take;i++)dst[i]=(unsigned char)(p.word[4+i/8]>>(8*(i%8)));
        cpu()->chunks++;cpu()->partial=offset+take;
        // CONTROL3 follows the actual stores, before their consumed acknowledgement.
        E(Q33_CONTROL,3,txn,b->address+offset,take,offset+take,b->id,0);
        if((flags&QF_PARTIAL)&&offset==0){
            ST(&cpu()->waiting,1);u64 deadline=R(cntvct_el0)+S->counter_hz;
            while(!LD(&cpu()->release)){check(R(cntvct_el0)<deadline,66);poll();}
            ST(&cpu()->waiting,0);
        }
        p=packet(QT_ACK,txn,offset,take,length,generation,flags);exchange(&p);
        check(p.word[10]==(offset+take==length?QT_DONE:QT_OK),10);
        E(Q33_ACK,txn,offset,take,p.word[10],length,generation,0);cpu()->acknowledgements++;
        offset+=take;
    }
    cpu()->partial=0;
    u64 verify=0;
    if(Q30->cpu[number()].context.cr3!=request.cr3||!source_read(rip,page,kind,&verify)||
       verify!=source||q31_code_generation(page)!=generation){
        cpu()->rejections++;E(Q33_REJECT,txn,2,0,generation,q31_code_generation(page),generation,0);return 0;
    }
    publish(b);return b;
}
static u64 add_flags(u64 old,u64 rhs,u64 sum,u64 flags){
    u64 p=sum&255;p^=p>>4;p^=p>>2;p^=p>>1;
    return (flags&~0x8d5UL)|(sum<old)|((!(p&1))<<2)|((old^rhs^sum)&16)|
        ((!sum)<<6)|((sum>>63)<<7)|(((~(old^rhs)&(old^sum))>>63)<<11);
}
static u64 execute(struct q33_block *b,u64 value);
static u64 generated_enter(struct q33_block *b,u64 pc,u64 input){
#ifdef Q30_FIXTURE_CHECK
    u64 lr=b->address+40;
#else
    u64 lr=(u64)__builtin_return_address(0);
#endif
    check(pc==b->address&&lr==b->address+40&&cpu()->current==b->id,11);
    E(Q33_ENTER,b->id,pc,lr,input,b->generation,LD(&b->users),LD(&b->link));
    cpu()->entries++;__atomic_fetch_add(&b->entered,1,__ATOMIC_RELAXED);
    if(cpu()->phase==b->id&&cpu()->group==3){
        ST(&cpu()->waiting,1);u64 deadline=R(cntvct_el0)+S->counter_hz;
        while(!LD(&cpu()->release)){check(R(cntvct_el0)<deadline,52);poll();}
    }
    check(arch()->rip==b->rip&&arch()->gpr[QR_RAX]==input,20);
    q31_begin(b->kind==1?QI_ADD:b->kind==3?QI_CODE_STORE:QI_LOAD,8,b->kind==1?6:3);
    if(cpu()->group==9&&cpu()->variant==1){
        ST(&cpu()->waiting,1);u64 deadline=R(cntvct_el0)+S->counter_hz;
        while(!LD(&cpu()->release)){check(R(cntvct_el0)<deadline,56);poll();}
    }
    if(b->kind==3){
        struct q31_access_result r=q31_memory(arch()->gpr[QR_RDI],Q_STORE,8,arch()->gpr[QR_RAX],2);
        check(!r.status&&q31_code_generation(b->page)>b->generation,57);return input;
    }
    if(b->kind==2){
        struct q31_access_result r=q31_memory(arch()->gpr[QR_RDI],Q_LOAD,8,0,1);
        if(r.status){
            check(r.status==14,12);cpu()->faults++;
            check(q31_raise(14,r.error,r.address)==14,13);
            q31_map(0xd000,7);q31_return(14,1);q31_begin(QI_LOAD,8,3);
            r=q31_memory(arch()->gpr[QR_RDI],Q_LOAD,8,0,1);check(!r.status,14);
        }
        return r.value;
    }
    return input;
}
static u64 generated_finish(struct q33_block *b,u64 pc,u64 result){
#ifdef Q30_FIXTURE_CHECK
    u64 lr=b->address+b->length-12;
#else
    u64 lr=(u64)__builtin_return_address(0);
#endif
    check(pc==b->address&&lr==b->address+b->length-12,15);
    u64 input=arch()->gpr[QR_RAX];
    if(b->kind==1)check(result==input+b->operand,16);
    arch()->gpr[QR_RAX]=result;
    if(b->kind==1)arch()->flags=add_flags(input,b->operand,result,arch()->flags);
    q31_retire();cpu()->effects++;
    E(Q33_EFFECT,b->id,lr,input,result,arch()->rip,arch()->flags,b->generation);
    q31_code_release();__atomic_fetch_sub(&b->users,1,__ATOMIC_RELEASE);
    if(cpu()->group==6){
        ST(&cpu()->phase,2);
        q32_receive(cpu()->variant+1,Q32_INTERIOR,0x41);q32_service();cpu()->interrupts++;
    }
    u64 link=LD(&b->link);
    if(link){
        check(link<=LD(&Q33->allocated),17);
        q31_begin(QI_JMP,1,2);u64 jump=read_ok(arch()->rip,Q_FETCH8,1),offset=read_ok(arch()->rip+1,Q_FETCH8,1);
        u64 target=arch()->rip+2+(long)(signed char)offset;
        check(jump==0xeb&&target==Q33->blocks[link-1].rip,53);q31_retire_at(target);
        E(Q33_LINK,2,b->id,link,link,Q30->epoch,1UL<<number(),0);cpu()->links++;
        return execute(&Q33->blocks[link-1],result);
    }
    return result;
}
static u64 execute(struct q33_block *b,u64 value){
    check(b&&LD(&b->valid)&&++cpu()->depth<=3,18);
    u64 epoch=LD(&Q30->epoch),verify=0;
    int source_valid=source_read(b->rip,b->page,b->kind,&verify)&&verify==b->source;
    if(source_valid&&cpu()->group==5&&cpu()->phase==4){
        ST(&cpu()->waiting,2);u64 deadline=R(cntvct_el0)+S->counter_hz;
        while(LD(&cpu()->release)!=2){check(R(cntvct_el0)<deadline,63);poll();}
        E(Q33_CONTROL,10,b->id,epoch,Q30->epoch,b->page,b->generation,q31_code_generation(b->page));
    }
    if(!source_valid||!q31_code_acquire_expected(b->rip,b->page,b->generation)){
        cpu()->depth--;cpu()->rejections++;E(Q33_REJECT,b->transaction,3,b->id,b->generation,
            q31_code_generation(b->page),b->generation,0);return ~0UL;
    }
    u64 current_epoch=LD(&Q30->epoch);
    if(epoch!=current_epoch){
        q31_code_release();cpu()->depth--;cpu()->rejections++;
        E(Q33_REJECT,b->transaction,4,b->id,epoch,current_epoch,b->generation,0);return ~0UL;
    }
    __atomic_fetch_add(&b->users,1,__ATOMIC_ACQUIRE);
    cpu()->current=b->id;
    NATIVE("isb");E(Q33_CONTROL,4,b->id,b->address,b->generation,number(),cpu()->depth,0);
#ifdef Q30_FIXTURE_CHECK
    u64 result=q33_fixture_execute(b,value);
#else
    u64 result=((u64 (*)(u64,struct q33_block *))b->address)(value,b);
#endif
    cpu()->depth--;cpu()->current=0;return result;
}
static u64 run_block(struct q33_block *b,u64 input){
    // Entry rejection has no guest effect; bounded retries discharge unrelated native misses.
    for(unsigned attempt=0;attempt<16;attempt++){
        arch()->rip=b->rip;arch()->gpr[QR_RAX]=input;
        u64 effects=cpu()->effects,result=execute(b,input);
        if(result!=~0UL)return result;
        check(cpu()->effects==effects,21);poll();
    }
    check(0,22);return 0;
}
static void link_set(struct q33_block *from,struct q33_block *to,u64 action){
    u64 old=LD(&from->link);ST(&from->link,to?to->id:0);
    E(Q33_LINK,action,from->id,old,to?to->id:0,Q30->epoch,0,0);
}
static void wait_word(u64 *word,u64 value){
    u64 deadline=R(cntvct_el0)+S->counter_hz;
    while(LD(word)!=value){check(R(cntvct_el0)<deadline,23);poll();}
}
static void prepare_case(u64 serial){
    q32_reuse_fixture(serial);
    cpu()->phase=cpu()->waiting=cpu()->release=cpu()->observed=cpu()->partial=0;
    cpu()->allocation_limit=Q33_OBJECTS;
    u64 own=0x28e000+number()*0x20000;
    write_ok(base()+0xe020,source_word(7+cpu()->repetition));
    write_ok(base()+0xd000,0x123000+number());
    q30_code_track(own,1);
    // Common guest RIP has a real identical source mapping in every address space.
    page_map(0x1000e020,0x28e000,7);q30_barrier();
}
static void simple_case(unsigned group,unsigned variant){
    u64 own=0x28e000+number()*0x20000,rip=base()+0xe020,page=own;
    if(group==1&&variant){
        if(!number()){
            struct q33_block *b=produce(0x1000e020,0x28e000,1,80,0);check(b!=0,24);ST(&cpu()->observed,b->id);
        }
        q30_barrier();struct q33_block *b=&Q33->blocks[LD(&Q33->cpu[0].observed)-1];
        check(run_block(b,100+number())==107+number()+cpu()->repetition,25);return;
    }
    if(group==4&&variant){rip=0x1000e020;page=0x28e000;}
    u64 length=group==2?(variant?4288:160):80;
    struct q33_block *b=produce(rip,page,1,length,0);check(b!=0,26);
    check(run_block(b,100+number())==107+number()+cpu()->repetition,27);
}
static void chain_case(unsigned variant){
    u64 rip=0x1000e020;
    if(!number()){
        write_ok(rip,source_word(1)|(0x18ebUL<<48));write_ok(rip+32,source_word(2)|(0x18ebUL<<48));write_ok(rip+64,source_word(3));
        struct q33_block *a=produce(rip,0x28e000,1,80,0),*b=produce(rip+32,0x28e000,1,80,0);
        struct q33_block *c=produce(rip+64,0x28e000,1,80,0);
        struct q33_block *replacement=produce(rip+(variant?64:32),0x28e000,1,160,0);
        check(a&&b&&c&&replacement,28);link_set(a,b,1);link_set(b,c,1);
        if(!variant)link_set(replacement,c,1);ST(&cpu()->observed,a->id);
    }
    q30_barrier();u64 first=LD(&Q33->cpu[0].observed);struct q33_block *a=&Q33->blocks[first-1];
    if(number()){
        cpu()->phase=first+(variant?2:1);check(run_block(a,100)==106,29);cpu()->phase=0;
    }else{
        for(unsigned peer=1;peer<Q33->ncpus;peer++)wait_word(&Q33->cpu[peer].waiting,1);
        struct q33_block *from=variant?&Q33->blocks[first]:a,*to=&Q33->blocks[first+2];
        link_set(from,to,1);
        for(unsigned peer=1;peer<Q33->ncpus;peer++)ST(&Q33->cpu[peer].release,1);
    }
    q30_barrier();E(Q33_LINK,3,a->id,0,0,Q30->epoch,1UL<<number(),0);q30_barrier();
    for(unsigned iteration=0;iteration<4;iteration++)check(run_block(a,iteration)==iteration+6,30);
}
static void stale_case(unsigned variant,u64 target){
    u64 actor=(target+1)%Q33->ncpus,rip=0x1000e020;
    if(number()==target){
        struct q33_block *old=produce(rip,0x28e000,1,80,variant==2?QF_DUPLICATE:QF_STALE);
        check(!old,31);u64 page=variant==1?0x300000+target*4096:0x28e000;
        struct q33_block *fresh=produce(rip,page,1,80,0);check(fresh!=0,32);
        if(variant!=2){
            cpu()->phase=4;arch()->rip=fresh->rip;arch()->gpr[QR_RAX]=10;u64 effects=cpu()->effects;
            check(execute(fresh,10)==~0UL&&cpu()->effects==effects,64);cpu()->phase=0;
            fresh=produce(rip,0x28e000,1,80,0);check(fresh!=0,65);
        }
        check(run_block(fresh,10)==10+fresh->operand,33);
    }else if(number()==actor&&variant!=2){
        wait_word(&Q33->cpu[target].waiting,1);
        if(!variant)write_ok(rip,source_word(30+target));
        else{
            u64 page=0x300000+target*4096;
            page_map(base()+0x1c000,page,7);write_ok(base()+0x1c020,source_word(40+target));q30_code_track(page,1);
            page_map(base()+0x1a000,0x253000+target*0x4000,7);
            write_ok(base()+0x1a000+((rip>>12)&511)*8,page|7);q30_invalidate(rip,0x33);
        }
        ST(&Q33->cpu[target].release,1);
        wait_word(&Q33->cpu[target].waiting,2);
        if(!variant)write_ok(rip,source_word(50+target));
        else{write_ok(base()+0x1a000+((rip>>12)&511)*8,0x28e000|7);q30_invalidate(rip,0x33);}
        ST(&Q33->cpu[target].release,2);
    }
    q30_barrier();
    if(variant==1){page_map(rip,0x28e000,7);q30_barrier();if(number()==actor)q30_code_track(0x300000+target*4096,0);}
}
static void interrupt_case(unsigned variant,u64 target){
    if(number()==target){
        u64 page=0x28e000+number()*0x20000;
        // A real source is serviced at the boundary before asking the host.
        ST(&cpu()->phase,1);
        q32_receive(variant+1,Q32_INTERIOR,0x41);q32_service();cpu()->interrupts++;
        write_ok(base()+0x3400,0x58eb078948078948UL);
        struct q33_block *b=produce(base()+0xe020,page,1,80,QF_DELAY);
        check(!b,62);b=produce(base()+0xe020,page,1,80,0);
        check(b!=0,34);check(run_block(b,10)==10+b->operand,35);
    }else if(number()==(target+1)%Q33->ncpus&&variant==1){
        for(unsigned send=0;send<3;send++){
            const u64 phase[]={1,3,2};wait_word(&Q33->cpu[target].phase,phase[send]);
            if(send==1){u64 until=R(cntvct_el0)+S->counter_hz/500;while(R(cntvct_el0)<until)poll();}
            wait_word(&Q32->cpu[target].ready,1);q32_emit(Q32_PEER,target,0x41);
            u64 seq=LD(&Q32->cpu[target].emitted[1][number()]);
            while(LD(&Q32->cpu[target].accepted[1][number()])!=seq)poll();
        }
    }
}
static void malformed_case(unsigned variant,u64 target){
    if(number()==target){
        static const u64 flags[]={QF_DUPLICATE,QF_OFFSET,QF_LENGTH,QF_IDENTITY,QF_FAILURE,QF_PARTIAL};
        u64 effects=cpu()->effects;
        struct q33_block *b=produce(base()+0xe020,0x28e000+number()*0x20000,1,80,flags[variant]);
        if(variant<5)check(!b&&cpu()->effects==effects,36);else check(b&&run_block(b,10)==17+cpu()->repetition,37);
        struct q33_block *fresh=produce(base()+0xe020,0x28e000+number()*0x20000,1,80,0);
        check(fresh&&run_block(fresh,20)==27+cpu()->repetition,38);
    }else if(number()==(target+1)%Q33->ncpus&&variant==5){
        wait_word(&Q33->cpu[target].waiting,1);check(Q33->cpu[target].partial==32,39);
        E(Q33_CONTROL,5,target,Q33->cpu[target].partial,Q33->cpu[target].effects,0,0,0);
        for(unsigned step=0;step<16;step++)poll();ST(&Q33->cpu[target].release,1);
    }
}
static void fetch_case(unsigned variant){
    u64 rip=base()+0xe020,page=0x28e000+number()*0x20000,requests=cpu()->requests;
    if(variant){
        q31_map(0xe000,variant==1?0:(7UL|(1UL<<63)));arch()->rip=rip;q31_begin(QI_FETCH,1,6);
        struct q31_access_result r=q31_memory(rip,Q_FETCH8,1,0,7);
        check(r.status==14&&r.error==(variant==1?16UL:17UL)&&cpu()->requests==requests,40);
        check(q31_raise(14,r.error,r.address)==14,41);q31_map(0xe000,7);q31_return(14,1);cpu()->faults++;
    }
    struct q33_block *b=produce(rip,page,1,80,0);check(b&&run_block(b,5)==12+cpu()->repetition,42);
    write_ok(rip,0x078b48);b=produce(rip,page,2,80,0);check(b!=0,54);
    arch()->gpr[QR_RDI]=base()+0xd000;q31_map(0xd000,0);
    check(run_block(b,5)==0x123000+number(),55);
}
static void source_change_case(unsigned remote,u64 target){
    u64 rip=0x1000e020;
    if(number()==target){
        write_ok(rip,remote?0x078b48:0x078948);
        if(!remote)write_ok(rip+32,source_word(7));
        struct q33_block *b=produce(rip,0x28e000,remote?2:3,80,0);check(b!=0,43);
        arch()->gpr[QR_RDI]=remote?base()+0xd000:rip+32;
        if(remote)q31_map(0xd000,0);
        u64 value=remote?1:source_word(60+target);
        check(run_block(b,value)==(remote?0x123000+number():value),44);
        q31_code_serialize(rip);u64 effects=cpu()->effects;arch()->rip=rip;arch()->gpr[QR_RAX]=1;
        check(execute(b,1)==~0UL&&cpu()->effects==effects,45);
        struct q33_block *fresh=produce(rip+(remote?0:32),0x28e000,1,80,0);
        check(fresh&&run_block(fresh,1)==61+target,46);
    }else if(remote&&number()==(target+1)%Q33->ncpus){
        wait_word(&Q33->cpu[target].waiting,1);write_ok(rip,source_word(60+target));ST(&Q33->cpu[target].release,1);
    }
}
static void budget_case(unsigned variant){
    u64 page=0x28e000+number()*0x20000;
    if(variant==1){
        struct q33_block *old=produce(base()+0xe020,page,1,80,0);check(old!=0,58);
        q30_barrier();cpu()->allocation_limit=LD(&Q33->allocated);q30_barrier();
        u64 allocated=Q33->allocated,bytes=Q33->pool_bytes;
        struct q33_block *b=produce(base()+0xe020,page,1,80,0);check(!b,47);
        q30_barrier();check(Q33->allocated==allocated&&Q33->pool_bytes==bytes,59);
        check(run_block(old,1)==8+cpu()->repetition,60);
        cpu()->allocation_limit=Q33_OBJECTS;return;
    }
    struct q33_block *b=produce(base()+0xe020,page,1,80,QF_DELAY);
    if(variant)check(!b,48);else check(b&&run_block(b,1)==8+cpu()->repetition,49);
}
void q33_run(u64 id){
    check(id==number()&&id<MAX_CPUS&&Q33->magic==Q33_MAGIC&&Q33->ncpus==Q32->ncpus&&Q32->phase==2,50);
    cpu()->started=1;cpu()->timer_start=q30_timer_count();
    E(Q33_CONTROL,7,R(ctr_el0),Q33_POOL,Q33_POOL_END,Q33_OBJECTS,Q33_MAX_BYTES,0);
    q30_barrier();if(!id)ST(&Q33->phase,1);q30_barrier();
    static const unsigned variants[]={2,2,2,2,3,3,6,3,2,3};u64 serial=0;
    for(unsigned group=1;group<=10;group++)for(unsigned rep=0;rep<Q33_REPETITIONS;rep++)
        for(unsigned variant=0;variant<variants[group-1];variant++){
            ++serial;cpu()->group=group;cpu()->variant=variant;cpu()->repetition=rep;
            q30_barrier();if(!id)E(Q33_CASE,group,variant,rep,1,serial,Q33->ncpus,0);q30_barrier();
            prepare_case(serial);
            if(group<=2||group==4)simple_case(group,variant);
            else if(group==3)chain_case(variant);
            else if(group==8)fetch_case(variant);
            else if(group==10)budget_case(variant);
            else for(u64 target=0;target<Q33->ncpus;target++){
                cpu()->waiting=cpu()->release=0;q30_barrier();
                if(group==5)stale_case(variant,target);
                else if(group==6)interrupt_case(variant,target);
                else if(group==7)malformed_case(variant,target);
                else source_change_case(variant,target);
                q30_barrier();
            }
            q30_barrier();q30_code_track(0x28e000+id*0x20000,0);q30_barrier();
            check(!cpu()->depth&&!cpu()->transport_active&&!Q31->cpu[id].depth,51);
            q31_state_snapshot(QS_PROGRESS);cpu()->cases++;
            E(Q33_END,serial,group,variant,rep,cpu()->entries,cpu()->effects,q30_timer_count());
            q30_barrier();if(!id){Q33->cases++;E(Q33_CASE,group,variant,rep,0,serial,Q33->ncpus,0);}q30_barrier();
        }
    cpu()->timer_end=q30_timer_count();cpu()->done=1;
    q30_barrier();if(!id)ST(&Q33->phase,2);q30_barrier();
}

// Called after Q34 has imported its real context and reached this source RIP.
u64 q34_continuation(u64 id,u64 input,u64 operand){
    check(id==number()&&id<MAX_CPUS&&Q33->phase==2,67);
    u64 rip=arch()->rip,source=0;
    q31_begin(QI_FETCH,1,6);
    struct q31_access_result fetch=q31_memory(rip,Q_FETCH8,1,0,7);check(!fetch.status,68);
    u64 page=Q30->cpu[id].request.gpa&~4095UL;
    cpu()->group=11;cpu()->variant=Q34->variant;cpu()->repetition=Q34->repetition;cpu()->allocation_limit=Q33_OBJECTS;
    check(source_read(rip,page,1,&source)&&(source&0xffffffffffffUL)==source_word(operand),69);
    q30_code_track(page,1);
    struct q33_block *b=produce(rip,page,1,80,0);check(b!=0,70);
    u64 result=run_block(b,input);check(result==input+operand,71);
    q30_code_track(page,0);return result;
}
