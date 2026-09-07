.text
.p2align 4
.globl _start
_start:
    hvc #0x32
    msr daifset, #15
    mov x19, x0
    msr tpidr_el1, x0
    mov x1, #0x41000000
    sub x1, x1, x0, lsl #16
    mov sp, x1
    adrp x1, _vectors@PAGE
    add x1, x1, _vectors@PAGEOFF
    msr vbar_el1, x1
    mrs x1, actlr_el1
    orr x1, x1, #2
    msr actlr_el1, x1
    isb
    bl _guest_main
    hvc #0x30
1:  b 1b

.p2align 11
_vectors:
.macro VECTOR kind
    sub sp, sp, #16
    stp x0, x1, [sp]
    mov x0, #\kind
    b _vector_entry
    .space 112
.endm
.rept 4
    VECTOR 0
    VECTOR 1
    VECTOR 2
    VECTOR 3
.endr

_vector_entry:
    sub sp, sp, #272
    stp x0, x1, [sp, #0]
    stp x2, x3, [sp, #16]
    stp x4, x5, [sp, #32]
    stp x6, x7, [sp, #48]
    stp x8, x9, [sp, #64]
    stp x10, x11, [sp, #80]
    stp x12, x13, [sp, #96]
    stp x14, x15, [sp, #112]
    stp x16, x17, [sp, #128]
    stp x18, x19, [sp, #144]
    stp x20, x21, [sp, #160]
    stp x22, x23, [sp, #176]
    stp x24, x25, [sp, #192]
    stp x26, x27, [sp, #208]
    stp x28, x29, [sp, #224]
    str x30, [sp, #240]
    mrs x0, elr_el1
    mrs x1, spsr_el1
    stp x0, x1, [sp, #256]
    ldr x1, [sp, #0]
    ldp x2, x3, [sp, #272]
    stp x2, x3, [sp, #0]
    mov x0, sp
    bl _exception
    ldp x0, x1, [sp, #256]
    msr elr_el1, x0
    msr spsr_el1, x1
    ldp x0, x1, [sp, #0]
    ldp x2, x3, [sp, #16]
    ldp x4, x5, [sp, #32]
    ldp x6, x7, [sp, #48]
    ldp x8, x9, [sp, #64]
    ldp x10, x11, [sp, #80]
    ldp x12, x13, [sp, #96]
    ldp x14, x15, [sp, #112]
    ldp x16, x17, [sp, #128]
    ldp x18, x19, [sp, #144]
    ldp x20, x21, [sp, #160]
    ldp x22, x23, [sp, #176]
    ldp x24, x25, [sp, #192]
    ldp x26, x27, [sp, #208]
    ldp x28, x29, [sp, #224]
    ldr x30, [sp, #240]
    add sp, sp, #288
    eret

.globl _fault_load, _fault_site, _fault_resume
_fault_load:
_fault_site:
    ldr x0, [x0]
_fault_resume:
    ret

// Handwritten translated access shapes. Labels are the fault/restart metadata.
.globl _q31_checkpoint
_q31_checkpoint:
    hvc #0x33
    ret
.globl _q32_checkpoint
_q32_checkpoint:
    hvc #0x34
    ret
.globl _q33_checkpoint
_q33_checkpoint:
    hvc #0x35
    ret
.globl _q33_transfer, _q33_transfer_exit, _q33_transfer_resume, _q33_transfer_copy_end
_q33_transfer:
    stp x19, x20, [sp, #-32]!
    str x30, [sp, #16]
    mov x19, x0
    ldp x0, x1, [x19]
    ldp x2, x3, [x19, #16]
    ldp x4, x5, [x19, #32]
    ldp x6, x7, [x19, #48]
    ldp x8, x9, [x19, #64]
    ldp x10, x11, [x19, #80]
    ldp x12, x13, [x19, #96]
    ldp x14, x15, [x19, #112]
_q33_transfer_exit:
    hvc #0x36
_q33_transfer_resume:
    msr daifclr, #2
    isb
    .rept 16
    nop
    .endr
    stp x0, x1, [x19]
    stp x2, x3, [x19, #16]
    stp x4, x5, [x19, #32]
    stp x6, x7, [x19, #48]
    stp x8, x9, [x19, #64]
    stp x10, x11, [x19, #80]
    stp x12, x13, [x19, #96]
    stp x14, x15, [x19, #112]
_q33_transfer_copy_end:
    ldr x30, [sp, #16]
    ldp x19, x20, [sp], #32
    ret
.globl _q32_hold, _q32_hold_begin, _q32_hold_end
_q32_hold:
_q32_hold_begin:
    mov x4, #1
    stlr x4, [x0]
    msr daifclr, #2
1:  ldar x4, [x1]
    cmp x4, x2
    b.hs 2f
    mrs x4, cntvct_el0
    cmp x4, x3
    b.hs 3f
    yield
    b 1b
_q32_hold_end:
2:  mov x0, #1
    ret
3:  mov x0, #0
    ret
.globl _q32_wait, _q32_wait_begin, _q32_wait_end
_q32_wait:
    sevl
    wfe
_q32_wait_begin:
    mov x4, #1
    stlr x4, [x0]
    msr daifclr, #2
1:  ldar x4, [x1]
    cmp x4, x2
    b.hs 2f
    mrs x4, cntvct_el0
    cmp x4, x3
    b.hs 3f
    wfe
.globl _q32_wait_after
_q32_wait_after:
    b 1b
_q32_wait_end:
2:  mov x0, #1
    ret
3:  mov x0, #0
    ret
.globl _q30_load8, _q30_load8_site, _q30_load8_return
_q30_load8:
_q30_load8_site:
    ldrb w0, [x0]
_q30_load8_return:
    ret
.globl _q30_load64, _q30_load_site, _q30_load_return
_q30_load64:
_q30_load_site:
    ldr x0, [x0]
_q30_load_return:
    ret
.globl _q30_store64, _q30_store_site, _q30_store_return
_q30_store64:
_q30_store_site:
    str x1, [x0]
_q30_store_return:
    mov x0, x1
    ret
.globl _q30_store8, _q30_byte_site, _q30_byte_return
_q30_store8:
_q30_byte_site:
    strb w1, [x0]
_q30_byte_return:
    mov x0, x1
    ret
.globl _q30_cas, _q30_cas_load, _q30_cas_store, _q30_cas_return
_q30_cas:
    mov x5, x0
_q30_cas_load:
    ldaxr x3, [x5]
    cmp x3, x2
    csel x4, x1, x3, eq
_q30_cas_store:
    stlxr w6, x4, [x5]
    cbnz w6, _q30_cas_load
_q30_cas_return:
    mov x0, x3
    ret
.globl _q30_exec, _q30_exec_return
_q30_exec:
    stp x29, x30, [sp, #-16]!
    blr x0
_q30_exec_return:
    ldp x29, x30, [sp], #16
    ret

.globl _q34_checkpoint
_q34_checkpoint:
    hvc #0x37
    ret
