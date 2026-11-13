//! virtio-net: one RX/TX pair and bounded owned Ethernet packets. No backend holds guest
//! addresses; the native backend is [`super::slirp`].
use super::*;
use std::collections::VecDeque;
use std::sync::{
    OnceLock, Weak,
    mpsc::{self, Receiver, SyncSender, TryRecvError, TrySendError},
};
use std::time::Instant;

/// Feature bits offered (virtio 1.3 §5.1.3): MTU, MAC and STATUS, and EVENT_IDX; checksum
/// offloads as the backend's [`Offloads`] allow. No segmentation offloads yet.
const F_CSUM: u64 = 1;
const F_GUEST_CSUM: u64 = 1 << 1;
const F_MTU: u64 = 1 << 3;
const F_MAC: u64 = 1 << 5;
const F_STATUS: u64 = 1 << 16;
pub(super) const FEATURES: u64 = VERSION_1 | EVENT_IDX | F_MTU | F_MAC | F_STATUS;
/// virtio_net_hdr for VERSION_1: flags, gso_type, hdr_len, gso_size, csum_start, csum_offset,
/// num_buffers.
const HEADER: usize = 12;
const NUM_BUFFERS: std::ops::Range<usize> = 10..12;
/// VIRTIO_NET_HDR_F_NEEDS_CSUM | DATA_VALID | RSC_INFO: meaningful only with offloads.
const OFFLOAD_FLAGS: u8 = 7;
const NEEDS_CSUM: u8 = 1;
const DATA_VALID: u8 = 2;
const CSUM_START: std::ops::Range<usize> = 6..8;
const CSUM_OFFSET: std::ops::Range<usize> = 8..10;
/// VIRTIO_NET_S_LINK_UP.
const LINK_UP: u16 = 1;
const MTU: u16 = 1500;
/// An Ethernet header.
const MIN_FRAME: usize = 14;
/// Ethernet without FCS, including one VLAN tag, MTU1500.
const MAX_FRAME: usize = 1518;

/// What the backend can take, for the device to offer the guest only that (virtio 1.3 §5.1.6).
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Offloads {
    /// TX: the guest may leave a TCP/UDP checksum partial (NEEDS_CSUM); [`PacketPort::transmit`]
    /// completes it before the backend sees the frame (CSUM).
    pub tx_checksum: bool,
    /// RX: every frame the backend delivers carries valid checksums (it built them itself), so the
    /// device marks them DATA_VALID and the guest skips verifying them (GUEST_CSUM).
    pub rx_checksum_valid: bool,
}

/// One frame in owned storage (moved between queues without copying).
pub(super) struct Packet {
    bytes: Vec<u8>,
    /// TX: a partial checksum to complete, (csum_start, csum_offset) in the frame.
    csum: Option<(usize, usize)>,
}
impl Packet {
    #[cfg(test)]
    fn new(bytes: &[u8]) -> Result<Self> {
        Self::owned(bytes.to_vec())
    }
    fn owned(bytes: Vec<u8>) -> Result<Self> {
        if !(MIN_FRAME..=MAX_FRAME).contains(&bytes.len()) {
            return Err("network frame length outside MTU1500 profile".into());
        }
        Ok(Self { bytes, csum: None })
    }
    fn bytes(&self) -> &[u8] {
        &self.bytes
    }
}
/// Frame buffers the device and its backend pass back and forth, so neither allocates per packet
/// (allocating on one thread and freeing on the other cost ~8-10% of a bidirectional stream).
#[derive(Clone, Default)]
pub struct Buffers(Arc<Mutex<Vec<Vec<u8>>>>);
impl Buffers {
    /// A buffer holding a copy of `bytes`.
    pub fn take(&self, bytes: &[u8]) -> Vec<u8> {
        let spare = self.0.lock().unwrap_or_else(|e| e.into_inner()).pop();
        let mut buffer = spare.unwrap_or_else(|| Vec::with_capacity(MAX_FRAME));
        buffer.clear();
        buffer.extend_from_slice(bytes);
        buffer
    }
    pub fn give(&self, buffer: Vec<u8>) {
        let mut spare = self.0.lock().unwrap_or_else(|e| e.into_inner());
        if spare.len() < 1024 {
            spare.push(buffer);
        }
    }
}
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
    /// Received frames dropped because the guest's buffer was too small ([`PacketPort::lost`]).
    pub(super) lost: AtomicU64,
}
impl Network {
    pub fn features(&self) -> u64 {
        let mut features = FEATURES;
        if self.offloads.tx_checksum {
            features |= F_CSUM;
        }
        if self.offloads.rx_checksum_valid {
            features |= F_GUEST_CSUM;
        }
        features
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
        let mut one = VecDeque::from([bytes.to_vec()]);
        self.receive_from(&mut one);
        one.is_empty()
    }
    /// [`Self::receive`] for each frame from the front of `frames`, under one lock: taken frames
    /// leave `frames`, and so do out-of-profile ones (dropped); the rest stay. True if any left.
    pub fn receive_from(&self, frames: &mut VecDeque<Vec<u8>>) -> bool {
        let Some(device) = self.device.upgrade() else {
            return false;
        };
        let mut s = device.lock();
        let id = s.identity(0);
        let before = frames.len();
        let mut taken = false;
        while let Some(frame) = frames.pop_front() {
            if !(MIN_FRAME..=MAX_FRAME).contains(&frame.len()) {
                continue;
            }
            if !s.current(id) || !s.accepting(0) || s.received.len() >= RX_SLOTS {
                frames.push_front(frame);
                break;
            }
            s.received.push_back((
                id,
                Packet {
                    bytes: frame,
                    csum: None,
                },
            ));
            taken = true;
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
    pub fn transmit(&self, send: impl FnOnce(&[u8]) -> Result<()>) -> Result<bool> {
        let mut packet = match self.tx.try_recv() {
            Ok(packet) => packet,
            Err(TryRecvError::Empty | TryRecvError::Disconnected) => return Ok(false),
        };
        if let Some((start, offset)) = packet.csum {
            finish_checksum(&mut packet.bytes, start, offset);
        }
        let result = send(packet.bytes());
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
            _arrival: device.irqs.busy(),
        };
        Ok((device, port))
    }
    /// RX: deliver the reserved packet into the chain. TX: copy the frame out and queue it for the
    /// backend (waiting only while that queue is full). Both then publish the used element.
    pub(super) fn net_request(
        &self,
        guest: Guest<'_>,
        chain: &Chain<'_>,
        network: &Network,
        received: Option<Packet>,
        cancel: &AtomicBool,
    ) -> Result<Request> {
        let rx = self.profile.is_rx(chain.id.queue);
        let capacity = match capacity(chain.descriptors, rx) {
            Ok(n) => n,
            Err(e) => return Ok(Request::Invalid(e)),
        };
        let mut bytes = [0u8; HEADER + MAX_FRAME];
        let len = if rx {
            let packet = received.expect("RX admission reserves a frame");
            let len = HEADER + packet.bytes.len();
            // Valid short buffers may receive smaller frames; a larger external packet is just lost.
            if capacity < len {
                network.lost.fetch_add(1, SeqCst);
                return Ok(Request::Empty);
            }
            if network.offloads.rx_checksum_valid && self.accepted(F_GUEST_CSUM) {
                bytes[0] = DATA_VALID;
            }
            bytes[NUM_BUFFERS].copy_from_slice(&1u16.to_le_bytes());
            bytes[HEADER..len].copy_from_slice(packet.bytes());
            network.buffers.give(packet.bytes);
            len
        } else {
            if !(HEADER + MIN_FRAME..=bytes.len()).contains(&capacity) {
                return Ok(Request::Invalid("network TX length".into()));
            }
            let frame = &mut bytes[..capacity];
            let extents = chain
                .descriptors
                .iter()
                .map(|d| (d.addr(), d.len() as usize));
            if !self.copy(guest, chain.id, &mut gather(extents, frame), None)? {
                return Ok(Request::Canceled);
            }
            let csum = self.accepted(F_CSUM);
            let packet = match transmit_packet(&bytes[..capacity], &network.buffers, csum) {
                Ok(p) => p,
                Err(e) => return Ok(Request::Invalid(e)),
            };
            if !self.submit(network, chain.id, packet, cancel)? {
                return Ok(Request::Canceled);
            }
            0
        };
        let writes = if rx {
            scatter(chain.descriptors, &bytes[..len])
        } else {
            Vec::with_capacity(3)
        };
        Ok(match self.complete(guest, chain, len as u32, writes)? {
            // Look at the ring again even when the index read after publication showed nothing
            // new: one capture costs less than the driver notifications it saves (November 12:
            // the exact check measured ~5% slower, 3.0/4.97 vs 3.3/5.3 Gbit/s up/down).
            Some(_) => Request::Committed { pending: true },
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
/// `csum`: the driver accepted CSUM, so NEEDS_CSUM is allowed and makes csum_start/offset active.
fn transmit_packet(bytes: &[u8], buffers: &Buffers, csum: bool) -> Result<Packet> {
    if bytes.len() < HEADER {
        return Err("short network TX header".into());
    }
    // Unknown flag bits and inactive checksum/header hints are ignored (virtio 1.3 §5.1.6.2.2).
    let (flags, gso_type) = (bytes[0], bytes[1]);
    let offload = flags & OFFLOAD_FLAGS & !(if csum { NEEDS_CSUM } else { 0 });
    if offload != 0 || gso_type != 0 || bytes[NUM_BUFFERS] != [0, 0] {
        return Err("unnegotiated network TX offload/header".into());
    }
    let mut packet = Packet::owned(buffers.take(&bytes[HEADER..]))?;
    if flags & NEEDS_CSUM != 0 {
        let field = |r: std::ops::Range<usize>| {
            usize::from(u16::from_le_bytes([bytes[r.start], bytes[r.start + 1]]))
        };
        let (start, offset) = (field(CSUM_START), field(CSUM_OFFSET));
        // The 16-bit field must lie inside the frame, after the checksummed range starts.
        if start
            .checked_add(offset)
            .and_then(|e| e.checked_add(2))
            .is_none_or(|e| e > packet.bytes.len())
        {
            return Err("network TX checksum outside the frame".into());
        }
        packet.csum = Some((start, offset));
    }
    Ok(packet)
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
        let b = Buffers::default();
        assert!(
            transmit_packet(&header(34, 16), &b, false).is_err(),
            "CSUM not accepted"
        );
        assert_eq!(
            transmit_packet(&header(34, 16), &b, true).unwrap().csum,
            Some((34, 16))
        );
        let last = frame.len() as u16 - 2;
        assert!(transmit_packet(&header(0, last), &b, true).is_ok());
        assert!(transmit_packet(&header(0, last + 1), &b, true).is_err());
        assert!(transmit_packet(&header(u16::MAX, u16::MAX), &b, true).is_err());
        let mut gso = header(34, 16);
        gso[1] = 1; // GSO_TCPV4 is not offered
        assert!(transmit_packet(&gso, &b, true).is_err());
        let mut valid = header(34, 16);
        valid[0] |= DATA_VALID; // RX-only
        assert!(transmit_packet(&valid, &b, true).is_err());
    }
    #[test]
    fn offered_features_follow_the_backend() {
        let rng = super::super::tests::device();
        for (offloads, extra) in [
            (Offloads::default(), 0),
            (
                Offloads {
                    tx_checksum: true,
                    rx_checksum_valid: false,
                },
                F_CSUM,
            ),
            (
                Offloads {
                    tx_checksum: true,
                    rx_checksum_valid: true,
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
        assert_eq!(
            transmit_packet(&bytes, &Buffers::default(), false)
                .unwrap()
                .bytes(),
            frame()
        );
        for at in [0, 1, 10, 11] {
            let mut bad = bytes.clone();
            bad[at] = 1;
            assert!(transmit_packet(&bad, &Buffers::default(), false).is_err());
        }
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
            .transmit(|bytes| {
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
        assert!(p.transmit(|_| Err("lost interface".into())).is_err());
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
}
