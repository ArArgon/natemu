#!/usr/bin/env python3
"""Seeded generator for the SHA corpus: SHA1RNDS4, SHA1NEXTE, SHA1MSG1, SHA1MSG2, SHA256RNDS2, SHA256MSG1, SHA256MSG2
(CPUID.7.0:EBX.SHA[29], advertised since November 3; boot-features plan, Built (SHA)).

    python3 tools/reference/gen_sha.py -o OUT.json [--seed N]

Pinned QEMU 7.2 has no SHA in TCG, so the reference is the reference AMD machine running each program's ELF natively
(`ref_native.py`); `run_slice1_vm.py` then compares natemu with it like any corpus. Each program loads XMM0..15 from
random slots, runs 16 SHA instructions and stores each destination right after its instruction, then all 16 registers.
Families: `sha-<insn>-*` (4 per instruction, only that instruction), `sha-rnds4-imm-*` (16: SHA1RNDS4 with every imm8,
16 each), `sha-mix-*` (16, all seven; SHA256RNDS2's implicit XMM0 is also written by earlier instructions). Operands:
registers XMM0..15 (destination = source too), memory (16-byte aligned: exception class 4) absolute, through RBX, R13,
R12 + R8*8, RIP-relative, with 0x67 through EBX, and raw-encoded with REX.W (ignored) and DS/ES/CS/SS prefixes (ignored).
"""
import argparse, json, pathlib, random, subprocess, sys
HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import ref, gen_i9
import gen_slice1 as g
from gen_slice1 import DATA

OPND, RESULT, FINAL = DATA + 0x800, DATA + 0x1800, DATA + 0x1a00   # operand slots (aligned), per-instruction results, XMM0..15
OPS = ['sha1nexte', 'sha1msg1', 'sha1msg2', 'sha256rnds2', 'sha256msg1', 'sha256msg2', 'sha1rnds4']
R12, R13 = OPND + 0x200, OPND + 0x400


def raw(text, prefixes=b'', rexw=False):
    """`text` assembled, with legacy `prefixes` in front and REX.W set (an existing REX, or 48H before the 0F escape)."""
    b = bytearray(ref.assemble(text))
    if rexw:
        at = b.index(0x0f)
        if at and 0x40 <= b[at - 1] <= 0x4f: b[at - 1] |= 8
        else: b.insert(at, 0x48)
    return '.byte ' + ', '.join(f'{x:#04x}' for x in bytes(prefixes) + bytes(b))


def one(p, r, op, imm=None):
    d = r.randrange(16)
    k = r.randrange(10)
    slot = 16 * r.randrange(0x60)                                    # within OPND .. OPND + 0x600
    src = [f'xmm{r.randrange(16)}', f'xmm{d}', f'xmmword ptr [{OPND + slot}]', f'xmmword ptr [rbx + {slot}]',
           f'xmmword ptr [r13 + {16 * r.randrange(0x20)}]', f'xmmword ptr [r12 + r8*8 + {16 * r.randrange(0x20)}]',
           f'xmmword ptr [rip + {{{OPND + slot}}}]', f'xmmword ptr [ebx + {slot}]', f'xmm{r.randrange(16)}', f'xmmword ptr [{OPND + slot}]'][k]
    imm = r.randrange(256) if imm is None else imm
    s = None if 'ptr' in src else int(src[3:])
    ea = [None, None, OPND + slot, OPND + slot, None, None, OPND + slot, OPND + slot, None, OPND + slot][k]
    if k == 4: ea = R13 + int(src.split('+ ')[1][:-1])
    if k == 5: ea = R12 + 8 * p.r8 + int(src.split('+ ')[2][:-1])
    p.ops.append([op, d, s, ea, imm])
    text = f'{op} xmm{d}, {src}' + (f', {imm}' if op == 'sha1rnds4' else '')
    if k >= 8:   # raw: REX.W and/or ignored segment prefixes
        text = raw(text, bytes(r.sample([0x3e, 0x26, 0x2e, 0x36], r.randrange(3))), rexw=r.random() < 0.7)
    p.x(text, op)
    p.x(f'movdqu xmmword ptr [{RESULT + 16 * p.done}], xmm{d}', 'movdqu'); p.done += 1


def program(name, rng, ops, imms=None):
    r = random.Random(rng.getrandbits(64)); gpr, rf = g.initial(r); p = gen_i9.Prog(name, r); p.done = 0
    gpr[3], gpr[12], gpr[13], gpr[8] = OPND, R12, R13, 2 * r.randrange(4)    # rbx, r12, r13: aligned bases; r8 even (r8*8)
    p.r8, p.ops, p.loads = gpr[8], [], []
    for i in range(16):
        p.loads.append(OPND + 16 * r.randrange(0xa0)); p.x(f'movdqu xmm{i}, xmmword ptr [{p.loads[-1]}]')
    for i in range(16): one(p, r, r.choice(ops), None if imms is None else imms[i])
    for i in range(16): p.x(f'movdqu xmmword ptr [{FINAL + 16 * i}], xmm{i}', 'movdqu')
    return g.finish(p, gpr, rf, r) | {'sse': True, 'sha': {'loads': p.loads, 'ops': p.ops}}


# The SDM's pseudocode (Vol.2 SHA1RNDS4, SHA1NEXTE, SHA1MSG1, SHA1MSG2, SHA256RNDS2, SHA256MSG1, SHA256MSG2) on 128-bit ints.
M = 0xffffffff
def rol(x, n): return (x << n | x >> (32 - n)) & M
def ror(x, n): return rol(x, 32 - n)
def lanes(v): return [v >> (32 * i) & M for i in range(4)]          # [31:0], [63:32], [95:64], [127:96]
def pack(l): return sum((x & M) << (32 * i) for i, x in enumerate(l))

def sha(op, s1, s2, xmm0=0, imm=0):
    a, b = lanes(s1), lanes(s2)
    if op == 'sha1nexte': return pack(b[:3] + [(b[3] + rol(a[3], 30)) & M])
    if op == 'sha1msg1':
        w0, w1, w2, w3, w4, w5 = a[3], a[2], a[1], a[0], b[3], b[2]
        return pack([w5 ^ w3, w4 ^ w2, w3 ^ w1, w2 ^ w0])
    if op == 'sha1msg2':
        w13, w14, w15 = b[2], b[1], b[0]
        w16 = rol(a[3] ^ w13, 1); w17 = rol(a[2] ^ w14, 1); w18 = rol(a[1] ^ w15, 1); w19 = rol(a[0] ^ w16, 1)
        return pack([w19, w18, w17, w16])
    if op == 'sha1rnds4':
        A, B, C, D = a[3], a[2], a[1], a[0]; W = [b[3], b[2], b[1], b[0]]; E = 0
        f, K = [(lambda b, c, d: (b & c) ^ (~b & d & M), 0x5A827999), (lambda b, c, d: b ^ c ^ d, 0x6ED9EBA1),
                (lambda b, c, d: (b & c) ^ (b & d) ^ (c & d), 0x8F1BBCDC), (lambda b, c, d: b ^ c ^ d, 0xCA62C1D6)][imm & 3]
        for i in range(4):   # round 0: W0E already holds E (SHA1NEXTE); then E_i = D_(i-1)
            A, B, C, D, E = (f(B, C, D) + rol(A, 5) + W[i] + E + K) & M, A, rol(B, 30), C, D
        return pack([D, C, B, A])
    if op == 'sha256msg1':
        s0 = lambda x: ror(x, 7) ^ ror(x, 18) ^ x >> 3
        w = a + [b[0]]
        return pack([(w[i] + s0(w[i + 1])) & M for i in range(4)])
    if op == 'sha256msg2':
        s1 = lambda x: ror(x, 17) ^ ror(x, 19) ^ x >> 10
        w16 = (a[0] + s1(b[2])) & M; w17 = (a[1] + s1(b[3])) & M; w18 = (a[2] + s1(w16)) & M; w19 = (a[3] + s1(w17)) & M
        return pack([w16, w17, w18, w19])
    if op == 'sha256rnds2':
        A, B, C, D, E, F, G, H = b[3], b[2], a[3], a[2], b[1], b[0], a[1], a[0]
        for wk in lanes(xmm0)[:2]:
            ch = (E & F) ^ (~E & G & M); maj = (A & B) ^ (A & C) ^ (B & C)
            S0 = ror(A, 2) ^ ror(A, 13) ^ ror(A, 22); S1 = ror(E, 6) ^ ror(E, 11) ^ ror(E, 25)
            t = (ch + S1 + wk + H) & M
            A, B, C, D, E, F, G, H = (t + maj + S0) & M, A, B, C, (t + D) & M, E, F, G
        return pack([F, E, B, A])
    raise ValueError(op)


def check(programs, expected):
    """Replays every program through `sha` from its initial memory; returns the mismatches with the reference's memory."""
    bad = []
    for p in programs:
        mem = bytes.fromhex(p['memory'][0]['data']); want = bytes.fromhex(expected[p['name']]['qemu']['memory'][0]['data'])
        rd = lambda a: int.from_bytes(mem[a - DATA:a - DATA + 16], 'little')
        x = [rd(a) for a in p['sha']['loads']]
        for k, (op, d, s, ea, imm) in enumerate(p['sha']['ops']):
            x[d] = sha(op, x[d], x[s] if s is not None else rd(ea), x[0], imm)
            got = int.from_bytes(want[RESULT - DATA + 16 * k:RESULT - DATA + 16 * k + 16], 'little')
            if got != x[d]: bad.append(f"{p['name']} #{k} {op} imm {imm}: model {x[d]:032x}, reference {got:032x}"); break
    return bad


def main():
    ap = argparse.ArgumentParser(); ap.add_argument('-o', '--output', type=pathlib.Path, required=True)
    ap.add_argument('--seed', type=int, default=20261103)
    ap.add_argument('--check', type=pathlib.Path, metavar='EXPECTED', help='replay OUT.json through the SDM model against EXPECTED')
    a = ap.parse_args(); rng = random.Random(a.seed)
    if a.check:
        exp = {r['name']: r for r in json.loads(a.check.read_text())['results']}
        progs = json.loads(a.output.read_text())['programs']
        bad = check(progs, exp)
        print(*bad[:10], f"{sum(len(p['sha']['ops']) for p in progs)} instructions in {len(progs)} programs, {len(bad)} mismatches", sep='\n')
        sys.exit(1 if bad else 0)
    progs = [program(f'sha-{op}-{i:03d}', rng, [op]) for op in OPS for i in range(4)]
    progs += [program(f'sha-rnds4-imm-{k:03d}', rng, ['sha1rnds4'], list(range(16 * k, 16 * k + 16))) for k in range(16)]
    progs += [program(f'sha-mix-{i:03d}', rng, OPS) for i in range(16)]
    clang = subprocess.run(['clang', '--version'], capture_output=True, text=True).stdout.splitlines()[0]
    a.output.write_text(json.dumps({'generator': {'script': 'tools/reference/gen_sha.py', 'seed': a.seed, 'assembler': clang,
        'layout': {'code': hex(g.CODE), 'data': hex(DATA), 'operands': hex(OPND), 'result': hex(RESULT), 'final': hex(FINAL)}},
        'programs': progs}, indent=1) + '\n')
    print(f'{len(progs)} programs -> {a.output}')

if __name__ == '__main__': main()
