#!/usr/bin/env python3
"""Seeded generator for the I1 corpus: small integer and BMI forms (boot-features plan, chunk I1).

    python3 tools/reference/gen_i1.py -o OUT.json [--seed N] [--random 60]

Straight-line programs mixing the I1 forms (flag moves CLC/STC/CMC/CLD/STD, LAHF/SAHF, PUSHF, ENTER/LEAVE, SHLD/SHRD,
CMPXCHG/XADD without LOCK, POPCNT/TZCNT/LZCNT, BMI1/BMI2 but PDEP/PEXT, PREFETCHh, MOVNTI) with the slice-1 forms of
gen_slice1.py, random operands and flags, plus hand-picked edge programs. Inputs only: expected results come from
ref.py under QEMU with the BMI1/BMI2/ABM/POPCNT features the CPU profile advertises (`cpu` of each program).
"""
import argparse, json, pathlib, random, re, subprocess, sys
HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import flags, gen_slice1 as g
from gen_slice1 import DATA, STACK, REGION, SIZE, NAMES

CPU = 'qemu64,+bmi1,+bmi2,+abm,+popcnt'      # tools/models/cpu_profile.json: BMI1, BMI2, LZCNT (ABM) and POPCNT advertised
BMI3 = ['andn', 'bextr', 'bzhi', 'sarx', 'shlx', 'shrx', 'mulx']


class Prog(g.Prog):
    def emit(self, text, count=None, **extra):      # gen_slice1's, plus the count annotation of shld/shrd
        a = g.annotate(text, count if count is not None else self.cl) | extra
        if a['op'] in ('shld', 'shrd'): a['count'] = count if count is not None else self.cl
        self.undef = flags.step(a, self.undef)
        m = re.fullmatch(r'mov cl, (\d+)', text); self.cl = int(m[1]) if m else None
        self.lines.append(text); self.insns.append(a)

    def one(self):
        if self.rng.random() < 0.45:
            return super().one()
        r = self.rng
        k = r.choice(['flag', 'flag', 'lahf', 'sahf', 'pushf', 'frame', 'shd', 'shd', 'cmpxchg', 'xadd', 'cnt', 'cnt',
                      'bmi', 'bmi', 'bmi', 'bls', 'rorx', 'prefetch', 'movnti'])
        w = r.choice([32, 64])
        if k == 'flag':
            op = r.choice(['clc', 'stc', 'cmc', 'cld', 'std'])
            if op == 'cmc': self.need_flags(flags.CF)
            self.emit(op)
        elif k == 'lahf':
            self.need_flags(flags.SF | flags.ZF | flags.AF | flags.PF | flags.CF); self.emit('lahf')
        elif k == 'sahf': self.emit('sahf')
        elif k == 'pushf':
            if self.depth < 12:
                self.need_flags(flags.ARITH); self.depth += 1; self.emit('pushfq', op='pushf')
        elif k == 'frame':       # enter N, 0 ... leave: RSP and RBP come back (RBP is a data pointer)
            self.emit(f'enter {r.choice([0, 8, 16, 0x18, 0x100, r.randrange(0, 0x200)])}, 0')
            if r.random() < 0.6: self.emit(f'mov qword ptr [rbp - {r.choice([8, 16])}], {self.reg(64)}')
            self.emit('leave')
        elif k == 'shd':
            op = r.choice(['shld', 'shrd']); dst = self.rm(w, False, True); src = self.reg(w)
            # The pinned QEMU (7.2) addresses a RIP-relative shld/shrd with an imm8 count one byte low (it leaves the imm8
            # out of the instruction end); fexc computes the SDM address (tools/fexc/test_slice1.py). No such form here.
            while '[rip' in dst: dst = self.rm(w, False, True)
            if r.random() < 0.35:
                c = r.choice([0, 1, w - 1, w, w + 1, 31, 32, 63, 64, r.randrange(256)]); self.emit(f'mov cl, {c}')
                if dst == NAMES[w][1]: dst = NAMES[w][2]       # keep CL the count's register
                if src == NAMES[w][1]: src = NAMES[w][9]
                self.emit(f'{op} {dst}, {src}, cl', count=c)
            else:
                c = r.choice([0, 1, 2, w - 1, w, 31, 32, 63, r.randrange(256)])
                self.emit(f'{op} {dst}, {src}, {c}', count=c)
        elif k in ('cmpxchg', 'xadd'):
            w = r.choice([8, 16, 32, 64]); dst = self.rm(w, False, True); src = self.reg(w, writable=True)
            if k == 'cmpxchg' and r.random() < 0.5 and dst != NAMES[w][0]:     # make the comparison succeed sometimes
                self.emit(f'mov {NAMES[w][0]}, {dst}')
            self.emit(f'{k} {dst}, {src}')
        elif k == 'cnt':
            w = r.choice([16, 32, 64]); op = r.choice(['popcnt', 'tzcnt', 'lzcnt'])
            src = self.zero_or(w)
            self.emit(f'{op} {self.reg(w, writable=True)}, {src}')
        elif k == 'bmi':
            op = r.choice(BMI3); a, b = self.reg(w, writable=True), self.reg(w, writable=True); m = self.rm(w, False, False)
            if op in ('bextr', 'bzhi', 'sarx', 'shlx', 'shrx'):      # b: the start/length, index or count
                if r.random() < 0.6: self.emit(f'mov {b}, {r.choice([0, 1, 7, w - 1, w, w + 1, 0x0808, 0x1004, 0x2010, 0x4020, 0xff, r.getrandbits(16)])}')
                self.emit(f'{op} {a}, {m}, {b}')
            else:
                self.emit(f'{op} {a}, {b}, {m}')
        elif k == 'bls':
            self.emit(f'{r.choice(["blsi", "blsmsk", "blsr"])} {self.reg(w, writable=True)}, {self.zero_or(w)}')
        elif k == 'rorx':
            self.emit(f'rorx {self.reg(w, writable=True)}, {self.rm(w, False, False)}, {r.choice([0, 1, w - 1, w, r.randrange(256)])}')
        elif k == 'prefetch':
            op = r.choice(['prefetcht0', 'prefetcht1', 'prefetcht2', 'prefetchnta'])
            # any address, mapped or not, canonical or not: a hint never faults (SDM PREFETCHh)
            m = self.mem(8) if r.random() < 0.6 else f'byte ptr [{r.choice(["rax", "rcx", "rdx", "r9", "r10"])}]'
            self.emit(f'{op} {m}')
        else:
            self.emit(f'movnti {self.mem(w)}, {self.reg(w)}')

    def zero_or(self, w):
        """A source operand, sometimes a register just zeroed (sub: all flags defined)."""
        if self.rng.random() < 0.2:
            z = self.reg(w, writable=True); self.emit(f'sub {z}, {z}'); return z
        return self.rm(w, False, False)


BMI_OPS = {'popcnt', 'tzcnt', 'lzcnt', 'andn', 'bextr', 'blsi', 'blsmsk', 'blsr', 'bzhi', 'mulx', 'rorx', 'sarx', 'shlx', 'shrx'}


def annotate(text):
    """gen_slice1.annotate plus the I1 specifics (shld/shrd immediate count, pushfq), for single lines (fault probes)."""
    a = g.annotate(text)
    m, ops = text.split()[0], g.ref.att_operands('x ' + text.split(None, 1)[1]) if len(text.split()) > 1 else []
    if m == 'pushfq': a['op'] = 'pushf'
    if m in ('shld', 'shrd'): a['count'] = int(ops[-1], 0)
    return a


def cpu(lines):
    """The QEMU CPU model for lines using BMI/ABM/POPCNT forms, else None (qemu64)."""
    return CPU if any(l.split()[0] in BMI_OPS for l in lines) else None


def random_program(seed, n):
    for attempt in range(100):
        rng = random.Random(seed + attempt); p = Prog(f'i1-random-{n:02d}', rng); gpr, rflags = g.initial(rng)
        for _ in range(rng.randrange(10, 36)): p.one()
        try: return g.finish(p, gpr, rflags, rng) | {'cpu': CPU}
        except ValueError as e: last = e
    raise last


