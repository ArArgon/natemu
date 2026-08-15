// Guest EL1 payload for the self-modifying-code (SMC) detection probe.
//
// Everything here runs INSIDE the VM at EL1 under our own VBAR_EL1. No host exit occurs
// between the two CNTVCT_EL0 reads that bracket any measured window.
//
// Register discipline
//   x28            param/result block VA (identity mapped)
//   x19            fault counter          x20  fault limit (handler panics past it, HVC #9)
//   x21            L3 table VA of the write-protected region
//   x22            alias delta: (writable alias VA) - (protected VA), same physical pages
//   x23            output-address mask    x24  spare
//   x25,x26,x27    handler scratch -- never used by measurement bodies
//   x11            result cursor          x16,x17  timestamps
//   x7..x14        Ltimeit parameters/state
//   free to bodies: x0-x6, x15, x18
//
// Param block layout (VA in x28):
//   0x000..0x17f   mode parameters p0..p46
//   0x180          barrier slot VA (0 = no barrier)      0x188  barrier arrival target N
//   0x1c0          fault counter init                    0x1c8  fault limit
//   0x1d0          protected-region L3 VA                0x1d8  alias delta
//   0x1e0          OA mask                               0x1e8  spare attrs
//   0x1f0          result-array VA
//   0xc00          faults taken        0xc08 ACTLR   0xc10 CNTFRQ
//   0xc18/0xc20    window open/close timestamps
//   0xc30          last ESR_EL1 seen by h_skip_esr
//   0xc48          "upgrade needed a TLBI" counter      0xc50  last FAR (handler state)

#define SMCE(n) "    str  x" #n ", [x26]\n    b    Lsmc_done\n"
#define SMC_TAB \
    SMCE(0)  SMCE(1)  SMCE(2)  SMCE(3)  SMCE(4)  SMCE(5)  SMCE(6)  SMCE(7)  \
    SMCE(8)  SMCE(9)  SMCE(10) SMCE(11) SMCE(12) SMCE(13) SMCE(14) SMCE(15) \
    SMCE(16) SMCE(17) SMCE(18) SMCE(19) SMCE(20) SMCE(21) SMCE(22) SMCE(23) \
    SMCE(24) SMCE(25) SMCE(26) SMCE(27) SMCE(28) SMCE(29) SMCE(30)          \
    "    str  xzr, [x26]\n    b    Lsmc_done\n"

