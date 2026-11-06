; Bare-metal x86-64 boot stub for qemu-system-x86_64 -kernel (Multiboot 1, a.out kludge: flat binary, no linker).
; Build: nasm -f bin -DBODY='"body.bin"' -DINIT='"init.bin"' -DBUF='"buf.bin"' stub.asm -o image.bin
;
; Image map (identity-mapped, 4 KiB pages, user-accessible, A/D preset so the page walk never writes):
;   0x100000  benchmark body (falls through into the capture sequence, same page; nothing else is stored to this page)
;   0x101000  multiboot header, 32-bit boot code, GDT          0x102000 PML4  0x103000 PDPT  0x104000 PD
;   0x105000  PT0 (0..2 MiB)   0x106000 PT1 (2..4 MiB)         0x107000 result block  0x107800 initial GPR block (16 qwords)
;   0x110000  64 KiB buffer (mixed benchmark)    0x200000 1 MiB array (not in image, RAM is zero)    0x300000 stack, 8 KiB
; Flow: multiboot 32-bit entry -> PAE, LME, paging -> far jump to 64-bit CPL0 -> mask the PIC -> iretq to CPL3 (IOPL=3 so the
; guest can OUT) with the initial GPRs -> body -> capture 16 GPRs -> OUT 0xe9 (debugcon) x128 -> OUT 0xf4 (isa-debug-exit).
; Interrupts: IF=0 throughout, no IDT.
%ifndef BODY
%error "define BODY INIT BUF"
%endif
bits 64
org 0x100000
RESULT equ 0x107000
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
INITB  equ 0x107800
body:   incbin BODY
capture:
%assign i 0
%rep 16
        mov [abs RESULT + 8*i], %[reg_%+i]
%assign i i+1
%endrep
        mov esi, RESULT
        mov ecx, 128
        mov edx, 0xe9
        rep outsb
        xor eax, eax
        mov dl, 0xf4
        out dx, al
        jmp $
capture_end:
        times 0x1000-($-$$) db 0xcc

mb_header:
        dd 0x1BADB002
        dd 0x00010000                       ; flag bit 16: address fields valid (flat binary)
        dd -(0x1BADB002 + 0x00010000)
        dd mb_header                        ; header_addr
        dd 0x100000                         ; load_addr
        dd image_end                        ; load_end_addr
        dd 0                                ; bss_end_addr
        dd entry32
bits 32
entry32:
        cli
        mov esp, 0x10f000
        mov eax, cr4
        or eax, 0x20                        ; PAE
        mov cr4, eax
        mov eax, 0x102000
        mov cr3, eax
        mov ecx, 0xC0000080                 ; EFER
        rdmsr
        or eax, 0x100                       ; LME
        wrmsr
        mov eax, cr0
        or eax, 0x80000001                  ; PG | PE
        mov cr0, eax
        lgdt [gdtr]
        jmp dword 0x08:long_entry
bits 64
long_entry:
        mov ax, 0x10
        mov ds, ax
        mov es, ax
        mov al, 0xff                        ; mask both PICs (no interrupts anyway)
        out 0x21, al
        out 0xa1, al
        push 0x1b                           ; SS  (user data, RPL3)
        push qword [abs INITB + 8*4]        ; RSP
        push 0x3002                         ; RFLAGS: IOPL=3, IF=0
        push 0x23                           ; CS  (user code, RPL3)
        push 0x100000                       ; RIP
%assign i 0
%rep 16
%if i != 4
        mov %[reg_%+i], [abs INITB + 8*i]
%endif
%assign i i+1
%endrep
        iretq


align 8
gdt:    dq 0
        dq 0x00AF9A000000FFFF               ; 0x08 code64 DPL0
        dq 0x00CF92000000FFFF               ; 0x10 data DPL0
        dq 0x00CFF2000000FFFF               ; 0x18 data DPL3
        dq 0x00AFFA000000FFFF               ; 0x20 code64 DPL3
gdtr:   dw $-gdt-1
        dd gdt
        times 0x1000-($-mb_header) db 0

PML4:   dq 0x103000 | 7
        times 511 dq 0
PDPT:   dq 0x104000 | 7
        times 511 dq 0
PD:     dq 0x105000 | 7
        dq 0x106000 | 7
        times 510 dq 0
%assign a 0
%rep 1024
        dq a | 0x67                         ; P RW US A D
%assign a a+4096
%endrep
        times 0x7800-($-$$) db 0            ; 0x107000 result block
        incbin INIT                         ; 0x107800
        times 0x10000-($-$$) db 0           ; to 0x110000
        incbin BUF
image_end:
