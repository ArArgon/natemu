// Hand-written EL1 harness.  Included by the generated cells.S (one translation unit).
// x8 = vCPU id at boot.  Per cell, the host sets x0 = descriptor VA, x20 = thread id and PC = _cell_entry.
// Descriptor (VA 0x40080000): +0 iters, +8 nthreads, +0x10 delay mask, +0x18 seed, +0x20..0x38 bodies,
// +0x40 fin, +0x48 K1, +0x50 K2, +0x58 X base, +0x60 Y base, +0x68 kind (0 litmus, 1 atomicity), +0x70 M.
// Sync page (desc+0x1000): arrive[t] at +128*t, result slot t at +0x200+128*t, writer done flags at +0x400+128*w.
// Histogram: desc+0x2000, one 8-byte counter per digit-packed outcome.
.text
.p2align 12
.globl _guest_start, _boot_resume, _cell_entry
_guest_start:
    msr daifset, #15
    mov x20, x8
    msr mair_el1, x10
    msr ttbr0_el1, x11
    msr tcr_el1, x12
    dsb sy
    isb
    tlbi vmalle1
    dsb sy
    isb
    mrs x16, sctlr_el1
    orr x16, x16, #1
    orr x16, x16, #4
    orr x16, x16, #0x1000
    msr sctlr_el1, x16
    isb
    adr x13, _vectors
    msr vbar_el1, x13
    isb
    mrs x9, actlr_el1
    mrs x10, mair_el1
    mrs x11, tcr_el1
    mrs x12, sctlr_el1
    mrs x13, vbar_el1
    dsb sy
    hvc #0x43
_boot_resume:
    b _boot_resume

.p2align 11
_vectors:
    .rept 4
    b .
    .balign 0x80
    .endr
    // current EL, SPx, synchronous: only svc is expected; anything else exits to the host
    mrs x14, esr_el1
    lsr x14, x14, #26
    cmp x14, #0x15
    b.ne 1f
    br x9
1:  mrs x0, esr_el1
    mrs x1, elr_el1
    mrs x2, far_el1
    hvc #0x45
    b .
    .balign 0x80
    .rept 11
    b .
    .balign 0x80
    .endr

.macro WAITALL target
    mov x12, x25
    mov x13, x22
1:  ldar x14, [x12]
    cmp x14, \target
    b.lo 1b
    add x12, x12, #128
    subs x13, x13, #1
    b.ne 1b
.endm

_zero256:
    mov x9, #16
1:  stp xzr, xzr, [x0], #16
    subs x9, x9, #1
    b.ne 1b
    ret

.p2align 4
_cell_entry:
    mov x19, x0
    ldr x21, [x19, #0]
    ldr x22, [x19, #8]
    ldr x23, [x19, #0x10]
    ldr x24, [x19, #0x18]
    mov x9, #0x7c15
    movk x9, #0x7f4a, lsl #16
    movk x9, #0x79b9, lsl #32
    movk x9, #0x9e37, lsl #48
    add x10, x20, #1
    mul x10, x10, x9
    eor x24, x24, x10
    orr x24, x24, #1
    add x25, x19, #0x1000
    add x9, x19, #0x20
    ldr x26, [x9, x20, lsl #3]
    ldr x27, [x19, #0x40]
    mov x17, #0
    ldr x9, [x19, #0x68]
    cbnz x9, _atom_entry
.p2align 4
Liter:
    eor x9, x24, x24, lsl #13        // xorshift64
    eor x9, x9, x9, lsr #7
    eor x24, x9, x9, lsl #17
    lsr x9, x24, #56
    and x9, x9, #15
    mov x10, #10
    cmp x9, x10
    csel x9, x10, x9, hi
    lsr x11, x23, x9
    lsr x10, x24, #20
    and x10, x10, x11                // random post-barrier delay count
    ldr x0, [x19, #0x58]
    ldr x1, [x19, #0x60]
    ldr x2, [x19, #0x48]
    ldr x3, [x19, #0x50]
    lsl x11, x17, #1
    add x11, x11, #1
    add x12, x25, x20, lsl #7
    stlr x11, [x12]
    WAITALL x11
    cbz x10, 4f
3:  subs x10, x10, #1
    b.ne 3b
4:  blr x26
    add x12, x25, #0x200
    add x12, x12, x20, lsl #7
    str x0, [x12]
    lsl x11, x17, #1
    add x11, x11, #2
    add x12, x25, x20, lsl #7
    stlr x11, [x12]
    WAITALL x11
    cbnz x20, 8f
    add x9, x25, #0x200
    mov x28, #0
    mov x13, x22
5:  ldr x14, [x9]
    orr x28, x28, x14
    add x9, x9, #128
    subs x13, x13, #1
    b.ne 5b
    cbz x27, 6f
    ldr x0, [x19, #0x58]
    ldr x1, [x19, #0x60]
    ldr x2, [x19, #0x48]
    ldr x3, [x19, #0x50]
    blr x27
    orr x28, x28, x0
6:  add x9, x19, #0x2000
    ldr x10, [x9, x28, lsl #3]
    add x10, x10, #1
    str x10, [x9, x28, lsl #3]
    ldr x0, [x19, #0x58]
    bl _zero256
    ldr x0, [x19, #0x60]
    bl _zero256
8:  add x17, x17, #1
    cmp x17, x21
    b.lo Liter
    dsb sy
    hvc #0x44
9:  b 9b

_atom_entry:
    mov x11, #1
    add x12, x25, x20, lsl #7
    stlr x11, [x12]
    WAITALL x11
    ldr x0, [x19, #0x58]
    ldr x1, [x19, #0x70]
    add x4, x25, #0x400
    blr x26
    add x12, x25, #0x200
    add x12, x12, x20, lsl #7
    stp x0, x1, [x12]
    stp x2, x3, [x12, #16]
    str x15, [x12, #32]
    dsb sy
    hvc #0x44
9:  b 9b
