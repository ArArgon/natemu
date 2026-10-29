//! The presented CPU's architectural answers (`crate::cpu_profile`, generated from `tools/models/cpu_profile.json`):
//! CPUID with its dynamic fields, and the MSR checks the resident's RDMSR/WRMSR transactions apply. Pure functions of
//! the profile and the vCPU's state; the resident owns the side effects. The tests replay `cpu_profile.py`'s answers
//! (`cpu_profile::vectors`), so this module and the Python judge cannot drift apart.
use crate::cpu_profile::{self as profile, Msr, access, rule};

pub const MSR_TSC: u32 = 0x10;
pub const MSR_APIC_BASE: u32 = 0x1b;
pub const MSR_UCODE_REV: u32 = 0x8b;
pub const MSR_MISC_ENABLE: u32 = 0x1a0;
pub const MSR_PAT: u32 = 0x277;
pub const MSR_MTRR_DEF_TYPE: u32 = 0x2ff;
pub const MSR_EFER: u32 = 0xc000_0080;
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
pub fn msr(index: u32) -> Option<usize> {
    profile::MSRS.iter().position(|m| (m.index..=m.last).contains(&index))
}
/// RDMSR is allowed. x2APIC registers are not: they exist only while IA32_APIC_BASE.EXTD = 1, which no write sets
/// (changes of IA32_APIC_BASE are refused until the LAPIC exists).
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
        0xd if sub == 0 => ebx = if d.xcr0 & 4 != 0 { 832 } else { 576 },
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
