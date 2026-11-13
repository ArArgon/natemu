//! virtio-net: one RX/TX pair and bounded owned Ethernet packets. No backend holds guest
//! addresses; the native backend is [`super::slirp`].
use super::*;
use std::collections::VecDeque;
use std::sync::{
    OnceLock, Weak,
    mpsc::{self, Receiver, SyncSender, TryRecvError, TrySendError},
};
use std::time::Instant;

/// Feature bits offered (virtio 1.3 §5.1.3): MTU, MAC and STATUS, and EVENT_IDX; checksum and
/// segmentation offloads as the backend's [`Offloads`] allow.
const F_CSUM: u64 = 1;
const F_GUEST_CSUM: u64 = 1 << 1;
const F_MTU: u64 = 1 << 3;
const F_MAC: u64 = 1 << 5;
const F_GUEST_TSO4: u64 = 1 << 7;
const F_GUEST_TSO6: u64 = 1 << 8;
const F_HOST_TSO4: u64 = 1 << 11;
const F_HOST_TSO6: u64 = 1 << 12;
const F_MRG_RXBUF: u64 = 1 << 15;
const F_STATUS: u64 = 1 << 16;
pub(super) const FEATURES: u64 = VERSION_1 | EVENT_IDX | F_MTU | F_MAC | F_STATUS;
/// virtio_net_hdr for VERSION_1: flags, gso_type, hdr_len, gso_size, csum_start, csum_offset,
/// num_buffers.
const HEADER: usize = 12;
const HDR_LEN: std::ops::Range<usize> = 2..4;
const GSO_SIZE: std::ops::Range<usize> = 4..6;
const CSUM_START: std::ops::Range<usize> = 6..8;
const CSUM_OFFSET: std::ops::Range<usize> = 8..10;
const NUM_BUFFERS: std::ops::Range<usize> = 10..12;
/// VIRTIO_NET_HDR_F_NEEDS_CSUM | DATA_VALID | RSC_INFO: meaningful only with offloads.
const OFFLOAD_FLAGS: u8 = 7;
const NEEDS_CSUM: u8 = 1;
const DATA_VALID: u8 = 2;
/// gso_type values (ECN and UDP segmentation are not offered).
const GSO_NONE: u8 = 0;
const GSO_TCPV4: u8 = 1;
const GSO_TCPV6: u8 = 4;
/// VIRTIO_NET_S_LINK_UP.
const LINK_UP: u16 = 1;
const MTU: u16 = 1500;
/// An Ethernet header.
const MIN_FRAME: usize = 14;
/// Ethernet without FCS, including one VLAN tag, MTU1500.
const MAX_FRAME: usize = 1518;
/// A segmentation-offload frame: Linux's 64 KiB GSO limit plus Ethernet and one VLAN tag.
const MAX_LARGE_FRAME: usize = 65536 + 18;

/// What the backend can take, for the device to offer the guest only that (virtio 1.3 §5.1.6).
/// A backend without `tx_segmentation` gets finished frames ([`PacketPort::transmit`] completes
/// a partial checksum first); one with it gets each frame's [`TxMeta`] untouched.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Offloads {
    /// TX: the guest may leave a TCP/UDP checksum partial (CSUM).
    pub tx_checksum: bool,
    /// RX: the backend states each frame's checksum ([`RxMeta::checksum`]): valid (DATA_VALID,
    /// the guest skips verifying) or partial (GUEST_CSUM).
    pub rx_checksum: bool,
    /// TX: TCP frames up to 64 KiB with their segmentation in [`TxMeta`] (HOST_TSO4/6; needs
    /// `tx_checksum`).
    pub tx_segmentation: bool,
    /// RX: TCP frames up to 64 KiB with their segmentation in [`RxMeta`], spread over several
    /// guest buffers if need be (GUEST_TSO4/6, MRG_RXBUF; needs `rx_checksum`).
    pub rx_segmentation: bool,
}
impl Offloads {
    pub(super) fn features(self) -> u64 {
        let mut features = FEATURES;
        if self.tx_checksum {
            features |= F_CSUM;
            if self.tx_segmentation {
                features |= F_HOST_TSO4 | F_HOST_TSO6;
            }
        }
        if self.rx_checksum {
            features |= F_GUEST_CSUM;
            if self.rx_segmentation {
                features |= F_GUEST_TSO4 | F_GUEST_TSO6 | F_MRG_RXBUF;
            }
        }
        features
    }
}
/// The driver accepted no offload without the one it depends on (virtio 1.3 §5.1.3.1).
pub(super) fn dependencies_met(accepted: u64) -> bool {
    (accepted & (F_HOST_TSO4 | F_HOST_TSO6) == 0 || accepted & F_CSUM != 0)
        && (accepted & (F_GUEST_TSO4 | F_GUEST_TSO6) == 0 || accepted & F_GUEST_CSUM != 0)
}

/// TCP segmentation offload of one frame: the protocol, the payload bytes per segment (MSS) and
/// the driver's header-length hint. From a guest (TX) both are untrusted: `size` is only nonzero
/// and `header` only a hint (virtio 1.3 §5.1.6.2.2); a backend must bound its work by them.
/// No ECN: GUEST_ECN/HOST_ECN are not offered, so a backend must not deliver CWR frames.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Gso {
    pub ipv6: bool,
    pub size: u16,
    pub header: u16,
}
/// What a TX frame still needs from a segmentation-capable backend: a partial checksum
/// (csum_start, csum_offset) and its segmentation. Empty for the other backends.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct TxMeta {
    pub checksum: Option<(u16, u16)>,
    pub gso: Option<Gso>,
}
/// An RX frame's checksum as its backend states it.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum RxChecksum {
    /// The guest verifies it.
    #[default]
    Unknown,
    /// Every checksum is correct (DATA_VALID).
    Valid,
    /// The TCP/UDP checksum is partial, as on TX (NEEDS_CSUM).
    Partial { start: u16, offset: u16 },
}
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct RxMeta {
    pub checksum: RxChecksum,
    pub gso: Option<Gso>,
}
/// One frame a backend delivers ([`PacketPort::receive_from`]).
pub struct Frame {
    pub bytes: Vec<u8>,
    pub meta: RxMeta,
}

/// One frame in owned storage (moved between queues without copying): `bytes[at..]`.
pub(super) struct Packet {
    bytes: Vec<u8>,
    at: usize,
    tx: TxMeta,
    rx: RxMeta,
}
impl Packet {
    #[cfg(test)]
    fn new(bytes: &[u8]) -> Result<Self> {
        if !(MIN_FRAME..=MAX_FRAME).contains(&bytes.len()) {
            return Err("network frame length outside MTU1500 profile".into());
        }
        Ok(Self {
            bytes: bytes.to_vec(),
            at: 0,
            tx: TxMeta::default(),
            rx: RxMeta::default(),
        })
    }
    fn bytes(&self) -> &[u8] {
        &self.bytes[self.at..]
    }
}
/// Frame buffers the device and its backend pass back and forth, so neither allocates per packet
/// (allocating on one thread and freeing on the other cost ~8-10% of a bidirectional stream).
#[derive(Clone, Default)]
pub struct Buffers(Arc<Mutex<Vec<Vec<u8>>>>);
impl Buffers {
    /// A buffer holding a copy of `bytes`.
    pub fn take(&self, bytes: &[u8]) -> Vec<u8> {
        let mut buffer = self.spare(bytes.len());
        buffer.extend_from_slice(bytes);
        buffer
    }
    /// A buffer of `len` zero bytes.
    fn zeroed(&self, len: usize) -> Vec<u8> {
        let mut buffer = self.spare(len);
        buffer.resize(len, 0);
        buffer
    }
    fn spare(&self, len: usize) -> Vec<u8> {
        let spare = (len <= POOLED)
            .then(|| self.0.lock().unwrap_or_else(|e| e.into_inner()).pop())
            .flatten();
        let mut buffer = spare.unwrap_or_else(|| Vec::with_capacity(len.max(POOLED)));
        buffer.clear();
        buffer
    }
    /// Only MTU-sized buffers are kept (a segmentation frame is 40 times rarer, and big).
    pub fn give(&self, buffer: Vec<u8>) {
        if buffer.capacity() > POOLED {
            return;
        }
        let mut spare = self.0.lock().unwrap_or_else(|e| e.into_inner());
        if spare.len() < 1024 {
            spare.push(buffer);
        }
    }
}
/// The largest buffer [`Buffers`] keeps: a header and an MTU frame, rounded up.
const POOLED: usize = 2048;
/// TX packets the device may queue for the backend; a full queue holds the device's TX.
const TX_QUEUE: usize = 256;
/// Received frames the device holds for the guest (the backend keeps the rest).
pub(super) const RX_SLOTS: usize = 64;
pub(super) struct Network {
    mac: [u8; 6],
    offloads: Offloads,
    /// TX packets handed to the [`PacketPort`], in order.
    tx: SyncSender<Packet>,
    buffers: Buffers,
    /// The backend's doorbell ([`PacketPort::on_ready`]).
    ready: OnceLock<Box<dyn Fn() + Send + Sync>>,
    /// Received frames dropped: the guest's buffer was too small, or the frame needed an offload
    /// the guest did not accept ([`PacketPort::lost`]).
    pub(super) lost: AtomicU64,
}
impl Network {
    pub fn features(&self) -> u64 {
        self.offloads.features()
    }
    /// Configuration space: mac, status, max_virtqueue_pairs (unused), mtu.
    pub fn config(&self) -> [u8; 12] {
        let mut bytes = [0; 12];
        bytes[..6].copy_from_slice(&self.mac);
        // The backend was admitted before the device was built, so the link is up.
        bytes[6..8].copy_from_slice(&LINK_UP.to_le_bytes());
        bytes[10..12].copy_from_slice(&MTU.to_le_bytes());
        bytes
    }
}

/// Owned-packet endpoint for an independently admitted backend. Service TX until `closed`, then
/// stop/join backend callbacks before ending its supervisor. Never hold a native backend lock
/// while delivering RX. Native launch/entitlement admission belongs to that supervisor.
pub struct PacketPort {
    device: Weak<Virtio>,
    tx: Receiver<Packet>,
    buffers: Buffers,
    offloads: Offloads,
    // An idle backend can still deliver a future packet; all-HLT is not VM completion.
    _arrival: Busy,
}
impl PacketPort {
    /// False (the frame is not taken) if it is out of profile, the RX queue is unavailable or
    /// [`RX_SLOTS`] frames already wait. Arrival schedules RX unless RX is starved (then the
    /// driver's next RX notification does). Only plain Ethernet bytes, excluding FCS.
    pub fn receive(&self, bytes: &[u8]) -> bool {
        if !(MIN_FRAME..=MAX_FRAME).contains(&bytes.len()) {
            return false;
        }
        let mut one = VecDeque::from([Frame {
            bytes: bytes.to_vec(),
            meta: RxMeta::default(),
        }]);
        self.receive_from(&mut one);
        one.is_empty()
    }
    /// [`Self::receive`] for each frame from the front of `frames`, under one lock: taken frames
    /// leave `frames`, and so do ones the guest cannot take (dropped, [`Self::lost`]); the rest
    /// stay. True if any left.
    pub fn receive_from(&self, frames: &mut VecDeque<Frame>) -> bool {
        let Some(device) = self.device.upgrade() else {
            return false;
        };
        let Profile::Network(network) = &device.profile else {
            return false;
        };
        let mut s = device.lock();
        // Under the lock: a reset and renegotiation cannot slip between the check and the tag.
        let accepted = device.accepted.load(SeqCst);
        let id = s.identity(0);
        let before = frames.len();
        let mut taken = false;
        while !frames.is_empty() {
            if !s.current(id) || !s.accepting(0) || s.received.len() >= RX_SLOTS {
                break;
            }
            let frame = frames.pop_front().expect("nonempty");
            match received_packet(frame, accepted, self.offloads) {
                Some(packet) => {
                    s.received.push_back((id, packet));
                    taken = true;
                }
                None => {
                    network.lost.fetch_add(1, SeqCst);
                }
            }
        }
        // An RX request in flight re-notifies itself while frames wait (`finish_request`).
        if taken && !s.notified[0] && !s.queue_active(0) && !s.rx_starved {
            s.notified[0] = true;
            device.busy(&mut s);
            device.wake.ring();
        }
        frames.len() < before
    }
    /// Send the oldest queued TX packet, outside every VM lock and RAM window; false if none. The
    /// guest already saw it complete (like a NIC that fetched it): return success for a transient
    /// drop, error for terminal loss (it fails the device). Never retried.
    /// A backend without `tx_segmentation` gets the frame finished and an empty [`TxMeta`].
    pub fn transmit(&self, send: impl FnOnce(&[u8], &TxMeta) -> Result<()>) -> Result<bool> {
        let mut packet = match self.tx.try_recv() {
            Ok(packet) => packet,
            Err(TryRecvError::Empty | TryRecvError::Disconnected) => return Ok(false),
        };
        if !self.offloads.tx_segmentation {
            if let Some((start, offset)) = packet.tx.checksum.take() {
                let at = packet.at;
                finish_checksum(&mut packet.bytes[at..], start.into(), offset.into());
            }
        }
        let result = send(packet.bytes(), &packet.tx);
        self.buffers.give(packet.bytes);
        if let Err(error) = result {
            self.fail(error.clone());
            return Err(error);
        }
        Ok(true)
    }
    /// The buffer pool frames given to [`Self::receive_from`] should come from.
    pub fn buffers(&self) -> Buffers {
        self.buffers.clone()
    }
    /// `ready` runs (under the device lock: it must only signal) when a TX packet is queued, the RX
    /// slot frees after a receive, and at stop or drain, so a backend can wait for those instead of
    /// polling. Set once.
    pub fn on_ready(&self, ready: impl Fn() + Send + Sync + 'static) {
        if let Some(device) = self.device.upgrade() {
            if let Profile::Network(network) = &device.profile {
                let _ = network.ready.set(Box::new(ready));
            }
        }
    }
    /// Received frames the device dropped because the guest's RX buffer was too small.
    pub fn lost(&self) -> u64 {
        self.device
            .upgrade()
            .map_or(0, |device| match &device.profile {
                Profile::Network(network) => network.lost.load(SeqCst),
                _ => 0,
            })
    }
    pub fn closed(&self) -> bool {
        self.device.upgrade().is_none_or(|device| {
            let s = device.lock();
            s.stopped || s.drained
        })
    }
    pub fn fail(&self, error: String) {
        if let Some(device) = self.device.upgrade() {
            let mut s = device.lock();
            if !s.stopped && !s.drained && s.backend_error.is_none() {
                s.backend_error = Some(error);
            }
            device.wake.ring();
        }
    }
}
impl Drop for PacketPort {
    fn drop(&mut self) {
        self.fail("packet endpoint closed".into());
    }
}

impl Virtio {
    /// Construct after backend admission and before VM boot; the returned port must be serviced
    /// concurrently with `run`. This does not start a network or obtain privileged access.
    pub fn network(
        irqs: Irqs,
        gsi: usize,
        mac: [u8; 6],
        offloads: Offloads,
    ) -> Result<(Arc<Self>, PacketPort)> {
        if mac == [0; 6] || mac[0] & 1 != 0 {
            return Err("network MAC must be nonzero unicast".into());
        }
        let (send, tx) = mpsc::sync_channel(TX_QUEUE);
        let buffers = Buffers::default();
        let device = Self::new(
            irqs,
            gsi,
            Profile::Network(Network {
                mac,
                offloads,
                tx: send,
                buffers: buffers.clone(),
                ready: OnceLock::new(),
                lost: AtomicU64::new(0),
            }),
        );
        let port = PacketPort {
            device: Arc::downgrade(&device),
            tx,
            buffers,
            offloads,
            _arrival: device.irqs.busy(),
        };
        Ok((device, port))
    }
    /// RX: deliver the reserved frame into the chain (with MRG_RXBUF, into as many chains as it
    /// needs). TX: copy the frame out and queue it for the backend (waiting only while that queue
    /// is full). Both then publish the used elements.
    pub(super) fn net_request(
        &self,
        guest: Guest<'_>,
        chain: &Chain<'_>,
        snapshot: &Snapshot,
        network: &Network,
        received: Option<Packet>,
        cancel: &AtomicBool,
    ) -> Result<Request> {
        if self.profile.is_rx(chain.id.queue) {
            let packet = received.expect("RX admission reserves a frame");
            return self.net_receive(guest, chain, snapshot, network, packet);
        }
        let capacity = match capacity(chain.descriptors, false) {
            Ok(n) => n,
            Err(e) => return Ok(Request::Invalid(e)),
        };
        let limit = if self.accepted(F_HOST_TSO4 | F_HOST_TSO6) {
            MAX_LARGE_FRAME
        } else {
            MAX_FRAME
        };
        if !(HEADER + MIN_FRAME..=HEADER + limit).contains(&capacity) {
            return Ok(Request::Invalid("network TX length".into()));
        }
        let mut bytes = network.buffers.zeroed(capacity);
        let extents = chain
            .descriptors
            .iter()
            .map(|d| (d.addr(), d.len() as usize));
        if !self.copy(guest, chain.id, &mut gather(extents, &mut bytes), None)? {
            return Ok(Request::Canceled);
        }
        let packet = match transmit_packet(bytes, self.accepted.load(SeqCst)) {
            Ok(p) => p,
            Err(e) => return Ok(Request::Invalid(e)),
        };
        if !self.submit(network, chain.id, packet, cancel)? {
            return Ok(Request::Canceled);
        }
        Ok(
            match self.complete(guest, chain, 0, Vec::with_capacity(3))? {
                // Look at the ring again even when the index read after publication showed nothing
                // new: one capture costs less than the driver notifications it saves (November 12:
                // the exact check measured ~5% slower, 3.0/4.97 vs 3.3/5.3 Gbit/s up/down).
                Some(_) => Request::Committed {
                    pending: true,
                    count: 1,
                },
                None => Request::Canceled,
            },
        )
    }
    /// One received frame: its header in the first buffer, then the frame, across `chain` and, with
    /// MRG_RXBUF, the chains after it until they hold it (num_buffers counts them). If the driver
    /// has not posted enough yet the frame is held ([`Request::Starved`]); a frame too big for a
    /// lone chain without MRG_RXBUF is lost.
    fn net_receive(
        &self,
        guest: Guest<'_>,
        chain: &Chain<'_>,
        snapshot: &Snapshot,
        network: &Network,
        packet: Packet,
    ) -> Result<Request> {
        let q = chain.queue;
        let need = HEADER + packet.bytes().len();
        let mut room = match capacity(chain.descriptors, true) {
            Ok(n) => n,
            Err(e) => return Ok(Request::Invalid(e)),
        };
        // Chains after the first (MRG_RXBUF); none allocated for a frame that fits one.
        let mut more: Vec<(u16, Vec<Descriptor>)> = Vec::new();
        while room < need && self.accepted(F_MRG_RXBUF) {
            let next = Queue {
                next_avail: q.next_avail.wrapping_add(1 + more.len() as u16),
                ..q
            };
            let (head, descriptors) = match snapshot.chain(next) {
                Ok(Some(c)) => c,
                // Every slot holds one of these chains and the frame still does not fit: it never will.
                Ok(None) if 1 + more.len() >= usize::from(q.size) => {
                    network.lost.fetch_add(1, SeqCst);
                    return Ok(Request::Empty);
                }
                Ok(None) => {
                    return Ok(
                        match self.ask_for_buffer(guest, q, chain.id, next.next_avail)? {
                            Some(posted) => Request::Starved { packet, posted },
                            None => Request::Canceled,
                        },
                    );
                }
                Err(e) => return Ok(Request::Invalid(e)),
            };
            if !descriptors_in(&guest.backing.ram, &descriptors) {
                return Ok(Request::Invalid("descriptor outside RAM".into()));
            }
            room += match capacity(&descriptors, true) {
                Ok(n) => n,
                Err(e) => return Ok(Request::Invalid(e)),
            };
            more.push((head, descriptors));
        }
        // Valid short buffers may receive smaller frames; a larger one is just lost.
        if room < need {
            network.lost.fetch_add(1, SeqCst);
            return Ok(Request::Empty);
        }
        let header = rx_header(&packet.rx, 1 + more.len() as u16);
        let chains = std::iter::once((chain.head, chain.descriptors))
            .chain(more.iter().map(|(head, d)| (*head, d.as_slice())));
        let (writes, used) = scatter_chains(chains, 1 + more.len(), [&header, packet.bytes()]);
        // Tiny buffers could need more spans than one copy takes: a driver fault, not the VM's.
        if writes.len() + 4 > MAX_SPANS {
            return Ok(Request::Invalid("network RX buffers too fragmented".into()));
        }
        let published = self.complete_chains(guest, chain.id, q, &used, writes)?;
        network.buffers.give(packet.bytes);
        Ok(match published {
            Some(_) => Request::Committed {
                pending: true,
                count: used.len() as u16,
            },
            None => Request::Canceled,
        })
    }
    /// Ring the network backend's doorbell, if any.
    pub(super) fn backend_ready(&self) {
        if let Profile::Network(network) = &self.profile {
            if let Some(ready) = network.ready.get() {
                ready();
            }
        }
    }
    /// Queue one TX packet for the backend while `id` is current: true once queued, false if a
    /// reset or stop came first. A full queue waits for the backend (outside the lock); 10 s of
    /// that is process-fatal.
    fn submit(
        &self,
        network: &Network,
        id: Identity,
        mut packet: Packet,
        cancel: &AtomicBool,
    ) -> Result<bool> {
        let start = Instant::now();
        loop {
            {
                let mut s = self.lock();
                if !s.current(id) || s.stopped || cancel.load(SeqCst) {
                    return Ok(false);
                }
                if let Some(error) = &s.backend_error {
                    return Err(error.clone());
                }
                match network.tx.try_send(packet) {
                    Ok(()) => {
                        self.backend_ready();
                        return Ok(true);
                    }
                    Err(TrySendError::Full(p)) => {
                        packet = p;
                        // Completions already published must not wait for the backend.
                        if s.owed > 0 {
                            self.raise(&mut s);
                        }
                    }
                    Err(TrySendError::Disconnected(_)) => {
                        return Err("network transmit endpoint disconnected".into());
                    }
                }
            }
            if start.elapsed() > Duration::from_secs(10) {
                return Err("network transmit queue stalled: VM worker must terminate".into());
            }
            std::thread::sleep(Duration::from_micros(50));
        }
    }
}

fn capacity(chain: &[Descriptor], rx: bool) -> Result<usize> {
    if chain.iter().any(|d| d.is_write_only() != rx) {
        return Err("network chain direction".into());
    }
    chain.iter().try_fold(0usize, |n, d| {
        n.checked_add(d.len() as usize)
            .ok_or("network chain length overflow".into())
    })
}
/// A TX buffer (header, then frame) as a packet, under the features the driver `accepted`:
/// NEEDS_CSUM only with CSUM, its csum_start/offset field inside the frame; TCP segmentation
/// only with the matching HOST_TSO, NEEDS_CSUM and a nonzero gso_size, and then up to 64 KiB.
fn transmit_packet(bytes: Vec<u8>, accepted: u64) -> Result<Packet> {
    if bytes.len() < HEADER {
        return Err("short network TX header".into());
    }
    let field =
        |r: std::ops::Range<usize>| u16::from_le_bytes([bytes[r.start], bytes[r.start + 1]]);
    // Unknown flag bits and inactive checksum/header hints are ignored (virtio 1.3 §5.1.6.2.2).
    let (flags, gso_type) = (bytes[0], bytes[1]);
    let allowed = if accepted & F_CSUM != 0 {
        NEEDS_CSUM
    } else {
        0
    };
    // num_buffers is ignored: Linux zeroes it only with MRG_RXBUF (stale headroom otherwise).
    if flags & OFFLOAD_FLAGS & !allowed != 0 {
        return Err("unnegotiated network TX offload/header".into());
    }
    let gso = match gso_type {
        GSO_NONE => None,
        GSO_TCPV4 | GSO_TCPV6 => {
            let ipv6 = gso_type == GSO_TCPV6;
            let feature = if ipv6 { F_HOST_TSO6 } else { F_HOST_TSO4 };
            let size = field(GSO_SIZE);
            // Each segment's checksum is computed from the partial one.
            if accepted & feature == 0 || flags & NEEDS_CSUM == 0 || size == 0 {
                return Err("invalid network TX segmentation".into());
            }
            Some(Gso {
                ipv6,
                size,
                header: field(HDR_LEN),
            })
        }
        _ => return Err("unnegotiated network TX offload/header".into()),
    };
    let checksum = (flags & NEEDS_CSUM != 0).then(|| (field(CSUM_START), field(CSUM_OFFSET)));
    let len = bytes.len() - HEADER;
    let limit = if gso.is_some() {
        MAX_LARGE_FRAME
    } else {
        MAX_FRAME
    };
    if !(MIN_FRAME..=limit).contains(&len) {
        return Err("network frame length outside its profile".into());
    }
    // The 16-bit field must lie inside the frame, after the checksummed range starts.
    if checksum.is_some_and(|(start, offset)| usize::from(start) + usize::from(offset) + 2 > len) {
        return Err("network TX checksum outside the frame".into());
    }
    Ok(Packet {
        bytes,
        at: HEADER,
        tx: TxMeta { checksum, gso },
        rx: RxMeta::default(),
    })
}

/// A backend frame as the guest may take it, or None (dropped): an MTU frame, or up to 64 KiB with
/// segmentation the backend offered and the guest accepted; a partial checksum inside the frame.
/// Without GUEST_CSUM a partial checksum is completed here and a valid one is not reported.
fn received_packet(frame: Frame, accepted: u64, offloads: Offloads) -> Option<Packet> {
    let Frame {
        mut bytes,
        mut meta,
    } = frame;
    let limit = match meta.gso {
        None => MAX_FRAME,
        Some(g) => {
            let feature = if g.ipv6 { F_GUEST_TSO6 } else { F_GUEST_TSO4 };
            // A segmentation frame carries NEEDS_CSUM (virtio 1.3 §5.1.6.4.1).
            let partial = matches!(meta.checksum, RxChecksum::Partial { .. });
            if !offloads.rx_segmentation || accepted & feature == 0 || g.size == 0 || !partial {
                return None;
            }
            MAX_LARGE_FRAME
        }
    };
    if !(MIN_FRAME..=limit).contains(&bytes.len()) {
        return None;
    }
    let reported = offloads.rx_checksum && accepted & F_GUEST_CSUM != 0;
    match meta.checksum {
        RxChecksum::Partial { start, offset } => {
            let (start, offset) = (usize::from(start), usize::from(offset));
            if start + offset + 2 > bytes.len() {
                return None;
            }
            if !reported {
                finish_checksum(&mut bytes, start, offset);
                meta.checksum = RxChecksum::Unknown;
            }
        }
        RxChecksum::Valid if !reported => meta.checksum = RxChecksum::Unknown,
        _ => {}
    }
    Some(Packet {
        bytes,
        at: 0,
        tx: TxMeta::default(),
        rx: meta,
    })
}

/// The virtio_net_hdr of a received frame spread over `buffers` chains.
fn rx_header(meta: &RxMeta, buffers: u16) -> [u8; HEADER] {
    let mut h = [0u8; HEADER];
    match meta.checksum {
        RxChecksum::Unknown => {}
        RxChecksum::Valid => h[0] = DATA_VALID,
        RxChecksum::Partial { start, offset } => {
            h[0] = NEEDS_CSUM;
            h[CSUM_START].copy_from_slice(&start.to_le_bytes());
            h[CSUM_OFFSET].copy_from_slice(&offset.to_le_bytes());
        }
    }
    if let Some(g) = meta.gso {
        h[1] = if g.ipv6 { GSO_TCPV6 } else { GSO_TCPV4 };
        h[HDR_LEN].copy_from_slice(&g.header.to_le_bytes());
        h[GSO_SIZE].copy_from_slice(&g.size.to_le_bytes());
    }
    h[NUM_BUFFERS].copy_from_slice(&buffers.to_le_bytes());
    h
}

