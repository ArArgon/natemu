#!/usr/bin/env python3
"""K2 checks: system instructions run from the decoded record kept in the code cache (boot-features plan, K2).

    tools/with-env python3 tools/run_k2.py --fexc FEXC --out DIR [--only cost,rdrand,xmc,coherence,rollback,nopruns,tsc,swapgs,sysfast] [--n 100000] [--reps 3]
        [--xmc-rewrites N] [--host HOST --runtime RUNTIME]

- `cost`: microseconds per loop iteration of one system instruction (CLI, STI, PUSHFQ+POPFQ, SWAPGS, RDTSC, CPUID,
  WRMSR KERNEL_GS_BASE, FLD+FSTP m80) at CPL 0 on 1 vCPU, as the slope (t(2N) - t(N)) / N of the driver's wall time
  (start-up and compiles fall into the intercept); every run must reach its end. With the stats `decode_exits` per
  run: after K2 a loop's decode exits do not grow with N. Perf chunk 17: `syscall`, one SYSCALL -> kernel -> SYSRET round trip
  from a CPL-3 loop, the kernel side shaped as Linux's (`endbr64; swapgs; ...; swapgs; 7-byte NOP; sysretq`).
- `rdrand`: 4,000 RDRANDs in a loop (the pool holds 32): all succeed, about one entropy refill exit per 32, values differ.
- `xmc`: cross-modifying code at a system instruction, by the x86 protocol (deterministic: see `xmc`): vCPU 1 rewrites
  one byte of X (CLI <-> NOP, CLI <-> STI, SWAPGS <-> RDTSCP, FLD m80 <-> FILD m32, CLI -> INT3) paced by vCPU 0's
  progress, then raises a flag; vCPU 0, after CPUID, must execute the new instruction (observed) every time; INT3 must be
  taken. Block sizes default and 1.
- `coherence`: a kept record never outlives its bytes or mapping (own writes, remap + INVLPG or CR3, an alias write, a
  page-crossing instruction's second page, a peer's write before a serializing CPUID) and a kept BTS record under
  alignment checking (see `coherence`).
- `rollback` (perf chunk 12): SYSRET with a non-canonical RCX and WRGSBASE with a non-canonical value fault with the
  state unchanged (see `rollback`).
- `nopruns` (perf chunk 12): a kept run of no-operation instructions crossing into a page made not present, then remapped,
  and rewritten by a peer vCPU, at 1-4 vCPUs (see `nopruns`).
- `tsc` (perf chunk 15): RDTSC translated inline at CPL 0 agrees with the resident's RDTSCP (one clock), follows a WRMSR of
  the TSC, and never runs backwards across vCPUs (see `tsc`).
- `swapgs` (perf chunk 17): SWAPGS translated inline at CPL 0 exchanges GS.base and IA32_KERNEL_GS_BASE as the resident's
  does, across a fault, precise stops and interrupts, and CPL 3 can neither read nor forge the kernel's value (see `swapgs`).
- `sysfast` (perf chunk 22): kept SYSCALL/SYSRET run by the resident's fast path stay exact at both boundaries: an interrupt
  SYSRET makes takeable, a TF it loads, instruction breakpoints at IA32_LSTAR and the return RIP, a data breakpoint before
  the SYSCALL, EFER.SCE cleared, a non-canonical RCX, interrupts at random times (see `sysfast`); and (see `sysfast_more`)
  an A-bit write into the code's own page after a CR3 reload, a user-side spin only an interrupt ends, a peer's code
  write, the LAPIC timer, a REP MOVSB at the return RIP, NMI blocking across SYSRET, a division (#DE or not) after SYSRET.

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
    # Perf chunk 15: STI mid-block at CPL 0 is translated inline (the row above starts its block: the resident's).
    'nop_sti': ('nop\nsti', {}),
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
    # Perf chunk 17: a SYSCALL -> kernel -> SYSRET round trip (`syscall_program`).
    'syscall': (None, {}),
}
USER_GS, KERNEL_GS, KERNEL_RIP = 0x2a0000, 0x2b0000, 0x200000
# Linux's entry_SYSCALL_64 and its SYSRET exit, in shape: ENDBR64; SWAPGS; the user RSP saved and the kernel's loaded through
# GS; ...; SWAPGS; a 7-byte NOP; SYSRETQ.
LINUX_KERNEL = ('endbr64\nswapgs\nmov gs:[16], rsp\nmov rsp, gs:[24]\npush rcx\npop rcx\nmov rsp, gs:[16]\nswapgs\n'
                '.byte 0x0f,0x1f,0x80,0,0,0,0\nsysretq')


def wrmsr(index, value):
    return f'mov ecx, {index:#x}\nmov eax, {value & 0xffffffff:#x}\nmov edx, {value >> 32:#x}\nwrmsr\n'


def syscall_program(user, kernel, regs, extra=''):
    """A CPL-0 start that sets EFER.SCE, STAR, LSTAR (KERNEL_RIP), FMASK (Linux's 0x47700), GS.base (USER_GS) and
    IA32_KERNEL_GS_BASE (KERNEL_GS, whose quadword 24 holds the kernel stack), then SYSRETs to `user` (CPL 3, IF = 1);
    `kernel` at LSTAR."""
    code = ref.assemble('mov ecx, 0xc0000080\nrdmsr\nor eax, 1\nwrmsr\n' + wrmsr(0xc0000081, 0x23 << 48 | 0x10 << 32) + wrmsr(0xc0000082, KERNEL_RIP)
                        + wrmsr(0xc0000084, 0x47700) + wrmsr(0xc0000101, USER_GS) + wrmsr(0xc0000102, KERNEL_GS)
                        + f'mov qword ptr [{KERNEL_GS + 24}], {STACK - 0x100}\nlea rcx, [rip + user]\nmov r11d, 0x202\nsysretq\nuser:\n' + user)
    return (f'rip {CODE:#x}\nrflags 0x2\n{gprs(regs)}\ncode {code.hex()}\nbody {KERNEL_RIP:#x} {ref.assemble(kernel).hex()}\ncpl 0\nif 0\n'
            f'memory {USER_GS:#x} {bytes(32).hex()}\nmemory {KERNEL_GS:#x} {bytes(32).hex()}\nregion 0x3e0000 0x10000\n{extra}')


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
        code = ref.assemble(f'1: {body}\ndec r8\njnz 1b') if body else None
        times, stats = {a.n: [], 2 * a.n: []}, {}
        for rep in range(a.reps + 1):
            for n in (a.n, 2 * a.n):
                text = (f'rip {CODE:#x}\nrflags 0x2\n{gprs({8: n, **regs})}\ncode {code.hex()}\ncpl 0\nif 0\n'
                        f'memory {DATA:#x} {F80_ONE.hex()}\nregion 0x3e0000 0x10000\n') if body else syscall_program('1: syscall\ndec r8\njnz 1b', LINUX_KERNEL, {8: n})
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
    alias of X's frame, E a SWAPGS crossing 0x200000 whose second page alone is remapped (to RDTSCP's byte) and INVLPGed
    (perf chunk 17: with a REX.W prefix, which keeps it the resident's at CPL 0: fexc translates only the plain form inline),
    F a peer's rewrite, then a flag, then CPUID (serializing), G (perf chunk 12) a run of NOPs kept as one record, its
    second NOP rewritten by the vCPU's own store. Also BTS under alignment checking (a kept BT_ALIGNED
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
    XC, P2, P2NEW = 0x1ffffd, 0x200000, 0x230000
    IF = lambda c: int(c['rflags'], 16) & 0x200 != 0
    gpr = lambda c, n: int(c['gpr'][n], 16)
    # Perf chunk 15: the probed CLI/STI carry a 66 prefix (ignored), which keeps them the resident's at CPL 0 (fexc translates
    # only the one-byte forms inline); the rewrites change their opcode byte, the second.
    cli, sti = bytes.fromhex('66fa'), bytes.fromhex('66fb')
    cases = [  # name, vCPUs [(address, code, items)], memory, judge, (old kind, decoded once)
        ('A-own-write-other-page', [(CODE1, asm(cr3 + go(X, 0) + go(X, 1) + f'mov byte ptr [{X + 1:#x}], 0xfb\n' + go(X, 2)), [])],
         tables({}) + [(X, cli + JMP)], IF, 0x40),
        ('B-own-write-same-page', [(CODE1, asm(cr3 + 'mov ecx, 4\n1: .byte 0x66\nx: cli\ncmp ecx, 2\njne 2f\nmov byte ptr [rip + x], 0xfb\n2: dec ecx\njnz 1b\n'), [])],
         tables({}), IF, 0x40),
        *[(f'C-remap-{n}', [(CODE1, asm(cr3 + go(L, 0) + go(L, 1) + f'mov rax, {F2 | LEAF:#x}\nmov qword ptr [{pte(L):#x}], rax\n' + change + go(L, 2)), [])],
           tables({L: F1 | LEAF}) + [(F1, cli + JMP), (F2, sti + JMP)], IF, 0x40)
          for n, change in (('invlpg', f'mov rax, {L:#x}\ninvlpg [rax]\n'), ('cr3', cr3))],
        ('D-alias-write', [(CODE1, asm(cr3 + go(X, 0) + go(X, 1) + f'mov byte ptr [{ALIAS + 1:#x}], 0xfb\n' + go(X, 2)), [])],
         tables({ALIAS: X | LEAF}) + [(X, cli + JMP)], IF, 0x40),
        ('E-crossing-second-page', [(CODE1, asm(cr3 + go(XC, 0) + go(XC, 1) + f'mov rax, {P2NEW | LEAF:#x}\nmov qword ptr [{pte(P2):#x}], rax\n'
                                                f'mov rax, {P2:#x}\ninvlpg [rax]\nmov rcx, 0x5555\n' + go(XC, 2)
                                                + 'mov r10, rcx\nmov ecx, 0xc0000101\nrdmsr\nmov r9, rax\n'), ['gsbase 0x1234'])],
         tables({}) + [(pte(0x1ff000), (0x1ff000 | LEAF).to_bytes(8, 'little')), (0x1ff000, bytes(0xffd) + bytes.fromhex('480f01')),
                       (P2, bytes.fromhex('f8') + JMP), (P2NEW, bytes.fromhex('f9') + JMP)],
         lambda c: gpr(c, 9) == 0x1234 and gpr(c, 10) != 0x5555, 0x22),     # two SWAPGS, then RDTSCP (RCX := TSC_AUX)
        *[(f'F-peer-then-cpuid-{r}', [(CODE1, asm(cr3 + f'1:\nlea r13, [rip + 2f]\nmov rax, {X:#x}\njmp rax\n2: inc qword ptr [{FLAG + 8:#x}]\n'
                                                  f'cmp byte ptr [{FLAG:#x}], 0\nje 1b\nxor eax, eax\ncpuid\n' + go(X, 9)), []),
                                       (CODE1 + 0x4000, asm(cr3 + f'1: cmp qword ptr [{FLAG + 8:#x}], 1000\njb 1b\nmov byte ptr [{X + 1:#x}], 0xfb\n'
                                                            f'mov byte ptr [{FLAG:#x}], 1\n'), [])],
           tables({}) + [(X, cli + JMP), (FLAG, bytes(16))], IF, 0x40) for r in range(3)],
        # Perf chunk 12: a run of NOPs before CLI, kept as one NOP_RUN record; its own write turns the second NOP into STC:
        # the next visit runs STC (CF = 1), not the kept run.
        ('G-nop-run-own-write', [(CODE1, asm(cr3 + go(X, 0) + go(X, 1) + f'mov byte ptr [{X + 1:#x}], 0xf9\nclc\n' + go(X, 2)), [])],
         tables({}) + [(X, asm('nop\nnop\nxchg ax, ax') + cli + JMP)], lambda c: int(c['rflags'], 16) & 1 == 1, 0x79),
    ]
    for block in (None, 1):
        for name, cpus, memory, judge, old in cases:
            lines = ['identity'] + [f'body {at:#x} {code.hex()}' for at, code, _ in cpus] + [f'memory {m:#x} {b.hex()}' for m, b in memory]
            for at, _, items in cpus:
                lines += ['cpu', f'rip {at:#x}', 'cpl 0', 'if 0', *items]
            _, out, err = driver(a, '\n'.join(lines) + '\n', a.out / 'coherence' / f'{name}-{block}', [block] if block else [])
            c = out and (out['cpus'] if 'cpus' in out else [out])[0]
            decodes = c and c['stats']['decodes'].get(f'{old:#x}')
            # (At block size 1 no block holds more than one instruction, so no NOP_RUN is made: its CF is judged alone.)
            ok = bool(c) and c['stop'] == 'reached' and judge(c) and decodes == (None if block == 1 and old == 0x79 else 1)
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


def rollback(a, report):
    """Perf chunk 12: SYSRET and WRGSBASE make every check before their first write (their transactions keep no copy of
    the state to restore), so a fault at the check leaves the state as it was (SDM Vol.3 7.5: a fault reports the state
    before the instruction; Vol.2 SYSRET: #GP(0) at CPL 0 for a non-canonical RCX, before any change; WRGSBASE: #GP(0) for
    a non-canonical value, the base unchanged). At CPL 0 with the program's own IDT, whose #GP handler records the frame's
    RIP (r10), CS (r13), RFLAGS (r11) and error code (r12) and the GS base (RDGSBASE, r9), then ends the run; RFLAGS (r14) and
    CS (rbp) are read before the instruction, the GS base starts at 0x1234. SYSRET: EFER.SCE set, RCX = 2^47 (kept), R11
    with OF, SF, ZF, AF, PF and CF (the RFLAGS it would load). Block sizes default and 1."""
    asm, failures, runs = ref.assemble, [], []
    idt, idtr, noncanonical = 0x300000, 0x300800, 1 << 47
    cases = {'sysret-noncanonical': f'mov ecx, 0xc0000080\nrdmsr\nor eax, 1\nwrmsr\nmov rcx, {noncanonical:#x}\nmov r11d, 0x8d7\nxor edx, edx\npushfq\npop r14\nx: sysretq',
             'wrgsbase-noncanonical': f'mov rax, {noncanonical:#x}\nxor edx, edx\npushfq\npop r14\nx: wrgsbase rax'}
    for name, body in cases.items():
        code, lab = asm(f'lidt [{idtr:#x}]\nmov eax, cs\nmov rbp, rax\n{body}\nmov r9, 0xbad\njmp done\n'
                        'h13: pop r12\nmov r10, [rsp]\nmov r13, [rsp + 8]\nmov r11, [rsp + 16]\nrdgsbase r9\ndone:\n', labels=True)
        h = CODE + lab['h13']
        gate = (h & 0xffff | 0x08 << 16 | 0x8e << 40 | (h >> 16 & 0xffff) << 48).to_bytes(8, 'little') + (h >> 32).to_bytes(8, 'little')
        text = (f'rip {CODE:#x}\nrflags 0x2\n{gprs({})}\ncode {code.hex()}\ncpl 0\nif 0\nfsgsbase 1\ngsbase 0x1234\nregion 0x3e0000 0x10000\n'
                f'memory {idt:#x} {(bytes(16 * 13) + gate).hex()}\nmemory {idtr:#x} {((14 * 16 - 1).to_bytes(2, "little") + idt.to_bytes(8, "little")).hex()}\n')
        for block in (None, 1):
            _, out, err = driver(a, text, a.out / 'rollback' / f'{name}-{block}', [block] if block else [])
            g = out and [int(v, 0) for v in out['gpr']]
            # (register, value the SDM requires; the frame's RFLAGS without RF, which a fault's frame may carry)
            want = g and {'frame RIP': (g[10], CODE + lab['x']), 'frame CS': (g[13], g[5]), 'frame RFLAGS': (g[11] & ~0x10000, g[14]),
                          'error code': (g[12], 0), 'GS base': (g[9], 0x1234), **({'RCX': (g[1], noncanonical)} if name.startswith('sysret') else {})}
            ok = bool(out) and out['stop'] == 'reached' and all(x == y for x, y in want.values())
            runs.append({'case': name, 'block': block, 'ok': ok, 'stop': out and out['stop'],
                         **({k: [hex(x), hex(y)] for k, (x, y) in want.items()} if g else {'error': err})})
            print(json.dumps(runs[-1]), flush=True)
            if not ok: failures.append(f'rollback {name} block={block}: {runs[-1]}')
    report['rollback'] = {'runs': runs, 'failures': failures}
    return failures


def nopruns(a, report):
    """Perf chunk 12: NOP runs (one kept record for a run of no-operation instructions before a system instruction) across
    a page and across vCPUs, at 1-4 vCPUs (each its own run, one shared table: `coherence`'s) and block sizes default and 1.
    Judged by the SDM (natemu only: the outcome is fixed by the architecture, so no QEMU run is needed to judge it).
    - `cross`: vCPU i's run `nop; nop; nop; nop dword [rax]` from A - 4 (A = 0x2a1000 + 0x4000i), the 0F 1F 00 crossing into
      page A, then `nop; cli; jmp r13` (CLI with a 66 prefix since perf chunk 15: the resident's), run twice (kept); A made not present (PTE P = 0) and INVLPGed: the next visit runs
      the three NOPs and takes #PF at the crossing NOP (SDM Vol.3 4.7: RIP A - 1, CR2 = A, error code 0x10: a supervisor
      fetch, NXE), whose handler restores the PTE and returns (the run completes); a visit more; then A remapped (PTE to
      another frame whose third byte is STC, not NOP) and INVLPGed: after CLC the next visit sets CF (the new bytes).
    - `peer`: vCPU j visits its run X_j (`nop; nop; xchg ax, ax; cli; jmp r13`, the 66-prefixed CLI) in a loop until vCPU (j + 1) % N has made
      its second NOP STC and raised j's flag (only after j's 1,000th visit), and j has done the same for vCPU (j - 1) % N;
      then CPUID (serializing), CLC and one visit: CF must be 1 (SDM Vol.3 9.1.3, cross-modifying code; at N = 1 the
      vCPU's own write).
    At the default block size the run must have been kept as a NOP_RUN record (decoded at least once)."""
    asm, failures, runs = ref.assemble, [], []
    I, LEAF = 1, P.LEAF
    pte = lambda va: P.sys_leaf(I, 0, va)
    tables = [(int(t['addr'], 16), bytes.fromhex(t['data'])) for t in P.sys_tables(I, 0, 1, {})]
    cr3 = f'mov rax, {P.SYS_T(I, 0):#x}\nmov cr3, rax\n'
    go = lambda at, k: f'lea r13, [rip + v{k}]\nmov rax, {at:#x}\njmp rax\nv{k}:\n'
    code_at, handler, idt, idtr = (lambda i: 0x110000 + 0x2000 * i), 0x118000, 0x2b0000, 0x2b0f00
    # #PF (IST 1: the driver's TSS gives each vCPU its own stack): error code, frame RIP and CR2 recorded, the PTE at r15
    # restored from rbx, count in r14.
    pf = asm('pop r12\nmov r10, [rsp]\nmov r11, cr2\ninc r14\nmov [r15], rbx\ninvlpg [r11]\niretq')
    gate = (handler & 0xffff | 0x08 << 16 | 1 << 32 | 0x8e << 40 | (handler >> 16 & 0xffff) << 48).to_bytes(8, 'little') + (handler >> 32).to_bytes(8, 'little')
    idt_items = [(idt, bytes(16 * 14) + gate), (idtr, (15 * 16 - 1).to_bytes(2, 'little') + idt.to_bytes(8, 'little'))]
    page = lambda i: 0x2a1000 + 0x4000 * i                        # A; its alternative frame A + 0x1000
    tail, tail_new = bytes.fromhex('1f009066fa41ffe5'), bytes.fromhex('1f00f966fa41ffe5')   # (66 fa: the resident's CLI, chunk 15)
    x_at, count, flag = (lambda j: 0x11a000 + 0x1000 * j), (lambda j: 0x2b8000 + 0x40 * j), (lambda j: 0x2b8008 + 0x40 * j)
    run = asm('nop\nnop\nxchg ax, ax') + bytes.fromhex('66fa') + asm('jmp r13')

    def cross(i, n):
        A = page(i)
        code = asm(cr3 + f'lidt [{idtr:#x}]\nmov r15, {pte(A):#x}\nmov rbx, {A | LEAF:#x}\nxor r14d, r14d\nxor r9d, r9d\n'
                   + go(A - 4, 0) + go(A - 4, 1) + f'mov rax, {(A | LEAF) & ~1:#x}\nmov [r15], rax\nmov rax, {A:#x}\ninvlpg [rax]\n'
                   + go(A - 4, 2) + go(A - 4, 3) + f'mov rax, {(A + 0x1000) | LEAF:#x}\nmov [r15], rax\nmov rax, {A:#x}\ninvlpg [rax]\nclc\n'
                   + go(A - 4, 4) + 'setc r9b\n')
        memory = [(A - 4, bytes.fromhex('9090900f')), (A, tail), (A + 0x1000, tail_new)]
        judge = lambda g: {'r10 (#PF RIP)': (g[10], A - 1), 'r11 (CR2)': (g[11], A), 'r12 (error code)': (g[12], 0x10), 'r14 (#PFs)': (g[14], 1), 'r9 (CF)': (g[9], 1)}
        return code, memory, judge

    def peer(j, n):
        prev = (j - 1) % n
        code = asm(cr3 + 'xor r9d, r9d\nxor r10d, r10d\n1:\n' + go(x_at(j), 0) + f'inc qword ptr [{count(j):#x}]\ntest r10, r10\njnz 2f\n'
                   f'cmp qword ptr [{count(prev):#x}], 1000\njb 1b\nmov byte ptr [{x_at(prev) + 1:#x}], 0xf9\nmov byte ptr [{flag(prev):#x}], 1\n'
                   f'mov r10d, 1\n2: cmp byte ptr [{flag(j):#x}], 0\nje 1b\nxor eax, eax\ncpuid\nclc\n' + go(x_at(j), 1) + 'setc r9b\n')
        judge = lambda g: {'r9 (CF)': (g[9], 1), 'r10 (wrote)': (g[10], 1)}
        return code, [(x_at(j), run)], judge

    for name, make in (('cross', cross), ('peer', peer)):
        for n in (1, 2, 3, 4):
            made = [make(i, n) for i in range(n)]
            lines = ['identity', f'body {handler:#x} {pf.hex()}'] + [f'body {code_at(i):#x} {c.hex()}' for i, (c, _, _) in enumerate(made)]
            lines += [f'memory {m:#x} {b.hex()}' for m, b in tables + idt_items + [x for _, mem, _ in made for x in mem]]
            lines += [f'memory {count(0):#x} {bytes(0x100).hex()}'] if name == 'peer' else []
            for i in range(n):
                lines += ['cpu', f'rip {code_at(i):#x}', 'cpl 0', 'if 0']
            for block in (None, 1):
                _, out, err = driver(a, '\n'.join(lines) + '\n', a.out / 'nopruns' / f'{name}-{n}-{block}', [block] if block else [])
                cpus = out and (out['cpus'] if 'cpus' in out else [out])
                problems = [] if out else [f'driver: {err}']
                for i, c in enumerate(cpus or []):
                    g = [int(v, 0) for v in c['gpr']]
                    decodes = c['stats']['decodes'].get('0x79')
                    if c['stop'] != 'reached': problems.append(f"cpu{i} stop {c['stop']} {c['reason']} fault {c['fault']} rip {c['rip']}")
                    problems += [f'cpu{i} {k}: {x:#x}, the SDM {y:#x}' for k, (x, y) in made[i][2](g).items() if x != y]
                    if block is None and not decodes: problems.append(f'cpu{i}: no NOP_RUN record made')
                runs.append({'case': name, 'vcpus': n, 'block': block, 'ok': not problems, 'problems': problems,
                             'nop_runs': cpus and [c['stats']['decodes'].get('0x79') for c in cpus]})
                print(json.dumps(runs[-1]), flush=True)
                if problems: failures.append(f'nopruns {name} vcpus={n} block={block}: {problems}')
    report['nopruns'] = {'runs': runs, 'failures': failures}
    return failures


def tsc(a, report):
    """Perf chunk 15: RDTSC at CPL 0 is translated inline (no decode of it); its value is the resident's TSC (SDM Vol.2 RDTSC:
    EDX:EAX, the upper halves zero; Vol.3 18.17: the TSC MSR's write moves it; one invariant, synchronized TSC), judged in the
    guest and here, at block sizes default and 1:
    - `one`: 2,000 alternations of an inline RDTSC and the resident's RDTSCP never decrease (r9 counts decreases), and every
      value lies on one lattice floor(V x 125 / 3) + offset (TSC 1 GHz from the 24 MHz count V; the guest records each value
      mod 125 in r12:r13, which must fit {o, o + 41, o + 83} for one o); then WRMSR TSC := X and RDTSC (r14) is in
      [X, X + 10^9).
    - `ring`: N = 2..4 vCPUs pass a token 2,000 times each; its holder's inline RDTSC must not be below the previous holder's
      (r9 counts the decreases; x86-TSO message passing: the value is read after the token is seen)."""
    asm, failures, runs = ref.assemble, [], []
    X = 0x0123_4567_89ab_cdef
    one = asm(f'''xor r9d, r9d\nxor r10d, r10d\nxor r12d, r12d\nxor r13d, r13d\nmov r8, 2000
1: rdtsc\nshl rdx, 32\nor rax, rdx\ncmp rax, r10\njae 2f\ninc r9
2: mov r10, rax\ncall residue\nrdtscp\nshl rdx, 32\nor rax, rdx\ncmp rax, r10\njae 3f\ninc r9
3: mov r10, rax\ncall residue\ndec r8\njnz 1b
mov ecx, 0x10\nmov eax, {X & 0xffffffff:#x}\nmov edx, {X >> 32:#x}\nwrmsr\nrdtsc\nshl rdx, 32\nor rax, rdx\nmov r14, rax\njmp done
residue: xor edx, edx\nmov ecx, 125\ndiv rcx\ncmp edx, 64\njae 4f\nbts r12, rdx\nret
4: sub edx, 64\nbts r13, rdx\nret
done:''')
    for block in (None, 1):
        text = f'rip {CODE:#x}\nrflags 0x2\n{gprs({})}\ncode {one.hex()}\ncpl 0\nif 0\nregion 0x3e0000 0x10000\n'
        _, out, err = driver(a, text, a.out / 'tsc' / f'one-{block}', [block] if block else [])
        problems = [] if out else [f'driver: {err}']
        if out:
            g = [int(v, 0) for v in out['gpr']]
            seen = {r for r in range(125) if (g[12] | g[13] << 64) >> r & 1}
            if out['stop'] != 'reached': problems.append(f"stop {out['stop']} {out['reason']}")
            if g[9]: problems.append(f'{g[9]} decreases')
            if not any(seen <= {o % 125, (o + 41) % 125, (o + 83) % 125} for o in range(125)): problems.append(f'values off one lattice: residues {sorted(seen)}')
            if not X <= g[14] < X + 10**9: problems.append(f'after WRMSR TSC := {X:#x}: {g[14]:#x}')
            if out['stats']['decodes'].get('0x8'): problems.append(f"RDTSC decoded {out['stats']['decodes']['0x8']} times: not inline")
        runs.append({'case': 'one', 'block': block, 'ok': not problems, 'problems': problems})
        print(json.dumps(runs[-1]), flush=True)
        if problems: failures.append(f'tsc one block={block}: {problems}')
    turn, last, code_at = 0x2b8000, 0x2b8040, (lambda i: 0x110000 + 0x1000 * i)
    for n in (2, 3, 4):
        made = [asm(f'''xor r9d, r9d\nmov r8, 2000
1: mov rax, [{turn:#x}]\ncmp rax, {i}\njne 1b\nrdtsc\nshl rdx, 32\nor rax, rdx\ncmp rax, [{last:#x}]\njae 2f\ninc r9
2: mov [{last:#x}], rax\nmov qword ptr [{turn:#x}], {(i + 1) % n}\ndec r8\njnz 1b''') for i in range(n)]
        lines = ['identity'] + [f'body {code_at(i):#x} {c.hex()}' for i, c in enumerate(made)] + [f'memory {turn:#x} {bytes(0x80).hex()}']
        for i in range(n):
            lines += ['cpu', f'rip {code_at(i):#x}', 'cpl 0', 'if 0']
        for block in (None, 1):
            _, out, err = driver(a, '\n'.join(lines) + '\n', a.out / 'tsc' / f'ring-{n}-{block}', [block] if block else [])
            problems = [] if out else [f'driver: {err}']
            for i, c in enumerate(out['cpus'] if out else []):
                g = [int(v, 0) for v in c['gpr']]
                if c['stop'] != 'reached': problems.append(f"cpu{i} stop {c['stop']} {c['reason']}")
                if g[9]: problems.append(f'cpu{i}: {g[9]} decreases')
                if c['stats']['decodes'].get('0x8'): problems.append(f'cpu{i}: RDTSC decoded: not inline')
            runs.append({'case': 'ring', 'vcpus': n, 'block': block, 'ok': not problems, 'problems': problems})
            print(json.dumps(runs[-1]), flush=True)
            if problems: failures.append(f'tsc ring vcpus={n} block={block}: {problems}')
    report['tsc'] = {'runs': runs, 'failures': failures}
    return failures


def swapgs(a, report):
    """Perf chunk 17: SWAPGS at CPL 0 is translated inline (no decode of it) and exchanges GS.base with IA32_KERNEL_GS_BASE
    (SDM Vol.2 SWAPGS), which the resident keeps in the guest-state page while CPL-0 code runs and banks after it. Judged by
    the SDM (natemu only), with the driver's `gs_base` and `kernel_gs_base` (the bank's and the MSR record's at the stop):
    - `msr` (block sizes default and 1): GS.base A, KERNEL_GS_BASE B; SWAPGS; load GS:[0] (B's marker); SWAPGS; load
      GS:[0] (A's); SWAPGS; RDMSR of both (B, A); the stop's bases B and A.
    - `fault`: SWAPGS then, in the same block, a load from an unmapped page: #PF there with GS.base B and KERNEL_GS_BASE A.
    - `kicks`: 200,000 SYSCALL round trips from CPL 3, the kernel side `swapgs; inc qword gs:[0]; jmp 2f; 2: swapgs; sysretq`
      (a block boundary between the SWAPGSs, where a stop banks the swapped bases) and the user side `inc qword gs:[8]`,
      under 20,000 precise stops at random times (a host kicker): both counts exact.
    - `events`: 4,000,000 rounds of a CPL-0 loop `swapgs; inc qword gs:[0]; jmp 2f; 2: swapgs; inc qword gs:[8]` with IF = 1
      and an IDT, under interrupts posted at random times (vector 0x40, whose handler counts it and ends it with an x2APIC EOI): both counts exact, some
      interrupts taken.
    - `contain`: CPL 3 loads the guest-state page's KERNEL_GS_BASE field (the recorded deviation's alias: it must read 0, not
      the kernel's value), stores a forged value there, then SYSCALLs; the kernel's SWAPGS must load the real
      KERNEL_GS_BASE (GS:[0] is B's marker) and RDMSR must read B."""
    asm, failures, runs = ref.assemble, [], []
    A, B, N = USER_GS, KERNEL_GS, 200000
    markers = f'memory {A:#x} {(0xaaaa).to_bytes(32, "little").hex()}\nmemory {B:#x} {(0xbbbb).to_bytes(32, "little").hex()}\n'
    def run(name, text, extra, check):
        _, out, err = driver(a, text, a.out / 'swapgs' / name, extra)
        problems = [f'driver: {err}'] if not out else check(out, [int(v, 0) for v in out['gpr']])
        if out and out['stats']['decodes'].get('0x22'): problems.append(f"SWAPGS decoded {out['stats']['decodes']['0x22']} times: not inline")
        runs.append({'case': name, 'ok': not problems, 'problems': problems})
        print(json.dumps(runs[-1]), flush=True)
        if problems: failures.append(f'swapgs {name}: {problems}')
    def mem(out, at):
        return next((int.from_bytes(bytes.fromhex(m['data'])[at - int(m['addr'], 0):][:8], 'little') for m in out['memory']
                     if int(m['addr'], 0) <= at < int(m['addr'], 0) + len(m['data']) // 2), None)
    def bases(out, gs, kernel):
        got = (int(out['gs_base'], 0), int(out['kernel_gs_base'], 0))
        return [] if got == (gs, kernel) else [f'GS.base, KERNEL_GS_BASE {[hex(x) for x in got]}, want {hex(gs)}, {hex(kernel)}']
    head = 'cpl 0\nif 0\nrflags 0x2\nregion 0x3e0000 0x10000\n' + markers
    msr = asm(wrmsr(0xc0000101, A) + wrmsr(0xc0000102, B) + 'nop\nswapgs\nmov rsi, gs:[0]\nswapgs\nmov rdi, gs:[0]\nswapgs\n'
              'mov ecx, 0xc0000101\nrdmsr\nshl rdx, 32\nor rax, rdx\nmov r8, rax\nmov ecx, 0xc0000102\nrdmsr\nshl rdx, 32\nor rax, rdx\nmov r9, rax')
    for block in (None, 1):
        run(f'msr-{block}', f'rip {CODE:#x}\n{gprs({})}\ncode {msr.hex()}\n{head}', [block] if block else [],
            lambda out, g: ([] if out['stop'] == 'reached' and (g[6], g[7], g[8], g[9]) == (0xbbbb, 0xaaaa, B, A) else
                            [f"stop {out['stop']} rsi {g[6]:#x} rdi {g[7]:#x} r8 {g[8]:#x} r9 {g[9]:#x}"]) + bases(out, B, A))
    fault, lab = asm(wrmsr(0xc0000101, A) + wrmsr(0xc0000102, B) + 'nop\nswapgs\nmov rsi, gs:[0]\nx: mov rax, [0x7000000]\nnop', labels=True)
    run('fault', f'rip {CODE:#x}\n{gprs({})}\ncode {fault.hex()}\n{head}', [],
        lambda out, g: ([] if out['stop'] == 'guest_fault' and int(out['fault'][0], 0) == 14 and int(out['rip'], 0) == CODE + lab['x'] and g[6] == 0xbbbb
                        else [f"stop {out['stop']} {out['reason']} fault {out.get('fault')} rip {out['rip']} rsi {g[6]:#x}"]) + bases(out, B, A))
    kicks = syscall_program('1: inc qword ptr gs:[8]\nsyscall\ndec r8\njnz 1b', 'swapgs\ninc qword ptr gs:[0]\njmp 2f\n2: swapgs\nsysretq', {8: N}, markers)
    run('kicks', kicks, ['--kick-vcpu', '0', '--kicks', '20000', '--kick-every-us', '2', '--kick-jitter-us', '6'],
        lambda out, g: ([] if out['stop'] == 'reached' and (mem(out, A + 8), mem(out, B)) == (N, 0xbbbb + N) else
                        [f"stop {out['stop']} {out['reason']} counts {mem(out, A + 8)} {mem(out, B) - 0xbbbb}"]) + bases(out, A, B)
        + ([] if out['kicks']['stops'] > 1000 else [f"only {out['kicks']['stops']} stops"]))
    idt, idtr, count = 0x300000, 0x300800, 0x300810
    events, lab = asm(f'lidt [{idtr:#x}]\nmov ecx, 0x1b\nmov eax, 0xfee00d00\nxor edx, edx\nwrmsr\nmov ecx, 0x80f\nmov eax, 0x1ff\nwrmsr\n'
                      + wrmsr(0xc0000101, A) + wrmsr(0xc0000102, B) + 'sti\nnop\n1: swapgs\ninc qword ptr gs:[0]\njmp 2f\n2: swapgs\ninc qword ptr gs:[8]\ndec r8\njnz 1b\ncli\njmp done\n'
                      f'h: push rax\npush rcx\npush rdx\ninc qword ptr [{count:#x}]\nmov ecx, 0x80b\nxor eax, eax\nxor edx, edx\nwrmsr\npop rdx\npop rcx\npop rax\niretq\ndone:', labels=True)
    h = CODE + lab['h']
    gate = (h & 0xffff | 0x08 << 16 | 0x8e << 40 | (h >> 16 & 0xffff) << 48).to_bytes(8, 'little') + (h >> 32).to_bytes(8, 'little')
    run('events', f'rip {CODE:#x}\n{gprs({8: 20 * N})}\ncode {events.hex()}\n{head}svr 0x1ff\nmemory {idt:#x} {(bytes(16 * 0x40) + gate).hex()}\n'
        f'memory {idtr:#x} {((0x41 * 16 - 1).to_bytes(2, "little") + idt.to_bytes(8, "little") + bytes(6) + bytes(8)).hex()}\n',
        ['--kick-vcpu', '0', '--kicks', '5000', '--kick-every-us', '5', '--kick-jitter-us', '10', '--kick-events', '0x40'],
        lambda out, g: ([] if out['stop'] == 'reached' and (mem(out, A + 8), mem(out, B)) == (20 * N, 0xbbbb + 20 * N) and mem(out, count) > 100 else
                        [f"stop {out['stop']} {out['reason']} counts {mem(out, A + 8)} {mem(out, B) - 0xbbbb} interrupts {mem(out, count)}"]) + bases(out, A, B))
    field = 0xffff_4000_0000_1000 + 0xa0
    contain = syscall_program(f'mov rax, {field:#x}\nmov r10, [rax]\nmov qword ptr [rax], 0x123000\nsyscall',
                              'mov r13, rcx\nswapgs\nmov r9, gs:[0]\nswapgs\nmov ecx, 0xc0000102\nrdmsr\nshl rdx, 32\nor rax, rdx\nmov r12, rax\nmov rcx, r13\nsysretq',
                              {}, markers)
    run('contain', contain, [],
        lambda out, g: ([] if out['stop'] == 'reached' and (g[10], g[9], g[12]) == (0, 0xbbbb, B) else
                        [f"stop {out['stop']} {out['reason']} CPL-3 read {g[10]:#x} kernel GS:[0] {g[9]:#x} RDMSR {g[12]:#x}"]) + bases(out, A, B))
    report['swapgs'] = {'runs': runs, 'failures': failures}
    return failures


def sysfast(a, report):
    """Perf chunk 22: the resident's SYSCALL/SYSRET fast path (a kept SYSCALL or SYSRET at a literal exit runs on the state
    record in place and enters its target's block directly) is exact at both boundaries. Judged by the SDM (natemu only).
    Every case is N SYSCALL round trips from a CPL-3 loop (`inc [UC]; syscall; dec r8; jnz`) into a Linux-shaped kernel side
    (`endbr64; swapgs; ...; inc [KC]; dec r9; jnz plain; SPECIAL; plain: ...; swapgs; 7-byte NOP; sysretq`, FMASK 0x47700:
    IF = 0 in the kernel), STAR naming the driver's GDT (SYSCALL CS 0x08, SYSRET CS 0x23), with the program's own IDT
    (gates on IST 1, the driver's TSS): handlers for #DB, #UD, #GP and vector 0x40 count, record their first frame (RIP,
    CS, RFLAGS; DR6 or the error code) and return. SPECIAL runs once, in round K (the records and blocks kept by then):
    - `sysret-irq`: posts vector 0x40 with IF = 0 (a hook `nop`); the SYSRET sets IF: taken at the first CPL-3 boundary
      (frame RIP the return RIP, CS 0x23, IF = 1), once (SDM Vol.3 7.9: recognised at the boundary after the instruction
      that sets IF; SYSRET has no shadow).
    - `sysret-tf`: R11 with TF: #DB (BS) before the first CPL-3 instruction (frame RIP the return RIP), once.
    - `lstar-break`, `ret-break` (no SPECIAL): an instruction breakpoint (DR0, DR7 L0) at IA32_LSTAR, resp. at the return RIP:
      a #DB fault there (B0) at every one of the N round trips (Vol.3 18.3.1.1; the handler sets RF).
    - `watch` (no SPECIAL): a data breakpoint on the quadword the loop writes right before its SYSCALL: a #DB trap (B0) with
      frame RIP the SYSCALL's, every round (Vol.3 18.3.1.2: after the writing instruction, before the SYSCALL). In these
      four #DB cases every delivery's frame RIP is checked, not only the first one's (the first round runs the slow path).
    - `syscall-sce0`: the kernel clears EFER.SCE and returns by IRETQ: the next SYSCALL is #UD at CPL 3 (its handler sets SCE
      and retries it), once. `sysret-sce0`: the kernel clears SCE before its SYSRET: #UD at the SYSRET (CPL 0), once.
    - `sysret-noncanonical`: RCX = 2^47 at the SYSRET: #GP(0) there at CPL 0 (the handler restores RCX and retries), once.
    - `kicks-events`: no SPECIAL, 200,000 rounds under vector 0x40 posted at random times (the driver's kicker): every
      delivery at CPL 3 with IF = 1 (none in the kernel's IF = 0 span), several hundred taken.
    Every case reaches its end with UC = KC = N."""
    asm, failures, runs = ref.assemble, [], []
    N, K, KSTACK = 2000, 1000, STACK - 0x100
    TABLES, IDTR = 0x300000, 0x300800
    SLOT = {1: 0x300900, 6: 0x300940, 13: 0x300980, 0x40: 0x3009c0}
    BAD, W, UC, KC, EXP1 = 0x300a00, 0x300a08, 0x300a10, 0x300a18, 0x300a20
    def handler(v):
        s, frame = SLOT[v], 16 if v == 13 else 8     # the frame's RIP after `push rax` (and #GP's error code)
        first = (f'mov rax, [rsp + {frame}]\nmov [{s + 8:#x}], rax\nmov rax, [rsp + {frame + 8}]\nmov [{s + 16:#x}], rax\n'
                 f'mov rax, [rsp + {frame + 16}]\nmov [{s + 24:#x}], rax\n'
                 + ('mov rax, dr6\n' if v == 1 else 'mov rax, [rsp + 8]\n' if v == 13 else 'xor eax, eax\n') + f'mov [{s + 32:#x}], rax\n')
        check = (f'cmp qword ptr [rsp + 16], 0x23\njne 3f\ntest qword ptr [rsp + 24], 0x200\njnz 4f\n3: inc qword ptr [{BAD:#x}]\n4:\n'
                 if v == 0x40 else
                 # Every #DB's frame RIP is the case's (EXP1, when set), not only the first one's.
                 f'mov rax, [{EXP1:#x}]\ntest rax, rax\njz 4f\ncmp rax, [rsp + 8]\nje 4f\ninc qword ptr [{BAD:#x}]\n4:\n' if v == 1 else '')
        tail = {1: f'and qword ptr [rsp + 24], -0x101\nor qword ptr [rsp + 24], 0x10000\n',
                6: 'push rcx\npush rdx\nmov ecx, 0xc0000080\nrdmsr\nor eax, 1\nwrmsr\npop rdx\npop rcx\n',
                13: 'mov rcx, r10\n',
                0x40: 'push rcx\npush rdx\nmov ecx, 0x80b\nxor eax, eax\nxor edx, edx\nwrmsr\npop rdx\npop rcx\n'}[v]
        return (f'h{v}:\npush rax\ninc qword ptr [{s:#x}]\n{check}cmp qword ptr [{s:#x}], 1\njne 2f\n{first}2:\n{tail}pop rax\n'
                + ('add rsp, 8\n' if v == 13 else '') + 'iretq\n')
    clear_sce = 'mov r10, rcx\nmov ecx, 0xc0000080\nrdmsr\nand eax, 0xfffffffe\nwrmsr\nmov rcx, r10\n'
    specials = {
        'sysret-irq': 'hook: nop\n', 'sysret-tf': 'or r11, 0x100\n', 'lstar-break': '', 'ret-break': '', 'watch': '',
        'syscall-sce0': clear_sce + 'mov rsp, gs:[16]\nswapgs\nmov rax, rsp\npush 0x1b\npush rax\npush r11\npush 0x23\npush rcx\niretq\n',
        'sysret-sce0': clear_sce, 'sysret-noncanonical': f'mov r10, rcx\nmov rcx, {1 << 47:#x}\n', 'kicks-events': ''}
    for name, special in specials.items():
        kernel, kl = asm('endbr64\nswapgs\nmov gs:[16], rsp\nmov rsp, gs:[24]\n'
                         f'inc qword ptr [{KC:#x}]\ndec r9\njnz plain\n{special}plain:\nmov rsp, gs:[16]\nswapgs\n'
                         '.byte 0x0f,0x1f,0x80,0,0,0,0\nsysret_at: sysretq', labels=True)
        n = 200_000 if name == 'kicks-events' else N
        dr = {'lstar-break': (KERNEL_RIP, 1), 'watch': (W, 0xd0001)}.get(name)
        setup = (f'lidt [{IDTR:#x}]\nmov ecx, 0x1b\nmov eax, 0xfee00d00\nxor edx, edx\nwrmsr\nmov ecx, 0x80f\nmov eax, 0x1ff\nwrmsr\n'
                 'mov ecx, 0xc0000080\nrdmsr\nor eax, 1\nwrmsr\n' + wrmsr(0xc0000081, 0x10 << 48 | 0x08 << 32) + wrmsr(0xc0000082, KERNEL_RIP)
                 + wrmsr(0xc0000084, 0x47700) + wrmsr(0xc0000101, USER_GS) + wrmsr(0xc0000102, KERNEL_GS)
                 + f'mov qword ptr [{KERNEL_GS + 24}], {KSTACK:#x}\n'
                 + ('lea rax, [rip + ret_here]\nmov dr0, rax\nmov eax, 1\nmov dr7, rax\n' if name == 'ret-break' else
                    f'mov rax, {dr[0]:#x}\nmov dr0, rax\nmov eax, {dr[1]:#x}\nmov dr7, rax\n' if dr else ''))
        code, lab = asm(setup + 'lea rcx, [rip + user]\nmov r11d, 0x202\nsysretq\n'
                        f'user:\n1: inc qword ptr [{UC:#x}]\nmov [{W:#x}], r8\nsc: syscall\nret_here: dec r8\njnz 1b\njmp done\n'
                        + ''.join(handler(v) for v in SLOT) + 'done:', labels=True)
        gates = bytearray(16 * 0x41)
        for v in SLOT:
            h = CODE + lab[f'h{v}']
            gates[16 * v:16 * v + 16] = ((h & 0xffff | 0x08 << 16 | 1 << 32 | 0x8e << 40 | (h >> 16 & 0xffff) << 48).to_bytes(8, 'little')
                                         + (h >> 32).to_bytes(8, 'little'))
        tables = bytes(gates) + bytes(IDTR - TABLES - len(gates)) + (0x41 * 16 - 1).to_bytes(2, 'little') + TABLES.to_bytes(8, 'little')
        tables += bytes(KC + 8 - TABLES - len(tables))
        exp1 = {'watch': CODE + lab['sc'], 'lstar-break': KERNEL_RIP, 'ret-break': CODE + lab['ret_here'], 'sysret-tf': CODE + lab['ret_here']}.get(name, 0)
        tables += exp1.to_bytes(8, 'little')
        text = (f'rip {CODE:#x}\nrflags 0x2\n{gprs({8: n, 9: K})}\ncode {code.hex()}\nbody {KERNEL_RIP:#x} {kernel.hex()}\ncpl 0\nif 0\nsvr 0x1ff\n'
                f'memory {USER_GS:#x} {bytes(32).hex()}\nmemory {KERNEL_GS:#x} {bytes(32).hex()}\nmemory {TABLES:#x} {tables.hex()}\nregion 0x3e0000 0x10000\n'
                + (f'post {KERNEL_RIP + kl["hook"]:#x} 0x40\n' if 'hook' in kl else ''))
        extra = ['--kick-vcpu', '0', '--kicks', '5000', '--kick-every-us', '5', '--kick-jitter-us', '10', '--kick-events', '0x40'] if name == 'kicks-events' else []
        _, out, err = driver(a, text, a.out / 'sysfast' / name, extra)
        def mem(at):
            return next((int.from_bytes(bytes.fromhex(m['data'])[at - int(m['addr'], 0):][:8], 'little') for m in out['memory']
                         if int(m['addr'], 0) <= at < int(m['addr'], 0) + len(m['data']) // 2), None)
        problems = []
        if not out:
            problems.append(f'driver: {err}')
        else:
            g = [int(x, 0) for x in out['gpr']]
            if out['stop'] != 'reached' or g[8] != 0 or (mem(UC), mem(KC)) != (n, n):
                problems.append(f"stop {out['stop']} {out.get('reason')} r8 {g[8]} UC {mem(UC)} KC {mem(KC)} (want {n})")
            ret, sysret = CODE + lab['ret_here'], KERNEL_RIP + kl['sysret_at']
            # vector: (count, RIP, CS, a bit the RFLAGS or DR6 field must hold, error code) of the first delivery
            want = {'sysret-irq': {0x40: (1, ret, 0x23, ('rflags', 0x200))}, 'sysret-tf': {1: (1, ret, 0x23, ('extra', 0x4000))},
                    'lstar-break': {1: (n, KERNEL_RIP, 0x08, ('extra', 1))}, 'ret-break': {1: (n, ret, 0x23, ('extra', 1))},
                    'watch': {1: (n, CODE + lab['sc'], 0x23, ('extra', 1))}, 'syscall-sce0': {6: (1, CODE + lab['sc'], 0x23, None)},
                    'sysret-sce0': {6: (1, sysret, 0x08, None)}, 'sysret-noncanonical': {13: (1, sysret, 0x08, ('error', 0))},
                    'kicks-events': {}}[name]
            for v, s in SLOT.items():
                count, rip, cs, rflags, extra_ = (mem(s + 8 * k) for k in range(5))
                if v not in want:
                    if count and not (name == 'kicks-events' and v == 0x40):
                        problems.append(f'vector {v:#x} delivered {count} times')
                    continue
                c, r, cs_want, field = want[v]
                if (count, rip, cs) != (c, r, cs_want):
                    problems.append(f'vector {v:#x}: count {count} first RIP {rip:#x} CS {cs:#x}, want {c} {r:#x} {cs_want:#x}')
                if field and ((field[0] == 'error' and extra_ != field[1]) or (field[0] != 'error' and not {'rflags': rflags, 'extra': extra_}[field[0]] & field[1])):
                    problems.append(f'vector {v:#x}: {field[0]} {rflags if field[0] == "rflags" else extra_:#x} lacks {field[1]:#x}')
            if name == 'kicks-events' and (mem(SLOT[0x40]) < 100 or mem(BAD)):
                problems.append(f'kicks-events: {mem(SLOT[0x40])} interrupts, {mem(BAD)} outside CPL 3 or with IF = 0')
            elif mem(BAD):
                problems.append(f'{mem(BAD)} deliveries at the wrong place (vector 0x40: outside CPL 3 or with IF = 0; #DB: another RIP than the first one\'s)')
        runs.append({'case': name, 'ok': not problems, 'problems': problems})
        print(json.dumps(runs[-1]), flush=True)
        if problems: failures.append(f'sysfast {name}: {problems}')
    sysfast_more(a, runs, failures)
    report['sysfast'] = {'runs': runs, 'failures': failures}
    return failures


def sysfast_more(a, runs, failures):
    """Perf chunk 22, the review's further `sysfast` cases (judged by the SDM, natemu only). Each is N = 2,000 SYSCALL round
    trips (`apic-timer`, `peer-write`: as many as they take) into a Linux-shaped kernel side (IF = 0 there, FMASK 0x47700)
    that counts its rounds in KC, runs a case's SPECIAL in the round it names, and returns by SYSRET; the program's own IDT
    (gates on IST 1) where a case takes events. Every case ends with UC = KC (= N where fixed):
    - `walk-ad` (identity space, the guest's own tables): the PTE of L2, a second linear page of the user code's frame F,
      lies in F itself once round K points L2's PDE at F (A = 0 there), then reloads CR3. The user loop at L1 = F ends with a
      direct JMP to the SYSCALL at L2 (a literal exit; the kernel returns to L1), so the first SYSCALL after the reload
      finds its kept decode not current, and the fetch walk that revalidates it writes that A bit into F, killing the
      decode and the block that exited (the SDM's A-bit write, Vol.3 4.8; the code-modification rule). Every round
      completes (the fast path's decode lookup, if it walked, would leave the linker a dead block: a halt).
    - `user-spin`: vector 0x40 posted in the kernel of round K (IF = 0); after the SYSRET the user side spins on a flag the
      handler sets, with no system instruction, at most 50,000,000 times. The interrupt must be taken at the first CPL-3
      boundary (Vol.3 7.9; SYSRET has no shadow), so the spin ends at once; running out is a failure.
    - `peer-write` (2 vCPUs): vCPU 0 makes round trips whose return RIP X is `mov eax, imm32`; vCPU 1 flips X's immediate
      (0x11 <-> 0x22) after every 20 of vCPU 0's rounds, raises a flag and waits for vCPU 0 to acknowledge it, 300 times.
      vCPU 0, on seeing the flag, runs CPUID (the SDM's serialising step, Vol.3 9.1.3) and its next X must load the new
      value (before the CPUID either is allowed).
    - `apic-timer`: the LAPIC timer periodic at 10 ms (crystal / 16, initial count 15,000) for about 0.1 s of round trips,
      the kernel side holding a two-instruction IF = 1 window (`sti; nop; inc; cli`). Every timer interrupt is taken at
      CPL 3 with IF = 1 or at one of the window's two interruptible boundaries (the instruction after STI is in its shadow,
      Vol.3 7.9) with IF = 1; none is missed: each delivery comes 0.5-1.5 periods after the previous one (the first
      after the start), and their number by the end is the periods elapsed, +-1 (the TSC rate from CPUID 0x15).
    - `rep-target`: every round's SYSRET returns to a REP MOVSB (so its block is kept by round K); vector 0x40 posted in
      round K's kernel, whose SYSRET returns to that REP MOVSB (RCX, its count, the return
      RIP 0x1000): the interrupt is taken before its first iteration (frame RIP the REP, RCX, RSI, RDI untouched; Vol.3
      7.9, Vol.2 REP: iterations are interruptible, none has run), and the 4 KiB copy then completes.
    - `nmi-sysret`: an NMI posted in round K's kernel is taken at the next boundary (CPL 0, IF = 0 in the frame); its
      handler posts a second NMI, which is blocked (Vol.3 6.7.1), and returns without IRET, so NMIs stay blocked across
      that SYSRET and every round trip after it until round K2 = 1,500's kernel executes an IRETQ to itself: the second
      NMI is taken right after that IRETQ (frame RIP its target), and only then.
    - `div-frame`: the return RIP is `div r10` (RDX:RAX / 3, fexc's division helper: RDX != 0): RDX = 1 (no fault) or,
      every 4th round, 5 (#DE: the quotient overflows). The #DE frame names the DIV (a fault: RIP, CS 0x23, IF, RSP) with
      RAX, RDX unchanged and the other registers live (r12, the round, read back from memory); the handler skips the DIV;
      the quotients and remainders sum to the SDM's values and r12 survives every division."""
    asm = ref.assemble
    N, K, K2, KSTACK = 2000, 1000, 1500, STACK - 0x100
    D = 0x300000                              # one page, read back: IDT, IDTR (D + 0x800), the slots below
    IDTR = D + 0x800
    UC, KC, BAD, CNT, FLAG, SPINFAIL, QSUM, RSUM = (D + 0x900 + 8 * k for k in range(8))
    F1, F2, M = D + 0x980, D + 0x9c0, D + 0xa00   # first and second frames (RIP, CS, RFLAGS, KC); misc slots M + 8k
    SAVE, RESUME, START, DEADLINE, LAST, END, KW, KIRQ = (D + 0xa80 + 8 * k for k in range(8))
    TS = D + 0xc00                            # apic-timer: the TSC at each of the first 64 deliveries
    gate = lambda h: ((h & 0xffff | 0x08 << 16 | 1 << 32 | 0x8e << 40 | (h >> 16 & 0xffff) << 48).to_bytes(8, 'little')
                      + (h >> 32).to_bytes(8, 'little'))
    eoi = 'push rcx\npush rdx\nmov ecx, 0x80b\nxor eax, eax\nxor edx, edx\nwrmsr\npop rdx\npop rcx\n'   # (rax saved by the caller)

    def frame(slot, at):
        """The handler's frame (RIP, CS, RFLAGS at rsp + at) and KC into `slot` (rax scratch)."""
        return ''.join(f'mov rax, [rsp + {at + 8 * k}]\nmov [{slot + 8 * k:#x}], rax\n' for k in range(3)) + f'mov rax, [{KC:#x}]\nmov [{slot + 24:#x}], rax\n'

    def kernel(specials, before=''):
        """The kernel side at KERNEL_RIP: `specials` {round: code} run in that round, `before` in every round before the exit."""
        jumps = ''.join(f'cmp qword ptr [{KC:#x}], {k}\nje s{k}\n' for k in specials)
        return asm(f'endbr64\nswapgs\nmov gs:[16], rsp\nmov rsp, gs:[24]\ninc qword ptr [{KC:#x}]\n{jumps}plain:\n{before}'
                   'mov rsp, gs:[16]\nswapgs\n.byte 0x0f,0x1f,0x80,0,0,0,0\nsysret_at: sysretq\n'
                   + ''.join(f's{k}:\n{code}jmp plain\n' for k, code in specials.items()), labels=True)

    def setup(kstack, idt):
        return ((f'lidt [{IDTR:#x}]\nmov ecx, 0x1b\nmov eax, 0xfee00d00\nxor edx, edx\nwrmsr\nmov ecx, 0x80f\nmov eax, 0x1ff\nwrmsr\n' if idt else '')
                + 'mov ecx, 0xc0000080\nrdmsr\nor eax, 1\nwrmsr\n' + wrmsr(0xc0000081, 0x10 << 48 | 0x08 << 32) + wrmsr(0xc0000082, KERNEL_RIP)
                + wrmsr(0xc0000084, 0x47700) + wrmsr(0xc0000101, USER_GS) + wrmsr(0xc0000102, KERNEL_GS)
                + f'mov qword ptr [{KERNEL_GS + 24}], {kstack:#x}\n')

    def program(user, handlers, extra=''):
        """CPL-0 setup, then SYSRET to `user` (CPL 3, IF = 1); `handlers` h<vector>; ends at `done`."""
        return asm(setup(KSTACK, True) + extra + f'lea rcx, [rip + user]\nmov r11d, 0x202\nsysretq\nuser:\n{user}jmp done\n{handlers}done:', labels=True)

    def page(lab, vectors):
        p = bytearray(0x1000)
        for v in vectors:
            p[16 * v:16 * v + 16] = gate(CODE + lab[f'h{v}'])
        p[0x800:0x80a] = (16 * (max(vectors, default=0) + 1) - 1).to_bytes(2, 'little') + D.to_bytes(8, 'little')
        return p

    def text(code, kern, lab, vectors, items='', regs=None):
        return (f'rip {CODE:#x}\nrflags 0x2\n{gprs({8: N, **(regs or {})})}\ncode {code.hex()}\nbody {KERNEL_RIP:#x} {kern.hex()}\ncpl 0\nif 0\nsvr 0x1ff\n'
                f'memory {USER_GS:#x} {bytes(32).hex()}\nmemory {KERNEL_GS:#x} {bytes(32).hex()}\nmemory {D:#x} {page(lab, vectors).hex()}\n'
                f'region 0x3e0000 0x10000\n{items}')

    def memory(c, at, n=8):
        for m in c['memory']:
            base, data = int(m['addr'], 0), bytes.fromhex(m['data'])
            if base <= at and at + n <= base + len(data):
                return data[at - base:at - base + n]
        return None

    def word(c, at):
        b = memory(c, at)
        return None if b is None else int.from_bytes(b, 'little')

    def common(c, n=N):
        g = [int(x, 0) for x in c['gpr']]
        uc, kc = word(c, UC), word(c, KC)
        ok = c['stop'] == 'reached' and uc == kc and (n is None or uc == n)
        return [] if ok else [f"stop {c['stop']} {c.get('reason')} fault {c.get('fault')} rip {c['rip']} r8 {g[8]} UC {uc} KC {kc} (want {n})"]

    def run(name, text_, extra, judge):
        _, out, err = driver(a, text_, a.out / 'sysfast' / name, extra)
        problems = [f'driver: {err}'] if not out else judge(out)
        runs.append({'case': name, 'ok': not problems, 'problems': problems})
        print(json.dumps(runs[-1]), flush=True)
        if problems: failures.append(f'sysfast {name}: {problems}')

    def first(c, slot, count, rip, cs, flag_if):
        got = (word(c, CNT), word(c, slot), word(c, slot + 8))
        want = (count, rip, cs)
        p = [] if got == want else [f'count, first RIP, CS {got}, want {want}']
        rf = word(c, slot + 16) or 0
        return p + ([] if bool(rf & 0x200) == flag_if else [f'frame RFLAGS {rf:#x}: IF should be {int(flag_if)}'])

    # walk-ad: identity space; the guest's tables (PML4 -> PDPT -> PD; PD[0] -> PT0, PD[1] -> PT1, PD[3] -> PT3).
    L1, L2, S, SC = 0x100000, 0x7ff000, 0x104000, 0x800
    PML4, PDPT, PD, PT0, PT1, PT3 = 0x110000, 0x111000, 0x112000, 0x113000, 0x114000, 0x115000
    WSTACK, NONLEAF = 0x3d0000, P.P | P.RW | P.US | P.A
    s_code = asm(f'mov eax, {PML4:#x}\nmov cr3, rax\n' + setup(WSTACK - 0x100, False) + f'mov ecx, {L1:#x}\nmov r11d, 0x202\nsysretq\n')
    end = S + len(s_code)
    user, ul = asm(f'1: inc qword ptr [{UC:#x}]\nj: .byte 0xe9,0,0,0,0\nret1: dec r8\njnz 1b\nmov eax, {end:#x}\njmp rax\n', labels=True)
    rel = (L2 + SC) - (L1 + ul['j'] + 5)
    user = user[:ul['j']] + b'\xe9' + rel.to_bytes(4, 'little', signed=True) + user[ul['j'] + 5:]
    assert len(user) < SC
    # F: the user code, the SYSCALL at offset SC (run through L2 only), PTE(L2) at 0xff8 (P, RW, US; A = 0) for round K.
    f_page = user + bytes(SC - len(user)) + asm('syscall') + bytes(0xff8 - SC - 2) + (L1 | P.P | P.RW | P.US).to_bytes(8, 'little')
    kern, kl = kernel({K: f'mov eax, {L1 | NONLEAF:#x}\nmov [{PD + 8 * 3:#x}], rax\nmov rax, cr3\nmov cr3, rax\n'}, f'mov ecx, {L1 + ul["ret1"]:#x}\n')
    tables = {PML4: {0: PDPT | NONLEAF}, PDPT: {0: PD | NONLEAF}, PD: {0: PT0 | NONLEAF, 1: PT1 | NONLEAF, 3: PT3 | NONLEAF},
              PT0: {(v >> 12) & 511: v | P.LEAF for v in (L1, S, PD)},
              PT1: {(v >> 12) & 511: v | P.LEAF for v in [KERNEL_RIP, USER_GS, KERNEL_GS, D, *range(0x3c0000, WSTACK, 0x1000),
                                                          *(0x3e8000 + o for o in (0, 0x1000, 0x3000, 0x4000, 0x5000, 0x6000))]},
              PT3: {(L2 >> 12) & 511: L1 | P.LEAF}}
    table_items = ''.join(f'memory {t:#x} {b"".join(e.get(i, 0).to_bytes(8, "little") for i in range(512)).hex()}\n' for t, e in tables.items())
    run('walk-ad', (f'identity\nrip {S:#x}\nrflags 0x2\n{gprs({4: WSTACK, 8: N})}\ncpl 0\nif 0\nbody {S:#x} {s_code.hex()}\nbody {L1:#x} {f_page.hex()}\n'
                    f'body {KERNEL_RIP:#x} {kern.hex()}\nmemory {USER_GS:#x} {bytes(32).hex()}\nmemory {KERNEL_GS:#x} {bytes(32).hex()}\n'
                    f'memory {D:#x} {bytes(0x1000).hex()}\n{table_items}region 0x3c0000 {WSTACK - 0x3c0000:#x}\n'), [],
        lambda c: common(c))

    # user-spin
    SPIN = 50_000_000
    kern, kl = kernel({K: 'spin_hook: nop\n'})
    code, lab = program(f'1: inc qword ptr [{UC:#x}]\nsyscall\nret_here: cmp qword ptr [{KC:#x}], {K}\njne 2f\nmov ebx, {SPIN}\n'
                        f'3: cmp qword ptr [{FLAG:#x}], 0\njne 2f\ndec rbx\njnz 3b\ninc qword ptr [{SPINFAIL:#x}]\n2: dec r8\njnz 1b\n',
                        f'h64: push rax\ninc qword ptr [{CNT:#x}]\ncmp qword ptr [{CNT:#x}], 1\njne 2f\n{frame(F1, 8)}'
                        f'2: mov qword ptr [{FLAG:#x}], 1\n{eoi}pop rax\niretq\n')
    run('user-spin', text(code, kern, lab, [0x40], f'post {KERNEL_RIP + kl["spin_hook"]:#x} 0x40\n'), [],
        lambda c: common(c) + first(c, F1, 1, CODE + lab['ret_here'], 0x23, True)
        + ([f'the spin ran out {word(c, SPINFAIL)} times: the interrupt was not taken'] if word(c, SPINFAIL) else []))

    # peer-write (vCPU 0's slots: ACK M, the flag M + 8, SEEN M + 16)
    ACK, PFLAG, SEEN, REWRITES, PACE = M, M + 8, M + 16, 300, 20
    kern, kl = kernel({})
    c0, l0 = asm(setup(KSTACK, False) + 'lea rcx, [rip + user]\nmov r11d, 0x202\nsysretq\nuser:\n'
                 f'''xor r9d, r9d\nxor r10d, r10d\nxor r12d, r12d\nxor r14d, r14d
1: inc qword ptr [{UC:#x}]\nsyscall
x: mov eax, 0x11
test r14, r14\njz 2f\ninc r12\nxor r14d, r14d\nmov [{SEEN:#x}], r10\ncmp eax, r13d\nje 2f\ninc r9
2: inc qword ptr [{ACK:#x}]\nmov rax, [{PFLAG:#x}]\ncmp rax, r10\nje 4f\nmov r10, rax\nxor eax, eax\ncpuid
mov r13d, 0x11\ntest r10b, 1\njz 3f\nmov r13d, 0x22\n3: mov r14d, 1
4: cmp r12, {REWRITES}\njb 1b\njmp done\ndone:''', labels=True)
    assert len(c0) < 0xff0
    PEER = 0x108000
    c1 = asm(f'''xor ecx, ecx
1: mov r8, [{ACK:#x}]
2: mov rax, [{ACK:#x}]\nsub rax, r8\ncmp rax, {PACE}\njae 3f\npause\njmp 2b
3: inc ecx\nmov al, 0x11\ntest cl, 1\njz 5f\nmov al, 0x22
5: mov byte ptr [{CODE + l0["x"] + 1:#x}], al\nmov [{PFLAG:#x}], rcx\ncmp ecx, {REWRITES}\njae 9f
6: cmp [{SEEN:#x}], rcx\nje 1b\npause\njmp 6b
9: nop''')

    def peer_judge(out):
        v0, v1 = out['cpus']
        g0, g1 = [int(x, 0) for x in v0['gpr']], [int(x, 0) for x in v1['gpr']]
        p = common(v0, None)
        if g0[9]: p.append(f'{g0[9]} of {g0[12]} checks ran the old X after the flag and CPUID')
        if (g0[12], g1[1]) != (REWRITES, REWRITES): p.append(f'{g0[12]} checks, {g1[1]} rewrites, {REWRITES} wanted')
        return p + ([] if v1['stop'] == 'reached' else [f"vCPU 1 stop {v1['stop']} {v1.get('reason')}"])
    # (The `memory` item on X's page makes it writable for the peer.)
    run('peer-write', (f'body {CODE:#x} {c0.hex()}\nbody {KERNEL_RIP:#x} {kern.hex()}\nbody {PEER:#x} {c1.hex()}\nmemory {CODE + 0xff0:#x} 00\n'
                       f'memory {USER_GS:#x} {bytes(32).hex()}\nmemory {KERNEL_GS:#x} {bytes(32).hex()}\nmemory {D:#x} {bytes(0x1000).hex()}\n'
                       f'region 0x3e0000 0x10000\ncpu\nrip {CODE:#x}\nrflags 0x2\n{gprs({})}\ncpl 0\nif 0\n'
                       f'cpu\nrip {PEER:#x}\nrflags 0x2\n{gprs({4: STACK - 0x8000})}\n'), [], peer_judge)

    # apic-timer: vector 0x41, LVT timer periodic, divide by 16 (DCR 3), initial count 15,000: a 10 ms period.
    DIVIDE, INITIAL = 16, 15000
    kern, kl = kernel({}, f'sti\nnop\nk1: inc qword ptr [{KW:#x}]\nk2: cli\nrdtsc\nshl rdx, 32\nor rax, rdx\nmov [{LAST:#x}], rax\n')
    window = (KERNEL_RIP + kl['k1'], KERNEL_RIP + kl['k2'])
    timer = (f'mov eax, 0x15\ncpuid\nmov r12d, eax\nmov r13d, ebx\nmov r14d, ecx\nmov [{M:#x}], r12\nmov [{M + 8:#x}], r13\nmov [{M + 16:#x}], r14\n'
             'mov rax, r14\nimul rax, r13\nxor edx, edx\ndiv r12\nxor edx, edx\nmov ecx, 10\ndiv rcx\nmov r15, rax\n'   # r15: TSC ticks in 0.1 s
             'mov ecx, 0x83e\nmov eax, 3\nxor edx, edx\nwrmsr\nmov ecx, 0x832\nmov eax, 0x20041\nwrmsr\n'
             f'mov ecx, 0x838\nmov eax, {INITIAL}\nwrmsr\nrdtsc\nshl rdx, 32\nor rax, rdx\nmov [{START:#x}], rax\nadd rax, r15\nmov [{DEADLINE:#x}], rax\n')
    code, lab = program(f'1: inc qword ptr [{UC:#x}]\nsyscall\nmov rax, [{LAST:#x}]\ncmp rax, [{DEADLINE:#x}]\njb 1b\nmov [{END:#x}], rax\n',
                        f'''h65: push rax\npush rcx\npush rdx\nmov rcx, [{CNT:#x}]\ncmp rcx, 64\njae 1f\nrdtsc\nshl rdx, 32\nor rax, rdx\nmov [{TS:#x} + rcx * 8], rax
1: inc qword ptr [{CNT:#x}]\ntest qword ptr [rsp + 40], 0x200\njz 8f\ncmp qword ptr [rsp + 32], 0x23\nje 9f\nmov rax, [rsp + 24]
cmp rax, {window[0]:#x}\nje 7f\ncmp rax, {window[1]:#x}\nje 7f
8: inc qword ptr [{BAD:#x}]\njmp 9f
7: inc qword ptr [{KIRQ:#x}]
9: mov ecx, 0x80b\nxor eax, eax\nxor edx, edx\nwrmsr\npop rdx\npop rcx\npop rax\niretq\n''', timer)

    def timer_judge(c):
        p = common(c, None)
        den, num, count, start, finish = word(c, M), word(c, M + 8), word(c, CNT), word(c, START), word(c, END)
        if not (den and num and count and finish):
            return p + [f'CPUID 0x15 {den} {num}, {count} interrupts, end {finish}']
        period = DIVIDE * INITIAL * num / den                   # TSC ticks (TSC = crystal x num / den)
        ts = [word(c, TS + 8 * i) for i in range(min(count, 64))]
        by_end, expected = sum(t <= finish for t in ts), int((finish - start) / period)
        if abs(by_end - expected) > 1: p.append(f'{by_end} interrupts by the end, {expected} periods elapsed')
        gaps = [(t - u) / period for u, t in zip([start] + ts, ts)]
        if not all(0.5 < g < 1.5 for g in gaps): p.append(f'delivery gaps (periods) {[round(g, 2) for g in gaps]}: one missed or extra')
        if word(c, BAD): p.append(f'{word(c, BAD)} interrupts outside CPL 3 and the IF = 1 window, or with IF = 0')
        runs_info.update({'interrupts': count, 'in_window': word(c, KIRQ), 'rounds': word(c, UC)})
        return p
    runs_info = {}
    run('apic-timer', text(code, kern, lab, [0x41]), [], timer_judge)
    runs[-1].update(runs_info)

    # rep-target: round K's SYSRET returns to REP (RCX = 0x1000, the REP's count) with RSI, RDI the user side's.
    REP, SRC, DST = 0x1000, 0x320000, 0x330000
    pattern = bytes((37 * i + 11) & 0xff for i in range(0x1000))
    kern, kl = kernel({K: 'rep_hook: nop\n'}, before=f'mov ecx, {REP:#x}\n')
    code, lab = program(f'1: inc qword ptr [{UC:#x}]\nmov esi, {SRC:#x}\nmov edi, {DST:#x}\nsyscall\nret_here: dec r8\njnz 1b\n',
                        f'h64: push rax\ninc qword ptr [{CNT:#x}]\ncmp qword ptr [{CNT:#x}], 1\njne 2f\n{frame(F1, 8)}'
                        f'mov [{M:#x}], rcx\nmov [{M + 8:#x}], rsi\nmov [{M + 16:#x}], rdi\n2: {eoi}pop rax\niretq\n')
    rep = asm(f'rep movsb\nmov eax, {CODE + lab["ret_here"]:#x}\njmp rax')
    run('rep-target', text(code, kern, lab, [0x40], f'body {REP:#x} {rep.hex()}\nmemory {SRC:#x} {pattern.hex()}\nmemory {DST:#x} {bytes(0x1000).hex()}\n'
                                                 f'post {KERNEL_RIP + kl["rep_hook"]:#x} 0x40\n'), [],
        lambda c: common(c) + first(c, F1, 1, REP, 0x23, True)
        + ([] if (word(c, M), word(c, M + 8), word(c, M + 16)) == (REP, SRC, DST) else
           [f'RCX, RSI, RDI at the interrupt {[hex(word(c, M + 8 * k) or 0) for k in range(3)]}, want the REP\'s start {[hex(REP), hex(SRC), hex(DST)]}'])
        + ([] if memory(c, DST, 0x1000) == pattern else ['the copy is wrong']))

    # nmi-sysret
    kern, kl = kernel({K: 'nmi_hook: nop\n',
                       K2: 'mov rax, rsp\npush 0x10\npush rax\npushfq\npush 0x08\nlea rax, [rip + iret_to]\npush rax\niretq\niret_to: nop\n'})
    code, lab = program(f'1: inc qword ptr [{UC:#x}]\nsyscall\ndec r8\njnz 1b\n',
                        f'''h2: mov [{SAVE:#x}], rax\ninc qword ptr [{CNT:#x}]\ncmp qword ptr [{CNT:#x}], 1\njne 5f\n{frame(F1, 0)}nmi_hook2: nop
mov eax, 1000\n6: dec eax\njnz 6b
mov rax, [rsp]\nmov [{RESUME:#x}], rax\npush qword ptr [rsp + 16]\npopfq\nmov rsp, [rsp + 24]\nmov rax, [{SAVE:#x}]\njmp qword ptr [{RESUME:#x}]
5: {frame(F2, 0)}mov rax, [{SAVE:#x}]\niretq\n''')

    def nmi_judge(c):
        p = common(c) + first(c, F1, 2, KERNEL_RIP + kl['nmi_hook'] + 1, 0x08, False)
        if word(c, F1 + 24) != K: p.append(f'first NMI in round {word(c, F1 + 24)}, want {K}')
        got, want = (word(c, F2), word(c, F2 + 8), word(c, F2 + 24)), (KERNEL_RIP + kl['iret_to'], 0x08, K2)
        return p + ([] if got == want else [f'second NMI RIP, CS, round {got}, want {want} (right after the IRETQ)'])
    run('nmi-sysret', text(code, kern, lab, [2], f'nmi {KERNEL_RIP + kl["nmi_hook"]:#x}\nnmi {CODE + lab["nmi_hook2"]:#x}\n'), [], nmi_judge)

    # div-frame
    kern, kl = kernel({})
    code, lab = program(f'''mov r10d, 3
1: inc qword ptr [{UC:#x}]\nmov r12, [{UC:#x}]\nmov rax, r12\nmov edx, 1\ntest r12b, 3\njnz 2f\nmov edx, 5
2: syscall
ret_here: div r10
cmp r12, [{UC:#x}]\nje 3f\ninc qword ptr [{BAD:#x}]
3: add [{QSUM:#x}], rax\nadd [{RSUM:#x}], rdx\ncmp qword ptr [{UC:#x}], {N}\njb 1b\n''',
                        f'''h0: push rbx\ninc qword ptr [{CNT:#x}]\nlea rbx, [rip + ret_here]\ncmp [rsp + 8], rbx\njne 8f\ncmp qword ptr [rsp + 16], 0x23\njne 8f
test qword ptr [rsp + 24], 0x200\njz 8f\ncmp qword ptr [rsp + 32], {STACK:#x}\njne 8f\ncmp rax, r12\njne 8f\ncmp rdx, 5\njne 8f
cmp r12, [{UC:#x}]\nje 9f
8: inc qword ptr [{BAD:#x}]
9: add qword ptr [rsp + 8], 3\nxor eax, eax\nxor edx, edx\npop rbx\niretq\n''')
    assert code[lab['ret_here']:lab['ret_here'] + 3] == bytes.fromhex('49f7f2')    # the 3 bytes the handler skips
    live = [k for k in range(1, N + 1) if k % 4]
    want_q, want_r = sum(((1 << 64) + k) // 3 for k in live) % (1 << 64), sum(((1 << 64) + k) % 3 for k in live)
    run('div-frame', text(code, kern, lab, [0]), [],
        lambda c: common(c)
        + ([] if (word(c, CNT), word(c, BAD)) == (N // 4, 0) else [f'{word(c, CNT)} #DE (want {N // 4}), {word(c, BAD)} with a wrong frame or registers'])
        + ([] if (word(c, QSUM), word(c, RSUM)) == (want_q, want_r) else [f'quotient, remainder sums {word(c, QSUM)}, {word(c, RSUM)}, want {want_q}, {want_r}']))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--fexc', type=pathlib.Path, required=True)
    ap.add_argument('--out', type=pathlib.Path, required=True)
    ap.add_argument('--only', type=lambda s: s.split(','), default=['cost', 'rdrand', 'xmc', 'coherence', 'rollback', 'nopruns', 'tsc', 'swapgs', 'sysfast'])
    ap.add_argument('--n', type=int, default=100_000)
    ap.add_argument('--reps', type=int, default=3)
    ap.add_argument('--xmc-rewrites', type=int, default=2000, help='xmc: rewrites, each checked (one per XMC_K or more iterations of vCPU 0)')
    ap.add_argument('--host', type=pathlib.Path, help='the host binary, or the directory holding it (keeps its name out of the command line)')
    ap.add_argument('--runtime', type=pathlib.Path)
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    if a.host and a.host.is_dir(): a.host = a.host / 'natemu-host'
    if not (a.host and a.runtime): a.host, a.runtime = vm.build()
    report, failures = {}, []
    if 'cost' in a.only: failures += cost(a, report)
    if 'rdrand' in a.only: failures += rdrand(a, report)
    if 'xmc' in a.only: failures += xmc(a, report)
    if 'coherence' in a.only: failures += coherence(a, report)
    if 'rollback' in a.only: failures += rollback(a, report)
    if 'nopruns' in a.only: failures += nopruns(a, report)
    if 'tsc' in a.only: failures += tsc(a, report)
    if 'swapgs' in a.only: failures += swapgs(a, report)
    if 'sysfast' in a.only: failures += sysfast(a, report)
    (a.out / 'k2.json').write_text(json.dumps(report, indent=1) + '\n')
    for f in failures: print('FAIL', f)
    sys.exit(1 if failures else 0)


if __name__ == '__main__': main()
