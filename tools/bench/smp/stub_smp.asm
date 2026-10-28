; Multi-CPU bare-metal stub for qemu-system-x86_64 -kernel (Multiboot 1, a.out kludge: flat binary, no linker). See README.md.
; Build: nasm -f bin stub_smp.asm -o stub.bin   (smp.py patches config/bodies/memory into the image at fixed offsets)
;
; Map (identity-mapped, 4 KiB pages 0..4 MiB, present/rw/user, A/D preset so no walk ever writes):
;   0x007000  AP trampoline (copied by the BSP; SIPI vector 0x07)
;   0x100000  multiboot header, 32/64-bit boot code, GDT, trampoline source;   0x100800 capture/finish (CPL3 code)
;   0x101000 PML4  0x102000 PDPT  0x103000 PD  0x104000 PT0  0x105000 PT1
;   0x106000  config: +0 "SMPCFG01", +8 ncpu, +0x10 "ENDDUMP!", +0x18 ARRIVE(dword) +0x1c GO(dword), +0x100 dump table
;             (64 x {addr,len}), +0x800 CPU table (stride 0x100: 16 GPR qwords, rip +0x80, rflags +0x88)
;   0x107000  result slots (stride 0x100: 16 GPRs, rflags +0x80, done +0x88, cpu id +0x90); GS base of CPU i = its slot
;   0x108000  boot stacks (0x1000 each, top = 0x109000 + 0x1000*id)
;   0x110000.. bodies and data (python-patched; RAM beyond the image is zero)
; Flow, every CPU: real mode (AP) -> 32-bit flat -> PAE/LME/PG -> 64-bit CPL0 -> GS base := own slot -> arrive on the boot
; barrier (BSP wakes the APs with x2APIC INIT + SIPI x2 first, then releases GO when all have arrived) -> iretq to CPL3 at the
; CPU's rip with its GPRs (IOPL=3, IF=0). A body "falls through" at its end into a 5-byte `jmp capture` that python appends.
; capture: save GPRs/flags to the own slot through GS, set done; CPU>0 parks; CPU 0 waits for all done flags, writes the dump
; table's segments to debugcon 0xe9, then isa-debug-exit 0xf4. User code must not use FS/GS.
TRAMP   equ 0x7000
CFG     equ 0x106000
NCPU    equ CFG+8
ARRIVE  equ CFG+0x18
GO      equ CFG+0x1c
DUMPTAB equ CFG+0x100
CPUTAB  equ CFG+0x800
RESULT  equ 0x107000
%define reg_0 rax
%define reg_1 rcx
%define reg_2 rdx
%define reg_3 rbx
%define reg_4 rsp
%define reg_5 rbp
%define reg_6 rsi
%define reg_7 rdi
%define reg_8 r8
%define reg_9 r9
%define reg_10 r10
%define reg_11 r11
%define reg_12 r12
%define reg_13 r13
%define reg_14 r14
%define reg_15 r15
bits 32
org 0x100000
mb_header:
        dd 0x1BADB002
        dd 0x00010000                       ; flag bit 16: address fields valid (flat binary)
        dd -(0x1BADB002 + 0x00010000)
        dd mb_header
        dd 0x100000                         ; load_addr
        dd 0                                ; load_end_addr 0: load the whole file
        dd 0                                ; bss_end_addr
        dd entry32
entry32:                                    ; BSP only
        cli
        cld
        mov esi, tramp_src
        mov edi, TRAMP
        mov ecx, tramp_end - tramp_src
        rep movsb
        lgdt [gdtr]
        jmp dword 0x28:common32             ; reload CS with the flat 32-bit code segment
common32:                                   ; BSP and APs (APs arrive here from the trampoline)
        mov ax, 0x10
        mov ds, ax
        mov es, ax
        mov ss, ax
        xor ax, ax
        mov fs, ax                          ; null FS/GS: iretq to CPL3 keeps their bases (GS base is set below)
        mov gs, ax
        mov eax, 1
        cpuid
        shr ebx, 24                         ; initial APIC id = cpu index
        mov edi, ebx
        mov esp, 0x109000
        shl ebx, 12
        add esp, ebx
        mov eax, cr4
        or eax, 0x20                        ; PAE
        mov cr4, eax
        mov eax, 0x101000
        mov cr3, eax
        mov ecx, 0xC0000080                 ; EFER
        rdmsr
        or eax, 0x100                       ; LME
        wrmsr
        mov eax, cr0
        or eax, 0x80000001                  ; PG | PE
        mov cr0, eax
        jmp dword 0x08:long_entry

bits 16
tramp_src:                                  ; runs at TRAMP (cs:ip = 0700:0000)
        cli
        xor ax, ax
        mov ds, ax
        o32 lgdt [TRAMP + (tr_gdtr - tramp_src)]
        mov eax, cr0
        or al, 1
        mov cr0, eax
        jmp dword 0x28:common32
