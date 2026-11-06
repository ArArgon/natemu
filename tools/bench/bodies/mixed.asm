; mixed: checksum over a 64 KiB buffer, 8 bytes per iteration, wrapping; load, byte load, multiply, shifts, store back; 13 per iteration.
; In: rcx=N rsi=buffer base rdi=0 (offset) r8=0 (sum) r9=1 (hash).
bits 64
l:  mov rax, [rsi+rdi]
    add r8, rax
    movzx edx, byte [rsi+rdi+1]
    imul r9, r9, 31
    xor r9, rdx
    mov r10, rax
    shr r10, 17
    xor r8, r10
    mov [rsi+rdi], r8
    add rdi, 8
    and edi, 0xfff8
    dec rcx
    jnz l
