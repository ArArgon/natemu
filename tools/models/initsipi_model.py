"""Reference model of natemu's INIT and start-up IPI (SIPI) handling (boot-interpreter plan section 6), explored
exhaustively over the interleavings of senders and targets and checked against an architectural reference.

Sources: Intel SDM Vol. 3A (253668-093US) and Vol. 3C (326019-093US), September 2026; AMD APM Vol. 2 (24593, rev. 3.38);
Linux master `arch/x86/kvm/lapic.c` (`__apic_accept_irq`, `kvm_apic_accept_events`) and `x86.c` (`kvm_vcpu_reset`,
`vcpu_block`, `vcpu_run`, `kvm_vcpu_has_events`); QEMU master `target/i386/helper.c` (`do_cpu_init`, `do_cpu_sipi`),
`hw/intc/apic.c` (`apic_deliver`, `apic_startup`, `apic_sipi`, `apic_irq_pending`), `hw/intc/apic_common.c`
(`apic_init_reset`), `target/i386/cpu.c` (`x86_cpu_pending_interrupt`) and `tcg/system/seg_helper.c`
(`x86_cpu_exec_interrupt`); the senders: Linux v6.12 `arch/x86/kernel/smpboot.c` (`send_init_sequence`,
`wakeup_secondary_cpu_via_init`), kvm-unit-tests `lib/x86/smp.c:290-293`, EDK2 `MpInitLib`.

WHAT THE ARCHITECTURE SAYS
A1 Wait-for-SIPI blocks external interrupts, NMIs, INIT signals and SMIs (3C 29.7.2 "Activity State"; 28.2 "Other Causes
   of VM Exits": "If a logical processor is in the wait-for-SIPI state, NMIs are blocked. The NMI is not delivered and no
   VM exit occurs", and likewise for INIT). Whether a blocked event is held or dropped Intel does not say (its only
   "pending" text is the old P6/Pentium 4 note that an SMI is held and serviced before the SIPI, 3C 34.2). AMD says it:
   in the INIT state "the target APIC is responsive only to the STARTUP IPI. All other interrupts (including SMI and
   NMI) are held pending until the STARTUP IPI has been accepted" (APM 16.5, ICR message type 101b). KVM holds an NMI
   queued while the vCPU is KVM_MP_STATE_INIT_RECEIVED (it enters the guest only when RUNNABLE) and injects it after the
   SIPI; QEMU's TCG does not block it (`x86_cpu_pending_interrupt` has no wait-for-SIPI test: the NMI wakes the AP and is
   delivered through the real-mode IVT). The plan's rule, marked Inferred, is confirmed for "blocked" and made precise:
   held pending, the first event after the SIPI (in interpreted code natemu's typed stop).
A2 External interrupts: INIT leaves the local APIC as after power-up except the APIC ID and arbitration ID (3A 13.4.7.3),
   SVR FFH: software disabled (13.4.7.1). A software-disabled APIC "will respond normally to INIT, NMI, SMI, and SIPI
   messages" and holds what its IRR and ISR already have (13.4.7.2); of new fixed interrupts Intel says nothing. AMD: a
   disabled APIC does not accept fixed, lowest-priority or ExtINT interrupts (APM 16.3.1, 16.4.7); KVM drops them
   (`if (unlikely(!apic_enabled(apic))) break;`); QEMU sets the IRR bit and delivers it once enabled. natemu's `send`
   already refuses them at a software-disabled target. So in wait-for-SIPI no fixed vector is pending: INIT cleared the
   IRR and nothing enters it until software enables the APIC (the plan says fixed vectors stay pending: corrected).
A3 SIPI: "If a logical processor is not in the wait-for-SIPI activity state when a SIPI arrives, no VM exit occurs and
   the SIPI is discarded" (3C 28.2: the only Intel statement, in VMX text; nothing outside VMX was found, nor in AMD's
   manual). KVM drops a SIPI unless the vCPU is INIT_RECEIVED; QEMU unless `wait_for_sipi`. SIPIs are not retried (3A
   13.6.1, delivery mode 110). The start: CS = vector << 8, base vector << 12, IP 0, the hidden limit and attributes of
   the INIT state kept (KVM `vcpu_deliver_sipi_vector`, QEMU `cpu_x86_load_seg_cache_sipi`): `segment_model.reset_state`.
A4 The INIT state (3A 12.1.1, Table 12-1, INIT column): EFLAGS 2, EIP FFF0H, CR0 60000010H with CD and NW unchanged
   (note 2), CR2-CR4 0, CS F000H/FFFF0000H/FFFFH, other segments 0/0/FFFFH, GDTR and IDTR 0/FFFFH, EDX the signature,
   EAX and the other GPRs 0, DR0-3 0, DR6 FFFF0FF0H, DR7 400H, IA32_EFER and the FS/GS bases 0. Unchanged: x87, SSE and
   AVX state, MXCSR, XCR0, MTRRs, caches, TSC, SYSENTER, STAR/LSTAR, performance counters, the APIC base and x2APIC
   mode (13.12.5.1). TLBs: invalidated by 12.1's prose, "Unchanged" in the table; KVM flushes; natemu drops its shadows
   and its L1 context. Outside VMX an INIT clears pending events (3C 28.2: VM exits on INIT "do not modify register
   state or clear pending events as they would outside of VMX operation"): KVM drops a queued NMI and SMI
   (`kvm_vcpu_reset`), QEMU every request but a pending SIPI (`do_cpu_init`).
A5 INIT restarts the BSP at the reset vector; an AP enters wait-for-SIPI (3A 11.4.2: after an INIT "each logical
   processor examines its BSP flag ... to determine whether it should execute the BIOS boot-strap code (if it is the BSP)
   or enter a wait-for-SIPI state (if it is an AP)"; KVM makes the BSP RUNNABLE, QEMU sets `wait_for_sipi = !bsp`).
A6 INIT is not masked by RFLAGS.IF (IF masks maskable interrupts only, 3A 7.8.1; INIT is a class-3 external hardware
   intervention, Table 7-2, outside the interrupt mechanism, 7.3.1; no text says "IF does not mask INIT" in so many
   words) and it ends HLT (Vol. 2 HLT: an enabled interrupt, NMI, SMI, a debug exception, BINIT#, INIT# or RESET# resume
   execution). A SIPI does not end HLT: it is discarded (A3). INIT outranks NMI at a boundary (Table 7-2), so an NMI
   pending at the boundary where INIT is taken is discarded with the other pending events.
A7 INIT level de-assert (mode 101, level 0, trigger level) only synchronises arbitration IDs and is not supported from the
   Pentium 4 on (3A 13.6.1): no effect (natemu's `send` ignores it; KVM ignores it; QEMU sets the arbitration IDs).
A8 A second SIPI while the CPU runs its trampoline (Linux sends two): discarded by A3. What an AP does with a second SIPI
   outside VMX neither manual states.
AMD differs: it states A1's holding and A2's refusal; it has no wait-for-SIPI wording, no BSP rule, nothing on a STARTUP
sent to a processor not waiting; INIT is held while GIF = 0 under SVM (APM 15.13.4, Table 15-12) where Intel blocks it in
VMX root operation (3C 26.8); its INIT keeps CR8, and its table gives the APIC "Disabled" after RESET, "Enabled" after
INIT (APM Table 14-1), Intel "Enabled" for both.

MODEL CHOICES where the sources leave room
W1 INIT in wait-for-SIPI: blocked (Intel); KVM re-INITs (a latched SIPI and a queued NMI dropped), QEMU re-INITs keeping a
   pending SIPI. Model: KVM's. It is the history "the SIPI started the CPU, the INIT stopped it before its first
   instruction", which the reference permits.
W2 A SIPI that arrives while an INIT is pending but not yet taken (kvm-unit-tests sends them back to back) is latched and
   honoured after the INIT (KVM: both are pending-event bits, taken INIT first; QEMU keeps the SIPI across
   `do_cpu_init`). natemu's INIT-pending window is its own lag (hardware takes INIT at the next boundary), so the
   reference lets the INIT arrive first.
W3 A SIPI that finds one latched is discarded (the first started the CPU). KVM keeps the last vector; they differ only for
   two SIPIs with different vectors reaching an AP before it runs.
W4 A SIPI sent before an INIT that is still pending is discarded (KVM: INIT overwrites the pending events; QEMU keeps it).

NATEMU'S PROTOCOL (what `explore` runs)
- The Events pending word gains I (INIT pending), W (the target is in wait-for-SIPI, set and cleared by it) and S (a
  SIPI latched, with its vector); N is the existing NMI word.
- The sender (lapic.rs `send`), per target: one atomic update, then `posts` += 1 and the doorbell, only if the word
  changed. INIT: in W, clear S and N; else set I, clear S and N. SIPI: if I or W and no S, set S and the vector, else
  nothing. NMI: set N. INIT de-assert: nothing. Fixed: read the target's published SW_ENABLED, then set the IRR bit if
  it was set (today's two steps).
- The target at a boundary (the interpreter loop at every instruction; translated code at its doorbell; after each
  resident transaction and host request): I -> take INIT; N -> deliver (translated) or typed stop (interpreted); an
  interrupt with IF = 1 and the APIC enabled -> the same. Taking INIT is one atomic update clearing I and setting W (the
  BSP: clearing I and S, restarting at the reset vector), then the INIT state: SW_ENABLED cleared, IRR cleared, IF = 0,
  `Events::halted` cleared.
- HLT, the mailbox wait and wait-for-SIPI each loop: read `posts` (seen); look (INIT; in HLT also an NMI or an enabled
  interrupt; in wait-for-SIPI the latch, taken by one atomic update clearing S and W: the start); else publish
  `halted` = seen and sleep until the doorbell.
- The host ends the run when every vCPU has ended or is idle (published `halted` equal to its `posts`), and reports each
  by where it waited: halted, wait-for-SIPI (not finished: never started or parked), mailbox.

THE REFERENCE (`explore(..., spec=True)`): IPIs queue per sender-target pair and arrive in order at any later instruction
boundary of the target. On arrival INIT puts an AP in wait-for-SIPI (the BSP at the reset vector) and drops its pending
NMI (W1 in wait-for-SIPI); a SIPI starts a waiting CPU and is dropped otherwise; an NMI is held in wait-for-SIPI and taken
at the next boundary elsewhere; a fixed interrupt is accepted by a software-enabled APIC only. Instructions are atomic.

INVARIANTS (checked at every reachable state of natemu's protocol, and when the run ends)
J1 A latched SIPI always has its INIT pending or its CPU in wait-for-SIPI (S implies I or W).
J2 W is set exactly while the CPU is in wait-for-SIPI; I is never set there; the BSP never has W.
J3 A CPU that runs never looks idle: `halted` is published only in HLT, the mailbox wait and wait-for-SIPI.
J4 When the run ends nothing could still change: no INIT pending, no SIPI latched, no HLT CPU with an event it takes.
J5 Refinement: every outcome natemu's protocol reaches is one the reference permits. An outcome is how the run ended
   (the host's end, a typed stop, or stuck: nothing can move and the host does not end it, a hang) and, per CPU, its
   runs (the boot program, each start with its vector, each BSP reset, with the number of instructions each executed;
   runs of no instruction are not observable and dropped), the NMIs and interrupts delivered, and where it ended (a
   wait-for-SIPI CPU as such, never as halted). So natemu never starts a CPU the reference would not, never misses one
   it would, never hangs where it would end, and never ends where it would not.

Programs: lists of ('op',), ('icr', kind, dest[, vector]) (kind init, deassert, sipi, nmi or fixed; dest a CPU, 'self',
'all' or 'others'), ('hlt',) (HLT, then the next instruction), ('halt',) (an idle loop: HLT, events taken, HLT again),
('host',) (an instruction answered by a host request), ('t',) (the climb's end: translated code from here), ('sti',),
('cli',), ('enable',) (SVR software enable), ('signal',) and ('await', n) (a shared counter: an AP's check-in, a BSP's
wait for n). A scenario gives each CPU its context at the start ('run', 'host', 'hlt', 'wfs', 'mbox'), whether it runs
translated code (`tr`), IF (`if_`), and its programs: `boot`, `start` (after a SIPI), `reset` (the BSP after INIT).
CPU 0 is the BSP. SCENARIOS are the senders of the plan (Linux, kvm-unit-tests, EDK2) and the corner cases; VARIANTS
are deliberate breaks of the protocol, each of which some scenario's `check` reports (the checks are shown to fail).
`outcomes(name)` is what a framework case's observed result must be one of. Run: python3 tools/models/initsipi_model.py
"""
from collections import namedtuple
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from models import cpu_profile, segment_model  # noqa: E402

# A4: what INIT leaves unchanged (Table 12-1's INIT column "Unchanged" rows and 12.1's prose).
KEPT_BY_INIT = ('x87, SSE, AVX and AVX-512 registers', 'MXCSR', 'XCR0', 'IA32_XSS', 'PKRU', 'MTRRs', 'PAT', 'caches',
                'TSC, TSC_AUX, TSC_ADJUST, TSC_DEADLINE', 'SYSENTER_CS/ESP/EIP', 'STAR, LSTAR', 'performance counters',
                'machine-check banks', 'IA32_APIC_BASE (with the BSP flag) and x2APIC mode', 'APIC ID',
                'CR0.CD and CR0.NW', 'other MSRs')


def init_state(cd_nw=0x60000000, sipi=None):
    """The architectural state INIT leaves (A4: Table 12-1, INIT column), or, with `sipi`, the state a SIPI with that
    vector starts: segment_model's register and segment state plus EDX (the CPU profile's signature), the other GPRs 0,
    DR0-DR3 0, DR6 FFFF0FF0H, DR7 400H, EFER and the FS/GS bases 0; the local APIC as after power-up but its ID (SVR FFH:
    software disabled, TPR, IRR, ISR, TMR 0, every LVT masked); TLBs flushed. `cd_nw`: CR0.CD and CR0.NW as before."""
    s = segment_model.reset_state(sipi=sipi, cd_nw=cd_nw)
    s.update(gpr={r: 0 for r in ('rax', 'rbx', 'rcx', 'rdx', 'rsi', 'rdi', 'rbp', 'rsp')}, dr=(0, 0, 0, 0),
             dr6=0xFFFF0FF0, dr7=0x400, fs_base=0, gs_base=0, efer=0,
             apic=dict(svr=0xFF, tpr=0, irr=0, isr=0, tmr=0, lvt_masked=True, ldr=0, dfr=0xFFFFFFFF))
    s['gpr']['rdx'] = int(cpu_profile.load().d['identity']['signature'], 16)
    return s


I, W, S, N = 'I', 'W', 'S', 'N'
V = 0x9A                               # Linux's trampoline vector (start_eip >> 12 at 9A000H); any vector works
LOOPS = ('hlt', 'wfs', 'mbox')
LABEL = {'hlt': 'halted', 'wfs': 'wait-for-sipi', 'mbox': 'mailbox', 'done': 'done', 'run': 'running',
         'host': 'running', 'init2': 'running'}
VARIANTS = {
    'sipi-needs-wfs': 'a SIPI is latched only in wait-for-SIPI, not while INIT is pending',
    'init-two-steps': 'taking INIT clears I, and sets W in a later step',
    'init-keeps-sipi': 'an INIT leaves a latched SIPI in place',
    'hlt-ignores-init': 'HLT wakes for NMIs and interrupts only',
    'wfs-not-idle': 'wait-for-SIPI never publishes halted',
    'wfs-reported-halted': 'the host reports a wait-for-SIPI CPU as halted',
    'nmi-wakes-wfs': "an NMI is taken in wait-for-SIPI (QEMU's TCG)",
    'fixed-held-in-wfs': 'a fixed IPI enters the IRR of a software-disabled APIC (the plan\'s wording; QEMU)',
}

Cpu = namedtuple('Cpu', 'ctx phase prog pc tr if_ word vec sw irr posts seen halted bell look send runs nmis ints')
Ref = namedtuple('Ref', 'ctx prog pc tr if_ nmi irr sw runs nmis ints')


