#!/usr/bin/env python3
"""Independent bounded native coherence and lifetime replay.

Translation and table effects come from the independent coherence reference;
state effects come from DWORD MOV semantics. Native trace/fixture binding is
required before a report can be accepted, never replaced by reported counters.
"""
import argparse
import copy
import json
from pathlib import Path

from check_i1 import state, resident_pc
from i4v_check import exact, require, uint, guest_seed
from check_i4v_ordering import native_seed, numeric_transport
import i4v_coherence_model as model

PRIVATE = 0x800040000000
CODE = PRIVATE + 0x30000
RIP = 0x102000
DATA = 0x100000
HIGH = 0xffff800000000000
MASK = (1 << 64) - 1
PROFILES = ('parent-four', 'ad-legacy32', 'ad-pae', 'ad-four', 'size-pae', 'size-four')
MODES = ('four', 'legacy32', 'pae', 'four', 'pae', 'four')
SOURCES = {'load': bytes.fromhex('8b03'), 'store': bytes.fromhex('8903')}
# Independently disassembled finite single-MOV bodies. Producer decode bitness
# remains separately bound even where these native bytes coincide.
BODIES = {'load': bytes.fromhex('c4fcdf881f2003d5'),
          'store': bytes.fromhex('1f2003d5c4fc9f88')}
SITES = {'load': 0, 'store': 4}


def configuration(profile, n, actor):
    uint(profile, 32, 'profile'); uint(n, 32, 'participants'); uint(actor, 32, 'actor')
    require(profile < 6 and 1 <= n <= 4 and actor < n, 'finite coherence fixture')
    return MODES[profile]



def address(profile, cpu, visit):
    stage, page = divmod(visit, 2)
    return DATA + page * 4096 + cpu * 256 + (HIGH if profile in (0, 3, 5) and (cpu + stage + page) % 2 else 0)


def operation(visit):
    require(type(visit) is int and 0 <= visit < 16, 'finite visit index')
    return 'load' if visit // 2 in (4, 6) else 'store'


def token(cpu, visit):
    return 0x5c000000 | ((visit + 1) << 12) | (cpu << 4) | (visit & 1)


def packet(vm, profile, n, actor, cpu, kind):
    mode = configuration(profile, n, actor)
    require(0 <= cpu < n and kind in (0, 1) and uint(vm, 64, 'VM nonce') != 0, 'compiler owner/operation')
    source = SOURCES['load' if kind else 'store']
    return [0x493456a1, 1, vm, cpu, 1, kind + 1, profile + 1 | (n << 8) | (actor << 16),
            RIP + cpu * 256 + kind * 16, 64 if mode == 'four' else 32,
            int.from_bytes(source, 'little'), 2, 8, kind, 1, 0, 0]


def reply(vm, profile, n, actor, cpu, kind):
    result = packet(vm, profile, n, actor, cpu, kind)
    result[0] = 0x493456a2
    result[14] = int.from_bytes(BODIES['load' if kind else 'store'], 'little')
    return result


def source_image(n):
    image = [0xcc] * 4096
    for cpu in range(n):
        for kind, op in enumerate(('store', 'load')):
            at = cpu * 256 + kind * 16
            image[at:at + 2] = SOURCES[op]
    return image


