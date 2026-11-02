#!/usr/bin/env python3
"""X1b and X1c checks (boot-features plan, X1b, X1c; programs in tools/bench/smp/entry.py).

    tools/with-env python3 tools/run_x1.py --fexc FEXC --out DIR [--only qemu,cases]

- `qemu`: `x1-entry@1/2/4` run unchanged by pinned QEMU (system mode) and natemu at block sizes default and 1: CPL 3 -> 0
  frames on RSP0, IST1, IST3, IST5..7 and CPL 0 -> 0 frames on IST1..4 (IST selection does not depend on the CPL), IRETQ
  to CPL 3 after each, the normal (SWAPGS) and paranoid (RDGSBASE, LSL of the CPU/node descriptor, WRGSBASE) entry
  shapes, #DF from CPL 3 and CPL 0, NMI blocking until IRETQ. natemu must equal QEMU (registers, the logs, the GS blocks:
  the saved context words and which GS base a paranoid entry found are judged there), and every logged frame of both
  must be delivery_model's given the logged context: the vector chain and error code, RF, and the RIP slot's address
  (the stack chosen: RSP0, IST n or the current one, aligned); the entry's per-CPU values (stamp, LSL = CPU number) and
  the CPL 3 results (LSL, RDGSBASE, [gs:0]) as the program sets them; the second NMI's frame RIP equals the first's.
- `cases`: X1c's refused-but-defined cases, now delivered (SDM rules, natemu only; each was a stop before X1c):
  the #UD encodings (PUSH/POP ES/CS/SS/DS, CALL/JMP ptr16:32 in 64-bit mode, LSS/LFS/LGS with a register operand, the
  0f 01 register forms no advertised feature defines, 0f 01 /5 m without F3, 0f 00 /7) and CLTS, SMSW, LMSW, SYSENTER,
  SYSEXIT with their faults and effects.

Writes DIR/x1.json; exit status 1 on any failure."""
import argparse, json, pathlib, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / 'tools'), str(ROOT / 'tools' / 'reference'), str(ROOT / 'tools' / 'bench' / 'smp')]
import ref, smp, entry, programs as P, run_slice1_vm as vm  # noqa: E402
from run_slice3_vm import driver, per_vcpu_checks, body_end, smp_description, vm_state  # noqa: E402
from models import delivery_model as dm  # noqa: E402


# Pinned QEMU's departures from the SDM met here (natemu is judged by delivery_model there, and compared with QEMU with RF
# cleared in every logged RFLAGS): it pushes RF = 1 for INT n/INT3 run right after an IRETQ that loaded RF = 1 (it keeps
# RF until a later event; X1a's note) and for NMIs (run_slice3_vm.QEMU_EVENTS_RF); the SDM pushes 0 for INT n/INT3 (RF is
# cleared at their start, Vol.3 "RF") and the current RF for an NMI.
QEMU_RF = 'pinned QEMU pushes RF = 1 for INT n/INT3 after an IRETQ that loaded RF = 1, and for NMIs (SDM: 0, and the current RF)'
RF = 1 << 16


def model_problems(prog, memory, qemu=False):
    """Every CPU's log against entry.expected and delivery_model; `memory` = {address: bytes} of the captured regions.
    `qemu`: QEMU's run, whose RF in the frames of INT n/INT3 and NMIs is not judged (QEMU_RF)."""
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
            if qemu and event['kind'] in ('int', 'int3', 'nmi') and len(r['events']) == 1:
                image = dm.qword(n['memory'], at + 16)
                undefined[at + 16] = frozenset({image, image ^ RF})
            words = [dm.qword(n['memory'], at + 8 * j) for j in range(5)]
            bad = [j for j, (g, w) in enumerate(zip((rip, cs, rflags, rsp, ss), words))
                   if g != w and not (at + 8 * j in undefined and (undefined[at + 8 * j] is None or g in undefined[at + 8 * j]))]
            if (vector, error if err is not None else None) != (v, err) or slot != at or bad:
                out.append(f'cpu{i} event {k}: logged {[hex(x) for x in e[:8]]}, model {[hex(v), err] + [hex(w) for w in words]} slot {at:#x} (words {bad})')
            if vector in entry.PARANOID:
                bases = {d + entry.K, d + entry.U}
                if x1 not in bases or x2 & 0xffff != i or x2 >> 16 & 0xffff != 0x100 + i or x2 >> 32:
                    out.append(f'cpu{i} event {k} (paranoid {vector:#x}): GS base found {x1:#x}, x2 {x2:#x}')
            elif x1 != 0x100 + i or x2 != (P.SYS_CODE(i) if vector == 14 else 0):
                out.append(f'cpu{i} event {k} (normal {vector:#x}): x1 {x1:#x} x2 {x2:#x}')
        nmis = [e[2] for e in log if e[0] == 2]
        if len(nmis) == 2 and nmis[0] != nmis[1]:
            out.append(f'cpu{i}: the second NMI was taken at {nmis[1]:#x}, the first at {nmis[0]:#x} (blocked until IRETQ: the same boundary)')
        res = [int.from_bytes(read(d + entry.RES + 8 * j, 8), 'little') for j in range(3)]
        if res != [i, d + entry.U, 0x200 + i]:
            out.append(f'cpu{i}: CPL 3 results {[hex(x) for x in res]} != {[hex(x) for x in (i, d + entry.U, 0x200 + i)]}')
    return out


def without_rf(prog, state):
    """`state` (smp.parse_dump's shape) with RF cleared in every logged frame's RFLAGS (QEMU_RF)."""
    logs = {P.SYS_D(i) + entry.LOG for i in range(prog['ncpu'])}
    memory = []
    for r in state['memory']:
        data = bytearray(r['data'])
        if r['addr'] in logs:
            for k in range(0, entry.RES - entry.LOG, 0x50):
                data[k + 34] &= ~1          # RFLAGS (+0x20), bit 16
        memory.append({**r, 'data': bytes(data)})
    return {**state, 'memory': memory}


def cmd_qemu(a, report):
    stub = smp.build_stub(a.out / 'qemu-build')
    failures = []
    for n in (1, 2, 4):
        prog = entry.program(n)
        label = prog['name']
        rc, err, dump, _ = smp.run_image(prog, smp.build_image(prog, stub), a.out / 'qemu' / label, 'multi', timeout=120)
        want = smp.parse_dump(prog, dump) if rc == 1 else None
        e = {'name': label, 'runs': []}
        if want is None:
            failures.append(f'{label}: QEMU run failed ({rc} {err})')
        else:
            q = model_problems(prog, {r['addr']: r['data'] for r in want['memory']}, qemu=True)
            e['qemu_vs_model'] = q
            failures += [f'{label}: QEMU vs model: {d}' for d in q]
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
                if want is not None:
                    problems += smp.compare(prog, without_rf(prog, got), without_rf(prog, want))
            e['runs'].append({'max_block': limit, 'problems': problems})
            failures += [f'{label} (max-block {limit}): {p}' for p in problems]
        print(f"{label}: {'FAIL' if any(r['problems'] for r in e['runs']) or e.get('qemu_vs_model') else 'ok'}",
              *([*e.get('qemu_vs_model', [])] + [p for r in e['runs'] for p in r['problems']])[:8], sep='\n  ', flush=True)
        report.append(e)
    # X1c: PUSH/POP FS/GS, PREFETCHW, CLDEMOTE: natemu equals QEMU (registers, the captured page) and the SDM's registers;
    # the Reserved-NOP cells: natemu equals QEMU and every one completes (R14 = 1, RBX/R8/R9 kept).
    for prog, expected in ((entry.sreg_program(), entry.sreg_expected()), (entry.rnop_program(), {'r14': 1, 'r8': 0x1111, 'r9': 0x2222, 'rbx': 0x7f0000}),
                             (entry.bnd_program(), {r: 1 for r in ('r8', 'r9', 'r10', 'r11', 'r12', 'r13')})):
        failures += plain_program(a, stub, report, prog, expected)
    return failures


