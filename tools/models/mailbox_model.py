"""Reference model of the ACPI Multiprocessor Wakeup mailbox, version 0 (ACPI 6.5 s5.2.12.19, Tables 5.43/5.44).

Mailbox (little endian): Command u16 @0, Reserved u16 @2, ApicId u32 @4, WakeupVector u64 @8.
Command: 0 Noop, 1 Wakeup, 2..0xFFFF reserved (the 6.6 "Test" command does not exist in version 0).
An AP waiting on the mailbox that sees Wakeup with its own APIC ID caches the vector, clears Command to
Noop (the acknowledgement) and jumps to the vector; a mismatched ID or reserved command is ignored; each AP
takes one wakeup, then never checks the mailbox again. The OS sends the next command only after Command is Noop.
APIC IDs are 0..N-1; 0 is the BSP (never waits), 1..N-1 wait.
Run: python3 tools/models/mailbox_model.py
"""

NOOP, WAKEUP = 0, 1


def run(ncpu, steps):
    """Outcome of an OS script. Each step: write ApicId, WakeupVector, Command; wait for Noop; else the OS cancels (writes 0)."""
    waiting = set(range(1, ncpu))
    starts = {i: [] for i in range(ncpu)}
    cmd, apic, vec = NOOP, 0, 0
    acks = []
    for s in steps:
        apic, vec, cmd = s['apic_id'] & 0xFFFFFFFF, s['vector'] & (2**64 - 1), s['command'] & 0xFFFF
        ack = cmd == WAKEUP and apic in waiting
        if ack:
            waiting.discard(apic)
            starts[apic].append(vec)  # vector cached before the clear
            cmd = NOOP
        elif cmd != NOOP:
            cmd = NOOP  # OS cancel after the bounded wait
        acks.append(ack)
    return {'acks': acks, 'starts': starts, 'mailbox': (cmd, apic, vec)}


def check_trace(ncpu, events):
    """Violations (strings) in an observed trace; empty list = conforms."""
    bad = []
    apic = vec = cmd = 0          # mailbox as the OS last wrote it
    pending = None                # (apic_id, cached vector) of an unacknowledged Wakeup
    cached, started = {}, set()   # per-AP: vector cached at ack; APs that have started
    for n, ev in enumerate(events):
        def v(msg):
            bad.append('event %d %r: %s' % (n, ev, msg))
        kind = ev[0]
        if kind == 'os_write':
            _, field, val = ev
            if field == 'apic_id':
                apic = val
            elif field == 'vector':
                vec = val
            elif field == 'command':
                if pending and val != NOOP:
                    v('new command while the previous Wakeup is unacknowledged')
                cmd = val
                pending = (apic, vec) if cmd == WAKEUP else None  # Noop cancels
            else:
                v('unknown field')
        elif kind in ('ap_ack', 'ap_start'):
            ap = ev[1]
            if ap == 0 or not 0 < ap < ncpu:
                v('BSP or unknown APIC ID')
            elif kind == 'ap_ack':
                if ap in cached:
                    v('second ack by the same AP')
                elif pending is None:
                    v('ack of Noop' if cmd == NOOP else 'ack of reserved command' if cmd != WAKEUP
                      else 'ack with no unacknowledged Wakeup')
                elif pending[0] != ap:
                    v('ack by an AP the Wakeup is not addressed to')
                else:
                    cached[ap], pending = pending[1], None
            else:
                if ap in started:
                    v('second start by the same AP')
                elif ap not in cached:
                    v('start without a preceding ack')
                elif ev[2] != cached[ap]:
                    v('start vector %#x is not the vector cached at the Wakeup (%#x)' % (ev[2], cached[ap]))
                started.add(ap)
        else:
            v('unknown event')
    return bad


if __name__ == '__main__':
    r = run(3, [{'apic_id': 2, 'vector': 0x1000, 'command': 1}, {'apic_id': 2, 'vector': 0x2000, 'command': 1},
                {'apic_id': 0, 'vector': 0x3000, 'command': 1}, {'apic_id': 1, 'vector': 0x4000, 'command': 7},
                {'apic_id': 1, 'vector': 0x5000, 'command': 1}])
    assert r['acks'] == [True, False, False, False, True], r
    assert r['starts'] == {0: [], 1: [0x5000], 2: [0x1000]} and r['mailbox'] == (0, 1, 0x5000), r
    assert check_trace(2, [('os_write', 'apic_id', 1), ('os_write', 'vector', 0x1000), ('os_write', 'command', 1),
                           ('os_write', 'vector', 0x9000), ('ap_ack', 1), ('ap_start', 1, 0x1000)]) == []
    assert check_trace(2, [('ap_start', 1, 0)])
    print('mailbox_model self-checks ok')
