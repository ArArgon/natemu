"""Loader, consistency validator and queries for the presented CPU (`cpu_profile.json`).

The JSON is the accepted virtual CPU of INV-03-01 CLM-03-01-16 (with CLM-03-01-17's compatibility registers); every
entry carries its source. This module adds no values: it parses them, checks that they agree with each other (a feature
bit with no MSR, an XSAVE leaf that disagrees with XCR0, a kernel MSR site that would #GP) and answers the questions
runners and models ask (`cpuid`, `msr_write_ok`, `cr4_valid_mask`, ...). Python 3.9, no emulator imports.
"""
import json
import pathlib

PATH = pathlib.Path(__file__).with_name('cpu_profile.json')
MASK64 = (1 << 64) - 1
MTRR_TYPES = (0, 1, 4, 5, 6)           # UC, WC, WT, WP, WB (SDM Vol.3 11.11.2.1)
PAT_TYPES = MTRR_TYPES + (7,)          # plus UC-  (SDM Vol.3 11.12.2)
RULES = {'canonical', 'reserved_gp', 'apic_base_extd_needs_en', 'ucode_low32_zero', 'misc_enable', 'mtrr_type_low_byte',
         'mtrr_type_all_bytes', 'pat_type_all_bytes', 'efer', 'always_gp', 'always_gp_on_write', 'lapic_owner'}
# feature bit -> MSRs that must exist when it is advertised / must be absent when it is clear
FEATURE_MSRS = {'1.edx.SEP': (0x174, 0x175, 0x176), '1.edx.MTRR': (0xfe, 0x2ff, 0x200, 0x201, 0x250, 0x268),
                '1.edx.PAT': (0x277,), '1.edx.APIC': (0x1b,), '0x80000001.edx.RDTSCP': (0xC0000103,)}
ABSENT_WHEN_CLEAR = {'7.0.ebx.TSC_ADJUST': 0x3b, '1.ecx.TSC_DEADLINE': 0x6e0, '13.1.eax.XSAVES': 0xda0, '13.1.eax.XFD': 0x1c4,
                     '7.0.edx.SPEC_CTRL': 0x48, '7.0.edx.ARCH_CAPABILITIES': 0x10a, '7.0.ecx.CET_SS': 0x6a2, '1.edx.MCA': 0x179}


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
        elif l == 4:
            eax |= ((n - 1) << 26) | ((n - 1) << 14 if sub == 3 else 0)
        elif l == 0xB:
            edx = vcpu
            if sub == 1: eax, ebx = (n - 1).bit_length(), n
            if sub >= 2: ecx = sub & 0xFF
        elif l == 0xD and sub == 0:
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
        `ram_limit` L (default: the profile's) moves the UC aperture [L, 4 GiB); the fixed ranges do not depend on it."""
        m = self.d['parameters']['mtrr_initial']
        if m['selected'] != 'enabled': raise ValueError('only the "enabled" initial state carries values')
        L = _i(m['ram_limit_L']) if ram_limit is None else ram_limit
        st = {_i(k): _i(v) for k, v in m['msr_values_for_default_L'].items() if _i(k) >= 0x250 or _i(k) == 0x2ff}
        for i, (base, size) in enumerate(uc_pairs(L, 1 << 32)):
            st[0x200 + 2 * i] = base
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

    def misc_enable_after_write(self, value):
        """IA32_MISC_ENABLE after an accepted write: bits 0 and 22 committed, bits 7/11/12 fixed 0/1/1 (CPU 17)."""
        e = self.msr(0x1a0)
        c = _i(e['committed_bits'])
        return (value & c) | (_i(e['reset']) & ~c)

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
                    on = _i(e[r]) >> int(b) & 1
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
        # 6 MSRs
        for i in set(self._msr) & set(self.absent): err(f'MSR {i:#x} both present and absent')
        for e in d['msrs']:
            for r in e['write_rules']:
                if r not in RULES: err(f'MSR {e["name"]}: unknown rule {r}')
            if e['access'] == 'rw':
                if _i(e['reset']) & ~_i(e['writable_mask']): err(f'MSR {e["name"]}: reset outside writable mask')
        for f, ms in FEATURE_MSRS.items():
            if self.feature(f) and not all(m in self._msr for m in ms): err(f'{f} advertised but MSRs {[hex(m) for m in ms if m not in self._msr]} missing')
        for f, m in ABSENT_WHEN_CLEAR.items():
            if self.feature(f): err(f'{f} is advertised: update this check')
            elif m in self._msr: err(f'{f} clear but MSR {m:#x} present')
        if not all(m in self._msr for m in range(0x200, 0x210)) or not all(m in self._msr for m in (0x250, 0x258, 0x259, *range(0x268, 0x270))): err('MTRR set incomplete')
        if self._msr[0xfe]['reset'] != '0x508' and _i(self._msr[0xfe]['reset']) & 0xFF != 8: err('MTRRCAP VCNT != 8')
        pat = _i(self._msr[0x277]['reset'])
        if any(t not in PAT_TYPES for t in pat.to_bytes(8, 'little')): err('PAT reset has invalid types')
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
            if {k: v for k, v in st.items()} != {_i(k): _i(v) for k, v in mi['msr_values_for_default_L'].items()}: err('mtrr_initial values != uc_pairs(L)')
            for k, v in st.items():
                if not self.msr_write_ok(k, v): err(f'mtrr_initial: WRMSR {k:#x} <- {v:#x} would #GP')
            if len(uc_pairs(1 << 30, 1 << 32)) > 8: err('too many variable pairs')
        return out


def load(path=PATH):
    p = Profile(json.loads(pathlib.Path(path).read_text()))
    bad = p.problems()
    if bad: raise ValueError('cpu_profile.json is inconsistent:\n  ' + '\n  '.join(bad))
    return p
