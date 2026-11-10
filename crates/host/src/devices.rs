//! M3, devices on guest-physical ranges: the host side of translated code's device accesses
//! (`natemu_abi::execution::Mmio`).
//!
//! **Registry.** [`Devices`] maps page-aligned guest-physical ranges outside guest RAM to [`Device`]s (a device may own
//! several ranges). The resident never shadows such a page (an unassigned one only for reads, onto the all-ones page:
//! [`serve_page`]): an access to it aborts at its site, the resident walks it
//! (every #GP/#SS/#AC/#PF first), and completes the instruction from the site's completion record (fexc's decode of the
//! instruction it translated: register, merge, length; `code_object::completion`). The host only performs the device
//! access: [`serve`] answers [`exec::REQUEST_DEVICE`] with exactly one read or write, never decoding x86.
//!
//! **Concurrency contract (for X3, L3, L4).** Every vCPU thread calls [`serve`] on its own exits; a device is behind its
//! own mutex (or, a [`Direct`] device, its own lock), so admission calls are serialised, each vCPU's in its program
//! order. An access may complete immediately
//! or return a host-only deferred token. A deferred operation retains its device ordering; polling never reissues it.
//! Its resident releases shadow holdings and services RAM-window requests while waiting, and completes the decoded
//! instruction once after the final reply. No device mutex is held across that wait. A handler runs on the accessing vCPU's thread with
//! that vCPU stopped, gets its number (per-vCPU state such as a LAPIC is indexed by it) and must return promptly: it
//! never waits for another vCPU or for external input (a UART buffers its receive side), since peers waiting for this
//! vCPU (shootdowns) spin meanwhile and their watchdogs count the time. A handler may refuse an access it does not
//! implement (`None`/`false`: e.g. a width a register does not take); the vCPU then stops `unsupported_mmio` (status
//! `device`) at the instruction, so a refusal must leave the device unchanged. Memory ordering: the vCPU's earlier guest
//! accesses are complete before its exit (`dsb sy`), the mutex orders the handler against other vCPUs' handlers, and the
//! vCPU's later accesses start after the final reply. The guest tables' A/D updates of the access are made before the device is
//! asked, also when it is then refused (as on x86). Limits: the registry is fixed before the vCPUs run ([`Devices::freeze`];
//! residents cache claimed pages on that basis) and lies above guest
//! RAM (a relocatable range such as IA32_APIC_BASE's, or holes below the top of RAM, need M1's layout and a mutable
//! registry); every access is a host exit, except those a resident device answers first (the resident's `mmio::local`
//! hook, X3's per-vCPU LAPIC).
use crate::execution::Area;
use crate::report::Hex;
use natemu_abi::execution::{self as exec, MMIO_OFFSET, Mmio, State, mmio};
use serde::Serialize;
use std::collections::BTreeMap;
use std::sync::{Arc, Mutex};

/// A device's register file: `offset` is from the start of the range it was registered at; `size` is 1, 2, 4 or 8 and
/// the access never crosses a page. `vcpu` is the accessing vCPU. A read returns the value in its low `size` bytes. `None`
/// (read) or `false` (write): the device refuses the access, unchanged.
pub trait Device: Send {
    fn read(&mut self, vcpu: usize, offset: u64, size: u64) -> Option<u64>;
    fn write(&mut self, vcpu: usize, offset: u64, size: u64, value: u64) -> bool;
    /// MMIO only: accept this access once, or refuse it without effects. A deferred access owns
    /// its ordering/serialization until completion, including conflicting reads and control writes.
    /// Return promptly without waiting for a peer, a RAM window or backend I/O. Ports keep the
    /// synchronous read/write interface above.
    fn access(&mut self, vcpu: usize, offset: u64, size: u64, value: Option<u64>) -> Access {
        match value {
            Some(value) => {
                if self.write(vcpu, offset, size, value) {
                    Access::Completed(0)
                } else {
                    Access::Refused
                }
            }
            None => self
                .read(vcpu, offset, size)
                .map_or(Access::Refused, Access::Completed),
        }
    }
    /// Poll a previously accepted access without replaying it. Deferred must return the same token.
    /// Refused here is a terminal failure, not a promise that the accepted operation had no effects.
    /// A backend owns its in-flight buffers until completion/join even if the waiting vCPU stops.
    fn poll(&mut self, _vcpu: usize, _token: u64) -> Access {
        Access::Refused
    }
    /// Idempotently close new admission and poll accepted work/durability. Never wait here.
    /// True means all accepted effects and callbacks have retired successfully.
    fn quiesce(&mut self) -> crate::Result<bool> {
        Ok(true)
    }
    /// A handle the registry calls instead, without this device's mutex (asked once, at registration).
    fn direct(&self) -> Option<Arc<dyn Direct>> {
        None
    }
}
/// MMIO accesses of a device that serialises them itself: [`serve`] and [`serve_poll`] call it without the outer
/// mutex, so a vCPU never holds that mutex while waiting for the device's own lock. Same contract as [`Device::access`]
/// and [`Device::poll`].
pub trait Direct: Send + Sync {
    fn access(&self, vcpu: usize, offset: u64, size: u64, value: Option<u64>) -> Access;
    fn poll(&self, vcpu: usize, token: u64) -> Access;
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Access {
    Completed(u64),
    Deferred(u64),
    Refused,
}
pub type Shared = Arc<Mutex<dyn Device>>;
/// Host-only accepted MMIO identity. Keeping the endpoint alive does not cancel or join its worker;
/// VM lifecycle management must drain workers before freeing backend or guest backing resources.
pub struct Pending {
    device: Shared,
    direct: Option<Arc<dyn Direct>>,
    token: u64,
    request: Mmio,
    vcpu: usize,
    rip: u64,
}
/// A view of `.0` whose offsets start at `.1` (one device registered on several ranges, each told apart by its offset).
pub(crate) struct At(pub(crate) Shared, pub(crate) u64);
impl Device for At {
    fn quiesce(&mut self) -> crate::Result<bool> {
        self.0.lock().unwrap_or_else(|e| e.into_inner()).quiesce()
    }
    fn read(&mut self, vcpu: usize, offset: u64, size: u64) -> Option<u64> {
        self.0
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .read(vcpu, self.1 + offset, size)
    }
    fn write(&mut self, vcpu: usize, offset: u64, size: u64, value: u64) -> bool {
        self.0
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .write(vcpu, self.1 + offset, size, value)
    }
    fn access(&mut self, vcpu: usize, offset: u64, size: u64, value: Option<u64>) -> Access {
        self.0
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .access(vcpu, self.1 + offset, size, value)
    }
    fn poll(&mut self, vcpu: usize, token: u64) -> Access {
        self.0
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .poll(vcpu, token)
    }
}

/// The guest-physical device ranges of a VM and its I/O port bus (L3).
///
/// **Unassigned accesses** (owner decision, October 29: as on a PC): a read of a port or a device-range page no device
/// claims returns all-ones at its width, a write is ignored, and each is counted (per port, per page) for the run report;
/// with [`Devices::strict`] (a driver's debug switch) they are refused instead: the vCPU stops (`unsupported_port`, or
/// `unsupported_mmio` status `unassigned`) at the instruction. **Ports** are 16 bits; a device owns a range of them and
/// gets the offset from its range's start (its `Device` methods, sizes 1, 2 or 4). An access lying inside one range is
/// one call; one that is not (it starts or ends outside every range, or spans two) is made byte by byte, port k + i
/// modulo 10000H, each byte its owner's or the rule's; a device refusing a later byte then leaves the earlier bytes made
/// (not all-or-nothing: no device refuses a byte today, the UART takes every one; the strict switch refuses before any).
/// An access is counted at every port it touches.
///
/// **Pages** (amendment after L3): a read of an unassigned page is answered once per shadow fill ([`serve_page`]: the
/// resident maps the shared all-ones page read-only and every read form then runs natively), counted as a read of the
/// page; a write reaching [`serve`] is ignored and counted per access; under the strict switch both are refused (and
/// counted). The report's page `reads` are therefore fills, its `writes` accesses.
pub struct Devices {
    ranges: Vec<(u64, u64, Shared, Option<Arc<dyn Direct>>)>,
    /// Set once the registry is shared with the vCPUs ([`Devices::freeze`]): no range is added after it, so a resident
    /// may remember a page answered [`mmio::CLAIMED`] for the rest of the run.
    frozen: bool,
    /// Installed RAM size; the legacy and 3–4 GiB holes remain device space.
    ram_end: u64,
    /// L3: port ranges `[base, end)`.
    ports: Vec<(u32, u32, Shared)>,
    /// L3: the strict switch (see above).
    pub strict: bool,
    /// L3: [reads, writes] per port (every access, by its first port), and per unassigned page.
    port_counts: Mutex<BTreeMap<u16, [u64; 2]>>,
    page_counts: Mutex<BTreeMap<u64, [u64; 2]>>,
    /// L4a: the interrupt controllers (`irq::install`), for the resident's EOI broadcasts and INTA cycles.
    pub(crate) irqs: Option<crate::irq::Irqs>,
    /// L4b: a power request made by a device access (`pm::POWEROFF`, `pm::RESET`; 0 none): the accessing vCPU's run
    /// ends with that typed stop after the access.
    pub(crate) power: Arc<std::sync::atomic::AtomicU8>,
}
impl Devices {
    pub fn new(ram_end: u64) -> Self {
        Self {
            ranges: Vec::new(),
            frozen: false,
            ram_end,
            ports: Vec::new(),
            strict: false,
            port_counts: Mutex::default(),
            page_counts: Mutex::default(),
            irqs: None,
            power: Arc::default(),
        }
    }
    /// L4b: the power request made so far (0 none).
    pub(crate) fn power(&self) -> u8 {
        self.power.load(std::sync::atomic::Ordering::SeqCst)
    }
    /// Close admission everywhere, even if an earlier device is still draining.
    pub(crate) fn quiesce(&self) -> crate::Result<bool> {
        let mut done = true;
        for (_, _, device, _) in self.ranges.iter() {
            done &= device.lock().unwrap_or_else(|e| e.into_inner()).quiesce()?;
        }
        for (_, _, device) in &self.ports {
            done &= device.lock().unwrap_or_else(|e| e.into_inner()).quiesce()?;
        }
        Ok(done)
    }
    /// L3: registers `device` on ports `[base, base + count)`, inside the 16-bit space and not overlapping another range.
    pub fn register_ports(&mut self, base: u32, count: u32, device: Shared) -> crate::Result<()> {
        assert!(!self.frozen, "device registry frozen");
        let end = base + count;
        if count == 0 || end > 0x1_0000 || self.ports.iter().any(|&(b, e, _)| base < e && b < end) {
            return Err(format!(
                "port range {base:#x}+{count:#x}: inside 0..10000H, not overlapping another"
            ));
        }
        self.ports.push((base, end, device));
        Ok(())
    }
    fn port_owner(&self, port: u32) -> Option<&(u32, u32, Shared)> {
        self.ports.iter().find(|&&(b, e, _)| (b..e).contains(&port))
    }
    /// L3: one access of `size` bytes (1, 2 or 4) from `port` by vCPU `vcpu`: a write of `data`, or a read, whose value
    /// is returned; `Err` with the refusal ([`mmio::UNASSIGNED`] under the strict switch, [`mmio::DEVICE`]).
    pub fn port(&self, vcpu: usize, port: u16, size: u64, data: Option<u64>) -> Result<u64, u64> {
        let first = u32::from(port);
        {
            let mut counts = self.port_counts.lock().unwrap_or_else(|e| e.into_inner());
            for k in 0..size as u32 {
                counts.entry(((first + k) & 0xffff) as u16).or_default()
                    [usize::from(data.is_some())] += 1;
            }
        }
        let call = |d: &Shared, offset: u64, size: u64, data: Option<u64>| -> Result<u64, u64> {
            let mut d = d.lock().unwrap_or_else(|e| e.into_inner());
            match data {
                Some(v) => d.write(vcpu, offset, size, v & mask(size)).then_some(0),
                None => d.read(vcpu, offset, size).map(|v| v & mask(size)),
            }
            .ok_or(mmio::DEVICE)
        };
        if let Some((b, _, d)) = self
            .port_owner(first)
            .filter(|&&(_, e, _)| first + size as u32 <= e)
        {
            return call(d, u64::from(first - b), size, data);
        }
        let bytes: Vec<u32> = (0..size as u32).map(|k| (first + k) & 0xffff).collect();
        if self.strict && bytes.iter().any(|&p| self.port_owner(p).is_none()) {
            return Err(mmio::UNASSIGNED);
        }
        let mut value = 0;
        for (k, &p) in bytes.iter().enumerate() {
            let byte = match self.port_owner(p) {
                Some((b, _, d)) => call(d, u64::from(p - b), 1, data.map(|v| v >> (8 * k)))?,
                None => 0xff,
            };
            value |= (byte & 0xff) << (8 * k);
        }
        Ok(if data.is_some() { 0 } else { value })
    }
    /// L3: the run report's port and unassigned-page counts.
    pub fn report(&self) -> AccessCounts {
        let ports = self.port_counts.lock().unwrap_or_else(|e| e.into_inner());
        let pages = self.page_counts.lock().unwrap_or_else(|e| e.into_inner());
        AccessCounts::new(&ports, &pages, |p| self.port_owner(u32::from(p)).is_none())
    }
    /// Registers `device` at `[base, base + bytes)`: page-aligned, outside guest RAM (above it, or inside the legacy hole,
    /// `exec::LEGACY_HOLE`), below MAXPHYADDR (40 bits), not overlapping another range.
    pub fn register(&mut self, base: u64, bytes: u64, device: Shared) -> crate::Result<()> {
        assert!(!self.frozen, "device registry frozen");
        let end = base
            .checked_add(bytes)
            .filter(|&e| bytes > 0 && e <= 1 << 40);
        let Some(end) =
            end.filter(|&e| base % 4096 == 0 && bytes % 4096 == 0 && self.not_ram(base, e))
        else {
            return Err(format!(
                "device range {base:#x}+{bytes:#x}: page-aligned, outside guest RAM, below 2^40"
            ));
        };
        if self.ranges.iter().any(|&(b, e, ..)| base < e && b < end) {
            return Err(format!(
                "device range {base:#x}+{bytes:#x} overlaps another"
            ));
        }
        let direct = device.lock().unwrap_or_else(|e| e.into_inner()).direct();
        self.ranges.push((base, end, device, direct));
        Ok(())
    }
    /// The registry as the vCPUs share it, fixed from now on (no hotplug, no relocatable range).
    pub fn freeze(mut self) -> Arc<Self> {
        self.frozen = true;
        Arc::new(self)
    }
    fn not_ram(&self, base: u64, end: u64) -> bool {
        base >= exec::ram_end(self.ram_end)
            || exec::LEGACY_HOLE.contains(&base) && end <= exec::LEGACY_HOLE.end
            || base >= self.ram_end.min(exec::LOW_RAM_END) && end <= exec::HIGH_RAM_START
    }
    pub fn find(&self, gpa: u64) -> Option<(&Shared, u64)> {
        self.entry(gpa).map(|(d, _, offset)| (d, offset))
    }
    fn entry(&self, gpa: u64) -> Option<(&Shared, &Option<Arc<dyn Direct>>, u64)> {
        self.ranges
            .iter()
            .find(|&&(b, e, ..)| (b..e).contains(&gpa))
            .map(|(b, _, d, direct)| (d, direct, gpa - b))
    }
}
pub fn mask(bytes: u64) -> u64 {
    if bytes >= 8 {
        u64::MAX
    } else {
        (1 << (8 * bytes)) - 1
    }
}

/// Serves vCPU `area.vcpu`'s device request ([`exec::REQUEST_DEVICE`]): one read or write of the device at the request's
/// address, its value or refusal into the [`Mmio`] record, and the reply. A request the resident could not have made (a
/// width other than 1, 2, 4, 8 or an access leaving its page) is a bad reply, never a device access.
pub(crate) fn serve(
    area: Area,
    state: &mut State,
    devices: Option<&Devices>,
    pending: &mut Option<Pending>,
) {
    let m = begin(
        area.vcpu,
        state.cpu.rip,
        area.read_record(MMIO_OFFSET),
        devices,
        pending,
    );
    area.write_record(MMIO_OFFSET, m);
    state.reply = exec::REPLY_DEVICE;
}
fn begin(
    vcpu: usize,
    rip: u64,
    mut m: Mmio,
    devices: Option<&Devices>,
    pending: &mut Option<Pending>,
) -> Mmio {
    // A device access to guest RAM is a resident error, never answered (L3: not all-ones either).
    let well_formed = matches!(m.width, 1 | 2 | 4 | 8)
        && (m.gpa & 4095) + m.width <= 4096
        && m.write <= 1
        && devices.is_none_or(|d| d.not_ram(m.gpa & !4095, (m.gpa & !4095) + 4096));
    m.status = match devices.map(|d| (d, d.entry(m.gpa))) {
        _ if !well_formed || pending.is_some() => 0,
        None => mmio::UNASSIGNED,
        // L3: the unassigned rule (or, strict, the refusal), counted either way.
        Some((d, None)) => {
            d.page_counts
                .lock()
                .unwrap_or_else(|e| e.into_inner())
                .entry(m.gpa & !4095)
                .or_default()[m.write as usize] += 1;
            if d.strict {
                m.status = mmio::UNASSIGNED;
            } else if m.write == 0 {
                m.value = mask(m.width);
            }
            if d.strict {
                mmio::UNASSIGNED
            } else {
                mmio::DONE
            }
        }
        Some((_, Some((device, direct, offset)))) => {
            let value = (m.write != 0).then_some(m.value & mask(m.width));
            let access = match direct {
                Some(d) => d.access(vcpu, offset, m.width, value),
                None => device
                    .lock()
                    .unwrap_or_else(|e| e.into_inner())
                    .access(vcpu, offset, m.width, value),
            };
            match access {
                Access::Completed(value) => {
                    if m.write == 0 {
                        m.value = value & mask(m.width);
                    }
                    mmio::DONE
                }
                Access::Deferred(token) => {
                    *pending = Some(Pending {
                        device: device.clone(),
                        direct: direct.clone(),
                        token,
                        request: m,
                        vcpu,
                        rip,
                    });
                    mmio::PENDING
                }
                Access::Refused => mmio::DEVICE,
            }
        }
    };
    m
}

/// Poll only the retained host identity. A malformed/replayed poll never calls a device. This
/// handler returns promptly; the resident then rechecks the RAM-window request before polling again.
pub(crate) fn serve_poll(area: Area, state: &mut State, pending: &mut Option<Pending>) {
    let m = poll_request(
        area.vcpu,
        state.cpu.rip,
        area.read_record(MMIO_OFFSET),
        pending,
    );
    area.write_record(MMIO_OFFSET, m);
    state.reply = exec::REPLY_DEVICE_POLL;
}
fn poll_request(vcpu: usize, rip: u64, mut m: Mmio, pending: &mut Option<Pending>) -> Mmio {
    m.status = 0;
    if let Some(p) = pending.as_ref().filter(|p| {
        p.vcpu == vcpu
            && p.rip == rip
            && p.request.gpa == m.gpa
            && p.request.width == m.width
            && p.request.write == m.write
            && p.request.completed == m.completed
            && (m.write == 0 || p.request.value == m.value)
    }) {
        let result = match &p.direct {
            Some(d) => d.poll(p.vcpu, p.token),
            None => p
                .device
                .lock()
                .unwrap_or_else(|e| e.into_inner())
                .poll(p.vcpu, p.token),
        };
        m = p.request;
        m.status = match result {
            Access::Completed(value) => {
                if m.write == 0 {
                    m.value = value & mask(m.width);
                }
                mmio::DONE
            }
            Access::Deferred(token) if token == p.token => mmio::PENDING,
            Access::Deferred(_) => 0,
            Access::Refused => mmio::FAILED,
        };
        if m.status != mmio::PENDING {
            *pending = None;
        }
    }
    m
}

/// L3 (amendment): serves vCPU `area.vcpu`'s page request ([`exec::REQUEST_PAGE`]): a read touched the non-RAM page
/// `Mmio::gpa`. `CLAIMED` if a device claims it, else (unassigned) the fill is counted as a read and answered `DONE` (the
/// resident maps the all-ones page), or `UNASSIGNED` under the strict switch. A page that is RAM or not page-aligned is a
/// bad reply.
pub(crate) fn serve_page(area: Area, state: &mut State, devices: Option<&Devices>) {
    let mut m: Mmio = area.read_record(MMIO_OFFSET);
    m.status = page_status(devices, m.gpa);
    area.write_record(MMIO_OFFSET, m);
    state.reply = exec::REPLY_PAGE;
}
fn page_status(devices: Option<&Devices>, gpa: u64) -> u64 {
    let m = Mmio {
        gpa,
        ..Mmio::default()
    };
    match devices {
        Some(d) if m.gpa % 4096 == 0 && m.gpa < 1 << 40 && d.not_ram(m.gpa, m.gpa + 4096) => {
            match d.find(m.gpa) {
                // The resident caches the claim for the run: only a frozen registry may make one.
                Some(_) if d.frozen => mmio::CLAIMED,
                Some(_) => panic!("device registry not frozen"),
                None => {
                    d.page_counts
                        .lock()
                        .unwrap_or_else(|e| e.into_inner())
                        .entry(m.gpa)
                        .or_default()[0] += 1;
                    if d.strict {
                        mmio::UNASSIGNED
                    } else {
                        mmio::DONE
                    }
                }
            }
        }
        _ => 0,
    }
}

/// L3: serves vCPU `area.vcpu`'s port request ([`exec::REQUEST_PORT`]): one access on the port bus ([`Devices::port`]),
/// its value or refusal into the [`Mmio`] record (`gpa` = the port), and the reply. A request the resident could not have
/// made (a width other than 1, 2, 4, a port above FFFFH) is a bad reply, never an access.
pub(crate) fn serve_port(area: Area, state: &mut State, devices: Option<&Devices>) {
    let mut m: Mmio = area.read_record(MMIO_OFFSET);
    let well_formed = matches!(m.width, 1 | 2 | 4) && m.gpa <= 0xffff && m.write <= 1;
    m.status = match devices {
        _ if !well_formed => 0,
        None => mmio::UNASSIGNED,
        Some(d) => match d.port(
            area.vcpu,
            m.gpa as u16,
            m.width,
            (m.write != 0).then_some(m.value),
        ) {
            Ok(v) => {
                m.value = v;
                mmio::DONE
            }
            Err(why) => why,
        },
    };
    area.write_record(MMIO_OFFSET, m);
    state.reply = exec::REPLY_PORT;
}

#[cfg(test)]
mod capacity_tests {
    use super::*;
    #[test]
    fn high_ram_cannot_be_registered_as_device_space() {
        let d = Devices::new(8 << 30);
        for (base, end, device) in [
            (0xa0000, 0x100000, true),
            ((3 << 30) - 4096, 3 << 30, false),
            (3 << 30, 4 << 30, true),
            ((4 << 30) - 4096, (4 << 30) + 4096, false),
            (4 << 30, (4 << 30) + 4096, false),
            ((9 << 30) - 4096, 9 << 30, false),
            (9 << 30, (9 << 30) + 4096, true),
        ] {
            assert_eq!(d.not_ram(base, end), device, "{base:#x}..{end:#x}");
        }
    }
}

#[cfg(test)]
mod deferred_tests {
    use super::*;
    use std::collections::VecDeque;
    const BASE: u64 = 0xf000_0000;
    const RIP: u64 = 0x1000;
    #[derive(Default)]
    struct Mock {
        accesses: Vec<(usize, u64, u64, Option<u64>)>,
        polls: Vec<(usize, u64)>,
        replies: VecDeque<Access>,
    }
    impl Device for Mock {
        fn read(&mut self, _: usize, _: u64, _: u64) -> Option<u64> {
            panic!("deferred MMIO must use access")
        }
        fn write(&mut self, _: usize, _: u64, _: u64, _: u64) -> bool {
            panic!("deferred MMIO must use access")
        }
        fn access(&mut self, v: usize, o: u64, s: u64, x: Option<u64>) -> Access {
            self.accesses.push((v, o, s, x));
            Access::Deferred(42)
        }
        fn poll(&mut self, v: usize, token: u64) -> Access {
            self.polls.push((v, token));
            self.replies.pop_front().unwrap_or(Access::Deferred(42))
        }
    }
    fn setup(replies: &[Access]) -> (Devices, Arc<Mutex<Mock>>, Mmio) {
        let mut bus = Devices::new(64 << 20);
        let device = Arc::new(Mutex::new(Mock {
            replies: replies.iter().copied().collect(),
            ..Default::default()
        }));
        bus.register(BASE, 4096, device.clone()).unwrap();
        (
            bus,
            device,
            Mmio {
                gpa: BASE + 4,
                width: 4,
                completed: 7,
                ..Mmio::default()
            },
        )
    }
    #[test]
    fn deferred_access_is_issued_once_and_load_result_is_masked() {
        let (bus, device, m) = setup(&[Access::Deferred(42), Access::Completed(u64::MAX)]);
        let mut pending = None;
        let mut m = begin(2, RIP, m, Some(&bus), &mut pending);
        assert_eq!(m.status, mmio::PENDING);
        assert_eq!(m.value, 0, "host token must not be exposed as read data");
        m = poll_request(2, RIP, m, &mut pending);
        assert_eq!(m.status, mmio::PENDING);
        m = poll_request(2, RIP, m, &mut pending);
        assert_eq!(
            (m.status, m.value, m.completed),
            (mmio::DONE, 0xffff_ffff, 7)
        );
        assert!(pending.is_none());
        let d = device.lock().unwrap();
        assert_eq!(d.accesses, [(2, 4, 4, None)]);
        assert_eq!(d.polls, [(2, 42), (2, 42)]);
    }
    #[test]
    fn changed_identity_and_duplicate_request_never_replay() {
        let (bus, device, m) = setup(&[Access::Completed(5)]);
        let mut pending = None;
        let m = begin(2, RIP, m, Some(&bus), &mut pending);
        assert_eq!(begin(2, RIP, m, Some(&bus), &mut pending).status, 0);
        for changed in [
            Mmio {
                gpa: m.gpa + 4,
                ..m
            },
            Mmio { width: 8, ..m },
            Mmio { write: 1, ..m },
            Mmio { completed: 8, ..m },
        ] {
            assert_eq!(poll_request(2, RIP, changed, &mut pending).status, 0);
        }
        assert_eq!(poll_request(3, RIP, m, &mut pending).status, 0);
        assert_eq!(poll_request(2, RIP + 1, m, &mut pending).status, 0);
        assert!(device.lock().unwrap().polls.is_empty());
        assert_eq!(device.lock().unwrap().accesses.len(), 1);
        assert_eq!(poll_request(2, RIP, m, &mut pending).status, mmio::DONE);
    }
    #[test]
    fn unsolicited_and_replayed_polls_never_touch_device() {
        let (bus, device, m) = setup(&[Access::Completed(1)]);
        let mut pending = None;
        assert_eq!(poll_request(0, RIP, m, &mut pending).status, 0);
        let m = begin(0, RIP, m, Some(&bus), &mut pending);
        let done = poll_request(0, RIP, m, &mut pending);
        assert_eq!(done.status, mmio::DONE);
        assert_eq!(poll_request(0, RIP, done, &mut pending).status, 0);
        assert_eq!(device.lock().unwrap().polls.len(), 1);
    }
    #[test]
    fn refused_poll_and_replaced_token_are_terminal_without_replay() {
        for (reply, status) in [(Access::Refused, mmio::FAILED), (Access::Deferred(43), 0)] {
            let (bus, device, m) = setup(&[reply]);
            let mut pending = None;
            let m = begin(0, RIP, m, Some(&bus), &mut pending);
            assert_eq!(poll_request(0, RIP, m, &mut pending).status, status);
            assert!(pending.is_none());
            assert_eq!(device.lock().unwrap().accesses.len(), 1);
        }
    }
    #[test]
    fn range_view_forwards_offset_and_stable_poll_token() {
        let (_, device, m) = setup(&[Access::Completed(9)]);
        let mut bus = Devices::new(64 << 20);
        bus.register(BASE, 4096, Arc::new(Mutex::new(At(device.clone(), 256))))
            .unwrap();
        let mut pending = None;
        let m = begin(0, RIP, m, Some(&bus), &mut pending);
        assert_eq!(poll_request(0, RIP, m, &mut pending).value, 9);
        let d = device.lock().unwrap();
        assert_eq!(d.accesses, [(0, 260, 4, None)]);
        assert_eq!(d.polls, [(0, 42)]);
    }
    #[test]
    fn store_identity_is_retained_and_endpoint_outlives_registry() {
        let (bus, device, mut m) = setup(&[Access::Completed(0)]);
        (m.write, m.value) = (1, u64::MAX);
        let weak = Arc::downgrade(&device);
        let mut pending = None;
        let m = begin(0, RIP, m, Some(&bus), &mut pending);
        assert_eq!(device.lock().unwrap().accesses[0].3, Some(0xffff_ffff));
        drop(device);
        drop(bus);
        assert!(weak.upgrade().is_some());
        assert_eq!(
            poll_request(0, RIP, Mmio { value: 0, ..m }, &mut pending).status,
            0
        );
        let m = poll_request(0, RIP, m, &mut pending);
        assert_eq!((m.status, m.value), (mmio::DONE, u64::MAX));
        assert!(weak.upgrade().is_none());
    }
    #[test]
    fn claimed_pages_are_answered_only_by_a_frozen_registry() {
        let (bus, ..) = setup(&[]);
        // Before freezing a claim could still change, so no resident may cache it: see the next test.
        let bus = bus.freeze();
        assert_eq!(page_status(Some(&bus), BASE), mmio::CLAIMED);
        // Unassigned pages keep their per-fill counting and the strict refusal.
        assert_eq!(page_status(Some(&bus), BASE + 4096), mmio::DONE);
        assert_eq!(page_status(Some(&bus), BASE + 4096), mmio::DONE);
        assert_eq!(bus.report().unassigned_pages[0].reads, 2);
        let (mut strict, ..) = setup(&[]);
        strict.strict = true;
        assert_eq!(
            page_status(Some(&strict.freeze()), BASE + 4096),
            mmio::UNASSIGNED
        );
        assert_eq!(page_status(Some(&bus), BASE + 1), 0);
    }
    #[test]
    #[should_panic(expected = "device registry not frozen")]
    fn an_unfrozen_registry_never_answers_a_claim() {
        let (bus, ..) = setup(&[]);
        page_status(Some(&bus), BASE);
    }
    #[test]
    #[should_panic(expected = "device registry frozen")]
    fn a_frozen_registry_takes_no_range() {
        let (bus, device, _) = setup(&[]);
        let mut bus = Arc::try_unwrap(bus.freeze()).ok().unwrap();
        bus.register(BASE + 4096, 4096, device).unwrap();
    }
    #[test]
    fn direct_devices_are_served_without_their_mutex() {
        struct Locked(Arc<Mock2>);
        #[derive(Default)]
        struct Mock2(Mutex<Vec<u64>>);
        impl Direct for Mock2 {
            fn access(&self, _: usize, o: u64, _: u64, _: Option<u64>) -> Access {
                self.0.lock().unwrap().push(o);
                Access::Deferred(5)
            }
            fn poll(&self, _: usize, t: u64) -> Access {
                Access::Completed(t)
            }
        }
        impl Device for Locked {
            fn read(&mut self, _: usize, _: u64, _: u64) -> Option<u64> {
                None
            }
            fn write(&mut self, _: usize, _: u64, _: u64, _: u64) -> bool {
                false
            }
            fn direct(&self) -> Option<Arc<dyn Direct>> {
                Some(self.0.clone())
            }
        }
        let inner = Arc::new(Mock2::default());
        let device: Shared = Arc::new(Mutex::new(Locked(inner.clone())));
        let mut bus = Devices::new(64 << 20);
        bus.register(BASE, 4096, device.clone()).unwrap();
        // The outer mutex stays held by a peer: access and poll still complete.
        let _held = device.lock().unwrap();
        let mut pending = None;
        let m = Mmio {
            gpa: BASE + 8,
            width: 4,
            ..Mmio::default()
        };
        let m = begin(0, RIP, m, Some(&bus), &mut pending);
        assert_eq!(m.status, mmio::PENDING);
        let m = poll_request(0, RIP, m, &mut pending);
        assert_eq!((m.status, m.value), (mmio::DONE, 5));
        assert_eq!(*inner.0.lock().unwrap(), [8]);
    }
    #[test]
    fn legacy_default_access_and_port_contract_remain_synchronous() {
        struct Legacy;
        impl Device for Legacy {
            fn read(&mut self, _: usize, _: u64, size: u64) -> Option<u64> {
                (size == 4).then_some(0x1234)
            }
            fn write(&mut self, _: usize, _: u64, size: u64, _: u64) -> bool {
                size == 4
            }
        }
        let mut d = Legacy;
        assert_eq!(d.access(0, 0, 4, None), Access::Completed(0x1234));
        assert_eq!(d.access(0, 0, 4, Some(1)), Access::Completed(0));
        assert_eq!(d.access(0, 0, 8, None), Access::Refused);
        let mut bus = Devices::new(64 << 20);
        bus.register_ports(0x100, 8, Arc::new(Mutex::new(d)))
            .unwrap();
        assert_eq!(bus.port(0, 0x100, 4, None), Ok(0x1234));
        assert_eq!(bus.port(0, 0x100, 4, Some(2)), Ok(0));
    }
}

/// The run report's port and unassigned-page counts (JSON members `"ports":[..],"unassigned_pages":[..]`).
#[derive(Debug, Default, Serialize)]
pub struct AccessCounts {
    pub ports: Vec<PortCount>,
    pub unassigned_pages: Vec<PageCount>,
}
#[derive(Debug, Serialize)]
pub struct PortCount {
    port: Hex,
    reads: u64,
    writes: u64,
    unassigned: bool,
}
#[derive(Debug, Serialize)]
pub struct PageCount {
    page: Hex,
    reads: u64,
    writes: u64,
}
impl AccessCounts {
    fn new(
        ports: &BTreeMap<u16, [u64; 2]>,
        pages: &BTreeMap<u64, [u64; 2]>,
        unassigned: impl Fn(u16) -> bool,
    ) -> Self {
        Self {
            ports: ports
                .iter()
                .map(|(&p, &[reads, writes])| PortCount {
                    port: Hex(p.into()),
                    reads,
                    writes,
                    unassigned: unassigned(p),
                })
                .collect(),
            unassigned_pages: pages
                .iter()
                .map(|(&g, &[reads, writes])| PageCount {
                    page: Hex(g),
                    reads,
                    writes,
                })
                .collect(),
        }
    }
    pub fn is_empty(&self) -> bool {
        self.ports.is_empty() && self.unassigned_pages.is_empty()
    }
}

#[cfg(test)]
mod report_tests {
    use super::*;
    #[test]
    fn access_counts_match_the_former_format() {
        let ports = BTreeMap::from([(0x20u16, [3, 1]), (0x3f8, [0, 9])]);
        let pages = BTreeMap::from([(0xfee0_0000u64, [2, 0])]);
        let unassigned = |p: u16| p == 0x20;
        let old_ports = ports.iter().map(|(&p, [r, w])| {
            let unassigned = unassigned(p);
            format!(
                "{{\"port\":\"{p:#x}\",\"reads\":{r},\"writes\":{w},\"unassigned\":{unassigned}}}"
            )
        });
        let old_pages = pages
            .iter()
            .map(|(&g, [r, w])| format!("{{\"page\":\"{g:#x}\",\"reads\":{r},\"writes\":{w}}}"));
        let old = format!(
            "\"ports\":[{}],\"unassigned_pages\":[{}]",
            old_ports.collect::<Vec<_>>().join(","),
            old_pages.collect::<Vec<_>>().join(",")
        );
        let old: serde_json::Value = serde_json::from_str(&format!("{{{old}}}")).unwrap();
        let new = AccessCounts::new(&ports, &pages, unassigned);
        assert_eq!(old, serde_json::to_value(&new).unwrap());
        assert!(!new.is_empty());
        assert!(AccessCounts::new(&BTreeMap::new(), &BTreeMap::new(), unassigned).is_empty());
    }
}
