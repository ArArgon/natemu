; The M2 exit test's CPU start-up, shared by the firmware (exit_fw.asm) and the 64-bit program (exit_prog.asm):
; docs/restructure/m2-exit-firmware.md. Two macros:
;
;   AP_STUB    the AP stub's page image, assembled for AP_BASE (0x8000, SIPI vector 08H) wherever it is placed: 16-bit
;              code at the SIPI entry, 32-bit code, 64-bit code, the AP's GDT, the fields the BSP fills (CR3, EFER), the
;              shared counter and one arrival byte per APIC ID: code and data on one page, on purpose (plan section 5:
;              stores to the counter land on a page other CPUs are still interpreting or have translated).
;   START_APS  64-bit code for the BSP (any address, RIP-relative): copy the stub to AP_BASE, fill its fields from the
;              BSP's own CR3 and EFER (LMA cleared), enable the x2APIC, send INIT, SIPI, SIPI to all excluding self,
;              wait for the counter (timeout from the TSC), and leave the result in
;                r12d  CPUs expected: CPUID.1:EBX[23:16] when EDX.HTT is set, else 1
;                r13d  APs arrived (the counter, read after a settle delay: an AP started twice counts twice)
;                r14d  status: 0 every AP arrived once; bit 0 timeout; bit 1 an AP arrived more than once, the BSP's own
;                      ID was marked, an ID >= 64 arrived or the counter exceeds the CPUs expected - 1; bit 2 the BSP's
;                      CR3 is above 4 GiB (the stub loads it in 32-bit mode; nothing was sent)
;                r15   bitmap of the APIC IDs that arrived exactly once (IDs 0-63)
;              Clobbers rax, rbx, rcx, rdx, rsi, rdi, r8-r11 and the direction flag (cleared). No stack.
;
; Each AP, from the SIPI (real mode, CS 0800H): enables the MTRRs first (IA32_MTRR_DEF_TYPE E = 1, FE = 0, default type
; write-back: until then all memory is UC), loads its GDT, sets PE (clearing CD and NW), far-jumps to flat 32-bit code
; (paging off), sets PAE, loads the BSP's CR3 and EFER (LME, and NXE as the BSP has it), sets PG (compatibility mode),
; far-jumps to 64-bit code, adds one to its arrival byte and to the counter (LOCK INC, RIP-relative: only 64-bit
; execution reaches them), and parks (cli; hlt loop).
; The 16- and 32-bit parts use only the boot-time interpreter's first-version instructions (plan section 1);
; tests/boot/test_exit_fw.py checks each instruction there. No port I/O outside 64-bit code.

