// feat_inventory -- what ARM features are actually usable at GUEST EL1 inside a
// Hypervisor.framework VM, as opposed to merely advertised.
//
// Every probe is a tiny guest payload run at EL1 with a real 4 KB stage-1 MMU on. Each payload
// runs in its own FORKED CHILD with its own VM and a host watchdog, so a payload that wedges the
// vCPU (or that HVF refuses in a hard way) cannot take the whole sweep down.
//
// The three outcomes we care about are kept distinct:
//     OK        the instruction executed at EL1; we report the value it produced
//     TRAP_EL2  it exited to the host: ESR_EL2 EC=0x18 (sysreg trap) or 0x24 (stage-2 abort)
//     UNDEF_EL1 the CPU raised an exception into the GUEST's own vector (ESR_EL1 EC=0x00 = unknown)
// "reads as 0 without trapping" and "traps" are different facts and are reported differently.
//
// Modes:
//   all        run the whole table (default)
//   list       print the table
//   one <name> run a single probe
//   hostel0    read/write the Apple IMPDEF FP register from the HOST process at EL0 (SIGILL-guarded)
//
// Build: probes/build.sh feat_inventory/feat_inventory.c

#include <Hypervisor/Hypervisor.h>
#include <inttypes.h>
#include <setjmp.h>
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
// Guest memory map (2 MB of RAM at IPA 0x4000_0000, identity-mapped as one 2 MB block)
// ------------------------------------------------------------------------------------------- //
#define RAM_IPA   0x40000000ULL
#define RAM_SIZE  (2ULL << 20)

#define OFF_BOOT    0x00000   // boot stub (entry)
#define OFF_PARAMS  0x01000   // boot parameter block
#define OFF_VEC     0x02000   // 16 x 0x80 exception vectors
#define OFF_L0      0x04000
#define OFF_L1_ID   0x05000
#define OFF_L2_ID   0x06000
#define OFF_L1_T    0x07000
#define OFF_L2_T    0x08000
#define OFF_L3_T    0x09000
#define OFF_STACK   0x0f000
#define OFF_DATA_A  0x10000   // mapped RW  at VA_A
#define OFF_DATA_B  0x11000   // mapped RO  at VA_B
#define OFF_CODE    0x20000   // the probe payload
#define OFF_CODE0   0x21000   // secondary payload (EL0 body)
#define OFF_SCRATCH 0x30000   // 16 KB of identity-mapped Normal WB scratch

#define VA_A 0x0000400000000000ULL
#define VA_B 0x0000400000001000ULL
#define VA_UNMAPPED 0x0000400000100000ULL

#define MAGIC_A 0xAAAA0000AAAA0001ULL
#define MAGIC_B 0xBBBB0000BBBB0001ULL

// HVC immediates used by the payload protocol.
#define HVC_DONE  0x10   // final checkpoint -- host stops
#define HVC_CP    0x11   // intermediate checkpoint -- host records x0..x7 and resumes
#define HVC_EXC   0x20   // emitted by the guest's own exception vector

// ------------------------------------------------------------------------------------------- //
// Payload declaration machinery
// ------------------------------------------------------------------------------------------- //
// Each payload is emitted at file scope as its own .text blob with start/end labels; the host
// memcpy's the blob into OFF_CODE. Because each forked child writes the code exactly once, before
// its first hv_vcpu_run, there is no host-rewrites-guest-code hazard and no IC IALLU is needed.
#define PROBE(name, body)                                                          \
    __asm__(".section __TEXT,__text\n.p2align 2\n"                                 \
            ".globl _pb_" #name "_s\n_pb_" #name "_s:\n"                           \
            body                                                                   \
            "    hvc #0x10\n"                                                      \
            ".globl _pb_" #name "_e\n_pb_" #name "_e:\n");                         \
    extern const uint8_t pb_##name##_s[], pb_##name##_e[]

// Read a system register into x0.
#define RD(name, reg) PROBE(name, "    mrs x0, " reg "\n")
// Write x0 (host-supplied) to a system register, then read it back into x0.
#define WR(name, reg) PROBE(name, "    msr " reg ", x0\n    isb\n    mrs x0, " reg "\n")

// ------------------------------------------------------------------------------------------- //
// Boot stub. Runs with the MMU off at IPA==VA, turns on a 4 KB stage-1 regime, enables FP/SIMD,
// then branches to the payload. Config comes from the params page, not from registers, so the
// payload gets x0..x7 all to itself.
// ------------------------------------------------------------------------------------------- //
__asm__(
".section __TEXT,__text\n.p2align 2\n"
".globl _boot_s\n_boot_s:\n"
"    movz x28, #0x4000, lsl #16\n"      // x28 = RAM_IPA
"    add  x27, x28, #0x1000\n"          // x27 = params
"    ldp  x0, x1, [x27, #0]\n"
"    msr  mair_el1, x0\n"
"    msr  tcr_el1,  x1\n"
"    ldp  x0, x1, [x27, #16]\n"
"    msr  ttbr0_el1, x0\n"
"    msr  vbar_el1,  x1\n"
"    movz x0, #0x30, lsl #16\n"         // CPACR_EL1.FPEN = 0b11  (FP/SIMD usable at EL1 and EL0)
"    msr  cpacr_el1, x0\n"
"    isb\n"
"    tlbi vmalle1\n"
"    dsb  nsh\n"
"    isb\n"
"    mrs  x0, sctlr_el1\n"
"    movz x1, #0x1005\n"                // M | C | I
"    orr  x0, x0, x1\n"
"    msr  sctlr_el1, x0\n"
"    isb\n"
// MMU is on. Everything below is translated by our own tables.
"    add  x2, x28, #0xf000\n"
"    mov  sp, x2\n"
"    ldr  x9, [x27, #32]\n"             // payload entry
"    ldp  x0, x1, [x27, #64]\n"
"    ldp  x2, x3, [x27, #80]\n"
"    ldp  x4, x5, [x27, #96]\n"
"    ldp  x6, x7, [x27, #112]\n"
"    br   x9\n"
".globl _boot_e\n_boot_e:\n"

// Guest exception vector, replicated into all 16 slots. Captures the EL1-visible syndrome and
// reports it to the host. x0..x7 are left untouched so a payload's outputs survive a fault.
".p2align 2\n"
".globl _vec_s\n_vec_s:\n"
"    mrs  x20, esr_el1\n"
"    mrs  x21, elr_el1\n"
"    mrs  x22, far_el1\n"
"    mrs  x23, spsr_el1\n"
"    hvc  #0x20\n"
".globl _vec_e\n_vec_e:\n"
);
extern const uint8_t boot_s[], boot_e[], vec_s[], vec_e[];

// ------------------------------------------------------------------------------------------- //
// 1. ID registers
// ------------------------------------------------------------------------------------------- //
RD(id_mmfr0,  "id_aa64mmfr0_el1");
RD(id_mmfr1,  "id_aa64mmfr1_el1");
RD(id_mmfr2,  "id_aa64mmfr2_el1");
RD(id_isar0,  "id_aa64isar0_el1");
RD(id_isar1,  "id_aa64isar1_el1");
RD(id_isar2,  "S3_0_c0_c6_2");          // ID_AA64ISAR2_EL1
RD(id_pfr0,   "id_aa64pfr0_el1");
RD(id_pfr1,   "id_aa64pfr1_el1");
RD(id_dfr0,   "id_aa64dfr0_el1");
RD(id_dfr1,   "id_aa64dfr1_el1");
RD(id_afr0,   "id_aa64afr0_el1");
RD(id_zfr0,   "S3_0_c0_c4_4");          // ID_AA64ZFR0_EL1 (SVE)
RD(id_smfr0,  "S3_0_c0_c4_5");          // ID_AA64SMFR0_EL1 (SME)
RD(ctr,       "ctr_el0");
RD(dczid,     "dczid_el0");
RD(clidr,     "clidr_el1");
RD(midr,      "midr_el1");
RD(revidr,    "revidr_el1");
RD(aidr,      "aidr_el1");
RD(mpidr,     "mpidr_el1");
RD(currentel, "currentel");

// ------------------------------------------------------------------------------------------- //
// 2. The priority item: Apple IMPDEF S3_6_C15_C2_5
// ------------------------------------------------------------------------------------------- //
RD(apl_fp_rd,  "S3_6_c15_c2_5");
RD(apl_c8_3,   "S3_6_c15_c8_3");        // the other IMPDEF register XNU switches next to it
RD(actlr_rd,   "actlr_el1");
WR(apl_fp_wr,  "S3_6_c15_c2_5");        // host supplies x0, we read back
WR(apl_c8_3_wr,"S3_6_c15_c8_3");
WR(actlr_wr,   "actlr_el1");

// Does the register survive host exits (i.e. does HVF context-switch it)?
PROBE(apl_fp_ctxsw,
"    msr S3_6_c15_c2_5, x0\n"
"    isb\n"
"    mrs x1, S3_6_c15_c2_5\n"
"    hvc #0x11\n"                        // host exit + host busywork + re-entry
"    mrs x2, S3_6_c15_c2_5\n"
"    hvc #0x11\n"
"    mrs x3, S3_6_c15_c2_5\n"
);

// Does it move in response to FP operations (i.e. is it status, not control)?
// x0 = value to preload. Outputs: x1 = after ops, x2 = FPSR, x3..x6 = intermediate reads.
PROBE(apl_fp_track,
"    msr S3_6_c15_c2_5, x0\n"
"    msr fpsr, xzr\n"
"    isb\n"
"    mrs x3, S3_6_c15_c2_5\n"            // baseline
"    fmov d0, #1.0\n"
"    fmov d1, #0.0\n"
"    fdiv d2, d0, d1\n"                  // 1/0 -> DZC (FPSR bit 1), result +inf
"    mrs x4, S3_6_c15_c2_5\n"
"    fcmp d2, d1\n"                      // ordered compare, sets NZCV
"    mrs x5, S3_6_c15_c2_5\n"
"    fsqrt d3, d1\n"
"    fmov d4, #-1.0\n"
"    fsqrt d5, d4\n"                     // sqrt(-1) -> IOC (FPSR bit 0), NaN
"    fcmp d5, d5\n"                      // unordered compare (NaN) -> N=0 Z=0 C=1 V=1
"    mrs x6, S3_6_c15_c2_5\n"
"    mrs x7, nzcv\n"
"    mrs x2, fpsr\n"
"    mrs x1, S3_6_c15_c2_5\n"
);

// The decisive behavioural test. Two multiplies isolate DAZ (flush-input) from FZ (flush-result).
//   x0 = value written to S3_6_C15_C2_5   x1 = FPCR value
//   x2 = 2^-1074 (min denormal)           x3 = 2^1000
//   x4 = 2^-1000                          x5 = 2^-60
// Outputs: x0 = 2^-1074 * 2^1000  (== 2^-74, a NORMAL number, unless the input was flushed => DAZ)
//          x1 = 2^-1000 * 2^-60   (== 2^-1060, a DENORMAL, unless the result was flushed => FZ)
//          x6 = FPSR   x7 = readback of S3_6_C15_C2_5
PROBE(apl_fp_denorm,
"    msr fpcr, x1\n"
"    msr S3_6_c15_c2_5, x0\n"
"    isb\n"
"    msr fpsr, xzr\n"
"    fmov d0, x2\n"
"    fmov d1, x3\n"
"    fmul d2, d0, d1\n"
"    fmov d3, x4\n"
"    fmov d4, x5\n"
"    fmul d5, d3, d4\n"
"    mrs  x6, fpsr\n"
"    mrs  x7, S3_6_c15_c2_5\n"
"    fmov x0, d2\n"
"    fmov x1, d5\n"
);

// Same experiment driven by architectural FPCR.FZ only, as a control that the test can detect
// flushing at all on this part.
PROBE(fpcr_denorm,
"    msr fpcr, x1\n"
"    isb\n"
"    msr fpsr, xzr\n"
"    fmov d0, x2\n"
"    fmov d1, x3\n"
"    fmul d2, d0, d1\n"
"    fmov d3, x4\n"
"    fmov d4, x5\n"
"    fmul d5, d3, d4\n"
"    mrs  x6, fpsr\n"
"    mrs  x7, fpcr\n"
"    fmov x0, d2\n"
"    fmov x1, d5\n"
);

// Which bits of the register are implemented? Write all-ones, read back.
// Also: is it readable from guest EL0? (payload el0_drop + el0_body)
PROBE(el0_drop,
"    msr elr_el1, x0\n"
"    movz x2, #0x3c0\n"                  // SPSR = EL0t, AArch64, DAIF masked
"    msr spsr_el1, x2\n"
"    eret\n"
);
PROBE(el0_body,
"    mrs x0, S3_6_c15_c2_5\n"
"    mrs x1, currentel\n"
"    mrs x2, cntvct_el0\n"
"    svc #0x77\n"
);

// Apple's undocumented x86-flag hypothesis: XNU sets ACTLR_EL1 |= 0x70 (bits 4,5,6) on M1/M2 next
// to EnTSO. If those enable x86 PF/AF synthesis into NZCV[27:26], two ADDS with known x86 PF/AF
// will differ in those bits.
//   x0 = bits to OR into ACTLR_EL1, x1 = value to write to S3_6_C15_C2_5
// Outputs: x0 = ACTLR readback, x1 = NZCV after 0x0f+1 (x86 AF=1 PF=0),
//          x4 = NZCV after 1+2 (x86 AF=0 PF=1), x5 = NZCV after 0x7f+1 (x86 AF=1 PF=1 OF=1)
PROBE(x86flags,
"    mrs x6, actlr_el1\n"
"    orr x6, x6, x0\n"
"    dsb sy\n"
"    msr actlr_el1, x6\n"
"    isb\n"
"    msr S3_6_c15_c2_5, x1\n"
"    isb\n"
"    mrs x6, actlr_el1\n"
"    msr nzcv, xzr\n"
"    mov x2, #0x0f\n"
"    adds x3, x2, #1\n"
"    mrs x1, nzcv\n"
"    msr nzcv, xzr\n"
"    mov x2, #0x01\n"
"    adds x3, x2, #2\n"
"    mrs x4, nzcv\n"
"    msr nzcv, xzr\n"
"    mov x2, #0x7f\n"
"    adds x3, x2, #1\n"
"    mrs x5, nzcv\n"
"    mov x0, x6\n"
);

// ------------------------------------------------------------------------------------------- //
// 3. PMU surface
// ------------------------------------------------------------------------------------------- //
RD(pmcr,       "pmcr_el0");
RD(pmccntr,    "pmccntr_el0");
RD(pmuserenr,  "pmuserenr_el0");
RD(pmselr,     "pmselr_el0");
RD(pmcntenset, "pmcntenset_el0");
RD(pmceid0,    "pmceid0_el0");
RD(pmceid1,    "pmceid1_el0");
RD(pmovsset,   "pmovsset_el0");
RD(pmevcntr0,  "pmevcntr0_el0");
RD(pmevtyper0, "pmevtyper0_el0");
RD(pmccfiltr,  "pmccfiltr_el0");
RD(pmintenset, "pmintenset_el1");
WR(pmuserenr_wr, "pmuserenr_el0");
WR(pmcr_wr,      "pmcr_el0");
WR(pmcntenset_wr,"pmcntenset_el0");

// Apple IMPDEF PMU (names per m1n1 / Asahi).
RD(apl_pmcr0,  "S3_1_c15_c0_0");
RD(apl_pmcr1,  "S3_1_c15_c1_0");
RD(apl_pmcr2,  "S3_1_c15_c2_0");
RD(apl_pmcr3,  "S3_1_c15_c3_0");
RD(apl_pmcr4,  "S3_1_c15_c4_0");
RD(apl_pmesr0, "S3_1_c15_c5_0");
RD(apl_pmesr1, "S3_1_c15_c6_0");
RD(apl_pmsr,   "S3_1_c15_c13_0");
RD(apl_pmc0,   "S3_2_c15_c0_0");
RD(apl_pmc1,   "S3_2_c15_c1_0");
RD(apl_pmc2,   "S3_2_c15_c2_0");
RD(apl_pmc8,   "S3_2_c15_c9_0");
WR(apl_pmcr0_wr, "S3_1_c15_c0_0");

// Does PMCCNTR advance if we can read it at all? (only meaningful if the read does not trap)
PROBE(pmccntr_delta,
"    mrs x0, pmccntr_el0\n"
"    mov x2, #1000\n"
"1:  subs x2, x2, #1\n"
"    b.ne 1b\n"
"    mrs x1, pmccntr_el0\n"
"    sub  x1, x1, x0\n"
);

// ------------------------------------------------------------------------------------------- //
// 4. Counters / FEAT_ECV
// ------------------------------------------------------------------------------------------- //
RD(cntfrq,    "cntfrq_el0");
RD(cntvct,    "cntvct_el0");
RD(cntpct,    "cntpct_el0");
RD(cntvctss,  "S3_3_c14_c0_6");         // CNTVCTSS_EL0  (FEAT_ECV)
RD(cntpctss,  "S3_3_c14_c0_5");         // CNTPCTSS_EL0  (FEAT_ECV)
RD(cntkctl,   "cntkctl_el1");
RD(cntv_ctl,  "cntv_ctl_el0");
RD(cntp_ctl,  "cntp_ctl_el0");
RD(cntpoff,   "S3_4_c14_c0_6");         // CNTPOFF_EL2   (FEAT_ECV, EL2)
RD(cnthctl,   "S3_4_c14_c1_0");         // CNTHCTL_EL2
RD(cntpct_el0_2, "S3_3_c14_c0_1");      // CNTPCT_EL0, raw encoding

// ------------------------------------------------------------------------------------------- //
// 5. FEAT_TTL -- TLBI with a level hint
//   x0 = VA_A, x1 = IPA of l3_t, x2 = replacement (RO) descriptor, x3 = TTL, x4 = store value
// ------------------------------------------------------------------------------------------- //
PROBE(ttl_inv,
"    str x4, [x0]\n"                     // touch: caches a level-3 TLB entry, RW
"    str x2, [x1]\n"                     // demote the leaf to read-only
"    dsb ishst\n"
"    lsl x5, x3, #44\n"                  // TTL field is Xt[47:44]
"    lsr x6, x0, #12\n"
"    and x6, x6, #0xfffffffffff\n"       // VA[55:12] -> Xt[43:0]
"    orr x5, x5, x6\n"
"    tlbi vae1is, x5\n"
"    dsb ish\n"
"    isb\n"
"    hvc #0x11\n"                        // alive, TLBI accepted
"    str x4, [x0]\n"                     // must now take a permission fault
"    hvc #0x10\n"                        // reaching here == the entry was NOT invalidated
);

// FEAT_TLBIRANGE / FEAT_TLBIOS availability by execution.
PROBE(tlbi_range,
"    tlbi rvae1is, x0\n"
"    dsb ish\n"
"    isb\n"
"    mov x0, #1\n"
);
PROBE(tlbi_os,
"    tlbi vmalle1os\n"
"    dsb ish\n"
"    isb\n"
"    mov x0, #1\n"
);
PROBE(tlbi_nxs,
"    tlbi vmalle1isnxs\n"
"    dsb ishnxs\n"
"    isb\n"
"    mov x0, #1\n"
);

// ------------------------------------------------------------------------------------------- //
// 6. Hierarchical permissions (APTable) and FEAT_HPDS / HPDS2
//   x0 = VA_A, x1 = IPA of l2_t, x2 = bits to OR into the table descriptor, x3 = store value,
//   x4 = bits to OR into TCR_EL1 (HPD0 etc.)
// ------------------------------------------------------------------------------------------- //
PROBE(aptable,
"    mrs x5, tcr_el1\n"
"    orr x5, x5, x4\n"
"    msr tcr_el1, x5\n"
"    isb\n"
"    mrs x6, tcr_el1\n"                  // did the TCR bits stick?
"    str x3, [x0]\n"                     // baseline write, must succeed
"    ldr x7, [x1]\n"
"    orr x7, x7, x2\n"
"    str x7, [x1]\n"                     // set APTable on the L2 table descriptor
"    dsb ishst\n"
"    tlbi vmalle1is\n"
"    dsb ish\n"
"    isb\n"
"    mov x0, x6\n"
"    hvc #0x11\n"                        // checkpoint: report TCR readback
"    ldr x1, [x1]\n"
"    mov x5, x3\n"
"    movz x0, #0x4000, lsl #16\n"
"    add  x0, x0, #0x0000\n"
"    ldr  x0, [x27, #64]\n"              // reload VA_A from params
"    str  x5, [x0]\n"                    // must fault iff APTable is honoured
"    hvc #0x10\n"
);

WR(tcr_wr, "tcr_el1");

// ------------------------------------------------------------------------------------------- //
// 7. FEAT_LSE / FEAT_LSE2 / CASP
//   x0 = 16-byte aligned scratch VA
// ------------------------------------------------------------------------------------------- //
PROBE(casp_aligned,
"    mov  x6, #0x1111\n"
"    mov  x7, #0x2222\n"
"    stp  x6, x7, [x0]\n"
"    dmb  ish\n"
"    mov  x2, #0x1111\n"
"    mov  x3, #0x2222\n"                 // expected == memory, so the CAS must succeed
"    mov  x4, #0x3333\n"
"    mov  x5, #0x4444\n"
"    caspal x2, x3, x4, x5, [x0]\n"
"    ldp  x6, x7, [x0]\n"
"    mov  x1, x2\n"                      // returned old lo
);
PROBE(casp_fail,
"    mov  x6, #0x1111\n"
"    mov  x7, #0x2222\n"
"    stp  x6, x7, [x0]\n"
"    dmb  ish\n"
"    mov  x2, #0xdead\n"
"    mov  x3, #0xbeef\n"                 // expected != memory, must fail and return the old pair
"    mov  x4, #0x3333\n"
"    mov  x5, #0x4444\n"
"    caspal x2, x3, x4, x5, [x0]\n"
"    ldp  x6, x7, [x0]\n"
"    mov  x1, x2\n"
);
PROBE(casp_unaligned,
"    add  x0, x0, #8\n"                  // 8-byte aligned, NOT 16-byte aligned
"    mov  x2, #0\n"
"    mov  x3, #0\n"
"    mov  x4, #0x3333\n"
"    mov  x5, #0x4444\n"
"    caspal x2, x3, x4, x5, [x0]\n"
"    mov  x1, x2\n"
);
PROBE(cas64,
"    mov  x6, #0x55\n"
"    str  x6, [x0]\n"
"    dmb  ish\n"
"    mov  x2, #0x55\n"
"    mov  x4, #0x66\n"
"    casal x2, x4, [x0]\n"
"    ldr  x1, [x0]\n"
);
PROBE(ldadd,
"    mov  x6, #7\n"
"    str  x6, [x0]\n"
"    dmb  ish\n"
"    mov  x2, #5\n"
"    ldaddal x2, x3, [x0]\n"
"    ldr  x1, [x0]\n"
);
// FEAT_LSE2: a 16-byte LDP that straddles an 8-byte boundary but stays inside one 16-byte block
// must not fault; and an unaligned LDR crossing a 16-byte boundary must not fault either.
PROBE(lse2_unaligned,
"    add  x1, x0, #4\n"
"    ldr  x2, [x1]\n"                    // 8-byte load at +4: crosses the 8-byte boundary
"    add  x1, x0, #12\n"
"    ldr  x3, [x1]\n"                    // 8-byte load at +12: crosses the 16-byte boundary
"    add  x1, x0, #4\n"
"    ldar x4, [x0]\n"                    // aligned acquire, for contrast
"    mov  x1, #1\n"
);

// ------------------------------------------------------------------------------------------- //
// 8. FEAT_PAN2 -- AT S1E1R/W and the PAN-qualified S1E1RP/WP
//   x0 = VA_A (RW), x1 = VA_B (RO), x2 = unmapped VA
// ------------------------------------------------------------------------------------------- //
PROBE(at_plain,
"    at s1e1r, x0\n    isb\n    mrs x3, par_el1\n"
"    at s1e1w, x0\n    isb\n    mrs x4, par_el1\n"
"    at s1e1r, x1\n    isb\n    mrs x5, par_el1\n"
"    at s1e1w, x1\n    isb\n    mrs x6, par_el1\n"
"    at s1e1r, x2\n    isb\n    mrs x7, par_el1\n"
);
PROBE(at_pan,
"    at s1e1rp, x0\n   isb\n    mrs x3, par_el1\n"
"    at s1e1wp, x0\n   isb\n    mrs x4, par_el1\n"
"    at s1e1rp, x1\n   isb\n    mrs x5, par_el1\n"
"    at s1e1wp, x1\n   isb\n    mrs x6, par_el1\n"
"    at s1e1wp, x2\n   isb\n    mrs x7, par_el1\n"
);
PROBE(at_el0,
"    at s1e0r, x0\n    isb\n    mrs x3, par_el1\n"
"    at s1e0w, x0\n    isb\n    mrs x4, par_el1\n"
"    at s1e0r, x1\n    isb\n    mrs x5, par_el1\n"
"    at s1e0w, x1\n    isb\n    mrs x6, par_el1\n"
"    at s1e0r, x2\n    isb\n    mrs x7, par_el1\n"
);

// ------------------------------------------------------------------------------------------- //
// 9. Cheap sweep: FlagM / FlagM2 / JSCVT / LRCPC / PSTATE bits
// ------------------------------------------------------------------------------------------- //
PROBE(cfinv,  "    msr nzcv, x0\n    cfinv\n    mrs x0, nzcv\n");
PROBE(rmif,   "    msr nzcv, xzr\n    rmif x0, #4, #0xf\n    mrs x0, nzcv\n");
PROBE(setf8,  "    msr nzcv, xzr\n    setf8 w0\n    mrs x0, nzcv\n");
PROBE(setf16, "    msr nzcv, xzr\n    setf16 w0\n    mrs x0, nzcv\n");
PROBE(axflag, "    msr nzcv, x0\n    axflag\n    mrs x0, nzcv\n");
PROBE(xaflag, "    msr nzcv, x0\n    xaflag\n    mrs x0, nzcv\n");
PROBE(fjcvtzs,"    fmov d0, x0\n    fjcvtzs w1, d0\n    mrs x2, nzcv\n    mov x0, x1\n");
PROBE(ldapr,  "    ldapr x1, [x0]\n    mov x0, x1\n");
PROBE(ldapur, "    ldapur x1, [x0, #8]\n    mov x0, x1\n");
PROBE(stlur,  "    mov x2, #0x99\n    stlur x2, [x0, #16]\n    dmb ish\n    ldr x0, [x0, #16]\n");
PROBE(pan_bit,"    msr pan, #1\n    mrs x0, pan\n    msr pan, #0\n    mrs x1, pan\n");
PROBE(uao_bit,"    msr uao, #1\n    mrs x0, uao\n    msr uao, #0\n");
PROBE(dit_bit,"    msr dit, #1\n    mrs x0, dit\n    msr dit, #0\n");
RD(ssbs_rd,   "S3_3_c4_c2_6");           // SSBS
RD(tco_rd,    "S3_3_c4_c2_7");           // TCO (MTE)
PROBE(dczva,  "    dc zva, x0\n    ldp x1, x2, [x0]\n");
PROBE(sb_insn,"    sb\n    mov x0, #1\n");
PROBE(cpp_insn,"   cfp rctx, x0\n    dsb sy\n    isb\n    mov x0, #1\n");
PROBE(rndr_rd,"    mrs x0, S3_3_c2_c4_0\n    mrs x1, nzcv\n");  // RNDR

// ------------------------------------------------------------------------------------------- //
// Host-side plumbing
// ------------------------------------------------------------------------------------------- //
#define OUT_OK        0
#define OUT_TRAP_EL2  1
#define OUT_UNDEF_EL1 2
#define OUT_EXC_EL1   3   // guest exception that is not EC=0x00
#define OUT_ODD       4
#define OUT_TIMEOUT   5
#define OUT_CRASH     6
#define OUT_HVERR     7
#define OUT_NORESULT  8

typedef struct {
    int      outcome;
    int      ncp;
    uint32_t hv_err;
    int      hv_line;
    uint64_t esr_el2;     // syndrome at the host exit that terminated the run
    uint32_t exit_reason;
    uint64_t esr_el1, elr, far, spsr;   // from the guest's vector, if it fired
    uint64_t x[8];        // x0..x7 at the last checkpoint
    uint64_t cp[6][8];    // x0..x7 at each checkpoint
    uint16_t cpimm[6];
    uint64_t pc;
} result_t;

typedef struct {
    const char    *name;
    const uint8_t *s, *e;
    const char    *desc;
    uint64_t       in[8];
    int            slow;      // insert host busywork between checkpoints
} probe_t;

#define ENT(nm, code, d, ...) { nm, pb_##code##_s, pb_##code##_e, d, {__VA_ARGS__}, 0 }
#define ENTS(nm, code, d, ...) { nm, pb_##code##_s, pb_##code##_e, d, {__VA_ARGS__}, 1 }

// Descriptor bits (4 KB granule, VMSAv8-64 stage 1)
#define D_TABLE   (3ULL << 0)
#define D_BLOCK   (1ULL << 0)
#define D_ATTR0   (0ULL << 2)
#define D_AP_RW   (0ULL << 6)     // EL1 RW, EL0 none
#define D_AP_RW0  (1ULL << 6)     // EL1 RW, EL0 RW
#define D_AP_RO   (2ULL << 6)     // EL1 RO, EL0 none
#define D_SH_IS   (3ULL << 8)
#define D_AF      (1ULL << 10)
#define D_PXN     (1ULL << 53)
#define D_UXN     (1ULL << 54)

#define TCR_VAL (16ULL | (1ULL<<8) | (1ULL<<10) | (3ULL<<12) | (0ULL<<14) \
                | (16ULL<<16) | (1ULL<<23) | (2ULL<<30) | (2ULL<<32))
#define MAIR_VAL 0xFFULL

// Double-precision bit patterns used by the denormal experiment.
#define D_MIN_DENORM 0x0000000000000001ULL   // 2^-1074
#define D_2P1000     0x7E70000000000000ULL   // 2^1000
#define D_2PM1000    0x0170000000000000ULL   // 2^-1000   (normal)
#define D_2PM60      0x3C30000000000000ULL   // 2^-60     (normal)
#define D_2PM74      0x3B50000000000000ULL   // 2^-74     expected product #1
#define FPCR_FZ      (1ULL << 24)
#define FPCR_FIZ     (1ULL << 0)
#define FPCR_AH      (1ULL << 1)

static uint64_t esr_ec(uint64_t esr) { return (esr >> 26) & 0x3f; }

#define CHV(e) do { hv_return_t _r = (e); if (_r != HV_SUCCESS) { \
        res->outcome = OUT_HVERR; res->hv_err = _r; res->hv_line = __LINE__; _exit(0); } } while (0)

// ------------------------------------------------------------------------------------------- //
static void run_child(const probe_t *p, result_t *res) {
    uint8_t *mem = mmap(NULL, RAM_SIZE, PROT_READ | PROT_WRITE, MAP_ANON | MAP_PRIVATE, -1, 0);
    if (mem == MAP_FAILED) { res->outcome = OUT_CRASH; _exit(0); }
    memset(mem, 0, RAM_SIZE);

    CHV(hv_vm_create(NULL));
    CHV(hv_vm_map(mem, RAM_IPA, RAM_SIZE, HV_MEMORY_READ | HV_MEMORY_WRITE | HV_MEMORY_EXEC));

    memcpy(mem + OFF_BOOT, boot_s, (size_t)(boot_e - boot_s));
    size_t vlen = (size_t)(vec_e - vec_s);
    for (int i = 0; i < 16; i++) memcpy(mem + OFF_VEC + i * 0x80, vec_s, vlen);
    memcpy(mem + OFF_CODE, p->s, (size_t)(p->e - p->s));
    memcpy(mem + OFF_CODE0, pb_el0_body_s, (size_t)(pb_el0_body_e - pb_el0_body_s));

    *(uint64_t *)(mem + OFF_DATA_A)  = MAGIC_A;
    *(uint64_t *)(mem + OFF_DATA_B)  = MAGIC_B;
    for (int i = 0; i < 8; i++) *(uint64_t *)(mem + OFF_SCRATCH + i * 8) = 0x1000 + i;

    // Page tables.
    uint64_t *l0 = (uint64_t *)(mem + OFF_L0);
    uint64_t *l1_id = (uint64_t *)(mem + OFF_L1_ID), *l2_id = (uint64_t *)(mem + OFF_L2_ID);
    uint64_t *l1_t = (uint64_t *)(mem + OFF_L1_T), *l2_t = (uint64_t *)(mem + OFF_L2_T);
    uint64_t *l3_t = (uint64_t *)(mem + OFF_L3_T);

    // Identity map of guest RAM, EL1+EL0 RW and executable at both ELs (the EL0 probe needs it).
    l0[0]    = (RAM_IPA + OFF_L1_ID) | D_TABLE;
    l1_id[1] = (RAM_IPA + OFF_L2_ID) | D_TABLE;
    l2_id[0] = RAM_IPA | D_BLOCK | D_AF | D_SH_IS | D_AP_RW0 | D_ATTR0;

    // Test window at VA_A/VA_B: two adjacent 4 KB pages, RW and RO, EL1-only.
    l0[128] = (RAM_IPA + OFF_L1_T) | D_TABLE;
    l1_t[0] = (RAM_IPA + OFF_L2_T) | D_TABLE;
    l2_t[0] = (RAM_IPA + OFF_L3_T) | D_TABLE;
    l3_t[0] = (RAM_IPA + OFF_DATA_A) | D_TABLE | D_AF | D_SH_IS | D_AP_RW | D_ATTR0 | D_UXN | D_PXN;
    l3_t[1] = (RAM_IPA + OFF_DATA_B) | D_TABLE | D_AF | D_SH_IS | D_AP_RO | D_ATTR0 | D_UXN | D_PXN;

    // Boot params.
    uint64_t *pp = (uint64_t *)(mem + OFF_PARAMS);
    pp[0] = MAIR_VAL;
    pp[1] = TCR_VAL;
    pp[2] = RAM_IPA + OFF_L0;
    pp[3] = RAM_IPA + OFF_VEC;
    pp[4] = RAM_IPA + OFF_CODE;
    for (int i = 0; i < 8; i++) pp[8 + i] = p->in[i];

    hv_vcpu_t vcpu; hv_vcpu_exit_t *vexit;
    CHV(hv_vcpu_create(&vcpu, &vexit, NULL));
    CHV(hv_vcpu_set_reg(vcpu, HV_REG_PC, RAM_IPA + OFF_BOOT));
    CHV(hv_vcpu_set_reg(vcpu, HV_REG_CPSR, 0x3c5));   // EL1h, DAIF masked -- the vCPU starts at EL0

    res->outcome = OUT_NORESULT;
    for (int iter = 0; iter < 24; iter++) {
        hv_return_t r = hv_vcpu_run(vcpu);
        if (r != HV_SUCCESS) { res->outcome = OUT_HVERR; res->hv_err = r; res->hv_line = __LINE__; break; }
        hv_vcpu_get_reg(vcpu, HV_REG_PC, &res->pc);
        if (vexit->reason != HV_EXIT_REASON_EXCEPTION) {
            res->exit_reason = vexit->reason; res->outcome = OUT_ODD; break;
        }
        uint64_t sy = vexit->exception.syndrome;
        res->esr_el2 = sy;
        if (esr_ec(sy) != 0x16) {           // not an HVC -- this is a real trap out to the host
            res->outcome = OUT_TRAP_EL2; break;
        }
        uint64_t imm = sy & 0xffff;
        uint64_t regs[8];
        for (int i = 0; i < 8; i++) hv_vcpu_get_reg(vcpu, (hv_reg_t)(HV_REG_X0 + i), &regs[i]);
        if (res->ncp < 6) {
            memcpy(res->cp[res->ncp], regs, sizeof regs);
            res->cpimm[res->ncp] = (uint16_t)imm;
            res->ncp++;
        }
        memcpy(res->x, regs, sizeof regs);
        if (imm == HVC_EXC) {
            hv_vcpu_get_reg(vcpu, HV_REG_X20, &res->esr_el1);
            hv_vcpu_get_reg(vcpu, HV_REG_X21, &res->elr);
            hv_vcpu_get_reg(vcpu, HV_REG_X22, &res->far);
            hv_vcpu_get_reg(vcpu, HV_REG_X23, &res->spsr);
            res->outcome = (esr_ec(res->esr_el1) == 0x00) ? OUT_UNDEF_EL1 : OUT_EXC_EL1;
            break;
        }
        if (imm == HVC_DONE) { res->outcome = OUT_OK; break; }
        if (p->slow) { usleep(2000); }
    }
    hv_vcpu_destroy(vcpu);
    hv_vm_destroy();
    _exit(0);
}

static int run_probe(const probe_t *p, result_t *res) {
    memset(res, 0, sizeof *res);
    res->outcome = OUT_NORESULT;
    pid_t pid = fork();
    if (pid == 0) { run_child(p, res); _exit(0); }
    if (pid < 0) { res->outcome = OUT_CRASH; return -1; }
    int st = 0, waited = 0;
    for (;;) {
        pid_t w = waitpid(pid, &st, WNOHANG);
        if (w == pid) break;
        if (waited > 8000) { kill(pid, SIGKILL); waitpid(pid, &st, 0); res->outcome = OUT_TIMEOUT; return -1; }
        usleep(2000); waited += 2;
    }
    if (WIFSIGNALED(st)) { res->outcome = OUT_CRASH; return -1; }
    return 0;
}

// ------------------------------------------------------------------------------------------- //
static const char *outcome_str(int o) {
    switch (o) {
        case OUT_OK:        return "OK       ";
        case OUT_TRAP_EL2:  return "TRAP_EL2 ";
        case OUT_UNDEF_EL1: return "UNDEF_EL1";
        case OUT_EXC_EL1:   return "EXC_EL1  ";
        case OUT_ODD:       return "ODD_EXIT ";
        case OUT_TIMEOUT:   return "TIMEOUT  ";
        case OUT_CRASH:     return "CRASH    ";
        case OUT_HVERR:     return "HV_ERROR ";
        default:            return "NORESULT ";
    }
}

