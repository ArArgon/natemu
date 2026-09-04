// Guest program for the INV-01-12 integrated prototype.
//
// x86-64, long mode, ring 0, flat addressing. Assembled with
//   clang -target x86_64-elf -c guest.s
// and loaded at guest-physical 0x00100000. It uses only RIP-relative and
// immediate operands so the object needs no relocations (see src/elfextract.py).
//
// It builds its own 4-level page tables and IDT, brings up a second vCPU,
// runs a concurrent locked-counter and message-passing phase, then exercises
// page faults, #UD, self-modifying code, a compare-and-exchange, an
// interrupts-disabled region and a halt/wake, and finally halts cleanly.

.intel_syntax noprefix
.text
.globl _start

.set IDT_BASE,   0x2000
.set PT_ARENA,   0x10000
.set MP_A_DATA,  0x810000
.set MP_A_FLAG,  0x818000
.set MP_B_DATA,  0x820000
.set MP_B_FLAG,  0x828000
.set FAULT_BACK, 0xD00000
.set IDMAP_END,  0xE00000
.set LAPIC,      0xFEE00000
.set ALIAS,      0x40000000

.set KLOOP,      100000          // locked increments per vCPU
.set NLOOP,      4096            // message-passing iterations per vCPU
.set NIPI,       64              // IPIs vCPU 1 sends to vCPU 0
.set SPINMAX,    20000000        // bounded spin, so a dropped event fails the
                                 // run through the checker rather than hanging
.set CLISPIN,    300000

// ---------------------------------------------------------------- entry

_start:
    mov r12, rdi                 // vCPU id
    cmp r12, 0
    jne vcpu1_start

    call build_pt
    call build_idt
    lea rax, [rip + idtr]
    lidt [rax]
    mov rax, [rip + pml4_pa]
    mov cr3, rax
    mov rax, 0x80000001          // PG | PE
    mov cr0, rax
    mov qword ptr [rip + tables_ready], 1
    jmp common

vcpu1_start:
1:  mov rax, [rip + tables_ready]
    test rax, rax
    jnz 2f
    pause
    jmp 1b
2:  lea rax, [rip + idtr]
    lidt [rax]
    mov rax, [rip + pml4_pa]
    mov cr3, rax
    mov rax, 0x80000001
    mov cr0, rax
    jmp common

// ------------------------------------------------------------ main flow

common:
    mov r15, 0                   // local barrier sense
    call barrier                 // B1: cannot be passed by a single thread
    cmp r12, 0
    jne 1f
    mov rax, LAPIC
    mov dword ptr [rax + 0x320], 0x20     // LVT timer vector
    mov dword ptr [rax + 0x380], 1        // start the host timer
1:  mov edx, 0x10
    out dx, eax                  // overlap window: begin
    call counter_loop
    call mp_loop
    mov edx, 0x11
    out dx, eax                  // overlap window: end
    call barrier                 // B2
    cmp r12, 0
    jne 2f
    call phase3
    mov qword ptr [rip + phase3_done], 1
    jmp 3f
2:  mov rax, [rip + phase3_done]
    test rax, rax
    jnz 3f
    pause
    jmp 2b
3:  call barrier                 // B3
    cmp r12, 0
    jne 4f
    mov rax, LAPIC
    mov dword ptr [rax + 0x380], 0        // stop the host timer
    sti
    mov rcx, CLISPIN
5:  pause
    sub rcx, 1
    jnz 5b                       // drain any interrupt still in flight
    mov qword ptr [rip + vcpu0_done], 1
    jmp 6f
4:  mov qword ptr [rip + vcpu1_done], 1
6:  mov edx, 0x13
    out dx, eax
    cli
7:  hlt
    jmp 7b

// --------------------------------------------------------- two-party barrier
// Uses r15 as the caller's sense. A non-preemptive single-threaded scheduler
// deadlocks here, which is why the barrier is part of the concurrency oracle.

