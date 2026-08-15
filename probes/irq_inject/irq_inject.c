// 22 — Asynchronous interrupt injection into JIT-translated code.
//
// Measures, all inside an HVF VM at guest EL1 with our own VBAR_EL1:
//
//   poll     cost of a per-block-entry "is an interrupt pending?" check in a realistic
//            translated loop, as a % of the hot loop, swept over block size and check
//            strategy (none / load+test per block / load+test at the backward branch only /
//            reserved-register test per block); plus the same question for a translated
//            REP MOVS loop (per-element vs per-chunk); plus the cost of an EL1 self-patch
//            of a chained branch (store + DC CVAU + IC IVAU + barriers); plus the cost of
//            arming an ARM virtual-timer deadline (the TSC_DEADLINE backend).
//   hvc      bare HVC round trip to the host — the exit floor on THIS machine.
//   hostkick hv_vcpus_exit + hv_vcpu_set_pending_interrupt -> guest EL1 IRQ vector.
//   vtnogic  does the ARM virtual timer reach guest EL1 with NO framework GIC?
//   vtgic    does it reach guest EL1 WITH a framework GIC, and at what latency/jitter?
//            also: IRQ entry+ERET round trip, and the cost of ICC_IAR1/ICC_EOIR1.
//   protect  cross-vCPU "force a boundary" by killing a stage-1 mapping the target is
//            executing from: TLBI VAAE1IS on vCPU0 -> instruction abort on vCPU1.
//
// Build: ../build.sh irq_inject/irq_inject.c
// Run:   ./bin/irq_inject <subcommand>
//
// Layout of guest RAM (16 MiB at IPA 0x40000000, identity-mapped VA=IPA):
//   0x000000  code
//   0x100000  vectors / page tables / param blocks / shared / flag
//   0x200000  4 KiB-granule window (L3-mapped) — trampoline page for `protect`
//   0x400000  scratch
//   0x500000  REP source
//   0x600000  REP destination
//   0x700000  patch target (a page of NOPs)
//   0x800000  results

#include <Hypervisor/Hypervisor.h>
#include <inttypes.h>
#include <math.h>
#include <pthread.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <sys/mman.h>
#include <pthread/qos.h>
#include <mach/mach_time.h>

#define RAM_IPA   0x40000000ULL
#define RAM_SIZE  0x01000000ULL           // 16 MiB

#define OFF_CODE    0x000000ULL
#define OFF_VEC     0x100000ULL
#define OFF_L0      0x102000ULL
#define OFF_L1      0x103000ULL
#define OFF_L2RAM   0x104000ULL
#define OFF_L2GIC   0x105000ULL
#define OFF_L3      0x106000ULL           // 4 KiB pages for VA 0x40200000..0x403fffff
#define OFF_P0      0x110000ULL
#define OFF_P1      0x112000ULL
#define OFF_SHARED  0x114000ULL
#define OFF_FLAG    0x116000ULL
#define OFF_TRAMP   0x200000ULL
#define OFF_SCRATCH 0x400000ULL
#define OFF_SRC     0x500000ULL
#define OFF_DST     0x600000ULL
#define OFF_PATCH   0x700000ULL
#define OFF_RES     0x800000ULL

#define GICD_BASE  0x10000000ULL
#define GICR_BASE  0x11000000ULL
#define MSI_BASE   0x14000000ULL

#define D_TABLE  3ULL
#define D_BLOCK  1ULL
#define D_PAGE   3ULL
#define D_AF     (1ULL << 10)
#define D_SH_IS  (3ULL << 8)
#define D_UXN    (1ULL << 54)
#define D_PXN    (1ULL << 53)
#define ATTR_DEV (1ULL << 2)              // MAIR index 1

#define MAIR_VAL (0xFFULL | (0x00ULL << 8))
#define TCR_VAL  (16ULL | (1ULL<<8) | (1ULL<<10) | (3ULL<<12) \
                 | (16ULL<<16) | (1ULL<<23) | (2ULL<<30) | (2ULL<<32))

#define HV(e) do { hv_return_t _r = (e); if (_r != HV_SUCCESS) { \
    fprintf(stderr, "  %s -> 0x%x at %d\n", #e, _r, __LINE__); exit(2);} } while (0)

// CNTVCT is 24 MHz on Apple silicon; confirmed by reading CNTFRQ below.
static double g_tick_ns = 1000000000.0 / 24000000.0;

// ---------------------------------------------------------------------------
// guest payload
// ---------------------------------------------------------------------------
__asm__(
".section __TEXT,__text\n"
".p2align 2\n"

// ---- one "translated x86 instruction" = 4 ARM ops ----
".macro XALU\n"
"  add x10, x10, #1\n"
"  eor x11, x11, x10\n"
"  add x12, x12, x11\n"
"  sub x13, x13, #1\n"
".endm\n"
".macro XMEM\n"
"  ldr x14, [x2, #8]\n"
"  add x14, x14, #1\n"
"  str x14, [x2, #16]\n"
"  add x15, x15, x14\n"
".endm\n"
// 5 x86 instructions, 40% of them memory-touching (06 section 2.3 density)
".macro XGROUP\n"
"  XALU\n"
"  XALU\n"
"  XMEM\n"
"  XALU\n"
"  XMEM\n"
".endm\n"

// one translated block: optional entry check, k groups, then the chaining branch
".macro PBLOCK k, chk\n"
"  .if \\chk == 1\n"
"    ldr w9, [x1]\n"
"    cbnz w9, 99f\n"
"  .endif\n"
"  .if \\chk == 3\n"
"    cbnz x24, 99f\n"
"  .endif\n"
"  .rept \\k\n"
"    XGROUP\n"
"  .endr\n"
"  b 1f\n"
"1:\n"
".endm\n"

// one timed section: 8 chained blocks per outer iteration, 2,000,000 x86 insns total
".macro PSECT k, chk, hdr, outoff\n"
"  mov x0, #(50000/\\k)\n"
"  isb\n"
"  mrs x16, cntvct_el0\n"
"98:\n"
"  .if \\hdr\n"
"    ldr w9, [x1]\n"
"    cbnz w9, 99f\n"
"  .endif\n"
"  .rept 8\n"
"    PBLOCK \\k, \\chk\n"
"  .endr\n"
"  subs x0, x0, #1\n"
"  b.ne 98b\n"
"  isb\n"
"  mrs x17, cntvct_el0\n"
"  sub x17, x17, x16\n"
"  str x17, [x28, #\\outoff]\n"
"  b 97f\n"
"99:\n"
"  hvc #11\n"
"97:\n"
".endm\n"

// check-DENSITY sweep: n checks per 5-x86-instruction block.  The slope in n is a far
// more sensitive estimator of the per-check cost than a checked-vs-unchecked difference.
".macro DBLOCK nchk, kind\n"
"  .if \\kind == 1\n"
"    .rept \\nchk\n"
"      ldr w9, [x1]\n"
"      cbnz w9, 99f\n"
"    .endr\n"
"  .endif\n"
"  .if \\kind == 2\n"
"    .rept \\nchk\n"
"      cbnz x24, 99f\n"
"    .endr\n"
"  .endif\n"
"  XGROUP\n"
"  b 1f\n"
"1:\n"
".endm\n"
".macro DSECT nchk, kind, outoff\n"
"  mov x0, #50000\n"
"  isb\n"
"  mrs x16, cntvct_el0\n"
"98:\n"
"  .rept 8\n"
"    DBLOCK \\nchk, \\kind\n"
"  .endr\n"
"  subs x0, x0, #1\n"
"  b.ne 98b\n"
"  isb\n"
"  mrs x17, cntvct_el0\n"
"  sub x17, x17, x16\n"
"  str x17, [x28, #\\outoff]\n"
"  b 97f\n"
"99:\n"
"  hvc #11\n"
"97:\n"
".endm\n"

// translated REP MOVSQ.  x3=src x4=dst x5=count x1=flag
".macro RSECT chunk, mode, outoff\n"
"  mov x0, #512\n"
"  isb\n"
"  mrs x16, cntvct_el0\n"
"90:\n"
"  mov x6, x3\n"
"  mov x7, x4\n"
"  mov x8, x5\n"
"  .if \\mode == 0\n"
"91:\n"
"  ldr x9, [x6], #8\n"
"  str x9, [x7], #8\n"
"  subs x8, x8, #1\n"
"  b.ne 91b\n"
"  .endif\n"
"  .if \\mode == 1\n"
"91:\n"
"  ldr w12, [x1]\n"
"  cbnz w12, 99f\n"
"  ldr x9, [x6], #8\n"
"  str x9, [x7], #8\n"
"  subs x8, x8, #1\n"
"  b.ne 91b\n"
"  .endif\n"
"  .if \\mode == 2\n"
"91:\n"
"  ldr w12, [x1]\n"
"  cbnz w12, 99f\n"
"  mov x13, #\\chunk\n"
"92:\n"
"  ldr x9, [x6], #8\n"
"  str x9, [x7], #8\n"
"  subs x13, x13, #1\n"
"  b.ne 92b\n"
"  subs x8, x8, #\\chunk\n"
"  b.ne 91b\n"
"  .endif\n"
"  .if \\mode == 3\n"
"91:\n"
"  cbnz x24, 99f\n"
"  ldr x9, [x6], #8\n"
"  str x9, [x7], #8\n"
"  subs x8, x8, #1\n"
"  b.ne 91b\n"
"  .endif\n"
"  subs x0, x0, #1\n"
"  b.ne 90b\n"
"  isb\n"
"  mrs x17, cntvct_el0\n"
"  sub x17, x17, x16\n"
"  str x17, [x28, #\\outoff]\n"
"  b 97f\n"
"99:\n"
"  hvc #11\n"
"97:\n"
".endm\n"

// ---- entry ----
".globl _g_start\n"
"_g_start:\n"
"    mov  x28, x4\n"
"    mov  x27, x5\n"
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
"    mov  x26, #0\n"                 // sync-handler override
"    mov  x25, #0\n"                 // irq-handler override
"    mov  x24, #0\n"                 // reserved "interrupt pending" register
"    mov  x19, #0\n"                 // event counter
"    ldr  x1,  [x28, #0x000]\n"      // flag VA
"    ldr  x2,  [x28, #0x008]\n"      // scratch VA
"    ldr  x22, [x28, #0x010]\n"      // shared VA
"    ldr  x23, [x28, #0x018]\n"      // iterations
"    ldr  x11, [x28, #0x028]\n"      // result cursor VA
"    ldr  x20, [x28, #0x030]\n"      // GICD VA
"    ldr  x21, [x28, #0x038]\n"      // my GICR VA
"    ldr  x3,  [x28, #0x040]\n"      // src VA
"    ldr  x4,  [x28, #0x048]\n"      // dst VA
"    ldr  x5,  [x28, #0x050]\n"      // element count / trampoline VA
"    mrs  x9, cntfrq_el0\n"
"    str  x9, [x28, #0x700]\n"
"    cmp  x27, #0\n"
"    b.eq Lpoll\n"
"    cmp  x27, #1\n"
"    b.eq Lhvc\n"
"    cmp  x27, #2\n"
"    b.eq Lkick\n"
"    cmp  x27, #3\n"
"    b.eq Lvtimer\n"
"    cmp  x27, #4\n"
"    b.eq Lprot0\n"
"    cmp  x27, #5\n"
"    b.eq Lprot1\n"
"    cmp  x27, #6\n"
"    b.eq Lsmppoll\n"
"    cmp  x27, #7\n"
"    b.eq Lsmpwrite\n"
"    hvc  #8\n"

// =====================================================================
// role 0 — the polling-cost sweep, plus REP, patch and timer-arm costs
// =====================================================================
"Lpoll:\n"
"    mov  x10, #0\n"
"    mov  x11, #0\n"
"    mov  x12, #0\n"
"    mov  x13, #0\n"
"    mov  x14, #0\n"
"    mov  x15, #0\n"
// warm the flag line and the scratch line
"    ldr  w9, [x1]\n"
"    ldr  x9, [x2]\n"
// --- k = 1 group (5 x86 insns/block) ---
"    PSECT 1, 0, 0, 0x800\n"
"    PSECT 1, 1, 0, 0x808\n"
"    PSECT 1, 0, 1, 0x810\n"
"    PSECT 1, 3, 0, 0x818\n"
// --- k = 2 (10) ---
"    PSECT 2, 0, 0, 0x820\n"
"    PSECT 2, 1, 0, 0x828\n"
"    PSECT 2, 0, 1, 0x830\n"
"    PSECT 2, 3, 0, 0x838\n"
// --- k = 4 (20) ---
"    PSECT 4, 0, 0, 0x840\n"
"    PSECT 4, 1, 0, 0x848\n"
"    PSECT 4, 0, 1, 0x850\n"
"    PSECT 4, 3, 0, 0x858\n"
// --- k = 8 (40) ---
"    PSECT 8, 0, 0, 0x860\n"
"    PSECT 8, 1, 0, 0x868\n"
"    PSECT 8, 0, 1, 0x870\n"
"    PSECT 8, 3, 0, 0x878\n"
// --- k = 16 (80) ---
"    PSECT 16, 0, 0, 0x880\n"
"    PSECT 16, 1, 0, 0x888\n"
"    PSECT 16, 0, 1, 0x890\n"
"    PSECT 16, 3, 0, 0x898\n"
// second pass over the two extremes, to bound drift
"    PSECT 1, 0, 0, 0x8a0\n"
"    PSECT 1, 1, 0, 0x8a8\n"
"    PSECT 16, 0, 0, 0x8b0\n"
"    PSECT 16, 1, 0, 0x8b8\n"
"    str  x10, [x28, #0x7f0]\n"      // keep the results live
"    str  x15, [x28, #0x7f8]\n"
// --- check-density slope: n load+cbnz checks per 5-x86-insn block ---
"    DSECT 0,  1, 0x8c0\n"
"    DSECT 1,  1, 0x8c8\n"
"    DSECT 2,  1, 0x8d0\n"
"    DSECT 4,  1, 0x8d8\n"
"    DSECT 8,  1, 0x8e0\n"
"    DSECT 16, 1, 0x8e8\n"
"    DSECT 32, 1, 0x8f0\n"
// --- same, with the reserved-register form ---
"    DSECT 1,  2, 0x9c0\n"
"    DSECT 2,  2, 0x9c8\n"
"    DSECT 4,  2, 0x9d0\n"
"    DSECT 8,  2, 0x9d8\n"
"    DSECT 16, 2, 0x9e0\n"
"    DSECT 32, 2, 0x9e8\n"
"    DSECT 0,  1, 0x9f0\n"           // baseline repeat, for drift

// --- translated REP MOVSQ ---
"    RSECT 0,   0, 0x900\n"
"    RSECT 0,   1, 0x908\n"
"    RSECT 8,   2, 0x910\n"
"    RSECT 64,  2, 0x918\n"
"    RSECT 512, 2, 0x920\n"
"    RSECT 0,   3, 0x928\n"
"    RSECT 0,   0, 0x930\n"          // repeat of the baseline, for drift

// --- cost of self-patching one instruction in the code cache from EL1 ---
"    ldr  x6, [x28, #0x058]\n"       // patch page VA
"    mov  x9, #0xd503\n"             // NOP = 0xd503201f
"    lsl  x9, x9, #16\n"
"    movk x9, #0x201f\n"
"    mov  x0, #10000\n"
"    isb\n"
"    mrs  x16, cntvct_el0\n"
"80: str  w9, [x6]\n"
"    subs x0, x0, #1\n"
"    b.ne 80b\n"
"    isb\n"
"    mrs  x17, cntvct_el0\n"
"    sub  x17, x17, x16\n"
"    str  x17, [x28, #0xa00]\n"      // bare store baseline
"    mov  x0, #10000\n"
"    isb\n"
"    mrs  x16, cntvct_el0\n"
"81: str  w9, [x6]\n"
"    dc   cvau, x6\n"
"    dsb  ish\n"
"    ic   ivau, x6\n"
"    dsb  ish\n"
"    isb\n"
"    subs x0, x0, #1\n"
"    b.ne 81b\n"
"    isb\n"
"    mrs  x17, cntvct_el0\n"
"    sub  x17, x17, x16\n"
"    str  x17, [x28, #0xa08]\n"      // full I-cache-coherent patch
"    mov  x0, #10000\n"
"    isb\n"
"    mrs  x16, cntvct_el0\n"
"82: isb\n"
"    subs x0, x0, #1\n"
"    b.ne 82b\n"
"    isb\n"
"    mrs  x17, cntvct_el0\n"
"    sub  x17, x17, x16\n"
"    str  x17, [x28, #0xa10]\n"      // ISB alone

// --- cost of reading the counter and of arming a virtual-timer deadline ---
"    mov  x0, #10000\n"
"    isb\n"
"    mrs  x16, cntvct_el0\n"
"83: mrs  x9, cntvct_el0\n"
"    subs x0, x0, #1\n"
"    b.ne 83b\n"
"    isb\n"
"    mrs  x17, cntvct_el0\n"
"    sub  x17, x17, x16\n"
"    str  x17, [x28, #0xa18]\n"      // MRS CNTVCT_EL0
"    mov  x7, #2\n"                  // CNTV_CTL: IMASK=1, ENABLE=0 -> never fires
"    mov  x0, #10000\n"
"    isb\n"
"    mrs  x16, cntvct_el0\n"
"84: mrs  x9, cntvct_el0\n"
"    add  x9, x9, #2400\n"
"    msr  cntv_cval_el0, x9\n"
"    msr  cntv_ctl_el0, x7\n"
"    subs x0, x0, #1\n"
"    b.ne 84b\n"
"    isb\n"
"    mrs  x17, cntvct_el0\n"
"    sub  x17, x17, x16\n"
"    str  x17, [x28, #0xa20]\n"      // arm a deadline (TSC_DEADLINE backend)
"    hvc  #0\n"

// =====================================================================
// role 6/7 — is the poll flag still free when another vCPU keeps writing its line?
//   x22+0x08 = writer target VA (same line as the flag, or a far line)
// =====================================================================
"Lsmppoll:\n"
"    mov  x10, #0\n"
"    mov  x11, #0\n"
"    mov  x12, #0\n"
"    mov  x13, #0\n"
"    mov  x14, #0\n"
"    mov  x15, #0\n"
"    add  x6, x22, #0x40\n"
"    mov  w0, #1\n"
"    ldaddal w0, w9, [x6]\n"
"    mov  x0, #0x20000000\n"
"30: ldar w9, [x6]\n"
"    cmp  w9, #2\n"
"    b.hs 31f\n"
"    subs x0, x0, #1\n"
"    b.ne 30b\n"
"    hvc  #10\n"
"31: PSECT 5, 0, 0, 0x800\n"
"    PSECT 5, 1, 0, 0x808\n"
"    PSECT 5, 3, 0, 0x818\n"
"    DSECT 0,  1, 0x8c0\n"
"    DSECT 8,  1, 0x8e0\n"
"    DSECT 8,  2, 0x9d8\n"
"    PSECT 5, 0, 0, 0x8a0\n"
"    PSECT 5, 1, 0, 0x8a8\n"
"    mov  x0, #1\n"
"    add  x6, x22, #0x60\n"
"    stlr x0, [x6]\n"
"    hvc  #0\n"
"Lsmpwrite:\n"
"    ldr  x7, [x22, #0x08]\n"        // line to hammer
"    add  x6, x22, #0x40\n"
"    mov  w0, #1\n"
"    ldaddal w0, w9, [x6]\n"
"    mov  x0, #0x20000000\n"
"32: ldar w9, [x6]\n"
"    cmp  w9, #2\n"
"    b.hs 33f\n"
"    subs x0, x0, #1\n"
"    b.ne 32b\n"
"    hvc  #10\n"
"33: add  x6, x22, #0x60\n"
"    mov  x9, #0\n"
"34: add  x9, x9, #1\n"
"    stlr x9, [x7]\n"
"    mov  x0, #200\n"                // ~ one store per few hundred ns
"35: subs x0, x0, #1\n"
"    b.ne 35b\n"
"    ldar x0, [x6]\n"
"    cbz  x0, 34b\n"
"    str  x9, [x28, #0x708]\n"
"    hvc  #0\n"

// =====================================================================
// role 1 — bare HVC round trip
// =====================================================================
"Lhvc:\n"
"    mov  x0, x23\n"
"    isb\n"
"    mrs  x16, cntvct_el0\n"
"70: hvc  #14\n"
"    subs x0, x0, #1\n"
"    b.ne 70b\n"
"    isb\n"
"    mrs  x17, cntvct_el0\n"
"    sub  x17, x17, x16\n"
"    str  x17, [x28, #0xa50]\n"
// same loop shape, but a local SVC to EL1 instead of an HVC to the host
"    mov  x0, x23\n"
"    adr  x26, Lsvcret\n"
"    isb\n"
"    mrs  x16, cntvct_el0\n"
"71: svc  #0\n"
"Lsvcret2:\n"
"    subs x0, x0, #1\n"
"    b.ne 71b\n"
"    isb\n"
"    mrs  x17, cntvct_el0\n"
"    sub  x17, x17, x16\n"
"    str  x17, [x28, #0xa58]\n"
"    mov  x26, #0\n"
"    hvc  #0\n"
"Lsvcret:\n"
"    eret\n"

// =====================================================================
// role 2 — host-mediated IPI target: spin with IRQs unmasked
// =====================================================================
"Lkick:\n"
"    adr  x25, Lkickirq\n"
"    msr  daifclr, #2\n"
"    isb\n"
"    hvc  #12\n"                     // tell the host we are armed
"60: mov  x0, #0x10000000\n"
"61: subs x0, x0, #1\n"
"    b.ne 61b\n"
"    b    60b\n"
"Lkickirq:\n"
"    mrs  x9, cntvct_el0\n"
"    str  x9, [x11], #8\n"
"    add  x19, x19, #1\n"
"    str  x19, [x28, #0x708]\n"
"    hvc  #13\n"
"    eret\n"

// =====================================================================
// role 3 — virtual timer.  x22+0x00 = 1 if a GIC is present.
// =====================================================================
"Lvtimer:\n"
"    adr  x25, Lvtirq\n"
"    ldr  x9, [x22]\n"
"    cbz  x9, Lvtnogic\n"
// ---- bring up the GICv3 CPU interface from guest EL1 ----
"    mov  x26, #1\n"                 // survive an EL2 sysreg trap: host sets x26=0 => trapped
"    mrs  x0, S3_0_C12_C12_5\n"      // ICC_SRE_EL1
"    orr  x0, x0, #1\n"
"    msr  S3_0_C12_C12_5, x0\n"
"    isb\n"
"    mov  x26, #0\n"
"    ldr  w0, [x21, #0x14]\n"        // GICR_WAKER
"    and  w0, w0, #0xfffffffd\n"
"    str  w0, [x21, #0x14]\n"
"    dsb  sy\n"
"    mov  x6, #0x100000\n"
"50: ldr  w0, [x21, #0x14]\n"
"    tbz  w0, #2, 51f\n"
"    subs x6, x6, #1\n"
"    b.ne 50b\n"
"51: str  w0, [x28, #0x710]\n"
"    add  x7, x21, #0x10000\n"       // SGI/PPI frame
"    mov  w0, #-1\n"
"    str  w0, [x7, #0x080]\n"        // GICR_IGROUPR0 = all Group1
"    str  wzr, [x7, #0x0d00]\n"      // GICR_IGRPMODR0 = 0
"    mov  x6, #0\n"
"52: add  x8, x7, x6\n"
"    str  wzr, [x8, #0x400]\n"       // GICR_IPRIORITYR
"    add  x6, x6, #4\n"
"    cmp  x6, #32\n"
"    b.lt 52b\n"
"    mov  w0, #-1\n"
"    str  w0, [x7, #0x100]\n"        // GICR_ISENABLER0: SGI 0-15 + PPI 16-31 (27 = vtimer)
"    dsb  sy\n"
"    ldr  w0, [x7, #0x100]\n"
"    str  w0, [x28, #0x718]\n"
"    ldr  w0, [x20, #0x000]\n"       // GICD_CTLR
"    mov  w6, #0x12\n"
"    orr  w0, w0, w6\n"
"    str  w0, [x20, #0x000]\n"
"    dsb  sy\n"
"    ldr  w0, [x20, #0x000]\n"
"    str  w0, [x28, #0x720]\n"
"    mov  x0, #0xf0\n"
"    msr  S3_0_C4_C6_0, x0\n"        // ICC_PMR_EL1
"    msr  S3_0_C12_C12_3, xzr\n"     // ICC_BPR1_EL1
"    mov  x0, #1\n"
"    msr  S3_0_C12_C12_7, x0\n"      // ICC_IGRPEN1_EL1
"    isb\n"
"    mrs  x0, S3_0_C12_C12_7\n"
"    str  x0, [x28, #0x728]\n"
// cost of ICC_IAR1_EL1 + ICC_EOIR1_EL1 with nothing pending (spurious 1023)
"    mov  x0, #2000\n"
"    isb\n"
"    mrs  x16, cntvct_el0\n"
"53: mrs  x9, S3_0_C12_C12_0\n"      // ICC_IAR1_EL1
"    subs x0, x0, #1\n"
"    b.ne 53b\n"
"    isb\n"
"    mrs  x17, cntvct_el0\n"
"    sub  x17, x17, x16\n"
"    str  x17, [x28, #0xa28]\n"
"    str  x9,  [x28, #0xa30]\n"      // what IAR returned (expect 1023)
"    mov  x0, #2000\n"
"    mov  x10, #1023\n"
"    isb\n"
"    mrs  x16, cntvct_el0\n"
"54: msr  S3_0_C12_C12_1, x10\n"     // ICC_EOIR1_EL1 (spurious: architecturally a no-op)
"    subs x0, x0, #1\n"
"    b.ne 54b\n"
"    isb\n"
"    mrs  x17, cntvct_el0\n"
"    sub  x17, x17, x16\n"
"    str  x17, [x28, #0xa38]\n"
// which GIC accesses are expensive?  time four more, 2000 each.
"    mov  x0, #2000\n"
"    mov  x10, #0xf0\n"
"    isb\n"
"    mrs  x16, cntvct_el0\n"
"64: msr  S3_0_C4_C6_0, x10\n"      // MSR ICC_PMR_EL1
"    subs x0, x0, #1\n"
"    b.ne 64b\n"
"    isb\n"
"    mrs  x17, cntvct_el0\n"
"    sub  x17, x17, x16\n"
"    str  x17, [x28, #0xa60]\n"
"    mov  x0, #2000\n"
"    isb\n"
"    mrs  x16, cntvct_el0\n"
"65: mrs  x9, S3_0_C4_C6_0\n"       // MRS ICC_PMR_EL1
"    subs x0, x0, #1\n"
"    b.ne 65b\n"
"    isb\n"
"    mrs  x17, cntvct_el0\n"
"    sub  x17, x17, x16\n"
"    str  x17, [x28, #0xa68]\n"
"    add  x7, x21, #0x10000\n"
"    mov  x0, #2000\n"
"    isb\n"
"    mrs  x16, cntvct_el0\n"
"66: ldr  w9, [x7, #0x100]\n"       // LDR GICR_ISENABLER0 (Device MMIO)
"    subs x0, x0, #1\n"
"    b.ne 66b\n"
"    isb\n"
"    mrs  x17, cntvct_el0\n"
"    sub  x17, x17, x16\n"
"    str  x17, [x28, #0xa70]\n"
"    mov  x0, #2000\n"
"    mov  w9, #-1\n"
"    isb\n"
"    mrs  x16, cntvct_el0\n"
"67: str  w9, [x7, #0x100]\n"       // STR GICR_ISENABLER0
"    subs x0, x0, #1\n"
"    b.ne 67b\n"
"    isb\n"
"    mrs  x17, cntvct_el0\n"
"    sub  x17, x17, x16\n"
"    str  x17, [x28, #0xa78]\n"
"Lvtnogic:\n"
"    msr  daifclr, #2\n"
"    isb\n"
"    hvc  #12\n"                     // checkpoint: armed
// ---- single-shot deadline sweep.  x22+0x08 = deadline delta in ticks, +0x10 = samples
"    mov  x19, #0\n"
"    mov  x9, #1\n"
"    str  x9, [x28, #0x760]\n"       // ack mode: handler does IAR + EOI
"    ldr  x6, [x22, #0x08]\n"
"    ldr  x7, [x22, #0x10]\n"
"Lvt1:\n"
"    mrs  x9, cntvct_el0\n"
"    add  x9, x9, x6\n"
"    str  x9, [x22, #0x18]\n"        // publish the deadline
"    msr  cntv_cval_el0, x9\n"
"    mov  x10, #1\n"                 // ENABLE=1, IMASK=0
"    msr  cntv_ctl_el0, x10\n"
"    isb\n"
"    mov  x8, x19\n"
"    mov  x0, #0x08000000\n"
"55: ldr  x12, [x28, #0x708]\n"
"    cmp  x12, x8\n"
"    b.hi 56f\n"
"    subs x0, x0, #1\n"
"    b.ne 55b\n"
"    hvc  #16\n"                     // timer never fired
"56: mov  x19, x12\n"
"    subs x7, x7, #1\n"
"    b.ne Lvt1\n"
// ---- IRQ storm A: level stays asserted, handler does NOT touch the GIC.
//      This is the pure ARM IRQ entry -> VBAR_EL1 -> ERET round trip inside the VM. ----
"    str  x19, [x28, #0x768]\n"     // IRQs after the single-shot phase
"    ldr  x7, [x22, #0x20]\n"        // storm count
"    cbz  x7, Lvtdone\n"
"    str  xzr, [x28, #0x760]\n"      // ack mode 0: no IAR, no EOI
"    mov  x10, #4\n"
"    str  x10, [x28, #0x730]\n"      // storm mode
"    msr  cntv_cval_el0, xzr\n"      // deadline permanently in the past
"    add  x8, x19, x7\n"
"    str  x8, [x28, #0x738]\n"
"    isb\n"
"    mrs  x16, cntvct_el0\n"
"    mov  x10, #1\n"
"    msr  cntv_ctl_el0, x10\n"       // ENABLE, unmasked -> IRQ storm begins
"    isb\n"
"    mov  x0, #0x40000000\n"
"57: ldr  x12, [x28, #0x708]\n"
"    cmp  x12, x8\n"
"    b.hs 58f\n"
"    subs x0, x0, #1\n"
"    b.ne 57b\n"
"    hvc  #17\n"
"58: isb\n"
"    mrs  x17, cntvct_el0\n"
"    sub  x17, x17, x16\n"
"    str  x17, [x28, #0xa40]\n"
"    str  x19, [x28, #0x770]\n"
// ---- IRQ storm B: same, but the handler does ICC_IAR1_EL1 + ICC_EOIR1_EL1 ----
"    ldr  x9, [x22]\n"
"    cbz  x9, Lvtdone\n"
"    mov  x9, #1\n"
"    str  x9, [x28, #0x760]\n"
"    add  x8, x19, x7\n"
"    str  x8, [x28, #0x738]\n"
"    isb\n"
"    mrs  x16, cntvct_el0\n"
"    mov  x10, #1\n"
"    msr  cntv_ctl_el0, x10\n"
"    isb\n"
"    mov  x0, #0x40000000\n"
"62: ldr  x12, [x28, #0x708]\n"
"    cmp  x12, x8\n"
"    b.hs 63f\n"
"    subs x0, x0, #1\n"
"    b.ne 62b\n"
"    hvc  #17\n"
"63: isb\n"
"    mrs  x17, cntvct_el0\n"
"    sub  x17, x17, x16\n"
"    str  x17, [x28, #0xa48]\n"
"    str  x19, [x28, #0x778]\n"
"Lvtdone:\n"
"    mov  x10, #2\n"
"    msr  cntv_ctl_el0, x10\n"
"    isb\n"
"    hvc  #0\n"

"Lvtirq:\n"
"    mrs  x10, cntvct_el0\n"         // stamp before touching anything else
"    ldr  x12, [x28, #0x730]\n"
"    cbnz x12, Lvtstorm\n"
"    ldr  x13, [x22, #0x18]\n"
"    sub  x13, x10, x13\n"           // lateness vs the programmed deadline
"    str  x13, [x11], #8\n"
"    mov  x12, #2\n"                 // IMASK=1: drop the condition
"    msr  cntv_ctl_el0, x12\n"
"    isb\n"
"Lvtstorm:\n"
"    ldr  x9, [x28, #0x760]\n"
"    cbz  x9, 60f\n"
"    mrs  x30, S3_0_C12_C12_0\n"     // ICC_IAR1_EL1
"    msr  S3_0_C12_C12_1, x30\n"     // ICC_EOIR1_EL1
"60: add  x19, x19, #1\n"
"    str  x19, [x28, #0x708]\n"
"    ldr  x12, [x28, #0x730]\n"
"    cbz  x12, 61f\n"
"    ldr  x13, [x28, #0x738]\n"
"    cmp  x19, x13\n"
"    b.lo 61f\n"
"    mov  x12, #2\n"                 // stop: mask the timer, drop the level
"    msr  cntv_ctl_el0, x12\n"
"    isb\n"
"61: eret\n"

// =====================================================================
// role 4/5 — cross-vCPU stage-1 invalidate as a "force a boundary" doorbell
//   vcpu0 (role 4) kills the trampoline page; vcpu1 (role 5) is executing from it.
//   x5 = trampoline VA, x22+0x28 = L3 entry VA, x22+0x30 = the valid descriptor
// =====================================================================
"Lprot0:\n"
"    ldr  x6, [x22, #0x28]\n"        // &L3[trampoline]
"    ldr  x7, [x22, #0x30]\n"        // valid descriptor
"    add  x12, x22, #0x40\n"         // rendezvous
"    add  x13, x22, #0x58\n"         // ack
"    add  x10, x22, #0x60\n"         // stop
"    add  x4,  x22, #0x50\n"         // t0
"    mov  w0, #1\n"
"    ldaddal w0, w9, [x12]\n"
"    mov  x0, #0x20000000\n"
"40: ldar w9, [x12]\n"
"    cmp  w9, #2\n"
"    b.hs 41f\n"
"    subs x0, x0, #1\n"
"    b.ne 40b\n"
"    hvc  #10\n"
"41: mov  x0, #0x02000000\n"         // let vcpu1 get going
"42: subs x0, x0, #1\n"
"    b.ne 42b\n"
"    mov  x14, x23\n"                // rounds
"    mov  x15, #0\n"
"Lprot0a:\n"
"    add  x15, x15, #1\n"
"    isb\n"
"    mrs  x16, cntvct_el0\n"
"    str  x16, [x4]\n"               // publish t0
"    dsb  ish\n"
"    str  xzr, [x6]\n"               // invalidate the descriptor
"    dsb  ishst\n"
"    lsr  x9, x5, #12\n"
"    tlbi vaae1is, x9\n"
"    dsb  ish\n"
"    mov  x0, #0x08000000\n"
"43: ldar x9, [x13]\n"               // vcpu1's ack counter
"    cmp  x9, x15\n"
"    b.hs 44f\n"
"    subs x0, x0, #1\n"
"    b.ne 43b\n"
"    hvc  #18\n"
"44: subs x14, x14, #1\n"
"    b.ne Lprot0a\n"
"    mov  x0, #1\n"
"    stlr x0, [x10]\n"               // stop
"    hvc  #0\n"

"Lprot1:\n"
"    adr  x26, Lprot1sync\n"
"    ldr  x6, [x22, #0x28]\n"
"    ldr  x7, [x22, #0x30]\n"
"    add  x12, x22, #0x40\n"
"    add  x14, x22, #0x58\n"
"    add  x15, x22, #0x60\n"
"    add  x2,  x22, #0x50\n"
"    mov  w0, #1\n"
"    ldaddal w0, w9, [x12]\n"
"    mov  x0, #0x20000000\n"
"45: ldar w9, [x12]\n"
"    cmp  w9, #2\n"
"    b.hs 46f\n"
"    subs x0, x0, #1\n"
"    b.ne 45b\n"
"    hvc  #10\n"
"46: mov  x19, #0\n"
"Lprot1a:\n"
"    blr  x5\n"                      // call into the trampoline page
"    ldar x9, [x15]\n"
"    cbz  x9, Lprot1a\n"
"    str  x19, [x28, #0x708]\n"
"    hvc  #0\n"
"Lprot1sync:\n"
"    mrs  x10, cntvct_el0\n"
"    ldr  x13, [x2]\n"
"    sub  x13, x10, x13\n"
"    str  x13, [x11], #8\n"
"    str  x7, [x6]\n"                // restore the mapping ourselves
"    dsb  ishst\n"
"    lsr  x9, x5, #12\n"
"    tlbi vaae1is, x9\n"
"    dsb  ish\n"
"    isb\n"
"    add  x19, x19, #1\n"
"    dsb  ish\n"
"    stlr x19, [x14]\n"
"    eret\n"
".globl _g_end\n"
"_g_end:\n"

".globl _g_irq\n"
"_g_irq:\n"
"    cbz  x25, 1f\n"
"    br   x25\n"
"1:  hvc  #7\n"
".globl _g_irq_end\n"
"_g_irq_end:\n"

".globl _g_sync\n"
"_g_sync:\n"
"    cbz  x26, 1f\n"
"    br   x26\n"
"1:  mrs  x9, esr_el1\n"
"    mrs  x10, far_el1\n"
"    mrs  x12, elr_el1\n"
"    str  x9,  [x28, #0x740]\n"
"    str  x10, [x28, #0x748]\n"
"    str  x12, [x28, #0x750]\n"
"    hvc  #9\n"
".globl _g_sync_end\n"
"_g_sync_end:\n"

".globl _g_tramp\n"
"_g_tramp:\n"
"    ret\n"
".globl _g_tramp_end\n"
"_g_tramp_end:\n"
);

extern const uint8_t g_start[], g_end[], g_irq[], g_irq_end[],
                     g_sync[], g_sync_end[], g_tramp[], g_tramp_end[];

// ---------------------------------------------------------------------------
// host side
// ---------------------------------------------------------------------------
static uint8_t *mem;
static uint64_t *tab(uint64_t off) { return (uint64_t *)(mem + off); }
static uint64_t *P(uint64_t poff, uint64_t o) { return (uint64_t *)(mem + poff + o); }

static void map_block2(uint64_t l2off, uint64_t va, uint64_t pa, uint64_t attrs) {
    tab(l2off)[(va >> 21) & 0x1ff] = (pa & ~0x1fffffULL) | D_BLOCK | attrs;
}

static void build_ram(bool with_gic) {
    memcpy(mem + OFF_CODE, g_start, (size_t)(g_end - g_start));
    size_t sl = (size_t)(g_sync_end - g_sync), il = (size_t)(g_irq_end - g_irq);
    for (int i = 0; i < 16; i++) memcpy(mem + OFF_VEC + i * 0x80, g_sync, sl);
    memcpy(mem + OFF_VEC + 0x280, g_irq, il);   // current EL SPx, IRQ
    memcpy(mem + OFF_VEC + 0x480, g_irq, il);   // lower EL AArch64, IRQ
    memcpy(mem + OFF_TRAMP, g_tramp, (size_t)(g_tramp_end - g_tramp));

    tab(OFF_L0)[0] = (RAM_IPA + OFF_L1) | D_TABLE;
    tab(OFF_L1)[0] = (RAM_IPA + OFF_L2GIC) | D_TABLE;
    tab(OFF_L1)[1] = (RAM_IPA + OFF_L2RAM) | D_TABLE;
    if (with_gic) {
        for (uint64_t va = GICD_BASE - 0x200000; va < MSI_BASE + 0x400000; va += 0x200000)
            map_block2(OFF_L2GIC, va, va, D_AF | ATTR_DEV | D_UXN | D_PXN);
    }
    for (uint64_t o = 0; o < RAM_SIZE; o += 0x200000ULL)
        map_block2(OFF_L2RAM, RAM_IPA + o, RAM_IPA + o, D_AF | D_SH_IS);
    // replace the 2 MiB block covering OFF_TRAMP with a 4 KiB L3 table
    tab(OFF_L2RAM)[((RAM_IPA + OFF_TRAMP) >> 21) & 0x1ff] = (RAM_IPA + OFF_L3) | D_TABLE;
    for (int i = 0; i < 512; i++)
        tab(OFF_L3)[i] = (RAM_IPA + OFF_TRAMP + (uint64_t)i * 0x1000) | D_PAGE | D_AF | D_SH_IS;
}

typedef struct {
    int role;
    hv_vcpu_t vcpu;
    hv_vcpu_exit_t *vexit;
    uint64_t param_ipa, mpidr;
    volatile int ready, done;
    int exits, rc, vtimer_exits, canceled;
    uint64_t hvc;
    uint64_t hvc_count[32];
    // hostkick
    volatile uint64_t kick_seq, kick_ack;
    volatile uint64_t t0;
    int nkick;
    double *lat;
} ctx_t;
static ctx_t C[2];
static volatile int g_started;
static volatile int g_quit;

static void set_entry(ctx_t *c) {
    HV(hv_vcpu_set_reg(c->vcpu, HV_REG_PC,   RAM_IPA + OFF_CODE));
    HV(hv_vcpu_set_reg(c->vcpu, HV_REG_CPSR, 0x3c5));   // EL1h, DAIF masked
    HV(hv_vcpu_set_reg(c->vcpu, HV_REG_X0,   MAIR_VAL));
    HV(hv_vcpu_set_reg(c->vcpu, HV_REG_X1,   TCR_VAL));
    HV(hv_vcpu_set_reg(c->vcpu, HV_REG_X2,   RAM_IPA + OFF_L0));
    HV(hv_vcpu_set_reg(c->vcpu, HV_REG_X3,   RAM_IPA + OFF_VEC));
    HV(hv_vcpu_set_reg(c->vcpu, HV_REG_X4,   c->param_ipa));
    HV(hv_vcpu_set_reg(c->vcpu, HV_REG_X5,   (uint64_t)c->role));
}

static void *vcpu_thread(void *p) {
    ctx_t *c = p;
    pthread_set_qos_class_self_np(QOS_CLASS_USER_INTERACTIVE, 0);
    HV(hv_vcpu_create(&c->vcpu, &c->vexit, NULL));
    HV(hv_vcpu_set_sys_reg(c->vcpu, HV_SYS_REG_MPIDR_EL1, c->mpidr));
    c->ready = 1;
    while (!g_started) usleep(200);
    set_entry(c);
    for (;;) {
        hv_return_t r = hv_vcpu_run(c->vcpu);
        c->exits++;
        if (r != HV_SUCCESS) { printf("  vcpu%d run -> 0x%x\n", c->role, r); c->rc = 1; break; }
        uint32_t reason = c->vexit->reason;
        if (reason == HV_EXIT_REASON_VTIMER_ACTIVATED) {
            c->vtimer_exits++;
            hv_vcpu_set_vtimer_mask(c->vcpu, true);
            continue;
        }
        if (reason == HV_EXIT_REASON_CANCELED) {
            c->canceled++;
            if (g_quit) { c->rc = 1; break; }
            if (c->role == 2) {
                // host-mediated IPI: inject and resume
                hv_vcpu_set_pending_interrupt(c->vcpu, HV_INTERRUPT_TYPE_IRQ, true);
                continue;
            }
            if (g_quit) { c->rc = 1; break; }
            continue;
        }
        if (reason != HV_EXIT_REASON_EXCEPTION) {
            printf("  !! vcpu%d exit reason %u\n", c->role, reason); c->rc = 1; break;
        }
        uint64_t sy = c->vexit->exception.syndrome, ec = (sy >> 26) & 0x3f;
        if (ec == 0x16) {
            uint64_t imm = sy & 0xffff;
            if (imm < 32) c->hvc_count[imm]++;
            if (imm == 0) { c->hvc = 0; break; }
            if (imm == 14) continue;                       // HVC round-trip probe
            if (imm == 12) { c->ready = 2; continue; }     // armed checkpoint
            if (imm == 13) {                               // hostkick sample done
                hv_vcpu_set_pending_interrupt(c->vcpu, HV_INTERRUPT_TYPE_IRQ, false);
                c->kick_ack = c->kick_seq;
                continue;
            }
            printf("  !! vcpu%d guest hvc #%llu\n", c->role, (unsigned long long)imm);
            for (int i = 0x700; i <= 0x758; i += 8)
                printf("     p[0x%03x] = 0x%llx\n", i,
                       (unsigned long long)*P(c->role ? OFF_P1 : OFF_P0, (uint64_t)i));
            c->hvc = imm; c->rc = 1; break;
        }
        if (ec == 0x18) {                                  // sysreg trapped to EL2
            uint64_t pc = 0; hv_vcpu_get_reg(c->vcpu, HV_REG_PC, &pc);
            hv_vcpu_set_reg(c->vcpu, HV_REG_PC, pc + 4);
            hv_vcpu_set_reg(c->vcpu, HV_REG_X26, 0);
            printf("  !! vcpu%d EL2 SYSREG TRAP ESR=0x%llx PC=0x%llx\n",
                   c->role, (unsigned long long)sy, (unsigned long long)pc);
            continue;
        }
        uint64_t pc = 0; hv_vcpu_get_reg(c->vcpu, HV_REG_PC, &pc);
        printf("  !! vcpu%d unexpected exit ESR=0x%llx EC=0x%llx PC=0x%llx\n",
               c->role, (unsigned long long)sy, (unsigned long long)ec,
               (unsigned long long)pc);
        c->rc = 1; break;
    }
    c->done = 1;
    return NULL;
}

static volatile int wd_done;
static int wd_secs = 60;
static int wd_n = 1;
static void *watchdog(void *p) {
    (void)p;
    for (int i = 0; i < wd_secs * 4 && !wd_done; i++) usleep(250000);
    if (!wd_done) {
        printf("  !! watchdog firing\n");
        g_quit = 1;
        hv_vcpu_t v[2] = { C[0].vcpu, C[1].vcpu };
        hv_vcpus_exit(v, wd_n);
    }
    return NULL;
}

static int dcmp(const void *a, const void *b) {
    double x = *(const double *)a, y = *(const double *)b;
    return x < y ? -1 : x > y ? 1 : 0;
}
static double pct(double *v, int n, double q) {
    if (n <= 0) return 0;
    int i = (int)(q * (n - 1) + 0.5);
    if (i < 0) i = 0; if (i >= n) i = n - 1;
    return v[i];
}

static void init_params(uint64_t poff, uint64_t iters) {
    uint64_t *p = (uint64_t *)(mem + poff);
    memset(p, 0, 0x1000);
    p[0] = RAM_IPA + OFF_FLAG;
    p[1] = RAM_IPA + OFF_SCRATCH;
    p[2] = RAM_IPA + OFF_SHARED;
    p[3] = iters;
    p[5] = RAM_IPA + OFF_RES + (poff == OFF_P1 ? 0x100000 : 0);
    p[6] = GICD_BASE;
    p[7] = GICR_BASE;
    p[8] = RAM_IPA + OFF_SRC;
    p[9] = RAM_IPA + OFF_DST;
    p[10] = 4096;                                  // REP element count
    p[11] = RAM_IPA + OFF_PATCH;
}

static uint64_t ticks(uint64_t poff, uint64_t o) { return *P(poff, o); }
static double ns(uint64_t t) { return (double)t * g_tick_ns; }

// ---------------------------------------------------------------------------
static int run_simple(int role, bool with_gic, uint64_t iters, int nvcpu) {
    HV(hv_vm_create(NULL));
    if (with_gic) {
        hv_gic_config_t cfg = hv_gic_config_create();
        hv_gic_config_set_distributor_base(cfg, GICD_BASE);
        hv_gic_config_set_redistributor_base(cfg, GICR_BASE);
        hv_gic_config_set_msi_region_base(cfg, MSI_BASE);
        hv_gic_config_set_msi_interrupt_range(cfg, 800, 64);
        hv_return_t gr = hv_gic_create(cfg);
        printf("  hv_gic_create -> 0x%x %s\n", gr, gr == HV_SUCCESS ? "OK" : "FAIL");
        if (gr != HV_SUCCESS) return 1;
    }
    mem = mmap(NULL, RAM_SIZE, PROT_READ | PROT_WRITE, MAP_ANON | MAP_PRIVATE, -1, 0);
    if (mem == MAP_FAILED) { perror("mmap"); return 2; }
    memset(mem, 0, RAM_SIZE);
    HV(hv_vm_map(mem, RAM_IPA, RAM_SIZE, HV_MEMORY_READ | HV_MEMORY_WRITE | HV_MEMORY_EXEC));
    build_ram(with_gic);
    init_params(OFF_P0, iters);
    init_params(OFF_P1, iters);
    C[0].role = role; C[0].param_ipa = RAM_IPA + OFF_P0; C[0].mpidr = 0x80000000ULL;
    C[1].role = role + 1; C[1].param_ipa = RAM_IPA + OFF_P1; C[1].mpidr = 0x80000001ULL;
    wd_n = nvcpu;
    pthread_t t[2], wd;
    for (int i = 0; i < nvcpu; i++) pthread_create(&t[i], NULL, vcpu_thread, &C[i]);
    for (int i = 0; i < nvcpu; i++) while (!C[i].ready) usleep(200);
    pthread_create(&wd, NULL, watchdog, NULL);
    g_started = 1;
    for (int i = 0; i < nvcpu; i++) pthread_join(t[i], NULL);
    wd_done = 1; pthread_join(wd, NULL);
    return C[0].rc || (nvcpu > 1 && C[1].rc);
}

static void report_gicr(void) {
    if (*P(OFF_P0, 0x718))
        printf("  GICR_ISENABLER0 readback = 0x%llx (bit27 = vtimer PPI: %s)\n",
               (unsigned long long)*P(OFF_P0, 0x718),
               (*P(OFF_P0, 0x718) & (1u << 27)) ? "ENABLED" : "not enabled");
}

// n checks per 5-x86-instruction block; 400,000 blocks per section
static void report_density(uint64_t poff) {
    const int nc[7] = {0, 1, 2, 4, 8, 16, 32};
    printf("\n  check-density slope: n checks inside every 5-x86-instruction block\n");
    printf("  (400,000 blocks per section; slope = marginal ns per check)\n");
    printf("  %-8s %12s %12s   %12s %12s\n", "n/block",
           "ld+cbnz us", "ns/check", "cbnz-reg us", "ns/check");
    double b = ns(ticks(poff, 0x8c0));
    double b2 = ns(ticks(poff, 0x9f0));
    for (int i = 0; i < 7; i++) {
        double a = ns(ticks(poff, 0x8c0 + (uint64_t)i * 8));
        printf("  %-8d %12.0f %12.4f", nc[i], a/1000,
               nc[i] ? (a - b) / (400000.0 * nc[i]) : 0.0);
        if (i == 0) { printf("   %12s %12s\n", "-", "-"); continue; }
        double r = ns(ticks(poff, 0x9c0 + (uint64_t)(i-1) * 8));
        printf("   %12.0f %12.4f\n", r/1000, (r - b) / (400000.0 * nc[i]));
    }
    printf("  baseline (n=0) measured twice: %.0f us / %.0f us  (drift %+.2f%%)\n",
           b/1000, b2/1000, 100.0*(b2-b)/b);
}

// --------------------------------------------------------------- poll
static void cmd_poll(void) {
    printf("=== per-block interrupt-poll cost, in a translated loop (guest EL1) ===\n");
    if (run_simple(0, false, 0, 1)) { printf("  FAILED\n"); return; }
    printf("  CNTFRQ_EL0 = %llu Hz\n", (unsigned long long)ticks(OFF_P0, 0x700));
    if (ticks(OFF_P0, 0x700)) g_tick_ns = 1e9 / (double)ticks(OFF_P0, 0x700);

    const int ks[5] = {5, 10, 20, 40, 80};
    printf("\n  Each section retires exactly 2,000,000 translated x86 instructions\n");
    printf("  (1 x86 insn = 4 ARM ops; 40%% memory-touching; 8 chained blocks/iteration)\n\n");
    printf("  %-6s %10s %10s %10s %10s   %8s %8s %8s\n",
           "x86/blk", "none", "ld+cbnz", "hdr-only", "cbnz-reg",
           "ld/blk%", "hdr%", "reg/blk%");
    for (int i = 0; i < 5; i++) {
        double base = ns(ticks(OFF_P0, 0x800 + i * 0x20));
        double chk  = ns(ticks(OFF_P0, 0x808 + i * 0x20));
        double hdr  = ns(ticks(OFF_P0, 0x810 + i * 0x20));
        double reg  = ns(ticks(OFF_P0, 0x818 + i * 0x20));
        printf("  %-6d %9.0fus %9.0fus %9.0fus %9.0fus   %+7.2f%% %+7.2f%% %+7.2f%%\n",
               ks[i], base/1000, chk/1000, hdr/1000, reg/1000,
               100.0*(chk-base)/base, 100.0*(hdr-base)/base, 100.0*(reg-base)/base);
    }
    printf("\n  per-x86-instruction cost, and per-check cost in ns:\n");
    printf("  %-6s %10s %12s %12s\n", "x86/blk", "ns/x86insn", "ns/ld-check", "ns/reg-check");
    for (int i = 0; i < 5; i++) {
        double base = ns(ticks(OFF_P0, 0x800 + i * 0x20));
        double chk  = ns(ticks(OFF_P0, 0x808 + i * 0x20));
        double reg  = ns(ticks(OFF_P0, 0x818 + i * 0x20));
        double nblocks = 2000000.0 / ks[i];
        printf("  %-6d %10.3f %12.3f %12.3f\n", ks[i], base/2e6,
               (chk-base)/nblocks, (reg-base)/nblocks);
    }
    {
        double b1 = ns(ticks(OFF_P0, 0x800)), c1 = ns(ticks(OFF_P0, 0x808));
        double b2 = ns(ticks(OFF_P0, 0x8a0)), c2 = ns(ticks(OFF_P0, 0x8a8));
        double b3 = ns(ticks(OFF_P0, 0x880)), c3 = ns(ticks(OFF_P0, 0x888));
        double b4 = ns(ticks(OFF_P0, 0x8b0)), c4 = ns(ticks(OFF_P0, 0x8b8));
        printf("\n  drift control (same section run twice, k=5 and k=80):\n");
        printf("    k=5  none %.0fus / %.0fus   ld+cbnz %.0fus / %.0fus  (%+.2f%% vs %+.2f%%)\n",
               b1/1000, b2/1000, c1/1000, c2/1000,
               100.0*(c1-b1)/b1, 100.0*(c2-b2)/b2);
        printf("    k=80 none %.0fus / %.0fus   ld+cbnz %.0fus / %.0fus  (%+.2f%% vs %+.2f%%)\n",
               b3/1000, b4/1000, c3/1000, c4/1000,
               100.0*(c3-b3)/b3, 100.0*(c4-b4)/b4);
    }

    report_density(OFF_P0);

    printf("\n=== translated REP MOVSQ: how do you keep it interruptible? ===\n");
    printf("  4096 elements x 512 repeats = 2,097,152 8-byte element copies per section\n");
    double rb = ns(ticks(OFF_P0, 0x900));
    struct { const char *n; uint64_t off; double bound_elems; } rr[] = {
        { "no check (uninterruptible)", 0x900, 0 },
        { "load+cbnz every element",    0x908, 1 },
        { "load+cbnz every 8",          0x910, 8 },
        { "load+cbnz every 64",         0x918, 64 },
        { "load+cbnz every 512",        0x920, 512 },
        { "cbnz reg every element",     0x928, 1 },
        { "no check (repeat)",          0x930, 0 },
    };
    printf("  %-30s %10s %10s %10s %14s\n", "strategy", "total", "ns/elem", "overhead",
           "worst-case IRQ");
    for (unsigned i = 0; i < sizeof(rr)/sizeof(rr[0]); i++) {
        double t = ns(ticks(OFF_P0, rr[i].off));
        double per = t / 2097152.0;
        printf("  %-30s %8.0fus %10.3f %+9.1f%% ", rr[i].n, t/1000, per,
               100.0*(t-rb)/rb);
        if (rr[i].bound_elems > 0) printf("%12.1f ns\n", per * rr[i].bound_elems);
        else printf("     unbounded\n");
    }

    printf("\n=== EL1 code-cache self-patch and timer-arm costs ===\n");
    printf("  bare STR to the code page                      %8.2f ns\n",
           ns(ticks(OFF_P0, 0xa00)) / 10000.0);
    printf("  STR + DC CVAU + DSB ISH + IC IVAU + DSB + ISB  %8.2f ns\n",
           ns(ticks(OFF_P0, 0xa08)) / 10000.0);
    printf("  ISB alone                                      %8.2f ns\n",
           ns(ticks(OFF_P0, 0xa10)) / 10000.0);
    printf("  MRS CNTVCT_EL0                                 %8.2f ns\n",
           ns(ticks(OFF_P0, 0xa18)) / 10000.0);
    printf("  arm a deadline (MRS CNTVCT + MSR CVAL + CTL)   %8.2f ns\n",
           ns(ticks(OFF_P0, 0xa20)) / 10000.0);
    printf("\n  host exits during the whole run: %d\n", C[0].exits);
}

// --------------------------------------------------------------- hvc
static void cmd_hvc(void) {
    uint64_t n = 20000;
    printf("=== bare HVC round trip (the host-exit floor on THIS machine) ===\n");
    uint64_t t0 = mach_absolute_time();
    int rc = run_simple(1, false, n, 1);
    uint64_t t1 = mach_absolute_time();
    mach_timebase_info_data_t tb; mach_timebase_info(&tb);
    if (rc) { printf("  FAILED\n"); return; }
    double total = (double)(t1 - t0) * tb.numer / tb.denom;
    printf("  %llu HVC exits, %d hv_vcpu_run returns\n",
           (unsigned long long)n, C[0].exits);
    printf("  whole process (incl. VM setup): %.3f ms\n", total / 1e6);
    printf("  guest-timed HVC round trip (CNTVCT_EL0 either side of the loop):"
           " %.1f ns\n", ns(ticks(OFF_P0, 0xa50)) / (double)n);
    printf("  same loop shape with a local SVC to EL1 instead (no host involved): %.1f ns\n",
           ns(ticks(OFF_P0, 0xa58)) / (double)n);
}

// --------------------------------------------------------------- hostkick
static void cmd_hostkick(void) {
    int N = 200;
    printf("=== host thread -> guest EL1 IRQ vector (hv_vcpus_exit + pending IRQ) ===\n");
    HV(hv_vm_create(NULL));
    mem = mmap(NULL, RAM_SIZE, PROT_READ | PROT_WRITE, MAP_ANON | MAP_PRIVATE, -1, 0);
    memset(mem, 0, RAM_SIZE);
    HV(hv_vm_map(mem, RAM_IPA, RAM_SIZE, HV_MEMORY_READ | HV_MEMORY_WRITE | HV_MEMORY_EXEC));
    build_ram(false);
    init_params(OFF_P0, 0);
    C[0].role = 2; C[0].param_ipa = RAM_IPA + OFF_P0; C[0].mpidr = 0x80000000ULL;
    pthread_t t, wd;
    pthread_create(&t, NULL, vcpu_thread, &C[0]);
    while (!C[0].ready) usleep(200);
    wd_n = 1;
    pthread_create(&wd, NULL, watchdog, NULL);
    g_started = 1;
    while (C[0].ready != 2 && !C[0].done) usleep(200);
    uint64_t off = 0;
    hv_vcpu_get_vtimer_offset(C[0].vcpu, &off);
    printf("  hv_vcpu_get_vtimer_offset = %llu (0 => guest CNTVCT == host mach_absolute_time)\n",
           (unsigned long long)off);
    double *wake = calloc(N, sizeof(double)), *vec = calloc(N, sizeof(double));
    int got = 0;
    for (int i = 0; i < N && !C[0].done; i++) {
        C[0].kick_seq = (uint64_t)i + 1;
        uint64_t t0 = mach_absolute_time();
        hv_vcpu_t v = C[0].vcpu;
        hv_vcpus_exit(&v, 1);
        int spins = 0;
        while (C[0].kick_ack != C[0].kick_seq && !C[0].done && spins++ < 4000000) ;
        if (C[0].kick_ack != C[0].kick_seq) break;
        uint64_t t1 = *(uint64_t *)(mem + OFF_RES + (uint64_t)got * 8);
        wake[got] = 0;
        vec[got] = (double)(t1 - t0) * g_tick_ns;
        got++;
    }
    g_quit = 1;
    hv_vcpu_t v = C[0].vcpu; hv_vcpus_exit(&v, 1);
    wd_done = 1;
    pthread_join(wd, NULL);
    pthread_join(t, NULL);
    if (got > 4) {
        qsort(vec, got, sizeof(double), dcmp);
        printf("  n=%d  host stamp -> guest EL1 IRQ vector:\n", got);
        printf("    p10 %.0f  p50 %.0f  p90 %.0f  p99 %.0f  min %.0f  max %.0f ns\n",
               pct(vec,got,0.10), pct(vec,got,0.50), pct(vec,got,0.90),
               pct(vec,got,0.99), vec[0], vec[got-1]);
    } else printf("  INCONCLUSIVE: only %d samples\n", got);
    printf("  hv_vcpu_run returns: %d (of which CANCELED %d)\n", C[0].exits, C[0].canceled);
    (void)wake;
}

// --------------------------------------------------------------- vtimer
static void cmd_vtimer(bool with_gic, uint64_t delta_ticks, uint64_t nsamp) {
    printf("=== ARM virtual timer -> guest EL1, %s framework GIC ===\n",
           with_gic ? "WITH" : "WITHOUT");
    HV(hv_vm_create(NULL));
    if (with_gic) {
        hv_gic_config_t cfg = hv_gic_config_create();
        hv_gic_config_set_distributor_base(cfg, GICD_BASE);
        hv_gic_config_set_redistributor_base(cfg, GICR_BASE);
        hv_gic_config_set_msi_region_base(cfg, MSI_BASE);
        hv_gic_config_set_msi_interrupt_range(cfg, 800, 64);
        hv_return_t gr = hv_gic_create(cfg);
        printf("  hv_gic_create -> 0x%x %s\n", gr, gr == HV_SUCCESS ? "OK" : "FAIL");
        if (gr != HV_SUCCESS) return;
    }
    mem = mmap(NULL, RAM_SIZE, PROT_READ | PROT_WRITE, MAP_ANON | MAP_PRIVATE, -1, 0);
    memset(mem, 0, RAM_SIZE);
    HV(hv_vm_map(mem, RAM_IPA, RAM_SIZE, HV_MEMORY_READ | HV_MEMORY_WRITE | HV_MEMORY_EXEC));
    build_ram(with_gic);
    init_params(OFF_P0, 0);
    uint64_t *sh = (uint64_t *)(mem + OFF_SHARED);
    sh[0] = with_gic ? 1 : 0;
    sh[1] = delta_ticks;
    sh[2] = nsamp;
    sh[4] = with_gic ? 20000 : 0;      // storm count (needs in-VM delivery)
    C[0].role = 3; C[0].param_ipa = RAM_IPA + OFF_P0; C[0].mpidr = 0x80000000ULL;
    pthread_t t, wd;
    pthread_create(&t, NULL, vcpu_thread, &C[0]);
    while (!C[0].ready) usleep(200);
    wd_n = 1; wd_secs = 30;
    pthread_create(&wd, NULL, watchdog, NULL);
    g_started = 1;
    pthread_join(t, NULL);
    wd_done = 1; pthread_join(wd, NULL);

    printf("  hv_vcpu_run returns: %d   of which VTIMER_ACTIVATED: %d, CANCELED: %d\n",
           C[0].exits, C[0].vtimer_exits, C[0].canceled);
    printf("  guest IRQs taken (EL1 vector entries): %llu\n",
           (unsigned long long)ticks(OFF_P0, 0x708));
    report_gicr();
    uint64_t n = ticks(OFF_P0, 0x708);
    if (n > nsamp) n = nsamp;
    if (n > 2) {
        double *v = calloc(n, sizeof(double));
        for (uint64_t i = 0; i < n; i++)
            v[i] = (double)(int64_t)*(uint64_t *)(mem + OFF_RES + i * 8) * g_tick_ns;
        qsort(v, n, sizeof(double), dcmp);
        printf("  deadline = now + %llu ticks (%.0f ns).  lateness (delivery - deadline):\n",
               (unsigned long long)delta_ticks, (double)delta_ticks * g_tick_ns);
        printf("    n=%llu  p10 %.0f  p50 %.0f  p90 %.0f  p99 %.0f  min %.0f  max %.0f ns\n",
               (unsigned long long)n, pct(v,n,0.10), pct(v,n,0.50), pct(v,n,0.90),
               pct(v,n,0.99), v[0], v[n-1]);
        free(v);
    }
    printf("  phase IRQ counts: single-shot %llu, after storm A %llu, after storm B %llu\n",
           (unsigned long long)ticks(OFF_P0, 0x768),
           (unsigned long long)ticks(OFF_P0, 0x770),
           (unsigned long long)ticks(OFF_P0, 0x778));
    if (ticks(OFF_P0, 0xa40)) {
        double na = (double)(ticks(OFF_P0,0x770) - ticks(OFF_P0,0x768));
        printf("  storm A: IRQ entry -> VBAR_EL1 -> ERET, handler touches NO GIC register\n");
        printf("    %.0f IRQs in %.0f us -> %.1f ns per IRQ round trip\n",
               na, ns(ticks(OFF_P0, 0xa40))/1000.0, ns(ticks(OFF_P0, 0xa40)) / na);
    }
    if (ticks(OFF_P0, 0xa48)) {
        double nb = (double)(ticks(OFF_P0,0x778) - ticks(OFF_P0,0x770));
        printf("  storm B: same, but handler does ICC_IAR1_EL1 + ICC_EOIR1_EL1\n");
        printf("    %.0f IRQs in %.0f us -> %.1f ns per IRQ round trip\n",
               nb, ns(ticks(OFF_P0, 0xa48))/1000.0, ns(ticks(OFF_P0, 0xa48)) / nb);
    }
    if (ticks(OFF_P0, 0xa28))
        printf("  MRS ICC_IAR1_EL1 (spurious, returned %llu): %.1f ns\n",
               (unsigned long long)ticks(OFF_P0, 0xa30),
               ns(ticks(OFF_P0, 0xa28)) / 2000.0);
    if (ticks(OFF_P0, 0xa38))
        printf("  MSR ICC_EOIR1_EL1 (spurious id 1023):        %.1f ns\n",
               ns(ticks(OFF_P0, 0xa38)) / 2000.0);
    if (ticks(OFF_P0, 0xa60)) {
        printf("  MSR ICC_PMR_EL1:                             %.1f ns\n",
               ns(ticks(OFF_P0, 0xa60)) / 2000.0);
        printf("  MRS ICC_PMR_EL1:                             %.1f ns\n",
               ns(ticks(OFF_P0, 0xa68)) / 2000.0);
        printf("  LDR GICR_ISENABLER0 (Device MMIO, stage 2):  %.1f ns\n",
               ns(ticks(OFF_P0, 0xa70)) / 2000.0);
        printf("  STR GICR_ISENABLER0 (Device MMIO, stage 2):  %.1f ns\n",
               ns(ticks(OFF_P0, 0xa78)) / 2000.0);
    }
}

// --------------------------------------------------------------- protect
static void cmd_protect(uint64_t rounds) {
    printf("=== cross-vCPU 'force a boundary' by killing a stage-1 mapping ===\n");
    printf("  vcpu0: store 0 to the L3 entry + TLBI VAAE1IS + DSB ISH\n");
    printf("  vcpu1: executing BLR into that page -> instruction abort at EL1\n");
    HV(hv_vm_create(NULL));
    mem = mmap(NULL, RAM_SIZE, PROT_READ | PROT_WRITE, MAP_ANON | MAP_PRIVATE, -1, 0);
    memset(mem, 0, RAM_SIZE);
    HV(hv_vm_map(mem, RAM_IPA, RAM_SIZE, HV_MEMORY_READ | HV_MEMORY_WRITE | HV_MEMORY_EXEC));
    build_ram(false);
    init_params(OFF_P0, rounds);
    init_params(OFF_P1, rounds);
    *P(OFF_P0, 0x050) = RAM_IPA + OFF_TRAMP;
    *P(OFF_P1, 0x050) = RAM_IPA + OFF_TRAMP;
    uint64_t *sh = (uint64_t *)(mem + OFF_SHARED);
    sh[5] = RAM_IPA + OFF_L3;                          // &L3[0] == the trampoline entry
    sh[6] = tab(OFF_L3)[0];
    C[0].role = 4; C[0].param_ipa = RAM_IPA + OFF_P0; C[0].mpidr = 0x80000000ULL;
    C[1].role = 5; C[1].param_ipa = RAM_IPA + OFF_P1; C[1].mpidr = 0x80000001ULL;
    wd_n = 2; wd_secs = 40;
    pthread_t t[2], wd;
    for (int i = 0; i < 2; i++) pthread_create(&t[i], NULL, vcpu_thread, &C[i]);
    for (int i = 0; i < 2; i++) while (!C[i].ready) usleep(200);
    pthread_create(&wd, NULL, watchdog, NULL);
    g_started = 1;
    for (int i = 0; i < 2; i++) pthread_join(t[i], NULL);
    wd_done = 1; pthread_join(wd, NULL);
    uint64_t n = *P(OFF_P1, 0x708);
    printf("  aborts taken on vcpu1: %llu / %llu rounds; host exits %d + %d\n",
           (unsigned long long)n, (unsigned long long)rounds, C[0].exits, C[1].exits);
    if (n > 4) {
        if (n > rounds) n = rounds;
        double *v = calloc(n, sizeof(double));
        for (uint64_t i = 0; i < n; i++)
            v[i] = (double)*(uint64_t *)(mem + OFF_RES + 0x100000 + i * 8) * g_tick_ns;
        qsort(v, n, sizeof(double), dcmp);
        printf("  vcpu0 stamp -> vcpu1 instruction-abort handler:\n");
        printf("    n=%llu  p10 %.0f  p50 %.0f  p90 %.0f  p99 %.0f  min %.0f  max %.0f ns\n",
               (unsigned long long)n, pct(v,n,0.10), pct(v,n,0.50), pct(v,n,0.90),
               pct(v,n,0.99), v[0], v[n-1]);
        free(v);
    } else printf("  INCONCLUSIVE: %llu samples\n", (unsigned long long)n);
}

// ------------------------------------------------------------- pollsmp
static void cmd_pollsmp(int shared) {
    printf("=== is the poll flag still free when another vCPU writes its cache line? ===\n");
    printf("  arm: writer stores to %s\n",
           shared ? "the SAME 64 B line as the poll flag" : "a line 4 KiB away (control)");
    HV(hv_vm_create(NULL));
    mem = mmap(NULL, RAM_SIZE, PROT_READ | PROT_WRITE, MAP_ANON | MAP_PRIVATE, -1, 0);
    memset(mem, 0, RAM_SIZE);
    HV(hv_vm_map(mem, RAM_IPA, RAM_SIZE, HV_MEMORY_READ | HV_MEMORY_WRITE | HV_MEMORY_EXEC));
    build_ram(false);
    init_params(OFF_P0, 0);
    init_params(OFF_P1, 0);
    uint64_t *sh = (uint64_t *)(mem + OFF_SHARED);
    sh[1] = RAM_IPA + OFF_FLAG + (shared ? 0x08 : 0x1000);
    C[0].role = 6; C[0].param_ipa = RAM_IPA + OFF_P0; C[0].mpidr = 0x80000000ULL;
    C[1].role = 7; C[1].param_ipa = RAM_IPA + OFF_P1; C[1].mpidr = 0x80000001ULL;
    wd_n = 2; wd_secs = 60;
    pthread_t t[2], wd;
    for (int i = 0; i < 2; i++) pthread_create(&t[i], NULL, vcpu_thread, &C[i]);
    for (int i = 0; i < 2; i++) while (!C[i].ready) usleep(200);
    pthread_create(&wd, NULL, watchdog, NULL);
    g_started = 1;
    for (int i = 0; i < 2; i++) pthread_join(t[i], NULL);
    wd_done = 1; pthread_join(wd, NULL);
    printf("  writer stores issued: %llu\n", (unsigned long long)ticks(OFF_P1, 0x708));
    double base = ns(ticks(OFF_P0, 0x800)), chk = ns(ticks(OFF_P0, 0x808));
    double reg = ns(ticks(OFF_P0, 0x818));
    double base2 = ns(ticks(OFF_P0, 0x8a0)), chk2 = ns(ticks(OFF_P0, 0x8a8));
    printf("  k=5 x86/block, 2,000,000 x86 insns, 400,000 blocks per section:\n");
    printf("    no check          %8.0f us   (repeat %.0f us)\n", base/1000, base2/1000);
    printf("    ld+cbnz per block %8.0f us   (repeat %.0f us)  %+.2f%% / %+.2f%%\n",
           chk/1000, chk2/1000, 100.0*(chk-base)/base, 100.0*(chk2-base2)/base2);
    printf("    cbnz reg per block%8.0f us                     %+.2f%%\n",
           reg/1000, 100.0*(reg-base)/base);
    double d0 = ns(ticks(OFF_P0, 0x8c0)), d8 = ns(ticks(OFF_P0, 0x8e0));
    double r8 = ns(ticks(OFF_P0, 0x9d8));
    printf("    density n=0 %.0f us, n=8 ld+cbnz %.0f us -> %.4f ns/check;"
           " n=8 cbnz-reg %.0f us -> %.4f ns/check\n",
           d0/1000, d8/1000, (d8-d0)/(400000.0*8),
           r8/1000, (r8-d0)/(400000.0*8));
}

int main(int argc, char **argv) {
    const char *cmd = argc > 1 ? argv[1] : "poll";
    setvbuf(stdout, NULL, _IOLBF, 0);
    if (!strcmp(cmd, "poll"))          cmd_poll();
    else if (!strcmp(cmd, "hvc"))      cmd_hvc();
    else if (!strcmp(cmd, "hostkick")) cmd_hostkick();
    else if (!strcmp(cmd, "vtnogic"))  cmd_vtimer(false, argc > 2 ? strtoull(argv[2],0,0) : 240,
                                                  argc > 3 ? strtoull(argv[3],0,0) : 200);
    else if (!strcmp(cmd, "vtgic"))    cmd_vtimer(true,  argc > 2 ? strtoull(argv[2],0,0) : 240,
                                                  argc > 3 ? strtoull(argv[3],0,0) : 2000);
    else if (!strcmp(cmd, "protect"))  cmd_protect(argc > 2 ? strtoull(argv[2],0,0) : 500);
    else if (!strcmp(cmd, "pollsmp"))  cmd_pollsmp(argc > 2 ? atoi(argv[2]) : 1);
    else { printf("usage: %s poll|pollsmp [0|1]|hvc|hostkick|vtnogic|vtgic|protect\n",
                  argv[0]); return 1; }
    return 0;
}
