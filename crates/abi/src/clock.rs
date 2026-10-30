//! The clock service's conversions (plan L5, the minimum X3 needs): every virtual clock is an exact rational function of
//! one time base V, the Arm virtual count CNTVCT_EL0 (the host sets the vtimer offset to 0, so V is the host's
//! `mach_absolute_time` and every vCPU reads the same V), counting at CNTFRQ_EL0 (24 MHz on the M4, read at run time and
//! never assumed). The presented CPU's clocks (CPU 13; CPUID leaf 0x15 of [`crate::cpu_profile`]):
//! - the core crystal clock, `CRYSTAL_HZ` = leaf 0x15 ECX (24 MHz), which also drives the local APIC timer before its
//!   divider (SDM Vol.3 11.5.4: "the core crystal clock frequency when enumerated in CPUID leaf 15H");
//! - the TSC at `cpu_profile::TSC_HZ` (1 GHz) = crystal x leaf 0x15 EBX / EAX (125 / 3), exactly (checked below); leaf
//!   0x16 is not advertised.
//!
//! Counts are floors of V x rate / CNTFRQ, so each is monotonic in V and a clock edge never comes before the V that
//! reaches it ([`count_at_crystal`] is its exact inverse). No clock pauses: V runs while a vCPU is halted or in the host.
use crate::cpu_profile;

const fn leaf_0x15() -> [u32; 4] {
    let mut i = 0;
    while i < cpu_profile::CPUID.len() {
        if cpu_profile::CPUID[i].0 == 0x15 {
            return cpu_profile::CPUID[i].2;
        }
        i += 1;
    }
    panic!("the profile has no leaf 0x15")
}
/// The core crystal clock (CPUID.15H:ECX).
pub const CRYSTAL_HZ: u64 = leaf_0x15()[2] as u64;
// TSC = crystal x EBX / EAX exactly (CPUID.15H: EAX denominator, EBX numerator).
const _: () = assert!(cpu_profile::TSC_HZ * leaf_0x15()[0] as u64 == CRYSTAL_HZ * leaf_0x15()[1] as u64);

/// floor(`count` x `hz` / `frequency`): the count of a clock at `hz` at V = `count` (V at `frequency`).
pub const fn scale(count: u64, hz: u64, frequency: u64) -> u64 {
    (count as u128 * hz as u128 / frequency as u128) as u64
}
/// Crystal-clock edges up to V = `count`.
pub const fn crystal(count: u64, frequency: u64) -> u64 {
    scale(count, CRYSTAL_HZ, frequency)
}
/// The first V at which the crystal count reaches `edges` (ceil(edges x frequency / CRYSTAL_HZ), saturated): the
/// exact inverse of [`crystal`] (`crystal(v) >= edges` iff `v >= count_at_crystal(edges)`).
pub const fn count_at_crystal(edges: u64, frequency: u64) -> u64 {
    let v = (edges as u128 * frequency as u128).div_ceil(CRYSTAL_HZ as u128);
    if v > u64::MAX as u128 { u64::MAX } else { v as u64 }
}
/// The common TSC count at V = `count` (each vCPU adds its own offset, the TSC MSR's slot).
pub const fn tsc(count: u64, frequency: u64) -> u64 {
    scale(count, cpu_profile::TSC_HZ, frequency)
}

#[cfg(test)]
mod tests {
    extern crate std;
    use super::*;
    /// The M4's CNTFRQ and other rates a host could report.
    const RATES: [u64; 5] = [24_000_000, 19_200_000, 25_000_000, 1_000_000_000, 54_000_000];

    #[test]
    fn profile_rates() {
        assert_eq!(CRYSTAL_HZ, 24_000_000);
        assert_eq!(cpu_profile::TSC_HZ, 1_000_000_000);
    }
    #[test]
    fn crystal_and_inverse_are_exact() {
        for f in RATES {
            for edges in (0..5000).chain([u32::MAX as u64, 1 << 40, 123_456_789_012]) {
                let v = count_at_crystal(edges, f);
                assert!(crystal(v, f) >= edges, "{f} {edges}");
                assert!(v == 0 || crystal(v - 1, f) < edges, "{f} {edges}: not the first V");
            }
            for v in (0..5000u64).chain([1 << 50, u64::MAX / 2]) {
                let e = crystal(v, f);
                assert!(count_at_crystal(e, f) <= v && count_at_crystal(e + 1, f) > v);
            }
        }
    }
    #[test]
    fn tsc_is_the_crystal_times_125_over_3() {
        // At CNTFRQ = the crystal (the M4), V is the crystal count and the TSC is floor(V x 125 / 3); at any rate the TSC
        // advances 125 / 3 times the crystal, to within the floors.
        for v in (0..100_000u64).chain([1 << 40, 3 << 50]) {
            assert_eq!(tsc(v, CRYSTAL_HZ), (v as u128 * 125 / 3) as u64);
            assert_eq!(crystal(v, CRYSTAL_HZ), v);
        }
        for f in RATES {
            for v in [0u64, 1, 999, 1 << 33, 1 << 45] {
                let (t, c) = (tsc(v, f) as i128, crystal(v, f) as i128);
                assert!((3 * t - 125 * c).abs() < 3 + 125, "{f} {v}");
            }
        }
    }
    #[test]
    fn monotonic() {
        for f in RATES {
            let mut last = (0, 0);
            for v in (0..200_000u64).step_by(7) {
                let now = (crystal(v, f), tsc(v, f));
                assert!(now.0 >= last.0 && now.1 >= last.1);
                last = now;
            }
        }
    }
    /// The host's CNTFRQ as macOS EL0 reads it (1 GHz on the M4 with macOS 26; the guest's, which the resident reads, may
    /// differ): the rates above include it.
    #[test]
    #[cfg(target_arch = "aarch64")]
    fn host_frequency() {
        let f: u64;
        unsafe { core::arch::asm!("mrs {}, cntfrq_el0", out(reg) f) };
        assert!(RATES.contains(&f), "CNTFRQ {f}");
        std::println!("CNTFRQ_EL0 = {f}");
    }
}