/// Write `parts` in order across the chains' buffers: the writes, and each chain's used element
/// (head, bytes written).
fn scatter_chains<'a, 'd>(
    chains: impl Iterator<Item = (u16, &'d [Descriptor])>,
    count: usize,
    parts: [&'a [u8]; 2],
) -> (Vec<Transfer<'a>>, Vec<(u16, u32)>) {
    // The buffers' writes, then the publication's four transfers (used elements, used index,
    // available ring, used_event) without growing.
    let mut writes = Vec::with_capacity(8);
    let mut used = Vec::with_capacity(count);
    let (mut part, mut at) = (0, 0);
    for (head, descriptors) in chains {
        let mut len = 0u32;
        for d in descriptors {
            let (mut gpa, mut room) = (d.addr(), d.len() as usize);
            while room > 0 && part < parts.len() {
                let rest: &'a [u8] = &parts[part][at..];
                let n = room.min(rest.len());
                if n > 0 {
                    writes.push(Transfer::Write {
                        gpa,
                        bytes: &rest[..n],
                    });
                }
                (gpa, room, at, len) = (gpa + n as u64, room - n, at + n, len + n as u32);
                if at == parts[part].len() {
                    (part, at) = (part + 1, 0);
                }
            }
        }
        used.push((head, len));
    }
    (writes, used)
}

/// Complete a partial checksum (NEEDS_CSUM, virtio 1.3 §5.1.6.2): the field at `start + offset`
/// holds the pseudo-header sum; the ones' complement of the folded sum of `frame[start..]` replaces
/// it, 0 written as 0xffff (as Linux's skb_checksum_help). The caller checked the bounds.
pub(super) fn finish_checksum(frame: &mut [u8], start: usize, offset: usize) {
    let data = &frame[start..];
    let mut sum: u64 = data
        .chunks_exact(2)
        .map(|w| u64::from(u16::from_be_bytes([w[0], w[1]])))
        .sum();
    if data.len() % 2 == 1 {
        sum += u64::from(data[data.len() - 1]) << 8;
    }
    while sum >> 16 != 0 {
        sum = (sum & 0xffff) + (sum >> 16);
    }
    let checksum = match !(sum as u16) {
        0 => 0xffff,
        c => c,
    };
    frame[start + offset..start + offset + 2].copy_from_slice(&checksum.to_be_bytes());
}

#[cfg(test)]
mod tests {
    use super::*;
    fn device() -> (Arc<Virtio>, PacketPort) {
        let rng = super::super::tests::device();
        let (device, port) = Virtio::network(
            rng.irqs.clone(),
            NET_RESOURCE.gsi as usize,
            [2, 0, 0, 0, 0, 1],
            Offloads::default(),
        )
        .unwrap();
        for (o, v) in [
            (reg::STATUS, 3),
            (reg::DRIVER_FEATURES_SEL, 1),
            (reg::DRIVER_FEATURES, 1),
            (reg::STATUS, 11),
        ] {
            write(&device, o, v);
        }
        for i in 0..2 {
            for (o, v) in [
                (reg::QUEUE_SEL, i),
                (reg::QUEUE_NUM, 256),
                (reg::QUEUE_DESC_LOW, 0x1000 + i * 0x4000),
                (reg::QUEUE_DRIVER_LOW, 0x2000 + i * 0x4000),
                (reg::QUEUE_DEVICE_LOW, 0x3000 + i * 0x4000),
                (reg::QUEUE_READY, 1),
            ] {
                write(&device, o, v);
            }
        }
        write(&device, reg::STATUS, 15);
        (device, port)
    }
    fn write(d: &Virtio, o: u64, v: u64) {
        assert!(matches!(d.access(0, o, 4, Some(v)), Access::Completed(0)));
    }
    fn frame() -> Vec<u8> {
        (0..64).collect()
    }
    /// The plain RFC 1071 checksum of `bytes` (independent of `finish_checksum`).
    fn rfc1071(bytes: &[u8]) -> u16 {
        let mut sum = 0u32;
        for (i, b) in bytes.iter().enumerate() {
            sum += u32::from(*b) << if i % 2 == 0 { 8 } else { 0 };
        }
        while sum > 0xffff {
            sum = (sum & 0xffff) + (sum >> 16);
        }
        !(sum as u16)
    }
    /// An Ethernet/IPv4/TCP frame with `payload`, its TCP checksum field holding the folded
    /// pseudo-header sum as a Linux guest leaves it (CHECKSUM_PARTIAL); returns it and the full
    /// checksum the field should end up with.
    fn partial_tcp(payload: &[u8]) -> (Vec<u8>, u16) {
        let (src, dst) = ([10, 0, 2, 15], [192, 168, 12, 100]);
        let tcp_len = 20 + payload.len();
        let mut f = vec![0u8; 14];
        f[12] = 8;
        let mut ip = vec![0x45, 0, 0, 0, 0, 0, 0x40, 0, 64, 6, 0, 0];
        ip[2..4].copy_from_slice(&((20 + tcp_len) as u16).to_be_bytes());
        ip.extend(src);
        ip.extend(dst);
        f.extend(&ip);
        let mut tcp = vec![0u8; 20];
        tcp[..4].copy_from_slice(&[0x9c, 0x40, 0x14, 0x51]);
        tcp[12] = 0x50;
        tcp[13] = 0x18;
        tcp.extend(payload);
        let mut pseudo = [src, dst].concat();
        pseudo.extend([0, 6]);
        pseudo.extend((tcp_len as u16).to_be_bytes());
        let full = rfc1071(&[pseudo.clone(), tcp.clone()].concat());
        // The guest's partial field: the uncomplemented folded pseudo-header sum.
        tcp[16..18].copy_from_slice(&(!rfc1071(&pseudo)).to_be_bytes());
        f.extend(tcp);
        (f, full)
    }
    #[test]
    fn partial_checksums_complete_to_the_full_tcp_checksum() {
        for payload in [&b""[..], b"x", b"natemu", &[0xffu8; 1459][..]] {
            let (mut f, full) = partial_tcp(payload);
            finish_checksum(&mut f, 34, 16);
            let got = u16::from_be_bytes([f[50], f[51]]);
            assert_eq!(
                got,
                if full == 0 { 0xffff } else { full },
                "{} bytes",
                payload.len()
            );
            // A receiver's verification sums the segment and pseudo-header to all ones.
            let mut pseudo = f[26..34].to_vec();
            pseudo.extend([0, 6]);
            pseudo.extend(((f.len() - 34) as u16).to_be_bytes());
            assert!(matches!(
                rfc1071(&[pseudo, f[34..].to_vec()].concat()),
                0 | 0xffff
            ));
        }
        // A sum that completes to 0 is written as 0xffff.
        let mut zero = vec![0u8; 4];
        zero[0] = 0xff;
        zero[1] = 0xff;
        finish_checksum(&mut zero, 0, 2);
        assert_eq!(zero[2..], [0xff, 0xff]);
    }
    #[test]
    fn needs_csum_is_accepted_only_when_negotiated_and_inside_the_frame() {
        let (frame, _) = partial_tcp(b"payload");
        let header = |start: u16, offset: u16| {
            let mut h = vec![0u8; HEADER];
            h[0] = NEEDS_CSUM;
            h[CSUM_START].copy_from_slice(&start.to_le_bytes());
            h[CSUM_OFFSET].copy_from_slice(&offset.to_le_bytes());
            [h, frame.clone()].concat()
        };
        assert!(
            transmit_packet(header(34, 16).to_vec(), 0).is_err(),
            "CSUM not accepted"
        );
        assert_eq!(
            transmit_packet(header(34, 16).to_vec(), F_CSUM)
                .unwrap()
                .tx
                .checksum,
            Some((34, 16))
        );
        let last = frame.len() as u16 - 2;
        assert!(transmit_packet(header(0, last).to_vec(), F_CSUM).is_ok());
        assert!(transmit_packet(header(0, last + 1).to_vec(), F_CSUM).is_err());
        assert!(transmit_packet(header(u16::MAX, u16::MAX).to_vec(), F_CSUM).is_err());
        let mut gso = header(34, 16);
        gso[1] = 1; // GSO_TCPV4 is not offered
        assert!(transmit_packet(gso.to_vec(), F_CSUM).is_err());
        let mut valid = header(34, 16);
        valid[0] |= DATA_VALID; // RX-only
        assert!(transmit_packet(valid.to_vec(), F_CSUM).is_err());
    }
    #[test]
    fn segmentation_headers_are_checked_against_what_was_accepted() {
        let (frame, _) = partial_tcp(b"x");
        let tx = |flags: u8, gso: u8, size: u16, len: usize, accepted: u64| {
            let mut h = vec![0u8; HEADER];
            h[0] = flags;
            h[1] = gso;
            h[GSO_SIZE].copy_from_slice(&size.to_le_bytes());
            h[CSUM_START].copy_from_slice(&34u16.to_le_bytes());
            h[CSUM_OFFSET].copy_from_slice(&16u16.to_le_bytes());
            let mut f = frame.clone();
            f.resize(len, 0);
            transmit_packet([h, f].concat(), accepted)
        };
        let tso4 = F_CSUM | F_HOST_TSO4;
        assert!(tx(NEEDS_CSUM, GSO_TCPV4, 1448, 9000, tso4).is_ok());
        assert!(tx(0, GSO_TCPV4, 1448, 9000, tso4).is_err(), "no NEEDS_CSUM");
        assert!(
            tx(NEEDS_CSUM, GSO_TCPV4, 0, 9000, tso4).is_err(),
            "gso_size 0"
        );
        assert!(
            tx(NEEDS_CSUM, GSO_TCPV6, 1448, 9000, tso4).is_err(),
            "TSO6 not accepted"
        );
        assert!(
            tx(NEEDS_CSUM, GSO_TCPV4 | 0x80, 1448, 9000, tso4).is_err(),
            "ECN"
        );
        assert!(tx(NEEDS_CSUM, 3, 1448, 9000, tso4).is_err(), "UDP");
        assert!(
            tx(NEEDS_CSUM, 0, 0, MAX_FRAME + 1, tso4).is_err(),
            "big without GSO"
        );
        assert!(tx(NEEDS_CSUM, GSO_TCPV4, 1448, MAX_LARGE_FRAME, tso4).is_ok());
        assert!(tx(NEEDS_CSUM, GSO_TCPV4, 1448, MAX_LARGE_FRAME + 1, tso4).is_err());
    }
    #[test]
    fn a_received_segmentation_frame_needs_a_partial_checksum() {
        let all = Offloads {
            tx_checksum: true,
            rx_checksum: true,
            tx_segmentation: true,
            rx_segmentation: true,
        };
        let accepted = F_GUEST_CSUM | F_GUEST_TSO4 | F_MRG_RXBUF;
        let gso = Some(Gso {
            ipv6: false,
            size: 1448,
            header: 54,
        });
        let frame = |checksum| Frame {
            bytes: vec![0; 9000],
            meta: RxMeta { checksum, gso },
        };
        let partial = RxChecksum::Partial {
            start: 34,
            offset: 16,
        };
        assert!(received_packet(frame(partial), accepted, all).is_some());
        assert!(received_packet(frame(RxChecksum::Valid), accepted, all).is_none());
        assert!(received_packet(frame(RxChecksum::Unknown), accepted, all).is_none());
        assert!(received_packet(frame(partial), accepted & !F_GUEST_TSO4, all).is_none());
    }
    #[test]
    fn offered_features_follow_the_backend() {
        let rng = super::super::tests::device();
        for (offloads, extra) in [
            (Offloads::default(), 0),
            (
                Offloads {
                    tx_checksum: true,
                    rx_checksum: false,
                    ..Offloads::default()
                },
                F_CSUM,
            ),
            (
                Offloads {
                    tx_checksum: true,
                    rx_checksum: true,
                    ..Offloads::default()
                },
                F_CSUM | F_GUEST_CSUM,
            ),
        ] {
            let (d, _p) = Virtio::network(
                rng.irqs.clone(),
                NET_RESOURCE.gsi as usize,
                [2, 0, 0, 0, 0, 1],
                offloads,
            )
            .unwrap();
            assert_eq!(d.profile.features(), FEATURES | extra);
        }
    }
    #[test]
    fn packet_contract_ignores_inactive_fields_and_unknown_flags() {
        let mut bytes = vec![0; HEADER];
        bytes.extend(frame());
        bytes[0] = 0xf8;
        bytes[2..10].fill(0xff);
        assert_eq!(transmit_packet(bytes.to_vec(), 0).unwrap().bytes(), frame());
        for at in [0, 1] {
            let mut bad = bytes.clone();
            bad[at] = 1;
            assert!(transmit_packet(bad.to_vec(), 0).is_err());
        }
        // num_buffers on TX is stale headroom unless MRG_RXBUF: ignored.
        let mut stale = bytes.clone();
        stale[10..12].copy_from_slice(&[0x5a, 0xa5]);
        assert!(transmit_packet(stale, 0).is_ok());
        assert!(Packet::new(&[0; 13]).is_err());
        assert!(Packet::new(&[0; MAX_FRAME + 1]).is_err());
        assert_eq!(
            Packet::new(&[0; MAX_FRAME]).unwrap().bytes().len(),
            MAX_FRAME
        );
        let split = [
            Descriptor::new(0x1000, 2, 0, 0),
            Descriptor::new(0x2000, 74, 0, 0),
        ];
        assert_eq!(capacity(&split, false).unwrap(), HEADER + frame().len());
        assert!(capacity(&split, true).is_err());
    }
    #[test]
    fn notify_selects_written_queue_and_rx_arrival_needs_no_second_kick() {
        let (d, p) = device();
        write(&d, reg::QUEUE_SEL, 0xffff_0000);
        write(&d, reg::QUEUE_NOTIFY, 1);
        {
            // TX's open gate records a kick, which the owner takes under the lock.
            let mut s = d.lock();
            d.take_kicks(&mut s);
            assert_eq!(s.notified, [false, true]);
            s.notified = [false; 2];
        }
        write(&d, reg::QUEUE_NOTIFY, 0);
        assert!(!d.lock().pending());
        assert!(p.receive(&frame()));
        assert_eq!(d.lock().notified, [true, false]);
        for _ in 1..RX_SLOTS {
            assert!(p.receive(&frame()));
        }
        assert!(!p.receive(&frame()), "RX_SLOTS frames wait at most");
        let old = d.lock().received.front().unwrap().0;
        write(&d, reg::QUEUE_SEL, 0);
        write(&d, reg::QUEUE_READY, 0);
        assert!(d.lock().received.is_empty());
        write(&d, reg::QUEUE_READY, 1);
        assert!(!d.current(old));
        assert!(p.receive(&frame()));
        assert_ne!(d.lock().received.front().unwrap().0, old);
        d.stop();
    }
    #[test]
    fn disabling_other_queue_preserves_active_copy_and_pending_irq() {
        let (d, _p) = device();
        let permit = Arc::new(CopyPermit::new());
        let id = d.lock().identity(1);
        {
            let mut s = d.lock();
            s.in_flight.push(id);
            s.permit = Some((id, permit.clone()));
            s.interrupt = 1;
            s.notified[1] = true;
        }
        write(&d, reg::QUEUE_SEL, 0);
        write(&d, reg::QUEUE_READY, 0);
        assert!(d.current(id));
        {
            let s = d.lock();
            assert!(s.notified[1] && s.interrupt == 1 && s.queues[1].ready && !s.queues[0].ready);
        }
        write(&d, reg::QUEUE_NUM, 128);
        write(&d, reg::QUEUE_DESC_LOW, 0xa000);
        assert_eq!(d.lock().queues[0].desc, 0xa000);
        // The active request and its permit remain owned by TX until TX itself is disabled.
        assert!(std::sync::Arc::ptr_eq(
            &d.lock().permit.as_ref().unwrap().1,
            &permit
        ));
        write(&d, reg::QUEUE_SEL, 1);
        assert!(matches!(
            d.access(0, reg::QUEUE_READY, 4, Some(0)),
            Access::Deferred(_)
        ));
        assert!(!d.current(id));
        assert!(permit.cancel());
        {
            let mut s = d.lock();
            s.in_flight.clear();
            s.permit = None;
            d.retire_reset(&mut s);
        }
        assert_eq!(d.lock().interrupt, 1);
        d.stop();
    }
    #[test]
    fn transmit_queue_is_bounded_ordered_and_a_send_error_fails_the_device() {
        let (d, p) = device();
        let Profile::Network(n) = &d.profile else {
            unreachable!()
        };
        for i in 0..TX_QUEUE {
            let mut bytes = frame();
            bytes[0] = i as u8;
            assert!(n.tx.try_send(Packet::new(&bytes).unwrap()).is_ok());
        }
        assert!(matches!(
            n.tx.try_send(Packet::new(&frame()).unwrap()),
            Err(TrySendError::Full(_))
        ));
        let mut seen = Vec::new();
        while p
            .transmit(|bytes, _| {
                seen.push(bytes[0]);
                Ok(())
            })
            .unwrap()
        {}
        assert_eq!(
            seen,
            (0..TX_QUEUE).map(|i| i as u8).collect::<Vec<_>>(),
            "each once, in order"
        );
        // A full queue makes the submitter wait; a stop releases it without queueing.
        let tx = d.lock().identity(1);
        for _ in 0..TX_QUEUE {
            assert!(
                d.submit(
                    n,
                    tx,
                    Packet::new(&frame()).unwrap(),
                    &AtomicBool::new(false)
                )
                .unwrap()
            );
        }
        assert!(
            !d.submit(
                n,
                tx,
                Packet::new(&frame()).unwrap(),
                &AtomicBool::new(true)
            )
            .unwrap()
        );
        assert!(p.transmit(|_, _| Err("lost interface".into())).is_err());
        assert!(d.lock().backend_error.is_some());
        d.stop();
    }
    #[test]
    fn event_idx_interrupts_only_when_the_used_index_passes_used_event() {
        let q = Queue {
            next_used: 10,
            ..Queue::default()
        };
        let wanted = |used_event: u16, count: u16| {
            let mut used = Used::new(q, (0..count).map(|h| (h, 0))).with_events(0);
            used.event.as_mut().unwrap().1 = used_event.to_le_bytes();
            used.interrupt_wanted()
        };
        assert!(wanted(10, 1), "the driver waits for slot 10");
        assert!(!wanted(11, 1), "not yet");
        assert!(wanted(12, 3), "slots 10..13 cross 12");
        assert!(!wanted(9, 1), "already passed: the driver is polling");
        let wrap = Queue {
            next_used: 0xffff,
            ..Queue::default()
        };
        let mut used = Used::new(wrap, [(0, 0), (1, 0)]).with_events(0);
        used.event.as_mut().unwrap().1 = 0u16.to_le_bytes();
        assert!(used.interrupt_wanted(), "across the index wrap");
    }
    #[test]
    fn a_notification_during_a_starved_rx_request_is_not_lost() {
        let (d, p) = device();
        assert!(p.receive(&frame()));
        let q = d.lock().queues[0];
        let starve = |kick: bool| {
            let Some(Work::Request { id, received, .. }) = d.admit(&mut d.lock()) else {
                panic!("RX admitted")
            };
            if kick {
                write(&d, reg::QUEUE_NOTIFY, 0); // the driver adds a buffer meanwhile
            }
            let packet = received.unwrap();
            d.finish_request(
                Ok(Request::Starved {
                    packet,
                    posted: false,
                }),
                &mut Snapshot::new(256),
                q,
                id,
            )
            .unwrap();
        };
        starve(true);
        {
            let s = d.lock();
            assert!(!s.rx_starved && s.notified[0], "retried at once");
            assert_eq!(s.received.len(), 1, "the frame is kept");
        }
        starve(false);
        assert!(
            d.lock().rx_starved && !d.lock().notified[0],
            "held until notified"
        );
        assert!(p.receive(&frame()));
        assert!(!d.lock().notified[0], "arrivals wait too");
        write(&d, reg::QUEUE_NOTIFY, 0);
        assert!(!d.lock().rx_starved && d.lock().notified[0]);
        d.stop();
    }
    #[test]
    fn doorbell_rings_for_queued_tx_freed_rx_slot_drain_and_stop() {
        let (d, p) = device();
        let rings = Arc::new(std::sync::atomic::AtomicUsize::new(0));
        let r = rings.clone();
        p.on_ready(move || {
            r.fetch_add(1, SeqCst);
        });
        let Profile::Network(n) = &d.profile else {
            unreachable!()
        };
        let tx = d.lock().identity(1);
        let packet = || Packet::new(&frame()).unwrap();
        let go = AtomicBool::new(false);
        assert!(d.submit(n, tx, packet(), &go).unwrap());
        assert_eq!(rings.load(SeqCst), 1, "a queued TX packet");
        d.lock().generations[1] += 1;
        assert!(!d.submit(n, tx, packet(), &go).unwrap());
        assert_eq!(
            rings.load(SeqCst),
            1,
            "a reset one is not queued and rings nothing"
        );
        let rx = d.lock().identity(0);
        let q = d.lock().queues[0];
        d.lock().in_flight.push(rx);
        d.finish_request(Ok(Request::Empty), &mut Snapshot::new(256), q, rx)
            .unwrap();
        assert_eq!(rings.load(SeqCst), 2, "the RX slot is free again");
        d.quiesce().unwrap();
        d.drain().unwrap();
        assert_eq!(rings.load(SeqCst), 3, "drained");
        d.stop();
        assert_eq!(rings.load(SeqCst), 4, "stopped");
        assert!(p.closed());
    }
    #[test]
    fn terminal_backend_failure_cannot_become_successful_poweroff() {
        let (d, p) = device();
        p.fail("lost interface".into());
        assert!(!d.quiesce().unwrap());
        assert!(d.drain().is_err());
        assert!(!d.lock().drained);
        d.stop();
        let (d, p) = device();
        assert!(!d.quiesce().unwrap());
        p.fail("lost during drain".into());
        assert!(d.drain().is_err());
        assert!(!d.lock().drained);
        d.stop();
        let (d, p) = device();
        d.quiesce().unwrap();
        d.drain().unwrap();
        assert!(p.closed());
        drop(p);
        assert!(d.lock().backend_error.is_none());
        let (d, p) = device();
        drop(p);
        assert!(d.lock().backend_error.is_some());
        d.stop();
    }
    #[test]
    fn idle_packet_endpoint_remains_an_asynchronous_wakeup_source() {
        let (d, p) = device();
        assert!(!d.lock().pending());
        assert!(d.lock().busy.is_none());
        assert_eq!(d.irqs.live(), 1);
        d.quiesce().unwrap();
        d.drain().unwrap();
        assert!(p.closed());
        assert_eq!(d.irqs.live(), 1);
        drop(p);
        assert_eq!(d.irqs.live(), 0);
    }

    /// A whole guest: RX/TX rings and buffers in fixture RAM, the device's owner on a thread.
    mod rig {
        use super::*;
        use crate::execution::io::tests as io;
        use std::sync::atomic::AtomicU16;
        const RAM: usize = 0x80000;
        const SIZE: u16 = 32;
        /// Per queue: descriptor table, available ring, used ring.
        const RINGS: [[u64; 3]; 2] = [[0x0, 0x1000, 0x2000], [0x4000, 0x5000, 0x6000]];
        const RX_BUFFERS: u64 = 0x8000;
        const RX_BUFFER: u32 = 1536;
        const TX_BUFFERS: u64 = 0x40000;
        const ALL: u64 = VERSION_1
            | F_MAC
            | F_MTU
            | F_STATUS
            | F_CSUM
            | F_GUEST_CSUM
            | F_HOST_TSO4
            | F_HOST_TSO6
            | F_GUEST_TSO4
            | F_GUEST_TSO6
            | F_MRG_RXBUF;
        const OFFLOADS: Offloads = Offloads {
            tx_checksum: true,
            rx_checksum: true,
            tx_segmentation: true,
            rx_segmentation: true,
        };
        struct Rig {
            b: crate::execution::Backing,
            d: Arc<Virtio>,
            p: PacketPort,
            /// Per queue: the next descriptor and available index the driver uses.
            next: Mutex<[(u16, u16); 2]>,
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
        impl Rig {
            /// Negotiate `features` (FEATURES_OK must hold if `ok`), lay out both queues.
            fn new(features: u64) -> Option<Self> {
                Self::with(features, OFFLOADS)
            }
            fn with(features: u64, offloads: Offloads) -> Option<Self> {
                let irqs = super::super::super::tests::device().irqs.clone();
                let (d, p) = Virtio::network(
                    irqs,
                    NET_RESOURCE.gsi as usize,
                    [2, 0, 0, 0, 0, 1],
                    offloads,
                )
                .unwrap();
                let w = |o, v| assert!(matches!(d.access(0, o, 4, Some(v)), Access::Completed(0)));
                w(reg::STATUS, 1);
                w(reg::STATUS, 3);
                for sel in 0..2u64 {
                    w(reg::DRIVER_FEATURES_SEL, sel);
                    w(reg::DRIVER_FEATURES, (features >> (32 * sel)) & 0xffff_ffff);
                }
                w(reg::STATUS, 11);
                if d.lock().status & status::FEATURES_OK == 0 {
                    return None;
                }
                for (q, [desc, avail, used]) in RINGS.iter().enumerate() {
                    for (o, v) in [
                        (reg::QUEUE_SEL, q as u64),
                        (reg::QUEUE_NUM, SIZE.into()),
                        (reg::QUEUE_DESC_LOW, *desc),
                        (reg::QUEUE_DRIVER_LOW, *avail),
                        (reg::QUEUE_DEVICE_LOW, *used),
                        (reg::QUEUE_READY, 1),
                    ] {
                        w(o, v);
                    }
                }
                w(reg::STATUS, 15);
                Some(Self {
                    b: io::backing_with(1, RAM),
                    d,
                    p,
                    next: Mutex::new([(0, 0); 2]),
                })
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
            /// Post a chain of `(gpa, len)` buffers on `q` (device-writable on RX); its head.
            fn post(&self, q: usize, buffers: &[(u64, u32)]) -> u16 {
                let mut next = self.next.lock().unwrap();
                let (first, index) = next[q];
                for (i, &(gpa, len)) in buffers.iter().enumerate() {
                    let n = first + i as u16;
                    let more = i + 1 < buffers.len();
                    let flags = u16::from(more) | if q == 0 { 2 } else { 0 };
                    let mut d = [0; 16];
                    d[..8].copy_from_slice(&gpa.to_le_bytes());
                    d[8..12].copy_from_slice(&len.to_le_bytes());
                    d[12..14].copy_from_slice(&flags.to_le_bytes());
                    d[14..].copy_from_slice(&((n + 1) % SIZE).to_le_bytes());
                    self.put(RINGS[q][0] + u64::from(n % SIZE) * 16, &d);
                }
                self.put(
                    RINGS[q][1] + 4 + u64::from(index % SIZE) * 2,
                    &first.to_le_bytes(),
                );
                self.word(RINGS[q][1] + 2)
                    .store(index.wrapping_add(1), SeqCst);
                next[q] = ((first + buffers.len() as u16) % SIZE, index.wrapping_add(1));
                first
            }
            /// One RX buffer `i` (its own chain).
            fn post_rx(&self, i: u64) {
                self.post(0, &[(RX_BUFFERS + i * 0x800, RX_BUFFER)]);
            }
            fn kick(&self, q: u64) {
                assert!(matches!(
                    self.d.access(0, reg::QUEUE_NOTIFY, 4, Some(q)),
                    Access::Completed(0)
                ));
            }
            fn used(&self, q: usize) -> u16 {
                self.word(RINGS[q][2] + 2).load(SeqCst)
            }
            /// Used element `slot` of `q`: (head, length).
            fn element(&self, q: usize, slot: u16) -> (u32, u32) {
                let e = self.get(RINGS[q][2] + 4 + u64::from(slot % SIZE) * 8, 8);
                (
                    u32::from_le_bytes(e[..4].try_into().unwrap()),
                    u32::from_le_bytes(e[4..].try_into().unwrap()),
                )
            }
            /// Run the owner while `body` drives the guest; then stop it.
            fn run(&self, body: impl FnOnce(&Self)) {
                let b = &self.b;
                let copy =
                    move |spans: &mut [Transfer<'_>], publish: usize, permit: &CopyPermit| {
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
                    owner.join().unwrap().expect("network owner failed");
                });
            }
        }
        fn pattern(len: usize, seed: u8) -> Vec<u8> {
            (0..len)
                .map(|i| (i as u8).wrapping_mul(31).wrapping_add(seed))
                .collect()
        }
        const TSO: Gso = Gso {
            ipv6: false,
            size: 1448,
            header: 54,
        };

        #[test]
        fn a_segmentation_frame_reaches_the_backend_whole_with_its_metadata() {
            let rig = Rig::new(ALL).unwrap();
            let frame = pattern(60_000, 7);
            let mut header = [0u8; HEADER];
            header[0] = NEEDS_CSUM;
            header[1] = GSO_TCPV4;
            header[HDR_LEN].copy_from_slice(&54u16.to_le_bytes());
            header[GSO_SIZE].copy_from_slice(&1448u16.to_le_bytes());
            header[CSUM_START].copy_from_slice(&34u16.to_le_bytes());
            header[CSUM_OFFSET].copy_from_slice(&16u16.to_le_bytes());
            // The header alone, then the frame split over two buffers (as Linux's scatter-gather).
            rig.put(TX_BUFFERS, &header);
            rig.put(TX_BUFFERS + 0x100, &frame[..20_000]);
            rig.put(TX_BUFFERS + 0x8000, &frame[20_000..]);
            rig.run(|rig| {
                rig.post(
                    1,
                    &[
                        (TX_BUFFERS, HEADER as u32),
                        (TX_BUFFERS + 0x100, 20_000),
                        (TX_BUFFERS + 0x8000, 40_000),
                    ],
                );
                rig.kick(1);
                until("TX used", || rig.used(1) == 1);
                assert_eq!(rig.element(1, 0), (0, 0));
                let mut got = None;
                assert!(
                    rig.p
                        .transmit(|bytes, meta| {
                            got = Some((bytes.to_vec(), *meta));
                            Ok(())
                        })
                        .unwrap()
                );
                let (bytes, meta) = got.unwrap();
                assert!(
                    bytes == frame,
                    "the frame, untouched (no checksum completed)"
                );
                assert_eq!(
                    meta,
                    TxMeta {
                        checksum: Some((34, 16)),
                        gso: Some(TSO),
                    }
                );
            });
        }
        #[test]
        fn a_large_frame_spreads_over_mergeable_buffers_and_waits_for_more() {
            let rig = Rig::new(ALL).unwrap();
            let meta = RxMeta {
                checksum: RxChecksum::Partial {
                    start: 34,
                    offset: 16,
                },
                gso: Some(TSO),
            };
            let deliver = |rig: &Rig, seed: u8| {
                let mut one = VecDeque::from([Frame {
                    bytes: pattern(6000, seed),
                    meta,
                }]);
                assert!(rig.p.receive_from(&mut one));
            };
            // Read back frame `n` from used slots `first..first + 4`.
            let check = |rig: &Rig, first: u16, seed: u8| {
                let lens: Vec<_> = (first..first + 4).map(|s| rig.element(0, s)).collect();
                let heads: Vec<_> = lens.iter().map(|e| e.0).collect();
                assert_eq!(
                    lens.iter().map(|e| e.1).collect::<Vec<_>>(),
                    [1536, 1536, 1536, 6012 - 3 * 1536]
                );
                let mut bytes = Vec::new();
                for (h, (_, len)) in heads.iter().zip(&lens) {
                    bytes.extend(rig.get(RX_BUFFERS + u64::from(*h) * 0x800, *len as usize));
                }
                let mut header = [0u8; HEADER];
                header.copy_from_slice(&bytes[..HEADER]);
                // NEEDS_CSUM, GSO_TCPV4, hdr_len 54, gso_size 1448, csum 34/16, num_buffers 4.
                assert_eq!(header, [1, 1, 54, 0, 0xa8, 0x05, 34, 0, 16, 0, 4, 0]);
                assert!(bytes[HEADER..] == pattern(6000, seed)[..]);
            };
            rig.run(|rig| {
                for i in 0..5 {
                    rig.post_rx(i);
                }
                deliver(rig, 1);
                until("four buffers used", || rig.used(0) == 4);
                check(rig, 0, 1);
                // One buffer left, four needed: the frame waits for the driver's next buffers.
                deliver(rig, 2);
                std::thread::sleep(Duration::from_millis(50));
                assert_eq!(rig.used(0), 4, "nothing partial is published");
                assert!(rig.d.lock().rx_starved);
                for i in 5..8 {
                    rig.post_rx(i);
                }
                rig.kick(0);
                until("the held frame", || rig.used(0) == 8);
                check(rig, 4, 2);
                assert_eq!(rig.p.lost(), 0);
            });
        }
        #[test]
        fn a_large_frame_the_guest_did_not_accept_is_dropped() {
            let rig = Rig::new(ALL & !(F_GUEST_TSO4 | F_GUEST_TSO6 | F_MRG_RXBUF)).unwrap();
            rig.run(|rig| {
                rig.post_rx(0);
                let mut frames = VecDeque::from([
                    Frame {
                        bytes: pattern(6000, 3),
                        meta: RxMeta {
                            checksum: RxChecksum::Partial {
                                start: 34,
                                offset: 16,
                            },
                            gso: Some(TSO),
                        },
                    },
                    Frame {
                        bytes: pattern(100, 4),
                        meta: RxMeta {
                            checksum: RxChecksum::Valid,
                            gso: None,
                        },
                    },
                ]);
                assert!(rig.p.receive_from(&mut frames));
                assert!(frames.is_empty());
                until("the small frame", || rig.used(0) == 1);
                assert_eq!(rig.element(0, 0), (0, (HEADER + 100) as u32));
                assert_eq!(rig.p.lost(), 1, "the large one");
                assert_eq!(rig.get(RX_BUFFERS, 1)[0], DATA_VALID);
            });
        }
        #[test]
        fn with_event_idx_a_short_ring_asks_for_the_first_missing_buffer() {
            let rig = Rig::new(ALL | EVENT_IDX).unwrap();
            let avail_event = RINGS[0][2] + 4 + u64::from(SIZE) * 8;
            rig.run(|rig| {
                rig.post_rx(0);
                let mut one = VecDeque::from([Frame {
                    bytes: pattern(6000, 5),
                    meta: RxMeta {
                        checksum: RxChecksum::Partial {
                            start: 34,
                            offset: 16,
                        },
                        gso: Some(TSO),
                    },
                }]);
                assert!(rig.p.receive_from(&mut one));
                until("starved", || rig.d.lock().rx_starved);
                assert_eq!(
                    rig.word(avail_event).load(SeqCst),
                    1,
                    "notify when slot 1 is added"
                );
                assert_eq!(rig.used(0), 0);
                for i in 1..4 {
                    rig.post_rx(i);
                }
                rig.kick(0);
                until("four buffers used", || rig.used(0) == 4);
                assert_eq!(rig.p.lost(), 0);
            });
        }
        #[test]
        fn a_frame_larger_than_the_whole_ring_is_dropped_not_held() {
            let rig = Rig::new(ALL).unwrap();
            rig.run(|rig| {
                for i in 0..u64::from(SIZE) {
                    rig.post_rx(i);
                }
                // 32 buffers of 1536 hold 49152 bytes: 60000 never fit.
                let mut frames = VecDeque::from([
                    Frame {
                        bytes: pattern(60_000, 6),
                        meta: RxMeta {
                            checksum: RxChecksum::Partial {
                                start: 34,
                                offset: 16,
                            },
                            gso: Some(TSO),
                        },
                    },
                    Frame {
                        bytes: pattern(100, 7),
                        meta: RxMeta::default(),
                    },
                ]);
                assert!(rig.p.receive_from(&mut frames));
                until("the small frame", || rig.used(0) == 1);
                assert_eq!(rig.p.lost(), 1);
                assert!(!rig.d.lock().rx_starved);
            });
        }
        #[test]
        fn a_plain_backend_gets_the_checksum_completed() {
            let offloads = Offloads {
                tx_checksum: true,
                rx_checksum: true,
                ..Offloads::default()
            };
            let rig = Rig::with(VERSION_1 | F_CSUM | F_GUEST_CSUM, offloads).unwrap();
            let (frame, full) = super::partial_tcp(b"natemu payload");
            let mut header = [0u8; HEADER];
            header[0] = NEEDS_CSUM;
            header[CSUM_START].copy_from_slice(&34u16.to_le_bytes());
            header[CSUM_OFFSET].copy_from_slice(&16u16.to_le_bytes());
            rig.put(TX_BUFFERS, &[&header[..], &frame].concat());
            rig.run(|rig| {
                rig.post(1, &[(TX_BUFFERS, (HEADER + frame.len()) as u32)]);
                rig.kick(1);
                until("TX used", || rig.used(1) == 1);
                let mut seen = None;
                assert!(
                    rig.p
                        .transmit(|bytes, meta| {
                            seen = Some((u16::from_be_bytes([bytes[50], bytes[51]]), *meta));
                            Ok(())
                        })
                        .unwrap()
                );
                assert_eq!(
                    seen,
                    Some((full, TxMeta::default())),
                    "finished, no metadata"
                );
            });
        }
        #[test]
        fn segmentation_is_not_accepted_without_its_checksum_offload() {
            assert!(Rig::new(ALL & !F_CSUM).is_none(), "HOST_TSO needs CSUM");
            assert!(
                Rig::new(ALL & !F_GUEST_CSUM).is_none(),
                "GUEST_TSO needs GUEST_CSUM"
            );
            assert!(Rig::new(ALL & !(F_CSUM | F_HOST_TSO4 | F_HOST_TSO6)).is_some());
        }
    }
}
