// Self-modifying-code (SMC) detection probe for the Tier 2 in-VM shadow-MMU design.
//
// Doc 16 measured what a shadow-PT fault costs. Doc 19 measured how it scales. Neither measured
// what it costs to (a) revoke write permission on a translated code page and take the resulting
// fault, (b) do the ARM I-cache maintenance that x86's coherent-I-fetch semantics force on us
// after any code is rewritten, or (c) do either of those on more than one core. This probe does.
//
//   info      CTR_EL0 (DIC/IDC/IminLine/DminLine), CNTFRQ, clock estimate, EL2-trap census
//   wp        write-protect a code page and take the write fault at guest EL1: five handler
//             strategies (skip / read ESR / emulate via alias x2 / classify), each against a
//             matched no-fault control over the identical address stream
//   cycle     the full protect->fault->unprotect->write->re-protect round trip, and its
//             components (bare store, descriptor edit, TLBI, permission round trip)
//   icache    dc cvau / ic ivau / ic iallu / the architecturally-required canonical sequence,
//             swept 64 B .. 64 KiB; plus execute-after-invalidate (the I-cache REFILL cost) and
//             the whole SMC turnaround (rewrite + maintenance + execute)
//   smp       the same at 1/2/4/8 vCPUs, with the other vCPUs idle, walking their own pages,
//             or issuing their own IC IVAU storms.  IC IVAU is broadcast to the inner-shareable
//             domain; this measures what that costs.
//   hash      byte-compare and multiply-accumulate checksum throughput over a 4 KiB page --
//             the cost of the "hash the code page instead of protecting it" design point
//
// All in-guest timing is CNTVCT_EL0 read at guest EL1 under our own VBAR_EL1: no host exit
// occurs inside any measured window.  A per-run EL2-trap counter proves it.
//
// Build: ../build.sh smc/smc.c        Run: ./smc/bin/smc [info|wp|cycle|icache|smp|hash|all]

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

extern const uint8_t smc_start[], smc_end[];
extern const uint8_t h_skip[], h_skip_esr[], h_unprot_tlbi[], h_unprot_notlbi[];
extern const uint8_t h_emul_fetch[], h_emul_esr[], h_classify[], h_panic[];

// ------------------------------------------------------------------ memory map
#define RAM_IPA        0x40000000ULL
#define RAM_SIZE       0x10000000ULL          // 256 MiB

#define OFF_CODE       0x00000000ULL
#define OFF_VEC        0x00010000ULL          // 2 KB vector table
#define OFF_GLOBAL     0x00011000ULL          // barriers (+0) and stop flags (+0x200)
#define OFF_SIDETAB    0x00020000ULL          // 64 KiB: one kind byte per 4 KiB guest frame
#define OFF_SHAREDPT   0x00040000ULL          // 1 MiB page-table arena
#define OFF_SHAREDPT_E 0x00140000ULL
#define OFF_PROT       0x00200000ULL          // 2 MiB = 512 pages, the protected region backing
#define OFF_CODEREG    0x00400000ULL          // 2 MiB executable region for mode 4
#define OFF_CMP        0x00600000ULL          // 6 MiB compare buffers (A at +0, B at +2 MiB)
#define OFF_VCPU0      0x01000000ULL
#define VCPU_STRIDE    0x00800000ULL          // 8 MiB per vCPU
#define MAXV           12

#define S_PARAM        0x000000ULL            // 4 KB
#define S_RES          0x001000ULL            // 128 KB
#define S_RES2         0x021000ULL            // 124 KB
#define S_CODE         0x200000ULL            // 2 MiB private executable region
#define S_WS           0x400000ULL            // 4 MiB private working set

#define G_BAR          0x0000ULL
#define G_STOP         0x0200ULL

// guest VAs (identity map covers all of RAM; these two are the 4 KiB-mapped test range)
#define VA_PROT        0x0000400000000000ULL  // 512 pages, write-protected in the shadow L3
#define VA_ALIAS       0x0000400000200000ULL  // same 512 physical pages, always writable
#define ALIAS_DELTA    (VA_ALIAS - VA_PROT)
#define NPROT          512ULL

#define D_PAGE   3ULL
#define D_BLOCK  1ULL
#define D_AF     (1ULL << 10)
#define D_SH_IS  (3ULL << 8)
#define D_RO     (1ULL << 7)                  // AP[2]
#define D_UXN    (1ULL << 54)
#define D_PXN    (1ULL << 53)
#define PA_MASK  0x0000fffffffff000ULL

#define MAIR_VAL 0xFFULL
#define TCR_4K   (16ULL | (1ULL<<8) | (1ULL<<10) | (3ULL<<12) \
                 | (16ULL<<16) | (1ULL<<23) | (2ULL<<30) | (2ULL<<32))

#define INSN_ADD 0x91000400u                  // add x0, x0, #1
#define INSN_RET 0xd65f03c0u

#define HV(e) do { hv_return_t _r = (e); if (_r != HV_SUCCESS) { \
    fprintf(stderr, "  %s -> 0x%x at %d\n", #e, _r, __LINE__); exit(2);} } while (0)

static uint8_t *g_mem;
static uint64_t g_cntfrq = 24000000;
static double   g_ghz    = 3.25;
static int      g_fail   = 0;
static int      g_maxv   = 0;
static int      g_traps  = 0;                 // EL2 traps seen inside any run (must stay 0)

static uint64_t slab(int i)      { return OFF_VCPU0 + (uint64_t)i * VCPU_STRIDE; }
static uint64_t *P(int i)        { return (uint64_t *)(g_mem + slab(i) + S_PARAM); }
static uint64_t *AUX(int i)      { return (uint64_t *)(g_mem + slab(i) + S_PARAM + 0xc00); }
static uint64_t *RES(int i)      { return (uint64_t *)(g_mem + slab(i) + S_RES); }
static uint64_t *RES2(int i)     { return (uint64_t *)(g_mem + slab(i) + S_RES2); }

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
    static const int sh[4] = { 39, 30, 21, 12 };
    uint64_t cur = root;
    for (int l = 0; l < level; l++) {
        uint64_t idx = (va >> sh[l]) & 0x1ff;
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
// identity-map all guest RAM with 2 MiB blocks, RW and executable at EL1
static uint64_t build_identity(arena_t *a) {
    uint64_t root = alloc_tab(a);
    for (uint64_t o = 0; o < RAM_SIZE; o += 0x200000ULL)
        map_block2(a, root, RAM_IPA + o, RAM_IPA + o, D_AF | D_SH_IS);
    return root;
}

// Build the whole world: identity map + the 512-page protected range + its writable alias.
// Returns the root offset; *l3out gets the IPA of the protected range's L3 table.
static uint64_t build_world(arena_t *a, uint64_t *l3out, int prot_ro) {
    uint64_t root = build_identity(a);
    for (uint64_t k = 0; k < NPROT; k++) {
        uint64_t pa = RAM_IPA + OFF_PROT + k * 4096;
        map_page(a, root, VA_PROT  + k * 4096, pa,
                 D_AF | D_SH_IS | D_UXN | D_PXN | (prot_ro ? D_RO : 0));
        map_page(a, root, VA_ALIAS + k * 4096, pa, D_AF | D_SH_IS | D_UXN | D_PXN);
    }
    *l3out = RAM_IPA + walk_to(a, root, VA_PROT, 3);
    return root;
}
static void set_prot_ro(uint64_t l3ipa, int ro) {
    uint64_t *t = (uint64_t *)(g_mem + (l3ipa - RAM_IPA));
    for (uint64_t k = 0; k < NPROT; k++)
        if (t[k] & 1) t[k] = ro ? (t[k] | D_RO) : (t[k] & ~D_RO);
}

// Fill an executable region: `bytes/4 - 1` copies of `add x0,x0,#1`, then `ret`.
static void fill_code(uint64_t off, uint64_t bytes) {
    uint32_t *p = (uint32_t *)(g_mem + off);
    uint64_t n = bytes / 4;
    for (uint64_t i = 0; i + 1 < n; i++) p[i] = INSN_ADD;
    p[n - 1] = INSN_RET;
}

// ------------------------------------------------------------------ vectors
static void install_vectors(const uint8_t *sync_handler) {
    uint32_t *v = (uint32_t *)(g_mem + OFF_VEC);
    memset(v, 0, 0x800);
    uint64_t panic_off = OFF_CODE + (uint64_t)(h_panic - smc_start);
    for (int i = 0; i < 16; i++) {
        int64_t d = (int64_t)panic_off - (int64_t)(OFF_VEC + i * 0x80);
        v[i * 0x20] = 0x14000000u | (uint32_t)((d >> 2) & 0x03ffffff);
    }
    if (!sync_handler) return;
    uint64_t slot = OFF_VEC + 0x200;                        // sync, current EL, SP_ELx
    uint64_t hoff = OFF_CODE + (uint64_t)(sync_handler - smc_start);
    int64_t d = (int64_t)hoff - (int64_t)slot;
    *(uint32_t *)(g_mem + slot) = 0x14000000u | (uint32_t)((d >> 2) & 0x03ffffff);
}

// ------------------------------------------------------------------ vCPU group runner
typedef struct {
    int idx;
    uint64_t mode, ttbr;
    hv_vcpu_t vcpu;
    hv_vcpu_exit_t *vexit;
    volatile int ready, rc, traps;
} ctx_t;

static ctx_t g_ctx[MAXV];
static volatile int g_group_fail;

static const char *ecname(uint64_t ec) {
    switch (ec) {
        case 0x00: return "unknown/undef";
        case 0x16: return "HVC";
        case 0x18: return "MSR/MRS/system-instruction trap to EL2";
        case 0x21: return "instruction abort";
        case 0x24: return "data abort lower EL";
        case 0x25: return "data abort current EL";
        default:   return "?";
    }
}

static void *vcpu_thread(void *arg) {
    ctx_t *c = arg;
    pthread_set_qos_class_self_np(QOS_CLASS_USER_INTERACTIVE, 0);
    HV(hv_vcpu_create(&c->vcpu, &c->vexit, NULL));
    HV(hv_vcpu_set_reg(c->vcpu, HV_REG_PC,   RAM_IPA + OFF_CODE));
    // An HVF vCPU starts at EL0. 0x3c5 = EL1h with DAIF masked; without this the first MRS
    // traps to VBAR+0x400.
    HV(hv_vcpu_set_reg(c->vcpu, HV_REG_CPSR, 0x3c5));
    HV(hv_vcpu_set_reg(c->vcpu, HV_REG_X0,   MAIR_VAL));
    HV(hv_vcpu_set_reg(c->vcpu, HV_REG_X1,   TCR_4K));
    HV(hv_vcpu_set_reg(c->vcpu, HV_REG_X2,   c->ttbr));
    HV(hv_vcpu_set_reg(c->vcpu, HV_REG_X3,   RAM_IPA + OFF_VEC));
    HV(hv_vcpu_set_reg(c->vcpu, HV_REG_X4,   RAM_IPA + slab(c->idx) + S_PARAM));
    HV(hv_vcpu_set_reg(c->vcpu, HV_REG_X5,   c->mode));
    HV(hv_vcpu_set_reg(c->vcpu, HV_REG_X6,   0));
    c->ready = 1; c->rc = 0; c->traps = 0;
    for (;;) {
        hv_return_t r = hv_vcpu_run(c->vcpu);
        if (r != HV_SUCCESS) { fprintf(stderr, "  vcpu%d run -> 0x%x\n", c->idx, r); c->rc = 1; break; }
        if (c->vexit->reason == HV_EXIT_REASON_VTIMER_ACTIVATED) {
            hv_vcpu_set_vtimer_mask(c->vcpu, true); continue;
        }
        if (c->vexit->reason == HV_EXIT_REASON_CANCELED) {
            fprintf(stderr, "  !! vcpu%d cancelled by watchdog (mode %llu)\n",
                    c->idx, (unsigned long long)c->mode);
            c->rc = 1; break;
        }
        if (c->vexit->reason != HV_EXIT_REASON_EXCEPTION) {
            fprintf(stderr, "  !! vcpu%d exit reason %u\n", c->idx, c->vexit->reason);
            c->rc = 1; break;
        }
        uint64_t sy = c->vexit->exception.syndrome, ec = (sy >> 26) & 0x3f;
        if (ec == 0x16 && (sy & 0xffff) == 0) break;                  // normal completion
        if (ec == 0x16 && (sy & 0xffff) == 10) {
            fprintf(stderr, "  !! vcpu%d BARRIER TIMEOUT (mode %llu)\n",
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
        if (ec == 0x18) {                    // a real host exit inside a measured window
            uint64_t pc = 0; hv_vcpu_get_reg(c->vcpu, HV_REG_PC, &pc);
            hv_vcpu_set_reg(c->vcpu, HV_REG_PC, pc + 4);
            if (c->traps < 4)
                fprintf(stderr, "  !! vcpu%d EL2 TRAP ESR=0x%llx PC=0x%llx (cache/sysreg op trapped)\n",
                        c->idx, (unsigned long long)sy, (unsigned long long)pc);
            c->traps++;
            continue;
        }
        uint64_t pc = 0; hv_vcpu_get_reg(c->vcpu, HV_REG_PC, &pc);
        fprintf(stderr, "  !! vcpu%d unexpected exit ESR=0x%llx EC=0x%llx (%s) PC=0x%llx\n",
                c->idx, (unsigned long long)sy, (unsigned long long)ec, ecname(ec),
                (unsigned long long)pc);
        c->rc = 1; break;
    }
    if (c->rc) g_group_fail = 1;
    g_traps += c->traps;
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
    memset(g_mem + slab(i) + S_RES, 0, 0x40000);
}
static void set_common(int i, int barrier, uint64_t n) {
    uint64_t *p = P(i);
    p[0x180/8] = barrier ? RAM_IPA + OFF_GLOBAL + G_BAR : 0;
    p[0x188/8] = n;
    p[0x1f0/8] = RAM_IPA + slab(i) + S_RES;
}
static void set_handler_state(int i, uint64_t limit, uint64_t l3ipa) {
    uint64_t *p = P(i);
    p[0x1c0/8] = 0;
    p[0x1c8/8] = limit;
    p[0x1d0/8] = l3ipa;
    p[0x1d8/8] = ALIAS_DELTA;
    p[0x1e0/8] = PA_MASK;
    // x24: base of the per-frame kind table, pre-biased so a raw frame number indexes it
    p[0x1e8/8] = RAM_IPA + OFF_SIDETAB - (RAM_IPA >> 12);
}
static void reset_globals(void) { memset(g_mem + OFF_GLOBAL, 0, 0x1000); }

// ------------------------------------------------------------------ stats
typedef struct { double p50, p90, p99, min, max; int n; } st_t;
static int dcmp(const void *a, const void *b) {
    double x = *(const double *)a, y = *(const double *)b;
    return x < y ? -1 : x > y ? 1 : 0;
}
static st_t stats(const uint64_t *ticks, int n, double per) {
    st_t s = {0};
    if (n <= 0) return s;
    double *v = malloc(sizeof(double) * (size_t)n);
    for (int i = 0; i < n; i++) v[i] = (double)ticks[i] * 1e9 / (double)g_cntfrq / per;
    qsort(v, (size_t)n, sizeof(double), dcmp);
    s.p50 = v[n/2]; s.p90 = v[(int)(n*0.90)]; s.p99 = v[(int)(n*0.99)];
    s.min = v[0]; s.max = v[n-1]; s.n = n;
    free(v);
    return s;
}

// ==================================================================
// info
// ==================================================================
static void exp_info(void) {
    printf("\n=== 0. Machine facts read from inside the VM at guest EL1 ===\n");
    arena_t a = { OFF_SHAREDPT, OFF_SHAREDPT_E };
    uint64_t l3 = 0, root = build_world(&a, &l3, 0);
    reset_globals(); clear_slab(0);
    P(0)[0] = 20000;
    set_common(0, 0, 1); set_handler_state(0, 0, l3);
    g_ctx[0].mode = 0; g_ctx[0].ttbr = RAM_IPA + root;
    install_vectors(h_skip);
    if (run_group(1, 60)) { printf("  FAILED\n"); return; }

    uint64_t *r = (uint64_t *)(g_mem + slab(0) + S_PARAM + 0x200);
    g_cntfrq = AUX(0)[2] ? AUX(0)[2] : 24000000;
    uint64_t ctr = AUX(0)[5];
    double t_isbmrs = (double)r[0] * 1e9 / g_cntfrq / 20000.0;
    double t_loop   = (double)r[1] * 1e9 / g_cntfrq / 20000.0;
    double t_add    = (double)r[2] * 1e9 / g_cntfrq / (20000.0 * 16);
    g_ghz = 1.0 / t_add;

    printf("  CNTFRQ_EL0            = %llu Hz  (%.2f ns/tick)\n",
           (unsigned long long)g_cntfrq, 1e9 / (double)g_cntfrq);
    printf("  {isb; mrs CNTVCT_EL0} = %.2f ns   empty loop iteration = %.2f ns\n", t_isbmrs, t_loop);
    printf("  dependent ADD latency = %.4f ns -> clock estimate %.2f GHz\n", t_add, g_ghz);
    printf("  CTR_EL0               = 0x%016llx\n", (unsigned long long)ctr);
    {
        int iminline = 4 << (ctr & 0xf);            // words
        int dminline = 4 << ((ctr >> 16) & 0xf);
        int idc = (int)((ctr >> 28) & 1), dic = (int)((ctr >> 29) & 1);
        int l1ip = (int)((ctr >> 14) & 3);
        printf("    IminLine = %d bytes   DminLine = %d bytes   CWG = %d B  ERG = %d B\n",
               iminline, dminline, 4 << ((ctr >> 24) & 0xf), 4 << ((ctr >> 20) & 0xf));
        printf("    IDC = %d  (%s)\n", idc,
               idc ? "data-to-PoU coherency: DC CVAU may be omitted"
                   : "DC CVAU to PoU is REQUIRED after writing code");
        printf("    DIC = %d  (%s)\n", dic,
               dic ? "instruction-to-PoU coherency: IC IVAU may be omitted"
                   : "IC IVAU is REQUIRED after writing code");
        printf("    L1Ip = %d (%s)\n", l1ip,
               l1ip == 0 ? "VPIPT" : l1ip == 1 ? "AIVIVT" : l1ip == 2 ? "VIPT" : "PIPT");
        if (iminline != 64 || dminline != 64)
            printf("    NOTE: this probe strides by 64 B; the real line size differs -- "
                   "per-line figures below are per-64-B-step, not per-line.\n");
    }
    printf("  EL2 traps during this run: %d %s\n", g_traps,
           g_traps ? "** cache maintenance may be trapping to the host" : "(none -- good)");
}

// ==================================================================
// wp -- write-protect a code page, take the fault, five handler strategies
// ==================================================================
// CNTVCT_EL0 ticks at 24 MHz = 41.67 ns. With 16 stores per timed sample the quantum is 2.6 ns
// per store, which is coarser than the differences between handler strategies; 128 stores per
// sample puts the quantum at 0.33 ns.
static const uint64_t WP_ROUNDS = 400, WP_GROUP = 128;

// ro: 1 = protected range is read-only (every store faults), 0 = writable (control)
static int wp_run(const uint8_t *handler, int ro, st_t *out, uint64_t *faults,
                  uint64_t *esr, uint64_t *far, uint64_t *insn, uint64_t *cls) {
    arena_t a = { OFF_SHAREDPT, OFF_SHAREDPT_E };
    uint64_t l3 = 0, root = build_world(&a, &l3, ro);
    memset(g_mem + OFF_PROT, 0, NPROT * 4096);
    // per-frame kind table: mark the protected frames as "code" (kind 2)
    memset(g_mem + OFF_SIDETAB, 0, RAM_SIZE >> 12);
    for (uint64_t k = 0; k < NPROT; k++)
        g_mem[OFF_SIDETAB + ((OFF_PROT >> 12) + k)] = 2;
    reset_globals(); clear_slab(0);
    uint64_t *p = P(0);
    p[0] = NPROT; p[1] = WP_ROUNDS; p[2] = VA_PROT; p[3] = WP_GROUP;
    set_common(0, 0, 1);
    set_handler_state(0, NPROT * (WP_ROUNDS + 2) + 4096, l3);
    g_ctx[0].mode = 2; g_ctx[0].ttbr = RAM_IPA + root;
    install_vectors(handler);
    if (run_group(1, 180)) return 1;
    int ns = (int)(WP_ROUNDS * NPROT / WP_GROUP);
    *out = stats(RES(0), ns, (double)WP_GROUP);
    *faults = AUX(0)[0];
    if (esr)  *esr  = AUX(0)[6];
    if (far)  *far  = AUX(0)[11];
    if (insn) *insn = AUX(0)[12];
    if (cls)  { cls[0] = AUX(0)[13]; cls[1] = AUX(0)[14]; cls[2] = AUX(0)[15]; }
    return 0;
}

static void exp_wp(void) {
    printf("\n=== 1. Revoking write permission on a code page and taking the write fault ===\n");
    printf("  %llu distinct 4 KiB pages, %llu rounds, one timed sample per %llu stores.\n",
           (unsigned long long)NPROT, (unsigned long long)WP_ROUNDS,
           (unsigned long long)WP_GROUP);
    printf("  The control runs the IDENTICAL instruction sequence over the IDENTICAL address\n"
           "  stream; only the shadow L3's AP[2] bit differs. NET is the difference.\n\n");

    st_t ctl;
    uint64_t f = 0;
    if (wp_run(h_skip, 0, &ctl, &f, NULL, NULL, NULL, NULL)) { printf("  control FAILED\n"); return; }
    printf("  %-52s %9.2f ns   faults %llu\n",
           "control: page WRITABLE, store lands, no fault", ctl.p50, (unsigned long long)f);
    if (f != 0) printf("    ** control took %llu faults; it should take none\n",
                       (unsigned long long)f);

    struct { const uint8_t *h; const char *name; } arms[] = {
        { h_skip,       "RO page, handler skips the store (bare permission fault)" },
        { h_skip_esr,   "  + read ESR_EL1 + FAR_EL1 + the faulting instruction word" },
        { h_classify,   "  + classify the frame: shadow-L3 -> OA -> per-frame kind byte" },
        { h_emul_esr,   "  + emulate the store via a writable alias, Rt from ESR.ISS.SRT" },
        { h_emul_fetch, "  + emulate the store via a writable alias, Rt decoded from the insn" },
    };
    uint64_t esr = 0, far = 0, insn = 0, cls[3] = {0,0,0};
    for (unsigned i = 0; i < sizeof(arms)/sizeof(arms[0]); i++) {
        st_t s;
        if (wp_run(arms[i].h, 1, &s, &f, &esr, &far, &insn, cls)) {
            printf("  %-52s FAILED\n", arms[i].name); continue;
        }
        printf("  %-52s %9.2f ns   NET %8.2f ns (%5.0f cyc)  faults %llu/%llu%s\n",
               arms[i].name, s.p50, s.p50 - ctl.p50, (s.p50 - ctl.p50) * g_ghz,
               (unsigned long long)f, (unsigned long long)(NPROT * WP_ROUNDS),
               f == NPROT * WP_ROUNDS ? "" : "  ** FAULT COUNT MISMATCH");
        if (arms[i].h == h_skip_esr) {
            uint64_t ec = (esr >> 26) & 0x3f, dfsc = esr & 0x3f;
            printf("        ESR_EL1 = 0x%llx  EC=0x%llx (%s)  DFSC=0x%llx (%s)  WnR=%llu  "
                   "ISV=%llu  SRT=%llu  SAS=%llu\n",
                   (unsigned long long)esr, (unsigned long long)ec, ecname(ec),
                   (unsigned long long)dfsc,
                   dfsc == 0x0f ? "permission fault, level 3" :
                   dfsc == 0x07 ? "translation fault, level 3" : "?",
                   (unsigned long long)((esr >> 6) & 1), (unsigned long long)((esr >> 24) & 1),
                   (unsigned long long)((esr >> 16) & 0x1f), (unsigned long long)((esr >> 22) & 3));
            printf("        FAR_EL1 = 0x%llx   faulting instruction word = 0x%08llx\n",
                   (unsigned long long)far, (unsigned long long)insn);
            printf("        -> %s\n", ((esr >> 24) & 1)
                   ? "ISV=1: the register number is in the syndrome, no instruction fetch needed"
                   : "ISV=0: the syndrome does NOT name the register; a software store emulator "
                     "must fetch and decode the faulting instruction itself");
        }
        if (arms[i].h == h_emul_esr)
            printf("        NOTE: with ISV=0 this arm reads SRT=0 and therefore emulates the\n"
                   "        store as if Rt were x0. It is reported as a COST measurement only --\n"
                   "        it is not a correct store emulator on this platform. The insn-decode\n"
                   "        arm below is the one that is semantically right.\n");
        if (arms[i].h == h_classify)
            printf("        classified: code=%llu  page-table=%llu  unknown=%llu\n",
                   (unsigned long long)cls[0], (unsigned long long)cls[1],
                   (unsigned long long)cls[2]);
    }
    printf("\n  Reference (doc 16 §1, same machine): bare in-VM permission fault 28.2 ns,\n"
           "  translation fault 94.4 ns, full shadow fill 145.8 ns.\n");
}

// ==================================================================
// cycle -- the whole protect/fault/unprotect/write/re-protect round trip
// ==================================================================
static const uint64_t CY_ROUNDS = 100, CY_GROUP = 16, CY_CROUNDS = 25;

static int cycle_run(const uint8_t *handler, int reprot, st_t *out, double comp[4],
                     uint64_t *faults, uint64_t *upg) {
    arena_t a = { OFF_SHAREDPT, OFF_SHAREDPT_E };
    uint64_t l3 = 0, root = build_world(&a, &l3, reprot ? 1 : 0);
    memset(g_mem + OFF_PROT, 0, NPROT * 4096);
    reset_globals(); clear_slab(0);
    uint64_t *p = P(0);
    p[0] = NPROT; p[1] = CY_ROUNDS; p[2] = VA_PROT; p[3] = CY_GROUP;
    p[4] = RAM_IPA + slab(0) + S_RES2; p[5] = CY_CROUNDS; p[6] = (uint64_t)reprot;
    set_common(0, 0, 1);
    // generous: the no-TLBI arm may fault twice per page if the stale read-only entry survives
    set_handler_state(0, NPROT * (CY_ROUNDS * 3 + 8) + 4096, l3);
    g_ctx[0].mode = 1; g_ctx[0].ttbr = RAM_IPA + root;
    install_vectors(handler);
    if (run_group(1, 240)) return 1;
    int ns = (int)(CY_ROUNDS * NPROT / CY_GROUP);
    *out = stats(RES(0), ns, (double)CY_GROUP);
    for (int b = 0; b < 4; b++)
        comp[b] = stats(RES2(0) + (uint64_t)b * CY_CROUNDS, (int)CY_CROUNDS, (double)NPROT).p50;
    *faults = AUX(0)[0];
    *upg = AUX(0)[9];
    return 0;
}

static void exp_cycle(void) {
    printf("\n=== 2. The full SMC write-protect cycle, and its components ===\n");
    printf("  Per page, per round: store (faults) -> handler clears AP[2] and retries ->\n"
           "  the store lands -> the loop re-sets AP[2] + TLBI VAAE1IS + barriers.\n"
           "  This is what a translator pays for ONE guest write to a protected code page when\n"
           "  it re-protects the page immediately afterwards.\n\n");
    st_t s; double comp[4]; uint64_t f = 0, upg = 0;

    if (!cycle_run(h_skip, 0, &s, comp, &f, &upg))
        printf("  %-56s %9.2f ns   faults %llu (bare-store component %.2f ns)\n",
               "control: page writable, no fault, no re-protect", s.p50,
               (unsigned long long)f, comp[0]);

    if (!cycle_run(h_unprot_tlbi, 1, &s, comp, &f, &upg)) {
        printf("  %-56s %9.2f ns   NET %8.2f   faults %llu/%llu%s\n",
               "FULL cycle, handler unprotects with TLBI VAAE1IS", s.p50, s.p50 - comp[0],
               (unsigned long long)f, (unsigned long long)(NPROT * CY_ROUNDS),
               f == NPROT * CY_ROUNDS ? "" : "  ** FAULT COUNT MISMATCH");
        printf("      component: bare store over the same VA stream        %8.2f ns\n", comp[0]);
        printf("      component: RW->RO->RW descriptor edits, no barriers  %8.2f ns\n", comp[1]);
        printf("      component: dsb + TLBI VAAE1IS + dsb + isb            %8.2f ns\n", comp[2]);
        printf("      component: full permission round trip (2 edits+2 TLBI) %6.2f ns\n", comp[3]);
        printf("      => of the %.0f ns, TLB maintenance is %.0f ns (%.0f%%): one TLBI in the\n"
               "         handler to make the page writable, one in the loop to re-protect it.\n",
               s.p50 - comp[0], 2 * comp[2], 200.0 * comp[2] / (s.p50 - comp[0]));
    }

    if (!cycle_run(h_unprot_notlbi, 1, &s, comp, &f, &upg)) {
        printf("  %-56s %9.2f ns   NET %8.2f   faults %llu (expected %llu if one per store)\n",
               "FULL cycle, handler unprotects WITHOUT a TLBI", s.p50, s.p50 - comp[0],
               (unsigned long long)f, (unsigned long long)(NPROT * CY_ROUNDS));
        printf("      retries that still faulted at the same FAR (stale RO entry was cached): %llu\n",
               (unsigned long long)upg);
        printf("      -> %s\n", upg == 0
               ? "a read-only -> writable UPGRADE took effect with no invalidate on this hardware."
                 " Architecturally NOT guaranteed; re-validate per CPU before relying on it."
               : "the stale read-only entry WAS cached. Un-write-protecting a page REQUIRES a"
                 " TLBI: unlike an invalid->valid shadow fill (doc 16 §2), this one cannot be"
                 " elided, because a valid restrictive entry is architecturally cacheable.");
    }
}

// ==================================================================
// icache -- what ARM makes us pay after rewriting code
// ==================================================================
#define NIC 11
static const char *IC_BODY[NIC] = {
    "dsb ish; isb                       (floor)",
    "dc cvau + ic ivau interleaved, one dsb+isb",
    "ic ivau x N;  dsb ish; isb",
    "dc cvau x N;  dsb ish; isb",
    "ic iallu;     dsb ish; isb        (whole I-cache)",
    "CANONICAL: dc x N; dsb; ic x N; dsb; isb",
    "execute the region, I-cache warm  (floor)",
    "ic ivau x N; dsb; isb; execute    (refill)",
    "rewrite + dc + dsb + ic + dsb + isb + execute",
    "rewrite only, no maintenance, no execute",
    "ic iallu; dsb; isb; execute       (refill)",
};
// bodies 0..5 are pure maintenance (per-line figures are meaningful);
// bodies 6..10 execute the region (per-line figures are not)
#define IC_IS_MAINT(b) ((b) <= 5)
static uint64_t IC_ROUNDS = 15;
static uint64_t g_ct_work[MAXV];   // contender activity witness

static int icache_run(int n, uint64_t bytes, uint64_t iters, int ckind, double out[NIC]) {
    arena_t a = { OFF_SHAREDPT, OFF_SHAREDPT_E };
    uint64_t l3 = 0, root = build_world(&a, &l3, 0);
    fill_code(OFF_CODEREG, bytes);
    for (int i = 1; i < n; i++) fill_code(slab(i) + S_CODE, 4096);
    reset_globals();
    for (int i = 0; i < n; i++) {
        clear_slab(i);
        uint64_t *p = P(i);
        if (i == 0) {
            p[0] = RAM_IPA + OFF_CODEREG; p[1] = bytes; p[2] = iters; p[3] = IC_ROUNDS;
            p[4] = RAM_IPA + OFF_GLOBAL + G_STOP;
            g_ctx[i].mode = 4;
        } else {
            // ckind 4 = "execute the SAME region vCPU 0 is invalidating": a true cross-core
            // I-cache shootdown, as opposed to ckind 3 where each vCPU has its own region.
            p[0] = (ckind == 1) ? RAM_IPA + slab(i) + S_WS
                 : (ckind == 4) ? RAM_IPA + OFF_CODEREG
                                : RAM_IPA + slab(i) + S_CODE;
            p[1] = (ckind == 1) ? (4ULL << 20) : (ckind == 4) ? bytes : 4096;
            p[2] = RAM_IPA + OFF_GLOBAL + G_STOP;
            p[3] = (ckind == 4) ? 3 : (uint64_t)ckind;
            p[4] = 200000000ULL;
            g_ctx[i].mode = 5;
        }
        set_common(i, 1, (uint64_t)n);
        set_handler_state(i, 0, l3);
        g_ctx[i].ttbr = RAM_IPA + root;
    }
    install_vectors(h_skip);
    if (run_group(n, 300)) return 1;
    for (int b = 0; b < NIC; b++)
        out[b] = stats(RES(0) + (uint64_t)b * IC_ROUNDS, (int)IC_ROUNDS, (double)iters).p50;
    for (int i = 0; i < n; i++) g_ct_work[i] = AUX(i)[8];
    return 0;
}

static void exp_icache(void) {
    printf("\n=== 3. I-cache / D-cache maintenance after rewriting code, solo ===\n");
    printf("  The region is real executable code (`add x0,x0,#1` x N, then `ret`), identity\n"
           "  mapped RW+X at EL1. Every figure is per WHOLE-REGION pass; the per-64-B-step and\n"
           "  per-4-KiB-page columns are that divided by the region size.\n");
    static const uint64_t sizes[] = { 64, 256, 1024, 4096, 16384, 65536 };
    for (unsigned k = 0; k < sizeof(sizes)/sizeof(sizes[0]); k++) {
        uint64_t bytes = sizes[k];
        uint64_t iters = 4194304ULL / bytes;
        if (iters < 16) iters = 16;
        double o[NIC];
        printf("\n  -- region = %llu bytes (%llu 64-B steps), %llu passes per timed sample\n",
               (unsigned long long)bytes, (unsigned long long)(bytes / 64),
               (unsigned long long)iters);
        if (icache_run(1, bytes, iters, 0, o)) { printf("     FAILED\n"); continue; }
        printf("     %-46s %11s %10s %10s\n", "sequence", "ns/pass", "ns/64B", "ns/4KiB");
        for (int b = 0; b < NIC; b++) {
            double perline = (o[b] - o[0]) / (double)(bytes / 64);
            if (b == 0)               printf("     %-46s %11.2f %10s %10s\n", IC_BODY[b], o[b], "-", "-");
            else if (IC_IS_MAINT(b))  printf("     %-46s %11.2f %10.4f %10.2f\n", IC_BODY[b], o[b],
                                             perline, perline * 64.0);
            else                      printf("     %-46s %11.2f %10s %10s\n", IC_BODY[b], o[b], ".", ".");
        }
        printf("     derived: I-cache REFILL of %llu B after IC IVAU  = %8.2f ns  "
               "(exec-after-ic - exec-warm - the ic loop)\n",
               (unsigned long long)bytes, o[7] - o[6] - (o[2] - o[0]));
        printf("     derived: I-cache REFILL after IC IALLU           = %8.2f ns  "
               "(exec-after-iallu - exec-warm - the iallu op)\n",
               o[10] - o[6] - (o[4] - o[0]));
        printf("     derived: whole SMC turnaround per %llu B         = %8.2f ns  "
               "(rewrite + canonical maintenance + execute, minus a warm execute)\n",
               (unsigned long long)bytes, o[8] - o[6]);
    }
}

static void exp_smp(void) {
    printf("\n=== 4. The same at 1/2/4/8 vCPUs -- IC IVAU is broadcast to the "
           "inner-shareable domain ===\n");
    printf("  vCPU 0 measures; vCPUs 1..N-1 contend. Every N is bracketed by a freshly measured\n"
           "  N=1 run so drift cannot masquerade as scaling.\n");
    static const int Ns[] = { 1, 2, 4, 8 };
    static const char *kname[5] = { "IDLE-spinning",
                                    "walking their own 4 KiB pages (4 MiB each)",
                                    "issuing their own IC IVAU storms over their own 4 KiB",
                                    "executing their own 4 KiB code region (I-fetch pressure)",
                                    "executing THE SAME region vCPU 0 invalidates "
                                    "(true cross-core I-cache shootdown)" };
    // long enough that a single run is ~0.3 s: at 64 passes/sample the whole sweep finished in
    // 1.7 s and the solo bracket alone varied by 13%, which is not a measurement.
    const uint64_t bytes = 4096, iters = 1024;
    uint64_t saved_rounds = IC_ROUNDS; IC_ROUNDS = 21;
    for (int ck = 0; ck < 5; ck++) {
        printf("\n  -- contenders: %s   (region 4 KiB = 64 steps, %llu passes/sample)\n",
               kname[ck], (unsigned long long)iters);
        printf("     %-46s", "sequence");
        for (unsigned k = 0; k < sizeof(Ns)/sizeof(Ns[0]); k++)
            if (Ns[k] <= g_maxv) printf(" %9d", Ns[k]);
        printf("   (ns/pass, and xsolo)\n");
        double res[NIC][8], sol[NIC][8]; int have[8], nk = 0;
        for (unsigned k = 0; k < sizeof(Ns)/sizeof(Ns[0]); k++) {
            int n = Ns[k];
            if (n > g_maxv) continue;
            double so[NIC], out[NIC];
            if (icache_run(1, bytes, iters, 0, so)) for (int b = 0; b < NIC; b++) so[b] = 0;
            if (n == 1) memcpy(out, so, sizeof out);
            else if (icache_run(n, bytes, iters, ck, out)) for (int b = 0; b < NIC; b++) out[b] = 0;
            for (int b = 0; b < NIC; b++) { res[b][nk] = out[b]; sol[b][nk] = so[b]; }
            have[nk] = n; nk++;
        }
        for (int b = 0; b < NIC; b++) {
            printf("     %-46s", IC_BODY[b]);
            for (int k = 0; k < nk; k++) {
                if (res[b][k] == 0) printf("        --");
                else printf(" %9.2f", res[b][k]);
            }
            printf("  |");
            for (int k = 0; k < nk; k++) {
                if (res[b][k] == 0 || sol[b][k] == 0) printf("      --");
                else printf(" %5.2fx", res[b][k] / sol[b][k]);
            }
            printf("\n");
        }
        printf("     contender work units (vCPU 1..N-1) at N=8:");
        for (int i = 1; i < (g_maxv < 8 ? g_maxv : 8); i++)
            printf(" %llu", (unsigned long long)g_ct_work[i]);
        printf("%s\n", g_ct_work[1] ? "" : "   ** CONTENDERS DID NO WORK");
        (void)have;
    }
    IC_ROUNDS = saved_rounds;
}

// ==================================================================
// hash -- the cost of the "checksum the code page" design point
// ==================================================================
static void exp_hash(void) {
    printf("\n=== 5. Cost of validating a code page by content instead of by protection ===\n");
    arena_t a = { OFF_SHAREDPT, OFF_SHAREDPT_E };
    uint64_t l3 = 0, root = build_world(&a, &l3, 0);
    // A and B identical, so the byte-compare arms always run to the end (worst case)
    memset(g_mem + OFF_CMP, 0x5a, 4ULL << 20);
    memset(g_mem + OFF_CMP + 0x200000, 0x5a, 2ULL << 20);
    // ...except one byte, for the early-exit arm
    g_mem[OFF_CMP + 0x201000] ^= 0xff;
    reset_globals(); clear_slab(0);
    uint64_t *p = P(0);
    const uint64_t BYTES = 4096, OUTER = 256, ROUNDS = 15;
    p[0] = RAM_IPA + OFF_CMP; p[1] = BYTES; p[2] = OUTER; p[3] = ROUNDS;
    set_common(0, 0, 1); set_handler_state(0, 0, l3);
    g_ctx[0].mode = 3; g_ctx[0].ttbr = RAM_IPA + root;
    install_vectors(h_skip);
    if (run_group(1, 180)) { printf("  FAILED\n"); return; }
    static const char *names[5] = {
        "byte-compare 4 KiB against a shadow copy, cache-warm",
        "byte-compare 4 KiB, rotating over 2 MiB (colder)",
        "byte-compare, mismatch in the first 16 bytes (early exit)",
        "64-bit multiply-accumulate checksum of 4 KiB, warm",
        "64-bit multiply-accumulate checksum of 4 KiB, rotating over 2 MiB",
    };
    printf("  %-56s %11s %12s\n", "variant", "ns/4KiB", "GB/s");
    for (int b = 0; b < 5; b++) {
        st_t s = stats(RES(0) + (uint64_t)b * ROUNDS, (int)ROUNDS, (double)OUTER);
        printf("  %-56s %11.2f %12.2f\n", names[b], s.p50, 4096.0 / s.p50);
    }
    printf("\n  Compare against: one permission fault (§1), and one 4 KiB page of I-cache\n"
           "  maintenance (§3). A hash must be recomputed on every block ENTRY to be sound,\n"
           "  a fault is paid only on an actual write.\n");
}

// ==================================================================
int main(int argc, char **argv) {
    const char *what = argc > 1 ? argv[1] : "all";

    uint32_t maxv = 0;
    hv_vm_get_max_vcpu_count(&maxv);
    printf("=== SMC detection probe ===\n");
    {
        char brand[128]; size_t bl = sizeof brand;
        if (sysctlbyname("machdep.cpu.brand_string", brand, &bl, NULL, 0) == 0)
            printf("  host: %s\n", brand);
        int np = 0, ne = 0; size_t l = sizeof(int);
        sysctlbyname("hw.perflevel0.physicalcpu", &np, &l, NULL, 0);
        l = sizeof(int);
        sysctlbyname("hw.perflevel1.physicalcpu", &ne, &l, NULL, 0);
        printf("  cores: %d performance + %d efficiency, max vCPUs %u\n", np, ne, maxv);
    }
    g_maxv = (int)(maxv < MAXV ? maxv : MAXV);

    HV(hv_vm_create(NULL));
    g_mem = mmap(NULL, RAM_SIZE, PROT_READ | PROT_WRITE, MAP_ANON | MAP_PRIVATE, -1, 0);
    if (g_mem == MAP_FAILED) { perror("mmap"); return 2; }
    memset(g_mem, 0, RAM_SIZE);
    HV(hv_vm_map(g_mem, RAM_IPA, RAM_SIZE, HV_MEMORY_READ | HV_MEMORY_WRITE | HV_MEMORY_EXEC));
    memcpy(g_mem + OFF_CODE, smc_start, (size_t)(smc_end - smc_start));
    printf("  payload %zu bytes\n", (size_t)(smc_end - smc_start));

    int all = strcmp(what, "all") == 0;
    exp_info();                                          // always: it calibrates the clock
    if (all || !strcmp(what, "wp"))     exp_wp();
    if (all || !strcmp(what, "cycle"))  exp_cycle();
    if (all || !strcmp(what, "icache")) exp_icache();
    if (all || !strcmp(what, "smp"))    exp_smp();
    if (all || !strcmp(what, "hash"))   exp_hash();

    hv_vm_destroy();
    printf("\n  failures: %d   EL2 traps seen anywhere: %d\n", g_fail, g_traps);
    return g_fail ? 1 : 0;
}
