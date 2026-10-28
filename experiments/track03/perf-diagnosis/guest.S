// Guest stub copied to the VM's first page: enable the MMU, return to the host (hvc #1); the block exit lands on hvc #2.
.text
.p2align 4
.globl _g_start, _g_exit, _g_dispatch, _g_end
_g_start:
  msr mair_el1, x10
  msr ttbr0_el1, x11
  msr tcr_el1, x12
  isb
  tlbi vmalle1
  dsb sy
  isb
  mrs x16, sctlr_el1
  orr x16, x16, #1          // M
  orr x16, x16, #4          // C
  orr x16, x16, #0x1000     // I
  msr sctlr_el1, x16
  isb
  ic iallu
  dsb sy
  isb
  mrs x9, actlr_el1
  mrs x10, sctlr_el1
  mrs x11, CurrentEL
  hvc #1
_g_exit:
  hvc #2
// tools/bench/attribution/tramp2.s native_dispatch (the resident's dispatch_fast with frame loads), verbatim.
_g_dispatch:
  ldr x0, [x28, #0x18]
  ldr x1, [x28, #0xa8]
  and x2, x0, #0xfff
  add x1, x1, x2, lsl #4
  ldp x2, x3, [x1]
  sub x2, x2, x0
  cbnz x2, 1f
  ldr x1, [x28, #0xb0]
  ldr x2, [x1]
  add x2, x2, #1
  str x2, [x1]
  br x3
1: brk #0x77
_g_end:
