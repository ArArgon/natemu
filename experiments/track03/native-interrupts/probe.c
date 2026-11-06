// Native-interrupt probe: HVF IRQ injection, in-kernel GIC (SPI, SGI, vtimer PPI), WFI, and an in-VM precise stop
// (IRQ at EL1 from EL0 -> resident makes the EL0 pad non-executable in its own stage 1 -> stop at the next block entry).
// Independent of natemu. usage: probe <mode> <N> [opt...]
//   spi N            GIC; host hv_gic_set_spi (edge) into vCPU0 running EL0 blocks
//   inj N [level]    no GIC; host hv_vcpus_exit + hv_vcpu_set_pending_interrupt(IRQ), guest acks with hvc #1
//   vt N [gic]       vtimer, random 20-100 us deadlines, EL0 blocks (no GIC: VTIMER_ACTIVATED exits + host pending IRQ)
//   stop N [is]      GIC + vtimer; IRQ handler arms the pad (UXN=1 + TLBI VALE1[IS] by ASID); stop at next `bl pad`
//   armbench N [is]  EL1 loop: arm+disarm the pad descriptor N times (two descriptor writes + two TLBIs)
//   sgi N [wfi]      GIC, 2 vCPUs: vCPU0 (EL1) writes ICC_SGI1R_EL1 -> vCPU1 (EL0 blocks, or EL1 WFI loop with `wfi`)
//   wfi N [gic]      vtimer, vCPU in an EL1 WFI loop
//   checks           EL0 tries msr daifset / ICC_PMR / ICC_SGI1R / wfi; reports the EL1 trap syndromes
//   opt park=US      host policy on a WFI exit: sleep until the vtimer deadline, at most US microseconds (default 0 = re-run)
#include <Hypervisor/Hypervisor.h>
#include <mach/mach_time.h>
#include <pthread.h>
#include <pthread/qos.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>

#define BASE UINT64_C(0x40000000)
#define SIZE 0x200000u
#define PG(n) (BASE + (uint64_t)(n) * 0x1000u)
#define REC(v) (0x20 + (v))          // per-vCPU record page (EL1 RW)
#define EL0D(v) (0x28 + (v))         // per-vCPU EL0 data page (progress slot)
#define MBOX 0x2f                    // SGI timestamp mailbox (EL1 RW)
#define PADCOPY 0x30                 // vCPU1's private copy of the pad page
#define SAMP(v) (0x100 + 0x10 * (v)) // 8 pages of samples per vCPU
#define TBL(v) (0x1e0 + 4 * (v))
#define ALT(v) (0x1f0 + 4 * (v))     // the 'stop view' tables of vCPU v: same, but the pad has UXN = 1     // L0..L3 of vCPU v (EL1 RW, so the resident can edit its own L3)
#define NSAMP 4096
// record offsets (asm uses the same numbers)
enum { R_CNT = 0x00, R_LAST = 0x08, R_INTID = 0x10, R_ELR = 0x18, R_FLAGS = 0x20, R_DMIN = 0x28, R_DMASK = 0x30, R_RNG = 0x38,
       R_TIRQ = 0x40, R_TARGET = 0x48, R_STOPS = 0x50, R_BAD = 0x58, R_ARMSUM = 0x60, R_LEVEL = 0x68, R_PADDESC = 0x70,
       R_PADVAL = 0x78, R_TLBI = 0x80, R_SAMP = 0x88, R_NSAMP = 0x90, R_MBOX = 0x98, R_SPUR = 0xa0, R_PEER = 0xa8,
       R_SGI = 0xb0, R_EL0PC = 0xb8, R_ROLE = 0xc0, R_ARMMAX = 0xd8, R_PADVA = 0xe0, R_NTRAP = 0xe8, R_TTBR_RUN = 0xf0, R_TTBR_STOP = 0xf8, R_HIST = 0x100, R_TRAPS = 0x180 };
enum { F_GIC = 1, F_TIMER = 2, F_ARM = 4, F_ACK = 8, F_LEVEL = 16, F_TLBIIS = 32, F_SGI = 64, F_ASID = 128, F_PATCH = 256, F_UNLINK = 512 };
enum { R_ARMVAL = 0x1e0, R_ULWORD = 0x1e8, R_ULADDR = 0x1f0, R_IMMED = 0x1f8 };
#define CHK 0x2e                     // read-only check page for the load-based stop

// Handler scratch: x9-x17 (EL0 blocks use x0-x8, x30). x18 = this vCPU's record page.
__asm__(
".text\n.balign 4096\n_g_start:\n"
"_vectors:\n"
"  b Lbad\n .balign 128\n b Lbad\n .balign 128\n b Lfiq\n .balign 128\n b Lser\n .balign 128\n"       // cur EL, SP0
"  b Lbad\n .balign 128\n b _irq_h\n .balign 128\n b Lfiq\n .balign 128\n b Lser\n .balign 128\n"     // cur EL, SPx
"  b _low_sync\n .balign 128\n b _irq_h\n .balign 128\n b Lfiq\n .balign 128\n b Lser\n .balign 128\n" // lower EL A64
"  b Lbad\n .balign 128\n b Lbad\n .balign 128\n b Lbad\n .balign 128\n b Lbad\n .balign 128\n"
"Lbad:\n  mrs x9, esr_el1\n  mrs x10, elr_el1\n  mrs x11, far_el1\n  mrs x12, spsr_el1\n  hvc #0xbad\n  b .\n"
"Lfiq:\n  hvc #0xf1\n  b .\n"
"Lser:\n  hvc #0xe1\n  b .\n"
// ---- synchronous from EL0: pad stop (EC 0x20), trapped checks (EC 0x00/0x01/0x18), svc ----
"_low_sync:\n  mrs x9, esr_el1\n  lsr x10, x9, #26\n  cmp x10, #0x20\n  b.eq Lsi\n  cmp x10, #0x24\n  b.eq Lsd\n  cmp x10, #0x3c\n  b.eq Lsb\n  cmp x10, #0x15\n  b.eq Lsvc\n"
"  cmp x10, #0x18\n  b.eq Ltrap\n  cmp x10, #0x01\n  b.eq Ltrap\n  cbz x10, Ltrap\n  b Lbad\n"
"Ltrap:\n  ldr x10, [x18, #0xe8]\n  add x11, x18, #0x180\n  str x9, [x11, x10, lsl #3]\n  add x10, x10, #1\n  str x10, [x18, #0xe8]\n"
"  mrs x10, elr_el1\n  add x10, x10, #4\n  msr elr_el1, x10\n  eret\n"
"Lsvc:\n  and x10, x9, #0xffff\n  cmp x10, #2\n  b.ne Lbad\n  hvc #3\n  b .\n"
// ---- stops. Each path sets x10 = the boundary PC where the stop happened, x11 = the resume PC ----
"Lsi:\n  mrs x12, cntvct_el0\n  mrs x10, far_el1\n  ldr x11, [x18, #0xe0]\n  cmp x10, x11\n  b.ne 1f\n  sub x10, x30, #4\n  mov x11, x30\n  b Lsc\n"  // pad (bl)
"1:eor x13, x10, x11\n  lsr x13, x13, #12\n  cbnz x13, Lbad\n  mov x11, x10\n  b Lsc\n"                     // xn: the armed code page
"Lsd:\n  mrs x12, cntvct_el0\n  mrs x10, far_el1\n  ldr x11, [x18, #0xe0]\n  cmp x10, x11\n  b.ne Lbad\n  mrs x10, elr_el1\n  mov x11, x10\n  b Lsc\n" // ldchk
"Lsb:\n  mrs x12, cntvct_el0\n  and x13, x9, #0xffff\n  mrs x10, elr_el1\n  cmp x13, #6\n  b.eq 2f\n"
"  ldr x11, [x18, #0xe0]\n  cmp x10, x11\n  b.ne Lbad\n  sub x10, x30, #4\n  mov x11, x30\n  b Lsc\n"                      // patched pad (brk #5)
"2:ldr w13, [x18, #0x1e8]\n  sbfx x13, x13, #0, #26\n  add x11, x10, x13, lsl #2\n  mov x10, x11\n"                // unlinked exit (brk #6): stop = its target
"Lsc:\n  and x13, x10, #63\n  cbz x13, 1f\n  ldr x13, [x18, #0x58]\n  add x13, x13, #1\n  str x13, [x18, #0x58]\n"   // not a block entry
"1:ldr x13, [x18, #0x40]\n  cbnz x13, 2f\n  ldr x14, [x18, #0x58]\n  add x14, x14, #1\n  str x14, [x18, #0x58]\n  b 3f\n" // stop without IRQ
"2:sub x14, x12, x13\n  str xzr, [x18, #0x40]\n  ldr x15, [x18, #0x90]\n  cmp x15, #4096\n  b.hs 3f\n"
"  ldr x16, [x18, #0x88]\n  str x14, [x16, x15, lsl #3]\n  add x15, x15, #1\n  str x15, [x18, #0x90]\n"
"3:ldr x13, [x18, #0x50]\n  add x13, x13, #1\n  str x13, [x18, #0x50]\n"
"  ldr x15, [x18, #0x20]\n  tbz x15, #7, 12f\n  ldr x14, [x18, #0xf0]\n  msr ttbr0_el1, x14\n  b 5f\n"
"12:tbz x15, #8, 14f\n  ldr x13, [x18, #0xe0]\n  movz w14, #0x03c0\n  movk w14, #0xd65f, lsl #16\n  str w14, [x13]\n"   // pad = ret again
"  dc cvau, x13\n  dsb ish\n  ic ivau, x13\n  dsb ish\n  b 5f\n"
"14:tbz x15, #9, 15f\n  ldr x13, [x18, #0x1f0]\n  ldr w14, [x18, #0x1e8]\n  str w14, [x13]\n  dc cvau, x13\n  dsb ish\n  ic ivau, x13\n  dsb ish\n  b 5f\n"
"15:ldr x13, [x18, #0x70]\n  ldr x14, [x18, #0x78]\n  str x14, [x13]\n  dsb ishst\n"           // disarm: pad executable again
"  ldr x14, [x18, #0x80]\n  ldr x15, [x18, #0x20]\n  tbnz x15, #5, 4f\n  tlbi vale1, x14\n  dsb nsh\n  b 5f\n"
"4:tlbi vale1is, x14\n  dsb ish\n5:isb\n"
"  msr elr_el1, x11\n"
"  ldr x13, [x18, #0x50]\n  ldr x14, [x18, #0x48]\n  cmp x13, x14\n  b.lo 6f\n  hvc #2\n6:eret\n"
// ---- IRQ (from EL0 or EL1) ----
"_irq_h:\n  mrs x12, cntvct_el0\n  mrs x11, elr_el1\n  ldr x15, [x18, #0x20]\n  mov x10, #1024\n  tbz x15, #0, 1f\n"
"  mrs x10, S3_0_C12_C12_0\n  isb\n  mrs x13, cntvct_el0\n  sub x13, x13, x12\n  ldr x14, [x18, #0x1d0]\n  add x14, x14, x13\n  str x14, [x18, #0x1d0]\n"
"  cmp x10, #1020\n  b.lo 1f\n"                                    // ICC_IAR1_EL1; >= 1020 spurious
"  ldr x13, [x18, #0xa0]\n  add x13, x13, #1\n  str x13, [x18, #0xa0]\n  eret\n"
"1:ldr x13, [x18]\n  add x13, x13, #1\n  str x13, [x18]\n  str x12, [x18, #8]\n  str x10, [x18, #0x10]\n  str x11, [x18, #0x18]\n"
"  and x13, x11, #63\n  lsr x13, x13, #2\n  add x13, x18, x13, lsl #3\n  ldr x14, [x13, #0x100]\n  add x14, x14, #1\n  str x14, [x13, #0x100]\n"
"  mov x14, #-1\n  tbz x15, #1, 2f\n"
"  mrs x13, cntv_cval_el0\n  sub x14, x12, x13\n"                                             // timer lateness
"  ldr x16, [x18, #0x38]\n  eor x16, x16, x16, lsl #13\n  eor x16, x16, x16, lsr #7\n  eor x16, x16, x16, lsl #17\n  str x16, [x18, #0x38]\n"
"  ldr x13, [x18, #0x30]\n  and x16, x16, x13\n  ldr x13, [x18, #0x28]\n  add x16, x16, x13\n  add x16, x16, x12\n  msr cntv_cval_el0, x16\n  isb\n"
"2:tbz x15, #6, 3f\n  ldr x13, [x18, #0x98]\n  ldr x13, [x13]\n  sub x14, x12, x13\n"           // SGI: receive - send
"3:tbz x15, #2, 4f\n  mov x14, #-1\n  ldr x13, [x18, #0x40]\n  cbnz x13, 4f\n"
"  tbz x15, #9, 17f\n  and x13, x11, #63\n  cbnz x13, 17f\n"                                             // unlink: already at a block entry
"  ldr x13, [x18, #0x1f8]\n  add x13, x13, #1\n  str x13, [x18, #0x1f8]\n  ldr x13, [x18, #0x50]\n  add x13, x13, #1\n  str x13, [x18, #0x50]\n  b 4f\n"
"17:str x12, [x18, #0x40]\n"                                                                               // arm the stop (once)
"  isb\n  mrs x16, cntvct_el0\n  tbz x15, #7, 11f\n  ldr x17, [x18, #0xf8]\n  msr ttbr0_el1, x17\n  b 6f\n"
"11:tbz x15, #8, 13f\n  ldr x13, [x18, #0xe0]\n  movz w17, #0x00a0\n  movk w17, #0xd420, lsl #16\n  str w17, [x13]\n"   // pad = brk #5
"  dc cvau, x13\n  dsb ish\n  ic ivau, x13\n  dsb ish\n  b 6f\n"
"13:tbz x15, #9, 18f\n  bic x13, x11, #63\n  add x13, x13, #56\n  ldr w17, [x13]\n  str w17, [x18, #0x1e8]\n  str x13, [x18, #0x1f0]\n" // unlink: the block's exit
"  movz w17, #0x00c0\n  movk w17, #0xd420, lsl #16\n  str w17, [x13]\n  dc cvau, x13\n  dsb ish\n  ic ivau, x13\n  dsb ish\n  b 6f\n" // -> brk #6
"18:ldr x13, [x18, #0x70]\n  ldr x17, [x18, #0x1e0]\n  str x17, [x13]\n  dsb ishst\n"
"  ldr x17, [x18, #0x80]\n  tbnz x15, #5, 5f\n  tlbi vale1, x17\n  dsb nsh\n  b 6f\n5:tlbi vale1is, x17\n  dsb ish\n6:isb\n"
"  mrs x17, cntvct_el0\n  sub x17, x17, x16\n  ldr x13, [x18, #0x60]\n  add x13, x13, x17\n  str x13, [x18, #0x60]\n"
"  ldr x13, [x18, #0xd8]\n  cmp x17, x13\n  csel x13, x17, x13, hi\n  str x13, [x18, #0xd8]\n"
"4:cmn x14, #1\n  b.eq 7f\n  ldr x13, [x18, #0x90]\n  cmp x13, #4096\n  b.hs 7f\n  ldr x16, [x18, #0x88]\n  str x14, [x16, x13, lsl #3]\n"
"  add x13, x13, #1\n  str x13, [x18, #0x90]\n"
"7:tbz x15, #0, 8f\n  isb\n  mrs x16, cntvct_el0\n  msr S3_0_C12_C12_1, x10\n  isb\n  mrs x17, cntvct_el0\n  sub x17, x17, x16\n"
"  ldr x13, [x18, #0x1c0]\n  add x13, x13, x17\n  str x13, [x18, #0x1c0]\n"                    // EOI cost
"8:isb\n  mrs x16, cntvct_el0\n  sub x16, x16, x12\n  ldr x13, [x18, #0x1c8]\n  add x13, x13, x16\n  str x13, [x18, #0x1c8]\n"                                            // ICC_EOIR1_EL1
"  tbz x15, #4, 9f\n  ldr x13, [x18, #0x68]\n  add x13, x13, #1\n  str x13, [x18, #0x68]\n  cmp x13, #5\n  b.lo Leret\n"
"9:tbz x15, #3, 10f\n  hvc #1\n"                                                            // ack to the host
"10:tbnz x15, #2, Leret\n  ldr x13, [x18]\n  ldr x14, [x18, #0x48]\n  cmp x13, x14\n  b.lo Leret\n  hvc #2\n"
"Leret:\n  eret\n"
// ---- EL1 start (EL1h, DAIF masked). x18 = record ----
"_el1_start:\n  ldr x15, [x18, #0x20]\n  tbz x15, #0, 1f\n"
"  mrs x9, S3_0_C12_C12_5\n  orr x9, x9, #1\n  msr S3_0_C12_C12_5, x9\n  isb\n"                // ICC_SRE_EL1.SRE
"  mov x9, #0xff\n  msr S3_0_C4_C6_0, x9\n  mov x9, #1\n  msr S3_0_C12_C12_7, x9\n  isb\n"     // ICC_PMR_EL1, ICC_IGRPEN1_EL1
"1:tbz x15, #1, 2f\n  mrs x9, cntvct_el0\n  ldr x10, [x18, #0x28]\n  add x9, x9, x10\n  msr cntv_cval_el0, x9\n  mov x9, #1\n  msr cntv_ctl_el0, x9\n  isb\n"
"2:ldr x9, [x18, #0xc0]\n  cbz x9, Lto_el0\n  cmp x9, #1\n  b.eq Lsender\n  cmp x9, #2\n  b.eq Lwfi\n  cmp x9, #3\n  b.eq Larmbench\n  b Lbad\n"
"Lto_el0:\n  ldr x9, [x18, #0xb8]\n  msr elr_el1, x9\n  msr spsr_el1, xzr\n  eret\n"           // EL0t, DAIF clear
"Lwfi:\n  msr daifclr, #2\n1:wfi\n  b 1b\n"
"Lsender:\n  ldr x5, [x18, #0x48]\n  ldr x6, [x18, #0xa8]\n  ldr x7, [x18, #0x98]\n  ldr x8, [x18, #0xb0]\n"
"1:ldr x0, [x6]\n"
"  ldr x16, [x18, #0x38]\n  eor x16, x16, x16, lsl #13\n  eor x16, x16, x16, lsr #7\n  eor x16, x16, x16, lsl #17\n  str x16, [x18, #0x38]\n"
"  ldr x13, [x18, #0x30]\n  and x16, x16, x13\n  ldr x13, [x18, #0x28]\n  add x16, x16, x13\n  isb\n  mrs x2, cntvct_el0\n  add x2, x2, x16\n"
"2:isb\n  mrs x3, cntvct_el0\n  cmp x3, x2\n  b.lo 2b\n"
"  isb\n  mrs x3, cntvct_el0\n  str x3, [x7]\n  dsb ish\n  msr S3_0_C12_C11_5, x8\n  isb\n"
"  mrs x9, cntvct_el0\n  sub x9, x9, x3\n  ldr x10, [x18, #0x1d8]\n  add x10, x10, x9\n  str x10, [x18, #0x1d8]\n"   // timestamp, then ICC_SGI1R_EL1
"  movz x9, #0x3600\n  movk x9, #0x16e, lsl #16\n  add x2, x3, x9\n"                          // 1 s timeout
"3:ldr x4, [x6]\n  cmp x4, x0\n  b.ne 4f\n  isb\n  mrs x3, cntvct_el0\n  cmp x3, x2\n  b.lo 3b\n"
"  ldr x4, [x18, #0x58]\n  add x4, x4, #1\n  str x4, [x18, #0x58]\n"
"4:subs x5, x5, #1\n  b.ne 1b\n  hvc #2\n  b .\n"
"Larmbench:\n  ldr x5, [x18, #0x48]\n  ldr x13, [x18, #0x70]\n  ldr x14, [x18, #0x78]\n  orr x12, x14, #(1 << 54)\n  ldr x17, [x18, #0x80]\n"
"  isb\n  mrs x16, cntvct_el0\n  tbnz x15, #7, 7f\n  tbnz x15, #8, 9f\n  tbnz x15, #5, 3f\n"
"1:str x12, [x13]\n  dsb ishst\n  tlbi vale1, x17\n  dsb nsh\n  isb\n  str x14, [x13]\n  dsb ishst\n  tlbi vale1, x17\n  dsb nsh\n  isb\n"
"  subs x5, x5, #1\n  b.ne 1b\n  b 4f\n"
"3:str x12, [x13]\n  dsb ishst\n  tlbi vale1is, x17\n  dsb ish\n  isb\n  str x14, [x13]\n  dsb ishst\n  tlbi vale1is, x17\n  dsb ish\n  isb\n"
"  subs x5, x5, #1\n  b.ne 3b\n  b 4f\n"
"7:ldr x12, [x18, #0xf8]\n  ldr x14, [x18, #0xf0]\n8:msr ttbr0_el1, x12\n  isb\n  msr ttbr0_el1, x14\n  isb\n  subs x5, x5, #1\n  b.ne 8b\n  b 4f\n"
"9:ldr x13, [x18, #0xe0]\n  movz w12, #0x00a0\n  movk w12, #0xd420, lsl #16\n  movz w14, #0x03c0\n  movk w14, #0xd65f, lsl #16\n"
"10:str w12, [x13]\n  dc cvau, x13\n  dsb ish\n  ic ivau, x13\n  dsb ish\n  isb\n  str w14, [x13]\n  dc cvau, x13\n  dsb ish\n  ic ivau, x13\n  dsb ish\n  isb\n"
"  subs x5, x5, #1\n  b.ne 10b\n"
"4:isb\n  mrs x17, cntvct_el0\n  sub x17, x17, x16\n  str x17, [x18, #0x60]\n  hvc #2\n  b .\n"
// ---- EL0 code: 16 translated-style blocks, 64-byte aligned; each starts with `bl pad` (the E1 stop site) ----
".balign 4096\n_el0_start:\n"
"_el0_work:\n  mov x1, #0\n  mov x2, #0\n  b _blk0\n"
".balign 64\n_blk0:\n"
".rept 15\n  bl _pad\n  add x1, x1, #1\n  add x2, x2, #3\n  add x3, x1, x1, lsl #1\n  cmp x3, x2\n  b.ne Lfail\n  str x1, [x4]\n"
"  .rept 7\n  add x6, x6, #1\n  .endr\n  b .+8\n  .balign 64\n.endr\n"
"  bl _pad\n  add x1, x1, #1\n  add x2, x2, #3\n  add x3, x1, x1, lsl #1\n  cmp x3, x2\n  b.ne Lfail\n  str x1, [x4]\n  .rept 7\n  add x6, x6, #1\n  .endr\n  b _blk0\n"
"Lfail:\n  svc #1\n"
"_el0_checks:\n  msr daifset, #2\n  msr S3_0_C4_C6_0, xzr\n  msr S3_0_C12_C11_5, x5\n  wfi\n  svc #2\n"
".balign 4096\n_pad:\n  ret\n"
".balign 4096\n_g_end:\n");
extern const char g_start[], g_end[], el1_start[], el0_start[], el0_work[], el0_checks[], pad[], blk0[];

