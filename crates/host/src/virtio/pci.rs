//! The virtio-PCI transport (virtio 1.2 4.1): one PCI function per device, its configuration space (PCI 3.0 6.1) with
//! an MSI-X capability (PCI 3.0 6.8.2) and the four virtio vendor capabilities (virtio 1.2 4.1.4), and BAR0 holding
//! the common configuration, ISR, device configuration, notification area, MSI-X table and PBA.
//!
//! Common-configuration fields with an MMIO equivalent go to that MMIO register through [`Virtio::locked_access`], so
//! resets, deferral and ordering are the MMIO front end's; the rest are private registers (`reg::PCI`-tagged
//! offsets, [`Virtio::pci_register`]) on the same path.
//!
//! Interrupts. The ISR is `State::interrupt` (bit 0 used buffer, bit 1 configuration change), published in
//! [`Transport::isr`]'s atomic under the state lock. With MSI-X enabled a used-buffer interrupt sends the queue's
//! vector and a configuration change the configuration vector ([`Transport::trigger`]); with MSI-X disabled the
//! device's GSI is an INTx line asserted while the ISR is nonzero and the command register's Interrupt Disable is
//! clear (virtio 1.2 4.1.4.5; PCI 3.0 6.2.2, 6.8.3.3).
//!
//! Lock order: virtio state, then this configuration space, then MSI-X, then the chipset. The line's level is decided
//! and driven only under the configuration-space lock, from the ISR atomic and the configuration space: a state-path
//! change stores the ISR, then takes the configuration lock and drives the line; a configuration write changes the
//! command register or MSI-X enable and drives the line before releasing that lock. Whichever drives last does so
//! after both the latest ISR store (it happened before its writer took the lock, or the driver is that writer) and
//! the latest configuration write (made under the lock), so the line ends at the level of the latest state. Two
//! state-path changes are ordered by the state lock, which their writers hold while they drive. MSI-X delivery takes
//! only the MSI-X lock (then the chipset's): bus mastering reaches it as an atomic gate the configuration write stores.
//!
//! Notes. Under MSI-X a driver never reads the ISR, so its bit 0 stays set; disabling MSI-X without a device reset
//! therefore asserts INTx at once from that stale bit (the specification's literal reading: the line is the ISR's
//! OR; QEMU instead waits for the next interrupt), and `profile::raised` counts are skewed under PCI. Reads of the
//! MSI-X table or PBA region past their entries return all-ones (`MsixConfig`'s behaviour), an exception to the
//! rule that an unimplemented BAR0 offset reads zero.
use super::{Access, Profile, State, Virtio, feature_word, reg};
use crate::{
    irq::Irqs,
    pci::{
        self as bus,
        configuration::{
            PciBarConfiguration, PciBarPrefetchable, PciBarRegionType, PciCapability,
            PciCapabilityId, PciClassCode, PciConfiguration, PciHeaderType, PciSubclass,
        },
        msix::{Deliver, MsixCap, MsixConfig},
    },
};
use std::sync::{
    Arc, Mutex, MutexGuard,
    atomic::{AtomicBool, AtomicU32, AtomicU64, Ordering::SeqCst},
};

/// Red Hat's virtio vendor ID (virtio 1.2 4.1.2).
const VENDOR: u16 = 0x1af4;
/// Modern device IDs are 0x1040 + the virtio device ID (virtio 1.2 4.1.2.1).
const DEVICE_BASE: u16 = 0x1040;
/// Revision 1: a modern (non-transitional) device (virtio 1.2 4.1.2.1).
const REVISION: u8 = 1;
/// BAR0's size and layout, each structure at natemu's choice of a 4 KiB-aligned offset (virtio 1.2 4.1.4; PCI 3.0
/// 6.8.2 for the table and PBA).
const BAR_LEN: u64 = 0x8000;
const COMMON: u64 = 0x0000;
const ISR: u64 = 0x1000;
const DEVICE: u64 = 0x2000;
const NOTIFY: u64 = 0x3000;
const TABLE: u64 = 0x4000;
const PBA: u64 = 0x5000;
/// Each structure's `length` in its capability.
const STRUCTURE: u64 = 0x1000;
/// Queue q's notification address is NOTIFY + q * this (`notify_off_multiplier`, `queue_notify_off` = q; virtio 1.2
/// 4.1.4.4).
const MULTIPLIER: u64 = 4;
/// virtio_pci_cap `cfg_type`s (virtio 1.2 4.1.4).
const COMMON_CFG: u8 = 1;
const NOTIFY_CFG: u8 = 2;
const ISR_CFG: u8 = 3;
const DEVICE_CFG: u8 = 4;
/// VIRTIO_MSI_NO_VECTOR (virtio 1.2 4.1.5.1.2).
pub(super) const NO_VECTOR: u16 = 0xffff;
/// Command register bits (PCI 3.0 6.2.2): Memory Space, Bus Master, Interrupt Disable.
const MEMORY: u16 = 1 << 1;
const BUS_MASTER: u16 = 1 << 2;
const INTX_DISABLE: u16 = 1 << 10;
/// Status register bit 3, Interrupt Status (PCI 3.0 6.2.3), in configuration register 1.
const INTERRUPT_STATUS: u32 = 1 << 19;
/// Interrupt Pin 1 = INTA# (PCI 3.0 6.2.4), in bits 15:8 of configuration register 15.
const INTA: u32 = 1 << 8;
/// [`Transport::bar`] while memory decoding is off.
const NO_BAR: u64 = u64::MAX;

fn lock<T>(m: &Mutex<T>) -> MutexGuard<'_, T> {
    m.lock().unwrap_or_else(|e| e.into_inner())
}

/// The MSI-X vector of each queue (0, 1) and of configuration changes (2); a device reset returns them to
/// [`NO_VECTOR`] (virtio 1.2 4.1.5.1.2).
#[derive(Clone, Copy)]
pub(super) struct Vectors(pub(super) [u16; 3]);
impl Default for Vectors {
    fn default() -> Self {
        Self([NO_VECTOR; 3])
    }
}
const CONFIG_VECTOR: usize = 2;

/// A class subcode (`PciSubclass` needs a type; the values are QEMU's, as the spec's device table says).
struct Subclass(u8);
impl PciSubclass for Subclass {
    fn get_register_value(&self) -> u8 {
        self.0
    }
}

/// A virtio vendor capability, `struct virtio_pci_cap` (virtio 1.2 4.1.4), with `notify_off_multiplier` appended for
/// the notification capability (4.1.4.4); the two header bytes (ID, next) are `add_capability`'s.
struct VendorCap(Vec<u8>);
impl VendorCap {
    fn new(cfg_type: u8, offset: u64, extra: &[u8]) -> Self {
        // cap_len counts the whole capability, its two header bytes included.
        let mut b = vec![(16 + extra.len()) as u8, cfg_type, 0, 0, 0, 0];
        b.extend((offset as u32).to_le_bytes());
        b.extend((STRUCTURE as u32).to_le_bytes());
        b.extend(extra);
        Self(b)
    }
}
impl PciCapability for VendorCap {
    fn bytes(&self) -> &[u8] {
        &self.0
    }
    fn id(&self) -> PciCapabilityId {
        PciCapabilityId::VendorSpecific
    }
}

/// The PCI side of a device, set once by [`Virtio::pci`].
pub(super) struct Transport {
    config: Mutex<PciConfiguration>,
    msix: Arc<Mutex<MsixConfig>>,
    /// The ISR (`State::interrupt`), stored under the state lock: read by configuration accesses, which never take it.
    isr: AtomicU32,
    /// BAR0's base while memory decoding is on, else [`NO_BAR`]; stored under the configuration lock.
    bar: AtomicU64,
    /// The command register's Bus Master bit, stored under the configuration lock; MSI-X's gate (`set_gate`): a
    /// message is sent or released only while it is set, else held pending, without the configuration lock on the
    /// delivery path. The device itself keeps processing requests (QEMU blocks DMA instead).
    bus_master: Arc<AtomicBool>,
    irqs: Irqs,
    gsi: usize,
}
impl Transport {
    /// Under the state lock: the ISR is now `isr`; drive the INTx line if it changed.
    pub(super) fn isr(&self, isr: u32) {
        // Unchanged, the line is already at its level: the last drive followed the last change of either input.
        if self.isr.swap(isr, SeqCst) != isr {
            self.drive(&lock(&self.config));
        }
    }
    /// Under the configuration lock: INTx asserted while the ISR is nonzero, Interrupt Disable is clear and MSI-X is
    /// disabled (virtio 1.2 4.1.4.5; PCI 3.0 6.2.2, 6.8.3.3). The chipset passes only changes on.
    fn drive(&self, config: &PciConfiguration) {
        let level = self.isr.load(SeqCst) != 0
            && config.command() & INTX_DISABLE == 0
            && !lock(&self.msix).enabled();
        self.irqs.line(self.gsi, level);
    }
    /// Under the state lock: interrupts for the queues in `queues` (bit n: queue n) and, with `config`, a
    /// configuration change. MSI-X sends each distinct vector once, or marks it pending while masked or without bus
    /// mastering (PCI 3.0 6.2.2: a function then issues no memory write, and an MSI is one; sent once Bus Master is
    /// set); nothing for [`NO_VECTOR`] or with MSI-X disabled (`MsixConfig::trigger`). Takes only the MSI-X lock.
    pub(super) fn trigger(&self, vectors: &Vectors, queues: u8, config: bool) {
        let mut msix = lock(&self.msix);
        let mut sent = [NO_VECTOR; 3];
        let wanted = (0..2)
            .filter(|q| queues >> q & 1 != 0)
            .chain(config.then_some(CONFIG_VECTOR));
        for (i, v) in wanted.map(|i| vectors.0[i]).enumerate() {
            if !sent.contains(&v) {
                msix.trigger(v);
            }
            sent[i] = v;
        }
    }
    /// Under the state lock, at a device reset: pending vectors belong to the closed incarnation (QEMU:
    /// `msix_unuse_all_vectors`); the table and MSI-X Enable are PCI state and stay.
    pub(super) fn clear_pending(&self) {
        lock(&self.msix).clear_pending();
    }
}

/// The function the bus sees.
pub(super) struct PciFunction(Arc<Virtio>);
impl PciFunction {
    fn transport(&self) -> &Transport {
        self.0.pci.get().expect("set by Virtio::pci")
    }
}

/// A private register: `offset` in BAR0, accessed `size` bytes wide.
fn private(offset: u64, size: u64) -> u64 {
    reg::PCI | size << 16 | offset
}

/// Where a common-configuration access at `offset`, `size` bytes wide, goes (virtio 1.2 4.1.4.3): the MMIO register
/// with the same effect, a private register ([`Virtio::pci_register`]), or None when no field is there at that width.
/// The 64-bit queue addresses take eight bytes or either four-byte half (4.1.3.1).
fn common(offset: u64, size: u64, value: Option<u64>) -> Option<u64> {
    use reg::*;
    let write = value.is_some();
    Some(match (offset, size) {
        (0x00, 4) if write => DEVICE_FEATURES_SEL,
        (0x04, 4) if !write => DEVICE_FEATURES,
        (0x08, 4) if write => DRIVER_FEATURES_SEL,
        (0x0c, 4) if write => DRIVER_FEATURES,
        // device_status, one byte; 0 is the device reset.
        (0x14, 1) => STATUS,
        (0x16, 2) if write => QUEUE_SEL,
        (0x18, 2) if write => QUEUE_NUM,
        // queue_enable reads as QueueReady; a write of 0, which a driver must not make, is the MMIO queue reset.
        (0x1c, 2) if !write || value == Some(0) => QUEUE_READY,
        (0x20, 4) if write => QUEUE_DESC_LOW,
        (0x24, 4) if write => QUEUE_DESC_HIGH,
        (0x28, 4) if write => QUEUE_DRIVER_LOW,
        (0x2c, 4) if write => QUEUE_DRIVER_HIGH,
        (0x30, 4) if write => QUEUE_DEVICE_LOW,
        (0x34, 4) if write => QUEUE_DEVICE_HIGH,
        (0x00 | 0x08 | 0x0c | 0x20 | 0x24 | 0x28 | 0x2c | 0x30 | 0x34, 4)
        | (0x10 | 0x12 | 0x16 | 0x18 | 0x1a | 0x1c | 0x1e, 2)
        | (0x15, 1)
        | (0x20 | 0x28 | 0x30, 8) => private(offset, size),
        _ => return None,
    })
}

impl Virtio {
    /// Make this device a virtio-PCI function for `slot` (for `pci::Bus::add`): BAR0 at `pci::platform::bar(slot)`
    /// with memory decoding and bus mastering off, its interrupts on MSI-X or, until the driver enables MSI-X, on its
    /// GSI as INTx. Once per device; the MMIO front end must not be registered for it.
    pub fn pci(self: &Arc<Self>, slot: u8) -> Arc<dyn bus::Function> {
        let irqs = self.irqs.clone();
        self.pci_with(
            slot,
            Arc::new(move |address, data| {
                irqs.msi(address, data);
            }),
        )
    }
    /// [`Self::pci`], sending MSI-X messages through `deliver`.
    pub(super) fn pci_with(self: &Arc<Self>, slot: u8, deliver: Deliver) -> Arc<PciFunction> {
        let id = self.profile.device_id() as u16;
        // One vector per queue and one for configuration changes.
        let vectors = self.profile.queue_count() as u16 + 1;
        let bus_master = Arc::new(AtomicBool::new(false));
        let mut table = MsixConfig::new(vectors, deliver, 0).expect("MSI-X table");
        table.set_gate(bus_master.clone());
        let msix = Arc::new(Mutex::new(table));
        // QEMU's class codes (the spec's device table): mass storage / SCSI, network / Ethernet, and for entropy
        // class 0x00 subclass 0xff.
        let (class, subclass) = match self.profile {
            Profile::Block(_) => (PciClassCode::MassStorage, 0),
            Profile::Network(_) => (PciClassCode::NetworkController, 0),
            Profile::Entropy => (PciClassCode::TooOld, 0xff),
        };
        let mut config = PciConfiguration::new(
            VENDOR,
            DEVICE_BASE + id,
            REVISION,
            class,
            &Subclass(subclass),
            None,
            PciHeaderType::Device,
            VENDOR,
            id, // subsystem ID = virtio device ID (virtio 1.2 4.1.2.1)
            Some(msix.clone()),
        );
        // BAR0: 32-bit, non-prefetchable memory (PCI 3.0 6.2.5.1), where a firmware would put it.
        let bar = PciBarConfiguration::new(
            0,
            BAR_LEN,
            PciBarRegionType::Memory32BitRegion,
            PciBarPrefetchable::NotPrefetchable,
        )
        .set_address(bus::platform::bar(slot).into());
        config.add_pci_bar(&bar).expect("BAR0");
        // Capabilities (PCI 3.0 6.7; add_capability sets the status register's Capabilities List bit): MSI-X with
        // table and PBA in BAR0 (PCI 3.0 6.8.2), then the virtio structures (virtio 1.2 4.1.4).
        let cap = MsixCap::new(0, vectors, TABLE as u32, 0, PBA as u32);
        config.add_capability(&cap).expect("MSI-X capability");
        let multiplier = (MULTIPLIER as u32).to_le_bytes();
        for (cfg_type, offset, extra) in [
            (COMMON_CFG, COMMON, &[][..]),
            (NOTIFY_CFG, NOTIFY, &multiplier[..]),
            (ISR_CFG, ISR, &[][..]),
            (DEVICE_CFG, DEVICE, &[][..]),
        ] {
            let cap = VendorCap::new(cfg_type, offset, extra);
            config.add_capability(&cap).expect("virtio capability");
        }
        // Interrupt Line: the GSI, as a firmware routes INTA# (PCI 3.0 6.2.4; the pin is added in `config_read`).
        config.write_config_register(15, 0, &[self.gsi as u8]);
        let transport = Transport {
            config: Mutex::new(config),
            msix,
            isr: AtomicU32::new(0),
            bar: AtomicU64::new(NO_BAR),
            bus_master,
            irqs: self.irqs.clone(),
            gsi: self.gsi,
        };
        assert!(
            self.pci.set(transport).is_ok(),
            "a device is one PCI function"
        );
        // From here the ISR drives INTx: start from the state's.
        self.irq(&self.lock());
        Arc::new(PciFunction(self.clone()))
    }
    /// An MSI-X table entry number, or NO_VECTOR for one the table lacks (virtio 1.2 4.1.5.1.2: the read-back is
    /// NO_VECTOR when the device cannot map it).
    fn vector(&self, value: u64) -> u16 {
        if value <= self.profile.queue_count() as u64 {
            value as u16
        } else {
            NO_VECTOR
        }
    }
    /// A private PCI register (`reg::PCI | size << 16 | offset`, offset in BAR0), under the state lock.
    pub(super) fn pci_register(&self, s: &mut State, register: u64, value: Option<u64>) -> Access {
        let (offset, size) = (register & 0xffff, register >> 16 & 0xff);
        let q = s.queue_sel as usize;
        let valid = q < self.profile.queue_count();
        let Some(value) = value else {
            let read = match offset {
                // ISR status: reading clears it and lowers INTx (virtio 1.2 4.1.4.5).
                ISR => {
                    let isr = std::mem::take(&mut s.interrupt);
                    self.irq(s);
                    isr.into()
                }
                // Read-write fields read back what was written (virtio 1.2 4.1.4.3); the MMIO registers they map
                // to are write-only, so these come from state.
                0x00 => s.device_sel.into(),
                0x08 => s.driver_sel.into(),
                0x0c => feature_word(s.features, s.driver_sel).into(),
                0x10 => s.vectors.0[CONFIG_VECTOR].into(),
                0x12 => self.profile.queue_count() as u64,
                0x16 => s.queue_sel.into(),
                // The queue's size; before one is written, the maximum (4.1.4.3.2: the driver reads it there).
                0x18 if valid => match s.queues[q].size {
                    0 => self.profile.queue_max().into(),
                    size => size.into(),
                },
                0x1a if valid => s.vectors.0[q].into(),
                0x1a => NO_VECTOR.into(),
                0x1e if valid => q as u64,
                0x20..0x38 if valid => {
                    let queue = &s.queues[q];
                    let field = match offset & !7 {
                        0x20 => queue.desc,
                        0x28 => queue.avail,
                        _ => queue.used,
                    };
                    match size {
                        8 => field,
                        _ => field >> ((offset & 4) * 8) & 0xffff_ffff,
                    }
                }
                // Including config_generation: configuration never changes.
                _ => 0,
            };
            return Access::Completed(read);
        };
        // Power-off owns lifecycle ordering, as for MMIO writes.
        if s.quiescing {
            return Access::Completed(0);
        }
        match offset {
            0x10 => s.vectors.0[CONFIG_VECTOR] = self.vector(value),
            0x1a if valid => s.vectors.0[q] = self.vector(value),
            // queue_enable = 1 (0 went to the MMIO queue reset): with no size written, the maximum (4.1.4.3.2).
            0x1c if valid => {
                if s.queues[q].size == 0 && !s.queues[q].ready && !s.queue_active(q) {
                    s.queues[q].size = self.profile.queue_max();
                }
                return self.write_queue_ready(s, q, value as u32);
            }
            // An eight-byte queue address write, as its two halves (4.1.3.1).
            0x20 | 0x28 | 0x30 if valid && size == 8 => {
                let low = match offset {
                    0x20 => reg::QUEUE_DESC_LOW,
                    0x28 => reg::QUEUE_DRIVER_LOW,
                    _ => reg::QUEUE_DEVICE_LOW,
                };
                self.write_queue_layout(s, q, low, value as u32);
                return self.write_queue_layout(s, q, low + 4, (value >> 32) as u32);
            }
            // Read-only fields.
            _ => {}
        }
        Access::Completed(0)
    }
}

impl bus::Function for PciFunction {
    fn config_read(&self, reg_idx: usize) -> u32 {
        let t = self.transport();
        let config = lock(&t.config);
        let value = config.read_reg(reg_idx);
        match reg_idx {
            // Interrupt Status mirrors the ISR while INTx is the function's interrupt (virtio 1.2 4.1.4.5; PCI 3.0
            // 6.2.3: whatever Interrupt Disable says).
            1 if t.isr.load(SeqCst) != 0 && !lock(&t.msix).enabled() => value | INTERRUPT_STATUS,
            15 => value | INTA,
            _ => value,
        }
    }
    /// Republish BAR0 and drive INTx from the new command register and MSI-X Enable, under the configuration lock.
    fn config_write(&self, reg_idx: usize, offset: u64, data: &[u8]) {
        let t = self.transport();
        let mut config = lock(&t.config);
        // A moved BAR is read back from the configuration below, so the reprogramming list is not needed.
        let _ = config.write_config_register(reg_idx, offset, data);
        let bar = match config.command() & MEMORY {
            0 => NO_BAR,
            // The live register, as hardware decodes it after any write width (the cached `get_bar_addr` follows
            // only dword writes); bits below the size are ignored, and a size probe's 0xFFFF_8000 lies outside the
            // window, so the BAR then decodes nothing (PCI 3.0 6.2.5.1).
            _ => u64::from(config.read_reg(4) & !(BAR_LEN as u32 - 1)),
        };
        t.bar.store(bar, SeqCst);
        let master = config.command() & BUS_MASTER != 0;
        if !t.bus_master.swap(master, SeqCst) && master {
            // Vectors held pending while bus mastering was off go out now, if MSI-X lets them.
            lock(&t.msix).release_pending();
        }
        t.drive(&config);
    }
    fn bar(&self) -> Option<u64> {
        let bar = self.transport().bar.load(SeqCst);
        (bar != NO_BAR).then_some(bar)
    }
    fn bar_len(&self) -> u64 {
        BAR_LEN
    }
    /// One BAR0 access. An offset or width no structure defines reads zero and ignores a write (the stated leniency:
    /// it does not stop the vCPU).
    fn bar_access(&self, vcpu: usize, offset: u64, size: u64, value: Option<u64>) -> Access {
        let d = &self.0;
        let done = Access::Completed(0);
        match offset {
            COMMON..ISR => match common(offset, size, value) {
                Some(register) => d.locked_access(vcpu, register, value),
                None => done,
            },
            ISR if size == 1 && value.is_none() => d.locked_access(vcpu, private(ISR, 1), None),
            // Device-specific configuration: read-only here, so a write is ignored (the spec's choice).
            DEVICE..NOTIFY if value.is_none() => {
                match d.config_access(reg::CONFIG + offset - DEVICE, size, None) {
                    Some(Access::Completed(v)) => Access::Completed(v),
                    _ => done,
                }
            }
            // Queue q's notification, a write of any width (virtio 1.2 4.1.4.4): the lock-free kick while its gate is
            // open, else the locked QueueNotify.
            NOTIFY..TABLE if value.is_some() && (offset - NOTIFY) % MULTIPLIER == 0 => {
                let q = (offset - NOTIFY) / MULTIPLIER;
                if q >= d.profile.queue_count() as u64 || d.kick(q) {
                    done
                } else {
                    d.locked_access(vcpu, reg::QUEUE_NOTIFY, Some(q))
                }
            }
            // MSI-X table and PBA: dword or qword accesses (PCI 3.0 6.8.2); the PBA is read-only.
            TABLE..0x6000 if matches!(size, 4 | 8) => {
                let mut msix = lock(&self.transport().msix);
                let mut word = [0; 8];
                let bytes = &mut word[..size as usize];
                match (offset < PBA, value) {
                    (true, None) => msix.read_table(offset - TABLE, bytes),
                    (true, Some(v)) => {
                        msix.write_table(offset - TABLE, &v.to_le_bytes()[..bytes.len()])
                    }
                    (false, None) => msix.read_pba(offset - PBA, bytes),
                    (false, Some(_)) => {}
                }
                match value {
                    None => Access::Completed(u64::from_le_bytes(word)),
                    Some(_) => done,
                }
            }
            _ => done,
        }
    }
    fn bar_poll(&self, vcpu: usize, token: u64) -> Access {
        self.0.poll(vcpu, token)
    }
    fn quiesce(&self) -> crate::Result<bool> {
        self.0.quiesce()
    }
}

