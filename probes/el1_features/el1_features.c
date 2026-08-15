// el1_features -- what a guest at EL1 under Hypervisor.framework can ACTUALLY do.
//
// Doc 13 of this corpus is an audit that found a gate closed on ID-register evidence. So every
// entry here is decided by EXECUTING something and observing the result. Three outcomes are kept
// strictly distinct:
//
//   OK        the instruction executed at guest EL1; we report what it produced
//   TRAP_EL2  it left the VM: hv_vcpu_run returned with an ESR_EL2 syndrome (EC 0x18 sysreg,
//             0x24 stage-2 abort, 0x17 SMC, 0x01 WFx, ...)
//   UNDEF_EL1 the CPU raised an exception into the GUEST's own VBAR_EL1 (ESR_EL1 EC=0x00)
//   EXC_EL1   some other exception into the guest's own vector (abort, BTI, alignment, ...)
//
// and, separately from all of those, "the write succeeded" vs "the write applies": a register that
// takes a value and reads it back is not thereby a register that does anything. Where a behavioural
// consequence exists it is provoked (mode `applies`).
//
// Modes (all default to `all`):
//   regs     system-register read / write+readback / survives-host-exit sweep, incl. Apple IMPDEF
//   applies  behavioural tests that a written register is honoured (TCR/TTBR0/MAIR/SCTLR/VBAR/TPIDR)
//   feat     FEAT_* by executed instruction (PAuth, LSE/LSE2, FlagM/2, JSCVT, BTI, MTE, DIT, SSBS,
//            CRC32, DotProd, SVE) plus the ID-register bits, reported side by side
//   time     in-guest clock: CNTVCT resolution, cost, monotonicity; PMU cycle counter reachability
//   at       AT S1E1R/W/RP/WP/E0R/E0W -- correctness (PAR_EL1) and LATENCY
//   tlbi     TLBI variants -- do they execute, do they APPLY (semantic test), and what they cost
//   timer    CNTV/CNTP: does the virtual timer interrupt land inside the VM, or does it exit?
//   exc      exceptions to VBAR_EL1 with zero host exits; DAIF masking; SError surface
//   exits    the catalogue: which operations silently cost a host exit
//
// Every mode runs in a forked child with its own VM and a watchdog, so one wedged payload does not
// take the sweep down. The host counts hv_vcpu_run returns around instrumented windows; that count
// is the instrument for "does this cost a host exit".
//
// Build:      probes/build.sh probes/el1_features/el1_features.c
// Reproduce:  probes/el1_features/bin/el1_features all

#include <Hypervisor/Hypervisor.h>
#include <inttypes.h>
#include <mach/mach_time.h>
#include <pthread.h>
#include <signal.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/wait.h>
#include <unistd.h>

// ------------------------------------------------------------------------------------------- //
// Guest memory map: 2 MiB of RAM at IPA 0x4000_0000, identity-mapped as one 2 MiB L2 block.
// ------------------------------------------------------------------------------------------- //
#define RAM_IPA   0x40000000ULL
#define RAM_SIZE  (2ULL << 20)

#define OFF_BOOT    0x00000   // boot stub (entry)
#define OFF_PARAMS  0x01000   // parameter block
#define OFF_RESULT  0x02000   // result block (timing rounds land here)
#define OFF_VEC     0x04000   // 16 x 0x80 exception vectors
#define OFF_VEC2    0x05000   // a SECOND vector table, for the VBAR-applies test
#define OFF_L0      0x06000
#define OFF_L1_ID   0x07000
#define OFF_L2_ID   0x08000
#define OFF_L1_T    0x09000
#define OFF_L2_T    0x0a000
#define OFF_L3_T    0x0b000
#define OFF_L0_ALT  0x0c000   // alternate root, for the TTBR0-applies test
#define OFF_L1_ALT  0x0d000
#define OFF_L2_ALT  0x0e000
#define OFF_L3_ALT  0x0f000
#define OFF_STACK   0x10000   // stack grows down from here
#define OFF_DATA_A  0x11000   // mapped RW at VA_A
#define OFF_DATA_B  0x12000   // mapped RO at VA_B
#define OFF_CODE_C  0x13000   // mapped EXEC + GP (guarded, for BTI) at VA_C
#define OFF_CODE    0x20000   // the payload
#define OFF_CODE0   0x21000   // secondary payload, executed at EL0
#define OFF_SCRATCH 0x30000   // 64 KiB scratch, identity-mapped Normal WB RW

// Test-window VAs. All need T0SZ=16 (48-bit VA) to be reachable at all.
#define VA_A        0x0000400000000000ULL   // RW,  AttrIndx=1, nG=1
#define VA_B        0x0000400000001000ULL   // RO,  AttrIndx=1, nG=1
#define VA_C        0x0000400000002000ULL   // EXEC + GP (BTI-guarded), nG=1
#define VA_D        0x0000400000003000ULL   // EL0-executable code page
#define VA_UNMAPPED 0x0000400000100000ULL   // L3 table exists, entry invalid

#define MAGIC_A 0xAAAA0000AAAA0001ULL
#define MAGIC_B 0xBBBB0000BBBB0001ULL

#define GUEST_ASID 0x2aULL

// HVC immediates: the guest<->host protocol.
#define HVC_DONE   0x10   // payload finished; host stops the run
#define HVC_CP     0x11   // checkpoint: host records x0..x7 and resumes
#define HVC_MARK   0x12   // exit-counting window marker (open, then close)
#define HVC_SLOW   0x13   // like CP but the host burns time before re-entering
#define HVC_EXC    0x20   // emitted by the guest's own reporting exception vector

// ------------------------------------------------------------------------------------------- //
// Payload machinery. Each payload is its own .text blob with start/end labels; the host memcpy's
// it to OFF_CODE. Written once per forked child before the first hv_vcpu_run, so no I-cache
// maintenance is needed.
// ------------------------------------------------------------------------------------------- //
__asm__(".section __TEXT,__text\n.p2align 2\n"
        ".arch armv9.2-a+memtag+crypto+crc+dotprod+sve+pauth+bti+flagm+lse\n");

#define PL(name, body)                                                             \
    __asm__(".section __TEXT,__text\n.p2align 2\n"                                 \
            ".globl _p_" #name "_s\n_p_" #name "_s:\n"                             \
            body                                                                   \
            "    hvc #0x10\n"                                                      \
            ".globl _p_" #name "_e\n_p_" #name "_e:\n");                           \
    extern const uint8_t p_##name##_s[], p_##name##_e[]

// Read a system register into x0.
#define RD(name, reg) PL(name, "    mrs x0, " reg "\n")

// Write host-supplied x0, read back into x1; then take a host exit and read again into x2.
// x1 != written  => the register ignored some or all of the bits.
// x2 != x1       => HVF does not preserve it across a host exit.
#define WRX(name, reg) PL(name,                                                    \
    "    mov x3, x0\n"                                                             \
    "    msr " reg ", x0\n"                                                        \
    "    isb\n"                                                                    \
    "    mrs x1, " reg "\n"                                                        \
    "    hvc #0x13\n"                                                              \
    "    mrs x2, " reg "\n"                                                        \
    "    mov x0, x3\n")

// A timed loop. x0 = iterations/round, x1 = rounds, x2 = result pointer, x3..x7 = payload args.
// The body is emitted four times per iteration; the host divides by 4*N. x12..x15 are the loop's;
// bodies may use x3..x11 and x16..x25.
#define TM(name, body) PL(name,                                                    \
    "    mov  x12, x1\n"                                                           \
    "9:  mov  x13, x0\n"                                                           \
    "    isb\n"                                                                    \
    "    mrs  x14, cntvct_el0\n"                                                    \
    "8:  " body body body body                                                     \
    "    subs x13, x13, #1\n"                                                      \
    "    b.ne 8b\n"                                                                \
    "    isb\n"                                                                    \
    "    mrs  x15, cntvct_el0\n"                                                    \
    "    sub  x15, x15, x14\n"                                                     \
    "    str  x15, [x2], #8\n"                                                     \
    "    subs x12, x12, #1\n"                                                      \
    "    b.ne 9b\n")

// One operation, executed 8x inside an exit-counting window.
#define XOP(name, body) PL(name,                                                   \
    "    hvc #0x12\n"                                                              \
    body body body body body body body body                                        \
    "    hvc #0x12\n")

// ------------------------------------------------------------------------------------------- //
// Boot stub. Entered at EL1h with the MMU off (VA == IPA). Installs our 4 KB stage-1 regime,
// enables FP/SIMD, then branches to the payload with x0..x7 from the params block.
// Constants: TCR/MAIR are the ones doc 14 proved work on this host.
// ------------------------------------------------------------------------------------------- //
__asm__(
".section __TEXT,__text\n.p2align 2\n"
".globl _boot_s\n_boot_s:\n"
"    movz x28, #0x4000, lsl #16\n"        // x28 = RAM_IPA
"    add  x27, x28, #1, lsl #12\n"        // x27 = params
"    ldp  x0, x1, [x27, #0]\n"            // MAIR_EL1, TCR_EL1
"    msr  mair_el1, x0\n"
"    msr  tcr_el1,  x1\n"
"    ldp  x0, x1, [x27, #16]\n"           // TTBR0_EL1 (ASID in [63:48]), VBAR_EL1
"    msr  ttbr0_el1, x0\n"
"    msr  vbar_el1,  x1\n"
"    movz x0, #0x30, lsl #16\n"           // CPACR_EL1.FPEN = 0b11
"    msr  cpacr_el1, x0\n"
"    ldr  x2, [x27, #40]\n"               // extra SCTLR_EL1 bits to OR in (0 for most payloads)
"    isb\n"
"    tlbi vmalle1\n"
"    ic   ialluis\n"                       // the host rewrites the payload between runs
"    dsb  nsh\n"
"    isb\n"
"    mrs  x0, sctlr_el1\n"
"    movz x1, #0x1005\n"                  // M | C | I
"    orr  x0, x0, x1\n"
// SPAN=1: do NOT set PSTATE.PAN on exception entry. With SPAN=0 (the reset value here) the first
// exception sets PAN, and under FEAT_PAN3 an EL1 instruction fetch from an EL0-executable page
// then faults -- which turns the vector table itself into an infinite exception loop.
"    orr  x0, x0, #(1 << 23)\n"
"    orr  x0, x0, x2\n"
"    msr  sctlr_el1, x0\n"
"    isb\n"
// The MMU is on from here; every access below goes through our own 4 KB tables.
"    add  x2, x28, #16, lsl #12\n"        // stack top = RAM + 0x10000
"    mov  sp, x2\n"
"    add  x26, x28, #2, lsl #12\n"        // x26 = result block
"    mov  x19, #0\n"                      // x19 = in-guest exception counter
"    ldr  x9, [x27, #32]\n"               // payload entry
"    ldp  x0, x1, [x27, #64]\n"
"    ldp  x2, x3, [x27, #80]\n"
"    ldp  x4, x5, [x27, #96]\n"
"    ldp  x6, x7, [x27, #112]\n"
"    br   x9\n"
".globl _boot_e\n_boot_e:\n"

// --- vector handler A: report to the host and stop. x0..x7 are left alone so a payload's
//     outputs survive the fault.
".p2align 2\n.globl _h_report_s\n_h_report_s:\n"
"    mrs  x20, esr_el1\n"
"    mrs  x21, elr_el1\n"
"    mrs  x22, far_el1\n"
"    mrs  x23, spsr_el1\n"
"    hvc  #0x20\n"
".globl _h_report_e\n_h_report_e:\n"

// --- vector handler B: count it, step over the faulting instruction, return. Entirely in-guest:
//     if this runs N times and the host sees no hv_vcpu_run return, exceptions are free of exits.
".p2align 2\n.globl _h_skip_s\n_h_skip_s:\n"
"    add  x19, x19, #1\n"
"    mrs  x17, elr_el1\n"
"    add  x17, x17, #4\n"
"    msr  elr_el1, x17\n"
"    eret\n"
".globl _h_skip_e\n_h_skip_e:\n"

// --- vector handler C: count and return (for SVC/IRQ, where ELR already points past).
".p2align 2\n.globl _h_count_s\n_h_count_s:\n"
"    add  x19, x19, #1\n"
"    eret\n"
".globl _h_count_e\n_h_count_e:\n"

// --- vector handler D: virtual-timer IRQ. Disable the timer, count, return.
".p2align 2\n.globl _h_vtimer_s\n_h_vtimer_s:\n"
"    msr  cntv_ctl_el0, xzr\n"
"    isb\n"
"    add  x19, x19, #1\n"
"    mrs  x24, cntvct_el0\n"
"    eret\n"
".globl _h_vtimer_e\n_h_vtimer_e:\n"

// --- vector handler E: like B but also records the first syndrome it saw (for BTI etc.)
".p2align 2\n.globl _h_skiprec_s\n_h_skiprec_s:\n"
"    add  x19, x19, #1\n"
"    mrs  x20, esr_el1\n"
"    mrs  x22, far_el1\n"
"    mrs  x17, elr_el1\n"
"    add  x17, x17, #4\n"
"    msr  elr_el1, x17\n"
"    eret\n"
".globl _h_skiprec_e\n_h_skiprec_e:\n"

// --- vector handler F: EL0 SVC. #0x99 is the sentinel that reports out; everything else is
//     counted and returned from without leaving the VM.
".p2align 2\n.globl _h_el0svc_s\n_h_el0svc_s:\n"
"    mrs  x20, esr_el1\n"
"    and  x17, x20, #0xffff\n"
"    cmp  x17, #0x99\n"
"    b.eq 1f\n"
"    add  x19, x19, #1\n"
"    eret\n"
"1:  mrs  x21, elr_el1\n"
"    mrs  x22, far_el1\n"
"    mrs  x23, spsr_el1\n"
"    hvc  #0x20\n"
".globl _h_el0svc_e\n_h_el0svc_e:\n"

// --- the BTI target page, copied to OFF_CODE_C and mapped guarded (GP=1) at VA_C.
//     +0x00: a plain RET       -- NOT a valid indirect-branch landing pad under BTI
//     +0x08: BTI C; RET        -- a valid landing pad
".p2align 2\n.globl _btipage_s\n_btipage_s:\n"
"    nop\n"
"    ret\n"
"    bti  c\n"
"    ret\n"
".globl _btipage_e\n_btipage_e:\n"
);
extern const uint8_t boot_s[], boot_e[];
extern const uint8_t h_report_s[], h_report_e[], h_skip_s[], h_skip_e[];
extern const uint8_t h_count_s[], h_count_e[], h_vtimer_s[], h_vtimer_e[];
extern const uint8_t h_skiprec_s[], h_skiprec_e[];
extern const uint8_t h_el0svc_s[], h_el0svc_e[];
extern const uint8_t btipage_s[], btipage_e[];

// ------------------------------------------------------------------------------------------- //
// 1. System registers
// ------------------------------------------------------------------------------------------- //
RD(rd_currentel, "currentel");
RD(rd_sctlr,     "sctlr_el1");
RD(rd_tcr,       "tcr_el1");
RD(rd_ttbr0,     "ttbr0_el1");
RD(rd_ttbr1,     "ttbr1_el1");
RD(rd_mair,      "mair_el1");
RD(rd_amair,     "amair_el1");
RD(rd_vbar,      "vbar_el1");
RD(rd_contextidr,"contextidr_el1");
RD(rd_tpidr_el0, "tpidr_el0");
RD(rd_tpidrro,   "tpidrro_el0");
RD(rd_tpidr_el1, "tpidr_el1");
RD(rd_par,       "par_el1");
RD(rd_actlr,     "actlr_el1");
RD(rd_cpacr,     "cpacr_el1");
RD(rd_esr,       "esr_el1");
RD(rd_far,       "far_el1");
RD(rd_isr,       "isr_el1");
RD(rd_disr,      "disr_el1");
RD(rd_rvbar,     "rvbar_el1");
RD(rd_afsr0,     "afsr0_el1");
RD(rd_afsr1,     "afsr1_el1");
RD(rd_mdscr,     "mdscr_el1");
RD(rd_oslsr,     "oslsr_el1");
RD(rd_dbgbvr0,   "dbgbvr0_el1");
RD(rd_dbgwcr0,   "dbgwcr0_el1");
RD(rd_mdccint,   "mdccint_el1");
RD(rd_cntkctl,   "cntkctl_el1");
RD(rd_sp_el0,    "sp_el0");
RD(rd_elr,       "elr_el1");
RD(rd_spsr,      "spsr_el1");
RD(rd_vpidr,     "S3_4_c0_c0_0");     // VPIDR_EL2  -- an EL2 register, must not be reachable
RD(rd_hcr,       "S3_4_c1_c1_0");     // HCR_EL2    -- ditto
RD(rd_scr,       "S3_6_c1_c1_0");     // SCR_EL3    -- ditto
RD(rd_ttbr0_el2, "S3_4_c2_c0_0");     // TTBR0_EL2  -- ditto

// ID registers (read only; these are evidence about the ADVERTISEMENT, not about behaviour).
RD(rd_mmfr0,  "id_aa64mmfr0_el1");
RD(rd_mmfr1,  "id_aa64mmfr1_el1");
RD(rd_mmfr2,  "id_aa64mmfr2_el1");
RD(rd_isar0,  "id_aa64isar0_el1");
RD(rd_isar1,  "id_aa64isar1_el1");
RD(rd_isar2,  "S3_0_c0_c6_2");
RD(rd_pfr0,   "id_aa64pfr0_el1");
RD(rd_pfr1,   "id_aa64pfr1_el1");
RD(rd_dfr0,   "id_aa64dfr0_el1");
RD(rd_zfr0,   "S3_0_c0_c4_4");        // ID_AA64ZFR0_EL1 (SVE)
RD(rd_smfr0,  "S3_0_c0_c4_5");        // ID_AA64SMFR0_EL1 (SME)
RD(rd_ctr,    "ctr_el0");
RD(rd_dczid,  "dczid_el0");
RD(rd_midr,   "midr_el1");
RD(rd_mpidr,  "mpidr_el1");
RD(rd_aidr,   "aidr_el1");
RD(rd_revidr, "revidr_el1");
RD(rd_clidr,  "clidr_el1");

// PMU. The corpus (doc 16) says PMCCNTR_EL0 traps to EL2; confirm or refute, and map the surface.
RD(rd_pmccntr,   "pmccntr_el0");
RD(rd_pmcr,      "pmcr_el0");
RD(rd_pmuserenr, "pmuserenr_el0");
RD(rd_pmselr,    "pmselr_el0");
RD(rd_pmcntenset,"pmcntenset_el0");
RD(rd_pmceid0,   "pmceid0_el0");
RD(rd_pmevcntr0, "pmevcntr0_el0");
RD(rd_pmintenset,"pmintenset_el1");
RD(rd_pmccfiltr, "pmccfiltr_el0");

// Apple IMPDEF. Reads only -- a write to an unknown IMPDEF control register on a real Apple core
// is not a safe experiment. ACTLR_EL1 is the exception and is written below.
RD(rd_apl_hid0,   "S3_0_c15_c0_0");
RD(rd_apl_hid1,   "S3_0_c15_c1_0");
RD(rd_apl_hid4,   "S3_0_c15_c4_0");
RD(rd_apl_hid5,   "S3_0_c15_c5_0");
RD(rd_apl_hid11,  "S3_0_c15_c13_0");
RD(rd_apl_pmcr0,  "S3_1_c15_c0_0");
RD(rd_apl_pmcr1,  "S3_1_c15_c1_0");
RD(rd_apl_pmesr0, "S3_1_c15_c5_0");
RD(rd_apl_pmc0,   "S3_2_c15_c0_0");
RD(rd_apl_pmc1,   "S3_2_c15_c1_0");
RD(rd_apl_lsu_err,"S3_3_c15_c0_0");
RD(rd_apl_fp,     "S3_6_c15_c2_5");
RD(rd_apl_c8_3,   "S3_6_c15_c8_3");
RD(rd_apl_ctrr,   "S3_4_c15_c2_0");   // APRR/CTRR-ish, EL2-flavoured: expect unreachable

// Write + read back + survive a host exit.
WRX(wr_tpidr_el1,  "tpidr_el1");
WRX(wr_tpidr_el0,  "tpidr_el0");
WRX(wr_tpidrro,    "tpidrro_el0");
WRX(wr_contextidr, "contextidr_el1");
WRX(wr_amair,      "amair_el1");
WRX(wr_par,        "par_el1");
WRX(wr_actlr,      "actlr_el1");
WRX(wr_ttbr1,      "ttbr1_el1");
WRX(wr_afsr0,      "afsr0_el1");
WRX(wr_cntkctl,    "cntkctl_el1");
WRX(wr_mdscr,      "mdscr_el1");
WRX(wr_dbgbvr0,    "dbgbvr0_el1");
WRX(wr_cntv_cval,  "cntv_cval_el0");
WRX(wr_cntp_cval,  "cntp_cval_el0");
WRX(wr_pmuserenr,  "pmuserenr_el0");
WRX(wr_pmcr,       "pmcr_el0");
WRX(wr_vbar,       "vbar_el1");
WRX(wr_cpacr,      "cpacr_el1");
WRX(wr_apl_fp,     "S3_6_c15_c2_5");

// ------------------------------------------------------------------------------------------- //
// 2. "It was written" vs "it applies"
// ------------------------------------------------------------------------------------------- //

// TCR_EL1.EPD0=1 disables translation-table walks via TTBR0. If TCR applies, the next load from
// VA_A must take a LEVEL-0 TRANSLATION FAULT even though the mapping is still present and valid.
// x0 = VA_A.
PL(ap_tcr_epd0,
"    ldr  x1, [x0]\n"                      // control: works before
"    mrs  x2, tcr_el1\n"
"    orr  x3, x2, #(1 << 7)\n"             // EPD0
"    msr  tcr_el1, x3\n"
"    isb\n"
"    tlbi vmalle1\n"
"    dsb  nsh\n"
"    isb\n"
"    hvc  #0x11\n"                         // checkpoint: x1 = value read while EPD0=0
"    ldr  x4, [x0]\n"                      // MUST fault if TCR_EL1 is honoured
"    mov  x0, x4\n");

// TTBR0_EL1 switched to a second root that maps VA_A onto DATA_B. If TTBR0 applies, the same VA
// now reads MAGIC_B.  x0 = VA_A, x1 = alternate TTBR0 value.
PL(ap_ttbr0,
"    ldr  x2, [x0]\n"                      // through the original root -> MAGIC_A
"    msr  ttbr0_el1, x1\n"
"    isb\n"
"    tlbi vmalle1\n"
"    dsb  nsh\n"
"    isb\n"
"    ldr  x3, [x0]\n"                      // through the alternate root -> MAGIC_B
"    mov  x0, x2\n"
"    mov  x1, x3\n");

// MAIR_EL1: the test pages use AttrIndx=1. Rewriting attr1 from Normal-WB to Device-nGnRnE must
// make an unaligned access to VA_A fault (Device memory forbids unaligned access), while the
// identity-mapped code at AttrIndx=0 keeps running.  x0 = VA_A, x1 = new MAIR.
PL(ap_mair_device,
"    add  x2, x0, #1\n"
"    ldr  x3, [x2]\n"                      // control: unaligned load succeeds on Normal memory
"    msr  mair_el1, x1\n"
"    isb\n"
"    tlbi vmalle1\n"
"    dsb  nsh\n"
"    isb\n"
"    hvc  #0x11\n"
"    ldr  x4, [x2]\n"                      // MUST alignment-fault if MAIR applies
"    mov  x0, x4\n");

// SCTLR_EL1.A (alignment check) -- another bit of SCTLR that is not M. x0 = VA_A.
PL(ap_sctlr_a,
"    add  x2, x0, #1\n"
"    ldr  x3, [x2]\n"                      // control: unaligned load allowed with A=0
"    mrs  x4, sctlr_el1\n"
"    orr  x4, x4, #2\n"                    // A
"    msr  sctlr_el1, x4\n"
"    isb\n"
"    mrs  x5, sctlr_el1\n"
"    mov  x1, x5\n"
"    hvc  #0x11\n"
"    ldr  x6, [x2]\n"                      // MUST alignment-fault if SCTLR.A applies
"    mov  x0, x6\n");

// VBAR_EL1 moved to a second table whose handler reports a different marker. x0 = VBAR2.
PL(ap_vbar,
"    msr  vbar_el1, x0\n"
"    isb\n"
"    mrs  x1, vbar_el1\n"
"    svc  #0\n"                            // -> whichever table VBAR now points at
"    hvc  #0x10\n");

// TPIDR_EL0 / TPIDRRO_EL0 seen from guest EL0, and a write to TPIDRRO_EL0 from EL0 (must undef).
// x0 = EL0 entry.
PL(ap_el0_drop,
"    movz x2, #0x1111, lsl #16\n"
"    msr  tpidr_el0, x2\n"
"    movz x3, #0x2222, lsl #16\n"
"    msr  tpidrro_el0, x3\n"
"    msr  elr_el1, x0\n"
"    movz x2, #0x3c0\n"                    // SPSR = EL0t, DAIF masked
"    msr  spsr_el1, x2\n"
"    eret\n");
PL(ap_el0_body,
"    mrs  x0, tpidr_el0\n"                 // expect 0x11110000
"    mrs  x1, tpidrro_el0\n"               // expect 0x22220000
"    mrs  x3, cntvct_el0\n"                // is the counter readable at EL0?
"    mov  x2, #0\n"
"    msr  tpidrro_el0, x0\n"               // must raise an undef into the EL1 vector
"    mov  x2, #0xbad\n");

// ------------------------------------------------------------------------------------------- //
// 3. FEAT_* by executed instruction
// ------------------------------------------------------------------------------------------- //

// FEAT_PAuth. SCTLR_EL1.EnIA gates the A-key instructions: with EnIA=0 a PACIA is a NOP, which is
// the archetypal "executed but silently ignored". x0 = pointer to sign, x1 = modifier.
PL(feat_pauth,
"    mrs  x9, sctlr_el1\n"
"    bic  x9, x9, #(1 << 31)\n"            // EnIA = 0
"    msr  sctlr_el1, x9\n"
"    isb\n"
"    mov  x2, x0\n"
"    pacia x2, x1\n"                       // expected: NOP  -> x2 == x0
"    mrs  x9, sctlr_el1\n"
"    orr  x9, x9, #(1 << 31)\n"            // EnIA = 1
"    msr  sctlr_el1, x9\n"
"    isb\n"
"    mov  x3, x0\n"
"    pacia x3, x1\n"                       // expected: signed -> x3 != x0
"    mov  x4, x3\n"
"    autia x4, x1\n"                       // expected: authenticates back to x0
"    mov  x5, x3\n"
"    eor  x5, x5, #(1 << 50)\n"            // corrupt the PAC
"    autia x5, x1\n"                       // expected: poisoned pointer, not x0
"    pacga x6, x0, x1\n"                   // generic key -- no enable bit
"    mrs  x7, sctlr_el1\n"
"    mov  x0, x2\n"
"    mov  x1, x3\n"
"    mov  x2, x4\n"
"    mov  x3, x5\n");

// APIAKeyLo/Hi_EL1 writable from EL1, and does the key actually feed the PAC?
// x0 = pointer, x1 = key low word.
PL(feat_pauth_key,
"    mrs  x9, sctlr_el1\n"
"    orr  x9, x9, #(1 << 31)\n"
"    msr  sctlr_el1, x9\n"
"    msr  apiakeylo_el1, xzr\n"
"    msr  apiakeyhi_el1, xzr\n"
"    isb\n"
"    mov  x2, x0\n"
"    paciza x2\n"                          // PAC under the all-zero key
"    msr  apiakeylo_el1, x1\n"
"    msr  apiakeyhi_el1, x1\n"
"    isb\n"
"    mov  x3, x0\n"
"    paciza x3\n"                          // PAC under the new key -- must differ
"    hvc  #0x13\n"                         // host exit: is the key context-switched?
"    mov  x4, x0\n"
"    paciza x4\n"                          // same key still in force?  must equal x3
"    mov  x0, x2\n"
"    mov  x1, x3\n"
"    mov  x2, x4\n");

// FEAT_BTI. Guarded page at VA_C, SCTLR_EL1.BT1=1. A BLR to a non-landing-pad instruction must
// raise a Branch Target Exception (EC=0x0D). x0 = VA_C (plain RET), x1 = VA_C+8 (BTI C; RET).
PL(feat_bti,
"    mrs  x9, sctlr_el1\n"
"    orr  x9, x9, #(1 << 36)\n"            // BT1: guarded pages checked at EL1
"    msr  sctlr_el1, x9\n"
"    isb\n"
"    mrs  x10, sctlr_el1\n"
"    mov  x20, xzr\n"
"    mov  x9, x1\n"
"    blr  x9\n"                            // to a real BTI C landing pad: must NOT fault
"    mov  x2, #1\n"
"    mov  x9, x0\n"
"    blr  x9\n"                            // to a plain RET in a guarded page: must fault
"    mov  x3, #1\n"
"    mov  x0, x10\n"
"    mov  x1, x20\n");

// FEAT_LSE, LSE2, atomics.
PL(feat_lse,
"    mov  x6, #0x55\n"
"    str  x6, [x0]\n"
"    dmb  ish\n"
"    mov  x2, #0x55\n"
"    mov  x4, #0x66\n"
"    casal x2, x4, [x0]\n"                 // CAS succeeds -> memory becomes 0x66
"    ldr  x3, [x0]\n"
"    add  x18, x0, #8\n"
"    mov  x5, #7\n"
"    str  x5, [x18]\n"
"    dmb  ish\n"
"    mov  x5, #5\n"
"    ldaddal x5, x6, [x18]\n"              // 7 += 5
"    ldr  x5, [x18]\n"
"    add  x18, x0, #16\n"
"    mov  x7, #0x1111\n"
"    mov  x8, #0x2222\n"
"    stp  x7, x8, [x18]\n"
"    dmb  ish\n"
"    mov  x10, #0x1111\n"
"    mov  x11, #0x2222\n"
"    mov  x16, #0x3333\n"
"    mov  x17, #0x4444\n"
"    caspal x10, x11, x16, x17, [x18]\n"
"    ldp  x10, x11, [x18]\n"
"    mov  x0, x2\n"                        // old value from CAS
"    mov  x1, x3\n"                        // memory after CAS
"    mov  x2, x6\n"                        // old value from LDADD
"    mov  x3, x5\n"                        // memory after LDADD
"    mov  x4, x10\n"                       // CASP result lo
"    mov  x5, x11\n");                     // CASP result hi

// FEAT_LSE2: unaligned ordinary accesses that cross an 8-byte and a 16-byte boundary.
PL(feat_lse2,
"    ldr  x1, [x0, #4]\n"                  // crosses the 8-byte boundary
"    ldr  x2, [x0, #12]\n"                 // crosses the 16-byte boundary
"    ldar x3, [x0]\n"
"    add  x4, x0, #8\n"
"    mov  x6, #0\n"
"    mov  x7, #0\n"
"    mov  x10, #0x3333\n"
"    mov  x11, #0x4444\n"
"    caspal x6, x7, x10, x11, [x4]\n"      // 8-byte-aligned CASP: must fault (alignment)
"    mov  x0, #0xdead\n");

// FlagM / FlagM2 / JSCVT / CRC32 / DotProd / DIT / SSBS / MTE / SVE.
PL(feat_flagm,
"    msr  nzcv, x0\n"
"    cfinv\n"
"    mrs  x1, nzcv\n"
"    msr  nzcv, xzr\n"
"    rmif x0, #4, #0xf\n"
"    mrs  x2, nzcv\n"
"    msr  nzcv, xzr\n"
"    setf8 w0\n"
"    mrs  x3, nzcv\n"
"    msr  nzcv, xzr\n"
"    setf16 w0\n"
"    mrs  x4, nzcv\n"
"    msr  nzcv, x0\n"
"    axflag\n"
"    mrs  x5, nzcv\n"
"    msr  nzcv, x0\n"
"    xaflag\n"
"    mrs  x6, nzcv\n"
"    mov  x0, x1\n"
"    mov  x1, x2\n"
"    mov  x2, x3\n"
"    mov  x3, x4\n"
"    mov  x4, x5\n"
"    mov  x5, x6\n");
PL(feat_jscvt,
"    fmov d0, x0\n"
"    fjcvtzs w1, d0\n"
"    mrs  x2, nzcv\n"
"    mov  x0, x1\n"
"    mov  x1, x2\n");
PL(feat_crc32,
"    crc32x  w2, w0, x1\n"
"    crc32cx w3, w0, x1\n"
"    crc32b  w4, w0, w1\n"
"    mov  x0, x2\n"
"    mov  x1, x3\n"
"    mov  x2, x4\n");
PL(feat_dotprod,
"    dup  v1.4s, w0\n"
"    dup  v2.4s, w1\n"
"    movi v0.4s, #0\n"
"    udot v0.4s, v1.16b, v2.16b\n"
"    sdot v3.4s, v1.16b, v2.16b\n"
"    mov  x0, v0.d[0]\n"
"    mov  x1, v0.d[1]\n"
"    mov  x2, v3.d[0]\n");
PL(feat_dit,
"    msr  dit, #1\n"
"    mrs  x0, dit\n"
"    msr  dit, #0\n"
"    mrs  x1, dit\n");
PL(feat_ssbs,
"    msr  ssbs, #1\n"
"    mrs  x0, ssbs\n"
"    msr  ssbs, #0\n"
"    mrs  x1, ssbs\n");
PL(feat_mte_tco,
"    msr  tco, #1\n"
"    mrs  x0, tco\n"
"    msr  tco, #0\n"
"    mrs  x1, tco\n");
PL(feat_mte_irg,
"    irg  x1, x0\n"
"    addg x2, x0, #16, #1\n"
"    gmi  x3, x0, xzr\n"
"    mov  x0, x1\n"
"    mov  x1, x2\n");
PL(feat_mte_stg,
"    stg  x0, [x0]\n"
"    dsb  ish\n"
"    ldg  x1, [x0]\n"
"    mov  x0, x1\n");
PL(feat_sve_cpacr,
"    mrs  x9, cpacr_el1\n"
"    orr  x9, x9, #(3 << 16)\n"            // ZEN = 0b11
"    msr  cpacr_el1, x9\n"
"    isb\n"
"    mrs  x0, cpacr_el1\n");               // does ZEN stick, or read back 0?
PL(feat_sve_insn,
"    ptrue p0.b\n"
"    mov  x0, #1\n");
PL(feat_rndr,
"    mrs  x0, S3_3_c2_c4_0\n"              // RNDR
"    mrs  x1, nzcv\n");
PL(feat_sb,      "    sb\n    mov x0, #1\n");
PL(feat_dczva,   "    dc zva, x0\n    ldp x1, x2, [x0]\n    mov x0, x1\n");
PL(feat_esb,     "    esb\n    mrs x0, disr_el1\n");
PL(feat_pan,     "    msr pan, #1\n    mrs x0, pan\n    msr pan, #0\n    mrs x1, pan\n");
PL(feat_uao,     "    msr uao, #1\n    mrs x0, uao\n    msr uao, #0\n    mrs x1, uao\n");
PL(feat_ecv,     "    mrs x0, S3_3_c14_c0_6\n    mrs x1, S3_3_c14_c0_5\n"); // CNTVCTSS/CNTPCTSS
PL(feat_lrcpc,   "    ldapr x1, [x0]\n    ldapur x2, [x0, #8]\n    mov x3, #0x99\n"
                 "    stlur x3, [x0, #16]\n    dmb ish\n    ldr x3, [x0, #16]\n"
                 "    mov x0, x1\n    mov x1, x2\n    mov x2, x3\n");

// ------------------------------------------------------------------------------------------- //
// 4. AT (address translation) instructions -- correctness, then latency
// ------------------------------------------------------------------------------------------- //
// x0 = VA_A (RW), x1 = VA_B (RO), x2 = unmapped VA.
PL(at_check,
"    at   s1e1r, x0\n    isb\n    mrs x3, par_el1\n"
"    at   s1e1w, x0\n    isb\n    mrs x4, par_el1\n"
"    at   s1e1w, x1\n    isb\n    mrs x5, par_el1\n"   // RO page, write probe -> must report a fault
"    at   s1e1r, x2\n    isb\n    mrs x6, par_el1\n"   // unmapped -> must report a fault
"    at   s1e0r, x0\n    isb\n    mrs x7, par_el1\n"   // EL1-only page probed as EL0 -> fault
"    mov  x0, x3\n    mov x1, x4\n    mov x2, x5\n"
"    mov  x3, x6\n    mov x4, x7\n");
PL(at_check_pan,
"    at   s1e1rp, x0\n   isb\n    mrs x3, par_el1\n"
"    at   s1e1wp, x0\n   isb\n    mrs x4, par_el1\n"
"    at   s1e0w, x0\n    isb\n    mrs x5, par_el1\n"
"    msr  pan, #1\n      isb\n"
"    at   s1e1rp, x0\n   isb\n    mrs x6, par_el1\n"   // with PSTATE.PAN=1 the EL0-accessible
"    at   s1e1r, x0\n    isb\n    mrs x7, par_el1\n"   // qualifier changes the answer
"    msr  pan, #0\n"
"    mov  x0, x3\n    mov x1, x4\n    mov x2, x5\n"
"    mov  x3, x6\n    mov x4, x7\n");

TM(tm_ctl,     "");
TM(tm_isb,     "    isb\n");
TM(tm_dsbish,  "    dsb ish\n");
TM(tm_cntvct,  "    isb\n    mrs x9, cntvct_el0\n");
TM(tm_cntvct_noisb, "    mrs x9, cntvct_el0\n");
TM(tm_at_r,    "    at s1e1r, x3\n    isb\n    mrs x9, par_el1\n");
TM(tm_at_w,    "    at s1e1w, x3\n    isb\n    mrs x9, par_el1\n");
TM(tm_at_rp,   "    at s1e1rp, x3\n   isb\n    mrs x9, par_el1\n");
TM(tm_at_wp,   "    at s1e1wp, x3\n   isb\n    mrs x9, par_el1\n");
TM(tm_at_e0r,  "    at s1e0r, x3\n    isb\n    mrs x9, par_el1\n");
TM(tm_at_e0w,  "    at s1e0w, x3\n    isb\n    mrs x9, par_el1\n");
TM(tm_at_bare, "    at s1e1r, x3\n");
TM(tm_at_noisb,"    at s1e1r, x3\n    mrs x9, par_el1\n");
TM(tm_at_fail, "    at s1e1r, x5\n    isb\n    mrs x9, par_el1\n");   // unmapped VA
TM(tm_at_ro,   "    at s1e1w, x4\n    isb\n    mrs x9, par_el1\n");   // permission-failing probe
TM(tm_ldr,     "    ldr x9, [x3]\n");
TM(tm_par_rd,  "    isb\n    mrs x9, par_el1\n");

// TLBI cost. x3 = VA_A, x6 = TLBI operand for VA forms (VA[55:12]), x7 = ASID operand.
TM(tm_tlbi_vmalle1,   "    tlbi vmalle1\n     dsb ish\n    isb\n");
TM(tm_tlbi_vmalle1is, "    tlbi vmalle1is\n   dsb ish\n    isb\n");
TM(tm_tlbi_vae1,      "    tlbi vae1, x6\n    dsb ish\n    isb\n");
TM(tm_tlbi_vae1is,    "    tlbi vae1is, x6\n  dsb ish\n    isb\n");
TM(tm_tlbi_vaae1,     "    tlbi vaae1, x6\n   dsb ish\n    isb\n");
TM(tm_tlbi_vaae1is,   "    tlbi vaae1is, x6\n dsb ish\n    isb\n");
TM(tm_tlbi_vale1,     "    tlbi vale1, x6\n   dsb ish\n    isb\n");
TM(tm_tlbi_vale1is,   "    tlbi vale1is, x6\n dsb ish\n    isb\n");
TM(tm_tlbi_aside1,    "    tlbi aside1, x7\n  dsb ish\n    isb\n");
TM(tm_tlbi_aside1is,  "    tlbi aside1is, x7\n dsb ish\n   isb\n");
TM(tm_tlbi_nosync,    "    tlbi vae1is, x6\n");
TM(tm_barrier,        "    dsb ish\n    isb\n");

// Does a TLBI variant APPLY? Touch VA_A (caching a level-3 entry), demote the leaf descriptor to
// read-only behind the TLB's back, invalidate with ONE variant, then store again. If the variant
// really invalidated the entry, the store takes a permission fault (reported by h_report).
// If it did not, the store silently succeeds through the stale entry and we reach hvc #0x10.
//   x0 = VA_A, x1 = IPA of the L3 entry, x2 = replacement RO descriptor,
//   x6 = VA operand, x7 = ASID operand
#define TLBI_SEM(name, insn) PL(name,                                              \
"    mov  x9, #0x5a\n"                                                             \
"    str  x9, [x0]\n"                                                              \
"    str  x2, [x1]\n"                                                              \
"    dsb  ishst\n"                                                                 \
    insn                                                                           \
"    dsb  ish\n"                                                                   \
"    isb\n"                                                                        \
"    hvc  #0x11\n"                                                                 \
"    str  x9, [x0]\n"                                                              \
"    mov  x0, #0\n")
TLBI_SEM(sem_none,      "    nop\n");
TLBI_SEM(sem_vmalle1,   "    tlbi vmalle1\n");
TLBI_SEM(sem_vmalle1is, "    tlbi vmalle1is\n");
TLBI_SEM(sem_vae1,      "    tlbi vae1, x6\n");
TLBI_SEM(sem_vae1is,    "    tlbi vae1is, x6\n");
TLBI_SEM(sem_vaae1,     "    tlbi vaae1, x6\n");
TLBI_SEM(sem_vaae1is,   "    tlbi vaae1is, x6\n");
TLBI_SEM(sem_vale1,     "    tlbi vale1, x6\n");
TLBI_SEM(sem_vale1is,   "    tlbi vale1is, x6\n");
TLBI_SEM(sem_aside1,    "    tlbi aside1, x7\n");
TLBI_SEM(sem_aside1is,  "    tlbi aside1is, x7\n");
TLBI_SEM(sem_aside_bad, "    tlbi aside1is, x5\n");   // deliberately the WRONG ASID
TLBI_SEM(sem_vae1_bad,  "    tlbi vae1is, x4\n");     // deliberately the WRONG VA
TLBI_SEM(sem_rvae1is,   "    tlbi rvae1is, x6\n");    // FEAT_TLBIRANGE
TLBI_SEM(sem_vmalle1os, "    tlbi vmalle1os\n");      // FEAT_TLBIOS

// ------------------------------------------------------------------------------------------- //
// 5. In-guest clock characterisation
// ------------------------------------------------------------------------------------------- //
// x0 = number of samples, x2 = result pointer. Records raw CNTVCT samples for the host to analyse.
PL(clk_samples,
"    mov  x13, x0\n"
"    mov  x14, x2\n"
"1:  mrs  x9, cntvct_el0\n"
"    str  x9, [x14], #8\n"
"    subs x13, x13, #1\n"
"    b.ne 1b\n"
"    mrs  x0, cntfrq_el0\n"
"    mrs  x1, cntvct_el0\n");

// A dependent-ADD chain: gives an independent estimate of the guest clock so ns can be turned
// into cycles without a cycle counter. x0 = iterations, x2 = results.
PL(clk_ghz,
"    mov  x13, x0\n"
"    isb\n"
"    mrs  x14, cntvct_el0\n"
"1:  add  x9, x9, #1\n    add x9, x9, #1\n    add x9, x9, #1\n    add x9, x9, #1\n"
"    add  x9, x9, #1\n    add x9, x9, #1\n    add x9, x9, #1\n    add x9, x9, #1\n"
"    add  x9, x9, #1\n    add x9, x9, #1\n    add x9, x9, #1\n    add x9, x9, #1\n"
"    add  x9, x9, #1\n    add x9, x9, #1\n    add x9, x9, #1\n    add x9, x9, #1\n"
"    subs x13, x13, #1\n"
"    b.ne 1b\n"
"    isb\n"
"    mrs  x15, cntvct_el0\n"
"    sub  x0, x15, x14\n");

// PMCCNTR_EL0 in a window: does reading it cost a host exit, and does it advance?
PL(clk_pmccntr,
"    hvc  #0x12\n"
"    mrs  x0, pmccntr_el0\n"
"    mov  x13, #4096\n"
"1:  subs x13, x13, #1\n"
"    b.ne 1b\n"
"    mrs  x1, pmccntr_el0\n"
"    hvc  #0x12\n"
"    sub  x1, x1, x0\n");

// Apple's IMPDEF cycle counter PMC0, same question.
PL(clk_apl_pmc0,
"    hvc  #0x12\n"
"    mrs  x0, S3_2_c15_c0_0\n"
"    mov  x13, #4096\n"
"1:  subs x13, x13, #1\n"
"    b.ne 1b\n"
"    mrs  x1, S3_2_c15_c0_0\n"
"    hvc  #0x12\n"
"    sub  x1, x1, x0\n");

// ------------------------------------------------------------------------------------------- //
// 6. Timers
// ------------------------------------------------------------------------------------------- //
// Arm the virtual timer for x0 ticks from now, unmask IRQs, and spin waiting for x19 (the IRQ
// handler's counter) to move. The host counts hv_vcpu_run returns across the whole wait.
//   x0 = TVAL, x1 = spin budget
PL(timer_vtimer,
"    hvc  #0x12\n"                          // open the exit-counting window
"    mrs  x10, cntvct_el0\n"
"    msr  cntv_tval_el0, x0\n"
"    mov  x9, #1\n"
"    msr  cntv_ctl_el0, x9\n"               // ENABLE=1, IMASK=0
"    isb\n"
"    msr  daifclr, #2\n"                    // unmask IRQ
"    mov  x13, x1\n"
"1:  cmp  x19, #0\n"
"    b.ne 2f\n"
"    subs x13, x13, #1\n"
"    b.ne 1b\n"
"2:  msr  daifset, #2\n"
"    mrs  x11, cntv_ctl_el0\n"
"    mrs  x12, cntvct_el0\n"
"    hvc  #0x12\n"                          // close the window
"    mov  x0, x19\n"                        // how many IRQs the GUEST's own vector took
"    sub  x1, x24, x10\n"                   // ticks from arming to the handler's timestamp
"    mov  x2, x11\n"                        // CNTV_CTL at the end (ISTATUS?)
"    sub  x3, x12, x10\n");

// Same, but with IRQs left masked at PSTATE. Does HVF still exit?
PL(timer_vtimer_masked,
"    hvc  #0x12\n"
"    msr  cntv_tval_el0, x0\n"
"    mov  x9, #1\n"
"    msr  cntv_ctl_el0, x9\n"
"    isb\n"
"    mov  x13, x1\n"
"1:  subs x13, x13, #1\n"
"    b.ne 1b\n"
"    mrs  x2, cntv_ctl_el0\n"
"    mrs  x3, isr_el1\n"
"    msr  cntv_ctl_el0, xzr\n"
"    hvc  #0x12\n"
"    mov  x0, x19\n"
"    mov  x1, x2\n"
"    mov  x2, x3\n");

// The physical timer at EL1, same shape.
PL(timer_ptimer,
"    hvc  #0x12\n"
"    msr  cntp_tval_el0, x0\n"
"    mov  x9, #1\n"
"    msr  cntp_ctl_el0, x9\n"
"    isb\n"
"    msr  daifclr, #2\n"
"    mov  x13, x1\n"
"1:  cmp  x19, #0\n"
"    b.ne 2f\n"
"    subs x13, x13, #1\n"
"    b.ne 1b\n"
"2:  msr  daifset, #2\n"
"    mrs  x2, cntp_ctl_el0\n"
"    msr  cntp_ctl_el0, xzr\n"
"    hvc  #0x12\n"
"    mov  x0, x19\n"
"    mov  x1, x2\n");

// Timer register access costs, inside a counted window.
PL(timer_regs,
"    hvc  #0x12\n"
"    mrs  x0, cntfrq_el0\n"
"    mrs  x1, cntv_ctl_el0\n"
"    mrs  x2, cntv_cval_el0\n"
"    mrs  x3, cntv_tval_el0\n"
"    msr  cntv_cval_el0, xzr\n"
"    mov  x9, #0x1000\n"
"    msr  cntv_tval_el0, x9\n"
"    mrs  x4, cntv_tval_el0\n"
"    msr  cntv_ctl_el0, xzr\n"
"    mrs  x5, cntkctl_el1\n"
"    hvc  #0x12\n");

// Does a host-injected IRQ (hv_vcpu_set_pending_interrupt) reach the guest's own vector, and does
// PSTATE.I actually hold it off?  x0 = spin budget.
PL(irq_masked_spin,
"    hvc  #0x11\n"                          // host injects an IRQ while PSTATE.I = 1
"    hvc  #0x12\n"
"    mov  x13, x0\n"
"1:  subs x13, x13, #1\n"
"    b.ne 1b\n"                             // spin with IRQ masked: must NOT be interrupted
"    mov  x1, x19\n"
"    hvc  #0x12\n"
"    msr  daifclr, #2\n"                    // now unmask: the IRQ should be taken here
"    nop\n    nop\n    nop\n    nop\n"
"    msr  daifset, #2\n"
"    mov  x0, x1\n"
"    mov  x1, x19\n");

// ------------------------------------------------------------------------------------------- //
// 7. Exceptions inside the VM
// ------------------------------------------------------------------------------------------- //
// N synchronous exceptions taken at EL1 into our own VBAR, handled and returned from entirely
// in-guest. The host counts hv_vcpu_run returns across the window: the claim is ZERO.
//   x0 = N, x1 = rounds, x2 = results, x3 = VA of a read-only page (for the abort variant)
PL(exc_svc,
"    hvc  #0x12\n"
"    mov  x12, x1\n"
"9:  mov  x13, x0\n"
"    isb\n    mrs x14, cntvct_el0\n"
"8:  svc  #0\n    svc #0\n    svc #0\n    svc #0\n"
"    subs x13, x13, #1\n    b.ne 8b\n"
"    isb\n    mrs x15, cntvct_el0\n"
"    sub  x15, x15, x14\n    str x15, [x2], #8\n"
"    subs x12, x12, #1\n    b.ne 9b\n"
"    hvc  #0x12\n"
"    mov  x0, x19\n");
PL(exc_abort,
"    hvc  #0x12\n"
"    mov  x12, x1\n"
"9:  mov  x13, x0\n"
"    isb\n    mrs x14, cntvct_el0\n"
"8:  str  x9, [x3]\n    str x9, [x3]\n    str x9, [x3]\n    str x9, [x3]\n"
"    subs x13, x13, #1\n    b.ne 8b\n"
"    isb\n    mrs x15, cntvct_el0\n"
"    sub  x15, x15, x14\n    str x15, [x2], #8\n"
"    subs x12, x12, #1\n    b.ne 9b\n"
"    hvc  #0x12\n"
"    mov  x0, x19\n");
PL(exc_undef,
"    hvc  #0x12\n"
"    mov  x12, x1\n"
"9:  mov  x13, x0\n"
"    isb\n    mrs x14, cntvct_el0\n"
"8:  .inst 0x00000000\n    .inst 0x00000000\n    .inst 0x00000000\n    .inst 0x00000000\n"
"    subs x13, x13, #1\n    b.ne 8b\n"
"    isb\n    mrs x15, cntvct_el0\n"
"    sub  x15, x15, x14\n    str x15, [x2], #8\n"
"    subs x12, x12, #1\n    b.ne 9b\n"
"    hvc  #0x12\n"
"    mov  x0, x19\n");
// Exceptions from EL0 into EL1 (the vector slot at +0x400), also entirely in-guest.
PL(exc_el0_loop,
"    mov  x13, x0\n"
"1:  svc  #0\n"
"    subs x13, x13, #1\n"
"    b.ne 1b\n"
"    svc  #0x99\n");                        // sentinel: handled by a slot that reports out

// DAIF manipulation at EL1.
PL(exc_daif,
"    mrs  x0, daif\n"
"    msr  daifclr, #0xf\n"
"    isb\n"
"    mrs  x1, daif\n"
"    msr  daifset, #0xf\n"
"    isb\n"
"    mrs  x2, daif\n"
"    msr  daifclr, #0x4\n"                  // unmask SError only
"    isb\n"
"    mrs  x3, daif\n"
"    mrs  x4, isr_el1\n"
"    mrs  x5, disr_el1\n"
"    msr  daifset, #0xf\n");

// ------------------------------------------------------------------------------------------- //
// 8. Host-exit catalogue. Each operation is executed 8x inside a counted window.
// ------------------------------------------------------------------------------------------- //
// x3 = VA_A, x4 = scratch VA (identity, Normal WB), x5 = VA operand for TLBI/IC/DC
XOP(x_nop,        "    nop\n");
XOP(x_isb,        "    isb\n");
XOP(x_dsb_sy,     "    dsb sy\n");
XOP(x_dmb_sy,     "    dmb sy\n");
XOP(x_tlbi,       "    tlbi vmalle1is\n    dsb ish\n");
XOP(x_ic_iallu,   "    ic iallu\n    dsb ish\n");
XOP(x_ic_ialluis, "    ic ialluis\n   dsb ish\n");
XOP(x_ic_ivau,    "    ic ivau, x4\n  dsb ish\n");
XOP(x_dc_cvau,    "    dc cvau, x4\n  dsb ish\n");
XOP(x_dc_civac,   "    dc civac, x4\n dsb ish\n");
XOP(x_dc_isw,     "    dc isw, xzr\n  dsb ish\n");
XOP(x_dc_zva,     "    dc zva, x4\n");
XOP(x_at,         "    at s1e1r, x3\n isb\n mrs x9, par_el1\n");
XOP(x_msr_ttbr0,  "    mrs x9, ttbr0_el1\n msr ttbr0_el1, x9\n isb\n");
XOP(x_msr_tcr,    "    mrs x9, tcr_el1\n   msr tcr_el1, x9\n   isb\n");
XOP(x_msr_sctlr,  "    mrs x9, sctlr_el1\n msr sctlr_el1, x9\n isb\n");
XOP(x_msr_vbar,   "    mrs x9, vbar_el1\n  msr vbar_el1, x9\n  isb\n");
XOP(x_msr_mair,   "    mrs x9, mair_el1\n  msr mair_el1, x9\n  isb\n");
XOP(x_msr_ctxidr, "    mrs x9, contextidr_el1\n msr contextidr_el1, x9\n");
XOP(x_msr_tpidr,  "    mrs x9, tpidr_el1\n msr tpidr_el1, x9\n");
XOP(x_msr_actlr,  "    mrs x9, actlr_el1\n msr actlr_el1, x9\n isb\n");
XOP(x_msr_cpacr,  "    mrs x9, cpacr_el1\n msr cpacr_el1, x9\n isb\n");
XOP(x_msr_daif,   "    msr daifclr, #2\n   msr daifset, #2\n");
XOP(x_mrs_cntvct, "    mrs x9, cntvct_el0\n");
XOP(x_mrs_cntpct, "    mrs x9, cntpct_el0\n");
XOP(x_mrs_cntfrq, "    mrs x9, cntfrq_el0\n");
XOP(x_msr_cntv,   "    msr cntv_cval_el0, xzr\n msr cntv_ctl_el0, xzr\n");
XOP(x_mrs_midr,   "    mrs x9, midr_el1\n");
XOP(x_mrs_mmfr0,  "    mrs x9, id_aa64mmfr0_el1\n");
XOP(x_mrs_ctr,    "    mrs x9, ctr_el0\n");
XOP(x_mrs_dczid,  "    mrs x9, dczid_el0\n");
XOP(x_mrs_pmccntr,"    mrs x9, pmccntr_el0\n");
XOP(x_mrs_mdscr,  "    mrs x9, mdscr_el1\n");
XOP(x_msr_oslar,  "    msr oslar_el1, xzr\n");
XOP(x_mrs_dbgbvr, "    mrs x9, dbgbvr0_el1\n");
XOP(x_ldxr_stxr,  "    ldxr x9, [x4]\n     stxr w10, x9, [x4]\n");
XOP(x_casal,      "    ldr x9, [x4]\n      casal x9, x9, [x4]\n");
XOP(x_fp,         "    fmov d0, #1.0\n     fadd d0, d0, d0\n");
XOP(x_wfe,        "    wfe\n");
XOP(x_wfi,        "    wfi\n");
XOP(x_yield,      "    yield\n");
XOP(x_sev,        "    sev\n");
XOP(x_eret_dummy, "    nop\n");
XOP(x_brk,        "    brk #0\n");
XOP(x_smc,        "    smc #0\n");
XOP(x_undef,      "    .inst 0x00000000\n");
XOP(x_hvc,        "    hvc #0x14\n");
XOP(x_svc,        "    svc #0\n");
XOP(x_aborted,    "    ldr x9, [x5]\n");    // faults into the guest's own vector

// ------------------------------------------------------------------------------------------- //
// Host side
// ------------------------------------------------------------------------------------------- //
// Stage-1 descriptor bits, 4 KB granule.
#define D_TABLE  (3ULL << 0)
#define D_PAGE   (3ULL << 0)
#define D_BLOCK  (1ULL << 0)
#define D_ATTR0  (0ULL << 2)
#define D_ATTR1  (1ULL << 2)
#define D_AP_RW  (0ULL << 6)     // EL1 RW, EL0 none
#define D_AP_RW0 (1ULL << 6)     // EL1 RW, EL0 RW
#define D_AP_RO  (2ULL << 6)     // EL1 RO, EL0 none
#define D_AP_RO0 (3ULL << 6)     // EL1 RO, EL0 RO
#define D_SH_IS  (3ULL << 8)
#define D_AF     (1ULL << 10)
#define D_NG     (1ULL << 11)
#define D_GP     (1ULL << 50)
#define D_PXN    (1ULL << 53)
#define D_UXN    (1ULL << 54)

// The TCR/MAIR that doc 14 proved work on this host. MAIR attr0 = Normal WB (the identity map,
// including code), attr1 = Normal WB (the test window, so it can be switched to Device later).
#define TCR_VAL (16ULL | (1ULL<<8) | (1ULL<<10) | (3ULL<<12) | (0ULL<<14) \
                | (16ULL<<16) | (1ULL<<23) | (2ULL<<30) | (2ULL<<32))
#define MAIR_VAL   0x000000000000FFFFULL   // attr0 = 0xFF, attr1 = 0xFF
#define MAIR_DEV1  0x00000000000000FFULL   // attr0 = 0xFF, attr1 = 0x00 (Device-nGnRnE)

enum { H_REPORT = 0, H_SKIP, H_COUNT, H_VTIMER, H_SKIPREC, H_EL0SVC };

enum { OUT_NORESULT = 0, OUT_OK, OUT_TRAP_EL2, OUT_UNDEF_EL1, OUT_EXC_EL1,
       OUT_ODD, OUT_TIMEOUT, OUT_CRASH, OUT_HVERR };

#define NRES 1024

typedef struct {
    int      outcome;
    uint64_t x[8];
    uint64_t cp[8][8];
    uint16_t cpimm[8];
    int      ncp;
    uint64_t esr_el2, pc;
    uint32_t exit_reason;
    uint64_t esr_el1, elr, far, spsr;
    uint64_t x19, x20, x22, x24;
    uint64_t runs, vtimer_exits;
    uint64_t trap_esr[8];
    int      ntrap;
    int      window_exits, windows, window_vtimer;
    uint32_t hv_err;
    int      hv_line;
    uint64_t res[NRES];
    uint64_t cntfrq;
} res_t;

typedef struct {
    const uint8_t *code_s, *code_e;
    const uint8_t *el0_s,  *el0_e;
    uint64_t in[8];
    int      vec_def, vec_cur_sync, vec_cur_irq, vec_low_sync;
    uint64_t sctlr_extra;
    uint64_t mair;
    uint64_t ttbr0_asid;
    int      inject_irq;      // inject an IRQ at the first HVC_CP
    int      skip_traps;      // step over EL2 traps rather than stopping
    int      mask_vtimer;     // call hv_vcpu_set_vtimer_mask on VTIMER exits
    int      timeout_ms;
} cfg_t;

static uint64_t esr_ec(uint64_t esr) { return (esr >> 26) & 0x3f; }

#define CHV(e) do { hv_return_t _r = (e); if (_r != HV_SUCCESS) {                 \
        res->outcome = OUT_HVERR; res->hv_err = _r; res->hv_line = __LINE__;      \
        return; } } while (0)

static const uint8_t *hcode(int h, size_t *len) {
    const uint8_t *s, *e;
    switch (h) {
        case H_SKIP:    s = h_skip_s;    e = h_skip_e;    break;
        case H_COUNT:   s = h_count_s;   e = h_count_e;   break;
        case H_VTIMER:  s = h_vtimer_s;  e = h_vtimer_e;  break;
        case H_SKIPREC: s = h_skiprec_s; e = h_skiprec_e; break;
        case H_EL0SVC:  s = h_el0svc_s;  e = h_el0svc_e;  break;
        default:        s = h_report_s;  e = h_report_e;  break;
    }
    *len = (size_t)(e - s);
    return s;
}

static void install_vectors(uint8_t *mem, const cfg_t *c) {
    size_t l;
    const uint8_t *d = hcode(c->vec_def, &l);
    for (int i = 0; i < 16; i++) memcpy(mem + OFF_VEC + i * 0x80, d, l);
    if (c->vec_cur_sync) { const uint8_t *h = hcode(c->vec_cur_sync, &l);
        memcpy(mem + OFF_VEC + 0x000, h, l); memcpy(mem + OFF_VEC + 0x200, h, l); }
    if (c->vec_cur_irq)  { const uint8_t *h = hcode(c->vec_cur_irq, &l);
        memcpy(mem + OFF_VEC + 0x080, h, l); memcpy(mem + OFF_VEC + 0x280, h, l); }
    if (c->vec_low_sync) { const uint8_t *h = hcode(c->vec_low_sync, &l);
        memcpy(mem + OFF_VEC + 0x400, h, l); }
    // The second table used by the VBAR-applies test always reports, with a distinct marker: it is
    // the H_REPORT handler, so the host tells the two apart by which ELR/vector fired.
    const uint8_t *r = hcode(H_REPORT, &l);
    for (int i = 0; i < 16; i++) memcpy(mem + OFF_VEC2 + i * 0x80, r, l);
}

static void build_tables(uint8_t *mem) {
    uint64_t *l0 = (uint64_t *)(mem + OFF_L0);
    uint64_t *l1_id = (uint64_t *)(mem + OFF_L1_ID), *l2_id = (uint64_t *)(mem + OFF_L2_ID);
    uint64_t *l1_t  = (uint64_t *)(mem + OFF_L1_T),  *l2_t  = (uint64_t *)(mem + OFF_L2_T);
    uint64_t *l3_t  = (uint64_t *)(mem + OFF_L3_T);

    // Identity map of guest RAM as one 2 MiB block: attr0, EL1-only RW, executable at EL1 only.
    // (EL0 gets its own 4 KB code page below; making this block EL0-executable interacts badly
    // with PSTATE.PAN, see the SPAN comment in the boot stub.)
    l0[0]    = (RAM_IPA + OFF_L1_ID) | D_TABLE;
    l1_id[1] = (RAM_IPA + OFF_L2_ID) | D_TABLE;
    l2_id[0] = RAM_IPA | D_BLOCK | D_AF | D_SH_IS | D_AP_RW | D_ATTR0 | D_UXN;

    // Test window under L0[128]: VA_A/VA_B/VA_C, attr1, non-global (so ASID-scoped TLBI can be
    // tested), and VA_UNMAPPED left invalid in an existing L3 table.
    l0[128] = (RAM_IPA + OFF_L1_T) | D_TABLE;
    l1_t[0] = (RAM_IPA + OFF_L2_T) | D_TABLE;
    l2_t[0] = (RAM_IPA + OFF_L3_T) | D_TABLE;
    l3_t[0] = (RAM_IPA + OFF_DATA_A) | D_PAGE | D_AF | D_SH_IS | D_AP_RW  | D_ATTR1 | D_NG | D_UXN | D_PXN;
    l3_t[1] = (RAM_IPA + OFF_DATA_B) | D_PAGE | D_AF | D_SH_IS | D_AP_RO  | D_ATTR1 | D_NG | D_UXN | D_PXN;
    l3_t[2] = (RAM_IPA + OFF_CODE_C) | D_PAGE | D_AF | D_SH_IS | D_AP_RO  | D_ATTR1 | D_NG | D_UXN | D_GP;
    // VA_D: the EL0 code page -- readable and executable at EL0, never fetched from at EL1.
    l3_t[3] = (RAM_IPA + OFF_CODE0)  | D_PAGE | D_AF | D_SH_IS | D_AP_RO0 | D_ATTR1 | D_NG | D_PXN;
    l3_t[256] = 0;                                    // VA_UNMAPPED

    // Alternate root: same identity map, but VA_A resolves to DATA_B.
    uint64_t *l0a = (uint64_t *)(mem + OFF_L0_ALT);
    uint64_t *l1a = (uint64_t *)(mem + OFF_L1_ALT), *l2a = (uint64_t *)(mem + OFF_L2_ALT);
    uint64_t *l3a = (uint64_t *)(mem + OFF_L3_ALT);
    l0a[0]   = l0[0];
    l0a[128] = (RAM_IPA + OFF_L1_ALT) | D_TABLE;
    l1a[0]   = (RAM_IPA + OFF_L2_ALT) | D_TABLE;
    l2a[0]   = (RAM_IPA + OFF_L3_ALT) | D_TABLE;
    l3a[0]   = (RAM_IPA + OFF_DATA_B) | D_PAGE | D_AF | D_SH_IS | D_AP_RW | D_ATTR1 | D_NG | D_UXN | D_PXN;
}

// ------------------------------------------------------------------------------------------- //
// Hypervisor.framework allows exactly one VM per process and does not survive fork(), so the VM
// and its RAM are created once and every probe gets a freshly rebuilt guest image and a fresh
// vCPU. A watchdog thread calls hv_vcpus_exit() so a wedged payload becomes a CANCELED exit
// rather than a hung run.
static uint8_t *g_mem;

typedef struct { hv_vcpu_t vcpu; volatile int done; int ms; } wd_t;
static void *watchdog(void *p) {
    wd_t *w = p;
    for (int i = 0; i < w->ms / 20 && !w->done; i++) usleep(20000);
    if (!w->done) hv_vcpus_exit((hv_vcpu_t *)&w->vcpu, 1);
    return NULL;
}

static void run_child(const cfg_t *c, res_t *res) {
    uint8_t *mem = g_mem;
    memset(mem, 0, RAM_SIZE);
    memcpy(mem + OFF_BOOT, boot_s, (size_t)(boot_e - boot_s));
    memcpy(mem + OFF_CODE, c->code_s, (size_t)(c->code_e - c->code_s));
    if (c->el0_s) memcpy(mem + OFF_CODE0, c->el0_s, (size_t)(c->el0_e - c->el0_s));
    memcpy(mem + OFF_CODE_C, btipage_s, (size_t)(btipage_e - btipage_s));
    install_vectors(mem, c);
    build_tables(mem);

    *(uint64_t *)(mem + OFF_DATA_A) = MAGIC_A;
    *(uint64_t *)(mem + OFF_DATA_B) = MAGIC_B;
    for (int i = 0; i < 512; i++) *(uint64_t *)(mem + OFF_SCRATCH + i * 8) = 0x1000 + i;

    uint64_t *p = (uint64_t *)(mem + OFF_PARAMS);
    p[0] = c->mair ? c->mair : MAIR_VAL;
    p[1] = TCR_VAL;
    p[2] = (RAM_IPA + OFF_L0) | ((c->ttbr0_asid ? c->ttbr0_asid : GUEST_ASID) << 48);
    p[3] = RAM_IPA + OFF_VEC;
    p[4] = RAM_IPA + OFF_CODE;
    p[5] = c->sctlr_extra;
    for (int i = 0; i < 8; i++) p[8 + i] = c->in[i];

    hv_vcpu_t vcpu; hv_vcpu_exit_t *vexit;
    CHV(hv_vcpu_create(&vcpu, &vexit, NULL));
    CHV(hv_vcpu_set_reg(vcpu, HV_REG_PC, RAM_IPA + OFF_BOOT));
    // An HVF vCPU starts at EL0. Without this, the first MRS traps to VBAR+0x400 with EC=0x20.
    CHV(hv_vcpu_set_reg(vcpu, HV_REG_CPSR, 0x3c5));   // EL1h, DAIF masked

    wd_t w = { vcpu, 0, c->timeout_ms ? c->timeout_ms : 8000 };
    pthread_t wt; pthread_create(&wt, NULL, watchdog, &w);

    res->outcome = OUT_NORESULT;
    int window_open = 0;
    for (int iter = 0; iter < 4000; iter++) {
        hv_return_t r = hv_vcpu_run(vcpu);
        res->runs++;
        if (r != HV_SUCCESS) { res->outcome = OUT_HVERR; res->hv_err = r; res->hv_line = __LINE__; break; }
        if (window_open) res->window_exits++;

        if (vexit->reason == HV_EXIT_REASON_VTIMER_ACTIVATED) {
            res->vtimer_exits++;
            if (window_open) res->window_vtimer++;
            if (c->mask_vtimer) hv_vcpu_set_vtimer_mask(vcpu, true);
            continue;
        }
        if (vexit->reason == HV_EXIT_REASON_CANCELED) {
            res->outcome = OUT_TIMEOUT;
            hv_vcpu_get_reg(vcpu, HV_REG_PC, &res->pc);
            hv_vcpu_get_sys_reg(vcpu, HV_SYS_REG_SCTLR_EL1, &res->esr_el2);
            hv_vcpu_get_sys_reg(vcpu, HV_SYS_REG_ESR_EL1, &res->esr_el1);
            hv_vcpu_get_sys_reg(vcpu, HV_SYS_REG_ELR_EL1, &res->elr);
            hv_vcpu_get_sys_reg(vcpu, HV_SYS_REG_FAR_EL1, &res->far);
            break;
        }
        if (vexit->reason != HV_EXIT_REASON_EXCEPTION) {
            res->exit_reason = vexit->reason; res->outcome = OUT_ODD; break;
        }
        uint64_t sy = vexit->exception.syndrome;
        res->esr_el2 = sy;
        hv_vcpu_get_reg(vcpu, HV_REG_PC, &res->pc);

        if (esr_ec(sy) != 0x16) {                       // a real trap out of the VM
            if (res->ntrap < 8) res->trap_esr[res->ntrap++] = sy;
            if (c->skip_traps && esr_ec(sy) != 0x21) {  // can't step over an instruction abort
                hv_vcpu_set_reg(vcpu, HV_REG_PC, res->pc + 4);
                continue;
            }
            res->outcome = OUT_TRAP_EL2;
            break;
        }

        uint64_t imm = sy & 0xffff, regs[8];
        for (int i = 0; i < 8; i++) hv_vcpu_get_reg(vcpu, (hv_reg_t)(HV_REG_X0 + i), &regs[i]);
        memcpy(res->x, regs, sizeof regs);

        if (imm == HVC_MARK) {
            if (!window_open) { window_open = 1; res->window_exits = 0; }
            else { window_open = 0; res->window_exits--; res->windows++; }
            continue;
        }
        if (imm == HVC_CP || imm == HVC_SLOW) {
            if (res->ncp < 8) {
                memcpy(res->cp[res->ncp], regs, sizeof regs);
                res->cpimm[res->ncp] = (uint16_t)imm;
                res->ncp++;
            }
            if (imm == HVC_SLOW) {
                // Force real host work between the write and the read-back.
                volatile double acc = 1.0;
                for (int k = 0; k < 200000; k++) acc = acc * 1.0000001 + 1.0;
                (void)acc;
                usleep(1000);
            }
            if (c->inject_irq && res->ncp == 1)
                hv_vcpu_set_pending_interrupt(vcpu, HV_INTERRUPT_TYPE_IRQ, true);
            continue;
        }
        if (imm == HVC_EXC) {
            hv_vcpu_get_reg(vcpu, HV_REG_X20, &res->esr_el1);
            hv_vcpu_get_reg(vcpu, HV_REG_X21, &res->elr);
            hv_vcpu_get_reg(vcpu, HV_REG_X22, &res->far);
            hv_vcpu_get_reg(vcpu, HV_REG_X23, &res->spsr);
            res->outcome = (esr_ec(res->esr_el1) == 0x00) ? OUT_UNDEF_EL1 : OUT_EXC_EL1;
            break;
        }
        if (imm == HVC_DONE) { res->outcome = OUT_OK; break; }
        // any other HVC immediate (e.g. the exit-catalogue's own hvc #0x14): just resume
    }

    w.done = 1;
    pthread_join(wt, NULL);
    hv_vcpu_get_reg(vcpu, HV_REG_X19, &res->x19);
    hv_vcpu_get_reg(vcpu, HV_REG_X20, &res->x20);
    hv_vcpu_get_reg(vcpu, HV_REG_X22, &res->x22);
    hv_vcpu_get_reg(vcpu, HV_REG_X24, &res->x24);
    memcpy(res->res, mem + OFF_RESULT, sizeof res->res);
    hv_vcpu_destroy(vcpu);
}

static res_t *g_res;

static void run_cfg(const cfg_t *c, res_t *res) {
    memset(res, 0, sizeof *res);
    res->outcome = OUT_NORESULT;
    run_child(c, res);
}

// A payload run with all the usual defaults.
static void run_simple(const uint8_t *s, const uint8_t *e, res_t *res, int vec, ...) {
    cfg_t c; memset(&c, 0, sizeof c);
    c.code_s = s; c.code_e = e; c.vec_def = vec; c.mask_vtimer = 1;
    va_list ap; va_start(ap, vec);
    for (int i = 0; i < 8; i++) c.in[i] = va_arg(ap, uint64_t);
    va_end(ap);
    run_cfg(&c, res);
}
#define RUN(name, res, vec, ...) \
    run_simple(p_##name##_s, p_##name##_e, res, vec, __VA_ARGS__, \
               0ULL,0ULL,0ULL,0ULL,0ULL,0ULL,0ULL,0ULL)

