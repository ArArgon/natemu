#ifndef TRACK01_Q32_H
#define TRACK01_Q32_H
#include "q31.h"
#define Q32_PA 0x40e10000UL
#define Q32_MAGIC 0x5133324952515354UL
#define Q32_VERSION 2UL
#define Q32_HALT_TRIALS 8
#define Q32_REPETITIONS 16
#define Q32_VARIANTS 37
#define Q32_SGI 2
enum { Q32_CASE=1,Q32_EMIT,Q32_NATIVE,Q32_ACCEPT,Q32_BOUNDARY,Q32_DISPATCH,
       Q32_EOI,Q32_SHADOW,Q32_HALT,Q32_CONTROL,Q32_END };
enum { Q32_TIMER=1,Q32_PEER,Q32_HOST };
enum { Q32_IDLE=0,Q32_INTERIOR=1,Q32_REPAIR=2,Q32_TRACKING=3,
       Q32_EXCEPTION=4,Q32_HALT_WAIT=5,Q32_CODE_WRITE=6 };
// QE_Q32 (14): subtype followed by seven words.
// CASE: group,variant,repetition,start/end,serial,target,role.
// EMIT: source,sender,target,sequence,vector,route,flags.
// NATIVE: source,INTID,actual ELR,actual SPSR,stage,native acknowledgement,target.
// ACCEPT: source,sender,previous sequence,new sequence,vector,old IRR bit,native acknowledgement.
// BOUNDARY: boundary,RIP,RFLAGS,shadow,TPR,selected vector,halted.
// DISPATCH: boundary,vector,frame RSP,depth,error-word-present,native acknowledgement,generation.
// EOI: vector,remaining highest ISR,TPR,boundary,0,0,0.
// SHADOW: instruction,old IF,new IF,old shadow,new shadow,boundary,exception termination.
// HALT: action,wake generation,eligible vector,IF,next RIP,native acknowledgements,stage.
// CONTROL: action and six action-specific words documented at the producer site.
// Action15 (format2): HLT trial1..8,actualnativeELR,requiredpostWFEPC,
// exactpostWFEbool,postHLTguestRIP,nativeackcount. Every trial is fully serviced;
// only an exact post-WFE trial may finish group7/variant2.
// END: serial,group,variant,repetition,guest deliveries,native acknowledgements,boundaries.
struct q32_cpu {
    u64 started,done,cases,native_count,guest_count,boundaries;
    u64 shadow,tpr,halted,stage,ready,wake_generation;
    u64 irr[4],isr[4];
    u64 emitted[3][MAX_CPUS],accepted[3][MAX_CPUS],vectors[3][MAX_CPUS];
    u64 spi_request,spi_sent,spi_deassert,spi_deasserted,spi_level;
    u64 hook_stage,hook_source,hook_target,hook_vector;
    u64 last_elr,last_spsr,last_stage,interior_count,handler_effects;
    u64 timer_start,timer_end;
    u64 raw_irq[3],empty_irq[3];
    u64 sgi_sent[MAX_CPUS];
    u64 diagnostic_request,diagnostic_ack,diagnostic_case;
};
struct q32_state {
    u64 magic,version,ncpus,failure,phase,cases,checkpoint_records,checkpoint_epoch;
    u64 spi_base,spi_count,sgi,eoimode;
    struct q32_cpu cpu[MAX_CPUS];
};
_Static_assert(Q32_PA+sizeof(struct q32_state)<0x40f00000UL,"Q32 overlaps native stacks");
#ifdef Q30_FIXTURE_CHECK
extern struct q32_state *q32_fixture_state;
#define Q32 q32_fixture_state
#else
#define Q32 ((struct q32_state *)Q32_PA)
#endif
void q32_run(u64 cpu);
int q32_irq(u64 intid,u64 elr,u64 spsr);
void q32_hook(u64 stage);
void q32_checkpoint(void);
void q32_reuse_fixture(u64 serial);
void q32_receive(u64 source,u64 stage,u64 vector);
void q32_service(void);
void q32_enable_interrupts(void);
void q32_service_change(u64 address,u64 value);
void q32_emit(u64 source,u64 target,u64 vector);
#endif
