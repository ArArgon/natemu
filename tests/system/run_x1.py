#!/usr/bin/env python3
"""X1b and X1c checks (boot-features plan, X1b, X1c; programs in tools/bench/smp/entry.py).

    tests/system/run_x1.py --fexc FEXC --out DIR [--only qemu,cases]

- `qemu` (the name kept from when pinned QEMU ran these programs too; natemu only now): `x1-entry@1/2/4` on natemu at
  block sizes default and 1: CPL 3 -> 0 frames on RSP0, IST1, IST3, IST5..7 and CPL 0 -> 0 frames on IST1..4 (IST
  selection does not depend on the CPL), IRETQ to CPL 3 after each, the normal (SWAPGS) and paranoid (RDGSBASE, LSL of
  the CPU/node descriptor, WRGSBASE) entry shapes, #DF from CPL 3 and CPL 0, NMI blocking until IRETQ. Every logged
  frame must be delivery_model's given the logged context: the vector chain and error code, RF, and the RIP slot's
  address (the stack chosen: RSP0, IST n or the current one, aligned); the GS base a paranoid entry found (U at CPL 3
  and between the CPL 0 SWAPGS pair, K otherwise); the entry's
  per-CPU values (stamp, LSL = CPU number) and the CPL 3 results (LSL, RDGSBASE, [gs:0]) as the program sets them; the
  second NMI's frame RIP equals the first's. X1c's PUSH/POP FS/GS, Reserved-NOP and BND programs: the SDM's registers.
  (Pinned QEMU, once compared here, pushes RF = 1 for INT n/INT3 after an IRETQ that loaded RF = 1 and for NMIs; the
  SDM pushes 0 for INT n/INT3, Vol.3 "RF", and the current RF for an NMI.)
- `cases`: X1c's refused-but-defined cases, now delivered (SDM rules, natemu only; each was a stop before X1c):
  the #UD encodings (PUSH/POP ES/CS/SS/DS, CALL/JMP ptr16:32 in 64-bit mode, LSS/LFS/LGS with a register operand, the
  0f 01 register forms no advertised feature defines, 0f 01 /5 m without F3, 0f 00 /7) and CLTS, SMSW, LMSW, SYSENTER,
  SYSEXIT with their faults and effects; CR4.UMIP (`umip-*`): SGDT, SIDT, SLDT, SMSW, STR run at CPL 0 and #GP(0) at CPL 3.

Writes DIR/x1.json; exit status 1 on any failure."""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'tests' / 'lib')); import testenv  # noqa: E401,E402 (tests/README.md)
import argparse, json, pathlib, sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'tools'), str(ROOT / 'tools' / 'reference'), str(ROOT / 'tools' / 'bench' / 'smp')]
import case_results, ref, smp, entry, programs as P, run_slice1_vm as vm  # noqa: E402
from run_slice3_vm import driver, per_vcpu_checks, body_end, smp_description, vm_state  # noqa: E402
from models import delivery_model as dm  # noqa: E402


def model_problems(prog, memory):
    """Every CPU's log against entry.expected and delivery_model; `memory` = {address: bytes} of the captured regions."""
    out = []
    read = lambda at, n: next(d[at - a:at - a + n] for a, d in memory.items() if a <= at and at + n <= a + len(d))
    for i in range(prog['ncpu']):
        d = P.SYS_D(i)
        log = entry.log(read(d + entry.LOG, 0x1000))
        want = entry.expected(i)
        if len(log) != len(want):
            out.append(f'cpu{i}: {len(log)} events logged, {len(want)} expected ({[hex(e[0]) for e in log]})')
        for k, (e, (event, length)) in enumerate(zip(log, want)):
            vector, error, rip, cs, rflags, rsp, ss, slot, x1, x2 = e
            s = entry.model_state(i, e)
            s['rip'] = rip - length
            r = dm.deliver(s, event)
            if r['outcome'] != 'delivered':
                out.append(f'cpu{i} event {k}: model {r["outcome"]} {r["events"]}'); continue
            v, err = r['events'][-1]
            n = r['state']
            at = n['rsp'] + (8 if err is not None else 0)
            undefined = dict(r['undefined']['frame'])
            words = [dm.qword(n['memory'], at + 8 * j) for j in range(5)]
            bad = [j for j, (g, w) in enumerate(zip((rip, cs, rflags, rsp, ss), words))
                   if g != w and not (at + 8 * j in undefined and (undefined[at + 8 * j] is None or g in undefined[at + 8 * j]))]
            if (vector, error if err is not None else None) != (v, err) or slot != at or bad:
                out.append(f'cpu{i} event {k}: logged {[hex(x) for x in e[:8]]}, model {[hex(v), err] + [hex(w) for w in words]} slot {at:#x} (words {bad})')
            if vector in entry.PARANOID:
                # The GS base live at the event (SDM Vol.2 SWAPGS): U at CPL 3 and for the last event, the #DF between
                # the CPL 0 SWAPGS pair; K for the other CPL 0 events. CPL 3 is read from the frame's SS RPL: #DF's
                # saved CS is undefined (SDM Vol.3 7.15, interrupt 8), and the handler logs it before rewriting it.
                base = d + (entry.U if ss & 3 == 3 or k == len(want) - 1 else entry.K)
                if x1 != base or x2 & 0xffff != i or x2 >> 16 & 0xffff != 0x100 + i or x2 >> 32:
                    out.append(f'cpu{i} event {k} (paranoid {vector:#x}): GS base found {x1:#x} (model {base:#x}), x2 {x2:#x}')
            elif x1 != 0x100 + i or x2 != (P.SYS_CODE(i) if vector == 14 else 0):
                out.append(f'cpu{i} event {k} (normal {vector:#x}): x1 {x1:#x} x2 {x2:#x}')
        nmis = [e[2] for e in log if e[0] == 2]
        if len(nmis) == 2 and nmis[0] != nmis[1]:
            out.append(f'cpu{i}: the second NMI was taken at {nmis[1]:#x}, the first at {nmis[0]:#x} (blocked until IRETQ: the same boundary)')
        res = [int.from_bytes(read(d + entry.RES + 8 * j, 8), 'little') for j in range(3)]
        if res != [i, d + entry.U, 0x200 + i]:
            out.append(f'cpu{i}: CPL 3 results {[hex(x) for x in res]} != {[hex(x) for x in (i, d + entry.U, 0x200 + i)]}')
    return out


def cmd_qemu(a, report):
    failures = []
    for n in (1, 2, 4):
        prog = entry.program(n)
        label = prog['name']
        e = {'name': label, 'runs': []}
        for limit in (None, 1):
            rc, out, err = driver(a.host, a.runtime, a.fexc, smp_description(prog), a.out / 'work' / f"{label}-{limit or 'd'}", [limit] if limit else [])
            if rc:
                problems = [f'driver exit {rc}: {err.strip()[-400:]}']
            else:
                problems = []
                for v, c in enumerate(out['cpus']):
                    end = body_end(prog, smp.num(prog['cpus'][v]['rip']))
                    if c['stop'] != 'reached' or smp.num(c['rip']) != end:
                        problems.append(f"cpu{v} stopped {c['stop']} {c['reason']} at {c['rip']} (end {end:#x})")
                    problems += [f'cpu{v} ' + d for d in per_vcpu_checks(c)]
                got = vm_state(prog, out)
                problems += model_problems(prog, {r['addr']: r['data'] for r in got['memory']})
            e['runs'].append({'max_block': limit, 'problems': problems})
            failures += [f'{label} (max-block {limit}): {p}' for p in problems]
        print(f"{label}: {'FAIL' if any(r['problems'] for r in e['runs']) else 'ok'}",
              *[p for r in e['runs'] for p in r['problems']][:8], sep='\n  ', flush=True)
        report.append(e)
    # X1c: PUSH/POP FS/GS, PREFETCHW, CLDEMOTE: the SDM's registers; the Reserved-NOP cells: every one completes (R14 = 1,
    # RBX/R8/R9 kept); BND: every path ran.
    for prog, expected in ((entry.sreg_program(), entry.sreg_expected()), (entry.rnop_program(), {'r14': 1, 'r8': 0x1111, 'r9': 0x2222, 'rbx': 0x7f0000}),
                             (entry.bnd_program(), {r: 1 for r in ('r8', 'r9', 'r10', 'r11', 'r12', 'r13')})):
        failures += plain_program(a, report, prog, expected)
    return failures


def plain_program(a, report, prog, expected):
    """One CPL 0 program against `expected` ({register: value}) at block sizes default and 1."""
    failures = []
    label = prog['name']
    e = {'name': label, 'runs': []}
    sdm = lambda gpr: [f'{r} {gpr[P.REGS.index(r)]:#x} != {v:#x}' for r, v in expected.items() if gpr[P.REGS.index(r)] != v]
    for limit in (None, 1):
        rc, out, err = driver(a.host, a.runtime, a.fexc, smp_description(prog), a.out / 'work' / f"{label}-{limit or 'd'}", [limit] if limit else [])
        if rc:
            problems = [f'driver exit {rc}: {err.strip()[-400:]}']
        else:
            c = out['cpus'][0]
            end = body_end(prog, smp.num(prog['cpus'][0]['rip']))
            problems = [] if c['stop'] == 'reached' and smp.num(c['rip']) == end else [f"stopped {c['stop']} {c['reason']} at {c['rip']} (end {end:#x})"]
            problems += sdm([smp.num(x) for x in vm_state(prog, out)['cpus'][0]['gpr']])
        e['runs'].append({'max_block': limit, 'problems': problems})
        failures += [f'{label} (max-block {limit}): {p}' for p in problems]
    print(f"{label}: {'FAIL' if any(r['problems'] for r in e['runs']) else 'ok'}",
          *[p for r in e['runs'] for p in r['problems']][:8], sep='\n  ', flush=True)
    report.append(e)
    return failures


# ---- cases: refused-but-defined -> delivered -------------------------------------------------------------------------
CODE = 0x40_0000


def run(a, text, work, limit, extra='', rip=CODE):
    """One CPL 0 program at `rip` (driver space; the logging IDT): (out, problems)."""
    code = ref.assemble(text) if isinstance(text, str) else text
    desc = f'rip {rip:#x}\ncpl 0\nrflags 0x2\ncode {code.hex()}\nregion 0x500000 0x1000\n' + extra
    rc, out, err = driver(a.host, a.runtime, a.fexc, desc, work, [limit] if limit else [])
    return (out, []) if not rc else (None, [f'driver exit {rc}: {err.strip()[-300:]}'])


def fault(out):
    return out and out['stop'] == 'guest_fault' and [smp.num(x) for x in out['fault']]


def cases():
    """(name, code (text or bytes), extra description lines, check(out) -> problems)."""
    out = []

    def ud_at(at):
        return lambda o: [] if fault(o) == [6, 0, 0, at] else [f"want #UD at {at:#x}, got {o['stop']} {o['reason']} {o.get('fault')}"]
    # #UD encodings (SDM Vol.2 opcode tables "Invalid" in 64-bit mode, PUSH "#UD If the PUSH is of CS, SS, DS, or ES";
    # Vol.3 7.15 #UD: an invalid or reserved opcode, an operand type invalid for its opcode (LSS/LFS/LGS register);
    # Vol.2 Table A-6 group 7 / group 6 blanks and unadvertised members). Each is preceded by a NOP: RIP is the encoding's.
    uds = {'push-es': '06', 'pop-es': '07', 'push-cs': '0e', 'push-ss': '16', 'pop-ss': '17', 'push-ds': '1e', 'pop-ds': '1f',
           'call-ptr1632': '9a0000000008 00'.replace(' ', ''), 'jmp-ptr1632': 'ea000000000800',
           'lss-reg': '0fb2c0', 'lfs-reg': '0fb4c0', 'lgs-reg': '0fb5c0',
           '0f01c7': '0f01c7', '0f01cd': '0f01cd', '0f01ce': '0f01ce', '0f01d2': '0f01d2', '0f01d3': '0f01d3',
           '0f01eb': '0f01eb', '0f01ec': '0f01ec', '0f01ed': '0f01ed', 'f3-0f01ec': 'f30f01ec',
           '0f01-5m': '0f012d00000500', '66-0f01-5m': '660f012d00000500', '0f00-7': '0f00f8', '0f00-7m': '0f003d00000500'}
    # Forms fexc refuses (`unsupported_form`) that the SDM makes #UD whatever their operands (host `system::undefined`):
    # Table A-3/A-6 blanks, register operands of memory-only forms (LEA, MOVNTI, MOVLPS/MOVHPS/MOVLPD/MOVHPD stores, MOVNTPS,
    # MOVNTDQ, LDDQU, MOVNTDQA, CMPXCHG8B/16B, the far JMP/CALL register forms fexc cuts as system), GETSEC without
    # CR4.SMXE, RSM outside SMM, the VMX group-9 forms, `d6`, unprefixed 0f 38 / 0f 3a forms nothing advertised defines.
    uds |= {'lea-reg': '8dc0', 'd6': 'd6', 'call-far-reg': 'ffd8', 'jmp-far-reg': 'ffe8', 'jmp-far-reg-66': '66ffe8',
            '0f04': '0f04', '0f0a': '0f0a', '0f0c': '0f0c', '0f0e': '0f0e', '0f24': '0f24', '0f25': '0f25', '0f26': '0f26',
            '0f27': '0f27', '0f36': '0f36', '0f39': '0f39', '0f3b': '0f3b', '0f3c': '0f3c', '0f3d': '0f3d', '0f3e': '0f3e',
            '0f3f': '0f3f', '0f7a': '0f7a', '0f7b': '0f7b', 'getsec': '0f37', 'rsm': '0faa',
            'movnti-reg': '0fc3c0', 'movlps-reg': '0f13c0', 'movlpd-reg': '660f13c0', 'movhpd-reg': '660f17c0',
            'movntps-reg': '0f2bc0', 'movntpd-reg': '660f2bc0', 'movntdq-reg': '660fe7c0', 'lddqu-reg': 'f20ff0c0',
            'movntdqa-reg': '660f382ac0', 'cmpxchg8b-reg': '0fc7c8', 'cmpxchg16b-reg': '480fc7c8', '0fc7-0r': '0fc7c0',
            '0fc7-5r': '0fc7e8', '0fc7-0m': '0fc70500000500', '0fc7-2m': '0fc71500000500', 'vmptrld': '0fc73500000500',
            'vmclear': '660fc73500000500', 'vmxon': 'f30fc73500000500', 'vmptrst': '0fc73d00000500',
            '0f38-50': '0f3850c0', '0f3a-00': '0f3a00c000',
            # Reserved-NOP cells with LOCK, and 0f 0d with a register operand (PREFETCHW takes m8; pinned QEMU: #UD too)
            'lock-0f19': 'f00f1900', 'lock-0f18-4': 'f00f1820', 'lock-0f1d-reg': 'f00f1dc0', '0f0d-reg': '0f0dc0', '66-0f0d-reg': '660f0dc8',
            # Review: JMPE and the blank 66/F2 0f b8 cells, blank mandatory-prefix cells of Tables A-3/A-4, operand types of
            # register-only/memory-only SIMD forms, the 66 SHA cell, the F2 0f 38 f6 cell, unadvertised GFNI/MOVDIRI cells, PTWRITE (Intel PT not advertised)
            'jmpe': '0fb8c0', '66-0fb8': '660fb8c0', 'f2-0fb8': 'f20fb800', 'np-0fd0': '0fd0c0', 'np-0fd6': '0fd6c0', 'np-0fe6': '0fe6c0',
            'np-0ff0-m': '0ff000', 'np-0f7c': '0f7cc0', 'f3-0f54': 'f30f54c0', 'f2-0f54': 'f20f54c0', 'f3-0f2b-m': 'f30f2b00',
            'f2-0f2b-m': 'f20f2b00', 'f2-0f5b': 'f20f5bc0', '66-0f52': '660f52c0', 'f3-0f6f-like-f2': 'f20f6fc0', '66-0f77': '660f77c0',
            'np-0f6c': '0f6cc0', 'f3-0fc6': 'f30fc6c000', '66-0f12-reg': '660f12c0', '0f50-m': '0f5000', '0fd7-m': '0fd700',
            'f3-0fd6-m': 'f30fd600', '0f71-m': '0f711000', '0f73-3-np': '0f73d800', '66-0f3850': '660f3850c0', 'f3-0f3800': 'f30f3800c0',
            '66-0f3a00': '660f3a00c000', 'sha256rnds2-66': '660f38cbc0', 'sha1msg1-f3': 'f30f38c9c0', 'gf2p8mulb': '660f38cfc0',
            'f2-0f38f6': 'f20f38f6c0', 'movdiri': '0f38f900', 'ptwrite-m': 'f30fae20', 'ptwrite-r': 'f30faee0'}
    for name, hexcode in uds.items():
        out.append((f'ud-{name}', b'\x90' + bytes.fromhex(hexcode) + b'\x90', '', ud_at(CODE + 1)))

    def reg(o, r):
        return smp.num(o['gpr'][P.REGS.index(r)])

    def reached(check):
        def f(o):
            if o['stop'] != 'reached':
                return [f"{o['stop']} {o['reason']} at {o['rip']} {o.get('fault')}"]
            return check(o)
        return f

    def gp_at(at, error=0):
        return lambda o: [] if fault(o) == [13, 0, error, at] else [f"want #GP({error:#x}) at {at:#x}, got {o['stop']} {o['reason']} {o.get('fault')}"]
    # CLTS (SDM Vol.2 CLTS: CR0.TS := 0; #GP(0) at CPL > 0); SMSW (CR0[15:0] to m16; in 64-bit mode a register at the
    # operand size: 16 bits merge, 32 and 64 zero-extend CR0); LMSW (CR0[3:0] from the source, PE never cleared; #GP(0) at
    # CPL > 0). R15 holds CR0 before the instruction under test.
    m = (1 << 64) - 1
    out.append(('clts-smsw', 'mov rax, cr0\nbts rax, 3\nmov cr0, rax\nmov r15, rax\nclts\nmov rbx, cr0\nmov rax, -1\nmov r8, -1\nsmsw ax\n'
                'smsw r8d\nmov r9, -1\nsmsw r9\nmov qword ptr [0x500000], -1\nsmsw word ptr [0x500000]\nmov r10, [0x500000]', '',
                reached(lambda o: [] if [reg(o, 'rbx'), reg(o, 'rax'), reg(o, 'r8'), reg(o, 'r9'), reg(o, 'r10')] == [
                    reg(o, 'r15') & ~8, m & ~0xffff | reg(o, 'rbx') & 0xffff, reg(o, 'rbx') & 0xffffffff, reg(o, 'rbx'), m & ~0xffff | reg(o, 'rbx') & 0xffff]
                    else [f"CR0 {reg(o, 'r15'):#x} -> {reg(o, 'rbx'):#x}, smsw ax {reg(o, 'rax'):#x}, r32 {reg(o, 'r8'):#x}, r64 {reg(o, 'r9'):#x}, m16 {reg(o, 'r10'):#x}"])))
    out.append(('lmsw', 'mov r15, cr0\nmov eax, 0xfffe\nlmsw ax\nmov rbx, cr0\nmov word ptr [0x500000], 0\nlmsw word ptr [0x500000]\nmov rcx, cr0', '',
                reached(lambda o: [] if [reg(o, 'rbx'), reg(o, 'rcx')] == [reg(o, 'r15') | 0xf, reg(o, 'r15') & ~0xe]
                        else [f"CR0 {reg(o, 'r15'):#x}: after LMSW 0xfffe {reg(o, 'rbx'):#x}, after LMSW 0 {reg(o, 'rcx'):#x}"])))
    sw = ref.assemble('nop')
    for name, insn in (('clts-cpl3', 'clts'), ('lmsw-cpl3', 'lmsw ax')):
        out.append((name, sw + ref.assemble(insn), f'switch {CODE:#x} 3\n', gp_at(CODE + 1)))
    head3 = ref.assemble('mov r15, cr0')
    out.append(('smsw-cpl3', head3 + sw + ref.assemble('smsw eax'), f'switch {CODE + len(head3):#x} 3\n',
                reached(lambda o: [] if reg(o, 'rax') == reg(o, 'r15') & 0xffffffff else [f"smsw at CPL 3 {reg(o, 'rax'):#x}, CR0 {reg(o, 'r15'):#x}"])))
    # UMIP (CPUID.7.0:ECX[2], CR4 bit 11; SDM Vol.2 SGDT, SIDT, SLDT, SMSW, STR: "#GP(0) If CR4.UMIP = 1 and CPL > 0", every
    # form; Vol.3 2.5). At CPL 0 each runs; at CPL 3 each form is #GP(0) at its RIP with its destination unwritten, before
    # any operand access (an unmapped operand still #GP, as pinned QEMU checks it at translation); with UMIP set then cleared
    # each runs at CPL 3. The reference AMD machine's CPL 3 probe: natemu-raw/umip-20261104-01/probe.
    umip_on = 'mov rax, cr4\nbts rax, 11\nmov cr4, rax\nmov r13, cr4\n'
    out.append(('umip-cpl0', umip_on + 'mov r15, cr0\nmov qword ptr [0x500000], -1\nmov qword ptr [0x500008], -1\nsgdt [0x500000]\n'
                'sidt [0x500010]\nmov rax, -1\nsldt eax\nmov rbx, -1\nstr ebx\nsmsw rcx\nmov r14, 1', '',
                reached(lambda o: [] if reg(o, 'r13') >> 11 & 1 and reg(o, 'r14') == 1 and reg(o, 'rcx') == reg(o, 'r15')
                        and reg(o, 'rax') >> 32 == 0 and reg(o, 'rbx') >> 32 == 0
                        else [f"CR4 {reg(o, 'r13'):#x}, smsw {reg(o, 'rcx'):#x} (CR0 {reg(o, 'r15'):#x}), sldt {reg(o, 'rax'):#x}, str {reg(o, 'rbx'):#x}"])))
    umip_forms = {'sgdt': 'sgdt [0x500000]', 'sidt': 'sidt [0x500000]', 'sldt-r': 'sldt eax', 'sldt-m': 'sldt word ptr [0x500000]',
                  'str-r': 'str eax', 'str-m': 'str word ptr [0x500000]', 'smsw-r16': 'smsw ax', 'smsw-r32': 'smsw eax',
                  'smsw-r64': 'smsw rax', 'smsw-m': 'smsw word ptr [0x500000]', 'f3-smsw': b'\xf3\x48\x0f\x01\xe0',
                  'sgdt-unmapped': 'sgdt [0x7f0000]', 'smsw-unmapped': 'smsw word ptr [0x7f0000]'}
    head = ref.assemble(umip_on + 'mov rax, -1\nmov qword ptr [0x500000], -1')

    def umip_gp(at):
        return lambda o: gp_at(at)(o) + ([] if reg(o, 'rax') == m else [f"RAX {reg(o, 'rax'):#x} written"])
    for name, insn in umip_forms.items():
        code = insn if isinstance(insn, bytes) else ref.assemble(insn)
        out.append((f'umip-cpl3-{name}', head + sw + code, f'switch {CODE + len(head):#x} 3\n', umip_gp(CODE + len(head) + 1)))
    # UMIP set then cleared (MOV CR4 at CPL 0): every form runs at CPL 3 again.
    off = ref.assemble(umip_on + 'btr rax, 11\nmov cr4, rax')
    body = b''.join(ref.assemble(x) if isinstance(x, str) else x for k, x in umip_forms.items() if 'unmapped' not in k) + ref.assemble('mov r14, 1')
    out.append(('umip-off-cpl3', off + sw + body, f'switch {CODE + len(off):#x} 3\n', reached(lambda o: [] if reg(o, 'r14') == 1 else ['r14 not set'])))
    # SYSENTER/SYSEXIT in IA-32e mode (SDM Vol.2 SYSENTER, SYSEXIT): #GP(0) when IA32_SYSENTER_CS[15:2] = 0, SYSEXIT also at
    # CPL > 0 and (REX.W) for a non-canonical RCX or RDX; SYSENTER: CS = SYSENTER_CS & FFFCH (64-bit, DPL 0), SS = CS + 8,
    # RSP = SYSENTER_ESP, RIP = SYSENTER_EIP, RFLAGS.VM/IF cleared, CPL 0; SYSEXIT (REX.W): CS = SYSENTER_CS + 32 | 3,
    # SS = SYSENTER_CS + 40 | 3, RSP = RCX, RIP = RDX, CPL 3.
    wr = lambda idx, v: f'mov ecx, {idx:#x}\nmov eax, {v & 0xffffffff:#x}\nmov edx, {v >> 32:#x}\nwrmsr\n'
    out.append(('sysenter-cs0', b'\x90' + ref.assemble('sysenter'), '', gp_at(CODE + 1)))
    out.append(('sysexit-cs0', b'\x90' + ref.assemble('sysexit'), '', gp_at(CODE + 1)))
    head = wr(0x174, 0x08) + wr(0x175, 0x500800) + wr(0x176, 0)        # EIP fixed up below
    # CPL 0 -> SYSEXITQ to CPL 3 at `user` (RSP 0x500f00) -> SYSENTER back to CPL 0 at `kernel` (RSP 0x500800, IF cleared).
    body = head + 'lea rdx, [rip+user]\nmov rcx, 0x500f00\nsti\nlea rax, [rip+kernel]\nmov r8, rax\n' + \
        'mov ecx, 0x176\nmov rdx, rax\nshr rdx, 32\nwrmsr\nlea rdx, [rip+user]\nmov rcx, 0x500f00\n.byte 0x48\nsysexit\n' + \
        'user:\nmov r9, rsp\nmov r10d, cs\nmov r11d, ss\npushfq\npop r12\nsysenter\nkernel:\nmov r13, rsp\nmov r14d, cs\nmov r15d, ss\npushfq\npop rbx'
    out.append(('sysexit-sysenter', body, '',
                reached(lambda o: [] if [reg(o, r) for r in ('r9', 'r10', 'r11', 'r13', 'r14', 'r15')] == [0x500f00, 0x2b, 0x33, 0x500800, 0x08, 0x10]
                        and reg(o, 'r12') & 0x200 and not reg(o, 'rbx') & 0x200
                        else [f"user rsp/cs/ss {[hex(reg(o, r)) for r in ('r9', 'r10', 'r11', 'r12')]} kernel {[hex(reg(o, r)) for r in ('r13', 'r14', 'r15', 'rbx')]}"])))
    out.append(('sysexit-cpl3', head + 'nop\nsysexit', f'switch {CODE + len(ref.assemble(head)):#x} 3\n', gp_at(CODE + len(ref.assemble(head)) + 1)))
    nc = head + 'mov rdx, 0x800000000000\nxor ecx, ecx\n.byte 0x48\nsysexit'
    out.append(('sysexit-noncanonical', nc, '', gp_at(CODE + len(ref.assemble(nc)) - 3)))
    # Direct branches to a non-canonical target (fexc refuses them; host `system::branch`, resident `system::BRANCH`;
    # SDM Vol.2 JMP/CALL/Jcc/LOOPcc/JrCXZ, 64-bit mode: #GP(0) at the branch when taken, nothing changed: CALL faults before
    # its push, LOOP before its count is written, Vol.3 7.5). The branch is at 0x7fff_ffff_ffc0: rel8 0x7f and rel32
    # 0x1000 both cross 0x8000_0000_0000; RSP starts at 0x500800. Not taken, the branch completes (LOOPcc: RCX
    # decremented; 0x67: ECX, written zero-extended) and the run ends after it.
    at = 0x7fff_ffff_ffc0

    def branch_case(name, head, insn, take, rcx=None):
        h = ref.assemble('mov rsp, 0x500800\n' + head)
        code = h + bytes.fromhex(insn) + b'\x90'

        def check(o):
            p = [] if rcx is None or reg(o, 'rcx') == rcx else [f"RCX {reg(o, 'rcx'):#x}, want {rcx:#x}"]
            if take:
                return gp_at(at)(o) + p + ([] if reg(o, 'rsp') == 0x500800 else [f"frame RSP {reg(o, 'rsp'):#x}"])
            if o['stop'] != 'reached' or smp.num(o['rip']) != at + len(code) - len(h):
                return [f"want completion, got {o['stop']} {o['reason']} at {o['rip']} {o.get('fault')}"]
            return p
        out.append((f'branch-{name}', code, '', check, at - len(h)))
    branch_case('jmp8', '', 'eb7f', True)
    branch_case('jmp32', '', 'e900100000', True)
    branch_case('call32', '', 'e800100000', True)
    branch_case('je8-taken', 'xor eax, eax', '747f', True)
    branch_case('jne8-not', 'xor eax, eax', '757f', False)
    branch_case('jl32-taken', 'mov eax, 1\ncmp eax, 2', '0f8c00100000', True)
    branch_case('jg32-not', 'mov eax, 1\ncmp eax, 2', '0f8f00100000', False)
    # Prefixes a near branch ignores (segment overrides and 2e/3e hints, 66, REX.W, F2 BND, F3): the unprefixed outcome.
    branch_case('ds-je8-taken', 'xor eax, eax', '3e747f', True)
    branch_case('cs-jne8-not', 'xor eax, eax', '2e757f', False)
    branch_case('rexw-jmp32', '', '48e900100000', True)
    branch_case('bnd-call32', '', 'f2e800100000', True)
    branch_case('bnd-jmp32', '', 'f2e900100000', True)
    branch_case('ds-jcc32', 'xor eax, eax', '3e0f8400100000', True)
    branch_case('o16-jmp32', '', '66e900100000', True)
    # 66 (patch 0029: fexc decodes rel8/rel32 as Intel; the target is still non-canonical, so still cut): CALL before its push
    branch_case('o16-call32', '', '66e800100000', True)
    branch_case('o16-jmp8', '', '66eb7f', True)
    branch_case('o16-je8-taken', 'xor eax, eax', '66747f', True)
    branch_case('o16-jne8-not', 'xor eax, eax', '66757f', False)
    branch_case('o16-jl32-taken', 'mov eax, 1\ncmp eax, 2', '660f8c00100000', True)
    branch_case('o16-jg32-not', 'mov eax, 1\ncmp eax, 2', '660f8f00100000', False)
    branch_case('o16-loop-taken', 'mov ecx, 2', '66e27f', True, 2)
    branch_case('o16-jrcxz-not', 'mov ecx, 1', '66e37f', False, 1)
    branch_case('rep-jmp8', '', 'f3eb7f', True)
    branch_case('fs-rex-jne32-not', 'xor eax, eax', '64410f8500100000', False)
    # Every condition, taken and not, from flags loaded by POPF (CF 0x1, PF 0x4, ZF 0x40, SF 0x80, OF 0x800).
    flags = {0: (0x802, 0x2), 2: (0x3, 0x2), 4: (0x42, 0x2), 6: (0x42, 0x2), 8: (0x82, 0x2), 10: (0x6, 0x2), 12: (0x82, 0x882), 14: (0x42, 0x2)}
    for cc in range(16):
        on, off = flags[cc & 14]
        if cc & 1:
            on, off = off, on
        branch_case(f'jcc{cc:x}-taken', f'push {on:#x}\npopfq', f'{0x70 + cc:02x}7f', True)
        branch_case(f'jcc{cc:x}-not', f'push {off:#x}\npopfq', f'0f{0x80 + cc:02x}00100000', False)
    big = 0xffff_ffff_0000_0000
    branch_case('loop-taken', 'mov ecx, 2', 'e27f', True, 2)
    branch_case('loop-not', 'mov ecx, 1', 'e27f', False, 0)
    branch_case('loop-wrap-taken', 'xor ecx, ecx', 'e27f', True, 0)            # 0 - 1 != 0: taken
    branch_case('loope-taken', 'mov ecx, 3\nxor eax, eax', 'e17f', True, 3)
    branch_case('loope-not-zf', 'mov ecx, 3\nmov eax, 1\ncmp eax, 2', 'e17f', False, 2)
    branch_case('loopne-taken', 'mov ecx, 3\nmov eax, 1\ncmp eax, 2', 'e07f', True, 3)
    branch_case('loopne-not-zf', 'mov ecx, 5\nxor eax, eax', 'e07f', False, 4)
    branch_case('jrcxz-taken', 'xor ecx, ecx', 'e37f', True, 0)
    branch_case('jrcxz-not', 'mov ecx, 1', 'e37f', False, 1)
    branch_case('loop-a32-taken', f'mov rcx, {big | 2:#x}', '67e27f', True, big | 2)
    branch_case('loop-a32-not', f'mov rcx, {big | 1:#x}', '67e27f', False, 0)
    branch_case('loopne-a32-not', f'mov rcx, {big | 5:#x}\nxor eax, eax', '67e07f', False, 4)
    branch_case('jecxz-taken', f'mov rcx, {big:#x}', '67e37f', True, big)
    branch_case('jecxz-not', f'mov rcx, {big | 1:#x}', '67e37f', False, big | 1)
    # PUSH/POP FS/GS (SDM Vol.2 PUSH, POP; MOV Sreg's checks): faults leave RSP and the segment unchanged. POP FS of a
    # selector beyond the GDT limit: #GP(selector); PUSH GS with RSP 4 past the canonical boundary: #SS(0) (the slot is
    # not canonical); POP FS of the DPL 0 data selector 0x10 at CPL 3: #GP(0x10); POP GS of null at CPL 3 loads null.
    pop_bad = 'mov rsp, 0x500800\npush 0x7f8\nnop'
    out.append(('pop-fs-beyond-gdt', ref.assemble(pop_bad) + bytes.fromhex('0fa1'), '',
                lambda o: gp_at(CODE + len(ref.assemble(pop_bad)), 0x7f8)(o) + ([] if reg(o, 'rsp') == 0x5007f8 else [f"RSP {reg(o, 'rsp'):#x}"])))
    nc = 'mov rsp, 0x800000000004\nnop'
    out.append(('push-gs-noncanonical', ref.assemble(nc) + bytes.fromhex('0fa8'), '',
                lambda o: ([] if fault(o) == [12, 0, 0, CODE + len(ref.assemble(nc))] else [f"want #SS(0), got {o['stop']} {o.get('fault')}"])))
    h3 = ref.assemble('mov rsp, 0x500800\npush 0x10\npush 0')
    out.append(('pop-fs-dpl0-cpl3', h3 + b'\x90' + bytes.fromhex('0fa90fa1'), f'switch {CODE + len(h3):#x} 3\n',
                lambda o: gp_at(CODE + len(h3) + 3, 0x10)(o) + ([] if reg(o, 'rsp') == 0x500800 - 8 else [f"RSP {reg(o, 'rsp'):#x}"])))
    # The guest-RIP-relative split lock (`xchg [rip + d], eax` on 0x50000e..0x500011, crossing 16 bytes): it was a host error
    # at admission, then the typed stop `unsupported_atomic`; since the no-stop change it runs (the resident's split lock).
    xchg = b'\x90\x87\x05' + (0x50000e - (CODE + 7)).to_bytes(4, 'little')
    out.append(('split-lock-rip', xchg, '', lambda o: [] if o['stop'] == 'reached'
                else [f"want it to run to the end, got {o['stop']} {o['reason']} at {o['rip']}"]))
    # Code on a page that is not guest RAM (an unassigned guest-physical page, mapped executable): the typed stop `no_code`
    # `fetch_not_ram` at its RIP (it was a host error; no SDM exception exists for fetching from memory-mapped I/O).
    out.append(('fetch-not-ram', 'mov rax, 0x700000\njmp rax', 'mmio 0x700000 0xfe000000 unassigned\nentry 0x700000 1 clear 0x8000000000000000\n',
                lambda o: [] if (o['stop'], o['reason'], smp.num(o['rip'])) == ('no_code', 'fetch_not_ram', 0x700000)
                else [f"want no_code fetch_not_ram at 0x700000, got {o['stop']} {o['reason']} at {o['rip']}"]))
    # An IRETQ that faults still releases NMI blocking (SDM Vol.3 7.7.2; delivery_model A8): an NMI's handler IRETQs to a
    # non-canonical RIP (#GP(0)); the #GP handler points gate 2 at the driver's logging handler and a second NMI is posted:
    # it must be taken at once (the run ends logged with vector 2), not stay pending to the end.
    import delivery_check as dc
    idt, ptr = 0x60_0000, 0x60_1000
    log2 = dm.gate(dc.LOG_BASE + 0x2000 + 16 * 2, 0x08, dpl=0, ist=0)
    pre = ref.assemble(f'lidt [{ptr:#x}]\nmov rsp, 0x500800')
    nmi_h = 'mov rax, rsp\npush 0x10\npush rax\npushfq\npush 0x08\nmov rbx, 0x8000000000000000\npush rbx\niretq'
    gp_h = (f'mov rax, {int.from_bytes(log2[:8], "little"):#x}\nmov [{idt + 32:#x}], rax\nmov rax, {int.from_bytes(log2[8:], "little"):#x}\n'
            f'mov [{idt + 40:#x}], rax\nnop\nnop\nmov r14, 1')
    body = pre + b'\x90\x90'
    nmi_at = CODE + len(body)
    gp_at_ = nmi_at + len(ref.assemble(nmi_h))
    code = body + ref.assemble(nmi_h) + ref.assemble(gp_h)
    second = gp_at_ + len(ref.assemble(gp_h)) - len(ref.assemble('nop\nnop\nmov r14, 1'))
    gates = bytearray(16 * 14)
    gates[32:48] = dm.gate(nmi_at, 0x08, dpl=0, ist=0)
    gates[13 * 16:14 * 16] = dm.gate(gp_at_, 0x08, dpl=0, ist=0)
    extra = (f'memory {idt:#x} {bytes(gates).hex()}\nmemory {ptr:#x} {((len(gates) - 1).to_bytes(2, "little") + idt.to_bytes(8, "little")).hex()}\n'
             f'nmi {CODE + len(pre):#x}\nnmi {second:#x}\n')
    out.append(('iret-fault-nmi-unblocked', code, extra,
                lambda o: [] if o['stop'] == 'guest_fault' and (o.get('delivered') or {}).get('vector') == 2 else
                [f"want the second NMI delivered (logged, vector 2), got {o['stop']} {o['reason']} at {o['rip']} {o.get('fault')}"]))
    # PREFETCHW m8 and CLDEMOTE m8 are hints: no fault on a non-canonical or unmapped operand, nothing changed (SDM Vol.2
    # PREFETCHW: only LOCK is #UD; CLDEMOTE: a NOP without CPUID.07H:ECX.CLDEMOTE).
    hints = 'mov rax, 0x8000000000000000\nmov rbx, 0x7f0000\nmov r15, rax\nprefetchw [rax]\nprefetchw [rbx]\n.byte 0x0f, 0x1c, 0x00\n.byte 0x0f, 0x1c, 0x03\nmov r14, 1'
    out.append(('hints-prefetchw-cldemote', hints, '', reached(lambda o: [] if reg(o, 'r14') == 1 and reg(o, 'r15') == 1 << 63 else [f"r14 {reg(o, 'r14'):#x}"])))
    return out


def mixed_far_cases():
    """Both code modes at CPL 3. Fixed addresses let the oracle check the failing instruction and CALL frame."""
    base, source, target, stack, pointer = 0x8000, 0x8140, 0x8200, 0x500800, 0x500100
    cs32, cs64 = 0x2b, 0x23
    gdt = [0, 0xAF9B000000FFFF, 0xCF93000000FFFF, 0xCFF3000000FFFF,
           0xAFFB000000FFFF, 0xCFFB000000FFFF,
           0xCF7B000000FFFF,                 # 33h: non-present user code
           0x40FA0000008200]                 # 3bh: user CS limit 8200h, A initially clear
    def setup(src, rsp, table='gdt'):
        return f"""org {base}
            mov esp, 0x500f00
            lgdt [rel gdtr]
            mov eax, 0x1b
            mov ds, eax
            mov es, eax
            mov rsi, {pointer}
            mov r8, 0x1122334455667788
            push 0x1b
            mov rax, {rsp}
            push rax
            push 0x47
            push {src}
            push {source}
            iretq
        gdtr: dw {8 * len(gdt) - 1}
            dq {table}
        gdt: dq {','.join(hex(x) for x in gdt)}
        """
    def check(at, vector, error, cr2, sp, frame=b'', cs=None, flags=None):
        def judge(out):
            actual = fault(out)
            problems = [] if actual == [vector, cr2, error, at] else [f'fault {actual}, expected {[vector, cr2, error, at]}']
            reg = lambda r: smp.num(out['gpr'][P.REGS.index(r)])
            if reg('rsp') != sp:
                problems.append(f'RSP {reg("rsp"):#x}, expected {sp:#x}')
            if reg('r8') != 0x1122334455667788:
                problems.append('R8 changed')
            if cs is not None and reg('rax') != cs:
                problems.append(f'CS {reg("rax"):#x}, expected {cs:#x}')
            if flags is not None and smp.num(out['rflags']) & 0x8D5 != flags:
                problems.append(f'RFLAGS {out["rflags"]}, expected arithmetic bits {flags:#x}')
            if frame:
                memory = next(bytes.fromhex(r['data']) for r in out['memory'] if smp.num(r['addr']) == sp)
                if memory[:len(frame)] != frame:
                    problems.append(f'frame {memory[:len(frame)].hex()}, expected {frame.hex()}')
            return problems
        return judge

    for bits in (32, 64):
        src, dst = (cs32, cs64) if bits == 32 else (cs64, cs32)
        for width in ((2, 4) if bits == 32 else (2, 4, 8)):
            for op in ('jmp', 'call', 'ret'):
                for high in ((0, 1 << 32) if width == 8 else (0,)):
                    addr = target | high
                    release = 6
                    slot = lambda x: x.to_bytes(width, 'little')
                    prefix = {2: b'\x66', 4: b'', 8: b'\x48'}[width]
                    imm = (op != 'ret' and bits == 32)
                    if op == 'ret':
                        insn = prefix + b'\xca' + release.to_bytes(2, 'little')
                        data = slot(addr) + slot(dst)
                        extra = f'memory {stack:#x} {data.hex()}\n'
                    elif imm:
                        insn = prefix + bytes([0xEA if op == 'jmp' else 0x9A]) + slot(addr) + dst.to_bytes(2, 'little')
                        extra = ''
                    else:
                        insn = prefix + bytes([0xFF, 0x2E if op == 'jmp' else 0x1E])  # [rsi]
                        extra = f'memory {pointer:#x} {(slot(addr) + dst.to_bytes(2, "little")).hex()}\n'
                    code = setup(src, stack) + f'times {source} - ($-$$+{base}) db 0x90\n' + \
                        'db ' + ','.join(hex(b) for b in insn) + f'\ntimes {target} - ($-$$+{base}) db 0x90\n'
                    body = P.asm(code + f'bits {96 - bits}\nmov eax, cs\nud2')
                    expected_sp = stack
                    frame = b''
                    if op == 'call':
                        expected_sp -= 2 * width
                        marker = bytes([0xA5]) * (2 * width)
                        extra += f'memory {expected_sp:#x} {marker.hex()}\n'
                        frame = slot(source + len(insn)) + slot(src)
                    elif op == 'ret':
                        expected_sp += 2 * width + release
                    judge = check(source, 13, 0, 0, stack) if high and op != 'call' else \
                        check(base + len(body) - 2, 6, 0, 0, expected_sp, frame, dst, 0x45)
                    yield f'mixed-{bits}-{op}{width * 8}-{"high" if high else "low"}', body, extra, judge, base

    # A fault reading either pointer/frame page precedes the descriptor load. A descriptor or stack
    # fault leaves the instruction restartable. In-bounds entry to a non-flat CS commits first.
    descriptor = base + len(P.asm(setup(cs64, stack))) - 8 * len(gdt) + 0x38
    for name, opcode, rsp, ptr, suffix, vector, error, cr2, at, after_sp in (
            ('selector-np', 'call far [rsi]', stack, 0x33, '', 11, 0x30, 0, source, stack),
            ('selector-type', 'call far [rsi]', stack, 0x1b, '', 13, 0x18, 0, source, stack),
            ('stack-ss', 'call far [rsi]', 0x800000000008, cs32, '', 12, 0, 0, source, 0x800000000008),
            ('stack-pf', 'call far [rsi]', 0x500008, cs32, '', 14, 6, 0x4ffff8, source, 0x500008),
            ('ret-split-pf', 'o64 retf', 0x500ff8, cs32, '', 14, 4, 0x501000, source, 0x500ff8),
            ('pointer-split-pf', 'jmp far [rsi]', stack, cs32, 'mov esi, 0x500ff8', 14, 4, 0x501000, source + 5, stack),
            ('accessed-bit-pf', 'call far [rsi]', stack, 0x3b, '', 14, 3, descriptor + 5, source, stack),
            ('limit-gp', 'call far [rsi]', stack, 0x3b, '', 13, 0, 0, source, stack),
            ('shape-after-call', 'call far [rsi]', stack, 0x3b, '', 13, 0x38, 0, target, stack - 16),
            ('first-user-fetch', 'jmp far [rsi]', stack, cs32, '', 14, 20, 0x700000, 0x700000, stack)):
        offset = target + 1 if name == 'limit-gp' else 0x700000 if name == 'first-user-fetch' else target
        extra = f'memory {pointer:#x} {(offset.to_bytes(8, "little") + ptr.to_bytes(2, "little")).hex()}\n'
        table = 'gdt'
        if name == 'shape-after-call':
            table = 0x500300
            descriptors = b''.join(raw.to_bytes(8, 'little') for raw in gdt)
            extra += f'memory {table:#x} {descriptors.hex()}\n'
        if name in ('pointer-split-pf', 'ret-split-pf'):
            extra += f'memory 0x500ff8 {target.to_bytes(8, "little").hex()}\n'
        code = setup(cs64, rsp, table) + f'times {source} - ($-$$+{base}) db 0x90\n{suffix}\n{opcode}\nud2'
        body = P.asm(code)
        frame = b''
        if name == 'shape-after-call':
            frame = (base + len(body) - 2).to_bytes(8, 'little') + cs64.to_bytes(8, 'little')
            extra += f'memory {after_sp:#x} {bytes(16).hex()}\n'
        yield 'mixed-' + name, body, extra, check(at, vector, error, cr2, after_sp, frame), base

    # A same-level far JMP from 64-bit code whose RSP is above 4 GiB, to 32-bit code whose first instruction the resident
    # runs: compatibility code's stack pointer is ESP (natemu zeroes the upper halves of RAX..RDI when a transfer commits
    # there), so POP DS, POPA and POPFD pop at ESP, where RSP would be #SS(0).
    eax = 0x600df00d
    for name, insn, data, popped, rax, flags in (
            ('pop-ds', 'pop ds\nmov eax, ds', (0x1b).to_bytes(4, 'little'), 4, 0x1b, None),
            ('popa', 'popa', b''.join(k.to_bytes(4, 'little') for k in (1, 2, 3, 0, 5, 6, 7, eax)), 32, eax, None),
            ('popfd', 'popfd', (0x8c3).to_bytes(4, 'little'), 4, None, 0x8c1)):
        code = setup(cs64, 1 << 32 | stack) + f'times {source} - ($-$$+{base}) db 0x90\njmp far [rsi]\n' + \
            f'times {target} - ($-$$+{base}) db 0x90\nbits 32\n{insn}\nud2'
        body = P.asm(code)
        extra = (f'memory {pointer:#x} {(target.to_bytes(8, "little") + cs32.to_bytes(2, "little")).hex()}\n'
                 f'memory {stack:#x} {data.hex()}\n')
        yield f'mixed-high-rsp-{name}', body, extra, check(base + len(body) - 2, 6, 0, 0, stack + popped, cs=rax, flags=flags), base

    # Narrow SYSEXIT ignores both register high halves and installs its synthetic segments without a GDT load.
    sysexit = f"""org {base}
        mov esp, 0x500f00
        mov ecx, 0x174
        mov eax, 8
        xor edx, edx
        wrmsr
        mov r8, 0x1122334455667788
        mov rcx, {1 << 48 | stack}
        mov rdx, {1 << 48 | target}
        push 0x47
        popfq
        sysexit
        times {target} - ($-$$+{base}) db 0x90
        bits 32
        mov eax, cs
        mov ebx, ss
        ud2"""
    body = P.asm(sysexit)
    def sysexit_check(out):
        problems = check(base + len(body) - 2, 6, 0, 0, stack, cs=0x1b, flags=0x45)(out)
        if smp.num(out['gpr'][P.REGS.index('rbx')]) != 0x23:
            problems.append('SYSEXIT loaded wrong SS')
        return problems
    yield 'mixed-sysexit', body, '', sysexit_check, base


