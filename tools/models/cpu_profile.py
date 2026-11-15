"""Loader, consistency validator and queries for the presented CPU (`cpu_profile.json`).

The JSON is the accepted virtual CPU of INV-03-01 CLM-03-01-16 (with CLM-03-01-17's compatibility registers); every
entry carries its source. This module adds no values: it parses them, checks that they agree with each other (a feature
bit with no MSR, an XSAVE leaf that disagrees with XCR0, a kernel MSR site that would #GP) and answers the questions
runners and models ask (`cpuid`, `msr_write_ok`, `cr4_valid_mask`, ...). Python 3.9, no emulator imports.
"""
import json
import pathlib
import re

PATH = pathlib.Path(__file__).with_name('cpu_profile.json')
MASK64 = (1 << 64) - 1
MTRR_TYPES = (0, 1, 4, 5, 6)           # UC, WC, WT, WP, WB (SDM Vol.3 11.11.2.1)
PAT_TYPES = MTRR_TYPES + (7,)          # plus UC-  (SDM Vol.3 11.12.2)
RULES = {'canonical', 'reserved_gp', 'apic_base_extd_needs_en', 'ucode_low32_zero', 'misc_enable', 'mtrr_type_low_byte',
         'mtrr_type_all_bytes', 'pat_type_all_bytes', 'efer', 'always_gp', 'always_gp_on_write', 'lapic_owner'}
# ---- the feature catalogue's judgement (cpu-features plan sec. 2.1) -------------------------------------------------------
# Arm features the host provides on every supported machine: fexc feature names, copied from the plan's appendix
# ("the host floor", the M1, M2 and M4 in common, ECV left out). The source of truth moves to `admission::FEATURES`' floor flags (chunk 2);
# a host-crate test will tie that table to this set. NOT in it: i8mm, ecv, afp, rpres, wfxt.
FLOOR = frozenset({'atomics_lse', 'flagm', 'flagm2', 'fcma', 'crc32', 'aes', 'sha', 'pmull128', 'frintts', 'dotprod', 'lrcpc',
                   'lrcpc2'})
# Every `host` item must be one of fexc's Arm feature names (tools/fexc/fexc.cpp FeatureTable), read from there.
FEXC = pathlib.Path(__file__).resolve().parents[1] / 'fexc/fexc.cpp'
ARM_FEATURES = frozenset(n for n in re.findall(r'FEAT\(\w+, "(\w+)", (?:true|false)\)', FEXC.read_text())
                         if not n.startswith('x86_'))
STATES = ('advertised', 'clear', 'dynamic')      # 'not built' is `built: false` on a clear entry, not a state
HOST_CLASSES = ('required', 'exactness', 'speed')
CONSUMERS = ('shim', 'system', 'resident')           # who may test a feature's generated constant (plan sec. 2.3)
FIELD_KEYS = {'name', 'state', 'built', 'requires', 'host', 'msrs', 'cr4', 'subleaf', 'resident', 'gates', 'layout', 'source',
              'rule', 'exceptions', 'simd_fp', 'tests', 'baseline'}
ROOT = pathlib.Path(__file__).resolve().parents[2]
# `exceptions` (plan sec. 2.1): the SDM exception classes of the feature's instructions. `none`: no instruction of its own
# (paging, MSR or platform behaviour); `GP`: general-purpose instructions (their own Exceptions sections); `SYS`: system or
# state-management instructions (privilege, #UD gating, memory operands); `X87`; `MMX`; `SSE Type n` (legacy SIMD, SDM Vol.2A
# 2.4-2.8) and `VEX Type n` (VEX-encoded: Vol.2A 2.8).
EXCEPTION_CLASS = re.compile(r'none|GP|SYS|X87|MMX|(?:SSE|VEX) Type [1-9]\d?')
# The corpora that deliver and judge #XM (an unmasked SIMD floating-point exception, before the destination is written).
XM_TESTS = frozenset({'tools/run_i5c.py'})
# x86-64-v1..v3 (x86-64 psABI, "x86-64 Microarchitecture Levels"), by catalogue name; the baseline's own SYSCALL and the
# dynamic OSXSAVE are not capability bits and are left out.
V3 = ('CMOV', 'CX8', 'FPU', 'FXSR', 'MMX', 'SSE', 'SSE2', 'CX16', 'LAHF', 'POPCNT', 'SSE3', 'SSE4_1', 'SSE4_2', 'SSSE3', 'AVX',
      'AVX2', 'BMI1', 'BMI2', 'F16C', 'FMA', 'LZCNT', 'MOVBE', 'XSAVE')


def _i(x):
    return int(x, 16) if isinstance(x, str) else x


def uc_pairs(lo, hi):
    """[lo, hi) as power-of-two sized, size-aligned (base, size) blocks: what one variable MTRR pair can describe."""
    out = []
    while lo < hi:
        size = 1 << (hi - lo).bit_length() - 1
        while lo % size: size >>= 1
        out.append((lo, size)); lo += size
    return out


def canonical(v, bits=48):
    return v < 1 << (bits - 1) or v >= (1 << 64) - (1 << (bits - 1))


class Profile:
    def __init__(self, d):
        self.d = d
        self.leaves = {}
        for e in d['cpuid']:
            self.leaves[(_i(e['leaf']), e['subleaf'])] = e
        self.names = {k: {int(b): n for b, n in v.items()} for k, v in d['feature_names'].items()}
        self._msr = {}
        for e in d['msrs']:
            for i in range(_i(e['index']), _i(e.get('index_last', e['index'])) + 1):
                self._msr[i] = e
        self.absent = {}
        for e in d['absent_msrs']:
            for i in range(_i(e['index']), _i(e.get('index_last', e['index'])) + 1):
                self.absent[i] = e

    def _field_word(self, e, r):
        """Static word of a `fields` register key: 'ecx', or 'sub.reg' (a subleaf with no entry of its own: zero)."""
        if '.' not in r: return _i(e[r])
        sub, reg = r.split('.')
        x = self.leaves.get((_i(e['leaf']), int(sub)))
        return _i(x[reg]) if x else 0

    def features(self):
        """The catalogue: {name: its `fields` entry plus leaf, sub, reg, bit and the resolved `built`}. `built` defaults to
        true for an advertised or dynamic feature and false for a clear one."""
        out = {}
        for e in self.d['cpuid']:
            for r, fs in e.get('fields', {}).items():
                for b, f in fs.items():
                    sub = int(r.split('.')[0]) if '.' in r else e['subleaf']
                    out.setdefault(f['name'], []).append(dict(f, leaf=_i(e['leaf']), sub=sub, reg=r.split('.')[-1], bit=int(b), remote='.' in r,
                                                              built=f.get('built', f['state'] != 'clear')))
        return out

    # ---- features -------------------------------------------------------------------------------------------------
    def _reg(self, key):
        """Static register value for a names key such as '1.edx', '7.0.ebx', '13.1.eax', '0x80000001.ecx'."""
        parts = key.split('.')
        reg = parts[-1]
        leaf = int(parts[0], 0)
        sub = int(parts[1], 0) if len(parts) == 3 else None
        e = self.leaves.get((leaf, sub))
        if e is None and sub is None:
            e = self.leaves.get((leaf, 0))
        return _i(e[reg]) if e else 0

    def feature(self, name):
        """True if the static CPUID bit `leaf[.sub].reg.NAME` (e.g. '7.0.ebx.BMI2') is advertised (dynamic bits: False)."""
        key, bit = name.rsplit('.', 1)
        for b, n in self.names[key].items():
            if n == bit:
                return bool(self._reg(key) >> b & 1)
        raise KeyError(name)

    # ---- CPUID ----------------------------------------------------------------------------------------------------
    def cpuid(self, leaf, sub=0, *, n=1, vcpu=0, cr4=0, apic_en=True, misc_enable=0x1801, mode64=True, xcr0=1):
        """(eax, ebx, ecx, edx) of CPUID(leaf, sub) for vCPU `vcpu` of `n`, in the given dynamic state."""
        idn = self.d['identity']
        max_basic = _i(idn['max_basic_leaf_limited'] if misc_enable >> 22 & 1 else idn['max_basic_leaf'])
        l = leaf & 0xFFFFFFFF
        if l == 0x40000000:
            pass
        elif l >= 0x40000000 and l < 0x50000000:
            l = max_basic                                   # OPEN: records say only "no further hypervisor leaves"
        elif l >= 0x80000000:
            if l > _i(idn['max_ext_leaf']): l = max_basic   # Intel: data of the highest basic leaf
        elif l > max_basic:
            l = max_basic
        sub &= 0xFFFFFFFF
        key_sub = None if (l, None) in self.leaves else sub
        if (l, key_sub) not in self.leaves:
            if l == 0xB and sub >= 2: key_sub = 2
            else: return 0, 0, 0, 0                         # hole or beyond the last subleaf (4, 7, 0xD)
        e = self.leaves[(l, key_sub)]
        eax, ebx, ecx, edx = (_i(e[r]) for r in ('eax', 'ebx', 'ecx', 'edx'))
        if l == 0:
            eax = max_basic
        elif l == 1:
            ebx = ebx & 0xFFFF | (vcpu & 0xFF) << 24 | (n & 0xFF) << 16
            ecx |= (cr4 >> 18 & 1) << 27
            edx |= (apic_en << 9) | ((n > 1) << 28)
        elif l == 4 and eax & 31:                             # a null cache type (the terminator) has EAX = 0 (SDM Vol.2A CPUID leaf 4)
            eax |= ((n - 1) << 26) | ((n - 1) << 14 if sub == 3 else 0)
        elif l == 0xB:
            edx = vcpu
            if sub == 1: eax, ebx = (n - 1).bit_length(), n
            if sub >= 2: ecx = sub & 0xFF
        elif l == 0xD and (sub == 0 or (sub == 1 and eax & 2)):     # 0xD.1 EBX: XSAVEC's size for XCR0 (no XSAVES)
            ebx = 832 if xcr0 >> 2 & 1 else 576
        elif l == 0x80000001:
            edx |= mode64 << 11
        return eax, ebx, ecx, edx

    # ---- control registers ----------------------------------------------------------------------------------------
    def _mask(self, reg, only_valid=True):
        return sum(1 << v['bit'] for v in self.d['control'][reg]['bits'].values() if v['valid'] or not only_valid)

    def cr0_valid_mask(self): return self._mask('cr0')
    def cr4_valid_mask(self): return self._mask('cr4')

    def efer_valid_mask(self):
        """Bits a WRMSR may set: SCE, LME, NXE, and LMA (accepted, ignored)."""
        return self._mask('efer')

    def xcr0_ok(self, v):
        return v in {_i(x) for x in self.d['control']['xcr0']['valid_values']}

    def mtrr_initial(self, choice):
        return self.d['parameters']['mtrr_initial']['choices'][choice]

    def mtrr_state(self, ram_limit=None):
        """{msr index: value} of the selected initial MTRR state (owner decision Oct 28: enabled, RAM WB, devices UC).
        `ram_limit` L (default: the profile's; the VM's guest RAM [0, L), a 2 MiB multiple up to 3 GiB, M1) moves the
        UC aperture [L, 4 GiB): default type WB and [L, 4 GiB) as UC pairs (`uc_pairs`). When those need more pairs than
        IA32_MTRRCAP.VCNT (L with many low bits, e.g. 1 GiB + 2 MiB: ten), the same map in the other encoding: default
        type UC and [0, L) as WB pairs, the fewer of L's power-of-two blocks or one block to the next power of two with
        [L, it) carved out by UC pairs (UC wins an overlap, SDM Vol.3 11.11.4.1). The fixed ranges do not depend on L."""
        m = self.d['parameters']['mtrr_initial']
        if m['selected'] != 'enabled': raise ValueError('only the "enabled" initial state carries values')
        L = _i(m['ram_limit_L']) if ram_limit is None else ram_limit
        st = {_i(k): _i(v) for k, v in m['msr_values_for_default_L'].items() if _i(k) >= 0x250 or _i(k) == 0x2ff}
        vcnt = _i(self.msr(0xfe)['reset']) & 0xff
        pairs = [(base, size, 0) for base, size in uc_pairs(L, 1 << 32)]
        if len(pairs) > vcnt:
            st[0x2ff] &= ~0xff                                          # default type UC
            top = 1 << (L - 1).bit_length()
            blocks = [(base, size, 6) for base, size in uc_pairs(0, L)]
            carved = [(0, top, 6)] + [(base, size, 0) for base, size in uc_pairs(L, top)]
            pairs = min(blocks, carved, key=len)
            assert len(pairs) <= vcnt, hex(L)
        for i, (base, size, t) in enumerate(pairs):
            st[0x200 + 2 * i] = base | t
            st[0x201 + 2 * i] = ~(size - 1) & 0xFFFFFFF000 | 0x800
        return st

    def memory_type(self, state, addr):
        """MTRR memory type at physical `addr` for a state from mtrr_state (SDM Vol.3 11.11.4.1: UC wins over any overlap,
        then WT over WB; fixed ranges override variable ones below 1 MiB when FE=1)."""
        d = state.get(0x2ff, 0)
        if not d >> 11 & 1: return 0
        if addr < 0x100000 and d >> 10 & 1:
            if addr < 0x80000: reg, sh = 0x250, addr >> 16
            elif addr < 0xC0000: reg, sh = 0x258 + (addr >> 17 & 1), addr >> 14 & 7
            else: reg, sh = 0x268 + (addr - 0xC0000 >> 15), addr >> 12 & 7
            return state[reg] >> 8 * sh & 0xFF
        hits = []
        for i in range(8):
            b, m = state.get(0x200 + 2 * i, 0), state.get(0x201 + 2 * i, 0)
            if m >> 11 & 1 and addr & m & ~0xFFF == b & m & ~0xFFF: hits.append(b & 0xFF)
        if 0 in hits: return 0
        if 4 in hits: return 4
        return hits[0] if hits else d & 0xFF

    # ---- MSRs -----------------------------------------------------------------------------------------------------
    def msr(self, index):
        return self._msr.get(index & 0xFFFFFFFF)

    def msr_read_ok(self, index):
        """False => #GP(0). x2APIC indices belong to the LAPIC owner and raise ValueError."""
        e = self.msr(index)
        if e is None or e['access'] == 'none': return False
        if 'lapic_owner' in e['write_rules']: raise ValueError('x2APIC MSR: decided by the LAPIC owner')
        return True

    def msr_write_ok(self, index, value, *, cr0=None, efer=None):
        """False => #GP(0). Context (`cr0` for EFER.LME vs PG, `efer` the old value) is checked only when given."""
        e = self.msr(index)
        value &= MASK64
        if e is None or e['access'] in ('none', 'ro'): return False
        rules = e['write_rules']
        if 'lapic_owner' in rules: raise ValueError('x2APIC MSR: decided by the LAPIC owner')
        if 'efer' in rules:
            if value & ~_i(e['writable_mask']): return False
            return not (cr0 is not None and efer is not None and cr0 >> 31 & 1 and (value ^ efer) >> 8 & 1)
        if value & ~_i(e['writable_mask']): return False
        if 'canonical' in rules and not canonical(value, self.d['identity']['maxlinaddr']): return False
        if 'mtrr_type_low_byte' in rules and value & 0xFF not in MTRR_TYPES: return False
        if 'mtrr_type_all_bytes' in rules and any(b not in MTRR_TYPES for b in value.to_bytes(8, 'little')): return False
        if 'pat_type_all_bytes' in rules and any(b not in PAT_TYPES for b in value.to_bytes(8, 'little')): return False
        if 'apic_base_extd_needs_en' in rules and value >> 10 & 1 and not value >> 11 & 1: return False
        return True

    def arch_capabilities(self, csv3):
        """IA32_ARCH_CAPABILITIES as a host presents it: the profile's value, plus `host_bits` (RDCL_NO) when the host
        reports FEAT_CSV3 (`csv3`)."""
        e = self.msr(0x10a)
        return _i(e['reset']) | (_i(e['host_bits']['mask']) if csv3 else 0)

    def misc_enable_after_write(self, value):
        """IA32_MISC_ENABLE after an accepted write: bits 0 and 22 committed, bits 7/11/12 fixed 0/1/1 (CPU 17)."""
        e = self.msr(0x1a0)
        c = _i(e['committed_bits'])
        return (value & c) | (_i(e['reset']) & ~c)

    # ---- feature catalogue ----------------------------------------------------------------------------------------
    @staticmethod
    def host_ok(item):
        """One `host.required` item (an Arm feature name, or an any-of group) lies within the floor."""
        return item in FLOOR if isinstance(item, str) else any(i in FLOOR for i in item)

    def core(self, f):
        """Core = built, and every Arm feature it requires is within the floor (plan sec. 2.1); derived, not hand-marked."""
        return f['built'] and all(self.host_ok(i) for i in f.get('host', {}).get('required', []))

    def catalogue_problems(self):
        out = []
        err = out.append
        cat, flat = self.features(), []
        names = {n for nm in self.names.values() for n in nm.values()}
        for n, fl in cat.items():
            if len(fl) > 1: err(f'{n}: more than one catalogue entry')
            flat += fl
        if set(cat) != names: err(f'feature_names and fields disagree: {sorted(set(cat) ^ names)}')
        for f in flat:
            n, st = f['name'], f['state']
            at = f'{f["leaf"]:#x}.{f["sub"]}.{f["reg"]}[{f["bit"]}] {n}'
            if st not in STATES: err(f'{at}: state {st!r} is not one of {STATES}')
            unknown = set(f) - FIELD_KEYS - {'leaf', 'sub', 'reg', 'bit', 'remote'}
            if unknown: err(f'{at}: unknown field {sorted(unknown)}')
            host = f.get('host', {})
            if set(host) - set(HOST_CLASSES): err(f'{at}: unknown host class {sorted(set(host) - set(HOST_CLASSES))}')
            for c in HOST_CLASSES:
                for item in host.get(c, []):
                    for a in [item] if isinstance(item, str) else item:
                        if a not in ARM_FEATURES: err(f'{at}: host {c} names {a!r}, not an fexc Arm feature')
            for x in f.get('requires', []):
                if x not in cat: err(f'{at}: requires {x!r}, which is not a feature')
                elif st == 'advertised' and cat[x][0]['state'] == 'clear': err(f'{at}: requires {x}, which is not advertised')
            for g in f.get('gates', []):
                if g not in CONSUMERS: err(f'{at}: gates names {g!r}, not one of {CONSUMERS}')
            exc, tests = f.get('exceptions'), f.get('tests', [])
            if exc is None:
                if f['built']: err(f'{at}: built but has no `exceptions` (the SDM exception class of its instructions)')
            elif not isinstance(exc, list) or not exc or not all(isinstance(c, str) and EXCEPTION_CLASS.fullmatch(c) for c in exc):
                err(f'{at}: exceptions {exc!r}: a list of {EXCEPTION_CLASS.pattern}')
            elif 'none' in exc and len(exc) > 1: err(f'{at}: exceptions: `none` with other classes')
            if f['built'] and not isinstance(f.get('simd_fp'), bool): err(f'{at}: built but `simd_fp` is not true or false')
            if not isinstance(tests, list) or not all(isinstance(t, str) and (ROOT / t).is_file() for t in tests):
                err(f'{at}: tests {tests!r}: a list of existing files')
            elif f['built'] and exc not in (None, ['none']) and not tests: err(f'{at}: built, but lists no fault tests for its exception class {exc}')
            if f.get('simd_fp') is True:
                if not (isinstance(tests, list) and XM_TESTS & set(tests)): err(f'{at}: simd_fp but no #XM-covering test ({sorted(XM_TESTS)})')
                if not any(c.startswith(('SSE', 'VEX')) for c in exc or []): err(f'{at}: simd_fp but no SSE or VEX exception class')
            if 'baseline' in f and (f['baseline'] is not True or st != 'advertised'): err(f'{at}: baseline is true on an advertised feature only')
            if f['remote'] and (f['leaf'], f['sub']) in self.leaves:
                err(f'{at}: stored under another subleaf\'s register key, but that subleaf has an entry: move it there')
            if 'subleaf' in f and f['subleaf'] != f['sub']: err(f'{at}: subleaf {f["subleaf"]} but it lives in subleaf {f["sub"]}')
            if st == 'advertised' and not f['built']: err(f'{at}: advertised but not built')
            if st == 'advertised' and not self.core(f):
                out_of = [i for i in f.get('host', {}).get('required', []) if not self.host_ok(i)]
                err(f'{at}: advertised but not core (requires {out_of} beyond the floor)')
            if st == 'clear' and f['built'] and self.core(f): err(f'{at}: built and core but not advertised')
            if st == 'clear' and f['built'] and not self.core(f): err(f'{at}: optional feature needs the host-derived mask (chunk 5)')
            if st == 'clear' and f['built'] and f.get('layout'): err(f'{at}: optional layout feature (layout features must be core)')
            # `msrs` = MSRs that exist only with this feature. An MSR shared by several features (IA32_U_CET, SPEC_CTRL, TSC_AUX)
            # needs any-of semantics here before such a feature is built.
            for m in map(_i, f.get('msrs', [])):
                if st == 'advertised' and m not in self._msr: err(f'{n} advertised but MSR {m:#x} missing')
                if st == 'clear' and m in self._msr: err(f'{n} clear but MSR {m:#x} present')
            for b in f.get('cr4', []):
                bit = self.d['control']['cr4']['bits'].get(b)
                if bit is None or bit['gate'].rsplit('.', 1)[-1] != n: err(f'{at}: cr4 {b} is not gated on it')
        for b, bit in self.d['control']['cr4']['bits'].items():
            g = bit['gate']
            if g.startswith('arch'): continue
            f = cat.get(g.rsplit('.', 1)[-1])
            if f is None or b not in f[0].get('cr4', []): err(f'CR4.{b}: gated on {g}, whose catalogue entry does not list it')
        for n in V3:
            f = cat.get(n)
            if f is None: err(f'x86-64-v3 feature {n} has no catalogue entry')
            elif f[0]['state'] != 'advertised': err(f'x86-64-v3 feature {n} is not advertised')
        # leaf 7's highest subleaf (7.0 EAX) follows the highest advertised feature there (or any non-zero subleaf entry)
        l7 = self.leaves.get((7, 0))
        if l7 is not None:
            top = max([f['sub'] for f in flat if f['leaf'] == 7 and f['state'] == 'advertised']
                      + [s for (l, s), e in self.leaves.items() if l == 7 and any(_i(e[r]) for r in ('eax', 'ebx', 'ecx', 'edx'))]
                      + [0])
            if _i(l7['eax']) != top: err(f'leaf 7 max subleaf (7.0 EAX = {_i(l7["eax"])}) != highest advertised subleaf {top}')
        return out

    # ---- generated gate constants (plan sec. 2.3) -----------------------------------------------------------------
    def gated(self, *consumers):
        """The catalogue features whose `gates` name one of `consumers`, in catalogue order."""
        return [f[0] for f in self.features().values() if set(f[0].get('gates', [])) & set(consumers)]

    def startup_names(self):
        """Advertised capability bits beyond x86-64-v3 and the platform baseline, lower case: the startup line's guest map."""
        return sorted(n.lower() for n, f in self.features().items() if f[0]['state'] == 'advertised' and n not in V3 and not f[0].get('baseline'))

    def host_required(self):
        """The Arm features (fexc names) the built guest features require, sorted; an any-of group contributes every member."""
        return sorted({a for f in self.features().values() if f[0]['built'] for i in f[0].get('host', {}).get('required', [])
                       for a in ([i] if isinstance(i, str) else i)})

    # ---- validation -----------------------------------------------------------------------------------------------
    def problems(self):
        d, out = self.d, []
        err = out.append
        idn = d['identity']
        # 1 words, uniqueness, names vs fields
        seen = set()
        for e in d['cpuid']:
            k = (_i(e['leaf']), e['subleaf'])
            if k in seen: err(f'duplicate cpuid entry {k}')
            seen.add(k)
            for r in ('eax', 'ebx', 'ecx', 'edx'):
                if not 0 <= _i(e[r]) < 1 << 32: err(f'{k} {r} out of 32-bit range')
            for r, fs in e.get('fields', {}).items():
                for b, f in fs.items():
                    on = self._field_word(e, r) >> int(b) & 1
                    if (f['state'] == 'advertised') != bool(on): err(f'{k} {r} bit {b} {f["name"]}: state {f["state"]} but bit is {on}')
        for key, nm in self.names.items():
            word = self._reg(key)
            for b in range(32):
                if word >> b & 1 and b not in nm: err(f'{key} bit {b} set but unnamed')
        # 2 identity
        if self.cpuid(0)[0] != _i(idn['max_basic_leaf']): err('leaf 0 EAX != max_basic_leaf')
        if self.cpuid(0x80000000)[0] != _i(idn['max_ext_leaf']): err('leaf 0x80000000 EAX != max_ext_leaf')
        if (_i(idn['max_basic_leaf']), None) not in self.leaves: err('max basic leaf has no entry')
        if self.cpuid(1)[0] != _i(idn['signature']): err('leaf 1 EAX != signature')
        sig = _i(idn['signature'])
        if (sig >> 8 & 0xF, (sig >> 16 & 0xF) << 4 | sig >> 4 & 0xF, sig & 0xF) != (idn['family'], _i(idn['model']), idn['stepping']): err('signature != family/model/stepping')
        v = b''.join(x.to_bytes(4, 'little') for x in (self.cpuid(0)[1], self.cpuid(0)[3], self.cpuid(0)[2]))
        if v.decode() != idn['vendor']: err('vendor string')
        h = b''.join(x.to_bytes(4, 'little') for x in self.cpuid(0x40000000)[1:])
        if h.decode() != idn['hypervisor_id'] or not self.feature('1.ecx.HYPERVISOR'): err('hypervisor id / bit')
        b = b''.join(x.to_bytes(4, 'little') for l in (2, 3, 4) for x in self.cpuid(0x80000000 + l))
        if b.rstrip(b'\0').decode() != idn['brand']: err('brand string')
        eax, ebx, ecx, _ = self.cpuid(0x15)
        if eax == 0 or ecx * ebx // eax != idn['tsc_hz'] or ecx != idn['crystal_hz']: err('leaf 0x15 does not give tsc_hz')
        a8 = self.cpuid(0x80000008)[0]
        if (a8 & 0xFF, a8 >> 8 & 0xFF) != (idn['maxphyaddr'], idn['maxlinaddr']): err('leaf 0x80000008 widths')
        # 3 terminators and cache geometry
        if self.cpuid(4, 4) != (0, 0, 0, 0) or self.cpuid(4, 3)[0] & 31 == 0: err('leaf 4 terminator')
        if self.cpuid(0xB, 2)[1] != 0 or self.cpuid(0xB, 2)[2] >> 8 & 0xFF != 0: err('leaf 0xB terminator')
        for s in range(4):
            c = self.cpuid(4, s)
            if (c[1] & 0xFFF) + 1 != idn['cache_line'] or (c[1] >> 12 & 0x3FF) != 0: err(f'leaf 4.{s} line/partitions')
        l2 = self.cpuid(4, 2)
        size2 = ((l2[1] >> 22) + 1) * (l2[2] + 1) * 64 // 1024
        if self.cpuid(0x80000006)[2] >> 16 != size2: err('leaf 0x80000006 L2 size != leaf 4.2')
        # 4 XSAVE/AVX/XCR0
        x = d['control']['xcr0']
        sm = _i(x['supported_mask'])
        if not sm & 1 or (sm & 4 and not sm & 2) or sm & ~7: err('XCR0 supported mask')
        if {_i(t) for t in x['valid_values']} != {m for m in range(8) if m & 1 and not (m & 4 and not m & 2) and m & ~sm == 0}: err('XCR0 valid_values')
        if self.feature('1.ecx.XSAVE'):
            if self.cpuid(0xD, 0)[0] != sm: err('XSAVE advertised but 0xD.0 EAX != XCR0 mask')
            if not self.d['control']['cr4']['bits']['OSXSAVE']['valid']: err('XSAVE advertised but CR4.OSXSAVE not valid')
            if self.feature('1.ecx.OSXSAVE'): err('OSXSAVE must be dynamic, not static')
            if (self.cpuid(1, cr4=1 << 18)[2] >> 27 & 1) != 1 or (self.cpuid(1)[2] >> 27 & 1): err('OSXSAVE not dynamic')
            if self.cpuid(0xD, 0, xcr0=7)[1] != 832 or self.cpuid(0xD, 0, xcr0=3)[1] != 576: err('0xD.0 EBX size')
            c1 = self.cpuid(0xD, 1)[0]
            if c1 & 8: err('XSAVES/IA32_XSS not modelled')
            # XSAVEC without XSAVES: 0xD.1 EBX is the compacted size for XCR0 (SDM Vol.1 13.2); with AVX the only extended
            # component, unaligned (0xD.2 ECX[1] = 0), compacted equals standard. Without XSAVEC, EBX = 0.
            for v in (1, 3, 7):
                want = (576 + (256 if v & 4 else 0)) if c1 & 2 else 0
                if self.cpuid(0xD, 1, xcr0=v)[1] != want: err(f'0xD.1 EBX for XCR0 {v}')
            if c1 & 2 and self.cpuid(0xD, 2)[2] & 2: err('compacted AVX alignment: the sizes above assume ECX[1] = 0')
        elif sm != 1 or (0xD, 0) in self.leaves and _i(self.leaves[(0xD, 0)]['eax']): err('no XSAVE but XCR0/leaf 0xD present')
        avx = self.feature('1.ecx.AVX')
        if avx != bool(sm & 4): err('AVX advertised <-> XCR0 bit 2')
        if avx and (self.cpuid(0xD, 2)[:2] != (256, 576)): err('AVX component layout')
        if avx and self.cpuid(0xD, 0)[2] != 576 + 256: err('0xD.0 ECX != 832')
        for f in ('1.ecx.FMA', '1.ecx.F16C', '7.0.ebx.AVX2'):
            if self.feature(f) and not avx: err(f'{f} needs AVX')
        if self.feature('1.ecx.X2APIC') and not (self.cpuid(1)[3] >> 9 & 1 and not self.cpuid(1, apic_en=False)[3] >> 9 & 1): err('x2APIC needs the dynamic APIC bit')
        # 5 control-register gates
        for reg in ('cr0', 'cr4', 'efer'):
            for n, bt in d['control'][reg]['bits'].items():
                g = bt['gate']
                if g.startswith('arch'):
                    if not bt['valid']: err(f'{reg}.{n}: architectural bit not valid')
                elif bt['valid'] != self.feature(g): err(f'{reg}.{n}: valid={bt["valid"]} but {g} is {self.feature(g)}')
        out += self.catalogue_problems()
        # 6 MSRs
        for i in set(self._msr) & set(self.absent): err(f'MSR {i:#x} both present and absent')
        for e in d['msrs']:
            for r in e['write_rules']:
                if r not in RULES: err(f'MSR {e["name"]}: unknown rule {r}')
            if e['access'] == 'rw':
                if _i(e['reset']) & ~_i(e['writable_mask']): err(f'MSR {e["name"]}: reset outside writable mask')
        if not all(m in self._msr for m in range(0x200, 0x210)) or not all(m in self._msr for m in (0x250, 0x258, 0x259, *range(0x268, 0x270))): err('MTRR set incomplete')
        if self._msr[0xfe]['reset'] != '0x508' and _i(self._msr[0xfe]['reset']) & 0xFF != 8: err('MTRRCAP VCNT != 8')
        pat = _i(self._msr[0x277]['reset'])
        if any(t not in PAT_TYPES for t in pat.to_bytes(8, 'little')): err('PAT reset has invalid types')
        ac = self._msr.get(0x10a)
        if ac:
            ones = sum(1 << int(b) for b, v in ac['bits'].items() if b.isdigit() and v['value'] == 1)
            if ones != _i(ac['reset']): err('IA32_ARCH_CAPABILITIES reset != its bits marked 1')
            if _i(ac['host_bits']['mask']) & _i(ac['reset']) or ac['host_bits']['condition'] != 'speculation-barrier switch on AND FEAT_CSV3': err('IA32_ARCH_CAPABILITIES host bits')
            if ac['access'] != 'ro': err('IA32_ARCH_CAPABILITIES is read-only (SDM Vol.4 Table 2-2)')
            if self.feature('7.0.edx.MD_CLEAR') or _i(ac['reset']) >> 5 & 1: err('MD_CLEAR/MDS_NO set (owner decision November 5: both 0)')
        me = self._msr[0x1a0]
        if _i(me['reset']) != 0x1801 or _i(me['committed_bits']) != 1 << 0 | 1 << 22: err('MISC_ENABLE reset/committed bits')
        # 7 kernel sites
        for s in d['kernel_msr_sites']:
            m, g = _i(s['msr']), s['gate']
            gated = g is not None
            if not s['safe'] and not gated and m in self.absent: err(f'{s["site"]}: unconditional unsafe access to absent MSR {m:#x}')
            if g and not g.startswith('cond:') and self.feature(g) and m not in self._msr: err(f'{s["site"]}: gate {g} advertised but MSR {m:#x} absent')
            if not s['safe'] and not gated and m not in self._msr: err(f'{s["site"]}: unsafe MSR {m:#x} not in the profile')
        # 8 parameter
        mi = d['parameters']['mtrr_initial']
        if mi['selected'] not in (None, 'disabled', 'enabled'): err('mtrr_initial.selected')
        if mi['selected'] == 'enabled':
            st = self.mtrr_state()
            if {k: v for k, v in st.items()} != {_i(k): _i(v) for k, v in mi['msr_values_for_default_L'].items()}: err('mtrr_initial values != mtrr_state(L)')
            for k, v in st.items():
                if not self.msr_write_ok(k, v): err(f'mtrr_initial: WRMSR {k:#x} <- {v:#x} would #GP')
        return out


def load(path=PATH):
    p = Profile(json.loads(pathlib.Path(path).read_text()))
    bad = p.problems()
    if bad: raise ValueError('cpu_profile.json is inconsistent:\n  ' + '\n  '.join(bad))
    return p