def code_image(returned):
    require(resident_pc(returned), 'resident direct-return target')
    image = bytearray(32)
    for kind, op in enumerate(('store', 'load')):
        at = kind * 16
        image[at:at + 8] = BODIES[op]
        delta = returned - (CODE + at + 8)
        require(delta % 4 == 0 and -(1 << 27) <= delta < 1 << 27, 'direct return branch range')
        image[at + 8:at + 12] = (0x14000000 | ((delta // 4) & 0x3ffffff)).to_bytes(4, 'little')
    return list(image)


def canonical_seed(profile, cpu):
    frame = native_seed(cpu)
    guest = guest_seed('cold', RIP + cpu * 256)
    for gpr, native in ((0, 4), (1, 7), (2, 5), (3, 6), (4, 8), (5, 9), (6, 10), (7, 11)):
        guest['gpr'][gpr] = frame['x'][native] & (MASK if profile in (0,3,5) else 0xffffffff)
    guest['control'][0] = 0x80010011
    guest['control'][2] = 0x1020 if profile in (2, 4) else 0x1000
    guest['control'][3] = 0x600 if profile == 1 else 0x620
    guest['control'][4] = 0xd00 if profile in (0, 3, 5) else 0
    guest['segments'][0][3] = 0xa09b if profile in (0, 3, 5) else 0xc09b
    return guest



def initial_tables(profile):
    tables, context, layout = model.fixture(MODES[profile])
    if profile >= 4:
        tables[layout['parent']] = layout['second'] | 7
    if profile in (2, 4):
        tables[0x6000] = layout['first' if profile == 4 else 'second'] | 7
    return tables, context, layout

def table_image(tables, bits):
    image = bytearray(0x6000)
    for address, value in tables.items():
        at = address - 0x1000
        require(0 <= at <= len(image) - bits // 8 and at % (bits // 8) == 0, 'owned table extent')
        image[at:at + bits // 8] = value.to_bytes(bits // 8, 'little')
    return list(image)


def zero_walk():
    result = {key: 0 for key in ('address', 'intent', 'gpa', 'leaf_bytes', 'writable', 'dirty', 'user',
                                 'executable', 'wb', 'vector', 'error', 'read_count', 'write_count')}
    result.update(reads=[[0, 0] for _ in range(4)], writes=[[0, 0, 0] for _ in range(4)])
    return result


def validate_walk(observed, tables, context, address, intent):
    require(intent in ('read', 'write', 'fetch'), 'walk access type')
    outcome = model.walk(tables, context, address, intent)
    expected = zero_walk()
    expected.update(address=address, intent=('read', 'write', 'fetch').index(intent),
                    vector=outcome.vector, error=outcome.error,
                    read_count=len(outcome.reads), write_count=len(outcome.writes))
    require(len(outcome.reads) <= 4 and len(outcome.writes) <= 4, 'bounded walk trace')
    expected['reads'][:len(outcome.reads)] = [[at, tables[at]] for at in outcome.reads]
    expected['writes'][:len(outcome.writes)] = [list(item) for item in outcome.writes]
    if outcome.route:
        for key in ('gpa', 'leaf_bytes', 'writable', 'dirty', 'user', 'executable', 'wb'):
            value = getattr(outcome.route, key)
            expected[key] = int(value) if type(value) is bool else value
    exact(observed, expected, 'ordered walk effects and permissions')
    return outcome

def memory_access(image, initial, access, gpa):
    """One actual DWORD MOV: exact full-frame and whole-image effects."""
    state(initial, 'native entry')
    require(access in SOURCES, 'admitted generated memory instruction')
    require(type(image) is list and len(image) == 8192 and
            all(type(b) is int and 0 <= b <= 255 for b in image), 'two full physical data pages')
    uint(gpa, 64, 'physical operand')
    require(DATA <= gpa <= DATA + len(image) - 4 and gpa % 4 == 0,
            'owned aligned DWORD physical operand')
    result, memory = copy.deepcopy(initial), image.copy()
    offset = gpa - DATA
    if access == 'load':
        result['x'][4] = int.from_bytes(bytes(memory[offset:offset + 4]), 'little')
    else:
        memory[offset:offset + 4] = (initial['x'][4] & 0xffffffff).to_bytes(4, 'little')
    return result, memory


def exact_route(observed, route, label='resolved route'):
    require(type(observed) is dict, label + ' object')
    expected = dict(gpa=route.gpa, leaf_bytes=route.leaf_bytes,
                    leaf_address=route.leaf_address, writable=int(route.writable),
                    user=int(route.user), executable=int(route.executable),
                    dirty=int(route.dirty), wb=int(route.wb))
    exact(observed, expected, label)


def bind_requested(record, expected_profile=None, expected_n=None, expected_actor=None):
    require(type(record) is dict, 'coherence report object')
    configuration(record['profile'], record['n'], record['actor'])
    for key, value in (('profile',expected_profile),('n',expected_n),('actor',expected_actor)):
        if value is not None:
            uint(value,32,'requested '+key)
            exact(record[key],value,'requested '+key)


def check(record, expected_profile=None, expected_n=None, expected_actor=None,
          producer_directory=None):
    bind_requested(record, expected_profile, expected_n, expected_actor)
    return audit_development(record, producer_directory)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('report', type=Path)
    parser.add_argument('--profile', type=int)
    parser.add_argument('--n', type=int)
    parser.add_argument('--actor', type=int)
    parser.add_argument('--producer-directory', type=Path)
    args = parser.parse_args()
    try:
        report = check(json.loads(args.report.read_text()), args.profile, args.n,
                       args.actor, args.producer_directory)
        print(json.dumps(report, indent=2))
    except (ValueError, AssertionError, KeyError, TypeError) as error:
        print(json.dumps(dict(status='FAIL', error=str(error))))
        raise SystemExit(1)


# Replay uses trace ordering for shared table effects. Data operands occupy
# disjoint owner spans, so concurrent DWORD effects commute until the visit cut.

def zero_frame():
    return dict(x=[0] * 31, q=[[0] * 16 for _ in range(32)], sp=0, nzcv=0, fpcr=0, fpsr=0)


def zero_visit():
    scalar = ('index stage page kind address rip admitted completed faults retries esr far pc spsr resume_pc '
              'saved_gpa saved_writable saved_leaf_bytes vector error cr2_before cr2_after walk_count').split()
    out = dict.fromkeys(scalar, 0)
    out.update(initial=zero_frame(), fault=zero_frame(), captured=zero_frame(),
               native_before=[0] * 4, native_after=[0] * 4, memory_before=[0] * 4,
               memory_after=[0] * 4, walks=[zero_walk() for _ in range(6)])
    return out


def native_slot(va):
    require((va & ~HIGH) & ~0x1fff == DATA, 'admitted data subpage')
    return ((va >> 12) & 1) + (2 if va & HIGH else 0)


def native_descriptor(route):
    require(DATA <= route.gpa < DATA + 8192 and route.wb, 'owned WB native route')
    return ((0x40000000 + 0x94000 + route.gpa - DATA) & ~4095) | 0x60000000000703 | (0 if route.writable and route.dirty else 128)


class Replay:
    def __init__(self, report):
        self.report = report
        self.p, self.n, self.actor = report['profile'], report['n'], report['actor']
        self.mode = configuration(self.p, self.n, self.actor)
        self.tables, self.context, self.layout = initial_tables(self.p)
        self.memory = [0xa5] * 4096 + [0x5a] * 4096
        self.all = (1 << self.n) - 1
        self.index = self.prepared = self.done = self.compiled = self.mutations = self.acks = self.blocked = 0
        self.active = self.queued = self.head = self.retained = self.coordinator_completed = 0
        self.phase, self.servicing, self.generation = 0, MASK, 1
        self.queue = []
        self.started = self.boot_snapshot = self.pdpt_changed = False
        self.released = self.reclaimed = self.finished = self.reuse_mask = 0
        self.terminal_probe_required = True
        self.terminal_probes = 0
        self.snapshots = []
        self.frames = [native_seed(cpu) for cpu in range(self.n)]
        self.guests = [canonical_seed(self.p, cpu) for cpu in range(self.n)]
        self.leaves = [[0] * 4 for _ in range(self.n)]
        self.routes = [[None, None] for _ in range(self.n)]
        self.caches = [model.Cache() for _ in range(self.n)]
        self.visits = [[None] * 16 for _ in range(self.n)]
        self.bootwalks = [0] * self.n
        self.detached = set()
        self.retried = set()
        self.owner_completed = [0] * self.n
        self.owner_faults = [0] * self.n
        self.owner_retries = [0] * self.n
        self.entry_blocks = [0] * self.n
        self.reuse_blocks = [0] * self.n
        self.ack_leaves = [[[0] * 4 for _ in range(3)] for _ in range(self.n)]
        self.owner_acks = [0] * self.n

    def observed(self, cpu, visit=None):
        return self.report['owners'][cpu]['visits'][self.index if visit is None else visit]

    def begin(self, cpu):
        require(self.started and self.index < 16 and self.phase == 0, 'visit begins only under open admission')
        require(self.visits[cpu][self.index] is None, 'one fixture binding per owner/visit')
        v = zero_visit()
        op = operation(self.index)
        va = address(self.p, cpu, self.index)
        frame = copy.deepcopy(self.frames[cpu])
        frame['x'][6] = va
        if op == 'store':
            frame['x'][4] = token(cpu, self.index)
        self.frames[cpu] = frame
        g = self.guests[cpu]
        g['gpr'][0], g['gpr'][3] = frame['x'][4], va
        g['rip'] = RIP + cpu * 256 + (16 if op == 'load' else 0)
        v.update(index=self.index, stage=self.index // 2, page=self.index % 2,
                 kind=int(op == 'load'), address=va, rip=g['rip'], initial=copy.deepcopy(frame),
                 cr2_before=g['control'][1], cr2_after=g['control'][1])
        self.visits[cpu][self.index] = v
        return v

    def remember(self, cpu, result):
        require(result.route is not None, 'successful resolved access')
        v = self.visits[cpu][self.index]
        t = result.route
        v['saved_gpa'], v['saved_leaf_bytes'] = t.gpa, t.leaf_bytes
        v['saved_writable'] = int(t.writable and t.dirty)
        v['memory_before'] = self.memory[t.gpa - DATA:t.gpa - DATA + 4]
        self.routes[cpu][self.index % 2] = t

    def install(self, cpu, result):
        require(result.route is not None, 'successful native installation')
        self.leaves[cpu][native_slot(result.address)] = native_descriptor(result.route)
        self.caches[cpu].remember(self.context, result)

    def fault(self, cpu):
        require(self.prepared == self.all and self.active & (1 << cpu), 'fault has an admitted actual entry')
        v = self.visits[cpu][self.index]
        if v['faults']:
            return
        observed = self.observed(cpu)
        leaf = v['native_before'][native_slot(v['address'])]
        op = operation(self.index)
        require(leaf == 0 or op == 'store' and leaf & 128, 'native fault requires missing or read-only mapping')
        esr = uint(observed['esr'], 64, 'actual native syndrome')
        require(esr >> 26 == 0x25 and ((esr >> 6) & 1) == int(op == 'store')
                and esr & ((1 << 7) | (1 << 8) | (1 << 10)) == 0,
                'direct data abort with valid FAR')
        require(esr & 63 == (7 if leaf == 0 else 15), 'actual leaf translation/permission fault')
        v.update(faults=1, esr=esr, far=v['address'],
                 pc=CODE + (16 if op == 'load' else 4), spsr=0x3c4 | v['initial']['nzcv'],
                 fault=copy.deepcopy(v['initial']))
        self.owner_faults[cpu] += 1

    def walk(self, cpu, slot):
        v = self.begin(cpu) if slot == 0 else self.visits[cpu][self.index]
        require(v is not None and slot == v['walk_count'] and slot < 3, 'ordered bounded per-visit walks')
        warm = self.index // 2 in (0, 1, 3)
        if slot == 0:
            va, intent = v['rip'], 'fetch'
        elif slot == 1:
            va, intent = v['address'], 'write' if operation(self.index) == 'store' else 'read'
            if not warm:
                self.fault(cpu)
                require(not (self.p in (1, 2, 3) and self.index // 2 == 2), 'cached AD phase must not rewalk data')
        else:
            require(self.mode == 'four' and v['walks'][1]['vector'] == 0, 'only successful four-level pair walks opposite alias')
            va, intent = v['address'] ^ HIGH, 'read'
        result = validate_walk(self.observed(cpu)['walks'][slot], self.tables, self.context, va, intent)
        self.tables = result.tables
        v['walks'][slot] = copy.deepcopy(self.observed(cpu)['walks'][slot])
        v['walk_count'] += 1
        if slot == 0:
            require(result.route is not None and result.route.gpa == v['rip'] and result.route.wb, 'stable actual source fetch')
        elif result.route is not None:
            if slot == 1:
                self.remember(cpu, result)
            else:
                exact(result.route.gpa, v['saved_gpa'], 'opposite alias independently resolves same byte')
            self.install(cpu, result)
        else:
            require(slot == 1 and self.index // 2 == 7 and result.vector == 14 and result.error == 3,
                    'only terminal write permission fault')

    def snapshot(self, kind, visit):
        expected = dict(kind=kind, visit=visit, mutation=self.mutations, generation=self.generation,
                        active=self.active, queued=self.queued, servicing=self.servicing,
                        retained=self.retained, acknowledgements=self.acks)
        index = len(self.snapshots)
        exact(self.report['common']['snapshots'][index], expected, 'actual mutation/drain snapshot metadata')
        image = bytes(self.memory + table_image(self.tables, self.layout['entry_bytes'] * 8)).hex()
        exact(self.report['images'][index], image, 'full snapshot memory and tables')
        self.snapshots.append(expected)

    def mutate(self):
        number = {3: 1, 7: 2, 11: 3}.get(self.index)
        require(number == self.mutations + 1 and self.done == self.all and self.active == 0
                and self.servicing == MASK and self.head == len(self.queue), 'all old operations drain before mutation')
        self.snapshot(1, self.index)
        self.prepared = 0
        self.phase = 2
        if self.p in (1, 2, 3):
            if number <= 2:
                for page in (0, 1):
                    at = self.layout['first'] + (256 + page) * self.layout['entry_bytes']
                    self.tables[at] &= ~0x60
            else:
                self.tables[self.layout['parent']] &= ~2
        elif self.p == 0:
            self.tables[self.layout['parent']] = (self.layout['second'] | 7, self.layout['first'] | 7,
                                                  self.layout['first'] | 5)[number - 1]
        else:
            self.tables[self.layout['parent']] = (0x87, self.layout['second'] | 7, 0x85)[number - 1]
        self.mutations = number
        self.acks = self.blocked = 0
        self.generation += 1
        self.snapshot(2, self.index)

    def step(self, word):
        event, cpu = divmod(uint(word, 64, 'trace word'), 256)
        require(cpu < self.n, 'trace owner in configured set')
        bit = 1 << cpu
        require(not self.detached or event == 63 and cpu in self.detached, 'retained marker binds immediately to saved-route detach')
        if event in (0x8000, 0x8001):
            kind = event - 0x8000
            require(not self.started and kind == self.bootwalks[cpu], 'ordered boot source fetch')
            result = validate_walk(self.report['owners'][cpu]['compile_walks'][kind], self.tables,
                                   self.context, RIP + cpu * 256 + kind * 16, 'fetch')
            require(result.route is not None and result.route.gpa == result.address, 'boot source GPA')
            self.tables = result.tables
            self.bootwalks[cpu] += 1
        elif event == 60:
            require(not self.started and self.bootwalks[cpu] == 2 and not self.compiled & bit, 'compile publication exactly once')
            self.compiled |= bit
        elif event == 72:
            require(self.mode == 'pae' and cpu == self.actor and self.compiled == self.all and not self.pdpt_changed, 'PDPT perturbation after all loaded roots')
            self.snapshot(0, 0); self.boot_snapshot = True
            self.tables[0x1020] = 0x6001
            self.pdpt_changed = True
            self.snapshot(4, 0)
        elif event == 61:
            require(cpu == self.actor and self.compiled == self.all and not self.started, 'all compilers ready before actual entries')
            if not self.boot_snapshot:
                require(self.mode != 'pae', 'PAE root memory must differ before execution')
                self.snapshot(0, 0)
            self.started = True
        elif 0x1000 <= event < 0x1080:
            visit, slot = divmod(event - 0x1000, 8)
            exact(visit, self.index, 'walk current visit')
            self.walk(cpu, slot)
        elif event == 1:
            require(self.phase == 0 and not self.active & bit and self.index < 16, 'entry only while open')
            v = self.visits[cpu][self.index]
            require(v is not None and v['walk_count'] >= 1 and v['admitted'] == 0, 'source checked before one entry per visit')
            self.active |= bit; self.retained |= bit
        elif 0x2000 <= event < 0x2010:
            exact(event - 0x2000, self.index, 'prepared current visit')
            require(self.active & bit and not self.prepared & bit, 'one admitted preparation')
            v = self.visits[cpu][self.index]
            warm = self.index // 2 in (0, 1, 3)
            exact(v['walk_count'], (3 if self.mode == 'four' else 2) if warm else 1, 'pre-entry walks')
            if self.index // 2 in (1, 3):
                self.leaves[cpu][native_slot(v['address'])] = 0
            if not warm and self.routes[cpu][self.index % 2] is not None:
                t = self.routes[cpu][self.index % 2]
                v.update(saved_gpa=t.gpa, saved_leaf_bytes=t.leaf_bytes,
                         saved_writable=int(t.writable and t.dirty), memory_before=self.memory[t.gpa-DATA:t.gpa-DATA+4])
            v['admitted'] = 1; v['native_before'] = self.leaves[cpu].copy()
            self.prepared |= bit
        elif event == 2:
            require(self.index // 2 in (1, 3) and self.active & bit and not self.queued & bit and self.phase in (0, 1), 'retained fault detaches once')
            self.fault(cpu)
            self.queue.append(cpu); self.queued |= bit; self.active &= ~bit; self.phase = 1
            self.detached.add(cpu)
        elif event == 63:
            require(cpu in self.detached, 'retained-route marker follows actual detach')
            self.detached.remove(cpu)
        elif event == 67:
            require(self.phase == 1 and self.head == 0 and self.queued == self.all and self.retained == self.all and not self.reuse_mask & (1 << self.index), 'reuse blocked while old code/route references retained')
            self.reuse_mask |= 1 << self.index; self.reuse_blocks[cpu] += 1
        elif event == 3:
            require(cpu == 0 and self.active == 0 and self.phase == 1 and self.queued == self.all, 'exclusive service only after all entries detach')
            self.phase = 2
        elif event == 4:
            require(self.phase == 2 and self.servicing == MASK and self.head < len(self.queue)
                    and self.queue[self.head] == cpu, 'FIFO saved-route service')
            self.servicing = cpu
        elif event in (73, 74):
            v = self.visits[cpu][self.index]
            require(v is not None and v['faults'] == 1 and v['retries'] == 0, 'one precise retry per fault')
            if event == 74:
                require(self.servicing == cpu and self.index // 2 in (1, 3) and v['saved_writable'] == 1,
                        'retained retry uses stored old writable route')
                route = self.routes[cpu][self.index % 2]
                require(route is not None and route.gpa == v['saved_gpa'], 'saved route identity')
                self.leaves[cpu][native_slot(v['address'])] = native_descriptor(route)
            else:
                require(self.active & bit and self.index // 2 not in (1, 3, 7), 'ordinary repair belongs to active current operation')
                exact(v['walk_count'], 3 if self.mode == 'four' else 2, 'repair including independent alias walk')
            v['retries'] = 1; v['resume_pc'] = v['pc']; self.owner_retries[cpu] += 1
            self.retried.add(cpu)
        elif event == 5:
            require(self.servicing == cpu and cpu in self.retried and not self.coordinator_completed & bit,
                    'FIFO completion only after saved-route retry')
            self.coordinator_completed |= bit; self.servicing = MASK; self.head += 1
        elif 0x3000 <= event < 0x3010 or 0x4000 <= event < 0x4010:
            denied = event >= 0x4000
            exact(event - (0x4000 if denied else 0x3000), self.index, 'completion current visit')
            require(self.prepared == self.all and not self.done & bit, 'one completion after prepared barrier')
            v = self.visits[cpu][self.index]
            require(v is not None and v['saved_gpa'] != 0, 'completion bound to resolved physical route')
            if denied:
                require(self.index // 2 == 7 and v['faults'] == 1 and v['retries'] == 0
                        and v['walk_count'] == 2 and v['walks'][1]['vector'] == 14 and v['walks'][1]['error'] == 3,
                        'precise terminal write permission failure')
                v.update(vector=14, error=3, cr2_after=v['address'])
                self.guests[cpu]['control'][1] = v['address']
                final = copy.deepcopy(v['initial'])
            else:
                require(self.index // 2 != 7, 'terminal write cannot commit')
                if self.index // 2 in (1, 3):
                    require(self.coordinator_completed & bit, 'retained retry completes before guest commit')
                else:
                    require(self.active & bit, 'ordinary completion is active')
                leaf = self.leaves[cpu][native_slot(v['address'])]
                require(leaf != 0 and (operation(self.index) == 'load' or leaf & 128 == 0), 'actual native access has required shadow permissions')
                actual_gpa = ((leaf & 0x0000fffffffff000) - 0x40000000 - 0x94000 + DATA) | (v['address'] & 4095)
                exact(actual_gpa, v['saved_gpa'], 'native leaf and saved guest physical route agree')
                if self.p in (1, 2, 3) and self.index // 2 == 2:
                    cached = model.access_outcomes(self.tables, self.context, v['address'], 'write', self.caches[cpu], policy='cached')
                    require(len(cached) == 1 and cached[0].policy == 'cached' and cached[0].route.gpa == actual_gpa,
                            'cached dirty history permits write without fresh AD')
                    exact((v['faults'], v['walk_count']), (0, 1), 'cached write has no repair walk')
                final, self.memory = memory_access(self.memory, v['initial'], operation(self.index), v['saved_gpa'])
                v['completed'] = 1; self.owner_completed[cpu] += 1
                self.guests[cpu]['rip'] = v['rip'] + 2
            self.frames[cpu] = final
            self.guests[cpu]['gpr'][0], self.guests[cpu]['gpr'][3] = final['x'][4], final['x'][6]
            v['captured'] = copy.deepcopy(final)
            v['native_after'] = self.leaves[cpu].copy()
            at = v['saved_gpa'] - DATA; v['memory_after'] = self.memory[at:at + 4]
            exact(self.observed(cpu), v, 'complete visit frame, fault, tables and memory effects')
            self.active &= ~bit; self.done |= bit; self.retried.discard(cpu)
        elif event == 64:
            require(cpu == self.actor, 'designated table writer')
            self.mutate()
        elif event == 66:
            delayed = (self.actor + self.n - 1) % self.n
            require(cpu == delayed and self.phase == 2 and self.acks == self.all ^ bit and self.blocked == 0,
                    'actual entry blocked while final acknowledgement is withheld')
            self.blocked = 1; self.entry_blocks[cpu] += 1
        elif event == 65:
            require(self.phase == 2 and self.mutations > 0 and self.prepared == 0 and not self.acks & bit, 'one owner acknowledgement per mutation')
            delayed = (self.actor + self.n - 1) % self.n
            require(cpu != delayed or self.blocked == 1, 'delayed last acknowledgement follows rejected entry')
            if not (self.p in (1, 2, 3) and self.mutations == 1):
                self.leaves[cpu] = [0] * 4; self.routes[cpu] = [None, None]; self.caches[cpu].invalidate()
            self.ack_leaves[cpu][self.mutations - 1] = self.leaves[cpu].copy()
            self.owner_acks[cpu] |= 1 << (self.mutations - 1)
            self.acks |= bit
        elif event == 68:
            require(cpu == self.actor and self.done == self.all, 'visit barrier closes after all real effects')
            if self.index in (3, 7, 11):
                require(self.acks == self.all and self.blocked == 1, 'no new entry until all alias acknowledgements')
                self.snapshot(3, self.index)
            else:
                self.snapshot(1, self.index)
            self.index += 1; self.prepared = self.done = self.active = self.queued = self.head = self.coordinator_completed = 0
            self.queue = []; self.servicing = MASK; self.phase = 0
        elif event == 75:
            require(cpu == self.actor and self.index == 16 and self.released == self.all ^ bit
                    and self.retained == bit and self.active == 0 and self.reclaimed == 0
                    and self.terminal_probes == 0, 'last code pin alone blocks actual reclaim')
            self.terminal_probes = 1
        elif event == 69:
            require(cpu != self.actor or not self.terminal_probe_required or self.terminal_probes == 1, 'last pin observed before release')
            require(self.index == 16 and not self.released & bit and self.acks == self.all, 'release only after terminal effects and invalidations')
            require(cpu != self.actor or self.released == self.all ^ bit, 'actor is last code-reference release')
            self.released |= bit; self.retained &= ~bit
        elif event == 70:
            require(self.released == self.all and self.retained == 0 and self.active == 0 and not self.reclaimed & bit,
                    'actual code clearing only after every retained reference drains')
            self.reclaimed |= bit
        elif event == 71:
            require(self.reclaimed == self.all and not self.finished & bit, 'terminal success after actual code clearing')
            self.finished |= bit
        else:
            raise ValueError(f'unknown or unimplemented coherence trace event {event}')


def audit_development(record, producer_directory=None, *, terminal_probe=True):
    """Full replay. Legacy diagnostics may explicitly disable the later pin probe;
    public acceptance always requires it. No universal cache-policy claim."""
    numeric_transport(record)
    exact(set(record), set(('case n configured_vcpus tested_vcpus profile actor vm_nonce owners images source_before source_after data_before data_after tables_before tables_after common coordinator').split()), 'complete coherence report schema')
    p,n,a = record['profile'],record['n'],record['actor']
    configuration(p,n,a)
    exact(record['case'],'coherence','fixture case')
    exact((record['configured_vcpus'],record['tested_vcpus']),(n,n),'actual participant count')
    require(uint(record['vm_nonce'],64,'VM nonce') != 0,'fresh VM identity')
    exact(len(record['owners']),n,'all owner records')
    exact(record['source_before'],source_image(n),'entire original source page')
    exact(record['source_after'],record['source_before'],'immutable source page')
    tables,_,layout=initial_tables(p)
    exact(record['tables_before'],table_image(tables,layout['entry_bytes']*8),'initial full table backing')
    exact(record['data_before'],[0xa5]*4096+[0x5a]*4096,'initial full physical data')
    replay=Replay(record)
    replay.terminal_probe_required=terminal_probe
    replay.terminal_probes=0
    trace=record['coordinator']['trace']
    require(type(trace) is list and 0 < len(trace) <= 768,'bounded actual native trace')
    for word in trace:
        replay.step(word)
    require(replay.index==16 and replay.finished==replay.all and replay.reclaimed==replay.all
            and replay.released==replay.all and replay.active==replay.retained==replay.queued==0
            and not replay.detached and not replay.retried,'all effects and lifetimes terminate')
    exact(replay.reuse_mask,0xcc,'all four old-route reuse controls executed')
    exact(replay.terminal_probes,int(terminal_probe),'terminal code pin blocks actual clearing')
    exact(record['data_after'],replay.memory,'entire final physical data')
    exact(record['tables_after'],table_image(replay.tables,layout['entry_bytes']*8),'entire final table backing')
    count=24 if p in (2,4) else 23
    exact(len(replay.snapshots),count,'all semantic cuts observed')
    exact(len(record['images']),count,'no missing or extra full snapshots')
    blank_snapshot=dict.fromkeys(('kind visit mutation generation active queued servicing retained acknowledgements').split(),0)
    expected_common=dict(n=n,profile=p,actor=a,initialized=1,compiled=replay.all,index=16,prepared=0,done=0,
                         mutations=3,acknowledgements=replay.all,blocked=1,released=replay.all,reclaimed=replay.all,
                         pdpt_changed=int(p in (2,4)),reuse_blocked=0xcc,snapshot_count=count,
                         snapshots=replay.snapshots+[blank_snapshot]*(24-count))
    exact(record['common'],expected_common,'shared final state and unused snapshot slots')
    coord=record['coordinator']
    expected_coord=dict.fromkeys(('scenario alias_profile stack_profile mixed_profile mixed_compiled ready phase active queued head length completed retained new_entries new_completed new_references stale_replies stale_entries reuse_blocked translation_wait').split(),0)
    expected_coord.update(participants=n,finished=replay.all,servicing=MASK,generation=4,
                          acknowledgements=replay.all,reclaimed=1,old_eligible=1,published=1,
                          queue=[MASK]*4,event_pending=[0]*4,event_notifications=[0]*4,
                          trace_length=len(trace),trace=trace,lock=coord['lock'],next_ticket=coord['lock'])
    require(coord['lock']>0,'actual coordinator acquisitions')
    exact(coord,expected_coord,'final admission/queue/ref state')
    from i4v_check import TCR
    for cpu,owner in enumerate(record['owners']):
        requests=[packet(record['vm_nonce'],p,n,a,cpu,k) for k in (0,1)]
        replies=[reply(record['vm_nonce'],p,n,a,cpu,k) for k in (0,1)]
        code=code_image(owner['return_pc'])
        expected=dict(id=cpu,visit=16,completed=14,faults=12,retries=10,transfers=2,
                      initial=native_seed(cpu),current=replay.frames[cpu],captured=replay.frames[cpu],
                      guest_before=canonical_seed(p,cpu),guest_after=replay.guests[cpu],
                      expected=requests[1],requests=requests,replies=replies,compiler_requests=requests,
                      code=code,code_before_clear=code,code_after_clear=[0]*32,published_code=[0]*32,
                      code_before=[0]*32,native_before=[0]*4,native_leaves=replay.leaves[cpu],
                      acknowledgements=7,ack_leaves=replay.ack_leaves[cpu],entry_blocked=replay.entry_blocks[cpu],
                      reuse_blocked=replay.reuse_blocks[cpu],released=1,entry_rejected=1,
                      status=1,detail=0,current_el=4,tcr=TCR)
        hidden=dict(entries=[0x2001,0,0,0],generation=1) if p in (2,4) else dict(entries=[0]*4,generation=0)
        expected.update(hidden_before=hidden,hidden_after=hidden,hidden_loads=int(p in (2,4)),
                        hidden_reads=[0x1020,0x1028,0x1030,0x1038] if p in (2,4) else [0]*4,
                        mapping_gpa=[r.gpa for r in replay.routes[cpu]],
                        mapping_writable=[int(r.writable and r.dirty) for r in replay.routes[cpu]],
                        mapping_leaf_bytes=[r.leaf_bytes for r in replay.routes[cpu]])
        bank=[((i*37+19)&255)^cpu for i in range(512)]
        expected.update(bank_before=bank,bank_after=bank)
        if terminal_probe:
            expected.update(terminal_reuse_blocked=int(cpu==a),code_while_held=code if cpu==a else [0]*32)
        exact(set(owner),set(expected)|set(('compile_walks visits return_pc sctlr actlr capture_pc service_pc').split()), 'complete owner report schema')
        for key,value in expected.items():
            exact(owner[key],value,f'owner {cpu} {key}')
        exact((replay.owner_completed[cpu],replay.owner_faults[cpu],replay.owner_retries[cpu]),(14,12,10),'replayed totals')
        require(owner['sctlr']&0x1005==0x1005 and owner['sctlr']&2==0,'native MMU/cache/alignment state')
        require(owner['actlr']&2!=0,'native hardware TSO enabled')
        for key in ('return_pc','capture_pc','service_pc'):
            require(resident_pc(owner[key]),f'actual resident {key}')
        exact(owner['capture_pc'],owner['return_pc'],'allocated direct-return capture')
        exact(len(owner['compile_walks']),2,'two actual source fetches')
        exact(len(owner['visits']),16,'all actual visits')
        require(all(v is not None for v in replay.visits[cpu]),'no missing native visit')
        if producer_directory is not None:
            from i4v_coherence_worker import verify_compilations
            exports=verify_compilations(Path(producer_directory)/f'cpu-{cpu}',requests,replies)
            for kind,export in enumerate(exports):
                exact(export['body_hex'],BODIES['load' if kind else 'store'].hex(),'actual generated body')
                exact(export['guest_rip'],requests[kind][7],'actual producer RIP')
                exact(export['bitness'],requests[kind][8],'actual producer decode mode')
    return dict(status='PASS',profile=p,n=n,actor=a,visits=16*n,snapshots=count,
                native_faults=12*n,retained_retries=4*n,actual_compiles=2*n)


if __name__ == '__main__':
    main()