#[cfg(test)]
mod tests {
    use super::super::{
        Identity, Used,
        block::owner_tests::{Rig, until},
        status,
        tests::{device, net},
    };
    use super::*;
    use crate::pci::Function;
    use std::time::{Duration, Instant};

    type Sent = Arc<Mutex<Vec<(u64, u32)>>>;
    /// A driver's view of one function: configuration space and BAR0 through the `Function` trait, and the MSI-X
    /// messages it sent.
    struct Driver {
        d: Arc<Virtio>,
        f: Arc<PciFunction>,
        sent: Sent,
    }
    fn attach(d: Arc<Virtio>) -> Driver {
        let sent = Sent::default();
        let log = sent.clone();
        let f = d.pci_with(1, Arc::new(move |a, v| log.lock().unwrap().push((a, v))));
        Driver { d, f, sent }
    }
    /// Entry i's message: physical destination i, vector 0x40 + i.
    fn message(i: u64) -> (u64, u32) {
        (0xfee0_0000 | i << 12, 0x40 + i as u32)
    }
    impl Driver {
        fn cfg(&self, offset: usize) -> u32 {
            self.f.config_read(offset / 4) >> (offset % 4 * 8)
        }
        fn cfg_write(&self, offset: usize, bytes: &[u8]) {
            self.f.config_write(offset / 4, (offset % 4) as u64, bytes);
        }
        fn command(&self, bits: u16) {
            self.cfg_write(4, &bits.to_le_bytes());
        }
        fn read(&self, offset: u64, size: u64) -> u64 {
            match self.f.bar_access(0, offset, size, None) {
                Access::Completed(v) => v,
                other => panic!("read {offset:#x}/{size}: {other:?}"),
            }
        }
        fn write(&self, offset: u64, size: u64, value: u64) {
            assert_eq!(
                self.f.bar_access(0, offset, size, Some(value)),
                Access::Completed(0),
                "write {offset:#x}/{size}"
            );
        }
        fn sent(&self) -> Vec<(u64, u32)> {
            self.sent.lock().unwrap().clone()
        }
        /// The capability list as Linux walks it: (offset, ID).
        fn caps(&self) -> Vec<(usize, u8)> {
            assert_ne!(self.cfg(6) as u16 & 0x10, 0, "Capabilities List bit");
            let mut at = self.cfg(0x34) as u8 as usize;
            let mut caps = vec![];
            while at != 0 {
                caps.push((at, self.cfg(at) as u8));
                at = self.cfg(at + 1) as u8 as usize;
            }
            caps
        }
        fn msix_cap(&self) -> usize {
            self.caps().iter().find(|c| c.1 == 0x11).unwrap().0
        }
        /// Message Control's Enable (bit 15) and Function Mask (bit 14), as a byte write to its high byte.
        fn msix(&self, enable: bool, mask: bool) {
            let high = u8::from(enable) << 7 | u8::from(mask) << 6;
            self.cfg_write(self.msix_cap() + 3, &[high]);
        }
        fn entry(&self, i: u64, masked: bool) {
            let (address, data) = message(i);
            self.write(TABLE + 16 * i, 8, address);
            self.write(
                TABLE + 16 * i + 8,
                8,
                u64::from(masked) << 32 | u64::from(data),
            );
        }
        fn level(&self) -> bool {
            self.d.irqs.level(self.d.gsi)
        }
        fn queues(&self) -> u64 {
            self.d.profile.queue_count() as u64
        }
    }
    /// Linux's `vp_modern_probe` and the feature negotiation of `virtio_dev_probe`, checking each field's width:
    /// memory decoding and bus mastering on, reset, ACKNOWLEDGE | DRIVER, VERSION_1 only, FEATURES_OK read back.
    fn negotiated_over_pci(rig: &Driver) {
        rig.command(MEMORY | BUS_MASTER);
        rig.write(0x14, 1, 0);
        assert_eq!(rig.read(0x14, 1), 0);
        rig.write(0x14, 1, 1);
        rig.write(0x14, 1, 3);
        rig.write(0x00, 4, 1);
        assert_ne!(rig.read(0x04, 4) & 1, 0, "VERSION_1");
        rig.write(0x08, 4, 0);
        rig.write(0x0c, 4, 0);
        rig.write(0x08, 4, 1);
        rig.write(0x0c, 4, 1);
        rig.write(0x14, 1, 11);
        assert_eq!(rig.read(0x14, 1), 11);
    }
    /// Then `vp_find_vqs`: with `msix`, entries 0..=queues, MSI-X on, configuration vector 0 and queue q's vector
    /// q + 1; every queue eight entries at 0x1000 * (q + 1); DRIVER_OK.
    fn live_over_pci(rig: &Driver, msix: bool) {
        negotiated_over_pci(rig);
        if msix {
            for i in 0..=rig.queues() {
                rig.entry(i, false);
            }
            rig.msix(true, false);
            rig.write(0x10, 2, 0);
            assert_eq!(rig.read(0x10, 2), 0);
        }
        for q in 0..rig.queues() {
            rig.write(0x16, 2, q);
            let base = 0x1000 * (q + 1);
            rig.write(0x18, 2, 8);
            rig.write(0x20, 8, base);
            rig.write(0x28, 8, base + 0x800);
            rig.write(0x30, 8, base + 0xc00);
            if msix {
                rig.write(0x1a, 2, q + 1);
                assert_eq!(rig.read(0x1a, 2), q + 1);
            }
            rig.write(0x1c, 2, 1);
            assert_eq!(rig.read(0x1c, 2), 1);
        }
        rig.write(0x14, 1, 15);
        assert_eq!(rig.read(0x14, 1), 15);
    }
    fn rng(msix: bool) -> Driver {
        let rig = attach(device());
        live_over_pci(&rig, msix);
        rig
    }
    /// A completion on `q` of its current incarnation that wants an interrupt (as the owner's `complete` ends).
    fn complete(d: &Virtio, q: usize) {
        let mut s = d.lock();
        let id = s.identity(q);
        complete_as(d, &mut s, id);
    }
    fn complete_as(d: &Virtio, s: &mut State, id: Identity) {
        let used = Used::new(s.queues[id.queue], [(0, 0)]);
        d.interrupt(s, id, &used);
    }
    fn pba(rig: &Driver) -> u64 {
        rig.read(PBA, 8)
    }

