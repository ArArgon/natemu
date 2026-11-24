; call chain (return plan's in-VM kill-test, docs/restructure/return-prediction-plan.md section 6): per iteration a xorshift64
; step, then a call down a chain of DEPTH functions; each function calls the next from one of CALLERS (1 or 4) call sites,
; chosen by two bits of the step's value (rotated two bits per level), so with 4 callers the return addresses of each level
; follow the generator's period (2^64 - 1), not a short pattern. The bottom function returns at once (TRIP 0), after CPUID
; (TRIP 1: a system instruction the resident serves, an EL0-EL1 round trip) or after IN from port 0x80 (TRIP 2: an access
; the host serves, an exit from the VM; needs IOPL 3). Each level adds its number + 1 to rax on the way back, so rax is
; DEPTH (DEPTH + 1) / 2 at the top whatever the path. Instruction counts per path: the model (model.c, `chain`).
; In: rcx=N r9=xorshift state (nonzero) r8=0 (checksum) rsp=valid stack. rbx is kept.
bits 64
%ifndef DEPTH
%error "define DEPTH CALLERS TRIP"
%endif
    jmp start
%assign d 0
%rep DEPTH
%assign n d + 1
f%[d]:
%if CALLERS == 4
    mov eax, r10d
    ror r10, 2
    and eax, 3
    cmp eax, 1
    jb .s0
    je .s1
    cmp eax, 2
    je .s2
    call f%[n]
    jmp .done
.s0:
    call f%[n]
    jmp .done
.s1:
    call f%[n]
    jmp .done
.s2:
    call f%[n]
.done:
%else
    call f%[n]
%endif
    add rax, n
    ret
%assign d d + 1
%endrep
f%[DEPTH]:
%if TRIP == 1
    push rcx
    push rbx
    xor eax, eax
    cpuid
    pop rbx
    pop rcx
    xor edx, edx
%elif TRIP == 2
    mov edx, 0x80
    in al, dx
    xor edx, edx
%endif
    xor eax, eax
    ret
start:
l:  mov rax, r9
    shl rax, 13
    xor r9, rax
    mov rax, r9
    shr rax, 7
    xor r9, rax
    mov rax, r9
    shl rax, 17
    xor r9, rax
    mov r10, r9
    call f0
    add r8, rax
    dec rcx
    jnz l
