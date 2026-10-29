#!/usr/bin/env python3
"""General x86-64 reference runner: program description -> final state under pinned QEMU Linux-user.

    tools/with-env python3 tools/reference/ref.py PROGRAMS.json -o RESULTS.json [--workdir DIR]

PROGRAMS.json: {"programs": [program, ...]} where a program is
  {"name": label only, "code": hex, "rip": addr, "gpr": [16 values], "rflags": value,
   "memory": [{"addr": a, "data": hex}, ...], "insns": [annotation, ...] (required), "prefix": k (optional)}
With "prefix": k only bytes [0, k) run (k on an instruction boundary, 0 allowed): the result is the state at that
boundary, e.g. before an instruction that faults; "insns" annotates the whole program and is cut to what ran.
GPR order: rax rcx rdx rbx rsp rbp rsi rdi r8..r15.  All integers may be JSON numbers or
"0x.." strings; this tool writes hex strings.  Every memory region is captured at the end.
Execution is the code bytes followed by fallthrough into a capture sequence.  See README.md.
"""
import argparse, hashlib, json, os, pathlib, re, struct, subprocess, sys, tempfile, uuid
HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE.parent))
import flags

# The pinned QEMU container image (Debian bookworm, qemu-user-static 7.2) and the SHA-256 of its QEMU binaries and
# sources (the pins of the earlier I4-V QEMU check, run_i4v_qemu.py, deleted at S3i).
IMAGE = 'sha256:27475cb39ecb4500d02b7c6e73c9d598e8d9f0736f0dc217d2d985e5cbe01aac'
PINS = {
'qemu-arm64.deb':'e420c417c12b42ca774795b62888d5388da0e08c1ef263dea1490e5ce4e32a0b',
'qemu-x86_64-static':'16ce7dbb6626f3c0e1f932f831817dbf3b506c848a168d16f2172151e9666594',
'qemu.dsc':'6b6bd27b947ec89e40a1d6b26d9c9f3c698ca691da13163e0483f6061bbb20b3',
'debian.tar.xz':'28a1c86310ee53b6bf1854cffe6f6be6ed29a08db7d05759f8ac57b8d6a6cb2f',
'qemu-source.tar.xz':'91aca71520040edc40b8d437aa3004dae614f58e286cf653ee8996c07af2962f'}
REGS = ('rax', 'rcx', 'rdx', 'rbx', 'rsp', 'rbp', 'rsi', 'rdi', 'r8', 'r9', 'r10', 'r11', 'r12', 'r13', 'r14', 'r15')
R64 = list(REGS)
R32 = ['eax', 'ecx', 'edx', 'ebx', 'esp', 'ebp', 'esi', 'edi'] + [f'r{i}d' for i in range(8, 16)]
R16 = ['ax', 'cx', 'dx', 'bx', 'sp', 'bp', 'si', 'di'] + [f'r{i}w' for i in range(8, 16)]
R8 = ['al', 'cl', 'dl', 'bl', 'spl', 'bpl', 'sil', 'dil'] + [f'r{i}b' for i in range(8, 16)]
HI = ['ah', 'ch', 'dh', 'bh']
WIDTH = {n: w for w, names in ((64, R64), (32, R32), (16, R16), (8, R8 + HI)) for n in names}
FORMAT = 'natemu-reference-v2'
PAGE = 0x1000
HARNESS = 0x40000                     # setup code; +0x1000 input block; +0x2000 output block; +0x2800 region table; stack to +0x4000
IN, OUT, TABLE, STACK_TOP = HARNESS + 0x1000, HARNESS + 0x2000, HARNESS + 0x2800, HARNESS + 0x4000
ADDR_MIN, ADDR_MAX = 0x10000, 0x300000000   # program bytes must lie here (tested to 0x2_0000_1000; 0x7fff_0000_0000 failed)
MAX_REGIONS = 100
# Fault mode (signal reference): a SIGSEGV handler on an alternate stack, the saved pages of unmapped regions
ALT, ALT_BYTES, SAVE, SAVE_PAGES = HARNESS + 0x4000, 0x2000, HARNESS + 0x6000, 2
HANDLER, ACT, SS, RESTORER = HARNESS + 0x400, HARNESS + 0x300, HARNESS + 0x340, HARNESS + 0x3c0
# I7: the FS/GS bases of a program with `fsbase`/`gsbase` (input block +144/+152) are loaded by WRFSBASE/WRGSBASE in a
# prelude (the ELF entry then; it jumps to the setup), QEMU's CPU with +fsgsbase. Linux-user's arch_prctl refuses bases
# at or above its task size, so negative (upper-half) bases need the instructions.
SEGMENTS = HARNESS + 0xf00
FAULT_BYTES = 32                      # appended to the 144-byte output: error code, trap number, CR2, si_addr


def require(ok, why):
    if not ok: raise ValueError(why)

def num(v): return int(v, 0) if isinstance(v, str) else int(v)
def hx(v): return '0x%016x' % v


def assemble(text, labels=False):
    """Assemble Intel-syntax x86-64 with the host clang integrated assembler; return .text bytes
    (and, with labels=True, also {label: offset in .text})."""
    with tempfile.TemporaryDirectory() as d:
        src, obj = pathlib.Path(d, 'a.s'), pathlib.Path(d, 'a.o')
        src.write_text('.intel_syntax noprefix\n' + text + '\n')
        r = subprocess.run(['clang', '-c', '-target', 'x86_64-linux-gnu', str(src), '-o', str(obj)], capture_output=True, text=True)
        if r.returncode: raise ValueError('assembler: ' + r.stderr.strip())
        data = obj.read_bytes()
    shoff = struct.unpack_from('<Q', data, 0x28)[0]; shentsize, shnum, shstrndx = struct.unpack_from('<HHH', data, 0x3a)
    secs = [struct.unpack_from('<IIQQQQIIQQ', data, shoff + i * shentsize) for i in range(shnum)]
    names = secs[shstrndx]
    def name(sec): return data[names[4] + sec[0]:].split(b'\0', 1)[0].decode()
    require(not any(name(s).startswith('.rela') for s in secs), 'assembly needs relocations (symbols unsupported)')
    text_secs = [s for s in secs if name(s) == '.text']
    require(len(text_secs) == 1, 'expected one .text section')
    code = data[text_secs[0][4]:text_secs[0][4] + text_secs[0][5]]
    if not labels: return code
    symtab = next(sec for sec in secs if sec[1] == 2); strtab = secs[symtab[6]]; found = {}
    for off in range(symtab[4], symtab[4] + symtab[5], 24):
        n, _, _, _, value, _ = struct.unpack_from('<IBBHQQ', data, off)
        found[data[strtab[4] + n:].split(b'\0', 1)[0].decode()] = value
    return code, found


def _stubs():
    """(setup, capture) machine code, assembled once per process; both use only fixed harness addresses."""
    setup = [f'mov esp, {IN + 8}', 'popfq'] + [f'mov {r}, qword ptr [{IN + 16 + 8 * i}]' for i, r in enumerate(REGS) if r != 'rsp']
    setup += [f'mov rsp, qword ptr [{IN + 16 + 8 * 4}]', f'jmp qword ptr [{IN}]']
    cap = ['cap:'] + [f'mov qword ptr [{OUT + 8 * i}], {r}' for i, r in enumerate(REGS)]
    cap += [f'mov esp, {STACK_TOP}', 'pushfq', f'pop qword ptr [{OUT + 128}]', 'lea rax, [rip + cap]', f'mov qword ptr [{OUT + 136}], rax',
            f'mov eax, 1', 'mov edi, 1', f'mov esi, {OUT}', 'mov edx, 144', 'syscall',
            f'mov r12d, {TABLE + 8}', f'mov r13d, dword ptr [{TABLE}]', 'test r13d, r13d', 'jz done', 'again:',
            'mov rsi, qword ptr [r12]', 'mov rdx, qword ptr [r12 + 8]', 'mov eax, 1', 'mov edi, 1', 'syscall',
            'add r12d, 16', 'dec r13d', 'jnz again', 'done:', 'mov eax, 231', 'xor edi, edi', 'syscall']
    return assemble('\n'.join(setup)), assemble('\n'.join(cap))
_STUBS = None
_FAULT_STUBS = {}


