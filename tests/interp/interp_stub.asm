; The boot-time interpreter's reference rig (docs/restructure/interp-cases.md): a variant of the SMP stub
; (tools/bench/smp/stub_smp.asm) for one CPU. A Multiboot-1 flat image (a.out kludge) that QEMU loads with -kernel, TCG or
; KVM alike. tests/interp/interp_rig.py appends the case table at TABLE; this file is only the stub.
;
; The stub's home is 32-bit protected mode, paging off, CPL 0 (Multiboot's entry state, flat). For each case it copies
; the common blocks and the case's memory into place, loads the case's state from its record, enters the case's mode and
; jumps to its first instruction. The case's code ends in a capture blob (one per landing point, built by interp_rig.py
; for the mode the case ends in), which saves the final state into REC without touching the case's memory and comes back
; home; home prints the record and the case's memory windows on COM1 as one line, then runs the next case.
;
;   R    real mode: home -> 16-bit protected code (0x18) -> PE clear -> real-mode setup (rm_setup) -> far JMP to the case
;   P16  PE set, the CS cache stale from real mode: as R, but rm_setup leaves EAX = the case's CR0 and jumps to a prologue
;        the generator put just before the case's first instruction, in the case's own code segment: MOV CR0,EAX (PE),
;        POPFD (the case's EFLAGS, which its memory holds just below its SP), MOV EAX,[CS:slot] (the case's EAX)
;   P32  protected mode, paging off: the case's control registers, tables and EFLAGS, segments, GPRs, far JMP
;   C32  as P32, with CR4.PAE, CR3 and EFER.LME written before CR0.PG: CR0 then activates IA-32e mode in the stub's 32-bit
;        code segment, so the case starts in compatibility mode
;
; Map (physical = linear; the case area and the tables are the generator's, interp_cases.py):
;   0x00000  real-mode IVT: vectors 0-31 -> the fault stubs in LOW     0x00500  PB, the entering case's state
;   0x01000  the case GDT (a common block)   0x02000 IDT32   0x03000 IDT64 (32 gates each, -> the fault stubs)
;   0x06000  stub stack top                  0x06F00 REC, the capture record (0x60 bytes)
;   0x07000  LOW, the 16-bit helper (real mode CS 0x0700, protected mode selector 0x18)
;   0x08000  the alternate GDT (a common block)          0x10000-0x8FFFF  the case area
;   0x90000  the 4-level tables (common blocks)          0x100000 this stub, 0x110000 TABLE
; An exception anywhere (stub or case) runs its fault stub: OUT 0xF4 of 0x40 | vector (isa-debug-exit: QEMU exits with
; (value << 1) | 1); the last line printed names the last case that completed. The end is OUT 0xF4 of 0 (exit status 1).
PB      equ 0x500
GDT     equ 0x1000
IDT32   equ 0x2000
IDT64   equ 0x3000
RSTACK  equ 0x6000
REC     equ 0x6F00
REC_SIZE equ 0x60
LOW     equ 0x7000
TABLE   equ 0x110000
COM1    equ 0x3f8
; PB (and the first 0x5C bytes of a case record): the entering state
PB_GPR  equ PB+0x00             ; EAX ECX EDX EBX ESP EBP ESI EDI (ESP: the entry ESP)
PB_EFL  equ PB+0x20
PB_SEG  equ PB+0x24             ; ES CS SS DS FS GS selectors (u16)
PB_CR0  equ PB+0x30
PB_CR3  equ PB+0x34
PB_CR4  equ PB+0x38
PB_EFER equ PB+0x3C             ; low, high
PB_GDTR equ PB+0x48             ; limit u16, base u32
PB_IDTR equ PB+0x50
PB_EIP  equ PB+0x58             ; the entry EIP (P16: the prologue's)
PB_MODE equ PB+0x5C             ; 0 R, 1 P16, 2 P32, 3 C32
C_MEM   equ 0x60                ; a case record's memory list: n, then n x {addr, len, src (offset from TABLE)}
M_P16   equ 1
M_P32   equ 2
%define LOWOFF(x) ((x) - low_src)

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
        times 0x20-($-$$) db 0
v_home: jmp home_report                     ; 0x100020: 16- and 32-bit protected-mode captures come here (CS 0x08)
        times 0x28-($-$$) db 0xcc
v_c32:  jmp c32_back                        ; 0x100028: compatibility-mode and 64-bit captures (CS 0x08)
        times 0x30-($-$$) db 0xcc
p32_fp: dd 0, 0                             ; 0x100030: the P32/C32 entry's far pointer (m16:32)
l64_fp: dd v_c32, 0x08                      ; 0x100038: the 64-bit captures' way back (m16:32)
        db "IRSTUB01"                       ; 0x100040
        times 0x50-($-$$) db 0

entry32:                                    ; Multiboot: protected mode, flat, paging off, IF = 0
        cli
        cld
        lgdt [boot_gdtr]
        jmp 0x08:.flat
.flat:  mov ax, 0x10
        mov ds, ax
        mov es, ax
        mov ss, ax
        mov fs, ax
        mov gs, ax
        mov esp, RSTACK
        mov al, 0xff                        ; mask both PICs: nothing interrupts a case that runs with IF = 1
        out 0x21, al
        out 0xa1, al
        mov esi, low_src
        mov edi, LOW
        mov ecx, low_end - low_src
        rep movsb
        xor edi, edi                        ; IVT: vector i -> 0x0700:fault stub i
        mov ecx, 32
        mov eax, ((LOW >> 4) << 16) | LOWOFF(rm_faults)
.ivt:   stosd
        add eax, 8
        loop .ivt
        mov edi, IDT32                      ; 32-bit interrupt gates, CS 0x08
        mov ebx, LOW + LOWOFF(rm_faults)
        mov ecx, 32
.g32:   mov eax, ebx
        and eax, 0xffff
        or eax, 0x00080000
        stosd
        mov eax, ebx
        and eax, 0xffff0000
        or eax, 0x8e00
        stosd
        add ebx, 8
        loop .g32
        mov edi, IDT64                      ; 64-bit interrupt gates, CS 0x28 (the fault stubs' bytes mean the same there)
        mov ebx, LOW + LOWOFF(rm_faults)
        mov ecx, 32
.g64:   mov eax, ebx
        and eax, 0xffff
        or eax, 0x00280000
        stosd
        mov eax, ebx
        and eax, 0xffff0000
        or eax, 0x8e00
        stosd
        xor eax, eax
        stosd
        stosd
        add ebx, 8
        loop .g64
        cmp dword [TABLE], 'IRCA'
        jne .bad
        cmp dword [TABLE+4], 'SES1'
        je .ok
.bad:   mov al, 0x7e
        out 0xf4, al
.ok:    mov esi, msg_start
        call puts
        mov dword [cur], 0

next_case:
        mov eax, [cur]
        cmp eax, [TABLE+8]
        jae finish
        mov esi, [TABLE+16+eax*4]
        add esi, TABLE
        mov [crec], esi
        mov edi, REC
        mov ecx, REC_SIZE / 4
        xor eax, eax
        rep stosd
        mov ebx, [TABLE+12]                 ; the common blocks
        add ebx, TABLE
        call copy_blocks
        mov ebx, [crec]                     ; the case's own blocks
        add ebx, C_MEM
        call copy_blocks
        mov esi, [crec]
        mov edi, PB
        mov ecx, C_MEM / 4
        rep movsd
        cmp byte [PB_MODE], M_P32
        jae enter32
        mov eax, [PB_EIP]                   ; R, P16: the real-mode far pointer (m16:16) in LOW
        mov [LOW + LOWOFF(rm_fp)], ax
        mov ax, [PB_SEG+2]
        mov [LOW + LOWOFF(rm_fp) + 2], ax
        jmp 0x18:LOWOFF(pm16_to_rm)

enter32:                                    ; P32 and C32
        mov eax, [PB_EIP]
        mov [p32_fp], eax
        movzx eax, word [PB_SEG+2]
        mov [p32_fp+4], eax
        mov eax, [PB_CR4]
        mov cr4, eax
        mov ecx, 0xC0000080
        mov eax, [PB_EFER]
        and eax, ~0x400                     ; LMA is the processor's to set
        mov edx, [PB_EFER+4]
        wrmsr
        mov eax, [PB_CR3]
        mov cr3, eax
        mov eax, [PB_CR0]
        mov cr0, eax                        ; C32: PG with LME, compatibility mode from here on
        lgdt [PB_GDTR]
        lidt [PB_IDTR]
        push dword [PB_EFL]
        popfd
        mov es, [PB_SEG+0]
        mov fs, [PB_SEG+8]
        mov gs, [PB_SEG+10]
        mov ss, [PB_SEG+4]
        mov esp, [PB_GPR+16]
        mov eax, [PB_GPR+0]
        mov ecx, [PB_GPR+4]
        mov edx, [PB_GPR+8]
        mov ebx, [PB_GPR+12]
        mov ebp, [PB_GPR+20]
        mov esi, [PB_GPR+24]
        mov edi, [PB_GPR+28]
        mov ds, [PB_SEG+6]
        jmp far [cs:p32_fp]

c32_back:                                   ; compatibility mode, CS 0x08: leave IA-32e mode
        mov eax, cr0
        and eax, 0x7fffffff
        mov cr0, eax
home_report:                                ; protected mode, CS 0x08, paging off; nothing else known
        mov ax, 0x10
        mov ds, ax
        mov es, ax
        mov ss, ax
        mov fs, ax
        mov gs, ax
        mov esp, RSTACK
        push dword 2                        ; EFLAGS as the stub needs them: DF and IF clear, IOPL 0, no AC, NT, ID
        popfd
        mov eax, 0x11                       ; PE | ET
        mov cr0, eax
        xor eax, eax
        mov cr4, eax
        mov cr3, eax
        mov ecx, 0xC0000080
        xor edx, edx
        wrmsr
        lgdt [boot_gdtr]
        lidt [idt32_r]
        mov al, 'C'
        call putc
        mov al, ' '
        call putc
        mov eax, [cur]
        call hex32
        mov al, ' '
        call putc
        mov esi, REC
        mov ecx, REC_SIZE
        call hexbytes
        mov ebx, [crec]                     ; the windows: after the memory list
        mov eax, [ebx + C_MEM]
        lea eax, [eax + eax * 2]
        lea ebx, [ebx + C_MEM + 4 + eax * 4]
        mov edx, [ebx]                      ; n windows x {addr, len}
        add ebx, 4
.w:     test edx, edx
        jz .done
        mov al, ' '
        call putc
        mov esi, [ebx]
        mov ecx, [ebx+4]
        call hexbytes
        add ebx, 8
        dec edx
        jmp .w
.done:  mov al, 10
        call putc
        inc dword [cur]
        jmp next_case

finish: mov esi, msg_end
        call puts
        mov eax, [cur]
        call hex32
        mov al, 10
        call putc
        xor eax, eax
        out 0xf4, al
.h:     hlt
        jmp .h

copy_blocks:                                ; ebx -> {n, n x {addr, len, src}}
        mov edx, [ebx]
        add ebx, 4
.n:     test edx, edx
        jz .e
        mov edi, [ebx]
        mov ecx, [ebx+4]
        mov esi, [ebx+8]
        add esi, TABLE
        rep movsb
        add ebx, 12
        dec edx
        jmp .n
.e:     ret

puts:   lodsb
        test al, al
        jz .e
        call putc
        jmp puts
.e:     ret
hexbytes:                                   ; esi, ecx bytes as hex (ecx may be 0)
        jecxz .e
.n:     lodsb
        call hex8
        loop .n
.e:     ret
hex32:  push eax
        shr eax, 24
        call hex8
        mov eax, [esp]
        shr eax, 16
        call hex8
        mov eax, [esp]
        shr eax, 8
        call hex8
        pop eax
hex8:   push eax
        shr al, 4
        call .nib
        pop eax
.nib:   and al, 15
        add al, '0'
        cmp al, '9'
        jbe putc
        add al, 'a' - '9' - 1
putc:   push edx
        push eax
        mov dx, COM1 + 5
.w:     in al, dx
        test al, 0x20                       ; transmitter holding register empty
        jz .w
        pop eax
        mov dx, COM1
        out dx, al
        pop edx
        ret

msg_start: db "IRSTART", 10, 0
msg_end:   db "END ", 0
align 4
cur:    dd 0
crec:   dd 0
idt32_r: dw 32*8 - 1
        dd IDT32
align 8
boot_gdt:                                   ; the stub's own descriptors (the case GDT's first six, interp_cases.py)
        dq 0
        dq 0x00CF9B000000FFFF               ; 0x08 code32 flat
        dq 0x00CF93000000FFFF               ; 0x10 data32 flat
        dq 0x00009B007000FFFF               ; 0x18 code16, base LOW
        dq 0x000093000000FFFF               ; 0x20 data16, base 0, limit 64 KiB
        dq 0x00AF9B000000FFFF               ; 0x28 code64
boot_gdt_end:
boot_gdtr: dw boot_gdt_end - boot_gdt - 1
        dd boot_gdt

bits 16
low_src:                                    ; runs at LOW: offsets are LOWOFF(label)
l_back: jmp rm_back                         ; LOW+0: real-mode captures come here (0x0700:0)
        times 8-($-low_src) db 0xcc
rm_fp:  dw 0, 0                             ; LOW+8: the R/P16 entry's far pointer (m16:16)
        times 16-($-low_src) db 0
rm_faults:                                  ; LOW+16: fault stub i at +8i: OUT 0xF4 of 0x40 | i (the same bytes in every mode)
%assign i 0
%rep 32
        db 0xb0, 0x40 + i, 0xe6, 0xf4, 0xf4, 0xeb, 0xfd, 0xcc     ; mov al, 0x40+i; out 0xf4, al; hlt; jmp $-1
%assign i i+1
%endrep
pm16_to_rm:                                 ; 16-bit protected mode, CS 0x18
        mov ax, 0x20                        ; 64 KiB data limits, 16-bit stack
        mov ds, ax
        mov es, ax
        mov ss, ax
        mov fs, ax
        mov gs, ax
        mov eax, cr0
        and eax, 0x7ffffffe
        mov cr0, eax
        jmp 0x0700:LOWOFF(rm_setup)
rm_setup:                                   ; real mode, CS 0x0700
        xor ax, ax
        mov ds, ax
        mov ss, ax
        mov esp, RSTACK
        mov eax, [PB_CR4]
        mov cr4, eax
        mov ecx, 0xC0000080
        mov eax, [PB_EFER]
        mov edx, [PB_EFER+4]
        wrmsr
        mov eax, [PB_CR3]
        mov cr3, eax
        o32 lgdt [PB_GDTR]
        o32 lidt [PB_IDTR]
        cmp byte [PB_MODE], M_P16
        je .p16
        mov eax, [PB_CR0]
        mov cr0, eax
        push dword [PB_EFL]
        popfd
        mov es, [PB_SEG+0]
        mov fs, [PB_SEG+8]
        mov gs, [PB_SEG+10]
        mov ss, [PB_SEG+4]
        mov esp, [PB_GPR+16]
        mov eax, [PB_GPR+0]
        jmp .gpr
.p16:   mov es, [PB_SEG+0]                  ; EFLAGS come from the prologue's POPFD, EAX from its slot
        mov fs, [PB_SEG+8]
        mov gs, [PB_SEG+10]
        mov ss, [PB_SEG+4]
        mov esp, [PB_GPR+16]
        mov eax, [PB_CR0]
.gpr:   mov ecx, [PB_GPR+4]
        mov edx, [PB_GPR+8]
        mov ebx, [PB_GPR+12]
        mov ebp, [PB_GPR+20]
        mov esi, [PB_GPR+24]
        mov edi, [PB_GPR+28]
        mov ds, [PB_SEG+6]
        jmp far [cs:LOWOFF(rm_fp)]
rm_back:                                    ; real mode, from a real-mode capture
        o32 lgdt [cs:LOWOFF(low_gdtr)]
        mov eax, cr0
        or al, 1
        mov cr0, eax
        jmp dword 0x08:home_report
low_gdtr: dw boot_gdt_end - boot_gdt - 1
        dd boot_gdt
low_end:
        times 0x10000-($-$$) db 0           ; TABLE (interp_rig.py) starts at 0x110000
