#!/usr/bin/env python3
"""Resident stack bound (contract 62) and ERET-path audit (what contract 61 still covers) of the runtime binary.

    python3 tools/check_resident_stack.py [RUNTIME]

RUNTIME defaults to target/aarch64-unknown-none/release/natemu-runtime (build it first). Disassembles it with
`objdump -d -C` and fails (exit 1) on any violation of:

Stack. Every resident entry starts on a known stack. The gates (exception vectors, which take generated code's `svc`
entries too, and the IRQ gate) start on SP_EL1 at EXCEPTION_STACK_TOP: the only way back to generated code,
`resume_exception`, resets SP_EL1 to that top before its ERET (checked below), and generated code runs at EL0 (on
SP_EL0, pointing at an EL1-only page: it never uses SP). The boot path (`_start`) runs at EL1t on SP_EL0 from STACK_TOP
until its first block entry. The dispatcher (`dispatch_template`, copied into the code area and run there by generated
code at EL0) has a budget of 0 bytes: it may not touch SP at all,
nor load a literal (its miss is an `svc`). A function's frame is the sum of every SP decrement
in it (`sub sp, sp, #N`, pre-indexed `[sp, #-N]!`), whether or not one path takes them all; its depth is its frame plus
its deepest callee (`bl`, or a branch to another function's start: a tail call, counted at the full frame). Refused:
recursion, an indirect call or branch in reachable code not listed in INDIRECT, an SP write that is neither a constant
adjustment nor a declared stack switch (STACK_SWITCHES, literal source checked), a branch into the middle of another
function. The gate bound must fit the exception stack's reservation. One nested gate entry (a fault inside the
resident; the resident runs with interrupts masked, so no IRQ enters it) ends in a halt before any other work:
`returned` sees an SPSR from EL1 and halts (`resident_fault` → `unknown_abort`). That halting path is declared in NESTED from
the source (which calls each function makes before it halts), not derived from the binary; the checker only confirms
the declared calls exist and sums their depths. The gate bound plus that path must fit too.

ERET path. The only ERET is resume_exception's last instruction, restoring every GPR, Q register, FPCR, FPSR and SP_EL0
from the frame at the NativeFrame offsets (x16, the base, last) with ELR/SPSR from x1/x2 and SP_EL1 reset to its top;
only ERET_CALLERS branch to it (or to enter_allocated). Each gate saves the full native state in that layout before any
scratch use and calls `finish` with its kind; the vectors are 16 branches at 128-byte spacing, the tenth (lower EL
AArch64, IRQ: generated code runs at EL0) to the IRQ gate, the others to the capture gate (lower EL synchronous: generated
code's entries; the rest: the resident's own faults, which `returned` halts on).

Stack tops and reservations are read from the abi and host sources, so a layout change cannot pass silently."""
import bisect, pathlib, re, subprocess, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT = ROOT / 'target/aarch64-unknown-none/release/natemu-runtime'

GATES = ('vectors',)
SAVES = {'exception_capture': 1, 'irq_gate': 3}  # gate -> `finish` kind
NO_STACK = ('dispatch_template',)                 # run by generated code (EL0) with a 0-byte budget
BOOT = '_start'
# Indirect branches allowed in reachable code: (function, instruction) -> functions it reaches.
INDIRECT = {('_start', 'br x9'): ['private_start']}      # boot: to the linked address of the next label
# Stack switches: function -> {(instruction, literal source)}. `msr spsel, #1` selects SP_EL1, at its top whenever
# generated code runs.
STACK_SWITCHES = {'private_start': {('mov sp, x9', 'EXCEPTION_STACK_TOP'), ('msr spsel, #0x0', None)},
                  'resume_exception': {('mov sp, x9', 'EXCEPTION_STACK_TOP')},
                  'enter_allocated': {('msr spsel, #0x1', None)}}
R = 'natemu_runtime::execution::'
ERET_CALLERS = {'enter_allocated', R + 'enter', R + 'resume', R + 'interrupted'}
# A nested gate entry's path to its halt: the gate's chain, then the calls its last function makes before halting.
NESTED = {'exception_capture': (['finish', R + 'returned', R + 'resident_fault'], [R + 'load', R + 'halt'])}
# Declared shared bodies: a branch from the function into the target's body past its prologue, after an identical
# prologue of its own: counts the target's depth without its frame.
SHARED_BODIES = set()
CALL_LIKE = re.compile(r'^(bl|b|b\.\w+|cbn?z|tbn?z)$')


