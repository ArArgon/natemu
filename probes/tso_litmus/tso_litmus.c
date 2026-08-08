// TSO litmus test: does ACTLR_EL1.EnTSO actually change the memory model inside a
// Hypervisor.framework VM, or is the bit merely stored?
//
// The original probe wrote ACTLR_EL1=2, read 2 back, and declared hardware TSO verified. That is
// Asahi's screening test (it rules out "hypervisor ignores the write"), but a hypervisor that
// saves/restores the register without applying it passes it too. This runs the actual experiment.
//
// Litmus: MP (message passing), the canonical test that separates ARM's weak model from TSO.
//
//     init: x = 0, y = 0
//     P0:  x = 1        P1:  r0 = y
//          y = 1             r1 = x
//
//   Under TSO (and under SC) the outcome r0==1 && r1==0 is FORBIDDEN: P0's stores cannot be
//   reordered, and P1's loads cannot be reordered.
//   Under ARM's weak model it is ALLOWED and observable in practice.
//
//   Note: the store-buffering (SB) litmus is the WRONG test here -- TSO permits SB, so it cannot
//   distinguish TSO from weak. MP is the discriminating one.
//
// No barriers appear between P0's two stores or between P1's two loads. Barriers are used only in
// the per-iteration synchronization, outside the measured window.
//
// The MMU is enabled with Normal Inner-Shareable Write-Back memory. This is essential: with the
// MMU off, guest memory is Device-nGnRnE, which is strongly ordered anyway and would make the
// test vacuously "pass" regardless of TSO.
//
// Build: ../build.sh tso_litmus/tso_litmus.c

#include <Hypervisor/Hypervisor.h>
#include <pthread.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>

#define RAM_IPA  0x40000000ULL
#define RAM_SIZE (2ULL << 20)

#define OFF_CODE   0x00000
#define OFF_VEC    0x02000
#define OFF_L0     0x04000
#define OFF_L1_ID  0x05000
#define OFF_L2_ID  0x06000
#define OFF_SHARED 0x20000

// Cache-line separated (Apple's line is 128 B).
#define S_X     0x000
#define S_Y     0x080
#define S_SEQ   0x100
#define S_DONE  0x180
#define S_COUNT 0x200

__asm__(
".section __TEXT,__text\n"
".p2align 2\n"
".globl _tso_start\n"
"_tso_start:\n"
// x0=MAIR x1=TCR x2=TTBR0 x3=VBAR x4=shared VA x5=iters x6=role x7=enable-TSO
"    msr  mair_el1, x0\n"
"    msr  tcr_el1,  x1\n"
"    msr  ttbr0_el1, x2\n"
"    msr  vbar_el1, x3\n"
"    cbz  x7, 1f\n"
"    mrs  x9, actlr_el1\n"
"    orr  x9, x9, #2\n"              // ACTLR_EL1.EnTSO
"    msr  actlr_el1, x9\n"
"1:  isb\n"
"    tlbi vmalle1\n"
"    dsb  nsh\n"
"    isb\n"
"    mrs  x9, sctlr_el1\n"
"    mov  x10, #0x1005\n"            // M | C | I
"    orr  x9, x9, x10\n"
"    msr  sctlr_el1, x9\n"
"    isb\n"
"    mrs  x28, actlr_el1\n"          // report what EnTSO actually reads as, MMU-on
"    mov  x2, #0\n"                  // iteration counter
"    mov  x16, #0\n"                 // violation counter (P1)
"    mov  x17, #-1\n"                // last observed Y
"    mov  x18, #0\n"                 // concurrency witness: times Y changed
"    cbnz x6, 20f\n"

// ---------------- P0: writer. Free-running, no synchronization at all. ----------------
// Publishes a monotonically increasing counter to X then to Y.
"10: add  x2, x2, #1\n"
"    str  x2, [x4, #0x000]\n"        // X = i    \\ measured window:
"    str  x2, [x4, #0x080]\n"        // Y = i    / no barrier between these two
"    cmp  x2, x5\n"
"    b.lo 10b\n"
"    hvc  #0\n"

// ---------------- P1: reader. Free-running, no synchronization at all. ----------------
// Reads Y then X. P0 writes X before Y and both only increase, so under TSO any observed
// Y value implies X has already reached at least that value: r1 >= r0 must always hold.
// r0 > r1 is therefore proof of reordering -- either P0's stores or P1's loads.
"20: ldr  x13, [x4, #0x080]\n"       // r0 = Y   \\ measured window:
"    ldr  x14, [x4, #0x000]\n"       // r1 = X   / no barrier between these two
"    cmp  x13, x14\n"
"    b.ls 21f\n"                     // r0 <= r1 -> consistent with TSO
"    add  x16, x16, #1\n"            // r0 >  r1 -> WEAK-MEMORY OUTCOME
"21: cmp  x13, x17\n"
"    b.eq 22f\n"
"    add  x18, x18, #1\n"            // P0 advanced while P1 was looping
"    mov  x17, x13\n"
"22: add  x2, x2, #1\n"
"    cmp  x2, x5\n"
"    b.lo 20b\n"
"    str  x16, [x4, #0x200]\n"
"    str  x18, [x4, #0x280]\n"
"    hvc  #0\n"
".globl _tso_end\n"
"_tso_end:\n"

".globl _tso_vec_start\n"
"_tso_vec_start:\n"
"    mrs  x20, esr_el1\n"
"    mrs  x21, far_el1\n"
"    hvc  #2\n"
".globl _tso_vec_end\n"
"_tso_vec_end:\n"
);

extern const uint8_t tso_start[], tso_end[];
extern const uint8_t tso_vec_start[], tso_vec_end[];

#define D_TABLE (3ULL << 0)
#define D_BLOCK (1ULL << 0)
#define D_AF    (1ULL << 10)
#define D_SH_IS (3ULL << 8)
#define TCR_VAL (16ULL | (1ULL<<8) | (1ULL<<10) | (3ULL<<12) \
                | (16ULL<<16) | (1ULL<<23) | (2ULL<<30) | (2ULL<<32))
#define MAIR_VAL 0xFFULL

#define HV(e) do { hv_return_t _r = (e); if (_r != HV_SUCCESS) { \
    fprintf(stderr, "  %s -> 0x%x at %d\n", #e, _r, __LINE__); exit(2);} } while (0)

static uint8_t *mem;
static uint64_t g_iters;
static int g_tso;
static uint64_t g_witness;

struct arg { int role; uint64_t actlr; uint64_t hvc_imm; uint64_t esr; };

static void *vcpu_thread(void *p) {
    struct arg *a = p;
    hv_vcpu_t vcpu; hv_vcpu_exit_t *vexit;
    HV(hv_vcpu_create(&vcpu, &vexit, NULL));
    HV(hv_vcpu_set_reg(vcpu, HV_REG_PC, RAM_IPA + OFF_CODE));
    HV(hv_vcpu_set_reg(vcpu, HV_REG_CPSR, 0x3c5));           // EL1h
    HV(hv_vcpu_set_reg(vcpu, HV_REG_X0, MAIR_VAL));
    HV(hv_vcpu_set_reg(vcpu, HV_REG_X1, TCR_VAL));
    HV(hv_vcpu_set_reg(vcpu, HV_REG_X2, RAM_IPA + OFF_L0));
    HV(hv_vcpu_set_reg(vcpu, HV_REG_X3, RAM_IPA + OFF_VEC));
    HV(hv_vcpu_set_reg(vcpu, HV_REG_X4, RAM_IPA + OFF_SHARED));
    HV(hv_vcpu_set_reg(vcpu, HV_REG_X5, g_iters));
    HV(hv_vcpu_set_reg(vcpu, HV_REG_X6, (uint64_t)a->role));
    HV(hv_vcpu_set_reg(vcpu, HV_REG_X7, (uint64_t)g_tso));

    HV(hv_vcpu_run(vcpu));
    uint64_t sy = vexit->exception.syndrome;
    a->hvc_imm = ((sy >> 26) & 0x3f) == 0x16 ? (sy & 0xffff) : 0xdead;
    a->esr = sy;
    HV(hv_vcpu_get_reg(vcpu, HV_REG_X28, &a->actlr));
    hv_vcpu_destroy(vcpu);
    return NULL;
}

// Returns weak-memory outcome count, or -1 on guest error.
static long long run_round(int tso, uint64_t iters, uint64_t *actlr_seen) {
    g_iters = iters; g_tso = tso;
    memset(mem + OFF_SHARED, 0, 0x400);

    struct arg a0 = { .role = 0 }, a1 = { .role = 1 };
    pthread_t t0, t1;
    pthread_create(&t0, NULL, vcpu_thread, &a0);
    pthread_create(&t1, NULL, vcpu_thread, &a1);
    pthread_join(t0, NULL); pthread_join(t1, NULL);

    *actlr_seen = a0.actlr;
    if (a0.hvc_imm != 0 || a1.hvc_imm != 0) {
        printf("    guest error: P0 hvc#%llu (ESR 0x%llx), P1 hvc#%llu (ESR 0x%llx)\n",
               (unsigned long long)a0.hvc_imm, (unsigned long long)a0.esr,
               (unsigned long long)a1.hvc_imm, (unsigned long long)a1.esr);
        return -1;
    }
    g_witness = *(uint64_t *)(mem + OFF_SHARED + 0x280);
    return (long long)*(uint64_t *)(mem + OFF_SHARED + S_COUNT);
}

int main(int argc, char **argv) {
    uint64_t iters = argc > 1 ? strtoull(argv[1], NULL, 0) : 2000000;

    printf("=== MP litmus: ACTLR_EL1.EnTSO effect on the guest memory model ===\n");
    printf("  iterations per round: %llu\n", (unsigned long long)iters);
    printf("  forbidden-under-TSO outcome: r0(Y) > r1(X)  [P0 stores X then Y, both increasing]\n\n");

    HV(hv_vm_create(NULL));
    mem = mmap(NULL, RAM_SIZE, PROT_READ | PROT_WRITE, MAP_ANON | MAP_PRIVATE, -1, 0);
    if (mem == MAP_FAILED) { perror("mmap"); return 2; }
    memset(mem, 0, RAM_SIZE);
    HV(hv_vm_map(mem, RAM_IPA, RAM_SIZE, HV_MEMORY_READ | HV_MEMORY_WRITE | HV_MEMORY_EXEC));

    memcpy(mem + OFF_CODE, tso_start, (size_t)(tso_end - tso_start));
    size_t vlen = (size_t)(tso_vec_end - tso_vec_start);
    for (int i = 0; i < 16; i++) memcpy(mem + OFF_VEC + i * 0x80, tso_vec_start, vlen);

    uint64_t *l0 = (uint64_t *)(mem + OFF_L0);
    uint64_t *l1 = (uint64_t *)(mem + OFF_L1_ID), *l2 = (uint64_t *)(mem + OFF_L2_ID);
    l0[0] = (RAM_IPA + OFF_L1_ID) | D_TABLE;
    l1[1] = (RAM_IPA + OFF_L2_ID) | D_TABLE;
    l2[0] = RAM_IPA | D_BLOCK | D_AF | D_SH_IS;   // Normal WB Inner-Shareable, RW, executable

    uint64_t actlr_off = 0, actlr_on = 0;
    printf("  round 1: EnTSO = 0 (baseline weak model)\n");
    long long weak = run_round(0, iters, &actlr_off);
    printf("    ACTLR_EL1 read back in-guest = 0x%llx (EnTSO=%llu)\n",
           (unsigned long long)actlr_off, (unsigned long long)((actlr_off >> 1) & 1));
    printf("    weak-memory outcomes: %lld / %llu\n", weak, (unsigned long long)iters);
    uint64_t w_off = g_witness;
    printf("    concurrency witness (Y changed under P1): %llu\n\n", (unsigned long long)w_off);

    printf("  round 2: EnTSO = 1\n");
    long long tso = run_round(1, iters, &actlr_on);
    printf("    ACTLR_EL1 read back in-guest = 0x%llx (EnTSO=%llu)\n",
           (unsigned long long)actlr_on, (unsigned long long)((actlr_on >> 1) & 1));
    printf("    weak-memory outcomes: %lld / %llu\n", tso, (unsigned long long)iters);
    uint64_t w_on = g_witness;
    printf("    concurrency witness (Y changed under P1): %llu\n\n", (unsigned long long)w_on);
    if (w_on < 1000) printf("    WARNING: threads barely overlapped in round 2 -- result unsafe\n");

    hv_vm_destroy();

    if (weak < 0 || tso < 0) { printf("  VERDICT: INCONCLUSIVE (guest error)\n"); return 2; }
    if (((actlr_on >> 1) & 1) != 1) {
        printf("  VERDICT: EnTSO could not be set from guest EL1.\n"); return 1;
    }
    if (weak == 0) {
        printf("  VERDICT: INCONCLUSIVE -- the weak baseline never produced the reordering,\n"
               "           so round 2 proves nothing. Raise iterations, or this core pairing\n"
               "           does not exhibit MP reordering at this rate.\n");
        return 1;
    }
    if (tso == 0) {
        printf("  VERDICT: HARDWARE TSO IS REAL. The reordering occurs %lld times with EnTSO=0\n"
               "           and never with EnTSO=1. The bit changes the memory model, it is not\n"
               "           merely stored.\n", weak);
        return 0;
    }
    printf("  VERDICT: EnTSO=1 did NOT eliminate the reordering (%lld -> %lld).\n"
           "           The bit reads back as set but does not enforce TSO.\n", weak, tso);
    return 1;
}