barrier:
    mov rax, 1
    xor r15, rax
    mov rax, 1
    lea rbx, [rip + barrier_count]
    lock xadd [rbx], rax
    cmp rax, 1
    jne 1f
    mov qword ptr [rbx], 0
    lea rbx, [rip + barrier_sense]
    mov [rbx], r15
    ret
1:  lea rbx, [rip + barrier_sense]
2:  mov rax, [rbx]
    cmp rax, r15
    je 3f
    pause
    jmp 2b
3:  ret

// ------------------------------------------------------- concurrent phases

counter_loop:
    mov rcx, KLOOP
    mov r9, 1000
    mov r10, 0
1:  mov rax, 1
    lea rbx, [rip + counter]
    lock xadd [rbx], rax
    cmp r12, 0
    je 2f
    sub r9, 1
    jnz 2f
    mov r9, 1000
    cmp r10, NIPI
    jae 2f
    add r10, 1
    push rcx
    push r9
    push r10
    call send_ipi
    pop r10
    pop r9
    pop rcx
2:  sub rcx, 1
    jnz 1b
    ret

// Message passing: the writer stores data then flag, the reader spins on flag
// then loads data. Under x86-TSO the reader must never see the flag without
// the data. Each vCPU writes one channel and reads the other.
mp_loop:
    mov rcx, 0
1:  cmp rcx, NLOOP
    jae 9f
    cmp r12, 0
    je 2f
    mov rbx, MP_B_DATA
    lea rbx, [rbx + rcx*8]
    mov rax, rcx
    add rax, 1
    mov [rbx], rax
    mov rbx, MP_B_FLAG
    lea rbx, [rbx + rcx*8]
    mov qword ptr [rbx], 1
    mov rbx, MP_A_FLAG
    lea rbx, [rbx + rcx*8]
    mov rsi, MP_A_DATA
    lea rsi, [rsi + rcx*8]
    jmp 3f
2:  mov rbx, MP_A_DATA
    lea rbx, [rbx + rcx*8]
    mov rax, rcx
    add rax, 1
    mov [rbx], rax
    mov rbx, MP_A_FLAG
    lea rbx, [rbx + rcx*8]
    mov qword ptr [rbx], 1
    mov rbx, MP_B_FLAG
    lea rbx, [rbx + rcx*8]
    mov rsi, MP_B_DATA
    lea rsi, [rsi + rcx*8]
3:  mov r8, SPINMAX
4:  mov rax, [rbx]
    test rax, rax
    jnz 5f
    pause
    sub r8, 1
    jnz 4b
    lea rax, [rip + mp_timeouts]
    mov rdx, [rax]
    add rdx, 1
    mov [rax], rdx
    jmp 6f
5:  mov rax, [rsi]
    mov rdx, rcx
    add rdx, 1
    cmp rax, rdx
    je 6f
    lea rax, [rip + mp_violations]
    mov rdx, [rax]
    add rdx, 1
    mov [rax], rdx
6:  add rcx, 1
    jmp 1b
9:  cmp r12, 0
    jne 8f
    lea rax, [rip + iters0]
    mov [rax], rcx
    ret
8:  lea rax, [rip + iters1]
    mov [rax], rcx
    ret

// Send one IPI and wait for the handler's acknowledgement, so sent and
// received counts are comparable exactly instead of coalescing.
send_ipi:
    lea rbx, [rip + ipi_ack]
    mov r8, [rbx]
    add r8, 1
    mov rcx, LAPIC
    mov dword ptr [rcx + 0x300], 0x21     // ICR: target vCPU 0, vector 0x21
    mov r9, SPINMAX
1:  mov rax, [rbx]
    cmp rax, r8
    jae 2f
    pause
    sub r9, 1
    jnz 1b
    lea rax, [rip + ipi_ack_timeouts]
    mov rdx, [rax]
    add rdx, 1
    mov [rax], rdx
2:  ret

// ------------------------------------------------------------- phase three