#define PAYLOAD_ASM                                                                        \
".section __TEXT,__text\n"                                                                 \
".p2align 2\n"                                                                             \
".globl _smc_start\n"                                                                      \
"_smc_start:\n"                                                                            \
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
"    mrs  x0, ctr_el0\n"                                                                   \
"    str  x0, [x28, #0xc28]\n"                                                             \
"    cmp  x5, #0\n"                                                                        \
"    b.eq Lm_cal\n"                                                                        \
"    cmp  x5, #1\n"                                                                        \
"    b.eq Lm_cycle\n"                                                                      \
"    cmp  x5, #2\n"                                                                        \
"    b.eq Lm_ro\n"                                                                         \
"    cmp  x5, #3\n"                                                                        \
"    b.eq Lm_cmp\n"                                                                        \
"    cmp  x5, #4\n"                                                                        \
"    b.eq Lm_icache\n"                                                                     \
"    cmp  x5, #5\n"                                                                        \
"    b.eq Lm_contend\n"                                                                    \
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
"    ldr  x1, [x28, #0x000]\n"                                                             \
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
"    isb\n"                                                                                \
"    mrs  x2, cntvct_el0\n"                                                                \
"    mov  x3, x1\n"                                                                        \
"2:  subs x3, x3, #1\n"                                                                    \
"    b.ne 2b\n"                                                                            \
"    isb\n"                                                                                \
"    mrs  x5, cntvct_el0\n"                                                                \
"    sub  x5, x5, x2\n"                                                                    \
"    str  x5, [x28, #0x208]\n"                                                             \
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
/* ---------------- mode 1: the write-protect / fault / unprotect / write / re-protect  */ \
/* p0 pages, p1 rounds, p2 base VA, p3 group, p4 comp result VA, p5 comp rounds,         */ \
/* p6 reprot (0 = control: page is RW, no fault, no re-protect; 1 = re-protect + TLBI)   */ \
"Lm_cycle:\n"                                                                               \
"    ldr  x8,  [x28, #0x000]\n"                                                            \
"    ldr  x9,  [x28, #0x008]\n"                                                            \
"    ldr  x10, [x28, #0x010]\n"                                                            \
"    ldr  x12, [x28, #0x018]\n"                                                            \
"    ldr  x18, [x28, #0x030]\n"                                                            \
"    bl   Lbar\n"                                                                          \
"    ldr  x8,  [x28, #0x000]\n"                                                            \
"    ldr  x9,  [x28, #0x008]\n"                                                            \
"    ldr  x10, [x28, #0x010]\n"                                                            \
"    ldr  x12, [x28, #0x018]\n"                                                            \
"    ldr  x18, [x28, #0x030]\n"                                                            \
"    bl   Lwin_open\n"                                                                     \
"    mov  x14, x9\n"                                                                       \
"Lcy_round:\n"                                                                              \
"    mov  x0, x10\n"                                                                       \
"    mov  x15, x8\n"                                                                       \
"Lcy_grp:\n"                                                                                \
"    isb\n"                                                                                \
"    mrs  x16, cntvct_el0\n"                                                               \
"    mov  x13, x12\n"                                                                      \
"2:  and  x1, x0, #0x3f000\n"               /* spread the store across 64 cache sets so   */\
"    lsr  x1, x1, #6\n"                     /* the no-fault control does not pay conflict */\
"    add  x1, x0, x1\n"                     /* misses the faulting arm never pays         */\
"    str  x1, [x1]\n"                        /* faults if the page is write-protected */   \
"    cbz  x18, 3f\n"                                                                       \
"    ubfx x2, x0, #12, #9\n"                                                               \
"    add  x2, x21, x2, lsl #3\n"                                                           \
"    ldr  x3, [x2]\n"                                                                      \
"    orr  x3, x3, #0x80\n"                   /* AP[2] = 1 -> read-only again */            \
"    str  x3, [x2]\n"                                                                      \
"    dsb  ishst\n"                                                                         \
"    lsr  x4, x0, #12\n"                                                                   \
"    tlbi vaae1is, x4\n"                                                                   \
"    dsb  ish\n"                                                                           \
"    isb\n"                                                                                \
"3:  add  x0, x0, #4096\n"                                                                 \
"    subs x13, x13, #1\n"                                                                  \
"    b.ne 2b\n"                                                                            \
"    isb\n"                                                                                \
"    mrs  x17, cntvct_el0\n"                                                               \
"    sub  x17, x17, x16\n"                                                                 \
"    str  x17, [x11], #8\n"                                                                \
"    subs x15, x15, x12\n"                                                                 \
"    b.ne Lcy_grp\n"                                                                       \
"    subs x14, x14, #1\n"                                                                  \
"    b.ne Lcy_round\n"                                                                     \
"    bl   Lwin_close\n"                                                                    \
/* leave every page WRITABLE before the component bodies run */                            \
"    mov  x15, #0\n"                                                                       \
"4:  ldr  x2, [x21, x15, lsl #3]\n"                                                        \
"    cbz  x2, 5f\n"                                                                        \
"    bic  x2, x2, #0x80\n"                                                                 \
"    str  x2, [x21, x15, lsl #3]\n"                                                        \
"5:  add  x15, x15, #1\n"                                                                  \
"    cmp  x15, x8\n"                                                                       \
"    b.lo 4b\n"                                                                            \
"    dsb  ishst\n"                                                                         \
"    tlbi vmalle1\n"                          /* local only: do not disturb other vCPUs */ \
"    dsb  nsh\n"                                                                           \
"    isb\n"                                                                                \
"    ldr  x11, [x28, #0x020]\n"                                                            \
"    ldr  x9,  [x28, #0x028]\n"                                                            \
"    adr  x7, Lb_storeonly\n"                                                              \
"    bl   Ltimeit\n"                                                                       \
"    adr  x7, Lb_desconly\n"                                                               \
"    bl   Ltimeit\n"                                                                       \
"    adr  x7, Lb_tlbionly\n"                                                               \
"    bl   Ltimeit\n"                                                                       \
"    adr  x7, Lb_reprot\n"                                                                 \
"    bl   Ltimeit\n"                                                                       \
"    str  x19, [x28, #0xc00]\n"                                                            \
"    hvc  #0\n"                                                                            \
/* bare store over the same VA stream: the floor for mode 1 */                              \
"Lb_storeonly:\n"                                                                           \
"    mov  x0, x10\n"                                                                       \
"    mov  x15, x8\n"                                                                       \
"1:  and  x1, x0, #0x3f000\n"                                                              \
"    lsr  x1, x1, #6\n"                                                                    \
"    add  x1, x0, x1\n"                                                                    \
"    str  x1, [x1]\n"                                                                      \
"    add  x0, x0, #4096\n"                                                                 \
"    subs x15, x15, #1\n"                                                                  \
"    b.ne 1b\n"                                                                            \
"    ret\n"                                                                                \
/* RW->RO->RW descriptor edits, no barriers, no invalidate */                               \
"Lb_desconly:\n"                                                                            \
"    mov  x0, x10\n"                                                                       \
"    mov  x15, x8\n"                                                                       \
"1:  ubfx x1, x0, #12, #9\n"                                                               \
"    add  x1, x21, x1, lsl #3\n"                                                           \
"    ldr  x2, [x1]\n"                                                                      \
"    orr  x2, x2, #0x80\n"                                                                 \
"    str  x2, [x1]\n"                                                                      \
"    ldr  x2, [x1]\n"                                                                      \
"    bic  x2, x2, #0x80\n"                                                                 \
"    str  x2, [x1]\n"                                                                      \
"    add  x0, x0, #4096\n"                                                                 \
"    subs x15, x15, #1\n"                                                                  \
"    b.ne 1b\n"                                                                            \
"    ret\n"                                                                                \
/* one single-page broadcast invalidate + barriers per page */                               \
"Lb_tlbionly:\n"                                                                             \
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
/* the complete permission round trip per page: RW->RO+TLBI, then RO->RW+TLBI */            \
"Lb_reprot:\n"                                                                               \
"    mov  x0, x10\n"                                                                       \
"    mov  x15, x8\n"                                                                       \
"1:  ubfx x1, x0, #12, #9\n"                                                               \
"    add  x1, x21, x1, lsl #3\n"                                                           \
"    lsr  x3, x0, #12\n"                                                                   \
"    ldr  x2, [x1]\n"                                                                      \
"    orr  x2, x2, #0x80\n"                                                                 \
"    str  x2, [x1]\n"                                                                      \
"    dsb  ishst\n"                                                                         \
"    tlbi vaae1is, x3\n"                                                                   \
"    dsb  ish\n"                                                                           \
"    isb\n"                                                                                \
"    ldr  x2, [x1]\n"                                                                      \
"    bic  x2, x2, #0x80\n"                                                                 \
"    str  x2, [x1]\n"                                                                      \
"    dsb  ishst\n"                                                                         \
"    tlbi vaae1is, x3\n"                                                                   \
"    dsb  ish\n"                                                                           \
"    isb\n"                                                                                \
"    add  x0, x0, #4096\n"                                                                 \
"    subs x15, x15, #1\n"                                                                  \
"    b.ne 1b\n"                                                                            \
"    ret\n"                                                                                \
                                                                                           \
/* ---------------- mode 2: permanently read-only page, store emulated in the handler */    \
/* p0 pages, p1 rounds, p2 base VA, p3 group */                                             \
"Lm_ro:\n"                                                                                   \
"    ldr  x8,  [x28, #0x000]\n"                                                            \
"    ldr  x9,  [x28, #0x008]\n"                                                            \
"    ldr  x10, [x28, #0x010]\n"                                                            \
"    ldr  x12, [x28, #0x018]\n"                                                            \
"    bl   Lbar\n"                                                                          \
"    ldr  x8,  [x28, #0x000]\n"                                                            \
"    ldr  x9,  [x28, #0x008]\n"                                                            \
"    ldr  x10, [x28, #0x010]\n"                                                            \
"    ldr  x12, [x28, #0x018]\n"                                                            \
"    bl   Lwin_open\n"                                                                     \
"    mov  x14, x9\n"                                                                       \
"Lro_round:\n"                                                                               \
"    mov  x0, x10\n"                                                                       \
"    mov  x15, x8\n"                                                                       \
"Lro_grp:\n"                                                                                 \
"    isb\n"                                                                                \
"    mrs  x16, cntvct_el0\n"                                                               \
"    mov  x13, x12\n"                                                                      \
"2:  and  x1, x0, #0x3f000\n"               /* same cache-set spreading as mode 1 */       \
"    lsr  x1, x1, #6\n"                                                                    \
"    add  x1, x0, x1\n"                                                                    \
"    str  x1, [x1]\n"                                                                      \
"    add  x0, x0, #4096\n"                                                                 \
"    subs x13, x13, #1\n"                                                                  \
"    b.ne 2b\n"                                                                            \
"    isb\n"                                                                                \
"    mrs  x17, cntvct_el0\n"                                                               \
"    sub  x17, x17, x16\n"                                                                 \
"    str  x17, [x11], #8\n"                                                                \
"    subs x15, x15, x12\n"                                                                 \
"    b.ne Lro_grp\n"                                                                       \
"    subs x14, x14, #1\n"                                                                  \
"    b.ne Lro_round\n"                                                                     \
"    bl   Lwin_close\n"                                                                    \
"    hvc  #0\n"                                                                            \
                                                                                           \
