// Guest EL1 payload for the shadow-MMU cost probe.
//
// Everything in here runs INSIDE the VM at EL1 with our own VBAR_EL1. No host exit occurs
// between the two CNTVCT_EL0 reads that bracket any measurement.
//
// Register discipline
// -------------------
//   x28            param/result block VA (identity mapped)
//   x19            fault counter (incremented by the instrumented handlers)
//   x20            fault limit  (handler panics via HVC #9 past this -> no wedged machine)
//   x21            shadow L3 table VA
//   x22            fake x86 CR3 (VA == IPA, identity mapped)
//   x23            x86/ARM output-address mask 0x0000_ffff_ffff_f000
//   x24            base ARM stage-1 page attributes for a filled shadow descriptor
//   x25,x26,x27    handler scratch -- never used by measurement bodies
//   x7..x14        `Ltimeit` parameters and state -- never clobbered by measurement bodies
//   x16            holds t0 ACROSS the blr -- measurement bodies must not touch it
//   x0-x6,x15,x18  free for measurement bodies
//
// `Ltimeit` protocol:  x7 = body, x8 = count, x9 = rounds, x10 = base VA, x11 = result cursor,
//                     x12 = extra.  Result r lands at [x11], x11 post-incremented by 8.

#define PAYLOAD_ASM                                                                        \
".section __TEXT,__text\n"                                                                 \
".p2align 2\n"                                                                             \
".globl _sm_start\n"                                                                       \
"_sm_start:\n"                                                                             \
/* x0=MAIR x1=TCR x2=TTBR0 x3=VBAR x4=param-block VA x5=mode x6=EnTSO */                    \
"    msr  mair_el1, x0\n"                                                                  \
"    msr  tcr_el1,  x1\n"                                                                  \
"    msr  ttbr0_el1, x2\n"                                                                 \
"    msr  vbar_el1, x3\n"                                                                  \
"    cbz  x6, 1f\n"                                                                        \
"    mrs  x9, actlr_el1\n"                                                                 \
"    orr  x9, x9, #2\n"                                                                    \
"    msr  actlr_el1, x9\n"                                                                 \
"1:  isb\n"                                                                                \
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
"    mrs  x0, cntfrq_el0\n"                                                                \
"    str  x0, [x28, #0xc18]\n"                                                             \
"    mrs  x0, actlr_el1\n"                                                                 \
"    str  x0, [x28, #0xc10]\n"                                                             \
"    cmp  x5, #0\n"                                                                        \
"    b.eq Lm_cal\n"                                                                         \
"    cmp  x5, #1\n"                                                                        \
"    b.eq Lm_fault\n"                                                                       \
"    cmp  x5, #2\n"                                                                        \
"    b.eq Lm_tlbi\n"                                                                        \
"    cmp  x5, #3\n"                                                                        \
"    b.eq Lm_chase\n"                                                                       \
"    cmp  x5, #4\n"                                                                        \
"    b.eq Lm_shadow\n"                                                                      \
"    cmp  x5, #5\n"                                                                        \
"    b.eq Lm_asid\n"                                                                        \
"    cmp  x5, #6\n"                                                                        \
"    b.eq Lm_ad\n"                                                                          \
"    cmp  x5, #7\n"                                                                        \
"    b.eq Lm_tso\n"                                                                         \
"    cmp  x5, #8\n"                                                                        \
"    b.eq Lm_spin\n"                                                                        \
"    hvc  #9\n"                                                                            \
                                                                                           \
/* ---------------- Ltimeit ---------------- */                                             \
"Ltimeit:\n"                                                                                \
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
"Lm_cal:\n"                                                                                 \
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
/* (c) 16 dependent ADDs per iteration -> clock estimate (1 cycle ALU latency) */           \
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
/* (d) is PMCCNTR_EL0 reachable from guest EL1? handler sets x27=1 and skips if not */      \
"    mov  x27, #0\n"                                                                       \
"    mov  x26, #0\n"                                                                       \
"    mrs  x26, pmccntr_el0\n"                                                              \
"    str  x27, [x28, #0x220]\n"                                                            \
"    str  x26, [x28, #0x228]\n"                                                            \
"    hvc  #0\n"                                                                            \
                                                                                           \
/* ---------------- mode 1: in-VM fault round trip ---------------- */                     \
/* P0/P1/P2 = unmapped / RO / RW base VAs, P3 iters, P4 rounds, P5 page-index mask */       \
"Lm_fault:\n"                                                                               \
"    ldr  x8,  [x28, #0x018]\n"                                                            \
"    ldr  x9,  [x28, #0x020]\n"                                                            \
"    ldr  x12, [x28, #0x028]\n"                                                            \
"    add  x11, x28, #0x200\n"                                                              \
"    mov  x18, #0\n"                                                                       \
"Lmf_next:\n"                                                                               \
"    lsl  x0, x18, #3\n"                                                                   \
"    add  x0, x28, x0\n"                                                                   \
"    ldr  x10, [x0]\n"                                                                     \
"    adr  x7, Lb_touchloop\n"                                                               \
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
"    add  x18, x18, #1\n"                                                                  \
"    cmp  x18, #3\n"                                                                       \
"    b.lo Lmf_next\n"                                                                       \
"    str  x19, [x28, #0xc00]\n"                                                            \
"    hvc  #0\n"                                                                            \
/* x10 base, x8 iters, x12 mask. Identical loop shape for faulting and non-faulting base */\
"Lb_touchloop:\n"                                                                           \
"    mov  x15, x8\n"                                                                       \
"    mov  x3, #0\n"                                                                        \
"1:  and  x1, x3, x12\n"                                                                   \
"    add  x1, x10, x1, lsl #12\n"                                                          \
"    and  x2, x3, #31\n"                                                                   \
"    add  x1, x1, x2, lsl #7\n"                                                            \
"    str  xzr, [x1]\n"                                                                     \
"    add  x3, x3, #1\n"                                                                    \
"    subs x15, x15, #1\n"                                                                  \
"    b.ne 1b\n"                                                                            \
"    ret\n"                                                                                \
                                                                                           \
/* ---------------- mode 2: TLB maintenance ---------------- */                            \
"Lm_tlbi:\n"                                                                                \
"    ldr  x8,  [x28, #0x000]\n"                                                            \
"    ldr  x9,  [x28, #0x008]\n"                                                            \
"    ldr  x10, [x28, #0x010]\n"                                                            \
"    lsr  x10, x10, #12\n"                                                                 \
"    add  x11, x28, #0x200\n"                                                              \
"    adr  x7, Lb_nop\n"                                                                     \
"    bl   Ltimeit\n"                                                                        \
"    adr  x7, Lb_isb\n"                                                                     \
"    bl   Ltimeit\n"                                                                        \
"    adr  x7, Lb_dsbish\n"                                                                  \
"    bl   Ltimeit\n"                                                                        \
"    adr  x7, Lb_vae1\n"                                                                    \
"    bl   Ltimeit\n"                                                                        \
"    adr  x7, Lb_vae1is\n"                                                                  \
"    bl   Ltimeit\n"                                                                        \
"    adr  x7, Lb_vaae1is\n"                                                                 \
"    bl   Ltimeit\n"                                                                        \
"    adr  x7, Lb_vmalle1\n"                                                                 \
"    bl   Ltimeit\n"                                                                        \
"    adr  x7, Lb_vmalle1is\n"                                                               \
"    bl   Ltimeit\n"                                                                        \
"    hvc  #0\n"                                                                            \
"Lb_nop:\n"                                                                                 \
"    mov  x15, x8\n"                                                                       \
"1:  subs x15, x15, #1\n"                                                                  \
"    b.ne 1b\n"                                                                            \
"    ret\n"                                                                                \
"Lb_isb:\n"                                                                                 \
"    mov  x15, x8\n"                                                                       \
"1:  isb\n"                                                                                \
"    subs x15, x15, #1\n"                                                                  \
"    b.ne 1b\n"                                                                            \
"    ret\n"                                                                                \
"Lb_dsbish:\n"                                                                              \
"    mov  x15, x8\n"                                                                       \
"1:  dsb  ishst\n"                                                                         \
"    dsb  ish\n"                                                                           \
"    isb\n"                                                                                \
"    subs x15, x15, #1\n"                                                                  \
"    b.ne 1b\n"                                                                            \
"    ret\n"                                                                                \
"Lb_vae1:\n"                                                                                \
"    mov  x15, x8\n"                                                                       \
"1:  dsb  nshst\n"                                                                         \
"    tlbi vae1, x10\n"                                                                     \
"    dsb  nsh\n"                                                                           \
"    isb\n"                                                                                \
"    subs x15, x15, #1\n"                                                                  \
"    b.ne 1b\n"                                                                            \
"    ret\n"                                                                                \
"Lb_vae1is:\n"                                                                              \
"    mov  x15, x8\n"                                                                       \
"1:  dsb  ishst\n"                                                                         \
"    tlbi vae1is, x10\n"                                                                   \
"    dsb  ish\n"                                                                           \
"    isb\n"                                                                                \
"    subs x15, x15, #1\n"                                                                  \
"    b.ne 1b\n"                                                                            \
"    ret\n"                                                                                \
"Lb_vaae1is:\n"                                                                             \
"    mov  x15, x8\n"                                                                       \
"1:  dsb  ishst\n"                                                                         \
"    tlbi vaae1is, x10\n"                                                                  \
"    dsb  ish\n"                                                                           \
"    isb\n"                                                                                \
"    subs x15, x15, #1\n"                                                                  \
"    b.ne 1b\n"                                                                            \
"    ret\n"                                                                                \
"Lb_vmalle1:\n"                                                                             \
"    mov  x15, x8\n"                                                                       \
"1:  dsb  nshst\n"                                                                         \
"    tlbi vmalle1\n"                                                                       \
"    dsb  nsh\n"                                                                           \
"    isb\n"                                                                                \
"    subs x15, x15, #1\n"                                                                  \
"    b.ne 1b\n"                                                                            \
"    ret\n"                                                                                \
"Lb_vmalle1is:\n"                                                                           \
"    mov  x15, x8\n"                                                                       \
"1:  dsb  ishst\n"                                                                         \
"    tlbi vmalle1is\n"                                                                     \
"    dsb  ish\n"                                                                           \
"    isb\n"                                                                                \
"    subs x15, x15, #1\n"                                                                  \
"    b.ne 1b\n"                                                                            \
"    ret\n"                                                                                \
                                                                                           \
/* ---------------- mode 3: pointer chase (TLB reach) ---------------- */                  \
"Lm_chase:\n"                                                                               \
"    ldr  x10, [x28, #0x000]\n"                                                            \
"    ldr  x8,  [x28, #0x008]\n"                                                            \
"    ldr  x9,  [x28, #0x010]\n"                                                            \
"    ldr  x12, [x28, #0x018]\n"                                                            \
"    add  x11, x28, #0x200\n"                                                              \
"    mov  x0, x10\n"                                                                       \
"    mov  x15, x12\n"                                                                      \
"1:  ldr  x0, [x0]\n"                                                                      \
"    subs x15, x15, #1\n"                                                                  \
"    b.ne 1b\n"                                                                            \
"    adr  x7, Lb_chase\n"                                                                   \
"    bl   Ltimeit\n"                                                                        \
"    str  x6, [x28, #0xc08]\n"                                                             \
"    hvc  #0\n"                                                                            \
"Lb_chase:\n"                                                                               \
"    mov  x0, x10\n"                                                                       \
"    mov  x15, x8\n"                                                                       \
"1:  ldr  x0, [x0]\n"                                                                      \
"    subs x15, x15, #1\n"                                                                  \
"    b.ne 1b\n"                                                                            \
"    mov  x6, x0\n"                                                                        \
"    ret\n"                                                                                \
                                                                                           \
/* ---------------- mode 4: shadow fill ---------------- */                                \
/* P0 pages, P1 rounds, P2 region base VA */                                               \
"Lm_shadow:\n"                                                                              \
"    ldr  x8,  [x28, #0x000]\n"                                                            \
"    ldr  x9,  [x28, #0x008]\n"                                                            \
"    ldr  x10, [x28, #0x010]\n"                                                            \
"    add  x11, x28, #0x200\n"                                                              \
"    mov  x14, x9\n"                                                                       \
"Lms_round:\n"                                                                              \
"    mov  x15, #0\n"                                                                       \
"1:  str  xzr, [x21, x15, lsl #3]\n"                                                       \
"    add  x15, x15, #1\n"                                                                  \
"    cmp  x15, x8\n"                                                                       \
"    b.lo 1b\n"                                                                            \
"    dsb  ishst\n"                                                                         \
"    tlbi vmalle1is\n"                                                                     \
"    dsb  ish\n"                                                                           \
"    isb\n"                                                                                \
"    isb\n"                                                                                \
"    mrs  x16, cntvct_el0\n"                                                               \
"    mov  x0, x10\n"                                                                       \
"    mov  x15, x8\n"                                                                       \
"2:  ldr  x1, [x0]\n"                                                                      \
"    add  x0, x0, #4096\n"                                                                 \
"    subs x15, x15, #1\n"                                                                  \
"    b.ne 2b\n"                                                                            \
"    isb\n"                                                                                \
"    mrs  x17, cntvct_el0\n"                                                               \
"    sub  x17, x17, x16\n"                                                                 \
"    str  x17, [x11], #8\n"                                                                \
"    subs x14, x14, #1\n"                                                                  \
"    b.ne Lms_round\n"                                                                      \
"    str  x19, [x28, #0xc00]\n"                                                            \
/* components, all fault-free, same count and same VA stream */                            \
"    adr  x7, Lb_x86walk\n"                                                                 \
"    bl   Ltimeit\n"                                                                        \
"    adr  x7, Lb_walkfill\n"                                                                \
"    bl   Ltimeit\n"                                                                        \
"    adr  x7, Lb_tlbiseq\n"                                                                 \
"    bl   Ltimeit\n"                                                                        \
"    adr  x7, Lb_touchonly\n"                                                               \
"    bl   Ltimeit\n"                                                                        \
"    hvc  #0\n"                                                                            \
/* 4 dependent loads through a 4-level x86-format table, x8 VAs from x10 */                \
"Lb_x86walk:\n"                                                                             \
"    mov  x0, x10\n"                                                                       \
"    mov  x15, x8\n"                                                                       \
"1:  ubfx x1, x0, #39, #9\n"                                                               \
"    add  x1, x22, x1, lsl #3\n"                                                           \
"    ldr  x1, [x1]\n"                                                                      \
"    and  x1, x1, x23\n"                                                                   \
"    ubfx x2, x0, #30, #9\n"                                                               \
"    add  x2, x1, x2, lsl #3\n"                                                            \
"    ldr  x1, [x2]\n"                                                                      \
"    and  x1, x1, x23\n"                                                                   \
"    ubfx x2, x0, #21, #9\n"                                                               \
"    add  x2, x1, x2, lsl #3\n"                                                            \
"    ldr  x1, [x2]\n"                                                                      \
"    and  x1, x1, x23\n"                                                                   \
"    ubfx x2, x0, #12, #9\n"                                                               \
"    add  x2, x1, x2, lsl #3\n"                                                            \
"    ldr  x1, [x2]\n"                                                                      \
"    mov  x6, x1\n"                                                                        \
"    add  x0, x0, #4096\n"                                                                 \
"    subs x15, x15, #1\n"                                                                  \
"    b.ne 1b\n"                                                                            \
"    ret\n"                                                                                \
/* walk + x86->ARM descriptor translation + store into the shadow L3 (no TLBI) */          \
"Lb_walkfill:\n"                                                                            \
"    mov  x0, x10\n"                                                                       \
"    mov  x15, x8\n"                                                                       \
"1:  ubfx x1, x0, #39, #9\n"                                                               \
"    add  x1, x22, x1, lsl #3\n"                                                           \
"    ldr  x1, [x1]\n"                                                                      \
"    and  x1, x1, x23\n"                                                                   \
"    ubfx x2, x0, #30, #9\n"                                                               \
"    add  x2, x1, x2, lsl #3\n"                                                            \
"    ldr  x1, [x2]\n"                                                                      \
"    and  x1, x1, x23\n"                                                                   \
"    ubfx x2, x0, #21, #9\n"                                                               \
"    add  x2, x1, x2, lsl #3\n"                                                            \
"    ldr  x1, [x2]\n"                                                                      \
"    and  x1, x1, x23\n"                                                                   \
"    ubfx x2, x0, #12, #9\n"                                                               \
"    add  x2, x1, x2, lsl #3\n"                                                            \
"    ldr  x1, [x2]\n"                                                                      \
"    and  x2, x1, x23\n"                                                                   \
"    orr  x2, x2, x24\n"                                                                   \
"    tbnz x1, #1, 2f\n"                                                                    \
"    orr  x2, x2, #0x80\n"                                                                 \
"2:  ubfx x3, x0, #12, #9\n"                                                               \
"    add  x3, x21, x3, lsl #3\n"                                                           \
"    str  x2, [x3]\n"                                                                      \
"    dsb  ishst\n"                                                                         \
"    add  x0, x0, #4096\n"                                                                 \
"    subs x15, x15, #1\n"                                                                  \
"    b.ne 1b\n"                                                                            \
"    ret\n"                                                                                \
/* the invalidate half of the handler, x8 times over the same VA stream */                 \
"Lb_tlbiseq:\n"                                                                             \
"    mov  x0, x10\n"                                                                       \
"    mov  x15, x8\n"                                                                       \
"1:  dsb  ishst\n"                                                                         \
"    lsr  x1, x0, #12\n"                                                                   \
"    tlbi vaae1is, x1\n"                                                                   \
"    dsb  ish\n"                                                                           \
"    isb\n"                                                                                \
"    add  x0, x0, #4096\n"                                                                 \
"    subs x15, x15, #1\n"                                                                  \
"    b.ne 1b\n"                                                                            \
"    ret\n"                                                                                \
/* the bare touch loop with everything already mapped: the floor for mode 4 */              \
"Lb_touchonly:\n"                                                                           \
"    mov  x0, x10\n"                                                                       \
"    mov  x15, x8\n"                                                                       \
"1:  ldr  x1, [x0]\n"                                                                      \
"    add  x0, x0, #4096\n"                                                                 \
"    subs x15, x15, #1\n"                                                                  \
"    b.ne 1b\n"                                                                            \
"    mov  x6, x1\n"                                                                        \
"    ret\n"                                                                                \
                                                                                           \
/* ---------------- mode 5: ASID ---------------- */                                       \
/* P0 head VA, P1 steps, P2 rounds, P3 TTBR0 base, P4 warm steps */                        \
"Lm_asid:\n"                                                                                \
"    ldr  x10, [x28, #0x000]\n"                                                            \
"    ldr  x8,  [x28, #0x008]\n"                                                            \
"    ldr  x9,  [x28, #0x010]\n"                                                            \
"    ldr  x12, [x28, #0x018]\n"                                                            \
"    ldr  x18, [x28, #0x020]\n"                                                            \
"    add  x11, x28, #0x200\n"                                                              \
"    mov  x0, x10\n"                                                                       \
"    mov  x15, x18\n"                                                                      \
"1:  ldr  x0, [x0]\n"                                                                      \
"    subs x15, x15, #1\n"                                                                  \
"    b.ne 1b\n"                                                                            \
"    adr  x7, Lb_chase\n"                                                                   \
"    bl   Ltimeit\n"                                                                        \
"    adr  x7, Lb_asidswitch\n"                                                              \
"    bl   Ltimeit\n"                                                                        \
"    msr  ttbr0_el1, x12\n"                                                                \
"    isb\n"                                                                                \
"    adr  x7, Lb_chase_switchback\n"                                                        \
"    bl   Ltimeit\n"                                                                        \
"    mov  x5, #0\n"                                                                        \
"    adr  x7, Lb_chase_newasid\n"                                                           \
"    bl   Ltimeit\n"                                                                        \
"    msr  ttbr0_el1, x12\n"                                                                \
"    isb\n"                                                                                \
"    mov  x0, x10\n"                                                                       \
"    mov  x15, x18\n"                                                                      \
"2:  ldr  x0, [x0]\n"                                                                      \
"    subs x15, x15, #1\n"                                                                  \
"    b.ne 2b\n"                                                                            \
"    adr  x7, Lb_chase_flushed\n"                                                           \
"    bl   Ltimeit\n"                                                                        \
"    hvc  #0\n"                                                                            \
"Lb_asidswitch:\n"                                                                          \
"    mov  x15, x8\n"                                                                       \
"    mov  x1, #1\n"                                                                        \
"    lsl  x1, x1, #48\n"                                                                   \
"    orr  x1, x1, x12\n"                                                                   \
"1:  msr  ttbr0_el1, x1\n"                                                                 \
"    isb\n"                                                                                \
"    msr  ttbr0_el1, x12\n"                                                                \
"    isb\n"                                                                                \
"    subs x15, x15, #1\n"                                                                  \
"    b.ne 1b\n"                                                                            \
"    ret\n"                                                                                \
"Lb_chase_switchback:\n"                                                                    \
"    mov  x1, #1\n"                                                                        \
"    lsl  x1, x1, #48\n"                                                                   \
"    orr  x1, x1, x12\n"                                                                   \
"    msr  ttbr0_el1, x1\n"                                                                 \
"    isb\n"                                                                                \
"    msr  ttbr0_el1, x12\n"                                                                \
"    isb\n"                                                                                \
"    mov  x0, x10\n"                                                                       \
"    mov  x15, x8\n"                                                                       \
"1:  ldr  x0, [x0]\n"                                                                      \
"    subs x15, x15, #1\n"                                                                  \
"    b.ne 1b\n"                                                                            \
"    mov  x6, x0\n"                                                                        \
"    ret\n"                                                                                \
"Lb_chase_newasid:\n"                                                                       \
"    add  x5, x5, #1\n"                                                                    \
"    and  x5, x5, #0x7f\n"                                                                 \
"    add  x1, x5, #1\n"                                                                    \
"    lsl  x1, x1, #48\n"                                                                   \
"    orr  x1, x1, x12\n"                                                                   \
"    msr  ttbr0_el1, x1\n"                                                                 \
"    isb\n"                                                                                \
"    mov  x0, x10\n"                                                                       \
"    mov  x15, x8\n"                                                                       \
"1:  ldr  x0, [x0]\n"                                                                      \
"    subs x15, x15, #1\n"                                                                  \
"    b.ne 1b\n"                                                                            \
"    mov  x6, x0\n"                                                                        \
"    ret\n"                                                                                \
"Lb_chase_flushed:\n"                                                                       \
"    dsb  ishst\n"                                                                         \
"    tlbi vmalle1is\n"                                                                     \
"    dsb  ish\n"                                                                           \
"    isb\n"                                                                                \
"    mov  x0, x10\n"                                                                       \
"    mov  x15, x8\n"                                                                       \
"1:  ldr  x0, [x0]\n"                                                                      \
"    subs x15, x15, #1\n"                                                                  \
"    b.ne 1b\n"                                                                            \
"    mov  x6, x0\n"                                                                        \
"    ret\n"                                                                                \
                                                                                           \
/* ---------------- mode 6: A/D emulation ---------------- */                              \
/* P0 region base VA, P1 pages, P2 rounds */                                               \
"Lm_ad:\n"                                                                                  \
"    ldr  x10, [x28, #0x000]\n"                                                            \
"    ldr  x8,  [x28, #0x008]\n"                                                            \
"    ldr  x9,  [x28, #0x010]\n"                                                            \
"    add  x11, x28, #0x200\n"                                                              \
"    mov  x14, x9\n"                                                                       \
"Lma_round:\n"                                                                              \
"    mov  x15, #0\n"                                                                       \
"1:  ldr  x0, [x21, x15, lsl #3]\n"                                                        \
"    and  x0, x0, #0xfffffffffffffbff\n"                                                   \
"    str  x0, [x21, x15, lsl #3]\n"                                                        \
"    add  x15, x15, #1\n"                                                                  \
"    cmp  x15, x8\n"                                                                       \
"    b.lo 1b\n"                                                                            \
"    dsb  ishst\n"                                                                         \
"    tlbi vmalle1is\n"                                                                     \
"    dsb  ish\n"                                                                           \
"    isb\n"                                                                                \
"    isb\n"                                                                                \
"    mrs  x16, cntvct_el0\n"                                                               \
"    mov  x0, x10\n"                                                                       \
"    mov  x15, x8\n"                                                                       \
"2:  ldr  x1, [x0]\n"                                                                      \
"    add  x0, x0, #4096\n"                                                                 \
"    subs x15, x15, #1\n"                                                                  \
"    b.ne 2b\n"                                                                            \
"    isb\n"                                                                                \
"    mrs  x17, cntvct_el0\n"                                                               \
"    sub  x17, x17, x16\n"                                                                 \
"    str  x17, [x11], #8\n"                                                                \
"    subs x14, x14, #1\n"                                                                  \
"    b.ne Lma_round\n"                                                                      \
"    str  x19, [x28, #0xc00]\n"                                                            \
"    adr  x7, Lb_touchonly\n"                                                               \
"    bl   Ltimeit\n"                                                                        \
"    adr  x7, Lb_at_s1e1r\n"                                                                \
"    bl   Ltimeit\n"                                                                        \
"    adr  x7, Lb_at_s1e1w\n"                                                                \
"    bl   Ltimeit\n"                                                                        \
"    adr  x7, Lb_at_s1e1rp\n"                                                               \
"    bl   Ltimeit\n"                                                                        \
"    adr  x7, Lb_at_s1e1wp\n"                                                               \
"    bl   Ltimeit\n"                                                                        \
"    hvc  #0\n"                                                                            \
"Lb_at_s1e1r:\n"                                                                            \
"    mov  x0, x10\n"                                                                       \
"    mov  x15, x8\n"                                                                       \
"1:  sys  #0, c7, c8, #0, x0\n"                                                            \
"    isb\n"                                                                                \
"    mrs  x1, par_el1\n"                                                                   \
"    add  x0, x0, #4096\n"                                                                 \
"    subs x15, x15, #1\n"                                                                  \
"    b.ne 1b\n"                                                                            \
"    mov  x6, x1\n"                                                                        \
"    ret\n"                                                                                \
"Lb_at_s1e1w:\n"                                                                            \
"    mov  x0, x10\n"                                                                       \
"    mov  x15, x8\n"                                                                       \
"1:  sys  #0, c7, c8, #1, x0\n"                                                            \
"    isb\n"                                                                                \
"    mrs  x1, par_el1\n"                                                                   \
"    add  x0, x0, #4096\n"                                                                 \
"    subs x15, x15, #1\n"                                                                  \
"    b.ne 1b\n"                                                                            \
"    mov  x6, x1\n"                                                                        \
"    ret\n"                                                                                \
"Lb_at_s1e1rp:\n"                                                                           \
"    mov  x0, x10\n"                                                                       \
"    mov  x15, x8\n"                                                                       \
"1:  sys  #0, c7, c9, #0, x0\n"                                                            \
"    isb\n"                                                                                \
"    mrs  x1, par_el1\n"                                                                   \
"    add  x0, x0, #4096\n"                                                                 \
"    subs x15, x15, #1\n"                                                                  \
"    b.ne 1b\n"                                                                            \
"    mov  x6, x1\n"                                                                        \
"    ret\n"                                                                                \
"Lb_at_s1e1wp:\n"                                                                           \
"    mov  x0, x10\n"                                                                       \
"    mov  x15, x8\n"                                                                       \
"1:  sys  #0, c7, c9, #1, x0\n"                                                            \
"    isb\n"                                                                                \
"    mrs  x1, par_el1\n"                                                                   \
"    add  x0, x0, #4096\n"                                                                 \
"    subs x15, x15, #1\n"                                                                  \
"    b.ne 1b\n"                                                                            \
"    mov  x6, x1\n"                                                                        \
"    ret\n"                                                                                \
                                                                                           \
