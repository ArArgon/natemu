"""x86 ordering between an interpreting vCPU and a translated one (boot-interpreter plan sections 4 and 8): litmus programs
for natemu's program driver, two vCPUs over one guest RAM, judged by tools/models/ordering_model.py. Run by
run_interp_natemu.py --litmus (case `natemu-litmus`).

vCPU A runs in protected mode with paging off (P32, CPL 0), so the boot-time interpreter executes every instruction and
makes every access through its memory layer (`interpret.rs`'s `Mem`, at EL1, through the guest-RAM alias); vCPU B runs in
64-bit mode at CPL 0, so its code is translated (EL0). Each pattern is one of the model's two-participant profiles, with A
as one participant and B as the other:

- `mp-a-writes`: message passing, A stores data then flag, B loads flag then data (A's store-store order);
- `mp-a-reads`: the same with the roles swapped (A's load-load order);
- `sb`: store buffering, each stores its location and loads the other's (store-load: both old is allowed under TSO and
  only counted);
- `lb`: load buffering, each loads the other's location and stores its own (load-store).

Every iteration's outcome must be in the model's allowed set for the profile. A's two accesses are a whole interpreted
instruction apart, B's a few native instructions, and a run whose B never landed between A's two could not see them out
of order: so each pattern must also show its overlap outcomes, the sequentially consistent outcomes that neither
participant running alone first gives (`classes`), in at least `MIN_OVERLAP` of the iterations. Each such iteration
sampled the stretch between A's two accesses, where a reordering of them would show as a forbidden outcome.

The protocol per iteration (after tools/bench/smp/programs.py's litmus programs: per-iteration sequence words, a random
start delay, one result byte per output): B waits until A has finished the previous iteration, resets the locations and
publishes its sequence word; A waits for it, publishes its own and runs its accesses after its delay; B runs its accesses
once it sees A's word, after a random delay of 0 to `MASK['B']` spins, which spreads them across A's accesses. Each
records its outputs at its result area, then its done word."""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'tests' / 'lib')); import testenv  # noqa: E401,E402,E702
import collections
import math

import interp_cases as G
from models import ordering_model as model

SYNC = 0x40000                           # the sequence and done words and the locations, each on a 128-byte line of its own
SEQ = {'A': 0x000, 'B': 0x080}
DONE = {'A': 0x200, 'B': 0x280}
LOC = {'x': 0x400, 'y': 0x480}
SYNC_BYTES = 0x500
RES = {'A': 0x100000, 'B': 0x140000}     # per iteration STRIDE bytes: the outputs in the model's order
STRIDE = 2
CODE = {'A': 0x10000, 'B': 0x13000}      # each on a page of its own, away from the data
MASK = {'A': 1, 'B': 2047}               # random delay, spins of `dec ecx; jns`: B's spans A's accesses (on the M2 Max
                                         # a sixth of B's draws land between them)
SEED = {'A': 0x9E3779B9, 'B': 0x7F4A7C15}
MIN_OVERLAP = 0.005                      # the share of iterations that must show an overlap outcome
WATCHDOG_MS = 120_000                    # neither vCPU exits to the host while it loops (the driver's default is 10 s)

# (pattern, the model's profile, A's participant)
CELLS = (('mp-a-writes', 'mp', 0), ('mp-a-reads', 'mp', 1), ('sb', 'sb', 0), ('lb', 'lb', 0))


def cpu_text(test: model.Litmus, p: int, side: str) -> list:
    """nasm lines of participant `p` of `test` run by `side` ('A': 32-bit code, interpreted; 'B': 64-bit code, translated).
    Registers: EBX the iteration (1..k), EDX k, EDI the sync page, ESI the result pointer, EBP the delay's xorshift32
    state; EAX and ECX the loaded values (in the order the program first loads them), ECX the delay before that. Only
    forms of the interpreter's first version (plan section 1) on A."""
    w = 'r' if side == 'B' else 'e'
    di, si = f'[{w}di+', f'{w}si'
    other = 'A' if side == 'B' else 'B'
    regs = {}

    def reg(name):
        if name not in regs:
            regs[name] = ('eax', 'ecx')[len(regs)]
        return regs[name]
    lines = ['    mov ebx, 1', '.top:',
             # this iteration's delay, drawn before the barrier
             '    mov eax, ebp', '    shl eax, 13', '    xor ebp, eax', '    mov eax, ebp', '    shr eax, 17', '    xor ebp, eax',
             '    mov eax, ebp', '    shl eax, 5', '    xor ebp, eax', '    mov ecx, ebp', f'    and ecx, {MASK[side]}']
    if side == 'B':
        lines += [f'    lea eax, [{w}bx-1]', f'.wd: cmp {di}{DONE[other]:#x}], eax', '    jae .od', '    jmp .wd', '.od:',
                  *[f'    mov dword {di}{LOC[a]:#x}], {v}' for a, v in test.initial],
                  f'    mov {di}{SEQ[side]:#x}], ebx',
                  f'.ws: cmp {di}{SEQ[other]:#x}], ebx', '    jae .os', '    jmp .ws', '.os:']
    else:
        lines += [f'.ws: cmp {di}{SEQ[other]:#x}], ebx', '    jae .os', '    jmp .ws', '.os:', f'    mov {di}{SEQ[side]:#x}], ebx']
    lines += ['.dl: dec ecx', '    jns .dl']
    for op in test.programs[p]:
        if op[0] == 'store':
            lines.append(f'    mov dword {di}{LOC[op[1]]:#x}], {op[2]}')
        elif op[0] == 'load':
            lines.append(f'    mov {reg(op[1])}, {di}{LOC[op[2]]:#x}]')
        else:
            raise ValueError(f'{test.name}: {op[0]} has no form here')
    outs = [r for c, r in test.outputs if c == p]
    lines += [f'    mov [{si}+{j}], {regs[r][1]}l' for j, r in enumerate(outs)]
    lines += [f'    mov {di}{DONE[side]:#x}], ebx', f'    add {si}, {STRIDE}', '    inc ebx', '    cmp ebx, edx', '    jbe .top']
    return lines


def programs(cell: tuple) -> dict:
    """{'A': bytes, 'B': bytes} of a cell (pattern, profile, A's participant)."""
    _, name, a = cell
    test = model.profile(name)
    (code_a,), (code_b,) = G.assemble([cpu_text(test, a, 'A')], 32), G.assemble([cpu_text(test, 1 - a, 'B')], 64)
    return {'A': code_a, 'B': code_b}


def description(cell: tuple, k: int) -> str:
    """The machine description of a cell at k iterations: the corpus's common blocks (its GDT and identity tables), both
    programs, A in P32 and B in 64-bit mode at CPL 0, each stopping at its program's end, with its results read back (and
    B the sync page)."""
    if not 1 <= k <= (RES['B'] - RES['A']) // STRIDE:
        raise ValueError(f'{k} iterations: 1 to {(RES["B"] - RES["A"]) // STRIDE}')
    code = programs(cell)
    lines = ['machine', *[f'memory {a:#x} {d.hex()}' for _, a, d in G.common_blocks()],
             f'memory {SYNC:#x} {bytes(SYNC_BYTES).hex()}', *[f'memory {CODE[s]:#x} {code[s].hex()}' for s in 'AB']]
    segs = {'A': G.start_segments('P32'),
            'B': {s: G.loaded(0x28 if s == 'cs' else 0x10) for s in G.SREGS}}
    ctl = {'A': {k2: G.MODES['P32'][k2] for k2 in ('cr0', 'cr3', 'cr4', 'efer')},
           'B': dict(cr0=G.CR0_PG | G.CR0_ET | G.CR0_PE, cr3=G.PML4, cr4=G.CR4_PAE, efer=G.EFER_LME | G.EFER_LMA)}
    idtr = {'A': G.MODES['P32']['idtr'], 'B': (G.IDT64, 0x1ff)}
    for s in 'AB':
        gpr = [0] * 16
        gpr[G.GPRS.index('edx')], gpr[G.GPRS.index('ebp')] = k, SEED[s]
        gpr[G.GPRS.index('esi')], gpr[G.GPRS.index('edi')], gpr[G.GPRS.index('esp')] = RES[s], SYNC, G.STACK
        lines += ['cpu', f'rip {CODE[s]:#x}', 'rflags 0x2', 'cpl 0', 'gpr ' + ' '.join(f'{v:#x}' for v in gpr),
                  *[f'{r} {v:#x}' for r, v in ctl[s].items()],
                  *[f"seg {r} {g['sel']:#x} {g['base']:#x} {g['limit']:#x} {g['attr']:#x}" for r, g in ((r, segs[s][r]) for r in G.SREGS)],
                  f'gdtr {G.GDT:#x} {G.GDT_LIMIT:#x}', f'idtr {idtr[s][0]:#x} {idtr[s][1]:#x}',
                  f'stop {CODE[s] + len(code[s]):#x}', f'window {RES[s]:#x} {STRIDE * k:#x}']
    lines.append(f'window {SYNC:#x} {SYNC_BYTES:#x}')
    return '\n'.join(lines) + '\n'


def classes(test: model.Litmus) -> tuple:
    """(allowed, overlap, relaxed) outcome sets of a two-participant profile: the model's allowed set; the sequentially
    consistent outcomes (the profile with a fence after every store) that neither participant running alone first gives;
    the allowed ones TSO adds to those (store buffering's both old)."""
    allowed = model.enumerate_outcomes(test).outcomes
    fenced = tuple(tuple(x for op in prog for x in ((op, ('fence',)) if op[0] == 'store' else (op,))) for prog in test.programs)
    sc = model.enumerate_outcomes(model.Litmus(test.name + '-sc', fenced, test.initial, test.outputs)).outcomes

    def alone(order):
        prog = tuple(op for p in order for op in test.programs[p])
        (o,) = model.enumerate_outcomes(model.Litmus(test.name + '-alone', (prog,), test.initial,
                                                     tuple((0, r) for _, r in test.outputs))).outcomes
        return o
    return allowed, sc - {alone((0, 1)), alone((1, 0))}, allowed - sc


def outcomes(test: model.Litmus, a: int, k: int, res: dict) -> list:
    """Per-iteration outcomes (the model's output order) from the result areas ({'A': bytes, 'B': bytes})."""
    side = {a: 'A', 1 - a: 'B'}
    index = {(c, r): [r2 for c2, r2 in test.outputs if c2 == c].index(r) for c, r in test.outputs}
    return [tuple(res[side[c]][STRIDE * it + index[(c, r)]] for c, r in test.outputs) for it in range(k)]


def judge(cell: tuple, k: int, out: dict) -> tuple:
    """(outcome counts, diffs) of one run's driver output: both vCPUs at their end (stop 0) with every iteration done,
    and the outcomes (forbidden ones are judged over all runs, `verdict`)."""
    _, name, a = cell
    cpus = out.get('cpus') or []
    if len(cpus) != 2:
        return collections.Counter(), [f'{len(cpus)} vCPUs reported: {out.get("stop")}: {str(out.get("reason"))[:300]}']
    diffs, res = [], {}
    for s, c in zip('AB', cpus):
        m = c.get('machine') or {}
        if c.get('stop') != 'reached' or m.get('stop') != 0:
            diffs.append(f'vCPU {s} stopped {c.get("stop")}: {str(c.get("reason"))[:300]}')
        res[s] = bytes.fromhex(m.get('window0', ''))
    sync = bytes.fromhex((cpus[1].get('machine') or {}).get('window1', ''))
    for s in 'AB':
        done = int.from_bytes(sync[DONE[s]:DONE[s] + 4], 'little') if len(sync) == SYNC_BYTES else None
        if done != k:
            diffs.append(f'vCPU {s} did {done} of {k} iterations')
    if diffs or any(len(res[s]) != STRIDE * k for s in 'AB'):
        return collections.Counter(), diffs or ['result areas missing']
    return collections.Counter(outcomes(model.profile(name), a, k, res)), []


def verdict(cell: tuple, hist: collections.Counter, trials: int) -> list:
    """The pattern's diffs over all its runs: a forbidden outcome; fewer than `MIN_OVERLAP` of the iterations showing an
    overlap outcome; a trial count short of `trials`."""
    allowed, overlap, _ = classes(model.profile(cell[1]))
    diffs = []
    if sum(hist.values()) != trials:
        diffs.append(f'{sum(hist.values())} of {trials} iterations')
    forbidden = {o: n for o, n in hist.items() if o not in allowed}
    if forbidden:
        diffs.append(f'forbidden outcomes {dict(sorted(forbidden.items()))}')
    seen = sum(hist[o] for o in overlap)
    if seen < math.ceil(MIN_OVERLAP * trials):
        diffs.append(f'control: {seen} of {trials} iterations show an overlap outcome {sorted(overlap)}, fewer than '
                     f'{MIN_OVERLAP:.1%}: B never landed between A\'s accesses often enough to see them reordered')
    return diffs


def run_cells(run, work: pathlib.Path, k: int, runs: int, only=None) -> tuple:
    """Every cell (or those named in `only`), `runs` runs of k iterations each, through `run(text, work, extra)` (the
    driver); -> (results for results.json, the report)."""
    results, report = [], []
    for cell in CELLS:
        if only and cell[0] not in only:
            continue
        test = model.profile(cell[1])
        allowed, overlap, relaxed = classes(test)
        hist, diffs = collections.Counter(), []
        for r in range(runs):
            out = run(description(cell, k), work / f'{cell[0]}-{r}', ['--watchdog-ms', str(WATCHDOG_MS)])
            h, d = judge(cell, k, out)
            hist.update(h)
            diffs += [f'run {r}: {x}' for x in d]
        diffs += verdict(cell, hist, k * runs)
        names = ', '.join(f'{p}: {"A" if p == cell[2] else "B"}' for p in range(2))
        print(f'{cell[0]} ({cell[1]}; participant {names}): {sum(hist.values())} iterations, outputs {list(test.outputs)}, '
              f'histogram {dict(sorted(hist.items()))}; overlap {sorted(overlap)} {sum(hist[o] for o in overlap)}, '
              f'TSO-only {sorted(relaxed)} {sum(hist[o] for o in relaxed)}, '
              f'forbidden {sum(n for o, n in hist.items() if o not in allowed)}', flush=True)
        results.append({'name': f'litmus-{cell[0]}', 'outcome': 'fail' if diffs else 'match', 'diffs': diffs})
        report.append({'pattern': cell[0], 'profile': cell[1], 'a': cell[2], 'iterations': sum(hist.values()),
                       'histogram': {str(o): n for o, n in sorted(hist.items())}, 'allowed': sorted(map(str, allowed)),
                       'overlap': sorted(map(str, overlap)), 'relaxed': sorted(map(str, relaxed)), 'diffs': diffs})
    return results, report