tr_gdtr: dw gdt_end - gdt - 1
        dd gdt
tramp_end:

bits 64
%macro delay 0
        mov r8d, 2000000
%%l:    dec r8d
        jnz %%l
%endmacro
long_entry:
        mov edi, edi
        mov ax, 0x10
        mov ds, ax
        mov es, ax
        mov al, 0xff                        ; mask both PICs
        out 0x21, al
        out 0xa1, al
        mov eax, edi
        shl eax, 8
        add eax, RESULT                     ; own result slot
        mov [rax + 0x90], rdi
        xor edx, edx
        mov ecx, 0xC0000101                 ; IA32_GS_BASE
        wrmsr
        test edi, edi
        jnz .barrier
        cmp dword [abs NCPU], 1
        je .barrier
        mov ecx, 0x1B                       ; IA32_APIC_BASE: enable x2APIC
        rdmsr
        or eax, 0xC00
        wrmsr
        mov ecx, 0x80F                      ; spurious vector register: APIC software enable
        mov eax, 0x1FF
        xor edx, edx
        wrmsr
        mov ecx, 0x830                      ; ICR: INIT, level assert, all excluding self
        xor edx, edx
        mov eax, 0xC4500
        wrmsr
        delay
%rep 2
        mov ecx, 0x830                      ; SIPI to vector TRAMP>>12, all excluding self (twice)
        xor edx, edx
        mov eax, 0xC4600 | (TRAMP >> 12)
        wrmsr
        delay
%endrep
.barrier:
        lock inc dword [abs ARRIVE]
        test edi, edi
        jnz .wait
.bw:    pause
        mov eax, [abs ARRIVE]
        cmp eax, [abs NCPU]
        jb .bw
        mov dword [abs GO], 1
.wait:  pause
        cmp dword [abs GO], 0
        je .wait
        mov r15d, edi
        shl r15d, 8
        add r15d, CPUTAB
        push 0x1b                           ; SS  (user data, RPL3)
        push qword [r15 + 8*4]              ; RSP
        mov rax, [r15 + 0x88]
        and eax, 0x8d5
        or eax, 0x3002                      ; IOPL=3, IF=0
        push rax
        push 0x23                           ; CS  (user code, RPL3)
        push qword [r15 + 0x80]             ; RIP
%assign i 0
%rep 15
%if i != 4
        mov %[reg_%+i], [r15 + 8*i]
%endif
%assign i i+1
%endrep
        mov r15, [r15 + 8*15]
        iretq

align 8
gdt:    dq 0
        dq 0x00AF9A000000FFFF               ; 0x08 code64 DPL0
        dq 0x00CF92000000FFFF               ; 0x10 data DPL0
        dq 0x00CFF2000000FFFF               ; 0x18 data DPL3
        dq 0x00AFFA000000FFFF               ; 0x20 code64 DPL3
        dq 0x00CF9A000000FFFF               ; 0x28 code32 DPL0
gdt_end:
gdtr:   dw gdt_end - gdt - 1
        dd gdt
boot_end:
        times 0x800-($-$$) db 0xcc

capture:                                    ; CPL3, entered by `jmp` from the end of every body; GS base = own slot
%assign i 0
%rep 16
        mov [gs:abs 8*i], %[reg_%+i]
%assign i i+1
%endrep
        pushfq
        pop qword [gs:abs 0x80]
        mov qword [gs:abs 0x88], 1          ; done (TSO: after the register stores)
        cmp qword [gs:abs 0x90], 0
        jne .park
        mov ebx, RESULT + 0x100
        mov ecx, [abs NCPU]
        dec ecx
        jz .dump
.w:     cmp qword [rbx + 0x88], 0
        jne .next
        pause
        jmp .w
.next:  add ebx, 0x100
        dec ecx
        jnz .w
.dump:  mov edx, 0xe9
        mov ebx, DUMPTAB
.d:     mov rcx, [rbx + 8]
        test rcx, rcx
        jz .end
        mov rsi, [rbx]
        rep outsb
        add ebx, 16
        jmp .d
.end:   xor eax, eax
        mov dl, 0xf4
        out dx, al
.park:  pause
        jmp .park
capture_end:
        times 0x1000-($-$$) db 0xcc

PML4:   dq 0x102000 | 7
        times 511 dq 0
PDPT:   dq 0x103000 | 7
        times 511 dq 0
PD:     dq 0x104000 | 7
        dq 0x105000 | 7
        times 510 dq 0
%assign a 0
%rep 1024
        dq a | 0x67                         ; P RW US A D
%assign a a+4096
%endrep
        db "SMPCFG01"                       ; 0x106000
        times 0x10000-($-$$) db 0           ; config, results, stacks: zero; bodies start at 0x110000
