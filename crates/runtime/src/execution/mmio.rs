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
//! Mapped device memory (docs/restructure/mapped-device-memory-plan.md) is not a device page: a page of an enabled region
//! of the host's table ([`mapped`]) is shadowed onto the region by `translate` and accessed natively, as RAM is.
//!
//! Cost: each device access ends its block (the resident dispatches at the next RIP, an L1/L2 lookup, not a re-entry of
//! the rest of the block) and, unless [`local`] serves it, makes one host exit plus polls if completion is deferred.
use super::*;
use natemu_abi::execution::{
    MMIO_VA, Mmio, REPLY_DEVICE, REPLY_DEVICE_POLL, REPLY_PAGE, REQUEST_DEVICE,
    REQUEST_DEVICE_POLL, REQUEST_PAGE, mmio as status,
};

/// `gpa` is guest RAM (anything else is a device page, or ROM: [`rom`]).
pub(super) fn ram(gpa: u64) -> bool {
    guest_ram().range(gpa, 1).is_some()
}
/// Mapped device memory (docs/restructure/mapped-device-memory-plan.md): an enabled region of the host's table, which the
/// resident reaches through its EL1 alias (`alias` backs guest-physical `base` .. `base + len`).
#[derive(Clone, Copy, PartialEq, Eq)]
pub(super) struct Region {
    pub base: u64,
    pub len: u64,
    pub alias: u64,
}
impl Region {
    /// Guest RAM and this region: the memory of an access whose pages are RAM or in this region (`GuestRam` holds four
    /// regions; RAM takes three at most, so one mapped region fits and two never are joined: an access touching two
    /// regions is refused by its caller).
    pub fn memory(self) -> GuestRam {
        // SAFETY: the host maps the region's alias EL1 read/write for the VM's life (`Backing::machine`), disjoint from
        // RAM's; alias and base are page-aligned (so they agree modulo 8).
        unsafe { guest_ram().and(self.base, self.len, self.alias) }
    }
}
/// The enabled region holding `gpa`, a page that is not RAM (callers test RAM first: no RAM path reads the table), or
/// `None`. Overlapping regions: the lowest-numbered entry wins. The install protocol: this vCPU's holding flag
/// (`cache::holding`, SeqCst) is set before the table is read, so a host remap's shootdown (Task 3) either finds the flag
/// or this read sees the new table. A table never published (`seq` 0) has no region and never will: `Backing::machine`
/// publishes every region's entry (disabled) before any vCPU exists, so `seq` is nonzero exactly in a VM with regions,
/// and that case skips the flag.
pub(super) fn mapped(gpa: u64) -> Option<Region> {
    use core::sync::atomic::Ordering::Relaxed;
    let table = cache::mapped_regions();
    if table.seq.load(Relaxed) == 0 {
        return None;
    }
    cache::holding();
    // The flag's store before the table's loads (store then load: the read's Acquire alone would not order them).
    core::sync::atomic::fence(core::sync::atomic::Ordering::SeqCst);
    let (_, regions) = table.read();
    regions
        .iter()
        .find(|r| r.enabled != 0 && gpa.wrapping_sub(r.base) < r.len)
        .map(|r| Region { base: r.base, len: r.len, alias: r.alias })
}
/// `gpa`, which is not RAM, is ROM: read as RAM, a store to it dropped, as x86 drops a store to ROM. Asked only on the paths
/// that found a page outside RAM, so RAM's paths are as without one. Either the firmware image ([`image`]) or (the
/// platform-devices plan, SMBIOS) the legacy ROM range in the legacy hole (`Control::legacy_rom`, none when `(0, 0)`: the
/// SMBIOS entry point's page at F0000H, which a direct boot's host writes): its read-only shadows go onto the hole's own
/// alias, which nothing else uses, and it is never fetched from (it holds no code: a fetch there is not RAM).
pub(super) fn rom(gpa: u64) -> bool {
    let (start, end) = cache::control().legacy_rom;
    image(gpa) || gpa.wrapping_sub(start) < end - start
}
/// M2 Part 2, the firmware boot mode (docs/restructure/firmware-mode.md): `gpa`, which is not RAM, is in the firmware image
/// (`exec::FIRMWARE_END` - `Control::firmware_bytes` .. 4 GiB; none when that is 0): ROM that is also fetched as RAM.
pub(super) fn image(gpa: u64) -> bool {
    let bytes = cache::control().firmware_bytes;
    gpa.wrapping_sub(natemu_abi::execution::FIRMWARE_END - bytes) < bytes
}
/// The ROM ([`rom`]: the firmware image and the legacy ROM range) through the resident's alias (the image's blocks mapped
/// read-only: a store reaching it would halt the resident; the legacy hole's part of the low RAM span, which no shadow but
/// a read-only one maps and the resident never writes).
pub(super) fn roms() -> GuestRam {
    let c = cache::control();
    // SAFETY: the host maps the image's blocks of the alias at prepare, and the low span (the hole included), for the VM's
    // life.
    unsafe { natemu_abi::execution::rom_ram(c.firmware_bytes, c.legacy_rom, GUEST_RAM_VA) }
}
fn exchange() -> *mut Mmio {
    MMIO_VA as *mut Mmio
}
/// A refusal made before any request: the record names no access (its request fields are cleared, the count kept).
pub(super) fn stop_with(s: &State, why: u64) -> ! {
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
/// came first) are all unassigned or in the firmware image: each is shadowed read-only onto the shared all-ones page (the
/// image's onto the image) and `true` returned (the caller
/// retries the instruction, which then runs natively, any read form). `false`: a page is a device's (this vCPU's LAPIC page
/// whatever its mode, or one the host's registry claims): the device path. Under the strict switch the read stops
/// (`unassigned`). The host counts each page asked about as a read (a fill), also one then found next to a claimed page.
/// A claimed page is asked about once while it stays in this vCPU's small table (`cache::Control::claimed_pages`).
pub(super) fn ones(
    s: &mut State,
    f: &NativeFrame,
    b: &Block,
    site: &Site,
    touched: [Option<u64>; 2],
    device: [Option<u64>; 2],
) -> bool {
    readable_pages(s, touched, device).unwrap_or_else(|why| refuse(*s, f, b, site, why))
}

/// Share the native read-shadow classification with resident operations whose CPU bank already
/// describes the instruction. A strict unassigned stop is reported to the caller at its own fault root.
fn readable_pages(s: &mut State, touched: [Option<u64>; 2], mut device: [Option<u64>; 2]) -> Result<bool, u64> {
    // M2 Part 2: a page of ROM (the firmware image, or the legacy ROM range) is shadowed read-only onto its own bytes (no
    // host request; not global, so a CR3 load drops it and the next read shadows it again); the caller retries the read,
    // which then runs natively.
    if device.iter().flatten().any(|&g| rom(g)) {
        for (va, gpa) in touched.into_iter().zip(device.iter_mut()) {
            if let (Some(va), Some(g)) = (va, *gpa)
                && rom(g)
            {
                super::install_rom(s, va, g, false);
                *gpa = None;
            }
        }
        if device == [None; 2] {
            return Ok(true);
        }
    }
    if device.iter().flatten().any(|&g| super::lapic::claims(g)) {
        return Ok(false);
    }
    // Mapped device memory: a page of an enabled region is never shadowed onto the all-ones page. Translated code's
    // accesses classify it first (`translate`) and never come here with one; the resident's own reads that do (a
    // compatibility FS/GS string) have no completion for it.
    if device.iter().flatten().any(|&g| mapped(g).is_some()) {
        return Err(status::FORM);
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
        let gpa = gpa & !4095;
        if cache::control().claimed(gpa) {
            claimed = true;
            continue;
        }
        match unsafe { page(s, gpa) } {
            status::DONE => {}
            status::CLAIMED => {
                // The host's registry is frozen: remember the claim instead of asking again.
                cache::control().remember_claim(gpa);
                claimed = true;
            }
            status::UNASSIGNED => return Err(status::UNASSIGNED),
            _ => halt(s, stop::BAD_REPLY),
        }
    }
    if claimed {
        return Ok(false);
    }
    for (va, gpa) in touched.into_iter().zip(fresh) {
        if let (Some(va), Some(gpa)) = (va, gpa) {
            super::install_ones(s, va, gpa);
        }
    }
    Ok(true)
}
/// M2 Part 2, the boot-time interpreter's access to non-RAM page `gpa` (layer 3 of its memory model): `Ok` for an
/// unassigned page (reads all-ones, writes ignored), else the typed stop's detail: a device's page
/// (`stop::interp::DEVICE`: this vCPU's LAPIC page whatever its mode, a claim remembered, or the host's registry), or an
/// unassigned page under the strict switch (`stop::interp::UNASSIGNED`). The host counts the page asked about as a fill, as
/// for [`ones`]'s.
pub(super) fn unassigned(s: &mut State, gpa: u64) -> Result<(), u64> {
    let gpa = gpa & !4095;
    if super::lapic::claims(gpa) || cache::control().claimed(gpa) {
        return Err(stop::interp::DEVICE);
    }
    match unsafe { page(s, gpa) } {
        status::DONE => Ok(()),
        status::CLAIMED => {
            cache::control().remember_claim(gpa);
            Err(stop::interp::DEVICE)
        }
        status::UNASSIGNED => Err(stop::interp::UNASSIGNED),
        _ => halt(s, stop::BAD_REPLY),
    }
}
/// A based string has no scalar MMIO completion. The native path accepts a read of ROM or an
/// unassigned page (all ones), including adjacent RAM. All other device forms stop before transfer.
pub(super) fn string_operand(s: &mut State, va: u64, gpa: [u64; 2], first: usize, bytes: &mut [u8], write: bool) {
    let crosses = first < bytes.len();
    if write {
        // Native split stores refuse a non-RAM half before either write. A single-page string
        // store has no device completion record.
        stop_with(s, if crosses { status::CROSSING } else { status::FORM })
    }
    let touched = [Some(va), crosses.then(|| segments::wrap(&s.cpu, va + first as u64))];
    let device = [0, 1].map(|k| touched[k].and_then(|_| (!ram(gpa[k])).then_some(gpa[k])));
    match readable_pages(s, touched, device) {
        Ok(true) => {}
        Ok(false) => stop_with(s, status::FORM),
        Err(why) => stop_with(s, why),
    }
    // Classifying or installing a shadow may service a peer's shadow drop. Recheck split-lock
    // exclusion before the physical read, just as for the wrapped generated operand.
    split_wait(s, &gpa[..1 + usize::from(crosses)]);
    unsafe { asm!("dmb ish") };
    segments::copy_mixed(s, gpa, first, bytes, false);
    unsafe { asm!("dmb ish") };
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

/// M2 task 4: a 4-byte load of a local APIC register, completed in place: the Arm load word that aborted (`ldr wT`, checked
/// here to be one: unsigned offset, register offset or unscaled, no writeback) gets the register's value in `wT` and the
/// block goes on after it, as after a load from RAM, where [`device`] would bank the state, end the block and dispatch at
/// the next RIP. The same access, made once: a read of an APIC register has no effect ([`super::lapic::peek`] answers only
/// those), and nothing [`device`] does after its access has anything to do here (no data breakpoint is armed, and the
/// instruction's end is the block's business as for any load). Linux reads the in-service register at every INT 0x80
/// system call (since 6.7, to tell the instruction from an external interrupt on its vector) and the timer's current
/// count from its clock event code. Returns when the access is not such a load.
pub(super) fn load_in_place(s: &State, site: &Site, gpa: u64, elr: u64, spsr: u64) {
    if site.kind != object::kind::LOAD || site.width != 4 || site.writeback != object::WB_NONE || s.cpu.dr[5] & DR7_ENABLES != 0 {
        return;
    }
    // SAFETY: `elr` is the aborted word of a live block (the caller found its site there).
    let word = unsafe { (elr as *const u32).read() };
    let plain = match word >> 21 {
        0x5ca | 0x5cb => true,                                        // ldr wT, [xN, #imm]
        0x5c3 => word >> 10 & 3 == 2,                                 // ldr wT, [xN, xM/wM, extend]
        0x5c2 => word >> 10 & 3 == 0,                                 // ldur wT, [xN, #imm]
        _ => false,
    };
    if !plain {
        return;
    }
    let Some(value) = super::lapic::peek(s, gpa) else {
        return;
    };
    unsafe {
        let frame = &raw mut (*shared()).captured;
        if word & 31 != 31 {
            (*frame).x[(word & 31) as usize] = value & 0xffff_ffff;
        }
        let mut m = read_record(exchange());
        m.completed += 1;
        write_record(exchange(), &m);
        write_record(record(), s);
        resume(s, frame, elr + 4, spsr)
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
    // M2 Part 2: a store to ROM (the firmware image, the legacy ROM range) is dropped here, with no host exit (x86's ROM); a
    // load reads it (none comes here: [`translate`] shadows its reads).
    let image = rom(gpa).then(|| match data {
        Some(_) => 0,
        None => {
            let mut v = [0u8; 8];
            if !roms().copy_out(gpa, &mut v[..(width as usize).min(8)]) {
                halt(&s, stop::BAD_REPLY)
            }
            u64::from_le_bytes(v)
        }
    });
    let value = match image.or_else(|| local(&mut s, gpa, width, data)) {
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
                // A windowless I/O copy waits for this vCPU to drain the code pages it posted
                // here (cache.rs `drain`). Safe here as in `io_checkpoint`'s own drain after a
                // release: no guest access or block continuation is live until the access
                // finishes, and dispatch drains again before entering any block. Ordering: the
                // host posts with SeqCst before polling the stale bit; this drain's SeqCst
                // swaps either take the post or leave it for the next poll.
                take_invalidations(&mut s);
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
