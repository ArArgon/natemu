// EL0 study probe: costs and traps of running translated code at EL0 instead of EL1t inside an HVF VM.
// Independent of natemu (no crates, no FEX). One vCPU, 4 KiB stage 1 (48-bit VA, TTBR0 only), identity map of a 2 MiB
// window with per-page AP/UXN/PXN, SCTLR 0x30d01985 (UCI, UCT, DZE, UMA, nTWI all 0), ACTLR EnTSO, CPACR FPEN.
// usage: probe [reps] [scale]     prints one line per measurement / trap check
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
#define PG(n) (BASE + (n) * 0x1000u)
// page map (index -> role); code pages come from the blob
enum { P_FRAME = 16, P_L1 = 17, P_EL1DATA = 18, P_UNMAPPED = 19, P_STACK0 = 20, P_STACK1 = 21, P_DATA0 = 22 };

// ---- guest code: page 0 = vectors + EL1-only handlers; pages 1.. = test code (EL0 and EL1 executable, read-only) ----
// Handler scratch: x9-x12. Tests end with `svc #1` (results in x0-x3). x19 = 1: a data abort skips the access.
__asm__(
".text\n.balign 4096\n_g_start:\n"
"_vectors:\n"
"  b _sync\n .balign 128\n b Lother\n .balign 128\n b Lother\n .balign 128\n b Lother\n .balign 128\n"   // cur EL, SP0
"  b Lother\n .balign 128\n b Lother\n .balign 128\n b Lother\n .balign 128\n b Lother\n .balign 128\n"  // cur EL, SPx
"  b _sync\n .balign 128\n b Lother\n .balign 128\n b Lother\n .balign 128\n b Lother\n .balign 128\n"   // lower EL A64
"  b Lother\n .balign 128\n b Lother\n .balign 128\n b Lother\n .balign 128\n b Lother\n .balign 128\n"  // lower EL A32
"_sync:\n"
"  mrs x9, esr_el1\n  lsr x10, x9, #26\n  cmp x10, #0x15\n  b.ne 1f\n  tst x9, #0xffff\n  b.ne Ldone\n  eret\n"
"1: cmp x10, #0x24\n  b.eq 2f\n  cmp x10, #0x25\n  b.ne Lother\n"
"2: cbz x19, Lother\n  mrs x10, elr_el1\n  add x10, x10, #4\n  msr elr_el1, x10\n  eret\n"
"Ldone:\n  hvc #2\n  b .\n"
"Lother:\n  mrs x9, esr_el1\n  mrs x10, far_el1\n  mrs x11, elr_el1\n  mrs x12, spsr_el1\n  hvc #1\n  b .\n"
// The current resident entry from EL1t: generated code `br`s into resident text, which switches to SP_EL1 and later
// returns with ERET (gates.S returned_allocated ... enter_allocated). x14 = return PC.
"_el1_enter:\n  msr spsel, #1\n  msr elr_el1, x14\n  mov x9, #0x3c4\n  msr spsr_el1, x9\n  eret\n"
".balign 4096\n_t_start:\n"
// timing helpers: x0 = iterations; returns x0 = ticks, x1 = cntfrq
".macro T0\n  isb\n  mrs x20, cntvct_el0\n.endm\n"
".macro T1\n  isb\n  mrs x21, cntvct_el0\n  sub x0, x21, x20\n  mrs x1, cntfrq_el0\n  svc #1\n.endm\n"
"_t_empty:\n  T0\n1: subs x0, x0, #1\n  b.ne 1b\n  T1\n"
"_t_svc:\n  T0\n1: svc #0\n  subs x0, x0, #1\n  b.ne 1b\n  T1\n"
"_t_brenter:\n  T0\n1: adr x14, 2f\n  br x13\n2: subs x0, x0, #1\n  b.ne 1b\n  T1\n"
"_t_abort:\n  T0\n1: ldr x2, [x15]\n  subs x0, x0, #1\n  b.ne 1b\n  T1\n"
"_t_abst:\n  T0\n1: str x2, [x16]\n  subs x0, x0, #1\n  b.ne 1b\n  T1\n"
"_t_hvc:\n  T0\n1: hvc #3\n  subs x0, x0, #1\n  b.ne 1b\n  T1\n"
// 64 'blocks' of 64 bytes, each `add; b next` (a linked block edge), then the loop branch
"_t_chain:\n  T0\n  mov x2, #0\n3:\n"
"  .rept 64\n  add x2, x2, #1\n  b .+60\n  .skip 56\n  .endr\n"
"  subs x0, x0, #1\n  b.ne 3b\n  T1\n"
// memory: 16 load-add-store on an EL0-RW page per iteration
"_t_memt:\n  T0\n1:\n  .rept 16\n  ldtr x2, [x15, #64]\n  add x2, x2, #1\n  sttr x2, [x15, #64]\n  .endr\n  subs x0, x0, #1\n  b.ne 1b\n  T1\n"
"_t_mem:\n  T0\n1:\n  .rept 16\n  ldr x2, [x15, #64]\n  add x2, x2, #1\n  str x2, [x15, #64]\n  .endr\n  subs x0, x0, #1\n  b.ne 1b\n  T1\n"
// dispatch_fast (gates.S) without its counter: a ring of 16 blocks, each a computed exit through the frame
// (`str x5, [x28, #rip]; ldr x1, [x28, #DLT]; br x1`), L1 probe in an RO table (x27), hit -> br to the next block
"_t_disp:\n  T0\n  b _blk0\n"
"_disp_entry:\n  ldr x0, [x28, #0x18]\n  and x2, x0, #0xfff\n  add x1, x27, x2, lsl #4\n  ldp x2, x3, [x1]\n  sub x2, x2, x0\n  cbnz x2, Ldmiss\n  br x3\n"
"Ldmiss:\n  mov x0, #0xdead\n  svc #1\n"
".balign 64\n_blk0:\n  subs x22, x22, #1\n  b.eq 9f\n  mov x5, #1\n  str x5, [x28, #0x18]\n  ldr x1, [x28, #8]\n  br x1\n"
"9:\n  T1\n"
".irp i, 1,2,3,4,5,6,7,8,9,10,11,12,13,14,15\n  .balign 64\n_blk\\i:\n  mov x5, #((\\i+1)&15)\n  str x5, [x28, #0x18]\n  ldr x1, [x28, #8]\n  br x1\n.endr\n"
// trap checks: each runs one instruction at EL0 then reports x0 = 0x600d (it completed) through svc #1
".balign 64\n_c_start:\n"
".macro C name, insn:vararg\n  .balign 16\n_c_\\name:\n  \\insn\n  mov x0, #0x600d\n  svc #1\n.endm\n"
"C ld_el1only, ldr x2, [x15]\n"
"C st_el1only, str x2, [x15]\n"
"C st_el0ro, str x2, [x16]\n"
"C ld_hole, ldr x2, [x17]\n"
"C ld_outside, ldr x2, [x18]\n"
"C ld_el0data, ldr x2, [x23]\n"
"C br_el1text, br x13\n"
"C msr_ttbr0, msr ttbr0_el1, x2\n"
"C mrs_sctlr, mrs x2, sctlr_el1\n"
"C msr_daif, msr daifset, #15\n"
"C msr_spsel, msr spsel, #1\n"
"C tlbi, tlbi vmalle1\n"
"C tlbi_va, tlbi vae1, x2\n"
"C dc_civac, dc civac, x23\n"
"C dc_cvau, dc cvau, x23\n"
"C ic_ivau, ic ivau, x23\n"
"C dc_zva, dc zva, x23\n"
"C at_s1e1r, at s1e1r, x23\n"
"C hvc, hvc #0\n"
"C eret, eret\n"
"C wfi, wfi\n"
"C mrs_ctr, mrs x2, ctr_el0\n"
"C mrs_nzcv, mrs x2, nzcv\n"
"C msr_nzcv, msr nzcv, x2\n"
"C msr_fpcr, msr fpcr, x2\n"
"C msr_tpidr, msr tpidr_el0, x2\n"
"C cntvct, mrs x2, cntvct_el0\n"
"C brk, brk #1\n"
"C st_el0data, str x2, [x23]\n"
"C ldtr_el1only, ldtr x2, [x15]\n"
"C sttr_el0ro, sttr x2, [x16]\n"
"C ldtr_hole, ldtr x2, [x17]\n"
"C sttr_el0data, sttr x2, [x23]\n"
"_c_end:\n"
".balign 4096\n_g_end:\n");
#define SYM(n) extern const char n[];
SYM(g_start) SYM(g_end) SYM(t_start) SYM(el1_enter) SYM(t_empty) SYM(t_svc) SYM(t_brenter) SYM(t_abort) SYM(t_abst) SYM(t_hvc)
SYM(t_chain) SYM(t_mem) SYM(t_memt) SYM(t_disp)
#define CHECKS X(ld_el1only) X(st_el1only) X(st_el0ro) X(ld_hole) X(ld_outside) X(ld_el0data) X(br_el1text) X(msr_ttbr0) \
    X(mrs_sctlr) X(msr_daif) X(msr_spsel) X(tlbi) X(tlbi_va) X(dc_civac) X(dc_cvau) X(ic_ivau) X(dc_zva) X(at_s1e1r) X(hvc) \
    X(eret) X(wfi) X(mrs_ctr) X(mrs_nzcv) X(msr_nzcv) X(msr_fpcr) X(msr_tpidr) X(cntvct) X(brk) X(st_el0data) X(ldtr_el1only) X(sttr_el0ro) X(ldtr_hole) X(sttr_el0data)
#define X(n) SYM(c_##n)
CHECKS
#undef X
extern const char blk0[], blk1[], blk2[], blk3[], blk4[], blk5[], blk6[], blk7[], blk8[], blk9[], blk10[], blk11[],
    blk12[], blk13[], blk14[], blk15[], disp_entry[];

static uint8_t *ram;
static hv_vcpu_t v;
static hv_vcpu_exit_t *ex;
static void check(hv_return_t rc, const char *s) { if (rc != HV_SUCCESS) { fprintf(stderr, "FAIL %s rc=0x%x\n", s, rc); exit(2); } }
#define HV(x) check((x), #x)
static uint64_t ga(const char *s) { return BASE + (uint64_t)(s - g_start); }   // guest address of a blob symbol
static uint64_t reg(hv_reg_t r) { uint64_t x; HV(hv_vcpu_get_reg(v, r, &x)); return x; }
static uint64_t sys(hv_sys_reg_t r) { uint64_t x; HV(hv_vcpu_get_sys_reg(v, r, &x)); return x; }

struct res { int done; uint64_t x0, x1, esr, far, elr, spsr; unsigned hvc3; };
// Runs from `pc` at EL0t (el = 0) or EL1t (el = 1), DAIF masked, until `hvc #1` (trap report) or `hvc #2` (done).
static struct res run(uint64_t pc, int el, uint64_t x0, uint64_t skip) {
    struct res r = {0};
    HV(hv_vcpu_set_reg(v, HV_REG_PC, pc)); HV(hv_vcpu_set_reg(v, HV_REG_CPSR, el ? 0x3c4 : 0x3c0));
    HV(hv_vcpu_set_sys_reg(v, HV_SYS_REG_SP_EL0, PG(P_STACK0) + 0x1000)); HV(hv_vcpu_set_sys_reg(v, HV_SYS_REG_SP_EL1, PG(P_STACK1) + 0x1000));
    uint64_t x[32] = {0};
    x[0] = x0; x[13] = ga(el1_enter); x[15] = PG(P_EL1DATA); x[16] = PG(P_L1); x[17] = UINT64_C(0x0000800040000000);
    x[18] = UINT64_C(0x0001000000000000); x[19] = skip; x[22] = x0; x[23] = PG(P_DATA0); x[27] = PG(P_L1); x[28] = PG(P_FRAME) + 0x400;
    if (pc == ga(t_mem) || pc == ga(t_memt)) x[15] = PG(P_DATA0);
    if (pc == ga(t_abort) && !el && skip == 2) x[15] = PG(P_UNMAPPED);    // EL0 translation fault variant
    if (pc == ga(t_abort) && el) x[15] = PG(P_UNMAPPED);
    for (int i = 0; i < 29; i++) HV(hv_vcpu_set_reg(v, HV_REG_X0 + i, x[i]));
    for (;;) {
        HV(hv_vcpu_run(v));
        if (ex->reason != HV_EXIT_REASON_EXCEPTION || ex->exception.syndrome >> 26 != 0x16) {
            r.done = -1; r.esr = ex->exception.syndrome; r.far = ex->exception.virtual_address; r.elr = reg(HV_REG_PC);
            r.spsr = reg(HV_REG_CPSR); r.x0 = ex->reason;   // an exit to the host, not taken by the guest's EL1
            return r;
        }
        unsigned imm = ex->exception.syndrome & 0xffff;
        if (imm == 3) { r.hvc3++; continue; }
        r.done = imm == 2; r.x0 = reg(HV_REG_X0); r.x1 = reg(HV_REG_X1);
        r.esr = reg(HV_REG_X9); r.far = reg(HV_REG_X10); r.elr = reg(HV_REG_X11); r.spsr = reg(HV_REG_X12);
        return r;
    }
}

static int cmpd(const void *a, const void *b) { double x = *(const double *)a, y = *(const double *)b; return x < y ? -1 : x > y; }
// ns per iteration, median of reps runs, minus the empty loop's per-iteration time when `sub`
static double timed(const char *name, uint64_t pc, int el, uint64_t n, uint64_t skip, int reps, double per, double sub, unsigned *hvcs) {
    double d[64];
    for (int k = 0; k < reps; k++) {
        struct res r = run(pc, el, n, skip);
        if (r.done != 1 || r.x0 == 0xdead) { fprintf(stderr, "FAIL %s el%d: trap esr=%#llx far=%#llx elr=%#llx x0=%#llx\n", name, el,
            (unsigned long long)r.esr, (unsigned long long)r.far, (unsigned long long)r.elr, (unsigned long long)r.x0); exit(4); }
        d[k] = (double)r.x0 * 1e9 / (double)r.x1 / (double)n / per - sub;
        if (hvcs) *hvcs = r.hvc3;
    }
    qsort(d, reps, sizeof d[0], cmpd);
    printf("time %-22s EL%d n=%-9llu ns_per_op median=%.3f min=%.3f max=%.3f reps=%d\n", name, el, (unsigned long long)n, d[reps / 2], d[0], d[reps - 1], reps);
    return d[reps / 2];
}

int main(int argc, char **argv) {
    setbuf(stdout, NULL);
    int reps = argc > 1 ? atoi(argv[1]) : 7; double scale = argc > 2 ? atof(argv[2]) : 1.0;
    pthread_set_qos_class_self_np(QOS_CLASS_USER_INTERACTIVE, 0);
    size_t blob = g_end - g_start;
    if (blob > 16 * 0x1000) { fprintf(stderr, "blob too big %zu\n", blob); return 1; }
    ram = mmap(NULL, SIZE, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANON, -1, 0);
    memcpy(ram, g_start, blob);
    // tables at +0x200000 (not mapped in stage 1); L3 maps the first 2 MiB with 4 KiB pages
    uint64_t *l0 = (void *)(ram + 0x200000), *l1 = (void *)(ram + 0x201000), *l2 = (void *)(ram + 0x202000), *l3 = (void *)(ram + 0x203000);
    l0[0] = (BASE + 0x201000) | 3; l1[1] = (BASE + 0x202000) | 3; l2[0] = (BASE + 0x203000) | 3;
    const uint64_t PAGE = 3 | 1u << 10 | 3u << 8, AP_EL0RW = 1u << 6, AP_RO = 2u << 6, AP_BOTHRO = 3u << 6, PXN = 1ull << 53, UXN = 1ull << 54;
    (void)AP_RO;
    l3[0] = PG(0) | PAGE | UXN;                                            // vectors + EL1 handlers: EL1 RWX, EL0 none
    for (unsigned p = 1; p < blob / 0x1000; p++) l3[p] = PG(p) | PAGE | AP_BOTHRO;   // test code: RO, executable at EL0 and EL1
    l3[P_FRAME] = PG(P_FRAME) | PAGE | AP_EL0RW | PXN | UXN;               // the x28 frame: EL0 RW
    l3[P_L1] = PG(P_L1) | PAGE | AP_BOTHRO | PXN | UXN;                    // lookup table: RO at both ELs
    l3[P_EL1DATA] = PG(P_EL1DATA) | PAGE | PXN | UXN;                      // resident-private data: EL1 RW, EL0 none
    l3[P_UNMAPPED] = 0;
    l3[P_STACK0] = PG(P_STACK0) | PAGE | AP_EL0RW | PXN | UXN;
    l3[P_STACK1] = PG(P_STACK1) | PAGE | PXN | UXN;
    l3[P_DATA0] = PG(P_DATA0) | PAGE | AP_EL0RW | PXN | UXN;               // a guest data shadow: EL0 RW
    // L1 lookup: rip i -> blk i; frame: DLT pointer at +8 = dispatch
    const char *blks[16] = {blk0, blk1, blk2, blk3, blk4, blk5, blk6, blk7, blk8, blk9, blk10, blk11, blk12, blk13, blk14, blk15};
    uint64_t *lk = (void *)(ram + P_L1 * 0x1000);
    for (int i = 0; i < 16; i++) { lk[2 * i] = i; lk[2 * i + 1] = ga(blks[i]); }
    lk[0] = 0;   // rip 0 -> blk0 (entry 0 compares rip 0)
    *(uint64_t *)(ram + P_FRAME * 0x1000 + 0x400 + 8) = ga(disp_entry);
    __asm__ volatile("dsb sy" ::: "memory");

    HV(hv_vm_create(NULL)); HV(hv_vm_map(ram, BASE, SIZE, HV_MEMORY_READ | HV_MEMORY_WRITE | HV_MEMORY_EXEC));
    HV(hv_vcpu_create(&v, &ex, NULL));
    uint64_t tcr = 16 | (1ull << 8) | (1ull << 10) | (3ull << 12) | (1ull << 23) | (2ull << 32);
    HV(hv_vcpu_set_sys_reg(v, HV_SYS_REG_MAIR_EL1, 0xff)); HV(hv_vcpu_set_sys_reg(v, HV_SYS_REG_TCR_EL1, tcr));
    HV(hv_vcpu_set_sys_reg(v, HV_SYS_REG_TTBR0_EL1, BASE + 0x200000)); HV(hv_vcpu_set_sys_reg(v, HV_SYS_REG_SCTLR_EL1, 0x30d01985));
    HV(hv_vcpu_set_sys_reg(v, HV_SYS_REG_ACTLR_EL1, 2)); HV(hv_vcpu_set_sys_reg(v, HV_SYS_REG_CPACR_EL1, 3u << 20));
    HV(hv_vcpu_set_sys_reg(v, HV_SYS_REG_CNTKCTL_EL1, 3)); HV(hv_vcpu_set_sys_reg(v, HV_SYS_REG_VBAR_EL1, BASE));
    printf("blob=%zu bytes, sctlr=%#llx tcr=%#llx\n", blob, (unsigned long long)sys(HV_SYS_REG_SCTLR_EL1), (unsigned long long)tcr);

    // (b) trap checks, at EL0
#define X(n) { #n, c_##n },
    struct { const char *name; const char *at; } cs[] = { CHECKS };
#undef X
    for (size_t i = 0; i < sizeof cs / sizeof cs[0]; i++) {
        struct res r = run(ga(cs[i].at), 0, 0, 0);
        unsigned ec = r.esr >> 26;
        if (r.done < 0) printf("check %-12s EL0 exited to the HOST: reason=%llu syndrome=%#llx EC=%#x pc=%#llx cpsr=%#llx\n", cs[i].name,
            (unsigned long long)r.x0, (unsigned long long)r.esr, ec, (unsigned long long)r.elr, (unsigned long long)r.spsr);
        else if (r.done) printf("check %-12s EL0 completed (x0=%#llx)\n", cs[i].name, (unsigned long long)r.x0);
        else printf("check %-12s EL0 trapped to EL1: ESR=%#llx EC=%#x ISS=%#llx FAR=%#llx ELR-insn=%+lld SPSR.M=%#llx\n", cs[i].name,
            (unsigned long long)r.esr, ec, (unsigned long long)(r.esr & 0x1ffffff), (unsigned long long)r.far,
            (long long)(r.elr - ga(cs[i].at)), (unsigned long long)(r.spsr & 0x1f));
    }
    // same accesses at EL1t, for contrast (EL1 may read EL1-only data; the hole/outside VAs fault at both levels)
    const char *el1c[] = {c_ld_el1only, c_ld_hole, c_ld_outside, c_tlbi, c_dc_civac, c_ldtr_el1only, c_sttr_el0ro, c_ldtr_hole, c_sttr_el0data};
    const char *el1n[] = {"ld_el1only", "ld_hole", "ld_outside", "tlbi", "dc_civac", "ldtr_el1only", "sttr_el0ro", "ldtr_hole", "sttr_el0data"};
    for (int i = 0; i < 9; i++) {
        struct res r = run(ga(el1c[i]), 1, 0, 0);
        if (r.done < 0) printf("check %-12s EL1t exited to the HOST: syndrome=%#llx\n", el1n[i], (unsigned long long)r.esr);
        else if (r.done) printf("check %-12s EL1t completed\n", el1n[i]);
        else printf("check %-12s EL1t trapped: ESR=%#llx EC=%#x FAR=%#llx\n", el1n[i], (unsigned long long)r.esr, (unsigned)(r.esr >> 26), (unsigned long long)r.far);
    }

    // (a), (c) timings
    uint64_t N = (uint64_t)(2000000 * scale), H = (uint64_t)(20000 * scale);
    double e0 = timed("empty-loop", ga(t_empty), 0, 10 * N, 0, reps, 1, 0, 0);
    double e1 = timed("empty-loop", ga(t_empty), 1, 10 * N, 0, reps, 1, 0, 0);
    timed("svc+eret EL0->EL1->EL0", ga(t_svc), 0, N, 0, reps, 1, e0, 0);
    timed("svc+eret EL1t->EL1h", ga(t_svc), 1, N, 0, reps, 1, e1, 0);
    timed("br+spsel+eret (EL1t)", ga(t_brenter), 1, N, 0, reps, 1, e1, 0);
    timed("abort+eret EL0 perm ld", ga(t_abort), 0, N, 1, reps, 1, e0, 0);
    timed("abort+eret EL0 perm st", ga(t_abst), 0, N, 1, reps, 1, e0, 0);
    timed("abort+eret EL1t perm st", ga(t_abst), 1, N, 1, reps, 1, e1, 0);
    timed("abort+eret EL0 transl", ga(t_abort), 0, N, 2, reps, 1, e0, 0);
    timed("abort+eret EL1t transl", ga(t_abort), 1, N, 1, reps, 1, e1, 0);
    unsigned h = 0;
    timed("hvc host round trip", ga(t_hvc), 1, H, 0, reps, 1, e1, &h);
    printf("hvc exits seen in last rep: %u\n", h);
    // stage 2 (the precise-stop mechanism) seen from EL0 and EL1t: protect the granule of the EL0 data page read-only
    for (int el = 0; el < 2; el++) {
        hv_ipa_granule_t gr; HV(hv_vm_config_get_default_ipa_granule(&gr)); uint64_t G = gr == HV_IPA_GRANULE_4KB ? 0x1000 : 0x4000;
        uint64_t ipa = PG(P_DATA0) & ~(G - 1);
        HV(hv_vm_protect(ipa, G, HV_MEMORY_READ));
        struct res r = run(ga(c_st_el0data), el, 0, 0);
        HV(hv_vm_protect(ipa, G, HV_MEMORY_READ | HV_MEMORY_WRITE));
        printf("check stage2-protect %s store: %s syndrome=%#llx EC=%#llx IPA-page=%#llx pc-insn=%+lld\n", el ? "EL1t" : "EL0",
            r.done < 0 ? "exit to HOST" : "no host exit", (unsigned long long)r.esr, (unsigned long long)(r.esr >> 26),
            (unsigned long long)(ex->exception.physical_address & ~0xfffull), (long long)(r.elr - ga(c_st_el0data)));
    }
    timed("mem ldtr/sttr (EL1t)", ga(t_memt), 1, N / 4, 0, reps, 16, 0, 0);
    for (int el = 0; el < 2; el++) {
        timed("chain (per edge)", ga(t_chain), el, N / 4, 0, reps, 65, 0, 0);
        timed("mem (per ld+add+st)", ga(t_mem), el, N / 4, 0, reps, 16, 0, 0);
        timed("dispatch (per exit)", ga(t_disp), el, N, 0, reps, 16, 0, 0);
    }
    printf("cntfrq=%llu\n", (unsigned long long)run(ga(t_empty), 0, 1, 0).x1);
    HV(hv_vcpu_destroy(v)); HV(hv_vm_destroy());
    return 0;
}
