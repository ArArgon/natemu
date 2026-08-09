// SMP scaling probe for the Tier 2 in-VM shadow-MMU design.
//
// Doc 16 measured every shadow-paging cost with ONE vCPU (and TLBI contention with exactly one
// other vCPU). This probe re-runs the load-bearing measurements at 1..N vCPUs.
//
//   scale     N vCPUs concurrently taking shadow-fill faults, private page tables + private
//             memory (pure infrastructure contention) and shared page tables (guest-kernel-like)
//   tlbi      broadcast TLBI issued by one vCPU while 0..N-1 others run: idle, TLB-heavy on
//             private VAs, TLB-heavy on the SAME VAs being invalidated (real shootdown)
//   ipi       cross-vCPU signalling: hv_vcpus_exit round trip, host-injected IRQ to guest EL1,
//             and an in-VM shared-memory flag ping-pong
//   litmus    MP-with-counters at 4 and 8 vCPUs, independent pairs and one-writer/many-readers
//   entso     cost of ACTLR_EL1.EnTSO=1 on contended vs private cache lines, N vCPUs
//   pe        P-core vs E-core placement of vCPU threads, steered with pthread QoS
//   oversub   more vCPUs than P-cores; per-vCPU throughput and fault-latency tails
//
// All in-guest timing is CNTVCT_EL0 read at guest EL1 under our own VBAR_EL1, so no host exit
// occurs inside a measured window.
//
// Build: ../build.sh smp_scaling/smp_scaling.c

#include <Hypervisor/Hypervisor.h>
#include <inttypes.h>
#include <mach/mach_time.h>
#include <pthread.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <sys/mman.h>
#include <sys/sysctl.h>
#include <pthread/qos.h>

#include "payload.h"
__asm__(PAYLOAD_ASM);

extern const uint8_t sp_start[], sp_end[];
extern const uint8_t h_skip[], h_shadow[], h_shadow_tlbi[], h_panic[], h_irq[];

// ------------------------------------------------------------------ memory map
#define RAM_IPA        0x40000000ULL
#define RAM_SIZE       0x10000000ULL          // 256 MiB

#define OFF_CODE       0x00000000ULL
#define OFF_VEC        0x00010000ULL          // 2 KB vector table
#define OFF_GLOBAL     0x00011000ULL          // 16 KB control block
#define OFF_SHAREDPT   0x00020000ULL          // 512 KB shared page-table arena
#define OFF_SHAREDPT_E 0x000a0000ULL
#define OFF_TLBITGT    0x00100000ULL          // 4 MiB shared TLBI target backing
#define OFF_SHAREDWS   0x00500000ULL          // 4 MiB shared working set (EnTSO contention)
#define OFF_VCPU0      0x01000000ULL
#define VCPU_STRIDE    0x00800000ULL          // 8 MiB per vCPU
#define MAXV           12

// within a vCPU slab
#define S_PARAM        0x000000ULL            // 4 KB
#define S_RES          0x001000ULL            // 128 KB fault samples
#define S_RES2         0x021000ULL            // 124 KB control samples
#define S_ARENA        0x040000ULL            // 256 KB private page-table arena
#define S_ARENA_E      0x080000ULL
#define S_X86PT        0x080000ULL            // 16 KB
#define S_SPIN         0x090000ULL            // 448 KB private contention buffer
#define S_BACK         0x100000ULL            // 2 MiB shadow backing (512 pages)
#define S_WS           0x400000ULL            // 4 MiB private working set

// global control block offsets
#define G_BAR          0x0000ULL              // 64 barrier slots
#define G_STOP         0x0200ULL              // 64 stop flags
#define G_LIT          0x0400ULL              // litmus var pairs, k*0x200
#define G_PPA          0x1000ULL
#define G_PPB          0x1080ULL

// guest VAs
#define VA_SHADOW      0x0000400000000000ULL  // + i*2 MiB
#define VA_TLBI        0x0000401000000000ULL  // 4 MiB, shared
#define VA_PRIV        0x0000402000000000ULL  // + i*4 MiB, private

// descriptor bits
#define D_PAGE   3ULL
#define D_BLOCK  1ULL
#define D_AF     (1ULL << 10)
#define D_SH_IS  (3ULL << 8)
#define D_UXN    (1ULL << 54)
#define D_PXN    (1ULL << 53)
#define PA_MASK  0x0000fffffffff000ULL

#define MAIR_VAL 0xFFULL
#define TCR_4K   (16ULL | (1ULL<<8) | (1ULL<<10) | (3ULL<<12) \
                 | (16ULL<<16) | (1ULL<<23) | (2ULL<<30) | (2ULL<<32))