static uint8_t *ram;
static mach_timebase_info_data_t tb;
static double ns(uint64_t ticks) { return (double)ticks * tb.numer / tb.denom; }
static void check(hv_return_t rc, const char *s) { if (rc != HV_SUCCESS) { fprintf(stderr, "FAIL %s rc=0x%x\n", s, rc); exit(2); } }
#define HV(x) check((x), #x)
static uint64_t ga(const char *s) { return BASE + (uint64_t)(s - g_start); }
static volatile uint64_t *rec(unsigned v) { return (volatile uint64_t *)(ram + REC(v) * 0x1000); }
#define R(v, off) rec(v)[(off) / 8]

static int gic, nv, park_us; static uint32_t spi_id;
struct cpu { unsigned id; hv_vcpu_t v; hv_vcpu_exit_t *ex; _Atomic int ready, done, kick; unsigned long canc, vtim, hvc[4], ec[64], wfi;
             uint64_t bad[5]; };
static struct cpu cpus[2];

static void *vcpu_thread(void *arg) {
    struct cpu *c = arg; unsigned id = c->id;
    pthread_set_qos_class_self_np(QOS_CLASS_USER_INTERACTIVE, 0);
    HV(hv_vcpu_create(&c->v, &c->ex, NULL)); hv_vcpu_t v = c->v;
    HV(hv_vcpu_set_sys_reg(v, HV_SYS_REG_MPIDR_EL1, id));                // Aff0 = id
    uint64_t tcr = 16 | (1ull << 8) | (1ull << 10) | (3ull << 12) | (1ull << 23) | (2ull << 32);
    HV(hv_vcpu_set_sys_reg(v, HV_SYS_REG_MAIR_EL1, 0xff)); HV(hv_vcpu_set_sys_reg(v, HV_SYS_REG_TCR_EL1, tcr));
    HV(hv_vcpu_set_sys_reg(v, HV_SYS_REG_TTBR0_EL1, PG(TBL(id)) | (uint64_t)(id + 1) << 48));   // ASID = id + 1
    HV(hv_vcpu_set_sys_reg(v, HV_SYS_REG_SCTLR_EL1, 0x30d01985)); HV(hv_vcpu_set_sys_reg(v, HV_SYS_REG_ACTLR_EL1, 2));
    HV(hv_vcpu_set_sys_reg(v, HV_SYS_REG_CPACR_EL1, 3u << 20)); HV(hv_vcpu_set_sys_reg(v, HV_SYS_REG_CNTKCTL_EL1, 3));
    HV(hv_vcpu_set_sys_reg(v, HV_SYS_REG_VBAR_EL1, BASE)); HV(hv_vcpu_set_sys_reg(v, HV_SYS_REG_SP_EL1, PG(0x38 + id) + 0x1000));
    HV(hv_vcpu_set_vtimer_offset(v, 0));                                  // CNTVCT == mach_absolute_time
    HV(hv_vcpu_set_reg(v, HV_REG_PC, ga(el1_start))); HV(hv_vcpu_set_reg(v, HV_REG_CPSR, 0x3c5));
    HV(hv_vcpu_set_reg(v, HV_REG_X18, PG(REC(id)))); HV(hv_vcpu_set_reg(v, HV_REG_X4, PG(EL0D(id))));
    HV(hv_vcpu_set_reg(v, HV_REG_X5, (1ull << 24) | 2)); HV(hv_vcpu_set_reg(v, HV_REG_X8, PG(CHK)));
    if (gic) {
        hv_ipa_t rd; HV(hv_gic_get_redistributor_base(v, &rd));
        uint32_t bits = (1u << 1) | (1u << 27);                            // SGI 1, vtimer PPI 27: group 1, enabled
        HV(hv_gic_set_redistributor_reg(v, HV_GIC_REDISTRIBUTOR_REG_GICR_IGROUPR0, bits));
        HV(hv_gic_set_redistributor_reg(v, HV_GIC_REDISTRIBUTOR_REG_GICR_ISENABLER0, bits));
        uint64_t en = 0, gr = 0, typer = 0, wk = 0;
        hv_return_t wr = hv_gic_get_redistributor_reg(v, (hv_gic_redistributor_reg_t)0x14, &wk);   // GICR_WAKER (not in the enum)
        hv_return_t ww = hv_gic_set_redistributor_reg(v, (hv_gic_redistributor_reg_t)0x14, 0);
        printf("vcpu%u GICR_WAKER read rc=%#x value=%#llx, write 0 rc=%#x\n", id, wr, (unsigned long long)wk, ww);
        HV(hv_gic_get_redistributor_reg(v, HV_GIC_REDISTRIBUTOR_REG_GICR_ISENABLER0, &en));
        HV(hv_gic_get_redistributor_reg(v, HV_GIC_REDISTRIBUTOR_REG_GICR_IGROUPR0, &gr));
        HV(hv_gic_get_redistributor_reg(v, HV_GIC_REDISTRIBUTOR_REG_GICR_TYPER, &typer));
        printf("vcpu%u redistributor base=%#llx TYPER=%#llx ISENABLER0=%#llx IGROUPR0=%#llx\n", id, (unsigned long long)rd,
               (unsigned long long)typer, (unsigned long long)en, (unsigned long long)gr);
    }
    atomic_store(&c->ready, 1);
    while (atomic_load(&c->ready) != 2) usleep(100);                     // main releases all vCPUs together
    for (;;) {
        if (atomic_load(&c->kick)) HV(hv_vcpu_set_pending_interrupt(v, HV_INTERRUPT_TYPE_IRQ, true));   // until the guest acks (pending clears on every return)
        HV(hv_vcpu_run(v));
        hv_vcpu_exit_t *ex = c->ex;
        if (ex->reason == HV_EXIT_REASON_CANCELED) { c->canc++; if (atomic_load(&c->done)) break; continue; }
        if (ex->reason == HV_EXIT_REASON_VTIMER_ACTIVATED) {                // no GIC: make the IRQ pending; unmask on ack
            c->vtim++; if (!gic) HV(hv_vcpu_set_pending_interrupt(v, HV_INTERRUPT_TYPE_IRQ, true)); continue; }
        if (ex->reason != HV_EXIT_REASON_EXCEPTION) { fprintf(stderr, "FAIL vcpu%u exit reason %u\n", id, ex->reason); exit(3); }
        uint64_t syn = ex->exception.syndrome; unsigned ec = syn >> 26; c->ec[ec]++;
        if (ec == 0x01) {                                                  // WFI/WFE trapped to the host
            c->wfi++; uint64_t pc; HV(hv_vcpu_get_reg(v, HV_REG_PC, &pc)); HV(hv_vcpu_set_reg(v, HV_REG_PC, pc + 4));
            if (park_us) {
                uint64_t ctl, cval, now = mach_absolute_time(), lim = now + (uint64_t)park_us * 1000 * tb.denom / tb.numer;
                HV(hv_vcpu_get_sys_reg(v, HV_SYS_REG_CNTV_CTL_EL0, &ctl)); HV(hv_vcpu_get_sys_reg(v, HV_SYS_REG_CNTV_CVAL_EL0, &cval));
                uint64_t until = (ctl & 3) == 1 && cval < lim ? cval : lim;
                if (until > now) mach_wait_until(until);
            }
            continue;
        }
        if (ec != 0x16) { fprintf(stderr, "FAIL vcpu%u EC %#x syndrome %#llx\n", id, ec, (unsigned long long)syn); exit(3); }
        unsigned imm = syn & 0xffff;
        if (imm == 1) { c->hvc[1]++; atomic_store(&c->kick, 0); if (!gic) HV(hv_vcpu_set_vtimer_mask(v, false)); continue; }   // IRQ ack
        if (imm == 2 || imm == 3) { c->hvc[imm]++; atomic_store(&c->done, 1); break; }
        for (int i = 0; i < 4; i++) HV(hv_vcpu_get_reg(v, HV_REG_X9 + i, &c->bad[i]));
        c->bad[4] = imm; atomic_store(&c->done, -1); break;
    }
    if (gic) {                                                             // owner-thread GIC state dump
        uint64_t a[8] = {0};
        hv_gic_get_icc_reg(v, HV_GIC_ICC_REG_SRE_EL1, &a[0]); hv_gic_get_icc_reg(v, HV_GIC_ICC_REG_PMR_EL1, &a[1]);
        hv_gic_get_icc_reg(v, HV_GIC_ICC_REG_IGRPEN1_EL1, &a[2]); hv_gic_get_icc_reg(v, HV_GIC_ICC_REG_CTLR_EL1, &a[3]);
        hv_gic_get_redistributor_reg(v, HV_GIC_REDISTRIBUTOR_REG_GICR_ISPENDR0, &a[4]);
        hv_gic_get_distributor_reg((hv_gic_distributor_reg_t)0x204, &a[5]); hv_gic_get_distributor_reg((hv_gic_distributor_reg_t)0x304, &a[6]);
        hv_gic_get_distributor_reg((hv_gic_distributor_reg_t)(0x6000 + 8 * spi_id), &a[7]);
        printf("vcpu%u end: ICC SRE=%#llx PMR=%#llx IGRPEN1=%#llx CTLR=%#llx GICR_ISPENDR0=%#llx GICD_ISPENDR1=%#llx ISACTIVER1=%#llx IROUTER=%#llx\n",
               id, (unsigned long long)a[0], (unsigned long long)a[1], (unsigned long long)a[2], (unsigned long long)a[3], (unsigned long long)a[4],
               (unsigned long long)a[5], (unsigned long long)a[6], (unsigned long long)a[7]);
    }
    return NULL;
}

static int cmp(const void *a, const void *b) { uint64_t x = *(const uint64_t *)a, y = *(const uint64_t *)b; return x < y ? -1 : x > y; }
static void dist(const char *name, uint64_t *a, size_t n) {
    if (!n) { printf("%s n=0\n", name); return; }
    qsort(a, n, 8, cmp); double s = 0; for (size_t i = 0; i < n; i++) s += ns(a[i]);
#define P(q) ns(a[(size_t)((n - 1) * (q))])
    printf("%s_ns n=%zu min=%.0f p50=%.0f p90=%.0f p99=%.0f p99.9=%.0f max=%.0f mean=%.1f\n", name, n, ns(a[0]), P(0.5), P(0.9), P(0.99),
           P(0.999), ns(a[n - 1]), s / n);
}

int main(int argc, char **argv) {
    setbuf(stdout, NULL); mach_timebase_info(&tb);
    if (argc < 2) { fprintf(stderr, "usage: probe spi|inj|vt|stop|armbench|sgi|wfi|checks N [opts]\n"); return 1; }
    const char *mode = argv[1]; unsigned N = argc > 2 ? atoi(argv[2]) : 1; int opt_unlink = 0, opt_xn = 0, opt_w0 = 0, opt_patch = 0, opt_noprio = 0, opt_asid = 0, opt_is = 0, opt_gic = 0, opt_wfi = 0, opt_level = 0, opt_kick = 0;
    for (int i = 3; i < argc; i++) {
        if (!strcmp(argv[i], "is")) opt_is = 1; else if (!strcmp(argv[i], "asid")) opt_asid = 1; else if (!strcmp(argv[i], "patch")) opt_patch = 1; else if (!strcmp(argv[i], "nopad") || !strcmp(argv[i], "unlink") || !strcmp(argv[i], "xn")) opt_w0 = 1;
        else if (!strcmp(argv[i], "ldchk")) opt_w0 = 2;
        if (!strcmp(argv[i], "unlink")) opt_unlink = 1; if (!strcmp(argv[i], "xn")) opt_xn = 1; else if (!strcmp(argv[i], "noprio")) opt_noprio = 1; else if (!strcmp(argv[i], "gic")) opt_gic = 1; else if (!strcmp(argv[i], "wfi")) opt_wfi = 1;
        else if (!strcmp(argv[i], "level")) opt_level = 1; else if (!strcmp(argv[i], "kick")) opt_kick = 1; else if (!strncmp(argv[i], "park=", 5)) park_us = atoi(argv[i] + 5);
    }
    int m_spi = !strcmp(mode, "spi"), m_inj = !strcmp(mode, "inj"), m_vt = !strcmp(mode, "vt"), m_stop = !strcmp(mode, "stop"),
        m_ab = !strcmp(mode, "armbench"), m_sgi = !strcmp(mode, "sgi"), m_wfi = !strcmp(mode, "wfi"), m_chk = !strcmp(mode, "checks"), m_tput = !strcmp(mode, "tput");
    gic = m_spi || m_stop || m_sgi || ((m_vt || m_wfi || m_chk) && opt_gic); nv = m_sgi ? 2 : 1;
    pthread_set_qos_class_self_np(QOS_CLASS_USER_INTERACTIVE, 0);

    ram = mmap(NULL, SIZE, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANON, -1, 0);
    size_t blob = g_end - g_start; memcpy(ram, g_start, blob);
    unsigned el0p = (el0_start - g_start) / 0x1000, padp = (pad - g_start) / 0x1000;
    memcpy(ram + PADCOPY * 0x1000, ram + padp * 0x1000, 0x1000);
    for (int i = 0; i < 16 && opt_w0; i++) *(uint32_t *)(ram + (blk0 - g_start) + 64 * i) = opt_w0 == 1 ? 0xd503201f : 0xf9400107;   // nop | ldr x7, [x8]
    const uint64_t PAGE = 3 | 1u << 10 | 3u << 8 | 1u << 11, EL0RW = 1u << 6, BOTHRO = 3u << 6, PXN = 1ull << 53, UXN = 1ull << 54;
    for (int v = 0; v < nv; v++) {
        uint64_t *l0 = (void *)(ram + TBL(v) * 0x1000), *l1 = l0 + 512, *l2 = l0 + 1024, *l3 = l0 + 1536;
        l0[0] = PG(TBL(v) + 1) | 3; l1[1] = PG(TBL(v) + 2) | 3; l2[0] = PG(TBL(v) + 3) | 3;
        for (unsigned p = 0; p < el0p; p++) l3[p] = PG(p) | PAGE | UXN;                   // vectors + resident: EL1 only
        for (unsigned p = el0p; p < padp; p++) l3[p] = PG(p) | PAGE | (opt_unlink ? 0 : BOTHRO) | PXN;   // translated code: EL0 RX (unlink: EL0 X, EL1 RW)
        l3[CHK] = PG(CHK) | PAGE | BOTHRO | PXN | UXN;                                    // the load-check page: EL0 read-only
        uint64_t padval = PG(v ? PADCOPY : padp) | PAGE | PXN;                            // the pad: EL0 execute-only (AP 00)
        l3[padp] = padval;
        for (int w = 0; w < nv; w++) { l3[REC(w)] = PG(REC(w)) | PAGE | PXN | UXN; l3[EL0D(w)] = PG(EL0D(w)) | PAGE | EL0RW | PXN | UXN;
                                         l3[0x38 + w] = PG(0x38 + w) | PAGE | PXN | UXN; }
        l3[MBOX] = PG(MBOX) | PAGE | PXN | UXN;
        for (unsigned p = 0; p < 8; p++) l3[SAMP(v) + p] = PG(SAMP(v) + p) | PAGE | PXN | UXN;
        for (unsigned p = 0; p < 4; p++) l3[TBL(v) + p] = PG(TBL(v) + p) | PAGE | PXN | UXN;
        unsigned di = opt_w0 == 2 ? CHK : opt_xn ? el0p : padp;                           // descriptor the stop arms
        uint64_t armval = opt_w0 == 2 ? l3[CHK] & ~(3ull << 6) : l3[di] | UXN;             // ldchk: EL0 no access; else EL0 no execute
        uint64_t *a0 = (void *)(ram + ALT(v) * 0x1000);
        memcpy(a0, l0, 0x4000); a0[0] = PG(ALT(v) + 1) | 3; a0[512 + 1] = PG(ALT(v) + 2) | 3; a0[1024] = PG(ALT(v) + 3) | 3; a0[1536 + di] = armval;
        volatile uint64_t *r = rec(v);
        r[R_TTBR_RUN / 8] = PG(TBL(v)) | (uint64_t)(v + 1) << 48; r[R_TTBR_STOP / 8] = PG(ALT(v)) | (uint64_t)(v + 3) << 48;
        r[R_PADDESC / 8] = PG(TBL(v) + 3) + 8 * di; r[R_PADVAL / 8] = l3[di]; r[R_ARMVAL / 8] = armval; r[R_PADVA / 8] = PG(di);
        r[R_TLBI / 8] = (uint64_t)(v + 1) << 48 | PG(di) >> 12;                            // TLBI operand: ASID | VA[55:12]
        r[R_SAMP / 8] = PG(SAMP(v)); r[R_MBOX / 8] = PG(MBOX); r[R_EL0PC / 8] = ga(m_chk ? el0_checks : el0_work);
        r[R_RNG / 8] = 0x9e3779b97f4a7c15ull * (v + 1); r[R_TARGET / 8] = N;
        r[R_DMIN / 8] = 480; r[R_DMASK / 8] = 2047;                                       // 20 us + [0, 85 us] at 24 MHz
    }
    __asm__ volatile("dsb sy" ::: "memory");
    printf("mode=%s N=%u gic=%d nvcpu=%d park_us=%d tlbi_is=%d blob=%zu el0_page=%u pad_page=%u timebase=%u/%u\n", mode, N, gic, nv, park_us,
           opt_is, blob, el0p, padp, tb.numer, tb.denom);

    hv_vm_config_t cfg = hv_vm_config_create(); HV(hv_vm_create(cfg));
    HV(hv_vm_map(ram, BASE, SIZE, HV_MEMORY_READ | HV_MEMORY_WRITE | HV_MEMORY_EXEC));
    if (gic) {
        size_t dsz, dal, rsz, ral, r1; uint32_t sb, sc;
        HV(hv_gic_get_distributor_size(&dsz)); HV(hv_gic_get_distributor_base_alignment(&dal));
        HV(hv_gic_get_redistributor_region_size(&rsz)); HV(hv_gic_get_redistributor_base_alignment(&ral)); HV(hv_gic_get_redistributor_size(&r1));
        HV(hv_gic_get_spi_interrupt_range(&sb, &sc)); spi_id = sb;
        uint32_t vt; HV(hv_gic_get_intid(HV_GIC_INT_EL1_VIRTUAL_TIMER, &vt));
        hv_gic_config_t g = hv_gic_config_create();
        HV(hv_gic_config_set_distributor_base(g, 0x08000000)); HV(hv_gic_config_set_redistributor_base(g, 0x09000000));
        HV(hv_gic_create(g));
        printf("gic: dist size=%#zx align=%#zx redist region=%#zx per-vcpu=%#zx align=%#zx spi base=%u count=%u vtimer intid=%u\n",
               dsz, dal, rsz, r1, ral, sb, sc, vt);
        uint64_t ctlr0, ctlr, typer; HV(hv_gic_get_distributor_reg(HV_GIC_DISTRIBUTOR_REG_GICD_CTLR, &ctlr0));
        HV(hv_gic_set_distributor_reg(HV_GIC_DISTRIBUTOR_REG_GICD_CTLR, ctlr0 | 0x13));   // EnableGrp0/1, ARE
        HV(hv_gic_get_distributor_reg(HV_GIC_DISTRIBUTOR_REG_GICD_CTLR, &ctlr)); HV(hv_gic_get_distributor_reg(HV_GIC_DISTRIBUTOR_REG_GICD_TYPER, &typer));
        unsigned n = spi_id;
        HV(hv_gic_set_distributor_reg((hv_gic_distributor_reg_t)(0x80 + 4 * (n / 32)), 1u << (n % 32)));       // IGROUPR: group 1
        HV(hv_gic_set_distributor_reg((hv_gic_distributor_reg_t)(0xc00 + 4 * (n / 16)), 2u << 2 * (n % 16)));  // ICFGR: edge
        HV(hv_gic_set_distributor_reg((hv_gic_distributor_reg_t)(0x6000 + 8 * n), 0));                         // IROUTER: Aff 0.0.0.0
        HV(hv_gic_set_distributor_reg((hv_gic_distributor_reg_t)(0x100 + 4 * (n / 32)), 1u << (n % 32)));      // ISENABLER
        uint64_t rb[5]; HV(hv_gic_get_distributor_reg((hv_gic_distributor_reg_t)0x84, &rb[0])); HV(hv_gic_get_distributor_reg((hv_gic_distributor_reg_t)0x104, &rb[1]));
        HV(hv_gic_get_distributor_reg((hv_gic_distributor_reg_t)0xc08, &rb[2])); HV(hv_gic_get_distributor_reg((hv_gic_distributor_reg_t)0x420, &rb[3]));
        HV(hv_gic_get_distributor_reg((hv_gic_distributor_reg_t)0x6100, &rb[4]));
        printf("gic: readback IGROUPR1=%#llx ISENABLER1=%#llx ICFGR2=%#llx IPRIORITYR8=%#llx IROUTER32=%#llx\n", (unsigned long long)rb[0],
               (unsigned long long)rb[1], (unsigned long long)rb[2], (unsigned long long)rb[3], (unsigned long long)rb[4]);
        printf("gic: GICD_CTLR %#llx -> %#llx TYPER=%#llx spi=%u\n", (unsigned long long)ctlr0, (unsigned long long)ctlr, (unsigned long long)typer, n);
    }
    for (int v = 0; v < nv; v++) {
        volatile uint64_t *r = rec(v); uint64_t f = 0;
        if (m_spi) f = F_GIC;
        if (m_inj) f = F_ACK | (opt_level ? F_LEVEL : 0);
        if (m_vt || m_wfi) f = F_TIMER | (gic ? F_GIC : F_ACK);
        if (m_stop) f = F_GIC | F_TIMER | F_ARM | (opt_is ? F_TLBIIS : 0) | (opt_asid ? F_ASID : 0) | (opt_patch ? F_PATCH : 0) | (opt_unlink ? F_UNLINK : 0);
        if (m_ab) f = (opt_is ? F_TLBIIS : 0) | (opt_asid ? F_ASID : 0) | (opt_patch ? F_PATCH : 0);
        if (m_sgi) f = F_GIC | (v ? F_SGI : 0);
        if (m_chk && gic) f = F_GIC;
        r[R_FLAGS / 8] = f;
        r[R_ROLE / 8] = m_ab ? 3 : (m_wfi || (m_sgi && v == 1 && opt_wfi)) ? 2 : (m_sgi && v == 0) ? 1 : 0;
        if (m_sgi && v == 0) { r[R_PEER / 8] = PG(REC(1)) + R_CNT; r[R_SGI / 8] = (1ull << 24) | (1u << 1); }   // INTID 1 -> Aff0 = 1
    }
    pthread_t th[2];
    for (int v = 0; v < nv; v++) { cpus[v].id = v; pthread_create(&th[v], NULL, vcpu_thread, &cpus[v]); }
    for (int v = 0; v < nv; v++) while (atomic_load(&cpus[v].ready) != 1) usleep(100);
    if (gic) {                                                              // route the SPI again now that vCPU affinities exist
        HV(hv_gic_set_distributor_reg((hv_gic_distributor_reg_t)(0x6000 + 8 * spi_id), 0));
        if (!opt_noprio) HV(hv_gic_set_distributor_reg((hv_gic_distributor_reg_t)(0x400 + (spi_id & ~3u)), 0x80u << 8 * (spi_id & 3)));   // priority 0x80
    }
    uint64_t *lat = calloc(N + 1, 8), *cost = calloc(N + 1, 8); unsigned k = 0, lost = 0;
    uint64_t t_start = mach_absolute_time();
    for (int v = 0; v < nv; v++) atomic_store(&cpus[v].ready, 2);
    if (m_spi || m_inj) {
        usleep(20000); uint64_t rng = 0x2545f4914f6cdd1dull;
        for (k = 0; k < N && atomic_load(&cpus[0].done) == 0; k++) {
            rng ^= rng << 13; rng ^= rng >> 7; rng ^= rng << 17;
            for (uint64_t g = mach_absolute_time() + 480 + rng % 1920; mach_absolute_time() < g;) ;   // 20-100 us gap
            uint64_t c0 = R(0, R_CNT), t0 = mach_absolute_time();
            if (m_spi) { HV(hv_gic_set_spi(spi_id, true)); if (opt_kick) HV(hv_vcpus_exit(&cpus[0].v, 1)); }
            else { atomic_store(&cpus[0].kick, 1); HV(hv_vcpus_exit(&cpus[0].v, 1)); }
            uint64_t t1 = mach_absolute_time();
            while (R(0, R_CNT) == c0 && mach_absolute_time() - t1 < 24000000) ;
            if (R(0, R_CNT) == c0) { lost++; break; }
            lat[k] = R(0, R_LAST) - t0; cost[k] = t1 - t0;
        }
    }
    if (m_tput) {                                                          // block throughput, no interrupts
        volatile uint64_t *prog = (volatile uint64_t *)(ram + EL0D(0) * 0x1000);
        usleep(50000); uint64_t p0 = *prog, t0 = mach_absolute_time(); usleep(N * 1000); uint64_t p1 = *prog, t1 = mach_absolute_time();
        printf("tput block-entry=%s blocks=%llu ns_per_block=%.4f\n", opt_w0 == 0 ? "bl pad" : opt_w0 == 1 ? "nop" : "ldr x7,[x8]",
               (unsigned long long)(p1 - p0), ns(t1 - t0) / (double)(p1 - p0));
        atomic_store(&cpus[0].done, 2); HV(hv_vcpus_exit(&cpus[0].v, 1));
    }
    for (uint64_t t0 = mach_absolute_time(); ;) {                        // wait for done (60 s cap), then stop every vCPU
        int all = 1; for (int v = 0; v < nv; v++) if (!atomic_load(&cpus[v].done)) all = 0;
        if (all || mach_absolute_time() - t0 > (lost ? 2ull : 60ull) * 24000000) break;
        usleep(1000);
    }
    uint64_t t_end = mach_absolute_time();
    for (int v = 0; v < nv; v++) {
        int d = atomic_load(&cpus[v].done);
        if (!d) { printf("vcpu%d NOT DONE (timeout), forcing exit\n", v); atomic_store(&cpus[v].done, 2); HV(hv_vcpus_exit(&cpus[v].v, 1)); }
    }
    for (int v = 0; v < nv; v++) pthread_join(th[v], NULL);
    printf("wall_ms=%.1f\n", ns(t_end - t_start) / 1e6);
    for (int v = 0; v < nv; v++) {
        struct cpu *c = &cpus[v]; volatile uint64_t *r = rec(v);
        if (c->done == -1) printf("vcpu%d GUEST FAULT hvc #%#llx ESR=%#llx ELR=%#llx FAR=%#llx SPSR=%#llx\n", v, (unsigned long long)c->bad[4],
            (unsigned long long)c->bad[0], (unsigned long long)c->bad[1], (unsigned long long)c->bad[2], (unsigned long long)c->bad[3]);
        printf("vcpu%d host_exits: canceled=%lu vtimer_activated=%lu wfi_ec01=%lu hvc1_ack=%lu hvc2=%lu hvc3=%lu ec:", v, c->canc, c->vtim, c->wfi,
               c->hvc[1], c->hvc[2], c->hvc[3]);
        for (int e = 0; e < 64; e++) if (c->ec[e]) printf(" %#x=%lu", e, c->ec[e]);
        printf("\nvcpu%d guest: irqs=%llu spurious=%llu stops=%llu bad=%llu level_entries=%llu last_intid=%llu el0_progress=%llu\n", v,
               (unsigned long long)r[R_CNT / 8], (unsigned long long)r[R_SPUR / 8], (unsigned long long)r[R_STOPS / 8],
               (unsigned long long)r[R_BAD / 8], (unsigned long long)r[R_LEVEL / 8], (unsigned long long)r[R_INTID / 8],
               (unsigned long long)*(volatile uint64_t *)(ram + EL0D(v) * 0x1000));
        printf("vcpu%d irq ELR offset-in-block histogram (word 0..15):", v);
        for (int i = 0; i < 16; i++) printf(" %llu", (unsigned long long)r[R_HIST / 8 + i]);
        printf("\n");
        if (r[R_CNT / 8]) printf("vcpu%d handler: IAR read (from entry) mean_ns=%.1f; EOI write mean_ns=%.1f; entry-to-eret mean_ns=%.1f\n", v, ns(r[0x1d0 / 8]) / r[R_CNT / 8],
                                ns(r[0x1c0 / 8]) / r[R_CNT / 8], ns(r[0x1c8 / 8]) / r[R_CNT / 8]);
        if (m_sgi && v == 0) printf("vcpu0 sender: ICC_SGI1R_EL1 write + ISB mean_ns=%.1f\n", ns(r[0x1d8 / 8]) / N);
        if (m_stop && opt_unlink) printf("vcpu%d unlink: stops taken at once because the IRQ landed on a block entry: %llu\n", v, (unsigned long long)r[R_IMMED / 8]);
        if (m_stop && r[R_STOPS / 8]) printf("vcpu%d arm (%s) mean_ns=%.1f max_ns=%.0f (CNTVCT 24 MHz)\n", v, opt_unlink ? "unlink: block exit word -> brk + DC CVAU + IC IVAU + DSBs + ISB" : opt_patch ? "pad word -> brk + DC CVAU + IC IVAU + DSBs + ISB" : opt_asid ? "TTBR0 switch to the stop view + ISB" : opt_is ? "UXN=1 + TLBI VALE1IS + DSB + ISB" : "UXN=1 + TLBI VALE1 + DSB + ISB",
            ns(r[R_ARMSUM / 8]) / r[R_STOPS / 8], ns(r[R_ARMMAX / 8]));
        if (m_ab) printf("vcpu%d armbench: %u x (arm + disarm), %s: %.2f ns per pair\n", v, N, opt_patch ? "pad patch brk/ret with DC CVAU + IC IVAU" : opt_asid ? "TTBR0/ASID switch" : opt_is ? "TLBI VALE1IS" : "TLBI VALE1", ns(r[R_ARMSUM / 8]) / N);
        if (r[R_NTRAP / 8]) { printf("vcpu%d EL0 traps taken at EL1:", v);
            for (unsigned i = 0; i < r[R_NTRAP / 8] && i < 8; i++) printf(" ESR=%#llx(EC %#llx)", (unsigned long long)r[R_TRAPS / 8 + i], (unsigned long long)(r[R_TRAPS / 8 + i] >> 26));
            printf("\n"); }
        size_t ns_ = r[R_NSAMP / 8]; uint64_t *s = malloc(ns_ * 8 + 8);
        for (size_t i = 0; i < ns_; i++) s[i] = ((volatile uint64_t *)(ram + SAMP(v) * 0x1000))[i];
        if (ns_) dist(m_stop ? "irq_entry_to_stop" : m_sgi ? "sgi_send_to_handler" : "timer_deadline_to_handler", s, ns_);
    }
    if (m_spi || m_inj) {
        printf("host kicks=%u lost=%u\n", k, lost);
        dist(m_spi ? "spi_call_to_handler" : "exit+pending_to_handler", lat, k);
        dist(m_spi ? "hv_gic_set_spi_call" : "hv_vcpus_exit_call", cost, k);
    }
    _exit(0);
}
