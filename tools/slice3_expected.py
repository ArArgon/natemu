"""The expected final state of the slice-3 programs `kicks`, `coherence` and `contain` compared with QEMU
(tools/run_slice3_vm.py), written from the programs: each loop is run here on its own inputs (the hot loop, the REP
strings, the adversarial division loop, the coherence programs' ALU loop and run-time functions), and the handshake words
end at the values their writers store last. `expected(prog)` -> the state smp.parse_dump would give
(tools/bench/smp/expect.py).

Every program's state equals QEMU 11.1.2's locked answer (tools/test_slice3_expected.py, tools/reference/crosscheck/
slice3.json)."""
import functools, pathlib, subprocess, sys, tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / 'tools'), str(ROOT / 'tools' / 'reference'), str(ROOT / 'tools' / 'bench' / 'smp')]
import expect, programs as P, smp  # noqa: E402

M = (1 << 64) - 1
END = 0x46                                        # every program ends in `cmp r11, r11` or a `dec` to 0 from 1
rol = lambda x, k: ((x << k) | (x >> (64 - k))) & M if k % 64 else x
HOT_C = r'''#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
int main(int argc, char **argv) {
    uint64_t a = strtoull(argv[1], 0, 0), n = strtoull(argv[2], 0, 0), d = strtoull(argv[3], 0, 0);
    for (; n; n--) { a += n; d ^= a; d = d << 7 | d >> 57; }     /* add rax, rcx; xor rdx, rax; rol rdx, 7; dec rcx; jnz */
    printf("%llu %llu\n", (unsigned long long)a, (unsigned long long)d);
    return 0;
}
'''


@functools.lru_cache(None)
def _hot_tool():
    d = pathlib.Path(tempfile.mkdtemp(prefix='natemu-hot-'))
    (d / 'hot.c').write_text(HOT_C)
    subprocess.run(['cc', '-O2', '-o', str(d / 'hot'), str(d / 'hot.c')], check=True)
    return d / 'hot'


@functools.lru_cache(None)
def hot(rax, rcx, rdx):
    """The hot loop (run_slice3_vm.HOT) from these registers: (rax, rdx) at its end (rcx 0); 4e8 rounds: in C."""
    a, d = subprocess.run([str(_hot_tool()), str(rax), str(rcx), str(rdx)], capture_output=True, text=True, check=True).stdout.split()
    return int(a), int(d)


def hot_flags(rdx):
    """After `dec rcx` to 0 (ZF PF, no AF, SF, OF), CF kept from `rol rdx, 7`: the result's bit 0."""
    return END | (rdx & 1)


def r13_of(code, at):
    """The target of the last `lea r13, [rip + disp32]` (4c 8d 2d) in a body's code placed at `at`."""
    k = code.rfind(b'\x4c\x8d\x2d')
    return at + k + 7 + int.from_bytes(code[k + 3:k + 7], 'little', signed=True)


def regs_of(prog, i):
    return dict(zip(smp.REGS, (smp.num(x) for x in prog['cpus'][i]['gpr'])))


def _hot(prog):
    cpus = []
    for i in range(prog['ncpu']):
        r = regs_of(prog, i)
        a, d = hot(r['rax'], r['rcx'], r['rdx'])
        cpus.append({'rax': a, 'rcx': 0, 'rdx': d, 'rflags': hot_flags(d)})
    return expect.state(prog, cpus)


def _rep(prog):
    """rep_program's rounds on each CPU's buffer (rbx, 0x11000 bytes: two 32 KiB halves and a sentinel page)."""
    cpus, writes = [], []
    for i in range(prog['ncpu']):
        r = regs_of(prog, i)
        b = r['rbx']
        region = next(x for x in prog['memory'] if smp.num(x['addr']) == b)
        buf = bytearray(smp.region_bytes(region))
        r8, r9, r10 = r['r8'], r['r9'], r['r10']
        for _ in range(r['r12']):
            buf[0:0x8000] = r8.to_bytes(8, 'little') * 0x1000           # rep stosq
            buf[0x8000:0x10000] = buf[0:0x8000]                           # rep movsq up, then (DF = 1) down: the same copy
            buf[0x8000:0x10000] = buf[0:0x8000]
            n = next((k + 1 for k in range(0xfff) if buf[1 + k] != buf[k]), 0xfff)   # repe cmpsb: the first mismatch ends it
            rcx = 0xfff - n
            r9 = (r9 + rcx) & M
            rsi, rdi = b + 1 + n, b + n
            hit = buf.find(0x5a, 0x8000, 0x10000)                         # repne scasb for al = 0x5a over the high half
            scanned = 0x8000 if hit < 0 else hit - 0x8000 + 1
            rcx = 0x8000 - scanned
            r10 = (r10 + rcx) & M
            rdi = b + 0x8000 + scanned
            rax = (r8 & ~0xff) | 0x5a
            r8 = (r8 + 0x0101010101010101) & M
        cpus.append({'rax': rax, 'rcx': rcx, 'rdx': 0x0101010101010101, 'rsi': rsi, 'rdi': rdi, 'r8': r8, 'r9': r9, 'r10': r10, 'r12': 0})
        writes.append((b, bytes(buf)))
    return expect.state(prog, cpus, writes)


def _adversarial(prog):
    """Each round: r10 = rol(r10 + rcx, 3); (1:rcx) / rbx: quotient into r8, remainder into r9 (rax, rdx keep the last)."""
    code = bytes.fromhex(prog['bodies'][0]['code'])
    r13 = r13_of(code, smp.num(prog['bodies'][0]['addr']))
    cpus = []
    for i in range(prog['ncpu']):
        r = regs_of(prog, i)
        rcx, rbx, r8, r9, r10 = r['rcx'], r['rbx'], r['r8'], r['r9'], r['r10']
        while rcx:
            r10 = rol((r10 + rcx) & M, 3)
            q, m = divmod((1 << 64) | rcx, rbx)
            r8, r9 = (r8 + q) & M, (r9 + m) & M
            rcx -= 1
        cpus.append({'rax': q, 'rcx': 0, 'rdx': m, 'r8': r8, 'r9': r9, 'r10': r10, 'r13': r13})
    return expect.state(prog, cpus)


def _peer(prog):
    """vCPU 0: the flag, then the hot loop; vCPU 1: its delay loops, then rcx 64..1 stored at 0x110800 (the last 1)."""
    r = regs_of(prog, 0)
    a, d = hot(r['rax'], r['rcx'], r['rdx'])
    return expect.state(prog, [{'rax': a, 'rcx': 0, 'rdx': d, 'rflags': hot_flags(d)}, {'rcx': 0, 'rflags': END}],
                        [expect.q(0x120000, 1, 1), expect.q(0x110800, 1)])


fn = lambda r, rbx: (rbx * (7 * r + 3) + r) & M                    # programs.cc_function(r)
fold = lambda r12, rax: rol(r12, 7) ^ rax                            # programs.cc_call


def cc_loop(iters, r8=0, r9=0):
    """programs.cc_loop's rounds: (r8, r9) at its end."""
    for c in range(iters, 0, -1):
        r8 = (r8 + c) & M
        r9 = (r8 * 13) & M
        r8 = rol(r8 ^ r9, 3)
    return r8, r9


def _page(prog, k):
    """(address, initial bytes) of run-time code page k's captured region (from +0x10)."""
    at = P.CC_PAGE(k) + 0x10
    return next(smp.region_bytes(x) for x in prog['memory'] if smp.num(x['addr']) == at)


def _loop_iters(prog, k):
    code = _page(prog, k)[P.CC_FN - 0x10:]
    assert code[0] == 0xb9, 'cc_loop starts with mov ecx, imm32'
    return int.from_bytes(code[1:5], 'little')


def _cc(prog):
    kind, n = prog['params']['coherence'], prog['ncpu']
    body = lambda i: (smp.num(prog['bodies'][-n + i]['addr']), bytes.fromhex(prog['bodies'][-n + i]['code']))
    r13 = lambda i: r13_of(body(i)[1], body(i)[0])
    rbx = lambda i: regs_of(prog, i)['rbx']
    cpus = [{} for _ in range(n)]
    writes = []
    sync = lambda off, i, v: expect.q(P.CC_SYNC + off + 0x80 * i, v)
    code_at = lambda k, code: (P.CC_PAGE(k) + P.CC_FN, code)
    if kind.startswith('fresh-'):
        k = {'new': 0, 'reused': 1, 'cr3': 2}[kind[6:]]
        last = P.cc_function(P.CC_ROUNDS)
        cpus[0] = {'rax': int.from_bytes(last[16:24], 'little')}              # cc_write's last qword
        for i in range(1, n):
            r12 = 0
            for r in range(1, P.CC_ROUNDS + 1): r12 = fold(r12, fn(r, rbx(i)))
            cpus[i] = {'rax': fn(P.CC_ROUNDS, rbx(i)), 'r12': r12, 'r13': r13(i)}
            if kind == 'fresh-reused':
                cpus[i]['r8'] = rbx(i)
                writes.append(expect.q(P.CC_PAGE(k) + 0x800 + 8 * i, rbx(i)))
            writes.append(sync(0x400, i, P.CC_ROUNDS))
        writes += [sync(0, 0, P.CC_ROUNDS), code_at(k, last)]
    elif kind == 'rounds':
        R = 3 * n
        for i in range(n):
            r12 = 0
            for r in range(1, R + 1): r12 = fold(r12, fn(r, rbx(i)))
            cpus[i] = {'rax': fn(R, rbx(i)), 'r12': r12, 'r13': r13(i)}
            writes.append(sync(0x400, i, R))
        writes += [sync(0, 0, R), code_at(3, P.cc_function(R))]
    elif kind == 'data':
        r8, r9 = cc_loop(_loop_iters(prog, 4))
        cpus[0] = {'rax': P.CC_PAGE(4) + P.CC_FN, 'rcx': 0, 'r8': r8, 'r9': r9, 'r10': sum(rbx(j) for j in range(1, n)) & M, 'r13': r13(0)}
        writes += [sync(0, 0, 1), sync(0x800, 0, 1)] + [w for j in range(1, n) for w in (sync(0x400, j, 1), expect.q(P.CC_PAGE(4) + 0x800 + 8 * j, rbx(j)))]
    elif kind == 'rewrite':
        r8, r9 = cc_loop(_loop_iters(prog, 5))
        cpus[0] = {'rax': P.CC_PAGE(5) + P.CC_FN, 'rcx': 0, 'r8': r8, 'r9': r9, 'r13': r13(0)}
        code = _page(prog, 5)[P.CC_FN - 0x10:]
        size = len(code.rstrip(b'\0')) + 7 & ~7                               # the loop's qwords the writers copy (its padding is 0)
        for j in range(1, n): cpus[j] = {'rax': int.from_bytes(code[size - 8:size], 'little')}
        writes.append(sync(0x800, 0, 1))
    elif kind == 'mutual':
        for i in range(n):
            r8, r9 = cc_loop(_loop_iters(prog, 6 + i))
            cpus[i] = {'rax': P.CC_PAGE(6 + i) + P.CC_FN, 'rcx': 0, 'r8': r8, 'r9': r9, 'r13': r13(i)}
            writes.append(expect.q(P.CC_PAGE(6 + (i + 1) % n) + 0x800 + 8 * i, 1))   # the counter's last value
    elif kind in ('finish', 'finish-late'):
        cpus[0] = {'rbx': 77, 'rax': fn(1, 77), 'r12': fold(0, fn(1, 77)), 'r13': r13(0)}
        for j in range(1, n):
            cpus[j] = {'rcx': 0}
            writes += [expect.q(P.CC_PAGE(10) + 0x800 + 8 * j, rbx(j)), sync(0x800, j, 1)]
    elif kind == 'stale':
        patch = P.asm('imul rax, rbx, 10\nadd rax, 2\njmp r13')[:8]          # the last writer's one-qword patch (variant 2)
        cpus[0] = {'rbx': 5, 'rax': 5 * 10 + 2, 'r13': r13(0)}
        cpus[n - 1] = {'rax': int.from_bytes(patch, 'little')}
        writes += [sync(0x800, 0, 1), (P.CC_PAGE(11) + P.CC_FN, patch)]
        writes += [w for j in range(1, n) for w in (sync(0x400, j, 1), expect.q(P.CC_PAGE(11) + 0x800 + 8 * j, rbx(j)))]
    elif kind.startswith('unacked'):
        k = int(kind[7:])
        cpus[0] = {'rcx': 0, 'rax': fn(1, rbx(0)), 'r12': fold(0, fn(1, rbx(0))), 'r13': r13(0)}
        writes += [expect.q(P.CC_PAGE(12) + 0x800, rbx(k)), sync(0, 0, 1), sync(0x800, 0, 1)]
    else:
        raise KeyError(kind)
    return expect.state(prog, [dict(c, rflags=END) for c in cpus], writes)


def expected(prog):
    name = prog['name'].split('@')[0]
    if 'coherence' in prog.get('params', {}): return _cc(prog)
    return {'hot': _hot, 'rep': _rep, 'adversarial': _adversarial, 'peer-kick': _peer}[name](prog)
