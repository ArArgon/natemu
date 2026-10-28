; memory loop: load-modify-store over a 1 MiB array, STRIDE bytes per iteration (wraps at 1 MiB); 8 insns per iteration.
; In: rcx=N rbx=array base rsi=0 (offset) r8=0 (checksum of loaded+modified values).
%ifndef STRIDE
%error "assemble with -DSTRIDE=64 or -DSTRIDE=4096"
%endif
bits 64
l:  mov rax, [rbx+rsi]
    add rax, rcx
    mov [rbx+rsi], rax
    add r8, rax
    add rsi, STRIDE
    and rsi, 0xFFFFF
    dec rcx
    jnz l