EDGES = [
 ('flag-moves', {}, 0x8d5, ['clc', 'cmc', 'stc', 'cmc', 'cmc', 'std', 'cld', 'std', 'pushfq', 'clc', 'pushfq', 'cld', 'stc', 'pushfq', 'pop rax', 'pop rcx', 'pop rdx', 'std']),
 ('lahf-sahf', {'rax': 0x1234_5678_9abc_d5ff, 'rcx': 0x00ff, 'rdx': 0}, 0, ['cmp rcx, rdx', 'lahf', 'mov r9, rax', 'mov ah, 0xd7', 'sahf', 'lahf', 'mov ah, 0',
   'sahf', 'pushfq', 'mov ah, 0x2a', 'sahf', 'pushfq', 'pop r10', 'pop r11', 'lahf']),
 ('pushf-all', {'rax': 0x7fffffffffffffff, 'rcx': 1}, 0x8d5, ['pushfq', 'add rax, rcx', 'pushfq', 'std', 'pushfq', 'sub rax, rcx', 'pushfq', 'cld', 'stc', 'pushfq',
   'sub eax, eax', 'pushfq', 'pop r9', 'pop r10', 'pop r11', 'pop r12', 'pop r13', 'pop r14']),
 ('enter-leave', {'rbp': DATA + 0x800, 'rax': 0x1111222233334444}, 0, ['enter 0, 0', 'mov qword ptr [rbp - 8], rax', 'leave', 'enter 0x20, 0', 'push rax', 'push rax',
   'leave', 'enter 0x1ff, 0', 'leave', 'push rbp', 'mov rbp, rsp', 'mov qword ptr [rbp], rax', 'leave']),
 ('shld-shrd-counts', {'rax': 0x8000000000000001, 'rcx': 0, 'rdx': 0xfedcba9876543210, 'r9': 0x80000001, 'r10': 0x7}, 0x1,
  ['shld rax, rdx, 0', 'shld rax, rdx, 1', 'shrd rax, rdx, 1', 'shld rax, rdx, 63', 'shrd r9d, edx, 31', 'shld r9d, edx, 32', 'shrd rax, rdx, 64',
   'mov cl, 0', 'shld rax, rdx, cl', 'mov cl, 33', 'shrd r9d, edx, cl', 'mov cl, 65', 'shld rax, rdx, cl', 'mov cl, 1', 'shrd r10, rax, cl',
   'shld qword ptr [rbx], rdx, 13', 'shrd dword ptr [rbx + 8], r9d, 7', 'mov cl, 200', 'shld qword ptr [rbx + 16], rax, cl', 'shrd qword ptr [rsi], rdx, 0']),
 ('cmpxchg-widths', {'rax': 0xaaaaaaaa_00000005, 'rcx': 0x99, 'rdx': 5, 'r9': 0x1234, 'rsi': DATA + 0xffd}, 0,
  ['mov qword ptr [rbx], 5', 'cmpxchg dword ptr [rbx], ecx', 'cmpxchg dword ptr [rbx], ecx', 'mov rax, 0xaaaaaaaa00000077', 'cmpxchg dword ptr [rbx], edx',
   'cmpxchg qword ptr [rbx + 8], r9', 'mov rax, qword ptr [rbx + 8]', 'cmpxchg qword ptr [rbx + 8], r9', 'cmpxchg byte ptr [rbx + 16], cl', 'cmpxchg word ptr [rbx + 18], r9w',
   'cmpxchg rcx, rdx', 'mov rax, rcx', 'cmpxchg rcx, rdx', 'cmpxchg ecx, r9d', 'cmpxchg eax, edx', 'cmpxchg al, ah', 'cmpxchg qword ptr [rsi], rcx',
   'mov rax, qword ptr [rsi]', 'cmpxchg qword ptr [rsi], rdx', 'xadd dword ptr [rsi + 1], r9d']),
 ('xadd-widths', {'rax': 0xffffffffffffffff, 'rcx': 1, 'rdx': 0x7f, 'r9': 0x8000}, 0,
  ['xadd rax, rcx', 'xadd qword ptr [rbx], rcx', 'xadd dword ptr [rbx + 8], ecx', 'xadd byte ptr [rbx + 12], dl', 'xadd word ptr [rbx + 14], r9w', 'xadd rcx, rcx',
   'xadd edx, eax', 'xadd dl, dl']),
 ('popcnt-zcnt', {'rax': 0, 'rcx': 0xffffffffffffffff, 'rdx': 0x8000000000000000, 'r9': 0x10}, 0x8d5,
  ['popcnt r10, rax', 'popcnt r11, rcx', 'popcnt r12d, ecx', 'popcnt r13w, cx', 'tzcnt r10, rax', 'tzcnt r11d, eax', 'tzcnt r12w, ax', 'lzcnt r13, rax',
   'lzcnt r14d, eax', 'lzcnt r15w, ax', 'tzcnt r10, rdx', 'lzcnt r11, rdx', 'lzcnt r12, r9', 'tzcnt r13, r9', 'lzcnt r14d, edx', 'tzcnt r15w, dx',
   'mov qword ptr [rbx], 0x00f0', 'popcnt rax, qword ptr [rbx]', 'tzcnt rcx, qword ptr [rbx]', 'lzcnt edx, dword ptr [rbx]']),
 ('bmi1', {'rax': 0xf0f0f0f0f0f0f0f0, 'rcx': 0x0ff00ff00ff00ff0, 'rdx': 0, 'r9': 0x0000000000003008, 'r10': 0x4040}, 0,
  ['andn r11, rax, rcx', 'andn r12d, eax, ecx', 'bextr r13, rax, r9', 'bextr r14d, ecx, r10d', 'bextr r15, rax, rdx', 'blsi r11, rcx', 'blsi r12, rdx',
   'blsmsk r13, rcx', 'blsmsk r14, rdx', 'blsr r15d, ecx', 'blsr r11d, edx', 'mov qword ptr [rbx], 0x10', 'blsr r12, qword ptr [rbx]', 'andn r13, rax, qword ptr [rbx]',
   'mov r9, 0x4000', 'bextr r14, rax, r9', 'mov r9, 0xff00', 'bextr r15, rax, r9']),
 ('bmi2', {'rax': 0x8000000000000001, 'rcx': 65, 'rdx': 0xfedcba9876543210, 'r9': 64, 'r10': 0}, 0x1,
  ['bzhi r11, rdx, rcx', 'bzhi r12, rdx, r9', 'bzhi r13d, edx, r10d', 'bzhi r14, rdx, rax', 'sarx r15, rdx, rcx', 'shlx r11, rdx, r9', 'shrx r12d, edx, ecx',
   'mulx r13, r14, rax', 'mulx r15, r15, rcx', 'mulx r11d, r12d, eax', 'rorx r13, rdx, 0', 'rorx r14, rdx, 63', 'rorx r15d, edx, 33',
   'shlx r11, qword ptr [rbx], rcx', 'mulx r12, r13, qword ptr [rbx]']),
 ('prefetch-movnti', {'rax': 0xdead000000000000, 'rcx': 0, 'rdx': 0x8000_0000_0000, 'r9': 0x1122334455667788, 'rdi': DATA + 0xffc}, 0,
  ['prefetcht0 byte ptr [rax]', 'prefetcht1 byte ptr [rcx]', 'prefetcht2 byte ptr [rdx]', 'prefetchnta byte ptr [rbx]', 'prefetcht0 byte ptr [rsi + 0x1000]',
   'movnti qword ptr [rbx], r9', 'movnti qword ptr [rdi], r9', 'movnti dword ptr [rdi + 2], r9d', 'mov r10, qword ptr [rdi]']),
]


def edge_program(spec, rng):
    name, regs, rflags, lines = spec; p = Prog('i1-edge-' + name, rng); gpr, _ = g.initial(rng)
    gpr = [0x0101010101010101 * (i + 1) if i not in g.PTRS + [g.INDEX, 4] else gpr[i] for i in range(16)]
    for r, v in regs.items(): gpr[g.R64.index(r)] = v
    for l in lines:
        m = l.split()[0]
        extra = {'op': 'pushf'} if m == 'pushfq' else {}
        if m in ('shld', 'shrd'):
            c = l.rsplit(',', 1)[1].strip()
            extra['count'] = p.cl if c == 'cl' else int(c, 0)
        p.emit(l, **extra)
    return g.finish(p, gpr, rflags, rng) | {'cpu': CPU}


def main():
    ap = argparse.ArgumentParser(); ap.add_argument('-o', '--output', type=pathlib.Path, required=True)
    ap.add_argument('--seed', type=int, default=20261028); ap.add_argument('--random', type=int, default=60)
    a = ap.parse_args(); rng = random.Random(a.seed)
    progs = [edge_program(e, rng) for e in EDGES] + [random_program(rng.getrandbits(64), n) for n in range(a.random)]
    clang = subprocess.run(['clang', '--version'], capture_output=True, text=True).stdout.splitlines()[0]
    a.output.write_text(json.dumps({'generator': {'script': 'tools/reference/gen_i1.py', 'seed': a.seed, 'assembler': clang, 'cpu': CPU,
        'layout': {'code': hex(g.CODE), 'data': hex(DATA), 'stack': hex(STACK)}}, 'programs': progs}, indent=1) + '\n')
    print(f'{len(progs)} programs -> {a.output}')

if __name__ == '__main__': main()
