#include "q31.h"
#ifdef Q30_FIXTURE_CHECK
extern u64 q30_fixture_cpu(void);
extern struct shared *q31_fixture_shared;
char q30_load_site[1],q30_load8_site[1],q30_store_site[1],q30_byte_site[1];
char q30_cas_load[1],q30_cas_store[1];
#define S q31_fixture_shared
#else
#define S ((volatile struct shared *)SHARED_PA)
#endif
#define NX (1UL << 63)
#define RF (1UL << 16)
#define LOAD(p) __atomic_load_n((p), __ATOMIC_ACQUIRE)
#define STORE(p,v) __atomic_store_n((p), (v), __ATOMIC_RELEASE)
#define E(kind,...) QLOG(QE_Q31, (kind), __VA_ARGS__)
__attribute__((weak)) void q32_hook(u64 stage) { (void)stage; }

enum { A_READ=1, A_WRITE, A_DESCRIPTOR, A_FRAME, A_IRET, A_REPAIR, A_FETCH,
       A_REP_READ, A_REP_WRITE };
#define access_result q31_access_result
struct local {
    u64 serial, substep, root, backing, base, vector, frame;
    u64 pinned_page, pinned_generation, pinned, pinned_va;
    int access_live;
};
static struct local local[MAX_CPUS];
static u64 cpu_id(void) {
#ifdef Q30_FIXTURE_CHECK
    return q30_fixture_cpu();
#else
    u64 cpu; __asm__ volatile("mrs %0, tpidr_el1" : "=r"(cpu)); return cpu;
#endif
}
static struct q31_cpu *self(void) { return &Q31->cpu[cpu_id()]; }
static struct local *here(void) { return &local[cpu_id()]; }
static u64 metadata_generation(void) {
    return here()->pinned && (self()->arch.rip&~4095UL)==here()->pinned_va ? here()->pinned_generation:1;
}
static void check(int value, u64 reason) {
    if (!value) Q31->failure = reason;
    q30_check(value, 0x3100 + reason);
}
static void snapshot(u64 point) {
    struct q31_cpu *c = self();
    const u64 *words = (const u64 *)&c->arch;
    u64 number = ++c->snapshots;
    for (u64 chunk=0; chunk<7; ++chunk)
        E(Q31_STATE, number, point | (c->depth << 8), c->current_attempt, chunk,
          words[chunk*3], words[chunk*3+1], words[chunk*3+2]);
}
static void context(u64 cpl) {
    struct x86_context c = Q30->cpu[cpu_id()].context;
    if (c.cr3 == here()->root && c.cpl == cpl) return;
    c.cr3=here()->root; c.cpl=cpl; c.wp=1; c.nxe=1; c.pge=0;
    q30_context(c, here()->serial);
}
static void attempt(u64 instruction, u64 width, u64 length) {
    struct q31_cpu *c=self();
    c->current_attempt=++c->attempts;
    c->metadata=(struct q31_metadata){c->arch.rip,metadata_generation(),c->arch.rip,length,instruction,0,width,0};
    here()->substep=0;
    E(Q31_ATTEMPT,c->current_attempt,c->metadata.identity,c->arch.rip,
      instruction,length,width,c->metadata.generation);
    snapshot(QS_BEFORE);
}
static struct access_result memory(u64 address,unsigned kind,unsigned width,u64 value,unsigned action) {
    struct q31_cpu *c=self();
    struct local *l=here();
    c->metadata.identity=c->arch.rip;
    c->metadata.rip=c->arch.rip;
    c->metadata.access=kind; c->metadata.width=width;
    c->metadata.substep=++l->substep;
    c->metadata.generation=metadata_generation();
    // MARK1 supplies the complete access-site recipe before a native operation.
    // Architectural GPRs/flags are canonical in c->arch; C temporaries are uncommitted.
    E(Q31_MARK,1,c->current_attempt,c->arch.rip,c->metadata.instruction,
      c->metadata.substep,kind | ((u64)width << 8),c->metadata.generation);
    l->access_live=1;
    u64 result=q30_access(address,kind,width,value,0);
    l->access_live=0;
    struct q30_request *r=&Q30->cpu[cpu_id()].request;
    struct access_result out={result,r->status,r->error,q30_fault_address()};
    E(Q31_STEP,c->current_attempt,action | (l->substep << 8),r->status,r->error,
      r->operation,c->commits,result);
    return out;
}
static int valid_metadata(u64 cpu,u64 pc) {
    if(cpu>=MAX_CPUS)return 0;
    struct q31_cpu *c=&Q31->cpu[cpu];
    struct local *l=&local[cpu];
    extern char q30_load_site[],q30_load8_site[],q30_store_site[],q30_byte_site[];
    extern char q30_cas_load[],q30_cas_store[];
    struct q30_request *r=&Q30->cpu[cpu].request;
    int known=(r->kind==Q_LOAD&&pc==(u64)(r->width==1?q30_load8_site:q30_load_site)) ||
              (r->kind==Q_FETCH8&&pc==(u64)q30_load8_site) ||
              (r->kind==Q_STORE&&pc==(u64)(r->width==1?q30_byte_site:q30_store_site)) ||
              (r->kind==Q_CMPXCHG&&(pc==(u64)q30_cas_load||pc==(u64)q30_cas_store)) ||
              (r->kind==Q_EXEC&&pc==((r->gva>>63)?r->gva:r->gva|(1UL<<47)));
    return known && l->access_live && c->started &&
           c->metadata.identity==c->arch.rip && c->metadata.rip==c->arch.rip &&
           c->metadata.length && c->metadata.substep==l->substep &&
           c->metadata.access==Q30->cpu[cpu].request.kind &&
           c->metadata.width==Q30->cpu[cpu].request.width &&
           c->metadata.generation==(l->pinned && (c->arch.rip&~4095UL)==l->pinned_va ? l->pinned_generation:1);
}
int q31_validate_fault(u64 cpu,u64 pc) { return valid_metadata(cpu,pc); }
static u64 read_ok(u64 va,unsigned action) {
    struct access_result r=memory(va,Q_LOAD,8,0,action); check(!r.status,1); return r.value;
}
static void write_ok(u64 va,u64 value,unsigned action) {
    struct access_result r=memory(va,Q_STORE,8,value,action); check(!r.status,2);
}
static u64 leaf_address(u64 va) {
    return here()->base+0x18000+((va>>12)&511)*8;
}
static void mapping(u64 offset,u64 flags) {
    context(0);
    write_ok(leaf_address(here()->base+offset),(here()->backing+offset)|flags,A_REPAIR);
    q30_invalidate(here()->base+offset,0x31);
}
static u64 gate_low(u64 handler,u64 selector,u64 ist,u64 type) {
    return (handler&65535)|(selector<<16)|(ist<<32)|((0x80|type)<<40)|(((handler>>16)&65535)<<48);
}
static void gate(u64 vector,u64 handler,u64 selector,u64 ist,u64 type) {
    context(0);
    write_ok(here()->base+0x1000+vector*16,gate_low(handler,selector,ist,type),A_REPAIR);
    write_ok(here()->base+0x1008+vector*16,handler>>32,A_REPAIR);
}
static void fixture(u64 serial,unsigned group,unsigned variant) {
    u64 id=cpu_id(); struct local *l=here(); struct q31_cpu *c=self();
    check(!l->pinned && !c->depth,3);
    *l=(struct local){.serial=serial,.root=0x250000+id*0x4000,
        .backing=0x280000+id*0x20000,.base=0x10000000+id*0x20000};
    u64 v=l->base,b=l->backing,r=l->root;
    struct q30_patch p[96]; u64 count=0;
#define P(address,value) (p[count++]=(struct q30_patch){(address),(value)})
    // Initial page sentinels are the only nonzero unused slots; zero both
    // boundaries, then overwrite every slot used by this fixed root layout.
    for(u64 page=0;page<4;page++){P(r+page*4096,0);P(r+page*4096+4088,0);}
    P(r,l->root+0x1000+7); P(r+0x1000,l->root+0x2000+7);
    P(r+0x2000+((v>>21)&511)*8,l->root+0x3000+7);
    for(u64 page=0;page<32;page++) P(r+0x3000+(((v>>12)&511)+page)*8,b+page*4096+7);
    P(r+0x3000+(((v>>12)&511)+0x18)*8,r+0x3000+7);
    P(r+0x3000+(((v>>12)&511)+0x19)*8,b+0xc000+7);
    P(r+0x3000+(((v>>12)&511)+0x1a)*8,0x253000+((id+1)%Q30->ncpus)*0x4000+7);
    P(r+0x3000+(((v>>12)&511)+0x1b)*8,0x28c000+7);
    P(r+0x3000+(((v>>12)&511)+0x1c)*8,0x4000+7);
    P(r+0x3000+(((v>>12)&511)+0x1d)*8,0x5000+7);
    P(r+0x3000+(((v>>12)&511)+0x1e)*8,b+0xd000+7);
    P(b+0x10008,0x00af9b000000ffffUL); P(b+0x10010,0x00cf93000000ffffUL);
    P(b+0x10018,0x00affb000000ffffUL); P(b+0x10020,0x00cff3000000ffffUL);
    u64 tss=v+0x2000;
    P(b+0x10028,0x67|((tss&0xffffff)<<16)|(0x8bUL<<40)|((tss&0xff000000)<<32));
    P(b+0x10030,tss>>32); P(b+0x10038,0x00af9b000000ffffUL);
    P(b+0x11008,0x00af9b000000ffffUL);
    u64 rsp=v+0x7ff7,ist1=v+0x9ffb,ist2=v+0xbffb;
    P(b+0x2000,(rsp&0xffffffff)<<32); P(b+0x2008,rsp>>32);
    P(b+0x2020,(ist1&0xffffffff)<<32);
    P(b+0x2028,(ist1>>32)|((ist2&0xffffffff)<<32)); P(b+0x2030,ist2>>32);
    P(b+0x2060,0x68UL<<48);
    u64 instruction=0x078b48; // MOV RAX,[RDI], three bytes.
    if((group==2&&variant==1)||(group==3&&(variant==0||variant==2))||group==6||
       (group==8&&(id&1))||(group==9&&(variant==1||(variant>=6&&variant!=9))))instruction=0x078948;
    if(group==5)instruction=0x07c10f48; // XADD [RDI],RAX, four bytes.
    if(group==7)instruction=0xa4f3; // REP MOVSB, two bytes.
    P(b+0x3000,instruction);P(b+0x2ff0,0x078948);P(b+0x3110,0x078b48);
    P(b+0x3010,0xa20f); // CPUID, immutable synchronization site.
    const u64 vectors[]={8,13,14};
    for(unsigned i=0;i<3;i++) {
        u64 vector=vectors[i],handler=v+(vector==8?0x3300:vector==13?0x3200:0x3100);
        P(b+0x1000+vector*16,gate_low(handler,vector==8?0x38:8,vector==8,14));
        P(b+0x1008+vector*16,handler>>32);
    }
    for(u64 offset=0xc000;offset<=0xc080;offset+=8) P(b+offset,0x600000+id+offset);
    P(b+0xcff8,0x123456789abcdef0UL); P(b+0xd000,0x0fedcba987654321UL);
    check(count<=96,4); q30_setup_batch(p,count);
#undef P
    context(0);
    for(unsigned i=0;i<16;i++) c->arch.gpr[i]=0x700000+id*256+i;
    c->arch.gpr[QR_RSP]=v+0x5ff9; c->arch.rip=v+0x3000;
    c->arch.flags=0x202;c->arch.cs=8;c->arch.ss=0x10;c->arch.cr2=0xdecaf000+id;
    c->metadata=(struct q31_metadata){v+0x3000,1,v+0x3000,1,QI_LOAD,0,8,0};
    c->current_attempt=0;
}
static int contributory(u64 vector) { return vector==0 || (vector>=10 && vector<=13); }
static u64 deliver(u64 vector,u64 error,u64 address);
static u64 delivery_fault(u64 first,u64 second,u64 error,u64 address,u64 phase) {
    struct q31_cpu *c=self();
    E(Q31_DELIVERY_FAULT,c->current_attempt,first,second,error,address,phase,c->depth);
    if(second==14)c->arch.cr2=address;
    check(first!=8,5); // Triple fault is excluded, never fabricated as recoverable.
    u64 next=(first==14 && (second==14 || contributory(second))) ||
             (contributory(first)&&contributory(second)) ? 8:second;
    return deliver(next,next==8?0:error,address);
}
static u64 deliver_kind(u64 vector,u64 error,u64 address,int has_error,int is_fault) {
    struct q31_cpu *c=self();struct local *l=here();
    struct q31_arch old=c->arch;
    if(vector==14)c->arch.cr2=address;
    snapshot(QS_FAULT);
    q32_hook(4); // Q32 may observe a native IRQ during actual exception work.
    context(0); // Implicit descriptor/TSS and inner-level stack accesses.
    u64 location=l->base+0x1000+16*vector;
    struct access_result low=memory(location,Q_LOAD,8,0,A_DESCRIPTOR);
    if(low.status)return delivery_fault(vector,low.status,low.error,low.address,1);
    struct access_result high=memory(location+8,Q_LOAD,8,0,A_DESCRIPTOR);
    if(high.status)return delivery_fault(vector,high.status,high.error,high.address,1);
    u64 selector=(low.value>>16)&65535,ist=(low.value>>32)&7,type=(low.value>>40)&15;
    if(!(low.value&(1UL<<47)))return delivery_fault(vector,11,(vector<<3)|3,address,1);
    check(type==14||type==15,6);
    check(selector && (selector&~7UL)+7<=0x103f,7);
    struct access_result desc=memory(l->base+0x10000+(selector&~7UL),Q_LOAD,8,0,A_DESCRIPTOR);
    if(desc.status)return delivery_fault(vector,desc.status,desc.error,desc.address,2);
    if(!(desc.value&(1UL<<47)))return delivery_fault(vector,11,(selector&~3UL)|1,address,2);
    check((desc.value&(1UL<<53)) && !(desc.value&(1UL<<54)) && ((desc.value>>40)&0x18)==0x18,8);
    u64 handler=(low.value&65535)|(((low.value>>48)&65535)<<16)|(high.value<<32);
    check(x86_canonical(handler),9);
    u64 stack=old.gpr[QR_RSP],newss=old.ss;
    if(ist || (old.cs&3)) {
        u64 offset=ist?28+ist*8:4;
        struct access_result tss=memory(l->base+0x2000+offset,Q_LOAD,8,0,A_DESCRIPTOR);
        if(tss.status)return delivery_fault(vector,tss.status,tss.error,tss.address,3);
        stack=tss.value;newss=0;
    }
    check(x86_canonical(stack),10);
    stack&=~15UL;
    u64 frame[]={old.ss,old.gpr[QR_RSP],old.flags|(is_fault&&vector!=8?RF:0),old.cs,old.rip,error};
    for(unsigned i=0;i<(has_error?6U:5U);i++) {
        stack-=8;
        struct access_result push=memory(stack,Q_STORE,8,frame[i],A_FRAME);
        if(push.status)return delivery_fault(vector,push.status,push.error,push.address,4);
    }
    c->arch.gpr[QR_RSP]=stack;c->arch.ss=newss;c->arch.cs=selector&~3UL;c->arch.rip=handler;
    c->arch.flags=old.flags&~((1UL<<8)|RF|(1UL<<14)|(1UL<<17));
    if(type==14)c->arch.flags&=~(1UL<<9);
    c->depth++;c->deliveries++;l->vector=vector;l->frame=stack;
    E(Q31_DELIVER,c->current_attempt,vector,error,c->arch.cr2,handler,stack,c->depth);
    snapshot(QS_HANDLER);
    return vector;
}
static u64 deliver(u64 vector,u64 error,u64 address) { return deliver_kind(vector,error,address,1,1); }
static void iret_kind(u64 vector,int has_error) {
    struct q31_cpu *c=self();u64 stack=c->arch.gpr[QR_RSP];
    check(vector!=8 && c->depth,11);
    u64 first=stack+(has_error?8:0);
    u64 rip=read_ok(first,A_IRET),cs=read_ok(first+8,A_IRET),flags=read_ok(first+16,A_IRET);
    u64 rsp=read_ok(first+24,A_IRET),ss=read_ok(first+32,A_IRET);
    check(x86_canonical(rip)&&x86_canonical(rsp)&&cs && ((cs&3)==0||(cs&3)==3),12);
    u64 code=read_ok(here()->base+0x10000+(cs&~7UL),A_DESCRIPTOR);
    check((code&(1UL<<47))&&(code&(1UL<<53))&&!(code&(1UL<<54))&&((code>>45)&3)==(cs&3),13);
    if(ss&~3UL) {
        u64 data=read_ok(here()->base+0x10000+(ss&~7UL),A_DESCRIPTOR);
        check((data&(1UL<<47))&&((data>>40)&0x1a)==0x12&&((data>>45)&3)==(cs&3)&&(ss&3)==(cs&3),14);
    } else check((cs&3)!=3&&(ss&3)==(cs&3),15);
    c->arch.rip=rip;c->arch.cs=cs;c->arch.flags=flags;c->arch.gpr[QR_RSP]=rsp;c->arch.ss=ss;
    c->depth--;context(cs&3);
    E(Q31_RETURN,c->current_attempt,vector,stack,rip,rsp,c->depth,0);snapshot(QS_RETURNED);
}
static void iret(u64 vector) { iret_kind(vector,1); }
static void fault(struct access_result r,u64 vector,u64 error) {
    check(r.status==vector&&r.error==error,16);
    check(deliver(vector,error,r.address)==vector,17);
}
static void commit(void) {
    self()->commits++;self()->arch.rip+=self()->metadata.length;
    self()->arch.flags&=~RF;snapshot(QS_COMMITTED);
}