static const char *outcome_str(int o) {
    switch (o) {
        case OUT_OK:        return "OK";
        case OUT_TRAP_EL2:  return "TRAP_EL2";
        case OUT_UNDEF_EL1: return "UNDEF_EL1";
        case OUT_EXC_EL1:   return "EXC_EL1";
        case OUT_ODD:       return "ODD_EXIT";
        case OUT_TIMEOUT:   return "TIMEOUT";
        case OUT_CRASH:     return "CRASH";
        case OUT_HVERR:     return "HV_ERROR";
        default:            return "NORESULT";
    }
}

static const char *ecname(uint64_t ec) {
    switch (ec) {
        case 0x00: return "unknown/undef";
        case 0x01: return "WFx trap";
        case 0x07: return "SIMD/FP access trap";
        case 0x0d: return "branch target (BTI)";
        case 0x15: return "SVC";
        case 0x16: return "HVC";
        case 0x17: return "SMC";
        case 0x18: return "MSR/MRS trap";
        case 0x20: return "instr abort lower EL";
        case 0x21: return "instr abort current EL";
        case 0x22: return "PC alignment";
        case 0x24: return "data abort lower EL";
        case 0x25: return "data abort current EL";
        case 0x26: return "SP alignment";
        case 0x2f: return "SError";
        case 0x3c: return "BRK";
        default:   return "?";
    }
}
static const char *dfsc_str(uint64_t esr) {
    uint64_t f = esr & 0x3f;
    if (f <= 0x03) return "address size fault";
    if (f >= 0x04 && f <= 0x07) return "translation fault";
    if (f >= 0x08 && f <= 0x0b) return "access flag fault";
    if (f >= 0x0c && f <= 0x0f) return "PERMISSION fault";
    if (f == 0x21) return "ALIGNMENT fault";
    if (f == 0x10) return "external abort";
    return "other";
}

// A compact one-line verdict for a probe.
static void verdict(const char *name, const res_t *r, const char *note) {
    printf("  %-22s %-10s", name, outcome_str(r->outcome));
    if (r->outcome == OUT_OK) {
        printf(" x0=%016llx x1=%016llx", (unsigned long long)r->x[0], (unsigned long long)r->x[1]);
    } else if (r->outcome == OUT_TRAP_EL2) {
        printf(" ESR_EL2=%08llx EC=0x%02llx (%s)", (unsigned long long)r->esr_el2,
               (unsigned long long)esr_ec(r->esr_el2), ecname(esr_ec(r->esr_el2)));
    } else if (r->outcome == OUT_UNDEF_EL1 || r->outcome == OUT_EXC_EL1) {
        printf(" ESR_EL1=%08llx EC=0x%02llx (%s)", (unsigned long long)r->esr_el1,
               (unsigned long long)esr_ec(r->esr_el1), ecname(esr_ec(r->esr_el1)));
    } else if (r->outcome == OUT_HVERR) {
        printf(" hv_return=0x%x line %d", r->hv_err, r->hv_line);
    }
    if (note && *note) printf("  %s", note);
    printf("\n");
}

// ------------------------------------------------------------------------------------------- //
// Timing helpers
// ------------------------------------------------------------------------------------------- //
static uint64_t g_cntfrq = 24000000;
static double   g_ghz    = 0.0;

static int dcmp(const void *a, const void *b) {
    double x = *(const double *)a, y = *(const double *)b;
    return x < y ? -1 : x > y ? 1 : 0;
}
typedef struct { double med, lo, hi, min; int n; } stat_t;

static stat_t stat_of(const uint64_t *ticks, int n, double per) {
    double *v = malloc(sizeof(double) * (size_t)n);
    for (int i = 0; i < n; i++) v[i] = (double)ticks[i] * 1e9 / (double)g_cntfrq / per;
    qsort(v, (size_t)n, sizeof(double), dcmp);
    stat_t s = { v[n / 2], v[n / 10], v[n - 1 - n / 10], v[0], n };
    free(v);
    return s;
}

