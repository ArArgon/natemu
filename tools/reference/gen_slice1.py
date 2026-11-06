#!/usr/bin/env python3
"""Seeded generator for the first-slice corpus (fixtures/slice1/programs.json).

    python3 tools/reference/gen_slice1.py -o fixtures/slice1/programs.json [--seed N] [--random 40]

Straight-line 64-bit integer + ordinary load/store programs, assembled with the host clang integrated
assembler.  Only inputs are produced here (bytes, initial state, instruction annotations); expected
results come from tools/reference/ref.py running the pinned QEMU, never from this file.
"""
import argparse, json, pathlib, random, re, subprocess, sys
HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import flags, ref

CODE, DATA, STACK, REGION = 0x100000, 0x200000, 0x300000, 0x2000     # two pages each: accesses can cross the 4 KiB boundary
from ref import R64, R32, R16, R8, HI, WIDTH
PTRS, INDEX, SCRATCH = [3, 5, 6, 7, 12, 13], 8, [0, 1, 2, 9, 10, 11, 14, 15]   # rbx rbp rsi rdi r12 r13 | r8 | writable scratch
DIVISORS = [1, 9, 10, 11, 14, 15]                                              # never rax/rdx, never pointers
NAMES = {64: R64, 32: R32, 16: R16, 8: R8}
SIZE = {8: 'byte', 16: 'word', 32: 'dword', 64: 'qword'}
CCS = list(flags.CC_READS)
ALU = ['add', 'adc', 'sub', 'sbb', 'and', 'or', 'xor', 'cmp']


def annotate(text, count=None):
    """Instruction annotation for flag analysis, derived from the assembly text (I3: `lock X` is X's, plus `lock`)."""
    if text.startswith('lock '): return annotate(text[5:], count) | {'lock': True}
    m = text.split()[0]; ops = [o.strip() for o in text[len(m):].split(',')] if len(text.split()) > 1 else []
    cc = None
    if m.startswith('set'): op, cc = 'setcc', m[3:]
    elif m.startswith('cmov'): op, cc = 'cmovcc', m[4:]
    else: op = {'movsxd': 'movsx', 'movabs': 'mov'}.get(m, m)
    if cc is not None: assert cc in flags.CC_READS, text
    first = re.sub(r' ptr.*', '', ops[0]).strip() if ops else ''
    width = WIDTH.get(first) or ({v: k for k, v in SIZE.items()}.get(first.split()[0]) if first else None) or 64
    ann = {'op': op, 'width': ref.IMPLICIT[op][1] if op in ref.IMPLICIT else width}
    if cc: ann['cc'] = cc
    if op in flags.SHIFTS | flags.ROTATES:
        ann['count'] = count if ops[1] == 'cl' else int(ops[1], 0)
        assert ann['count'] is not None, text
    return ann


class Prog:
    def __init__(self, name, rng):
        self.name, self.rng, self.lines, self.insns, self.undef, self.depth = name, rng, [], [], 0, 0
    cl = None                                   # value of CL if the previous instruction was `mov cl, imm`
    def emit(self, text, count=None, **extra):
        a = annotate(text, count if count is not None else self.cl) | extra; self.undef = flags.step(a, self.undef)
        m = re.fullmatch(r'mov cl, (\d+)', text); self.cl = int(m[1]) if m else None
        self.lines.append(text); self.insns.append(a)
    def need_flags(self, mask):               # make flags defined before a consumer reads them
        if mask & self.undef: self.emit(f'cmp {self.reg(64)}, {self.reg(64)}')
    def reg(self, w, hi=False, writable=False, rex_ok=True):
        r = self.rng
        if w == 8 and hi: return r.choice(['al', 'cl', 'dl'] + HI[:3])
        if w == 8 and not rex_ok: return r.choice(['al', 'cl', 'dl'])
        return NAMES[w][r.choice(SCRATCH if writable else SCRATCH + PTRS + [INDEX])]
    def imm(self, w):
        r = self.rng; lim = min(w, 32)
        pool = [0, 1, -1, 0x7f, -0x80, 0x80, 0xff, 0x7fff, -0x8000, 0x7fffffff, -0x80000000]
        v = r.choice(pool) if r.random() < 0.4 else r.randrange(-(1 << (lim - 1)), 1 << (lim - 1))
        return v if -(1 << (lim - 1)) <= v < (1 << (lim - 1)) else v & ((1 << lim) - 1) if w < 32 else v
    def mem(self, w, hi=False):
        r = self.rng; d = r.randrange(-0x200, 0x200); sc = r.choice([1, 2, 4, 8])
        b = r.choice(['rbx', 'rbp', 'rsi', 'rdi', 'rsp'] if hi else [R64[i] for i in PTRS] + ['rsp'])   # rbp/r13 need disp, rsp/r12 need SIB
        form = r.randrange(6 if hi else 8)
        s = {0: f'[{b}]', 1: f'[{b} + {d}]', 2: f'[{DATA + r.randrange(0, REGION - 8)}]', 3: f'[rip + {{{DATA + r.randrange(0, REGION - 8)}}}]',
             4: f'[{b}]', 5: f'[{b} + {d}]', 6: f'[{b} + r8*{sc}]', 7: f'[{b} + r8*{sc} + {d}]'}[form]
        return f'{SIZE[w]} ptr {s}'
    def rm(self, w, hi, writable=True):          # register or memory operand
        return self.mem(w, hi) if self.rng.random() < 0.4 else self.reg(w, hi, writable)
    def src(self, w, hi):                          # register, memory or immediate source
        k = self.rng.random()
        return str(self.imm(w)) if k < 0.3 else self.mem(w, hi) if k < 0.55 else self.reg(w, hi)

    def one(self):
        r = self.rng; w = r.choice([8, 16, 32, 32, 64, 64]); hi = w == 8 and r.random() < 0.3
        k = r.choices(['mov', 'movx', 'lea', 'alu', 'unary', 'setcc', 'cmov', 'shift', 'imul', 'mul', 'stack',
                       'conv', 'xchg', 'movabs', 'bswap', 'bt', 'bs', 'div'], [14, 6, 5, 20, 6, 4, 5, 12, 5, 3, 7, 2, 2, 3, 2, 4, 3, 3])[0]
        if k == 'mov':
            t = r.randrange(4)
            if t == 0: self.emit(f'mov {self.reg(w, hi, True)}, {self.reg(w, hi)}')
            elif t == 1: self.emit(f'mov {self.reg(w, hi, True)}, {r.randrange(1 << w) if w == 64 else self.imm(w)}')
            elif t == 2: self.emit(f'mov {self.reg(w, hi, True)}, {self.mem(w, hi)}')
            else: self.emit(f'mov {self.mem(w, hi)}, {self.reg(w, hi) if r.random() < .7 else self.imm(w)}')
        elif k == 'movx':
            sw = r.choice([8, 16]) if w > 8 else 8; w = max(w, 16) if sw == 8 else 32 if w < 32 else w
            hi = sw == 8 and hi; srcw = self.rm(sw, hi, writable=False)
            if sw == 16 and w == 16: w = 32
            op = r.choice(['movzx', 'movsx']) if w != 64 or sw != 32 else 'movsxd'
            self.emit(f'{op} {self.reg(w, writable=True)}, {srcw}')
        elif k == 'lea':
            w = r.choice([16, 32, 64]); b, i = (self.reg(64) for _ in range(2)); d = r.randrange(-0x80000, 0x80000)
            self.emit(r.choice([f'lea {self.reg(w, writable=True)}, [{b} + {d}]', f'lea {self.reg(w, writable=True)}, [{b} + {i}*{r.choice([1, 2, 4, 8])}]',
                                f'lea {self.reg(w, writable=True)}, [{i}*{r.choice([2, 4, 8])} + {d}]', f'lea {self.reg(w, writable=True)}, [{b} + {i}*{r.choice([1, 2, 4, 8])} + {d}]']))
        elif k == 'alu':
            op = r.choice(ALU + ['test', 'test']); 
            if op in ('adc', 'sbb'): self.need_flags(flags.CF)
            dst = self.rm(w, hi, True) if op not in ('cmp', 'test') else self.rm(w, hi, False)
            s = self.src(w, hi)
            if dst.startswith(SIZE[w]) and s.startswith(SIZE[w]): s = self.reg(w, hi)   # no mem,mem
            if op == 'test' and not s.lstrip('-').isdigit() and s.startswith(SIZE[w]): s = self.reg(w, hi)
            self.emit(f'{op} {dst}, {s}')
        elif k == 'unary': self.emit(f'{r.choice(["not", "neg", "inc", "dec"])} {self.rm(w, hi, True)}')
        elif k == 'setcc':
            cc = r.choice(CCS); self.need_flags(flags.CC_READS[cc]); self.emit(f'set{cc} {self.rm(8, hi, True)}')
        elif k == 'cmov':
            w = max(w, 16); cc = r.choice(CCS); self.need_flags(flags.CC_READS[cc])
            self.emit(f'cmov{cc} {self.reg(w, writable=True)}, {self.rm(w, False, False)}')
        elif k == 'shift':
            op = r.choice(['shl', 'shr', 'sar', 'rol', 'ror']); dst = self.rm(w, hi, True)
            if r.random() < 0.3:
                c = r.choice([0, 1, w - 1, w, w + 1, 31, 32, 63, 64, 65, r.randrange(256)]); self.emit(f'mov cl, {c}')
                if dst in ('ch', 'cl'): dst = 'dl'
                self.emit(f'{op} {dst}, cl')
            else: self.emit(f'{op} {dst}, {r.choice([0, 1, 1, 2, w - 1, w, w + 1, 31, 32, r.randrange(70)])}')
        elif k == 'imul':
            w = max(w, 16); t = r.randrange(2)
            self.emit(f'imul {self.reg(w, writable=True)}, {self.rm(w, False, False)}' + (f', {self.imm(w)}' if t else ''))
        elif k == 'mul': self.emit(f'{r.choice(["mul", "imul"])} {self.rm(w, hi, False)}')
        elif k == 'conv': self.emit(r.choice(list(ref.IMPLICIT)))
        elif k == 'xchg':
            a = self.reg(w, hi, True); b = self.reg(w, hi, True)
            while b == a: b = self.reg(w, hi, True)
            self.emit(f'xchg {a}, {b}')
        elif k == 'movabs':
            reg, addr = NAMES[w][0], DATA + r.randrange(0, REGION - 8); ptr = f'{SIZE[w]} ptr [{addr}]'
            self.emit(f'movabs {reg}, {ptr}' if r.random() < 0.5 else f'movabs {ptr}, {reg}')
        elif k == 'bswap': self.emit(f'bswap {self.reg(r.choice([32, 64]), writable=True)}')
        elif k == 'bt':
            w = max(w, 16); op = r.choice(['bt', 'bts', 'btr', 'btc']); dst = self.rm(w, False, True)
            off = self.reg(w) if not dst.startswith(SIZE[w]) and r.random() < 0.5 else r.choice([0, 1, w - 1, w, w + 1, r.randrange(256)])
            self.emit(f'{op} {dst}, {off}')
        elif k == 'bs':     # source made nonzero by construction: a preceding store/mov of a nonzero immediate
            w = max(w, 16); v = r.randrange(1, 1 << min(w, 31)); src = self.mem(w) if r.random() < 0.4 else self.reg(w, writable=True)
            self.emit(f'mov {src}, {v}'); self.emit(f'{r.choice(["bsf", "bsr"])} {self.reg(w, writable=True)}, {src}', src_nonzero=True)
        elif k == 'div':    # operands built so the division cannot fault: nonzero (and for idiv, not -1) divisor, dividend sign/zero-extended
            signed = r.random() < 0.5; lim = min(w, 16)
            v = r.choice([x for x in range(-(1 << (lim - 1)), 1 << (lim - 1)) if x not in (0, -1)][:: max(1, (1 << lim) // 64)]) if signed else r.randrange(1, 1 << lim)
            if w == 8 and not signed: v = r.randrange(1, 256)
            self.emit({(8, True): 'cbw', (16, True): 'cwd', (32, True): 'cdq', (64, True): 'cqo', (8, False): 'movzx eax, al'}.get((w, signed), 'xor edx, edx'))
            div = NAMES[w][r.choice(DIVISORS)]; opnd = self.mem(w) if r.random() < 0.3 else div
            self.emit(f'mov {opnd}, {v}'); self.emit(f'{"idiv" if signed else "div"} {opnd}')
        else:
            t = r.randrange(5)
            if t < 3 and self.depth < 12: self.depth += 1; self.emit(f'push {r.choice([self.reg(64), self.reg(16), str(self.imm(32)), "qword ptr " + self.mem(64).split(" ptr ")[1]])}')
            elif self.depth > 0: self.depth -= 1; self.emit(f'pop {r.choice([self.reg(64, writable=True), "qword ptr " + self.mem(64).split(" ptr ")[1]])}')


INTERESTING = [0, 1, 0xff, 0x80, 0x7fff, 0x8000, 0xffff, 0x7fffffff, 0x80000000, 0xffffffff, 2**63 - 1, 2**63, 2**64 - 1, 2**64 - 0x80]
def initial(rng):
    gpr = [rng.choice(INTERESTING) if rng.random() < 0.35 else rng.getrandbits(64) for _ in range(16)]
    for i in PTRS: gpr[i] = DATA + rng.randrange(0xc00, 0x1400)       # around the page boundary at DATA+0x1000
    gpr[INDEX], gpr[4] = rng.randrange(8), STACK + 0x1000 + rng.randrange(-8, 9)
    return gpr, rng.choice([0, 0x1, 0x41, 0x8d5, 0x800, 0x85, 0x10]) | 0

def finish(p, gpr, rflags, rng):
    lines = p.lines
    if any('{' in l for l in lines):     # RIP-relative: displacement = target - end of instruction (two passes; disp32 length is fixed)
        _, ends = ref.assemble('\n'.join(re.sub(r'\{\d+\}', '0', l) + f'\ne{i}:' for i, l in enumerate(lines)), labels=True)
        lines = [re.sub(r'\{(\d+)\}', lambda m: str(int(m[1]) - (CODE + ends[f'e{i}'])), l) for i, l in enumerate(lines)]
    code = ref.assemble('\n'.join(lines))
    mem = [{'addr': hex(DATA), 'data': bytes(rng.getrandbits(8) for _ in range(REGION)).hex()},
           {'addr': hex(STACK), 'data': bytes(rng.getrandbits(8) for _ in range(REGION)).hex()}]
    return {'name': p.name, 'code': code.hex(), 'rip': hex(CODE), 'gpr': [hex(g) for g in gpr], 'rflags': hex(rflags), 'memory': mem,
            'insns': p.insns, 'asm': lines}

def random_program(seed, n):
    for attempt in range(100):               # a draw the assembler cannot encode (e.g. high-byte + REX register) is redrawn
        rng = random.Random(seed + attempt); p = Prog(f'random-{n:02d}', rng); gpr, rflags = initial(rng)
        for _ in range(rng.randrange(8, 32)): p.one()
        try: return finish(p, gpr, rflags, rng)
        except ValueError as e: last = e
    raise last

# Hand-picked edge cases: (name, initial registers, initial flags, assembly).  rbx/rsi/rdi point into the data region.
EDGES = [
 ('carry-chain', {'rax': 2**64 - 1, 'rcx': 1, 'rdx': 0}, 0, ['add rax, rcx', 'adc rdx, 0', 'sbb rax, rax', 'sbb rdx, rcx', 'adc rcx, rcx', 'neg rax']),
 ('signed-overflow', {'rax': 0x7fffffff, 'rcx': 0x80000000, 'rdx': 1, 'r9': 0x7fff, 'r10': 0x80}, 0,
  ['add eax, 1', 'sub ecx, edx', 'add r9w, 1', 'sub r10b, 1', 'inc eax', 'dec ecx', 'cmp rax, rcx']),
 ('zero-extension-32', {'rax': 0xdeadbeefcafef00d, 'rdx': 2**64 - 1, 'rcx': 0xffffffff00000001, 'r10': 0x1122334455667788, 'r11': 2**64 - 1, 'r12': 0x7777777777777777, 'r13': 5}, 0x40,
  ['mov eax, eax', 'add edx, 1', 'xor ecx, ecx', 'mov r10d, 5', 'not r11d', 'cmp eax, eax', 'cmovne r12d, r13d', 'lea r13d, [rdx + rcx*4 - 1]']),
 ('merge-8-16', {'rax': 0xaaaaaaaaaaaaaaaa, 'rcx': 0xff, 'rdx': 1, 'r9': 0x5555555555555555, 'r10': 0x00ff, 'r11': 0x1}, 0,
  ['mov al, 0x12', 'mov ax, 0x3456', 'add cl, dl', 'mov r9w, 0x1234', 'add dx, 0xffff', 'inc r10b', 'neg r11b', 'movzx r9d, r10b', 'mov r10b, r9b']),
 ('high-byte', {'rax': 0x1122334455667788, 'rcx': 0x99aabbccddeeff00, 'rdx': 0x0123456789abcdef}, 0,
  ['mov ah, 0x7f', 'add ah, al', 'inc ch', 'mov dl, ah', 'add ah, 0x80', 'sub ch, dh', 'xor dh, dh', 'movsx ecx, ah', 'movzx eax, dh', 'cmp ah, ch']),
 ('shift-counts-32-64', {'rax': 0x80000001, 'rcx': 0xfedcba9876543210, 'rdx': 0x8000000000000001, 'r9': 0xdeadbeef}, 0x8d5,
  ['shl eax, 0', 'shl eax, 1', 'shr rdx, 63', 'shr rdx, 64', 'sar ecx, 31', 'sar ecx, 32', 'shl rcx, 65', 'sar r9d, 1', 'shl r9, 0']),
 ('shift-counts-8-16', {'rax': 0x8081, 'rcx': 0xf0f1, 'rdx': 0x1234, 'r9': 0xc001, 'r10': 0x7e}, 0x1,
  ['shl ax, 16', 'shl ax, 17', 'shl r10b, 8', 'shr dl, 9', 'sar cl, 7', 'rol r9b, 8', 'ror r9w, 16', 'rol dx, 17', 'shl ch, 1', 'ror al, 1']),
 ('shift-by-cl', {'rax': 0xf0f0f0f0f0f0f0f0, 'rcx': 0, 'rdx': 0x80000000, 'r9': 0x123456789}, 0x1,
  ['mov cl, 33', 'shl edx, cl', 'mov cl, 64', 'shr rax, cl', 'mov cl, 1', 'sar rdx, cl', 'mov cl, 255', 'rol r9, cl', 'mov cl, 0', 'ror r9d, cl', 'mov cl, 70', 'shl r9w, cl']),
 ('mul-imul', {'rax': 2**64 - 1, 'rdx': 2, 'rcx': 0xfffffffffffffff9, 'r9': 0x80000000, 'r10': 0x7f}, 0,
  ['mul rdx', 'imul rcx', 'imul eax, ecx, -7', 'imul ax, dx', 'mul cl', 'imul dl', 'mul r9d', 'add rax, 0', 'imul r10, r10, 0x7fffffff', 'imul r10w, r10w, 0x4000']),
 ('neg-inc-dec-not', {'rax': 0, 'rcx': 0x80000000, 'rdx': 0xffffffffffffffff, 'r9': 0x7f, 'r10': 0}, 0x1,
  ['inc rdx', 'dec r10', 'neg rax', 'neg ecx', 'neg r9b', 'not edx', 'dec r10', 'inc r9b', 'neg r10']),
 ('load-store-widths', {'rax': 0x8877665544332211, 'rcx': 0xf1f2f3f4, 'rdx': 0xa5a5, 'r9': 0xcc}, 0,
  ['mov qword ptr [rbx], rax', 'mov dword ptr [rbx + 8], ecx', 'mov word ptr [rbx + 12], dx', 'mov byte ptr [rbx + 14], r9b', 'mov rdx, qword ptr [rbx + 3]', 'mov r10b, byte ptr [rbx + 1]',
   'movzx r11d, word ptr [rbx + 6]', 'movsx r12, byte ptr [rbx + 7]', 'movsxd r13, dword ptr [rbx + 8]', 'mov r14w, word ptr [rbx + 13]', 'mov eax, dword ptr [rsi + r8*4 - 4]',
   'mov qword ptr [rdi + r8*8 + 16], -1', 'mov byte ptr [2097152 + 4000], 0x80', 'mov r15, qword ptr [rdi + r8*8 + 16]']),
 ('push-pop', {'rax': 0x1122334455667788, 'rcx': 0xabcd, 'rdx': 0, 'r9': 0}, 0,
  ['push rax', 'push 0x1234', 'push -1', 'push cx', 'push qword ptr [rbx]', 'pop rdx', 'pop r9', 'pop qword ptr [rdi + 8]', 'pop ax', 'pop r10', 'pop r11']),
 ('setcc-cmov', {'rax': 5, 'rcx': 7, 'rdx': 0, 'r9': 0x1111111111111111, 'r10': 0x2222222222222222}, 0,
  ['cmp rax, rcx'] + [f'set{cc} {r}' for cc, r in zip(CCS, ['al', 'cl', 'dl', 'r9b', 'r10b', 'r11b', 'r12b', 'r13b', 'r14b', 'r15b', 'bpl', 'al', 'cl', 'dl', 'r9b', 'r10b'])]
  + ['cmovl r9, rax', 'cmovge r10d, ecx', 'cmovbe r11w, ax', 'cmova r12, qword ptr [rbx]', 'setle byte ptr [rdi + 5]', 'setp byte ptr [rdi + 6]']),
 ('test-cmp-flags', {'rax': 0xf0, 'rcx': 0x0f, 'rdx': 0x80, 'r9': 0x8000000000000000}, 0,
  ['test al, cl', 'sete r10b', 'test rdx, rdx', 'sets r11b', 'cmp al, 0x80', 'setl r12b', 'cmp r9, 1', 'seto r13b', 'cmp ecx, 0x10', 'setb r14b', 'test r9, r9', 'setp r15b']),
]

def RIP(addr): return f'[rip + {{{addr}}}]'          # resolved to a displacement by finish()
EDGES += [
 ('rip-relative', {'rax': 0x1122334455667788, 'rcx': 0xdeadbeef, 'r9': 0}, 0x1,
  [f'mov rdx, qword ptr {RIP(DATA + 0x10)}', f'mov dword ptr {RIP(DATA + 0x20)}, ecx', f'movzx r9d, byte ptr {RIP(DATA + 0x30)}', f'add qword ptr {RIP(DATA + 0x40)}, 0x1234',
   f'cmp word ptr {RIP(DATA + 0x50)}, 0x7fff', f'mov byte ptr {RIP(DATA + 0xffe)}, cl', f'mov rax, qword ptr {RIP(DATA + 0xffc)}', f'mov qword ptr {RIP(STACK + 0x1ff8)}, rax']),
 ('special-bases', {'rsp': STACK + 0x1000, 'rbp': DATA + 0x400, 'r12': DATA + 0x500, 'r13': DATA + 0x600, 'r8': 3, 'rax': 0x0102030405060708, 'rcx': 0x1111, 'rdx': 0x80}, 0,
  ['mov r9, qword ptr [rsp]', 'mov r10, qword ptr [rsp + 8]', 'mov qword ptr [rsp - 16], rax', 'mov r11, qword ptr [rbp]', 'mov r14d, dword ptr [rbp + 0x100]',
   'mov qword ptr [r12], rax', 'mov r15, qword ptr [r12 + r8*4]', 'mov word ptr [r13], cx', 'mov r9w, word ptr [r13 + 0x10]', 'mov byte ptr [r13 + r8*1 + 0x100], dl',
   'add dword ptr [rsp + r8*8 + 16], 7', 'mov ecx, dword ptr [rbp + r8*2]', 'movzx eax, byte ptr [r12 + r8*4 + 1]']),
 ('page-crossing', {'rbx': DATA + 0xffc, 'rsi': DATA + 0xfff, 'rdi': DATA + 0xffd, 'rsp': STACK + 0x1004, 'rax': 0x8877665544332211, 'rcx': 0xaabbccdd, 'rdx': 0xeeff},
  0, ['mov qword ptr [rbx], rax', 'mov r9, qword ptr [rbx]', 'mov dword ptr [rbx + 2], ecx', 'mov r10d, dword ptr [rbx + 1]', 'mov word ptr [rsi], dx', 'movzx r11d, word ptr [rsi]',
      'mov r12, qword ptr [rdi]', 'add word ptr [rsi - 1], 0x101', 'push rax', 'push cx', 'push -2', 'pop r13', 'pop r14w', 'pop r15', 'movabs qword ptr [2101244], rax', 'movabs eax, dword ptr [2101246]']),
 ('sign-extend-family', {'rax': 0x1122334455668788, 'rdx': 0x5a5a5a5a5a5a5a5a}, 0,
  ['cbw', 'mov rax, 0x1122334455667f80', 'cwde', 'mov rax, 0x80000000', 'cdqe', 'mov rax, 0x0badc0de7fff8000', 'cwd', 'mov rax, 0x1234567880000001', 'cdq',
   'mov rax, 0x7fffffffffffffff', 'cqo', 'mov rax, -2', 'cqo', 'mov ax, 0x7f', 'cbw', 'cwd']),
 ('xchg', {'rax': 0x1111111122222222, 'rcx': 0x3333333344444444, 'rdx': 0xaabbccddeeff0011, 'r9': 0x5555555566666666, 'r10': 0x1234, 'r11': 0x5678}, 0x8d5,
  ['xchg rax, rcx', 'xchg r9d, edx', 'xchg al, ah', 'xchg r10w, r11w', 'xchg r9, r10', 'xchg cl, dl', 'xchg dh, ch']),
 ('movabs', {'rax': 0xfedcba9876543210}, 0,
  ['movabs qword ptr [2097152 + 64], rax', 'mov rcx, qword ptr [2097152 + 64]', 'movabs dword ptr [2097152 + 72], eax', 'movabs word ptr [2097152 + 80], ax', 'movabs byte ptr [2097152 + 88], al',
   'movabs eax, dword ptr [2097152 + 64]', 'movabs al, byte ptr [2097152 + 65]', 'movabs ax, word ptr [2097152 + 66]', 'movabs rax, qword ptr [2101244]', 'movabs qword ptr [2101248], rax']),
 ('bswap', {'rax': 0x0102030405060708, 'rcx': 0xffffffff11223344, 'r9': 0x8000000000000001}, 0x8d5, ['bswap eax', 'bswap rax', 'bswap ecx', 'bswap r9', 'bswap r9d']),
 ('bit-test', {'rax': 0x8000000000000001, 'rcx': 67, 'rdx': 0xf0f0, 'r9': 0x1, 'r10': 0xffff}, 0x40,
  ['bt rax, 63', 'setb r11b', 'bts rax, 1', 'btr rax, rcx', 'adc r9, 0', 'btc edx, 33', 'bt dx, 17', 'bts r10w, 16', 'btr r10d, 0', 'btc dword ptr [rbx + 4], 31', 'bts word ptr [rbx], 15',
   'btr qword ptr [rbx + 8], 63', 'bt qword ptr [rbx + 16], 64', 'cmovb r9, rdx']),
 ('bit-scan', {'rax': 0, 'rcx': 0, 'rdx': 0xdead, 'r9': 0x77}, 0,
  ['mov rcx, 0x8000', 'bsf rax, rcx', 'bsr r9, rcx', 'mov edx, 0x40000000', 'bsf r10d, edx', 'bsr r11d, edx', 'mov r12w, 0x4001', 'bsf ax, r12w', 'bsr cx, r12w',
   'mov dword ptr [rbx], 0x10', 'bsf r13d, dword ptr [rbx]', 'mov qword ptr [rsi], 0x7fffffff', 'bsr r14, qword ptr [rsi]']),
 ('division', {'rax': 200, 'rcx': 7, 'rdx': 0xdeadbeef, 'r9': 300, 'r10': 0, 'r11': 0}, 0,
  ['div cl', 'mov eax, 100000', 'xor edx, edx', 'mov ecx, 7', 'div ecx', 'mov rax, -100', 'cqo', 'mov rcx, 7', 'idiv rcx', 'mov ax, 0xffff', 'xor edx, edx', 'mov r9w, 300', 'div r9w',
   'mov eax, -2147483648', 'cdq', 'mov ecx, 2', 'idiv ecx', 'mov al, -128', 'cbw', 'mov cl, 3', 'idiv cl', 'mov dword ptr [rbx], 10', 'mov rax, 12345', 'xor edx, edx', 'div dword ptr [rbx]']),
]

def edge_program(spec, rng):
    name, regs, rflags, lines = spec; p = Prog('edge-' + name, rng); gpr, _ = initial(rng)
    gpr = [0x0101010101010101 * (i + 1) if i not in PTRS + [INDEX, 4] else gpr[i] for i in range(16)]
    for r, v in regs.items(): gpr[R64.index(r)] = v
    for l in lines:     # bsf/bsr edge cases each have a nonzero-immediate move into their source immediately before
        p.emit(l, **({'src_nonzero': True} if l.split()[0] in ('bsf', 'bsr') else {}))
    return finish(p, gpr, rflags, rng)

def main():
    ap = argparse.ArgumentParser(); ap.add_argument('-o', '--output', type=pathlib.Path, required=True)
    ap.add_argument('--seed', type=int, default=20261026); ap.add_argument('--random', type=int, default=40)
    a = ap.parse_args(); rng = random.Random(a.seed)
    progs = [edge_program(e, rng) for e in EDGES] + [random_program(rng.getrandbits(64), n) for n in range(a.random)]
    clang = subprocess.run(['clang', '--version'], capture_output=True, text=True).stdout.splitlines()[0]
    a.output.write_text(json.dumps({'generator': {'script': 'tools/reference/gen_slice1.py', 'seed': a.seed, 'assembler': clang,
        'layout': {'code': hex(CODE), 'data': hex(DATA), 'stack': hex(STACK)}}, 'programs': progs}, indent=1) + '\n')
    print(f'{len(progs)} programs -> {a.output}')

if __name__ == '__main__': main()
