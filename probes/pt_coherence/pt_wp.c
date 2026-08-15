// pt_wp.c -- what write-protecting the guest's own x86 page tables costs, measured in-VM at EL1.
//
// The design question (doc 20): to learn that the guest modified its own x86 page tables we can
// either (a) map the guest's PT pages read-only in the shadow stage-1 tables and trap every write,
// or (b) leave them writable and resync lazily on INVLPG / MOV-CR3.  (b) is nearly free; (a) costs
// a trap-emulate-reflect cycle per guest PTE store.  This probe measures (a)'s inner loop.
//
// Modes
// -----
//   cal    CNTVCT granularity, loop floor, clock estimate (same method as probes/shadow_mmu)
//   isv    what ESR_EL1 actually tells the handler about a faulting store at EL1 --
//          in particular whether ISV/SAS/SRT are populated, which decides whether the handler
//          can skip fetching and decoding the faulting instruction
//   wp     incremental cost ladder for the write-protect handler:
//            0 bare permission fault + ERET                       (the floor, cf. 16 sec.1)
//            1 + read ESR_EL1/FAR_EL1, check DFSC and WnR
//            2 + fetch the faulting instruction and decode Rt, read the guest register
//            3 + apply the store to the guest PT page via a writable alias
//            4 + rmap lookup: PT page frame -> shadow L3 table + shadow VA base
//            5 + synthesise the ARM descriptor and store it into the shadow  (INSTALL case)
//            6 + DSB/TLBI VAAE1IS/DSB/ISB                                    (MODIFY case)
//          each over two address patterns: 512 consecutive PTE slots in ONE PT page
//          (fork / page-table copy) and one slot in each of 512 PT pages (scattered).
//   burst  the alternative to trapping every store: on the first fault, unprotect the PT page,
//          let the guest run natively, then re-protect and re-derive the whole page's shadow.
//          Sweeps the burst length N and finds the break-even against N individual traps.
//
// Timing is CNTVCT_EL0 read in-guest, so no host exit occurs inside a measured window.
//
// Build: ../build.sh pt_coherence/pt_wp.c
// Run:   ./bin/pt_wp all        (or: cal / isv / wp / burst)

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

// ------------------------------------------------------------------ memory map
#define RAM_IPA        0x40000000ULL
#define RAM_SIZE       0x04000000ULL          // 64 MiB

#define OFF_CODE       0x00000000ULL
#define OFF_VEC        0x00020000ULL
#define OFF_PARAM      0x00021000ULL          // 4 KiB param/result block
#define OFF_RMAP       0x00022000ULL          // 1024 x 32 B rmap buckets  (param + 0x1000)
#define OFF_GPR        0x0002a000ULL          // guest GPR spill frame     (param + 0x9000)
#define OFF_ARENA      0x00100000ULL          // stage-1 table arena
#define OFF_ARENA_END  0x00e00000ULL
#define OFF_PTLEAF     0x00e00000ULL          // 512 "x86 PT pages", write-protected  (2 MiB)
#define OFF_PTCTRL     0x01000000ULL          // 512 identical pages, writable (control)
#define OFF_TARGET     0x01200000ULL          // frames the fake x86 PTEs point at

#define NPT            512                    // PT pages / PTE slots per page

// guest VAs
#define VA_PTRO        0x0000400000000000ULL  // read-only alias of the PT pages: the guest's view
#define VA_PTRW        0x0000401000000000ULL  // writable control region, same geometry
#define VA_SHADOW      0x0000402000000000ULL  // region whose L3 tables the handler writes into

// descriptor bits
#define D_PAGE   3ULL
#define D_BLOCK  1ULL
#define D_AF     (1ULL << 10)
#define D_SH_IS  (3ULL << 8)
#define D_AP_RO  (2ULL << 6)
#define D_UXN    (1ULL << 54)
#define D_PXN    (1ULL << 53)
#define PA_MASK  0x0000fffffffff000ULL

#define MAIR_VAL 0xFFULL
// T0SZ=16, IRGN0/ORGN0=WB, SH0=IS, TG0=4K, T1SZ=16, EPD1=1, TG1=4K, IPS=40-bit
#define TCR_4K   (16ULL | (1ULL<<8) | (1ULL<<10) | (3ULL<<12) | (0ULL<<14) \
                 | (16ULL<<16) | (1ULL<<23) | (2ULL<<30) | (2ULL<<32))

// a plausible x86-64 leaf PTE: P|RW|U|A|D, output = OFF_TARGET
#define PTEVAL   ((RAM_IPA + OFF_TARGET) | 0x67ULL)

#define HV(e) do { hv_return_t _r = (e); if (_r != HV_SUCCESS) { \
    fprintf(stderr, "  %s -> 0x%x at %d\n", #e, _r, __LINE__); exit(2);} } while (0)

// ------------------------------------------------------------------ guest payload
//
// Register discipline
//   x28  param block VA            x18  guest GPR spill frame VA
//   x19  fault counter             x20  fault limit (panic past it)
//   x21  VA_PTRO -> writable-alias delta
//   x22  rmap hash mask            x23  output-address mask
//   x24  base ARM stage-1 attributes for a synthesised descriptor
//   x25,x26,x27,x2,x3  handler scratch  (measurement bodies must not rely on these)
//   Ltimeit owns x7,x9,x11,x13,x14,x16,x17,x30; bodies own x0,x1,x4,x5,x6,x8,x10,x12,x15.
//
__asm__(
".section __TEXT,__text\n"
".p2align 2\n"
".globl _pw_start\n"
"_pw_start:\n"
"    msr  mair_el1, x0\n"
"    msr  tcr_el1,  x1\n"
"    msr  ttbr0_el1, x2\n"
"    msr  vbar_el1, x3\n"
"    isb\n"
"    tlbi vmalle1\n"
"    dsb  nsh\n"
"    isb\n"
"    mrs  x9, sctlr_el1\n"
"    mov  x10, #0x1005\n"                     // M | C | I
"    orr  x9, x9, x10\n"
"    msr  sctlr_el1, x9\n"
"    isb\n"
"    ic   iallu\n"                            // the host rewrites vectors between runs (16 sec.1)
"    dsb  nsh\n"
"    isb\n"
"    mov  x28, x4\n"
"    ldr  x19, [x28, #0x1c0]\n"
"    ldr  x20, [x28, #0x1c8]\n"
"    ldr  x21, [x28, #0x1d0]\n"
"    ldr  x22, [x28, #0x1d8]\n"
"    ldr  x23, [x28, #0x1e0]\n"
"    ldr  x24, [x28, #0x1e8]\n"
"    ldr  x18, [x28, #0x1f0]\n"
"    mrs  x0, cntfrq_el0\n"
"    str  x0, [x28, #0xc18]\n"
"    cmp  x5, #0\n"
"    b.eq Lm_cal\n"
"    cmp  x5, #1\n"
"    b.eq Lm_isv\n"
"    cmp  x5, #2\n"
"    b.eq Lm_wp\n"
"    cmp  x5, #3\n"
"    b.eq Lm_burst\n"
"    hvc  #9\n"

// ---------------- Ltimeit: x7=body x9=rounds x11=result cursor ----------------
"Ltimeit:\n"
"    mov  x13, x30\n"
"    mov  x14, x9\n"
"1:  isb\n"
"    mrs  x16, cntvct_el0\n"
"    blr  x7\n"
"    isb\n"
"    mrs  x17, cntvct_el0\n"
"    sub  x17, x17, x16\n"
"    str  x17, [x11], #8\n"
"    subs x14, x14, #1\n"
"    b.ne 1b\n"
"    mov  x30, x13\n"
"    ret\n"

// ---------------- mode 0: calibration ----------------
"Lm_cal:\n"
"    ldr  x1, [x28, #0x000]\n"
"    isb\n"
"    mrs  x2, cntvct_el0\n"
"    mov  x3, x1\n"
"1:  isb\n"
"    mrs  x4, cntvct_el0\n"
"    subs x3, x3, #1\n"
"    b.ne 1b\n"
"    isb\n"
"    mrs  x5, cntvct_el0\n"
"    sub  x5, x5, x2\n"
"    str  x5, [x28, #0x200]\n"
"    isb\n"
"    mrs  x2, cntvct_el0\n"
"    mov  x3, x1\n"
"2:  subs x3, x3, #1\n"
"    b.ne 2b\n"
"    isb\n"
"    mrs  x5, cntvct_el0\n"
"    sub  x5, x5, x2\n"
"    str  x5, [x28, #0x208]\n"
"    isb\n"
"    mrs  x2, cntvct_el0\n"
"    mov  x3, x1\n"
"    mov  x6, #0\n"
"3:  add  x6, x6, #1\n"
"    add  x6, x6, #1\n"
"    add  x6, x6, #1\n"
"    add  x6, x6, #1\n"
"    add  x6, x6, #1\n"
"    add  x6, x6, #1\n"
"    add  x6, x6, #1\n"
"    add  x6, x6, #1\n"
"    add  x6, x6, #1\n"
"    add  x6, x6, #1\n"
"    add  x6, x6, #1\n"
"    add  x6, x6, #1\n"
"    add  x6, x6, #1\n"
"    add  x6, x6, #1\n"
"    add  x6, x6, #1\n"
"    add  x6, x6, #1\n"
"    subs x3, x3, #1\n"
"    b.ne 3b\n"
"    isb\n"
"    mrs  x5, cntvct_el0\n"
"    sub  x5, x5, x2\n"
"    str  x5, [x28, #0x210]\n"
"    hvc  #0\n"

// ---------------- mode 1: what ESR tells us about the faulting store ----------------
// One faulting store, then one faulting load, to a read-only page.  The dedicated handler
// (h_isv) records ESR/FAR/ELR and the faulting instruction word.
"Lm_isv:\n"
"    ldr  x0, [x28, #0x000]\n"                // VA_PTRO
"    mov  x5, #0x1234\n"
"    movk x5, #0xabcd, lsl #16\n"
"    mov  x6, #0x99\n"
"    ldr  x1, [x0]\n"                         // read: must NOT fault (AP=RO permits EL1 reads)
"    str  x1, [x28, #0x258]\n"
"    str  x19, [x28, #0x250]\n"               // faults so far -- must be 0
"    str  x5, [x0]\n"                         // 64-bit store
"    strb w5, [x0]\n"                         // byte store
"    str  w5, [x0]\n"                         // 32-bit store
"    stp  x5, x6, [x0]\n"                     // pair store: TWO transfer registers
"    stur x5, [x0, #1]\n"                     // unaligned, unscaled-offset store
"    str  x19, [x28, #0x260]\n"
"    hvc  #0\n"

// ---------------- mode 2: the write-protect handler ladder ----------------
// x10 = base VA of the region being stored to, x8 = iterations, x1 = stride
"Lm_wp:\n"
"    ldr  x8,  [x28, #0x000]\n"                // iterations
"    ldr  x9,  [x28, #0x008]\n"                // rounds
"    ldr  x10, [x28, #0x010]\n"                // base VA (RO or RW region)
"    ldr  x1,  [x28, #0x018]\n"                // stride
"    ldr  x5,  [x28, #0x020]\n"                // value to store (a plausible x86 PTE)
"    add  x11, x28, #0x200\n"
"    adr  x7, Lb_store\n"
"    bl   Ltimeit\n"
"    str  x19, [x28, #0x260]\n"
"    hvc  #0\n"

"Lb_store:\n"
"    mov  x0, x10\n"
"    mov  x15, x8\n"
"1:  str  x5, [x0]\n"
"    add  x0, x0, x1\n"
"    subs x15, x15, #1\n"
"    b.ne 1b\n"
"    ret\n"

// ---------------- mode 3: unprotect / burst / re-protect / resync ----------------
// x10 = VA_PTRO page 0, x8 = burst length N, x4 = shadow-L3-entry VA that maps VA_PTRO page 0,
// x6 = VA_PTRO>>12, x12 = writable alias of PT page 0, x2s below are scratch.
"Lm_burst:\n"
"    ldr  x8,  [x28, #0x000]\n"
"    ldr  x9,  [x28, #0x008]\n"
"    ldr  x10, [x28, #0x010]\n"
"    ldr  x5,  [x28, #0x020]\n"
"    ldr  x4,  [x28, #0x028]\n"                // VA of the shadow L3 descriptor for the PT page
"    ldr  x6,  [x28, #0x030]\n"                // VA_PTRO >> 12
"    ldr  x12, [x28, #0x038]\n"                // writable alias VA of PT page 0
"    ldr  x2,  [x28, #0x040]\n"                // shadow L3 table VA for PT page 0
"    str  x2,  [x28, #0x048]\n"
"    add  x11, x28, #0x200\n"
"    adr  x7, Lb_trapburst\n"
"    ldr  x9, [x28, #0x008]\n"
"    bl   Ltimeit\n"
"    str  x19, [x28, #0x260]\n"
"    adr  x7, Lb_flipburst\n"
"    ldr  x9, [x28, #0x008]\n"
"    bl   Ltimeit\n"
"    str  x19, [x28, #0x268]\n"
"    adr  x7, Lb_flipburst_notlbi\n"
"    ldr  x9, [x28, #0x008]\n"
"    bl   Ltimeit\n"
"    str  x19, [x28, #0x270]\n"
"    adr  x7, Lb_nativeburst\n"
"    ldr  x9, [x28, #0x008]\n"
"    bl   Ltimeit\n"
"    str  x19, [x28, #0x278]\n"
"    hvc  #0\n"

// Each body repeats the whole N-store episode M times so the timed window is many CNTVCT ticks
// even at N = 1.  x9 is free inside a body (Ltimeit copies it to x14 at entry) but must be
// reloaded before each call, which is what Lm_burst does above.
//
// N faulting stores, each handled by whichever handler the host installed.
"Lb_trapburst:\n"
"    ldr  x9, [x28, #0x050]\n"
"9:  mov  x0, x10\n"
"    mov  x15, x8\n"
"1:  str  x5, [x0]\n"
"    add  x0, x0, #8\n"
"    subs x15, x15, #1\n"
"    b.ne 1b\n"
"    subs x9, x9, #1\n"
"    b.ne 9b\n"
"    ret\n"

// N native stores to the SAME PHYSICAL PTEs through our writable alias: the floor.
"Lb_nativeburst:\n"
"    ldr  x9, [x28, #0x050]\n"
"9:  mov  x0, x12\n"
"    mov  x15, x8\n"
"1:  str  x5, [x0]\n"
"    add  x0, x0, #8\n"
"    subs x15, x15, #1\n"
"    b.ne 1b\n"
"    subs x9, x9, #1\n"
"    b.ne 9b\n"
"    ret\n"

// unprotect -> N native stores -> re-protect -> re-derive all 512 shadow descriptors -> global TLBI
"Lb_flipburst:\n"
"    ldr  x9, [x28, #0x050]\n"
"9:  ldr  x2, [x4]\n"
"    bic  x2, x2, #0x80\n"                     // AP[2] = 0 -> writable at EL1
"    str  x2, [x4]\n"
"    dsb  ishst\n"
"    tlbi vaae1is, x6\n"
"    dsb  ish\n"
"    isb\n"
"    mov  x0, x10\n"
"    mov  x15, x8\n"
"1:  str  x5, [x0]\n"
"    add  x0, x0, #8\n"
"    subs x15, x15, #1\n"
"    b.ne 1b\n"
"    ldr  x2, [x4]\n"
"    orr  x2, x2, #0x80\n"                     // back to read-only
"    str  x2, [x4]\n"
"    dsb  ishst\n"
"    tlbi vaae1is, x6\n"
"    dsb  ish\n"
"    isb\n"
"    mov  x0, x12\n"
"    ldr  x1, [x28, #0x048]\n"
"    mov  x15, #512\n"
"2:  ldr  x2, [x0], #8\n"
"    and  x3, x2, x23\n"
"    orr  x3, x3, x24\n"
"    tbnz x2, #1, 3f\n"
"    orr  x3, x3, #0x80\n"
"3:  str  x3, [x1], #8\n"
"    subs x15, x15, #1\n"
"    b.ne 2b\n"
"    dsb  ishst\n"
"    tlbi vmalle1is\n"
"    dsb  ish\n"
"    isb\n"
"    subs x9, x9, #1\n"
"    b.ne 9b\n"
"    ret\n"

// the same, minus the closing global TLBI (legal when every touched PTE was an INSTALL, 16 sec.2)
"Lb_flipburst_notlbi:\n"
"    ldr  x9, [x28, #0x050]\n"
"9:  ldr  x2, [x4]\n"
"    bic  x2, x2, #0x80\n"
"    str  x2, [x4]\n"
"    dsb  ishst\n"
"    tlbi vaae1is, x6\n"
"    dsb  ish\n"
"    isb\n"
"    mov  x0, x10\n"
"    mov  x15, x8\n"
"1:  str  x5, [x0]\n"
"    add  x0, x0, #8\n"
"    subs x15, x15, #1\n"
"    b.ne 1b\n"
"    ldr  x2, [x4]\n"
"    orr  x2, x2, #0x80\n"
"    str  x2, [x4]\n"
"    dsb  ishst\n"
"    tlbi vaae1is, x6\n"
"    dsb  ish\n"
"    isb\n"
"    mov  x0, x12\n"
"    ldr  x1, [x28, #0x048]\n"
"    mov  x15, #512\n"
"2:  ldr  x2, [x0], #8\n"
"    and  x3, x2, x23\n"
"    orr  x3, x3, x24\n"
"    tbnz x2, #1, 3f\n"
"    orr  x3, x3, #0x80\n"
"3:  str  x3, [x1], #8\n"
"    subs x15, x15, #1\n"
"    b.ne 2b\n"
"    dsb  ishst\n"
"    isb\n"
"    subs x9, x9, #1\n"
"    b.ne 9b\n"
"    ret\n"

// ================= handlers =================
// h0: bare permission fault -> advance ELR -> ERET.  The floor (16 sec.1 measured 28.2 ns).
".globl _h_wp0\n"
"_h_wp0:\n"
"    mrs  x25, elr_el1\n"
"    add  x25, x25, #4\n"
"    msr  elr_el1, x25\n"
"    add  x19, x19, #1\n"
"    cmp  x19, x20\n"
"    b.hi Lh_panic\n"
"    eret\n"

// h1: + classify the fault from ESR_EL1 and read FAR_EL1
".globl _h_wp1\n"
"_h_wp1:\n"
"    mrs  x25, esr_el1\n"
"    and  x27, x25, #0x3f\n"
"    cmp  x27, #0x0f\n"                        // DFSC = permission fault, level 3
"    b.ne Lh_panic\n"
"    tbz  x25, #6, Lh_panic\n"                 // WnR must be 1
"    mrs  x26, far_el1\n"
"    mrs  x25, elr_el1\n"
"    add  x25, x25, #4\n"
"    msr  elr_el1, x25\n"
"    add  x19, x19, #1\n"
"    cmp  x19, x20\n"
"    b.hi Lh_panic\n"
"    eret\n"

// h2: + fetch the faulting instruction, decode Rt, read the guest register value
".globl _h_wp2\n"
"_h_wp2:\n"
"    mrs  x25, esr_el1\n"
"    and  x27, x25, #0x3f\n"
"    cmp  x27, #0x0f\n"
"    b.ne Lh_panic\n"
"    tbz  x25, #6, Lh_panic\n"
"    mrs  x26, far_el1\n"
"    mrs  x27, elr_el1\n"
"    ldr  w2,  [x27]\n"
"    add  x27, x27, #4\n"
"    msr  elr_el1, x27\n"
"    and  w3, w2, #0x1f\n"
"    add  x3, x18, x3, lsl #3\n"
"    ldr  x3, [x3]\n"
"    add  x19, x19, #1\n"
"    cmp  x19, x20\n"
"    b.hi Lh_panic\n"
"    eret\n"

// h3: + apply the store to the guest's x86 PT page through our writable alias
".globl _h_wp3\n"
"_h_wp3:\n"
"    mrs  x25, esr_el1\n"
"    and  x27, x25, #0x3f\n"
"    cmp  x27, #0x0f\n"
"    b.ne Lh_panic\n"
"    tbz  x25, #6, Lh_panic\n"
"    mrs  x26, far_el1\n"
"    mrs  x27, elr_el1\n"
"    ldr  w2,  [x27]\n"
"    add  x27, x27, #4\n"
"    msr  elr_el1, x27\n"
"    and  w3, w2, #0x1f\n"
"    add  x3, x18, x3, lsl #3\n"
"    ldr  x3, [x3]\n"
"    add  x2, x26, x21\n"
"    str  x3, [x2]\n"
"    add  x19, x19, #1\n"
"    cmp  x19, x20\n"
"    b.hi Lh_panic\n"
"    eret\n"

// h4: + rmap lookup, PT page frame -> shadow L3 table VA and shadow VA base
".globl _h_wp4\n"
"_h_wp4:\n"
"    mrs  x25, esr_el1\n"
"    and  x27, x25, #0x3f\n"
"    cmp  x27, #0x0f\n"
"    b.ne Lh_panic\n"
"    tbz  x25, #6, Lh_panic\n"
"    mrs  x26, far_el1\n"
"    mrs  x27, elr_el1\n"
"    ldr  w2,  [x27]\n"
"    add  x27, x27, #4\n"
"    msr  elr_el1, x27\n"
"    and  w3, w2, #0x1f\n"
"    add  x3, x18, x3, lsl #3\n"
"    ldr  x3, [x3]\n"
"    add  x2, x26, x21\n"
"    str  x3, [x2]\n"
"    lsr  x27, x2, #12\n"
"    eor  x25, x27, x27, lsr #5\n"
"    and  x25, x25, x22\n"
"    add  x25, x28, x25, lsl #5\n"
"    ldr  x26, [x25, #0x1000]\n"
"    cmp  x26, x27\n"
"    b.ne Lh_panic\n"
"    ldr  x26, [x25, #0x1008]\n"
"    ubfx x27, x2, #3, #9\n"
"    add  x26, x26, x27, lsl #3\n"
"    add  x19, x19, #1\n"
"    cmp  x19, x20\n"
"    b.hi Lh_panic\n"
"    eret\n"

// h5: + synthesise the ARM descriptor and store it into the shadow.  INSTALL case: no TLBI,
//     which is legal only because the descriptor being replaced was invalid (16's design rule).
".globl _h_wp5\n"
"_h_wp5:\n"
"    mrs  x25, esr_el1\n"
"    and  x27, x25, #0x3f\n"
"    cmp  x27, #0x0f\n"
"    b.ne Lh_panic\n"
"    tbz  x25, #6, Lh_panic\n"
"    mrs  x26, far_el1\n"
"    mrs  x27, elr_el1\n"
"    ldr  w2,  [x27]\n"
"    add  x27, x27, #4\n"
"    msr  elr_el1, x27\n"
"    and  w3, w2, #0x1f\n"
"    add  x3, x18, x3, lsl #3\n"
"    ldr  x3, [x3]\n"
"    add  x2, x26, x21\n"
"    str  x3, [x2]\n"
"    lsr  x27, x2, #12\n"
"    eor  x25, x27, x27, lsr #5\n"
"    and  x25, x25, x22\n"
"    add  x25, x28, x25, lsl #5\n"
"    ldr  x26, [x25, #0x1000]\n"
"    cmp  x26, x27\n"
"    b.ne Lh_panic\n"
"    ldr  x26, [x25, #0x1008]\n"
"    ubfx x27, x2, #3, #9\n"
"    add  x26, x26, x27, lsl #3\n"
"    and  x2, x3, x23\n"
"    orr  x2, x2, x24\n"
"    tbnz x3, #1, 1f\n"
"    orr  x2, x2, #0x80\n"
"1:  str  x2, [x26]\n"
"    dsb  ishst\n"
"    add  x19, x19, #1\n"
"    cmp  x19, x20\n"
"    b.hi Lh_panic\n"
"    eret\n"

// h6: + invalidate.  MODIFY case: the shadow descriptor being replaced was valid, so the
//     architecture requires a TLBI.
".globl _h_wp6\n"
"_h_wp6:\n"
"    mrs  x25, esr_el1\n"
"    and  x27, x25, #0x3f\n"
"    cmp  x27, #0x0f\n"
"    b.ne Lh_panic\n"
"    tbz  x25, #6, Lh_panic\n"
"    mrs  x26, far_el1\n"
"    mrs  x27, elr_el1\n"
"    ldr  w2,  [x27]\n"
"    add  x27, x27, #4\n"
"    msr  elr_el1, x27\n"
"    and  w3, w2, #0x1f\n"
"    add  x3, x18, x3, lsl #3\n"
"    ldr  x3, [x3]\n"
"    add  x2, x26, x21\n"
"    str  x3, [x2]\n"
"    lsr  x27, x2, #12\n"
"    eor  x25, x27, x27, lsr #5\n"
"    and  x25, x25, x22\n"
"    add  x25, x28, x25, lsl #5\n"
"    ldr  x26, [x25, #0x1000]\n"
"    cmp  x26, x27\n"
"    b.ne Lh_panic\n"
"    ldr  x26, [x25, #0x1008]\n"
"    ubfx x27, x2, #3, #9\n"
"    add  x26, x26, x27, lsl #3\n"
"    and  x2, x3, x23\n"
"    orr  x2, x2, x24\n"
"    tbnz x3, #1, 1f\n"
"    orr  x2, x2, #0x80\n"
"1:  str  x2, [x26]\n"
"    ldr  x26, [x25, #0x1010]\n"
"    add  x26, x26, x27\n"
"    dsb  ishst\n"
"    tlbi vaae1is, x26\n"
"    dsb  ish\n"
"    isb\n"
"    add  x19, x19, #1\n"
"    cmp  x19, x20\n"
"    b.hi Lh_panic\n"
"    eret\n"

// ESR-reporting handler for mode 1
".globl _h_isv\n"
"_h_isv:\n"
"    mrs  x25, esr_el1\n"
"    mrs  x26, far_el1\n"
"    mrs  x27, elr_el1\n"
"    add  x2, x28, x19, lsl #5\n"
"    str  x25, [x2, #0x280]\n"
"    str  x26, [x2, #0x288]\n"
"    str  x27, [x2, #0x290]\n"
"    ldr  w3, [x27]\n"
"    str  x3,  [x2, #0x298]\n"
"    add  x27, x27, #4\n"
"    msr  elr_el1, x27\n"
"    add  x19, x19, #1\n"
"    cmp  x19, x20\n"
"    b.hi Lh_panic\n"
"    eret\n"

"Lh_panic:\n"
".globl _h_panic\n"
"_h_panic:\n"
"    mrs  x25, esr_el1\n"
"    mrs  x26, far_el1\n"
"    mrs  x27, elr_el1\n"
"    hvc  #9\n"
".globl _pw_end\n"
"_pw_end:\n"
);

extern const uint8_t pw_start[], pw_end[];
extern const uint8_t h_wp0[], h_wp1[], h_wp2[], h_wp3[], h_wp4[], h_wp5[], h_wp6[];
extern const uint8_t h_isv[], h_panic[];

// ------------------------------------------------------------------ host state
static uint8_t *g_mem;
static uint64_t g_alloc;
static uint64_t g_cntfrq = 24000000;
static double   g_ghz = 0.0;
static int      g_fail = 0;
static int      g_rmap_shift = 9;
static uint64_t g_rmap_mask = 0x3ff;

static uint64_t *P(void) { return (uint64_t *)(g_mem + OFF_PARAM); }
static uint64_t *R(void) { return (uint64_t *)(g_mem + OFF_PARAM + 0x200); }

// ------------------------------------------------------------------ page tables
static void reset_arena(void) { g_alloc = OFF_ARENA; }
static uint64_t alloc_tab(void) {
    uint64_t off = g_alloc; g_alloc += 4096;
    if (g_alloc > OFF_ARENA_END) { fprintf(stderr, "arena exhausted\n"); exit(2); }
    memset(g_mem + off, 0, 4096);
    return off;
}
static uint64_t *tab(uint64_t off) { return (uint64_t *)(g_mem + off); }

static uint64_t walk_to(uint64_t root, uint64_t va, int level) {
    int sh[4] = { 39, 30, 21, 12 };
    uint64_t cur = root;
    for (int l = 0; l < level; l++) {
        uint64_t idx = (va >> sh[l]) & 0x1ff;
        uint64_t *t = tab(cur);
        if ((t[idx] & 1) == 0) t[idx] = (RAM_IPA + alloc_tab()) | D_PAGE;
        cur = (t[idx] & PA_MASK) - RAM_IPA;
    }
    return cur;
}
static void map_page(uint64_t root, uint64_t va, uint64_t pa, uint64_t attrs) {
    uint64_t t3 = walk_to(root, va, 3);
    tab(t3)[(va >> 12) & 0x1ff] = (pa & PA_MASK) | attrs | D_PAGE;
}
static void map_block2(uint64_t root, uint64_t va, uint64_t pa, uint64_t attrs) {
    uint64_t t2 = walk_to(root, va, 2);
    tab(t2)[(va >> 21) & 0x1ff] = (pa & PA_MASK) | attrs | D_BLOCK;
}
// VA of the L3 descriptor that maps `va`
static uint64_t l3_entry_va(uint64_t root, uint64_t va) {
    uint64_t t3 = walk_to(root, va, 3);
    return RAM_IPA + t3 + (((va >> 12) & 0x1ff) * 8);
}

// ------------------------------------------------------------------ world build
struct world { uint64_t root; uint64_t shadow_l3[NPT]; };

static uint64_t build_world(struct world *w) {
    reset_arena();
    uint64_t root = alloc_tab();
    for (uint64_t o = 0; o < RAM_SIZE; o += (2ULL << 20))
        map_block2(root, RAM_IPA + o, RAM_IPA + o, D_AF | D_SH_IS);   // RW + EL1-executable

    // The guest's own view of its page tables: read-only.  This is the write-protect.
    for (int i = 0; i < NPT; i++)
        map_page(root, VA_PTRO + (uint64_t)i * 4096, RAM_IPA + OFF_PTLEAF + (uint64_t)i * 4096,
                 D_AF | D_SH_IS | D_AP_RO | D_UXN | D_PXN);
    // Control region: identical geometry, writable.
    for (int i = 0; i < NPT; i++)
        map_page(root, VA_PTRW + (uint64_t)i * 4096, RAM_IPA + OFF_PTCTRL + (uint64_t)i * 4096,
                 D_AF | D_SH_IS | D_UXN | D_PXN);
    // 512 real L3 tables for the shadow region, one per PT page, left entirely invalid.
    for (int i = 0; i < NPT; i++) {
        uint64_t va = VA_SHADOW + (uint64_t)i * (2ULL << 20);
        w->shadow_l3[i] = RAM_IPA + walk_to(root, va, 3);
    }
    w->root = root;
    return root;
}

// rmap: PT page frame -> { tag, shadow L3 table VA, shadow VA >> 12 }
static void build_rmap(struct world *w) {
    memset(g_mem + OFF_RMAP, 0, 1024 * 32);
    // The shift is hard-coded in the payload (`eor x25, x27, x27, lsr #5`); verify here that it
    // is collision-free for our frames rather than letting the handler take a slow path we did
    // not intend to measure.
    int used[1024]; memset(used, 0, sizeof used);
    g_rmap_shift = 5;
    for (int i = 0; i < NPT; i++) {
        uint64_t f = (RAM_IPA + OFF_PTLEAF) / 4096 + (uint64_t)i;
        uint64_t h = (f ^ (f >> g_rmap_shift)) & g_rmap_mask;
        if (used[h]) { fprintf(stderr, "rmap hash collides at frame %d\n", i); exit(2); }
        used[h] = 1;
    }
    uint64_t *b = (uint64_t *)(g_mem + OFF_RMAP);
    for (int i = 0; i < NPT; i++) {
        uint64_t f = (RAM_IPA + OFF_PTLEAF) / 4096 + (uint64_t)i;
        uint64_t h = (f ^ (f >> g_rmap_shift)) & g_rmap_mask;
        b[h * 4 + 0] = f;
        b[h * 4 + 1] = w->shadow_l3[i];
        b[h * 4 + 2] = (VA_SHADOW + (uint64_t)i * (2ULL << 20)) >> 12;
        b[h * 4 + 3] = 0;
    }
}

// ------------------------------------------------------------------ guest runner
typedef struct { hv_vcpu_t vcpu; volatile int done; int secs; } wd_t;
static void *watchdog(void *p) {
    wd_t *w = p;
    for (int i = 0; i < w->secs * 20 && !w->done; i++) usleep(50000);
    if (!w->done) { fprintf(stderr, "  !! watchdog: cancelling vCPU after %d s\n", w->secs);
                    hv_vcpus_exit((hv_vcpu_t *)&w->vcpu, 1); }
    return NULL;
}

enum { H_NONE = 0, H_WP0, H_WP1, H_WP2, H_WP3, H_WP4, H_WP5, H_WP6, H_ISV };

static void install_vectors(int which) {
    uint32_t *v = (uint32_t *)(g_mem + OFF_VEC);
    uint64_t panic_off = OFF_CODE + (uint64_t)(h_panic - pw_start);
    for (int i = 0; i < 16; i++) {
        int64_t d = (int64_t)panic_off - (int64_t)(OFF_VEC + i * 0x80);
        v[i * 0x20] = 0x14000000u | (uint32_t)((d >> 2) & 0x03ffffff);
    }
    if (which == H_NONE) return;
    const uint8_t *h = NULL;
    switch (which) {
        case H_WP0: h = h_wp0; break;  case H_WP1: h = h_wp1; break;
        case H_WP2: h = h_wp2; break;  case H_WP3: h = h_wp3; break;
        case H_WP4: h = h_wp4; break;  case H_WP5: h = h_wp5; break;
        case H_WP6: h = h_wp6; break;  case H_ISV: h = h_isv; break;
    }
    uint64_t slot = OFF_VEC + 0x200;                    // current EL, SP_ELx, synchronous
    uint64_t hoff = OFF_CODE + (uint64_t)(h - pw_start);
    int64_t d = (int64_t)hoff - (int64_t)slot;
    *(uint32_t *)(g_mem + slot) = 0x14000000u | (uint32_t)((d >> 2) & 0x03ffffff);
}

static const char *ecname(uint64_t ec) {
    switch (ec) {
        case 0x16: return "HVC"; case 0x18: return "MSR/MRS trap";
        case 0x21: return "instruction abort"; case 0x25: return "data abort current EL";
        default: return "?";
    }
}

static int run_guest(uint64_t mode, uint64_t ttbr, int secs) {
    hv_vcpu_t vcpu; hv_vcpu_exit_t *vexit;
    HV(hv_vcpu_create(&vcpu, &vexit, NULL));
    HV(hv_vcpu_set_reg(vcpu, HV_REG_PC,   RAM_IPA + OFF_CODE));
    HV(hv_vcpu_set_reg(vcpu, HV_REG_CPSR, 0x3c5));      // EL1h, DAIF masked -- HVF starts at EL0
    HV(hv_vcpu_set_reg(vcpu, HV_REG_X0,   MAIR_VAL));
    HV(hv_vcpu_set_reg(vcpu, HV_REG_X1,   TCR_4K));
    HV(hv_vcpu_set_reg(vcpu, HV_REG_X2,   ttbr));
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
        if (vexit->reason == HV_EXIT_REASON_CANCELED) { fprintf(stderr, "  !! cancelled\n"); rc = 1; break; }
        if (vexit->reason != HV_EXIT_REASON_EXCEPTION) {
            fprintf(stderr, "  !! unexpected exit reason %u\n", vexit->reason); rc = 1; break; }
        uint64_t sy = vexit->exception.syndrome, ec = (sy >> 26) & 0x3f;
        if (ec == 0x16 && (sy & 0xffff) == 0) break;
        if (ec == 0x16 && (sy & 0xffff) == 9) {
            uint64_t esr = 0, far = 0, elr = 0, pc = 0;
            hv_vcpu_get_reg(vcpu, HV_REG_X25, &esr); hv_vcpu_get_reg(vcpu, HV_REG_X26, &far);
            hv_vcpu_get_reg(vcpu, HV_REG_X27, &elr); hv_vcpu_get_reg(vcpu, HV_REG_PC, &pc);
            fprintf(stderr, "  !! guest PANIC mode %llu: ESR=0x%llx EC=0x%llx (%s) FAR=0x%llx "
                            "ELR=0x%llx PC=0x%llx\n", (unsigned long long)mode,
                    (unsigned long long)esr, (unsigned long long)((esr >> 26) & 0x3f),
                    ecname((esr >> 26) & 0x3f), (unsigned long long)far,
                    (unsigned long long)elr, (unsigned long long)pc);
            rc = 1; break;
        }
        uint64_t pc = 0; hv_vcpu_get_reg(vcpu, HV_REG_PC, &pc);
        fprintf(stderr, "  !! unexpected exit ESR=0x%llx EC=0x%llx (%s) PC=0x%llx\n",
                (unsigned long long)sy, (unsigned long long)ec, ecname(ec), (unsigned long long)pc);
        rc = 1; break;
    }
    w.done = 1; pthread_join(wt, NULL);
    hv_vcpu_destroy(vcpu);
    if (rc) g_fail++;
    return rc;
}

// ------------------------------------------------------------------ stats
typedef struct { double med, lo, hi, min; } stat_t;
static int dcmp(const void *a, const void *b) {
    double x = *(const double *)a, y = *(const double *)b; return x < y ? -1 : x > y ? 1 : 0; }
static stat_t stats(const uint64_t *ticks, int n, double per) {
    double *v = malloc(sizeof(double) * (size_t)n);
    for (int i = 0; i < n; i++) v[i] = (double)ticks[i] * 1e9 / (double)g_cntfrq / per;
    qsort(v, (size_t)n, sizeof(double), dcmp);
    stat_t s = { v[n/2], v[n/10], v[n - 1 - n/10], v[0] };
    free(v); return s;
}

static void clear_params(void) { memset(g_mem + OFF_PARAM, 0, 0x1000); }
static void set_state(uint64_t limit) {
    uint64_t *p = P();
    p[0x1c0/8] = 0;
    p[0x1c8/8] = limit;
    p[0x1d0/8] = (RAM_IPA + OFF_PTLEAF) - VA_PTRO;      // RO alias -> writable alias delta
    p[0x1d8/8] = g_rmap_mask;
    p[0x1e0/8] = PA_MASK;
    p[0x1e8/8] = D_AF | D_SH_IS | D_UXN | D_PXN | D_PAGE;
    p[0x1f0/8] = RAM_IPA + OFF_GPR;
}

