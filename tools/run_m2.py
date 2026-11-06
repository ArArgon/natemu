#!/usr/bin/env python3
"""M2 checks: memory types (PAT, PWT/PCD, MTRRs) in the walker (boot-features plan, M2).

    tools/with-env python3 tools/run_m2.py --fexc FEXC --out DIR [--cpus 1 2] [--only qemu,types,devices,firmware,faults,litmus,cost]
        [--k K --runs R --uc-cpus 2 3 4] [--host HOST --runtime RUNTIME]

The walker gives each leaf its PAT index (4*PAT + 2*PCD + PWT, SDM Vol.3 Table 14-11; PAT is bit 7 of a PTE, bit 12 of a
2 MiB PDE); the page's effective type is that PAT entry combined with the MTRRs' type of its guest-physical address (Table
14-7, 14.11.4.1 "MTRR Precedences"). WB, WT, WP and WC RAM run natively; UC RAM (owner decision, November 2: it keeps
x86 strong ordering) is never shadowed while a block runs on: each access runs its instruction alone between two full
barriers; a device page goes to its device whatever its type (M3). The resident counts data shadows by type
(`stats.k1.memory_types`; a UC one per access) and its UC accesses (`stats.k1.uncached`: instructions run alone, its
own accesses).

- `qemu` (pinned QEMU system mode, which ignores memory types: it judges that every type behaves as RAM, i.e. the values):
  CPL 0 programs on each CPU's own tables (`programs.sys_text`) at N CPUs and block sizes default and 1. `pat`: Linux's
  PAT (WB WC UC- UC WB WP UC- WT); eight pages with PAT index 0..7 in their PTEs, each: load, store, LOCK XADD, XCHG,
  MOVNTI + SFENCE, CLFLUSH, REP STOSB, LODSQ, MFENCE, INC m, and code run from the page; again with PWT|PCD in the PML4E,
  PDPTE and PDE above them and in CR3; again through a 2 MiB leaf with each PAT index (bit 12). `mtrr`: the SDM's MTRR
  update sequence (Vol.3 14.11.8: CR0.CD, WBINVD, CR3 reload, E clear, ranges, E set, CD clear) with variable ranges UC,
  WC, WT and WP over four of the pages (one discontinuous mask covering every CPU's), default type UC, the same accesses
  and code; then MTRRs disabled (all UC: code, stack and tables included, as in Linux's `cache_disable` window) and the
  accesses again; the ranges read back. `uc`: under the reset PAT, pages UC, WB, UC (the stack), UC-: MOV loads and stores
  of 1-8 bytes, MOVZX/MOVSX, RMW (ADD, INC, NOT, SHL, XOR, NEG, BTS, BTR, ADC), SETcc, CMOVcc, CMP, LOCK ADD/XADD/CMPXCHG/
  INC/BTS, CMPXCHG8B/16B, XCHG, REP MOVSB/MOVSQ both ways between UC and WB, LODSQ, STOSQ, REP STOSQ, REPE CMPSB, REPNE
  SCASB, SSE (MOVUPS, MOVAPS, PADDD m, MOVDQU, MOVQ, MOVD), AVX (VMOVUPS ymm, VPADDD m256, VMOVDQU), x87 (FLD/FSTP m64
  and m80, FILD, FISTP, FNSTSW), PUSH/POP r and m, CALL/RET on a UC stack, loads, split stores and 16/32-byte vector
  accesses straddling UC|WB and WB|UC, MOVNTI, CLFLUSH, fences, code run from a UC page. Registers and the pages against
  QEMU; natemu's type counts must include every non-WB type the SDM gives these pages, and each CPU must have run UC
  accesses alone (`uncached.steps`).
- `types` (natemu only, SDM Table 14-7 and the MTRR rules transcribed below): one store to each of eight fresh pages, so
  each page is one data shadow, under the reset PAT and Linux's, PWT/PCD at every upper level and in CR3 (no effect on
  the page), 2 MiB leaves with each PAT index, MTRRs disabled, variable ranges of every type with the overlaps the SDM
  defines (UC wins, WT with WB is WT), a fixed range below 1 MiB with FE set and clear: `memory_types` must equal the
  model's count per type. `pat-to-uc-*`: a page shadowed WB, a PAT write making its entry UC, then a CR3 reload or
  INVLPG: the next access is one UC shadow run alone (with no flush the WB shadow is kept, as a TLB may keep it).
- `devices` (natemu only, SDM: a device's accesses happen once whatever the type; M3's path): a test device page mapped
  with each PAT index under Linux's PAT, under the firmware's UC range, a WC or WB variable range over it and MTRRs
  disabled: a MOV load and a MOV store each reach the device exactly once with the right values, and no data shadow of
  any type is counted (device pages are never shadowed).

- `firmware` (natemu only; M2 addendum 1): at 4 MiB, 1, 2 and 3 GiB of guest RAM, RDMSR of every MTRR equals
  `cpu_profile.py`'s `mtrr_state(ram)`, by which all RAM is WB and the legacy hole and [ram, 4 GiB) UC; a store to RAM's
  last MiB is one WB shadow.
- `faults` (natemu only; M2 addendum 2): #PF (not present, U/S, R/W with and without WP, reserved bit, PDE, the second
  page of a crossing access either way), #AC, read-only RMW/LOCK/XCHG/REP MOVSB, crossing loads, split stores and vector
  forms, data breakpoints (store, load, RMW, LOCK, REP MOVSB, beside the range) with the data page UC and WB: the same
  outcome, the expected one (`uc_fault_cases`).
- `litmus` (natemu only; addendum 2): store buffering at 2-4 vCPUs with its locations on UC pages: never every load old
  (`litmus_cases`); on WB pages at 2 vCPUs that outcome must show (the control); at 2 vCPUs with one location UC and the
  other WB (a UC store before a WB load and a WB store before a UC load) never both old.
- `cost` (reported): ns per iteration of MOV load, MOV store and LOCK ADD loops on a UC and a WB page.

Writes DIR/m2.json; exit status 1 on any failure."""
import argparse, collections, json, os, pathlib, subprocess, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / 'tools'), str(ROOT / 'tools' / 'reference'), str(ROOT / 'tools' / 'bench' / 'smp')]
import run_slice1_vm as vm, run_slice3_vm as s3, smp, programs as P  # noqa: E402
from models import cpu_profile  # noqa: E402

# ---- The SDM's rules (Vol.3 ch. 14), transcribed for the expectations ---------------------------------------------------
UC, WC, WT, WP, WB, UCM = 0, 1, 4, 5, 6, 7
NAME = {UC: 'UC', WC: 'WC', WT: 'WT', WP: 'WP', WB: 'WB'}
# Table 14-7: MTRR type -> {PAT entry: effective type}.
T14_7 = {UC: {UC: UC, UCM: UC, WC: WC, WT: UC, WB: UC, WP: UC},
         WC: {UC: UC, UCM: WC, WC: WC, WT: UC, WB: WC, WP: UC},
         WT: {UC: UC, UCM: UC, WC: WC, WT: WT, WB: WT, WP: WP},
         WB: {UC: UC, UCM: UC, WC: WC, WT: WT, WB: WB, WP: WP},
         WP: {UC: UC, UCM: WC, WC: WC, WT: WT, WB: WP, WP: WP}}
PAT_RESET, PAT_LINUX = 0x0007_0406_0007_0406, 0x0407_0506_0007_0106   # Table 14-12; K mm/pat/memtype.c:344
PA = 0xff_ffff_f000                                                   # MAXPHYADDR 40
# The firmware's MTRR state for the VM's guest RAM (the driver's NATEMU_RAM, hex; default 2 GiB).
FIRMWARE = cpu_profile.load().mtrr_state(int(os.environ.get('NATEMU_RAM', '0x80000000').removeprefix('0x'), 16))


def mtrr_type(m, gpa):
    """14.11.4.1: E clear -> UC; below 1 MiB with FE -> the fixed field (Table 14-9); else the matching variable ranges
    (identical: that type; one UC: UC; WT with WB: WT; other overlaps undefined -> None); none: the default type."""
    d = m.get(0x2ff, 0)
    if not d >> 11 & 1: return UC
    if d >> 10 & 1 and gpa < 0x10_0000:
        reg, field = ((0x250, gpa >> 16) if gpa < 0x8_0000 else (0x258 + ((gpa - 0x8_0000) >> 17), gpa >> 14 & 7) if gpa < 0xc_0000
                      else (0x268 + ((gpa - 0xc_0000) >> 15), gpa >> 12 & 7))
        return m.get(reg, 0) >> 8 * field & 0xff
    hits = {m.get(0x200 + 2 * n, 0) & 0xff for n in range(8)
            if m.get(0x201 + 2 * n, 0) >> 11 & 1 and gpa & m[0x201 + 2 * n] & PA == m.get(0x200 + 2 * n, 0) & m[0x201 + 2 * n] & PA}
    if not hits: return d & 0xff
    if len(hits) == 1: return hits.pop()
    return UC if UC in hits else WT if hits == {WT, WB} else None


def page_type(m, pat, gpa, index):
    """Table 14-7's effective type; an MTRR overlap the SDM leaves undefined (`mtrr_type` None: at 4 MiB of RAM the
    firmware's WB range 0 meets the `types` ranges' WC and WP pages) is judged as natemu's recorded choice, UC
    (`natemu_abi::cpu::mtrr_type`; plan, Built (M2))."""
    t = mtrr_type(m, gpa)
    return T14_7[UC if t is None else t][pat >> 8 * index & 7]


def bits(index, large=False):
    """PWT (3), PCD (4) and PAT (7 in a PTE, 12 in a 2 MiB PDE) selecting PAT entry `index`."""
    return (index & 1) << 3 | (index >> 1 & 1) << 4 | (index >> 2) << (12 if large else 7)


def wrmsr(index, value): return P.msr_write(index, f'{value:#x}')


# ---- qemu: system programs ----------------------------------------------------------------------------------------------
T = lambda i, k: P.SYS_D(i) + 0x8000 + 0x1000 * k       # CPU i's eight test pages (identity-mapped frames)
ALIAS = 0x40_0000                                       # linear 2 MiB region of table A's PD entry 2 -> frame 0x200000
MTRR_RANGES = [(0x208, UC), (0x20a, WC), (0x20c, WT), (0x20e, WP)]   # variable ranges 4..7 over pages 0..3
MTRR_MASK = 0xff_fffc_f800                              # 4 KiB, bits 17:16 ignored (every CPU's page k), V


def access(a, tag):
    """Data accesses of every kind at linear `a`, then a call to the code at a + 0xf00 (`add r11, k; jmp r13`)."""
    return (f'    mov rbx, {a:#x}\n    mov rax, [rbx]\n    add r8, rax\n    mov [rbx + 8], r8\n    lock xadd [rbx + 16], r8\n'
            f'    xchg [rbx + 24], r9\n    add r9, r8\n    movnti [rbx + 32], r9\n    sfence\n    clflush [rbx + 40]\n    clflushopt [rbx + 41]\n    clwb [rbx + 42]\n'
            f'    lea rdi, [rbx + 0x40]\n    mov ecx, 32\n    mov eax, 0x5a\n    rep stosb\n    lea rsi, [rbx + 0x38]\n    lodsq\n'
            f'    add r10, rax\n    mfence\n    add r10, [rbx + 0x800]\n    inc qword [rbx + 0x808]\n'
            f'    lea r13, [rel .r{tag}]\n    mov rax, {a + 0xf00:#x}\n    jmp rax\n.r{tag}:\n')


def pat_body(i):
    ta, out = P.SYS_T(i, 0), wrmsr(0x277, PAT_LINUX)
    out += ''.join(access(T(i, k), f'a{k}') for k in range(8))
    # PWT|PCD in the PML4E, PDPTE and PDE above the pages and in CR3: the walk reads the tables with those types; the
    # pages keep their own.
    out += (f'    or qword [{ta:#x}], 0x18\n    or qword [{ta + 0x1000:#x}], 0x18\n    or qword [{ta + 0x2008:#x}], 0x18\n'
            f'    mov rax, {ta | 0x18:#x}\n    mov cr3, rax\n')
    out += ''.join(access(T(i, k), f'b{k}') for k in range(8))
    for k in range(8):      # the same frames through a 2 MiB leaf with PAT index k (PDE 2 of table A)
        alias = ALIAS + T(i, k) - 0x20_0000
        out += f'    mov rax, {0x20_00e3 | bits(k, True):#x}\n    mov [{ta + 0x2010:#x}], rax\n    mov rax, {alias:#x}\n    invlpg [rax]\n'
        out += access(alias, f'c{k}')
    return out


def mtrr_body(i):
    cr0 = lambda op: f'    mov rax, cr0\n    {op} rax, 30\n    mov cr0, rax\n'
    reload = '    mov rax, cr3\n    mov cr3, rax\n'
    out = cr0('bts') + '    wbinvd\n' + reload + wrmsr(0x2ff, 0)
    for k, (base, t) in enumerate(MTRR_RANGES):
        out += wrmsr(base, T(0, k) | t) + wrmsr(base + 1, MTRR_MASK)
    out += wrmsr(0x2ff, 0xc00) + '    wbinvd\n' + reload + cr0('btr')
    out += ''.join(access(T(i, k), f'a{k}') for k in range(8))
    out += P.msr_read(0x20a, 'r12') + P.msr_read(0x20b, 'r14') + P.msr_read(0x2ff, 'r15')
    out += wrmsr(0x2ff, 0)                               # MTRRs disabled: all of memory UC
    out += ''.join(access(T(i, k), f'b{k}') for k in range(8))
    out += wrmsr(0x2ff, FIRMWARE[0x2ff]) + ''.join(wrmsr(b, 0) + wrmsr(b + 1, 0) for b, _ in MTRR_RANGES)
    return out


# UC forms (owner decision, November 2: uncached RAM keeps x86 strong ordering): every access form on UC pages, under the
# reset PAT: page 0 UC (index 3), 1 WB, 2 UC (the stack), 3 UC- (index 2, UC with a WB MTRR), so 0|1 and 1|2 are WB/UC
# boundaries in both orders.
UC_PAGES = {0: 3, 1: 0, 2: 3, 3: 2}


def uc_body(i):
    u0, w1, u2, m3 = (T(i, k) for k in range(4))
    return f"""    mov rax, cr4
    or rax, 0x40600
    mov cr4, rax
    xor ecx, ecx
    xor edx, edx
    mov eax, 7
    xsetbv
    mov rbx, {u0:#x}
    mov rbp, {w1:#x}
    mov r12, {u2:#x}
    mov r15, {m3:#x}
    mov al, [rbx]
    mov [rbx+1], al
    mov ax, [rbx+2]
    mov [rbx+4], ax
    mov eax, [rbx+8]
    mov [rbx+12], eax
    mov rax, [rbx+16]
    mov [rbx+24], rax
    mov byte [rbx+0x20], 0x7f
    mov qword [rbx+0x28], -5
    movzx ecx, byte [rbx+5]
    movsx rdx, word [rbx+6]
    lea r8, [rcx+rdx]
    mov r10, [r15+8]
    mov [r15+16], r8
    add [rbx+0x30], r8
    inc dword [rbx+0x38]
    not qword [rbx+0x40]
    shl qword [rbx+0x48], 3
    xor [rbx+0x50], r8d
    neg byte [rbx+0x58]
    bts qword [rbx+0x60], 5
    btr dword [rbx+0x68], 3
    setc byte [rbx+0x6c]
    cmovnz r9, [rbx+0x70]
    cmp qword [rbx+0x78], 7
    adc r9, [rbx+0x80]
    lock add [rbx+0x88], r8
    lock xadd [rbx+0x90], r9
    mov eax, [rbx+0x98]
    lock cmpxchg [rbx+0x98], r8d
    xchg [rbx+0xa0], r10
    lock inc qword [rbx+0xa8]
    lock bts qword [rbx+0xb8], 7
    mov rdi, rbx
    mov eax, [rdi+0xc0]
    mov edx, [rdi+0xc4]
    mov ebx, 0x11
    mov ecx, 0x22
    lock cmpxchg8b [rdi+0xc0]
    mov rax, [rdi+0xd0]
    mov rdx, [rdi+0xd8]
    mov rbx, 0x33
    mov rcx, 0x44
    lock cmpxchg16b [rdi+0xd0]
    mov rbx, rdi
    cld
    lea rsi, [rbp+0x100]
    lea rdi, [rbx+0x200]
    mov ecx, 24
    rep movsb
    lea rsi, [rbx+0x100]
    lea rdi, [rbp+0x200]
    mov ecx, 3
    rep movsq
    lea rsi, [rbx+0x300]
    lodsq
    add r8, rax
    lea rdi, [r12+0x300]
    stosq
    mov ecx, 4
    rep stosq
    lea rsi, [rbx+0x340]
    lea rdi, [r12+0x340]
    mov ecx, 8
    repe cmpsb
    lea rdi, [rbx+0x380]
    xor eax, eax
    mov ecx, 16
    repne scasb
    add r9, rcx
    movups xmm0, [rbx+0x400]
    movaps [rbx+0x410], xmm0
    paddd xmm0, [rbx+0x420]
    movdqu [rbx+0x430], xmm0
    movq xmm1, [rbx+0x440]
    movd [rbx+0x448], xmm1
    vmovups ymm2, [rbx+0x480]
    vpaddd ymm2, ymm2, [rbx+0x4a0]
    vmovdqu [rbx+0x4c0], ymm2
    mov rax, 0x400921fb54442d18
    mov [rbx+0x500], rax
    fninit
    fld qword [rbx+0x500]
    fild dword [rbx+0x508]
    fstp qword [rbx+0x510]
    fstp tword [rbx+0x530]
    fld tword [rbx+0x530]
    fistp qword [rbx+0x518]
    fnstsw [rbx+0x540]
    mov r14, rsp
    lea rsp, [r12+0x800]
    push r8
    push qword [rbx+0x18]
    pop qword [rbx+0x600]
    pop rcx
    add r9, rcx
    call .f
    jmp .g
.f:
    add r9, 3
    ret
.g:
    mov rsp, r14
    mov rax, [rbx+0xffc]
    add r8, rax
    mov rax, [rbp+0xffd]
    add r8, rax
    mov [rbx+0xffa], r8
    mov [rbp+0xffb], r9
    movups xmm3, [rbp+0xff8]
    movups [rbx+0xff4], xmm3
    vmovups ymm4, [rbx+0xfe8]
    vmovups [rbp+0xff0], ymm4
    movnti [rbx+0x700], r8
    sfence
    clflush [rbx+0x708]
    clflushopt [rbx+0x709]
    clwb [rbx+0x70a]
    mfence
    lea r13, [rel .r]
    mov rax, {u0 + 0xf00:#x}
    jmp rax
.r:
"""


def page_bytes(i, k):
    data = bytearray(smp.lcg_bytes(0x1000, 0x5150 + 16 * i + k))
    data[0xf00:0xf00 + 16] = P.asm(f'add r11, {k + 1}\njmp r13').ljust(16, b'\x90')
    return {'addr': f'0x{T(i, k):x}', 'data': bytes(data).hex()}


PROGRAMS = {'pat': (pat_body, lambda i: {T(i, k): T(i, k) | P.LEAF | bits(k) for k in range(8)}),
            'mtrr': (mtrr_body, lambda i: {}),
            'uc': (uc_body, lambda i: {T(i, k): T(i, k) | P.LEAF | bits(x) for k, x in UC_PAGES.items()})}
# Non-WB types the SDM gives the test pages (each CPU's counts must include them); a UC access runs its instruction alone.
# `mtrr`: pages 0..3 under the firmware's MTRRs plus MTRR_RANGES (default type UC, PAT entry 0 WB), by `page_type`: UC, WC,
# WT, WP at 2 GiB; at 4 MiB the firmware's WB range 0 also covers them, so WC and WP meet WB (undefined: natemu's UC).
_MTRR_PROGRAM = {**FIRMWARE, 0x2ff: 0xc00, **{b: T(0, k) | t for k, (b, t) in enumerate(MTRR_RANGES)},
                 **{b + 1: MTRR_MASK for b, _ in MTRR_RANGES}}
QEMU_TYPES = {'pat': {WC, UC, WP, WT}, 'mtrr': {page_type(_MTRR_PROGRAM, PAT_RESET, T(0, k), 0) for k in range(4)} - {WB},
              'uc': {UC}}


def program(name, n):
    text, overrides = PROGRAMS[name]
    out = {'format': 'natemu-smp-v1', 'name': f'm2-{name}@{n}', 'ncpu': n, 'system': True, 'bodies': [], 'cpus': [], 'memory': [], 'params': {}}
    for i in range(n):
        out['bodies'].append(P.body(P.SYS_CODE(i), P.sys_text(i, text(i))))
        out['cpus'].append(dict(P.cpu(i, P.SYS_CODE(i)), cpl=0))
        out['memory'] += P.sys_tables(i, 0, n, overrides(i)) + P.sys_tables(i, 1, n, {}) + [page_bytes(i, k) for k in range(8)] \
            + [dict(P.zero(P.SYS_D(i) + P.SAVE, 8), capture=False)]
    return out


def qemu_cases(a):
    stub = smp.build_stub(a.out / 'qemu-build')
    for n in a.cpus:
        for name in PROGRAMS:
            prog = program(name, n)
            rc, err, dump, _ = smp.run_image(prog, smp.build_image(prog, stub), a.out / 'qemu' / prog['name'], 'multi', timeout=120)
            try: want = smp.parse_dump(prog, dump)
            except ValueError as e: yield prog['name'], False, f'QEMU run failed ({rc}): {e} {err[-200:]}'; continue
            for extra in ([], [1]):
                work = a.out / 'work' / f"{prog['name']}{'-1' if extra else ''}"
                rc, out, err = s3.driver(a.host, a.runtime, a.fexc, s3.smp_description(prog), work, extra, timeout=300)
                problems = [] if rc == 0 else [f'driver exit {rc}: {err.strip()[-300:]}']
                for v, c in enumerate(out['cpus'] if out else []):
                    end = s3.body_end(prog, smp.num(prog['cpus'][v]['rip']))
                    if c['stop'] != 'reached' or smp.num(c['rip']) != end:
                        problems.append(f"cpu{v} stopped {c['stop']} {c['reason']} at {c['rip']} (end {end:#x})")
                    problems += [f'cpu{v} ' + p for p in s3.per_vcpu_checks(c)]
                    seen = c['stats'].get('k1', {}).get('memory_types', {})
                    missing = sorted(NAME[t] for t in QEMU_TYPES[name] if not seen.get(NAME[t]))
                    if missing: problems.append(f'cpu{v} memory_types {seen} lack {missing}')
                    steps = c['stats'].get('k1', {}).get('uncached', {}).get('steps')
                    if not steps: problems.append(f'cpu{v} ran no instruction alone for UC RAM (uncached {steps})')
                if out and not problems:
                    problems += smp.compare(prog, s3.vm_state(prog, out), want)
                yield f"{prog['name']}{' (max-block 1)' if extra else ''}", not problems, problems


# ---- types: natemu only, one data shadow per page ------------------------------------------------------------------------
CODE, PAGES = 0x10_0000, 0x30_0000


def run(a, name, text, extra=()):
    w = a.out / 'work' / name
    w.mkdir(parents=True, exist_ok=True)
    (w / 'program.txt').write_text(text)
    r = subprocess.run([a.host, a.runtime, 'run-program', a.fexc, w / 'program.txt', w, *map(str, extra)], capture_output=True, text=True, timeout=300)
    return json.loads(r.stdout) if r.returncode == 0 else {'stop': 'driver', 'reason': r.stderr.strip()[-300:]}


def types_case(a, name, msrs, pages, prelude='', cr3=0, upper=0, large=False):
    """Stores one byte to each page (va, PAT index) after `msrs` are written (in order) and CR3's PWT/PCD set to `cr3`;
    `upper` PWT/PCD in every entry above the leaves; `large`: the pages lie in one 2 MiB leaf (the index of the first)."""
    text = ''.join(wrmsr(i, v) for i, v in msrs) + prelude
    if cr3: text += f'    mov rax, cr3\n    or rax, {cr3:#x}\n    mov cr3, rax\n'
    text += ''.join(f'    mov byte [{va:#x}], 1\n' for va, _ in pages)
    lo = min(va for va, _ in pages) & ~0xfff
    hi = max(va for va, _ in pages) + 0x1000 & ~0xfff
    desc = ('' if large else 'identity\n') + f'rip {CODE:#x}\nrflags 0x2\ncpl 0\ncode {P.asm(text).hex()}\nregion {lo:#x} {hi - lo:#x}\n'
    if large:
        desc += f'large {lo:#x}\n' + (f'entry {lo:#x} 2 set {bits(pages[0][1], True):#x}\n' if pages[0][1] else '')
    else:
        desc += ''.join(f'entry {va:#x} 1 set {bits(k):#x}\n' for va, k in pages if bits(k))
    desc += ''.join(f'entry {lo:#x} {level} set {upper:#x}\n' for level in ((4, 3) if large else (4, 3, 2)) if upper)
    out = run(a, name, desc)
    m = {**FIRMWARE, 0x277: PAT_RESET}
    for i, v in msrs: m[i] = v
    # The driver's large frame is RAM above 1 MiB (write-back under the firmware's MTRRs); identity pages are at their VA.
    want = collections.Counter(NAME[page_type(m, m[0x277], va if not large else 0x20_0000, pages[0][1] if large else k)] for va, k in pages)
    got = out.get('stats', {}).get('k1', {}).get('memory_types')
    ok = out.get('stop') == 'reached' and got == dict(want)
    return name, ok, {'stop': out.get('stop'), 'reason': out.get('reason'), 'memory_types': got, 'expected': dict(want)}


def types_cases(a):
    eight = [(PAGES + 0x1000 * k, k) for k in range(8)]
    plain = [(PAGES + 0x1000 * k, 0) for k in range(8)]
    yield types_case(a, 'reset-pat', [], eight)
    yield types_case(a, 'linux-pat', [(0x277, PAT_LINUX)], eight)
    for cr3 in (0x8, 0x10, 0x18):
        for upper in (0x8, 0x10, 0x18):
            yield types_case(a, f'upper-{upper:#x}-cr3-{cr3:#x}', [(0x277, PAT_LINUX)], plain, cr3=cr3, upper=upper)
    for k in range(8):
        yield types_case(a, f'large-{k}', [(0x277, PAT_LINUX)], [(0x60_0000 + 0x1000 * j, k) for j in range(4)], large=True)
    yield types_case(a, 'mtrr-disabled', [(0x277, PAT_LINUX), (0x2ff, 0)], eight)
    # Variable ranges 4..7 on pages 0..3 (UC, WC, WT, WP), range 3 WB over pages 4..7 (16 KiB), default UC.
    ranges = [(0x208, PAGES | UC), (0x209, 0xff_ffff_f800), (0x20a, PAGES + 0x1000 | WC), (0x20b, 0xff_ffff_f800),
              (0x20c, PAGES + 0x2000 | WT), (0x20d, 0xff_ffff_f800), (0x20e, PAGES + 0x3000 | WP), (0x20f, 0xff_ffff_f800),
              (0x206, PAGES + 0x4000 | WB), (0x207, 0xff_ffff_c800)]
    for label, pat in (('reset', PAT_RESET), ('linux', PAT_LINUX)):
        yield types_case(a, f'mtrr-ranges-{label}', [(0x277, pat), (0x2ff, 0), *ranges, (0x2ff, 0xc00)], eight)
    # The overlaps the SDM defines: range 2 WB over pages 0..3 meets page 0's UC (UC) and page 2's WT (WT); pages 1 and 3
    # (WC and WP with WB: undefined) are not stored to.
    yield types_case(a, 'mtrr-overlaps', [(0x2ff, 0), *ranges, (0x204, PAGES | WB), (0x205, 0xff_ffff_c800), (0x2ff, 0xc06)],
                     [(PAGES, 0), (PAGES + 0x2000, 0)] + plain[4:])
    # A fixed range below 1 MiB (RAM at 0x9f000, FIX16K_80000's last field): WC with FE set, the default (WB) with FE clear.
    fixed = [(0x9_f000, 0)]
    yield types_case(a, 'fixed-fe', [(0x258, 0x0106_0606_0606_0606)], fixed)
    yield types_case(a, 'fixed-no-fe', [(0x258, 0x0106_0606_0606_0606), (0x2ff, 0x806)], fixed)
    # A page shadowed WB (PAT index 4), then a PAT write making entry 4 UC: after a TLB flush (CR3 reload or INVLPG) the
    # next access takes the UC path; with no flush the shadow may keep its type (SDM Vol.3 14.12.4, 5.9.3; natemu keeps it).
    for label, flush, want, steps in (('cr3', 'mov rax, cr3\nmov cr3, rax', {'WB': 1, 'UC': 1}, 1),
                                      ('invlpg', f'invlpg [{PAGES:#x}]', {'WB': 1, 'UC': 1}, 1), ('no-flush', '', {'WB': 1}, 0)):
        name = f'pat-to-uc-{label}'
        text = f'mov byte [{PAGES:#x}], 1\n' + wrmsr(0x277, PAT_RESET & ~(0xff << 32)) + flush + f'\nmov byte [{PAGES + 8:#x}], 2\n'
        out = run(a, name, f'identity\nrip {CODE:#x}\nrflags 0x2\ncpl 0\ncode {P.asm(text).hex()}\nregion {PAGES:#x} 0x1000\n'
                           f'entry {PAGES:#x} 1 set {bits(4):#x}\n')
        k1 = out.get('stats', {}).get('k1', {})
        got = (k1.get('memory_types'), k1.get('uncached', {}).get('steps'))
        yield name, out.get('stop') == 'reached' and got == (want, steps), {'stop': out.get('stop'), 'reason': out.get('reason'), 'got': got}


# ---- firmware: the virtual firmware's MTRRs follow the VM's guest RAM (M2 addendum) ----------------------------------------
FIRMWARE_MSRS = [0x2ff, *range(0x200, 0x210), 0x250, 0x258, 0x259, *range(0x268, 0x270)]


def firmware_cases(a):
    """At 1, 2 and 3 GiB of guest RAM (and 4 MiB, the smallest test size), with the tables and the result page in RAM's
    last MiB: RDMSR of the MTRRs equals `cpu_profile.py`'s `mtrr_state(ram)`, by which every RAM 2 MiB block (and every
    page below 0xa0000) is WB and the legacy hole and [ram, 4 GiB) are UC; the result page's shadow is counted WB."""
    prof, mib = cpu_profile.load(), 1 << 20
    text = ''.join(f'    mov ecx, {i:#x}\n    rdmsr\n    mov [{0x30_0000 + 8 * k:#x}], eax\n    mov [{0x30_0004 + 8 * k:#x}], edx\n'
                   for k, i in enumerate(FIRMWARE_MSRS))
    for ram in (4 * mib, 1 << 30, 2 << 30, 3 << 30):
        desc = f'rip {CODE:#x}\nrflags 0x2\ncpl 0\ncode {P.asm(text).hex()}\nmemory 0x300000 {bytes(8 * len(FIRMWARE_MSRS)).hex()}\n'
        out = run(a, f'firmware-{ram:#x}', desc, ['--ram', hex(ram), '--place', hex(ram - mib)])
        mem = bytes.fromhex(next((m['data'] for m in out.get('memory', []) if int(m['addr'], 0) == 0x30_0000), ''))
        got = {i: int.from_bytes(mem[8 * k:8 * k + 8], 'little') for k, i in enumerate(FIRMWARE_MSRS)} if mem else {}
        st = prof.mtrr_state(ram)
        want = {i: st.get(i, 0) for i in FIRMWARE_MSRS}
        wrong = [f'{a2:#x}' for a2 in [*range(0, 0xa_0000, 0x1000), *range(0xa_0000, 0x10_0000, 0x1000), *range(0x10_0000, 1 << 32, 2 * mib)]
                 for t in [prof.memory_type(got, a2)] if t != (WB if a2 < 0xa_0000 or 0x10_0000 <= a2 < ram else UC)]
        ok = (out.get('stop') == 'reached' and got == want and not wrong
              and out['stats']['k1'].get('memory_types') == {'WB': 1})
        yield f'firmware-{ram:#x}', ok, {'stop': out.get('stop'), 'reason': out.get('reason'), 'msrs': {hex(k): hex(v) for k, v in got.items() if v},
                                         'want': {hex(k): hex(v) for k, v in want.items() if v}, 'wrong': wrong[:8],
                                         'memory_types': out.get('stats', {}).get('k1', {}).get('memory_types')}


# ---- uc: natemu only (owner decision, November 2: uncached RAM keeps x86 strong ordering) --------------------------------
def litmus_desc(prog, uc):
    """The driver description of litmus `prog` (`page` locations), the pages of the locations in `uc` UC (PWT|PCD: PAT
    index 3 under the reset PAT): entry mutations of the shared space, before the first `cpu` section."""
    lines = s3.smp_description(prog).splitlines()
    at = lines.index('cpu')
    locs = P.family_locations(P.litmus_family(prog['name'].split('@')[0], prog['ncpu']), 'page')
    entries = [f'entry {P.SYNC + off:#x} 1 set {bits(3):#x}' for loc, off in sorted(locs.items()) if loc in uc]
    return '\n'.join(lines[:at] + entries + lines[at:]) + '\n'


def litmus_cases(a):
    """Store buffering at 2-4 vCPUs (each stores its location, then loads the next one's), locations on pages of their own:
    on UC pages every iteration's outcome must be in the sequentially consistent set (sb-mfence's, from
    tools/models/ordering_model.py: never every load old), while the same program on WB pages must show that outcome at 2
    vCPUs (the control: TSO allows it, so a run that never shows it could not see a violation; at 3 and 4 it is reported,
    being rare). `mixed` (2 vCPUs, x0 UC, x1 WB): vCPU 0 stores UC x0 then loads WB x1, vCPU 1 stores WB x1 then loads UC
    x0. UC accesses are "executed in program order without reordering" (SDM Vol.3 14.3, Table 14-2 "Strong Ordering";
    11.2.5: "out-of-order or speculative accesses are not performed"); the store-load relaxation of 11.2.2 is stated for
    WB regions. So neither vCPU's load passes its store and both old is forbidden. vCPU 0's WB load is translated code run
    right after the UC store's step block, with no host exit between: only the resident entry orders it."""
    from models import ordering_model as model
    for n in a.uc_cpus:
        test = P.litmus_family('sb', n)
        prog = P.family_program(test, 'page', a.k)
        sc = model.enumerate_outcomes(P.litmus_family('sb-mfence', n)).outcomes
        every = {f'x{i}' for i in range(n)}
        for label, uc in (('uc', every), ('wb', set()), *((('mixed', {'x0'}),) if n == 2 else ())):
            hist, problems = collections.Counter(), []
            for r in range(a.runs):
                rc, out, err = s3.driver(a.host, a.runtime, a.fexc, litmus_desc(prog, uc), a.out / 'work' / f"litmus-{label}@{n}-{r}", timeout=600)
                if rc: problems.append(f'driver exit {rc}: {err.strip()[-300:]}'); continue
                problems += [f"cpu{v} stopped {c['stop']} {c['reason']}" for v, c in enumerate(out['cpus']) if c['stop'] != 'reached']
                got = {'memory': [{'addr': smp.num(m['addr']), 'data': bytes.fromhex(m['data'])} for m in out['cpus'][0]['memory']]}
                hist.update(P.litmus_outcomes(prog, test, got, a.k, prog['params']['stride'], P.FAMILY_RES))
                steps = sum(c['stats']['k1'].get('uncached', {}).get('steps', 0) for c in out['cpus'])
                if steps < 2 * len(uc) * a.k: problems.append(f'{steps} instructions run alone for UC, fewer than the {2 * len(uc) * a.k} accesses')
            outside = {str(o): c for o, c in hist.items() if o not in sc}
            if uc and outside: problems.append(f'outcomes outside SC with {sorted(uc)} UC: {outside}')
            if not uc and n == 2 and not outside: problems.append('control: WB shows no non-SC outcome')
            if sum(hist.values()) != a.k * a.runs: problems.append(f'{sum(hist.values())} of {a.k * a.runs} trials')
            yield f"litmus-sb@{n}-{label}", not problems, {'histogram': {str(o): c for o, c in sorted(hist.items())}, 'problems': problems}


UCD = 0x20_0000                                         # the data page (the next one is WB unless a case says)
SAME = ('stop', 'reason', 'fault', 'gpr', 'rip', 'rflags', 'memory', 'xmm', 'x87', 'pages', 'delivered')


def uc_fault_cases(a):
    """Faults, permission and CPL checks and data breakpoints on UC RAM (natemu only): each case runs with the data page
    UC (PWT|PCD) and WB; the outcome (stop, fault vector, CR2, error and RIP, registers, memory, A/D bits, the delivered
    event) must be the same and the expected one, and on UC a completed case must have run an instruction alone or had
    the resident make the access (a page-crossing store)."""
    head = P.asm('mov rax, cr0\nbts rax, 18\nmov cr0, rax\nxor eax, eax')
    dbg = 'mov rax, {0:#x}\nmov dr0, rax\nmov rax, {1:#x}\nmov dr7, rax\n'
    ld, st = f'mov eax, dword [{UCD + 8:#x}]', f'mov dword [{UCD + 8:#x}], ecx'
    # (name, code, cpl, extra description lines, (vector, CR2, error, RIP offset) or None: completes; the code at CPL 3 for
    # `#AC` runs after `head`, its switch at its NOP)
    cases = [('np-load', ld, 3, f'unmap {UCD:#x}', (14, UCD + 8, 4)), ('np-store', st, 3, f'unmap {UCD:#x}', (14, UCD + 8, 6)),
             ('np-load-cpl0', ld, 0, f'unmap {UCD:#x}', (14, UCD + 8, 0)),
             ('supervisor-load', ld, 3, f'entry {UCD:#x} 1 clear 4', (14, UCD + 8, 5)),
             ('supervisor-store', st, 3, f'entry {UCD:#x} 1 clear 4', (14, UCD + 8, 7)),
             ('supervisor-cpl0', ld, 0, f'entry {UCD:#x} 1 clear 4', None),
             ('readonly-store', st, 3, f'readonly {UCD:#x}', (14, UCD + 8, 7)),
             ('readonly-store-cpl0-wp', st, 0, f'readonly {UCD:#x}', (14, UCD + 8, 3)),
             ('readonly-load', ld, 3, f'readonly {UCD:#x}', None),
             ('readonly-lock-add', f'lock add dword [{UCD + 8:#x}], ecx', 3, f'readonly {UCD:#x}', (14, UCD + 8, 7)),
             ('readonly-xchg', f'xchg dword [{UCD + 8:#x}], ecx', 3, f'readonly {UCD:#x}', (14, UCD + 8, 7)),
             ('readonly-movsb', f'mov rsi, {UCD + 0x1000:#x}\nmov rdi, {UCD + 0xffe:#x}\nmov ecx, 4\nrep movsb', 3,
              f'readonly {UCD:#x}', (14, UCD + 0xffe, 7)),
             ('reserved-bit', ld, 3, f'entry {UCD:#x} 1 set 0x8000000000000', (14, UCD + 8, 0xd)),
             ('pde-not-present', ld, 3, f'entry {UCD:#x} 2 clear 1', (14, UCD + 8, 4)),
             # Crossing from the data page into the next: its second page not present, or read-only for a split store.
             ('crossing-np', f'mov rax, qword [{UCD + 0xffc:#x}]', 3, f'unmap {UCD + 0x1000:#x}', (14, UCD + 0x1000, 4)),
             ('crossing-store-readonly', f'mov qword [{UCD + 0xffc:#x}], rcx', 3, f'readonly {UCD + 0x1000:#x}', (14, UCD + 0x1000, 7)),
             ('crossing-np-into', f'mov rax, qword [{UCD - 4:#x}]', 3, f'unmap {UCD:#x}', (14, UCD, 4)),
             # UC to WB and WB to UC: loads, split stores, 16- and 32-byte vector forms.
             ('crossing-load', f'mov rax, qword [{UCD + 0xffc:#x}]\nmov rdx, qword [{UCD - 4:#x}]\nmovups xmm1, [{UCD - 8:#x}]\n'
              f'vmovups ymm2, [{UCD + 0xff0:#x}]', 3, '', None),
             ('crossing-store', f'mov qword [{UCD + 0xffc:#x}], rcx\nmov qword [{UCD - 2:#x}], rcx\nmovups [{UCD + 0xff8:#x}], xmm0\n'
              f'vmovups [{UCD - 0x10:#x}], ymm0', 3, '', None),
             ('ac-misaligned', 'mov ecx, dword [{0:#x}]'.format(UCD + 2), 'ac', '', (17, 0, 0)),
             ('ac-aligned', 'mov ecx, dword [{0:#x}]\nmov edx, 1'.format(UCD + 4), 'ac', '', None)]
    # Data breakpoints (S4): a trap after the instruction whose access meets the range (DR7 L0, R/W 01 write or 11 read
    # or write, LEN 1); no #DB for an access beside it. With no IDT the #DB stops the run (`guest_fault`, vector 1).
    for name, rw, text, hit in (('db-store', 1, f'mov byte [{UCD + 0x10:#x}], 1', True), ('db-load', 3, f'mov al, [{UCD + 0x10:#x}]', True),
                                ('db-rmw', 1, f'add byte [{UCD + 0x10:#x}], 1', True), ('db-lock', 3, f'lock inc byte [{UCD + 0x10:#x}]', True),
                                ('db-movsb', 1, f'mov rsi, {UCD + 0x40:#x}\nmov rdi, {UCD + 0xe:#x}\nmov ecx, 4\nrep movsb', True),
                                ('db-beside', 3, f'mov byte [{UCD + 0x11:#x}], 1', False)):
        cases.append((name, dbg.format(UCD + 0x10, 1 | rw << 16) + text + '\nnop\nmov edx, 7', 0, '', 'db' if hit else None))
    for name, text, cpl, extra, want in cases:
        outs = {}
        for uc in (True, False):
            if cpl == 'ac':
                code = head + b'\x90' + P.asm(text)
                desc = f'rip {CODE:#x}\nrflags 0x40002\ncpl 0\nswitch {CODE + len(head):#x} 3\ncode {code.hex()}\n'
            else:
                desc = f'rip {CODE:#x}\nrflags 0x2\ncpl {cpl}\ngpr 0 0x5a5a5a5a 0 0 0 0 0 0 0 0 0 0 0 0 0 0\ncode {P.asm(text).hex()}\n'
            desc += f'sse 1\nxcr0 7\nmemory {UCD - 0x1000:#x} {smp.lcg_bytes(0x3000, 0x0c0c).hex()}\n' + (f'entry {UCD:#x} 1 set {bits(3):#x}\n' if uc else '') + (extra + '\n' if extra else '')
            outs[uc] = run(a, f"ucf-{name}-{'uc' if uc else 'wb'}", desc)
        u, w = outs[True], outs[False]
        problems = [f'{k}: UC {u.get(k)} != WB {w.get(k)}' for k in SAME if u.get(k) != w.get(k)]
        if want is None:
            if u.get('stop') != 'reached': problems.append(f"stop {u.get('stop')} {u.get('reason')}")
            elif not any(u['stats']['k1'].get('uncached', {}).values()): problems.append('no UC access run alone or by the resident')
        elif want == 'db':
            if u.get('stop') != 'guest_fault' or int(u['fault'][0], 0) != 1: problems.append(f"no #DB: {u.get('stop')} {u.get('fault')}")
        elif u.get('stop') != 'guest_fault' or [int(x, 0) for x in u['fault'][:3]] != list(want):
            problems.append(f"fault {u.get('stop')} {u.get('fault')} != {want}")
        yield f'uc-{name}', not problems, {'uc': {k: u.get(k) for k in ('stop', 'reason', 'fault')}, 'problems': problems[:4]}


