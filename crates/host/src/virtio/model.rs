//! The device interface for virtio devices on owned buffers ([`Model`]): the owner captures a chain, hands the model
//! the readable bytes of a request and copies its reply, and the data the model delivers ([`ModelPort::deliver`]),
//! into guest buffers through [`Virtio::copy`] (windowless, with today's ring and descriptor checks). A model never sees
//! guest memory or a guest address.
//!
//! Locking: [`Model::started`], [`Model::reset`] and the [`ModelPort::on_ready`] callback run under the device's state
//! lock and only record and signal; [`Model::request`] and [`Model::drain`] run without it.
// The console (the plan's next task) is the first device on this interface outside the tests.
#![cfg_attr(not(test), expect(dead_code))]
use super::{
    Arrival, Chain, Guest, Identity, Profile, Request, Virtio, descriptors_in, gather,
    queue::{Descriptor, Queue, Snapshot},
    scatter,
};
use crate::{
    Result,
    execution::io::{MAX_BYTES, MAX_SPANS},
    irq::Irqs,
    pci::configuration::PciClassCode,
};
use std::{
    sync::{
        Arc, Mutex, OnceLock, Weak,
        atomic::{AtomicBool, AtomicU64, Ordering::SeqCst},
    },
    time::Duration,
};

/// The most bytes a DeviceWrites queue holds for the guest beyond its posted buffers (a message queue: or one message,
/// when it holds none).
pub(super) const BACKLOG: usize = 64 * 1024;
/// The most messages a message queue holds.
const MESSAGES: usize = 64;

/// Which way a queue's buffers go.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum Direction {
    /// The driver posts requests (readable bytes, then an optional writable part for a reply): a transmit queue.
    DeviceReads,
    /// The driver posts empty buffers for the data the device delivers: a receive queue.
    DeviceWrites,
}
/// One queue of a model.
#[derive(Clone, Copy, Debug)]
pub(crate) struct QueueSpec {
    /// QueueNumMax: a power of two.
    pub max: u16,
    pub direction: Direction,
    /// DeviceWrites only: delivered bytes are a stream, split across chains as they fit; otherwise each delivery is
    /// one message, written whole into one chain.
    pub stream: bool,
}
/// A model's answer to one piece of a request.
pub(crate) enum Reply {
    Done(Option<Vec<u8>>),
    Busy,
}
pub(crate) trait Model: Send + Sync {
    /// The virtio device ID (virtio 1.2 §5).
    fn device_id(&self) -> u32;
    /// The feature bits offered besides VERSION_1.
    fn features(&self) -> u64;
    /// The device's feature dependencies: whether the driver may accept `accepted` (offered bits, VERSION_1 among them).
    fn accepts(&self, accepted: u64) -> bool;
    /// Device configuration space, read-only to the guest.
    fn config(&self) -> Vec<u8>;
    /// 1..=[`super::MAX_QUEUES`] queues.
    fn queues(&self) -> &[QueueSpec];
    /// Class and subclass for the PCI transport.
    fn pci_class(&self) -> (PciClassCode, u8);
    /// A request on a DeviceReads queue: `readable` is one piece (at most MAX_BYTES) of the chain's readable bytes,
    /// `last` true for the final piece. `Busy` leaves this piece unconsumed: the request leaves the owner's in-flight
    /// set and is offered again after `ModelPort::resume(queue)`; a device reset or stop drops it (its chain is never
    /// completed, as a reset abandons any unfinished request), and no piece of an old incarnation reaches a new one.
    /// `Done(reply)`: the reply (at most `writable` bytes) goes to the chain's writable part with the last piece;
    /// earlier pieces return `Done(None)`. Runs without the state lock; returns promptly and never waits for the
    /// guest or a backend.
    fn request(&self, queue: u16, readable: &[u8], last: bool, writable: usize) -> Reply;
    /// DRIVER_OK: `port` serves this incarnation. Under the state lock: record and signal only (never a port call).
    fn started(&self, port: ModelPort);
    /// Device reset. Under the state lock: record and signal only.
    fn reset(&self);
    /// Power-off drain: true once every accepted transmit byte has reached its backend. Polled, never waits; turns
    /// true within the model's own grace, discarding (and counting) what its backend did not take by then.
    fn drain(&self) -> bool;
}