static void print_sysreg_iss(uint64_t esr) {
    uint64_t iss = esr & 0x1ffffff;
    printf("        ESR_EL2=0x%016llx EC=0x%02llx  MSR/MRS trap: op0=%llu op1=%llu CRn=%llu CRm=%llu op2=%llu Rt=x%llu dir=%s\n",
           (unsigned long long)esr, (unsigned long long)esr_ec(esr),
           (iss >> 20) & 3, (iss >> 14) & 7, (iss >> 10) & 0xf, (iss >> 1) & 0xf, (iss >> 17) & 7,
           (iss >> 5) & 0x1f, (iss & 1) ? "read" : "write");
}

static void report(const probe_t *p, const result_t *r, bool verbose) {
    printf("  %-18s %s  ", p->name, outcome_str(r->outcome));
    if (r->outcome == OUT_OK) {
        printf("x0=0x%016llx x1=0x%016llx\n", (unsigned long long)r->x[0], (unsigned long long)r->x[1]);
    } else if (r->outcome == OUT_TRAP_EL2) {
        printf("EC=0x%02llx\n", (unsigned long long)esr_ec(r->esr_el2));
        if (esr_ec(r->esr_el2) == 0x18) print_sysreg_iss(r->esr_el2);
        else printf("        ESR_EL2=0x%016llx PC=0x%llx\n",
                    (unsigned long long)r->esr_el2, (unsigned long long)r->pc);
    } else if (r->outcome == OUT_UNDEF_EL1 || r->outcome == OUT_EXC_EL1) {
        printf("ESR_EL1=0x%016llx EC=0x%02llx FAR=0x%llx ELR=0x%llx\n",
               (unsigned long long)r->esr_el1, (unsigned long long)esr_ec(r->esr_el1),
               (unsigned long long)r->far, (unsigned long long)r->elr);
    } else if (r->outcome == OUT_HVERR) {
        printf("hv_return=0x%x at line %d\n", r->hv_err, r->hv_line);
    } else {
        printf("exit_reason=%u PC=0x%llx\n", r->exit_reason, (unsigned long long)r->pc);
    }
    if (verbose) {
        for (int i = 0; i < r->ncp; i++) {
            printf("        cp[%d] hvc#0x%02x:", i, r->cpimm[i]);
            for (int j = 0; j < 8; j++) printf(" x%d=0x%llx", j, (unsigned long long)r->cp[i][j]);
            printf("\n");
        }
    }
    printf("        (%s)\n", p->desc);
}

// ------------------------------------------------------------------------------------------- //
// The probe table
// ------------------------------------------------------------------------------------------- //
static const probe_t TABLE[] = {
    // --- ID registers ---
    ENT("id_mmfr0",  id_mmfr0,  "ID_AA64MMFR0_EL1: ECV[63:60], TGran, PARange, ASIDBits"),
    ENT("id_mmfr1",  id_mmfr1,  "ID_AA64MMFR1_EL1: HPDS[15:12], PAN[23:20], AFP[47:44], HAFDBS"),
    ENT("id_mmfr2",  id_mmfr2,  "ID_AA64MMFR2_EL1: TTL[51:48], BBM[55:52], AT[35:32]=LSE2, VARange"),
    ENT("id_isar0",  id_isar0,  "ID_AA64ISAR0_EL1: Atomic[23:20]=LSE, TS[55:52]=FlagM, TLB[59:56]"),
    ENT("id_isar1",  id_isar1,  "ID_AA64ISAR1_EL1: JSCVT[15:12], LRCPC[23:20], XS[59:56]"),
    ENT("id_isar2",  id_isar2,  "ID_AA64ISAR2_EL1: MOPS, CLRBHB, ..."),
    ENT("id_pfr0",   id_pfr0,   "ID_AA64PFR0_EL1: FP, AdvSIMD, EL levels, SVE, DIT, CSV2/3, AMU"),
    ENT("id_pfr1",   id_pfr1,   "ID_AA64PFR1_EL1: BT, SSBS, MTE"),
    ENT("id_dfr0",   id_dfr0,   "ID_AA64DFR0_EL1: PMUVer[11:8], BRPs, WRPs, PMSVer"),
    ENT("id_dfr1",   id_dfr1,   "ID_AA64DFR1_EL1"),
    ENT("id_afr0",   id_afr0,   "ID_AA64AFR0_EL1 (IMPDEF feature register)"),
    ENT("id_zfr0",   id_zfr0,   "ID_AA64ZFR0_EL1 (SVE)"),
    ENT("id_smfr0",  id_smfr0,  "ID_AA64SMFR0_EL1 (SME)"),
    ENT("ctr",       ctr,       "CTR_EL0: cache line sizes, DIC/IDC"),
    ENT("dczid",     dczid,     "DCZID_EL0: DC ZVA block size / prohibited"),
    ENT("clidr",     clidr,     "CLIDR_EL1"),
    ENT("midr",      midr,      "MIDR_EL1 (HVF masks the part number)"),
    ENT("revidr",    revidr,    "REVIDR_EL1"),
    ENT("aidr",      aidr,      "AIDR_EL1 (bit 9 = Apple TSO capable)"),
    ENT("mpidr",     mpidr,     "MPIDR_EL1"),
    ENT("currentel", currentel, "CurrentEL -- confirms we really are at EL1 (0x4)"),

    // --- the priority item ---
    ENT("apl_fp_rd",   apl_fp_rd,  "S3_6_C15_C2_5 read at guest EL1"),
    ENT("apl_c8_3",    apl_c8_3,   "S3_6_C15_C8_3 read at guest EL1 (the other IMPDEF XNU switches)"),
    ENT("actlr_rd",    actlr_rd,   "ACTLR_EL1 read"),
    ENT("apl_fp_wr0",  apl_fp_wr,  "S3_6_C15_C2_5 <- 0, read back", 0x0),
    ENT("apl_fp_wr1",  apl_fp_wr,  "S3_6_C15_C2_5 <- 1 (DAZ?), read back", 0x1),
    ENT("apl_fp_wr2",  apl_fp_wr,  "S3_6_C15_C2_5 <- 2 (FZ?), read back", 0x2),
    ENT("apl_fp_wr3",  apl_fp_wr,  "S3_6_C15_C2_5 <- 3, read back", 0x3),
    ENT("apl_fp_wrf",  apl_fp_wr,  "S3_6_C15_C2_5 <- all ones: which bits are implemented?",
                                    0xFFFFFFFFFFFFFFFFULL),
    ENT("apl_c8_3_wr", apl_c8_3_wr,"S3_6_C15_C8_3 <- 6, read back", 0x6),
    ENT("actlr_wr",    actlr_wr,   "ACTLR_EL1 <- 0x72 (EnTSO + XNU's unexplained bits 4,5,6)", 0x72),
    ENTS("apl_fp_ctxsw", apl_fp_ctxsw,
         "does S3_6_C15_C2_5 survive host exits (is it context-switched by HVF)?", 0x3),
    ENT("apl_fp_track",  apl_fp_track,
        "does S3_6_C15_C2_5 move in response to FP ops (status) or stay put (control)?", 0x0),

    // the decisive DAZ/FZ behavioural matrix
    ENT("fp_base",   apl_fp_denorm, "reg=0 FPCR=0 : baseline, nothing flushed",
        0x0, 0x0, D_MIN_DENORM, D_2P1000, D_2PM1000, D_2PM60),
    ENT("fp_reg1",   apl_fp_denorm, "reg=1 FPCR=0 : bit0 == x86 DAZ?",
        0x1, 0x0, D_MIN_DENORM, D_2P1000, D_2PM1000, D_2PM60),
    ENT("fp_reg2",   apl_fp_denorm, "reg=2 FPCR=0 : bit1 == x86 FZ?",
        0x2, 0x0, D_MIN_DENORM, D_2P1000, D_2PM1000, D_2PM60),
    ENT("fp_reg3",   apl_fp_denorm, "reg=3 FPCR=0 : both",
        0x3, 0x0, D_MIN_DENORM, D_2P1000, D_2PM1000, D_2PM60),
    ENT("fp_fpcrfz", fpcr_denorm,   "control: FPCR.FZ only (architectural flush-to-zero)",
        0x0, FPCR_FZ, D_MIN_DENORM, D_2P1000, D_2PM1000, D_2PM60),
    ENT("fp_fpcrfiz",fpcr_denorm,   "control: FPCR bit0 (FIZ, RES0 without FEAT_AFP)",
        0x0, FPCR_FIZ, D_MIN_DENORM, D_2P1000, D_2PM1000, D_2PM60),
    ENT("fp_fpcrah", fpcr_denorm,   "control: FPCR bit1 (AH, RES0 without FEAT_AFP)",
        0x0, FPCR_AH, D_MIN_DENORM, D_2P1000, D_2PM1000, D_2PM60),
    ENT("fp_none",   fpcr_denorm,   "control: FPCR=0, no register writes at all",
        0x0, 0x0, D_MIN_DENORM, D_2P1000, D_2PM1000, D_2PM60),

    ENT("el0_apl_fp", el0_drop, "read S3_6_C15_C2_5 from guest EL0 (Rosetta's vantage point)",
        RAM_IPA + OFF_CODE0),

    ENT("x86flags_0",  x86flags, "NZCV[27:26] after ADDS, ACTLR untouched", 0x0, 0x0),
    ENT("x86flags_70", x86flags, "NZCV[27:26] after ADDS, ACTLR |= 0x70 (XNU's M1/M2 bits)", 0x70, 0x0),
    ENT("x86flags_72", x86flags, "NZCV[27:26] after ADDS, ACTLR |= 0x72 (+EnTSO)", 0x72, 0x0),
    ENT("x86flags_r3", x86flags, "NZCV[27:26] after ADDS, ACTLR |= 0x72 and IMPDEF reg = 3", 0x72, 0x3),

    // --- PMU ---
    ENT("pmcr",       pmcr,       "PMCR_EL0 read"),
    ENT("pmccntr",    pmccntr,    "PMCCNTR_EL0 read (doc 16 says this traps)"),
    ENT("pmuserenr",  pmuserenr,  "PMUSERENR_EL0 read"),
    ENT("pmselr",     pmselr,     "PMSELR_EL0 read"),
    ENT("pmcntenset", pmcntenset, "PMCNTENSET_EL0 read"),
    ENT("pmceid0",    pmceid0,    "PMCEID0_EL0 read (which events exist)"),
    ENT("pmceid1",    pmceid1,    "PMCEID1_EL0 read"),
    ENT("pmovsset",   pmovsset,   "PMOVSSET_EL0 read"),
    ENT("pmevcntr0",  pmevcntr0,  "PMEVCNTR0_EL0 read"),
    ENT("pmevtyper0", pmevtyper0, "PMEVTYPER0_EL0 read"),
    ENT("pmccfiltr",  pmccfiltr,  "PMCCFILTR_EL0 read"),
    ENT("pmintenset", pmintenset, "PMINTENSET_EL1 read"),
    ENT("pmuserenr_w",pmuserenr_wr,"PMUSERENR_EL0 <- 0xf", 0xf),
    ENT("pmcr_w",     pmcr_wr,    "PMCR_EL0 <- 0x7 (E|P|C: enable+reset)", 0x7),
    ENT("pmcnten_w",  pmcntenset_wr,"PMCNTENSET_EL0 <- 0x80000001", 0x80000001ULL),
    ENT("pmccntr_d",  pmccntr_delta,"does PMCCNTR_EL0 advance over a 1000-iteration loop?"),
    ENT("apl_pmcr0",  apl_pmcr0,  "Apple IMPDEF PMCR0 S3_1_C15_C0_0"),
    ENT("apl_pmcr1",  apl_pmcr1,  "Apple IMPDEF PMCR1 S3_1_C15_C1_0"),
    ENT("apl_pmcr2",  apl_pmcr2,  "Apple IMPDEF PMCR2 S3_1_C15_C2_0"),
    ENT("apl_pmcr3",  apl_pmcr3,  "Apple IMPDEF PMCR3 S3_1_C15_C3_0"),
    ENT("apl_pmcr4",  apl_pmcr4,  "Apple IMPDEF PMCR4 S3_1_C15_C4_0"),
    ENT("apl_pmesr0", apl_pmesr0, "Apple IMPDEF PMESR0 S3_1_C15_C5_0"),
    ENT("apl_pmesr1", apl_pmesr1, "Apple IMPDEF PMESR1 S3_1_C15_C6_0"),
    ENT("apl_pmsr",   apl_pmsr,   "Apple IMPDEF PMSR S3_1_C15_C13_0"),
    ENT("apl_pmc0",   apl_pmc0,   "Apple IMPDEF PMC0 S3_2_C15_C0_0 (cycle counter)"),
    ENT("apl_pmc1",   apl_pmc1,   "Apple IMPDEF PMC1 S3_2_C15_C1_0 (instruction counter)"),
    ENT("apl_pmc2",   apl_pmc2,   "Apple IMPDEF PMC2 S3_2_C15_C2_0"),
    ENT("apl_pmc8",   apl_pmc8,   "Apple IMPDEF PMC8 S3_2_C15_C9_0"),
    ENT("apl_pmcr0_w",apl_pmcr0_wr,"Apple IMPDEF PMCR0 <- 0x3003ff (enable PMC0-9)", 0x3003ffULL),

    // --- counters / ECV ---
    ENT("cntfrq",   cntfrq,   "CNTFRQ_EL0"),
    ENT("cntvct",   cntvct,   "CNTVCT_EL0"),
    ENT("cntpct",   cntpct,   "CNTPCT_EL0 (09 N-04 claims this traps)"),
    ENT("cntvctss", cntvctss, "CNTVCTSS_EL0 (FEAT_ECV self-synchronised virtual counter)"),
    ENT("cntpctss", cntpctss, "CNTPCTSS_EL0 (FEAT_ECV self-synchronised physical counter)"),
    ENT("cntkctl",  cntkctl,  "CNTKCTL_EL1"),
    ENT("cntv_ctl", cntv_ctl, "CNTV_CTL_EL0"),
    ENT("cntp_ctl", cntp_ctl, "CNTP_CTL_EL0"),
    ENT("cntpoff",  cntpoff,  "CNTPOFF_EL2 (FEAT_ECV) -- EL2 register, expected unreachable"),
    ENT("cnthctl",  cnthctl,  "CNTHCTL_EL2 -- EL2 register, expected unreachable"),

    // --- FEAT_TTL / TLBI ---
    ENT("ttl_0", ttl_inv, "TLBI VAE1IS with TTL=0 (no hint) -- control",
        VA_A, RAM_IPA + OFF_L3_T,
        (RAM_IPA + OFF_DATA_A) | D_TABLE | D_AF | D_SH_IS | D_AP_RO | D_UXN | D_PXN, 0x0, 0x5A5A5A5A),
    ENT("ttl_l3", ttl_inv, "TLBI VAE1IS with TTL=0b0111 (4 KB, level 3) -- the correct hint",
        VA_A, RAM_IPA + OFF_L3_T,
        (RAM_IPA + OFF_DATA_A) | D_TABLE | D_AF | D_SH_IS | D_AP_RO | D_UXN | D_PXN, 0x7, 0x5A5A5A5A),
    ENT("ttl_l2", ttl_inv, "TLBI VAE1IS with TTL=0b0110 (4 KB, level 2) -- deliberately wrong level",
        VA_A, RAM_IPA + OFF_L3_T,
        (RAM_IPA + OFF_DATA_A) | D_TABLE | D_AF | D_SH_IS | D_AP_RO | D_UXN | D_PXN, 0x6, 0x5A5A5A5A),
    ENT("ttl_l1", ttl_inv, "TLBI VAE1IS with TTL=0b0101 (4 KB, level 1) -- deliberately wrong level",
        VA_A, RAM_IPA + OFF_L3_T,
        (RAM_IPA + OFF_DATA_A) | D_TABLE | D_AF | D_SH_IS | D_AP_RO | D_UXN | D_PXN, 0x5, 0x5A5A5A5A),
    ENT("tlbi_range", tlbi_range, "TLBI RVAE1IS (FEAT_TLBIRANGE) executes at EL1?", VA_A >> 12),
    ENT("tlbi_os",    tlbi_os,    "TLBI VMALLE1OS (FEAT_TLBIOS) executes at EL1?"),
    ENT("tlbi_nxs",   tlbi_nxs,   "TLBI ...nXS + DSB nXS (FEAT_XS) execute at EL1?"),

    // --- hierarchical permissions ---
    ENT("aptable_on", aptable, "APTable=0b10 on the L2 table descriptor, TCR.HPD0=0",
        VA_A, RAM_IPA + OFF_L2_T, (2ULL << 61), 0x5A5A5A5A, 0x0),
    ENT("aptable_hpd", aptable, "same, but TCR_EL1.HPD0=1 first (FEAT_HPDS should make it ignored)",
        VA_A, RAM_IPA + OFF_L2_T, (2ULL << 61), 0x5A5A5A5A, (1ULL << 41)),
    ENT("aptable_xn", aptable, "PXNTable=1 on the L2 table descriptor (execute-never subtree)",
        VA_A, RAM_IPA + OFF_L2_T, (1ULL << 59), 0x5A5A5A5A, 0x0),
    ENT("tcr_hpd_hwu", tcr_wr, "TCR_EL1 <- base | HPD0|HPD1|HWU059..162: which bits stick?",
        TCR_VAL | (0xFFFULL << 41)),

    // --- atomics ---
    ENT("casp_ok",   casp_aligned,  "CASPAL, 16-byte aligned, expected==memory: must swap",
        RAM_IPA + OFF_SCRATCH),
    ENT("casp_fail", casp_fail,     "CASPAL, expected!=memory: must NOT swap, returns old pair",
        RAM_IPA + OFF_SCRATCH),
    ENT("casp_unal", casp_unaligned,"CASPAL at an 8-byte-aligned (not 16) address",
        RAM_IPA + OFF_SCRATCH),
    ENT("cas64",     cas64,         "CASAL 64-bit", RAM_IPA + OFF_SCRATCH),
    ENT("ldadd",     ldadd,         "LDADDAL (FEAT_LSE base)", RAM_IPA + OFF_SCRATCH),
    ENT("lse2_unal", lse2_unaligned,"unaligned 8-byte loads within and across a 16-byte block",
        RAM_IPA + OFF_SCRATCH),

    // --- AT ---
    ENT("at_plain", at_plain, "AT S1E1R/W on RW page, RO page, unmapped VA", VA_A, VA_B, VA_UNMAPPED),
    ENT("at_pan",   at_pan,   "AT S1E1RP/WP (FEAT_PAN2) on the same three", VA_A, VA_B, VA_UNMAPPED),
    ENT("at_el0",   at_el0,   "AT S1E0R/W -- the pages are EL1-only, so these must all fault",
        VA_A, VA_B, VA_UNMAPPED),

    // --- cheap sweep ---
    ENT("cfinv",   cfinv,   "CFINV (FEAT_FlagM): input NZCV=0x20000000 (C=1), expect C cleared",
        0x20000000ULL),
    ENT("rmif",    rmif,    "RMIF (FeAT_FlagM)", 0xF0F0F0F0F0F0F0F0ULL),
    ENT("setf8",   setf8,   "SETF8 (FEAT_FlagM)", 0x80ULL),
    ENT("setf16",  setf16,  "SETF16 (FEAT_FlagM)", 0x8000ULL),
    ENT("axflag",  axflag,  "AXFLAG (FEAT_FlagM2)", 0x30000000ULL),
    ENT("xaflag",  xaflag,  "XAFLAG (FEAT_FlagM2)", 0x30000000ULL),
    ENT("fjcvtzs", fjcvtzs, "FJCVTZS (FEAT_JSCVT): 1.5 -> 1", 0x3FF8000000000000ULL),
    ENT("ldapr",   ldapr,   "LDAPR (FEAT_LRCPC)", RAM_IPA + OFF_SCRATCH),
    ENT("ldapur",  ldapur,  "LDAPUR (FEAT_LRCPC2)", RAM_IPA + OFF_SCRATCH),
    ENT("stlur",   stlur,   "STLUR (FEAT_LRCPC2)", RAM_IPA + OFF_SCRATCH),
    ENT("pan_bit", pan_bit, "MSR PAN,#1 / MRS PAN (FEAT_PAN)"),
    ENT("uao_bit", uao_bit, "MSR UAO,#1 / MRS UAO (FEAT_UAO)"),
    ENT("dit_bit", dit_bit, "MSR DIT,#1 / MRS DIT (FEAT_DIT)"),
    ENT("ssbs",    ssbs_rd, "MRS SSBS"),
    ENT("tco",     tco_rd,  "MRS TCO (FEAT_MTE)"),
    ENT("dczva",   dczva,   "DC ZVA on scratch", RAM_IPA + OFF_SCRATCH),
    ENT("sb",      sb_insn, "SB (FEAT_SB)"),
    ENT("cfp",     cpp_insn,"CFP RCTX (FEAT_SPECRES)", 0x0),
    ENT("rndr",    rndr_rd, "MRS RNDR (FEAT_RNG)"),
};
#define NTABLE (int)(sizeof(TABLE) / sizeof(TABLE[0]))

// ------------------------------------------------------------------------------------------- //
// Host-side EL0 test of the Apple IMPDEF register (doc 04 claims Rosetta reads it from EL0)
// ------------------------------------------------------------------------------------------- //
static sigjmp_buf jb;
static void sigill(int s) { (void)s; siglongjmp(jb, 1); }

static int host_el0(void) {
    struct sigaction sa, old4, old10;
    memset(&sa, 0, sizeof sa);
    sa.sa_handler = sigill;
    sigaction(SIGILL, &sa, &old4);
    sigaction(SIGBUS, &sa, &old10);

    printf("\n=== host process, EL0 (macOS userspace, no VM) ===\n");
    uint64_t v = 0;
    if (sigsetjmp(jb, 1) == 0) {
        __asm__ volatile("mrs %0, S3_6_c15_c2_5" : "=r"(v));
        printf("  MRS S3_6_C15_C2_5      -> OK,   value = 0x%016llx\n", (unsigned long long)v);
    } else {
        printf("  MRS S3_6_C15_C2_5      -> SIGILL (not EL0-readable in a macOS process)\n");
    }
    if (sigsetjmp(jb, 1) == 0) {
        uint64_t w = 3;
        __asm__ volatile("msr S3_6_c15_c2_5, %0\n isb\n mrs %0, S3_6_c15_c2_5" : "+r"(w));
        printf("  MSR S3_6_C15_C2_5 <- 3 -> OK,   read back 0x%016llx\n", (unsigned long long)w);
        uint64_t z = 0;
        __asm__ volatile("msr S3_6_c15_c2_5, %0" :: "r"(z));
    } else {
        printf("  MSR S3_6_C15_C2_5 <- 3 -> SIGILL (not EL0-writable in a macOS process)\n");
    }
    if (sigsetjmp(jb, 1) == 0) {
        __asm__ volatile("mrs %0, S3_6_c15_c8_3" : "=r"(v));
        printf("  MRS S3_6_C15_C8_3      -> OK,   value = 0x%016llx\n", (unsigned long long)v);
    } else {
        printf("  MRS S3_6_C15_C8_3      -> SIGILL\n");
    }
    if (sigsetjmp(jb, 1) == 0) {
        __asm__ volatile("mrs %0, id_aa64mmfr1_el1" : "=r"(v));
        printf("  MRS ID_AA64MMFR1_EL1   -> OK,   value = 0x%016llx (AFP field = %llu)\n",
               (unsigned long long)v, (unsigned long long)((v >> 44) & 0xf));
    } else {
        printf("  MRS ID_AA64MMFR1_EL1   -> SIGILL (macOS does not enable EL0 ID-register emulation)\n");
    }
    if (sigsetjmp(jb, 1) == 0) {
        __asm__ volatile("mrs %0, pmccntr_el0" : "=r"(v));
        printf("  MRS PMCCNTR_EL0        -> OK,   value = 0x%016llx\n", (unsigned long long)v);
    } else {
        printf("  MRS PMCCNTR_EL0        -> SIGILL\n");
    }
    // Does the host's own FP behave as if DAZ/FZ are off, and can we change it?
    {
        volatile double a = 0.0, b = 0.0, r1 = 0.0, r2 = 0.0;
        uint64_t ba = D_MIN_DENORM, bb = D_2P1000;
        memcpy((void *)&a, &ba, 8); memcpy((void *)&b, &bb, 8);
        r1 = a * b;
        uint64_t bc = D_2PM1000, bd = D_2PM60;
        memcpy((void *)&a, &bc, 8); memcpy((void *)&b, &bd, 8);
        r2 = a * b;
        uint64_t o1, o2; memcpy(&o1, (void *)&r1, 8); memcpy(&o2, (void *)&r2, 8);
        printf("  host FP: denorm*2^1000 = 0x%016llx (expect 0x%016llx if no DAZ)\n",
               (unsigned long long)o1, (unsigned long long)D_2PM74);
        printf("  host FP: 2^-1000*2^-60 = 0x%016llx (expect 0x0000400000000000 if no FZ)\n",
               (unsigned long long)o2);
    }
    sigaction(SIGILL, &old4, NULL);
    sigaction(SIGBUS, &old10, NULL);
    return 0;
}

// ------------------------------------------------------------------------------------------- //
int main(int argc, char **argv) {
    const char *mode = argc > 1 ? argv[1] : "all";
    bool verbose = false;
    for (int i = 1; i < argc; i++) if (!strcmp(argv[i], "-v")) verbose = true;

    if (!strcmp(mode, "list")) {
        for (int i = 0; i < NTABLE; i++) printf("%-18s %s\n", TABLE[i].name, TABLE[i].desc);
        return 0;
    }
    if (!strcmp(mode, "hostel0")) return host_el0();

    result_t *res = mmap(NULL, sizeof(result_t), PROT_READ | PROT_WRITE,
                         MAP_SHARED | MAP_ANON, -1, 0);
    if (res == MAP_FAILED) { perror("mmap"); return 2; }

    printf("=== feat_inventory: what is reachable at GUEST EL1 under Hypervisor.framework ===\n");
    printf("    %d probes, each in its own forked VM with an 8 s watchdog\n\n", NTABLE);

    int only = -1;
    if (!strcmp(mode, "one")) {
        if (argc < 3) { fprintf(stderr, "usage: %s one <name>\n", argv[0]); return 2; }
        for (int i = 0; i < NTABLE; i++) if (!strcmp(TABLE[i].name, argv[2])) only = i;
        if (only < 0) { fprintf(stderr, "no such probe: %s\n", argv[2]); return 2; }
        verbose = true;
    }

    for (int i = 0; i < NTABLE; i++) {
        if (only >= 0 && i != only) continue;
        run_probe(&TABLE[i], res);
        report(&TABLE[i], res, verbose);
        fflush(stdout);
    }
    return 0;
}
