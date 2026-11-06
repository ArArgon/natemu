#include "shared.h"
#include "q34.h"
#define READ(reg) ({ u64 v; __asm__ volatile("mrs %0, " #reg : "=r"(v)); v; })
#define WRITE(reg, v) __asm__ volatile("msr " #reg ", %0" :: "r"((u64)(v)) : "memory")
#define BARRIER() __asm__ volatile("dsb sy\nisb" ::: "memory")
#define S ((volatile struct shared *)SHARED_PA)
static u64 l1[512] __attribute__((aligned(4096)));
static u64 l2[2][512] __attribute__((aligned(4096)));
static u64 device_l2[512] __attribute__((aligned(4096)));
static u64 pages[8][512] __attribute__((aligned(4096)));
static u64 aliases[512] __attribute__((aligned(4096)));
extern u64 fault_load(u64);
extern char fault_site[], fault_resume[];

static void trace(u64 id, u64 kind, u64 value) {
    volatile struct cpu_state *c = &S->cpu[id];
    u64 n = c->trace_count;
    c->trace[n % 32] = (struct event){kind, value, READ(cntvct_el0)};
    c->trace_count = n + 1;
}
static void fail(u64 id, u64 reason) {
    S->cpu[id].failure = reason;
    trace(id, 255, reason);
    BARRIER();
    __asm__ volatile("hvc #0x31");
    for (;;) __asm__ volatile("wfe");
}
static void require(u64 id, int ok, u64 reason) { if (!ok) fail(id, reason); }
static void tables(void) {
    l1[1] = (u64)l2[0] | 3;
    l1[2] = (u64)l2[1] | 3;
    l1[0] = (u64)device_l2 | 3;
    _Static_assert(RAM_SIZE>0x40000000UL && RAM_SIZE<0x80000000UL &&
                   !(RAM_SIZE&0x1fffffUL) && ALIAS_VA==RAM_BASE+RAM_SIZE,"identity layout");
    for (u64 i = 0; i < RAM_SIZE / 0x200000; ++i) {
        if(i<8){
            l2[0][i] = (u64)pages[i] | 3;
            for (u64 j = 0; j < 512; ++j)
                pages[i][j] = (RAM_BASE + i * 0x200000 + j * 4096) | 0x703;
        }else{
            // Trace storage only: 2MiB Normal-WB L2 blocks in the same 4KiB
            // granule. Code/state below16MiB and Q30 guest shadows stay4KiB.
            l2[i/512][i%512] = (RAM_BASE + i * 0x200000) | 0x701;
        }
    }
    // Device-nGnRnE, EL1 RW, execute-never. Stage-1 still uses 4KiB granule.
    device_l2[GICD_PA >> 21] = GICD_PA | 0x405 | (3UL << 53);
    for (u64 i = 0; i < 8; ++i)
        device_l2[(GICR_PA >> 21) + i] = (GICR_PA + i * 0x200000) | 0x405 | (3UL << 53);
    l2[(ALIAS_VA-RAM_BASE)>>30][(ALIAS_VA >> 21) & 511] = (u64)aliases | 3;
    aliases[0] = DATA_PA | 0x703;
    *(volatile u64 *)DATA_PA = 0x13579bdf2468ace0UL;
    BARRIER();
    __atomic_store_n(&S->tables_ready, 1, __ATOMIC_RELEASE);
}
static void mmu(void) {
    WRITE(mair_el1, 0xff); // Attr0 normal WB; Attr1 device nGnRnE (zero).
    // Select the extended layout before enabling translation; no live switch
    // through incompatible TTBR/TCR geometries is needed during the test.
    q30_bootstrap((u64)l1);
    BARRIER();
    __asm__ volatile("tlbi vmalle1\ndsb sy\nisb" ::: "memory");
    WRITE(sctlr_el1, READ(sctlr_el1) | 1UL | (1UL << 2) | (1UL << 12));
    BARRIER();
}
static void timer_arm(void) {
    WRITE(cntp_tval_el0, S->counter_hz / 100); // Diagnostic ~10ms, no latency claim.
    WRITE(cntp_ctl_el0, 1);
    __asm__ volatile("isb" ::: "memory");
}
void exception(u64 *frame, u64 vector_kind) {
    u64 id = READ(tpidr_el1);
    volatile struct cpu_state *c = &S->cpu[id];
    u64 esr = READ(esr_el1);
    // Expected synchronous access has a unique PC; IRQs never originate there
    // because the deliberate fault runs with DAIF.I masked.
    if (vector_kind == 0 && frame[32] == (u64)fault_site) {
        c->esr = esr; c->far = READ(far_el1); c->elr = frame[32];
        require(id, (esr >> 26) == 0x25 && (esr & 63) == 7 &&
                    c->far == ALIAS_VA + 4096, 11);
        c->sync_count++;
        trace(id, 2, esr);
        frame[0] = 0;
        frame[32] = (u64)fault_resume;
        return;
    }
    if (vector_kind == 0 && q30_exception(frame, esr, READ(far_el1))) return;
    if (vector_kind != 1) {
        c->esr = esr; c->far = READ(far_el1); c->elr = frame[32];
        fail(id, 0x4000 + vector_kind);
    }
    u64 irq = READ(icc_iar1_el1) & 0xffffff;
    if (irq == S->timer_intid) {
        c->timer_count++;
        timer_arm();
        q32_irq(irq,frame[32],frame[33]);
    } else if (irq == 1) {
        c->sgi_count++;
    } else if (q32_irq(irq,frame[32],frame[33])) {
        WRITE(icc_eoir1_el1,irq);__asm__ volatile("isb":::"memory");return;
    } else {
        c->esr = esr; c->far = READ(far_el1); c->elr = frame[32];
        fail(id, 0x1000 + irq);
    }
    trace(id, 3, irq);
    WRITE(icc_eoir1_el1, irq);
    __asm__ volatile("isb" ::: "memory");
}
void guest_main(u64 id) {
    volatile struct cpu_state *c = &S->cpu[id];
    c->ticks_start = READ(cntvct_el0);
    c->tso_start = READ(actlr_el1);
    require(id, c->tso_start & 2, 1);
    c->mmfr0 = READ(id_aa64mmfr0_el1);
    require(id, ((c->mmfr0 >> 28) & 15) != 15, 2);
    if (id == 0) tables();
    while (!__atomic_load_n(&S->tables_ready, __ATOMIC_ACQUIRE)) __asm__ volatile("yield");
    mmu();
    c->sctlr = READ(sctlr_el1); c->tcr = READ(tcr_el1);
    c->alias_value = *(volatile u64 *)ALIAS_VA;
    require(id, c->alias_value == 0x13579bdf2468ace0UL, 3);
    trace(id, 1, c->alias_value);
    fault_load(ALIAS_VA + 4096);
    require(id, c->sync_count == 1, 4);
    volatile u32 *waker = (volatile u32 *)(c->redistributor + 0x14);
    c->waker_before = *waker;
    *waker = (u32)c->waker_before & ~2U;
    BARRIER();
    while (*waker & 4U)
        require(id, READ(cntvct_el0) - c->ticks_start < S->counter_hz * 5, 7);
    c->waker_after = *waker;
    require(id, !(c->waker_after & 6), 8);
    WRITE(icc_sre_el1, 1);
    __asm__ volatile("isb" ::: "memory");
    WRITE(icc_pmr_el1, 255);
    WRITE(icc_bpr1_el1, 0);
    WRITE(icc_ctlr_el1, READ(icc_ctlr_el1)&~2UL); // EOImode0: EOIR deactivates.
    WRITE(icc_igrpen1_el1, 1);
    __atomic_store_n(&c->ready, 1, __ATOMIC_RELEASE);
    for (u64 i = 0; i < S->ncpus; ++i)
        while (!__atomic_load_n(&S->cpu[i].ready, __ATOMIC_ACQUIRE)) __asm__ volatile("yield");
    timer_arm();
    BARRIER();
    WRITE(icc_sgi1r_el1, (1UL << 24) | (1UL << ((id + 1) % S->ncpus)));
    __asm__ volatile("isb\nmsr daifclr, #2" ::: "memory");
    while (c->timer_count < 3 || c->sgi_count < 1) {
        require(id, READ(cntvct_el0) - c->ticks_start < S->counter_hz * 5, 5);
        __asm__ volatile("yield");
    }
    q30_run(id, (u64)l1);
    q31_checkpoint();
    q31_run(id);
    q32_checkpoint();
    q32_run(id);
    q33_checkpoint();
    q33_run(id);
    q34_checkpoint();
    q34_run(id);
    __asm__ volatile("msr daifset, #2" ::: "memory");
    c->tso_end = READ(actlr_el1);
    require(id, (c->tso_end & 2) && c->sgi_count == 1, 6);
    c->ticks_end = READ(cntvct_el0);
    trace(id, 4, c->timer_count);
    __atomic_store_n(&c->done, 1, __ATOMIC_RELEASE);
}