def _fault_stubs(protect):
    """(setup, handler, restorer) machine code of fault mode for the page protections `protect` [(page, 'unmap'|'readonly')]:
    the setup installs the handler (rt_sigaction on SIGSEGV, sigaltstack) and applies the protections before the program
    starts; the handler stores the faulting context (ucontext: GPRs, RIP, EFLAGS, err, trapno, CR2; siginfo: si_addr),
    undoes the protections (unmapped pages come back with their saved bytes, so the regions can be written out) and
    writes the state and the regions out like the capture sequence."""
    key = tuple(protect)
    if key in _FAULT_STUBS: return _FAULT_STUBS[key]
    setup = [f'mov eax, 131', f'mov edi, {SS}', 'xor esi, esi', 'syscall',                        # sigaltstack(&ss, 0)
             f'mov eax, 13', 'mov edi, 11', f'mov esi, {ACT}', 'xor edx, edx', 'mov r10d, 8', 'syscall']   # rt_sigaction(SIGSEGV, &act, 0, 8)
    restore = []
    for i, (page, mode) in enumerate(protect):
        if mode == 'unmap':
            setup += [f'mov esi, {page}', f'mov edi, {SAVE + PAGE * i}', 'mov ecx, 4096', 'cld', 'rep movsb',
                      'mov eax, 11', f'mov edi, {page}', 'mov esi, 4096', 'syscall']             # copy the page away, munmap it
            restore += ['mov eax, 9', f'mov edi, {page}', 'mov esi, 4096', 'mov edx, 3', 'mov r10d, 0x32', 'mov r8, -1', 'xor r9d, r9d', 'syscall',
                        f'mov esi, {SAVE + PAGE * i}', f'mov edi, {page}', 'mov ecx, 4096', 'cld', 'rep movsb']   # mmap it back, refill it
        else:
            setup += ['mov eax, 10', f'mov edi, {page}', 'mov esi, 4096', 'mov edx, 1', 'syscall']        # mprotect(PROT_READ)
            restore += ['mov eax, 10', f'mov edi, {page}', 'mov esi, 4096', 'mov edx, 3', 'syscall']
    # ucontext.uc_mcontext (offset 0x28): r8..r15, rdi, rsi, rbp, rbx, rdx, rax, rcx, rsp, rip, eflags, cs/gs/fs, err, trapno, oldmask, cr2
    at = {'r8': 0, 'r9': 8, 'r10': 16, 'r11': 24, 'r12': 32, 'r13': 40, 'r14': 48, 'r15': 56, 'rdi': 64, 'rsi': 72, 'rbp': 80, 'rbx': 88,
          'rdx': 96, 'rax': 104, 'rcx': 112, 'rsp': 120}
    handler = ['mov r9, rsi', 'mov r8, rdx']                                                         # siginfo, ucontext
    for i, r in enumerate(REGS): handler += [f'mov rax, qword ptr [r8 + {0x28 + at[r]}]', f'mov qword ptr [{OUT + 8 * i}], rax']
    for k, off in enumerate((136, 128, 152, 160, 176)):      # eflags, rip, err, trapno, cr2
        handler += [f'mov rax, qword ptr [r8 + {0x28 + off}]', f'mov qword ptr [{OUT + 128 + 8 * k}], rax']
    handler += ['mov rax, qword ptr [r9 + 16]', f'mov qword ptr [{OUT + 168}], rax']                 # si_addr
    handler += restore
    handler += ['mov eax, 1', 'mov edi, 1', f'mov esi, {OUT}', f'mov edx, {144 + FAULT_BYTES}', 'syscall',
                f'mov r12d, {TABLE + 8}', f'mov r13d, dword ptr [{TABLE}]', 'test r13d, r13d', 'jz done', 'again:',
                'mov rsi, qword ptr [r12]', 'mov rdx, qword ptr [r12 + 8]', 'mov eax, 1', 'mov edi, 1', 'syscall',
                'add r12d, 16', 'dec r13d', 'jnz again', 'done:', 'mov eax, 231', 'xor edi, edi', 'syscall']
    if _STUBS is None: globals()['_STUBS'] = _stubs()
    base_setup = [f'mov esp, {IN + 8}', 'popfq'] + [f'mov {r}, qword ptr [{IN + 16 + 8 * i}]' for i, r in enumerate(REGS) if r != 'rsp']
    base_setup += [f'mov rsp, qword ptr [{IN + 16 + 8 * 4}]', f'jmp qword ptr [{IN}]']
    code = assemble('\n'.join(setup + base_setup))
    require(len(code) < ACT - HARNESS, 'fault-mode setup code too long')
    cap = ['cap:'] + [f'mov qword ptr [{OUT + 8 * i}], {r}' for i, r in enumerate(REGS)]
    cap += [f'mov esp, {STACK_TOP}', 'pushfq', f'pop qword ptr [{OUT + 128}]', 'lea rax, [rip + cap]', f'mov qword ptr [{OUT + 136}], rax'] + restore
    cap += ['mov eax, 1', 'mov edi, 1', f'mov esi, {OUT}', 'mov edx, 144', 'syscall',      # then written out as the plain capture
            f'mov r12d, {TABLE + 8}', f'mov r13d, dword ptr [{TABLE}]', 'test r13d, r13d', 'jz done', 'again:',
            'mov rsi, qword ptr [r12]', 'mov rdx, qword ptr [r12 + 8]', 'mov eax, 1', 'mov edi, 1', 'syscall',
            'add r12d, 16', 'dec r13d', 'jnz again', 'done:', 'mov eax, 231', 'xor edi, edi', 'syscall']
    out = (code, assemble('\n'.join(handler)), assemble('mov eax, 15\nsyscall'), assemble('\n'.join(cap)))
    _FAULT_STUBS[key] = out
    return out



def norm(p):
    """Validate a program description; return it with integers/bytes decoded."""
    code = bytes.fromhex(p['code']); rip = num(p['rip']); gpr = [num(x) & (2**64 - 1) for x in p['gpr']]
    rflags = num(p['rflags']); regions = [(num(m['addr']), bytes.fromhex(m['data'])) for m in p.get('memory', [])]
    prefix = p.get('prefix')
    if prefix is not None:
        require(0 <= prefix <= len(code), f'prefix {prefix} outside the code'); code = code[:prefix]
    require((code or prefix is not None) and len(gpr) == 16, 'code and 16 GPRs required')
    require(p.get('insns'), 'insns annotations are required (flag masks and the disassembly cross-check depend on them)')
    require(rflags & ~(flags.ARITH | 2) == 0, 'initial RFLAGS may only set the arithmetic flags')
    require(len(regions) <= MAX_REGIONS and all(d for _, d in regions), 'regions: 1..%d non-empty' % MAX_REGIONS)
    flow, offsets = bool(p.get('flow')), p.get('offsets')
    if flow:
        require(prefix is None, 'a flow program cannot also be a prefix program')
        require(offsets and len(offsets) == len(p['insns']) and offsets == sorted(set(offsets)) and offsets[-1] < len(code),
                'flow: "offsets" must list the start offset of each annotated instruction (increasing, inside the code)')
    faults = p.get('faults')
    if faults is not None:
        protect = [(num(x['addr']), x['mode']) for x in faults.get('protect', [])]
        require(flow, 'fault mode needs a flow program (the executed path ends where the fault is)')
        require(len(protect) <= SAVE_PAGES and len({a for a, _ in protect}) == len(protect), f'at most {SAVE_PAGES} distinct protected pages')
        for page, mode in protect:
            require(mode in ('unmap', 'readonly') and page % PAGE == 0 and any(a <= page and page + PAGE <= a + len(d) for a, d in regions),
                    f'protection {mode} {page:#x}: a whole page of a memory region')
        faults = protect
    cpu = p.get('cpu', 'qemu64')
    bases = [num(p.get('fsbase', 0)) & (2**64 - 1), num(p.get('gsbase', 0)) & (2**64 - 1)]
    require(all(b >> 47 in (0, 0x1ffff) for b in bases), 'fsbase/gsbase must be canonical (WRFSBASE/WRGSBASE #GP otherwise)')
    if any(bases) and '+fsgsbase' not in cpu: cpu += ',+fsgsbase'
    require(re.fullmatch(r'qemu64(,\+[a-z0-9_]+)*', cpu) is not None, f'cpu {cpu!r}: qemu64 plus +feature flags only')
    return dict(name=p.get('name', ''), code=code, rip=rip, gpr=gpr, rflags=rflags, regions=regions, insns=p.get('insns'), prefix=prefix,
                flow=flow, offsets=offsets, faults=faults, cpu=cpu, bases=bases)


def build_elf(p):
    """ELF64 with the harness page set, the code + capture stub at rip, and one mapping per page run. Entry = setup stub."""
    global _STUBS
    if _STUBS is None: _STUBS = _stubs()
    setup, cap = _STUBS
    if p['faults'] is not None: setup, handler, restorer, cap = _fault_stubs(p['faults'])
    pages, claimed = {}, {}
    def put(addr, data, what):
        require(ADDR_MIN <= addr and addr + len(data) <= ADDR_MAX, f'{what} at {addr:#x} outside [{ADDR_MIN:#x},{ADDR_MAX:#x})')
        for i, b in enumerate(data):
            a = addr + i
            require(a not in claimed, f'{what} overlaps {claimed.get(a)} at {a:#x}')
            claimed[a] = what; pages.setdefault(a // PAGE, bytearray(PAGE))[a % PAGE] = b
    for i in range(HARNESS // PAGE, STACK_TOP // PAGE): pages[i] = bytearray(PAGE)
    for i in range(HARNESS, STACK_TOP): claimed[i] = 'harness'
    if p['faults'] is not None:
        for i in range(ALT // PAGE, (SAVE + SAVE_PAGES * PAGE) // PAGE): pages[i] = bytearray(PAGE)
        for i in range(ALT, SAVE + SAVE_PAGES * PAGE): claimed[i] = 'harness'
    def poke(addr, data):
        for i, b in enumerate(data): pages[(addr + i) // PAGE][(addr + i) % PAGE] = b
    poke(HARNESS, setup)
    if p['faults'] is not None:
        poke(HANDLER, handler); poke(RESTORER, restorer)
        poke(ACT, struct.pack('<4Q', HANDLER, 4 | 0x04000000 | 0x08000000 | 0x40000000, RESTORER, 0))     # SA_SIGINFO|RESTORER|ONSTACK|NODEFER
        poke(SS, struct.pack('<QiiQ', ALT, 0, 0, ALT_BYTES))
    poke(IN, struct.pack('<2Q', p['rip'], p['rflags'] | 0x202) + b''.join(struct.pack('<Q', g) for g in p['gpr']) + struct.pack('<2Q', *p['bases']))
    entry = HARNESS
    if any(p['bases']):
        prelude = assemble(f'mov rax, qword ptr [{IN + 144}]\nwrfsbase rax\nmov rax, qword ptr [{IN + 152}]\nwrgsbase rax\nmov eax, {HARNESS}\njmp rax')
        require(p['faults'] is None or HANDLER + len(handler) <= SEGMENTS, 'fault-mode handler overlaps the segment prelude')
        poke(SEGMENTS, prelude); entry = SEGMENTS
    poke(TABLE, struct.pack('<Q', len(p['regions'])) + b''.join(struct.pack('<2Q', a, len(d)) for a, d in p['regions']))
    for a, d in p['regions']: put(a, d, f'memory {a:#x}')
    put(p['rip'], p['code'] + cap, 'code')
    runs, order = [], sorted(pages)
    for n in order:
        if runs and runs[-1][-1] == n - 1: runs[-1].append(n)
        else: runs.append([n])
    hdr = 64 + 56 * len(runs); offset = (hdr + PAGE - 1) // PAGE * PAGE; body, phs = b'', []
    for run in runs:
        data = b''.join(bytes(pages[n]) for n in run)
        phs.append(struct.pack('<IIQQQQQQ', 1, 7, offset + len(body), run[0] * PAGE, run[0] * PAGE, len(data), len(data), PAGE)); body += data
    header = struct.pack('<16sHHIQQQIHHHHHH', b'\x7fELF\x02\x01\x01' + bytes(9), 2, 62, 1, entry, 64, 0, 0, 64, 56, len(runs), 0, 0, 0)
    return (header + b''.join(phs)).ljust(offset, b'\0') + body, len(cap)


def parse_output(p, out):
    sizes = [len(d) for _, d in p['regions']]
    extra = len(out) - 144 - sum(sizes)
    require(extra == 0 or (p['faults'] is not None and extra == FAULT_BYTES), f'QEMU output length {len(out)}, expected {144 + sum(sizes)}')
    v = struct.unpack('<18Q', out[:144]); at = 144 + extra; mem = []
    for (a, d) in p['regions']: mem.append({'addr': hx(a), 'data': out[at:at + len(d)].hex()}); at += len(d)
    require(v[16] & ~(flags.ARITH | flags.DF) == flags.FIXED, f'RFLAGS {v[16]:#x}: unexpected non-arithmetic bits')
    final = {'gpr': [hx(x) for x in v[:16]], 'rflags': hx(v[16]), 'rip': hx(v[17]), 'memory': mem}
    if extra:      # the signal handler ran: the context at the fault
        err, trapno, cr2, si_addr = struct.unpack('<4Q', out[144:144 + FAULT_BYTES])
        return final, {'err': err, 'trapno': hx(trapno), 'cr2': hx(cr2), 'si_addr': hx(si_addr)}
    require(v[17] == p['rip'] + len(p['code']), f'final RIP {v[17]:#x}: capture sequence not reached by fallthrough')
    return final, None


LINE = re.compile(r'^0x([0-9a-f]+):\s+((?:[0-9a-f]{2}(?=\s|$)\s?)+)\s*(\S.*)?$')
def disasm(log, rip, length):
    """Instructions QEMU translated inside [rip, rip+length): [(addr, bytes, text)]."""
    insns, raw = [], bytearray()
    for line in log.splitlines():
        m = LINE.match(line)
        if not m or not (rip <= int(m[1], 16) < rip + length): continue
        raw += bytes.fromhex(m[2].replace(' ', ''))
        if m[3]: insns.append((int(m[1], 16), m[3].split()[0], m[3]))
    return insns, bytes(raw)

IMPLICIT = {'cbw': ('cbtw', 16), 'cwde': ('cwtl', 32), 'cdqe': ('cltq', 64), 'cwd': ('cwtd', 16), 'cdq': ('cltd', 32), 'cqo': ('cqto', 64)}
SUFFIX = {'b': 8, 'w': 16, 'l': 32, 'q': 64}

BRANCH_OPS = {'jcc', 'jmp', 'call', 'ret', 'loop', 'loope', 'loopne', 'jrcxz'}      # control flow: no operand width to check
# No explicit (or only a fixed) operand: no width to check (I1).
NO_WIDTH_OPS = {'clc', 'stc', 'cmc', 'cld', 'std', 'lahf', 'sahf', 'pushf', 'leave', 'enter', 'prefetcht0', 'prefetcht1',
                'prefetcht2', 'prefetchnta'}

def mnemonic_ok(insn, m):
    op = insn['op']
    if op == 'jcc': return m == 'j' + insn['cc']
    if op in BRANCH_OPS: return m in (op, op + 'q')            # QEMU prints jmpq/callq/retq; loop, loope, loopne, jrcxz as is
    if op in IMPLICIT: return m == IMPLICIT[op][0]
    if op == 'setcc': return m == 'set' + insn['cc']
    if op == 'cmovcc': return m in ['cmov' + insn['cc']] + ['cmov' + insn['cc'] + s for s in 'wlq']
    if op == 'movzx': return re.fullmatch(r'movz[bw][wlq]', m) is not None
    if op == 'movsx': return re.fullmatch(r'movs[bwl][wlq]', m) is not None
    if op == 'mov': return m in ('mov', 'movabs') or re.fullmatch(r'mov(abs)?[bwlq]', m) is not None
    return m == op or (m[:-1] == op and m[-1] in 'bwlq')

def att_operands(text):
    """AT&T operand strings of one disassembled instruction (commas inside parentheses stay together)."""
    rest = text.split(None, 1)[1] if len(text.split(None, 1)) > 1 else ''
    ops, depth, cur = [], 0, ''
    for c in rest:
        depth += (c == '(') - (c == ')')
        if c == ',' and depth == 0: ops.append(cur.strip()); cur = ''
        else: cur += c
    return [o for o in ops + [cur.strip()] if o]

def att_width(ann, m, ops):
    """Operand width QEMU's disassembly implies for the annotation's operand: destination register, else size suffix."""
    if ann['op'] in IMPLICIT: return IMPLICIT[ann['op']][1]
    if ann['op'] == 'setcc': return 8
    if ops and ops[-1][1:] in WIDTH: return WIDTH[ops[-1][1:]]      # a register (`%fs:...` is memory, I7)
    if m[-1] in SUFFIX: return SUFFIX[m[-1]]
    regs = [o for o in ops if o[1:] in WIDTH]                  # memory destination: the last register operand (I1:
    return WIDTH.get(regs[-1][1:]) if regs and ann['op'] in REG_WIDTH_OPS else None   # shld/shrd, cmpxchg, xadd, movnti)
REG_WIDTH_OPS = {'shld', 'shrd', 'cmpxchg', 'xadd', 'movnti'}

def check_one(ann, a, m, t, prev):
    """One annotation against QEMU's disassembly (`m` mnemonic, `t` text) of the instruction at `a`; `prev` is the text of the
    instruction executed before it."""
    require(mnemonic_ok(ann, m), f'annotation {ann} does not match disassembly "{t}" at {a:#x}')
    ops = att_operands(t)
    w = att_width(ann, m, ops)
    require(ann['op'] in BRANCH_OPS | NO_WIDTH_OPS or w == ann.get('width', 64),
            f'annotated width {ann.get("width")} but disassembly "{t}" implies {w} at {a:#x}')
    if ann['op'] in flags.SHIFTS | flags.ROTATES:
        if ops[0] == '%cl':          # count came from CL: the previous instruction must be mov $N, %cl
            pm = re.fullmatch(r'movb\s+\$(\S+), %cl', prev)
            require(pm and int(pm[1], 0) & 0xff == ann['count'] & 0xff, f'CL count {ann["count"]} not set by preceding "mov $N, %cl" at {a:#x}')
        else:
            require(ops[0].startswith('$') and int(ops[0][1:], 0) & 0xff == ann['count'] & 0xff, f'shift count {ann["count"]} vs "{t}"')

def check_annotations(p, log):
    """Cross-check the annotations (mnemonic, operand width, shift count) against QEMU's disassembly of the executed bytes."""
    insns, raw = disasm(log, p['rip'], len(p['code']))
    require(raw == p['code'], 'QEMU did not translate exactly the supplied code bytes')
    if p['prefix'] is not None: p['insns'] = p['insns'][:len(insns)]     # the annotations of what ran
    require(len(insns) == len(p['insns']), f'{len(insns)} instructions executed, {len(p["insns"])} annotated')
    prev = ''
    for (a, m, t), ann in zip(insns, p['insns']):
        check_one(ann, a, m, t, prev)
        prev = t
    return [t for _, _, t in insns]


TRACE = re.compile(r'^Trace \d+: 0x[0-9a-f]+ \[[0-9a-f]+/([0-9a-f]+)/')
def executed_path(p, log, stop=None):
    """The dynamic instruction sequence of a flow program from QEMU's `-d in_asm,exec,nochain` log: the translation blocks it
    entered, in order, from the program's first instruction until it reaches the capture sequence at the end of the code.
    Returns [(address, mnemonic, text)]. Every translated instruction inside the code must be the supplied bytes. With `stop`
    (the RIP of a fault) the last block ends before the instruction at that address, which did not complete."""
    rip, end = p['rip'], p['rip'] + len(p['code'])
    blocks, cur, trace, fresh = {}, None, [], False
    for line in log.splitlines():
        if line.startswith('IN:'): cur = None; fresh = True; continue
        m = TRACE.match(line)
        if m: trace.append(int(m[1], 16)); continue
        m = LINE.match(line)
        if not m: continue
        addr, raw = int(m[1], 16), bytes.fromhex(m[2].replace(' ', ''))
        if fresh: cur = blocks[addr] = []; fresh = False       # the block's start; a retranslation replaces it
        if cur is None: continue
        if m[3]: cur.append([addr, m[3].split()[0], m[3], raw])
        elif cur: cur[-1][3] += raw
    for tb in blocks.values():
        for addr, _, t, raw in tb:
            if rip <= addr < end: require(p['code'][addr - rip:addr - rip + len(raw)] == raw, f'QEMU translated other bytes than supplied at {addr:#x}')
    if _STUBS is None: globals()['_STUBS'] = _stubs()
    cap = _fault_stubs(p['faults'])[3] if p['faults'] is not None else _STUBS[1]
    path, started = [], False
    for pc in trace:
        if rip <= pc < end: started = True
        elif started:      # left the code: only the capture sequence (or, in fault mode, the signal handler) may follow
            require(end <= pc < end + len(cap) or (p['faults'] is not None and pc == HANDLER),
                    f'the program left its code for {pc:#x} (jumps outside the code are not supported)')
            break
        else: continue
        require(pc in blocks, f'executed block {pc:#x} was not translated in the log')
        path += [(a, m, t) for a, m, t, _ in blocks[pc]]
    require(started, 'the trace never entered the code')      # reaching the end is checked by parse_output (final RIP)
    path = [x for x in path if rip <= x[0] < end]
    if stop is not None:
        hits = [i for i, x in enumerate(path) if x[0] == stop]
        require(hits and hits[-1] == len(path) - 1, f'the faulting instruction at {stop:#x} is not the last executed one')
        path = path[:hits[-1]]
    return path

def check_flow(p, log, stop=None):
    """Annotations along the executed path: returns (distinct executed disassembly, executed annotations)."""
    path = executed_path(p, log, stop)
    index = {off: i for i, off in enumerate(p['offsets'])}
    prev, anns, ixs = '', [], []
    for a, m, t in path:
        require(a - p['rip'] in index, f'executed instruction {a:#x} "{t}" is not an annotated instruction start')
        ann = p['insns'][index[a - p['rip']]]
        check_one(ann, a, m, t, prev)
        anns.append(ann); ixs.append(index[a - p['rip']]); prev = t
    return sorted({t for _, _, t in path}), anns, ixs


def run_programs(programs, workdir, qemu):
    """Run all programs in one pinned-image container; return (results, provenance)."""
    workdir = pathlib.Path(workdir).resolve(); qemu = pathlib.Path(qemu).resolve()
    digest = hashlib.sha256(qemu.read_bytes()).hexdigest()
    require(digest == PINS['qemu-x86_64-static'], 'QEMU binary does not match the pinned hash')
    progs = [norm(p) for p in programs]
    (workdir / 'jobs').mkdir(parents=True, exist_ok=True)
    (workdir / 'container_entry.py').write_bytes((HERE / 'container_entry.py').read_bytes())
    for i, p in enumerate(progs):
        d = workdir / 'jobs' / f'p{i:04d}'; d.mkdir(exist_ok=True); (d / 'prog.elf').write_bytes(build_elf(p)[0]); (d / 'prog.elf').chmod(0o755)
        for marker, on in (('flow', p['flow']), ('faults', p['faults'] is not None)):
            if on: (d / marker).write_text('1')
            elif (d / marker).exists(): (d / marker).unlink()
        if p['cpu'] != 'qemu64': (d / 'cpu').write_text(p['cpu'])
        elif (d / 'cpu').exists(): (d / 'cpu').unlink()
    token = uuid.uuid4().hex; name = 'natemu-ref-' + token; cidfile = workdir / 'container-id'
    if cidfile.exists(): cidfile.unlink()
    args = ['docker', 'create', '--name', name, '--label', 'natemu.qemu.token=' + token, '--cidfile', str(cidfile), '--network', 'none', '--read-only',
            '--cpus', '2', '--memory', '512m', '--pids-limit', '128', '--tmpfs', '/tmp:rw,size=32m', '--tmpfs', '/var/lib/postgresql/data:rw,size=1m',
            '--mount', f'type=bind,source={workdir},target=/work', '--mount', f'type=bind,source={qemu},target=/qemu,readonly',
            '--workdir', '/work', '--entrypoint', 'python3', IMAGE, '/work/container_entry.py']
    try:
        require(json.loads(subprocess.run(['docker', 'image', 'inspect', IMAGE], capture_output=True, check=True, timeout=20).stdout)[0]['Id'] == IMAGE, 'pinned image')
        subprocess.run(args, capture_output=True, check=True, timeout=20)
        r = subprocess.run(['docker', 'start', '--attach', name], capture_output=True, timeout=1200)
        (workdir / 'container.stderr').write_bytes(r.stderr)
        require(r.returncode == 0, 'container failed: ' + r.stderr.decode()[-500:])
    finally:
        subprocess.run(['docker', 'rm', '--force', '--volumes', name], capture_output=True, timeout=30)
    results = []
    for i, (src, p) in enumerate(zip(programs, progs)):
        d = workdir / 'jobs' / f'p{i:04d}'
        require((d / 'rc').read_text().strip() == '0', f'QEMU exit status {(d / "rc").read_text().strip()} for program {i}')
        final, fault = parse_output(p, (d / 'qemu.stdout').read_bytes())
        if p['flow']:      # the flags rule runs along the executed path (the same instructions as QEMU executed, in order)
            text, executed, path_ix = check_flow(p, (d / 'qemu.log').read_text(), num(final['rip']) if fault else None)
            und = flags.analyze(executed)
        else:
            text = check_annotations(p, (d / 'qemu.log').read_text())
            und = flags.analyze(p['insns'])
        results.append({'name': p['name'], 'qemu': final, 'disasm': text, 'undefined_flags': hx(und), 'rflags_compare_mask': hx(flags.TRACKED & ~und)}
                       | ({'prefix': p['prefix']} if p['prefix'] is not None else {})
                       | ({'cpu': p['cpu']} if p['cpu'] != 'qemu64' else {})
                       | ({'executed_insns': len(executed)} if p['flow'] else {})
                       | ({'fault': fault, 'path_ix': path_ix} if fault else {}))
    prov = {'format': FORMAT, 'image': IMAGE, 'qemu_sha256': digest, 'qemu_package': 'qemu-user-static 1:7.2+dfsg-7+deb12u18+b3 arm64',
            'qemu_argv': ['/qemu', '-cpu', 'qemu64', '-d', 'in_asm', '-D', 'qemu.log', 'prog.elf'], 'rflags_fixed_bits': hx(flags.FIXED)}
    return results, prov


def compare(result, gpr, rflags, rip, memory):
    """Differences between a QEMU `result` and another implementation's final state ([] = equal).
    gpr: 16 ints; rflags, rip: ints; memory: list of {'addr','data'} with exactly the program's regions, in order.
    RFLAGS is compared only under the rule-derived mask (undefined bits excluded); IF/bit1 are ignored."""
    diffs, q = [], result['qemu']
    for i, r in enumerate(REGS):
        if num(q['gpr'][i]) != gpr[i]: diffs.append(f'{r}: qemu {q["gpr"][i]} vs {gpr[i]:#x}')
    mask = num(result['rflags_compare_mask'])
    if (num(q['rflags']) ^ rflags) & mask: diffs.append(f'rflags: qemu {q["rflags"]} vs {rflags:#x} (mask {mask:#x})')
    if num(q['rip']) != rip: diffs.append(f'rip: qemu {q["rip"]} vs {rip:#x}')
    if [num(m['addr']) for m in q['memory']] != [num(m['addr']) for m in memory]:
        diffs.append(f'memory regions differ: qemu {[m["addr"] for m in q["memory"]]} vs {[m["addr"] for m in memory]}')
    else:
        for a, b in zip(q['memory'], memory):
            if a['data'] != b['data']: diffs.append(f'memory at {a["addr"]} differs')
    return diffs


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('programs', type=pathlib.Path); ap.add_argument('-o', '--output', type=pathlib.Path, required=True)
    ap.add_argument('--workdir', type=pathlib.Path); ap.add_argument('--qemu', type=pathlib.Path)
    a = ap.parse_args()
    raw = pathlib.Path(os.environ.get('NATEMU_RAW_ROOT', HERE.parent.parent.parent / 'natemu-raw'))
    qemu = a.qemu or raw / 'reference-slice1-20261026-01' / 'pin' / 'qemu-x86_64-static'
    work = a.workdir or raw / 'reference-slice1-20261026-01' / 'run'
    work.mkdir(parents=True, exist_ok=True)
    programs = json.loads(a.programs.read_text())['programs']
    results, prov = run_programs(programs, work, qemu)
    a.output.write_text(json.dumps({**prov, 'results': results}, indent=1) + '\n')
    print(f'{len(results)} programs run; wrote {a.output}')

if __name__ == '__main__': main()
