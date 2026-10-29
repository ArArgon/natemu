"""Judge a delivery to the program driver's logging IDT against tools/models/delivery_model.py (X1a).

The driver (crates/host/src/drivers/program.rs, "logging IDT") gives every vCPU a GDT, TSSs and an IDT of 256 interrupt
gates (CS 0x08, DPL 3, IST 1) at LOG_BASE (driver-built spaces) or LOG_IDENTITY (identity spaces); reaching a handler
ends the run and the driver reports `delivered` (vector, raw frame RIP CS RFLAGS RSP SS error, handler RSP and CPL).
`problems(out, ...)` rebuilds that environment as the model's state at the faulting instruction (its RIP, RSP, RFLAGS,
CPL and the selectors the driver starts with), delivers the expected exception in the model and lists every difference
in the frame, the handler's RSP and CPL. The model is the judge; the expected vector, error code and CR2 come from the
runner's own SDM reference, as before."""
from models import delivery_model as dm

LOG_BASE, LOG_IDENTITY = 0xffff_ff00_0000_0000, 0x3e_8000
RF = 1 << 16


def _state(base, vcpu, cpl, rip, rsp, rflags):
    memory = {}
    for off in (0, 0x1000, 0x3000, 0x4000, 0x5000, 0x6000):
        memory[(base + off) >> 12] = dm.page(write=True, user=False)     # supervisor-only, as the driver maps them
    gdt = [0, dm.segment(0), dm.segment(0, code=False), dm.segment(3, code=False), dm.segment(3), dm.segment(0, l=0, d=1)]
    for i, d in enumerate(gdt):
        dm.place(memory, base + 8 * i, d.to_bytes(8, 'little'))
    for v in range(4):
        top = base + 0x4000 + 0x1000 * v
        dm.place(memory, base + 0x800 + 0x80 * v, dm.tss(rsp=(top, 0, 0), ist=(top,) + (0,) * 6))
    for n in range(256):
        dm.place(memory, base + 0x1000 + 16 * n, dm.gate(base + 0x2000 + 16 * n, 0x08, dpl=3, ist=1))
    return {'rip': rip, 'rsp': rsp, 'rflags': rflags, 'cpl': cpl, 'cs': 0x23 if cpl == 3 else 0x08, 'ss': 0x1B if cpl == 3 else 0x10,
            'cs_l': 1, 'cr0': 0x80010011, 'cr2': 0, 'cr4': 0x20, 'idtr': (base + 0x1000, 0xFFF), 'gdtr': (base, 0x6F),
            'tr': (0x30 + 16 * vcpu, base + 0x800 + 0x80 * vcpu, 0x67), 'memory': memory, 'nmi_blocked': False,
            'shadow': None, 'halted': False}


def problems(out, vector, error, cr2, cpl, identity=False, vcpu=None):
    """Differences between the driver's `delivered` record in `out` and delivery_model's delivery of exception
    (vector, error, cr2) at the state `out` reports (RIP, RSP, RFLAGS from the frame's slots; the model recomputes RF)."""
    d = out.get('delivered')
    if not d:
        return [f"no logged delivery ({out['stop']} {out['reason']})"]
    num = lambda x: int(x, 16) if isinstance(x, str) else x
    vcpu = out.get('vcpu', 0) if vcpu is None else vcpu          # its own TSS and IST stack
    rip, rsp = num(out['rip']), num(out['gpr'][4])
    base = LOG_IDENTITY if identity else LOG_BASE
    s = _state(base, vcpu, cpl, rip, rsp, num(out['rflags']) & ~RF)
    event = {'kind': 'exception', 'vector': vector, 'error': error}
    if vector == dm.PF:
        event['cr2'] = cr2
    r = dm.deliver(s, event)
    if r['outcome'] != 'delivered' or len(r['events']) != 1:
        return [f"model: {r['outcome']} {r['events']}"]
    n = r['state']
    want = [dm.qword(n['memory'], n['rsp'] + 8 * k) for k in range(6 if vector in dm.ERROR_CODE_VECTORS else 5)]
    if vector not in dm.ERROR_CODE_VECTORS:
        want.append(0)
    else:
        want = want[1:] + want[:1]                                 # model order error, RIP, ...; the driver's ends with it
    got = [num(x) for x in d['frame']]
    out_ = []
    if got != want:
        out_.append(f"frame {[hex(x) for x in got]} != model {[hex(x) for x in want]}")
    if num(d['handler_rsp']) != n['rsp'] or d['cpl'] != n['cpl'] or d['vector'] != vector:
        out_.append(f"handler rsp {d['handler_rsp']} cpl {d['cpl']} vector {d['vector']} != model {n['rsp']:#x} {n['cpl']} {vector}")
    return out_


def model_frame(vector, error, cr2, rip, rsp, rflags, length=None):
    """delivery_model's same-CPL delivery at CPL 0 through a present 64-bit interrupt gate (CS 0x08, no IST) of an
    exception (vector, error, cr2) at `rip`, or with `length` of an INT n / INT3 at `rip`: the frame (RIP, CS, RFLAGS,
    RSP, SS, error code or 0) and the address of its RIP slot."""
    gdt, idt = 0x1000, 0x2000
    memory = {1: dm.page(), 2: dm.page()}
    for a in range(((rsp & ~15) - 64) >> 12, ((rsp & ~15) >> 12) + 1):
        memory.setdefault(a, dm.page())
    dm.place(memory, gdt + 8, dm.segment(0).to_bytes(8, 'little'))
    dm.place(memory, gdt + 16, dm.segment(0, code=False).to_bytes(8, 'little'))
    dm.place(memory, idt + 16 * vector, dm.gate(0x10_0000, 0x08))
    s = {'rip': rip, 'rsp': rsp, 'rflags': rflags & ~RF, 'cpl': 0, 'cs': 0x08, 'ss': 0x10, 'cs_l': 1, 'cr0': 0x80010011,
         'cr2': 0, 'cr4': 0x20, 'idtr': (idt, 0xFFF), 'gdtr': (gdt, 0x17), 'tr': (0, 0, 0), 'memory': memory,
         'nmi_blocked': False, 'shadow': None, 'halted': False}
    event = ({'kind': 'int3', 'length': length} if vector == dm.BP and length == 1 else {'kind': 'int', 'vector': vector, 'length': length}
             ) if length else {'kind': 'exception', 'vector': vector, 'error': error, 'cr2': cr2}
    r = dm.deliver(s, event)
    assert r['outcome'] == 'delivered' and len(r['events']) == 1, r
    n = r['state']
    words = [dm.qword(n['memory'], n['rsp'] + 8 * k) for k in range(6 if vector in dm.ERROR_CODE_VECTORS and not length else 5)]
    if len(words) == 6:
        return words[1:] + words[:1], n['rsp'] + 8
    return words + [0], n['rsp']


if __name__ == '__main__':
    # Self-check: a #PF at CPL 3 and a #UD at CPL 0 as the driver would report them.
    top = LOG_BASE + 0x4000
    out = {'stop': 'guest_fault', 'reason': 'delivered', 'rip': '0x400000', 'rflags': '0x202', 'gpr': [0] * 4 + ['0x601000'],
           'delivered': {'vector': 14, 'frame': ['0x400000', '0x23', '0x10202', '0x601000', '0x1b', '0x4'], 'error_code': True,
                         'handler_rsp': hex(top - 48), 'cpl': 0}}
    assert problems(out, 14, 4, 0x1234_5000, 3) == [], problems(out, 14, 4, 0x1234_5000, 3)
    out['delivered']['frame'][2] = '0x202'
    assert problems(out, 14, 4, 0x1234_5000, 3), 'RF image not checked'
    out = {'stop': 'guest_fault', 'reason': 'delivered', 'rip': '0x400000', 'rflags': '0x2', 'gpr': [0] * 4 + ['0x7008'],
           'delivered': {'vector': 6, 'frame': ['0x400000', '0x8', '0x10002', '0x7008', '0x10', '0x0'], 'error_code': False,
                         'handler_rsp': hex(top - 40), 'cpl': 0}}
    assert problems(out, 6, 0, 0, 0) == [], problems(out, 6, 0, 0, 0)
    assert model_frame(14, 2, 0x5000, 0x110010, 0x3e1808, 0x46) == ([0x110010, 8, 0x10046, 0x3e1808, 0x10, 2], 0x3e1800 - 40)
    assert model_frame(3, 0, 0, 0x110010, 0x3e1800, 0x10046, length=1) == ([0x110011, 8, 0x46, 0x3e1800, 0x10, 0], 0x3e1800 - 40)
    print('ok')
