//! Page-currency plan chunk 1 (docs/restructure/page-currency-plan.md): the per-vCPU record of fetch-walk results
//! (`natemu_abi::page_study`, the record and its rules as pure functions) used for real. A fetch translation, a block's
//! revalidation or a placement ([`translation`]), is answered from the record when a walk of this page in this TLB epoch
//! under this privilege class already found it, as a TLB entry answers; else it is a walk, and its answer is recorded.
//! INVLPG and every #PF clear the entries of their page and 2 MiB region, both classes ([`invalidated`]).
//!
//! `Control::pages` (the host's `--page-currency`): off is today's walk; on answers from the record; the check modes answer
//! from the record too and make the walk as well, counting (and in `check-halt` stopping at) a difference. A hit sets no A
//! bit (a TLB hit sets none); the check's walk is a real walk (A bits, the observer killing blocks) that never overwrites the
//! record, which is the entry under test.
//!
//! Epochs are `u64`, monotonic for the vCPU's life (`cache::reset` keeps them), so a record never aliases: at a million
//! events a second the counter wraps after 5.8 x 10^5 years (not handled).
use super::*;
use natemu_abi::{execution::cache::{PAGES_CHECK, PAGES_CHECK_HALT, PAGES_OFF, PAGES_ON}, page_study as ps};

/// This vCPU's record, in the lookup area (`area::PAGE_STAMPS`).
pub(super) fn table() -> &'static mut [ps::Record] {
    // SAFETY: the record is this vCPU's, in its lookup area; only this vCPU's resident reads or writes it.
    unsafe {
        core::slice::from_raw_parts_mut(
            cache::lookup_area::<ps::Record>(natemu_abi::execution::cache::PAGE_STAMPS),
            ps::ENTRIES,
        )
    }
}

/// INVLPG or a #PF at `address`, at any CPL ([`cache::invlpg`]): the 4 KiB record of its page and the 2 MiB record of its
/// region, both privilege classes (SDM Vol.3 4.10.4.1: the entries for the page and for a larger page containing it,
/// whatever privilege filled them; Linux invalidates a user PTE it edited at CPL 0).
pub(crate) fn invalidated(address: u64) {
    let _cleared = ps::invalidate(table(), address);
    #[cfg(feature = "page-study")]
    page_study::cleared(_cleared);
}

/// The record's answer for `va` now, or `None`: `(the 4 KiB page, the leaf's flags)`. With the speculation barriers on, a hit
/// ends in the `sb` the walk's leaf check ends in, so every fetch translation handed out follows a barrier (no load is issued
/// through the answer: a revalidation compares frame numbers and a placement's bytes are copied by the host after the `hvc`).
#[inline(always)]
fn probe(s: &State, va: u64) -> Option<(u64, u32)> {
    let control = s.cpu.control()?;
    let c = cache::control();
    let (page, flags, _) = ps::probe(
        table(),
        va,
        control.cpl == 3,
        c.epoch,
        c.global_epoch,
        s.cpu.cr4 & CR4_PGE != 0,
    )?;
    if c.speculation_barriers != 0 {
        paging::speculation_barrier();
    }
    Some((page, flags as u32))
}

/// The fetch translation of `va` (a revalidation's or a placement's): [`fetch_walk`]'s answer, from the record when it holds one.
pub(super) fn translation(s: &mut State, va: u64) -> Result<(u64, u32), [u64; 3]> {
    let c = cache::control();
    if c.pages == PAGES_OFF {
        c.page_walks += 1;
        return fetch_walk(s, va);
    }
    if let Some(hit) = probe(s, va) {
        c.page_hits += 1;
        if c.pages >= PAGES_CHECK {
            checked(s, va, hit);
        }
        return Ok(hit);
    }
    c.page_walks += 1;
    let walked = fetch_walk(s, va);
    if let Ok((gpa, flags)) = walked {
        record(s, va, gpa, flags);
    }
    walked
}

/// Records a walk's answer: `gpa` is the 4 KiB page `va` is fetched from, `flags` the leaf's. A 2 MiB leaf is recorded only if
/// its whole range lies in one guest-RAM region (`ps::recordable`): else a record made from the RAM part would later answer a
/// fetch in the non-RAM part with a frame, turning the typed stop `no_code fetch_not_ram` into a host error on the copy.
fn record(s: &State, va: u64, gpa: u64, flags: u32) {
    let large = u64::from(flags) & ps::LARGE != 0;
    if large && !ps::recordable(&guest_ram(), ps::base(gpa, true), true) {
        cache::control().page_split_2m += 1;
        return;
    }
    let Some(control) = s.cpu.control() else { return };
    let c = cache::control();
    let r = ps::record(va, gpa, u64::from(flags), control.cpl == 3, c.epoch);
    ps::insert(
        table(),
        r,
        c.epoch,
        c.global_epoch,
        s.cpu.cr4 & CR4_PGE != 0,
    );
}

/// Check mode: the walk after a hit, compared with it (the walk may kill blocks and set A bits, as any walk; it never touches the
/// record). A different frame or other flags (a size, G), or a walk that faults, is a difference: counted, and a halt in
/// `check-halt`.
#[inline(never)]
fn checked(s: &mut State, va: u64, (page, flags): (u64, u32)) {
    let walked = fetch_walk(s, va);
    if walked == Ok((page, flags)) {
        return;
    }
    cache::control().page_disagree += 1;
    if cache::control().pages == PAGES_CHECK_HALT {
        s.native = [va, page, walked.map_or(u64::MAX, |w| w.0)];
        halt(s, stop::PAGE_CHECK)
    }
}

/// A block's revalidation from the record alone ([`cache::lookup`] on the fast miss path): the answer a walk of each source
/// page would give where every page is recorded ([`Mapped::Yes`] when each page is the block's own, [`Mapped::No`] when a
/// recorded frame differs), else [`Mapped::Unknown`]: some page has no record, so a walk is owed, which may fault or write an A
/// bit and is the full path's. Off and in the check modes (which owe the walk) it knows nothing, nor does a `page-study` build
/// (its record is the shadow it measures).
pub(super) fn revalidated_from_record(s: &State, b: &Block) -> Mapped {
    if cfg!(feature = "page-study") || cache::control().pages != PAGES_ON {
        return Mapped::Unknown;
    }
    let mut flags = [cache::GLOBAL; 2];
    let mut differ = false;
    let mut hits = 0;
    for (k, &gpa) in b.source_gpa.iter().enumerate() {
        if gpa != u64::MAX {
            let Some((page, f)) = probe(s, (b.rip & !4095).wrapping_add(4096 * k as u64)) else {
                return Mapped::Unknown;
            };
            (flags[k], differ, hits) = (f, differ || page != gpa, hits + 1);
        }
    }
    cache::control().page_hits += hits;
    if differ {
        Mapped::No
    } else {
        Mapped::Yes(leaves(s, flags))
    }
}
