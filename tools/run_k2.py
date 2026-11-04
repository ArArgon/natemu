#!/usr/bin/env python3
"""K2 checks: system instructions run from the decoded record kept in the code cache (boot-features plan, K2).

    tools/with-env python3 tools/run_k2.py --fexc FEXC --out DIR [--only cost,rdrand,xmc,coherence,rollback,nopruns,tsc] [--n 100000] [--reps 3]
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
- `rollback` (perf chunk 12): SYSRET with a non-canonical RCX and WRGSBASE with a non-canonical value fault with the
  state unchanged (see `rollback`).
- `nopruns` (perf chunk 12): a kept run of no-operation instructions crossing into a page made not present, then remapped,
  and rewritten by a peer vCPU, at 1-4 vCPUs (see `nopruns`).
- `tsc` (perf chunk 15): RDTSC translated inline at CPL 0 agrees with the resident's RDTSCP (one clock), follows a WRMSR of
  the TSC, and never runs backwards across vCPUs (see `tsc`).

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
    XC, P2, P2NEW = 0x1ffffe, 0x200000, 0x230000
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
         tables({}) + [(pte(0x1ff000), (0x1ff000 | LEAF).to_bytes(8, 'little')), (0x1ff000, bytes(0xffe) + bytes.fromhex('0f01')),
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


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--fexc', type=pathlib.Path, required=True)
    ap.add_argument('--out', type=pathlib.Path, required=True)
    ap.add_argument('--only', type=lambda s: s.split(','), default=['cost', 'rdrand', 'xmc', 'coherence', 'rollback', 'nopruns', 'tsc'])
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
    if 'rollback' in a.only: failures += rollback(a, report)
    if 'nopruns' in a.only: failures += nopruns(a, report)
    if 'tsc' in a.only: failures += tsc(a, report)
    (a.out / 'k2.json').write_text(json.dumps(report, indent=1) + '\n')
    for f in failures: print('FAIL', f)
    sys.exit(1 if failures else 0)


if __name__ == '__main__': main()
