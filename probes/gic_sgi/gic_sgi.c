// Can one vCPU send an SGI to another vCPU's EL1 vector WITHOUT a host exit?
//
// doc 02 section 3.1 established that hv_gic_create() works on Apple silicon but left
// "SGIs via ICC_SGI1R_EL1" explicitly Unverified. This probe answers it:
//
//   1. create a GICv3 with hv_gic_create()
//   2. give each vCPU an MPIDR_EL1 affinity, map the distributor + its own redistributor
//      frames into guest stage-1 as Device memory
//   3. from guest EL1, enable ICC_SRE_EL1, wake the redistributor, enable SGI INTIDs,
//      set ICC_PMR_EL1 / ICC_IGRPEN1_EL1, unmask PSTATE.I
//   4. vCPU0 writes ICC_SGI1R_EL1 targeting vCPU1
//   5. vCPU1's EL1 IRQ vector stamps CNTVCT_EL0 and acknowledges
//
// The host counts every hv_vcpu_run return for both vCPUs, so "no host exit" is a measured
// claim, not an assumption.
//
// Build: ../build.sh gic_sgi/gic_sgi.c

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

#define RAM_IPA   0x40000000ULL
#define RAM_SIZE  0x00400000ULL           // 4 MiB

#define OFF_CODE   0x00000ULL
#define OFF_VEC    0x02000ULL
#define OFF_L0     0x04000ULL
#define OFF_L1     0x05000ULL
#define OFF_L2RAM  0x06000ULL             // covers 0x40000000..0x7fffffff
#define OFF_L2GIC  0x07000ULL             // covers 0x00000000..0x3fffffff
#define OFF_P0     0x10000ULL
#define OFF_P1     0x12000ULL
#define OFF_SHARED 0x14000ULL
#define OFF_RES    0x20000ULL

// GIC placement (IPA), well away from RAM
#define GICD_BASE  0x10000000ULL
#define GICR_BASE  0x11000000ULL
#define MSI_BASE   0x14000000ULL

#define D_TABLE  3ULL
#define D_BLOCK  1ULL
#define D_AF     (1ULL << 10)
#define D_SH_IS  (3ULL << 8)
#define D_UXN    (1ULL << 54)
#define D_PXN    (1ULL << 53)
#define ATTR_DEV (1ULL << 2)              // MAIR index 1

// MAIR: idx0 = Normal WB inner/outer, idx1 = Device-nGnRnE
#define MAIR_VAL (0xFFULL | (0x00ULL << 8))
#define TCR_VAL  (16ULL | (1ULL<<8) | (1ULL<<10) | (3ULL<<12) \
                 | (16ULL<<16) | (1ULL<<23) | (2ULL<<30) | (2ULL<<32))

#define HV(e) do { hv_return_t _r = (e); if (_r != HV_SUCCESS) { \
    fprintf(stderr, "  %s -> 0x%x at %d\n", #e, _r, __LINE__); exit(2);} } while (0)
#define TRY(e) do { hv_return_t _r = (e); \
    printf("  %-56s -> 0x%x %s\n", #e, _r, _r == HV_SUCCESS ? "OK" : "FAIL"); } while (0)

__asm__(
".section __TEXT,__text\n"
".p2align 2\n"
".globl _g_start\n"
"_g_start:\n"
// x0=MAIR x1=TCR x2=TTBR0 x3=VBAR x4=param VA x5=role
"    msr  mair_el1, x0\n"
"    msr  tcr_el1,  x1\n"
"    msr  ttbr0_el1, x2\n"
"    msr  vbar_el1, x3\n"
"    isb\n"
"    tlbi vmalle1\n"
"    dsb  nsh\n"
"    isb\n"
"    mrs  x9, sctlr_el1\n"
"    mov  x10, #0x1005\n"
"    orr  x9, x9, x10\n"
"    msr  sctlr_el1, x9\n"
"    isb\n"
"    ic   iallu\n"
"    dsb  nsh\n"
"    isb\n"
"    mov  x28, x4\n"
"    mov  x27, x5\n"
"    ldr  x20, [x28, #0x000]\n"        // GICD VA
"    ldr  x21, [x28, #0x008]\n"        // my GICR frame VA (RD_base)
"    ldr  x22, [x28, #0x010]\n"        // shared block VA
"    ldr  x23, [x28, #0x018]\n"        // iterations
"    ldr  x24, [x28, #0x020]\n"        // SGI1R value (sender only)
"    ldr  x11, [x28, #0x028]\n"        // result cursor VA
"    mov  x19, #0\n"                   // IRQ count
// --- probe 1: is ICC_SRE_EL1 reachable from guest EL1? handler sets x26=1 on trap ---
"    mov  x26, #0\n"
"    mrs  x0, S3_0_C12_C12_5\n"        // ICC_SRE_EL1
"    orr  x0, x0, #1\n"                // SRE
"    msr  S3_0_C12_C12_5, x0\n"
"    isb\n"
"    mrs  x0, S3_0_C12_C12_5\n"
"    str  x0, [x28, #0xc00]\n"
"    str  x26, [x28, #0xc08]\n"        // 1 => the access trapped to EL2
// --- redistributor: clear GICR_WAKER.ProcessorSleep, wait ChildrenAsleep ---
"    ldr  w0, [x21, #0x14]\n"
"    str  w0, [x28, #0xc10]\n"
"    and  w0, w0, #0xfffffffd\n"
"    str  w0, [x21, #0x14]\n"
"    dsb  sy\n"
"    mov  x1, #0x100000\n"
"1:  ldr  w0, [x21, #0x14]\n"
"    tbz  w0, #2, 2f\n"
"    subs x1, x1, #1\n"
"    b.ne 1b\n"
"2:  str  w0, [x28, #0xc18]\n"
// --- SGI/PPI frame is RD_base + 64 KiB ---
"    add  x2, x21, #0x10000\n"
"    mov  w0, #-1\n"
"    str  w0, [x2, #0x080]\n"          // GICR_IGROUPR0  = all Group1
"    str  wzr, [x2, #0x0d00]\n"        // GICR_IGRPMODR0 = 0 -> Group1NS
"    str  wzr, [x2, #0x400]\n"         // GICR_IPRIORITYR0..3 = 0 (highest)
"    str  wzr, [x2, #0x404]\n"
"    str  wzr, [x2, #0x408]\n"
"    str  wzr, [x2, #0x40c]\n"
"    mov  w0, #0xffff\n"
"    str  w0, [x2, #0x100]\n"          // GICR_ISENABLER0 = enable INTID 0..15
"    dsb  sy\n"
"    ldr  w0, [x2, #0x100]\n"
"    str  w0, [x28, #0xc20]\n"
// --- distributor: make sure Group1NS is enabled (framework may already have done it) ---
"    ldr  w0, [x20, #0x000]\n"         // GICD_CTLR
"    str  w0, [x28, #0xc28]\n"
"    mov  w1, #0x12\n"                 // EnableGrp1NS | ARE_NS
"    orr  w0, w0, w1\n"
"    str  w0, [x20, #0x000]\n"
"    dsb  sy\n"
"    ldr  w0, [x20, #0x000]\n"
"    str  w0, [x28, #0xc30]\n"
// --- CPU interface ---
"    mov  x0, #0xf0\n"
"    msr  S3_0_C4_C6_0, x0\n"          // ICC_PMR_EL1
"    msr  S3_0_C12_C12_3, xzr\n"       // ICC_BPR1_EL1 = 0
"    mov  x0, #1\n"
"    msr  S3_0_C12_C12_7, x0\n"        // ICC_IGRPEN1_EL1 = 1
"    isb\n"
"    mrs  x0, S3_0_C12_C12_7\n"
"    str  x0, [x28, #0xc38]\n"
"    msr  daifclr, #2\n"               // unmask PSTATE.I
"    isb\n"
"    add  x10, x22, #0x100\n"          // ack counter address (used by the IRQ handler too)
"    add  x9,  x22, #0x180\n"          // stop flag address
// --- rendezvous on the shared block: [x22] = arrivals ---
"    mov  w0, #1\n"
"    ldaddal w0, w1, [x22]\n"
"    mov  x1, #0x20000000\n"
"1:  ldar w0, [x22]\n"
"    cmp  w0, #2\n"
"    b.hs 2f\n"
"    subs x1, x1, #1\n"
"    b.ne 1b\n"
"    hvc  #10\n"
"2:  cbnz x27, Lrecv\n"
// --- sender ---
"Lsend:\n"
// how much of the latency is the SGI1R write itself? aim it at a non-existent Aff1 so
// nothing is ever delivered, and time 4096 back-to-back writes.
"    mov  x0, #0xff0000\n"
"    orr  x0, x0, #0x3000000\n"        // INTID 3, Aff1 = 0xff, TargetList = 0
"    isb\n"
"    mrs  x16, cntvct_el0\n"
"    mov  x1, #4096\n"
"5:  msr  S3_0_C12_C11_5, x0\n"
"    subs x1, x1, #1\n"
"    b.ne 5b\n"
"    isb\n"
"    mrs  x17, cntvct_el0\n"
"    sub  x17, x17, x16\n"
"    str  x17, [x28, #0xc78]\n"
"    mov  x14, x23\n"
"    mov  x15, #0\n"
"    mov  x17, #0\n"                   // resend counter
"    mov  x13, #0\n"                   // give-up counter
"Lsend1:\n"
"    add  x15, x15, #1\n"
"    mov  x12, #64\n"                  // at most 64 attempts per SGI
"Lsend2:\n"
"    isb\n"
"    mrs  x16, cntvct_el0\n"
"    str  x16, [x22, #0x080]\n"        // publish t0
"    dsb  ish\n"
"    msr  S3_0_C12_C11_5, x24\n"       // ICC_SGI1R_EL1
"    isb\n"
"    mov  x1, #0x00002000\n"           // ~ tens of microseconds
"3:  ldar x0, [x10]\n"                 // wait for the receiver's ack counter
"    cmp  x0, x15\n"
"    b.hs 4f\n"
"    subs x1, x1, #1\n"
"    b.ne 3b\n"
"    add  x17, x17, #1\n"              // timed out: resend the same SGI
"    subs x12, x12, #1\n"
"    b.ne Lsend2\n"
"    add  x13, x13, #1\n"              // gave up on this one; force the counters level
"    stlr x15, [x10]\n"
"4:  mov  x0, #65\n"
"    sub  x0, x0, x12\n"
"    str  x0, [x11], #8\n"             // attempts this SGI needed
"    str  x17, [x28, #0xc68]\n"
"    str  x13, [x28, #0xc70]\n"
"    subs x14, x14, #1\n"
"    b.ne Lsend1\n"
"    str  x17, [x28, #0xc68]\n"
"    str  x13, [x28, #0xc70]\n"
"    mov  x0, #1\n"
"    stlr x0, [x9]\n"                  // tell the receiver we are finished
"    hvc  #0\n"
// --- receiver: just spin until the sender says stop; the IRQ handler does the work ---
"Lrecv:\n"
"    mov  x1, #0x40000000\n"
"1:  ldar x0, [x9]\n"
"    cbnz x0, 2f\n"
"    subs x1, x1, #1\n"
"    b.ne 1b\n"
"2:  str  x19, [x28, #0xc40]\n"
"    hvc  #0\n"
".globl _g_end\n"
"_g_end:\n"

// IRQ handler: stamp CNTVCT, record the delta against the sender's t0, ack.
".globl _g_irq\n"
"_g_irq:\n"
"    mrs  x25, S3_0_C12_C12_0\n"       // ICC_IAR1_EL1
"    mrs  x26, cntvct_el0\n"
"    ldr  x0, [x22, #0x080]\n"
"    sub  x0, x26, x0\n"
"    str  x0, [x11], #8\n"
"    msr  S3_0_C12_C12_1, x25\n"       // ICC_EOIR1_EL1
"    add  x19, x19, #1\n"
"    str  x25, [x28, #0xc48]\n"
"    dsb  ish\n"
"    stlr x19, [x10]\n"
"    eret\n"
".globl _g_irq_end\n"
"_g_irq_end:\n"

// synchronous handler: used only to survive an EL2 sysreg trap during setup
".globl _g_sync\n"
"_g_sync:\n"
"    mrs  x25, esr_el1\n"
"    mrs  x26, far_el1\n"
"    mrs  x0, elr_el1\n"
"    str  x25, [x28, #0xc50]\n"
"    str  x26, [x28, #0xc58]\n"
"    str  x0,  [x28, #0xc60]\n"
"    hvc  #9\n"
".globl _g_sync_end\n"
"_g_sync_end:\n"
);

extern const uint8_t g_start[], g_end[], g_irq[], g_irq_end[], g_sync[], g_sync_end[];

static uint8_t *mem;
static uint64_t g_alloc = OFF_L0;

static uint64_t *tab(uint64_t off) { return (uint64_t *)(mem + off); }

static void map_block2(uint64_t l2off, uint64_t va, uint64_t pa, uint64_t attrs) {
    tab(l2off)[(va >> 21) & 0x1ff] = (pa & ~0x1fffffULL) | D_BLOCK | attrs;
}

typedef struct {
    int role;
    hv_vcpu_t vcpu;
    hv_vcpu_exit_t *vexit;
    uint64_t param_ipa, mpidr;
    volatile int ready;
    int exits, rc;
    uint64_t hvc;
} ctx_t;
static ctx_t C[2];
static volatile int g_started;

static void *vcpu_thread(void *p) {
    ctx_t *c = p;
    pthread_set_qos_class_self_np(QOS_CLASS_USER_INTERACTIVE, 0);
    HV(hv_vcpu_create(&c->vcpu, &c->vexit, NULL));
    HV(hv_vcpu_set_sys_reg(c->vcpu, HV_SYS_REG_MPIDR_EL1, c->mpidr));
    c->ready = 1;
    while (!g_started) usleep(200);
    HV(hv_vcpu_set_reg(c->vcpu, HV_REG_PC,   RAM_IPA + OFF_CODE));
    HV(hv_vcpu_set_reg(c->vcpu, HV_REG_CPSR, 0x3c5));
    HV(hv_vcpu_set_reg(c->vcpu, HV_REG_X0,   MAIR_VAL));
    HV(hv_vcpu_set_reg(c->vcpu, HV_REG_X1,   TCR_VAL));
    HV(hv_vcpu_set_reg(c->vcpu, HV_REG_X2,   RAM_IPA + OFF_L0));
    HV(hv_vcpu_set_reg(c->vcpu, HV_REG_X3,   RAM_IPA + OFF_VEC));
    HV(hv_vcpu_set_reg(c->vcpu, HV_REG_X4,   c->param_ipa));
    HV(hv_vcpu_set_reg(c->vcpu, HV_REG_X5,   (uint64_t)c->role));
    for (;;) {
        hv_return_t r = hv_vcpu_run(c->vcpu);
        c->exits++;
        if (r != HV_SUCCESS) { printf("  vcpu%d run -> 0x%x\n", c->role, r); c->rc = 1; break; }
        if (c->vexit->reason == HV_EXIT_REASON_VTIMER_ACTIVATED) {
            hv_vcpu_set_vtimer_mask(c->vcpu, true); continue;
        }
        if (c->vexit->reason == HV_EXIT_REASON_CANCELED) {
            printf("  !! vcpu%d cancelled by watchdog\n", c->role); c->rc = 1; break;
        }
        if (c->vexit->reason != HV_EXIT_REASON_EXCEPTION) {
            printf("  !! vcpu%d exit reason %u\n", c->role, c->vexit->reason); c->rc = 1; break;
        }
        uint64_t sy = c->vexit->exception.syndrome, ec = (sy >> 26) & 0x3f;
        if (ec == 0x16) { c->hvc = sy & 0xffff; if (c->hvc) c->rc = 1; break; }
        if (ec == 0x18) {                  // sysreg trapped to EL2 -> skip it and flag it
            uint64_t pc = 0; hv_vcpu_get_reg(c->vcpu, HV_REG_PC, &pc);
            hv_vcpu_set_reg(c->vcpu, HV_REG_PC, pc + 4);
            hv_vcpu_set_reg(c->vcpu, HV_REG_X26, 1);
            printf("  !! vcpu%d EL2 SYSREG TRAP ESR=0x%llx PC=0x%llx\n",
                   c->role, (unsigned long long)sy, (unsigned long long)pc);
            continue;
        }
        uint64_t pc = 0; hv_vcpu_get_reg(c->vcpu, HV_REG_PC, &pc);
        printf("  !! vcpu%d unexpected exit ESR=0x%llx EC=0x%llx PC=0x%llx\n",
               c->role, (unsigned long long)sy, (unsigned long long)ec, (unsigned long long)pc);
        c->rc = 1; break;
    }
    hv_vcpu_destroy(c->vcpu);
    return NULL;
}

static volatile int wd_done;
static void *watchdog(void *p) {
    (void)p;
    for (int i = 0; i < 40 && !wd_done; i++) usleep(250000);
    if (!wd_done) {
        printf("  !! watchdog firing\n");
        hv_vcpu_t v[2] = { C[0].vcpu, C[1].vcpu };
        hv_vcpus_exit(v, 2);
    }
    return NULL;
}

static int dcmp(const void *a, const void *b) {
    double x = *(const double *)a, y = *(const double *)b;
    return x < y ? -1 : x > y ? 1 : 0;
}

int main(int argc, char **argv) {
    uint64_t iters = argc > 1 ? strtoull(argv[1], NULL, 0) : 20000;
    printf("=== GICv3 SGI between vCPUs, without a host exit? ===\n");

    HV(hv_vm_create(NULL));

    size_t dsz = 0, rrsz = 0, rsz = 0, msz = 0, dal = 0, ral = 0, mal = 0;
    hv_gic_get_distributor_size(&dsz);
    hv_gic_get_redistributor_region_size(&rrsz);
    hv_gic_get_redistributor_size(&rsz);
    hv_gic_get_msi_region_size(&msz);
    hv_gic_get_distributor_base_alignment(&dal);
    hv_gic_get_redistributor_base_alignment(&ral);
    hv_gic_get_msi_region_base_alignment(&mal);
    printf("  distributor %zu B (align %zu), redistributor region %zu B, per-vCPU %zu B (align %zu),"
           " msi %zu B (align %zu)\n", dsz, dal, rrsz, rsz, ral, msz, mal);

    hv_gic_config_t cfg = hv_gic_config_create();
    TRY(hv_gic_config_set_distributor_base(cfg, GICD_BASE));
    TRY(hv_gic_config_set_redistributor_base(cfg, GICR_BASE));
    TRY(hv_gic_config_set_msi_region_base(cfg, MSI_BASE));
    TRY(hv_gic_config_set_msi_interrupt_range(cfg, 800, 64));
    hv_return_t gr = hv_gic_create(cfg);
    printf("  hv_gic_create -> 0x%x %s\n", gr, gr == HV_SUCCESS ? "OK" : "FAIL");
    if (gr != HV_SUCCESS) { printf("  VERDICT: no framework GIC on this host.\n"); return 1; }

    mem = mmap(NULL, RAM_SIZE, PROT_READ | PROT_WRITE, MAP_ANON | MAP_PRIVATE, -1, 0);
    if (mem == MAP_FAILED) { perror("mmap"); return 2; }
    memset(mem, 0, RAM_SIZE);
    HV(hv_vm_map(mem, RAM_IPA, RAM_SIZE, HV_MEMORY_READ | HV_MEMORY_WRITE | HV_MEMORY_EXEC));

    memcpy(mem + OFF_CODE, g_start, (size_t)(g_end - g_start));
    // vectors: everything -> synchronous panic handler, except IRQ (offset 0x280)
    {
        size_t sl = (size_t)(g_sync_end - g_sync), il = (size_t)(g_irq_end - g_irq);
        for (int i = 0; i < 16; i++) memcpy(mem + OFF_VEC + i * 0x80, g_sync, sl);
        memcpy(mem + OFF_VEC + 0x280, g_irq, il);
    }
    // page tables: L0[0] -> L1; L1[0] -> L2GIC (0..1 GiB, device); L1[1] -> L2RAM (1..2 GiB)
    (void)g_alloc;
    tab(OFF_L0)[0] = (RAM_IPA + OFF_L1) | D_TABLE;
    tab(OFF_L1)[0] = (RAM_IPA + OFF_L2GIC) | D_TABLE;
    tab(OFF_L1)[1] = (RAM_IPA + OFF_L2RAM) | D_TABLE;
    for (uint64_t o = 0; o < 0x40000000ULL; o += 0x200000ULL) {
        uint64_t va = o;
        if (va >= GICD_BASE - 0x200000 && va < MSI_BASE + 0x400000)
            map_block2(OFF_L2GIC, va, va, D_AF | ATTR_DEV | D_UXN | D_PXN);
    }
    map_block2(OFF_L2RAM, RAM_IPA, RAM_IPA, D_AF | D_SH_IS);
    for (uint64_t o = 0x200000; o < RAM_SIZE; o += 0x200000ULL)
        map_block2(OFF_L2RAM, RAM_IPA + o, RAM_IPA + o, D_AF | D_SH_IS);

    C[0].role = 0; C[0].mpidr = 0x80000000ULL | 0;      // Aff0 = 0
    C[1].role = 1; C[1].mpidr = 0x80000000ULL | 1;      // Aff0 = 1
    C[0].param_ipa = RAM_IPA + OFF_P0;
    C[1].param_ipa = RAM_IPA + OFF_P1;

    pthread_t th[2];
    pthread_create(&th[0], NULL, vcpu_thread, &C[0]);
    pthread_create(&th[1], NULL, vcpu_thread, &C[1]);
    while (!C[0].ready || !C[1].ready) usleep(500);

    hv_ipa_t rb[2] = {0,0};
    for (int i = 0; i < 2; i++) {
        hv_return_t r = hv_gic_get_redistributor_base(C[i].vcpu, &rb[i]);
        printf("  vcpu%d redistributor base -> 0x%x  IPA 0x%llx\n", i, r,
               (unsigned long long)rb[i]);
        if (r != HV_SUCCESS) { printf("  VERDICT: cannot place redistributors.\n"); return 1; }
    }
    {
        uint64_t v = 0;
        if (hv_gic_get_distributor_reg(HV_GIC_DISTRIBUTOR_REG_GICD_CTLR, &v) == HV_SUCCESS)
            printf("  GICD_CTLR (host view, before guest setup) = 0x%llx\n", (unsigned long long)v);
        if (hv_gic_get_distributor_reg(HV_GIC_DISTRIBUTOR_REG_GICD_TYPER, &v) == HV_SUCCESS)
            printf("  GICD_TYPER = 0x%llx\n", (unsigned long long)v);
    }

    // SGI1R: INTID 3 in [27:24], Aff1 [23:16] = 0, TargetList [15:0] = bit(Aff0 of target)
    uint64_t sgi1r = (3ULL << 24) | (1ULL << 1);

    uint64_t *p0 = (uint64_t *)(mem + OFF_P0), *p1 = (uint64_t *)(mem + OFF_P1);
    p0[0] = GICD_BASE; p0[1] = rb[0]; p0[2] = RAM_IPA + OFF_SHARED; p0[3] = iters;
    p0[4] = sgi1r;     p0[5] = RAM_IPA + OFF_RES;
    p1[0] = GICD_BASE; p1[1] = rb[1]; p1[2] = RAM_IPA + OFF_SHARED; p1[3] = iters;
    p1[4] = 0;         p1[5] = RAM_IPA + OFF_RES + 0x100000;

    pthread_t wt; wd_done = 0; pthread_create(&wt, NULL, watchdog, NULL);
    g_started = 1;
    pthread_join(th[0], NULL); pthread_join(th[1], NULL);
    wd_done = 1; pthread_join(wt, NULL);

    uint64_t *a0 = (uint64_t *)(mem + OFF_P0 + 0xc00), *a1 = (uint64_t *)(mem + OFF_P1 + 0xc00);
    printf("\n  guest EL1 GIC setup, as read back inside the VM:\n");
    for (int i = 0; i < 2; i++) {
        uint64_t *a = i ? a1 : a0;
        printf("   vcpu%d ICC_SRE_EL1=0x%llx trapped=%llu  GICR_WAKER %llx->%llx  "
               "GICR_ISENABLER0=0x%llx  GICD_CTLR %llx->%llx  ICC_IGRPEN1_EL1=0x%llx\n",
               i, (unsigned long long)a[0], (unsigned long long)a[1],
               (unsigned long long)a[2], (unsigned long long)a[3],
               (unsigned long long)a[4], (unsigned long long)a[5],
               (unsigned long long)a[6], (unsigned long long)a[7]);
    }
    printf("  receiver IRQs taken: %llu / %llu   last ICC_IAR1_EL1 = 0x%llx\n",
           (unsigned long long)a1[8], (unsigned long long)iters,
           (unsigned long long)a1[9]);
    printf("  cost of one MSR ICC_SGI1R_EL1 with no deliverable target: %.1f ns\n",
           (double)a0[15] * 1e9 / 24000000.0 / 4096.0);
    printf("  sender resends: %llu, gave up on: %llu\n",
           (unsigned long long)a0[13], (unsigned long long)a0[14]);
    {
        uint64_t *at = (uint64_t *)(mem + OFF_RES);
        uint64_t hist[8] = {0}; uint64_t seen = 0;
        for (uint64_t i = 0; i < iters; i++) {
            if (at[i] == 0) break;
            seen++;
            uint64_t b = at[i] == 1 ? 0 : at[i] == 2 ? 1 : at[i] <= 4 ? 2 :
                         at[i] <= 8 ? 3 : at[i] <= 16 ? 4 : at[i] <= 32 ? 5 :
                         at[i] < 64 ? 6 : 7;
            hist[b]++;
        }
        printf("  SGIs completed: %llu; attempts needed  1:%llu 2:%llu 3-4:%llu 5-8:%llu "
               "9-16:%llu 17-32:%llu 33-63:%llu 64(gave up):%llu\n",
               (unsigned long long)seen, (unsigned long long)hist[0],
               (unsigned long long)hist[1], (unsigned long long)hist[2],
               (unsigned long long)hist[3], (unsigned long long)hist[4],
               (unsigned long long)hist[5], (unsigned long long)hist[6],
               (unsigned long long)hist[7]);
    }
    printf("  hv_vcpu_run returns: vcpu0 = %d, vcpu1 = %d   (1 each = no host exit at all)\n",
           C[0].exits, C[1].exits);
    if (C[0].hvc || C[1].hvc)
        printf("  guest HVC codes: vcpu0 #%llu vcpu1 #%llu (11 = SGI never arrived, "
               "10 = rendezvous timeout, 9 = synchronous fault)\n",
               (unsigned long long)C[0].hvc, (unsigned long long)C[1].hvc);
    if (C[0].hvc == 9 || C[1].hvc == 9)
        printf("  fault: ESR=0x%llx FAR=0x%llx ELR=0x%llx\n",
               (unsigned long long)a1[10], (unsigned long long)a1[11],
               (unsigned long long)a1[12]);

    uint64_t got = a1[8];
    if (got > 16) {
        int n = (int)(got < iters ? got : iters);
        double *v = malloc(sizeof(double) * (size_t)n);
        uint64_t *res = (uint64_t *)(mem + OFF_RES + 0x100000);
        for (int i = 0; i < n; i++) v[i] = (double)res[i] * 1e9 / 24000000.0;
        qsort(v, (size_t)n, sizeof(double), dcmp);
        printf("\n  SGI latency, ICC_SGI1R_EL1 write on vcpu0 -> EL1 IRQ vector on vcpu1:\n");
        printf("    p50 %7.1f ns   p10 %7.1f   p90 %7.1f   p99 %7.1f   min %7.1f   (n=%d,"
               " CNTVCT granularity 41.67 ns)\n",
               v[n/2], v[n/10], v[n - 1 - n/10], v[(int)(n*0.99)], v[0], n);
        free(v);
    }

    printf("\n  VERDICT: %s\n",
           (got >= iters && C[0].exits == 1 && C[1].exits == 1)
             ? "SGIs are delivered vCPU-to-vCPU entirely inside the VM. No host exit occurred."
             : (got > 16 ? "SGIs delivered, but the run was not exit-free -- see counts above."
                         : "SGI delivery did NOT work; see the diagnostics above."));
    hv_vm_destroy();
    return 0;
}
