//! Shadow fault-around (docs/restructure/fault-around-plan.md, chunk 2): at a demand shadow install of linear page `va`,
//! the resident also installs the shadows of the other pages of `va`'s aligned window of W pages inside the same guest
//! leaf table (or the same 2 MiB leaf) that the guest tables already justify: entries present, reserved-bit clean and
//! already accessed (A = 1), each read-only unless its entry has D = 1 and grants the write. Nothing is written to the
//! guest tables, so every A and D bit reads back as without the fill (SDM Vol.3 4.8, 4.10.2.3: a TLB may cache any
//! translation the tables permit; plan section 4).
//!
//! This module holds the rules that need no machine: the window, the neighbour rule of a 4 KiB leaf's entry and the
//! counters' layout. The resident's `execution/fault_around.rs` applies them (RAM, memory type, the writable protocol,
//! split locks, the install).
use crate::paging::{Control, PA_MASK};

/// The window sizes `--fault-around` accepts (pages; 0 is off). The default is Linux's own `fault_around_bytes` (64 KiB):
/// the fifteen entries Linux maps young at a file fault are then the window of the retry that follows.
pub const WINDOWS: [u64; 6] = [0, 8, 16, 32, 64, 512];
pub const DEFAULT_WINDOW: u64 = 16;
/// Whether `w` is a window `Control::fault_around` may hold.
pub const fn valid(w: u64) -> bool {
    matches!(w, 0 | 8 | 16 | 32 | 64 | 512)
}
/// The leaf indices of `va`'s window of `w` pages (a power of two, at most 512) inside its 2 MiB region: the W-aligned
/// run holding `va`'s index (aligned, not centred: one or two cache lines of PTEs, and how Linux populates).
pub const fn window(va: u64, w: u64) -> core::ops::Range<usize> {
    let i = (va >> 12 & 511) as usize;
    let w = if w == 0 || w > 512 { 1 } else { w as usize };
    let first = i & !(w - 1);
    first..first + w
}

/// Supervisor-space neighbours are never installed writable (plan 3.6): a writable fill of the kernel's direct map (2 MiB,
/// RW, A = D = 1) would set writable bits on many frames per abort (page-cache frames about to be executed among them) and
/// overflow the per-vCPU reverse map of writable shadows into full resets (docs/writable-drop.md). And a supervisor
/// neighbour that would be writable but for this rule is not installed at all (review 9.1, B2): the kernel writes such
/// pages first (`clear_page_rep` through the direct map), and a write to a read-only shadow costs an upgrade abort with
/// break-before-make, more than the miss it replaces.
pub const SUPERVISOR_WRITABLE: bool = false;

/// A 4 KiB leaf's neighbour entry that may be shadowed: its frame, PAT index and G bit, and whether a write through it is
/// granted with D already set (the shadow may then be writable, memory type and the writable protocol permitting).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Page {
    pub gpa: u64,
    pub pat: u8,
    pub global: bool,
    pub writable: bool,
}

/// The neighbour rule of a 4 KiB leaf's entry `entry` (plan 3.3 item 2): under the controls `c` of the demand access and
/// the R/W and U/S bits of the levels above the leaf (`Translation::parent_rw`, `parent_user`). A skip names its counter.
/// Present (bit 0); reserved bits 51:40 clear, and bit 63 when `nxe` is 0 (a reserved entry is a #PF on use, and no
/// TLB entry is made for it, 4.10.2.2); A set (bit 5); at CPL 3 a user page (U/S at every level). The write grant is
/// `walk_once`'s leaf rule for a write (every level's R/W, or CR0.WP = 0 at CPL 0), and the shadow may be writable only
/// with D (bit 6) set as well: a write through an entry cached with D = 0 must set D first (4.8), so such a page is
/// installed read-only and its first write aborts and walks as today.
pub const fn pte(entry: u64, c: &Control, parent_rw: bool, parent_user: bool) -> Result<Page, usize> {
    if entry & 1 == 0 {
        return Err(count::NOT_PRESENT);
    }
    if entry & 0x000f_ff00_0000_0000 != 0 || (c.nxe == 0 && entry >> 63 != 0) {
        return Err(count::RESERVED);
    }
    if entry & crate::paging::ACCESSED == 0 {
        return Err(count::NOT_ACCESSED);
    }
    let user = parent_user && entry & 4 != 0;
    if c.cpl == 3 && !user {
        return Err(count::SUPERVISOR_PAGE);
    }
    let rw = parent_rw && entry & 2 != 0;
    // `cpl == 0` is the supervisor case because the walker refuses CPL 1 and 2 (`walk_once`, `Fault::Unsupported`).
    let grant = rw || (c.cpl == 0 && c.wp == 0);
    Ok(Page {
        gpa: entry & PA_MASK,
        pat: ((entry >> 7 & 1) << 2 | entry >> 3 & 3) as u8,
        global: entry & 256 != 0,
        writable: grant && entry & crate::paging::DIRTY != 0,
    })
}

/// `cache::Control::fault_around_counts`: plain counters in every build (`k1.fault_around` in the host's stats).
pub mod count {
    /// Demand installs that ran a fill (the window was looked at).
    pub const FILLS: usize = 0;
    /// Neighbour shadows installed: in the user space, in the supervisor space, from a 2 MiB leaf, writable.
    pub const USER: usize = 1;
    pub const SUPERVISOR: usize = 2;
    pub const LARGE: usize = 3;
    pub const WRITABLE: usize = 4;
    /// Neighbours skipped, by reason: shadowed already; the entry not present, with a reserved bit set, with A = 0, a
    /// supervisor page at CPL 3; not guest RAM; UC memory; held by a peer's split lock; a non-global page in the kept half.
    pub const SHADOWED: usize = 5;
    pub const NOT_PRESENT: usize = 6;
    pub const RESERVED: usize = 7;
    pub const NOT_ACCESSED: usize = 8;
    pub const SUPERVISOR_PAGE: usize = 9;
    pub const NOT_RAM: usize = 10;
    pub const UNCACHED: usize = 11;
    pub const SPLIT: usize = 12;
    pub const KEPT: usize = 13;
    /// Installed read-only although D = 1 and the write is granted, by reason: WT or WP memory (writes there run alone);
    /// the frame holds translations (the writable protocol, plan 3.3 item 6). And skipped: a supervisor-space neighbour
    /// with D = 1 and the write granted ([`SUPERVISOR_WRITABLE`]).
    pub const NOT_WRITABLE_TYPE: usize = 14;
    pub const TRANSLATED: usize = 15;
    pub const SUPERVISOR_READ_ONLY: usize = 16;
    /// Demand installs that ran no fill though the window was on: a step block (UC), a data breakpoint armed.
    pub const DECLINED: usize = 17;
    pub const COUNT: usize = 18;
    pub const NAMES: [&str; COUNT] = [
        "fills", "user", "supervisor", "large", "writable", "shadowed", "not_present", "reserved", "not_accessed",
        "supervisor_page", "not_ram", "uncached", "split", "kept", "not_writable_type", "translated",
        "supervisor_read_only", "declined",
    ];
}

#[cfg(test)]
mod tests {
    use super::*;

    const C0: Control = Control { cr3: 0, cpl: 0, wp: 1, nxe: 1, smep: 0 };

    #[test]
    fn windows_are_aligned_runs_inside_the_region() {
        assert_eq!(window(0x20_3000, 16), 0..16);
        assert_eq!(window(0x20_f000, 16), 0..16);
        assert_eq!(window(0x21_0000, 16), 16..32);
        assert_eq!(window(0x10_3000, 16), 256..272);
        assert_eq!(window(0x3f_f000, 8), 504..512);
        assert_eq!(window(0xffff_8000_0012_3000, 512), 0..512);
        assert_eq!(window(0x20_5000, 0), 5..6);
        assert!(WINDOWS.iter().all(|&w| valid(w)) && !valid(4) && !valid(1024) && valid(DEFAULT_WINDOW));
    }

    /// The rule table (plan 7, Tier 0): present, reserved, A, U/S at CPL 3, R/W, WP at CPL 0 and D against what a walk of the
    /// same entry grants.
    #[test]
    fn neighbour_rule_table() {
        let frame = 0x1234_5000u64;
        // Present, R/W, U/S, A, D: a writable user page.
        let all = frame | 0x67;
        assert_eq!(pte(all, &C0, true, true), Ok(Page { gpa: frame, pat: 0, global: false, writable: true }));
        assert_eq!(pte(all & !1, &C0, true, true), Err(count::NOT_PRESENT));
        // A PROT_NONE or NUMA-hint shape: P = 0 with A, D and a frame.
        assert_eq!(pte(frame | 0x66, &C0, true, true), Err(count::NOT_PRESENT));
        for bit in [40, 45, 51] {
            assert_eq!(pte(all | 1 << bit, &C0, true, true), Err(count::RESERVED), "bit {bit}");
        }
        assert_eq!(pte(all | 1 << 63, &C0, true, true).map(|p| p.gpa), Ok(frame), "XD is not reserved with NXE");
        assert_eq!(pte(all | 1 << 63, &Control { nxe: 0, ..C0 }, true, true), Err(count::RESERVED));
        // A = 0 is never installed, whatever else the entry holds (M1).
        for low in 0..0x200u64 {
            let e = frame | low | 1;
            if low & 0x20 == 0 {
                assert_eq!(pte(e, &C0, true, true), Err(count::NOT_ACCESSED), "{e:#x}");
                assert_eq!(pte(e, &Control { cpl: 3, ..C0 }, true, true), Err(count::NOT_ACCESSED), "{e:#x}");
            }
        }
        // D = 0: never writable (M2), at either CPL, whatever R/W and WP say.
        for (cpl, wp) in [(0, 0), (0, 1), (3, 1), (3, 0)] {
            let c = Control { cpl, wp, ..C0 };
            assert_eq!(pte(all & !0x40, &c, true, true).map(|p| p.writable), Ok(false), "cpl {cpl} wp {wp}");
        }
        // U/S at CPL 3: the leaf's and the parents' (M7, M12); CPL 0 reads supervisor and user pages alike (no SMAP).
        let c3 = Control { cpl: 3, ..C0 };
        assert_eq!(pte(all & !4, &c3, true, true), Err(count::SUPERVISOR_PAGE));
        assert_eq!(pte(all, &c3, true, false), Err(count::SUPERVISOR_PAGE));
        assert!(pte(all & !4, &C0, true, false).is_ok());
        // R/W (M8): the leaf's and the parents'; at CPL 0 with WP = 0 the write is granted anyway, at CPL 3 never.
        assert_eq!(pte(all & !2, &c3, true, true).map(|p| p.writable), Ok(false));
        assert_eq!(pte(all, &c3, false, true).map(|p| p.writable), Ok(false));
        assert_eq!(pte(all & !2, &Control { wp: 1, ..C0 }, true, true).map(|p| p.writable), Ok(false));
        assert_eq!(pte(all & !2, &Control { wp: 0, ..C0 }, true, true).map(|p| p.writable), Ok(true));
        assert_eq!(pte(all & !2, &Control { wp: 0, ..c3 }, true, true).map(|p| p.writable), Ok(false));
        // PAT index from bits 7 (PAT), 4 (PCD), 3 (PWT); G from bit 8.
        assert_eq!(pte(all | 0x80, &C0, true, true).map(|p| p.pat), Ok(4));
        assert_eq!(pte(all | 0x10, &C0, true, true).map(|p| p.pat), Ok(2));
        assert_eq!(pte(all | 0x08, &C0, true, true).map(|p| p.pat), Ok(1));
        assert_eq!(pte(all | 0x100, &C0, true, true).map(|p| p.global), Ok(true));
        // The frame is bits 39:12 alone.
        assert_eq!(pte(all | 0xfff, &C0, true, true).map(|p| p.gpa), Ok(frame));
        // Against the walk: every combination of the leaf's low bits under every CPL/WP, the rule grants a writable
        // shadow only where a write walk of the same entry would succeed and leave D as it was.
        for low in 0..0x80u64 {
            for (cpl, wp) in [(0, 0), (0, 1), (3, 0), (3, 1)] {
                let c = Control { cpl, wp, ..C0 };
                let e = frame | low;
                if let Ok(p) = pte(e, &c, true, true) {
                    let walk_writes = (e & 2 != 0 || (cpl == 0 && wp == 0)) && (cpl == 0 || e & 4 != 0);
                    assert_eq!(p.writable, walk_writes && e & 0x40 != 0, "{e:#x} cpl {cpl} wp {wp}");
                    assert!(e & 0x21 == 0x21);
                }
            }
        }
        assert_eq!(count::NAMES.len(), count::COUNT);
    }
}
