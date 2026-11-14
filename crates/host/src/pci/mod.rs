//! Generic PCI layer adapted from cloud-hypervisor (see the header of each file), and natemu's own bus: configuration
//! mechanism #1 (PCI Local Bus 3.0 3.2.2.3.1), a host bridge at 00:00.0, and routing of the PCI memory window to
//! whichever function's BAR0 currently decodes an address. One bus, one function per device.
pub mod configuration;
pub mod msix;

use crate::devices::{Access, Device, Devices, Direct, mask};
use configuration::{PciBridgeSubclass, PciClassCode, PciConfiguration, PciHeaderType};
use std::sync::{Arc, Mutex};

pub mod platform {
    /// The 32-bit memory window the bus forwards, `[start, end)`: natemu's choice, and the window the ACPI root
    /// bridge's `_CRS` declares (`\_SB.PCI0`, ACPI 6.5 6.4.3.5). The registry is frozen at launch, so the bus
    /// registers all of it as one range and decodes BARs itself; a BAR outside it never decodes, as the root bridge
    /// forwards only what `_CRS` declares.
    pub const WINDOW: (u32, u32) = (0xE000_0000, 0xF000_0000);
    /// BAR0's launch-time address for the function in `slot`: the assignment a firmware would make (natemu boots
    /// without one), at natemu's choice of a 64 KiB stride; BAR format and sizing are PCI 3.0 6.2.5.1.
    pub const fn bar(slot: u8) -> u32 {
        WINDOW.0 + 0x1_0000 * slot as u32
    }
}

/// Device slots on the bus (PCI 3.0 3.2.2.3.1: the device number is 5 bits).
const SLOTS: usize = 32;
/// A deferred token's slot lives above this many bits of the function's own token.
const TOKEN_BITS: u32 = 56;

/// One function (function number 0 of its slot): its configuration space and its BAR0.
pub trait Function: Send + Sync {
    /// Configuration register `reg_idx` (offset / 4).
    fn config_read(&self, reg_idx: usize) -> u32;
    /// A write of 1, 2 or 4 bytes at byte `offset` of register `reg_idx`, as `PciConfiguration::write_config_register`.
    fn config_write(&self, reg_idx: usize, offset: u64, data: &[u8]);
    /// BAR0's current base, or None while memory decoding is off (PCI 3.0 6.2.2, Command register bit 1). Read
    /// lock-free: the function publishes it from its config writes.
    fn bar(&self) -> Option<u64>;
    /// BAR0's size in bytes.
    fn bar_len(&self) -> u64;
    /// An access inside BAR0; `offset` from its base. Tokens must be below 2^56.
    fn bar_access(&self, vcpu: usize, offset: u64, size: u64, value: Option<u64>) -> Access;
    /// Polls an access `bar_access` deferred, by the token it returned (as `Direct::poll`).
    fn bar_poll(&self, vcpu: usize, token: u64) -> Access;
    /// As `Device::quiesce`.
    fn quiesce(&self) -> crate::Result<bool>;
}

/// The host bridge, 00:00.0: QEMU `pc`'s i440FX (vendor 0x8086 Intel, device 0x1237 "440FX - 82441FX PMC"), class 0x06
/// (bridge), subclass 0x00 (host bridge) (PCI Code and ID Assignment spec, Base Class 06h), header type 0 (PCI 3.0
/// 6.2.1), no BARs, no capabilities.
struct HostBridge(Mutex<PciConfiguration>);
impl HostBridge {
    fn new() -> Self {
        HostBridge(Mutex::new(PciConfiguration::new(
            0x8086,
            0x1237,
            0,
            PciClassCode::BridgeDevice,
            &PciBridgeSubclass::HostBridge,
            None,
            PciHeaderType::Device,
            0,
            0,
            None,
        )))
    }
}
impl Function for HostBridge {
    fn config_read(&self, reg_idx: usize) -> u32 {
        let c = self.0.lock().unwrap_or_else(|e| e.into_inner());
        c.read_reg(reg_idx)
    }
    fn config_write(&self, reg_idx: usize, offset: u64, data: &[u8]) {
        // No BARs, so there is no reprogramming to act on.
        let mut c = self.0.lock().unwrap_or_else(|e| e.into_inner());
        c.write_config_register(reg_idx, offset, data);
    }
    fn bar(&self) -> Option<u64> {
        None
    }
    fn bar_len(&self) -> u64 {
        0
    }
    fn bar_access(&self, _: usize, _: u64, _: u64, _: Option<u64>) -> Access {
        Access::Refused
    }
    fn bar_poll(&self, _: usize, _: u64) -> Access {
        Access::Refused
    }
    fn quiesce(&self) -> crate::Result<bool> {
        Ok(true)
    }
}

type Slots = [Option<Arc<dyn Function>>; SLOTS];

/// The functions on bus 0, by slot (slot 0 is the host bridge).
pub struct Bus {
    slots: Slots,
}
impl Default for Bus {
    fn default() -> Self {
        Self::new()
    }
}
impl Bus {
    pub fn new() -> Self {
        let mut slots: Slots = std::array::from_fn(|_| None);
        slots[0] = Some(Arc::new(HostBridge::new()));
        Bus { slots }
    }
    pub fn add(&mut self, slot: u8, f: Arc<dyn Function>) -> crate::Result<()> {
        match self.slots.get_mut(usize::from(slot)) {
            Some(s @ None) if slot != 0 => *s = Some(f),
            _ => return Err(format!("PCI slot {slot}: 1..{SLOTS} and not in use")),
        }
        Ok(())
    }
    /// Registers the configuration ports (0xCF8..0xD00) and the whole memory window; both are fixed from here on.
    pub fn install(self, devices: &mut Devices) -> crate::Result<()> {
        let (ports, window) = self.into_parts();
        devices.register_ports(0xcf8, 8, Arc::new(Mutex::new(ports)))?;
        let size = u64::from(platform::WINDOW.1 - platform::WINDOW.0);
        devices.register(
            platform::WINDOW.0.into(),
            size,
            Arc::new(Mutex::new(window)),
        )
    }
    fn into_parts(self) -> (ConfigPorts, Window) {
        let slots = Arc::new(self.slots);
        (
            ConfigPorts {
                address: 0,
                slots: slots.clone(),
            },
            Window(slots),
        )
    }
}

/// Configuration mechanism #1 (PCI 3.0 3.2.2.3.2) on ports 0xCF8..0xD00: the address register at offsets 0-3, the
/// data register at 4-7. `Devices::port` delivers an access inside the range as one call with its offset; one that
/// runs past 0xCFF (e.g. 2 bytes at 0xCFF) it splits into bytes, the last of which falls on the unowned port 0xD00:
/// byte 3 of the data register is accessed alone and the rest reads as all-ones / is dropped.
struct ConfigPorts {
    address: u32,
    slots: Arc<Slots>,
}
impl ConfigPorts {
    /// The function the address register selects: enabled (bit 31), bus 0 (23:16), function 0 (10:8), slot present.
    fn selected(&self) -> Option<(&Arc<dyn Function>, usize)> {
        let a = self.address;
        (a >> 31 == 1 && (a >> 16) & 0xff == 0 && (a >> 8) & 7 == 0)
            .then(|| self.slots[((a >> 11) & 0x1f) as usize].as_ref())
            .flatten()
            .map(|f| (f, ((a >> 2) & 0x3f) as usize))
    }
}
impl Device for ConfigPorts {
    fn read(&mut self, _: usize, offset: u64, size: u64) -> Option<u64> {
        let ones = mask(size);
        Some(match offset {
            // The address register is read as a dword; narrower reads are not claimed (3.2.2.3.2).
            0 if size == 4 => self.address.into(),
            4..=7 if offset - 4 + size <= 4 => match self.selected() {
                Some((f, reg)) => u64::from(f.config_read(reg)) >> (8 * (offset - 4)) & ones,
                None => ones,
            },
            _ => ones,
        })
    }
    fn write(&mut self, _: usize, offset: u64, size: u64, value: u64) -> bool {
        match offset {
            // Only a dword write reaches the address register (3.2.2.3.2); narrower ones are ignored. It keeps
            // only the defined bits: enable, bus, device, function, register (bits 30:24 and 1:0 read 0). QEMU's
            // i440FX stores the raw value; this follows the specification.
            0 if size == 4 => self.address = value as u32 & 0x80ff_fffc,
            4..=7 if offset - 4 + size <= 4 => {
                if let Some((f, reg)) = self.selected() {
                    f.config_write(reg, offset - 4, &value.to_le_bytes()[..size as usize]);
                }
            }
            _ => {}
        }
        true
    }
}