phase3:
    call test_pf
    call test_ud
    call test_smc
    call test_cas
    call test_cli
    call test_hlt
    ret

// Three faults with three distinct error codes. Each faulting instruction's
// effect must be applied exactly once after the handler maps the page and
// IRETQ returns to the faulting instruction.
test_pf:
    mov rcx, FAULT_BACK
    mov rax, 0xA5A5A5A5
    mov [rcx], rax
    mov rcx, FAULT_BACK + 0x1000
    mov qword ptr [rcx], 0
    mov rcx, FAULT_BACK + 0x2000
    mov qword ptr [rcx], 0

    lea rax, [rip + tA]
    lea rbx, [rip + fault_log]
    mov [rbx + 24], rax
    mov rcx, ALIAS
tA: mov rax, [rcx]                        // not-present read, error code 0
    lea rbx, [rip + test_a_val]
    mov [rbx], rax

    lea rax, [rip + tB]
    lea rbx, [rip + fault_log]
    mov [rbx + 56], rax
    mov rcx, ALIAS + 0x1000
    mov rax, 1
tB: lock xadd [rcx], rax                  // not-present write, error code 2
    mov rax, [rcx]
    lea rbx, [rip + test_b_mem]
    mov [rbx], rax

    lea rax, [rip + tC]
    lea rbx, [rip + fault_log]
    mov [rbx + 88], rax
    mov rcx, ALIAS + 0x2000
    mov rax, 1
tC: lock xadd [rcx], rax                  // write to read-only, error code 3
    mov rax, [rcx]
    lea rbx, [rip + test_c_mem]
    mov [rbx], rax
    ret

test_ud:
    ud2
    ret

test_smc:
    call smc_target
    lea rbx, [rip + smc_before]
    mov [rbx], rax
    lea rcx, [rip + smc_target]
    mov dword ptr [rcx + 1], 222          // rewrite the immediate in place
    call smc_target
    lea rbx, [rip + smc_after]
    mov [rbx], rax
    ret

smc_target:
    mov eax, 111
    ret

test_cas:
    lea rbx, [rip + cas_cell]
    mov qword ptr [rbx], 0
    mov rax, 0
    mov rdx, 1
    lock cmpxchg [rbx], rdx
    mov rcx, 0
    jne 1f
    mov rcx, 1
1:  lea rsi, [rip + cas_ok]
    mov [rsi], rcx
    mov rax, 0
    mov rdx, 2
    lock cmpxchg [rbx], rdx               // must fail and load the old value
    lea rsi, [rip + cas_fail_rax]
    mov [rsi], rax
    ret

test_cli:
    cli
    lea rbx, [rip + timer_ticks]
    mov r10, [rbx]
    mov rcx, CLISPIN
1:  pause
    sub rcx, 1
    jnz 1b
    mov rax, [rbx]
    sub rax, r10
    lea rsi, [rip + cli_delta]
    mov [rsi], rax                        // must be zero
    sti
    ret

test_hlt:
    lea rbx, [rip + timer_ticks]
    mov r10, [rbx]
    sti
    hlt                                   // must be woken by a timer interrupt
    mov rax, [rbx]
    cmp rax, r10
    jbe 1f
    lea rsi, [rip + hlt_wakeups]
    mov rdx, [rsi]
    add rdx, 1
    mov [rsi], rdx
1:  ret

// ------------------------------------------------------------- handlers

timer_isr:
    push rax
    push rbx
    lea rbx, [rip + timer_ticks]
    mov rax, [rbx]
    add rax, 1
    mov [rbx], rax
    lea rbx, [rip + timer_eois]
    mov rax, [rbx]
    add rax, 1
    mov [rbx], rax
    mov rbx, LAPIC
    mov dword ptr [rbx + 0xB0], 0         // EOI
    pop rbx
    pop rax
    iretq

