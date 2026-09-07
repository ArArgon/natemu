#ifndef TRACK01_SHARED_H
#define TRACK01_SHARED_H
typedef unsigned long u64;
typedef unsigned int u32;
#define RAM_BASE 0x40000000UL
#define RAM_SIZE 0x7c000000UL
#define SHARED_PA 0x40800000UL
#define ALIAS_VA 0xbc000000UL
#define DATA_PA 0x40900000UL
#define GICD_PA 0x10000000UL
#define GICR_PA 0x11000000UL
#define MAX_CPUS 4
#define MAGIC 0x4e4154454d553031UL
struct event { u64 kind, value, ticks; };
struct cpu_state {
    u64 ready, done, failure, tso_start, tso_end, sctlr, tcr, mmfr0;
    u64 sync_count, timer_count, sgi_count, esr, far, elr, alias_value;
    u64 trace_count, ticks_start, ticks_end, timer_intid, redistributor;
    u64 waker_before, waker_after;
    struct event trace[32];
};
struct shared {
    u64 magic, version, ncpus, tables_ready, timer_intid, counter_hz;
    struct cpu_state cpu[MAX_CPUS];
};
#endif
