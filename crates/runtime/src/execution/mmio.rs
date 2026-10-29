//! M3, device accesses from translated code. A guest-physical page that is not guest RAM is a device page: shadows never
//! map one, so every access to it aborts at its access site. The site's walk runs first ([`translate`]: #PF, and before
//! it the canonical #GP/#SS and #AC checks of [`access_aborted`]), so every fault the guest tables owe comes before the
//! device (its A/D updates stay, as on x86, also when the access is then refused). The instruction is completed from its
//! site's completion record (`code_object::completion`, fexc's decode of the instruction it translated, never the guest
//! bytes now): the state at the instruction is banked, the device access is made once ([`local`], else the host:
//! [`REQUEST_DEVICE`]), the load's register merged as x86 does and RIP moved past the instruction; it never runs again,
//! so the device sees each access exactly once. A peer rewriting the code meanwhile changes nothing here: the block ran
//! the old instruction, and a block a rewrite killed is stepped from the new bytes before any site is served
//! ([`aborted`]). Device pages are uncached (UC): the access is one operation in program order, every earlier access of
//! this vCPU complete before it (`dsb sy` before the exit) and every later one after it (the exit is synchronous).
//! Refused, before any device effect, with the state at the instruction (`unsupported_mmio`, the reason in
//! [`Mmio::status`]): atomic sites (LOCK forms, XCHG), pair/push/pop sites, accesses crossing a page with a device page on
//! either side, sites without a completion record, pages no device claims, accesses a device refuses, and system
//! instructions' and deliveries' own accesses to device pages ([`system_access`]).
//!
//! Cost: each device access ends its block (the resident dispatches at the next RIP, an L1/L2 lookup, not a re-entry of
//! the rest of the block) and, unless [`local`] serves it, makes one host exit.
use super::*;
use natemu_abi::execution::{MMIO_VA, Mmio, REPLY_DEVICE, REQUEST_DEVICE, mmio as status};

/// `gpa` is guest RAM (anything else is a device page).
pub(super) fn ram(gpa: u64) -> bool {
    RAM.range(gpa, 1).is_some()
}
fn exchange() -> *mut Mmio {
    MMIO_VA as *mut Mmio
}
fn stop_with(s: &State, why: u64) -> ! {
    unsafe {
        let mut m = read_record(exchange());
        m.status = why;
        write_record(exchange(), m);
    }
    halt(s, stop::UNSUPPORTED_MMIO)
}

/// Stops at `site`'s instruction with `why` ([`status`]): nothing of it done.
pub(super) fn refuse(mut s: State, f: &NativeFrame, b: &Block, site: &Site, why: u64) -> ! {
    bank_at(&mut s, f, b, site);
    stop_with(&s, why)
}
/// A system instruction's or a delivery's own access (`segments::linear`) to a device page: refused at the instruction.
pub(super) fn system_access(s: &State) -> ! {
    stop_with(s, status::SYSTEM)
}

/// The resident's own devices (fast path, no host exit): the access to `gpa` of `width` bytes (a store's `data`, or a
/// load), answered with the load's value (0 for a store), or `None` when no resident device claims the page. None today;
/// X3's per-vCPU LAPIC (xAPIC page, e.g. EOI and TPR writes) is meant to answer here, with this vCPU's state at hand.
fn local(_s: &mut State, _gpa: u64, _width: u64, _data: Option<u64>) -> Option<u64> {
    None
}

/// Completes the access at `site` of `b` to device byte `gpa` (`None`: the access crosses a page, refused).
pub(super) fn device(mut s: State, f: &NativeFrame, b: &Block, site: &Site, gpa: Option<u64>) -> ! {
    match site.kind {
        object::kind::LOAD | object::kind::STORE => {}
        object::kind::SWAP | object::kind::ATOMIC => refuse(s, f, b, site, status::ATOMIC),
        _ => refuse(s, f, b, site, status::SITE),
    }
    let Some(c) = object::completion(site) else { refuse(s, f, b, site, status::FORM) };
    let Some(gpa) = gpa else { refuse(s, f, b, site, status::CROSSING) };
    let width = u64::from(c.width);
    let mask = if width == 8 { u64::MAX } else { (1 << (8 * width)) - 1 };
    // A store's data: the site's transfer register as the translated instruction holds it (xzr: 0).
    let data = c.store.then(|| if site.data_reg[0] == 31 { 0 } else { f.x[site.data_reg[0] as usize] } & mask);
    bank_at(&mut s, f, b, site);
    let value = match local(&mut s, gpa, width, data) {
        Some(v) => v,
        None => unsafe {
            let done = read_record(exchange()).completed;
            let request = Mmio { gpa, width, write: u64::from(c.store), value: data.unwrap_or(0), completed: done, ..Mmio::default() };
            write_record(exchange(), request);
            (s.request, s.reply) = (REQUEST_DEVICE, NONE);
            write_record(record(), s);
            // Every earlier access of this vCPU is complete before the device sees this one (x86 UC ordering).
            asm!("dsb sy", "hvc #{imm}", imm = const TRANSLATE_HVC);
            s = load();
            s.request = NONE;
            let m = read_record(exchange());
            if s.reply != REPLY_DEVICE {
                halt(&s, stop::BAD_REPLY)
            }
            match m.status {
                status::DONE => m.value,
                status::UNASSIGNED | status::DEVICE => halt(&s, stop::UNSUPPORTED_MMIO),
                _ => halt(&s, stop::BAD_REPLY),
            }
        },
    };
    // Committed: the load's register with x86's merge, RIP past the instruction (it completed: shadows and RF end with it).
    if !c.store {
        let bits = 64 - 8 * u32::from(c.width);
        let v = if c.signed { (((value << bits) as i64) >> bits) as u64 } else { value & mask };
        let old = s.cpu.gpr[c.reg as usize];
        s.cpu.gpr[c.reg as usize] = match (c.dest, c.high) {
            (1, true) => old & !0xff00 | (v & 0xff) << 8,
            (1, false) => old & !0xff | v & 0xff,
            (2, _) => old & !0xffff | v & 0xffff,
            (4, _) => v & 0xffff_ffff,
            _ => v,
        };
    }
    (s.cpu.mov_ss, s.sti_shadow, s.mid_string) = (0, 0, 0);
    completed(&mut s);
    s.cpu.rip = s.cpu.rip.wrapping_add(u64::from(c.length));
    unsafe {
        let mut m = read_record(exchange());
        (m.status, m.completed) = (status::DONE, m.completed + 1);
        write_record(exchange(), m);
        dispatch(s)
    }
}