#define HV(e) do { hv_return_t _r = (e); if (_r != HV_SUCCESS) { \
    fprintf(stderr, "  %s -> 0x%x at %d\n", #e, _r, __LINE__); exit(2);} } while (0)

static uint8_t *g_mem;
static uint64_t g_cntfrq = 24000000;
static double   g_ghz = 3.25;
static int      g_fail = 0;
static int      g_maxv = 0;

static uint64_t slab(int i)      { return OFF_VCPU0 + (uint64_t)i * VCPU_STRIDE; }
static uint64_t *P(int i)        { return (uint64_t *)(g_mem + slab(i) + S_PARAM); }
static uint64_t *AUX(int i)      { return (uint64_t *)(g_mem + slab(i) + S_PARAM + 0xc00); }
static uint64_t *RES(int i)      { return (uint64_t *)(g_mem + slab(i) + S_RES); }
static uint64_t *RES2(int i)     { return (uint64_t *)(g_mem + slab(i) + S_RES2); }
static uint64_t *GLOB(uint64_t o){ return (uint64_t *)(g_mem + OFF_GLOBAL + o); }

// ------------------------------------------------------------------ page tables
typedef struct { uint64_t cur, end; } arena_t;

static uint64_t alloc_tab(arena_t *a) {
    a->cur = (a->cur + 0xfff) & ~0xfffULL;
    uint64_t off = a->cur;
    a->cur += 0x1000;
    if (a->cur > a->end) { fprintf(stderr, "arena exhausted\n"); exit(2); }
    memset(g_mem + off, 0, 0x1000);
    return off;
}
static uint64_t *tab(uint64_t off) { return (uint64_t *)(g_mem + off); }

static uint64_t walk_to(arena_t *a, uint64_t root, uint64_t va, int level) {
    int sh[4] = { 39, 30, 21, 12 };
    int nb[4] = { 9, 9, 9, 9 };
    uint64_t cur = root;
    for (int l = 0; l < level; l++) {
        uint64_t idx = (va >> sh[l]) & ((1ULL << nb[l]) - 1);
        uint64_t *t = tab(cur);
        if ((t[idx] & 1) == 0) t[idx] = (RAM_IPA + alloc_tab(a)) | D_PAGE;
        cur = (t[idx] & PA_MASK) - RAM_IPA;
    }
    return cur;
}
static void map_page(arena_t *a, uint64_t root, uint64_t va, uint64_t pa, uint64_t attrs) {
    uint64_t l3 = walk_to(a, root, va, 3);
    tab(l3)[(va >> 12) & 0x1ff] = (pa & PA_MASK) | D_PAGE | attrs;
}
static void map_block2(arena_t *a, uint64_t root, uint64_t va, uint64_t pa, uint64_t attrs) {
    uint64_t l2 = walk_to(a, root, va, 2);
    tab(l2)[(va >> 21) & 0x1ff] = (pa & ~0x1fffffULL) | D_BLOCK | attrs;
}
static uint64_t l3_ipa(arena_t *a, uint64_t root, uint64_t va) {
    return RAM_IPA + walk_to(a, root, va, 3);
}
// identity-map all guest RAM with 2 MiB blocks, RW + executable at EL1
static uint64_t build_identity(arena_t *a) {
    uint64_t root = alloc_tab(a);
    for (uint64_t o = 0; o < RAM_SIZE; o += 0x200000ULL)
        map_block2(a, root, RAM_IPA + o, RAM_IPA + o, D_AF | D_SH_IS);
    return root;
}

// x86-format 4-level tables for vCPU i: map its shadow VA range onto its backing pages
static void build_x86_tables(int i, uint64_t sva) {
    uint64_t base = slab(i) + S_X86PT;
    uint64_t *pml4 = (uint64_t *)(g_mem + base + 0x0000);
    uint64_t *pdpt = (uint64_t *)(g_mem + base + 0x1000);
    uint64_t *pd   = (uint64_t *)(g_mem + base + 0x2000);
    uint64_t *pt   = (uint64_t *)(g_mem + base + 0x3000);
    memset(g_mem + base, 0, 0x4000);
    pml4[(sva >> 39) & 0x1ff] = (RAM_IPA + base + 0x1000) | 0x7;
    pdpt[(sva >> 30) & 0x1ff] = (RAM_IPA + base + 0x2000) | 0x7;
    pd  [(sva >> 21) & 0x1ff] = (RAM_IPA + base + 0x3000) | 0x7;
    for (int k = 0; k < 512; k++)
        pt[k] = (RAM_IPA + slab(i) + S_BACK + (uint64_t)k * 4096) | 0x67;   // P|RW|US|A|D
}

// ------------------------------------------------------------------ vectors
enum { H_NONE = 0, H_SKIP, H_SHADOW, H_SHADOW_TLBI };
static void install_vectors(int which) {
    uint32_t *v = (uint32_t *)(g_mem + OFF_VEC);
    uint64_t panic_off = OFF_CODE + (uint64_t)(h_panic - sp_start);
    for (int i = 0; i < 16; i++) {
        int64_t d = (int64_t)panic_off - (int64_t)(OFF_VEC + i * 0x80);
        v[i * 0x20] = 0x14000000u | (uint32_t)((d >> 2) & 0x03ffffff);
    }
    // IRQ, current EL with SP_ELx, at VBAR+0x280
    {
        uint64_t slot = OFF_VEC + 0x280;
        uint64_t hoff = OFF_CODE + (uint64_t)(h_irq - sp_start);
        int64_t d = (int64_t)hoff - (int64_t)slot;
        *(uint32_t *)(g_mem + slot) = 0x14000000u | (uint32_t)((d >> 2) & 0x03ffffff);
    }
    if (which == H_NONE) return;
    const uint8_t *h = which == H_SKIP ? h_skip
                     : which == H_SHADOW ? h_shadow : h_shadow_tlbi;
    uint64_t slot = OFF_VEC + 0x200;
    uint64_t hoff = OFF_CODE + (uint64_t)(h - sp_start);
    int64_t d = (int64_t)hoff - (int64_t)slot;
    *(uint32_t *)(g_mem + slot) = 0x14000000u | (uint32_t)((d >> 2) & 0x03ffffff);
}

// ------------------------------------------------------------------ vCPU group runner
typedef struct {
    int idx;
    uint64_t mode, ttbr, entso, cpsr;
    qos_class_t qos;
    hv_vcpu_t vcpu;
    hv_vcpu_exit_t *vexit;
    volatile int ready, rc;
    volatile int want_cancel_measure;   // 1 = stop on CANCELED, 2 = inject IRQ and re-enter
    uint64_t t_run_returned, t_irq_done;
} ctx_t;

static ctx_t g_ctx[MAXV];
static int   g_n;
static volatile int g_group_fail;

static const char *ecname(uint64_t ec) {
    switch (ec) {
        case 0x00: return "unknown/undef";
        case 0x16: return "HVC";
        case 0x18: return "MSR/MRS trap";
        case 0x21: return "instruction abort";
        case 0x24: return "data abort lower EL";
        case 0x25: return "data abort current EL";
        default:   return "?";
    }
}

static void *vcpu_thread(void *arg) {
    ctx_t *c = arg;
    if (c->qos) pthread_set_qos_class_self_np(c->qos, 0);
    HV(hv_vcpu_create(&c->vcpu, &c->vexit, NULL));
    HV(hv_vcpu_set_reg(c->vcpu, HV_REG_PC,   RAM_IPA + OFF_CODE));
    HV(hv_vcpu_set_reg(c->vcpu, HV_REG_CPSR, c->cpsr ? c->cpsr : 0x3c5));
    HV(hv_vcpu_set_reg(c->vcpu, HV_REG_X0,   MAIR_VAL));
    HV(hv_vcpu_set_reg(c->vcpu, HV_REG_X1,   TCR_4K));
    HV(hv_vcpu_set_reg(c->vcpu, HV_REG_X2,   c->ttbr));
    HV(hv_vcpu_set_reg(c->vcpu, HV_REG_X3,   RAM_IPA + OFF_VEC));
    HV(hv_vcpu_set_reg(c->vcpu, HV_REG_X4,   RAM_IPA + slab(c->idx) + S_PARAM));
    HV(hv_vcpu_set_reg(c->vcpu, HV_REG_X5,   c->mode));
    HV(hv_vcpu_set_reg(c->vcpu, HV_REG_X6,   c->entso));
    c->ready = 1;
    c->rc = 0;
    for (;;) {
        hv_return_t r = hv_vcpu_run(c->vcpu);
        if (r != HV_SUCCESS) { fprintf(stderr, "  vcpu%d run -> 0x%x\n", c->idx, r); c->rc = 1; break; }
        if (c->vexit->reason == HV_EXIT_REASON_VTIMER_ACTIVATED) {
            hv_vcpu_set_vtimer_mask(c->vcpu, true); continue;
        }
        if (c->vexit->reason == HV_EXIT_REASON_CANCELED) {
            if (c->want_cancel_measure == 2) {
                c->t_run_returned = mach_absolute_time();
                hv_vcpu_set_pending_interrupt(c->vcpu, HV_INTERRUPT_TYPE_IRQ, true);
                continue;
            }
            if (c->want_cancel_measure) { c->t_run_returned = mach_absolute_time(); c->rc = 0; break; }
            fprintf(stderr, "  !! vcpu%d cancelled by watchdog (mode %llu)\n",
                    c->idx, (unsigned long long)c->mode);
            c->rc = 1; break;
        }
        if (c->vexit->reason != HV_EXIT_REASON_EXCEPTION) {
            fprintf(stderr, "  !! vcpu%d exit reason %u\n", c->idx, c->vexit->reason);
            c->rc = 1; break;
        }
        uint64_t sy = c->vexit->exception.syndrome, ec = (sy >> 26) & 0x3f;
        if (ec == 0x16 && (sy & 0xffff) == 0) break;                 // normal completion
        if (ec == 0x16 && (sy & 0xffff) == 3) {                      // guest IRQ vector reached
            c->t_irq_done = mach_absolute_time();
            hv_vcpu_set_pending_interrupt(c->vcpu, HV_INTERRUPT_TYPE_IRQ, false);
            break;
        }
        if (ec == 0x16 && (sy & 0xffff) == 10) {
            fprintf(stderr, "  !! vcpu%d BARRIER/SPIN TIMEOUT (mode %llu)\n",
                    c->idx, (unsigned long long)c->mode);
            c->rc = 1; break;
        }
        if (ec == 0x16 && (sy & 0xffff) == 9) {
            uint64_t esr = 0, far = 0, elr = 0;
            hv_vcpu_get_reg(c->vcpu, HV_REG_X25, &esr);
            hv_vcpu_get_reg(c->vcpu, HV_REG_X26, &far);
            hv_vcpu_get_reg(c->vcpu, HV_REG_X27, &elr);
            fprintf(stderr, "  !! vcpu%d PANIC mode %llu ESR=0x%llx EC=0x%llx (%s) FAR=0x%llx ELR=0x%llx\n",
                    c->idx, (unsigned long long)c->mode, (unsigned long long)esr,
                    (unsigned long long)((esr >> 26) & 0x3f), ecname((esr >> 26) & 0x3f),
                    (unsigned long long)far, (unsigned long long)elr);
            c->rc = 1; break;
        }
        if (ec == 0x18) {          // sysreg trap to EL2: skip and keep going, but it is a defect
            uint64_t pc = 0; hv_vcpu_get_reg(c->vcpu, HV_REG_PC, &pc);
            hv_vcpu_set_reg(c->vcpu, HV_REG_PC, pc + 4);
            fprintf(stderr, "  !! vcpu%d EL2 sysreg trap ESR=0x%llx\n", c->idx, (unsigned long long)sy);
            continue;
        }
        uint64_t pc = 0; hv_vcpu_get_reg(c->vcpu, HV_REG_PC, &pc);
        fprintf(stderr, "  !! vcpu%d unexpected exit ESR=0x%llx EC=0x%llx (%s) PC=0x%llx\n",
                c->idx, (unsigned long long)sy, (unsigned long long)ec, ecname(ec),
                (unsigned long long)pc);
        c->rc = 1; break;
    }
    if (c->rc) g_group_fail = 1;
    hv_vcpu_destroy(c->vcpu);
    return NULL;
}

typedef struct { volatile int done; int secs; int n; } wd_t;
static wd_t g_wd;
static void *watchdog(void *p) {
    wd_t *w = p;
    for (int i = 0; i < w->secs * 20 && !w->done; i++) usleep(50000);
    if (!w->done) {
        fprintf(stderr, "  !! watchdog fired: cancelling %d vCPUs\n", w->n);
        hv_vcpu_t v[MAXV];
        for (int i = 0; i < w->n; i++) v[i] = g_ctx[i].vcpu;
        hv_vcpus_exit(v, (uint32_t)w->n);
    }
    return NULL;
}

// Launch n vCPU threads, wait for all. Returns 0 on success.
static int run_group(int n, int secs) {
    g_group_fail = 0;
    pthread_t th[MAXV];
    for (int i = 0; i < n; i++) { g_ctx[i].ready = 0; g_ctx[i].idx = i; }
    g_wd.done = 0; g_wd.secs = secs; g_wd.n = n;
    for (int i = 0; i < n; i++) pthread_create(&th[i], NULL, vcpu_thread, &g_ctx[i]);
    for (int i = 0; i < n; i++) while (!g_ctx[i].ready) usleep(200);
    pthread_t wt; pthread_create(&wt, NULL, watchdog, &g_wd);
    for (int i = 0; i < n; i++) pthread_join(th[i], NULL);
    g_wd.done = 1; pthread_join(wt, NULL);
    if (g_group_fail) g_fail++;
    return g_group_fail;
}

static void clear_slab(int i) {
    memset(g_mem + slab(i) + S_PARAM, 0, 0x1000);
    memset(g_mem + slab(i) + S_RES, 0, 0x3f000);
}
static void set_common(int i, uint64_t barrier_slot, uint64_t n) {
    uint64_t *p = P(i);
    p[0x180/8] = barrier_slot ? RAM_IPA + OFF_GLOBAL + G_BAR + (barrier_slot - 1) * 8 : 0;
    p[0x188/8] = n;
    p[0x1f0/8] = RAM_IPA + slab(i) + S_RES;
}
static void set_handler_state(int i, uint64_t limit, uint64_t l3ipa, uint64_t cr3, uint64_t attrs) {
    uint64_t *p = P(i);
    p[0x1c0/8] = 0; p[0x1c8/8] = limit; p[0x1d0/8] = l3ipa;
    p[0x1d8/8] = cr3; p[0x1e0/8] = PA_MASK; p[0x1e8/8] = attrs;
}
static void reset_globals(void) { memset(g_mem + OFF_GLOBAL, 0, 0x4000); }

// ------------------------------------------------------------------ stats
typedef struct { double p50, p90, p99, min, max, mean; int n; } st_t;
static int dcmp(const void *a, const void *b) {
    double x = *(const double *)a, y = *(const double *)b;
    return x < y ? -1 : x > y ? 1 : 0;
}
static st_t stats(const uint64_t *ticks, int n, double per) {
    st_t s = {0};
    if (n <= 0) return s;
    double *v = malloc(sizeof(double) * (size_t)n);
    double sum = 0;
    for (int i = 0; i < n; i++) { v[i] = (double)ticks[i] * 1e9 / (double)g_cntfrq / per; sum += v[i]; }
    qsort(v, (size_t)n, sizeof(double), dcmp);
    s.p50 = v[n/2]; s.p90 = v[(int)(n*0.90)]; s.p99 = v[(int)(n*0.99)];
    s.min = v[0]; s.max = v[n-1]; s.mean = sum / n; s.n = n;
    free(v);
    return s;
}
static double med_d(double *v, int n) {
    double *c = malloc(sizeof(double)*(size_t)n);
    memcpy(c, v, sizeof(double)*(size_t)n);
    qsort(c, (size_t)n, sizeof(double), dcmp);
    double r = c[n/2]; free(c); return r;
}

// overlap witness: fraction of each vCPU's measured window that all N were simultaneously inside
static double overlap_frac(int n) {
    uint64_t lo = 0, hi = ~0ULL, span_lo = ~0ULL, span_hi = 0;
    for (int i = 0; i < n; i++) {
        uint64_t s = AUX(i)[3], e = AUX(i)[4];       // 0xc18, 0xc20
        if (s == 0 || e == 0) return -1;
        if (s > lo) lo = s;
        if (e < hi) hi = e;
        if (s < span_lo) span_lo = s;
        if (e > span_hi) span_hi = e;
    }
    if (hi <= lo) return 0.0;
    return (double)(hi - lo) / (double)(span_hi - span_lo);
}

// ==================================================================
// 1. shadow-fill scaling
// ==================================================================
static const uint64_t SH_PAGES = 512;
static uint64_t SH_GROUP = 16, SH_ROUNDS = 300, SH_CROUNDS = 60;

typedef struct { double net_p50, net_p99, ctrl_p50, raw_p50; uint64_t faults; } shres_t;

// shared_pt: 0 = private root per vCPU, 1 = one root shared by all
static int exp_shadow_run(int n, int shared_pt, int with_tlbi, shres_t *out, double *agg,
                          double *ovl, int quiet) {
    static arena_t sa;
    uint64_t roots[MAXV], l3[MAXV], sva[MAXV];
    sa.cur = OFF_SHAREDPT; sa.end = OFF_SHAREDPT_E;
    uint64_t shared_root = 0;
    if (shared_pt) shared_root = build_identity(&sa);
    for (int i = 0; i < n; i++) {
        arena_t *a; uint64_t root;
        if (shared_pt) { a = &sa; root = shared_root; }
        else {
            static arena_t pa[MAXV];
            pa[i].cur = slab(i) + S_ARENA; pa[i].end = slab(i) + S_ARENA_E;
            a = &pa[i]; root = build_identity(a);
        }
        sva[i] = VA_SHADOW + (uint64_t)i * 0x200000ULL;
        (void)walk_to(a, root, sva[i], 3);                // create an empty L3 -> faults
        memset(g_mem + (l3[i] = walk_to(a, root, sva[i], 3)), 0, 0x1000);
        l3[i] = RAM_IPA + l3[i];
        roots[i] = RAM_IPA + root;
        build_x86_tables(i, sva[i]);
    }
    reset_globals();
    for (int i = 0; i < n; i++) {
        clear_slab(i);
        uint64_t *p = P(i);
        p[0] = SH_PAGES; p[1] = SH_ROUNDS; p[2] = sva[i]; p[3] = SH_GROUP;
        p[4] = SH_CROUNDS; p[5] = RAM_IPA + slab(i) + S_RES2;
        set_common(i, 1, (uint64_t)n);
        set_handler_state(i, SH_PAGES * (SH_ROUNDS + 4) + 1024, l3[i],
                          RAM_IPA + slab(i) + S_X86PT,
                          D_PAGE | D_AF | D_SH_IS | D_UXN | D_PXN);
        g_ctx[i].mode = 1; g_ctx[i].ttbr = roots[i]; g_ctx[i].entso = 0;
        g_ctx[i].cpsr = 0x3c5; g_ctx[i].qos = QOS_CLASS_USER_INTERACTIVE;
        g_ctx[i].want_cancel_measure = 0;
    }
    install_vectors(with_tlbi ? H_SHADOW_TLBI : H_SHADOW);
    if (run_group(n, 120)) return 1;

    int nf = (int)(SH_ROUNDS * SH_PAGES / SH_GROUP);
    int nc = (int)(SH_CROUNDS * SH_PAGES / SH_GROUP);
    double sum_rate = 0;
    for (int i = 0; i < n; i++) {
        st_t f = stats(RES(i), nf, (double)SH_GROUP);
        st_t c = stats(RES2(i), nc, (double)SH_GROUP);
        out[i].raw_p50 = f.p50; out[i].ctrl_p50 = c.p50;
        out[i].net_p50 = f.p50 - c.p50;
        out[i].net_p99 = f.p99 - c.p50;
        out[i].faults  = AUX(i)[0];
        sum_rate += 1e9 / out[i].net_p50;
        if (!quiet)
            printf("    vcpu%-2d  fault %7.2f  ctrl %5.2f  NET p50 %7.2f  p99 %7.2f ns   "
                   "faults %llu/%llu\n",
                   i, f.p50, c.p50, out[i].net_p50, out[i].net_p99,
                   (unsigned long long)out[i].faults,
                   (unsigned long long)(SH_PAGES * SH_ROUNDS));
    }
    *agg = sum_rate;
    *ovl = overlap_frac(n);
    return 0;
}

static void exp_scale(void) {
    static const int Ns[] = { 1, 2, 4, 6, 8 };
    printf("\n=== 1. Shadow-fill fault handling vs vCPU count ===\n");
    printf("  %llu private 4 KB pages/vCPU/round, %llu rounds, group of %llu pages per sample.\n",
           (unsigned long long)SH_PAGES, (unsigned long long)SH_ROUNDS,
           (unsigned long long)SH_GROUP);
    printf("  Touched bytes per vCPU per round: %llu B of data (one 8 B load per page) + a 4 KB\n"
           "  shadow L3 + 16 KB of x86 tables -> ~%llu KiB of live cache lines per vCPU.\n",
           (unsigned long long)(SH_PAGES * 128), (unsigned long long)(SH_PAGES * 128 / 1024 + 20));

    for (int arm = 0; arm < 2; arm++) {
        printf("\n  -- page tables: %s\n", arm ? "SHARED root (one TTBR0/ASID for all vCPUs, "
               "shared L0/L1/L2, private L3 + private pages)"
               : "PRIVATE per vCPU (own root, own tables, own pages)");
        printf("  Each N is bracketed by a freshly measured N=1 run so DVFS/background drift\n"
               "  cannot masquerade as scaling; 'solo' is that adjacent N=1 figure.\n");
        printf("  %3s %9s %9s %9s %9s %8s %12s %6s %6s\n",
               "N", "solo ns", "p50 ns", "worst", "p99 ns", "vs solo", "agg faults/s",
               "eff", "ovl");
        for (unsigned k = 0; k < sizeof(Ns)/sizeof(Ns[0]); k++) {
            int n = Ns[k];
            if (n > g_maxv) continue;
            shres_t r1[MAXV], r[MAXV];
            double agg1 = 0, ovl1 = 0, agg = 0, ovl = 0;
            if (exp_shadow_run(1, arm, 0, r1, &agg1, &ovl1, 1)) { printf("  N=%d solo FAILED\n", n); continue; }
            if (exp_shadow_run(n, arm, 0, r,  &agg,  &ovl,  1)) { printf("  N=%d FAILED\n", n); continue; }
            double p50[MAXV], p99[MAXV];
            uint64_t bad = 0;
            for (int i = 0; i < n; i++) {
                p50[i] = r[i].net_p50; p99[i] = r[i].net_p99;
                if (r[i].faults != SH_PAGES * SH_ROUNDS) bad++;
            }
            double m50 = med_d(p50, n), m99 = med_d(p99, n);
            double mx = 0; for (int i = 0; i < n; i++) if (p50[i] > mx) mx = p50[i];
            double solo = r1[0].net_p50;
            printf("  %3d %9.2f %9.2f %9.2f %9.2f %7.3fx %12.2fM %5.0f%% %6.2f%s\n",
                   n, solo, m50, mx, m99, m50 / solo, agg / 1e6,
                   100.0 * (agg / agg1) / n, ovl,
                   bad ? "   ** FAULT COUNT MISMATCH" : "");
        }
    }
}

// ==================================================================
// 2. TLBI under real contention
// ==================================================================
static const char *tlbi_names[8] = {
    "empty loop (floor)", "isb", "dsb ishst; dsb ish; isb",
    "dsb nsh + TLBI VAE1    + dsb + isb",
    "dsb ish + TLBI VAE1IS  + dsb + isb",
    "dsb ish + TLBI VAAE1IS + dsb + isb",
    "dsb nsh + TLBI VMALLE1   + dsb + isb",
    "dsb ish + TLBI VMALLE1IS + dsb + isb",
};

// kind: 0 = contenders idle-spin, 1 = contenders walk PRIVATE 4 KB pages,
//       2 = contenders walk the SAME 4 KB pages the TLBI targets
static int exp_tlbi_run(int n, int kind, double out[8]) {
    const uint64_t ITERS = 60000, ROUNDS = 15;
    arena_t sa = { OFF_SHAREDPT, OFF_SHAREDPT_E };
    uint64_t root = build_identity(&sa);
    for (int k = 0; k < 1024; k++)
        map_page(&sa, root, VA_TLBI + (uint64_t)k * 4096,
                 RAM_IPA + OFF_TLBITGT + (uint64_t)k * 4096,
                 D_AF | D_SH_IS | D_UXN | D_PXN);
    for (int i = 0; i < n; i++)
        for (int k = 0; k < 1024; k++)
            map_page(&sa, root, VA_PRIV + (uint64_t)i * 0x400000ULL + (uint64_t)k * 4096,
                     RAM_IPA + slab(i) + S_WS + (uint64_t)k * 4096,
                     D_AF | D_SH_IS | D_UXN | D_PXN);
    reset_globals();
    for (int i = 0; i < n; i++) {
        clear_slab(i);
        uint64_t *p = P(i);
        if (i == 0) {
            p[0] = ITERS; p[1] = ROUNDS; p[2] = VA_TLBI + 64 * 4096;
            p[4] = RAM_IPA + OFF_GLOBAL + G_STOP;
            g_ctx[i].mode = 2;
        } else {
            p[0] = kind == 2 ? VA_TLBI : VA_PRIV + (uint64_t)i * 0x400000ULL;
            p[1] = 4ULL << 20;
            p[2] = RAM_IPA + OFF_GLOBAL + G_STOP;
            p[3] = kind == 0 ? 0 : 1;
            p[4] = 20000000;
            g_ctx[i].mode = 3;
        }
        set_common(i, 1, (uint64_t)n);
        set_handler_state(i, 0, 0, 0, 0);
        g_ctx[i].ttbr = RAM_IPA + root; g_ctx[i].entso = 0;
        g_ctx[i].cpsr = 0x3c5; g_ctx[i].qos = QOS_CLASS_USER_INTERACTIVE;
        g_ctx[i].want_cancel_measure = 0;
    }
    install_vectors(H_NONE);
    if (run_group(n, 180)) return 1;
    for (int v = 0; v < 8; v++)
        out[v] = stats(RES(0) + (uint64_t)v * ROUNDS, (int)ROUNDS, (double)ITERS).p50;
    return 0;
}

static void exp_tlbi(void) {
    printf("\n=== 2. Broadcast TLB maintenance vs number of other running vCPUs ===\n");
    static const int Ns[] = { 1, 2, 4, 6, 8 };
    static const char *kindname[3] = { "others IDLE-spinning",
                                       "others walking PRIVATE 4 KB pages (own TLB entries)",
                                       "others walking the SAME 4 KB pages being invalidated" };
    for (int kind = 0; kind < 3; kind++) {
        printf("\n  -- contenders: %s\n", kindname[kind]);
        printf("     every column is bracketed by a freshly measured solo (N=1) run;\n"
               "     the second table is the ratio to that adjacent solo figure.\n");
        double res[8][8], sol[8][8]; int have[8] = {0}; int nk = 0;
        for (unsigned k = 0; k < sizeof(Ns)/sizeof(Ns[0]); k++) {
            int n = Ns[k];
            if (n > g_maxv) continue;
            double out[8], so[8];
            if (exp_tlbi_run(1, 0, so)) for (int v = 0; v < 8; v++) so[v] = 0;
            if (n == 1) { for (int v = 0; v < 8; v++) out[v] = so[v]; }
            else if (exp_tlbi_run(n, kind, out)) { for (int v = 0; v < 8; v++) out[v] = 0; }
            for (int v = 0; v < 8; v++) { res[v][nk] = out[v]; sol[v][nk] = so[v]; }
            have[nk] = n; nk++;
        }
        printf("  %-38s", "N (total vCPUs)");
        for (int k = 0; k < nk; k++) printf(" %9d", have[k]);
        printf("\n");
        for (int v = 0; v < 8; v++) {
            printf("  %-38s", tlbi_names[v]);
            for (int k = 0; k < nk; k++) {
                if (res[v][k] == 0) printf("        --");
                else printf(" %9.2f", res[v][k]);
            }
            printf("\n");
        }
        printf("  %-38s", "   ratio to adjacent solo");
        for (int k = 0; k < nk; k++) printf(" %9d", have[k]);
        printf("\n");
        for (int v = 3; v < 8; v++) {
            printf("  %-38s", tlbi_names[v]);
            for (int k = 0; k < nk; k++) {
                if (res[v][k] == 0 || sol[v][k] == 0) printf("        --");
                else printf(" %8.3fx", res[v][k] / sol[v][k]);
            }
            printf("\n");
        }
    }
}

// ==================================================================
// 3. cross-vCPU signalling
// ==================================================================
static void exp_ipi(void) {
    printf("\n=== 3. Cross-vCPU signalling ===\n");

    mach_timebase_info_data_t tb; mach_timebase_info(&tb);
    double ns_per_tick = (double)tb.numer / (double)tb.denom;

    // (a) host-mediated wake, and (b) the full host-mediated IRQ into guest EL1
    for (int full = 0; full < 2; full++) {
        arena_t sa = { OFF_SHAREDPT, OFF_SHAREDPT_E };
        uint64_t root = build_identity(&sa);
        reset_globals();
        clear_slab(1);
        uint64_t *p = P(1);
        p[0] = RAM_IPA + OFF_GLOBAL + G_STOP; p[1] = 100000000;
        set_common(1, 0, 1);
        set_handler_state(1, 0, 0, 0, 0);
        g_ctx[1].mode = 8; g_ctx[1].ttbr = RAM_IPA + root; g_ctx[1].entso = 0;
        g_ctx[1].cpsr = full ? 0x345 : 0x3c5;         // 0x345 = EL1h with IRQ unmasked
        g_ctx[1].qos = QOS_CLASS_USER_INTERACTIVE;
        g_ctx[1].want_cancel_measure = full ? 2 : 1;
        install_vectors(H_SKIP);
        g_group_fail = 0;
        const int NR = 40;
        double wake[64], done[64], vec[64];
        int nn = 0, nvec = 0;
        uint64_t vtoff = 0;
        for (int r = 0; r < NR; r++) {
            pthread_t th;
            g_ctx[1].idx = 1; g_ctx[1].ready = 0;
            g_ctx[1].t_run_returned = g_ctx[1].t_irq_done = 0;
            AUX(1)[11] = 0;
            pthread_create(&th, NULL, vcpu_thread, &g_ctx[1]);
            while (!g_ctx[1].ready) usleep(200);
            usleep(3000);
            if (r == 0) hv_vcpu_get_vtimer_offset(g_ctx[1].vcpu, &vtoff);
            hv_vcpu_t v = g_ctx[1].vcpu;
            uint64_t t0 = mach_absolute_time();
            hv_vcpus_exit(&v, 1);
            pthread_join(th, NULL);
            if (g_ctx[1].t_run_returned == 0) continue;
            wake[nn] = (double)(g_ctx[1].t_run_returned - t0) * ns_per_tick;
            if (full && g_ctx[1].t_irq_done)
                done[nn] = (double)(g_ctx[1].t_irq_done - t0) * ns_per_tick;
            else done[nn] = 0;
            if (full && AUX(1)[11] && vtoff == 0)
                vec[nvec++] = (double)((int64_t)AUX(1)[11] - (int64_t)t0) * ns_per_tick;
            nn++;
        }
        if (nn == 0) { printf("  (%c) FAILED\n", full ? 'b' : 'a'); continue; }
        qsort(wake, (size_t)nn, sizeof(double), dcmp);
        if (!full) {
            printf("  (a) hv_vcpus_exit -> hv_vcpu_run returns on the target thread (the WAKE only):\n");
            printf("      p50 %8.0f ns   p10 %8.0f   p90 %8.0f   min %8.0f   (n=%d)\n",
                   wake[nn/2], wake[nn/10], wake[nn - 1 - nn/10], wake[0], nn);
        } else {
            qsort(done, (size_t)nn, sizeof(double), dcmp);
            printf("  (b) full host-mediated IPI: hv_vcpus_exit -> run returns -> "
                   "set_pending_interrupt(IRQ) -> re-enter -> guest EL1 IRQ vector:\n");
            printf("      wake                       p50 %8.0f ns\n", wake[nn/2]);
            if (nvec > 3) {
                qsort(vec, (size_t)nvec, sizeof(double), dcmp);
                printf("      guest EL1 vector reached   p50 %8.0f ns   p10 %8.0f  p90 %8.0f  "
                       "(in-guest CNTVCT stamp, vtimer_offset=0 so the timebases coincide)\n",
                       vec[nvec/2], vec[nvec/10], vec[nvec - 1 - nvec/10]);
            } else {
                printf("      guest EL1 vector stamp unusable (vtimer_offset=%llu)\n",
                       (unsigned long long)vtoff);
            }
            printf("      + trailing HVC back to host p50 %8.0f ns (whole cycle, host-timed)\n",
                   done[nn/2]);
        }
    }

    // (c) in-VM shared-memory flag ping-pong, no host involvement at all
    {
        arena_t sa = { OFF_SHAREDPT, OFF_SHAREDPT_E };
        uint64_t root = build_identity(&sa);
        reset_globals();
        const uint64_t SAMPLES = 2000, BATCH = 64;
        for (int i = 0; i < 2; i++) {
            clear_slab(i);
            uint64_t *p = P(i);
            p[0] = (uint64_t)i;
            p[1] = RAM_IPA + OFF_GLOBAL + G_PPA;
            p[2] = RAM_IPA + OFF_GLOBAL + G_PPB;
            p[3] = SAMPLES; p[4] = BATCH;
            set_common(i, 1, 2);
            set_handler_state(i, 0, 0, 0, 0);
            g_ctx[i].mode = 6; g_ctx[i].ttbr = RAM_IPA + root; g_ctx[i].entso = 0;
            g_ctx[i].cpsr = 0x3c5; g_ctx[i].qos = QOS_CLASS_USER_INTERACTIVE;
            g_ctx[i].want_cancel_measure = 0;
        }
        install_vectors(H_SKIP);
        if (!run_group(2, 60)) {
            st_t s = stats(RES(0), (int)SAMPLES, (double)BATCH);
            printf("  (c) in-VM shared-memory flag round trip (STLR/LDAR, no host exit),\n"
                   "      %llu round trips per timed window:\n", (unsigned long long)BATCH);
            printf("      round trip p50 %7.1f ns  p90 %7.1f  p99 %7.1f  min %7.1f\n",
                   s.p50, s.p90, s.p99, s.min);
            printf("      one-way    p50 %7.1f ns\n", s.p50 / 2);
        }
    }
}

// ==================================================================
// 4. TSO litmus at scale + EnTSO cost under contention
// ==================================================================
static long long exp_litmus_run(int n, int shared_var, int entso, uint64_t iters,
                                uint64_t *witness_out, uint64_t *actlr_out) {
    arena_t sa = { OFF_SHAREDPT, OFF_SHAREDPT_E };
    uint64_t root = build_identity(&sa);
    reset_globals();
    for (int i = 0; i < n; i++) {
        clear_slab(i);
        uint64_t *p = P(i);
        int role, pair;
        if (shared_var) { role = (i == 0) ? 0 : 1; pair = 0; }
        else            { role = i & 1; pair = i / 2; }
        p[0] = (uint64_t)role;
        p[1] = RAM_IPA + OFF_GLOBAL + G_LIT + (uint64_t)pair * 0x200;
        p[2] = iters;
        set_common(i, 1, (uint64_t)n);
        set_handler_state(i, 0, 0, 0, 0);
        g_ctx[i].mode = 4; g_ctx[i].ttbr = RAM_IPA + root; g_ctx[i].entso = (uint64_t)entso;
        g_ctx[i].cpsr = 0x3c5; g_ctx[i].qos = QOS_CLASS_USER_INTERACTIVE;
        g_ctx[i].want_cancel_measure = 0;
    }
    install_vectors(H_SKIP);
    if (run_group(n, 120)) return -1;
    long long viol = 0; uint64_t wit = 0;
    for (int i = 0; i < n; i++) { viol += (long long)AUX(i)[9]; wit += AUX(i)[10]; }
    *witness_out = wit;
    *actlr_out = AUX(0)[1];
    return viol;
}

static void exp_litmus(void) {
    printf("\n=== 4a. MP litmus at scale (r0(Y) > r1(X) is forbidden under TSO) ===\n");
    const uint64_t IT = 3000000;
    static const int Ns[] = { 2, 4, 8 };
    printf("  %4s %-28s %10s %14s %14s\n", "N", "variant", "EnTSO", "weak outcomes", "witness");
    for (unsigned k = 0; k < sizeof(Ns)/sizeof(Ns[0]); k++) {
        for (int sv = 0; sv < 2; sv++) {
            int n = Ns[k];
            if (n > g_maxv) continue;
            for (int e = 0; e < 2; e++) {
                uint64_t wit = 0, actlr = 0;
                long long v = exp_litmus_run(n, sv, e, IT, &wit, &actlr);
                printf("  %4d %-28s %10s %14lld %14llu%s\n", n,
                       sv ? "1 writer / N-1 readers" : "N/2 independent pairs",
                       e ? "1" : "0", v, (unsigned long long)wit,
                       (e && ((actlr >> 1) & 1) == 0) ? "  ** EnTSO NOT SET" : "");
            }
        }
    }
}

static void exp_entso_cost(void) {
    printf("\n=== 4b. Cost of EnTSO=1 under multi-core cache-line contention ===\n");
    const uint64_t ROUNDS = 21, PASSES = 8;
    static const int Ns[] = { 1, 2, 4, 8 };
    // arms: 0 = private 64 KiB buffer (no sharing), 1 = shared 64 KiB buffer (true sharing)
    for (int arm = 0; arm < 2; arm++) {
        for (int kind = 0; kind < 2; kind++) {          // 0 = stp stores, 1 = ldp loads
            printf("\n  -- %s buffer, %s, 64 KiB\n",
                   arm ? "SHARED (all vCPUs write the same lines)" : "private per vCPU",
                   kind ? "streaming ldp" : "streaming stp");
            printf("  %4s %14s %14s %10s\n", "N", "EnTSO=0 ns/8B", "EnTSO=1 ns/8B", "ratio");
            for (unsigned k = 0; k < sizeof(Ns)/sizeof(Ns[0]); k++) {
                int n = Ns[k];
                if (n > g_maxv) continue;
                double got[2] = {0,0};
                for (int e = 0; e < 2; e++) {
                    arena_t sa = { OFF_SHAREDPT, OFF_SHAREDPT_E };
                    uint64_t root = build_identity(&sa);
                    reset_globals();
                    uint64_t BYTES = 64 << 10;
                    for (int i = 0; i < n; i++) {
                        clear_slab(i);
                        uint64_t *p = P(i);
                        p[0] = arm ? RAM_IPA + OFF_SHAREDWS : RAM_IPA + slab(i) + S_WS;
                        p[1] = BYTES; p[2] = ROUNDS; p[3] = PASSES; p[4] = (uint64_t)kind;
                        set_common(i, 1, (uint64_t)n);
                        set_handler_state(i, 0, 0, 0, 0);
                        g_ctx[i].mode = 5; g_ctx[i].ttbr = RAM_IPA + root;
                        g_ctx[i].entso = (uint64_t)e; g_ctx[i].cpsr = 0x3c5;
                        g_ctx[i].qos = QOS_CLASS_USER_INTERACTIVE;
                        g_ctx[i].want_cancel_measure = 0;
                    }
                    install_vectors(H_SKIP);
                    if (run_group(n, 90)) continue;
                    double per[MAXV];
                    for (int i = 0; i < n; i++)
                        per[i] = stats(RES(i), (int)ROUNDS, (double)(BYTES / 8 * PASSES)).p50;
                    got[e] = med_d(per, n);
                }
                printf("  %4d %14.4f %14.4f %10.3fx\n", n, got[0], got[1],
                       got[0] > 0 ? got[1] / got[0] : 0.0);
            }
        }
    }
}

// ==================================================================
// 5. P vs E core placement
// ==================================================================
static void exp_pe(void) {
    printf("\n=== 5. P-core vs E-core placement of vCPU threads ===\n");
    const uint64_t IT = 200000, ROUNDS = 11;
    static const struct { qos_class_t q; const char *name; } qoses[] = {
        { QOS_CLASS_USER_INTERACTIVE, "USER_INTERACTIVE" },
        { QOS_CLASS_UTILITY,          "UTILITY" },
        { QOS_CLASS_BACKGROUND,       "BACKGROUND" },
    };
    printf("  16-deep dependent ADD chain; 1 cycle/ADD -> clock estimate.\n");
    printf("  %-20s %4s %14s %12s %10s\n", "thread QoS", "N", "ns per ADD", "est GHz", "spread");
    for (unsigned q = 0; q < sizeof(qoses)/sizeof(qoses[0]); q++) {
        static const int Ns[] = { 1, 8 };
        for (unsigned k = 0; k < sizeof(Ns)/sizeof(Ns[0]); k++) {
            int n = Ns[k];
            if (n > g_maxv) continue;
            arena_t sa = { OFF_SHAREDPT, OFF_SHAREDPT_E };
            uint64_t root = build_identity(&sa);
            reset_globals();
            for (int i = 0; i < n; i++) {
                clear_slab(i);
                uint64_t *p = P(i);
                p[0] = IT; p[1] = ROUNDS;
                set_common(i, 1, (uint64_t)n);
                set_handler_state(i, 0, 0, 0, 0);
                g_ctx[i].mode = 7; g_ctx[i].ttbr = RAM_IPA + root; g_ctx[i].entso = 0;
                g_ctx[i].cpsr = 0x3c5; g_ctx[i].qos = qoses[q].q;
                g_ctx[i].want_cancel_measure = 0;
            }
            install_vectors(H_SKIP);
            if (run_group(n, 90)) continue;
            double per[MAXV], lo = 1e9, hi = 0;
            for (int i = 0; i < n; i++) {
                per[i] = stats(RES(i), (int)ROUNDS, (double)(IT * 16)).p50;
                if (per[i] < lo) lo = per[i];
                if (per[i] > hi) hi = per[i];
            }
            double m = med_d(per, n);
            printf("  %-20s %4d %14.4f %12.2f %9.2fx\n", qoses[q].name, n, m, 1.0 / m,
                   lo > 0 ? hi / lo : 0.0);
            if (n > 1) {
                printf("      per-vCPU GHz:");
                for (int i = 0; i < n; i++) printf(" %.2f", 1.0 / per[i]);
                printf("\n");
            }
            if (q == 0 && n == 1) g_ghz = 1.0 / m;
        }
    }
    // mixed: half USER_INTERACTIVE, half BACKGROUND, running together
    {
        int n = g_maxv >= 8 ? 8 : g_maxv;
        arena_t sa = { OFF_SHAREDPT, OFF_SHAREDPT_E };
        uint64_t root = build_identity(&sa);
        reset_globals();
        for (int i = 0; i < n; i++) {
            clear_slab(i);
            uint64_t *p = P(i);
            p[0] = IT; p[1] = ROUNDS;
            set_common(i, 1, (uint64_t)n);
            set_handler_state(i, 0, 0, 0, 0);
            g_ctx[i].mode = 7; g_ctx[i].ttbr = RAM_IPA + root; g_ctx[i].entso = 0;
            g_ctx[i].cpsr = 0x3c5;
            g_ctx[i].qos = (i < n/2) ? QOS_CLASS_USER_INTERACTIVE : QOS_CLASS_BACKGROUND;
            g_ctx[i].want_cancel_measure = 0;
        }
        install_vectors(H_SKIP);
        if (!run_group(n, 90)) {
            printf("  mixed QoS (%d interactive + %d background), per-vCPU GHz:", n/2, n - n/2);
            for (int i = 0; i < n; i++)
                printf(" %.2f", 1.0 / stats(RES(i), (int)ROUNDS, (double)(IT * 16)).p50);
            printf("\n");
        }
    }
}

// ==================================================================
// 6. oversubscription
// ==================================================================
static volatile int g_busy_stop;
static void *busy_thread(void *a) {
    (void)a;
    pthread_set_qos_class_self_np(QOS_CLASS_USER_INTERACTIVE, 0);
    volatile uint64_t x = 0;
    while (!g_busy_stop) for (int i = 0; i < 100000; i++) x += i;
    return NULL;
}

static void exp_oversub(void) {
    printf("\n=== 6. Oversubscription: vCPUs (+ host threads) beyond 8 P-cores ===\n");
    printf("  %4s %6s %10s %10s %10s %10s %12s %8s\n",
           "N", "busy", "p50 ns", "p90 ns", "p99 ns", "max ns", "agg faults/s", "ovl");
    struct { int n, busy; } cases[] = {
        { 8, 0 }, { 8, 4 }, { 8, 8 }, { 10, 0 }, { 12, 0 }, { 12, 4 },
    };
    for (unsigned k = 0; k < sizeof(cases)/sizeof(cases[0]); k++) {
        int n = cases[k].n, nb = cases[k].busy;
        if (n > g_maxv) continue;
        pthread_t bt[16];
        g_busy_stop = 0;
        for (int i = 0; i < nb; i++) pthread_create(&bt[i], NULL, busy_thread, NULL);
        shres_t r[MAXV]; double agg = 0, ovl = 0;
        int rc = exp_shadow_run(n, 0, 0, r, &agg, &ovl, 1);
        g_busy_stop = 1;
        for (int i = 0; i < nb; i++) pthread_join(bt[i], NULL);
        if (rc) { printf("  N=%d busy=%d FAILED\n", n, nb); continue; }
        double p50[MAXV], p99[MAXV];
        double mx = 0;
        for (int i = 0; i < n; i++) {
            p50[i] = r[i].net_p50; p99[i] = r[i].net_p99;
            if (p50[i] > mx) mx = p50[i];
        }
        printf("  %4d %6d %10.2f %10.2f %10.2f %10.2f %12.2fM %8.2f\n",
               n, nb, med_d(p50, n), mx, med_d(p99, n), mx, agg / 1e6, ovl);
    }
}

// ==================================================================
static void exp_baseline(void) {
    printf("\n=== 0. Single-vCPU cross-check against doc 16 ===\n");
    shres_t r[MAXV]; double agg = 0, ovl = 0;
    printf("  no-TLBI shadow-fill handler, one timed window per 512-page round (doc-16 method):\n");
    uint64_t sg = SH_GROUP; SH_GROUP = SH_PAGES;
    if (!exp_shadow_run(1, 0, 0, r, &agg, &ovl, 0))
        printf("    doc 16 measured 145.8 ns here.\n");
    SH_GROUP = sg;
    printf("  no-TLBI shadow-fill handler, one timed window per %llu pages (this probe):\n",
           (unsigned long long)SH_GROUP);
    if (!exp_shadow_run(1, 0, 0, r, &agg, &ovl, 0))
        printf("    (finer windows so p99 tails are meaningful; costs a few ns of ISB overhead)\n");
    printf("  with TLBI VAAE1IS in the handler:\n");
    if (!exp_shadow_run(1, 0, 1, r, &agg, &ovl, 0))
        printf("    doc 16 measured 439.2 ns here.\n");
}

static void exp_scale_tlbi_handler(void) {
    printf("\n=== 1b. Same, with the naive handler that DOES issue TLBI VAAE1IS ===\n");
    static const int Ns[] = { 1, 2, 4, 8 };
    printf("  %3s %10s %10s %10s %12s\n", "N", "p50 ns", "max ns", "p99 ns", "agg faults/s");
    double base = 0;
    for (unsigned k = 0; k < sizeof(Ns)/sizeof(Ns[0]); k++) {
        int n = Ns[k];
        if (n > g_maxv) continue;
        shres_t r[MAXV]; double agg = 0, ovl = 0;
        if (exp_shadow_run(n, 0, 1, r, &agg, &ovl, 1)) { printf("  N=%d FAILED\n", n); continue; }
        double p50[MAXV], p99[MAXV], mx = 0;
        for (int i = 0; i < n; i++) {
            p50[i] = r[i].net_p50; p99[i] = r[i].net_p99;
            if (p50[i] > mx) mx = p50[i];
        }
        double m = med_d(p50, n);
        if (base == 0) base = m;
        printf("  %3d %10.2f %10.2f %10.2f %12.2fM   (%.2fx N=1)\n",
               n, m, mx, med_d(p99, n), agg / 1e6, m / base);
    }
}

int main(int argc, char **argv) {
    const char *what = argc > 1 ? argv[1] : "all";

    uint32_t maxv = 0;
    hv_vm_get_max_vcpu_count(&maxv);
    printf("=== SMP scaling probe ===\n");
    {
        char brand[128]; size_t bl = sizeof brand;
        if (sysctlbyname("machdep.cpu.brand_string", brand, &bl, NULL, 0) == 0)
            printf("  host: %s\n", brand);
        int np = 0, ne = 0; size_t l = sizeof(int);
        sysctlbyname("hw.perflevel0.physicalcpu", &np, &l, NULL, 0);
        l = sizeof(int);
        sysctlbyname("hw.perflevel1.physicalcpu", &ne, &l, NULL, 0);
        printf("  cores: %d performance + %d efficiency\n", np, ne);
    }
    printf("  hv_vm_get_max_vcpu_count = %u\n", maxv);
    g_maxv = (int)(maxv < MAXV ? maxv : MAXV);

    HV(hv_vm_create(NULL));
    g_mem = mmap(NULL, RAM_SIZE, PROT_READ | PROT_WRITE, MAP_ANON | MAP_PRIVATE, -1, 0);
    if (g_mem == MAP_FAILED) { perror("mmap"); return 2; }
    memset(g_mem, 0, RAM_SIZE);
    HV(hv_vm_map(g_mem, RAM_IPA, RAM_SIZE, HV_MEMORY_READ | HV_MEMORY_WRITE | HV_MEMORY_EXEC));
    memcpy(g_mem + OFF_CODE, sp_start, (size_t)(sp_end - sp_start));
    printf("  payload %zu bytes\n", (size_t)(sp_end - sp_start));

    int all = strcmp(what, "all") == 0;
    if (all || !strcmp(what, "pe"))      exp_pe();
    if (all || !strcmp(what, "base"))    exp_baseline();
    if (all || !strcmp(what, "scale"))   exp_scale();
    if (all || !strcmp(what, "scale2"))  exp_scale_tlbi_handler();
    if (all || !strcmp(what, "tlbi"))    exp_tlbi();
    if (all || !strcmp(what, "ipi"))     exp_ipi();
    if (all || !strcmp(what, "litmus"))  exp_litmus();
    if (all || !strcmp(what, "entso"))   exp_entso_cost();
    if (all || !strcmp(what, "oversub")) exp_oversub();

    hv_vm_destroy();
    printf("\n  failures: %d\n", g_fail);
    return g_fail ? 1 : 0;
}
