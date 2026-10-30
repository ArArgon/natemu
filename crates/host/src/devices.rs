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
//! own mutex, so its accesses are serialised in one total order, each vCPU's in its program order (an access is
//! synchronous: the vCPU waits in the host until its handler returns). A handler runs on the accessing vCPU's thread with
//! that vCPU stopped, gets its number (per-vCPU state such as a LAPIC is indexed by it) and must return promptly: it
//! never waits for another vCPU or for external input (a UART buffers its receive side), since peers waiting for this
//! vCPU (shootdowns) spin meanwhile and their watchdogs count the time. A handler may refuse an access it does not
//! implement (`None`/`false`: e.g. a width a register does not take); the vCPU then stops `unsupported_mmio` (status
//! `device`) at the instruction, so a refusal must leave the device unchanged. Memory ordering: the vCPU's earlier guest
//! accesses are complete before its exit (`dsb sy`), the mutex orders the handler against other vCPUs' handlers, and the
//! vCPU's later accesses start after the reply. The guest tables' A/D updates of the access are made before the device is
//! asked, also when it is then refused (as on x86). Limits: the registry is fixed before the vCPUs run and lies above guest
//! RAM (a relocatable range such as IA32_APIC_BASE's, or holes below the top of RAM, need M1's layout and a mutable
//! registry); every access is a host exit, except those a resident device answers first (the resident's `mmio::local`
//! hook, X3's per-vCPU LAPIC).
use crate::execution::Area;
use natemu_abi::execution::{self as exec, MMIO_OFFSET, Mmio, State, cache::MAX_VCPUS, mmio};
use std::collections::BTreeMap;
use std::sync::{Arc, Mutex};

/// A device's register file: `offset` is from the start of the range it was registered at; `size` is 1, 2, 4 or 8 and
/// the access never crosses a page. `vcpu` is the accessing vCPU. A read returns the value in its low `size` bytes. `None`
/// (read) or `false` (write): the device refuses the access, unchanged.
pub(crate) trait Device: Send {
    fn read(&mut self, vcpu: usize, offset: u64, size: u64) -> Option<u64>;
    fn write(&mut self, vcpu: usize, offset: u64, size: u64, value: u64) -> bool;
}
pub(crate) type Shared = Arc<Mutex<dyn Device>>;

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
pub(crate) struct Devices {
    ranges: Vec<(u64, u64, Shared)>,
    /// The end of the VM's guest RAM (M1: its run-time size): no device below it but in the legacy hole.
    ram_end: u64,
    /// L3: port ranges `[base, end)`.
    ports: Vec<(u32, u32, Shared)>,
    /// L3: the strict switch (see above).
    pub(crate) strict: bool,
    /// L3: [reads, writes] per port (every access, by its first port), and per unassigned page.
    port_counts: Mutex<BTreeMap<u16, [u64; 2]>>,
    page_counts: Mutex<BTreeMap<u64, [u64; 2]>>,
    /// L4a: the interrupt controllers (`irq::install`), for the resident's EOI broadcasts and INTA cycles.
    pub(crate) irqs: Option<crate::irq::Irqs>,
}
impl Devices {
    pub(crate) fn new(ram_end: u64) -> Self {
        Self { ranges: Vec::new(), ram_end, ports: Vec::new(), strict: false, port_counts: Mutex::default(), page_counts: Mutex::default(), irqs: None }
    }
    /// L3: registers `device` on ports `[base, base + count)`, inside the 16-bit space and not overlapping another range.
    pub(crate) fn register_ports(&mut self, base: u32, count: u32, device: Shared) -> crate::Result<()> {
        let end = base + count;
        if count == 0 || end > 0x1_0000 || self.ports.iter().any(|&(b, e, _)| base < e && b < end) {
            return Err(format!("port range {base:#x}+{count:#x}: inside 0..10000H, not overlapping another"));
        }
        self.ports.push((base, end, device));
        Ok(())
    }
    fn port_owner(&self, port: u32) -> Option<&(u32, u32, Shared)> {
        self.ports.iter().find(|&&(b, e, _)| (b..e).contains(&port))
    }
    /// L3: one access of `size` bytes (1, 2 or 4) from `port` by vCPU `vcpu`: a write of `data`, or a read, whose value
    /// is returned; `Err` with the refusal ([`mmio::UNASSIGNED`] under the strict switch, [`mmio::DEVICE`]).
    pub(crate) fn port(&self, vcpu: usize, port: u16, size: u64, data: Option<u64>) -> Result<u64, u64> {
        let first = u32::from(port);
        {
            let mut counts = self.port_counts.lock().unwrap_or_else(|e| e.into_inner());
            for k in 0..size as u32 {
                counts.entry(((first + k) & 0xffff) as u16).or_default()[usize::from(data.is_some())] += 1;
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
        if let Some((b, _, d)) = self.port_owner(first).filter(|&&(_, e, _)| first + size as u32 <= e) {
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
    /// L3: the run report's port and unassigned-page counts, as JSON members (`"ports":[..],"unassigned_pages":[..]`).
    pub(crate) fn report(&self) -> String {
        let ports = self.port_counts.lock().unwrap_or_else(|e| e.into_inner());
        let ports = ports.iter().map(|(&p, [r, w])| {
            let unassigned = self.port_owner(u32::from(p)).is_none();
            format!("{{\"port\":\"{p:#x}\",\"reads\":{r},\"writes\":{w},\"unassigned\":{unassigned}}}")
        });
        let pages = self.page_counts.lock().unwrap_or_else(|e| e.into_inner());
        let pages = pages.iter().map(|(&g, [r, w])| format!("{{\"page\":\"{g:#x}\",\"reads\":{r},\"writes\":{w}}}"));
        format!("\"ports\":[{}],\"unassigned_pages\":[{}]", ports.collect::<Vec<_>>().join(","), pages.collect::<Vec<_>>().join(","))
    }
    /// Registers `device` at `[base, base + bytes)`: page-aligned, outside guest RAM (above it, or inside the legacy hole,
    /// `exec::LEGACY_HOLE`), below MAXPHYADDR (40 bits), not overlapping another range.
    pub(crate) fn register(&mut self, base: u64, bytes: u64, device: Shared) -> crate::Result<()> {
        let end = base.checked_add(bytes).filter(|&e| bytes > 0 && e <= 1 << 40);
        let Some(end) = end.filter(|&e| base % 4096 == 0 && bytes % 4096 == 0 && self.not_ram(base, e)) else {
            return Err(format!("device range {base:#x}+{bytes:#x}: page-aligned, outside guest RAM, below 2^40"));
        };
        if self.ranges.iter().any(|&(b, e, _)| base < e && b < end) {
            return Err(format!("device range {base:#x}+{bytes:#x} overlaps another"));
        }
        self.ranges.push((base, end, device));
        Ok(())
    }
    fn not_ram(&self, base: u64, end: u64) -> bool {
        base >= self.ram_end || exec::LEGACY_HOLE.contains(&base) && end <= exec::LEGACY_HOLE.end
    }
    fn find(&self, gpa: u64) -> Option<(&Shared, u64)> {
        self.ranges.iter().find(|&&(b, e, _)| (b..e).contains(&gpa)).map(|(b, _, d)| (d, gpa - b))
    }
}
fn mask(bytes: u64) -> u64 {
    if bytes >= 8 { u64::MAX } else { (1 << (8 * bytes)) - 1 }
}

/// Serves vCPU `area.vcpu`'s device request ([`exec::REQUEST_DEVICE`]): one read or write of the device at the request's
/// address, its value or refusal into the [`Mmio`] record, and the reply. A request the resident could not have made (a
/// width other than 1, 2, 4, 8 or an access leaving its page) is a bad reply, never a device access.
pub(crate) fn serve(area: Area, state: &mut State, devices: Option<&Devices>) {
    let mut m: Mmio = area.read_record(MMIO_OFFSET);
    // A device access to guest RAM is a resident error, never answered (L3: not all-ones either).
    let well_formed = matches!(m.width, 1 | 2 | 4 | 8)
        && (m.gpa & 4095) + m.width <= 4096
        && m.write <= 1
        && devices.is_none_or(|d| d.not_ram(m.gpa & !4095, (m.gpa & !4095) + 4096));
    m.status = match devices.map(|d| (d, d.find(m.gpa))) {
        _ if !well_formed => 0,
        None => mmio::UNASSIGNED,
        // L3: the unassigned rule (or, strict, the refusal), counted either way.
        Some((d, None)) => {
            d.page_counts.lock().unwrap_or_else(|e| e.into_inner()).entry(m.gpa & !4095).or_default()[m.write as usize] += 1;
            if d.strict {
                m.status = mmio::UNASSIGNED;
            } else if m.write == 0 {
                m.value = mask(m.width);
            }
            if d.strict { mmio::UNASSIGNED } else { mmio::DONE }
        }
        Some((_, Some((device, offset)))) => {
            let mut d = device.lock().unwrap_or_else(|e| e.into_inner());
            let done = if m.write != 0 {
                d.write(area.vcpu, offset, m.width, m.value & mask(m.width))
            } else {
                d.read(area.vcpu, offset, m.width).map(|v| m.value = v & mask(m.width)).is_some()
            };
            if done { mmio::DONE } else { mmio::DEVICE }
        }
    };
    area.write_record(MMIO_OFFSET, m);
    state.reply = exec::REPLY_DEVICE;
}

/// L3 (amendment): serves vCPU `area.vcpu`'s page request ([`exec::REQUEST_PAGE`]): a read touched the non-RAM page
/// `Mmio::gpa`. `CLAIMED` if a device claims it, else (unassigned) the fill is counted as a read and answered `DONE` (the
/// resident maps the all-ones page), or `UNASSIGNED` under the strict switch. A page that is RAM or not page-aligned is a
/// bad reply.
pub(crate) fn serve_page(area: Area, state: &mut State, devices: Option<&Devices>) {
    let mut m: Mmio = area.read_record(MMIO_OFFSET);
    m.status = match devices {
        Some(d) if m.gpa % 4096 == 0 && m.gpa < 1 << 40 && d.not_ram(m.gpa, m.gpa + 4096) => match d.find(m.gpa) {
            Some(_) => mmio::CLAIMED,
            None => {
                d.page_counts.lock().unwrap_or_else(|e| e.into_inner()).entry(m.gpa).or_default()[0] += 1;
                if d.strict { mmio::UNASSIGNED } else { mmio::DONE }
            }
        },
        _ => 0,
    };
    area.write_record(MMIO_OFFSET, m);
    state.reply = exec::REPLY_PAGE;
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
        Some(d) => match d.port(area.vcpu, m.gpa as u16, m.width, (m.write != 0).then_some(m.value)) {
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

/// A test device for the M3 checks: one page. `COUNTER` (8 bytes) reads as the number of earlier reads of it (each read
/// counts, whatever its size); `SEQUENCE` (8 bytes) checks order: the n-th write to it by a vCPU must carry n (else an
/// order error), and it reads as that vCPU's next n. The last 8 bytes refuse every access (`Device`'s refusal). Every
/// other byte is plain memory (an access straddling into the
/// registers from below is memory too). Every access is counted per vCPU and logged (the first `LOG` of them).
pub(crate) struct TestDevice {
    pub(crate) bytes: [u8; 4096],
    counter: u64,
    sequence: [u64; MAX_VCPUS],
    pub(crate) reads: [u64; MAX_VCPUS],
    pub(crate) writes: [u64; MAX_VCPUS],
    pub(crate) order_errors: u64,
    /// (vCPU, offset, size, 1 for a write, value).
    pub(crate) log: Vec<[u64; 5]>,
}
impl TestDevice {
    pub(crate) const COUNTER: u64 = 0xf00;
    pub(crate) const SEQUENCE: u64 = 0xf08;
    /// Accesses reaching the page's last 8 bytes are refused (uncounted, unlogged).
    pub(crate) const REFUSE: u64 = 0xff8;
    const LOG: usize = 4096;
    pub(crate) fn new() -> Self {
        Self { bytes: [0; 4096], counter: 0, sequence: [0; MAX_VCPUS], reads: [0; MAX_VCPUS], writes: [0; MAX_VCPUS], order_errors: 0, log: vec![] }
    }
    fn register(offset: u64, size: u64) -> Option<u64> {
        let reg = offset & !7;
        (matches!(reg, Self::COUNTER | Self::SEQUENCE) && (offset & 7) + size <= 8).then_some(reg)
    }
    fn logged(&mut self, entry: [u64; 5]) {
        if self.log.len() < Self::LOG {
            self.log.push(entry);
        }
    }
}
impl Device for TestDevice {
    fn read(&mut self, vcpu: usize, offset: u64, size: u64) -> Option<u64> {
        if offset + size > Self::REFUSE {
            return None;
        }
        self.reads[vcpu] += 1;
        let shift = 8 * (offset & 7);
        let value = match Self::register(offset, size) {
            Some(Self::COUNTER) => {
                self.counter += 1;
                (self.counter - 1) >> shift
            }
            Some(_) => self.sequence[vcpu] >> shift,
            None => (0..size).rev().fold(0, |v, i| v << 8 | u64::from(self.bytes[(offset + i) as usize])),
        } & mask(size);
        self.logged([vcpu as u64, offset, size, 0, value]);
        Some(value)
    }
    fn write(&mut self, vcpu: usize, offset: u64, size: u64, value: u64) -> bool {
        if offset + size > Self::REFUSE {
            return false;
        }
        self.writes[vcpu] += 1;
        self.logged([vcpu as u64, offset, size, 1, value]);
        match Self::register(offset, size) {
            Some(Self::SEQUENCE) => {
                if value != self.sequence[vcpu] & mask(size) {
                    self.order_errors += 1;
                }
                self.sequence[vcpu] += 1;
            }
            Some(_) => {}
            None => {
                for i in 0..size {
                    self.bytes[(offset + i) as usize] = (value >> (8 * i)) as u8;
                }
            }
        }
        true
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn registry_and_test_device() {
        let mut d = Devices::new(4 << 20);
        let t: Shared = Arc::new(Mutex::new(TestDevice::new()));
        assert!(d.register(0xfeb0_0000, 4096, t.clone()).is_ok());
        assert!(d.register(0xfeb0_0000, 4096, t.clone()).is_err()); // overlaps
        assert!(d.register(0x1000, 4096, t.clone()).is_err()); // guest RAM
        assert!(d.register(0x9_f000, 0x2000, t.clone()).is_err()); // RAM, then the hole
        assert!(d.register(0xb_8000, 0x8000, t.clone()).is_ok()); // inside the legacy hole (M1)
        assert!(d.register(0xfeb0_1800, 4096, t.clone()).is_err()); // unaligned
        assert_eq!(d.find(0xfeb0_0f00).map(|(_, o)| o), Some(0xf00));
        assert!(d.find(0xfeb0_1000).is_none());
        let mut t = TestDevice::new();
        assert_eq!([0, 1, 2].map(|_| t.read(1, TestDevice::COUNTER, 4)), [Some(0), Some(1), Some(2)]);
        assert!(t.write(2, TestDevice::SEQUENCE, 8, 0) && t.write(2, TestDevice::SEQUENCE, 8, 5));
        assert!(t.write(0, 0x10, 4, 0xdead_beef));
        assert_eq!((t.read(0, 0x11, 2), t.order_errors, t.reads, t.writes), (Some(0xadbe), 1, [1, 3, 0, 0], [1, 0, 2, 0]));
    }

    #[test]
    fn port_bus_and_unassigned_rule() {
        let mut d = Devices::new(4 << 20);
        let t = Arc::new(Mutex::new(TestDevice::new()));
        assert!(d.register_ports(0x100, 4, t.clone()).is_ok());
        assert!(d.register_ports(0x102, 4, t.clone()).is_err()); // overlaps
        assert!(d.register_ports(0xfffe, 4, t.clone()).is_err()); // past FFFFH
        assert_eq!(d.port(0, 0x100, 4, Some(0x4433_2211)), Ok(0));
        assert_eq!(d.port(0, 0x101, 2, None), Ok(0x3322)); // inside the range: one call
        assert_eq!(d.port(0, 0x102, 4, None), Ok(0xffff_4433)); // ends outside: byte by byte, the rest all-ones
        assert_eq!((d.port(0, 0x200, 1, None), d.port(0, 0x200, 2, None), d.port(0, 0x200, 4, None)), (Ok(0xff), Ok(0xffff), Ok(0xffff_ffff)));
        assert_eq!(d.port(0, 0x200, 4, Some(0)), Ok(0)); // ignored
        assert_eq!(d.port(0, 0xffff, 2, None), Ok(0xffff)); // wraps to port 0
        assert_eq!(t.lock().unwrap().reads[0], 3); // 0x101 once, 0x102 and 0x103 bytes
        assert!(d.report().contains("{\"port\":\"0x200\",\"reads\":3,\"writes\":1,\"unassigned\":true}"));
        d.strict = true;
        assert_eq!((d.port(0, 0x200, 1, None), d.port(0, 0x103, 2, Some(0))), (Err(mmio::UNASSIGNED), Err(mmio::UNASSIGNED)));
        assert_eq!(d.port(0, 0x100, 1, None), Ok(0x11));
    }
}
