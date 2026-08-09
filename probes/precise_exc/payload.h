// Guest EL1 payload for the precise-exception cost probe.
//
// Everything in here runs INSIDE the VM at EL1 with our own VBAR_EL1. No host exit occurs
// between the two CNTVCT_EL0 reads that bracket any measurement. Structure, register
// discipline and the `Ltimeit` protocol are copied from probes/shadow_mmu/payload.h so the
// two probes' numbers are directly comparable.
//
// Register discipline
// -------------------
//   x28            param/result block VA (identity mapped)
//   x19            fault counter (incremented by the instrumented handlers)
//   x20            fault limit  (handler panics via HVC #9 past this -> no wedged machine)
//   x21            stackmap table VA          (mode 4 handler)
//   x22            log2(stackmap entries)     (mode 4 handler)
//   x23            guest-CPUState VA          (mode 4 handler)
//   x24            spare handler state
//   x25,x26,x27    handler scratch -- never used by measurement bodies
//   x7..x14        `Ltimeit` parameters and state -- never clobbered by measurement bodies
//   x16            holds t0 ACROSS the blr -- measurement bodies must not touch it
//   x0-x6,x15,x17,x18  free for measurement bodies
//
// `Ltimeit` protocol:  x7 = body, x8 = count, x9 = rounds, x10 = base VA, x11 = result cursor,
//                     x12 = extra.  Result r lands at [x11], x11 post-incremented by 8.
//
// SP_EL1 is set from param[0x1f0] in the prologue -- the mode-4 recovery handler needs a stack.

#define PAYLOAD_ASM                                                                        \
".section __TEXT,__text\n"                                                                 \
".p2align 2\n"                                                                             \
".globl _pe_start\n"                                                                       \
"_pe_start:\n"                                                                             \
/* x0=MAIR x1=TCR x2=TTBR0 x3=VBAR x4=param-block VA x5=mode */                             \
"    msr  mair_el1, x0\n"                                                                  \
"    msr  tcr_el1,  x1\n"                                                                  \
"    msr  ttbr0_el1, x2\n"                                                                 \
"    msr  vbar_el1, x3\n"                                                                  \
"    isb\n"                                                                                \
"    tlbi vmalle1\n"                                                                       \
"    dsb  nsh\n"                                                                           \
"    isb\n"                                                                                \
"    mrs  x9, sctlr_el1\n"                                                                 \
"    mov  x10, #0x1005\n"           /* M | C | I */                                        \
"    orr  x9, x9, x10\n"                                                                   \
"    msr  sctlr_el1, x9\n"                                                                 \
"    isb\n"                                                                                \
/* The host rewrites the vector table between runs; drop any stale instruction lines. */   \
"    ic   iallu\n"                                                                         \
"    dsb  nsh\n"                                                                           \
"    isb\n"                                                                                \
/* MMU is on. Everything below is translated by our own stage-1 tables. */                 \
"    mov  x28, x4\n"                                                                       \
"    ldr  x19, [x28, #0x1c0]\n"                                                            \
"    ldr  x20, [x28, #0x1c8]\n"                                                            \
"    ldr  x21, [x28, #0x1d0]\n"                                                            \
"    ldr  x22, [x28, #0x1d8]\n"                                                            \
"    ldr  x23, [x28, #0x1e0]\n"                                                            \
"    ldr  x24, [x28, #0x1e8]\n"                                                            \
"    ldr  x9,  [x28, #0x1f0]\n"                                                            \
"    mov  sp, x9\n"                                                                        \
"    mrs  x0, cntfrq_el0\n"                                                                \
"    str  x0, [x28, #0xc18]\n"                                                             \
"    cmp  x5, #0\n"                                                                        \
"    b.eq Lm_cal\n"                                                                        \
"    cmp  x5, #1\n"                                                                        \
"    b.eq Lm_spill\n"                                                                      \
"    cmp  x5, #2\n"                                                                        \
"    b.eq Lm_lookup\n"                                                                     \
"    cmp  x5, #3\n"                                                                        \
"    b.eq Lm_at\n"                                                                         \
"    cmp  x5, #4\n"                                                                        \
"    b.eq Lm_pf\n"                                                                         \
"    cmp  x5, #5\n"                                                                        \
"    b.eq Lm_atverify\n"                                                                   \
"    hvc  #9\n"                                                                            \
                                                                                           \
/* ---------------- Ltimeit ---------------- */                                            \
"Ltimeit:\n"                                                                               \
"    mov  x13, x30\n"                                                                      \
"    mov  x14, x9\n"                                                                       \
"1:  isb\n"                                                                                \
"    mrs  x16, cntvct_el0\n"                                                               \
"    blr  x7\n"                                                                            \
"    isb\n"                                                                                \
"    mrs  x17, cntvct_el0\n"                                                               \
"    sub  x17, x17, x16\n"                                                                 \
"    str  x17, [x11], #8\n"                                                                \
"    subs x14, x14, #1\n"                                                                  \
"    b.ne 1b\n"                                                                            \
"    mov  x30, x13\n"                                                                      \
"    ret\n"                                                                                \
                                                                                           \
/* ---------------- mode 0: calibration ---------------- */                                \
"Lm_cal:\n"                                                                                \
"    ldr  x1, [x28, #0x000]\n"                                                             \
/* (a) back-to-back {isb; mrs cntvct} cost */                                              \
"    isb\n"                                                                                \
"    mrs  x2, cntvct_el0\n"                                                                \
"    mov  x3, x1\n"                                                                        \
"1:  isb\n"                                                                                \
"    mrs  x4, cntvct_el0\n"                                                                \
"    subs x3, x3, #1\n"                                                                    \
"    b.ne 1b\n"                                                                            \
"    isb\n"                                                                                \
"    mrs  x5, cntvct_el0\n"                                                                \
"    sub  x5, x5, x2\n"                                                                    \
"    str  x5, [x28, #0x200]\n"                                                             \
/* (b) empty loop */                                                                       \
"    isb\n"                                                                                \
"    mrs  x2, cntvct_el0\n"                                                                \
"    mov  x3, x1\n"                                                                        \
"2:  subs x3, x3, #1\n"                                                                    \
"    b.ne 2b\n"                                                                            \
"    isb\n"                                                                                \
"    mrs  x5, cntvct_el0\n"                                                                \
"    sub  x5, x5, x2\n"                                                                    \
"    str  x5, [x28, #0x208]\n"                                                             \
/* (c) 16 dependent ADDs per iteration -> clock estimate (1 cycle ALU latency) */          \
"    isb\n"                                                                                \
"    mrs  x2, cntvct_el0\n"                                                                \
"    mov  x3, x1\n"                                                                        \
"    mov  x6, #0\n"                                                                        \
"3:  add  x6, x6, #1\n"                                                                    \
"    add  x6, x6, #1\n"                                                                    \
"    add  x6, x6, #1\n"                                                                    \
"    add  x6, x6, #1\n"                                                                    \
"    add  x6, x6, #1\n"                                                                    \
"    add  x6, x6, #1\n"                                                                    \
"    add  x6, x6, #1\n"                                                                    \
"    add  x6, x6, #1\n"                                                                    \
"    add  x6, x6, #1\n"                                                                    \
"    add  x6, x6, #1\n"                                                                    \
"    add  x6, x6, #1\n"                                                                    \
"    add  x6, x6, #1\n"                                                                    \
"    add  x6, x6, #1\n"                                                                    \
"    add  x6, x6, #1\n"                                                                    \
"    add  x6, x6, #1\n"                                                                    \
"    add  x6, x6, #1\n"                                                                    \
"    subs x3, x3, #1\n"                                                                    \
"    b.ne 3b\n"                                                                            \
"    isb\n"                                                                                \
"    mrs  x5, cntvct_el0\n"                                                                \
"    sub  x5, x5, x2\n"                                                                    \
"    str  x5, [x28, #0x210]\n"                                                             \
"    str  x6, [x28, #0x218]\n"                                                             \
"    hvc  #0\n"                                                                            \
                                                                                           \
/* ---------------- mode 1: conservative materialisation (spill) ---------------- */       \
/* P0 iters, P1 rounds, P2 context-block VA, P3 body select                        */      \
"Lm_spill:\n"                                                                              \
"    ldr  x8,  [x28, #0x000]\n"                                                            \
"    ldr  x9,  [x28, #0x008]\n"                                                            \
"    ldr  x10, [x28, #0x010]\n"                                                            \
"    ldr  x12, [x28, #0x018]\n"                                                            \
"    add  x11, x28, #0x200\n"                                                              \
"    adr  x7, Lb_alu\n"                                                                    \
"    cmp  x12, #0\n"                                                                       \
"    b.eq 9f\n"                                                                            \
"    adr  x7, Lb_st1\n"                                                                    \
"    cmp  x12, #1\n"                                                                       \
"    b.eq 9f\n"                                                                            \
"    adr  x7, Lb_st2\n"                                                                    \
"    cmp  x12, #2\n"                                                                       \
"    b.eq 9f\n"                                                                            \
"    adr  x7, Lb_st4\n"                                                                    \
"    cmp  x12, #3\n"                                                                       \
"    b.eq 9f\n"                                                                            \
"    adr  x7, Lb_st8\n"                                                                    \
"    cmp  x12, #4\n"                                                                       \
"    b.eq 9f\n"                                                                            \
"    adr  x7, Lb_st16\n"                                                                   \
"    cmp  x12, #5\n"                                                                       \
"    b.eq 9f\n"                                                                            \
"    adr  x7, Lb_stp16\n"                                                                  \
"    cmp  x12, #6\n"                                                                       \
"    b.eq 9f\n"                                                                            \
"    adr  x7, Lb_ld16\n"                                                                   \
"    cmp  x12, #7\n"                                                                       \
"    b.eq 9f\n"                                                                            \
"    adr  x7, Lb_ldst16\n"                                                                 \
"    cmp  x12, #8\n"                                                                       \
"    b.eq 9f\n"                                                                            \
"    adr  x7, Lb_memld\n"                                                                  \
"    cmp  x12, #9\n"                                                                       \
"    b.eq 9f\n"                                                                            \
"    adr  x7, Lb_memld_st1\n"                                                              \
"    cmp  x12, #10\n"                                                                      \
"    b.eq 9f\n"                                                                            \
"    adr  x7, Lb_memld_st3\n"                                                              \
"    cmp  x12, #11\n"                                                                      \
"    b.eq 9f\n"                                                                            \
"    hvc  #9\n"                                                                            \
"9:  bl   Ltimeit\n"                                                                       \
"    hvc  #0\n"                                                                            \
                                                                                           \
/* Baseline: four independent ALU ops per iteration. Stand-in for the ARM expansion of one */\
/* simple x86 ALU instruction. x10 = context block (one 128-byte L1-resident line pair).   */\
"Lb_alu:\n"                                                                                \
"    mov  x15, x8\n"                                                                       \
"1:  add  x0, x0, #1\n"                                                                    \
"    eor  x1, x1, x0\n"                                                                    \
"    add  x2, x2, #3\n"                                                                    \
"    eor  x3, x3, x2\n"                                                                    \
"    subs x15, x15, #1\n"                                                                  \
"    b.ne 1b\n"                                                                            \
"    ret\n"                                                                                \
"Lb_st1:\n"                                                                                \
"    mov  x15, x8\n"                                                                       \
"1:  add  x0, x0, #1\n"                                                                    \
"    eor  x1, x1, x0\n"                                                                    \
"    add  x2, x2, #3\n"                                                                    \
"    eor  x3, x3, x2\n"                                                                    \
"    str  x0, [x10, #0]\n"                                                                 \
"    subs x15, x15, #1\n"                                                                  \
"    b.ne 1b\n"                                                                            \
"    ret\n"                                                                                \
"Lb_st2:\n"                                                                                \
"    mov  x15, x8\n"                                                                       \
"1:  add  x0, x0, #1\n"                                                                    \
"    eor  x1, x1, x0\n"                                                                    \
"    add  x2, x2, #3\n"                                                                    \
"    eor  x3, x3, x2\n"                                                                    \
"    str  x0, [x10, #0]\n"                                                                 \
"    str  x1, [x10, #8]\n"                                                                 \
"    subs x15, x15, #1\n"                                                                  \
"    b.ne 1b\n"                                                                            \
"    ret\n"                                                                                \
"Lb_st4:\n"                                                                                \
"    mov  x15, x8\n"                                                                       \
"1:  add  x0, x0, #1\n"                                                                    \
"    eor  x1, x1, x0\n"                                                                    \
"    add  x2, x2, #3\n"                                                                    \
"    eor  x3, x3, x2\n"                                                                    \
"    str  x0, [x10, #0]\n"                                                                 \
"    str  x1, [x10, #8]\n"                                                                 \
"    str  x2, [x10, #16]\n"                                                                \
"    str  x3, [x10, #24]\n"                                                                \
"    subs x15, x15, #1\n"                                                                  \
"    b.ne 1b\n"                                                                            \
"    ret\n"                                                                                \
"Lb_st8:\n"                                                                                \
"    mov  x15, x8\n"                                                                       \
"1:  add  x0, x0, #1\n"                                                                    \
"    eor  x1, x1, x0\n"                                                                    \
"    add  x2, x2, #3\n"                                                                    \
"    eor  x3, x3, x2\n"                                                                    \
"    str  x0, [x10, #0]\n"                                                                 \
"    str  x1, [x10, #8]\n"                                                                 \
"    str  x2, [x10, #16]\n"                                                                \
"    str  x3, [x10, #24]\n"                                                                \
"    str  x0, [x10, #32]\n"                                                                \
"    str  x1, [x10, #40]\n"                                                                \
"    str  x2, [x10, #48]\n"                                                                \
"    str  x3, [x10, #56]\n"                                                                \
"    subs x15, x15, #1\n"                                                                  \
"    b.ne 1b\n"                                                                            \
"    ret\n"                                                                                \
"Lb_st16:\n"                                                                               \
"    mov  x15, x8\n"                                                                       \
"1:  add  x0, x0, #1\n"                                                                    \
"    eor  x1, x1, x0\n"                                                                    \
"    add  x2, x2, #3\n"                                                                    \
"    eor  x3, x3, x2\n"                                                                    \
"    str  x0, [x10, #0]\n"                                                                 \
"    str  x1, [x10, #8]\n"                                                                 \
"    str  x2, [x10, #16]\n"                                                                \
"    str  x3, [x10, #24]\n"                                                                \
"    str  x0, [x10, #32]\n"                                                                \
"    str  x1, [x10, #40]\n"                                                                \
"    str  x2, [x10, #48]\n"                                                                \
"    str  x3, [x10, #56]\n"                                                                \
"    str  x0, [x10, #64]\n"                                                                \
"    str  x1, [x10, #72]\n"                                                                \
"    str  x2, [x10, #80]\n"                                                                \
"    str  x3, [x10, #88]\n"                                                                \
"    str  x0, [x10, #96]\n"                                                                \
"    str  x1, [x10, #104]\n"                                                               \
"    str  x2, [x10, #112]\n"                                                               \
"    str  x3, [x10, #120]\n"                                                               \
"    subs x15, x15, #1\n"                                                                  \
"    b.ne 1b\n"                                                                            \
"    ret\n"                                                                                \
"Lb_stp16:\n"                                                                              \
"    mov  x15, x8\n"                                                                       \
"1:  add  x0, x0, #1\n"                                                                    \
"    eor  x1, x1, x0\n"                                                                    \
"    add  x2, x2, #3\n"                                                                    \
"    eor  x3, x3, x2\n"                                                                    \
"    stp  x0, x1, [x10, #0]\n"                                                             \
"    stp  x2, x3, [x10, #16]\n"                                                            \
"    stp  x0, x1, [x10, #32]\n"                                                            \
"    stp  x2, x3, [x10, #48]\n"                                                            \
"    stp  x0, x1, [x10, #64]\n"                                                            \
"    stp  x2, x3, [x10, #80]\n"                                                            \
"    stp  x0, x1, [x10, #96]\n"                                                            \
"    stp  x2, x3, [x10, #112]\n"                                                           \
"    subs x15, x15, #1\n"                                                                  \
"    b.ne 1b\n"                                                                            \
"    ret\n"                                                                                \
"Lb_ld16:\n"                                                                               \
"    mov  x15, x8\n"                                                                       \
"1:  add  x0, x0, #1\n"                                                                    \
"    eor  x1, x1, x0\n"                                                                    \
"    add  x2, x2, #3\n"                                                                    \
"    eor  x3, x3, x2\n"                                                                    \
"    ldp  x4, x5, [x10, #0]\n"                                                             \
"    ldp  x6, x17, [x10, #16]\n"                                                           \
"    ldp  x4, x5, [x10, #32]\n"                                                            \
"    ldp  x6, x17, [x10, #48]\n"                                                           \
"    ldp  x4, x5, [x10, #64]\n"                                                            \
"    ldp  x6, x17, [x10, #80]\n"                                                           \
"    ldp  x4, x5, [x10, #96]\n"                                                            \
"    ldp  x6, x17, [x10, #112]\n"                                                          \
"    subs x15, x15, #1\n"                                                                  \
"    b.ne 1b\n"                                                                            \
"    ret\n"                                                                                \
/* store-all then load-all: the shape a strict "materialise at every fault point" JIT     */\
/* would emit if it also had to reload afterwards.                                        */\
"Lb_ldst16:\n"                                                                             \
"    mov  x15, x8\n"                                                                       \
"1:  add  x0, x0, #1\n"                                                                    \
"    eor  x1, x1, x0\n"                                                                    \
"    add  x2, x2, #3\n"                                                                    \
"    eor  x3, x3, x2\n"                                                                    \
"    stp  x0, x1, [x10, #0]\n"                                                             \
"    stp  x2, x3, [x10, #16]\n"                                                            \
"    stp  x0, x1, [x10, #32]\n"                                                            \
"    stp  x2, x3, [x10, #48]\n"                                                            \
"    stp  x0, x1, [x10, #64]\n"                                                            \
"    stp  x2, x3, [x10, #80]\n"                                                            \
"    stp  x0, x1, [x10, #96]\n"                                                            \
"    stp  x2, x3, [x10, #112]\n"                                                           \
"    ldp  x4, x5, [x10, #0]\n"                                                             \
"    ldp  x6, x17, [x10, #16]\n"                                                           \
"    ldp  x4, x5, [x10, #32]\n"                                                            \
"    ldp  x6, x17, [x10, #48]\n"                                                           \
"    ldp  x4, x5, [x10, #64]\n"                                                            \
"    ldp  x6, x17, [x10, #80]\n"                                                           \
"    ldp  x4, x5, [x10, #96]\n"                                                            \
"    ldp  x6, x17, [x10, #112]\n"                                                          \
"    subs x15, x15, #1\n"                                                                  \
"    b.ne 1b\n"                                                                            \
"    ret\n"                                                                                \
/* A memory-touching "x86 instruction": load + ALU + store, i.e. the class that can #PF.  */\
/* x12 (reused as a mask) is not available here; walk a 64-page window off x10 instead.   */\
"Lb_memld:\n"                                                                              \
"    mov  x15, x8\n"                                                                       \
"    mov  x18, #0\n"                                                                       \
"1:  and  x4, x18, #0x0f8\n"                                                               \
"    ldr  x5, [x10, x4]\n"                                                                 \
"    add  x5, x5, #1\n"                                                                    \
"    str  x5, [x10, x4]\n"                                                                 \
"    add  x18, x18, #8\n"                                                                  \
"    subs x15, x15, #1\n"                                                                  \
"    b.ne 1b\n"                                                                            \
"    ret\n"                                                                                \
/* the same, plus one store of the guest RIP to the context block per x86 instruction */   \
"Lb_memld_st1:\n"                                                                          \
"    mov  x15, x8\n"                                                                       \
"    mov  x18, #0\n"                                                                       \
"1:  and  x4, x18, #0x0f8\n"                                                               \
"    ldr  x5, [x10, x4]\n"                                                                 \
"    add  x5, x5, #1\n"                                                                    \
"    str  x5, [x10, x4]\n"                                                                 \
"    str  x18, [x10, #0x100]\n"                                                            \
"    add  x18, x18, #8\n"                                                                  \
"    subs x15, x15, #1\n"                                                                  \
"    b.ne 1b\n"                                                                            \
"    ret\n"                                                                                \
/* the same, plus RIP + two lazy-flag operand stores per x86 instruction */                \
"Lb_memld_st3:\n"                                                                          \
"    mov  x15, x8\n"                                                                       \
"    mov  x18, #0\n"                                                                       \
"1:  and  x4, x18, #0x0f8\n"                                                               \
"    ldr  x5, [x10, x4]\n"                                                                 \
"    add  x5, x5, #1\n"                                                                    \
"    str  x5, [x10, x4]\n"                                                                 \
"    str  x18, [x10, #0x100]\n"                                                            \
"    stp  x4, x5, [x10, #0x108]\n"                                                         \
"    add  x18, x18, #8\n"                                                                  \
"    subs x15, x15, #1\n"                                                                  \
"    b.ne 1b\n"                                                                            \
"    ret\n"                                                                                \
                                                                                           \
