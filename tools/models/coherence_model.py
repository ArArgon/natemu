"""Bounded independent translation/coherence reference for the admitted modes.

Legacy32 is PSE-off; PAE and four-level support 4KiB/2MiB, 40-bit physical
addresses and the existing WB fixture. Fault-path A updates use the selected
permitted eager-parent/permission-before-leaf policy, not a universal CPU trace.
Without invalidation both a legitimate cached hit and a fresh/internal walk are
permitted. Explicit invalidation requires a fresh walk. This model is preparation
for native tests, not evidence that any native execution or shootdown occurred.

Declared policy and CPU profile (contracts §3 items 4 and 12), not SDM requirements; natemu's walker
(`crates/abi/src/paging.rs`) makes the same choices:
- LEAF_A_ON_FAULT (False): a permission-, NX- or SMEP-faulting access sets A on the parents it walked but not on the leaf
  (SDM §4.8 does not say whether a faulting access sets the leaf's A; True models the other permitted choice).
- MAXPHYADDR (40): four-level entry bits 51:MAXPHYADDR are reserved (RSVD fault).
- No 1 GiB pages: PS in a PDPTE (or PML4E) is reserved; the CPUID profile must report Page1GB = 0.
- Memory types: a route carries its leaf's PAT index (PAT, PCD, PWT; SDM Vol.3 §14.12.3 Table 14-11); `route_type`
  turns it into the architectural effective type from IA32_PAT and the MTRRs (§14.5.2.2 Table 14-7, §14.11.4.1).
  Non-leaf PWT/PCD and CR3 PWT/PCD never contribute. No route is refused for its type: WB, WT, WP and WC RAM run
  natively in natemu, UC RAM keeps strong ordering (each access runs its instruction alone between full barriers; owner
  decision, November 2); the translation is the same for every type. Device pages are separate and not modelled here.
The legacy32 and PAE modes serve the boot phase only (owner decision 3).
"""
from dataclasses import dataclass, field
from typing import Optional

PAGE = 4096
PA_MASK = 0x000000fffffff000
A, D, P, RW, US, PS, NX = 32, 64, 1, 2, 4, 128, 1 << 63
MODES = ('legacy32', 'pae', 'four')
LEAF_A_ON_FAULT = False
MAXPHYADDR = 40
# Memory-type encodings (SDM Vol.3 Tables 14-8 and 14-10); UC- exists only as a PAT entry.
UC, WC, WT, WP, WB, UC_MINUS = 0, 1, 4, 5, 6, 7
MTRR_TYPES, PAT_TYPES = (UC, WC, WT, WP, WB), (UC, WC, WT, WP, WB, UC_MINUS)
PAT_RESET = 0x0007040600070406     # Table 14-12: WB, WT, UC-, UC, WB, WT, UC-, UC
CR3_MEMORY_TYPE = 0x18             # CR3.PWT/PCD: accepted, never part of a page's type


def require(condition, message):
    if not condition:
        raise ValueError(message)


@dataclass(frozen=True)
class HiddenPAE:
    entries: tuple = (0, 0, 0, 0)
    generation: int = 0


@dataclass(frozen=True)
class Context:
    mode: str
    cr3: int
    generation: int = 1
    cpl: int = 0
    wp: bool = True
    nxe: bool = False
    smep: bool = False
    hidden: HiddenPAE = HiddenPAE()


@dataclass(frozen=True)
class Route:
    gpa: int
    leaf_bytes: int
    leaf_address: int
    writable: bool
    user: bool
    executable: bool
    dirty: bool
    pat_index: int


@dataclass
class Result:
    policy: str
    tables: dict
    route: Optional[Route] = None
    vector: int = 0
    error: int = 0
    address: int = 0
    reads: list = field(default_factory=list)
    writes: list = field(default_factory=list)


def load_hidden(tables, cr3, old=HiddenPAE()):
    """Return (new hidden snapshot, exact four reads); failed loads publish none."""
    require(type(cr3) is int and 0 <= cr3 <= 0xffffffff, 'PAE CR3 width')
    addresses = [(cr3 & ~31) + i * 8 for i in range(4)]
    require(all(address in tables for address in addresses), 'owned PDPT backing')
    entries = tuple(tables[address] for address in addresses)
    require(all(type(e) is int and 0 <= e < 1 << 64 for e in entries), '64-bit PDPT entry')
    if any(e & P and e & ~(PA_MASK | 0xe19) for e in entries):
        return None, addresses  # #GP(0); caller retains the entire old snapshot.
    return HiddenPAE(entries, old.generation + 1), addresses


def _validate(context, address, access):
    require(context.mode in MODES and context.cpl in (0, 3) and context.generation > 0, 'admitted context')
    require(type(address) is int and 0 <= address < 1 << 64, 'linear address width')
    require(access in ('read', 'write', 'fetch'), 'access intent')
    require(context.cr3 >= 0, 'CR3 width')
    if context.mode == 'legacy32':
        require(context.cr3 <= 0xffffffff and not context.nxe and not context.smep, 'admitted legacy control bits')
    elif context.mode == 'pae':
        require(context.hidden.generation > 0 and len(context.hidden.entries) == 4, 'loaded hidden PAE root')
    else:
        require(context.cr3 & ~(PA_MASK | CR3_MEMORY_TYPE) == 0, 'aligned four-level root')


def _fault_error(context, access):
    return (2 if access == 'write' else 0) | (4 if context.cpl == 3 else 0) | (
        16 if access == 'fetch' and (context.nxe or context.smep) else 0)


def walk(tables, context, address, access):
    """Fresh walk, with full resulting table words and ordered read/write effects."""
    _validate(context, address, access)
    result = Result('fresh', dict(tables), address=address)
    base_error = _fault_error(context, access)

    def fault(extra=0):
        result.vector, result.error = 14, base_error | extra
        return result

    def read(at, bits):
        require(at in result.tables, 'walker reached unowned table backing')
        value = result.tables[at]
        require(type(value) is int and 0 <= value < 1 << bits, 'paging entry width')
        result.reads.append(at)
        return value

    def update(at, value):
        old = result.tables[at]
        if value != old:
            result.writes.append((at, old, value))
            result.tables[at] = value

    if context.mode != 'four' and address > 0xffffffff:
        raise ValueError('protected32 address formation precedes this reference')
    if context.mode == 'four' and address >> 48 != (0xffff if address & (1 << 47) else 0):
        result.vector = 13
        return result
    writable = user = executable = True
    pat_index = 0
    if context.mode == 'legacy32':
        base, levels, bits = context.cr3 & ~4095, (22, 12), 32
    elif context.mode == 'pae':
        root = context.hidden.entries[address >> 30]
        if not root & P:
            return fault()
        base, levels, bits = root & PA_MASK, (21, 12), 64
    else:
        base, levels, bits = context.cr3 & PA_MASK, (39, 30, 21, 12), 64
    for shift in levels:
        at = base + ((address >> shift) & (1023 if bits == 32 else 511)) * (bits // 8)
        entry = read(at, bits)
        if not entry & P:
            return fault()
        large = bits == 64 and shift == 21 and bool(entry & PS)
        if bits == 64:
            reserved = ((1 << 52) - (1 << MAXPHYADDR)) if context.mode == 'four' else 0x7fffff0000000000
            if (entry & reserved or (not context.nxe and entry & NX)
                    or (shift >= 30 and entry & PS) or (large and entry & 0x1fe000)):
                return fault(9)
        writable = writable and bool(entry & RW)
        user = user and bool(entry & US)
        executable = executable and not bool(bits == 64 and entry & NX)
        leaf = shift == 12 or large
        if leaf:
            # Table 14-11 index: PAT is bit 7 of a 4KiB PTE, bit 12 of a larger-page entry; PCD/PWT are bits 4/3.
            pat_index = (bool(entry & (4096 if large else PS)) << 2) | (bool(entry & 16) << 1) | bool(entry & 8)
            if ((context.cpl == 3 and not user)
                    or (access == 'write' and not writable and (context.wp or context.cpl == 3))
                    or (access == 'fetch' and (not executable or (context.cpl == 0 and context.smep and user)))):
                if LEAF_A_ON_FAULT:
                    update(at, entry | A)
                return fault(1)
            update(at, entry | A | (D if access == 'write' else 0))
            size = 1 << 21 if large else PAGE
            frame = entry & (0xfffff000 if bits == 32 else PA_MASK) & ~(size - 1)
            result.route = Route(frame | (address & (size - 1)), size, at,
                                 writable or (context.cpl == 0 and not context.wp), user,
                                 executable, bool(entry & D or access == 'write'), pat_index)
            return result
        update(at, entry | A)
        base = entry & (0xfffff000 if bits == 32 else PA_MASK)
    raise ValueError('walker exhausted without leaf')


def pat_type(pat, index):
    """PAT entry `index` (0-7) of an IA32_PAT value; a reserved encoding or non-zero upper bits would #GP at WRMSR."""
    require(type(pat) is int and 0 <= pat < 1 << 64 and 0 <= index < 8, 'PAT value and index')
    require(all(pat >> 8 * i & 0xff in PAT_TYPES for i in range(8)), 'PAT entry encodings (WRMSR would #GP)')
    return pat >> 8 * index & 0xff


def _fixed_field(address):
    """(fixed MTRR index, byte number) of Table 14-9 for a physical address below 1 MiB."""
    if address < 0x80000:
        return 0x250, address >> 16
    if address < 0xc0000:
        return 0x258 + ((address - 0x80000) >> 17), address >> 14 & 7
    return 0x268 + ((address - 0xc0000) >> 15), address >> 12 & 7


def mtrr_type(mtrr, address):
    """MTRR type at a physical address (SDM Vol.3 §14.11.4.1) from {MSR index: value}; None where the SDM leaves it
    undefined (overlapping variable ranges other than identical, any with UC, or exactly WT+WB). An empty state is
    the reset state (E clear). Variable pairs are 0x200..0x20f; the model's CPU profile has 8."""
    require(type(address) is int and 0 <= address < 1 << MAXPHYADDR, 'physical address width')
    default = mtrr.get(0x2ff, 0)
    require(default & ~0xcff == 0 and default & 0xff in MTRR_TYPES, 'IA32_MTRR_DEF_TYPE encoding')
    if not default & 0x800:                                  # E clear: UC for everything, FE ignored
        return UC
    if address < 0x100000 and default & 0x400:               # FE: fixed ranges win below 1 MiB
        register, byte = _fixed_field(address)
        found = mtrr.get(register, 0) >> 8 * byte & 0xff
        require(found in MTRR_TYPES, 'fixed-range MTRR type encoding')
        return found
    found = set()
    for n in range(8):
        base, mask = mtrr.get(0x200 + 2 * n, 0), mtrr.get(0x201 + 2 * n, 0)
        if mask & 0x800 and (address ^ base) & mask & PA_MASK == 0:
            require(base & 0xff in MTRR_TYPES, 'variable-range MTRR type encoding')
            found.add(base & 0xff)
    if not found:
        return default & 0xff
    if len(found) == 1:
        return next(iter(found))
    if UC in found:
        return UC
    return WT if found == {WT, WB} else None


# Table 14-7, rows = MTRR type, columns = PAT entry in the order UC UC- WC WT WB WP; effective types (UC1/UC2 are both UC).
_PAT_ORDER = (UC, UC_MINUS, WC, WT, WB, WP)
_TABLE_14_7 = {UC: (UC, UC, WC, UC, UC, UC), WC: (UC, WC, WC, UC, WC, UC), WT: (UC, UC, WC, WT, WT, WP),
               WB: (UC, UC, WC, WT, WB, WP), WP: (UC, WC, WC, WT, WP, WP)}


def effective_type(mtrr_memory_type, pat_memory_type):
    """Table 14-7 (normal caching): None when the MTRR type is None (undefined overlap)."""
    require(pat_memory_type in PAT_TYPES and (mtrr_memory_type is None or mtrr_memory_type in MTRR_TYPES),
            'memory type encodings')
    if mtrr_memory_type is None:
        return None
    return _TABLE_14_7[mtrr_memory_type][_PAT_ORDER.index(pat_memory_type)]


def route_type(route, pat=PAT_RESET, mtrr=None, address=None):
    """Architectural effective memory type of a route's page at physical `address` (default: the route's GPA).
    The translation does not depend on it; natemu runs WB/WT/WP/WC RAM natively and UC RAM strongly ordered (M2 addendum 2)."""
    address = route.gpa if address is None else address
    return effective_type(mtrr_type(mtrr or {}, address), pat_type(pat, route.pat_index))


@dataclass(frozen=True)
class CacheEntry:
    context: Context
    linear_page: int
    route: Route


class Cache:
    """Finite native 4KiB shadow history, even for a larger guest leaf."""
    def __init__(self):
        self.entries = {}

    def remember(self, context, result):
        require(result.policy == 'fresh' and result.route is not None and result.vector == 0,
                'cache entry requires a successful actual walk history')
        page = result.address & ~4095
        self.entries[(context, page)] = CacheEntry(context, page, result.route)

    def invalidate(self, start=None, size=None):
        if start is None:
            require(size is None, 'global invalidation has no range')
            self.entries.clear()
            return
        require(type(start) is int and start >= 0 and start % PAGE == 0
                and type(size) is int and size > 0 and size % PAGE == 0, 'page-aligned invalidation range')
        self.entries = {key: entry for key, entry in self.entries.items()
                        if not start <= entry.linear_page < start + size}


def access_outcomes(tables, context, address, access, cache=None, policy='either', invalidated=False):
    """Enumerate the bounded cached/fresh choices; explicit invalidation wins.

    `fresh` labels eager internal rewalking; it is a permitted implementation
    policy, not a requirement merely because software cleared memory A/D.
    A cached clean translation cannot fabricate dirty history for a first write.
    """
    _validate(context, address, access)
    require(policy in ('either', 'cached', 'fresh'), 'declared cache policy')
    result = []
    entry = cache.entries.get((context, address & ~4095)) if cache is not None else None
    if entry is not None and not invalidated and policy != 'fresh':
        route = entry.route
        allowed = ((context.cpl == 0 or route.user)
                   and (access != 'write' or route.writable and route.dirty)
                   and (access != 'fetch' or route.executable and not (context.cpl == 0 and context.smep and route.user)))
        if allowed:
            current = Route((route.gpa & ~4095) | (address & 4095), route.leaf_bytes, route.leaf_address,
                            route.writable, route.user, route.executable, route.dirty, route.pat_index)
            result.append(Result('cached', dict(tables), route=current, address=address))
    if policy != 'cached' or not result:
        result.append(walk(tables, context, address, access))
    return result


def check_observation(observed, permitted):
    require(any(observed == candidate for candidate in permitted),
            'observed translation/effects are outside the declared cache history and invalidation policy')
    return observed


def fixture(mode):
    """Independent small A/B table topology; both PTs retain the source GPA."""
    require(mode in MODES, 'fixture mode')
    bits = 32 if mode == 'legacy32' else 64
    tables = {address: 0 for address in range(0x1000, 0x7000, bits // 8)}
    if mode == 'legacy32':
        tables[0x1000] = 0x2007
        first, second, parent = 0x2000, 0x3000, 0x1000
        context = Context(mode, 0x1000)
    elif mode == 'pae':
        tables[0x1020] = 0x2001
        tables[0x2000] = 0x3007
        first, second, parent = 0x3000, 0x4000, 0x2000
        hidden, _ = load_hidden(tables, 0x1020)
        context = Context(mode, 0x1020, hidden=hidden)
    else:
        tables[0x1000] = tables[0x1000 + 256 * 8] = 0x2007
        tables[0x2000], tables[0x3000] = 0x3007, 0x4007
        first, second, parent = 0x4000, 0x5000, 0x3000
        context = Context(mode, 0x1000, nxe=True)
    for pt, frames in ((first, (0x100000, 0x101000)), (second, (0x101000, 0x100000))):
        for index, frame in ((256, frames[0]), (257, frames[1]), (258, 0x102000)):
            tables[pt + index * (bits // 8)] = frame | 7
    return tables, context, dict(parent=parent, first=first, second=second, entry_bytes=bits // 8)
