; call-heavy loop: call/ret of a leaf with push/pop; 11 instructions per iteration (plus one initial jmp).
; In: rcx=N rbx=callee-saved value (restored by the leaf) rsi=const r8=0 (checksum) rsp=valid stack.
bits 64
    jmp short start
leaf:
    push rbx
    lea rbx, [rdi+rdi*2]
    add rbx, rsi
    mov rax, rbx
    pop rbx
    ret
start:
l:  mov rdi, rcx
    call leaf
    add r8, rax
    dec rcx
    jnz l