def cmd_mixed_far(a, report):
    import compat as c
    failures = cmd_cases(a, report, mixed_far_cases)
    # IRET loads TF. The first user instruction is the far CALL, the first 32-bit instruction its RET.
    # Leave TF set on the first return from #DB so both transfers trap at their destination.
    at32 = c.U + 0x40
    call = f'call dword far [abs {c.W:#x}]'
    back = c.U + len(P.asm(call))
    user = f"""bits 64
        {call}
        inc dword [abs {c.W + 0x30:#x}]
        int 0x80
        times 0x40 - ($ - $$) db 0x90
        bits 32
        retf
    """
    kernel = c.gate(1, '.v1') + c.XMMS + f'mov dword [{c.W:#x}], {at32:#x}\nmov word [{c.W+4:#x}], 0xdb\n'
    kernel += c.ENTER(c.HIGH_REGS, cs=0x23, flags=0x103) + c.handlers(f"""
        cmp qword [rsp], 1
        jne .fin
        cmp qword [{c.LOG:#x}], 2
        ja .fin
        mov rax, [rsp+32]
        and eax, 0x101
        test rbx, rbx
        jnz .return_trap
        mov [{c.W+0x10:#x}], rax
        mov rax, [rsp+40]
        mov rax, [rax]
        mov [{c.W+8:#x}], rax
        jmp .resume
    .return_trap:
        mov [{c.W+0x18:#x}], rax
        btr qword [rsp+32], 8
        jmp .resume
    """, xmm=True, vectors=(1, 6, 13, 14, 128))
    prog = c.program('mixed-step-call-ret', kernel, user)
    entry = {'name': prog['name'], 'runs': []}
    for limit in (None, 1):
        rc, out, err = driver(a.host, a.runtime, a.fexc, smp_description(prog), a.out / 'work' / f'mixed-step-{limit}',
                              [limit] if limit else [])
        problems = []
        if rc:
            problems.append(f'driver {rc}: {err}')
        else:
            cpu = out['cpus'][0]
            if cpu['stop'] != 'reached' or smp.num(cpu['rip']) != body_end(prog, smp.num(prog['cpus'][0]['rip'])):
                problems.append(f'stopped {cpu["stop"]} at {cpu["rip"]}')
            captured = {smp.num(r['addr']): bytes.fromhex(r['data']) for r in cpu['memory']}
            words, log = captured[c.W], captured[c.LOG]
            def qword(data, at):
                return int.from_bytes(data[at:at + 8], 'little')

            def expect(label, got, want):
                if got != want:
                    problems.append(f'{label}: got {got}, expected {want}')

            expect('events', qword(log, 0), 3)
            expect('CALL return CS:EIP', qword(words, 8), (0x23 << 32) | back)
            expect('saved TF/CF', [qword(words, 0x10), qword(words, 0x18)], [0x101, 0x101])
            expect('continuation executions', qword(words, 0x30), 1)
            frames = ((1, 0xdb, c.US - 8), (1, 0x23, c.US), (0x80, 0x23, c.US))
            for k, (vec, cs, sp) in enumerate(frames):
                start = 0x100 + 0x200 * k
                frame = [qword(log, start + slot) for slot in (0, 8, 24, 40)]
                expect(f'event {k} vector/error/CS/RSP', frame, [vec, 0, cs, sp])
                if k < 2:
                    expect(f'trap {k} RIP', qword(log, start + 16), (at32, back)[k])
                regs = [qword(log, start + 0x40 + 8 * r) for r in range(8, 16)]
                expect(f'event {k} R8..15', regs, [c.UPPER(r) for r in range(8, 16)])
                expect(f'event {k} XMM8..15', log[start + 0x180:start + 0x200], bytes(range(0x80, 0x100)))
        entry['runs'].append({'max_block': limit, 'problems': problems})
        failures += [f'{entry["name"]} (max-block {limit}): {p}' for p in problems]
    report.append(entry)
    return failures