/// One device incarnation's endpoint for its model ([`Model::started`]): data for DeviceWrites queues, and resuming
/// requests the model answered `Busy`. It holds the device weakly, so a model may keep it without a cycle.
#[derive(Clone)]
pub struct ModelPort {
    device: Weak<Virtio>,
    /// The device incarnation (`State::generation`) this port serves.
    generation: u64,
}
impl ModelPort {
    /// For a DeviceWrites queue: takes as many bytes as fit in posted buffers and the queue's bounded backlog
    /// (stream queues split across chains; a message queue takes a message whole or not at all) and returns the
    /// count taken; 0 after the device was reset.
    pub fn deliver(&self, queue: u16, bytes: &[u8]) -> usize {
        let Some(device) = self.device.upgrade() else {
            return 0;
        };
        let Profile::Model(hosted) = &device.profile else {
            return 0;
        };
        let q = usize::from(queue);
        let Some(spec) = hosted.model.queues().get(q).copied() else {
            return 0;
        };
        if spec.direction != Direction::DeviceWrites || bytes.is_empty() {
            return 0;
        }
        let mut s = device.lock();
        // Under the lock: a reset cannot slip between the check and the tag.
        let id = s.identity(q);
        if s.generation != self.generation || !s.current(id) || !s.accepting(q) {
            return 0;
        }
        let backlog = &mut s.received[q];
        let held: usize = backlog
            .iter()
            .map(|(_, data)| match data {
                Arrival::Bytes(bytes) => bytes.len(),
                Arrival::Packet(_) => 0,
            })
            .sum();
        let taken = if spec.stream {
            let n = bytes.len().min(BACKLOG.saturating_sub(held));
            match backlog.back_mut() {
                // The owner takes a queue's whole run at once: append to it.
                Some((tag, Arrival::Bytes(run))) if *tag == id => {
                    run.extend_from_slice(&bytes[..n])
                }
                _ if n > 0 => backlog.push_back((id, Arrival::Bytes(bytes[..n].to_vec()))),
                _ => {}
            }
            n
        } else if backlog.len() < MESSAGES && (held + bytes.len() <= BACKLOG || backlog.is_empty())
        {
            backlog.push_back((id, Arrival::Bytes(bytes.to_vec())));
            bytes.len()
        } else {
            0
        };
        // A request of this queue in flight re-notifies itself while data waits (`finish_request`).
        if taken > 0 && !s.notified[q] && !s.queue_active(q) && !s.rx_starved[q] {
            s.notified[q] = true;
            device.busy(&mut s);
            device.wake.ring();
        }
        taken
    }
    /// `ready` runs (under the device lock: it must only signal) when backlog room frees after a delivery, a receive
    /// queue becomes ready, DRIVER_OK, and at stop or drain, so a model's thread can wait for those instead of
    /// polling. Replaces an earlier one.
    pub fn on_ready(&self, ready: impl Fn() + Send + Sync + 'static) {
        if let Some(device) = self.device.upgrade() {
            if let Profile::Model(hosted) = &device.profile {
                *hosted.ready.lock().unwrap_or_else(|e| e.into_inner()) = Some(Arc::new(ready));
            }
        }
    }
    /// A `Busy` request of `queue` may be offered again.
    pub fn resume(&self, queue: u16) {
        let Some(device) = self.device.upgrade() else {
            return;
        };
        let q = usize::from(queue);
        if q >= device.profile.queue_count() {
            return;
        }
        let mut s = device.lock();
        if s.generation != self.generation || s.stopped {
            return;
        }
        // Also a resume that overtakes the owner's recording of the Busy answer (`finish_request`).
        s.resumed[q] = true;
        if s.parked[q].is_some() && s.accepting(q) {
            s.notified[q] = true;
            device.busy(&mut s);
            device.wake.ring();
        }
    }
}

/// A model and what the device keeps for it (`Profile::Model`).
pub(super) struct Hosted {
    pub model: Arc<dyn Model>,
    /// The device itself, for the ports [`Model::started`] gets.
    this: OnceLock<Weak<Virtio>>,
    /// The model's doorbell ([`ModelPort::on_ready`]).
    ready: Mutex<Option<Arc<dyn Fn() + Send + Sync>>>,
    /// Messages dropped because the posted chain could not hold them whole.
    pub lost: AtomicU64,
}
impl Hosted {
    /// `queue` is a DeviceWrites queue.
    pub fn receives(&self, queue: usize) -> bool {
        self.model
            .queues()
            .get(queue)
            .is_some_and(|q| q.direction == Direction::DeviceWrites)
    }
    /// DRIVER_OK of incarnation `generation` (under the state lock).
    pub fn start(&self, generation: u64) {
        let device = self.this.get().cloned().unwrap_or_default();
        self.model.started(ModelPort { device, generation });
    }
    /// Ring the model's doorbell, if it set one (under the state lock).
    pub fn ring(&self) {
        let ready = self.ready.lock().unwrap_or_else(|e| e.into_inner()).clone();
        if let Some(ready) = ready {
            ready();
        }
    }
}

/// A DeviceReads request part-way through: its chain, the bytes of its readable part the model consumed, and the
/// piece after them once copied. A request the model answered `Busy` waits as this in its queue (`State::parked`),
/// outside the in-flight set, so a reset never waits for it.
pub(super) struct Parked {
    id: Identity,
    head: u16,
    descriptors: Vec<Descriptor>,
    offset: usize,
    piece: Option<Vec<u8>>,
}
impl Parked {
    pub fn id(&self) -> Identity {
        self.id
    }
}

impl Virtio {
    /// A device on `model`, created before boot; its owner runs as any other device's.
    pub(crate) fn model(irqs: Irqs, gsi: usize, model: Arc<dyn Model>) -> Arc<Self> {
        let queues = model.queues();
        assert!(
            (1..=super::MAX_QUEUES).contains(&queues.len()),
            "a model has 1 to {} queues",
            super::MAX_QUEUES
        );
        assert!(
            queues.iter().all(|q| q.max.is_power_of_two()),
            "queue maxima are powers of two"
        );
        let device = Self::new(
            irqs,
            gsi,
            Profile::Model(Hosted {
                model,
                this: OnceLock::new(),
                ready: Mutex::new(None),
                lost: AtomicU64::new(0),
            }),
        );
        if let Profile::Model(hosted) = &device.profile {
            let _ = hosted.this.set(Arc::downgrade(&device));
        }
        device
    }
    /// A model request: a DeviceReads chain's readable bytes go to the model piece by piece; a DeviceWrites chain (and,
    /// for a stream, the chains after it) receives the data admitted with it.
    pub(super) fn model_request(
        &self,
        guest: Guest<'_>,
        chain: &Chain<'_>,
        snapshot: &Snapshot,
        hosted: &Hosted,
        received: Option<Arrival>,
    ) -> Result<Request> {
        if !hosted.receives(chain.id.queue) {
            let request = Parked {
                id: chain.id,
                head: chain.head,
                descriptors: chain.descriptors.to_vec(),
                offset: 0,
                piece: None,
            };
            return self.model_transmit(guest, chain.queue, request, hosted);
        }
        let Some(Arrival::Bytes(bytes)) = received else {
            panic!("receive admission reserves data")
        };
        if hosted.model.queues()[chain.id.queue].stream {
            self.model_stream(guest, chain, snapshot, bytes)
        } else {
            self.model_message(guest, chain, hosted, bytes)
        }
    }
    /// A parked request the model may now take (`ModelPort::resume`), on its queue's registers `q`.
    pub(super) fn model_resume(
        &self,
        guest: Guest<'_>,
        q: Queue,
        parked: Parked,
    ) -> Result<Request> {
        let Profile::Model(hosted) = &self.profile else {
            unreachable!("only a model parks requests")
        };
        self.model_transmit(guest, q, parked, hosted)
    }
    /// Offer the request's readable bytes from `request.offset` to the model, a piece at a time (at most one copy's
    /// bytes and spans each), and publish its reply with the last. A `Busy` answer parks the request.
    fn model_transmit(
        &self,
        guest: Guest<'_>,
        q: Queue,
        mut request: Parked,
        hosted: &Hosted,
    ) -> Result<Request> {
        let descriptors = std::mem::take(&mut request.descriptors);
        let split = descriptors
            .iter()
            .position(Descriptor::is_write_only)
            .unwrap_or(descriptors.len());
        let (readable, writable) = descriptors.split_at(split);
        if writable.iter().any(|d| !d.is_write_only()) {
            return Ok(Request::Invalid(
                "model chain: a readable buffer after a writable one".into(),
            ));
        }
        let total: usize = readable.iter().map(|d| d.len() as usize).sum();
        let room: usize = writable.iter().map(|d| d.len() as usize).sum();
        loop {
            let piece = match request.piece.take() {
                Some(piece) => piece,
                None => {
                    let extents = next_piece(readable, request.offset);
                    let mut piece = vec![0; extents.iter().map(|e| e.1).sum()];
                    let mut reads = gather(extents.into_iter(), &mut piece);
                    if !self.copy(guest, request.id, &mut reads, None)? {
                        return Ok(Request::Canceled);
                    }
                    drop(reads);
                    piece
                }
            };
            // A reset closing now waits for this request: the model sees the piece before its `reset`.
            if !self.current(request.id) {
                return Ok(Request::Canceled);
            }
            let last = request.offset + piece.len() == total;
            match hosted
                .model
                .request(request.id.queue as u16, &piece, last, room)
            {
                Reply::Busy => {
                    request.piece = Some(piece);
                    request.descriptors = descriptors;
                    return Ok(Request::Parked(request));
                }
                Reply::Done(_) if !last => request.offset += piece.len(),
                Reply::Done(reply) => {
                    let mut reply = reply.unwrap_or_default();
                    debug_assert!(reply.len() <= room, "a model reply larger than its buffers");
                    reply.truncate(room);
                    let writes = scatter(writable, &reply);
                    if writes.len() + 4 > MAX_SPANS {
                        return Ok(Request::Invalid(
                            "model reply buffers too fragmented".into(),
                        ));
                    }
                    let used = [(request.head, reply.len() as u32)];
                    return Ok(
                        match self.complete_chains(guest, request.id, q, &used, writes)? {
                            Some(pending) => Request::Committed { pending, count: 1 },
                            None => Request::Canceled,
                        },
                    );
                }
            }
        }
    }
    /// One message into `chain`, whole; one too large for it is dropped (and counted), as the network device drops a
    /// frame.
    fn model_message(
        &self,
        guest: Guest<'_>,
        chain: &Chain<'_>,
        hosted: &Hosted,
        message: Vec<u8>,
    ) -> Result<Request> {
        let room = match receive_room(chain.descriptors) {
            Ok(n) => n,
            Err(e) => return Ok(Request::Invalid(e)),
        };
        if room < message.len() {
            hosted.lost.fetch_add(1, SeqCst);
            return Ok(Request::Empty);
        }
        let writes = scatter(chain.descriptors, &message);
        if writes.len() + 4 > MAX_SPANS {
            return Ok(Request::Invalid(
                "model receive buffers too fragmented".into(),
            ));
        }
        Ok(
            match self.complete(guest, chain, message.len() as u32, writes)? {
                Some(_) => Request::Committed {
                    pending: true,
                    count: 1,
                },
                None => Request::Canceled,
            },
        )
    }
    /// A stream's bytes across `chain` and the chains posted after it, each filled before the next. Bytes that do not
    /// fit stay at the front of the queue's backlog ([`Request::Partial`]) for the driver's next buffers.
    fn model_stream(
        &self,
        guest: Guest<'_>,
        chain: &Chain<'_>,
        snapshot: &Snapshot,
        bytes: Vec<u8>,
    ) -> Result<Request> {
        let q = chain.queue;
        let (mut writes, mut used, mut at) = (Vec::new(), Vec::new(), 0);
        // Chains are left for the next request once the copy's spans would run out.
        let mut left = false;
        while at < bytes.len() {
            let k = used.len() as u16;
            let (head, descriptors) = if k == 0 {
                (chain.head, chain.descriptors.to_vec())
            } else {
                let next = Queue {
                    next_avail: q.next_avail.wrapping_add(k),
                    ..q
                };
                match snapshot.chain(next) {
                    Ok(Some(c)) => c,
                    Ok(None) => break,
                    Err(e) => return Ok(Request::Invalid(e)),
                }
            };
            if !descriptors_in(&guest.backing.ram, &descriptors) {
                return Ok(Request::Invalid("descriptor outside RAM".into()));
            }
            let room = match receive_room(&descriptors) {
                Ok(n) => n,
                Err(e) => return Ok(Request::Invalid(e)),
            };
            let n = room.min(bytes.len() - at);
            let part = scatter(&descriptors, &bytes[at..at + n]);
            if writes.len() + part.len() + 4 > MAX_SPANS {
                if k == 0 {
                    return Ok(Request::Invalid(
                        "model receive buffers too fragmented".into(),
                    ));
                }
                left = true;
                break;
            }
            writes.extend(part);
            used.push((head, n as u32));
            at += n;
        }
        let count = used.len() as u16;
        let Some(more) = self.complete_chains(guest, chain.id, q, &used, writes)? else {
            return Ok(Request::Canceled);
        };
        if at == bytes.len() {
            return Ok(Request::Committed {
                pending: true,
                count,
            });
        }
        // The chains ran out: ask the driver to notify the next one (with EVENT_IDX), as a starved frame does.
        let next = q.next_avail.wrapping_add(count);
        let posted = left
            || more
            || self
                .ask_for_buffer(guest, q, chain.id, next)?
                .unwrap_or(false);
        Ok(Request::Partial {
            count,
            rest: bytes[at..].to_vec(),
            posted,
        })
    }
    /// Power-off: poll the model until its transmitted bytes reached their backends. False if the device stopped first.
    pub(super) fn model_drained(&self, cancel: &AtomicBool) -> bool {
        let Profile::Model(hosted) = &self.profile else {
            return true;
        };
        while !hosted.model.drain() {
            if self.lock().stopped || cancel.load(SeqCst) {
                return false;
            }
            self.wake.wait(Duration::from_millis(1));
        }
        true
    }
}

/// The extents of the next piece of `readable`'s bytes from `offset`: at most one copy's bytes and spans.
fn next_piece(readable: &[Descriptor], offset: usize) -> Vec<(u64, usize)> {
    let (mut extents, mut start, mut bytes) = (Vec::new(), 0, 0);
    for d in readable {
        let len = d.len() as usize;
        let end = start + len;
        if offset < end {
            let skip = offset.saturating_sub(start);
            let n = (len - skip).min(MAX_BYTES - bytes);
            extents.push((d.addr() + skip as u64, n));
            bytes += n;
            if bytes == MAX_BYTES || extents.len() == MAX_SPANS {
                break;
            }
        }
        start = end;
    }
    extents
}

/// The bytes a DeviceWrites chain holds; every buffer must be device-writable.
fn receive_room(chain: &[Descriptor]) -> Result<usize> {
    if chain.iter().any(|d| !d.is_write_only()) {
        return Err("model chain direction".into());
    }
    Ok(chain.iter().map(|d| d.len() as usize).sum())
}

#[cfg(test)]
mod tests {
    use super::super::{Copier, Guest, RNG_RESOURCE, reg, status, tests::device};
    use super::*;
    use crate::{
        devices::Access,
        execution::io::{CopyPermit, MAX_BYTES, Transfer, tests as io},
        pci::Function,
    };
    use std::{
        sync::atomic::{AtomicU16, AtomicUsize},
        time::Instant,
    };

    const ECHO_ID: u32 = 42;
    /// Queue 0 receives (DeviceWrites), queue 1 transmits (DeviceReads); their maxima differ.
    const SIZES: [u16; 2] = [64, 256];
    /// The echo device: bytes transmitted on queue 1 are delivered back on queue 0 through its port. It offers feature
    /// bits 0 and 1, bit 1 depending on bit 0, and ten bytes of configuration.
    struct Echo {
        queues: [QueueSpec; 2],
        port: Mutex<Option<ModelPort>>,
        /// Every piece offered: (queue, bytes, last, writable).
        offered: Mutex<Vec<(u16, Vec<u8>, bool, usize)>>,
        /// `request` answers Busy while set.
        busy: AtomicBool,
        /// Transmitted bytes go back on queue 0.
        echo: AtomicBool,
        /// The reply to a request's last piece.
        reply: Mutex<Option<Vec<u8>>>,
        drained: AtomicBool,
        starts: AtomicUsize,
        resets: AtomicUsize,
    }
    impl Echo {
        fn new(stream: bool) -> Self {
            Self {
                queues: [
                    QueueSpec {
                        max: SIZES[0],
                        direction: Direction::DeviceWrites,
                        stream,
                    },
                    QueueSpec {
                        max: SIZES[1],
                        direction: Direction::DeviceReads,
                        stream: false,
                    },
                ],
                port: Mutex::new(None),
                offered: Mutex::new(Vec::new()),
                busy: AtomicBool::new(false),
                echo: AtomicBool::new(true),
                reply: Mutex::new(None),
                drained: AtomicBool::new(true),
                starts: AtomicUsize::new(0),
                resets: AtomicUsize::new(0),
            }
        }
        fn offered(&self) -> Vec<(u16, Vec<u8>, bool, usize)> {
            self.offered.lock().unwrap().clone()
        }
    }
    impl Model for Echo {
        fn device_id(&self) -> u32 {
            ECHO_ID
        }
        fn features(&self) -> u64 {
            0b11
        }
        fn accepts(&self, accepted: u64) -> bool {
            accepted & 0b10 == 0 || accepted & 0b01 != 0
        }
        fn config(&self) -> Vec<u8> {
            (1..=10).collect()
        }
        fn queues(&self) -> &[QueueSpec] {
            &self.queues
        }
        fn pci_class(&self) -> (PciClassCode, u8) {
            (PciClassCode::SimpleCommunicationController, 0x80)
        }
        fn request(&self, queue: u16, readable: &[u8], last: bool, writable: usize) -> Reply {
            self.offered
                .lock()
                .unwrap()
                .push((queue, readable.to_vec(), last, writable));
            if self.busy.load(SeqCst) {
                return Reply::Busy;
            }
            // The port is taken out first: `started` takes this lock under the device's.
            let port = self.port.lock().unwrap().clone();
            if let Some(port) = port.filter(|_| self.echo.load(SeqCst)) {
                assert_eq!(port.deliver(0, readable), readable.len(), "echo backlog");
            }
            Reply::Done(if last {
                self.reply.lock().unwrap().clone()
            } else {
                None
            })
        }
        fn started(&self, port: ModelPort) {
            self.starts.fetch_add(1, SeqCst);
            *self.port.lock().unwrap() = Some(port);
        }
        fn reset(&self) {
            self.resets.fetch_add(1, SeqCst);
            *self.port.lock().unwrap() = None;
        }
        fn drain(&self) -> bool {
            self.drained.load(SeqCst)
        }
    }

    const GSI: usize = RNG_RESOURCE.gsi as usize;
    const RAM: usize = 4 << 20;
    /// Per queue: descriptor table, available ring, used ring.
    const RINGS: [[u64; 3]; 2] = [[0x0, 0x1000, 0x2000], [0x4000, 0x5000, 0x6000]];
    /// Receive buffer `i` is at `RX + i * 0x1000`.
    const RX: u64 = 0x10000;
    const TX: u64 = 0x60000;
    const BIG: u64 = 0x100000;
    const NEXT: u16 = 1;
    const WRITE: u16 = 2;

    fn echo(stream: bool) -> (Arc<Virtio>, Arc<Echo>) {
        let model = Arc::new(Echo::new(stream));
        let d = Virtio::model(device().irqs.clone(), GSI, model.clone());
        (d, model)
    }
    fn write(d: &Virtio, offset: u64, value: u64) {
        assert_eq!(d.access(0, offset, 4, Some(value)), Access::Completed(0));
    }
    fn read(d: &Virtio, offset: u64, size: u64) -> u64 {
        match d.access(0, offset, size, None) {
            Access::Completed(v) => v,
            other => panic!("read {offset:#x}: {other:?}"),
        }
    }
    /// ACKNOWLEDGE, DRIVER, the driver's `features` (and VERSION_1), FEATURES_OK: the status read back.
    fn negotiate(d: &Virtio, features: u64) -> u64 {
        write(d, reg::STATUS, 1);
        write(d, reg::STATUS, 3);
        write(d, reg::DRIVER_FEATURES_SEL, 0);
        write(d, reg::DRIVER_FEATURES, features);
        write(d, reg::DRIVER_FEATURES_SEL, 1);
        write(d, reg::DRIVER_FEATURES, 1);
        write(d, reg::STATUS, 11);
        read(d, reg::STATUS, 4)
    }
    /// Messages the device dropped.
    fn lost(d: &Virtio) -> u64 {
        let Profile::Model(hosted) = &d.profile else {
            unreachable!()
        };
        hosted.lost.load(SeqCst)
    }
    fn until(what: &str, f: impl Fn() -> bool) {
        let start = Instant::now();
        while !f() {
            assert!(
                start.elapsed() < Duration::from_secs(5),
                "timed out: {what}"
            );
            std::thread::sleep(Duration::from_micros(200));
        }
    }
    fn pattern(len: usize, seed: u8) -> Vec<u8> {
        (0..len)
            .map(|i| (i as u8).wrapping_mul(31).wrapping_add(seed))
            .collect()
    }

    /// A whole guest: both queues' rings and buffers in fixture RAM, the device's owner on a thread.
    struct Rig {
        b: crate::execution::Backing,
        d: Arc<Virtio>,
        model: Arc<Echo>,
        /// Per queue: the next descriptor and available index the driver uses.
        next: Mutex<[(u16, u16); 2]>,
    }
    impl Rig {
        fn new(stream: bool) -> Self {
            let (d, model) = echo(stream);
            let rig = Self {
                b: io::backing_with(1, RAM),
                d,
                model,
                next: Mutex::new([(0, 0); 2]),
            };
            rig.init();
            rig
        }
        /// Negotiate, lay out both queues at their maximum size, DRIVER_OK.
        fn init(&self) {
            assert_eq!(negotiate(&self.d, 0b11), 11);
            for (q, [desc, avail, used]) in RINGS.iter().enumerate() {
                for (o, v) in [
                    (reg::QUEUE_SEL, q as u64),
                    (reg::QUEUE_NUM, SIZES[q].into()),
                    (reg::QUEUE_DESC_LOW, *desc),
                    (reg::QUEUE_DRIVER_LOW, *avail),
                    (reg::QUEUE_DEVICE_LOW, *used),
                    (reg::QUEUE_READY, 1),
                ] {
                    write(&self.d, o, v);
                }
            }
            write(&self.d, reg::STATUS, 15);
        }
        /// A device reset (waiting for it to retire), then a fresh driver: rings cleared, [`Self::init`].
        fn reinit(&self) {
            match self.d.access(0, reg::STATUS, 4, Some(0)) {
                Access::Completed(0) => {}
                Access::Deferred(token) => until("reset retired", || {
                    self.d.poll(0, token) == Access::Completed(0)
                }),
                other => panic!("reset: {other:?}"),
            }
            for [desc, avail, used] in RINGS {
                for at in [desc, avail, used] {
                    self.put(at, &[0; 0x1000]);
                }
            }
            *self.next.lock().unwrap() = [(0, 0); 2];
            self.init();
        }
        fn port(&self) -> ModelPort {
            self.model.port.lock().unwrap().clone().expect("started")
        }
        fn put(&self, gpa: u64, bytes: &[u8]) {
            let at = self.b.ram.range(gpa, bytes.len() as u64).unwrap();
            // SAFETY: fixture RAM in bounds; the device writes only buffers it was given.
            unsafe { std::ptr::copy_nonoverlapping(bytes.as_ptr(), at, bytes.len()) };
        }
        fn get(&self, gpa: u64, len: usize) -> Vec<u8> {
            let at = self.b.ram.range(gpa, len as u64).unwrap();
            // SAFETY: as `put`; read after the used index says the device is done.
            unsafe { std::slice::from_raw_parts(at, len) }.to_vec()
        }
        fn word(&self, gpa: u64) -> &AtomicU16 {
            // SAFETY: an aligned ring field of fixture RAM, accessed atomically on both sides.
            unsafe { AtomicU16::from_ptr(self.b.ram.range(gpa, 2).unwrap().cast()) }
        }
        /// Post a chain of `(gpa, len, device-writable)` buffers on `q`; its head.
        fn post(&self, q: usize, buffers: &[(u64, u32, bool)]) -> u16 {
            let size = SIZES[q];
            let mut next = self.next.lock().unwrap();
            let (first, index) = next[q];
            for (i, &(gpa, len, writable)) in buffers.iter().enumerate() {
                let n = (first + i as u16) % size;
                let more = i + 1 < buffers.len();
                let flags = if more { NEXT } else { 0 } | if writable { WRITE } else { 0 };
                let mut d = [0; 16];
                d[..8].copy_from_slice(&gpa.to_le_bytes());
                d[8..12].copy_from_slice(&len.to_le_bytes());
                d[12..14].copy_from_slice(&flags.to_le_bytes());
                d[14..].copy_from_slice(&((n + 1) % size).to_le_bytes());
                self.put(RINGS[q][0] + u64::from(n) * 16, &d);
            }
            self.put(
                RINGS[q][1] + 4 + u64::from(index % size) * 2,
                &first.to_le_bytes(),
            );
            self.word(RINGS[q][1] + 2)
                .store(index.wrapping_add(1), SeqCst);
            next[q] = ((first + buffers.len() as u16) % size, index.wrapping_add(1));
            first
        }
        /// Receive buffer `i` of `len` bytes, as its own chain.
        fn post_rx(&self, i: u64, len: u32) -> u16 {
            self.post(0, &[(RX + i * 0x1000, len, true)])
        }
        fn kick(&self, q: u64) {
            write(&self.d, reg::QUEUE_NOTIFY, q);
        }
        fn used(&self, q: usize) -> u16 {
            self.word(RINGS[q][2] + 2).load(SeqCst)
        }
        /// Used element `slot` of `q`: (head, length).
        fn element(&self, q: usize, slot: u16) -> (u32, u32) {
            let e = self.get(RINGS[q][2] + 4 + u64::from(slot % SIZES[q]) * 8, 8);
            (
                u32::from_le_bytes(e[..4].try_into().unwrap()),
                u32::from_le_bytes(e[4..].try_into().unwrap()),
            )
        }
        /// Everything queue 0's used elements hold, in used order (each head is receive buffer `head`).
        fn received(&self) -> Vec<u8> {
            (0..self.used(0))
                .flat_map(|slot| {
                    let (head, len) = self.element(0, slot);
                    self.get(RX + u64::from(head) * 0x1000, len as usize)
                })
                .collect()
        }
        fn needs_reset(&self) -> bool {
            self.d.lock().status & status::NEEDS_RESET != 0
        }
        /// Run the owner while `body` drives the guest; then stop it.
        fn run(&self, body: impl FnOnce(&Self)) {
            let b = &self.b;
            let copy = move |spans: &mut [Transfer<'_>], publish: usize, permit: &CopyPermit| {
                io::windowless_permit(b, spans, publish, permit)
            };
            let guest = Guest {
                backing: &self.b,
                copier: Copier::Test(&copy),
            };
            let cancel = AtomicBool::new(false);
            // Stops the owner even when `body` panics, so a failed check fails rather than hangs.
            struct Stop<'a>(&'a Virtio);
            impl Drop for Stop<'_> {
                fn drop(&mut self) {
                    self.0.stop();
                }
            }
            std::thread::scope(|scope| {
                let (d, cancel) = (&self.d, &cancel);
                let owner = scope.spawn(move || d.owner(guest, cancel));
                {
                    let _stop = Stop(&self.d);
                    body(self);
                }
                owner.join().unwrap().expect("model owner failed");
            });
        }
    }

    #[test]
    fn features_and_config_reach_the_driver() {
        let (d, model) = echo(true);
        assert_eq!(read(&d, reg::DEVICE_ID, 4), u64::from(ECHO_ID));
        write(&d, reg::DEVICE_FEATURES_SEL, 0);
        assert_eq!(read(&d, reg::DEVICE_FEATURES, 4), 0b11);
        write(&d, reg::DEVICE_FEATURES_SEL, 1);
        assert_eq!(read(&d, reg::DEVICE_FEATURES, 4), 1, "VERSION_1");
        for (q, max) in [(0, 64), (1, 256), (2, 0)] {
            write(&d, reg::QUEUE_SEL, q);
            assert_eq!(read(&d, reg::QUEUE_NUM_MAX, 4), max, "queue {q}");
        }
        assert_eq!(read(&d, reg::CONFIG, 1), 1);
        assert_eq!(read(&d, reg::CONFIG + 4, 4), 0x0807_0605);
        assert_eq!(read(&d, reg::CONFIG + 8, 2), 0x0a09);
        assert_eq!(
            d.access(0, reg::CONFIG, 1, Some(0)),
            Access::Refused,
            "read-only"
        );
        // The model's dependency: bit 1 needs bit 0.
        assert_eq!(negotiate(&d, 0b10), 3, "FEATURES_OK refused");
        write(&d, reg::STATUS, 0);
        assert_eq!(negotiate(&d, 0b11), 11);
        assert_eq!(d.lock().features, 1 << 32 | 0b11);
        for q in 0..2u64 {
            write(&d, reg::QUEUE_SEL, q);
            write(&d, reg::QUEUE_NUM, SIZES[q as usize].into());
            write(&d, reg::QUEUE_DESC_LOW, 0x1000 * (q + 1));
            write(&d, reg::QUEUE_DRIVER_LOW, 0x1000 * (q + 1) + 0x800);
            write(&d, reg::QUEUE_DEVICE_LOW, 0x1000 * (q + 1) + 0xc00);
            write(&d, reg::QUEUE_READY, 1);
        }
        assert_eq!(model.starts.load(SeqCst), 0);
        write(&d, reg::STATUS, 15);
        assert_eq!(model.starts.load(SeqCst), 1, "DRIVER_OK starts the model");
        write(&d, reg::STATUS, 15);
        assert_eq!(model.starts.load(SeqCst), 1, "once per incarnation");
        // Each queue's size is checked against its own maximum.
        let (d, _) = echo(true);
        assert_eq!(negotiate(&d, 0), 11);
        write(&d, reg::QUEUE_SEL, 1);
        write(&d, reg::QUEUE_NUM, 256);
        assert_eq!(read(&d, reg::STATUS, 4) & u64::from(status::NEEDS_RESET), 0);
        write(&d, reg::QUEUE_SEL, 0);
        write(&d, reg::QUEUE_NUM, 128);
        assert_ne!(read(&d, reg::STATUS, 4) & u64::from(status::NEEDS_RESET), 0);
    }
    #[test]
    fn request_reply_lands_in_writable_part() {
        let rig = Rig::new(true);
        *rig.model.reply.lock().unwrap() = Some(b"ok!".to_vec());
        rig.put(TX, b"hello");
        rig.put(TX + 0x100, &[0xee; 16]);
        rig.run(|rig| {
            let head = rig.post(1, &[(TX, 5, false), (TX + 0x100, 16, true)]);
            rig.kick(1);
            until("TX used", || rig.used(1) == 1);
            assert_eq!(rig.element(1, 0), (head.into(), 3));
            assert_eq!(rig.get(TX + 0x100, 16), [&b"ok!"[..], &[0xee; 13]].concat());
            assert_eq!(rig.model.offered(), [(1, b"hello".to_vec(), true, 16)]);
        });
    }
    #[test]
    fn deliver_fills_posted_buffers_in_order() {
        let rig = Rig::new(false);
        let messages: [&[u8]; 3] = [b"one", b"two!", b"three"];
        rig.run(|rig| {
            for i in 0..4 {
                rig.post_rx(i, 16);
            }
            rig.kick(0);
            for (k, m) in messages.iter().enumerate() {
                let at = TX + k as u64 * 0x100;
                rig.put(at, m);
                rig.post(1, &[(at, m.len() as u32, false)]);
                rig.kick(1);
            }
            until("three echoed", || rig.used(0) == 3);
            for (slot, m) in messages.iter().enumerate() {
                let (head, len) = rig.element(0, slot as u16);
                assert_eq!((head, len), (slot as u32, m.len() as u32), "slot {slot}");
                assert_eq!(rig.get(RX + u64::from(head) * 0x1000, m.len()), *m);
            }
            until("three transmitted", || rig.used(1) == 3);
        });
    }
    #[test]
    fn stream_delivery_splits_across_chains() {
        let rig = Rig::new(true);
        let data = pattern(250, 1);
        rig.run(|rig| {
            rig.post_rx(0, 100);
            rig.post_rx(1, 100);
            rig.kick(0);
            assert_eq!(rig.port().deliver(0, &data), 250);
            until("two chains", || rig.used(0) == 2);
            assert_eq!([rig.element(0, 0), rig.element(0, 1)], [(0, 100), (1, 100)]);
            // The rest waits for the driver's next buffer.
            std::thread::sleep(Duration::from_millis(50));
            assert_eq!(rig.used(0), 2);
            rig.post_rx(2, 100);
            rig.kick(0);
            until("the rest", || rig.used(0) == 3);
            assert_eq!(rig.element(0, 2), (2, 50));
            assert!(rig.received() == data);
        });
    }
    #[test]
    fn message_too_large_is_dropped_and_counted() {
        let rig = Rig::new(false);
        rig.run(|rig| {
            rig.post_rx(0, 16);
            rig.kick(0);
            let port = rig.port();
            assert_eq!(port.deliver(0, &[7; 32]), 32, "taken whole");
            until("dropped", || lost(&rig.d) == 1);
            assert_eq!(port.deliver(0, b"fits"), 4);
            until("the next message", || rig.used(0) == 1);
            assert_eq!(rig.element(0, 0), (0, 4));
            assert_eq!(rig.received(), b"fits");
            assert_eq!(lost(&rig.d), 1);
        });
    }
    #[test]
    fn deliver_takes_what_fits_and_reports_it() {
        let rig = Rig::new(true);
        let data = pattern(3 * BACKLOG + 123, 2);
        rig.run(|rig| {
            let port = rig.port();
            let ready = Arc::new(AtomicBool::new(false));
            let doorbell = ready.clone();
            port.on_ready(move || doorbell.store(true, SeqCst));
            // No buffer is posted: only the bounded backlog takes bytes, and the count says how many.
            let first = port.deliver(0, &data);
            assert!(
                0 < first && first <= BACKLOG,
                "took {first} of {}",
                data.len()
            );
            // The producer keeps the rest and offers it again each time the doorbell rings.
            for i in 0..64 {
                rig.post_rx(i, 4096);
            }
            rig.kick(0);
            let mut at = first;
            while at < data.len() {
                until("the doorbell", || ready.swap(false, SeqCst));
                at += port.deliver(0, &data[at..]);
            }
            until("all received", || rig.received().len() == data.len());
            assert!(rig.received() == data, "each byte once, in order");
        });
    }
    #[test]
    fn busy_request_is_offered_again_after_resume() {
        let rig = Rig::new(true);
        rig.model.busy.store(true, SeqCst);
        rig.put(TX, b"abc");
        rig.run(|rig| {
            let head = rig.post(1, &[(TX, 3, false)]);
            rig.kick(1);
            until("offered", || rig.model.offered().len() == 1);
            std::thread::sleep(Duration::from_millis(50));
            assert_eq!(rig.model.offered().len(), 1, "not again before a resume");
            assert_eq!(rig.used(1), 0);
            // The other queue is served meanwhile.
            rig.post_rx(0, 16);
            rig.kick(0);
            assert_eq!(rig.port().deliver(0, b"rx"), 2);
            until("delivered while busy", || rig.used(0) == 1);
            rig.model.busy.store(false, SeqCst);
            rig.port().resume(1);
            until("completed", || rig.used(1) == 1);
            assert_eq!(rig.element(1, 0), (head.into(), 0));
            let abc = (1, b"abc".to_vec(), true, 0);
            assert_eq!(rig.model.offered(), [abc.clone(), abc]);
        });
    }
    #[test]
    fn large_transmit_chain_is_delivered_in_pieces() {
        let rig = Rig::new(true);
        rig.model.echo.store(false, SeqCst);
        // 2.5 MiB in one chain of 256 descriptors: more than one copy takes.
        const SEGMENT: usize = 10 * 1024;
        let data = pattern(256 * SEGMENT, 3);
        rig.put(BIG, &data);
        rig.run(|rig| {
            let buffers: Vec<_> = (0..256)
                .map(|i| (BIG + (i * SEGMENT) as u64, SEGMENT as u32, false))
                .collect();
            rig.post(1, &buffers);
            rig.kick(1);
            until("completed", || rig.used(1) == 1);
            assert!(!rig.needs_reset());
            let offered = rig.model.offered();
            assert!(offered.len() > 1, "pieces");
            for (i, (q, piece, last, _)) in offered.iter().enumerate() {
                assert_eq!(*q, 1);
                assert!(!piece.is_empty() && piece.len() <= MAX_BYTES, "piece {i}");
                assert_eq!(*last, i + 1 == offered.len(), "piece {i}");
            }
            assert!(offered.iter().flat_map(|p| p.1.clone()).eq(data));
        });
    }
    #[test]
    fn reset_drops_queued_host_data() {
        let rig = Rig::new(true);
        rig.model.echo.store(false, SeqCst);
        rig.run(|rig| {
            let old = rig.port();
            assert_eq!(old.deliver(0, &[0xaa; BACKLOG]), BACKLOG, "held: no buffer");
            until("held for a buffer", || rig.d.lock().rx_starved[0]);
            rig.reinit();
            assert_eq!(rig.model.resets.load(SeqCst), 1);
            assert_eq!(rig.model.starts.load(SeqCst), 2);
            assert_eq!(old.deliver(0, b"late"), 0, "the old incarnation's port");
            // The old incarnation's bytes were dropped: they take no room and reach no buffer.
            let fresh = pattern(BACKLOG, 4);
            assert_eq!(rig.port().deliver(0, &fresh), BACKLOG);
            for i in 0..16 {
                rig.post_rx(i, 4096);
            }
            rig.kick(0);
            until("delivered", || rig.received().len() == BACKLOG);
            assert!(rig.received() == fresh);
        });
    }
    #[test]
    fn reset_during_busy_completes() {
        let rig = Rig::new(true);
        rig.model.busy.store(true, SeqCst);
        rig.put(TX, b"held");
        rig.run(|rig| {
            rig.post(1, &[(TX, 4, false)]);
            rig.kick(1);
            until("offered", || rig.model.offered().len() == 1);
            let port = rig.port();
            // No resume: the reset does not wait for the busy request.
            match rig.d.access(0, reg::STATUS, 4, Some(0)) {
                Access::Completed(0) => {}
                Access::Deferred(token) => until("reset retired", || {
                    rig.d.poll(0, token) == Access::Completed(0)
                }),
                other => panic!("reset: {other:?}"),
            }
            assert_eq!(rig.model.resets.load(SeqCst), 1);
            // The dropped request is never offered again, nor completed.
            rig.model.busy.store(false, SeqCst);
            port.resume(1);
            std::thread::sleep(Duration::from_millis(50));
            assert_eq!(rig.model.offered().len(), 1);
            assert_eq!(rig.used(1), 0);
        });
    }
    #[test]
    fn drain_waits_for_model() {
        let rig = Rig::new(true);
        rig.model.drained.store(false, SeqCst);
        rig.run(|rig| {
            assert!(!rig.d.quiesce().unwrap());
            std::thread::sleep(Duration::from_millis(50));
            assert!(!rig.d.quiesce().unwrap(), "quiet before the model drained");
            rig.model.drained.store(true, SeqCst);
            until("quiet", || rig.d.quiesce().unwrap());
        });
    }
    #[test]
    fn pci_class_from_model() {
        let (d, _) = echo(true);
        let f = d.pci_with(1, Arc::new(|_, _| {}));
        assert_eq!(
            f.config_read(2),
            0x0780_0001,
            "class 07h, subclass 80h, revision 1"
        );
        // The PCI queue size reads each queue's own maximum.
        for (q, max) in [(0, 64), (1, 256)] {
            let mut s = d.lock();
            s.queue_sel = q;
            let size = d.pci_register(&mut s, reg::PCI | 2 << 16 | 0x18, None);
            assert_eq!(size, Access::Completed(max), "queue {q}");
        }
    }
}