def cost_cases(a, n=20_000):
    """The cost of an access to UC RAM against WB (reported, judged only by completing): RDTSC (1 GHz, ns) around n
    iterations of a MOV load loop, a MOV store loop and a LOCK ADD loop on one page, UC and WB."""
    t = 'rdtsc\nshl rdx, 32\nor rax, rdx\n'
    loops = [('load', f'mov rax, [{UCD + 8:#x}]'), ('store', f'mov [{UCD + 16:#x}], ecx'), ('lock-add', f'lock add [{UCD + 24:#x}], ecx')]
    text = ''.join(f'{t}mov r{8 + k}, rax\nmov ecx, {n}\n.l{k}:\n{body}\ndec ecx\njnz .l{k}\n{t}sub r{8 + k}, rax\nneg r{8 + k}\n'
                   for k, (_, body) in enumerate(loops))
    got = {}
    for uc in (True, False):
        desc = (f'rip {CODE:#x}\nrflags 0x2\ncpl 0\ncode {P.asm(text).hex()}\nregion {UCD:#x} 0x1000\n'
                + (f'entry {UCD:#x} 1 set {bits(3):#x}\n' if uc else ''))
        out = run(a, f"cost-{'uc' if uc else 'wb'}", desc)
        if out.get('stop') != 'reached':
            yield f"cost-{'uc' if uc else 'wb'}", False, {'stop': out.get('stop'), 'reason': out.get('reason')}; continue
        got[uc] = {name: int(out['gpr'][8 + k], 0) / n for k, (name, _) in enumerate(loops)}
        yield f"cost-{'uc' if uc else 'wb'}", True, {'ns_per_iteration': got[uc], 'uncached': out['stats']['k1'].get('uncached')}
    if len(got) == 2:
        print('cost (ns per iteration, UC / WB): ' + ', '.join(f'{k} {got[True][k]:.0f} / {got[False][k]:.1f}' for k in got[True]), flush=True)


# ---- devices: natemu only ------------------------------------------------------------------------------------------------
DEV, GPA = 0x20_0000, 0xfeb0_0000


def devices_cases(a):
    text = P.asm('mov rax, [0x200008]\nmov qword [0x200010], rcx')
    setups = {'firmware-uc': [], 'mtrr-wc': [(0x2ff, 0), (0x208, GPA | WC), (0x209, 0xff_ffff_f800), (0x2ff, 0xc06)],
              'mtrr-wb': [(0x2ff, 0), (0x208, GPA | WB), (0x209, 0xff_ffff_f800), (0x2ff, 0xc06)], 'mtrr-off': [(0x2ff, 0)]}
    for label, msrs in setups.items():
        for k in range(8):
            code = P.asm(''.join(wrmsr(i, v) for i, v in [(0x277, PAT_LINUX), *msrs])) + text
            desc = (f'rip {CODE:#x}\nrflags 0x2\ncpl 0\ngpr 0 0x1122334455667788 0 0 0 0 0 0 0 0 0 0 0 0 0 0\ncode {code.hex()}\n'
                    f'mmio {DEV:#x} {GPA:#x}\nmemory {DEV + 8:#x} efcdab8967452301\n' + (f'entry {DEV:#x} 1 set {bits(k):#x}\n' if bits(k) else ''))
            out = run(a, f'device-{label}-{k}', desc)
            d = (out.get('devices') or [{}])[0]
            ok = (out.get('stop') == 'reached' and int(out['gpr'][0], 0) == 0x0123_4567_89ab_cdef and d.get('reads') == [1]
                  and d.get('writes') == [1] and not out['stats']['k1'].get('memory_types'))
            yield f'device-{label}-{k}', ok, {k2: out.get(k2) for k2 in ('stop', 'reason', 'devices')}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--fexc', type=pathlib.Path, required=True)
    ap.add_argument('--out', type=pathlib.Path, required=True)
    ap.add_argument('--cpus', type=int, nargs='+', default=[1, 2])
    ap.add_argument('--only', type=lambda s: s.split(','), default=['qemu', 'types', 'devices', 'firmware', 'faults', 'litmus', 'cost'])
    ap.add_argument('--k', type=int, default=10_000, help='litmus: iterations per run')
    ap.add_argument('--runs', type=int, default=2, help='litmus: runs per cell')
    ap.add_argument('--uc-cpus', type=int, nargs='+', default=[2, 3, 4], help='litmus: vCPU counts')
    ap.add_argument('--host')
    ap.add_argument('--runtime')
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    if not (a.host and a.runtime): a.host, a.runtime = vm.build()
    results, failures = [], []
    for part, cases in (('types', types_cases), ('devices', devices_cases), ('firmware', firmware_cases), ('qemu', qemu_cases),
                        ('faults', uc_fault_cases), ('litmus', litmus_cases), ('cost', cost_cases)):
        if part not in a.only: continue
        for name, ok, detail in cases(a):
            results.append({'case': name, 'ok': ok, 'detail': detail})
            print(f"{name}: {'ok' if ok else 'FAIL'} {'' if ok else json.dumps(detail)[:600]}", flush=True)
            if not ok: failures.append(name)
    (a.out / 'm2.json').write_text(json.dumps({'results': results, 'failures': failures}, indent=1) + '\n')
    print(f'{len(results)} cases, {len(failures)} failures')
    sys.exit(1 if failures else 0)


if __name__ == '__main__':
    main()
