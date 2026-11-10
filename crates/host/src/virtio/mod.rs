//! Modern virtio-MMIO devices (split rings, one active guest request at a time). Every guest buffer
//! is copied into owned storage inside a short copy window ([`crate::execution::io`]); no backend
//! call holds a window or a guest address.
//!
//! Lifecycle: create the device before boot, run its owner ([`Virtio::run`]) on its own thread
//! alongside the vCPU owners, call [`Virtio::stop`], and join the owner before detaching IRQs or
//! destroying the VM.
mod block;
mod net;
mod queue;
pub use net::PacketPort;
#[cfg(feature = "testing")]
pub mod testing;
use crate::{
    Result,
    boot::acpi::VirtioMmio,
    devices::{self, Access},
    execution::{
        Backing, Stops,
        io::{CopyPermit, Transfer},
    },
    irq::{Busy, Irqs},
    vm::Machine,
};
use queue::{Queue, Snapshot};
use std::{
    collections::VecDeque,
    sync::{
        Arc, Condvar, Mutex, MutexGuard,
        atomic::{AtomicBool, Ordering::SeqCst},
    },
    time::Duration,
};
use virtio_queue::desc::split::Descriptor;

/// Modern virtio-MMIO register offsets (virtio 1.3 §4.2.2).
mod reg {
    pub const MAGIC_VALUE: u64 = 0x00;
    pub const VERSION: u64 = 0x04;
    pub const DEVICE_ID: u64 = 0x08;
    pub const VENDOR_ID: u64 = 0x0c;
    pub const DEVICE_FEATURES: u64 = 0x10;
    pub const DEVICE_FEATURES_SEL: u64 = 0x14;
    pub const DRIVER_FEATURES: u64 = 0x20;
    pub const DRIVER_FEATURES_SEL: u64 = 0x24;
    pub const QUEUE_SEL: u64 = 0x30;
    pub const QUEUE_NUM_MAX: u64 = 0x34;
    pub const QUEUE_NUM: u64 = 0x38;
    pub const QUEUE_READY: u64 = 0x44;
    pub const QUEUE_NOTIFY: u64 = 0x50;
    pub const INTERRUPT_STATUS: u64 = 0x60;
    pub const INTERRUPT_ACK: u64 = 0x64;
    pub const STATUS: u64 = 0x70;
    pub const QUEUE_DESC_LOW: u64 = 0x80;
    pub const QUEUE_DESC_HIGH: u64 = 0x84;
    pub const QUEUE_DRIVER_LOW: u64 = 0x90;
    pub const QUEUE_DRIVER_HIGH: u64 = 0x94;
    pub const QUEUE_DEVICE_LOW: u64 = 0xa0;
    pub const QUEUE_DEVICE_HIGH: u64 = 0xa4;
    /// Device-specific configuration space.
    pub const CONFIG: u64 = 0x100;
}

/// Device status bits (virtio 1.3 §2.1).
mod status {
    pub const ACKNOWLEDGE: u32 = 1;
    pub const DRIVER: u32 = 2;
    pub const DRIVER_OK: u32 = 4;
    pub const FEATURES_OK: u32 = 8;
    pub const NEEDS_RESET: u32 = 64;
    pub const FAILED: u32 = 128;
    /// The bits a driver may set; NEEDS_RESET is the device's.
    pub const DRIVER_BITS: u32 = ACKNOWLEDGE | DRIVER | DRIVER_OK | FEATURES_OK | FAILED;
}

/// InterruptStatus bits.
const USED_BUFFER: u32 = 1;
const CONFIG_CHANGE: u32 = 2;
/// "virt", little-endian.
const MAGIC: u32 = 0x7472_6976;
/// The modern (non-legacy) virtio-MMIO interface.
const MMIO_VERSION: u32 = 2;
/// "NATE", little-endian.
const VENDOR: u32 = 0x4e41_5445;
/// VIRTIO_F_VERSION_1, required of every driver.
const VERSION_1: u64 = 1 << 32;
/// VIRTQ_AVAIL_F_NO_INTERRUPT in the available ring's flags.
const AVAIL_NO_INTERRUPT: u16 = 1;
const RNG_MAX_QUEUE: u16 = 8;
/// The most entropy one request returns (also getentropy's limit).
const MAX_PAYLOAD: usize = 256;

/// One resource declaration per device, shared by ACPI discovery and the MMIO registry.
pub const BLOCK_RESOURCE: VirtioMmio = VirtioMmio {
    uid: 0,
    base: 0xf000_0000,
    gsi: 16,
};
pub const NET_RESOURCE: VirtioMmio = VirtioMmio {
    uid: 1,
    base: 0xf000_1000,
    gsi: 17,
};
pub const RNG_RESOURCE: VirtioMmio = VirtioMmio {
    uid: 2,
    base: 0xf000_2000,
    gsi: 18,
};

/// What a reset closes: one queue (QueueReady = 0) or the whole device (Status = 0).
#[derive(Clone, Copy)]
enum Reset {
    Queue(usize),
    Device,
}
impl Reset {
    fn affects(self, id: Identity) -> bool {
        match self {
            Self::Device => true,
            Self::Queue(q) => q == id.queue,
        }
    }
}

/// The incarnation a request belongs to. A device reset advances `device`, a queue reset that
/// queue's `generation`; work from an older incarnation may finish its copy but never completes
/// visibly (no interrupt, no further admission).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
struct Identity {
    queue: usize,
    device: u64,
    generation: u64,
}

/// A register access waiting its turn behind a reset or an earlier waiting access.
struct Waiting {
    token: u64,
    vcpu: usize,
    offset: u64,
    value: Option<u64>,
    /// The access was the reset itself: it already took effect and only waits for retirement.
    applied: bool,
}

#[derive(Default)]
struct State {
    /// The Status register.
    status: u32,
    /// DeviceFeaturesSel and DriverFeaturesSel.
    device_sel: u32,
    driver_sel: u32,
    /// The driver's accepted features.
    features: u64,
    queue_sel: u32,
    queues: [Queue; 2],
    /// Per-queue incarnation, advanced by a queue reset.
    generations: [u64; 2],
    /// The InterruptStatus register.
    interrupt: u32,
    /// Device incarnation, advanced by a device reset.
    generation: u64,
    /// Per-queue work flag: a guest notification, a packet arrival, or work left after a completion.
    notified: [bool; 2],
    /// The request the owner is processing.
    active: Option<Identity>,
    /// Where the owner's round-robin scan of `notified` starts.
    cursor: usize,
    /// Power-off asked the device to drain; guest writes other than InterruptACK are ignored.
    quiescing: bool,
    /// The drain finished successfully.
    drained: bool,
    /// An accepted reset waiting for the active request to retire.
    closing: Option<Reset>,
    /// Admission closed for good (the VM is ending).
    stopped: bool,
    /// The active request's copy permit; reset and stop cancel it.
    permit: Option<Arc<CopyPermit>>,
    /// Deferred register accesses, answered in arrival order.
    waiting: VecDeque<Waiting>,
    /// The last deferred-access token issued.
    token: u64,
    /// Keeps the VM from ending as all-halted while work is pending.
    busy: Option<Busy>,
    /// One received packet waiting for an RX buffer, tagged with the RX queue's incarnation.
    received: Option<(Identity, net::Packet)>,
    backend_error: Option<String>,
}

impl State {
    fn identity(&self, queue: usize) -> Identity {
        Identity {
            queue,
            device: self.generation,
            generation: self.generations[queue],
        }
    }
    /// `id` is still the live incarnation: not stopped, not reset, and no reset closing it.
    fn current(&self, id: Identity) -> bool {
        !self.stopped
            && self.identity(id.queue) == id
            && !self.closing.is_some_and(|r| r.affects(id))
    }
    fn pending(&self) -> bool {
        self.notified.iter().any(|v| *v)
    }
    /// DRIVER_OK is set and the device has neither failed nor asked for a reset.
    fn driver_ready(&self) -> bool {
        let mask = status::DRIVER_OK | status::NEEDS_RESET | status::FAILED;
        self.status & mask == status::DRIVER_OK
    }
    /// The driver may hand `queue` new work now.
    fn accepting(&self, queue: usize) -> bool {
        !self.quiescing && self.queues[queue].ready && self.driver_ready()
    }
    /// The owner is processing a request from `queue`.
    fn queue_active(&self, queue: usize) -> bool {
        self.active.is_some_and(|id| id.queue == queue)
    }
    /// Another access may wait: each vCPU has at most one outstanding access, the queue is
    /// bounded, and tokens never wrap.
    fn can_wait(&self, vcpu: usize) -> bool {
        self.waiting.len() < natemu_abi::execution::cache::MAX_VCPUS
            && !self.waiting.iter().any(|w| w.vcpu == vcpu)
            && self.token != u64::MAX
    }
}

/// A production virtio device with up to two queues. Clones share one register/IRQ ordering
/// domain; exactly one independent owner calls `run`. The MMIO adapter retains no guest-memory
/// authority.
pub struct Virtio {
    profile: Profile,
    state: Mutex<State>,
    wake: Condvar,
    irqs: Irqs,
    gsi: usize,
    running: AtomicBool,
}
enum Profile {
    Entropy,
    Block(block::Disk),
    Network(net::Network),
}
impl Profile {
    fn device_id(&self) -> u32 {
        match self {
            Self::Network(_) => 1,
            Self::Block(_) => 2,
            Self::Entropy => 4,
        }
    }
    fn queue_count(&self) -> usize {
        if matches!(self, Self::Network(_)) {
            2
        } else {
            1
        }
    }
    fn queue_max(&self) -> u16 {
        match self {
            Self::Entropy => RNG_MAX_QUEUE,
            Self::Block(_) => 128,
            Self::Network(_) => 256,
        }
    }
    fn features(&self) -> u64 {
        match self {
            Self::Entropy => VERSION_1,
            Self::Block(_) => block::FEATURES,
            Self::Network(_) => net::FEATURES,
        }
    }
    fn required(&self) -> u64 {
        match self {
            Self::Entropy | Self::Network(_) => VERSION_1,
            Self::Block(_) => block::REQUIRED,
        }
    }
    /// The driver accepted only offered features and every required one.
    fn accepts(&self, features: u64) -> bool {
        features & !self.features() == 0 && features & self.required() == self.required()
    }
    /// The network RX queue: its buffers alone are not work; a packet arrival is.
    fn is_rx(&self, queue: usize) -> bool {
        matches!(self, Self::Network(_)) && queue == 0
    }
}

/// What a request needs to open copy windows on the running VM.
#[derive(Clone, Copy)]
struct Guest<'a> {
    backing: &'a Backing,
    machine: &'a Machine,
    stops: &'a Stops,
}

/// One admitted descriptor chain: its queue's registers at admission, incarnation and head.
struct Chain<'c> {
    queue: Queue,
    id: Identity,
    head: u16,
    descriptors: &'c [Descriptor],
}

/// A used-ring element and index for one chain, and room to read the available ring's flags
/// and index back after publishing them.
struct UsedEntry {
    element: [u8; 8],
    index: [u8; 2],
    avail: [u8; 4],
}
impl UsedEntry {
    fn new(chain: &Chain<'_>, len: u32) -> Self {
        let mut element = [0; 8];
        element[..4].copy_from_slice(&u32::from(chain.head).to_le_bytes());
        element[4..].copy_from_slice(&len.to_le_bytes());
        Self {
            element,
            index: chain.queue.next_used.wrapping_add(1).to_le_bytes(),
            avail: [0; 4],
        }
    }
    /// Append the element, then the used index, then a read of the available ring's flags and
    /// index. Reading after publication sees suppression and newly published work even when a
    /// guest buffer aliases ring metadata.
    fn publish<'a>(&'a mut self, q: Queue, transfers: &mut Vec<Transfer<'a>>) {
        transfers.push(Transfer::Write {
            gpa: q.used + 4 + u64::from(q.next_used % q.size) * 8,
            bytes: &self.element,
        });
        transfers.push(Transfer::Write {
            gpa: q.used + 2,
            bytes: &self.index,
        });
        transfers.push(Transfer::Read {
            gpa: q.avail,
            bytes: &mut self.avail,
        });
    }
    fn interrupt_wanted(&self) -> bool {
        u16::from_le_bytes([self.avail[0], self.avail[1]]) & AVAIL_NO_INTERRUPT == 0
    }
    /// The driver published more chains than the one just completed.
    fn more_published(&self, q: Queue) -> bool {
        u16::from_le_bytes([self.avail[2], self.avail[3]]) != q.next_avail.wrapping_add(1)
    }
}

/// Read guest extents back to back into `bytes`, which must be exactly their total length.
fn gather<'a>(
    extents: impl ExactSizeIterator<Item = (u64, usize)>,
    mut bytes: &'a mut [u8],
) -> Vec<Transfer<'a>> {
    let mut reads = Vec::with_capacity(extents.len());
    for (gpa, len) in extents {
        let (part, rest) = bytes.split_at_mut(len);
        bytes = rest;
        reads.push(Transfer::Read { gpa, bytes: part });
    }
    reads
}

/// Write `bytes` across the chain's buffers in order; buffers past the data get no span.
fn scatter<'a>(descriptors: &[Descriptor], bytes: &'a [u8]) -> Vec<Transfer<'a>> {
    let mut writes = Vec::with_capacity(descriptors.len() + 3);
    let mut offset = 0;
    for d in descriptors {
        let count = (d.len() as usize).min(bytes.len() - offset);
        if count != 0 {
            writes.push(Transfer::Write {
                gpa: d.addr().0,
                bytes: &bytes[offset..offset + count],
            });
            offset += count;
        }
    }
    writes
}

/// Replace the low (`high` false) or high 32 bits of `word`.
fn set_half(word: &mut u64, high: bool, value: u32) {
    let shift = if high { 32 } else { 0 };
    *word = (*word & !(u64::from(u32::MAX) << shift)) | (u64::from(value) << shift);
}

/// Feature word `sel` (0: bits 0-31, 1: bits 32-63, otherwise none).
fn feature_word(features: u64, sel: u32) -> u32 {
    match sel {
        0 => features as u32,
        1 => (features >> 32) as u32,
        _ => 0,
    }
}

/// A driver only adds status bits, in the order virtio 1.3 §3.1.1 sets them, and never sets
/// NEEDS_RESET. Status 0 (reset) is handled before this check.
fn status_write_allowed(old: u32, new: u32) -> bool {
    use status::*;
    let has = |bit: u32| new & bit != 0;
    let unknown_bits = new & !DRIVER_BITS != 0;
    let clears_bits = new & old != old;
    let driver_before_acknowledge = has(DRIVER) && !has(ACKNOWLEDGE);
    let features_before_driver =
        has(FEATURES_OK) && new & (ACKNOWLEDGE | DRIVER) != ACKNOWLEDGE | DRIVER;
    let ok_before_features = has(DRIVER_OK) && (!has(FEATURES_OK) || old & FEATURES_OK == 0);
    !(unknown_bits
        || clears_bits
        || driver_before_acknowledge
        || features_before_driver
        || ok_before_features)
}

/// A read of device configuration `config` at `offset` (from its start): byte, half or word,
/// naturally aligned and inside it. Configuration is read-only.
fn read_config(config: &[u8], offset: u64, size: u64, value: Option<u64>) -> Access {
    let fits = || offset + size <= config.len() as u64;
    // `size` is checked nonzero before it divides.
    if value.is_some() || !matches!(size, 1 | 2 | 4) || offset % size != 0 || !fits() {
        return Access::Refused;
    }
    let (start, end) = (offset as usize, (offset + size) as usize);
    let mut word = [0; 8];
    word[..end - start].copy_from_slice(&config[start..end]);
    Access::Completed(u64::from_le_bytes(word))
}

/// Fill `bytes` (at most 256) from the host's entropy source.
fn fill_entropy(bytes: &mut [u8]) -> std::io::Result<()> {
    unsafe extern "C" {
        fn getentropy(buffer: *mut u8, size: usize) -> i32;
    }
    // SAFETY: `bytes` is a live, writable slice of `bytes.len()` bytes, at most getentropy's
    // 256-byte limit (callers pass at most MAX_PAYLOAD).
    if unsafe { getentropy(bytes.as_mut_ptr(), bytes.len()) } == 0 {
        Ok(())
    } else {
        Err(std::io::Error::last_os_error())
    }
}

/// How a request ended, for the owner's bookkeeping.
enum Request {
    /// No chain was available (or, for RX, no packet fit).
    Empty,
    /// A reset or stop overtook the request before it published anything.
    Canceled,
    /// The used ring was published. `pending` asks the owner to look at the queue again.
    Committed { pending: bool },
    /// The driver broke the protocol; the device asks for a reset.
    Invalid(String),
}

/// What the owner does next.
enum Work {
    Request {
        queue: Queue,
        id: Identity,
        received: Option<net::Packet>,
    },
    Drain,
    Stop,
}

impl Virtio {
    pub fn rng(irqs: Irqs, gsi: usize) -> Arc<Self> {
        Self::new(irqs, gsi, Profile::Entropy)
    }
    pub fn block(irqs: Irqs, gsi: usize, path: &std::path::Path) -> Result<Arc<Self>> {
        let disk = block::Disk::open(path)?;
        Ok(Self::new(irqs, gsi, Profile::Block(disk)))
    }
    fn new(irqs: Irqs, gsi: usize, profile: Profile) -> Arc<Self> {
        assert!((16..24).contains(&gsi));
        Arc::new(Self {
            profile,
            state: Mutex::new(State::default()),
            wake: Condvar::new(),
            irqs,
            gsi,
            running: AtomicBool::new(false),
        })
    }
    /// Install a finite native-call schedule before the test driver starts the VM.
    #[cfg(feature = "testing")]
    pub fn test_control(&self, control: Arc<testing::Control>) -> Result<()> {
        if self.running.load(SeqCst) {
            return Err("test schedule installed after owner start".into());
        }
        let Profile::Block(disk) = &self.profile else {
            return Err("test schedule requires disk".into());
        };
        *disk.control.lock().unwrap_or_else(|e| e.into_inner()) = Some(control);
        Ok(())
    }
    /// Report `name` to an installed test schedule (block devices only).
    #[cfg(feature = "testing")]
    fn observe(&self, name: &str) {
        if let Profile::Block(disk) = &self.profile {
            if let Some(control) = &*disk.control.lock().unwrap_or_else(|e| e.into_inner()) {
                control.event(name);
            }
        }
    }
    pub fn device(self: &Arc<Self>) -> devices::Shared {
        Arc::new(Mutex::new(Mmio(self.clone())))
    }
    fn lock(&self) -> MutexGuard<'_, State> {
        self.state.lock().unwrap_or_else(|e| e.into_inner())
    }
    /// Drive the interrupt line from InterruptStatus (level-triggered).
    fn irq(&self, s: &State) {
        self.irqs.line(self.gsi, s.interrupt != 0);
    }
    fn busy(&self, s: &mut State) {
        if s.busy.is_none() {
            s.busy = Some(self.irqs.busy());
        }
    }
    /// Stop admission and cancel ungranted windows. A granted copy/backend call retains its
    /// storage until the owner returns. The caller must join `run` before destroying the VM.
    pub fn stop(&self) {
        let mut s = self.lock();
        s.stopped = true;
        s.notified = [false; 2];
        s.received = None;
        if let Some(p) = &s.permit {
            p.cancel();
        }
        self.wake.notify_all();
    }
    /// Close admission for power-off; true once the owner has drained.
    fn quiesce(&self) -> Result<bool> {
        let mut s = self.lock();
        s.quiescing = true;
        s.notified = [false; 2];
        s.received = None;
        self.wake.notify_all();
        Ok(s.drained)
    }
    /// Complete a closing reset once no affected request is active: advance the incarnation and
    /// clear what the reset clears.
    fn retire_reset(&self, s: &mut State) {
        let Some(reset) = s.closing else {
            return;
        };
        if s.active.is_some_and(|id| reset.affects(id)) {
            return;
        }
        s.closing = None;
        match reset {
            Reset::Device => {
                s.generation = s
                    .generation
                    .checked_add(1)
                    .expect("virtio generation exhausted");
                s.queues = [Queue::default(); 2];
                s.notified = [false; 2];
                s.cursor = 0;
                s.received = None;
                s.status = 0;
                s.features = 0;
                s.device_sel = 0;
                s.driver_sel = 0;
                s.queue_sel = 0;
                s.interrupt = 0;
            }
            Reset::Queue(i) => {
                s.generations[i] = s.generations[i]
                    .checked_add(1)
                    .expect("virtio queue generation exhausted");
                s.queues[i] = Queue {
                    ready: false,
                    next_avail: 0,
                    next_used: 0,
                    ..s.queues[i]
                };
                s.notified[i] = false;
                if i == 0 {
                    s.received = None;
                }
            }
        }
        self.irq(s);
        if !s.pending() && s.active.is_none() {
            s.busy = None;
        }
    }
    /// Accept a reset: cancel an affected request's ungranted copy and retire now if nothing is
    /// active, otherwise defer the access until the owner retires the request.
    fn reset(&self, s: &mut State, reset: Reset) -> Access {
        s.closing = Some(reset);
        match reset {
            Reset::Device => s.notified = [false; 2],
            Reset::Queue(i) => s.notified[i] = false,
        }
        if s.active.is_some_and(|id| reset.affects(id)) {
            if let Some(p) = &s.permit {
                p.cancel();
            }
        }
        self.retire_reset(s);
        self.wake.notify_all();
        if s.closing.is_some() {
            Access::Deferred(0)
        } else {
            Access::Completed(0)
        }
    }
    /// Set DEVICE_NEEDS_RESET, drop pending work and, once the driver is live, signal a
    /// configuration change.
    fn fail(&self, s: &mut State) {
        s.status |= status::NEEDS_RESET;
        s.notified = [false; 2];
        if s.status & status::DRIVER_OK != 0 {
            s.interrupt |= CONFIG_CHANGE;
        }
        self.irq(s);
    }
    /// One register access under the same lock as physical IRQ publication. Only reset/disable
    /// can defer here; the outer waiting queue orders conflicting accesses behind retirement.
    fn register(&self, s: &mut State, offset: u64, value: Option<u64>) -> Access {
        match value {
            None => Access::Completed(self.read_register(s, offset).into()),
            // The caller validated the full access width.
            Some(value) => self.write_register(s, offset, value as u32),
        }
    }
    fn read_register(&self, s: &State, offset: u64) -> u32 {
        let selected = s.queue_sel as usize;
        let valid_queue = selected < self.profile.queue_count();
        match offset {
            reg::MAGIC_VALUE => MAGIC,
            reg::VERSION => MMIO_VERSION,
            reg::DEVICE_ID => self.profile.device_id(),
            reg::VENDOR_ID => VENDOR,
            reg::DEVICE_FEATURES => feature_word(self.profile.features(), s.device_sel),
            reg::QUEUE_NUM_MAX if valid_queue => self.profile.queue_max().into(),
            reg::QUEUE_READY => u32::from(valid_queue && s.queues[selected].ready),
            reg::INTERRUPT_STATUS => s.interrupt,
            reg::STATUS => s.status,
            // Including ConfigGeneration: configuration never changes.
            _ => 0,
        }
    }
    fn write_register(&self, s: &mut State, offset: u64, value: u32) -> Access {
        // Poweroff already owns lifecycle ordering. Peer writes cannot cancel an accepted drain.
        if s.quiescing && offset != reg::INTERRUPT_ACK {
            return Access::Completed(0);
        }
        let selected = s.queue_sel as usize;
        let valid_queue = selected < self.profile.queue_count();
        match offset {
            reg::DEVICE_FEATURES_SEL => s.device_sel = value,
            reg::DRIVER_FEATURES_SEL => s.driver_sel = value,
            reg::DRIVER_FEATURES if s.status & status::FEATURES_OK == 0 && s.driver_sel < 2 => {
                set_half(&mut s.features, s.driver_sel == 1, value);
            }
            reg::QUEUE_SEL => s.queue_sel = value,
            reg::QUEUE_NUM
            | reg::QUEUE_DESC_LOW
            | reg::QUEUE_DESC_HIGH
            | reg::QUEUE_DRIVER_LOW
            | reg::QUEUE_DRIVER_HIGH
            | reg::QUEUE_DEVICE_LOW
            | reg::QUEUE_DEVICE_HIGH
                if valid_queue =>
            {
                return self.write_queue_layout(s, selected, offset, value);
            }
            reg::QUEUE_READY if valid_queue => return self.write_queue_ready(s, selected, value),
            reg::QUEUE_NOTIFY => self.notify(s, value as usize),
            reg::INTERRUPT_ACK => {
                s.interrupt &= !(value & (USED_BUFFER | CONFIG_CHANGE));
                self.irq(s);
            }
            reg::STATUS => return self.write_status(s, value),
            _ => {}
        }
        Access::Completed(0)
    }
    /// QueueNum or a ring address half. Refused while the queue is enabled or still owns a
    /// request; an invalid size fails the device.
    fn write_queue_layout(&self, s: &mut State, queue: usize, offset: u64, value: u32) -> Access {
        if s.queues[queue].ready || s.queue_active(queue) {
            return Access::Refused;
        }
        if offset == reg::QUEUE_NUM {
            let max = self.profile.queue_max().into();
            if value == 0 || value > max || !value.is_power_of_two() {
                self.fail(s);
            } else {
                s.queues[queue].size = value as u16;
            }
            return Access::Completed(0);
        }
        let q = &mut s.queues[queue];
        let (field, high) = match offset {
            reg::QUEUE_DESC_LOW => (&mut q.desc, false),
            reg::QUEUE_DESC_HIGH => (&mut q.desc, true),
            reg::QUEUE_DRIVER_LOW => (&mut q.avail, false),
            reg::QUEUE_DRIVER_HIGH => (&mut q.avail, true),
            reg::QUEUE_DEVICE_LOW => (&mut q.used, false),
            _ => (&mut q.used, true),
        };
        set_half(field, high, value);
        Access::Completed(0)
    }
    /// QueueReady: 0 resets the queue; 1 enables a well-formed queue after FEATURES_OK.
    fn write_queue_ready(&self, s: &mut State, queue: usize, value: u32) -> Access {
        if value == 0 {
            return self.reset(s, Reset::Queue(queue));
        }
        let valid = s.queues[queue].valid(self.profile.queue_max());
        if value != 1 || s.status & status::FEATURES_OK == 0 || !valid {
            self.fail(s);
        } else {
            s.queues[queue].ready = true;
        }
        Access::Completed(0)
    }
    /// QueueNotify: schedule `queue` if it accepts work. RX buffers alone are not work; packet
    /// arrival schedules the scan, even without a new kick.
    fn notify(&self, s: &mut State, queue: usize) {
        if queue >= self.profile.queue_count() || !s.accepting(queue) {
            return;
        }
        if self.profile.is_rx(queue) && s.received.is_none() {
            return;
        }
        s.notified[queue] = true;
        self.busy(s);
        self.wake.notify_one();
    }
    /// Status: 0 resets the device; anything else must follow the initialization order.
    fn write_status(&self, s: &mut State, value: u32) -> Access {
        if value == 0 {
            return self.reset(s, Reset::Device);
        }
        if !status_write_allowed(s.status, value) {
            self.fail(s);
            return Access::Completed(0);
        }
        s.status = value;
        // FEATURES_OK does not stick over an unacceptable feature set; the driver reads it back.
        if value & status::FEATURES_OK != 0 && !self.profile.accepts(s.features) {
            s.status &= !status::FEATURES_OK;
        }
        if value & status::FAILED != 0 {
            s.notified = [false; 2];
        }
        Access::Completed(0)
    }
    /// A read of device-specific configuration, or None outside it.
    fn config_access(&self, offset: u64, size: u64, value: Option<u64>) -> Option<Access> {
        let offset = offset.checked_sub(reg::CONFIG)?;
        let access = |config: &[u8]| {
            (offset < config.len() as u64).then(|| read_config(config, offset, size, value))
        };
        match &self.profile {
            Profile::Entropy => None,
            Profile::Block(disk) => access(&disk.config()),
            Profile::Network(network) => access(&network.config()),
        }
    }
    /// One guest MMIO access. Configuration reads complete at once. Register accesses complete,
    /// or wait in arrival order behind a closing reset (a vCPU then polls its token).
    fn access(&self, vcpu: usize, offset: u64, size: u64, value: Option<u64>) -> Access {
        if let Some(access) = self.config_access(offset, size, value) {
            return access;
        }
        if size != 4 || offset % 4 != 0 || value.is_some_and(|v| v > u32::MAX.into()) {
            return Access::Refused;
        }
        let mut s = self.lock();
        // Reserve the waiting slot before applying a deferred lifecycle effect: a refusal must
        // leave the device unchanged.
        if s.stopped || !s.can_wait(vcpu) {
            return Access::Refused;
        }
        let result = if s.closing.is_some() || !s.waiting.is_empty() {
            Access::Deferred(0)
        } else {
            self.register(&mut s, offset, value)
        };
        match result {
            Access::Deferred(_) => self.wait(&mut s, vcpu, offset, value),
            other => other,
        }
    }
    /// Queue a deferred access and hand out its token.
    fn wait(&self, s: &mut State, vcpu: usize, offset: u64, value: Option<u64>) -> Access {
        s.token += 1;
        let token = s.token;
        let applied = s.waiting.is_empty() && s.closing.is_some();
        s.waiting.push_back(Waiting {
            token,
            vcpu,
            offset,
            value,
            applied,
        });
        #[cfg(feature = "testing")]
        if applied && value == Some(0) {
            match offset {
                reg::STATUS => self.observe("reset-deferred"),
                reg::QUEUE_READY => self.observe("disable-deferred"),
                _ => {}
            }
        }
        Access::Deferred(token)
    }
    /// Poll a deferred access. It completes in arrival order once no reset is closing; an access
    /// that is itself a reset may defer again until that reset retires.
    fn poll(&self, vcpu: usize, token: u64) -> Access {
        let mut s = self.lock();
        let Some(w) = s
            .waiting
            .iter()
            .find(|w| w.token == token && w.vcpu == vcpu)
        else {
            return Access::Refused;
        };
        let (offset, value, applied) = (w.offset, w.value, w.applied);
        let first = s.waiting.front().is_some_and(|w| w.token == token);
        if s.closing.is_some() || !first {
            return Access::Deferred(token);
        }
        let result = if applied {
            Access::Completed(0)
        } else {
            self.register(&mut s, offset, value)
        };
        if matches!(result, Access::Deferred(_)) {
            s.waiting.front_mut().unwrap().applied = true;
            Access::Deferred(token)
        } else {
            s.waiting.pop_front();
            self.wake.notify_one();
            result
        }
    }
    fn current(&self, id: Identity) -> bool {
        self.lock().current(id)
    }
    /// Copy `spans` in one window while `id` is current. False when a reset or stop overtook the
    /// request before its grant; a granted copy always counts, even if a reset arrived meanwhile.
    fn copy(&self, guest: Guest<'_>, id: Identity, spans: &mut [Transfer<'_>]) -> Result<bool> {
        let permit = Arc::new(CopyPermit::new());
        {
            let mut s = self.lock();
            if !s.current(id) {
                return Ok(false);
            }
            s.permit = Some(permit.clone());
        }
        let copied = guest.backing.copy_guest_with_permit(
            guest.machine,
            guest.stops,
            Duration::from_secs(2),
            spans,
            &permit,
        );
        let mut s = self.lock();
        s.permit = None;
        match copied {
            // Even a reset that lost the grant must retire this committed effect.
            Ok(()) => Ok(true),
            Err(_) if !s.current(id) => Ok(false),
            Err(e) => Err(e),
        }
    }
    /// Copy `data` and the chain's used-ring publication in one window, then raise the used-buffer
    /// interrupt unless the driver suppressed it or a reset closed the incarnation (ACK, reset and
    /// level publication share the state lock). None when the window was canceled.
    ///
    /// With `refresh`, the same window then reads the queue's metadata into that snapshot, after
    /// the publication, so the next request needs no metadata window of its own. It counts as
    /// captured in the chain's incarnation only when the window was granted.
    fn complete(
        &self,
        guest: Guest<'_>,
        chain: &Chain<'_>,
        len: u32,
        data: Vec<Transfer<'_>>,
        mut refresh: Option<&mut Snapshot>,
    ) -> Result<Option<UsedEntry>> {
        let mut used = UsedEntry::new(chain, len);
        let mut transfers = data;
        used.publish(chain.queue, &mut transfers);
        if let Some(snapshot) = refresh.as_deref_mut() {
            transfers.extend(snapshot.reads(chain.queue));
        }
        if !self.copy(guest, chain.id, &mut transfers)? {
            return Ok(None);
        }
        drop(transfers);
        if let Some(snapshot) = refresh {
            snapshot.captured(chain.id);
        }
        #[cfg(feature = "testing")]
        self.observe("commit");
        let mut s = self.lock();
        if s.current(chain.id) && used.interrupt_wanted() {
            s.interrupt |= USED_BUFFER;
            self.irq(&s);
        }
        Ok(Some(used))
    }
    /// Run once on an independent scoped host thread. Cancellation/stop closes admission; the
    /// method joins normal disk shutdown before returning. Fatal disk timeout/cancellation may
    /// retain an owned-only lane until its syscall retires or the VM worker process exits.
    /// No returning path retains guest RAM authority. Errors cancel VM owners.
    pub fn run(
        &self,
        backing: &Backing,
        machine: &Machine,
        stops: &Stops,
        cancel: &AtomicBool,
    ) -> Result<()> {
        if self.running.swap(true, SeqCst) {
            return Err("virtio owner already started".into());
        }
        let guest = Guest {
            backing,
            machine,
            stops,
        };
        let result = self.owner(guest, cancel);
        if result.is_err() {
            crate::owners::cancel_owners(cancel);
        }
        let mut s = self.lock();
        s.stopped = !s.drained;
        s.active = None;
        s.permit = None;
        s.notified = [false; 2];
        s.busy = None;
        s.interrupt = 0;
        self.irq(&s);
        result
    }
    /// Power-off drain: a final full sync of the disk lane and its join, then a quiet device.
    /// A network backend failure cannot become a successful power-off.
    fn drain(&self, lane: Option<block::Lane>, cancel: &AtomicBool) -> Result<()> {
        if let Some(mut disk) = lane {
            disk.submit(block::Job::Flush)?;
            let done = disk.wait(|| cancel.load(SeqCst) || self.lock().stopped)?;
            if let Some(error) = done.error {
                return Err(format!("disk shutdown full-sync: {error}"));
            }
            disk.finish()?;
            #[cfg(feature = "testing")]
            self.observe("lane-joined");
        }
        let mut s = self.lock();
        if let Some(error) = &s.backend_error {
            return Err(format!("network shutdown: {error}"));
        }
        s.notified = [false; 2];
        s.busy = None;
        s.interrupt = 0;
        self.irq(&s);
        s.drained = true;
        Ok(())
    }
    fn owner(&self, guest: Guest<'_>, cancel: &AtomicBool) -> Result<()> {
        let mut snapshot = Snapshot::new(self.profile.queue_max())?;
        let mut lane = match &self.profile {
            Profile::Entropy | Profile::Network(_) => None,
            Profile::Block(disk) => Some(disk.start()?),
        };
        loop {
            let (queue, id, received) = match self.next_work(cancel)? {
                Work::Request {
                    queue,
                    id,
                    received,
                } => (queue, id, received),
                Work::Stop => return lane.map_or(Ok(()), block::Lane::finish),
                Work::Drain => return self.drain(lane, cancel),
            };
            let result = self.request(guest, &mut snapshot, queue, id, &mut lane, received, cancel);
            self.finish_request(result, &mut snapshot, queue, id)?;
        }
    }
    /// Wait until there is work, a drain, or a stop.
    fn next_work(&self, cancel: &AtomicBool) -> Result<Work> {
        let mut s = self.lock();
        loop {
            if s.stopped || cancel.load(SeqCst) {
                return Ok(Work::Stop);
            }
            if let Some(error) = &s.backend_error {
                return Err(format!("network backend: {error}"));
            }
            if s.quiescing {
                return Ok(Work::Drain);
            }
            if let Some(work) = self.admit(&mut s) {
                return Ok(work);
            }
            s = self
                .wake
                .wait_timeout(s, Duration::from_millis(10))
                .unwrap_or_else(|e| e.into_inner())
                .0;
        }
    }
    /// Take the next notified queue, round-robin, unless a reset or a deferred access must go
    /// first. The taken queue becomes active; RX takes the packet tagged with its incarnation.
    fn admit(&self, s: &mut State) -> Option<Work> {
        if s.closing.is_some() || !s.waiting.is_empty() {
            return None;
        }
        let count = self.profile.queue_count();
        let i = (0..count)
            .map(|n| (s.cursor + n) % count)
            .find(|&i| s.notified[i])?;
        s.notified[i] = false;
        let id = s.identity(i);
        s.active = Some(id);
        s.cursor = (i + 1) % count;
        let received = if i == 0 {
            s.received
                .take()
                .filter(|(tag, _)| *tag == id)
                .map(|(_, packet)| packet)
        } else {
            None
        };
        Some(Work::Request {
            queue: s.queues[i],
            id,
            received,
        })
    }
    /// Retire the active request: advance the ring indices or fail the device, then complete
    /// a reset waiting for it.
    fn finish_request(
        &self,
        result: Result<Request>,
        snapshot: &mut Snapshot,
        q: Queue,
        id: Identity,
    ) -> Result<()> {
        let mut s = self.lock();
        s.active = None;
        match result? {
            Request::Committed { pending } => self.retire_request(&mut s, snapshot, q, id, pending),
            Request::Invalid(reason) => {
                snapshot.discard();
                eprintln!("virtio: {reason}");
                if s.current(id) {
                    self.fail(&mut s);
                }
            }
            Request::Empty | Request::Canceled => snapshot.discard(),
        }
        self.retire_reset(&mut s);
        if !s.pending() {
            s.busy = None;
        }
        Ok(())
    }
    fn retire_request(
        &self,
        s: &mut State,
        snapshot: &Snapshot,
        q: Queue,
        id: Identity,
        pending: bool,
    ) {
        // A granted completion retires once even if reset arrived; reset clears its old queue afterward.
        s.queues[id.queue].next_avail = q.next_avail.wrapping_add(1);
        s.queues[id.queue].next_used = q.next_used.wrapping_add(1);
        if !self.profile.is_rx(id.queue) && s.current(id) && s.accepting(id.queue) {
            // Keep both notifications arriving after admission and already-owned heads even if
            // completion writes aliased the live available index. This does not admit another request.
            s.notified[id.queue] |= pending || snapshot.reusable(s.queues[id.queue], id);
        }
    }
    /// Read (or reuse) the queue's metadata, take the next chain and hand it to the device.
    fn request(
        &self,
        guest: Guest<'_>,
        snapshot: &mut Snapshot,
        q: Queue,
        id: Identity,
        lane: &mut Option<block::Lane>,
        received: Option<net::Packet>,
        cancel: &AtomicBool,
    ) -> Result<Request> {
        if self.profile.is_rx(id.queue) && received.is_none() {
            return Ok(Request::Empty);
        }
        if !q.valid(self.profile.queue_max()) {
            return Ok(Request::Invalid("invalid queue geometry".into()));
        }
        let ram = &guest.backing.ram;
        // Validation only: no raw pointer is dereferenced or retained by this device.
        if !q.rings_in(ram) {
            return Ok(Request::Invalid("queue metadata outside RAM".into()));
        }
        if !snapshot.reusable(q, id) && !self.capture(guest, snapshot, q, id)? {
            return Ok(Request::Canceled);
        }
        if !self.current(id) {
            return Ok(Request::Canceled);
        }
        let (head, descriptors) = match snapshot.chain(q) {
            Ok(Some(c)) => c,
            Ok(None) => return Ok(Request::Empty),
            Err(e) => return Ok(Request::Invalid(e)),
        };
        let outside_ram =
            |d: &Descriptor| d.len() != 0 && ram.range(d.addr().0, d.len().into()).is_none();
        if descriptors.iter().any(outside_ram) {
            return Ok(Request::Invalid("descriptor outside RAM".into()));
        }
        let chain = Chain {
            queue: q,
            id,
            head,
            descriptors: &descriptors,
        };
        match &self.profile {
            Profile::Block(disk) => {
                let lane = lane.as_mut().expect("a block owner starts its lane");
                self.block_request(guest, &chain, disk, lane, snapshot, cancel)
            }
            Profile::Network(network) => self.net_request(guest, &chain, network, received, cancel),
            Profile::Entropy => self.entropy_request(guest, &chain),
        }
    }
    /// Copy the descriptor table and available ring into `snapshot`. False if canceled.
    fn capture(
        &self,
        guest: Guest<'_>,
        snapshot: &mut Snapshot,
        q: Queue,
        id: Identity,
    ) -> Result<bool> {
        if !self.copy(guest, id, &mut snapshot.reads(q))? {
            return Ok(false);
        }
        snapshot.captured(id);
        Ok(true)
    }
    /// virtio-rng: fill every device-writable buffer, up to MAX_PAYLOAD bytes, from host entropy.
    fn entropy_request(&self, guest: Guest<'_>, chain: &Chain<'_>) -> Result<Request> {
        if chain.descriptors.iter().any(|d| !d.is_write_only()) {
            return Ok(Request::Invalid("entropy chain direction".into()));
        }
        let capacity: usize = chain.descriptors.iter().map(|d| d.len() as usize).sum();
        let len = capacity.min(MAX_PAYLOAD);
        if len == 0 {
            return Ok(Request::Invalid("empty entropy chain".into()));
        }
        if !self.current(chain.id) {
            return Ok(Request::Canceled);
        }
        // Host entropy fills only owned storage, outside the memory window and every lock.
        let mut payload = [0u8; MAX_PAYLOAD];
        let payload = &mut payload[..len];
        if let Err(error) = fill_entropy(payload) {
            return Ok(Request::Invalid(format!("getentropy: {error}")));
        }
        let writes = scatter(chain.descriptors, payload);
        Ok(
            match self.complete(guest, chain, len as u32, writes, None)? {
                Some(_) => Request::Committed { pending: true },
                None => Request::Canceled,
            },
        )
    }
}

/// The MMIO registry's view of a device: register accesses only, never guest memory.
struct Mmio(Arc<Virtio>);
impl devices::Device for Mmio {
    fn quiesce(&mut self) -> Result<bool> {
        self.0.quiesce()
    }
    fn read(&mut self, _: usize, _: u64, _: u64) -> Option<u64> {
        None
    }
    fn write(&mut self, _: usize, _: u64, _: u64, _: u64) -> bool {
        false
    }
    fn access(&mut self, v: usize, o: u64, n: u64, value: Option<u64>) -> Access {
        self.0.access(v, o, n, value)
    }
    fn poll(&mut self, v: usize, token: u64) -> Access {
        self.0.poll(v, token)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    pub(super) fn device() -> Arc<Virtio> {
        let memory = Arc::new(crate::memory::Memory::with_size(4096).unwrap());
        let mut devices = devices::Devices::new(64 << 20);
        let uart = Arc::new(Mutex::new(crate::uart::Uart::new()));
        let irqs = crate::irq::install(&mut devices, &memory, 0, &uart).unwrap();
        Virtio::rng(irqs, RNG_RESOURCE.gsi as usize)
    }
    fn write(r: &Virtio, offset: u64, value: u64) {
        assert!(matches!(
            r.access(0, offset, 4, Some(value)),
            Access::Completed(0)
        ));
    }
    fn read(r: &Virtio, offset: u64) -> u64 {
        match r.access(0, offset, 4, None) {
            Access::Completed(v) => v,
            _ => panic!("register read not complete"),
        }
    }
    fn negotiated(r: &Virtio) {
        write(r, reg::STATUS, 1);
        write(r, reg::STATUS, 3);
        write(r, reg::DRIVER_FEATURES_SEL, 1);
        write(r, reg::DRIVER_FEATURES, 1);
        write(r, reg::STATUS, 11);
        assert_eq!(read(r, reg::STATUS), 11);
    }
    #[test]
    fn completion_preserves_later_notifications_and_captured_work_across_wrap() {
        for wrap in [false, true] {
            for pending in [false, true] {
                for notify in [false, true] {
                    for captured in [false, true] {
                        let r = device();
                        let q = Queue {
                            size: 8,
                            ready: true,
                            next_avail: if wrap { u16::MAX } else { 3 },
                            next_used: 17,
                            ..Default::default()
                        };
                        let id = {
                            let mut s = r.lock();
                            s.status = status::DRIVER_OK;
                            s.queues[0] = q;
                            s.notified[1] = true;
                            s.identity(0)
                        };
                        let mut snapshot = Snapshot::new(8).unwrap();
                        snapshot.avail[2..4].copy_from_slice(
                            &q.next_avail
                                .wrapping_add(1 + u16::from(captured))
                                .to_le_bytes(),
                        );
                        snapshot.captured(id);
                        // A real QueueNotify after completion's observation must survive owner retirement.
                        if notify {
                            write(&r, reg::QUEUE_NOTIFY, 0);
                        }
                        let mut s = r.lock();
                        r.retire_request(&mut s, &snapshot, q, id, pending);
                        assert_eq!(s.notified, [pending || notify || captured, true]);
                        assert_eq!(s.queues[0].next_avail, q.next_avail.wrapping_add(1));
                        assert_eq!(s.queues[0].next_used, 18);
                    }
                }
            }
        }
    }
    #[test]
    fn completion_rearm_obeys_lifecycle_and_snapshot_identity() {
        for gate in 0..7 {
            let r = device();
            let q = Queue {
                size: 8,
                ready: true,
                ..Default::default()
            };
            let mut s = r.lock();
            s.status = status::DRIVER_OK;
            s.queues[0] = q;
            let id = s.identity(0);
            let mut snapshot = Snapshot::new(8).unwrap();
            snapshot.avail[2] = 2;
            snapshot.captured(id);
            match gate {
                0 => s.quiescing = true,
                1 => s.closing = Some(Reset::Device),
                2 => s.closing = Some(Reset::Queue(0)),
                3 => s.stopped = true,
                4 => s.queues[0].ready = false,
                5 => s.status |= status::NEEDS_RESET,
                _ => s.generation += 1,
            }
            r.retire_request(&mut s, &snapshot, q, id, true);
            assert!(!s.notified[0]);
            assert_eq!((s.queues[0].next_avail, s.queues[0].next_used), (1, 1));
        }
        let r = device();
        let q = Queue {
            size: 8,
            ready: true,
            ..Default::default()
        };
        let mut s = r.lock();
        s.status = status::DRIVER_OK;
        s.queues[0] = q;
        let id = s.identity(0);
        let mut snapshot = Snapshot::new(8).unwrap();
        snapshot.avail[2] = 2;
        snapshot.captured(Identity {
            device: id.device + 1,
            ..id
        });
        r.retire_request(&mut s, &snapshot, q, id, false);
        assert!(!s.notified[0]);
    }
    #[test]
    fn power_quiesce_preserves_accepted_work_and_ignores_later_reset_notify() {
        let r = device();
        negotiated(&r);
        {
            let mut s = r.lock();
            s.active = Some(s.identity(0));
            s.notified[0] = true;
            s.queues[0].ready = true;
            s.status |= status::DRIVER_OK;
        }
        assert!(!r.quiesce().unwrap());
        write(&r, reg::QUEUE_NOTIFY, 0);
        write(&r, reg::STATUS, 0);
        write(&r, reg::QUEUE_READY, 0);
        let s = r.lock();
        assert!(s.active.is_some() && s.queues[0].ready && s.quiescing);
        assert!(!s.pending() && s.closing.is_none() && !s.drained && !s.stopped);
        assert_ne!(s.status, 0);
    }
    #[test]
    fn full_selectors_and_feature_negotiation_do_not_truncate() {
        let r = device();
        write(&r, reg::DEVICE_FEATURES_SEL, 0x1_0000);
        assert_eq!(read(&r, reg::DEVICE_FEATURES), 0);
        write(&r, reg::DEVICE_FEATURES_SEL, 1);
        assert_eq!(read(&r, reg::DEVICE_FEATURES), 1);
        write(&r, reg::QUEUE_SEL, 0x1_0000);
        assert_eq!(read(&r, reg::QUEUE_NUM_MAX), 0);
        write(&r, reg::STATUS, 3);
        write(&r, reg::STATUS, 11);
        assert_eq!(read(&r, reg::STATUS), 3); // missing mandatory VERSION_1
        negotiated(&device());
        assert!(matches!(
            r.access(0, reg::STATUS, 8, Some(0)),
            Access::Refused
        ));
        assert_eq!(read(&r, reg::STATUS), 3); // refusal made no reset effect
    }
    #[test]
    fn queue_geometry_fails_device_without_scheduling_work() {
        let r = device();
        negotiated(&r);
        write(&r, reg::QUEUE_NUM, 65544); // would truncate to an apparently valid eight-entry queue
        assert_ne!(read(&r, reg::STATUS) & u64::from(status::NEEDS_RESET), 0);
        assert!(!r.lock().pending());
        write(&r, reg::STATUS, 0);
        negotiated(&r);
        write(&r, reg::QUEUE_NUM, 8);
        write(&r, reg::QUEUE_DESC_LOW, 1);
        write(&r, reg::QUEUE_READY, 1);
        assert_eq!(read(&r, reg::QUEUE_READY), 0);
        assert_ne!(read(&r, reg::STATUS) & u64::from(status::NEEDS_RESET), 0);
    }
    #[test]
    fn reset_cancels_pending_copy_and_orders_conflicting_read() {
        let r = device();
        negotiated(&r);
        let permit = Arc::new(CopyPermit::new());
        {
            let mut s = r.lock();
            s.active = Some(s.identity(0));
            s.queues[0].ready = true;
            s.permit = Some(permit.clone());
        }
        let token = match r.access(0, reg::STATUS, 4, Some(0)) {
            Access::Deferred(t) => t,
            _ => panic!("reset did not defer"),
        };
        assert!(permit.cancel()); // reset already won the pending permit
        let read_token = match r.access(1, reg::STATUS, 4, None) {
            Access::Deferred(t) => t,
            _ => panic!("conflicting read passed reset"),
        };
        assert!(matches!(r.poll(0, token), Access::Deferred(t) if t == token));
        {
            let mut s = r.lock();
            s.active = None;
            s.permit = None;
            r.retire_reset(&mut s);
        }
        assert!(matches!(r.poll(1, read_token), Access::Deferred(_))); // FIFO: original access retires first
        assert!(matches!(r.poll(0, token), Access::Completed(0)));
        assert!(matches!(r.poll(1, read_token), Access::Completed(0)));
        assert!(matches!(r.poll(0, token), Access::Refused)); // no replay
        assert_eq!(r.lock().generation, 1);
    }
    #[test]
    fn queue_disable_readback_and_ack_share_serialized_state() {
        let r = device();
        negotiated(&r);
        write(&r, reg::QUEUE_NUM, 8);
        write(&r, reg::QUEUE_DESC_LOW, 0x1000);
        write(&r, reg::QUEUE_DRIVER_LOW, 0x2000);
        write(&r, reg::QUEUE_DEVICE_LOW, 0x3000);
        write(&r, reg::QUEUE_READY, 1);
        assert_eq!(read(&r, reg::QUEUE_READY), 1);
        write(&r, reg::QUEUE_READY, 0);
        assert_eq!(read(&r, reg::QUEUE_READY), 0);
        assert_eq!(r.lock().queues[0].desc, 0x1000);
        {
            let mut s = r.lock();
            s.interrupt = 3;
            r.irq(&s);
        }
        write(&r, reg::INTERRUPT_ACK, 1);
        assert_eq!(read(&r, reg::INTERRUPT_STATUS), 2);
        write(&r, reg::INTERRUPT_ACK, 2);
        assert_eq!(read(&r, reg::INTERRUPT_STATUS), 0);
    }
}