// Run a TM() payload: N iterations x 4 bodies per round, R rounds. Returns per-body ns.
static stat_t timed(const uint8_t *s, const uint8_t *e, uint64_t N, uint64_t R,
                    uint64_t a3, uint64_t a4, uint64_t a5, uint64_t a6, uint64_t a7,
                    res_t *res, int *ok) {
    cfg_t c; memset(&c, 0, sizeof c);
    c.code_s = s; c.code_e = e; c.vec_def = H_REPORT; c.mask_vtimer = 1; c.timeout_ms = 30000;
    c.in[0] = N; c.in[1] = R; c.in[2] = RAM_IPA + OFF_RESULT;
    c.in[3] = a3; c.in[4] = a4; c.in[5] = a5; c.in[6] = a6; c.in[7] = a7;
    run_cfg(&c, res);
    *ok = (res->outcome == OUT_OK);
    stat_t z = { 0, 0, 0, 0, 0 };
    if (!*ok) return z;
    return stat_of(res->res, (int)R, (double)(N * 4));
}
#define TIMED(name, N, R, a3, a4, a5, a6, a7, res, ok) \
    timed(p_##name##_s, p_##name##_e, N, R, a3, a4, a5, a6, a7, res, ok)

static void trow(const char *label, stat_t s, double base, int ok, const res_t *r) {
    if (!ok) { printf("  %-38s  %s\n", label, outcome_str(r->outcome)); return; }
    double net = s.med - base;
    if (g_ghz > 0)
        printf("  %-38s %8.2f ns  [%7.2f .. %7.2f]  net %8.2f ns  %7.1f cyc\n",
               label, s.med, s.lo, s.hi, net, net * g_ghz);
    else
        printf("  %-38s %8.2f ns  [%7.2f .. %7.2f]  net %8.2f ns\n",
               label, s.med, s.lo, s.hi, net);
}

// ------------------------------------------------------------------------------------------- //
// Mode: regs
// ------------------------------------------------------------------------------------------- //
typedef struct { const char *name; const uint8_t *s, *e; int kind; uint64_t val; const char *note; }
        sreg_t;
#define R_(nm, code, note)     { nm, p_##code##_s, p_##code##_e, 0, 0, note }
#define W_(nm, code, v, note)  { nm, p_##code##_s, p_##code##_e, 1, v, note }

static const sreg_t SREGS[] = {
    R_("CurrentEL",        rd_currentel, "must be 0x4 == EL1"),
    R_("SCTLR_EL1",        rd_sctlr,     ""),
    R_("TCR_EL1",          rd_tcr,       ""),
    R_("TTBR0_EL1",        rd_ttbr0,     ""),
    R_("TTBR1_EL1",        rd_ttbr1,     ""),
    R_("MAIR_EL1",         rd_mair,      ""),
    R_("AMAIR_EL1",        rd_amair,     ""),
    R_("VBAR_EL1",         rd_vbar,      ""),
    R_("CONTEXTIDR_EL1",   rd_contextidr,""),
    R_("TPIDR_EL0",        rd_tpidr_el0, ""),
    R_("TPIDRRO_EL0",      rd_tpidrro,   ""),
    R_("TPIDR_EL1",        rd_tpidr_el1, ""),
    R_("PAR_EL1",          rd_par,       ""),
    R_("ACTLR_EL1",        rd_actlr,     "doc 14: EnTSO here is behaviourally proven"),
    R_("CPACR_EL1",        rd_cpacr,     ""),
    R_("ESR_EL1",          rd_esr,       ""),
    R_("FAR_EL1",          rd_far,       ""),
    R_("ELR_EL1",          rd_elr,       ""),
    R_("SPSR_EL1",         rd_spsr,      ""),
    R_("SP_EL0",           rd_sp_el0,    ""),
    R_("ISR_EL1",          rd_isr,       "pending virtual interrupt state"),
    R_("DISR_EL1",         rd_disr,      "FEAT_RAS deferred SError"),
    R_("RVBAR_EL1",        rd_rvbar,     ""),
    R_("AFSR0_EL1",        rd_afsr0,     ""),
    R_("AFSR1_EL1",        rd_afsr1,     ""),
    R_("MDSCR_EL1",        rd_mdscr,     ""),
    R_("OSLSR_EL1",        rd_oslsr,     ""),
    R_("DBGBVR0_EL1",      rd_dbgbvr0,   ""),
    R_("DBGWCR0_EL1",      rd_dbgwcr0,   ""),
    R_("MDCCINT_EL1",      rd_mdccint,   ""),
    R_("CNTKCTL_EL1",      rd_cntkctl,   ""),
    R_("VPIDR_EL2",        rd_vpidr,     "EL2 register: must NOT be reachable from EL1"),
    R_("HCR_EL2",          rd_hcr,       "EL2 register: must NOT be reachable from EL1"),
    R_("TTBR0_EL2",        rd_ttbr0_el2, "EL2 register: must NOT be reachable from EL1"),
    R_("SCR_EL3",          rd_scr,       "EL3 register: must NOT be reachable from EL1"),

    R_("PMCCNTR_EL0",      rd_pmccntr,   "the cycle counter the whole project would like"),
    R_("PMCR_EL0",         rd_pmcr,      ""),
    R_("PMUSERENR_EL0",    rd_pmuserenr, ""),
    R_("PMSELR_EL0",       rd_pmselr,    ""),
    R_("PMCNTENSET_EL0",   rd_pmcntenset,""),
    R_("PMCEID0_EL0",      rd_pmceid0,   ""),
    R_("PMEVCNTR0_EL0",    rd_pmevcntr0, ""),
    R_("PMCCFILTR_EL0",    rd_pmccfiltr, ""),
    R_("PMINTENSET_EL1",   rd_pmintenset,""),

    R_("S3_0_c15_c0_0",    rd_apl_hid0,  "Apple IMPDEF HID0"),
    R_("S3_0_c15_c1_0",    rd_apl_hid1,  "Apple IMPDEF HID1"),
    R_("S3_0_c15_c4_0",    rd_apl_hid4,  "Apple IMPDEF HID4"),
    R_("S3_0_c15_c5_0",    rd_apl_hid5,  "Apple IMPDEF HID5"),
    R_("S3_0_c15_c13_0",   rd_apl_hid11, "Apple IMPDEF HID11"),
    R_("S3_1_c15_c0_0",    rd_apl_pmcr0, "Apple IMPDEF PMCR0"),
    R_("S3_1_c15_c1_0",    rd_apl_pmcr1, "Apple IMPDEF PMCR1"),
    R_("S3_1_c15_c5_0",    rd_apl_pmesr0,"Apple IMPDEF PMESR0"),
    R_("S3_2_c15_c0_0",    rd_apl_pmc0,  "Apple IMPDEF PMC0 (cycle counter)"),
    R_("S3_2_c15_c1_0",    rd_apl_pmc1,  "Apple IMPDEF PMC1"),
    R_("S3_3_c15_c0_0",    rd_apl_lsu_err,"Apple IMPDEF LSU_ERR_STS"),
    R_("S3_6_c15_c2_5",    rd_apl_fp,    "Apple IMPDEF (FP mode?)"),
    R_("S3_6_c15_c8_3",    rd_apl_c8_3,  "Apple IMPDEF"),
    R_("S3_4_c15_c2_0",    rd_apl_ctrr,  "Apple IMPDEF, EL2-flavoured"),

    W_("TPIDR_EL1",        wr_tpidr_el1,  0x1122334455667788ULL, ""),
    W_("TPIDR_EL0",        wr_tpidr_el0,  0x1122334455667788ULL, ""),
    W_("TPIDRRO_EL0",      wr_tpidrro,    0x1122334455667788ULL, ""),
    W_("CONTEXTIDR_EL1",   wr_contextidr, 0x00000000deadbeefULL, "32-bit register"),
    W_("AMAIR_EL1",        wr_amair,      0xffffffffffffffffULL, "RES0 unless IMPDEF attrs exist"),
    W_("PAR_EL1",          wr_par,        0x00000000deadbee0ULL, ""),
    W_("ACTLR_EL1",        wr_actlr,      0x2ULL,                "bit1 = Apple EnTSO"),
    W_("TTBR1_EL1",        wr_ttbr1,      0x0000000040006000ULL, "EPD1=1, so unused here"),
    W_("AFSR0_EL1",        wr_afsr0,      0xffffffffULL,         ""),
    W_("CNTKCTL_EL1",      wr_cntkctl,    0x00000303ULL,         "EL0 counter/timer access bits"),
    W_("MDSCR_EL1",        wr_mdscr,      0x00008000ULL,         ""),
    W_("DBGBVR0_EL1",      wr_dbgbvr0,    0x0000400000000000ULL, ""),
    W_("CNTV_CVAL_EL0",    wr_cntv_cval,  0x00ffffffffffffffULL, ""),
    W_("CNTP_CVAL_EL0",    wr_cntp_cval,  0x00ffffffffffffffULL, ""),
    W_("PMUSERENR_EL0",    wr_pmuserenr,  0xfULL,                ""),
    W_("PMCR_EL0",         wr_pmcr,       0x7ULL,                "E|P|C: enable + reset"),
    W_("VBAR_EL1",         wr_vbar,       RAM_IPA + OFF_VEC2,    "moved, then restored by teardown"),
    W_("CPACR_EL1",        wr_cpacr,      0x03330000ULL,         "FPEN|ZEN|SMEN all 0b11"),
    W_("S3_6_c15_c2_5",    wr_apl_fp,     0x3ULL,                "Apple IMPDEF"),
};
#define NSREGS (int)(sizeof(SREGS) / sizeof(SREGS[0]))

static void mode_regs(void) {
    printf("\n=== 1. system registers at guest EL1 =========================================\n");
    printf("  reads:  value, or where the access went instead\n");
    printf("  writes: WROTE -> READBACK -> AFTER-HOST-EXIT (the last column is HVF's context switch)\n\n");
    res_t *res = g_res;
    for (int i = 0; i < NSREGS; i++) {
        const sreg_t *s = &SREGS[i];
        cfg_t c; memset(&c, 0, sizeof c);
        c.code_s = s->s; c.code_e = s->e; c.vec_def = H_REPORT; c.mask_vtimer = 1;
        c.in[0] = s->val;
        run_cfg(&c, res);
        printf("  %-18s %-10s", s->name, outcome_str(res->outcome));
        if (res->outcome == OUT_OK && s->kind == 0) {
            printf(" = 0x%016llx", (unsigned long long)res->x[0]);
        } else if (res->outcome == OUT_OK) {
            printf(" w=0x%llx rb=0x%llx post=0x%llx%s%s",
                   (unsigned long long)res->x[0], (unsigned long long)res->x[1],
                   (unsigned long long)res->x[2],
                   res->x[1] != res->x[0] ? "  [PARTIAL/IGNORED]" : "",
                   res->x[2] != res->x[1] ? "  [NOT PRESERVED ACROSS HOST EXIT]" : "");
        } else if (res->outcome == OUT_TRAP_EL2) {
            uint64_t sy = res->esr_el2;
            printf(" EC=0x%02llx (%s)", (unsigned long long)esr_ec(sy), ecname(esr_ec(sy)));
            if (esr_ec(sy) == 0x18) {
                uint64_t iss = sy & 0x1ffffff;
                printf(" op0=%llu op1=%llu CRn=%llu CRm=%llu op2=%llu %s",
                       (iss >> 20) & 3, (iss >> 14) & 7, (iss >> 10) & 0xf,
                       (iss >> 1) & 0xf, (iss >> 17) & 7, (iss & 1) ? "read" : "write");
            }
        } else if (res->outcome == OUT_UNDEF_EL1 || res->outcome == OUT_EXC_EL1) {
            printf(" ESR_EL1=0x%llx EC=0x%02llx (%s)", (unsigned long long)res->esr_el1,
                   (unsigned long long)esr_ec(res->esr_el1), ecname(esr_ec(res->esr_el1)));
        }
        if (s->note && *s->note) printf("   -- %s", s->note);
        printf("\n");
        fflush(stdout);
    }
}

// ------------------------------------------------------------------------------------------- //
// Mode: applies
// ------------------------------------------------------------------------------------------- //
static void mode_applies(void) {
    printf("\n=== 2. does the write APPLY, or merely stick? =================================\n");
    res_t *res = g_res;

    RUN(ap_tcr_epd0, res, H_REPORT, VA_A);
    printf("  TCR_EL1.EPD0=1 then load VA_A:\n");
    printf("      before: read 0x%llx (expect MAGIC_A)   after: %s ESR_EL1=0x%llx EC=0x%02llx %s\n",
           (unsigned long long)(res->ncp ? res->cp[0][1] : 0), outcome_str(res->outcome),
           (unsigned long long)res->esr_el1, (unsigned long long)esr_ec(res->esr_el1),
           dfsc_str(res->esr_el1));
    printf("      => TCR_EL1 %s\n",
           (res->outcome == OUT_EXC_EL1 && esr_ec(res->esr_el1) == 0x25 &&
            (res->esr_el1 & 0x3c) == 0x04) ? "APPLIES (translation fault, level 0)"
                                           : "did NOT visibly apply -- inspect");

    RUN(ap_ttbr0, res, H_REPORT, VA_A, (RAM_IPA + OFF_L0_ALT) | (GUEST_ASID << 48));
    printf("  TTBR0_EL1 switched to an alternate root:\n");
    printf("      root1 -> 0x%llx   root2 -> 0x%llx  => TTBR0_EL1 %s\n",
           (unsigned long long)res->x[0], (unsigned long long)res->x[1],
           (res->outcome == OUT_OK && res->x[0] == MAGIC_A && res->x[1] == MAGIC_B)
               ? "APPLIES" : "did NOT visibly apply");

    RUN(ap_mair_device, res, H_REPORT, VA_A, MAIR_DEV1);
    printf("  MAIR_EL1 attr1 Normal-WB -> Device-nGnRnE, then an unaligned load:\n");
    printf("      control (Normal) unaligned load: 0x%llx   after: %s EC=0x%02llx %s\n",
           (unsigned long long)(res->ncp ? res->cp[0][3] : 0), outcome_str(res->outcome),
           (unsigned long long)esr_ec(res->esr_el1), dfsc_str(res->esr_el1));
    printf("      => MAIR_EL1 %s\n",
           (res->outcome == OUT_EXC_EL1 && (res->esr_el1 & 0x3f) == 0x21)
               ? "APPLIES (memory type changed: alignment fault on Device memory)"
               : "did NOT visibly apply");

    RUN(ap_sctlr_a, res, H_REPORT, VA_A);
    printf("  SCTLR_EL1.A = 1, then an unaligned load:\n");
    printf("      SCTLR readback 0x%llx   after: %s EC=0x%02llx %s\n",
           (unsigned long long)(res->ncp ? res->cp[0][1] : 0), outcome_str(res->outcome),
           (unsigned long long)esr_ec(res->esr_el1), dfsc_str(res->esr_el1));
    printf("      => SCTLR_EL1 (beyond .M) %s\n",
           (res->outcome == OUT_EXC_EL1 && (res->esr_el1 & 0x3f) == 0x21)
               ? "APPLIES" : "did NOT visibly apply");

    RUN(ap_vbar, res, H_SKIP, RAM_IPA + OFF_VEC2);
    printf("  VBAR_EL1 moved to a second table, then SVC:\n");
    printf("      outcome %s, ELR=0x%llx, readback=0x%llx (expect 0x%llx)\n",
           outcome_str(res->outcome), (unsigned long long)res->elr,
           (unsigned long long)(res->x[1]), (unsigned long long)(RAM_IPA + OFF_VEC2));
    printf("      => VBAR_EL1 %s\n",
           (res->outcome == OUT_EXC_EL1 || res->outcome == OUT_UNDEF_EL1)
               ? "APPLIES (the SVC landed in the NEW table, whose handler reports out)"
               : "did NOT visibly apply (the SVC was handled by the OLD table's skip handler)");

    // TPIDR_EL0 / TPIDRRO_EL0 as seen from guest EL0.
    {
        cfg_t c; memset(&c, 0, sizeof c);
        c.code_s = p_ap_el0_drop_s; c.code_e = p_ap_el0_drop_e;
        c.el0_s  = p_ap_el0_body_s; c.el0_e  = p_ap_el0_body_e;
        c.vec_def = H_REPORT; c.mask_vtimer = 1;
        c.in[0] = VA_D;
        run_cfg(&c, res);
        printf("  guest EL0: read TPIDR_EL0/TPIDRRO_EL0, then write TPIDRRO_EL0:\n");
        printf("      outcome %s  CurrentEL(x2)=0x%llx  CNTVCT(x3)=0x%llx  ESR_EL1=0x%llx EC=0x%02llx (%s)\n",
               outcome_str(res->outcome), (unsigned long long)res->x[2],
               (unsigned long long)res->x[3], (unsigned long long)res->esr_el1,
               (unsigned long long)esr_ec(res->esr_el1), ecname(esr_ec(res->esr_el1)));
        printf("      => a write to TPIDRRO_EL0 from EL0 %s\n",
               (res->outcome == OUT_UNDEF_EL1)
                   ? "raises an undef INTO THE GUEST's OWN VECTOR (no host exit)"
                   : "did something else -- inspect");
    }
}

// ------------------------------------------------------------------------------------------- //
// Mode: feat
// ------------------------------------------------------------------------------------------- //
static uint64_t idreg(const uint8_t *s, const uint8_t *e, res_t *res, int *ok) {
    cfg_t c; memset(&c, 0, sizeof c);
    c.code_s = s; c.code_e = e; c.vec_def = H_REPORT; c.mask_vtimer = 1;
    run_cfg(&c, res);
    *ok = (res->outcome == OUT_OK);
    return *ok ? res->x[0] : 0;
}
#define IDREG(name, res, ok) idreg(p_##name##_s, p_##name##_e, res, ok)

static uint32_t crc32_ref(uint32_t crc, const uint8_t *b, int n, uint32_t poly) {
    for (int i = 0; i < n; i++) {
        crc ^= b[i];
        for (int k = 0; k < 8; k++) crc = (crc >> 1) ^ (poly & (uint32_t)(-(int32_t)(crc & 1)));
    }
    return crc;
}

static void mode_feat(void) {
    printf("\n=== 3. FEAT_* : ID bit vs EXECUTED INSTRUCTION ================================\n");
    res_t *res = g_res;
    int ok;
    uint64_t isar0 = IDREG(rd_isar0, res, &ok);
    uint64_t isar1 = IDREG(rd_isar1, res, &ok);
    uint64_t isar2 = IDREG(rd_isar2, res, &ok);
    uint64_t pfr0  = IDREG(rd_pfr0,  res, &ok);
    uint64_t pfr1  = IDREG(rd_pfr1,  res, &ok);
    uint64_t mmfr2 = IDREG(rd_mmfr2, res, &ok);
    uint64_t dfr0  = IDREG(rd_dfr0,  res, &ok);
    uint64_t zfr0  = IDREG(rd_zfr0,  res, &ok);
    printf("  ID_AA64ISAR0_EL1 = 0x%016llx  ID_AA64ISAR1_EL1 = 0x%016llx\n",
           (unsigned long long)isar0, (unsigned long long)isar1);
    printf("  ID_AA64ISAR2_EL1 = 0x%016llx  ID_AA64PFR0_EL1  = 0x%016llx\n",
           (unsigned long long)isar2, (unsigned long long)pfr0);
    printf("  ID_AA64PFR1_EL1  = 0x%016llx  ID_AA64MMFR2_EL1 = 0x%016llx\n",
           (unsigned long long)pfr1, (unsigned long long)mmfr2);
    printf("  ID_AA64DFR0_EL1  = 0x%016llx  ID_AA64ZFR0_EL1  = 0x%016llx\n",
           (unsigned long long)dfr0, (unsigned long long)zfr0);
    printf("    ID bits: Atomic=%llu TS=%llu CRC32=%llu DP=%llu APA=%llu API=%llu GPA=%llu GPI=%llu\n",
           (isar0 >> 20) & 0xf, (isar0 >> 52) & 0xf, (isar0 >> 16) & 0xf, (isar0 >> 44) & 0xf,
           (isar1 >> 4) & 0xf, (isar1 >> 8) & 0xf, (isar1 >> 24) & 0xf, (isar1 >> 28) & 0xf);
    printf("             JSCVT=%llu LRCPC=%llu  BT=%llu SSBS=%llu MTE=%llu  SVE=%llu DIT=%llu  AT(LSE2)=%llu  PMUVer=%llu\n",
           (isar1 >> 12) & 0xf, (isar1 >> 20) & 0xf, pfr1 & 0xf, (pfr1 >> 4) & 0xf,
           (pfr1 >> 8) & 0xf, (pfr0 >> 32) & 0xf, (pfr0 >> 48) & 0xf,
           (mmfr2 >> 32) & 0xf, (dfr0 >> 8) & 0xf);
    printf("\n  executed-instruction evidence:\n");

    RUN(feat_pauth, res, H_REPORT, 0x0000400000000000ULL, 0x1234ULL);
    if (res->outcome == OUT_OK) {
        uint64_t p_off = res->x[0], p_on = res->x[1], auth = res->x[2], bad = res->x[3];
        uint64_t pacga = res->x[6], sctlr = res->x[7];
        printf("    PAuth   PACIA with SCTLR.EnIA=0 -> 0x%016llx %s\n", (unsigned long long)p_off,
               p_off == 0x0000400000000000ULL ? "(unchanged: SILENTLY A NOP)" : "(modified)");
        printf("            PACIA with SCTLR.EnIA=1 -> 0x%016llx %s\n", (unsigned long long)p_on,
               p_on != 0x0000400000000000ULL ? "(signed)" : "(UNCHANGED -- PAuth not applied)");
        printf("            AUTIA(signed)  -> 0x%016llx %s ; AUTIA(corrupted) -> 0x%016llx %s\n",
               (unsigned long long)auth, auth == 0x0000400000000000ULL ? "OK" : "MISMATCH",
               (unsigned long long)bad, bad != 0x0000400000000000ULL ? "poisoned (as required)" : "NOT poisoned");
        printf("            PACGA -> 0x%016llx ; SCTLR_EL1 after EnIA write = 0x%llx\n",
               (unsigned long long)pacga, (unsigned long long)sctlr);
    } else verdict("PAuth", res, "");

    RUN(feat_pauth_key, res, H_REPORT, 0x0000400000000000ULL, 0xa5a5a5a5a5a5a5a5ULL);
    if (res->outcome == OUT_OK)
        printf("    PAuthKey  PAC under key=0 -> 0x%016llx ; under new key -> 0x%016llx %s ;"
               " after a host exit -> 0x%016llx %s\n",
               (unsigned long long)res->x[0], (unsigned long long)res->x[1],
               res->x[0] != res->x[1] ? "(key applies)" : "(KEY IGNORED)",
               (unsigned long long)res->x[2],
               res->x[2] == res->x[1] ? "(preserved)" : "(NOT PRESERVED)");
    else verdict("PAuthKey", res, "");

    {
        cfg_t c; memset(&c, 0, sizeof c);
        c.code_s = p_feat_bti_s; c.code_e = p_feat_bti_e;
        c.vec_def = H_REPORT; c.vec_cur_sync = H_SKIPREC; c.mask_vtimer = 1;
        c.in[0] = VA_C; c.in[1] = VA_C + 8;
        run_cfg(&c, res);
        printf("    BTI     SCTLR_EL1.BT1 readback=0x%llx  in-guest exceptions=%llu  ESR_EL1=0x%llx EC=0x%02llx (%s)\n",
               (unsigned long long)res->x[0], (unsigned long long)res->x19,
               (unsigned long long)res->x20, (unsigned long long)esr_ec(res->x20),
               ecname(esr_ec(res->x20)));
        printf("            => BLR into a guarded page at a non-landing-pad %s\n",
               (res->x19 >= 1 && esr_ec(res->x20) == 0x0d)
                   ? "RAISES a Branch Target Exception: BTI IS ENFORCED at guest EL1"
                   : "did not fault: BTI not enforced (or not reached)");
    }

    RUN(feat_lse, res, H_REPORT, RAM_IPA + OFF_SCRATCH);
    if (res->outcome == OUT_OK)
        printf("    LSE     CASAL old=0x%llx mem=0x%llx (expect 0x55/0x66) ; LDADDAL old=0x%llx mem=0x%llx"
               " (expect 7/12) ; CASPAL -> 0x%llx/0x%llx (expect 0x1111/0x2222)\n",
               (unsigned long long)res->x[0], (unsigned long long)res->x[1],
               (unsigned long long)res->x[2], (unsigned long long)res->x[3],
               (unsigned long long)res->x[4], (unsigned long long)res->x[5]);
    else verdict("LSE", res, "");

    RUN(feat_lse2, res, H_REPORT, RAM_IPA + OFF_SCRATCH);
    printf("    LSE2    unaligned 8-byte loads across 8- and 16-byte boundaries: %s",
           res->outcome == OUT_OK ? "no fault" :
           (res->outcome == OUT_EXC_EL1 ? "faulted" : outcome_str(res->outcome)));
    if (res->outcome != OUT_OK)
        printf(" (ESR_EL1=0x%llx %s)", (unsigned long long)res->esr_el1, dfsc_str(res->esr_el1));
    printf("\n            8-byte-aligned CASPAL: %s\n",
           res->outcome == OUT_OK ? "accepted (no alignment fault)" :
           ((res->esr_el1 & 0x3f) == 0x21 ? "ALIGNMENT FAULT (16-byte alignment required)" : "other"));

    RUN(feat_flagm, res, H_REPORT, 0x20000000ULL);
    if (res->outcome == OUT_OK)
        printf("    FlagM   CFINV->0x%llx RMIF->0x%llx SETF8->0x%llx SETF16->0x%llx AXFLAG->0x%llx XAFLAG->0x%llx\n",
               (unsigned long long)res->x[0], (unsigned long long)res->x[1],
               (unsigned long long)res->x[2], (unsigned long long)res->x[3],
               (unsigned long long)res->x[4], (unsigned long long)res->x[5]);
    else verdict("FlagM", res, "");

    RUN(feat_jscvt, res, H_REPORT, 0x3FF8000000000000ULL);   // 1.5
    if (res->outcome == OUT_OK)
        printf("    JSCVT   FJCVTZS(1.5) = %lld  NZCV=0x%llx  %s\n", (long long)(int32_t)res->x[0],
               (unsigned long long)res->x[1], res->x[0] == 1 ? "(correct)" : "(WRONG)");
    else verdict("JSCVT", res, "");

    RUN(feat_crc32, res, H_REPORT, 0x00000000ULL, 0x0123456789abcdefULL);
    if (res->outcome == OUT_OK) {
        uint64_t v = 0x0123456789abcdefULL;
        uint8_t b[8]; for (int i = 0; i < 8; i++) b[i] = (uint8_t)(v >> (8 * i));
        uint32_t e1 = crc32_ref(0, b, 8, 0xEDB88320u), e2 = crc32_ref(0, b, 8, 0x82F63B78u);
        printf("    CRC32   CRC32X=0x%08llx (expect 0x%08x) %s ; CRC32CX=0x%08llx (expect 0x%08x) %s\n",
               (unsigned long long)res->x[0], e1, (uint32_t)res->x[0] == e1 ? "OK" : "MISMATCH",
               (unsigned long long)res->x[1], e2, (uint32_t)res->x[1] == e2 ? "OK" : "MISMATCH");
    } else verdict("CRC32", res, "");

    RUN(feat_dotprod, res, H_REPORT, 0x02020202ULL, 0x03030303ULL);
    if (res->outcome == OUT_OK)
        printf("    DotProd UDOT lanes = 0x%016llx / 0x%016llx (each lane = 4*2*3 = 24 = 0x18)\n",
               (unsigned long long)res->x[0], (unsigned long long)res->x[1]);
    else verdict("DotProd", res, "");

    RUN(feat_dit, res, H_REPORT, 0ULL);
    printf("    DIT     %s DIT after set=0x%llx after clear=0x%llx\n",
           outcome_str(res->outcome), (unsigned long long)res->x[0], (unsigned long long)res->x[1]);
    RUN(feat_ssbs, res, H_REPORT, 0ULL);
    printf("    SSBS    %s SSBS after set=0x%llx after clear=0x%llx\n",
           outcome_str(res->outcome), (unsigned long long)res->x[0], (unsigned long long)res->x[1]);
    RUN(feat_pan, res, H_REPORT, 0ULL);
    printf("    PAN     %s PAN after set=0x%llx after clear=0x%llx\n",
           outcome_str(res->outcome), (unsigned long long)res->x[0], (unsigned long long)res->x[1]);
    RUN(feat_uao, res, H_REPORT, 0ULL);
    printf("    UAO     %s UAO after set=0x%llx\n", outcome_str(res->outcome),
           (unsigned long long)res->x[0]);
    RUN(feat_mte_tco, res, H_REPORT, 0ULL);
    printf("    MTE.TCO %s TCO after set=0x%llx after clear=0x%llx\n",
           outcome_str(res->outcome), (unsigned long long)res->x[0], (unsigned long long)res->x[1]);
    RUN(feat_mte_irg, res, H_REPORT, RAM_IPA + OFF_SCRATCH);
    printf("    MTE.IRG %s IRG->0x%llx ADDG->0x%llx\n", outcome_str(res->outcome),
           (unsigned long long)res->x[0], (unsigned long long)res->x[1]);
    RUN(feat_mte_stg, res, H_REPORT, RAM_IPA + OFF_SCRATCH);
    verdict("MTE.STG/LDG", res, "");
    RUN(feat_sve_cpacr, res, H_REPORT, 0ULL);
    printf("    SVE     CPACR_EL1 after writing ZEN=0b11: 0x%llx  (ZEN reads back %llu)\n",
           (unsigned long long)res->x[0], (res->x[0] >> 16) & 3);
    RUN(feat_sve_insn, res, H_REPORT, 0ULL);
    verdict("SVE PTRUE", res, res->outcome == OUT_UNDEF_EL1 ? "undefined instruction: SVE ABSENT" : "");
    RUN(feat_rndr, res, H_REPORT, 0ULL);
    verdict("RNDR", res, "");
    RUN(feat_sb, res, H_REPORT, 0ULL);
    verdict("SB", res, "");
    RUN(feat_esb, res, H_REPORT, 0ULL);
    verdict("ESB / DISR_EL1", res, "SError machinery");
    RUN(feat_ecv, res, H_REPORT, 0ULL);
    verdict("CNTVCTSS/CNTPCTSS", res, "FEAT_ECV self-synchronised counters");
    RUN(feat_lrcpc, res, H_REPORT, RAM_IPA + OFF_SCRATCH);
    verdict("LDAPR/LDAPUR/STLUR", res, "FEAT_LRCPC / LRCPC2");
    RUN(feat_dczva, res, H_REPORT, RAM_IPA + OFF_SCRATCH);
    verdict("DC ZVA", res, "");
}

// ------------------------------------------------------------------------------------------- //
// Mode: time -- what clock does a guest at EL1 actually have?
// ------------------------------------------------------------------------------------------- //
static void mode_time(void) {
    printf("\n=== 4. the in-guest clock ======================================================\n");
    res_t *res = g_res;
    int ok;

    // Raw CNTVCT samples: frequency, granularity, monotonicity.
    uint64_t NS = 512;
    {
        cfg_t c; memset(&c, 0, sizeof c);
        c.code_s = p_clk_samples_s; c.code_e = p_clk_samples_e;
        c.vec_def = H_REPORT; c.mask_vtimer = 1;
        c.in[0] = NS; c.in[2] = RAM_IPA + OFF_RESULT;
        uint64_t h0 = mach_absolute_time();
        run_cfg(&c, res);
        uint64_t h1 = mach_absolute_time();
        if (res->outcome != OUT_OK) { verdict("CNTVCT sampling", res, ""); return; }
        g_cntfrq = res->x[0];
        uint64_t back = 0, same = 0, mind = ~0ULL, maxd = 0;
        for (uint64_t i = 1; i < NS; i++) {
            uint64_t a = res->res[i - 1], b = res->res[i];
            if (b < a) back++;
            else if (b == a) same++;
            else { uint64_t d = b - a; if (d < mind) mind = d; if (d > maxd) maxd = d; }
        }
        printf("  CNTFRQ_EL0                = %llu Hz  (%.2f ns/tick)\n",
               (unsigned long long)g_cntfrq, 1e9 / (double)g_cntfrq);
        printf("  %llu back-to-back MRS CNTVCT_EL0: %llu repeats, %llu decreases, "
               "smallest nonzero step %llu tick(s), largest %llu\n",
               (unsigned long long)NS, (unsigned long long)same, (unsigned long long)back,
               (unsigned long long)mind, (unsigned long long)maxd);
        printf("  monotonic: %s\n", back == 0 ? "YES (no decrease observed)" : "NO");
        printf("  host mach_absolute_time around the run: %llu .. %llu ; guest's last CNTVCT %llu\n",
               (unsigned long long)h0, (unsigned long long)h1, (unsigned long long)res->x[1]);
        printf("  => guest CNTVCT %s the host's mach_absolute_time timebase\n",
               (res->x[1] >= h0 && res->x[1] <= h1) ? "SHARES (same value range as)"
                                                    : "is OFFSET from");
    }

    // Guest clock estimate from a dependent-ADD chain, so ns can be quoted in cycles.
    {
        cfg_t c; memset(&c, 0, sizeof c);
        c.code_s = p_clk_ghz_s; c.code_e = p_clk_ghz_e; c.vec_def = H_REPORT; c.mask_vtimer = 1;
        c.in[0] = 200000;
        run_cfg(&c, res);
        if (res->outcome == OUT_OK) {
            double ns = (double)res->x[0] * 1e9 / (double)g_cntfrq / (200000.0 * 16.0);
            g_ghz = 1.0 / ns;
            printf("  dependent ADD (1 cycle)    = %.4f ns  => guest clock ~ %.2f GHz\n", ns, g_ghz);
        }
    }

    stat_t ctl = TIMED(tm_ctl, 4096, 41, 0, 0, 0, 0, 0, res, &ok);
    double base = ok ? ctl.med : 0;
    printf("\n  cost of reading the clock (median of 41 rounds x 16384 reads):\n");
    trow("empty loop body (floor)", ctl, 0, ok, res);
    stat_t s;
    s = TIMED(tm_cntvct, 4096, 41, 0, 0, 0, 0, 0, res, &ok);
    trow("ISB ; MRS CNTVCT_EL0", s, base, ok, res);
    s = TIMED(tm_cntvct_noisb, 4096, 41, 0, 0, 0, 0, 0, res, &ok);
    trow("MRS CNTVCT_EL0 (no ISB)", s, base, ok, res);
    s = TIMED(tm_isb, 4096, 41, 0, 0, 0, 0, 0, res, &ok);
    trow("ISB alone", s, base, ok, res);
    s = TIMED(tm_dsbish, 4096, 41, 0, 0, 0, 0, 0, res, &ok);
    trow("DSB ISH alone", s, base, ok, res);

    printf("\n  the cycle counters:\n");
    {
        cfg_t c; memset(&c, 0, sizeof c);
        c.code_s = p_clk_pmccntr_s; c.code_e = p_clk_pmccntr_e;
        c.vec_def = H_REPORT; c.mask_vtimer = 1; c.skip_traps = 1;
        run_cfg(&c, res);
        printf("    PMCCNTR_EL0   %-9s host exits inside the window = %d  delta over a 4096-loop = %llu",
               outcome_str(res->outcome), res->window_exits, (unsigned long long)res->x[1]);
        if (res->ntrap) printf("  first trap ESR_EL2=0x%llx EC=0x%02llx (%s)",
                               (unsigned long long)res->trap_esr[0],
                               (unsigned long long)esr_ec(res->trap_esr[0]),
                               ecname(esr_ec(res->trap_esr[0])));
        printf("\n");
    }
    {
        cfg_t c; memset(&c, 0, sizeof c);
        c.code_s = p_clk_apl_pmc0_s; c.code_e = p_clk_apl_pmc0_e;
        c.vec_def = H_REPORT; c.mask_vtimer = 1; c.skip_traps = 1;
        run_cfg(&c, res);
        printf("    Apple PMC0    %-9s host exits inside the window = %d  delta = %llu",
               outcome_str(res->outcome), res->window_exits, (unsigned long long)res->x[1]);
        if (res->ntrap) printf("  first trap ESR_EL2=0x%llx EC=0x%02llx",
                               (unsigned long long)res->trap_esr[0],
                               (unsigned long long)esr_ec(res->trap_esr[0]));
        printf("\n");
    }
}

// ------------------------------------------------------------------------------------------- //
// Mode: at
// ------------------------------------------------------------------------------------------- //
static void par_print(const char *what, uint64_t par) {
    if (par & 1) {
        printf("      %-26s PAR=0x%016llx  F=1 FAULT, FST=0x%02llx (%s)\n", what,
               (unsigned long long)par, (unsigned long long)((par >> 1) & 0x3f),
               dfsc_str((par >> 1) & 0x3f));
    } else {
        printf("      %-26s PAR=0x%016llx  F=0 OK, PA=0x%llx SH=%llu ATTR=0x%llx\n", what,
               (unsigned long long)par, (unsigned long long)(par & 0x0000fffffffff000ULL),
               (par >> 7) & 3, (par >> 56) & 0xff);
    }
}

static void mode_at(void) {
    printf("\n=== 5. AT (address translation) instructions ===================================\n");
    res_t *res = g_res;
    RUN(at_check, res, H_REPORT, VA_A, VA_B, VA_UNMAPPED);
    if (res->outcome == OUT_OK) {
        printf("  correctness (DATA_A ipa=0x%llx, DATA_B ipa=0x%llx):\n",
               (unsigned long long)(RAM_IPA + OFF_DATA_A), (unsigned long long)(RAM_IPA + OFF_DATA_B));
        par_print("AT S1E1R  VA_A (RW page)", res->x[0]);
        par_print("AT S1E1W  VA_A (RW page)", res->x[1]);
        par_print("AT S1E1W  VA_B (RO page)", res->x[2]);
        par_print("AT S1E1R  unmapped VA",    res->x[3]);
        par_print("AT S1E0R  VA_A (EL1-only)",res->x[4]);
    } else verdict("AT S1E1R/W/E0R", res, "");
    RUN(at_check_pan, res, H_REPORT, VA_A, VA_B, VA_UNMAPPED);
    if (res->outcome == OUT_OK) {
        par_print("AT S1E1RP VA_A", res->x[0]);
        par_print("AT S1E1WP VA_A", res->x[1]);
        par_print("AT S1E0W  VA_A", res->x[2]);
        par_print("AT S1E1RP VA_A, PAN=1", res->x[3]);
        par_print("AT S1E1R  VA_A, PAN=1", res->x[4]);
    } else verdict("AT S1E1RP/WP (FEAT_PAN2)", res, "");

    printf("\n  latency (median of 41 rounds x 16384 executions; the loop floor is subtracted;\n"
           "  timed in-guest with CNTVCT_EL0, so no host exit is inside the window):\n");
    int ok;
    stat_t ctl = TIMED(tm_ctl, 4096, 41, VA_A, VA_B, VA_UNMAPPED, 0, 0, res, &ok);
    double base = ok ? ctl.med : 0;
    stat_t s;
    trow("empty loop body (floor)", ctl, 0, ok, res);
    s = TIMED(tm_ldr, 4096, 41, VA_A, VA_B, VA_UNMAPPED, 0, 0, res, &ok);
    trow("LDR from the same page (reference)", s, base, ok, res);
    s = TIMED(tm_par_rd, 4096, 41, VA_A, VA_B, VA_UNMAPPED, 0, 0, res, &ok);
    trow("ISB ; MRS PAR_EL1 (no AT)", s, base, ok, res);
    s = TIMED(tm_at_bare, 4096, 41, VA_A, VA_B, VA_UNMAPPED, 0, 0, res, &ok);
    trow("AT S1E1R alone (no ISB, no PAR)", s, base, ok, res);
    s = TIMED(tm_at_noisb, 4096, 41, VA_A, VA_B, VA_UNMAPPED, 0, 0, res, &ok);
    trow("AT S1E1R ; MRS PAR_EL1 (no ISB)", s, base, ok, res);
    s = TIMED(tm_at_r, 4096, 41, VA_A, VA_B, VA_UNMAPPED, 0, 0, res, &ok);
    trow("AT S1E1R ; ISB ; MRS PAR_EL1", s, base, ok, res);
    s = TIMED(tm_at_w, 4096, 41, VA_A, VA_B, VA_UNMAPPED, 0, 0, res, &ok);
    trow("AT S1E1W ; ISB ; MRS PAR_EL1", s, base, ok, res);
    s = TIMED(tm_at_rp, 4096, 41, VA_A, VA_B, VA_UNMAPPED, 0, 0, res, &ok);
    trow("AT S1E1RP ; ISB ; MRS PAR_EL1", s, base, ok, res);
    s = TIMED(tm_at_wp, 4096, 41, VA_A, VA_B, VA_UNMAPPED, 0, 0, res, &ok);
    trow("AT S1E1WP ; ISB ; MRS PAR_EL1", s, base, ok, res);
    s = TIMED(tm_at_e0r, 4096, 41, VA_A, VA_B, VA_UNMAPPED, 0, 0, res, &ok);
    trow("AT S1E0R ; ISB ; MRS PAR_EL1", s, base, ok, res);
    s = TIMED(tm_at_e0w, 4096, 41, VA_A, VA_B, VA_UNMAPPED, 0, 0, res, &ok);
    trow("AT S1E0W ; ISB ; MRS PAR_EL1", s, base, ok, res);
    s = TIMED(tm_at_ro, 4096, 41, VA_A, VA_B, VA_UNMAPPED, 0, 0, res, &ok);
    trow("AT S1E1W on a RO page (fails)", s, base, ok, res);
    s = TIMED(tm_at_fail, 4096, 41, VA_A, VA_B, VA_UNMAPPED, 0, 0, res, &ok);
    trow("AT S1E1R on an unmapped VA (fails)", s, base, ok, res);
}

// ------------------------------------------------------------------------------------------- //
// Mode: tlbi
// ------------------------------------------------------------------------------------------- //
typedef struct { const char *name; const uint8_t *s, *e; int expect_inv; } tsem_t;
#define TS_(nm, code, ex) { nm, p_##code##_s, p_##code##_e, ex }

static void mode_tlbi(void) {
    printf("\n=== 6. TLBI at guest EL1 =======================================================\n");
    res_t *res = g_res;

    static const tsem_t SEM[] = {
        TS_("(no TLBI)",      sem_none,      0),
        TS_("VMALLE1",        sem_vmalle1,   1),
        TS_("VMALLE1IS",      sem_vmalle1is, 1),
        TS_("VAE1",           sem_vae1,      1),
        TS_("VAE1IS",         sem_vae1is,    1),
        TS_("VAAE1",          sem_vaae1,     1),
        TS_("VAAE1IS",        sem_vaae1is,   1),
        TS_("VALE1",          sem_vale1,     1),
        TS_("VALE1IS",        sem_vale1is,   1),
        TS_("ASIDE1",         sem_aside1,    1),
        TS_("ASIDE1IS",       sem_aside1is,  1),
        TS_("ASIDE1IS wrong ASID", sem_aside_bad, 0),
        TS_("VAE1IS wrong VA",     sem_vae1_bad,  0),
        TS_("RVAE1IS (TLBIRANGE)", sem_rvae1is,   1),
        TS_("VMALLE1OS (TLBIOS)",  sem_vmalle1os, 1),
    };
    int nsem = (int)(sizeof(SEM) / sizeof(SEM[0]));

    uint64_t l3_ipa = RAM_IPA + OFF_L3_T;
    uint64_t ro_desc = (RAM_IPA + OFF_DATA_A) | D_PAGE | D_AF | D_SH_IS | D_AP_RO | D_ATTR1
                     | D_NG | D_UXN | D_PXN;
    uint64_t va_op   = (VA_A >> 12) | (GUEST_ASID << 48);
    uint64_t va_bad  = ((VA_A + 0x8000) >> 12) | (GUEST_ASID << 48);
    uint64_t asid_op = (GUEST_ASID << 48);
    uint64_t asid_bad= ((GUEST_ASID + 1) << 48);
    // RVAE1IS operand: TG=4KB(0b01), scale/num covering 1 page, BaseADDR = VA>>12.
    uint64_t rva_op  = (VA_A >> 12) | (1ULL << 46) | (0ULL << 44) | (0ULL << 39) | (GUEST_ASID << 48);

    printf("  semantic test: cache a level-3 entry, demote the leaf to read-only behind the TLB,\n"
           "  invalidate with ONE variant, then store again. FAULT = the variant really invalidated.\n");
    for (int i = 0; i < nsem; i++) {
        cfg_t c; memset(&c, 0, sizeof c);
        c.code_s = SEM[i].s; c.code_e = SEM[i].e; c.vec_def = H_REPORT; c.mask_vtimer = 1;
        c.in[0] = VA_A; c.in[1] = l3_ipa; c.in[2] = ro_desc;
        c.in[4] = va_bad; c.in[5] = asid_bad;
        c.in[6] = strstr(SEM[i].name, "RVAE") ? rva_op : va_op;
        c.in[7] = asid_op;
        run_cfg(&c, res);
        int invalidated = (res->outcome == OUT_EXC_EL1 && (res->esr_el1 & 0x3c) == 0x0c);
        int ran_through = (res->outcome == OUT_OK);
        const char *w;
        if (invalidated)      w = "INVALIDATED (store faulted)";
        else if (ran_through) w = "not invalidated (stale entry used)";
        else                  w = outcome_str(res->outcome);
        printf("    TLBI %-22s %-34s %s\n", SEM[i].name, w,
               (invalidated == SEM[i].expect_inv) ? "" :
               (SEM[i].expect_inv ? "  <-- EXPECTED to invalidate" : "  <-- expected NOT to"));
        fflush(stdout);
    }

    printf("\n  latency (median of 41 rounds x 16384 executions, loop floor subtracted;\n"
           "  each body is `TLBI <op> ; DSB ISH ; ISB`):\n");
    int ok;
    stat_t ctl = TIMED(tm_ctl, 4096, 41, VA_A, 0, 0, va_op, asid_op, res, &ok);
    double base = ok ? ctl.med : 0;
    trow("empty loop body (floor)", ctl, 0, ok, res);
    stat_t s;
    s = TIMED(tm_barrier, 4096, 41, VA_A, 0, 0, va_op, asid_op, res, &ok);
    trow("DSB ISH ; ISB (no TLBI)", s, base, ok, res);
    s = TIMED(tm_tlbi_nosync, 4096, 41, VA_A, 0, 0, va_op, asid_op, res, &ok);
    trow("TLBI VAE1IS alone (no barriers)", s, base, ok, res);
    s = TIMED(tm_tlbi_vmalle1, 4096, 41, VA_A, 0, 0, va_op, asid_op, res, &ok);
    trow("TLBI VMALLE1", s, base, ok, res);
    s = TIMED(tm_tlbi_vmalle1is, 4096, 41, VA_A, 0, 0, va_op, asid_op, res, &ok);
    trow("TLBI VMALLE1IS", s, base, ok, res);
    s = TIMED(tm_tlbi_vae1, 4096, 41, VA_A, 0, 0, va_op, asid_op, res, &ok);
    trow("TLBI VAE1", s, base, ok, res);
    s = TIMED(tm_tlbi_vae1is, 4096, 41, VA_A, 0, 0, va_op, asid_op, res, &ok);
    trow("TLBI VAE1IS", s, base, ok, res);
    s = TIMED(tm_tlbi_vaae1, 4096, 41, VA_A, 0, 0, va_op, asid_op, res, &ok);
    trow("TLBI VAAE1", s, base, ok, res);
    s = TIMED(tm_tlbi_vaae1is, 4096, 41, VA_A, 0, 0, va_op, asid_op, res, &ok);
    trow("TLBI VAAE1IS", s, base, ok, res);
    s = TIMED(tm_tlbi_vale1, 4096, 41, VA_A, 0, 0, va_op, asid_op, res, &ok);
    trow("TLBI VALE1", s, base, ok, res);
    s = TIMED(tm_tlbi_vale1is, 4096, 41, VA_A, 0, 0, va_op, asid_op, res, &ok);
    trow("TLBI VALE1IS", s, base, ok, res);
    s = TIMED(tm_tlbi_aside1, 4096, 41, VA_A, 0, 0, va_op, asid_op, res, &ok);
    trow("TLBI ASIDE1", s, base, ok, res);
    s = TIMED(tm_tlbi_aside1is, 4096, 41, VA_A, 0, 0, va_op, asid_op, res, &ok);
    trow("TLBI ASIDE1IS", s, base, ok, res);
}

// ------------------------------------------------------------------------------------------- //
// Mode: timer
// ------------------------------------------------------------------------------------------- //
static void mode_timer(void) {
    printf("\n=== 7. timers ==================================================================\n");
    res_t *res = g_res;

    {
        cfg_t c; memset(&c, 0, sizeof c);
        c.code_s = p_timer_regs_s; c.code_e = p_timer_regs_e;
        c.vec_def = H_REPORT; c.mask_vtimer = 1; c.skip_traps = 1;
        run_cfg(&c, res);
        printf("  timer register block, all inside one exit-counted window:\n");
        printf("    outcome %s  host exits in window = %d  vtimer exits = %d\n",
               outcome_str(res->outcome), res->window_exits, res->window_vtimer);
        printf("    CNTFRQ=%llu  CNTV_CTL=0x%llx  CNTV_CVAL=0x%llx  CNTV_TVAL(read back after"
               " writing 0x1000)=0x%llx  CNTKCTL=0x%llx\n",
               (unsigned long long)res->x[0], (unsigned long long)res->x[1],
               (unsigned long long)res->x[2], (unsigned long long)res->x[4],
               (unsigned long long)res->x[5]);
        for (int i = 0; i < res->ntrap; i++)
            printf("    trap: ESR_EL2=0x%llx EC=0x%02llx (%s)\n",
                   (unsigned long long)res->trap_esr[i],
                   (unsigned long long)esr_ec(res->trap_esr[i]),
                   ecname(esr_ec(res->trap_esr[i])));
    }

    // The decisive question: does the virtual timer's interrupt reach the guest's own vector
    // without leaving the VM?
    for (int masked = 0; masked <= 1; masked++) {
        cfg_t c; memset(&c, 0, sizeof c);
        c.code_s = p_timer_vtimer_s; c.code_e = p_timer_vtimer_e;
        c.vec_def = H_REPORT; c.vec_cur_irq = H_VTIMER;
        c.mask_vtimer = masked;      // does the host acknowledge the VTIMER exit or not?
        c.timeout_ms = 8000;
        c.in[0] = 2400;              // 100 us at 24 MHz
        c.in[1] = 200000000ULL;      // spin budget
        run_cfg(&c, res);
        printf("\n  virtual timer armed 2400 ticks (100 us) ahead, PSTATE.I unmasked,"
               " host %s the VTIMER exit:\n", masked ? "acknowledges (set_vtimer_mask)" : "ignores");
        printf("    outcome %s  IRQs taken by the GUEST's own vector = %llu\n",
               outcome_str(res->outcome), (unsigned long long)res->x[0]);
        printf("    hv_vcpu_run returns inside the window = %d  (of which VTIMER_ACTIVATED = %d)\n",
               res->window_exits, res->window_vtimer);
        printf("    ticks from arming to the handler = %llu (%.2f us) ; CNTV_CTL at exit = 0x%llx"
               " ; total ticks spun = %llu\n",
               (unsigned long long)res->x[1],
               (double)(int64_t)res->x[1] * 1e6 / (double)g_cntfrq,
               (unsigned long long)res->x[2], (unsigned long long)res->x[3]);
        printf("    => %s\n",
               (res->x[0] > 0 && res->window_exits == 0)
                   ? "the vtimer IRQ is delivered ENTIRELY INSIDE THE VM"
                   : (res->x[0] > 0
                        ? "the IRQ reached the guest, but only after a host exit (see the count)"
                        : "the guest's vector never fired"));
    }

    {
        cfg_t c; memset(&c, 0, sizeof c);
        c.code_s = p_timer_vtimer_masked_s; c.code_e = p_timer_vtimer_masked_e;
        c.vec_def = H_REPORT; c.vec_cur_irq = H_VTIMER; c.mask_vtimer = 1; c.timeout_ms = 8000;
        c.in[0] = 2400; c.in[1] = 40000000ULL;
        run_cfg(&c, res);
        printf("\n  same, but with PSTATE.I left MASKED throughout:\n");
        printf("    outcome %s  guest IRQs = %llu  window exits = %d (VTIMER = %d)"
               "  CNTV_CTL=0x%llx (ISTATUS=%llu)  ISR_EL1=0x%llx\n",
               outcome_str(res->outcome), (unsigned long long)res->x[0], res->window_exits,
               res->window_vtimer, (unsigned long long)res->x[1],
               (res->x[1] >> 2) & 1, (unsigned long long)res->x[2]);
    }

    {
        cfg_t c; memset(&c, 0, sizeof c);
        c.code_s = p_timer_ptimer_s; c.code_e = p_timer_ptimer_e;
        c.vec_def = H_REPORT; c.vec_cur_irq = H_VTIMER; c.mask_vtimer = 1;
        c.skip_traps = 1; c.timeout_ms = 8000;
        c.in[0] = 2400; c.in[1] = 40000000ULL;
        run_cfg(&c, res);
        printf("\n  PHYSICAL timer (CNTP_*) at guest EL1:\n");
        printf("    outcome %s  guest IRQs = %llu  window exits = %d  CNTP_CTL=0x%llx  traps=%d",
               outcome_str(res->outcome), (unsigned long long)res->x[0], res->window_exits,
               (unsigned long long)res->x[1], res->ntrap);
        if (res->ntrap) printf("  first ESR_EL2=0x%llx EC=0x%02llx",
                               (unsigned long long)res->trap_esr[0],
                               (unsigned long long)esr_ec(res->trap_esr[0]));
        printf("\n");
    }

    {
        cfg_t c; memset(&c, 0, sizeof c);
        c.code_s = p_irq_masked_spin_s; c.code_e = p_irq_masked_spin_e;
        c.vec_def = H_REPORT; c.vec_cur_irq = H_COUNT; c.mask_vtimer = 1;
        c.inject_irq = 1; c.timeout_ms = 8000;
        c.in[0] = 20000000ULL;
        run_cfg(&c, res);
        printf("\n  host-injected IRQ (hv_vcpu_set_pending_interrupt) with PSTATE.I = 1:\n");
        printf("    outcome %s  IRQs during the masked spin = %llu  after DAIFClr = %llu"
               "  window exits = %d\n",
               outcome_str(res->outcome), (unsigned long long)res->x[0],
               (unsigned long long)res->x[1], res->window_exits);
        printf("    => PSTATE.I masking %s ; the injected IRQ %s\n",
               res->x[0] == 0 ? "HOLDS the injected IRQ off" : "did NOT hold it off",
               res->x[1] > res->x[0] ? "lands in the guest's own vector once unmasked"
                                     : "never reached the guest");
    }
}

// ------------------------------------------------------------------------------------------- //
// Mode: exc
// ------------------------------------------------------------------------------------------- //
static void exc_one(const char *label, const uint8_t *s, const uint8_t *e, int vec_sync,
                    uint64_t N, uint64_t R, uint64_t a3, double base) {
    res_t *res = g_res;
    cfg_t c; memset(&c, 0, sizeof c);
    c.code_s = s; c.code_e = e; c.vec_def = H_REPORT; c.vec_cur_sync = vec_sync;
    c.mask_vtimer = 1; c.timeout_ms = 30000;
    c.in[0] = N; c.in[1] = R; c.in[2] = RAM_IPA + OFF_RESULT; c.in[3] = a3;
    run_cfg(&c, res);
    if (res->outcome != OUT_OK) { printf("  %-34s %s\n", label, outcome_str(res->outcome)); return; }
    stat_t st = stat_of(res->res, (int)R, (double)(N * 4));
    printf("  %-34s %8.2f ns  [%7.2f .. %7.2f]  net %7.2f ns %6.1f cyc | exceptions=%llu"
           " host exits in window=%d\n",
           label, st.med, st.lo, st.hi, st.med - base, (st.med - base) * g_ghz,
           (unsigned long long)res->x[0], res->window_exits);
}

static void mode_exc(void) {
    printf("\n=== 8. exceptions and interrupts inside the VM =================================\n");
    res_t *res = g_res;
    int ok;
    stat_t ctl = TIMED(tm_ctl, 4096, 41, 0, 0, 0, 0, 0, res, &ok);
    double base = ok ? ctl.med : 0;
    printf("  N exceptions taken at EL1 into OUR VBAR_EL1, handled and returned from in-guest.\n"
           "  'host exits in window' is the number of hv_vcpu_run returns across all of them.\n");
    printf("  loop floor (no exception): %.2f ns\n", base);
    exc_one("SVC #0 from EL1 -> VBAR+0x200", p_exc_svc_s, p_exc_svc_e, H_COUNT,
            4096, 41, 0, base);
    exc_one("permission fault -> VBAR+0x200", p_exc_abort_s, p_exc_abort_e, H_SKIP,
            1024, 41, VA_B, base);
    exc_one("undefined instruction -> VBAR", p_exc_undef_s, p_exc_undef_e, H_SKIP,
            1024, 41, 0, base);

    {
        cfg_t c; memset(&c, 0, sizeof c);
        c.code_s = p_ap_el0_drop_s; c.code_e = p_ap_el0_drop_e;
        c.el0_s = p_exc_el0_loop_s; c.el0_e = p_exc_el0_loop_e;
        c.vec_def = H_REPORT; c.vec_low_sync = H_EL0SVC; c.mask_vtimer = 1; c.timeout_ms = 20000;
        c.in[0] = VA_D;
        // the EL0 payload's own x0 comes from the same params block
        c.in[1] = 0;
        run_cfg(&c, res);
        printf("  EL0 -> EL1: %s, exceptions counted in-guest = %llu, total hv_vcpu_run returns"
               " for the whole run = %llu\n",
               outcome_str(res->outcome), (unsigned long long)res->x19,
               (unsigned long long)res->runs);
    }

    RUN(exc_daif, res, H_REPORT, 0ULL);
    if (res->outcome == OUT_OK)
        printf("  DAIF: entry=0x%llx  after DAIFClr #0xf=0x%llx  after DAIFSet #0xf=0x%llx"
               "  after DAIFClr #4 (A only)=0x%llx  ISR_EL1=0x%llx  DISR_EL1=0x%llx\n",
               (unsigned long long)res->x[0], (unsigned long long)res->x[1],
               (unsigned long long)res->x[2], (unsigned long long)res->x[3],
               (unsigned long long)res->x[4], (unsigned long long)res->x[5]);
    else verdict("DAIF", res, "");
}

// ------------------------------------------------------------------------------------------- //
// Mode: exits -- what silently costs a host exit
// ------------------------------------------------------------------------------------------- //
typedef struct { const char *name; const uint8_t *s, *e; const char *note; } xop_t;
#define X_(nm, code, note) { nm, p_##code##_s, p_##code##_e, note }

static void mode_exits(void) {
    printf("\n=== 9. host-exit catalogue =====================================================\n");
    printf("  each operation executed 8x inside a window; 'exits' counts hv_vcpu_run returns\n"
           "  strictly inside that window. 0 = the operation never leaves the VM.\n\n");
    static const xop_t OPS[] = {
        X_("NOP",              x_nop,        "control"),
        X_("ISB",              x_isb,        ""),
        X_("DSB SY",           x_dsb_sy,     ""),
        X_("DMB SY",           x_dmb_sy,     ""),
        X_("TLBI VMALLE1IS",   x_tlbi,       ""),
        X_("IC IALLU",         x_ic_iallu,   "I-cache invalidate all"),
        X_("IC IALLUIS",       x_ic_ialluis, "inner-shareable broadcast"),
        X_("IC IVAU",          x_ic_ivau,    "by VA to PoU -- the SMC path"),
        X_("DC CVAU",          x_dc_cvau,    "clean to PoU -- the SMC path"),
        X_("DC CIVAC",         x_dc_civac,   ""),
        X_("DC ISW",           x_dc_isw,     "by set/way"),
        X_("DC ZVA",           x_dc_zva,     ""),
        X_("AT S1E1R + PAR",   x_at,         ""),
        X_("MSR TTBR0_EL1",    x_msr_ttbr0,  "the address-space switch"),
        X_("MSR TCR_EL1",      x_msr_tcr,    ""),
        X_("MSR SCTLR_EL1",    x_msr_sctlr,  ""),
        X_("MSR VBAR_EL1",     x_msr_vbar,   ""),
        X_("MSR MAIR_EL1",     x_msr_mair,   ""),
        X_("MSR CONTEXTIDR",   x_msr_ctxidr, ""),
        X_("MSR TPIDR_EL1",    x_msr_tpidr,  ""),
        X_("MSR ACTLR_EL1",    x_msr_actlr,  "the TSO switch"),
        X_("MSR CPACR_EL1",    x_msr_cpacr,  ""),
        X_("MSR DAIFClr/Set",  x_msr_daif,   "interrupt masking"),
        X_("MRS CNTVCT_EL0",   x_mrs_cntvct, "the guest clock"),
        X_("MRS CNTPCT_EL0",   x_mrs_cntpct, ""),
        X_("MRS CNTFRQ_EL0",   x_mrs_cntfrq, ""),
        X_("MSR CNTV_CVAL/CTL",x_msr_cntv,   "arming the vtimer"),
        X_("MRS MIDR_EL1",     x_mrs_midr,   ""),
        X_("MRS ID_AA64MMFR0", x_mrs_mmfr0,  ""),
        X_("MRS CTR_EL0",      x_mrs_ctr,    ""),
        X_("MRS DCZID_EL0",    x_mrs_dczid,  ""),
        X_("MRS PMCCNTR_EL0",  x_mrs_pmccntr,"the cycle counter"),
        X_("MRS MDSCR_EL1",    x_mrs_mdscr,  "debug"),
        X_("MSR OSLAR_EL1",    x_msr_oslar,  "debug lock"),
        X_("MRS DBGBVR0_EL1",  x_mrs_dbgbvr, "breakpoint register"),
        X_("LDXR/STXR",        x_ldxr_stxr,  ""),
        X_("CASAL",            x_casal,      ""),
        X_("FP (FMOV/FADD)",   x_fp,         ""),
        X_("YIELD",            x_yield,      ""),
        X_("SEV",              x_sev,        ""),
        X_("WFE",              x_wfe,        ""),
        X_("WFI",              x_wfi,        ""),
        X_("BRK #0",           x_brk,        "into the guest's own vector?"),
        X_("SMC #0",           x_smc,        ""),
        X_("undefined instr",  x_undef,      "into the guest's own vector?"),
        X_("HVC #0x14",        x_hvc,        "reference: this MUST exit"),
        X_("SVC #0",           x_svc,        "into the guest's own vector"),
        X_("faulting load",    x_aborted,    "into the guest's own vector"),
    };
    int n = (int)(sizeof(OPS) / sizeof(OPS[0]));
    res_t *res = g_res;
    for (int i = 0; i < n; i++) {
        cfg_t c; memset(&c, 0, sizeof c);
        c.code_s = OPS[i].s; c.code_e = OPS[i].e;
        c.vec_def = H_SKIPREC; c.mask_vtimer = 1; c.skip_traps = 1; c.timeout_ms = 6000;
        c.in[3] = VA_A; c.in[4] = RAM_IPA + OFF_SCRATCH; c.in[5] = VA_UNMAPPED;
        run_cfg(&c, res);
        printf("  %-22s exits=%-4d guest-exceptions=%-4llu %-9s", OPS[i].name,
               res->window_exits, (unsigned long long)res->x19, outcome_str(res->outcome));
        if (res->ntrap)
            printf(" trapEC=0x%02llx (%s)", (unsigned long long)esr_ec(res->trap_esr[0]),
                   ecname(esr_ec(res->trap_esr[0])));
        if (res->x19 && res->x20)
            printf(" guestEC=0x%02llx (%s)", (unsigned long long)esr_ec(res->x20),
                   ecname(esr_ec(res->x20)));
        if (res->vtimer_exits) printf(" vtimer=%llu", (unsigned long long)res->vtimer_exits);
        if (OPS[i].note && *OPS[i].note) printf("  -- %s", OPS[i].note);
        printf("\n");
        fflush(stdout);
    }

    // How expensive is the exit we cannot avoid? Time an HVC round trip from the host.
    printf("\n  cost of the exits themselves, timed on the HOST across hv_vcpu_run:\n");
    {
        uint8_t *mem = g_mem;
        memset(mem, 0, RAM_SIZE);
        {
            // A guest that does nothing but HVC in a loop.
            uint32_t *code = (uint32_t *)(mem + OFF_BOOT);
            code[0] = 0xd4000282;   // hvc #0x14
            code[1] = 0x17ffffff;   // b .-4
            hv_vcpu_t vcpu; hv_vcpu_exit_t *vexit;
            if (hv_vcpu_create(&vcpu, &vexit, NULL) == HV_SUCCESS) {
                hv_vcpu_set_reg(vcpu, HV_REG_PC, RAM_IPA + OFF_BOOT);
                hv_vcpu_set_reg(vcpu, HV_REG_CPSR, 0x3c5);
                hv_vcpu_run(vcpu);                       // warm
                int N = 20000;
                double *v = malloc(sizeof(double) * (size_t)N);
                mach_timebase_info_data_t tb; mach_timebase_info(&tb);
                for (int i = 0; i < N; i++) {
                    uint64_t t0 = mach_absolute_time();
                    hv_vcpu_run(vcpu);
                    uint64_t t1 = mach_absolute_time();
                    v[i] = (double)(t1 - t0) * tb.numer / tb.denom;
                    uint64_t pc; hv_vcpu_get_reg(vcpu, HV_REG_PC, &pc);
                    hv_vcpu_set_reg(vcpu, HV_REG_PC, RAM_IPA + OFF_BOOT);
                }
                qsort(v, (size_t)N, sizeof(double), dcmp);
                printf("    HVC round trip (guest -> host -> guest)  median %.0f ns"
                       "  [p10 %.0f .. p90 %.0f]  min %.0f\n",
                       v[N / 2], v[N / 10], v[N - 1 - N / 10], v[0]);
                free(v);
                hv_vcpu_destroy(vcpu);
            }
        }
    }
}

// ------------------------------------------------------------------------------------------- //
int main(int argc, char **argv) {
    const char *mode = argc > 1 ? argv[1] : "all";

    g_res = calloc(1, sizeof(res_t));
    g_mem = mmap(NULL, RAM_SIZE, PROT_READ | PROT_WRITE, MAP_ANON | MAP_PRIVATE, -1, 0);
    if (!g_res || g_mem == MAP_FAILED) { perror("alloc"); return 2; }
    memset(g_mem, 0, RAM_SIZE);
    hv_return_t hr = hv_vm_create(NULL);
    if (hr != HV_SUCCESS) { fprintf(stderr, "hv_vm_create -> 0x%x (entitlement?)\n", hr); return 2; }
    hr = hv_vm_map(g_mem, RAM_IPA, RAM_SIZE, HV_MEMORY_READ | HV_MEMORY_WRITE | HV_MEMORY_EXEC);
    if (hr != HV_SUCCESS) { fprintf(stderr, "hv_vm_map -> 0x%x\n", hr); return 2; }

    printf("=== el1_features: what a guest at EL1 under Hypervisor.framework can ACTUALLY do ===\n");
    printf("    every probe runs in its own forked VM; the vCPU is put in EL1h (CPSR=0x3c5) before\n"
           "    its first MRS, and VBAR_EL1 points at our own vector table so an undef is survivable\n");

    if (!strcmp(mode, "dbg")) {
        res_t *r = g_res;
        cfg_t c; memset(&c, 0, sizeof c);
        c.code_s = p_rd_currentel_s; c.code_e = p_rd_currentel_e;
        c.vec_def = H_REPORT; c.mask_vtimer = 1; c.timeout_ms = 3000;
        run_cfg(&c, r);
        printf("dbg: outcome=%s runs=%llu PC=0x%llx SCTLR=0x%llx ESR_EL1=0x%llx ELR=0x%llx FAR=0x%llx x0=0x%llx\n",
               outcome_str(r->outcome), (unsigned long long)r->runs, (unsigned long long)r->pc,
               (unsigned long long)r->esr_el2, (unsigned long long)r->esr_el1,
               (unsigned long long)r->elr, (unsigned long long)r->far,
               (unsigned long long)r->x[0]);
        return 0;
    }

    int all = !strcmp(mode, "all");
    // `time` first: it establishes CNTFRQ and the clock estimate the other modes quote.
    if (all || !strcmp(mode, "time"))    mode_time();
    if (all || !strcmp(mode, "regs"))    mode_regs();
    if (all || !strcmp(mode, "applies")) mode_applies();
    if (all || !strcmp(mode, "feat"))    mode_feat();
    if (all || !strcmp(mode, "at"))      mode_at();
    if (all || !strcmp(mode, "tlbi"))    mode_tlbi();
    if (all || !strcmp(mode, "timer"))   mode_timer();
    if (all || !strcmp(mode, "exc"))     mode_exc();
    if (all || !strcmp(mode, "exits"))   mode_exits();
    if (!all && strcmp(mode, "time") && strcmp(mode, "regs") && strcmp(mode, "applies") &&
        strcmp(mode, "feat") && strcmp(mode, "at") && strcmp(mode, "tlbi") &&
        strcmp(mode, "timer") && strcmp(mode, "exc") && strcmp(mode, "exits")) {
        fprintf(stderr, "usage: %s [all|time|regs|applies|feat|at|tlbi|timer|exc|exits]\n", argv[0]);
        return 2;
    }
    printf("\n");
    return 0;
}
