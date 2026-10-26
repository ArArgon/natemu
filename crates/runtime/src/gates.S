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
.global enter
enter:
    mov x16, x0
    ldr x9, [x16, #{sp_offset}]
    mov sp, x9
    ldr x9, [x16, #{fpcr_offset}]
    msr fpcr, x9
    ldr x9, [x16, #{fpsr_offset}]
    msr fpsr, x9
    ldr x9, [x16, #{nzcv_offset}]
    msr nzcv, x9
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
    br x16

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
    mrs x9, fpsr
    str x9, [sp, #{fpsr_offset}]
    str xzr, [sp, #{reserved_offset}]
.endm

.global returned
returned:
    save_native
    add x9, sp, #{frame_size}
    str x9, [sp, #{sp_offset}]
    mrs x9, nzcv
    str x9, [sp, #{nzcv_offset}]
    mov x0, sp
    mov x1, #0
    bl finish
    b .

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

// Called only with all guest roots saved. Host may transfer x0..x15 here.
.global service
.global service_resume
service:
    mov x16, x0
    ldp x0, x1, [x16, #0]
    ldp x2, x3, [x16, #16]
    ldp x4, x5, [x16, #32]
    ldp x6, x7, [x16, #48]
    ldp x8, x9, [x16, #64]
    ldp x10, x11, [x16, #80]
    ldp x12, x13, [x16, #96]
    ldp x14, x15, [x16, #112]
    dsb sy
    hvc #0x4a
service_resume:
    stp x0, x1, [x16, #0]
    stp x2, x3, [x16, #16]
    stp x4, x5, [x16, #32]
    stp x6, x7, [x16, #48]
    stp x8, x9, [x16, #64]
    stp x10, x11, [x16, #80]
    stp x12, x13, [x16, #96]
    stp x14, x15, [x16, #112]
    ret

.global restart_dispatch
restart_dispatch:
    ldr x9, ={stack_top}
    mov sp, x9
    b runtime_dispatch
.ltorg


.global resume_exception
resume_exception:
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

.global leave_exception
leave_exception:
    ldr x9, ={exception_stack_top}
    mov sp, x9
    msr spsel, #0
    b restart_dispatch
.ltorg

// Allocated roots can include x16 and the frame below SP. Use the exception
// stack for capture so return does not overwrite still-live recovery spills.
.global enter_allocated
enter_allocated:
    msr spsel, #1
    b resume_exception
.global returned_allocated
returned_allocated:
    msr spsel, #1
    save_native
    mrs x9, sp_el0
    str x9, [sp, #{sp_offset}]
    mrs x9, nzcv
    str x9, [sp, #{nzcv_offset}]
    mov x0, sp
    mov x1, #0
    bl finish
    b .

.global helper_gate
helper_gate:
    msr spsel, #1
    save_native
    mrs x9, sp_el0
    str x9, [sp, #{sp_offset}]
    mrs x9, nzcv
    str x9, [sp, #{nzcv_offset}]
    mov x0, sp
    bl helper_prepare
    // Deliberately hostile native callee: all roots, flags and FP state change.
    mov x9, #0x400000
    msr fpcr, x9
    mov x9, #0x08000000
    orr x9, x9, #0x1
    msr fpsr, x9
    mov x9, #0xf0000000
    msr nzcv, x9
    mov x0, #0x5a
    mov x1, #0x5a
    mov x2, #0x5a
    mov x3, #0x5a
    mov x4, #0x5a
    mov x5, #0x5a
    mov x6, #0x5a
    mov x7, #0x5a
    mov x8, #0x5a
    mov x9, #0x5a
    mov x10, #0x5a
    mov x11, #0x5a
    mov x12, #0x5a
    mov x13, #0x5a
    mov x14, #0x5a
    mov x15, #0x5a
    mov x16, #0x5a
    mov x17, #0x5a
    mov x18, #0x5a
    mov x19, #0x5a
    mov x20, #0x5a
    mov x21, #0x5a
    mov x22, #0x5a
    mov x23, #0x5a
    mov x24, #0x5a
    mov x25, #0x5a
    mov x26, #0x5a
    mov x27, #0x5a
    mov x28, #0x5a
    mov x29, #0x5a
    mov x30, #0x5a
    movi v0.16b, #0xa5
    movi v1.16b, #0xa5
    movi v2.16b, #0xa5
    movi v3.16b, #0xa5
    movi v4.16b, #0xa5
    movi v5.16b, #0xa5
    movi v6.16b, #0xa5
    movi v7.16b, #0xa5
    movi v8.16b, #0xa5
    movi v9.16b, #0xa5
    movi v10.16b, #0xa5
    movi v11.16b, #0xa5
    movi v12.16b, #0xa5
    movi v13.16b, #0xa5
    movi v14.16b, #0xa5
    movi v15.16b, #0xa5
    movi v16.16b, #0xa5
    movi v17.16b, #0xa5
    movi v18.16b, #0xa5
    movi v19.16b, #0xa5
    movi v20.16b, #0xa5
    movi v21.16b, #0xa5
    movi v22.16b, #0xa5
    movi v23.16b, #0xa5
    movi v24.16b, #0xa5
    movi v25.16b, #0xa5
    movi v26.16b, #0xa5
    movi v27.16b, #0xa5
    movi v28.16b, #0xa5
    movi v29.16b, #0xa5
    movi v30.16b, #0xa5
    movi v31.16b, #0xa5
    save_native
    add x9, sp, #{frame_size}
    str x9, [sp, #{sp_offset}]
    mrs x9, nzcv
    str x9, [sp, #{nzcv_offset}]
    mov x0, sp
    bl helper_restore
    b .

.global partial_store
partial_store:
    strb w1, [x0]
    ret

// Direct-branch LFENCE sink: save computed roots before any Rust/helper work.
.global fence_sink_gate
fence_sink_gate:
    msr spsel, #1
    save_native
    mrs x9, sp_el0
    str x9, [sp, #{sp_offset}]
    mrs x9, nzcv
    str x9, [sp, #{nzcv_offset}]
    dsb sy
    isb
    mov x0, sp
    bl fence_sink_handler
    b .
