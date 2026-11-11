#!/usr/bin/env python3
"""PIC study checks (docs/restructure/pic-plan.md section 6): the K1 programs (tools/run_k1.py, natemu only, CPL 0; the
program driver shares every privilege's templates once a second address needs them: the reuse and pic programs run shared
code) under each of the resident's test entries (`NATEMU_PIC_TEST`, `cache::Control::pic_test`), on every second entry
of a shared translation's instance from the resident:

- `and`, `str`, `body`: entered at its full entry's `and` (x0 = the instance's address), its `str` (x0 = its page base) or
  its body (the page base in the guest-state page) with the vCPU's own doorbell rung: the stop there must take the
  instance's address (every program's values as without the test), stops counted at that word (`pic` counter
  ENTRY_STOPS + 0, 1, 2).
- `svc`: only the `svc`-word (checked) links of any block entered armed, and no L1 entry filled (blocks are entered
  from the resident after their links are made): stops at a linked `svc` word, the RIP x0 (SVC_STOPS > 0).
- `forge`, `forge-noncanonical`, `forge-half`: another page's base in the slot (the guest corrupting its own state: the
  base XOR 0x1000, XOR bit 47, XOR bits 47-63): the programs' values may differ, but the resident never stops on it (no
  bad_link, bad_stop, reuse_mismatch, panic or unknown abort). Only a run's first entries are forged
  (`cache::PIC_FORGES`): K1 `pic-again` (pic-two twice) must give its exact values in its second run.
- `ac` (no K1 programs): one user frame at two upper-half user pages above 4 GiB (the driver's own tables), CR0.AM and
  RFLAGS.AC on at CPL 3: two calls through the first page (placed) and three through the second (shared, with a local
  link inside): the sums of the aliases and the calls, then a deliberately misaligned load: #AC(0) there and nothing else.
- `fence` (no K1 programs; review of stage 3, finding 1): frame F holds a loop head A at 0 (`nop; inc r9; dec ecx;
  jnz A; jmp r13`) and B at 0x40 (`mov ecx, 3; jmp A`), called at L1, twice at L2 (shared, B locally linked to A), then
  at L3 with the driver's hook at L3 + A (its `nop` switches to CPL 3): B at L3 must not reach A through the local link;
  the hook is taken (the MOV from CR0 after the calls is #GP(0) at CPL 3), and L2 is still shared.

- `user` (and under each mode above): CPL-3 ports of K1 programs in two user address spaces switched by CR3 at CPL 0
  (the driver's `switch` hooks): `user-two`, `user-cow`, `user-twoframe`, `user-pfault` (a #PF at the alias's own RIP
  and CR2 in shared code reached by a local link), each with its exact values; under the stop modes each must stop at
  the tested word.

The tested stops are required per program (every `pic-*` run with an instance; `svc`: pic-two, pic-ctx, pic-xm,
pic-twosrc). Every run also requires links of each form (local shared to shared on one page; checked, through the dispatcher slot:
from shared code, and from absolute code into shared) and the host's link audit after each program
(`check_links`, in the driver) with all of them.

    tools/with-env python3 tools/run_pic.py --fexc FEXC --out DIR [--cpus 1 2] [--modes and,...,ac] [--programs a,b]

Run under the shared VM lock (tools/run_suite.py takes it); exit 1 on any failure."""
import argparse, json, os, pathlib, subprocess, sys, types

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / 'tools'), str(ROOT / 'tools' / 'reference'), str(ROOT / 'tools' / 'bench' / 'smp')]
import ref, run_k1 as K, run_k2, run_slice1_vm as vm, programs as P  # noqa: E402
# `natemu_abi::pic::count`.
SAME, ABS_TO_SHARED, UNPROVED, ENTRY_STOPS, SVC_STOPS, SHARES, INSTANCES, CHECKED = 10, 13, 19, 21, 24, 25, 26, 28
MODES = 'and,str,body,svc,forge,forge-noncanonical,forge-half,ac,fence,user'
SVC_PROGRAMS = ('pic-two', 'pic-ctx', 'pic-xm', 'pic-twosrc')


# The driver cases' space: CODE (two pages), user frames F, F2, FT mapped at upper-half user pages above 4 GiB (FEX's
# 64-bit RIP moves; K1's addresses, `run_k1`), the stack and IDT pages below WSTACK, the driver's logging pages; per
# address space (A, B) its own tables (PML4 and seven more pages); each case's code loads A's first.
F, F2, FT, CODE, WSTACK, IDT = 0x104000, 0x105000, 0x106000, 0x100000, 0x3d0000, 0x3c8000
L1, L2, L3 = K.RV1, K.RV3, K.RV5
PML4, PML4B = 0x110000, 0x120000