static void privilege(u64 cpl) {
    self()->arch.cs=cpl?0x1b:8;self()->arch.ss=cpl?0x23:0x10;context(cpl);
}
static void ordinary(unsigned group,unsigned variant) {
    struct q31_cpu *c=self();u64 v=here()->base,va=v+0xc000;
    unsigned kind=Q_LOAD;u64 error=0,denial=14;
    if(group==1) {
        u64 cr2=c->arch.cr2;
        c->arch.gpr[QR_RDI]=va;
        attempt(QI_LOAD,8,3);struct access_result r=memory(va,Q_LOAD,8,0,A_READ);
        check(!r.status&&c->arch.cr2==cr2,20);c->arch.gpr[QR_RAX]=r.value;commit();return;
    }
    if(group==2||group==8) {
        int user=group==8?(cpu_id()&1):variant;
        mapping(0xd000,0);va=v+0xd000;privilege(user?3:0);kind=user?Q_STORE:Q_LOAD;error=user?6:0;
    } else if(group==3) {
        u64 flags=variant==1?3:5;
        if(variant==3)flags=7|(1UL<<36);
        mapping(0xc000,flags);privilege(variant<2?3:0);
        kind=variant==1||variant==3?Q_LOAD:Q_STORE;
        const u64 errors[]={7,5,3,9};error=errors[variant];
    }
    c->arch.gpr[QR_RDI]=va;
    if(kind==Q_STORE)c->arch.gpr[QR_RAX]=0xabcdef0011223344UL;
    attempt(kind==Q_LOAD?QI_LOAD:QI_STORE,8,3);
    if(group==8)q30_barrier();
    struct access_result r=memory(va,kind,8,0xabcdef0011223344UL,kind==Q_LOAD?A_READ:A_WRITE);
    fault(r,denial,error);
    mapping(va-v,7);iret(14);
    attempt(kind==Q_LOAD?QI_LOAD:QI_STORE,8,3);
    r=memory(va,kind,8,0xabcdef0011223344UL,kind==Q_LOAD?A_READ:A_WRITE);
    check(!r.status,21);if(kind==Q_LOAD)c->arch.gpr[QR_RAX]=r.value;commit();
}
static void fetch_case(unsigned variant) {
    struct q31_cpu *c=self();u64 v=here()->base;
    if(variant==2) {
        c->arch.gpr[QR_RDI]=0x800000000000UL;
        attempt(QI_LOAD,8,3);u64 cr2=c->arch.cr2;
        struct access_result r=memory(0x800000000000UL,Q_LOAD,8,0,A_READ);
        fault(r,13,0);check(c->arch.cr2==cr2,22);iret(13);return;
    }
    u64 start=variant==0?v+0xc000:v+0xcffe,count=3;
    const unsigned char instruction[]={0x48,0x8b,0x07};
    for(unsigned i=0;i<3;i++) {
        struct access_result r=memory(start+i,Q_STORE,1,instruction[i],A_REPAIR);check(!r.status,57);
    }
    if(variant==0)mapping(0xc000,7|NX);
    else mapping(0xd000,0);
    c->arch.rip=start;c->arch.gpr[QR_RDI]=v+0xc080;attempt(QI_FETCH,1,count);
    u64 fetched=0;
    for(u64 i=0;i<count;i++) {
        struct access_result r=memory(start+i,Q_FETCH8,1,0,A_FETCH);
        if(r.status) {
            fault(r,14,variant==0?17:16);
            check(c->arch.cr2==(variant==0?start:v+0xd000),23);
            mapping(variant==0?0xc000:0xd000,7);iret(14);
            attempt(QI_FETCH,1,count);i=~0UL;fetched=0;
        } else fetched|=r.value<<(i*8);
    }
    check(fetched==0x078b48,58);
    struct access_result operand=memory(c->arch.gpr[QR_RDI],Q_LOAD,8,0,A_READ);
    check(!operand.status,59);c->arch.gpr[QR_RAX]=operand.value;commit();
}
static void rmw_case(unsigned variant) {
    struct q31_cpu *c=self();u64 v=here()->base;
    c->arch.rip=v+0x2ff0;c->arch.gpr[QR_RDI]=v+0xc080;c->arch.gpr[QR_RAX]=1;
    attempt(QI_STORE,8,3);write_ok(v+0xc080,1,A_WRITE);commit();
    c->arch.rip=v+0x3000;mapping(0xc000,variant?5:0);
    c->arch.gpr[QR_RAX]=1;c->arch.gpr[QR_RDI]=v+0xc000;
    for(unsigned retry=0;retry<2;retry++) {
        attempt(QI_RMW,8,4);
        u64 oldrax=c->arch.gpr[QR_RAX],oldflags=c->arch.flags;
        struct access_result r=memory(v+0xc000,Q_LOAD,8,0,A_READ);
        if(!r.status) {
            // XADD [RDI],RAX (48 0f c1 07). Keep sum, old memory and flags
            // temporary until the destination store succeeds.
            u64 old=r.value,rhs=c->arch.gpr[QR_RAX],temporary=old+rhs;
            u64 parity=temporary&255;parity^=parity>>4;parity^=parity>>2;parity^=parity>>1;
            u64 temporary_flags=(c->arch.flags&~0x8d5UL)|(temporary<old)|
                ((!(parity&1))<<2)|(((old^rhs^temporary)&16))|
                ((!temporary)<<6)|((temporary>>63)<<7)|
                (((~(old^rhs)&(old^temporary))>>63)<<11);
            r=memory(v+0xc000,Q_STORE,8,temporary,A_WRITE);
            if(!r.status) {c->arch.gpr[QR_RAX]=old;c->arch.flags=temporary_flags;commit();break;}
        }
        check(c->arch.gpr[QR_RAX]==oldrax&&c->arch.flags==oldflags,24);
        fault(r,14,variant?3:0);mapping(0xc000,7);iret(14);
    }
    check(read_ok(v+0xc080,A_REPAIR)==1,25);
}
struct split_words { u64 low,high; };
static struct split_words split_observe(void) {
    u64 v=here()->base;
    struct split_words words={read_ok(v+0x19ff8,A_REPAIR),read_ok(v+0x1e000,A_REPAIR)};
    return words;
}
static void split_check(struct split_words before,int committed) {
    struct split_words after=split_observe();
    u64 low=before.low,high=before.high;
    if(committed) {
        low=(low&0xffffffffUL)|0x5566778800000000UL;
        high=(high&0xffffffff00000000UL)|0x11223344UL;
    }
    check(after.low==low&&after.high==high,69);
}
static void cross_case(unsigned variant) {
    struct q31_cpu *c=self();u64 v=here()->base;
    c->arch.gpr[QR_RDI]=v+0xcffc;c->arch.gpr[QR_RAX]=0x1122334455667788UL;
    if(variant<2) {
        mapping(0xd000,variant?5:0);
        struct split_words before=split_observe();attempt(QI_STORE,8,3);
        struct access_result r=memory(v+0xcffc,Q_STORE,8,0x1122334455667788UL,A_WRITE);
        split_check(before,0);
        fault(r,14,variant?3:2);check(c->arch.cr2==v+0xd000,26);
        mapping(0xd000,7);iret(14);attempt(QI_STORE,8,3);
        r=memory(v+0xcffc,Q_STORE,8,0x1122334455667788UL,A_WRITE);check(!r.status,27);commit();
        split_check(before,1);
    } else {
        q30_barrier();
        for(unsigned round=0;round<4;round++) {
            c->arch.rip=v+0x3000;
            struct split_words before=split_observe();
            u64 peer=(cpu_id()+1)%Q30->ncpus,peer_va=0x10000000+peer*0x20000+0xd000;
            write_ok(v+0x1a000+((peer_va>>12)&511)*8,
                     0x280000+peer*0x20000+0xd000+(round&1?5:7),A_REPAIR);
            attempt(QI_STORE,8,3);
            struct access_result r=memory(v+0xcffc,Q_STORE,8,0x1122334455667788UL,A_WRITE);
            if(r.status) {split_check(before,0);fault(r,14,3);mapping(0xd000,7);iret(14);}
            else {commit();split_check(before,1);}
            // Peers may revoke during the first access. Finish those writes
            // before the bounded restart; the first attempt remains concurrent.
            q30_barrier();
            mapping(0xd000,7);q30_barrier();
            if(r.status) {
                attempt(QI_STORE,8,3);
                r=memory(v+0xcffc,Q_STORE,8,0x1122334455667788UL,A_WRITE);
                check(!r.status,61);commit();split_check(before,1);
            }
            q30_barrier();
        }
        q30_barrier();mapping(0xd000,7);
    }
}
static void rep_case(unsigned variant) {
    struct q31_cpu *c=self();u64 v=here()->base;
    int reverse=variant==2;u64 missing=reverse?0xd000:0xe000;
    mapping(missing,0);c->arch.gpr[QR_RCX]=variant==1?0:8;
    c->arch.gpr[QR_RSI]=v+0xc000+(reverse?7:0);
    c->arch.gpr[QR_RDI]=v+(reverse?0xe003:0xdffc);
    if(reverse)c->arch.flags|=1UL<<10;
    attempt(QI_REP,1,2);
    while(c->arch.gpr[QR_RCX]) {
        struct access_result r=memory(c->arch.gpr[QR_RSI],Q_LOAD,1,0,A_REP_READ);check(!r.status,28);
        r=memory(c->arch.gpr[QR_RDI],Q_STORE,1,r.value,A_REP_WRITE);
        if(r.status) {fault(r,14,2);mapping(missing,7);iret(14);attempt(QI_REP,1,2);continue;}
        c->arch.gpr[QR_RSI]+=reverse?-1UL:1;c->arch.gpr[QR_RDI]+=reverse?-1UL:1;
        c->arch.gpr[QR_RCX]--;snapshot(QS_PROGRESS);
    }
    commit();
}
static void nested_case(unsigned variant) {
    struct q31_cpu *c=self();u64 v=here()->base;
    int user=variant==1||variant>=6;
    mapping(0xd000,0);
    if(variant==2||variant==9) {
        gate(14,v+0x3100,8,1,14);gate(8,v+0x3300,0x38,2,14);
    }
    if(variant==3)gate(14,v+0x3100,8,0,15);
    if(variant==6) {
        write_ok(v+0x2004,v+0x8000,A_REPAIR);mapping(0x7000,0);
    }
    if(variant==7)write_ok(v+0x1000+14*16,gate_low(v+0x3100,8,0,14)&~(1UL<<47),A_REPAIR);
    if(variant==8)write_ok(v+0x10008,0x00af9b000000ffffUL&~(1UL<<47),A_REPAIR);
    if(variant==9) {gate(13,v+0x3200,0x1008,0,14);mapping(0x11000,0);}
    privilege(user?3:0);
    c->arch.flags=variant<5?(variant==4?0x10302:0x302):0x202;
    unsigned kind=user&&variant!=9?Q_STORE:Q_LOAD;
    u64 target=variant==9?0x800000000000UL:v+0xd000;
    c->arch.gpr[QR_RDI]=target;if(kind==Q_STORE)c->arch.gpr[QR_RAX]=0x12345678;
    attempt(kind==Q_STORE?QI_STORE:QI_LOAD,8,3);
    struct access_result r=memory(target,kind,8,0x12345678,kind==Q_STORE?A_WRITE:A_READ);
    check(r.status==(variant==9?13:14)&&r.error==(user&&variant!=9?6:0),29);
    u64 delivered=deliver(r.status,r.error,r.address);
    if(variant>=6&&variant<=8) {
        check(delivered==8,30);
        write_ok(v+0xc078,8,A_WRITE); // A real translated diagnostic-handler store.
        E(Q31_MARK,3,c->current_attempt,8,c->depth,here()->serial,0,0);
        c->depth=0; // End this modeled context; never an IRET/restart from #DF.
        return;
    }
    check(delivered==(variant==9?14:r.status),31);
    if(variant<5) {
        u64 expected_stack=v+(variant==1?0x7fc0:variant==2?0x9fc0:0x5fc0);
        check(c->arch.gpr[QR_RSP]==expected_stack,32);
        check(read_ok(expected_stack+24,A_READ)==0x10302,33);
        check(c->arch.flags==(variant==3?0x202:2),34);
        iret(14);check(c->arch.flags==0x10302&&c->arch.gpr[QR_RSP]==v+0x5ff9,35);return;
    }
    if(variant==5) {
        iret(14);attempt(QI_LOAD,8,3);
        r=memory(v+0xd000,Q_LOAD,8,0,A_READ);fault(r,14,0);
    } else if(variant==9) {
        check(c->arch.gpr[QR_RSP]==v+0x9fc0&&c->arch.cr2==v+0x11008,36);
        mapping(0x11000,7);iret(14);attempt(QI_LOAD,8,3);
        r=memory(target,Q_LOAD,8,0,A_READ);fault(r,13,0);
        check(c->arch.cs==0x1008,37);iret(13);return;
    } else if(variant==10) {
        // The first handler executes this store before its separate faulting load.
        u64 outer_cr2=c->arch.cr2;
        write_ok(v+0xc060,c->arch.gpr[QR_RDI],A_REPAIR);
        write_ok(v+0xc068,c->arch.gpr[QR_RAX],A_REPAIR);
        write_ok(v+0xc070,outer_cr2,A_WRITE);
        c->arch.rip=v+0x3110;c->arch.gpr[QR_RDI]=v+0xe000;
        mapping(0xe000,0);attempt(QI_LOAD,8,3);
        r=memory(v+0xe000,Q_LOAD,8,0,A_READ);fault(r,14,0);
        check(c->depth==2&&c->arch.cr2==v+0xe000,38);
        mapping(0xe000,7);iret(14);
        check(c->arch.rip==v+0x3110&&c->arch.ss==0&&c->depth==1,39);
        attempt(QI_LOAD,8,3);r=memory(v+0xe000,Q_LOAD,8,0,A_READ);
        check(!r.status,60);c->arch.gpr[QR_RAX]=r.value;commit();
        c->arch.gpr[QR_RDI]=read_ok(v+0xc060,A_REPAIR);
        c->arch.gpr[QR_RAX]=read_ok(v+0xc068,A_REPAIR);
        c->arch.cr2=read_ok(v+0xc070,A_READ);snapshot(QS_PROGRESS);
        check(c->arch.cr2==outer_cr2,40);
    }
    mapping(0xd000,7);iret(14);
    attempt(kind==Q_STORE?QI_STORE:QI_LOAD,8,3);
    r=memory(v+0xd000,kind,8,0x12345678,kind==Q_STORE?A_WRITE:A_READ);
    check(!r.status,41);if(kind==Q_LOAD)c->arch.gpr[QR_RAX]=r.value;commit();
}
static void pin_code(u64 va,u64 page) {
    struct local *l=here();check(!l->pinned,42);
    u64 generation=q31_code_generation(page);
    check(q30_code_pin(page,generation),43);
    l->pinned=1;l->pinned_page=page;l->pinned_generation=generation;l->pinned_va=va&~4095UL;
}
static void unpin_code(void) {
    struct local *l=here();check(l->pinned,44);
    q30_code_unpin(l->pinned_page);l->pinned=0;
}
static u64 dispatch_code(u64 va,u64 page) {
    struct q31_cpu *c=self();struct local *l=here();
    c->arch.rip=va;pin_code(va,page);attempt(QI_FETCH,1,5);
    u64 bytes=0;
    for(unsigned i=0;i<5;i++) {
        struct access_result r=memory(va+i,Q_FETCH8,1,0,A_FETCH);check(!r.status,45);
        bytes|=r.value<<(8*i);
    }
    check((bytes&255)==0xb8,46);
    u64 immediate=(bytes>>8)&0xffffffff;
    check(immediate==42||immediate==43,47);
    // Select actual immutable arm64 MOVZ/RET by validated guest MOV EAX bytes.
    struct access_result r=memory(l->base+(immediate==42?0x1c000:0x1d000),Q_EXEC,8,0,A_FETCH);
    check(!r.status&&r.value==immediate,48);c->arch.gpr[QR_RAX]=r.value;commit();
    unpin_code();return r.value;
}
static void serialize_code(u64 va) {
    struct q31_cpu *c=self();
    c->arch.rip=here()->base+0x3010;c->arch.gpr[QR_RAX]=0;c->arch.gpr[QR_RCX]=0;
    attempt(QI_CPUID,1,2);
    struct access_result first=memory(c->arch.rip,Q_FETCH8,1,0,A_FETCH);
    struct access_result second=memory(c->arch.rip+1,Q_FETCH8,1,0,A_FETCH);
    check(!first.status&&!second.status&&first.value==0x0f&&second.value==0xa2,63);
    // Finite CPUID leaf 0: no other leaves advertised; vendor NatemuProbe!.
    // The writer lock/quiescence completes before retirement. The next block
    // must acquire a fresh generation; no old successor crosses this boundary.
    q30_invalidate(va,0x33);
    c->arch.gpr[QR_RAX]=0;c->arch.gpr[QR_RBX]=0x6574614e;
    c->arch.gpr[QR_RDX]=0x7250756d;c->arch.gpr[QR_RCX]=0x2165626f;
    // MARK5: action, input leaf, EAX, EBX, ECX, EDX, reserved zero.
    E(Q31_MARK,5,0,c->arch.gpr[QR_RAX],c->arch.gpr[QR_RBX],
      c->arch.gpr[QR_RCX],c->arch.gpr[QR_RDX],0);
    commit();
}
static void code_case(int remote) {
    struct q31_cpu *c=self();struct local *l=here();u64 v=l->base;
    u64 page=remote?0x28c000:l->backing+0xc000,va=v+(remote?0x1b020:0xc020);
    if(!remote||cpu_id()==0) {
        write_ok(va-16,0x078948,A_REPAIR);
        write_ok(va+16,0x078b48,A_REPAIR);
        write_ok(va,0xc30000002ab8UL,A_REPAIR);
        q30_code_track(page,1);
    }
    q30_barrier();
    check(dispatch_code(va,page)==42,49);
    pin_code(va,page);c->arch.rip=remote&&cpu_id()!=0?va+16:va-16;
    c->arch.gpr[QR_RDI]=remote&&cpu_id()!=0?v+0xd000:va;
    if(!remote||cpu_id()==0)c->arch.gpr[QR_RAX]=0xc30000002bb8UL;
    attempt(remote&&cpu_id()!=0?QI_LOAD:QI_CODE_STORE,8,3);
    q30_barrier();
    if(!remote||cpu_id()==0) {
        write_ok(va,0xc30000002bb8UL,A_WRITE);
        // This store is the current block's last guest operation; do not reenter
        // its stale successor after changing the bytes on the same code page.
        commit();
    }
    // All old pins survive the writer's generation change. Peer faults below
    // therefore recover old metadata after invalidation, independent of timing.
    q30_barrier();
    if(remote&&cpu_id()!=0) {
        mapping(0xd000,0);
        struct access_result r=memory(v+0xd000,Q_LOAD,8,0,A_READ);
        fault(r,14,0);mapping(0xd000,7);iret(14);
        attempt(QI_LOAD,8,3);
        r=memory(v+0xd000,Q_LOAD,8,0,A_READ);
        check(!r.status,62);c->arch.gpr[QR_RAX]=r.value;commit();
    }
    q30_barrier();unpin_code();
    serialize_code(va);q30_barrier();
    check(dispatch_code(va,page)==43,50);
    q30_barrier();if(!remote||cpu_id()==0)q30_code_track(page,0);
    q30_barrier();
}
static void invalid_case(unsigned variant) {
    struct q31_cpu *c=self();struct local *l=here();
    c->arch.gpr[QR_RDI]=l->base+0xc000;attempt(QI_LOAD,8,3);
    struct access_result r=memory(l->base+0xc000,Q_LOAD,8,0,A_READ);check(!r.status,51);
    extern char q30_load_site[];
    l->access_live=1;
    if(variant==0)c->metadata.identity^=8;
    else c->metadata.generation++;
    check(!valid_metadata(cpu_id(),(u64)q30_load_site),52);
    check(!valid_metadata(cpu_id(),1),53);
    E(Q31_MARK,4,c->current_attempt,variant,c->metadata.identity,c->metadata.generation,1,0);
    c->metadata.identity=c->arch.rip;c->metadata.generation=1;l->access_live=0;
    if(variant==1) {
        u64 page=l->backing+0xc000,va=l->base+0xc020,index=page/4096;
        write_ok(va,0xc30000002ab8UL,A_REPAIR);q30_code_track(page,1);
        u64 old=q31_code_generation(page),users=LOAD(&Q31->code_users[index]);
        c->arch.rip=l->base+0x2ff0;c->arch.gpr[QR_RDI]=va;c->arch.gpr[QR_RAX]=0xc30000002bb8UL;
        attempt(QI_STORE,8,3);write_ok(va,c->arch.gpr[QR_RAX],A_WRITE);commit();
        u64 current=q31_code_generation(page);
        check(current>old&&!users,64);
        int stale=q30_code_pin(page,old);
        check(!stale&&LOAD(&Q31->code_users[index])==users,65);
        int fresh=q30_code_pin(page,current);check(fresh,66);q30_code_unpin(page);
        check(LOAD(&Q31->code_users[index])==users,67);
        // MARK6: action, page, old/new generation, stale rejected, fresh
        // accepted, users after unpin. CODE events independently bind lifetime.
        E(Q31_MARK,6,page,old,current,!stale,fresh,LOAD(&Q31->code_users[index]));
        q30_code_track(page,0);
    }
    c->arch.rip=l->base+0x3000;c->arch.gpr[QR_RDI]=l->base+0xc000;
    attempt(QI_LOAD,8,3);
    r=memory(c->arch.gpr[QR_RDI],Q_LOAD,8,0,A_READ);
    check(!r.status,68);c->arch.gpr[QR_RAX]=r.value;commit();
}
void q31_run(u64 cpu) {
    check(cpu==cpu_id()&&Q31->magic==Q31_MAGIC&&Q31->version==Q31_VERSION&&Q31->ncpus==Q30->ncpus,54);
    struct q31_cpu *c=self();c->started=1;c->timer_start=q30_timer_count();
    q30_barrier();if(cpu==0)STORE(&Q31->phase,1);q30_barrier();
    static const unsigned variants[]={1,2,4,3,2,3,3,1,11,1,1,2};
    u64 serial=0;
    for(unsigned group=1;group<=12;group++)for(unsigned rep=0;rep<Q31_REPETITIONS;rep++)
        for(unsigned variant=0;variant<variants[group-1];variant++) {
            ++serial;q30_barrier();fixture(serial,group,variant);q30_barrier();
            if(cpu==0)E(Q31_CASE,group,variant,rep,1,serial,Q31->ncpus,0);
            q30_barrier();
            if(group<=3||group==8)ordinary(group,variant);
            else if(group==4)fetch_case(variant);
            else if(group==5)rmw_case(variant);
            else if(group==6)cross_case(variant);
            else if(group==7)rep_case(variant);
            else if(group==9)nested_case(variant);
            else if(group==10||group==11)code_case(group==11);
            else invalid_case(variant);
            check(!c->depth&&!here()->pinned,55);c->cases++;
            E(Q31_END,serial,group,variant,rep,c->commits,c->deliveries,q30_timer_count());
            q30_barrier();if(cpu==0) {E(Q31_CASE,group,variant,rep,0,serial,Q31->ncpus,0);Q31->cases++;}
            q30_barrier();
        }
    check(c->cases==Q31_VARIANTS*Q31_REPETITIONS,56);
    while(q30_timer_count()<=c->timer_start)q30_poll();
    c->timer_end=q30_timer_count();c->done=1;
    q30_barrier();if(cpu==0)STORE(&Q31->phase,2);q30_barrier();
}
void q31_reuse_fixture(u64 serial) { fixture(serial,1,0); }
void q31_begin(u64 instruction,u64 width,u64 length) { attempt(instruction,width,length); }
struct q31_access_result q31_memory(u64 address,unsigned kind,unsigned width,u64 value,unsigned action) {
    return memory(address,kind,width,value,action);
}
void q31_set_privilege(u64 cpl) { privilege(cpl); }
void q31_map(u64 offset,u64 flags) { mapping(offset,flags); }
void q31_set_gate(u64 vector,u64 handler,u64 selector,u64 ist,u64 type) { gate(vector,handler,selector,ist,type); }
u64 q31_raise(u64 vector,u64 error,u64 address) { return deliver(vector,error,address); }
u64 q31_interrupt(u64 vector,int partial_progress) { check(vector>=32&&vector<=255,70);return deliver_kind(vector,0,0,0,partial_progress); }
void q31_return(u64 vector,int has_error) { iret_kind(vector,has_error); }
void q31_retire(void) { commit(); }
void q31_retire_at(u64 next_rip) { self()->commits++;self()->arch.rip=next_rip;self()->arch.flags&=~RF;snapshot(QS_COMMITTED); }
void q31_state_snapshot(u64 point) { snapshot(point); }
void q31_code_acquire(u64 va,u64 page) { pin_code(va,page); }
int q31_code_acquire_expected(u64 va,u64 page,u64 generation) {
    struct local *l=here();check(!l->pinned,42);
    if(!q30_code_pin(page,generation))return 0;
    l->pinned=1;l->pinned_page=page;l->pinned_generation=generation;l->pinned_va=va&~4095UL;
    return 1;
}
void q31_code_release(void) { unpin_code(); }
u64 q31_code_dispatch(u64 va,u64 page) { return dispatch_code(va,page); }
void q31_code_serialize(u64 va) { serialize_code(va); }

u64 q31_base(void){return here()->base;}
void q31_import_context(struct x86_context context,u64 base,u64 backing,u64 serial,const struct q31_arch *arch){
    struct local *l=here();struct q31_cpu *c=self();
    check(!l->pinned&&!c->depth&&!(context.cr3&4095)&&x86_canonical(base),71);
    *l=(struct local){.serial=serial,.root=context.cr3,.base=base,.backing=backing};
    volatile u64 *to=(volatile u64 *)&c->arch;const u64 *from=(const u64 *)arch;
    for(unsigned i=0;i<sizeof(*arch)/8;i++)to[i]=from[i];
    c->current_attempt=0;
    c->metadata=(struct q31_metadata){arch->rip,1,arch->rip,3,QI_LOAD,0,8,0};
    q30_context(context,serial);snapshot(QS_PROGRESS);
}