// ------------------------------------------------------------------ experiments
// A first measured configuration in a fresh process reads 2-3x high because the core has not
// boosted yet (the same effect doc 16 sec.1 hit and reported as bimodality). Burn ~1 M faults
// and throw the result away before any measurement.
static void warmup(void) {
    struct world w; uint64_t root = build_world(&w); build_rmap(&w);
    for (int i = 0; i < 32; i++) ((uint64_t *)(g_mem + OFF_GPR))[i] = PTEVAL;
    clear_params();
    uint64_t *p = P();
    p[0] = 512; p[1] = 2000; p[2] = VA_PTRO; p[3] = 8; p[4] = PTEVAL;
    set_state(512ULL * 2000 + 16);
    install_vectors(H_WP0);
    int save = g_fail;
    (void)run_guest(2, RAM_IPA + root, 120);
    g_fail = save;
}

static void exp_cal(void) {
    printf("\n=== 0. calibration ===\n");
    struct world w; uint64_t root = build_world(&w); build_rmap(&w);
    clear_params(); P()[0] = 200000; set_state(1);
    install_vectors(H_WP0);
    if (run_guest(0, RAM_IPA + root, 30)) return;
    g_cntfrq = ((uint64_t *)(g_mem + OFF_PARAM))[0xc18/8];
    double N = 200000;
    double t_cnt = (double)R()[0] * 1e9 / g_cntfrq / N;
    double t_emp = (double)R()[1] * 1e9 / g_cntfrq / N;
    double t_add = (double)R()[2] * 1e9 / g_cntfrq / (N * 16);
    g_ghz = 1.0 / t_add;
    printf("  CNTFRQ_EL0 %llu Hz (%.2f ns/tick);  {isb;mrs cntvct} %.2f ns;  empty loop %.3f ns\n",
           (unsigned long long)g_cntfrq, 1e9 / (double)g_cntfrq, t_cnt, t_emp);
    printf("  dependent ADD %.4f ns -> clock ~ %.2f GHz  (cycle columns are +/-5%%, modelled)\n",
           t_add, g_ghz);
    printf("  rmap hash: (frame ^ (frame >> %d)) & 0x%llx, verified collision-free for %d frames\n",
           g_rmap_shift, (unsigned long long)g_rmap_mask, NPT);
}

static void exp_isv(void) {
    printf("\n=== 1. what ESR_EL1 tells the write-protect handler ===\n");
    struct world w; uint64_t root = build_world(&w); build_rmap(&w);
    clear_params(); P()[0] = VA_PTRO; set_state(16);
    install_vectors(H_ISV);
    if (run_guest(1, RAM_IPA + root, 30)) return;
    uint64_t *p = P();
    printf("  a LOAD from the write-protected page took %llu fault(s) and returned 0x%llx\n",
           (unsigned long long)p[0x250/8], (unsigned long long)p[0x258/8]);
    printf("  faults taken by the five stores: %llu (expected 5)\n",
           (unsigned long long)p[0x260/8]);
    static const char *what[5] = { "STR  Xt, [Xn]      (8-byte store)",
                                   "STRB Wt, [Xn]      (1-byte store)",
                                   "STR  Wt, [Xn]      (4-byte store)",
                                   "STP  Xt, Xt2, [Xn] (16-byte pair store)",
                                   "STUR Xt, [Xn, #1]  (unaligned store)" };
    for (int i = 0; i < 5; i++) {
        uint64_t *e = (uint64_t *)((uint8_t *)p + 0x280 + i * 32);
        uint64_t esr = e[0];
        printf("\n  %s\n", what[i]);
        printf("    ESR_EL1 = 0x%016llx   EC=0x%llx  ISV=%llu  SAS=%llu  SSE=%llu  SRT=%llu  "
               "SF=%llu  AR=%llu\n",
               (unsigned long long)esr, (unsigned long long)((esr >> 26) & 0x3f),
               (unsigned long long)((esr >> 24) & 1), (unsigned long long)((esr >> 22) & 3),
               (unsigned long long)((esr >> 21) & 1), (unsigned long long)((esr >> 16) & 0x1f),
               (unsigned long long)((esr >> 15) & 1), (unsigned long long)((esr >> 14) & 1));
        printf("    WnR=%llu  CM=%llu  S1PTW=%llu  DFSC=0x%02llx   FAR_EL1=0x%llx\n",
               (unsigned long long)((esr >> 6) & 1), (unsigned long long)((esr >> 8) & 1),
               (unsigned long long)((esr >> 7) & 1), (unsigned long long)(esr & 0x3f),
               (unsigned long long)e[1]);
        printf("    ELR_EL1=0x%llx  instruction word at ELR = 0x%08llx  (Rt field = %llu)\n",
               (unsigned long long)e[2], (unsigned long long)e[3],
               (unsigned long long)(e[3] & 0x1f));
    }
    printf("\n  If ISV=0 the syndrome carries neither the transfer register nor the access size,\n"
           "  so a write-protect handler must fetch the faulting instruction from ELR_EL1 and\n"
           "  decode it (or carry a side table keyed by translated-code address). That cost is\n"
           "  measured as step 2 of the ladder below. Note also that STP has TWO transfer\n"
           "  registers and writes 16 bytes -- i.e. two guest PTEs -- from one fault.\n");
}

static void exp_wp(void) {
    printf("\n=== 2. write-protect trap-emulate-reflect cost ladder ===\n");
    const uint64_t ITERS = 512, ROUNDS = 101;
    struct { const char *name; int h; } lad[] = {
        { "0  bare permission fault + ERET",                     H_WP0 },
        { "1  + read ESR/FAR, check DFSC + WnR",                 H_WP1 },
        { "2  + fetch faulting insn, decode Rt, read guest reg", H_WP2 },
        { "3  + apply the store to the guest PT page",           H_WP3 },
        { "4  + rmap lookup PT frame -> shadow L3",              H_WP4 },
        { "5  + synthesise + store the shadow descriptor",       H_WP5 },
        { "6  + DSB/TLBI VAAE1IS/DSB/ISB",                       H_WP6 },
        { "0' bare permission fault again (drift check)",        H_WP0 },
    };
    struct { const char *name; uint64_t stride; } pat[] = {
        { "512 consecutive PTE slots in ONE PT page (fork-like)", 8    },
        { "one PTE slot in each of 512 PT pages (scattered)",     4096 },
    };
    const int NLAD = (int)(sizeof(lad)/sizeof(lad[0]));
    // Every configuration is run TWICE and both passes are printed. Doc 16 sec.1 found that the
    // first configurations executed in a process read 3-4x high; running the identical list twice
    // separates "position in the run" from "what the handler does".
    for (int q = 0; q < 2; q++) {
      double got[2][16], ctl[2];
      for (int pass = 0; pass < 2; pass++) {
        double ctrl = 0;
        for (int c = -1; c < NLAD; c++) {
            struct world w; uint64_t root = build_world(&w); build_rmap(&w);
            memset(g_mem + OFF_PTLEAF, 0, NPT * 4096);
            memset(g_mem + OFF_PTCTRL, 0, NPT * 4096);
            for (int i = 0; i < 32; i++) ((uint64_t *)(g_mem + OFF_GPR))[i] = PTEVAL;
            clear_params();
            uint64_t *p = P();
            p[0] = ITERS; p[1] = ROUNDS;
            p[2] = (c < 0) ? VA_PTRW : VA_PTRO;
            p[3] = pat[q].stride;
            p[4] = PTEVAL;
            set_state(ITERS * ROUNDS + 16);
            install_vectors(c < 0 ? H_NONE : lad[c].h);
            if (run_guest(2, RAM_IPA + root, 60)) continue;
            stat_t s = stats(R(), (int)ROUNDS, (double)ITERS);
            if (c < 0) { ctrl = s.med; ctl[pass] = ctrl; continue; }
            uint64_t nf = p[0x260/8];
            int applied = 1;
            if (lad[c].h >= H_WP3) {
                uint64_t *ptp = (uint64_t *)(g_mem + OFF_PTLEAF);
                for (uint64_t i = 0; i < ITERS; i++) {
                    uint64_t idx = (pat[q].stride == 8) ? i : i * 512;
                    if (ptp[idx] != PTEVAL) { applied = 0; break; }
                }
            }
            got[pass][c] = s.med;
            if (!applied) printf("     !! step %d: PT write NOT applied\n", c);
            if (nf != ITERS * ROUNDS)
                printf("     !! step %d: %llu faults, expected %llu\n", c,
                       (unsigned long long)nf, (unsigned long long)(ITERS * ROUNDS));
        }
      }
      printf("\n  -- pattern: %s\n", pat[q].name);
      printf("     %-52s %8.2f %8.2f  ns/store\n",
             "control: same loop, writable region (pass 1 / pass 2)", ctl[0], ctl[1]);
      printf("     %-52s %8s %8s %10s %8s\n", "", "pass1", "pass2", "NET(p2)", "cyc");
      for (int c = 0; c < NLAD; c++)
          printf("     %-52s %8.2f %8.2f %10.2f %8.0f\n", lad[c].name, got[0][c], got[1][c],
                 got[1][c] - ctl[1], g_ghz > 0 ? (got[1][c] - ctl[1]) * g_ghz : 0.0);
      printf("     (increments, pass 2:");
      for (int c = 1; c < NLAD - 1; c++) printf(" %+.1f", got[1][c] - got[1][c-1]);
      printf(" ns)\n");
    }
}

static void exp_burst(void) {
    printf("\n=== 3. trap every store, versus unprotect / burst / re-protect / resync ===\n");
    printf("  The flip arms pay: 1 shadow-descriptor edit + TLBI to unprotect, N native stores,\n"
           "  1 edit + TLBI to re-protect, then a full 512-entry re-derivation of the page's\n"
           "  shadow. The `+TLBI` arm ends with one TLBI VMALLE1IS (needed when any entry was\n"
           "  MODIFIED); the `noTLBI` arm omits it (legal when every entry was an INSTALL).\n");
    const uint64_t ROUNDS = 25;
    const uint64_t Ns[] = { 1, 2, 4, 8, 16, 32, 64, 128, 256, 512 };
    printf("\n  %6s %5s %13s %13s %13s %13s\n", "N", "M",
           "trap-each", "flip+TLBI", "flip noTLBI", "native floor");
    printf("  %6s %5s %13s %13s %13s %13s\n", "", "",
           "ns/episode", "ns/episode", "ns/episode", "ns/episode");
    double tr[16], fl[16], fn[16], nat[16];
    int nN = (int)(sizeof(Ns)/sizeof(Ns[0]));
    // Two passes; only the second is reported (see the note in exp_wp).
    for (int pass = 0; pass < 2; pass++)
    for (int k = 0; k < nN; k++) {
        uint64_t M = 16384 / Ns[k]; if (M < 8) M = 8;
        struct world w; uint64_t root = build_world(&w); build_rmap(&w);
        memset(g_mem + OFF_PTLEAF, 0, NPT * 4096);
        for (int i = 0; i < 32; i++) ((uint64_t *)(g_mem + OFF_GPR))[i] = PTEVAL;
        clear_params();
        uint64_t *p = P();
        p[0] = Ns[k]; p[1] = ROUNDS; p[2] = VA_PTRO; p[4] = PTEVAL;
        p[5] = l3_entry_va(root, VA_PTRO);          // shadow descriptor for PT page 0
        p[6] = VA_PTRO >> 12;
        p[7] = RAM_IPA + OFF_PTLEAF;                // writable alias of PT page 0
        p[8] = w.shadow_l3[0];
        p[10] = M;                                  // 0x050: repeats per timed window
        set_state(Ns[k] * M * ROUNDS + 16);
        install_vectors(H_WP5);                     // install case: no per-fault TLBI
        if (run_guest(3, RAM_IPA + root, 300)) continue;
        double per = (double)M;
        tr[k]  = stats(R() + 0 * ROUNDS, (int)ROUNDS, per).med;
        fl[k]  = stats(R() + 1 * ROUNDS, (int)ROUNDS, per).med;
        fn[k]  = stats(R() + 2 * ROUNDS, (int)ROUNDS, per).med;
        nat[k] = stats(R() + 3 * ROUNDS, (int)ROUNDS, per).med;
        uint64_t nf = p[0x260/8];
        if (nf != Ns[k] * M * ROUNDS)
            printf("  !! N=%llu: %llu faults, expected %llu\n", (unsigned long long)Ns[k],
                   (unsigned long long)nf, (unsigned long long)(Ns[k] * M * ROUNDS));
        if (pass == 1)
            printf("  %6llu %5llu %13.1f %13.1f %13.1f %13.1f\n",
                   (unsigned long long)Ns[k], (unsigned long long)M, tr[k], fl[k], fn[k], nat[k]);
    }
    printf("\n  per-store:\n  %6s %14s %14s %14s\n", "N", "trap-each", "flip+TLBI", "flip noTLBI");
    for (int k = 0; k < nN; k++)
        printf("  %6llu %14.2f %14.2f %14.2f\n", (unsigned long long)Ns[k],
               tr[k] / (double)Ns[k], fl[k] / (double)Ns[k], fn[k] / (double)Ns[k]);
    int be1 = -1, be2 = -1;
    for (int k = 0; k < nN; k++) {
        if (be1 < 0 && fl[k] < tr[k]) be1 = k;
        if (be2 < 0 && fn[k] < tr[k]) be2 = k;
    }
    printf("\n  break-even burst length, flip+TLBI  : %s\n",
           be1 < 0 ? "never within N<=512" : "");
    if (be1 >= 0) printf("      N = %llu  (at N = %llu trapping is still cheaper)\n",
                         (unsigned long long)Ns[be1],
                         (unsigned long long)(be1 ? Ns[be1-1] : 0));
    printf("  break-even burst length, flip noTLBI: %s\n", be2 < 0 ? "never within N<=512" : "");
    if (be2 >= 0) printf("      N = %llu  (at N = %llu trapping is still cheaper)\n",
                         (unsigned long long)Ns[be2],
                         (unsigned long long)(be2 ? Ns[be2-1] : 0));
}

int main(int argc, char **argv) {
    const char *what = argc > 1 ? argv[1] : "all";
    pthread_set_qos_class_self_np(QOS_CLASS_USER_INTERACTIVE, 0);
    printf("=== guest page-table write-protection cost probe (in-VM, guest EL1, CNTVCT_EL0) ===\n");
    HV(hv_vm_create(NULL));
    g_mem = mmap(NULL, RAM_SIZE, PROT_READ | PROT_WRITE, MAP_ANON | MAP_PRIVATE, -1, 0);
    if (g_mem == MAP_FAILED) { perror("mmap"); return 2; }
    memset(g_mem, 0, RAM_SIZE);
    HV(hv_vm_map(g_mem, RAM_IPA, RAM_SIZE, HV_MEMORY_READ | HV_MEMORY_WRITE | HV_MEMORY_EXEC));
    memcpy(g_mem + OFF_CODE, pw_start, (size_t)(pw_end - pw_start));
    printf("  payload %zu bytes, RAM %llu MiB, %d write-protected PT pages\n",
           (size_t)(pw_end - pw_start), (unsigned long long)(RAM_SIZE >> 20), NPT);

    int all = !strcmp(what, "all");
    exp_cal();
    warmup();
    if (all || !strcmp(what, "isv"))   exp_isv();
    if (all || !strcmp(what, "wp"))    exp_wp();
    if (all || !strcmp(what, "burst")) exp_burst();
    hv_vm_destroy();
    printf("\n  %d failed experiment(s)\n", g_fail);
    return g_fail ? 1 : 0;
}
