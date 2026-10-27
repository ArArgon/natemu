#!/usr/bin/env python3
"""General x86-64 reference runner: program description -> final state under pinned QEMU Linux-user.

    tools/with-env python3 tools/reference/ref.py PROGRAMS.json -o RESULTS.json [--workdir DIR]

PROGRAMS.json: {"programs": [program, ...]} where a program is
  {"name": label only, "code": hex, "rip": addr, "gpr": [16 values], "rflags": value,
   "memory": [{"addr": a, "data": hex}, ...], "insns": [annotation, ...] (required)}
GPR order: rax rcx rdx rbx rsp rbp rsi rdi r8..r15.  All integers may be JSON numbers or
"0x.." strings; this tool writes hex strings.  Every memory region is captured at the end.
Execution is the code bytes followed by fallthrough into a capture sequence.  See README.md.
"""
import argparse, hashlib, json, os, pathlib, re, struct, subprocess, sys, tempfile, uuid
HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE.parent))
import flags
import run_i4v_qemu as old          # reuse the pinned image id and QEMU hashes

IMAGE, PINS = old.IMAGE, old.PINS
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


def norm(p):
    """Validate a program description; return it with integers/bytes decoded."""
    code = bytes.fromhex(p['code']); rip = num(p['rip']); gpr = [num(x) & (2**64 - 1) for x in p['gpr']]
    rflags = num(p['rflags']); regions = [(num(m['addr']), bytes.fromhex(m['data'])) for m in p.get('memory', [])]
    require(code and len(gpr) == 16, 'code and 16 GPRs required')
    require(p.get('insns'), 'insns annotations are required (flag masks and the disassembly cross-check depend on them)')
    require(rflags & ~(flags.ARITH | 2) == 0, 'initial RFLAGS may only set the arithmetic flags')
    require(len(regions) <= MAX_REGIONS and all(d for _, d in regions), 'regions: 1..%d non-empty' % MAX_REGIONS)
    return dict(name=p.get('name', ''), code=code, rip=rip, gpr=gpr, rflags=rflags, regions=regions, insns=p.get('insns'))


def build_elf(p):
    """ELF64 with the harness page set, the code + capture stub at rip, and one mapping per page run. Entry = setup stub."""
    global _STUBS
    if _STUBS is None: _STUBS = _stubs()
    setup, cap = _STUBS
    pages, claimed = {}, {}
    def put(addr, data, what):
        require(ADDR_MIN <= addr and addr + len(data) <= ADDR_MAX, f'{what} at {addr:#x} outside [{ADDR_MIN:#x},{ADDR_MAX:#x})')
        for i, b in enumerate(data):
            a = addr + i
            require(a not in claimed, f'{what} overlaps {claimed.get(a)} at {a:#x}')
            claimed[a] = what; pages.setdefault(a // PAGE, bytearray(PAGE))[a % PAGE] = b
    for i in range(HARNESS // PAGE, STACK_TOP // PAGE): pages[i] = bytearray(PAGE)
    for i in range(HARNESS, STACK_TOP): claimed[i] = 'harness'
    def poke(addr, data):
        for i, b in enumerate(data): pages[(addr + i) // PAGE][(addr + i) % PAGE] = b
    poke(HARNESS, setup)
    poke(IN, struct.pack('<2Q', p['rip'], p['rflags'] | 0x202) + b''.join(struct.pack('<Q', g) for g in p['gpr']))
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
    header = struct.pack('<16sHHIQQQIHHHHHH', b'\x7fELF\x02\x01\x01' + bytes(9), 2, 62, 1, HARNESS, 64, 0, 0, 64, 56, len(runs), 0, 0, 0)
    return (header + b''.join(phs)).ljust(offset, b'\0') + body, len(cap)


def parse_output(p, out):
    sizes = [len(d) for _, d in p['regions']]
    require(len(out) == 144 + sum(sizes), f'QEMU output length {len(out)}, expected {144 + sum(sizes)}')
    v = struct.unpack('<18Q', out[:144]); at = 144; mem = []
    for (a, d) in p['regions']: mem.append({'addr': hx(a), 'data': out[at:at + len(d)].hex()}); at += len(d)
    require(v[17] == p['rip'] + len(p['code']), f'final RIP {v[17]:#x}: capture sequence not reached by fallthrough')
    require(v[16] & ~flags.ARITH == flags.FIXED, f'RFLAGS {v[16]:#x}: unexpected non-arithmetic bits')
    return {'gpr': [hx(x) for x in v[:16]], 'rflags': hx(v[16]), 'rip': hx(v[17]), 'memory': mem}


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

def mnemonic_ok(insn, m):
    op = insn['op']
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
    if ops and ops[-1].startswith('%'): return WIDTH[ops[-1][1:]]
    return SUFFIX.get(m[-1]) if m[-1] in SUFFIX else None

def check_annotations(p, log):
    """Cross-check the annotations (mnemonic, operand width, shift count) against QEMU's disassembly of the executed bytes."""
    insns, raw = disasm(log, p['rip'], len(p['code']))
    require(raw == p['code'], 'QEMU did not translate exactly the supplied code bytes')
    require(len(insns) == len(p['insns']), f'{len(insns)} instructions executed, {len(p["insns"])} annotated')
    prev = ''
    for (a, m, t), ann in zip(insns, p['insns']):
        require(mnemonic_ok(ann, m), f'annotation {ann} does not match disassembly "{t}" at {a:#x}')
        ops = att_operands(t)
        w = att_width(ann, m, ops)
        require(w == ann.get('width', 64),
                f'annotated width {ann.get("width")} but disassembly "{t}" implies {w} at {a:#x}')
        if ann['op'] in flags.SHIFTS | flags.ROTATES:
            if ops[0] == '%cl':          # count came from CL: the previous instruction must be mov $N, %cl
                pm = re.fullmatch(r'movb\s+\$(\S+), %cl', prev)
                require(pm and int(pm[1], 0) & 0xff == ann['count'] & 0xff, f'CL count {ann["count"]} not set by preceding "mov $N, %cl" at {a:#x}')
            else:
                require(ops[0].startswith('$') and int(ops[0][1:], 0) & 0xff == ann['count'] & 0xff, f'shift count {ann["count"]} vs "{t}"')
        prev = t
    return [t for _, _, t in insns]


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
        final = parse_output(p, (d / 'qemu.stdout').read_bytes())
        text = check_annotations(p, (d / 'qemu.log').read_text())
        und = flags.analyze(p['insns'])
        results.append({'name': p['name'], 'qemu': final, 'disasm': text, 'undefined_flags': hx(und), 'rflags_compare_mask': hx(flags.ARITH & ~und)})
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
