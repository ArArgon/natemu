#ifndef TRACK01_Q30_H
#define TRACK01_Q30_H
#include "x86.h"
#define Q30_PA 0x41000000UL
#define Q30_MAGIC 0x5133305041474531UL
#define Q30_VERSION 6UL
#define Q30_LOG_CAPACITY 20971520
#define Q30_SHADOW_PA 0x40200000UL
#define Q30_SHADOW_PAGES 128
enum { Q_LOAD, Q_STORE, Q_CMPXCHG, Q_EXEC, Q_FETCH8 };
enum { QE_CONTEXT = 1, QE_BEGIN, QE_END, QE_MUTATION, QE_GATE, QE_ACK,
       QE_ROLE, QE_MAP, QE_CASE, QE_FAULT, QE_WAIT, QE_MARK, QE_Q31, QE_Q32, QE_Q33, QE_Q34 };
enum { QM_GUEST = 1, QM_AD, QM_SETUP, QM_CODE };
struct q30_record { u64 sequence, kind, cpu, epoch, data[8]; };
struct q30_request {
    u64 operation, gva, kind, width, value, compare, rax, flags;
    u64 status, error, gpa, faults, inflight;
};
struct q30_cpu {
    u64 started, done, active, ack, waiting, shadow_used;
    u64 operations, faults, table_traps, guest_faults, epochs, acknowledgements;
    u64 timer_start, timer_end, final_tcr, final_ttbr0, final_ttbr1;
    struct x86_context context;
    struct q30_request request;
    struct cpu_state foundation;
};
struct q30_state {
    u64 magic, version, ncpus, failure, records, epoch, gate, owner, lock;
    u64 barrier_arrived, barrier_generation, case_id, completed_groups;
    struct q30_cpu cpu[MAX_CPUS];
    unsigned char roles[X86_RAM_SIZE / 4096];
    struct q30_record log[Q30_LOG_CAPACITY];
};
_Static_assert(sizeof(struct q30_state) <= RAM_SIZE - (Q30_PA - RAM_BASE),
               "Q30 state exceeds retained RAM");
#ifdef Q30_FIXTURE_CHECK
extern struct q30_state *q30_fixture_state;
#define Q30 q30_fixture_state
#else
#define Q30 ((struct q30_state *)Q30_PA)
#endif
void q30_run(u64 id, u64 shim_l1);
void q30_bootstrap(u64 shim_l1);
int q30_exception(u64 *frame, u64 esr, u64 far);
void q30_cases(u64 id);
void q30_barrier(void);
void q30_poll(void);
u64 q30_timer_count(void);
void q30_check(int ok, u64 reason);
void q30_context(struct x86_context context, u64 reason);
void q30_invalidate(u64 address, u64 reason);
void q30_setup(u64 gpa, u64 value, unsigned width);
struct q30_patch { u64 gpa, value; };
void q30_setup_batch(const struct q30_patch *patches, u64 count);
u64 q30_access(u64 gva, unsigned kind, unsigned width, u64 value, u64 compare);
u64 q30_fault_address(void);
u64 q30_record(u64 kind, const u64 data[8]);
#define QLOG(kind, ...) q30_record((kind), (u64[8]){__VA_ARGS__})
#endif
