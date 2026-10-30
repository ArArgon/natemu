//! M3, devices on guest-physical ranges: the host side of translated code's device accesses
//! (`natemu_abi::execution::Mmio`).
//!
//! **Registry.** [`Devices`] maps page-aligned guest-physical ranges outside guest RAM to [`Device`]s (a device may own
//! several ranges). The resident never shadows such a page: an access to it aborts at its site, the resident walks it
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
use std::sync::{Arc, Mutex};

/// A device's register file: `offset` is from the start of the range it was registered at; `size` is 1, 2, 4 or 8 and
/// the access never crosses a page. `vcpu` is the accessing vCPU. A read returns the value in its low `size` bytes. `None`
/// (read) or `false` (write): the device refuses the access, unchanged.
pub(crate) trait Device: Send {
    fn read(&mut self, vcpu: usize, offset: u64, size: u64) -> Option<u64>;
    fn write(&mut self, vcpu: usize, offset: u64, size: u64, value: u64) -> bool;
}
pub(crate) type Shared = Arc<Mutex<dyn Device>>;

/// The guest-physical device ranges of a VM.
pub(crate) struct Devices {
    ranges: Vec<(u64, u64, Shared)>,
    /// The end of the VM's guest RAM (M1: its run-time size): no device below it.
    ram_end: u64,
}
impl Devices {
    pub(crate) fn new(ram_end: u64) -> Self {
        Self { ranges: Vec::new(), ram_end }
    }
    /// Registers `device` at `[base, base + bytes)`: page-aligned, above guest RAM, below MAXPHYADDR (40 bits), not
    /// overlapping another range.
    pub(crate) fn register(&mut self, base: u64, bytes: u64, device: Shared) -> crate::Result<()> {
        let end = base.checked_add(bytes).filter(|&e| bytes > 0 && e <= 1 << 40);
        let Some(end) = end.filter(|_| base % 4096 == 0 && bytes % 4096 == 0 && base >= self.ram_end) else {
            return Err(format!("device range {base:#x}+{bytes:#x}: page-aligned, above guest RAM, below 2^40"));
        };
        if self.ranges.iter().any(|&(b, e, _)| base < e && b < end) {
            return Err(format!("device range {base:#x}+{bytes:#x} overlaps another"));
        }
        self.ranges.push((base, end, device));
        Ok(())
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
    let well_formed = matches!(m.width, 1 | 2 | 4 | 8) && (m.gpa & 4095) + m.width <= 4096 && m.write <= 1;
    m.status = match devices.and_then(|d| d.find(m.gpa)) {
        _ if !well_formed => 0,
        None => mmio::UNASSIGNED,
        Some((device, offset)) => {
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
        assert!(d.register(0xfeb0_1800, 4096, t.clone()).is_err()); // unaligned
        assert_eq!(d.find(0xfeb0_0f00).map(|(_, o)| o), Some(0xf00));
        assert!(d.find(0xfeb0_1000).is_none());
        let mut t = TestDevice::new();
        assert_eq!([0, 1, 2].map(|_| t.read(1, TestDevice::COUNTER, 4)), [Some(0), Some(1), Some(2)]);
        assert!(t.write(2, TestDevice::SEQUENCE, 8, 0) && t.write(2, TestDevice::SEQUENCE, 8, 5));
        assert!(t.write(0, 0x10, 4, 0xdead_beef));
        assert_eq!((t.read(0, 0x11, 2), t.order_errors, t.reads, t.writes), (Some(0xadbe), 1, [1, 3, 0, 0], [1, 0, 2, 0]));
    }
}
