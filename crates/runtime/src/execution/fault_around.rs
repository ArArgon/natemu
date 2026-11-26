//! Shadow fault-around (docs/restructure/fault-around-plan.md, chunk 2; its review 9.1 for the desktop): at a demand
//! shadow install of linear page `va` ([`super::translate`]), the shadows of the other pages of `va`'s aligned window of
//! W pages (`Control::fault_around`) inside the same guest leaf table, or the same 2 MiB leaf, that the guest tables
//! already justify. The rules without the machine are `natemu_abi::fault_around`'s; this file applies them:
//! - a neighbour is looked at only if its shadow slot is empty (nothing is replaced: no break-before-make, no TLBI);
//! - a 4 KiB leaf's neighbour entry is one plain load from the leaf table (no observer: the demand walk recorded the
//!   table page, and nothing is written), present, reserved-clean, A = 1, U/S at CPL 3 ([`natemu_abi::fault_around::pte`]);
//!   a 2 MiB leaf's neighbours share its one entry (one A, one D);
//! - guest RAM only (no device, mapped-region, ROM or unassigned page); its memory type per page: UC never, WT and WP
//!   read-only;
//! - not a page a peer's split lock holds (this vCPU's holding flag is set: the demand install's `split_wait`);
//! - in the kept half (the supervisor space's upper half) global pages only;
//! - writable only with D = 1 and the write granted, on WB or WC memory, by the read-side protocol (plan 3.3 item 6:
//!   `translated`, then the writable bit, then `translated` again), in the user space only; a supervisor neighbour that
//!   would be writable but for that rule is not installed (review 9.1, B2);
//! - each writable shadow installed goes into the reverse map of writable shadows (`cache::writable_installed`,
//!   docs/writable-drop.md invariant W; review 9.1, B1) before the resident returns to the guest.
//! No guest-visible bit is written: A and D read back exactly as without the fill (plan section 4).
use super::*;
use natemu_abi::fault_around::{self as fa, count};
use natemu_abi::shadow::Fill;

/// Neighbours installed per [`tables::fill`] (one publish each): a window of up to 64 pages is one batch.
const BATCH: usize = 64;

/// After the demand install of `va` (walk `t`) by block `b`: installs the window's justified neighbours. Nothing in a
/// step block (a UC page shadowed for one instruction) or while a data breakpoint is armed (the watchpoint paths stay
/// exactly as they were); nothing when the window is off.
pub(super) fn fill(s: &mut State, b: &Block, va: u64, t: &paging::Translation) {
    let w = cache::control().fault_around;
    if w == 0 {
        return;
    }
    if b.flags & cache::STEP != 0 || s.cpu.dr[5] & DR7_ENABLES != 0 {
        cache::control().fault_around_counts[count::DECLINED] += 1;
        return;
    }
    cache::control().fault_around_counts[count::FILLS] += 1;
    let c = controls(s);
    let cpl = s.cpu.cpl;
    // Review 9.1 B2: a supervisor 2 MiB leaf with D = 1 granting the write (the kernel's direct map) offers only neighbours the
    // rule below would skip: none is looked at.
    if t.leaf_bytes != 4096 && cpl != 3 && t.writable && t.dirty && !fa::SUPERVISOR_WRITABLE {
        count(count::SUPERVISOR_READ_ONLY);
        return;
    }
    let window = fa::window(va, w);
    let valid = match tables::valid_slots(cpl, va, window.clone()) {
        Ok(Some(valid)) => valid,
        Ok(None) | Err(shadow::Error::Noncanonical) => return,
        Err(_) => halt(s, stop::SHADOW),
    };
    let own = (va >> 12 & 511) as usize;
    let region = va & !0x1f_ffff;
    let mut batch = [Fill::default(); BATCH];
    let mut gpas = [0u64; BATCH];
    let mut n = 0;
    for j in window {
        if j == own {
            continue;
        }
        if valid[j / 64] >> (j % 64) & 1 != 0 {
            count(count::SHADOWED);
            continue;
        }
        let Some((fill, gpa)) = neighbour(s, &c, t, region, j) else {
            continue;
        };
        (batch[n], gpas[n]) = (fill, gpa);
        n += 1;
        if n == BATCH {
            install(s, region, t, &mut batch, &gpas);
            n = 0;
        }
    }
    if n != 0 {
        install(s, region, t, &mut batch[..n], &gpas[..n]);
    }
}

fn count(k: usize) {
    cache::control().fault_around_counts[k] += 1;
}

/// The shadow of leaf index `j` of the 2 MiB region at `region` that the demand walk `t` (under the controls `c`) justifies,
/// with its guest-physical page, or `None` (the reason counted). A writable one has its writable bit set already.
fn neighbour(s: &State, c: &paging::Control, t: &paging::Translation, region: u64, j: usize) -> Option<(Fill, u64)> {
    let user = c.cpl == 3;
    let page = if t.leaf_bytes == 4096 {
        // The demand walk read this table page (a RAM page): its entries are RAM too.
        let entry = guest_ram().load64(t.table + 8 * j as u64)?;
        let page = fa::pte(entry, c, t.parent_rw, t.parent_user).map_err(count).ok()?;
        if page.writable && !user && !fa::SUPERVISOR_WRITABLE {
            count(count::SUPERVISOR_READ_ONLY);
            return None;
        }
        page
    } else {
        // One entry for the 2 MiB leaf (its A set by the demand walk, its D the walk's): the same rights for every page.
        fa::Page { gpa: (t.gpa & !0x1f_ffff) + 4096 * j as u64, pat: t.pat, global: t.global, writable: t.writable && t.dirty }
    };
    if !mmio::ram(page.gpa) {
        count(count::NOT_RAM);
        return None;
    }
    let kind = cpu::memory_type(msrs(), page.gpa, page.pat);
    if kind == cpu::memory_type::UC {
        count(count::UNCACHED);
        return None;
    }
    let global = page.global && s.cpu.cr4 & CR4_PGE != 0;
    let va = region + 4096 * j as u64;
    if !user && va >> 47 != 0 && !global {
        count(count::KEPT);
        return None;
    }
    if cache::split_held(page.gpa) {
        count(count::SPLIT);
        return None;
    }
    let Some(target) = tables::ipa(&guest_ram(), page.gpa) else {
        count(count::NOT_RAM);
        return None;
    };
    let writable = page.writable && {
        if !matches!(kind, cpu::memory_type::WB | cpu::memory_type::WC) {
            count(count::NOT_WRITABLE_TYPE);
            false
        } else {
            // Plan 3.3 item 6: `translated` first (then read-only, no bit), else the bit, then `translated` again. (Not
            // switched by `--writable-on-read`, which is chunk 1's demand-read rule: `--fault-around off` is the fill's switch.)
            let w = cpu::writable_on_read(true, true, true, kind, || cache::translated(page.gpa), || {
                cache::shadowed_writable(page.gpa)
            });
            if !w {
                count(count::TRANSLATED);
            }
            w
        }
    };
    Some((Fill { index: j as u16, target, writable, global, written: false }, page.gpa))
}

/// Installs `batch` (guest-physical pages `gpas`) in the region's leaf table and records each writable shadow written in
/// the reverse map, before anything else runs.
fn install(s: &State, region: u64, t: &paging::Translation, batch: &mut [Fill], gpas: &[u64]) {
    let cpl = s.cpu.cpl;
    let user = cpl == 3;
    match tables::fill(cpl, region, batch) {
        Ok(_) => {}
        Err(shadow::Error::Noncanonical) => return,
        Err(_) => halt(s, stop::SHADOW),
    }
    let counts = &mut cache::control().fault_around_counts;
    for (e, &gpa) in batch.iter().zip(gpas) {
        if !e.written {
            continue;
        }
        let va = region + 4096 * u64::from(e.index);
        if e.writable {
            cache::writable_installed(user, va, gpa);
            counts[count::WRITABLE] += 1;
        }
        counts[if user { count::USER } else { count::SUPERVISOR }] += 1;
        if t.leaf_bytes != 4096 {
            counts[count::LARGE] += 1;
        }
        crate::study::installed(user, va, e.writable, e.global);
    }
}
