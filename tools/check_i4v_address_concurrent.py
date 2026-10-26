#!/usr/bin/env python3
"""Independent protected32 address formation and concurrent effect oracle.

Address-size truncation, segment checks, linear addition, legacy paging and
final physical A20 masking are distinct operations. No emulator implementation
or reported output supplies their expected semantics.
"""

import argparse
import copy
import json
from pathlib import Path

from i4v_check import exact, require, uint
import check_i4
from check_i1 import resident_pc
from check_i3_paging import guest_seed
from i4v_check import TCR

EIP = 0x2000
SOURCE = bytes.fromhex("89c1890b8b02")
REPLACEMENT = bytes.fromhex("89c189138b02")
SOURCE16 = bytes.fromhex("89c1678908678b00")
REPLACEMENT16 = bytes.fromhex("89c1678910678b00")
OFFSETS = (0, 2, 4, 6)
OFFSETS16 = (0, 2, 5, 8)
PROFILES = ("subpage", "a20-off", "linear-wrap", "address16", "ds-boundary", "cs-boundary")
MASK32 = (1 << 32) - 1
NO_CPU = (1 << 64) - 1
CODE, CODE_NEXT = check_i4.CODE, check_i4.CODE_NEXT
ROOTS = (4, 7, 5, 6, 8, 9, 10, 11)
RET = bytes.fromhex("c0035fd6")
# Checked against the retained FEX exports and independent LLVM disassembly.
BODIES = {
    "copy": bytes.fromhex("e70304aa"),
    "store_ecx": bytes.fromhex("1f2003d5c7fc9f88"),
    "store_edx": bytes.fromhex("1f2003d5c5fc9f88"),
    "load_edx": bytes.fromhex("a4fcdf881f2003d5"),
    "store_ecx16": bytes.fromhex("d4000a0b943e00531f2003d587fe9f88"),
    "store_edx16": bytes.fromhex("d4000a0b943e00531f2003d585fe9f88"),
    "load16": bytes.fromhex("d4000a0b943e005384fedf881f2003d5"),
}


def configuration(profile, n, actor, cpu, terminal=False):
    require(type(profile) is int and 0 <= profile < 6 and type(n) is int and 1 <= n <= 4,
            "bounded protected32 address profile")
    require(type(actor) is int and type(cpu) is int and 0 <= actor < n and 0 <= cpu < n,
            "bounded actor/owner")
    require(type(terminal) is bool and (not terminal or profile >= 4 and cpu == actor),
            "terminal fixture applies only to the selected actor")
    paged = cpu != actor
    cs = [8, 0x400123 if paged else 0x300123,
          EIP + 2 if terminal and profile == 5 else MASK32,
          0x409B if terminal and profile == 5 else 0xC09B]
    ds = [0x10, (0x400080 if paged else 0xFFF00080) if profile == 2 else
          (0x400123 if paged else 0x200123), MASK32, 0xC093]
    old = 0 if profile == 2 else 0x200000 if profile == 3 else 0x300000
    alternate = 0x300000 if profile == 2 else old + 4096
    return {"profile": profile, "n": n, "actor": actor, "cpu": cpu, "terminal": terminal,
            "paged": paged, "a20": int(profile != 1), "cs": cs, "ds": ds,
            "address16": profile == 3, "cr3": 0x1000, "old_raw": old, "alternate_raw": alternate,
            "data_index": 0 if profile == 3 else 256,
            "ebx": 0xFFFFFFFE if terminal and profile == 4 else 0x1234FFF0 if profile == 3 else 0x100040 + cpu * 256,
            "edx": 0x33440000 + cpu if profile == 3 else 0x1000C0 + cpu * 256,
            "esi": 0x56780030 + cpu * 256,
            "identity": (profile + 1) | (n << 8) | (actor << 16) | (int(terminal) << 24) | (int(paged) << 25)}


def initial_tables(profile):
    require(type(profile) is int and 0 <= profile < 6, "address profile")
    result = {at: 0 for at in range(0x1000, 0x5000, 4)}
    result[0x1004] = 0x2007
    result[0x2008] = 0x302007
    result[0x2000 if profile == 3 else 0x2400] = (0 if profile == 2 else 0x200000 if profile == 3 else 0x300000) | 7
    return result


def body_name(profile, generation, step):
    require(0 <= profile < 6 and generation in (1, 2) and 0 <= step < 3, "finite native body")
    return ("copy" if step == 0 else ("load16" if profile == 3 else "load_edx") if step == 2 else
            ("store_ecx" if generation == 1 else "store_edx") + ("16" if profile == 3 else ""))


def code_image(profile, generation):
    image = [0] * 96
    for step in range(3):
        body = BODIES[body_name(profile, generation, step)] + RET
        image[step * 32:step * 32 + len(body)] = body
    return image


def native_frame(initial, profile, step, code, capture, value=0, fault=False):
    frame = copy.deepcopy(initial)
    frame["x"][16], frame["x"][30] = code + step * 32, capture
    if step == 0:
        require(not fault, "register-only MOV cannot have a memory fault")
        frame["x"][7] = initial["x"][4]
    else:
        if profile == 3:
            frame["x"][20] = (initial["x"][6] + initial["x"][10]) & 0xFFFF
        if step == 2 and not fault:
            frame["x"][4] = value & MASK32
    return frame


def packet(vm, context, generation, serial, step):
    uint(vm, 64, "VM identity")
    uint(serial, 64, "compiler serial")
    require(vm != 0 and serial != 0, "nonzero compiler identity")
    source = source_instruction(context["address16"], generation, step)
    eip = EIP + (OFFSETS16 if context["address16"] else OFFSETS)[step]
    linear = segment_linear(context["cs"], eip, len(source), "fetch")
    raw = 0x302123 + eip - EIP
    gpa = raw if context["a20"] else raw & ~(1 << 20)
    body = BODIES[body_name(context["profile"], generation, step)]
    return [0x49345671, 1, vm, context["cpu"], 1, generation, context["identity"], eip, 32,
            int.from_bytes(source.ljust(8, b"\0"), "little"), len(source), linear, gpa, serial, len(body), step]


def reply(vm, context, generation, serial, step):
    result = packet(vm, context, generation, serial, step)
    body = BODIES[body_name(context["profile"], generation, step)].ljust(16, b"\0")
    result[0] = 0x49345672
    result[9], result[10] = (int.from_bytes(body[i:i + 8], "little") for i in (0, 8))
    return result


def source_writer_route(context, tables):
    """The explicit fixture writer uses DS with a 32-bit offset, even for 67 data."""
    linear = (context["cs"][1] + EIP) & MASK32
    offset = (linear - context["ds"][1]) & MASK32
    width = len(SOURCE16 if context["address16"] else SOURCE)
    return resolve(tables, context["ds"], offset, width, "write",
                   context["paged"], context["a20"], context["cr3"])


class GuestFault(Exception):
    def __init__(self, vector, error, address=0):
        self.vector, self.error, self.address = vector, error, address
        super().__init__(f"guest fault vector={vector} error={error} address={address:#x}")


def source_instruction(address16, generation, step):
    require(type(address16) is bool and generation in (1, 2) and 0 <= step < 3,
            "bounded actual source identity")
    body = (SOURCE16 if generation == 1 else REPLACEMENT16) if address16 else (SOURCE if generation == 1 else REPLACEMENT)
    offsets = OFFSETS16 if address16 else OFFSETS
    return body[offsets[step]:offsets[step + 1]]


def operand_offset(registers, step, address16=False):
    require(step in (1, 2), "only store/load has a data offset")
    if address16:
        return (registers["EBX"] + registers["ESI"]) & 0xFFFF
    return registers["EBX" if step == 1 else "EDX"] & MASK32


def segment_linear(segment, offset, width, access):
    """Finite present CPL0 expand-up segments; max-limit #GP is chosen policy."""
    require(type(segment) is list and len(segment) == 4, "segment cache shape")
    selector, base, limit, attributes = segment
    for value in segment:
        uint(value, 64, "segment field")
    require(base <= MASK32 and limit <= MASK32 and 0 <= offset <= MASK32 and 1 <= width <= 8,
            "bounded 32-bit segment/offset/span")
    require(attributes & 0x90 == 0x90 and not attributes & 0x60,
            "profile requires present CPL0 code/data descriptors")
    require(access in ("fetch", "read", "write"), "access kind")
    code = bool(attributes & 8)
    require(code if access == "fetch" else not code, "profile uses CS fetch and DS data")
    # Do not wrap the last byte of an operand at the address-size boundary.
    # At maximum effective limit this is the declared deterministic GP policy.
    if offset + width - 1 > limit or (access == "write" and not attributes & 2):
        raise GuestFault(13, 0)
    return (base + offset) & MASK32


def legacy_walk(tables, cr3, linear, access):
    """Supervisor WP=1/PSE-off two-level translation, preserving exact A/D writes."""
    uint(linear, 32, "legacy linear address")
    uint(cr3, 32, "legacy CR3")
    require(access in ("fetch", "read", "write"), "walk access kind")
    require(cr3 & 0x18 == 0, "finite WB table-root profile")
    error = 2 if access == "write" else 0
    pde_address = (cr3 & ~4095) + (linear >> 22) * 4
    require(pde_address in tables, "legacy PDE outside owned table backing")
    pde = tables[pde_address]
    reads, writes, path = 1, 0, []
    if not pde & 1:
        raise GuestFault(14, error, linear)
    next_pde = pde | 32
    if next_pde != pde:
        tables[pde_address] = next_pde
        writes += 1
    path.append([pde_address, pde, next_pde])
    pte_address = (pde & 0xFFFFF000) + ((linear >> 12) & 1023) * 4
    require(pte_address in tables, "legacy PTE outside owned table backing")
    pte = tables[pte_address]
    reads += 1
    if not pte & 1:
        raise GuestFault(14, error, linear)
    if access == "write" and not pde & pte & 2:
        raise GuestFault(14, error | 1, linear)
    next_pte = pte | 32 | (64 if access == "write" else 0)
    if next_pte != pte:
        tables[pte_address] = next_pte
        writes += 1
    path.append([pte_address, pte, next_pte])
    return {"raw_gpa": (pte & 0xFFFFF000) + (linear & 4095),
            "reads": reads, "writes": writes, "path": path}


def resolve(tables, segment, offset, width, access, paged, a20, cr3=0x1000):
    require(type(paged) is bool and a20 in (0, 1), "explicit paging/A20 context")
    linear = segment_linear(segment, offset, width, access)
    route = legacy_walk(tables, cr3, linear, access) if paged else {
        "raw_gpa": linear, "reads": 0, "writes": 0, "path": []}
    raw = route["raw_gpa"]
    gpa = raw if a20 else raw & ~(1 << 20)
    require((gpa & 4095) + width <= 4096, "this profile does not admit crossing data/source spans")
    return dict(route, offset=offset, linear=linear, gpa=gpa)


def execute_instruction(registers, memory, step, generation, route=None):
    """Literal x86 MOV semantics; source generation changes only the stored value."""
    require(type(step) is int and 0 <= step < 3 and generation in (1, 2), "finite MOV instruction")
    result, after = dict(registers), dict(memory)
    if step == 0:
        result["ECX"] = registers["EAX"] & MASK32
    else:
        require(type(route) is dict and "gpa" in route, "memory operation needs an admitted physical route")
        physical = route["gpa"]
        require(all(physical + i in memory for i in range(4)), "guest access escapes owned backing")
        if step == 1:
            value = registers["ECX" if generation == 1 else "EDX"] & MASK32
            after.update(enumerate(value.to_bytes(4, "little"), physical))
        else:
            result["EAX"] = int.from_bytes(bytes(memory[physical + i] for i in range(4)), "little")
    return result, after


def architectural_step(context, registers, memory, tables, generation, step):
    """One due instruction, including fetch-before-data fault precedence."""
    expected_source = source_instruction(context["address16"], generation, step)
    offsets = OFFSETS16 if context["address16"] else OFFSETS
    eip = EIP + offsets[step]
    after_tables, after_memory, after_registers = dict(tables), dict(memory), dict(registers)
    fetch = operand = None
    try:
        fetch = resolve(after_tables, context["cs"], eip, len(expected_source), "fetch",
                        context["paged"], context["a20"], context["cr3"])
        require(all(fetch["gpa"] + i in memory for i in range(len(expected_source))),
                "instruction fetch outside owned source backing")
        actual = bytes(memory[fetch["gpa"] + i] for i in range(len(expected_source)))
        exact(actual, expected_source, "actual selected physical source bytes")
        if step:
            offset = operand_offset(registers, step, context["address16"])
            operand = resolve(after_tables, context["ds"], offset, 4, "write" if step == 1 else "read",
                              context["paged"], context["a20"], context["cr3"])
        after_registers, after_memory = execute_instruction(registers, memory, step, generation, operand)
    except GuestFault as fault:
        return {"completed": False, "rip": eip, "registers": after_registers, "memory": after_memory,
                "tables": after_tables, "fetch": fetch, "operand": operand,
                "fault": [fault.vector, fault.error, fault.address]}
    return {"completed": True, "rip": EIP + offsets[step + 1], "registers": after_registers,
            "memory": after_memory, "tables": after_tables, "fetch": fetch, "operand": operand, "fault": None}


def zero(value):
    if isinstance(value, dict):
        return {k: zero(v) for k, v in value.items()}
    if isinstance(value, list):
        return [zero(v) for v in value]
    return 0


def zero_visit():
    empty = {key: 0 for key in ('program', 'step', 'generation', 'terminal', 'entered', 'completed',
                               'esr', 'far', 'pc', 'spsr', 'service', 'continuation', 'value')}
    empty.update(request=[0] * 16, reply=[0] * 16, prefix=zero(check_i4.seed(0)[0]),
                 fault=zero(check_i4.seed(0)[0]), fetch={k: 0 for k in ('offset', 'linear', 'raw', 'gpa')},
                 access={k: 0 for k in ('offset', 'linear', 'raw', 'gpa')}, memory_before=[0] * 4,
                 memory_after=[0] * 4)
    return empty


