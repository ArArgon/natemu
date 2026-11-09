//! M3, device accesses from translated code. A guest-physical page that is not guest RAM is a device page if a device
//! claims it (the host's registry, or this vCPU's LAPIC page whatever its mode) and unassigned otherwise. Shadows never map
//! a device page, so every access to it aborts at its access site. An unassigned page is shadowed only for reads, read-only
//! onto the shared all-ones page, once a read touches it ([`ones`], L3's amendment: every read form then runs natively);
//! a write to it aborts as to a device page. The site's walk runs first ([`translate`]: #PF, and before
//! it the canonical #GP/#SS and #AC checks of [`access_aborted`]), so every fault the guest tables owe comes before the
//! device (its A/D updates stay, as on x86, also when the access is then refused). The instruction is completed from its
//! site's completion record (`code_object::completion`, fexc's decode of the instruction it translated, never the guest
//! bytes now): the state at the instruction is banked, the device access is made once ([`local`], else the host:
//! [`REQUEST_DEVICE`]), the load's register merged as x86 does and RIP moved past the instruction; it never runs again,
//! so the device sees each access exactly once. A peer rewriting the code meanwhile changes nothing here: the block ran
//! the old instruction, and a block a rewrite killed is stepped from the new bytes before any site is served
//! ([`aborted`]). Device pages are uncached (UC): the access is one operation in program order, every earlier access of
//! this vCPU complete before it (`dsb sy` before the exit) and every later one after its final reply. A deferred access
//! waits with owned completion data, releasing shadows and acknowledging host RAM windows; it never reissues the access.
//! Refused, before any device effect, with the state at the instruction (`unsupported_mmio`, the reason in
//! [`Mmio::status`]): atomic sites (LOCK forms, XCHG), pair/push/pop sites, accesses crossing a page with a device page on
//! either side, sites without a completion record, pages no device claims, accesses a device refuses, and system
//! instructions' and deliveries' own accesses to device pages ([`system_access`]).
//!
//! Cost: each device access ends its block (the resident dispatches at the next RIP, an L1/L2 lookup, not a re-entry of
//! the rest of the block) and, unless [`local`] serves it, makes one host exit plus polls if completion is deferred.
use super::*;
use natemu_abi::execution::{
    MMIO_VA, Mmio, REPLY_DEVICE, REPLY_DEVICE_POLL, REPLY_PAGE, REQUEST_DEVICE,
    REQUEST_DEVICE_POLL, REQUEST_PAGE, mmio as status,
};

/// `gpa` is guest RAM (anything else is a device page).
pub(super) fn ram(gpa: u64) -> bool {
    guest_ram().range(gpa, 1).is_some()
}
fn exchange() -> *mut Mmio {
    MMIO_VA as *mut Mmio
}
/// A refusal made before any request: the record names no access (its request fields are cleared, the count kept).
fn stop_with(s: &State, why: u64) -> ! {
    unsafe {
        let m = read_record(exchange());
        write_record(
            exchange(),
            &Mmio {
                status: why,
                completed: m.completed,
                ..Mmio::default()
            },
        );
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

/// L3 (amendment): a read at `site` whose non-RAM pages (`device`, of the linear pages `touched`; walked, so every fault
/// came first) are all unassigned: each is shadowed read-only onto the shared all-ones page and `true` returned (the caller
/// retries the instruction, which then runs natively, any read form). `false`: a page is a device's (this vCPU's LAPIC page
/// whatever its mode, or one the host's registry claims): the device path. Under the strict switch the read stops
/// (`unassigned`). The host counts each page asked about as a read (a fill), also one then found next to a claimed page.
pub(super) fn ones(
    s: &mut State,
    f: &NativeFrame,
    b: &Block,
    site: &Site,
    touched: [Option<u64>; 2],
    device: [Option<u64>; 2],
) -> bool {
    if device.iter().flatten().any(|&g| super::lapic::claims(g)) {
        return false;
    }
    // A page already shadowed onto the all-ones page (the access aborted on its other page) is not asked about again.
    let ones = |va: Option<u64>| {
        va.is_some_and(|va| {
            tables::descriptor(s.cpu.cpl, va) & 0xffff_ffff_f001 == natemu_abi::ALL_ONES_IPA | 1
        })
    };
    let fresh = [0, 1].map(|k| device[k].filter(|_| !ones(touched[k])));
    let mut claimed = false;
    for &gpa in fresh.iter().flatten() {
        match unsafe { page(s, gpa & !4095) } {
            status::DONE => {}
            status::CLAIMED => claimed = true,
            status::UNASSIGNED => refuse(*s, f, b, site, status::UNASSIGNED),
            _ => halt(s, stop::BAD_REPLY),
        }
    }
    if claimed {
        return false;
    }
    for (va, gpa) in touched.into_iter().zip(fresh) {
        if let (Some(va), Some(_)) = (va, gpa) {
            super::install_ones(s, va);
        }
    }
    true
}
/// Asks the host about non-RAM page `gpa` ([`REQUEST_PAGE`]): its status.
unsafe fn page(s: &mut State, gpa: u64) -> u64 {
    unsafe {
        let completed = read_record(exchange()).completed;
        write_record(
            exchange(),
            &Mmio {
                gpa,
                completed,
                ..Mmio::default()
            },
        );
        (s.request, s.reply) = (REQUEST_PAGE, NONE);
        write_record(record(), s);
        let p = crate::perf::enter(crate::perf::HOST);
        asm!("dsb sy", "hvc #{imm}", imm = const TRANSLATE_HVC);
        crate::perf::leave(p);
        *s = load();
        s.request = NONE;
        if s.reply != REPLY_PAGE {
            halt(s, stop::BAD_REPLY)
        }
        read_record(exchange()).status
    }
}

/// The resident's own devices (fast path, no host exit): the access to `gpa` of `width` bytes (a store's `data`, or a
/// load), answered with the load's value (0 for a store), or `None` when no resident device claims the page: this vCPU's
/// local APIC in xAPIC mode (X3, [`super::lapic::mmio`]; an access it does not implement is refused as a device's).
fn local(s: &mut State, gpa: u64, width: u64, data: Option<u64>) -> Option<u64> {
    match super::lapic::mmio(s, gpa, width, data)? {
        Ok(v) => Some(v),
        Err(()) => stop_with(s, status::DEVICE),
    }
}

/// Completes the access at `site` of `b` to device byte `gpa` (`None`: the access crosses a page, refused).
pub(super) fn device(mut s: State, f: &NativeFrame, b: &Block, site: &Site, gpa: Option<u64>) -> ! {
    match site.kind {
        object::kind::LOAD | object::kind::STORE => {}
        object::kind::SWAP | object::kind::ATOMIC => refuse(s, f, b, site, status::ATOMIC),
        _ => refuse(s, f, b, site, status::SITE),
    }
    let Some(c) = object::completion(site) else {
        refuse(s, f, b, site, status::FORM)
    };
    let Some(gpa) = gpa else {
        refuse(s, f, b, site, status::CROSSING)
    };
    let width = u64::from(c.width);
    let mask = if width == 8 {
        u64::MAX
    } else {
        (1 << (8 * width)) - 1
    };
    // A store's data: the site's transfer register as the translated instruction holds it (xzr: 0).
    let data = c.store.then(|| if site.data_reg[0] == 31 { 0 } else { f.x[site.data_reg[0] as usize] } & mask);
    // A deferred reply can flush the cache while parked. Retain only owned completion data and
    // the linear address, never a Site/Block reference into cache metadata across that wait.
    let linear = site_address(site, f);
    bank_at(&mut s, f, b, site);
    let value = match local(&mut s, gpa, width, data) {
        Some(v) => v,
        None => unsafe {
            let done = read_record(exchange()).completed;
            let request = Mmio {
                gpa,
                width,
                write: u64::from(c.store),
                value: data.unwrap_or(0),
                completed: done,
                ..Mmio::default()
            };
            write_record(exchange(), &request);
            (s.request, s.reply) = (REQUEST_DEVICE, NONE);
            write_record(record(), &s);
            // Every earlier access of this vCPU is complete before the device sees this one (x86 UC ordering).
            let p = crate::perf::enter(crate::perf::HOST);
            asm!("dsb sy", "hvc #{imm}", imm = const TRANSLATE_HVC);
            crate::perf::leave(p);
            s = load();
            s.request = NONE;
            let mut m = read_record(exchange());
            if s.reply != REPLY_DEVICE {
                halt(&s, stop::BAD_REPLY)
            }
            if m.status == status::PENDING {
                // Unlike an ordinary host exit, this wait must release writable/atomic holdings
                // before another vCPU or the I/O owner can make progress. No guest RAM is touched
                // until the accepted access finishes and dispatch selects the next instruction.
                reset_shadows(&mut s);
            }
            loop {
                match m.status {
                    status::DONE => break m.value,
                    status::UNASSIGNED | status::DEVICE | status::FAILED => {
                        halt(&s, stop::UNSUPPORTED_MMIO)
                    }
                    status::PENDING => {}
                    _ => halt(&s, stop::BAD_REPLY),
                }
                io_checkpoint(&mut s);
                (s.request, s.reply) = (REQUEST_DEVICE_POLL, NONE);
                write_record(record(), &s);
                let p = crate::perf::enter(crate::perf::HOST);
                asm!("dsb sy", "hvc #{imm}", imm = const TRANSLATE_HVC);
                crate::perf::leave(p);
                s = load();
                s.request = NONE;
                if s.reply != REPLY_DEVICE_POLL {
                    halt(&s, stop::BAD_REPLY)
                }
                m = read_record(exchange());
            }
        },
    };
    // Committed: the load's register with x86's merge, RIP past the instruction (it completed: shadows and RF end with it).
    if !c.store {
        let bits = 64 - 8 * u32::from(c.width);
        let v = if c.signed {
            (((value << bits) as i64) >> bits) as u64
        } else {
            value & mask
        };
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
    // S4: the device access meets data breakpoints as any (the linear address, x86's width).
    super::watch::access(&mut s, linear, width, c.store);
    completed(&mut s);
    s.cpu.rip = s.cpu.rip.wrapping_add(u64::from(c.length));
    unsafe {
        let mut m = read_record(exchange());
        (m.status, m.completed) = (status::DONE, m.completed + 1);
        write_record(exchange(), &m);
        dispatch(&mut s)
    }
}
