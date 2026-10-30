"""X2 event programs (interrupt eligibility, shadows, POPF, IRETQ, HLT, NMI): one source, two variants.

Every program runs at CPL 0 (unless stated) with its own IDT (256 interrupt gates, CS 0x08, at EV(i)) whose handlers log
each event at LOG(i) + 0x40 * k: vector, error code (-1 for none), the frame's RIP, its RFLAGS & 0x10ed5 (the arithmetic
flags, IF, DF, RF), r12 (the main line's progress marker), RCX and the vectors 0..63 whose handlers were running; then return with IRETQ (an exception resumes at r13;
an interrupt is ended with an x2APIC EOI). Event sources are marked in the text:
  SIGNAL v | SIGNAL nmi   an event becomes pending here (rcx, rax, rdx clobbered);
  EOI, APIC_ON            the x2APIC's end-of-interrupt and enable (both variants; natemu's LAPIC since X3).
QEMU's variant raises the event through its own x2APIC (a self IPI by the SELF_IPI MSR for a vector, an NMI by the ICR to
the CPU's own APIC ID) with `wrmsr`; natemu's has two `nop`s in place of each signalling `wrmsr` (the same length, so every
address is the same), and its driver posts the event at the second `nop` (`post`/`nmi` items: the host posts into the vCPU's
event record and rings its doorbell, X2). The SDM makes QEMU's event pending when the `wrmsr` completes; natemu's is
pending when the hook completes: the same boundary.

Expectations come from `tools/models/delivery_model.py` (`recognise`, `after_instruction`): `expect(prog)` lists, per
logged event, the set of permitted (vector, frame RIP label) pairs, judged on both runs.
"""
import pathlib, sys
HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE.parent.parent))
from programs import BODY0, STACK, cpu, ref_assemble  # noqa: E402

EV = lambda i: 0x300000 + 0x10000 * i       # IDT (one page), then IDTR operand at +0x1000, log cursor at +0x1010
LOG = lambda i: EV(i) + 0x2000              # the log (captured)
DATA = lambda i: EV(i) + 0x8000             # a program's data (stress)
CODE = lambda i: BODY0 + 0x1000 * i
NMI = 'nmi'

HANDLERS = """
ev_common:
    push r15
    push r14
    mov r15, 0x40
    xadd [{cur:#x}], r15
    inc qword [{count:#x}]
    cmp r15, {end:#x}
    jae ev_logged
    mov r14, [rsp+16]
    mov [r15], r14
    mov r14, [rsp+24]
    mov [r15+8], r14
    mov r14, [rsp+32]
    mov [r15+16], r14
    mov r14, [rsp+48]
    and r14, 0x10ed5
    mov [r15+24], r14
    mov [r15+32], r12
    mov [r15+40], rcx
    mov r14, [{active:#x}]
    mov [r15+48], r14
ev_logged:
    mov r14, [rsp+16]
    bts [{active:#x}], r14
    cmp r14, 2
    je ev_nmi
    cmp r14, 32
    jae ev_intr
    mov [rsp+32], r13
    jmp ev_out
ev_intr:
    push rax
    push rcx
    push rdx
    EOI
    pop rdx
    pop rcx
    pop rax
    jmp ev_out
ev_nmi:
{nmi_action}
ev_out:
    mov r14, [rsp+16]
    btr [{active:#x}], r14
    pop r14
    pop r15
    add rsp, 16
    iretq
"""
ERROR_VECTORS = (8, 10, 11, 12, 13, 14, 17)
STUB_VECTORS = (2, 6, 13, 17, 0x40, 0x41, 0x50, 0x80)


def stubs():
    return ''.join(f"ev_v{v}:\n" + ('' if v in ERROR_VECTORS else '    push -1\n') + f"    push {v}\n    jmp ev_common\n"
                   for v in STUB_VECTORS)


def expand(text, i, qemu):
    """The variant's assembly and, for natemu, the hook labels: [(label, vector or NMI)]."""
    out, hooks = [], []
    for line in text.splitlines():
        words = line.split()
        if words[:1] == ['SIGNAL']:
            e = words[1]
            if e == NMI:
                out += ['    mov ecx, 0x830', '    mov eax, 0x400', f'    mov edx, {i}']
            else:
                out += ['    mov ecx, 0x83f', f'    mov eax, {e}', '    xor edx, edx']
            label = f'hook_{len(hooks)}'
            out += ['    wrmsr'] if qemu else ['    nop', f'{label}:', '    nop']
            hooks.append((label, NMI if e == NMI else int(e, 0)))
        elif words[:1] == ['EOI']:
            out += ['    mov ecx, 0x80b', '    xor eax, eax', '    xor edx, edx', '    wrmsr']
        elif words[:1] == ['APIC_ON']:
            base = 0xfee00d00 if i == 0 else 0xfee00c00
            out += ['    mov ecx, 0x1b', f'    mov eax, {base:#x}', '    xor edx, edx', '    wrmsr']
            out += ['    mov ecx, 0x80f', '    mov eax, 0x1ff', '    xor edx, edx', '    wrmsr']
        else:
            out.append(line)
    return '\n'.join(out), hooks


def source(body, i, nmi_action='', cpl3='', log_size=0x600, log_at=None):
    """The whole text of CPU i: IDT load, APIC on, `body`, then the handlers. Each event reserves its log slot (at
    `log_at(i)` or LOG(i)) with one XADD, so an NMI inside a handler never shares a slot; events beyond `log_size` bytes
    are counted, not logged."""
    log = (log_at or LOG)(i)
    f = dict(idtr=EV(i) + 0x1000, cur=EV(i) + 0x1010, count=EV(i) + 0x1020, log=log, end=log + log_size, active=EV(i) + 0x1040)
    head = f"    lidt [{f['idtr']:#x}]\n    cli\nAPIC_ON\n    lea r13, [rel ev_end]\n"
    nmi_action = nmi_action.replace('{flag}', f'{flag(i):#x}')
    return (head + body + '\n    jmp ev_end\n' + cpl3 + HANDLERS.format(**f, nmi_action=nmi_action) + stubs() + 'ev_end:\n'
            + '    cmp r11, r11\n')


def build(name, body, n=1, nmi_action='', cpl3='', expect=None, regs=None, log_size=0x600, memory_extra=lambda i: [], log_at=None):
    """{'qemu': natemu-smp-v1 program, 'natemu': (description text extras per CPU), 'labels': per CPU, 'expect': ...}."""
    progs = {}
    for qemu in (True, False):
        bodies, cpus, memory, extra, labels = [], [], [], [], []
        for i in range(n):
            text, hooks = expand(source(body.replace('{data}', f'{DATA(i):#x}'), i, nmi_action, cpl3, log_size, log_at), i, qemu)
            log = (log_at or LOG)(i)
            code, lab = ref_assemble(text)
            gates = b''
            for v in range(256):
                off = CODE(i) + lab[f'ev_v{v}'] if f'ev_v{v}' in lab else 0
                lo = off & 0xffff | 0x08 << 16 | (0x8e if off else 0) << 40 | (off >> 16 & 0xffff) << 48
                gates += lo.to_bytes(8, 'little') + (off >> 32).to_bytes(8, 'little')
            idtr = (256 * 16 - 1).to_bytes(2, 'little') + EV(i).to_bytes(8, 'little')
            bodies.append({'addr': f'0x{CODE(i):x}', 'code': code.hex()})
            cpus.append(dict(cpu(i, CODE(i), **(regs or {})), cpl=0))
            memory += [{'addr': f'0x{EV(i):x}', 'data': gates.hex(), 'capture': False},
                       # IDTR operand, log cursor, NMI flag, event count (+0x20).
                       {'addr': f'0x{EV(i) + 0x1000:x}', 'data': (idtr + bytes(6) + log.to_bytes(8, 'little') + bytes(0x50)).hex(), 'capture': False},
                       {'addr': f'0x{log:x}', 'size': log_size, 'fill': 'zero'},
                       # The stack's page (natemu's identity space maps only what the description names).
                       {'addr': f'0x{STACK(i) - 0x800:x}', 'size': 0x800, 'fill': 'zero', 'capture': False}]
            memory += memory_extra(i)
            extra.append([] if qemu else [(CODE(i) + lab[label], e) for label, e in hooks])
            labels.append({k: CODE(i) + v for k, v in lab.items()})
        progs['qemu' if qemu else 'natemu'] = {'format': 'natemu-smp-v1', 'name': f'{name}@{n}', 'ncpu': n, 'system': True,
                                               'bodies': bodies, 'cpus': cpus, 'memory': memory, 'params': {}}
        progs['hooks' if not qemu else 'qemu_hooks'] = extra
        progs['labels' if not qemu else 'qemu_labels'] = labels
    assert all(q.items() <= nat.items() for q, nat in zip(progs['qemu_labels'], progs['labels'])), 'the variants must have the same addresses'
    progs['expect'] = expect
    return progs


def description(prog, hooks, rflags=0x3000):
    """natemu's driver description of a variant: identity space, one `cpu` section per CPU with its event hooks. RFLAGS
    starts with IOPL = 3 and IF = 0, as on QEMU's stub."""
    from run_slice3_vm import smp_description
    lines = smp_description(prog).splitlines()
    out, k = [], -1
    for line in lines:
        if line.startswith('rflags'):
            line = f'rflags {rflags:#x}'
        out.append(line)
        if line == 'cpu':
            k += 1
        if line.startswith('rip'):
            out.append('if 0')     # IF = 0 at the start, as on QEMU's stub
            out += [f'post {a:#x} {e:#x}' if e != NMI else f'nmi {a:#x}' for a, e in hooks[k]]
    return '\n'.join(out) + '\n'


def log(memory_bytes):
    """Log entries of one CPU: [(vector, error, rip, rflags, r12, rcx, active)] until the first all-zero entry (`active`:
    the vectors 0..63 whose handlers were running when it was delivered)."""
    out = []
    for k in range(0, len(memory_bytes), 0x40):
        e = [int.from_bytes(memory_bytes[k + 8 * j:k + 8 * j + 8], 'little') for j in range(7)]
        if not any(e): break
        out.append(tuple(e))
    return out


# ---- the programs ------------------------------------------------------------------------------------------------
# `events`: per CPU, the logged events in order, each (vector, pending kind, start state, trace): the trace lists the
# instructions after the event became pending ('sti', 'cli', 'mov_ss', 'hlt', 'other'; 'set_if' for POPF or IRETQ
# loading IF = 1), each with the label of the boundary after it (None: not a label). `permitted` runs the model over it.
RF, IF = 1 << 16, 1 << 9


def permitted(kind, start, trace):
    """Labels of the boundaries where delivery_model.recognise lets the pending event (`kind`) be taken, up to the first
    where it must be; asserts that one exists."""
    from models import delivery_model as dm
    s = {'rflags': 2, 'shadow': None, 'nmi_blocked': False, 'halted': False, **start}
    out = set()
    for instruction, label in trace:
        if instruction == 'set_if':
            s = dict(dm.after_instruction(s, 'other'), rflags=s['rflags'] | IF)
        else:
            s = dm.after_instruction(s, instruction)
        r = dm.recognise(s, {kind})
        if kind in r:
            assert label, f'the event may be taken at an unlabelled boundary after {instruction}'
            out.add(label)
            if None not in r:
                return out
    raise AssertionError('the model never forces the event')


ROUNDS = """    xor r9d, r9d
again:
    cli
    mov r12, r9
SIGNAL 0x40
{xfer}
target:
    mov r12, 0x77
    inc r9
    cmp r9, 3
    jb again
    cli"""

PROGRAMS = {
    # An interrupt pending before STI is taken after the next instruction (the STI shadow), not after STI.
    'ev-sti-shadow': dict(body="""    mov r12, 1
SIGNAL 0x40
    mov r12, 2
    sti
    mov r12, 3
after_shadow:
    mov r12, 4
    cli""", events=[(0x40, 'intr', {}, [('other', None), ('sti', None), ('other', 'after_shadow')])]),
    # STI then MOV SS: the MOV SS in the STI shadow sets its own shadow, or not (the model's `mov_ss?`).
    'ev-mov-ss': dict(body="""SIGNAL 0x41
    mov eax, 0x10
    sti
    mov ss, ax
after_mov_ss:
    mov r12, 2
after_next:
    mov r12, 3
    cli""", events=[(0x41, 'intr', {}, [('other', None), ('sti', None), ('mov_ss', 'after_mov_ss'), ('other', 'after_next')])]),
    # MOV SS then STI (IF 0 -> 1) in its shadow: STI's own shadow, or not (`sti?`).
    'ev-mov-ss-sti': dict(body="""SIGNAL 0x40
    mov eax, 0x10
    mov ss, ax
    sti
after_sti:
    mov r12, 2
after_next:
    mov r12, 3
    cli""", events=[(0x40, 'intr', {}, [('other', None), ('mov_ss', None), ('sti', 'after_sti'), ('other', 'after_next')])]),
    # POPF setting IF: no shadow, taken right after it; POPF clearing IF masks the next until a POPF sets it again.
    'ev-popf': dict(body="""    mov r12, 1
SIGNAL 0x40
    push 0x202
    popfq
after_popf:
    mov r12, 2
    push 0x2
    popfq
SIGNAL 0x41
    mov r12, 3
    pushfq
    or qword [rsp], 0x200
    popfq
after_popf2:
    mov r12, 4
    cli""", events=[(0x40, 'intr', {}, [('other', None), ('set_if', 'after_popf')]),
                     (0x41, 'intr', {}, [('other', None), ('other', None), ('other', None), ('set_if', 'after_popf2')])]),
    # IRETQ restoring IF = 1: the pending interrupt is taken at the return target.
    'ev-iretq': dict(body="""    mov r12, 1
SIGNAL 0x50
    mov rax, rsp
    push 0x10
    push rax
    push 0x202
    push 0x08
    lea rax, [rel iret_target]
    push rax
    iretq
iret_target:
    mov r12, 2
    cli""", events=[(0x50, 'intr', {}, [('other', None)] * 7 + [('set_if', 'iret_target')])]),
    # STI; HLT with an interrupt pending: the shadow covers HLT, the interrupt wakes it at once, RIP after the HLT.
    'ev-hlt-wake': dict(body="""    mov r12, 1
SIGNAL 0x40
    sti
    hlt
after_hlt:
    mov r12, 2
    cli""", events=[(0x40, 'intr', {}, [('sti', None), ('hlt', 'after_hlt')])]),
    # An NMI is taken whatever IF; a second NMI raised in its handler (NMIs blocked) waits for its IRETQ and is taken at the
    # IRETQ's target; a maskable interrupt raised there too (IF = 0 in the handler, 1 again after the IRETQ) is taken
    # after the second NMI's handler returns (Table 7-2: NMI first).
    'ev-nmi': dict(body="""    sti
    mov r12, 1
SIGNAL nmi
after_nmi:
    mov r12, 2
    cli""", nmi_action="""    cmp byte [{flag}], 0
    jne ev_out
    mov byte [{flag}], 1
    push rax
    push rcx
    push rdx
SIGNAL nmi
SIGNAL 0x40
    pop rdx
    pop rcx
    pop rax
    mov r12, 0x99""", events=[(2, 'nmi', {'rflags': 2 | IF}, [('other', 'after_nmi')]),
                              (2, 'nmi', {'rflags': 2 | IF}, [('set_if', 'after_nmi')]),
                              (0x40, 'intr', {'rflags': 2 | IF}, [('set_if', 'after_nmi')])]),
    # The shadowed instruction is a computed exit (RET, JMP reg), three rounds: from the second on its target block is
    # cached and linked through L1, so the step after the shadow must still return to the resident at the target.
    'ev-sti-ret': dict(body=ROUNDS.format(xfer="    lea rax, [rel target]\n    push rax\n    sti\n    ret"),
                       events=[(0x40, 'intr', {}, [('other', None), ('other', None), ('sti', None), ('other', 'target')])] * 3),
    'ev-sti-jmp': dict(body=ROUNDS.format(xfer="    lea rax, [rel target]\n    sti\n    jmp rax"),
                       events=[(0x40, 'intr', {}, [('other', None), ('sti', None), ('other', 'target')])] * 3),
    'ev-movss-ret': dict(body=ROUNDS.format(xfer="    lea rax, [rel target]\n    push rax\n    mov dx, ss\n    sti\n    mov ss, dx\nafter_movss:\n    ret"),
                         events=[(0x40, 'intr', {}, [('other', None), ('other', None), ('other', None), ('sti', None), ('mov_ss', 'after_movss'),
                                                     ('other', 'target')])] * 3),
}


def flag(i): return EV(i) + 0x1018


def make(name, n):
    """The program `name` on n CPUs (each CPU runs the same text with its own IDT, log and NMI flag)."""
    p = PROGRAMS[name]
    return build(name, p['body'], n, p.get('nmi_action', ''), expect=p['events'])


def judge(prog, logs):
    """Problems of the per-CPU logs (each [(vector, error, rip, rflags, r12, rcx)]) against the model's permitted
    boundaries (RF = 0 at each: no REP string here)."""
    problems = []
    for i, entries in enumerate(logs):
        labels = {a: k for k, a in prog['labels'][i].items()}
        want = prog['expect']
        if len(entries) != len(want):
            problems.append(f'cpu{i}: {len(entries)} events logged, {len(want)} expected: {entries}')
            continue
        for k, ((vector, kind, start, trace), e) in enumerate(zip(want, entries)):
            ok = permitted(kind, start, trace)
            got = labels.get(e[2], hex(e[2]))
            if e[0] != vector or got not in ok or e[3] & RF:
                problems.append(f'cpu{i} event {k}: vector {e[0]:#x} at {got} RF {e[3] >> 16 & 1}; model: vector {vector:#x} at {sorted(ok)}, RF 0')
    return problems


# ---- stress: events at random times (natemu: the driver's kicker posts one before each doorbell) -------------------
# The main loop mixes REP strings (interrupt windows between iterations), STI after CLI and MOV SS (shadows) and plain
# code; its results do not depend on the events (handlers keep every register but r14/r15, which the loop does not use,
# and log into a ring of LOG(i)). `stress_check` judges every logged delivery: at an instruction boundary of the loop,
# never at the boundary a shadow covers, IF = 1 in the frame of a maskable interrupt, RF = 1 exactly between REP
# iterations (RCX between the start count and 0), never inside a handler (an NMI blocked until IRETQ).
STRESS_BODY = """    mov r8, {iterations}
    xor r9d, r9d
    xor r10d, r10d
    sti
    nop
after_shadow_0:
stress_loop:
    mov rax, r8
    mov rdi, {data}
    mov ecx, 2000
rep_stosb:
    rep stosb
    mov rsi, {data}
    mov rdi, {data} + 0x1000
    mov ecx, 250
rep_movsq:
    rep movsq
    cli
    add r9, r8
    sti
sti_shadow:
    add r10, r9
after_shadow_1:
    mov ax, ss
    mov ss, ax
mov_ss_shadow:
    imul r9, r9, 3
after_shadow_2:
    xor r10, qword [{data} + 0x1008]
    dec r8
    jnz stress_loop
    cli"""
STRESS_HLT = """    mov r8, {iterations}
    sti
hlt_loop:
    hlt
after_hlt:
    dec r8
    jnz hlt_loop
    cli"""
REPS = {'rep_stosb': 2000, 'rep_movsq': 250}
SHADOWED = ('sti_shadow', 'mov_ss_shadow')
STRESS_LOG = 0x20000                         # 2,048 entries: every delivery of 2,000 events is logged
STRESS_LOG_AT = lambda i: 0x340000 + STRESS_LOG * i


def stress(name, n, iterations):
    body = (STRESS_BODY if name == 'stress' else STRESS_HLT).replace('{iterations}', str(iterations))
    data = lambda i: [{'addr': f'0x{DATA(i):x}', 'size': 0x2000, 'fill': 'lcg', 'seed': 7 + i}]
    return build(f'ev-{name}', body, n, log_size=STRESS_LOG, memory_extra=data, log_at=STRESS_LOG_AT)


def boundaries(prog, i):
    """The loop's instruction starts of CPU i (from its bytes, decoded by nasm's listing of the source)."""
    labels = prog['labels'][i]
    end = CODE(i) + len(bytes.fromhex(prog['natemu']['bodies'][i]['code']))      # the program's end: the run stops there
    return {a for a in all_boundaries(prog, i) if a < labels['ev_common']} | {labels['ev_end'], end}


def all_boundaries(prog, i):
    """Every instruction start of CPU i's text."""
    import subprocess, tempfile
    code = bytes.fromhex(prog['natemu']['bodies'][i]['code'])
    with tempfile.NamedTemporaryFile(suffix='.bin') as f:
        f.write(code); f.flush()
        lines = subprocess.run(['ndisasm', '-b', '64', '-o', str(CODE(i)), f.name], capture_output=True, text=True, check=True).stdout
    return {int(line.split()[0], 16) for line in lines.splitlines()}


def stress_check(prog, i, entries, count, coverage, shadow_steps=0):
    """Problems of CPU i's logged deliveries (all `count` of them: the log holds STRESS_LOG / 0x40); adds to `coverage`
    (Counter) where they landed."""
    labels, problems = prog['labels'][i], []
    ok = boundaries(prog, i)
    shadowed = {labels[k] for k in SHADOWED if k in labels}
    reps = {labels[k]: c for k, c in REPS.items() if k in labels}
    handler = all_boundaries(prog, i)
    for vector, error, rip, rflags, r12, rcx, active in entries:
        where = next((k for k, a in labels.items() if a == rip), hex(rip))
        rf = rflags >> 16 & 1
        # A maskable interrupt only in the loop (handlers run with IF = 0); an NMI also inside a handler, but never inside
        # the NMI handler (blocked until its IRETQ).
        if rip not in ok and not (vector == 2 and rip in handler):
            problems.append(f'cpu{i}: vector {vector:#x} delivered at {where}, not a boundary where it may be')
        if vector == 2 and active & 4: problems.append(f'cpu{i}: NMI delivered inside the NMI handler at {where}')
        if rip in shadowed: problems.append(f'cpu{i}: vector {vector:#x} delivered in a shadow ({where})')
        if vector >= 32 and not rflags & IF: problems.append(f'cpu{i}: vector {vector:#x} delivered with IF = 0 at {where}')
        mid = rip in reps and 0 < rcx < reps[rip]
        coverage['nmi' if vector == 2 else 'interrupt'] += 1
        coverage['mid_rep' if mid else 'rep_start' if rip in reps else 'after_hlt' if rip == labels.get('after_hlt') else
                 'in_handler' if rip >= labels['ev_common'] and rip != labels['ev_end'] else 'other'] += 1
        if rf != int(mid): problems.append(f'cpu{i}: vector {vector:#x} at {where} RCX {rcx}: RF {rf}, the rule {int(mid)}')
        if vector not in (2, 0x40, 0x41, 0x50): problems.append(f'cpu{i}: vector {vector:#x} (error {error:#x}) at {where}')
    if count != len(entries): problems.append(f'cpu{i}: {count} deliveries, {len(entries)} logged (log of {STRESS_LOG // 0x40})')
    # Lateness: an event a shadow held is taken at the boundary right after the one instruction stepped, so each shadow step
    # puts at least one delivery at a boundary after a shadowed instruction (`after_shadow_k`).
    post = {a for k, a in labels.items() if k.startswith('after_shadow_')}
    at_post = sum(1 for e in entries if e[2] in post)
    if post and at_post < shadow_steps:
        problems.append(f'cpu{i}: {shadow_steps} shadow steps, only {at_post} deliveries right after a shadowed instruction (late)')
    return problems