def _targets(n, me, dest):
    if dest == 'self':
        return [me]
    if dest in ('all', 'others'):
        return [t for t in range(n) if dest == 'all' or t != me]
    return [dest]


def _progress(runs):
    label, k = runs[-1]
    return runs[:-1] + ((label, k + 1),)


def _instruction(sc, i, c):
    p = sc['cpus'][i].get(c.prog, [] if c.prog == 'boot' else [('halt',)])
    return p[c.pc] if c.pc < len(p) else None


def _ended(sc, i, c):
    """A program that has run its last instruction (and finished sending) has ended: the program driver's 'reached'."""
    if c.ctx == 'run' and _instruction(sc, i, c) is None and not getattr(c, 'send', ()):
        return c._replace(ctx='done')
    return c


def _complete(sc, i, c, **changes):
    return _ended(sc, i, c._replace(pc=c.pc + 1, runs=_progress(c.runs), **changes))


def _execute(sc, i, c, counter):
    """The CPU's next instruction other than an ICR write or a host request: (cpu, counter), or None while it waits
    (`await`). Shared by natemu's protocol and the reference."""
    ins = _instruction(sc, i, c)
    op = ins[0]
    if op in ('hlt', 'halt'):
        return c._replace(ctx='hlt', **({'phase': 'c1'} if 'phase' in c._fields else {})), counter
    if op == 'await':
        return (_complete(sc, i, c), counter) if counter >= ins[1] else None
    if op == 'signal':
        return _complete(sc, i, c), counter + 1
    change = {'t': {'tr': True}, 'sti': {'if_': True}, 'cli': {'if_': False}, 'enable': {'sw': True}, 'op': {}}[op]
    return _complete(sc, i, c, **change), counter


def _sends(sc, i, ins):
    """natemu's per-target steps of an ICR write."""
    kind = ins[1]
    if kind == 'deassert':
        return ()
    first = lambda t: ('read', t, ins[3]) if kind == 'fixed' else ('rmw', t, kind, ins[3] if kind == 'sipi' else None)
    return tuple(s for t in _targets(len(sc['cpus']), i, ins[2]) for s in (first(t), ('post', t)))


# ---- natemu's protocol -------------------------------------------------------------------------------------------

def _initial(sc):
    cpus = []
    for c in sc['cpus']:
        ctx = c.get('ctx', 'run')
        cpus.append(Cpu(ctx=ctx, phase='c1' if ctx in LOOPS else None, prog='boot', pc=0, tr=c.get('tr', False),
                        if_=c.get('if_', False), word=frozenset({W}) if ctx == 'wfs' else frozenset(), vec=None,
                        sw=c.get('sw', ctx != 'wfs'), irr=False, posts=0, seen=0, halted=None, bell=False, look=False,
                        send=(), runs=(('boot', 0),) if ctx in ('run', 'host', 'hlt') else (), nmis=0, ints=0))
    return tuple(cpus), 0


def _take_init(i, c, variant):
    reset = dict(tr=False, if_=False, sw=False, irr=False, halted=None, look=False, send=())
    if i == 0:
        return c._replace(ctx='run', phase=None, prog='reset', pc=0, word=c.word - {I, S}, vec=None,
                          runs=c.runs + (('reset', 0),), **reset)
    if 'init-two-steps' in variant:
        return c._replace(ctx='init2', phase=None, word=c.word - {I}, **reset)
    return c._replace(ctx='wfs', phase='c1', word=c.word - {I} | {W}, **reset)


def _event(i, c, variant, hlt=False):
    """What this boundary takes: a Cpu (INIT taken, or an NMI or interrupt delivered in translated code), a typed stop
    ('stop', reason), or None."""
    if I in c.word and not (hlt and 'hlt-ignores-init' in variant):
        return _take_init(i, c, variant)
    if N in c.word:
        return c._replace(word=c.word - {N}, nmis=c.nmis + 1) if c.tr else ('stop', 'nmi')
    if c.irr and c.sw and c.if_:
        return c._replace(irr=False, ints=c.ints + 1) if c.tr else ('stop', 'interrupt')
    return None


