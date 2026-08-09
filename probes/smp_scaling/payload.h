// Guest EL1 payload for the SMP-scaling probe.
//
// Everything here runs INSIDE the VM at EL1 under our own VBAR_EL1. No host exit occurs
// between the two CNTVCT_EL0 reads that bracket any measured window.
//
// Register discipline
//   x28            param/result block VA (identity mapped)
//   x19            fault counter          x20  fault limit (handler panics past it, HVC #9)
//   x21            shadow L3 table VA     x22  fake x86 CR3
//   x23            output-address mask    x24  base ARM stage-1 attrs for a filled descriptor
//   x25,x26,x27    handler scratch -- never used by measurement bodies
//   x11            result cursor          x16,x17  timestamps
//
// Param block layout (VA in x28):
//   0x000..0x17f   mode parameters p0..p46
//   0x180          barrier slot VA (0 = no barrier)      0x188  barrier arrival target N
//   0x1c0          fault counter init                    0x1c8  fault limit
//   0x1d0          shadow L3 VA                          0x1d8  x86 CR3
//   0x1e0          OA mask                               0x1e8  descriptor attrs
//   0x1f0          result-array VA
//   0xc00..0xcf8   aux outputs (see host)

#define PAYLOAD_ASM                                                                        \
".section __TEXT,__text\n"                                                                 \
".p2align 2\n"                                                                             \
".globl _sp_start\n"                                                                       \
"_sp_start:\n"                                                                             \
/* x0=MAIR x1=TCR x2=TTBR0 x3=VBAR x4=param VA x5=mode x6=EnTSO */                          \
"    msr  mair_el1, x0\n"                                                                  \
"    msr  tcr_el1,  x1\n"                                                                  \
"    msr  ttbr0_el1, x2\n"                                                                 \
"    msr  vbar_el1, x3\n"                                                                  \
"    cbz  x6, 1f\n"                                                                        \
"    mrs  x9, actlr_el1\n"                                                                 \
"    orr  x9, x9, #2\n"                          /* ACTLR_EL1.EnTSO */                     \
"    msr  actlr_el1, x9\n"                                                                 \
"1:  isb\n"                                                                                \
"    tlbi vmalle1\n"                                                                       \
"    dsb  nsh\n"                                                                           \
"    isb\n"                                                                                \
"    mrs  x9, sctlr_el1\n"                                                                 \
"    mov  x10, #0x1005\n"                        /* M | C | I */                           \
"    orr  x9, x9, x10\n"                                                                   \
"    msr  sctlr_el1, x9\n"                                                                 \
"    isb\n"                                                                                \
"    ic   iallu\n"                               /* host rewrites vectors between runs */  \
"    dsb  nsh\n"                                                                           \
"    isb\n"                                                                                \
"    mov  x28, x4\n"                                                                       \
"    ldr  x19, [x28, #0x1c0]\n"                                                            \
"    ldr  x20, [x28, #0x1c8]\n"                                                            \
"    ldr  x21, [x28, #0x1d0]\n"                                                            \
"    ldr  x22, [x28, #0x1d8]\n"                                                            \
"    ldr  x23, [x28, #0x1e0]\n"                                                            \
"    ldr  x24, [x28, #0x1e8]\n"                                                            \
"    ldr  x11, [x28, #0x1f0]\n"                                                            \
"    mrs  x0, cntfrq_el0\n"                                                                \
"    str  x0, [x28, #0xc10]\n"                                                             \
"    mrs  x0, actlr_el1\n"                                                                 \
"    str  x0, [x28, #0xc08]\n"                                                             \
"    mrs  x0, mpidr_el1\n"                                                                 \
"    str  x0, [x28, #0xc28]\n"                                                             \
"    cmp  x5, #0\n"                                                                        \
"    b.eq Lm_cal\n"                                                                        \
"    cmp  x5, #1\n"                                                                        \
"    b.eq Lm_shadow\n"                                                                     \
"    cmp  x5, #2\n"                                                                        \
"    b.eq Lm_tlbi\n"                                                                       \
"    cmp  x5, #3\n"                                                                        \
"    b.eq Lm_contend\n"                                                                    \
"    cmp  x5, #4\n"                                                                        \
"    b.eq Lm_litmus\n"                                                                     \
"    cmp  x5, #5\n"                                                                        \
"    b.eq Lm_store\n"                                                                      \
"    cmp  x5, #6\n"                                                                        \
"    b.eq Lm_pingpong\n"                                                                   \
"    cmp  x5, #7\n"                                                                        \
"    b.eq Lm_compute\n"                                                                    \
"    cmp  x5, #8\n"                                                                        \
"    b.eq Lm_idle\n"                                                                       \
"    hvc  #9\n"                                                                            \
                                                                                           \
/* ---------------- shared helpers ---------------- */                                     \
/* Bounded arrival barrier. Clobbers x0-x4. HVC #10 if it does not complete. */             \
"Lbar:\n"                                                                                  \
"    ldr  x0, [x28, #0x180]\n"                                                             \
"    cbz  x0, 9f\n"                                                                        \
"    ldr  x1, [x28, #0x188]\n"                                                             \
"    mov  w2, #1\n"                                                                        \
"    ldaddal w2, w3, [x0]\n"                                                               \
"    mov  x4, #0x20000000\n"                                                               \
"1:  ldar w3, [x0]\n"                                                                      \
"    cmp  x3, x1\n"                                                                        \
"    b.hs 9f\n"                                                                            \
"    subs x4, x4, #1\n"                                                                    \
"    b.ne 1b\n"                                                                            \
"    hvc  #10\n"                                                                           \
"9:  ret\n"                                                                                \
/* Timed-body harness: x7 body, x8 count, x9 rounds, x10 base, x11 cursor, x12 extra. */    \
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
"Lwin_open:\n"                                                                             \
"    isb\n"                                                                                \
"    mrs  x0, cntvct_el0\n"                                                                \
"    str  x0, [x28, #0xc18]\n"                                                             \
"    ret\n"                                                                                \
"Lwin_close:\n"                                                                            \
"    isb\n"                                                                                \
"    mrs  x0, cntvct_el0\n"                                                                \
"    str  x0, [x28, #0xc20]\n"                                                             \
"    str  x19, [x28, #0xc00]\n"                                                            \
"    ret\n"                                                                                \
                                                                                           \
/* ---------------- mode 0: calibration + clock estimate ---------------- */                \
/* p0 = iters */                                                                            \
"Lm_cal:\n"                                                                                 \
"    bl   Lbar\n"                                                                          \
"    bl   Lwin_open\n"                                                                     \
"    ldr  x1, [x28, #0x000]\n"                                                             \
/* (a) back-to-back {isb; mrs cntvct} */                                                   \
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
"    str  x5, [x11], #8\n"                                                                 \
/* (b) empty loop */                                                                       \
"    isb\n"                                                                                \
"    mrs  x2, cntvct_el0\n"                                                                \
"    mov  x3, x1\n"                                                                        \
"2:  subs x3, x3, #1\n"                                                                    \
"    b.ne 2b\n"                                                                            \
"    isb\n"                                                                                \
"    mrs  x5, cntvct_el0\n"                                                                \
"    sub  x5, x5, x2\n"                                                                    \
"    str  x5, [x11], #8\n"                                                                 \
/* (c) 16 dependent ADDs per iteration -> clock estimate */                                 \
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
"    str  x5, [x11], #8\n"                                                                 \
"    str  x6, [x28, #0xc38]\n"                                                             \
"    bl   Lwin_close\n"                                                                    \
"    hvc  #0\n"                                                                            \
                                                                                           \
/* ---------------- mode 1: concurrent shadow fill ---------------- */                      \
/* p0 pages, p1 rounds, p2 base VA, p3 group, p4 control rounds, p5 control result VA */    \
"Lm_shadow:\n"                                                                              \
"    ldr  x8,  [x28, #0x000]\n"                                                            \
"    ldr  x9,  [x28, #0x008]\n"                                                            \
"    ldr  x10, [x28, #0x010]\n"                                                            \
"    ldr  x12, [x28, #0x018]\n"                                                            \
/* warm-up round: fill everything once so caches/x86 tables are hot, then reset */          \
"    mov  x0, x10\n"                                                                       \
"    mov  x15, x8\n"                                                                       \
"1:  ldr  x1, [x0]\n"                                                                      \
"    add  x0, x0, #4096\n"                                                                 \
"    subs x15, x15, #1\n"                                                                  \
"    b.ne 1b\n"                                                                            \
"    mov  x19, #0\n"                                                                       \
"    bl   Lbar\n"                                                                          \
"    ldr  x8,  [x28, #0x000]\n"                                                            \
"    ldr  x9,  [x28, #0x008]\n"                                                            \
"    ldr  x10, [x28, #0x010]\n"                                                            \
"    ldr  x12, [x28, #0x018]\n"                                                            \
"    bl   Lwin_open\n"                                                                     \
"    mov  x14, x9\n"                                                                       \
"Lms_round:\n"                                                                              \
/* invalidate this vCPU's own shadow L3 entries; local TLBI only, so other vCPUs are not */ \
/* disturbed by our per-round reset.                                                     */ \
"    mov  x15, #0\n"                                                                       \
"1:  str  xzr, [x21, x15, lsl #3]\n"                                                       \
"    add  x15, x15, #1\n"                                                                  \
"    cmp  x15, x8\n"                                                                       \
"    b.lo 1b\n"                                                                            \
"    dsb  ishst\n"                                                                         \
"    tlbi vmalle1\n"                                                                       \
"    dsb  nsh\n"                                                                           \
"    isb\n"                                                                                \
"    mov  x0, x10\n"                                                                       \
"    mov  x15, x8\n"                                                                       \
"Lms_grp:\n"                                                                                \
"    isb\n"                                                                                \
"    mrs  x16, cntvct_el0\n"                                                               \
"    mov  x13, x12\n"                                                                      \
"2:  ldr  x1, [x0]\n"                                                                      \
"    add  x0, x0, #4096\n"                                                                 \
"    subs x13, x13, #1\n"                                                                  \
"    b.ne 2b\n"                                                                            \
"    isb\n"                                                                                \
"    mrs  x17, cntvct_el0\n"                                                               \
"    sub  x17, x17, x16\n"                                                                 \
"    str  x17, [x11], #8\n"                                                                \
"    subs x15, x15, x12\n"                                                                 \
"    b.ne Lms_grp\n"                                                                       \
"    subs x14, x14, #1\n"                                                                  \
"    b.ne Lms_round\n"                                                                     \
"    bl   Lwin_close\n"                                                                    \
/* control arm: identical loop, identical VA stream, no reset -> no faults */               \
"    ldr  x11, [x28, #0x028]\n"                                                            \
"    ldr  x14, [x28, #0x020]\n"                                                            \
"Lmsc_round:\n"                                                                             \
"    mov  x0, x10\n"                                                                       \
"    mov  x15, x8\n"                                                                       \
"Lmsc_grp:\n"                                                                               \
"    isb\n"                                                                                \
"    mrs  x16, cntvct_el0\n"                                                               \
"    mov  x13, x12\n"                                                                      \
"2:  ldr  x1, [x0]\n"                                                                      \
"    add  x0, x0, #4096\n"                                                                 \
"    subs x13, x13, #1\n"                                                                  \
"    b.ne 2b\n"                                                                            \
"    isb\n"                                                                                \
"    mrs  x17, cntvct_el0\n"                                                               \
"    sub  x17, x17, x16\n"                                                                 \
"    str  x17, [x11], #8\n"                                                                \
"    subs x15, x15, x12\n"                                                                 \
"    b.ne Lmsc_grp\n"                                                                      \
"    subs x14, x14, #1\n"                                                                  \
"    b.ne Lmsc_round\n"                                                                    \
"    str  x19, [x28, #0xc00]\n"                                                            \
"    hvc  #0\n"                                                                            \
                                                                                           \
