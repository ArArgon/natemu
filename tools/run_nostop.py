#!/usr/bin/env python3
"""No-stop checks (correctness pass, November 6, 2026: no instruction a guest program can execute stops the machine;
docs/restructure/correctness-nostop.md). Programs for the `run-program` driver, natemu only, judged by the models here.

    tools/with-env python3 tools/run_nostop.py --fexc FEXC --out DIR [--only maskmov,splitlock,refused,sweep] [--host H --runtime R]

Every program installs its own IDT (all 32 exception vectors, IST 1): a handler logs (vector, error, CR2, RIP) per entry,
counts entries per vector, and resumes at the address in RESUME (each case stores where it ends), so one program runs many
faulting cases; at CPL 3 the program switches there (`switch`) after LIDT.

- `maskmov`: VMASKMOVPS/PD, VPMASKMOVD/Q loads and stores (VEX.128 and .256), MASKMOVDQU, VMASKMOVDQU next to a page that is
  unmapped or read-only, every mask kind, at CPL 3 and 0, block sizes default and 1, against `maskmov_model` (the SDM's
  operation and the reference AMD machine's faults: the first faulting masked-in element's first faulting byte as CR2,
  nothing written by a faulting store, MASKMOVDQU one 16-byte write access whatever the mask, no access to a masked-off
  element of the VEX forms); #UD/#NM gating.
- `splitlock`: every LOCKed read-modify-write FEX lowers to an Arm atomic (ADD OR ADC SBB AND SUB XOR INC DEC NEG NOT XADD
  XCHG CMPXCHG CMPXCHG8B, locked and not), widths 2, 4, 8, crossing 16 bytes, 64 bytes and a page: memory, the result
  registers and RFLAGS against the same instruction on an aligned operand (FEX's native atomic path); a #PF on either part
  (CR2, error, nothing written); CMPXCHG16B misaligned is #GP(0). `splitlock-smp-N`: N = 1, 2, 4 vCPUs each make 20,000
  LOCK XADDs on one qword crossing 64 bytes and (vCPU 1, N > 1) 20,000 aligned LOCK ADD WORDs on its bytes 2-3: the final
  value must be exact. `splitlock-cost`: microseconds per split LOCK XADD at N = 1 and 4 against the aligned one.
- `refused`: forms natemu does not run yet are #UD at the instruction (the REFUSED decode counted), at CPL 3 and 0.
- `sweep`: every one-byte and two-byte opcode with ModRM.reg 0..7 (memory [rax]/[r8] at an unmapped address, and register
  forms), under 15 prefix combinations, and the VEX maps 1-3 with every pp, L and W, at CPL 3: the program must reach its
  end (every encoding a guest-visible outcome: a fault the IDT took, or completion), never a machine stop.

Writes DIR/nostop.json; exit status 1 on any failure."""
import argparse, json, pathlib, random, struct, subprocess, sys, time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / 'tools'), str(ROOT / 'tools' / 'reference')]
import ref, run_slice1_vm as vm  # noqa: E402

CODE = 0x100000
IDT, IDTR, RESUME, CNT, HIST, LOG = 0x300000, 0x301000, 0x302000, 0x302008, 0x302100, 0x303000
STACK = 0x3f0000
ERR = {8, 10, 11, 12, 13, 14, 17, 21}


def driver(a, text, work, extra=()):
    work.mkdir(parents=True, exist_ok=True)
    (work / 'program.txt').write_text(text)
    t0 = time.perf_counter()
    run = subprocess.run([a.host, a.runtime, 'run-program', a.fexc, work / 'program.txt', work, '--watchdog-ms', '600000', *map(str, extra)],
                         capture_output=True, text=True, timeout=900)
    return time.perf_counter() - t0, (json.loads(run.stdout) if run.returncode == 0 else None), run.stderr[-1500:]


def gprs(values=None):
    g = [0] * 16
    g[4] = STACK
    for r, v in (values or {}).items(): g[r] = v
    return 'gpr ' + ' '.join(f'{v:#x}' for v in g)


def harness(body, cpl, logs=256):
    """A program: LIDT of the logging IDT, then (CPL 3: switched there) `body`, then the end. Returns (text, labels)."""
    stubs = ''.join(f'v{v}:\n' + ('' if v in ERR else 'push 0\n') + f'push {v}\njmp common\n' for v in range(32))
    common = (f'common:\npush rax\npush rbx\nmov rbx, [{CNT:#x}]\ncmp rbx, {logs}\njae 1f\nshl rbx, 5\nadd rbx, {LOG:#x}\n'
              'mov rax, [rsp + 16]\nmov [rbx], rax\nmov rax, [rsp + 24]\nmov [rbx + 8], rax\nmov rax, cr2\nmov [rbx + 16], rax\n'
              'mov rax, [rsp + 32]\nmov [rbx + 24], rax\n1:\ninc qword ptr [' + f'{CNT:#x}]\nmov rbx, [rsp + 16]\n'
              f'inc qword ptr [{HIST:#x} + rbx * 8]\nmov rax, [{RESUME:#x}]\nmov [rsp + 32], rax\npop rbx\npop rax\nadd rsp, 16\niretq\n')
    code, lab = ref.assemble(f'lidt [{IDTR:#x}]\nmov rax, cr0\nor eax, 0x20\nmov cr0, rax\nsw:\n' + body + '\njmp done\n' + stubs + common + 'done: nop',
                             labels=True)
    gates = b''
    for v in range(32):
        h = CODE + lab[f'v{v}']
        gates += (h & 0xffff | 0x08 << 16 | 1 << 32 | 0x8e << 40 | (h >> 16 & 0xffff) << 48).to_bytes(8, 'little') + (h >> 32).to_bytes(8, 'little')
    text = (f'rip {CODE:#x}\nrflags 0x2\n{gprs()}\ncode {code.hex()}\ncpl 0\n' + (f'switch {CODE + lab["sw"]:#x} 3\n' if cpl == 3 else '')
            + f'if 0\nsse 1\nxcr0 7\nne 1\nmemory {IDT:#x} {gates.hex()}\nmemory {IDTR:#x} {((32 * 16 - 1).to_bytes(2, "little") + IDT.to_bytes(8, "little")).hex()}\n'
            f'memory {RESUME:#x} {bytes(0x100 + 8 * 32).hex()}\nmemory {LOG:#x} {bytes(32 * logs).hex()}\nregion 0x3e0000 0x10000\n')
    return text, lab


def mem(out, addr, n):
    for m in out.get('memory') or out['cpus'][0]['memory']:
        lo = int(m['addr'], 0)
        if lo <= addr and addr + n <= lo + len(m['data']) // 2:
            return bytes.fromhex(m['data'])[addr - lo:addr - lo + n]
    return None


def entries(out):
    n = min(int.from_bytes(mem(out, CNT, 8), 'little'), 256)
    raw = mem(out, LOG, 32 * n)
    return [struct.unpack_from('<4Q', raw, 32 * k) for k in range(n)]


class Report:
    def __init__(self, name):
        self.name, self.runs, self.failures = name, [], []

    def record(self, tag, problems, **extra):
        self.runs.append({'case': tag, 'ok': not problems, 'problems': problems[:8], **extra})
        print(json.dumps(self.runs[-1]), flush=True)
        if problems: self.failures.append(f'{self.name} {tag}: {problems[:3]}')


def stopped(out, err):
    if not out: return [f'driver: {err}']
    return [] if out['stop'] == 'reached' else [f"stop {out['stop']} {out.get('reason')} fault {out.get('fault')} rip {out.get('rip')}"]


# ---- masked moves -------------------------------------------------------------------------------------------------------
MASKED = [  # (mnemonic, element width, VEX.L, store)
    ('vmaskmovps', 4, 1, False), ('vmaskmovps', 4, 1, True), ('vmaskmovpd', 8, 0, False), ('vmaskmovpd', 8, 0, True),
    ('vpmaskmovd', 4, 0, False), ('vpmaskmovd', 4, 1, True), ('vpmaskmovq', 8, 1, False), ('vpmaskmovq', 8, 1, True),
    ('vmaskmovps', 4, 0, True), ('vmaskmovpd', 8, 1, False), ('vpmaskmovq', 8, 0, True), ('vpmaskmovd', 4, 1, False),
    ('maskmovdqu', 1, 0, True), ('vmaskmovdqu', 1, 0, True)]


def maskmov_model(ew, L, store, data, mask, dest, at, image, bad, ro, cpl):
    """SDM Vol.2 VMASKMOV/VPMASKMOV (no access to a masked-off element; loads zero it and the destination's upper bytes) and
    MASKMOVDQU (one 16-byte write access whatever the mask, the reference AMD machine's), all or nothing; the faults as the
    AMD machine gives them: #PF with CR2 the first faulting byte of the first faulting masked-in element (MASKMOVDQU: of the
    16 bytes). `image`: {address: byte}; `bad`: the page start that is unmapped (or `ro`: read-only). Returns (dest, image,
    fault or None)."""
    n = 16 * (1 + L) // ew if ew > 1 else 16
    on = [mask[ew * j + ew - 1] & 0x80 != 0 for j in range(n)]
    user = 4 if cpl == 3 else 0
    def check(addr, length, write):
        for b in range(addr, addr + length):
            if b >> 12 == bad >> 12 and (not ro or write):
                return (14, (1 if ro else 0) | (2 if write else 0) | user, b)
        return None
    if ew == 1:
        f = check(at, 16, True)
        if f: return dest, image, f
    else:
        for j in range(n):
            f = on[j] and check(at + ew * j, ew, store)
            if f: return dest, image, f
    image, d = dict(image), bytearray(32)
    for j in range(n):
        if on[j]:
            for b in range(ew):
                if store: image[at + ew * j + b] = data[ew * j + b]
                else: d[ew * j + b] = image[at + ew * j + b]
    return (dest if store else bytes(d)), image, None


def maskmov(a, report):
    r = Report('maskmov')
    rng = random.Random(20261106)
    T, R, BASE = 0x400000, 0x480000, 0x500000
    for cpl in (3, 0):
        cases, body, extra = [], '', ''
        for i, (mn, ew, L, store) in enumerate([(m, e, l, s) for m, e, l, s in MASKED for _ in range(10)]):
            n = 16 * (1 + L) // ew if ew > 1 else 16
            kind = i % 10
            ro = kind >= 7
            page = BASE + 0x2000 * i
            bad = page + 0x1000
            off = [16 * (1 + L), 16 * (1 + L) + 2, 8, 4, 12, 0, 16 * (1 + L), 16 * (1 + L), 8, 0][kind]   # bytes before `bad`
            at = bad - off
            masks = [(1 << n) - 1, 0, (1 << (n // 2)) - 1, 1 << (n - 1), 0b101 & ((1 << n) - 1), rng.getrandbits(n),
                     1 << (n // 2), (1 << n) - 1, 1, 1 << (n - 1)]
            bits = masks[kind]
            mask = bytearray(rng.getrandbits(8) & 0x7f for _ in range(32))
            for j in range(n): mask[ew * j + ew - 1] |= 0x80 if bits >> j & 1 else 0
            data, dest = bytes(rng.getrandbits(8) for _ in range(32)), bytes(rng.getrandbits(8) for _ in range(32))
            image = {page + k: (page + k) * 7 & 0xff for k in range(0x2000)}
            regs = 'xmm' if (L == 0) else 'ymm'
            if ew == 1:
                insn = f'{mn} xmm1, xmm2'
            elif store:
                insn = f'{mn} [rdi], {regs}2, {regs}1'
            else:
                insn = f'{mn} {regs}3, {regs}2, [rdi]'
            ld = 'movdqu' if mn == 'maskmovdqu' else 'vmovdqu'
            body += (f'lea rax, [rip + n{i}]\nmov [{RESUME:#x}], rax\n{ld} {"xmm" if ld == "movdqu" else "ymm"}1, [{T + 96 * i:#x}]\n'
                     f'{ld} {"xmm" if ld == "movdqu" else "ymm"}2, [{T + 96 * i + 32:#x}]\nvmovdqu ymm3, [{T + 96 * i + 64:#x}]\n'
                     f'mov rdi, {at:#x}\ng{i}: {insn}\nn{i}: vmovdqu [{R + 32 * i:#x}], ymm3\n')
            want_dest, want_image, fault = maskmov_model(ew, L, store, data, bytes(mask), dest, at, image, bad, ro, cpl)
            cases.append(dict(i=i, mn=mn, store=store, page=page, dest=want_dest, image=want_image, fault=fault, d=data + bytes(mask) + dest))
            extra += f'memory {page:#x} {bytes(image[page + k] for k in range(0x2000)).hex()}\n' + (f'readonly {bad:#x}\n' if ro else f'unmap {bad:#x}\n')
        text, lab = harness(body, cpl)
        text += f'memory {T:#x} {b"".join(c["d"] for c in cases).hex()}\nmemory {R:#x} {bytes(32 * len(cases)).hex()}\n' + extra
        for block in (None, 1):
            tag = f'cpl{cpl}-{block}'
            _, out, err = driver(a, text, a.out / 'maskmov' / tag, [block] if block else [])
            problems = stopped(out, err)
            if out and not problems:
                log = [e for e in entries(out)]
                k = 0
                for c in cases:
                    if c['fault']:
                        if k >= len(log) or log[k] != (c['fault'][0], c['fault'][1], c['fault'][2], CODE + lab[f'g{c["i"]}']):
                            problems.append(f"{c['mn']} #{c['i']}: log {log[k] if k < len(log) else None}, want {c['fault']} at g{c['i']}")
                        k += 1
                    got = mem(out, c['page'], 0x2000)
                    if got != bytes(c['image'].get(c['page'] + q, 0) for q in range(0x2000)):
                        problems.append(f"{c['mn']} #{c['i']}: memory differs")
                    if not c['store'] and mem(out, R + 32 * c['i'], 32) != c['dest']:
                        problems.append(f"{c['mn']} #{c['i']}: dest {mem(out, R + 32 * c['i'], 32).hex()}, want {c['dest'].hex()}")
                if k != len(log): problems.append(f'{len(log)} faults logged, want {k}')
                if block is None and not out['stats']['decodes'].get(f'{0x7c:#x}'): problems.append('no MASKMOV decoded')
            r.record(tag, problems, cases=len(cases))
    # Gating: CR4.OSXSAVE clear (#UD, VEX), CR0.TS (#NM, both), CR0.EM (#UD, MASKMOVDQU).
    for name, pre, insn, vector in (('ts-vex', 'mov rax, cr0\nor rax, 8\nmov cr0, rax\n', 'vmaskmovps [rdi], ymm2, ymm1', 7),
                                    ('ts-sse', 'mov rax, cr0\nor rax, 8\nmov cr0, rax\n', 'maskmovdqu xmm1, xmm2', 7),
                                    ('em-sse', 'mov rax, cr0\nor rax, 4\nmov cr0, rax\n', 'maskmovdqu xmm1, xmm2', 6),
                                    ('osxsave', 'mov rax, cr4\nand rax, -0x40001\nmov cr4, rax\n', 'vpmaskmovq ymm3, ymm2, [rdi]', 6)):
        text, lab = harness(pre + f'lea rax, [rip + n]\nmov [{RESUME:#x}], rax\nmov rdi, 0x500000\ng: {insn}\nn: nop\n', 0)
        _, out, err = driver(a, text + 'memory 0x500000 ' + bytes(32).hex() + '\n', a.out / 'maskmov' / name)
        problems = stopped(out, err)
        if out and not problems:
            log = entries(out)
            if [(e[0], e[3]) for e in log] != [(vector, CODE + lab['g'])]: problems.append(f'log {log}, want #{vector} at g')
        r.record(name, problems)
    report['maskmov'] = r.__dict__
    return r.failures


# ---- split locks --------------------------------------------------------------------------------------------------------
SIZE = {2: 'word', 4: 'dword', 8: 'qword'}
REG = {2: ('si', 'ax', 'cx'), 4: ('esi', 'eax', 'ecx'), 8: ('rsi', 'rax', 'rcx')}


def split_ops(w):
    s, a_, _ = REG[w]
    p = f'{SIZE[w]} ptr [rdi]'
    ops = [f'lock {o} {p}, {s}' for o in ('add', 'or', 'adc', 'sbb', 'and', 'sub', 'xor')]
    ops += [f'lock {o} {p}' for o in ('inc', 'dec', 'neg', 'not')]
    ops += [f'lock add {p}, 0x55', f'lock xadd {p}, {s}', f'xchg {p}, {s}', f'lock cmpxchg {p}, {s}']
    if w == 8: ops += ['lock cmpxchg8b qword ptr [rdi]', 'cmpxchg8b qword ptr [rdi]']
    return ops


def splitlock(a, report):
    r = Report('splitlock')
    rng = random.Random(61106)
    A, S, R = 0x1000000, 0x2000000, 0x3000000
    for cpl in (3, 0):
        body, cases = '', []
        for w in (2, 4, 8):
            for op in split_ops(w):
                for off in (16 - w + 1, 64 - w + 2 if w > 2 else 63, 4096 - w + 1):
                    for cf in (0, 1):
                        i = len(cases)
                        val, src = rng.getrandbits(64), rng.getrandbits(64)
                        equal = i % 3 == 0   # CMPXCHG*: RAX (EDX:EAX) equal to the operand (a third) or not
                        rax = val & ((1 << 8 * w) - 1) if equal else rng.getrandbits(64)
                        rdx = val >> 32 if equal else rng.getrandbits(32)
                        a_at, s_at = A + 0x2000 * i, S + 0x2000 * i + off
                        def one(at, k):
                            return (f'mov rdi, {at:#x}\nmov rsi, {src:#x}\nmov rax, {rax:#x}\n'
                                    f'mov rdx, {rdx:#x}\nmov rbx, {src:#x}\nmov rcx, {src >> 7:#x}\n'
                                    + ('stc\n' if cf else 'clc\n') + f'{op}\npushfq\npop r9\nmov [{R + 64 * i + 32 * k:#x}], r9\n'
                                    f'mov [{R + 64 * i + 32 * k + 8:#x}], rax\nmov [{R + 64 * i + 32 * k + 16:#x}], rsi\nmov [{R + 64 * i + 32 * k + 24:#x}], rdx\n')
                        body += one(a_at, 0) + one(s_at, 1)
                        cases.append(dict(op=op, w=w, off=off, a=a_at, s=s_at, val=val))
        text, lab = harness(f'lea rax, [rip + end]\nmov [{RESUME:#x}], rax\n' + body + 'end: nop\n', cpl)
        for c in cases:
            v = c['val'].to_bytes(8, 'little')
            text += f'memory {c["a"]:#x} {v.hex()}\nmemory {c["s"] & ~0xfff:#x} {(bytes((c["s"] & 0xfff)) + v + bytes(16)).hex()}\n'
        text += f'memory {R:#x} {bytes(64 * len(cases)).hex()}\n'
        for block in (None, 1):
            tag = f'cpl{cpl}-{block}'
            _, out, err = driver(a, text, a.out / 'splitlock' / tag, [block] if block else [])
            problems = stopped(out, err)
            if out and not problems:
                if entries(out): problems.append(f'faults {entries(out)[:4]}')
                for i, c in enumerate(cases):
                    ra, rs = bytearray(mem(out, R + 64 * i, 32)), bytearray(mem(out, R + 64 * i + 32, 32))
                    if c['op'].split()[1] in ('or', 'and', 'xor'):   # AF is undefined after AND/OR/XOR (SDM): not compared
                        ra[0] &= ~0x10; rs[0] &= ~0x10
                    ma, ms = mem(out, c['a'], 8), mem(out, c['s'], 8)
                    if ra != rs or ma != ms:
                        problems.append(f"{c['op']} off {c['off']:#x}: split flags/rax/rsi/rdx {rs.hex()} mem {ms.hex()}, aligned {ra.hex()} {ma.hex()}")
                splits = out['stats'].get('splits', None)
                if block is None and splits is not None and splits < len(cases): problems.append(f'splits {splits} < {len(cases)}')
            r.record(tag, problems, cases=len(cases))
    # Faults on either part (CPL 3): the second page unmapped or read-only, the first read-only; memory unchanged.
    fcases = []
    body = ''
    for i, (kind, w, op) in enumerate((k, w, op) for k in ('second-unmapped', 'second-readonly', 'first-readonly') for w in (2, 4, 8)
                                       for op in (f'lock add {SIZE[w]} ptr [rdi], 1', f'xchg {SIZE[w]} ptr [rdi], {REG[w][0]}')):
        page = S + 0x2000 * i
        at = page + 0x1000 - w + 1
        body += f'lea rax, [rip + n{i}]\nmov [{RESUME:#x}], rax\nmov rdi, {at:#x}\ng{i}: {op}\nn{i}:\n'
        fcases.append((i, kind, w, op, page, at))
    for cpl in (3, 0):
        text, lab = harness(body, cpl)
        for i, kind, w, op, page, at in fcases:
            text += f'memory {page:#x} {bytes((page + k) * 5 & 0xff for k in range(0x2000)).hex()}\n'
            text += {'second-unmapped': f'unmap {page + 0x1000:#x}\n', 'second-readonly': f'readonly {page + 0x1000:#x}\n',
                     'first-readonly': f'readonly {page:#x}\n'}[kind]
        _, out, err = driver(a, text, a.out / 'splitlock' / f'faults-cpl{cpl}')
        problems = stopped(out, err)
        if out and not problems:
            log = entries(out)
            u = 4 if cpl == 3 else 0
            for k, (i, kind, w, op, page, at) in enumerate(fcases):
                want = {'second-unmapped': (14, 2 | u, page + 0x1000), 'second-readonly': (14, 3 | u, page + 0x1000),
                        'first-readonly': (14, 3 | u, at)}[kind] + (CODE + lab[f'g{i}'],)
                if k >= len(log) or log[k] != want: problems.append(f'{kind} {op}: {log[k] if k < len(log) else None}, want {want}')
                if mem(out, page, 0x2000)[0xff0:0x1000] != bytes((page + q) * 5 & 0xff for q in range(0xff0, 0x1000)):
                    problems.append(f'{kind} {op}: first page written')
        r.record(f'faults-cpl{cpl}', problems)
    # CMPXCHG16B on an operand crossing 16 bytes: #GP(0) (it must be 16-byte aligned).
    text, lab = harness(f'lea rax, [rip + n]\nmov [{RESUME:#x}], rax\nmov rdi, {S + 8:#x}\ng: lock cmpxchg16b xmmword ptr [rdi]\nn: nop\n', 3)
    _, out, err = driver(a, text + f'memory {S:#x} {bytes(64).hex()}\n', a.out / 'splitlock' / 'cmpxchg16b')
    problems = stopped(out, err)
    if out and not problems and [(e[0], e[1], e[3]) for e in entries(out)] != [(13, 0, CODE + lab['g'])]: problems.append(f'log {entries(out)}')
    r.record('cmpxchg16b-misaligned', problems)
    # SMP: exact sums; vCPU 1 also adds 1 << 16 through an aligned LOCK ADD WORD on the counter's bytes 2-3.
    N, B = 20_000, 0x500000
    X = B + 62
    for n in (1, 2, 4):
        bodies, cpus = '', ''
        for v in range(n):
            code = ref.assemble(f'mov ecx, {N}\nmov esi, 1\n1: mov rax, rsi\nlock xadd qword ptr [{X:#x}], rax\n'
                                + (f'lock add word ptr [{B + 64:#x}], 1\n' if v == 1 else '') + 'dec ecx\njnz 1b\nnop')
            at = CODE + 0x10000 * v
            bodies += f'body {at:#x} {code.hex()}\n'
            cpus += f'cpu\nrip {at:#x}\nrflags 0x2\n{gprs({4: STACK - 0x2000 * v})}\n'
        text = bodies + f'memory {B:#x} {bytes(128).hex()}\nregion 0x3e0000 0x10000\n' + cpus
        dt, out, err = driver(a, text, a.out / 'splitlock' / f'smp-{n}')
        problems = [f'driver: {err}'] if not out else [f"cpu{k} {c['stop']} {c.get('reason')}" for k, c in enumerate(out['cpus']) if c['stop'] != 'reached']
        want = n * N + ((N << 16) if n > 1 else 0)
        got = out and int.from_bytes(mem(out, X, 8), 'little')
        if out and got != want: problems.append(f'sum {got:#x}, want {want:#x}')
        r.record(f'smp-{n}', problems, seconds=round(dt, 2))
    # Cost: N split LOCK XADDs against N aligned ones, 1 and 4 vCPUs (the others spinning in a linked loop on their own data).
    cost = {}
    for n in (1, 4):
        for name, addr in (('split', X), ('aligned', B + 64)):
            times = []
            for count in (20_000, 40_000):
                code = ref.assemble(f'mov ecx, {count}\n1: lock xadd qword ptr [{addr:#x}], rax\ndec ecx\njnz 1b\nnop')
                spin = ref.assemble(f'mov ecx, {count * 50}\n1: add qword ptr [rsp - 64], 1\ndec ecx\njnz 1b\nnop')
                text = f'body {CODE:#x} {code.hex()}\n' + ''.join(f'body {CODE + 0x10000 * v:#x} {spin.hex()}\n' for v in range(1, n))
                text += f'memory {B:#x} {bytes(128).hex()}\nregion 0x3e0000 0x10000\n' + ''.join(
                    f'cpu\nrip {CODE + 0x10000 * v:#x}\nrflags 0x2\n{gprs({4: STACK - 0x2000 * v})}\n' for v in range(n))
                dt, out, err = driver(a, text, a.out / 'splitlock' / f'cost-{name}-{n}-{count}')
                times.append(dt if out else None)
            cost[f'{name}-n{n}'] = None if None in times else round((times[1] - times[0]) / 20_000 * 1e6, 3)
    r.record('cost', [], us_per_op=cost)
    report['splitlock'] = r.__dict__
    return r.failures


# ---- refused forms and the sweep ----------------------------------------------------------------------------------------
REFUSED_FORMS = {   # forms natemu does not run yet (correctness-nostop.md, "Left as #UD"), as bytes
    'shld16-imm17': '660fa4c811',
    'cmpxchg8b-66': '660fc70f', 'fbld': 'df27', 'fbstp': 'df37', 'x87-alias-dc-d0': 'dcd0', 'sal6': 'd1f0', 'test1': 'f7c800000000',
    'repne-movs': 'f2a4', 'rep-inc': 'f3ffc0', '66-f3-cvttss2si': '66f30f2cc1'}


def refused(a, report):
    r = Report('refused')
    for cpl in (3, 0):
        body = ''
        for i, (name, hexs) in enumerate(REFUSED_FORMS.items()):
            body += (f'lea rax, [rip + n{i}]\nmov [{RESUME:#x}], rax\nmov rdi, 0x500000\nmov rsp, 0x3e8000\nmov rcx, 3\ng{i}: .byte '
                     + ','.join('0x' + hexs[k:k + 2] for k in range(0, len(hexs), 2)) + f'\nn{i}: nop\n')
        text, lab = harness(body, cpl)
        _, out, err = driver(a, text + f'memory 0x500000 {bytes(64).hex()}\n', a.out / 'refused' / f'cpl{cpl}')
        problems = stopped(out, err)
        if out and not problems:
            log = entries(out)
            for i, name in enumerate(REFUSED_FORMS):
                e = log[i] if i < len(log) else None
                if not e or (e[0], e[3]) != (6, CODE + lab[f'g{i}']): problems.append(f'{name}: {e}, want #UD at it')
            got = out['stats']['decodes'].get(f'{0x7b:#x}', 0)
            if got < len(REFUSED_FORMS): problems.append(f'{got} REFUSED decodes, want {len(REFUSED_FORMS)}')
        r.record(f'cpl{cpl}', problems)
    report['refused'] = r.__dict__
    return r.failures


PREFIXES = [b'', b'\x66', b'\xf2', b'\xf3', b'\xf0', b'\x67', b'\x48', b'\x41', b'\x2e', b'\x64', b'\x66\xf3', b'\x66\xf2',
            b'\xf2\xf3', b'\xf0\x66', b'\x66\x48']
UNMAPPED = 0x5000_0000


def sweep_slots(kind):
    """The encodings of one part: ('one'|'two'|'vex')."""
    out = []
    for op in range(256):
        for reg in range(8):
            for modrm in (reg << 3, 0xc0 | reg << 3):
                if kind == 'vex':
                    for m in (1, 2, 3):
                        for pp in range(4):
                            for L in (0, 1):
                                if reg % 2: continue   # ModRM.reg 0, 2, 4, 6 (VEX.R/vvvv vary below)
                                for W in (0, 1):
                                    out.append(bytes([0xc4, 0xe0 | m, W << 7 | 0x78 | L << 2 | pp, op, modrm]))
                else:
                    # A rel8 branch takes the byte after its opcode as its displacement: kept forward (a backward one would
                    # loop through the slot's own setup).
                    rel8 = kind == 'one' and (0x70 <= op <= 0x7f or 0xe0 <= op <= 0xe3 or op == 0xeb)
                    for p in PREFIXES:
                        out.append(p + (bytes([op]) if kind == 'one' else bytes([0x0f, op])) + bytes([modrm & 0x7f if rel8 else modrm]))
    return out


def sweep(a, report):
    r = Report('sweep')
    setup = b''.join(bytes([0xb8 + k]) + UNMAPPED.to_bytes(4, 'little') for k in (0, 2, 3, 5, 6, 7))   # mov eax/edx/ebx/ebp/esi/edi
    setup += b'\xbc' + UNMAPPED.to_bytes(4, 'little') + b'\x41\xb8' + UNMAPPED.to_bytes(4, 'little') + b'\xb9\x01\x00\x00\x00' + b'\xfc'
    for kind in ('one', 'two', 'vex'):
        slots = sweep_slots(kind)
        for part in range(0, len(slots), 20_000):
            chunk = slots[part:part + 20_000]
            SWEEP = 0x1000000
            text, lab = harness(f'mov rax, {SWEEP:#x}\njmp rax\n', 3)
            code = bytearray()
            for s in chunk:
                end = SWEEP + len(code) + 11 + len(setup) + 24 + 5
                code += b'\xc7\x04\x25' + RESUME.to_bytes(4, 'little') + end.to_bytes(4, 'little')   # mov dword [RESUME], end
                code += setup + s + bytes(24 - len(s)) + b'\xe9' + bytes(4)                           # zero padding, jmp end
            code += ref.assemble(f'mov rax, {CODE + lab["done"]:#x}\njmp rax')
            text += f'body {SWEEP:#x} {bytes(code).hex()}\n'
            tag = f'{kind}-{part // 20_000}'
            dt, out, err = driver(a, text, a.out / 'sweep' / tag)
            problems = stopped(out, err)
            hist = {}
            if out:
                raw = mem(out, HIST, 8 * 32)
                hist = {v: int.from_bytes(raw[8 * v:8 * v + 8], 'little') for v in range(32) if raw[8 * v:8 * v + 8] != bytes(8)}
            r.record(tag, problems, slots=len(chunk), seconds=round(dt, 1), vectors=hist,
                     refused=out and out['stats']['decodes'].get(f'{0x7b:#x}', 0))
    report['sweep'] = r.__dict__
    return r.failures


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--fexc', type=pathlib.Path, required=True)
    ap.add_argument('--out', type=pathlib.Path, required=True)
    ap.add_argument('--only', type=lambda s: s.split(','), default=['maskmov', 'splitlock', 'refused', 'sweep'])
    ap.add_argument('--host', type=pathlib.Path)
    ap.add_argument('--runtime', type=pathlib.Path)
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    if a.host and a.host.is_dir(): a.host = a.host / 'natemu-host'
    if not (a.host and a.runtime): a.host, a.runtime = vm.build()
    report, failures = {}, []
    for g in ('maskmov', 'splitlock', 'refused', 'sweep'):
        if g in a.only: failures += globals()[g](a, report)
    (a.out / 'nostop.json').write_text(json.dumps(report, indent=1) + '\n')
    for f in failures: print('FAIL', f)
    sys.exit(1 if failures else 0)


if __name__ == '__main__': main()
