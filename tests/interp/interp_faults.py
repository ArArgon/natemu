"""The boot-time interpreter's fault cases against the models (boot-interpreter plan section 8; the interpreter's
transitions, docs/restructure/interp-transitions.md): machine descriptions for natemu's program driver, each with the
result the model gives. Run by run_interp_natemu.py --faults (case `natemu-faults`).

- **Segment cases** (`tools/models/segment_model.py`: `jmp_far_interp`, `ret_far_interp`, `load_interp`): far JMP (ptr16:16,
  ptr16:32, m16:32), far RET (16- and 32-bit, with an imm16) and MOV Sreg in R, P16, P32 and C32 at CPL 0, over a GDT of
  their own (`FDESC`, at `FGDT`) whose descriptors cover every check: null, the table's limit, TI = 1 (the INIT state's LDT
  at base 0), data, system descriptors (call gate, TSS available and busy, task gate, LDT), DPL and RPL, conforming code, not present, L = D
  = 1, a small limit, the accessed bit, read-only and execute-only segments; the operand's and the stack's segment
  limits; in C32 a not-present page under the operand, the stack or the descriptor, and a read-only GDT page under
  CR0.WP. The model runs on the case's own state (its memory, the hidden segment parts, the paging of C32 as present and
  supervisor-writable pages) and gives: completion (the state natemu must reach: the stop at the target or the
  fall-through, the selectors, ESP, the GDT's bytes with any accessed bit set), an exception (`permitted`: the typed stop
  `interp_fault` with one of them, and nothing committed: every field as it started), or a refusal (a gate, a task switch
  or an outer return: the typed stop `interp_unsupported`, detail GATE or OUTER, nothing committed).
- **Mode programs** (`tools/models/modes_model.py`'s profiles 22-49 that end in an interpreted mode or complete): the
  model's program from P32 with paging off, run whole; natemu must end where the model's records first fault or stop (a
  typed stop `interp_fault` with the record's vector and error code at its instruction, or `interp_mode` detail PAGING at
  the MOV to CR0 that enables paging without long mode), or reach the program's end. Their 64-bit stretches run
  translated: the far JMP to 64-bit code is the interpreter's hand-off, the 64-bit m16:64 far JMP back to the 32-bit
  CS the translator's entry into the interpreter (`refuse_compatibility`'s CPL-0 arm). Left out: profiles whose ending is
  in 64-bit code (24, 28, 30, 34, 35, 38: the translator's, already the `system` family's), and 36 (a 16-bit TSS in TR,
  which a machine description cannot state)."""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'tests' / 'lib')); import testenv  # noqa: E401,E402,E702
import copy

import interp_cases as G
import interp_rig as R

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'tools'))
from models import segment_model as S  # noqa: E402
from models import modes_model as M  # noqa: E402

num = R.num
FGDT = 0x9000
FDESC = {
    0x08: 0x00CF9B000000FFFF,     # code32 flat
    0x10: 0x00CF93000000FFFF,     # data32 flat
    0x18: 0x00AF9B000000FFFF,     # code64 (16-bit code while LMA = 0)
    0x20: 0x00009B000000FFFF,     # code16, base 0, limit FFFFH
    0x28: 0x00CF13000000FFFF,     # data, not present
    0x30: 0x00CF9A000000FFFF,     # code32, accessed bit clear
    0x38: 0x00CFFB000000FFFF,     # code32 DPL 3
    0x40: 0x00CF9F000000FFFF,     # conforming readable code, DPL 0
    0x48: 0x00CFFF000000FFFF,     # conforming code, DPL 3
    0x50: 0x00CF1B000000FFFF,     # code32, not present
    0x58: 0x00EF9B000000FFFF,     # L = D = 1
    0x60: 0x00008C0000081000,     # 32-bit call gate to 08H:1000H
    0x68: 0x0000890030000067,     # 32-bit TSS, available
    0x70: 0x0000850000680000,     # task gate (TSS 68H)
    0x78: 0x0000820040000FFF,     # LDT
    0x80: 0x00409B0000000FFF,     # code32, limit FFFH (byte granular)
    0x88: 0x00CF91000000FFFF,     # data, read-only
    0x90: 0x00CFF3000000FFFF,     # data DPL 3
    0x98: 0x00CF99000000FFFF,     # code, execute-only
    0xA0: 0x00CF92000000FFFF,     # data32 flat, accessed bit clear
    0xA8: 0x00CF93040000FFFF,     # data32, base 40000H
    0xB0: 0x00008B0030000067,     # 32-bit TSS, busy
    0xB8: 0x0000830030000067,     # 16-bit TSS, busy
}
FGDT_LIMIT = 0xBF
FGDT_BYTES = b''.join(FDESC.get(s, 0).to_bytes(8, 'little') for s in range(0, FGDT_LIMIT + 1, 8))
PT = G.PML4 + 0x3000             # the common tables' 4 KiB page table of 0-2 MiB (supervisor, writable, A and D set)
TARGET32, TARGET16 = G.LAND, 0x5000
MODEL_SREGS = S.SREGS             # ('es', 'cs', 'ss', 'ds', 'fs', 'gs'), the bank's order
GATE, OUTER, PAGING = 6, 7, 1     # natemu_abi::execution::stop::interp


class Pages(dict):
    """The model's memory for a case: guest RAM below 2 MiB (zero unless a block wrote it) but the legacy hole, as
    present supervisor-writable pages; `absent` pages are not (C32: their PTE cleared), `readonly` ones not writable."""

    def __init__(self, blocks, absent=(), readonly=()):
        super().__init__()
        self.absent, self.readonly = set(absent), set(readonly)
        for a, data in blocks:
            for k, b in enumerate(data):
                self._page((a + k) >> 12)['data'][(a + k) & 0xFFF] = b

    def _page(self, p):
        if p not in self.keys():
            super().__setitem__(p, {'data': bytearray(4096), 'write': p not in self.readonly, 'user': False})
        return super().__getitem__(p)

    def _ram(self, p):
        return p < 0x200 and not 0xA0 <= p < 0x100 and p not in self.absent

    def __contains__(self, p):
        return self._ram(p)

    def __getitem__(self, p):
        if not self._ram(p): raise KeyError(p)
        return self._page(p)

    def get(self, p, default=None):
        return self._page(p) if self._ram(p) else default

    def __deepcopy__(self, memo):
        new = Pages([], self.absent, self.readonly)
        for p, v in self.items():
            dict.__setitem__(new, p, copy.deepcopy(v, memo))
        return new


def model_state(case, blocks, absent=(), readonly=()):
    """The segment model's state of a case (its `state`, memory `blocks` as (address, bytes))."""
    st = case['state']
    seg = {r: (num(st['seg'][r]['sel']), num(st['seg'][r]['attr'])) for r in MODEL_SREGS}
    shape = {r: (num(st['seg'][r]['base']), num(st['seg'][r]['limit'])) for r in MODEL_SREGS}
    return dict(rip=num(st['eip']), length=0, rsp=num(st['gpr']['esp']), rflags=num(st['eflags']), cpl=0, cs=seg['cs'][0],
                ss=seg['ss'][0], cr0=num(st['cr0']), cr3=num(st['cr3']), cr4=num(st['cr4']), efer=num(st['efer']),
                gdtr=(num(st['gdtr']['base']), num(st['gdtr']['limit'])),
                idtr=(num(st['idtr']['base']), num(st['idtr']['limit'])), ldtr=(0, 0, 0xFFFF), tr=(0, 0, 0xFFFF),
                ldtr_attributes=0x82, tr_attributes=0x8B, seg=seg, shape=shape, fs_base=shape['fs'][0],
                gs_base=shape['gs'][0], memory=Pages(blocks, absent, readonly))


def starting_fields(case, blocks):
    """The machine fields a case starts with (what a typed stop must leave): GPRs, EFLAGS, selectors, controls, tables,
    windows."""
    st, mem = case['state'], {}
    for a, data in blocks:
        mem.update({a + k: b for k, b in enumerate(data)})
    out = {**{r: num(st['gpr'][r]) for r in G.GPRS}, 'eflags': num(st['eflags']),
           **{s: num(st['seg'][s]['sel']) for s in G.SREGS}, **{k: num(st[k]) & 0xffffffff for k in ('cr0', 'cr3', 'cr4', 'efer')},
           'gdtr': [num(st['gdtr']['limit']), num(st['gdtr']['base'])], 'idtr': [num(st['idtr']['limit']), num(st['idtr']['base'])]}
    for i, w in enumerate(case['compare']['windows']):
        out[f'window{i}'] = bytes(mem.get(num(w['addr']) + k, 0) for k in range(w['len'])).hex()
    return out


def _case(N, name, mode, asm, *, regs=None, segs=None, mem=(), stops=None, ctl=None, gdt=True, absent=(), readonly=()):
    """A case through run_interp_natemu.interp_case, with the fault GDT (`gdt`), `ctl` over the mode's controls, the
    pages `absent` (C32: not present) and `readonly` (C32: read-only, CR0.WP set): its machine description input and its
    memory blocks (common blocks first)."""
    pte = [(PT + 8 * p, ((p << 12) | (0x61 if p in readonly else 0x63)).to_bytes(8, 'little') if p not in absent else bytes(8))
           for p in sorted({*absent, *readonly})]
    if pte: assert mode == 'C32', name
    mem = [*((FGDT, FGDT_BYTES),) * gdt, *mem, *pte]
    c = N.interp_case(name, mode, asm, regs=regs, segs=segs, mem=mem, stops=stops, windows=[(FGDT, FGDT_LIMIT + 1)])
    if gdt:
        c['state']['gdtr'] = {'base': G.h(FGDT), 'limit': G.h(FGDT_LIMIT)}
    for k, v in (ctl or {}).items():
        c['state'][k] = G.h(v)
    if readonly:
        c['state']['cr0'] = G.h(num(c['state']['cr0']) | 0x10000)
    common = [(num(b['addr']), bytes.fromhex(b['data'])) for b in G.corpus()['common']]
    return c, [*common, *mem], (absent, readonly)


def segment_cases(N):
    """(name, case, blocks, model call, paging) for the segment model's cases; the code is assembled by the caller."""
    out = []
    data_off = lambda mode: G.DATA + 8 - G.start_segments(mode)['ds']['base']
    sel_ptr = lambda sel, off, w=4: off.to_bytes(w, 'little') + sel.to_bytes(2, 'little')
    for mode in ('P16', 'P32', 'C32'):
        lma = mode == 'C32'
        b = 'bx' if mode == 'P16' else 'ebx'
        targets = [(0x00, TARGET32), (0x03, TARGET32), (0xC0, TARGET32), (0x0C, TARGET32), (0x10, TARGET32), (0x60, TARGET32),
                   (0x68, TARGET32), (0xB0, TARGET32), (0xB8, TARGET32), (0x70, TARGET32), (0x78, TARGET32), (0x38, TARGET32),
                   (0x0B, TARGET32), (0x43, TARGET32), (0x48, TARGET32), (0x50, TARGET32), (0x58, TARGET32), (0x80, 0x1000),
                   (0x80, 0xFFF), (0x30, TARGET32), (0x18, TARGET32), (0x20, TARGET16), (0x20, 0x10000)]
        for sel, off in targets:
            tag = f'{sel:02x}-{off:x}'
            out.append((f'{mode}-jmp_far_ptr16:32-{tag}', mode, [f'jmp dword {sel:#x}:{off:#x}'], {}, [],
                        ('jmp', dict(target=(sel, off), osize=32)), (), ()))
            out.append((f'{mode}-jmp_far_m16:32-{tag}', mode, [f'jmp far dword [{b}]'], {'ebx': data_off(mode)},
                        [(G.DATA + 8, sel_ptr(sel, off))], ('jmp', dict(pointer=('ds', data_off(mode)), osize=32)), (), ()))
            st = sel | 0x5a5a0000                     # the CS slot's upper half: ignored
            out.append((f'{mode}-retf-{tag}', mode, ['o32 retf'], {}, [(G.STACK, off.to_bytes(4, 'little') + st.to_bytes(4, 'little'))],
                        ('ret', dict(osize=32)), (), ()))
        out.append((f'{mode}-jmp_far_ptr16:16-20-5000', mode, [f'jmp word 0x20:{TARGET16:#x}'], {}, [],
                    ('jmp', dict(target=(0x20, TARGET16), osize=16)), (), ()))
        out.append((f'{mode}-retf16-20-5000', mode, ['o16 retf'], {}, [(G.STACK, TARGET16.to_bytes(2, 'little') + (0x20).to_bytes(2, 'little'))],
                    ('ret', dict(osize=16)), (), ()))
        out.append((f'{mode}-retf_imm-08', mode, ['o32 retf 8'], {}, [(G.STACK, TARGET32.to_bytes(4, 'little') + (8).to_bytes(4, 'little'))],
                    ('ret', dict(osize=32, release=8)), (), ()))
        # MOV Sreg.
        for sreg, sels in (('ss', (0x00, 0x10, 0x13, 0x88, 0x08, 0x90, 0x28, 0xA0, 0xC0, 0x60)),
                           ('ds', (0x00, 0x03, 0x10, 0x08, 0x98, 0x13, 0x43, 0x28, 0xC0, 0x60, 0x0C, 0xA0)),
                           ('fs', (0xA8, 0x00)), ('es', (0x88, 0x48))):
            for sel in sels:
                out.append((f'{mode}-mov_{sreg}-{sel:02x}', mode, [f'mov {sreg}, ax'], {'eax': sel}, [],
                            ('load', dict(sreg=sreg, sel=sel)), (), ()))
        if lma:
            # Pages that are not present (the operand's, the stack's, the descriptor's) and a read-only GDT page under WP.
            out.append((f'{mode}-jmp_far_m16:32-pf', mode, [f'jmp far dword [{b}]'], {'ebx': data_off(mode)},
                        [(G.DATA + 8, sel_ptr(8, TARGET32))], ('jmp', dict(pointer=('ds', data_off(mode)), osize=32)),
                        (G.DATA >> 12,), ()))
            out.append((f'{mode}-retf-pf', mode, ['o32 retf'], {}, [], ('ret', dict(osize=32)), (G.STACK >> 12,), ()))
            out.append((f'{mode}-jmp_far-descriptor-pf', mode, [f'jmp dword 0x08:{TARGET32:#x}'], {}, [],
                        ('jmp', dict(target=(8, TARGET32), osize=32)), (FGDT >> 12,), ()))
            out.append((f'{mode}-jmp_far-accessed-readonly', mode, [f'jmp dword 0x30:{TARGET32:#x}'], {}, [],
                        ('jmp', dict(target=(0x30, TARGET32), osize=32)), (), (FGDT >> 12,)))
            out.append((f'{mode}-mov_ds-accessed-readonly', mode, ['mov ds, ax'], {'eax': 0xA0}, [],
                        ('load', dict(sreg='ds', sel=0xA0)), (), (FGDT >> 12,)))
            out.append((f'{mode}-mov_ds-accessed-written', mode, ['mov ds, ax'], {'eax': 0x10}, [],
                        ('load', dict(sreg='ds', sel=0x10)), (), (FGDT >> 12,)))   # accessed already: no write, no fault
    # Real mode: the limits (the core's).
    out.append(('R-jmp_far_ptr16:32-limit', 'R', ['jmp dword 0x1000:0x10000'], {}, [], ('jmp', dict(target=(0x1000, 0x10000), osize=32)), (), ()))
    out.append(('R-jmp_far_ptr16:16', 'R', ['jmp word 0x1100:0x0'], {}, [], ('jmp', dict(target=(0x1100, 0), osize=16)), (), ()))
    out.append(('R-jmp_far_m16:32-limit', 'R', ['jmp far dword [bx]'], {'ebx': 0xFFFC}, [],
                ('jmp', dict(pointer=('ds', 0xFFFC), osize=32)), (), ()))
    out.append(('R-retf16-stack-limit', 'R', ['o16 retf'], {'esp': 0xFFFE}, [], ('ret', dict(osize=16)), (), ()))
    out.append(('R-retf-stack-limit', 'R', ['o32 retf'], {'esp': 0xFFFC}, [], ('ret', dict(osize=32)), (), ()))
    out.append(('R-retf16-offset-limit', 'R', ['o16 retf'], {}, [(G.STACK, (0xFFFF).to_bytes(2, 'little') + (0x1100).to_bytes(2, 'little'))],
                ('ret', dict(osize=16)), (), ()))
    return out


def run_model(kind, args, state):
    if kind == 'jmp': return S.jmp_far_interp(state, **args)
    if kind == 'ret': return S.ret_far_interp(state, **args)
    return S.load_interp(state, args['sreg'], args['sel'])


def segment_expectations(N):
    """(name, case, want) of every segment case: `want` as run_interp_natemu's self-tests have it (machine fields, and for
    a typed stop `resident`, `permitted` faults or a `detail`)."""
    specs = segment_cases(N)
    built = []
    for name, mode, asm, regs, mem, call, absent, readonly in specs:
        bits = G.MODES[mode]['bits']
        built.append((bits, name, mode, asm, regs, mem, call, absent, readonly))
    codes = {}
    for bits in (16, 32):
        group = [b for b in built if b[0] == bits]
        for b, code in zip(group, G.assemble([b[3] for b in group], bits)):
            codes[b[1]] = code
    out = []
    for bits, name, mode, asm, regs, mem, (kind, args), absent, readonly in built:
        code = codes[name]
        c, blocks, _ = _case(N, name, mode, asm, regs=regs, mem=mem, absent=absent, readonly=readonly, gdt=mode != 'R')
        c['code'] = {'addr': G.h(c['at']), 'data': code.hex()}
        blocks = [*blocks, (c['at'], code)]
        state = model_state(c, blocks, absent, readonly)
        state['length'] = len(code)
        r = run_model(kind, args, state)
        fall = c['at'] + len(code)
        if r['outcome'] == 'done' and kind != 'load':
            s = r['state']
            base, _ = s.get('shape', {}).get('cs', (0, 0))
            target = (base + s['rip']) & 0xffffffff
            stops = [target, fall]
        else:
            stops = [fall]
        c['stops'] = [{'id': k, 'linear': G.h(a)} for k, a in enumerate(stops)]
        start = starting_fields(c, blocks)
        if r['outcome'] == 'fault':
            want = {'resident': 'interp_fault', 'permitted': sorted({(v, cr2, err) for o, f in r['permitted'] if o == 'fault'
                                                                       for v, err, cr2 in [f]}), **start, 'stop': None}
            if ('done', None) in r['permitted']:
                want['may_complete'] = True
        elif r['outcome'] == 'refused':
            detail = OUTER if 'outer' in r['reason'] else GATE
            want = {'resident': 'interp_unsupported', 'detail': detail, **start, 'stop': None}
        else:
            s, fields = r['state'], dict(start)
            fields.update({x: s['seg'][x][0] for x in MODEL_SREGS})
            fields['esp'] = s['rsp'] & 0xffffffff
            mem_after = {a + k: b for a, d in blocks for k, b in enumerate(d)}
            for a, d in r['diff'].items():
                mem_after.update({a + k: b for k, b in enumerate(d)})
            fields['window0'] = bytes(mem_after.get(FGDT + k, 0) for k in range(FGDT_LIMIT + 1)).hex()
            want = {**fields, 'stop': 0}
        out.append((name, c, want))
    return out


# ---- modes_model's programs -----------------------------------------------------------------------------------------

PROFILES = [p for p in M.PROFILES if p not in (24, 28, 30, 34, 35, 36, 38)]


def model_ending(profile):
    """(index, vector, error, stop) where natemu's run of the profile must end (the model's first record that faults or
    stops), or None when it completes; and the records' count."""
    res = M.model(profile, 0, M.data(profile, [0] * 0x400))
    for rec in res['records']:
        if rec['vector'] or rec.get('stop'):
            return (rec['index'], rec['vector'], rec['error'], bool(rec.get('stop'))), rec['imode']
    return None, 'end'


def mode_cases(N):
    """(name, case, want) of each profile: P32, paging off, CS 08H of the model's GDT at DATA + 200H; EBX DATA (the load),
    ESI DATA + 100H (the m16:64 pointer), ECX EFER's number, ESP STACK; the model's page tables at 6000H; the code at
    SOURCE. `want`: `ending` (index, vector, error, paging stop) or None, and the program's instruction offsets."""
    out = []
    for p in PROFILES:
        ending, imode = model_ending(p)
        assert imode is not None, (p, 'ends in 64-bit code')
        prog = M.programs(p)
        code = b''.join(prog)
        offsets, at = [], 0
        for ins in prog:
            offsets.append(at); at += len(ins)
        mem = [(M.DATA, bytes(M.data(p, [0] * 0x400))), (0x6000, bytes(M.tables()))]
        regs = {'ebx': M.DATA, 'esi': M.DATA + 0x100, 'ecx': 0xC0000080, 'esp': M.STACK, 'eax': 0, 'edx': 0}
        cs = {'sel': 8, 'base': 0, 'limit': 0xffffffff, 'attr': 0xc09b}
        c = N.interp_case(f'modes-{p}', 'P32', [], regs=regs, segs={'cs': cs}, mem=mem, at=M.SOURCE)
        c['state'].update(cr0=G.h(0x10011), cr3=G.h(0x1020), cr4=G.h(0x600), efer=G.h(0),
                          gdtr={'base': G.h(M.DATA + 0x200), 'limit': G.h(0x37)})
        c['code'] = {'addr': G.h(M.SOURCE), 'data': code.hex()}
        c['stops'] = [{'id': 0, 'linear': G.h(M.SOURCE + len(code))}]
        out.append((f'modes-{p}', c, {'ending': ending, 'offsets': offsets}))
    return out


def all_cases(N):
    return segment_expectations(N) + mode_cases(N)
