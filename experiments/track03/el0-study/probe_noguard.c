// EL0-without-guard probe: what the hardware does for every x86-non-canonical address, for execute-only and read-only
// pages, for page-crossing stores, and for the stage-2 variants of the stop check. Independent of natemu.
// One vCPU, 4 KiB stage 1 with TTBR0 and TTBR1 (natemu's TCR_BOTH: T0SZ = T1SZ = 16, TBI off), SCTLR 0x30d01985.
// usage: probe_noguard [reps] [scale]
#include <Hypervisor/Hypervisor.h>
#include <pthread.h>
#include <pthread/qos.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>

#define BASE UINT64_C(0x40000000)
#define SIZE 0x400000u
// data pages (ram offsets; identity-mapped unless noted)
enum { D_RO = 0x100000, D_A = 0x104000, D_B = 0x105000, D_C = 0x106000, /* D_C + 0x1000 unmapped */ D_S2 = 0x108000,
       D_STK0 = 0x10c000, D_STK1 = 0x10d000, P_PRIV = 0x110000, P_WIN = 0x111000, P_LTOP = 0x112000, P_UBOT = 0x113000,
       P_TOP = 0x114000, ROOT0 = 0x200000, ROOT1 = 0x201000 };
#define VA_PRIV UINT64_C(0x0000800040000000)   // natemu's PRIVATE (EL1-only here)
#define VA_WIN  UINT64_C(0xffff400000000000)   // a candidate EL0-RW window in the TTBR1-side hole
#define VA_LTOP UINT64_C(0x00007ffffffff000)   // last canonical lower page
#define VA_UBOT UINT64_C(0xffff800000000000)   // first canonical upper page
#define VA_TOP  UINT64_C(0xfffffffffffff000)   // last page of the address space

__asm__(
".text\n.balign 16384\n_g_start:\n"
"_vectors:\n"
"  b _sync\n .balign 128\n b Lother\n .balign 128\n b Lother\n .balign 128\n b Lother\n .balign 128\n"
"  b _sync\n .balign 128\n b Lother\n .balign 128\n b Lother\n .balign 128\n b Lother\n .balign 128\n"
"  b _sync\n .balign 128\n b Lother\n .balign 128\n b Lother\n .balign 128\n b Lother\n .balign 128\n"
"  b Lother\n .balign 128\n b Lother\n .balign 128\n b Lother\n .balign 128\n b Lother\n .balign 128\n"
"_sync:\n  mrs x9, esr_el1\n  lsr x10, x9, #26\n  cmp x10, #0x15\n  b.ne Lother\n  tst x9, #0xffff\n  b.ne Ldone\n  eret\n"
"Ldone:\n  hvc #2\n  b .\n"
"Lother:\n  mrs x9, esr_el1\n  mrs x10, far_el1\n  mrs x11, elr_el1\n  mrs x12, spsr_el1\n  hvc #1\n  b .\n"
".balign 4096\n_t_start:\n"
".macro T0\n  isb\n  mrs x20, cntvct_el0\n.endm\n"
".macro T1\n  isb\n  mrs x21, cntvct_el0\n  sub x0, x21, x20\n  mrs x1, cntfrq_el0\n  svc #1\n.endm\n"
"_t_empty:\n  T0\n1: subs x0, x0, #1\n  b.ne 1b\n  T1\n"
// stop check per block entry: today's store to an EL0-RW stop word, versus an instruction fetch (bl to `ret` in XO)
"_t_stopst:\n  T0\n1: str xzr, [x28, #3776]\n  subs x0, x0, #1\n  b.ne 1b\n  T1\n"
"_t_stopld:\n  T0\n1: ldr xzr, [x28, #3776]\n  subs x0, x0, #1\n  b.ne 1b\n  T1\n"
"_t_blpad:\n  T0\n1: bl _xo_pad\n  subs x0, x0, #1\n  b.ne 1b\n  T1\n"
// computed exit with the next RIP in x0 (no frame): L1 as data (EL0 read-only, x27) versus L1 as code (execute-only)
"_t_dd:\n  T0\n  mov x0, #0\n  b _dd_blk0\n_t_dd_end:\nLdd_end:\n  T1\n"
"_t_cd:\n  T0\n  mov x0, #0\n  b _cd_blk0\n_t_cd_end:\nLcd_end:\n  T1\n"
".macro C name, insn:vararg\n  .balign 16\n_c_\\name:\n  \\insn\n  mov x0, #0x600d\n  svc #1\n.endm\n"
"C ld8, ldr x2, [x15]\n"
"C st8, str x2, [x15]\n"
"C st4, str w2, [x15]\n"
"C stp, stp x2, x3, [x15]\n"
"C stq, str q0, [x15]\n"
"C br, br x15\n"
".balign 16384\n_xo_start:\n"     // execute-only at EL0 (AP 00, UXN 0, PXN 1), alone in its 16 KiB granule
"_xo_exec:\n  mov x0, #0x600d\n  svc #1\n"
"_xo_lit:\n  ldr x2, 1f\n  mov x0, #0x600d\n  svc #1\n  .balign 8\n1: .quad 0x1234\n"
"_xo_pad:\n  ret\n"
"_dd_disp:\n  and x2, x0, #15\n  add x1, x27, x2, lsl #4\n  ldp x2, x3, [x1]\n  sub x2, x2, x0\n  cbnz x2, Lxo_miss\n  br x3\n"
"_xo_miss:\nLxo_miss:\n  mov x0, #0xdead\n  svc #1\n"
"_cd_disp:\n  and x2, x0, #15\n  adr x1, Lcd_tbl\n  add x1, x1, x2, lsl #5\n  br x1\n"
".balign 64\n_cd_tbl:\nLcd_tbl:\n  .skip 16 * 32\n"          // filled by the host: movz/movk x2 = rip; sub x2, x2, x0; cbnz x2, miss; b block
".balign 64\n_dd_blk0:\n  subs x22, x22, #1\n  b.eq Ldd_end\n  mov x0, #1\n  b _dd_disp\n"
".irp i, 1,2,3,4,5,6,7,8,9,10,11,12,13,14,15\n  .balign 64\n_dd_blk\\i:\n  mov x0, #((\\i+1)&15)\n  b _dd_disp\n.endr\n"
".balign 64\n_cd_blk0:\n  subs x22, x22, #1\n  b.eq Lcd_end\n  mov x0, #1\n  b _cd_disp\n"
".irp i, 1,2,3,4,5,6,7,8,9,10,11,12,13,14,15\n  .balign 64\n_cd_blk\\i:\n  mov x0, #((\\i+1)&15)\n  b _cd_disp\n.endr\n"
".balign 16384\n_g_end:\n");
#define SYM(n) extern const char n[];
SYM(g_start) SYM(g_end) SYM(t_start) SYM(t_empty) SYM(t_stopst) SYM(t_blpad) SYM(t_stopld) SYM(t_dd) SYM(t_cd) SYM(c_ld8) SYM(c_st8)
SYM(c_st4) SYM(c_stp) SYM(c_stq) SYM(c_br) SYM(xo_start) SYM(xo_exec) SYM(xo_lit) SYM(cd_tbl) SYM(dd_blk0) SYM(cd_blk0) SYM(cd_disp)
SYM(dd_blk1) SYM(cd_blk1) SYM(xo_miss)

static uint8_t *ram;
static hv_vcpu_t v;
static hv_vcpu_exit_t *ex;
static void check(hv_return_t rc, const char *s) { if (rc != HV_SUCCESS) { fprintf(stderr, "FAIL %s rc=0x%x\n", s, rc); exit(2); } }
#define HV(x) check((x), #x)
static uint64_t ga(const char *s) { return BASE + (uint64_t)(s - g_start); }
static uint64_t reg(hv_reg_t r) { uint64_t x; HV(hv_vcpu_get_reg(v, r, &x)); return x; }

static uint64_t next_table = 0x202000;
static uint64_t *tab(uint64_t off) { return (uint64_t *)(ram + off); }
static void map(uint64_t root, uint64_t va, uint64_t pa, uint64_t attrs) {
    uint64_t off = root;
    for (int level = 0; level < 3; level++) {
        uint64_t *e = &tab(off)[va >> (39 - 9 * level) & 511];
        if (!*e) { *e = (BASE + next_table) | 3; next_table += 0x1000; }
        off = (*e & UINT64_C(0xfffffffff000)) - BASE;
    }
    tab(off)[va >> 12 & 511] = pa | attrs;
}

struct res { int done; uint64_t x0, x1, esr, far, elr, spsr; };
static struct res run(uint64_t pc, int el, uint64_t x0, uint64_t x15) {
    struct res r = {0};
    HV(hv_vcpu_set_reg(v, HV_REG_PC, pc)); HV(hv_vcpu_set_reg(v, HV_REG_CPSR, el ? 0x3c4 : 0x3c0));
    HV(hv_vcpu_set_sys_reg(v, HV_SYS_REG_SP_EL0, BASE + D_STK0 + 0x1000)); HV(hv_vcpu_set_sys_reg(v, HV_SYS_REG_SP_EL1, BASE + D_STK1 + 0x1000));
    uint64_t x[29] = {0};
    x[0] = x0; x[2] = UINT64_C(0x2222222222222222); x[3] = UINT64_C(0x3333333333333333); x[15] = x15; x[22] = x0;
    x[27] = BASE + D_RO; x[28] = BASE + D_S2 + 8 - 3776;
    for (int i = 0; i < 29; i++) HV(hv_vcpu_set_reg(v, HV_REG_X0 + i, x[i]));
    hv_simd_fp_uchar16_t q; memset(&q, 0x44, sizeof q); HV(hv_vcpu_set_simd_fp_reg(v, HV_SIMD_FP_REG_Q0, q));
    HV(hv_vcpu_run(v));
    if (ex->reason != HV_EXIT_REASON_EXCEPTION || ex->exception.syndrome >> 26 != 0x16) {
        r.done = -1; r.esr = ex->exception.syndrome; r.far = ex->exception.virtual_address; r.elr = reg(HV_REG_PC); r.x0 = ex->reason;
        return r;
    }
    r.done = (ex->exception.syndrome & 0xffff) == 2; r.x0 = reg(HV_REG_X0); r.x1 = reg(HV_REG_X1);
    r.esr = reg(HV_REG_X9); r.far = reg(HV_REG_X10); r.elr = reg(HV_REG_X11); r.spsr = reg(HV_REG_X12);
    return r;
}
static const char *dfsc(uint64_t esr) {
    unsigned f = esr & 63;
    static char b[32];
    if (f >= 4 && f <= 7) snprintf(b, sizeof b, "translation L%u", f - 4);
    else if (f >= 12 && f <= 15) snprintf(b, sizeof b, "permission L%u", f - 12);
    else if (f >= 8 && f <= 11) snprintf(b, sizeof b, "access-flag L%u", f - 8);
    else snprintf(b, sizeof b, "dfsc %#x", f);
    return b;
}
// one check: prints how the access ended, and for stores the 32 bytes around `peek` (a ram offset) afterwards
static void one(const char *name, const char *code, int el, uint64_t addr, long peek) {
    if (peek >= 0) memset(ram + peek - 16, 0x11, 32);
    struct res r = run(ga(code), el, 0, addr);
    printf("check %-26s EL%d addr=%#018llx ", name, el, (unsigned long long)addr);
    if (r.done < 0) printf("HOST exit reason=%llu syndrome=%#llx EC=%#llx %s ipa=%#llx",
        (unsigned long long)r.x0, (unsigned long long)r.esr, (unsigned long long)(r.esr >> 26), dfsc(r.esr),
        (unsigned long long)ex->exception.physical_address);
    else if (r.done) printf("completed");
    else printf("FAULT EC=%#llx %s WnR=%llu FAR=%#018llx FAR-addr=%+lld", (unsigned long long)(r.esr >> 26), dfsc(r.esr),
        (unsigned long long)(r.esr >> 6 & 1), (unsigned long long)r.far, (long long)(r.far - addr));
    if (peek >= 0) {
        printf(" bytes[-16..+16)=");
        for (int i = -16; i < 16; i++) printf("%s%02x", i == 0 ? "|" : "", ram[peek + i]);
    }
    printf("\n");
}

static int cmpd(const void *a, const void *b) { double x = *(const double *)a, y = *(const double *)b; return x < y ? -1 : x > y; }
static double timed(const char *name, uint64_t pc, int el, uint64_t n, int reps, double per, double sub) {
    double d[64];
    for (int k = 0; k < reps; k++) {
        struct res r = run(pc, el, n, 0);
        if (r.done != 1 || r.x0 == 0xdead) { fprintf(stderr, "FAIL %s: esr=%#llx far=%#llx elr=%#llx x0=%#llx\n", name,
            (unsigned long long)r.esr, (unsigned long long)r.far, (unsigned long long)r.elr, (unsigned long long)r.x0); exit(4); }
        d[k] = (double)r.x0 * 1e9 / (double)r.x1 / (double)n / per - sub;
    }
    qsort(d, reps, sizeof d[0], cmpd);
    printf("time %-34s EL%d n=%-9llu ns_per_op median=%.3f min=%.3f max=%.3f reps=%d\n", name, el, (unsigned long long)n, d[reps / 2], d[0], d[reps - 1], reps);
    return d[reps / 2];
}

int main(int argc, char **argv) {
    setbuf(stdout, NULL);
    int reps = argc > 1 ? atoi(argv[1]) : 7; double scale = argc > 2 ? atof(argv[2]) : 1.0;
    pthread_set_qos_class_self_np(QOS_CLASS_USER_INTERACTIVE, 0);
    size_t blob = g_end - g_start;
    if (blob > D_RO) { fprintf(stderr, "blob too big %zu\n", blob); return 1; }
    ram = mmap(NULL, SIZE, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANON, -1, 0);
    memcpy(ram, g_start, blob);
    const uint64_t PAGE = 3 | 1u << 10 | 3u << 8, EL0RW = 1u << 6, RO = 3u << 6, PXN = 1ull << 53, UXN = 1ull << 54;
    uint64_t xo = (uint64_t)(xo_start - g_start), t = (uint64_t)(t_start - g_start);
    map(ROOT0, BASE, BASE, PAGE | UXN);                                                     // vectors: EL1 only
    for (uint64_t o = t; o < xo; o += 0x1000) map(ROOT0, BASE + o, BASE + o, PAGE | RO);    // test code: RO, X at both
    for (uint64_t o = xo; o < blob; o += 0x1000) map(ROOT0, BASE + o, BASE + o, PAGE | PXN); // AP 00 UXN 0: EL0 execute-only
    map(ROOT0, BASE + D_RO, BASE + D_RO, PAGE | RO | PXN | UXN);                            // lookup-table role: EL0 read-only
    map(ROOT0, BASE + D_A, BASE + D_A, PAGE | EL0RW | PXN | UXN);
    map(ROOT0, BASE + D_B, BASE + D_B, PAGE | RO | PXN | UXN);
    map(ROOT0, BASE + D_C, BASE + D_C, PAGE | EL0RW | PXN | UXN);
    map(ROOT0, BASE + D_S2, BASE + D_S2, PAGE | EL0RW | PXN | UXN);
    map(ROOT0, BASE + D_STK0, BASE + D_STK0, PAGE | EL0RW | PXN | UXN);
    map(ROOT0, BASE + D_STK1, BASE + D_STK1, PAGE | PXN | UXN);
    map(ROOT0, VA_PRIV, BASE + P_PRIV, PAGE | PXN | UXN);                                   // natemu private: EL1 only
    map(ROOT0, VA_LTOP, BASE + P_LTOP, PAGE | EL0RW | PXN | UXN);
    map(ROOT1, VA_WIN, BASE + P_WIN, PAGE | EL0RW | PXN | UXN);
    map(ROOT1, VA_UBOT, BASE + P_UBOT, PAGE | EL0RW | PXN | UXN);
    map(ROOT1, VA_TOP, BASE + P_TOP, PAGE | EL0RW | PXN | UXN);
    // L1 as data: rip i -> dd_blk i; L1 as code: entry i = movz/movk x2 = i; sub x2, x2, x0; cbnz x2, miss; b cd_blk i
    uint64_t *lk = (void *)(ram + D_RO);
    for (int i = 0; i < 16; i++) { lk[2 * i] = (uint64_t)i; lk[2 * i + 1] = ga(dd_blk0) + (i ? (uint64_t)(dd_blk1 - dd_blk0) + 64 * (uint64_t)(i - 1) : 0); }
    for (int i = 0; i < 16; i++) {
        uint32_t *e = (uint32_t *)(ram + (cd_tbl - g_start) + 32 * i);
        uint64_t rip = (uint64_t)i, at = ga(cd_tbl) + 32 * (uint64_t)i;
        uint64_t blk = ga(cd_blk0) + (i ? (uint64_t)(cd_blk1 - cd_blk0) + 64 * (uint64_t)(i - 1) : 0), miss = ga(xo_miss);
        e[0] = 0xd2800002 | (uint32_t)(rip & 0xffff) << 5;
        for (int hw = 1; hw < 4; hw++) e[hw] = 0xf2800002 | (uint32_t)hw << 21 | (uint32_t)(rip >> (16 * hw) & 0xffff) << 5;
        e[4] = 0xcb000042;
        e[5] = 0xb5000002 | (uint32_t)(((int64_t)(miss - (at + 20)) / 4) & 0x7ffff) << 5;   // miss: x0 = 0xdead
        e[6] = 0x14000000 | (uint32_t)(((int64_t)(blk - (at + 24)) / 4) & 0x3ffffff);
        e[7] = 0xd503201f;
    }
    __asm__ volatile("dsb sy" ::: "memory");

    HV(hv_vm_create(NULL)); HV(hv_vm_map(ram, BASE, SIZE, HV_MEMORY_READ | HV_MEMORY_WRITE | HV_MEMORY_EXEC));
    HV(hv_vcpu_create(&v, &ex, NULL));
    const uint64_t tcr = 16 | 1ull << 8 | 1ull << 10 | 3ull << 12 | 16ull << 16 | 1ull << 24 | 1ull << 26 | 3ull << 28 | 2ull << 30 | 2ull << 32;
    HV(hv_vcpu_set_sys_reg(v, HV_SYS_REG_MAIR_EL1, 0xff)); HV(hv_vcpu_set_sys_reg(v, HV_SYS_REG_TCR_EL1, tcr));
    HV(hv_vcpu_set_sys_reg(v, HV_SYS_REG_TTBR0_EL1, BASE + ROOT0)); HV(hv_vcpu_set_sys_reg(v, HV_SYS_REG_TTBR1_EL1, BASE + ROOT1));
    HV(hv_vcpu_set_sys_reg(v, HV_SYS_REG_SCTLR_EL1, 0x30d01985));
    HV(hv_vcpu_set_sys_reg(v, HV_SYS_REG_ACTLR_EL1, 2)); HV(hv_vcpu_set_sys_reg(v, HV_SYS_REG_CPACR_EL1, 3u << 20));
    HV(hv_vcpu_set_sys_reg(v, HV_SYS_REG_CNTKCTL_EL1, 3)); HV(hv_vcpu_set_sys_reg(v, HV_SYS_REG_VBAR_EL1, BASE));
    hv_ipa_granule_t gr; HV(hv_vm_config_get_default_ipa_granule(&gr)); uint64_t G = gr == HV_IPA_GRANULE_4KB ? 0x1000 : 0x4000;
    printf("blob=%zu xo=%#llx tcr=%#llx ipa_granule=%#llx\n", blob, (unsigned long long)ga(xo_start), (unsigned long long)tcr, (unsigned long long)G);

    // 1. execute-only and read-only pages
    one("xo exec (br)", c_br, 0, ga(xo_exec), -1);
    one("xo ldr-literal inside xo", c_br, 0, ga(xo_lit), -1);
    one("xo ld8", c_ld8, 0, ga(xo_start), -1);
    one("xo st8", c_st8, 0, ga(xo_start) + 0x800, -1);
    one("xo ld8 from EL1t", c_ld8, 1, ga(xo_start), -1);
    one("ro ld8 (lookup role)", c_ld8, 0, BASE + D_RO, -1);
    one("ro st8 (lookup role)", c_st8, 0, BASE + D_RO + 0x800, -1);
    one("el0rw ld8 from EL1t (PAN)", c_ld8, 1, VA_WIN, -1);
    // 2. every region of the 64-bit space, loads at EL0
    const struct { const char *n; uint64_t a; } as[] = {
        {"canonical lower top page", VA_LTOP}, {"canonical upper first page", VA_UBOT}, {"canonical last page", VA_TOP},
        {"window page (TTBR1 hole)", VA_WIN}, {"private page (TTBR0 hole)", VA_PRIV},
        {"TTBR0 hole first byte", UINT64_C(0x0000800000000000)}, {"TTBR0 hole last qword", UINT64_C(0x0000fffffffffff8)},
        {"TTBR1 hole first byte", UINT64_C(0xffff000000000000)}, {"TTBR1 hole last qword", UINT64_C(0xffff7ffffffffff8)},
        {"outside: 2^48", UINT64_C(0x0001000000000000)}, {"outside: 2^63", UINT64_C(0x8000000000000000)},
        {"LIST_POISON1", UINT64_C(0xdead000000000100)}, {"LIST_POISON2", UINT64_C(0xdead000000000122)},
        {"KASAN shadow offset", UINT64_C(0xdffffc0000000000)}, {"slab poison 6b", UINT64_C(0x6b6b6b6b6b6b6b6b)},
        {"truncated kernel ptr", UINT64_C(0x0000888012345678)},
    };
    for (size_t i = 0; i < sizeof as / sizeof as[0]; i++) one(as[i].n, c_ld8, 0, as[i].a, -1);
    // 3. accesses that straddle a canonical boundary, or wrap
    one("ld8 straddle lower->hole", c_ld8, 0, VA_LTOP + 0xffc, -1);
    one("st8 straddle lower->hole", c_st8, 0, VA_LTOP + 0xffc, P_LTOP + 0x1000);
    one("ld8 straddle hole->upper", c_ld8, 0, VA_UBOT - 4, -1);
    one("ld8 wrap 2^64", c_ld8, 0, VA_TOP + 0xffc, -1);
    one("st8 window page end->unmapped", c_st8, 0, VA_WIN + 0xffc, P_WIN + 0x1000);
    // 4. page-crossing stores: does a faulting store commit its first part?
    one("st8 cross into RO", c_st8, 0, BASE + D_A + 0xffc, D_A + 0x1000);
    one("st4 cross into RO", c_st4, 0, BASE + D_A + 0xffe, D_A + 0x1000);
    one("stp cross into RO at reg boundary", c_stp, 0, BASE + D_A + 0xff8, D_A + 0x1000);
    one("stp cross into RO mid-register", c_stp, 0, BASE + D_A + 0xffc, D_A + 0x1000);
    one("str q cross into RO", c_stq, 0, BASE + D_A + 0xff8, D_A + 0x1000);
    one("st8 cross into unmapped", c_st8, 0, BASE + D_C + 0xffc, D_C + 0x1000);
    one("str q cross into unmapped", c_stq, 0, BASE + D_C + 0xff1, D_C + 0x1000);
    one("st8 cross into RO from EL1t", c_st8, 1, BASE + D_A + 0xffc, D_A + 0x1000);
    // 5. stage 2: stop by an instruction fetch (no EXEC) or by any data access (no permission)
    uint64_t xg = ga(xo_start) & ~(G - 1);
    HV(hv_vm_protect(xg, G, HV_MEMORY_READ | HV_MEMORY_WRITE));
    one("stage2 no-X: EL0 exec xo", c_br, 0, ga(xo_exec), -1);
    HV(hv_vm_protect(xg, G, HV_MEMORY_READ | HV_MEMORY_WRITE | HV_MEMORY_EXEC));
    hv_return_t rc = hv_vm_protect(BASE + D_S2, G, 0);
    printf("hv_vm_protect(flags=0) rc=%#x\n", rc);
    if (rc == HV_SUCCESS) {
        one("stage2 none: EL0 ld8", c_ld8, 0, BASE + D_S2, -1);
        one("stage2 none: EL0 st8", c_st8, 0, BASE + D_S2, -1);
        HV(hv_vm_protect(BASE + D_S2, G, HV_MEMORY_READ | HV_MEMORY_WRITE | HV_MEMORY_EXEC));
    }
    one("after restore: EL0 exec xo", c_br, 0, ga(xo_exec), -1);
    // 6. timings (EL0)
    uint64_t N = (uint64_t)(2000000 * scale);
    double e0 = timed("empty-loop", ga(t_empty), 0, 10 * N, reps, 1, 0);
    timed("stop check: str xzr to stop word", ga(t_stopst), 0, 10 * N, reps, 1, e0);
    timed("stop check: ldr xzr from stop word", ga(t_stopld), 0, 10 * N, reps, 1, e0);
    timed("stop check: bl to ret in XO", ga(t_blpad), 0, 10 * N, reps, 1, e0);
    timed("exit via L1 as data (RO), XO blocks", ga(t_dd), 0, N, reps, 16, 0);
    timed("exit via L1 as code (XO)", ga(t_cd), 0, N, reps, 16, 0);
    timed("exit via L1 as data (RO), XO blocks", ga(t_dd), 0, N, reps, 16, 0);
    timed("exit via L1 as code (XO)", ga(t_cd), 0, N, reps, 16, 0);
    HV(hv_vcpu_destroy(v)); HV(hv_vm_destroy());
    return 0;
}