ipi_isr:
    push rax
    push rbx
    lea rbx, [rip + ipi_recv]
    mov rax, [rbx]
    add rax, 1
    mov [rbx], rax
    lea rbx, [rip + ipi_ack]
    mov rax, [rbx]
    add rax, 1
    mov [rbx], rax
    mov rbx, LAPIC
    mov dword ptr [rbx + 0xB0], 0
    pop rbx
    pop rax
    iretq

ud_handler:
    push rax
    push rbx
    lea rbx, [rip + ud_count]
    mov rax, [rbx]
    add rax, 1
    mov [rbx], rax
    mov rax, [rsp + 16]                   // pushed RIP
    add rax, 2                            // step over ud2
    mov [rsp + 16], rax
    pop rbx
    pop rax
    iretq

gp_handler:
    lea rbx, [rip + gp_count]
    mov rax, [rbx]
    add rax, 1
    mov [rbx], rax
    cli
1:  hlt
    jmp 1b

// #PF: log CR2, the error code and the pushed RIP, then repair the mapping the
// way a guest kernel would, invalidate, and return to the faulting instruction.
pf_handler:
    push rax
    push rbx
    push rcx
    push rdx
    push rsi
    push rdi
    push r8
    push r9
    push r10
    mov r9, [rsp + 72]                    // error code
    mov r10, [rsp + 80]                   // pushed RIP
    mov rax, cr2
    mov rdi, rax
    lea rbx, [rip + pf_count]
    mov rcx, [rbx]
    cmp rcx, 4
    jae 1f
    mov rdx, rcx
    shl rdx, 5
    lea rsi, [rip + fault_log]
    add rsi, rdx
    mov [rsi], rax
    mov [rsi + 8], r9
    mov [rsi + 16], r10
1:  add rcx, 1
    mov [rbx], rcx
    call pte_ptr                          // rax = &PTE for rdi
    mov rdx, [rax]
    or rdx, 1                             // present
    test r9, 2
    jz 2f
    or rdx, 2                             // writable, when the access was a write
2:  mov [rax], rdx
    invlpg [rdi]
    pop r10
    pop r9
    pop r8
    pop rdi
    pop rsi
    pop rdx
    pop rcx
    pop rbx
    pop rax
    add rsp, 8                            // discard the error code
    iretq

// -------------------------------------------------------- page-table build

// out: rax = physical address of a zeroed 4 KiB page. clobbers rcx, rdx.
alloc_page:
    lea rdx, [rip + arena_ptr]
    mov rax, [rdx]
    mov rcx, rax
    add rcx, 4096
    mov [rdx], rcx
    mov rcx, rax
    mov rdx, 512
1:  mov qword ptr [rcx], 0
    add rcx, 8
    sub rdx, 1
    jnz 1b
    ret

// in: r8 = table physical address, rsi = index. out: r8 = child table.
pt_next:
    lea rcx, [r8 + rsi*8]
    mov rdx, [rcx]
    test rdx, 1
    jnz 1f
    push rcx
    call alloc_page
    pop rcx
    mov rdx, rax
    or rdx, 7                             // present, writable, user
    mov [rcx], rdx
1:  mov r8, rdx
    and r8, -4096
    ret

// in: rdi = virtual address (preserved). out: rax = pointer to the PTE.
// clobbers rax, rcx, rdx, rsi, r8.
pte_ptr:
    mov r8, [rip + pml4_pa]
    mov rsi, rdi
    shr rsi, 39
    and rsi, 511
    call pt_next
    mov rsi, rdi
    shr rsi, 30
    and rsi, 511
    call pt_next
    mov rsi, rdi
    shr rsi, 21
    and rsi, 511
    call pt_next
    mov rsi, rdi
    shr rsi, 12
    and rsi, 511
    lea rax, [r8 + rsi*8]
    ret

// in: rdi = virtual address, r13 = physical address, r14 = flags.
map_page:
    call pte_ptr
    mov rdx, r13
    or rdx, r14
    mov [rax], rdx
    ret

build_pt:
    call alloc_page
    lea rcx, [rip + pml4_pa]
    mov [rcx], rax
    mov rdi, 0
    mov r14, 3                            // present | writable
1:  cmp rdi, IDMAP_END
    jae 2f
    mov r13, rdi
    call map_page
    add rdi, 4096
    jmp 1b
2:  mov rdi, ALIAS                        // alias A: not present, writable
    mov r13, FAULT_BACK
    mov r14, 2
    call map_page
    mov rdi, ALIAS + 0x1000               // alias B: not present, writable
    mov r13, FAULT_BACK + 0x1000
    mov r14, 2
    call map_page
    mov rdi, ALIAS + 0x2000               // alias C: present, read-only
    mov r13, FAULT_BACK + 0x2000
    mov r14, 1
    call map_page
    mov rdi, LAPIC                        // local APIC register block
    mov r13, LAPIC
    mov r14, 3
    call map_page
    ret

// in: rdi = vector, rsi = handler address. clobbers rax, rcx, rdx.
set_gate:
    mov rax, rsi
    and rax, 0xFFFF                       // offset 15:0
    mov rcx, 8
    shl rcx, 16
    or rax, rcx                           // code selector
    mov rcx, 0x8E00                       // present, DPL 0, 64-bit interrupt gate
    shl rcx, 32
    or rax, rcx
    mov rcx, rsi
    shr rcx, 16
    and rcx, 0xFFFF
    shl rcx, 48                           // offset 31:16
    or rax, rcx
    mov rdx, rdi
    shl rdx, 4
    add rdx, IDT_BASE
    mov [rdx], rax
    mov rcx, rsi
    shr rcx, 32                           // offset 63:32
    mov [rdx + 8], rcx
    ret

build_idt:
    mov rdi, 6
    lea rsi, [rip + ud_handler]
    call set_gate
    mov rdi, 13
    lea rsi, [rip + gp_handler]
    call set_gate
    mov rdi, 14
    lea rsi, [rip + pf_handler]
    call set_gate
    mov rdi, 0x20
    lea rsi, [rip + timer_isr]
    call set_gate
    mov rdi, 0x21
    lea rsi, [rip + ipi_isr]
    call set_gate
    ret

// ------------------------------------------------------------------- data
// On its own guest page so that ordinary data stores do not land on a page
// holding translated code and trigger the self-modifying-code path.

.p2align 12
.globl counter, mp_violations, mp_timeouts, pf_count, ud_count, gp_count
.globl timer_ticks, timer_eois, ipi_recv, ipi_ack_timeouts, hlt_wakeups
.globl cli_delta, smc_before, smc_after, cas_ok, cas_fail_rax
.globl test_a_val, test_b_mem, test_c_mem, iters0, iters1
.globl vcpu0_done, vcpu1_done, fault_log, loop_k, loop_n

tables_ready:     .quad 0
pml4_pa:          .quad 0
arena_ptr:        .quad PT_ARENA
barrier_count:    .quad 0
barrier_sense:    .quad 0
phase3_done:      .quad 0
ipi_ack:          .quad 0
cas_cell:         .quad 0
counter:          .quad 0
mp_violations:    .quad 0
mp_timeouts:      .quad 0
pf_count:         .quad 0
ud_count:         .quad 0
gp_count:         .quad 0
timer_ticks:      .quad 0
timer_eois:       .quad 0
ipi_recv:         .quad 0
ipi_ack_timeouts: .quad 0
hlt_wakeups:      .quad 0
cli_delta:        .quad 0
smc_before:       .quad 0
smc_after:        .quad 0
cas_ok:           .quad 0
cas_fail_rax:     .quad 0
test_a_val:       .quad 0
test_b_mem:       .quad 0
test_c_mem:       .quad 0
iters0:           .quad 0
iters1:           .quad 0
vcpu0_done:       .quad 0
vcpu1_done:       .quad 0
loop_k:           .quad KLOOP
loop_n:           .quad NLOOP
fault_log:        .zero 128
idtr:             .short 4095
                  .quad IDT_BASE