def numeric_transport(value):
    """Reject booleans/floats and overflowing ABI numbers even inside tuples."""
    if type(value) is dict:
        for key, item in value.items():
            require(type(key) is str, 'JSON object field name')
            numeric_transport(item)
    elif type(value) is list:
        for item in value:
            numeric_transport(item)
    elif type(value) is not str:
        uint(value, 64, 'ABI number')


def seed(context):
    frame, bank, _ = check_i4.seed(context['cpu'])
    for reg in ROOTS:
        frame['x'][reg] &= MASK32
    frame['x'][4] = 0x11220000 + context['cpu']
    frame['x'][7] = 0x55660000 + context['cpu']
    # This fixture keeps the base native seed's RDI sentinel; it is not an
    # owner data pointer, unlike the earlier scalar I4 fixture.
    frame['x'][11] = 0x100080
    for reg, key in ((5, 'edx'), (6, 'ebx'), (10, 'esi')):
        frame['x'][reg] = context[key]
    guest = guest_seed('cold')
    for gpr, native in enumerate(ROOTS):
        guest['gpr'][gpr] = frame['x'][native] & MASK32
    for i in (0, 1):
        guest['ymm'][i][:16] = frame['q'][16 + i]
    guest['rip'] = EIP
    guest['control'][0] = 0x80010001 if context['paged'] else 0x10001
    guest['control'][2:5] = [0x1000, 0x600, 0]
    guest['segments'] = [context['cs'], context['ds']] + [[0x10, 0, MASK32, 0xC093] for _ in range(6)]
    return frame, bank, guest


def table_bytes(tables):
    return list(b''.join(tables[a].to_bytes(4, 'little') for a in range(0x1000, 0x5000, 4)))


def initial_memory(profile, n):
    data = [0xA5] * 4096 + [0x5A] * 4096
    if profile != 3:
        for cpu in range(n):
            offset = (0x140 if profile == 2 else 0x1E3) + cpu * 256
            for page, value in ((0, 0xA1B20000 + cpu), (1, 0xC3D40000 + cpu)):
                data[page * 4096 + offset:page * 4096 + offset + 4] = value.to_bytes(4, 'little')
    source = [0xCC] * 8192
    program = SOURCE16 if profile == 3 else SOURCE
    source[0x123:0x123 + len(program)] = program
    return data, source


def address_only(route):
    return {k: route[k] for k in ('offset', 'linear', 'gpa')} | {'raw': route['raw_gpa']}


def check_native_fault(v, frame, profile, step, code):
    exact(v['fault'], frame, 'precise native state before serviced memory operation')
    offset = (frame['x'][6] + frame['x'][10]) & 0xFFFF if profile == 3 else frame['x'][6 if step == 1 else 5]
    site = (12 if step == 1 else 8) if profile == 3 else (4 if step == 1 else 0)
    exact(v['far'], offset, 'native FAR is the effective offset, not linear/GPA')
    exact(v['pc'], code + step * 32 + site, 'actual generated memory instruction')
    esr, spsr = uint(v['esr'], 64, 'ESR'), uint(v['spsr'], 64, 'SPSR')
    exact(esr >> 26, 0x25, 'same-EL data abort')
    exact((esr >> 6) & 1, int(step == 1), 'native read/write direction')
    require(4 <= esr & 63 <= 7, 'forced missing native mapping')
    exact(esr & ((1 << 7) | (1 << 8) | (1 << 10)), 0, 'direct meaningful FAR syndrome')
    exact(spsr & 0x3DF, 0x3C4, 'masked EL1t')
    exact(spsr & 0xF0000000, frame['nzcv'], 'native flags unchanged')
    exact(v['continuation'], v['pc'] + 4, 'service resumes after exactly the faulting memory instruction')


