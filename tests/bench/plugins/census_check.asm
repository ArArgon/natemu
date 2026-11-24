; The census plugin's check program (tests/bench/run_census.py check): a Multiboot 1 flat binary for
; qemu-system-x86_64 -kernel. Every indirect call, indirect jmp and ret it runs, and how often, is known; the check
; compares the plugin's segment "program" (from long_entry on) with the counts below, by the addresses in the map file.
;
; Memory: identity-mapped 2 MiB pages (PD[0], PD[1]), user-accessible; PD[2] (0x400000) another mapping of the first
; 2 MiB; PD[3] and PD[4] the same, made present by the page-fault handler; KBASE (the top 2 GiB) maps the PD again, at
; CPL0: kernel addresses.
;
; Phases (counts per site in the check's EXPECTED):
;   kernel  a jmp into the KBASE alias; 7 calls through a register to one target and its ret; a jmp back
;   A       1000 calls through a register to one target (f1) and f1's ret
;   B       600 calls through memory alternating between two targets (g1, g2)
;   C       500 jmps through a register cycling over five targets (t0..t4): 4 kept, 100 overflow
;   D       10 times a recursion 12 deep by direct calls (E8): the depth-8 return model misses (finds empty) 4 of 12
;   E       prefixes: 3E FF D3 (notrack call, F2 C3 ret), 41 FF 10 (REX, F3 C3 ret), 2E FF 13 (CS, C2 00 00 ret),
;           FF 25 (RIP-relative jmp)
;   F       far call REX.W FF /3 and far ret REX.W CB twice, far jmp REX.W FF /5 once: counted, not sites
;   G       one ret at two virtual addresses of one physical page (QEMU runs one translation for both): two sites
;   H       a call whose target page is not present: the fault is at the target (the call has its target)
;   I       a jmp through memory whose operand page is not present: the jmp faults, is dropped, runs again
; then OUT 0xF4 (isa-debug-exit).
bits 32
org 0x100000
[map symbols census_check.map]
KBASE   equ 0xffffffff80000000
ALIAS   equ 0x400000
HALIAS  equ 0x600000
IALIAS  equ 0x800000
KSTACK  equ 0x1e0000
USTACK  equ 0x1f0000
%define ABS(x) ((x) - $$ + 0x100000)

mb_header:
        dd 0x1BADB002
        dd 0x00010000
        dd -(0x1BADB002 + 0x00010000)
        dd ABS(mb_header)
        dd 0x100000
        dd ABS(image_end)
        dd 0
        dd ABS(entry32)

entry32:
        cli
        mov esp, KSTACK
        mov eax, cr4
        or eax, 0x20                        ; PAE
        mov cr4, eax
        mov eax, ABS(pml4)
        mov cr3, eax
        mov ecx, 0xC0000080                 ; EFER.LME
        rdmsr
        or eax, 0x100
        wrmsr
        mov eax, cr0
        or eax, 0x80000001                  ; PG | PE
        mov cr0, eax
        lgdt [gdtr]
        jmp dword 0x08:long_entry

bits 64
long_entry:                                 ; the plugin's segment "program" starts here (mark=)
        mov ax, 0x10
        mov ds, ax
        mov es, ax
        mov ss, ax
        mov rsp, KSTACK
        mov ax, 0x28
        ltr ax
        lidt [rel idtr]
        mov rax, KBASE + kcode
L_kjmp: jmp rax
kcode:  mov rbx, KBASE + kfunc
        mov ecx, 7
kloop:
K_call: call rbx
K_call_next:
        dec ecx
        jnz kloop
        mov rax, low_cont
K_back: jmp rax
kfunc:  ret

low_cont:
        push 0x23                           ; SS (user data, RPL3)
        push USTACK
        push 0x3002                         ; RFLAGS: IOPL=3, IF=0
        push 0x1b                           ; CS (user code64, RPL3)
        push user_main
        iretq

user_main:
        lea rbx, [rel f1]                   ; A
        mov ecx, 1000
aloop:
A_call: call rbx
A_next: dec ecx
        jnz aloop

        mov ecx, 600                        ; B
        xor edx, edx
bloop:     mov eax, edx
        and eax, 1
B_call: call [rax * 8 + btable]
B_next: inc edx
        dec ecx
        jnz bloop

        mov ecx, 500                        ; C
        xor r8d, r8d
        lea r9, [rel ctable]
cloop:     mov rax, [r9 + r8 * 8]
C_jmp:  jmp rax
c_next: inc r8d
        cmp r8d, 5
        jb cskip
        xor r8d, r8d
cskip:    dec ecx
        jnz cloop

        mov r12d, 10                        ; D
dloop:     mov ecx, 12
D_call: call rec
D_next: dec r12d
        jnz dloop

        lea rbx, [rel f3]                   ; E
        mov ecx, 5
e1loop:
E1:     db 0x3e
        call rbx
E1_next:
        dec ecx
        jnz e1loop
        lea r8, [rel fptr4]
        mov ecx, 4
e2loop:
E2:     call [r8]
E2_next:
        dec ecx
        jnz e2loop
        lea rbx, [rel fptr5]
        mov ecx, 3
e3loop:
E3:     call [cs:rbx]                       ; NASM warns that CS is ignored in 64-bit mode; it emits the 2E
E3_next:
        dec ecx
        jnz e3loop
E4:     jmp [rel eptr]
e_after:

        mov ecx, 2                          ; F
floop:     call far [rel farptr]
        dec ecx
        jnz floop
        jmp far [rel farjmp]
f_after:

        lea rbx, [rel f6]                   ; G
        mov ecx, 2
g1loop:
G1:     call rbx
G1_next:
        dec ecx
        jnz g1loop
        mov rbx, ALIAS + f6
        mov ecx, 3
g2loop:
G2:     call rbx
G2_next:
        dec ecx
        jnz g2loop

        mov rbx, HALIAS + f7                ; H
H_call: call rbx
H_next:
        mov rbx, IALIAS + iptr              ; I
I_jmp:  jmp [rbx]
I_target:

        mov dx, 0xf4                        ; exit
        xor eax, eax
        out dx, al
        jmp $

rec:    dec ecx
        jz rec_done
        call rec
rec_after:
rec_done:
        ret

f1:     ret
g1:     ret
g2:     ret
t0:     jmp c_next
t1:     jmp c_next
t2:     jmp c_next
t3:     jmp c_next
t4:     jmp c_next
f3:     db 0xf2
        ret
f4:     db 0xf3
        ret
f5:     ret 0
ffar:   o64 retf
f6:     ret
f7:     ret

; #PF at CPL0: make the faulting 2 MiB region present (another mapping of the first 2 MiB), drop the error code, iretq.
pf:     push rax
        push rbx
        mov rax, cr2
        shr rax, 21
        and eax, 511
        lea rbx, [rel pd]
        mov qword [rbx + rax * 8], 0x87
        mov rax, cr2
        invlpg [rax]
        pop rbx
        pop rax
        add rsp, 8
        iretq

align 8
btable: dq g1, g2
ctable: dq t0, t1, t2, t3, t4
fptr4:  dq f4
fptr5:  dq f5
eptr:   dq e_after
iptr:   dq I_target
farptr: dq ffar                            ; m16:64 (NASM gives the far call and jmp REX.W)
        dw 0x1b
farjmp: dq f_after
        dw 0x1b

align 8
gdt:    dq 0
        dq 0x00AF9A000000FFFF               ; 0x08 code64 DPL0
        dq 0x00CF92000000FFFF               ; 0x10 data DPL0
        dq 0x00AFFA000000FFFF               ; 0x18 code64 DPL3
        dq 0x00CFF2000000FFFF               ; 0x20 data DPL3
        dw 103                              ; 0x28 TSS (16 bytes)
        dw ABS(tss) & 0xffff
        db (ABS(tss) >> 16) & 0xff
        db 0x89, 0
        db (ABS(tss) >> 24) & 0xff
        dd 0, 0
gdtr:   dw $ - gdt - 1
        dq gdt
idtr:   dw 256 * 16 - 1
        dq idt
align 16
tss:    dd 0
        dq KSTACK                           ; RSP0
        times 104 - ($ - tss) db 0

align 4096
pml4:   dq ABS(pdpt) | 7
        times 510 dq 0
        dq ABS(pdpt_hi) | 7                 ; KBASE
pdpt:   dq ABS(pd) | 7
        times 511 dq 0
pdpt_hi: times 510 dq 0
        dq ABS(pd) | 7
        dq 0
pd:     dq 0x000000 | 0x87                  ; P RW US PS
        dq 0x200000 | 0x87
        dq 0x000000 | 0x87                  ; ALIAS
        times 509 dq 0
idt:    times 14 * 16 db 0
        dw ABS(pf) & 0xffff                 ; vector 14
        dw 0x08
        db 0, 0x8E
        dw (ABS(pf) >> 16) & 0xffff
        dd 0, 0
        times 256 * 16 - ($ - idt) db 0
image_end:
