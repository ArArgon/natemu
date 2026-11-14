//! Page-currency plan, chunk 0 (`page-study` builds only; the record and its rules are `natemu_abi::page_study`): the record of
//! fetch-walk results in shadow mode. At every fetch walk, a revalidation's or a placement's ([`super::page_translation`]), the
//! record is probed, the walk is made all the same, the two answers are compared and counted, and a walk the record would
//! not have answered is recorded. Nothing the guest or the cache sees changes: the walk's answer is what is returned. The
//! timestamps are plain counter reads around the probe and around the walk alone (not `mark`, which a placement does after
//! it), so the cost of a walk the record would have saved is measured apart from the one it would not.
use super::*;
use natemu_abi::page_study::{self as ps, Counts, Record};

fn table() -> &'static mut [Record] {
    // SAFETY: the record is this vCPU's, in its lookup area; only this vCPU's resident reads or writes it.
    unsafe {
        core::slice::from_raw_parts_mut(
            cache::lookup_area::<Record>(natemu_abi::execution::cache::PAGE_STAMPS),
            ps::ENTRIES,
        )
    }
}
fn counts() -> &'static mut Counts {
    &mut cache::control().page_study
}
/// The virtual counter, with no barrier (accounting, not ordering; as `perf`), but ordered against the compiler's memory
/// accesses.
#[inline(always)]
fn now() -> u64 {
    let t: u64;
    // SAFETY: reads the virtual counter.
    unsafe { asm!("mrs {}, cntvct_el0", out(reg) t, options(nostack, preserves_flags)) };
    t
}

/// Start-up: what reading the counter twice in a row costs, in ticks over many pairs (the timestamps' own share of a measured
/// walk).
pub fn init() {
    let k = counts();
    for _ in 0..1024 {
        let t0 = now();
        let t1 = now();
        k.tick_pair_ticks += t1.wrapping_sub(t0);
        k.tick_pairs += 1;
    }
}

/// A revalidation of a block is starting: until [`block_end`], `translate` calls for it say whether the record answered each page.
pub fn block_begin() {
    counts().block_all_hit = 1;
}
/// The revalidation of `b` succeeded with `flags` ([`super::leaves`]).
pub fn block_end(b: &Block, flags: u32) {
    let k = counts();
    let crossing = b.source_gpa[1] != u64::MAX;
    let classes = [
        (ps::B_ALL, true),
        (ps::B_GLOBAL, flags & cache::GLOBAL != 0),
        (ps::B_LARGE, flags & cache::LARGE != 0),
        (ps::B_USER, b.context & 3 == 3),
        (ps::B_CROSSING, crossing),
    ];
    for (class, member) in classes {
        k.blocks[class] += u64::from(member);
        k.blocks_hit[class] += u64::from(member) & k.block_all_hit;
    }
}

/// INVLPG or a #PF at `address` ([`cache::invlpg`]): its page's and its 2 MiB region's records, both classes, are cleared.
pub fn invalidated(address: u64) {
    let cleared = ps::invalidate(table(), address);
    if cleared != 0 {
        let k = counts();
        k.clear_events += 1;
        k.cleared += cleared;
    }
}

/// A fetch walk of `va` for `who` ([`ps::REVAL`], [`ps::PLACE`]), with the record probed before it and compared after it.
pub(super) fn translate(s: &mut State, va: u64, who: usize) -> Result<(u64, u32), [u64; 3]> {
    let (user, pge) = match s.cpu.control() {
        Some(c) => (c.cpl == 3, s.cpu.cr4 & CR4_PGE != 0),
        // (No walk is possible: `fetch_walk` halts.)
        None => return fetch_walk(s, va),
    };
    let c = cache::control();
    let (epoch, global_epoch) = (c.epoch, c.global_epoch);
    let t0 = now();
    let answer = ps::probe(table(), va, user, epoch, global_epoch, pge);
    let t1 = now();
    let result = fetch_walk(s, va);
    let t2 = now();
    let hit = usize::from(answer.is_some());
    let k = counts();
    k.calls[who] += 1;
    k.probe_ticks += t1.wrapping_sub(t0);
    k.walk_ticks[who][hit] += t2.wrapping_sub(t1);
    k.walk_count[who][hit] += 1;
    k.block_all_hit &= hit as u64 | u64::from(who != ps::REVAL);
    // The flags the call is classed by: the walk's, or the answer's when the walk faulted.
    let flags = match (&result, answer) {
        (Ok((_, f)), _) => u64::from(*f),
        (Err(_), Some((_, f, _))) => f,
        (Err(_), None) => 0,
    };
    for (class, member) in [
        (ps::C_GLOBAL, flags & ps::GLOBAL != 0),
        (ps::C_LARGE, flags & ps::LARGE != 0),
        (ps::C_USER, user),
    ] {
        k.class_calls[who][class] += u64::from(member);
        k.class_hits[who][class] += u64::from(member) & hit as u64;
    }
    if let Err(fault) = &result {
        k.faulted[who] += 1;
        k.not_ram[who] += u64::from(fault[0] == fetch::NOT_RAM);
    }
    match (&result, answer) {
        (Ok((gpa, f)), Some((page, aflags, large))) => {
            k.hit_4k[who] += u64::from(!large);
            k.hit_2m[who] += u64::from(large);
            let same_flags = u64::from(*f) & (ps::GLOBAL | ps::LARGE) == aflags;
            if *gpa != page {
                k.disagree_frame += 1;
            } else if !same_flags {
                k.disagree_flags += 1;
            }
            k.disagree[who] += u64::from(*gpa != page || !same_flags);
        }
        (Err(_), Some((_, _, large))) => {
            k.hit_4k[who] += u64::from(!large);
            k.hit_2m[who] += u64::from(large);
            k.disagree_fault += 1;
            k.disagree[who] += 1;
        }
        // A miss records the walk's answer (never over a hit: the record is the TLB entry under test).
        (Ok((gpa, f)), None) => record(va, *gpa, u64::from(*f), user, (epoch, global_epoch, pge)),
        (Err(_), None) => {}
    }
    result
}

/// Records a walk's answer: `gpa` is the 4 KiB page `va` is fetched from, `flags` the leaf's.
fn record(va: u64, gpa: u64, flags: u64, user: bool, (epoch, global_epoch, pge): (u64, u64, bool)) {
    let large = flags & ps::LARGE != 0;
    let k = counts();
    if !ps::recordable(&guest_ram(), ps::base(gpa, large), large) {
        k.split_2m += 1;
        return;
    }
    let r = ps::record(va, gpa, flags, user, epoch);
    let evicted = ps::insert(table(), r, epoch, global_epoch, pge);
    k.records += 1;
    if k.epoch_of_count != epoch {
        (k.epoch_of_count, k.epoch_records) = (epoch, 0);
    }
    k.evictions += u64::from(evicted.is_some());
    // (A record replaced in its own epoch was counted already; one of an earlier epoch was not.)
    if !evicted.is_some_and(|old| old.epoch == epoch) {
        k.epoch_records += 1;
    }
    k.live_max = k.live_max.max(k.epoch_records);
}
