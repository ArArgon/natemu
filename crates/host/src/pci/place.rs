//! Launch-time BAR placement: the addresses a firmware would assign (natemu boots without one), chosen once before the
//! registry freezes. Pure, so the rule is pinned by unit tests.
//!
//! - Memory: function 0's memory BAR0 of at most 64 KiB keeps [`platform::bar`] (the 64 KiB stride over the window's
//!   first 2 MiB), so virtio layouts do not move. Every other memory BAR goes from [`platform::WINDOW`]`.0 + 2 MiB`
//!   upward, naturally aligned, largest first; 32- and 64-bit memory BARs share the 32-bit window (a 64-bit BAR below
//!   4 GiB is legal, PCI 3.0 6.2.5.1).
//! - I/O: [`platform::IO_WINDOW`], naturally aligned, largest first.
//!
//! Ties (equal lengths) go lowest (slot, function, BAR) first. A BAR that does not fit is a launch error naming it.
use super::{BARS, BarKind, FUNCTIONS, SLOTS, platform};

/// One launch-time BAR request: BAR `bar` of function `slot`.`function`, `len` bytes (a power of two, memory at least
/// 16, I/O at least 4: PCI 3.0 6.2.5.1).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Request {
    pub slot: u8,
    pub function: u8,
    pub bar: u8,
    pub kind: BarKind,
    pub len: u64,
}
/// A request and the base it was given.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Placed {
    pub request: Request,
    pub base: u64,
}

/// The stride region [`platform::bar`] covers: slots 0-31 at 64 KiB each.
const STRIDE_REGION: u64 = 0x20_0000;
/// The largest BAR0 that keeps its slot's stride address.
const STRIDE: u64 = 0x1_0000;

/// Bases for `requests`, in their order; `Err` naming the first BAR that is malformed or does not fit.
pub fn place(requests: &[Request]) -> crate::Result<Vec<Placed>> {
    for r in requests {
        let min = if r.kind == BarKind::Io { 4 } else { 16 };
        let last = usize::from(r.bar) + usize::from(r.kind == BarKind::Mem64);
        let at = usize::from(r.slot) < SLOTS && usize::from(r.function) < FUNCTIONS;
        if !at || last >= BARS || !r.len.is_power_of_two() || r.len < min {
            return Err(format!(
                "{}: at least {min} bytes, a power of two, in BAR0-BAR5 of a function on the bus",
                name(r)
            ));
        }
    }
    let mut base: Vec<Option<u64>> = requests
        .iter()
        .map(|r| {
            (r.kind != BarKind::Io && r.function == 0 && r.bar == 0 && r.len <= STRIDE)
                .then(|| u64::from(platform::bar(r.slot)))
        })
        .collect();
    let memory = (
        u64::from(platform::WINDOW.0) + STRIDE_REGION,
        u64::from(platform::WINDOW.1),
    );
    let io = (
        u64::from(platform::IO_WINDOW.0),
        u64::from(platform::IO_WINDOW.1) + 1,
    );
    for (window, io_space) in [(memory, false), (io, true)] {
        let mut order: Vec<usize> = (0..requests.len())
            .filter(|&i| base[i].is_none() && (requests[i].kind == BarKind::Io) == io_space)
            .collect();
        order.sort_by_key(|&i| {
            let r = &requests[i];
            (std::cmp::Reverse(r.len), r.slot, r.function, r.bar)
        });
        // Largest first from an aligned start: every later (smaller, power-of-two) BAR is aligned where the last ended.
        let mut next = window.0;
        for i in order {
            let r = &requests[i];
            let at = next.next_multiple_of(r.len);
            if at.checked_add(r.len).is_none_or(|end| end > window.1) {
                return Err(format!(
                    "{}: no room left in the {} window {:X}H-{:X}H",
                    name(r),
                    if io_space { "I/O" } else { "memory" },
                    window.0,
                    window.1 - 1
                ));
            }
            base[i] = Some(at);
            next = at + r.len;
        }
    }
    Ok(requests
        .iter()
        .zip(base)
        .map(|(&request, base)| Placed {
            request,
            base: base.expect("every request placed"),
        })
        .collect())
}

fn name(r: &Request) -> String {
    format!(
        "PCI {:02x}.{} BAR{} ({:?}, {:#x} bytes)",
        r.slot, r.function, r.bar, r.kind, r.len
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    fn req(slot: u8, function: u8, bar: u8, kind: BarKind, len: u64) -> Request {
        Request {
            slot,
            function,
            bar,
            kind,
            len,
        }
    }
    fn bases(requests: &[Request]) -> Vec<u64> {
        place(requests).unwrap().iter().map(|p| p.base).collect()
    }
    const W: u64 = platform::WINDOW.0 as u64;
    /// No two placed BARs of one space overlap, and each is naturally aligned inside its window.
    fn check(placed: &[Placed]) {
        for (i, a) in placed.iter().enumerate() {
            let (r, b) = (a.request, a.base);
            assert_eq!(b % r.len, 0, "{r:?} at {b:#x}: aligned");
            let (lo, hi) = if r.kind == BarKind::Io {
                (0xc000, 0x1_0000)
            } else {
                (W, u64::from(platform::WINDOW.1))
            };
            assert!(b >= lo && b + r.len <= hi, "{r:?} at {b:#x}: inside");
            for c in &placed[i + 1..] {
                let same_space = (c.request.kind == BarKind::Io) == (r.kind == BarKind::Io);
                assert!(
                    !same_space || b + r.len <= c.base || c.base + c.request.len <= b,
                    "{a:?} overlaps {c:?}"
                );
            }
        }
    }

    #[test]
    fn function0_memory_bar0_keeps_slot_stride() {
        // virtio's BAR0 (32 KiB) at slots 1, 2, 3 and 31; a 64 KiB BAR0 (64-bit) keeps its stride too
        let r = [
            req(1, 0, 0, BarKind::Mem32, 0x8000),
            req(2, 0, 0, BarKind::Mem32, 0x8000),
            req(3, 0, 0, BarKind::Mem32, 0x8000),
            req(31, 0, 0, BarKind::Mem32, 0x8000),
            req(5, 0, 0, BarKind::Mem64, 0x1_0000),
        ];
        assert_eq!(
            bases(&r),
            [W + 0x1_0000, W + 0x2_0000, W + 0x3_0000, W + 0x1f_0000, W + 0x5_0000]
        );
        // what keeps it: function 0, BAR0, memory, at most 64 KiB; anything else goes above the stride region
        let r = [
            req(4, 0, 0, BarKind::Mem32, 0x2_0000),
            req(6, 1, 0, BarKind::Mem32, 0x8000),
            req(7, 0, 1, BarKind::Mem32, 0x8000),
        ];
        assert!(bases(&r).iter().all(|&b| b >= W + 0x20_0000), "{:x?}", bases(&r));
    }

    #[test]
    fn others_naturally_aligned_above_2mib() {
        let r = [
            req(3, 0, 0, BarKind::Mem32, 0x8000),
            req(3, 0, 1, BarKind::Mem32, 0x1000),
            // 4 MiB: at the first 4 MiB-aligned address above 2 MiB
            req(3, 1, 2, BarKind::Mem64, 0x40_0000),
            req(3, 1, 0, BarKind::Mem32, 16),
        ];
        let placed = place(&r).unwrap();
        check(&placed);
        assert_eq!(placed[0].base, W + 0x3_0000, "the stride");
        assert_eq!(placed[2].base, W + 0x40_0000);
        assert_eq!(placed[1].base, W + 0x80_0000);
        assert_eq!(placed[3].base, W + 0x80_1000);
        assert_eq!(placed.iter().map(|p| p.request).collect::<Vec<_>>(), r, "in request order");
    }

    #[test]
    fn largest_first_no_overlap() {
        let r = [
            req(1, 0, 0, BarKind::Mem32, 0x8000),
            req(2, 1, 0, BarKind::Mem32, 0x100),
            req(2, 1, 1, BarKind::Mem32, 0x2_0000),
            req(2, 0, 3, BarKind::Mem64, 0x2_0000),
            req(9, 2, 5, BarKind::Mem32, 0x10),
            req(2, 0, 1, BarKind::Mem32, 0x1_0000),
            req(2, 0, 0, BarKind::Io, 0x100),
        ];
        let placed = place(&r).unwrap();
        check(&placed);
        let up = W + 0x20_0000;
        // 128 KiB (02.0 BAR3 before 02.1 BAR1), 64 KiB, 256 bytes, 16 bytes; gap-free
        assert_eq!(
            placed.iter().map(|p| p.base).collect::<Vec<_>>(),
            [W + 0x1_0000, up + 0x5_0000, up + 0x2_0000, up, up + 0x5_0100, up + 0x4_0000, 0xc000]
        );
    }

    #[test]
    fn io_bars_in_io_window() {
        assert_eq!(platform::IO_WINDOW, (0xc000, 0xffff));
        let r = [
            req(4, 0, 1, BarKind::Io, 0x40),
            req(4, 1, 1, BarKind::Io, 0x100),
            req(5, 0, 0, BarKind::Io, 4),
            req(6, 0, 4, BarKind::Io, 0x2000),
        ];
        let placed = place(&r).unwrap();
        check(&placed);
        assert_eq!(
            placed.iter().map(|p| p.base).collect::<Vec<_>>(),
            [0xe100, 0xe000, 0xe140, 0xc000]
        );
        // the window's 16 KiB fill exactly
        let full = [req(4, 0, 0, BarKind::Io, 0x2000), req(4, 0, 1, BarKind::Io, 0x2000)];
        assert_eq!(bases(&full), [0xc000, 0xe000]);
        // an I/O function 0 BAR0 does not take the memory stride
        assert_eq!(bases(&[req(1, 0, 0, BarKind::Io, 0x20)]), [0xc000]);
    }

    #[test]
    fn exhaustion_names_the_bar() {
        // memory: 254 MiB above the stride region; a 128 MiB BAR fits, a second does not
        let r = [
            req(3, 0, 2, BarKind::Mem64, 0x800_0000),
            req(3, 2, 4, BarKind::Mem32, 0x800_0000),
        ];
        let e = place(&r).unwrap_err();
        assert!(e.contains("PCI 03.2 BAR4"), "{e}");
        assert!(e.contains("memory window E0200000H-EFFFFFFFH"), "{e}");
        // I/O: 16 KiB, then one more port range
        let r = [
            req(7, 0, 0, BarKind::Io, 0x4000),
            req(7, 1, 3, BarKind::Io, 4),
        ];
        let e = place(&r).unwrap_err();
        assert!(e.contains("PCI 07.1 BAR3") && e.contains("I/O window C000H-FFFFH"), "{e}");
        // malformed requests are named too
        for (bad, what) in [
            (req(2, 0, 1, BarKind::Mem32, 0x3000), "not a power of two"),
            (req(2, 0, 1, BarKind::Mem32, 8), "memory below 16"),
            (req(2, 0, 1, BarKind::Io, 2), "I/O below 4"),
            (req(2, 0, 5, BarKind::Mem64, 0x1000), "64-bit at BAR5"),
            (req(2, 0, 6, BarKind::Mem32, 0x1000), "BAR6"),
            (req(2, 8, 0, BarKind::Mem32, 0x1000), "function 8"),
        ] {
            let e = place(&[bad]).unwrap_err();
            let named = format!("PCI 02.{} BAR{}", bad.function, bad.bar);
            assert!(e.contains(&named), "{what}: {e}");
        }
        // a length no window holds fails without overflow
        let e = place(&[req(2, 0, 1, BarKind::Mem64, 1 << 63)]).unwrap_err();
        assert!(e.contains("PCI 02.0 BAR1"), "{e}");
    }
}