    #[test]
    fn capabilities_are_the_four_virtio_structures_and_msix_within_bar0() {
        for (d, id, class) in [(device(), 4, 0x00ff_0001), (net(), 1, 0x0200_0001)] {
            let rig = attach(d);
            assert_eq!(rig.cfg(0), 0x1040_1af4 + (id << 16));
            assert_eq!(rig.cfg(8), class, "class and revision 1");
            assert_eq!(rig.cfg(0x2c), id << 16 | 0x1af4, "subsystem");
            assert_eq!(
                rig.cfg(0x3c) & 0xffff,
                0x100 | rig.d.gsi as u32,
                "INTA#, GSI"
            );
            assert_eq!(rig.cfg(0x10), 0xe001_0000, "BAR0: slot 1, 32-bit memory");
            assert_eq!(rig.cfg(4) & 0xffff, 0, "decoding and bus mastering off");
            assert_eq!(rig.f.bar(), None);
            let caps = rig.caps();
            let ids: Vec<u8> = caps.iter().map(|c| c.1).collect();
            assert_eq!(ids, [0x11, 9, 9, 9, 9]);
            let m = caps[0].0;
            assert_eq!(
                rig.cfg(m + 2) & 0xffff,
                rig.queues() as u32,
                "table size N - 1"
            );
            assert_eq!(rig.cfg(m + 4), 0x4000, "table, BIR 0");
            assert_eq!(rig.cfg(m + 8), 0x5000, "PBA, BIR 0");
            let mut seen = vec![];
            for &(at, _) in &caps[1..] {
                let (len, cfg_type, bar) = (
                    rig.cfg(at + 2) as u8,
                    rig.cfg(at + 3) as u8,
                    rig.cfg(at + 4) as u8,
                );
                let (offset, length) = (rig.cfg(at + 8), rig.cfg(at + 12));
                assert_eq!(bar, 0);
                assert_eq!(length, 0x1000);
                assert!(u64::from(offset + length) <= rig.f.bar_len());
                if cfg_type == 2 {
                    assert_eq!((len, rig.cfg(at + 16)), (20, 4), "notify_off_multiplier");
                } else {
                    assert_eq!(len, 16);
                }
                seen.push((cfg_type, offset));
            }
            assert_eq!(seen, [(1, 0), (2, 0x3000), (3, 0x1000), (4, 0x2000)]);
        }
    }
    #[test]
    fn negotiation_reaches_driver_ok_with_exact_field_widths() {
        let rig = rng(true);
        assert_eq!(rig.f.bar(), Some(0xe001_0000));
        assert_eq!(rig.read(0x12, 2), 1, "num_queues");
        let s = rig.d.lock();
        assert_eq!(s.status, 15);
        assert_eq!(s.features, 1 << 32);
        assert!(s.queues[0].ready);
        assert_eq!(
            (s.queues[0].desc, s.queues[0].avail, s.queues[0].used),
            (0x1000, 0x1800, 0x1c00)
        );
        assert_eq!(s.vectors.0, [1, NO_VECTOR, 0]);
    }
    #[test]
    fn queue_size_reads_the_maximum_until_written() {
        let rig = attach(device());
        negotiated_over_pci(&rig);
        assert_eq!(rig.read(0x18, 2), 8);
        rig.write(0x18, 2, 4);
        assert_eq!(rig.read(0x18, 2), 4);
        rig.write(0x16, 2, 1);
        assert_eq!(rig.read(0x18, 2), 0, "no queue 1");
    }
    #[test]
    fn queue_enable_without_a_size_uses_the_maximum() {
        let rig = attach(device());
        negotiated_over_pci(&rig);
        rig.write(0x20, 8, 0x1000);
        rig.write(0x28, 8, 0x1800);
        rig.write(0x30, 8, 0x1c00);
        rig.write(0x1c, 2, 1);
        let s = rig.d.lock();
        assert!(s.queues[0].ready);
        assert_eq!(s.queues[0].size, 8);
        assert_eq!(s.status & status::NEEDS_RESET, 0);
    }
    #[test]
    fn vector_fields_read_back_and_refuse_out_of_range_with_no_vector() {
        let rig = attach(device()); // table of 2
        for (value, back) in [
            (1, 1),
            (2, NO_VECTOR),
            (0, 0),
            (0x7ff, NO_VECTOR),
            (0xffff, NO_VECTOR),
        ] {
            rig.write(0x10, 2, value);
            assert_eq!(rig.read(0x10, 2), u64::from(back));
            rig.write(0x1a, 2, value);
            assert_eq!(rig.read(0x1a, 2), u64::from(back));
        }
        rig.write(0x16, 2, 1);
        rig.write(0x1a, 2, 0);
        assert_eq!(rig.read(0x1a, 2), u64::from(NO_VECTOR), "no queue 1");
    }
    #[test]
    fn completion_delivers_the_queues_vector_once() {
        let block = Rig::new("pci-msix");
        let rig = attach(block.r.clone());
        rig.command(MEMORY | BUS_MASTER);
        rig.entry(0, false);
        rig.entry(1, false);
        rig.msix(true, false);
        rig.write(0x16, 2, 0);
        rig.write(0x1a, 2, 1);
        block.run(|block| {
            block.publish(block.read_request(0));
            assert!(block.kick());
            until("completed", || block.used_index() == 1);
        });
        assert_eq!(rig.sent(), [message(1)]);
        assert!(!rig.level(), "no INTx while MSI-X is enabled");
    }
    #[test]
    fn no_vector_or_msix_disabled_or_bus_master_off_delivers_nothing() {
        for case in 0..3 {
            let rig = rng(true);
            match case {
                0 => rig.write(0x1a, 2, u64::from(NO_VECTOR)),
                1 => rig.msix(false, false),
                _ => rig.command(MEMORY),
            }
            complete(&rig.d, 0);
            assert!(rig.sent().is_empty(), "case {case}");
            // Without bus mastering the vector waits pending (sent once Bus Master is set); the others leave none.
            let pending = if case == 2 { 2 } else { 0 };
            assert_eq!(pba(&rig), pending, "case {case}");
        }
    }
    #[test]
    fn completion_while_masked_is_delivered_at_unmask() {
        // The function mask, then the entry's own mask.
        let rig = rng(true);
        rig.msix(true, true);
        complete(&rig.d, 0);
        assert!(rig.sent().is_empty());
        assert_eq!(pba(&rig), 2);
        rig.msix(true, false);
        assert_eq!(rig.sent(), [message(1)]);
        rig.entry(1, true);
        complete(&rig.d, 0);
        assert_eq!(rig.sent().len(), 1);
        rig.entry(1, false);
        assert_eq!(rig.sent(), [message(1), message(1)]);
        assert_eq!(pba(&rig), 0);
    }
    #[test]
    fn device_reset_returns_vectors_to_no_vector_and_drops_stale_raises() {
        let rig = rng(true);
        let old = rig.d.lock().identity(0);
        // A request in flight: the reset waits for it, and its completion meanwhile raises nothing.
        rig.d.lock().in_flight.push(old);
        let Access::Deferred(token) = rig.f.bar_access(1, 0x14, 1, Some(0)) else {
            panic!("reset did not wait for the request in flight");
        };
        complete_as(&rig.d, &mut rig.d.lock(), old);
        {
            let mut s = rig.d.lock();
            s.retire(old);
            rig.d.retire_reset(&mut s);
        }
        assert_eq!(rig.f.bar_poll(1, token), Access::Completed(0));
        complete_as(&rig.d, &mut rig.d.lock(), old);
        assert!(rig.sent().is_empty());
        assert_eq!(rig.read(ISR, 1), 0);
        assert_eq!(rig.read(0x10, 2), u64::from(NO_VECTOR));
        assert_eq!(rig.read(0x1a, 2), u64::from(NO_VECTOR));
        assert_eq!(rig.d.lock().vectors.0, [NO_VECTOR; 3]);
        // MSI-X itself is PCI state: still enabled, the table kept.
        assert_eq!(rig.read(TABLE + 16, 8), message(1).0);
        assert!(lock(&rig.d.pci.get().unwrap().msix).enabled());
        // A vector pending under a mask, or without bus mastering, is the closed incarnation's: a reset drops it.
        for gated in [false, true] {
            let rig = rng(true);
            if gated {
                rig.command(MEMORY);
            } else {
                rig.msix(true, true);
            }
            complete(&rig.d, 0);
            assert_eq!(pba(&rig), 2);
            rig.write(0x14, 1, 0);
            assert_eq!(pba(&rig), 0, "gated {gated}");
            rig.msix(true, false);
            rig.command(MEMORY | BUS_MASTER);
            assert!(rig.sent().is_empty(), "gated {gated}");
        }
    }
    #[test]
    fn network_hold_back_delivers_each_owing_queue_once() {
        for shared in [false, true] {
            let rig = attach(net());
            live_over_pci(&rig, true);
            if shared {
                rig.write(0x16, 2, 1);
                rig.write(0x1a, 2, 1);
            }
            {
                let mut s = rig.d.lock();
                // Hold back for good: the release below is the owner's.
                s.owed_at = Some(Instant::now() + Duration::from_secs(60));
                for q in [0, 1, 0, 1, 0] {
                    let id = s.identity(q);
                    complete_as(&rig.d, &mut s, id);
                }
                assert_eq!(s.owed, 5);
            }
            assert!(rig.sent().is_empty(), "held back");
            rig.d.raise(&mut rig.d.lock());
            let mut sent = rig.sent();
            sent.sort();
            let want = if shared {
                vec![message(1)]
            } else {
                vec![message(1), message(2)]
            };
            assert_eq!(sent, want, "shared {shared}");
            rig.d.raise(&mut rig.d.lock());
            assert_eq!(rig.sent().len(), want.len(), "nothing owed any more");
        }
    }
    #[test]
    fn notify_at_multiplier_offset_kicks_that_queue_without_the_state_lock() {
        let rig = attach(net());
        live_over_pci(&rig, true);
        {
            let _held = rig.d.state.lock().unwrap();
            // TX (queue 1) at 0x3004, as a 16-bit write of its index.
            assert_eq!(
                rig.f.bar_access(0, NOTIFY + 4, 2, Some(1)),
                Access::Completed(0)
            );
        }
        assert_ne!(rig.d.kicks[1].load(SeqCst), 0);
        assert_eq!(rig.d.kicks[0].load(SeqCst), 0);
        // Beyond the queues: ignored.
        rig.write(NOTIFY + 4 * 2, 2, 2);
        rig.write(NOTIFY + 4 * 7, 4, 7);
        assert!(!rig.d.lock().notified.iter().any(|n| *n));
        // RX's gate never opens: the locked notify, which sees no received frame.
        rig.write(NOTIFY, 2, 0);
        assert!(rig.d.lock().rx_kicked);
    }
    #[test]
    fn isr_read_clears() {
        let rig = rng(false);
        complete(&rig.d, 0);
        assert_eq!(rig.read(ISR, 1), 1);
        assert_eq!(rig.read(ISR, 1), 0);
        rig.d.fail(&mut rig.d.lock());
        assert_eq!(rig.read(ISR, 1), 2);
        assert_eq!(rig.read(ISR, 1), 0);
    }
    #[test]
    fn unimplemented_offsets_and_widths_read_zero_and_never_refuse() {
        let rig = attach(net());
        live_over_pci(&rig, true);
        rig.d.raise(&mut rig.d.lock());
        for (offset, size) in [
            (0x00, 2),
            (0x04, 8),
            (0x14, 4),
            (0x10, 4),
            (0x22, 4),
            (0x24, 8),
            (0x38, 4),
            (0xffc, 4),
            (ISR, 4),
            (ISR + 1, 1),
            (DEVICE, 8),
            (DEVICE + 1, 2),
            (DEVICE + 0x800, 4),
            (NOTIFY, 4),
            (NOTIFY + 2, 2),
            (TABLE, 2),
            (TABLE + 1, 1),
            (PBA, 1),
            (0x6000, 4),
            (0x7ff8, 8),
        ] {
            assert_eq!(rig.read(offset, size), 0, "{offset:#x}/{size}");
        }
        assert_eq!(rig.read(ISR, 1), 1, "the wide ISR read did not clear it");
        let before = rig.d.lock().status;
        for (offset, size) in [
            (0x04, 4),
            (0x12, 2),
            (0x14, 4),
            (0x15, 1),
            (0x1e, 2),
            (0x22, 4),
            (0x38, 4),
            (ISR, 1),
            (DEVICE, 4),
            (PBA, 8),
            (0x6000, 4),
            (0x20, 4), // the queue is enabled: the MMIO front end refuses this
        ] {
            rig.write(offset, size, 0x5a);
        }
        assert_eq!(rig.d.lock().status, before);
        assert_eq!(rig.d.lock().queues[0].desc, 0x1000);
    }
    #[test]
    fn a_deferred_reset_over_pci_polls_to_completion() {
        let block = Rig::new("pci-reset");
        let rig = attach(block.r.clone());
        block.disk.hold(true);
        block.run(|block| {
            block.publish(block.read_request(0));
            assert!(block.kick());
            until("inline call", || block.disk.calls() == 1);
            let Access::Deferred(reset) = rig.f.bar_access(1, 0x14, 1, Some(0)) else {
                panic!("the reset did not wait for the call");
            };
            let Access::Deferred(read) = rig.f.bar_access(2, 0x14, 1, None) else {
                panic!("a read behind the reset did not wait");
            };
            assert!(matches!(rig.f.bar_poll(1, reset), Access::Deferred(_)));
            block.disk.hold(false);
            until("reset retired", || {
                rig.f.bar_poll(1, reset) == Access::Completed(0)
            });
            assert_eq!(rig.f.bar_poll(2, read), Access::Completed(0));
        });
        assert_eq!(block.r.lock().generation, 1);
        assert_eq!(block.used_index(), 0);
    }
    #[test]
    fn mmio_front_end_cannot_reach_the_private_registers() {
        let rig = rng(true);
        let plain = device();
        super::super::tests::live(&plain, &[0]);
        plain.access(0, reg::DEVICE_FEATURES_SEL, 4, Some(1));
        // Every MMIO register reads as on an MMIO device in the same state (the vectors are not visible).
        for offset in (0..0x100).step_by(4) {
            let read = |d: &Virtio| d.access(0, offset, 4, None);
            assert_eq!(read(&rig.d), read(&plain), "{offset:#x}");
        }
        // A tagged offset is not a register the MMIO front end serves.
        for register in [reg::PCI | 2 << 16 | 0x10, reg::PCI | 1 << 16 | ISR] {
            assert_eq!(rig.d.access(0, register, 4, Some(1)), Access::Completed(0));
            assert_eq!(rig.d.access(0, register, 4, None), Access::Completed(0));
        }
        complete(&rig.d, 0);
        assert_eq!(
            rig.d.access(0, reg::PCI | 1 << 16 | ISR, 4, None),
            Access::Completed(0)
        );
        assert_eq!(rig.d.lock().vectors.0, [1, NO_VECTOR, 0]);
        assert_eq!(rig.d.lock().interrupt, 1, "the ISR was not read");
        // And on an MMIO device the private path itself serves nothing.
        let mut s = plain.lock();
        assert_eq!(
            plain.register(&mut s, reg::PCI | 2 << 16 | 0x10, None),
            Access::Completed(0)
        );
    }
    #[test]
    fn every_read_write_field_reads_back_what_was_written() {
        let rig = attach(net());
        rig.command(MEMORY);
        rig.write(0x14, 1, 1);
        rig.write(0x14, 1, 3);
        for (offset, size, value) in [
            (0x00, 4, 1),
            (0x08, 4, 0),
            (0x0c, 4, 0x0001_0020),
            (0x08, 4, 1),
            (0x0c, 4, 1),
            (0x10, 2, 2),
            (0x16, 2, 1),
            (0x18, 2, 64),
            (0x1a, 2, 0),
            (0x20, 4, 0x2000),
            (0x24, 4, 0x1),
            (0x28, 4, 0x3000),
            (0x2c, 4, 0x2),
            (0x30, 4, 0x4000),
            (0x34, 4, 0x3),
        ] {
            rig.write(offset, size, value);
            assert_eq!(rig.read(offset, size), value, "{offset:#x}");
        }
        rig.write(0x08, 4, 0);
        assert_eq!(rig.read(0x0c, 4), 0x0001_0020, "the selected half");
        rig.write(0x0c, 4, 0);
        rig.write(0x14, 1, 11);
        assert_eq!(rig.read(0x14, 1), 11);
        rig.write(0x1c, 2, 1);
        assert_eq!(rig.read(0x1c, 2), 1);
        rig.write(0x16, 2, 0);
        assert_eq!(
            (rig.read(0x16, 2), rig.read(0x18, 2), rig.read(0x1e, 2)),
            (0, 256, 0)
        );
        rig.write(0x16, 2, 1);
        assert_eq!(rig.read(0x1e, 2), 1, "queue_notify_off");
    }
    #[test]
    fn eight_byte_accesses_to_queue_addresses_and_the_msix_table_take_effect() {
        let rig = attach(device());
        negotiated_over_pci(&rig);
        rig.write(0x20, 8, 0x1_0000_1000);
        assert_eq!(rig.read(0x20, 8), 0x1_0000_1000);
        assert_eq!((rig.read(0x20, 4), rig.read(0x24, 4)), (0x1000, 1));
        rig.write(0x28, 4, 0x1800);
        rig.write(0x2c, 4, 2);
        assert_eq!(rig.read(0x28, 8), 0x2_0000_1800);
        rig.write(0x30, 8, 0x1c00);
        {
            let s = rig.d.lock();
            assert_eq!(
                (s.queues[0].desc, s.queues[0].avail, s.queues[0].used),
                (0x1_0000_1000, 0x2_0000_1800, 0x1c00)
            );
        }
        rig.write(TABLE + 16, 8, 0xfee0_3000);
        rig.write(TABLE + 24, 8, 0x61);
        assert_eq!(rig.read(TABLE + 16, 8), 0xfee0_3000);
        assert_eq!(rig.read(TABLE + 24, 8), 0x61);
        assert_eq!(rig.read(TABLE + 24, 4), 0x61);
        rig.msix(true, false);
        rig.write(0x1a, 2, 1);
        rig.write(0x1c, 2, 1);
        rig.write(0x14, 1, 15);
        assert!(rig.d.lock().queues[0].ready);
        complete(&rig.d, 0);
        assert_eq!(rig.sent(), [(0xfee0_3000, 0x61)]);
    }
    #[test]
    fn device_config_writes_are_ignored() {
        let rig = attach(net());
        let mac = rig.read(DEVICE, 4);
        assert_eq!(mac, 2, "MAC 02:00:00:00 in the first word");
        assert_eq!(rig.read(DEVICE + 4, 2), 0x0100);
        rig.write(DEVICE, 4, 0xffff_ffff);
        rig.write(DEVICE + 4, 1, 0xff);
        assert_eq!(rig.read(DEVICE, 4), mac);
        assert_eq!(rig.read(DEVICE + 4, 2), 0x0100);
    }
    #[test]
    fn a_failed_device_delivers_the_config_vector() {
        let rig = rng(true);
        // Clearing a status bit breaks the initialization order: NEEDS_RESET.
        rig.write(0x14, 1, 1);
        assert_eq!(rig.read(0x14, 1), 15 | 64);
        assert_eq!(rig.sent(), [message(0)]);
        assert_eq!(rig.d.lock().interrupt, 2);
    }
    #[test]
    fn isr_is_mirrored_in_pci_status_while_msix_is_disabled() {
        let rig = rng(false);
        assert_eq!(rig.cfg(4) & INTERRUPT_STATUS, 0);
        complete(&rig.d, 0);
        assert_ne!(rig.cfg(4) & INTERRUPT_STATUS, 0);
        rig.read(ISR, 1);
        assert_eq!(rig.cfg(4) & INTERRUPT_STATUS, 0);
        complete(&rig.d, 0);
        rig.msix(true, false);
        assert_eq!(
            rig.cfg(4) & INTERRUPT_STATUS,
            0,
            "not INTx while MSI-X is enabled"
        );
        assert_eq!(rig.d.lock().interrupt, 1);
    }
    #[test]
    fn intx_line_follows_the_isr_while_msix_is_disabled() {
        let rig = rng(false);
        assert!(!rig.level());
        complete(&rig.d, 0);
        assert!(rig.level());
        complete(&rig.d, 0);
        assert!(rig.level());
        rig.read(ISR, 1);
        assert!(!rig.level());
        rig.d.fail(&mut rig.d.lock());
        assert!(rig.level(), "a configuration change too");
        assert!(rig.sent().is_empty());
    }
    #[test]
    fn interrupt_disable_bit_holds_the_line_low() {
        let rig = rng(false);
        rig.command(MEMORY | BUS_MASTER | INTX_DISABLE);
        complete(&rig.d, 0);
        assert!(!rig.level());
        assert_ne!(rig.cfg(4) & INTERRUPT_STATUS, 0, "still pending");
        rig.command(MEMORY | BUS_MASTER);
        assert!(rig.level(), "re-evaluated from the ISR");
        rig.command(MEMORY | BUS_MASTER | INTX_DISABLE);
        assert!(!rig.level());
    }
    #[test]
    fn enabling_msix_lowers_the_line_and_completions_no_longer_raise_it() {
        let rig = rng(false);
        complete(&rig.d, 0);
        assert!(rig.level());
        rig.msix(true, false);
        assert!(!rig.level());
        rig.read(ISR, 1);
        complete(&rig.d, 0);
        assert!(!rig.level());
        rig.msix(false, false);
        assert!(rig.level(), "disabling re-evaluates the line from the ISR");
    }
    #[test]
    fn isr_read_lowers_the_line() {
        let rig = rng(false);
        complete(&rig.d, 0);
        assert!(rig.level());
        assert_eq!(rig.read(ISR, 1), 1);
        assert!(!rig.level());
        assert_eq!(rig.d.lock().interrupt, 0);
    }
    #[test]
    fn power_off_drains_a_held_block_request_through_the_bus() {
        let block = Rig::new("pci-power");
        let rig = attach(block.r.clone());
        let mut devices = crate::devices::Devices::new(1 << 30);
        let mut b = bus::Bus::new();
        b.add(1, rig.f.clone()).unwrap();
        b.install(&mut devices).unwrap();
        block.disk.hold(true);
        block.run(|block| {
            block.publish(block.read_request(0));
            assert!(block.kick());
            until("disk call", || block.disk.calls() == 1);
            assert!(!devices.quiesce().unwrap());
            // Writes are ignored while draining.
            rig.write(0x14, 1, 0);
            block.disk.hold(false);
            until("drained", || devices.quiesce().unwrap());
        });
        assert_eq!(block.used_index(), 1);
        assert_eq!(block.r.lock().status, 15);
    }
    #[test]
    fn a_pending_vector_waits_for_bus_mastering_on_every_release_path() {
        let rig = rng(true);
        // Pending under the function mask, then bus mastering off: unmasking the function sends nothing.
        rig.msix(true, true);
        complete(&rig.d, 0);
        assert_eq!(pba(&rig), 2);
        rig.command(MEMORY);
        rig.msix(true, false);
        assert!(rig.sent().is_empty());
        assert_eq!(pba(&rig), 2, "still pending");
        // The entry's own mask and unmask while off: nothing either.
        rig.entry(1, true);
        rig.entry(1, false);
        assert!(rig.sent().is_empty());
        assert_eq!(pba(&rig), 2);
        // Bus mastering on: sent once.
        rig.command(MEMORY | BUS_MASTER);
        assert_eq!(rig.sent(), [message(1)]);
        assert_eq!(pba(&rig), 0);
        rig.command(MEMORY);
        rig.command(MEMORY | BUS_MASTER);
        assert_eq!(rig.sent().len(), 1);
        // A completion while bus mastering is off is not lost: it waits pending and goes out when it is set.
        rig.command(MEMORY);
        complete(&rig.d, 0);
        assert_eq!((rig.sent().len(), pba(&rig)), (1, 2));
        rig.command(MEMORY | BUS_MASTER);
        assert_eq!(rig.sent(), [message(1), message(1)]);
        assert_eq!(pba(&rig), 0);
    }
    /// BAR0 on a real bus window: (offset in the window, size) reads through the registry's handle.
    fn window(rig: &Driver) -> (crate::devices::Devices, impl Fn(u64, u64) -> u64) {
        let mut devices = crate::devices::Devices::new(1 << 30);
        let mut b = bus::Bus::new();
        b.add(1, rig.f.clone()).unwrap();
        b.install(&mut devices).unwrap();
        let base = u64::from(bus::platform::WINDOW.0);
        let direct = devices
            .find(base)
            .unwrap()
            .0
            .lock()
            .unwrap()
            .direct()
            .unwrap();
        let read = move |addr: u64, size: u64| match direct.access(0, addr - base, size, None) {
            Access::Completed(v) => v,
            other => panic!("window read {addr:#x}: {other:?}"),
        };
        (devices, read)
    }
    #[test]
    fn bar0_moves_with_any_write_width_and_decodes_only_where_it_is() {
        let rig = attach(device());
        let (_devices, read) = window(&rig);
        let num_queues = 0x12;
        // Decoding off: the launch address reads as unclaimed.
        assert_eq!(read(0xe001_0000 + num_queues, 2), 0xffff);
        rig.command(MEMORY);
        assert_eq!(read(0xe001_0000 + num_queues, 2), 1);
        let moves: [(u64, &[(usize, &[u8])]); 3] = [
            (0xe002_0000, &[(0x10, &0xe002_0000u32.to_le_bytes())]),
            (0xe003_0000, &[(0x10, &[0, 0]), (0x12, &[0x03, 0xe0])]),
            (
                0xe004_8000,
                &[
                    (0x10, &[0]),
                    (0x11, &[0x80]),
                    (0x12, &[0x04]),
                    (0x13, &[0xe0]),
                ],
            ),
        ];
        let mut old = 0xe001_0000;
        for (base, writes) in moves {
            for &(offset, bytes) in writes {
                rig.cfg_write(offset, bytes);
            }
            assert_eq!(rig.f.bar(), Some(base));
            assert_eq!(rig.cfg(0x10), base as u32);
            assert_eq!(read(base + num_queues, 2), 1, "{base:#x}");
            assert_eq!(read(old + num_queues, 2), 0xffff, "{old:#x} after the move");
            old = base;
        }
        rig.command(0);
        assert_eq!(rig.f.bar(), None);
        assert_eq!(read(old + num_queues, 2), 0xffff);
    }
    #[test]
    fn bar0_size_probe_reads_the_mask_and_decodes_nothing_meanwhile() {
        for decoding in [false, true] {
            let rig = attach(device());
            let (_devices, read) = window(&rig);
            rig.command(if decoding { MEMORY } else { 0 });
            rig.cfg_write(0x10, &u32::MAX.to_le_bytes());
            assert_eq!(rig.cfg(0x10), 0xffff_8000, "32 KiB, 32-bit memory");
            assert_eq!(read(0xe001_0000 + 0x12, 2), 0xffff, "decoding {decoding}");
            rig.cfg_write(0x10, &0xe001_0000u32.to_le_bytes());
            assert_eq!(rig.cfg(0x10), 0xe001_0000);
            rig.command(MEMORY);
            assert_eq!(read(0xe001_0000 + 0x12, 2), 1, "decoding {decoding}");
        }
    }
    /// The line-ordering argument under contention: completions and ISR reads on one thread, MSI-X Enable and
    /// Interrupt Disable toggles through configuration writes on another; afterwards the line is at the level of the
    /// final state.
    #[test]
    fn the_line_matches_the_final_state_after_racing_completions_and_config_writes() {
        for round in 0..20 {
            let rig = rng(false);
            std::thread::scope(|scope| {
                scope.spawn(|| {
                    for i in 0..500 {
                        complete(&rig.d, 0);
                        if (i + round) % 3 == 0 {
                            rig.read(ISR, 1);
                        }
                    }
                });
                scope.spawn(|| {
                    for i in 0..500u32 {
                        rig.msix(i % 2 == round % 2, false);
                        let disable = if (i + round) % 5 == 0 {
                            INTX_DISABLE
                        } else {
                            0
                        };
                        rig.command(MEMORY | BUS_MASTER | disable);
                    }
                });
            });
            let msix = rig.cfg(rig.msix_cap() + 2) & 0x8000 != 0;
            let disabled = rig.cfg(4) & u32::from(INTX_DISABLE) != 0;
            let isr = rig.d.lock().interrupt;
            assert_eq!(
                rig.level(),
                isr != 0 && !disabled && !msix,
                "round {round}: isr {isr}, MSI-X {msix}, disable {disabled}"
            );
        }
    }
}
