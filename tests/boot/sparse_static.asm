; A small static x86-64 Linux program (the shadow pool's full reset on a real guest, tests/boot/run_boot_smoke.py
; `--shadow-pool-pages`): `nasm -f bin` makes the whole ELF file, no libc. It maps one page in each of N regions 1 GiB
; apart (1 GiB, 2 GiB, ... N GiB: mmap with MAP_FIXED_NOREPLACE), stores each page's address into it, then reads every
; page back ROUNDS times. Each page needs a level-2 and a leaf shadow table of its own, so one pass needs about 2N table
; pages: with N = 300 every pass overflows a pool of 285 pages. It prints natemu-sparse-ok and exits 42 when every page
; read back its own address; 1: a mapping failed or landed elsewhere, 2: a read gave another value.
bits 64
org 0x400000
%define N 300
%define ROUNDS 3
ehdr:
    db 0x7f, 'ELF', 2, 1, 1, 0
    times 8 db 0
    dw 2, 62                                   ; ET_EXEC, EM_X86_64
    dd 1
    dq start, phdr - ehdr, 0
    dd 0
    dw 64, 56, 1, 0, 0, 0
phdr:
    dd 1, 5                                    ; PT_LOAD, R+X
    dq 0, ehdr, ehdr, file_end - ehdr, file_end - ehdr, 0x1000

start:
    xor r12d, r12d                             ; i
map:
    lea rdi, [r12 + 1]
    shl rdi, 30                                ; (i + 1) GiB
    mov r13, rdi
    mov esi, 4096
    mov edx, 3                                 ; PROT_READ | PROT_WRITE
    mov r10d, 0x100022                         ; MAP_FIXED_NOREPLACE | MAP_ANONYMOUS | MAP_PRIVATE
    mov r8, -1
    xor r9d, r9d
    mov eax, 9                                 ; mmap
    syscall
    cmp rax, r13
    jne map_failed
    mov [rax], rax
    inc r12
    cmp r12, N
    jb map
    mov r15d, ROUNDS
round:
    xor r12d, r12d
    xor r14d, r14d                             ; reads that differ
read:
    lea rdi, [r12 + 1]
    shl rdi, 30
    mov rax, [rdi]
    cmp rax, rdi
    je same
    inc r14
same:
    inc r12
    cmp r12, N
    jb read
    test r14, r14
    jnz read_failed
    dec r15d
    jnz round
    mov eax, 1                                 ; write
    mov edi, 1
    lea rsi, [rel ok]
    mov edx, ok_end - ok
    syscall
    mov edi, 42
exit:
    mov eax, 60                                ; exit
    syscall
map_failed:
    mov edi, 1
    jmp exit
read_failed:
    mov edi, 2
    jmp exit
ok:
    db 'natemu-sparse-ok', 10
ok_end:
file_end:
