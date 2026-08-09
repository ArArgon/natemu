// Precise-exception cost probe.  Measures, INSIDE a Hypervisor.framework VM at guest EL1 with
// our own VBAR_EL1, the costs of the four candidate precise-exception strategies for a
// system-level x86-64 DBT:
//
//   cal     CNTVCT granularity/overhead + a clock estimate (same method as probes/shadow_mmu)
//   spill   strategy (a): marginal cost of materialising N guest registers per x86 instruction
//   lookup  strategy (b)/(c): StackMap binary search vs hash vs QEMU's sleb128 linear scan
//   at      probe-then-execute: AT S1E1WP + ISB + PAR_EL1 for 1 and 2 operands before a store
//   pf      the whole fault path: permission fault -> full state recovery -> ERET
//   atv     correctness check that AT reports faults in PAR_EL1 without taking one
//
// Timing is CNTVCT_EL0 read in-guest, so no host exit occurs inside a measured window.
//
// Build: ../build.sh precise_exc/precise_exc.c
// Run:   ./bin/precise_exc [all|cal|spill|lookup|at|pf|atv]

#include <Hypervisor/Hypervisor.h>
#include <inttypes.h>
#include <pthread.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include "payload.h"
__asm__(PAYLOAD_ASM);

extern const uint8_t pe_start[], pe_end[];
extern const uint8_t h_skip_cnt[], h_recover[], h_recover_nosearch[], h_spillonly[], h_panic[];

// ------------------------------------------------------------------ memory map
#define RAM_IPA        0x40000000ULL
#define RAM_SIZE       0x08000000ULL          // 128 MiB

#define OFF_CODE       0x00000000ULL
#define OFF_VEC        0x00020000ULL
#define OFF_PARAM      0x00021000ULL
#define OFF_STACK      0x00024000ULL          // 8 KiB guest stack, SP at its top
#define OFF_ARENA      0x00100000ULL          // page-table arena
#define OFF_ARENA_END  0x00900000ULL
#define OFF_CTX        0x00900000ULL          // guest CPUState + spill context block
#define OFF_TABLE      0x00a00000ULL          // StackMap / hash / sleb tables, up to 4 MiB
#define OFF_TABLE_END  0x00e00000ULL
#define OFF_REGION     0x01000000ULL          // 1 MiB backing for the RO/RW test regions

// guest VAs (all under L0[128] in the 4 KB regime)
#define VA_RO          0x0000403000000000ULL
#define VA_RW          0x0000404000000000ULL
#define VA_UNMAP       0x0000402000000000ULL
#define NREGION_PAGES  128

// descriptor bits
#define D_PAGE   3ULL
#define D_BLOCK  1ULL
#define D_AF     (1ULL << 10)
#define D_SH_IS  (3ULL << 8)
#define D_AP_RO  (2ULL << 6)
#define PA_MASK  0x0000fffffffff000ULL

#define MAIR_VAL 0xFFULL
// T0SZ=16, IRGN0/ORGN0=WB, SH0=IS, T1SZ=16, EPD1=1, TG1=4K, IPS=40bit, TG0=4K
#define TCR_4K   (16ULL | (1ULL<<8) | (1ULL<<10) | (3ULL<<12) | (0ULL<<14) \
                 | (16ULL<<16) | (1ULL<<23) | (2ULL<<30) | (2ULL<<32))

#define HV(e) do { hv_return_t _r = (e); if (_r != HV_SUCCESS) { \
    fprintf(stderr, "  %s -> 0x%x at %d\n", #e, _r, __LINE__); exit(2);} } while (0)

static uint8_t   *g_mem;
static uint64_t   g_alloc;
static uint64_t   g_cntfrq = 24000000;
static double     g_ghz    = 0.0;
static int        g_fail   = 0;
static uint64_t   g_msr_traps = 0;

static uint64_t *P(void)   { return (uint64_t *)(g_mem + OFF_PARAM); }
static uint64_t *R(void)   { return (uint64_t *)(g_mem + OFF_PARAM + 0x200); }
static uint64_t *AUX(void) { return (uint64_t *)(g_mem + OFF_PARAM + 0xc00); }

// ------------------------------------------------------------------ page tables (4 KB granule)
static void reset_arena(void) { g_alloc = OFF_ARENA; }
static uint64_t alloc_tab(void) {
    g_alloc = (g_alloc + 0xfff) & ~0xfffULL;
    uint64_t off = g_alloc; g_alloc += 4096;
    if (g_alloc > OFF_ARENA_END) { fprintf(stderr, "arena exhausted\n"); exit(2); }
    memset(g_mem + off, 0, 4096);
    return off;
}
static uint64_t *tab(uint64_t off) { return (uint64_t *)(g_mem + off); }

static uint64_t walk_to(uint64_t root, uint64_t va, int level) {
    int sh[4] = { 39, 30, 21, 12 };
    int nb[4] = {  9,  9,  9,  9 };
    uint64_t cur = root;
    for (int l = 0; l < level; l++) {
        uint64_t idx = (va >> sh[l]) & ((1ULL << nb[l]) - 1);
        uint64_t *t = tab(cur);
        if ((t[idx] & 1) == 0) t[idx] = (RAM_IPA + alloc_tab()) | D_PAGE;
        cur = (t[idx] & PA_MASK) - RAM_IPA;
    }
    return cur;
}
static void map_page(uint64_t root, uint64_t va, uint64_t pa, uint64_t attrs) {
    uint64_t t3 = walk_to(root, va, 3);
    tab(t3)[(va >> 12) & 511] = (pa & PA_MASK) | attrs | D_PAGE;
}
static void map_block2(uint64_t root, uint64_t va, uint64_t pa, uint64_t attrs) {
    uint64_t t2 = walk_to(root, va, 2);
    tab(t2)[(va >> 21) & 511] = (pa & PA_MASK) | attrs | D_BLOCK;
}

// Identity-map all of guest RAM with 2 MiB L2 blocks, then add the RO/RW test regions.
static uint64_t build_tables(void) {
    reset_arena();
    uint64_t root = alloc_tab();
    for (uint64_t o = 0; o < RAM_SIZE; o += 0x200000ULL)
        map_block2(root, RAM_IPA + o, RAM_IPA + o, D_AF | D_SH_IS);
    for (int i = 0; i < NREGION_PAGES; i++) {
        uint64_t pa = RAM_IPA + OFF_REGION + (uint64_t)i * 4096;
        map_page(root, VA_RO + (uint64_t)i * 4096, pa, D_AF | D_SH_IS | D_AP_RO);
        map_page(root, VA_RW + (uint64_t)i * 4096, pa, D_AF | D_SH_IS);
    }
    // VA_UNMAP is deliberately left with no descriptor at all.
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

enum { H_NONE = 0, H_SKIP_CNT, H_SPILLONLY, H_REC_NOSEARCH, H_RECOVER };

static void install_vectors(int which) {
    uint32_t *v = (uint32_t *)(g_mem + OFF_VEC);
    uint64_t panic_off = OFF_CODE + (uint64_t)(h_panic - pe_start);
    for (int i = 0; i < 16; i++) {
        int64_t d = (int64_t)panic_off - (int64_t)(OFF_VEC + i * 0x80);
        v[i * 0x20] = 0x14000000u | (uint32_t)((d >> 2) & 0x03ffffff);
    }
    if (which == H_NONE) return;
    const uint8_t *h = NULL;
    switch (which) {
        case H_SKIP_CNT:      h = h_skip_cnt;         break;
        case H_SPILLONLY:     h = h_spillonly;        break;
        case H_REC_NOSEARCH:  h = h_recover_nosearch; break;
        case H_RECOVER:       h = h_recover;          break;
    }
    uint64_t slot = OFF_VEC + 0x200;             // current EL, SP_ELx, synchronous
    uint64_t hoff = OFF_CODE + (uint64_t)(h - pe_start);
    int64_t d = (int64_t)hoff - (int64_t)slot;
    *(uint32_t *)(g_mem + slot) = 0x14000000u | (uint32_t)((d >> 2) & 0x03ffffff);
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

static int run_guest(uint64_t mode, uint64_t root, int secs) {
    hv_vcpu_t vcpu; hv_vcpu_exit_t *vexit;
    HV(hv_vcpu_create(&vcpu, &vexit, NULL));
    HV(hv_vcpu_set_reg(vcpu, HV_REG_PC,   RAM_IPA + OFF_CODE));
    HV(hv_vcpu_set_reg(vcpu, HV_REG_CPSR, 0x3c5));                 // EL1h, DAIF masked
    HV(hv_vcpu_set_reg(vcpu, HV_REG_X0,   MAIR_VAL));
    HV(hv_vcpu_set_reg(vcpu, HV_REG_X1,   TCR_4K));
    HV(hv_vcpu_set_reg(vcpu, HV_REG_X2,   RAM_IPA + root));
    HV(hv_vcpu_set_reg(vcpu, HV_REG_X3,   RAM_IPA + OFF_VEC));
    HV(hv_vcpu_set_reg(vcpu, HV_REG_X4,   RAM_IPA + OFF_PARAM));
    HV(hv_vcpu_set_reg(vcpu, HV_REG_X5,   mode));

    wd_t w = { .vcpu = vcpu, .done = 0, .secs = secs };
    pthread_t wt; pthread_create(&wt, NULL, watchdog, &w);

    int rc = 0;
    for (;;) {
        hv_return_t r = hv_vcpu_run(vcpu);
        if (r != HV_SUCCESS) { fprintf(stderr, "  hv_vcpu_run -> 0x%x\n", r); rc = 1; break; }
        if (vexit->reason == HV_EXIT_REASON_VTIMER_ACTIVATED) { hv_vcpu_set_vtimer_mask(vcpu, true); continue; }
        if (vexit->reason == HV_EXIT_REASON_CANCELED) {
            fprintf(stderr, "  !! guest cancelled (watchdog) in mode %llu\n", (unsigned long long)mode);
            rc = 1; break;
        }
        if (vexit->reason != HV_EXIT_REASON_EXCEPTION) {
            fprintf(stderr, "  !! unexpected exit reason %u\n", vexit->reason); rc = 1; break;
        }
        uint64_t sy = vexit->exception.syndrome, ec = (sy >> 26) & 0x3f;
        if (ec == 0x16 && (sy & 0xffff) == 0) break;                // normal completion
        if (ec == 0x18) {
            uint64_t pc = 0; hv_vcpu_get_reg(vcpu, HV_REG_PC, &pc);
            hv_vcpu_set_reg(vcpu, HV_REG_PC, pc + 4);
            g_msr_traps++;
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
                (unsigned long long)sy, (unsigned long long)ec, ecname(ec), (unsigned long long)pc);
        rc = 1; break;
    }
    w.done = 1; pthread_join(wt, NULL);
    if (g_msr_traps > 0)
        fprintf(stderr, "  !! %llu system-register traps to EL2 during mode %llu\n",
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
        printf("  %-44s %9.3f ns   [%.3f .. %.3f]  %7.1f cyc\n",
               label, s.med, s.lo, s.hi, s.med * g_ghz);
    else
        printf("  %-44s %9.3f ns   [%.3f .. %.3f]\n", label, s.med, s.lo, s.hi);
}
static void clear_params(void) { memset(g_mem + OFF_PARAM, 0, 0x2000); }
static void set_handler_state(uint64_t limit, uint64_t tabva, uint64_t log2n, uint64_t ctxva) {
    uint64_t *p = P();
    p[0x1c0/8] = 0;                       // fault counter
    p[0x1c8/8] = limit;
    p[0x1d0/8] = tabva;
    p[0x1d8/8] = log2n;
    p[0x1e0/8] = ctxva;
    p[0x1e8/8] = 0;
    p[0x1f0/8] = RAM_IPA + OFF_STACK + 0x2000;   // SP_EL1
}

// ------------------------------------------------------------------ 0. calibration
static void exp_cal(uint64_t root) {
    printf("\n=== 0. calibration ===\n");
    clear_params();
    P()[0] = 200000;
    set_handler_state(0, 0, 0, 0);
    install_vectors(H_SKIP_CNT);
    if (run_guest(0, root, 30)) return;
    double it = 200000.0;
    double cnt = (double)R()[0] * 1e9 / g_cntfrq / it;
    double emp = (double)R()[1] * 1e9 / g_cntfrq / it;
    double add = (double)R()[2] * 1e9 / g_cntfrq / (it * 16.0);
    g_ghz = 1.0 / add;
    printf("  CNTFRQ_EL0                                %.0f Hz\n", (double)AUX()[3]);
    printf("  {isb; mrs cntvct} back-to-back            %8.3f ns\n", cnt);
    printf("  empty {subs;b.ne} loop iteration          %8.3f ns\n", emp);
    printf("  dependent ADD latency                     %8.3f ns  -> clock ~%.2f GHz\n", add, g_ghz);
}

// ------------------------------------------------------------------ 1. spill
static void exp_spill(uint64_t root) {
    printf("\n=== 1. strategy (a): conservative materialisation ===\n");
    printf("  All stores hit ONE 128-byte L1-resident context block: this is the BEST case\n");
    printf("  for spilling, so every figure here is a lower bound on the real cost.\n\n");
    const uint64_t ITERS = 200000, ROUNDS = 21;
    struct { int sel; const char *name; double per; } v[] = {
        { 0,  "4 ALU ops (baseline x86 insn body)",      1 },
        { 1,  "  + 1 store",                             1 },
        { 2,  "  + 2 stores",                            1 },
        { 3,  "  + 4 stores",                            1 },
        { 4,  "  + 8 stores",                            1 },
        { 5,  "  + 16 stores",                           1 },
        { 6,  "  + 16 regs via 8 STP",                   1 },
        { 7,  "  + 16 regs via 8 LDP",                   1 },
        { 8,  "  + 8 STP then 8 LDP (store-all/load-all)",1 },
        { 9,  "load+ALU+store (memory x86 insn)",        1 },
        { 10, "  + 1 store (RIP materialised)",          1 },
        { 11, "  + 3 stores (RIP + 2 flag operands)",    1 },
    };
    double med[12];
    for (size_t i = 0; i < sizeof(v)/sizeof(v[0]); i++) {
        clear_params();
        P()[0] = ITERS; P()[1] = ROUNDS; P()[2] = RAM_IPA + OFF_CTX; P()[3] = (uint64_t)v[i].sel;
        set_handler_state(0, 0, 0, 0);
        install_vectors(H_SKIP_CNT);
        if (run_guest(1, root, 60)) return;
        stat_t s = stats(R(), (int)ROUNDS, (double)ITERS);
        med[v[i].sel] = s.med;
        row(v[i].name, s);
    }
    printf("\n  marginal cost of one 8-byte store to a hot context block:\n");
    printf("    from 1 store   %6.3f ns   from 4  %6.3f ns   from 16 %6.3f ns\n",
           med[1]-med[0], (med[3]-med[0])/4.0, (med[5]-med[0])/16.0);
    printf("    16 regs as STP %6.3f ns total (%.3f ns/reg)\n", med[6]-med[0], (med[6]-med[0])/16.0);
    printf("    store-all + load-all round trip %6.3f ns\n", med[8]-med[0]);
    printf("\n  relative cost on a memory-touching x86 instruction:\n");
    printf("    +1 store  %+6.1f %%      +3 stores %+6.1f %%\n",
           100.0*(med[10]-med[9])/med[9], 100.0*(med[11]-med[9])/med[9]);
    printf("    +16 regs (STP) would be %+6.1f %% of it\n", 100.0*(med[6]-med[0])/med[9]);
}

// ------------------------------------------------------------------ 2. lookup
static uint8_t *enc_sleb(uint8_t *p, int64_t val) {   // verbatim shape of QEMU encode_sleb128
    int more, byte;
    do {
        byte = val & 0x7f;
        val >>= 7;
        more = !((val == 0 && (byte & 0x40) == 0) || (val == -1 && (byte & 0x40) != 0));
        if (more) byte |= 0x80;
        *p++ = (uint8_t)byte;
    } while (more);
    return p;
}

// Build a QEMU-format TB search table: icount rows of INSN_START_WORDS(=3) sleb128 deltas
// plus one host-offset delta.  Returns total bytes and fills *target with the host offset
// of the middle instruction (so the decoder walks icount/2 rows).
static size_t build_sleb(uint8_t *dst, int icount, uint64_t *target) {
    uint8_t *p = dst;
    uint32_t host = 0;
    unsigned rng = 12345;
    for (int i = 0; i < icount; i++) {
        rng = rng * 1103515245u + 12345u;
        int64_t dpc  = 1 + (int64_t)((rng >> 16) % 8);     // x86 insn length 1..8
        rng = rng * 1103515245u + 12345u;
        int64_t dcc  = (int64_t)((rng >> 16) % 5) - 2;     // cc_op delta, small signed
        int64_t dw2  = 0;
        rng = rng * 1103515245u + 12345u;
        int64_t dhost = 40 + (int64_t)((rng >> 16) % 48);  // host bytes per guest insn
        p = enc_sleb(p, dpc);
        p = enc_sleb(p, dcc);
        p = enc_sleb(p, dw2);
        p = enc_sleb(p, dhost);
        host += (uint32_t)dhost;
        if (i == icount / 2) *target = host;
    }
    return (size_t)(p - dst);
}

static void exp_lookup(uint64_t root) {
    printf("\n=== 2. strategy (b)/(c): fault-path metadata lookup ===\n");
    const uint64_t ITERS = 100000, ROUNDS = 21;

    printf("\n  -- (b) branchless binary search over N sorted uint32 host-PC keys --\n");
    {
        clear_params();
        P()[0] = RAM_IPA + OFF_TABLE; P()[1] = 12; P()[2] = ITERS; P()[3] = ROUNDS; P()[4] = 3;
        set_handler_state(0, 0, 0, 0); install_vectors(H_SKIP_CNT);
        if (run_guest(2, root, 60)) return;
        row("key generation only (xorshift, control)", stats(R(), (int)ROUNDS, (double)ITERS));
    }
    double rngcost = stats(R(), (int)ROUNDS, (double)ITERS).med;
    for (int k = 6; k <= 18; k += 2) {
        uint32_t *t = (uint32_t *)(g_mem + OFF_TABLE);
        size_t n = (size_t)1 << k;
        if (n * 4 > (OFF_TABLE_END - OFF_TABLE)) break;
        for (size_t i = 0; i < n; i++) t[i] = (uint32_t)(i * 4);
        clear_params();
        P()[0] = RAM_IPA + OFF_TABLE; P()[1] = (uint64_t)k; P()[2] = ITERS; P()[3] = ROUNDS; P()[4] = 0;
        set_handler_state(0, 0, 0, 0); install_vectors(H_SKIP_CNT);
        if (run_guest(2, root, 60)) return;
        stat_t s = stats(R(), (int)ROUNDS, (double)ITERS);
        char lab[96];
        snprintf(lab, sizeof lab, "N=%-7zu (%2d steps, %5zu KiB table)", n, k, n*4/1024);
        row(lab, s);
        printf("      net of key gen: %8.3f ns   (%.3f ns/step)\n",
               s.med - rngcost, (s.med - rngcost) / k);
    }

    printf("\n  -- (b') open-addressed hash: 2 ALU + 1 dependent load of a 16-byte entry --\n");
    for (int k = 10; k <= 16; k += 3) {
        uint64_t *t = (uint64_t *)(g_mem + OFF_TABLE);
        size_t n = (size_t)1 << k;
        if (n * 16 > (OFF_TABLE_END - OFF_TABLE)) break;
        for (size_t i = 0; i < n; i++) { t[2*i] = i; t[2*i+1] = i * 7; }
        clear_params();
        P()[0] = RAM_IPA + OFF_TABLE; P()[1] = (uint64_t)k; P()[2] = ITERS; P()[3] = ROUNDS; P()[4] = 1;
        set_handler_state(0, 0, 0, 0); install_vectors(H_SKIP_CNT);
        if (run_guest(2, root, 60)) return;
        stat_t s = stats(R(), (int)ROUNDS, (double)ITERS);
        char lab[96];
        snprintf(lab, sizeof lab, "N=%-7zu (%5zu KiB table)", n, n*16/1024);
        row(lab, s);
        printf("      net of key gen: %8.3f ns\n", s.med - rngcost);
    }

    printf("\n  -- (c) QEMU cpu_unwind_data_from_tb: linear sleb128 scan of the TB search table --\n");
    printf("     4 sleb128 values per guest insn (INSN_START_WORDS=3 + host-offset delta),\n");
    printf("     decoded from the start of the TB until iter_pc > host_pc.  Target = middle insn.\n");
    for (int ic = 4; ic <= 128; ic *= 2) {
        uint64_t target = 0;
        size_t bytes = build_sleb(g_mem + OFF_TABLE, ic, &target);
        clear_params();
        P()[0] = RAM_IPA + OFF_TABLE; P()[1] = (uint64_t)ic; P()[2] = ITERS / 8;
        P()[3] = ROUNDS; P()[4] = 2; P()[5] = target;
        set_handler_state(0, 0, 0, 0); install_vectors(H_SKIP_CNT);
        if (run_guest(2, root, 60)) return;
        stat_t s = stats(R(), (int)ROUNDS, (double)(ITERS / 8));
        char lab[96];
        snprintf(lab, sizeof lab, "icount=%-4d (%4zu B table, %d rows walked)", ic, bytes, ic/2 + 1);
        row(lab, s);
    }
}

// ------------------------------------------------------------------ 3. AT probing
static void exp_at(uint64_t root) {
    printf("\n=== 3. probe-then-execute with AT S1E1WP ===\n");
    const uint64_t ITERS = 100000, ROUNDS = 21, MASK = 63;
    struct { int sel; const char *name; } v[] = {
        { 0, "plain STR (control)" },
        { 5, "AT S1E1WP alone, no ISB, no PAR read" },
        { 1, "AT + ISB + MRS PAR_EL1 (1 probe)" },
        { 2, "AT + ISB + MRS PAR_EL1, then STR (1 operand)" },
        { 3, "2x{AT+ISB+MRS}, then STR (2 operands)" },
        { 4, "AT;AT;ISB;MRS then STR  [WRONG, bound only]" },
    };
    double med[8];
    printf("\n  -- on a mapped read-write region (probe succeeds) --\n");
    for (size_t i = 0; i < sizeof(v)/sizeof(v[0]); i++) {
        clear_params();
        P()[0] = VA_RW; P()[1] = MASK; P()[2] = ITERS; P()[3] = ROUNDS; P()[4] = (uint64_t)v[i].sel;
        set_handler_state(0, 0, 0, 0); install_vectors(H_SKIP_CNT);
        if (run_guest(3, root, 60)) return;
        stat_t s = stats(R(), (int)ROUNDS, (double)ITERS);
        med[v[i].sel] = s.med;
        row(v[i].name, s);
    }
    printf("\n  net cost of probing 1 operand before a store  %8.3f ns  (%.2fx the bare store)\n",
           med[2] - med[0], med[2] / med[0]);
    printf("  net cost of probing 2 operands before a store %8.3f ns  (%.2fx the bare store)\n",
           med[3] - med[0], med[3] / med[0]);
    printf("  ISB+PAR share of one probe                    %8.3f ns of %.3f ns\n",
           med[1] - med[5], med[1]);

    printf("\n  -- the same probe on a region where it FAILS (cost of a negative probe) --\n");
    struct { uint64_t va; const char *name; } r[] = {
        { VA_RW,    "AT+ISB+MRS on read-write pages   (PAR.F=0)" },
        { VA_RO,    "AT+ISB+MRS on read-only pages    (PAR.F=1, perm)" },
        { VA_UNMAP, "AT+ISB+MRS on unmapped pages     (PAR.F=1, xlat)" },
    };
    for (size_t i = 0; i < 3; i++) {
        clear_params();
        P()[0] = r[i].va; P()[1] = MASK; P()[2] = ITERS; P()[3] = ROUNDS; P()[4] = 1;
        set_handler_state(0, 0, 0, 0); install_vectors(H_SKIP_CNT);
        if (run_guest(3, root, 60)) return;
        row(r[i].name, stats(R(), (int)ROUNDS, (double)ITERS));
        printf("      last PAR_EL1 = 0x%016llx  (F=%llu)\n",
               (unsigned long long)AUX()[0], (unsigned long long)(AUX()[0] & 1));
    }
}

// ------------------------------------------------------------------ 5. AT correctness
static void exp_atv(uint64_t root) {
    printf("\n=== 5. AT correctness: does a failing probe stay fault-free? ===\n");
    clear_params();
    P()[0] = VA_RW; P()[1] = VA_RO; P()[2] = VA_UNMAP;
    set_handler_state(0, 0, 0, 0);
    install_vectors(H_SKIP_CNT);
    if (run_guest(5, root, 20)) return;
    const char *nm[3] = { "read-write page", "read-only page ", "unmapped page  " };
    for (int i = 0; i < 3; i++) {
        uint64_t rp = R()[2*i], wp = R()[2*i+1];
        printf("  %s  AT S1E1RP -> PAR=0x%016llx F=%llu %s | "
               "AT S1E1WP -> PAR=0x%016llx F=%llu %s\n",
               nm[i], (unsigned long long)rp, (unsigned long long)(rp & 1),
               (rp & 1) ? "FAULT" : "ok   ",
               (unsigned long long)wp, (unsigned long long)(wp & 1),
               (wp & 1) ? "FAULT" : "ok   ");
        if (rp & 1) printf("      RP FST=0x%llx\n", (unsigned long long)((rp >> 1) & 0x3f));
        if (wp & 1) printf("      WP FST=0x%llx\n", (unsigned long long)((wp >> 1) & 0x3f));
    }
    printf("  faults actually taken during this mode: %llu (must be 0)\n",
           (unsigned long long)AUX()[0]);
}

// ------------------------------------------------------------------ 4. the fault path
static void exp_pf(uint64_t root) {
    printf("\n=== 4. the fault path: permission fault -> recovery -> ERET ===\n");
    const uint64_t ITERS = 20000, ROUNDS = 21, MASK = 63;

    // fill a 64 K StackMap
    uint32_t *t = (uint32_t *)(g_mem + OFF_TABLE);
    for (size_t i = 0; i < (1u << 16); i++) t[i] = (uint32_t)(i * 4);

    struct { int h; uint64_t log2n; const char *name; } v[] = {
        { H_SKIP_CNT,     0,  "minimal handler (ELR+=4; ERET)" },
        { H_SPILLONLY,    0,  "  + spill/reload x0-x17, read ELR/FAR/ESR" },
        { H_REC_NOSEARCH, 0,  "  + permute 16 regs into guest CPUState" },
        { H_RECOVER,      8,  "  + StackMap binary search, N=256" },
        { H_RECOVER,      12, "  + StackMap binary search, N=4096" },
        { H_RECOVER,      16, "  + StackMap binary search, N=65536" },
        { H_SKIP_CNT,     0,  "minimal handler AGAIN (ordering check)" },
    };
    const size_t NV = sizeof(v)/sizeof(v[0]);
    double med[16];
    stat_t ctl;

    // `16 §1` found that the FIRST configuration executed in a process always pays extra.
    // Run the whole sequence twice and report only the second pass.
    for (int pass = 0; pass < 2; pass++) {
        if (pass) printf("\n  (pass 1 was a warm-up and is discarded; below is pass 2)\n\n");
        clear_params();
        P()[0] = VA_RW; P()[1] = ITERS; P()[2] = ROUNDS; P()[3] = MASK;
        set_handler_state(1, RAM_IPA + OFF_TABLE, 12, RAM_IPA + OFF_CTX);
        install_vectors(H_SKIP_CNT);
        if (run_guest(4, root, 120)) return;
        ctl = stats(R(), (int)ROUNDS, (double)ITERS);
        if (pass) {
            row("no-fault control (same stream, RW pages)", ctl);
            if (AUX()[0] != 0)
                printf("  !! control took %llu faults\n", (unsigned long long)AUX()[0]);
        }
        for (size_t i = 0; i < NV; i++) {
            clear_params();
            P()[0] = VA_RO; P()[1] = ITERS; P()[2] = ROUNDS; P()[3] = MASK;
            set_handler_state(ITERS * ROUNDS + 16, RAM_IPA + OFF_TABLE, v[i].log2n,
                              RAM_IPA + OFF_CTX);
            install_vectors(v[i].h);
            if (run_guest(4, root, 180)) return;
            stat_t s = stats(R(), (int)ROUNDS, (double)ITERS);
            med[i] = s.med;
            if (!pass) continue;
            row(v[i].name, s);
            uint64_t nf = AUX()[0];
            if (nf != ITERS * ROUNDS)
                printf("      !! fault count %llu, expected %llu\n",
                       (unsigned long long)nf, (unsigned long long)(ITERS * ROUNDS));
            printf("      NET fault round trip: %8.3f ns", s.med - ctl.med);
            if (i > 0) printf("   (+%.3f ns over the minimal handler)", s.med - med[0]);
            printf("\n");
        }
    }
    printf("\n  ordering check: minimal handler first=%.3f last=%.3f ns (%.1f %% apart)\n",
           med[0], med[NV-1], 100.0*(med[NV-1]-med[0])/med[0]);
}

// ------------------------------------------------------------------ main
int main(int argc, char **argv) {
    const char *what = argc > 1 ? argv[1] : "all";
    hv_vm_config_t cfg = hv_vm_config_create();
    HV(hv_vm_create(cfg));
    g_mem = aligned_alloc(0x4000, RAM_SIZE);
    if (!g_mem) { perror("alloc"); return 2; }
    memset(g_mem, 0, RAM_SIZE);
    HV(hv_vm_map(g_mem, RAM_IPA, RAM_SIZE, HV_MEMORY_READ | HV_MEMORY_WRITE | HV_MEMORY_EXEC));
    memcpy(g_mem + OFF_CODE, pe_start, (size_t)(pe_end - pe_start));

    printf("precise-exception cost probe — payload %zu bytes\n", (size_t)(pe_end - pe_start));
    uint64_t root = build_tables();

    int all = strcmp(what, "all") == 0;
    if (all || !strcmp(what,"cal"))    exp_cal(root);
    else                                exp_cal(root);   // always calibrate for the cycle column
    if (all || !strcmp(what,"spill"))  exp_spill(root);
    if (all || !strcmp(what,"lookup")) exp_lookup(root);
    if (all || !strcmp(what,"at"))     exp_at(root);
    if (all || !strcmp(what,"atv"))    exp_atv(root);
    if (all || !strcmp(what,"pf"))     exp_pf(root);

    printf("\n%s\n", g_fail ? "!! SOME RUNS FAILED" : "all runs completed");
    return g_fail ? 1 : 0;
}