/* ---------------- mode 7: EnTSO cost ---------------- */                                 \
/* P0 buf VA, P1 bytes(pow2), P2 rounds, P3 passes, P4 chain head, P5 chase steps */        \
"Lm_tso:\n"                                                                                 \
"    ldr  x10, [x28, #0x000]\n"                                                            \
"    ldr  x12, [x28, #0x008]\n"                                                            \
"    ldr  x9,  [x28, #0x010]\n"                                                            \
"    ldr  x8,  [x28, #0x018]\n"                                                            \
"    add  x11, x28, #0x200\n"                                                              \
"    adr  x7, Lb_store_stream\n"                                                            \
"    bl   Ltimeit\n"                                                                        \
"    adr  x7, Lb_load_stream\n"                                                             \
"    bl   Ltimeit\n"                                                                        \
"    adr  x7, Lb_rand_store\n"                                                              \
"    bl   Ltimeit\n"                                                                        \
"    ldr  x10, [x28, #0x020]\n"                                                            \
"    ldr  x8,  [x28, #0x028]\n"                                                            \
"    mov  x0, x10\n"                                                                       \
"    mov  x15, x8\n"                                                                       \
"1:  ldr  x0, [x0]\n"                                                                      \
"    subs x15, x15, #1\n"                                                                  \
"    b.ne 1b\n"                                                                            \
"    adr  x7, Lb_chase\n"                                                                   \
"    bl   Ltimeit\n"                                                                        \
"    hvc  #0\n"                                                                            \
"Lb_store_stream:\n"                                                                        \
"    mov  x18, x8\n"                                                                       \
"    mov  x1, #0x1111\n"                                                                   \
"    mov  x2, #0x2222\n"                                                                   \
"1:  mov  x0, x10\n"                                                                       \
"    lsr  x15, x12, #6\n"                                                                  \
"2:  stp  x1, x2, [x0, #0]\n"                                                              \
"    stp  x1, x2, [x0, #16]\n"                                                             \
"    stp  x1, x2, [x0, #32]\n"                                                             \
"    stp  x1, x2, [x0, #48]\n"                                                             \
"    add  x0, x0, #64\n"                                                                   \
"    subs x15, x15, #1\n"                                                                  \
"    b.ne 2b\n"                                                                            \
"    subs x18, x18, #1\n"                                                                  \
"    b.ne 1b\n"                                                                            \
"    ret\n"                                                                                \
"Lb_load_stream:\n"                                                                         \
"    mov  x18, x8\n"                                                                       \
"    mov  x5, #0\n"                                                                        \
"1:  mov  x0, x10\n"                                                                       \
"    lsr  x15, x12, #6\n"                                                                  \
"2:  ldp  x1, x2, [x0, #0]\n"                                                              \
"    ldp  x3, x4, [x0, #16]\n"                                                             \
"    add  x5, x5, x1\n"                                                                    \
"    add  x5, x5, x3\n"                                                                    \
"    ldp  x1, x2, [x0, #32]\n"                                                             \
"    ldp  x3, x4, [x0, #48]\n"                                                             \
"    add  x5, x5, x1\n"                                                                    \
"    add  x5, x5, x3\n"                                                                    \
"    add  x0, x0, #64\n"                                                                   \
"    subs x15, x15, #1\n"                                                                  \
"    b.ne 2b\n"                                                                            \
"    subs x18, x18, #1\n"                                                                  \
"    b.ne 1b\n"                                                                            \
"    mov  x6, x5\n"                                                                        \
"    ret\n"                                                                                \
"Lb_rand_store:\n"                                                                          \
"    movz x2, #0x79b1\n"                                                                   \
"    movk x2, #0x9e37, lsl #16\n"                                                          \
"    lsr  x4, x12, #7\n"                                                                   \
"    sub  x4, x4, #1\n"                                                                    \
"    mov  x18, x8\n"                                                                       \
"    mov  x3, #0\n"                                                                        \
"1:  lsr  x15, x12, #7\n"                                                                  \
"2:  add  x3, x3, #1\n"                                                                    \
"    mul  x1, x3, x2\n"                                                                    \
"    lsr  x1, x1, #7\n"                                                                    \
"    and  x1, x1, x4\n"                                                                    \
"    add  x1, x10, x1, lsl #7\n"                                                           \
"    str  x2, [x1]\n"                                                                      \
"    subs x15, x15, #1\n"                                                                  \
"    b.ne 2b\n"                                                                            \
"    subs x18, x18, #1\n"                                                                  \
"    b.ne 1b\n"                                                                            \
"    ret\n"                                                                                \
                                                                                           \
/* ---------------- mode 8: contention spinner ---------------- */                         \
"Lm_spin:\n"                                                                                \
"    ldr  x10, [x28, #0x000]\n"                                                            \
"    ldr  x12, [x28, #0x008]\n"                                                            \
"    ldr  x13, [x28, #0x010]\n"                                                            \
"    ldr  x8,  [x28, #0x018]\n"                                                            \
"1:  ldr  x0, [x13]\n"                                                                     \
"    cbnz x0, 9f\n"                                                                        \
"    mov  x1, x10\n"                                                                       \
"    lsr  x15, x12, #7\n"                                                                  \
"2:  ldr  x2, [x1]\n"                                                                      \
"    str  x2, [x1]\n"                                                                      \
"    add  x1, x1, #128\n"                                                                  \
"    subs x15, x15, #1\n"                                                                  \
"    b.ne 2b\n"                                                                            \
"    subs x8, x8, #1\n"                                                                    \
"    b.ne 1b\n"                                                                            \
"9:  hvc  #0\n"                                                                            \
                                                                                           \
/* ---------------- exception handlers ---------------- */                                 \
".globl _h_skip\n"                                                                         \
"_h_skip:\n"                                                                               \
"    mrs  x25, elr_el1\n"                                                                  \
"    add  x25, x25, #4\n"                                                                  \
"    msr  elr_el1, x25\n"                                                                  \
"    eret\n"                                                                               \
".globl _h_skip_cnt\n"                                                                     \
"_h_skip_cnt:\n"                                                                           \
"    mrs  x25, elr_el1\n"                                                                  \
"    add  x25, x25, #4\n"                                                                  \
"    msr  elr_el1, x25\n"                                                                  \
"    add  x19, x19, #1\n"                                                                  \
"    mov  x27, #1\n"                                                                       \
"    eret\n"                                                                               \
".globl _h_shadow\n"                                                                       \
"_h_shadow:\n"                                                                             \
"    mrs  x25, far_el1\n"                                                                  \
"    ubfx x26, x25, #39, #9\n"                                                             \
"    add  x26, x22, x26, lsl #3\n"                                                         \
"    ldr  x26, [x26]\n"                                                                    \
"    and  x26, x26, x23\n"                                                                 \
"    ubfx x27, x25, #30, #9\n"                                                             \
"    add  x27, x26, x27, lsl #3\n"                                                         \
"    ldr  x26, [x27]\n"                                                                    \
"    and  x26, x26, x23\n"                                                                 \
"    ubfx x27, x25, #21, #9\n"                                                             \
"    add  x27, x26, x27, lsl #3\n"                                                         \
"    ldr  x26, [x27]\n"                                                                    \
"    and  x26, x26, x23\n"                                                                 \
"    ubfx x27, x25, #12, #9\n"                                                             \
"    add  x27, x26, x27, lsl #3\n"                                                         \
"    ldr  x26, [x27]\n"                                                                    \
"    and  x27, x26, x23\n"                                                                 \
"    orr  x27, x27, x24\n"                                                                 \
"    tbnz x26, #1, 1f\n"                                                                   \
"    orr  x27, x27, #0x80\n"                                                               \
"1:  ubfx x26, x25, #12, #9\n"                                                             \
"    add  x26, x21, x26, lsl #3\n"                                                         \
"    str  x27, [x26]\n"                                                                    \
"    dsb  ishst\n"                                                                         \
"    lsr  x26, x25, #12\n"                                                                 \
"    tlbi vaae1is, x26\n"                                                                  \
"    dsb  ish\n"                                                                           \
"    isb\n"                                                                                \
"    add  x19, x19, #1\n"                                                                  \
"    cmp  x19, x20\n"                                                                      \
"    b.hi Lh_panic\n"                                                                      \
"    eret\n"                                                                               \
".globl _h_shadow_notlbi\n"                                                                \
"_h_shadow_notlbi:\n"                                                                     \
"    mrs  x25, far_el1\n"                                                                  \
"    ubfx x26, x25, #39, #9\n"                                                             \
"    add  x26, x22, x26, lsl #3\n"                                                         \
"    ldr  x26, [x26]\n"                                                                    \
"    and  x26, x26, x23\n"                                                                 \
"    ubfx x27, x25, #30, #9\n"                                                             \
"    add  x27, x26, x27, lsl #3\n"                                                         \
"    ldr  x26, [x27]\n"                                                                    \
"    and  x26, x26, x23\n"                                                                 \
"    ubfx x27, x25, #21, #9\n"                                                             \
"    add  x27, x26, x27, lsl #3\n"                                                         \
"    ldr  x26, [x27]\n"                                                                    \
"    and  x26, x26, x23\n"                                                                 \
"    ubfx x27, x25, #12, #9\n"                                                             \
"    add  x27, x26, x27, lsl #3\n"                                                         \
"    ldr  x26, [x27]\n"                                                                    \
"    and  x27, x26, x23\n"                                                                 \
"    orr  x27, x27, x24\n"                                                                 \
"    tbnz x26, #1, 1f\n"                                                                   \
"    orr  x27, x27, #0x80\n"                                                               \
"1:  ubfx x26, x25, #12, #9\n"                                                             \
"    add  x26, x21, x26, lsl #3\n"                                                         \
"    str  x27, [x26]\n"                                                                    \
"    dsb  ishst\n"                                                                         \
"    isb\n"                                                                                \
"    add  x19, x19, #1\n"                                                                  \
"    cmp  x19, x20\n"                                                                      \
"    b.hi Lh_panic\n"                                                                      \
"    eret\n"                                                                               \
".globl _h_af\n"                                                                           \
"_h_af:\n"                                                                                 \
"    mrs  x25, far_el1\n"                                                                  \
"    ubfx x26, x25, #12, #9\n"                                                             \
"    add  x26, x21, x26, lsl #3\n"                                                         \
"    ldr  x27, [x26]\n"                                                                    \
"    orr  x27, x27, #0x400\n"                                                              \
"    str  x27, [x26]\n"                                                                    \
"    dsb  ishst\n"                                                                         \
"    lsr  x27, x25, #12\n"                                                                 \
"    tlbi vaae1is, x27\n"                                                                  \
"    dsb  ish\n"                                                                           \
"    isb\n"                                                                                \
"    add  x19, x19, #1\n"                                                                  \
"    cmp  x19, x20\n"                                                                      \
"    b.hi Lh_panic\n"                                                                      \
"    eret\n"                                                                               \
".globl _h_af_notlbi\n"                                                                    \
"_h_af_notlbi:\n"                                                                          \
"    mrs  x25, far_el1\n"                                                                  \
"    ubfx x26, x25, #12, #9\n"                                                             \
"    add  x26, x21, x26, lsl #3\n"                                                         \
"    ldr  x27, [x26]\n"                                                                    \
"    orr  x27, x27, #0x400\n"                                                              \
"    str  x27, [x26]\n"                                                                    \
"    dsb  ishst\n"                                                                         \
"    isb\n"                                                                                \
"    add  x19, x19, #1\n"                                                                  \
"    cmp  x19, x20\n"                                                                      \
"    b.hi Lh_panic\n"                                                                      \
"    eret\n"                                                                               \
"Lh_panic:\n"                                                                    \
".globl _h_panic\n"                                                                        \
"_h_panic:\n"                                                                              \
"    mrs  x25, esr_el1\n"                                                                  \
"    mrs  x26, far_el1\n"                                                                  \
"    mrs  x27, elr_el1\n"                                                                  \
"    hvc  #9\n"                                                                            \
".globl _sm_end\n"                                                                         \
"_sm_end:\n"
