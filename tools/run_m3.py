#!/usr/bin/env python3
"""M3 checks: guest accesses to device pages (guest-physical pages that are not RAM) complete exactly once, with x86
semantics, through the host's device registry (boot-features plan, M3).

    tools/with-env python3 tools/run_m3.py --fexc FEXC --out DIR [--programs 60] [--seed S] [--kicks 1000]
        [--iterations 20000] [--cpus 1 2 3 4] [--xmc-iterations N] [--xmc-peer N] [--only corpus,faults,refusals,kicks,contain,xmc]

Every description maps linear page 0x200000 to guest-physical 0xfeb00000 (`mmio` item), where the driver registers its
test device (`crates/host/src/devices.rs` TestDevice: a counter register at 0xf00 whose every read counts, a sequence
register at 0xf08 whose n-th write by a vCPU must carry n, a last 8 bytes that refuse every access, plain memory elsewhere;
every access counted and logged). Needs fexc series23-m3 (completion records on load/store sites).

- `corpus`: seeded straight-line MOV-family programs (MOV loads and stores of 1/2/4/8 bytes incl. AH..BH and REX byte
  registers, MOV of an immediate, MOVZX, MOVSX, MOVSXD, moffs, absolute, RIP-relative, base+index*scale+disp, FS/GS) on
  the device page. Registers, RFLAGS, RIP and the page's final bytes against pinned QEMU running the same program on RAM
  (`tools/reference/ref.py`: a RAM-like device must give RAM's results), and the device's access log (offset, size,
  direction, value, in order: each instruction exactly once) against `tools/models/mmio_model.py` (independent author),
  which also rederives the registers. Also 6 fixed encodings (REX before 66, judged by the SDM where pinned QEMU differs;
  66 before REX.W, BPL/CH stores, imm16, a 16-bit moffs). Block sizes default and 1, CPL 3 and 0.
- `faults`: the guest tables' faults come before the device (#PF not present / user-supervisor / read-only / reserved bit /
  an upper level not present / the second page of a crossing access, #GP and #SS non-canonical, #AC under alignment
  checking), each delivered with its vector, error code and CR2 at the instruction, the device untouched; the passing
  cases (a read-only page read, CPL 0 on a supervisor page, aligned under AC) complete once. ADD, LOCK ADD and XCHG on a
  read-only device page give #PF(7) before their refusal. Identical loads and stores back to back in one block each reach
  the device. CLFLUSH of a device page completes without a device access. Judged by the SDM (natemu only: QEMU's Linux-user mode has no device pages).
- `refusals`: every form the device path does not complete stops `unsupported_mmio` with its reason, RIP at the
  instruction and the device untouched: read-modify-write, compare, BT, CMPXCHG, SETcc, PUSH/POP, MOVNTI, string, SSE,
  LOCK and XCHG (`atomic`), a load or store crossing between RAM and the device page either way (`crossing`), a page no device claims
  (`unassigned`), an access the device refuses (`device`).
- `kicks`: 1-4 vCPUs loop `iterations` times over a counter read (each vCPU checks the values it gets increase) and a
  sequence write (n-th write carries n) while host kickers stop each vCPU at random times (`--kicks` per vCPU): every vCPU
  reaches its end, the device saw exactly `iterations` reads and writes from each (no access dropped or repeated by a
  stop), no order error, no non-increasing counter value, kick accounting exact. Block sizes default and 1.
- `contain`: a device reply with an unknown status (`--tamper mmio`) halts `bad_reply` with nothing committed (RIP and RAX
  at the instruction).
- `xmc`: cross-modifying code at a device access (see `xmc`): old or new instruction, never a stop, exactly one access per
  round.

Writes DIR/m3.json; exit status 1 on any failure."""
import argparse, concurrent.futures as cf, json, pathlib, random, subprocess, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools')); sys.path.insert(0, str(ROOT / 'tools' / 'reference')); sys.path.insert(0, str(ROOT / 'tools' / 'models'))
import ref, run_slice1_vm as vm  # noqa: E402
from run_slice3_vm import kick_checks  # noqa: E402

CODE, DEV, GPA, PREV, NEXT = 0x100000, 0x200000, 0xfeb0_0000, 0x1ff000, 0x201000
M64 = 2**64 - 1
REGS = ['rax', 'rcx', 'rdx', 'rbx', 'rsp', 'rbp', 'rsi', 'rdi'] + [f'r{i}' for i in range(8, 16)]
R32 = ['eax', 'ecx', 'edx', 'ebx', 'esp', 'ebp', 'esi', 'edi'] + [f'r{i}d' for i in range(8, 16)]
R16 = ['ax', 'cx', 'dx', 'bx', 'sp', 'bp', 'si', 'di'] + [f'r{i}w' for i in range(8, 16)]
R8 = ['al', 'cl', 'dl', 'bl', 'spl', 'bpl', 'sil', 'dil'] + [f'r{i}b' for i in range(8, 16)]
HIGH = ['ah', 'ch', 'dh', 'bh']
SIZE = {1: 'byte', 2: 'word', 4: 'dword', 8: 'qword'}
NAMES = {1: R8, 2: R16, 4: R32, 8: REGS}
BASE, INDEX, FS_PTR, GS_PTR = 3, 6, 12, 13          # rbx = DEV, rsi = small index, r12/r13 = DEV - FS/GS base
DATA_REGS = [0, 1, 2, 5, 7, 8, 9, 10, 11, 14, 15]   # never an address register or rsp


def driver(host, runtime, fexc, text, work, extra=()):
    work.mkdir(parents=True, exist_ok=True)
    (work / 'program.txt').write_text(text)
    run = subprocess.run([host, runtime, 'run-program', fexc, work / 'program.txt', work, *vm.CODE_SHAPE, *map(str, extra)],
                         capture_output=True, text=True, timeout=600)
    return run.returncode, (json.loads(run.stdout) if run.returncode == 0 else None), run.stderr[-1500:]


def mmio_lines(data=None):
    return f'mmio {DEV:#x} {GPA:#x}\n' + (f'memory {DEV:#x} {data.hex()}\n' if data is not None else '')


# ---- corpus ---------------------------------------------------------------------------------------------------------
class Gen:
    def __init__(self, rng, fs, gs):
        self.rng, self.fs, self.gs, self.code, self.insns, self.lines = rng, fs, gs, b'', [], []

    def operand(self, w, high=False):
        """A memory operand of width `w` on the device page (offsets below 0x800: away from the device registers)."""
        r = self.rng
        off = r.randrange(0, 0x800 - w)
        forms = [(f'[rbx + {off}]', None)] + [(f'[rbx + rsi*{s} + {off - 3 * s}]', s) for s in (1, 2, 4, 8)]
        forms += [(f'[{DEV + off}]', None), ('rip', off)] + ([] if high else [(f'fs:[r12 + {off}]', None), (f'gs:[r13 + {off}]', None)])
        text, extra = r.choice(forms)
        return text, off, extra

    def add(self, text, ann):
        self.code += ref.assemble(text); self.insns.append(ann); self.lines.append(text)

    def mem(self, w, high=False):
        text, off, extra = self.operand(w, high)
        if text != 'rip':
            return f'{SIZE[w]} ptr {text}'
        return ('rip', off)

    def emit(self, template, w, ann, high=False):
        """`template` holds `{m}` for the memory operand; a RIP-relative one is placed from the instruction's length."""
        m = self.mem(w, high)  # (AH..BH cannot take r12/r13's REX)
        if isinstance(m, str):
            return self.add(template.format(m=m), ann)
        at = CODE + len(self.code)
        probe = ref.assemble(template.format(m=f'{SIZE[w]} ptr [rip + 0x1000]'))
        disp = DEV + m[1] - (at + len(probe))
        self.add(template.format(m=f'{SIZE[w]} ptr [rip + {disp}]'), ann)

    def one(self):
        r = self.rng
        kind = r.choice(['load', 'load', 'store', 'store', 'imm', 'movzx', 'movsx', 'movsxd', 'moffs'])
        d = r.choice(DATA_REGS)
        if kind == 'load':
            w = r.choice([1, 2, 4, 8])
            reg = r.choice(HIGH[:3]) if w == 1 and r.random() < 0.3 else NAMES[w][d]   # (not BH: RBX is the base)
            self.emit(f'mov {reg}, {{m}}', w, {'op': 'mov', 'width': 8 * w}, reg in HIGH)
        elif kind == 'store':
            w = r.choice([1, 2, 4, 8])
            reg = r.choice(HIGH) if w == 1 and r.random() < 0.3 else NAMES[w][d]
            self.emit(f'mov {{m}}, {reg}', w, {'op': 'mov', 'width': 8 * w}, reg in HIGH)
        elif kind == 'imm':
            w = r.choice([1, 2, 4, 8])
            imm = r.randrange(-(1 << min(31, 8 * w - 1)), 1 << min(31, 8 * w - 1))
            self.emit(f'mov {{m}}, {imm}', w, {'op': 'mov', 'width': 8 * w})
        elif kind in ('movzx', 'movsx'):
            w, dest = r.choice([(1, 2), (1, 4), (1, 8), (2, 4), (2, 8)])
            self.emit(f'{kind} {NAMES[dest][d]}, {{m}}', w, {'op': kind, 'width': 8 * dest})
        elif kind == 'movsxd':
            self.emit(f'movsxd {REGS[d]}, {{m}}', 4, {'op': 'movsx', 'width': 64})
        else:
            w = r.choice([1, 2, 4, 8]); off = r.randrange(0, 0x800 - w)
            acc = {1: 'al', 2: 'ax', 4: 'eax', 8: 'rax'}[w]
            text = f'movabs {acc}, {SIZE[w]} ptr [{DEV + off}]' if r.random() < 0.5 else f'movabs {SIZE[w]} ptr [{DEV + off}], {acc}'
            self.add(text, {'op': 'mov', 'width': 8 * w})


def corpus_programs(n, seed):
    rng, out = random.Random(seed), []
    for i in range(n):
        fs = rng.choice([0, rng.randrange(0x1000_0000, 0x7fff_0000_0000), 0xffff_8000_0000_0000 + rng.randrange(0, 1 << 40)])
        gs = rng.choice([0, rng.randrange(0x1000_0000, 0x7fff_0000_0000)])
        g = Gen(rng, fs, gs)
        for _ in range(rng.randrange(8, 16)):
            g.one()
        gpr = [rng.randrange(0, 2**64) for _ in range(16)]
        gpr[BASE], gpr[INDEX], gpr[4] = DEV, 3, 0x7f0000
        gpr[FS_PTR], gpr[GS_PTR] = (DEV - fs) & M64, (DEV - gs) & M64
        data = bytes(rng.randrange(256) for _ in range(0x800))
        out.append({'name': f'm3-{i}', 'code': g.code.hex(), 'rip': hex(CODE), 'gpr': [hex(x) for x in gpr], 'rflags': hex(rng.choice([0, 0x8d5])),
                    'memory': [{'addr': hex(DEV), 'data': data.hex()}], 'insns': g.insns, 'fsbase': hex(fs), 'gsbase': hex(gs), 'asm': g.lines})
    # Fixed encodings whose prefixes a decoder can get wrong: REX before 66 (REX ignored: a 16-bit MOV), 66 before REX.W,
    # REX and legacy byte registers, a 16-bit immediate, a 16-bit moffs. Each at [rbx] = DEV + 0x10.
    for name, code, width in (('rex-before-66', '48668b03', 16), ('66-rex-w-imm', '6648c70378563412', 64), ('bpl-store', '40882b', 8),
                              ('ch-store', '882b', 8), ('imm16', '66c7037856', 16), ('moffs16', '66a11002200000000000', 16)):
        gpr = [0x1111_1111_1111_1111 * (i + 1) & M64 for i in range(16)]
        gpr[BASE], gpr[4] = DEV + 0x10, 0x7f0000
        # SDM Vol.2 2.2.1: a REX prefix not immediately before the opcode is ignored (`48 66 8b 03` is MOV AX, [RBX]); pinned
        # QEMU 7.2 keeps REX.W and loads 8 bytes.
        sdm = {'sdm_rax': gpr[0] & ~0xffff | 0x1110, 'sdm_width': 2} if name == 'rex-before-66' else {}
        out.append(sdm | {'name': f'm3-{name}', 'code': code, 'rip': hex(CODE), 'gpr': [hex(x) for x in gpr], 'rflags': '0x0',
                    'memory': [{'addr': hex(DEV), 'data': (bytes(range(256)) * 8).hex()}], 'insns': [{'op': 'mov', 'width': width}],
                    'fsbase': '0x0', 'gsbase': '0x0', 'asm': [code]})
    return out


def corpus(a, host, runtime, report):
    import mmio_model
    progs = corpus_programs(a.programs, a.seed)
    raw = pathlib.Path(__import__('os').environ.get('NATEMU_RAW_ROOT', ROOT.parent / 'natemu-raw'))
    qemu = raw / 'reference-slice1-20261026-01' / 'pin' / 'qemu-x86_64-static'
    expected_path = a.out / 'qemu.json'
    if not expected_path.exists():
        results, _ = ref.run_programs(progs, a.out / 'qemu-work', qemu)
        expected_path.write_text(json.dumps(results))
    expected = {r['name']: r for r in json.loads(expected_path.read_text())}
    failures = []
    def one(job):
        p, block, cpl = job
        text = vm.description(dict(p, memory=[]) | ({'cpl': cpl})) + mmio_lines(bytes.fromhex(p['memory'][0]['data']))
        rc, out, err = driver(host, runtime, a.fexc, text, a.out / 'corpus' / f"{p['name']}-{block}-{cpl}", [block] if block else [])
        if out is None: return [f'driver {rc}: {err}']
        diffs = ref.compare(expected[p['name']], [int(x, 0) for x in out['gpr']], int(out['rflags'], 0), int(out['rip'], 0), out['memory'])
        if 'sdm_rax' in p:      # QEMU differs from the SDM here (judged by the SDM's value instead)
            diffs = [d for d in diffs if not d.startswith('rax:')] + ([] if int(out['gpr'][0], 0) == p['sdm_rax'] else [f"rax {out['gpr'][0]} != SDM {p['sdm_rax']:#x}"])
        if out['stop'] != 'reached': diffs.insert(0, f"stopped {out['stop']} {out['reason']}")
        gpr = [int(x, 0) for x in p['gpr']]
        log = [(e[1], e[2], e[3], int(e[4], 0)) for e in out['devices'][0]['log']]
        try:
            model = mmio_model.run(bytes.fromhex(p['code']), CODE, gpr, int(p['fsbase'], 0), int(p['gsbase'], 0), DEV,
                                   bytes.fromhex(p['memory'][0]['data']) + bytes(0x800))
        except ValueError:      # the model refuses a prefix after REX (its documented choice): QEMU/SDM alone, one access
            width = p.get('sdm_width')
            ok = len(log) == 1 and out['mmio']['completed'] == 1 and (width is None or log[0][1] == width)
            return diffs + ([] if ok else [f'device log {log} (SDM width {width})'])
        if log != [tuple(x) for x in model['accesses']]: diffs.append(f"device log {log} != model {model['accesses']}")
        if model['gpr'] != [int(x, 0) for x in out['gpr']]: diffs.append('model registers differ')
        if model['device_bytes'][:0x800].hex() != out['memory'][0]['data']: diffs.append('model device bytes differ')
        if out['mmio']['completed'] != len(model['accesses']): diffs.append(f"completed {out['mmio']['completed']} != {len(model['accesses'])}")
        return diffs
    jobs = [(p, b, c) for p in progs for b in (None, 1) for c in (3, 0)]
    with cf.ThreadPoolExecutor(a.parallel) as pool:
        for (p, b, c), diffs in zip(jobs, pool.map(one, jobs)):
            if diffs: failures.append(f"{p['name']} block={b} cpl={c}: {'; '.join(diffs)[:600]}")
    report['corpus'] = {'programs': len(progs), 'runs': len(jobs), 'accesses': sum(len(p['insns']) for p in progs), 'failures': failures}
    return failures


# ---- faults, refusals, CLFLUSH ---------------------------------------------------------------------------------------
def regs(**kw):
    g = [0] * 16; g[BASE], g[4] = DEV, 0x3e8000
    for k, v in kw.items(): g[REGS.index(k)] = v & M64
    return 'gpr ' + ' '.join(hex(x) for x in g)


def touched(out):
    d = out['devices'][0]
    return sum(d['reads']) + sum(d['writes'])


def fault_cases():
    """(name, description, check(out) -> problems). Faults: vector, CR2, error at the instruction, device untouched."""
    asm, cases = ref.assemble, []
    def delivered(vec, cr2, err, at=CODE):
        def check(out):
            p = []
            if out['stop'] != 'guest_fault': p.append(f"stop {out['stop']} {out['reason']}")
            elif [int(x, 0) for x in out['fault']] != [vec, cr2, err, at]: p.append(f"fault {out['fault']} != {[vec, cr2, err, at]}")
            if touched(out): p.append('device touched')
            return p
        return check
    def completed(n, **want):
        def check(out):
            p = [] if out['stop'] == 'reached' else [f"stop {out['stop']} {out['reason']}"]
            if touched(out) != n or out['mmio']['completed'] != n: p.append(f"device accesses {touched(out)}, completed {out['mmio']['completed']} != {n}")
            for k, v in want.items():
                if int(out['gpr'][REGS.index(k)], 0) != v: p.append(f"{k} {out['gpr'][REGS.index(k)]} != {v:#x}")
            return p
        return check
    def case(name, text, check, extra='', cpl=3, gpr=None, rflags=0):
        code = asm(text)
        cases.append((f'fault-{name}', f'rip {CODE:#x}\nrflags {rflags:#x}\ncpl {cpl}\n{gpr or regs()}\ncode {code.hex()}\n{mmio_lines(bytes(16))}{extra}', check))
    load, store = 'mov eax, dword ptr [rbx + 8]', 'mov dword ptr [rbx + 8], eax'
    case('np-load', load, delivered(14, DEV + 8, 4), f'unmap {DEV:#x}\n')
    case('np-store', store, delivered(14, DEV + 8, 6), f'unmap {DEV:#x}\n')
    case('np-load-cpl0', load, delivered(14, DEV + 8, 0), f'unmap {DEV:#x}\n', cpl=0)
    case('supervisor-load', load, delivered(14, DEV + 8, 5), f'entry {DEV:#x} 1 clear 4\n')
    case('supervisor-store', store, delivered(14, DEV + 8, 7), f'entry {DEV:#x} 1 clear 4\n')
    case('supervisor-cpl0', load + '\nmov ecx, 1', completed(1, rcx=1), f'entry {DEV:#x} 1 clear 4\n', cpl=0)
    case('readonly-store', store, delivered(14, DEV + 8, 7), f'readonly {DEV:#x}\n')
    case('readonly-store-cpl0-wp', store, delivered(14, DEV + 8, 3), f'readonly {DEV:#x}\n', cpl=0)
    case('readonly-load', load + '\nmov ecx, 1', completed(1, rcx=1), f'readonly {DEV:#x}\n')
    case('reserved-bit', load, delivered(14, DEV + 8, 0xd), f'entry {DEV:#x} 1 set 0x8000000000000\n')
    case('pde-not-present', load, delivered(14, DEV + 8, 4), f'entry {DEV:#x} 2 clear 1\n')
    case('noncanonical', 'mov eax, dword ptr [rcx]', delivered(13, 0, 0), gpr=regs(rcx=0x8000_0000_0000_0000))
    case('noncanonical-stack', 'mov eax, dword ptr [rbp + 8]', delivered(12, 0, 0), gpr=regs(rbp=0x8000_0000_0000_0000))
    # A crossing access whose second page is not mapped: #PF there (both pages walked before any device access).
    case('crossing-np', 'mov rax, qword ptr [rbx + 0xffc]', delivered(14, NEXT, 4))
    case('clflush', 'clflush byte ptr [rbx + 8]\nmov ecx, 1', completed(0, rcx=1))
    # Priority before refusal: RMW, LOCK and XCHG forms (refused on device pages) on a read-only page are #PF(7) first.
    for name, text in (('add', 'add dword ptr [rbx + 8], eax'), ('lock-add', 'lock add dword ptr [rbx + 8], eax'), ('xchg', 'xchg dword ptr [rbx + 8], eax')):
        case(f'readonly-{name}', text, delivered(14, DEV + 8, 7), f'readonly {DEV:#x}\n')
    # Identical device accesses back to back in one block: none merged or dropped (counter 0 then 1; sequence 0 then 1).
    def twice(out):
        p = completed(4, rax=0, rcx=1)(out)
        d = out['devices'][0]
        if d['order_errors'] or [e[:4] for e in d['log']] != [[0, 0xf00, 4, 0], [0, 0xf00, 4, 0], [0, 0xf08, 8, 1], [0, 0xf08, 8, 1]]:
            p.append(f"log {d['log']} order errors {d['order_errors']}")
        return p
    case('back-to-back', 'mov eax, dword ptr [rbx + 0xf00]\nmov ecx, dword ptr [rbx + 0xf00]\n'
         'mov qword ptr [rbx + 0xf08], 0\nmov qword ptr [rbx + 0xf08], 1', twice)
    case('clflush-np', 'clflush byte ptr [rbx + 8]', delivered(14, DEV + 8, 4), f'unmap {DEV:#x}\n')
    # #AC (X2): CR0.AM at CPL 0, RFLAGS.AC, then CPL 3: misaligned #AC(0) before the device; aligned completes.
    head = asm('mov rax, cr0\nbts rax, 18\nmov cr0, rax\nxor eax, eax')
    for name, text, check in (('ac-misaligned', 'mov ecx, dword ptr [rbx + 2]', delivered(17, 0, 0, CODE + len(head) + 1)),
                              ('ac-aligned', 'mov ecx, dword ptr [rbx + 4]\nmov edx, 1', completed(1, rdx=1))):
        code = head + b'\x90' + asm(text)
        cases.append((f'fault-{name}', f'rip {CODE:#x}\nrflags 0x40000\ncpl 0\nswitch {CODE + len(head):#x} 3\n{regs()}\ncode {code.hex()}\n{mmio_lines(bytes(16))}', check))
    return cases


def refusal_cases():
    asm, cases = ref.assemble, []
    def refused(why):
        def check(out):
            p = []
            if (out['stop'], out['reason'].split()[0]) != ('resident', 'unsupported_mmio'): p.append(f"stop {out['stop']} {out['reason']}")
            if out['mmio']['status'] != why: p.append(f"status {out['mmio']['status']} != {why}")
            if int(out['rip'], 0) != CODE: p.append(f"rip {out['rip']}")
            if touched(out): p.append('device touched')
            return p
        return check
    forms = [('add', 'add dword ptr [rbx + 8], eax', 'form'), ('cmp', 'cmp eax, dword ptr [rbx + 8]', 'form'),
             ('test', 'test byte ptr [rbx + 8], 1', 'form'), ('inc', 'inc qword ptr [rbx + 8]', 'form'),
             ('bt', 'bt dword ptr [rbx + 8], ecx', 'form'), ('bts', 'bts dword ptr [rbx + 8], 3', 'form'),
             ('cmpxchg', 'cmpxchg dword ptr [rbx + 8], ecx', 'form'), ('setcc', 'sete byte ptr [rbx + 8]', 'form'),
             ('push', 'push qword ptr [rbx + 8]', 'form'), ('pop', 'pop qword ptr [rbx + 8]', 'form'),
             ('movnti', 'movnti dword ptr [rbx + 8], eax', 'form'), ('call', 'call qword ptr [rbx + 8]', 'form'),
             ('movsb', 'lea rsi, [rbx + 8]\nlea rdi, [rsp - 64]', None), ('lodsd', 'lea rsi, [rbx + 8]', None),
             ('lock-add', 'lock add dword ptr [rbx + 8], eax', 'atomic'), ('lock-xadd', 'lock xadd qword ptr [rbx + 8], rcx', 'atomic'),
             ('xchg', 'xchg dword ptr [rbx + 8], eax', 'atomic'), ('lock-cmpxchg', 'lock cmpxchg dword ptr [rbx + 8], ecx', 'atomic'),
             ('crossing-load', 'mov rax, qword ptr [rbx - 4]', 'crossing'), ('crossing-store', 'mov qword ptr [rbx - 4], rax', 'crossing'),
             ('crossing-store2', 'mov word ptr [rbx - 1], ax', 'crossing'),
             ('crossing-out-load', 'mov rax, qword ptr [rbx + 0xffc]', 'crossing'), ('crossing-out-store', 'mov dword ptr [rbx + 0xffe], eax', 'crossing')]
    for name, text, why in forms:
        extra = f'region {NEXT if "-out-" in name else PREV:#x} 0x1000\n' if name.startswith('crossing') else ''
        if why is None:     # string forms: the set-up runs first, then the string instruction is the one refused
            setup = asm(text)
            body = setup + asm('movsb' if name == 'movsb' else 'lodsd')
            code_at = CODE + len(setup)
            def check(out, code_at=code_at):
                p = refused('form')(dict(out, rip=hex(CODE)))
                if int(out['rip'], 0) != code_at: p.append(f"rip {out['rip']} != {code_at:#x}")
                return p
            cases.append((f'refuse-{name}', f'rip {CODE:#x}\nrflags 0\n{regs()}\ncode {body.hex()}\n{mmio_lines(bytes(16))}region 0x3e0000 0x10000\n', check))
            continue
        cases.append((f'refuse-{name}', f'rip {CODE:#x}\nrflags 0\n{regs()}\ncode {asm(text).hex()}\n{mmio_lines(bytes(16))}{extra}region 0x3e0000 0x10000\n', refused(why)))
    for name, text in (('movups', 'movups xmm0, xmmword ptr [rbx]'), ('movq', 'movq xmm0, qword ptr [rbx + 8]'), ('movd-store', 'movd dword ptr [rbx + 8], xmm1')):
        cases.append((f'refuse-{name}', f'rip {CODE:#x}\nrflags 0\nsse 1\n{regs()}\ncode {asm(text).hex()}\n{mmio_lines(bytes(16))}', refused('form')))
    def unassigned(out):
        p = []
        if (out['stop'], out['reason'].split()[0]) != ('resident', 'unsupported_mmio'): p.append(f"stop {out['stop']} {out['reason']}")
        if out['mmio']['status'] != 'unassigned' or int(out['rip'], 0) != CODE: p.append(f"{out['mmio']} rip {out['rip']}")
        return p
    cases.append(('refuse-device', f'rip {CODE:#x}\nrflags 0\n{regs()}\ncode {asm("mov eax, dword ptr [rbx + 0xffc]").hex()}\n{mmio_lines(bytes(16))}',
                  lambda out: refused('device')(out)))
    cases.append(('refuse-unassigned', f'rip {CODE:#x}\nrflags 0\n{regs()}\ncode {asm("mov eax, dword ptr [rbx + 8]").hex()}\n'
                  f'mmio {DEV:#x} {GPA:#x} unassigned\nmmio 0x210000 {GPA + 0x10000:#x}\n', unassigned))
    return cases


def run_cases(a, host, runtime, cases, report, key):
    failures, rows = [], []
    def one(job):
        (name, text, check), block = job
        rc, out, err = driver(host, runtime, a.fexc, text, a.out / key / f'{name}-{block}', [block] if block else [])
        return (name, block, [f'driver {rc}: {err}'] if out is None else check(out))
    jobs = [(c, b) for c in cases for b in (None, 1)]
    with cf.ThreadPoolExecutor(a.parallel) as pool:
        for name, block, problems in pool.map(one, jobs):
            rows.append({'name': name, 'block': block, 'problems': problems})
            if problems: failures.append(f'{name} block={block}: {"; ".join(problems)}')
    report[key] = {'cases': len(cases), 'runs': len(jobs), 'failures': failures}
    return failures


# ---- kicks (exactly once under random stops, 1-4 vCPUs) ----------------------------------------------------------------
def kick_program(n, iterations):
    """Each vCPU: rcx iterations of a counter read (r10 counts values not above the previous one) and a sequence write
    of its iteration number (r9)."""
    body = ref.assemble(f'''
        mov r11, -1
    1:  mov eax, dword ptr [rbx + {0xf00}]
        lea r12, [r11 + 1]
        cmp rax, r12
        jae 2f
        inc r10
    2:  mov r11, rax
        mov qword ptr [rbx + {0xf08}], r9
        inc r9
        dec rcx
        jnz 1b
        cmp r11, r11''')
    text = f'body {CODE:#x} {body.hex()}\n{mmio_lines()}'
    for v in range(n):
        text += f'cpu\nrip {CODE:#x}\nrflags 0\n{regs(rcx=iterations)}\n'
    return text


def kicks(a, host, runtime, report):
    failures, runs = [], []
    for n in a.cpus:
        for block in (None, 1):
            extra = [x for v in range(n) for x in ('--kick-vcpu', v)] + ['--kicks', a.kicks] + ([block] if block else [])
            rc, out, err = driver(host, runtime, a.fexc, kick_program(n, a.iterations), a.out / 'kicks' / f'{n}-{block}', extra)
            problems = []
            if out is None: problems.append(f'driver {rc}: {err}')
            else:
                dev = out['devices'][0]
                for v, c in enumerate(out['cpus']):
                    if c['stop'] != 'reached': problems.append(f"cpu{v} stop {c['stop']} {c['reason']}")
                    g = [int(x, 0) for x in c['gpr']]
                    if g[9] != a.iterations or g[10] != 0: problems.append(f'cpu{v} r9 {g[9]} r10 {g[10]}')
                    if dev['reads'][v] != a.iterations or dev['writes'][v] != a.iterations or c['mmio']['completed'] != 2 * a.iterations:
                        problems.append(f"cpu{v} device reads {dev['reads'][v]} writes {dev['writes'][v]} completed {c['mmio']['completed']} != {a.iterations}")
                    problems += [f'cpu{v} {p}' for p in kick_checks(c, a.kicks)]
                if dev['order_errors']: problems.append(f"{dev['order_errors']} order errors")
                stops = [c['kicks']['stops'] for c in out['cpus']]
                runs.append({'cpus': n, 'block': block, 'stops': stops})
            if problems: failures.append(f'kicks n={n} block={block}: {"; ".join(problems)[:800]}')
    report['kicks'] = {'runs': runs, 'iterations': a.iterations, 'kicks': a.kicks, 'failures': failures}
    return failures


def contain(a, host, runtime, report):
    code = ref.assemble('mov eax, dword ptr [rbx + 8]\nmov ecx, 1')
    rc, out, err = driver(host, runtime, a.fexc, f'rip {CODE:#x}\nrflags 0\n{regs(rax=0x1234)}\ncode {code.hex()}\n{mmio_lines(bytes(16))}', a.out / 'contain', ['--tamper', 'mmio'])
    problems = []
    if out is None: problems.append(f'driver {rc}: {err}')
    else:
        if (out['stop'], out['reason'].split()[0], out['tampered']) != ('resident', 'bad_reply', True): problems.append(f"stop {out['stop']} {out['reason']} tampered {out['tampered']}")
        if int(out['rip'], 0) != CODE or int(out['gpr'][0], 0) != 0x1234 or out['mmio']['completed'] != 0: problems.append(f"committed: rip {out['rip']} rax {out['gpr'][0]} {out['mmio']}")
    report['contain'] = {'failures': problems}
    return [f'contain: {p}' for p in problems]


def xmc(a, host, runtime, report):
    """Cross-modifying code at a device access (the independent review's repro): vCPU 0 loops `mov eax, [rbx + 0x10]` on the
    device page while vCPU 1 rewrites it (the same byte; load <-> store; another register; INT3 <-> the load, as Linux's
    text_poke_bp does), with no synchronisation. x86 runs the old or the new instruction: vCPU 0 must reach its end, or
    (INT3) take #BP (a trap: RIP after the INT3), never stop otherwise; every iteration makes exactly one device access (accesses =
    iterations done). Also INT3 over a plain RAM load (a step that meets a system instruction)."""
    asm, failures, runs = ref.assemble, [], []
    c0 = asm('1: mov eax, dword ptr [rbx + 0x10]\ndec rcx\njnz 1b')
    peer = {'same': 'mov byte ptr [0x100000], 0x8b', 'toggle': 'mov byte ptr [0x100000], 0x89\nmov byte ptr [0x100000], 0x8b',
            'reg': 'mov byte ptr [0x100001], 0x53\nmov byte ptr [0x100001], 0x43',
            'int3': 'mov byte ptr [0x100000], 0xcc\nmov byte ptr [0x100000], 0x8b'}
    for mode, ram in [(m, False) for m in peer] + [('int3', True)]:
        c1 = asm(f'1: {peer[mode]}\ndec rcx\njnz 1b')
        for block in (None, 1):
            text = f'body {CODE:#x} {c0.hex()}\nbody 0x101000 {c1.hex()}\nmemory 0x100800 00\n{mmio_lines(bytes(16))}'
            text += ('region 0x300000 0x1000\n' if ram else '') + f'cpu\nrip {CODE:#x}\nrflags 0\n{regs(rcx=a.xmc_iterations, rbx=0x300000 if ram else DEV)}\n'
            text += f'cpu\nrip 0x101000\nrflags 0\n{regs(rcx=a.xmc_peer)}\n'
            rc, out, err = driver(host, runtime, a.fexc, text, a.out / 'xmc' / f'{mode}-{ram}-{block}', [block] if block else [])
            problems = []
            if out is None: problems.append(f'driver {rc}: {err}')
            else:
                c, peer_out = out['cpus']
                left = int(c['gpr'][1], 0)
                bp = c['stop'] == 'guest_fault' and [int(x, 0) for x in c['fault']] == [3, 0, 0, CODE + 1]   # a trap: RIP after it
                if not (c['stop'] == 'reached' and left == 0) and not (mode == 'int3' and bp):
                    problems.append(f"vCPU 0 stop {c['stop']} {c['reason']} fault {c['fault']} rip {c['rip']} rcx {left}")
                if peer_out['stop'] != 'reached': problems.append(f"vCPU 1 stop {peer_out['stop']} {peer_out['reason']}")
                done = a.xmc_iterations - left
                if not ram:
                    d = out['devices'][0]
                    if d['reads'][0] + d['writes'][0] != done or c['mmio']['completed'] != done:
                        problems.append(f"device accesses {d['reads'][0]}+{d['writes'][0]}, completed {c['mmio']['completed']} != iterations {done}")
                runs.append({'mode': mode, 'ram': ram, 'block': block, 'stop': c['stop'], 'iterations': done, 'steps': c['stats']['steps'],
                             'invalidations': c['stats']['invalidations']})
            if problems: failures.append(f"xmc {mode} ram={ram} block={block}: {'; '.join(problems)}")
    report['xmc'] = {'runs': runs, 'failures': failures}
    return failures


def boundary(a, host, runtime, report):
    """An interrupt made eligible by an instruction a peer rewrote in (so vCPU 0 meets it in a single-instruction step): vCPU 0
    runs at CPL 0 with IF = 0 and vector 0x40 pending, looping `push 0x202; X; dec rcx; jnz` while vCPU 1 toggles X between
    POP RAX and POPF (which sets IF: no shadow), or `X; dec rcx; jnz` with X toggling NOP <-> STI (a shadow: one more
    instruction). The delivery must be at the boundary tools/models/delivery_model.py `recognise` first allows after X; a run in
    which X never changed reaches its end (counted)."""
    import delivery_model as dm
    asm, failures, runs = ref.assemble, [], []
    state = {'rflags': 0x2, 'shadow': None, 'halted': False, 'nmi_blocked': False}
    for name, body, at_x, toggle, after in (('popf', 'push 0x202\npop rax', 5, (0x58, 0x9d), ['other', 'other', 'other']),
                                            ('sti', 'nop', 0, (0x90, 0xfb), ['sti', 'other', 'other'])):
        loop = asm(f'1: {body}\ndec rcx\njnz 1b')
        code = b'\x90' + loop                         # the hook nop at CODE posts the vector
        x = CODE + 1 + at_x
        # The boundary: after X (as POPF or STI) and each following instruction until recognise must take 'intr'.
        lengths = [1, 3, 2]                           # X, dec rcx, jnz rel8
        s1, want = dm.after_instruction(state | {'rflags': 0x2}, after[0]), x + 1
        if name == 'popf': s1['rflags'] |= dm.IF
        for k in range(1, 3):
            if dm.recognise(s1, {'intr'}) == frozenset({'intr'}): break
            s1, want = dm.after_instruction(s1, after[k]), want + lengths[k]
        peer = asm(f'1: mov byte ptr [{x}], {toggle[1]}\nmov byte ptr [{x}], {toggle[0]}\ndec rcx\njnz 1b')
        for block in (None, 1):
            text = (f'body {CODE:#x} {code.hex()}\nbody 0x101000 {peer.hex()}\nmemory 0x100800 00\nregion 0x3e0000 0x10000\n'
                    f'cpu\nrip {CODE:#x}\nrflags 0\nif 0\ncpl 0\nsvr 0x1ff\npost {CODE:#x} 0x40\n{regs(rcx=a.xmc_iterations)}\n'
                    f'cpu\nrip 0x101000\nrflags 0\n{regs(rcx=a.xmc_peer)}\n')
            rc, out, err = driver(host, runtime, a.fexc, text, a.out / 'boundary' / f'{name}-{block}', [block] if block else [])
            problems = []
            if out is None: problems.append(f'driver {rc}: {err}')
            else:
                c = out['cpus'][0]
                if c['stop'] == 'guest_fault':
                    d = c.get('delivered', {})
                    got = (d.get('vector'), int(d.get('frame', ['0'])[0], 0))
                    if got != (64, want): problems.append(f'delivered {got}, want (64, {want:#x})')
                elif c['stop'] != 'reached': problems.append(f"vCPU 0 stop {c['stop']} {c['reason']}")
                runs.append({'mode': name, 'block': block, 'stop': c['stop'], 'steps': c['stats']['steps']})
            if problems: failures.append(f"boundary {name} block={block}: {'; '.join(problems)}")
    report['boundary'] = {'runs': runs, 'failures': failures}
    return failures


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--fexc', type=pathlib.Path, required=True)
    ap.add_argument('--out', type=pathlib.Path, required=True)
    ap.add_argument('--programs', type=int, default=60)
    ap.add_argument('--seed', type=int, default=20261029)
    ap.add_argument('--parallel', type=int, default=4)
    ap.add_argument('--kicks', type=int, default=1000)
    ap.add_argument('--iterations', type=int, default=20000)
    ap.add_argument('--cpus', type=int, nargs='+', default=[1, 2, 3, 4])
    ap.add_argument('--xmc-iterations', type=int, default=300_000, help='xmc: device-access loop rounds of vCPU 0')
    ap.add_argument('--xmc-peer', type=int, default=20_000_000, help='xmc: code rewrites of vCPU 1')
    ap.add_argument('--only', type=lambda s: s.split(','), default=['corpus', 'faults', 'refusals', 'kicks', 'contain', 'xmc', 'boundary'])
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    host, runtime = vm.build()
    report, failures = {}, []
    if 'corpus' in a.only: failures += corpus(a, host, runtime, report)
    if 'faults' in a.only: failures += run_cases(a, host, runtime, fault_cases(), report, 'faults')
    if 'refusals' in a.only: failures += run_cases(a, host, runtime, refusal_cases(), report, 'refusals')
    if 'kicks' in a.only: failures += kicks(a, host, runtime, report)
    if 'contain' in a.only: failures += contain(a, host, runtime, report)
    if 'xmc' in a.only: failures += xmc(a, host, runtime, report)
    if 'boundary' in a.only: failures += boundary(a, host, runtime, report)
    (a.out / 'm3.json').write_text(json.dumps(report, indent=1) + '\n')
    for f in failures: print('FAIL', f)
    print({k: {kk: (len(vv) if kk == 'failures' else vv) for kk, vv in v.items() if kk != 'runs'} for k, v in report.items()})
    sys.exit(1 if failures else 0)


if __name__ == '__main__': main()