/* ---------------- mode 2: fault-path metadata lookup ---------------- */                 \
/* P0 table VA, P1 log2N (or icount), P2 iters, P3 rounds, P4 body, P5 sleb target        */\
"Lm_lookup:\n"                                                                             \
"    ldr  x10, [x28, #0x000]\n"                                                            \
"    ldr  x12, [x28, #0x008]\n"                                                            \
"    ldr  x8,  [x28, #0x010]\n"                                                            \
"    ldr  x9,  [x28, #0x018]\n"                                                            \
"    ldr  x18, [x28, #0x020]\n"                                                            \
"    add  x11, x28, #0x200\n"                                                              \
"    adr  x7, Lb_bins\n"                                                                   \
"    cmp  x18, #0\n"                                                                       \
"    b.eq 9f\n"                                                                            \
"    adr  x7, Lb_hash\n"                                                                   \
"    cmp  x18, #1\n"                                                                       \
"    b.eq 9f\n"                                                                            \
"    adr  x7, Lb_sleb\n"                                                                   \
"    cmp  x18, #2\n"                                                                       \
"    b.eq 9f\n"                                                                            \
"    adr  x7, Lb_rng\n"                                                                    \
"    cmp  x18, #3\n"                                                                       \
"    b.eq 9f\n"                                                                            \
"    hvc  #9\n"                                                                            \
"9:  bl   Ltimeit\n"                                                                       \
"    str  x6, [x28, #0xc00]\n"                                                             \
"    hvc  #0\n"                                                                            \
                                                                                           \
/* xorshift-only control: the key-generation cost that every lookup body pays. */          \
"Lb_rng:\n"                                                                                \
"    mov  x15, x8\n"                                                                       \
"    mov  x0, #0x2545\n"                                                                   \
"    movk x0, #0xf491, lsl #16\n"                                                          \
"    mov  x17, #1\n"                                                                       \
"    lsl  x17, x17, x12\n"                                                                 \
"    sub  x17, x17, #1\n"                                                                  \
"    mov  x2, #0\n"                                                                        \
"1:  eor  x0, x0, x0, lsl #13\n"                                                           \
"    eor  x0, x0, x0, lsr #7\n"                                                            \
"    eor  x0, x0, x0, lsl #17\n"                                                           \
"    and  x1, x0, x17\n"                                                                   \
"    add  x2, x2, x1\n"                                                                    \
"    subs x15, x15, #1\n"                                                                  \
"    b.ne 1b\n"                                                                            \
"    mov  x6, x2\n"                                                                        \
"    ret\n"                                                                                \
                                                                                           \
/* Branchless binary search over N sorted uint32 host-PC keys. Trip count is exactly       */\
/* log2(N) so the loop branch is perfectly predicted; the cost is log2(N) DEPENDENT loads. */\
"Lb_bins:\n"                                                                               \
"    mov  x15, x8\n"                                                                       \
"    mov  x0, #0x2545\n"                                                                   \
"    movk x0, #0xf491, lsl #16\n"                                                          \
"    mov  x17, #1\n"                                                                       \
"    lsl  x17, x17, x12\n"                                                                 \
"    sub  x17, x17, #1\n"                                                                  \
"1:  eor  x0, x0, x0, lsl #13\n"                                                           \
"    eor  x0, x0, x0, lsr #7\n"                                                            \
"    eor  x0, x0, x0, lsl #17\n"                                                           \
"    and  x1, x0, x17\n"                                                                   \
"    lsl  x1, x1, #2\n"                                                                    \
"    mov  x2, #0\n"                                                                        \
"    mov  x3, #1\n"                                                                        \
"    lsl  x3, x3, x12\n"                                                                   \
"    mov  x4, x12\n"                                                                       \
"2:  lsr  x5, x3, #1\n"                                                                    \
"    add  x18, x2, x5\n"                                                                   \
"    ldr  w6, [x10, x18, lsl #2]\n"                                                        \
"    cmp  w6, w1\n"                                                                        \
"    csel x2, x18, x2, ls\n"                                                               \
"    sub  x3, x3, x5\n"                                                                    \
"    subs x4, x4, #1\n"                                                                    \
"    b.ne 2b\n"                                                                            \
"    subs x15, x15, #1\n"                                                                  \
"    b.ne 1b\n"                                                                            \
"    mov  x6, x2\n"                                                                        \
"    ret\n"                                                                                \
                                                                                           \
/* Open-addressed hash lookup: 2 ALU to mix, 1 dependent load of a 16-byte entry, 1 cmp. */\
"Lb_hash:\n"                                                                               \
"    mov  x15, x8\n"                                                                       \
"    mov  x0, #0x2545\n"                                                                   \
"    movk x0, #0xf491, lsl #16\n"                                                          \
"    mov  x17, #1\n"                                                                       \
"    lsl  x17, x17, x12\n"                                                                 \
"    sub  x17, x17, #1\n"                                                                  \
"    mov  x2, #0\n"                                                                        \
"1:  eor  x0, x0, x0, lsl #13\n"                                                           \
"    eor  x0, x0, x0, lsr #7\n"                                                            \
"    eor  x0, x0, x0, lsl #17\n"                                                           \
"    and  x1, x0, x17\n"                                                                   \
"    eor  x5, x1, x1, lsr #11\n"                                                           \
"    and  x5, x5, x17\n"                                                                   \
"    add  x18, x10, x5, lsl #4\n"                                                          \
"    ldp  x4, x6, [x18]\n"                                                                 \
"    cmp  x4, x1\n"                                                                        \
"    csel x2, x6, x2, eq\n"                                                                \
"    subs x15, x15, #1\n"                                                                  \
"    b.ne 1b\n"                                                                            \
"    mov  x6, x2\n"                                                                        \
"    ret\n"                                                                                \
                                                                                           \
/* QEMU's actual mechanism: linear sleb128 decode of the per-TB search table until the    */\
/* accumulated host-PC exceeds the faulting host PC. INSN_START_WORDS = 3, plus one       */\
/* host-offset delta, so 4 sleb128 values per guest instruction.                          */\
/* x10 = table VA, x12 = icount, x18 = target host offset.                                */\
"Lb_sleb:\n"                                                                               \
"    mov  x15, x8\n"                                                                       \
"    ldr  x18, [x28, #0x028]\n"                                                            \
"Lsleb_outer:\n"                                                                           \
"    mov  x17, x10\n"                    /* p */                                           \
"    mov  x5,  #0\n"                     /* iter_pc */                                     \
"    mov  x6,  #0\n"                     /* data[0] */                                     \
"    mov  x1,  x12\n"                    /* rows remaining */                              \
"Lsleb_row:\n"                                                                             \
/* --- value 0 --- */                                                                      \
"    mov  x0, #0\n"                                                                        \
"    mov  x2, #0\n"                                                                        \
"1:  ldrb w3, [x17], #1\n"                                                                 \
"    and  x4, x3, #0x7f\n"                                                                 \
"    lsl  x4, x4, x2\n"                                                                    \
"    orr  x0, x0, x4\n"                                                                    \
"    add  x2, x2, #7\n"                                                                    \
"    tbnz w3, #7, 1b\n"                                                                    \
"    cmp  x2, #64\n"                                                                       \
"    b.hs 2f\n"                                                                            \
"    tbz  w3, #6, 2f\n"                                                                    \
"    mov  x4, #-1\n"                                                                       \
"    lsl  x4, x4, x2\n"                                                                    \
"    orr  x0, x0, x4\n"                                                                    \
"2:  add  x6, x6, x0\n"                                                                    \
/* --- value 1 --- */                                                                      \
"    mov  x0, #0\n"                                                                        \
"    mov  x2, #0\n"                                                                        \
"1:  ldrb w3, [x17], #1\n"                                                                 \
"    and  x4, x3, #0x7f\n"                                                                 \
"    lsl  x4, x4, x2\n"                                                                    \
"    orr  x0, x0, x4\n"                                                                    \
"    add  x2, x2, #7\n"                                                                    \
"    tbnz w3, #7, 1b\n"                                                                    \
"    cmp  x2, #64\n"                                                                       \
"    b.hs 2f\n"                                                                            \
"    tbz  w3, #6, 2f\n"                                                                    \
"    mov  x4, #-1\n"                                                                       \
"    lsl  x4, x4, x2\n"                                                                    \
"    orr  x0, x0, x4\n"                                                                    \
"2:  add  x6, x6, x0\n"                                                                    \
/* --- value 2 --- */                                                                      \
"    mov  x0, #0\n"                                                                        \
"    mov  x2, #0\n"                                                                        \
"1:  ldrb w3, [x17], #1\n"                                                                 \
"    and  x4, x3, #0x7f\n"                                                                 \
"    lsl  x4, x4, x2\n"                                                                    \
"    orr  x0, x0, x4\n"                                                                    \
"    add  x2, x2, #7\n"                                                                    \
"    tbnz w3, #7, 1b\n"                                                                    \
"    cmp  x2, #64\n"                                                                       \
"    b.hs 2f\n"                                                                            \
"    tbz  w3, #6, 2f\n"                                                                    \
"    mov  x4, #-1\n"                                                                       \
"    lsl  x4, x4, x2\n"                                                                    \
"    orr  x0, x0, x4\n"                                                                    \
"2:  add  x6, x6, x0\n"                                                                    \
/* --- host-offset delta --- */                                                            \
"    mov  x0, #0\n"                                                                        \
"    mov  x2, #0\n"                                                                        \
"1:  ldrb w3, [x17], #1\n"                                                                 \
"    and  x4, x3, #0x7f\n"                                                                 \
"    lsl  x4, x4, x2\n"                                                                    \
"    orr  x0, x0, x4\n"                                                                    \
"    add  x2, x2, #7\n"                                                                    \
"    tbnz w3, #7, 1b\n"                                                                    \
"    cmp  x2, #64\n"                                                                       \
"    b.hs 2f\n"                                                                            \
"    tbz  w3, #6, 2f\n"                                                                    \
"    mov  x4, #-1\n"                                                                       \
"    lsl  x4, x4, x2\n"                                                                    \
"    orr  x0, x0, x4\n"                                                                    \
"2:  add  x5, x5, x0\n"                                                                    \
"    cmp  x5, x18\n"                                                                       \
"    b.hi Lsleb_done\n"                                                                    \
"    subs x1, x1, #1\n"                                                                    \
"    b.ne Lsleb_row\n"                                                                     \
"Lsleb_done:\n"                                                                            \
"    subs x15, x15, #1\n"                                                                  \
"    b.ne Lsleb_outer\n"                                                                   \
"    mov  x6, x6\n"                                                                        \
"    ret\n"                                                                                \
                                                                                           \
/* ---------------- mode 3: AT-based fault-free operand probing ---------------- */        \
/* P0 addr A, P1 addr B, P2 iters, P3 rounds, P4 body, P5 page mask                       */\
"Lm_at:\n"                                                                                 \
"    ldr  x10, [x28, #0x000]\n"                                                            \
"    ldr  x12, [x28, #0x008]\n"                                                            \
"    ldr  x8,  [x28, #0x010]\n"                                                            \
"    ldr  x9,  [x28, #0x018]\n"                                                            \
"    ldr  x18, [x28, #0x020]\n"                                                            \
"    add  x11, x28, #0x200\n"                                                              \
"    adr  x7, Lb_st_plain\n"                                                               \
"    cmp  x18, #0\n"                                                                       \
"    b.eq 9f\n"                                                                            \
"    adr  x7, Lb_at1\n"                                                                    \
"    cmp  x18, #1\n"                                                                       \
"    b.eq 9f\n"                                                                            \
"    adr  x7, Lb_at1_st\n"                                                                 \
"    cmp  x18, #2\n"                                                                       \
"    b.eq 9f\n"                                                                            \
"    adr  x7, Lb_at2_st\n"                                                                 \
"    cmp  x18, #3\n"                                                                       \
"    b.eq 9f\n"                                                                            \
"    adr  x7, Lb_at2_1isb\n"                                                               \
"    cmp  x18, #4\n"                                                                       \
"    b.eq 9f\n"                                                                            \
"    adr  x7, Lb_at_raw\n"                                                                 \
"    cmp  x18, #5\n"                                                                       \
"    b.eq 9f\n"                                                                            \
"    hvc  #9\n"                                                                            \
"9:  bl   Ltimeit\n"                                                                       \
"    str  x6, [x28, #0xc00]\n"                                                             \
"    hvc  #0\n"                                                                            \
/* All five bodies walk the SAME address stream: base + (i & mask)*4096 + (i&31)*128. */   \
"Lb_st_plain:\n"                                                                           \
"    mov  x15, x8\n"                                                                       \
"    mov  x3, #0\n"                                                                        \
"1:  and  x0, x3, x12\n"                                                                   \
"    add  x0, x10, x0, lsl #12\n"                                                          \
"    and  x2, x3, #31\n"                                                                   \
"    add  x0, x0, x2, lsl #7\n"                                                            \
"    str  xzr, [x0]\n"                                                                     \
"    add  x3, x3, #1\n"                                                                    \
"    subs x15, x15, #1\n"                                                                  \
"    b.ne 1b\n"                                                                            \
"    ret\n"                                                                                \
/* AT S1E1WP (sys #0,c7,c9,#1) + ISB + read PAR_EL1, no store. */                          \
"Lb_at1:\n"                                                                                \
"    mov  x15, x8\n"                                                                       \
"    mov  x3, #0\n"                                                                        \
"    mov  x6, #0\n"                                                                        \
"1:  and  x0, x3, x12\n"                                                                   \
"    add  x0, x10, x0, lsl #12\n"                                                          \
"    and  x2, x3, #31\n"                                                                   \
"    add  x0, x0, x2, lsl #7\n"                                                            \
"    sys  #0, c7, c9, #1, x0\n"                                                            \
"    isb\n"                                                                                \
"    mrs  x6, par_el1\n"                                                                   \
"    add  x3, x3, #1\n"                                                                    \
"    subs x15, x15, #1\n"                                                                  \
"    b.ne 1b\n"                                                                            \
"    ret\n"                                                                                \
/* probe one operand, then commit the store */                                             \
"Lb_at1_st:\n"                                                                             \
"    mov  x15, x8\n"                                                                       \
"    mov  x3, #0\n"                                                                        \
"    mov  x6, #0\n"                                                                        \
"1:  and  x0, x3, x12\n"                                                                   \
"    add  x0, x10, x0, lsl #12\n"                                                          \
"    and  x2, x3, #31\n"                                                                   \
"    add  x0, x0, x2, lsl #7\n"                                                            \
"    sys  #0, c7, c9, #1, x0\n"                                                            \
"    isb\n"                                                                                \
"    mrs  x6, par_el1\n"                                                                   \
"    str  xzr, [x0]\n"                                                                     \
"    add  x3, x3, #1\n"                                                                    \
"    subs x15, x15, #1\n"                                                                  \
"    b.ne 1b\n"                                                                            \
"    ret\n"                                                                                \
/* probe TWO operands (the unaligned-across-a-page-boundary case), then commit */          \
"Lb_at2_st:\n"                                                                             \
"    mov  x15, x8\n"                                                                       \
"    mov  x3, #0\n"                                                                        \
"    mov  x6, #0\n"                                                                        \
"1:  and  x0, x3, x12\n"                                                                   \
"    add  x0, x10, x0, lsl #12\n"                                                          \
"    and  x2, x3, #31\n"                                                                   \
"    add  x0, x0, x2, lsl #7\n"                                                            \
"    add  x1, x0, #4096\n"                                                                 \
"    sys  #0, c7, c9, #1, x0\n"                                                            \
"    isb\n"                                                                                \
"    mrs  x6, par_el1\n"                                                                   \
"    sys  #0, c7, c9, #1, x1\n"                                                            \
"    isb\n"                                                                                \
"    mrs  x5, par_el1\n"                                                                   \
"    orr  x6, x6, x5\n"                                                                    \
"    str  xzr, [x0]\n"                                                                     \
"    add  x3, x3, #1\n"                                                                    \
"    subs x15, x15, #1\n"                                                                  \
"    b.ne 1b\n"                                                                            \
"    ret\n"                                                                                \
/* two ATs but only one ISB -- ARCHITECTURALLY WRONG (PAR_EL1 is single-copy), measured   */\
/* only to split the cost of `AT` from the cost of the mandatory `ISB`.                   */\
"Lb_at2_1isb:\n"                                                                           \
"    mov  x15, x8\n"                                                                       \
"    mov  x3, #0\n"                                                                        \
"    mov  x6, #0\n"                                                                        \
"1:  and  x0, x3, x12\n"                                                                   \
"    add  x0, x10, x0, lsl #12\n"                                                          \
"    and  x2, x3, #31\n"                                                                   \
"    add  x0, x0, x2, lsl #7\n"                                                            \
"    add  x1, x0, #4096\n"                                                                 \
"    sys  #0, c7, c9, #1, x0\n"                                                            \
"    sys  #0, c7, c9, #1, x1\n"                                                            \
"    isb\n"                                                                                \
"    mrs  x6, par_el1\n"                                                                   \
"    str  xzr, [x0]\n"                                                                     \
"    add  x3, x3, #1\n"                                                                    \
"    subs x15, x15, #1\n"                                                                  \
"    b.ne 1b\n"                                                                            \
"    ret\n"                                                                                \
/* AT alone, no ISB, no PAR read: the raw issue cost, an unusable lower bound. */          \
"Lb_at_raw:\n"                                                                             \
"    mov  x15, x8\n"                                                                       \
"    mov  x3, #0\n"                                                                        \
"1:  and  x0, x3, x12\n"                                                                   \
"    add  x0, x10, x0, lsl #12\n"                                                          \
"    and  x2, x3, #31\n"                                                                   \
"    add  x0, x0, x2, lsl #7\n"                                                            \
"    sys  #0, c7, c9, #1, x0\n"                                                            \
"    add  x3, x3, #1\n"                                                                    \
"    subs x15, x15, #1\n"                                                                  \
"    b.ne 1b\n"                                                                            \
"    ret\n"                                                                                \
                                                                                           \
/* ---------------- mode 5: AT correctness verification ---------------- */                \
/* P0..P2 = RW / RO / unmapped VA.  Results: 6 PAR_EL1 values (RP then WP for each).      */\
"Lm_atverify:\n"                                                                           \
"    add  x11, x28, #0x200\n"                                                              \
"    mov  x18, #0\n"                                                                       \
"1:  lsl  x0, x18, #3\n"                                                                   \
"    add  x0, x28, x0\n"                                                                   \
"    ldr  x0, [x0]\n"                                                                      \
"    sys  #0, c7, c9, #0, x0\n"                     /* AT S1E1RP */                        \
"    isb\n"                                                                                \
"    mrs  x1, par_el1\n"                                                                   \
"    str  x1, [x11], #8\n"                                                                 \
"    sys  #0, c7, c9, #1, x0\n"                     /* AT S1E1WP */                        \
"    isb\n"                                                                                \
"    mrs  x1, par_el1\n"                                                                   \
"    str  x1, [x11], #8\n"                                                                 \
"    add  x18, x18, #1\n"                                                                  \
"    cmp  x18, #3\n"                                                                       \
"    b.lo 1b\n"                                                                            \
"    str  x19, [x28, #0xc00]\n"                                                            \
"    hvc  #0\n"                                                                            \
                                                                                           \
/* ---------------- mode 4: fault path, trivial vs full recovery ---------------- */       \
/* P0 RO region base VA, P1 iters, P2 rounds, P3 page mask                                */\
"Lm_pf:\n"                                                                                 \
"    ldr  x10, [x28, #0x000]\n"                                                            \
"    ldr  x8,  [x28, #0x008]\n"                                                            \
"    ldr  x9,  [x28, #0x010]\n"                                                            \
"    ldr  x12, [x28, #0x018]\n"                                                            \
"    add  x11, x28, #0x200\n"                                                              \
"    adr  x7, Lb_pf\n"                                                                     \
"    bl   Ltimeit\n"                                                                       \
"    str  x19, [x28, #0xc00]\n"                                                            \
"    hvc  #0\n"                                                                            \
/* Uses only x0, x8, x10, x12, x15, x17 -- everything else is free for the handler.       */\
"Lb_pf:\n"                                                                                 \
"    mov  x15, x8\n"                                                                       \
"    mov  x0, #0\n"                                                                        \
"1:  and  x17, x0, x12\n"                                                                  \
"    add  x17, x10, x17, lsl #12\n"                                                        \
"    and  x1,  x0, #31\n"                                                                  \
"    add  x17, x17, x1, lsl #7\n"                                                          \
"    str  xzr, [x17]\n"                                                                    \
"    add  x0, x0, #1\n"                                                                    \
"    subs x15, x15, #1\n"                                                                  \
"    b.ne 1b\n"                                                                            \
"    ret\n"                                                                                \
                                                                                           \
/* ---------------- exception handlers ---------------- */                                 \
".globl _h_skip_cnt\n"                                                                     \
"_h_skip_cnt:\n"                                                                           \
"    mrs  x25, elr_el1\n"                                                                  \
"    add  x25, x25, #4\n"                                                                  \
"    msr  elr_el1, x25\n"                                                                  \
"    add  x19, x19, #1\n"                                                                  \
"    cmp  x19, x20\n"                                                                      \
"    b.hi Lh_panic\n"                                                                      \
"    eret\n"                                                                               \
                                                                                           \
/* Strategy (b) fault path, in full:                                                       */\
/*   1. spill x0-x17 to a stack frame (the host registers holding guest state)             */\
/*   2. read ELR/FAR/ESR                                                                   */\
/*   3. branchless binary search of a log2(N)-deep StackMap keyed on the faulting host PC  */\
/*   4. permute 16 spilled host registers into the guest CPUState in guest-register order  */\
/*   5. materialise RIP / CR2 / error code                                                 */\
/*   6. reload x0-x17 and ERET                                                             */\
".globl _h_recover\n"                                                                      \
"_h_recover:\n"                                                                            \
"    sub  sp, sp, #160\n"                                                                  \
"    stp  x0,  x1,  [sp, #0]\n"                                                            \
"    stp  x2,  x3,  [sp, #16]\n"                                                           \
"    stp  x4,  x5,  [sp, #32]\n"                                                           \
"    stp  x6,  x7,  [sp, #48]\n"                                                           \
"    stp  x8,  x9,  [sp, #64]\n"                                                           \
"    stp  x10, x11, [sp, #80]\n"                                                           \
"    stp  x12, x13, [sp, #96]\n"                                                           \
"    stp  x14, x15, [sp, #112]\n"                                                          \
"    stp  x16, x17, [sp, #128]\n"                                                          \
"    mrs  x25, elr_el1\n"                                                                  \
"    mrs  x26, far_el1\n"                                                                  \
"    mrs  x27, esr_el1\n"                                                                  \
/* key = (ELR ^ FAR>>12) & (N-1), scaled by 4 -- varies per fault, as a real one would */  \
"    eor  x1, x25, x26, lsr #12\n"                                                         \
"    mov  x0, #1\n"                                                                        \
"    lsl  x0, x0, x22\n"                                                                   \
"    sub  x0, x0, #1\n"                                                                    \
"    and  x1, x1, x0\n"                                                                    \
"    lsl  x1, x1, #2\n"                                                                    \
"    mov  x2, #0\n"                                                                        \
"    mov  x3, #1\n"                                                                        \
"    lsl  x3, x3, x22\n"                                                                   \
"    mov  x4, x22\n"                                                                       \
"1:  lsr  x5, x3, #1\n"                                                                    \
"    add  x6, x2, x5\n"                                                                    \
"    ldr  w7, [x21, x6, lsl #2]\n"                                                         \
"    cmp  w7, w1\n"                                                                        \
"    csel x2, x6, x2, ls\n"                                                                \
"    sub  x3, x3, x5\n"                                                                    \
"    subs x4, x4, #1\n"                                                                    \
"    b.ne 1b\n"                                                                            \
/* reconstruct: 16 guest GPRs moved from their host locations into the guest CPUState */   \
"    ldp  x8,  x9,  [sp, #0]\n"                                                            \
"    stp  x8,  x9,  [x23, #0]\n"                                                           \
"    ldp  x8,  x9,  [sp, #16]\n"                                                           \
"    stp  x8,  x9,  [x23, #16]\n"                                                          \
"    ldp  x8,  x9,  [sp, #32]\n"                                                           \
"    stp  x8,  x9,  [x23, #32]\n"                                                          \
"    ldp  x8,  x9,  [sp, #48]\n"                                                           \
"    stp  x8,  x9,  [x23, #48]\n"                                                          \
"    ldp  x8,  x9,  [sp, #64]\n"                                                           \
"    stp  x8,  x9,  [x23, #64]\n"                                                          \
"    ldp  x8,  x9,  [sp, #80]\n"                                                           \
"    stp  x8,  x9,  [x23, #80]\n"                                                          \
"    ldp  x8,  x9,  [sp, #96]\n"                                                           \
"    stp  x8,  x9,  [x23, #96]\n"                                                          \
"    ldp  x8,  x9,  [sp, #112]\n"                                                          \
"    stp  x8,  x9,  [x23, #112]\n"                                                         \
/* RIP, CR2, error code */                                                                 \
"    str  x25, [x23, #128]\n"                                                              \
"    str  x26, [x23, #136]\n"                                                              \
"    str  x27, [x23, #144]\n"                                                              \
"    str  x2,  [x23, #152]\n"                                                              \
"    ldp  x0,  x1,  [sp, #0]\n"                                                            \
"    ldp  x2,  x3,  [sp, #16]\n"                                                           \
"    ldp  x4,  x5,  [sp, #32]\n"                                                           \
"    ldp  x6,  x7,  [sp, #48]\n"                                                           \
"    ldp  x8,  x9,  [sp, #64]\n"                                                           \
"    ldp  x10, x11, [sp, #80]\n"                                                           \
"    ldp  x12, x13, [sp, #96]\n"                                                           \
"    ldp  x14, x15, [sp, #112]\n"                                                          \
"    ldp  x16, x17, [sp, #128]\n"                                                          \
"    add  sp, sp, #160\n"                                                                  \
"    add  x25, x25, #4\n"                                                                  \
"    msr  elr_el1, x25\n"                                                                  \
"    add  x19, x19, #1\n"                                                                  \
"    cmp  x19, x20\n"                                                                      \
"    b.hi Lh_panic\n"                                                                      \
"    eret\n"                                                                               \
                                                                                           \
/* Same as _h_recover but WITHOUT the StackMap search: isolates the search from the        */\
/* spill/permute/ERET scaffolding.                                                         */\
".globl _h_recover_nosearch\n"                                                             \
"_h_recover_nosearch:\n"                                                                   \
"    sub  sp, sp, #160\n"                                                                  \
"    stp  x0,  x1,  [sp, #0]\n"                                                            \
"    stp  x2,  x3,  [sp, #16]\n"                                                           \
"    stp  x4,  x5,  [sp, #32]\n"                                                           \
"    stp  x6,  x7,  [sp, #48]\n"                                                           \
"    stp  x8,  x9,  [sp, #64]\n"                                                           \
"    stp  x10, x11, [sp, #80]\n"                                                           \
"    stp  x12, x13, [sp, #96]\n"                                                           \
"    stp  x14, x15, [sp, #112]\n"                                                          \
"    stp  x16, x17, [sp, #128]\n"                                                          \
"    mrs  x25, elr_el1\n"                                                                  \
"    mrs  x26, far_el1\n"                                                                  \
"    mrs  x27, esr_el1\n"                                                                  \
"    mov  x2, #0\n"                                                                        \
"    ldp  x8,  x9,  [sp, #0]\n"                                                            \
"    stp  x8,  x9,  [x23, #0]\n"                                                           \
"    ldp  x8,  x9,  [sp, #16]\n"                                                           \
"    stp  x8,  x9,  [x23, #16]\n"                                                          \
"    ldp  x8,  x9,  [sp, #32]\n"                                                           \
"    stp  x8,  x9,  [x23, #32]\n"                                                          \
"    ldp  x8,  x9,  [sp, #48]\n"                                                           \
"    stp  x8,  x9,  [x23, #48]\n"                                                          \
"    ldp  x8,  x9,  [sp, #64]\n"                                                           \
"    stp  x8,  x9,  [x23, #64]\n"                                                          \
"    ldp  x8,  x9,  [sp, #80]\n"                                                           \
"    stp  x8,  x9,  [x23, #80]\n"                                                          \
"    ldp  x8,  x9,  [sp, #96]\n"                                                           \
"    stp  x8,  x9,  [x23, #96]\n"                                                          \
"    ldp  x8,  x9,  [sp, #112]\n"                                                          \
"    stp  x8,  x9,  [x23, #112]\n"                                                         \
"    str  x25, [x23, #128]\n"                                                              \
"    str  x26, [x23, #136]\n"                                                              \
"    str  x27, [x23, #144]\n"                                                              \
"    str  x2,  [x23, #152]\n"                                                              \
"    ldp  x0,  x1,  [sp, #0]\n"                                                            \
"    ldp  x2,  x3,  [sp, #16]\n"                                                           \
"    ldp  x4,  x5,  [sp, #32]\n"                                                           \
"    ldp  x6,  x7,  [sp, #48]\n"                                                           \
"    ldp  x8,  x9,  [sp, #64]\n"                                                           \
"    ldp  x10, x11, [sp, #80]\n"                                                           \
"    ldp  x12, x13, [sp, #96]\n"                                                           \
"    ldp  x14, x15, [sp, #112]\n"                                                          \
"    ldp  x16, x17, [sp, #128]\n"                                                          \
"    add  sp, sp, #160\n"                                                                  \
"    add  x25, x25, #4\n"                                                                  \
"    msr  elr_el1, x25\n"                                                                  \
"    add  x19, x19, #1\n"                                                                  \
"    cmp  x19, x20\n"                                                                      \
"    b.hi Lh_panic\n"                                                                      \
"    eret\n"                                                                               \
                                                                                           \
/* Spill + ERET only: the cost of merely getting scratch registers in the handler. */      \
".globl _h_spillonly\n"                                                                    \
"_h_spillonly:\n"                                                                          \
"    sub  sp, sp, #160\n"                                                                  \
"    stp  x0,  x1,  [sp, #0]\n"                                                            \
"    stp  x2,  x3,  [sp, #16]\n"                                                           \
"    stp  x4,  x5,  [sp, #32]\n"                                                           \
"    stp  x6,  x7,  [sp, #48]\n"                                                           \
"    stp  x8,  x9,  [sp, #64]\n"                                                           \
"    stp  x10, x11, [sp, #80]\n"                                                           \
"    stp  x12, x13, [sp, #96]\n"                                                           \
"    stp  x14, x15, [sp, #112]\n"                                                          \
"    stp  x16, x17, [sp, #128]\n"                                                          \
"    mrs  x25, elr_el1\n"                                                                  \
"    mrs  x26, far_el1\n"                                                                  \
"    mrs  x27, esr_el1\n"                                                                  \
"    ldp  x0,  x1,  [sp, #0]\n"                                                            \
"    ldp  x2,  x3,  [sp, #16]\n"                                                           \
"    ldp  x4,  x5,  [sp, #32]\n"                                                           \
"    ldp  x6,  x7,  [sp, #48]\n"                                                           \
"    ldp  x8,  x9,  [sp, #64]\n"                                                           \
"    ldp  x10, x11, [sp, #80]\n"                                                           \
"    ldp  x12, x13, [sp, #96]\n"                                                           \
"    ldp  x14, x15, [sp, #112]\n"                                                          \
"    ldp  x16, x17, [sp, #128]\n"                                                          \
"    add  sp, sp, #160\n"                                                                  \
"    add  x25, x25, #4\n"                                                                  \
"    msr  elr_el1, x25\n"                                                                  \
"    add  x19, x19, #1\n"                                                                  \
"    cmp  x19, x20\n"                                                                      \
"    b.hi Lh_panic\n"                                                                      \
"    eret\n"                                                                               \
                                                                                           \
".globl _h_panic\n"                                                                        \
"Lh_panic:\n"                                                                              \
"_h_panic:\n"                                                                              \
"    mrs  x25, esr_el1\n"                                                                  \
"    mrs  x26, far_el1\n"                                                                  \
"    mrs  x27, elr_el1\n"                                                                  \
"    hvc  #9\n"                                                                            \
".globl _pe_end\n"                                                                         \
"_pe_end:\n"                                                                               \
".p2align 2\n"
