#!/usr/bin/env python3
"""K2 checks: system instructions run from the decoded record kept in the code cache (boot-features plan, K2).

    tools/with-env python3 tools/run_k2.py --fexc FEXC --out DIR [--only cost,rdrand,xmc,coherence,rollback,nopruns,tsc,swapgs,sysfast,stifirst,stmxcsr,repbulk,cr2,verw,gather] [--n 100000] [--reps 3]
        [--xmc-rewrites N] [--host HOST --runtime RUNTIME]

- `cost`: microseconds per loop iteration of one system instruction (CLI, STI, PUSHFQ+POPFQ, SWAPGS, RDTSC, CPUID,
  WRMSR KERNEL_GS_BASE, FLD+FSTP m80) at CPL 0 on 1 vCPU, as the slope (t(2N) - t(N)) / N of the driver's wall time
  (start-up and compiles fall into the intercept); every run must reach its end. With the stats `decode_exits` per
  run: after K2 a loop's decode exits do not grow with N. Perf chunk 17: `syscall`, one SYSCALL -> kernel -> SYSRET round trip
  from a CPL-3 loop, the kernel side shaped as Linux's (`endbr64; swapgs; ...; swapgs; 7-byte NOP; sysretq`). Perf P2:
  `verw` and `verw_null` (VERW m16 of a writable data selector, inline, and of the null one, the resident's), and
  `syscall_verw` (the round trip with Linux's VERW before SYSRETQ in place of the NOP).
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
- `stifirst` (perf chunk 25): an STI that starts its block, translated inline at CPL 0, keeps STI's semantics with an
  interrupt or NMI held at it, entered through a link, after MOV SS, under TF, after rejected frame-alias stores at CPL 0
  or CPL 3, and under interrupts and NMIs at random times (see `stifirst`).
- `stmxcsr` (perf chunk 25): STMXCSR and VSTMXCSR, translated, store MXCSR with the sticky flags the code before them raised,
  as the resident's FXSAVE (its bank) does and as the SDM gives them, at both CPLs and block sizes (see `stmxcsr`).
- `repbulk` (perf chunk 26): REP STOS stored in bulk inside a page keeps the SDM's semantics: every width, alignment, count,
  page crossing and DF; faults mid-string; interrupts at random times; TF; a data breakpoint; UC memory; writes into
  translated code (see `repbulk`).
- `cr2` (perf chunk 26): MOV r64, CR2 translated inline at CPL 0 reads CR2 after #PF deliveries (nested too), MOV to CR2, under
  TF; at CPL 3 it is #GP(0), as is a guest load from the frame alias (see `cr2`).
- `verw` (perf P2): VERW m16 translated inline at CPL 0 gives the SDM's ZF for every selector class, form and table change,
  faults as the resident's, and leaves every other flag (see `verw`).
- `gather` (correctness pass, finding 2): the AVX2 gathers, the resident's element-by-element transaction: every form, width,
  scale and mask pattern, faults on each element with the partial state and the restart, #UD and gating, data breakpoints
  (the trap in lieu of a fault), TF, UC and device pages, TSO (see `gather`).

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
    # Perf chunk 15: STI mid-block at CPL 0 is translated inline (perf chunk 25: so is the row above, which starts its block).
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
    # Perf chunk 26: 4 KiB of REP STOSQ and REP STOSB (RDI page-aligned), one iteration each.
    'stosq_4k': ('mov edi, 0x300000\nmov ecx, 512\nrep stosq', {}),
    'stosb_4k': ('mov edi, 0x300000\nmov ecx, 4096\nrep stosb', {}),
    # Perf P2: VERW m16 at CPL 0 of the driver's DPL-0 data selector (inline), and of the null selector (ZF = 0: the
    # resident's); each after the store of its selector. `syscall_verw`: `syscall` with Linux's VERW before SYSRETQ.
    'verw': ('mov word ptr [rbx], 0x10\nverw word ptr [rbx]', {3: DATA + 16}),
    'verw_null': ('mov word ptr [rbx], 0\nverw word ptr [rbx]', {3: DATA + 16}),
    'syscall_verw': (None, {}),
}
USER_GS, KERNEL_GS, KERNEL_RIP = 0x2a0000, 0x2b0000, 0x200000
# The noncanonical guest-state alias: guest accesses must #GP; generated frame-relative accesses remain internal.
# The suite selects a distant alias for these fixed-address GP controls. The actual selected-page
# hole is exercised by run_frame_randomization.py and the preserved original containment probes.
GUEST_STATE_VA = 0xffff_4000_0000_1000
# Linux's entry_SYSCALL_64 and its SYSRET exit, in shape: ENDBR64; SWAPGS; the user RSP saved and the kernel's loaded through
# GS; ...; SWAPGS; a 7-byte NOP; SYSRETQ.
LINUX_KERNEL = ('endbr64\nswapgs\nmov gs:[16], rsp\nmov rsp, gs:[24]\npush rcx\npop rcx\nmov rsp, gs:[16]\nswapgs\n'
                '.byte 0x0f,0x1f,0x80,0,0,0,0\nsysretq')
# Perf P2: the same with Linux's MDS clearing (mitigations on, MDS_NO clear): VERW of __KERNEL_DS (here the driver's 0x10)
# RIP-relative after the last SWAPGS, as `CLEAR_CPU_BUFFERS` before SYSRETQ.
LINUX_KERNEL_VERW = ('endbr64\nswapgs\nmov gs:[16], rsp\nmov rsp, gs:[24]\npush rcx\npop rcx\nmov rsp, gs:[16]\nswapgs\n'
                     'verw word ptr [rip + kds]\nsysretq\nkds: .word 0x10')


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
    run = subprocess.run([a.host, a.runtime, 'run-program', a.fexc, work / 'program.txt', work, *vm.frame_alias_args(), '--watchdog-ms', '600000', *map(str, extra)],
                         capture_output=True, text=True, timeout=900)
    dt = time.perf_counter() - t0
    return dt, (json.loads(run.stdout) if run.returncode == 0 else None), run.stderr[-1500:]


def gprs(values):
    g = [0] * 16
    g[4] = STACK
    for r, v in values.items(): g[r] = v
    return 'gpr ' + ' '.join(f'{v:#x}' for v in g)


def gp_at(out, rip, regs, cpl, flags=2, cr2=None):
    """An intercepted #GP must identify the rejected instruction and preserve its inputs."""
    fault = [int(x, 0) for x in out.get('fault', [])]
    frame = [int(x, 0) if isinstance(x, str) else x for x in out.get('delivered', {}).get('frame', [])]
    g = [int(x, 0) for x in out['gpr']]
    ok = (out.get('frame_intact', False) and out['stop'] == 'guest_fault' and fault == [13, 0, 0, rip] and int(out['rip'], 0) == rip
          and len(frame) >= 3 and frame[1] & 3 == cpl and frame[2] & ~0x10000 == flags
          and all(g[r] == value for r, value in regs.items())
          and (cr2 is None or int(out.get('delivered', {}).get('cr2', '-1'), 0) == cr2))
    return [] if ok else [f"#GP state: stop {out['stop']} fault {fault} frame {frame} regs {[hex(x) for x in g]} delivered {out.get('delivered')}"]


def cost(a, report):
    failures, rows = [], {}
    for name, (body, regs) in COSTS.items():
        if a.cost_rows and name not in a.cost_rows: continue
        code = ref.assemble(f'1: {body}\ndec r8\njnz 1b') if body else None
        times, stats = {a.n: [], 2 * a.n: []}, {}
        for rep in range(a.reps + 1):
            for n in (a.n, 2 * a.n):
                text = (f'rip {CODE:#x}\nrflags 0x2\n{gprs({8: n, **regs})}\ncode {code.hex()}\ncpl 0\nif 0\n'
                        f'memory {DATA:#x} {F80_ONE.hex()}\nregion 0x3e0000 0x10000\n') if body else syscall_program('1: syscall\ndec r8\njnz 1b', LINUX_KERNEL_VERW if name == 'syscall_verw' else LINUX_KERNEL, {8: n})
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
    - `contain-syscall`: CPL 3 SYSCALLs; the kernel's SWAPGS loads the real KERNEL_GS_BASE and RDMSR reads B.
    - `contain-load/store`: CPL-3 accesses to the noncanonical KERNEL_GS_BASE alias #GP(0) at that instruction,
      preserving the destination, following instruction's sentinel and both banked bases (default and block size 1)."""
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
    # Keep the legitimate CPL-3 syscall/SWAPGS path, then independently reject both alias access intents.
    kernel = ('mov r13, rcx\nswapgs\nmov r9, gs:[0]\nswapgs\nmov ecx, 0xc0000102\nrdmsr\nshl rdx, 32\nor rax, rdx\n'
              'mov r12, rax\nmov rcx, r13\nsysretq')
    run('contain-syscall', syscall_program('syscall', kernel, {}, markers), [],
        lambda out, g: ([] if out['stop'] == 'reached' and (g[9], g[12]) == (0xbbbb, B) else
                        [f"stop {out['stop']} kernel GS:[0] {g[9]:#x} RDMSR {g[12]:#x}"]) + bases(out, A, B))
    field = GUEST_STATE_VA + 0xa0
    for intent, ins in [('load', 'mov r10, [rax]'), ('store', 'mov qword ptr [rax], 0x123000')]:
        code, lab = asm(wrmsr(0xc0000101, A) + wrmsr(0xc0000102, B)
                        + f'push 2\npopfq\nsw: nop\nmov rax, {field:#x}\nx: {ins}\ninc r11', labels=True)
        for block in (None, 1):
            run(f'contain-{intent}-{block}', f'rip {CODE:#x}\n{gprs({10: 0x12345678, 11: 0x87654321})}\ncode {code.hex()}\n{head}'
                f'switch {CODE + lab["sw"]:#x} 3\n', [block] if block else [],
                lambda out, g, rip=CODE + lab['x']: gp_at(out, rip, {0: field, 10: 0x12345678, 11: 0x87654321}, 3) + bases(out, A, B))
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


def stifirst(a, report):
    """Perf chunk 25: an STI that starts its block is translated inline at CPL 0 (the block marked; where its slow path would
    exit to the block's own start the resident runs the STI itself). Judged by the SDM (STI, Vol.3 7.8.1, 7.9 Table 7-2;
    MOV SS; 18.3.1.4) and natemu's delivery model (both shadows block NMIs too), natemu only, every case at block sizes
    default and 1 (where the STI is alone, the resident's) unless noted. CPL 0, IF = 0, the program's own IDT (gates on
    IST 1): the interrupt (0x40) and NMI handlers count, record the first frame (RIP, CS, RFLAGS) and R9, R10 at entry, and
    count as bad a delivery at a forbidden boundary (for 0x40 also one with IF = 0 in the frame); the #DB handler records the
    RIPs of its first 8 frames. At the default block size no STI may be decoded (it is translated, first in its block).
    - `held-find`: a hook posts 0x40, then `sti; inc r9; inc r10`: taken once after `inc r9` (R9 1, R10 0).
    - `held-linked`: 2,000 rounds `jmp x; x: sti; inc r9; inc r10; cli`; in round 1,001 a hook posts 0x40 before the `jmp`,
      which by then is linked to the STI's block, entered with the event held: taken once after `inc r9` (R9 = R10 + 1).
    - `nmi-held`: hooks post 0x40, then an NMI, then the STI: the NMI at the STI (IF = 0 in its frame), then 0x40 after
      `inc r9`.
    - `mov-ss`: a hook posts 0x40, `mov ss, eax; sti; inc r9; inc r10`: 0x40 after `inc r9`.
    - `tf`: POPFQ sets TF, then `sti; nop; nop; pushfq; and; popfq` (clearing it): #DB traps after each of the six (each
      instruction stepped alone, compiled without `--cpl0`: the STI decoded).
    - `forge-pending`: 1,000 CPL-0 stores to the event-pending alias each #GP(0). The handler validates RIP, CS, error and
      pre-fault flags, skips the rejected store, and returns to `jmp x; x: sti; inc r9; cli`: every round completes.
    - `cpl3-forge` (default size): both CPL-3 stores (pending=1, IF=0) #GP(0) and are skipped by the same checked handler;
      a hook then posts 0x40 (taken at CPL 3 at once), followed by SYSCALL into `sti; pushfq; pop r12; cli; sysretq`:
      IF = 1 after the kernel's STI and back at CPL 3. Rejected stores cannot forge pending state or clear IF.
    - `kicks-jmp`, `kicks-ret`, `kicks-movss` (default size): 4,000,000 rounds `cli; inc [W1]; X; sti; inc [W2]` with X a
      `jmp` to the STI, a `call` to a `ret` returning to it, or `mov eax, ss; mov ss, eax`, under 0x40 and NMIs posted at
      random times: 0x40 never with IF = 0 nor right after the STI, an NMI never right after the STI (nor at it after MOV
      SS), W1 = W2 = 4,000,000, more than 100 of each taken.
    - `sysfast-held` (review): SYSCALL's fast path enters a kernel side starting with STI with 0x40 held (see below).
    - `reuse-copy` (review): an STI-first block reached as an in-resident copy of a template at another RIP (`reuse_copy`)."""
    asm, failures, runs = ref.assemble, [], []
    TABLES, IDTR = 0x300000, 0x300800
    SLOT = {1: 0x300900, 2: 0x300980, 0x40: 0x3009c0}
    BAD, FORBID, FORBIDN, W1, W2 = 0x300a00, 0x300a40, 0x300a60, 0x300a80, 0x300a88
    PEND, IFB, GPLOG = GUEST_STATE_VA + 0x406, GUEST_STATE_VA + 0x3f9, 0x300b00
    def handler(v):
        s = SLOT[v]
        if v == 1:
            return (f'h1:\npush rax\npush rcx\nmov rcx, [{s:#x}]\ncmp rcx, 8\njae 2f\nmov rax, [rsp + 16]\nmov [{s + 8:#x} + rcx * 8], rax\n'
                    f'2: inc qword ptr [{s:#x}]\npop rcx\npop rax\niretq\n')
        forbid = FORBID if v == 0x40 else FORBIDN
        checks = ''.join(f'mov rax, [{forbid + 8 * k:#x}]\ntest rax, rax\njz 3f\ncmp rax, [rsp + 8]\njne 3f\ninc qword ptr [{BAD:#x}]\n3:\n' for k in range(4))
        if v == 0x40:
            checks += f'test qword ptr [rsp + 24], 0x200\njnz 3f\ninc qword ptr [{BAD:#x}]\n3:\n'
        first = ''.join(f'mov rax, [rsp + {8 + 8 * k}]\nmov [{s + 8 + 8 * k:#x}], rax\n' for k in range(3)) + f'mov [{s + 32:#x}], r9\nmov [{s + 40:#x}], r10\n'
        eoi = 'push rcx\npush rdx\nmov ecx, 0x80b\nxor eax, eax\nxor edx, edx\nwrmsr\npop rdx\npop rcx\n' if v == 0x40 else ''
        return f'h{v}:\npush rax\ninc qword ptr [{s:#x}]\ncmp qword ptr [{s:#x}], 1\njne 2f\n{first}2:\n{checks}{eoi}pop rax\niretq\n'
    setup = (f'lidt [{IDTR:#x}]\nmov ecx, 0x1b\nmov eax, 0xfee00d00\nxor edx, edx\nwrmsr\nmov ecx, 0x80f\nmov eax, 0x1ff\nwrmsr\n')
    def run(name, body, want, block=None, posts=(), forbid=(), forbidn=(), extra=(), user=None, check=None, decoded=False, gp=()):
        # Only these alias stores are recoverable: validate every frame before changing its return RIP.
        h13 = ''
        if gp:
            h13 = (f'h13: push rax\ncmp qword ptr [rsp + 8], 0\njne gp_bad\ncmp qword ptr [rsp + 24], {0x23 if user else 8}\njne gp_bad\n'
                   'mov rax, [rsp + 32]\nand rax, -0x10001\ncmp rax, r14\njne gp_bad\n')
            for i, (fault, resume, count) in enumerate(gp):
                h13 += (f'lea rax, [rip + {fault}]\ncmp rax, [rsp + 16]\njne gp_next{i}\ninc qword ptr [{GPLOG + 8 * i:#x}]\n'
                        f'lea rax, [rip + {resume}]\nmov [rsp + 16], rax\npop rax\nadd rsp, 8\niretq\ngp_next{i}:\n')
            h13 += f'gp_bad: inc qword ptr [{BAD:#x}]\npop rax\njmp done\n'
        code, lab = asm(setup + (user or '') + body + '\njmp done\n' + ''.join(handler(v) for v in SLOT) + h13 + 'done:', labels=True)
        at = lambda l: CODE + lab[l]
        gates = bytearray(16 * 0x41)
        for v in (*SLOT, *((13,) if gp else ())):
            h = at(f'h{v}')
            # IST 1 only where an event may come from CPL 3 (the driver's TSS); else none, so an NMI may nest in the 0x40 handler.
            gates[16 * v:16 * v + 16] = ((h & 0xffff | 0x08 << 16 | (1 if user else 0) << 32 | 0x8e << 40 | (h >> 16 & 0xffff) << 48).to_bytes(8, 'little')
                                         + (h >> 32).to_bytes(8, 'little'))
        tables = bytes(gates) + bytes(IDTR - TABLES - len(gates)) + (0x41 * 16 - 1).to_bytes(2, 'little') + TABLES.to_bytes(8, 'little')
        tables += bytes(FORBID - TABLES - len(tables)) + b''.join(at(l).to_bytes(8, 'little') for l in forbid) + bytes(8 * (4 - len(forbid)))
        tables += bytes(FORBIDN - TABLES - len(tables)) + b''.join(at(l).to_bytes(8, 'little') for l in forbidn) + bytes(8 * (4 - len(forbidn)))
        tables += bytes(max(W2 + 8, GPLOG + 8 * len(gp) if gp else 0) - TABLES - len(tables))
        text = (f'rip {CODE:#x}\nrflags 0x2\n{gprs({})}\ncode {code.hex()}\ncpl 0\nif 0\nsvr 0x1ff\nmemory {TABLES:#x} {tables.hex()}\n'
                f'region 0x3e0000 0x10000\n' + ''.join(f'{"nmi" if v == 2 else "post"} {at(l):#x}{"" if v == 2 else f" {v:#x}"}\n' for l, v in posts))
        tag = f'{name}-{block}'
        _, out, err = driver(a, text, a.out / 'stifirst' / tag, [*([block] if block else []), '--watchdog-ms', '60000', *extra])
        problems = []
        if not out:
            problems.append(f'driver: {err}')
        else:
            def mem(addr):
                return next((int.from_bytes(bytes.fromhex(m['data'])[addr - int(m['addr'], 0):][:8], 'little') for m in out['memory']
                             if int(m['addr'], 0) <= addr < int(m['addr'], 0) + len(m['data']) // 2), None)
            g = [int(x, 0) for x in out['gpr']]
            if out['stop'] != 'reached':
                problems.append(f"stop {out['stop']} {out.get('reason')} rip {out.get('rip')}")
            for v, w in want.items():
                s = SLOT[v]
                if v == 1:
                    got = [mem(s)] + [mem(s + 8 + 8 * k) for k in range(min(mem(s) or 0, 8))]
                    exp = [len(w)] + [at(l) for l in w]
                    if got != exp: problems.append(f'#DB count and RIPs {[hex(x) for x in got]}, want {[hex(x) for x in exp]}')
                    continue
                count, rip, cs, rflags, r9, r10 = (mem(s + 8 * k) for k in range(6))
                if callable(w):
                    problems += w(count, rip, cs, rflags, r9, r10)
                    continue
                c, l, if_, regs = w
                if (count, rip) != (c, at(l) if l else 0) or (c and (bool(rflags & 0x200) != if_ or (regs and regs(r9, r10)))):
                    problems.append(f'vector {v:#x}: count {count} RIP {rip:#x} RFLAGS {rflags:#x} r9 {r9} r10 {r10}, want {c} at {l} IF {if_}')
            for v in SLOT:
                if v not in want and mem(SLOT[v]):
                    problems.append(f'vector {v:#x} delivered {mem(SLOT[v])} times')
            if mem(BAD):
                problems.append(f'{mem(BAD)} deliveries at a forbidden boundary or with IF = 0')
            if gp and not out.get('frame_intact', False): problems.append('frame sentinel changed')
            for i, (fault, resume, count) in enumerate(gp):
                if mem(GPLOG + 8 * i) != count:
                    problems.append(f'#GP at {fault}: {mem(GPLOG + 8 * i)} deliveries, want {count}')
            if check: problems += check(g, mem)
            if not block and not decoded and out['stats']['decodes'].get('0x41'):
                problems.append(f"STI decoded {out['stats']['decodes']['0x41']} times: not translated")
        taken = {f'{v:#x}': mem(SLOT[v]) for v in SLOT} if out else {}
        runs.append({'case': tag, 'ok': not problems, 'problems': problems, 'taken': taken,
                     **({'gp': {fault: mem(GPLOG + 8 * i) for i, (fault, _, _) in enumerate(gp)}} if out and gp else {})})
        print(json.dumps(runs[-1]), flush=True)
        if problems: failures.append(f'stifirst {tag}: {problems}')
    shadow = 'x: sti\na: inc r9\nb: inc r10\n'
    after = (1, 'b', True, None)
    for block in (None, 1):
        run('held-find', 'hk: nop\n' + shadow, {0x40: (1, 'b', True, lambda r9, r10: (r9, r10) != (1, 0))}, block, [('hk', 0x40)], ['x', 'a'])
        run('held-linked', 'mov r8, 2000\nlp: cmp r8, 1000\njne sk\nhk: nop\nsk: jmp x\n' + shadow + 'cli\ndec r8\njnz lp\n',
            {0x40: (1, 'b', True, lambda r9, r10: r9 != r10 + 1)}, block, [('hk', 0x40)], ['x', 'a'],
            check=lambda g, mem: [] if (g[9], g[10]) == (2000, 2000) else [f'r9 {g[9]} r10 {g[10]}'])
        run('nmi-held', 'hk: nop\nhn: nop\n' + shadow, {2: (1, 'x', False, None), 0x40: after}, block, [('hk', 0x40), ('hn', 2)], ['x', 'a'], ['a'])
        run('mov-ss', 'mov eax, ss\nhk: nop\nmov ss, eax\n' + shadow, {0x40: after}, block, [('hk', 0x40)], ['x', 'a'])
        run('tf', 'pushfq\nor qword ptr [rsp], 0x100\npopfq\nx: sti\na: nop\nb: nop\nc: pushfq\nd: and qword ptr [rsp], -0x101\ne: popfq\nf: nop\n',
            {1: ['a', 'b', 'c', 'd', 'e', 'f']}, block, decoded=True)
        run('forge-pending', f'mov r8, 1000\nmov rbx, {PEND:#x}\nlp: pushfq\npop r14\nforge: mov byte ptr [rbx], 1\nresume: jmp x\nx: sti\na: inc r9\ncli\ndec r8\njnz lp\n', {}, block,
            check=lambda g, mem: [] if (g[8], g[9], g[3]) == (0, 1000, PEND) else [f'r8 {g[8]} r9 {g[9]} rbx {g[3]:#x}'],
            gp=[('forge', 'resume', 1000)])
    # CPL 3 attempts both stores (each rejected and skipped), then SYSCALL into a kernel side that starts with STI (STAR: SYSCALL CS 0x08, SYSRET CS 0x23).
    sysc = ('mov ecx, 0xc0000080\nrdmsr\nor eax, 1\nwrmsr\n' + wrmsr(0xc0000081, 0x10 << 48 | 0x08 << 32) + 'lea rax, [rip + x]\nmov rdx, rax\nshr rdx, 32\n'
            'mov ecx, 0xc0000082\nwrmsr\n' + wrmsr(0xc0000084, 0x47700) + 'lea rcx, [rip + user]\nmov r11d, 0x202\nsysretq\n'
            f'user:\nmov rax, {PEND:#x}\npushfq\npop r14\nforge_pending: mov byte ptr [rax], 1\nresume_pending: mov rax, {IFB:#x}\npushfq\npop r14\nforge_if: mov byte ptr [rax], 0\nhk: nop\nu1: syscall\npushfq\npop r13\njmp done\n'
            'x: sti\npushfq\npop r12\ncli\nsysretq\n')
    run('cpl3-forge', '', {0x40: lambda c, rip, cs, rf, r9, r10: [] if (c, cs) == (1, 0x23) and rf & 0x200 else [f'0x40: count {c} CS {cs:#x} RFLAGS {rf:#x}']},
        None, [('hk', 0x40)], user=sysc,
        check=lambda g, mem: [] if g[12] & 0x200 and g[13] & 0x200 else [f'r12 {g[12]:#x} r13 {g[13]:#x}: IF lost'],
        gp=[('forge_pending', 'resume_pending', 1), ('forge_if', 'hk', 1)])
    # Review: SYSCALL's fast path into a kernel side that starts with STI, with 0x40 held (posted at CPL 3 with IF = 0, which
    # the SYSRET's R11 left; FMASK clears IF anyway), the SYSCALL a literal exit of the block after the hook (so the fast
    # path runs it): the fast path enters the kernel's block directly, its STI exits to its own start and the resident runs
    # it: 0x40 once, after the kernel's `inc r9`. 2,000 round trips, the post in round 1,001.
    held = ('mov ecx, 0xc0000080\nrdmsr\nor eax, 1\nwrmsr\n' + wrmsr(0xc0000081, 0x10 << 48 | 0x08 << 32) + 'lea rax, [rip + x]\nmov rdx, rax\nshr rdx, 32\n'
            'mov ecx, 0xc0000082\nwrmsr\n' + wrmsr(0xc0000084, 0x47700) + 'lea rcx, [rip + user]\nmov r11d, 0x2\nsysretq\n'
            'user:\nmov r8, 2000\n1: cmp r8, 1000\njne 2f\nhk: nop\n2: inc r12\nsyscall\ndec r8\njnz 1b\njmp done\n'
            'x: sti\na: inc r9\nb: inc r10\ncli\nsysretq\n')
    for block in (None, 1):
        run('sysfast-held', '', {0x40: lambda c, rip, cs, rf, r9, r10, b=block: [] if (c, cs) == (1, 0x08) and rf & 0x200 and r9 == r10 + 1 else
                                 [f'0x40: count {c} CS {cs:#x} RFLAGS {rf:#x} r9 {r9} r10 {r10}']},
            block, [('hk', 0x40)], ['x', 'a'], user=held,
            check=lambda g, mem: [] if (g[9], g[10]) == (2000, 2000) else [f'r9 {g[9]} r10 {g[10]}'])
    reuse_copy(a, runs, failures, handler, setup, SLOT, BAD, TABLES, IDTR)
    kick = ['--kick-vcpu', '0', '--kicks', '5000', '--kick-every-us', '5', '--kick-jitter-us', '10', '--kick-events', '0x40,nmi']
    some = lambda c, *_: [] if c and c > 100 else [f'only {c} taken']
    for name, x, nmi in (('kicks-jmp', 'jmp x\n', ['a']), ('kicks-ret', 'call f\n', ['a']), ('kicks-movss', 'mov eax, ss\nmov ss, eax\n', ['x', 'a'])):
        n = 4_000_000
        exact = lambda g, mem, n=n: [] if (mem(W1), mem(W2)) == (n, n) else [f'W1 {mem(W1)} W2 {mem(W2)}']
        # (After MOV SS an event held by its shadow steps the STI alone, compiled without `--cpl0`: decoded.)
        run(name, f'mov r8, {n}\nlp: cli\ninc qword ptr [{W1:#x}]\n{x}x: sti\na: inc qword ptr [{W2:#x}]\ndec r8\njnz lp\njmp done\nf: ret\n',
            {0x40: some, 2: some}, None, [], ['x', 'a'], nmi, kick, check=exact, decoded=name == 'kicks-movss')
    report['stifirst'] = {'runs': runs, 'failures': failures}
    return failures


def reuse_copy(a, runs, failures, handler, setup, SLOT, BAD, TABLES, IDTR):
    """Perf chunk 25 review, `stifirst` `reuse-copy` (default block size): the guest's own identity tables (as `walk-ad`) map a
    frame F at two upper-half linear pages L1 and L2, F holding `sti; inc r9; inc r10; jmp r13`. Run once through F (the host's block,
    a template, run through L1), IF = 0 again, a hook posts 0x40, then a jump to L2: the resident copies the template (perf chunk 10, same
    frame, page offset and half), the copy keeps STI_FIRST and its STI runs as the resident's: 0x40 once at L2 + 4 (after
    `inc r9`, R9 2, R10 1). The driver must report the copy (its k1 `reuse.copies` at least 1)."""
    asm = ref.assemble
    # Frame F at two upper-half pages L1 and L2 (FEX moves a RIP below 4 GiB with 32-bit `movz/movk w0`, which the relocator
    # does not rewrite, so a low template is never copied; the kernel's are 64-bit moves).
    F, L1, L2 = 0x104000, 0xffff_ffff_8010_4000, 0xffff_ffff_8070_4000
    PML4, PDPT, PD, PT0, PT1, PT3 = 0x110000, 0x111000, 0x112000, 0x113000, 0x114000, 0x115000
    PDPTH, PDH, PTH0, PTH3 = 0x116000, 0x117000, 0x118000, 0x119000
    NONLEAF, WSTACK = P.P | P.RW | P.US | P.A, 0x3d0000
    code, lab = asm(f'mov eax, {PML4:#x}\nmov cr3, rax\n' + setup + f'lea r13, [rip + v0]\nmov rax, {L1:#x}\njmp rax\nv0: cli\nhk: nop\n'
                    f'lea r13, [rip + v1]\nmov rax, {L2:#x}\njmp rax\nv1: jmp done\n' + ''.join(handler(v) for v in SLOT) + 'done:', labels=True)
    page = asm('sti\ninc r9\ninc r10\njmp r13')
    gates = bytearray(16 * 0x41)
    for v in SLOT:
        h = CODE + lab[f'h{v}']
        gates[16 * v:16 * v + 16] = ((h & 0xffff | 0x08 << 16 | 0x8e << 40 | (h >> 16 & 0xffff) << 48).to_bytes(8, 'little') + (h >> 32).to_bytes(8, 'little'))
    tables_mem = bytes(gates) + bytes(IDTR - TABLES - len(gates)) + (0x41 * 16 - 1).to_bytes(2, 'little') + TABLES.to_bytes(8, 'little')
    tables_mem += bytes(BAD + 0x100 - TABLES - len(tables_mem))
    tables = {PML4: {0: PDPT | NONLEAF, 511: PDPTH | NONLEAF}, PDPT: {0: PD | NONLEAF}, PD: {0: PT0 | NONLEAF, 1: PT1 | NONLEAF},
              PDPTH: {510: PDH | NONLEAF}, PDH: {0: PTH0 | NONLEAF, 3: PTH3 | NONLEAF},
              PTH0: {(L1 >> 12) & 511: F | P.LEAF}, PTH3: {(L2 >> 12) & 511: F | P.LEAF},
              PT0: {(v >> 12) & 511: v | P.LEAF for v in (CODE, CODE + 0x1000)},
              PT1: {(v >> 12) & 511: v | P.LEAF for v in [TABLES, *range(0x3c0000, WSTACK, 0x1000), *(0x3e8000 + o for o in (0, 0x1000, 0x3000, 0x4000, 0x5000, 0x6000))]},
              }
    items = ''.join(f'memory {t_:#x} {b"".join(e.get(i, 0).to_bytes(8, "little") for i in range(512)).hex()}\n' for t_, e in tables.items())
    text = (f'identity\nrip {CODE:#x}\nrflags 0x2\n{gprs({4: WSTACK})}\ncode {code.hex()}\nbody {F:#x} {page.hex()}\ncpl 0\nif 0\nsvr 0x1ff\n'
            f'memory {TABLES:#x} {tables_mem.hex()}\n{items}region 0x3c0000 {WSTACK - 0x3c0000:#x}\npost {CODE + lab["hk"]:#x} 0x40\n')
    _, out, err = driver(a, text, a.out / 'stifirst' / 'reuse-copy', ['--watchdog-ms', '60000'])
    problems = []
    if not out:
        problems.append(f'driver: {err}')
    else:
        def mem(addr):
            return next((int.from_bytes(bytes.fromhex(m['data'])[addr - int(m['addr'], 0):][:8], 'little') for m in out['memory']
                         if int(m['addr'], 0) <= addr < int(m['addr'], 0) + len(m['data']) // 2), None)
        s = SLOT[0x40]
        count, rip, cs, rflags, r9, r10 = (mem(s + 8 * k) for k in range(6))
        copies = out['stats']['k1']['reuse']['copies']
        if out['stop'] != 'reached': problems.append(f"stop {out['stop']} {out.get('reason')} rip {out.get('rip')}")
        if (count, rip, r9, r10) != (1, L2 + 4, 2, 1): problems.append(f'0x40: count {count} RIP {rip} r9 {r9} r10 {r10}, want 1 at {L2 + 4:#x}, 2, 1')
        if not copies: problems.append(f'no in-resident copy made ({copies})')
        if mem(BAD): problems.append(f'{mem(BAD)} bad deliveries')
    runs.append({'case': 'reuse-copy-None', 'ok': not problems, 'problems': problems})
    print(json.dumps(runs[-1]), flush=True)
    if problems: failures.append(f'stifirst reuse-copy: {problems}')


def stmxcsr(a, report):
    """Perf chunk 25: STMXCSR and VSTMXCSR are translated (no decode of either) and store MXCSR with the exception flags
    generated code raised since the last bank (FPSR and the SIMD flag byte), DAZ, FTZ, RC and the masks as loaded. Judged by
    the SDM (Vol.1 10.2.3, 11.5.2: the flags each operation sets, masked responses; LDMXCSR; STMXCSR) and against the
    resident's own bank: every store is followed by an FXSAVE (a resident transaction), whose MXCSR must be the same value.
    Each case at CPL 0 and 3 and block sizes default and 1; a case is a series of `ldmxcsr CTL; OP; [V]STMXCSR [OUT+4k];
    fxsave [FX+512k]` steps (OP's flags, from operands in memory, raised on a fresh MXCSR):
    - `flags`: SQRTSS of -1 (IE), 1 + the smallest denormal (DE, PE), 1 / 0 (ZE), FLT_MAX * 2 (OE, PE), FLT_MIN * FLT_MIN
      (UE, PE), 1 + 2^-30 (PE), the last by VSTMXCSR; and IE, ZE and PE raised in three blocks (a JMP, then a CPUID between):
      all three stored.
    - `controls`: LDMXCSR of every flag set, of FTZ | RC 11 | DAZ, of RC 01: stored as loaded; DAZ with a denormal operand
      (no DE, exact); FTZ with FLT_MIN * 0.5 (zero, UE, PE); ZM unmasked with an inexact add (PE through the SIMD flag byte).
    - `place`: a store to an odd address and one crossing into the next page (the bytes, no fault); at CPL 3 with CR0.AM and
      RFLAGS.AC an odd address is #AC(0) at the STMXCSR, nothing stored.
    - `link` (review): ZE raised before a JMP linked to the STMXCSR's block (no resident entry between): 50 stores of it.
    - `resumes` (review): ZE, then in the same block an x87 FSQRT of -1 (its IE is FSW's only), a 128/64 DIV through the
      division helper (resumed mid-block), or a first load from a fresh page (a shadow miss, retried): ZE alone stored."""
    asm, failures, runs = ref.assemble, [], []
    D, OUT, FX = 0x300000, 0x300400, 0x302000
    consts = {'m1': 0xbf800000, 'one': 0x3f800000, 'den': 1, 'zero': 0, 'max': 0x7f7fffff, 'two': 0x40000000, 'min': 0x00800000,
              'tiny': 0x30800000, 'half': 0x3f000000}
    at = {k: D + 4 * i for i, k in enumerate(consts)}
    CTL = D + 0x100
    ld = lambda r, k: f'movss {r}, dword ptr [{at[k]:#x}]\n'
    ops = {'ie': ld('xmm1', 'm1') + 'sqrtss xmm0, xmm1', 'de': ld('xmm0', 'one') + ld('xmm1', 'den') + 'addss xmm0, xmm1',
           'ze': ld('xmm0', 'one') + ld('xmm1', 'zero') + 'divss xmm0, xmm1', 'oe': ld('xmm0', 'max') + ld('xmm1', 'two') + 'mulss xmm0, xmm1',
           'ue': ld('xmm0', 'min') + ld('xmm1', 'min') + 'mulss xmm0, xmm1', 'pe': ld('xmm0', 'one') + ld('xmm1', 'tiny') + 'addss xmm0, xmm1',
           'half': ld('xmm0', 'min') + ld('xmm1', 'half') + 'mulss xmm0, xmm1', 'none': 'nop'}
    def run(name, steps, cpl, block, extra_lines='', tail='', check=None):
        """steps: (ctl, op asm, store mnemonic, want)."""
        body, ctls = '', []
        for k, (ctl, op, store, want) in enumerate(steps):
            ctls.append(ctl)
            body += (f'ldmxcsr dword ptr [{CTL + 4 * k:#x}]\n{op}\n{store} dword ptr [{OUT + 4 * k:#x}]\nfxsave [{FX + 512 * k:#x}]\n')
        code, lab = asm(body + tail + '\nnop', labels=True)
        data = b''.join(v.to_bytes(4, 'little') for v in consts.values())
        data += bytes(CTL - D - len(data)) + b''.join(c.to_bytes(4, 'little') for c in ctls)
        text = (f'rip {CODE:#x}\nrflags 0x2\n{gprs({})}\ncode {code.hex()}\ncpl {cpl}\nsse 1\nxcr0 7\nmemory {D:#x} {data.hex()}\n'
                f'memory {OUT:#x} {bytes(0x400).hex()}\nmemory {FX:#x} {bytes(512 * len(steps)).hex()}\nregion 0x3e0000 0x10000\n{extra_lines}')
        tag = f'{name}-cpl{cpl}-{block}'
        _, out, err = driver(a, text, a.out / 'stmxcsr' / tag, [block] if block else [])
        problems = []
        if not out:
            problems.append(f'driver: {err}')
        else:
            def word(addr):
                return next((int.from_bytes(bytes.fromhex(m['data'])[addr - int(m['addr'], 0):][:4], 'little') for m in out['memory']
                             if int(m['addr'], 0) <= addr < int(m['addr'], 0) + len(m['data']) // 2), None)
            if check:
                problems += check(out, word, lab)
            else:
                if out['stop'] != 'reached':
                    problems.append(f"stop {out['stop']} {out.get('reason')} rip {out.get('rip')} fault {out.get('fault')}")
                for k, (ctl, op, store, want) in enumerate(steps):
                    got, banked = word(OUT + 4 * k), word(FX + 512 * k + 24)
                    if (got, banked) != (want, want):
                        problems.append(f'step {k} ({op.splitlines()[-1]}, {store}): stored {got:#x}, FXSAVE {banked:#x}, want {want:#x}')
            if out['stats']['decodes'].get(f'{0x20:#x}') or out['stats']['decodes'].get(f'{0x33:#x}'):
                problems.append(f"STMXCSR/VSTMXCSR decoded: {out['stats']['decodes']}")
        runs.append({'case': tag, 'ok': not problems, 'problems': problems})
        print(json.dumps(runs[-1]), flush=True)
        if problems: failures.append(f'stmxcsr {tag}: {problems}')
    R = 0x1f80
    flags = [(R, ops['ie'], 'stmxcsr', R | 0x01), (R, ops['de'], 'stmxcsr', R | 0x22), (R, ops['ze'], 'stmxcsr', R | 0x04),
             (R, ops['oe'], 'stmxcsr', R | 0x28), (R, ops['ue'], 'stmxcsr', R | 0x30), (R, ops['pe'], 'vstmxcsr', R | 0x20),
             (R, ops['ie'] + '\njmp 1f\n1:\n' + ops['ze'] + '\nxor eax, eax\ncpuid\n' + ops['pe'], 'stmxcsr', R | 0x25)]
    controls = [(R | 0x3f, ops['none'], 'stmxcsr', R | 0x3f), (0xffc0, ops['none'], 'vstmxcsr', 0xffc0), (0x3f80, ops['none'], 'stmxcsr', 0x3f80),
                (R | 0x40, ops['de'], 'stmxcsr', R | 0x40), (0x9f80, ops['half'], 'stmxcsr', 0x9f80 | 0x30), (R & ~0x200, ops['pe'], 'stmxcsr', (R & ~0x200) | 0x20)]
    # Review: flags carried across a patched link (50 rounds `ldmxcsr; divss 1/0; jmp 3f; 3: stmxcsr`: from round 2 the JMP
    # is linked, so no resident entry lies between the DIVSS and the STMXCSR: ZE only in FPSR), and STMXCSR in the block of
    # an x87 op (FSQRT of -1: IE in FSW only), after a division helper's resume and after a shadow miss's retry.
    FRESH = D + 0x3000
    link = (f'xor ecx, ecx\n2: ldmxcsr dword ptr [{CTL:#x}]\n{ops["ze"]}\njmp 3f\n3: stmxcsr dword ptr [{OUT + 0x40:#x} + rcx * 4]\n'
            'inc ecx\ncmp ecx, 50\njb 2b')
    resumes = [(R, ops['ze'] + f'\nfld dword ptr [{at["m1"]:#x}]\nfsqrt\nfstp st(0)', 'stmxcsr', R | 0x04),
               (R, ops['ze'] + '\nmov edx, 1\nmov eax, 7\nmov r9d, 2\ndiv r9', 'stmxcsr', R | 0x04),
               (R, ops['ze'] + f'\nmov eax, dword ptr [{FRESH:#x}]', 'vstmxcsr', R | 0x04)]
    for cpl in (0, 3):
        for block in (None, 1):
            run('link', [(R, 'nop', 'stmxcsr', R)], cpl, block, extra_lines=f'memory {FRESH:#x} {bytes(16).hex()}\n', tail=link,
                check=lambda out, word, lab: ([] if out['stop'] == 'reached' and all(word(OUT + 0x40 + 4 * k) == R | 4 for k in range(50)) else
                                             [f"stop {out['stop']} {out.get('reason')} stored {[hex(word(OUT + 0x40 + 4 * k) or 0) for k in range(50) if word(OUT + 0x40 + 4 * k) != R | 4][:6]}"]))
            run('resumes', resumes, cpl, block, extra_lines=f'memory {FRESH:#x} {bytes(16).hex()}\n')
            run('flags', flags, cpl, block)
            run('controls', controls, cpl, block)
            # Odd and page-crossing destinations (no fault): the bytes.
            run('place', [(R | 0x05, 'nop', 'stmxcsr', R | 0x05)], cpl, block,
                tail=f'stmxcsr dword ptr [{OUT + 0x101:#x}]\nvstmxcsr dword ptr [{OUT + 0x3fe:#x}]',
                check=lambda out, word, lab: ([] if out['stop'] == 'reached' and word(OUT + 0x101) == R | 5 and word(OUT + 0x3fe) == R | 5 else
                                             [f"stop {out['stop']} {out.get('reason')} odd {word(OUT + 0x101)} crossing {word(OUT + 0x3fe)}"]))
    # #AC(0): CR0.AM and RFLAGS.AC, then CPL 3 (a switch hook), an odd destination.
    code, lab = asm(f'mov rax, cr0\nor rax, 0x40000\nmov cr0, rax\npushfq\nor qword ptr [rsp], 0x40000\npopfq\nsw: nop\nx: stmxcsr dword ptr [{OUT + 0x101:#x}]\nnop', labels=True)
    for block in (None, 1):
        text = (f'rip {CODE:#x}\nrflags 0x2\n{gprs({})}\ncode {code.hex()}\ncpl 0\nsse 1\nswitch {CODE + lab["sw"]:#x} 3\n'
                f'memory {OUT:#x} {bytes(0x400).hex()}\nregion 0x3e0000 0x10000\n')
        tag = f'ac-cpl3-{block}'
        _, out, err = driver(a, text, a.out / 'stmxcsr' / tag, [block] if block else [])
        problems = [f'driver: {err}'] if not out else (
            [] if out['stop'] == 'guest_fault' and int(out['fault'][0], 0) == 17 and int(out['rip'], 0) == CODE + lab['x'] and
            not any(int(m['data'][k:k + 2], 16) for m in out['memory'] if int(m['addr'], 0) == OUT for k in range(0, len(m['data']), 2))
            else [f"stop {out['stop']} {out.get('reason')} fault {out.get('fault')} rip {out.get('rip')}"])
        runs.append({'case': tag, 'ok': not problems, 'problems': problems})
        print(json.dumps(runs[-1]), flush=True)
        if problems: failures.append(f'stmxcsr {tag}: {problems}')
    report['stmxcsr'] = {'runs': runs, 'failures': failures}
    return failures


def repbulk(a, report):
    """Perf chunk 26: REP STOS in 64-bit addressing stores the part of its string inside RDI's page in bulk (16-byte stores,
    RCX/RDI updated after each). Judged by the SDM (STOS, REP: Vol.2; RF and single-step: Vol.3 18.3.1.4, 18.2.4; #PF and
    CR2: Vol.3 4.7; self-modifying code: Vol.3 9.1.3), natemu only, at block sizes default and 1:
    - `fill-w`: STOSB/W/D/Q of RAX's low bytes, with RDI at offsets 0, 8, 3, 0xff8, 0xff0, 0x10 and counts 0, 1, one short
      of a 16-byte chunk, a chunk, one more, 100, two pages and 3; DF = 1 twice; 0x67 (EDI/ECX) once: RCX 0, RDI the end,
      exactly the string's bytes written (the bytes around it keep 0xEE).
    - `fault`: REP STOSQ from D+0xf00 with D+0x2000 not present; REP STOSB from a read-only page's start; REP STOSB from
      D+0x1003 into a read-only D+0x2000: #PF with CR2 the faulting element's address, RIP the REP's, RCX/RDI at that element,
      every earlier element written.
    - `kicks-wN` (every width, both block sizes) and `kicks-w1/w8-misc-off` (review: IA32_MISC_ENABLE bit 0 cleared): 20,000
      rounds of 64 KiB REP STOS (round r stores r * 0x0101010101010101) under interrupts posted at random times
      (`--kick-events`): at each one taken at the REP, RCX + (RDI - BUF) / w = 65536 / w, the element before RDI holds this
      round's low byte and the one at RDI the last round's, and RF is set in the frame once an element ran; more than 50 taken
      there. With fast strings on none lies in a page's interior (16 to 4079 bytes in: a pass stores in bulk to the page's end
      or its last chunk, then one element); with the bit cleared more than half do (every iteration a boundary).
    - `wt-wp` (review): REP STOSQ over a WT and a WP page (PAT entries 1 and 2): every element run alone.
    - `cpl3-us`, `cpl3-rsvd`, `noncanonical`, `into-noncanonical` (review): #PF P|W|U and P|W|U|RSVD at a page reached in bulk,
      #GP(0) with RCX/RDI unchanged, and #GP(0) after the last canonical page's 512 elements.
    - `tso` (review): a second vCPU reads a flag a bulk REP STOSQ's vCPU stores after it, then the buffer: never older.
    - `tf`: REP STOSQ of 5 under TF: #DB after each iteration (RCX 4, 3, 2, 1 at the REP, 0 after it).
    - `watch`: a data breakpoint (DR0, write, 8 bytes) on the second quadword of a 64-quadword REP STOSQ: one #DB after that
      iteration, at the REP with RCX 62 and RDI past it, DR6.B0.
    - `uc`: REP STOSQ over a UC page (PCD|PWT): written, every element run alone (`uncached.steps` >= 512).
    - `smc-other`, `smc-own`: REP STOSB of NOPs over code already run on another (writable) page, and over code after the
      REP on its own page: the new code runs.
    - `linked`: N and 2N calls of `mov ecx, 511; xor eax, eax; rep stosq; ret` (Linux's clear_page with an odd count, its
      last element left to the one-element pass): the linker entries do not grow with N (the last element ends the
      instruction by a branch, not by an exit to its own RIP with RCX = 0, which is never linked)."""
    asm, failures, runs = ref.assemble, [], []
    D, RES, PAT = 0x400000, 0x3c0000, 0x1122334455667788
    EE3 = (b'\xee' * 0x3000).hex()
    def record(tag, problems, extra=None):
        runs.append({'case': tag, 'ok': not problems, 'problems': problems, **(extra or {})})
        print(json.dumps(runs[-1]), flush=True)
        if problems: failures.append(f'repbulk {tag}: {problems}')
    def go(tag, text, block, extra=()):
        _, out, err = driver(a, text, a.out / 'repbulk' / f'{tag}-{block}', [*([block] if block else []), '--watchdog-ms', '120000', *extra])
        return out, err
    def mem(out, addr, n=8):
        for m in out['memory']:
            lo = int(m['addr'], 0)
            if lo <= addr and addr + n <= lo + len(m['data']) // 2:
                return int.from_bytes(bytes.fromhex(m['data'])[addr - lo:addr - lo + n], 'little')
        return None
    def membytes(out, addr, n):
        return b''.join(mem(out, addr + k, 1).to_bytes(1, 'little') for k in range(n))
    for block in (None, 1):
        # fill-w: one program per width, each fill into its own three pages of 0xEE.
        for w, sfx in ((1, 'b'), (2, 'w'), (4, 'd'), (8, 'q')):
            e = 16 // w
            fills = [(0, 0, 0), (0, 1, 0), (0, e - 1, 0), (0, e, 0), (0, e + 1, 0), (8, 100, 0), (3, 100, 0), (0xff8, 64 // w + 1, 0),
                     (0xff0, 5, 0), (0x10, 0x2000 // w + 3, 0), (0x1800, 300 // w + 7, 1), (0x1004, 9, 1)] + ([(0x20, 50, 2)] if w == 4 else [])
            body = f'mov rax, {PAT:#x}\n'
            for i, (o, n, mode) in enumerate(fills):
                base = D + 0x3000 * i
                op = f'.byte 0x67, 0xf3, 0xab' if mode == 2 else f'rep stos{sfx}'
                body += (f'mov rdi, {base + o:#x}\nmov rcx, {n}\n' + ('std\n' if mode == 1 else '') + op + '\n' + ('cld\n' if mode == 1 else '')
                         + f'mov [{RES + 16 * i:#x}], rdi\nmov [{RES + 16 * i + 8:#x}], rcx\n')
            code = asm(body + 'nop')
            text = (f'rip {CODE:#x}\nrflags 0x2\n{gprs({})}\ncode {code.hex()}\nmemory {RES:#x} {bytes(16 * len(fills)).hex()}\n'
                    + ''.join(f'memory {D + 0x3000 * i:#x} {EE3}\n' for i in range(len(fills))))
            tag = f'fill-w{w}-{block}'
            out, err = go(f'fill-w{w}', text, block)
            problems = [f'driver: {err}'] if not out else ([] if out['stop'] == 'reached' else [f"stop {out['stop']} {out.get('reason')}"])
            if out and not problems:
                elem = (PAT & ((1 << 8 * w) - 1)).to_bytes(w, 'little')
                for i, (o, n, mode) in enumerate(fills):
                    base = D + 0x3000 * i
                    start, end = (base + o, base + o + n * w) if mode != 1 else (base + o - (n - 1) * w, base + o + w)
                    want = bytearray(b'\xee' * 0x3000)
                    want[start - base:end - base] = elem * n
                    rdi = base + o + (n * w if mode != 1 else -n * w)
                    if mem(out, RES + 16 * i) != rdi or mem(out, RES + 16 * i + 8) != 0:
                        problems.append(f'fill {i} {(o, n, mode)}: RDI {mem(out, RES + 16 * i):#x} RCX {mem(out, RES + 16 * i + 8)}, want {rdi:#x} 0')
                    got = membytes(out, base, 0x3000)
                    if got != bytes(want):
                        k = next(k for k in range(0x3000) if got[k] != want[k])
                        problems.append(f'fill {i} {(o, n, mode)}: byte {base + k:#x} {got[k]:#x}, want {want[k]:#x}')
            record(tag, problems)
        # fault: three programs, each stopping at its #PF.
        for name, w, o, n, mut, cr2 in (('fault-unmapped', 8, 0xf00, 1024, 'unmap', D + 0x2000), ('fault-first', 1, 0x1000, 0x1800, 'readonly', D + 0x1000),
                                         ('fault-misaligned', 1, 0x1003, 0x2000, 'readonly', D + 0x2000)):
            code, lab = asm(f'mov rax, {PAT:#x}\nmov rdi, {D + o:#x}\nmov rcx, {n}\nx: rep stos{"q" if w == 8 else "b"}\nnop', labels=True)
            text = (f'rip {CODE:#x}\nrflags 0x2\n{gprs({})}\ncode {code.hex()}\nmemory {D:#x} {EE3}\n{mut} {cr2:#x}\n')
            out, err = go(name, text, block)
            done = (cr2 - D - o) // w
            problems = [f'driver: {err}'] if not out else []
            if out:
                g = [int(x, 0) for x in out['gpr']]
                fault = [int(x, 0) for x in out['fault']]
                if out['stop'] != 'guest_fault' or fault[0] != 14 or cr2 not in fault[1:] or int(out['rip'], 0) != CODE + lab['x']:
                    problems.append(f"stop {out['stop']} {out.get('reason')} fault {out.get('fault')} rip {out.get('rip')}")
                if (g[1], g[7]) != (n - done, cr2):
                    problems.append(f'RCX {g[1]} RDI {g[7]:#x}, want {n - done} {cr2:#x}')
                elem = (PAT & ((1 << 8 * w) - 1)).to_bytes(w, 'little')
                lo = max(D, D + o - 0x100)
                got = membytes(out, lo, min(cr2, D + 0x3000) - lo) if mut != 'unmap' else membytes(out, lo, cr2 - lo)
                want = b'\xee' * (D + o - lo) + elem * done
                if got != want:
                    problems.append('memory before the faulting element is not exactly the elements done')
            record(f'{name}-{block}', problems, {'fault': out and out.get('fault')})
        # uc: a UC page (PCD|PWT in its PTE: PAT entry 3 at reset, UC).
        code = asm(f'mov rax, {PAT:#x}\nmov rdi, {D:#x}\nmov ecx, 512\nrep stosq\nnop')
        out, err = go('uc', f'rip {CODE:#x}\nrflags 0x2\n{gprs({})}\ncode {code.hex()}\nmemory {D:#x} {bytes(0x1000).hex()}\nentry {D:#x} 1 set 0x18\n', block)
        problems = [f'driver: {err}'] if not out else []
        if out:
            steps = out['stats'].get('k1', {}).get('uncached', {}).get('steps', 0)
            if out['stop'] != 'reached' or membytes(out, D, 0x1000) != PAT.to_bytes(8, 'little') * 512 or steps < 512:
                problems.append(f"stop {out['stop']} uncached steps {steps}")
        record(f'uc-{block}', problems)
        # smc-other: code at X run, then overwritten by 64 NOPs (it then slides to `mov eax, 2`); smc-own: the REP's own page.
        X = 0x200000
        xcode = asm('mov eax, 1\nret\n.fill 58, 1, 0xcc\nmov eax, 2\nret')
        code = asm(f'mov eax, {X:#x}\ncall rax\nmov r12d, eax\nmov rdi, {X:#x}\nmov ecx, 64\nmov al, 0x90\nrep stosb\nmov eax, {X:#x}\ncall rax\nmov r13d, eax\nnop')
        out, err = go('smc-other', f'rip {CODE:#x}\nrflags 0x2\n{gprs({})}\ncode {code.hex()}\nbody {X:#x} {xcode.hex()}\nentry {X:#x} 1 set 2\nregion 0x3e0000 0x10000\n', block)
        problems = [f'driver: {err}'] if not out else []
        if out:
            g = [int(x, 0) for x in out['gpr']]
            if out['stop'] != 'reached' or (g[12], g[13]) != (1, 2): problems.append(f"stop {out['stop']} r12 {g[12]} r13 {g[13]}")
        record(f'smc-other-{block}', problems)
        code = asm('lea rdi, [rip + t]\nmov ecx, 64\nmov al, 0x90\nrep stosb\n.p2align 4\nt: mov eax, 1\njmp d\n.fill 57, 1, 0xcc\nmov eax, 2\nd: nop')
        out, err = go('smc-own', f'rip {CODE:#x}\nrflags 0x2\n{gprs({})}\ncode {code.hex()}\nentry {CODE:#x} 1 set 2\n', block)
        problems = [f'driver: {err}'] if not out else []
        if out:
            g = [int(x, 0) for x in out['gpr']]
            if out['stop'] != 'reached' or g[0] != 2: problems.append(f"stop {out['stop']} {out.get('reason')} rax {g[0]}")
        record(f'smc-own-{block}', problems)
    code = asm('lea rbx, [rip + cp]\nmov r12d, 0x400000\n1: mov rdi, r12\ncall rbx\nadd r12, 4096\ncmp r12, 0x500000\njb 2f\n'
               'mov r12d, 0x400000\n2: dec r8\njnz 1b\njmp done\ncp: mov ecx, 511\nxor eax, eax\nrep stosq\nret\ndone: nop')
    entries = {}
    for n in (2000, 4000):
        out, err = go(f'linked-{n}', f'rip {CODE:#x}\nrflags 0x2\n{gprs({8: n})}\ncode {code.hex()}\nregion 0x3e0000 0x10000\nregion {D:#x} 0x100000\n', None)
        entries[n] = out and out['stop'] == 'reached' and out['stats']['linker']
    record('linked', [] if entries[2000] and entries[2000] == entries[4000] else [f'linker entries {entries}'], {'linker': entries})
    # CPL 0 with the program's own IDT (#DB 1, 0x40), as `stifirst`: tf, watch, kicks.
    TABLES, IDTR, DBG, CNT = 0x300000, 0x300800, 0x300900, 0x300b00
    BUF = 0x500000
    def idt_text(code, lab, extra_lines=''):
        at = lambda l: CODE + lab[l]
        gates = bytearray(16 * 0x41)
        for v in (1, 0x40):
            h = at(f'h{v}')
            gates[16 * v:16 * v + 16] = (h & 0xffff | 0x08 << 16 | 0x8e << 40 | (h >> 16 & 0xffff) << 48).to_bytes(8, 'little') + (h >> 32).to_bytes(8, 'little')
        tables = bytes(gates) + bytes(IDTR - TABLES - len(gates)) + (0x41 * 16 - 1).to_bytes(2, 'little') + TABLES.to_bytes(8, 'little')
        tables += bytes(CNT + 0x40 - TABLES - len(tables))
        return (f'rip {CODE:#x}\nrflags 0x2\n{gprs({})}\ncode {code.hex()}\ncpl 0\nif 0\nsvr 0x1ff\nmemory {TABLES:#x} {tables.hex()}\n'
                f'region 0x3e0000 0x10000\n{extra_lines}')
    setup = 'lidt [0x300800]\nmov ecx, 0x1b\nmov eax, 0xfee00d00\nxor edx, edx\nwrmsr\nmov ecx, 0x80f\nmov eax, 0x1ff\nwrmsr\n'
    # #DB: up to 8 records of (frame RIP, RCX, RDI, frame RFLAGS, DR6); with `clear`, DR7 cleared at the first.
    def h1(clear):
        return (f'h1:\npush rax\npush rbx\nmov rbx, [{CNT:#x}]\ncmp rbx, 8\njae 2f\nshl rbx, 6\nmov rax, [rsp + 16]\nmov [{DBG:#x} + rbx], rax\n'
                f'mov [{DBG + 8:#x} + rbx], rcx\nmov [{DBG + 16:#x} + rbx], rdi\nmov rax, [rsp + 32]\nmov [{DBG + 24:#x} + rbx], rax\n'
                f'mov rax, dr6\nmov [{DBG + 32:#x} + rbx], rax\n2: inc qword ptr [{CNT:#x}]\nxor eax, eax\nmov dr6, rax\n'
                + ('mov dr7, rax\n' if clear else '') + 'pop rbx\npop rax\niretq\n')
    for block in (None, 1):
        code, lab = asm(setup + f'mov rax, {PAT:#x}\nmov rdi, {D:#x}\nmov ecx, 5\npushfq\nor qword ptr [rsp], 0x100\npopfq\nx: rep stosq\n'
                        'y: pushfq\nand qword ptr [rsp], -0x101\npopfq\njmp done\n' + h1(False) + 'h64:\niretq\ndone: nop', labels=True)
        out, err = go('tf', idt_text(code, lab, f'memory {D:#x} {bytes(64).hex()}\n'), block)
        problems = [f'driver: {err}'] if not out else []
        if out:
            got = [(mem(out, DBG + 64 * k), mem(out, DBG + 64 * k + 8)) for k in range(5)]
            want = [(CODE + lab['x'], 4), (CODE + lab['x'], 3), (CODE + lab['x'], 2), (CODE + lab['x'], 1), (CODE + lab['y'], 0)]
            if out['stop'] != 'reached' or got != want: problems.append(f"stop {out['stop']} #DB (RIP, RCX) {[(hex(r or 0), c) for r, c in got]}")
        record(f'tf-{block}', problems)
        code, lab = asm(setup + f'mov rax, {D + 0x808:#x}\nmov dr0, rax\nmov eax, 0xd0001\nmov dr7, rax\nmov rax, {PAT:#x}\nmov rdi, {D + 0x800:#x}\n'
                        'mov ecx, 64\nx: rep stosq\njmp done\n' + h1(True) + 'h64:\niretq\ndone: nop', labels=True)
        out, err = go('watch', idt_text(code, lab, f'memory {D:#x} {bytes(0x1000).hex()}\n'), block)
        problems = [f'driver: {err}'] if not out else []
        if out:
            got = (mem(out, CNT), mem(out, DBG), mem(out, DBG + 8), mem(out, DBG + 16), (mem(out, DBG + 32) or 0) & 1)
            want = (1, CODE + lab['x'], 62, D + 0x810, 1)
            if out['stop'] != 'reached' or got != want or membytes(out, D + 0x800, 512) != PAT.to_bytes(8, 'little') * 64:
                problems.append(f"stop {out['stop']} #DB (count, RIP, RCX, RDI, B0) {got}, want {want}")
        record(f'watch-{block}', problems)
    # kicks: rounds of 64 KiB REP STOS of every width under 0x40 at random times, both block sizes; and (review) with
    # IA32_MISC_ENABLE's fast-string bit cleared, where every iteration is an interrupt boundary again.
    R, STEP = 20_000, 0x0101010101010101
    SFX = {1: 'b', 2: 'w', 4: 'd', 8: 'q'}
    def kicks(w, block, misc_off):
        n = 65536 // w
        h64 = (f'h64:\npush rax\npush rbx\npush rdx\ninc qword ptr [{CNT:#x}]\nlea rbx, [rip + x]\ncmp rbx, [rsp + 24]\njne 9f\n'
               f'inc qword ptr [{CNT + 8:#x}]\nmov rbx, rdi\nsub rbx, {BUF:#x}\nshr rbx, {w.bit_length() - 1}\nadd rbx, rcx\ncmp rbx, {n}\njne 81f\n'
               f'cmp rdi, {BUF:#x}\nje 3f\nmov rdx, [rsp + 16]\ncmp byte ptr [rdi - {w}], dl\njne 82f\ntest qword ptr [rsp + 40], 0x10000\njz 83f\n'
               f'mov rbx, rdi\nsub rbx, {BUF:#x}\nand ebx, 4095\ncmp ebx, 16\njb 3f\ncmp ebx, 4080\njae 3f\ninc qword ptr [{CNT + 24:#x}]\n'
               f'3: test rcx, rcx\njz 9f\nmov rdx, [rsp + 16]\ndec dl\ncmp byte ptr [rdi], dl\nje 9f\n'
               f'jmp 8f\n81: inc qword ptr [{CNT + 32:#x}]\njmp 8f\n82: inc qword ptr [{CNT + 40:#x}]\njmp 8f\n83: inc qword ptr [{CNT + 48:#x}]\n'
               f'8: inc qword ptr [{CNT + 16:#x}]\n9: push rcx\nmov ecx, 0x80b\nxor eax, eax\nxor edx, edx\nwrmsr\npop rcx\npop rdx\npop rbx\npop rax\niretq\n')
        off = 'mov ecx, 0x1a0\nrdmsr\nand eax, -2\nwrmsr\n' if misc_off else ''
        code, lab = asm(setup + off + f'sti\nxor r15d, r15d\nxor eax, eax\n1: mov rbx, {STEP:#x}\nadd rax, rbx\nmov rdi, {BUF:#x}\nmov ecx, {n}\n'
                        f'x: rep stos{SFX[w]}\ninc r15\ncmp r15, {R}\njb 1b\ncli\njmp done\n' + h1(False) + h64 + 'done: nop', labels=True)
        tag = f'kicks-w{w}{"-misc-off" if misc_off else ""}'
        out, err = go(tag, idt_text(code, lab, f'region {BUF:#x} 0x10000\n'), block,
                      ['--kick-vcpu', '0', '--kick-events', '0x40', '--kicks', '3000', '--kick-every-us', '20', '--kick-jitter-us', '200'])
        problems = [f'driver: {err}'] if not out else []
        taken = {}
        if out:
            taken = {'all': mem(out, CNT), 'at_rep': mem(out, CNT + 8), 'bad': mem(out, CNT + 16), 'mid_page': mem(out, CNT + 24),
                     'bad_count': mem(out, CNT + 32), 'bad_before': mem(out, CNT + 40), 'bad_rf': mem(out, CNT + 48)}
            at_rep, mid = taken['at_rep'] or 0, taken['mid_page'] or 0
            # Fast strings on: a pass stores in bulk to its page's end (or the last 16-byte chunk before it) and one element
            # more, so every boundary taken inside the REP lies within 16 bytes of a page boundary; off: boundaries anywhere,
            # nearly all in a page's interior.
            grouping = mid > at_rep // 2 if misc_off else mid == 0
            if out['stop'] != 'reached' or int(out['gpr'][15], 0) != R or taken['bad'] or at_rep < 50 or not grouping:
                problems.append(f"stop {out['stop']} {out.get('reason')} r15 {out['gpr'][15]} taken {taken}")
        record(f'{tag}-{block}', problems, {'taken': taken})
    for block in (None, 1):
        for w in (1, 2, 4, 8):
            kicks(w, block, False)
    for w in (1, 8):
        kicks(w, None, True)
    # Review: WT and WP RAM (PAT entries 1 and 2 made WT and WP; the pages' PWT / PCD select them): every element alone.
    code = asm(f'mov ecx, 0x277\nmov eax, 0x00050406\nmov edx, 0x00070406\nwrmsr\nmov rax, {PAT:#x}\nmov rdi, {D:#x}\nmov ecx, 1024\nrep stosq\nnop')
    for block in (None, 1):
        out, err = go('wt-wp', f'rip {CODE:#x}\nrflags 0x2\n{gprs({})}\ncode {code.hex()}\ncpl 0\nmemory {D:#x} {bytes(0x2000).hex()}\n'
                      f'entry {D:#x} 1 set 0x8\nentry {D + 0x1000:#x} 1 set 0x10\n', block)
        problems = [f'driver: {err}'] if not out else []
        if out:
            k1 = out['stats'].get('k1', {})
            steps, types = k1.get('uncached', {}).get('steps', 0), k1.get('memory_types', {})
            if out['stop'] != 'reached' or membytes(out, D, 0x2000) != PAT.to_bytes(8, 'little') * 1024 or steps < 1024 or not (types.get('WT') and types.get('WP')):
                problems.append(f"stop {out['stop']} {out.get('reason')} uncached steps {steps} types {types}")
        record(f'wt-wp-{block}', problems)
    # Review: faults at CPL 3 (U/S cleared, a reserved bit set in D+0x1000's PTE: #PF P|W|U, RSVD), a non-canonical RDI (#GP(0),
    # RCX and RDI unchanged) and a string running from the last canonical page into the non-canonical half (#GP(0) after the
    # page's 512 elements).
    TOP = 0x7ffffffff000
    for name, rdi, count, extra, want in (
            ('cpl3-us', D + 0xf00, 1024, f'entry {D + 0x1000:#x} 1 clear 0x4\n', (14, D + 0x1000, 0x7, 1024 - 32, D + 0x1000)),
            ('cpl3-rsvd', D + 0xf00, 1024, f'entry {D + 0x1000:#x} 1 set 0x8000000000000\n', (14, D + 0x1000, 0xf, 1024 - 32, D + 0x1000)),
            ('noncanonical', 0x800000000000, 100, '', (13, 0, 0, 100, 0x800000000000)),
            ('into-noncanonical', TOP, 513, f'memory {TOP:#x} {bytes(0x1000).hex()}\n', (13, 0, 0, 1, 0x800000000000))):
        code, lab = asm(f'mov rax, {PAT:#x}\nmov rdi, {rdi:#x}\nmov rcx, {count}\nx: rep stosq\nnop', labels=True)
        for block in (None, 1):
            out, err = go(name, f'rip {CODE:#x}\nrflags 0x2\n{gprs({})}\ncode {code.hex()}\nmemory {D:#x} {EE3}\n{extra}', block)
            problems = [f'driver: {err}'] if not out else []
            if out:
                g = [int(x, 0) for x in out['gpr']]
                f = [int(x, 0) for x in out['fault']] if out.get('fault') else []
                got = (f[0] if f else None, f[1] if f else None, f[2] if f else None, g[1], g[7])
                written = membytes(out, TOP, 0x1000) == PAT.to_bytes(8, 'little') * 512 if name == 'into-noncanonical' else True
                if out['stop'] != 'guest_fault' or got != want or int(out['rip'], 0) != CODE + lab['x'] or not written:
                    problems.append(f"stop {out['stop']} {out.get('reason')} (vector, CR2, error, RCX, RDI) {[hex(v) if v is not None else None for v in got]}, want {[hex(v) for v in want]}, page written {written}")
            record(f'{name}-{block}', problems)
    # Review: TSO as a second vCPU sees it: vCPU 0 stores round r over 16 KiB by REP STOSQ, then r into FLAG; vCPU 1 reads FLAG
    # (f) and then every quadword of the buffer, which must all be >= f (x86 orders each store before the later flag store).
    FLAG, R2 = 0x3c0000, 20_000
    c0 = asm(f'xor eax, eax\n1: inc rax\nmov rdi, {BUF:#x}\nmov ecx, 2048\nrep stosq\nmov [{FLAG:#x}], rax\ncmp rax, {R2}\njb 1b\nnop')
    c1 = asm(f'xor r9d, r9d\nxor r10d, r10d\n1: mov rbx, [{FLAG:#x}]\ntest rbx, rbx\njz 1b\nmov rsi, {BUF:#x}\nmov ecx, 2048\n'
             f'2: cmp [rsi], rbx\njae 3f\ninc r9\n3: add rsi, 8\ndec ecx\njnz 2b\ninc r10\ncmp rbx, {R2}\njb 1b\nnop')
    for block in (None, 1):
        text = (f'body {CODE:#x} {c0.hex()}\nbody 0x110000 {c1.hex()}\nregion {BUF:#x} 0x4000\nmemory {FLAG:#x} {bytes(8).hex()}\nregion 0x3e0000 0x10000\n'
                f'cpu\nrip {CODE:#x}\nrflags 0x2\n{gprs({})}\ncpu\nrip 0x110000\nrflags 0x2\n{gprs({4: STACK - 0x8000})}\n')
        out, err = go('tso', text, block)
        problems = [f'driver: {err}'] if not out else []
        if out:
            c, peer = out['cpus']
            stale, scans = int(peer['gpr'][9], 0), int(peer['gpr'][10], 0)
            if c['stop'] != 'reached' or peer['stop'] != 'reached' or stale or scans < 10:
                problems.append(f"stops {c['stop']} / {peer['stop']} stale quadwords {stale} scans {scans}")
        record(f'tso-{block}', problems)
    report['repbulk'] = {'runs': runs, 'failures': failures}
    return failures


def cr2(a, report):
    """Perf chunk 26: MOV r64, CR2 at CPL 0 is translated inline (a load of the guest-state field the resident writes at every
    entry into a CPL-0 block). Judged by the SDM (MOV from CR: Vol.2; CR2: Vol.3 2.5, 4.7, 7.15 #PF), natemu only, at block
    sizes default and 1, CPL 0 with the program's own IDT (#PF, #DB):
    - `value`: loads from two not-present pages fault; the #PF handler reads CR2 into RBX and R9 (REX.B), records both, skips
      the 7-byte load and returns: each pair the faulting address.
    - `nested`: the #PF handler reads CR2, touches a third not-present page (a nested #PF, whose handler only skips), reads
      CR2 again: the outer fault's address, then the inner's.
    - `mov-to-cr2`: MOV to CR2 of a value, then MOV r64, CR2 (in the next block, and after a JMP in a later one): that value.
    - `tf`: under TF, MOV r64, CR2 (stepped, the resident's): the value, #DB after it.
    - `cpl3`, `cpl3-alias`: after a CPL-0 MOV to CR2 and switch to CPL 3, MOV r64, CR2 and a load of its noncanonical
      frame alias independently #GP(0), leaving CR2, destination and the following instruction's sentinel unchanged.
    - `mod-cpl3`, `mod-cpl0` (review): MOV r64, CRn with mod other than 11 as a page's last three bytes, the next page not
      mapped (the hardware ignores mod): #GP(0) at it at CPL 3; MOV RBP, CR2 then a fetch #PF at the next page at CPL 0.
    - `cr3` (review): MOV r64, CR3 at CPL 0 reads CR3, not CR2.
    - `smp` (review): two vCPUs take 20,000 #PFs each at their own address through one IDT; the handler's inline CR2 always
      equals its own vCPU's address."""
    asm, failures, runs = ref.assemble, [], []
    TABLES, IDTR, LOG, P = 0x300000, 0x300800, 0x300900, 0x500000
    def record(tag, problems):
        runs.append({'case': tag, 'ok': not problems, 'problems': problems})
        print(json.dumps(runs[-1]), flush=True)
        if problems: failures.append(f'cr2 {tag}: {problems}')
    def program(body, handlers, extra=''):
        code, lab = asm('lidt [0x300800]\n' + body + '\njmp done\n' + handlers + 'done: nop', labels=True)
        gates = bytearray(16 * 16)
        for v in (1, 14):
            h = CODE + lab.get(f'h{v}', lab['h14'])
            gates[16 * v:16 * v + 16] = (h & 0xffff | 0x08 << 16 | 0x8e << 40 | (h >> 16 & 0xffff) << 48).to_bytes(8, 'little') + (h >> 32).to_bytes(8, 'little')
        tables = bytes(gates) + bytes(IDTR - TABLES - len(gates)) + (16 * 16 - 1).to_bytes(2, 'little') + TABLES.to_bytes(8, 'little')
        tables += bytes(LOG + 0x100 - TABLES - len(tables))
        return (f'rip {CODE:#x}\nrflags 0x2\n{gprs({})}\ncode {code.hex()}\ncpl 0\nif 0\nmemory {TABLES:#x} {tables.hex()}\n'
                f'region 0x3e0000 0x10000\nmemory {P:#x} {bytes(0x4000).hex()}\nunmap {P + 0x1000:#x}\nunmap {P + 0x2000:#x}\nunmap {P + 0x3000:#x}\n{extra}'), lab
    def logged(out, n):
        memory = out['memory'] if 'memory' in out else out['cpus'][0]['memory']
        return [next((int.from_bytes(bytes.fromhex(m['data'])[LOG + 8 * k - int(m['addr'], 0):][:8], 'little') for m in memory
                      if int(m['addr'], 0) <= LOG + 8 * k < int(m['addr'], 0) + len(m['data']) // 2), None) for k in range(n)]
    # #PF: the log index in [LOG]; each entry two quadwords (CR2 by RBX, by R9); skip the 7-byte `mov rax, [abs32]`.
    h14 = ('h14:\npush rbx\npush r9\npush rcx\nmov rcx, [0x300900]\nmov rbx, cr2\nmov r9, cr2\nmov [0x300908 + rcx * 8], rbx\n'
           'mov [0x300910 + rcx * 8], r9\nadd qword ptr [0x300900], 2\npop rcx\npop r9\npop rbx\nadd qword ptr [rsp + 8], 8\nadd rsp, 8\niretq\n')
    for block in (None, 1):
        extra = [block] if block else []
        def go(name, text, n, want, stop='reached'):
            _, out, err = driver(a, text, a.out / 'cr2' / f'{name}-{block}', extra)
            problems = [f'driver: {err}'] if not out else []
            if out:
                got = logged(out, n)
                if out['stop'] != stop or got != want:
                    problems.append(f"stop {out['stop']} {out.get('reason')} fault {out.get('fault')} log {[hex(x or 0) for x in got]}, want {[hex(x) for x in want]}")
            record(f'{name}-{block}', problems)
            return out
        # The handler pops the error code by `add rsp, 8` after fixing RIP at [rsp + 8] (RIP sits above the error code).
        text, lab = program(f'mov rax, qword ptr [{P + 0x1123:#x}]\nmov rax, qword ptr [{P + 0x2ff8:#x}]', h14)
        go('value', text, 5, [4, P + 0x1123, P + 0x1123, P + 0x2ff8, P + 0x2ff8])
        nested = ('h14:\npush rbx\ncmp qword ptr [0x300900], 0\njne 1f\ninc qword ptr [0x300900]\nmov rbx, cr2\nmov [0x300908], rbx\n'
                  f'mov rbx, qword ptr [{P + 0x3010:#x}]\nmov rbx, cr2\nmov [0x300910], rbx\njmp 2f\n1: inc qword ptr [0x300918]\n'
                  '2: pop rbx\nadd qword ptr [rsp + 8], 8\nadd rsp, 8\niretq\n')
        text, lab = program(f'mov rax, qword ptr [{P + 0x1040:#x}]', nested)
        go('nested', text, 4, [1, P + 0x1040, P + 0x3010, 1])
        text, lab = program('mov rax, 0x123456789abc\nmov cr2, rax\nmov rbx, cr2\nmov [0x300900], rbx\njmp 1f\n1: mov r10, cr2\nmov [0x300908], r10', h14)
        go('mov-to-cr2', text, 2, [0x123456789abc, 0x123456789abc])
        h1 = 'h1:\ninc qword ptr [0x300910]\niretq\n'
        text, lab = program('mov rax, 0xfedc000\nmov cr2, rax\npushfq\nor qword ptr [rsp], 0x100\npopfq\nmov rbx, cr2\npushfq\nand qword ptr [rsp], -0x101\npopfq\n'
                            'mov [0x300900], rbx', h1 + h14)
        go('tf', text, 3, [0xfedc000, 0, 4])   # #DB after the MOV, the PUSHFQ, the AND, the POPFQ (TF cleared by it)
        # Test alias isolation and the real MOV CR2 privilege check separately; neither may hide the other.
        for name, ins, addr in [('cpl3-alias', 'mov rbx, qword ptr [rdx]', GUEST_STATE_VA + 0xa8),
                                ('cpl3', 'mov rcx, cr2', 0)]:
            code, lab = asm(f'mov rax, 0xabc000\nmov cr2, rax\nsw: nop\nmov rdx, {addr:#x}\nx: {ins}\ninc r11', labels=True)
            text = (f'rip {CODE:#x}\nrflags 0x2\n{gprs({3: 0x12345678, 1: 0x87654321, 11: 0xabcdef})}\ncode {code.hex()}\ncpl 0\nif 0\n'
                    f'switch {CODE + lab["sw"]:#x} 3\nregion 0x3e0000 0x10000\n')
            _, out, err = driver(a, text, a.out / 'cr2' / f'{name}-{block}', extra)
            problems = [f'driver: {err}'] if not out else gp_at(out, CODE + lab['x'],
                {0: 0xabc000, 2: addr, 3: 0x12345678, 1: 0x87654321, 11: 0xabcdef}, 3, cr2=0xabc000)
            record(f'{name}-{block}', problems)
    # Review: MOV r64, CRn with mod other than 11 (the hardware ignores mod: three bytes) as the last bytes of a page whose
    # successor is not mapped: at CPL 3 #GP(0) at it (not a fetch #PF); at CPL 0 `0f 20 15` is MOV RBP, CR2, then the fetch of
    # the next page faults. And MOV r64, CR3 at CPL 0 reads CR3 (never CR2).
    END = 0x1ffffd
    for block in (None, 1):
        extra = [block] if block else []
        _, out, err = driver(a, f'rip {END:#x}\nrflags 0x2\n{gprs({})}\nbody {END:#x} 0f2005\nregion 0x3e0000 0x10000\n', a.out / 'cr2' / f'mod-cpl3-{block}', extra)
        ok = out and out['stop'] == 'guest_fault' and [int(x, 0) for x in out['fault']][0] == 13 and int(out['rip'], 0) == END
        record(f'mod-cpl3-{block}', [] if ok else [f"driver: {err}" if not out else f"stop {out['stop']} {out.get('reason')} fault {out.get('fault')} rip {out.get('rip')}"])
        code = asm(f'mov rax, 0x5555000\nmov cr2, rax\nmov eax, {END:#x}\njmp rax')
        _, out, err = driver(a, f'rip {CODE:#x}\nrflags 0x2\n{gprs({})}\ncode {code.hex()}\nbody {END:#x} 0f2015\ncpl 0\nregion 0x3e0000 0x10000\n',
                             a.out / 'cr2' / f'mod-cpl0-{block}', extra)
        ok = out and out['stop'] == 'guest_fault' and [int(x, 0) for x in out['fault']][:2] == [14, END + 3] and int(out['gpr'][5], 0) == 0x5555000
        record(f'mod-cpl0-{block}', [] if ok else [f"driver: {err}" if not out else f"stop {out['stop']} {out.get('reason')} fault {out.get('fault')} rbp {out['gpr'][5]}"])
        code = asm('mov rax, 0x777000\nmov cr2, rax\nmov rbx, cr3\nmov rcx, cr2\nnop')
        _, out, err = driver(a, f'rip {CODE:#x}\nrflags 0x2\n{gprs({})}\ncode {code.hex()}\ncpl 0\nregion 0x3e0000 0x10000\n', a.out / 'cr2' / f'cr3-{block}', extra)
        ok = out and out['stop'] == 'reached' and int(out['gpr'][3], 0) not in (0, 0x777000) and int(out['gpr'][3], 0) & 0xfff == 0 and int(out['gpr'][1], 0) == 0x777000
        record(f'cr3-{block}', [] if ok else [f"driver: {err}" if not out else f"stop {out['stop']} {out.get('reason')} rbx (CR3) {out['gpr'][3]} rcx (CR2) {out['gpr'][1]}"])
    # Review: CR2 per vCPU: two vCPUs fault 20,000 times each on their own not-present address (R12) through one IDT; the
    # handler compares CR2 (read inline) with R12 and counts mismatches at [R13].
    hs = ('h14:\npush rbx\nmov rbx, cr2\ncmp rbx, r12\nje 2f\ninc qword ptr [r13]\n2: inc qword ptr [r13 + 8]\npop rbx\n'
          'add qword ptr [rsp + 8], 4\nadd rsp, 8\niretq\n')
    for block in (None, 1):
        code, lab = asm('lidt [0x300800]\nmov r15d, 20000\n1: mov rax, qword ptr [r12]\ndec r15\njnz 1b\njmp done\n' + hs + 'done: nop', labels=True)
        h = CODE + lab['h14']
        gates = bytearray(16 * 16)
        gates[16 * 14:16 * 15] = (h & 0xffff | 0x08 << 16 | 0x8e << 40 | (h >> 16 & 0xffff) << 48).to_bytes(8, 'little') + (h >> 32).to_bytes(8, 'little')
        tables = bytes(gates) + bytes(IDTR - TABLES - len(gates)) + (16 * 16 - 1).to_bytes(2, 'little') + TABLES.to_bytes(8, 'little')
        tables += bytes(LOG + 0x100 - TABLES - len(tables))
        text = (f'body {CODE:#x} {code.hex()}\nmemory {TABLES:#x} {tables.hex()}\nregion 0x3e0000 0x10000\nmemory {P:#x} {bytes(0x4000).hex()}\n'
                f'unmap {P + 0x1000:#x}\nunmap {P + 0x2000:#x}\n'
                f'cpu\nrip {CODE:#x}\nrflags 0x2\n{gprs({12: P + 0x1100, 13: LOG})}\ncpl 0\nif 0\n'
                f'cpu\nrip {CODE:#x}\nrflags 0x2\n{gprs({12: P + 0x2208, 13: LOG + 0x40, 4: STACK - 0x8000})}\ncpl 0\nif 0\n')
        _, out, err = driver(a, text, a.out / 'cr2' / f'smp-{block}', [block] if block else [])
        problems = [f'driver: {err}'] if not out else []
        if out:
            got = [(m, n) for m, n in (logged(out, 10)[0:2], logged(out, 10)[8:10])]
            if any(c['stop'] != 'reached' for c in out['cpus']) or got != [(0, 20000), (0, 20000)]:
                problems.append(f"stops {[c['stop'] for c in out['cpus']]} (mismatches, faults) per vCPU {got}")
        record(f'smp-{block}', problems)
    report['cr2'] = {'runs': runs, 'failures': failures}
    return failures


def verw_model(sel, gdt, gdt_limit, ldt=None, ldt_limit=0, cpl=0):
    """SDM Vol.2 VERR/VERW, VERW in 64-bit mode: ZF for selector `sel` at `cpl` with the tables {offset: descriptor} and
    limits (ldt None: LDTR null). Null, outside the limit, a system or code segment, read-only data, DPL < CPL or DPL < RPL
    (data is never conforming): 0. The descriptor's P bit is not checked."""
    if sel & 0xfffc == 0: return 0
    table, limit = (ldt, ldt_limit) if sel & 4 else (gdt, gdt_limit)
    if table is None or (sel & 0xfff8) + 7 > limit: return 0
    d = table.get(sel & 0xfff8, 0)
    s_, kind, dpl = d >> 44 & 1, d >> 40 & 15, d >> 45 & 3
    return int(bool(s_) and not kind & 8 and bool(kind & 2) and dpl >= cpl and dpl >= (sel & 3))


def seg(access, base=0, limit=0xfffff, flags=0xc):
    return limit & 0xffff | (base & 0xffffff) << 16 | access << 40 | (limit >> 16 & 0xf) << 48 | flags << 52 | (base >> 24 & 0xff) << 56


def verw(a, report):
    """Perf P2: VERW m16 at CPL 0 translated inline (ZF = 1 for a writable data segment of the GDT with DPL >= RPL, the
    descriptor read through the shadow; anything else exits to the resident's VERW, the reference). Judged by the SDM (Vol.2
    VERR/VERW, `verw_model`), at block sizes default (inline) and 1 (every VERW the resident's), and the two must agree:
    - `table`: 100 selectors (every GDT entry of a 22-entry table at RPL 0-3: null, 64-bit code, writable, read-only and
      expand-down data at DPL 0-3, conforming code, a data segment not present or not accessed, LDT, TSS, call gate; beyond
      the limit by one byte and far; LDT selectors), each by five VERW forms (absolute, [rbx], [rbx + r9 * 2] (REX.X),
      [r12 + 8] (REX.B), RIP-relative) and the register form (the resident's), under three arithmetic-flag patterns: RFLAGS
      & 8D5H after each is the pattern with only ZF changed. Five phases: LDTR null; after LLDT; after LGDT with the limit one
      byte short of the last entry; one byte past it (the next entry in); after LGDT of another table with other answers.
      (Phase A loads a null LDTR first: the reset LDTR is present with base 0.)
      At the default size exactly the GDT selectors' ZF = 1 cases of the memory forms do not reach the resident (inline).
    - `alias`: the GDT page read-only (as Linux's fixmap GDT), written through a writable alias of its frame between VERWs
      of one block: each VERW sees the last write (writable, read-only, DPL 3, back).
    - `smp`: a peer rewrites a descriptor through the alias (writable / read-only by round) and then raises a round flag;
      vCPU 0 waits for the flag and VERWs: 20,000 rounds, every answer the round's.
    - `faults`: the m16 operand in a not-present page (#PF, error 0, CR2 the operand), crossing into one (CR2 the next
      page), non-canonical through RBX (#GP(0)) and RBP (#SS(0)); the descriptor in a not-present page (#PF, error 0, CR2
      the descriptor); a selector beyond the limit there (ZF = 0, no fault).
    - `cpl3`: at CPL 3 (the resident's VERW) DPL-3 data 1, DPL-0 data and RPL 3 0; the guest-state page's VERW fields read 0
      there (and the GDTR base and limit - 7 at CPL 0).
    - `watch`: a data breakpoint on the descriptor is not met by VERW's implicit read (no #DB), one on the m16 operand is
      (one #DB); ZF = 1 both times.
    - `misaligned`, `edge`, `uc`: a GDT base 4 mod 8, one in the last canonical page of the lower half (base + limit not
      canonical), a GDT page of type UC: every answer as the SDM's (the inline form defers to the resident; for `edge` a
      descriptor at a non-canonical address gives ZF = 0, the resident's rule).
    - `misaligned-op`, `straddle-op`, `cross-op`, `end-op`, `own-group`, `watch-group`: the m16 operand at +3, at +7 (two
      8-byte groups), crossing into a mapped page, two bytes before a page not mapped, as the GDT slot its own selector
      names, and with a watchpoint on another byte of its 8-byte group; each with a selector accepted inline, one refused by
      DPL and one beyond the limit: ZF as the SDM's, every other flag kept, no fault, no #DB.
    - `misaligned-cross-mapped`, `-unmapped`: a GDT base 4 mod 8 whose descriptor crosses a page: ZF = 1, or #PF at the next
      page. Fault cases also check the delivered frame's RFLAGS (the flag pattern: nothing of the VERW committed); `watch`
      and the `misaligned`/`edge`/`uc` re-runs check every flag after the VERW.
    - `first-many`: a VERW at a branch target (its block's first: the resident's), each answer; six VERWs in one straight
      block (one translated, the block's literal exits stay within four), each answer; selector 0xfff8 (beyond the limit)
      as the low word of an 8-byte group that reads as a writable data descriptor: ZF = 0.
    - `kicks`: 200,000 SYSCALL round trips whose kernel side ends `swapgs; verw [rip + KDS]; jnz bad; sysretq` (Linux's
      shape) under 20,000 precise stops at random times: counts exact, no wrong answer."""
    # Every VERW meant to be translated follows a `jmp` to the instruction before it: one per block (a block translates at
    # most one; whether a later one is depends on where the slow paths of earlier ones end blocks).
    asm, failures, runs = ref.assemble, [], []
    T, LDT, GDTR, SELS, SLOT, RES, T2 = 0x300000, 0x301000, 0x302000, 0x303000, 0x304000, 0x305000, 0x30c000
    def record(tag, problems):
        runs.append({'case': tag, 'ok': not problems, 'problems': problems})
        print(json.dumps(runs[-1]), flush=True)
        if problems: failures.append(f'verw {tag}: {problems}')
    def mem(out, at, n=8):
        memory = out['memory'] if 'memory' in out else out['cpus'][0]['memory']
        return next((int.from_bytes(bytes.fromhex(m['data'])[at - int(m['addr'], 0):][:n], 'little') for m in memory
                     if int(m['addr'], 0) <= at < int(m['addr'], 0) + len(m['data']) // 2), None)
    def raw(out, at, n):
        m = next(m for m in out['memory'] if int(m['addr'], 0) <= at < int(m['addr'], 0) + len(m['data']) // 2)
        return bytes.fromhex(m['data'])[at - int(m['addr'], 0):][:n]
    def image(table, n):
        b = bytearray(n)
        for o, d in table.items(): b[o:o + 8] = d.to_bytes(8, 'little')
        return bytes(b)
    def gdtr(base, limit): return limit.to_bytes(2, 'little') + base.to_bytes(8, 'little') + bytes(6)
    def space(items, at=0x300000, n=0x10000):
        """One memory item of n bytes at `at` holding {address: bytes} (the driver reports memory items only)."""
        b = bytearray(n)
        for x, d in items.items(): b[x - at:x - at + len(d)] = d
        return f'memory {at:#x} {bytes(b).hex()}\n'
    # The driver's first five entries (CS 0x08 stays usable for delivery), then the cases.
    A = {0x08: 0x00af9b000000ffff, 0x10: seg(0x93), 0x18: seg(0xf3), 0x20: 0x00affb000000ffff, 0x28: seg(0x91), 0x30: seg(0x97),
         0x38: seg(0xb3), 0x40: seg(0xd3), 0x48: seg(0x9f), 0x50: seg(0xff), 0x58: seg(0x13), 0x60: seg(0x92),
         0x68: seg(0x82, LDT & 0xffffffff, 0x17, 0), 0x70: LDT >> 32, 0x78: seg(0x89, 0x3a0000, 0x67, 0), 0x80: 0,
         0x88: 0x0000_8c00_0008_1000, 0x90: 0, 0x98: seg(0xf1), 0xa0: seg(0x93), 0xa8: seg(0x93)}
    B = dict(A); B.update({0x10: seg(0x91), 0x28: seg(0x93), 0x18: seg(0xd3), 0x38: seg(0x9b), 0xa0: seg(0xf1)})
    L = {0x00: seg(0x93), 0x08: seg(0xf3), 0x10: seg(0x9b)}
    sels = sorted({o | r for o in range(0, 0xb0, 8) for r in range(4)} | {0x04, 0x07, 0x0c, 0x0e, 0x0f, 0x14, 0x17, 0x1c, 0xfff8, 0xfffb, 0xfffc, 0xffff})
    PHASES = [('A', T, 0xa7, None), ('B', T, 0xa7, 0x68), ('C', T, 0xa6, 0x68), ('D', T, 0xaf, 0x68), ('E', T2, 0xaf, 0x68)]
    FORMS = ['verw word ptr [0x304000]', 'verw word ptr [rbx]', 'verw word ptr [rbx + r9 * 2]', 'verw word ptr [r12 + 8]', 'RIP', 'verw ax']
    PAT = [('mov edx, 0\ncmp edx, 1', 0x95), ('mov dl, 0x7f\nadd dl, 1', 0x890), ('mov edx, 5\ncmp edx, 5', 0x44)]
    body, k = '', 0
    for f in FORMS:
        p, _ = PAT[k % 3]; k += 1
        form = f'rv{k}: .byte 0x0f, 0x00, 0x2d, 0, 0, 0, 0' if f == 'RIP' else f
        body += f'{p}\nmov r10d, ecx\n{form}\npushfq\npop rdx\nmov [rdi], dx\nadd rdi, 2\njmp f{k}\nf{k}:\n'   # one VERW per block
    loop = (f'run: lea rsi, [{SELS:#x}]\nmov ecx, {len(sels)}\n1: movzx eax, word ptr [rsi]\nmov [rbx], ax\n{body}add rsi, 2\ndec ecx\njnz 1b\nret\n')
    main_ = ''
    for n, (ph, base, limit, ldtr) in enumerate(PHASES):
        main_ += f'lgdt [{GDTR + 16 * n:#x}]\n' + (f'mov eax, {ldtr or 0:#x}\nlldt ax\n' if n < 2 else '') + f'mov rdi, {RES + 0x1000 * n:#x}\ncall run\n'
    code, lab = asm(f'mov rbx, {SLOT:#x}\nxor r9d, r9d\nmov r12, {SLOT - 8:#x}\n' + main_ + 'jmp done\n' + loop + 'done: nop', labels=True)
    code = bytearray(code)
    for j in range(1, len(FORMS) + 1):
        if f'rv{j}' in lab:
            at = lab[f'rv{j}']; code[at + 3:at + 7] = (SLOT - (CODE + at + 7)).to_bytes(4, 'little', signed=True)
    gdtrs = b''.join(gdtr(base, limit) for _, base, limit, _ in PHASES)
    tables = space({T: image(A, 0xb0), LDT: image(L, 0x18), GDTR: gdtrs, SELS: b"".join(x.to_bytes(2, "little") for x in sels), T2: image(B, 0xb0)})
    text = f'rip {CODE:#x}\nrflags 0x2\n{gprs({})}\ncode {bytes(code).hex()}\ncpl 0\nif 0\nregion 0x3e0000 0x10000\n{tables}'
    systems = {}
    results = {}
    for block in (None, 1):
        _, out, err = driver(a, text, a.out / 'verw' / f'table-{block}', [block] if block else [])
        problems = [f'driver: {err}'] if not out else ([] if out['stop'] == 'reached' else [f"stop {out['stop']} {out['reason']} fault {out.get('fault')}"])
        if out and not problems:
            systems[block] = out['stats']['systems']
            got = {}
            for n, (ph, base, limit, ldtr) in enumerate(PHASES):
                data = raw(out, RES + 0x1000 * n, 2 * len(FORMS) * len(sels))
                gdt = A if base == T else B
                for i, sel in enumerate(sels):
                    want = verw_model(sel, gdt, limit, L if ldtr else None, 0x17)
                    for j, f in enumerate(FORMS):
                        fl = int.from_bytes(data[2 * (len(FORMS) * i + j):][:2], 'little') & 0x8d5
                        pat = PAT[j % 3][1]
                        got[(ph, sel, j)] = fl
                        if fl != (pat & ~0x40) | want << 6:
                            problems.append(f'{ph} sel {sel:#06x} form {j}: flags {fl:#x}, want {(pat & ~0x40) | want << 6:#x}')
            results[block] = got
        record(f'table-{block}', problems[:12])
    if len(results) == 2 and results[None] != results[1]:
        record('table-agree', ['block sizes differ'])
    # Inline: every memory form of a GDT selector with ZF = 1 (an LDT selector exits to the resident).
    ins = sum(verw_model(s_, A if b == T else B, lim, L if ld else None, 0x17) for _, b, lim, ld in PHASES for s_ in sels if not s_ & 4) * 5
    if len(systems) == 2:
        record('table-inline', [] if systems[1] - systems[None] == ins else [f'systems at size 1 {systems[1]}, default {systems[None]}: {ins} inline expected'])

    # alias: identity space; the GDT page read-only, its frame's alias at 0x310000 writable.
    G, AL, ISTACK = 0x300000, 0x310000, 0x2f0000
    gd = {0x08: 0x00af9b000000ffff, 0x10: seg(0x93)}
    steps = [(0x93, 1), (0x91, 0), (0xf3, 1), (0x9b, 0), (0x93, 1)]
    body = ''.join(f'mov byte ptr [{AL + 0x15:#x}], {acc:#x}\njmp a{i}\na{i}: nop\nverw word ptr [rbx]\nsetz byte ptr [{RES + i:#x}]\n' for i, (acc, _) in enumerate(steps))
    code = asm(f'lgdt [{GDTR:#x}]\nmov rbx, {SLOT:#x}\nmov word ptr [rbx], 0x10\n' + body + 'nop')
    for block in (None, 1):
        text = (f'identity\nrip {CODE:#x}\nrflags 0x2\n{gprs({4: ISTACK})}\ncode {code.hex()}\ncpl 0\nif 0\n{space({G: image(gd, 0x18), GDTR: gdtr(G, 0x17)}, n=0x11000)}'
                f'region {ISTACK - 0x10000:#x} 0x10000\nreadonly {G:#x}\nentry {AL:#x} 1 clear 0xffffffffff000\nentry {AL:#x} 1 set {G:#x}\n')
        _, out, err = driver(a, text, a.out / 'verw' / f'alias-{block}', [block] if block else [])
        problems = [f'driver: {err}'] if not out else []
        if out:
            got = [mem(out, RES + i, 1) for i in range(len(steps))]
            if out['stop'] != 'reached' or got != [w for _, w in steps]:
                problems.append(f"stop {out['stop']} {out['reason']} fault {out.get('fault')} answers {got}, want {[w for _, w in steps]}")
        record(f'alias-{block}', problems)

    # smp: vCPU 1 rewrites the descriptor through the alias, then the round flag; vCPU 0 VERWs after seeing the flag.
    N = 20000
    v0 = asm(f'lgdt [{GDTR:#x}]\nmov rbx, {SLOT:#x}\nmov word ptr [rbx], 0x10\nmov r8d, 1\n'
             f'1: cmp dword ptr [{RES + 0x100:#x}], r8d\njne 1b\nnop\nverw word ptr [rbx]\nsetz al\nmov edx, r8d\nand edx, 1\ncmp al, dl\n'
             f'je 2f\ninc qword ptr [{RES + 0x108:#x}]\n2: mov dword ptr [{RES + 0x110:#x}], r8d\ninc r8d\ncmp r8d, {N + 1}\njne 1b\nnop')
    v1 = asm(f'mov r8d, 1\n1: mov eax, r8d\nand eax, 1\nmov edx, 0x91\nmov ecx, 0x93\ntest eax, eax\ncmovnz edx, ecx\nmov byte ptr [{AL + 0x15:#x}], dl\n'
             f'mov dword ptr [{RES + 0x100:#x}], r8d\n2: cmp dword ptr [{RES + 0x110:#x}], r8d\njne 2b\ninc r8d\ncmp r8d, {N + 1}\njne 1b\nnop')
    for block in (None, 1):
        text = (f'identity\nbody {CODE:#x} {v0.hex()}\nbody {CODE + 0x10000:#x} {v1.hex()}\n{space({G: image(gd, 0x18), GDTR: gdtr(G, 0x17)}, n=0x11000)}'
                f'region {ISTACK - 0x10000:#x} 0x10000\nreadonly {G:#x}\nentry {AL:#x} 1 clear 0xffffffffff000\nentry {AL:#x} 1 set {G:#x}\n'
                f'cpu\nrip {CODE:#x}\nrflags 0x2\n{gprs({4: ISTACK})}\ncpl 0\nif 0\ncpu\nrip {CODE + 0x10000:#x}\nrflags 0x2\n{gprs({4: ISTACK - 0x8000})}\ncpl 0\nif 0\n')
        _, out, err = driver(a, text, a.out / 'verw' / f'smp-{block}', [block] if block else [])
        problems = [f'driver: {err}'] if not out else []
        if out:
            if any(c['stop'] != 'reached' for c in out['cpus']) or mem(out, RES + 0x108) != 0 or mem(out, RES + 0x110, 4) != N:
                problems.append(f"stops {[c['stop'] for c in out['cpus']]} wrong {mem(out, RES + 0x108)} rounds {mem(out, RES + 0x110, 4)}")
        record(f'smp-{block}', problems)

    # faults (the driver's IDT reports them): [vector, CR2, error, RIP].
    P_, NC = 0x320000, 0x0000_8000_0000_0000
    fg = {0x08: 0x00af9b000000ffff, 0x10: seg(0x93), 0x1008: seg(0x93), 0x1010: seg(0x93)}
    cases = [('pf-operand', f'mov rbx, {P_ + 0x10:#x}', 'verw word ptr [rbx]', [14, P_ + 0x10, 0], None),
             ('pf-cross', f'mov rbx, {P_ - 1:#x}', 'verw word ptr [rbx]', [14, P_, 0], None),
             ('gp-noncanonical', f'mov rbx, {NC:#x}', 'verw word ptr [rbx]', [13, 0, 0], None),
             ('ss-noncanonical', f'mov rbp, {NC:#x}', 'verw word ptr [rbp]', [12, 0, 0], None),
             ('pf-descriptor', f'mov rbx, {SLOT:#x}\nmov word ptr [rbx], 0x1008', 'verw word ptr [rbx]', [14, 0x330000 + 0x1008, 0], None),
             ('beyond-limit', f'mov rbx, {SLOT:#x}\nmov word ptr [rbx], 0x1010', 'verw word ptr [rbx]', None, 0)]
    for name, setup, insn, want, zf in cases:
        code, lab = asm(f'lgdt [{GDTR:#x}]\n{setup}\nmov edx, 0\ncmp edx, 1\nnop\nx: {insn}\nsetz al\nnop', labels=True)
        for block in (None, 1):
            text = (f'rip {CODE:#x}\nrflags 0x2\n{gprs({0: 0x77})}\ncode {code.hex()}\ncpl 0\nif 0\n{space({GDTR: gdtr(0x330000, 0x100f)})}region 0x3e0000 0x10000\n'
                    f'region {P_ - 0x1000:#x} 0x2000\nunmap {P_:#x}\nmemory 0x330000 {image(fg, 0x1018).hex()}\nunmap 0x331000\n')
            _, out, err = driver(a, text, a.out / 'verw' / f'{name}-{block}', [block] if block else [])
            problems = [f'driver: {err}'] if not out else []
            if out:
                if want is not None:
                    f = [int(x, 0) for x in out['fault']]
                    frame = [int(x, 0) if isinstance(x, str) else x for x in out.get('delivered', {}).get('frame', [0] * 6)]
                    if out['stop'] != 'guest_fault' or f[:3] != want or f[3] != CODE + lab['x'] or frame[2] & 0x8d5 != 0x95:
                        problems.append(f"stop {out['stop']} {out['reason']} fault {[hex(x) for x in f]}, want {[hex(x) for x in want]} at {CODE + lab['x']:#x}, "
                                        f"frame RFLAGS {frame[2]:#x} (want the pattern 0x95: nothing of the VERW committed)")
                elif out['stop'] != 'reached' or int(out['gpr'][0], 0) & 0xff != zf:
                    problems.append(f"stop {out['stop']} {out['reason']} fault {out.get('fault')} al {out['gpr'][0]}")
            record(f'{name}-{block}', problems)

    # Keep the legitimate CPL-3 VERW checks independent of rejected accesses to its internal frame fields.
    code, lab = asm(f'sw: nop\nmov rbx, {SLOT:#x}\n'
                    + ''.join(f'mov word ptr [rbx], {s_:#x}\njmp c{i}\nc{i}: nop\nverw word ptr [rbx]\nsetz byte ptr [{RES + i:#x}]\n' for i, s_ in enumerate((0x10, 0x13, 0x18, 0x1b, 0x20)))
                    + 'nop', labels=True)
    for block in (None, 1):
        text = f'rip {CODE:#x}\nrflags 0x2\n{gprs({})}\ncode {code.hex()}\ncpl 0\nswitch {CODE + lab["sw"]:#x} 3\n{space({})}region 0x3e0000 0x10000\n'
        _, out, err = driver(a, text, a.out / 'verw' / f'cpl3-{block}', [block] if block else [])
        problems = [f'driver: {err}'] if not out else []
        if out:
            got = [mem(out, RES + i, 1) for i in range(5)]
            if out['stop'] != 'reached' or got != [0, 0, 1, 1, 0]:
                problems.append(f"stop {out['stop']} {out['reason']} answers {got}")
        record(f'cpl3-{block}', problems)
        for cpl in (0, 3):
            for field, offset in [('base', 0x4a0), ('limit', 0x4a8)]:
                addr = GUEST_STATE_VA + offset
                alias, labels = asm(f'mov rax, {addr:#x}\nx: mov r12, [rax]\ninc r13', labels=True)
                text = (f'rip {CODE:#x}\nrflags 0x2\n{gprs({12: 0x12345678, 13: 0x87654321})}\ncode {alias.hex()}\ncpl {cpl}\nif 0\n'
                        f'{space({})}region 0x3e0000 0x10000\n')
                tag = f'frame-{field}-cpl{cpl}-{block}'
                _, out, err = driver(a, text, a.out / 'verw' / tag, [block] if block else [])
                problems = [f'driver: {err}'] if not out else gp_at(out, CODE + labels['x'], {0: addr, 12: 0x12345678, 13: 0x87654321}, cpl)
                if out and mem(out, RES) != 0: problems.append('frame access changed result memory')
                record(tag, problems)

    # watch: DR0 on the descriptor (8 bytes, read/write), then DR1 on the operand (2 bytes); a #DB handler counts.
    TB, IDTR_ = 0x306000, 0x306800
    def dr7(n, ln): return (1 << 2 * n) | (3 << 16 + 4 * n) | ({2: 1, 8: 2}[ln] << 18 + 4 * n)
    code, lab = asm(f'lidt [{IDTR_:#x}]\nlgdt [{GDTR:#x}]\nmov rbx, {SLOT:#x}\nmov word ptr [rbx], 0x10\nmov rax, {T + 0x10:#x}\nmov dr0, rax\n'
                    f'mov rax, {dr7(0, 8):#x}\nmov dr7, rax\nmov edx, 0\ncmp edx, 1\nnop\nverw word ptr [rbx]\npushfq\npop qword ptr [{RES + 0x10:#x}]\nsetz byte ptr [{RES:#x}]\n'
                    f'mov rax, {SLOT:#x}\nmov dr1, rax\nmov rax, {dr7(1, 2):#x}\nmov dr7, rax\nmov edx, 0\ncmp edx, 1\nnop\nverw word ptr [rbx]\npushfq\npop qword ptr [{RES + 0x18:#x}]\nsetz byte ptr [{RES + 1:#x}]\n'
                    f'xor eax, eax\nmov dr7, rax\njmp done\nh1: inc qword ptr [{RES + 8:#x}]\niretq\ndone: nop', labels=True)
    h = CODE + lab['h1']
    gates = bytearray(16 * 2); gates[16:32] = (h & 0xffff | 0x08 << 16 | 0x8e << 40 | (h >> 16 & 0xffff) << 48).to_bytes(8, 'little') + (h >> 32).to_bytes(8, 'little')
    for block in (None, 1):
        text = (f'rip {CODE:#x}\nrflags 0x2\n{gprs({})}\ncode {code.hex()}\ncpl 0\nif 0\nregion 0x3e0000 0x10000\n'
                f'{space({T: image(A, 0xb0), GDTR: gdtr(T, 0xaf), TB: bytes(gates), IDTR_: gdtr(TB, 31)})}')
        _, out, err = driver(a, text, a.out / 'verw' / f'watch-{block}', [block] if block else [])
        problems = [f'driver: {err}'] if not out else []
        if out and (out['stop'] != 'reached' or [mem(out, RES, 1), mem(out, RES + 1, 1), mem(out, RES + 8)] != [1, 1, 1]
                    or [mem(out, RES + 0x10) & 0x8d5, mem(out, RES + 0x18) & 0x8d5] != [0xd5, 0xd5]):
            problems.append(f"stop {out['stop']} {out['reason']} fault {out.get('fault')} answers {[mem(out, RES, 1), mem(out, RES + 1, 1)]} #DBs {mem(out, RES + 8)} "
                            f"flags {[hex((mem(out, RES + o) or 0) & 0x8d5) for o in (0x10, 0x18)]} (want 0xd5: only ZF changed)")
        record(f'watch-{block}', problems)

    # misaligned, edge, uc: the inline form defers; the answers are the SDM's (edge: a non-canonical descriptor gives 0).
    EDGE = 0x7fff_ffff_f000
    probe = (0x10, 0x13, 0x18, 0x28, 0x30, 0x48, 0xa0)
    variants = [('misaligned', T + 4, 0xaf, {T + 4: image(A, 0xb0)}, '', {s_: verw_model(s_, A, 0xaf) for s_ in probe}),
                ('edge', EDGE, 0xffff, {}, f'memory {EDGE:#x} {image(A, 0x1000).hex()}\n', {**{s_: verw_model(s_, A, 0xaf) for s_ in probe}, 0x1010: 0}),
                ('uc', T, 0xaf, {T: image(A, 0xb0)}, f'entry {T:#x} 1 set 0x18\n', {s_: verw_model(s_, A, 0xaf) for s_ in probe})]
    for name, base, limit, items, extra_, want in variants:
        code = asm(f'lgdt [{GDTR:#x}]\nmov rbx, {SLOT:#x}\n' + ''.join(f'mov word ptr [rbx], {s_:#x}\nmov edx, 0\ncmp edx, 1\njmp e{i}\ne{i}: nop\nverw word ptr [rbx]\n'
                                                                    f'pushfq\npop qword ptr [{RES + 0x40 + 8 * i:#x}]\nsetz byte ptr [{RES + i:#x}]\n'
                                                                    for i, s_ in enumerate(want)) + 'nop')
        for block in (None, 1):
            text = (f'rip {CODE:#x}\nrflags 0x2\n{gprs({})}\ncode {code.hex()}\ncpl 0\nif 0\nregion 0x3e0000 0x10000\n'
                    f'{space({**items, GDTR: gdtr(base, limit)})}{extra_}')
            _, out, err = driver(a, text, a.out / 'verw' / f'{name}-{block}', [block] if block else [])
            problems = [f'driver: {err}'] if not out else []
            if out:
                got = [mem(out, RES + i, 1) for i in range(len(want))]
                flags = [mem(out, RES + 0x40 + 8 * i) & 0x8d5 for i in range(len(want))]
                if out['stop'] != 'reached' or got != list(want.values()) or flags != [0x95 & ~0x40 | z << 6 for z in want.values()]:
                    problems.append(f"stop {out['stop']} {out['reason']} fault {out.get('fault')} answers {got}, want {list(want.values())}; flags {[hex(x) for x in flags]}")
                if name == 'uc' and not out['stats']['k1']['uncached']['resident']:
                    problems.append(f"no UC access by the resident: {out['stats']['k1']['uncached']}")
            record(f'{name}-{block}', problems)

    # operand: where the m16 operand lies decides the inline form's fallback read (the operand's own 8-byte group): misaligned
    # (+3), straddling two groups (+7), crossing into a mapped page, two bytes before a page not mapped (the fallback must not
    # cross), the operand itself the GDT slot its selector names (an exit), and a watchpoint on a byte of the operand's group
    # outside the operand (met by the fallback read only: the instruction runs alone, no #DB). Each with a selector accepted
    # inline (0x10), one refused by DPL (0x13: the descriptor read) and one beyond the limit (0xfff8: the fallback read);
    # RFLAGS & 8D5H after each the pattern 95H with only ZF changed.
    own = dict(A); own[0x40] = seg(0x93, 0, 0x40)   # its low word, the selector stored there, is 0x40
    places = [('misaligned-op', SLOT + 3, A, ''), ('straddle-op', SLOT + 7, A, ''), ('cross-op', 0x30afff, A, ''),
              ('end-op', 0x30fffe, A, ''), ('own-group', T + 0x40, own, ''),
              ('watch-group', SLOT, A, f'mov rax, {SLOT + 4:#x}\nmov dr0, rax\nmov rax, {(1 | 3 << 16 | 0 << 18):#x}\nmov dr7, rax\n')]
    for name, op, gdt, arm in places:
        sels = (0x40,) if name == 'own-group' else (0x10, 0x13, 0xfff8)
        body = ''.join((f'mov word ptr [rbx], {x:#x}\n' if name != 'own-group' else '') + f'mov edx, 0\ncmp edx, 1\njmp o{i}\no{i}: nop\nverw word ptr [rbx]\n'
                       f'pushfq\npop qword ptr [{RES + 8 * i:#x}]\n' for i, x in enumerate(sels))
        code = asm(f'lgdt [{GDTR:#x}]\nmov rbx, {op:#x}\n{arm}' + body + 'xor eax, eax\nmov dr7, rax\nnop')
        want = [verw_model(x, gdt, 0xaf) for x in sels]
        for block in (None, 1):
            text = (f'rip {CODE:#x}\nrflags 0x2\n{gprs({})}\ncode {code.hex()}\ncpl 0\nif 0\nregion 0x3e0000 0x10000\n'
                    f'{space({T: image(gdt, 0xb0), GDTR: gdtr(T, 0xaf), **({T + 0x40: (0x40).to_bytes(2, "little")} if name == "own-group" else {})})}')
            _, out, err = driver(a, text, a.out / 'verw' / f'{name}-{block}', [block] if block else [])
            problems = [f'driver: {err}'] if not out else []
            if out:
                flags = [(mem(out, RES + 8 * i) or 0) & 0x8d5 for i in range(len(sels))]
                if out['stop'] != 'reached' or flags != [0x95 & ~0x40 | z << 6 for z in want]:
                    problems.append(f"stop {out['stop']} {out['reason']} fault {out.get('fault')} flags {[hex(x) for x in flags]}, want ZF {want} over 0x95")
                if name == 'watch-group' and block is None and not out['stats']['watch_hits']:
                    problems.append('the fallback read met no watchpoint')
            record(f'{name}-{block}', problems)

    # misaligned-cross: a GDT base 4 mod 8 whose 0x10 descriptor crosses a page, the next page mapped (ZF = 1) or not (#PF at
    # the VERW, CR2 the next page): the resident's answers (no inline read: the base is not 8-aligned).
    for name, base, items, mapped in (('misaligned-cross-mapped', 0x30a000 - 0x14, {}, True), ('misaligned-cross-unmapped', 0x310000 - 0x14, {}, False)):
        img = {base: image(A, 0x18)} if mapped else {base: image(A, 0x14)}
        code, lab = asm(f'lgdt [{GDTR:#x}]\nmov rbx, {SLOT:#x}\nmov word ptr [rbx], 0x10\nmov edx, 0\ncmp edx, 1\njmp mc\nmc: nop\nx: verw word ptr [rbx]\n'
                        f'pushfq\npop qword ptr [{RES:#x}]\nnop', labels=True)
        tail = '' if mapped else f'memory 0x310000 {image({0: A[0x10] >> 32}, 8).hex()}\nunmap 0x310000\n'
        for block in (None, 1):
            text = (f'rip {CODE:#x}\nrflags 0x2\n{gprs({})}\ncode {code.hex()}\ncpl 0\nif 0\nregion 0x3e0000 0x10000\n'
                    f'{space({**img, GDTR: gdtr(base, 0xaf)})}{tail}')
            _, out, err = driver(a, text, a.out / 'verw' / f'{name}-{block}', [block] if block else [])
            problems = [f'driver: {err}'] if not out else []
            if out:
                if mapped and (out['stop'] != 'reached' or (mem(out, RES) or 0) & 0x8d5 != 0xd5):
                    problems.append(f"stop {out['stop']} {out['reason']} fault {out.get('fault')} flags {hex((mem(out, RES) or 0) & 0x8d5)}")
                if not mapped and (out['stop'] != 'guest_fault' or [int(x, 0) for x in out['fault']] != [14, 0x310000, 0, CODE + lab['x']]):
                    problems.append(f"stop {out['stop']} {out['reason']} fault {out.get('fault')}, want #PF at 0x310000")
            record(f'{name}-{block}', problems)

    # first: a VERW starting its block (a branch target) is the resident's (a translated one's exit would re-enter its own
    # block), with ZF 0 and 1; many: six VERWs in one straight block (one translated: a block has at most four literal exits).
    # fake: the m16 operand is the low word of an 8-byte group that reads as a writable DPL-0 data descriptor (selector
    # 0xfff8, beyond the limit): the inline form reads that group in place of a descriptor and must not accept it (ZF = 0).
    sl = (0x10, 0x0, 0x13, 0x18, 0x28, 0x10)
    first = ''.join(f'mov word ptr [rbx], {x:#x}\njmp v{i}\nv{i}: verw word ptr [rbx]\nsetz byte ptr [{RES + i:#x}]\n' for i, x in enumerate(sl))
    many = ''.join(f'mov word ptr [r{8 + i}], {x:#x}\n' for i, x in enumerate(sl)) + 'nop\n' + ''.join(
        f'verw word ptr [r{8 + i}]\nsetz byte ptr [{RES + 8 + i:#x}]\n' for i in range(len(sl)))
    fake = f'jmp fk\nfk: mov rax, {seg(0x93, 0, 0xffff8):#x}\nmov [rbx + 0x40], rax\nnop\nverw word ptr [rbx + 0x40]\nsetz byte ptr [{RES + 0x10:#x}]\n'
    code = asm(f'lgdt [{GDTR:#x}]\nmov rbx, {SLOT:#x}\n' + ''.join(f'lea r{8 + i}, [rbx + {2 * i}]\n' for i in range(len(sl))) + first + 'jmp m\nm: ' + many + fake + 'nop')
    want = [verw_model(x, A, 0xaf) for x in sl]
    for block in (None, 1):
        text = (f'rip {CODE:#x}\nrflags 0x2\n{gprs({})}\ncode {code.hex()}\ncpl 0\nif 0\nregion 0x3e0000 0x10000\n'
                f'{space({T: image(A, 0xb0), GDTR: gdtr(T, 0xaf)})}')
        _, out, err = driver(a, text, a.out / 'verw' / f'first-many-{block}', [block] if block else [])
        problems = [f'driver: {err}'] if not out else []
        if out:
            got = [mem(out, RES + i, 1) for i in range(len(sl))], [mem(out, RES + 8 + i, 1) for i in range(len(sl))], mem(out, RES + 0x10, 1)
            if out['stop'] != 'reached' or got != (want, want, 0):
                problems.append(f"stop {out['stop']} {out['reason']} first {got[0]} many {got[1]} fake {got[2]}, want {want} and 0")
        record(f'first-many-{block}', problems)

    # kicks: Linux's return shape under precise stops; the driver's GDT has 0x10 as the kernel data segment.
    BAD = KERNEL_GS + 16
    kicks = syscall_program('1: inc qword ptr gs:[8]\nsyscall\ndec r8\njnz 1b',
                            f'swapgs\ninc qword ptr gs:[0]\nswapgs\nverw word ptr [rip + kds]\njnz 2f\nsysretq\n2: inc qword ptr [{BAD:#x}]\nsysretq\nkds: .word 0x10', {8: 200000})
    _, out, err = driver(a, kicks, a.out / 'verw' / 'kicks', ['--kick-vcpu', '0', '--kicks', '20000', '--kick-every-us', '2', '--kick-jitter-us', '6'])
    problems = [f'driver: {err}'] if not out else []
    if out and (out['stop'] != 'reached' or (mem(out, USER_GS + 8), mem(out, KERNEL_GS), mem(out, BAD)) != (200000, 200000, 0) or out['kicks']['stops'] < 1000):
        problems.append(f"stop {out['stop']} {out['reason']} counts {mem(out, USER_GS + 8)} {mem(out, KERNEL_GS)} bad {mem(out, BAD)} stops {out['kicks']['stops']}")
    record('kicks', problems)
    report['verw'] = {'runs': runs, 'failures': failures}
    return failures


# The AVX2 gathers (VEX.66.0F38 90..93): (mnemonic, element bytes, index bytes, VEX.L). Each executes in the resident.
GATHERS = [(m, ew, iw, L) for m, ew, iw in (('vpgatherdd', 4, 4), ('vpgatherqd', 4, 8), ('vpgatherdq', 8, 4), ('vpgatherqq', 8, 8),
                                             ('vgatherdps', 4, 4), ('vgatherqps', 4, 8), ('vgatherdpd', 8, 4), ('vgatherqpd', 8, 8))
           for L in (0, 1)]


def gather_model(ew, iw, L, d, m, x, address, read):
    """SDM Vol.2 VPGATHERDD/DQ/QD/QQ, VGATHERDPS/DPD/QPS/QPD, 64-bit mode: element j (lowest first) is read when its mask
    element's MSB is set, at `address(sign-extended index j)`; `read(addr, n)` gives its bytes or an exception tuple, which
    ends the instruction. d, m, x: the destination, mask and index registers (32 bytes each). Completed: the whole mask 0,
    the destination past the gathered elements 0 (to bit 255). At a fault on element k: elements 0..k-1 done (destination
    elements loaded where masked in, mask elements 0), nothing else changed, as on the reference AMD machine
    (natemu-raw/gather-20261105-01/probe). Returns (d, m, exception or None)."""
    n = (16 << L) // max(ew, iw)
    d, m = bytearray(d), bytearray(m)
    for j in range(n):
        if m[ew * j + ew - 1] & 0x80:
            ix = int.from_bytes(x[iw * j:iw * j + iw], 'little', signed=True)
            r = read(address(ix), ew)
            if isinstance(r, tuple):
                return bytes(d), bytes(m), r
            d[ew * j:ew * j + ew] = r
        m[ew * j:ew * j + ew] = bytes(ew)
    d[ew * n:] = bytes(32 - ew * n)
    return bytes(d), bytes(32), None


def gather(a, report):
    """The AVX2 gathers (VPGATHERDD/DQ/QD/QQ, VGATHERDPS/DPD/QPS/QPD; correctness pass finding 2), the resident's
    element-by-element transaction. Judged by `gather_model` (the SDM's operation and exceptions, Type 12; the partial state at
    a fault as the reference AMD machine leaves it), natemu only, block sizes default and 1:
    - `forms-cplN`: every form at VEX.128 and VEX.256, scales 1/2/4/8, masks all set, none, MSB only alternating with every
      other bit, MSB with low garbage, random; positive and negative indices, displacements, five register triples (VEX.R,
      VEX.X, VEX.vvvv high registers) and base registers (none: disp32); garbage in every unused bit of the inputs; 0x67
      (32-bit addresses, a qword index wrapping past 2^32) and an FS override; at CPL 3 and CPL 0: the 32-byte destination
      and mask after each.
    - `fault-FORM`: CPL 0 with the program's own IDT: the element k (each k) at a not-present page, a masked-off element there
      (no fault), an element crossing into it (CR2 the page start), two faulting elements: #PF with CR2, error 0, RIP at the
      gather, the destination and mask at the fault (the elements below k done, the rest unchanged); the handler points the
      faulting index (and the done elements', at other memory) elsewhere and returns: the gather restarts from that state and
      completes, the done elements not read again.
    - `user-*`: CPL 3, the driver's logging IDT: #PF error 4 (not present) and 5 (a supervisor page) with the partial state;
      #GP(0) for a non-canonical element, #SS(0) through an RSP base; a masked-off non-canonical element: completes.
    - `ud-*`: #UD with the registers unchanged for destination = index, mask = index, destination = mask, no SIB, a register
      operand, LOCK, a 66 prefix before VEX; CR4.OSXSAVE clear and XCR0 = 3 (#UD), CR0.TS (#NM at CPL 0).
    - `watch`, `watch-masked`, `inlieu`, `tf`, `tf-fault`, `codebreak` (CPL 0, own IDT): a data breakpoint on an element: one
      #DB after the gather (B0); on a masked-off element: none; on element 0 with element 2 faulting: the #DB in lieu of the
      #PF, RIP at the gather, RF = 1 in its frame, elements 0..1 done, then the #PF on the restart; TF: one #DB (BS) after
      it, after a #PF and its restart too; an instruction breakpoint at a gather that faults: #DB, #PF (its frame RF = 1),
      completed without a second #DB.
    - `ac`: CPL 3 with CR0.AM and RFLAGS.AC: misaligned elements, no #AC (a misaligned MOV after it: #AC).
    - `uc`: elements on a UC page: read (the UC count grows). `mmio`: an element on a device page stops `unsupported_mmio`; a
      masked-off one there completes.
    - `tso`: two vCPUs: vCPU 1 stores X then Y (a counter) 100,000 times; vCPU 0 loads Y, then gathers X: never older."""
    asm, failures, runs = ref.assemble, [], []
    D, T, R, FIXV, TABLES, IDTR, LOG = 0x400000, 0x600000, 0x700000, 0x680000, 0x300000, 0x300800, 0x310000
    B = D + 0x5000                       # the common base; D..D+0xa000 mapped, the fault page FP = B - 0x1000 in fault runs
    FP = B - 0x1000
    CNT, FIXP = LOG - 16, LOG - 8
    pattern = bytes((r * 7 + (r >> 8) * 13 + 1) & 0xff for r in range(0xa000))
    rng = __import__('random').Random(20261105)
    rb = lambda n: bytes(rng.getrandbits(8) for _ in range(n))

    def record(tag, problems, extra=None):
        runs.append({'case': tag, 'ok': not problems, 'problems': problems, **(extra or {})})
        print(json.dumps(runs[-1]), flush=True)
        if problems: failures.append(f'gather {tag}: {problems}')
    def mem(out, addr, n):
        for m in out['memory']:
            lo = int(m['addr'], 0)
            if lo <= addr and addr + n <= lo + len(m['data']) // 2:
                return bytes.fromhex(m['data'])[addr - lo:addr - lo + n]
        return None
    def ymm(out, r):
        x = [int(v, 0) for v in out['xmm']]
        h = [int(v, 0) for v in out['ymmh']]
        return b''.join(q.to_bytes(8, 'little') for q in (x[2 * r], x[2 * r + 1], h[2 * r], h[2 * r + 1]))
    def reader(unmapped=(), supervisor=(), cpl=0, ss=False):
        def read(addr, n):
            last = (addr + n - 1) % (1 << 64)
            canon = lambda v: v < 1 << 47 or v >= (1 << 64) - (1 << 47)
            if not canon(addr) or not canon(last) or last < addr:
                return (12 if ss else 13, 0)
            for p in sorted({addr & ~0xfff, last & ~0xfff}):
                if p in unmapped: return (14, max(p, addr), 0 if cpl == 0 else 4)
                if p in supervisor and cpl == 3: return (14, max(p, addr), 5)
            return pattern[addr - D:addr - D + n]
        return read
    def widths(ew, iw, L):
        n = (16 << L) // max(ew, iw)
        return n, 'ymm' if n * ew == 32 else 'xmm', 'ymm' if n * iw == 32 else 'xmm'
    def insn(mn, ew, iw, L, rd, ri, rm, base, s, disp, seg=''):
        n, dw, xw = widths(ew, iw, L)
        addr = (f'{base} + ' if base else '') + f'{xw}{ri}*{s}' + (f' + {disp:#x}' if disp >= 0 else f' - {-disp:#x}')
        return f'{mn} {dw}{rd}, {seg}[{addr}], {dw}{rm}'
    def inputs(ew, iw, L, offs, s, kind):
        """Destination, index (offsets / scale, the unused bytes random) and mask (kind) registers."""
        n = (16 << L) // max(ew, iw)
        d, x, m = bytearray(rb(32)), bytearray(rb(32)), bytearray(rb(32))
        top = 1 << (8 * ew - 1)
        for j in range(n):
            x[iw * j:iw * j + iw] = (offs[j] // s).to_bytes(iw, 'little', signed=True) if iw == 8 or -2**31 <= offs[j] // s < 2**31 \
                else ((offs[j] // s) % (1 << 32)).to_bytes(4, 'little')
            v = {'all': (1 << 8 * ew) - 1, 'none': 0, 'sparse': top if j % 2 == 0 else top - 1, 'msb': top | j * 0x01010101,
                 'random': rng.getrandbits(8 * ew)}[kind]
            m[ew * j:ew * j + ew] = v.to_bytes(ew, 'little')
        return bytes(d), bytes(x), bytes(m)

    # forms: one program per CPL and block size, every case's result stored at R + 64 i.
    triples = [(0, 1, 2), (8, 15, 4), (3, 9, 14), (12, 5, 13), (7, 6, 10)]
    bases = [('rdi', 7), ('r13', 13), ('rbp', 5), ('r9', 9), (None, None)]
    cases = []
    for fi, (mn, ew, iw, L) in enumerate(GATHERS):
        n = (16 << L) // max(ew, iw)
        for si, s in enumerate((1, 2, 4, 8)):
            for ki, kind in enumerate(('all', 'none', 'sparse', 'msb', 'random')):
                disp = (0, 0x10, -0x20, 0x7f, 0x234)[(fi + si + ki) % 5]
                offs = [rng.randrange(-0x4f00, 0x4c00) // s * s for _ in range(n)]
                rd, ri, rm = triples[(fi + ki) % 5]
                base, breg = bases[(si + ki) % 5]
                d, x, m = inputs(ew, iw, L, offs, s, kind)
                cases.append(dict(insn=insn(mn, ew, iw, L, rd, ri, rm, base, s, disp if base else B + disp), regs=(rd, ri, rm), breg=breg, bval=B,
                                  form=(ew, iw, L), d=d, x=x, m=m, addr=lambda ix, s=s, disp=disp: B + ix * s + disp))
    # 0x67 (EDI base; a qword index wrapping past 2^32 to D) and an FS override (RDI = B - FS.base).
    FSB = 0x10000
    for mn, ew, iw, L, s in (('vpgatherdd', 4, 4, 1, 4), ('vpgatherqq', 8, 8, 1, 8)):
        n = (16 << L) // max(ew, iw)
        offs = [0x80 * j for j in range(n)]
        if iw == 8: offs[2] = (1 << 32) + 0x100 - 0x5000
        d, x, m = inputs(ew, iw, L, offs, s, 'all')
        cases.append(dict(insn=insn(mn, ew, iw, L, 0, 1, 2, 'edi', s, 0), regs=(0, 1, 2), breg=7, bval=B, form=(ew, iw, L), d=d, x=x, m=m,
                          addr=lambda ix, s=s: (B + ix * s) % (1 << 32)))
    d, x, m = inputs(4, 4, 1, [0x40 * j - 0x200 for j in range(8)], 4, 'msb')
    cases.append(dict(insn=insn('vpgatherdd', 4, 4, 1, 0, 1, 2, 'rdi', 4, 8, seg='fs:'), regs=(0, 1, 2), breg=7, bval=B - FSB, form=(4, 4, 1),
                      d=d, x=x, m=m, addr=lambda ix: B - FSB + ix * 4 + 8 + FSB))
    body = ''
    for i, c in enumerate(cases):
        rd, ri, rm = c['regs']
        body += (f'vmovdqu ymm{rd}, [{T + 96 * i:#x}]\nvmovdqu ymm{ri}, [{T + 96 * i + 32:#x}]\nvmovdqu ymm{rm}, [{T + 96 * i + 64:#x}]\n'
                 + (f'mov {["rax", "rcx", "rdx", "rbx", "rsp", "rbp", "rsi", "rdi", "r8", "r9", "r10", "r11", "r12", "r13", "r14", "r15"][c["breg"]]}, {c["bval"]:#x}\n' if c['breg'] is not None else '')
                 + c['insn'] + f'\nvmovdqu [{R + 64 * i:#x}], ymm{rd}\nvmovdqu [{R + 64 * i + 32:#x}], ymm{rm}\n')
    code = asm(body + 'nop')
    table = b''.join(c['d'] + c['x'] + c['m'] for c in cases)
    for cpl in (3, 0):
        for block in (None, 1):
            text = (f'rip {CODE:#x}\nrflags 0x2\n{gprs({})}\ncode {code.hex()}\ncpl {cpl}\nsse 1\nxcr0 7\nfsbase {FSB:#x}\nmemory {D:#x} {pattern.hex()}\n'
                    f'memory {T:#x} {table.hex()}\nmemory {R:#x} {bytes(64 * len(cases)).hex()}\nregion 0x3e0000 0x10000\n')
            tag = f'forms-cpl{cpl}-{block}'
            _, out, err = driver(a, text, a.out / 'gather' / tag, [block] if block else [])
            problems = [f'driver: {err}'] if not out else ([] if out['stop'] == 'reached' else [f"stop {out['stop']} {out.get('reason')} fault {out.get('fault')} rip {out.get('rip')}"])
            if out and not problems:
                for i, c in enumerate(cases):
                    wd, wm, e = gather_model(*c['form'], c['d'], c['m'], c['x'], c['addr'], reader())
                    got = mem(out, R + 64 * i, 64)
                    if e or got != wd + wm:
                        problems.append(f"{c['insn']}: dest {got[:32][::-1].hex()} mask {got[32:][::-1].hex()}, want {wd[::-1].hex()} {wm[::-1].hex()}")
                decodes = out['stats']['decodes'].get(f'{0x7a:#x}', 0)
                if block is None and decodes == 0: problems.append('no gather decoded')
            record(tag, problems[:6], {'cases': len(cases)})

    # CPL 0 with the program's own IDT (#DB 1, #PF 14): handlers log (CR2 or DR6, error, RIP, RFLAGS, vector, -, YMM0, YMM2)
    # per entry at LOG + 128 k; #PF loads YMM1 from the next FIX vector ([FIXP], advanced by 32) and returns; #DB sets RF in
    # its frame (after logging it) and clears DR6.
    def idt_program(body, extra=''):
        h = (f'h14:\npush rax\npush rbx\nmov rbx, [{CNT:#x}]\nshl rbx, 7\nadd rbx, {LOG:#x}\nmov rax, cr2\nmov [rbx], rax\nmov rax, [rsp + 16]\n'
             f'mov [rbx + 8], rax\nmov rax, [rsp + 24]\nmov [rbx + 16], rax\nmov rax, [rsp + 40]\nmov [rbx + 24], rax\nmov qword ptr [rbx + 32], 14\n'
             f'vmovdqu [rbx + 64], ymm0\nvmovdqu [rbx + 96], ymm2\nmov rax, [{FIXP:#x}]\nvmovdqu ymm1, [rax]\nadd qword ptr [{FIXP:#x}], 32\n'
             f'inc qword ptr [{CNT:#x}]\npop rbx\npop rax\nadd rsp, 8\niretq\n'
             f'h1:\npush rax\npush rbx\nmov rbx, [{CNT:#x}]\nshl rbx, 7\nadd rbx, {LOG:#x}\nmov rax, dr6\nmov [rbx], rax\nmov rax, [rsp + 16]\n'
             f'mov [rbx + 16], rax\nmov rax, [rsp + 32]\nmov [rbx + 24], rax\nmov qword ptr [rbx + 32], 1\nvmovdqu [rbx + 64], ymm0\n'
             f'vmovdqu [rbx + 96], ymm2\nxor eax, eax\nmov dr6, rax\nor qword ptr [rsp + 32], 0x10000\ninc qword ptr [{CNT:#x}]\npop rbx\npop rax\niretq\n')
        code, lab = asm(f'lidt [{IDTR:#x}]\n' + body + '\njmp done\n' + h + 'done: nop', labels=True)
        gates = bytearray(16 * 16)
        for v in (1, 14):
            hv = CODE + lab[f'h{v}']
            gates[16 * v:16 * v + 16] = (hv & 0xffff | 0x08 << 16 | 0x8e << 40 | (hv >> 16 & 0xffff) << 48).to_bytes(8, 'little') + (hv >> 32).to_bytes(8, 'little')
        tables = bytes(gates) + bytes(IDTR - TABLES - len(gates)) + (16 * 16 - 1).to_bytes(2, 'little') + TABLES.to_bytes(8, 'little')
        return (f'rip {CODE:#x}\nrflags 0x2\n{gprs({})}\ncode {code.hex()}\ncpl 0\nif 0\nsse 1\nxcr0 7\nmemory {TABLES:#x} {tables.hex()}\n'
                f'memory {D:#x} {pattern.hex()}\nmemory {LOG - 16:#x} {bytes(16 + 128 * 64).hex()}\nregion 0x3e0000 0x10000\n{extra}'), lab
    def logs(out, k):
        raw = mem(out, LOG + 128 * k, 128)
        q = lambda at: int.from_bytes(raw[at:at + 8], 'little')
        return dict(a=q(0), error=q(8), rip=q(16), rflags=q(24), vector=q(32), d=raw[64:96], m=raw[96:128])
    def count(out): return int.from_bytes(mem(out, CNT, 8), 'little')

    # fault-FORM: per form one program of cases, each `set FIXP; load YMM0..2; gather; store YMM0, YMM2`.
    for fi, (mn, ew, iw, L) in enumerate(GATHERS):
        n, _, _ = widths(ew, iw, L)
        s = 4 if iw == 4 else 8
        fcases = []
        def good(j): return 0x100 + 0x48 * j
        for k in range(n):
            offs = [good(j) for j in range(n)]; offs[k] = -0x800
            fcases.append((f'k{k}', offs, 'all', s, [[good(j) + (0x200 if j < k else 0) for j in range(n)]]))
        if n >= 3:
            offs = [good(j) for j in range(n)]; offs[1] = offs[2] = -0x800
            fcases.append(('sparse', offs, 'sparse', s, [[good(j) for j in range(n)]]))   # element 1 masked off, 2 faults
            offs = [good(j) for j in range(n)]; offs[1] = -0x700; offs[n - 1] = -0xf00
            fcases.append(('two', offs, 'all', s, [[good(j) if j == 1 else offs[j] for j in range(n)], [good(j) for j in range(n)]]))
        offs = [good(j) for j in range(n)]; offs[n - 1] = -0x1000 - 2
        fcases.append(('cross', offs, 'all', 1 if ew == 4 else 2, [[good(j) for j in range(n)]]))
        body, fix, model = '', b'', []
        for i, (name, offs, kind, sc, fixes) in enumerate(fcases):
            d, x, m = inputs(ew, iw, L, offs, sc, kind)
            fx = [inputs(ew, iw, L, f, sc, 'none')[1] for f in fixes]
            at = FIXV + len(fix)
            fix += b''.join(fx)
            body += (f'mov qword ptr [{FIXP:#x}], {at:#x}\nvmovdqu ymm0, [{T + 96 * i:#x}]\nvmovdqu ymm1, [{T + 96 * i + 32:#x}]\nvmovdqu ymm2, [{T + 96 * i + 64:#x}]\n'
                     f'mov rdi, {B:#x}\ng{i}: {insn(mn, ew, iw, L, 0, 1, 2, "rdi", sc, 0)}\nvmovdqu [{R + 64 * i:#x}], ymm0\nvmovdqu [{R + 64 * i + 32:#x}], ymm2\n')
            # The expected log entries and result: fault, fix, restart until done.
            addr = lambda ix, sc=sc: B + ix * sc
            read, entries, cur = reader(unmapped={FP}), [], (d, m, x)
            for f in fx + [None]:
                wd, wm, e = gather_model(ew, iw, L, cur[0], cur[1], cur[2], addr, read)
                if not e: break
                entries.append((e, wd, wm))
                cur = (wd, wm, f if f is not None else cur[2])
            model.append((name, entries, wd + wm, d + x + m))
        text, lab = idt_program(body, f'memory {T:#x} {b"".join(c[3] for c in model).hex()}\nmemory {R:#x} {bytes(64 * len(model)).hex()}\n'
                                      f'memory {FIXV:#x} {fix.hex()}\nunmap {FP:#x}\n')
        for block in (None, 1):
            tag = f'fault-{mn}-{"xy"[L]}-{block}'
            _, out, err = driver(a, text, a.out / 'gather' / tag, [block] if block else [])
            problems = [f'driver: {err}'] if not out else ([] if out['stop'] == 'reached' else [f"stop {out['stop']} {out.get('reason')} fault {out.get('fault')} rip {out.get('rip')}"])
            if out and not problems:
                k = 0
                for i, (name, entries, want, _) in enumerate(model):
                    for e, wd, wm in entries:
                        g = logs(out, k); k += 1
                        if (g['vector'], g['a'], g['error'], g['rip'], g['d'], g['m']) != (14, e[1], e[2], CODE + lab[f'g{i}'], wd, wm):
                            problems.append(f"{name}: #{g['vector']} cr2 {g['a']:#x} error {g['error']} rip {g['rip']:#x} dest {g['d'][::-1].hex()} mask {g['m'][::-1].hex()}, "
                                            f"want #PF {e[1]:#x} {e[2]} dest {wd[::-1].hex()} mask {wm[::-1].hex()}")
                    if mem(out, R + 64 * i, 64) != want:
                        problems.append(f'{name}: after the restart dest+mask {mem(out, R + 64 * i, 64)[::-1].hex()}, want {want[::-1].hex()}')
                if count(out) != k: problems.append(f'{count(out)} handler entries, want {k}')
            record(tag, problems[:6])

    # user-*: CPL 3, one fault each through the driver's logging IDT; the state at the fault from `xmm`/`ymmh`.
    def user(name, mn, ew, iw, L, offs, kind, extra='', base='rdi', bval=B, s=4, read=None):
        d, x, m = inputs(ew, iw, L, offs, s, kind)
        code, lab = asm(f'vmovdqu ymm0, [{T:#x}]\nvmovdqu ymm1, [{T + 32:#x}]\nvmovdqu ymm2, [{T + 64:#x}]\nmov {base}, {bval:#x}\n'
                        f'g: {insn(mn, ew, iw, L, 0, 1, 2, base, s, 0)}\nnop', labels=True)
        wd, wm, e = gather_model(ew, iw, L, d, m, x, lambda ix: bval + ix * s, read or reader(cpl=3))
        for block in (None, 1):
            text = (f'rip {CODE:#x}\nrflags 0x2\n{gprs({})}\ncode {code.hex()}\nsse 1\nxcr0 7\nmemory {D:#x} {pattern.hex()}\nmemory {T:#x} {(d + x + m).hex()}\n'
                    f'region 0x3e0000 0x10000\n{extra}')
            _, out, err = driver(a, text, a.out / 'gather' / f'{name}-{block}', [block] if block else [])
            problems = [f'driver: {err}'] if not out else []
            if out:
                f = [int(v, 0) for v in out['fault']]
                if e is None:
                    if out['stop'] != 'reached': problems.append(f"stop {out['stop']} {out.get('reason')} fault {f}")
                else:
                    wf = [e[0], e[1] if e[0] == 14 else 0, e[2] if e[0] == 14 else 0, CODE + lab['g']]
                    if out['stop'] != 'guest_fault' or f != wf: problems.append(f"stop {out['stop']} fault {[hex(v) for v in f]}, want {[hex(v) for v in wf]}")
                if (ymm(out, 0), ymm(out, 2)) != (wd, wm):
                    problems.append(f'dest {ymm(out, 0)[::-1].hex()} mask {ymm(out, 2)[::-1].hex()}, want {wd[::-1].hex()} {wm[::-1].hex()}')
            record(f'{name}-{block}', problems)
    good8 = [0x100 + 0x48 * j for j in range(8)]
    user('user-pf', 'vpgatherdd', 4, 4, 1, good8[:3] + [-0x800] + good8[4:], 'all', f'unmap {FP:#x}\n', read=reader(unmapped={FP}, cpl=3))
    user('user-us', 'vpgatherqq', 8, 8, 1, good8[:2] + [-0x800, good8[3]], 'msb', f'entry {FP:#x} 1 clear 4\n', s=8, read=reader(supervisor={FP}, cpl=3))
    user('user-gp', 'vpgatherqq', 8, 8, 1, [0, 8, 0x4000000000000000, 16], 'all', s=8)
    user('user-ss', 'vpgatherqq', 8, 8, 0, [0, 0x4000000000000000], 'all', base='rsp', s=8, read=reader(cpl=3, ss=True))
    user('user-nc-masked', 'vpgatherqq', 8, 8, 1, [0, 0x4000000000000000, 0x48, 0x4000000000000000], 'sparse', s=8)

    # ud-*: #UD at the gather (CPL 3, logging IDT), YMM0..2 unchanged; gating.
    def ud(name, hexs, extra='xcr0 7\n', cpl=3, pre='', vector=6):
        # Without AVX enabled no VEX load can set the registers first: then only the #UD at the gather is judged.
        load = f'vmovdqu ymm0, [{T:#x}]\nvmovdqu ymm1, [{T + 32:#x}]\nvmovdqu ymm2, [{T + 64:#x}]\n' if 'xcr0 7' in extra else ''
        code, lab = asm(f'{load}mov rax, {B:#x}\n{pre}g: .byte {",".join("0x" + hexs[i:i + 2] for i in range(0, len(hexs), 2))}\nnop', labels=True)
        regs = rb(96)
        for block in (None, 1):
            text = (f'rip {CODE:#x}\nrflags 0x2\n{gprs({})}\ncode {code.hex()}\ncpl {cpl}\nsse 1\n{extra}memory {D:#x} {pattern.hex()}\nmemory {T:#x} {regs.hex()}\n'
                    'region 0x3e0000 0x10000\n')
            _, out, err = driver(a, text, a.out / 'gather' / f'ud-{name}-{block}', [block] if block else [])
            problems = [f'driver: {err}'] if not out else []
            if out:
                f = [int(v, 0) for v in out['fault']]
                if out['stop'] != 'guest_fault' or f[0] != vector or f[3] != CODE + lab['g']:
                    problems.append(f"stop {out['stop']} {out.get('reason')} fault {f}, want #{vector} at {CODE + lab['g']:#x}")
                if load and ymm(out, 0) + ymm(out, 1) + ymm(out, 2) != regs: problems.append('registers changed')
            record(f'ud-{name}-{block}', problems)
    ud('mask-index', 'c4e27590048890')          # vpgatherdd ymm0, [rax + ymm1*4], ymm1
    ud('dest-index', 'c4e26d900c8890')          # vpgatherdd ymm1, [rax + ymm1*4], ymm2
    ud('dest-mask', 'c4e26d90148890')           # vpgatherdd ymm2, [rax + ymm1*4], ymm2
    ud('no-sib', 'c4e26d900090')                # vpgatherdd ymm0, [rax], ymm2
    ud('register', 'c4e26d90c190')              # vpgatherdd ymm0, ymm1, ymm2
    ud('lock', 'f0c4e26d90048890')
    ud('66', '66c4e26d90048890')
    ud('no-osxsave', 'c4e26d90048890', extra='')
    ud('xcr0-3', 'c4e26d90048890', extra='xcr0 3\n')
    ud('ts', 'c4e26d90048890', cpl=0, pre='mov rbx, cr0\nor rbx, 8\nmov cr0, rbx\n', vector=7)

    # Debug: watch, watch-masked, inlieu, tf, tf-fault, codebreak (one program each, CPL 0, own IDT).
    def debug_case(name, setup, offs, kind, fixes, want_log, after=''):
        d, x, m = inputs(4, 4, 1, offs, 4, kind)
        fx = b''.join(inputs(4, 4, 1, f, 4, 'none')[1] for f in fixes)
        body = (f'mov qword ptr [{FIXP:#x}], {FIXV:#x}\nvmovdqu ymm0, [{T:#x}]\nvmovdqu ymm1, [{T + 32:#x}]\nvmovdqu ymm2, [{T + 64:#x}]\nmov rdi, {B:#x}\n'
                + setup + f'g: vpgatherdd ymm0, [rdi + ymm1*4], ymm2\nn: {after}vmovdqu [{R:#x}], ymm0\nvmovdqu [{R + 32:#x}], ymm2\n'
                + 'pushfq\nand qword ptr [rsp], -0x101\npopfq\nxor eax, eax\nmov dr7, rax\n')
        text, lab = idt_program(body, f'memory {T:#x} {(d + x + m).hex()}\nmemory {R:#x} {bytes(64).hex()}\nmemory {FIXV:#x} {(fx or bytes(32)).hex()}\nunmap {FP:#x}\n')
        cur, read = (d, m, x), reader(unmapped={FP})
        for f in [*(inputs(4, 4, 1, f, 4, 'none')[1] for f in fixes), None]:
            wd, wm, e = gather_model(4, 4, 1, *cur[:2], cur[2], lambda ix: B + ix * 4, read)
            if not e: break
            cur = (wd, wm, f if f is not None else cur[2])
        for block in (None, 1):
            _, out, err = driver(a, text, a.out / 'gather' / f'{name}-{block}', [block] if block else [])
            problems = [f'driver: {err}'] if not out else ([] if out['stop'] == 'reached' else [f"stop {out['stop']} {out.get('reason')} fault {out.get('fault')}"])
            if out and not problems:
                got = [logs(out, k) for k in range(min(count(out), 16))]
                problems += want_log(got, lab, d, m)
                if mem(out, R, 64) != wd + wm: problems.append(f'result {mem(out, R, 64)[::-1].hex()}, want {(wd + wm)[::-1].hex()}')
            record(f'{name}-{block}', problems)
    E = [0x100 + 0x48 * j for j in range(8)]
    dr0 = lambda addr, rw=3: f'mov rax, {addr:#x}\nmov dr0, rax\nmov eax, {0x1 | rw << 16 | 3 << 18 if rw else 1:#x}\nmov dr7, rax\n'
    RF, BS = 1 << 16, 1 << 14
    def want(*spec):
        """spec: (vector, check(entry, lab, d, m) -> problem or None) per entry."""
        def judge(got, lab, d, m):
            if len(got) != len(spec): return [f'{len(got)} handler entries {[(g["vector"], hex(g["rip"])) for g in got]}, want {len(spec)}']
            return [p for g, (v, c) in zip(got, spec) for p in [f'entry vector {g["vector"]}, want {v}' if g['vector'] != v else c(g, lab, d, m)] if p]
        return judge
    done_at = lambda k: lambda g, lab, d, m: None if g['m'][:4 * k] == bytes(4 * k) and g['m'][4 * k:] == m[4 * k:] else f'mask {g["m"][::-1].hex()} at the entry'
    def chk(cond, text): return lambda g, lab, d, m: None if cond(g, lab) else f'{text}: rip {g["rip"]:#x} rflags {g["rflags"]:#x} dr6/cr2 {g["a"]:#x}'
    debug_case('watch', dr0(B + E[1]), E, 'all', [], want((1, chk(lambda g, l: g['rip'] == CODE + l['n'] and g['a'] & 1 and not g['rflags'] & RF, 'trap after it, B0')),))
    debug_case('watch-masked', dr0(B + E[1]), E, 'sparse', [], want())
    debug_case('inlieu', dr0(B + E[0]), E[:2] + [-0x800] + E[3:], 'all', [E],
               want((1, lambda g, lab, d, m: chk(lambda g, l: g['rip'] == CODE + l['g'] and g['a'] & 1 and g['rflags'] & RF, 'in lieu: at the gather, RF, B0')(g, lab, d, m) or done_at(2)(g, lab, d, m)),
                    (14, lambda g, lab, d, m: chk(lambda g, l: g['rip'] == CODE + l['g'] and g['a'] == FP + 0x800, '#PF at element 2')(g, lab, d, m) or done_at(2)(g, lab, d, m))))
    tf = 'pushfq\nor qword ptr [rsp], 0x100\npopfq\n'
    debug_case('tf', tf, E, 'all', [], want((1, chk(lambda g, l: g['rip'] == CODE + l['n'] and g['a'] & BS, 'BS after it')), (1, chk(lambda g, l: True, '')),
                                         (1, chk(lambda g, l: True, '')), (1, chk(lambda g, l: True, '')), (1, chk(lambda g, l: True, '')), (1, chk(lambda g, l: True, ''))))
    debug_case('tf-fault', tf, E[:1] + [-0x800] + E[2:], 'all', [E],
               want((14, chk(lambda g, l: g['rip'] == CODE + l['g'], '#PF at it')), (1, chk(lambda g, l: g['rip'] == CODE + l['n'] and g['a'] & BS, 'BS after it')),
                    (1, chk(lambda g, l: True, '')), (1, chk(lambda g, l: True, '')), (1, chk(lambda g, l: True, '')), (1, chk(lambda g, l: True, '')), (1, chk(lambda g, l: True, ''))))
    debug_case('codebreak', 'lea rax, [rip + g]\nmov dr0, rax\nmov eax, 1\nmov dr7, rax\n', E[:1] + [-0x800] + E[2:], 'all', [E],
               want((1, chk(lambda g, l: g['rip'] == CODE + l['g'] and g['a'] & 1 and not g['rflags'] & RF, 'instruction breakpoint at it')),
                    (14, chk(lambda g, l: g['rip'] == CODE + l['g'] and g['rflags'] & RF, '#PF at it, RF in its frame'))))
    # uc, mmio.
    for name, extra, kind, ok_stop in (('uc', f'entry {B:#x} 1 set 0x18\n', 'all', 'reached'), ('mmio', f'mmio {B:#x} 0xfd000000\n', 'all', 'resident'),
                                       ('mmio-masked', f'mmio {B:#x} 0xfd000000\n', 'sparse', 'reached')):
        # Even elements on B's page (the UC or device page), odd ones on the next; `mmio-masked`: the other way round, so the
        # device page holds only masked-off elements (the sparse mask loads the even ones).
        on = (lambda j: j % 2 == 1) if name == 'mmio-masked' else (lambda j: j % 2 == 0)
        d, x, m = inputs(4, 4, 1, [0x48 * j if on(j) else 0x1000 + 0x48 * j for j in range(8)], 4, kind)
        code = asm(f'vmovdqu ymm0, [{T:#x}]\nvmovdqu ymm1, [{T + 32:#x}]\nvmovdqu ymm2, [{T + 64:#x}]\nmov rdi, {B:#x}\nvpgatherdd ymm0, [rdi + ymm1*4], ymm2\n'
                   f'vmovdqu [{R:#x}], ymm0\nvmovdqu [{R + 32:#x}], ymm2\nnop')
        for block in (None, 1):
            text = (f'rip {CODE:#x}\nrflags 0x2\n{gprs({})}\ncode {code.hex()}\nsse 1\nxcr0 7\nmemory {D:#x} {pattern.hex()}\nmemory {T:#x} {(d + x + m).hex()}\n'
                    f'memory {R:#x} {bytes(64).hex()}\nregion 0x3e0000 0x10000\n{extra}')
            _, out, err = driver(a, text, a.out / 'gather' / f'{name}-{block}', [block] if block else [])
            problems = [f'driver: {err}'] if not out else []
            if out:
                if out['stop'] != ok_stop or (ok_stop == 'resident' and not out['reason'].startswith('unsupported_mmio')):
                    problems.append(f"stop {out['stop']} {out.get('reason')}")
                elif name == 'uc':
                    wd, wm, _ = gather_model(4, 4, 1, d, m, x, lambda ix: B + ix * 4, reader())
                    if mem(out, R, 64) != wd + wm or not out['stats']['k1'].get('uncached', {}).get('resident', 0):
                        problems.append(f"result {mem(out, R, 64)[::-1].hex()} uncached {out['stats']['k1'].get('uncached')}")
            record(f'{name}-{block}', problems)
    # ac: CR0.AM and RFLAGS.AC at CPL 3: misaligned elements raise no #AC (exception class 12); the misaligned MOV after
    # the gather does (#AC(0) there: alignment checking was on).
    d, x, m = inputs(8, 4, 1, [0x101 + 0x43 * j for j in range(4)], 1, 'all')
    wd, wm, _ = gather_model(8, 4, 1, d, m, x, lambda ix: B + ix, reader())
    code, lab = asm(f'mov rax, cr0\nor rax, 0x40000\nmov cr0, rax\npushfq\nor qword ptr [rsp], 0x40000\npopfq\nsw: nop\n'
                    f'vmovdqu ymm0, [{T:#x}]\nvmovdqu ymm1, [{T + 32:#x}]\nvmovdqu ymm2, [{T + 64:#x}]\nmov rdi, {B:#x}\n'
                    f'vpgatherdq ymm0, [rdi + xmm1*1], ymm2\nvmovdqu [{R:#x}], ymm0\nvmovdqu [{R + 32:#x}], ymm2\nx: mov eax, [{B + 1:#x}]\nnop', labels=True)
    for block in (None, 1):
        text = (f'rip {CODE:#x}\nrflags 0x2\n{gprs({})}\ncode {code.hex()}\ncpl 0\nswitch {CODE + lab["sw"]:#x} 3\nsse 1\nxcr0 7\n'
                f'memory {D:#x} {pattern.hex()}\nmemory {T:#x} {(d + x + m).hex()}\nmemory {R:#x} {bytes(64).hex()}\nregion 0x3e0000 0x10000\n')
        _, out, err = driver(a, text, a.out / 'gather' / f'ac-{block}', [block] if block else [])
        problems = [f'driver: {err}'] if not out else []
        if out:
            f = [int(v, 0) for v in out['fault']]
            if out['stop'] != 'guest_fault' or f[0] != 17 or f[3] != CODE + lab['x']: problems.append(f"stop {out['stop']} {out.get('reason')} fault {f}")
            if mem(out, R, 64) != wd + wm: problems.append(f'result {mem(out, R, 64)[::-1].hex()}, want {(wd + wm)[::-1].hex()}')
        record(f'ac-{block}', problems)
    # tso: vCPU 1 stores X (B + 0x100) then Y (B + 0x200) := r, rounds 1..N; vCPU 0 loads Y, then gathers X (element 0; the
    # others read B): X >= that Y. R8 counts violations.
    N = 100_000
    c0 = asm(f'mov ecx, {N}\nvpcmpeqd ymm4, ymm4, ymm4\nvpxor ymm5, ymm5, ymm5\n'
             f'mov rdi, {B:#x}\n1: mov rax, [{B + 0x200:#x}]\nvmovdqa ymm2, ymm4\nvpinsrq xmm1, xmm5, qword ptr [{T:#x}], 0\n'
             f'vpgatherqq ymm0, [rdi + ymm1*1], ymm2\nvmovq rbx, xmm0\ncmp rbx, rax\njae 2f\ninc r8\n2: dec ecx\njnz 1b\nnop')
    c1 = asm(f'mov ecx, {N}\nxor eax, eax\n1: inc rax\nmov [{B + 0x100:#x}], rax\nmov [{B + 0x200:#x}], rax\ndec ecx\njnz 1b\nnop')
    for block in (None, 1):
        text = (f'body {CODE:#x} {c0.hex()}\nbody 0x200000 {c1.hex()}\nmemory {D:#x} {bytes(0xa000).hex()}\nmemory {T:#x} {(0x100).to_bytes(8, "little").hex()}\n'
                'region 0x3e0000 0x10000\n'
                f'cpu\nrip {CODE:#x}\nrflags 0x2\n{gprs({})}\nsse 1\nxcr0 7\ncpu\nrip 0x200000\nrflags 0x2\n{gprs({4: STACK - 0x8000})}\n')
        _, out, err = driver(a, text, a.out / 'gather' / f'tso-{block}', [block] if block else [])
        problems = [f'driver: {err}'] if not out else []
        if out:
            stops = [c['stop'] for c in out['cpus']]
            if stops != ['reached', 'reached'] or int(out['cpus'][0]['gpr'][8], 0): problems.append(f"stops {stops} violations {out['cpus'][0]['gpr'][8]}")
        record(f'tso-{block}', problems)
    report['gather'] = {'runs': runs, 'failures': failures}
    return failures


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--fexc', type=pathlib.Path, required=True)
    ap.add_argument('--out', type=pathlib.Path, required=True)
    ap.add_argument('--only', type=lambda s: s.split(','), default=['cost', 'rdrand', 'xmc', 'coherence', 'rollback', 'nopruns', 'tsc', 'swapgs', 'sysfast', 'stifirst', 'stmxcsr', 'repbulk', 'cr2', 'verw', 'gather'])
    ap.add_argument('--n', type=int, default=100_000)
    ap.add_argument('--reps', type=int, default=3)
    ap.add_argument('--cost-rows', type=lambda s: s.split(','), default=None, help='cost: only these rows')
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
    if 'stifirst' in a.only: failures += stifirst(a, report)
    if 'stmxcsr' in a.only: failures += stmxcsr(a, report)
    if 'repbulk' in a.only: failures += repbulk(a, report)
    if 'cr2' in a.only: failures += cr2(a, report)
    if 'verw' in a.only: failures += verw(a, report)
    if 'gather' in a.only: failures += gather(a, report)
    (a.out / 'k2.json').write_text(json.dumps(report, indent=1) + '\n')
    for f in failures: print('FAIL', f)
    sys.exit(1 if failures else 0)


if __name__ == '__main__': main()