def _send_step(sc, cpus, i, variant):
    """The sender's next step of its ICR write: the new CPU tuple."""
    step, rest = cpus[i].send[0], cpus[i].send[1:]
    cpus = list(cpus)
    kind, t = step[0], step[1]
    if kind == 'post':
        tc = cpus[t]
        cpus[t] = tc._replace(posts=tc.posts + 1, bell=True)
    elif kind == 'read':
        rest = (('set', t, cpus[t].sw or 'fixed-held-in-wfs' in variant),) + rest
    else:
        tc = cpus[t]
        w, vec = tc.word, tc.vec
        if kind == 'set':
            changed = step[2]                                  # accepted: rung (natemu rings every accepting target)
            cpus[t] = tc._replace(irr=tc.irr or step[2])
        else:
            what = step[2]
            if what == 'init':
                drop = {N} if 'init-keeps-sipi' in variant else {S, N}
                w = w - drop if W in w else w - drop | {I}
            elif what == 'sipi':
                if (W in w or I in w and 'sipi-needs-wfs' not in variant) and S not in w:
                    w, vec = w | {S}, step[3]
            else:
                w = w | {N}
            if S not in w:
                vec = None
            changed = (w, vec) != (tc.word, tc.vec)
            cpus[t] = tc._replace(word=w, vec=vec)
        if not changed:
            rest = rest[1:]                                    # no post, no doorbell
    cpus[i] = _ended(sc, i, cpus[i]._replace(send=rest))
    return tuple(cpus)


def _run_step(sc, i, c, counter):
    """The next instruction of a running CPU in natemu: (cpu, counter) or None."""
    ins = _instruction(sc, i, c)
    if ins[0] == 'icr':
        return _complete(sc, i, c, send=_sends(sc, i, ins), look=c.tr), counter
    if ins[0] == 'host':
        return c._replace(ctx='host'), counter
    return _execute(sc, i, c, counter)


def _cpu_steps(sc, cpus, counter, i, variant):
    """(cpus, counter) successors of CPU i's possible steps, or ('stop', i, reason)."""
    c = cpus[i]

    def put(new, cnt=counter):
        return tuple(new if k == i else x for k, x in enumerate(cpus)), cnt

    out = []
    if c.ctx == 'run':
        if c.send:
            return [(_send_step(sc, cpus, i, variant), counter)]
        if c.look or c.bell or not c.tr:                       # a boundary that looks (interpreted: every one)
            e = _event(i, c._replace(bell=False, look=False), variant)
            if type(e) is tuple:                               # a typed stop (not a Cpu)
                return [('stop', i, e[1])]
            if e is not None:
                out.append(put(e))
            elif not c.tr:
                r = _run_step(sc, i, c._replace(bell=False), counter)
                if r:
                    out.append(put(*r))
            else:
                out.append(put(c._replace(bell=False, look=False)))
        if c.tr and not c.look:                                # translated code runs on until the doorbell lands
            r = _run_step(sc, i, c, counter)
            if r:
                out.append(put(*r))
    elif c.ctx == 'host':
        out.append(put(_complete(sc, i, c, ctx='run', look=True)))
    elif c.ctx == 'init2':
        out.append(put(c._replace(ctx='wfs', phase='c1', word=c.word | {W})))
    elif c.ctx in LOOPS and c.phase == 'c1':
        out.append(put(c._replace(phase='c2', seen=c.posts)))
    elif c.ctx in LOOPS and c.phase == 'sleep':
        if c.bell:
            out.append(put(c._replace(phase='c1', bell=False)))
    elif c.ctx == 'hlt':
        e = _event(i, c, variant, hlt=True)
        if type(e) is tuple:                               # a typed stop (not a Cpu)
            return [('stop', i, e[1])]
        if e is None:
            out.append(put(c._replace(phase='sleep', halted=c.seen)))
        elif e.ctx != 'hlt':                                   # INIT taken
            out.append(put(e))
        elif _instruction(sc, i, c)[0] == 'halt':              # delivered: the idle loop halts again
            out.append(put(e._replace(phase='c1')))
        else:
            out.append(put(_complete(sc, i, e, ctx='run', phase=None, halted=None)))
    elif c.ctx == 'wfs':
        if N in c.word and 'nmi-wakes-wfs' in variant:
            return [('stop', i, 'nmi')]
        if S in c.word:
            out.append(put(c._replace(ctx='run', phase=None, prog='start', pc=0, word=c.word - {S, W}, vec=None,
                                      halted=None, runs=c.runs + ((('sipi', c.vec), 0),))))
        else:
            out.append(put(c._replace(phase='sleep', halted=None if 'wfs-not-idle' in variant else c.seen)))
    elif c.ctx == 'mbox':
        e = _take_init(i, c, variant) if I in c.word else None
        out.append(put(e or c._replace(phase='sleep', halted=c.seen)))
    return out


def _observe(cpus, label):
    return tuple((label(c), tuple(r for r in c.runs if r[1]), c.nmis, c.ints) for c in cpus)


def _end_label(variant):
    def label(c):
        return 'halted' if c.ctx == 'wfs' and 'wfs-reported-halted' in variant else LABEL[c.ctx]
    return label


def _impl_invariants(cpus):
    bad = []
    for i, c in enumerate(cpus):
        if S in c.word and not c.word & {I, W}:
            bad.append('J1: cpu%d has a SIPI latched with neither INIT pending nor wait-for-SIPI' % i)
        if (W in c.word) != (c.ctx == 'wfs') or I in c.word and c.ctx == 'wfs' or i == 0 and W in c.word:
            bad.append('J2: cpu%d: W %s in context %s' % (i, sorted(c.word), c.ctx))
        if c.halted is not None and c.ctx not in LOOPS:
            bad.append('J3: cpu%d looks idle in context %s' % (i, c.ctx))
    return bad


def _impl_end(cpus, variant):
    """The host's end decision: every vCPU ended or idle with every post seen; and J4's check."""
    if not all(c.ctx == 'done' or c.halted is not None and c.halted == c.posts for c in cpus):
        return None, []
    bad = []
    for i, c in enumerate(cpus):
        if c.ctx == 'done':
            continue
        if I in c.word or S in c.word:
            bad.append('J4: the run ends with cpu%d holding %s' % (i, sorted(c.word & {I, S})))
        if c.ctx == 'hlt' and _event(i, c, ()) is not None:
            bad.append('J4: the run ends with halted cpu%d able to take an event' % i)
    return ('end', _observe(cpus, _end_label(variant))), bad


# ---- the architectural reference ---------------------------------------------------------------------------------

def _ref_initial(sc):
    n = len(sc['cpus'])
    cpus = tuple(Ref(ctx=c.get('ctx', 'run') if c.get('ctx') != 'host' else 'run', prog='boot', pc=0,
                     tr=c.get('tr', False), if_=c.get('if_', False), nmi=False, irr=False,
                     sw=c.get('sw', c.get('ctx', 'run') != 'wfs'),
                     runs=(('boot', 0),) if c.get('ctx', 'run') in ('run', 'host', 'hlt') else (), nmis=0, ints=0)
                 for c in sc['cpus'])
    return cpus, 0, ((),) * (n * n)


def _ref_pending(c):
    return c.nmi or c.irr and c.sw and c.if_


def _ref_deliver(c):
    """(cpu or ('stop', reason)) for the event a boundary takes."""
    if c.nmi:
        return c._replace(nmi=False, nmis=c.nmis + 1) if c.tr else ('stop', 'nmi')
    return c._replace(irr=False, ints=c.ints + 1) if c.tr else ('stop', 'interrupt')


def _ref_arrive(i, c, msg):
    kind = msg[0]
    if c.ctx in ('done',):
        return c
    if kind == 'init':
        if c.ctx == 'wfs':
            return c._replace(nmi=False)                       # W1
        reset = dict(tr=False, if_=False, sw=False, irr=False, nmi=False, pc=0)
        if i == 0:
            return c._replace(ctx='run', prog='reset', runs=c.runs + (('reset', 0),), **reset)
        return c._replace(ctx='wfs', **reset)
    if kind == 'sipi':
        if c.ctx != 'wfs':
            return c
        return c._replace(ctx='run', prog='start', pc=0, tr=False, if_=False, runs=c.runs + ((('sipi', msg[1]), 0),))
    if kind == 'nmi':
        return c._replace(nmi=True)
    return c._replace(irr=c.irr or c.sw)


def _ref_steps(sc, state):
    cpus, counter, queues = state
    n = len(cpus)
    out = []

    def put(i, new, cnt=counter, q=queues):
        return tuple(new if k == i else x for k, x in enumerate(cpus)), cnt, q

    for i, c in enumerate(cpus):
        if c.ctx in ('run', 'hlt') and _ref_pending(c):
            e = _ref_deliver(c)
            if type(e) is tuple:                               # a typed stop (not a Cpu)
                out.append(('stop', i, e[1]))
                continue
            if c.ctx == 'hlt' and _instruction(sc, i, c)[0] != 'halt':
                e = _complete(sc, i, e, ctx='run')
            out.append(put(i, e))
        elif c.ctx == 'run':
            ins = _instruction(sc, i, c)
            if ins[0] == 'icr':
                q = list(queues)
                if ins[1] != 'deassert':
                    for t in _targets(n, i, ins[2]):
                        q[i * n + t] += ((ins[1], ins[3] if len(ins) > 3 else None),)
                out.append(put(i, _complete(sc, i, c), q=tuple(q)))
            elif ins[0] == 'host':
                out.append(put(i, _complete(sc, i, c)))
            else:
                r = _execute(sc, i, c, counter)
                if r:
                    out.append(put(i, *r))
    for k, q in enumerate(queues):
        if q:
            src, dst = divmod(k, n)
            c = cpus[dst]
            if c.ctx in ('run', 'hlt', 'wfs', 'mbox', 'done'):
                nq = list(queues)
                nq[k] = q[1:]
                out.append((tuple(_ref_arrive(dst, c, q[0]) if j == dst else x for j, x in enumerate(cpus)),
                            counter, tuple(nq)))
    return out


def _ref_end(state):
    cpus, _, queues = state
    if any(queues) or not all(c.ctx in ('done', 'wfs', 'mbox') or c.ctx == 'hlt' and not _ref_pending(c) for c in cpus):
        return None
    return 'end', _observe(cpus, lambda c: LABEL[c.ctx])


# ---- exploration ---------------------------------------------------------------------------------------------------

def explore(sc, spec=False, variant=(), limit=2_000_000):
    """Every outcome of the scenario under natemu's protocol (or the reference with `spec`), with the invariant
    violations found: {'outcomes': set, 'violations': [str], 'states': n}."""
    start = _ref_initial(sc) if spec else _initial(sc)
    seen, stack, outcomes, violations = {start}, [start], set(), []
    while stack:
        state = stack.pop()
        cpus = state[0]
        if spec:
            succ = _ref_steps(sc, state)
            end = _ref_end(state)
        else:
            cpus, counter = state
            violations += [v for v in _impl_invariants(cpus) if v not in violations]
            end, bad = _impl_end(cpus, variant)
            violations += [v for v in bad if v not in violations]
            succ = [s for i in range(len(cpus)) for s in _cpu_steps(sc, cpus, counter, i, variant)]
        if end:
            outcomes.add(end)
            continue
        if not succ:
            outcomes.add(('stuck', _observe(cpus, lambda c: LABEL.get(c.ctx, c.ctx))))
        for s in succ:
            if s[0] == 'stop':
                outcomes.add(('stop', s[1], s[2], tuple((r, m, k) for _, r, m, k in _observe(cpus, lambda c: ''))))
            elif s not in seen:
                seen.add(s)
                stack.append(s)
                if len(seen) > limit:
                    raise RuntimeError('state limit')
    return {'outcomes': outcomes, 'violations': violations, 'states': len(seen)}


def check(name, variant=()):
    """J1-J5 for a named scenario: the violations (empty: natemu's protocol is right there)."""
    sc = SCENARIOS[name]
    impl, ref = explore(sc, variant=variant), explore(sc, spec=True)
    extra = sorted(impl['outcomes'] - ref['outcomes'], key=repr)
    return impl['violations'] + ['J5: an outcome the reference does not permit: %r' % (o,) for o in extra]


def outcomes(name):
    """The outcomes the reference permits for a named scenario (what a framework case's result must be one of)."""
    return explore(SCENARIOS[name], spec=True)['outcomes']


# ---- scenarios ---------------------------------------------------------------------------------------------------

TRAMPOLINE = [('op',), ('op',), ('t',), ('sti',), ('signal',), ('halt',)]       # real mode, the climb, the OS idles
STUB = [('op',), ('op',), ('t',), ('signal',), ('halt',)]                        # EDK2's AP: back to its HLT loop

SCENARIOS = {
    # Linux v6.12 without the mailbox: INIT assert and de-assert, two SIPIs, the wait for the AP's check-in.
    'linux': dict(cpus=[dict(tr=True, boot=[('icr', 'init', 1), ('icr', 'deassert', 1), ('icr', 'sipi', 1, V),
                                            ('icr', 'sipi', 1, V), ('await', 1), ('halt',)]),
                        dict(ctx='wfs', start=TRAMPOLINE)]),
    # kvm-unit-tests: INIT and SIPI back to back to all-but-self, then the wait for every AP.
    'kvm-unit-tests': dict(cpus=[dict(tr=True, boot=[('icr', 'init', 'others'), ('icr', 'sipi', 'others', 0),
                                                     ('await', 2), ('halt',)]),
                                 dict(ctx='wfs', start=STUB), dict(ctx='wfs', start=STUB)]),
    # EDK2 MpInitLib: INIT-SIPI-SIPI rounds to the same AP, each after the last round's check-in.
    'edk2': dict(cpus=[dict(tr=True, boot=[('icr', 'init', 1), ('icr', 'sipi', 1, 0x87), ('icr', 'sipi', 1, 0x87),
                                           ('await', 1), ('icr', 'init', 1), ('icr', 'sipi', 1, 0x87),
                                           ('icr', 'sipi', 1, 0x87), ('await', 2), ('halt',)]),
                       dict(ctx='wfs', start=STUB)]),
    # The SIPI reaches the AP before it takes its INIT: it runs translated code, or is inside a host request.
    'sipi-before-init-taken': dict(cpus=[dict(tr=True, boot=[('icr', 'init', 1), ('icr', 'sipi', 1, V), ('halt',)]),
                                         dict(tr=True, boot=[('op',), ('op',), ('halt',)], start=STUB)]),
    'sipi-during-host-request': dict(cpus=[dict(tr=True, boot=[('icr', 'init', 1), ('icr', 'sipi', 1, V), ('halt',)]),
                                           dict(ctx='host', tr=True, boot=[('host',), ('op',), ('halt',)],
                                                start=STUB)]),
    # A SIPI sent before the INIT, to a running AP: discarded; the AP ends waiting.
    'sipi-then-init': dict(cpus=[dict(tr=True, boot=[('icr', 'sipi', 1, V), ('icr', 'init', 1), ('halt',)]),
                                 dict(tr=True, boot=[('op',), ('halt',)], start=STUB)]),
    # An NMI in wait-for-SIPI: held, then the first event after the SIPI (a typed stop in interpreted code) ...
    'nmi-in-wfs': dict(cpus=[dict(tr=True, boot=[('icr', 'nmi', 1), ('icr', 'sipi', 1, V), ('halt',)]),
                             dict(ctx='wfs', start=STUB)]),
    # ... held for good when no SIPI follows; and around an INIT: one before it may be delivered or dropped.
    'nmi-held': dict(cpus=[dict(tr=True, boot=[('icr', 'nmi', 1), ('halt',)]), dict(ctx='wfs', start=STUB)]),
    'nmi-around-init': dict(cpus=[dict(tr=True, boot=[('icr', 'nmi', 1), ('icr', 'init', 1), ('icr', 'nmi', 1),
                                                      ('icr', 'sipi', 1, V), ('halt',)]),
                                  dict(tr=True, boot=[('op',), ('halt',)], start=STUB)]),
    # HLT with IF = 0 in interpreted code: a SIPI does not wake it, an INIT does, the next SIPI starts the CPU.
    'hlt-init-not-sipi': dict(cpus=[dict(tr=True, boot=[('icr', 'sipi', 1, V), ('icr', 'init', 1),
                                                        ('icr', 'sipi', 1, V), ('halt',)]),
                                    dict(ctx='hlt', boot=[('halt',)], start=STUB)]),
    # An NMI to HLT in interpreted code: a typed stop (no delivery in interpreted modes).
    'nmi-in-interpreted-hlt': dict(cpus=[dict(tr=True, boot=[('icr', 'nmi', 1), ('halt',)]),
                                         dict(ctx='hlt', boot=[('halt',)])]),
    # A second INIT while a SIPI is latched (or after it started the CPU): the CPU ends waiting again; EDK2's rounds
    # back to back, without the check-in between them.
    'init-sipi-init': dict(cpus=[dict(tr=True, boot=[('icr', 'init', 1), ('icr', 'sipi', 1, V), ('icr', 'init', 1),
                                                     ('halt',)]),
                                 dict(ctx='wfs', start=STUB)]),
    'edk2-back-to-back': dict(cpus=[dict(tr=True, boot=[('icr', 'init', 1), ('icr', 'sipi', 1, 0x87),
                                                        ('icr', 'sipi', 1, 0x87), ('icr', 'init', 1),
                                                        ('icr', 'sipi', 1, 0x87), ('icr', 'sipi', 1, 0x87), ('halt',)]),
                                    dict(tr=True, boot=[('op',), ('halt',)], start=STUB)]),
    # INIT taken in each place a target can be: the mailbox wait, translated code, translated HLT, interpreted code.
    'init-in-mailbox': dict(cpus=[dict(tr=True, boot=[('icr', 'init', 1), ('icr', 'sipi', 1, V), ('icr', 'sipi', 1, V),
                                                      ('await', 1), ('halt',)]),
                                  dict(ctx='mbox', start=STUB)]),
    'init-in-translated-hlt': dict(cpus=[dict(tr=True, boot=[('icr', 'init', 1), ('icr', 'sipi', 1, V),
                                                             ('icr', 'sipi', 1, V), ('await', 1), ('halt',)]),
                                         dict(ctx='hlt', tr=True, boot=[('halt',)], start=STUB)]),
    'init-in-interpreted-code': dict(cpus=[dict(tr=True, boot=[('icr', 'init', 1), ('icr', 'sipi', 1, V),
                                                               ('await', 1), ('halt',)]),
                                           dict(boot=[('op',), ('op',), ('op',), ('halt',)], start=STUB)]),
    # Linux's shutdown without a mailbox: the other CPUs halted with IF = 0 are parked with INIT; the run ends with
    # them waiting for a SIPI, reported as such.
    'park-with-init': dict(cpus=[dict(tr=True, boot=[('icr', 'init', 'others'), ('halt',)]),
                                 dict(ctx='hlt', tr=True, boot=[('halt',)]), dict(ctx='hlt', tr=True, boot=[('halt',)])]),
    # Firmware boot with an AP never started: the run ends when the BSP halts; the AP is reported waiting.
    'never-started': dict(cpus=[dict(tr=True, boot=[('op',), ('halt',)]), dict(ctx='wfs', start=STUB)]),
    # INIT to the BSP: it restarts at the reset vector; the SIPI after it is discarded.
    'bsp-init': dict(cpus=[dict(tr=True, boot=[('op',), ('op',), ('halt',)], reset=[('op',), ('halt',)]),
                           dict(tr=True, boot=[('icr', 'init', 0), ('icr', 'sipi', 0, V), ('halt',)])]),
    # A fixed IPI to a CPU waiting for its SIPI: not accepted (the APIC is software-disabled), so never delivered.
    'fixed-in-wfs': dict(cpus=[dict(tr=True, boot=[('icr', 'fixed', 1, 0x40), ('icr', 'sipi', 1, V), ('halt',)]),
                               dict(ctx='wfs', start=[('op',), ('t',), ('enable',), ('sti',), ('halt',)])]),
    # A fixed IPI racing an INIT and the started AP's APIC enable (its arrival is never earlier than the read of
    # SW_ENABLED nor later than the IRR store: either is a delivery latency the SDM allows).
    'fixed-around-init': dict(cpus=[dict(tr=True, boot=[('icr', 'init', 1), ('icr', 'sipi', 1, V), ('halt',)]),
                                    dict(tr=True, if_=True, boot=[('op',), ('halt',)],
                                         start=[('op',), ('t',), ('enable',), ('sti',), ('halt',)]),
                                    dict(tr=True, boot=[('icr', 'fixed', 1, 0x40), ('halt',)])]),
}


if __name__ == '__main__':
    for name in SCENARIOS:
        problems = check(name)
        assert not problems, (name, problems)
    print('initsipi_model: %d scenarios, natemu protocol refines the reference, J1-J5 hold' % len(SCENARIOS))
