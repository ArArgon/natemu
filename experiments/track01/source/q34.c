#include "q34.h"
#ifdef Q30_FIXTURE_CHECK
extern u64 q30_fixture_cpu(void);
extern void q34_fixture_poll(void);
#define CPU_ID() q30_fixture_cpu()
#define NATIVE_POLL() q34_fixture_poll()
#else
static u64 native_cpu(void){u64 n;__asm__ volatile("mrs %0,tpidr_el1":"=r"(n));return n;}
#define CPU_ID() native_cpu()
#define NATIVE_POLL() __asm__ volatile("yield":::"memory")
#endif
#define LD(p) __atomic_load_n((p),__ATOMIC_ACQUIRE)
#define ST(p,v) __atomic_store_n((p),(v),__ATOMIC_RELEASE)
#define E(type,...) QLOG(Q34_RECORD_KIND,(type),__VA_ARGS__)
#define PE 1UL
#define PG (1UL<<31)
#define PAE (1UL<<5)
#define LA57 (1UL<<12)
#define LME (1UL<<8)
#define LMA (1UL<<10)
#define NX (1UL<<11)
#define CR0_STATE 0x80050033UL
#define TRAMP 0x301000UL
#define STACK_TOP 0x302ff0UL
#define HEADER 0x303000UL
#define TR_LOCK (HEADER+0x40)
#define GDT (HEADER+0x100)
#define BOOT4 0x308000UL
#define BOOT5 0x309000UL
#define TR_PGD 0x30a000UL
#define KERNEL_PGD 0x30b000UL
#define LOW_PDPT 0x30c000UL
#define LOW_PD 0x30d000UL
#define HIGH_PDPT 0x30e000UL
#define HIGH_PD 0x30f000UL
#define K(v) (Q34_KERNEL_BASE+(v))
static u64 id(void){return CPU_ID();}
static struct q34_cpu *self(void){return &Q34->cpu[id()];}
static struct q34_arch *a(void){return &self()->arch;}
static void check(int ok,u64 reason){if(!ok){Q34->failure=reason;self()->failures++;}q30_check(ok,0x3400+reason);}
static void poll(void){q30_poll();NATIVE_POLL();}
static void zero(void *p,u64 n){volatile unsigned char *b=p;while(n--)*b++=0;}
static void io_enter(void){while(__atomic_exchange_n(&Q34->io_lock,1,__ATOMIC_ACQUIRE))poll();}
static void io_leave(void){ST(&Q34->io_lock,0);}
static unsigned char *physical(u64 pa,unsigned width){
    check(pa>=Q34_RAM_BASE&&pa-Q34_RAM_BASE<=Q34_RAM_BYTES-width,1);
    return Q34->memory+(pa-Q34_RAM_BASE);
}
static u64 raw_read(u64 pa,unsigned width){
    unsigned char *p=physical(pa,width);
    if(pa&(width-1)){u64 value=0;for(unsigned i=0;i<width;i++)value|=(u64)__atomic_load_n(p+i,__ATOMIC_ACQUIRE)<<(i*8);return value;}
    if(width==2)return __atomic_load_n((unsigned short *)p,__ATOMIC_ACQUIRE);
    if(width==4)return __atomic_load_n((u32 *)p,__ATOMIC_ACQUIRE);
    if(width==8)return __atomic_load_n((u64 *)p,__ATOMIC_ACQUIRE);
    check(width==1,3);return __atomic_load_n(p,__ATOMIC_ACQUIRE);
}
static void raw_write(u64 pa,unsigned width,u64 value){
    unsigned char *p=physical(pa,width);
    if(pa&(width-1)){for(unsigned i=0;i<width;i++)__atomic_store_n(p+i,(unsigned char)(value>>(i*8)),__ATOMIC_RELEASE);return;}
    if(width==2)__atomic_store_n((unsigned short *)p,(unsigned short)value,__ATOMIC_RELEASE);
    else if(width==4)__atomic_store_n((u32 *)p,(u32)value,__ATOMIC_RELEASE);
    else if(width==8)__atomic_store_n((u64 *)p,value,__ATOMIC_RELEASE);
    else {check(width==1,5);__atomic_store_n(p,(unsigned char)value,__ATOMIC_RELEASE);}
}
static u64 physical_event(u64 pa,unsigned width,int write,u64 value){
    io_enter();u64 old=raw_read(pa,width);
    if(write)raw_write(pa,width,value);
    u64 out=raw_read(pa,width);
    E(Q34_MEMORY,self()->steps,pa,pa,width,write,old,out);io_leave();return out;
}
// This is a narrow startup walker over actual guest-owned backing. It is not
// Q30's mirror walker, and never changes the native arm64 translation regime.
static u64 translate(u64 va,unsigned width,int write){
    struct q34_arch *s=a();
    if(!(s->cr0&PG)){check(s->mode==Q34_PROTECTED32,6);return (u32)va;}
    check((s->cr4&PAE)&&(s->efer&LMA),7);
    unsigned levels=(s->cr4&LA57)?5:4,bits=levels==5?57:48;
    u64 high=va>>bits,sign=(va>>(bits-1))&1;
    check(high==(sign?((1UL<<(64-bits))-1):0),8);
    u64 table=s->cr3&~4095UL;
    for(unsigned level=levels;level;level--){
        unsigned shift=12+9*(level-1);u64 slot=table+((va>>shift)&511)*8;
        io_enter();u64 entry=raw_read(slot,8);check(entry&1,9);
        check(!write||(entry&2),10);
        int leaf=level==1||((entry&128)&&level==2);
        check(!(entry&128)||level==2,11);
        u64 updated=entry|32|(leaf&&write?64:0);raw_write(slot,8,updated);
        E(Q34_MEMORY,self()->steps,slot,slot,8,1,entry,updated);io_leave();
        if(leaf){u64 mask=(1UL<<shift)-1,pa=(entry&0x000ffffffffff000UL&~mask)|(va&mask);
            check((va&mask)+width<=mask+1,12);return pa;}
        table=entry&0x000ffffffffff000UL;
    }
    check(0,13);return 0;
}
static u64 startup_memory(u64 va,unsigned width,int write,u64 value){
    u64 pa=translate(va,width,write);io_enter();u64 old=raw_read(pa,width);
    if(write)raw_write(pa,width,value);
    u64 out=raw_read(pa,width);E(Q34_MEMORY,self()->steps,va,pa,width,write,old,out);io_leave();return out;
}
static void mode(void){
    check(a()->cr0&PE,14);
    if(!(a()->cr0&PG))a()->mode=Q34_PROTECTED32;
    else a()->mode=a()->cs==16?Q34_LONG64:Q34_COMPAT32;
}
static void snapshot(void){
    const u64 *p=(const u64 *)a();
    for(u64 chunk=0;chunk<9;chunk++)E(Q34_STATE,Q34->serial,self()->steps,chunk,p[4*chunk],p[4*chunk+1],p[4*chunk+2],p[4*chunk+3]);
}
static void step_to(u64 op,u64 width,u64 dst,u64 operand,u64 result,u64 next){
    u64 pc=a()->rip;check(++self()->steps<=Q34_MAX_STEPS,15);
    if(a()->mode==Q34_LONG64)self()->long_steps++;
    else if(a()->mode==Q34_COMPAT32)self()->compat_steps++;
    else {check(a()->mode==Q34_PROTECTED32,16);self()->protected_steps++;}
    E(Q34_STEP,self()->steps,op,width,dst,operand,result,pc);
    a()->rip=next;snapshot();poll();
}
static void step(u64 op,u64 width,u64 dst,u64 operand,u64 result){step_to(op,width,dst,operand,result,a()->rip+4);}
static u64 mask(unsigned width){return width==8?~0UL:(1UL<<(width*8))-1;}
static void mov(unsigned reg,u64 value,unsigned width){
    u64 old=a()->gpr[reg];
    a()->gpr[reg]=width==2?(old&~65535UL)|(value&65535):value&mask(width);
    step(Q34_MOV,width,reg,value,a()->gpr[reg]);
}
static void logic(u64 op,unsigned reg,u64 value,unsigned width){
    u64 x=a()->gpr[reg]&mask(width),v=value&mask(width),out=op==Q34_XOR?x^v:op==Q34_AND?x&v:x|v;
    a()->gpr[reg]=out;u64 parity=out&255;parity^=parity>>4;parity^=parity>>2;parity^=parity>>1;
    a()->flags=(a()->flags&~0x8c5UL)|((!(parity&1))<<2)|((!out)<<6)|(((out>>(width*8-1))&1)<<7);
    step(op,width,reg,value,out);
}
static void compare(u64 left,u64 right,unsigned width,u64 op){
    u64 m=mask(width),l=left&m,r=right&m,out=op==Q34_TEST?l&r:(l-r)&m;
    u64 parity=out&255;parity^=parity>>4;parity^=parity>>2;parity^=parity>>1;
    u64 flags=((!(parity&1))<<2)|((!out)<<6)|(((out>>(width*8-1))&1)<<7);
    if(op==Q34_CMP)flags|=(l<r)|((l^r^out)&16)|((((l^r)&(l^out))>>(width*8-1)&1)<<11);
    a()->flags=(a()->flags&~0x8d5UL)|flags;step(op,width,left,right,out);
}
static void arithmetic(unsigned reg,u64 value,unsigned width,int increment){
    u64 old=a()->gpr[reg]&mask(width),rhs=value&mask(width),sum=(old+rhs)&mask(width);
    u64 cf=a()->flags&1;
    u64 parity=sum&255;parity^=parity>>4;parity^=parity>>2;parity^=parity>>1;
    a()->flags=(a()->flags&~0x8d5UL)|(sum<old)|((!(parity&1))<<2)|((old^rhs^sum)&16)|
        ((!sum)<<6)|(((sum>>(width*8-1))&1)<<7)|(((~(old^rhs)&(old^sum))>>(width*8-1)&1)<<11);
    if(increment)a()->flags=(a()->flags&~1UL)|cf;
    a()->gpr[reg]=sum;step(increment?Q34_INC:Q34_ADD,width,reg,value,sum);
}
static void add(unsigned reg,u64 value,unsigned width){arithmetic(reg,value,width,0);}
static int branch(int condition,u64 identity){step(Q34_JCC,0,identity,condition,condition);return condition;}
static void load(unsigned reg,u64 va,unsigned width){u64 value=startup_memory(va,width,0,0);a()->gpr[reg]=value;step(Q34_LOAD,width,reg,va,value);}
static void rdcr(unsigned cr,unsigned reg){u64 v=cr==0?a()->cr0:cr==3?a()->cr3:a()->cr4;a()->gpr[reg]=v;step(Q34_RDCR,8,reg,cr,v);}
static void wrcr(unsigned cr,u64 value){
    if(cr==0){check((value&PE)&&(!((a()->cr0^value)&PG)||a()->cs!=16),17);a()->cr0=value;
        if(value&PG){check((a()->cr4&PAE)&&(a()->efer&LME),18);a()->efer|=LMA;}else a()->efer&=~LMA;
    }else if(cr==3){check(!(value&4095),19);a()->cr3=value;}
    else {check(cr==4&&(!(a()->cr0&PG)||!((a()->cr4^value)&LA57)),20);a()->cr4=value;}
    a()->tlb_generation++;mode();step(Q34_WRCR,a()->mode==Q34_LONG64?8:4,cr,value,value);
}
static void segment(unsigned which,u64 selector){
    u64 desc=startup_memory(a()->gdtr_base+(selector&~7UL),8,0,0);
    check(selector==24&&(desc&0xffffffffff00ffffUL)==(0x00cf93000000ffffUL&0xffffffffff00ffffUL),21);
    u64 *p=which==0?&a()->ss:which==1?&a()->ds:which==2?&a()->es:which==3?&a()->fs:&a()->gs;
    *p=selector;step(Q34_SEG,2,which,selector,selector);
}
static void far_to(u64 selector,u64 pc,u64 op){
    u64 desc=startup_memory(a()->gdtr_base+(selector&~7UL),8,0,0);
    check(selector==8||selector==16,22);check((desc&(1UL<<47))!=0,23);
    check(((desc>>53)&1)==(selector==16)&&((desc>>54)&1)==(selector==8),24);
    check(selector!=16||(a()->efer&LMA),25);a()->cs=selector;mode();
    step_to(op,op==Q34_LRET?8:4,selector,pc,pc,pc);
}
static void rd_efer(void){
    check(a()->gpr[QR_RCX]==0xc0000080UL,26);a()->gpr[QR_RAX]=(u32)a()->efer;a()->gpr[QR_RDX]=(u32)(a()->efer>>32);
    self()->efer_reads++;step(Q34_RDMSR,8,a()->gpr[QR_RCX],0,a()->efer);
}
static void wr_efer(void){
    u64 v=(a()->gpr[QR_RDX]<<32)|(u32)a()->gpr[QR_RAX];
    check(a()->gpr[QR_RCX]==0xc0000080UL&&!(a()->cr0&PG)&&!(v&LMA)&&(v&LME),27);
    a()->efer=v;self()->efer_writes++;step(Q34_WRMSR,8,a()->gpr[QR_RCX],v,v);
}
static void take_lock(void){
    for(;;){
        check(++self()->lock_attempts<=MAX_CPUS+1,28);
        u64 pa=translate(TR_LOCK,4,1);io_enter();
        u32 old=__atomic_fetch_or((u32 *)physical(pa,4),1,__ATOMIC_ACQ_REL);
        u64 carry=old&1;a()->flags=(a()->flags&~1UL)|carry;
        if(!carry){check(!Q34->lock_owner,29);ST(&Q34->lock_owner,id()+1);ST(&self()->lock_acquired,1);}
        else __atomic_fetch_or(&Q34->contention,1UL<<id(),__ATOMIC_RELEASE);
        E(Q34_LOCK,self()->steps+1,1,old,old|1,carry,Q34->lock_owner,raw_read(TR_LOCK+4,4));io_leave();
        step(Q34_BTS,4,TR_LOCK,0,old|1);
        if(branch(!carry,1))break;
        step(Q34_PAUSE,0,0,0,0);
        // Fixture scheduling suspension after the source PAUSE; no invented
        // guest instruction or fabricated successful BTS while another owns it.
        while(raw_read(TR_LOCK,4)&1)poll();step(Q34_JMP,0,0,1,1);
    }
    mov(QR_RSP,STACK_TOP-Q34_RAM_BASE,4);
    u64 first=(Q34->publisher+1)%Q34->ncpus;
    if(Q34->ncpus==4&&id()==first){
        E(Q34_CONTROL,1,id(),Q34->lock_owner,a()->gpr[QR_RSP],Q34->serial,0,0);
        while(!LD(&Q34->contention))poll();
        E(Q34_CONTROL,2,id(),LD(&Q34->contention),Q34->lock_owner,Q34->serial,0,0);
    }
}
static void release_lock(void){
    check(self()->stack_private&&Q34->lock_owner==id()+1,30);
    u64 va=a()->gpr[QR_RAX],pa=translate(va,4,1);check(pa==TR_LOCK,53);
    io_enter();u64 old=raw_read(pa,4);check(old==1&&raw_read(TR_LOCK+4,4)==0x89abcdefUL,31);
    E(Q34_LOCK,self()->steps+1,2,old,0,0,id()+1,raw_read(TR_LOCK+4,4));
    ST(&Q34->lock_owner,0);raw_write(pa,4,0);io_leave();
    step(Q34_STORE,4,va,0,0);
}
static void data_segments(void){
    segment(0,(unsigned short)a()->gpr[QR_RDX]);add(QR_RSP,Q34_RAM_BASE,4);
    for(unsigned seg=1;seg<5;seg++)segment(seg,(unsigned short)a()->gpr[QR_RDX]);
}
static void protected_start(void){
    data_segments();u64 flags=startup_memory(HEADER+20,4,0,0);
    a()->flags=(a()->flags&~1UL)|(flags&1);step(Q34_BT,4,HEADER+20,0,flags&1);
    check(branch(!(a()->flags&1),2),32); // Actual CPU profile: SME inactive.
    load(QR_RAX,HEADER+16,4);wrcr(4,a()->gpr[QR_RAX]);
    mov(QR_RAX,TR_PGD,4);wrcr(3,a()->gpr[QR_RAX]);
    mov(QR_RCX,0xc0000080UL,4);rd_efer();
    u64 low=startup_memory(HEADER+8,4,0,0);compare(a()->gpr[QR_RAX],low,4,Q34_CMP);
    int write=branch(!(a()->flags&64),3);
    if(!write){u64 high=startup_memory(HEADER+12,4,0,0);compare(a()->gpr[QR_RDX],high,4,Q34_CMP);write=!branch((a()->flags&64)!=0,4);}
    if(write){load(QR_RAX,HEADER+8,4);load(QR_RDX,HEADER+12,4);wr_efer();}
    mov(QR_RAX,CR0_STATE,4);wrcr(0,a()->gpr[QR_RAX]);far_to(16,TRAMP+0x700,Q34_FAR);
    u64 start=startup_memory(HEADER,8,0,0);step_to(Q34_JMP,8,HEADER,start,start,start);
}
static void start64(int debian,int match){
    a()->idtr_limit=startup_memory(HEADER+0x120,2,0,0);a()->idtr_base=startup_memory(HEADER+0x122,8,0,0);step(Q34_LIDT,10,HEADER+0x120,a()->idtr_base,a()->idtr_limit);
    a()->gdtr_limit=startup_memory(HEADER+0x130,2,0,0);a()->gdtr_base=startup_memory(HEADER+0x132,8,0,0);step(Q34_LGDT,10,HEADER+0x130,a()->gdtr_base,a()->gdtr_limit);
    if(debian){
        rdcr(4,QR_RAX);logic(Q34_XOR,QR_RAX,startup_memory(HEADER+16,4,0,0),4);
        compare(a()->gpr[QR_RAX],LA57,4,Q34_TEST);
        int mismatch=branch(!(a()->flags&64),5);check(mismatch==!match,33);
        if(!mismatch){
            take_lock();mov(QR_RDX,24,2);data_segments();mov(QR_RAX,TR_PGD,4);wrcr(3,a()->gpr[QR_RAX]);
            a()->gpr[QR_RSP]-=8;startup_memory(a()->gpr[QR_RSP],8,1,16);step(Q34_PUSH,8,QR_RSP,16,a()->gpr[QR_RSP]);
            u64 start=startup_memory(HEADER,8,0,0);a()->gpr[QR_RSP]-=8;startup_memory(a()->gpr[QR_RSP],8,1,start);step(Q34_PUSH,8,QR_RSP,start,a()->gpr[QR_RSP]);
            u64 pc=startup_memory(a()->gpr[QR_RSP],8,0,0),cs=startup_memory(a()->gpr[QR_RSP]+8,8,0,0);a()->gpr[QR_RSP]+=16;
            far_to(cs,pc,Q34_LRET);return;
        }
    }
    u64 target=startup_memory(HEADER+0x140,4,0,0),selector=startup_memory(HEADER+0x144,2,0,0);
    far_to(selector,target,Q34_FAR);take_lock();mov(QR_RDX,24,2);
    mov(QR_RAX,CR0_STATE&~PG,4);wrcr(0,a()->gpr[QR_RAX]);far_to(8,TRAMP+0x300,Q34_FAR);
    protected_start();
}
static void head64(int debian){
    // Finite verify_cpu call contract: Intel profile, long mode/PAE/NX present,
    // SME absent. This source-indexed CALL uses an assumed successful callee
    // return. The returned EAX/DI and defined XOR flags are modeled below;
    // CPUID's RBX/RCX/RDX outputs are unconstrained contract values, named by
    // CONTROL13, and overwritten by following source before use.
    // The general CPUID/vendor sanitization routine is not executed here.
    check((a()->efer&NX)&&a()->mode==Q34_LONG64&&Q34->lock_owner==id()+1,34);
    u64 return_pc=a()->rip+4;a()->gpr[QR_RSP]-=8;startup_memory(a()->gpr[QR_RSP],8,1,return_pc);
    u64 old_di=a()->gpr[QR_RDI];
    a()->gpr[QR_RAX]=0;a()->gpr[QR_RDI]&=~65535UL;
    a()->flags=(a()->flags&~0x8c5UL)|0x44; // XOR EAX,EAX; AF remains unspecified.
    E(Q34_CONTROL,13,(1UL<<QR_RBX)|(1UL<<QR_RCX)|(1UL<<QR_RDX),16,0,old_di,a()->gpr[QR_RDI],Q34->serial);
    step(Q34_CALL_VERIFY,8,0,return_pc,1);
    u64 resumed=startup_memory(a()->gpr[QR_RSP],8,0,0);a()->gpr[QR_RSP]+=8;
    step_to(Q34_JMP,8,QR_RSP,resumed,resumed,resumed);
    logic(Q34_XOR,15,a()->gpr[15],debian?4:8);
    if(debian){load(QR_RAX,K(HEADER+0x50),8);add(QR_RAX,KERNEL_PGD,8);add(QR_RAX,startup_memory(K(HEADER+0x58),8,0,0),8);wrcr(3,a()->gpr[QR_RAX]);
        mov(QR_RDX,PAE|LA57,4);logic(Q34_OR,QR_RDX,1UL<<6,4);rdcr(4,QR_RCX);logic(Q34_AND,QR_RCX,a()->gpr[QR_RDX],4);
        a()->flags=(a()->flags&~1UL)|((a()->gpr[QR_RCX]>>4)&1);a()->gpr[QR_RCX]|=1UL<<4;step(Q34_BTS,4,QR_RCX,4,a()->gpr[QR_RCX]);wrcr(4,a()->gpr[QR_RCX]);
        a()->flags=(a()->flags&~1UL)|((a()->gpr[QR_RCX]>>7)&1);a()->gpr[QR_RCX]|=1UL<<7;step(Q34_BTS,4,QR_RCX,7,a()->gpr[QR_RCX]);wrcr(4,a()->gpr[QR_RCX]);
    }else{
        load(QR_RAX,K(HEADER+0x58),8);add(QR_RAX,KERNEL_PGD,8);rdcr(4,QR_RCX);logic(Q34_AND,QR_RCX,1UL<<6,4);logic(Q34_OR,QR_RCX,PAE|16|128,4);
        compare(startup_memory(K(HEADER+0x60),1,0,0),1,1,Q34_TEST);check(branch((a()->flags&64)!=0,6),35);
        wrcr(4,a()->gpr[QR_RCX]);add(QR_RAX,startup_memory(K(HEADER+0x50),8,0,0),8);wrcr(3,a()->gpr[QR_RAX]);
        rdcr(4,QR_RCX);mov(QR_RAX,a()->gpr[QR_RCX],8);logic(Q34_XOR,QR_RCX,128,8);wrcr(4,a()->gpr[QR_RCX]);wrcr(4,a()->gpr[QR_RAX]);
        mov(QR_RAX,K(TRAMP+0x900),8);step_to(Q34_JMP,8,QR_RAX,a()->gpr[QR_RAX],a()->gpr[QR_RAX],a()->gpr[QR_RAX]);
    }
    // Parallel-boot APIC-ID branch, finite x2APIC-already-enabled profile.
    load(QR_RCX,K(HEADER+0x64),4);compare(a()->gpr[QR_RCX],1UL<<31,4,Q34_TEST);check(branch(!(a()->flags&64),7),36);
    mov(QR_RCX,0x1b,4);a()->gpr[QR_RAX]=0xc00;a()->gpr[QR_RDX]=0;step(Q34_RDMSR,8,0x1b,0,0xc00);
    compare(a()->gpr[QR_RAX],1UL<<10,4,Q34_TEST);check(branch(!(a()->flags&64),8),37);
    mov(QR_RCX,0x802,4);a()->gpr[QR_RAX]=(u32)self()->cached_id;a()->gpr[QR_RDX]=0;step(Q34_RDMSR,8,0x802,0,a()->gpr[QR_RAX]);
    logic(Q34_XOR,QR_RCX,a()->gpr[QR_RCX],debian?4:8);mov(QR_RBX,K(HEADER+0x80),8);
    for(;;){
        compare(a()->gpr[QR_RAX],startup_memory(a()->gpr[QR_RBX]+a()->gpr[QR_RCX]*4,4,0,0),4,Q34_CMP);
        if(branch((a()->flags&64)!=0,9))break;
        // INC preserves CF, unlike ADD. Retain it explicitly in this source-indexed operation.
        arithmetic(QR_RCX,1,4,1);
        compare(a()->gpr[QR_RCX],startup_memory(K(HEADER+0x70),4,0,0),4,Q34_CMP);check(branch((a()->flags&1)!=0,10),38);
    }
    load(QR_RDX,K(HEADER+0xa0)+a()->gpr[QR_RCX]*8,8);
    load(QR_RAX,K(HEADER+0xc0)+a()->gpr[QR_RDX],8);
    load(QR_RSP,a()->gpr[QR_RAX],8);
    check(a()->gpr[QR_RSP]==K(0x304ff0+id()*4096),39);self()->stack_private=1;
    E(Q34_CONTROL,3,id(),a()->gpr[QR_RSP],Q34->lock_owner,a()->cr3,a()->mode,Q34->serial);
    load(QR_RAX,K(HEADER+0x68),8);compare(a()->gpr[QR_RAX],a()->gpr[QR_RAX],8,Q34_TEST);check(!branch((a()->flags&64)!=0,11),40);
    release_lock();check(a()->mode==Q34_LONG64&&!(a()->flags&512),41);step(Q34_READY,8,id(),a()->gpr[QR_RSP],1);ST(&self()->ready,1);
}
static void startup(void){
    int variant=(int)Q34->variant,debian=variant>=2&&variant<=4,match=variant==4;
    int write=variant==1||variant==3;struct q34_arch *s=a();zero(s,sizeof(*s));
    for(unsigned i=0;i<16;i++)s->gpr[i]=0xa500000000000000UL+id()*256+i;
    s->rip=self()->cached_vector;s->flags=2;s->cr0=CR0_STATE;s->cr4=PAE|(variant==2||variant==3?LA57:0);
    s->cr3=(s->cr4&LA57)?BOOT5:BOOT4;s->efer=LME|LMA|(write?0:NX);s->cs=16;s->ss=s->ds=s->es=24;s->mode=Q34_LONG64;
    E(Q34_CONTROL,4,id(),self()->cached_vector,s->cr0,s->cr3,s->cr4,s->efer);snapshot();
    start64(debian,match);head64(debian);
    check((self()->protected_steps==0)==match,42);check(self()->efer_writes==(u64)write&&self()->efer_reads==(u64)!match,43);
}
static void initialize(u64 serial,u64 variant,u64 rep,u64 publisher){
    Q34->serial=serial;Q34->variant=variant;Q34->repetition=rep;Q34->publisher=publisher;
    Q34->request_sequence=Q34->request_count=Q34->finish=Q34->lock_owner=Q34->contention=0;
    Q34->diagnostic_request=Q34->diagnostic_ack=Q34->diagnostic_reason=Q34->diagnostic_cpu=0;
    Q34->responders=variant==Q34_NO_RESPONDER?0:((1UL<<Q34->ncpus)-1)&~(1UL<<publisher);
    E(Q34_CASE,variant,rep,1,serial,publisher,Q34->ncpus,Q34->responders);
    zero(Q34->memory,sizeof(Q34->memory));
    for(u64 cpu=0;cpu<Q34->ncpus;cpu++){u64 cases=Q34->cpu[cpu].cases;zero(&Q34->cpu[cpu],sizeof(Q34->cpu[cpu]));Q34->cpu[cpu].cases=cases;}
    const struct {u64 pa,value;} setup[]={
        {HEADER,K(TRAMP+0x800)},{HEADER+8,LME|NX},{HEADER+16,PAE},
        {TR_LOCK,0x89abcdef00000000UL},{GDT+8,0x00cf9b000000ffffUL},{GDT+16,0x00af9b000000ffffUL},{GDT+24,0x00cf93000000ffffUL},
        {BOOT4,LOW_PDPT|3},{BOOT4+511*8,HIGH_PDPT|3},{BOOT5,BOOT4|3},{BOOT5+511*8,BOOT4|3},
        {TR_PGD,LOW_PDPT|3},{TR_PGD+511*8,HIGH_PDPT|3},{KERNEL_PGD+511*8,HIGH_PDPT|3},
        {LOW_PDPT,LOW_PD|3},{LOW_PD+8,0x200000|0x83},{HIGH_PDPT+510*8,HIGH_PD|3},{HIGH_PD+8,0x200000|0x83},
        {HEADER+0x68,K(TR_LOCK)}
    };
    for(u64 i=0;i<sizeof(setup)/sizeof(setup[0]);i++)physical_event(setup[i].pa,8,1,setup[i].value);
    physical_event(HEADER+0x130,2,1,31);physical_event(HEADER+0x132,8,1,GDT);
    physical_event(HEADER+0x140,4,1,TRAMP+0x100);physical_event(HEADER+0x144,2,1,8);
    physical_event(HEADER+0x64,4,1,1UL<<31);
    physical_event(HEADER+0x70,4,1,Q34->ncpus);
    for(u64 cpu=0;cpu<Q34->ncpus;cpu++){
        physical_event(HEADER+0x80+cpu*4,4,1,0x100+cpu);
        physical_event(HEADER+0xa0+cpu*8,8,1,cpu*8);
        physical_event(HEADER+0xc0+cpu*8,8,1,K(HEADER+0xe0+cpu*8));
        physical_event(HEADER+0xe0+cpu*8,8,1,K(0x304ff0+cpu*4096));
        physical_event(0x304ff0+cpu*4096,8,1,0x3405000000000000UL|(serial<<8)|cpu);
    }
}
static void publish_mail(u64 target){
    io_enter();check(!raw_read(Q34_RAM_BASE,2),44);u64 seq=Q34->request_sequence+1,vector=TRAMP+(seq&15)*16;ST(&Q34->request_sequence,seq);
    u64 old=raw_read(Q34_RAM_BASE+4,4);raw_write(Q34_RAM_BASE+4,4,target);E(Q34_MEMORY,self()->steps,Q34_RAM_BASE+4,Q34_RAM_BASE+4,4,1,old,target);
    old=raw_read(Q34_RAM_BASE+8,8);raw_write(Q34_RAM_BASE+8,8,vector);E(Q34_MEMORY,self()->steps,Q34_RAM_BASE+8,Q34_RAM_BASE+8,8,1,old,vector);
    E(Q34_MAIL,Q34_MAIL_PUBLISH,seq,target,vector,1,0,Q34->serial);
    raw_write(Q34_RAM_BASE,2,1);Q34->request_count++;io_leave();
}
static int consume(void){
    io_enter();if(raw_read(Q34_RAM_BASE,2)!=1){io_leave();return 0;}u64 seq=LD(&Q34->request_sequence);
    if(self()->seen==seq){io_leave();return 0;}
    u64 target=raw_read(Q34_RAM_BASE+4,4),vector=raw_read(Q34_RAM_BASE+8,8);
    if(target!=0x100+id()||!(Q34->responders&(1UL<<id()))||self()->booted){
        self()->ignored++;E(Q34_MAIL,Q34_MAIL_IGNORE,seq,target,vector,1,self()->booted,Q34->serial);ST(&self()->seen,seq);io_leave();return 0;
    }
    self()->cached_id=target;self()->cached_vector=vector;self()->booted=1;
    E(Q34_MAIL,Q34_MAIL_CACHE,seq,target,vector,1,1,Q34->serial);
    // Cache precedes ACK; ACK trace precedes the release-store so the next OS
    // publication cannot appear before the prior cache/ack evidence.
    E(Q34_MAIL,Q34_MAIL_ACK,seq,target,vector,0,1,Q34->serial);
    raw_write(Q34_RAM_BASE,2,0);self()->acknowledged++;ST(&self()->seen,seq);io_leave();return 1;
}
static void diagnostic(u64 reason){
    Q34->diagnostic_reason=reason;Q34->diagnostic_cpu=id();
    E(Q34_MAIL,Q34_MAIL_TIMEOUT,Q34->request_sequence,raw_read(Q34_RAM_BASE+4,4),raw_read(Q34_RAM_BASE+8,8),raw_read(Q34_RAM_BASE,2),0,Q34->serial);
    ST(&Q34->diagnostic_request,Q34->serial);
    // External observer must retain command1/target/vector/case and explicitly
    // acknowledge termination of this cold startup context. No guest ACK/wake.
    while(LD(&Q34->diagnostic_ack)!=Q34->serial)poll();
    check(raw_read(Q34_RAM_BASE,2)==1,45);
    E(Q34_CONTROL,5,reason,Q34->diagnostic_request,Q34->diagnostic_ack,1,Q34->serial,0);
}
#define RUNTIME_PT 0x3a0000UL
static u64 runtime_backing(u64 cpu){return 0x320000+cpu*0x20000;}
static unsigned char *runtime_memory(void){
#ifdef Q30_FIXTURE_CHECK
    extern unsigned char *q30_fixture_memory;return q30_fixture_memory;
#else
    return (unsigned char *)X86_RAM_IPA;
#endif
}
static u64 runtime_word(u64 pa){u64 out=0;for(unsigned i=0;i<8;i++)out|=(u64)runtime_memory()[pa+i]<<(8*i);return out;}
static void project(void){
    // All startup interpreters have finished. Copy only differing words through
    // the existing retained Q30 setup path; no live table is written behind it.
    struct q30_patch patches[256];u64 count=0,total=0;
    for(u64 off=0;off<Q34_RAM_BYTES;off+=8){
        u64 value=raw_read(Q34_RAM_BASE+off,8);
        if(runtime_word(Q34_RAM_BASE+off)!=value){check(++total<=Q34_PROJECT_MAX,64);patches[count++]=(struct q30_patch){Q34_RAM_BASE+off,value};}
        if(count==256){q30_setup_batch(patches,count);count=0;}
    }
    if(count)q30_setup_batch(patches,count);
    for(u64 off=0;off<Q34_RAM_BYTES;off+=8)check(runtime_word(Q34_RAM_BASE+off)==raw_read(Q34_RAM_BASE+off,8),55);
    E(Q34_CONTROL,8,Q34_RAM_BASE,Q34_RAM_BYTES,total,KERNEL_PGD,Q34->serial,0);
}
static void import_context(void){
    struct q31_arch g={0};for(unsigned i=0;i<16;i++)g.gpr[i]=a()->gpr[i];
    g.rip=a()->rip;g.flags=a()->flags;g.cs=a()->cs;g.ss=a()->ss;
    // CR2 is not written by startup. Keep the established runtime's value.
    g.cr2=Q31->cpu[id()].arch.cr2;
    struct x86_context context={a()->cr3,a()->cs&3,!!(a()->cr0&(1UL<<16)),!!(a()->efer&NX),!!(a()->cr4&(1UL<<7))};
    check(a()->mode==Q34_LONG64&&!(a()->cr4&LA57)&&context.cr3==KERNEL_PGD,56);
    E(Q34_CONTROL,9,a()->rip,g.gpr[QR_RSP],context.cr3,g.cs,g.ss,Q34->serial);
    q31_import_context(context,K(runtime_backing(id())),runtime_backing(id()),0x340000+Q34->serial,&g);
    // Diagnostic physical-continuity probe, not retirement of a made-up MOV at
    // the startup source label. It leaves every architectural word unchanged.
    q31_begin(QI_IMPORT,8,1);
    struct q31_access_result r=q31_memory(g.gpr[QR_RSP],Q_LOAD,8,0,6);
    u64 pa=g.gpr[QR_RSP]-Q34_KERNEL_BASE;
    check(!r.status&&Q30->cpu[id()].request.gpa==pa&&r.value==raw_read(pa,8),57);
    E(Q34_CONTROL,10,g.gpr[QR_RSP],pa,r.value,Q30->cpu[id()].request.operation,context.cr3,Q34->serial);
    q31_state_snapshot(QS_PROGRESS);
}
static u64 gate_word(u64 handler){return (handler&65535)|(16UL<<16)|(0x8eUL<<40)|(((handler>>16)&65535)<<48);}
static void runtime_setup(void){
    // Split exactly the imported 2MiB mapping, preserving its physical identity.
    // The only aliases below are explicit fixture runtime data mappings.
    struct q30_patch p[256];u64 n=0;
    for(u64 page=0;page<512;page++){
        p[n++]=(struct q30_patch){RUNTIME_PT+page*8,0x200000+page*4096+3};
        if(n==256){q30_setup_batch(p,n);n=0;}
    }
    for(u64 cpu=0;cpu<Q34->ncpus;cpu++){
        u64 b=runtime_backing(cpu),v=K(b);
#define P(pa,val) (p[n++]=(struct q30_patch){(pa),(val)})
        P(RUNTIME_PT+(((b+0x18000)>>12)&511)*8,RUNTIME_PT|3);
        P(b+0x10008,0x00cf9b000000ffffUL);P(b+0x10010,0x00af9b000000ffffUL);P(b+0x10018,0x00cf93000000ffffUL);
        const u64 vectors[]={14,0x41,0x4f,0x71};
        for(unsigned j=0;j<4;j++){
            u64 h=v+(vectors[j]==14?0x3100:0x3400);
            P(b+0x1000+vectors[j]*16,gate_word(h));P(b+0x1008+vectors[j]*16,h>>32);
        }
        P(b+0x3400,0x5beb078948UL);P(b+0x3460,0x078948);P(b+0xc100,0);
        P(b+0xd000,0x3400000000000000UL|Q34->serial*16|cpu);
        unsigned char bytes[32]={0x48,0x89,0xe7,0x48,0x8b,0x07,0x48,0xbf};
        u64 target=v+0xd000;for(unsigned i=0;i<8;i++)bytes[8+i]=(unsigned char)(target>>(8*i));
        bytes[16]=0x48;bytes[17]=0x8b;bytes[18]=0x07;bytes[19]=0x48;bytes[20]=0x05;
        bytes[21]=(unsigned char)(17+cpu);bytes[25]=0xfb;bytes[26]=0x90;
        for(unsigned j=0;j<4;j++){u64 word=0;for(unsigned i=0;i<8;i++)word|=(u64)bytes[j*8+i]<<(8*i);P(b+0x3500+j*8,word);}
        check(n<=256,58);
#undef P
    }
    q30_setup_batch(p,n);q30_setup(HIGH_PD+8,RUNTIME_PT|3,8);
    E(Q34_CONTROL,11,HIGH_PD+8,RUNTIME_PT,KERNEL_PGD,0x200000,0x200000,Q34->serial);
}
static void fetch_bytes(u64 expected,unsigned length){
    struct q31_arch *g=&Q31->cpu[id()].arch;
    for(unsigned i=0;i<length;i++){
        struct q31_access_result r=q31_memory(g->rip+i,Q_FETCH8,1,0,7);
        check(!r.status&&r.value==((expected>>(8*i))&255),59);
    }
}
static void continuation(void){
    struct q31_arch *g=&Q31->cpu[id()].arch;u64 b=runtime_backing(id()),v=K(b),old=g->rip;
    // Explicit post-startup fixture descriptor installation: cached CS/SS and
    // original RSP stay intact; Q31's descriptor helper now reads these bases.
    E(Q34_CONTROL,12,a()->gdtr_base,v+0x10000,a()->idtr_base,v+0x1000,g->cs,g->ss);
    a()->gdtr_base=v+0x10000;a()->gdtr_limit=31;a()->idtr_base=v+0x1000;a()->idtr_limit=4095;
    step_to(Q34_JMP,8,old,v+0x3500,v+0x3500,v+0x3500);
    g->rip=a()->rip;q31_state_snapshot(QS_PROGRESS);
    q31_begin(QI_MOV_RDI,8,3);fetch_bytes(0xe78948,3);g->gpr[QR_RDI]=g->gpr[QR_RSP];q31_retire();
    q31_begin(QI_LOAD,8,3);fetch_bytes(0x078b48,3);
    struct q31_access_result r=q31_memory(g->gpr[QR_RDI],Q_LOAD,8,0,1);
    check(!r.status&&Q30->cpu[id()].request.gpa==a()->gpr[QR_RSP]-Q34_KERNEL_BASE,60);
    g->gpr[QR_RAX]=r.value;q31_retire();
    q31_begin(QI_MOV_RDI,8,10);fetch_bytes(0xbf48,2);
    u64 immediate=0;for(unsigned i=0;i<8;i++){
        r=q31_memory(g->rip+2+i,Q_FETCH8,1,0,7);check(!r.status,61);immediate|=r.value<<(8*i);
    }
    check(immediate==v+0xd000,62);g->gpr[QR_RDI]=immediate;q31_retire();
    q31_map(0xd000,0);q31_begin(QI_LOAD,8,3);fetch_bytes(0x078b48,3);
    r=q31_memory(g->gpr[QR_RDI],Q_LOAD,8,0,1);
    check(r.status==14&&r.error==0,46);check(q31_raise(14,r.error,r.address)==14,47);
    q31_map(0xd000,3);q31_return(14,1);q31_begin(QI_LOAD,8,3);fetch_bytes(0x078b48,3);
    r=q31_memory(g->gpr[QR_RDI],Q_LOAD,8,0,1);check(!r.status,48);g->gpr[QR_RAX]=r.value;q31_retire();
    u64 input=r.value,operand=17+id();self()->input=input;
    E(Q34_CONTINUE,1,input,operand,0,a()->mode,g->gpr[QR_RSP],self()->cached_vector);
    u64 result=q34_continuation(id(),input,operand);
    check(result==input+operand&&g->gpr[QR_RAX]==result,49);self()->result=result;self()->continuations++;
    E(Q34_CONTINUE,2,input,operand,result,a()->mode,g->gpr[QR_RSP],self()->cached_vector);
    q32_enable_interrupts();q32_receive(Q32_TIMER,Q32_INTERIOR,0x41);q32_service();
    check(g->gpr[QR_RAX]==result&&g->gpr[QR_RSP]==a()->gpr[QR_RSP]&&g->cs==16&&g->ss==24&&Q30->cpu[id()].context.cr3==KERNEL_PGD,50);
    q31_state_snapshot(QS_PROGRESS);
}
static void publisher_progress(void){
    struct q31_arch *g=&Q31->cpu[id()].arch;u64 v=0x10000000+id()*0x20000;
    u64 owner=LD(&Q34->lock_owner);
    E(Q34_CONTROL,6,Q34->request_sequence,owner,Q30->epoch,Q30->cpu[id()].operations,0,0);
    q30_invalidate(v+0xc000,0x34);g->rip=v+0x3000;g->gpr[QR_RDI]=v+0xc000;q31_begin(QI_LOAD,8,3);
    struct q31_access_result r=q31_memory(g->gpr[QR_RDI],Q_LOAD,8,0,1);check(!r.status,54);
    g->gpr[QR_RAX]=r.value;q31_retire();q32_receive(Q32_TIMER,Q32_INTERIOR,0x41);q32_service();
    E(Q34_CONTROL,7,Q34->request_sequence,LD(&Q34->lock_owner),Q30->epoch,Q30->cpu[id()].request.operation,Q30->cpu[id()].request.faults,r.value);
}
static void case_body(void){
    u64 pub=Q34->publisher,variant=Q34->variant;
    if(id()==pub){
        if(variant==Q34_WRONG_ID||variant==Q34_NO_RESPONDER){
            publish_mail(variant==Q34_WRONG_ID?0xffffffffUL:0x100+(pub+1)%Q34->ncpus);
            for(u64 cpu=0;cpu<Q34->ncpus;cpu++)if(cpu!=pub)while(LD(&Q34->cpu[cpu].seen)!=Q34->request_sequence)poll();
            diagnostic(variant);ST(&Q34->finish,1);
        }else{
            for(u64 n=1;n<Q34->ncpus;n++){
                u64 target=(pub+n)%Q34->ncpus;publish_mail(0x100+target);
                while(raw_read(Q34_RAM_BASE,2))poll();
                if(n==1&&Q34->ncpus==4)while(!LD(&Q34->cpu[target].lock_acquired))poll();
                publisher_progress();
            }
            for(u64 cpu=0;cpu<Q34->ncpus;cpu++)if(cpu!=pub)while(!LD(&Q34->cpu[cpu].ready))poll();
            if(variant==Q34_BOOT_ONCE){
                publish_mail(0x100+(pub+1)%Q34->ncpus);
                for(u64 cpu=0;cpu<Q34->ncpus;cpu++)if(cpu!=pub)while(LD(&Q34->cpu[cpu].seen)!=Q34->request_sequence)poll();
                diagnostic(variant);
            }
            ST(&Q34->finish,1);
        }
    }else{
        while(!LD(&Q34->finish)){
            if(consume())startup();poll();
        }
    }
    q30_barrier();
    if(!id())project();q30_barrier();
    if(self()->ready)import_context();q30_barrier();
    if(!id())runtime_setup();q30_barrier();
    if(self()->ready)continuation();q30_barrier();
}
void q34_run(u64 cpu){
    check(cpu<MAX_CPUS&&cpu==id(),51);q30_barrier();
    if(!cpu){check(Q34->magic==Q34_MAGIC&&Q34->version==Q34_VERSION&&Q34->ncpus==Q30->ncpus,63);ST(&Q34->phase,1);}
    q30_barrier();
    for(u64 rep=0;rep<Q34_REPETITIONS;rep++)for(u64 variant=0;variant<Q34_VARIANTS;variant++){
        u64 serial=rep*Q34_VARIANTS+variant+1;
        if(!cpu)initialize(serial,variant,rep,rep%Q34->ncpus);q30_barrier();
        self()->timer_start=q30_timer_count();q32_reuse_fixture(0x340000+serial);q30_barrier();
        case_body();q31_state_snapshot(QS_PROGRESS);self()->cases++;self()->timer_end=q30_timer_count();
        check(!Q34->lock_owner&&!raw_read(TR_LOCK,4)&&raw_read(TR_LOCK+4,4)==0x89abcdefUL,52);
        E(Q34_END,serial,variant,rep,self()->booted,self()->ready,self()->steps,self()->timer_end);q30_barrier();
        if(!cpu){E(Q34_CASE,variant,rep,0,serial,Q34->publisher,Q34->ncpus,Q34->responders);Q34->cases++;}q30_barrier();
    }
    if(!cpu)ST(&Q34->phase,2);q30_barrier();
}