def constants(root=ROOT):
    """{STACK_TOP, EXCEPTION_STACK_TOP} addresses, and the exception and boot stack reservations, from the abi and host
    sources."""
    num = lambda s: int(s.replace('_', ''), 0)
    lib = (root / 'crates/abi/src/lib.rs').read_text()
    private = num(re.search(r'pub const PRIVATE: u64 = (0x[0-9a-f_]+);', lib)[1])
    tops = {n: private + num(re.search(rf'pub const {n}: u64 = PRIVATE \+ (0x[0-9a-f_]+);', lib)[1])
            for n in ('STACK_TOP', 'EXCEPTION_STACK_TOP')}
    abi = (root / 'crates/abi/src/execution.rs').read_text()
    host = (root / 'crates/host/src/execution.rs').read_text()
    exc = re.search(r'own\(\(EXCEPTION_STACK_TOP - PRIVATE\) as usize - (0x[0-9a-f_]+), (0x[0-9a-f_]+)\)', host)
    boot = re.search(r'own\(\(STACK_TOP - PRIVATE\) as usize - exec::STACK_BYTES, exec::STACK_BYTES\)', host)
    stack = re.search(r'pub const STACK_BYTES: usize = (0x[0-9a-f_]+);', abi)
    if not (exc and boot and stack and exc[1] == exc[2]):
        raise SystemExit('stack reservations not found in the host/abi sources: update check_resident_stack.py')
    return tops, num(exc[1]), num(stack[1])


def parse(text):
    """Functions (name -> [(address, normalized instruction)]) and literal words (address -> 32-bit value) of an
    `objdump -d -C --no-show-raw-insn` listing; names drop LLVM's ` (.llvm.N)` suffix."""
    funcs, words, current = {}, {}, None
    for line in text.splitlines():
        if m := re.match(r'^([0-9a-f]+) <(.+)>:$', line):
            # Two codegen units may each keep a copy of one function (one of them with the suffix): later copies of a
            # name are keyed `name #2`, ... so each is analysed at its own address.
            name, k = re.sub(r' \(\.llvm\.\d+\)$', '', m[2]), 1
            while funcs.get(key := name if k == 1 else f'{name} #{k}'):
                k += 1
            current = funcs.setdefault(key, [])
        elif m := re.match(r'^\s*([0-9a-f]+):\s+(?:[0-9a-f]{2} ){4}\s*\.word\s+(0x[0-9a-f]+)', line):
            words[int(m[1], 16)] = int(m[2], 16)
        elif (m := re.match(r'^\s*([0-9a-f]+):\s+(\S+)(?:\s+([^/]*?))?\s*(?://.*)?$', line)) and current is not None:
            current.append((int(m[1], 16), f'{m[2].lower()} {(m[3] or "").lower()}'.strip()))
    return funcs, words


def literal(words, insns, i):
    """The 64-bit literal the `ldr x9, ADDR` just before instruction i loads (None without one)."""
    m = re.match(r'ldr x9, 0x([0-9a-f]+) <', insns[i - 1][1]) if i else None
    return m and words.get(int(m[1], 16), 0) | words.get(int(m[1], 16) + 4, 0) << 32


def analyse(funcs, words, tops):
    """Per function: (frame bytes, callees, problems found in it)."""
    starts = sorted((insns[0][0], name) for name, insns in funcs.items() if insns)
    def owner(address):
        i = bisect.bisect_right([a for a, _ in starts], address) - 1
        return starts[i] if i >= 0 else (None, None)
    info = {}
    for name, insns in funcs.items():
        frame, callees, problems = 0, set(), []
        for i, (addr, ins) in enumerate(insns):
            op, _, rest = ins.partition(' ')
            at = f'{name}+{addr - insns[0][0]:#x}'
            if m := re.search(r'\[sp, #-(0x[0-9a-f]+)\]!', ins):
                frame += int(m[1], 16)
            elif m := re.fullmatch(r'sub sp, sp, #(0x[0-9a-f]+)', ins):
                frame += int(m[1], 16)
            elif re.fullmatch(r'add sp, (sp|x29), #0x[0-9a-f]+', ins):
                pass                                                     # a release
            elif re.match(r'(sp|wsp)\b', rest) and not op.startswith(('st', 'ld', 'cmp', 'cmn')) \
                    or op == 'msr' and rest.startswith('spsel'):
                lit = literal(words, insns, i) if op == 'mov' else None
                source = next((n for n, v in tops.items() if v == lit), None)
                if (ins, source) not in STACK_SWITCHES.get(name, ()):
                    problems.append(f'{at}: stack write `{ins}` is not a declared switch')
            if op in ('blr', 'br'):
                if (name, ins) in INDIRECT:
                    callees.update(INDIRECT[name, ins])
                else:
                    problems.append(f'{at}: unaccounted indirect branch `{ins}`')
            elif CALL_LIKE.match(op) and (m := re.search(r'0x([0-9a-f]+) <', rest)):
                start, target = owner(int(m[1], 16))
                if target != name:
                    if start == int(m[1], 16):
                        callees.add(target)
                    elif (name, target) in SHARED_BODIES:
                        callees.add((target, 'body'))
                    else:
                        problems.append(f'{at}: branch into the middle of {target}')
        info[name] = (frame, callees, problems)
    return info


def depth(info, entry):
    """(deepest stack bytes from `entry`, the path as [(function, frame)], problems: recursion, unknown callees and
    every reachable function's own problems)."""
    memo, problems, active = {}, [], []
    def walk(f):
        if isinstance(f, tuple):                                    # a shared body: the target without its frame
            d, path = walk(f[0])
            return d - info[f[0]][0], path[1:]
        if f in active:
            problems.append('recursion: ' + ' -> '.join(map(short, active[active.index(f):] + [f])))
            return 0, []
        if f not in info:
            problems.append(f'unknown function {f}')
            return 0, []
        if f not in memo:
            frame, callees, own = info[f]
            problems.extend(own)
            active.append(f)
            best = max((walk(c) for c in sorted(callees, key=str)), default=(0, []), key=lambda r: r[0])
            active.pop()
            memo[f] = frame + best[0], [(f, frame)] + best[1]
        return memo[f]
    d, path = walk(entry)
    return d, path, sorted(set(problems))


def short(name):
    """The last two path segments of a demangled name, generics dropped."""
    s = name
    while (t := re.sub(r'<[^<>]*>', '', s)) != s:
        s = t
    return '::'.join([x for x in s.split('::') if x][-2:]) or name


def render(path):
    return ' -> '.join(f'{short(f)} {frame}' for f, frame in path)


def nested(info, gate):
    """(bytes, problems) of a nested entry through `gate` to its halt (NESTED)."""
    chain, before_halt = NESTED[gate]
    problems, total, prev = [], info[gate][0] if gate in info else 0, gate
    for f in chain:
        if f not in info or f not in info[prev][1]:
            return 0, [f'nested path: {short(prev)} does not call {short(f)}']
        total, prev = total + info[f][0], f
    worst = 0
    for f in before_halt:
        if f not in info[prev][1]:
            return 0, [f'nested path: {short(prev)} does not call {short(f)}']
        d, _, p = depth(info, f)
        worst, problems = max(worst, d), problems + p
    return total + worst, problems


def eret_problems(funcs, words, tops):
    """The ERET path, the gates' save sequences and the vectors, instruction by instruction."""
    problems = []
    erets = [(n, a) for n, insns in funcs.items() for a, i in insns if i == 'eret']
    body = funcs.get('resume_exception', [])
    if len(erets) != 1 or not body or erets[0] != ('resume_exception', body[-1][0]):
        problems.append(f'ERET sites {[(short(n), hex(a)) for n, a in erets]}: want only resume_exception\'s last')
    o = lambda n: f'#{n:#x}'
    want = (['msr elr_el1, x1', 'msr spsr_el1, x2', 'mov x16, x0', 'ldr x9, [x16, #0xf8]', 'msr sp_el0, x9',
             'ldr x9, LIT', 'mov sp, x9', 'ldr x9, [x16, #0x308]', 'msr fpcr, x9', 'ldr x9, [x16, #0x310]',
             'msr fpsr, x9'] + [f'ldp q{2 * i}, q{2 * i + 1}, [x16, {o(256 + 32 * i)}]' for i in range(16)] +
            [f'ldr x{r}, [x16{", " + o(8 * r) if r else ""}]' for r in range(31) if r != 16] +
            ['ldr x16, [x16, #0x80]', 'eret'])
    got = [re.sub(r'^ldr x9, 0x[0-9a-f]+ <.*', 'ldr x9, LIT', i) for _, i in body]
    if got != want:
        diff = next((f'#{k}: `{g}`, want `{w}`' for k, (g, w) in enumerate(zip(got, want)) if g != w),
                    f'{len(got)} instructions, want {len(want)}')
        problems.append(f'resume_exception restore sequence differs: {diff}')
    elif literal(words, body, 6) != tops['EXCEPTION_STACK_TOP']:
        problems.append('resume_exception does not reset SP_EL1 to EXCEPTION_STACK_TOP')
    callers = {n for n, insns in funcs.items() for _, i in insns
               if re.match(r'bl? 0x[0-9a-f]+ <(resume_exception|enter_allocated)>$', i)} - {'resume_exception'}
    if callers != ERET_CALLERS:
        problems.append(f'branches to the ERET path from {sorted(map(short, callers))}, '
                        f'want {sorted(map(short, ERET_CALLERS))}')
    saves = ([f'stp x{2 * i}, x{2 * i + 1}, [sp{", " + o(16 * i) if i else ""}]' for i in range(15)] +
             ['str x30, [sp, #0xf0]'] + [f'stp q{2 * i}, q{2 * i + 1}, [sp, {o(256 + 32 * i)}]' for i in range(16)] +
             ['mrs x9, fpcr', 'str x9, [sp, #0x308]', 'mrs x9, fpsr', 'str x9, [sp, #0x310]', 'str xzr, [sp, #0x318]'])
    for gate, kind in SAVES.items():
        # Both gates are exception entries (on SP_EL1): NZCV from SPSR.
        flags = ['mrs x9, spsr_el1', 'and x9, x9, #0xf0000000']
        want = (['sub sp, sp, #0x320'] + saves + ['mrs x9, sp_el0', 'str x9, [sp, #0xf8]'] + flags +
                ['str x9, [sp, #0x300]', 'mov x0, sp', f'mov x1, #{kind:#x}', 'bl FINISH', 'b SELF'])
        got = [re.sub(r'^bl 0x[0-9a-f]+ <finish>$', 'bl FINISH',
                      re.sub(rf'^b 0x[0-9a-f]+ <{gate}\+0x[0-9a-f]+>$', 'b SELF', i)) for _, i in funcs.get(gate, [])]
        if got[:len(want)] != want or any(i != 'nop' for i in got[len(want):]):
            problems.append(f'{gate} save sequence differs from the declared gate shape')
    vec = funcs.get('vectors', [])
    branches = [(a - vec[0][0], i) for a, i in vec if i != 'nop'] if vec else []
    target = lambda k: 'irq_gate' if k == 9 else 'exception_capture'
    if [a for a, _ in branches] != [128 * k for k in range(16)] or \
            any(not re.fullmatch(rf'b 0x[0-9a-f]+ <{target(k)}>', i) for k, (_, i) in enumerate(branches)):
        problems.append('vectors are not 16 branches at 128-byte spacing, the tenth (lower EL IRQ) to irq_gate, the others to exception_capture')
    return problems


def check(text, root=ROOT):
    """(report lines, problems) for an `objdump -d -C --no-show-raw-insn` listing of the runtime."""
    tops, reserve, boot_reserve = constants(root)
    funcs, words = parse(text)
    info = analyse(funcs, words, tops)
    lines, problems, worst = [], [], 0
    for gate in GATES:
        d, path, p = depth(info, gate)
        problems, worst = problems + p, max(worst, d)
        lines.append(f'{gate}: {d} bytes: {render(path)}')
    nest = 0
    for gate in NESTED:
        d, p = nested(info, gate)
        problems, nest = problems + p, max(nest, d)
    lines.append(f'gate bound {worst} of {reserve} bytes (SP_EL1); with one nested entry to its halt {worst + nest}')
    if worst + nest > reserve:
        problems.append(f'exception stack: {worst} + nested {nest} exceeds the reserve {reserve}')
    d, path, p = depth(info, BOOT)
    problems += p
    lines.append(f'boot: {d} of {boot_reserve} bytes (SP_EL0): {render(path)}')
    if d > boot_reserve:
        problems.append(f'boot stack: {d} exceeds the reserve {boot_reserve}')
    for entry in NO_STACK:
        uses = [i for _, i in funcs.get(entry, []) if re.search(r'\bsp\b', i) or re.match(r'ldr\w* \w+, 0x', i)]
        lines.append(f'{entry}: {len(uses)} SP uses or literal loads, budget 0 (SP_EL0)')
        if uses or entry not in funcs:
            problems.append(f'{entry}: uses the stack or a literal ({uses}) or is missing; its budget is 0')
    problems += eret_problems(funcs, words, tops)
    return lines, list(dict.fromkeys(problems))


def main():
    runtime = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT
    text = subprocess.run(['objdump', '-d', '-C', '--no-show-raw-insn', str(runtime)], check=True,
                          capture_output=True, text=True).stdout
    lines, problems = check(text)
    print('\n'.join(lines + [f'FAIL {p}' for p in problems] + ['ok' if not problems else f'{len(problems)} problem(s)']))
    return 1 if problems else 0


if __name__ == '__main__':
    sys.exit(main())