def cmd_cases(a, report, items=cases):
    failures = []
    for name, text, extra, check, *rip in items():
        e = {'name': name, 'runs': []}
        for limit in (None, 1):
            out, problems = run(a, text, a.out / 'work' / f"{name}-{limit or 'd'}", limit, extra, *rip)
            problems = problems or check(out)
            e['runs'].append({'max_block': limit, 'problems': problems})
            failures += [f'{name} (max-block {limit}): {p}' for p in problems]
        print(f"{name}: {'FAIL' if any(r['problems'] for r in e['runs']) else 'ok'}", *[p for r in e['runs'] for p in r['problems']][:2], sep='\n  ', flush=True)
        report.append(e)
    return failures


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--fexc', type=pathlib.Path, required=True)
    ap.add_argument('--out', type=pathlib.Path, required=True)
    ap.add_argument('--only', type=lambda s: s.split(','), default=['qemu', 'cases'])
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    a.host, a.runtime = vm.build()
    report, failures = {}, []
    for mode, fn in (('qemu', cmd_qemu), ('cases', cmd_cases), ('mixed-far', cmd_mixed_far)):
        if mode in a.only:
            report[mode] = []
            failures += fn(a, report[mode])
    (a.out / 'x1.json').write_text(json.dumps({'report': report, 'failures': failures}, indent=1, default=str) + '\n')
    case_results.write(a.out, [c for m, v in report.items() for c in case_results.program_cases(m, v)], failures)
    for f in failures: print('FAIL', f)
    print({k: f'{sum(1 for r in v if not any(x["problems"] for x in r["runs"]))}/{len(v)} ok' for k, v in report.items()})
    sys.exit(1 if failures else 0)


if __name__ == '__main__':
    testenv.require_framework(__file__)
    main()
