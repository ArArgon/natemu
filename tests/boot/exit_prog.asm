; The exit test's AP part as a 64-bit program (docs/restructure/m2-exit-firmware.md): the CPU start-up slice runs it
; under natemu's program driver before the firmware boot mode exists. One body at BODY: the AP stub's image (data, copied
; to AP_BASE), then the program at BODY + 1000H (its start RIP), which runs START_APS (exit_ap.inc) at CPL 0 in 64-bit
; mode with paging on and ends at the body's end with
;   RAX  CPUs expected (CPUID)     RBX  APs arrived (the stub page's counter)     RCX  status (0: every AP arrived once)
;   RDX  bitmap of the APIC IDs that arrived once
; and every other GPR zero but RSP (never used: no stack), the flags of `cmp eax, eax`. It needs: CPL 0; the BSP's CR3
; below 4 GiB and mapping, identity, the stub page (writable and executable: no NX bit on it) and the body; EFER's NXE
; as the APs may have it (they copy the BSP's EFER); the x2APIC; the other CPUs waiting for a SIPI.
; Build: nasm -f bin -I tests/boot/ -DBODY=0x110000 -o exit_prog.bin tests/boot/exit_prog.asm
%ifdef MAPFILE
[map symbols MAPFILE]
%endif
%ifndef BODY
%define BODY 0x110000
%endif
        org BODY
        default rel
%include "exit_ap.inc"

        AP_STUB
        times 0x1000 - ($ - $$) db 0
        bits 64
start:  START_APS
        mov eax, r12d
        mov ebx, r13d
        mov ecx, r14d
        mov rdx, r15
        xor esi, esi
        xor edi, edi
        xor ebp, ebp
        xor r8d, r8d
        xor r9d, r9d
        xor r10d, r10d
        xor r11d, r11d
        xor r12d, r12d
        xor r13d, r13d
        xor r14d, r14d
        xor r15d, r15d
        cmp eax, eax
end:
