.text
.globl _run_block
.p2align 2
// x0 = regs[32] (x0..x31 values; x28 = frame), x1 = entry. Exit stub = _exit_stub, saves back.
_run_block:
  sub sp, sp, #112
  stp x19, x20, [sp, #0]
  stp x21, x22, [sp, #16]
  stp x23, x24, [sp, #32]
  stp x25, x26, [sp, #48]
  stp x27, x28, [sp, #64]
  stp x29, x30, [sp, #80]
  str x0, [sp, #96]
  adrp x2, _saved_sp@PAGE
  add x2, x2, _saved_sp@PAGEOFF
  mov x3, sp
  str x3, [x2]
  mov x30, x1
  mov x9, x0
  ldp x4, x5, [x9, #32]
  ldp x6, x7, [x9, #48]
  ldr x8, [x9, #64]
  ldp x10, x11, [x9, #80]
  ldp x12, x13, [x9, #96]
  ldp x14, x15, [x9, #112]
  ldp x16, x17, [x9, #128]
  ldr x19, [x9, #152]
  ldp x20, x21, [x9, #160]
  ldp x22, x23, [x9, #176]
  ldp x24, x25, [x9, #192]
  ldp x26, x27, [x9, #208]
  ldp x28, x29, [x9, #224]
  ldr x9, [x9, #72]
  br x30
.globl _exit_stub
.p2align 2
_exit_stub:
  adrp x1, _saved_sp@PAGE
  add x1, x1, _saved_sp@PAGEOFF
  ldr x1, [x1]
  mov sp, x1
  ldr x1, [sp, #96]
  stp x4, x5, [x1, #32]
  stp x6, x7, [x1, #48]
  stp x8, x9, [x1, #64]
  stp x10, x11, [x1, #80]
  stp x12, x13, [x1, #96]
  stp x14, x15, [x1, #112]
  stp x16, x17, [x1, #128]
  str x19, [x1, #152]
  stp x26, x27, [x1, #208]
  ldp x19, x20, [sp, #0]
  ldp x21, x22, [sp, #16]
  ldp x23, x24, [sp, #32]
  ldp x25, x26, [sp, #48]
  ldp x27, x28, [sp, #64]
  ldp x29, x30, [sp, #80]
  add sp, sp, #112
  ret
.globl _calib
.p2align 4
// x0 = iterations; 64 dependent adds per iteration
_calib:
  mov x1, #0
1:
  .rept 64
  add x1, x1, #1
  .endr
  subs x0, x0, #1
  b.ne 1b
  mov x0, x1
  ret
.data
.globl _saved_sp
_saved_sp: .quad 0
.text
.globl _native_dispatch
.p2align 2
// The resident's dispatch_fast (crates/runtime/src/gates.S) with its two literal-pool loads replaced by frame loads.
_native_dispatch:
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
