//! M2 Part 2: the boot-time interpreter's plumbing in the resident (docs/restructure/m2-boot-interpreter-plan.md
//! sections 4 and 5). A vCPU reaches a state only the interpreter runs ([`Cpu::interpreted_mode`]: real mode, protected
//! mode outside IA-32e mode, compatibility mode at CPL 0) only at a cold point: the end of a system transaction (its
//! `commit`, or `set_cpl` inside it), where the resident halted before, records it ([`record`]: [`debug::INTERPRET`] in
//! `State::debug`), and the next boundary enters the interpreter once the transaction has completed ([`boundary`], from
//! `find_from`'s loop, before a trap-class #DB). Every path out of a system transaction reaches that loop, and none needs a
//! test of its own: the fast system-call path declines when `State::debug` is nonzero, and the loop's first look at the
//! debug state is at its trap bits, which hold the record. The interpreter returns to that loop when the state is one the
//! translator runs again (no cycle through `dispatch`). The vCPU's start enters directly ([`run`]). The interpreter itself is
//! not built yet: [`run`] stops the vCPU as it stopped before (`unsupported_mode`, `unsupported_code_mode`), so a guest
//! sees the same stop, now after the transaction that made the state.
//!
//! The paging-off walk ([`unpaged_pages`], [`fetch_unpaged`], [`revalidated_unpaged`]) is the variant of
//! [`segments::pages`], [`fetch_walk`] and [`revalidated`] for the interpreter's accesses, fetches and records with CR0.PG =
//! 0; those three keep their mode check (translatable state only: a halt otherwise, an assertion).
use super::*;

/// [`ends_in_mode`]'s arms where [`controls`] halts with `reason`: a state the interpreter runs is recorded for the next
/// boundary; any other halts as [`controls`] does (`unsupported_mode` for the paging mode or CPL, `unsupported_code_mode`
/// for the code segment), at the instruction.
#[cold]
#[inline(never)]
pub(super) fn record(s: &mut State, reason: u64) {
    if s.cpu.interpreted_mode().is_some() {
        s.debug |= debug::INTERPRET;
    } else {
        halt(s, reason)
    }
}

/// The boundary after a transaction [`record`]ed a state the interpreter runs (`trap_due`'s first check, which it ends):
/// clears the record and runs the interpreter; true, so `find_from` looks at the boundary again (a state that is no longer
/// one, the transaction having restored the bank after it recorded, then goes on as any other).
#[cold]
#[inline(never)]
pub(super) fn boundary(s: &mut State) -> bool {
    s.debug &= !debug::INTERPRET;
    if s.cpu.interpreted_mode().is_some() {
        run(s);
    }
    true
}

/// The interpreter from the bank's state ([`Cpu::interpreted_mode`]), returning once the state is one the translator runs.
/// Not built yet: the stop natemu made before there was one.
pub(super) fn run(s: &mut State) {
    halt(s, stopped(s))
}

/// The stop [`controls`] makes for a state it does not run.
fn stopped(s: &State) -> u64 {
    if s.cpu.paging().is_none() {
        stop::UNSUPPORTED_MODE
    } else {
        stop::UNSUPPORTED_CODE_MODE
    }
}

/// [`segments::pages`] with paging off (CR0.PG = 0, interpreted state only: R, P16, P32), for an access the interpreter
/// has checked against its segment: the guest-physical pages are the linear ones ([`paging::unpaged`]: wrapping at 4
/// GiB), reached through the guest-RAM alias as [`segments::pages`]' are. Nothing faults; as there, a page of UC RAM, or a
/// UC access made since the last boundary, puts a barrier before the caller's access, and the access waits while a peer
/// holds a split lock on its pages. The memory type is the MTRRs' alone (without paging the PAT is not used).
#[expect(dead_code, reason = "M2 Part 2: the interpreter's memory layer (B1 (c) on) calls it")]
pub(super) fn unpaged_pages(s: &mut State, va: u64, n: u64) -> ([u64; 2], usize) {
    let (first, next) = paging::unpaged(va, n);
    let (gpa, count) = match next {
        Some(page) => ([first, page], 2),
        None => ([first, 0], 1),
    };
    let c = cache::control();
    let owed = c.uncached_count != 0 || c.uncached_fence != 0;
    let mut uncached = false;
    for &g in &gpa[..count] {
        if mmio::ram(g) && cpu::mtrr_type(msrs(), g) == cpu::memory_type::UC {
            (c.uncached_fence, c.uncached_resident) = (1, c.uncached_resident + 1);
            uncached = true;
        }
    }
    if owed || uncached {
        // SAFETY: a barrier; no other effect.
        unsafe { asm!("dmb ish") };
    }
    split_wait(s, &gpa[..count]);
    (gpa, count)
}

/// [`fetch_walk`] with paging off (interpreted state only): the page `va` is fetched from is its linear page (wrapping at
/// 4 GiB), when it is guest RAM; no leaf, so neither [`cache::GLOBAL`] nor [`cache::LARGE`].
pub(super) fn fetch_unpaged(va: u64) -> Result<(u64, u32), [u64; 3]> {
    let page = paging::unpaged(va, 1).0 & !4095;
    if guest_ram().range(page, 4096).is_some() {
        Ok((page, 0))
    } else {
        Err([fetch::NOT_RAM, 0, 0])
    }
}

/// [`revalidated`] with paging off (interpreted state only), for a record keyed by its linear address: its source pages
/// are where its linear pages are fetched from now ([`fetch_unpaged`]); its flags as [`leaves`] makes them.
#[expect(dead_code, reason = "M2 Part 2: the interpreter's records (B1 (c) on) are revalidated through it")]
pub(super) fn revalidated_unpaged(s: &State, b: &Block) -> Option<u32> {
    let mut flags = [cache::GLOBAL; 2];
    for (k, &gpa) in b.source_gpa.iter().enumerate() {
        if gpa != u64::MAX {
            flags[k] = fetch_unpaged((b.rip & !4095).wrapping_add(4096 * k as u64))
                .ok()
                .filter(|&(page, _)| page == gpa)?
                .1;
        }
    }
    Some(leaves(s, flags))
}
