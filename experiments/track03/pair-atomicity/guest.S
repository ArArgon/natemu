.text
.p2align 4
.globl _pair_start, _pair_end, _pair_boot_resume, _pair_done_resume
.globl _pair_load, _pair_store, _pair_split_low, _pair_split_high
_pair_start:
    msr daifset, #15
    mov x20, x8                  // immutable CPU identity
    mov x21, x0                  // 0 forced split control, 1 paired sample
    mov x22, x1                  // writer CPU
    mov x23, x2                  // writer iteration bound
    msr mair_el1, x10            // Attr0 Normal WB 0xff
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
    mrs x9, actlr_el1
    mrs x10, mair_el1
    mrs x11, tcr_el1
    mrs x12, sctlr_el1
    // Read actual two stage-1 descriptors through their identity mapping.
    mov x16, #0x7000
    movk x16, #0x4000, lsl #16
    ldr x13, [x16, #128]
    ldr x14, [x16, #256]
    dsb sy
_pair_boot_hvc:
    hvc #0x43
_pair_boot_resume:
    mov x24, #0x40000000
    add x24, x24, #0x10, lsl #12
    add x24, x24, x20, lsl #16 // CPU1 uses the second alias
    add x25, x24, #128        // reader ready
    add x26, x24, #256        // writer done / control phase
    add x27, x24, #384        // control acknowledgement
    add x28, x24, #512        // reader saw a bad pair
    mov x0, #0
    mov x1, #0
    mov x2, #0
    mov x3, #0
    mov x4, #0
    mov x5, #0
    mov x6, #0
    mov x7, #0
    cmp x20, x22
    b.eq writer
    mov x9, #1
    stlr x9, [x25]
    cbnz x21, reader_loop
control_wait:
    ldar x9, [x26]
    cmp x9, #1
    b.ne control_wait
    b sample
reader_loop:
    ldar x9, [x26]
    cbnz x9, done
sample:
_pair_load:
    ldp x10, x11, [x24]       // tested 128-bit read, two64-bit GPRs
    add x0, x0, #1
    cmp x10, x5
    cinc x1, x1, ne
    mov x5, x10
    mvn x12, x10
    cmp x11, x12
    b.eq sample_good
    cbnz x2, already_bad
    mov x3, x10
    mov x4, x11
already_bad:
    add x2, x2, #1
    mov x9, #1
    stlr x9, [x28]
sample_good:
    cbnz x21, reader_loop
    mov x9, #1
    stlr x9, [x27]
control_finish:
    ldar x9, [x26]
    cmp x9, #2
    b.ne control_finish
    b done
writer:
    ldar x9, [x25]
    cbz x9, writer
    cbnz x21, writer_loop
    mov x10, #1
_pair_split_low:
    str x10, [x24]
    mov x9, #1
    stlr x9, [x26]            // publishes a known half-written pair
control_ack:
    ldar x9, [x27]
    cbz x9, control_ack
    mvn x11, x10
_pair_split_high:
    str x11, [x24, #8]
    mov x0, #1
    mov x9, #2
    stlr x9, [x26]
    b done
writer_loop:
    add x10, x0, #1
    mvn x11, x10
_pair_store:
    stp x10, x11, [x24]       // tested 128-bit write, two64-bit GPRs
    mov x0, x10
    ldar x9, [x28]
    cbnz x9, writer_finish
    cmp x0, x23
    b.lo writer_loop
writer_finish:
    mov x9, #1
    stlr x9, [x26]
done:
    dsb sy
    hvc #0x44
_pair_done_resume:
    b _pair_done_resume       // never automatically resumed
_pair_end:
