//! X3: the local APIC's per-vCPU record and its pure rules (SDM Vol.3 ch. 11, Advanced Programmable Interrupt
//! Controller; numbers below are that chapter's sections). The resident owns the record and the side effects
//! (`runtime/src/execution/lapic.rs`); these functions are the arithmetic it applies, unit-tested here.
//!
//! The presented APIC (CPU 16 profile): integrated, version 0x14, six LVT entries (timer, thermal, performance counter,
//! LINT0, LINT1, error: no CMCI, the profile clears MCE/MCA), no directed EOI (version bit 24 clear, so SVR bit 12 is
//! reserved), x2APIC advertised (CPUID.1:ECX.21), TSC-deadline not (CPUID.1:ECX.24 clear: LVT timer bit 18 is reserved).
//! The timer counts the core crystal clock ([`crate::clock::CRYSTAL_HZ`], 24 MHz) through its divider.
//!
//! Model-specific choices (each permitted by the SDM): the xAPIC ID register is read-only (11.4.6: writing it is
//! processor specific; IDs are the vCPU numbers); APR and RRD are not implemented (Table 11-1 note 1: not on Pentium 4
//! and later; they read 0); acceptance into the IRR and delivery to the core are one resident step, so no spurious
//! interrupt is ever generated (11.9: a spurious vector comes from an interrupt masked between them) and delivery status
//! always reads idle; lowest-priority delivery picks the lowest-numbered member of the destination set (11.6.2.4: the
//! arbitration is model specific; any member is a legal recipient); a fixed or lowest-priority message to a
//! software-disabled APIC is not accepted (11.4.7.2: it responds to INIT, NMI, SMI and start-up only); the timer's
//! period is the initial count in divided ticks (the count is reloaded as it reaches 0, 11.5.4); a divide configuration
//! written while it counts keeps the current count and counts on at the new rate; a mode change counts no expiry that
//! passed under the old mode, and a one-shot count that reached 0 stays 0 until the initial count is written.
pub const VERSION: u64 = 0x0005_0014;
pub const SVR_RESET: u64 = 0xff;
pub const SVR_ENABLE: u64 = 1 << 8;
/// The spurious vector and software enable; focus-processor checking (bit 9, not supported on this generation) and
/// EOI-broadcast suppression (bit 12, not enumerated) are reserved.
pub const SVR_WRITABLE: u64 = 0x1ff;
pub const DFR_RESET: u64 = 0xffff_ffff;
pub const MASKED: u64 = 1 << 16;
pub const PERIODIC: u64 = 1 << 17;
/// The LVT entries in register order (xAPIC 0x320 + 0x10 i, x2APIC 0x832 + i).
pub const TIMER: usize = 0;
pub const THERMAL: usize = 1;
pub const PMC: usize = 2;
pub const LINT0: usize = 3;
pub const LINT1: usize = 4;
pub const ERROR: usize = 5;
/// Writable bits of each LVT entry (11.5.1, Figure 11-8): vector, delivery mode (not timer, error), pin polarity and
/// trigger mode (LINT0/1), mask, timer mode (one-shot or periodic only).
pub const LVT_WRITABLE: [u64; 6] = [0x300ff, 0x107ff, 0x107ff, 0x1a7ff, 0x1a7ff, 0x100ff];
/// Defined bits (writable, delivery status 12 and LINT remote IRR 14, read-only and 0 here): the rest is reserved, and an
/// x2APIC write with a reserved bit set is #GP (11.12.1.3).
pub const LVT_DEFINED: [u64; 6] = [0x310ff, 0x117ff, 0x117ff, 0x1f7ff, 0x1f7ff, 0x110ff];
/// ICR low: vector, delivery mode, destination mode, level, trigger mode, shorthand (delivery status 12 is read-only in
/// xAPIC mode and reserved in x2APIC mode).
pub const ICR_WRITABLE: u64 = 0xc_cfff;
/// The timer's divide configuration bits (0, 1, 3).
pub const DCR_WRITABLE: u64 = 0xb;
/// ESR bits (11.5.3): an illegal vector (0..15) sent or received, an access to an unimplemented xAPIC register.
pub const SEND_ILLEGAL: u64 = 1 << 5;
pub const RECEIVE_ILLEGAL: u64 = 1 << 6;
pub const ILLEGAL_REGISTER: u64 = 1 << 7;

/// ICR fields (11.6.1).
pub mod icr {
    pub const FIXED: u64 = 0;
    pub const LOWEST: u64 = 1;
    pub const SMI: u64 = 2;
    pub const NMI: u64 = 4;
    pub const INIT: u64 = 5;
    pub const STARTUP: u64 = 6;
    pub const LOGICAL: u64 = 1 << 11;
    pub const ASSERT: u64 = 1 << 14;
    pub const LEVEL: u64 = 1 << 15;
    pub const SELF: u64 = 1;
    pub const ALL: u64 = 2;
    pub const OTHERS: u64 = 3;
    pub fn mode(icr: u64) -> u64 {
        icr >> 8 & 7
    }
    pub fn shorthand(icr: u64) -> u64 {
        icr >> 18 & 3
    }
}

/// One vCPU's local APIC state, private to its resident (a record in its state page, [`crate::execution::LAPIC_OFFSET`]).
/// The IRR and TMR are the vCPU's shared [`crate::execution::cache::Events`] `vectors` and `tmr` (senders set bits there,
/// L4a: the TMR with the IRR bit, as the I/O APIC's level-triggered messages need), the TPR is the
/// bank's `Cpu::cr8` (bits 7:0; CR8 is TPR[7:4]), and what senders need to match a destination is published in
/// [`crate::execution::cache::Events::apic`] ([`published`]).
#[repr(C)]
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Lapic {
    pub svr: u64,
    /// xAPIC LDR (bits 31:24) and DFR (bits 31:28 written, the rest reads 1).
    pub ldr: u64,
    pub dfr: u64,
    pub isr: [u64; 4],
    pub lvt: [u64; 6],
    /// ICR low (bits 31:0) and high (bits 63:32: the xAPIC destination in 63:56, the x2APIC one in 63:32).
    pub icr: u64,
    /// The ESR as last updated by a write to it, and the errors detected since (11.5.3).
    pub esr: u64,
    pub errors: u64,
    /// Timer: initial count (0: stopped), divide configuration, the crystal count at the initial-count write, expiries
    /// taken since, and the V (CNTVCT) of the next expiry (`u64::MAX`: none to wait for).
    pub initial: u64,
    pub dcr: u64,
    pub start: u64,
    pub fired: u64,
    pub deadline: u64,
    /// 1 once a one-shot count has reached 0: it stays 0, with no further expiry, until the initial count is written
    /// again (11.5.4), whatever later LVT mode writes say.
    pub spent: u64,
    /// CNTFRQ_EL0 as the resident read it at start (V's rate; 0 before).
    pub frequency: u64,
    /// L5: the periodic timer's lost-tick policy (1: reinject, 0: discard; set by the host, kept across APIC resets),
    /// the expiries still to deliver under reinject, and since start the expiries that merged (with one taken, or into
    /// a pending IRR bit) and those reinjected.
    pub reinject: u64,
    pub backlog: u64,
    pub missed: u64,
    pub reinjected: u64,
}
/// The state at power-up, reset and after a disable through IA32_APIC_BASE (11.4.7.1, "local APIC state after power-up
/// or reset"; 11.12.5.1: entering the disabled state resets it).
pub const RESET: Lapic = Lapic {
    svr: SVR_RESET,
    ldr: 0,
    dfr: DFR_RESET,
    isr: [0; 4],
    lvt: [MASKED; 6],
    icr: 0,
    esr: 0,
    errors: 0,
    initial: 0,
    dcr: 0,
    start: 0,
    fired: 0,
    deadline: u64::MAX,
    spent: 0,
    frequency: 0,
    reinject: 0,
    backlog: 0,
    missed: 0,
    reinjected: 0,
};

/// The highest vector set in a 256-bit register.
pub fn highest(bits: &[u64; 4]) -> Option<u64> {
    (0..4).rev().find_map(|w| match bits[w] {
        0 => None,
        b => Some(64 * w as u64 + 63 - u64::from(b.leading_zeros())),
    })
}
/// PPR from the TPR and ISR (11.8.3.1): the TPR if its class is at least the highest in-service vector's, else that
/// vector's class.
pub fn ppr(tpr: u64, isr: &[u64; 4]) -> u64 {
    let isrv = highest(isr).unwrap_or(0);
    if tpr >> 4 & 0xf >= isrv >> 4 {
        tpr & 0xff
    } else {
        isrv & 0xf0
    }
}
/// The interrupt the APIC delivers to the core (11.8.4): the highest pending vector, if its class is above the PPR's.
pub fn deliverable(irr: &[u64; 4], tpr: u64, isr: &[u64; 4]) -> Option<u64> {
    let v = highest(irr)?;
    (v >> 4 > ppr(tpr, isr) >> 4).then_some(v)
}
/// The timer's divider from the divide configuration register (11.5.4, Figure 11-10).
pub fn divisor(dcr: u64) -> u64 {
    match dcr & 3 | dcr >> 1 & 4 {
        7 => 1,
        v => 2 << v,
    }
}
/// The current count after `ticks` divided ticks since `initial` was loaded.
pub fn current_count(initial: u64, periodic: bool, ticks: u64) -> u64 {
    match (initial, periodic) {
        (0, _) => 0,
        (n, true) => n - ticks % n,
        (n, false) => n.saturating_sub(ticks),
    }
}
/// Times the count has reached 0 after `ticks` (one-shot: at most once).
pub fn expiries(initial: u64, periodic: bool, ticks: u64) -> u64 {
    match (initial, periodic) {
        (0, _) => 0,
        (n, true) => ticks / n,
        (n, false) => u64::from(ticks >= n),
    }
}
/// The divided tick at which expiry `fired + 1` happens, if any.
pub fn next_expiry(initial: u64, periodic: bool, fired: u64) -> Option<u64> {
    match (initial, periodic) {
        (0, _) => None,
        (n, true) => fired.checked_add(1)?.checked_mul(n),
        (n, false) => (fired == 0).then_some(n),
    }
}

/// x2APIC logical ID (11.12.10.2): cluster ID (bits 31:16) = x2APIC ID[19:4], bit ID[3:0] in bits 15:0.
pub fn x2apic_ldr(id: u64) -> u64 {
    (id >> 4) << 16 | 1 << (id & 0xf)
}
/// What senders read in a target's published word: its LDR (bits 31:0), DFR model (bits 35:32), and whether it is
/// hardware enabled (IA32_APIC_BASE.EN), in x2APIC mode and software enabled.
pub const HW_ENABLED: u64 = 1 << 40;
pub const X2APIC: u64 = 1 << 41;
pub const SW_ENABLED: u64 = 1 << 42;
/// L4a: the APIC passes an ExtINT at LINT0 to the core (LVT LINT0 unmasked with delivery mode ExtINT, or the APIC
/// hardware disabled: the 8259 wired through), so the chipset rings it when the 8259's output rises.
pub const EXTINT: u64 = 1 << 43;
/// L4a: LVT LINT1 is unmasked with delivery mode NMI (the MADT's LINT1 wiring): the board's NMI input reaches the core.
pub const LINT1_NMI: u64 = 1 << 44;
/// L4a: LVT LINT0 is unmasked, delivery mode fixed, edge-triggered: a rising LINT0 input is accepted as the edge
/// interrupt whose vector is in bits 55:48 (11.5.1: "fixed" delivers the LVT vector; QEMU instead passes the 8259 through
/// in any unmasked mode). A level-triggered fixed LINT0 (its LVT remote IRR) is not built.
pub const LINT0_FIXED: u64 = 1 << 45;
pub fn published(apic_base: u64, l: &Lapic, id: u64) -> u64 {
    let (en, extd) = (apic_base >> 11 & 1 != 0, apic_base >> 10 & 1 != 0);
    let ldr = if extd {
        x2apic_ldr(id)
    } else {
        l.ldr & 0xff00_0000
    };
    ldr | (l.dfr >> 28 & 0xf) << 32
        | if en { HW_ENABLED } else { 0 }
        | if en && extd { X2APIC } else { 0 }
        | if en && l.svr & SVR_ENABLE != 0 {
            SW_ENABLED
        } else {
            0
        }
        | if !en || l.lvt[LINT0] & (MASKED | 0x700) == 0x700 {
            EXTINT
        } else {
            0
        }
        | if en && l.lvt[LINT1] & (MASKED | 0x700) == 0x400 {
            LINT1_NMI
        } else {
            0
        }
        | if en && l.lvt[LINT0] & (MASKED | 0x700 | 0x8000) == 0 {
            LINT0_FIXED | (l.lvt[LINT0] & 0xff) << 48
        } else {
            0
        }
}
/// The target `id` with published word `word` is in the destination `dest` (11.6.2): physical (the ID; all-ones
/// broadcast, 11.6.2.1) or logical (11.6.2.2: xAPIC flat: LDR[31:24] AND dest, so all ones reaches every APIC with a
/// logical ID bit set, not one whose LDR is 0; cluster: LDR[31:28] = dest[7:4] and LDR[27:24] AND dest[3:0], all ones
/// reaching every cluster; x2APIC: the cluster equal and the bit set, all ones a broadcast); `x2apic` is the sender's
/// mode (8- or 32-bit destinations).
pub fn in_destination(word: u64, id: u64, dest: u64, logical: bool, x2apic: bool) -> bool {
    let all = if x2apic { 0xffff_ffff } else { 0xff };
    if !logical {
        return dest == all || dest == id;
    }
    let ldr = word & 0xffff_ffff;
    if x2apic {
        return dest == all || ldr >> 16 == dest >> 16 && ldr & dest & 0xffff != 0;
    }
    let (ldr, model) = (ldr >> 24, word >> 32 & 0xf);
    match model {
        0xf => ldr & dest != 0,
        0 => (dest == all || ldr >> 4 == dest >> 4) && ldr & dest & 0xf != 0,
        _ => false, // reserved models select nothing
    }
}

#[cfg(test)]
mod tests {
    extern crate std;
    use super::*;
    use std::vec::Vec;
    fn set(v: &[u64]) -> [u64; 4] {
        let mut b = [0; 4];
        v.iter().for_each(|&v| b[v as usize / 64] |= 1 << (v % 64));
        b
    }
    #[test]
    fn priority() {
        assert_eq!(highest(&set(&[])), None);
        assert_eq!(highest(&set(&[0x31, 0xfe, 0x40])), Some(0xfe));
        // PPR: TPR when its class is at least the ISR's (subclass kept), else the ISR class.
        assert_eq!(ppr(0x45, &set(&[0x41])), 0x45);
        assert_eq!(ppr(0x35, &set(&[0x41])), 0x40);
        assert_eq!(ppr(0, &set(&[])), 0);
        // Deliverable: the class strictly above PPR's class.
        assert_eq!(deliverable(&set(&[0x41]), 0, &set(&[0x40])), None); // same class in service
        assert_eq!(deliverable(&set(&[0x51]), 0, &set(&[0x40])), Some(0x51));
        assert_eq!(deliverable(&set(&[0x51]), 0x50, &set(&[])), None); // TPR class 5 masks class 5
        assert_eq!(
            deliverable(&set(&[0x61, 0x51]), 0x50, &set(&[])),
            Some(0x61)
        );
        assert_eq!(deliverable(&set(&[0x20]), 0x1f, &set(&[])), Some(0x20));
        assert_eq!(deliverable(&set(&[0x20]), 0x20, &set(&[])), None);
    }
    #[test]
    fn divider() {
        let want = [
            (0, 2),
            (1, 4),
            (2, 8),
            (3, 16),
            (8, 32),
            (9, 64),
            (0xa, 128),
            (0xb, 1),
        ];
        for (dcr, d) in want {
            assert_eq!(divisor(dcr), d);
            assert_eq!(divisor(dcr | 4), d); // bit 2 is not part of the encoding
        }
    }
    #[test]
    fn timer_counts() {
        // One-shot: N, N-1, .., 1, then 0 at tick N, which is the one expiry.
        let n = 5;
        let one: Vec<u64> = (0..8).map(|t| current_count(n, false, t)).collect();
        assert_eq!(one, [5, 4, 3, 2, 1, 0, 0, 0]);
        assert_eq!(
            (0..8).map(|t| expiries(n, false, t)).collect::<Vec<_>>(),
            [0, 0, 0, 0, 0, 1, 1, 1]
        );
        assert_eq!(
            (next_expiry(n, false, 0), next_expiry(n, false, 1)),
            (Some(5), None)
        );
        // Periodic: reloaded as it reaches 0: period N, the count in N..=1.
        let per: Vec<u64> = (0..11).map(|t| current_count(n, true, t)).collect();
        assert_eq!(per, [5, 4, 3, 2, 1, 5, 4, 3, 2, 1, 5]);
        assert_eq!(
            (0..11).map(|t| expiries(n, true, t)).collect::<Vec<_>>(),
            [0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 2]
        );
        for k in 0..4 {
            let at = next_expiry(n, true, k).unwrap();
            assert_eq!(
                (expiries(n, true, at - 1), expiries(n, true, at)),
                (k, k + 1)
            );
        }
        assert_eq!(
            (
                current_count(0, true, 9),
                expiries(0, true, 9),
                next_expiry(0, false, 0)
            ),
            (0, 0, None)
        );
        assert_eq!(
            current_count(u32::MAX as u64, true, 1 << 40),
            u32::MAX as u64 - (1 << 40) % u32::MAX as u64
        );
    }
    #[test]
    fn destinations() {
        let l = |ldr, dfr| Lapic {
            ldr,
            dfr,
            svr: SVR_ENABLE,
            ..RESET
        };
        let flat = published(0xfee0_0800, &l(0x0400_0000, DFR_RESET), 2);
        assert_eq!(
            flat & (HW_ENABLED | X2APIC | SW_ENABLED),
            HW_ENABLED | SW_ENABLED
        );
        assert!(
            in_destination(flat, 2, 0x04, true, false)
                && in_destination(flat, 2, 0x0c, true, false)
        );
        assert!(!in_destination(flat, 2, 0x03, true, false));
        assert!(
            in_destination(flat, 2, 0xff, true, false)
                && in_destination(flat, 2, 0xff, false, false)
        );
        let unset = published(0xfee0_0800, &l(0, DFR_RESET), 3); // flat, LDR 0: no logical destination reaches it
        assert!(
            !in_destination(unset, 3, 0xff, true, false)
                && in_destination(unset, 3, 0xff, false, false)
        );
        assert!(
            in_destination(flat, 2, 2, false, false) && !in_destination(flat, 2, 3, false, false)
        );
        let cluster = published(0xfee0_0800, &l(0x2100_0000, 0x0fff_ffff), 1);
        assert!(
            in_destination(cluster, 1, 0x21, true, false)
                && in_destination(cluster, 1, 0x23, true, false)
        );
        assert!(
            !in_destination(cluster, 1, 0x11, true, false)
                && !in_destination(cluster, 1, 0x22, true, false)
        );
        assert!(in_destination(cluster, 1, 0xff, true, false));
        let x2 = published(0xfee0_0c00, &l(0, DFR_RESET), 0x13);
        assert_eq!(x2 & 0xffff_ffff, 0x1_0008);
        assert!(
            in_destination(x2, 0x13, 0x1_0008, true, true)
                && in_destination(x2, 0x13, 0x1_000c, true, true)
        );
        assert!(
            !in_destination(x2, 0x13, 0x0_0008, true, true)
                && in_destination(x2, 0x13, 0xffff_ffff, true, true)
        );
        assert!(
            in_destination(x2, 0x13, 0x13, false, true)
                && !in_destination(x2, 0x13, 0xff, false, true)
        );
        assert_eq!(
            published(0xfee0_0000, &l(0, DFR_RESET), 0) & (HW_ENABLED | SW_ENABLED),
            0
        );
        // L4a's LINT inputs: a hardware-disabled APIC passes the 8259 through (EXTINT whatever its LVT); LINT0 ExtINT,
        // fixed edge (with its vector), LINT1 NMI; masked or level-triggered fixed: none.
        let lint = |base, lint0, lint1| {
            published(
                base,
                &Lapic {
                    lvt: [MASKED, MASKED, MASKED, lint0, lint1, MASKED],
                    ..l(0, DFR_RESET)
                },
                0,
            ) >> 43
        };
        assert_eq!(lint(0xfee0_0000, MASKED, MASKED), 1);
        assert_eq!(lint(0xfee0_0800, MASKED | 0x700, MASKED | 0x400), 0);
        assert_eq!(lint(0xfee0_0800, 0x700, 0x400), 0b011);
        assert_eq!(lint(0xfee0_0800, 0x31, MASKED), 0x31 << 5 | 0b100);
        assert_eq!(lint(0xfee0_0800, 0x8031, 0x431), 0b010);
    }
}
