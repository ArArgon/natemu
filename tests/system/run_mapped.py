#!/usr/bin/env python3
"""Mapped device memory, the resident side (docs/restructure/mapped-device-memory-plan.md, Task 2): bare CPL 0 programs on
their own page tables under `natemu-host run-program --mapped-at GPA,LEN --mapped-control-at GPA`, each at block sizes
default and 1. natemu only: the cases are judged against a model of the region written here and against the same program
on RAM (permissions).

    tests/system/run_mapped.py --fexc FEXC --out DIR [--only CASE,..]

- `mapped-load-store`: stores and loads of 1, 2, 4, 8, 16 and 32 bytes, an unaligned 16-byte store across two pages of
  the region and a 32-byte one, a store crossing from RAM into the region, unaligned `rep movsb` (from RAM) and
  `rep stosq` across pages, two aligned LOCK ADDs: the loads' values, the region's words (the driver's report, read
  through the object's second mapping) equal the model's, the control page's `SUM` (read by the guest) and the report's
  sum are the model's, no access to the region's range reached the host, and shadows were installed onto the region.
- `mapped-permissions`: a store through a read-only mapping of a region page (#PF, its error code and CR2) and a store
  through a D=0 mapping (D set by it, not by the read before it) give what the same program gives on RAM pages.
- `mapped-host-fill`: the guest writes the control page's `FILL` and reads the host's pattern (also on a page it had
  shadowed before), and `SUM`.
- `mapped-uc`: a UC mapping (PCD|PWT) of region pages runs natively: values right, no host access, no instruction run
  alone, mapped installs counted.
- `mapped-refusals`: each refusal of the audit gives its typed stop (the plan's Global constraints): an instruction
  fetch (`fetch_not_ram`), a page table in the region (`unsupported_mode`), a split lock (`unsupported_mmio` atomic), a
  store crossing from the region onto an unassigned page and one across two adjacent regions (`unsupported_mmio`
  crossing), a descriptor table, the IDT (INT3's gate), a system transaction's stack (POPF, IRETQ's frame), FXSAVE and
  XSAVE images, the resident's operand completions (RCL m
  with a CL count, ENTER with nesting, INS, MASKMOVDQU, FLD m80, LDMXCSR, VCVTPS2PH m, a gather's element)
  (`unsupported_mmio` system).
- `mapped-interpreter`: 32-bit protected-mode code with paging off, which the boot-time interpreter runs: its data
  accesses (a store and load, a store across two region pages, an aligned LOCK ADD) are the region's; a LOCK crossing
  16 bytes there (`unsupported_mmio` atomic) and a descriptor table there (the interpreter's device stop) are refused.
- `lapic_beats_mapped_region`: a region covering the xAPIC page: the vCPU's APIC answers there, the region the next page.
- `overlapping_regions_pick_lowest`: two overlapping regions: the lowest-numbered table entry (the first `--mapped-at`)
  holds the shared range.

Writes DIR/mapped.json and results.json; exit status 1 on any failure."""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'tests' / 'lib')); import testenv  # noqa: E401,E402 (tests/README.md)
import argparse, json, os, pathlib, sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'tools'), str(ROOT / 'tools' / 'reference'), str(ROOT / 'tools' / 'bench' / 'smp')]
import ref, run_slice1_vm as vm  # noqa: E402
import case_results  # noqa: E402
from run_slice3_vm import driver  # noqa: E402

# A CPL 0 program on its own tables (as run_l3.py's pages): 0-4 MiB identity in 2 MiB pages, then 4 KiB pages at WINDOW
# (slot k at WINDOW + 0x1000 k) onto any guest-physical page; PD entry 3 (linear 0x600000) names a region page as its
# page table. Every RAM page used is below 4 MiB (the 4 MiB suite runs every runner).
TABLES, WINDOW, BUF, RAMPG, RAMPERM, GDTPTR = 0x300000, 0x400000, 0x3d0000, 0x3c0000, 0x3b0000, 0x3a0000
R0, LEN, CTL = 0xD000_0000, 0x20_0000, 0xD100_0000
MASK64 = 2**64 - 1
FLAGS = 0x67           # P, R/W, U/S, A, D (executable: no NX)
SUM, FILL = 8, 16      # the control page's registers (test_devices::MappedControl)
BLOCKS = (None, 1)


def va(slot):
    return WINDOW + 0x1000 * slot


def pte_address(slot):
    return TABLES + 0x3000 + 8 * slot


def tables(slots, pt_page=R0 + 0x1f000):
    """The tables' memory items: `slots` {slot: (gpa, flags)}."""
    q = lambda *v: b''.join(x.to_bytes(8, 'little') for x in v)  # noqa: E731
    pt = bytearray(0x1000)
    for k, (gpa, flags) in slots.items():
        pt[8 * k:8 * k + 8] = q(gpa | flags)
    return [(TABLES, q((TABLES + 0x1000) | 0x27)), (TABLES + 0x1000, q((TABLES + 0x2000) | 0x27)),
            (TABLES + 0x2000, q(0xe7, 0x200000 | 0xe7, (TABLES + 0x3000) | 0x27, pt_page | 0x27)), (TABLES + 0x3000, bytes(pt))]


def description(text, slots, extra_memory=(), avx=False):
    code = ref.assemble(f'mov rax, {TABLES:#x}\nmov cr3, rax\n' + text + '\nhlt')
    memory = ''.join(f'memory {a:#x} {d.hex()}\n' for a, d in list(tables(slots)) + list(extra_memory))
    # Every RAM page the programs use is an identity page of the description (the driver's allocator leaves them alone).
    memory += ''.join(f'memory {p:#x} 00\n' for p in (RAMPG, RAMPERM, RAMPERM + 0x1000, GDTPTR) if not any(a == p for a, _ in extra_memory))
    return (f'identity\nbody 0x100000 {code.hex()}\nregion 0x3e0000 0x2000\n{memory}'
            f'cpu\nrip 0x100000\nrflags 0x2\ncpl 0\nsse 1\n{"xcr0 7" + chr(10) if avx else ""}gpr 0 0 0 0 0x3e1800 0 0 0 0 0 0 0 0 0 0 0\n')


GPR = ['rax', 'rcx', 'rdx', 'rbx', 'rsp', 'rbp', 'rsi', 'rdi', 'r8', 'r9', 'r10', 'r11', 'r12', 'r13', 'r14', 'r15']


def reg(c, r):
    return int(c['gpr'][GPR.index(r)], 0)


def halted(c):
    return [] if c['stop'] == 'resident' and c['reason'].startswith('halted') else [f"stop {c['stop']} {c['reason']} rip {c['rip']} fault {c.get('fault')} mmio {c.get('mmio')}"]


def words_of(model):
    return {8 * k: int.from_bytes(model[8 * k:8 * k + 8], 'little') for k in range(len(model) // 8) if any(model[8 * k:8 * k + 8])}


def region_problems(o, i, model, accesses=True):
    """Region `i` of the report against `model` (its bytes): the nonzero words, the sum, no host access."""
    r = o['mapped'][i]
    p = []
    got = {int(a, 0): int(v, 0) for a, v in r['words']}
    want = words_of(model)
    if got != want:
        diff = sorted(set(got.items()) ^ set(want.items()))[:6]
        p.append(f'region {i} words differ: {[(hex(a), hex(v), "got" if (a, v) in got.items() else "want") for a, v in diff]}')
    total = sum(int.from_bytes(model[k:k + 8], 'little') for k in range(0, len(model), 8)) & MASK64
    if int(r['sum'], 0) != total: p.append(f"region {i} sum {r['sum']} != {total:#x}")
    if accesses and r['accesses'] != [0, 0]: p.append(f"region {i} accesses reached the host: {r['accesses']}")
    return p, total


def installs(c):
    return [] if (c.get('mapped_installs') or 0) > 0 else [f"mapped_installs {c.get('mapped_installs')}"]


def run(a, name, text, extra, block):
    w = a.out / name / f'block-{block}'
    for attempt in range(2):  # one retry on a concurrent hv_vm_create (0xfae94007)
        rc, out, err = driver(a.host, a.runtime, a.fexc, text, w, ([block] if block else []) + list(extra), timeout=120)
        if rc == 0 or 'fae94007' not in str(err): break
    if rc != 0:
        return None, [f'driver {rc}: {str(err).strip()[-400:]}']
    return out, []


MAPPED = ['--mapped-at', f'{R0:#x},{LEN:#x}', '--mapped-control-at', f'{CTL:#x}']


# ---- mapped-load-store ---------------------------------------------------------------------------------------------
def load_store():
    slots = {k: (R0 + 0x1000 * k, FLAGS) for k in range(7)}
    slots |= {8: (RAMPG, FLAGS), 9: (R0 + 0x8000, FLAGS), 31: (CTL, FLAGS)}
    pattern = bytes((7 * i + 3) & 0xff for i in range(512))
    w = va
    text = f'''mov rbx, {w(0):#x}
mov byte ptr [rbx + 1], 0x11
mov word ptr [rbx + 2], 0x2233
mov dword ptr [rbx + 4], 0x44556677
mov rax, 0x8899aabbccddeeff
mov qword ptr [rbx + 8], rax
movzx r8d, byte ptr [rbx + 1]
movzx r9d, word ptr [rbx + 2]
mov r10d, dword ptr [rbx + 4]
mov r11, qword ptr [rbx + 8]
mov rax, 0x0706050403020100
mov rcx, 0x0f0e0d0c0b0a0908
movq xmm0, rax
movq xmm1, rcx
punpcklqdq xmm0, xmm1
movq xmm4, rcx
movq xmm5, rax
punpcklqdq xmm4, xmm5
movdqa xmmword ptr [rbx + 0x20], xmm0
movdqu xmmword ptr [rbx + 0xff8], xmm0
movdqu xmm2, xmmword ptr [rbx + 0xff8]
movq r12, xmm2
psrldq xmm2, 8
movq r13, xmm2
vinsertf128 ymm3, ymm0, xmm4, 1
vmovdqu ymmword ptr [rbx + 0x1ff0], ymm3
xor ebp, ebp
vmovdqu ymm6, ymmword ptr [rbx + 0x1ff0]
vxorps ymm7, ymm6, ymm3
vptest ymm7, ymm7
setz bpl
mov rdx, {w(8):#x}
mov rax, 0x1122334455667788
mov qword ptr [rdx + 0xffc], rax
mov r14, qword ptr [rdx + 0xffc]
mov rsi, {BUF + 1:#x}
mov rdi, {w(2) + 0xf83:#x}
mov ecx, 300
cld
rep movsb
mov rdi, {w(3) + 0xf05:#x}
mov rax, 0x0123456789abcdef
mov ecx, 40
rep stosq
lock add qword ptr [rbx + 0x100], 5
lock add qword ptr [rbx + 0x100], 7
mov r15, qword ptr [rbx + 0x100]
mov rsi, {w(31):#x}
mov rax, qword ptr [rsi + {SUM}]'''
    model = bytearray(LEN)
    def put(off, b): model[off:off + len(b)] = b
    put(1, b'\x11'); put(2, (0x2233).to_bytes(2, 'little')); put(4, (0x44556677).to_bytes(4, 'little'))
    put(8, (0x8899aabbccddeeff).to_bytes(8, 'little'))
    x0 = bytes(range(16)); x4 = bytes(range(8, 16)) + bytes(range(8))
    put(0x20, x0); put(0xff8, x0); put(0x1ff0, x0 + x4)
    put(0x8000, (0x1122334455667788).to_bytes(8, 'little')[4:])
    put(0x2f83, pattern[1:301])
    put(0x3f05, (0x0123456789abcdef).to_bytes(8, 'little') * 40)
    put(0x100, (12).to_bytes(8, 'little'))
    want = {'r8': 0x11, 'r9': 0x2233, 'r10': 0x44556677, 'r11': 0x8899aabbccddeeff, 'r12': 0x0706050403020100,
            'r13': 0x0f0e0d0c0b0a0908, 'r14': 0x1122334455667788, 'r15': 12, 'rbp': 1}
    def check(o):
        c = o['cpus'][0]
        p = halted(c) + installs(c)
        rp, total = region_problems(o, 0, model)
        p += rp
        p += [f'{r} {reg(c, r):#x} != {v:#x}' for r, v in want.items() if reg(c, r) != v]
        if reg(c, 'rax') != total: p.append(f"guest's SUM {reg(c, 'rax'):#x} != {total:#x}")
        return p
    return [('mapped-load-store', description(text, slots, [(BUF, pattern)], avx=True), MAPPED, check)]


# ---- mapped-permissions --------------------------------------------------------------------------------------------
def permissions():
    out = []
    for kind, (slot, flags) in {'read-only': (12, 0x65), 'dirty': (13, 0x27)}.items():
        variants = {}
        for target, gpa in (('region', R0 + 0x1000 * slot), ('ram', RAMPERM + 0x1000 * (slot - 12))):
            slots = {slot: (gpa, flags)}
            text = (f'mov rbx, {va(slot):#x}\nmov r8, qword ptr [rbx + 0x10]\nmov r11, qword ptr [{pte_address(slot):#x}]\n'
                    f'mov qword ptr [rbx + 0x18], rbx\nmov r9, qword ptr [{pte_address(slot):#x}]\nmov r10, qword ptr [rbx + 0x18]')
            variants[target] = description(text, slots)
        out.append((kind, variants, slot))
    def fields(c):
        f = [int(x, 0) for x in c.get('fault') or []]
        return {'stop': c['stop'], 'reason': c['reason'].split()[0] if c['reason'] else '', 'rip': c['rip'], 'fault': f[:3],
                'r8': reg(c, 'r8'), 'r9': reg(c, 'r9') & 0xfff, 'r10': reg(c, 'r10'), 'r11': reg(c, 'r11') & 0xfff}
    cases = []
    for kind, variants, slot in out:
        def check(both, kind=kind, slot=slot):
            region, ram = (fields(both[t]['cpus'][0]) for t in ('region', 'ram'))
            p = [f'{k}: region {region[k]} != RAM {ram[k]}' for k in region if region[k] != ram[k]]
            if kind == 'read-only':
                if region['stop'] != 'guest_fault' or region['fault'][:1] != [14] or 3 not in region['fault'][1:] or va(slot) + 0x18 not in region['fault'][1:]:
                    p.append(f'want #PF error 3 at {va(slot) + 0x18:#x}: {region}')
            else:
                if region['stop'] != 'resident' or region['r11'] & 0x40 or not region['r9'] & 0x40 or region['r10'] != va(slot):
                    p.append(f'want D clear after the read and set by the store: {region}')
            if not both['region']['mapped'][0]['accesses'] == [0, 0]: p.append(f"accesses {both['region']['mapped'][0]['accesses']}")
            return p
        cases.append((f'mapped-permissions/{kind}', variants, MAPPED, check))
    return cases


# ---- mapped-host-fill ----------------------------------------------------------------------------------------------
def host_fill():
    value = 0x5a5a_1234_dead_beef
    slots = {0: (R0, FLAGS), 6: (R0 + 0x6000, FLAGS), 31: (CTL, FLAGS)}
    text = (f'mov rbx, {va(0):#x}\nmov r12, qword ptr [rbx]\nmov rsi, {va(31):#x}\nmov rax, {value:#x}\nmov qword ptr [rsi + {FILL}], rax\n'
            f'mov r8, qword ptr [rbx]\nmov r9, qword ptr [rbx + 8]\nmov r10, qword ptr [rbx + 0xff8]\nmov rdx, {va(6):#x}\n'
            f'mov r11, qword ptr [rdx + 0x10]\nmov rax, qword ptr [rsi + {SUM}]')
    fill = lambda k: value ^ (k * 0x9e37_79b9_7f4a_7c15) & MASK64  # noqa: E731 (test_devices::fill_word)
    want = {'r12': 0, 'r8': fill(0), 'r9': fill(1), 'r10': fill(0xff8 // 8), 'r11': fill(0x6010 // 8)}
    def check(o):
        c = o['cpus'][0]
        p = halted(c) + installs(c)
        p += [f'{r} {reg(c, r):#x} != {v:#x}' for r, v in want.items() if reg(c, r) != v]
        r = o['mapped'][0]
        total = sum(fill(k) for k in range(LEN // 8)) & MASK64
        if int(r['sum'], 0) != total or reg(c, 'rax') != total: p.append(f"sum {r['sum']} guest {reg(c, 'rax'):#x} != {total:#x}")
        if r['accesses'] != [0, 0]: p.append(f"accesses {r['accesses']}")
        # (The report's words are the first 4096 nonzero ones: the pattern's.)
        got = {int(a, 0): int(v, 0) for a, v in r['words']}
        if any(got.get(8 * k) != fill(k) for k in range(64)): p.append('the report does not show the pattern')
        return p
    return [('mapped-host-fill', description(text, slots), MAPPED, check)]


# ---- mapped-uc -----------------------------------------------------------------------------------------------------
def uncached():
    uc = FLAGS | 0x18  # PWT and PCD: PAT entry 3, UC
    slots = {16: (R0 + 0x10000, uc), 17: (R0 + 0x11000, uc)}
    text = (f'mov rbx, {va(16):#x}\nmov rax, 0x1111222233334444\nmov qword ptr [rbx + 8], rax\nmov r8, qword ptr [rbx + 8]\n'
            f'mov dword ptr [rbx + 0xffe], 0x55667788\nmov r9d, dword ptr [rbx + 0xffe]\nlea rdi, [rbx + 0x101]\nmov al, 0x99\n'
            f'mov ecx, 64\nrep stosb\nlock add qword ptr [rbx + 8], 1\nmov r10, qword ptr [rbx + 8]')
    model = bytearray(LEN)
    model[0x10008:0x10010] = (0x1111222233334445).to_bytes(8, 'little')
    model[0x10ffe:0x11002] = (0x55667788).to_bytes(4, 'little')
    model[0x10101:0x10141] = b'\x99' * 64
    want = {'r8': 0x1111222233334444, 'r9': 0x55667788, 'r10': 0x1111222233334445}
    def check(o):
        c = o['cpus'][0]
        p = halted(c) + installs(c) + region_problems(o, 0, model)[0]
        p += [f'{r} {reg(c, r):#x} != {v:#x}' for r, v in want.items() if reg(c, r) != v]
        k1 = c['stats']['k1']
        if k1['uncached'] != {'steps': 0, 'resident': 0}: p.append(f"run alone or resident UC accesses: {k1['uncached']}")
        return p
    return [('mapped-uc', description(text, slots), MAPPED, check)]


# ---- mapped-refusals -----------------------------------------------------------------------------------------------
def refusals():
    # Slots 40, 41: the region's last page and the page after it (unassigned, or a second region's first).
    slots = {0: (R0, FLAGS), 10: (R0 + 0xa000, FLAGS), 40: (R0 + LEN - 0x1000, FLAGS), 41: (R0 + LEN, FLAGS)}
    base = va(0)
    cross = f'mov rax, -1\nmov qword ptr [{va(40) + 0xffc:#x}], rax'
    iret = (f'mov rsp, {base + 0x800:#x}\nmov ax, ss\nmovzx eax, ax\npush rax\nlea rax, [rsp + 8]\npush rax\npushfq\n'
            'mov ax, cs\nmovzx eax, ax\npush rax\nlea rax, [rip + 1f]\npush rax\niretq\n1:')
    gdt = (0xffff).to_bytes(2, 'little') + base.to_bytes(8, 'little')
    cases = {
        'fetch': (f'mov rax, {va(10):#x}\ncall rax', ('no_code', 'fetch_not_ram', None)),
        'page-table': ('mov rax, qword ptr [0x600000]', ('resident', 'unsupported_mode', None)),
        'split-lock': (f'mov rbx, {base:#x}\nmov eax, 1\nlock xadd dword ptr [rbx + 0x0e], eax', ('resident', 'unsupported_mmio', 'atomic')),
        'descriptor-table': (f'lgdt [{GDTPTR:#x}]\nmov ax, 0x10\nmov ds, ax', ('resident', 'unsupported_mmio', 'system')),
        'idt': (f'lidt [{GDTPTR:#x}]\nint3', ('resident', 'unsupported_mmio', 'system')),
        'iret-frame': (iret, ('resident', 'unsupported_mmio', 'system')),
        'xsave-image': (f'mov rbx, {base:#x}\nmov eax, -1\nmov edx, -1\nxsave [rbx]', ('resident', 'unsupported_mmio', 'system')),
        'crossing-device': (cross, ('resident', 'unsupported_mmio', 'crossing')),
        'crossing-regions': (cross, ('resident', 'unsupported_mmio', 'crossing')),
        'popf-stack': (f'mov rsp, {base + 0x800:#x}\nmov qword ptr [rsp], 2\npopfq', ('resident', 'unsupported_mmio', 'system')),
        'fxsave-image': (f'mov rbx, {base:#x}\nfxsave [rbx]', ('resident', 'unsupported_mmio', 'system')),
        'rcl-operand': (f'mov rbx, {base:#x}\nmov cl, 3\nrcl qword ptr [rbx + 8], cl', ('resident', 'unsupported_mmio', 'system')),
        'enter-frame': (f'mov rsp, {base + 0x800:#x}\nmov rbp, rsp\nenter 16, 2', ('resident', 'unsupported_mmio', 'system')),
        'ins-destination': (f'mov dx, 0x3ff\nmov rdi, {base:#x}\ninsb', ('resident', 'unsupported_mmio', 'system')),
        'maskmov-destination': (f'mov rdi, {base:#x}\npcmpeqb xmm1, xmm1\nmaskmovdqu xmm0, xmm1', ('resident', 'unsupported_mmio', 'system')),
        'x87-m80': (f'mov rbx, {base:#x}\nfld tbyte ptr [rbx]', ('resident', 'unsupported_mmio', 'system')),
        'ldmxcsr-operand': (f'mov rbx, {base:#x}\nldmxcsr dword ptr [rbx]', ('resident', 'unsupported_mmio', 'system')),
        'vcvtps2ph-destination': (f'mov rbx, {base:#x}\nvcvtps2ph qword ptr [rbx], xmm0, 0', ('resident', 'unsupported_mmio', 'system')),
        'gather-element': (f'mov rbx, {base:#x}\nvpcmpeqd ymm2, ymm2, ymm2\nvpxor ymm1, ymm1, ymm1\nvpgatherdd ymm0, [rbx + ymm1*4], ymm2',
                           ('resident', 'unsupported_mmio', 'system')),
    }
    avx = {'vcvtps2ph-destination', 'gather-element', 'xsave-image'}
    # Two adjacent regions: the second's first page follows the first's last.
    two = ['--mapped-at', f'{R0:#x},{LEN:#x}', '--mapped-at', f'{R0 + LEN:#x},{LEN:#x}']
    out = []
    for name, (text, (stop, reason, status)) in cases.items():
        def check(o, stop=stop, reason=reason, status=status):
            c = o['cpus'][0]
            got = (c['stop'], c['reason'], (c.get('mmio') or {}).get('status'))
            ok = c['stop'] == stop and reason in c['reason'] and (status is None or got[2] == status)
            return [] if ok else [f'want {stop} {reason} {status}: got {got} rip {c["rip"]}']
        extra = [(GDTPTR, gdt)] if name in ('descriptor-table', 'idt') else []
        out.append((f'mapped-refusals/{name}', description(text, slots, extra, avx=name in avx), two if name == 'crossing-regions' else MAPPED, check))
    return out


# ---- lapic_beats_mapped_region and overlapping_regions_pick_lowest -------------------------------------------------
def lapic():
    apic = 0xFEE0_0000
    slots = {20: (apic, FLAGS), 21: (apic + 0x1000, FLAGS)}
    text = (f'mov r8d, dword ptr [{va(20) + 0x30:#x}]\nmov rbx, {va(21):#x}\nmov qword ptr [rbx + 8], rbx\nmov r9, qword ptr [rbx + 8]\n'
            f'mov dword ptr [{va(20) + 0x80:#x}], 0x20\nmov r10d, dword ptr [{va(20) + 0x80:#x}]')
    model = bytearray(LEN)
    model[0x1008:0x1010] = va(21).to_bytes(8, 'little')
    want = {'r8': 0x50014, 'r9': va(21), 'r10': 0x20}
    def check(o):
        c = o['cpus'][0]
        p = halted(c) + installs(c) + region_problems(o, 0, model)[0]
        return p + [f'{r} {reg(c, r):#x} != {v:#x}' for r, v in want.items() if reg(c, r) != v]
    return [('lapic_beats_mapped_region', description(text, slots), ['--mapped-at', f'{apic:#x},{LEN:#x}'], check)]


def overlapping():
    first, second = 0xD010_0000, 0xD000_0000  # table entries 0 and 1: [D0100000, D0300000) and [D0000000, D0200000)
    slots = {24: (0xD008_0000, FLAGS), 25: (0xD018_0000, FLAGS), 26: (0xD028_0000, FLAGS)}
    text = (f'mov qword ptr [{va(24):#x}], 1\nmov qword ptr [{va(25):#x}], 2\nmov qword ptr [{va(26):#x}], 3\n'
            f'mov r8, qword ptr [{va(24):#x}]\nmov r9, qword ptr [{va(25):#x}]\nmov r10, qword ptr [{va(26):#x}]')
    m0, m1 = bytearray(LEN), bytearray(LEN)
    m0[0x80000:0x80008] = (2).to_bytes(8, 'little')
    m0[0x180000:0x180008] = (3).to_bytes(8, 'little')
    m1[0x80000:0x80008] = (1).to_bytes(8, 'little')
    def check(o):
        c = o['cpus'][0]
        p = halted(c) + installs(c) + region_problems(o, 0, m0)[0] + region_problems(o, 1, m1)[0]
        return p + [f'{r} {reg(c, r)} != {v}' for r, v in {'r8': 1, 'r9': 2, 'r10': 3}.items() if reg(c, r) != v]
    extra = ['--mapped-at', f'{first:#x},{LEN:#x}', '--mapped-at', f'{second:#x},{LEN:#x}']
    return [('overlapping_regions_pick_lowest', description(text, slots), extra, check)]


# ---- mapped-interpreter -------------------------------------------------------------------------------------------
def interpreter():
    """32-bit protected-mode code with paging off (the boot-time interpreter's), on a machine description: the core's data
    accesses to the region complete as RAM's; a LOCK crossing 16 bytes and a descriptor table there are refused."""
    code_at, ptr = 0x10000, 0x20000
    def machine(text, memory=()):
        code = ref.assemble('.code32\n' + text + '\nnop')
        lines = ['machine', f'memory {code_at:#x} {code.hex()}', *[f'memory {a:#x} {d.hex()}' for a, d in memory],
                 f'rip {code_at:#x}', 'rflags 0x2', 'gpr ' + ' '.join(['0'] * 16), 'cpl 0', 'cr0 0x11', 'cr3 0x0', 'cr4 0x0',
                 'efer 0x0', 'seg cs 0x8 0x0 0xffffffff 0xc09b',
                 *[f'seg {r} 0x10 0x0 0xffffffff 0xc093' for r in ('es', 'ss', 'ds', 'fs', 'gs')],
                 'gdtr 0x0 0x0', 'idtr 0x0 0x0', f'stop {code_at + len(code) - 1:#x}']
        return '\n'.join(lines) + '\n'
    data = (f'mov dword ptr [{R0 + 0x10:#x}], 0x12345678\nmov eax, dword ptr [{R0 + 0x10:#x}]\n'
            f'mov word ptr [{R0 + 0xfff:#x}], 0xabcd\nmovzx ebx, word ptr [{R0 + 0xfff:#x}]\n'
            f'lock add dword ptr [{R0 + 0x20:#x}], 3\nmov ecx, dword ptr [{R0 + 0x20:#x}]')
    model = bytearray(LEN)
    model[0x10:0x14] = (0x12345678).to_bytes(4, 'little')
    model[0xfff:0x1001] = (0xabcd).to_bytes(2, 'little')
    model[0x20:0x24] = (3).to_bytes(4, 'little')
    def check_data(o):
        c = o.get('cpus', [o])[0]
        m = c.get('machine') or {}
        p = [] if m.get('stop') == 0 else [f"stop {c['stop']} {c['reason']} machine stop {m.get('stop')}"]
        p += region_problems(o, 0, model)[0]
        want = {'eax': 0x12345678, 'ebx': 0xabcd, 'ecx': 3}
        return p + [f'{r} {m.get(r)} != {v:#x}' for r, v in want.items() if m.get(r) is None or int(str(m[r]), 0) != v]
    def refused(reason, status):
        def check(o):
            c = o.get('cpus', [o])[0]
            got = (c['stop'], c['reason'], (c.get('mmio') or {}).get('status'))
            ok = reason in c['reason'] and (status is None or got[2] == status) and (c.get('machine') or {}).get('stop') is None
            return [] if ok else [f'want {reason} {status}: got {got}']
        return check
    gdt = (0xffff).to_bytes(2, 'little') + R0.to_bytes(4, 'little')
    return [('mapped-interpreter/data', machine(data), MAPPED, check_data),
            ('mapped-interpreter/split-lock', machine(f'lock add dword ptr [{R0 + 0x0e:#x}], 1'), MAPPED, refused('unsupported_mmio', 'atomic')),
            ('mapped-interpreter/descriptor-table', machine(f'lgdt [{ptr:#x}]\nmov ax, 0x10\nmov ds, ax', [(ptr, gdt)]), MAPPED,
             refused('interp_memory', None))]


CASES = {'mapped-load-store': load_store, 'mapped-permissions': permissions, 'mapped-host-fill': host_fill,
         'mapped-uc': uncached, 'mapped-refusals': refusals, 'mapped-interpreter': interpreter, 'lapic_beats_mapped_region': lapic,
         'overlapping_regions_pick_lowest': overlapping}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--fexc', type=pathlib.Path, required=True)
    ap.add_argument('--out', type=pathlib.Path, required=True)
    ap.add_argument('--host'); ap.add_argument('--runtime')
    ap.add_argument('--only', type=lambda s: s.split(','), default=list(CASES))
    a = ap.parse_args()
    if not (a.host and a.runtime): a.host, a.runtime = vm.build()
    a.out.mkdir(parents=True, exist_ok=True)
    report, failures = {'cases': []}, []
    for group in a.only:
        for name, text, extra, check in CASES[group]():
            for block in BLOCKS:
                if isinstance(text, dict):  # variants of one program, judged together
                    outs, problems = {}, []
                    for target, t in text.items():
                        outs[target], p = run(a, f'{name}-{target}', t, extra, block)
                        problems += p
                    problems = problems or check(outs)
                    detail = {t: o and {k: o['cpus'][0].get(k) for k in ('stop', 'reason', 'rip', 'fault')} for t, o in outs.items()}
                else:
                    o, problems = run(a, name, text, extra, block)
                    problems = problems or check(o)
                    detail = o and {k: o.get('cpus', [o])[0].get(k) for k in ('stop', 'reason', 'rip', 'fault', 'mmio', 'mapped_installs')} | {'mapped': [{k: r[k] for k in ('base', 'accesses', 'sum')} for r in o.get('mapped', [])]}
                report['cases'].append({'name': f'{name}/block-{block}', 'detail': detail, 'problems': problems})
                failures += [f'{name} block={block}: {d}' for d in problems]
                print(f"{name} block={block}: {'ok' if not problems else problems}", flush=True)
    report['failures'] = failures
    (a.out / 'mapped.json').write_text(json.dumps(report, indent=1, default=str) + '\n')
    case_results.write_report(a.out, report, failures)
    print(f'{len(failures)} failures')
    sys.exit(1 if failures else 0)


if __name__ == '__main__':
    testenv.require_framework(__file__)
    main()
