//! The presented CPU's architectural answers (`crate::cpu_profile`, generated from `tools/models/cpu_profile.json`):
//! CPUID with its dynamic fields, and the MSR checks the resident's RDMSR/WRMSR transactions apply. Pure functions of
//! the profile and the vCPU's state; the resident owns the side effects. The tests replay `cpu_profile.py`'s answers
//! (`cpu_profile::vectors`), so this module and the Python judge cannot drift apart.
use crate::cpu_profile::{self as profile, Msr, access, rule};

pub const MSR_TSC: u32 = 0x10;
pub const MSR_APIC_BASE: u32 = 0x1b;
pub const MSR_SYSENTER_CS: u32 = 0x174;
pub const MSR_SYSENTER_ESP: u32 = 0x175;
pub const MSR_SYSENTER_EIP: u32 = 0x176;
pub const MSR_UCODE_REV: u32 = 0x8b;
pub const MSR_MISC_ENABLE: u32 = 0x1a0;
pub const MSR_PAT: u32 = 0x277;
pub const MSR_MTRR_DEF_TYPE: u32 = 0x2ff;
pub const MSR_EFER: u32 = 0xc000_0080;
pub const MSR_STAR: u32 = 0xc000_0081;
pub const MSR_LSTAR: u32 = 0xc000_0082;
pub const MSR_FMASK: u32 = 0xc000_0084;
pub const MSR_FS_BASE: u32 = 0xc000_0100;
pub const MSR_GS_BASE: u32 = 0xc000_0101;
pub const MSR_KERNEL_GS_BASE: u32 = 0xc000_0102;
pub const MSR_TSC_AUX: u32 = 0xc000_0103;
/// The per-vCPU MSR values, one per [`profile::MSRS`] entry (the x2APIC range's slot and EFER's are unused: the LAPIC
/// owner's, and `Cpu::efer`; TSC's holds the offset added to the common count).
pub type Msrs = [u64; profile::MSRS.len()];

/// The variable and fixed MTRRs and MTRR_DEF_TYPE (SDM Vol.3 11.11): a write changes memory types.
pub fn is_mtrr(index: u32) -> bool {
    matches!(index, 0x200..=0x20f | 0x250 | 0x258 | 0x259 | 0x268..=0x26f | MSR_MTRR_DEF_TYPE)
}

/// The slot of MSR `index` (ECX) in [`profile::MSRS`]; `None`: not in the profile (#GP(0)).
pub const fn msr(index: u32) -> Option<usize> {
    let mut i = 0;
    while i < profile::MSRS.len() {
        if profile::MSRS[i].index <= index && index <= profile::MSRS[i].last {
            return Some(i);
        }
        i += 1;
    }
    None
}
const fn slot(index: u32) -> usize {
    match msr(index) {
        Some(i) => i,
        None => panic!("not in the profile"),
    }
}

/// M2: memory-type encodings (SDM Vol.3 Table 14-8 "Memory Types That Can Be Encoded in MTRRs" and Table 14-10 "... With
/// PAT"; UC- only in the PAT). WRMSR refuses every other encoding (#GP, [`write_ok`]), so the MSRs hold only these.
pub mod memory_type {
    pub const UC: u8 = 0;
    pub const WC: u8 = 1;
    pub const WT: u8 = 4;
    pub const WP: u8 = 5;
    pub const WB: u8 = 6;
    pub const UC_MINUS: u8 = 7;
}
use memory_type::*;
const MTRRCAP: u32 = 0xfe;
/// Variable ranges (IA32_MTRRCAP.VCNT; MTRRCAP is read-only, so the profile's value) and their PHYSBASE/PHYSMASK slots.
const VCNT: usize = (profile::MSRS[slot(MTRRCAP)].reset & 0xff) as usize;
const VARIABLE: [usize; 2 * VCNT] = {
    let mut out = [0; 2 * VCNT];
    let mut n = 0;
    while n < 2 * VCNT {
        out[n] = slot(0x200 + n as u32);
        n += 1;
    }
    out
};
/// The fixed-range MTRRs (SDM Vol.3 Table 14-9): FIX64K_00000, FIX16K_80000, FIX16K_A0000, FIX4K_C0000 ..= FIX4K_F8000.
const FIXED: [usize; 11] = {
    let mut out = [slot(0x250), slot(0x258), slot(0x259), 0, 0, 0, 0, 0, 0, 0, 0];
    let mut k = 0;
    while k < 8 {
        out[3 + k] = slot(0x268 + k as u32);
        k += 1;
    }
    out
};
const _: () = assert!(profile::MSRS[slot(MTRRCAP)].reset >> 8 & 1 != 0, "fixed ranges advertised");

/// The MTRRs' memory type of guest-physical `gpa` (SDM Vol.3 14.11.4.1 "MTRR Precedences"): all UC while
/// IA32_MTRR_DEF_TYPE.E is clear; below 1 MiB with FE set, the fixed range's field (14.11.2.2, Table 14-9); else the
/// matching variable ranges (`gpa & PhysMask == PhysBase & PhysMask` with V set, 14.11.2.3): one, or several of one type,
/// gives that type, any UC among them UC, WT with WB WT; with none, the default type. Other overlaps are undefined in the
/// SDM; natemu takes UC for them (the strongest: an access there is then strongly ordered, M2 addendum 2).
pub fn mtrr_type(m: &Msrs, gpa: u64) -> u8 {
    let def = m[const { slot(MSR_MTRR_DEF_TYPE) }];
    if def >> 11 & 1 == 0 {
        return UC;
    }
    if def >> 10 & 1 != 0 && gpa < 0x10_0000 {
        let (k, field) = match gpa {
            ..0x8_0000 => (0, gpa >> 16),
            ..0xc_0000 => (1 + ((gpa - 0x8_0000) >> 17), gpa >> 14 & 7),
            _ => (3 + ((gpa - 0xc_0000) >> 15), gpa >> 12 & 7),
        };
        return (m[FIXED[k as usize]] >> (8 * field)) as u8;
    }
    let mut found = None;
    for n in 0..VCNT {
        let (base, mask) = (m[VARIABLE[2 * n]], m[VARIABLE[2 * n + 1]]);
        let range = mask & crate::paging::PA_MASK;
        if mask >> 11 & 1 == 0 || gpa & range != base & range {
            continue;
        }
        let t = base as u8;
        found = Some(match found {
            None => t,
            Some(f) if f == t => t,
            Some(f) if f == UC || t == UC => UC,
            Some(WT | WB) if matches!(t, WT | WB) => WT,
            Some(_) => UC,
        });
    }
    found.unwrap_or(def as u8)
}
/// [lo, hi) as power-of-two sized, size-aligned blocks (what one variable pair describes), appended to `out` with type
/// `t` from `n`; the count after, or `None` past [`VCNT`] pairs. `cpu_profile.py`'s `uc_pairs`.
fn blocks(out: &mut [(u64, u64, u64); VCNT], mut n: usize, mut lo: u64, hi: u64, t: u64) -> Option<usize> {
    while lo < hi {
        let mut size = 1u64 << (63 - (hi - lo).leading_zeros());
        while lo % size != 0 {
            size >>= 1;
        }
        *out.get_mut(n)? = (lo, size, t);
        (n, lo) = (n + 1, lo + size);
    }
    Some(n)
}
/// The virtual firmware's variable MTRRs and default type for guest RAM [0, `ram`) (M1: a 2 MiB multiple up to 3 GiB),
/// written over [`reset_msrs`]' state, which is this for the profile's L = 2 GiB: `cpu_profile.py`'s `mtrr_state(ram)`
/// (the generated vectors check it). Default type WB with [ram, 4 GiB) in UC pairs; when they need more than [`VCNT`]
/// pairs, default type UC with [0, ram) in WB pairs: the fewer of its power-of-two blocks or one block to the next power
/// of two with the rest carved out in UC pairs (UC wins an overlap, SDM Vol.3 14.11.4.1). Every RAM page is then WB by
/// the MTRRs and every other address below 4 GiB UC.
pub fn firmware_mtrrs(m: &mut Msrs, ram: u64) {
    let mut pairs = [(0, 0, 0); VCNT];
    let def = profile::MTRR_INITIAL.iter().find(|e| e.0 == MSR_MTRR_DEF_TYPE).unwrap().1;
    let (def, n) = match blocks(&mut pairs, 0, ram, 1 << 32, UC as u64) {
        Some(n) => (def, n),
        None => {
            let mut direct = [(0, 0, 0); VCNT];
            let d = blocks(&mut direct, 0, 0, ram, WB as u64);
            let top = ram.next_power_of_two();
            let mut carved = [(0, 0, 0); VCNT];
            carved[0] = (0, top, WB as u64);
            let c = blocks(&mut carved, 1, ram, top, UC as u64);
            let (chosen, n) = match (d, c) {
                (Some(d), Some(c)) if c < d => (carved, c),
                (Some(d), _) => (direct, d),
                (None, Some(c)) => (carved, c),
                (None, None) => panic!("RAM of {ram:#x} needs more than {VCNT} variable MTRRs"),
            };
            pairs = chosen;
            (def & !0xff, n)
        }
    };
    m[const { slot(MSR_MTRR_DEF_TYPE) }] = def;
    for (k, &(base, size, t)) in pairs.iter().enumerate() {
        let used = k < n;
        m[VARIABLE[2 * k]] = if used { base | t } else { 0 };
        m[VARIABLE[2 * k + 1]] = if used { !(size - 1) & crate::paging::PA_MASK | 1 << 11 } else { 0 };
    }
}
/// The effective memory type of an MTRR type and a PAT entry (SDM Vol.3 Table 14-7, normal caching: CR0.CD and NW clear;
/// with CD set the processor stops filling its caches but the table's types and orderings stay, Table 14-5).
pub fn effective_type(mtrr: u8, pat: u8) -> u8 {
    match (mtrr, pat) {
        (_, UC) => UC,
        (_, WC) => WC,
        (UC, _) => UC,
        (WC, UC_MINUS | WB) => WC,
        (WC, _) => UC,
        (WT | WB, UC_MINUS) => UC,
        (WT, WT | WB) => WT,
        (WT, WP) => WP,
        (WB, t) => t,
        (WP, UC_MINUS) => WC,
        (WP, WT) => WT,
        (WP, _) => WP,
        _ => UC,
    }
}
/// The effective memory type of a guest-physical page that a leaf with PAT index `pat` (paging `Translation::pat`) maps:
/// the PAT entry it selects (IA32_PAT bits 8i+2:8i, Table 14-11) combined with the MTRRs' type of `gpa` (Table 14-7). M2:
/// WB, WT, WP and WC RAM run natively, UC RAM one access at a time between full barriers (owner decision, November 2:
/// strong ordering), and a device page goes to its device whatever its type; the resident counts data shadows by this type
/// (`cache::Control::memory_types`).
pub fn memory_type(m: &Msrs, gpa: u64, pat: u8) -> u8 {
    effective_type(mtrr_type(m, gpa), (m[const { slot(MSR_PAT) }] >> (8 * (pat & 7))) as u8 & 7)
}
/// RDMSR is allowed. The x2APIC range's entry is not (`owned-by-LAPIC`): the resident's local APIC answers 0x800-0x8ff
/// before the profile is consulted (X3).
pub fn read_ok(m: &Msr) -> bool {
    matches!(m.access, access::RO | access::RW)
}
/// WRMSR of `value` is allowed (else #GP(0)), with `cr0`/`efer` the current values (EFER.LME may not change while
/// CR0.PG = 1). The order and the checks are `cpu_profile.py`'s `msr_write_ok`.
pub fn write_ok(m: &Msr, value: u64, cr0: u64, efer: u64) -> bool {
    if m.access != access::RW || value & !m.writable != 0 {
        return false;
    }
    let has = |r: u8| m.rules & r != 0;
    let mtrr_type = |t: u64| matches!(t, 0 | 1 | 4 | 5 | 6); // UC WC WT WP WB (SDM Vol.3 11.11.2.1)
    let bytes = |ok: &dyn Fn(u64) -> bool| (0..8).all(|k| ok(value >> (8 * k) & 0xff));
    !(has(rule::EFER) && cr0 >> 31 & 1 != 0 && (value ^ efer) >> 8 & 1 != 0
        || has(rule::CANONICAL) && !canonical(value)
        || has(rule::MTRR_TYPE_LOW_BYTE) && !mtrr_type(value & 0xff)
        || has(rule::MTRR_TYPE_ALL_BYTES) && !bytes(&mtrr_type)
        || has(rule::PAT_TYPE_ALL_BYTES) && !bytes(&|t| mtrr_type(t) || t == 7) // and UC- (11.12.2)
        || has(rule::APIC_BASE_EXTD_NEEDS_EN) && value >> 10 & 1 != 0 && value >> 11 & 1 == 0)
}
/// Canonical for the profile's linear-address width.
pub fn canonical(v: u64) -> bool {
    let shift = 64 - profile::MAXLINADDR;
    ((v << shift) as i64 >> shift) as u64 == v
}
/// IA32_MISC_ENABLE after an accepted write: the committed bits from `value`, the rest at reset (CPU 17).
pub fn misc_enable_after_write(value: u64) -> u64 {
    let reset = profile::MSRS[msr(MSR_MISC_ENABLE).unwrap()].reset;
    value & profile::MISC_ENABLE_COMMITTED | reset & !profile::MISC_ENABLE_COMMITTED
}
/// A vCPU's MSRs after reset (vCPU 0 is the BSP) with the virtual firmware's MTRR state applied.
pub const fn reset_msrs(bsp: bool) -> Msrs {
    let mut out = [0; profile::MSRS.len()];
    let mut i = 0;
    while i < out.len() {
        let m = &profile::MSRS[i];
        out[i] = if m.index == MSR_APIC_BASE && bsp { profile::APIC_BASE_BSP_RESET } else { m.reset };
        let mut k = 0;
        while k < profile::MTRR_INITIAL.len() {
            if profile::MTRR_INITIAL[k].0 == m.index {
                out[i] = profile::MTRR_INITIAL[k].1;
            }
            k += 1;
        }
        i += 1;
    }
    out
}

/// The state CPUID's dynamic fields follow (CPU 16): the vCPU count and number (initial APIC ID = x2APIC ID = vCPU
/// number), CR4 (OSXSAVE), IA32_APIC_BASE (EN), IA32_MISC_ENABLE (bit 22) and XCR0.
#[derive(Clone, Copy, Debug)]
pub struct Dynamic {
    pub vcpus: u64,
    pub vcpu: u64,
    pub cr4: u64,
    pub apic_base: u64,
    pub misc_enable: u64,
    pub xcr0: u64,
}
/// CPUID(EAX = `leaf`, ECX = `sub`) in 64-bit mode: [EAX, EBX, ECX, EDX]. Above the maximum, a basic or extended leaf
/// returns the highest basic leaf's data; holes and subleaves past the last return zero.
pub fn cpuid(leaf: u32, sub: u32, d: &Dynamic) -> [u32; 4] {
    let max_basic = if d.misc_enable >> 22 & 1 != 0 { profile::MAX_BASIC_LEAF_LIMITED } else { profile::MAX_BASIC_LEAF };
    let l = match leaf {
        profile::HYPERVISOR_LEAF => leaf,
        0x4000_0000..=0x4fff_ffff => max_basic, // OPEN in the profile: treated as out of range
        0x8000_0000.. if leaf <= profile::MAX_EXT_LEAF => leaf,
        _ => leaf.min(max_basic),
    };
    let find = |s: u32| profile::CPUID.iter().find(|e| e.0 == l && e.1 == s).map(|e| e.2);
    let Some([mut eax, mut ebx, mut ecx, mut edx]) =
        find(profile::ANY).or_else(|| find(sub)).or_else(|| if l == 0xb && sub >= 2 { find(2) } else { None })
    else {
        return [0; 4];
    };
    let (n, bit) = (d.vcpus as u32, |v: u64, b: u32| (v >> b & 1) as u32);
    match l {
        0 => eax = max_basic,
        1 => {
            ebx = ebx & 0xffff | (d.vcpu as u32 & 0xff) << 24 | (n & 0xff) << 16;
            ecx |= bit(d.cr4, 18) << 27;
            edx |= bit(d.apic_base, 11) << 9 | u32::from(n > 1) << 28;
        }
        // The terminator (cache type 0, EAX[4:0] = 0) stays all zeros (SDM Vol.2A CPUID leaf 4).
        4 if eax & 31 != 0 => eax |= (n - 1) << 26 | if sub == 3 { (n - 1) << 14 } else { 0 },
        0xb => {
            edx = d.vcpu as u32;
            if sub == 1 {
                (eax, ebx) = (32 - (n - 1).leading_zeros(), n);
            }
            if sub >= 2 {
                ecx = sub & 0xff;
            }
        }
        // 0xD.1 EBX with XSAVEC and no XSAVES (EAX = 3): XSAVEC's area size for XCR0, the standard one (SDM Vol.1 13.2).
        0xd if sub == 0 || (sub == 1 && eax & 2 != 0) => ebx = if d.xcr0 & 4 != 0 { 832 } else { 576 },
        0x8000_0001 => edx |= 1 << 11, // SYSCALL: 64-bit mode
        _ => {}
    }
    [eax, ebx, ecx, edx]
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::cpu_profile::vectors as v;

    #[test]
    fn cpuid_matches_the_model() {
        for &(leaf, sub, vcpus, vcpu, cr4, en, misc_enable, xcr0, want) in v::CPUID.iter() {
            let d = Dynamic { vcpus, vcpu, cr4, apic_base: if en { 0xfee0_0800 } else { 0xfee0_0000 }, misc_enable, xcr0 };
            assert_eq!(cpuid(leaf, sub, &d), want, "leaf {leaf:#x}.{sub} {d:?}");
        }
    }

    #[test]
    fn msr_checks_match_the_model() {
        for &(index, read, writes) in v::MSR.iter() {
            let m = msr(index).map(|i| profile::MSRS[i]);
            assert_eq!(m.is_some_and(|m| read_ok(&m)), read, "rdmsr {index:#x}");
            for (k, &value) in v::VALUES.iter().enumerate() {
                let ok = m.is_some_and(|m| write_ok(&m, value, v::CR0, v::EFER));
                assert_eq!(ok, writes >> k & 1 != 0, "wrmsr {index:#x} <- {value:#x}");
            }
        }
        for &(value, after) in v::MISC_ENABLE.iter() {
            assert_eq!(misc_enable_after_write(value), after, "{value:#x}");
        }
    }

    /// M2: SDM Vol.3 Table 14-7, transcribed row by row (MTRR type, PAT entry, effective type).
    #[test]
    fn effective_type_is_table_14_7() {
        const TABLE: [(u8, [u8; 6]); 5] = [
            // PAT entry:  UC  UC-        WC  WT  WB  WP
            (UC, [UC, UC, WC, UC, UC, UC]),
            (WC, [UC, WC, WC, UC, WC, UC]),
            (WT, [UC, UC, WC, WT, WT, WP]),
            (WB, [UC, UC, WC, WT, WB, WP]),
            (WP, [UC, WC, WC, WT, WP, WP]),
        ];
        for (mtrr, row) in TABLE {
            for (pat, want) in [UC, UC_MINUS, WC, WT, WB, WP].into_iter().zip(row) {
                assert_eq!(effective_type(mtrr, pat), want, "MTRR {mtrr} PAT {pat}");
            }
        }
    }

    /// M2: the MTRR precedences (SDM Vol.3 14.11.4.1) and the PAT entry selected by index (Table 14-11).
    #[test]
    fn memory_types_follow_the_mtrrs_and_the_pat() {
        let mut m = reset_msrs(true);
        let set = |m: &mut Msrs, index: u32, v: u64| {
            assert!(write_ok(&profile::MSRS[slot(index)], v, 0x8000_0011, 0xd00), "{index:#x} <- {v:#x}");
            m[slot(index)] = v;
        };
        // The firmware state (`profile::MTRR_INITIAL`): fixed 0-0x9ffff WB, 0xa0000-0xfffff UC; 2-4 GiB UC; default WB.
        let t = |m: &Msrs, gpa| mtrr_type(m, gpa);
        assert_eq!([0, 0x9_f000, 0xa_0000, 0xf_f000, 0x10_0000, 0x7fff_f000, 0x8000_0000, 0xffff_f000, 0x1_0000_0000].map(|g| t(&m, g)),
                   [WB, WB, UC, UC, WB, WB, UC, UC, WB]);
        // The reset PAT (Table 14-12): WB WT UC- UC WB WT UC- UC; under WB the index selects WB WT UC UC (UC- -> UC).
        assert_eq!(core::array::from_fn::<u8, 8, _>(|i| memory_type(&m, 0x10_0000, i as u8)), [WB, WT, UC, UC, WB, WT, UC, UC]);
        // Linux's PAT (WB WC UC- UC WB WP UC- WT): under the UC range at 2 GiB, WC stays WC and the rest is UC.
        set(&mut m, MSR_PAT, 0x0407_0506_0007_0106);
        assert_eq!(core::array::from_fn::<u8, 8, _>(|i| memory_type(&m, 0x10_0000, i as u8)), [WB, WC, UC, UC, WB, WP, UC, WT]);
        assert_eq!(core::array::from_fn::<u8, 8, _>(|i| memory_type(&m, 0x8000_0000, i as u8)), [UC, WC, UC, UC, UC, UC, UC, UC]);
        // Every fixed-range field: register k's byte j covers its own sub-range (Table 14-9).
        let fixed = [(0x250, 0u64, 0x1_0000u64), (0x258, 0x8_0000, 0x4000), (0x259, 0xa_0000, 0x4000)]
            .into_iter()
            .chain((0..8).map(|k| (0x268 + k, 0xc_0000 + 0x8000 * k as u64, 0x1000)));
        for (index, start, size) in fixed {
            let types = [UC, WC, WT, WP, WB, WC, WT, UC];
            set(&mut m, index, u64::from_le_bytes(types));
            for (j, &want) in types.iter().enumerate() {
                for gpa in [start + size * j as u64, start + size * (j as u64 + 1) - 1] {
                    assert_eq!(t(&m, gpa), want, "{index:#x} {gpa:#x}");
                }
            }
        }
        // FE clear: the fixed ranges are off, the variable ranges and the default apply below 1 MiB too.
        set(&mut m, MSR_MTRR_DEF_TYPE, 0x806);
        assert_eq!(t(&m, 0xa_0000), WB);
        set(&mut m, 0x20e, 0x5); // range 7: 0-1 MiB WP
        set(&mut m, 0x20f, 0xff_fff0_0800);
        assert_eq!(t(&m, 0xa_0000), WP);
        // Overlaps: identical types; UC wins; WT with WB is WT; a range with V clear is ignored.
        set(&mut m, 0x202, 0x4000_0006); // range 1: 1 GiB at 1 GiB, WB
        set(&mut m, 0x203, 0xff_c000_0800);
        set(&mut m, 0x204, 0x4000_0004); // range 2: 4 KiB at 1 GiB, WT
        set(&mut m, 0x205, 0xff_ffff_f800);
        set(&mut m, 0x206, 0x4000_1000); // range 3: 4 KiB at 1 GiB + 4 KiB, UC
        set(&mut m, 0x207, 0xff_ffff_f800);
        set(&mut m, 0x208, 0x4000_2006); // range 4: 4 KiB at 1 GiB + 8 KiB, WB (identical to range 1)
        set(&mut m, 0x209, 0xff_ffff_f800);
        set(&mut m, 0x20a, 0x4000_3001); // range 5: 4 KiB at 1 GiB + 12 KiB, WC, not valid
        set(&mut m, 0x20b, 0xff_ffff_f000);
        assert_eq!([0x4000_0000, 0x4000_1000, 0x4000_2000, 0x4000_3000, 0x4000_4000, 0x7fff_f000].map(|g| t(&m, g)),
                   [WT, UC, WB, WB, WB, WB]);
        // A discontinuous mask (14.11.2.3): bit 30 only, so every address with bit 30 set matches: WC alone; WC with range
        // 1's WB is undefined (natemu: UC); with range 0's UC, UC; bit 30 clear, the default.
        set(&mut m, 0x20b, 0x00_4000_0800);
        assert_eq!([0x1_4000_3000, 0x4000_3000, 0xc000_3000, 0x10_3000].map(|g| t(&m, g)), [WC, UC, UC, WB]);
        // Default type: outside every range.
        set(&mut m, MSR_MTRR_DEF_TYPE, 0x800);
        assert_eq!(t(&m, 0x2_0000_0000), UC);
        // E clear: everything UC (the PAT cannot make it cacheable; WC stays WC, Table 14-7).
        set(&mut m, MSR_MTRR_DEF_TYPE, 0x406);
        assert_eq!([0, 0x10_0000, 0x4000_0000].map(|g| memory_type(&m, g, 0)), [UC; 3]);
        assert_eq!(memory_type(&m, 0x10_0000, 1), WC);
    }

    /// M2: WRMSR to IA32_PAT and the MTRRs (SDM Vol.3 Tables 14-8, 14-10 and 14.11.2): only the defined encodings in every
    /// type field, no reserved bit; IA32_MTRRCAP is read-only.
    #[test]
    fn memory_type_msrs_refuse_reserved_encodings() {
        let ok = |index: u32, v: u64| write_ok(&profile::MSRS[slot(index)], v, 0x8000_0011, 0xd00);
        for byte in 0..=255u64 {
            let pat = matches!(byte, 0 | 1 | 4 | 5 | 6 | 7);
            let mtrr = matches!(byte, 0 | 1 | 4 | 5 | 6);
            for k in 0..8 {
                let v = 0x0606_0606_0606_0606 & !(0xff << (8 * k)) | byte << (8 * k);
                assert_eq!(ok(MSR_PAT, v), pat, "PAT {v:#x}");
                for fixed in [0x250, 0x258, 0x259, 0x268, 0x26f] {
                    assert_eq!(ok(fixed, v), mtrr, "{fixed:#x} {v:#x}");
                }
            }
            assert_eq!(ok(MSR_MTRR_DEF_TYPE, 0xc00 | byte), mtrr);
            assert_eq!(ok(0x200, 0x8000_0000 | byte), mtrr);
        }
        for bit in 0..64 {
            let b = 1u64 << bit;
            assert_eq!(ok(MSR_MTRR_DEF_TYPE, 6 | b), matches!(bit, 1 | 2 | 10 | 11), "DEF_TYPE bit {bit}");
            assert_eq!(ok(0x200, 6 | b), matches!(bit, 1 | 2 | 12..=39), "PHYSBASE bit {bit}");
            assert_eq!(ok(0x201, b), matches!(bit, 11..=39), "PHYSMASK bit {bit}");
        }
        assert_ne!(profile::MSRS[slot(MTRRCAP)].access, access::RW);
    }

    /// M2 addendum: the firmware's MTRRs follow guest RAM (`cpu_profile.py`'s `mtrr_state(ram)`, as vectors), and for
    /// every RAM size M1 allows (2 MiB multiples to 3 GiB) every RAM page is WB by the MTRRs and every other address
    /// below 4 GiB UC: the legacy hole, [ram, 4 GiB) with the I/O APIC and the local APIC, at 1, 2 and 3 GiB and all others.
    #[test]
    fn firmware_mtrrs_cover_guest_ram() {
        for &(ram, want) in v::MTRR_FOR_RAM.iter() {
            let mut m = reset_msrs(false);
            firmware_mtrrs(&mut m, ram);
            for (index, value) in want {
                assert_eq!(m[slot(index)], value, "RAM {ram:#x}: MSR {index:#x}");
            }
        }
        let mut m = reset_msrs(true);
        firmware_mtrrs(&mut m, 2 << 30);
        assert_eq!(m, reset_msrs(true), "the profile's state is the one for its L = 2 GiB");
        let block = 2u64 << 20;
        for ram in (1..=1536).map(|k| k * block) {
            let mut m = reset_msrs(false);
            firmware_mtrrs(&mut m, ram);
            for page in (0..0xa_0000).step_by(0x1000) {
                assert_eq!(mtrr_type(&m, page), WB, "RAM {ram:#x}: {page:#x}");
            }
            for page in (0xa_0000..0x10_0000).step_by(0x1000) {
                assert_eq!(mtrr_type(&m, page), UC, "RAM {ram:#x}: {page:#x}");
            }
            // Every range is 2 MiB aligned, so each 2 MiB block has one type: its first and last page.
            for start in [0x10_0000].into_iter().chain((block..1 << 32).step_by(block as usize)) {
                let want = if start < ram { WB } else { UC };
                for gpa in [start, (start | (block - 1)) & !0xfff] {
                    assert_eq!(mtrr_type(&m, gpa), want, "RAM {ram:#x}: {gpa:#x}");
                }
            }
            if [1 << 30, 2 << 30, 3 << 30].contains(&ram) {
                assert_eq!([ram - 0x1000, ram, 0xfec0_0000, 0xfee0_0000, 0xffff_f000].map(|g| mtrr_type(&m, g)), [WB, UC, UC, UC, UC]);
            }
        }
    }

    #[test]
    fn reset_matches_the_model() {
        let (ap, bsp) = (reset_msrs(false), reset_msrs(true));
        for &(index, want_ap, want_bsp) in v::RESET.iter() {
            let i = msr(index).unwrap();
            assert_eq!((ap[i], bsp[i]), (want_ap, want_bsp), "{index:#x}");
        }
        assert_eq!(v::RESET.len(), profile::MSRS.iter().filter(|m| read_ok(m)).count());
        // CR4: the profile's mask (bits whose CPUID feature is advertised).
        assert_eq!(profile::CR4_VALID, 0x1507bc);
    }
}
