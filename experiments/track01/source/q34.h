#ifndef TRACK01_Q34_H
#define TRACK01_Q34_H
#include "q33.h"
#define Q34_PA 0x40700000UL
#define Q34_MAGIC 0x5133345354415254UL
#define Q34_VERSION 1UL
#define Q34_REPETITIONS 16
#define Q34_VARIANTS 8
#define Q34_RECORD_KIND 16
#define Q34_RAM_BASE 0x300000UL
#define Q34_RAM_BYTES 0x20000UL
#define Q34_KERNEL_BASE 0xffffffff80000000UL
#define Q34_MAX_STEPS 128UL
#define Q34_PROJECT_MAX 1024UL
enum { Q34_CASE=1,Q34_MAIL,Q34_STEP,Q34_STATE,Q34_MEMORY,Q34_LOCK,
       Q34_CONTROL,Q34_CONTINUE,Q34_END };
enum { Q34_UBUNTU_EQUAL=0,Q34_UBUNTU_WRITE,Q34_DEBIAN_EQUAL,
       Q34_DEBIAN_WRITE,Q34_DEBIAN_MATCH,Q34_WRONG_ID,Q34_NO_RESPONDER,Q34_BOOT_ONCE };
enum { Q34_MOV=1,Q34_ADD,Q34_XOR,Q34_AND,Q34_OR,Q34_BT,Q34_BTS,
       Q34_CMP,Q34_TEST,Q34_JCC,Q34_JMP,Q34_PAUSE,Q34_LIDT,Q34_LGDT,
       Q34_FAR,Q34_SEG,Q34_RDCR,Q34_WRCR,Q34_RDMSR,Q34_WRMSR,
       Q34_PUSH,Q34_LRET,Q34_CALL_VERIFY,Q34_LOAD,Q34_STORE,Q34_READY,Q34_INC };
enum { Q34_LONG64=1,Q34_COMPAT32,Q34_PROTECTED32 };
enum { Q34_MAIL_PUBLISH=1,Q34_MAIL_IGNORE,Q34_MAIL_CACHE,Q34_MAIL_ACK,Q34_MAIL_TIMEOUT };
// A real ACPI v0 mailbox prefix in guest-owned backing, never a widened u64 command.
struct q34_mailbox { unsigned short command,reserved; u32 apic_id; u64 vector; };
_Static_assert(sizeof(struct q34_mailbox)==16,"mailbox ABI");
_Static_assert(__builtin_offsetof(struct q34_mailbox,apic_id)==4,"APIC ID ABI");
_Static_assert(__builtin_offsetof(struct q34_mailbox,vector)==8,"vector ABI");
// Source-level startup instruction state, distinct from the Q31 long-mode runtime.
// RIP uses fixture trace labels (four-byte positions), not decoded stock x86
// instruction lengths. JCC source IDs specify the executed semantic branch;
// far transfers carry actual modeled selector/target and mode effects.
// STATE serial,step,chunk followed by four words; 9 chunks cover all 36 words.
struct q34_arch {
    u64 gpr[16],rip,flags,cr0,cr3,cr4,efer,cs,ss,ds,es,fs,gs;
    u64 gdtr_base,gdtr_limit,idtr_base,idtr_limit,mode,tlb_generation,reserved[2];
};
_Static_assert(sizeof(struct q34_arch)==36*8,"startup state ABI");
struct q34_cpu {
    u64 cases,booted,cached_id,cached_vector,seen,ignored,acknowledged,steps;
    u64 lock_attempts,lock_acquired,stack_private,ready,continuations,failures;
    u64 efer_reads,efer_writes,protected_steps,compat_steps,long_steps;
    u64 timer_start,timer_end,input,result;
    struct q34_arch arch;
};
struct q34_state {
    u64 magic,version,ncpus,failure,phase,cases,serial,variant,repetition,publisher;
    u64 request_sequence,request_count,responders,finish,lock_owner,contention;
    u64 io_lock,diagnostic_request,diagnostic_ack,diagnostic_reason,diagnostic_cpu;
    u64 checkpoint_records,checkpoint_epoch;
    struct q34_cpu cpu[MAX_CPUS];
    unsigned char memory[Q34_RAM_BYTES] __attribute__((aligned(4096)));
};
_Static_assert(Q34_PA>=Q33_POOL_END,"Q34 overlaps generated pool");
_Static_assert(Q34_PA+sizeof(struct q34_state)<=SHARED_PA,"Q34 exceeds its 1MiB interval");
#ifdef Q30_FIXTURE_CHECK
extern struct q34_state *q34_fixture_state;
#define Q34 q34_fixture_state
#else
#define Q34 ((struct q34_state *)Q34_PA)
#endif
// Q34_STEP: step,opcode,width,destination,operand,result,source-PC.
// Q34_MEMORY: step,linear,physical,width,write,old,new (ordinary reads old==new).
// Q34_LOCK: step,action(1=BTS32/2=release32),old32,new32,CF,owner,upper-canary.
// Q34_MAIL: action,request-sequence,id,vector,command16,booted,case-serial.
// Q34_CONTROL: action and six arguments, documented at its emission site.
// 8 projection base/bytes/changed/root/serial/0;9 import RIP/RSP/CR3/CS/SS/serial;
// 10 diagnostic stack VA/GPA/value/Q30op/CR3/serial;11 split parent/PT/root/base/size/serial;
// 12 explicit fixture old/new GDTR,old/new IDTR,CS,SS;
// 13 assumed verify_cpu return: unknownGPRmask,undefinedFlagsMask,EAX0,oldRDI,newRDI,serial.
// QI_IMPORT18 is a nonretiring Q34-only memory probe, not an x86 instruction.
// Q34_CONTINUE: action,input,operand,result,mode,RSP,cached-vector.
// Q34_END: serial,variant,rep,booted,ready,steps,timer.
void q34_run(u64 cpu);
void q34_checkpoint(void);
// Integration seam: must execute Q33 host-produced ADD RAX,imm with the supplied
// canonical input/operand, retaining request/native-PC/result evidence. No marker stub.
u64 q34_continuation(u64 cpu,u64 input,u64 operand);
#endif
