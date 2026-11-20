; A small static i386 Linux program (M2 task 3b): `nasm -f bin` makes the whole ELF file, no libc. It checks, in order,
; and exits with the number of the first check that fails (42 when every one passes, after printing natemu-i386-ok):
;  2 the kernel's SYSENTER entry (__kernel_vsyscall from AT_SYSINFO) writes a line; INT 0x80 wrote the one before
;  3 thread-local data: set_thread_area, GS loaded with its selector, a store and load through GS
;  4 PUSH/POP of segment registers (DS to ES, GS to FS) and a load through FS
;  5 PUSHAD/POPAD, ARPL, BOUND within bounds, LDS
;  6 x87: FILD, FSQRT, FIMUL, FISTP (sqrt(2) * 10^6 rounds to 1414214), FNSAVE/FRSTOR
;  7 BOUND out of bounds (#BR), INTO with OF set (#OF): SIGSEGV each; the handler moves EIP 2 bytes on (past BOUND,
;    past the NOP after INTO) and returns through sigreturn
;  8 AAM 0 (#DE): SIGFPE, the same
;  9 SIGUSR1 from kill(getpid()): its handler runs, sigreturn back
; 10 CALL far through m16:32 to the program's own code segment, RET far
bits 32
org 0x08048000
ehdr:
    db 0x7f, 'ELF', 1, 1, 1, 0
    times 8 db 0
    dw 2, 3                                    ; ET_EXEC, EM_386
    dd 1, start, phdr - ehdr, 0, 0
    dw 52, 32, 2, 0, 0, 0
phdr:
    dd 1, 0, ehdr, ehdr, file_end - ehdr, file_end - ehdr, 7, 0x1000
    dd 0x6474e551, 0, 0, 0, 0, 0, 6, 16       ; PT_GNU_STACK: a stack that is not executable

%define SYS_exit 1
%define SYS_write 4
%define SYS_getpid 20
%define SYS_kill 37
%define SYS_sigaction 67
%define SYS_sigreturn 119
%define SYS_set_thread_area 243
%define SA_RESTORER 0x04000000

%macro fail_unless 2                           ; jcc-not condition, check number
    j%+1 %%ok
    mov ebx, %2
    jmp exit
%%ok:
%endmacro

start:
    mov esi, esp                               ; argc, argv..., 0, envp..., 0, auxv
    lodsd
    lea esi, [esi + 4 * eax + 4]
.env:
    lodsd
    test eax, eax
    jnz .env
.aux:
    lodsd
    mov edx, eax
    lodsd
    test edx, edx
    jz .aux_end
    cmp edx, 32                                ; AT_SYSINFO
    jne .aux
    mov [sysinfo], eax
.aux_end:
    mov eax, SYS_write
    mov ebx, 1
    mov ecx, line_int80
    mov edx, line_int80_len
    int 0x80
    cmp dword [sysinfo], 0
    fail_unless ne, 2
    mov eax, SYS_write
    mov ebx, 1
    mov ecx, line_sysenter
    mov edx, line_sysenter_len
    call [sysinfo]
    cmp eax, line_sysenter_len
    fail_unless e, 2

    mov dword [desc + 4], tls                  ; set_thread_area: entry -1 (any), base tls, 4 GiB, 32-bit, usable
    mov eax, SYS_set_thread_area
    mov ebx, desc
    int 0x80
    test eax, eax
    fail_unless z, 3
    mov eax, [desc]
    lea eax, [eax * 8 + 3]
    mov gs, eax
    mov dword [gs:4], 0x1234
    cmp dword [tls + 4], 0x1234
    fail_unless e, 3
    cmp dword [gs:4], 0x1234
    fail_unless e, 3

    push ds
    pop es
    mov eax, es
    mov ecx, ds
    cmp eax, ecx
    fail_unless e, 4
    push gs
    pop fs
    cmp dword [fs:4], 0x1234
    fail_unless e, 4

    mov eax, 0x11
    mov ecx, 0x22
    mov edx, 0x33
    mov ebx, 0x44
    mov ebp, 0x66
    mov esi, 0x77
    mov edi, 0x88
    pushad
    xor eax, eax
    xor ecx, ecx
    xor edx, edx
    xor ebx, ebx
    xor ebp, ebp
    xor esi, esi
    xor edi, edi
    popad
    add eax, ecx
    add eax, edx
    add eax, ebx
    add eax, ebp
    add eax, esi
    add eax, edi
    cmp eax, 0x11 + 0x22 + 0x33 + 0x44 + 0x66 + 0x77 + 0x88
    fail_unless e, 5
    mov eax, 0x18
    mov ecx, 3
    arpl ax, cx
    fail_unless z, 5
    cmp eax, 0x1b
    fail_unless e, 5
    mov eax, 5
    bound eax, [bounds]
    mov eax, ds
    mov [far_data + 4], ax
    lds esi, [far_data]
    cmp esi, line_int80
    fail_unless e, 5

    fninit
    fild dword [two]
    fsqrt
    fimul dword [million]
    fistp dword [result]
    cmp dword [result], 1414214
    fail_unless e, 6
    fld1
    fnsave [fpu]
    fninit
    frstor [fpu]
    fistp dword [result]
    cmp dword [result], 1
    fail_unless e, 6

    mov dword [action], skip                   ; SIGSEGV and SIGFPE: step over the 2-byte instruction
    mov eax, SYS_sigaction
    mov ebx, 11
    mov ecx, action
    xor edx, edx
    int 0x80
    test eax, eax
    fail_unless z, 7
    mov eax, 50
    mov ebx, bounds
    bound eax, [ebx]
    cmp dword [skipped], 1
    fail_unless e, 7
    mov eax, 0x7fffffff
    add eax, 1
    into
    nop
    nop
    cmp dword [skipped], 2
    fail_unless e, 7
    mov eax, SYS_sigaction
    mov ebx, 8
    mov ecx, action
    xor edx, edx
    int 0x80
    mov eax, 0x1234
    aam 0
    cmp dword [skipped], 3
    fail_unless e, 8

    mov dword [action], usr1
    mov eax, SYS_sigaction
    mov ebx, 10
    mov ecx, action
    xor edx, edx
    int 0x80
    mov eax, SYS_getpid
    int 0x80
    mov ebx, eax
    mov eax, SYS_kill
    mov ecx, 10
    int 0x80
    cmp dword [usr1_seen], 10
    fail_unless e, 9

    mov eax, cs
    mov [far_code + 4], ax
    xor eax, eax
    call far [far_code]
    cmp eax, 0x5a5a
    fail_unless e, 10

    mov eax, SYS_write
    mov ebx, 1
    mov ecx, line_ok
    mov edx, line_ok_len
    int 0x80
    mov ebx, 42
exit:
    mov eax, SYS_exit
    int 0x80

; Old-style handlers (sigaction, not SA_SIGINFO): [esp+4] the signal, the sigcontext from [esp+8]; its EIP at +56.
skip:
    add dword [esp + 8 + 56], 2
    inc dword [skipped]
    ret
usr1:
    mov eax, [esp + 4]
    mov [usr1_seen], eax
    ret
restorer:                                      ; as the kernel's __kernel_sigreturn
    pop eax
    mov eax, SYS_sigreturn
    int 0x80
far_target:
    mov eax, 0x5a5a
    retf

align 4096, db 0                               ; the data on its own page (no stores into the code's)
line_int80: db 'natemu-i386: int80', 10
line_int80_len equ $ - line_int80
line_sysenter: db 'natemu-i386: sysenter', 10
line_sysenter_len equ $ - line_sysenter
line_ok: db 'natemu-i386-ok', 10
line_ok_len equ $ - line_ok
align 4
desc: dd -1, 0, 0xfffff, 0x51                  ; entry, base, limit, seg_32bit | limit_in_pages | useable
bounds: dd 0, 10
two: dd 2
million: dd 1000000
far_data: dd line_int80
    dw 0
far_code: dd far_target
    dw 0
action: dd 0, 0, SA_RESTORER, restorer         ; handler, mask, flags, restorer
sysinfo: dd 0
skipped: dd 0
usr1_seen: dd 0
result: dd 0
align 16, db 0
tls: times 64 db 0
fpu: times 108 db 0
file_end:
