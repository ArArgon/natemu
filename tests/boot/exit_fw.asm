; The M2 exit test's firmware: a 64 KiB image for the top of the 4 GiB space (FFFF0000H-FFFFFFFFH), QEMU's -bios and,
; later, natemu's firmware boot mode (docs/restructure/m2-exit-firmware.md says what that mode must provide).
; Build: nasm -f bin -I tests/boot/ -o exit_fw.bin tests/boot/exit_fw.asm   (tests/boot/run_exit_fw.py builds it)
;
; The BSP, from the reset vector (CS F000H base FFFF0000H, IP FFF0H):
;   16-bit  enable the MTRRs first (IA32_MTRR_DEF_TYPE E = 1, FE = 0, default type WB), load the GDT through the reset
;           CS base (o32 lgdt [cs:...]), set PE (CD and NW cleared), far JMP ptr16:32 under the stale real-mode CS;
;   32-bit  flat, paging off: identity page tables for 4 GiB at PT (2 MiB pages), PAE, CR3, EFER.LME, PG, far JMP to
;           the 64-bit CS (one instruction in compatibility mode);
;   64-bit  (here at the start of the image, run from the ROM) the AP start-up of exit_ap.inc, a report on COM1, power
;           off: ACPI S5 through natemu's PM1a control register (B004H), else QEMU's isa-debug-exit (F4H), else hlt.
; The report, two lines (CR LF):
;   EXITFW long mode
;   EXITFW cpus=<CPUs expected> aps=<APs arrived> ids=<APIC IDs that arrived once, comma-separated, or -> status=<0 ok>
; Memory used: AP_BASE (the AP stub page, 8000H), PT..PT+6000H (10000H), the stack below STACK (80000H). Writes nothing
; to the image (every descriptor's accessed bit is preset). 16- and 32-bit code: first-version instructions only.
%ifdef MAPFILE
[map symbols MAPFILE]
%endif
        org 0xFFFF0000
        default rel
%include "exit_ap.inc"

PT              equ 0x10000             ; PML4, PDPT, four PDs
STACK           equ 0x80000
SEL64           equ 0x08                ; = the AP stub's selectors
SEL32           equ 0x10
SELDATA         equ 0x18
COM1            equ 0x3F8
PM1A_CNT        equ 0xB004              ; natemu's ACPI PM1a control (crates/host/src/pm.rs): SLP_EN with SLP_TYP 0 = S5
DEBUG_EXIT      equ 0xF4                ; QEMU's isa-debug-exit (the reference runs): exit status 1

        bits 64
long64:                                 ; the BSP, 64-bit, CPL 0, the identity map
        mov esp, STACK
        call serial_init
        lea rsi, [rel msg_long]
        call puts
        START_APS
        call report
        mov dx, PM1A_CNT
        mov ax, 0x2000
        out dx, ax
        mov dx, DEBUG_EXIT
        xor eax, eax
        out dx, al
.park:  cli
        hlt
        jmp .park

serial_init:                            ; COM1: no interrupts, 8N1
        mov dx, COM1 + 1
        xor eax, eax
        out dx, al
        mov dx, COM1 + 3
        mov al, 3
        out dx, al
        ret

putc:                                   ; al to COM1 once THR is empty (LSR bit 5)
        push rdx
        push rax
        mov dx, COM1 + 5
.wait:  in al, dx
        test al, 0x20
        jz .wait
        pop rax
        mov dx, COM1
        out dx, al
        pop rdx
        ret

puts:                                   ; the NUL-terminated string at rsi
        lodsb
        test al, al
        jz .done
        call putc
        jmp puts
.done:  ret

putdec:                                 ; eax in decimal
        push rbx
        push rcx
        push rdx
        mov ecx, 10
        xor ebx, ebx
.digit: xor edx, edx
        div ecx
        push rdx
        inc ebx
        test eax, eax
        jnz .digit
.out:   pop rax
        add al, '0'
        call putc
        dec ebx
        jnz .out
        pop rdx
        pop rcx
        pop rbx
        ret

report:                                 ; START_APS's r12d-r15
        lea rsi, [rel msg_cpus]
        call puts
        mov eax, r12d
        call putdec
        lea rsi, [rel msg_aps]
        call puts
        mov eax, r13d
        call putdec
        lea rsi, [rel msg_ids]
        call puts
        xor ebx, ebx                    ; the bit
        xor ebp, ebp                    ; any printed
.id:    bt r15, rbx
        jnc .next
        test ebp, ebp
        jz .first
        mov al, ','
        call putc
.first: mov ebp, 1
        mov eax, ebx
        call putdec
.next:  inc ebx
        cmp ebx, 64
        jb .id
        test ebp, ebp
        jnz .status
        mov al, '-'
        call putc
.status: lea rsi, [rel msg_status]
        call puts
        mov eax, r14d
        call putdec
        lea rsi, [rel msg_eol]
        jmp puts

msg_long:   db "EXITFW long mode", 13, 10, 0
msg_cpus:   db "EXITFW cpus=", 0
msg_aps:    db " aps=", 0
msg_ids:    db " ids=", 0
msg_status: db " status=", 0
msg_eol:    db 13, 10, 0

        AP_STUB                         ; copied to AP_BASE by START_APS

        align 8
gdt:    dq 0
        dq 0x00AF9B000000FFFF           ; 08H: 64-bit code (accessed: the CPU never writes the ROM)
        dq 0x00CF9B000000FFFF           ; 10H: 32-bit code
        dq 0x00CF93000000FFFF           ; 18H: flat data
gdt_end:
gdtr:   dw gdt_end - gdt - 1
        dd gdt

        times 0xF000 - ($ - $$) db 0
        bits 16
fw16:   cli                             ; from the reset vector: real mode, CS base FFFF0000H
        cld
        mov ecx, 0x2FF                  ; IA32_MTRR_DEF_TYPE: E, default type WB (6)
        mov eax, 0x806
        xor edx, edx
        wrmsr
        o32 lgdt [cs:gdtr - $$]
        mov eax, cr0
        and eax, 0x9FFFFFFF             ; CD and NW clear
        or al, 1                        ; PE
        mov cr0, eax
        jmp dword SEL32:fw32            ; P16: far JMP ptr16:32 under the real-mode CS
fw16_end:
        bits 32
fw32:   mov ax, SELDATA                 ; flat, paging off
        mov ds, ax
        mov es, ax
        mov ss, ax
        mov edi, PT                     ; zero the PML4 and the PDPT
        xor eax, eax
        mov ecx, 2 * 1024
.zero:  mov [edi], eax
        add edi, 4
        loop .zero
        mov dword [PT], PT + 0x1000 + 3             ; PML4[0] -> the PDPT (present, writable)
        mov dword [PT + 0x1000], PT + 0x2000 + 3    ; PDPT[0..3] -> the four PDs
        mov dword [PT + 0x1008], PT + 0x3000 + 3
        mov dword [PT + 0x1010], PT + 0x4000 + 3
        mov dword [PT + 0x1018], PT + 0x5000 + 3
        mov edi, PT + 0x2000            ; 2048 PDEs: 2 MiB pages, identity (present, writable, PS)
        mov eax, 0x83
        mov ecx, 2048
.pde:   mov [edi], eax
        mov dword [edi + 4], 0
        add eax, 0x200000
        add edi, 8
        loop .pde
        mov eax, cr4
        or eax, 0x20                    ; PAE
        mov cr4, eax
        mov eax, PT
        mov cr3, eax
        mov ecx, 0xC0000080             ; EFER.LME
        rdmsr
        or eax, 0x100
        wrmsr
        mov eax, cr0
        or eax, 0x80000000              ; PG: compatibility mode
        mov cr0, eax
        jmp SEL64:long64                ; C32: far JMP ptr16:32 to the 64-bit CS
fw32_end:

        times 0xFFF0 - ($ - $$) db 0
        bits 16
reset:  jmp near fw16                   ; the reset vector, FFFFFFF0H
reset_end:
        times 0x10000 - ($ - $$) db 0