def plain_program(a, stub, report, prog, expected):
    """One CPL 0 program vs pinned QEMU and `expected` ({register: value}) at block sizes default and 1."""
    failures = []
    label = prog['name']
    rc, err, dump, _ = smp.run_image(prog, smp.build_image(prog, stub), a.out / 'qemu' / label, 'multi', timeout=120)
    want = smp.parse_dump(prog, dump) if rc == 1 else None
    e = {'name': label, 'runs': []}
    sdm = lambda gpr: [f'{r} {gpr[P.REGS.index(r)]:#x} != {v:#x}' for r, v in expected.items() if gpr[P.REGS.index(r)] != v]
    if want is None:
        failures.append(f'{label}: QEMU run failed ({rc} {err})')
    else:
        e['qemu_vs_sdm'] = sdm([smp.num(x) for x in want['cpus'][0]['gpr']])
        failures += [f'{label}: QEMU vs SDM: {d}' for d in e['qemu_vs_sdm']]
    for limit in (None, 1):
        rc, out, err = driver(a.host, a.runtime, a.fexc, smp_description(prog), a.out / 'work' / f"{label}-{limit or 'd'}", [limit] if limit else [])
        if rc:
            problems = [f'driver exit {rc}: {err.strip()[-400:]}']
        else:
            c = out['cpus'][0]
            end = body_end(prog, smp.num(prog['cpus'][0]['rip']))
            problems = [] if c['stop'] == 'reached' and smp.num(c['rip']) == end else [f"stopped {c['stop']} {c['reason']} at {c['rip']} (end {end:#x})"]
            got = vm_state(prog, out)
            problems += sdm([smp.num(x) for x in got['cpus'][0]['gpr']])
            if want is not None:
                problems += smp.compare(prog, got, want)
        e['runs'].append({'max_block': limit, 'problems': problems})
        failures += [f'{label} (max-block {limit}): {p}' for p in problems]
    print(f"{label}: {'FAIL' if any(r['problems'] for r in e['runs']) or e.get('qemu_vs_sdm') else 'ok'}",
          *(e.get('qemu_vs_sdm', []) + [p for r in e['runs'] for p in r['problems']])[:8], sep='\n  ', flush=True)
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
            # register-only/memory-only SIMD forms, unadvertised SHA/GFNI/ADX/MOVDIRI cells, PTWRITE (Intel PT not advertised)
            'jmpe': '0fb8c0', '66-0fb8': '660fb8c0', 'f2-0fb8': 'f20fb800', 'np-0fd0': '0fd0c0', 'np-0fd6': '0fd6c0', 'np-0fe6': '0fe6c0',
            'np-0ff0-m': '0ff000', 'np-0f7c': '0f7cc0', 'f3-0f54': 'f30f54c0', 'f2-0f54': 'f20f54c0', 'f3-0f2b-m': 'f30f2b00',
            'f2-0f2b-m': 'f20f2b00', 'f2-0f5b': 'f20f5bc0', '66-0f52': '660f52c0', 'f3-0f6f-like-f2': 'f20f6fc0', '66-0f77': '660f77c0',
            'np-0f6c': '0f6cc0', 'f3-0fc6': 'f30fc6c000', '66-0f12-reg': '660f12c0', '0f50-m': '0f5000', '0fd7-m': '0fd700',
            'f3-0fd6-m': 'f30fd600', '0f71-m': '0f711000', '0f73-3-np': '0f73d800', '66-0f3850': '660f3850c0', 'f3-0f3800': 'f30f3800c0',
            '66-0f3a00': '660f3a00c000', 'sha256rnds2-66': '660f38cbc0', 'sha1msg1': '0f38c9c0', 'gf2p8mulb': '660f38cfc0',
            'adox': 'f30f38f6c0', 'movdiri': '0f38f900', 'ptwrite-m': 'f30fae20', 'ptwrite-r': 'f30faee0'}
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
    # The guest-RIP-relative split lock (`xchg [rip + d], eax` on 0x50000e..0x500011, crossing 16 bytes): the typed stop
    # `unsupported_atomic` at the instruction (it was a host error at admission).
    xchg = b'\x90\x87\x05' + (0x50000e - (CODE + 7)).to_bytes(4, 'little')
    out.append(('split-lock-rip', xchg, '', lambda o: [] if (o['stop'], o['reason'].split()[0], smp.num(o['rip'])) == ('resident', 'unsupported_atomic', CODE + 1)
                else [f"want unsupported_atomic at {CODE + 1:#x}, got {o['stop']} {o['reason']} at {o['rip']}"]))
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


def cmd_cases(a, report):
    failures = []
    for name, text, extra, check, *rip in cases():
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
    for mode, fn in (('qemu', cmd_qemu), ('cases', cmd_cases)):
        if mode in a.only:
            report[mode] = []
            failures += fn(a, report[mode])
    (a.out / 'x1.json').write_text(json.dumps({'report': report, 'failures': failures}, indent=1, default=str) + '\n')
    for f in failures: print('FAIL', f)
    print({k: f'{sum(1 for r in v if not any(x["problems"] for x in r["runs"]))}/{len(v)} ok' for k, v in report.items()})
    sys.exit(1 if failures else 0)


if __name__ == '__main__': main()