/// The memory window as one registered range: each access goes to the function whose BAR0 decodes it. `Direct`, so
/// the registry never takes a mutex around a slow function; the slot list is immutable and `Function::bar` lock-free.
struct Window(Arc<Slots>);
impl Window {
    /// The function decoding `[addr, addr + size)`, lowest slot first if BARs overlap (a guest error, kept
    /// deterministic), with the offset in its BAR. A BAR not wholly inside the window does not decode.
    fn decode(&self, addr: u64, size: u64) -> Option<(&Arc<dyn Function>, u64, u64)> {
        let (lo, hi) = (u64::from(platform::WINDOW.0), u64::from(platform::WINDOW.1));
        self.0.iter().enumerate().find_map(|(slot, f)| {
            let f = f.as_ref()?;
            let (base, len) = (f.bar()?, f.bar_len());
            (base >= lo
                && base.checked_add(len)? <= hi
                && addr >= base
                && addr + size <= base + len)
                .then(|| (f, slot as u64, addr - base))
        })
    }
}
impl Direct for Window {
    fn access(&self, vcpu: usize, offset: u64, size: u64, value: Option<u64>) -> Access {
        match self.decode(u64::from(platform::WINDOW.0) + offset, size) {
            Some((f, slot, off)) => match f.bar_access(vcpu, off, size, value) {
                Access::Deferred(t) => {
                    assert!(
                        t >> TOKEN_BITS == 0,
                        "function token {t:#x} uses the slot bits"
                    );
                    Access::Deferred(slot << TOKEN_BITS | t)
                }
                other => other,
            },
            // As an unclaimed address on a PC: reads all-ones, writes are dropped.
            None => Access::Completed(if value.is_none() { mask(size) } else { 0 }),
        }
    }
    fn poll(&self, vcpu: usize, token: u64) -> Access {
        let slot = (token >> TOKEN_BITS) as usize;
        match self.0.get(slot).and_then(Option::as_ref) {
            Some(f) => match f.bar_poll(vcpu, token & ((1 << TOKEN_BITS) - 1)) {
                Access::Deferred(t) => Access::Deferred((slot as u64) << TOKEN_BITS | t),
                other => other,
            },
            None => Access::Refused,
        }
    }
}
impl Device for Window {
    fn read(&mut self, _: usize, _: u64, _: u64) -> Option<u64> {
        None
    }
    fn write(&mut self, _: usize, _: u64, _: u64, _: u64) -> bool {
        false
    }
    fn access(&mut self, vcpu: usize, offset: u64, size: u64, value: Option<u64>) -> Access {
        Direct::access(self, vcpu, offset, size, value)
    }
    fn poll(&mut self, vcpu: usize, token: u64) -> Access {
        Direct::poll(self, vcpu, token)
    }
    /// Every function, even after one is not done (`Devices::quiesce` reaches only registered devices).
    fn quiesce(&mut self) -> crate::Result<bool> {
        let results: Vec<_> = self.0.iter().flatten().map(|f| f.quiesce()).collect();
        results.into_iter().try_fold(true, |done, r| Ok(done & r?))
    }
    fn direct(&self) -> Option<Arc<dyn Direct>> {
        Some(Arc::new(Window(self.0.clone())))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::devices::{Device, Direct};
    use std::sync::atomic::{AtomicBool, AtomicU64, Ordering::SeqCst};

    /// A function with a movable BAR0 that records its accesses (writes are stored, reads return the id).
    struct Stub {
        id: u64,
        base: AtomicU64,
        on: AtomicBool,
        len: u64,
        quiesced: AtomicBool,
        done: bool,
        last_write: AtomicU64,
    }
    const OFF: u64 = u64::MAX;
    impl Stub {
        fn new(id: u64, base: u64) -> Arc<Self> {
            Arc::new(Stub {
                id,
                base: AtomicU64::new(base),
                on: AtomicBool::new(true),
                len: 0x8000,
                quiesced: AtomicBool::new(false),
                done: true,
                last_write: AtomicU64::new(OFF),
            })
        }
    }
    impl Function for Stub {
        fn config_read(&self, reg_idx: usize) -> u32 {
            reg_idx as u32
        }
        fn config_write(&self, _: usize, _: u64, _: &[u8]) {}
        fn bar(&self) -> Option<u64> {
            self.on.load(SeqCst).then(|| self.base.load(SeqCst))
        }
        fn bar_len(&self) -> u64 {
            self.len
        }
        fn bar_access(&self, _: usize, offset: u64, _: u64, value: Option<u64>) -> Access {
            match value {
                Some(v) => {
                    self.last_write.store(offset << 8 | v, SeqCst);
                    Access::Completed(0)
                }
                None => Access::Completed(self.id << 32 | offset),
            }
        }
        fn bar_poll(&self, _: usize, token: u64) -> Access {
            Access::Deferred(token + 1)
        }
        fn quiesce(&self) -> crate::Result<bool> {
            self.quiesced.store(true, SeqCst);
            Ok(self.done)
        }
    }

    fn ports() -> Devices {
        let mut d = Devices::new(1 << 30);
        Bus::new().install(&mut d).unwrap();
        d
    }
    fn win(fs: &[(u8, &Arc<Stub>)]) -> Window {
        let mut b = Bus::new();
        for (s, f) in fs {
            b.add(*s, (*f).clone()).unwrap();
        }
        b.into_parts().1
    }
    const BASE: u64 = platform::WINDOW.0 as u64;
    fn bar(slot: u8) -> u64 {
        platform::bar(slot).into()
    }

    #[test]
    fn type1_probe_as_linux_does_it() {
        let d = ports();
        assert_eq!(d.port(0, 0xcfb, 1, Some(0x80)), Ok(0)); // the probe's first, byte-wide write
        d.port(0, 0xcf8, 4, Some(0x8000_0000)).unwrap();
        assert_eq!(d.port(0, 0xcf8, 4, None), Ok(0x8000_0000));
        // pci_sanity_check: class 0x0600 at 00:00.0 (register 2, bits 31:16)
        d.port(0, 0xcf8, 4, Some(0x8000_0008)).unwrap();
        assert_eq!(d.port(0, 0xcfe, 2, None), Ok(0x0600));
        d.port(0, 0xcf8, 4, Some(0x8000_0000)).unwrap();
        assert_eq!(d.port(0, 0xcfc, 4, None), Ok(0x1237_8086));
        d.port(0, 0xcf8, 4, Some(0x8000_000c)).unwrap();
        assert_eq!(d.port(0, 0xcfe, 1, None), Ok(0)); // header type 0
    }
    #[test]
    fn absent_function_reads_all_ones_and_ignores_writes() {
        let d = ports();
        d.port(0, 0xcf8, 4, Some(0x8000_0000 | 5 << 11)).unwrap();
        assert_eq!(d.port(0, 0xcfc, 4, None), Ok(0xffff_ffff));
        assert_eq!(d.port(0, 0xcfd, 1, None), Ok(0xff));
        assert_eq!(d.port(0, 0xcfc, 2, None), Ok(0xffff));
        d.port(0, 0xcfc, 4, Some(0x1234)).unwrap();
        d.port(0, 0xcf8, 4, Some(0x8000_0000)).unwrap();
        assert_eq!(d.port(0, 0xcfc, 4, None), Ok(0x1237_8086));
    }
    #[test]
    fn byte_and_word_data_accesses_address_within_the_register() {
        let d = ports();
        d.port(0, 0xcf8, 4, Some(0x8000_0000)).unwrap();
        assert_eq!(d.port(0, 0xcfd, 1, None), Ok(0x80));
        assert_eq!(d.port(0, 0xcfe, 2, None), Ok(0x1237));
        assert_eq!(d.port(0, 0xcfd, 2, None), Ok(0x3780)); // 2 bytes at byte 1
        // A 2-byte access at 0xCFF crosses the end of the range: Devices::port splits it into byte 3 of the data
        // register and the unowned port 0xD00 (all-ones).
        assert_eq!(d.port(0, 0xcff, 2, None), Ok(0xff12));
    }
    #[test]
    fn address_without_enable_bit_reads_ones() {
        let d = ports();
        d.port(0, 0xcf8, 4, Some(0)).unwrap();
        assert_eq!(d.port(0, 0xcfc, 4, None), Ok(0xffff_ffff));
        assert_eq!(d.port(0, 0xcfc, 1, None), Ok(0xff));
    }
    #[test]
    fn byte_write_to_cf9_does_not_change_the_address() {
        let d = ports();
        d.port(0, 0xcf8, 4, Some(0x8000_0008)).unwrap();
        d.port(0, 0xcf9, 1, Some(0xff)).unwrap();
        d.port(0, 0xcf8, 2, Some(0)).unwrap();
        assert_eq!(d.port(0, 0xcf8, 4, None), Ok(0x8000_0008));
    }
    #[test]
    fn nonzero_bus_or_function_is_absent() {
        let d = ports();
        for addr in [0x8001_0000u64, 0x8000_0100] {
            d.port(0, 0xcf8, 4, Some(addr)).unwrap();
            assert_eq!(d.port(0, 0xcfc, 4, None), Ok(0xffff_ffff));
        }
    }
    #[test]
    fn only_a_dword_write_changes_the_address_and_cf8_reads_as_unclaimed_below_dword() {
        let d = ports();
        d.port(0, 0xcf8, 4, Some(0x8000_0800)).unwrap();
        assert_eq!(d.port(0, 0xcf8, 1, None), Ok(0xff));
        assert_eq!(d.port(0, 0xcf9, 1, None), Ok(0xff));
        assert_eq!(d.port(0, 0xcfa, 2, None), Ok(0xffff));
        d.port(0, 0xcfb, 1, Some(0)).unwrap();
        d.port(0, 0xcfa, 2, Some(0)).unwrap();
        assert_eq!(d.port(0, 0xcf8, 4, None), Ok(0x8000_0800));
        d.port(0, 0xcf8, 4, Some(0xffff_ffff)).unwrap();
        assert_eq!(d.port(0, 0xcf8, 4, None), Ok(0x80ff_fffc));
    }
    #[test]
    fn installed_window_serves_accesses_through_the_registry() {
        let a = Stub::new(1, bar(1));
        let mut b = Bus::new();
        b.add(1, a.clone()).unwrap();
        let mut d = Devices::new(1 << 30);
        b.install(&mut d).unwrap();
        // the registry's own lookup, then the handle it would call without the mutex
        let (dev, off) = d.find(bar(1) + 0x10).unwrap();
        assert_eq!(off, 0x1_0010);
        let direct = dev.lock().unwrap().direct().unwrap();
        assert_eq!(
            direct.access(0, off, 4, None),
            Access::Completed(1 << 32 | 0x10)
        );
        assert_eq!(direct.access(0, off, 4, Some(9)), Access::Completed(0));
        assert_eq!(a.last_write.load(SeqCst), 0x10 << 8 | 9);
    }

    #[test]
    fn window_routes_to_the_bar_that_decodes_the_address() {
        let (a, b) = (Stub::new(1, bar(1)), Stub::new(2, bar(2)));
        let w = win(&[(1, &a), (2, &b)]);
        assert_eq!(
            w.access(0, 0x1_0010, 4, None),
            Access::Completed(1 << 32 | 0x10)
        );
        assert_eq!(
            w.access(0, 0x2_7ff8, 8, None),
            Access::Completed(2 << 32 | 0x7ff8)
        );
        w.access(0, 0x2_0004, 4, Some(7));
        assert_eq!(b.last_write.load(SeqCst), 4 << 8 | 7);
        assert_eq!(a.last_write.load(SeqCst), OFF);
    }
    #[test]
    fn a_moved_bar_decodes_at_its_new_address_only() {
        let a = Stub::new(1, bar(1));
        let w = win(&[(1, &a)]);
        a.base.store(BASE + 0x8_0000, SeqCst);
        assert_eq!(
            w.access(0, 0x1_0000, 4, None),
            Access::Completed(0xffff_ffff)
        );
        assert_eq!(
            w.access(0, 0x8_0008, 4, None),
            Access::Completed(1 << 32 | 8)
        );
    }
    #[test]
    fn memory_decoding_off_reads_all_ones_and_ignores_writes() {
        let a = Stub::new(1, bar(1));
        let w = win(&[(1, &a)]);
        a.on.store(false, SeqCst);
        assert_eq!(w.access(0, 0x1_0000, 2, None), Access::Completed(0xffff));
        assert_eq!(w.access(0, 0x1_0000, 4, Some(5)), Access::Completed(0));
        assert_eq!(a.last_write.load(SeqCst), OFF);
    }
    #[test]
    fn window_addresses_no_bar_claims_read_all_ones() {
        let a = Stub::new(1, bar(1));
        let w = win(&[(1, &a)]);
        for (off, size, want) in [
            (0x1_8000, 4, 0xffff_ffff),
            (0x0, 1, 0xff),
            (0xfff_fff8, 8, u64::MAX),
        ] {
            assert_eq!(w.access(0, off, size, None), Access::Completed(want));
        }
        assert_eq!(w.access(0, 0x1_8000, 4, Some(1)), Access::Completed(0));
        assert_eq!(a.last_write.load(SeqCst), OFF);
    }
    #[test]
    fn overlapping_bars_route_to_the_lowest_slot() {
        let (a, b) = (Stub::new(1, BASE + 0x4_0000), Stub::new(2, BASE + 0x4_0000));
        let w = win(&[(5, &b), (3, &a)]);
        assert_eq!(
            w.access(0, 0x4_0008, 4, None),
            Access::Completed(1 << 32 | 8)
        );
    }
    #[test]
    fn a_bar_outside_the_window_does_not_decode() {
        let a = Stub::new(1, 0xf000_0000);
        let w = win(&[(1, &a)]);
        assert_eq!(
            w.access(0, 0x0fff_fff8, 4, None),
            Access::Completed(0xffff_ffff)
        );
        // straddling the end of the window: the part inside must not decode either
        a.base.store(0xf000_0000 - 0x4000, SeqCst);
        assert_eq!(
            w.access(0, 0x0fff_c000, 4, None),
            Access::Completed(0xffff_ffff)
        );
    }
    #[test]
    fn deferred_tokens_carry_the_slot() {
        let a = Stub::new(1, bar(4));
        let w = win(&[(4, &a)]);
        assert_eq!(w.poll(0, 4 << 56 | 9), Access::Deferred(4 << 56 | 10));
        assert_eq!(w.poll(0, 7 << 56), Access::Refused);
    }
    #[test]
    fn bus_add_refuses_slot_zero_occupied_and_out_of_range() {
        let mut b = Bus::new();
        let a: Arc<dyn Function> = Stub::new(1, BASE);
        assert!(b.add(0, a.clone()).is_err());
        assert!(b.add(32, a.clone()).is_err());
        assert!(b.add(31, a.clone()).is_ok());
        assert!(b.add(31, a).is_err());
    }
    #[test]
    fn quiesce_reaches_every_function_and_reports_done_only_when_all_are() {
        let (a, mut b) = (Stub::new(1, BASE), Stub::new(2, BASE));
        Arc::get_mut(&mut b).unwrap().done = false;
        let c = Stub::new(3, BASE);
        let mut w = win(&[(1, &a), (2, &b), (3, &c)]);
        assert_eq!(Device::quiesce(&mut w), Ok(false));
        assert!([&a, &b, &c].iter().all(|f| f.quiesced.load(SeqCst)));
        let d = Stub::new(4, BASE);
        assert_eq!(Device::quiesce(&mut win(&[(1, &d)])), Ok(true));
    }
    #[test]
    fn install_registers_the_whole_window_with_the_registry() {
        let d = ports();
        assert!(d.find(BASE).is_some() && d.find(BASE + 0xfff_f000).is_some());
        assert!(d.find(BASE - 0x1000).is_none() && d.find(platform::WINDOW.1.into()).is_none());
    }
}
