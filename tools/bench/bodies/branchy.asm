; branchy loop: two data-dependent conditional branches per iteration on an LCG's high bits; 9..12 instructions per iteration
; (exact dynamic count from the model). In: rcx=N rax=LCG state rsi=multiplier rdi=increment r8,r9,r10=0.
bits 64
l:  imul rax, rsi
    add rax, rdi
    mov rdx, rax
    shr rdx, 40
    test edx, 1
    jz a
    add r8, rdx
    jmp b
a:  xor r9, rdx
b:  test edx, 6
    jnz c
    inc r10
c:  dec rcx
    jnz l
