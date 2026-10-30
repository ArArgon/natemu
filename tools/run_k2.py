#!/usr/bin/env python3
"""K2 checks: system instructions run from the decoded record kept in the code cache (boot-features plan, K2).

    tools/with-env python3 tools/run_k2.py --fexc FEXC --out DIR [--only cost,rdrand,xmc,coherence] [--n 100000] [--reps 3]
        [--xmc-rewrites N] [--host HOST --runtime RUNTIME]

- `cost`: microseconds per loop iteration of one system instruction (CLI, STI, PUSHFQ+POPFQ, SWAPGS, RDTSC, CPUID,
  WRMSR KERNEL_GS_BASE, FLD+FSTP m80) at CPL 0 on 1 vCPU, as the slope (t(2N) - t(N)) / N of the driver's wall time
  (start-up and compiles fall into the intercept); every run must reach its end. With the stats `decode_exits` per
  run: after K2 a loop's decode exits do not grow with N.
- `rdrand`: 4,000 RDRANDs in a loop (the pool holds 32): all succeed, about one entropy refill exit per 32, values differ.
- `xmc`: cross-modifying code at a system instruction, by the x86 protocol (deterministic: see `xmc`): vCPU 1 rewrites
  one byte of X (CLI <-> NOP, CLI <-> STI, SWAPGS <-> RDTSCP, FLD m80 <-> FILD m32, CLI -> INT3) paced by vCPU 0's
  progress, then raises a flag; vCPU 0, after CPUID, must execute the new instruction (observed) every time; INT3 must be
  taken. Block sizes default and 1.
- `coherence`: a kept record never outlives its bytes or mapping (own writes, remap + INVLPG or CR3, an alias write, a
  page-crossing instruction's second page, a peer's write before a serializing CPUID) and a kept BTS record under
  alignment checking (see `coherence`).

Writes DIR/k2.json; exit status 1 on any failure."""
import argparse, json, pathlib, statistics, subprocess, sys, time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / 'tools'), str(ROOT / 'tools' / 'reference'), str(ROOT / 'tools' / 'bench' / 'smp')]
import ref, run_slice1_vm as vm, programs as P  # noqa: E402

CODE, DATA, STACK = 0x100000, 0x300000, 0x3f0000
F80_ONE = bytes.fromhex('0000000000000080ff3f')
# xmc: vCPU 0 iterations between the peer's rewrites; RFLAGS.IF after X (old CLI: 0).
XMC_K = 100
OBS_IF = 'pushfq\npop rax\nshr eax, 9\nand eax, 1'
# name: (loop body, extra gpr settings); r8 counts the iterations.
COSTS = {
    'cli': ('cli', {}),
    'sti': ('sti', {}),
    'popf': ('pushfq\npopfq', {}),
    'swapgs': ('swapgs', {}),
    'rdtsc': ('rdtsc', {}),
    'cpuid': ('xor eax, eax\ncpuid', {}),
    'wrmsr': ('mov ecx, 0xc0000102\nxor eax, eax\nxor edx, edx\nwrmsr', {}),
    'fld_fstp_m80': ('fld tbyte ptr [rbx]\nfstp tbyte ptr [rbx]', {3: DATA}),
    'nop': ('nop', {}),
    # Reference: one resident round trip with no system instruction: a 128/64 division with RDX != 0 (fexc's helper,
    # `svc #DIV_U`; with RDX = 0 it divides inline).
    'div': ('mov edx, 1\nmov eax, 7\ndiv r9', {9: 2}),
}


def driver(a, text, work, extra=()):
    work.mkdir(parents=True, exist_ok=True)
    (work / 'program.txt').write_text(text)
    t0 = time.perf_counter()
    run = subprocess.run([a.host, a.runtime, 'run-program', a.fexc, work / 'program.txt', work, '--watchdog-ms', '600000', *map(str, extra)],
                         capture_output=True, text=True, timeout=900)
    dt = time.perf_counter() - t0
    return dt, (json.loads(run.stdout) if run.returncode == 0 else None), run.stderr[-1500:]


def gprs(values):
    g = [0] * 16
    g[4] = STACK
    for r, v in values.items(): g[r] = v
    return 'gpr ' + ' '.join(f'{v:#x}' for v in g)


def cost(a, report):
    failures, rows = [], {}
    for name, (body, regs) in COSTS.items():
        code = ref.assemble(f'1: {body}\ndec r8\njnz 1b')
        times, stats = {a.n: [], 2 * a.n: []}, {}
        for rep in range(a.reps + 1):
            for n in (a.n, 2 * a.n):
                text = (f'rip {CODE:#x}\nrflags 0x2\n{gprs({8: n, **regs})}\ncode {code.hex()}\ncpl 0\nif 0\n'
                        f'memory {DATA:#x} {F80_ONE.hex()}\nregion 0x3e0000 0x10000\n')
                dt, out, err = driver(a, text, a.out / 'cost' / f'{name}-{n}')
                if out is None or out['stop'] != 'reached' or int(out['gpr'][8], 0) != 0:
                    failures.append(f"cost {name} n={n}: {err if out is None else (out['stop'], out['reason'])}")
                    break
                if rep: times[n].append(dt)
                stats[n] = {k: out['stats'].get(k) for k in ('systems', 'decode_exits', 'requests', 'linker')}
        # K2: the decode exits of a loop do not grow with its iterations (one per translation).
        if len(stats) == 2 and stats[a.n]['decode_exits'] != stats[2 * a.n]['decode_exits']:
            failures.append(f"cost {name}: decode exits grow with N: {stats}")
        slopes = [1e6 * (t2 - t1) / a.n for t1, t2 in zip(times[a.n], times[2 * a.n])]
        rows[name] = {'us_per_iteration': statistics.median(slopes) if slopes else None, 'all': slopes, 'stats': stats}
        print(f"{name:14} {rows[name]['us_per_iteration'] or 0:9.4f} us/iter  {stats}", flush=True)
    report['cost'] = {'n': a.n, 'reps': a.reps, 'rows': rows, 'failures': failures}
    return failures


def rdrand(a, report):
    """RDRAND in a loop far beyond the pool (32 words): every one succeeds (CF = 1, counted in r10), the pool refilled by
    one host exit each time it runs dry (`entropy_exits`), and the values are not all equal (r11 ORs their XOR with the first)."""
    n, failures = 4000, []
    code = ref.assemble('rdrand r9\nmov r12, r9\n1: rdrand rax\nadc r10, 0\nxor rax, r12\nor r11, rax\ndec r8\njnz 1b')
    _, out, err = driver(a, f'rip {CODE:#x}\nrflags 0x2\n{gprs({8: n})}\ncode {code.hex()}\n', a.out / 'rdrand')
    if out is None: failures.append(f'rdrand: driver {err}')
    else:
        g = [int(v, 0) for v in out['gpr']]
        st = out['stats']
        if out['stop'] != 'reached' or g[10] != n or g[11] == 0 or not (n // 32 - 2 <= st['entropy_exits'] <= n // 32 + 2) or st['decode_exits'] > 2:
            failures.append(f"rdrand: stop {out['stop']} {out['reason']} successes {g[10]} of {n}, or {g[11]:#x}, stats {st['entropy_exits']} entropy, {st['decode_exits']} decodes")
        report['rdrand'] = {'n': n, 'successes': g[10], 'entropy_exits': st['entropy_exits'], 'decode_exits': st['decode_exits']}
        print(report['rdrand'], flush=True)
    report.setdefault('rdrand', {})['failures'] = failures
    return failures


def xmc(a, report):
    """Cross-modifying code at a system instruction, deterministic (the x86 protocol, SDM Vol.3 9.1.3): vCPU 0 loops
    `P; X; observe; ...` at CPL 0 and counts each iteration in ACK; vCPU 1 rewrites one byte of X (old <-> new), but only
    after ACK has advanced by `XMC_K` since its last rewrite, then raises FLAG (its rewrite count) and waits for vCPU 0 to
    acknowledge it in SEEN. vCPU 0, on seeing FLAG change, runs CPUID (serialising) and must then execute the instruction
    FLAG's parity names: the old one (observed 0) or the new one (observed 1); any other observation is a mismatch. Before
    the serialising instruction either is allowed. vCPU 0 runs until it has checked `--xmc-rewrites` rewrites, so both
    vCPUs' work follows the other's progress and every count is exact whatever the scheduling. INT3 <-> CLI: the first rewrite
    puts INT3 there, which vCPU 0 must take (#BP, RIP after it) right after its CPUID. Block sizes default and 1. Prints
    both vCPUs' counters (iterations, checks, mismatches; rewrites)."""
    asm, failures, runs = ref.assemble, [], []
    ack, flag, seen = 0x3e0000, 0x3e0008, 0x3e0010
    # name: (setup, P, X, observe (eax = 0 for the old instruction, 1 for the new), byte offset in X, (old, new))
    modes = {'cli-nop': ('', 'sti', 'cli', OBS_IF, 0, (0xfa, 0x90)),
             'cli-sti': ('', 'cli', 'cli', OBS_IF, 0, (0xfa, 0xfb)),
             'swapgs-rdtscp': ('mov ecx, 0xc0000103\nmov eax, 1\nxor edx, edx\nwrmsr\n', 'xor ecx, ecx', 'swapgs', 'mov eax, ecx', 2, (0xf8, 0xf9)),
             'fld80-fild': ('', '', 'fld tbyte ptr [rbx]', 'fstp qword ptr [rbx + 16]\nmov rax, qword ptr [rbx + 16]\ntest rax, rax\nsetz al\nmovzx eax, al', 1, (0x2b, 0x03)),
             'int3-cli': ('', '', 'cli', 'xor eax, eax', 0, (0xfa, 0xcc))}
    for mode, (setup, pre, x, observe, at, (own, other)) in modes.items():
        rewrites = 1 if mode == 'int3-cli' else a.xmc_rewrites
        c0, lab = asm(f"""xor r9d, r9d
xor r10d, r10d
xor r11d, r11d
xor r12d, r12d
{setup}
1: {pre}
x: {x}
{observe}
test r11, r11
jz 2f
inc r12
xor r11d, r11d
mov qword ptr [{seen:#x}], r10
cmp rax, r13
je 2f
inc r9
2: inc qword ptr [{ack:#x}]
mov rax, qword ptr [{flag:#x}]
cmp rax, r10
je 4f
mov r10, rax
mov r14, rbx
xor eax, eax
cpuid
mov rbx, r14
mov r13, r10
and r13, 1
mov r11d, 1
4: inc r8
cmp r12, {rewrites}
jb 1b""", labels=True)
        byte = CODE + lab['x'] + at
        c1 = asm(f"""xor ecx, ecx
1: mov r8, qword ptr [{ack:#x}]
2: mov rax, qword ptr [{ack:#x}]
sub rax, r8
cmp rax, {XMC_K}
jae 3f
pause
jmp 2b
3: inc ecx
mov al, {own:#x}
test cl, 1
jz 5f
mov al, {other:#x}
5: mov byte ptr [{byte:#x}], al
mov qword ptr [{flag:#x}], rcx
cmp ecx, {rewrites}
jae 9f
6: cmp qword ptr [{seen:#x}], rcx
je 1b
pause
jmp 6b
9: nop""")
        for block in (None, 1):
            # (The `memory` item on X's page makes it writable for the peer, at CPL 3.)
            text = (f'body {CODE:#x} {c0.hex()}\nbody 0x101000 {c1.hex()}\nmemory 0x100800 00\nmemory {DATA:#x} {F80_ONE.hex()}\nregion 0x3e0000 0x10000\n'
                    f'cpu\nrip {CODE:#x}\nrflags 0x2\ncpl 0\nif 0\n{gprs({3: DATA})}\n'
                    f'cpu\nrip 0x101000\nrflags 0x2\n{gprs({})}\n')
            _, out, err = driver(a, text, a.out / 'xmc' / f'{mode}-{block}', [block] if block else [])
            problems = []
            if out is None: problems.append(f'driver: {err}')
            else:
                c, peer = out['cpus']
                g = lambda cpu, r: int(cpu['gpr'][r], 0)
                iterations, mismatches, checks, done = g(c, 8), g(c, 9), g(c, 12), g(peer, 1)
                bp = c['stop'] == 'guest_fault' and [int(v, 0) for v in c['fault']][:3] == [3, 0, 0] and int(c['fault'][3], 0) == CODE + lab['x'] + 1
                if mode == 'int3-cli':
                    if not bp: problems.append(f"vCPU 0 stop {c['stop']} {c['reason']} fault {c['fault']} rip {c['rip']}: the INT3 was not taken after CPUID")
                else:
                    if c['stop'] != 'reached': problems.append(f"vCPU 0 stop {c['stop']} {c['reason']} fault {c['fault']} rip {c['rip']}")
                    if mismatches: problems.append(f'{mismatches} of {checks} checks ran the old instruction after the serialising CPUID')
                    if (checks, done) != (rewrites, rewrites): problems.append(f'{checks} checks, {done} rewrites, {rewrites} wanted')
                if peer['stop'] != 'reached': problems.append(f"vCPU 1 stop {peer['stop']} {peer['reason']}")
                runs.append({'mode': mode, 'block': block, 'stop': c['stop'], 'bp': bp, 'iterations': iterations, 'checks': checks,
                             'mismatches': mismatches, 'rewrites': done,
                             **{k: c['stats'].get(k) for k in ('systems', 'decode_exits', 'steps', 'invalidations')}})
                print(json.dumps(runs[-1]), flush=True)
            if problems: failures.append(f"xmc {mode} block={block}: {'; '.join(problems)}")
    report['xmc'] = {'runs': runs, 'failures': failures}
    return failures

def coherence(a, report):
    """A kept record never outlives the bytes or the mapping it was decoded from (the K2 review's cases). Each case runs
    the instruction at X from its kept record (two visits: its old kind decoded once), changes X the architectural way,
    and the next visit must run the new instruction: A the vCPU's own write from another page, B its own write on X's
    page in a loop, C a remap of X's linear page with INVLPG, or with a CR3 reload, D a write through another linear
    alias of X's frame, E a SWAPGS crossing 0x200000 whose second page alone is remapped (to RDTSCP's byte) and INVLPGed,
    F a peer's rewrite, then a flag, then CPUID (serializing). Also BTS under alignment checking (a kept BT_ALIGNED
    record): aligned five times (stepped plain each time), then misaligned: #AC(0) at the BTS; all aligned: completes.
    Block sizes default and 1."""
    asm, failures, runs = ref.assemble, [], []
    I = 1                                           # "CPU 1"'s table slots: 0x200000.. stays free for case E
    TA, LEAF = P.SYS_T(I, 0), P.LEAF
    pte = lambda va: P.sys_leaf(I, 0, va)
    tables = lambda over: [(int(t['addr'], 16), bytes.fromhex(t['data'])) for t in P.sys_tables(I, 0, 1, over)]
    CODE1, X, JMP = 0x110000, 0x112000, asm('jmp r13')
    go = lambda at, k: f'lea r13, [rip + v{k}]\nmov rax, {at:#x}\njmp rax\nv{k}:\n'
    cr3 = f'mov rax, {TA:#x}\nmov cr3, rax\n'
    L, F1, F2, ALIAS, FLAG = P.SYS_D(I), P.SYS_D(I) + 0x1000, P.SYS_D(I) + 0x2000, P.SYS_D(I) + 0x3000, P.SYS_D(I) + 0x4000
    XC, P2, P2NEW = 0x1ffffe, 0x200000, 0x230000
    IF = lambda c: int(c['rflags'], 16) & 0x200 != 0
    gpr = lambda c, n: int(c['gpr'][n], 16)
    cases = [  # name, vCPUs [(address, code, items)], memory, judge, (old kind, decoded once)
        ('A-own-write-other-page', [(CODE1, asm(cr3 + go(X, 0) + go(X, 1) + f'mov byte ptr [{X:#x}], 0xfb\n' + go(X, 2)), [])],
         tables({}) + [(X, asm('cli') + JMP)], IF, 0x40),
        ('B-own-write-same-page', [(CODE1, asm(cr3 + 'mov ecx, 4\n1: x: cli\ncmp ecx, 2\njne 2f\nmov byte ptr [rip + x], 0xfb\n2: dec ecx\njnz 1b\n'), [])],
         tables({}), IF, 0x40),
        *[(f'C-remap-{n}', [(CODE1, asm(cr3 + go(L, 0) + go(L, 1) + f'mov rax, {F2 | LEAF:#x}\nmov qword ptr [{pte(L):#x}], rax\n' + change + go(L, 2)), [])],
           tables({L: F1 | LEAF}) + [(F1, asm('cli') + JMP), (F2, asm('sti') + JMP)], IF, 0x40)
          for n, change in (('invlpg', f'mov rax, {L:#x}\ninvlpg [rax]\n'), ('cr3', cr3))],
        ('D-alias-write', [(CODE1, asm(cr3 + go(X, 0) + go(X, 1) + f'mov byte ptr [{ALIAS:#x}], 0xfb\n' + go(X, 2)), [])],
         tables({ALIAS: X | LEAF}) + [(X, asm('cli') + JMP)], IF, 0x40),
        ('E-crossing-second-page', [(CODE1, asm(cr3 + go(XC, 0) + go(XC, 1) + f'mov rax, {P2NEW | LEAF:#x}\nmov qword ptr [{pte(P2):#x}], rax\n'
                                                f'mov rax, {P2:#x}\ninvlpg [rax]\nmov rcx, 0x5555\n' + go(XC, 2)
                                                + 'mov r10, rcx\nmov ecx, 0xc0000101\nrdmsr\nmov r9, rax\n'), ['gsbase 0x1234'])],
         tables({}) + [(pte(0x1ff000), (0x1ff000 | LEAF).to_bytes(8, 'little')), (0x1ff000, bytes(0xffe) + bytes.fromhex('0f01')),
                       (P2, bytes.fromhex('f8') + JMP), (P2NEW, bytes.fromhex('f9') + JMP)],
         lambda c: gpr(c, 9) == 0x1234 and gpr(c, 10) != 0x5555, 0x22),     # two SWAPGS, then RDTSCP (RCX := TSC_AUX)
        *[(f'F-peer-then-cpuid-{r}', [(CODE1, asm(cr3 + f'1:\nlea r13, [rip + 2f]\nmov rax, {X:#x}\njmp rax\n2: inc qword ptr [{FLAG + 8:#x}]\n'
                                                  f'cmp byte ptr [{FLAG:#x}], 0\nje 1b\nxor eax, eax\ncpuid\n' + go(X, 9)), []),
                                       (CODE1 + 0x4000, asm(cr3 + f'1: cmp qword ptr [{FLAG + 8:#x}], 1000\njb 1b\nmov byte ptr [{X:#x}], 0xfb\n'
                                                            f'mov byte ptr [{FLAG:#x}], 1\n'), [])],
           tables({}) + [(X, asm('cli') + JMP), (FLAG, bytes(16))], IF, 0x40) for r in range(3)],
    ]
    for block in (None, 1):
        for name, cpus, memory, judge, old in cases:
            lines = ['identity'] + [f'body {at:#x} {code.hex()}' for at, code, _ in cpus] + [f'memory {m:#x} {b.hex()}' for m, b in memory]
            for at, _, items in cpus:
                lines += ['cpu', f'rip {at:#x}', 'cpl 0', 'if 0', *items]
            _, out, err = driver(a, '\n'.join(lines) + '\n', a.out / 'coherence' / f'{name}-{block}', [block] if block else [])
            c = out and (out['cpus'] if 'cpus' in out else [out])[0]
            decodes = c and c['stats']['decodes'].get(f'{old:#x}')
            ok = bool(c) and c['stop'] == 'reached' and judge(c) and decodes == 1
            runs.append({'case': name, 'block': block, 'ok': ok, 'stop': c and c['stop'], 'old_decodes': decodes,
                         **({k: c['stats'][k] for k in ('systems', 'decode_exits', 'steps', 'invalidations')} if c else {'error': err})})
            print(json.dumps(runs[-1]), flush=True)
            if not ok: failures.append(f'coherence {name} block={block}: {runs[-1]}')
        # BTS under alignment checking: CR0.AM, then CPL 3 with RFLAGS.AC (the driver's switch hook).
        head = asm('mov rax, cr0\nbts rax, 18\nmov cr0, rax\nxor eax, eax\nxor edx, edx')
        for name, odd_at in (('bts-ac-odd-last', 1), ('bts-ac-aligned', 0)):
            body, labels = asm(f'mov ecx, 6\n1: cmp ecx, {odd_at}\njne 2f\nmov edx, 1\n2: bt_: bts dword ptr [rbx+rdx+4], ecx\ndec ecx\njnz 1b\nmov eax, [rbx+4]\n', labels=True)
            code, base = head + b'\x90' + body, 0x110000
            bt = base + len(head) + 1 + labels['bt_']
            text = (f'cpl 0\nrflags 0x40000\nrip {base:#x}\nswitch {base + len(head):#x} 3\nregion 0x200000 0x2000\n'
                    f'gpr 0 0 0 0x200000 0x201800 {" ".join(["0"] * 11)}\ncode {code.hex()}\n')
            _, out, err = driver(a, text, a.out / 'coherence' / f'{name}-{block}', [block] if block else [])
            st = out and out['stats']
            if odd_at:   # #AC(0) delivered at the BTS, the record (decoded once) reused for every visit
                ok = bool(out) and out['stop'] == 'guest_fault' and [int(v, 0) for v in out['fault']] == [17, 0, 0, bt]
            else:
                ok = bool(out) and out['stop'] == 'reached' and int(out['gpr'][0], 0) == 0x7e
            ok = ok and st['decodes'].get('0x45') == 1 and st['systems'] >= 6
            runs.append({'case': name, 'block': block, 'ok': ok, 'stop': out and out['stop'], **({'fault': out['fault'], 'systems': st['systems'],
                         'decodes': st['decodes']} if out else {'error': err})})
            print(json.dumps(runs[-1]), flush=True)
            if not ok: failures.append(f'coherence {name} block={block}: {runs[-1]}')
    report['coherence'] = {'runs': runs, 'failures': failures}
    return failures


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--fexc', type=pathlib.Path, required=True)
    ap.add_argument('--out', type=pathlib.Path, required=True)
    ap.add_argument('--only', type=lambda s: s.split(','), default=['cost', 'rdrand', 'xmc', 'coherence'])
    ap.add_argument('--n', type=int, default=100_000)
    ap.add_argument('--reps', type=int, default=3)
    ap.add_argument('--xmc-rewrites', type=int, default=2000, help='xmc: rewrites, each checked (one per XMC_K or more iterations of vCPU 0)')
    ap.add_argument('--host', type=pathlib.Path)
    ap.add_argument('--runtime', type=pathlib.Path)
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    if not (a.host and a.runtime): a.host, a.runtime = vm.build()
    report, failures = {}, []
    if 'cost' in a.only: failures += cost(a, report)
    if 'rdrand' in a.only: failures += rdrand(a, report)
    if 'xmc' in a.only: failures += xmc(a, report)
    if 'coherence' in a.only: failures += coherence(a, report)
    (a.out / 'k2.json').write_text(json.dumps(report, indent=1) + '\n')
    for f in failures: print('FAIL', f)
    sys.exit(1 if failures else 0)


if __name__ == '__main__': main()
