// Position-independent, stackless native EL1 fixture. Never called by host.
.text
.p2align 4
.globl _handoff_start, _handoff_end, _handoff_boot_hvc, _handoff_reply_hvc
.globl _handoff_boot_resume, _handoff_reply_resume
_handoff_start:
    msr daifset, #15
    mov x20, x8                  // immutable CPU identity
    mov x21, #0                 // native operation sequence
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
_handoff_boot_hvc:
    hvc #0x43
_handoff_boot_resume:
1:
    dsb sy                      // every release, before any payload access
    mov x16, #0x40000000
    add x16, x16, #0x10, lsl #12 // alias 0 = 0x40010000
    add x16, x16, x3, lsl #16    // alias 1 = 0x40020000
    cbz x0, 2f                  // command 0 read; command 1 write then read
    str x4, [x16]
    str x5, [x16, #128]
    str x6, [x16, #256]
    str x7, [x16, #384]
2:
    ldr x4, [x16]
    ldr x5, [x16, #128]
    ldr x6, [x16, #256]
    ldr x7, [x16, #384]
    add x21, x21, #1
    mov x8, x20
    mov x9, x21
    dsb sy                      // native all-access completion before ack
_handoff_reply_hvc:
    hvc #0x44
_handoff_reply_resume:
    b 1b
_handoff_end:
