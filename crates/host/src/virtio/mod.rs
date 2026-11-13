//! Modern virtio-MMIO devices (split rings; several block requests in flight, one entropy or
//! network request). Every guest buffer is copied into owned storage inside a short copy window
//! ([`crate::execution::io`]); no backend call holds a window or a guest address.
//!
//! Lifecycle: create the device before boot, run its owner ([`Virtio::run`]) on its own thread
//! alongside the vCPU owners, call [`Virtio::stop`], and join the owner before detaching IRQs or
//! destroying the VM.
mod block;
mod net;
mod profile;
mod queue;
mod slirp;
pub use net::{Frame, Gso, Offloads, PacketPort, RxChecksum, RxMeta, TxMeta};
pub use slirp::Slirp;
#[cfg(feature = "testing")]
pub mod testing;
use crate::{
    Result,
    boot::acpi::VirtioMmio,
    devices::{self, Access},
    execution::{
        Backing, Stops,
        io::{CopyPermit, MAX_BYTES, MAX_SPANS, Transfer},
    },
    irq::{Busy, Irqs},
    vm::Machine,
};
use natemu_abi::guest_memory::GuestRam;
use queue::{Descriptor, Queue, Snapshot};
use std::{
    collections::VecDeque,
    ops::Range,
    sync::{
        Arc, Condvar, Mutex, MutexGuard,
        atomic::{
            AtomicBool, AtomicU64,
            Ordering::{Relaxed, SeqCst},
        },
    },
    time::{Duration, Instant},
};

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
/// VIRTIO_F_EVENT_IDX (virtio 1.2 §2.7.10): the used_event/avail_event fields after each ring replace the
/// flags below. Offered by the network device only.
const EVENT_IDX: u64 = 1 << 29;
/// Network: at most this many completions, or this long, share one held-back interrupt.
const COALESCE: u32 = 32;
const COALESCE_TIME: Duration = Duration::from_micros(100);
/// VIRTQ_AVAIL_F_NO_INTERRUPT in the available ring's flags.
const AVAIL_NO_INTERRUPT: u16 = 1;
/// VIRTQ_USED_F_NO_NOTIFY in the used ring's flags (virtio 1.2 §2.7.7: without EVENT_IDX a driver checks it
/// before each notification).
const USED_NO_NOTIFY: u16 = 1;
/// A queue's notification gate ([`Virtio::gates`]): bit 0, a QueueNotify would take effect now and may skip the state
/// lock; bit 1, pending work is dropped now (the queue is closed or being reset); the rest, an epoch advanced each time
/// it starts dropping work.
const GATE_OPEN: u64 = 1;
const GATE_KILLED: u64 = 2;
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
    /// Network: used-buffer interrupts held back while the owner keeps working, and since when
    /// (raised when it goes idle, after [`COALESCE`] of them or [`COALESCE_TIME`], or when RX starves).
    owed: u32,
    owed_at: Option<Instant>,
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
    /// The incarnation of every admitted request not yet retired (block: up to its in-flight
    /// bound; entropy and network: at most one). A reset retires once none of its own remain.
    in_flight: Vec<Identity>,
    /// Where the owner's round-robin scan of `notified` starts.
    cursor: usize,
    /// Power-off asked the device to drain; guest writes other than InterruptACK are ignored.
    quiescing: bool,
    /// The drain finished successfully.
    drained: bool,
    /// An accepted reset waiting for its in-flight requests and open window to retire.
    closing: Option<Reset>,
    /// Admission closed for good (the VM is ending).
    stopped: bool,
    /// The owner's pending or open copy window and the incarnation it copies for; reset and
    /// stop cancel it.
    permit: Option<(Identity, Arc<CopyPermit>)>,
    /// Deferred register accesses, answered in arrival order.
    waiting: VecDeque<Waiting>,
    /// The last deferred-access token issued.
    token: u64,
    /// Keeps the VM from ending as all-halted while work is pending.
    busy: Option<Busy>,
    /// One received packet waiting for an RX buffer, tagged with the RX queue's incarnation.
    received: VecDeque<(Identity, net::Packet)>,
    /// RX found no buffer: hold the received frames until the driver notifies RX.
    rx_starved: bool,
    /// The driver notified RX since the current RX request was admitted: that request cannot starve.
    rx_kicked: bool,
    backend_error: Option<String>,
    /// Block: the incarnation whose used ring holds VIRTQ_USED_F_NO_NOTIFY (the owner's kick suppression). A reset
    /// waits for the owner to clear it, so no incarnation ends with its driver's kicks suppressed.
    suppressed: Option<Identity>,
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
    /// The owner holds a request from `queue`.
    fn queue_active(&self, queue: usize) -> bool {
        self.in_flight.iter().any(|id| id.queue == queue)
    }
    /// A request or the open copy window still belongs to an incarnation `reset` closes.
    fn holds(&self, reset: Reset) -> bool {
        self.in_flight.iter().any(|&id| reset.affects(id))
            || self.suppressed.is_some_and(|id| reset.affects(id))
            || self
                .permit
                .as_ref()
                .is_some_and(|(id, _)| reset.affects(*id))
    }
    /// Forget one retired request of incarnation `id`.
    fn retire(&mut self, id: Identity) {
        let i = self.in_flight.iter().position(|x| *x == id);
        self.in_flight
            .swap_remove(i.expect("retired request was in flight"));
    }
    /// The owner may take new requests: no reset closing and no register access waiting.
    fn admission_open(&self) -> bool {
        self.closing.is_none() && self.waiting.is_empty()
    }
    /// Another access may wait: each vCPU has at most one outstanding access, the queue is
    /// bounded, and tokens never wrap.
    fn can_wait(&self, vcpu: usize) -> bool {
        self.waiting.len() < natemu_abi::execution::cache::MAX_VCPUS
            && !self.waiting.iter().any(|w| w.vcpu == vcpu)
            && self.token != u64::MAX
    }
    /// A notification of `queue` is dropped now, as every earlier one not yet admitted: the queue does not accept work,
    /// the device stopped, or a reset of the queue is closing.
    fn kills(&self, queue: usize) -> bool {
        self.stopped
            || !self.accepting(queue)
            || self
                .closing
                .is_some_and(|r| r.affects(self.identity(queue)))
    }
}

/// The device state held under its lock; dropping it republishes the notification gates from the state left behind.
struct Locked<'a> {
    guard: MutexGuard<'a, State>,
    device: &'a Virtio,
}
impl std::ops::Deref for Locked<'_> {
    type Target = State;
    fn deref(&self) -> &State {
        &self.guard
    }
}
impl std::ops::DerefMut for Locked<'_> {
    fn deref_mut(&mut self) -> &mut State {
        &mut self.guard
    }
}
impl Drop for Locked<'_> {
    fn drop(&mut self) {
        self.device.publish_gates(&self.guard);
    }
}

/// A production virtio device with up to two queues. Clones share one register/IRQ ordering
/// domain; exactly one independent owner calls `run`. The MMIO adapter retains no guest-memory
/// authority.
pub struct Virtio {
    profile: Profile,
    state: Mutex<State>,
    wake: Arc<Bell>,
    irqs: Irqs,
    gsi: usize,
    running: AtomicBool,
    /// Per queue: its notification gate (`GATE_*`), written only under the state lock as each holder releases it.
    gates: [AtomicU64; 2],
    /// Per queue: a QueueNotify taken without the state lock, as the open gate it saw (0: none). The owner moves it to
    /// `notified` under the lock if the gate's epoch has not moved since; a nonzero kick holds one [`Irqs::busy`] count.
    kicks: [AtomicU64; 2],
    /// The features the driver accepted, once FEATURES_OK holds (0 otherwise, and after a reset):
    /// read lock-free by the owner (EVENT_IDX, network checksum offloads).
    accepted: AtomicU64,
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
            Self::Network(network) => network.features(),
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
        features & !self.features() == 0
            && features & self.required() == self.required()
            && (!matches!(self, Self::Network(_)) || net::dependencies_met(features))
    }
    /// The network RX queue: its buffers alone are not work; a packet arrival is.
    fn is_rx(&self, queue: usize) -> bool {
        matches!(self, Self::Network(_)) && queue == 0
    }
}

/// The owner's one wake source: guest notifications, resets, power drain, stop and disk
/// workers ring it. A ring between the owner's check and its wait is not lost. One waiter.
#[derive(Default)]
pub(super) struct Bell {
    rung: AtomicBool,
    /// The waiter is (about to be) blocked: a ring takes the lock and signals. Dekker with `rung` (both `SeqCst`): a
    /// ringer that sees it clear stored `rung` before the waiter's check under the lock.
    sleeping: AtomicBool,
    lock: Mutex<()>,
    signal: Condvar,
}
impl Bell {
    pub fn ring(&self) {
        self.rung.store(true, SeqCst);
        if self.sleeping.load(SeqCst) {
            drop(self.lock.lock().unwrap_or_else(|e| e.into_inner()));
            self.signal.notify_one();
        }
    }
    /// Wait for a ring (at most `timeout`) and consume it; false if none came.
    pub fn wait(&self, timeout: Duration) -> bool {
        self.wait_spinning(timeout, Duration::ZERO)
    }
    /// [`Self::wait`], first spinning up to `spin` (counted in `timeout`) without blocking.
    pub fn wait_spinning(&self, timeout: Duration, spin: Duration) -> bool {
        if self.rung.swap(false, SeqCst) {
            return true;
        }
        if !spin.is_zero() {
            let until = Instant::now() + spin;
            while Instant::now() < until {
                if self.rung.load(Relaxed) && self.rung.swap(false, SeqCst) {
                    return true;
                }
                std::hint::spin_loop();
            }
        }
        let guard = self.lock.lock().unwrap_or_else(|e| e.into_inner());
        self.sleeping.store(true, SeqCst);
        let (guard, _) = self
            .signal
            .wait_timeout_while(guard, timeout.saturating_sub(spin), |_| {
                !self.rung.load(SeqCst)
            })
            .unwrap_or_else(|e| e.into_inner());
        self.sleeping.store(false, SeqCst);
        drop(guard);
        self.rung.swap(false, SeqCst)
    }
}

/// What a request needs to open copy windows on the running VM.
#[derive(Clone, Copy)]
struct Guest<'a> {
    backing: &'a Backing,
    copier: Copier<'a>,
}
/// How a copy reaches guest RAM: the running VM's windowless copy, or (unit tests) a scripted one.
#[derive(Clone, Copy)]
enum Copier<'a> {
    Vm {
        machine: &'a Machine,
        stops: &'a Stops,
    },
    #[cfg(test)]
    Test(&'a TestCopy<'a>),
}
#[cfg(test)]
type TestCopy<'a> = dyn Fn(&mut [Transfer<'_>], usize, &CopyPermit) -> Result<()> + Sync + 'a;

/// One admitted descriptor chain: its queue's registers at admission, incarnation and head.
struct Chain<'c> {
    queue: Queue,
    id: Identity,
    head: u16,
    descriptors: &'c [Descriptor],
}

/// The used-ring publication of completed chains: their elements in consecutive used slots
/// from `next_used`, one used index after them all, and room to read the available ring's
/// flags and index back after publishing them.
struct Used {
    elements: Vec<[u8; 8]>,
    index: [u8; 2],
    avail: [u8; 4],
    /// The used index before these elements.
    old: u16,
    /// With EVENT_IDX: the avail_event to write, and the driver's used_event read back.
    event: Option<([u8; 2], [u8; 2])>,
}
impl Used {
    /// `chains` are `(head, used length)` pairs in the order they take used slots.
    fn new(q: Queue, chains: impl IntoIterator<Item = (u16, u32)>) -> Self {
        let elements: Vec<[u8; 8]> = chains
            .into_iter()
            .map(|(head, len)| {
                let mut element = [0; 8];
                element[..4].copy_from_slice(&u32::from(head).to_le_bytes());
                element[4..].copy_from_slice(&len.to_le_bytes());
                element
            })
            .collect();
        Self {
            index: q
                .next_used
                .wrapping_add(elements.len() as u16)
                .to_le_bytes(),
            elements,
            avail: [0; 4],
            old: q.next_used,
            event: None,
        }
    }
    /// EVENT_IDX: also write `avail_event` (the available index whose addition the driver must
    /// notify) and read the driver's used_event back for [`Used::interrupt_wanted`].
    fn with_events(mut self, avail_event: u16) -> Self {
        self.event = Some((avail_event.to_le_bytes(), [0; 2]));
        self
    }
    /// The number of chains published.
    fn count(&self) -> u16 {
        self.elements.len() as u16
    }
    /// Append the elements, then the used index, then a read of the available ring's flags and
    /// index. Reading after publication sees suppression and newly published work even when a
    /// guest buffer aliases ring metadata. Returns the used index's position in `transfers`: the
    /// start of the publication a windowless copy holds back until posted vCPUs have drained.
    fn publish<'a>(&'a mut self, q: Queue, transfers: &mut Vec<Transfer<'a>>) -> usize {
        let Self {
            elements,
            index,
            avail,
            event,
            ..
        } = self;
        for (i, element) in elements.iter().enumerate() {
            let slot = q.next_used.wrapping_add(i as u16) % q.size;
            transfers.push(Transfer::Write {
                gpa: q.used + 4 + u64::from(slot) * 8,
                bytes: element,
            });
        }
        let size = u64::from(q.size);
        let (avail_event, used_event) = match event {
            Some((write, read)) => (Some(&*write), Some(read)),
            None => (None, None),
        };
        if let Some(avail_event) = avail_event {
            transfers.push(Transfer::Write {
                gpa: q.used + 4 + size * 8,
                bytes: avail_event,
            });
        }
        let publish = transfers.len();
        transfers.push(Transfer::Write {
            gpa: q.used + 2,
            bytes: index,
        });
        transfers.push(Transfer::Read {
            gpa: q.avail,
            bytes: avail,
        });
        if let Some(used_event) = used_event {
            transfers.push(Transfer::Read {
                gpa: q.avail + 4 + size * 2,
                bytes: used_event,
            });
        }
        publish
    }
    /// Without EVENT_IDX the driver's NO_INTERRUPT flag; with it, whether these elements moved the
    /// used index past used_event (vring_need_event, virtio 1.2 §2.7.10).
    fn interrupt_wanted(&self) -> bool {
        match self.event {
            Some((_, used_event)) => {
                let (new, event) = (
                    u16::from_le_bytes(self.index),
                    u16::from_le_bytes(used_event),
                );
                new.wrapping_sub(event).wrapping_sub(1) < new.wrapping_sub(self.old)
            }
            None => u16::from_le_bytes([self.avail[0], self.avail[1]]) & AVAIL_NO_INTERRUPT == 0,
        }
    }
    /// The driver's available index, read after publication.
    fn avail_index(&self) -> u16 {
        u16::from_le_bytes([self.avail[2], self.avail[3]])
    }
}

/// One request's (or one window's fixed) share of a copy window's bounds.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Default)]
struct Cost {
    spans: usize,
    bytes: usize,
}
impl Cost {
    fn of(transfers: &[Transfer<'_>]) -> Self {
        let bytes = transfers.iter().map(|t| match t {
            Transfer::Read { bytes, .. } => bytes.len(),
            Transfer::Write { bytes, .. } => bytes.len(),
        });
        Self {
            spans: transfers.len(),
            bytes: bytes.sum(),
        }
    }
    fn plus(self, other: Self) -> Self {
        Self {
            spans: self.spans + other.spans,
            bytes: self.bytes + other.bytes,
        }
    }
    /// Within one window's span and byte bounds.
    fn fits(self) -> bool {
        self.spans <= MAX_SPANS && self.bytes <= MAX_BYTES
    }
}

/// Split consecutive requests into as few copy windows as the window bounds allow, each window
/// holding whole requests plus `overhead`. A request that does not fit alone still gets its own
/// window (its copy then fails); callers bound each request well below the window bounds.
fn pack(costs: &[Cost], overhead: Cost) -> Vec<Range<usize>> {
    let mut windows = Vec::new();
    let (mut start, mut total) = (0, overhead);
    for (i, cost) in costs.iter().enumerate() {
        if i > start && !total.plus(*cost).fits() {
            windows.push(start..i);
            (start, total) = (i, overhead);
        }
        total = total.plus(*cost);
    }
    if start < costs.len() {
        windows.push(start..costs.len());
    }
    windows
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
                gpa: d.addr(),
                bytes: &bytes[offset..offset + count],
            });
            offset += count;
        }
    }
    writes
}

/// Every nonempty descriptor lies wholly in guest RAM (checked only; nothing is dereferenced).
fn descriptors_in(ram: &GuestRam, descriptors: &[Descriptor]) -> bool {
    descriptors
        .iter()
        .all(|d| d.len() == 0 || ram.range(d.addr(), d.len().into()).is_some())
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
    /// `count` used elements were published (several for one mergeable RX frame). `pending` asks
    /// the owner to look at the queue again.
    Committed { pending: bool, count: u16 },
    /// RX: no buffer was posted. The frame goes back to the front of the queue; `posted` if a
    /// buffer appeared after the device asked to be notified of one.
    Starved { packet: net::Packet, posted: bool },
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
            wake: Arc::default(),
            irqs,
            gsi,
            running: AtomicBool::new(false),
            gates: Default::default(),
            kicks: Default::default(),
            accepted: AtomicU64::new(0),
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
    fn lock(&self) -> Locked<'_> {
        Locked {
            guard: self.state.lock().unwrap_or_else(|e| e.into_inner()),
            device: self,
        }
    }
    /// Republish each queue's gate from `s` (under its lock): open when a QueueNotify would be applied at once; a new
    /// epoch when it starts dropping work, so a kick that saw an older gate is dropped as a notification before it was.
    fn publish_gates(&self, s: &State) {
        for (queue, gate) in self.gates.iter().enumerate() {
            let killed =
                queue >= self.profile.queue_count() || self.profile.is_rx(queue) || s.kills(queue);
            let open = !killed && s.admission_open();
            let old = gate.load(SeqCst);
            let mut epoch = old >> 2;
            if killed && old & GATE_KILLED == 0 {
                epoch += 1;
            }
            let new = epoch << 2 | if killed { GATE_KILLED } else { 0 } | u64::from(open);
            if new != old {
                gate.store(new, SeqCst);
            }
        }
    }
    /// QueueNotify without the state lock, when the gate is open: record the kick and ring the owner. False: take the
    /// locked path. The RX queue never opens (a packet arrival, not a kick, is its work).
    fn kick(&self, queue: u64) -> bool {
        let Some(q) = usize::try_from(queue).ok().filter(|&q| q < 2) else {
            return false;
        };
        let gate = self.gates[q].load(SeqCst);
        if gate & GATE_OPEN == 0 {
            return false;
        }
        self.record_kick(q, gate);
        true
    }
    /// The second half of [`Self::kick`], after it saw `gate` open (a reset may have overtaken it since).
    fn record_kick(&self, q: usize, gate: u64) {
        profile::notified();
        // Keep the VM from ending as all-halted until the owner takes the kick; one count per recorded kick, taken
        // before the kick is visible. The newest epoch wins (an older kick it replaces would be dropped anyway).
        self.irqs.hold();
        if self.kicks[q].fetch_max(gate, SeqCst) != 0 {
            self.irqs.release();
        }
        // The queue dropped work since the gate was read (possibly after the owner's last look, e.g. at power-off):
        // withdraw this now-stale kick unless another replaced it, so its count does not outlive the owner.
        if self.gates[q].load(SeqCst) >> 2 != gate >> 2
            && self.kicks[q]
                .compare_exchange(gate, 0, SeqCst, SeqCst)
                .is_ok()
        {
            self.irqs.release();
        }
        self.wake.ring();
    }
    /// Move lock-free kicks into `notified` (under the lock), unless their queue dropped work since they were made.
    fn take_kicks(&self, s: &mut State) {
        for q in 0..2 {
            let kick = self.kicks[q].swap(0, SeqCst);
            if kick == 0 {
                continue;
            }
            if kick >> 2 == self.gates[q].load(SeqCst) >> 2 && !s.kills(q) {
                // RX's gate never opens (`publish_gates`): its notifications take `notify`.
                debug_assert!(!self.profile.is_rx(q));
                s.notified[q] = true;
                self.busy(s);
            }
            self.irqs.release();
        }
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
        self.take_kicks(&mut s);
        s.notified = [false; 2];
        s.received.clear();
        s.rx_starved = false;
        if let Some((_, p)) = &s.permit {
            p.cancel();
        }
        self.wake.ring();
        self.backend_ready();
    }
    /// Close admission for power-off; true once the owner has drained.
    fn quiesce(&self) -> Result<bool> {
        let mut s = self.lock();
        s.quiescing = true;
        s.notified = [false; 2];
        s.received.clear();
        s.rx_starved = false;
        self.wake.ring();
        Ok(s.drained)
    }
    /// Complete a closing reset once none of its requests or windows remain: advance the
    /// incarnation and clear what the reset clears.
    fn retire_reset(&self, s: &mut State) {
        let Some(reset) = s.closing else {
            return;
        };
        if s.holds(reset) {
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
                s.received.clear();
                s.rx_starved = false;
                s.status = 0;
                s.features = 0;
                self.accepted.store(0, SeqCst);
                s.owed = 0;
                s.owed_at = None;
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
                    s.received.clear();
                    s.rx_starved = false;
                }
            }
        }
        self.irq(s);
        self.settle(s);
    }
    /// Release the busy guard once no work is notified and nothing is in flight.
    fn settle(&self, s: &mut State) {
        if !s.pending() && s.in_flight.is_empty() {
            s.busy = None;
        }
    }
    /// Accept a reset: cancel an affected ungranted copy and retire now if nothing affected is in
    /// flight, otherwise defer the access until the owner retires those requests.
    fn reset(&self, s: &mut State, reset: Reset) -> Access {
        s.closing = Some(reset);
        match reset {
            Reset::Device => s.notified = [false; 2],
            Reset::Queue(i) => s.notified[i] = false,
        }
        if let Some((id, p)) = &s.permit {
            if reset.affects(*id) {
                p.cancel();
            }
        }
        self.retire_reset(s);
        self.wake.ring();
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
            reg::INTERRUPT_STATUS => {
                profile::count(profile::STATUS_READS);
                s.interrupt
            }
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
                profile::acked();
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
            if self.profile.is_rx(queue) {
                self.backend_ready(); // frames the backend holds may be taken now
            }
        }
        Access::Completed(0)
    }
    /// QueueNotify: schedule `queue` if it accepts work. RX buffers alone are not work; packet
    /// arrival schedules the scan, even without a new kick.
    fn notify(&self, s: &mut State, queue: usize) {
        if queue >= self.profile.queue_count() || !s.accepting(queue) {
            return;
        }
        if self.profile.is_rx(queue) {
            s.rx_starved = false;
            s.rx_kicked = true;
        }
        if self.profile.is_rx(queue) && s.received.is_empty() {
            return;
        }
        s.notified[queue] = true;
        profile::notified();
        self.busy(s);
        self.wake.ring();
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
        if value & status::DRIVER_OK != 0 {
            self.backend_ready();
        }
        // FEATURES_OK does not stick over an unacceptable feature set; the driver reads it back.
        if value & status::FEATURES_OK != 0 && !self.profile.accepts(s.features) {
            s.status &= !status::FEATURES_OK;
        }
        let accepted = if s.status & status::FEATURES_OK != 0 {
            s.features
        } else {
            0
        };
        self.accepted.store(accepted, SeqCst);
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
        if offset == reg::QUEUE_NOTIFY && value.is_some_and(|q| self.kick(q)) {
            return Access::Completed(0);
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
            self.wake.ring();
            result
        }
    }
    fn current(&self, id: Identity) -> bool {
        self.lock().current(id)
    }
    /// Copy `spans` while `id` is current, windowless unless a fallback needs a window;
    /// `spans[publish..]` is the used-ring publication, if any. False when a
    /// reset or stop overtook the request before its grant; a granted copy always counts, even
    /// if a reset arrived meanwhile.
    fn copy(
        &self,
        guest: Guest<'_>,
        id: Identity,
        spans: &mut [Transfer<'_>],
        publish: Option<usize>,
    ) -> Result<bool> {
        self.copy_while(guest, id, spans, publish, State::current)
    }
    /// [`Self::copy`] while `live(state, id)` holds instead of while `id` is current.
    fn copy_while(
        &self,
        guest: Guest<'_>,
        id: Identity,
        spans: &mut [Transfer<'_>],
        publish: Option<usize>,
        live: fn(&State, Identity) -> bool,
    ) -> Result<bool> {
        let publish = publish.unwrap_or(spans.len());
        let permit = Arc::new(CopyPermit::new());
        {
            let mut s = self.lock();
            if !live(&s, id) {
                return Ok(false);
            }
            s.permit = Some((id, permit.clone()));
        }
        let copied = match guest.copier {
            Copier::Vm { machine, stops } => guest.backing.copy_guest_windowless(
                machine,
                stops,
                Duration::from_secs(2),
                spans,
                publish,
                &permit,
            ),
            #[cfg(test)]
            Copier::Test(copy) => copy(spans, publish, &permit),
        };
        let mut s = self.lock();
        s.permit = None;
        self.retire_reset(&mut s);
        // A consumed permit stays as it ended: false here means the copy won its grant.
        let granted = !permit.cancel();
        match copied {
            // Even a reset that lost the grant must retire this committed effect.
            Ok(()) => Ok(true),
            // A reset or stop took the permit first: a cancellation, even while `live` still holds
            // (the clear of a suppression a closing reset waits for: the next pass retries it).
            Err(_) if !granted && (!live(&s, id) || !s.current(id)) => Ok(false),
            // After its grant a failed copy may have written guest RAM without publishing it:
            // never a cancellation, always fatal to the VM (the owner's error cancels it).
            Err(e) => Err(e),
        }
    }
    /// Copy `data` and the chain's used-ring publication in one batch, then raise the used-buffer
    /// interrupt. False when the batch was canceled.
    fn complete(
        &self,
        guest: Guest<'_>,
        chain: &Chain<'_>,
        len: u32,
        data: Vec<Transfer<'_>>,
    ) -> Result<Option<bool>> {
        self.complete_chains(guest, chain.id, chain.queue, &[(chain.head, len)], data)
    }
    /// Publish `chains` (head, used length) in the next used slots after `data`, then raise the
    /// interrupt if due. None if a reset or stop came first; else whether the available index read
    /// after publication shows more heads.
    fn complete_chains(
        &self,
        guest: Guest<'_>,
        id: Identity,
        q: Queue,
        chains: &[(u16, u32)],
        data: Vec<Transfer<'_>>,
    ) -> Result<Option<bool>> {
        let mut used = Used::new(q, chains.iter().copied());
        let next = q.next_avail.wrapping_add(chains.len() as u16);
        if self.accepted(EVENT_IDX) {
            // TX: notify when the buffer after these is added (later ones are found by the
            // available index read after publication). RX never waits for a notification (packet
            // arrival drives it; a starved request asks separately), so ask for none.
            let rx = self.profile.is_rx(id.queue);
            used = used.with_events(if rx { next.wrapping_add(0x7fff) } else { next });
        }
        let mut transfers = data;
        let publish = used.publish(q, &mut transfers);
        if !self.copy(guest, id, &mut transfers, Some(publish))? {
            return Ok(None);
        }
        drop(transfers);
        #[cfg(feature = "testing")]
        self.observe("commit");
        self.interrupt(&mut self.lock(), id, &used);
        // More heads already wait (read after publication, behind the full barrier): the owner looks
        // again. A head added after that read is notified (avail_event or no suppression at all).
        Ok(Some(used.avail_index() != next))
    }
    /// RX found no buffer at available index `at`: ask the driver to notify when it adds one (with
    /// EVENT_IDX by avail_event; without it the driver always notifies, the network device never
    /// suppressing), then read the available index again (Dekker with the driver's index store and
    /// avail_event read). None if a reset or stop came first; else whether a buffer appeared.
    fn ask_for_buffer(
        &self,
        guest: Guest<'_>,
        q: Queue,
        id: Identity,
        at: u16,
    ) -> Result<Option<bool>> {
        if !self.accepted(EVENT_IDX) {
            return Ok(Some(false));
        }
        let (event, mut index) = (at.to_le_bytes(), [0; 2]);
        let mut transfers = vec![
            Transfer::Write {
                gpa: q.used + 4 + u64::from(q.size) * 8,
                bytes: &event,
            },
            Transfer::Read {
                gpa: q.avail + 2,
                bytes: &mut index,
            },
        ];
        if !self.copy(guest, id, &mut transfers, None)? {
            return Ok(None);
        }
        drop(transfers);
        Ok(Some(u16::from_le_bytes(index) != at))
    }
    /// After a granted publication: raise the used-buffer interrupt unless the driver suppressed
    /// it or a reset closed the incarnation (ACK, reset and level publication share the lock).
    fn interrupt(&self, s: &mut State, id: Identity, used: &Used) {
        if s.current(id) && used.interrupt_wanted() {
            if matches!(self.profile, Profile::Network(_)) {
                let since = *s.owed_at.get_or_insert_with(Instant::now);
                s.owed += 1;
                if s.owed < COALESCE && since.elapsed() < COALESCE_TIME {
                    return;
                }
            }
            self.raise(s);
        }
    }
    /// Whether the driver accepted `feature`.
    fn accepted(&self, feature: u64) -> bool {
        self.accepted.load(SeqCst) & feature != 0
    }
    fn raise(&self, s: &mut State) {
        s.owed = 0;
        s.owed_at = None;
        profile::raised(s.interrupt == 0);
        s.interrupt |= USED_BUFFER;
        self.irq(s);
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
            copier: Copier::Vm { machine, stops },
        };
        let result = self.owner(guest, cancel);
        if result.is_err() {
            crate::owners::cancel_owners(cancel);
        }
        let mut s = self.lock();
        s.stopped = !s.drained;
        self.take_kicks(&mut s);
        s.in_flight.clear();
        s.permit = None;
        s.notified = [false; 2];
        s.busy = None;
        s.interrupt = 0;
        self.irq(&s);
        result
    }
    /// The end of a power-off drain, after the backend's own (block: the final full sync and the
    /// workers' join): a quiet device. A network backend failure cannot become a successful
    /// power-off.
    fn drain(&self) -> Result<()> {
        let mut s = self.lock();
        if let Some(error) = &s.backend_error {
            return Err(format!("network shutdown: {error}"));
        }
        self.take_kicks(&mut s);
        s.notified = [false; 2];
        s.busy = None;
        s.interrupt = 0;
        self.irq(&s);
        s.drained = true;
        self.backend_ready();
        Ok(())
    }
    /// Entropy and network serve one request at a time; block has its own owner loop.
    fn owner(&self, guest: Guest<'_>, cancel: &AtomicBool) -> Result<()> {
        if let Profile::Block(disk) = &self.profile {
            let result = self.block_owner(guest, disk, cancel);
            profile::report();
            return result;
        }
        let mut snapshot = Snapshot::new(self.profile.queue_max());
        loop {
            let (queue, id, received) = match self.next_work(cancel)? {
                Work::Request {
                    queue,
                    id,
                    received,
                } => (queue, id, received),
                Work::Stop => return Ok(()),
                Work::Drain => return self.drain(),
            };
            let result = self.request(guest, &mut snapshot, queue, id, received, cancel);
            self.finish_request(result, &mut snapshot, queue, id)?;
        }
    }
    /// Wait until there is work, a drain, or a stop.
    fn next_work(&self, cancel: &AtomicBool) -> Result<Work> {
        loop {
            let mut s = self.lock();
            if s.stopped || cancel.load(SeqCst) {
                return Ok(Work::Stop);
            }
            if let Some(error) = &s.backend_error {
                return Err(format!("network backend: {error}"));
            }
            if s.quiescing {
                return Ok(Work::Drain);
            }
            if s.owed_at.is_some_and(|t| t.elapsed() >= COALESCE_TIME) {
                self.raise(&mut s);
            }
            if let Some(work) = self.admit(&mut s) {
                return Ok(work);
            }
            if s.owed > 0 {
                self.raise(&mut s);
            }
            drop(s);
            self.wake.wait(Duration::from_millis(10));
        }
    }
    /// Take the next notified queue, round-robin, unless a reset or a deferred access must go
    /// first. The taken queue becomes active; RX takes the packet tagged with its incarnation.
    fn admit(&self, s: &mut State) -> Option<Work> {
        self.take_kicks(s);
        if !s.admission_open() {
            return None;
        }
        let count = self.profile.queue_count();
        let i = (0..count)
            .map(|n| (s.cursor + n) % count)
            .find(|&i| s.notified[i])?;
        s.notified[i] = false;
        let id = s.identity(i);
        s.in_flight.push(id);
        s.cursor = (i + 1) % count;
        if self.profile.is_rx(i) {
            s.rx_kicked = false;
        }
        let received = if i == 0 {
            // Frames of an older incarnation are dropped.
            std::iter::from_fn(|| s.received.pop_front())
                .find_map(|(tag, packet)| (tag == id).then_some(packet))
        } else {
            None
        };
        Some(Work::Request {
            queue: s.queues[i],
            id,
            received,
        })
    }
    /// Retire the one entropy or network request: advance the ring indices or fail the device,
    /// then complete a reset waiting for it.
    fn finish_request(
        &self,
        result: Result<Request>,
        snapshot: &mut Snapshot,
        q: Queue,
        id: Identity,
    ) -> Result<()> {
        let mut s = self.lock();
        s.retire(id);
        match result? {
            Request::Committed { pending, count } => {
                self.retire_request(&mut s, snapshot, q, id, pending, count)
            }
            Request::Invalid(reason) => {
                snapshot.discard();
                eprintln!("virtio: {reason}");
                if s.current(id) {
                    self.fail(&mut s);
                }
            }
            Request::Empty | Request::Canceled => snapshot.discard(),
            Request::Starved { packet, posted } => {
                snapshot.discard();
                if s.current(id) {
                    s.received.push_front((id, packet));
                    // A notification during this request may already announce the buffer.
                    s.rx_starved = !posted && !s.rx_kicked;
                    // The driver refills after it has seen the completions.
                    if s.owed > 0 {
                        self.raise(&mut s);
                    }
                }
            }
        }
        if self.profile.is_rx(id.queue) && !s.received.is_empty() && s.accepting(0) && !s.rx_starved
        {
            s.notified[0] = true;
        }
        self.retire_reset(&mut s);
        self.settle(&mut s);
        if self.profile.is_rx(id.queue) {
            // A slot is free: the backend may deliver more (it is woken only while it sleeps).
            self.backend_ready();
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
        count: u16,
    ) {
        // A granted completion retires once even if reset arrived; reset clears its old queue afterward.
        s.queues[id.queue].next_avail = q.next_avail.wrapping_add(count);
        s.queues[id.queue].next_used = q.next_used.wrapping_add(count);
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
        received: Option<net::Packet>,
        cancel: &AtomicBool,
    ) -> Result<Request> {
        if self.profile.is_rx(id.queue) && received.is_none() {
            return Ok(Request::Empty);
        }
        let ram = &guest.backing.ram;
        if let Err(reason) = self.check_rings(q, ram) {
            return Ok(Request::Invalid(reason));
        }
        if !snapshot.reusable(q, id) && !self.capture(guest, snapshot, q, id)? {
            return Ok(Request::Canceled);
        }
        if !self.current(id) {
            return Ok(Request::Canceled);
        }
        let (head, descriptors) = match snapshot.chain(q) {
            Ok(Some(c)) => c,
            Ok(None) => {
                let Some(packet) = received else {
                    return Ok(Request::Empty);
                };
                return Ok(match self.ask_for_buffer(guest, q, id, q.next_avail)? {
                    Some(posted) => Request::Starved { packet, posted },
                    None => Request::Canceled,
                });
            }
            Err(e) => return Ok(Request::Invalid(e)),
        };
        if !descriptors_in(ram, &descriptors) {
            return Ok(Request::Invalid("descriptor outside RAM".into()));
        }
        let chain = Chain {
            queue: q,
            id,
            head,
            descriptors: &descriptors,
        };
        match &self.profile {
            Profile::Block(_) => unreachable!("block requests run in the block owner"),
            Profile::Network(network) => {
                self.net_request(guest, &chain, snapshot, network, received, cancel)
            }
            Profile::Entropy => self.entropy_request(guest, &chain),
        }
    }
    /// A well-formed queue whose rings lie in guest RAM (validation only: no raw pointer is
    /// dereferenced or retained by this device).
    fn check_rings(&self, q: Queue, ram: &GuestRam) -> Result<()> {
        if !q.valid(self.profile.queue_max()) {
            return Err("invalid queue geometry".into());
        }
        if !q.rings_in(ram) {
            return Err("queue metadata outside RAM".into());
        }
        Ok(())
    }
    /// Copy the descriptor table and available ring into `snapshot`. False if canceled.
    fn capture(
        &self,
        guest: Guest<'_>,
        snapshot: &mut Snapshot,
        q: Queue,
        id: Identity,
    ) -> Result<bool> {
        if !self.copy(guest, id, &mut snapshot.reads(q), None)? {
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
        Ok(match self.complete(guest, chain, len as u32, writes)? {
            Some(pending) => Request::Committed { pending, count: 1 },
            None => Request::Canceled,
        })
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
    fn direct(&self) -> Option<Arc<dyn devices::Direct>> {
        Some(self.0.clone())
    }
}
/// Register accesses serialise on the state lock (QueueNotify's open-gate case on atomics), so the registry skips the
/// MMIO adapter's mutex.
impl devices::Direct for Virtio {
    fn access(&self, v: usize, o: u64, n: u64, value: Option<u64>) -> Access {
        Virtio::access(self, v, o, n, value)
    }
    fn poll(&self, v: usize, token: u64) -> Access {
        Virtio::poll(self, v, token)
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
                        let mut snapshot = Snapshot::new(8);
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
                        r.retire_request(&mut s, &snapshot, q, id, pending, 1);
                        r.take_kicks(&mut s);
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
            let mut snapshot = Snapshot::new(8);
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
            r.retire_request(&mut s, &snapshot, q, id, true, 1);
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
        let mut snapshot = Snapshot::new(8);
        snapshot.avail[2] = 2;
        snapshot.captured(Identity {
            device: id.device + 1,
            ..id
        });
        r.retire_request(&mut s, &snapshot, q, id, false, 1);
        assert!(!s.notified[0]);
    }
    #[test]
    fn power_quiesce_preserves_accepted_work_and_ignores_later_reset_notify() {
        let r = device();
        negotiated(&r);
        {
            let mut s = r.lock();
            let id = s.identity(0);
            s.in_flight.push(id);
            s.notified[0] = true;
            s.queues[0].ready = true;
            s.status |= status::DRIVER_OK;
        }
        assert!(!r.quiesce().unwrap());
        write(&r, reg::QUEUE_NOTIFY, 0);
        write(&r, reg::STATUS, 0);
        write(&r, reg::QUEUE_READY, 0);
        let s = r.lock();
        assert!(!s.in_flight.is_empty() && s.queues[0].ready && s.quiescing);
        assert!(!s.pending() && s.closing.is_none() && !s.drained && !s.stopped);
        assert_ne!(s.status, 0);
    }
    /// Negotiate, lay out and enable `queues` (eight entries each), then set DRIVER_OK.
    fn live(r: &Virtio, queues: &[u64]) {
        negotiated(r);
        for &q in queues {
            write(r, reg::QUEUE_SEL, q);
            write(r, reg::QUEUE_NUM, 8);
            write(r, reg::QUEUE_DESC_LOW, 0x1000 + q * 0x1000);
            write(r, reg::QUEUE_DRIVER_LOW, 0x1800 + q * 0x1000);
            write(r, reg::QUEUE_DEVICE_LOW, 0x1c00 + q * 0x1000);
            write(r, reg::QUEUE_READY, 1);
        }
        write(r, reg::STATUS, 15);
    }
    fn net() -> Arc<Virtio> {
        let r = device();
        Virtio::network(
            r.irqs.clone(),
            NET_RESOURCE.gsi as usize,
            [2, 0, 0, 0, 0, 1],
            Offloads::default(),
        )
        .unwrap()
        .0
    }
    #[test]
    fn open_gate_notify_skips_the_state_lock_and_keeps_the_vm_busy_until_admitted() {
        let r = device();
        live(&r, &[0]);
        let base = r.irqs.live();
        {
            // A peer holds the state lock: the notify still completes, as a kick.
            let _held = r.state.lock().unwrap();
            assert_eq!(
                r.access(0, reg::QUEUE_NOTIFY, 4, Some(0)),
                Access::Completed(0)
            );
            assert_eq!(
                r.access(1, reg::QUEUE_NOTIFY, 4, Some(0)),
                Access::Completed(0)
            );
        }
        assert_eq!(r.irqs.live(), base + 1, "one count per pending kick");
        assert!(!r.lock().pending());
        let mut s = r.lock();
        assert!(matches!(r.admit(&mut s), Some(Work::Request { .. })));
        assert_eq!(
            r.irqs.live(),
            base + 1,
            "the busy guard took over the kick's count"
        );
        drop(s);
        // Out-of-range queues take the locked path, which ignores them as before.
        write(&r, reg::QUEUE_NOTIFY, 5);
        assert_eq!(r.kicks.iter().map(|k| k.load(SeqCst)).sum::<u64>(), 0);
    }
    #[test]
    fn closed_gates_take_the_locked_path() {
        let r = device();
        negotiated(&r);
        // Before DRIVER_OK the notify is ignored under the lock.
        write(&r, reg::QUEUE_NOTIFY, 0);
        assert_eq!(r.kicks[0].load(SeqCst), 0);
        assert!(!r.lock().pending());
        // A disable deferred behind a request in flight: a later notify waits in the FIFO.
        let r = device();
        live(&r, &[0]);
        {
            let mut s = r.lock();
            let id = s.identity(0);
            s.in_flight.push(id);
        }
        assert!(matches!(
            r.access(0, reg::QUEUE_READY, 4, Some(0)),
            Access::Deferred(_)
        ));
        assert_eq!(r.gates[0].load(SeqCst) & GATE_OPEN, 0);
        assert!(matches!(
            r.access(1, reg::QUEUE_NOTIFY, 4, Some(0)),
            Access::Deferred(_)
        ));
        assert_eq!(r.kicks[0].load(SeqCst), 0);
        assert_eq!(r.lock().waiting.len(), 2);
    }
    #[test]
    fn a_kick_racing_reset_disable_or_power_off_is_dropped_with_its_incarnation() {
        for event in 0..4 {
            let r = device();
            live(&r, &[0]);
            let base = r.irqs.live();
            // The kicker saw the open gate, then the event overtook it before its kick was recorded.
            let gate = r.gates[0].load(SeqCst);
            assert_ne!(gate & GATE_OPEN, 0);
            match event {
                0 => write(&r, reg::STATUS, 0),
                1 => write(&r, reg::QUEUE_READY, 0),
                2 => assert!(!r.quiesce().unwrap()),
                _ => r.stop(),
            }
            if event < 2 {
                // The driver brings a new incarnation up before the owner looks.
                if event == 0 {
                    live(&r, &[0]);
                } else {
                    write(&r, reg::QUEUE_READY, 1);
                }
                assert_ne!(r.gates[0].load(SeqCst) & GATE_OPEN, 0);
            }
            r.record_kick(0, gate);
            // The kicker sees the moved epoch itself: no count waits for an owner that may be gone.
            assert_eq!(r.kicks[0].load(SeqCst), 0, "event {event}: stale kick kept");
            assert_eq!(
                r.irqs.live(),
                base,
                "event {event}: stale kick held a count"
            );
            let mut s = r.lock();
            r.take_kicks(&mut s);
            assert!(!s.pending(), "event {event}: stale kick scheduled work");
            r.settle(&mut s);
            drop(s);
            assert_eq!(r.irqs.live(), base, "event {event}: kick count released");
        }
    }
    #[test]
    fn a_kick_survives_a_closing_reset_of_another_queue_as_a_locked_notify_does() {
        let r = net();
        live(&r, &[0, 1]);
        let gate = r.gates[1].load(SeqCst);
        assert_ne!(gate & GATE_OPEN, 0, "TX opens");
        assert_eq!(r.gates[0].load(SeqCst) & GATE_OPEN, 0, "RX never opens");
        {
            let mut s = r.lock();
            let id = s.identity(0);
            s.in_flight.push(id);
        }
        // RX disable waits for its request: TX's gate closes, but its epoch stays.
        write(&r, reg::QUEUE_SEL, 0);
        assert!(matches!(
            r.access(0, reg::QUEUE_READY, 4, Some(0)),
            Access::Deferred(_)
        ));
        assert_eq!(r.gates[1].load(SeqCst) >> 2, gate >> 2);
        r.record_kick(1, gate);
        let mut s = r.lock();
        r.take_kicks(&mut s);
        assert_eq!(s.notified, [false, true]);
        // A device reset then drops it, as it drops a locked notify.
        s.in_flight.clear();
        r.retire_reset(&mut s);
        drop(s);
        r.record_kick(1, gate);
        let mut s = r.lock();
        let _ = r.reset(&mut s, Reset::Device);
        drop(s);
        let mut s = r.lock();
        r.take_kicks(&mut s);
        assert!(!s.pending());
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
            let id = s.identity(0);
            s.in_flight.push(id);
            s.queues[0].ready = true;
            s.permit = Some((id, permit.clone()));
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
            s.in_flight.clear();
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
    fn batches_split_into_windows_of_whole_requests_at_the_byte_and_span_bounds() {
        let cost = |spans, bytes| Cost { spans, bytes };
        let overhead = cost(2, 6);
        assert!(pack(&[], overhead).is_empty());
        // 32 full-size reads: 128 KiB + status + element each; seven fit 1 MiB.
        let read = cost(34, 128 * 1024 + 9);
        let windows = pack(&[read; 32], overhead);
        assert_eq!(windows.first(), Some(&(0..7)));
        assert_eq!(windows.len(), 5);
        assert_eq!(windows.last(), Some(&(28..32)));
        // Many small requests split at the span bound instead.
        let small = cost(100, 512);
        let per_window = (MAX_SPANS - 2) / 100;
        assert_eq!(pack(&[small; 40], overhead)[0], 0..per_window);
        // Exactly at both bounds still fits one window; one more byte or span splits.
        let exact = cost(MAX_SPANS - 2, MAX_BYTES - 6);
        assert_eq!(pack(&[exact], overhead), [0..1]);
        let halves = [cost(1, (MAX_BYTES - 6) / 2), cost(1, (MAX_BYTES - 6) / 2)];
        assert_eq!(pack(&halves, overhead), [0..2]);
        let over = [
            cost(1, (MAX_BYTES - 6) / 2),
            cost(1, (MAX_BYTES - 6) / 2 + 1),
        ];
        assert_eq!(pack(&over, overhead), [0..1, 1..2]);
        let spans = [cost(MAX_SPANS - 3, 0), cost(2, 0)];
        assert_eq!(pack(&spans, overhead), [0..1, 1..2]);
        // Every window holds whole requests and stays within both bounds.
        let mixed: Vec<Cost> = (0..64)
            .map(|i| cost(1 + i % 35, (i % 9) * 32 * 1024 + 9))
            .collect();
        let windows = pack(&mixed, overhead);
        assert_eq!(windows.iter().map(|w| w.len()).sum::<usize>(), 64);
        for w in windows {
            let total = mixed[w].iter().fold(overhead, |t, c| t.plus(*c));
            assert!(total.fits());
        }
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
