// Shadow-MMU cost probe.  Measures, INSIDE a Hypervisor.framework VM at guest EL1 with our own
// VBAR_EL1, the actual per-operation costs the Tier 2 shadow-paging design would pay:
//
//   cal     CNTVCT granularity/overhead + a clock estimate from a dependent-ADD chain
//   fault   in-VM stage-1 fault round trip (translation / permission / no-fault control)
//   tlbi    TLBI VAE1 / VAE1IS / VAAE1IS / VMALLE1 / VMALLE1IS, alone and under contention
//   reach   4 KB vs 16 KB stage-1 TLB reach, same physical working set, same access stream
//   shadow  full shadow-fill handler (ESR/FAR -> 4-level x86 walk -> descriptor -> TLBI -> ERET)
//   asid    ASID switch vs ASID rollover (TLBI VMALLE1IS + refill)
//   ad      AF-fault-driven Accessed-bit emulation + AT S1E1R/W/RP/WP
//   tso     cost of running with ACTLR_EL1.EnTSO = 1
//
// Timing is CNTVCT_EL0 read in-guest, so no host exit occurs inside a measured window.
//
// Build: ../build.sh shadow_mmu/shadow_mmu.c

#include <Hypervisor/Hypervisor.h>
#include <inttypes.h>
#include <pthread.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <sys/mman.h>
#include <pthread/qos.h>

#include "payload.h"
__asm__(PAYLOAD_ASM);

extern const uint8_t sm_start[], sm_end[];
extern const uint8_t h_skip[], h_skip_cnt[], h_shadow[], h_shadow_notlbi[];
extern const uint8_t h_af[], h_af_notlbi[], h_panic[];

// ------------------------------------------------------------------ memory map
#define RAM_IPA        0x40000000ULL
#define RAM_SIZE       0x14000000ULL          // 320 MiB

#define OFF_CODE       0x00000000ULL
#define OFF_VEC        0x00020000ULL          // 2 KB aligned
#define OFF_PARAM      0x00021000ULL          // 8 KB param/result block
#define OFF_STOP       0x00023000ULL
#define OFF_ARENA      0x00100000ULL          // page-table arena, 16 MiB
#define OFF_ARENA_END  0x01100000ULL
#define OFF_X86PT      0x01100000ULL          // fake x86 4-level tables
#define OFF_SHADOWBK   0x01200000ULL          // 2 MiB backing for the shadow-fill region
#define OFF_SPINBUF    0x01400000ULL          // 4 MiB, second vCPU
#define OFF_WS         0x02000000ULL          // working set, up to 256 MiB

// guest VAs (all under L0[128] in the 4 KB regime)
#define VA_WS          0x0000400000000000ULL
#define VA_SHADOW      0x0000401000000000ULL
#define VA_UNMAP       0x0000402000000000ULL
#define VA_RO          0x0000403000000000ULL
#define VA_RW          0x0000404000000000ULL
#define VA_SPIN        0x0000405000000000ULL

// descriptor bits
#define D_PAGE   3ULL
#define D_BLOCK  1ULL
#define D_AF     (1ULL << 10)
#define D_SH_IS  (3ULL << 8)
#define D_AP_RO  (2ULL << 6)
#define D_NG     (1ULL << 11)
#define D_UXN    (1ULL << 54)
#define D_PXN    (1ULL << 53)
#define PA_MASK  0x0000fffffffff000ULL

#define MAIR_VAL 0xFFULL
// T0SZ=16, IRGN0/ORGN0=WB, SH0=IS, T1SZ=16, EPD1=1, TG1=4K, IPS=40bit
#define TCR_BASE (16ULL | (1ULL<<8) | (1ULL<<10) | (3ULL<<12) \
                 | (16ULL<<16) | (1ULL<<23) | (2ULL<<30) | (2ULL<<32))
#define TCR_4K   (TCR_BASE | (0ULL<<14))
#define TCR_16K  (TCR_BASE | (2ULL<<14))

#define HV(e) do { hv_return_t _r = (e); if (_r != HV_SUCCESS) { \
    fprintf(stderr, "  %s -> 0x%x at %d\n", #e, _r, __LINE__); exit(2);} } while (0)

static uint8_t   *g_mem;
static uint64_t   g_alloc;
static uint64_t   g_cntfrq = 24000000;
static double     g_ghz    = 0.0;      // estimated guest clock, from mode 0
static int        g_fail   = 0;
static uint64_t   g_msr_trapped = 0;
static uint64_t   g_msr_traps = 0;

static uint64_t *P(void)   { return (uint64_t *)(g_mem + OFF_PARAM); }
static uint64_t *R(void)   { return (uint64_t *)(g_mem + OFF_PARAM + 0x200); }
static uint64_t *AUX(void) { return (uint64_t *)(g_mem + OFF_PARAM + 0xc00); }

// ------------------------------------------------------------------ page tables
static void reset_arena(void) { g_alloc = OFF_ARENA; }

static uint64_t alloc_tab(int g) {
    uint64_t psz = 1ULL << g;
    g_alloc = (g_alloc + psz - 1) & ~(psz - 1);
    uint64_t off = g_alloc;
    g_alloc += psz;
    if (g_alloc > OFF_ARENA_END) { fprintf(stderr, "arena exhausted\n"); exit(2); }
    memset(g_mem + off, 0, (size_t)psz);
    return off;
}
static uint64_t *tab(uint64_t off) { return (uint64_t *)(g_mem + off); }

// Walk to the table at `level` (0..3), creating tables as needed. Returns its RAM offset.
static uint64_t walk_to(uint64_t root, uint64_t va, int level, int g) {
    int b = g - 3;
    int sh[4] = { g + 3*b, g + 2*b, g + b, g };
    int nb[4] = { 48 - (g + 3*b), b, b, b };
    uint64_t cur = root;
    for (int l = 0; l < level; l++) {
        uint64_t idx = (va >> sh[l]) & ((1ULL << nb[l]) - 1);
        uint64_t *t = tab(cur);
        if ((t[idx] & 1) == 0) t[idx] = (RAM_IPA + alloc_tab(g)) | D_PAGE;
        cur = (t[idx] & PA_MASK) - RAM_IPA;
    }
    return cur;
}
static void map_page(uint64_t root, uint64_t va, uint64_t pa, uint64_t attrs, int g) {
    int b = g - 3;
    uint64_t t3 = walk_to(root, va, 3, g);
    uint64_t idx = (va >> g) & ((1ULL << b) - 1);
    tab(t3)[idx] = (pa & PA_MASK) | attrs | D_PAGE;
}
static void map_block2(uint64_t root, uint64_t va, uint64_t pa, uint64_t attrs, int g) {
    int b = g - 3;
    uint64_t t2 = walk_to(root, va, 2, g);
    uint64_t idx = (va >> (g + b)) & ((1ULL << b) - 1);
    tab(t2)[idx] = (pa & PA_MASK) | attrs | D_BLOCK;
}
static uint64_t l3_ipa(uint64_t root, uint64_t va, int g) {
    return RAM_IPA + walk_to(root, va, 3, g);
}

// Identity-map all of guest RAM with L2 blocks so code/params/tables stay reachable.
static uint64_t build_identity(int g) {
    reset_arena();
    uint64_t root = alloc_tab(g);
    uint64_t bsz  = 1ULL << (g + (g - 3));          // 2 MiB at 4K, 32 MiB at 16K
    for (uint64_t o = 0; o < RAM_SIZE; o += bsz)
        map_block2(root, RAM_IPA + o, RAM_IPA + o, D_AF | D_SH_IS, g);   // RW + executable
    return root;
}

// ------------------------------------------------------------------ guest runner
typedef struct { hv_vcpu_t vcpu; volatile int done; int secs; } wd_t;
static void *watchdog(void *p) {
    wd_t *w = p;
    for (int i = 0; i < w->secs * 20 && !w->done; i++) usleep(50000);
    if (!w->done) {
        fprintf(stderr, "  !! watchdog: cancelling vCPU after %d s\n", w->secs);
        hv_vcpus_exit((hv_vcpu_t *)&w->vcpu, 1);
    }
    return NULL;
}

// handler selection
enum { H_NONE = 0, H_SKIP_INLINE, H_SKIP, H_SKIP_CNT, H_SHADOW, H_SHADOW_NOTLBI, H_AF, H_AF_NOTLBI };

static void install_vectors(int which) {
    uint32_t *v = (uint32_t *)(g_mem + OFF_VEC);
    uint64_t panic_off = OFF_CODE + (uint64_t)(h_panic - sm_start);
    for (int i = 0; i < 16; i++) {
        int64_t d = (int64_t)panic_off - (int64_t)(OFF_VEC + i * 0x80);
        v[i * 0x20] = 0x14000000u | (uint32_t)((d >> 2) & 0x03ffffff);
    }
    if (which == H_NONE) return;
    const uint8_t *h = NULL;
    switch (which) {
        case H_SKIP_INLINE: case H_SKIP: h = h_skip; break;
        case H_SKIP_CNT:   h = h_skip_cnt;   break;
        case H_SHADOW:     h = h_shadow;     break;
        case H_SHADOW_NOTLBI: h = h_shadow_notlbi; break;
        case H_AF:         h = h_af;         break;
        case H_AF_NOTLBI:  h = h_af_notlbi;  break;
    }
    uint64_t slot = OFF_VEC + 0x200;                       // current EL, SP_ELx, synchronous
    if (which == H_SKIP_INLINE) {
        memcpy(g_mem + slot, h_skip, 16);                  // 4 instructions, no branch
    } else {
        uint64_t hoff = OFF_CODE + (uint64_t)(h - sm_start);
        int64_t d = (int64_t)hoff - (int64_t)slot;
        *(uint32_t *)(g_mem + slot) = 0x14000000u | (uint32_t)((d >> 2) & 0x03ffffff);
    }
}

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

// Runs the payload to its terminating HVC #0. Returns 0 ok, 1 guest panic/timeout.
static int run_guest(uint64_t mode, uint64_t tcr, uint64_t ttbr, uint64_t entso, int secs) {
    hv_vcpu_t vcpu; hv_vcpu_exit_t *vexit;
    HV(hv_vcpu_create(&vcpu, &vexit, NULL));
    HV(hv_vcpu_set_reg(vcpu, HV_REG_PC,   RAM_IPA + OFF_CODE));
    HV(hv_vcpu_set_reg(vcpu, HV_REG_CPSR, 0x3c5));                 // EL1h, DAIF masked
    HV(hv_vcpu_set_reg(vcpu, HV_REG_X0,   MAIR_VAL));
    HV(hv_vcpu_set_reg(vcpu, HV_REG_X1,   tcr));
    HV(hv_vcpu_set_reg(vcpu, HV_REG_X2,   ttbr));
    HV(hv_vcpu_set_reg(vcpu, HV_REG_X3,   RAM_IPA + OFF_VEC));
    HV(hv_vcpu_set_reg(vcpu, HV_REG_X4,   RAM_IPA + OFF_PARAM));
    HV(hv_vcpu_set_reg(vcpu, HV_REG_X5,   mode));
    HV(hv_vcpu_set_reg(vcpu, HV_REG_X6,   entso));

    wd_t w = { .vcpu = vcpu, .done = 0, .secs = secs };
    pthread_t wt; pthread_create(&wt, NULL, watchdog, &w);

    int rc = 0;
    for (;;) {
        hv_return_t r = hv_vcpu_run(vcpu);
        if (r != HV_SUCCESS) { fprintf(stderr, "  hv_vcpu_run -> 0x%x\n", r); rc = 1; break; }
        if (vexit->reason == HV_EXIT_REASON_VTIMER_ACTIVATED) { hv_vcpu_set_vtimer_mask(vcpu, true); continue; }
        if (vexit->reason == HV_EXIT_REASON_CANCELED) {
            fprintf(stderr, "  !! guest cancelled (watchdog) in mode %llu\n",
                    (unsigned long long)mode);
            rc = 1; break;
        }
        if (vexit->reason != HV_EXIT_REASON_EXCEPTION) {
            fprintf(stderr, "  !! unexpected exit reason %u\n", vexit->reason); rc = 1; break;
        }
        uint64_t sy = vexit->exception.syndrome, ec = (sy >> 26) & 0x3f;
        if (ec == 0x16 && (sy & 0xffff) == 0) break;                // normal completion
        if (ec == 0x18) {
            // A system-register access HVF traps to EL2 rather than letting EL1 have it.
            // Skip it and record the fact (used by the PMCCNTR_EL0 reachability probe).
            uint64_t pc = 0; hv_vcpu_get_reg(vcpu, HV_REG_PC, &pc);
            hv_vcpu_set_reg(vcpu, HV_REG_PC, pc + 4);
            hv_vcpu_set_reg(vcpu, HV_REG_X27, 1);
            g_msr_trapped = sy; g_msr_traps++;
            continue;
        }
        if (ec == 0x16 && (sy & 0xffff) == 9) {
            uint64_t esr = 0, far = 0, elr = 0, pc = 0;
            hv_vcpu_get_reg(vcpu, HV_REG_X25, &esr);
            hv_vcpu_get_reg(vcpu, HV_REG_X26, &far);
            hv_vcpu_get_reg(vcpu, HV_REG_X27, &elr);
            hv_vcpu_get_reg(vcpu, HV_REG_PC, &pc);
            fprintf(stderr, "  !! guest PANIC mode %llu: ESR=0x%llx EC=0x%llx (%s) FAR=0x%llx "
                            "ELR=0x%llx PC=0x%llx\n",
                    (unsigned long long)mode, (unsigned long long)esr,
                    (unsigned long long)((esr >> 26) & 0x3f), ecname((esr >> 26) & 0x3f),
                    (unsigned long long)far, (unsigned long long)elr, (unsigned long long)pc);
            rc = 1; break;
        }
        uint64_t pc = 0; hv_vcpu_get_reg(vcpu, HV_REG_PC, &pc);
        fprintf(stderr, "  !! unexpected exit ESR=0x%llx EC=0x%llx (%s) PC=0x%llx\n",
                (unsigned long long)sy, (unsigned long long)ec, ecname(ec),
                (unsigned long long)pc);
        rc = 1; break;
    }
    w.done = 1; pthread_join(wt, NULL);
    if (g_msr_traps > 1)
        fprintf(stderr, "  !! %llu system-register/instruction traps to EL2 during mode %llu\n",
                (unsigned long long)g_msr_traps, (unsigned long long)mode);
    g_msr_traps = 0;
    hv_vcpu_destroy(vcpu);
    if (rc) g_fail++;
    return rc;
}

// ------------------------------------------------------------------ stats
typedef struct { double med, lo, hi, min; int n; } stat_t;
static int dcmp(const void *a, const void *b) {
    double x = *(const double *)a, y = *(const double *)b;
    return x < y ? -1 : x > y ? 1 : 0;
}
static stat_t stats(const uint64_t *ticks, int n, double per) {
    double *v = malloc(sizeof(double) * (size_t)n);
    for (int i = 0; i < n; i++) v[i] = (double)ticks[i] * 1e9 / (double)g_cntfrq / per;
    qsort(v, (size_t)n, sizeof(double), dcmp);
    stat_t s = { .med = v[n/2], .lo = v[n/10], .hi = v[n - 1 - n/10], .min = v[0], .n = n };
    free(v);
    return s;
}
static void row(const char *label, stat_t s) {
    if (g_ghz > 0)
        printf("  %-42s %9.2f ns   [%.2f .. %.2f]  min %.2f   %8.1f cyc\n",
               label, s.med, s.lo, s.hi, s.min, s.med * g_ghz);
    else
        printf("  %-42s %9.2f ns   [%.2f .. %.2f]  min %.2f\n",
               label, s.med, s.lo, s.hi, s.min);
}
static double medof(const uint64_t *t, int n, double per) { return stats(t, n, per).med; }

// ------------------------------------------------------------------ helpers
static void clear_params(void) { memset(g_mem + OFF_PARAM, 0, 0x2000); }
static void set_handler_state(uint64_t limit, uint64_t l3va, uint64_t cr3, uint64_t attrs) {
    uint64_t *p = P();
    p[0x1c0/8] = 0;
    p[0x1c8/8] = limit;
    p[0x1d0/8] = l3va;
    p[0x1d8/8] = cr3;
    p[0x1e0/8] = PA_MASK;
    p[0x1e8/8] = attrs;
}

// ------------------------------------------------------------------ experiment: calibration
static void exp_cal(void) {
    printf("\n=== 0. calibration (CNTVCT granularity, loop floor, clock estimate) ===\n");
    uint64_t root = build_identity(12);
    clear_params();
    P()[0] = 200000;
    set_handler_state(0, 0, 0, 0);
    install_vectors(H_SKIP_CNT);
    if (run_guest(0, TCR_4K, RAM_IPA + root, 0, 20)) return;

    g_cntfrq = AUX()[3];   // 0xc18
    uint64_t N = 200000;
    double t_cnt   = (double)R()[0] * 1e9 / g_cntfrq / N;
    double t_empty = (double)R()[1] * 1e9 / g_cntfrq / N;
    double t_add   = (double)R()[2] * 1e9 / g_cntfrq / (N * 16);
    g_ghz = 1.0 / t_add;

    printf("  CNTFRQ_EL0                                 %llu Hz  (%.2f ns/tick)\n",
           (unsigned long long)g_cntfrq, 1e9 / (double)g_cntfrq);
    printf("  {isb; mrs cntvct_el0} back to back         %.2f ns\n", t_cnt);
    printf("  empty {subs; b.ne} loop iteration          %.3f ns\n", t_empty);
    printf("  dependent ADD (1-cycle ALU latency)        %.4f ns  -> clock ~ %.2f GHz\n",
           t_add, g_ghz);
    printf("  PMCCNTR_EL0 from guest EL1                 %s\n",
           g_msr_trapped ? "TRAPS TO EL2 (host exit) - unusable in-guest" :
           R()[4] ? "traps to our EL1 vector (undef)" : "readable");
    printf("  NOTE: every timed window below is bracketed by two {isb; mrs cntvct} pairs,\n"
           "        so ~%.0f ns of read overhead is included once per ROUND, not per iteration.\n",
           t_cnt);
}

// ------------------------------------------------------------------ experiment: fault round trip
static void build_fault_regions(uint64_t root) {
    uint64_t dat = RAM_IPA + OFF_SHADOWBK;
    // VA_UNMAP: create the L3 table but leave every entry invalid -> translation fault, level 3
    (void)walk_to(root, VA_UNMAP, 3, 12);
    for (int i = 0; i < 512; i++) {
        map_page(root, VA_RO + (uint64_t)i * 4096, dat + (uint64_t)i * 4096,
                 D_AF | D_SH_IS | D_AP_RO | D_UXN | D_PXN, 12);
        map_page(root, VA_RW + (uint64_t)i * 4096, dat + (uint64_t)i * 4096,
                 D_AF | D_SH_IS | D_UXN | D_PXN, 12);
    }
}

static void exp_fault(void) {
    printf("\n=== 1. in-VM stage-1 fault round trip (EL1 fault -> our VBAR -> ERET) ===\n");
    const uint64_t ITERS = 200000, ROUNDS = 21;
    struct { const char *name; int h; uint64_t mask; } cfg[] = {
        { "COLD: 64 pages, first config of the process", H_SKIP,  63  },
        { "warm: 1 faulting page",                H_SKIP_INLINE, 0   },
        { "warm: 64 faulting pages",              H_SKIP_INLINE, 63  },
        { "warm: 512 faulting pages (> L1 dTLB)", H_SKIP_INLINE, 511 },
        { "warm: 64 pages, out-of-line handler",  H_SKIP,        63  },
        { "warm: 64 pages, inline handler again", H_SKIP_INLINE, 63  },
    };
    printf("  The permission-fault path is bimodal: it is cheap when the translation is already\n"
           "  TLB/cache-resident and ~4x dearer when the walk data is cold or the page count\n"
           "  exceeds the L1 dTLB. A translation fault always pays a full 4-level walk because\n"
           "  VMSAv8-64 never caches an invalid descriptor. Both modes are reported.\n");
    for (unsigned c = 0; c < sizeof(cfg)/sizeof(cfg[0]); c++) {
        uint64_t root = build_identity(12);
        build_fault_regions(root);
        clear_params();
        uint64_t *p = P();
        p[0] = VA_UNMAP; p[1] = VA_RO; p[2] = VA_RW;
        p[3] = ITERS; p[4] = ROUNDS; p[5] = cfg[c].mask;
        set_handler_state(0, 0, 0, 0);
        install_vectors(cfg[c].h);
        if (run_guest(1, TCR_4K, RAM_IPA + root, 0, 60)) continue;

        stat_t tf = stats(R() + 0,          (int)ROUNDS, (double)ITERS);
        stat_t pf = stats(R() + ROUNDS,     (int)ROUNDS, (double)ITERS);
        stat_t nf = stats(R() + 2 * ROUNDS, (int)ROUNDS, (double)ITERS);
        printf("\n  -- %s\n", cfg[c].name);
        row("loop iteration, no fault (control)", nf);
        row("loop iteration + translation fault", tf);
        row("loop iteration + permission fault", pf);
        double dt = tf.med - nf.med, dp = pf.med - nf.med;
        printf("  %-42s %9.2f ns %s\n", "=> NET translation-fault round trip", dt,
               g_ghz > 0 ? "" : "");
        if (g_ghz > 0) printf("  %-42s %9.1f cycles\n", "", dt * g_ghz);
        printf("  %-42s %9.2f ns\n", "=> NET permission-fault round trip", dp);
        if (g_ghz > 0) printf("  %-42s %9.1f cycles\n", "", dp * g_ghz);
    }
}

// ------------------------------------------------------------------ experiment: TLBI
static void *spin_thread(void *arg);
static struct { uint64_t root; uint64_t tcr; volatile int started; } g_spin;

static void exp_tlbi(int contend) {
    printf("\n=== 3. TLB maintenance cost %s ===\n",
           contend ? "WITH a second vCPU running" : "(single vCPU)");
    const uint64_t ITERS = 100000, ROUNDS = 15;
    uint64_t root = build_identity(12);
    build_fault_regions(root);
    for (int i = 0; i < 4096; i++)
        map_page(root, VA_SPIN + (uint64_t)i * 4096, RAM_IPA + OFF_SPINBUF + (uint64_t)i * 4096,
                 D_AF | D_SH_IS | D_UXN | D_PXN, 12);
    clear_params();
    uint64_t *p = P();
    p[0] = ITERS; p[1] = ROUNDS; p[2] = VA_RW;
    set_handler_state(0, 0, 0, 0);
    install_vectors(H_NONE);

    pthread_t st;
    *(volatile uint64_t *)(g_mem + OFF_STOP) = 0;
    if (contend) {
        g_spin.root = RAM_IPA + root; g_spin.tcr = TCR_4K; g_spin.started = 0;
        pthread_create(&st, NULL, spin_thread, NULL);
        while (!g_spin.started) usleep(1000);
        usleep(50000);
    }
    int rc = run_guest(2, TCR_4K, RAM_IPA + root, 0, 90);
    if (contend) {
        *(volatile uint64_t *)(g_mem + OFF_STOP) = 1;
        __sync_synchronize();
        pthread_join(st, NULL);
    }
    if (rc) return;

    const char *names[] = { "empty loop (floor)", "isb", "dsb ishst; dsb ish; isb",
                            "dsb nsh + TLBI VAE1   + dsb + isb",
                            "dsb ish + TLBI VAE1IS + dsb + isb",
                            "dsb ish + TLBI VAAE1IS+ dsb + isb",
                            "dsb nsh + TLBI VMALLE1  + dsb + isb",
                            "dsb ish + TLBI VMALLE1IS+ dsb + isb" };
    printf("\n");
    for (int v = 0; v < 8; v++) row(names[v], stats(R() + (uint64_t)v * ROUNDS, (int)ROUNDS, (double)ITERS));
}

// The contention vCPU. Needs its own thread (HVF binds a vCPU to its creating thread).
static void *spin_thread(void *arg) {
    (void)arg;
    pthread_set_qos_class_self_np(QOS_CLASS_USER_INTERACTIVE, 0);
    // a private param block copy is unnecessary: mode 8 only reads slots 0..3, which the
    // measured mode does not use.
    uint64_t *p = (uint64_t *)(g_mem + OFF_PARAM + 0x100);
    p[0] = VA_SPIN; p[1] = 4ULL << 20; p[2] = RAM_IPA + OFF_STOP; p[3] = 1000000;

    hv_vcpu_t vcpu; hv_vcpu_exit_t *vexit;
    HV(hv_vcpu_create(&vcpu, &vexit, NULL));
    HV(hv_vcpu_set_reg(vcpu, HV_REG_PC,   RAM_IPA + OFF_CODE));
    HV(hv_vcpu_set_reg(vcpu, HV_REG_CPSR, 0x3c5));
    HV(hv_vcpu_set_reg(vcpu, HV_REG_X0,   MAIR_VAL));
    HV(hv_vcpu_set_reg(vcpu, HV_REG_X1,   g_spin.tcr));
    HV(hv_vcpu_set_reg(vcpu, HV_REG_X2,   g_spin.root));
    HV(hv_vcpu_set_reg(vcpu, HV_REG_X3,   RAM_IPA + OFF_VEC));
    HV(hv_vcpu_set_reg(vcpu, HV_REG_X4,   RAM_IPA + OFF_PARAM + 0x100));
    HV(hv_vcpu_set_reg(vcpu, HV_REG_X5,   8));
    HV(hv_vcpu_set_reg(vcpu, HV_REG_X6,   0));
    g_spin.started = 1;
    for (;;) {
        if (hv_vcpu_run(vcpu) != HV_SUCCESS) break;
        if (vexit->reason == HV_EXIT_REASON_VTIMER_ACTIVATED) { hv_vcpu_set_vtimer_mask(vcpu, true); continue; }
        break;
    }
    hv_vcpu_destroy(vcpu);
    return NULL;
}

// ------------------------------------------------------------------ experiment: TLB reach
// Same physical working set, same node addresses, same random chain; only the stage-1 page
// size differs. Nodes sit one per 4 KiB unit, at a rotating offset inside that unit so they
// do not all collide in the same cache sets.
static uint64_t build_chain_at(uint64_t off, uint64_t span) {
    uint64_t n = span / 4096;
    uint32_t *perm = malloc(sizeof(uint32_t) * n);
    for (uint64_t i = 0; i < n; i++) perm[i] = (uint32_t)i;
    uint64_t s = 0x243f6a8885a308d3ULL;
    for (uint64_t i = n - 1; i > 0; i--) {
        s ^= s << 13; s ^= s >> 7; s ^= s << 17;
        uint64_t j = s % (i + 1);
        uint32_t t = perm[i]; perm[i] = perm[j]; perm[j] = t;
    }
    for (uint64_t k = 0; k < n; k++) {
        uint64_t a = (uint64_t)perm[k] * 4096 + ((perm[k] * 13) % 32) * 128;
        uint64_t b = (uint64_t)perm[(k + 1) % n] * 4096 + ((perm[(k + 1) % n] * 13) % 32) * 128;
        *(uint64_t *)(g_mem + OFF_WS + off + a) = VA_WS + off + b;
    }
    uint64_t head = VA_WS + off + (uint64_t)perm[0] * 4096 + ((perm[0] * 13) % 32) * 128;
    free(perm);
    return head;
}
static uint64_t build_chain(uint64_t span) { return build_chain_at(0, span); }

static void exp_reach(void) {
    printf("\n=== 4. stage-1 TLB reach: 4 KiB vs 16 KiB pages, identical access stream ===\n");
    printf("  (one chased node per 4 KiB of span in BOTH cases: same node count, same data\n"
           "   footprint, same random order. Only the number of distinct PAGES differs.)\n\n");
    const uint64_t STEPS = 200000, ROUNDS = 15;
    static const uint64_t spans_mib[] = { 1, 2, 4, 8, 12, 16, 20, 24, 32, 40, 48, 64, 96, 128, 192, 256 };
    int ns = (int)(sizeof(spans_mib) / sizeof(spans_mib[0]));
    double r4[32], r16[32];

    for (int gi = 0; gi < 2; gi++) {
        int g = gi ? 14 : 12;
        for (int i = 0; i < ns; i++) {
            uint64_t span = spans_mib[i] << 20;
            uint64_t root = build_identity(g);
            for (uint64_t o = 0; o < span; o += (1ULL << g))
                map_page(root, VA_WS + o, RAM_IPA + OFF_WS + o,
                         D_AF | D_SH_IS | D_UXN | D_PXN, g);
            uint64_t head = build_chain(span);
            clear_params();
            uint64_t *p = P();
            p[0] = head; p[1] = STEPS; p[2] = ROUNDS; p[3] = (span / 4096) * 3 + 1000;
            set_handler_state(0, 0, 0, 0);
            install_vectors(H_NONE);
            if (run_guest(3, g == 12 ? TCR_4K : TCR_16K, RAM_IPA + root, 0, 90)) {
                (gi ? r16 : r4)[i] = 0; continue; }
            double m = medof(R(), (int)ROUNDS, (double)STEPS);
            (gi ? r16 : r4)[i] = m;
        }
    }
    printf("  %8s %8s %9s %11s %9s %11s %8s\n",
           "span", "nodes", "4K pages", "4K ns/acc", "16K pages", "16K ns/acc", "4K/16K");
    for (int i = 0; i < ns; i++) {
        uint64_t span = spans_mib[i] << 20;
        printf("  %6llu M %8llu %9llu %11.2f %9llu %11.2f %7.2fx\n",
               (unsigned long long)spans_mib[i], (unsigned long long)(span / 4096),
               (unsigned long long)(span / 4096), r4[i],
               (unsigned long long)(span / 16384), r16[i],
               r16[i] > 0 ? r4[i] / r16[i] : 0.0);
    }
}

// ------------------------------------------------------------------ experiment: shadow fill
static void build_x86_tables(void) {
    uint64_t *pml4 = (uint64_t *)(g_mem + OFF_X86PT + 0x0000);
    uint64_t *pdpt = (uint64_t *)(g_mem + OFF_X86PT + 0x1000);
    uint64_t *pd   = (uint64_t *)(g_mem + OFF_X86PT + 0x2000);
    uint64_t *pt   = (uint64_t *)(g_mem + OFF_X86PT + 0x3000);
    memset(g_mem + OFF_X86PT, 0, 0x4000);
    pml4[(VA_SHADOW >> 39) & 0x1ff] = (RAM_IPA + OFF_X86PT + 0x1000) | 0x7;
    pdpt[(VA_SHADOW >> 30) & 0x1ff] = (RAM_IPA + OFF_X86PT + 0x2000) | 0x7;
    pd  [(VA_SHADOW >> 21) & 0x1ff] = (RAM_IPA + OFF_X86PT + 0x3000) | 0x7;
    for (int i = 0; i < 512; i++)
        pt[i] = (RAM_IPA + OFF_SHADOWBK + (uint64_t)i * 4096) | 0x67;   // P|RW|US|A|D
}

static void exp_shadow_one(int handler, const char *name) {
    const uint64_t PAGES = 512, ROUNDS = 25;
    printf("\n  -- %s\n", name);
    uint64_t root = build_identity(12);
    (void)walk_to(root, VA_SHADOW, 3, 12);                 // empty L3 -> translation faults
    uint64_t l3 = l3_ipa(root, VA_SHADOW, 12);
    build_x86_tables();
    clear_params();
    uint64_t *p = P();
    p[0] = PAGES; p[1] = ROUNDS; p[2] = VA_SHADOW;
    set_handler_state(PAGES * ROUNDS + 64, l3, RAM_IPA + OFF_X86PT,
                      D_PAGE | D_AF | D_SH_IS | D_UXN | D_PXN);
    install_vectors(handler);
    if (run_guest(4, TCR_4K, RAM_IPA + root, 0, 60)) return;

    printf("  faults taken: %llu (expected %llu)\n",
           (unsigned long long)AUX()[0], (unsigned long long)(PAGES * ROUNDS));
    stat_t full  = stats(R() + 0,              (int)ROUNDS, (double)PAGES);
    stat_t walk  = stats(R() + ROUNDS,         (int)ROUNDS, (double)PAGES);
    stat_t fill  = stats(R() + 2 * ROUNDS,     (int)ROUNDS, (double)PAGES);
    stat_t tlbi  = stats(R() + 3 * ROUNDS,     (int)ROUNDS, (double)PAGES);
    stat_t touch = stats(R() + 4 * ROUNDS,     (int)ROUNDS, (double)PAGES);
    printf("\n");
    row("FULL: touch + fault + fill + TLBI + ERET", full);
    row("  component: bare touch loop, all mapped", touch);
    row("  component: 4-level x86 walk only", walk);
    row("  component: walk + xlate + descriptor store", fill);
    row("  component: dsb+TLBI VAAE1IS+dsb+isb", tlbi);
    double net = full.med - touch.med;
    printf("  %-42s %9.2f ns", "=> NET cost per shadow-fill fault", net);
    if (g_ghz > 0) printf("   (%.0f cycles)", net * g_ghz);
    printf("\n");
    printf("  %-42s %9.2f ns  (%.0f%% of net)\n", "     of which: walk+xlate+store",
           fill.med, 100.0 * fill.med / net);
    printf("  %-42s %9.2f ns  (%.0f%% of net)\n", "     of which: TLBI + barriers",
           tlbi.med, 100.0 * tlbi.med / net);
}

static void exp_shadow(void) {
    printf("\n=== 2. full shadow-fill sequence (fault -> x86 walk -> descriptor -> [TLBI] -> ERET) ===\n");
    exp_shadow_one(H_SHADOW, "handler ends with dsb ishst; TLBI VAAE1IS; dsb ish; isb");
    exp_shadow_one(H_SHADOW_NOTLBI,
        "handler ends with dsb ishst; isb  (no TLBI: the descriptor was INVALID, and "
        "VMSAv8-64 forbids caching invalid entries)");
}

// ------------------------------------------------------------------ experiment: ASID
static void exp_asid(void) {
    printf("\n=== 5. ASID switch vs ASID rollover (only 256 ASIDs, N-11) ===\n");
    const uint64_t ROUNDS = 41;
    static const uint64_t ws_pages[] = { 256, 1024, 3072, 8192 };
    for (unsigned k = 0; k < sizeof(ws_pages)/sizeof(ws_pages[0]); k++) {
        uint64_t span = ws_pages[k] * 4096;
        uint64_t root = build_identity(12);
        for (uint64_t o = 0; o < span; o += 4096)
            map_page(root, VA_WS + o, RAM_IPA + OFF_WS + o,
                     D_AF | D_SH_IS | D_UXN | D_PXN | D_NG, 12);   // nG=1: ASID-tagged
        uint64_t head = build_chain(span);
        uint64_t steps = ws_pages[k];   // exactly one pass: a cold pass is 100% refill
        clear_params();
        uint64_t *p = P();
        p[0] = head; p[1] = steps; p[2] = ROUNDS; p[3] = RAM_IPA + root; p[4] = ws_pages[k] * 4;
        set_handler_state(0, 0, 0, 0);
        install_vectors(H_NONE);
        if (run_guest(5, TCR_4K, RAM_IPA + root, 0, 120)) continue;

        stat_t warm  = stats(R() + 0,          (int)ROUNDS, (double)steps);
        stat_t sw    = stats(R() + ROUNDS,     (int)ROUNDS, (double)(steps * 2));
        stat_t back  = stats(R() + 2 * ROUNDS, (int)ROUNDS, (double)steps);
        stat_t nasid = stats(R() + 3 * ROUNDS, (int)ROUNDS, (double)steps);
        stat_t flush = stats(R() + 4 * ROUNDS, (int)ROUNDS, (double)steps);
        printf("\n  -- working set %llu pages (%llu KiB), %llu chased steps/round\n",
               (unsigned long long)ws_pages[k], (unsigned long long)(span >> 10),
               (unsigned long long)steps);
        row("chase, warm, same ASID", warm);
        row("TTBR0_EL1 ASID switch (msr+isb), each", sw);
        row("chase after switch away and back", back);
        row("chase under a BRAND NEW ASID", nasid);
        row("chase right after TLBI VMALLE1IS", flush);
        printf("  %-42s %9.2f ns/page  -> %8.2f us for the whole working set\n",
               "=> refill penalty of a fresh ASID", nasid.med - warm.med,
               (nasid.med - warm.med) * (double)steps / 1000.0);
        printf("  %-42s %9.2f ns/page  -> %8.2f us for the whole working set\n",
               "=> refill penalty after TLBI VMALLE1IS", flush.med - warm.med,
               (flush.med - warm.med) * (double)steps / 1000.0);
    }
}

// ------------------------------------------------------------------ experiment: A/D
static void exp_ad(void) {
    printf("\n=== 6. Accessed-bit emulation by AF fault (HAFDBS=0, N-12) ===\n");
    const uint64_t PAGES = 512, ROUNDS = 25;
    struct { const char *name; int h; } cfg[] = {
        { "handler sets AF + TLBI VAAE1IS + barriers", H_AF },
        { "handler sets AF, dsb ishst only (no TLBI)", H_AF_NOTLBI },
    };
    for (unsigned c = 0; c < 2; c++) {
        uint64_t root = build_identity(12);
        for (int i = 0; i < 512; i++)
            map_page(root, VA_SHADOW + (uint64_t)i * 4096,
                     RAM_IPA + OFF_SHADOWBK + (uint64_t)i * 4096,
                     D_AF | D_SH_IS | D_UXN | D_PXN, 12);
        uint64_t l3 = l3_ipa(root, VA_SHADOW, 12);
        clear_params();
        uint64_t *p = P();
        p[0] = VA_SHADOW; p[1] = PAGES; p[2] = ROUNDS;
        set_handler_state(PAGES * ROUNDS + 64, l3, RAM_IPA + OFF_X86PT,
                          D_PAGE | D_AF | D_SH_IS | D_UXN | D_PXN);
        install_vectors(cfg[c].h);
        if (run_guest(6, TCR_4K, RAM_IPA + root, 0, 60)) continue;

        printf("\n  -- %s\n", cfg[c].name);
        printf("  faults taken: %llu (expected %llu)%s\n",
               (unsigned long long)AUX()[0], (unsigned long long)(PAGES * ROUNDS),
               AUX()[0] == PAGES * ROUNDS ? "" : "   <-- MISMATCH");
        stat_t af    = stats(R() + 0,          (int)ROUNDS, (double)PAGES);
        stat_t touch = stats(R() + ROUNDS,     (int)ROUNDS, (double)PAGES);
        row("touch page with AF=0 (fault + set + retry)", af);
        row("  bare touch loop, AF already set", touch);
        printf("  %-42s %9.2f ns", "=> NET AF-fault cost per page", af.med - touch.med);
        if (g_ghz > 0) printf("   (%.0f cycles)", (af.med - touch.med) * g_ghz);
        printf("\n");
        if (c == 0) {
            row("AT S1E1R  + isb + read PAR_EL1", stats(R() + 2 * ROUNDS, (int)ROUNDS, (double)PAGES));
            row("AT S1E1W  + isb + read PAR_EL1", stats(R() + 3 * ROUNDS, (int)ROUNDS, (double)PAGES));
            row("AT S1E1RP + isb + read PAR_EL1", stats(R() + 4 * ROUNDS, (int)ROUNDS, (double)PAGES));
            row("AT S1E1WP + isb + read PAR_EL1", stats(R() + 5 * ROUNDS, (int)ROUNDS, (double)PAGES));
        }
    }
}

// ------------------------------------------------------------------ experiment: EnTSO cost
static void exp_tso_one(uint64_t BYTES, uint64_t PASSES, uint64_t CHASE, const char *tier) {
    const uint64_t ROUNDS = 15;
    printf("\n  -- buffer %llu KiB (%s), %llu passes/round\n",
           (unsigned long long)(BYTES >> 10), tier, (unsigned long long)PASSES);
    const uint64_t CHAIN_OFF = 32ULL << 20;      // the scattered-store workload would
    uint64_t root = build_identity(12);         // otherwise overwrite the chain
    for (uint64_t o = 0; o < CHAIN_OFF + BYTES; o += 4096)
        map_page(root, VA_WS + o, RAM_IPA + OFF_WS + o, D_AF | D_SH_IS | D_UXN | D_PXN, 12);
    uint64_t head = build_chain_at(CHAIN_OFF, BYTES);

    // ABBA order across repeats to cancel any thermal/frequency drift.
    const int REP = 10;
    double m[2][4][REP];
    uint64_t actlr[2] = {0,0};
    for (int r = 0; r < REP; r++) {
        int order[2] = { (r % 2) ? 1 : 0, (r % 2) ? 0 : 1 };
        for (int oi = 0; oi < 2; oi++) {
            int tso = order[oi];
            clear_params();
            uint64_t *p = P();
            p[0] = VA_WS; p[1] = BYTES; p[2] = ROUNDS; p[3] = PASSES;
            p[4] = head;  p[5] = CHASE;
            set_handler_state(0, 0, 0, 0);
            install_vectors(H_NONE);
            if (run_guest(7, TCR_4K, RAM_IPA + root, (uint64_t)tso, 180)) return;
            actlr[tso] = AUX()[2];
            double per[4] = { (double)(BYTES / 8) * PASSES, (double)(BYTES / 8) * PASSES,
                              (double)(BYTES / 128) * PASSES, (double)CHASE };
            for (int w = 0; w < 4; w++)
                m[tso][w][r] = medof(R() + (uint64_t)w * ROUNDS, (int)ROUNDS, per[w]);
        }
    }
    printf("  ACTLR_EL1 read back in guest: EnTSO=0 -> 0x%llx,  EnTSO=1 -> 0x%llx\n",
           (unsigned long long)actlr[0], (unsigned long long)actlr[1]);
    const char *wn[4] = { "streaming stp (per 8 B stored)",
                          "streaming ldp (per 8 B loaded)",
                          "scattered str (per 128 B line)",
                          "dependent pointer chase (per load)" };
    printf("\n  %-36s %12s %12s %8s\n", "workload", "EnTSO=0 ns", "EnTSO=1 ns", "ratio");
    for (int w = 0; w < 4; w++) {
        double a[REP], b[REP];
        memcpy(a, m[0][w], sizeof a); memcpy(b, m[1][w], sizeof b);
        qsort(a, REP, sizeof(double), dcmp); qsort(b, REP, sizeof(double), dcmp);
        double ma = (a[REP/2 - 1] + a[REP/2]) / 2, mb = (b[REP/2 - 1] + b[REP/2]) / 2;
        printf("  %-36s %12.4f %12.4f %7.3fx\n", wn[w], ma, mb, mb / ma);
        printf("  %-36s   raw0: ", "");
        for (int i = 0; i < REP; i++) printf("%.4f ", m[0][w][i]);
        printf("  raw1: ");
        for (int i = 0; i < REP; i++) printf("%.4f ", m[1][w][i]);
        printf("\n");
    }
}

static void exp_tso(void) {
    printf("\n=== 7. cost of running guest EL1 with ACTLR_EL1.EnTSO = 1 ===\n");
    printf("  10 repeats per setting in ABBA order; each number is the median of those medians.\n");
    exp_tso_one(   32ULL << 10, 2048, 200000, "L1D-resident");
    exp_tso_one( 1024ULL << 10,   64, 200000, "L2-resident");
    exp_tso_one(   16ULL << 20,    4, 200000, "DRAM-resident");
}

// ------------------------------------------------------------------ main
int main(int argc, char **argv) {
    const char *what = argc > 1 ? argv[1] : "all";
    pthread_set_qos_class_self_np(QOS_CLASS_USER_INTERACTIVE, 0);
    printf("=== shadow-MMU cost probe (all timing taken in-guest at EL1 via CNTVCT_EL0) ===\n");

    HV(hv_vm_create(NULL));
    g_mem = mmap(NULL, RAM_SIZE, PROT_READ | PROT_WRITE, MAP_ANON | MAP_PRIVATE, -1, 0);
    if (g_mem == MAP_FAILED) { perror("mmap"); return 2; }
    memset(g_mem, 0, RAM_SIZE);                 // pre-fault every host page
    HV(hv_vm_map(g_mem, RAM_IPA, RAM_SIZE, HV_MEMORY_READ | HV_MEMORY_WRITE | HV_MEMORY_EXEC));
    memcpy(g_mem + OFF_CODE, sm_start, (size_t)(sm_end - sm_start));
    printf("  payload %zu bytes at IPA 0x%llx, RAM %llu MiB\n",
           (size_t)(sm_end - sm_start), RAM_IPA + OFF_CODE,
           (unsigned long long)(RAM_SIZE >> 20));

    int all = !strcmp(what, "all");
    exp_cal();                                   // always: needed for the clock estimate
    if (all || !strcmp(what, "fault"))  exp_fault();
    if (all || !strcmp(what, "shadow")) exp_shadow();
    if (all || !strcmp(what, "tlbi"))  { exp_tlbi(0); exp_tlbi(1); }
    if (all || !strcmp(what, "reach"))  exp_reach();
    if (all || !strcmp(what, "asid"))   exp_asid();
    if (all || !strcmp(what, "ad"))     exp_ad();
    if (all || !strcmp(what, "tso"))    exp_tso();

    hv_vm_destroy();
    printf("\n  %d failed experiment(s)\n", g_fail);
    return g_fail ? 1 : 0;
}