class Replay:
    """Global trace order determines every shared byte and A/D update.

    The fixture's schedule selects *when* an operation happens. Literal x86
    semantics and address formation above determine what that operation does.
    """
    def __init__(self, record):
        numeric_transport(record)
        exact(set(record), {'case', 'profile', 'configured_vcpus', 'tested_vcpus', 'actor', 'vm_nonce',
                            'address', 'coordinator', 'maintenance', 'owners', 'source_before', 'source_after',
                            'data_before', 'data_after', 'tables_before', 'tables_after'}, 'complete address report schema')
        self.r = record
        self.n, self.profile, self.actor = record['configured_vcpus'], record['profile'], record['actor']
        configuration(self.profile, self.n, self.actor, 0)
        self.ids = set(range(self.n))
        self.all = (1 << self.n) - 1
        self.vm = uint(record['vm_nonce'], 64, 'VM identity')
        require(self.vm != 0, 'nonzero shared VM identity')
        self.cells = {c['id']: c for c in record['owners']}
        exact(set(self.cells), self.ids, 'exact configured owners')
        exact(len(record['owners']), self.n, 'no duplicate owners')
        self.ctx = [configuration(self.profile, self.n, self.actor, i) for i in range(self.n)]
        self.frames, self.guests, self.entered, self.captures = [], [], [], []
        self.tables = initial_tables(self.profile)
        self.data, self.source = initial_memory(self.profile, self.n)
        exact(record['data_before'], self.data, 'independent whole physical data seed')
        exact(record['source_before'], self.source, 'one valid active source and fully poisoned inactive page')
        exact(record['tables_before'], table_bytes(self.tables), 'exact legacy fixture')
        for cpu in range(self.n):
            cell = self.cells[cpu]
            exact(set(cell), {'id', 'vm_nonce', 'compiler_requests', 'bank_before', 'bank_after', 'old_code',
                              'new_code', 'initial', 'entered', 'final_frame', 'guest_before', 'guest_after',
                              'owner', 'visits', 'event', 'lower_leaf_before', 'lower_leaf_after',
                              'upper_root_after', 'upper_leaf_after', 'status', 'detail', 'current_el',
                              'sctlr', 'actlr', 'tcr', 'capture_pc', 'service_pc', 'esr', 'far', 'elr', 'spsr'},
                  'complete owner transport schema')
            frame, bank, guest = seed(self.ctx[cpu])
            for key, val in [('initial', frame), ('bank_before', bank), ('bank_after', bank),
                             ('guest_before', guest), ('vm_nonce', self.vm), ('status', 1), ('detail', 0)]:
                exact(cell[key], val, 'owner seed/status.' + key)
            capture = uint(cell['capture_pc'], 64, 'capture PC')
            require(resident_pc(capture) and resident_pc(cell['service_pc']), 'resident callback targets')
            self.captures.append(capture)
            self.frames.append(frame)
            self.entered.append(copy.deepcopy(frame))
            self.guests.append(guest)
            exact(len(cell['visits']), 21, 'fixed bounded visit records')
            for v in cell['visits']:
                exact(zero(v), zero_visit(), 'complete Visit transport shape')
        self.program = [0] * self.n
        self.step = [0] * self.n
        self.visits = [0] * self.n
        self.current = [None] * self.n
        self.states = ['idle'] * self.n
        self.walks, self.reads, self.writes = ([0] * self.n for _ in range(3))
        self.serial = [0] * self.n
        self.requests = [[] for _ in range(self.n)]
        self.images = [[0] * 96 for _ in range(self.n)]
        self.saved = [None] * self.n
        self.old_saved = [None] * self.n
        self.acks = [[] for _ in range(self.n)]
        self.source_ack = [None] * self.n
        self.retained_snapshot = [0] * self.n
        self.terminal_initial = [zero(f) for f in self.frames]
        self.active, self.retained, self.admitted = set(), set(), set()
        self.queue, self.old_queue, self.old_done = [], [], set()
        self.servicing = None
        self.phase = 0
        self.mutated = False
        self.source_acks, self.rejected, self.released, self.reclaimed = set(), set(), set(), set()
        self.new_started, self.new_done, self.finished = set(), set(), set()
        self.pqueue, self.pending = [], {}
        self.submitted, self.completed, self.intervals = ([0] * self.n for _ in range(3))
        self.pserial = self.total = self.batch = self.ordinary_total = self.depth = 0
        self.head = self.ordinary = None
        self.packs = set()
        self.history, self.leaves = [], []
        self.ring = [{'cpu': NO_CPU, 'serial': 0} for _ in range(4)]
        self.ring_head = 0
        self.compose_leaf = None
        self.terminal_ready, self.terminal_done, self.terminal_queued = set(), set(), set()
        self.terminal_queue, self.terminal_order = [], []
        self.terminal_service = None
        self.event_delivered, self.expired = set(), set()
        self.event_guests, self.event_boundaries = {}, {}
        self.completed_instructions = self.faults = self.services = 0

    def route(self, cpu, segment, offset, width, access):
        c = self.ctx[cpu]
        result = resolve(self.tables, c['cs' if segment == 0 else 'ds'], offset, width, access,
                         c['paged'], c['a20'])
        self.walks[cpu] += int(c['paged'])
        self.reads[cpu] += result['reads']
        self.writes[cpu] += result['writes']
        return address_only(result)

    def physical_index(self, gpa, source=False):
        c = self.ctx[0]
        mask = lambda a: a if c['a20'] else a & ~(1 << 20)
        bases = ([mask(0x302000), 0x202000 if c['a20'] else 0x302000] if source else
                 [mask(c['old_raw']), mask(c['alternate_raw'])])
        for page, base in enumerate(bases):
            if base <= gpa < base + 4096:
                return page * 4096 + gpa - base
        raise ValueError('resolved address outside explicitly owned physical backing')

    def regs(self, cpu):
        return {name: self.frames[cpu]['x'][reg] for name, reg in
                [('EAX', 4), ('ECX', 7), ('EDX', 5), ('EBX', 6), ('ESI', 10)]}

    def canonical(self, cpu, offset):
        for i, native in enumerate(ROOTS):
            self.guests[cpu]['gpr'][i] = self.frames[cpu]['x'][native] & MASK32
        self.guests[cpu]['rip'] = EIP + offset

    def open_visit(self, cpu):
        require(self.current[cpu] is None and self.visits[cpu] < 21, 'one bounded due instruction')
        p, step = self.program[cpu], self.step[cpu]
        if p == 0:
            require(cpu in self.admitted and not self.mutated, 'old entry requires live old reference')
            require(cpu in self.active or self.servicing == cpu, 'old instruction has active/saved-route authority')
        elif p == 1:
            exact(self.reclaimed, self.ids, 'all old private slots reclaimed before replacement admission')
            if cpu not in self.new_started:
                self.new_started.add(cpu)
                self.active.add(cpu)
                self.phase = 0
        elif p < 6:
            require(self.ordinary == cpu and self.head is None and self.phase == 0,
                    'ordinary instruction within its retained grant')
        else:
            exact(self.terminal_ready, self.ids, 'all terminal contexts bound before wave execution')
            require(cpu in self.active or self.terminal_service == cpu, 'terminal entry retains authority')
        v = self.cells[cpu]['visits'][self.visits[cpu]]
        exact((v['program'], v['step'], v['generation'], v['terminal']),
              (p, step, 1 if p == 0 else 2, int(self.ctx[cpu]['terminal'])), 'visit identity')
        self.visits[cpu] += 1
        self.current[cpu] = v
        offsets = OFFSETS16 if self.profile == 3 else OFFSETS
        self.canonical(cpu, offsets[step])
        self.states[cpu] = 'due'
        return v

    def fetch(self, cpu):
        v = self.open_visit(cpu)
        step, generation = self.step[cpu], 1 if self.program[cpu] == 0 else 2
        source = source_instruction(self.profile == 3, generation, step)
        route = self.route(cpu, 0, self.guests[cpu]['rip'], len(source), 'fetch')
        exact(v['fetch'], route, 'segmented fetch offset/linear/raw/GPA')
        at = self.physical_index(route['gpa'], True)
        exact(self.source[at:at + len(source)], list(source), 'actual physical instruction bytes')
        if step:
            offset = operand_offset(self.regs(cpu), step, self.profile == 3)
            try:
                segment_linear(self.ctx[cpu]['ds'], offset, 4, 'write' if step == 1 else 'read')
            except GuestFault:
                require(self.profile == 4 and cpu == self.actor and self.program[cpu] == 6 and step == 1,
                        'unexpected operand segment fault')
                self.states[cpu] = 'gp'
                return
        self.states[cpu] = 'fetched'

    def enter(self, cpu, step):
        exact((self.step[cpu], self.states[cpu]), (step, 'fetched'), 'entry after successful current fetch/segment guard')
        v, p = self.current[cpu], self.program[cpu]
        generation, context = 1 if p == 0 else 2, self.ctx[cpu]
        if p <= 1 or context['terminal']:
            self.serial[cpu] += 1
            req = packet(self.vm, context, generation, self.serial[cpu], step)
            exact(v['request'], req, 'full resident compiler identity')
            exact(v['reply'], reply(self.vm, context, generation, self.serial[cpu], step), 'actual delivered native body')
            self.requests[cpu].append(req)
            start = step * 32
            exact(self.images[cpu][start:start + 32], [0] * 32, 'no overwrite of an already published instruction')
            body = BODIES[body_name(self.profile, generation, step)] + RET
            self.images[cpu][start:start + len(body)] = body
            if p == 0 and step == 0:
                self.serial[cpu] += 1
                self.requests[cpu].append(packet(self.vm, context, 1, self.serial[cpu], step))
        else:
            exact(v['request'], [0] * 16, 'cache reuse must not invent compilation')
            exact(v['reply'], [0] * 16, 'cache reuse must not invent delivery')
        code = CODE if p == 0 else CODE_NEXT
        frame = copy.deepcopy(self.frames[cpu])
        frame['x'][16], frame['x'][30] = code + step * 32, self.captures[cpu]
        self.entered[cpu] = frame
        exact(v['entered'], 1, 'actual native instruction entry')
        self.states[cpu] = 'entered'

    def saved_routes(self, cpu):
        exact((self.step[cpu], self.states[cpu]), (1, 'entered'), 'retained access after real store entry')
        store = self.route(cpu, 1, operand_offset(self.regs(cpu), 1, self.profile == 3), 4, 'write')
        load = self.route(cpu, 1, operand_offset(self.regs(cpu), 2, self.profile == 3), 4, 'read')
        self.saved[cpu] = (store, load)
        if self.program[cpu] == 0:
            self.old_saved[cpu] = (store, load)

    def service(self, cpu, step):
        exact((self.step[cpu], self.states[cpu]), (step, 'entered'), 'one service for one actual entered memory instruction')
        require(step in (1, 2), 'register MOV cannot be serviced')
        p, v = self.program[cpu], self.current[cpu]
        if p == 0:
            require(self.servicing == cpu and self.phase == 2, 'old saved route only under FIFO service')
        elif p == 6:
            require(cpu != self.actor and self.terminal_service == cpu and self.actor in self.terminal_done,
                    'terminal peer service follows persistent actor fault')
        else:
            require(cpu in self.active and self.phase == 0 and self.head is None, 'service under ordinary admission')
        generation = 1 if p == 0 else 2
        code = CODE if p == 0 else CODE_NEXT
        fault = native_frame(self.frames[cpu], self.profile, step, code, self.captures[cpu], fault=True)
        check_native_fault(v, fault, self.profile, step, code)
        if p == 0 or p == 6:
            require(self.saved[cpu] is not None, 'retained route missing')
            route = self.saved[cpu][step - 1]
        else:
            route = self.route(cpu, 1, operand_offset(self.regs(cpu), step, self.profile == 3), 4,
                               'write' if step == 1 else 'read')
        exact(v['access'], route, 'mode-specific saved/current access route')
        at = self.physical_index(route['gpa'])
        before = self.data[at:at + 4]
        value = self.frames[cpu]['x'][7 if generation == 1 else 5] & MASK32 if step == 1 else int.from_bytes(bytes(before), 'little')
        exact(v['memory_before'], before, 'physical bytes before exact-once service')
        if step == 1:
            self.data[at:at + 4] = value.to_bytes(4, 'little')
        exact(v['memory_after'], self.data[at:at + 4], 'physical bytes after exact-once service')
        exact((v['value'], v['service']), (value, 1), 'service value and count')
        self.states[cpu] = 'serviced'
        self.faults += 1
        self.services += 1

    def complete(self, cpu, step):
        exact((self.step[cpu], self.states[cpu]), (step, 'entered' if step == 0 else 'serviced'),
              'completion after actual entry/service')
        v, p = self.current[cpu], self.program[cpu]
        if step == 0:
            for key in ('fault', 'access', 'esr', 'far', 'pc', 'spsr', 'service', 'continuation', 'value', 'memory_before', 'memory_after'):
                exact(v[key], zero(v[key]), 'no fabricated register-only memory activity.' + key)
        self.frames[cpu] = native_frame(self.frames[cpu], self.profile, step, CODE if p == 0 else CODE_NEXT,
                                        self.captures[cpu], v['value'] if step else 0)
        exact(v['prefix'], self.frames[cpu], 'full committed native instruction prefix')
        exact(v['completed'], 1, 'one committed instruction')
        self.canonical(cpu, (OFFSETS16 if self.profile == 3 else OFFSETS)[step + 1])
        self.current[cpu] = None
        self.step[cpu] += 1
        self.states[cpu] = 'done' if step == 2 else 'idle'
        self.completed_instructions += 1

    def ack(self, cpu):
        route = self.route(cpu, 1, operand_offset(self.regs(cpu), 1, self.profile == 3), 4, 'read')
        return [2 + self.total + int(self.head is not None), route['offset'], route['linear'], route['raw'], route['gpa'], 0]

    def pressure_finished(self):
        return self.total == 9 * self.n and self.batch == 0 and self.ordinary is None and self.head is None

    def terminal_fault(self, cpu):
        require(self.profile >= 4 and cpu == self.actor and self.program[cpu] == 6 and self.step[cpu] == 1,
                'only actor terminal second instruction faults')
        exact(self.terminal_queued, self.ids - {self.actor}, 'all peers retain real memory faults before actor outcome')
        if self.profile == 5:
            v = self.open_visit(cpu)
            try:
                segment_linear(self.ctx[cpu]['cs'], EIP + 2, 2, 'fetch')
            except GuestFault as fault:
                exact((fault.vector, fault.error), (13, 0), 'instruction-span limit fault')
            else:
                raise ValueError('terminal CS fixture did not fault')
            exact(v['fetch'], zero(v['fetch']), 'fetch fault has no physical route')
        else:
            exact(self.states[cpu], 'gp', 'DS span fault before compilation or entry')
            v = self.current[cpu]
        for key in ('request', 'reply', 'prefix', 'fault', 'access', 'entered', 'completed', 'esr', 'far', 'pc',
                    'spsr', 'service', 'continuation', 'value', 'memory_before', 'memory_after'):
            exact(v[key], zero(v[key]), 'terminal fault has no uncommitted effects.' + key)
        self.terminal_done.add(cpu)
        self.active.remove(cpu)
        self.states[cpu] = 'gp-final'

    def trace(self, words):
        require(type(words) is list and 0 < len(words) <= 768, 'bounded actual trace')
        for word in words:
            uint(word, 64, 'trace word')
            event, cpu = word >> 8, word & 255
            require(cpu in self.ids, 'trace references configured owner')
            if event == 1:
                require(cpu not in self.admitted and self.phase == 0, 'one initial admission per owner')
                self.admitted.add(cpu); self.active.add(cpu); self.retained.add(cpu)
            elif event == 60:
                self.fetch(cpu)
            elif 64 <= event <= 66:
                self.enter(cpu, event - 64)
            elif 68 <= event <= 70:
                self.complete(cpu, event - 68)
            elif event in (73, 74):
                self.service(cpu, event - 72)
            elif event == 2:
                exact(self.admitted, self.ids, 'ready barrier before any retained fault')
                require(cpu in self.active and cpu not in self.old_queue and not self.mutated, 'detach exactly one admitted old store')
                if cpu == self.actor:
                    exact(set(self.old_queue), self.ids - {cpu}, 'selected actor detaches last')
                self.saved_routes(cpu)
                self.active.remove(cpu); self.queue.append(cpu); self.old_queue.append(cpu); self.phase = 1
            elif event == 3:
                require(cpu == 0 and not self.active and self.phase == 1 and set(self.queue) == self.ids,
                        'exclusive old service only after all admissions drain')
                self.phase = 2
            elif event == 4:
                require(self.phase == 2 and self.servicing is None and self.queue, 'one FIFO service owner')
                exact(cpu, self.queue[0], 'retained route FIFO order')
                self.servicing = cpu
            elif event == 5:
                require(self.servicing == cpu and self.step[cpu] == 3 and self.states[cpu] == 'done', 'old program fully completes before release')
                self.old_done.add(cpu); self.queue.pop(0); self.servicing = None
            elif event == 80:
                exact(self.old_done, self.ids, 'no shared mutation until every admitted old operation completes')
                require(cpu == 0 and not self.active and self.servicing is None and not self.mutated, 'single closed shared mutation')
                s = self.r['address']
                exact(s['source_leaf_before'], self.tables[0x2008], 'source PTE before fixture write')
                c = self.ctx[cpu]
                delta = 4 if self.profile == 3 else 3
                linear = (c['cs'][1] + EIP + delta) & MASK32
                route = self.route(cpu, 1, (linear - c['ds'][1]) & MASK32, 1, 'write')
                exact(s['source_write'], route, 'explicit DS32 source writer route')
                exact(s['source_before'], self.source[0x123:0x12B], 'source before byte replacement')
                self.source[self.physical_index(route['gpa'], True)] = 0x10 if self.profile == 3 else 0x13
                exact(s['source_after'], self.source[0x123:0x12B], 'source after exact replacement')
                exact(s['source_leaf_after'], self.tables[0x2008], 'only paged source writer sets D')
                index = 0x2000 if self.profile == 3 else 0x2400
                exact(s['data_leaf_before'], self.tables[index], 'data PTE after drained accesses')
                self.tables[index] = c['alternate_raw'] | 7
                exact(s['data_leaf_after'], self.tables[index], 'raw unmasked remap PTE')
                self.mutated = True
            elif event == 81:
                require(self.mutated and cpu not in self.source_acks and not self.active, 'single post-mutation acknowledgement')
                if cpu == (self.actor + self.n - 1) % self.n:
                    exact(self.source_acks, self.ids - {cpu}, 'delayed source ack last')
                self.source_ack[cpu] = self.ack(cpu); self.source_acks.add(cpu)
            elif event == 82:
                exact(self.source_acks, self.ids, 'all peers acknowledge before stale reply/entry attempt')
                require(cpu not in self.rejected and cpu in self.retained, 'stale checks retain old code reference')
                self.rejected.add(cpu)
            elif event == 83:
                require(cpu in self.rejected and cpu in self.retained and cpu not in self.released, 'release follows actual stale checks')
                exact(self.r['address']['code_before'][cpu], code_image(self.profile, 1), 'old native code retained until reference release')
                self.retained_snapshot[cpu] = sum(1 << i for i in self.retained)
                self.retained.remove(cpu); self.released.add(cpu)
            elif event == 84:
                require(cpu in self.released and cpu not in self.reclaimed, 'private physical code reuse follows its reference release')
                exact(self.r['address']['code_after'][cpu], [0] * 96, 'actual old private code cleared')
                self.reclaimed.add(cpu); self.images[cpu] = [0] * 96
                self.program[cpu], self.step[cpu], self.states[cpu] = 1, 0, 'idle'
            elif event == 85:
                require(self.program[cpu] == 1 and self.step[cpu] == 3 and cpu not in self.new_done,
                        'new program completes once')
                self.active.discard(cpu); self.new_done.add(cpu)
            elif event in (20, 22):
                require(self.phase == 0 and self.step[cpu] == 3 and self.states[cpu] == 'done',
                        'event handoff/expiry at committed OPEN boundary')
                self.active.discard(cpu)  # completion releases before the immediately adjacent event attempt
                if event == 22:
                    require(cpu in (1, 2) and cpu not in self.expired, 'explicit shadow/priority expiry once')
                    self.expired.add(cpu)
                else:
                    require(cpu not in self.event_delivered and (cpu not in (1, 2) or cpu in self.expired),
                            'eligible event delivery once, after shadow/priority expiry')
                    self.event_delivered.add(cpu)
                    self.event_guests[cpu] = copy.deepcopy(self.guests[cpu])
                    self.event_boundaries[cpu] = self.visits[cpu]
            elif event == 23:
                exact(self.new_done, self.ids, 'pressure after all replacement programs')
                if self.compose_leaf is None:
                    self.compose_leaf = self.tables[0x2000 if self.profile == 3 else 0x2400]
                require(cpu not in self.pending and cpu != self.ordinary and self.submitted[cpu] < 9,
                        'one bounded pending maintenance request per owner')
                self.pserial += 1; self.submitted[cpu] += 1
                self.pending[cpu] = self.pserial
                self.ring[(self.ring_head + len(self.pqueue)) % self.n] = {'cpu': cpu, 'serial': self.pserial}
                self.pqueue.append((cpu, self.pserial)); self.depth = max(self.depth, len(self.pending))
                require(len(self.pending) <= self.n, 'bounded pending storage')
            elif event == 25:
                require(self.head is None and self.ordinary is None and self.pqueue and self.batch < 8 and not self.active,
                        'exclusive maintenance follows all admitted instructions and bounded batch')
                self.head = self.pqueue.pop(0); self.ring_head = (self.ring_head + 1) % self.n
                exact(cpu, self.head[0], 'maintenance FIFO')
                self.packs = set(); self.phase = 2
                index = 0x2000 if self.profile == 3 else 0x2400
                c = self.ctx[0]
                raw = self.tables[index] & ~4095
                exact(raw in (c['old_raw'], c['alternate_raw']), True, 'known current raw PTE route')
                self.tables[index] = (c['alternate_raw'] if raw == c['old_raw'] else c['old_raw']) | 7
            elif event == 26:
                require(self.head is not None and cpu not in self.packs and not self.active, 'one ack per head after drain')
                if cpu == (3 + self.total) % self.n:
                    exact(self.packs, self.ids - {cpu}, 'delayed maintenance acknowledgement last')
                self.acks[cpu].append(self.ack(cpu)); self.packs.add(cpu)
            elif event == 27:
                require(self.head is not None, 'head completion requires current request')
                exact(self.packs, self.ids, 'all owner contexts acknowledge remap')
                owner, serial = self.head
                exact(self.pending.pop(owner), serial, 'complete the original pending identity')
                self.history.append({'cpu': owner, 'serial': serial}); self.completed[owner] += 1
                self.leaves.append(self.tables[0x2000 if self.profile == 3 else 0x2400])
                self.total += 1; self.batch += 1; self.head = None; self.phase = 1
            elif event == 29:
                require(self.head is None and self.ordinary is None and cpu not in self.pending and 1 <= self.batch <= 8,
                        'ordinary progress within bounded maintenance batch')
                exact(cpu, self.ordinary_total % self.n, 'round-robin ordinary service')
                require(self.intervals[cpu] < 4 and not self.active, 'bounded retained ordinary slot')
                self.ordinary = cpu; self.phase = 0; self.active.add(cpu)
                self.program[cpu], self.step[cpu], self.states[cpu] = 2 + self.intervals[cpu], 0, 'idle'
            elif event == 30:
                require(self.ordinary == cpu and self.step[cpu] == 3 and self.states[cpu] == 'done',
                        'entire generated ordinary program completes without replay')
                self.active.discard(cpu); self.intervals[cpu] += 1; self.ordinary_total += 1
                self.ordinary = None; self.batch = 0
            elif event == 90:
                require(self.profile >= 4 and self.pressure_finished() and cpu not in self.terminal_ready,
                        'terminal fixture begins only after completed real pressure')
                if not self.terminal_ready:
                    exact(self.r['address']['terminal_data_before'], self.data, 'actual before-terminal whole data')
                    exact(self.r['address']['terminal_tables_before'], table_bytes(self.tables), 'actual before-terminal tables')
                self.program[cpu], self.step[cpu], self.states[cpu] = 6, 0, 'idle'
                if cpu == self.actor:
                    self.ctx[cpu] = configuration(self.profile, self.n, self.actor, cpu, True)
                    self.frames[cpu]['x'][6] = self.ctx[cpu]['ebx']
                    self.guests[cpu]['segments'][0] = self.ctx[cpu]['cs']
                    self.images[cpu] = [0] * 96
                self.canonical(cpu, 0)
                self.terminal_initial[cpu] = copy.deepcopy(self.frames[cpu])
                self.terminal_ready.add(cpu); self.active.add(cpu); self.phase = 0
            elif event == 92:
                require(self.program[cpu] == 6 and cpu != self.actor and cpu in self.active and cpu not in self.terminal_queued,
                        'terminal peers retain one real store fault')
                self.saved_routes(cpu); self.active.remove(cpu)
                self.terminal_queue.append(cpu); self.terminal_order.append(cpu); self.terminal_queued.add(cpu)
            elif event == 91:
                self.terminal_fault(cpu)
            elif event == 93:
                require(self.actor in self.terminal_done and self.terminal_service is None and self.terminal_queue,
                        'terminal saved peer service after actor fault publication')
                exact(cpu, self.terminal_queue[0], 'terminal retained-route FIFO')
                self.terminal_service = cpu
            elif event == 95:
                require(self.terminal_service == cpu and self.step[cpu] == 3 and self.states[cpu] == 'done',
                        'terminal peer completes its original program exactly once')
                self.terminal_done.add(cpu); self.terminal_queue.pop(0); self.terminal_service = None
            elif event == 94:
                require(self.pressure_finished() and cpu not in self.finished and not self.active, 'stop after all retained work drains')
                if self.profile >= 4:
                    exact(self.terminal_done, self.ids, 'actor fault packet survives all peer completions')
                self.finished.add(cpu)
            else:
                raise ValueError(f'unknown address concurrency event {event}')

    def finish(self):
        exact(self.finished, self.ids, 'every configured owner completed')
        exact(self.event_delivered, self.ids, 'each identified event delivered exactly once')
        require(not self.active and not self.pending and not self.pqueue and not self.queue and not self.retained,
                'no admitted access, pending request or retired code reference leaks')
        exact(self.submitted, [9] * self.n, 'nine actual maintenance submissions per owner')
        exact(self.completed, [9] * self.n, 'nine actual maintenance completions per owner')
        require(all(self.intervals) and self.ordinary_total * 8 >= self.total, 'bounded ordinary progress')
        exact(self.depth, self.n, 'all pending storage exercised')
        exact(self.r['data_after'], self.data, 'entire physical memory, including unowned bytes')
        exact(self.r['source_after'], self.source, 'physical source mutation and inactive poison preserved')
        exact(self.r['tables_after'], table_bytes(self.tables), 'every legacy table byte and exact A/D effects')
        m = self.r['maintenance']
        expected_m = {'n': self.n, 'ready': self.all, 'serial': self.pserial, 'head': self.ring_head,
                      'length': 0, 'pending': [0] * 4, 'submitted': self.submitted + [0] * (4 - self.n),
                      'completed': self.completed + [0] * (4 - self.n), 'epoch': 2 + self.total,
                      'acknowledgements': self.all, 'total': self.total, 'batch': 0,
                      'next_ordinary': self.ordinary_total % self.n, 'ordinary': NO_CPU,
                      'intervals': self.intervals + [0] * (4 - self.n), 'ordinary_total': self.ordinary_total,
                      'closing': 1, 'finished': 1, 'duplicate_rejections': self.pserial, 'maximum_depth': self.n,
                      'leaf_history': self.leaves + [0] * (36 - self.total), 'compose_leaf': self.compose_leaf,
                      'current': {'cpu': NO_CPU, 'serial': 0}, 'queue': self.ring,
                      'request_history': self.history + [{'cpu': NO_CPU, 'serial': 0}] * (36 - self.total)}
        exact(m, expected_m, 'full maintenance FIFO/history/ack/fairness state')
        c = self.r['coordinator']
        trace = c['trace']
        expected_c = {'participants': self.n, 'scenario': 5, 'alias_profile': 0, 'stack_profile': 0,
                      'mixed_profile': 0, 'mixed_compiled': 0, 'ready': self.all, 'finished': self.all,
                      'phase': self.phase, 'active': 0, 'queued': self.all,
                      'head': self.n, 'length': self.n, 'servicing': NO_CPU, 'completed': self.all,
                      'retained': 0, 'generation': 2, 'acknowledgements': 0, 'new_entries': 0,
                      'new_completed': 0, 'new_references': 0, 'reclaimed': 1, 'old_eligible': 0,
                      'published': 2, 'stale_replies': 0, 'stale_entries': 0, 'reuse_blocked': 0,
                      'translation_wait': 0, 'trace_length': len(trace), 'trace': trace,
                      'queue': self.old_queue + [NO_CPU] * (4 - self.n),
                      'event_pending': [2] * self.n + [0] * (4 - self.n),
                      'event_notifications': [2] * self.n + [0] * (4 - self.n)}
        require(uint(c['lock'], 64, 'serving lock ticket') > 0, 'coordinator lock used')
        exact(c['next_ticket'], c['lock'], 'all queued coordinator locks released')
        exact({k: v for k, v in c.items() if k not in ('lock', 'next_ticket')}, expected_c, 'complete coordinator final state')
        s = self.r['address']
        expected_s = {'n': self.n, 'profile': self.profile, 'actor': self.actor, 'compiled': 0,
                      'old_done': self.all, 'source_changed': 1, 'acknowledgements': self.all,
                      'new_done': self.all, 'maintenance_started': 1,
                      'terminal_ready': self.all if self.profile >= 4 else 0,
                      'terminal_done': self.all if self.profile >= 4 else 0,
                      'terminal_queued': self.all ^ (1 << self.actor) if self.profile >= 4 else 0,
                      'terminal_queue': self.terminal_order + [0] * (4 - len(self.terminal_order)),
                      'terminal_length': len(self.terminal_order), 'terminal_head': len(self.terminal_order),
                      'terminal_servicing': NO_CPU if self.profile >= 4 else 0,
                      'released': self.all, 'reclaimed': self.all}
        exact(set(s), set(expected_s) | {'source_write', 'source_before', 'source_after', 'source_leaf_before',
                                        'source_leaf_after', 'data_leaf_before', 'data_leaf_after', 'code_before',
                                        'code_after', 'terminal_tables_before', 'terminal_data_before'},
              'complete shared address state schema')
        for key, value in expected_s.items():
            exact(s[key], value, 'shared address state.' + key)
        for cpu in range(self.n, 4):
            exact(s['code_before'][cpu], [0] * 96, 'unused code-before owner')
            exact(s['code_after'][cpu], [0] * 96, 'unused code-after owner')
        if self.profile < 4:
            exact(s['terminal_tables_before'], [0] * 16384, 'no fabricated terminal table snapshot')
            exact(s['terminal_data_before'], [0] * 8192, 'no fabricated terminal data snapshot')
        for cpu in range(self.n):
            cell, ctx, o = self.cells[cpu], self.ctx[cpu], self.cells[cpu]['owner']
            original_ctx = configuration(self.profile, self.n, self.actor, cpu)
            initial, _, initial_guest = seed(original_ctx)
            terminal = int(ctx['terminal'])
            scalar = {'enabled': 1, 'id': cpu, 'mode': int(ctx['paged']), 'context': ctx['identity'],
                      'generation': 2, 'serial': 7 + terminal, 'program': self.program[cpu],
                      'step': 1 if terminal else 2, 'visits': self.visits[cpu], 'current_visit': self.visits[cpu] - 1,
                      'terminal': terminal, 'code_context': ctx['identity'],
                      'walks': self.walks[cpu], 'reads': self.reads[cpu], 'writes': self.writes[cpu],
                      'stale_rejected': 1, 'wrong_owner_rejected': 1, 'entry_rejected': 1 | (2 if terminal else 0),
                      'retained_before_release': self.retained_snapshot[cpu], 'reclaimed': 1,
                      'vector': 13 if terminal else 0, 'error': 0,
                      'fault_address': 0xFFFFFFFE if terminal and self.profile == 4 else 0,
                      'fault_rip': EIP + 2 if terminal else 0}
            exact(set(o), set(scalar) | {'initial', 'current', 'guest_before', 'guest_after', 'terminal_initial',
                                        'expected', 'deferred', 'stale_reply', 'saved_store', 'saved_load',
                                        'source_ack', 'maintenance_acks'}, 'complete owner state schema')
            for key, value in scalar.items():
                exact(o[key], value, 'owner final.' + key)
            exact(o['initial'], initial, 'resident original native seed')
            exact(o['current'], self.frames[cpu], 'resident current full native state')
            exact(o['guest_before'], initial_guest, 'resident original canonical state')
            exact(o['guest_after'], self.guests[cpu], 'resident canonical state including terminal prefix')
            exact(o['terminal_initial'], self.terminal_initial[cpu], 'explicit terminal fixture rebind only')
            exact(o['expected'], self.requests[cpu][-1], 'last actual resident compiler request')
            exact(o['deferred'], packet(self.vm, original_ctx, 1, 2, 0), 'retained actual old compiler job')
            exact(o['stale_reply'], reply(self.vm, original_ctx, 1, 2, 0), 'old job actually delivered with unchanged identity')
            exact(o['saved_store'], self.saved[cpu][0], 'retained store physical route')
            exact(o['saved_load'], self.saved[cpu][1], 'retained load physical route')
            exact(o['source_ack'], self.source_ack[cpu], 'context-specific source-remap acknowledgement')
            exact(o['maintenance_acks'], self.acks[cpu] + [[0] * 6 for _ in range(36 - self.total)],
                  'every owner remap acknowledgement and physical route')
            exact(cell['compiler_requests'], self.requests[cpu], 'all and only actual producer requests')
            exact(cell['entered'], self.entered[cpu], 'last actual native entry frame')
            exact(cell['final_frame'], self.frames[cpu], 'captured final native state')
            exact(cell['guest_after'], self.guests[cpu], 'full canonical state, CR2 and unrelated banks preserved')
            exact(cell['old_code'], [0] * 96, 'retired native code physically blank')
            exact(cell['new_code'], self.images[cpu], 'published code matches actual producing bodies')
            for v in cell['visits'][self.visits[cpu]:]:
                exact(v, zero_visit(), 'unused visit contains no invented execution')
            event = cell['event']
            require(uint(event['wait_checks'], 64, 'compiler event checks') > 0, 'actual compiler wait checks pending event')
            exact({k: v for k, v in event.items() if k != 'wait_checks'},
                  {'consumed': 1, 'deliveries': 1, 'boundary': self.event_boundaries[cpu],
                   'blocked': 1 | (8 if cpu == 1 else 16 if cpu == 2 else 0),
                   'shadow': 0, 'priority': 0, 'packet': self.event_guests[cpu]},
                  'one coalesced event with exact canonical committed prefix')
            exact(cell['current_el'], 4, 'EL1 execution')
            exact(cell['tcr'], TCR, 'admitted translation configuration')
            require(uint(cell['sctlr'], 64, 'SCTLR') & 0x1005 == 0x1005, 'MMU and caches enabled')
            exact(cell['sctlr'] & 2, 0, 'unaligned subpage operations admitted')
            require(uint(cell['actlr'], 64, 'ACTLR') & 2, 'TSO profile enabled')
            for key in ('lower_leaf_before', 'lower_leaf_after', 'upper_root_after', 'upper_leaf_after'):
                exact(cell[key], [0] * 512, 'native memory offsets remain deliberately unmapped.' + key)
            # Shared diagnostic registers are not populated by this path; actual
            # native syndromes are retained per Visit and checked above.
            for key in ('esr', 'far', 'elr', 'spsr'):
                exact(cell[key], 0, 'unused Shared diagnostic.' + key)
        return {'owners': self.n, 'visits': sum(self.visits), 'instructions': self.completed_instructions,
                'native_faults': self.faults, 'services': self.services, 'compilations': sum(map(len, self.requests)),
                'walks': sum(self.walks), 'table_reads': sum(self.reads), 'table_writes': sum(self.writes),
                'maintenance_heads': self.total, 'ordinary_programs': self.ordinary_total,
                'events': len(self.event_delivered), 'trace_events': len(trace)}


def audit_development(record, producer_directory=None):
    exact(record['case'], 'address-concurrent', 'address report kind')
    exact(record['tested_vcpus'], record['configured_vcpus'], 'actual configured count')
    model = Replay(record)
    model.trace(record['coordinator']['trace'])
    result = model.finish()
    if producer_directory is not None:
        from i4v_address_worker import verify_compilations
        for cpu in range(model.n):
            requests = model.requests[cpu]
            deliveries = [reply(model.vm,
                                configuration(model.profile, model.n, model.actor, cpu, bool(p[6] & (1 << 24))),
                                p[5], p[13], p[15]) for p in requests]
            verify_compilations(Path(producer_directory) / f'cpu-{cpu}', requests, deliveries)
    return result


def check(record, expected_vcpus=None, expected_profile=None, expected_actor=None, producer_directory=None):
    require(type(record) is dict, "expected concurrent address report")
    require(all(k in record for k in ('configured_vcpus', 'profile', 'actor')), 'missing requested address identity')
    configuration(record['profile'], record['configured_vcpus'], record['actor'], 0)
    for field, expected in (('configured_vcpus', expected_vcpus), ('profile', expected_profile), ('actor', expected_actor)):
        if expected is not None:
            uint(expected, 32, 'requested ' + field)
            exact(record[field], expected, 'requested ' + field)
    return audit_development(record, producer_directory)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reports", nargs="+", type=Path)
    parser.add_argument('--vcpus', type=int)
    parser.add_argument('--profile', type=int)
    parser.add_argument('--actor', type=int)
    args = parser.parse_args()
    for path in args.reports:
        check(json.loads(path.read_text()), args.vcpus, args.profile, args.actor)
        print(f"{path}: PASS")


if __name__ == "__main__":
    main()
