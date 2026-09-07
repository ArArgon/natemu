#ifndef TRACK01_Q33_H
#define TRACK01_Q33_H
#include "q32.h"
#define Q33_PA 0x40e20000UL
#define Q33_MAGIC 0x513333434f444531UL
#define Q33_VERSION 1UL
#define Q33_POOL 0x40400000UL
#define Q33_POOL_END 0x40700000UL
#define Q33_OBJECTS 4096
#define Q33_VARIANTS 28
#define Q33_MAX_BYTES 8192
#define Q33_CHUNK 32
#define Q33_REPETITIONS 16
enum { Q33_CASE=1,Q33_REQUEST,Q33_SEND,Q33_RECEIVE,Q33_ACK,Q33_REJECT,
       Q33_PUBLISH,Q33_ENTER,Q33_EFFECT,Q33_LINK,Q33_CONTROL,Q33_END };
enum { QT_BEGIN=1,QT_REQUEST,QT_PULL,QT_ACK,QT_CANCEL };
enum { QT_OK=0,QT_WAIT=1,QT_DONE=2,QT_BAD=3 };
enum { QF_DELAY=1,QF_STALE=2,QF_DUPLICATE=4,QF_OFFSET=8,QF_LENGTH=16,
       QF_IDENTITY=32,QF_FAILURE=64,QF_PARTIAL=128 };
// HVC36 at one exact helper: X0op,X1txn,X2offset,X3length,X4..7payload
// (four little-endian u64), X8total,X9generation,X10status,X11flags,
// X12group,X13variant,X14repetition,X15cpu. Both directions use this layout.
struct q33_packet { u64 word[16]; };
// A whole immutable request snapshot is exported as three 32-byte chunks.
struct q33_request {
    u64 rip,cr3,page,generation,source,operand,kind,length;
    u64 identity,cpu,serial,nonce;
};
struct q33_block {
    u64 id,address,length,rip,page,generation,source,operand,kind,link;
    u64 enter,finish; // Native callbacks: template LDR offsets80 and88.
    u64 valid,transaction,users,entered;
};
struct q33_cpu {
    u64 started,done,cases,transaction,requests,chunks,acknowledgements,rejections;
    u64 publishes,entries,effects,links,timer_start,timer_end,phase,progress;
    u64 group,variant,repetition,current,depth,faults,interrupts,restarts;
    u64 waiting,release,observed,source_generation,partial,transport_active;
    u64 allocation_limit;
};
struct q33_state {
    u64 magic,version,ncpus,failure,phase,cases,checkpoint_records,checkpoint_epoch;
    u64 allocated,pool_bytes,pool_exhaustions;
    struct q33_cpu cpu[MAX_CPUS];
    struct q33_block blocks[Q33_OBJECTS];
};
_Static_assert(Q33_POOL>=Q30_SHADOW_PA+MAX_CPUS*Q30_SHADOW_PAGES*4096UL,"code overlaps shadows");
_Static_assert(Q33_POOL_END<=SHARED_PA,"code overlaps foundation");
_Static_assert(Q33_PA+sizeof(struct q33_state)<0x40f00000UL,"Q33 overlaps native stacks");
// QE_Q33=15: subtype then seven words. CASE group,variant,rep,start/end,serial,ncpus,0.
// REQUEST txn,rip,page,generation,source,kind,length.
// SEND/RECEIVE txn,offset,length,payload0,payload1,payload2,payload3.
// ACK txn,offset,length,status,total,generation,0.
// REJECT txn,reason,offset,expected,actual,generation,0.
// PUBLISH id,address,length,generation,DC-line,IC-line,transaction.
// ENTER id,actual-entry-PC,actual-callback-LR,input,generation,users,link.
// EFFECT id,actual-callback-LR,input,result,RIP,flags,generation.
// LINK action,source,old-target,new-target,epoch,ack-mask,0.
// CONTROL1 op,txn,offset,epoch,gate,lock: before transfer.
// CONTROL2 op,txn,offset,status,epoch,gate: after copy and polling.
// CONTROL3 txn,destination,length,copied-total,object,0: copied before ACK.
// CONTROL4 object,address,generation,cpu,depth,0: executor ISB before entry.
// CONTROL5 target,partial-bytes,effects,0,0,0: peer sees unpublished partial.
// CONTROL6 allocation-limit,allocated,pool-bytes,requested-length,0,0: quota refusal.
// CONTROL7 CTR_EL0,pool-start,pool-end,object-capacity,max-bytes,0: native configuration.
// CONTROL8 source-sequence,CNTP_CVAL,CNTVCT,txn,offset,0: masked delay window armed.
// CONTROL9 source-sequence,txn,status,native-ELR,native-SPSR,0: copied before service.
// CONTROL10 object,old-epoch,new-epoch,page,expected-generation,current-generation: entry seam.
// CONTROL11 chunk,word0,word1,word2,word3,txn: full delayed response after copy/before handler.
// Variant counts by group: 2,2,2,2,3,3,6,3,2,3; all16 repetitions per CPU.
// END serial,group,variant,rep,entries,effects,timer.
#ifdef Q30_FIXTURE_CHECK
extern struct q33_state *q33_fixture_state;
#define Q33 q33_fixture_state
#else
#define Q33 ((struct q33_state *)Q33_PA)
#endif
void q33_run(u64 cpu);
void q33_checkpoint(void);
void q33_transfer(struct q33_packet *packet);
// Host-only emitter also usable by the explicitly synthetic fixture harness.
// The guest never calls it: it stores response register bytes verbatim.
static inline u64 q33_emit(const struct q33_request *r,u32 *out){
    u64 bytes=r->length,imm=(r->source>>16)&0xffffffffUL;
    if(bytes<80||bytes>Q33_MAX_BYTES||(bytes&3)||r->kind<1||r->kind>3)return 0;
    if(r->kind==1&&((r->source&65535)!=0x0548||imm>4095))return 0;
    if(r->kind==2&&((r->source&0xffffff)!=0x078b48))return 0;
    if(r->kind==3&&((r->source&0xffffff)!=0x078948))return 0;
    if(r->kind!=1)imm=0;
    const u32 begin[]={0xa9be7bfd,0xa90153f3,0x910003fd,0xaa0003f3,
        0xaa0103f4,0xaa1403e0,0x10ffff41,0xaa1303e2,0xf9402a90,0xd63f0200,0xaa0003f3};
    for(u64 i=0;i<bytes/4;i++)out[i]=0xd503201f; // Executed padding, not skipped data.
    for(u64 i=0;i<11;i++)out[i]=begin[i];
    u64 tail=bytes/4-9;out[tail]=0x91000273|((u32)imm<<10);
    out[tail+1]=0xaa1403e0;
    // ADR X1 to the actual block entry, from this second callback site.
    u32 delta=(u32)(-(long)((tail+2)*4))&0x1fffff;
    out[tail+2]=0x10000001|((delta&3)<<29)|((delta>>2)<<5);
    out[tail+3]=0xaa1303e2;out[tail+4]=0xf9402e90;out[tail+5]=0xd63f0200;
    out[tail+6]=0xa94153f3;out[tail+7]=0xa8c27bfd;out[tail+8]=0xd65f03c0;
    return bytes;
}
struct q33_host_transaction {
    u64 active,transaction,received,offered,acknowledged,flags,waited;
    struct q33_request request;
    u32 code[Q33_MAX_BYTES/4];
};
// Pure protocol/emitter, shared by host and explicitly synthetic model.
// Failure leaves the transaction unusable until matching CANCEL; no guest entry implied.
static inline int q33_respond(struct q33_host_transaction *t,unsigned cpu,struct q33_packet *p){
    u64 op=p->word[0],txn=p->word[1],offset=p->word[2],length=p->word[3];
    p->word[10]=QT_BAD;
    if(p->word[15]!=cpu||!txn)return 0;
    if(op==QT_BEGIN){
        if(t->active||offset||length||p->word[8]!=sizeof(struct q33_request))return 0;
        *t=(struct q33_host_transaction){.active=1,.transaction=txn,.flags=p->word[11]};
        p->word[10]=QT_OK;return 1;
    }
    if(!t->active||txn!=t->transaction)return 0;
    if(op==QT_CANCEL){t->active=0;p->word[10]=QT_DONE;return 1;}
    if(op==QT_REQUEST){
        if(offset!=t->received||length!=Q33_CHUNK||offset+length>sizeof(t->request))return 0;
        u64 *words=(u64 *)&t->request;
        for(unsigned i=0;i<4;i++)words[offset/8+i]=p->word[4+i];
        t->received+=length;
        if(t->received==sizeof(t->request)){
            if(t->request.identity!=txn||t->request.cpu!=cpu||t->request.generation!=p->word[9]||
               !q33_emit(&t->request,t->code))return 0;
        }
        p->word[10]=QT_OK;return 1;
    }
    if(t->received!=sizeof(t->request)||p->word[8]!=t->request.length||p->word[9]!=t->request.generation)return 0;
    if(op==QT_PULL){
        if(offset!=t->acknowledged||t->offered!=t->acknowledged||offset>=t->request.length||
           length!=(t->request.length-offset<Q33_CHUNK?t->request.length-offset:Q33_CHUNK))return 0;
        if((t->flags&QF_FAILURE))return 1;
        if(!t->waited&&((t->flags&(QF_DELAY|QF_STALE))||((t->flags&QF_PARTIAL)&&offset>=Q33_CHUNK))){
            t->waited=1;p->word[10]=QT_WAIT;return 1;
        }
        const unsigned char *bytes=(const unsigned char *)t->code;
        for(unsigned i=0;i<4;i++)p->word[4+i]=0;
        for(u64 i=0;i<length;i++)p->word[4+i/8]|=(u64)bytes[offset+i]<<(8*(i%8));
        t->offered=offset+length;p->word[10]=QT_OK;
        if((t->flags&QF_DUPLICATE)&&offset)p->word[2]=0;
        if(t->flags&QF_OFFSET)p->word[2]=offset+Q33_CHUNK;
        if(t->flags&QF_LENGTH)p->word[3]=Q33_CHUNK+1;
        if(t->flags&QF_IDENTITY)p->word[1]=txn+1;
        return 1;
    }
    if(op==QT_ACK){
        if(offset!=t->acknowledged||!length||offset+length!=t->offered)return 0;
        t->acknowledged=t->offered;p->word[10]=QT_OK;
        if(t->acknowledged==t->request.length){t->active=0;p->word[10]=QT_DONE;}
        return 1;
    }
    return 0;
}
#endif