/* ---------------- mode 3: shadow-copy compare / checksum ---------------- */              \
/* p0 buffer A VA (B is A + 2 MiB), p1 bytes, p2 iters, p3 rounds */                        \
"Lm_cmp:\n"                                                                                  \
"    ldr  x10, [x28, #0x000]\n"                                                            \
"    ldr  x12, [x28, #0x008]\n"                                                            \
"    ldr  x8,  [x28, #0x010]\n"                                                            \
"    ldr  x9,  [x28, #0x018]\n"                                                            \
"    adr  x7, Lb_cmp_warm\n"                                                               \
"    bl   Ltimeit\n"                                                                       \
"    adr  x7, Lb_cmp_rot\n"                                                                \
"    bl   Ltimeit\n"                                                                       \
"    adr  x7, Lb_cmp_early\n"                                                              \
"    bl   Ltimeit\n"                                                                       \
"    adr  x7, Lb_hash_warm\n"                                                              \
"    bl   Ltimeit\n"                                                                       \
"    adr  x7, Lb_hash_rot\n"                                                               \
"    bl   Ltimeit\n"                                                                       \
"    str  x6, [x28, #0xc38]\n"                                                             \
"    hvc  #0\n"                                                                            \
/* byte-exact compare of two equal buffers: always runs to the end (worst case) */          \
"Lb_cmp_warm:\n"                                                                             \
"    mov  x15, x8\n"                                                                       \
"    mov  x18, #0\n"                                                                       \
"1:  add  x0, x10, x18\n"                                                                  \
"    add  x1, x0, #512, lsl #12\n"                                                         \
"    mov  x6, x12\n"                                                                       \
"2:  ldp  x2, x3, [x0], #16\n"                                                             \
"    ldp  x4, x5, [x1], #16\n"                                                             \
"    cmp  x2, x4\n"                                                                        \
"    ccmp x3, x5, #0, eq\n"                                                                \
"    b.ne 3f\n"                                                                            \
"    subs x6, x6, #16\n"                                                                   \
"    b.ne 2b\n"                                                                            \
"3:  subs x15, x15, #1\n"                                                                  \
"    b.ne 1b\n"                                                                            \
"    mov  x6, x2\n"                                                                        \
"    ret\n"                                                                                \
/* the same, rotating the compared extent through a 2 MiB region (cache-cold-ish) */        \
"Lb_cmp_rot:\n"                                                                              \
"    mov  x15, x8\n"                                                                       \
"    mov  x18, #0\n"                                                                       \
"1:  add  x0, x10, x18\n"                                                                  \
"    add  x1, x0, #512, lsl #12\n"                                                         \
"    mov  x6, x12\n"                                                                       \
"2:  ldp  x2, x3, [x0], #16\n"                                                             \
"    ldp  x4, x5, [x1], #16\n"                                                             \
"    cmp  x2, x4\n"                                                                        \
"    ccmp x3, x5, #0, eq\n"                                                                \
"    b.ne 3f\n"                                                                            \
"    subs x6, x6, #16\n"                                                                   \
"    b.ne 2b\n"                                                                            \
"3:  add  x18, x18, #4096\n"                                                               \
"    and  x18, x18, #0x1ff000\n"                                                           \
"    subs x15, x15, #1\n"                                                                  \
"    b.ne 1b\n"                                                                            \
"    mov  x6, x2\n"                                                                        \
"    ret\n"                                                                                \
/* mismatch in the first 16 bytes: the cost when the compare can exit immediately */        \
"Lb_cmp_early:\n"                                                                            \
"    mov  x15, x8\n"                                                                       \
"    mov  x18, #0\n"                                                                       \
"1:  add  x0, x10, x18\n"                                                                  \
"    add  x1, x0, #512, lsl #12\n"                                                         \
"    add  x1, x1, #0x1000\n"                  /* B+4K differs from A at byte 0 */          \
"    mov  x6, x12\n"                                                                       \
"2:  ldp  x2, x3, [x0], #16\n"                                                             \
"    ldp  x4, x5, [x1], #16\n"                                                             \
"    cmp  x2, x4\n"                                                                        \
"    ccmp x3, x5, #0, eq\n"                                                                \
"    b.ne 3f\n"                                                                            \
"    subs x6, x6, #16\n"                                                                   \
"    b.ne 2b\n"                                                                            \
"3:  subs x15, x15, #1\n"                                                                  \
"    b.ne 1b\n"                                                                            \
"    mov  x6, x2\n"                                                                        \
"    ret\n"                                                                                \
/* 64-bit multiply-accumulate checksum, two independent accumulators */                     \
"Lb_hash_warm:\n"                                                                            \
"    mov  x15, x8\n"                                                                       \
"    mov  x18, #0\n"                                                                       \
"    mov  x2, #0\n"                                                                        \
"    mov  x3, #0\n"                                                                        \
"    movz x4, #0x79b1\n"                                                                   \
"    movk x4, #0x9e37, lsl #16\n"                                                          \
"1:  add  x0, x10, x18\n"                                                                  \
"    mov  x6, x12\n"                                                                       \
"2:  ldp  x1, x5, [x0], #16\n"                                                             \
"    madd x2, x2, x4, x1\n"                                                                \
"    madd x3, x3, x4, x5\n"                                                                \
"    subs x6, x6, #16\n"                                                                   \
"    b.ne 2b\n"                                                                            \
"    subs x15, x15, #1\n"                                                                  \
"    b.ne 1b\n"                                                                            \
"    eor  x6, x2, x3\n"                                                                    \
"    ret\n"                                                                                \
"Lb_hash_rot:\n"                                                                             \
"    mov  x15, x8\n"                                                                       \
"    mov  x18, #0\n"                                                                       \
"    mov  x2, #0\n"                                                                        \
"    mov  x3, #0\n"                                                                        \
"    movz x4, #0x79b1\n"                                                                   \
"    movk x4, #0x9e37, lsl #16\n"                                                          \
"1:  add  x0, x10, x18\n"                                                                  \
"    mov  x6, x12\n"                                                                       \
"2:  ldp  x1, x5, [x0], #16\n"                                                             \
"    madd x2, x2, x4, x1\n"                                                                \
"    madd x3, x3, x4, x5\n"                                                                \
"    subs x6, x6, #16\n"                                                                   \
"    b.ne 2b\n"                                                                            \
"    add  x18, x18, #4096\n"                                                               \
"    and  x18, x18, #0x1ff000\n"                                                           \
"    subs x15, x15, #1\n"                                                                  \
"    b.ne 1b\n"                                                                            \
"    eor  x6, x2, x3\n"                                                                    \
"    ret\n"                                                                                \
                                                                                           \
/* ---------------- mode 4: I-cache / D-cache maintenance for a rewritten range ------- */  \
/* p0 base VA (must be executable: a run of `add x0,x0,#1` ending in `ret`),              */ \
/* p1 bytes, p2 iters, p3 rounds, p4 stop-flag VA (0 = none; set to 1 when done)          */ \
"Lm_icache:\n"                                                                               \
"    bl   Lbar\n"                                                                          \
"    ldr  x10, [x28, #0x000]\n"                                                            \
"    ldr  x12, [x28, #0x008]\n"                                                            \
"    ldr  x8,  [x28, #0x010]\n"                                                            \
"    ldr  x9,  [x28, #0x018]\n"                                                            \
"    bl   Lwin_open\n"                                                                     \
"    adr  x7, Lb_barrieronly\n"                                                            \
"    bl   Ltimeit\n"                                                                       \
"    adr  x7, Lb_dcic\n"                                                                   \
"    bl   Ltimeit\n"                                                                       \
"    adr  x7, Lb_iconly\n"                                                                 \
"    bl   Ltimeit\n"                                                                       \
"    adr  x7, Lb_dconly\n"                                                                 \
"    bl   Ltimeit\n"                                                                       \
"    adr  x7, Lb_iallu\n"                                                                  \
"    bl   Ltimeit\n"                                                                       \
"    adr  x7, Lb_canon\n"                                                                  \
"    bl   Ltimeit\n"                                                                       \
"    adr  x7, Lb_exec_warm\n"                                                              \
"    bl   Ltimeit\n"                                                                       \
"    adr  x7, Lb_exec_ic\n"                                                                \
"    bl   Ltimeit\n"                                                                       \
"    adr  x7, Lb_exec_full\n"                                                              \
"    bl   Ltimeit\n"                                                                       \
"    adr  x7, Lb_store_only\n"                                                             \
"    bl   Ltimeit\n"                                                                       \
"    adr  x7, Lb_exec_iallu\n"                                                             \
"    bl   Ltimeit\n"                                                                       \
"    bl   Lwin_close\n"                                                                    \
"    ldr  x0, [x28, #0x020]\n"                                                             \
"    cbz  x0, 1f\n"                                                                        \
"    mov  x1, #1\n"                                                                        \
"    stlr x1, [x0]\n"                                                                      \
"1:  hvc  #0\n"                                                                            \
"Lb_barrieronly:\n"                                                                          \
"    mov  x15, x8\n"                                                                       \
"1:  dsb  ish\n"                                                                           \
"    isb\n"                                                                                \
"    subs x15, x15, #1\n"                                                                  \
"    b.ne 1b\n"                                                                            \
"    ret\n"                                                                                \
"Lb_dcic:\n"                                                                                 \
"    mov  x15, x8\n"                                                                       \
"1:  mov  x0, x10\n"                                                                       \
"    lsr  x6, x12, #6\n"                                                                   \
"2:  dc   cvau, x0\n"                                                                      \
"    ic   ivau, x0\n"                                                                      \
"    add  x0, x0, #64\n"                                                                   \
"    subs x6, x6, #1\n"                                                                    \
"    b.ne 2b\n"                                                                            \
"    dsb  ish\n"                                                                           \
"    isb\n"                                                                                \
"    subs x15, x15, #1\n"                                                                  \
"    b.ne 1b\n"                                                                            \
"    ret\n"                                                                                \
"Lb_iconly:\n"                                                                               \
"    mov  x15, x8\n"                                                                       \
"1:  mov  x0, x10\n"                                                                       \
"    lsr  x6, x12, #6\n"                                                                   \
"2:  ic   ivau, x0\n"                                                                      \
"    add  x0, x0, #64\n"                                                                   \
"    subs x6, x6, #1\n"                                                                    \
"    b.ne 2b\n"                                                                            \
"    dsb  ish\n"                                                                           \
"    isb\n"                                                                                \
"    subs x15, x15, #1\n"                                                                  \
"    b.ne 1b\n"                                                                            \
"    ret\n"                                                                                \
"Lb_dconly:\n"                                                                               \
"    mov  x15, x8\n"                                                                       \
"1:  mov  x0, x10\n"                                                                       \
"    lsr  x6, x12, #6\n"                                                                   \
"2:  dc   cvau, x0\n"                                                                      \
"    add  x0, x0, #64\n"                                                                   \
"    subs x6, x6, #1\n"                                                                    \
"    b.ne 2b\n"                                                                            \
"    dsb  ish\n"                                                                           \
"    isb\n"                                                                                \
"    subs x15, x15, #1\n"                                                                  \
"    b.ne 1b\n"                                                                            \
"    ret\n"                                                                                \
"Lb_iallu:\n"                                                                                \
"    mov  x15, x8\n"                                                                       \
"1:  ic   iallu\n"                                                                         \
"    dsb  ish\n"                                                                           \
"    isb\n"                                                                                \
"    subs x15, x15, #1\n"                                                                  \
"    b.ne 1b\n"                                                                            \
"    ret\n"                                                                                \
/* the architecturally required sequence, in the required order:                          */ \
/*   dc cvau (all lines); dsb ish; ic ivau (all lines); dsb ish; isb                      */ \
"Lb_canon:\n"                                                                                \
"    mov  x15, x8\n"                                                                       \
"1:  mov  x0, x10\n"                                                                       \
"    lsr  x6, x12, #6\n"                                                                   \
"2:  dc   cvau, x0\n"                                                                      \
"    add  x0, x0, #64\n"                                                                   \
"    subs x6, x6, #1\n"                                                                    \
"    b.ne 2b\n"                                                                            \
"    dsb  ish\n"                                                                           \
"    mov  x0, x10\n"                                                                       \
"    lsr  x6, x12, #6\n"                                                                   \
"3:  ic   ivau, x0\n"                                                                      \
"    add  x0, x0, #64\n"                                                                   \
"    subs x6, x6, #1\n"                                                                    \
"    b.ne 3b\n"                                                                            \
"    dsb  ish\n"                                                                           \
"    isb\n"                                                                                \
"    subs x15, x15, #1\n"                                                                  \
"    b.ne 1b\n"                                                                            \
"    ret\n"                                                                                \
/* execute the region with a warm I-cache: the floor for the three exec bodies */           \
"Lb_exec_warm:\n"                                                                            \
"    mov  x5, x30\n"                                                                       \
"    mov  x15, x8\n"                                                                       \
"1:  mov  x0, #0\n"                                                                        \
"    blr  x10\n"                                                                           \
"    subs x15, x15, #1\n"                                                                  \
"    b.ne 1b\n"                                                                            \
"    mov  x30, x5\n"                                                                       \
"    ret\n"                                                                                \
/* invalidate the region from the I-cache, then execute it: the REFILL cost */              \
"Lb_exec_ic:\n"                                                                              \
"    mov  x5, x30\n"                                                                       \
"    mov  x15, x8\n"                                                                       \
"1:  mov  x0, x10\n"                                                                       \
"    lsr  x6, x12, #6\n"                                                                   \
"2:  ic   ivau, x0\n"                                                                      \
"    add  x0, x0, #64\n"                                                                   \
"    subs x6, x6, #1\n"                                                                    \
"    b.ne 2b\n"                                                                            \
"    dsb  ish\n"                                                                           \
"    isb\n"                                                                                \
"    mov  x0, #0\n"                                                                        \
"    blr  x10\n"                                                                           \
"    subs x15, x15, #1\n"                                                                  \
"    b.ne 1b\n"                                                                            \
"    mov  x30, x5\n"                                                                       \
"    ret\n"                                                                                \
/* the whole SMC turnaround: rewrite one instruction word per 64 B line, full maintenance, */\
/* then execute the region. This is what a translator pays to re-emit a code page.        */ \
"Lb_exec_full:\n"                                                                            \
"    mov  x5, x30\n"                                                                       \
"    mov  x15, x8\n"                                                                       \
"    movz x4, #0x0400\n"                                                                   \
"    movk x4, #0x9100, lsl #16\n"          /* add x0, x0, #1 */                            \
"1:  mov  x0, x10\n"                                                                       \
"    lsr  x6, x12, #6\n"                                                                   \
"2:  str  w4, [x0]\n"                                                                      \
"    add  x0, x0, #64\n"                                                                   \
"    subs x6, x6, #1\n"                                                                    \
"    b.ne 2b\n"                                                                            \
"    mov  x0, x10\n"                                                                       \
"    lsr  x6, x12, #6\n"                                                                   \
"3:  dc   cvau, x0\n"                                                                      \
"    add  x0, x0, #64\n"                                                                   \
"    subs x6, x6, #1\n"                                                                    \
"    b.ne 3b\n"                                                                            \
"    dsb  ish\n"                                                                           \
"    mov  x0, x10\n"                                                                       \
"    lsr  x6, x12, #6\n"                                                                   \
"4:  ic   ivau, x0\n"                                                                      \
"    add  x0, x0, #64\n"                                                                   \
"    subs x6, x6, #1\n"                                                                    \
"    b.ne 4b\n"                                                                            \
"    dsb  ish\n"                                                                           \
"    isb\n"                                                                                \
"    mov  x0, #0\n"                                                                        \
"    blr  x10\n"                                                                           \
"    subs x15, x15, #1\n"                                                                  \
"    b.ne 1b\n"                                                                            \
"    mov  x30, x5\n"                                                                       \
"    ret\n"                                                                                \
/* the whole-I-cache hammer, then execute: IC IALLU is O(1) in the range size, but it also */\
/* throws away the translator's OWN hot code, so the refill it causes is the real cost.    */\
"Lb_exec_iallu:\n"                                                                           \
"    mov  x5, x30\n"                                                                       \
"    mov  x15, x8\n"                                                                       \
"1:  ic   iallu\n"                                                                         \
"    dsb  ish\n"                                                                           \
"    isb\n"                                                                                \
"    mov  x0, #0\n"                                                                        \
"    blr  x10\n"                                                                           \
"    subs x15, x15, #1\n"                                                                  \
"    b.ne 1b\n"                                                                            \
"    mov  x30, x5\n"                                                                       \
"    ret\n"                                                                                \
/* control for Lb_exec_full: the rewrite stores alone, no maintenance, no execution */      \
"Lb_store_only:\n"                                                                           \
"    mov  x15, x8\n"                                                                       \
"    movz x4, #0x0400\n"                                                                   \
"    movk x4, #0x9100, lsl #16\n"                                                          \
"1:  mov  x0, x10\n"                                                                       \
"    lsr  x6, x12, #6\n"                                                                   \
"2:  str  w4, [x0]\n"                                                                      \
"    add  x0, x0, #64\n"                                                                   \
"    subs x6, x6, #1\n"                                                                    \
"    b.ne 2b\n"                                                                            \
"    subs x15, x15, #1\n"                                                                  \
"    b.ne 1b\n"                                                                            \
"    ret\n"                                                                                \
                                                                                           \
/* ---------------- mode 5: contender ---------------- */                                   \
/* p0 buffer VA, p1 bytes, p2 stop-flag VA, p4 bound,                                     */ \
/* p3 kind: 0 = idle spin, 1 = walk own 4 KB pages, 2 = IC IVAU storm over own buffer,    */ \
/*          3 = execute own buffer (I-fetch pressure; buffer must end in `ret`)           */ \
"Lm_contend:\n"                                                                              \
"    bl   Lbar\n"                                                                          \
"    ldr  x10, [x28, #0x000]\n"                                                            \
"    ldr  x12, [x28, #0x008]\n"                                                            \
"    ldr  x13, [x28, #0x010]\n"                                                            \
"    ldr  x0,  [x28, #0x018]\n"                                                            \
"    ldr  x8,  [x28, #0x020]\n"                                                            \
"    mov  x18, #0\n"                                                                       \
"    cmp  x0, #2\n"                                                                        \
"    b.eq Lct_ic\n"                                                                        \
"    cmp  x0, #3\n"                                                                        \
"    b.eq Lct_exec\n"                                                                      \
"    cbz  x0, Lct_idle\n"                                                                  \
"Lct_mem:\n"                                                                                 \
"    ldar x1, [x13]\n"                                                                     \
"    cbnz x1, 9f\n"                                                                        \
"    mov  x1, x10\n"                                                                       \
"    lsr  x15, x12, #12\n"                                                                 \
"2:  ldr  x2, [x1]\n"                                                                      \
"    add  x18, x18, x2\n"                                                                  \
"    add  x1, x1, #4096\n"                                                                 \
"    subs x15, x15, #1\n"                                                                  \
"    b.ne 2b\n"                                                                            \
"    add  x18, x18, #1\n"                    /* pass counter: the loaded data is zero    */\
"    subs x8, x8, #1\n"                                                                    \
"    b.ne Lct_mem\n"                                                                       \
"    b    9f\n"                                                                            \
"Lct_ic:\n"                                                                                  \
"    ldar x1, [x13]\n"                                                                     \
"    cbnz x1, 9f\n"                                                                        \
"    mov  x1, x10\n"                                                                       \
"    lsr  x15, x12, #6\n"                                                                  \
"2:  ic   ivau, x1\n"                                                                      \
"    add  x1, x1, #64\n"                                                                   \
"    subs x15, x15, #1\n"                                                                  \
"    b.ne 2b\n"                                                                            \
"    dsb  ish\n"                                                                           \
"    isb\n"                                                                                \
"    add  x18, x18, #1\n"                                                                  \
"    subs x8, x8, #1\n"                                                                    \
"    b.ne Lct_ic\n"                                                                        \
"    b    9f\n"                                                                            \
"Lct_exec:\n"                                                                                \
"    mov  x5, x30\n"                                                                       \
"2:  ldar x1, [x13]\n"                                                                     \
"    cbnz x1, 8f\n"                                                                        \
"    mov  x0, #0\n"                                                                        \
"    blr  x10\n"                                                                           \
"    add  x18, x18, #1\n"                                                                  \
"    subs x8, x8, #1\n"                                                                    \
"    b.ne 2b\n"                                                                            \
"8:  mov  x30, x5\n"                                                                       \
"    b    9f\n"                                                                            \
"Lct_idle:\n"                                                                                \
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
/* ---------------- exception handlers ---------------- */                                  \
/* (a) skip the faulting store, count it: the bare permission-fault round trip */           \
".globl _h_skip\n"                                                                         \
"_h_skip:\n"                                                                               \
"    mrs  x25, elr_el1\n"                                                                  \
"    add  x25, x25, #4\n"                                                                  \
"    msr  elr_el1, x25\n"                                                                  \
"    add  x19, x19, #1\n"                                                                  \
"    cmp  x19, x20\n"                                                                      \
"    b.hi Lh_panic\n"                                                                      \
"    eret\n"                                                                               \
/* (b) same, but also record ESR_EL1 so the host can report ISV/DFSC */                     \
".globl _h_skip_esr\n"                                                                     \
"_h_skip_esr:\n"                                                                           \
"    mrs  x25, esr_el1\n"                                                                  \
"    str  x25, [x28, #0xc30]\n"                                                            \
"    mrs  x25, far_el1\n"                                                                  \
"    str  x25, [x28, #0xc58]\n"                                                            \
"    mrs  x25, elr_el1\n"                                                                  \
"    ldr  w26, [x25]\n"                                                                    \
"    str  x26, [x28, #0xc60]\n"                                                            \
"    add  x25, x25, #4\n"                                                                  \
"    msr  elr_el1, x25\n"                                                                  \
"    add  x19, x19, #1\n"                                                                  \
"    cmp  x19, x20\n"                                                                      \
"    b.hi Lh_panic\n"                                                                      \
"    eret\n"                                                                               \
/* (c) un-protect the page and retry the store, invalidating the stale RO entry */          \
".globl _h_unprot_tlbi\n"                                                                  \
"_h_unprot_tlbi:\n"                                                                        \
"    mrs  x25, far_el1\n"                                                                  \
"    ubfx x26, x25, #12, #9\n"                                                             \
"    add  x26, x21, x26, lsl #3\n"                                                         \
"    ldr  x27, [x26]\n"                                                                    \
"    bic  x27, x27, #0x80\n"                                                               \
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
/* (d) the same WITHOUT an invalidate. If the stale read-only entry is still cached the */  \
/*     retried store faults again at the same FAR; we detect that, invalidate, and count */ \
/*     it, so the experiment terminates either way and reports which happened.           */ \
".globl _h_unprot_notlbi\n"                                                                \
"_h_unprot_notlbi:\n"                                                                      \
"    mrs  x25, far_el1\n"                                                                  \
"    ubfx x26, x25, #12, #9\n"                                                             \
"    add  x26, x21, x26, lsl #3\n"                                                         \
"    ldr  x27, [x26]\n"                                                                    \
"    bic  x27, x27, #0x80\n"                                                               \
"    str  x27, [x26]\n"                                                                    \
"    dsb  ishst\n"                                                                         \
"    isb\n"                                                                                \
"    ldr  x27, [x28, #0xc50]\n"                                                            \
"    cmp  x27, x25\n"                                                                      \
"    b.ne 1f\n"                                                                            \
"    lsr  x27, x25, #12\n"                                                                 \
"    tlbi vaae1is, x27\n"                                                                  \
"    dsb  ish\n"                                                                           \
"    isb\n"                                                                                \
"    ldr  x27, [x28, #0xc48]\n"                                                            \
"    add  x27, x27, #1\n"                                                                  \
"    str  x27, [x28, #0xc48]\n"                                                            \
"    str  xzr, [x28, #0xc50]\n"                                                            \
"    b    2f\n"                                                                            \
"1:  str  x25, [x28, #0xc50]\n"                                                            \
"2:  add  x19, x19, #1\n"                                                                  \
"    cmp  x19, x20\n"                                                                      \
"    b.hi Lh_panic\n"                                                                      \
"    eret\n"                                                                               \
/* (e0) the fault CLASSIFIER: the doc-20/doc-21 shared-mechanism question. The handler   */  \
/*      does not know why the page was protected. It reads FAR, indexes the shadow L3 it */  \
/*      would have to read anyway, extracts the output address (= guest physical frame), */  \
/*      and loads one byte from a dense per-frame kind table (x24 is pre-biased by the   */  \
/*      host so the frame number indexes it directly). 1 = page table, 2 = code, 0 = ?.  */  \
/*      Counters at 0xc68 (code) / 0xc70 (page table) / 0xc78 (unknown).                 */  \
".globl _h_classify\n"                                                                     \
"_h_classify:\n"                                                                           \
"    mrs  x25, far_el1\n"                                                                  \
"    ubfx x26, x25, #12, #9\n"                                                             \
"    add  x26, x21, x26, lsl #3\n"                                                         \
"    ldr  x27, [x26]\n"                                                                    \
"    and  x27, x27, x23\n"                                                                 \
"    lsr  x27, x27, #12\n"                                                                 \
"    ldrb w26, [x24, x27]\n"                                                               \
"    cbz  w26, 1f\n"                                                                       \
"    cmp  w26, #1\n"                                                                       \
"    b.eq 2f\n"                                                                            \
"    ldr  x27, [x28, #0xc68]\n"                                                            \
"    add  x27, x27, #1\n"                                                                  \
"    str  x27, [x28, #0xc68]\n"                                                            \
"    b    3f\n"                                                                            \
"2:  ldr  x27, [x28, #0xc70]\n"                                                            \
"    add  x27, x27, #1\n"                                                                  \
"    str  x27, [x28, #0xc70]\n"                                                            \
"    b    3f\n"                                                                            \
"1:  ldr  x27, [x28, #0xc78]\n"                                                            \
"    add  x27, x27, #1\n"                                                                  \
"    str  x27, [x28, #0xc78]\n"                                                            \
"3:  mrs  x25, elr_el1\n"                                                                  \
"    add  x25, x25, #4\n"                                                                  \
"    msr  elr_el1, x25\n"                                                                  \
"    add  x19, x19, #1\n"                                                                  \
"    cmp  x19, x20\n"                                                                      \
"    b.hi Lh_panic\n"                                                                      \
"    eret\n"                                                                               \
/* (e) leave the page read-only forever; emulate the store through a writable alias of  */  \
/*     the same physical page. Rt comes from the faulting instruction word at ELR.       */ \
".globl _h_emul_fetch\n"                                                                   \
"_h_emul_fetch:\n"                                                                         \
"    mrs  x25, elr_el1\n"                                                                  \
"    ldr  w27, [x25]\n"                                                                    \
"    and  x27, x27, #0x1f\n"                                                               \
"    mrs  x26, far_el1\n"                                                                  \
"    add  x26, x26, x22\n"                                                                 \
"    adr  x25, Lsmc_tab\n"                                                                 \
"    add  x25, x25, x27, lsl #3\n"                                                         \
"    br   x25\n"                                                                           \
/* (f) the same, taking Rt from ESR_EL1.ISS.SRT instead (only valid if ISS.ISV == 1) */     \
".globl _h_emul_esr\n"                                                                     \
"_h_emul_esr:\n"                                                                           \
"    mrs  x27, esr_el1\n"                                                                  \
"    ubfx x27, x27, #16, #5\n"                                                             \
"    mrs  x26, far_el1\n"                                                                  \
"    add  x26, x26, x22\n"                                                                 \
"    adr  x25, Lsmc_tab\n"                                                                 \
"    add  x25, x25, x27, lsl #3\n"                                                         \
"    br   x25\n"                                                                           \
".p2align 3\n"                                                                             \
"Lsmc_tab:\n"                                                                              \
SMC_TAB                                                                                    \
"Lsmc_done:\n"                                                                             \
"    mrs  x25, elr_el1\n"                                                                  \
"    add  x25, x25, #4\n"                                                                  \
"    msr  elr_el1, x25\n"                                                                  \
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
".globl _smc_end\n"                                                                        \
"_smc_end:\n"
