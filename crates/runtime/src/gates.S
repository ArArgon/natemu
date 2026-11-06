// Offsets come from repr(C) NativeFrame through global_asm const operands.
.section .text.boot,"ax"
.global _start
_start:
    msr daifset, #15
    msr ttbr0_el1, x0
    // Separate upper root; legacy profiles keep TTBR1 disabled in TCR.
    add x9, x0, #{upper_root_pages}, lsl #12
    msr ttbr1_el1, x9
    msr tcr_el1, x1
    msr mair_el1, x2
    isb
    mrs x9, sctlr_el1
    orr x9, x9, #1
    orr x9, x9, #4
    orr x9, x9, #0x1000
    msr sctlr_el1, x9
    isb
    ldr x9, =private_start
    br x9
private_start:
    // Remove the temporary low alias before guest addresses are admitted.
    ldr x9, ={identity_entry}
    str xzr, [x9]
    dsb ishst
    tlbi vmalle1
    dsb ish
    isb
    adr x9, vectors
    msr vbar_el1, x9
    ldr x9, ={shared_address}
    msr tpidr_el1, x9
    ldr x9, ={exception_stack_top}
    mov sp, x9
    ldr x9, ={stack_top}
    msr sp_el0, x9
    msr spsel, #0
    bl runtime_main
    b .
.ltorg

.section .text,"ax"
.macro save_native
    sub sp, sp, #{frame_size}
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
    stp q0, q1, [sp, #({q_offset}+0)]
    stp q2, q3, [sp, #({q_offset}+32)]
    stp q4, q5, [sp, #({q_offset}+64)]
    stp q6, q7, [sp, #({q_offset}+96)]
    stp q8, q9, [sp, #({q_offset}+128)]
    stp q10, q11, [sp, #({q_offset}+160)]
    stp q12, q13, [sp, #({q_offset}+192)]
    stp q14, q15, [sp, #({q_offset}+224)]
    stp q16, q17, [sp, #({q_offset}+256)]
    stp q18, q19, [sp, #({q_offset}+288)]
    stp q20, q21, [sp, #({q_offset}+320)]
    stp q22, q23, [sp, #({q_offset}+352)]
    stp q24, q25, [sp, #({q_offset}+384)]
    stp q26, q27, [sp, #({q_offset}+416)]
    stp q28, q29, [sp, #({q_offset}+448)]
    stp q30, q31, [sp, #({q_offset}+480)]
    mrs x9, fpcr
    str x9, [sp, #{fpcr_offset}]
    // The resident never runs under the guest's FPCR (I5a: rounding, flush and any trap enables are generated code's).
    msr fpcr, xzr
    mrs x9, fpsr
    str x9, [sp, #{fpsr_offset}]
    str xzr, [sp, #{reserved_offset}]
    // X2: alignment checking (SCTLR_EL1.A, set only while generated code runs under x86 alignment checking: CR0.AM,
    // RFLAGS.AC, CPL 3) is off before the resident runs any code of its own. The saves above are aligned.
    mrs x9, sctlr_el1
    tbz x9, #1, 1f
    bic x9, x9, #2
    msr sctlr_el1, x9
    isb
1:
.endm

exception_capture:
    save_native
    mrs x9, sp_el0
    str x9, [sp, #{sp_offset}]
    mrs x9, spsr_el1
    and x9, x9, #0xf0000000
    str x9, [sp, #{nzcv_offset}]
    mov x0, sp
    mov x1, #1
    bl finish
    b .

// Generated code runs at EL0: its exceptions arrive in the lower-EL AArch64 group, synchronous (`svc`, `brk`, aborts,
// traps) at +0x400 and IRQ (the doorbell, `irq_gate`) at +0x480. Every other vector is the resident's own fault (it runs
// at EL1 with IRQs masked): it reaches the capture gate too, and `returned` halts on its SPSR (not EL0t).
.balign 2048
vectors:
    b exception_capture
    .balign 128
    b exception_capture
    .balign 128
    b exception_capture
    .balign 128
    b exception_capture
    .balign 128
    b exception_capture
    .balign 128
    b exception_capture
    .balign 128
    b exception_capture
    .balign 128
    b exception_capture
    .balign 128
    b exception_capture
    .balign 128
    b irq_gate
    .balign 128
    b exception_capture
    .balign 128
    b exception_capture
    .balign 128
    b exception_capture
    .balign 128
    b exception_capture
    .balign 128
    b exception_capture
    .balign 128
    b exception_capture
    .balign 128

// The IRQ gate: a doorbell interrupted generated code at EL0 (the resident runs with interrupts masked); ELR_EL1 is
// the next instruction, SPSR_EL1 its PSTATE.
.global irq_gate
irq_gate:
    save_native
    mrs x9, sp_el0
    str x9, [sp, #{sp_offset}]
    mrs x9, spsr_el1
    and x9, x9, #0xf0000000
    str x9, [sp, #{nzcv_offset}]
    mov x0, sp
    mov x1, #3
    bl finish
    b .

.global resume_exception
resume_exception:
    // x3 != 0: generated code runs under x86 alignment checking (X2, #AC): every misaligned data access at EL0 takes an
    // alignment fault (SCTLR_EL1.A). Every load below is aligned.
    cbz x3, 1f
    mrs x9, sctlr_el1
    orr x9, x9, #2
    msr sctlr_el1, x9
    isb
1:
    msr elr_el1, x1
    msr spsr_el1, x2
    mov x16, x0
    ldr x9, [x16, #{sp_offset}]
    msr sp_el0, x9
    ldr x9, ={exception_stack_top}
    mov sp, x9
    ldr x9, [x16, #{fpcr_offset}]
    msr fpcr, x9
    ldr x9, [x16, #{fpsr_offset}]
    msr fpsr, x9
    ldp q0, q1, [x16, #({q_offset}+0)]
    ldp q2, q3, [x16, #({q_offset}+32)]
    ldp q4, q5, [x16, #({q_offset}+64)]
    ldp q6, q7, [x16, #({q_offset}+96)]
    ldp q8, q9, [x16, #({q_offset}+128)]
    ldp q10, q11, [x16, #({q_offset}+160)]
    ldp q12, q13, [x16, #({q_offset}+192)]
    ldp q14, q15, [x16, #({q_offset}+224)]
    ldp q16, q17, [x16, #({q_offset}+256)]
    ldp q18, q19, [x16, #({q_offset}+288)]
    ldp q20, q21, [x16, #({q_offset}+320)]
    ldp q22, q23, [x16, #({q_offset}+352)]
    ldp q24, q25, [x16, #({q_offset}+384)]
    ldp q26, q27, [x16, #({q_offset}+416)]
    ldp q28, q29, [x16, #({q_offset}+448)]
    ldp q30, q31, [x16, #({q_offset}+480)]
    ldr x0, [x16, #0]
    ldr x1, [x16, #8]
    ldr x2, [x16, #16]
    ldr x3, [x16, #24]
    ldr x4, [x16, #32]
    ldr x5, [x16, #40]
    ldr x6, [x16, #48]
    ldr x7, [x16, #56]
    ldr x8, [x16, #64]
    ldr x9, [x16, #72]
    ldr x10, [x16, #80]
    ldr x11, [x16, #88]
    ldr x12, [x16, #96]
    ldr x13, [x16, #104]
    ldr x14, [x16, #112]
    ldr x15, [x16, #120]
    ldr x17, [x16, #136]
    ldr x18, [x16, #144]
    ldr x19, [x16, #152]
    ldr x20, [x16, #160]
    ldr x21, [x16, #168]
    ldr x22, [x16, #176]
    ldr x23, [x16, #184]
    ldr x24, [x16, #192]
    ldr x25, [x16, #200]
    ldr x26, [x16, #208]
    ldr x27, [x16, #216]
    ldr x28, [x16, #224]
    ldr x29, [x16, #232]
    ldr x30, [x16, #240]
    ldr x16, [x16, #128]
    eret


// Allocated roots can include x16 and the frame below SP. Use the exception
// stack for capture so return does not overwrite still-live recovery spills.
.global enter_allocated
enter_allocated:
    msr spsel, #1
    b resume_exception

// The dispatcher, copied by the resident into the code area (`cache::DISPATCH`) so that computed exits reach it with
// one `b`; it runs from there, never here. Data-free; its `adrp` is only the form: the resident writes the copy's own
// (`cache::fill_head`, to L1 table 0; each dispatcher slot's names its table). x0 = the next guest RIP, guest state in
// the static registers. It branches to the RIP's L1 entry, code the resident writes (`cache::L1_CODE`): a hit enters
// the cached block, a miss comes back to the `svc #MISS` here (`cache::DISPATCH_MISS`, the last word), which enters
// the resident. Uses only x0-x2 (FEX's temporaries at a block boundary) and keeps NZCV: no flag-setting instruction.
.global dispatch_template
.global dispatch_template_end
dispatch_template:
    and x1, x0, #{l1_mask}
    adrp x2, .
    add x2, x2, x1, lsl #5
    br x2
    svc #{svc_miss}
dispatch_template_end:
