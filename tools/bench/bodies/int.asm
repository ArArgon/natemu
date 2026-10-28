; integer ALU loop: 8 ALU instructions (add/mov/shl/xor/mov/shr/xor/imul) + dec/jnz = 10 per iteration.
; In: rcx=N rax=seed rbx=add const rsi=odd multiplier. Loop-carried dependency through rax (xorshift + multiply).
bits 64
l:  add rax, rbx
    mov rdx, rax
    shl rdx, 13
    xor rax, rdx
    mov rdx, rax
    shr rdx, 7
    xor rax, rdx
    imul rax, rsi
    dec rcx
    jnz l
