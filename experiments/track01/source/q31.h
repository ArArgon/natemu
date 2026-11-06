#ifndef TRACK01_Q31_H
#define TRACK01_Q31_H
#include "q30.h"
#define Q31_PA 0x40e00000UL
#define Q31_MAGIC 0x5133314641554c54UL
#define Q31_VERSION 1UL
#define Q31_REPETITIONS 16
#define Q31_VARIANTS 34
enum { Q31_CASE = 1, Q31_ATTEMPT, Q31_STATE, Q31_STEP, Q31_DELIVER,
       Q31_DELIVERY_FAULT, Q31_RETURN, Q31_CODE, Q31_MARK, Q31_END };
enum { QS_BEFORE = 1, QS_FAULT, QS_HANDLER, QS_RETURNED, QS_COMMITTED, QS_PROGRESS };
enum { QR_RAX, QR_RCX, QR_RDX, QR_RBX, QR_RSP, QR_RBP, QR_RSI, QR_RDI };
enum { QI_LOAD = 1, QI_STORE, QI_RMW, QI_REP, QI_FETCH, QI_CODE_STORE, QI_CPUID,
       QI_CLI, QI_STI, QI_MOVSS, QI_HLT, QI_NOP, QI_ADD, QI_EOI, QI_MOVSP, QI_JMP, QI_MOV_RDI, QI_IMPORT };
enum { QC_ROLE = 1, QC_GENERATION, QC_PIN, QC_UNPIN };
// QE_Q31 data[0] is the subtype above; the remaining seven words are:
// CASE: group, variant, repetition, start/end, serial, ncpus, 0.
// ATTEMPT: attempt, metadata identity, RIP, instruction, length, width, generation.
// STATE: snapshot, point | depth<<8, attempt, chunk (0..6), three arch words.
// STEP: attempt, action, status, error, Q30 operation, effects, value.
// DELIVER: attempt, vector, error, CR2, handler, frame RSP, depth.
// DELIVERY_FAULT: attempt, first vector, second vector, error, CR2, phase, depth.
// RETURN: attempt, vector, frame RSP, new RIP, new RSP, depth, 0.
// CODE: action, aligned physical page, old, new, Q30 operation, generation, users.
// CODE generation/users are the values AFTER the action. PIN/UNPIN old/new
// are aggregate users; ROLE old/new are booleans; GENERATION old/new are epochs.
// END: serial, group, variant, repetition, commits, deliveries, timer.
struct q31_arch { u64 gpr[16], rip, flags, cs, ss, cr2; };
struct q31_metadata {
    u64 identity, generation, rip, length, instruction, access, width, substep;
};
struct q31_cpu {
    u64 started, done, cases, attempts, deliveries, commits, code_writes;
    u64 snapshots, timer_start, timer_end, depth, current_attempt;
    struct q31_arch arch;
    struct q31_metadata metadata;
};
struct q31_state {
    u64 magic, version, ncpus, failure, cases, phase, checkpoint_records, checkpoint_epoch;
    struct q31_cpu cpu[MAX_CPUS];
    u64 code_generation[X86_RAM_SIZE / 4096];
    u64 code_users[X86_RAM_SIZE / 4096];
    unsigned char code_roles[X86_RAM_SIZE / 4096];
};
_Static_assert(sizeof(struct q31_state) <= Q30_PA - Q31_PA, "Q31 state exceeds free RAM");
#ifdef Q30_FIXTURE_CHECK
extern struct q31_state *q31_fixture_state;
#define Q31 q31_fixture_state
#else
#define Q31 ((struct q31_state *)Q31_PA)
#endif
void q31_run(u64 cpu);
void q31_checkpoint(void);
int q31_validate_fault(u64 cpu, u64 pc);
// Q32 grows the same interpreter state after the immutable Q31 checkpoint.
struct q31_access_result { u64 value, status, error, address; };
void q31_reuse_fixture(u64 serial);
// Q34 only: import exact final startup state, without resetting earlier counters.
void q31_import_context(struct x86_context context,u64 base,u64 backing,u64 serial,const struct q31_arch *arch);
u64 q31_base(void);
void q31_begin(u64 instruction,u64 width,u64 length);
struct q31_access_result q31_memory(u64 address,unsigned kind,unsigned width,u64 value,unsigned action);
void q31_set_privilege(u64 cpl);
void q31_map(u64 offset,u64 flags);
void q31_set_gate(u64 vector,u64 handler,u64 selector,u64 ist,u64 type);
u64 q31_raise(u64 vector,u64 error,u64 address);
u64 q31_interrupt(u64 vector,int partial_progress);
void q31_return(u64 vector,int has_error);
void q31_retire(void);
void q31_retire_at(u64 next_rip);
void q31_state_snapshot(u64 point);
void q31_code_acquire(u64 va,u64 page);
int q31_code_acquire_expected(u64 va,u64 page,u64 generation);
void q31_code_release(void);
u64 q31_code_dispatch(u64 va,u64 page);
void q31_code_serialize(u64 va);
// Code-role changes use the same writer lock, native quiescence and eviction
// as page-table tracking. Generation changes precede subsequent dispatch.
void q30_code_track(u64 page, int enabled);
int q30_code_pin(u64 page, u64 expected_generation);
void q30_code_unpin(u64 page);
u64 q31_code_generation(u64 page);
#endif