AP_BASE         equ 0x8000                      ; the stub page (SIPI vector AP_BASE >> 12)
AP_SEL64        equ 0x08                        ; the AP's GDT: 64-bit code, 32-bit code, flat data
AP_SEL32        equ 0x10
AP_SELDATA      equ 0x18
INIT_DELAY      equ 10000000                    ; TSC cycles after INIT (10 ms at natemu's 1 GHz TSC)
SIPI_DELAY      equ 200000                      ; after each SIPI (200 us at 1 GHz)
SETTLE          equ 20000000                    ; after the last expected arrival, before the tally (20 ms at 1 GHz)
TIMEOUT         equ 0x400000000                 ; for the arrivals (17 s at 1 GHz)
%define AP(x)   ((x) - ap_src + AP_BASE)        ; a stub label's address once copied to AP_BASE

%macro AP_STUB 0
        align 16
ap_src:                                         ; = AP_BASE: the SIPI entry, CS 0800H, IP 0
        bits 16
ap16:   cli
        mov ecx, 0x2FF                          ; IA32_MTRR_DEF_TYPE: E, default type WB (6)
        mov eax, 0x806
        xor edx, edx
        wrmsr
        mov ax, cs
        mov ds, ax
        o32 lgdt [ap_gdtr - ap_src]
        mov eax, cr0
        and eax, 0x9FFFFFFF                     ; CD and NW clear
        or al, 1                                ; PE
        mov cr0, eax
        jmp dword AP_SEL32:AP(ap32)             ; P16: far JMP ptr16:32 under the real-mode CS
ap16_end:
        bits 32
ap32:   mov ax, AP_SELDATA
        mov ds, ax
        mov es, ax
        mov ss, ax
        mov eax, cr4
        or eax, 0x20                            ; PAE
        mov cr4, eax
        mov eax, [AP(ap_cr3)]
        mov cr3, eax
        mov ecx, 0xC0000080                     ; EFER: the BSP's, LMA clear
        mov eax, [AP(ap_efer)]
        xor edx, edx
        wrmsr
        mov eax, cr0
        or eax, 0x80000000                      ; PG: compatibility mode
        mov cr0, eax
        jmp AP_SEL64:AP(ap64)                   ; C32: far JMP ptr16:32 to the 64-bit CS
ap32_end:
        bits 64
ap64:   mov eax, 1
        cpuid
        shr ebx, 24                             ; the initial APIC ID
        lea rax, [rel ap_arrivals]              ; RIP-relative (position-independent): run as 32-bit code these bytes
        lock inc byte [rax + rbx]               ; address low memory instead, so an AP that is not in 64-bit mode
        lock inc dword [rel ap_count]           ; never counts and the BSP times out
.park:  cli
        hlt
        jmp .park
        align 8
ap_gdt: dq 0
        dq 0x00AF9B000000FFFF                   ; 08H: 64-bit code (accessed: no descriptor write on this page)
        dq 0x00CF9B000000FFFF                   ; 10H: 32-bit code
        dq 0x00CF93000000FFFF                   ; 18H: flat data
ap_gdt_end:
ap_gdtr: dw ap_gdt_end - ap_gdt - 1
        dd AP(ap_gdt)
        align 4
ap_cr3: dd 0                                    ; filled by the BSP
ap_efer: dd 0
ap_count: dd 0                                  ; the shared counter
        align 16
ap_arrivals: times 256 db 0                     ; per initial APIC ID
ap_end:
%endmacro

AP_CR3          equ AP(ap_cr3)
AP_EFER         equ AP(ap_efer)
AP_COUNT        equ AP(ap_count)
AP_ARRIVALS     equ AP(ap_arrivals)

; rax := TSC (rdx, rax clobbered)
%macro TSC 0
        rdtsc
        shl rdx, 32
        or rax, rdx
%endmacro

; busy-wait %1 TSC cycles (rax, rdx, r8 clobbered)
%macro TSC_DELAY 1
        TSC
        mov r8, rax
%%wait: pause
        TSC
        sub rax, r8
        cmp rax, %1
        jb %%wait
%endmacro

; an x2APIC ICR write: %1 the low word (destination ignored: shorthand). A WRMSR to an x2APIC register is not
; serializing (SDM Vol.3A 11.12.3): the fences make the stub and its fields visible before the IPI.
%macro ICR 1
        mfence
        lfence
        mov ecx, 0x830
        xor edx, edx
        mov eax, %1
        wrmsr
%endmacro

%macro START_APS 0
        bits 64
        cld
        lea rsi, [rel ap_src]                   ; the stub to its page
        mov edi, AP_BASE
        mov ecx, ap_end - ap_src
        rep movsb
        mov eax, 1                              ; CPUs expected; r9d := the BSP's initial APIC ID
        cpuid
        mov r9d, ebx
        shr r9d, 24
        mov r12d, 1
        bt edx, 28                              ; HTT
        jnc %%counted
        mov r12d, ebx
        shr r12d, 16
        and r12d, 0xFF
%%counted:
        xor r13d, r13d
        xor r15d, r15d
        mov r14d, 4
        mov rax, cr3
        mov rdx, rax
        shr rdx, 32
        jnz %%end                               ; the stub loads CR3 in 32-bit mode
        mov [abs AP_CR3], eax
        mov ecx, 0xC0000080
        rdmsr
        and eax, ~0x400                         ; EFER without LMA
        mov [abs AP_EFER], eax
        mov ecx, 0x1B                           ; IA32_APIC_BASE: x2APIC (EN and EXTD)
        rdmsr
        or eax, 0xC00
        wrmsr
        mov ecx, 0x80F                          ; SVR: software enable, spurious vector FFH
        xor edx, edx
        mov eax, 0x1FF
        wrmsr
        ICR 0xC4500                             ; INIT, level assert, all excluding self
        TSC_DELAY INIT_DELAY
        ICR 0xC4600 | (AP_BASE >> 12)           ; SIPI, all excluding self
        TSC_DELAY SIPI_DELAY
        ICR 0xC4600 | (AP_BASE >> 12)           ; the second SIPI (ignored by a started AP)
        TSC_DELAY SIPI_DELAY
        xor r14d, r14d
        mov r10d, r12d                          ; the APs expected
        dec r10d
        TSC
        mov r11, rax
%%wait: mov eax, [abs AP_COUNT]
        cmp eax, r10d
        jae %%arrived
        pause
        TSC
        sub rax, r11
        mov rcx, TIMEOUT
        cmp rax, rcx
        jb %%wait
        or r14d, 1                              ; timeout
        jmp %%tally
%%arrived:
        TSC_DELAY SETTLE
%%tally:
        mov r13d, [abs AP_COUNT]
        xor ecx, ecx                            ; each APIC ID's arrivals
%%id:   movzx eax, byte [rcx + AP_ARRIVALS]
        test eax, eax
        jz %%next
        cmp eax, 1
        jne %%bad
        cmp ecx, r9d
        je %%bad
        cmp ecx, 64
        jae %%bad
        bts r15, rcx
        jmp %%next
%%bad:  or r14d, 2
%%next: inc ecx
        cmp ecx, 256
        jb %%id
        cmp r13d, r10d
        jbe %%end
        or r14d, 2
%%end:
%endmacro