/* ---------------- mode 2: TLB maintenance (the measuring vCPU) ---------------- */        \
/* p0 iters, p1 rounds, p2 target VA, p4 stop-flag VA */                                    \
"Lm_tlbi:\n"                                                                                \
"    bl   Lbar\n"                                                                          \
"    ldr  x8,  [x28, #0x000]\n"                                                            \
"    ldr  x9,  [x28, #0x008]\n"                                                            \
"    ldr  x10, [x28, #0x010]\n"                                                            \
"    lsr  x10, x10, #12\n"                                                                 \
"    bl   Lwin_open\n"                                                                     \
"    adr  x7, Lb_nop\n"                                                                    \
"    bl   Ltimeit\n"                                                                       \
"    adr  x7, Lb_isb\n"                                                                    \
"    bl   Ltimeit\n"                                                                       \
"    adr  x7, Lb_dsb\n"                                                                    \
"    bl   Ltimeit\n"                                                                       \
"    adr  x7, Lb_vae1\n"                                                                   \
"    bl   Ltimeit\n"                                                                       \
"    adr  x7, Lb_vae1is\n"                                                                 \
"    bl   Ltimeit\n"                                                                       \
"    adr  x7, Lb_vaae1is\n"                                                                \
"    bl   Ltimeit\n"                                                                       \
"    adr  x7, Lb_vmalle1\n"                                                                \
"    bl   Ltimeit\n"                                                                       \
"    adr  x7, Lb_vmalle1is\n"                                                              \
"    bl   Ltimeit\n"                                                                       \
"    bl   Lwin_close\n"                                                                    \
"    ldr  x0, [x28, #0x020]\n"                                                             \
"    cbz  x0, 1f\n"                                                                        \
"    mov  x1, #1\n"                                                                        \
"    stlr x1, [x0]\n"                                                                      \
"1:  hvc  #0\n"                                                                            \
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
"Lb_dsb:\n"                                                                                 \
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
/* ---------------- mode 3: contender ---------------- */                                   \
/* p0 buffer VA, p1 bytes, p2 stop-flag VA, p3 kind (0 idle / 1 page-walk), p4 outer bound */\
"Lm_contend:\n"                                                                             \
"    bl   Lbar\n"                                                                          \
"    ldr  x10, [x28, #0x000]\n"                                                            \
"    ldr  x12, [x28, #0x008]\n"                                                            \
"    ldr  x13, [x28, #0x010]\n"                                                            \
"    ldr  x0,  [x28, #0x018]\n"                                                            \
"    ldr  x8,  [x28, #0x020]\n"                                                            \
"    mov  x18, #0\n"                                                                       \
"    cbz  x0, Lct_idle\n"                                                                  \
"Lct_mem:\n"                                                                                \
"    ldar x1, [x13]\n"                                                                     \
"    cbnz x1, 9f\n"                                                                        \
"    mov  x1, x10\n"                                                                       \
"    lsr  x15, x12, #12\n"                                                                 \
"2:  ldr  x2, [x1]\n"                                                                      \
"    add  x18, x18, x2\n"                                                                  \
"    add  x1, x1, #4096\n"                                                                 \
"    subs x15, x15, #1\n"                                                                  \
"    b.ne 2b\n"                                                                            \
"    subs x8, x8, #1\n"                                                                    \
"    b.ne Lct_mem\n"                                                                       \
"    b    9f\n"                                                                            \
"Lct_idle:\n"                                                                               \
"    ldar x1, [x13]\n"                                                                     \
"    cbnz x1, 9f\n"                                                                        \
"    mov  x15, #4096\n"                                                                    \
"3:  add  x18, x18, #1\n"                                                                  \
"    subs x15, x15, #1\n"                                                                  \
"    b.ne 3b\n"                                                                            \
"    subs x8, x8, #1\n"                                                                    \
"    b.ne Lct_idle\n"                                                                      \
"9:  str  x18, [x28, #0xc40]\n"                                                            \
"    hvc  #0\n"                                                                            \
                                                                                           \
/* ---------------- mode 4: MP litmus at scale ---------------- */                          \
/* p0 role (0 writer / 1 reader), p1 shared line-pair VA, p2 iters */                       \
"Lm_litmus:\n"                                                                              \
"    ldr  x6,  [x28, #0x000]\n"                                                            \
"    ldr  x4,  [x28, #0x008]\n"                                                            \
"    ldr  x5,  [x28, #0x010]\n"                                                            \
"    bl   Lbar\n"                                                                          \
"    ldr  x6,  [x28, #0x000]\n"                                                            \
"    ldr  x4,  [x28, #0x008]\n"                                                            \
"    ldr  x5,  [x28, #0x010]\n"                                                            \
"    bl   Lwin_open\n"                                                                     \
"    mov  x2, #0\n"                                                                        \
"    mov  x16, #0\n"                                                                       \
"    mov  x17, #-1\n"                                                                      \
"    mov  x18, #0\n"                                                                       \
"    cbnz x6, Llit_read\n"                                                                 \
"Llit_write:\n"                                                                             \
"    add  x2, x2, #1\n"                                                                    \
"    str  x2, [x4, #0x000]\n"                                                              \
"    str  x2, [x4, #0x080]\n"                                                              \
"    cmp  x2, x5\n"                                                                        \
"    b.lo Llit_write\n"                                                                    \
"    b    Llit_done\n"                                                                     \
"Llit_read:\n"                                                                              \
"    ldr  x13, [x4, #0x080]\n"                                                             \
"    ldr  x14, [x4, #0x000]\n"                                                             \
"    cmp  x13, x14\n"                                                                      \
"    b.ls 21f\n"                                                                           \
"    add  x16, x16, #1\n"                                                                  \
"21: cmp  x13, x17\n"                                                                      \
"    b.eq 22f\n"                                                                           \
"    add  x18, x18, #1\n"                                                                  \
"    mov  x17, x13\n"                                                                      \
"22: add  x2, x2, #1\n"                                                                    \
"    cmp  x2, x5\n"                                                                        \
"    b.lo Llit_read\n"                                                                     \
"Llit_done:\n"                                                                              \
"    str  x16, [x28, #0xc48]\n"                                                            \
"    str  x18, [x28, #0xc50]\n"                                                            \
"    bl   Lwin_close\n"                                                                    \
"    hvc  #0\n"                                                                            \
                                                                                           \
/* ---------------- mode 5: store throughput (EnTSO cost under contention) ---------- */    \
/* p0 buffer VA, p1 bytes, p2 rounds, p3 passes, p4 kind (0 stp store / 1 ldp load) */      \
"Lm_store:\n"                                                                               \
"    ldr  x10, [x28, #0x000]\n"                                                            \
"    ldr  x12, [x28, #0x008]\n"                                                            \
"    ldr  x9,  [x28, #0x010]\n"                                                            \
"    ldr  x6,  [x28, #0x018]\n"                                                            \
"    ldr  x5,  [x28, #0x020]\n"                                                            \
/* warm the buffer */                                                                      \
"    mov  x1, x10\n"                                                                       \
"    lsr  x15, x12, #6\n"                                                                  \
"1:  ldr  x2, [x1]\n"                                                                      \
"    add  x1, x1, #64\n"                                                                   \
"    subs x15, x15, #1\n"                                                                  \
"    b.ne 1b\n"                                                                            \
"    bl   Lbar\n"                                                                          \
"    ldr  x10, [x28, #0x000]\n"                                                            \
"    ldr  x12, [x28, #0x008]\n"                                                            \
"    ldr  x9,  [x28, #0x010]\n"                                                            \
"    ldr  x6,  [x28, #0x018]\n"                                                            \
"    ldr  x5,  [x28, #0x020]\n"                                                            \
"    bl   Lwin_open\n"                                                                     \
"    mov  x14, x9\n"                                                                       \
"Lst_round:\n"                                                                              \
"    isb\n"                                                                                \
"    mrs  x16, cntvct_el0\n"                                                               \
"    mov  x8, x6\n"                                                                        \
"Lst_pass:\n"                                                                               \
"    mov  x1, x10\n"                                                                       \
"    lsr  x15, x12, #4\n"                                                                  \
"    cbnz x5, Lst_load\n"                                                                  \
"2:  stp  x2, x3, [x1], #16\n"                                                             \
"    subs x15, x15, #1\n"                                                                  \
"    b.ne 2b\n"                                                                            \
"    b    Lst_next\n"                                                                      \
"Lst_load:\n"                                                                               \
"3:  ldp  x2, x3, [x1], #16\n"                                                             \
"    subs x15, x15, #1\n"                                                                  \
"    b.ne 3b\n"                                                                            \
"Lst_next:\n"                                                                               \
"    subs x8, x8, #1\n"                                                                    \
"    b.ne Lst_pass\n"                                                                      \
"    isb\n"                                                                                \
"    mrs  x17, cntvct_el0\n"                                                               \
"    sub  x17, x17, x16\n"                                                                 \
"    str  x17, [x11], #8\n"                                                                \
"    subs x14, x14, #1\n"                                                                  \
"    b.ne Lst_round\n"                                                                     \
"    bl   Lwin_close\n"                                                                    \
"    hvc  #0\n"                                                                            \
                                                                                           \
/* ---------------- mode 6: in-VM shared-memory ping-pong ---------------- */                \
/* p0 role (0 ping / 1 pong), p1 flagA VA, p2 flagB VA, p3 samples, p4 round trips/sample */ \
"Lm_pingpong:\n"                                                                             \
"    bl   Lbar\n"                                                                          \
"    ldr  x6,  [x28, #0x000]\n"                                                            \
"    ldr  x10, [x28, #0x008]\n"                                                            \
"    ldr  x12, [x28, #0x010]\n"                                                            \
"    ldr  x9,  [x28, #0x018]\n"                                                            \
"    ldr  x5,  [x28, #0x020]\n"                                                            \
"    bl   Lwin_open\n"                                                                     \
"    mov  x2, #0\n"                                                                        \
"    mov  x14, x9\n"                                                                       \
"    cbnz x6, Lpp_pong\n"                                                                  \
"Lpp_ping:\n"                                                                               \
"    isb\n"                                                                                \
"    mrs  x16, cntvct_el0\n"                                                               \
"    mov  x8, x5\n"                                                                        \
"3:  add  x2, x2, #1\n"                                                                    \
"    stlr x2, [x10]\n"                                                                     \
"    mov  x4, #0x10000000\n"                                                               \
"1:  ldar x3, [x12]\n"                                                                     \
"    cmp  x3, x2\n"                                                                        \
"    b.eq 2f\n"                                                                            \
"    subs x4, x4, #1\n"                                                                    \
"    b.ne 1b\n"                                                                            \
"    hvc  #10\n"                                                                           \
"2:  subs x8, x8, #1\n"                                                                    \
"    b.ne 3b\n"                                                                            \
"    isb\n"                                                                                \
"    mrs  x17, cntvct_el0\n"                                                               \
"    sub  x17, x17, x16\n"                                                                 \
"    str  x17, [x11], #8\n"                                                                \
"    subs x14, x14, #1\n"                                                                  \
"    b.ne Lpp_ping\n"                                                                      \
"    b    Lpp_done\n"                                                                      \
"Lpp_pong:\n"                                                                               \
"    mul  x14, x9, x5\n"                                                                   \
"Lpp_pong1:\n"                                                                              \
"    add  x2, x2, #1\n"                                                                    \
"    mov  x4, #0x10000000\n"                                                               \
"1:  ldar x3, [x10]\n"                                                                     \
"    cmp  x3, x2\n"                                                                        \
"    b.eq 2f\n"                                                                            \
"    subs x4, x4, #1\n"                                                                    \
"    b.ne 1b\n"                                                                            \
"    hvc  #10\n"                                                                           \
"2:  stlr x2, [x12]\n"                                                                     \
"    subs x14, x14, #1\n"                                                                  \
"    b.ne Lpp_pong1\n"                                                                     \
"Lpp_done:\n"                                                                               \
"    bl   Lwin_close\n"                                                                    \
"    hvc  #0\n"                                                                            \
                                                                                           \
/* ---------------- mode 7: fixed compute (P/E core discrimination, throughput) ------ */   \
/* p0 iters per round, p1 rounds */                                                        \
"Lm_compute:\n"                                                                             \
"    ldr  x8, [x28, #0x000]\n"                                                             \
"    ldr  x9, [x28, #0x008]\n"                                                             \
"    bl   Lbar\n"                                                                          \
"    ldr  x8, [x28, #0x000]\n"                                                             \
"    ldr  x9, [x28, #0x008]\n"                                                             \
"    bl   Lwin_open\n"                                                                     \
"    mov  x14, x9\n"                                                                       \
"Lcp_round:\n"                                                                              \
"    isb\n"                                                                                \
"    mrs  x16, cntvct_el0\n"                                                               \
"    mov  x3, x8\n"                                                                        \
"    mov  x6, #0\n"                                                                        \
"1:  add  x6, x6, #1\n"                                                                    \
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
"    b.ne 1b\n"                                                                            \
"    isb\n"                                                                                \
"    mrs  x17, cntvct_el0\n"                                                               \
"    sub  x17, x17, x16\n"                                                                 \
"    str  x17, [x11], #8\n"                                                                \
"    subs x14, x14, #1\n"                                                                  \
"    b.ne Lcp_round\n"                                                                     \
"    str  x6, [x28, #0xc38]\n"                                                             \
"    bl   Lwin_close\n"                                                                    \
"    hvc  #0\n"                                                                            \
                                                                                           \
/* ---------------- mode 8: idle spin, cancelled by the host ---------------- */             \
/* p0 stop-flag VA, p1 outer bound */                                                       \
"Lm_idle:\n"                                                                                \
"    ldr  x13, [x28, #0x000]\n"                                                            \
"    ldr  x8,  [x28, #0x008]\n"                                                            \
"    mov  x18, #0\n"                                                                       \
"1:  ldar x1, [x13]\n"                                                                     \
"    cbnz x1, 9f\n"                                                                        \
"    mov  x15, #1024\n"                                                                    \
"2:  add  x18, x18, #1\n"                                                                  \
"    subs x15, x15, #1\n"                                                                  \
"    b.ne 2b\n"                                                                            \
"    subs x8, x8, #1\n"                                                                    \
"    b.ne 1b\n"                                                                            \
"9:  str  x18, [x28, #0xc40]\n"                                                            \
"    hvc  #0\n"                                                                            \
                                                                                           \
/* ---------------- exception handlers ---------------- */                                  \
/* IRQ vector: stamp the guest counter the instant EL1 takes the interrupt, then exit. */   \
".globl _h_irq\n"                                                                          \
"_h_irq:\n"                                                                                \
"    mrs  x25, cntvct_el0\n"                                                               \
"    str  x25, [x28, #0xc58]\n"                                                            \
"    dsb  ish\n"                                                                           \
"    hvc  #3\n"                                                                            \
".globl _h_skip\n"                                                                         \
"_h_skip:\n"                                                                               \
"    mrs  x25, elr_el1\n"                                                                  \
"    add  x25, x25, #4\n"                                                                  \
"    msr  elr_el1, x25\n"                                                                  \
"    eret\n"                                                                               \
/* shadow fill, no TLBI (invalid -> valid never needs one; see doc 16 section 2) */         \
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
"    isb\n"                                                                                \
"    add  x19, x19, #1\n"                                                                  \
"    cmp  x19, x20\n"                                                                      \
"    b.hi Lh_panic\n"                                                                      \
"    eret\n"                                                                               \
/* same, but with the broadcast invalidate the naive implementation would issue */          \
".globl _h_shadow_tlbi\n"                                                                  \
"_h_shadow_tlbi:\n"                                                                        \
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
"Lh_panic:\n"                                                                              \
".globl _h_panic\n"                                                                        \
"_h_panic:\n"                                                                              \
"    mrs  x25, esr_el1\n"                                                                  \
"    mrs  x26, far_el1\n"                                                                  \
"    mrs  x27, elr_el1\n"                                                                  \
"    hvc  #9\n"                                                                            \
".globl _sp_end\n"                                                                         \
"_sp_end:\n"