def space(code, frames, maps, rflags, extra):
    """The driver description of a case: `code` at CODE (CPL 0, IF 0), `frames` {address: bytes}, `maps` {PML4: {user
    page: frame}} (each with the identity part), `extra` lines."""
    NONLEAF = P.P | P.RW | P.US | P.A
    ix = lambda va, k: va >> (12 + 9 * k) & 511
    items = ''
    for root, m in maps.items():
        PDPT, PD, PT0, PT1, PDPTH, PDH, PTH = (root + 0x1000 * k for k in range(1, 8))
        tables = {root: {0: PDPT | NONLEAF, ix(L1, 3): PDPTH | NONLEAF}, PDPT: {0: PD | NONLEAF}, PD: {0: PT0 | NONLEAF, 1: PT1 | NONLEAF},
                  PDPTH: {ix(L1, 2): PDH | NONLEAF}, PDH: {ix(L1, 1): PTH | NONLEAF}, PTH: {ix(v, 0): f | P.LEAF for v, f in m.items()},
                  PT0: {ix(v, 0): v | P.LEAF for v in (CODE, CODE + 0x1000)},
                  PT1: {ix(v, 0): v | P.LEAF for v in [*range(0x3c0000, WSTACK, 0x1000), *(0x3e8000 + o for o in (0, 0x1000, 0x3000, 0x4000, 0x5000, 0x6000))]}}
        items += ''.join(f'memory {t:#x} {b"".join(e.get(i, 0).to_bytes(8, "little") for i in range(512)).hex()}\n' for t, e in tables.items())
    bodies = ''.join(f'body {at:#x} {bytes(data).hex()}\n' for at, data in frames.items())
    return (f'identity\nrip {CODE:#x}\nrflags {rflags:#x}\n{run_k2.gprs({4: WSTACK})}\ncode {code.hex()}\n{bodies}cpl 0\nif 0\n'
            f'{items}region 0x3c0000 {WSTACK - 0x3c0000:#x}\n{extra}')


def calls(vas):
    return ''.join(f'lea r13, [rip + c{k}]\nmov rax, {va:#x}\njmp rax\nc{k}:\n' for k, va in enumerate(vas))


def ac(a):
    """The `ac` case (module docstring): its problems."""
    code, lab = ref.assemble(f'mov eax, {PML4:#x}\nmov cr3, rax\nmov rax, cr0\nbts rax, 18\nmov cr0, rax\nsw: nop\n'
                             f'xor r8d, r8d\nxor r9d, r9d\n{calls([L1] * 2 + [L2] * 3)}ld: mov eax, [rsp + 1]\n', labels=True)
    page = ref.assemble('lea rax, [rip - 7]\nadd r8, rax\njmp 1f\n1: inc r9\njmp r13')
    text = space(code, {F: page}, {PML4: {L1: F, L2: F, L3: F}}, 0x40002, f'switch {CODE + lab["sw"]:#x} 3\n')
    _, out, err = run_k2.driver(a, text, a.out / 'ac', ['--watchdog-ms', '60000'])
    if not out:
        return [f'driver: {err}']
    g = [int(x, 0) for x in out['gpr']]
    pic = out['stats']['k1']['pic']
    problems = []
    if out['stop'] != 'guest_fault' or [int(v, 0) for v in out['fault']] != [17, 0, 0, CODE + lab['ld']]:
        problems.append(f"stop {out['stop']} {out.get('fault')}, want #AC(0) at {CODE + lab['ld']:#x}")
    if (g[8], g[9]) != (2 * L1 + 3 * L2, 5):
        problems.append(f'r8 {g[8]:#x} r9 {g[9]}, want {2 * L1 + 3 * L2:#x} 5')
    if pic[SHARES] < 1 or pic[INSTANCES] < 1 or pic[SAME] < 1:
        problems.append(f'shares {pic[SHARES]}, instances {pic[INSTANCES]}, local links {pic[SAME]}: want each at least 1')
    return problems

def fence(a):
    """The `fence` case (module docstring): its problems."""
    code, lab = ref.assemble(f'mov eax, {PML4:#x}\nmov cr3, rax\nxor r9d, r9d\n{calls([L1 + 0x40, L2 + 0x40, L2 + 0x40, L3 + 0x40])}'
                             f'pv: mov rax, cr0\n', labels=True)
    page = bytearray(0x4a)
    head = ref.assemble('1: nop\ninc r9\ndec ecx\njnz 1b\njmp r13')
    page[:len(head)] = head
    page[0x40:0x45] = ref.assemble('mov ecx, 3')
    page[0x45:0x4a] = b'\xe9' + (-0x4a).to_bytes(4, 'little', signed=True)
    text = space(code, {F: page}, {PML4: {L1: F, L2: F, L3: F}}, 2, f'switch {L3:#x} 3\n')
    _, out, err = run_k2.driver(a, text, a.out / 'fence', ['--watchdog-ms', '60000'])
    if not out:
        return [f'driver: {err}']
    g = [int(x, 0) for x in out['gpr']]
    pic = out['stats']['k1']['pic']
    problems = []
    # The hook taken (CPL 3 from A at L3 on): the MOV from CR0 after the calls is #GP(0).
    if out['stop'] != 'guest_fault' or [int(v, 0) for v in out['fault']] != [13, 0, 0, CODE + lab['pv']]:
        problems.append(f"stop {out['stop']} {out.get('fault')}, want #GP(0) at {CODE + lab['pv']:#x} (the hook at L3 taken)")
    if g[9] != 12:
        problems.append(f'r9 {g[9]}, want 12')
    if pic[SHARES] < 1:
        problems.append('nothing shared (L2, on no hook\'s page, should be)')
    return problems


# CPL-3 ports of K1 programs (two user address spaces, A and B, switched at CPL 0 between the driver's `switch` hooks):
# F holds Y (`run_k1.Y`) at 0 and W (a block local-linked into one that loads [r11]) at WO; F2 Y(2); FT the trampolines
# (`jmp` RV3 at 0, `jmp` RV1 at 0x100). Each item: an address to call, ('save', reg, reg), ('cr3', root), ('set', reg,
# value).
WO = 0x600
W_HEAD = 'mov ebx, 7\nlea rax, [rel $$]\njmp .b\n.b:\nmov ecx, 1\n'
W = P.asm(W_HEAD + 'mov rdx, [r11]\njmp r13')
W_FAULT = WO + len(P.asm(W_HEAD))
V1, V3, V5, VT, VT2 = K.RV1, K.RV3, K.RV5, K.RVT, K.RVT2
FRAMES = {F: bytes(K.Y(1)) + bytes(WO - len(K.Y(1))) + W, F2: K.Y(2),
          FT: K.JMP(VT, V3) + bytes(0x100 - 5) + K.JMP(VT + 0x100, V1)}
sv = lambda *pairs: [('save', d, r) for d, r in pairs]
USER = {
    # pic-two, then process B with F at V5: one translation at three addresses in two address spaces.
    'user-two': ([V1, V3, V3, V3, *sv(('r8', 'rax'), ('r9', 'rsi')), VT, VT, VT, *sv(('r10', 'rax')), VT + 0x100, VT + 0x100,
                  VT + 0x100, *sv(('r12', 'rax'), ('r14', 'rsi')), V1, *sv(('r15', 'rbx')), ('cr3', PML4B), V5, V5, *sv(('rdi', 'rax')),
                  ('cr3', PML4), V3, *sv(('rbp', 'rax'))],
                 {PML4: {V1: F, V3: F, VT: FT}, PML4B: {V5: F}},
                 {'r8': V3, 'r9': V3 + K.YN, 'r10': V3, 'r12': V1, 'r14': V1 + K.YN, 'r15': 1, 'rdi': V5, 'rbp': V3}, None, (2, 1)),
    # pic-cow: B's V1 another frame (a child that wrote the page).
    'user-cow': ([V1, V3, V3, V3, ('cr3', PML4B), V1, V1, V1, *sv(('r8', 'rbx'), ('r9', 'rax')), ('cr3', PML4), V1, *sv(('r10', 'rbx')),
                  V3, *sv(('r12', 'rbx'))],
                 {PML4: {V1: F, V3: F}, PML4B: {V1: F2}}, {'r8': 2, 'r9': V1, 'r10': 1, 'r12': 1}, None, (1, 0)),
    # pic-twoframe, then B, where V5 maps F: the trampoline's one checked link reaches each process's own frame.
    'user-twoframe': ([V3, V1, VT, *[x for _ in range(3) for x in (VT, *sv(('r8', 'rax'), ('r10', 'rbx')), VT2, *sv(('r9', 'rax'), ('r12', 'rbx')))],
                       ('cr3', PML4B), VT2, VT2, *sv(('rdi', 'rax'), ('rbp', 'rbx'))],
                      {PML4: {V1: F, V3: F, V5: F2, VT: FT, VT2: FT}, PML4B: {VT2: FT, V5: F}},
                      {'r8': V3, 'r9': V5, 'r10': 1, 'r12': 2, 'rdi': V5, 'rbp': 1}, None, (2, 1)),
    # pic-pfault: W placed at V1, shared at V3 (its local link), then in B at V5, the load's address made not present:
    # #PF at V5's own RIP, CR2 the address.
    'user-pfault': ([('set', 'r11', 0x3c0000), V1 + WO, V3 + WO, V3 + WO, V3 + WO, ('cr3', PML4B), V5 + WO, V5 + WO,
                     ('set', 'r11', K.PF_VA), V5 + WO],
                    {PML4: {V1: F, V3: F}, PML4B: {V5: F}}, {'rbx': 7}, (14, V5 + W_FAULT, K.PF_VA), (2, 0)),
}


def user_text(items):
    """A case's code (CPL 0 start: CR3 A, then CPL 3) and its `switch` lines."""
    text, switches = f'mov eax, {PML4:#x}\nmov cr3, rax\ns3_0: nop\n', [('s3_0', 3)]
    for k, it in enumerate(items):
        if isinstance(it, int):
            text += f'lea r13, [rip + c{k}]\nmov rax, {it:#x}\njmp rax\nc{k}:\n'
        elif it[0] == 'save':
            text += f'mov {it[1]}, {it[2]}\n'
        elif it[0] == 'set':
            text += f'mov {it[1]}, {it[2]:#x}\n'
        else:
            text += f'mov eax, {it[1]:#x}\nlea r13, [rip + c{k}]\njmp sw\nc{k}:\n'
    # (One CR3 switch for all, a hook pair at CPL 0: the host keeps at most four hook ranges as fences.)
    switches += [('s0', 0), ('s3', 3)]
    code, lab = ref.assemble(text + 'jmp end\nsw:\ns0: nop\nmov cr3, rax\ns3: nop\njmp r13\nend: nop\n', labels=True)
    return code, lab, ''.join(f'switch {CODE + lab[l]:#x} {c}\n' for l, c in switches)


def user(a, name, env):
    """One CPL-3 case under `env` (NATEMU_PIC_TEST or none): (its problems, its `pic` counters)."""
    items, maps, want, fault, (least_instances, least_checked) = USER[name]
    code, lab, switches = user_text(items)
    text = space(code, FRAMES, maps, 2, switches)
    saved = dict(os.environ)
    os.environ.update(env)
    try:
        _, out, err = run_k2.driver(a, text, a.out / name / env.get('NATEMU_PIC_TEST', 'clean'), ['--watchdog-ms', '60000'])
    finally:
        os.environ.clear()
        os.environ.update(saved)
    if not out:
        return [f'driver: {err}'], [0] * 32
    g = [int(x, 0) for x in out['gpr']]
    pic = out['stats']['k1']['pic']
    problems = [f'{r} {g[ref.REGS.index(r)]:#x}, want {v:#x}' for r, v in want.items() if g[ref.REGS.index(r)] != v]
    got = [int(x, 0) for x in out.get('fault', [])]
    if fault:
        cr2 = int(out.get('delivered', {}).get('cr2', '-1'), 0) if isinstance(out.get('delivered', {}).get('cr2'), str) else None
        if out['stop'] != 'guest_fault' or not got or (got[0], got[-1]) != fault[:2] or cr2 not in (None, fault[2]):
            problems.append(f"stop {out['stop']} fault {out.get('fault')} delivered {out.get('delivered')}, want #PF at {fault[1]:#x} CR2 {fault[2]:#x}")
    elif out['stop'] != 'reached' or int(out['rip'], 0) != CODE + lab['end'] + 1:
        problems.append(f"stop {out['stop']} {out.get('reason')} at {out.get('rip')} {out.get('fault')}")
    if pic[INSTANCES] < least_instances or pic[CHECKED] < least_checked:
        problems.append(f'instances {pic[INSTANCES]}, checked links {pic[CHECKED]}: at least {least_instances}, {least_checked}')
    return problems, pic


HALTS = ('bad_link', 'bad_stop', 'bad_reply', 'reuse_mismatch', 'status=4', 'unknown_abort')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--fexc', required=True)
    ap.add_argument('--out', type=pathlib.Path, required=True)
    ap.add_argument('--cpus', nargs='+', default=['1', '2'])
    ap.add_argument('--modes', default=MODES)
    ap.add_argument('--programs', help="run_k1.py's --programs")
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    failed = []
    for mode in a.modes.split(','):
        a.host, a.runtime = vm.build()
        d = types.SimpleNamespace(host=a.host, runtime=a.runtime, fexc=a.fexc, out=a.out)
        if mode in ('ac', 'fence'):
            bad = {'ac': ac, 'fence': fence}[mode](d)
            print(f"{mode}: {'FAIL ' + '; '.join(bad) if bad else 'ok'}", flush=True)
            failed += [mode] * bool(bad)
            continue
        # The CPL-3 cases (clean in `user`; under each test mode with it, as the K1 programs below).
        env = {} if mode == 'user' else {'NATEMU_PIC_TEST': mode}
        user_bad = []
        for name in USER:
            problems, pic = user(d, name, env)
            if mode.startswith('forge'):
                problems = [p for p in problems if any(h in p for h in HALTS) or p.startswith('driver')]
            elif mode in ('and', 'str', 'body', 'svc'):
                want = SVC_STOPS if mode == 'svc' else ENTRY_STOPS + ['and', 'str', 'body'].index(mode)
                if not pic[want] and (mode != 'svc' or pic[CHECKED]):
                    problems.append(f'no stop at the tested word (counter {want})')
            user_bad += [f'{name}: {p}' for p in problems]
        if mode == 'user':
            print(f"user: {'FAIL ' + '; '.join(user_bad) if user_bad else 'ok'} ({', '.join(USER)})", flush=True)
            failed += [mode] * bool(user_bad)
            continue
        out = a.out / mode
        env = dict(os.environ, NATEMU_PIC_TEST=mode)
        rc = subprocess.run([sys.executable, str(ROOT / 'tools/run_k1.py'), '--fexc', a.fexc, '--out', str(out),
                             '--cpus', *a.cpus, *(['--programs', a.programs] if a.programs else [])],
                            env=env, stdout=open(a.out / f'{mode}.log', 'w'), stderr=subprocess.STDOUT).returncode
        report = json.loads((out / 'k1.json').read_text())['report']
        stats = [s['k1']['pic'] for r in report for s in r.get('stats') or [] if 'k1' in s]
        total = [sum(p[i] for p in stats) for i in range(len(stats[0]) if stats else 33)]
        problems = [p for r in report for p in r['problems']]
        bad = []
        bad += user_bad
        if mode.startswith('forge'):
            bad += [p for p in problems if any(h in p for h in HALTS)]
            # (Forged only in a run's first entries: `pic-again`'s second run must give its exact values.)
            bad += [f"{r['name']}: {p}" for r in report if r['name'].startswith('k1-pic-again') for p in r['problems']]
        else:
            if rc != 0 or problems:
                bad.append(f'run_k1 exit {rc}, {len(problems)} problems (first: {problems[:2]})')
            want = SVC_STOPS if mode == 'svc' else ENTRY_STOPS + ['and', 'str', 'body'].index(mode)
            # Per program: each pic-* run with an instance stops at the tested word; `svc`: each of SVC_PROGRAMS (whose
            # checked links run after they are made; in others a fault or a write ends them first).
            missed = [f"{r['name']}{'/1' if r['max_block'] else ''}" for r in report
                      if (r['name'].split('@')[0][3:] in SVC_PROGRAMS if mode == 'svc' else r['name'].startswith('k1-pic-'))
                      for s in r.get('stats') or [] if (mode == 'svc' or s['k1']['pic'][INSTANCES]) and not s['k1']['pic'][want]]
            if missed or total[want] == 0:
                bad.append(f'no stop at the tested word (counter {want}) in {missed or "any run"}')
        for k, name in [(SAME, 'local shared'), (CHECKED, 'checked'), (ABS_TO_SHARED, 'absolute into shared')]:
            if total[k] == 0:
                bad.append(f'no {name} link')
        if total[UNPROVED]:
            bad.append(f'{total[UNPROVED]} rewrites unproved')
        print(f"{mode}: {'FAIL ' + '; '.join(bad) if bad else 'ok'} (entry stops {total[ENTRY_STOPS:ENTRY_STOPS + 3]}, svc stops "
              f"{total[SVC_STOPS]}, links local/checked/abs->shared "
              f"{total[SAME]}/{total[CHECKED]}/{total[ABS_TO_SHARED]})", flush=True)
        failed += [mode] * bool(bad)
    return int(bool(failed))


if __name__ == '__main__':
    sys.exit(main())
