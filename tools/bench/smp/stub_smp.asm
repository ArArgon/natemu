; Multi-CPU bare-metal stub for qemu-system-x86_64 -kernel (Multiboot 1, a.out kludge: flat binary, no linker). See README.md.
; Build: nasm -f bin stub_smp.asm -o stub.bin   (smp.py patches config/bodies/memory into the image at fixed offsets)
;
; Map (identity-mapped, 4 KiB pages 0..4 MiB, present/rw/user, A/D preset so no walk ever writes):
;   0x007000  AP trampoline (copied by the BSP; SIPI vector 0x07)
;   0x100000  multiboot header, 32/64-bit boot code, GDT, trampoline source;   0x100800 capture/finish (CPL3 code)
;   0x101000 PML4  0x102000 PDPT  0x103000 PD  0x104000 PT0  0x105000 PT1
;   0x106000  config: +0 "SMPCFG01", +8 ncpu, +0x10 "ENDDUMP!", +0x18 ARRIVE(dword) +0x1c GO(dword), +0x100 dump table
;             (64 x {addr,len}), +0x800 CPU table (stride 0x100: 16 GPR qwords, rip +0x80, rflags +0x88, cpl +0x90)
;   0x107000  result slots (stride 0x100: 16 GPRs, rflags +0x80, done +0x88, cpu id +0x90, fault record +0xa0: faulted,
;             vector, error code, RIP, CR2); GS base of CPU i = its slot
;   0x108000  boot stacks (0x1000 each, top = 0x109000 + 0x1000*id; after start-up CPU i's TSS.RSP0)
;   0x10c000  IDT (32 interrupt gates), its 32 entry stubs (16 bytes each) and the fault handler; TSS i at 0x10e000 + 0x80*i
;   0x110000.. bodies and data (python-patched; RAM beyond the image is zero)
; Flow, every CPU: real mode (AP) -> 32-bit flat -> PAE/LME/PG -> 64-bit CPL0 -> GS base := own slot -> arrive on the boot
; barrier (BSP wakes the APs with x2APIC INIT + SIPI x2 first, then releases GO when all have arrived) -> iretq to CPL3 (or
; CPL0 when its cpl is 0) at the CPU's rip with its GPRs (IOPL=3, IF=0). A body "falls through" at its end into a 5-byte
; `jmp capture` that python appends. Control state at the body's start: CR0 = PE|ET|WP|PG (0x80010011), CR4 = PAE,
; EFER = LME|LMA|NXE (0xd00). An exception (vector 0..31) records the fault (the GPRs and RFLAGS as the faulting
; instruction found them, vector, error code, RIP, CR2) and goes to the capture, after restoring CR0/CR3/CR4/EFER to
; the values above.
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
TSSADDR equ 0x10e000
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
        mov eax, 0x20                       ; PAE (nothing else)
        mov cr4, eax
        mov eax, 0x101000
        mov cr3, eax
        mov ecx, 0xC0000080                 ; EFER
        mov eax, 0x900                      ; LME | NXE (nothing else)
        xor edx, edx
        wrmsr
        mov eax, 0x80010011                 ; PG | WP | ET | PE (CD, NW clear)
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
        lea eax, [rdi + 0x109]              ; own TSS: RSP0 = own boot stack top (free once the body runs)
        shl eax, 12
        mov edx, edi
        shl edx, 7
        mov [rdx + TSSADDR + 4], rax
        lea eax, [rdi * 2 + 6]              ; selector 0x30 + 16 * id
        shl eax, 3
        ltr ax
        test edi, edi
        jnz .idt
        mov ecx, 32                         ; BSP: the IDT's gates, gate i -> stub i (cs 0x08, interrupt gate, DPL0)
        mov ebx, IDT
        mov eax, fault_stubs
.gate:  mov edx, eax
        and edx, 0xffff
        or edx, 0x80000
        mov [rbx], edx
        mov edx, eax
        and edx, 0xffff0000
        or edx, 0x8e00
        mov [rbx + 4], edx
        mov qword [rbx + 8], 0
        add eax, 16
        add ebx, 16
        dec ecx
        jnz .gate
.idt:   lidt [abs idtr]
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
        mov ecx, 0x1b                       ; SS, CS: user data and code (RPL3), or kernel ones for cpl 0
        mov edx, 0x23
        cmp qword [r15 + 0x90], 0
        jne .user
        mov ecx, 0x10
        mov edx, 0x08
.user:  push rcx                            ; SS
        push qword [r15 + 8*4]              ; RSP
        mov rax, [r15 + 0x88]
        and eax, 0x8d5
        or eax, 0x3002                      ; IOPL=3, IF=0
        push rax
        push rdx                            ; CS
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
%assign i 0
%rep 4                                      ; 0x30 + 16*i: TSS i (64-bit available TSS, 0x68 bytes at TSS + 0x80*i)
        dq 0x67 | (((TSSADDR + 0x80*i) & 0xffff) << 16) | ((((TSSADDR + 0x80*i) >> 16) & 0xff) << 32) | (0x89 << 40) | ((((TSSADDR + 0x80*i) >> 24) & 0xff) << 56)
        dq 0
%assign i i+1
%endrep
gdt_end:
gdtr:   dw gdt_end - gdt - 1
        dd gdt
idtr:   dw 32*16 - 1
        dq IDT
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
done:   mov qword [gs:abs 0x88], 1          ; done (TSO: after the register stores)
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
        times 0xc000-($-$$) db 0            ; config, results, stacks: zero
IDT:    times 32*16 db 0                    ; 0x10c000, filled by the BSP
fault_stubs:                                ; stub i: push 0 (vectors without an error code), push i, jmp fault
%assign i 0
%rep 32
%if i != 8 && !(i >= 10 && i <= 14) && i != 17 && i != 21 && i != 29 && i != 30
        push 0
%endif
        push i
        jmp fault
        times fault_stubs+16*(i+1)-$ db 0xcc
%assign i i+1
%endrep
fault:                                      ; CPL0, stack: vector, error, rip, cs, rflags, rsp, ss
%assign i 0
%rep 16
%if i != 4
        mov [gs:abs 8*i], %[reg_%+i]
%endif
%assign i i+1
%endrep
        mov rax, [rsp + 40]
        mov [gs:abs 8*4], rax               ; RSP before the exception
        mov rax, [rsp + 32]
        mov [gs:abs 0x80], rax              ; RFLAGS
        mov qword [gs:abs 0xa0], 1
        mov rax, [rsp]
        mov [gs:abs 0xa8], rax              ; vector
        mov rax, [rsp + 8]
        mov [gs:abs 0xb0], rax              ; error code
        mov rax, [rsp + 16]
        mov [gs:abs 0xb8], rax              ; RIP
        mov rax, cr2
        mov [gs:abs 0xc0], rax
        mov eax, 0x20                       ; the start-up controls (the body may have changed them)
        mov cr4, rax
        mov eax, 0x101000
        mov cr3, rax
        mov eax, 0x80010011
        mov cr0, rax
        mov ecx, 0xC0000080
        mov eax, 0xd00
        xor edx, edx
        wrmsr
        jmp done
        times 0xe000-($-$$) db 0
        times 0x200 db 0                    ; 0x10e000: TSS i at +0x80*i (RSP0 at +4, written at start-up)
        times 0x10000-($-$$) db 0           ; bodies start at 0x110000
