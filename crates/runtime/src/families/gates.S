// Family gates (`families` feature): appended to gates.S, sharing its macros and operands.
.section .text,"ax"

.global restart_dispatch
restart_dispatch:
    ldr x9, ={stack_top}
    mov sp, x9
    b runtime_dispatch
.ltorg

.global leave_exception
leave_exception:
    ldr x9, ={exception_stack_top}
    mov sp, x9
    msr spsel, #0
    b restart_dispatch
.ltorg

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
